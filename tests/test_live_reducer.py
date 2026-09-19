import math
from datetime import datetime, timezone

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def message(payload, *, topic="TimingData", session_time_seconds=None):
    return LiveTimingMessage(
        topic, payload, NOW, NOW, session_time_seconds=session_time_seconds
    )


def timed_message(payload, seconds, *, topic="TimingData"):
    return LiveTimingMessage(
        topic,
        payload,
        NOW,
        NOW,
        session_time_seconds=float(seconds),
    )


def untimed_message(payload, *, topic="TimingData"):
    return LiveTimingMessage(topic, payload, None, NOW)


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
        (2, None),
        (4, None),
        (5, None),
    ]


@pytest.mark.parametrize("suspension_s", [137.0, 1877.34])
def test_red_flag_suspension_is_excluded_from_canonical_racing_time(suspension_s):
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"1": {"Tla": "AAA"}}, 0.0, topic="DriverList"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 100.0}
    }}}, 190.0))
    store.apply(timed_message({"Status": "Aborted"}, 200.0, topic="SessionStatus"))
    store.apply(timed_message(
        {"Status": "Started"}, 200.0 + suspension_s, topic="SessionStatus"
    ))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 3,
        "LastLapTime": {"Value": suspension_s + 95.0},
    }}}, suspension_s + 285.0))

    assert [lap.lap_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [90.0, 100.0, 95.0]
    )
    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [90.0, 190.0, 285.0]
    )


def test_multiple_red_flags_are_removed_exactly_once():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}, 90.0))
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 150.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 150.0}
    }}}, 240.0))
    store.apply(timed_message({"Status": "Aborted"}, 250.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 370.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 3, "LastLapTime": {"Value": 220.0}
    }}}, 460.0))

    assert store._suspensions == [(100.0, 150.0), (250.0, 370.0)]
    assert [lap.lap_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [90.0, 100.0, 100.0]
    )
    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [90.0, 190.0, 290.0]
    )


def test_duplicate_and_reordered_status_events_are_idempotent():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}, 90.0))
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 200.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 200.0, topic="SessionStatus"))
    # A delayed copy of the old Aborted event must not reopen suspension.
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 200.0}
    }}}, 290.0))

    assert store._suspensions == [(100.0, 200.0)]
    assert store._suspension_started_s is None
    assert snapshot.session_status == "Started"
    assert [lap.lap_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [90.0, 100.0]
    )
    assert snapshot.completed_laps[-1].cumulative_time_s == pytest.approx(190.0)


def test_suspension_intersecting_lap_suppresses_unreliable_sector_breakdown():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1,
        "LastLapTime": {"Value": 90.0},
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 90.0))
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 200.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2,
        "LastLapTime": {"Value": 200.0},
        "Sectors": [{"Value": 60.0}, {"Value": 70.0}, {"Value": 70.0}],
    }}}, 290.0))

    lap2 = next(lap for lap in snapshot.completed_laps if lap.lap_number == 2)
    assert (lap2.lap_time_s, lap2.cumulative_time_s) == pytest.approx((100.0, 190.0))
    assert not [sector for sector in snapshot.completed_sectors if sector.lap_number == 2]


def test_mid_session_without_initial_started_has_no_per_driver_zero_origin():
    store = LiveSessionStore()
    snapshot = store.apply(timed_message({"Lines": {
        "1": {"NumberOfLaps": 8, "LastLapTime": {"Value": 90.0}},
        "2": {"NumberOfLaps": 8, "LastLapTime": {"Value": 91.0}},
    }}, 800.0))

    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == [None, None]


def test_first_started_after_laps_is_restart_not_race_origin():
    store = LiveSessionStore()
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 8, "LastLapTime": {"Value": 90.0}
    }}}, 800.0))
    store.apply(timed_message({"Status": "Started"}, 900.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 9, "LastLapTime": {"Value": 90.0}
    }}}, 990.0))

    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == [None, None]


def test_started_after_aborted_is_restart_without_common_origin():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 200.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 190.0}
    }}}, 290.0))

    assert snapshot.completed_laps[0].cumulative_time_s is None


def test_late_original_started_recovers_origin_from_source_chronology():
    store = LiveSessionStore()
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 8, "LastLapTime": {"Value": 90.0}
    }}}, 800.0))
    assert store.snapshot().completed_laps[0].cumulative_time_s is None

    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 9, "LastLapTime": {"Value": 90.0}
    }}}, 890.0))

    assert store._session_status_state == "running"
    assert store._race_time_origin_s == 0.0
    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [800.0, 890.0]
    )


def test_late_original_started_does_not_rewind_running_status_watermark():
    store = LiveSessionStore()
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 8, "LastLapTime": {"Value": 90.0}
    }}}, 800.0))
    store.apply(timed_message({"Status": "Started"}, 900.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))

    assert store._race_time_origin_s == 0.0
    assert store._session_status_state == "running"
    assert store._last_session_status_event_s == 900.0


def test_reordered_old_started_does_not_rewind_suspended_state():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}, 90.0))
    store.apply(timed_message({"Status": "Aborted"}, 100.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))

    assert store._session_status_state == "suspended"
    assert store._suspension_started_s == 100.0
    assert store._last_session_status_event_s == 100.0


def test_unanchored_restart_lap_is_pending_then_recovered_from_next_endpoint():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 100.0}
    }}}, 190.0))
    store.apply(timed_message({"Status": "Aborted"}, 200.0, topic="SessionStatus"))
    store.apply(timed_message({"Status": "Started"}, 1852.0, topic="SessionStatus"))
    partial = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 600.0}},
    }}}, 1942.0))
    pending_sector = next(
        sector
        for sector in partial.completed_sectors
        if sector.lap_number == 3 and sector.sector == "S1"
    )
    assert pending_sector.sector_time_s == 600.0
    assert pending_sector.cumulative_time_s is None

    pending = store.apply(untimed_message({"Lines": {"1": {
        "NumberOfLaps": 3,
        "LastLapTime": {"Value": 1752.0},
        "Sectors": [{"Value": 600.0}, {"Value": 600.0}, {"Value": 552.0}],
    }}}))

    lap3 = next(lap for lap in pending.completed_laps if lap.lap_number == 3)
    assert math.isnan(lap3.lap_time_s)
    assert lap3.cumulative_time_s is None
    assert lap3.is_valid is False

    recovered = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 4, "LastLapTime": {"Value": 100.0}
    }}}, 2042.0))
    by_lap = {lap.lap_number: lap for lap in recovered.completed_laps}
    assert (by_lap[3].lap_time_s, by_lap[3].cumulative_time_s) == pytest.approx(
        (100.0, 290.0)
    )
    assert by_lap[4].cumulative_time_s == pytest.approx(390.0)
    assert not [sector for sector in recovered.completed_sectors if sector.lap_number == 3]


def test_common_origin_preserves_r13_pairwise_order_until_real_crossing():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message(
        {"1": {"Tla": "RUS"}, "12": {"Tla": "ANT"}}, 0.0, topic="DriverList"
    ))
    for lap in range(2, 19):
        store.apply(timed_message({"Lines": {"1": {
            "NumberOfLaps": lap, "LastLapTime": {"Value": 90.0}
        }}}, lap * 90.0))
    for lap in range(2, 19):
        gap = 6.6 if lap <= 5 else 6.6 - 0.5 * (lap - 5)
        if lap == 18:
            gap = -0.4
        ant_end = lap * 90.0 + gap
        store.apply(timed_message({"Lines": {"12": {
            "NumberOfLaps": lap,
            "LastLapTime": {
                "Value": 90.0 if lap <= 5 else (89.5 if lap <= 17 else 89.0)
            },
        }}}, ant_end))

    completed = store.snapshot().completed_laps
    by_driver_lap = {
        (lap.driver_code, lap.lap_number): lap.cumulative_time_s
        for lap in completed
    }
    independently_zeroed_l17 = {
        driver: sum(
            lap.lap_time_s
            for lap in completed
            if lap.driver_code == driver and lap.lap_number <= 17
        )
        for driver in ("RUS", "ANT")
    }
    assert independently_zeroed_l17["ANT"] - independently_zeroed_l17["RUS"] == pytest.approx(-6.0)
    assert by_driver_lap[("ANT", 6)] - by_driver_lap[("RUS", 6)] == pytest.approx(6.1)
    assert by_driver_lap[("ANT", 17)] - by_driver_lap[("RUS", 17)] == pytest.approx(0.6)
    assert by_driver_lap[("ANT", 18)] - by_driver_lap[("RUS", 18)] == pytest.approx(-0.4)
    common_reference = 1500.0
    assert (
        (by_driver_lap[("ANT", 17)] - common_reference)
        - (by_driver_lap[("RUS", 17)] - common_reference)
    ) == pytest.approx(0.6)


def test_common_origin_preserves_r14_rus_ahead_of_pia_at_lap_30():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message(
        {"1": {"Tla": "RUS"}, "81": {"Tla": "PIA"}}, 0.0, topic="DriverList"
    ))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 30, "LastLapTime": {"Value": 90.0}
    }}}, 2702.0))
    snapshot = store.apply(timed_message({"Lines": {"81": {
        "NumberOfLaps": 30, "LastLapTime": {"Value": 90.0}
    }}}, 2704.0))

    cumulative = {lap.driver_code: lap.cumulative_time_s for lap in snapshot.completed_laps}
    assert cumulative["RUS"] == pytest.approx(2702.0)
    assert cumulative["PIA"] == pytest.approx(2704.0)
    assert cumulative["RUS"] - cumulative["PIA"] == pytest.approx(-2.0)
    assert {
        lap.driver_code: lap.lap_time_s for lap in snapshot.completed_laps
    } == {"RUS": 90.0, "PIA": 90.0}


def test_unchanged_merged_lap_time_commits_when_complete_sectors_match():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"1": {"Tla": "VER"}}, 0.0, topic="DriverList"))
    sectors = [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}]
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 35, "LastLapTime": {"Value": 90.0}, "Sectors": sectors,
    }}}, 3150.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 36, "Sectors": sectors,
    }}}, 3240.0))
    assert [(lap.lap_number, lap.lap_time_s) for lap in snapshot.completed_laps] == [
        (35, 90.0), (36, 90.0)
    ]

    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 37, "LastLapTime": {"Value": 91.0},
    }}}, 3331.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [35, 36, 37]
    assert [lap.cumulative_time_s for lap in snapshot.completed_laps] == pytest.approx(
        [3150.0, 3240.0, 3331.0]
    )


def test_omitted_lap_time_without_matching_complete_sectors_is_not_committed():
    store = LiveSessionStore()
    store.apply(message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}
    }}}))
    snapshot = store.apply(message({"Lines": {"1": {
        "NumberOfLaps": 2,
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}],
    }}}))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1]


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


def test_equal_lap_time_is_recovered_when_counter_precedes_sectors():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    sectors = [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}]
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}, "Sectors": sectors,
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {"Sectors": sectors}}}, 179.0))

    assert [(lap.lap_number, lap.lap_time_s) for lap in snapshot.completed_laps] == [
        (1, 90.0), (2, 90.0)
    ]
    lap2_sectors = [
        sector for sector in snapshot.completed_sectors if sector.lap_number == 2
    ]
    assert [sector.sector for sector in lap2_sectors] == ["S1", "S2", "S3"]
    assert lap2_sectors[-1].cumulative_time_s == pytest.approx(180.0)


def test_equal_lap_time_is_recovered_when_sectors_precede_counter():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    sectors = [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}]
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}, "Sectors": sectors,
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"Sectors": sectors}}}, 180.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))

    assert [(lap.lap_number, lap.lap_time_s) for lap in snapshot.completed_laps] == [
        (1, 90.0), (2, 90.0)
    ]
    assert [
        sector.sector for sector in snapshot.completed_sectors
        if sector.lap_number == 2
    ] == ["S1", "S2", "S3"]


def test_delayed_previous_lap_sectors_do_not_attach_to_fresh_counter():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    sectors = [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}]
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}, "Sectors": sectors,
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {"Sectors": sectors}}}, 89.0))

    assert [lap.lap_number for lap in snapshot.completed_laps] == [1]
    assert not [sector for sector in snapshot.completed_sectors if sector.lap_number == 2]

    snapshot = store.apply(timed_message({"Lines": {"1": {"Sectors": sectors}}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]


def test_delayed_sectors_between_commits_cannot_authorize_next_equal_lap():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1,
        "LastLapTime": {"Value": 90.0},
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 90.0))
    lap2_sectors = [{"Value": 25.0}, {"Value": 25.0}, {"Value": 40.0}]
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2,
        "LastLapTime": {"Value": 90.0},
        "Sectors": lap2_sectors,
    }}}, 180.0))

    # Delayed source evidence is received after commit, but its source time is
    # still the already-committed L2 boundary.
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": lap2_sectors,
    }}}, 180.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 3,
    }}}, 270.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]

    lap3_sectors = [{"Value": 30.0}, {"Value": 20.0}, {"Value": 40.0}]
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": lap3_sectors,
    }}}, 270.0))

    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2, 3]
    assert [
        sector.sector_time_s
        for sector in snapshot.completed_sectors
        if sector.lap_number == 3
    ] == [30.0, 20.0, 40.0]
    assert not [sector for sector in snapshot.completed_sectors if sector.lap_number == 4]


def test_current_lap_sectors_are_published_as_each_sector_completes():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(
        timed_message(
            {"Lines": {"1": {"NumberOfLaps": 1, "LastLapTime": {"Value": "1:30"}}}},
            90.0,
        )
    )

    snapshot = store.apply(
        timed_message(
            {"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}},
            120.0,
        )
    )
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.completed_sectors
    ] == [
        (2, "S1", 120.0),
    ]

    snapshot = store.apply(
        timed_message(
            {"Lines": {"1": {"Sectors": {"1": {"Value": "31.000"}}}}},
            151.0,
        )
    )
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.completed_sectors
    ] == [
        (2, "S1", 120.0),
        (2, "S2", 151.0),
    ]

    snapshot = store.apply(
        timed_message(
            {"Lines": {"1": {"Sectors": {"2": {"Value": "29.000"}}}}},
            180.0,
        )
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
    ] == [(1, "S1", 30.0)]


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
