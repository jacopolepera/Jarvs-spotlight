"""Deterministic local speech protocol and privacy boundary tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from core.audio.transcriber import (
    DEFAULT_VOICE_CONFIG,
    MAX_AUDIO_BYTES,
    MAX_TRANSCRIPTION_RESPONSE_BYTES,
    VoiceTranscriptionError,
    normalize_voice_config,
    transcribe_audio_data,
)


class Response:
    def __init__(self, payload=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]

    def close(self):
        self.closed = True


def test_missing_config_is_local_even_when_cloud_key_exists(monkeypatch):
    captured = {}
    response = Response({"text": "  Ciao Jarvs  "})

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return response

    monkeypatch.setattr(
        "core.audio.transcriber.transcribe_audio_gemini_data",
        lambda *_args, **_kwargs: pytest.fail("Cloud must be explicitly selected"),
    )
    assert transcribe_audio_data(b"audio", "audio/webm;codecs=opus", api_key="cloud-secret", post=post) == "Ciao Jarvs"
    assert captured["url"] == "http://127.0.0.1:8000/v1/audio/transcriptions"
    assert captured["files"] == {"file": ("recording.webm", b"audio", "audio/webm")}
    assert captured["data"] == {"model": DEFAULT_VOICE_CONFIG["model"], "response_format": "json"}
    assert captured["allow_redirects"] is False
    assert captured["timeout"] == (5.0, 45.0)
    assert "cloud-secret" not in repr(captured)
    assert response.closed


@pytest.mark.parametrize("base_url", [
    "https://api.openai.com/v1", "http://192.168.1.20:8000/v1",
    "http://local.example/v1", "http://127.0.0.1.evil.test/v1",
    "http://127.0.0.1@evil.test/v1", "http://user:secret@127.0.0.1/v1",
    "http://127.0.0.1/v1?forward=external", "http://127.0.0.1/v1#external",
    "file:///tmp/speech", "http://127.0.0.1:99999/v1", "http://127.0.0.1:0/v1",
    "http://127.0.0.1\n.evil.test/v1", "http://[::ffff:192.168.1.20]/v1",
])
def test_non_loopback_and_ambiguous_urls_are_rejected_without_network(base_url):
    with pytest.raises(VoiceTranscriptionError) as error:
        transcribe_audio_data(
            b"audio", "audio/wav", {"base_url": base_url},
            post=lambda *_args, **_kwargs: pytest.fail("Invalid endpoint must not be contacted"),
        )
    assert error.value.code == "voice_configuration_invalid"


def test_localhost_is_canonicalized_and_config_is_not_mutated():
    config = {"base_url": "http://localhost:8000/v1/", "language": "it"}
    normalized = normalize_voice_config(config)
    assert normalized["base_url"] == "http://127.0.0.1:8000/v1"
    assert config["base_url"] == "http://localhost:8000/v1/"
    assert normalize_voice_config({"base_url": "http://[::1]:8000/v1"})["base_url"] == "http://[::1]:8000/v1"


@pytest.mark.parametrize("config", [
    [], {"provider": "auto"}, {"provider": None}, {"model": ""},
    {"language": "Italian"}, {"language": 3}, {"api_key": "secret"},
])
def test_invalid_provider_settings_fail_closed(config):
    with pytest.raises(ValueError):
        normalize_voice_config(config)


@pytest.mark.parametrize(("audio", "mime", "code"), [
    (b"", "audio/webm", "invalid_audio"),
    (b"audio", "text/plain", "unsupported_audio_type"),
    (b"audio", None, "unsupported_audio_type"),
    (b"a" * (MAX_AUDIO_BYTES + 1), "audio/webm", "audio_too_large"),
], ids=["empty", "unsupported", "invalid-mime", "too-large"])
def test_invalid_recordings_never_reach_provider(audio, mime, code):
    with pytest.raises(VoiceTranscriptionError) as error:
        transcribe_audio_data(audio, mime, post=lambda *_args, **_kwargs: pytest.fail("No network expected"))
    assert error.value.code == code


@pytest.mark.parametrize(("failure", "code"), [
    (requests.Timeout("sensitive endpoint detail"), "voice_provider_timeout"),
    (requests.ConnectionError("sensitive endpoint detail"), "voice_provider_unavailable"),
])
def test_local_failure_never_retries_or_falls_back_to_cloud(monkeypatch, failure, code):
    calls = []

    def post(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(
        "core.audio.transcriber.transcribe_audio_gemini_data",
        lambda *_args, **_kwargs: pytest.fail("Must never fall back to cloud"),
    )
    with pytest.raises(VoiceTranscriptionError) as error:
        transcribe_audio_data(b"audio", "audio/wav", api_key="cloud-secret", post=post)
    assert error.value.code == code
    assert "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(("response", "code"), [
    (Response({"text": ""}), "empty_transcription"),
    (Response({"text": None}), "invalid_voice_response"),
    (Response(["not-an-object"]), "invalid_voice_response"),
    (Response(raw=b"invalid-json"), "invalid_voice_response"),
    (Response(raw=b"x" * (MAX_TRANSCRIPTION_RESPONSE_BYTES + 1)), "invalid_voice_response"),
    (Response(status=307), "voice_provider_error"),
    (Response(status=500), "voice_provider_error"),
])
def test_local_response_is_bounded_validated_and_closed(response, code):
    with pytest.raises(VoiceTranscriptionError) as error:
        transcribe_audio_data(b"audio", "audio/wav", post=lambda *_args, **_kwargs: response)
    assert error.value.code == code
    assert response.closed


def test_cloud_provider_requires_explicit_selection(monkeypatch):
    calls = []

    def gemini(audio, mime, key):
        calls.append((audio, mime, key))
        return "cloud text"

    monkeypatch.setattr("core.audio.transcriber.transcribe_audio_gemini_data", gemini)
    assert transcribe_audio_data(b"audio", "audio/wav", {"provider": "gemini"}, api_key="key") == "cloud text"
    assert calls == [(b"audio", "audio/wav", "key")]


def test_real_local_multipart_request_ignores_environment_proxy(monkeypatch):
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.update(
                path=self.path, content_type=self.headers["Content-Type"],
                body=self.rfile.read(int(self.headers["Content-Length"])),
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"text":"Ciao dal servizio locale"}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        text = transcribe_audio_data(b"recorded-wave", "audio/wav", {
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model": "local-speech-model", "language": "it",
        })
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert text == "Ciao dal servizio locale"
    assert captured["path"] == "/v1/audio/transcriptions"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    for fragment in (b'recorded-wave', b'filename="recording.wav"', b'local-speech-model', b'name="language"\r\n\r\nit'):
        assert fragment in captured["body"]
