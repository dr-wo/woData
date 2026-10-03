import math
from datetime import datetime, timedelta, timezone

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


def source_timed_message(payload, seconds, *, topic="TimingData"):
    return LiveTimingMessage(
        topic, payload, NOW + timedelta(seconds=seconds), NOW,
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
        for sector in partial.provisional_sectors
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
    store.apply(timed_message({"Status": "Started"}, 33.5, topic="SessionStatus"))
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
    store.apply(source_timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(source_timed_message({"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}}, 30.0))
    store.apply(source_timed_message({"Lines": {"1": {"Sectors": {"1": {"Value": "31.000"}}}}}, 61.0))
    store.apply(source_timed_message({"Lines": {"1": {"Sectors": {"2": {"Value": "29.000"}}}}}, 90.0))
    snapshot = store.apply(
        source_timed_message(
            {
                "Lines": {
                    "1": {
                        "NumberOfLaps": 1,
                        "LastLapTime": {"Value": "1:30.000"},
                    }
                }
            }, 90.0,
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


def test_counter_before_delayed_s1_reset_keeps_sectors_on_the_completed_lap():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}, "1": {"Value": ""}, "2": {"Value": ""}},
    }}}, 120.0))
    assert store._pending["1"]["sector_lap"] == 2
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 30.0}, "2": {"Value": 30.0}},
    }}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors] == [
        120.0, 150.0, 180.0,
    ]


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
        for sector in snapshot.provisional_sectors
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
        for sector in snapshot.provisional_sectors
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
    assert [sector.sector for sector in snapshot.provisional_sectors[-3:]] == [
        "S1", "S2", "S3"
    ]


def test_late_connection_sector_has_no_assumed_zero_race_time():
    store = LiveSessionStore()
    store.apply(message({"1": {"Tla": "AAA", "TeamName": "Mercedes"}}, topic="DriverList"))
    store.apply(message({"Lines": {"1": {"NumberOfLaps": 1}}}))

    snapshot = store.apply(
        message({"Lines": {"1": {"Sectors": {"0": {"Value": "30.000"}}}}})
    )

    assert snapshot.completed_laps == ()
    # Counter-before-sector can describe delayed lap-1 evidence, but neither
    # a first-lap origin nor the previous lap's cumulative base is known.
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.provisional_sectors
    ] == [(1, "S1", None)]


@pytest.mark.parametrize("patch", [
    {"PreviousValue": "29.000"},
    {"Segments": {"0": {"Status": 2049}}},
    {},
    {"Value": None},
    {"Value": 0},
    {"Value": "invalid"},
])
def test_sector_metadata_and_invalid_values_do_not_clear_sparse_duration(patch):
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}, "1": {"Value": 31.0}},
    }}}, 61.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": patch},
    }}}, 62.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1,
        "LastLapTime": {"Value": 90.0},
        "Sectors": {"2": {"Value": 29.0}},
    }}}, 90.0))
    assert [sector.sector_time_s for sector in snapshot.completed_sectors] == [
        30.0, 31.0, 29.0,
    ]


def test_explicit_sector_clear_invalidates_previously_complete_pending_tuple():
    store = LiveSessionStore()
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 31.0}, {"Value": 29.0}],
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": ""}},
    }}}, 91.0))
    assert store._pending["1"]["sector_values"] == [30.0, None, 29.0]
    assert store._pending["1"]["sectors"] is None
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 92.0))
    assert len(snapshot.completed_laps) == 1
    assert snapshot.completed_sectors == ()


@pytest.mark.parametrize("delivery", ["clears_before_s1", "s1_before_clears"])
def test_split_trailing_resets_advance_pending_lap_without_false_finite_time(delivery):
    # The counter identifies completed lap 2 even though its LastLapTime is
    # delayed. The same lap-3 S1/reset evidence must work in either patch order.
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 90.0))
    for second, index, value in ((121.0, 0, 31.0), (153.0, 1, 32.0), (186.0, 2, 33.0)):
        store.apply(timed_message({"Lines": {"1": {
            "Sectors": {str(index): {"Value": value}},
        }}}, second))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 186.0))
    assert [
        (sector.lap_number, sector.cumulative_time_s)
        for sector in store.snapshot().provisional_sectors if sector.lap_number == 2
    ] == [(2, 121.0), (2, 153.0), (2, 186.0)]
    unchanged = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"PreviousValue": "31.000"}, "1": {"OverallFastest": False}},
    }}}, 200.0))
    assert [
        (sector.lap_number, sector.sector_time_s, sector.cumulative_time_s)
        for sector in unchanged.provisional_sectors if sector.lap_number == 2
    ] == [(2, 31.0, 121.0), (2, 32.0, 153.0), (2, 33.0, 186.0)]

    clear = {"Sectors": {"1": {"Value": ""}, "2": {"Value": ""}}}
    s1 = {"Sectors": {"0": {"Value": 29.0}}}
    patches = (
        ((214.0, clear), (215.0, s1))
        if delivery == "clears_before_s1"
        else ((215.0, s1), (216.0, clear))
    )
    for second, patch in patches:
        snapshot = store.apply(timed_message({"Lines": {"1": patch}}, second))
        assert not [
            sector for sector in snapshot.completed_sectors
            if sector.lap_number == 2 and sector.sector_time_s == 29.0
        ]
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 32.0}},
    }}}, 247.0))
    lap3 = [sector for sector in snapshot.provisional_sectors if sector.lap_number == 3]
    assert [(sector.sector, sector.sector_time_s) for sector in lap3] == [
        ("S1", 29.0), ("S2", 32.0),
    ]
    assert [sector.cumulative_time_s for sector in lap3] == [215.0, 247.0]
    assert not [
        sector for sector in snapshot.completed_sectors
        if sector.lap_number == 2 and sector.sector_time_s == 29.0
    ]

    # Once a source-backed lap-2 endpoint arrives, lap 3 can be based on it;
    # the late commit must keep the already-received lap-3 sector evidence.
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 96.0},
    }}}, 186.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [(sector.sector, sector.cumulative_time_s) for sector in snapshot.provisional_sectors
            if sector.lap_number == 3] == [("S1", 215.0), ("S2", 247.0)]

    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 3, "LastLapTime": {"Value": 95.0},
        "Sectors": {"2": {"Value": 34.0}},
    }}}, 281.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2, 3]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors
            if sector.lap_number == 3] == [215.0, 247.0, 281.0]


def test_delayed_complete_tuple_stays_live_only_after_completed_lap_recovery():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    intermediate = store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 181.0))
    assert [(lap.lap_number, lap.cumulative_time_s) for lap in intermediate.completed_laps] == [
        (1, 90.0),
    ]
    assert {sector.lap_number for sector in intermediate.completed_sectors} == {1}
    assert not [
        sector for sector in intermediate.completed_sectors
        if sector.cumulative_time_s in (210.0, 240.0, 270.0)
    ]
    unchanged = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"PreviousValue": "30.000"}},
        "GapToLeader": "+1.000",
    }}}, 182.0))
    assert {sector.lap_number for sector in unchanged.completed_sectors} == {1}

    snapshot = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 180.0))
    assert [(lap.lap_number, lap.cumulative_time_s) for lap in snapshot.completed_laps] == [
        (1, 90.0), (2, 180.0),
    ]
    sectors = [sector for sector in snapshot.completed_sectors if sector.lap_number == 2]
    assert sectors == []
    assert snapshot.provisional_sectors
    assert not [sector for sector in snapshot.completed_sectors if sector.lap_number == 3]


@pytest.mark.parametrize("tuple_source_s", [179.0, 180.0])
def test_complete_tuple_at_or_before_counter_stays_on_completed_lap(tuple_source_s):
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, tuple_source_s))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors] == [
        120.0, 150.0, 180.0,
    ]


def test_older_lap_time_copy_cannot_reconcile_provisional_tuple():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 181.0))
    stale = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    assert [lap.lap_number for lap in stale.completed_laps] == [1]
    assert not [sector for sector in stale.completed_sectors if sector.lap_number > 1]
    valid = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 180.0))
    assert [lap.lap_number for lap in valid.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in valid.completed_sectors
            if sector.lap_number == 2] == []


def test_delayed_sector_copy_cannot_rewind_provisional_tuple_provenance():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 181.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 29.0}},
    }}}, 179.0))
    assert store._pending["1"]["sectors_event_time_s"] == 181.0
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors
            if sector.lap_number == 2] == []


def test_duplicate_s1_cannot_rewind_later_provisional_sector_provenance():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 29.0}},
    }}}, 215.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 32.0}},
    }}}, 247.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 29.0}},
    }}}, 215.0))
    transition = store._pending["1"]
    assert transition["sector_values"] == [29.0, 32.0, None]
    assert transition["sectors_event_time_s"] == 247.0
    assert not [sector for sector in store.snapshot().completed_sectors
                if sector.lap_number > 1]


def test_duplicate_reset_and_stale_s1_copies_cannot_advance_twice():
    store = LiveSessionStore()
    store.apply(source_timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(source_timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    for second, index, value in ((121.0, 0, 31.0), (153.0, 1, 32.0), (186.0, 2, 33.0)):
        store.apply(source_timed_message({"Lines": {"1": {
            "Sectors": {str(index): {"Value": value}},
        }}}, second))
    store.apply(source_timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 186.0))
    s1 = {"Lines": {"1": {"Sectors": {"0": {"Value": 29.0}}}}}
    clear = {"Lines": {"1": {"Sectors": {
        "1": {"Value": ""}, "2": {"Value": ""},
    }}}}
    store.apply(source_timed_message(s1, 215.0))
    store.apply(source_timed_message(s1, 215.0))
    store.apply(source_timed_message(clear, 216.0))
    store.apply(source_timed_message(clear, 217.0))
    store.apply(source_timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 32.0}},
    }}}, 247.0))
    store.apply(source_timed_message(s1, 215.0))
    store.apply(source_timed_message(clear, 216.0))
    snapshot = store.snapshot()
    assert store._pending["1"]["sector_lap"] == 3
    assert [(sector.sector, sector.sector_time_s, sector.cumulative_time_s)
            for sector in snapshot.provisional_sectors if sector.lap_number == 3] == [
        ("S1", 29.0, 215.0), ("S2", 32.0, 247.0),
    ]


def test_stale_transition_candidate_cannot_follow_a_newer_counter():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 29.0}},
    }}}, 181.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 3}}}, 270.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": ""}, "2": {"Value": ""}},
    }}}, 271.0))
    assert not [sector for sector in snapshot.completed_sectors if sector.lap_number == 4]
    assert store._pending["1"]["sector_lap"] != 4


def test_unmatched_buffered_tuple_cannot_leak_into_new_s1_after_commit():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 181.0))
    store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 91.0},
    }}}, 180.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 29.0}},
    }}}, 210.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [(sector.lap_number, sector.sector, sector.sector_time_s)
            for sector in snapshot.provisional_sectors] == [(3, "S1", 29.0)]
    assert store._pending["1"]["sector_values"] == [29.0, None, None]


def test_commit_does_not_attach_complete_sectors_from_a_different_lap():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1,
        "Sectors": [{"Value": 30.0}, {"Value": 30.0}, {"Value": 30.0}],
    }}}, 90.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 90.0},
    }}}, 180.0))
    assert [(lap.lap_number, lap.cumulative_time_s) for lap in snapshot.completed_laps] == [
        (2, 180.0),
    ]
    assert snapshot.completed_sectors == ()


def test_source_less_reconnect_patch_cannot_certify_sector_identity():
    # R12's overlap recovery receives this source-less lap-2 snapshot after
    # only a lap-1 S2. Its counter labels the duration, but cannot supply
    # publication boundaries for the sector values in this cached snapshot.
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 27.056}},
    }}}, 60.0))
    snapshot = store.apply(untimed_message({"Lines": {"1": {
        "NumberOfLaps": 2,
        "LastLapTime": {"Value": 123.197},
        "Sectors": [{"Value": 26.899}, {"Value": 40.058}, {"Value": 56.240}],
    }}}))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [2]
    assert snapshot.completed_sectors == ()


def test_delayed_counter_cannot_certify_sectors_spanning_its_boundary():
    # R12 LIN's lap counter is corrected while this coherent sector set is
    # arriving. Sum equality cannot prove S1 before counter 4 belongs to lap 5.
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"41": {
        "NumberOfLaps": 3, "LastLapTime": {"Value": 90.0},
    }}}, 270.0))
    store.apply(timed_message({"Lines": {"41": {
        "Sectors": {"0": {"Value": 31.802}, "1": {"Value": ""}},
    }}}, 301.802))
    store.apply(timed_message({"Lines": {"41": {"NumberOfLaps": 4}}}, 325.0))
    store.apply(timed_message({"Lines": {"41": {
        "Sectors": {"1": {"Value": 28.998}},
    }}}, 330.8))
    snapshot = store.apply(timed_message({"Lines": {"41": {
        "NumberOfLaps": 5, "LastLapTime": {"Value": 87.335},
        "Sectors": {"2": {"Value": 26.535}},
    }}}, 357.335))
    sectors = [sector for sector in snapshot.completed_sectors if sector.lap_number == 5]
    assert sectors == []
    assert snapshot.completed_laps[-1].lap_number == 5
    assert snapshot.completed_laps[-1].lap_time_s == pytest.approx(87.335)


def test_matching_sector_sum_cannot_reconcile_values_before_the_lap_interval():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}, "1": {"Value": 30.0}},
    }}}, 60.0))
    store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 1}}}, 90.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 90.0},
        "Sectors": {"2": {"Value": 30.0}},
    }}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [2]
    assert snapshot.completed_sectors == ()


def test_delayed_previous_lap_commit_preserves_current_lap_sector_evidence():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "Sectors": {"2": {"Value": 30.0}},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}, "1": {"Value": ""}, "2": {"Value": ""}},
    }}}, 120.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1]
    assert [
        (sector.lap_number, sector.sector, sector.cumulative_time_s)
        for sector in snapshot.provisional_sectors
    ] == [(2, "S1", 120.0)]
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 2,
        "Sectors": {"1": {"Value": 30.0}, "2": {"Value": 30.0}},
    }}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors] == [
        120.0, 150.0, 180.0,
    ]


def test_complete_next_lap_survives_delayed_equal_previous_lap_commit():
    store = LiveSessionStore()
    store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    store.apply(timed_message({"Lines": {"1": {
        "NumberOfLaps": 1, "Sectors": {"2": {"Value": 30.0}},
    }}}, 90.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}, "1": {"Value": ""}, "2": {"Value": ""}},
    }}}, 120.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"1": {"Value": 30.0}},
    }}}, 150.0))
    store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"2": {"Value": 30.0}},
    }}}, 180.0))
    store.apply(timed_message({"Lines": {"1": {
        "LastLapTime": {"Value": 90.0},
    }}}, 90.0))
    snapshot = store.apply(timed_message({"Lines": {"1": {"NumberOfLaps": 2}}}, 180.0))
    assert [lap.lap_number for lap in snapshot.completed_laps] == [1, 2]
    assert [sector.cumulative_time_s for sector in snapshot.completed_sectors] == [
        120.0, 150.0, 180.0,
    ]


@pytest.mark.parametrize("started", [False, True])
def test_first_lap_pending_sector_requires_common_race_origin(started):
    store = LiveSessionStore()
    if started:
        store.apply(timed_message({"Status": "Started"}, 0.0, topic="SessionStatus"))
    snapshot = store.apply(timed_message({"Lines": {"1": {
        "Sectors": {"0": {"Value": 30.0}},
    }}}, 30.0))
    assert snapshot.provisional_sectors[0].cumulative_time_s == (30.0 if started else None)


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
