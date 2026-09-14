"""Bounded native tools scoped to the configured Jarvs workspace."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .tooling import (
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolRisk,
    ToolValidationError,
)


MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_WRITE_BYTES = 512 * 1024
MAX_SEARCH_BYTES = 16 * 1024 * 1024
MAX_SEARCH_FILES = 500
MAX_WALK_ENTRIES = 5_000
MAX_WALK_DIRECTORIES = 1_000
MAX_WALK_DEPTH = 64
_MUTATION_LOCK = threading.RLock()
SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
}
SENSITIVE_DIRECTORIES = {
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    ".jarvs",
    "credentials",
    "secrets",
}
SENSITIVE_FILENAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "settings.json",
    "user_models.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
SAFE_ENV_SUFFIXES = {".example", ".sample", ".template"}
TYPED_SCRIPT_SUFFIXES = {".bat", ".cmd", ".ps1", ".py", ".pyw"}


class WorkspaceScopeError(ToolValidationError):
    def __init__(self, message: str):
        super().__init__(message, reason_code="workspace_scope_escape")


class SensitiveWorkspacePathError(ToolValidationError):
    def __init__(self, message: str):
        super().__init__(message, reason_code="sensitive_workspace_path")


class WorkspaceTraversalLimitError(RuntimeError):
    pass


def _workspace_root(context: ToolContext) -> Path:
    if context.workspace_root is None:
        raise ToolValidationError(
            "workspace root is not configured",
            reason_code="workspace_unavailable",
        )
    root = context.workspace_root.resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise ToolValidationError(
            "workspace root is not available",
            reason_code="workspace_unavailable",
        )
    if any(part.casefold() in SENSITIVE_DIRECTORIES for part in root.parts):
        raise SensitiveWorkspacePathError(
            "sensitive workspace roots are not available to model tools"
        )
    return root


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        stat_result = path.lstat()
        is_reparse = bool(getattr(stat_result, "st_file_attributes", 0) & 0x400)
        return (
            path.is_symlink()
            or (callable(is_junction) and is_junction())
            or is_reparse
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _is_sensitive_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = [part.casefold() for part in relative.parts]
    if any(part in SENSITIVE_DIRECTORIES for part in parts):
        return True
    if not parts:
        return False
    name = parts[-1]
    if name in SENSITIVE_FILENAMES or Path(name).suffix.casefold() in SENSITIVE_SUFFIXES:
        return True
    if name == ".env" or name.endswith(".env"):
        return True
    if name.startswith(".env."):
        return not any(name.endswith(suffix) for suffix in SAFE_ENV_SUFFIXES)
    return False


def _resolve_workspace_path(context: ToolContext, raw_path: object) -> tuple[Path, Path]:
    root = _workspace_root(context)
    if not isinstance(raw_path, str):
        raise WorkspaceScopeError("workspace path must be a relative string")
    if "\x00" in raw_path:
        raise WorkspaceScopeError("workspace path contains a null byte")
    relative = Path(raw_path or ".")
    if relative.is_absolute():
        raise WorkspaceScopeError("absolute workspace paths are not allowed")
    if ".." in relative.parts:
        raise WorkspaceScopeError("parent traversal is not allowed")
    if os.name == "nt" and any(":" in part for part in relative.parts):
        raise WorkspaceScopeError("Windows alternate data streams are not allowed")

    unresolved = root / relative
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if _is_link_or_junction(current):
            raise WorkspaceScopeError("links and junctions are not allowed in tool paths")

    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceScopeError("path escapes the configured workspace") from exc
    if _is_sensitive_path(root, candidate):
        raise SensitiveWorkspacePathError(
            "sensitive workspace paths are not available to model tools"
        )
    return root, candidate


def _read_bounded(path: Path, limit: int) -> bytes:
    if _is_link_or_junction(path):
        raise WorkspaceScopeError("links and junctions are not readable")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("workspace file exceeds the bounded read limit")
    return data


def _path_preflight(arguments: Mapping[str, Any], context: ToolContext) -> str | None:
    try:
        _resolve_workspace_path(context, arguments.get("path", ""))
    except ToolValidationError as exc:
        return exc.reason_code
    return None


def _write_preflight(
    arguments: Mapping[str, Any],
    context: ToolContext,
    *,
    allow_typed_script: bool = False,
) -> str | None:
    try:
        root, path = _resolve_workspace_path(context, arguments.get("path", ""))
    except ToolValidationError as exc:
        return exc.reason_code
    if path == root:
        return "workspace_root_write_denied"
    if not allow_typed_script and path.suffix.casefold() in TYPED_SCRIPT_SUFFIXES:
        return "typed_script_tool_required"
    if path.exists() and path.is_dir():
        return "workspace_target_is_directory"
    if path.exists() and not arguments.get("overwrite", False):
        return "workspace_target_exists"
    parent = path.parent
    if not parent.exists() and not arguments.get("create_parents", False):
        return "workspace_parent_missing"
    return None


def _directory_preflight(arguments: Mapping[str, Any], context: ToolContext) -> str | None:
    try:
        root, path = _resolve_workspace_path(context, arguments.get("path", ""))
    except ToolValidationError as exc:
        return exc.reason_code
    if path == root:
        return "workspace_root_write_denied"
    if path.exists() and not path.is_dir():
        return "workspace_target_is_file"
    if not path.parent.exists() and not arguments.get("parents", False):
        return "workspace_parent_missing"
    return None


def _relative(root: Path, path: Path) -> str:
    value = path.relative_to(root).as_posix()
    return value if value != "." else ""


def _write_text(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    with _MUTATION_LOCK:
        return _write_text_locked(arguments, context)


def _write_text_locked(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    root, path = _resolve_workspace_path(context, arguments["path"])
    content = arguments["content"]
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_WRITE_BYTES:
        raise ValueError("workspace text write exceeds the bounded size limit")
    existed = path.exists()
    if existed and not arguments.get("overwrite", False):
        raise FileExistsError("workspace target already exists")
    if arguments.get("create_parents", False):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".jarvs-write-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Revalidate after staging, including optimistic edits against the
        # latest on-disk content. Never publish a cancelled staged write.
        _resolve_workspace_path(context, arguments["path"])
        if context.cancelled.is_set():
            raise RuntimeError("workspace write cancelled")
        expected = arguments.get("expected_sha256")
        if expected is not None and hashlib.sha256(
            _read_bounded(path, MAX_TEXT_FILE_BYTES)
        ).hexdigest() != expected:
            raise ToolValidationError(
                "The file changed; read it again before editing.",
                reason_code="workspace_edit_conflict",
            )
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
    return {
        "path": _relative(root, path),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "operation": "replaced" if existed else "created",
    }


def _edit_preflight(arguments: Mapping[str, Any], context: ToolContext) -> str | None:
    return _write_preflight({**arguments, "overwrite": True}, context)


def _edit_text(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    """Apply one bounded edit batch or leave the file entirely unchanged."""
    with _MUTATION_LOCK:
        _root, path = _resolve_workspace_path(context, arguments["path"])
        raw = _read_bounded(path, MAX_TEXT_FILE_BYTES)
        expected = arguments["expected_sha256"]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ToolValidationError(
                "The file changed; read it again before editing.",
                reason_code="workspace_edit_conflict",
            )
        if b"\x00" in raw:
            raise ValueError("workspace file appears to be binary")
        content = raw.decode("utf-8")
        replacements = 0
        for edit in arguments["edits"]:
            old = edit["old_text"]
            expected_count = edit.get("expected_count", 1)
            count = content.count(old)
            if count != expected_count:
                raise ToolValidationError(
                    f"Expected {expected_count} exact matches, found {count}; no edits applied.",
                    reason_code="workspace_edit_match_conflict",
                )
            content = content.replace(old, edit["new_text"])
            if len(content.encode("utf-8")) > MAX_TEXT_WRITE_BYTES:
                raise ValueError("workspace edited text exceeds the bounded size limit")
            replacements += count
        result = _write_text_locked(
            {"path": arguments["path"], "content": content,
             "overwrite": True, "expected_sha256": expected},
            context,
        )
        return {**result, "operation": "edited", "replacements": replacements,
                "previous_sha256": expected}


def _make_directory(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    root, path = _resolve_workspace_path(context, arguments["path"])
    existed = path.exists()
    path.mkdir(parents=bool(arguments.get("parents", False)), exist_ok=True)
    return {
        "path": _relative(root, path),
        "operation": "already_exists" if existed else "created",
    }


def _safe_entries(
    context: ToolContext,
    root: Path,
    start: Path,
    *,
    recursive: bool,
) -> Iterable[Path]:
    if start.is_file():
        if not _is_link_or_junction(start) and not _is_sensitive_path(root, start):
            yield start
        return
    if _is_link_or_junction(start):
        return
    if not start.exists() or not start.is_dir():
        raise FileNotFoundError("workspace path does not exist")

    stack: list[tuple[Path, int]] = [(start, 0)]
    visited_entries = 0
    visited_directories = 0
    while stack:
        directory, depth = stack.pop()
        visited_directories += 1
        if visited_directories > MAX_WALK_DIRECTORIES or depth > MAX_WALK_DEPTH:
            raise WorkspaceTraversalLimitError("workspace traversal limit reached")
        if context.cancelled.is_set():
            raise RuntimeError("workspace traversal cancelled")

        batch: list[tuple[Path, bool]] = []
        try:
            scanner = os.scandir(directory)
        except OSError:
            continue
        with scanner:
            for entry in scanner:
                visited_entries += 1
                if visited_entries > MAX_WALK_ENTRIES:
                    raise WorkspaceTraversalLimitError(
                        "workspace entry limit reached"
                    )
                if context.cancelled.is_set():
                    raise RuntimeError("workspace traversal cancelled")
                path = Path(entry.path)
                folded_name = entry.name.casefold()
                if _is_link_or_junction(path) or _is_sensitive_path(root, path):
                    continue
                if folded_name in SKIPPED_DIRECTORIES and entry.is_dir(
                    follow_symlinks=False
                ):
                    continue
                try:
                    path.resolve(strict=False).relative_to(root)
                    is_directory = entry.is_dir(follow_symlinks=False)
                except (OSError, ValueError):
                    continue
                batch.append((path, is_directory))

        batch.sort(key=lambda item: item[0].name.casefold())
        child_directories = []
        for path, is_directory in batch:
            yield path
            if recursive and is_directory:
                child_directories.append(path)
        if not recursive:
            return
        for child in reversed(child_directories):
            try:
                child.resolve(strict=False).relative_to(root)
            except (OSError, ValueError):
                continue
            stack.append((child, depth + 1))


def _list_files(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    root, start = _resolve_workspace_path(context, arguments.get("path", ""))
    recursive = arguments.get("recursive", False)
    limit = arguments.get("limit", 100)
    entries = []
    truncated = False
    for path in _safe_entries(context, root, start, recursive=recursive):
        if context.cancelled.is_set():
            raise RuntimeError("workspace listing cancelled")
        if len(entries) >= limit:
            truncated = True
            break
        try:
            kind = "directory" if path.is_dir() else "file"
            size = path.stat().st_size if kind == "file" else None
        except OSError:
            continue
        entries.append(
            {
                "path": _relative(root, path),
                "kind": kind,
                "size_bytes": size,
            }
        )
    return {
        "path": _relative(root, start),
        "entries": entries,
        "truncated": truncated,
    }


def _read_text(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    root, path = _resolve_workspace_path(context, arguments["path"])
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("workspace file does not exist")
    raw = _read_bounded(path, MAX_TEXT_FILE_BYTES)
    if b"\x00" in raw:
        raise ValueError("workspace file appears to be binary")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("workspace file is not UTF-8 text") from exc
    # Preserve line separators between selected lines: edit_text consumes exact
    # snippets from this result, including Windows CRLF files.
    lines = text.splitlines(keepends=True)
    start_line = arguments.get("start_line", 1)
    max_lines = arguments.get("max_lines", 200)
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    excerpt = "".join(selected)
    if excerpt.endswith("\r\n"):
        excerpt = excerpt[:-2]
    elif excerpt.endswith(("\r", "\n")):
        excerpt = excerpt[:-1]
    return {
        "path": _relative(root, path),
        "start_line": start_line,
        "line_count": len(selected),
        "total_lines": len(lines),
        "truncated": start_line - 1 + len(selected) < len(lines),
        "text": excerpt,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _search_text(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    root, start = _resolve_workspace_path(context, arguments.get("path", ""))
    query = arguments["query"]
    case_sensitive = arguments.get("case_sensitive", False)
    limit = arguments.get("limit", 50)
    needle = query if case_sensitive else query.casefold()
    results = []
    scanned_files = 0
    scanned_bytes = 0
    truncated = False

    for path in _safe_entries(context, root, start, recursive=True):
        if context.cancelled.is_set():
            raise RuntimeError("workspace search cancelled")
        if not path.is_file():
            continue
        if scanned_files >= MAX_SEARCH_FILES or scanned_bytes >= MAX_SEARCH_BYTES:
            truncated = True
            break
        scanned_files += 1
        try:
            size = path.stat().st_size
            remaining = MAX_SEARCH_BYTES - scanned_bytes
            if size > MAX_TEXT_FILE_BYTES or size > remaining:
                continue
            raw = _read_bounded(path, min(MAX_TEXT_FILE_BYTES, remaining))
        except (OSError, ValueError, WorkspaceScopeError):
            continue
        scanned_bytes += len(raw)
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.casefold()
            if needle not in haystack:
                continue
            results.append(
                {
                    "path": _relative(root, path),
                    "line": line_number,
                    "text": line[:500],
                }
            )
            if len(results) >= limit:
                truncated = True
                break
        if len(results) >= limit:
            break

    return {
        "query": query,
        "path": _relative(root, start),
        "results": results,
        "scanned_files": scanned_files,
        "truncated": truncated,
    }


def workspace_tool_definitions() -> tuple[ToolDefinition, ...]:
    read_common = {
        "capability": "workspace.read",
        "effect": ToolEffect.READ_ONLY,
        "risk": ToolRisk.LOW,
        "source": "jarvs_builtin",
        "scope": "configured_workspace",
        "preflight": _path_preflight,
    }
    return (
        ToolDefinition(
            name="workspace.list_files",
            description=(
                "List files and directories using paths relative to the workspace root; "
                "for the root, omit path or use an empty string without a workspace prefix"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 4096},
                    "recursive": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
            handler=_list_files,
            **read_common,
        ),
        ToolDefinition(
            name="workspace.read_text",
            description=(
                "Read a bounded portion of a UTF-8 file; path is relative to the workspace "
                "root and must not start with workspace/"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_read_text,
            **read_common,
        ),
        ToolDefinition(
            name="workspace.search_text",
            description=(
                "Search text in UTF-8 files with explicit limits; path is relative to the "
                "configured root and must be omitted when searching the root"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "path": {"type": "string", "maxLength": 4096},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_search_text,
            **read_common,
        ),
        ToolDefinition(
            name="workspace.write_text",
            description=(
                "Create or atomically replace a UTF-8 text file in the workspace. This "
                "tool does not accept executable source (.py, .pyw, .ps1, .bat, .cmd); "
                "always use script.save_python for Python"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "content": {
                        "type": "string",
                        "maxLength": MAX_TEXT_WRITE_BYTES,
                    },
                    "overwrite": {"type": "boolean"},
                    "create_parents": {"type": "boolean"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=_write_text,
            capability="workspace.write",
            effect=ToolEffect.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace",
            preflight=_write_preflight,
        ),
        ToolDefinition(
            name="workspace.edit_text",
            description=(
                "Apply precise text replacements atomically to an existing UTF-8 file. "
                "First read_text to obtain the full-file sha256; pass it as expected_sha256. "
                "Edits apply in order, each requiring exactly expected_count matches (default 1). "
                "Any mismatch leaves the entire file unchanged. Executable scripts are excluded."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                    "edits": {
                        "type": "array", "minItems": 1, "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string", "minLength": 1, "maxLength": 65536},
                                "new_text": {"type": "string", "maxLength": 65536},
                                "expected_count": {"type": "integer", "minimum": 1, "maximum": 1000},
                            },
                            "required": ["old_text", "new_text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "expected_sha256", "edits"],
                "additionalProperties": False,
            },
            handler=_edit_text,
            capability="workspace.write",
            effect=ToolEffect.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace",
            preflight=_edit_preflight,
        ),
        ToolDefinition(
            name="workspace.make_directory",
            description="Create a directory in the configured workspace",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "parents": {"type": "boolean"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_make_directory,
            capability="workspace.write",
            effect=ToolEffect.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace",
            preflight=_directory_preflight,
        ),
    )


def register_workspace_tools(registry: ToolRegistry) -> None:
    for definition in workspace_tool_definitions():
        registry.register(definition)
