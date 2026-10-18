from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live.models import LiveTimingMessage
from wodata.live.quali_models import QualiPositionObservation, QualiRawSnapshot
from wodata.live.quali_reducer import QualiSessionStore


UTC = datetime(2026, 1, 1, tzinfo=timezone.utc)


def car_message(samples, *, received=10, source="signalr_live", **kwargs):
    return LiveTimingMessage(
        "CarData.z",
        {"Entries": [
            {"Utc": (UTC + timedelta(seconds=seconds)).isoformat(),
             "Cars": {str(number): {"Channels": {"2": speed}}}}
            for number, seconds, speed in samples
        ]},
        UTC + timedelta(seconds=received), UTC + timedelta(seconds=received),
        source=source, session_time_seconds=received, **kwargs,
    )


def heartbeat(seconds):
    return LiveTimingMessage("Heartbeat", {}, None, UTC, session_time_seconds=seconds)


def match(snapshot, number, position_time):
    return next((sample for sample in reversed(snapshot.speed_history.get(number, ()))
                 if 0 <= position_time - sample.session_time_s <= snapshot.speed_freshness_s), None)


def test_batch_retains_every_speed_for_causal_positions_and_keeps_live_register():
    store = QualiSessionStore()
    store.apply(LiveTimingMessage("DriverList", {"1": {"Tla": "AAA"}}, UTC, UTC))
    snapshot = store.apply(car_message([("1", 9, 190), ("1", 8, 180), ("1", 10, 200)]))
    assert [sample.session_time_s for sample in snapshot.speed_history["1"]] == [8, 9, 10]
    assert [match(snapshot, "1", at).speed_kph for at in (8, 8.5, 9.5, 10)] == [180, 180, 190, 200]
    assert match(snapshot, "1", 7) is None
    assert match(snapshot, "1", 15.001) is None
    assert snapshot.drivers[0].speed_kph == 200
    assert snapshot.drivers[0].latest_telemetry_time_s == 10
    earlier = store.apply(car_message([("1", 8.5, 185)], received=11))
    assert match(earlier, "1", 8.75).speed_kph == 185
    # The history does not change the delivery-order operational register.
    assert earlier.drivers[0].speed_kph == 185
    assert earlier.drivers[0].latest_telemetry_time_s == 8.5
    assert [sample.session_time_s for sample in snapshot.speed_history["1"]] == [8, 9, 10]


def test_history_only_contains_already_received_samples_and_packet_samples_reset():
    store = QualiSessionStore()
    before = store.apply(heartbeat(10))
    assert before.speed_history == {} and before.speed_observations == ()
    after = store.apply(car_message([("1", 8, 180)]))
    assert before.speed_history == {}
    assert len(after.speed_observations) == 1
    polled = store.snapshot()
    assert polled is after
    heartbeat_snapshot = store.apply(heartbeat(11))
    assert heartbeat_snapshot.speed_observations == ()
    assert match(heartbeat_snapshot, "1", 10).speed_kph == 180
    assert QualiRawSnapshot.empty().speed_history is None


def test_duplicate_and_conflicting_same_timestamp_keep_first_received_value():
    store = QualiSessionStore()
    first = store.apply(car_message([("1", 8, 180), ("1", 8, 180), ("1", 8, 200)]))
    assert len(first.speed_observations) == len(first.speed_history["1"]) == 1
    assert first.speed_history["1"][0].speed_kph == 180
    repeat = store.apply(car_message([("1", 8, 180)], received=12))
    assert repeat.speed_observations == ()
    assert repeat.speed_history["1"][0].received_at_utc == UTC + timedelta(seconds=10)


def test_missing_invalid_stale_and_configured_freshness():
    store = QualiSessionStore(telemetry_freshness_s=2)
    snapshot = store.apply(car_message([("1", 8, None), ("2", 8, -1), ("3", 8, "nan")]))
    assert snapshot.speed_history == {} and snapshot.speed_observations == ()
    snapshot = store.apply(car_message([("1", 8, 180)]))
    assert snapshot.speed_freshness_s == 2
    assert match(snapshot, "1", 10) is not None
    assert match(snapshot, "1", 10.001) is None
    assert match(snapshot, "2", 10) is None


def test_time_sample_driver_bounds_and_reset():
    store = QualiSessionStore()
    snapshot = store.apply(car_message([("1", index / 100, index) for index in range(700)]))
    assert len(snapshot.speed_history["1"]) == len(snapshot.speed_observations) == 512
    snapshot = store.apply(car_message([("2", 30, 180)], received=30))
    assert "1" not in snapshot.speed_history
    old = store.apply(car_message([("1", 1, 180)], received=31))
    assert "1" not in old.speed_history
    # Received old samples remain available to latency diagnostics, not matching.
    assert old.speed_observations[0].session_time_s == 1
    bounded = store.apply(car_message([(str(number), 31, 180) for number in range(100)], received=31))
    assert len(bounded.speed_history) == 64
    assert len(bounded.speed_observations) <= 64
    store.reset()
    assert store.snapshot().speed_history == {}


@pytest.mark.parametrize("source,trusted,kind", [
    ("signalr_live", True, "receive"), ("f1_archive_replay", False, "playback"),
    ("synthetic_test", False, "playback"),
])
def test_reception_provenance_and_replay_scheduler_clock(source, trusted, kind):
    store = QualiSessionStore()
    snapshot = store.apply(car_message([("1", 8, 180)], source=source, playback_time_seconds=11))
    assert snapshot.message_source == source
    assert snapshot.receive_time_trusted is trusted
    assert snapshot.traffic_clock_kind == kind
    assert snapshot.traffic_clock_time_s == ((UTC + timedelta(seconds=10)).timestamp() if trusted else 11)
    assert (snapshot.speed_observations[0].received_at_utc is not None) is trusted


def test_explicit_replay_reception_trust_and_untrusted_live_override():
    message = car_message([("1", 8, 180)], source="f1_archive_replay", playback_time_seconds=11)
    trusted = QualiSessionStore().apply(replace(message, received_at_is_trusted=True))
    assert trusted.receive_time_trusted and trusted.traffic_clock_kind == "receive"
    assert trusted.speed_observations[0].received_at_utc == message.received_at_utc
    untrusted = QualiSessionStore().apply(replace(message, source="signalr_live", received_at_is_trusted=False))
    assert not untrusted.receive_time_trusted and untrusted.traffic_clock_time_s == 11
    assert untrusted.speed_observations[0].received_at_utc is None


def test_archive_clock_mapping_preserves_acquisition_coordinate():
    store = QualiSessionStore()
    store.apply(LiveTimingMessage("ExtrapolatedClock", {
        "Utc": (UTC + timedelta(seconds=10)).isoformat(), "Remaining": "10:00",
    }, None, UTC, source="f1_archive_replay", session_time_seconds=10))
    message = car_message([("1", 8, 180)], source="f1_archive_replay")
    snapshot = store.apply(replace(message, source_timestamp_utc=None))
    assert snapshot.speed_history["1"][0].session_time_s == 8
    assert snapshot.speed_history["1"][0].received_at_utc is None


def test_missing_or_nonfinite_scheduler_clock_does_not_claim_latency():
    for clock in (None, float("nan"), float("inf")):
        message = car_message([("1", 8, 180)], source="f1_archive_replay")
        message = replace(message, session_time_seconds=None, playback_time_seconds=clock)
        snapshot = QualiSessionStore().apply(message)
        assert snapshot.traffic_clock_time_s is None
        assert snapshot.traffic_clock_kind is None
        assert not snapshot.receive_time_trusted


def test_malformed_future_speed_cannot_expire_received_causal_samples():
    store = QualiSessionStore()
    snapshot = store.apply(car_message([("1", 9, 190), ("1", 1000, 300)]))
    assert [sample.session_time_s for sample in snapshot.speed_history["1"]] == [9, 1000]
    assert match(snapshot, "1", 10).speed_kph == 190
    # A large batch of future rows also cannot consume the causal sample budget.
    snapshot = store.apply(car_message([("1", 1000 + index, 300) for index in range(600)], received=11))
    assert len(snapshot.speed_history["1"]) == 512
    assert match(snapshot, "1", 10).speed_kph == 190
    snapshot = store.apply(car_message([("2", 31, 200)], received=31))
    assert match(snapshot, "1", 10) is None
    assert match(snapshot, "2", 31).speed_kph == 200


def test_unknown_source_bootstrap_defers_time_expiry_until_position_clock():
    store = QualiSessionStore()
    message = replace(car_message([("1", 9, 190), ("1", 1000, 300)]),
                      source_timestamp_utc=None, session_time_seconds=None)
    snapshot = store.apply(message)
    position_time = (UTC + timedelta(seconds=10)).timestamp()
    assert store._speed_history_anchors == {}
    assert match(snapshot, "1", position_time).speed_kph == 190
    position_message = LiveTimingMessage("Position.z", {"Position": [{
        "Timestamp": (UTC + timedelta(seconds=31)).isoformat(),
        "Entries": {"1": {"X": 1, "Y": 2}},
    }]}, None, UTC)
    after = store.apply(position_message)
    assert match(after, "1", position_time) is None
    assert snapshot.speed_history["1"][0].session_time_s == (UTC + timedelta(seconds=9)).timestamp()


def test_preclock_archive_history_aligns_when_independent_offset_arrives():
    store = QualiSessionStore()
    bootstrap = replace(car_message([("1", 9, 190)], source="f1_archive_replay"),
                        source_timestamp_utc=None, session_time_seconds=None)
    before = store.apply(bootstrap)
    assert before.speed_history["1"][0].session_time_s == (UTC + timedelta(seconds=9)).timestamp()
    mapped = store.apply(LiveTimingMessage("ExtrapolatedClock", {
        "Utc": (UTC + timedelta(seconds=10)).isoformat(), "Remaining": "10:00",
    }, None, UTC, source="f1_archive_replay", session_time_seconds=10))
    # Cross-domain conversion belongs to matching, not destructive history mutation.
    assert mapped.speed_history["1"][0].acquisition_coordinate == "utc"
    assert mapped.speed_history["1"][0].session_time_s == (UTC + timedelta(seconds=9)).timestamp()
    assert mapped.speed_coordinate_offset_s == UTC.timestamp()
    after = store.apply(replace(car_message([("1", 11, 195)], source="f1_archive_replay"),
                                source_timestamp_utc=None))
    assert [(sample.acquisition_coordinate, sample.session_time_s)
            for sample in after.speed_history["1"]] == [("session", 11), ("utc", UTC.timestamp() + 9)]


@pytest.mark.parametrize("source_timestamp,session_time,offset,sample_timestamp,coordinate,expected_time", [
    (UTC + timedelta(seconds=10), 10, None, UTC + timedelta(seconds=9), "session", 9),
    (UTC + timedelta(seconds=10), None, None, UTC + timedelta(seconds=9), "utc",
     (UTC + timedelta(seconds=9)).timestamp()),
    (None, 10, None, UTC + timedelta(seconds=9), "utc",
     (UTC + timedelta(seconds=9)).timestamp()),
    (None, 10, UTC.timestamp(), UTC + timedelta(seconds=9), "session", 9),
    (None, 10, None, None, "session", 10),
    (UTC + timedelta(seconds=10), None, None, None, "utc",
     (UTC + timedelta(seconds=10)).timestamp()),
])
def test_position_acquisition_coordinate_follows_actual_timestamp_conversion(
    source_timestamp, session_time, offset, sample_timestamp, coordinate, expected_time,
):
    store = QualiSessionStore()
    if offset is not None:
        store.apply(LiveTimingMessage("ExtrapolatedClock", {
            "Utc": (UTC + timedelta(seconds=10)).isoformat(), "Remaining": "10:00",
        }, None, UTC, source="f1_archive_replay", session_time_seconds=10))
    snapshot = store.apply(LiveTimingMessage("Position.z", {"Position": [{
        "Timestamp": sample_timestamp.isoformat() if sample_timestamp is not None else None,
        "Entries": {"1": {"X": 1, "Y": 2, "Status": "OnTrack"}},
    }]}, source_timestamp, UTC, source="f1_archive_replay", session_time_seconds=session_time))
    observation, = snapshot.position_observations
    assert observation.acquisition_coordinate == coordinate
    assert observation.session_time_s == expected_time


def test_position_coordinate_preserves_legacy_constructor_and_unknown_clock():
    assert QualiPositionObservation("1", 10, "OnTrack", False).acquisition_coordinate is None
    store = QualiSessionStore()
    message = LiveTimingMessage("Position.z", {"Position": [{
        "Timestamp": None, "Entries": {"1": {"X": 1, "Y": 2}},
    }]}, None, UTC, source="f1_archive_replay")
    assert store._sample_coordinate(None, message) is None
    assert store.apply(message).position_observations == ()
