"""D3: public canonical live receipts survive legacy format and scheduling."""
import json
from datetime import datetime, timezone

import pytest

from wodata.live.quali_reducer import QualiSessionStore
from wodata.live.replay.faults import ReplayFaultConfig
from wodata.live.replay.player import ReplayPlayer, load_replay
from wodata.live.replay.schema import ReplayEvent


RECEIPT = "2026-01-01T00:01:40+00:00"


def canonical_path(tmp_path, *, receipt=RECEIPT, source="signalr_live", flag=None,
                   include_receipt=True):
    # Generate the valid historical format through the public writer, not a
    # hand-built approximation of its optional provenance fields.
    record = ReplayEvent("Heartbeat", {}, 1., 1, source=source,
                         original_received_at=receipt,
                         received_at_is_trusted=flag).to_dict()
    if not include_receipt:
        record.pop("original_received_at", None)
    path = tmp_path / "canonical.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return path


def consume(path, **options):
    store = QualiSessionStore()
    messages = []
    ReplayPlayer(path, **options).play(lambda message: (messages.append(message), store.apply(message)))
    return messages, store.snapshot()


@pytest.mark.parametrize("speed", [0., 10000.])
@pytest.mark.parametrize("permissive", [False, True])
def test_public_legacy_live_receipt_remains_genuine_under_faults(tmp_path, speed, permissive):
    path = canonical_path(tmp_path)
    events, invalid = load_replay(path, permissive=permissive)
    assert invalid == 0
    assert events[0].received_at_is_trusted is True
    assert events[0].to_dict()["original_received_at"] == RECEIPT
    messages, raw = consume(path, speed=speed, permissive=permissive,
        fault_config=ReplayFaultConfig(duplicate_rate=1., topic_delay_seconds={"Heartbeat": 30.}))
    assert len(messages) == 2
    assert all(m.received_at_is_trusted is True for m in messages)
    assert all(m.received_at_utc.isoformat() == RECEIPT for m in messages)
    assert all(m.playback_time_seconds == 31. for m in messages)
    assert all(m.session_time_seconds == 1. for m in messages)
    assert all(m.received_at_monotonic_s is None for m in messages)
    assert raw.receive_time_trusted is True
    assert raw.traffic_clock_kind == "receive"
    assert raw.traffic_clock_time_s == datetime.fromisoformat(RECEIPT).timestamp()


@pytest.mark.parametrize("receipt,include_receipt", [(None, False), (None, True),
                                                     ("", True), ("invalid", True)])
def test_legacy_claimed_live_missing_or_invalid_receipt_strict_error(tmp_path, receipt, include_receipt):
    path = canonical_path(tmp_path, receipt=receipt, include_receipt=include_receipt)
    with pytest.raises(ValueError, match="line 1.*valid original receipt"):
        load_replay(path)


@pytest.mark.parametrize("receipt,include_receipt", [(None, False), (None, True),
                                                     ("", True), ("invalid", True)])
def test_legacy_invalid_receipt_permissive_is_deterministic_untrusted(tmp_path, receipt, include_receipt):
    path = canonical_path(tmp_path, receipt=receipt, include_receipt=include_receipt)
    first, raw = consume(path, speed=0, permissive=True)
    second, _ = consume(path, speed=10000, permissive=True)
    assert first[0].received_at_utc == second[0].received_at_utc == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert first[0].received_at_is_trusted is False
    assert raw.receive_time_trusted is False
    assert raw.traffic_clock_kind == "playback"
    assert raw.traffic_clock_time_s == 1.


@pytest.mark.parametrize("source", ["signalr_live", "f1_archive_replay", "recovered_live"])
@pytest.mark.parametrize("receipt", [RECEIPT, None, "invalid"])
def test_explicit_untrusted_legacy_declaration_stays_untrusted(tmp_path, source, receipt):
    path = canonical_path(tmp_path, source=source, receipt=receipt, flag=False)
    messages, raw = consume(path, speed=0)
    assert messages[0].received_at_is_trusted is False
    assert raw.receive_time_trusted is False
    assert raw.traffic_clock_kind == "playback"


@pytest.mark.parametrize("source", ["f1_archive_replay", "recovered_live", "synthetic_test", "signalr"])
def test_parseable_receipt_alone_does_not_claim_legacy_live_provenance(tmp_path, source):
    path = canonical_path(tmp_path, source=source)
    messages, raw = consume(path, speed=0)
    assert messages[0].received_at_utc.isoformat() == RECEIPT
    assert messages[0].received_at_is_trusted is False
    assert raw.receive_time_trusted is False
    assert raw.traffic_clock_kind == "playback"


@pytest.mark.parametrize("flag", ["false", 0, "true", 1, [], {}])
def test_legacy_malformed_trust_flag_is_rejected_or_downgraded(tmp_path, flag):
    path = canonical_path(tmp_path, flag=flag)
    with pytest.raises(ValueError, match="line 1.*boolean"):
        load_replay(path)
    messages, raw = consume(path, speed=0, permissive=True)
    assert messages[0].received_at_is_trusted is False
    assert raw.receive_time_trusted is False
