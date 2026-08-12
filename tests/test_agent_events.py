import math

import pytest

from core.agent.events import (
    EventType,
    EventValidationError,
    RunEvent,
    RunEventStream,
)


def test_event_round_trip_is_versioned_and_payload_is_immutable():
    event = RunEvent.create(
        event_id="event-1",
        run_id="run-1",
        sequence=1,
        type=EventType.RUN_STARTED,
        occurred_at="2026-01-01T00:00:00.000Z",
        payload={"nested": {"items": [1, 2]}},
    )

    with pytest.raises(TypeError):
        event.payload["new"] = True
    with pytest.raises(TypeError):
        event.payload["nested"]["new"] = True

    restored = RunEvent.from_dict(event.as_dict())
    assert restored.as_dict() == event.as_dict()
    assert restored.schema_version == 1


def test_run_event_stream_assigns_monotonic_sequence_to_all_sinks():
    captured = []
    stream = RunEventStream("run-1", sinks=[captured.append])

    first = stream.emit(EventType.RUN_STARTED)
    second = stream.emit(EventType.ROUTE_SELECTED, {"lane": "fast"})

    assert [event.sequence for event in captured] == [1, 2]
    assert first.sequence == 1
    assert second.sequence == 2
    assert stream.last_sequence == 2


def test_event_sequence_is_never_reused_after_partial_sink_failure():
    persisted = []
    should_fail = {1}

    def failing_observer(event):
        if event.sequence in should_fail:
            should_fail.remove(event.sequence)
            raise RuntimeError("observer failed")

    stream = RunEventStream("run-1", sinks=[persisted.append, failing_observer])
    with pytest.raises(RuntimeError):
        stream.emit("test.first")
    second = stream.emit("test.second")

    assert [event.sequence for event in persisted] == [1, 2]
    assert second.sequence == 2
    assert stream.last_sequence == 2


@pytest.mark.parametrize("invalid", [{"value": object()}, {"value": math.nan}])
def test_event_payload_rejects_non_json_values(invalid):
    with pytest.raises(EventValidationError):
        RunEvent.create(
            run_id="run-1",
            sequence=1,
            type="test.invalid",
            payload=invalid,
        )


def test_event_replay_rejects_incomplete_identity_instead_of_regenerating_it():
    with pytest.raises(EventValidationError):
        RunEvent.from_dict(
            {
                "run_id": "run-1",
                "sequence": 1,
                "type": "run.started",
                "occurred_at": "2026-01-01T00:00:00Z",
                "payload": {},
            }
        )


def test_direct_event_constructor_cannot_bypass_payload_validation():
    with pytest.raises(EventValidationError):
        RunEvent(
            event_id="event",
            run_id="run",
            sequence=1,
            type="test.invalid",
            occurred_at="2026-01-01T00:00:00Z",
            payload={"invalid": object()},
        )
