import json
import os
import threading
import urllib.request

import pytest

from core.agent.egress import CloudWorkspaceEgress, DataClass, DataEgressPolicy
from core.agent.model_adapter import (
    ModelRequest,
    OpenAICompatibleAdapter,
    ProviderConfigurationError,
    ProviderContext,
    ProviderDestination,
    ProviderNotCompatibleError,
    provider_context_from_model,
)


def test_provider_context_is_explicit_and_local_endpoints_are_loopback_only():
    context = provider_context_from_model(
        {
            "id": "local",
            "provider": "local",
            "model_string": "openai/qwen",
            "api_base": "http://localhost:1234/v1",
        }
    )
    assert context.model == "qwen"
    assert context.destination is ProviderDestination.LOCAL

    with pytest.raises(ProviderConfigurationError, match="loopback"):
        provider_context_from_model(
            {
                "id": "false-local",
                "provider": "local",
                "model_string": "openai/qwen",
                "api_base": "https://models.example.test/v1",
            }
        )


def test_gemini_uses_official_compatible_surface_and_unknown_cloud_falls_back():
    context = provider_context_from_model(
        {
            "id": "gemini",
            "provider": "cloud",
            "model_string": "gemini/gemini-2.5-flash",
            "api_key": "secret",
        }
    )
    assert context.destination is ProviderDestination.CLOUD
    assert context.model == "gemini-2.5-flash"
    assert "generativelanguage.googleapis.com" in context.api_base

    with pytest.raises(ProviderNotCompatibleError):
        provider_context_from_model(
            {
                "id": "claude",
                "provider": "cloud",
                "model_string": "claude-3-5-sonnet",
                "api_key": "secret",
            }
        )


def test_data_egress_matrix_is_fail_closed_and_distinct_from_user_prompt():
    selected = {"value": CloudWorkspaceEgress.DENY.value}
    policy = DataEgressPolicy(lambda: selected["value"])

    assert policy.evaluate(ProviderDestination.LOCAL, DataClass.WORKSPACE_CONTENT).allowed
    assert policy.evaluate(ProviderDestination.CLOUD, DataClass.USER_CONTENT).allowed
    assert not policy.evaluate(ProviderDestination.CLOUD, DataClass.WORKSPACE_CONTENT).allowed
    assert policy.evaluate(ProviderDestination.LOCAL, DataClass.PERSONAL_MEMORY).allowed
    assert not policy.evaluate(ProviderDestination.CLOUD, DataClass.PERSONAL_MEMORY).allowed
    assert not policy.evaluate(ProviderDestination.LOCAL, DataClass.SENSITIVE_CONTENT).allowed

    selected["value"] = CloudWorkspaceEgress.ALLOW.value
    assert policy.evaluate(ProviderDestination.CLOUD, DataClass.WORKSPACE_CONTENT).allowed


class FakeResponse:
    def __init__(self, records):
        self.headers = {"Content-Type": "text/event-stream"}
        self._lines = iter(
            [f"data: {json.dumps(record)}\n".encode() for record in records]
            + [b"data: [DONE]\n"]
        )
        self.closed = False

    def readline(self):
        return next(self._lines, b"")

    def close(self):
        self.closed = True


def test_openai_compatible_transcript_fixture_streams_separate_channels(monkeypatch):
    response = FakeResponse(
        [
            {"choices": [{"delta": {"reasoning_content": "check"}}]},
            {"choices": [{"delta": {"content": "Hello "}}]},
            {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
        ]
    )
    captured = {}

    def open_url(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["authorization"] = request.headers["Authorization"]
        return response

    monkeypatch.setattr("urllib.request.urlopen", open_url)
    context = ProviderContext(
        model_id="local",
        model="test-model",
        destination=ProviderDestination.LOCAL,
        api_base="http://127.0.0.1:1234/v1",
        api_key="local-key",
    )
    events = list(
        OpenAICompatibleAdapter().stream(
            context,
            ModelRequest(messages=[{"role": "user", "content": "hello"}]),
            threading.Event(),
        )
    )

    assert "".join(event.content for event in events) == "Hello world"
    assert "".join(event.reasoning_content for event in events) == "check"
    assert events[-1].finish_reason == "stop"
    assert captured["payload"]["tools"] if "tools" in captured["payload"] else True
    assert captured["authorization"] == "Bearer local-key"
    assert response.closed


def test_cancellation_closes_a_blocked_provider_stream(monkeypatch):
    closed = threading.Event()
    entered = threading.Event()

    class BlockingResponse:
        headers = {"Content-Type": "text/event-stream"}

        def readline(self):
            entered.set()
            closed.wait(2)
            return b""

        def close(self):
            closed.set()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: BlockingResponse())
    context = ProviderContext(
        model_id="local",
        model="test-model",
        destination=ProviderDestination.LOCAL,
        api_base="http://127.0.0.1:1234/v1",
        api_key="local-key",
    )
    cancelled = threading.Event()
    errors = []

    def consume():
        try:
            list(
                OpenAICompatibleAdapter().stream(
                    context,
                    ModelRequest(messages=[{"role": "user", "content": "wait"}]),
                    cancelled,
                )
            )
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    assert entered.wait(1)
    cancelled.set()
    worker.join(1)

    assert not worker.is_alive()
    assert closed.is_set()
    assert errors and errors[0].code == "generation_cancelled"


def test_stop_interrupts_real_socket_read_without_waiting_for_provider_timeout():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            entered.set()
            release.wait(5)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    context = ProviderContext(
        model_id="local", model="fixture", destination=ProviderDestination.LOCAL,
        api_base=f"http://127.0.0.1:{server.server_port}/v1", api_key="fixture",
    )
    cancelled = threading.Event()
    errors = []

    def consume():
        try:
            list(OpenAICompatibleAdapter(read_timeout_seconds=20).stream(
                context, ModelRequest(messages=[{"role": "user", "content": "wait"}]), cancelled,
            ))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        worker.join(1)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert errors[0].code == "generation_cancelled"
    finally:
        cancelled.set()
        release.set()
        worker.join(3)
        server.shutdown()
        server.server_close()


@pytest.mark.lmstudio
def test_lm_studio_native_fast_path_smoke():
    api_base = os.environ.get("JARVS_LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
    with urllib.request.urlopen(f"{api_base}/models", timeout=3) as response:
        catalog = json.load(response)
    models = catalog.get("data", [])
    assert models, "LM Studio did not report a loaded model"
    model_id = models[0]["id"]
    context = ProviderContext(
        model_id=model_id,
        model=model_id,
        destination=ProviderDestination.LOCAL,
        api_base=api_base,
        api_key="lm-studio",
    )
    events = OpenAICompatibleAdapter(connect_timeout_seconds=10).stream(
        context,
        ModelRequest(
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: NATIVE FAST PATH OK",
                }
            ]
        ),
        threading.Event(),
    )
    answer = "".join(event.content for event in events).strip()
    assert answer
