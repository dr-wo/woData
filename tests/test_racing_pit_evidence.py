"""Racing visit evidence must remain distinct from inferred tyre-change flags."""

from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage


def setup_store():
    store = LiveSessionStore()
    origin = datetime(2026, 10, 6, tzinfo=timezone.utc)

    def send(time, payload, topic="TimingData"):
        return store.apply(LiveTimingMessage(
            topic, payload, origin + timedelta(seconds=time), origin,
            session_time_seconds=float(time),
        ))

    send(0, {"Status": "Started"}, "SessionStatus")
    send(0, {"Status": "1"}, "TrackStatus")
    send(0, {"1": {"Tla": "AAA", "TeamName": "Team"}}, "DriverList")
    send(0, {"Lines": {"1": {"Stints": {"0": {
        "Compound": "MEDIUM", "StartLaps": 0, "New": "true",
    }}}}}, "TimingAppData")
    send(90, {"Lines": {"1": {"NumberOfLaps": 1, "LastLapTime": {"Value": 90}}}})
    return store, send


def finish_visit(send, *, compound="HARD", late_metadata=False):
    send(160, {"Lines": {"1": {"InPit": True}}})
    send(180, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 90}}}})
    patch = {"Lines": {"1": {"Stints": {"1": {
        "Compound": compound, "StartLaps": 0, "TotalLaps": 0, "New": "true",
    }}}}}
    if not late_metadata:
        send(181, patch, "TimingAppData")
    send(190, {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    send(280, {"Lines": {"1": {"NumberOfLaps": 3, "LastLapTime": {"Value": 100}}}})
    if late_metadata:
        send(281, patch, "TimingAppData")


@pytest.mark.parametrize("late_metadata", [False, True])
@pytest.mark.parametrize("compound", ["HARD", "MEDIUM"])
def test_explicit_racing_visit_identity_survives_late_metadata(compound, late_metadata):
    store, send = setup_store()
    finish_visit(send, compound=compound, late_metadata=late_metadata)
    laps = {lap.lap_number: lap for lap in store.snapshot().completed_laps}
    assert laps[1].racing_pit_stop_id is None
    assert laps[2].racing_pit_stop_id == laps[3].racing_pit_stop_id
    assert laps[2].racing_pit_stop_id > 0
    assert laps[2].pit_in and laps[3].pit_out
    assert laps[2].stint == 1 and laps[3].stint == 2


def test_stint_change_without_pit_visit_has_no_racing_evidence():
    store, send = setup_store()
    send(100, {"Status": "Aborted"}, "SessionStatus")
    send(110, {"Lines": {"1": {"Stints": {"1": {
        "Compound": "HARD", "StartLaps": 0, "New": "true",
    }}}}}, "TimingAppData")
    send(200, {"Status": "Started"}, "SessionStatus")
    send(290, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 90}}}})
    laps = store.snapshot().completed_laps
    assert laps[0].pit_in and laps[1].pit_out
    assert all(lap.racing_pit_stop_id is None for lap in laps)


@pytest.mark.parametrize("interruption", ["abort", "red_track"])
def test_mid_visit_red_flag_invalidates_racing_evidence(interruption):
    store, send = setup_store()
    send(160, {"Lines": {"1": {"InPit": True}}})
    send(180, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 90}}}})
    if interruption == "abort":
        send(182, {"Status": "Aborted"}, "SessionStatus")
        send(185, {"Status": "Started"}, "SessionStatus")
    else:
        send(182, {"Status": "5"}, "TrackStatus")
        send(185, {"Status": "1"}, "TrackStatus")
    send(190, {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    send(280, {"Lines": {"1": {"NumberOfLaps": 3, "LastLapTime": {"Value": 100}}}})
    assert all(lap.racing_pit_stop_id is None for lap in store.snapshot().completed_laps)


def test_exit_without_observed_entry_cannot_certify_racing_stop():
    store, send = setup_store()
    send(190, {"Lines": {"1": {"PitOut": True}}})
    send(280, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 100}}}})
    assert all(lap.racing_pit_stop_id is None for lap in store.snapshot().completed_laps)


def test_late_lap_duration_does_not_shift_visit_identity():
    store, send = setup_store()
    send(160, {"Lines": {"1": {"InPit": True}}})
    send(180, {"Lines": {"1": {"NumberOfLaps": 2}}})
    send(181, {"Lines": {"1": {"Stints": {"1": {
        "Compound": "HARD", "StartLaps": 0, "TotalLaps": 0, "New": "true",
    }}}}}, "TimingAppData")
    send(190, {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    send(191, {"Lines": {"1": {"LastLapTime": {"Value": 90}}}})
    send(280, {"Lines": {"1": {"NumberOfLaps": 3, "LastLapTime": {"Value": 100}}}})
    laps = {lap.lap_number: lap for lap in store.snapshot().completed_laps}
    assert laps[1].racing_pit_stop_id is None
    assert laps[2].racing_pit_stop_id == laps[3].racing_pit_stop_id
    assert laps[2].racing_pit_stop_id > 0
    assert laps[2].stint == 1 and laps[3].stint == 2


def test_entry_during_abort_exit_after_restart_is_not_a_racing_visit():
    store, send = setup_store()
    send(150, {"Status": "Aborted"}, "SessionStatus")
    send(160, {"Lines": {"1": {"InPit": True}}})
    send(185, {"Status": "Started"}, "SessionStatus")
    send(190, {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    send(280, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 100}}}})
    assert all(lap.racing_pit_stop_id is None for lap in store.snapshot().completed_laps)


def test_exit_while_aborted_is_not_a_racing_visit():
    store, send = setup_store()
    send(160, {"Lines": {"1": {"InPit": True}}})
    send(180, {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": 90}}}})
    send(185, {"Status": "Aborted"}, "SessionStatus")
    send(190, {"Lines": {"1": {"InPit": False, "PitOut": True}}})
    assert all(lap.racing_pit_stop_id is None for lap in store.snapshot().completed_laps)
