import asyncio
import threading

import pytest

from core.agent.approvals import ApprovalBroker
from core.agent.events import EventType, RunEventStream
from core.agent.executor import ToolExecutor
from core.agent.policy import ExecutionDecision, PolicyResult, evaluate_execution
from core.agent.tooling import (
    ToolBackend,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
    ToolRegistryError,
    ToolRisk,
    ToolStatus,
    ToolValidationError,
)


SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 100}},
    "required": ["text"],
    "additionalProperties": False,
}


def build_executor(
    tmp_path,
    policy,
    handler,
    *,
    preflight=None,
    notifier=None,
    policy_evaluator=evaluate_execution,
    preflight_timeout_seconds=2.0,
):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.echo",
            description="Echo test input",
            input_schema=SCHEMA,
            handler=handler,
            capability="test.read",
            effect=ToolEffect.READ_ONLY,
            risk=ToolRisk.LOW,
            preflight=preflight,
        )
    )
    captured = []
    events = RunEventStream("run", sinks=[captured.append])
    broker = ApprovalBroker(timeout_seconds=2)
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=events,
        approval_notifier=notifier,
    )
    executor = ToolExecutor(
        registry,
        broker,
        lambda: policy,
        policy_evaluator=policy_evaluator,
        preflight_timeout_seconds=preflight_timeout_seconds,
    )
    return executor, broker, context, captured


def test_auto_run_executes_once_through_registry_policy_and_executor(tmp_path):
    calls = []

    def handler(arguments, _context):
        calls.append(dict(arguments))
        return {"echo": arguments["text"]}

    executor, _broker, context, events = build_executor(
        tmp_path, "auto_run", handler
    )
    invocation = ToolInvocation.create(
        "test.echo", {"text": "secret input"}, invocation_id="invocation"
    )
    result = asyncio.run(executor.execute(context, invocation))

    assert result.status is ToolStatus.COMPLETED
    assert result.output == {"echo": "secret input"}
    assert calls == [{"text": "secret input"}]
    assert [event.type for event in events] == [
        EventType.TOOL_REQUESTED.value,
        EventType.TOOL_STARTED.value,
        EventType.TOOL_COMPLETED.value,
    ]
    assert all("arguments" not in event.payload for event in events)
    assert "secret input" not in str([event.as_dict() for event in events])


def test_stop_after_handler_effect_preserves_completed_result(tmp_path):
    def handler(arguments, context):
        (context.workspace_root / "done.txt").write_text(arguments["text"])
        context.cancelled.set()
        return {"written": True}

    executor, _broker, context, events = build_executor(tmp_path, "auto_run", handler)
    result = asyncio.run(executor.execute(
        context, ToolInvocation.create("test.echo", {"text": "completed"})
    ))
    assert result.status is ToolStatus.COMPLETED
    assert result.output == {"written": True}
    assert (tmp_path / "done.txt").read_text() == "completed"
    assert events[-1].type == EventType.TOOL_COMPLETED.value


def test_backend_cancellation_result_is_not_relabelled_completed(tmp_path):
    executor, _broker, context, events = build_executor(
        tmp_path, "auto_run", lambda *_args: {"cancelled": True}
    )
    result = asyncio.run(executor.execute(
        context, ToolInvocation.create("test.echo", {"text": "cancel"})
    ))
    assert result.status is ToolStatus.CANCELLED
    assert events[-1].type == EventType.TOOL_CANCELLED.value


def test_deny_all_never_reaches_handler(tmp_path):
    calls = []
    executor, _broker, context, events = build_executor(
        tmp_path, "deny_all", lambda *_args: calls.append(True)
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "policy_denies_execution"
    assert calls == []
    assert events[-1].type == EventType.TOOL_DENIED.value


def test_invalid_schema_is_hard_denied_even_in_auto_run(tmp_path):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path, "auto_run", lambda *_args: calls.append(True)
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"unknown": 1}))
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "invalid_tool_arguments"
    assert calls == []


def test_custom_policy_evaluator_cannot_override_invalid_schema(tmp_path):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: calls.append(True),
        policy_evaluator=lambda *_args: PolicyResult(
            ExecutionDecision.ALLOW,
            "forced_allow",
        ),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"unknown": 1}))
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "invalid_tool_arguments"
    assert calls == []


@pytest.mark.parametrize(
    ("policy", "expected_reason"),
    [
        ("deny_all", "policy_denies_execution"),
        ("always_ask", "approval_channel_unavailable"),
    ],
)
def test_custom_policy_evaluator_cannot_weaken_ui_policy(
    tmp_path,
    policy,
    expected_reason,
):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path,
        policy,
        lambda *_args: calls.append(True),
        policy_evaluator=lambda *_args: PolicyResult(
            ExecutionDecision.ALLOW,
            "forced_allow",
        ),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )

    assert result.status is ToolStatus.DENIED
    assert result.reason_code == expected_reason
    assert calls == []


def test_custom_policy_evaluator_can_make_ui_policy_more_restrictive(tmp_path):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: calls.append(True),
        policy_evaluator=lambda *_args: PolicyResult(
            ExecutionDecision.DENY,
            "capability_disabled",
        ),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )

    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "capability_disabled"
    assert calls == []


def test_always_ask_notifies_and_runs_only_after_bound_approval(tmp_path):
    calls = []
    approval_events = []
    broker_holder = {}

    def notifier(event):
        approval_events.append(event)
        broker_holder["broker"].resolve(
            event["approval_id"],
            approve=True,
            session_id=event["session_id"],
            generation_id=event["generation_id"],
        )

    executor, broker, context, events = build_executor(
        tmp_path,
        "always_ask",
        lambda arguments, _context: calls.append(dict(arguments)) or {"ok": True},
        notifier=notifier,
    )
    broker_holder["broker"] = broker
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "yes"}))
    )

    assert result.status is ToolStatus.COMPLETED
    assert calls == [{"text": "yes"}]
    assert len(approval_events) == 1
    assert EventType.TOOL_APPROVAL_REQUIRED.value in [event.type for event in events]


def test_always_ask_without_approval_channel_fails_closed(tmp_path):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path, "always_ask", lambda *_args: calls.append(True)
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "approval_channel_unavailable"
    assert calls == []


def test_approval_notification_failure_after_resolution_still_fails_closed(tmp_path):
    calls = []
    broker_holder = {}

    def notifier(event):
        broker_holder["broker"].resolve(
            event["approval_id"],
            approve=True,
            session_id=event["session_id"],
            generation_id=event["generation_id"],
        )
        raise RuntimeError("transport failed")

    executor, broker, context, _events = build_executor(
        tmp_path,
        "always_ask",
        lambda *_args: calls.append(True),
        notifier=notifier,
    )
    broker_holder["broker"] = broker
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "approval_notification_failed"
    assert calls == []


def test_preflight_hard_deny_precedes_auto_run(tmp_path):
    calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: calls.append(True),
        preflight=lambda *_args: "capability_disabled",
        policy_evaluator=lambda *_args: PolicyResult(
            ExecutionDecision.ALLOW,
            "forced_allow",
        ),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )
    assert result.reason_code == "capability_disabled"
    assert calls == []


def test_always_ask_approves_before_preflight(tmp_path):
    order = []
    broker_holder = {}

    def notifier(event):
        order.append("approval")
        broker_holder["broker"].resolve(
            event["approval_id"],
            approve=True,
            session_id=event["session_id"],
            generation_id=event["generation_id"],
        )

    def preflight(*_args):
        order.append("preflight")

    def handler(*_args):
        order.append("handler")

    executor, broker, context, _events = build_executor(
        tmp_path,
        "always_ask",
        handler,
        preflight=preflight,
        notifier=notifier,
    )
    broker_holder["broker"] = broker
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "yes"}))
    )

    assert result.status is ToolStatus.COMPLETED
    assert order == ["approval", "preflight", "handler"]


def test_deny_all_does_not_run_even_trusted_preflight(tmp_path):
    preflight_calls = []
    executor, _broker, context, _events = build_executor(
        tmp_path,
        "deny_all",
        lambda *_args: None,
        preflight=lambda *_args: preflight_calls.append(True),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )
    assert result.status is ToolStatus.DENIED
    assert preflight_calls == []


def test_cancellation_during_preflight_never_reaches_handler(tmp_path):
    calls = []

    def preflight(_arguments, context):
        context.cancelled.set()

    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: calls.append(True),
        preflight=preflight,
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.echo", {"text": "no"}))
    )

    assert result.status is ToolStatus.CANCELLED
    assert result.reason_code == "execution_cancelled"
    assert calls == []


def test_synchronous_preflight_does_not_block_event_loop(tmp_path):
    entered = threading.Event()
    event_loop_progressed = threading.Event()

    def preflight(*_args):
        entered.set()
        if not event_loop_progressed.wait(timeout=0.2):
            return "event_loop_blocked"

    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: {"ok": True},
        preflight=preflight,
        preflight_timeout_seconds=1,
    )

    async def scenario():
        task = asyncio.create_task(
            executor.execute(
                context,
                ToolInvocation.create("test.echo", {"text": "yes"}),
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        event_loop_progressed.set()
        return await task

    result = asyncio.run(scenario())
    assert result.status is ToolStatus.COMPLETED


def test_preflight_timeout_fails_closed(tmp_path):
    release = threading.Event()

    def preflight(*_args):
        release.wait(timeout=0.2)

    executor, _broker, context, _events = build_executor(
        tmp_path,
        "auto_run",
        lambda *_args: pytest.fail("handler must not run"),
        preflight=preflight,
        preflight_timeout_seconds=0.01,
    )
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        result = asyncio.run(
            executor.execute(
                context,
                ToolInvocation.create("test.echo", {"text": "no"}),
            )
        )
    finally:
        release.set()
        timer.cancel()

    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "tool_preflight_timeout"


@pytest.mark.parametrize(
    "overrides",
    [
        {"effect": ToolEffect.CODE_EXECUTION, "risk": ToolRisk.CRITICAL},
        {"source": "plugin"},
        {"backend": ToolBackend.MCP},
    ],
)
def test_in_process_executor_rejects_effectful_or_untrusted_tools(tmp_path, overrides):
    calls = []
    registry = ToolRegistry()
    values = {
        "name": "test.restricted",
        "description": "Restricted",
        "input_schema": {"type": "object", "additionalProperties": False},
        "handler": lambda *_args: calls.append(True),
    }
    values.update(overrides)
    registry.register(ToolDefinition(**values))
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run"),
    )
    executor = ToolExecutor(
        registry,
        ApprovalBroker(),
        lambda: "auto_run",
        policy_evaluator=lambda *_args: PolicyResult(
            ExecutionDecision.ALLOW,
            "forced_allow",
        ),
    )
    result = asyncio.run(
        executor.execute(context, ToolInvocation.create("test.restricted", {}))
    )
    assert result.status is ToolStatus.DENIED
    assert calls == []


def test_policy_is_rechecked_after_approval_before_handler(tmp_path):
    calls = []
    state = {"policy": "always_ask"}
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.echo",
            description="Echo",
            input_schema=SCHEMA,
            handler=lambda *_args: calls.append(True),
        )
    )
    broker = ApprovalBroker(timeout_seconds=1)

    def notifier(event):
        broker.resolve(
            event["approval_id"],
            approve=True,
            session_id=event["session_id"],
            generation_id=event["generation_id"],
        )
        state["policy"] = "deny_all"

    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run"),
        approval_notifier=notifier,
    )
    result = asyncio.run(
        ToolExecutor(registry, broker, lambda: state["policy"]).execute(
            context,
            ToolInvocation.create("test.echo", {"text": "no"}),
        )
    )
    assert result.status is ToolStatus.DENIED
    assert calls == []


def test_approval_preview_mutation_cannot_change_executed_arguments(tmp_path):
    nested_schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
                "additionalProperties": False,
            }
        },
        "required": ["nested"],
        "additionalProperties": False,
    }
    observed = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.nested",
            description="Nested",
            input_schema=nested_schema,
            handler=lambda arguments, _context: observed.append(arguments) or {"ok": True},
        )
    )
    broker = ApprovalBroker(timeout_seconds=1)

    def notifier(event):
        event["arguments"]["nested"]["target"] = "mutated"
        broker.resolve(
            event["approval_id"],
            approve=True,
            session_id=event["session_id"],
            generation_id=event["generation_id"],
        )

    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run"),
        approval_notifier=notifier,
    )
    result = asyncio.run(
        ToolExecutor(registry, broker, lambda: "always_ask").execute(
            context,
            ToolInvocation.create(
                "test.nested",
                {"nested": {"target": "approved"}},
            ),
        )
    )
    assert result.status is ToolStatus.COMPLETED
    assert observed[0]["nested"]["target"] == "approved"


def test_cancelling_async_notifier_consumes_pending_approval(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()

        async def notifier(_event):
            entered.set()
            await never.wait()

        executor, broker, context, _events = build_executor(
            tmp_path,
            "always_ask",
            lambda *_args: None,
            notifier=notifier,
        )
        task = asyncio.create_task(
            executor.execute(
                context,
                ToolInvocation.create("test.echo", {"text": "cancel"}),
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker._requests == {}

    asyncio.run(scenario())


def test_completed_read_only_result_survives_non_authoritative_sink_failure(tmp_path):
    calls = []

    def observer(event):
        if event.type == EventType.TOOL_COMPLETED.value:
            raise RuntimeError("observer failed")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.echo",
            description="Echo",
            input_schema=SCHEMA,
            handler=lambda *_args: calls.append(True) or {"ok": True},
        )
    )
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run", sinks=[observer]),
    )
    result = asyncio.run(
        ToolExecutor(registry, ApprovalBroker(), lambda: "auto_run").execute(
            context,
            ToolInvocation.create("test.echo", {"text": "ok"}),
        )
    )
    assert result.status is ToolStatus.COMPLETED
    assert result.reason_code == "action_succeeded_audit_failed"
    assert calls == [True]


def test_registry_rejects_duplicates_and_changes_after_freeze():
    definition = ToolDefinition(
        name="test.echo",
        description="Echo",
        input_schema=SCHEMA,
        handler=lambda *_args: None,
    )
    registry = ToolRegistry()
    registry.register(definition)
    with pytest.raises(ToolRegistryError):
        registry.register(definition)
    registry.freeze()
    with pytest.raises(ToolRegistryError):
        registry.register(
            ToolDefinition(
                name="test.other",
                description="Other",
                input_schema={"type": "object"},
                handler=lambda *_args: None,
            )
        )


def test_tool_invocation_rejects_non_json_arguments():
    with pytest.raises(ToolValidationError):
        ToolInvocation.create("test.echo", {"value": object()})


def test_tool_definition_rejects_unknown_effect_or_risk():
    with pytest.raises(ToolRegistryError):
        ToolDefinition(
            name="test.invalid",
            description="Invalid",
            input_schema={"type": "object"},
            handler=lambda *_args: None,
            effect="surprise",
        )
