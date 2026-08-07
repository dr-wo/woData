from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import types

import pytest

from wodata.live.replay import (
    ArchiveRecord,
    ArchiveSession,
    FastF1ArchiveClient,
    ReplayFaultConfig,
    ReplayPlayer,
    build_replay,
    download_archive,
    merge_records,
    session_time_seconds,
)


class FakeArchiveClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def fetch_topic(self, api_path, topic):
        self.calls.append((api_path, topic))
        return self.records.get(topic, [])


def record(topic, seconds, order, payload=None):
    return ArchiveRecord(topic, seconds, payload or {"Value": order}, order)


def write_replay(path, events):
    path.write_text(
        "".join(json.dumps(event.to_dict(), separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )


def test_session_timestamp_conversion():
    assert session_time_seconds("01:02:03.500") == 3723.5
    assert session_time_seconds("01:02:03:500") == 3723.5
    assert session_time_seconds("00:00:01:250".replace(":250", ".250")) == 1.25
    assert session_time_seconds(2) == 2.0
    with pytest.raises(ValueError, match="invalid session timestamp"):
        session_time_seconds("bad")


def test_merge_order_is_timestamp_priority_then_source_order():
    records = [
        record("TimingData", 2, 0),
        record("TimingData", 1, 1),
        record("SessionStatus", 1, 1),
        record("TimingData", 1, 0),
    ]
    merged = merge_records(records)
    assert [(item.topic, item.archive_topic_index) for item in merged] == [
        ("SessionStatus", 1),
        ("TimingData", 0),
        ("TimingData", 1),
        ("TimingData", 0),
    ]
    assert [item.sequence for item in merged] == [1, 2, 3, 4]


def test_download_is_per_topic_and_idempotent_then_builds_deterministically(tmp_path):
    session = ArchiveSession(2025, 14, "Hungarian Grand Prix", "Race", "R", "/api/")
    client = FakeArchiveClient({
        "SessionStatus": [record("SessionStatus", 0, 0, {"Status": "Started"})],
        "TimingData": [record("TimingData", 1, 0, {"Lines": {}})],
    })
    first = download_archive(
        session,
        output_root=tmp_path,
        topics=("TimingData", "SessionStatus", "TimingAppData"),
        client=client,
    )
    assert first.available_topics == ("TimingData", "SessionStatus")
    assert first.missing_topics == ("TimingAppData",)
    assert (first.archive_dir / "raw" / "TimingData.json").is_file()
    reused = download_archive(
        session,
        output_root=tmp_path,
        topics=("TimingData", "SessionStatus", "TimingAppData"),
        client=client,
    )
    assert reused.reused is True
    assert len(client.calls) == 3

    built = build_replay(first.archive_dir)
    content = built.replay_path.read_bytes()
    assert built.sha256 == hashlib.sha256(content).hexdigest()
    assert built.event_count == 2
    rebuilt = build_replay(first.archive_dir)
    assert rebuilt.sha256 == built.sha256
    manifest = json.loads((first.archive_dir / "manifest.json").read_text())
    assert manifest["replay"]["ordering_policy"] == "session_time_topic_priority_source_order"


def test_download_refuses_different_existing_archive_without_overwrite(tmp_path):
    session = ArchiveSession(2025, 14, "Hungarian Grand Prix", "Race", "R", "/api/")
    client = FakeArchiveClient({})
    download_archive(session, output_root=tmp_path, topics=("Heartbeat",), client=client)
    with pytest.raises(FileExistsError, match="overwrite=True"):
        download_archive(session, output_root=tmp_path, topics=("TimingData",), client=client)


def test_download_updates_existing_archive_with_new_telemetry_topics(tmp_path):
    session = ArchiveSession(2025, 14, "Hungarian Grand Prix", "Race", "R", "/api/")
    first_client = FakeArchiveClient({
        "TimingData": [record("TimingData", 1, 0, {"Lines": {}})],
    })
    first = download_archive(
        session,
        output_root=tmp_path,
        topics=("TimingData",),
        client=first_client,
    )
    telemetry_client = FakeArchiveClient({
        "CarData.z": [record("CarData.z", 2, 0, {"Entries": []})],
        "Position.z": [record("Position.z", 2, 0, {"Position": []})],
    })

    updated = download_archive(
        session,
        output_root=tmp_path,
        topics=("TimingData", "CarData.z", "Position.z"),
        mandatory_topics=("CarData.z", "Position.z"),
        update_existing=True,
        client=telemetry_client,
    )

    assert telemetry_client.calls == [("/api/", "CarData.z"), ("/api/", "Position.z")]
    assert updated.available_topics == ("TimingData", "CarData.z", "Position.z")
    assert (first.archive_dir / "raw" / "TimingData.json").is_file()
    manifest = json.loads((first.archive_dir / "manifest.json").read_text())
    assert manifest["requested_topics"] == ["TimingData", "CarData.z", "Position.z"]


@pytest.mark.parametrize("topic,page", [("CarData.z", "car_data"), ("Position.z", "position")])
def test_fastf1_client_decodes_raw_compressed_telemetry_records(monkeypatch, topic, page):
    calls = []
    fake_api = types.SimpleNamespace(
        fetch_page=lambda path, name: calls.append((path, name)) or ["00:00:01:250encoded"],
        parse=lambda value, zipped: {"Decoded": value, "Zipped": zipped},
    )
    fake_fastf1 = types.ModuleType("fastf1")
    fake_fastf1._api = fake_api
    monkeypatch.setitem(sys.modules, "fastf1", fake_fastf1)

    records = FastF1ArchiveClient().fetch_topic("/api/", topic)

    assert calls == [("/api/", page)]
    assert len(records) == 1
    assert records[0].session_time_seconds == 1.25
    assert records[0].payload == {"Decoded": "encoded", "Zipped": True}


def test_builder_strict_and_permissive_malformed_record_modes(tmp_path):
    archive = tmp_path / "archive"
    raw = archive / "raw"
    raw.mkdir(parents=True)
    (archive / "manifest.json").write_text(
        json.dumps({"available_topics": ["TimingData"]}), encoding="utf-8"
    )
    (raw / "TimingData.json").write_text(
        json.dumps({"records": [{"session_time_seconds": 1, "payload": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed TimingData record 0"):
        build_replay(archive)
    result = build_replay(archive, strict=False)
    assert result.event_count == 0
    assert result.skipped_records == 1


def test_faults_are_seeded_filtered_delayed_and_do_not_modify_file(tmp_path):
    path = tmp_path / "replay.jsonl"
    events = merge_records([
        record("TimingData", 0, 0),
        record("TimingAppData", 1, 0),
        record("TimingData", 2, 1),
        record("WeatherData", 3, 0),
    ])
    write_replay(path, events)
    original = path.read_bytes()
    config = ReplayFaultConfig(
        excluded_topics=frozenset({"WeatherData"}),
        topic_delay_seconds={"TimingAppData": 5},
        duplicate_rate=0.5,
        drop_rate=0.1,
        random_seed=7,
    )
    first = []
    second = []
    result_a = ReplayPlayer(path, speed=0, fault_config=config).play(first.append)
    result_b = ReplayPlayer(path, speed=0, fault_config=config).play(second.append)
    projection = lambda values: [
        (item.topic, item.session_time_seconds, item.sequence) for item in values
    ]
    assert projection(first) == projection(second)
    assert result_a == result_b
    assert all(item.topic != "WeatherData" for item in first)
    assert all(item.session_time_seconds == 6 for item in first if item.topic == "TimingAppData")
    assert path.read_bytes() == original


def test_player_start_end_stop_and_pause_resume(tmp_path):
    path = tmp_path / "replay.jsonl"
    write_replay(path, merge_records([record("TimingData", value, value) for value in range(4)]))
    selected = []
    result = ReplayPlayer(path, speed=0).play(
        selected.append, start_time_seconds=1, end_time_seconds=2
    )
    assert result.status == "completed"
    assert [item.session_time_seconds for item in selected] == [1, 2]

    player = ReplayPlayer(path, speed=10)
    first_seen = threading.Event()
    output = []

    def consume(message):
        output.append(message)
        if len(output) == 1:
            player.pause()
            first_seen.set()

    worker = threading.Thread(target=lambda: player.play(consume))
    worker.start()
    assert first_seen.wait(1)
    time.sleep(0.03)
    assert len(output) == 1
    player.resume()
    worker.join(2)
    assert not worker.is_alive()
    assert len(output) == 4

    stopped = []
    stop_player = ReplayPlayer(path, speed=0)

    def stop_after_first(message):
        stopped.append(message)
        stop_player.stop()

    stop_result = stop_player.play(stop_after_first)
    assert stop_result.status == "stopped"
    assert len(stopped) == 1
