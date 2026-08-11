"""Typed tool contracts and the in-process registry for Jarvs-native tools."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from .events import RunEventStream


MAX_TOOL_ARGUMENT_BYTES = 262_144
MAX_TOOL_OUTPUT_BYTES = 1_048_576
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")


class ToolError(RuntimeError):
    pass


class ToolNotFoundError(ToolError, LookupError):
    pass


class ToolRegistryError(ToolError):
    pass


class ToolValidationError(ToolError, ValueError):
    def __init__(self, message: str, *, reason_code: str = "invalid_tool_arguments"):
        super().__init__(message)
        self.reason_code = reason_code


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    ORCHESTRATION = "orchestration"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    CODE_EXECUTION = "code_execution"


class ToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolBackend(str, Enum):
    IN_PROCESS = "in_process"
    ISOLATED_PROCESS = "isolated_process"
    MCP = "mcp"
    SPECIALIST = "specialist"


class ToolStatus(str, Enum):
    COMPLETED = "completed"
    DENIED = "denied"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _normalize_json(value: Any, *, byte_limit: int, label: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(f"{label} must contain only JSON values") from exc
    if len(encoded.encode("utf-8")) > byte_limit:
        raise ToolValidationError(f"{label} exceeds its size limit")
    return json.loads(encoded)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolInvocation:
    invocation_id: str
    tool_name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_tool_name(self.tool_name)
        if not isinstance(self.invocation_id, str) or not 0 < len(self.invocation_id) <= 200:
            raise ToolValidationError("invocation_id must contain 1 to 200 characters")
        if not isinstance(self.arguments, Mapping):
            raise ToolValidationError("tool arguments must be a JSON object")
        normalized = _normalize_json(
            _thaw(self.arguments),
            byte_limit=MAX_TOOL_ARGUMENT_BYTES,
            label="tool arguments",
        )
        object.__setattr__(self, "arguments", _freeze(normalized))

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        invocation_id: str | None = None,
    ) -> "ToolInvocation":
        _validate_tool_name(tool_name)
        normalized_id = invocation_id if invocation_id is not None else uuid.uuid4().hex
        if not isinstance(normalized_id, str) or not 0 < len(normalized_id) <= 200:
            raise ToolValidationError("invocation_id must contain 1 to 200 characters")
        return cls(
            normalized_id,
            tool_name,
            arguments if arguments is not None else {},
        )

    def arguments_dict(self) -> dict[str, Any]:
        return _thaw(self.arguments)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {"tool_name": self.tool_name, "arguments": self.arguments_dict()},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


ApprovalNotifier = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class ToolContext:
    run_id: str
    session_id: str
    generation_id: str
    events: RunEventStream
    workspace_root: Path | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    approval_notifier: ApprovalNotifier | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "session_id", "generation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not 0 < len(value) <= 200:
                raise ValueError(f"{name} must contain 1 to 200 characters")
        if self.events.run_id != self.run_id:
            raise ValueError("event stream does not belong to this run")
        if self.workspace_root is not None:
            self.workspace_root = Path(self.workspace_root)


ToolHandler = Callable[[Mapping[str, Any], ToolContext], Any]
ToolPreflight = Callable[[Mapping[str, Any], ToolContext], str | None]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    capability: str = "general"
    effect: ToolEffect = ToolEffect.READ_ONLY
    risk: ToolRisk = ToolRisk.LOW
    source: str = "jarvs_builtin"
    version: str = "1.0.0"
    scope: str = "none"
    backend: ToolBackend = ToolBackend.IN_PROCESS
    preflight: ToolPreflight | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_tool_name(self.name)
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolRegistryError("tool description cannot be empty")
        if not callable(self.handler):
            raise ToolRegistryError("tool handler must be callable")
        if self.preflight is not None and not callable(self.preflight):
            raise ToolRegistryError("tool preflight must be callable")
        if not isinstance(self.capability, str) or not self.capability:
            raise ToolRegistryError("tool capability cannot be empty")
        if not isinstance(self.source, str) or not self.source:
            raise ToolRegistryError("tool source cannot be empty")
        if not isinstance(self.version, str) or not self.version:
            raise ToolRegistryError("tool version cannot be empty")
        if not isinstance(self.scope, str) or not self.scope:
            raise ToolRegistryError("tool scope cannot be empty")
        try:
            canonical_effect = ToolEffect(self.effect)
            canonical_risk = ToolRisk(self.risk)
            canonical_backend = ToolBackend(self.backend)
        except (TypeError, ValueError) as exc:
            raise ToolRegistryError(
                "tool effect, risk, and backend must use known values"
            ) from exc
        object.__setattr__(self, "effect", canonical_effect)
        object.__setattr__(self, "risk", canonical_risk)
        object.__setattr__(self, "backend", canonical_backend)
        normalized_schema = _normalize_json(
            dict(self.input_schema),
            byte_limit=MAX_TOOL_ARGUMENT_BYTES,
            label="tool input schema",
        )
        if normalized_schema.get("type") != "object":
            raise ToolRegistryError("tool input schema root type must be object")
        object.__setattr__(self, "input_schema", _freeze(normalized_schema))

    def public_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": _thaw(self.input_schema),
            "capability": self.capability,
            "effect": self.effect.value,
            "risk": self.risk.value,
            "source": self.source,
            "scope": self.scope,
            "backend": self.backend.value,
        }


@dataclass(frozen=True)
class ToolResult:
    invocation_id: str
    tool_name: str
    status: ToolStatus
    output: Any = None
    reason_code: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


class ToolRegistry:
    """Thread-safe registry that can be frozen after application bootstrap."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._frozen = False
        self._lock = threading.RLock()

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen

    def register(self, definition: ToolDefinition) -> None:
        with self._lock:
            if self._frozen:
                raise ToolRegistryError("tool registry is frozen")
            if definition.name in self._definitions:
                raise ToolRegistryError(f"tool already registered: {definition.name}")
            self._definitions[definition.name] = definition

    def freeze(self) -> None:
        with self._lock:
            self._frozen = True

    def resolve(self, name: str) -> ToolDefinition:
        with self._lock:
            try:
                return self._definitions[name]
            except KeyError as exc:
                raise ToolNotFoundError(f"unknown tool: {name}") from exc

    def definitions(self) -> tuple[ToolDefinition, ...]:
        with self._lock:
            return tuple(self._definitions[name] for name in sorted(self._definitions))

    def catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(definition.public_metadata() for definition in self.definitions())


def normalize_tool_output(value: Any) -> Any:
    return _normalize_json(
        value,
        byte_limit=MAX_TOOL_OUTPUT_BYTES,
        label="tool output",
    )


def validate_tool_arguments(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> None:
    """Validate the bounded JSON-Schema subset used by native tools.

    Full MCP JSON Schema support intentionally belongs to the MCP adapter phase.
    This validator supports object/array/scalar types, required/properties,
    additionalProperties, enum/const, size limits, and numeric ranges.
    """

    normalized = _normalize_json(
        _thaw(arguments),
        byte_limit=MAX_TOOL_ARGUMENT_BYTES,
        label="tool arguments",
    )
    _validate_value(_thaw(schema), normalized, path="$", depth=0)


def _validate_tool_name(name: object) -> None:
    if not isinstance(name, str) or len(name) > 200 or not TOOL_NAME_PATTERN.fullmatch(name):
        raise ToolValidationError(
            "tool name must be a lowercase, namespaced identifier such as workspace.read_text"
        )


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ToolValidationError(f"unsupported schema type: {expected}")


def _validate_value(schema: Mapping[str, Any], value: Any, *, path: str, depth: int) -> None:
    if depth > 12:
        raise ToolValidationError("tool arguments exceed maximum nesting depth")

    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    if expected is not None:
        if not all(isinstance(item, str) for item in expected_types):
            raise ToolValidationError(f"invalid schema type at {path}")
        if not any(_matches_type(value, item) for item in expected_types):
            raise ToolValidationError(f"{path} has the wrong type")

    if "const" in schema and value != schema["const"]:
        raise ToolValidationError(f"{path} does not match the required constant")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise ToolValidationError(f"{path} is not an allowed value")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ToolValidationError(f"invalid object schema at {path}")
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolValidationError(f"{path} is missing required fields: {', '.join(missing)}")
        if schema.get("additionalProperties", True) is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ToolValidationError(
                    f"{path} contains unknown fields: {', '.join(unknown)}"
                )
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                if not isinstance(child_schema, dict):
                    raise ToolValidationError(f"invalid schema for {path}.{key}")
                _validate_value(child_schema, item, path=f"{path}.{key}", depth=depth + 1)

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise ToolValidationError(f"{path} contains too few items")
        if maximum is not None and len(value) > maximum:
            raise ToolValidationError(f"{path} contains too many items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise ToolValidationError(f"invalid item schema at {path}")
            for index, item in enumerate(value):
                _validate_value(
                    item_schema,
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                )

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise ToolValidationError(f"{path} is shorter than allowed")
        if maximum is not None and len(value) > maximum:
            raise ToolValidationError(f"{path} is longer than allowed")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ToolValidationError(f"{path} is below the minimum")
        if maximum is not None and value > maximum:
            raise ToolValidationError(f"{path} is above the maximum")


def handler_is_async(handler: ToolHandler) -> bool:
    return inspect.iscoroutinefunction(handler)
