"""Explicit managed terminal tools for the native Jarvs action path."""

from __future__ import annotations

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
)


def _backend_only(_arguments: Mapping[str, Any], _context: ToolContext) -> None:
    raise RuntimeError("This tool requires the configured managed-process backend.")


def _command_preflight(
    arguments: Mapping[str, Any],
    _context: ToolContext,
) -> str | None:
    try:
        argv = json.loads(arguments.get("arguments_json", ""))
    except json.JSONDecodeError:
        return "invalid_command_arguments"
    if (
        not isinstance(argv, list)
        or len(argv) > 32
        or any(not isinstance(item, str) or len(item) > 4096 for item in argv)
    ):
        return "invalid_command_arguments"
    return None


def system_tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="system.run_command",
            description=(
                "Run an explicit Python or PowerShell command without an implicit shell, "
                "using the workspace as cwd, a reduced environment, a timeout, and bounded "
                "output. Prefer deterministic tools or existing scripts/routines."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "program": {
                        "type": "string",
                        "enum": ["python", "powershell"],
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
                },
                "required": ["program", "arguments_json"],
                "additionalProperties": False,
            },
            handler=_backend_only,
            capability="system.execute",
            effect=ToolEffect.CODE_EXECUTION,
            risk=ToolRisk.HIGH,
            source="jarvs_builtin",
            scope="configured_workspace_process",
            backend=ToolBackend.ISOLATED_PROCESS,
            preflight=_command_preflight,
        ),
    )


def register_system_tools(registry: ToolRegistry) -> None:
    for definition in system_tool_definitions():
        registry.register(definition)
