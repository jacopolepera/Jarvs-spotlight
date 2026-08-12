import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.daemon.security import DaemonSecurity
from core.daemon.server import DaemonRuntime, create_app
from core.tools.settings_manager import SettingsManager
from core.vision.attachment_broker import AttachmentBroker


TOKEN = "test-token"
ORIGIN = "http://tauri.localhost"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN}
WS_PROTOCOLS = ["jarvs-v1", f"jarvs-auth.{TOKEN}"]


class FakeEngine:
    def __init__(self):
        self.llm = SimpleNamespace(
            model="provider/model",
            api_key="top-secret",
            api_base=None,
        )
        self.offline = False
        self.auto_run = True
        self.messages = []
        self.executed = False
        self.chat_impl = lambda _messages: iter(())

    def chat(self, messages, stream, display):
        assert stream is True and display is False
        self.messages.extend(messages)
        return self.chat_impl(messages)


def write_config(root):
    (root / "models.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "models": [
                    {
                        "id": "cloud",
                        "name": "Cloud",
                        "provider": "cloud",
                        "model_string": "provider/model",
                        "api_key": "top-secret",
                        "api_base": None,
                        "supports_vision": True,
                        "context_window": None,
                    },
                    {
                        "id": "local",
                        "name": "LM Studio",
                        "provider": "local",
                        "model_string": "openai/local-model",
                        "api_key": "lm-studio",
                        "api_base": "http://localhost:1234/v1",
                        "supports_vision": True,
                        "context_window": 8192,
                    },
                ],
                "active_model": "cloud",
            }
        ),
        encoding="utf-8",
    )
    (root / "settings.json").write_text(
        json.dumps(
            {
                "theme": "Dark",
                "workspace_path": str(root / "workspace"),
                "model": "cloud",
                "execution_policy": "always_ask",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def api(tmp_path):
    write_config(tmp_path)
    attachment_root = tmp_path / "attachments"
    attachment_root.mkdir()
    manager = SettingsManager(root_dir=tmp_path)
    engine = FakeEngine()
    runtime = DaemonRuntime(
        manager,
        engine=engine,
        instruction_reloader=lambda *_args: None,
        attachment_broker=AttachmentBroker(attachment_root),
    )
    security = DaemonSecurity.create(TOKEN, [ORIGIN])
    with TestClient(create_app(runtime, security)) as client:
        yield SimpleNamespace(
            client=client,
            manager=manager,
            engine=engine,
            runtime=runtime,
        )


def websocket(api):
    return api.client.websocket_connect(
        "/chat",
        headers={"Origin": ORIGIN},
        subprotocols=WS_PROTOCOLS,
    )


def test_http_requires_token_and_exact_origin(api):
    assert api.client.get("/health").status_code == 200
    assert api.client.get("/settings").status_code == 401
    assert api.client.get(
        "/settings",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "https://evil.example",
        },
    ).status_code == 403
    assert api.client.get("/settings", headers=AUTH_HEADERS).status_code == 200


def test_cors_preflight_is_limited_to_the_exact_origin(api):
    allowed = api.client.options(
        "/settings",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN

    rejected = api.client.options(
        "/settings",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert rejected.status_code == 403


def test_get_settings_redacts_all_secrets(api):
    response = api.client.get("/settings", headers=AUTH_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["execution_policy"] == "always_ask"
    assert data["cloud_workspace_egress"] == "deny"
    assert data["active_model_config"]["api_key_configured"] is True
    assert "top-secret" not in json.dumps(data)
    assert "api_key" not in data["active_model_config"]


def test_model_switch_response_is_redacted(api):
    response = api.client.post(
        "/settings/model",
        headers=AUTH_HEADERS,
        json={"model_id": "cloud"},
    )
    assert response.status_code == 200
    assert "top-secret" not in response.text
    assert api.engine.auto_run is False


def test_blank_api_key_preserves_secret_and_policy_round_trips(api):
    response = api.client.post(
        "/settings/save",
        headers=AUTH_HEADERS,
        json={
            "model_id": "cloud",
            "api_key": "",
            "execution_policy": "auto_run",
            "cloud_workspace_egress": "allow",
        },
    )
    assert response.status_code == 200
    assert response.json()["execution_policy"] == "auto_run"
    assert response.json()["cloud_workspace_egress"] == "allow"
    assert api.manager.get_cloud_workspace_egress() == "allow"
    assert api.manager.get_active_model()["api_key"] == "top-secret"


def test_deleting_active_model_applies_fallback_to_ready_engine(api):
    response = api.client.post(
        "/settings/models/delete",
        headers=AUTH_HEADERS,
        json={"model_id": "cloud"},
    )
    assert response.status_code == 200
    assert response.json()["active_model"] == "local"
    assert api.manager.get_active_model_id() == "local"
    assert api.engine.llm.model == "openai/local-model"
    assert api.engine.llm.api_base == "http://localhost:1234/v1"
    assert api.engine.offline is True


def test_model_configuration_callbacks_apply_latest_persisted_model(api, monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        api.runtime,
        "configure_ready_engine",
        lambda callback: callbacks.append(callback) or True,
    )

    assert api.client.post(
        "/settings/model",
        headers=AUTH_HEADERS,
        json={"model_id": "local"},
    ).status_code == 200
    assert api.client.post(
        "/settings/model",
        headers=AUTH_HEADERS,
        json={"model_id": "cloud"},
    ).status_code == 200

    # Simulate engine-lock acquisition in the opposite order from persistence.
    for callback in reversed(callbacks):
        callback(api.engine)
    assert api.manager.get_active_model_id() == "cloud"
    assert api.engine.llm.model == "provider/model"
    assert api.engine.offline is False


def test_local_model_without_url_uses_lm_studio_default(api):
    added = api.client.post(
        "/settings/models/add",
        headers=AUTH_HEADERS,
        json={
            "name": "Local Without URL",
            "provider": "local",
            "model_string": "openai/local-no-url",
        },
    )
    assert added.status_code == 200
    selected = api.client.post(
        "/settings/model",
        headers=AUTH_HEADERS,
        json={"model_id": added.json()["model_id"]},
    )
    assert selected.status_code == 200
    assert api.engine.llm.api_base == "http://localhost:1234/v1"


def test_invalid_policy_and_multiline_secret_are_rejected_without_mutation(api):
    invalid_policy = api.client.post(
        "/settings/save",
        headers=AUTH_HEADERS,
        json={"model_id": "cloud", "execution_policy": "unsafe"},
    )
    assert invalid_policy.status_code == 422
    assert api.manager.get_execution_policy() == "always_ask"

    multiline = api.client.post(
        "/settings/save",
        headers=AUTH_HEADERS,
        json={"model_id": "cloud", "api_key": "secret\nINJECTED=x"},
    )
    assert multiline.status_code == 422
    assert api.manager.get_active_model()["api_key"] == "top-secret"


def test_websocket_rejects_missing_auth_before_session_event(api):
    with pytest.raises(WebSocketDisconnect) as error:
        with api.client.websocket_connect(
            "/chat",
            headers={"Origin": ORIGIN},
            subprotocols=["jarvs-v1"],
        ):
            pass
    assert error.value.code == 1008


def test_websocket_preserves_streaming_contract(api):
    def chunks(_messages):
        yield {"role": "assistant", "type": "message", "content": "Hello"}
        yield {"role": "assistant", "type": "message", "content": " World"}

    api.engine.chat_impl = chunks
    with websocket(api) as socket:
        assert socket.accepted_subprotocol == "jarvs-v1"
        session_id = socket.receive_json()["session_id"]
        socket.send_json(
            {
                "prompt": "Hi",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "generation",
            }
        )
        assert socket.receive_json()["type"] == "started"
        first = socket.receive_json()
        second = socket.receive_json()
        terminal = socket.receive_json()

    assert first["text"] == "Hello"
    assert first["channel"] == "answer"
    assert second["text"] == " World"
    assert terminal["type"] == "done"


def confirmation_chunks(engine):
    yield {
        "role": "assistant",
        "type": "code",
        "format": "python",
        "content": "print('probe')",
    }
    yield {
        "role": "computer",
        "type": "confirmation",
        "format": "execution",
        "content": {
            "type": "code",
            "format": "python",
            "content": "print('probe')",
        },
    }
    engine.executed = True
    yield {
        "role": "computer",
        "type": "console",
        "format": "output",
        "content": "probe\n",
    }


def test_approval_event_and_one_shot_http_decision_resume_execution(api):
    api.engine.chat_impl = lambda _messages: confirmation_chunks(api.engine)
    with websocket(api) as socket:
        session_id = socket.receive_json()["session_id"]
        socket.send_json(
            {
                "prompt": "Run a probe",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "approval-generation",
            }
        )
        assert socket.receive_json()["type"] == "started"
        assert socket.receive_json()["chunk_type"] == "code"
        approval = socket.receive_json()
        assert approval["type"] == "approval_required"
        assert api.engine.executed is False

        decision = api.client.post(
            f"/approvals/{approval['approval_id']}/decision",
            headers=AUTH_HEADERS,
            json={
                "decision": "approve",
                "session_id": session_id,
                "generation_id": "approval-generation",
            },
        )
        assert decision.status_code == 200
        assert socket.receive_json()["chunk_type"] == "console"
        assert socket.receive_json()["type"] == "done"
        assert api.engine.executed is True

        duplicate = api.client.post(
            f"/approvals/{approval['approval_id']}/decision",
            headers=AUTH_HEADERS,
            json={
                "decision": "approve",
                "session_id": session_id,
                "generation_id": "approval-generation",
            },
        )
        assert duplicate.status_code == 409


def test_simultaneous_inbound_prompt_cannot_hide_approval_event(api):
    api.engine.chat_impl = lambda _messages: confirmation_chunks(api.engine)
    with websocket(api) as socket:
        session_id = socket.receive_json()["session_id"]
        socket.send_json(
            {
                "prompt": "Run the first probe",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "race-generation",
            }
        )
        assert socket.receive_json()["type"] == "started"
        assert socket.receive_json()["chunk_type"] == "code"

        # A second inbound frame can become ready in the same event-loop tick
        # as approval_required. The approval must still reach the renderer.
        socket.send_json(
            {
                "prompt": "This prompt must be rejected as busy",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "second-generation",
            }
        )
        concurrent_events = [socket.receive_json(), socket.receive_json()]
        approval = next(
            event
            for event in concurrent_events
            if event.get("type") == "approval_required"
        )
        assert any(
            event.get("type") == "error" and event.get("code") == "session_busy"
            for event in concurrent_events
        )

        decision = api.client.post(
            f"/approvals/{approval['approval_id']}/decision",
            headers=AUTH_HEADERS,
            json={
                "decision": "approve",
                "session_id": session_id,
                "generation_id": "race-generation",
            },
        )
        assert decision.status_code == 200
        assert socket.receive_json()["chunk_type"] == "console"
        assert socket.receive_json()["type"] == "done"


def test_inbound_prompt_near_terminal_event_is_rejected_not_lost(api):
    generation_entered = threading.Event()
    release_generation = threading.Event()

    def delayed_terminal(_messages):
        generation_entered.set()
        assert release_generation.wait(timeout=2)
        return
        yield  # pragma: no cover - keeps this function a generator

    api.engine.chat_impl = delayed_terminal
    with websocket(api) as socket:
        session_id = socket.receive_json()["session_id"]
        socket.send_json(
            {
                "prompt": "First",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "first-generation",
            }
        )
        assert socket.receive_json()["type"] == "started"
        assert generation_entered.wait(timeout=1)
        socket.send_json(
            {
                "prompt": "Second",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "second-generation",
            }
        )
        release_generation.set()
        events = [socket.receive_json(), socket.receive_json()]

    busy = next(event for event in events if event.get("code") == "session_busy")
    terminal = next(event for event in events if event.get("type") == "done")
    assert busy["generation_id"] == "second-generation"
    assert terminal["generation_id"] == "first-generation"


def test_deny_all_never_advances_confirmation(api):
    api.manager.set_execution_policy("deny_all")
    api.engine.chat_impl = lambda _messages: confirmation_chunks(api.engine)
    with websocket(api) as socket:
        session_id = socket.receive_json()["session_id"]
        socket.send_json(
            {
                "prompt": "Run a probe",
                "attachments": [],
                "session_id": session_id,
                "generation_id": "denied-generation",
            }
        )
        assert socket.receive_json()["type"] == "started"
        assert socket.receive_json()["chunk_type"] == "code"
        denied = socket.receive_json()
    assert denied["type"] == "error"
    assert denied["code"] == "execution_denied"
    assert api.engine.executed is False


def test_session_reset_rejects_stale_websocket_prompt(api):
    with websocket(api) as socket:
        old_session_id = socket.receive_json()["session_id"]
        reset = api.client.post("/session/reset", headers=AUTH_HEADERS).json()
        socket.send_json(
            {
                "prompt": "old",
                "attachments": [],
                "session_id": old_session_id,
                "generation_id": "stale",
            }
        )
        error = socket.receive_json()
    assert error["code"] == "stale_session"
    assert error["session_id"] == reset["session_id"]


def test_personal_memory_api_requires_consent_and_supports_crud_export(api):
    denied = api.client.post(
        "/memory",
        headers=AUTH_HEADERS,
        json={
            "category": "preference",
            "content": "I prefer concise answers.",
            "provenance": "user_explicit",
            "confidence": 1.0,
            "consent": False,
        },
    )
    assert denied.status_code == 422

    created = api.client.post(
        "/memory",
        headers=AUTH_HEADERS,
        json={
            "category": "preference",
            "content": "I prefer concise answers.",
            "provenance": "user_explicit",
            "confidence": 1.0,
            "consent": True,
        },
    )
    assert created.status_code == 200
    memory_id = created.json()["memory"]["memory_id"]
    listed = api.client.get("/memory", headers=AUTH_HEADERS).json()["memories"]
    assert [item["memory_id"] for item in listed] == [memory_id]

    updated = api.client.patch(
        f"/memory/{memory_id}",
        headers=AUTH_HEADERS,
        json={"content": "I prefer very concise answers.", "consent": True},
    )
    assert updated.status_code == 200
    assert updated.json()["memory"]["version"] == 2
    assert api.client.get("/memory/export", headers=AUTH_HEADERS).json()["memories"]
    assert api.client.delete(f"/memory/{memory_id}", headers=AUTH_HEADERS).status_code == 200


def test_memory_api_rejects_secret_and_routine_api_versions_manifests(api):
    secret = api.client.post(
        "/memory",
        headers=AUTH_HEADERS,
        json={
            "category": "profile",
            "content": "api_key=very-secret-value",
            "provenance": "user_explicit",
            "consent": True,
        },
    )
    assert secret.status_code == 422

    manifest = {
        "schema_version": 1,
        "name": "Read file",
        "description": "Read a known file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "tool_name": "workspace.read_text",
                "arguments": {"path": {"$input": "path"}},
            }
        ],
    }
    created = api.client.post(
        "/routines",
        headers=AUTH_HEADERS,
        json={"manifest": manifest},
    )
    assert created.status_code == 200
    routine_id = created.json()["routine"]["routine_id"]
    manifest["description"] = "Versione aggiornata."
    updated = api.client.put(
        f"/routines/{routine_id}",
        headers=AUTH_HEADERS,
        json={"manifest": manifest},
    )
    assert updated.json()["routine"]["version"] == 2
    assert len(api.client.get("/routines/export", headers=AUTH_HEADERS).json()["routines"]) == 2
