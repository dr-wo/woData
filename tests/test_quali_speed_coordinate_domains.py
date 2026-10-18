"""C2 regression: actual reducer and adapter must never order unlike clocks."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from wodata.live.models import LiveTimingMessage
from wodata.live.quali_reducer import QualiSessionStore

UTC = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message(topic, payload, domain, stamp, *, receipt=0, paired=False):
    return LiveTimingMessage(
        topic, payload,
        UTC + timedelta(seconds=stamp) if domain == "utc" or paired else None,
        UTC + timedelta(seconds=receipt),
        source="synthetic_test", session_time_seconds=stamp if domain == "session" else None,
    )


def speed(domain, stamp, value=180, **kwargs):
    return message("CarData.z", {"Entries": [{
        "Utc": (UTC + timedelta(seconds=stamp)).isoformat() if domain == "utc" or kwargs.get("paired") else None,
        "Cars": {"1": {"Channels": {"2": value}}},
    }]}, domain, stamp, **kwargs)


def position(domain, stamp, **kwargs):
    return message("Position.z", {"Position": [{
        "Timestamp": (UTC + timedelta(seconds=stamp)).isoformat() if domain == "utc" or kwargs.get("paired") else None,
        "Entries": {"1": {"X": 1, "Y": 2, "Status": "OnTrack"}},
    }]}, domain, stamp, **kwargs)


@pytest.mark.parametrize("first,other", [("session", "utc"), ("utc", "session")])
def test_coordinate_switch_recovers_without_mapping(first, other):
    store = QualiSessionStore()
    store.apply(speed(first, 100))
    store.apply(speed(other, 101))
    store.apply(speed(first, 102))
    snapshot = store.apply(speed(first, 103))
    samples = snapshot.speed_history["1"]
    expected = [100, 102, 103] if first == "session" else [UTC.timestamp() + i for i in (100, 102, 103)]
    # Assert raw values before metadata so frozen failure proves actual data loss.
    assert all(value in [sample.session_time_s for sample in samples] for value in expected)
    assert {sample.acquisition_coordinate for sample in samples} == {"session", "utc"}
    assert snapshot.speed_coordinate_offset_s is None


def test_mapping_preserves_original_history_domains_for_matching():
    store = QualiSessionStore()
    store.apply(speed("session", 100))
    store.apply(speed("utc", 101))
    store.apply(speed("session", 102, paired=True))
    samples = store.snapshot().speed_history["1"]
    assert [(sample.acquisition_coordinate, sample.session_time_s) for sample in samples] == [
        ("session", 100), ("session", 102), ("utc", UTC.timestamp() + 101)]
    assert store.snapshot().speed_coordinate_offset_s == UTC.timestamp()


def test_duplicate_identity_includes_domain_and_out_of_order_is_retained():
    store = QualiSessionStore()
    store.apply(speed("session", 100, 180))
    store.apply(speed("utc", 101, 190))
    store.apply(speed("session", 102, 200))
    snapshot = store.apply(speed("session", 100, 999))
    assert snapshot.speed_observations == ()
    store.apply(speed("session", 101, 185))
    samples = store.snapshot().speed_history["1"]
    assert [(s.session_time_s, s.speed_kph) for s in samples if s.acquisition_coordinate == "session"] == [
        (100, 180), (101, 185), (102, 200)]


def test_retention_and_capacity_apply_independently_to_coordinates():
    store = QualiSessionStore()
    for index in range(600):
        store.apply(speed("session", 100 + index / 100))
        store.apply(speed("utc", 100 + index / 100))
    samples = store.snapshot().speed_history["1"]
    assert len(samples) <= 512
    assert {s.acquisition_coordinate for s in samples} == {"utc", "session"}
    store.apply(speed("session", 140))
    samples = store.snapshot().speed_history["1"]
    assert [s.session_time_s for s in samples if s.acquisition_coordinate == "session"] == [140]
    assert any(s.acquisition_coordinate == "utc" for s in samples)
    store.apply(speed("utc", 150))
    assert len(store.snapshot().speed_history["1"]) == 2


def adapter_store():
    from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
    store, adapter = QualiSessionStore(), RollingQualiPhysicalTraffic()
    adapter.ingest(store.apply(message("DriverList", {"1": {"Tla": "AAA"}}, "session", 0)))
    return store, adapter


@pytest.mark.parametrize("first,other", [("session", "utc"), ("utc", "session")])
def test_real_adapter_matches_correct_domain_after_switching(first, other):
    store, adapter = adapter_store()
    for index, domain in enumerate((first, other, first)):
        adapter.ingest(store.apply(speed(domain, 100 + index, 180 + index)))
        adapter.ingest(store.apply(position(domain, 100 + index)))
    assert adapter.row_count == 3
    assert sorted(row["Speed"] for row in adapter._rows["1"]) == [180, 181, 182]


def test_adapter_rejects_cross_domain_without_mapping_even_if_numbers_overlap():
    store, adapter = adapter_store()
    # A session number can happen to look like an epoch; numerical overlap is not evidence.
    epoch = UTC.timestamp() + 100
    adapter.ingest(store.apply(speed("session", epoch)))
    adapter.ingest(store.apply(position("utc", 100)))
    assert adapter.row_count == 0


def test_adapter_converts_only_with_independently_established_mapping():
    store, adapter = adapter_store()
    adapter.ingest(store.apply(speed("session", 100, paired=True)))
    adapter.ingest(store.apply(position("utc", 100.5)))
    assert adapter.row_count == 1
    assert adapter._rows["1"][0]["Speed"] == 180


@pytest.mark.parametrize("speed_time,position_time,count", [(100, 100, 1), (100, 105, 1), (100, 105.001, 0), (101, 100, 0)])
def test_adapter_five_second_freshness_and_future_rejection(speed_time, position_time, count):
    store, adapter = adapter_store()
    adapter.ingest(store.apply(speed("session", speed_time)))
    adapter.ingest(store.apply(position("session", position_time)))
    assert adapter.row_count == count


def test_speed_received_after_position_does_not_retroactively_match():
    store, adapter = adapter_store()
    adapter.ingest(store.apply(position("session", 100)))
    adapter.ingest(store.apply(speed("session", 100)))
    assert adapter.row_count == 0


def test_unmapped_position_advances_only_its_own_domain_retention():
    store = QualiSessionStore()
    store.apply(speed("session", 100))
    store.apply(speed("utc", 100))
    position_message = replace(position("utc", 140), source_timestamp_utc=None,
                               session_time_seconds=101)
    snapshot = store.apply(position_message)
    assert [(s.acquisition_coordinate, s.session_time_s) for s in snapshot.speed_history["1"]] == [
        ("session", 100)]


def test_bounded_mixed_driver_domains_and_retained_ordinal_bookkeeping():
    store = QualiSessionStore()
    for number in range(70):
        packet = speed("session" if number % 2 else "utc", 100)
        packet = replace(packet, payload={"Entries": [{
            "Utc": packet.payload["Entries"][0]["Utc"],
            "Cars": {str(number): {"Channels": {"2": 180}}},
        }]})
        store.apply(packet)
    assert len(store.snapshot().speed_history) == 64
    assert len(store._speed_receipt_ordinals) == 64
    assert set(store.snapshot().speed_history) == {str(i) for i in range(6, 70)}
    store.reset()
    assert store._speed_history_anchors == {}
    assert store._speed_receipt_ordinals == {}


def test_mapping_preserves_distinct_domain_records_and_immutable_snapshot():
    store = QualiSessionStore()
    before = store.apply(speed("utc", 100, 170))
    store.apply(speed("session", 100, 180))
    snapshot = store.apply(speed("session", 101, paired=True))
    assert [(s.acquisition_coordinate, s.session_time_s, s.speed_kph)
            for s in snapshot.speed_history["1"]] == [
        ("session", 100, 180), ("session", 101, 180), ("utc", UTC.timestamp() + 100, 170)]
    assert before.speed_history["1"][0].acquisition_coordinate == "utc"
    assert before.speed_history["1"][0].session_time_s == UTC.timestamp() + 100


def test_conflicting_independent_mapping_disables_conversion_without_history_loss():
    store, adapter = adapter_store()
    adapter.ingest(store.apply(speed("session", 100, paired=True)))
    before = store.apply(speed("utc", 101, 190))
    assert before.speed_coordinate_offset_s == UTC.timestamp()
    # A separately paired source timestamp contradicts the existing UTC/session bridge.
    conflicting = replace(speed("session", 102, paired=True),
                          source_timestamp_utc=UTC + timedelta(seconds=162))
    snapshot = store.apply(conflicting)
    assert snapshot.speed_coordinate_offset_s is None
    assert any(s.acquisition_coordinate == "utc" and s.session_time_s == UTC.timestamp() + 101
               for s in snapshot.speed_history["1"])
    adapter.ingest(snapshot)
    adapter.ingest(store.apply(position("session", 102)))
    # The local conflicting envelope sample occurs at session42; UTC101 cannot be mapped.
    assert adapter.row_count == 1
    assert adapter._rows["1"][0]["Speed"] == 180
    # Repetition is not independent resolution of contradictory evidence.
    store.apply(speed("session", 103, paired=True))
    assert store.snapshot().speed_coordinate_offset_s is None
    store.reset()
    assert store.apply(speed("session", 104, paired=True)).speed_coordinate_offset_s == UTC.timestamp()


def test_reducer_preserves_native_monotonic_receipt_for_speed_and_position():
    store = QualiSessionStore()
    raw = store.apply(replace(speed("session", 100), received_at_monotonic_s=10))
    assert raw.receive_monotonic_s == 10
    assert raw.speed_observations[0].received_at_monotonic_s == 10
    assert raw.speed_history["1"][0].received_at_monotonic_s == 10
    raw = store.apply(replace(position("session", 100), received_at_monotonic_s=11))
    assert raw.receive_monotonic_s == 11
    assert raw.position_observations[0].received_at_monotonic_s == 11
    assert store.apply(position("session", 101)).receive_monotonic_s is None


def test_real_adapter_uses_monotonic_receipt_when_utc_jumps_backwards():
    store, adapter = adapter_store()
    adapter.ingest(store.apply(replace(speed("session", 100, receipt=60),
                                      source="signalr_live", received_at_monotonic_s=10)))
    adapter.ingest(store.apply(replace(position("session", 100, receipt=0),
                                      source="signalr_live", received_at_monotonic_s=11)))
    assert adapter.row_count == 1


def test_preclock_archive_speed_matches_after_mapping_without_rewriting_history():
    store, adapter = adapter_store()
    bootstrap = replace(speed("utc", 9), source_timestamp_utc=None, source="f1_archive_replay")
    adapter.ingest(store.apply(bootstrap))
    clock = message("ExtrapolatedClock", {"Utc": (UTC + timedelta(seconds=10)).isoformat(),
                                         "Remaining": "10:00"}, "session", 10)
    adapter.ingest(store.apply(clock))
    mapped_position = replace(position("utc", 9.5), source_timestamp_utc=None,
                              session_time_seconds=10)
    adapter.ingest(store.apply(mapped_position))
    assert adapter.row_count == 1
    assert store.snapshot().speed_history["1"][0].acquisition_coordinate == "utc"
    assert adapter._rows["1"][0]["SessionTime"] == 9.5
