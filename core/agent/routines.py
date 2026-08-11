"""Versioned deterministic routines composed only from registered tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .events import RunEventStream, utc_now
from .executor import ToolExecutor
from .personal_memory import MemoryNotFoundError
from .tooling import (
    ToolContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolStatus,
    validate_tool_arguments,
)


MAX_ROUTINE_STEPS = 32
MAX_ROUTINE_MANIFEST_BYTES = 128 * 1024
MAX_TEMPLATE_DEPTH = 12


class RoutineError(RuntimeError):
    pass


class RoutineValidationError(RoutineError, ValueError):
    pass


class RoutineNotFoundError(RoutineError, LookupError):
    pass


@dataclass(frozen=True)
class RoutineVersion:
    routine_id: str
    version: int
    name: str
    description: str
    manifest: Mapping[str, Any]
    manifest_hash: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "routine_id": self.routine_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "manifest": json.loads(json.dumps(self.manifest)),
            "manifest_hash": self.manifest_hash,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RoutineRunResult:
    routine_id: str
    version: int
    run_id: str
    status: ToolStatus
    steps: tuple[ToolResult, ...]


def _canonical_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoutineValidationError("routine manifest must contain only JSON values") from exc
    if len(encoded) > MAX_ROUTINE_MANIFEST_BYTES:
        raise RoutineValidationError("routine manifest exceeds its size limit")
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()


class RoutineValidator:
    """Statically test templates against input and registered-tool schemas."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def validate(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        canonical, _hash = _canonical_manifest(manifest)
        allowed = {"schema_version", "name", "description", "input_schema", "steps"}
        unknown = set(canonical) - allowed
        if unknown:
            raise RoutineValidationError(
                f"routine manifest contains unknown fields: {', '.join(sorted(unknown))}"
            )
        if canonical.get("schema_version", 1) != 1:
            raise RoutineValidationError("unsupported routine schema version")
        name = canonical.get("name")
        description = canonical.get("description")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise RoutineValidationError("routine name is required and must be bounded")
        if not isinstance(description, str) or not description.strip() or len(description) > 2_000:
            raise RoutineValidationError("routine description is required and must be bounded")
        input_schema = canonical.get("input_schema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise RoutineValidationError("routine input_schema must describe an object")
        properties = input_schema.get("properties", {})
        if not isinstance(properties, dict):
            raise RoutineValidationError("routine input_schema properties must be an object")
        steps = canonical.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_ROUTINE_STEPS:
            raise RoutineValidationError(
                f"routine must contain 1 to {MAX_ROUTINE_STEPS} steps"
            )
        sample_inputs = {
            name: self._sample(schema)
            for name, schema in properties.items()
            if isinstance(schema, dict)
        }
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or set(step) != {"tool_name", "arguments"}:
                raise RoutineValidationError(
                    f"routine step {index + 1} must contain only tool_name and arguments"
                )
            tool_name = step["tool_name"]
            arguments = step["arguments"]
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                raise RoutineValidationError(f"routine step {index + 1} is invalid")
            try:
                definition = self.registry.resolve(tool_name)
            except Exception as exc:
                raise RoutineValidationError(
                    f"routine step {index + 1} references an unknown tool"
                ) from exc
            resolved = self.resolve_template(
                arguments,
                sample_inputs,
                allowed_inputs=frozenset(properties),
            )
            try:
                validate_tool_arguments(definition.input_schema, resolved)
            except Exception as exc:
                raise RoutineValidationError(
                    f"routine step {index + 1} does not match the tool schema: {exc}"
                ) from exc
        canonical["schema_version"] = 1
        return canonical

    def resolve_template(
        self,
        value: Any,
        inputs: Mapping[str, Any],
        *,
        allowed_inputs: frozenset[str] | None = None,
        depth: int = 0,
    ) -> Any:
        if depth > MAX_TEMPLATE_DEPTH:
            raise RoutineValidationError("routine template exceeds maximum nesting")
        if isinstance(value, dict) and set(value) == {"$input"}:
            name = value["$input"]
            if not isinstance(name, str) or not name:
                raise RoutineValidationError("$input must name a routine input")
            if allowed_inputs is not None and name not in allowed_inputs:
                raise RoutineValidationError(f"routine template references unknown input: {name}")
            if name not in inputs:
                raise RoutineValidationError(f"routine input is missing: {name}")
            return json.loads(json.dumps(inputs[name]))
        if isinstance(value, dict):
            if "$input" in value:
                raise RoutineValidationError("$input placeholders cannot contain sibling fields")
            return {
                key: self.resolve_template(
                    item,
                    inputs,
                    allowed_inputs=allowed_inputs,
                    depth=depth + 1,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self.resolve_template(
                    item,
                    inputs,
                    allowed_inputs=allowed_inputs,
                    depth=depth + 1,
                )
                for item in value
            ]
        return value

    @staticmethod
    def _sample(schema: Mapping[str, Any]) -> Any:
        if "const" in schema:
            return schema["const"]
        if isinstance(schema.get("enum"), list) and schema["enum"]:
            return schema["enum"][0]
        expected = schema.get("type", "string")
        if isinstance(expected, list):
            expected = next((item for item in expected if item != "null"), "null")
        if expected == "string":
            return "sample"
        if expected == "integer":
            return int(schema.get("minimum", 1))
        if expected == "number":
            return float(schema.get("minimum", 1.0))
        if expected == "boolean":
            return False
        if expected == "array":
            return []
        if expected == "object":
            return {}
        return None


class RoutineStore:
    def __init__(
        self,
        path_provider: Callable[[], str | Path],
        validator: RoutineValidator,
    ) -> None:
        self._path_provider = path_provider
        self.validator = validator
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return Path(self._path_provider())

    def save(
        self,
        manifest: Mapping[str, Any],
        *,
        routine_id: str | None = None,
    ) -> RoutineVersion:
        tested = self.validator.validate(manifest)
        canonical, manifest_hash = _canonical_manifest(tested)
        identifier = routine_id or uuid.uuid4().hex
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 200:
            raise RoutineValidationError("routine_id must contain 1 to 200 characters")
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT active_version FROM routines WHERE routine_id = ?", (identifier,)
            ).fetchone()
            version = 1 if current is None else int(current["active_version"]) + 1
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO routine_versions (
                    routine_id, version, name, description, manifest_json,
                    manifest_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    version,
                    canonical["name"],
                    canonical["description"],
                    json.dumps(canonical, ensure_ascii=False, separators=(",", ":")),
                    manifest_hash,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO routines (routine_id, active_version, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(routine_id) DO UPDATE SET
                    active_version = excluded.active_version,
                    updated_at = excluded.updated_at
                """,
                (identifier, version, created_at, created_at),
            )
        return self.get(identifier, version=version)

    def get(self, routine_id: str, *, version: int | None = None) -> RoutineVersion:
        with self._lock, self._connect() as connection:
            if version is None:
                row = connection.execute(
                    """
                    SELECT versions.* FROM routine_versions AS versions
                    JOIN routines USING (routine_id)
                    WHERE versions.routine_id = ?
                      AND versions.version = routines.active_version
                    """,
                    (routine_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM routine_versions WHERE routine_id = ? AND version = ?",
                    (routine_id, version),
                ).fetchone()
        if row is None:
            raise RoutineNotFoundError("Routine not found.")
        return self._version(row)

    def list(self, *, limit: int = 100) -> tuple[RoutineVersion, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.* FROM routine_versions AS versions
                JOIN routines USING (routine_id)
                WHERE versions.version = routines.active_version
                ORDER BY routines.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._version(row) for row in rows)

    def delete(self, routine_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM routines WHERE routine_id = ?", (routine_id,)
            )
        return cursor.rowcount == 1

    def export(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM routine_versions ORDER BY routine_id, version"
            ).fetchall()
        return {
            "schema_version": 1,
            "exported_at": utc_now(),
            "routines": [self._version(row).as_dict() for row in rows],
        }

    def _connect(self) -> sqlite3.Connection:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS routines (
                routine_id TEXT PRIMARY KEY,
                active_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS routine_versions (
                routine_id TEXT NOT NULL REFERENCES routines(routine_id) ON DELETE CASCADE
                    DEFERRABLE INITIALLY DEFERRED,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (routine_id, version)
            )
            """
        )
        return connection

    @staticmethod
    def _version(row: Mapping[str, Any]) -> RoutineVersion:
        return RoutineVersion(
            routine_id=row["routine_id"],
            version=int(row["version"]),
            name=row["name"],
            description=row["description"],
            manifest=json.loads(row["manifest_json"]),
            manifest_hash=row["manifest_hash"],
            created_at=row["created_at"],
        )


class RoutineRunner:
    """Replay a validated routine without a model call or generated code."""

    def __init__(
        self,
        store: RoutineStore,
        registry: ToolRegistry,
        executor: ToolExecutor,
    ) -> None:
        self.store = store
        self.registry = registry
        self.executor = executor

    async def execute(
        self,
        routine_id: str,
        inputs: Mapping[str, Any],
        context: ToolContext,
        *,
        version: int | None = None,
    ) -> RoutineRunResult:
        routine = self.store.get(routine_id, version=version)
        manifest = routine.manifest
        validate_tool_arguments(manifest["input_schema"], inputs)
        results: list[ToolResult] = []
        allowed = frozenset(manifest["input_schema"].get("properties", {}))
        for step in manifest["steps"]:
            if context.cancelled.is_set():
                break
            arguments = self.store.validator.resolve_template(
                step["arguments"],
                inputs,
                allowed_inputs=allowed,
            )
            result = await self.executor.execute(
                context,
                ToolInvocation.create(step["tool_name"], arguments),
            )
            results.append(result)
            if result.status is not ToolStatus.COMPLETED:
                break
        status = (
            ToolStatus.COMPLETED
            if len(results) == len(manifest["steps"])
            and all(result.status is ToolStatus.COMPLETED for result in results)
            else (results[-1].status if results else ToolStatus.CANCELLED)
        )
        return RoutineRunResult(
            routine_id=routine.routine_id,
            version=routine.version,
            run_id=context.run_id,
            status=status,
            steps=tuple(results),
        )
