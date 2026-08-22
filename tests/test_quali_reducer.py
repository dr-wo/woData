from datetime import datetime, timezone

from wodata.live import (
    LiveTimingMessage, PitLaneGeometry, QualiPitState, QualiSessionStore,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def msg(topic, payload, seconds=0):
    return LiveTimingMessage(topic, payload, NOW, NOW, session_time_seconds=float(seconds))


def timing(in_pit=True):
    return msg("TimingData", {"Lines": {"1": {"Position": "1", "InPit": str(in_pit).lower()}}})


def car(rpm, seconds):
    return msg("CarData.z", {"Entries": [{"Cars": {"1": {"Channels": {"0": rpm, "2": 0, "3": 0}}}}]}, seconds)


def position(x, y, seconds, status="OnTrack"):
    return msg("Position.z", {"Position": [{"Entries": {"1": {"X": x, "Y": y, "Z": 0, "Status": status}}}]}, seconds)


def test_fresh_rpm_in_garage_and_stale_rpm():
    store = QualiSessionStore(telemetry_freshness_s=5)
    store.apply(msg("DriverList", {"1": {"Tla": "AAA"}})); store.apply(timing())
    assert store.apply(car(2000, 10)).drivers[0].pit_state == QualiPitState.FIRED_UP
    # Another driver's newer sample advances freshness time; old non-zero RPM becomes stale.
    store.apply(msg("CarData.z", {"Entries": [{"Cars": {"2": {"Channels": {"0": 1000}}}}]}, 20))
    states = {d.driver_number: d for d in store.snapshot().drivers}
    assert states["1"].pit_state == QualiPitState.INACTIVE


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
    assert states["AAA"].pit_state == states["BBB"].pit_state == QualiPitState.QUEUE
    assert states["BBB"].pit_lane_progress > states["AAA"].pit_lane_progress
    snapshot = store.apply(msg("TimingData", {"Lines": {"2": {"InPit": "false"}}}))
    bbb = next(d for d in snapshot.drivers if d.driver_code == "BBB")
    assert bbb.pit_state == QualiPitState.ON_TRACK


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


def test_clock_reset_advances_qualifying_part():
    store = QualiSessionStore()
    store.apply(msg("ExtrapolatedClock", {"Remaining": "0:03"}))
    snapshot = store.apply(msg("ExtrapolatedClock", {"Remaining": "15:00"}))
    assert snapshot.session_part == "Q2"
