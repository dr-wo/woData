"""Reducer projection provenance follows events, never accepted-lap counts."""
from dataclasses import asdict
from datetime import datetime, timezone
import json

import pytest

from wodata.live import LiveTimingMessage, QualiRawDriverState, QualiSessionStore
from wodata.live.quali_models import QualiCompletedLap


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def apply(store, patch, seconds, topic="TimingData"):
    payload = {"Lines": {"31": patch}} if topic == "TimingData" else patch
    return store.apply(LiveTimingMessage(topic, payload, NOW, NOW,
                                        session_time_seconds=float(seconds)))


def complete(store, counter=3, seconds=100, best_lap=2, sectors=True):
    patch = {"NumberOfLaps": counter, "InPit": False,
             "LastLapTime": {"Value": "1:38.879"},
             "BestLapTime": {"Value": "1:38.879", "Lap": best_lap}}
    if sectors:
        patch["Sectors"] = {str(i): {"Value": value} for i, value in
                            enumerate(("25.488", "32.895", "40.496"))}
    return apply(store, patch, seconds)


def test_distinct_count_boundary_and_official_lap_preserve_existing_event_key():
    snapshot = complete(QualiSessionStore())
    driver, event = snapshot.drivers[0], snapshot.completed_lap_sequence[0]
    assert (driver.completed_laps, event.lap_number, event.official_lap_number) == (1, 3, 2)
    assert driver.best_lap_event_key == event.event_key == ("Q1", "31", 3, 100.0)
    assert event.sectors_s == (25.488, 32.895, 40.496)


@pytest.mark.parametrize("late_sector", [False, True])
def test_sectors_before_and_after_completion_have_same_event_owner(late_sector):
    store = QualiSessionStore()
    apply(store, {"NumberOfLaps": 2}, 0)
    apply(store, {"Sectors": {"0": {"Value": "25.488"}}}, 26)
    apply(store, {"Sectors": {"1": {"Value": "32.895"}}}, 59)
    patch = {"NumberOfLaps": 3, "InPit": False,
             "LastLapTime": {"Value": "1:38.879"}}
    if not late_sector:
        apply(store, {"Sectors": {"2": {"Value": "40.496"}}}, 99)
    before = apply(store, patch, 100)
    key = before.completed_lap_sequence[0].event_key
    if late_sector:
        assert before.completed_lap_sequence[0].sectors_s == (25.488, 32.895, None)
        apply(store, {"Sectors": {"2": {"Value": "40.496"}}}, 100.1)
    snapshot = apply(store, {"BestLapTime": {"Value": "1:38.879", "Lap": 2}}, 100.2)
    assert snapshot.drivers[0].best_lap_event_key == key
    assert snapshot.completed_lap_sequence[0].sectors_s == (25.488, 32.895, 40.496)
    # Previously published snapshots are immutable.
    if late_sector:
        assert before.completed_lap_sequence[0].sectors_s[2] is None


def test_official_best_before_recognition_binds_only_when_event_is_accepted():
    store = QualiSessionStore()
    early = apply(store, {"BestLapTime": {"Value": "1:38.879", "Lap": 2}}, 99)
    assert early.drivers[0].best_lap_event_key is None
    snapshot = complete(store)
    assert snapshot.drivers[0].best_lap_event_key == snapshot.completed_lap_sequence[0].event_key


def test_old_sectors_are_not_relabelled_by_a_new_completion_or_status_update():
    store = QualiSessionStore()
    original = complete(store)
    key = original.drivers[0].best_lap_event_key
    status = apply(store, {"Sectors": {"0": {"OverallFastest": False, "PreviousValue": "25.488"}},
                           "BestLapTime": {"OverallFastest": False}}, 101)
    assert status.drivers[0].best_lap_event_key == key
    assert status.completed_lap_sequence[0].sectors_s == original.completed_lap_sequence[0].sectors_s
    # Equal durations cannot turn carried-over sector values into event B.
    next_lap = complete(store, counter=6, seconds=200, best_lap=5, sectors=False)
    assert next_lap.drivers[0].best_lap_event_key == next_lap.completed_lap_sequence[-1].event_key
    assert next_lap.completed_lap_sequence[-1].sectors_s == (None, None, None)


@pytest.mark.parametrize("patch", [
    {"BestLapTime": {"Value": ""}},
    {"NumberOfLaps": 3, "LastLapTime": {"Value": ""}},
    {"NumberOfLaps": 3, "LastLapTime": {"Value": "1:40.000"}},
    {"NumberOfLaps": 2},
    {"BestLapTime": {"Value": "1:37.000", "Lap": 2}},
    {"BestLapTime": {"Value": "1:38.879", "Lap": 77}},
])
def test_retracted_or_wrong_event_remains_unassociated_on_status_only_packets(patch):
    store = QualiSessionStore()
    complete(store)
    snapshot = apply(store, patch, 101)
    assert snapshot.drivers[0].best_lap_event_key is None
    snapshot = apply(store, {"Sectors": {"2": {"PersonalFastest": False}}}, 102)
    assert snapshot.drivers[0].best_lap_event_key is None


def test_official_alias_can_restore_a_valid_previous_best_without_sector_reuse():
    store = QualiSessionStore()
    first = complete(store)
    key = first.drivers[0].best_lap_event_key
    complete(store, counter=6, seconds=200, best_lap=5)
    restored = apply(store, {"BestLapTime": {"Value": "1:38.879", "Lap": 2}}, 201)
    assert restored.drivers[0].best_lap_event_key == key
    assert restored.completed_lap_sequence[1].event_key != key


def test_unofficial_best_identity_survives_status_updates():
    store = QualiSessionStore()
    first = apply(store, {"NumberOfLaps": 3, "LastLapTime": {"Value": "1:30"},
                          "Sectors": [{"Value": "30"}] * 3}, 100)
    key = first.completed_lap_sequence[0].event_key
    assert first.drivers[0].best_lap_event_key == key
    status = apply(store, {"Position": 1}, 101)
    assert status.drivers[0].best_lap_event_key == key


def test_unassociated_untimed_last_lap_does_not_retract_an_accepted_best():
    store = QualiSessionStore()
    first = complete(store)
    key = first.drivers[0].best_lap_event_key
    # This is the real-feed shape of OCO's subsequent pit/in-lap publication.
    pit_lap = apply(store, {"InPit": True, "LastLapTime": {"Value": "2:04.739"}}, 225)
    assert pit_lap.drivers[0].best_lap_event_key == key
    assert pit_lap.completed_lap_sequence[0].sectors_s == first.completed_lap_sequence[0].sectors_s


def test_independent_best_sector_scoreboard_is_not_completed_lap_evidence():
    store = QualiSessionStore()
    complete(store, sectors=False)
    packet = {"Lines": {"31": {"BestSectors": {
        str(index): {"Value": value} for index, value in
        enumerate(("25.488", "32.895", "40.496"))
    }, "PersonalBestLapTime": {"Value": "1:38.879", "Lap": 2}}}}
    snapshot = apply(store, packet, 100.1, topic="TimingStats")
    assert snapshot.completed_lap_sequence[0].sectors_s == (None, None, None)


def test_changed_value_without_lap_cannot_inherit_deep_merged_old_alias():
    store = QualiSessionStore()
    complete(store)
    newer = apply(store, {"NumberOfLaps": 6, "LastLapTime": {"Value": "1:30"},
                          "BestLapTime": {"Value": "1:30"},
                          "Sectors": [{"Value": "30"}] * 3}, 200)
    key = newer.completed_lap_sequence[-1].event_key
    assert newer.drivers[0].best_lap_event_key == key
    assert newer.completed_lap_sequence[-1].official_lap_number is None
    status = apply(store, {"BestLapTime": {"OverallFastest": False}}, 201)
    assert status.drivers[0].best_lap_event_key == key
    # Lap 2 is known to belong to the first event, whose validated time differs.
    wrong = apply(store, {"BestLapTime": {"Value": "1:30", "Lap": 2}}, 202)
    assert wrong.drivers[0].best_lap_event_key is None


def test_retracted_event_alias_cannot_be_reassigned_to_an_equal_time_event():
    store = QualiSessionStore()
    complete(store)
    apply(store, {"NumberOfLaps": 3, "LastLapTime": {"Value": ""}}, 101)
    apply(store, {"NumberOfLaps": 6, "LastLapTime": {"Value": "1:38.879"}}, 200)
    wrong = apply(store, {"BestLapTime": {"Value": "1:38.879", "Lap": 2}}, 201)
    assert wrong.drivers[0].best_lap_event_key is None
    assert wrong.completed_lap_sequence[-1].official_lap_number is None


@pytest.mark.parametrize("part", ["Q2", "Q3"])
def test_part_transition_clears_identity_and_pending_sector_provenance(part):
    store = QualiSessionStore()
    previous = complete(store)
    cleared = apply(store, {"SessionPart": part}, 110, topic="SessionInfo")
    assert cleared.drivers[0].best_lap_event_key is None
    assert cleared.drivers[0].completed_laps == 0
    fresh = complete(store, counter=6, seconds=220, best_lap=2, sectors=False)
    event = fresh.completed_lap_sequence[-1]
    assert fresh.drivers[0].best_lap_event_key == event.event_key
    assert event.event_key[0] == part
    assert event.event_key != previous.drivers[0].best_lap_event_key
    assert event.sectors_s == (None, None, None)


def test_backward_defaults_and_snapshot_export_keep_identity_optional():
    assert QualiRawDriverState("31", "OCO", "Haas").best_lap_event_key is None
    event = QualiCompletedLap("Q1", "31", 3, 98.879, 100)
    assert event.sectors_s == (None, None, None)
    assert event.official_lap_number is None
    snapshot = complete(QualiSessionStore())
    exported = json.loads(json.dumps(asdict(snapshot.drivers[0])))
    assert tuple(exported["best_lap_event_key"]) == snapshot.completed_lap_sequence[0].event_key
