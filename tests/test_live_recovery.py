from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from wodata.live.models import LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.recovery import (
    discover_live_recordings, load_recording, merge_recordings,
    recover_replay_directory,
)
from wodata.live.replay.player import load_replay
from wodata.live.reducer import LiveSessionStore


BASE = datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)


def message(sequence, topic, payload):
    timestamp = BASE + timedelta(seconds=sequence)
    return LiveTimingMessage(topic, payload, timestamp, timestamp, sequence=sequence)


def test_legacy_and_envelope_recordings_merge_to_uninterrupted_state(tmp_path):
    common = [
        message(1, "DriverList", {"Lines": {"1": {"Tla": "AAA", "TeamName": "Team"}}}),
        message(2, "TimingAppData", {"Lines": {"1": {"Stints": {"0": {"Compound": "MEDIUM", "StartLaps": 0}}}}}),
        message(3, "TimingData", {"Lines": {"1": {"Position": "1", "NumberOfLaps": 1, "LastLapTime": {"Value": "1:30.000"}}}}),
    ]
    continuation = [
        message(4, "TimingData", {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "1:31.000"}}}}),
        message(5, "TimingData", {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "1:30.500"}}}}),
        message(6, "TimingData", {"Lines": {"1": {"NumberOfLaps": 3, "LastLapTime": {"Value": "1:32.000"}}}}),
    ]
    legacy = tmp_path / "live_timing_raw.txt"
    legacy.write_text(
        "".join(
            str([item.topic, item.payload, item.source_timestamp_utc.isoformat()]) + "\n"
            for item in common + continuation[:1]
        ),
        encoding="utf-8",
    )
    envelope = tmp_path / "live_timing_raw_2.jsonl"
    recorder = LiveTimingRecorder(envelope)
    for item in common + continuation:
        recorder.write(item)
    recorder.close()

    merged = merge_recordings([load_recording(legacy), load_recording(envelope)])
    recovered = LiveSessionStore()
    recovered.apply_many([item.message for item in merged])
    uninterrupted = LiveSessionStore()
    uninterrupted.apply_many(common + continuation)

    recovered_laps = [
        (lap.driver_number, lap.lap_number, lap.lap_time_s, lap.compound, lap.stint)
        for lap in recovered.snapshot().completed_laps
    ]
    uninterrupted_laps = [
        (lap.driver_number, lap.lap_number, lap.lap_time_s, lap.compound, lap.stint)
        for lap in uninterrupted.snapshot().completed_laps
    ]
    assert recovered_laps == uninterrupted_laps
    assert recovered_laps[-2][2] == 90.5


def test_envelope_preserves_recovery_metadata(tmp_path):
    path = tmp_path / "live_timing_raw.jsonl"
    original = LiveTimingMessage(
        "Heartbeat", {"Utc": "2026-08-23T13:00:00Z"}, BASE, BASE,
        source="synthetic_test", sequence=42, session_time_seconds=123.5,
    )
    recorder = LiveTimingRecorder(path)
    recorder.write(original)
    recorder.close()

    payload = json.loads(path.read_text(encoding="utf-8"))
    loaded = load_recording(path)[0].message
    assert payload["recording_schema_version"] == 2
    assert loaded.sequence == 42
    assert loaded.received_at_utc == BASE
    assert loaded.session_time_seconds == 123.5
    assert loaded.payload == original.payload


def test_multiple_envelopes_keep_source_order_and_later_correction(tmp_path):
    shared = [
        message(1, "DriverList", {"1": {"Tla": "AAA", "TeamName": "Team"}}),
        message(2, "TimingData", {"Lines": {"1": {
            "NumberOfLaps": 1, "LastLapTime": {"Value": "1:30.000"}
        }}}),
    ]
    first = tmp_path / "live_timing_raw.jsonl"
    second = tmp_path / "live_timing_raw_2.jsonl"
    recorder = LiveTimingRecorder(first)
    for item in shared + [message(4, "TimingData", {"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": "1:31.000"}
    }}})]:
        recorder.write(item)
    recorder.close()
    recorder = LiveTimingRecorder(second)
    for item in shared + [message(5, "TimingData", {"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": "1:30.500"}
    }}})]:
        recorder.write(item)
    recorder.close()

    merged = merge_recordings([load_recording(first), load_recording(second)])
    for path in (first, second):
        orders = [item.source_order for item in merged if item.path == path]
        assert orders == sorted(orders)
    store = LiveSessionStore()
    store.apply_many([item.message for item in merged])
    laps = store.snapshot().completed_laps
    assert [(lap.lap_number, lap.lap_time_s) for lap in laps] == [
        (1, 90.0), (2, 90.5)
    ]


def test_directory_recovery_materializes_reuses_and_rebuilds_canonical(tmp_path):
    identity = message(0, "SessionInfo", {
        "Name": "Race", "StartDate": "2026-08-23T13:00:00Z",
        "Meeting": {"RoundNumber": 12},
    })
    for index, values in enumerate(((identity, message(1, "LapCount", {"CurrentLap": 1})),
                                    (identity, message(2, "LapCount", {"CurrentLap": 2}))), 1):
        path = tmp_path / ("live_timing_raw.txt" if index == 1 else f"live_timing_raw_{index}.txt")
        path.write_text("".join(
            str([item.topic, item.payload, item.source_timestamp_utc.isoformat()]) + "\n"
            for item in values
        ), encoding="utf-8")

    first = recover_replay_directory(tmp_path, year=2026, round_number=12, session="R")
    assert not first.reused
    assert len(load_replay(first.path)[0]) == first.report.merged_messages
    assert first.path not in discover_live_recordings(tmp_path)
    second = recover_replay_directory(tmp_path, year=2026, round_number=12, session="R")
    assert second.reused
    source = tmp_path / "live_timing_raw_2.txt"
    source.write_text(source.read_text(encoding="utf-8") + str([
        "LapCount", {"CurrentLap": 3}, (BASE + timedelta(seconds=3)).isoformat()
    ]) + "\n", encoding="utf-8")
    third = recover_replay_directory(tmp_path, year=2026, round_number=12, session="R")
    assert not third.reused
