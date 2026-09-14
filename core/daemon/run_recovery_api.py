"""Recovery inspection mounted inside the daemon's authentication middleware."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.agent.run_store import RunNotFoundError, RunStore, RunStoreError


@contextmanager
def exclusive_run_store_owner(store: RunStore) -> Iterator[None]:
    """Hold an OS lock for the daemon lifetime; crashes release ownership.

    Keep the lock file in place after release: deleting it would let processes
    lock different inodes for the same audit database. The SQLite DB stays lazy.
    """
    path = store.path.resolve().with_suffix(".owner.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunStoreError("Another daemon owns this run store.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RecoveryAcknowledgement(BaseModel):
    resolution: Literal["confirmed_applied", "confirmed_not_applied", "reviewed"]


def create_run_recovery_router(store: RunStore) -> APIRouter:
    router = APIRouter(prefix="/run-recovery")

    @router.get("")
    def list_recoveries(include_acknowledged: bool = False, limit: int = 100):
        try:
            records = store.list_recoveries(
                include_acknowledged=include_acknowledged, limit=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"recoveries": [record.as_dict() for record in records]}

    @router.post("/{run_id}/{invocation_id}/acknowledge")
    def acknowledge(run_id: str, invocation_id: str, payload: RecoveryAcknowledgement):
        try:
            record = store.acknowledge_recovery(
                run_id, invocation_id, resolution=payload.resolution
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RunStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"recovery": record.as_dict()}

    return router
