from datetime import datetime, timezone

from wodata.live import (
    LiveTimingMessage, PitLaneGeometry, QualiPitState, QualiSessionStore,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def msg(topic, payload, seconds=0):
    return LiveTimingMessage(topic, payload, NOW, NOW, session_time_seconds=float(seconds))


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

    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}))

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
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}))
    assert snapshot.session_part == "Q2"


def test_sprint_qualifying_metadata_and_clock_reset_use_sq_parts():
    store = QualiSessionStore()
    snapshot = store.apply(
        msg("SessionInfo", {"Name": "Sprint Qualifying", "Type": "Qualifying"})
    )
    assert snapshot.session_part == "SQ1"

    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03", "Extrapolating": True}))
    store.apply(msg("Heartbeat", {}, seconds=4))
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "10:00"}))
    assert snapshot.session_part == "SQ2"
