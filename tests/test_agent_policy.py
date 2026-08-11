import threading
import time

import pytest

from core.agent.approvals import (
    ApprovalBroker,
    ApprovalConflictError,
    ApprovalStatus,
)
from core.agent.policy import (
    ExecutionDecision,
    ExecutionIntent,
    ExecutionPolicy,
    evaluate_execution,
)


def intent(**overrides):
    values = {
        "source": "test",
        "tool_name": "test.echo",
        "title": "Echo",
        "arguments": {"text": "hello"},
    }
    values.update(overrides)
    return ExecutionIntent(**values)


@pytest.mark.parametrize(
    ("policy", "decision"),
    [
        (ExecutionPolicy.ALWAYS_ASK, ExecutionDecision.REQUIRE_APPROVAL),
        (ExecutionPolicy.AUTO_RUN, ExecutionDecision.ALLOW),
        (ExecutionPolicy.DENY_ALL, ExecutionDecision.DENY),
    ],
)
def test_execution_policy_matrix(policy, decision):
    assert evaluate_execution(policy, intent()).decision is decision


def test_hard_safety_denial_preempts_auto_run():
    result = evaluate_execution(
        ExecutionPolicy.AUTO_RUN,
        intent(blocked_reason="workspace_scope_escape"),
    )
    assert result.decision is ExecutionDecision.DENY
    assert result.reason_code == "workspace_scope_escape"


def test_intent_fingerprint_is_stable_and_argument_bound():
    first = intent(arguments={"language": "python", "code": "print(1)"})
    reordered = intent(arguments={"code": "print(1)", "language": "python"})
    changed = intent(arguments={"language": "python", "code": "print(2)"})
    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != changed.fingerprint


def test_intent_and_approval_preview_do_not_share_mutable_nested_arguments():
    broker = ApprovalBroker(timeout_seconds=1)
    immutable = intent(arguments={"nested": {"target": "approved"}})
    original_hash = immutable.fingerprint
    request = broker.create(
        session_id="session",
        generation_id="generation",
        intent=immutable,
    )

    preview = request.as_event()
    preview["arguments"]["nested"]["target"] = "mutated-preview"

    assert immutable.arguments["nested"]["target"] == "approved"
    assert immutable.fingerprint == original_hash
    with pytest.raises(TypeError):
        immutable.arguments["nested"]["target"] = "mutated-intent"


def test_approval_is_one_shot_and_bound_to_generation():
    broker = ApprovalBroker(timeout_seconds=1)
    request = broker.create(
        session_id="session",
        generation_id="generation",
        intent=intent(),
    )

    with pytest.raises(ApprovalConflictError):
        broker.resolve(
            request.approval_id,
            approve=True,
            session_id="session",
            generation_id="other",
        )

    assert broker.resolve(
        request.approval_id,
        approve=True,
        session_id="session",
        generation_id="generation",
    ) is ApprovalStatus.APPROVED
    assert broker.wait(request.approval_id) is ApprovalStatus.APPROVED

    with pytest.raises(ApprovalConflictError):
        broker.resolve(
            request.approval_id,
            approve=True,
            session_id="session",
            generation_id="generation",
        )


def test_wait_is_cancelled_without_approving():
    broker = ApprovalBroker(timeout_seconds=1)
    request = broker.create(
        session_id="session",
        generation_id="generation",
        intent=intent(),
    )
    cancelled = threading.Event()
    cancelled.set()
    assert broker.wait(request.approval_id, cancelled=cancelled) is ApprovalStatus.CANCELLED


def test_pending_approval_expires_closed():
    broker = ApprovalBroker(timeout_seconds=0.02)
    request = broker.create(
        session_id="session",
        generation_id="generation",
        intent=intent(),
    )
    started = time.monotonic()
    assert broker.wait(request.approval_id, poll_seconds=0.005) is ApprovalStatus.EXPIRED
    assert time.monotonic() - started < 0.5


def test_consumed_approvals_drop_sensitive_arguments_and_bound_tombstones():
    broker = ApprovalBroker(timeout_seconds=1, tombstone_limit=2)
    approval_ids = []
    for index in range(3):
        request = broker.create(
            session_id="session",
            generation_id=f"generation-{index}",
            intent=intent(arguments={"secret": f"value-{index}"}),
        )
        approval_ids.append(request.approval_id)
        broker.resolve(
            request.approval_id,
            approve=False,
            session_id=request.session_id,
            generation_id=request.generation_id,
        )
        assert broker.wait(request.approval_id) is ApprovalStatus.DENIED

    assert broker._requests == {}
    assert len(broker._terminal) == 2
    assert approval_ids[0] not in broker._terminal
    assert all(not hasattr(item, "intent") for item in broker._terminal.values())


def test_abandon_cancels_and_consumes_pending_arguments_idempotently():
    broker = ApprovalBroker(timeout_seconds=1)
    request = broker.create(
        session_id="session",
        generation_id="generation",
        intent=intent(arguments={"secret": "value"}),
    )
    assert broker.abandon(
        request.approval_id,
        session_id="session",
        generation_id="generation",
    ) is ApprovalStatus.CANCELLED
    assert broker._requests == {}
    assert broker.abandon(
        request.approval_id,
        session_id="session",
        generation_id="generation",
    ) is ApprovalStatus.CANCELLED
