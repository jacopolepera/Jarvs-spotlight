import json
import os
import sqlite3
import threading

import pytest

from core.tools.settings_manager import SettingsManager


def write_models(root, secret="top-secret"):
    (root / "models.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "models": [
                    {
                        "id": "cloud",
                        "name": "Cloud",
                        "provider": "cloud",
                        "model_string": "provider/model",
                        "api_key": secret,
                        "api_base": None,
                    }
                ],
                "active_model": "cloud",
            }
        ),
        encoding="utf-8",
    )


def test_missing_or_invalid_execution_policy_fails_closed(tmp_path):
    write_models(tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"theme": "Dark", "execution_policy": "anything-goes"}),
        encoding="utf-8",
    )
    manager = SettingsManager(root_dir=tmp_path)
    assert manager.get_execution_policy() == "always_ask"
    assert manager.get_theme() == "Dark"


@pytest.mark.parametrize("policy", ["always_ask", "auto_run", "deny_all"])
def test_execution_policy_round_trip(tmp_path, policy):
    write_models(tmp_path)
    manager = SettingsManager(root_dir=tmp_path)
    assert manager.set_execution_policy(policy) == policy
    assert SettingsManager(root_dir=tmp_path).get_execution_policy() == policy


def test_invalid_execution_policy_does_not_mutate_settings(tmp_path):
    write_models(tmp_path)
    manager = SettingsManager(root_dir=tmp_path)
    before = manager.get_execution_policy()
    with pytest.raises(ValueError):
        manager.set_execution_policy("unsafe")
    assert manager.get_execution_policy() == before


def test_public_models_redact_secret_without_mutating_registry(tmp_path):
    write_models(tmp_path)
    manager = SettingsManager(root_dir=tmp_path)
    public = manager.get_public_models_list()
    assert "api_key" not in public[0]
    assert public[0]["api_key_configured"] is True
    assert manager.get_models_list()[0]["api_key"] == "top-secret"


def test_policy_write_failure_is_reported_and_does_not_mutate_memory(
    tmp_path,
    monkeypatch,
):
    write_models(tmp_path)
    manager = SettingsManager(root_dir=tmp_path)
    settings_file = tmp_path / "settings.json"
    before_disk = settings_file.read_text(encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated persistence failure"):
        manager.set_execution_policy("auto_run")

    assert manager.get_execution_policy() == "always_ask"
    assert settings_file.read_text(encoding="utf-8") == before_disk
    assert not list(tmp_path.glob(".settings.json.*.tmp"))


def test_model_and_policy_writers_share_one_transaction_lock(tmp_path, monkeypatch):
    write_models(tmp_path)
    manager = SettingsManager(root_dir=tmp_path)
    manager.add_model(
        {
            "id": "local",
            "name": "Local",
            "provider": "local",
            "model_string": "openai/local",
            "api_key": "lm-studio",
            "api_base": "http://localhost:1234/v1",
        }
    )

    model_write_entered = threading.Event()
    release_model_write = threading.Event()
    policy_finished = threading.Event()
    original_save_raw = manager._save_raw

    def pause_model_settings_write(data):
        if threading.current_thread().name == "model-writer":
            model_write_entered.set()
            assert release_model_write.wait(timeout=2)
        return original_save_raw(data)

    monkeypatch.setattr(manager, "_save_raw", pause_model_settings_write)
    model_writer = threading.Thread(
        target=lambda: manager.set_active_model("local"),
        name="model-writer",
        daemon=True,
    )
    policy_writer = threading.Thread(
        target=lambda: (
            manager.set_execution_policy("auto_run"),
            policy_finished.set(),
        ),
        name="policy-writer",
        daemon=True,
    )

    model_writer.start()
    assert model_write_entered.wait(timeout=1)
    policy_writer.start()
    assert not policy_finished.wait(timeout=0.05)
    release_model_write.set()
    model_writer.join(timeout=1)
    policy_writer.join(timeout=1)

    assert not model_writer.is_alive()
    assert not policy_writer.is_alive()
    assert manager.get_active_model_id() == "local"
    assert manager.get_execution_policy() == "auto_run"
    persisted_settings = json.loads(
        (tmp_path / "settings.json").read_text(encoding="utf-8")
    )
    persisted_models = json.loads(
        (tmp_path / "user_models.json").read_text(encoding="utf-8")
    )
    assert persisted_settings["execution_policy"] == "auto_run"
    assert persisted_models["active_model"] == "local"


def test_workspace_databases_are_migrated_to_application_state(tmp_path):
    write_models(tmp_path)
    workspace = tmp_path / "workspace"
    old_state = workspace / ".jarvs"
    old_state.mkdir(parents=True)
    (tmp_path / "settings.json").write_text(
        json.dumps({"workspace_path": str(workspace)}),
        encoding="utf-8",
    )
    source = old_state / "personal_memory.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('preserved')")

    manager = SettingsManager(root_dir=tmp_path)
    destination = tmp_path / "data" / "personal_memory.sqlite3"

    assert source.exists()
    assert destination.exists()
    assert (tmp_path / "data" / ".workspace-state-migrated").is_file()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "preserved",
        )
    assert manager.get_state_dir() == str(tmp_path / "data")
