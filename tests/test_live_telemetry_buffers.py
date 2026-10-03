from __future__ import annotations

from datetime import datetime, timezone

from wodata.live import LiveSessionStore, LiveTimingMessage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _message(topic, payload, sequence):
    return LiveTimingMessage(
        topic,
        payload,
        NOW,
        NOW,
        source="synthetic_test",
        sequence=sequence,
        session_time_seconds=float(sequence),
    )


def _messages():
    return [
        _message(
            "DriverList",
            {"1": {"Tla": "AAA", "TeamName": "Team"}},
            1,
        ),
        _message(
            "CarData.z",
            {
                "Entries": [
                    {
                        "Utc": "2026-01-01T00:00:02Z",
                        "Cars": {"1": {"Channels": {"2": 180}}},
                    }
                ]
            },
            2,
        ),
        _message(
            "Position.z",
            {
                "Position": [
                    {
                        "Timestamp": "2026-01-01T00:00:02Z",
                        "Entries": {"1": {"X": 10, "Y": 20, "Z": 0, "Status": "OnTrack"}},
                    }
                ]
            },
            3,
        ),
    ]


def test_batch_and_replay_style_delivery_produce_identical_normalized_buffers():
    batch = LiveSessionStore().apply_many(_messages())
    rolling_store = LiveSessionStore()
    rolling = None
    for message in _messages():
        rolling = rolling_store.apply(message)

    assert rolling is not None
    assert rolling.telemetry_samples == batch.telemetry_samples
    assert rolling.position_samples == batch.position_samples
    assert batch.telemetry_samples[0].speed_kph == 180.0
    assert batch.telemetry_samples[0].lap_number == 1
    assert batch.position_samples[0].on_track is True


def test_embedded_telemetry_timestamp_is_normalized_to_session_clock():
    timing_message = LiveTimingMessage(
        "CarData.z",
        {
            "Entries": [
                {
                    "Utc": "2026-01-01T00:00:12Z",
                    "Cars": {"1": {"Channels": {"2": 180}}},
                }
            ]
        },
        datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
        NOW,
        source="f1_archive_replay",
        sequence=1,
        session_time_seconds=100.0,
    )

    snapshot = LiveSessionStore().apply(timing_message)

    assert snapshot.telemetry_samples[0].session_time_seconds == 102.0


def test_position_retention_uses_sample_time_for_delayed_batches():
    def position(t):
        from datetime import timedelta
        return LiveTimingMessage('Position.z', {'Position': [{
            'Timestamp': (NOW + timedelta(seconds=t)).isoformat(),
            'Entries': {'1': {'X': t+1, 'Y': 10, 'Z': 1, 'Status': 'OnTrack'}},
        }]}, NOW, NOW, session_time_seconds=0)
    store = LiveSessionStore(position_sample_limit=3)
    for t in [100, 101, 102, 99, 103, 101]: store.reduce(position(t))
    assert [s.session_time_seconds for s in store.snapshot().position_samples] == [101,102,103]
    store.reduce(position(1400))
    assert [s.session_time_seconds for s in store.snapshot().position_samples] == [1400]
    store.reduce(position(100))
    assert [s.session_time_seconds for s in store.snapshot().position_samples] == [1400]
    latest = LiveSessionStore(position_sample_limit=1)
    for t in [100, 102, 101]: latest.reduce(position(t))
    assert latest.snapshot().position_samples[0].session_time_seconds == 102


def test_position_requires_aware_embedded_time_and_conflicts_remain_unknown():
    def packet(t, x):
        return LiveTimingMessage('Position.z', {'Position': [{
            'Timestamp':t, 'Entries':{'1':{'X':x,'Y':20,'Z':0,'Status':'OnTrack'}},
        }]}, NOW, NOW, session_time_seconds=0)
    store=LiveSessionStore(position_sample_limit=3)
    for t in [None,'invalid','2026-01-01T00:00:01']:
        store.reduce(packet(t,10))
    assert store.snapshot().position_samples == ()
    store.reduce(packet('2026-01-01T00:00:01Z',10))
    store.reduce(packet('2026-01-01T00:00:01Z',11))
    store.reduce(packet('2026-01-01T00:00:01Z',11))
    assert len(store.snapshot().position_samples)==1
    assert store.snapshot().position_samples[0].on_track is None


def test_unrecognised_position_status_remains_unknown():
    for status in ['garbage', '', {}, True, 2, None]:
        store = LiveSessionStore()
        result = store.apply(LiveTimingMessage('Position.z', {'Position': [{
            'Timestamp': NOW.isoformat(),
            'Entries': {'1': {'X': 10, 'Y': 20, 'Z': 1, 'Status': status}},
        }]}, NOW, NOW))
        assert result.position_samples[0].on_track is None, status


def test_only_known_position_statuses_become_booleans():
    for status, expected in [('OnTrack',True),('OffTrack',False),(0,True),(1,False)]:
        result = LiveSessionStore().apply(LiveTimingMessage('Position.z', {'Position': [{
            'Timestamp': NOW.isoformat(),
            'Entries': {'1': {'X': 10, 'Y': 20, 'Z': 1, 'Status': status}},
        }]}, NOW, NOW))
        assert result.position_samples[0].on_track is expected
