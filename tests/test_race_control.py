from datetime import datetime, timezone

import pytest

from wodata.live import LiveConfig, LiveSessionStore, LiveTimingController, LiveTimingMessage


def message(payload, second=0, *, source="synthetic_test"):
    return LiveTimingMessage("RaceControlMessages", payload, None,
                             datetime(2030, 1, 1, tzinfo=timezone.utc),
                             source=source, session_time_seconds=second)


def test_sparse_patches_order_duplicates_missing_fields_and_snapshot_immutability():
    store = LiveSessionStore()
    late = {"Utc": "2026-10-11T13:33:18Z", "Lap": 55, "Category": "Other",
            "Message": "CAR 44 (HAM) TIME 1:40.977 DELETED - TRACK LIMITS AT TURN 2 LAP 54"}
    store.reduce(message({"Messages": {"8": late}}, 10))
    before = store.snapshot()
    store.reduce(message({"Messages": {"8": {"Status": "Updated", "Future": {"Values": [1, 2]}}}}, 12))
    # Retransmitted snapshot, duplicate source occurrence, and sparse unknown notice.
    store.reduce(message({"Messages": {"8": late, "9": late, "10": {"Message": "No event time"},
                                        "3": {"Utc": "2026-10-11T13:30:05Z", "Message": "5 SECOND TIME PENALTY FOR CAR 63"}}}, 11))
    result = store.snapshot()
    assert [event.event_id for event in result.race_control_events] == ["3", "8", "9", "10"]
    # Different fields are not discarded as a duplicate. Identical complete copies are.
    store.reduce(message({"Messages": {"11": late}}, 13))
    assert len(store.snapshot().race_control_events) == 4
    updated = result.race_control_events[1]
    assert updated.kind == "Lap time deleted"
    assert updated.driver_number == "44"
    assert updated.fields["Lap"] == 55
    assert "LAP 54" in updated.message
    assert "Status" not in before.race_control_events[0].fields
    assert updated.fields["Status"] == "Updated"
    assert result.race_control_events[0].kind == "Penalty"
    assert result.race_control_events[-1].utc is None
    with pytest.raises(TypeError):
        updated.fields["Future"]["Values"][0] = 4
    assert result.completed_laps == before.completed_laps == ()
    store.reset()
    assert store.snapshot().race_control_events == ()


def test_list_snapshot_corrections_and_old_updates():
    store = LiveSessionStore()
    store.reduce(message({"Messages": [{"Message": "CAR 23 LAP DELETED (PIT)", "Utc": "bad"}]}))
    store.reduce(message({"Messages": {"0": {"Message": "CAR 23 LAP TIME REINSTATED"}}}, 20))
    store.reduce(message({"Messages": {"0": {"Message": "CAR 23 LAP DELETED (PIT)", "Lap": 39}}}, 10))
    event, = store.snapshot().race_control_events
    assert event.kind == "Other"
    assert event.fields["Lap"] == 39
    assert event.utc is None
    store.reduce(message({"Messages": [None, 5]}))
    store.reduce(message({"Messages": None}))
    assert len(store.snapshot().race_control_events) == 1


def test_controller_race_control_change_is_independent_of_timing():
    controller = LiveTimingController()
    controller._enqueue_message(message({"Messages": {"0": {"Message": "DRIVE THROUGH PENALTY FOR CAR 12"}}}))
    result = controller.drain()
    assert result.changes == frozenset({"race_control"})
    assert result.snapshot.race_control_events[0].driver_number == "12"
    assert controller.drain().changes == frozenset()


def test_live_race_subscribes_to_race_control(tmp_path, monkeypatch):
    controller = LiveTimingController()
    monkeypatch.setattr(controller, "_recover_live_history", lambda *args: None)
    sources = []
    monkeypatch.setattr(controller, "_start_source", lambda source, **kwargs: sources.append(source))
    try:
        controller.start_live(LiveConfig(2026, 17, "R", data_root=tmp_path))
        assert sources[0].extra_topics == ("RaceControlMessages",)
    finally:
        controller.stop()
