from pathlib import Path

from wodata.live import LiveSessionStore, ReplayLiveTimingSource
from wodata.live.replay.player import ReplayPlayer, load_replay


FIXTURE = Path(__file__).parent / "fixtures" / "live_phase_1.txt"


def test_replay_produces_expected_corrected_snapshot():
    store = LiveSessionStore()
    statuses = []
    ReplayLiveTimingSource(FIXTURE, speed=0).run(store.apply, statuses.append)
    snapshot = store.snapshot()
    assert snapshot.session_status == "Finished"
    assert len(snapshot.completed_laps) == 6
    aaa = [lap for lap in snapshot.completed_laps if lap.driver_code == "AAA"]
    bbb = [lap for lap in snapshot.completed_laps if lap.driver_code == "BBB"]
    assert [lap.cumulative_time_s for lap in aaa] == [90.0, 180.5, 272.5]
    assert [lap.cumulative_time_s for lap in bbb] == [91.0, 183.5, 276.5]
    assert statuses[-1].message == "Replay completed"


def test_malformed_line_warns_without_crashing(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("bad line\n['Heartbeat', {}, '']\n", encoding="utf-8")
    source = ReplayLiveTimingSource(path)
    messages = []
    source.run(messages.append, lambda _status: None)
    assert source.invalid_record_count == 1
    assert len(messages) == 1


def test_jsonl_live_recording_without_session_clock_replays_by_receive_time(tmp_path):
    path = tmp_path / "live_timing_raw.jsonl"
    path.write_text(
        '{"recording_schema_version":2,"message_schema_version":1,'
        '"topic":"SessionStatus","payload":{"Status":"Started"},'
        '"source_timestamp_utc":null,"received_at_utc":"2026-09-06T13:00:00Z",'
        '"source":"signalr_live","sequence":1,"session_time_seconds":null}\n'
        '{"recording_schema_version":2,"message_schema_version":1,'
        '"topic":"SessionStatus","payload":{"Status":"Finished"},'
        '"source_timestamp_utc":null,"received_at_utc":"2026-09-06T13:00:02.5Z",'
        '"source":"signalr_live","sequence":2,"session_time_seconds":null}\n',
        encoding="utf-8",
    )

    events, invalid = load_replay(path)

    assert invalid == 0
    assert [event.session_time_seconds for event in events] == [0.0, 2.5]
    assert [event.message_session_time_seconds for event in events] == [None, None]
    assert all(event.preserve_message_session_time for event in events)
    replayed = []
    ReplayPlayer(path, speed=0).play(replayed.append)
    assert [message.received_at_utc.isoformat() for message in replayed] == [
        "2026-09-06T13:00:00+00:00",
        "2026-09-06T13:00:02.500000+00:00",
    ]
