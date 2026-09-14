import base64
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import core.agent.hermes as hermes_module

from core.agent.hermes import (
    HERMES_REVISION,
    HERMES_TAG,
    HERMES_VERSION,
    HermesAdapterError,
    HermesConfig,
    HermesGatewayAdapter,
    HermesInteractionUnsupported,
    HermesProtocolError,
    HermesRequest,
    HermesUnavailableError,
)


def _runtime(tmp_path: Path, *, scenario: str = "success", contract: int = 5):
    root = tmp_path / "runtime"
    package = root / "tui_gateway"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.20.0"\n',
        encoding="utf-8",
    )
    gateway = f'''import json
import os
import sys

SCENARIO = {scenario!r}
CONTRACT = {contract}

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

def response(request, result):
    send({{"jsonrpc": "2.0", "id": request["id"], "result": result}})

def event(kind, payload, session_id="live"):
    send({{"jsonrpc": "2.0", "method": "event", "params": {{
        "type": kind, "session_id": session_id, "payload": payload
    }}}})

send({{"jsonrpc": "2.0", "method": "event", "params": {{
    "type": "gateway.ready", "payload": {{"change_events": True}}
}}}})
approval_choice = ""
attach_count = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params") or {{}}
    if method == "session.create":
        response(request, {{
            "session_id": "live",
            "stored_session_id": "stored",
            "info": {{"desktop_contract": CONTRACT}},
        }})
    elif method == "image.attach_bytes":
        attach_count += 1
        response(request, {{"attached": True}})
    elif method == "prompt.submit":
        response(request, {{"status": "streaming"}})
        if SCENARIO == "clarify":
            event("clarify.request", {{"request_id": "clarify-1", "question": "private"}})
        else:
            event("message.delta", {{"text": "hello "}})
            event("tool.start", {{
                "tool_call_id": "tool-1",
                "name": "web_search",
                "args": {{"api_key": "must-not-cross"}},
            }})
            event("approval.request", {{
                "title": "Bounded action",
                "command": "secret command",
                "choices": ["once", "deny"],
            }})
    elif method == "approval.respond":
        approval_choice = params.get("choice", "")
        response(request, {{"resolved": True}})
        event("message.delta", {{"text": "world"}})
        snapshot = {{
            "approval": approval_choice,
            "attached": attach_count,
            "host_secret": os.environ.get("OPENAI_API_KEY"),
            "safe_mode": os.environ.get("HERMES_SAFE_MODE"),
            "toolsets": os.environ.get("HERMES_TUI_TOOLSETS"),
            "home_is_ephemeral": "jarvs-run-" in os.environ.get("HERMES_HOME", ""),
        }}
        event("tool.complete", {{"tool_call_id": "tool-1", "result": "private result"}})
        event("message.complete", {{
            "text": "hello world", "status": "complete", "snapshot": snapshot
        }})
    elif method == "clarify.respond":
        response(request, {{"resolved": True}})
    elif method == "session.interrupt":
        response(request, {{"status": "interrupted"}})
    elif method == "session.close":
        response(request, {{"closed": True}})
    elif method == "session.delete":
        response(request, {{"deleted": "stored"}})
        break
'''
    (package / "entry.py").write_text(gateway, encoding="utf-8")
    return root


def _adapter(tmp_path: Path, **runtime_options):
    runtime = _runtime(tmp_path, **runtime_options)
    home = tmp_path / "home"
    home.mkdir()
    adapter = HermesGatewayAdapter(
        HermesConfig(
            runtime_root=runtime,
            python_path=Path(sys.executable),
            home_root=home,
            startup_timeout=5,
            rpc_timeout=5,
            turn_timeout=5,
            shutdown_timeout=1,
        )
    )
    adapter._verify_runtime = lambda cancelled=None: None
    adapter._verify_venv_attestation = lambda _layout, cancelled=None: None
    adapter._create_runtime_snapshot = (
        lambda root, _run_home, cancelled=None, capability=None: root
    )
    return adapter, home


def _request(tmp_path: Path, *, capability="research", images=()):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return HermesRequest(
        messages=[{"role": "user", "content": "Research this"}],
        workspace_root=workspace,
        capability=capability,
        model="openai/local-model",
        provider="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key="lm-studio",
        images=images,
    )


def _wire_event(kind: str, payload, *, session_id="live"):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": kind, "session_id": session_id, "payload": payload},
    }


def _defer(adapter, *messages):
    for message in messages:
        encoded = json.dumps(message).encode("utf-8")
        item = hermes_module._QueuedMessage(message, len(encoded))
        adapter._deferred.append(item)
        adapter._deferred_bytes += item.size


def _write_attestation(runtime: Path, python_path: Path, *, extra=None):
    venv = python_path.parent.parent
    files = {
        path.relative_to(venv).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in venv.rglob("*")
        if path.is_file()
    }
    manifest = {
        "schema_version": 1,
        "hermes_version": HERMES_VERSION,
        "hermes_tag": HERMES_TAG,
        "hermes_revision": HERMES_REVISION,
        "python_executable": python_path.relative_to(runtime).as_posix(),
        "python_version": "3.13.1",
        "dependency_lock_sha256": hashlib.sha256(
            (runtime / "uv.lock").read_bytes()
        ).hexdigest(),
        "files": files,
    }
    if extra:
        manifest.update(extra)
    (runtime / ".jarvs-hermes-attestation.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )


def test_gateway_round_trip_is_ephemeral_normalized_and_secret_free(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret-must-not-cross")
    adapter, home = _adapter(tmp_path)
    events = []
    approvals = []
    request = _request(tmp_path)

    result = adapter.execute(
        request,
        threading.Event(),
        event_notifier=events.append,
        interactive_handler=lambda event: approvals.append(event) or True,
    )

    assert result["text"] == "hello world"
    assert result["provider"] == "lmstudio"
    assert adapter.status().as_dict() == {
        "state": "stopped",
        "ready": False,
        "pid": None,
        "version": "0.20.0",
        "error": None,
    }
    assert approvals[0]["payload"]["title"] == "Bounded action"
    serialized = json.dumps(events)
    assert approvals[0]["payload"]["display_command"] == "secret command"
    assert "must-not-cross" not in serialized
    assert "private result" not in serialized
    tool_start = next(event for event in events if event["type"] == "tool.start")
    assert tool_start["payload"]["argument_keys"] == ["api_key"]
    assert list((home / "runs").iterdir()) == []


def test_environment_uses_exact_toolset_without_safe_mode_or_host_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    complete_payloads = []

    # The fake gateway reports environment invariants inside an otherwise
    # ignored upstream payload. Inspecting the raw message at this seam avoids
    # weakening production normalization merely for diagnostics.
    original = adapter._consume_turn

    def inspect(*args, **kwargs):
        return original(*args, **kwargs)

    adapter._consume_turn = inspect
    result = adapter.execute(
        request,
        threading.Event(),
        event_notifier=lambda event: complete_payloads.append(event),
        interactive_handler=lambda _event: True,
    )

    assert result["text"] == "hello world"
    environment = adapter._build_environment(
        request,
        adapter._resolve_layout(),
        Path(adapter.config.home_root) / "synthetic-home",
    )
    assert environment["HERMES_TUI_TOOLSETS"] == "web"
    assert "HERMES_IGNORE_USER_CONFIG" not in environment
    assert "HERMES_SAFE_MODE" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["LM_API_KEY"] == "lm-studio"


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("research", "web"),
        ("vision", "vision"),
        ("coding_fixture", "file,terminal"),
        ("browser_fixture", "browser"),
        ("delegation_fixture", "delegation"),
    ],
)
def test_capabilities_map_to_exact_explicit_toolsets(
    tmp_path, capability, expected
):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path, capability=capability)
    if capability == "vision":
        request = _request(
            tmp_path,
            capability=capability,
            images=(
                {
                    "format": "base64.png",
                    "content": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
                },
            ),
        )
    if capability.endswith("_fixture"):
        (Path(request.workspace_root) / ".jarvs-hermes-fixture").write_text("")
    assert adapter.preflight(request) is None
    environment = adapter._build_environment(
        request,
        adapter._resolve_layout(),
        Path(adapter.config.home_root) / "synthetic-home",
    )
    assert environment["HERMES_TUI_TOOLSETS"] == expected


def test_preflight_fails_closed_for_vision_unknown_remote_and_revision(tmp_path):
    adapter, _home = _adapter(tmp_path)
    assert adapter.preflight(_request(tmp_path, capability="vision")) == (
        "hermes_vision_image_required"
    )
    assert adapter.preflight(_request(tmp_path, capability="unknown")) == (
        "hermes_capability_not_allowed"
    )

    remote = _request(tmp_path)
    remote = HermesRequest(
        **{**remote.__dict__, "base_url": "https://api.example.test/v1"}
    )
    assert adapter.preflight(remote) == "hermes_local_provider_required"

    adapter._verify_runtime = HermesGatewayAdapter._verify_runtime.__get__(adapter)
    adapter._git_capture = lambda root, args, **_kwargs: (
        str(root) if args == ["rev-parse", "--show-toplevel"] else None
    )
    adapter._read_revision = lambda _root, cancelled=None: "0" * 40
    assert adapter.preflight(_request(tmp_path)) == (
        "hermes_runtime_revision_mismatch"
    )


def test_modified_runtime_is_rejected_and_rechecked_immediately_before_spawn(
    tmp_path,
):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    checks = iter([None, "hermes_runtime_modified"])
    adapter._verify_runtime = lambda cancelled=None: next(checks)
    with pytest.raises(HermesUnavailableError, match="runtime_modified"):
        adapter.execute(request, threading.Event())
    assert adapter.status().pid is None


def test_contract_mismatch_fails_and_removes_ephemeral_home(tmp_path):
    adapter, home = _adapter(tmp_path, contract=4)
    with pytest.raises(HermesProtocolError, match="desktop_contract"):
        adapter.execute(_request(tmp_path), threading.Event())
    assert list((home / "runs").iterdir()) == []
    assert adapter.status().state == "error"


def test_clarify_is_rejected_and_session_is_interrupted(tmp_path):
    adapter, home = _adapter(tmp_path, scenario="clarify")
    with pytest.raises(HermesInteractionUnsupported):
        adapter.execute(_request(tmp_path), threading.Event())
    assert list((home / "runs").iterdir()) == []


def test_close_is_permanent_and_old_configuration_cannot_restart(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)

    adapter.close()

    assert adapter.status().state == "closed"
    assert adapter.preflight(request) == "hermes_adapter_closed"
    with pytest.raises(HermesUnavailableError, match="adapter_closed"):
        adapter.execute(request, threading.Event())


def test_success_fails_if_ephemeral_transcript_cannot_be_removed(tmp_path):
    adapter, _home = _adapter(tmp_path)
    adapter._remove_run_home = lambda _path: (_ for _ in ()).throw(
        OSError("locked")
    )

    with pytest.raises(HermesAdapterError, match="cleanup_failed"):
        adapter.execute(
            _request(tmp_path),
            threading.Event(),
            interactive_handler=lambda _event: True,
        )


def test_image_rpc_budget_accepts_payload_larger_than_generic_rpc_limit(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(
        tmp_path,
        capability="vision",
        images=(
            {
                "format": "base64.png",
                "content": base64.b64encode(
                    b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024)
                ).decode("ascii"),
            },
        ),
    )

    result = adapter.execute(request, threading.Event())

    assert result["text"] == "hello world"


def test_real_clean_check_rejects_any_dirty_or_untracked_runtime_source(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    runtime = _runtime(tmp_path)
    subprocess = __import__("subprocess")
    subprocess.run([git, "-C", str(runtime), "init"], check=True, capture_output=True)
    subprocess.run([git, "-C", str(runtime), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            git,
            "-C",
            str(runtime),
            "-c",
            "user.name=Jarvs test",
            "-c",
            "user.email=jarvs@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    adapter = HermesGatewayAdapter(
        HermesConfig(
            runtime_root=runtime,
            python_path=Path(sys.executable),
            home_root=tmp_path / "home-clean",
        )
    )
    (runtime / "hermes_bootstrap.py").write_text("raise SystemExit(1)\n")

    assert adapter._runtime_code_is_clean(runtime) is False


def test_request_count_and_image_count_are_bounded(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    too_many_messages = HermesRequest(
        **{**request.__dict__, "messages": request.messages * 25}
    )
    assert adapter.preflight(too_many_messages) == "hermes_too_many_messages"

    image = {
        "format": "base64.png",
        "content": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
    }
    too_many_images = HermesRequest(
        **{**request.__dict__, "images": (image,) * 9}
    )
    assert adapter.preflight(too_many_images) == "hermes_too_many_images"


def test_safe_mode_and_plugin_profile_are_capability_scoped(tmp_path):
    adapter, _home = _adapter(tmp_path)
    for capability in ("research", "browser_fixture"):
        request = _request(tmp_path, capability=capability)
        env = adapter._build_environment(
            request, adapter._resolve_layout(), tmp_path / "synthetic"
        )
        assert "HERMES_SAFE_MODE" not in env
    for capability in ("vision", "coding_fixture", "delegation_fixture"):
        request = _request(tmp_path, capability=capability)
        env = adapter._build_environment(
            request, adapter._resolve_layout(), tmp_path / "synthetic"
        )
        assert env["HERMES_SAFE_MODE"] == "1"
        assert env["PYTHONPYCACHEPREFIX"].endswith(".pycache")

    run_home = adapter._create_run_home()
    config = (run_home / "config.yaml").read_text(encoding="utf-8")
    assert "enabled: []" in config
    assert "disabled: []" in config
    assert "mcp_servers: {}" in config
    assert "outbound: []" in config
    assert "platforms: {}" in config
    assert "allow_lazy_installs: false" in config
    adapter._remove_run_home(run_home)


def test_venv_attestation_is_exact_and_rejects_bytecode_or_drift(tmp_path):
    runtime = _runtime(tmp_path)
    (runtime / "uv.lock").write_bytes(b"locked")
    python_path = runtime / "venv" / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True)
    python_path.write_bytes(b"pinned interpreter")
    package = runtime / "venv" / "Lib" / "site-packages" / "gateway.py"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"pinned dependency")
    home = tmp_path / "attested-home"
    home.mkdir()
    adapter = HermesGatewayAdapter(HermesConfig(runtime, python_path, home))
    layout = hermes_module._Layout(runtime.resolve(), python_path.resolve())

    _write_attestation(runtime, python_path)
    assert adapter._verify_venv_attestation(layout) is None

    package.write_bytes(b"mutated dependency")
    assert adapter._verify_venv_attestation(layout) == "hermes_venv_drift"
    package.write_bytes(b"pinned dependency")
    _write_attestation(runtime, python_path)
    pyc = package.parent / "gateway.pyc"
    pyc.write_bytes(b"sourceless executable bytecode")
    assert adapter._verify_venv_attestation(layout) == (
        "hermes_venv_bytecode_forbidden"
    )
    pyc.unlink()
    _write_attestation(runtime, python_path, extra={"generated_at_utc": "now"})
    assert adapter._verify_venv_attestation(layout) == "hermes_attestation_invalid"


def test_fake_path_git_is_never_selected(tmp_path, monkeypatch):
    fake = tmp_path / ("git.exe" if os.name == "nt" else "git")
    fake.write_text("malicious", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    adapter, _home = _adapter(tmp_path / "nested")

    assert adapter._git_path is None or adapter._git_path != fake.resolve()


@pytest.mark.parametrize(
    ("capability", "kept"),
    [("research", "web"), ("browser_fixture", "browser")],
)
def test_non_safe_mode_snapshot_exposes_only_required_bundled_plugins(
    tmp_path, capability, kept
):
    snapshot = tmp_path / capability
    plugins = snapshot / "plugins"
    for name in ("web", "browser", "image_gen", "platforms"):
        (plugins / name).mkdir(parents=True)
        (plugins / name / "plugin.yaml").write_text("name: fixture")
    (plugins / "__init__.py").write_text("")

    HermesGatewayAdapter._restrict_bundled_plugins(snapshot, capability)

    assert sorted(path.name for path in plugins.iterdir()) == ["__init__.py", kept]


def test_fixture_is_revalidated_after_snapshot_before_spawn(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path, capability="coding_fixture")
    marker = Path(request.workspace_root) / ".jarvs-hermes-fixture"
    marker.write_text("consent")

    def remove_marker(root, _run_home, cancelled=None, capability=None):
        marker.unlink()
        return root

    adapter._create_runtime_snapshot = remove_marker
    with pytest.raises(HermesUnavailableError, match="fixture_marker_missing"):
        adapter.execute(request, threading.Event())
    assert adapter.status().pid is None


def test_cancellation_during_preflight_never_snapshots_or_spawns(tmp_path):
    adapter, _home = _adapter(tmp_path)
    cancel_signal = threading.Event()
    snapshots = []

    def cancel_during_verification(cancelled=None):
        cancel_signal.set()
        raise hermes_module._HermesCancelled()

    adapter._verify_runtime = cancel_during_verification
    adapter._create_runtime_snapshot = lambda *args, **kwargs: snapshots.append(args)

    result = adapter.execute(_request(tmp_path), cancel_signal)

    assert result["cancelled"] is True
    assert snapshots == []
    assert adapter.status().pid is None


def test_complete_text_is_canonical_after_interim_and_delta_revisions(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    _defer(
        adapter,
        _wire_event("message.delta", {"text": "draft"}),
        _wire_event("message.interim", {"text": "revised"}),
        _wire_event("message.complete", {"text": "canonical", "status": "complete"}),
    )
    events = []

    result = adapter._consume_turn(
        request,
        "live",
        threading.Event(),
        event_notifier=events.append,
        interactive_handler=None,
    )

    assert result["text"] == "canonical"
    assert events == [
        {
            "type": "message.complete",
            "payload": {"content_chars": 9, "status": "complete"},
        }
    ]


def test_large_final_is_transactional_and_bounded_for_transcript(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    success_text = "x" * (64 * 1024)
    _defer(
        adapter,
        _wire_event(
            "message.complete", {"text": success_text, "status": "complete"}
        ),
    )
    result = adapter._consume_turn(
        request,
        "live",
        threading.Event(),
        event_notifier=None,
        interactive_handler=None,
    )
    assert result["text"] == success_text
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 96 * 1024

    too_large = "x" * (93 * 1024)
    _defer(
        adapter,
        _wire_event("message.delta", {"text": too_large}),
        _wire_event("message.complete", {"text": too_large, "status": "complete"}),
    )
    notifications = []
    with pytest.raises(HermesProtocolError, match="response_too_large"):
        adapter._consume_turn(
            request,
            "live",
            threading.Event(),
            event_notifier=notifications.append,
            interactive_handler=None,
        )
    assert notifications == []


def test_incomplete_and_unknown_blocking_requests_fail_closed(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    _defer(
        adapter,
        _wire_event("message.complete", {"text": "partial", "status": "stop"}),
    )
    with pytest.raises(HermesProtocolError, match="turn_failed"):
        adapter._consume_turn(
            request,
            "live",
            threading.Event(),
            event_notifier=None,
            interactive_handler=None,
        )

    _defer(adapter, _wire_event("preview.request", {"request_id": "bounded"}))
    with pytest.raises(HermesInteractionUnsupported):
        adapter._consume_turn(
            request,
            "live",
            threading.Event(),
            event_notifier=None,
            interactive_handler=None,
        )


def test_cancel_after_approval_decision_prevents_approval_response(tmp_path):
    adapter, _home = _adapter(tmp_path)
    request = _request(tmp_path)
    cancelled = threading.Event()
    calls = []
    adapter._rpc = lambda *args, **kwargs: calls.append(args)
    _defer(adapter, _wire_event("approval.request", {"command": "echo safe"}))

    def decide(_event):
        cancelled.set()
        return True

    with pytest.raises(hermes_module._HermesCancelled):
        adapter._consume_turn(
            request,
            "live",
            cancelled,
            event_notifier=None,
            interactive_handler=decide,
        )
    assert calls == []


def test_display_command_strips_control_format_and_bidi_characters():
    event = hermes_module._normalize_event(
        "approval.request",
        {"command": "echo\nsecret\t\u202emisdirect\u200b"},
    )
    assert event["payload"]["display_command"] == "echosecretmisdirect"
    assert len(event["payload"]["command_hash"]) == 64


def test_continuous_wrong_session_frames_still_obey_absolute_deadline(
    tmp_path, monkeypatch
):
    adapter, _home = _adapter(tmp_path)
    object.__setattr__(adapter.config, "turn_timeout", 0.5)
    _defer(
        adapter,
        _wire_event("status", {}, session_id="other"),
        _wire_event("status", {}, session_id="other"),
    )
    moments = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(
        hermes_module.time, "monotonic", lambda: next(moments, 1.0)
    )

    with pytest.raises(HermesProtocolError, match="turn_timeout"):
        adapter._consume_turn(
            _request(tmp_path),
            "live",
            threading.Event(),
            event_notifier=None,
            interactive_handler=None,
        )


def test_blocked_writer_is_cancelled_by_terminating_owned_process(tmp_path):
    adapter, _home = _adapter(tmp_path)
    released = threading.Event()
    cancelled = threading.Event()

    class BlockingInput:
        def write(self, _frame):
            released.wait(5)
            return 1

        def flush(self):
            return None

    class Process:
        stdin = BlockingInput()

        @staticmethod
        def poll():
            return None

    process = Process()
    adapter._process = process
    adapter._terminate_tree = lambda _process, _handle=None: released.set()
    threading.Timer(0.05, cancelled.set).start()
    started = time.monotonic()

    with pytest.raises(hermes_module._HermesCancelled):
        adapter._write_frame(
            process,
            b"large frame",
            deadline=time.monotonic() + 2,
            cancelled=cancelled,
        )
    assert time.monotonic() - started < 0.5
    assert released.is_set()
