"""Thread-safe, one-shot approval broker for local execution requests."""

from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .policy import ExecutionIntent


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalTombstone:
    """Bounded, non-sensitive record used to reject replayed decisions."""

    session_id: str
    generation_id: str
    status: ApprovalStatus


@dataclass
class ApprovalRequest:
    approval_id: str
    session_id: str
    generation_id: str
    intent: ExecutionIntent
    created_monotonic: float
    expires_monotonic: float
    expires_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    resolved: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_event(self) -> dict[str, Any]:
        return {
            "type": "approval_required",
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "generation_id": self.generation_id,
            "tool_name": self.intent.tool_name,
            "title": self.intent.title,
            "arguments": self.intent.arguments_dict(),
            "capability": self.intent.capability,
            "effect": self.intent.effect,
            "risk": self.intent.risk,
            "scope": self.intent.scope,
            "tool_version": self.intent.tool_version,
            "invocation_hash": self.intent.fingerprint,
            "expires_at": self.expires_at,
        }


class ApprovalBroker:
    """Keep pending approvals in memory and resolve each exactly once.

    Restarts intentionally lose pending requests and therefore fail closed.
    Durable approvals belong to the later run/event-store phase.
    """

    def __init__(
        self,
        timeout_seconds: float = 300.0,
        *,
        tombstone_limit: int = 512,
        pending_limit: int = 128,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if tombstone_limit <= 0:
            raise ValueError("tombstone_limit must be positive")
        if pending_limit <= 0:
            raise ValueError("pending_limit must be positive")
        self.timeout_seconds = timeout_seconds
        self.tombstone_limit = tombstone_limit
        self.pending_limit = pending_limit
        self._lock = threading.RLock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._terminal: OrderedDict[str, ApprovalTombstone] = OrderedDict()

    def create(
        self,
        *,
        session_id: str,
        generation_id: str,
        intent: ExecutionIntent,
    ) -> ApprovalRequest:
        now = time.monotonic()
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=self.timeout_seconds
        )
        request = ApprovalRequest(
            approval_id=uuid.uuid4().hex,
            session_id=session_id,
            generation_id=generation_id,
            intent=intent,
            created_monotonic=now,
            expires_monotonic=now + self.timeout_seconds,
            expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            self._prune_expired_locked()
            if len(self._requests) >= self.pending_limit:
                raise ApprovalConflictError("Too many approval requests are pending.")
            self._requests[request.approval_id] = request
        return request

    def resolve(
        self,
        approval_id: str,
        *,
        approve: bool,
        session_id: str,
        generation_id: str,
    ) -> ApprovalStatus:
        with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                if approval_id in self._terminal:
                    raise ApprovalConflictError(
                        "Approval request has already been consumed."
                    )
                raise ApprovalNotFoundError("Approval request not found.")
            self._expire_locked(request)
            if request.status is not ApprovalStatus.PENDING:
                raise ApprovalConflictError(
                    f"Approval request is already {request.status.value}."
                )
            if (
                request.session_id != session_id
                or request.generation_id != generation_id
            ):
                raise ApprovalConflictError(
                    "Approval does not belong to the supplied session/generation."
                )
            request.status = (
                ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
            )
            request.resolved.set()
            return request.status

    def wait(
        self,
        approval_id: str,
        *,
        cancelled: threading.Event | None = None,
        poll_seconds: float = 0.05,
    ) -> ApprovalStatus:
        while True:
            with self._lock:
                request = self._requests.get(approval_id)
                if request is None:
                    raise ApprovalNotFoundError("Approval request not found.")
                self._expire_locked(request)
                if request.status is not ApprovalStatus.PENDING:
                    return self._consume_locked(request)
                if cancelled is not None and cancelled.is_set():
                    request.status = ApprovalStatus.CANCELLED
                    request.resolved.set()
                    return self._consume_locked(request)
                remaining = max(0.0, request.expires_monotonic - time.monotonic())
                event = request.resolved
            event.wait(min(poll_seconds, remaining))

    def deny_pending(self) -> int:
        """Deny all pending invocations, for example when policy becomes deny-all."""

        denied = 0
        with self._lock:
            for request in self._requests.values():
                self._expire_locked(request)
                if request.status is ApprovalStatus.PENDING:
                    request.status = ApprovalStatus.DENIED
                    request.resolved.set()
                    denied += 1
        return denied

    def abandon(
        self,
        approval_id: str,
        *,
        session_id: str,
        generation_id: str,
    ) -> ApprovalStatus:
        """Cancel and consume a request idempotently when its owner stops waiting."""

        with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                tombstone = self._terminal.get(approval_id)
                if tombstone is None:
                    raise ApprovalNotFoundError("Approval request not found.")
                if (
                    tombstone.session_id != session_id
                    or tombstone.generation_id != generation_id
                ):
                    raise ApprovalConflictError(
                        "Approval does not belong to the supplied session/generation."
                    )
                return tombstone.status
            if request.session_id != session_id or request.generation_id != generation_id:
                raise ApprovalConflictError(
                    "Approval does not belong to the supplied session/generation."
                )
            request.status = ApprovalStatus.CANCELLED
            request.resolved.set()
            return self._consume_locked(request)

    def _consume_locked(self, request: ApprovalRequest) -> ApprovalStatus:
        """Drop invocation arguments once the waiting execution resumes."""

        status = request.status
        self._requests.pop(request.approval_id, None)
        self._terminal[request.approval_id] = ApprovalTombstone(
            session_id=request.session_id,
            generation_id=request.generation_id,
            status=status,
        )
        self._terminal.move_to_end(request.approval_id)
        while len(self._terminal) > self.tombstone_limit:
            self._terminal.popitem(last=False)
        return status

    def _prune_expired_locked(self) -> None:
        for request in tuple(self._requests.values()):
            self._expire_locked(request)
            if request.status is ApprovalStatus.EXPIRED:
                self._consume_locked(request)

    @staticmethod
    def _expire_locked(request: ApprovalRequest) -> None:
        if (
            request.status is ApprovalStatus.PENDING
            and time.monotonic() >= request.expires_monotonic
        ):
            request.status = ApprovalStatus.EXPIRED
            request.resolved.set()
