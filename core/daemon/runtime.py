"""Application runtime owned by the daemon process, independent from FastAPI."""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, Callable

from core.agent.approvals import ApprovalBroker
from core.agent.coordinator import NativeSessionService, RunCoordinator, load_system_prompt
from core.agent.egress import DataEgressPolicy
from core.agent.executor import ToolExecutor
from core.agent.model_adapter import OpenAICompatibleAdapter
from core.agent.personal_memory import PersonalMemoryStore, render_memory_context
from core.agent.policy import evaluate_native_tool_execution
from core.agent.process_backend import ManagedProcessBackend
from core.agent.routine_tools import register_routine_tools
from core.agent.routines import RoutineStore, RoutineValidator
from core.agent.run_store import RunStore
from core.agent.script_tools import register_script_tools
from core.agent.system_tools import register_system_tools
from core.agent.tooling import ToolBackend, ToolEffect, ToolRegistry
from core.agent.workspace_tools import register_workspace_tools
from core.daemon.session_service import SessionBusyError, SessionService
from core.tools.settings_manager import SettingsManager
from core.vision.attachment_broker import AttachmentBroker


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

    def reset_session(self) -> dict[str, str | None]:
        """Rotate a cold session without paying the AI-runtime startup cost."""

        with self._initialization_lock:
            if self._session_service is not None:
                result = self._session_service.reset()
                self._session_id = result["session_id"]
                return result
            if self._native_service is not None:
                result = self._native_service.reset()
                self._session_id = result["session_id"]
                return result

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
            return {
                "session_id": self._session_id,
                "previous_session_id": previous_session_id,
                "invalidated_generation_id": None,
            }

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
