import json
import subprocess
import sys

from fastapi.testclient import TestClient

from core.daemon.security import DaemonSecurity
from core.daemon.server import DaemonRuntime, create_app
from core.tools.settings_manager import SettingsManager
from core.agent.model_adapter import ModelStreamEvent
from core.agent.action_routing import ACTION_DISPATCH_PROVIDER_NAME


def test_daemon_import_does_not_load_optional_heavy_runtimes():
    probe = (
        "import json,sys; import core.daemon.server; "
        "names=('interpreter','litellm','mcp','PyQt6','requests','sounddevice'); "
        "print(json.dumps([name for name in names if name in sys.modules]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_health_is_live_before_agent_runtime_initializes(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({"models": [], "active_model": ""}),
        encoding="utf-8",
    )
    manager = SettingsManager(root_dir=tmp_path)
    runtime = DaemonRuntime(manager)
    app = create_app(runtime, DaemonSecurity.create("token"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "engine_ready": False}


def test_websocket_handshake_does_not_initialize_agent_runtime(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps({"models": [], "active_model": ""}),
        encoding="utf-8",
    )
    manager = SettingsManager(root_dir=tmp_path)
    runtime = DaemonRuntime(manager)
    app = create_app(runtime, DaemonSecurity.create("token"))

    with TestClient(app) as client:
        with client.websocket_connect(
            "/chat",
            subprotocols=["jarvs-v1", "jarvs-auth.token"],
        ) as socket:
            event = socket.receive_json()
            assert event == {"type": "session", "session_id": runtime.session_id}

    assert runtime.engine_ready is False


def test_reading_canonical_settings_does_not_rewrite_file(tmp_path):
    settings = {
        "theme": "Default",
        "workspace_path": str(tmp_path / "workspace"),
        "model": "",
        "window_size": [600, 120],
        "execution_policy": "always_ask",
        "cloud_workspace_egress": "deny",
    }
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    (tmp_path / "models.json").write_text(
        json.dumps({"models": [], "active_model": ""}),
        encoding="utf-8",
    )
    before = settings_file.stat().st_mtime_ns
    SettingsManager(root_dir=tmp_path)
    assert settings_file.stat().st_mtime_ns == before


def test_native_websocket_fast_path_uses_native_runtime(tmp_path):
    (tmp_path / "models.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "local",
                        "name": "Local",
                        "provider": "local",
                        "model_string": "openai/test-model",
                        "api_base": "http://127.0.0.1:1234/v1",
                        "api_key": "lm-studio",
                    }
                ],
                "active_model": "local",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "theme": "Default",
                "workspace_path": str(tmp_path / "workspace"),
                "model": "local",
                "window_size": [600, 120],
                "execution_policy": "always_ask",
                "cloud_workspace_egress": "deny",
            }
        ),
        encoding="utf-8",
    )
    manager = SettingsManager(root_dir=tmp_path)
    class Adapter:
        def __init__(self):
            self.requests = []

        def stream(self, context, request, cancelled):
            self.requests.append(request)
            yield ModelStreamEvent(content="FAST PATH OK")
            yield ModelStreamEvent(finish_reason="stop")

    adapter = Adapter()
    runtime = DaemonRuntime(
        manager,
        model_adapter=adapter,
    )
    app = create_app(runtime, DaemonSecurity.create("token"))

    with TestClient(app) as client:
        with client.websocket_connect(
            "/chat",
            subprotocols=["jarvs-v1", "jarvs-auth.token"],
        ) as socket:
            session_id = socket.receive_json()["session_id"]
            socket.send_json(
                {
                    "prompt": "Hello",
                    "attachments": [],
                    "session_id": session_id,
                    "generation_id": "native-fast",
                }
            )
            events = []
            while not events or events[-1]["type"] != "done":
                events.append(socket.receive_json())

    assert next(item for item in events if item["type"] == "chunk")["text"] == "FAST PATH OK"
    assert any(item["type"] == "activity" for item in events)
    assert len(adapter.requests[0].tools) == 1
    assert adapter.requests[0].tools[0]["function"]["name"] == ACTION_DISPATCH_PROVIDER_NAME
