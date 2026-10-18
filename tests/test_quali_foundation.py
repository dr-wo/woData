from copy import deepcopy
from datetime import datetime, timezone
import json
import logging

from wodata.live import QualiSessionStore, QualiPitState, PitLaneGeometry, LiveTimingMessage
from wodata.live.recorder import FeedIdentityRecorder, live_recording_path
from wodata.live.session_identity import parse_session_identity

NOW = datetime(2026, 9, 5, 14, tzinfo=timezone.utc)
IDENTITY = {"StartDate": "2026-09-05T16:00:00", "Name": "Qualifying",
            "Meeting": {"Name": "Italian Grand Prix", "Number": 13}}


def message(topic, payload, seconds=0):
    return LiveTimingMessage(topic, payload, None, NOW, source="f1_archive_replay", session_time_seconds=seconds)


def clock(remaining, seconds=0, running=False):
    return message("ExtrapolatedClock", {"Remaining": remaining, "Extrapolating": running}, seconds)


def lap(number, value, seconds=0):
    return message("TimingData", {"Lines": {"12": {"NumberOfLaps": number,
                   "LastLapTime": {"Value": value}, "BestLapTime": {"Value": value},
                   "Sectors": {"0": {"Value": "26.0"}}}}}, seconds)


def car(speed, rpm, seconds):
    return message("CarData.z", {"Entries": [{"Cars": {"12": {
        "Channels": {"0": rpm, "2": speed}}}}]}, seconds)


def test_session_number_is_never_the_event_round():
    identity = parse_session_identity({**IDENTITY, "Name": "Practice 3", "Number": 3})
    assert identity.round_number == 13
    assert identity.session_name == "Practice 3"
    assert parse_session_identity({"Number": 3}).round_number is None


def test_feed_identity_relocates_capture_and_keeps_provenance_without_overwrite(tmp_path, caplog):
    original = live_recording_path(year=2026, round_number=13, session="Q", data_root=tmp_path)
    original.parent.mkdir(parents=True)
    original.write_bytes(b"existing recording")
    recorder = FeedIdentityRecorder(year=2026, round_number=3, session="Q", data_root=tmp_path, logger=logging.getLogger(__name__))
    provisional = recorder.path
    recorder.write(message("Heartbeat", {}, 1))
    # Even an exact capture-name collision must be safe, including an empty file.
    collision = original.with_name(recorder.path.name)
    collision.touch()
    recorder.write(message("SessionInfo", IDENTITY, 2))
    recorder.write(message("Heartbeat", {}, 3))
    recorder.close()
    assert recorder.path.parent == original.parent
    assert recorder.path != collision
    assert original.read_bytes() == b"existing recording"
    assert collision.read_bytes() == b""
    assert not provisional.exists()
    assert not provisional.with_suffix(".identity.json").exists()
    assert len(recorder.path.read_text().splitlines()) == 3
    provenance = json.loads(recorder.path.with_suffix(".identity.json").read_text())
    assert provenance["provisional_identity"]["round_number"] == 3
    assert provenance["validated_identity"]["round_number"] == 13
    assert provenance["identity_mismatch"] is True
    assert "identity corrected" in caplog.text


def test_incomplete_feed_identity_remains_explicitly_provisional(tmp_path):
    recorder = FeedIdentityRecorder(year=2026, round_number=3, session="Q", data_root=tmp_path, logger=logging.getLogger(__name__))
    recorder.write(message("SessionInfo", {"Number": 3, "Name": "Practice 3"}))
    recorder.close()
    provenance = json.loads(recorder.path.with_suffix(".identity.json").read_text())
    assert provenance["validated_identity"] is None
    assert provenance["identity_status"] == "provisional_unresolved"


def test_segment_scoping_history_zero_and_13_minute_q3():
    store = QualiSessionStore()
    store.apply(message("SessionInfo", IDENTITY))
    store.apply(clock("00:00:00"))  # Pre-session zero is not chequered.
    assert not store.snapshot().normal_finished
    assert store.apply(clock("18:00", 1, running=True)).session_part == "Q1"
    store.apply(lap(3, "1:22.758", 100))
    zero = store.apply(clock("00:00:00", 1080))
    assert zero.remaining_time_s == 0 and zero.normal_finished
    q2 = store.apply(clock("15:00", 1200))
    assert q2.session_part == "Q2" and not q2.normal_finished
    assert q2.drivers[0].historical_results == (("Q1", 82.758),)
    store.apply(clock("14:59", 1201, running=True))
    store.apply(lap(6, "1:21.882", 1300))
    store.apply(clock("0:00", 2100))
    # R13 feed clears current fields just before the next clock reset.
    store.apply(message("TimingData", {"Lines": {"12": {
        "BestLapTime": {"Value": ""}, "LastLapTime": {"Value": ""}}}}, 2159))
    q3 = store.apply(clock("13:00", 2160))
    assert q3.session_part == "Q3" and q3.segment_duration_s == 780
    driver = q3.drivers[0]
    assert driver.best_lap_s is driver.last_lap_s is None
    assert driver.sectors_s == (None, None, None) and driver.segments == ()
    assert driver.completed_laps == 0
    assert driver.historical_results == (("Q1", 82.758), ("Q2", 81.882))
    # Pit-out increments must not turn an old last lap into a completion.
    store.apply(message("TimingData", {"Lines": {"12": {"NumberOfLaps": 7, "InPit": False}}}, 2161))
    assert store.snapshot().drivers[0].best_lap_s is None
    assert len(store.snapshot().completed_lap_sequence) == 2
    genuine = store.apply(lap(8, "1:22.100", 2260))
    assert genuine.drivers[0].best_lap_s == 82.1
    assert genuine.completed_lap_sequence[-1].session_part == "Q3"


def test_replay_bootstrap_zero_cannot_latch_finish_before_positive_countdown():
    store = QualiSessionStore()
    # Some archives carry a stale Started status and zero clock before Q1.
    store.apply(message("SessionStatus", {"Status": "Started"}))
    bootstrap = store.apply(clock("00:00:00"))
    assert not bootstrap.normal_finished

    store.apply(clock("18:00", 1, running=True))
    genuine_end = store.apply(clock("00:00:00", 1081))
    assert genuine_end.normal_finished
    assert genuine_end.normal_finish_source == "clock_zero"


def test_live_late_join_zero_keeps_existing_finish_semantics():
    store = QualiSessionStore()
    store.apply(LiveTimingMessage(
        "SessionStatus", {"Status": "Started"}, None, NOW,
        source="signalr_live", session_time_seconds=0,
    ))
    finished = store.apply(LiveTimingMessage(
        "ExtrapolatedClock", {"Remaining": "00:00:00"}, None, NOW,
        source="signalr_live", session_time_seconds=1,
    ))
    assert finished.normal_finished
    assert finished.normal_finish_source == "clock_zero"


def test_explicit_part_overrides_duration_defaults_and_red_flag_freezes():
    store = QualiSessionStore()
    store.apply(message("SessionInfo", {**IDENTITY, "SessionPart": "Q3"}))
    raw = store.apply(clock("14:00", running=True))
    assert raw.session_part == "Q3" and raw.segment_duration_s == 840
    store.apply(message("SessionStatus", {"Status": "Aborted"}, 20))
    raw = store.apply(message("Heartbeat", {}, 200))
    assert raw.remaining_time_s == 820 and not raw.normal_finished
    store.apply(message("SessionStatus", {"Status": "Started"}, 201))
    assert store.apply(message("Heartbeat", {}, 211)).remaining_time_s == 810


def test_snapshots_do_not_mutate_and_queue_clears_on_stationary_or_stale_evidence():
    messages = [message("TimingData", {"Lines": {"12": {"InPit": True}}}),
                car(4, 4000, 1), car(0, 0, 2), car(20, 4000, 3),
                message("Heartbeat", {}, 10)]
    states = []
    for polls in (0, 1, 20):
        store = QualiSessionStore()
        sequence = []
        for msg in messages:
            store._apply_message(msg)
            before = deepcopy({k: v for k, v in vars(store).items() if k != "_snapshot"})
            for _ in range(polls):
                store.snapshot()
            assert before == {k: v for k, v in vars(store).items() if k != "_snapshot"}
            sequence.append(store.snapshot().drivers[0].pit_state)
        states.append(sequence)
    assert states[0] == states[1] == states[2]
    assert states[0] == [QualiPitState.NOT_ACTIVE, QualiPitState.PIT_LANE,
                         QualiPitState.NOT_ACTIVE, QualiPitState.PIT_LANE,
                         QualiPitState.NOT_ACTIVE]


def test_stale_geometry_does_not_hold_departing_car_in_queue():
    store = QualiSessionStore(pit_lane_geometry=PitLaneGeometry(((0, 0), (100, 0)), 10))
    store.apply(message("TimingData", {"Lines": {"12": {"InPit": False}}}))
    queued = store.apply(message("Position.z", {"Position": [{"Entries": {"12": {"X": 50, "Y": 0}}}]}, 1))
    assert queued.drivers[0].pit_state == QualiPitState.NOT_ACTIVE
    stale = store.apply(message("Heartbeat", {}, 10))
    assert stale.drivers[0].pit_state == QualiPitState.ON_TRACK
    assert stale.drivers[0].pit_lane_progress is None


def test_explicit_chequered_is_session_level_even_before_clock_zero():
    store = QualiSessionStore()
    store.apply(clock("2:00", running=True))
    raw = store.apply(message("RaceControlMessages", {"Messages": {"0": {
        "Flag": "CHEQUERED", "Scope": "Track"}}}, 1))
    assert raw.normal_finished and raw.normal_finish_source == "race_control"
    assert raw.remaining_time_s == 119


def test_explicit_shortened_q1_takes_precedence_over_15_minute_inference():
    store = QualiSessionStore()
    store.apply(message("SessionInfo", {**IDENTITY, "SessionPart": "Q1"}))
    snapshot = store.apply(clock("15:00", running=True))
    assert snapshot.session_part == "Q1"
    assert snapshot.segment_duration_s == 900


def test_controller_qualifying_uses_feed_recorder_and_subscribes_chequered(tmp_path, monkeypatch):
    from wodata.live import LiveTimingController, LiveConfig
    controller = LiveTimingController(store=QualiSessionStore())
    sources = []
    monkeypatch.setattr(controller, "_start_source", lambda source, **kwargs: sources.append(source))
    controller.start_live(LiveConfig(2026, 3, "Q", tmp_path))
    assert "RaceControlMessages" in sources[0].extra_topics
    controller._enqueue_message(message("SessionInfo", IDENTITY))
    controller._enqueue_message(message("Heartbeat", {}, 1))
    controller.stop()
    assert "round=13" in str(controller.recording_path)
    assert len(controller.recording_path.read_text().splitlines()) == 2


def test_untimed_out_lap_cannot_populate_a_cleared_official_current_result():
    store = QualiSessionStore()
    store.apply(clock("18:00", running=True))
    store.apply(lap(6, "1:21.882", 1))
    store.apply(clock("0:00", 1080, running=True))
    store.apply(clock("15:00", 1100, running=True))
    store.apply(lap(9, "1:21.000", 1200))
    store.apply(clock("0:00", 2000, running=True))
    store.apply(clock("13:00", 2100, running=True))
    out = store.apply(message("TimingData", {"Lines": {"12": {
        "NumberOfLaps": 11, "LastLapTime": {"Value": "6:23.639"}}}}, 2200))
    assert out.drivers[0].best_lap_s is None
    assert dict(out.drivers[0].historical_results)["Q2"] == 81.0
