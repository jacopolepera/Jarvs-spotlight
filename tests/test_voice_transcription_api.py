import base64
import json

import pytest
from fastapi.testclient import TestClient

from core.audio.transcriber import VoiceTranscriptionError, transcribe_audio_gemini_data
from core.daemon.security import DaemonSecurity
from core.daemon.server import DaemonRuntime, create_app
from core.tools.settings_manager import SettingsManager


TOKEN = "voice-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield json.dumps(self._payload).encode()

    def close(self):
        pass


def write_config(root, *, api_key="voice-secret"):
    (root / "models.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gemini",
                        "name": "Gemini",
                        "provider": "cloud",
                        "model_string": "gemini/gemini-2.5-flash",
                        "api_key": api_key,
                    }
                ],
                "active_model": "gemini",
            }
        ),
        encoding="utf-8",
    )
    (root / "settings.json").write_text(
        json.dumps(
            {
                "theme": "Dark",
                "workspace_path": str(root / "workspace"),
                "model": "gemini",
                "execution_policy": "always_ask",
                "cloud_workspace_egress": "deny",
                "voice": {"provider": "gemini"},
            }
        ),
        encoding="utf-8",
    )


def test_transcriber_sends_bounded_audio_and_returns_only_provider_text():
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            payload={
                "candidates": [{"content": {"parts": [{"text": "  hello Jarvs  "}]}}]
            }
        )

    text = transcribe_audio_gemini_data(
        b"recorded-audio",
        "audio/webm;codecs=opus",
        "secret-key",
        post=post,
    )

    assert text == "hello Jarvs"
    assert captured["timeout"] == 45.0
    part = captured["json"]["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/webm"
    assert base64.b64decode(part["data"]) == b"recorded-audio"


@pytest.mark.parametrize(
    ("audio", "mime_type", "api_key", "code"),
    [
        (b"", "audio/webm", "key", "invalid_audio"),
        (b"audio", "application/octet-stream", "key", "unsupported_audio_type"),
        (b"audio", "audio/webm", "", "voice_api_key_missing"),
    ],
)
def test_transcriber_rejects_invalid_inputs_before_network(audio, mime_type, api_key, code):
    with pytest.raises(VoiceTranscriptionError) as error:
        transcribe_audio_gemini_data(
            audio,
            mime_type,
            api_key,
            post=lambda *_args, **_kwargs: pytest.fail("network must not be called"),
        )
    assert error.value.code == code


def test_voice_api_is_authenticated_and_never_returns_the_api_key(tmp_path, monkeypatch):
    write_config(tmp_path)
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path))
    captured = {}

    def transcribe(audio, mime_type, api_key):
        captured.update(audio=audio, mime_type=mime_type, api_key=api_key)
        return "testo trascritto"

    monkeypatch.setattr("core.audio.transcriber.transcribe_audio_gemini_data", transcribe)
    app = create_app(runtime, DaemonSecurity.create(TOKEN))
    payload = {
        "audio_base64": base64.b64encode(b"voice-bytes").decode("ascii"),
        "mime_type": "audio/webm",
    }

    with TestClient(app) as client:
        assert client.post("/voice/transcribe", json=payload).status_code == 401
        response = client.post("/voice/transcribe", json=payload, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "text": "testo trascritto"}
    assert captured == {
        "audio": b"voice-bytes",
        "mime_type": "audio/webm",
        "api_key": "voice-secret",
    }
    assert "voice-secret" not in response.text


def test_voice_api_returns_stable_errors_for_invalid_audio_and_missing_key(tmp_path):
    write_config(tmp_path, api_key="")
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path))
    app = create_app(runtime, DaemonSecurity.create(TOKEN))

    with TestClient(app) as client:
        invalid = client.post(
            "/voice/transcribe",
            json={"audio_base64": "not-base64!", "mime_type": "audio/webm"},
            headers=AUTH,
        )
        missing = client.post(
            "/voice/transcribe",
            json={
                "audio_base64": base64.b64encode(b"audio").decode("ascii"),
                "mime_type": "audio/webm",
            },
            headers=AUTH,
        )

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_audio"
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "voice_api_key_missing"


def test_local_voice_settings_persist_and_drive_transcription_without_cloud(tmp_path, monkeypatch):
    write_config(tmp_path)
    settings = SettingsManager(root_dir=tmp_path)
    runtime = DaemonRuntime(settings)
    app = create_app(runtime, DaemonSecurity.create(TOKEN))
    captured = {}

    class LocalSession:
        trust_env = True

        def post(self, url, **kwargs):
            captured.update(url=url, trust_env=self.trust_env, **kwargs)
            return FakeResponse(payload={"text": "  dettatura locale  "})

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("core.audio.transcriber.requests.Session", LocalSession)
    monkeypatch.setattr(
        "core.audio.transcriber.transcribe_audio_gemini_data",
        lambda *_args, **_kwargs: pytest.fail("Cloud must not be used for local speech"),
    )
    voice = {
        "provider": "local", "base_url": "http://localhost:8088/v1/",
        "model": "local-whisper", "language": "it",
    }
    with TestClient(app) as client:
        assert client.post("/settings/voice", json=voice).status_code == 401
        saved = client.post("/settings/voice", json=voice, headers=AUTH)
        assert saved.status_code == 200
        expected = {**voice, "base_url": "http://127.0.0.1:8088/v1"}
        assert saved.json() == {"status": "ok", "voice": expected}
        assert client.get("/settings", headers=AUTH).json()["voice"] == expected
        assert SettingsManager(root_dir=tmp_path).get_voice_config() == expected
        response = client.post("/voice/transcribe", headers=AUTH, json={
            "audio_base64": base64.b64encode(b"recorded-audio").decode(),
            "mime_type": "audio/webm;codecs=opus",
        })
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "text": "dettatura locale"}
    assert captured["url"] == "http://127.0.0.1:8088/v1/audio/transcriptions"
    assert captured["data"]["model"] == "local-whisper"
    assert captured["data"]["language"] == "it"
    assert captured["trust_env"] is False
    assert captured["closed"] is True
    assert "voice-secret" not in repr(captured)


def test_invalid_local_endpoint_does_not_change_persisted_voice_settings(tmp_path):
    write_config(tmp_path)
    settings = SettingsManager(root_dir=tmp_path)
    expected = settings.get_voice_config()
    app = create_app(DaemonRuntime(settings), DaemonSecurity.create(TOKEN))
    with TestClient(app) as client:
        response = client.post("/settings/voice", headers=AUTH, json={
            "provider": "local", "base_url": "https://external.test/v1",
        })
    assert response.status_code == 422
    assert settings.get_voice_config() == expected
    assert SettingsManager(root_dir=tmp_path).get_voice_config() == expected


@pytest.mark.parametrize(("code", "status"), [
    ("voice_provider_unavailable", 502), ("voice_provider_timeout", 504),
    ("voice_configuration_invalid", 409),
])
def test_voice_api_maps_local_service_failures(tmp_path, monkeypatch, code, status):
    write_config(tmp_path)
    settings = SettingsManager(root_dir=tmp_path)
    settings.set_voice_config({"provider": "local"})

    def transcribe(*_args, **_kwargs):
        raise VoiceTranscriptionError(code, "Actionable local speech error")

    monkeypatch.setattr("core.audio.transcriber.transcribe_audio_data", transcribe)
    app = create_app(DaemonRuntime(settings), DaemonSecurity.create(TOKEN))
    with TestClient(app) as client:
        response = client.post("/voice/transcribe", headers=AUTH, json={
            "audio_base64": base64.b64encode(b"audio").decode(), "mime_type": "audio/wav",
        })
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
