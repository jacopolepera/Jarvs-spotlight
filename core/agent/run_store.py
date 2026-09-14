"""Durable append-only run events and effect recovery metadata.

The semantic event log is authoritative.  The ``runs`` and ``effects`` tables
are transactional projections used for bounded inspection and crash recovery;
they can always be rebuilt from ``run_events``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .events import EventType, RunEvent, utc_now


RUN_STATUSES = frozenset({"running", "completed", "cancelled", "failed"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "failed"})
EFFECT_EVENT_STATUSES = {
    EventType.TOOL_REQUESTED.value: "requested",
    EventType.TOOL_APPROVAL_REQUIRED.value: "approval_required",
    EventType.TOOL_STARTED.value: "started",
    EventType.TOOL_COMPLETED.value: "completed",
    EventType.TOOL_DENIED.value: "denied",
    EventType.TOOL_CANCELLED.value: "cancelled",
    EventType.TOOL_FAILED.value: "failed",
}
TERMINAL_EFFECT_STATUSES = frozenset({"completed", "denied", "cancelled", "failed"})
RECOVERY_ACKNOWLEDGED = "recovery.acknowledged"
RECOVERY_RESOLUTIONS = frozenset({"confirmed_applied", "confirmed_not_applied", "reviewed"})


class RunStoreError(RuntimeError):
    pass


class RunNotFoundError(RunStoreError, LookupError):
    pass


class RunStoreSequenceError(RunStoreError, ValueError):
    pass


@dataclass(frozen=True)
class RecoveryRecord:
    run_id: str
    invocation_id: str
    tool_name: str
    effect: str | None
    detected_at: str
    resolution: str | None
    acknowledged_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    session_id: str
    generation_id: str
    status: str
    started_at: str
    updated_at: str
    completed_at: str | None
    last_sequence: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EffectRecord:
    run_id: str
    invocation_id: str
    tool_name: str
    arguments_hash: str
    invocation_hash: str | None
    capability: str | None
    effect: str | None
    risk: str | None
    scope: str | None
    tool_version: str | None
    status: str
    requested_at: str
    started_at: str | None
    completed_at: str | None
    reason_code: str | None
    last_sequence: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunStore:
    """Lazy SQLite event store with deterministic replay.

    Construction, health checks, and an empty read do not create the database.
    The first ``run.started`` event creates the store and schema.
    """

    def __init__(self, path_provider: Callable[[], str | Path]) -> None:
        self._path_provider = path_provider
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return Path(self._path_provider())

    def append_event(self, event: RunEvent) -> bool:
        """Append one event, returning ``False`` for an identical retry."""

        if not isinstance(event, RunEvent):
            raise TypeError("event must be a RunEvent")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event(connection, event)

    def _append_event(self, connection: sqlite3.Connection, event: RunEvent) -> bool:
        record = event.as_dict()
        payload_json = json.dumps(
            record["payload"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = connection.execute(
            "SELECT * FROM run_events WHERE run_id = ? AND sequence = ?",
            (event.run_id, event.sequence),
        ).fetchone()
        if existing is not None:
            if self._same_event(existing, event, payload_json):
                return False
            raise RunStoreSequenceError(
                "run sequence is already occupied by a different event"
            )

        run = connection.execute(
            "SELECT last_sequence, status FROM runs WHERE run_id = ?",
            (event.run_id,),
        ).fetchone()
        if run is None:
            if event.sequence != 1 or event.type != EventType.RUN_STARTED.value:
                raise RunStoreSequenceError(
                    "a run must begin with run.started at sequence 1"
                )
            payload = event.payload_dict()
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, session_id, generation_id, status, started_at,
                    updated_at, completed_at, last_sequence
                ) VALUES (?, ?, ?, 'running', ?, ?, NULL, 0)
                """,
                (
                    event.run_id,
                    self._bounded_text(payload.get("session_id")),
                    self._bounded_text(payload.get("generation_id")),
                    event.occurred_at,
                    event.occurred_at,
                ),
            )
            expected_sequence = 1
        else:
            if run["status"] in TERMINAL_RUN_STATUSES and event.type != RECOVERY_ACKNOWLEDGED:
                raise RunStoreError("run lifecycle cannot advance after a terminal event")
            expected_sequence = int(run["last_sequence"]) + 1
        if event.sequence != expected_sequence:
            raise RunStoreSequenceError(
                f"expected run sequence {expected_sequence}, got {event.sequence}"
            )

        connection.execute(
            """
            INSERT INTO run_events (
                event_id, run_id, sequence, type, occurred_at,
                payload_json, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.run_id,
                event.sequence,
                event.type,
                event.occurred_at,
                payload_json,
                event.schema_version,
            ),
        )
        status = self._run_status(event.type)
        connection.execute(
            """
            UPDATE runs
            SET status = COALESCE(?, status),
                updated_at = ?,
                completed_at = CASE
                    WHEN ? IS NULL THEN completed_at ELSE ?
                END,
                last_sequence = ?
            WHERE run_id = ?
            """,
            (
                status,
                event.occurred_at,
                status,
                event.occurred_at,
                event.sequence,
                event.run_id,
            ),
        )
        self._project_effect(connection, event)
        self._project_recovery(connection, event)
        return True

    def recover_interrupted_runs(self) -> dict[str, int]:
        """Close orphaned runs once at daemon startup, before accepting work.

        This must only be called by the exclusive daemon owner, never while
        another process can execute work in this store. No tool is invoked.
        Each run's recovery events and projections commit atomically; a crash
        during recovery leaves that run eligible for the next startup.
        """
        counts = {"runs": 0, "cancelled_effects": 0, "uncertain_effects": 0}
        if not self.path.exists():
            return counts
        while True:
            with self._lock, self._connect(create=False) as connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at, run_id LIMIT 1"
                ).fetchone()
                if run is None:
                    return counts
                effects = connection.execute(
                    "SELECT * FROM effects WHERE run_id = ? AND status IN ('requested', 'approval_required', 'started') ORDER BY last_sequence",
                    (run["run_id"],),
                ).fetchall()
                sequence = int(run["last_sequence"])
                timestamp = utc_now()
                uncertain = cancelled = 0
                for effect in effects:
                    started = effect["status"] == "started"
                    sequence += 1
                    self._append_event(connection, RunEvent.create(
                        run_id=run["run_id"], sequence=sequence,
                        type=EventType.TOOL_FAILED if started else EventType.TOOL_CANCELLED,
                        occurred_at=timestamp,
                        payload={
                            "invocation_id": effect["invocation_id"],
                            "tool_name": effect["tool_name"],
                            "arguments_hash": effect["arguments_hash"],
                            "reason_code": "restart_outcome_unknown" if started else "restart_before_start",
                            "recovery_required": started,
                        },
                    ))
                    uncertain += int(started)
                    cancelled += int(not started)
                self._append_event(connection, RunEvent.create(
                    run_id=run["run_id"], sequence=sequence + 1,
                    type=EventType.RUN_FAILED, occurred_at=timestamp,
                    payload={"reason_code": "daemon_restarted", "uncertain_effects": uncertain},
                ))
            counts["runs"] += 1
            counts["uncertain_effects"] += uncertain
            counts["cancelled_effects"] += cancelled

    def list_recoveries(
        self, *, include_acknowledged: bool = False, limit: int = 100
    ) -> tuple[RecoveryRecord, ...]:
        self._validate_limit(limit)
        if not self.path.exists():
            return ()
        where = "" if include_acknowledged else "WHERE r.acknowledged_at IS NULL"
        with self._lock, self._connect(create=False) as connection:
            rows = connection.execute(
                f"""SELECT r.*, e.tool_name, e.effect FROM effect_recoveries r
                JOIN effects e USING (run_id, invocation_id) {where}
                ORDER BY r.detected_at DESC, r.run_id, r.invocation_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(self._recovery(row) for row in rows)

    def acknowledge_recovery(
        self, run_id: str, invocation_id: str, *, resolution: str
    ) -> RecoveryRecord:
        """Record human review, without claiming or retrying tool execution."""
        self._validate_identifier(run_id, "run_id")
        self._validate_identifier(invocation_id, "invocation_id")
        if resolution not in RECOVERY_RESOLUTIONS:
            raise ValueError("invalid recovery resolution")
        if not self.path.exists():
            raise RunNotFoundError("Recovery not found.")
        with self._lock, self._connect(create=False) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT r.*, e.tool_name, e.effect FROM effect_recoveries r
                JOIN effects e USING (run_id, invocation_id)
                WHERE r.run_id = ? AND r.invocation_id = ?""",
                (run_id, invocation_id),
            ).fetchone()
            if row is None:
                raise RunNotFoundError("Recovery not found.")
            if row["resolution"] is not None:
                if row["resolution"] != resolution:
                    raise RunStoreError("recovery has already been acknowledged differently")
                return self._recovery(row)
            sequence = connection.execute(
                "SELECT last_sequence FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["last_sequence"]
            timestamp = utc_now()
            self._append_event(connection, RunEvent.create(
                run_id=run_id, sequence=int(sequence) + 1,
                type=RECOVERY_ACKNOWLEDGED, occurred_at=timestamp,
                payload={"invocation_id": invocation_id, "resolution": resolution},
            ))
            return RecoveryRecord(
                run_id=run_id, invocation_id=invocation_id, tool_name=row["tool_name"],
                effect=row["effect"], detected_at=row["detected_at"],
                resolution=resolution, acknowledged_at=timestamp,
            )

    def _project_recovery(self, connection: sqlite3.Connection, event: RunEvent) -> None:
        payload = event.payload_dict()
        if event.type == EventType.TOOL_FAILED.value and payload.get("reason_code") == "restart_outcome_unknown":
            connection.execute(
                """INSERT INTO effect_recoveries
                (run_id, invocation_id, detected_at, resolution, acknowledged_at)
                VALUES (?, ?, ?, NULL, NULL)""",
                (event.run_id, payload["invocation_id"], event.occurred_at),
            )
        elif event.type == RECOVERY_ACKNOWLEDGED:
            resolution = payload.get("resolution")
            if resolution not in RECOVERY_RESOLUTIONS:
                raise RunStoreError("invalid recovery resolution")
            updated = connection.execute(
                """UPDATE effect_recoveries SET resolution = ?, acknowledged_at = ?
                WHERE run_id = ? AND invocation_id = ? AND resolution IS NULL""",
                (resolution, event.occurred_at, event.run_id, payload.get("invocation_id")),
            )
            if updated.rowcount != 1:
                raise RunStoreError("recovery is missing or already acknowledged")

    @staticmethod
    def _recovery(row: Mapping[str, Any]) -> RecoveryRecord:
        return RecoveryRecord(**{field: row[field] for field in RecoveryRecord.__dataclass_fields__})

    def get_run(self, run_id: str) -> RunSummary:
        self._validate_identifier(run_id, "run_id")
        if not self.path.exists():
            raise RunNotFoundError("Run not found.")
        with self._lock, self._connect(create=False) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError("Run not found.")
        return self._summary(row)

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[RunSummary, ...]:
        if status is not None and status not in RUN_STATUSES:
            raise ValueError("invalid run status")
        self._validate_limit(limit)
        if not self.path.exists():
            return ()
        where = "WHERE status = ?" if status is not None else ""
        parameters: tuple[Any, ...] = (status, limit) if status is not None else (limit,)
        with self._lock, self._connect(create=False) as connection:
            rows = connection.execute(
                f"SELECT * FROM runs {where} ORDER BY started_at DESC, run_id LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(self._summary(row) for row in rows)

    def incomplete_runs(self, *, limit: int = 100) -> tuple[RunSummary, ...]:
        return self.list_runs(status="running", limit=limit)

    def replay(self, run_id: str) -> tuple[RunEvent, ...]:
        self.get_run(run_id)
        with self._lock, self._connect(create=False) as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        events = tuple(
            RunEvent.from_dict(
                {
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "sequence": int(row["sequence"]),
                    "type": row["type"],
                    "occurred_at": row["occurred_at"],
                    "payload": json.loads(row["payload_json"]),
                    "schema_version": int(row["schema_version"]),
                }
            )
            for row in rows
        )
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise RunStoreSequenceError("stored run has a non-contiguous event sequence")
        return events

    def list_effects(
        self,
        *,
        run_id: str | None = None,
        incomplete_only: bool = False,
        limit: int = 100,
    ) -> tuple[EffectRecord, ...]:
        if run_id is not None:
            self._validate_identifier(run_id, "run_id")
        self._validate_limit(limit)
        if not self.path.exists():
            return ()
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if incomplete_only:
            placeholders = ",".join("?" for _ in TERMINAL_EFFECT_STATUSES)
            clauses.append(f"status NOT IN ({placeholders})")
            parameters.extend(sorted(TERMINAL_EFFECT_STATUSES))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock, self._connect(create=False) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM effects {where}
                ORDER BY requested_at DESC, invocation_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._effect(row) for row in rows)

    def _project_effect(self, connection: sqlite3.Connection, event: RunEvent) -> None:
        status = EFFECT_EVENT_STATUSES.get(event.type)
        if status is None:
            return
        payload = event.payload_dict()
        invocation_id = self._bounded_text(payload.get("invocation_id"))
        tool_name = self._bounded_text(payload.get("tool_name"))
        arguments_hash = self._bounded_text(payload.get("arguments_hash"))
        if not invocation_id or not tool_name or not arguments_hash:
            raise RunStoreError("tool event is missing its durable correlation fields")
        existing = connection.execute(
            "SELECT status FROM effects WHERE run_id = ? AND invocation_id = ?",
            (event.run_id, invocation_id),
        ).fetchone()
        if existing is None:
            if event.type != EventType.TOOL_REQUESTED.value:
                raise RunStoreError("tool lifecycle must begin with tool.requested")
            connection.execute(
                """
                INSERT INTO effects (
                    run_id, invocation_id, tool_name, arguments_hash,
                    invocation_hash, capability, effect, risk, scope,
                    tool_version, status, requested_at, started_at,
                    completed_at, reason_code, last_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    event.run_id,
                    invocation_id,
                    tool_name,
                    arguments_hash,
                    self._optional_text(payload.get("invocation_hash")),
                    self._optional_text(payload.get("capability")),
                    self._optional_text(payload.get("effect")),
                    self._optional_text(payload.get("risk")),
                    self._optional_text(payload.get("scope")),
                    self._optional_text(payload.get("tool_version")),
                    status,
                    event.occurred_at,
                    self._optional_text(payload.get("reason_code")),
                    event.sequence,
                ),
            )
            return
        if existing["status"] in TERMINAL_EFFECT_STATUSES:
            raise RunStoreError("tool lifecycle cannot advance after a terminal event")
        connection.execute(
            """
            UPDATE effects
            SET invocation_hash = COALESCE(?, invocation_hash),
                capability = COALESCE(?, capability),
                effect = COALESCE(?, effect),
                risk = COALESCE(?, risk),
                scope = COALESCE(?, scope),
                tool_version = COALESCE(?, tool_version),
                status = ?,
                started_at = CASE WHEN ? = 'started' THEN ? ELSE started_at END,
                completed_at = CASE WHEN ? IN ('completed', 'denied', 'cancelled', 'failed') THEN ? ELSE completed_at END,
                reason_code = COALESCE(?, reason_code),
                last_sequence = ?
            WHERE run_id = ? AND invocation_id = ?
            """,
            (
                self._optional_text(payload.get("invocation_hash")),
                self._optional_text(payload.get("capability")),
                self._optional_text(payload.get("effect")),
                self._optional_text(payload.get("risk")),
                self._optional_text(payload.get("scope")),
                self._optional_text(payload.get("tool_version")),
                status,
                status,
                event.occurred_at,
                status,
                event.occurred_at,
                self._optional_text(payload.get("reason_code")),
                event.sequence,
                event.run_id,
                invocation_id,
            ),
        )

    @contextmanager
    def _connect(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection(create=create)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _open_connection(self, *, create: bool = True) -> sqlite3.Connection:
        path = self.path
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.exists():
            raise RunNotFoundError("Run store does not exist.")
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'cancelled', 'failed')),
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                last_sequence INTEGER NOT NULL CHECK(last_sequence >= 0)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK(sequence >= 1),
                type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                UNIQUE(run_id, sequence)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                invocation_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                invocation_hash TEXT,
                capability TEXT,
                effect TEXT,
                risk TEXT,
                scope TEXT,
                tool_version TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                reason_code TEXT,
                last_sequence INTEGER NOT NULL CHECK(last_sequence >= 1),
                PRIMARY KEY(run_id, invocation_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS effects_status ON effects(status, requested_at DESC)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS effect_recoveries (
                run_id TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                resolution TEXT,
                acknowledged_at TEXT,
                PRIMARY KEY(run_id, invocation_id),
                FOREIGN KEY(run_id, invocation_id)
                    REFERENCES effects(run_id, invocation_id) ON DELETE CASCADE
            )"""
        )
        return connection

    @staticmethod
    def _same_event(row: Mapping[str, Any], event: RunEvent, payload_json: str) -> bool:
        return (
            row["event_id"] == event.event_id
            and row["type"] == event.type
            and row["occurred_at"] == event.occurred_at
            and row["payload_json"] == payload_json
            and int(row["schema_version"]) == event.schema_version
        )

    @staticmethod
    def _run_status(event_type: str) -> str | None:
        return {
            EventType.RUN_COMPLETED.value: "completed",
            EventType.RUN_CANCELLED.value: "cancelled",
            EventType.RUN_FAILED.value: "failed",
        }.get(event_type)

    @staticmethod
    def _summary(row: Mapping[str, Any]) -> RunSummary:
        return RunSummary(
            run_id=row["run_id"],
            session_id=row["session_id"],
            generation_id=row["generation_id"],
            status=row["status"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            last_sequence=int(row["last_sequence"]),
        )

    @staticmethod
    def _effect(row: Mapping[str, Any]) -> EffectRecord:
        return EffectRecord(
            run_id=row["run_id"],
            invocation_id=row["invocation_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            invocation_hash=row["invocation_hash"],
            capability=row["capability"],
            effect=row["effect"],
            risk=row["risk"],
            scope=row["scope"],
            tool_version=row["tool_version"],
            status=row["status"],
            requested_at=row["requested_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            reason_code=row["reason_code"],
            last_sequence=int(row["last_sequence"]),
        )

    @staticmethod
    def _validate_identifier(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ValueError(f"{field_name} must contain 1 to 200 characters")
        return value

    @staticmethod
    def _validate_limit(limit: object) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return limit

    @classmethod
    def _bounded_text(cls, value: object) -> str:
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > 500:
            raise RunStoreError("run event contains an invalid correlation field")
        return value

    @classmethod
    def _optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return cls._bounded_text(value)
