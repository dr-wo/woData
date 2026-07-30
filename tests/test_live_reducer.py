from datetime import datetime, timezone

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def message(payload, *, topic="TimingData"):
    return LiveTimingMessage(topic, payload, NOW, NOW)


def test_lap_time_then_count_count_then_time_duplicate_and_correction():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(message({"Lines": {"1": {"LastLapTime": {"Value": "1:30.000"}}}}))
    snapshot = store.apply(message({"Lines": {"1": {"NumberOfLaps": 1}}}))
    assert snapshot.completed_laps[0].lap_time_s == 90.0
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 2}}}))
    snapshot = store.apply(message({"Lines": {"1": {"LastLapTime": {"Value": "1:31.000"}}}}))
    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == [90.0, 181.0]
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "1:31.000"}}}}))
    assert store.duplicate_completed_lap_count == 1
    snapshot = store.apply(message({"Lines": {"1": {"LastLapTime": {"Value": "1:30.500"}}}}))
    assert snapshot.completed_laps[-1].lap_time_s == 90.5
    assert snapshot.completed_laps[-1].cumulative_time_s == 180.5
    assert store.corrected_completed_lap_count == 1


@pytest.mark.parametrize("value", [0, -1, "nan", "invalid"])
def test_invalid_lap_time_is_rejected(value):
    store = LiveSessionStore()
    snapshot = store.apply(message({"Lines": {"1": {"NumberOfLaps": 1, "LastLapTime": {"Value": value}}}}))
    assert snapshot.completed_laps == ()


def test_partial_driver_metadata_is_deep_merged():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    snapshot = store.apply(message({"1": {"FullName": "Driver Alpha"}}, topic="DriverList"))
    assert snapshot.drivers[0].driver_code == "AAA"
    assert snapshot.drivers[0].team == "Mercedes"
    assert snapshot.drivers[0].full_name == "Driver Alpha"


def test_cumulative_time_is_unknown_after_missing_internal_lap():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "1:30.000"}}}}))
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 4, "LastLapTime": {"Value": "1:31.000"}}}}))
    snapshot = store.apply(
        message({"Lines": {"1": {"NumberOfLaps": 5, "LastLapTime": {"Value": "1:32.000"}}}})
    )
    assert [(lap.lap_number, lap.cumulative_time_s) for lap in snapshot.completed_laps] == [
        (2, 90.0),
        (4, None),
        (5, None),
    ]


def test_completed_sectors_are_extracted_when_available():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    snapshot = store.apply(
        message(
            {
                "Lines": {
                    "1": {
                        "NumberOfLaps": 1,
                        "LastLapTime": {"Value": "1:30.000"},
                        "Sectors": [
                            {"Value": "30.000"},
                            {"Value": "31.000"},
                            {"Value": "29.000"},
                        ],
                    }
                }
            }
        )
    )
    assert [(sector.sector, sector.segment_index, sector.cumulative_time_s) for sector in snapshot.completed_sectors] == [
        ("S1", 1, 30.0),
        ("S2", 2, 61.0),
        ("S3", 3, 90.0),
    ]
