"""Single policy-gated execution path for registered Jarvs tools."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Mapping
from typing import Any, Callable

from .approvals import (
    ApprovalBroker,
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStatus,
)
from .events import EventType
from .policy import (
    ExecutionDecision,
    ExecutionIntent,
    ExecutionPolicy,
    PolicyResult,
    evaluate_execution,
)
from .tooling import (
    ToolBackend,
    ToolContext,
    ToolEffect,
    ToolInvocation,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    ToolValidationError,
    handler_is_async,
    normalize_tool_output,
    validate_tool_arguments,
)


PolicyProvider = Callable[[], ExecutionPolicy | str]
PolicyEvaluator = Callable[[ExecutionPolicy | str, ExecutionIntent], PolicyResult]
BackendExecutor = Callable[[Any, Mapping[str, Any], ToolContext], Any | Awaitable[Any]]


class ToolExecutor:
    """Authorize and invoke tools through explicitly configured backends.

    The default remains intentionally narrow: only trusted, read-only handlers
    may run in process.  The application can opt specific deterministic effects
    into that backend and must inject a dedicated executor for process, MCP, or
    specialist definitions.  Registry, schema, policy, approval, preflight and
    audit ordering remain identical for every backend.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        approval_broker: ApprovalBroker,
        policy_provider: PolicyProvider,
        *,
        policy_evaluator: PolicyEvaluator = evaluate_execution,
        trusted_in_process_sources: frozenset[str] = frozenset({"jarvs_builtin"}),
        allowed_in_process_effects: frozenset[ToolEffect] = frozenset(
            {ToolEffect.READ_ONLY}
        ),
        backend_executors: Mapping[ToolBackend, BackendExecutor] | None = None,
        trusted_backend_sources: frozenset[str] = frozenset({"jarvs_builtin"}),
        approval_notification_timeout_seconds: float = 10.0,
        preflight_timeout_seconds: float = 2.0,
    ) -> None:
        if approval_notification_timeout_seconds <= 0:
            raise ValueError("approval notification timeout must be positive")
        if preflight_timeout_seconds <= 0:
            raise ValueError("preflight timeout must be positive")
        self.registry = registry
        self.approval_broker = approval_broker
        self.policy_provider = policy_provider
        self.policy_evaluator = policy_evaluator
        self.trusted_in_process_sources = trusted_in_process_sources
        self.allowed_in_process_effects = frozenset(allowed_in_process_effects)
        self.backend_executors = dict(backend_executors or {})
        self.trusted_backend_sources = trusted_backend_sources
        self.approval_notification_timeout_seconds = (
            approval_notification_timeout_seconds
        )
        self.preflight_timeout_seconds = preflight_timeout_seconds

    async def execute(
        self,
        context: ToolContext,
        invocation: ToolInvocation,
    ) -> ToolResult:
        requested_payload = {
            "invocation_id": invocation.invocation_id,
            "tool_name": invocation.tool_name,
            "arguments_hash": invocation.fingerprint,
        }
        context.events.emit(EventType.TOOL_REQUESTED, requested_payload)

        try:
            definition = self.registry.resolve(invocation.tool_name)
        except ToolNotFoundError:
            return self._denied(
                context,
                invocation,
                reason_code="unknown_tool",
            )

        immutable_arguments = invocation.arguments
        blocked_reason = self._validate_definition_backend(definition)
        if blocked_reason is not None:
            intent = self._intent(
                definition,
                invocation,
                blocked_reason=blocked_reason,
            )
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code=blocked_reason,
            )

        try:
            validate_tool_arguments(definition.input_schema, immutable_arguments)
        except ToolValidationError as exc:
            intent = self._intent(
                definition,
                invocation,
                blocked_reason=exc.reason_code,
            )
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code=exc.reason_code,
            )

        intent = self._intent(definition, invocation, blocked_reason=None)
        decision = self._evaluate_policy(intent)
        if decision.decision is ExecutionDecision.DENY:
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code=decision.reason_code,
            )

        approved = False
        if decision.decision is ExecutionDecision.REQUIRE_APPROVAL:
            approved, denial = await self._request_approval(
                context,
                invocation,
                definition,
                intent,
            )
            if not approved:
                if denial == "approval_cancelled":
                    return self._cancelled(context, invocation, intent, denial)
                return self._denied(
                    context,
                    invocation,
                    intent=intent,
                    definition=definition,
                    reason_code=denial,
                )

        if context.cancelled.is_set():
            return self._cancelled(
                context,
                invocation,
                intent,
                "execution_cancelled",
            )

        # Re-read the live policy before the preflight.  Preflights are trusted
        # and read-only, but they may still touch the filesystem, so they must
        # happen only after the current UI policy has allowed or approved the
        # concrete invocation.
        latest = self._evaluate_policy(intent)
        if latest.decision is ExecutionDecision.DENY:
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code=latest.reason_code,
            )
        if latest.decision is ExecutionDecision.REQUIRE_APPROVAL and not approved:
            approved, denial = await self._request_approval(
                context,
                invocation,
                definition,
                intent,
            )
            if not approved:
                if denial == "approval_cancelled":
                    return self._cancelled(context, invocation, intent, denial)
                return self._denied(
                    context,
                    invocation,
                    intent=intent,
                    definition=definition,
                    reason_code=denial,
                )
            latest = self._evaluate_policy(intent)
            if latest.decision is ExecutionDecision.DENY:
                return self._denied(
                    context,
                    invocation,
                    intent=intent,
                    definition=definition,
                    reason_code=latest.reason_code,
                )

        if context.cancelled.is_set():
            return self._cancelled(
                context,
                invocation,
                intent,
                "execution_cancelled",
            )

        if definition.preflight is not None:
            try:
                blocked_reason = await asyncio.wait_for(
                    asyncio.to_thread(
                        definition.preflight,
                        immutable_arguments,
                        context,
                    ),
                    timeout=self.preflight_timeout_seconds,
                )
                if blocked_reason is not None and (
                    not isinstance(blocked_reason, str) or not blocked_reason
                ):
                    blocked_reason = "invalid_tool_preflight"
            except asyncio.CancelledError:
                context.cancelled.set()
                raise
            except asyncio.TimeoutError:
                blocked_reason = "tool_preflight_timeout"
            except ToolValidationError as exc:
                blocked_reason = exc.reason_code
            except Exception:
                blocked_reason = "tool_preflight_failed"
            if context.cancelled.is_set():
                return self._cancelled(
                    context,
                    invocation,
                    intent,
                    "execution_cancelled",
                )
            if blocked_reason is not None:
                blocked_intent = self._intent(
                    definition,
                    invocation,
                    blocked_reason=blocked_reason,
                )
                return self._denied(
                    context,
                    invocation,
                    intent=blocked_intent,
                    definition=definition,
                    reason_code=blocked_reason,
                )

        # Revalidate the immutable snapshot and re-read the live UI policy at
        # the last possible point.  This catches always-ask -> deny-all changes
        # made after an approval and auto-run -> always-ask changes made while
        # the request was being prepared.
        try:
            validate_tool_arguments(definition.input_schema, immutable_arguments)
        except ToolValidationError as exc:
            return self._denied(
                context,
                invocation,
                intent=self._intent(
                    definition,
                    invocation,
                    blocked_reason=exc.reason_code,
                ),
                definition=definition,
                reason_code=exc.reason_code,
            )

        latest = self._evaluate_policy(intent)
        if latest.decision is ExecutionDecision.DENY:
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code=latest.reason_code,
            )
        if latest.decision is ExecutionDecision.REQUIRE_APPROVAL and not approved:
            approved, denial = await self._request_approval(
                context,
                invocation,
                definition,
                intent,
            )
            if not approved:
                if denial == "approval_cancelled":
                    return self._cancelled(context, invocation, intent, denial)
                return self._denied(
                    context,
                    invocation,
                    intent=intent,
                    definition=definition,
                    reason_code=denial,
                )
            latest = self._evaluate_policy(intent)
            if latest.decision is ExecutionDecision.DENY:
                return self._denied(
                    context,
                    invocation,
                    intent=intent,
                    definition=definition,
                    reason_code=latest.reason_code,
                )

        if context.cancelled.is_set():
            return self._cancelled(
                context,
                invocation,
                intent,
                "execution_cancelled",
            )

        execution_arguments = invocation.arguments_dict()
        rebound_intent = self._intent(definition, invocation, blocked_reason=None)
        if rebound_intent.fingerprint != intent.fingerprint:
            return self._denied(
                context,
                invocation,
                intent=intent,
                definition=definition,
                reason_code="invocation_changed_after_authorization",
            )

        event_payload = self._event_payload(invocation, definition, intent)
        context.events.emit(EventType.TOOL_STARTED, event_payload)
        started = time.perf_counter()
        try:
            output = await self._invoke_definition(
                definition,
                execution_arguments,
                context,
            )
            normalized_output = normalize_tool_output(output)
        except asyncio.CancelledError:
            context.cancelled.set()
            self._emit_best_effort(
                context,
                EventType.TOOL_CANCELLED,
                {**event_payload, "reason_code": "execution_task_cancelled"},
            )
            raise
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            if context.cancelled.is_set():
                return self._cancelled(
                    context,
                    invocation,
                    intent,
                    "execution_cancelled",
                    duration_ms=duration_ms,
                )
            self._emit_best_effort(
                context,
                EventType.TOOL_FAILED,
                {
                    **event_payload,
                    "reason_code": "tool_handler_failed",
                    "error_type": type(exc).__name__,
                    "duration_ms": round(duration_ms, 3),
                },
            )
            return ToolResult(
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool_name,
                status=ToolStatus.FAILED,
                reason_code="tool_handler_failed",
                error=type(exc).__name__,
                duration_ms=duration_ms,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        # A returned result confirms that the handler finished. Stop may have
        # arrived after its effect (for example an atomic file replacement);
        # relabelling success as cancellation would hide that completed effect.
        # Interruptible backends raise when they actually stop an operation.
        if isinstance(normalized_output, Mapping) and normalized_output.get("cancelled") is True:
            return self._cancelled(
                context, invocation, intent, "execution_cancelled", duration_ms=duration_ms
            )

        audit_reason = None
        try:
            context.events.emit(
                EventType.TOOL_COMPLETED,
                {**event_payload, "duration_ms": round(duration_ms, 3)},
            )
        except Exception:
            # The read-only action completed.  Do not present it as unexecuted
            # and invite a retry merely because a non-authoritative observer
            # failed.  Effectful executors will require a transactional outbox.
            audit_reason = "action_succeeded_audit_failed"
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            status=ToolStatus.COMPLETED,
            output=normalized_output,
            reason_code=audit_reason,
            duration_ms=duration_ms,
        )

    def _evaluate_policy(self, intent: ExecutionIntent) -> PolicyResult:
        """Compose UI policy and capability overrides without weakening UI.

        The global policy is the authority selected by the user.  A custom
        evaluator may impose a stricter capability/tool rule, but it cannot
        turn ``deny_all`` into execution or bypass ``always_ask``.
        """

        try:
            policy = self.policy_provider()
            baseline = evaluate_execution(policy, intent)
        except Exception:
            return PolicyResult(
                ExecutionDecision.DENY,
                "policy_provider_failed",
            )

        if baseline.decision is ExecutionDecision.DENY:
            return baseline

        try:
            override = self.policy_evaluator(policy, intent)
        except Exception:
            return PolicyResult(
                ExecutionDecision.DENY,
                "policy_evaluator_failed",
            )
        if (
            not isinstance(override, PolicyResult)
            or not isinstance(override.decision, ExecutionDecision)
            or not isinstance(override.reason_code, str)
            or not override.reason_code
        ):
            return PolicyResult(
                ExecutionDecision.DENY,
                "invalid_policy_result",
            )

        strictness = {
            ExecutionDecision.ALLOW: 0,
            ExecutionDecision.REQUIRE_APPROVAL: 1,
            ExecutionDecision.DENY: 2,
        }
        if strictness[override.decision] > strictness[baseline.decision]:
            return override
        if strictness[override.decision] < strictness[baseline.decision]:
            return baseline
        return override

    def _validate_definition_backend(self, definition) -> str | None:
        if definition.backend is ToolBackend.IN_PROCESS:
            if definition.effect not in self.allowed_in_process_effects:
                return "isolated_executor_required"
            if definition.source not in self.trusted_in_process_sources:
                return "untrusted_in_process_source"
            return None
        if definition.backend not in self.backend_executors:
            return "isolated_executor_required"
        if definition.source not in self.trusted_backend_sources:
            return "untrusted_backend_source"
        return None

    async def _invoke_definition(
        self,
        definition,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> Any:
        if definition.backend is ToolBackend.IN_PROCESS:
            handler = definition.handler
            call_arguments = (arguments, context)
        else:
            handler = self.backend_executors[definition.backend]
            call_arguments = (definition, arguments, context)
        if handler_is_async(handler):
            return await handler(*call_arguments)
        output = await asyncio.to_thread(handler, *call_arguments)
        if inspect.isawaitable(output):
            return await output
        return output

    @staticmethod
    def _intent(
        definition,
        invocation: ToolInvocation,
        *,
        blocked_reason: str | None,
    ) -> ExecutionIntent:
        return ExecutionIntent(
            source=definition.source,
            tool_name=definition.name,
            title=definition.description,
            arguments=invocation.arguments,
            capability=definition.capability,
            effect=definition.effect.value,
            risk=definition.risk.value,
            scope=definition.scope,
            tool_version=definition.version,
            blocked_reason=blocked_reason,
        )

    async def _request_approval(
        self,
        context: ToolContext,
        invocation: ToolInvocation,
        definition,
        intent: ExecutionIntent,
    ) -> tuple[bool, str]:
        if context.approval_notifier is None:
            return False, "approval_channel_unavailable"
        try:
            request = self.approval_broker.create(
                session_id=context.session_id,
                generation_id=context.generation_id,
                intent=intent,
            )
        except ApprovalConflictError:
            return False, "approval_capacity_exceeded"

        try:
            context.events.emit(
                EventType.TOOL_APPROVAL_REQUIRED,
                {
                    **self._event_payload(invocation, definition, intent),
                    "approval_id": request.approval_id,
                },
            )
        except BaseException:
            self._abandon_approval(context, request.approval_id)
            raise

        try:
            await asyncio.wait_for(
                self._notify_approval(context, request.as_event()),
                timeout=self.approval_notification_timeout_seconds,
            )
            status = await asyncio.to_thread(
                self.approval_broker.wait,
                request.approval_id,
                cancelled=context.cancelled,
            )
        except asyncio.CancelledError:
            context.cancelled.set()
            self._abandon_approval(context, request.approval_id)
            raise
        except (ApprovalNotFoundError, asyncio.TimeoutError):
            self._abandon_approval(context, request.approval_id)
            return False, "approval_notification_failed"
        except Exception:
            self._abandon_approval(context, request.approval_id)
            return False, "approval_notification_failed"

        if status is ApprovalStatus.APPROVED:
            return True, ""
        if status is ApprovalStatus.CANCELLED:
            return False, "approval_cancelled"
        return False, f"approval_{status.value}"

    async def _notify_approval(
        self,
        context: ToolContext,
        event: dict,
    ) -> None:
        notifier = context.approval_notifier
        if notifier is None:
            return
        if inspect.iscoroutinefunction(notifier):
            result = await notifier(event)
        else:
            result = await asyncio.to_thread(notifier, event)
        if inspect.isawaitable(result):
            await result

    def _abandon_approval(self, context: ToolContext, approval_id: str) -> None:
        try:
            self.approval_broker.abandon(
                approval_id,
                session_id=context.session_id,
                generation_id=context.generation_id,
            )
        except (ApprovalConflictError, ApprovalNotFoundError):
            pass

    @staticmethod
    def _event_payload(
        invocation: ToolInvocation,
        definition,
        intent: ExecutionIntent,
    ) -> dict:
        return {
            "invocation_id": invocation.invocation_id,
            "tool_name": invocation.tool_name,
            "invocation_hash": intent.fingerprint,
            "arguments_hash": invocation.fingerprint,
            "capability": definition.capability,
            "effect": definition.effect.value,
            "risk": definition.risk.value,
            "scope": definition.scope,
            "tool_version": definition.version,
        }

    @classmethod
    def _denied(
        cls,
        context: ToolContext,
        invocation: ToolInvocation,
        *,
        reason_code: str,
        intent: ExecutionIntent | None = None,
        definition=None,
        status: ToolStatus = ToolStatus.DENIED,
    ) -> ToolResult:
        payload = {
            "invocation_id": invocation.invocation_id,
            "tool_name": invocation.tool_name,
            "arguments_hash": invocation.fingerprint,
            "reason_code": reason_code,
        }
        if definition is not None and intent is not None:
            payload.update(cls._event_payload(invocation, definition, intent))
            payload["reason_code"] = reason_code
        context.events.emit(EventType.TOOL_DENIED, payload)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            status=status,
            reason_code=reason_code,
        )

    @classmethod
    def _cancelled(
        cls,
        context: ToolContext,
        invocation: ToolInvocation,
        intent: ExecutionIntent,
        reason_code: str,
        *,
        duration_ms: float = 0.0,
    ) -> ToolResult:
        payload = {
            "invocation_id": invocation.invocation_id,
            "tool_name": invocation.tool_name,
            "invocation_hash": intent.fingerprint,
            "arguments_hash": invocation.fingerprint,
            "reason_code": reason_code,
        }
        if duration_ms:
            payload["duration_ms"] = round(duration_ms, 3)
        cls._emit_best_effort(context, EventType.TOOL_CANCELLED, payload)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            status=ToolStatus.CANCELLED,
            reason_code=reason_code,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _emit_best_effort(context: ToolContext, type: EventType, payload: dict) -> None:
        try:
            context.events.emit(type, payload)
        except Exception:
            pass
