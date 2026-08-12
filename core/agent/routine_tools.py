"""Model-facing tools for inspecting, saving, and replaying Jarvs routines."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .executor import ToolExecutor
from .routines import RoutineRunner, RoutineStore
from .tooling import (
    ToolContext,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolRisk,
)


StoreProvider = Callable[[], RoutineStore]
RegistryProvider = Callable[[], ToolRegistry]
ExecutorProvider = Callable[[], ToolExecutor]


def routine_tool_definitions(
    store_provider: StoreProvider,
    registry_provider: RegistryProvider,
    executor_provider: ExecutorProvider,
) -> tuple[ToolDefinition, ...]:
    def list_routines(
        arguments: Mapping[str, Any],
        _context: ToolContext,
    ) -> dict[str, Any]:
        items = store_provider().list(limit=int(arguments.get("limit", 20)))
        return {
            "routines": [
                {
                    "routine_id": item.routine_id,
                    "version": item.version,
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.manifest["input_schema"],
                    "manifest_hash": item.manifest_hash,
                }
                for item in items
            ]
        }

    def get_routine(
        arguments: Mapping[str, Any],
        _context: ToolContext,
    ) -> dict[str, Any]:
        item = store_provider().get(
            arguments["routine_id"],
            version=arguments.get("version"),
        )
        return item.as_dict()

    def save_routine(
        arguments: Mapping[str, Any],
        _context: ToolContext,
    ) -> dict[str, Any]:
        manifest = json.loads(arguments["manifest_json"])
        if not isinstance(manifest, dict):
            raise ValueError("routine manifest JSON must describe an object")
        item = store_provider().save(
            manifest,
            routine_id=arguments.get("routine_id"),
        )
        return item.as_dict()

    async def run_routine(
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        inputs = json.loads(arguments.get("inputs_json", "{}"))
        if not isinstance(inputs, dict):
            raise ValueError("routine inputs JSON must describe an object")
        runner = RoutineRunner(
            store_provider(),
            registry_provider(),
            executor_provider(),
        )
        result = await runner.execute(
            arguments["routine_id"],
            inputs,
            context,
            version=arguments.get("version"),
        )
        return {
            "routine_id": result.routine_id,
            "version": result.version,
            "status": result.status.value,
            "steps": [
                {
                    "tool_name": step.tool_name,
                    "status": step.status.value,
                    "output": step.output,
                    "reason_code": step.reason_code,
                }
                for step in result.steps
            ],
        }

    common_read = {
        "capability": "routine.read",
        "effect": ToolEffect.READ_ONLY,
        "risk": ToolRisk.LOW,
        "source": "jarvs_builtin",
        "scope": "configured_workspace_routine_store",
    }
    return (
        ToolDefinition(
            name="routine.list",
            description=(
                "List saved routines with their descriptions and input schemas; use this "
                "tool before creating a new script or routine"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}
                },
                "additionalProperties": False,
            },
            handler=list_routines,
            **common_read,
        ),
        ToolDefinition(
            name="routine.get",
            description="Retrieve the manifest and metadata for a saved routine",
            input_schema={
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "version": {"type": "integer", "minimum": 1},
                },
                "required": ["routine_id"],
                "additionalProperties": False,
            },
            handler=get_routine,
            **common_read,
        ),
        ToolDefinition(
            name="routine.save",
            description=(
                "Save or update a deterministic routine composed only of registered tools; "
                "pass the manifest as a JSON string for Jarvs to decode, validate, and "
                "version"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "manifest_json": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 131072,
                    },
                },
                "required": ["manifest_json"],
                "additionalProperties": False,
            },
            handler=save_routine,
            capability="routine.manage",
            effect=ToolEffect.WORKSPACE_WRITE,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace_routine_store",
        ),
        ToolDefinition(
            name="routine.run",
            description=(
                "Run a saved routine without regenerating code; every step continues to "
                "pass through the registry, policy, approval, and executor"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "version": {"type": "integer", "minimum": 1},
                    "inputs_json": {
                        "type": "string",
                        "minLength": 2,
                        "maxLength": 131072,
                    },
                },
                "required": ["routine_id"],
                "additionalProperties": False,
            },
            handler=run_routine,
            capability="routine.execute",
            effect=ToolEffect.ORCHESTRATION,
            risk=ToolRisk.MEDIUM,
            source="jarvs_builtin",
            scope="configured_workspace_routine_store",
        ),
    )


def register_routine_tools(
    registry: ToolRegistry,
    store_provider: StoreProvider,
    registry_provider: RegistryProvider,
    executor_provider: ExecutorProvider,
) -> None:
    for definition in routine_tool_definitions(
        store_provider,
        registry_provider,
        executor_provider,
    ):
        registry.register(definition)
