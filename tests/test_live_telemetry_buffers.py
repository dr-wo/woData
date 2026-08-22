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
