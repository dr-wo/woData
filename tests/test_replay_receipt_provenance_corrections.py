import json
from datetime import datetime, timezone

import pytest

from wodata.live.quali_reducer import QualiSessionStore
from wodata.live.replay.faults import ReplayFaultConfig
from wodata.live.replay.player import ReplayPlayer, load_replay


RECEIPT = "2026-01-01T00:01:40+00:00"


def recording(tmp_path, **changes):
    record = dict(
        recording_schema_version=2, message_schema_version=1,
        topic="Heartbeat", payload={}, source="signalr_live", sequence=1,
        received_at_utc=RECEIPT, source_timestamp_utc=None,
        session_time_seconds=100, playback_time_seconds=0,
    )
    record.update(changes)
    path = tmp_path / "recording.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def replay_snapshot(path, **options):
    store = QualiSessionStore()
    messages = []

    def consume(message):
        messages.append(message)
        store.apply(message)

    ReplayPlayer(path, **options).play(consume)
    return messages, store.snapshot()


@pytest.mark.parametrize("receipt", ["not-a-timestamp", None, ""])
def test_strict_genuine_recording_requires_valid_original_receipt(tmp_path, receipt):
    path = recording(tmp_path, received_at_utc=receipt)
    with pytest.raises(ValueError, match="line 1.*receipt"):
        ReplayPlayer(path, speed=0).play(lambda _message: None)


@pytest.mark.parametrize("receipt", ["not-a-timestamp", None, ""])
def test_permissive_invalid_receipt_has_deterministic_untrusted_fallback(tmp_path, receipt):
    path = recording(tmp_path, received_at_utc=receipt)
    first, evidence = replay_snapshot(path, speed=0, permissive=True)
    second, _ = replay_snapshot(path, speed=0, permissive=True)
    assert first[0].received_at_utc == second[0].received_at_utc
    assert first[0].received_at_utc == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert first[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False
    assert evidence.traffic_clock_kind == "playback"
    assert evidence.traffic_clock_time_s == 0


@pytest.mark.parametrize("speed", [0, 10000])
def test_valid_genuine_receipt_survives_accelerated_and_fault_scheduling(tmp_path, speed):
    path = recording(tmp_path)
    messages, evidence = replay_snapshot(
        path, speed=speed,
        fault_config=ReplayFaultConfig(topic_delay_seconds={"Heartbeat": 30}, duplicate_rate=1),
    )
    assert len(messages) == 2
    assert all(message.received_at_utc.isoformat() == RECEIPT for message in messages)
    assert all(message.received_at_is_trusted is True for message in messages)
    assert all(message.session_time_seconds == 100 for message in messages)
    assert all(message.playback_time_seconds == 30 for message in messages)
    assert evidence.receive_time_trusted is True
    assert evidence.traffic_clock_kind == "receive"
    assert evidence.traffic_clock_time_s == datetime.fromisoformat(RECEIPT).timestamp()


def test_archive_without_receipt_uses_untrusted_playback_clock(tmp_path):
    path = tmp_path / "archive.jsonl"
    path.write_text(json.dumps(dict(schema_version=1, topic="Heartbeat", payload={},
                                    sequence=1, session_time_seconds=100)) + "\n")
    messages, evidence = replay_snapshot(path, speed=0)
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False
    assert evidence.traffic_clock_kind == "playback"
    assert evidence.traffic_clock_time_s == 100


def test_explicit_synthetic_recording_remains_compatible_when_receipt_absent(tmp_path):
    path = recording(tmp_path, received_at_utc=None, received_at_is_trusted=False)
    messages, evidence = replay_snapshot(path, speed=0)
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False


@pytest.mark.parametrize("flag", ["false", "true", 1, 0, [], {}])
def test_invalid_trust_flag_cannot_certify_receipt(tmp_path, flag):
    path = recording(tmp_path, received_at_is_trusted=flag)
    with pytest.raises(ValueError, match="line 1.*received_at_is_trusted"):
        load_replay(path)
    messages, evidence = replay_snapshot(path, speed=0, permissive=True)
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False


def test_recovered_archive_explicit_trust_requires_valid_receipt(tmp_path):
    path = tmp_path / "recovered.jsonl"
    record = dict(schema_version=1, topic="Heartbeat", payload={}, sequence=1,
                  session_time_seconds=0, source="recovered_live",
                  original_received_at="bad", received_at_is_trusted=True)
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="line 1.*receipt"):
        load_replay(path)
    messages, evidence = replay_snapshot(path, speed=0, permissive=True)
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False


def test_missing_receipt_key_and_bad_receipt_without_clocks_never_invent_trust(tmp_path):
    path = recording(tmp_path, received_at_utc=None, session_time_seconds=None,
                     playback_time_seconds=None)
    record = json.loads(path.read_text())
    del record["received_at_utc"]
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="receipt"):
        load_replay(path)
    events, invalid = load_replay(path, permissive=True)
    assert invalid == 0
    assert events[0].session_time_seconds == 0
    messages, evidence = replay_snapshot(path, speed=10000, permissive=True)
    assert messages[0].session_time_seconds is None
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False


def test_fallback_to_source_timestamp_stays_untrusted_and_schedule_independent(tmp_path):
    path = recording(tmp_path, received_at_utc="bad", source_timestamp_utc=RECEIPT)
    messages, evidence = replay_snapshot(
        path, speed=10000, permissive=True,
        fault_config=ReplayFaultConfig(topic_delay_seconds={"Heartbeat": 60}),
    )
    assert messages[0].received_at_utc.isoformat() == RECEIPT
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False
    assert evidence.traffic_clock_time_s == 60


def test_schema2_archive_receipt_without_trust_claim_is_not_genuine(tmp_path):
    path = recording(tmp_path, source="f1_archive_replay")
    messages, evidence = replay_snapshot(path, speed=0)
    assert messages[0].received_at_utc.isoformat() == RECEIPT
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False


def test_valid_receipt_cannot_override_explicit_untrusted_provenance(tmp_path):
    path = recording(tmp_path, received_at_is_trusted=False)
    messages, evidence = replay_snapshot(path, speed=0)
    assert messages[0].received_at_utc.isoformat() == RECEIPT
    assert messages[0].received_at_is_trusted is False
    assert evidence.receive_time_trusted is False
    assert getattr(messages[0], "received_at_monotonic_s", None) is None


def test_genuine_recovered_receipt_roundtrip_and_schedule_faults(tmp_path):
    path = tmp_path / "recovered.jsonl"
    record = dict(schema_version=1, topic="Heartbeat", payload={}, sequence=1,
                  session_time_seconds=0, source="recovered_live",
                  original_received_at=RECEIPT, received_at_is_trusted=True)
    path.write_text(json.dumps(record) + "\n")
    events, invalid = load_replay(path)
    assert invalid == 0
    path.write_text(json.dumps(events[0].to_dict()) + "\n")
    messages, evidence = replay_snapshot(path, speed=10000)
    assert messages[0].received_at_utc.isoformat() == RECEIPT
    assert messages[0].received_at_is_trusted is True
    assert evidence.receive_time_trusted is True


def test_original_receipt_is_parsed_once_including_duplicate_faults(tmp_path, monkeypatch):
    import wodata.live.replay.player as player_module
    import wodata.live.replay.schema as schema_module

    original_parser = schema_module.parse_timestamp
    calls = []

    def tracked_parser(value):
        if value == RECEIPT:
            calls.append(value)
        return original_parser(value)

    monkeypatch.setattr(schema_module, "parse_timestamp", tracked_parser)
    monkeypatch.setattr(player_module, "parse_timestamp", tracked_parser)
    path = recording(tmp_path)
    messages, _ = replay_snapshot(
        path, speed=0, fault_config=ReplayFaultConfig(duplicate_rate=1),
    )
    assert len(messages) == 2
    assert calls == [RECEIPT]
