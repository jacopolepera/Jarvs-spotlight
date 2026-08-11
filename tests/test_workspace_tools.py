import asyncio
import os

import pytest

from core.agent.approvals import ApprovalBroker
from core.agent.events import RunEventStream
from core.agent.executor import ToolExecutor
from core.agent.tooling import (
    ToolContext,
    ToolEffect,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)
from core.agent.workspace_tools import register_workspace_tools
from core.agent import workspace_tools


def execute(
    tmp_path,
    tool_name,
    arguments,
    *,
    policy="auto_run",
    allow_writes=False,
):
    registry = ToolRegistry()
    register_workspace_tools(registry)
    registry.freeze()
    context = ToolContext(
        run_id="run",
        session_id="session",
        generation_id="generation",
        workspace_root=tmp_path,
        events=RunEventStream("run"),
    )
    executor = ToolExecutor(
        registry,
        ApprovalBroker(timeout_seconds=1),
        lambda: policy,
        allowed_in_process_effects=frozenset(
            {ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE}
            if allow_writes
            else {ToolEffect.READ_ONLY}
        ),
    )
    return asyncio.run(
        executor.execute(context, ToolInvocation.create(tool_name, arguments))
    )


def test_workspace_tools_list_read_and_search_bounded_text(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "today.txt").write_text(
        "prima riga\nJarvs trova questa frase\nultima riga",
        encoding="utf-8",
    )

    listing = execute(
        tmp_path,
        "workspace.list_files",
        {"path": "notes", "recursive": True, "limit": 20},
    )
    assert listing.status is ToolStatus.COMPLETED
    assert [item["path"] for item in listing.output["entries"]] == [
        "notes/today.txt"
    ]

    reading = execute(
        tmp_path,
        "workspace.read_text",
        {"path": "notes/today.txt", "start_line": 2, "max_lines": 1},
    )
    assert reading.status is ToolStatus.COMPLETED
    assert reading.output["text"] == "Jarvs trova questa frase"
    assert reading.output["truncated"] is True

    search = execute(
        tmp_path,
        "workspace.search_text",
        {"query": "jarvs trova", "path": "notes", "limit": 10},
    )
    assert search.status is ToolStatus.COMPLETED
    assert search.output["results"][0]["line"] == 2
    assert search.output["results"][0]["path"] == "notes/today.txt"


def test_workspace_write_is_bounded_atomic_and_requires_explicit_overwrite(tmp_path):
    created = execute(
        tmp_path,
        "workspace.write_text",
        {
            "path": "notes/today.txt",
            "content": "prima versione",
            "create_parents": True,
        },
        allow_writes=True,
    )
    assert created.status is ToolStatus.COMPLETED
    assert created.output["operation"] == "created"
    assert (tmp_path / "notes" / "today.txt").read_text(encoding="utf-8") == "prima versione"
    assert list((tmp_path / "notes").glob(".jarvs-write-*.tmp")) == []

    denied = execute(
        tmp_path,
        "workspace.write_text",
        {"path": "notes/today.txt", "content": "seconda versione"},
        allow_writes=True,
    )
    assert denied.status is ToolStatus.DENIED
    assert denied.reason_code == "workspace_target_exists"

    replaced = execute(
        tmp_path,
        "workspace.write_text",
        {
            "path": "notes/today.txt",
            "content": "seconda versione",
            "overwrite": True,
        },
        allow_writes=True,
    )
    assert replaced.status is ToolStatus.COMPLETED
    assert replaced.output["operation"] == "replaced"
    assert (tmp_path / "notes" / "today.txt").read_text(encoding="utf-8") == "seconda versione"


def test_workspace_write_scope_and_sensitive_paths_remain_hard_denied(tmp_path):
    outside_name = f"outside-{tmp_path.name}.txt"
    traversal = execute(
        tmp_path,
        "workspace.write_text",
        {"path": f"../{outside_name}", "content": "no"},
        allow_writes=True,
    )
    secret = execute(
        tmp_path,
        "workspace.write_text",
        {"path": ".env", "content": "no"},
        allow_writes=True,
    )
    assert traversal.status is ToolStatus.DENIED
    assert traversal.reason_code == "workspace_scope_escape"
    assert secret.status is ToolStatus.DENIED
    assert secret.reason_code == "sensitive_workspace_path"
    assert not (tmp_path.parent / outside_name).exists()


@pytest.mark.parametrize("path", ["demo.py", "demo.pyw", "demo.ps1", "demo.bat", "demo.cmd"])
def test_workspace_writer_requires_typed_tool_for_executable_sources(tmp_path, path):
    result = execute(
        tmp_path,
        "workspace.write_text",
        {"path": path, "content": "print('no')"},
        allow_writes=True,
    )

    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "typed_script_tool_required"
    assert not (tmp_path / path).exists()


def test_workspace_make_directory_is_idempotent(tmp_path):
    first = execute(
        tmp_path,
        "workspace.make_directory",
        {"path": "a/b", "parents": True},
        allow_writes=True,
    )
    second = execute(
        tmp_path,
        "workspace.make_directory",
        {"path": "a/b"},
        allow_writes=True,
    )
    assert first.output["operation"] == "created"
    assert second.output["operation"] == "already_exists"


UNSAFE_PATHS = ["../outside.txt"]
if os.name == "nt":
    UNSAFE_PATHS.extend(["C:\\Windows\\win.ini", "notes.txt:private"])


@pytest.mark.parametrize("unsafe_path", UNSAFE_PATHS)
def test_workspace_scope_escape_is_denied_even_in_auto_run(tmp_path, unsafe_path):
    result = execute(
        tmp_path,
        "workspace.read_text",
        {"path": unsafe_path},
    )
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "workspace_scope_escape"


def test_workspace_symlink_escape_is_denied_when_supported(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable for this account")

    result = execute(tmp_path, "workspace.read_text", {"path": "link.txt"})
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "workspace_scope_escape"


def test_workspace_binary_reads_fail_without_output(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"text\x00binary")
    result = execute(tmp_path, "workspace.read_text", {"path": "binary.dat"})
    assert result.status is ToolStatus.FAILED
    assert result.output is None


def test_workspace_oversized_reads_are_bounded(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    result = execute(tmp_path, "workspace.read_text", {"path": "large.txt"})
    assert result.status is ToolStatus.FAILED
    assert result.output is None


@pytest.mark.parametrize(
    "sensitive_path",
    [
        ".env",
        ".env.local",
        "production.env",
        "credentials",
        "secrets",
        "settings.json",
        "user_models.json",
        "private.key",
    ],
)
def test_sensitive_workspace_files_are_hard_denied(tmp_path, sensitive_path):
    (tmp_path / sensitive_path).write_text("secret", encoding="utf-8")
    result = execute(tmp_path, "workspace.read_text", {"path": sensitive_path})
    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "sensitive_workspace_path"


@pytest.mark.parametrize("directory", [".aws", ".ssh", "credentials", "secrets"])
def test_sensitive_workspace_root_is_hard_denied(tmp_path, directory):
    sensitive_root = tmp_path / directory
    sensitive_root.mkdir()
    (sensitive_root / "ordinary-name.txt").write_text("secret", encoding="utf-8")

    result = execute(
        sensitive_root,
        "workspace.read_text",
        {"path": "ordinary-name.txt"},
    )

    assert result.status is ToolStatus.DENIED
    assert result.reason_code == "sensitive_workspace_path"


def test_workspace_search_skips_sensitive_and_case_insensitive_heavy_directories(tmp_path):
    (tmp_path / ".env").write_text("unique-secret", encoding="utf-8")
    (tmp_path / "NODE_MODULES").mkdir()
    (tmp_path / "NODE_MODULES" / "token.txt").write_text(
        "unique-secret", encoding="utf-8"
    )
    (tmp_path / "visible.txt").write_text("unique-secret", encoding="utf-8")

    result = execute(
        tmp_path,
        "workspace.search_text",
        {"query": "unique-secret", "limit": 10},
    )
    assert result.status is ToolStatus.COMPLETED
    assert [match["path"] for match in result.output["results"]] == ["visible.txt"]


def test_workspace_traversal_has_an_entry_budget(tmp_path, monkeypatch):
    for index in range(4):
        (tmp_path / f"file-{index}.txt").write_text("value", encoding="utf-8")
    monkeypatch.setattr(workspace_tools, "MAX_WALK_ENTRIES", 2)

    result = execute(
        tmp_path,
        "workspace.search_text",
        {"query": "value", "limit": 10},
    )
    assert result.status is ToolStatus.FAILED
