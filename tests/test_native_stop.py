import json
import threading

import pytest

from core.agent.approvals import ApprovalConflictError
from core.agent.model_adapter import ModelGenerationCancelled, ModelStreamEvent, ModelToolCallDelta
from core.agent.process_backend import ManagedProcessBackend
from core.agent.routing import RoutingSignal
from core.agent.tooling import ToolInvocation
from core.daemon.session_service import SessionBusyError
from tests.test_run_coordinator import FixtureAdapter, service


def test_stop_before_any_tool_effect_discards_interrupted_turn_and_keeps_prior_history(tmp_path):
    adapter = FixtureAdapter([
        [ModelStreamEvent(content="remembered", finish_reason="stop")],
        [ModelStreamEvent(content="partial")],
        [ModelStreamEvent(content="next", finish_reason="stop")],
    ])
    native, _settings = service(tmp_path, adapter)
    first = native.start_generation("session", "first")
    list(native.stream(first, [{"role": "user", "content": "keep this"}]))
    interrupted = native.start_generation("session", "interrupted")
    stream = native.stream(interrupted, [{"role": "user", "content": "discard this"}])
    for chunk in stream:
        if chunk.get("content") == "partial":
            native.cancel_generation("session", "interrupted")
            break
    with pytest.raises(ModelGenerationCancelled):
        list(stream)
    next_generation = native.start_generation("session", "next")
    list(native.stream(next_generation, [{"role": "user", "content": "continue"}]))
    history = adapter.requests[-1][1].messages
    assert any(message.get("content") == "remembered" for message in history)
    assert "partial" not in str(history)
    assert "discard this" not in str(history)


@pytest.mark.parametrize("structured", [True, False])
def test_cloud_egress_deny_keeps_local_output_out_of_next_turn(tmp_path, structured):
    sensitive = "private-result-sentinel"
    responses = []
    if not structured:
        responses.append([ModelStreamEvent(tool_calls=(ModelToolCallDelta(
            index=0, call_id="read-private", name="workspace__read_text",
            arguments='{"path":"private.txt"}',
        ),), finish_reason="tool_calls")])
    responses.append([ModelStreamEvent(content="next", finish_reason="stop")])
    adapter = FixtureAdapter(responses)
    native, settings = service(tmp_path, adapter, provider="cloud", egress="deny")
    (settings.workspace / "private.txt").write_text(sensitive)
    generation = native.start_generation(
        "session", "read", routing_signal=RoutingSignal(tool_name="workspace.read_text"),
        tool_invocation=ToolInvocation.create("workspace.read_text", {"path": "private.txt"}) if structured else None,
    )
    chunks = list(native.stream(generation, [{"role": "user", "content": "read private file"}]))
    assert len(adapter.requests) == (0 if structured else 1)
    assert any(sensitive in str(chunk.get("content")) for chunk in chunks if chunk.get("type") == "message")
    followup = native.start_generation("session", "followup")
    list(native.stream(followup, [{"role": "user", "content": "continue"}]))
    assert sensitive not in str(adapter.requests[-1][1].messages)


@pytest.mark.parametrize("egress", ["allow", "deny"])
def test_switching_local_tool_history_to_cloud_respects_egress(tmp_path, egress):
    sensitive = "workspace-result-sentinel"
    adapter = FixtureAdapter([
        [ModelStreamEvent(content=sensitive, finish_reason="stop")],
        [ModelStreamEvent(content="derived " + sensitive, finish_reason="stop")],
        [ModelStreamEvent(content="cloud answer", finish_reason="stop")],
        [ModelStreamEvent(content="local answer", finish_reason="stop")],
        [ModelStreamEvent(content="safe answer after reset", finish_reason="stop")],
        [ModelStreamEvent(content="cloud after reset", finish_reason="stop")],
    ])
    native, settings = service(tmp_path, adapter)
    (settings.workspace / "private.txt").write_text(sensitive)
    read = native.start_generation("session", "read", tool_invocation=ToolInvocation.create(
        "workspace.read_text", {"path": "private.txt"}
    ))
    list(native.stream(read, [{"role": "user", "content": "read file"}]))
    summary = native.start_generation("session", "summary")
    list(native.stream(summary, [{"role": "user", "content": "summarize previous result"}]))

    settings.provider = "cloud"
    settings.egress = egress
    cloud = native.start_generation("session", "cloud")
    list(native.stream(cloud, [{"role": "user", "content": "new cloud question"}]))
    cloud_messages = adapter.requests[-1][1].messages
    assert (sensitive in str(cloud_messages)) is (egress == "allow")
    assert any(message.get("content") == "new cloud question" for message in cloud_messages)

    settings.provider = "local"
    local = native.start_generation("session", "local")
    list(native.stream(local, [{"role": "user", "content": "back to local"}]))
    assert sensitive in str(adapter.requests[-1][1].messages)

    native.reset()
    safe = native.start_generation(native.session_id, "safe")
    list(native.stream(safe, [{"role": "user", "content": "safe question after reset"}]))
    settings.provider = "cloud"
    settings.egress = "deny"
    clean_cloud = native.start_generation(native.session_id, "clean-cloud")
    list(native.stream(clean_cloud, [{"role": "user", "content": "continue safely"}]))
    assert "safe answer after reset" in str(adapter.requests[-1][1].messages)
    assert sensitive not in str(adapter.requests[-1][1].messages)


@pytest.mark.parametrize("provider", ["local", "cloud"])
def test_stop_during_summary_keeps_completed_effect_and_excludes_partial_answer(tmp_path, provider):
    adapter = FixtureAdapter([
        [ModelStreamEvent(content="unfinished summary")],
        [ModelStreamEvent(content="continued", finish_reason="stop")],
    ])
    native, settings = service(tmp_path, adapter, provider=provider, egress="allow")
    generation = native.start_generation(
        "session", "write", tool_invocation=ToolInvocation.create(
            "workspace.write_text", {"path": "done.txt", "content": "written"}
        ),
    )
    stream = native.stream(generation, [{"role": "user", "content": "write file"}])
    for chunk in stream:
        if chunk.get("content") == "unfinished summary":
            native.cancel_generation("session", "write")
            break
    with pytest.raises(SessionBusyError):
        native.start_generation("session", "too-soon")
    with pytest.raises(ModelGenerationCancelled):
        list(stream)
    assert (settings.workspace / "done.txt").read_text() == "written"

    followup = native.start_generation("session", "followup")
    list(native.stream(followup, [{"role": "user", "content": "continue"}]))
    history = adapter.requests[-1][1].messages
    assert "unfinished summary" not in str(history)
    assert sum(message.get("content") == "write file" for message in history) == 1
    call = next(message for message in history if message.get("tool_calls"))
    result = next(message for message in history if message["role"] == "tool")
    assert call["tool_calls"][0]["id"] == result["tool_call_id"]
    assert json.loads(result["content"])["path"] == "done.txt"


def test_stop_in_batch_approval_keeps_only_first_completed_call(tmp_path):
    adapter = FixtureAdapter([
        [ModelStreamEvent(tool_calls=tuple(
            ModelToolCallDelta(index=index, call_id=name, name="workspace__write_text",
                               arguments=json.dumps({"path": name + ".txt", "content": name}))
            for index, name in enumerate(["first", "second"])
        ), finish_reason="tool_calls")],
        [ModelStreamEvent(content="continue", finish_reason="stop")],
    ])
    captured = []
    native, settings = service(tmp_path, adapter, event_sinks=(captured.append,))
    generation = native.start_generation("session", "batch", routing_signal=RoutingSignal(tool_name="workspace.write_text"))
    stream = native.stream(generation, [{"role": "user", "content": "write both"}])
    approval = None
    for chunk in stream:
        if chunk.get("type") == "activity" and chunk["content"]["event"] == "tool.completed":
            settings.policy = "always_ask"
        if chunk.get("type") == "approval_required":
            approval = chunk
            native.cancel_generation("session", "batch")
            break
    assert approval is not None
    with pytest.raises(ModelGenerationCancelled):
        list(stream)
    assert (settings.workspace / "first.txt").exists()
    assert not (settings.workspace / "second.txt").exists()
    assert captured[-1].type == "run.cancelled"
    assert all(event.type != "run.failed" for event in captured)
    with pytest.raises(ApprovalConflictError):
        native.approval_broker.resolve(approval["approval_id"], approve=True,
                                       session_id="session", generation_id="batch")
    next_generation = native.start_generation("session", "next")
    list(native.stream(next_generation, [{"role": "user", "content": "continue"}]))
    history = adapter.requests[-1][1].messages
    calls = [call["id"] for message in history for call in message.get("tool_calls", [])]
    results = [message["tool_call_id"] for message in history if message["role"] == "tool"]
    assert calls == results == ["first"]


def test_stop_waits_for_running_process_to_exit_before_releasing_session(tmp_path, monkeypatch):
    import core.agent.process_backend as process_backend

    spawned = threading.Event()
    processes = []
    original_popen = process_backend.subprocess.Popen

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(process_backend.subprocess, "Popen", capture_process)
    native, settings = service(tmp_path, FixtureAdapter([]), action_backend=ManagedProcessBackend().execute)
    (settings.workspace / "wait.py").write_text("import time\ntime.sleep(30)\n")
    generation = native.start_generation("session", "process", tool_invocation=ToolInvocation.create(
        "script.run_python", {"path": "wait.py"}
    ))
    errors = []

    def consume():
        try:
            list(native.stream(generation, [{"role": "user", "content": "run script"}]))
        except ModelGenerationCancelled as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    try:
        assert spawned.wait(3)
        native.cancel_generation("session", "process")
        worker.join(8)
        assert not worker.is_alive()
        assert processes[0].poll() is not None
        assert len(errors) == 1
        native.start_generation("session", "after-process")
    finally:
        generation.cancelled.set()
        if processes and processes[0].poll() is None:
            processes[0].kill()
