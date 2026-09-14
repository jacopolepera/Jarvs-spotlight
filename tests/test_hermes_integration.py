import json
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.agent.routing import RoutingSignal
from core.agent.tooling import ToolBackend, ToolEffect, ToolRisk
from core.daemon.runtime import DaemonRuntime, HERMES_TOOL_NAME
from core.daemon.security import DaemonSecurity
from core.daemon.server import create_app
from core.tools.settings_manager import SettingsManager


TOKEN = "hermes-test-token"
ORIGIN = "http://tauri.localhost"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}


def _manager(tmp_path, *, policy="auto_run", enabled=True, effectful=False):
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "models": [
                    {
                        "id": "local",
                        "name": "Local",
                        "provider": "local",
                        "model_string": "openai/test-model",
                        "api_key": "lm-studio",
                        "api_base": "http://127.0.0.1:1234/v1",
                    }
                ],
                "active_model": "local",
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "hermes-runtime"
    home_root = tmp_path / "hermes-home"
    workspace.mkdir()
    runtime_root.mkdir()
    home_root.mkdir()
    manager = SettingsManager(root_dir=tmp_path)
    manager.set_execution_policy(policy)
    manager.set_hermes_settings(
        enabled=enabled,
        runtime_path=str(runtime_root),
        home_path=str(home_root),
        allow_effectful_fixture=effectful,
    )
    return manager


def _install_fake_protocol(monkeypatch):
    module = types.ModuleType("core.agent.hermes")

    class HermesConfig:
        def __init__(self, **values):
            self.__dict__.update(values)

    class HermesRequest:
        def __init__(self, **values):
            self.__dict__.update(values)

    module.HermesConfig = HermesConfig
    module.HermesRequest = HermesRequest
    module.HermesGatewayAdapter = object
    monkeypatch.setitem(sys.modules, "core.agent.hermes", module)


class _Status:
    def __init__(self, *, state="ready", ready=True, pid=123):
        self.state = state
        self.ready = ready
        self.pid = pid
        self.version = "test-version"
        self.error = None

    def as_dict(self):
        return dict(self.__dict__)


class _FakeHermesAdapter:
    def __init__(self, config, *, internal_approval=False):
        self.config = config
        self.internal_approval = internal_approval
        self.execute_calls = []
        self.preflight_calls = []
        self.closed = False
        self.internal_decision = None

    def preflight(self, request=None):
        self.preflight_calls.append(request)
        return None

    def execute(
        self,
        request,
        cancelled,
        *,
        event_notifier=None,
        interactive_handler=None,
    ):
        self.execute_calls.append(request)
        if self.internal_approval:
            event = {
                "type": "approval.request",
                "payload": {
                    "title": "Use a bounded\u202e browser fixture",
                    "command": "sensitive command is not approval metadata",
                    "display_command": "printf '[REDACTED]'\n\u200b",
                },
            }
            event_notifier(event)
            self.internal_decision = interactive_handler(event)
            if not self.internal_decision:
                raise RuntimeError("internal approval denied")
        event_notifier(
            {"type": "message.delta", "payload": {"content": "Hermes "}}
        )
        event_notifier(
            {
                "type": "tool.progress",
                "payload": {"stage": "research", "api_key": "must-redact"},
            }
        )
        event_notifier(
            {"type": "message.delta", "payload": {"content": "answer"}}
        )
        return {
            "content": "Hermes answer",
            "text": "Hermes answer",
            "session_id": "hermes-session",
            "finish_reason": "stop",
            "events": [],
            "cancelled": cancelled.is_set(),
            "provider": "lmstudio",
            "model": "openai/test-model",
        }

    def status(self):
        return _Status()

    def close(self):
        self.closed = True


class _NoNativeModel:
    def __init__(self):
        self.calls = 0

    def stream(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("the native provider must not run for SPECIALIST")
        yield


def _resolve_approval(runtime, chunk):
    runtime.approval_broker.resolve(
        chunk["approval_id"],
        approve=True,
        session_id=chunk["session_id"],
        generation_id=chunk["generation_id"],
    )


def test_startup_and_native_registry_do_not_import_or_construct_hermes(tmp_path):
    sys.modules.pop("core.agent.hermes", None)
    manager = _manager(tmp_path, enabled=False)
    factory_calls = []
    runtime = DaemonRuntime(
        manager,
        model_adapter=_NoNativeModel(),
        hermes_adapter_factory=lambda config: factory_calls.append(config),
    )

    runtime.get_session_service()

    assert "core.agent.hermes" not in sys.modules
    assert factory_calls == []
    assert runtime._hermes_tool_registry is None
    assert runtime.get_hermes_status()["state"] == "disabled"


def test_deny_all_never_imports_or_constructs_adapter(tmp_path):
    manager = _manager(tmp_path, policy="deny_all")
    factory_calls = []
    native_model = _NoNativeModel()
    runtime = DaemonRuntime(
        manager,
        model_adapter=native_model,
        hermes_adapter_factory=lambda config: factory_calls.append(config),
    )
    service = runtime.get_session_service()
    generation = service.start_generation(
        service.session_id,
        "deny-hermes",
        routing_signal=RoutingSignal(specialist_capability="research"),
    )

    chunks = list(
        service.stream(
            generation,
            [{"role": "user", "type": "message", "content": "private prompt"}],
        )
    )

    assert factory_calls == []
    assert native_model.calls == 0
    denied = [item for item in chunks if item.get("type") == "execution_denied"]
    assert denied[0]["content"]["reason_code"] == "policy_denies_execution"


@pytest.mark.parametrize("policy", ["always_ask", "auto_run"])
def test_specialist_is_approved_before_factory_and_streams_without_native_model(
    tmp_path, monkeypatch, policy
):
    _install_fake_protocol(monkeypatch)
    manager = _manager(tmp_path, policy=policy)
    adapters = []

    def factory(config):
        adapter = _FakeHermesAdapter(config)
        adapters.append(adapter)
        return adapter

    native_model = _NoNativeModel()
    runtime = DaemonRuntime(
        manager,
        model_adapter=native_model,
        hermes_adapter_factory=factory,
    )
    service = runtime.get_session_service()
    generation = service.start_generation(
        service.session_id,
        f"approved-{policy}",
        routing_signal=RoutingSignal(specialist_capability="research"),
    )
    stream = service.stream(
        generation,
        [{"role": "user", "type": "message", "content": "sensitive prompt"}],
    )
    chunks = []
    for chunk in stream:
        chunks.append(chunk)
        if chunk.get("type") == "approval_required":
            assert adapters == []
            assert "sensitive prompt" not in json.dumps(chunk)
            _resolve_approval(runtime, chunk)

    assert len(adapters) == 1
    assert len(adapters[0].execute_calls) == 1
    assert native_model.calls == 0
    assert "".join(
        item["content"]
        for item in chunks
        if item.get("role") == "assistant" and item.get("type") == "message"
    ) == "Hermes answer"
    event_types = {
        item.get("content", {}).get("event")
        for item in chunks
        if item.get("type") == "activity"
    }
    assert "specialist.message.delta" in event_types
    assert "specialist.tool.progress" in event_types
    assert HERMES_TOOL_NAME == "specialist.hermes.run"


def test_specialist_receives_jarvs_history_and_commits_only_final_answer(
    tmp_path, monkeypatch
):
    _install_fake_protocol(monkeypatch)
    manager = _manager(tmp_path, policy="auto_run")
    adapters = []

    def factory(config):
        adapter = _FakeHermesAdapter(config)
        adapters.append(adapter)
        return adapter

    runtime = DaemonRuntime(
        manager,
        model_adapter=_NoNativeModel(),
        hermes_adapter_factory=factory,
    )
    service = runtime.get_session_service()

    for generation_id, prompt in (("first", "first prompt"), ("second", "follow up")):
        generation = service.start_generation(
            service.session_id,
            generation_id,
            routing_signal=RoutingSignal(specialist_capability="research"),
        )
        assistant_chunks = []
        for chunk in service.stream(
            generation,
            [{"role": "user", "type": "message", "content": prompt}],
        ):
            if chunk.get("type") == "approval_required":
                _resolve_approval(runtime, chunk)
            if chunk.get("role") == "assistant" and chunk.get("type") == "message":
                assistant_chunks.append(chunk["content"])
        # Upstream deltas are not displayed before their interim/replacement
        # semantics are reconciled; the canonical complete text appears once.
        assert assistant_chunks == ["Hermes answer"]

    second_messages = adapters[0].execute_calls[1].messages
    assert second_messages == [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "Hermes answer"},
        {"role": "user", "content": "follow up"},
    ]


def test_internal_hermes_approval_uses_a_second_broker_request(tmp_path, monkeypatch):
    _install_fake_protocol(monkeypatch)
    manager = _manager(tmp_path, policy="auto_run")
    adapters = []

    def factory(config):
        adapter = _FakeHermesAdapter(config, internal_approval=True)
        adapters.append(adapter)
        return adapter

    runtime = DaemonRuntime(
        manager,
        model_adapter=_NoNativeModel(),
        hermes_adapter_factory=factory,
    )
    service = runtime.get_session_service()
    generation = service.start_generation(
        service.session_id,
        "nested-approval",
        routing_signal=RoutingSignal(specialist_capability="research"),
    )
    approvals = []
    chunks = []
    for chunk in service.stream(
        generation,
        [{"role": "user", "type": "message", "content": "private prompt"}],
    ):
        chunks.append(chunk)
        if chunk.get("type") == "approval_required":
            approvals.append(chunk)
            _resolve_approval(runtime, chunk)

    assert len(approvals) == 2
    assert approvals[0]["approval_id"] != approvals[1]["approval_id"]
    assert adapters[0].internal_decision is True
    serialized = json.dumps(approvals)
    assert "private prompt" not in serialized
    assert "sensitive command" not in serialized
    assert approvals[1]["title"] == "Hermes: Use a bounded browser fixture"
    assert approvals[1]["arguments"]["code"] == "printf '[REDACTED]'"


def test_effectful_fixture_requires_setting_and_marker_before_adapter(tmp_path):
    manager = _manager(tmp_path, policy="auto_run", effectful=True)
    factory_calls = []
    runtime = DaemonRuntime(
        manager,
        model_adapter=_NoNativeModel(),
        hermes_adapter_factory=lambda config: factory_calls.append(config),
    )
    service = runtime.get_session_service()
    generation = service.start_generation(
        service.session_id,
        "fixture-marker",
        routing_signal=RoutingSignal(specialist_capability="coding_fixture"),
    )
    chunks = []
    for chunk in service.stream(
        generation,
        [{"role": "user", "type": "message", "content": "edit fixture"}],
    ):
        chunks.append(chunk)
        if chunk.get("type") == "approval_required":
            _resolve_approval(runtime, chunk)

    assert factory_calls == []
    denied = [item for item in chunks if item.get("type") == "execution_denied"]
    assert denied[0]["content"]["reason_code"] == "hermes_fixture_marker_missing"


def test_public_settings_status_validation_reset_and_close(tmp_path, monkeypatch):
    _install_fake_protocol(monkeypatch)
    manager = _manager(tmp_path)
    before = (tmp_path / "settings.json").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        manager.set_hermes_settings(runtime_path="relative/runtime")
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == before

    adapters = []

    def factory(config):
        adapter = _FakeHermesAdapter(config)
        adapters.append(adapter)
        return adapter

    runtime = DaemonRuntime(manager, hermes_adapter_factory=factory)
    security = DaemonSecurity.create(TOKEN, [ORIGIN])
    with TestClient(create_app(runtime, security)) as client:
        settings = client.get("/settings", headers=AUTH_HEADERS).json()
        assert settings["hermes_enabled"] is True
        assert settings["hermes_version"] is None
        assert "api_key" not in settings
        status = client.get(
            "/specialists/hermes/status", headers=AUTH_HEADERS
        ).json()
        assert status == {
            "enabled": True,
            "available": True,
            "configured": True,
            "running": False,
            "state": "stopped",
            "version": None,
            "error": None,
            "capabilities": ["research", "vision"],
        }

    # Construct once through the authorized post-gate path, then verify reset
    # closes the session without deleting the user-owned Hermes home.
    runtime._get_hermes_adapter()
    adapter = adapters[0]
    home = manager.get_hermes_settings()["hermes_home_path"]
    runtime.reset_session()
    assert adapter.closed is True
    assert runtime._hermes_adapter is None
    assert __import__("os").path.isdir(home)


def test_specialist_tool_metadata_is_high_risk_code_execution(tmp_path):
    runtime = DaemonRuntime(_manager(tmp_path))
    definition = runtime.get_hermes_tool_executor().registry.resolve(HERMES_TOOL_NAME)

    assert definition.backend is ToolBackend.SPECIALIST
    assert definition.effect is ToolEffect.CODE_EXECUTION
    assert definition.risk is ToolRisk.HIGH
    assert runtime.get_hermes_tool_executor().preflight_timeout_seconds == 25.0


def test_specialist_staging_is_structurally_bounded(tmp_path):
    runtime = DaemonRuntime(_manager(tmp_path))
    generation = SimpleNamespace(session_id="session", generation_id="generation")

    with pytest.raises(Exception, match="too many messages"):
        runtime._stage_hermes_request(
            "research",
            [
                {"role": "user", "type": "message", "content": "x"}
                for _ in range(25)
            ],
            generation,
        )
    assert runtime._hermes_requests == {}

    with pytest.raises(Exception, match="unsupported message type"):
        runtime._stage_hermes_request(
            "research",
            [{"role": "user", "type": "arbitrary", "content": "x"}],
            generation,
        )
    assert runtime._hermes_requests == {}


def test_cloud_model_is_rejected_before_adapter_and_capabilities_are_hidden(
    tmp_path
):
    manager = _manager(tmp_path, policy="auto_run")
    manager.models_data = {
        "models": [
            {
                "id": "cloud",
                "provider": "cloud",
                "model_string": "cloud/model",
                "api_base": "https://api.example.test/v1",
                "api_key": "must-never-reach-hermes",
            }
        ],
        "active_model": "cloud",
    }
    factory_calls = []
    runtime = DaemonRuntime(
        manager,
        model_adapter=_NoNativeModel(),
        hermes_adapter_factory=lambda config: factory_calls.append(config),
    )
    status = runtime.get_hermes_status()
    assert status["capabilities"] == []
    assert status["error"] == "Hermes requires an active local loopback provider."

    service = runtime.get_session_service()
    generation = service.start_generation(
        service.session_id,
        "cloud-denied",
        routing_signal=RoutingSignal(specialist_capability="research"),
    )
    chunks = []
    for chunk in service.stream(
        generation,
        [{"role": "user", "type": "message", "content": "do not send"}],
    ):
        chunks.append(chunk)
        if chunk.get("type") == "approval_required":
            _resolve_approval(runtime, chunk)

    assert factory_calls == []
    denied = [item for item in chunks if item.get("type") == "execution_denied"]
    assert denied[0]["content"]["reason_code"] == "hermes_local_provider_required"


def test_invalid_hermes_path_does_not_partially_commit_unified_settings(tmp_path):
    manager = _manager(tmp_path, policy="always_ask")
    runtime = DaemonRuntime(manager)
    security = DaemonSecurity.create(TOKEN, [ORIGIN])

    with TestClient(create_app(runtime, security)) as client:
        response = client.post(
            "/settings/save",
            headers=AUTH_HEADERS,
            json={
                "model_id": "local",
                "execution_policy": "auto_run",
                "theme": "Changed",
                "hermes_runtime_path": "relative/runtime",
            },
        )

    assert response.status_code == 422
    assert manager.get_execution_policy() == "always_ask"
    assert manager.get_theme() == "Default"


def test_configuration_change_after_preflight_cannot_reuse_authorized_request(
    tmp_path, monkeypatch
):
    _install_fake_protocol(monkeypatch)
    manager = _manager(tmp_path, policy="auto_run")
    adapters = []

    def factory(config):
        adapter = _FakeHermesAdapter(config)
        adapters.append(adapter)
        return adapter

    runtime = DaemonRuntime(manager, hermes_adapter_factory=factory)
    generation = SimpleNamespace(
        session_id="session",
        generation_id="generation",
    )
    invocation = runtime._stage_hermes_request(
        "research",
        [{"role": "user", "type": "message", "content": "private"}],
        generation,
    )

    assert runtime._hermes_preflight(invocation.arguments_dict(), None) is None
    manager.set_hermes_settings(enabled=False)

    with pytest.raises(RuntimeError, match="configuration changed"):
        runtime._execute_hermes_backend(
            None,
            invocation.arguments_dict(),
            SimpleNamespace(),
        )
    assert adapters[0].execute_calls == []


def test_hermes_audit_redaction_has_a_global_work_budget():
    payload = {
        f"key-{index}": {
            "command": "sensitive" * 10_000,
            "api_key": "secret",
        }
        for index in range(1_000)
    }

    redacted = DaemonRuntime._redact_hermes_payload(payload)
    encoded = json.dumps(redacted)

    assert len(encoded) < 100_000
    assert "secret" not in encoded
    assert "[truncated]" in encoded
