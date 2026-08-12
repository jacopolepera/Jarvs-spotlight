"""Deterministic routing between Jarvs execution lanes.

Routing consumes a structured plan produced by the application/model adapter;
it deliberately does not classify natural-language prompts with keyword rules
or an extra model call.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class ExecutionLane(str, Enum):
    FAST = "fast"
    ACTION = "action"
    SPECIALIST = "specialist"


class LaneUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutingSignal:
    """Explicit evidence used to select a lane for one planned step."""

    tool_name: str | None = None
    specialist_capability: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("tool_name", "specialist_capability"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 200
            ):
                raise ValueError(f"{field_name} must contain 1 to 200 characters")
        if self.tool_name is not None and self.specialist_capability is not None:
            raise ValueError(
                "a routing signal cannot request a tool and a specialist simultaneously"
            )


@dataclass(frozen=True)
class RouteDecision:
    lane: ExecutionLane
    reason_code: str


def select_execution_lane(signal: RoutingSignal) -> RouteDecision:
    if signal.tool_name:
        return RouteDecision(ExecutionLane.ACTION, "structured_tool_call")
    if signal.specialist_capability:
        return RouteDecision(ExecutionLane.SPECIALIST, "specialist_requested")
    return RouteDecision(ExecutionLane.FAST, "direct_response")


LaneHandler = Callable[[Any], Any]
LaneFactory = Callable[[], LaneHandler]


class LazyLaneDispatcher:
    """Load action and specialist handlers only when their lane is selected."""

    def __init__(
        self,
        fast_handler: LaneHandler,
        *,
        action_factory: LaneFactory | None = None,
        specialist_factory: LaneFactory | None = None,
    ) -> None:
        if not callable(fast_handler):
            raise TypeError("fast_handler must be callable")
        self._handlers: dict[ExecutionLane, LaneHandler] = {
            ExecutionLane.FAST: fast_handler,
        }
        self._factories = {
            ExecutionLane.ACTION: action_factory,
            ExecutionLane.SPECIALIST: specialist_factory,
        }
        self._lock = threading.RLock()

    def is_loaded(self, lane: ExecutionLane) -> bool:
        with self._lock:
            return lane in self._handlers

    def dispatch(self, signal: RoutingSignal, payload: Any) -> Any:
        decision = select_execution_lane(signal)
        handler = self._handler_for(decision.lane)
        return handler(payload)

    def _handler_for(self, lane: ExecutionLane) -> LaneHandler:
        with self._lock:
            handler = self._handlers.get(lane)
            if handler is not None:
                return handler
            factory = self._factories.get(lane)
            if factory is None:
                raise LaneUnavailableError(f"{lane.value} lane is not configured")
            handler = factory()
            if not callable(handler):
                raise TypeError(f"{lane.value} lane factory did not return a callable")
            self._handlers[lane] = handler
            return handler
