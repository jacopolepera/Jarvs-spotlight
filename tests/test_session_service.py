import threading
import time

import pytest

from core.daemon.session_service import (
    SessionBusyError,
    SessionService,
    StaleSessionError,
)


class FakeSettings:
    def __init__(self, workspace):
        self.workspace = str(workspace)

    def get_workspace_path(self):
        return self.workspace


class FakeEngine:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.chat_impl = lambda _messages: iter(())

    def chat(self, messages, stream, display):
        assert stream is True
        assert display is False
        return self.chat_impl(messages)


def message(role, content):
    return {"role": role, "type": "message", "content": content}


def test_reset_clears_context_history_without_silent_long_term_persistence(tmp_path):
    history = tmp_path / "history.json"
    history.write_text("[]", encoding="utf-8")
    engine = FakeEngine([
        message("user", "Remember the blue deployment."),
        message("assistant", "I will."),
        message("user", "Close this session."),
    ])
    reload_calls = []
    def reload_after_archive(actual_engine, settings):
        reload_calls.append((actual_engine, settings))

    service = SessionService(
        engine,
        FakeSettings(tmp_path),
        reload_after_archive,
    )
    previous_session_id = service.session_id

    result = service.reset()

    assert result["session_id"] != previous_session_id
    assert result["previous_session_id"] == previous_session_id
    assert result["invalidated_generation_id"] is None
    assert engine.messages == []
    assert not history.exists()
    assert len(reload_calls) == 1
    assert not (tmp_path / "long_term_memory.txt").exists()


def test_stale_session_is_rejected_and_client_generation_id_is_preserved(tmp_path):
    service = SessionService(
        FakeEngine(),
        FakeSettings(tmp_path),
        lambda *_args: None,
    )
    stale_session_id = service.session_id
    current_session_id = service.reset()["session_id"]

    with pytest.raises(StaleSessionError) as error:
        service.start_generation(stale_session_id, "client-generation")

    assert error.value.requested_session_id == stale_session_id
    assert error.value.current_session_id == current_session_id

    generation = service.start_generation(current_session_id, "client-generation")
    assert generation.session_id == current_session_id
    assert generation.generation_id == "client-generation"
    service.finish_generation(generation)


def test_reset_invalidates_active_generation_and_suppresses_late_chunks(tmp_path):
    entered_model = threading.Event()
    release_model = threading.Event()
    engine = FakeEngine([message("user", "old context")])

    def delayed_chunks(_messages):
        engine.messages.append(message("assistant", "first"))
        yield message("assistant", "first")
        entered_model.set()
        assert release_model.wait(timeout=2)
        engine.messages.append(message("assistant", "late"))
        yield message("assistant", "late")

    engine.chat_impl = delayed_chunks
    reload_calls = []
    service = SessionService(
        engine,
        FakeSettings(tmp_path),
        lambda *_args: reload_calls.append(True),
    )
    old_session_id = service.session_id
    generation = service.start_generation(old_session_id, "old-generation")
    received = []

    worker = threading.Thread(
        target=lambda: received.extend(service.stream(generation, [message("user", "go")])),
        daemon=True,
    )
    worker.start()
    assert entered_model.wait(timeout=1)

    reset_result = {}
    reset_errors = []

    def reset_session():
        try:
            reset_result.update(service.reset())
        except Exception as exc:
            reset_errors.append(exc)

    reset_worker = threading.Thread(target=reset_session, daemon=True)
    reset_worker.start()

    deadline = time.monotonic() + 1
    while not generation.cancelled.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert generation.cancelled.is_set()
    with pytest.raises(SessionBusyError):
        service.start_generation(service.session_id, "too-early")

    release_model.set()
    worker.join(timeout=2)
    reset_worker.join(timeout=2)

    assert not worker.is_alive()
    assert not reset_worker.is_alive()
    assert reset_errors == []
    assert reset_result["invalidated_generation_id"] == "old-generation"
    assert [chunk["content"] for chunk in received] == ["first"]
    assert engine.messages == []
    assert reload_calls == [True]

    next_generation = service.start_generation(
        reset_result["session_id"], "new-generation"
    )
    assert next_generation.generation_id == "new-generation"
    service.finish_generation(next_generation)


def test_explicit_snapshot_archiver_never_reinserts_messages_into_new_context(tmp_path):
    summarizer_started = threading.Event()
    release_summarizer = threading.Event()
    engine = FakeEngine([
        message("user", "one"),
        message("assistant", "two"),
        message("user", "three"),
    ])

    archived = []

    def slow_archiver(snapshot):
        summarizer_started.set()
        assert release_summarizer.wait(timeout=2)
        archived.extend(snapshot)

    service = SessionService(
        engine,
        FakeSettings(tmp_path),
        lambda *_args: None,
        snapshot_archiver=slow_archiver,
    )

    reset_result = {}
    reset_worker = threading.Thread(
        target=lambda: reset_result.update(service.reset()),
        daemon=True,
    )
    reset_worker.start()

    assert summarizer_started.wait(timeout=1)
    assert engine.messages == []
    with pytest.raises(SessionBusyError):
        service.start_generation(service.session_id, "too-early")

    release_summarizer.set()
    reset_worker.join(timeout=2)

    assert not reset_worker.is_alive()
    new_session_id = reset_result["session_id"]
    generation = service.start_generation(new_session_id, "new-generation")
    engine.messages.append(message("user", "new context"))
    service.finish_generation(generation)

    assert engine.messages == [message("user", "new context")]
    assert archived[0]["content"] == "one"
    assert not (tmp_path / "long_term_memory.txt").exists()


def test_engine_configuration_waits_for_active_generation(tmp_path):
    generation_entered = threading.Event()
    release_generation = threading.Event()
    configured = threading.Event()
    engine = FakeEngine()

    def delayed_chunks(_messages):
        generation_entered.set()
        assert release_generation.wait(timeout=2)
        yield message("assistant", "done")

    engine.chat_impl = delayed_chunks
    service = SessionService(engine, FakeSettings(tmp_path), lambda *_args: None)
    generation = service.start_generation(service.session_id, "generation")
    stream_worker = threading.Thread(
        target=lambda: list(service.stream(generation, [message("user", "go")])),
        daemon=True,
    )
    stream_worker.start()
    assert generation_entered.wait(timeout=1)

    configure_worker = threading.Thread(
        target=lambda: service.configure_engine(lambda _engine: configured.set()),
        daemon=True,
    )
    configure_worker.start()
    assert not configured.wait(timeout=0.05)

    release_generation.set()
    stream_worker.join(timeout=1)
    configure_worker.join(timeout=1)
    assert not stream_worker.is_alive()
    assert not configure_worker.is_alive()
    assert configured.is_set()
