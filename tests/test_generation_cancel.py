import threading

import pytest
from fastapi.testclient import TestClient

from core.agent.coordinator import NativeSessionService
from core.agent.approvals import ApprovalBroker
from core.daemon.runtime import DaemonRuntime
from core.daemon.security import DaemonSecurity
from core.daemon.server import create_app
from core.daemon.session_service import SessionBusyError, StaleGenerationError, StaleSessionError
from core.tools.settings_manager import SettingsManager
from tests.test_settings_api import FakeEngine, write_config


def test_native_stop_preserves_session_and_prevents_overlapping_generation():
    service = NativeSessionService(None, approval_broker=ApprovalBroker(),
                                   execution_policy_provider=lambda: "always_ask")
    session = service.session_id
    generation = service.start_generation(session, "first")
    with pytest.raises(StaleGenerationError):
        service.cancel_generation(session, "old")
    assert not generation.cancelled.is_set()
    with pytest.raises(StaleSessionError):
        service.cancel_generation("old-session", "first")
    assert service.cancel_generation(session, "first") is True
    assert service.cancel_generation(session, "first") is True
    assert generation.cancelled.is_set()
    with pytest.raises(SessionBusyError):
        service.start_generation(session, "second")
    service.finish_generation(generation)
    assert service.session_id == session
    assert service.cancel_generation(session, "first") is False
    next_generation = service.start_generation(session, "second")
    with pytest.raises(StaleGenerationError):
        service.cancel_generation(session, "first")
    assert not next_generation.cancelled.is_set()


def test_cancel_endpoint_is_authenticated_and_does_not_initialize_cold_runtime(tmp_path):
    write_config(tmp_path)
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path))
    payload = {"session_id": runtime.session_id, "generation_id": "missing"}
    with TestClient(create_app(runtime, DaemonSecurity.create("token"))) as client:
        assert client.post("/session/cancel", json=payload).status_code == 401
        result = client.post("/session/cancel", json=payload,
                             headers={"Authorization": "Bearer token"})
        assert result.json()["cancelled"] is False
        assert not runtime.engine_ready


def test_injected_engine_stop_preserves_history_and_same_websocket(tmp_path):
    write_config(tmp_path)
    engine = FakeEngine()
    entered = threading.Event()
    release = threading.Event()

    def stream(_messages):
        entered.set()
        yield {"role": "assistant", "type": "message", "content": "partial"}
        assert release.wait(5)
        yield {"role": "assistant", "type": "message", "content": "not delivered"}

    engine.chat_impl = stream
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path), engine=engine)
    with TestClient(create_app(runtime, DaemonSecurity.create("token"))) as client:
        with client.websocket_connect("/chat", subprotocols=["jarvs-v1", "jarvs-auth.token"]) as ws:
            session = ws.receive_json()["session_id"]
            ws.send_json({"prompt": "remember this", "session_id": session, "generation_id": "first"})
            assert ws.receive_json()["type"] == "started"
            assert entered.wait(5)
            assert ws.receive_json()["type"] == "chunk"
            result = client.post("/session/cancel", json={
                "session_id": session, "generation_id": "first",
            }, headers={"Authorization": "Bearer token"})
            assert result.json()["cancelled"] is True
            release.set()
            assert ws.receive_json()["code"] == "generation_cancelled"
            assert engine.messages
            assert runtime.session_id == session
            engine.chat_impl = lambda _messages: iter(())
            ws.send_json({"prompt": "continue", "session_id": session, "generation_id": "second"})
            assert ws.receive_json()["type"] == "started"
            assert ws.receive_json()["type"] == "done"
