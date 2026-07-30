from pathlib import Path

from wodata.live import LiveSessionStore, ReplayLiveTimingSource


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
