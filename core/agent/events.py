"""Versioned semantic events emitted by the Jarvs application core.

The transport may translate these events for WebSocket, ACP, or an evaluation
harness, but event creation and ordering belong to the Python core.  Payloads
are restricted to JSON values so they can be replayed without importing the
runtime that originally produced them.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


EVENT_SCHEMA_VERSION = 1
MAX_EVENT_PAYLOAD_BYTES = 1_048_576


class EventValidationError(ValueError):
    """An event cannot be represented by the stable JSON event contract."""


class EventType(str, Enum):
    RUN_STARTED = "run.started"
    ROUTE_SELECTED = "route.selected"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_RETRY_SCHEDULED = "model.retry_scheduled"
    TOOL_REQUESTED = "tool.requested"
    TOOL_APPROVAL_REQUIRED = "tool.approval_required"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_DENIED = "tool.denied"
    TOOL_CANCELLED = "tool.cancelled"
    TOOL_FAILED = "tool.failed"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _validate_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise EventValidationError(f"{field_name} must contain 1 to 200 characters")
    return value


def _json_round_trip(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EventValidationError("event payload must contain only JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise EventValidationError("event payload exceeds the 1 MiB limit")
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
class RunEvent:
    """One immutable event in a run-scoped, monotonically ordered stream."""

    event_id: str
    run_id: str
    sequence: int
    type: str
    occurred_at: str
    payload: Mapping[str, Any]
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_identifier(self.event_id, "event_id")
        _validate_identifier(self.run_id, "run_id")
        normalized_type = self.type.value if isinstance(self.type, EventType) else self.type
        if not isinstance(normalized_type, str) or not normalized_type:
            raise EventValidationError("type must be a non-empty string")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise EventValidationError("sequence must be a positive integer")
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise EventValidationError("schema_version must be a positive integer")
        if (
            not isinstance(self.occurred_at, str)
            or not self.occurred_at
            or len(self.occurred_at) > 100
        ):
            raise EventValidationError("occurred_at must be a non-empty timestamp string")
        if not isinstance(self.payload, Mapping):
            raise EventValidationError("event payload must be a JSON object")
        normalized_payload = _json_round_trip(_thaw(self.payload))
        object.__setattr__(self, "type", normalized_type)
        object.__setattr__(self, "payload", _freeze(normalized_payload))

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        type: EventType | str,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        schema_version: int = EVENT_SCHEMA_VERSION,
    ) -> "RunEvent":
        normalized_type = type.value if isinstance(type, EventType) else type
        _validate_identifier(run_id, "run_id")
        normalized_event_id = event_id if event_id is not None else uuid.uuid4().hex
        _validate_identifier(normalized_event_id, "event_id")
        if not isinstance(normalized_type, str) or not normalized_type:
            raise EventValidationError("type must be a non-empty string")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise EventValidationError("sequence must be a positive integer")
        if not isinstance(schema_version, int) or schema_version < 1:
            raise EventValidationError("schema_version must be a positive integer")
        normalized_occurred_at = occurred_at if occurred_at is not None else utc_now()
        if (
            not isinstance(normalized_occurred_at, str)
            or not normalized_occurred_at
            or len(normalized_occurred_at) > 100
        ):
            raise EventValidationError("occurred_at must be a non-empty timestamp string")
        normalized_payload = _json_round_trip(payload if payload is not None else {})
        return cls(
            event_id=normalized_event_id,
            run_id=run_id,
            sequence=sequence,
            type=normalized_type,
            occurred_at=normalized_occurred_at,
            payload=_freeze(normalized_payload),
            schema_version=schema_version,
        )

    def payload_dict(self) -> dict[str, Any]:
        return _thaw(self.payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload_dict(),
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> "RunEvent":
        required = {"event_id", "run_id", "sequence", "type", "occurred_at"}
        missing = sorted(required - set(record))
        if missing:
            raise EventValidationError(
                f"event record is missing required fields: {', '.join(missing)}"
            )
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise EventValidationError("payload must be a JSON object")
        return cls.create(
            event_id=record.get("event_id"),
            run_id=record.get("run_id"),
            sequence=record.get("sequence"),
            type=record.get("type"),
            occurred_at=record.get("occurred_at"),
            payload=payload,
            schema_version=record.get("schema_version", EVENT_SCHEMA_VERSION),
        )


EventSink = Callable[[RunEvent], None]


class RunEventStream:
    """Assign run-local sequence numbers and synchronously notify event sinks."""

    def __init__(
        self,
        run_id: str,
        *,
        sinks: Sequence[EventSink] = (),
        starting_sequence: int = 0,
    ) -> None:
        _validate_identifier(run_id, "run_id")
        if starting_sequence < 0:
            raise ValueError("starting_sequence cannot be negative")
        self.run_id = run_id
        self._sinks = tuple(sinks)
        self._sequence = starting_sequence
        self._last_type: str | None = None
        self._lock = threading.RLock()

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def last_type(self) -> str | None:
        with self._lock:
            return self._last_type

    def emit(
        self,
        type: EventType | str,
        payload: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        # Keep ordering and durable sinks serialized.  If a sink fails before an
        # external action starts, the executor fails closed rather than losing
        # the audit trail.
        with self._lock:
            event = RunEvent.create(
                run_id=self.run_id,
                sequence=self._sequence + 1,
                type=type,
                payload=payload,
            )
            # Sequence numbers are never reused, including when an observer
            # fails after an earlier sink has already persisted the event.
            self._sequence = event.sequence
            self._last_type = event.type
            for sink in self._sinks:
                sink(event)
            return event

    def advance_to(self, sequence: int) -> None:
        """Adopt a higher sequence emitted by a serialized child operation."""

        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        with self._lock:
            if sequence < self._sequence:
                raise ValueError("event sequence cannot move backwards")
            self._sequence = sequence
