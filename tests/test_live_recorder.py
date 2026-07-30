from datetime import datetime, timezone

from wodata.live import LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder, unused_recording_path


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_recorder_does_not_create_or_truncate_until_first_message(tmp_path):
    path = tmp_path / "live_timing_raw.txt"
    path.write_text("existing data\n", encoding="utf-8")

    recorder = LiveTimingRecorder(path)
    assert path.read_text(encoding="utf-8") == "existing data\n"
    recorder.close()
    assert path.read_text(encoding="utf-8") == "existing data\n"


def test_recorder_creates_file_when_first_message_arrives(tmp_path):
    path = tmp_path / "live_timing_raw.txt"
    recorder = LiveTimingRecorder(path)
    assert not path.exists()

    recorder.write(LiveTimingMessage("Heartbeat", {}, NOW, NOW))
    recorder.close()

    assert path.stat().st_size > 0
    assert "Heartbeat" in path.read_text(encoding="utf-8")


def test_unused_recording_path_preserves_nonempty_captures(tmp_path):
    original = tmp_path / "live_timing_raw.txt"
    second = tmp_path / "live_timing_raw_2.txt"
    original.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    assert unused_recording_path(original) == tmp_path / "live_timing_raw_3.txt"
