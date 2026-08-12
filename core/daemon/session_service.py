"""Session lifecycle for an injected streaming-engine protocol adapter.

The adapter is stateful and cannot be used concurrently. This service assigns
it a single owner, explicit session and generation identifiers, and an atomic
reset boundary while a model call unwinds in a worker thread.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

from core.agent.approvals import ApprovalBroker, ApprovalStatus
from core.agent.routing import RoutingSignal
from core.agent.tooling import ToolInvocation
from core.agent.policy import (
    ExecutionDecision,
    ExecutionIntent,
    ExecutionPolicy,
    evaluate_execution,
)


class SessionError(RuntimeError):
    """Base class for errors that can be translated by a transport adapter."""

    code = "session_error"


class StaleSessionError(SessionError):
    """The client tried to use a session that is no longer current."""

    code = "stale_session"

    def __init__(self, requested_session_id: str, current_session_id: str):
        super().__init__("The requested session is no longer active.")
        self.requested_session_id = requested_session_id
        self.current_session_id = current_session_id


class SessionBusyError(SessionError):
    """The stateful engine is still finishing another operation."""

    code = "session_busy"


@dataclass
class Generation:
    """A generation lease issued by :class:`SessionService`."""

    session_id: str
    generation_id: str
    routing_signal: RoutingSignal = field(default_factory=RoutingSignal)
    tool_invocation: ToolInvocation | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type", "message") == "message":
            value = block.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def summarize_snapshot(messages: Sequence[dict[str, Any]]) -> str:
    """Build a bounded, deterministic session summary without touching the engine.

    Asking the active engine to summarize its own history after reset races
    with the next conversation because the engine is a mutable singleton. The
    default records a compact transcript-style summary instead. An isolated
    summarizer can be injected through
    ``SessionService(..., snapshot_summarizer=...)``.
    """

    bullets: list[str] = []
    for message in messages[-16:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = " ".join(_message_text(message).split())
        if not text:
            continue
        if len(text) > 800:
            text = f"{text[:797]}..."
        label = "User" if role == "user" else "Jarvs Spotlight"
        bullets.append(f"- {label}: {text}")
    return "\n".join(bullets)


class SessionService:
    """Own the short-term context and generation lifecycle for one engine."""

    def __init__(
        self,
        engine: Any,
        settings_manager: Any,
        reload_instructions: Callable[[Any, Any], None],
        *,
        snapshot_summarizer: Callable[[Sequence[dict[str, Any]]], str] = summarize_snapshot,
        snapshot_archiver: Callable[[Sequence[dict[str, Any]]], None] | None = None,
        approval_broker: ApprovalBroker | None = None,
        execution_policy_provider: Callable[[], str] | None = None,
        initial_session_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.settings_manager = settings_manager
        self._reload_instructions = reload_instructions
        self._snapshot_summarizer = snapshot_summarizer
        self._snapshot_archiver = snapshot_archiver
        self.approval_broker = approval_broker or ApprovalBroker()
        self._execution_policy_provider = execution_policy_provider or getattr(
            settings_manager,
            "get_execution_policy",
            lambda: ExecutionPolicy.ALWAYS_ASK.value,
        )

        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._engine_lock = threading.Lock()
        self._session_id = initial_session_id or uuid.uuid4().hex
        self._active_generation: Generation | None = None
        self._reset_pending = False

    @property
    def session_id(self) -> str:
        with self._state_lock:
            return self._session_id

    def start_generation(
        self,
        requested_session_id: str | None,
        requested_generation_id: str | None = None,
        *,
        routing_signal: RoutingSignal | None = None,
        tool_invocation: ToolInvocation | None = None,
    ) -> Generation:
        """Validate a request and reserve the singleton engine.

        Missing IDs remain supported temporarily for older clients. New clients
        should always send both values received/generated by the protocol.
        """

        with self._state_lock:
            effective_session_id = requested_session_id or self._session_id
            if effective_session_id != self._session_id:
                raise StaleSessionError(effective_session_id, self._session_id)
            if self._active_generation is not None or self._reset_pending:
                raise SessionBusyError("The previous generation is still stopping.")

            generation = Generation(
                session_id=self._session_id,
                generation_id=requested_generation_id or uuid.uuid4().hex,
                routing_signal=routing_signal or RoutingSignal(),
                tool_invocation=tool_invocation,
            )
            self._active_generation = generation
            return generation

    def is_current(self, generation: Generation) -> bool:
        with self._state_lock:
            return (
                self._active_generation is generation
                and generation.session_id == self._session_id
                and not generation.cancelled.is_set()
            )

    def configure_engine(self, configure: Callable[[Any], None]) -> None:
        """Apply a runtime configuration only between engine generations."""

        with self._engine_lock:
            configure(self.engine)

    def stream(
        self,
        generation: Generation,
        messages: Iterable[dict[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        """Run one engine generation and yield raw adapter chunks."""

        snapshot: list[dict[str, Any]] = []
        invalidated = False

        with self._engine_lock:
            try:
                if not self.is_current(generation):
                    return

                chunks = self.engine.chat(list(messages), stream=True, display=False)
                for chunk in chunks:
                    if not self.is_current(generation):
                        invalidated = True
                        close = getattr(chunks, "close", None)
                        if callable(close):
                            close()
                        break
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("type") != "confirmation":
                        yield chunk
                        continue

                    intent = self._execution_intent(chunk)
                    policy_result = evaluate_execution(
                        self._execution_policy_provider(),
                        intent,
                    )
                    if policy_result.decision is ExecutionDecision.ALLOW:
                        # Re-read the live UI policy at the last possible point;
                        # a settings update must not leave a stale auto-run
                        # decision queued for execution.
                        policy_result = evaluate_execution(
                            self._execution_policy_provider(),
                            intent,
                        )
                    if policy_result.decision is ExecutionDecision.ALLOW:
                        # The generator is suspended immediately before the
                        # adapter's execution call. Advancing is the explicit
                        # auto-run action.
                        continue

                    if policy_result.decision is ExecutionDecision.REQUIRE_APPROVAL:
                        request = self.approval_broker.create(
                            session_id=generation.session_id,
                            generation_id=generation.generation_id,
                            intent=intent,
                        )
                        yield request.as_event()
                        status = self.approval_broker.wait(
                            request.approval_id,
                            cancelled=generation.cancelled,
                        )
                        if (
                            status is ApprovalStatus.APPROVED
                            and self.is_current(generation)
                        ):
                            latest = evaluate_execution(
                                self._execution_policy_provider(),
                                intent,
                            )
                            if latest.decision is not ExecutionDecision.DENY:
                                continue
                            reason = latest.reason_code
                        else:
                            reason = f"approval_{status.value}"
                    else:
                        reason = policy_result.reason_code

                    close = getattr(chunks, "close", None)
                    if callable(close):
                        close()
                    self._record_execution_denial(reason)
                    yield {
                        "role": "jarvs",
                        "type": "execution_denied",
                        "content": {
                            "reason_code": reason,
                            "tool_name": intent.tool_name,
                            "invocation_hash": intent.fingerprint,
                        },
                    }
                    break
            finally:
                with self._state_lock:
                    invalidated = (
                        invalidated
                        or generation.cancelled.is_set()
                        or generation.session_id != self._session_id
                    )

                if invalidated:
                    snapshot = self._copy_engine_messages()
                    self.engine.messages = []
                    self._archive_snapshot(snapshot)
                    self._reload_current_instructions()

                with self._state_changed:
                    if self._active_generation is generation:
                        self._active_generation = None
                    if invalidated:
                        self._reset_pending = False
                    self._state_changed.notify_all()

    @staticmethod
    def _execution_intent(chunk: dict[str, Any]) -> ExecutionIntent:
        content = chunk.get("content")
        if not isinstance(content, dict):
            return ExecutionIntent(
                source="streaming_engine_adapter",
                tool_name="adapter.execute_code",
                title="Run code proposed by the streaming adapter",
                blocked_reason="invalid_execution_intent",
            )

        language = content.get("format")
        code = content.get("content")
        blocked_reason = None
        if not isinstance(language, str) or not isinstance(code, str) or not code.strip():
            blocked_reason = "invalid_execution_intent"
        return ExecutionIntent(
            source="streaming_engine_adapter",
            tool_name="adapter.execute_code",
            title=f"Run {language or 'unknown'} code",
            arguments={"language": language or "", "code": code or ""},
            blocked_reason=blocked_reason,
        )

    def _record_execution_denial(self, reason_code: str) -> None:
        messages = getattr(self.engine, "messages", None)
        if not isinstance(messages, list):
            return
        messages.append(
            {
                "role": "computer",
                "type": "console",
                "format": "output",
                "content": f"Execution was not run ({reason_code}).",
            }
        )

    def finish_generation(self, generation: Generation) -> None:
        """Release a lease if setup failed before :meth:`stream` took ownership."""

        finalize_reset = False
        with self._state_changed:
            if self._active_generation is generation:
                self._active_generation = None
                finalize_reset = (
                    self._reset_pending
                    or generation.cancelled.is_set()
                    or generation.session_id != self._session_id
                )
            if not finalize_reset:
                self._state_changed.notify_all()

        if finalize_reset:
            self._finalize_reset()

    def reset(self, wait_timeout: float = 10.0) -> dict[str, str | None]:
        """Atomically create a fresh logical session and invalidate active work.

        The method returns only after the singleton engine has released the old
        generation, archived its detached snapshot, cleared short-term history,
        and reloaded instructions. This prevents the UI from accepting the first
        prompt of a new session while the old one is still unwinding.
        """

        deadline = time.monotonic() + wait_timeout
        with self._state_changed:
            while self._reset_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._state_changed.wait(remaining):
                    raise SessionBusyError("The previous session is still resetting.")

            old_session_id = self._session_id
            invalidated_generation_id: str | None = None
            generation = self._active_generation
            self._reset_pending = True
            if generation is not None:
                invalidated_generation_id = generation.generation_id
                generation.cancelled.set()

            self._session_id = uuid.uuid4().hex

        self._delete_history()

        if generation is None:
            self._finalize_reset()
        else:
            with self._state_changed:
                while self._reset_pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._state_changed.wait(remaining):
                        raise SessionBusyError(
                            "The cancelled generation has not stopped yet."
                        )

        return {
            "session_id": self.session_id,
            "previous_session_id": old_session_id,
            "invalidated_generation_id": invalidated_generation_id,
        }

    def _finalize_reset(self) -> None:
        try:
            with self._engine_lock:
                self._clear_archive_and_reload()
        finally:
            with self._state_changed:
                self._reset_pending = False
                self._state_changed.notify_all()

    def _clear_archive_and_reload(self) -> None:
        """Finalize reset while the caller exclusively owns the engine."""

        try:
            snapshot = self._copy_engine_messages()
            self.engine.messages = []
            self._archive_snapshot(snapshot)
            self._reload_current_instructions()
        finally:
            # Clearing the mutable short-term context is the non-negotiable part
            # of reset, even if snapshot persistence or instruction reload fail.
            self.engine.messages = []

    def _copy_engine_messages(self) -> list[dict[str, Any]]:
        messages = getattr(self.engine, "messages", [])
        if not isinstance(messages, list):
            return []
        try:
            return copy.deepcopy(messages)
        except Exception:
            return [dict(message) for message in messages if isinstance(message, dict)]

    def _delete_history(self) -> None:
        history_file = os.path.join(
            self.settings_manager.get_workspace_path(), "history.json"
        )
        try:
            os.remove(history_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[Session] Failed to clear short-term history: {exc}")

    def _reload_current_instructions(self) -> None:
        try:
            self._reload_instructions(self.engine, self.settings_manager)
        except Exception as exc:
            print(f"[Session] Failed to reload instructions: {exc}")

    def _archive_snapshot(self, snapshot: Sequence[dict[str, Any]]) -> None:
        """Invoke an explicitly supplied archiver; default reset stores nothing."""

        if self._snapshot_archiver is None or len(snapshot) <= 2:
            return
        try:
            self._snapshot_archiver(copy.deepcopy(snapshot))
        except Exception as exc:
            print(f"[Session] Explicit snapshot archive failed: {exc}")
