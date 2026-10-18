"""Input contract: publication clocks survive source adaptation and scheduling."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone, timedelta
import json
import sys
import types

import pytest

from wodata.live.models import LiveTimingMessage, PublicationTime
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.recovery import load_recording, _write_canonical
from wodata.live.replay import ArchiveRecord, FastF1ArchiveClient, ReplayFaultConfig, ReplayPlayer, merge_records
from wodata.live.replay.faults import apply_faults
from wodata.live.replay.player import load_replay
from wodata.live.replay.schema import ReplayEvent

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def write_events(path, events):
    path.write_text("".join(json.dumps(event.to_dict()) + "\n" for event in events))


def test_message_coordinates_are_immutable_and_arrival_is_not_provenance():
    unknown = LiveTimingMessage("TimingData", {}, None, NOW, sequence=100)
    assert unknown.publication_time is None
    assert replace(unknown, received_at_utc=NOW + timedelta(days=1), sequence=200).publication_time is None
    utc = replace(unknown, source_timestamp_utc=NOW)
    session = replace(unknown, session_time_seconds=NOW.timestamp())
    assert utc.publication_time == PublicationTime("utc", NOW.timestamp())
    assert session.publication_time == PublicationTime("session", NOW.timestamp())
    assert utc.publication_time != session.publication_time
    with pytest.raises(FrozenInstanceError):
        utc.source_timestamp_utc = None


@pytest.mark.parametrize("legacy_file", [False, True])
def test_archive_schedule_delay_and_duplicate_preserve_publication(tmp_path, monkeypatch, legacy_file):
    events = merge_records([
        ArchiveRecord("TimingData", 190, {"marker": "clear"}, 0),
        ArchiveRecord("TimingData", 200, {"marker": "values"}, 1),
        ArchiveRecord("SessionInfo", 210, {"SessionPart": "Q2"}, 0),
    ])
    path = tmp_path / "replay.jsonl"
    if legacy_file:
        path.write_text("".join(json.dumps({"topic": e.topic, "payload": e.payload,
            "session_time_seconds": e.session_time_seconds, "sequence": e.sequence}) + "\n" for e in events))
    else:
        write_events(path, events)
    content = path.read_bytes()
    waits, messages = [], []
    config = ReplayFaultConfig(topic_delay_seconds={"TimingData": 60}, duplicate_rate=1)
    player = ReplayPlayer(path, speed=2, fault_config=config)
    monkeypatch.setattr(player, "_wait", lambda seconds, _: waits.append(seconds) or True)
    result = player.play(messages.append)
    assert [m.topic for m in messages] == ["SessionInfo"] * 2 + ["TimingData"] * 4
    assert [m.session_time_seconds for m in messages] == [210, 210, 190, 190, 200, 200]
    assert [m.playback_time_seconds for m in messages] == [210, 210, 250, 250, 260, 260]
    assert [m.publication_time for m in messages] == [PublicationTime("session", t) for t in [210, 210, 190, 190, 200, 200]]
    assert waits == [0, 0, 20, 0, 5, 0]
    assert result.last_session_time_seconds == 260
    assert (result.fault_stats.delayed, result.fault_stats.duplicated) == (2, 3)
    assert path.read_bytes() == content
    assert [e.session_time_seconds for e in events] == [190, 200, 210]
    shifted, _ = apply_faults(events, config)
    shifted_twice, _ = apply_faults(shifted, ReplayFaultConfig(topic_delay_seconds={"TimingData": 60}))
    assert [e.message_session_time_seconds for e in shifted_twice if e.topic == "TimingData"] == [190, 190, 200, 200]
    assert [e.session_time_seconds for e in shifted_twice if e.topic == "TimingData"] == [310, 310, 320, 320]
    assert ReplayEvent.from_dict(shifted_twice[-1].to_dict()) == shifted_twice[-1]


@pytest.mark.parametrize("source_clock", ["utc", "session", "unknown"])
def test_v2_roundtrip_preserves_original_envelope_through_faults_and_recording(tmp_path, source_clock):
    path = tmp_path / "capture.jsonl"
    message = LiveTimingMessage("TimingData", {"Lines": {}}, NOW if source_clock == "utc" else None,
        NOW + timedelta(seconds=5), session_time_seconds=100 if source_clock == "session" else None, sequence=17)
    recorder = LiveTimingRecorder(path)
    recorder.write(message)
    recorder.close()
    loaded = load_recording(path)
    assert loaded[0].message == message
    events, invalid = load_replay(path)
    assert invalid == 0
    original_schedule = 100 if source_clock == "session" else 0
    assert events[0].session_time_seconds == original_schedule
    replayed = []
    ReplayPlayer(path, speed=0, fault_config=ReplayFaultConfig(topic_delay_seconds={"TimingData": 60})).play(replayed.append)
    actual = replayed[0]
    assert actual.publication_time == message.publication_time
    assert actual.source_timestamp_utc == message.source_timestamp_utc
    assert actual.session_time_seconds == message.session_time_seconds
    assert actual.received_at_utc == message.received_at_utc
    assert actual.playback_time_seconds == original_schedule + 60
    capture = tmp_path / "recapture.jsonl"
    recorder = LiveTimingRecorder(capture)
    recorder.write(actual)
    recorder.close()
    recaptured = load_recording(capture)
    assert recaptured[0].message == actual
    recovered = tmp_path / "canonical.jsonl"
    _write_canonical(recovered, recaptured)
    for replay_path in (capture, recovered):
        output = []
        ReplayPlayer(replay_path, speed=0).play(output.append)
        assert output[0].publication_time == message.publication_time
        assert output[0].received_at_utc == message.received_at_utc
        assert output[0].playback_time_seconds == actual.playback_time_seconds


def test_untimestamped_legacy_record_has_schedule_but_no_fabricated_provenance(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_text("['TimingData', {'Lines': {}}, '']\n")
    output = []
    ReplayPlayer(path, speed=0).play(output.append)
    assert output[0].playback_time_seconds == 0
    assert output[0].publication_time is None


def test_archive_snapshot_zero_is_a_schedule_not_an_occurrence(monkeypatch, tmp_path):
    fake = types.ModuleType("fastf1")
    fake._api = types.SimpleNamespace(fetch_page=lambda *_: {"Lines": {}})
    monkeypatch.setitem(sys.modules, "fastf1", fake)
    records = FastF1ArchiveClient().fetch_topic("/api/", "TimingData")
    assert records[0].session_time_seconds == 0
    assert records[0].publication_time_known is False
    assert ArchiveRecord.from_dict(records[0].to_dict()) == records[0]
    path = tmp_path / "archive.jsonl"
    write_events(path, merge_records(records))
    output = []
    ReplayPlayer(path, speed=0).play(output.append)
    assert output[0].playback_time_seconds == 0
    assert output[0].publication_time is None


def test_ordinary_archive_player_keeps_source_and_schedule_equal(tmp_path):
    path = tmp_path / "ordinary.jsonl"
    write_events(path, merge_records([ArchiveRecord("Heartbeat", t, {}, i) for i, t in enumerate((0, 10, 5))]))
    output = []
    ReplayPlayer(path, speed=0).play(output.append)
    assert [m.session_time_seconds for m in output] == [0, 5, 10]
    assert [m.playback_time_seconds for m in output] == [0, 5, 10]


def test_download_builder_and_player_preserve_archive_stream_coordinates(monkeypatch, tmp_path):
    from wodata.live.replay import ArchiveSession, download_archive, build_replay
    fake = types.ModuleType("fastf1")
    fake._api = types.SimpleNamespace(fetch_page=lambda _path, topic:
        [["00:03:10.000", {"Lines": {}}], ["00:03:20.000", {"Lines": {}}]]
        if topic == "timing_data" else [["00:03:30.000", {"SessionPart": "Q2"}]])
    monkeypatch.setitem(sys.modules, "fastf1", fake)
    downloaded = download_archive(ArchiveSession(2026, 16, "Test", "Qualifying", "Q", "/api/"),
        output_root=tmp_path, topics=("TimingData", "SessionInfo"), client=FastF1ArchiveClient())
    raw = json.loads((downloaded.archive_dir / "raw/TimingData.json").read_text())
    assert [r["session_time_seconds"] for r in raw["records"]] == [190, 200]
    built = build_replay(downloaded.archive_dir)
    events, _ = load_replay(built.replay_path)
    assert [(e.session_time_seconds, e.message_session_time_seconds) for e in events] == [(190, 190), (200, 200), (210, 210)]
    output = []
    ReplayPlayer(built.replay_path, speed=0, fault_config=ReplayFaultConfig(topic_delay_seconds={"TimingData": 60})).play(output.append)
    assert [(m.topic, m.session_time_seconds, m.playback_time_seconds) for m in output] == [
        ("SessionInfo", 210, 210), ("TimingData", 190, 250), ("TimingData", 200, 260)]


def test_clock_reset_uses_its_own_publication_fence_for_both_part_transitions():
    from wodata.live import QualiSessionStore
    store = QualiSessionStore()
    def send(topic, payload, time):
        return store.apply(LiveTimingMessage(topic, payload, None, NOW, session_time_seconds=time))
    def timing(patch, time):
        return send("TimingData", {"Lines": {"31": patch}}, time)
    send("SessionStatus", {"Status": "Started"}, 0)
    send("ExtrapolatedClock", {"Remaining": "18:00", "Extrapolating": True}, 0)
    timing({"InPit": False, "NumberOfLaps": 3, "LastLapTime": {"Value": "1:30"},
            "BestLapTime": {"Value": "1:30", "Lap": 2},
            "Sectors": {str(i): {"Value": str(v)} for i, v in enumerate((20, 30, 40))}}, 100)
    for offset, part, remaining, boundary in ((0, "Q2", "15:00", 4), (200, "Q3", "13:00", 5)):
        send("ExtrapolatedClock", {"Remaining": "00:01", "Extrapolating": True}, offset + 109)
        send("Heartbeat", {}, offset + 150)
        transitioned = send("ExtrapolatedClock", {"Remaining": remaining, "Extrapolating": True}, offset + 110)
        assert transitioned.session_part == part
        timing({"Sectors": {"0": {"Value": ""}}}, offset + 120)
        timing({"Sectors": {"0": {"Value": "25"}}}, offset + 145)
        timing({"Sectors": {"0": {"Value": "21"}}}, offset + 90)
        timing({"Sectors": {"1": {"Value": "26"}}}, offset + 171)
        timing({"NumberOfLaps": boundary, "LastLapTime": {"Value": "1:30"},
                "BestLapTime": {"Value": "1:30", "Lap": boundary - 1}}, offset + 300)
        raw = timing({"Sectors": {"2": {"Value": "39"}}}, offset + 301)
        assert raw.completed_lap_sequence[-1].event_key == (part, "31", boundary, offset + 300)
        assert raw.completed_lap_sequence[-1].sectors_s == (25, 26, 39)
        assert raw.drivers[0].best_lap_event_key == (part, "31", boundary, offset + 300)
