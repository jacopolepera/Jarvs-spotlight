"""Application runtime owned by the daemon process, independent from FastAPI."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import inspect
import json
import os
import threading
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core.agent.approvals import (
    ApprovalBroker,
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStatus,
)
from core.agent.coordinator import (
    NativeSessionService,
    RunCoordinator,
    SpecialistUnavailableError,
    load_system_prompt,
)
from core.agent.egress import DataEgressPolicy
from core.agent.executor import ToolExecutor
from core.agent.model_adapter import OpenAICompatibleAdapter
from core.agent.personal_memory import PersonalMemoryStore, render_memory_context
from core.agent.policy import (
    ExecutionDecision,
    ExecutionIntent,
    evaluate_native_tool_execution,
)
from core.agent.process_backend import ManagedProcessBackend
from core.agent.routine_tools import register_routine_tools
from core.agent.routines import RoutineStore, RoutineValidator
from core.agent.run_store import RunStore
from core.agent.script_tools import register_script_tools
from core.agent.system_tools import register_system_tools
from core.agent.tooling import (
    ToolBackend,
    ToolDefinition,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
    ToolRisk,
)
from core.agent.workspace_tools import register_workspace_tools
from core.daemon.session_service import SessionBusyError, SessionService
from core.tools.settings_manager import SettingsManager
from core.vision.attachment_broker import AttachmentBroker


HERMES_TOOL_NAME = "specialist.hermes.run"
HERMES_TOOL_VERSION = "1.0.0"
HERMES_CAPABILITIES = frozenset(
    {
        "research",
        "vision",
        "coding_fixture",
        "browser_fixture",
        "delegation_fixture",
    }
)
HERMES_EFFECTFUL_CAPABILITIES = frozenset(
    {"coding_fixture", "browser_fixture", "delegation_fixture"}
)
HERMES_FIXTURE_MARKER = ".jarvs-hermes-fixture"
HERMES_PROTOCOL_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "message.interim",
        "message.complete",
        "tool.start",
        "tool.progress",
        "tool.complete",
        "approval.request",
        "clarify.request",
        "session.info",
    }
)
HERMES_AUDIT_MAX_NODES = 512
HERMES_AUDIT_MAX_CHARS = 32_768
HERMES_MAX_STAGED_MESSAGES = 24
HERMES_MAX_STAGED_IMAGES = 8
HERMES_MAX_STAGED_BYTES = 36 * 1024 * 1024
HERMES_MAX_DISPLAY_COMMAND_CHARS = 4_000
HERMES_MAX_FINAL_TEXT_BYTES = 96 * 1024


class DaemonRuntime:
    """Own lazily initialized application state outside the HTTP transport."""

    def __init__(
        self,
        settings_manager: SettingsManager | None = None,
        *,
        engine=None,
        instruction_reloader: Callable[[object, SettingsManager], None] | None = None,
        approval_broker: ApprovalBroker | None = None,
        attachment_broker: AttachmentBroker | None = None,
        model_adapter=None,
        hermes_adapter_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.settings_manager = settings_manager or SettingsManager()
        self.approval_broker = approval_broker or ApprovalBroker()
        self.attachment_broker = attachment_broker or AttachmentBroker.from_environment()
        self._engine = engine
        self._injected_engine = engine is not None
        # ``engine`` remains injectable for deterministic protocol-adapter tests.
        # Production always uses the native coordinator below.
        self._instruction_reloader = instruction_reloader or (lambda *_args: None)
        self._session_service: SessionService | None = None
        self._native_service: NativeSessionService | None = None
        self._model_adapter = model_adapter
        self._tool_registry: ToolRegistry | None = None
        self._tool_executor: ToolExecutor | None = None
        self._hermes_adapter_factory = hermes_adapter_factory
        self._hermes_adapter = None
        self._hermes_tool_registry: ToolRegistry | None = None
        self._hermes_tool_executor: ToolExecutor | None = None
        self._hermes_requests: dict[str, dict[str, Any]] = {}
        self._hermes_active_runs: dict[str, threading.Event] = {}
        # Correlation hashes are bindings, not durable content fingerprints.
        # A per-process HMAC key prevents the audit log becoming an offline
        # dictionary oracle for short prompts or approval descriptions.
        self._hermes_binding_key = os.urandom(32)
        self.memory_store = PersonalMemoryStore(
            lambda: os.path.join(
                self.settings_manager.get_state_dir(),
                "personal_memory.sqlite3",
            )
        )
        self.run_store = RunStore(
            lambda: os.path.join(
                self.settings_manager.get_state_dir(),
                "run_audit.sqlite3",
            )
        )
        self._routine_store: RoutineStore | None = None
        self._initialization_lock = threading.RLock()
        self._session_id = uuid.uuid4().hex
        if self._engine is not None:
            self._finish_engine_initialization()

    @property
    def engine_ready(self) -> bool:
        return self._session_service is not None or self._native_service is not None

    @property
    def session_id(self) -> str:
        with self._initialization_lock:
            if self._session_service is not None:
                return self._session_service.session_id
            if self._native_service is not None:
                return self._native_service.session_id
            return self._session_id

    def get_session_service(self):
        if self._session_service is not None:
            return self._session_service
        if self._native_service is not None:
            return self._native_service
        with self._initialization_lock:
            if self._session_service is not None:
                return self._session_service
            if self._native_service is None:
                self._native_service = self._build_native_service()
        return self._native_service

    def configure_ready_engine(self, configure: Callable[[object], None]) -> bool:
        """Configure an initialized engine without racing an active stream."""

        with self._initialization_lock:
            service = self._session_service
            native = self._native_service
        if service is None and native is None:
            # A later factory reads the already-persisted settings.
            return False
        if service is not None:
            service.configure_engine(configure)
            return True
        return False

    def cancel_generation(self, session_id: str, generation_id: str) -> bool:
        """A cold stop is a no-op; never initialize a model to cancel it."""
        from core.daemon.session_service import StaleSessionError

        with self._initialization_lock:
            service = self._session_service or self._native_service
            if service is None:
                if session_id != self._session_id:
                    raise StaleSessionError(session_id, self._session_id)
                return False
            return service.cancel_generation(session_id, generation_id)

    def reset_session(self) -> dict[str, str | None]:
        """Rotate a cold session without paying the AI-runtime startup cost."""

        # Stop a specialist before waiting for the session service to quiesce.
        # In particular, a gateway blocked in startup/session.create must not
        # make reset time out while its process continues running.
        self._reset_hermes_adapter()
        with self._initialization_lock:
            session_service = self._session_service
            native_service = self._native_service
        if session_service is not None:
            result = session_service.reset()
            with self._initialization_lock:
                self._session_id = result["session_id"]
            return result
        if native_service is not None:
            result = native_service.reset()
            with self._initialization_lock:
                self._session_id = result["session_id"]
            return result

        with self._initialization_lock:
            previous_session_id = self._session_id
            self._session_id = uuid.uuid4().hex
            history_path = os.path.join(
                self.settings_manager.get_workspace_path(),
                "history.json",
            )
            try:
                os.remove(history_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"[Session] Failed to clear short-term history: {exc}")
            result = {
                "session_id": self._session_id,
                "previous_session_id": previous_session_id,
                "invalidated_generation_id": None,
            }
        return result

    def _finish_engine_initialization(self) -> None:
        # Defensive enforcement: even an injected adapter must expose its
        # confirmation intent to Jarvs' policy layer.
        self._engine.auto_run = False
        self._session_service = SessionService(
            self._engine,
            self.settings_manager,
            self._instruction_reloader,
            approval_broker=self.approval_broker,
            execution_policy_provider=self.settings_manager.get_execution_policy,
            initial_session_id=self._session_id,
        )

    def _build_native_service(self) -> NativeSessionService:
        registry = self.get_tool_registry()
        executor = ToolExecutor(
            registry,
            self.approval_broker,
            self.settings_manager.get_execution_policy,
            policy_evaluator=evaluate_native_tool_execution,
            allowed_in_process_effects=frozenset(
                {
                    ToolEffect.READ_ONLY,
                    ToolEffect.WORKSPACE_WRITE,
                    ToolEffect.ORCHESTRATION,
                }
            ),
            backend_executors={
                ToolBackend.ISOLATED_PROCESS: ManagedProcessBackend().execute,
            },
        )
        self._tool_executor = executor
        coordinator = RunCoordinator(
            settings_manager=self.settings_manager,
            model_adapter=self._model_adapter or OpenAICompatibleAdapter(),
            registry=registry,
            executor=executor,
            egress_policy=DataEgressPolicy(
                self.settings_manager.get_cloud_workspace_egress
            ),
            system_prompt_provider=lambda: load_system_prompt(self.settings_manager),
            memory_context_provider=self._memory_context,
            event_sinks=(self.run_store.append_event,),
            specialist_request_stager=self._stage_hermes_request,
            specialist_executor_provider=self.get_hermes_tool_executor,
            specialist_request_discarder=self._discard_hermes_request,
        )
        return NativeSessionService(
            coordinator,
            approval_broker=self.approval_broker,
            execution_policy_provider=self.settings_manager.get_execution_policy,
            initial_session_id=self._session_id,
        )

    def get_tool_registry(self) -> ToolRegistry:
        with self._initialization_lock:
            if self._tool_registry is None:
                registry = ToolRegistry()
                register_workspace_tools(registry)
                register_script_tools(registry)
                register_system_tools(registry)
                register_routine_tools(
                    registry,
                    self.get_routine_store,
                    self.get_tool_registry,
                    self.get_tool_executor,
                )
                registry.freeze()
                self._tool_registry = registry
            return self._tool_registry

    def get_tool_executor(self) -> ToolExecutor:
        with self._initialization_lock:
            if self._tool_executor is None:
                raise RuntimeError("The native tool executor is not initialized.")
            return self._tool_executor

    def get_hermes_tool_executor(self) -> ToolExecutor:
        """Build the specialist registry and gate only on first delegation."""

        with self._initialization_lock:
            if self._hermes_tool_executor is not None:
                return self._hermes_tool_executor
            registry = ToolRegistry()
            registry.register(
                ToolDefinition(
                    name=HERMES_TOOL_NAME,
                    description=(
                        "Delegate one explicitly selected, open-ended task to the "
                        "optional Hermes specialist runtime."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "request_id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                            "capability": {
                                "type": "string",
                                "enum": sorted(HERMES_CAPABILITIES),
                            },
                            "input_hash": {
                                "type": "string",
                                "minLength": 64,
                                "maxLength": 64,
                            },
                            "message_count": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": HERMES_MAX_STAGED_MESSAGES,
                            },
                        },
                        "required": [
                            "request_id",
                            "capability",
                            "input_hash",
                            "message_count",
                        ],
                        "additionalProperties": False,
                    },
                    # Non-in-process definitions are invoked through their
                    # backend executor; a handler remains required by the
                    # stable ToolDefinition contract.
                    handler=lambda *_args: None,
                    capability="specialist.hermes",
                    effect=ToolEffect.CODE_EXECUTION,
                    risk=ToolRisk.HIGH,
                    source="jarvs_hermes",
                    version=HERMES_TOOL_VERSION,
                    scope="local_specialist_or_disposable_fixture",
                    backend=ToolBackend.SPECIALIST,
                    preflight=self._hermes_preflight,
                )
            )
            registry.freeze()
            self._hermes_tool_registry = registry
            self._hermes_tool_executor = ToolExecutor(
                registry,
                self.approval_broker,
                self.settings_manager.get_execution_policy,
                policy_evaluator=evaluate_native_tool_execution,
                backend_executors={
                    ToolBackend.SPECIALIST: self._execute_hermes_backend,
                },
                trusted_backend_sources=frozenset({"jarvs_hermes"}),
                # Runtime attestation invokes several bounded Git checks.  The
                # generic two-second tool preflight is intentionally too short
                # for that work and cancelling asyncio.to_thread would not stop
                # the verifier thread.
                preflight_timeout_seconds=25.0,
            )
            return self._hermes_tool_executor

    def _stage_hermes_request(
        self,
        capability: str,
        messages: Sequence[Mapping[str, Any]],
        generation,
    ) -> ToolInvocation:
        settings = self.settings_manager.get_hermes_settings()
        if not settings["hermes_enabled"]:
            raise SpecialistUnavailableError("The Hermes specialist is disabled.")
        if capability not in HERMES_CAPABILITIES:
            raise SpecialistUnavailableError(
                f"Hermes capability {capability!r} is not allowed."
            )
        if not settings["hermes_runtime_path"] or not settings["hermes_home_path"]:
            raise SpecialistUnavailableError(
                "Hermes is enabled but its runtime or home path is not configured."
            )

        if (
            not isinstance(messages, Sequence)
            or isinstance(messages, (str, bytes, bytearray))
            or len(messages) > HERMES_MAX_STAGED_MESSAGES
        ):
            raise SpecialistUnavailableError(
                "The specialist request contains too many messages."
            )

        # Build a strict, bounded protocol snapshot instead of copying arbitrary
        # Mapping implementations.  This keeps model-controlled staging work
        # bounded before policy/preflight and drops fields Hermes never needs.
        normalized_messages: list[dict[str, str]] = []
        image_count = 0
        content_bytes = 0
        for item in messages:
            if not isinstance(item, Mapping):
                raise SpecialistUnavailableError(
                    "The specialist request contains an invalid message."
                )
            kind = item.get("type", "message")
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"}:
                role = "user"
            if not isinstance(content, str) or not content:
                continue
            try:
                content_bytes += len(content.encode("utf-8"))
            except UnicodeError as exc:
                raise SpecialistUnavailableError(
                    "The specialist request contains invalid text."
                ) from exc
            if content_bytes > HERMES_MAX_STAGED_BYTES:
                raise SpecialistUnavailableError(
                    "The specialist request exceeds its size limit."
                )
            if kind == "image":
                image_count += 1
                if image_count > HERMES_MAX_STAGED_IMAGES:
                    raise SpecialistUnavailableError(
                        "The specialist request contains too many images."
                    )
                image_format = item.get("format")
                if not isinstance(image_format, str) or not image_format.startswith(
                    "base64."
                ):
                    raise SpecialistUnavailableError(
                        "The specialist request contains an invalid image."
                    )
                normalized_messages.append(
                    {
                        "role": role,
                        "type": "image",
                        "format": image_format[:32],
                        "content": content,
                    }
                )
            elif kind == "message":
                normalized_messages.append(
                    {"role": role, "type": "message", "content": content}
                )
            else:
                raise SpecialistUnavailableError(
                    "The specialist request contains an unsupported message type."
                )

        # JSON round-tripping creates a private immutable-shaped snapshot and
        # catches any remaining encoding issue before approval or sidecar init.
        try:
            private_messages = json.loads(
                json.dumps(
                    normalized_messages,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SpecialistUnavailableError(
                "The specialist request contains unsupported message data."
            ) from exc
        encoded = json.dumps(
            private_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > HERMES_MAX_STAGED_BYTES:
            raise SpecialistUnavailableError(
                "The specialist request exceeds its size limit."
            )
        request_id = uuid.uuid4().hex
        input_hash = hmac.new(
            self._hermes_binding_key,
            encoded,
            hashlib.sha256,
        ).hexdigest()
        message_count = len(private_messages)
        with self._initialization_lock:
            self._hermes_requests[request_id] = {
                "capability": capability,
                "messages": private_messages,
                "session_id": generation.session_id,
                "generation_id": generation.generation_id,
                "input_hash": input_hash,
                "message_count": message_count,
            }
        return ToolInvocation.create(
            HERMES_TOOL_NAME,
            {
                "request_id": request_id,
                "capability": capability,
                "input_hash": input_hash,
                "message_count": message_count,
            },
            invocation_id=request_id,
        )

    def _discard_hermes_request(self, request_id: str) -> None:
        with self._initialization_lock:
            self._hermes_requests.pop(request_id, None)

    def _hermes_staged_request(self, request_id: str) -> dict[str, Any] | None:
        with self._initialization_lock:
            return self._hermes_requests.get(request_id)

    def _hermes_preflight(self, arguments, context) -> str | None:
        request_id = str(arguments.get("request_id") or "")
        staged = self._hermes_staged_request(request_id)
        if staged is None:
            return "hermes_request_expired"
        if (
            str(arguments.get("capability") or "") != staged["capability"]
            or str(arguments.get("input_hash") or "") != staged["input_hash"]
            or arguments.get("message_count") != staged["message_count"]
        ):
            return "hermes_request_mismatch"
        if context is not None and (
            getattr(context, "session_id", None) != staged["session_id"]
            or getattr(context, "generation_id", None) != staged["generation_id"]
        ):
            return "hermes_request_mismatch"
        settings = self.settings_manager.get_hermes_settings()
        if not settings["hermes_enabled"]:
            return "hermes_disabled"
        if not settings["hermes_runtime_path"] or not settings["hermes_home_path"]:
            return "hermes_unconfigured"
        model = self.settings_manager.get_active_model()
        if not self._hermes_uses_local_loopback_model(model):
            return "hermes_local_provider_required"
        capability = staged["capability"]
        if capability not in HERMES_CAPABILITIES:
            return "hermes_capability_not_allowed"
        workspace = Path(self.settings_manager.get_workspace_path()).resolve()
        if capability in HERMES_EFFECTFUL_CAPABILITIES:
            if not settings["hermes_allow_effectful_fixture"]:
                return "hermes_effectful_fixture_disabled"
            if not (workspace / HERMES_FIXTURE_MARKER).is_file():
                return "hermes_fixture_marker_missing"
        try:
            fingerprint = self._hermes_execution_fingerprint(
                settings=settings,
                model=model,
                workspace=workspace,
                capability=capability,
            )
            adapter = self._get_hermes_adapter(expected_settings=settings)
            request = self._build_hermes_request(
                staged,
                model=model,
                workspace_root=workspace,
            )
            cancelled = getattr(context, "cancelled", None)
            try:
                blocked = adapter.preflight(request, cancelled=cancelled)
            except TypeError:
                # Keep injected test adapters using the initial contract while
                # the production adapter receives cancellation throughout its
                # Git/attestation preflight.
                blocked = adapter.preflight(request)
        except Exception:
            return "hermes_adapter_unavailable"
        if blocked is not None:
            return str(blocked) or "hermes_preflight_failed"
        if context is not None and getattr(context, "cancelled", None) is not None:
            if context.cancelled.is_set():
                return "hermes_preflight_cancelled"

        # Bind the authorization to the exact adapter, local model, workspace,
        # marker state and settings that passed preflight.  The backend must
        # consume this immutable hand-off instead of rebuilding a request from
        # settings that may have changed while the approval was in flight.
        try:
            latest = self._current_hermes_execution_fingerprint(staged)
        except Exception:
            return "hermes_configuration_changed"
        if latest != fingerprint:
            return "hermes_configuration_changed"
        with self._initialization_lock:
            current = self._hermes_requests.get(request_id)
            if current is not staged or self._hermes_adapter is not adapter:
                return "hermes_configuration_changed"
            current["authorization"] = {
                "fingerprint": fingerprint,
                "adapter": adapter,
                "request": request,
            }
        return None

    def _hermes_uses_local_loopback_model(self, model=None) -> bool:
        """Pilot boundary: Hermes may only use the active local loopback model."""

        try:
            if model is None:
                model = self.settings_manager.get_active_model()
            if model.get("provider") != "local":
                return False
            parsed = urlparse(str(model.get("api_base") or ""))
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                return False
            hostname = (parsed.hostname or "").rstrip(".").lower()
            # Accessing ``port`` also rejects malformed/non-numeric values and
            # keeps the cheap status gate aligned with the adapter preflight.
            _ = parsed.port
            if hostname == "localhost":
                return True
            return ipaddress.ip_address(hostname).is_loopback
        except (KeyError, TypeError, ValueError):
            return False

    def _build_hermes_request(
        self,
        staged: Mapping[str, Any],
        *,
        model=None,
        workspace_root: Path | None = None,
    ):
        # Importing the protocol types is intentionally confined to the
        # specialist lane, after Jarvs policy has authorized the invocation.
        from core.agent.hermes import HermesRequest

        if model is None:
            model = self.settings_manager.get_active_model()
        if not self._hermes_uses_local_loopback_model(model):
            raise SpecialistUnavailableError(
                "The Hermes pilot requires an active local loopback provider."
            )
        hermes_provider = "lmstudio"
        base_url = str(model.get("api_base"))
        # This fixed local placeholder is not sourced from the cloud secret
        # registry and cannot disclose a user credential to the sidecar.
        api_key = "lm-studio"

        messages = []
        images = []
        for item in staged["messages"]:
            if item.get("type") == "image":
                images.append(dict(item))
                continue
            content = item.get("content")
            if isinstance(content, str) and content:
                messages.append(
                    {
                        "role": (
                            item.get("role")
                            if item.get("role") in {"user", "assistant"}
                            else "user"
                        ),
                        "content": content,
                    }
                )
        return HermesRequest(
            messages=messages,
            workspace_root=str(
                workspace_root
                if workspace_root is not None
                else Path(self.settings_manager.get_workspace_path()).resolve()
            ),
            capability=staged["capability"],
            model=str(model.get("model_string") or ""),
            provider=hermes_provider,
            base_url=base_url,
            api_key=api_key,
            images=images,
        )

    def _get_hermes_adapter(self, *, expected_settings=None):
        with self._initialization_lock:
            settings = self.settings_manager.get_hermes_settings()
            if expected_settings is not None and settings != expected_settings:
                raise SpecialistUnavailableError(
                    "Hermes configuration changed during authorization."
                )
            if self._hermes_adapter is not None:
                return self._hermes_adapter
            runtime_path = Path(settings["hermes_runtime_path"])
            python_path = None
            runtime_root = runtime_path
            if runtime_path.suffix.lower() in {".exe", ".py"}:
                runtime_root = runtime_path.parent
                if "python" in runtime_path.name.lower():
                    python_path = runtime_path

            from core.agent.hermes import HermesConfig, HermesGatewayAdapter

            config = HermesConfig(
                runtime_root=runtime_root,
                python_path=python_path,
                home_root=Path(settings["hermes_home_path"]),
            )
            factory = self._hermes_adapter_factory
            if factory is None:
                adapter = HermesGatewayAdapter(config=config)
            else:
                try:
                    adapter = factory(config=config)
                except TypeError:
                    adapter = factory(config)
            self._hermes_adapter = adapter
            return adapter

    def _hermes_execution_fingerprint(
        self,
        *,
        settings: Mapping[str, Any],
        model: Mapping[str, Any],
        workspace: Path,
        capability: str,
    ) -> str:
        marker = workspace / HERMES_FIXTURE_MARKER
        marker_metadata = None
        if capability in HERMES_EFFECTFUL_CAPABILITIES:
            try:
                stat = marker.stat()
                marker_metadata = {
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            except OSError:
                marker_metadata = None
        snapshot = {
            "enabled": settings.get("hermes_enabled") is True,
            "runtime_path": str(settings.get("hermes_runtime_path") or ""),
            "home_path": str(settings.get("hermes_home_path") or ""),
            "effectful_fixture": (
                settings.get("hermes_allow_effectful_fixture") is True
            ),
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("model_string") or ""),
            "api_base": str(model.get("api_base") or ""),
            "workspace": str(workspace),
            "capability": capability,
            "marker": marker_metadata,
        }
        return hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _current_hermes_execution_fingerprint(
        self, staged: Mapping[str, Any]
    ) -> str:
        settings = self.settings_manager.get_hermes_settings()
        model = self.settings_manager.get_active_model()
        workspace = Path(self.settings_manager.get_workspace_path()).resolve()
        return self._hermes_execution_fingerprint(
            settings=settings,
            model=model,
            workspace=workspace,
            capability=str(staged.get("capability") or ""),
        )

    def _execute_hermes_backend(self, _definition, arguments, context):
        request_id = str(arguments.get("request_id") or "")
        staged = self._hermes_staged_request(request_id)
        if staged is None:
            raise RuntimeError("The staged Hermes request has expired.")
        authorization = staged.get("authorization")
        if not isinstance(authorization, Mapping):
            raise RuntimeError("The Hermes request was not authorized.")
        try:
            latest_fingerprint = self._current_hermes_execution_fingerprint(staged)
        except Exception as exc:
            raise RuntimeError("The Hermes configuration is no longer valid.") from exc
        if latest_fingerprint != authorization.get("fingerprint"):
            raise RuntimeError("The Hermes configuration changed after authorization.")
        adapter = authorization.get("adapter")
        request = authorization.get("request")
        cancelled = getattr(context, "cancelled", None)
        if not isinstance(cancelled, threading.Event):
            raise RuntimeError("The Hermes execution context is invalid.")
        with self._initialization_lock:
            if (
                adapter is None
                or request is None
                or self._hermes_adapter is not adapter
                or self._hermes_requests.get(request_id) is not staged
            ):
                raise RuntimeError("The authorized Hermes adapter is no longer active.")
            self._hermes_active_runs[request_id] = cancelled
        outward_notifier = getattr(context, "specialist_event_notifier", None)

        def event_notifier(event):
            self._record_hermes_event(context, request_id, event)
            if callable(outward_notifier):
                outward_notifier(event)

        def interactive_handler(event):
            return self._authorize_hermes_interaction(
                context,
                request_id,
                staged["capability"],
                event,
            )

        try:
            output = adapter.execute(
                request,
                cancelled,
                event_notifier=event_notifier,
                interactive_handler=interactive_handler,
            )
        finally:
            with self._initialization_lock:
                self._hermes_active_runs.pop(request_id, None)
        if not isinstance(output, Mapping):
            raise RuntimeError("Hermes returned an invalid result.")
        final_text = str(output.get("text") or output.get("content") or "")
        if len(final_text.encode("utf-8")) > HERMES_MAX_FINAL_TEXT_BYTES:
            raise RuntimeError("Hermes returned an answer that exceeds the size limit.")
        return {
            "text": final_text,
            "session_id": str(output.get("session_id") or ""),
            "finish_reason": str(output.get("finish_reason") or "completed"),
            "cancelled": bool(output.get("cancelled")),
            "provider": str(output.get("provider") or ""),
            "model": str(output.get("model") or ""),
            "event_count": (
                min(output.get("event_count"), 10_000)
                if isinstance(output.get("event_count"), int)
                and not isinstance(output.get("event_count"), bool)
                and output.get("event_count") >= 0
                else len(output.get("events"))
                if isinstance(output.get("events"), Sequence)
                and not isinstance(output.get("events"), (str, bytes))
                else 0
            ),
        }

    def _record_hermes_event(self, context, request_id: str, event) -> None:
        if not isinstance(event, Mapping):
            return
        event_type = str(event.get("type") or "unknown")[:200]
        if event_type not in HERMES_PROTOCOL_EVENT_TYPES:
            event_type = "unknown"
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        safe_payload = self._hermes_audit_metadata(payload)
        context.events.emit(
            f"specialist.{event_type}",
            {
                **safe_payload,
                "invocation_id": request_id,
                "specialist": "hermes",
            },
        )

    @classmethod
    def _hermes_audit_metadata(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Reduce protocol events to metadata before durable audit storage."""

        allowed_keys = frozenset(
            {
                "argument_keys",
                "choices",
                "command_hash",
                "content",
                "duration_ms",
                "percent",
                "progress",
                "request_present",
                "result_chars",
                "stage",
                "state",
                "status",
            }
        )
        result: dict[str, Any] = {
            "payload_keys": sorted(
                key for key in allowed_keys if key in payload
            ),
        }
        content = payload.get("content")
        if isinstance(content, str):
            result["content_chars"] = len(content)
        for key in ("progress", "percent", "duration_ms", "result_chars"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if -1_000_000_000 <= value <= 1_000_000_000:
                    result[key] = value
        for key in ("status", "state", "stage"):
            value = payload.get(key)
            if (
                isinstance(value, str)
                and 0 < len(value) <= 64
                and all(char.isalnum() or char in "._:-" for char in value)
            ):
                result[key] = value
        if isinstance(payload.get("request_present"), bool):
            result["request_present"] = payload["request_present"]
        choices = payload.get("choices")
        if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
            result["choice_count"] = min(len(choices), 100)
        argument_keys = payload.get("argument_keys")
        if isinstance(argument_keys, Sequence) and not isinstance(
            argument_keys, (str, bytes)
        ):
            result["argument_key_count"] = min(len(argument_keys), 100)
        if isinstance(payload.get("command_hash"), str):
            result["command_present"] = True
        return result

    @classmethod
    def _redact_hermes_payload(
        cls,
        value,
        *,
        depth: int = 0,
        _budget: dict[str, int] | None = None,
    ):
        # Protocol input is bounded again in the adapter, but this layer also
        # accepts injected test adapters. Keep hashing/audit work bounded even
        # when a compromised backend provides adversarial nesting or strings.
        if _budget is None:
            _budget = {
                "nodes": HERMES_AUDIT_MAX_NODES,
                "chars": HERMES_AUDIT_MAX_CHARS,
            }
        if depth > 6 or _budget["nodes"] <= 0:
            return "[truncated]"
        _budget["nodes"] -= 1
        if isinstance(value, Mapping):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100 or _budget["nodes"] <= 0:
                    result["[truncated]"] = True
                    break
                key_text = str(key)[: min(200, _budget["chars"])]
                _budget["chars"] = max(0, _budget["chars"] - len(key_text))
                lowered = key_text.lower()
                if any(
                    marker in lowered
                    for marker in ("api_key", "authorization", "password", "secret", "token")
                ):
                    result[key_text] = "[redacted]"
                else:
                    result[key_text] = cls._redact_hermes_payload(
                        item,
                        depth=depth + 1,
                        _budget=_budget,
                    )
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for index, item in enumerate(value):
                if index >= 100 or _budget["nodes"] <= 0:
                    result.append("[truncated]")
                    break
                result.append(
                    cls._redact_hermes_payload(
                        item,
                        depth=depth + 1,
                        _budget=_budget,
                    )
                )
            return result
        if isinstance(value, str):
            allowed = min(4_096, _budget["chars"])
            result = value[:allowed]
            _budget["chars"] = max(0, _budget["chars"] - len(result))
            return result
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:500]

    def _authorize_hermes_interaction(
        self,
        context,
        request_id: str,
        capability: str,
        event,
    ) -> bool:
        if not self._hermes_authorization_is_current(request_id, capability):
            return False
        if not isinstance(event, Mapping) or event.get("type") != "approval.request":
            return False
        payload = event.get("payload") if isinstance(event, Mapping) else {}
        payload = payload if isinstance(payload, Mapping) else {}
        safe_payload = self._redact_hermes_payload(payload)
        payload_hash = hmac.new(
            self._hermes_binding_key,
            json.dumps(
                safe_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        title = self._sanitize_hermes_display_text(
            payload.get("title")
            or payload.get("description")
            or payload.get("name")
            or "action requiring approval",
            maximum=200,
        )
        title = f"Hermes: {title or 'action requiring approval'}"
        display_command = self._sanitize_hermes_display_text(
            # Only the adapter's explicitly normalized preview is displayable.
            # Never promote an arbitrary raw protocol command into the card.
            payload.get("display_command") or "",
            maximum=HERMES_MAX_DISPLAY_COMMAND_CHARS,
            preserve_lines=True,
        )
        intent = ExecutionIntent(
            source="jarvs_hermes",
            tool_name="specialist.hermes.internal_action",
            title=title,
            arguments={
                "request_id": request_id,
                "request_hash": payload_hash,
                "event_type": "approval.request",
                "language": "text",
                "code": display_command or "(Hermes did not provide a command preview.)",
            },
            capability=f"specialist.hermes.{capability}",
            effect=ToolEffect.CODE_EXECUTION.value,
            risk=ToolRisk.HIGH.value,
            scope="local_specialist_or_disposable_fixture",
            tool_version=HERMES_TOOL_VERSION,
        )
        decision = evaluate_native_tool_execution(
            self.settings_manager.get_execution_policy(), intent
        )
        if decision.decision is ExecutionDecision.DENY:
            context.events.emit(
                "specialist.approval.denied",
                {"invocation_id": request_id, "reason_code": decision.reason_code},
            )
            return False
        if context.approval_notifier is None:
            return False
        approval = None
        try:
            approval = self.approval_broker.create(
                session_id=context.session_id,
                generation_id=context.generation_id,
                intent=intent,
            )
            context.events.emit(
                "specialist.approval.required",
                {
                    "invocation_id": request_id,
                    "approval_id": approval.approval_id,
                    "request_hash": payload_hash,
                },
            )
            notified = context.approval_notifier(approval.as_event())
            if inspect.isawaitable(notified):
                asyncio.run(notified)
            status = self.approval_broker.wait(
                approval.approval_id,
                cancelled=context.cancelled,
            )
        except Exception:
            if approval is not None:
                try:
                    self.approval_broker.abandon(
                        approval.approval_id,
                        session_id=context.session_id,
                        generation_id=context.generation_id,
                    )
                except (ApprovalConflictError, ApprovalNotFoundError):
                    pass
            return False
        if status is not ApprovalStatus.APPROVED:
            return False
        if context.cancelled.is_set():
            return False
        if not self._hermes_authorization_is_current(request_id, capability):
            return False
        latest = evaluate_native_tool_execution(
            self.settings_manager.get_execution_policy(), intent
        )
        return (
            not context.cancelled.is_set()
            and latest.decision is not ExecutionDecision.DENY
        )

    @staticmethod
    def _sanitize_hermes_display_text(
        value: object,
        *,
        maximum: int,
        preserve_lines: bool = False,
    ) -> str:
        """Bound untrusted UI text and remove bidi/control spoofing characters."""

        if not isinstance(value, str):
            return ""
        cleaned = "".join(
            char
            for char in value
            if unicodedata.category(char) not in {"Cc", "Cf"}
            or (preserve_lines and char in "\n\t")
        )
        if preserve_lines:
            lines = [" ".join(line.split()) for line in cleaned.splitlines()]
            cleaned = "\n".join(line for line in lines if line)
        else:
            cleaned = " ".join(cleaned.split())
        return cleaned[:maximum]

    def _hermes_authorization_is_current(
        self, request_id: str, capability: str
    ) -> bool:
        with self._initialization_lock:
            staged = self._hermes_requests.get(request_id)
            if staged is None or staged.get("capability") != capability:
                return False
            authorization = staged.get("authorization")
            if not isinstance(authorization, Mapping):
                return False
            adapter = authorization.get("adapter")
            fingerprint = authorization.get("fingerprint")
            if (
                adapter is None
                or self._hermes_adapter is not adapter
                or request_id not in self._hermes_active_runs
            ):
                return False
        try:
            return self._current_hermes_execution_fingerprint(staged) == fingerprint
        except Exception:
            return False

    def get_hermes_status(self) -> dict[str, Any]:
        settings = self.settings_manager.get_hermes_settings()
        enabled = settings["hermes_enabled"]
        configured = bool(
            settings["hermes_runtime_path"] and settings["hermes_home_path"]
        )
        available = bool(
            configured and os.path.exists(settings["hermes_runtime_path"])
        )
        result: dict[str, Any] = {
            "enabled": enabled,
            "available": available,
            "configured": configured,
            "running": False,
            "state": (
                "disabled"
                if not enabled
                else "unconfigured"
                if not configured
                else "unavailable"
                if not available
                else "stopped"
            ),
            "version": None,
            "error": None,
            "capabilities": sorted(HERMES_CAPABILITIES),
        }
        if enabled and configured and available:
            effectful_allowed = settings["hermes_allow_effectful_fixture"]
            try:
                workspace = Path(self.settings_manager.get_workspace_path()).resolve()
                marker_present = (workspace / HERMES_FIXTURE_MARKER).is_file()
            except (OSError, TypeError, ValueError):
                marker_present = False
            if not effectful_allowed or not marker_present:
                result["capabilities"] = sorted(
                    HERMES_CAPABILITIES - HERMES_EFFECTFUL_CAPABILITIES
                )
        if enabled and not configured:
            result["error"] = "Hermes runtime and home paths are required."
        elif enabled and not available:
            result["error"] = "The configured Hermes runtime path does not exist."
        elif enabled and configured and available and not self._hermes_uses_local_loopback_model():
            result["state"] = "unavailable"
            result["error"] = "Hermes requires an active local loopback provider."
        with self._initialization_lock:
            adapter = self._hermes_adapter
        if adapter is None:
            if not (
                result["enabled"]
                and result["configured"]
                and result["available"]
                and result["error"] is None
            ):
                result["capabilities"] = []
            return result
        try:
            status = adapter.status()
            data = status.as_dict() if hasattr(status, "as_dict") else dict(status)
            result.update(
                {
                    "state": str(data.get("state") or result["state"]),
                    "running": bool(data.get("pid")) or bool(data.get("ready")),
                    "version": data.get("version") or result["version"],
                    "error": data.get("error"),
                }
            )
        except Exception as exc:
            result.update(
                {
                    "state": "error",
                    "running": False,
                    "error": type(exc).__name__,
                }
            )
        if not (
            result["enabled"]
            and result["configured"]
            and result["available"]
            and result["error"] is None
        ):
            result["capabilities"] = []
        return result

    def reconfigure_hermes(self) -> None:
        """Drop a loaded adapter so the next run reads the new configuration."""

        self._reset_hermes_adapter()

    def _reset_hermes_adapter(self) -> None:
        with self._initialization_lock:
            adapter = self._hermes_adapter
            active_cancellations = tuple(self._hermes_active_runs.values())
            self._hermes_adapter = None
            self._hermes_requests.clear()
        for cancelled in active_cancellations:
            cancelled.set()
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    def close(self) -> None:
        """Release optional sidecars owned by the daemon process."""

        self._reset_hermes_adapter()

    def get_routine_store(self) -> RoutineStore:
        with self._initialization_lock:
            if self._routine_store is None:
                self._routine_store = RoutineStore(
                    lambda: os.path.join(
                        self.settings_manager.get_state_dir(),
                        "routines.sqlite3",
                    ),
                    RoutineValidator(self.get_tool_registry()),
                )
            return self._routine_store

    def _memory_context(self, prompt: str) -> str:
        # Do not create or open a database merely because the fast path ran.
        if not self.memory_store.path.exists():
            return ""
        return render_memory_context(self.memory_store.retrieve(prompt))
