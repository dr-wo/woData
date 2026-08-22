from datetime import datetime, timezone

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def message(payload, *, topic="TimingData", session_time_seconds=None):
    return LiveTimingMessage(
        topic, payload, NOW, NOW, session_time_seconds=session_time_seconds
    )


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


def test_scheduled_total_laps_is_reduced_from_lap_count():
    store = LiveSessionStore()
    snapshot = store.apply(
        message({"CurrentLap": 1, "TotalLaps": 70}, topic="LapCount")
    )
    assert snapshot.scheduled_total_laps == 70
    snapshot = store.apply(message({"CurrentLap": 2}, topic="LapCount"))
    assert snapshot.scheduled_total_laps == 70
    snapshot = store.apply(message({"TotalLaps": 68}, topic="LapCount"))
    assert snapshot.scheduled_total_laps == 70


def test_zero_lap_count_placeholder_does_not_block_first_positive_schedule():
    store = LiveSessionStore()
    assert store.apply(message({"TotalLaps": 0}, topic="LapCount")).scheduled_total_laps is None
    assert store.apply(message({"TotalLaps": 44}, topic="LapCount")).scheduled_total_laps == 44


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
            },
            session_time_seconds=123.5,
        )
    )
    assert [(sector.sector, sector.segment_index, sector.cumulative_time_s) for sector in snapshot.completed_sectors] == [
        ("S1", 1, 30.0),
        ("S2", 2, 61.0),
        ("S3", 3, 90.0),
    ]
    assert snapshot.completed_laps[0].session_time_seconds == 123.5
    assert {sector.session_time_seconds for sector in snapshot.completed_sectors} == {123.5}


def test_sparse_archive_sector_updates_are_accumulated_for_completed_lap():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(message({"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}}))
    store.apply(message({"Lines": {"1": {"Sectors": {"1": {"Value": "31.000"}}}}}))
    store.apply(message({"Lines": {"1": {"Sectors": {"2": {"Value": "29.000"}}}}}))
    snapshot = store.apply(
        message(
            {
                "Lines": {
                    "1": {
                        "NumberOfLaps": 1,
                        "LastLapTime": {"Value": "1:30.000"},
                    }
                }
            }
        )
    )

    assert [sector.sector_time_s for sector in snapshot.completed_sectors] == [30.0, 31.0, 29.0]


def test_current_lap_sectors_are_published_as_each_sector_completes():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(
        message(
            {"Lines": {"1": {"NumberOfLaps": 1, "LastLapTime": {"Value": "1:30"}}}}
        )
    )

    snapshot = store.apply(
        message({"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}})
    )
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.completed_sectors
    ] == [
        (2, "S1", 120.0),
    ]

    snapshot = store.apply(
        message({"Lines": {"1": {"Sectors": {"1": {"Value": "31.000"}}}}})
    )
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.completed_sectors
    ] == [
        (2, "S1", 120.0),
        (2, "S2", 151.0),
    ]

    snapshot = store.apply(
        message({"Lines": {"1": {"Sectors": {"2": {"Value": "29.000"}}}}})
    )
    assert [sector.sector for sector in snapshot.completed_sectors[-3:]] == [
        "S1", "S2", "S3"
    ]


def test_late_connection_publishes_sector_before_any_full_lap_is_available():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 1}}}))

    snapshot = store.apply(
        message({"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}})
    )

    assert snapshot.completed_laps == ()
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.completed_sectors
    ] == [(2, "S1", 30.0)]


def test_retirement_after_completed_lap_stops_later_timing_accumulation():
    store = LiveSessionStore()
    store.apply(message({"27": {"Tla": "HUL", "TeamName": "Audi"}}, topic="DriverList"))
    for lap in range(1, 6):
        store.apply(
            message(
                {"Lines": {"27": {"NumberOfLaps": lap, "LastLapTime": {"Value": "1:20"}}}}
            )
        )

    snapshot = store.apply(
        message({"Lines": {"27": {"Retired": True, "Stopped": True}}})
    )
    assert snapshot.drivers[0].retired is True
    assert snapshot.drivers[0].retirement_lap == 5

    store.apply(message({"Lines": {"27": {"Retired": False, "Stopped": False}}}))
    snapshot = store.apply(
        message(
            {
                "Lines": {
                    "27": {
                        "NumberOfLaps": 6,
                        "LastLapTime": {"Value": "22:31.924"},
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

    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2, 3, 4, 5]
    assert snapshot.completed_laps[-1].cumulative_time_s == 400.0
    assert {sector.lap_number for sector in snapshot.completed_sectors} <= {1, 2, 3, 4, 5}


def test_pre_start_retired_flag_without_completed_lap_is_not_sticky():
    store = LiveSessionStore()
    store.apply(message({"11": {"Tla": "PER", "TeamName": "Cadillac"}}, topic="DriverList"))
    store.apply(message({"Lines": {"11": {"Retired": True, "Stopped": True}}}))
    snapshot = store.apply(
        message(
            {
                "Lines": {
                    "11": {
                        "Retired": False,
                        "Stopped": False,
                        "NumberOfLaps": 1,
                        "LastLapTime": {"Value": "1:21"},
                    }
                }
            }
        )
    )

    assert snapshot.drivers[0].retired is False
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1]


def test_timing_app_stints_assign_compound_and_tyre_life_to_laps():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(
        message(
            {
                "Lines": {
                    "1": {
                        "Stints": {
                            "0": {"Compound": "MEDIUM", "New": "false", "StartLaps": 3}
                        }
                    }
                }
            },
            topic="TimingAppData",
        )
    )
    store.apply(
        message(
            {"Lines": {"1": {"NumberOfLaps": 1, "LastLapTime": {"Value": "1:30"}}}}
        )
    )
    store.apply(
        message(
            {
                "Lines": {
                    "1": {
                        "Stints": {
                            "1": {"Compound": "HARD", "New": "true", "StartLaps": 0}
                        }
                    }
                }
            },
            topic="TimingAppData",
        )
    )
    snapshot = store.apply(
        message(
            {"Lines": {"1": {"NumberOfLaps": 2, "LastLapTime": {"Value": "1:31"}}}}
        )
    )

    first, second = snapshot.completed_laps
    assert (first.compound, first.tyre_life, first.stint, first.fresh_tyre) == (
        "MEDIUM", 4.0, 1, False
    )
    assert (second.compound, second.tyre_life, second.stint, second.fresh_tyre) == (
        "HARD", 1.0, 2, True
    )
