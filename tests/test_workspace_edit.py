import hashlib

import pytest

from core.agent import workspace_tools
from core.agent.tooling import ToolStatus
from tests.test_workspace_tools import execute


def edit(tmp_path, raw, edits, **kwargs):
    return execute(tmp_path, "workspace.edit_text", {
        "path": "notes.txt", "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "edits": edits, **kwargs,
    }, allow_writes=True)


def test_edit_batch_preserves_crlf_and_uses_read_digest(tmp_path):
    raw = b"first\r\nsecond\r\nthird\r\n"
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    read = execute(tmp_path, "workspace.read_text", {"path": "notes.txt", "max_lines": 1})
    assert read.output["sha256"] == hashlib.sha256(raw).hexdigest()
    result = edit(tmp_path, raw, [
        {"old_text": "first", "new_text": "updated"},
        {"old_text": "third", "new_text": "last"},
    ])
    assert result.status is ToolStatus.COMPLETED
    assert path.read_bytes() == b"updated\r\nsecond\r\nlast\r\n"
    assert result.output["replacements"] == 2
    assert result.output["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_edit_refuses_stale_content_without_overwriting(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"newer user edit")
    result = edit(tmp_path, b"old content", [{"old_text": "old", "new_text": "new"}])
    assert result.status is not ToolStatus.COMPLETED
    assert path.read_bytes() == b"newer user edit"


@pytest.mark.parametrize("raw", [b"first\r\nsecond\r\nthird\r\n", b"first\nsecond\r\nthird\n"])
def test_multiline_read_can_be_used_as_exact_edit_snippet(tmp_path, raw):
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    read = execute(tmp_path, "workspace.read_text", {"path": "notes.txt", "max_lines": 2})
    snippet = read.output["text"]
    assert snippet.encode() in raw
    result = edit(tmp_path, raw, [{"old_text": snippet, "new_text": "replacement"}])
    assert result.status is ToolStatus.COMPLETED
    assert path.read_bytes() == raw.replace(snippet.encode(), b"replacement", 1)


def test_edit_batch_is_all_or_nothing_on_ambiguous_second_edit(tmp_path):
    raw = b"one two two"
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    result = edit(tmp_path, raw, [
        {"old_text": "one", "new_text": "changed"},
        {"old_text": "two", "new_text": "three"},
    ])
    assert result.status is not ToolStatus.COMPLETED
    assert path.read_bytes() == raw
    success = edit(tmp_path, raw, [{"old_text": "two", "new_text": "three", "expected_count": 2}])
    assert success.status is ToolStatus.COMPLETED
    assert path.read_bytes() == b"one three three"


@pytest.mark.parametrize("path", [".env", "settings.json", "code.py", "../outside.txt"])
def test_edit_cannot_bypass_workspace_or_script_boundaries(tmp_path, path):
    result = edit(tmp_path, b"x", [{"old_text": "x", "new_text": "y"}], path=path)
    assert result.status is ToolStatus.DENIED


def test_staged_edit_rechecks_file_after_external_change(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    raw = b"original"
    path.write_bytes(raw)
    real_fsync = workspace_tools.os.fsync

    def changed_during_stage(fd):
        real_fsync(fd)
        path.write_bytes(b"external update")

    monkeypatch.setattr(workspace_tools.os, "fsync", changed_during_stage)
    result = edit(tmp_path, raw, [{"old_text": "original", "new_text": "edited"}])
    assert result.status is not ToolStatus.COMPLETED
    assert path.read_bytes() == b"external update"
    assert not list(tmp_path.glob(".jarvs-write-*"))


def test_edit_respects_deny_policy(tmp_path):
    raw = b"original"
    path = tmp_path / "notes.txt"
    path.write_bytes(raw)
    result = execute(tmp_path, "workspace.edit_text", {
        "path": "notes.txt", "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "edits": [{"old_text": "original", "new_text": "edited"}],
    }, policy="deny_all", allow_writes=True)
    assert result.status is ToolStatus.DENIED
    assert path.read_bytes() == raw
