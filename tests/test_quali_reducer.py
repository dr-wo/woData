from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import (
    LiveTimingMessage, PitLaneGeometry, QualiPitState, QualiSessionStore,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def msg(topic, payload, seconds=0):
    return LiveTimingMessage(topic, payload, NOW, NOW, session_time_seconds=float(seconds))


def feed(topic, payload, seconds=0, *, snapshot=False, source="signalr_live"):
    utc = NOW + timedelta(seconds=seconds)
    return LiveTimingMessage(topic, payload, None if snapshot else utc, utc, source=source)


def flag(kind, seconds):
    return {"Utc": (NOW + timedelta(seconds=seconds)).isoformat(),
            "Flag": kind, "Scope": "Track"}


@pytest.mark.parametrize("part", [1, 2, 3])
@pytest.mark.parametrize("source", ["signalr_live", "f1_archive_replay"])
def test_timing_session_part_bootstraps_mid_session_without_resetting_clock(part, source):
    store = QualiSessionStore()
    store.apply(feed("SessionStatus", {"Status": "Started"}, 100, snapshot=True, source=source))
    store.apply(feed("ExtrapolatedClock", {
        "Utc": (NOW + timedelta(seconds=100)).isoformat(),
        "Remaining": "08:32", "Extrapolating": True,
    }, 100, snapshot=True, source=source))
    raw = store.apply(feed("TimingData", {"SessionPart": part, "Lines": {
        "1": {"Position": 1, "NumberOfLaps": 12, "BestLapTime": {"Value": "90"}},
    }}, 100, snapshot=True, source=source))
    assert raw.session_part == f"Q{part}"
    assert raw.remaining_time_s == 512 and raw.clock_running
    assert raw.session_part_established
    assert not raw.normal_finished
    assert raw.drivers[0].historical_results == ()
    assert store._segment_start_time_s is None


def test_numeric_part_progression_repetition_and_stale_metadata():
    store = QualiSessionStore()
    for part, start, duration in ((1, 0, "18:00"), (2, 1200, "15:00"), (3, 2160, "13:00")):
        raw = store.apply(msg("TimingData", {"SessionPart": part}, start))
        assert raw.session_part == f"Q{part}" and not raw.normal_finished
        store.apply(msg("ExtrapolatedClock", {"Remaining": duration, "Extrapolating": True}, start))
        # Redelivery is state, never an additional transition or clock reset.
        again = store.apply(msg("TimingData", {"SessionPart": part}, start + 1))
        assert again.session_part == f"Q{part}" and again.remaining_time_s > 0
        finished = store.apply(msg("SessionStatus", {"Status": "Finished"}, start + 1000))
        repeated = store.apply(msg("TimingData", {"SessionPart": part}, start + 1001))
        assert finished.normal_finished and repeated.normal_finished
    stale = store.apply(msg("TimingData", {"SessionPart": 2, "Lines": {
        "1": {"BestLapTime": {"Value": "80"}},
    }}, 1200))
    assert stale.session_part == "Q3" and stale.drivers == ()
    assert store.apply(msg("SessionInfo", {"SessionPart": "Q1"}, 0)).session_part == "Q3"
    assert store.apply(msg("SessionInfo", {"Name": "Qualifying"}, 3200)).session_part == "Q3"


def test_clock_fallback_remains_available_after_one_explicit_part():
    store = QualiSessionStore()
    store.apply(msg("TimingData", {"SessionPart": 2}))
    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03", "Extrapolating": True}))
    store.apply(msg("Heartbeat", {}, 4))
    raw = store.apply(msg("ExtrapolatedClock", {"Remaining": "13:00"}, 5))
    assert raw.session_part == "Q3" and not raw.normal_finished


@pytest.mark.parametrize("with_part", [False, True])
@pytest.mark.parametrize("mapping", [False, True])
def test_historical_chequered_followed_by_green_is_not_current_finish(with_part, mapping):
    store = QualiSessionStore()
    if with_part:
        store.apply(feed("TimingData", {"SessionPart": 2}, 200, snapshot=True))
    entries = [flag("GREEN", 0), flag("CHEQUERED", 100), flag("GREEN", 150)]
    # Ownership comes from each item's Utc, including out-of-order lists/maps.
    entries = {str(i): item for i, item in enumerate(reversed(entries))} if mapping else list(reversed(entries))
    snapshot = feed("RaceControlMessages", {"Messages": entries}, 200, snapshot=True)
    assert not store.apply(snapshot).normal_finished
    assert not store.apply(snapshot).normal_finished
    # Delayed and duplicate historical records cannot replace the newer green.
    assert not store.apply(feed("RaceControlMessages", {"Messages": {"8": flag("CHEQUERED", 100)}}, 210)).normal_finished
    genuine = store.apply(feed("RaceControlMessages", {"Messages": {"9": flag("CHEQUERED", 300)}}, 300))
    assert genuine.normal_finished and genuine.normal_finish_source == "race_control"
    assert store.apply(snapshot).normal_finished


def test_finished_mid_session_snapshot_and_transition_reject_prior_segment_flags():
    store = QualiSessionStore()
    store.apply(feed("TimingData", {"SessionPart": 2}, 200, snapshot=True))
    history = {"Messages": [flag("GREEN", 150), flag("CHEQUERED", 300)]}
    assert store.apply(feed("RaceControlMessages", history, 310, snapshot=True)).normal_finished
    # A real transition resets finish even before Q3 green arrives.
    raw = store.apply(feed("TimingData", {"SessionPart": 3}, 400))
    assert raw.session_part == "Q3" and not raw.normal_finished
    assert not store.apply(feed("RaceControlMessages", history, 410, snapshot=True)).normal_finished
    assert not store.apply(feed("RaceControlMessages", {"Messages": {"9": flag("CHEQUERED", 350)}}, 411)).normal_finished
    assert store.apply(feed("RaceControlMessages", {"Messages": {"10": flag("CHEQUERED", 600)}}, 600)).normal_finished


def test_positive_current_clock_rejects_old_finish_without_green_history():
    store = QualiSessionStore()
    store.apply(feed("ExtrapolatedClock", {
        "Utc": (NOW + timedelta(seconds=200)).isoformat(),
        "Remaining": "08:00", "Extrapolating": True,
    }, 200, snapshot=True))
    raw = store.apply(feed("RaceControlMessages", {"Messages": [flag("CHEQUERED", 100)]}, 200, snapshot=True))
    assert not raw.normal_finished


def test_undated_snapshot_flag_does_not_use_receive_time_as_freshness():
    store = QualiSessionStore()
    raw = store.apply(feed("RaceControlMessages", {"Messages": [{"Flag": "CHEQUERED"}]}, 200, snapshot=True))
    assert not raw.normal_finished


def test_duplicate_legacy_flag_keeps_original_publication_across_part_transition():
    store = QualiSessionStore()
    packet = {"Messages": {"1": {"Flag": "CHEQUERED", "Scope": "Track"}}}
    assert store.apply(msg("RaceControlMessages", packet, 100)).normal_finished
    store.apply(msg("TimingData", {"SessionPart": 2}, 150))
    # Establish an observed part before the real Q2 -> Q3 transition.
    store.apply(msg("TimingData", {"SessionPart": 3}, 200))
    assert not store.apply(msg("RaceControlMessages", packet, 300)).normal_finished


def test_numeric_sprint_part_uses_session_family():
    store = QualiSessionStore()
    store.apply(msg("SessionInfo", {"Name": "Sprint Qualifying"}))
    assert store.apply(msg("TimingData", {"SessionPart": 2})).session_part == "SQ2"


def test_historical_finish_received_before_current_clock_is_reconciled():
    store = QualiSessionStore()
    assert store.apply(feed("RaceControlMessages", {"Messages": [flag("CHEQUERED", 100)]}, 200, snapshot=True)).normal_finished
    raw = store.apply(feed("ExtrapolatedClock", {
        "Utc": (NOW + timedelta(seconds=200)).isoformat(),
        "Remaining": "08:00", "Extrapolating": True,
    }, 200, snapshot=True))
    assert not raw.normal_finished


def test_delayed_transition_preserves_already_received_current_part_finish():
    store = QualiSessionStore()
    store.apply(feed("TimingData", {"SessionPart": 2}, 200))
    store.apply(feed("RaceControlMessages", {"Messages": [flag("GREEN", 400), flag("CHEQUERED", 600)]}, 610))
    raw = store.apply(feed("TimingData", {"SessionPart": 3}, 390))
    assert raw.session_part == "Q3" and raw.normal_finished


def test_prior_flags_stay_prior_when_archive_utc_mapping_is_unavailable():
    store = QualiSessionStore()
    store.apply(msg("TimingData", {"SessionPart": 2}, 200))
    packet = {"Messages": {"1": flag("CHEQUERED", 300)}}
    assert store.apply(msg("RaceControlMessages", packet, 300)).normal_finished
    assert not store.apply(msg("TimingData", {"SessionPart": 3}, 400)).normal_finished
    assert not store.apply(msg("RaceControlMessages", packet, 410)).normal_finished
    assert store.apply(msg("RaceControlMessages", {"Messages": {"2": flag("CHEQUERED", 600)}}, 600)).normal_finished


@pytest.mark.parametrize("part", [0, 4, True, "", "unknown"])
def test_insufficient_part_metadata_leaves_clock_fallback_available(part):
    store = QualiSessionStore()
    assert store.apply(msg("TimingData", {"SessionPart": part})).session_part == "Q1"
    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03", "Extrapolating": True}))
    assert store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}, 4)).session_part == "Q2"


def timing(in_pit=True):
    return msg("TimingData", {"Lines": {"1": {"Position": "1", "InPit": str(in_pit).lower()}}})


def car(rpm, seconds, *, speed=0):
    return msg(
        "CarData.z",
        {"Entries": [{"Cars": {"1": {"Channels": {
            "0": rpm, "2": speed, "3": 0
        }}}}]},
        seconds,
    )


def position(x, y, seconds, status="OnTrack"):
    return msg("Position.z", {"Position": [{"Entries": {"1": {"X": x, "Y": y, "Z": 0, "Status": status}}}]}, seconds)


def test_position_packet_preserves_each_causal_traffic_observation():
    store = QualiSessionStore(pit_lane_geometry=PitLaneGeometry(((0, 0), (10, 0)), 2))
    packet = {"Position": [
        {"Timestamp": f"2026-01-01T00:00:0{index}Z",
         "Entries": {"1": {"X": x, "Y": 0, "Status": "OnTrack"}}}
        for index, x in enumerate((1, 20, 21))
    ]}
    snapshot = store.apply(msg("Position.z", packet, 2))
    assert len(snapshot.position_observations) == 3
    assert [item.pit_side for item in snapshot.position_observations] == [True, False, False]
    assert [item.session_time_s for item in snapshot.position_observations] == sorted(
        item.session_time_s for item in snapshot.position_observations
    )
    assert not store.apply(timing(False)).position_observations


def test_timing_app_stint_index_persists_across_qualifying_parts():
    store = QualiSessionStore()
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}}))
    first = store.apply(msg("TimingAppData", {"Lines": {"1": {"Stints": {
        "0": {"Compound": "MEDIUM", "New": "true", "StartLaps": 0},
    }}}}))
    assert first.drivers[0].tyre_history[0].stint_index == 0
    store._set_part("Q2")
    second = store.apply(msg("TimingAppData", {"Lines": {"1": {"Stints": {
        "1": {"Compound": "SOFT", "New": "true", "StartLaps": 0},
    }}}}))
    assert [(tyre.stint_index, tyre.compound) for tyre in second.drivers[0].tyre_history] == [
        (0, "MEDIUM"), (1, "SOFT"),
    ]


def test_position_only_placeholders_do_not_create_historical_driver_rows():
    store = QualiSessionStore()
    store.apply(msg("DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"}}))
    snapshot = store.apply(msg("Position.z", {"Position": [{"Entries": {
        "1": {"X": 10, "Y": 20, "Status": "OnTrack"},
        "15": {"X": 0, "Y": 0, "Status": "OnTrack"},
    }}]}, 1))
    assert [(driver.driver_number, driver.driver_code, driver.team) for driver in snapshot.drivers] == [
        ("1", "AAA", "Team"),
    ]
    # Timing evidence still permits a genuine late, unidentified participant.
    snapshot = store.apply(msg("TimingData", {"Lines": {"15": {"Position": 22}}}))
    unknown = next(driver for driver in snapshot.drivers if driver.driver_number == "15")
    assert unknown.driver_code == "15" and unknown.team == ""


def test_fresh_rpm_in_garage_and_stale_rpm():
    store = QualiSessionStore(telemetry_freshness_s=5)
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}})); store.apply(timing())
    assert store.apply(car(2000, 10)).drivers[0].pit_state == QualiPitState.FIRE_UP
    # Another driver's newer sample advances freshness time; old non-zero RPM becomes stale.
    store.apply(msg("CarData.z", {"Entries": [{"Cars": {"2": {"Channels": {"0": 1000}}}}]}, 20))
    states = {d.driver_number: d for d in store.snapshot().drivers}
    assert states["1"].pit_state == QualiPitState.NOT_ACTIVE


def test_pit_lane_queue_physical_order_then_pit_exit_on_track():
    geometry = PitLaneGeometry(((0, 0), (50, 20), (100, 20)), maximum_distance=10)
    store = QualiSessionStore(pit_lane_geometry=geometry)
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}, "2": {"Tla": "BBB"}}))
    store.apply(msg("TimingData", {"Lines": {"1": {"InPit": "true"}, "2": {"InPit": "true"}}}))
    store.apply(msg("Position.z", {"Position": [{"Entries": {
        "1": {"X": 30, "Y": 12, "Z": 0, "Status": "OnTrack"},
        "2": {"X": 80, "Y": 20, "Z": 0, "Status": "OnTrack"},
    }}]}, 5))
    states = {d.driver_code: d for d in store.snapshot().drivers}
    assert states["AAA"].pit_state == states["BBB"].pit_state == QualiPitState.NOT_ACTIVE
    assert states["BBB"].pit_lane_progress > states["AAA"].pit_lane_progress
    # TimingData can report on-track before the car physically leaves pit lane.
    snapshot = store.apply(msg("TimingData", {"Lines": {"2": {"InPit": "false"}}}))
    bbb = next(d for d in snapshot.drivers if d.driver_code == "BBB")
    assert bbb.pit_state == QualiPitState.PIT_LANE
    snapshot = store.apply(
        msg(
            "Position.z",
            {"Position": [{"Entries": {"2": {
                "X": 200, "Y": 200, "Z": 0, "Status": "OnTrack"
            }}}]},
            6,
        )
    )
    bbb = next(d for d in snapshot.drivers if d.driver_code == "BBB")
    assert bbb.pit_state == QualiPitState.ON_TRACK


def test_speed_fallback_latches_queue_until_car_has_left_pit_lane():
    store = QualiSessionStore(pit_exit_speed_kph=100)
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}}))
    store.apply(timing(in_pit=True))
    assert store.apply(car(2000, 1, speed=0)).drivers[0].pit_state == (
        QualiPitState.FIRE_UP
    )
    assert store.apply(car(3000, 2, speed=20)).drivers[0].pit_state == (
        QualiPitState.PIT_LANE
    )

    store.apply(timing(in_pit=False))
    assert store.apply(car(4000, 3, speed=60)).drivers[0].pit_state == (
        QualiPitState.PIT_LANE
    )
    assert store.apply(car(8000, 4, speed=120)).drivers[0].pit_state == (
        QualiPitState.ON_TRACK
    )


def test_inbound_car_transitions_from_inlap_to_in_pit():
    geometry = PitLaneGeometry(((0, 0), (100, 0)), maximum_distance=10)
    store = QualiSessionStore(pit_lane_geometry=geometry)
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}}))
    store.apply(timing(in_pit=False))
    store.apply(position(200, 100, 1))

    snapshot = store.apply(timing(in_pit=True))
    assert snapshot.drivers[0].pit_state == QualiPitState.IN_LAP

    snapshot = store.apply(position(50, 0, 2))
    assert snapshot.drivers[0].pit_state == QualiPitState.PIT_LANE


def test_timing_app_data_uses_real_tyre_age_at_stint_start():
    store = QualiSessionStore()
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}}))
    snapshot = store.apply(
        msg(
            "TimingAppData",
            {"Lines": {"1": {"Stints": {
                "0": {
                    "Compound": "SOFT",
                    "New": "true",
                    "StartLaps": 0,
                    "TotalLaps": 2,
                },
                "1": {"Compound": "MEDIUM", "New": "false", "StartLaps": 3},
            }}}},
        )
    )

    assert [
        (stint.compound, stint.start_laps, stint.fresh)
        for stint in snapshot.drivers[0].tyre_history
    ] == [("SOFT", 0, True), ("MEDIUM", 3, False)]


def test_clock_sectors_segments_and_best_lap_reduction():
    store = QualiSessionStore()
    store.apply(msg("ExtrapolatedClock", {"Remaining": "12:34", "Extrapolating": True}))
    store.apply(msg("DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"}}))
    snapshot = store.apply(msg("TimingData", {"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": "1:30"},
        "Sectors": [{"Value": "30"}, {"Value": "31"}, {"Value": "29"}],
    }}}))
    assert snapshot.remaining_time_s == 754
    assert snapshot.drivers[0].best_lap_s == 90
    assert snapshot.drivers[0].sectors_s == (30, 31, 29)


def test_running_domain_clock_only_advances_on_messages(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    store = QualiSessionStore()
    store.apply(
        msg("ExtrapolatedClock", {"Remaining": "15:00", "Extrapolating": True})
    )

    now[0] += 7.9

    assert store.snapshot().remaining_time_s == 900
    assert store.apply(msg("Heartbeat", {}, seconds=7.9)).remaining_time_s == 893


def test_accelerated_replay_clock_counts_down_by_session_time():
    store = QualiSessionStore()
    store.apply(
        msg(
            "ExtrapolatedClock",
            {"Remaining": "15:00", "Extrapolating": True},
            seconds=100,
        )
    )

    snapshot = store.apply(msg("Heartbeat", {}, seconds=107.9))

    assert snapshot.remaining_time_s == 893


def test_clock_reset_uses_extrapolated_zero_to_advance_part(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    store = QualiSessionStore()
    store.apply(
        msg("ExtrapolatedClock", {"Remaining": "0:05", "Extrapolating": True})
    )
    now[0] += 6
    store.apply(msg("Heartbeat", {}, seconds=6))

    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}, 7))

    assert snapshot.session_part == "Q2"


def test_late_live_connection_at_15_minutes_stays_in_q1():
    store = QualiSessionStore()
    store.apply(msg("SessionInfo", {"Name": "Qualifying", "Type": "Qualifying"}))
    snapshot = store.apply(
        LiveTimingMessage(
            "ExtrapolatedClock",
            {
                    "Utc": "2026-01-01T00:00:00Z",
                "Remaining": "15:00",
                "Extrapolating": True,
            },
            None,
            NOW,
        )
    )

    assert snapshot.session_part == "Q1"
    assert snapshot.remaining_time_s == 15 * 60


def test_late_q2_connection_requires_explicit_feed_part():
    store = QualiSessionStore()
    store.apply(msg("SessionInfo", {"Name": "Q2", "SessionPart": "Q2"}))
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00", "Extrapolating": True}))
    assert snapshot.session_part == "Q2"


def test_clock_reset_advances_qualifying_part():
    store = QualiSessionStore()
    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03", "Extrapolating": True}))
    store.apply(msg("Heartbeat", {}, seconds=4))
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}, 5))
    assert snapshot.session_part == "Q2"


def test_sprint_qualifying_metadata_and_clock_reset_use_sq_parts():
    store = QualiSessionStore()
    snapshot = store.apply(
        msg("SessionInfo", {"Name": "Sprint Qualifying", "Type": "Qualifying"})
    )
    assert snapshot.session_part == "SQ1"

    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03", "Extrapolating": True}))
    store.apply(msg("Heartbeat", {}, seconds=4))
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "10:00"}, 5))
    assert snapshot.session_part == "SQ2"
