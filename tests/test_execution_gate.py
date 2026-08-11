import threading

from core.agent.approvals import ApprovalBroker
from core.daemon.session_service import SessionService


class FakeSettings:
    def __init__(self, workspace, policy):
        self.workspace = str(workspace)
        self.policy = policy

    def get_workspace_path(self):
        return self.workspace

    def get_execution_policy(self):
        return self.policy


class ConfirmationEngine:
    def __init__(self):
        self.messages = []
        self.executed = False
        self.generator_closed = False
        self.auto_run = True

    def chat(self, messages, stream, display):
        assert stream is True and display is False
        self.messages.extend(messages)
        try:
            yield {
                "role": "assistant",
                "type": "code",
                "format": "python",
                "content": "print('safe probe')",
            }
            yield {
                "role": "computer",
                "type": "confirmation",
                "format": "execution",
                "content": {
                    "type": "code",
                    "format": "python",
                    "content": "print('safe probe')",
                },
            }
            self.executed = True
            yield {
                "role": "computer",
                "type": "console",
                "format": "output",
                "content": "safe probe\n",
            }
        finally:
            self.generator_closed = True


class CapturingBroker(ApprovalBroker):
    def __init__(self):
        super().__init__(timeout_seconds=2)
        self.created = threading.Event()
        self.request = None

    def create(self, **kwargs):
        self.request = super().create(**kwargs)
        self.created.set()
        return self.request


def run_stream(tmp_path, policy, broker=None):
    engine = ConfirmationEngine()
    settings = FakeSettings(tmp_path, policy)
    service = SessionService(
        engine,
        settings,
        lambda *_args: None,
        approval_broker=broker,
    )
    generation = service.start_generation(service.session_id, "generation")
    return engine, settings, service, generation


def test_auto_run_advances_suspended_generator(tmp_path):
    engine, _settings, service, generation = run_stream(tmp_path, "auto_run")
    chunks = list(service.stream(generation, [{"role": "user", "type": "message", "content": "go"}]))
    assert engine.auto_run is True  # the fake's flag is irrelevant to the central gate
    assert engine.executed is True
    assert any(chunk.get("type") == "console" for chunk in chunks)


def test_deny_all_closes_before_side_effect(tmp_path):
    engine, _settings, service, generation = run_stream(tmp_path, "deny_all")
    chunks = list(service.stream(generation, [{"role": "user", "type": "message", "content": "go"}]))
    assert engine.executed is False
    assert engine.generator_closed is True
    assert chunks[-1]["type"] == "execution_denied"


def test_always_ask_does_not_advance_until_one_shot_approval(tmp_path):
    broker = CapturingBroker()
    engine, _settings, service, generation = run_stream(tmp_path, "always_ask", broker)
    chunks = []
    worker = threading.Thread(
        target=lambda: chunks.extend(
            service.stream(
                generation,
                [{"role": "user", "type": "message", "content": "go"}],
            )
        ),
        daemon=True,
    )
    worker.start()
    assert broker.created.wait(timeout=1)
    assert engine.executed is False
    request = broker.request
    broker.resolve(
        request.approval_id,
        approve=True,
        session_id=request.session_id,
        generation_id=request.generation_id,
    )
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert engine.executed is True
    assert sum(chunk.get("type") == "approval_required" for chunk in chunks) == 1


def test_reset_cancels_pending_approval_and_never_executes(tmp_path):
    broker = CapturingBroker()
    engine, _settings, service, generation = run_stream(tmp_path, "always_ask", broker)
    worker = threading.Thread(
        target=lambda: list(
            service.stream(
                generation,
                [{"role": "user", "type": "message", "content": "go"}],
            )
        ),
        daemon=True,
    )
    worker.start()
    assert broker.created.wait(timeout=1)
    result = service.reset(wait_timeout=1)
    worker.join(timeout=1)
    assert result["invalidated_generation_id"] == "generation"
    assert engine.executed is False
    assert not worker.is_alive()


def test_switching_to_deny_wakes_existing_pending_request(tmp_path):
    broker = CapturingBroker()
    engine, settings, service, generation = run_stream(tmp_path, "always_ask", broker)
    worker = threading.Thread(
        target=lambda: list(
            service.stream(
                generation,
                [{"role": "user", "type": "message", "content": "go"}],
            )
        ),
        daemon=True,
    )
    worker.start()
    assert broker.created.wait(timeout=1)
    settings.policy = "deny_all"
    assert broker.deny_pending() == 1
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert engine.executed is False


def test_gate_rechecks_policy_after_approval(tmp_path):
    class FlipAfterApprovalBroker(CapturingBroker):
        def __init__(self):
            super().__init__()
            self.settings = None

        def wait(self, *args, **kwargs):
            status = super().wait(*args, **kwargs)
            self.settings.policy = "deny_all"
            return status

    broker = FlipAfterApprovalBroker()
    engine, settings, service, generation = run_stream(
        tmp_path, "always_ask", broker
    )
    broker.settings = settings
    chunks = []
    worker = threading.Thread(
        target=lambda: chunks.extend(
            service.stream(
                generation,
                [{"role": "user", "type": "message", "content": "go"}],
            )
        ),
        daemon=True,
    )
    worker.start()
    assert broker.created.wait(timeout=1)
    request = broker.request
    broker.resolve(
        request.approval_id,
        approve=True,
        session_id=request.session_id,
        generation_id=request.generation_id,
    )
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert engine.executed is False
    assert chunks[-1]["type"] == "execution_denied"
    assert chunks[-1]["content"]["reason_code"] == "policy_denies_execution"


def test_auto_run_rechecks_live_policy_before_advancing(tmp_path):
    engine = ConfirmationEngine()
    settings = FakeSettings(tmp_path, "auto_run")
    calls = {"count": 0}

    def policy_provider():
        calls["count"] += 1
        return "auto_run" if calls["count"] == 1 else "deny_all"

    service = SessionService(
        engine,
        settings,
        lambda *_args: None,
        execution_policy_provider=policy_provider,
    )
    generation = service.start_generation(service.session_id, "generation")
    chunks = list(
        service.stream(
            generation,
            [{"role": "user", "type": "message", "content": "go"}],
        )
    )

    assert engine.executed is False
    assert chunks[-1]["type"] == "execution_denied"
