import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core.daemon.security import DaemonSecurity
from core.daemon.server import DaemonRuntime, create_app
from core.agent.events import EventType, RunEvent
from core.agent.run_store import (
    RunNotFoundError,
    RunStore,
    RunStoreError,
    RunStoreSequenceError,
)
from core.tools.settings_manager import SettingsManager


def event(run_id, sequence, event_type, payload=None, *, event_id=None):
    return RunEvent.create(
        run_id=run_id,
        sequence=sequence,
        type=event_type,
        payload=payload or {},
        event_id=event_id or f"event-{sequence}",
        occurred_at=f"2026-08-10T10:00:0{sequence}.000Z",
    )


def start(run_id="run-1"):
    return event(
        run_id,
        1,
        EventType.RUN_STARTED,
        {"session_id": "session-1", "generation_id": "generation-1"},
    )


def test_store_is_lazy_until_the_first_run(tmp_path):
    path = tmp_path / ".jarvs" / "run_audit.sqlite3"
    store = RunStore(lambda: path)

    assert store.list_runs() == ()
    assert store.list_effects() == ()
    assert not path.exists()
    with pytest.raises(RunNotFoundError):
        store.get_run("missing")

    store.append_event(start())

    assert path.exists()
    assert store.get_run("run-1").status == "running"


def test_append_only_replay_and_identical_retry(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    started = start()
    completed = event(
        "run-1",
        2,
        EventType.RUN_COMPLETED,
        {"iterations": 1},
    )

    assert store.append_event(started) is True
    assert store.append_event(started) is False
    assert store.append_event(completed) is True

    assert [item.as_dict() for item in store.replay("run-1")] == [
        started.as_dict(),
        completed.as_dict(),
    ]
    summary = store.get_run("run-1")
    assert summary.status == "completed"
    assert summary.completed_at == completed.occurred_at
    assert summary.last_sequence == 2


def test_sequence_gaps_and_conflicting_retries_fail_closed(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    store.append_event(start())

    with pytest.raises(RunStoreSequenceError):
        store.append_event(event("run-1", 3, EventType.RUN_COMPLETED))
    with pytest.raises(RunStoreSequenceError):
        store.append_event(
            event(
                "run-1",
                1,
                EventType.RUN_STARTED,
                {"session_id": "other", "generation_id": "generation-1"},
                event_id="other-event",
            )
        )


def test_effect_projection_correlates_requested_started_and_completed(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    store.append_event(start())
    base = {
        "invocation_id": "invocation-1",
        "tool_name": "workspace.write_text",
        "arguments_hash": "args-hash",
    }
    store.append_event(event("run-1", 2, EventType.TOOL_REQUESTED, base))
    assert store.list_effects(incomplete_only=True)[0].status == "requested"

    metadata = {
        **base,
        "invocation_hash": "invocation-hash",
        "capability": "workspace.write",
        "effect": "workspace_write",
        "risk": "medium",
        "scope": "workspace",
        "tool_version": "1",
    }
    store.append_event(event("run-1", 3, EventType.TOOL_STARTED, metadata))
    store.append_event(event("run-1", 4, EventType.TOOL_COMPLETED, metadata))

    assert store.list_effects(incomplete_only=True) == ()
    effect = store.list_effects(run_id="run-1")[0]
    assert effect.status == "completed"
    assert effect.invocation_hash == "invocation-hash"
    assert effect.started_at is not None
    assert effect.completed_at is not None
    assert effect.last_sequence == 4


def test_effect_lifecycle_requires_request_and_cannot_advance_after_terminal(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    store.append_event(start())
    base = {
        "invocation_id": "invocation-1",
        "tool_name": "workspace.read_text",
        "arguments_hash": "args-hash",
    }
    with pytest.raises(RunStoreError):
        store.append_event(event("run-1", 2, EventType.TOOL_STARTED, base))

    # The failed projection is part of the same transaction as the event.
    assert [item.sequence for item in store.replay("run-1")] == [1]
    store.append_event(event("run-1", 2, EventType.TOOL_REQUESTED, base))
    store.append_event(event("run-1", 3, EventType.TOOL_DENIED, base))
    with pytest.raises(RunStoreError):
        store.append_event(event("run-1", 4, EventType.TOOL_STARTED, base))
    assert store.get_run("run-1").last_sequence == 3


def test_incomplete_runs_and_effects_survive_a_new_store_instance(tmp_path):
    path = tmp_path / "audit.sqlite3"
    first = RunStore(lambda: path)
    first.append_event(start())
    first.append_event(
        event(
            "run-1",
            2,
            EventType.TOOL_REQUESTED,
            {
                "invocation_id": "invocation-1",
                "tool_name": "script.run_python",
                "arguments_hash": "args-hash",
            },
        )
    )
    first.append_event(
        event(
            "run-1",
            3,
            EventType.TOOL_STARTED,
            {
                "invocation_id": "invocation-1",
                "tool_name": "script.run_python",
                "arguments_hash": "args-hash",
                "effect": "code_execution",
            },
        )
    )

    recovered = RunStore(lambda: path)

    assert [item.run_id for item in recovered.incomplete_runs()] == ["run-1"]
    pending = recovered.list_effects(incomplete_only=True)
    assert len(pending) == 1
    assert pending[0].status == "started"
    assert pending[0].effect == "code_execution"


def test_replay_detects_database_sequence_corruption(tmp_path):
    path = tmp_path / "audit.sqlite3"
    store = RunStore(lambda: path)
    store.append_event(start())
    store.append_event(event("run-1", 2, EventType.RUN_COMPLETED))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE run_events SET sequence = 3 WHERE run_id = 'run-1' AND sequence = 2"
        )

    with pytest.raises(RunStoreSequenceError):
        store.replay("run-1")


def test_authenticated_run_api_exposes_bounded_summary_replay_and_effects(tmp_path):
    workspace = tmp_path / "workspace"
    (tmp_path / "models.json").write_text(
        json.dumps({"models": [], "active_model": ""}),
        encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "theme": "Dark",
                "workspace_path": str(workspace),
                "model": "",
                "execution_policy": "always_ask",
                "cloud_workspace_egress": "deny",
            }
        ),
        encoding="utf-8",
    )
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path))
    runtime.run_store.append_event(start("api-run"))
    runtime.run_store.append_event(
        event("api-run", 2, EventType.RUN_COMPLETED, {"iterations": 1})
    )
    security = DaemonSecurity.create("test-token")

    with TestClient(create_app(runtime, security)) as client:
        assert client.get("/runs").status_code == 401
        response = client.get(
            "/runs?limit=1",
            headers={"Authorization": "Bearer test-token"},
        )
        detail = client.get(
            "/runs/api-run",
            headers={"Authorization": "Bearer test-token"},
        )
        missing = client.get(
            "/runs/missing",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.json()["runs"][0]["run_id"] == "api-run"
    assert detail.status_code == 200
    assert [item["sequence"] for item in detail.json()["events"]] == [1, 2]
    assert detail.json()["effects"] == []
    assert missing.status_code == 404
