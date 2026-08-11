import json
import subprocess
import sys

import pytest

from core.agent.routing import (
    ExecutionLane,
    LaneUnavailableError,
    LazyLaneDispatcher,
    RoutingSignal,
    select_execution_lane,
)


def test_structured_routing_defaults_to_fast_without_prompt_classification():
    assert select_execution_lane(RoutingSignal()).lane is ExecutionLane.FAST
    assert (
        select_execution_lane(RoutingSignal(tool_name="workspace.read_text")).lane
        is ExecutionLane.ACTION
    )
    assert (
        select_execution_lane(RoutingSignal(specialist_capability="coding")).lane
        is ExecutionLane.SPECIALIST
    )


def test_conflicting_structured_route_fails_closed():
    with pytest.raises(ValueError):
        RoutingSignal(
            tool_name="workspace.read_text",
            specialist_capability="coding",
        )


@pytest.mark.parametrize("kwargs", [{"tool_name": ""}, {"specialist_capability": " "}])
def test_empty_route_evidence_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RoutingSignal(**kwargs)


def test_fast_dispatch_never_initializes_action_or_specialist_factory():
    factory_calls = []

    def factory():
        factory_calls.append("loaded")
        return lambda payload: f"action:{payload}"

    dispatcher = LazyLaneDispatcher(
        lambda payload: f"fast:{payload}",
        action_factory=factory,
        specialist_factory=factory,
    )

    assert dispatcher.dispatch(RoutingSignal(), "hello") == "fast:hello"
    assert factory_calls == []
    assert dispatcher.is_loaded(ExecutionLane.ACTION) is False
    assert dispatcher.dispatch(
        RoutingSignal(tool_name="workspace.read_text"), "file"
    ) == "action:file"
    assert factory_calls == ["loaded"]
    dispatcher.dispatch(RoutingSignal(tool_name="workspace.read_text"), "again")
    assert factory_calls == ["loaded"]


def test_unconfigured_non_fast_lane_is_explicit():
    dispatcher = LazyLaneDispatcher(lambda payload: payload)
    with pytest.raises(LaneUnavailableError):
        dispatcher.dispatch(RoutingSignal(specialist_capability="coding"), {})


def test_fast_kernel_import_does_not_load_heavy_runtimes():
    probe = (
        "import json,sys; import core.agent.routing; "
        "names=('interpreter','litellm','mcp','fastapi','pydantic','PyQt6'); "
        "print(json.dumps([name for name in names if name in sys.modules]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []
