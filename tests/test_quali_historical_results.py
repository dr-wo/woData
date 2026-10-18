"""Completed-part results retain rollover clears, but honor result retractions."""

from datetime import datetime, timezone

import pytest

from wodata.live import LiveTimingMessage, QualiSessionStore


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def apply(store, topic, payload, seconds):
    return store.apply(LiveTimingMessage(
        topic, payload, None, NOW, source="f1_archive_replay",
        session_time_seconds=seconds,
    ))


def timing(store, patch, seconds=100, **metadata):
    return apply(store, "TimingData", {"Lines": {"1": patch}, **metadata}, seconds)


def start():
    store = QualiSessionStore()
    apply(store, "SessionInfo", {"Name": "Qualifying", "SessionPart": "Q1"}, 0)
    timing(store, {"NumberOfLaps": 1, "BestLapTime": {"Value": "90"},
                   "LastLapTime": {"Value": "90"}}, 100)
    return store


def complete(store):
    return apply(store, "SessionInfo", {"SessionPart": "Q2"}, 1200).drivers[0]


@pytest.mark.parametrize("replacement", ["93", "", None, "0"])
@pytest.mark.parametrize("after_finish", [False, True])
def test_corrected_or_deleted_best_is_the_completed_result(replacement, after_finish):
    store = start()
    if after_finish:
        apply(store, "SessionStatus", {"Status": "Finished"}, 1080)
    timing(store, {"BestLapTime": {"Value": replacement}}, 1100)
    # The next-part reset must not restore a deleted/obsolete best.
    timing(store, {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}},
           1199, SessionPart=2)
    result = complete(store)
    assert result.best_lap_s is None
    assert dict(result.historical_results)["Q1"] == (93 if replacement == "93" else None)


@pytest.mark.parametrize("metadata, finished", [
    ({"SessionPart": 2}, False),
    ({"SessionPart": "Q2"}, False),
    ({}, True),  # Existing implicit legacy feed contract after chequered.
])
def test_rollover_clear_preserves_completed_result(metadata, finished):
    store = start()
    if finished:
        apply(store, "SessionStatus", {"Status": "Finished"}, 1080)
    cleared = timing(store, {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}},
                     1199, **metadata)
    assert cleared.drivers[0].best_lap_s is None
    assert dict(complete(store).historical_results) == {"Q1": 90}


def test_status_only_best_metadata_does_not_erase_completed_result():
    store = start()
    timing(store, {"BestLapTime": {"PersonalFastest": False}}, 101)
    assert dict(complete(store).historical_results) == {"Q1": 90}


def test_best_and_last_deletion_before_chequered_remains_unavailable():
    store = start()
    timing(store, {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}}, 101)
    assert dict(complete(store).historical_results) == {"Q1": None}


def test_unofficial_result_history_is_preserved():
    store = QualiSessionStore()
    timing(store, {"NumberOfLaps": 1, "LastLapTime": {"Value": "90"}}, 100)
    assert dict(complete(store).historical_results) == {"Q1": 90}


@pytest.mark.parametrize("part", ["Q1", "Q2"])
@pytest.mark.parametrize("part_scope", ["name", "number"])
@pytest.mark.parametrize("replacement", ["", None, "0", "93"])
def test_explicit_current_part_correction_after_chequered_overrides_legacy_clear(
        part, part_scope, replacement):
    store = start()
    if part == "Q2":
        complete(store)
        timing(store, {"NumberOfLaps": 2, "BestLapTime": {"Value": "90"},
                       "LastLapTime": {"Value": "90"}}, 1300)
    apply(store, "SessionStatus", {"Status": "Finished"}, 1400)
    scope = part if part_scope == "name" else int(part[-1])
    deleted = timing(store, {"BestLapTime": {"Value": replacement},
                             "LastLapTime": {"Value": ""}}, 1410, SessionPart=scope)
    expected = 93 if replacement == "93" else None
    assert deleted.drivers[0].best_lap_s == expected
    next_part = f"Q{int(part[-1]) + 1}"
    timing(store, {"BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}},
           1450, SessionPart=next_part)
    result = apply(store, "SessionInfo", {"SessionPart": next_part}, 1500).drivers[0]
    assert dict(result.historical_results)[part] == expected
