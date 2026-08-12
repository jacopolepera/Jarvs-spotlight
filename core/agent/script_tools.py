"""Native script lifecycle tools: validated save and managed execution."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any

from .tooling import (
    ToolBackend,
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolRisk,
    ToolValidationError,
)
from .workspace_tools import (
    MAX_TEXT_WRITE_BYTES,
    _read_bounded,
    _resolve_workspace_path,
    _write_preflight,
    _write_text,
)


MAX_PYTHON_SCRIPT_BYTES = 256 * 1024


def _python_path_preflight(
    arguments: Mapping[str, Any],
    context: ToolContext,
) -> str | None:
    try:
        _root, path = _resolve_workspace_path(context, arguments.get("path", ""))
    except ToolValidationError as exc:
        return exc.reason_code
    if path.suffix.casefold() != ".py":
        return "python_script_extension_required"
    return None


def _save_python_preflight(
    arguments: Mapping[str, Any],
    context: ToolContext,
) -> str | None:
    blocked = _python_path_preflight(arguments, context)
    if blocked is not None:
        return blocked
    code = arguments.get("code")
    if not isinstance(code, str):
        return "invalid_tool_arguments"
    if len(code.encode("utf-8")) > MAX_PYTHON_SCRIPT_BYTES:
        return "python_script_too_large"
    try:
        ast.parse(code)
    except SyntaxError:
        return "python_syntax_invalid"
    return _write_preflight(
        {
            "path": arguments.get("path"),
            "overwrite": arguments.get("overwrite", False),
            "create_parents": arguments.get("create_parents", False),
        },
        context,
        allow_typed_script=True,
    )


def _run_python_preflight(
    arguments: Mapping[str, Any],
    context: ToolContext,
) -> str | None:
    blocked = _python_path_preflight(arguments, context)
    if blocked is not None:
        return blocked
    try:
        argv = json.loads(arguments.get("arguments_json", "[]"))
    except json.JSONDecodeError:
        return "invalid_command_arguments"
    if (
        not isinstance(argv, list)
        or len(argv) > 32
        or any(not isinstance(item, str) or len(item) > 4096 for item in argv)
    ):
        return "invalid_command_arguments"
    try:
        _root, path = _resolve_workspace_path(context, arguments["path"])
        if not path.exists() or not path.is_file():
            return "python_script_not_found"
        source = _read_bounded(path, MAX_PYTHON_SCRIPT_BYTES)
        ast.parse(source.decode("utf-8"))
    except UnicodeDecodeError:
        return "python_script_not_utf8"
    except SyntaxError:
        return "python_syntax_invalid"
    except (OSError, ValueError):
        return "python_script_unreadable"
    return None


def _create_and_run_python_preflight(
    arguments: Mapping[str, Any],
    context: ToolContext,
) -> str | None:
    blocked = _save_python_preflight(arguments, context)
    if blocked is not None:
        return blocked
    try:
        argv = json.loads(arguments.get("arguments_json", "[]"))
    except json.JSONDecodeError:
        return "invalid_command_arguments"
    if (
        not isinstance(argv, list)
        or len(argv) > 32
        or any(not isinstance(item, str) or len(item) > 4096 for item in argv)
    ):
        return "invalid_command_arguments"
    return None


def _save_python(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
    result = _write_text(
        {
            "path": arguments["path"],
            "content": arguments["code"],
            "overwrite": arguments.get("overwrite", False),
            "create_parents": arguments.get("create_parents", False),
        },
        context,
    )
    return {**result, "language": "python", "syntax_valid": True}


def _backend_only(_arguments: Mapping[str, Any], _context: ToolContext) -> None:
    raise RuntimeError("This tool requires the configured managed-process backend.")


def script_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="script.save_python",
            description=(
                "Validate syntax and atomically save a Python script in the workspace; "
                "reuse an existing script when it can perform the task. Do not use this "
                "when the user also asks to run the new script immediately; use "
                "script.create_and_run_python instead"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TEXT_WRITE_BYTES,
                    },
                    "overwrite": {"type": "boolean"},
                    "create_parents": {"type": "boolean"},
                },
                "required": ["path", "code"],
                "additionalProperties": False,
            },
            handler=_save_python,
            capability="script.manage",
            effect=ToolEffect.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace",
            preflight=_save_python_preflight,
        ),
        ToolDefinition(
            name="script.run_python",
            description=(
                "Run an existing UTF-8 Python script from the workspace in a separate "
                "managed process; this always requires approval"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "arguments_json": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 131072,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_backend_only,
            capability="script.execute",
            effect=ToolEffect.CODE_EXECUTION,
            risk=ToolRisk.HIGH,
            source="jarvs_builtin",
            scope="configured_workspace",
            backend=ToolBackend.ISOLATED_PROCESS,
            preflight=_run_python_preflight,
        ),
        ToolDefinition(
            name="script.create_and_run_python",
            description=(
                "Preferred tool for multi-step requests that create and then run a new "
                "Python script. It validates the source, shows one approval containing the "
                "code, saves it atomically in the workspace, and runs it in the managed "
                "process"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TEXT_WRITE_BYTES,
                    },
                    "arguments_json": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 131072,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                    },
                    "overwrite": {"type": "boolean"},
                    "create_parents": {"type": "boolean"},
                },
                "required": ["path", "code"],
                "additionalProperties": False,
            },
            handler=_backend_only,
            capability="script.execute",
            effect=ToolEffect.CODE_EXECUTION,
            risk=ToolRisk.HIGH,
            source="jarvs_builtin",
            scope="configured_workspace",
            backend=ToolBackend.ISOLATED_PROCESS,
            preflight=_create_and_run_python_preflight,
        ),
    )


def register_script_tools(registry: ToolRegistry) -> None:
    for definition in script_tool_definitions():
        registry.register(definition)
