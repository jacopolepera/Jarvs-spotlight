"""Restart boundaries must never turn an uncertain effect into an automatic retry."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agent.events import EventType, RunEvent
from core.agent.run_store import RunStore, RunStoreError
from core.daemon.run_recovery_api import create_run_recovery_router, exclusive_run_store_owner


def append(store, run_id, kind, payload=None):
    try:
        sequence = store.get_run(run_id).last_sequence + 1
    except LookupError:
        sequence = 1
    store.append_event(RunEvent.create(
        run_id=run_id, sequence=sequence, type=kind, payload=payload or {}
    ))


def interrupted(store, *, run_id="interrupted", started=True):
    append(store, run_id, EventType.RUN_STARTED, {"session_id": "session"})
    fields = {"invocation_id": "tool-1", "tool_name": "workspace.write_text", "arguments_hash": "hash-only"}
    append(store, run_id, EventType.TOOL_REQUESTED, fields)
    append(store, run_id, EventType.TOOL_STARTED if started else EventType.TOOL_APPROVAL_REQUIRED, {
        **fields, "effect": "workspace_write", "risk": "medium"
    })


def test_recovery_and_reads_do_not_create_an_unused_database(tmp_path):
    store = RunStore(lambda: tmp_path / "unused.sqlite3")
    assert store.recover_interrupted_runs() == {"runs": 0, "cancelled_effects": 0, "uncertain_effects": 0}
    assert store.list_recoveries() == ()
    assert not store.path.exists()


def test_abrupt_process_exit_preserves_uncertain_effect_without_replaying_it(tmp_path):
    path = tmp_path / "audit.sqlite3"
    external_effect = tmp_path / "effect.txt"
    script = """
import os, sys
from pathlib import Path
from core.agent.events import RunEvent, EventType
from core.agent.run_store import RunStore
from core.daemon.run_recovery_api import exclusive_run_store_owner
store = RunStore(lambda: sys.argv[1])
owner = exclusive_run_store_owner(store)
owner.__enter__()
base = {'invocation_id':'tool-1','tool_name':'workspace.write_text','arguments_hash':'hash-only'}
for seq, kind, payload in [(1, EventType.RUN_STARTED, {}), (2, EventType.TOOL_REQUESTED, base), (3, EventType.TOOL_STARTED, base)]:
    store.append_event(RunEvent.create(run_id='crashed', sequence=seq, type=kind, payload=payload))
Path(sys.argv[2]).write_text('applied once', encoding='utf-8')
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), str(external_effect)],
                            cwd=Path(__file__).resolve().parents[1], check=False)
    assert result.returncode == 23
    restarted = RunStore(lambda: path)
    with exclusive_run_store_owner(restarted):
        assert restarted.recover_interrupted_runs() == {"runs": 1, "cancelled_effects": 0, "uncertain_effects": 1}
    assert external_effect.read_text(encoding="utf-8") == "applied once"
    assert restarted.get_run("crashed").status == "failed"
    effect = restarted.list_effects()[0]
    assert effect.status == "failed"
    assert effect.reason_code == "restart_outcome_unknown"
    assert restarted.list_recoveries()[0].resolution is None
    events = restarted.replay("crashed")
    assert [item.sequence for item in events] == [1, 2, 3, 4, 5]
    assert restarted.recover_interrupted_runs()["runs"] == 0
    assert restarted.replay("crashed") == events


def test_not_started_effects_are_cancelled_and_terminal_runs_preserved(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    interrupted(store, started=False)
    append(store, "completed", EventType.RUN_STARTED)
    append(store, "completed", EventType.RUN_COMPLETED)
    before = store.replay("completed")
    assert store.recover_interrupted_runs() == {"runs": 1, "cancelled_effects": 1, "uncertain_effects": 0}
    assert store.list_recoveries() == ()
    effect = store.list_effects(run_id="interrupted")[0]
    assert effect.status == "cancelled"
    assert effect.reason_code == "restart_before_start"
    assert effect.started_at is None
    assert store.replay("completed") == before


def test_recovery_rolls_back_the_entire_run_on_failure(tmp_path, monkeypatch):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    interrupted(store)
    before = store.replay("interrupted")
    original = store._append_event

    def fail_terminal(connection, event):
        if event.type == EventType.RUN_FAILED.value:
            raise OSError("simulated disk failure")
        return original(connection, event)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_append_event", fail_terminal)
        with pytest.raises(OSError):
            store.recover_interrupted_runs()
    assert store.replay("interrupted") == before
    assert store.list_effects()[0].status == "started"
    assert store.list_recoveries() == ()
    assert store.recover_interrupted_runs()["uncertain_effects"] == 1


def test_acknowledgement_is_durable_idempotent_and_does_not_relabel_effect(tmp_path):
    path = tmp_path / "audit.sqlite3"
    store = RunStore(lambda: path)
    interrupted(store)
    store.recover_interrupted_runs()
    record = store.acknowledge_recovery("interrupted", "tool-1", resolution="confirmed_applied")
    events = store.replay("interrupted")
    restarted = RunStore(lambda: path)
    assert restarted.list_recoveries() == ()
    assert restarted.list_recoveries(include_acknowledged=True) == (record,)
    assert restarted.acknowledge_recovery("interrupted", "tool-1", resolution="confirmed_applied") == record
    assert restarted.replay("interrupted") == events
    assert restarted.list_effects()[0].reason_code == "restart_outcome_unknown"
    assert restarted.get_run("interrupted").status == "failed"
    with pytest.raises(RunStoreError):
        restarted.acknowledge_recovery("interrupted", "tool-1", resolution="confirmed_not_applied")


def test_recovery_metadata_excludes_arguments_results_and_freeform_notes(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    interrupted(store)
    store.recover_interrupted_runs()
    store.acknowledge_recovery("interrupted", "tool-1", resolution="reviewed")
    for item in store.replay("interrupted")[3:]:
        assert set(item.payload_dict()) <= {
            "invocation_id", "tool_name", "arguments_hash", "reason_code",
            "recovery_required", "uncertain_effects", "resolution"
        }
    with pytest.raises(ValueError):
        store.acknowledge_recovery("interrupted", "tool-1", resolution="secret-freeform-value")
    assert "secret-freeform-value" not in json.dumps([event.as_dict() for event in store.replay("interrupted")])


def test_concurrent_store_instances_cannot_duplicate_recovery_or_acknowledgement(tmp_path):
    path = tmp_path / "audit.sqlite3"
    first = RunStore(lambda: path)
    second = RunStore(lambda: path)
    interrupted(first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda store: store.recover_interrupted_runs(), [first, second]))
        assert sum(item["runs"] for item in outcomes) == 1
        acknowledgements = list(pool.map(lambda store: store.acknowledge_recovery(
            "interrupted", "tool-1", resolution="reviewed"), [first, second]))
    assert acknowledgements[0] == acknowledgements[1]
    assert len(first.replay("interrupted")) == 6


def test_recovered_runs_reject_late_tool_or_run_events(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    interrupted(store)
    store.recover_interrupted_runs()
    with pytest.raises(RunStoreError):
        append(store, "interrupted", EventType.RUN_COMPLETED)
    assert store.get_run("interrupted").status == "failed"


def test_recovery_api_validation_and_acknowledgement(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    interrupted(store)
    store.recover_interrupted_runs()
    app = FastAPI()
    app.include_router(create_run_recovery_router(store))
    with TestClient(app) as client:
        assert client.get("/run-recovery?limit=501").status_code == 400
        assert len(client.get("/run-recovery").json()["recoveries"]) == 1
        url = "/run-recovery/interrupted/tool-1/acknowledge"
        assert client.post(url, json={"resolution": "retry"}).status_code == 422
        assert client.post(url, json={"resolution": "reviewed"}).status_code == 200
        assert client.get("/run-recovery").json()["recoveries"] == []
        assert len(client.get("/run-recovery?include_acknowledged=true").json()["recoveries"]) == 1
        assert client.post(url, json={"resolution": "confirmed_applied"}).status_code == 409
        assert client.post("/run-recovery/missing/tool-1/acknowledge", json={"resolution": "reviewed"}).status_code == 404


def test_exclusive_daemon_owner_prevents_a_second_startup_from_recovering_live_work(tmp_path):
    store = RunStore(lambda: tmp_path / "audit.sqlite3")
    with exclusive_run_store_owner(store):
        assert not store.path.exists()
        interrupted(store)
        with pytest.raises(RunStoreError, match="Another daemon"):
            with exclusive_run_store_owner(RunStore(lambda: store.path)):
                store.recover_interrupted_runs()
        assert store.get_run("interrupted").status == "running"
    with exclusive_run_store_owner(store):
        assert store.recover_interrupted_runs()["runs"] == 1


def test_authenticated_daemon_startup_recovers_and_secures_acknowledgements(tmp_path):
    from core.daemon.runtime import DaemonRuntime
    from core.daemon.security import DaemonSecurity
    from core.daemon.server import create_app
    from core.tools.settings_manager import SettingsManager

    (tmp_path / "models.json").write_text(json.dumps({"models": [], "active_model": ""}), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({
        "workspace_path": str(tmp_path / "workspace"), "model": "",
        "execution_policy": "always_ask", "cloud_workspace_egress": "deny"
    }), encoding="utf-8")
    runtime = DaemonRuntime(SettingsManager(root_dir=tmp_path))
    interrupted(runtime.run_store)
    app = create_app(runtime, DaemonSecurity.create("recovery-test-token"))
    headers = {"Authorization": "Bearer recovery-test-token"}
    with TestClient(app) as client:
        assert runtime.run_store.get_run("interrupted").status == "failed"
        assert client.get("/run-recovery").status_code == 401
        url = "/run-recovery/interrupted/tool-1/acknowledge"
        assert client.post(url, json={"resolution": "reviewed"}).status_code == 401
        assert runtime.run_store.list_recoveries()[0].resolution is None
        assert len(client.get("/run-recovery", headers=headers).json()["recoveries"]) == 1
        assert client.post(url, headers=headers, json={"resolution": "reviewed"}).status_code == 200
        assert client.get("/run-recovery", headers=headers).json()["recoveries"] == []


def test_daemon_lock_remains_valid_after_workspace_setting_changes(tmp_path):
    from core.daemon.runtime import DaemonRuntime
    from core.daemon.security import DaemonSecurity
    from core.daemon.server import create_app
    from core.tools.settings_manager import SettingsManager

    (tmp_path / "models.json").write_text(json.dumps({"models": [], "active_model": ""}), encoding="utf-8")
    manager = SettingsManager(root_dir=tmp_path)
    first = DaemonRuntime(manager)
    original_path = first.run_store.path
    with TestClient(create_app(first, DaemonSecurity.create("token"))):
        interrupted(first.run_store)
        manager.save_settings("Default", str(tmp_path / "different-workspace"))
        assert first.run_store.path == original_path
        assert original_path == tmp_path / "data" / "run_audit.sqlite3"
        second = DaemonRuntime(SettingsManager(root_dir=tmp_path))
        with pytest.raises(RunStoreError, match="Another daemon"):
            with TestClient(create_app(second, DaemonSecurity.create("token"))):
                pass
        assert first.run_store.get_run("interrupted").status == "running"
