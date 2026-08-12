import asyncio
import json

import pytest

from core.agent.approvals import ApprovalBroker
from core.agent.events import RunEventStream
from core.agent.executor import ToolExecutor
from core.agent.routine_tools import register_routine_tools
from core.agent.routines import RoutineRunner, RoutineStore, RoutineValidationError, RoutineValidator
from core.agent.tooling import (
    ToolContext,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)
from core.agent.workspace_tools import register_workspace_tools


def setup(tmp_path):
    registry = ToolRegistry()
    register_workspace_tools(registry)
    registry.freeze()
    routine_store = RoutineStore(
        lambda: tmp_path / ".jarvs" / "routines.sqlite3",
        RoutineValidator(registry),
    )
    executor = ToolExecutor(
        registry,
        ApprovalBroker(timeout_seconds=1),
        lambda: "auto_run",
    )
    return registry, routine_store, executor


def manifest():
    return {
        "schema_version": 1,
        "name": "Read note",
        "description": "Read a text note from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "tool_name": "workspace.read_text",
                "arguments": {"path": {"$input": "path"}, "max_lines": 10},
            }
        ],
    }


def test_routine_manifest_is_validated_then_saved_with_versions(tmp_path):
    _registry, store, _executor = setup(tmp_path)
    assert not store.path.exists()

    first = store.save(manifest(), routine_id="read-note")
    revised = manifest()
    revised["description"] = "Read a note deterministically."
    second = store.save(revised, routine_id="read-note")

    assert first.version == 1
    assert second.version == 2
    assert first.manifest_hash != second.manifest_hash
    assert store.get("read-note").version == 2
    assert len(store.export()["routines"]) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"script": "print('no')"}),
        lambda value: value["steps"][0].update({"tool_name": "unknown.execute"}),
        lambda value: value["steps"][0].update({"arguments": {"path": {"$input": "missing"}}}),
    ],
)
def test_invalid_or_executable_routines_are_rejected_before_save(tmp_path, mutation):
    _registry, store, _executor = setup(tmp_path)
    value = manifest()
    mutation(value)

    with pytest.raises(RoutineValidationError):
        store.save(value)
    assert not store.path.exists() or store.list() == ()


def test_saved_routine_reuses_tools_without_a_model_generation(tmp_path):
    registry, store, executor = setup(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("content", encoding="utf-8")
    routine = store.save(manifest(), routine_id="read-note")
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        events=RunEventStream("run"),
        workspace_root=workspace,
    )

    result = asyncio.run(
        RoutineRunner(store, registry, executor).execute(
            routine.routine_id,
            {"path": "note.txt"},
            context,
        )
    )

    assert result.status is ToolStatus.COMPLETED
    assert result.steps[0].output["text"] == "content"


def test_model_facing_routine_tools_save_list_and_replay_registered_steps(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    register_workspace_tools(registry)
    holders = {}
    register_routine_tools(
        registry,
        lambda: holders["store"],
        lambda: registry,
        lambda: holders["executor"],
    )
    registry.freeze()
    holders["store"] = RoutineStore(
        lambda: workspace / ".jarvs" / "routines.sqlite3",
        RoutineValidator(registry),
    )
    holders["executor"] = ToolExecutor(
        registry,
        ApprovalBroker(timeout_seconds=1),
        lambda: "auto_run",
        allowed_in_process_effects=frozenset(
            {
                ToolEffect.READ_ONLY,
                ToolEffect.WORKSPACE_WRITE,
                ToolEffect.ORCHESTRATION,
            }
        ),
    )
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        events=RunEventStream("run"),
        workspace_root=workspace,
    )
    write_manifest = {
        "schema_version": 1,
        "name": "Write note",
        "description": "Write a reusable text note.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "steps": [
            {
                "tool_name": "workspace.write_text",
                "arguments": {
                    "path": "routine-note.txt",
                    "content": {"$input": "text"},
                    "overwrite": True,
                },
            }
        ],
    }

    saved = asyncio.run(
        holders["executor"].execute(
            context,
            ToolInvocation.create(
                "routine.save",
                {"manifest_json": json.dumps(write_manifest)},
            ),
        )
    )
    listed = asyncio.run(
        holders["executor"].execute(
            context,
            ToolInvocation.create("routine.list", {}),
        )
    )
    replayed = asyncio.run(
        holders["executor"].execute(
            context,
            ToolInvocation.create(
                "routine.run",
                {
                    "routine_id": saved.output["routine_id"],
                    "inputs_json": json.dumps({"text": "reused"}),
                },
            ),
        )
    )

    assert saved.status is ToolStatus.COMPLETED
    assert listed.output["routines"][0]["name"] == "Write note"
    assert replayed.status is ToolStatus.COMPLETED
    assert replayed.output["status"] == "completed"
    assert (workspace / "routine-note.txt").read_text(encoding="utf-8") == "reused"
