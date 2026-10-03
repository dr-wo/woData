"""Chronology attacks for the provisional/canonical boundary.

Case numbers correspond to the user's 26-item adversarial matrix. Tests use
source clocks as well as session event clocks so sparse patch order cannot hide
behind identical source timestamps.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage


ORIGIN = datetime(2026, 7, 25, tzinfo=timezone.utc)


def apply(store, second, patch, *, topic="TimingData"):
    payload = {"Lines": {"1": patch}} if topic == "TimingData" else patch
    timestamp = None if second is None else ORIGIN + timedelta(seconds=second)
    return store.apply(LiveTimingMessage(
        topic, payload, timestamp, ORIGIN, session_time_seconds=second,
    ))


def sector(index, value):
    return {"Sectors": {str(index): {"Value": value}}}


def tuple_patch(values=(30.0, 30.0, 30.0)):
    return {"Sectors": [{"Value": value} for value in values]}


def lap_sectors(store, lap):
    return [entry for entry in store.snapshot().completed_sectors
            if entry.driver_number == "1" and entry.lap_number == lap]


def live_sectors(store, lap):
    return [entry for entry in store.snapshot().provisional_sectors
            if entry.driver_number == "1" and entry.lap_number == lap]


def lap_numbers(store):
    return [entry.lap_number for entry in store.snapshot().completed_laps]


def baseline(*, lap2=False, lap1_sectors=False):
    store = LiveSessionStore()
    apply(store, 0.0, {"Status": "Started"}, topic="SessionStatus")
    patch = {"NumberOfLaps": 1, "LastLapTime": {"Value": 90.0}}
    if lap1_sectors:
        patch.update(tuple_patch())
    apply(store, 90.0, patch)
    if lap2:
        for second, index, value in ((121.0, 0, 31.0), (153.0, 1, 32.0),
                                     (186.0, 2, 33.0)):
            apply(store, second, sector(index, value))
        apply(store, 186.0, {"NumberOfLaps": 2})
    return store


def clear(*indices):
    return {"Sectors": {str(index): {"Value": ""} for index in indices}}


def assert_identified3(store, values):
    pending = store._pending["1"]
    assert pending["sector_lap"] == 3
    assert pending["sector_values"] == values
    assert not lap_sectors(store, 3)
    assert [entry.sector_time_s for entry in live_sectors(store, 3)] == [
        value for value in values[:next((i for i, value in enumerate(values) if value is None), 3)]
    ]


def finish3(store):
    apply(store, 186.0, {"LastLapTime": {"Value": 96.0}})
    assert lap_numbers(store) == [1, 2]
    return apply(store, 281.0, {
        "NumberOfLaps": 3, "LastLapTime": {"Value": 95.0},
        "Sectors": {"2": {"Value": 34.0}},
    })


@pytest.mark.parametrize("case,tuple_second,tuple_first", [
    (1, 181.0, False), (2, 179.0, True), (3, 179.0, False),
    (4, 180.0, False), (5, 181.0, False), (6, 181.0, True),
])
def test_completed_lap_recovery_cases_01_06(case, tuple_second, tuple_first):
    store = baseline()
    events = [(180.0, {"NumberOfLaps": 2}), (tuple_second, tuple_patch())]
    if tuple_first:
        events.reverse()
    for second, patch in events:
        apply(store, second, patch)
    if tuple_second > 180.0:
        # Same source evidence must stage regardless of transport delivery.
        assert lap_numbers(store) == [1], f"case {case}: no fresh lap2 time yet"
        assert not lap_sectors(store, 2), f"case {case}: ambiguous tuple published"
        assert not lap_sectors(store, 3)
    apply(store, 180.0, {"LastLapTime": {"Value": 90.0}})
    assert lap_numbers(store) == [1, 2]
    assert [entry.cumulative_time_s for entry in lap_sectors(store, 2)] == (
        [120.0, 150.0, 180.0] if tuple_second <= 180.0 else []
    )


@pytest.mark.parametrize("case,events,expected", [
    (7, [(214.0, clear(1, 2)), (215.0, sector(0, 29.0))], [29.0, None, None]),
    (8, [(215.0, sector(0, 29.0)), (216.0, clear(1, 2))], [29.0, None, None]),
    (9, [(214.0, clear(1, 2)), (215.0, sector(0, 29.0)),
         (247.0, sector(1, 32.0))], [29.0, 32.0, None]),
    (10, [(215.0, sector(0, 29.0)), (247.0, sector(1, 32.0)),
          (216.0, clear(1, 2))], [29.0, 32.0, None]),
    (11, [(215.0, sector(0, 29.0)), (247.0, sector(1, 32.0)),
          (248.0, clear(1, 2))], [29.0, None, None]),
    (12, [(215.0, sector(0, 29.0)), (216.0, clear(1, 2)),
          (216.0, clear(1, 2)), (217.0, clear(1, 2)),
          (247.0, sector(1, 32.0)), (216.0, clear(1, 2))], [29.0, 32.0, None]),
    (13, [(215.0, sector(0, 29.0)), (216.0, clear(2)),
          (247.0, sector(1, 32.0))], [29.0, 32.0, None]),
])
def test_new_lap_transition_cases_07_13(case, events, expected):
    store = baseline(lap2=True)
    for second, patch in events:
        apply(store, second, patch)
    assert_identified3(store, expected)
    assert not any(entry.sector_time_s == 29.0 for entry in lap_sectors(store, 2))
    if case == 10:
        # Exact reviewer reproduction: stale lap2 S2 must never return.
        apply(store, 153.0, sector(1, 32.0))
        assert_identified3(store, expected)
        finish3(store)
        assert [entry.cumulative_time_s for entry in lap_sectors(store, 3)] == [
            215.0, 247.0, 281.0,
        ]
    elif case == 11:
        # Symmetry: a newer reset destroys only addressed older evidence.
        apply(store, 247.0, sector(1, 32.0))
        assert_identified3(store, expected)
        finish3(store)
        assert lap_numbers(store) == [1, 2, 3]
        assert not lap_sectors(store, 3)


@pytest.mark.parametrize("case,index,older_second,older_value", [
    (14, 0, 214.0, 28.0), (15, 1, 246.0, 31.0),
])
def test_stale_individual_sector_cases_14_15(case, index, older_second, older_value):
    store = baseline(lap2=True)
    apply(store, 214.0, clear(1, 2))
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 247.0, sector(1, 32.0))
    apply(store, older_second, sector(index, older_value))
    assert_identified3(store, [29.0, 32.0, None])
    finish3(store)
    assert [entry.cumulative_time_s for entry in lap_sectors(store, 3)] == [215, 247, 281]


def test_stale_complete_tuple_case_16():
    store = baseline(lap2=True)
    apply(store, 214.0, clear(1, 2))
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 247.0, sector(1, 32.0))
    apply(store, 186.0, tuple_patch((31.0, 32.0, 33.0)))
    assert_identified3(store, [29.0, 32.0, None])
    finish3(store)
    assert [entry.cumulative_time_s for entry in lap_sectors(store, 3)] == [215, 247, 281]


def test_duplicate_sector_case_17():
    store = baseline(lap2=True)
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 247.0, sector(1, 32.0))
    apply(store, 215.0, sector(0, 29.0))
    staged = store._pending["1"]
    assert staged["sector_values"] == [29.0, 32.0, None]
    assert staged["sector_event_times_s"] == [215.0, 247.0, None]
    assert staged["sectors_event_time_s"] == 247.0
    assert not lap_sectors(store, 3)


def test_duplicate_counter_case_18():
    store = baseline(lap2=True)
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 216.0, clear(1, 2))
    for _ in range(2):
        apply(store, 186.0, {"NumberOfLaps": 2})
    assert_identified3(store, [29.0, None, None])
    assert lap_numbers(store) == [1]


@pytest.mark.parametrize("lap2_committed", [True, False])
def test_delayed_old_lap_time_case_19(lap2_committed):
    store = baseline(lap2=True)
    if lap2_committed:
        apply(store, 186.0, {"LastLapTime": {"Value": 96.0}})
    apply(store, 281.0, {"NumberOfLaps": 3})
    apply(store, 186.0, {"LastLapTime": {"Value": 96.0}})
    assert lap_numbers(store) == [1, 2]
    apply(store, 281.0, {"LastLapTime": {"Value": 95.0}})
    assert [(entry.lap_number, entry.lap_time_s, entry.cumulative_time_s)
            for entry in store.snapshot().completed_laps] == [
        (1, 90.0, 90.0), (2, 96.0, 186.0), (3, 95.0, 281.0),
    ]


@pytest.mark.parametrize("case,patch", [
    (20, {"GapToLeader": "+1.000"}),
    (21, {"Sectors": {"0": {"Segments": {"0": {"Status": 2049}}},
                      "1": {"OverallFastest": False}}}),
    (22, {"Sectors": {"0": {"PreviousValue": "28.000"},
                      "1": {"Value": None, "PreviousValue": "31.000"}}}),
])
def test_sparse_non_evidence_cases_20_22(case, patch):
    store = baseline(lap2=True)
    apply(store, 214.0, clear(1, 2))
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 247.0, sector(1, 32.0))
    apply(store, 250.0, patch)
    assert_identified3(store, [29.0, 32.0, None])
    assert store._pending["1"]["sector_event_times_s"][:2] == [215.0, 247.0]


def test_no_committed_cumulative_base_case_23():
    store = LiveSessionStore()
    apply(store, 90.0, {"NumberOfLaps": 1})
    apply(store, 90.0, sector(0, 30.0))
    assert lap_numbers(store) == []
    assert [(entry.lap_number, entry.sector, entry.cumulative_time_s)
            for entry in live_sectors(store, 1)] == [(1, "S1", None)]


def test_late_connection_case_24():
    store = LiveSessionStore()
    apply(store, 800.0, {"NumberOfLaps": 8, "LastLapTime": {"Value": 90.0}})
    apply(store, 830.0, sector(0, 30.0))
    assert [(entry.lap_number, entry.cumulative_time_s)
            for entry in store.snapshot().completed_laps] == [(8, None)]
    assert [(entry.lap_number, entry.sector_time_s, entry.cumulative_time_s)
            for entry in live_sectors(store, 9)] == [(9, 30.0, None)]
    apply(store, 900.0, {"Status": "Started"}, topic="SessionStatus")
    assert store._race_time_origin_s is None
    assert live_sectors(store, 9)[0].cumulative_time_s is None


def test_source_less_restart_case_25():
    store = baseline()
    apply(store, 190.0, {"NumberOfLaps": 2, "LastLapTime": {"Value": 100.0}})
    apply(store, 200.0, {"Status": "Aborted"}, topic="SessionStatus")
    apply(store, 1852.0, {"Status": "Started"}, topic="SessionStatus")
    apply(store, None, {"NumberOfLaps": 3, "LastLapTime": {"Value": 1752.0},
                        **tuple_patch((600.0, 600.0, 552.0))})
    lap3 = store.snapshot().completed_laps[-1]
    assert lap3.lap_number == 3 and math.isnan(lap3.lap_time_s)
    assert lap3.cumulative_time_s is None and not lap3.is_valid
    assert not lap_sectors(store, 3)
    apply(store, 2042.0, {"NumberOfLaps": 4, "LastLapTime": {"Value": 100.0},
                        **tuple_patch((30.0, 30.0, 40.0))})
    lap3, lap4 = store.snapshot().completed_laps[-2:]
    assert (lap3.lap_time_s, lap3.cumulative_time_s, lap4.cumulative_time_s) == (100, 290, 390)
    assert not lap_sectors(store, 3)
    assert not lap_sectors(store, 4)  # retro-anchoring does not certify sectors


def test_suspension_restart_with_staged_evidence_case_26():
    store = baseline(lap2=True)
    apply(store, 215.0, sector(0, 29.0))
    assert store._pending["1"]["sector_values"] == [29.0, None, None]
    apply(store, 220.0, {"Status": "Aborted"}, topic="SessionStatus")
    apply(store, 320.0, {"Status": "Started"}, topic="SessionStatus")
    apply(store, 216.0, clear(1, 2))
    assert_identified3(store, [29.0, None, None])
    # S2's wall-clock duration spans the 100s pause; it is unsafe on the
    # canonical racing axis until that lap is resolved.
    apply(store, 347.0, sector(1, 132.0))
    assert not lap_sectors(store, 3)
    assert all(entry.cumulative_time_s is None for entry in live_sectors(store, 3))
    apply(store, 186.0, {"LastLapTime": {"Value": 96.0}})
    assert not lap_sectors(store, 3)
    assert all(entry.cumulative_time_s is None for entry in live_sectors(store, 3))
    apply(store, 381.0, {"NumberOfLaps": 3, "LastLapTime": {"Value": 195.0},
                        "Sectors": {"2": {"Value": 34.0}}})
    lap3 = store.snapshot().completed_laps[-1]
    assert (lap3.lap_time_s, lap3.cumulative_time_s) == (95.0, 281.0)
    assert not lap_sectors(store, 3)


def test_trailing_clear_before_older_unaddressed_s1_delivery():
    store = baseline(lap2=True)
    apply(store, 216.0, clear(1, 2))
    apply(store, 215.0, sector(0, 29.0))
    assert_identified3(store, [29.0, None, None])


@pytest.mark.parametrize("events", [
    [(248.0, clear(1)), (247.0, sector(1, 32.0))],
    [(247.0, sector(1, 32.0)), (248.0, clear(1))],
])
def test_newer_clear_tombstone_prevents_value_resurrection(events):
    store = baseline(lap2=True)
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 216.0, clear(1, 2))
    for second, patch in events:
        apply(store, second, patch)
    assert_identified3(store, [29.0, None, None])


def test_reverse_transport_sparse_same_lap_values():
    store = baseline()
    for second, index, value in ((153.0, 1, 32.0), (121.0, 0, 31.0),
                                 (186.0, 2, 33.0)):
        apply(store, second, sector(index, value))
    assert store._pending["1"]["sector_values"] == [31.0, 32.0, 33.0]
    apply(store, 186.0, {"NumberOfLaps": 2, "LastLapTime": {"Value": 96.0}})
    assert [entry.cumulative_time_s for entry in lap_sectors(store, 2)] == [121, 153, 186]


@pytest.mark.parametrize("identified", [False, True])
def test_stale_complete_tuple_routes_older_candidate_without_erasing_next(identified):
    store = baseline()
    apply(store, 186.0, {"NumberOfLaps": 2})
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 247.0, sector(1, 32.0))
    if identified:
        apply(store, 216.0, clear(2))
    apply(store, 186.0, tuple_patch((31.0, 32.0, 33.0)))
    if identified:
        assert_identified3(store, [29.0, 32.0, None])
    else:
        assert store._pending["1"]["sector_values"] == [29.0, 32.0, None]
    apply(store, 186.0, {"LastLapTime": {"Value": 96.0}})
    assert lap_sectors(store, 2) == []  # older ambiguous detail may be omitted
    if not identified:
        apply(store, 216.0, clear(2))
    assert store._pending["1"]["sector_lap"] == 3
    assert store._pending["1"]["sector_values"] == [29.0, 32.0, None]


def test_duplicate_counter_publication_cannot_move_known_boundary():
    store = baseline(lap2=True)
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 217.0, {"NumberOfLaps": 2})
    apply(store, 216.0, clear(1, 2))
    assert_identified3(store, [29.0, None, None])


def test_late_equal_tuple_cannot_be_identified_only_by_lap_time_sum():
    store = baseline()
    apply(store, 180.0, {"NumberOfLaps": 2})
    # Both an old lap2 snapshot and a genuinely completed 90s lap3 are
    # compatible with this source clock. Sum equality is not lap identity.
    apply(store, 270.0, tuple_patch())
    apply(store, 180.0, {"LastLapTime": {"Value": 90.0}})
    assert lap_numbers(store) == [1, 2]
    assert not lap_sectors(store, 2)
    assert not lap_sectors(store, 3)
    assert store.snapshot().provisional_sectors


def test_live_estimate_is_revised_and_removed_without_creating_history():
    store = baseline()
    apply(store, 120.0, sector(0, 30.0))
    first = store.snapshot()
    assert first.provisional_sectors[0].cumulative_time_s == 120.0
    assert first.completed_sectors == ()
    apply(store, 121.0, sector(0, 31.0))
    revised = store.snapshot()
    assert revised.provisional_sectors[0].cumulative_time_s == 121.0
    assert revised.completed_laps == first.completed_laps
    apply(store, 122.0, clear(0))
    assert store.snapshot().provisional_sectors == ()
    assert store.snapshot().completed_laps == first.completed_laps
    assert store.snapshot().completed_sectors == ()


def test_reconciled_completion_replaces_provisional_points_only_for_its_lap():
    store = baseline()
    for second, index, value in ((120.0, 0, 30.0), (150.0, 1, 30.0)):
        apply(store, second, sector(index, value))
    assert len(store.snapshot().provisional_sectors) == 2
    snapshot = apply(store, 180.0, {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 90.0},
        "Sectors": {"2": {"Value": 30.0}},
    })
    assert snapshot.provisional_sectors == ()
    assert [entry.cumulative_time_s for entry in snapshot.completed_sectors] == [120, 150, 180]
    committed = snapshot.completed_sectors
    apply(store, 210.0, sector(0, 29.0))
    apply(store, 150.0, clear(1, 2))
    assert store.snapshot().completed_sectors == committed


def test_official_register_freshness_is_preserved_per_field():
    store = baseline()
    apply(store, 125.0, {"Position": 2, "GapToLeader": "+1.5"})
    apply(store, 124.0, {"Position": 3, "GapToLeader": "+2.5"})
    apply(store, 126.0, {"IntervalToPositionAhead": {"Value": "+0.7"}})
    state = store.snapshot().live_timing[0]
    assert (state.position, state.gap_to_leader_s, state.interval_to_position_ahead_s) == (2, 1.5, 0.7)
    assert (state.position_source_time_s, state.gap_source_time_s,
            state.interval_source_time_s) == (125, 125, 126)


def test_unknown_official_gap_is_not_fabricated_for_leader():
    store = baseline()
    apply(store, 100.0, {"Position": 1, "GapToLeader": "unknown"})
    assert store.snapshot().live_timing[0].gap_to_leader_s is None
    apply(store, 101.0, {"GapToLeader": "LAP 2"})
    assert store.snapshot().live_timing[0].gap_to_leader_s == 0


def test_clear_before_older_s1_keeps_tombstone_when_live_generation_changes():
    store = baseline(lap2=True)
    apply(store, 216.0, clear(1, 2))
    apply(store, 215.0, sector(0, 29.0))
    apply(store, 210.0, sector(1, 32.0))
    assert [entry.sector for entry in live_sectors(store, 3)] == ["S1"]
    finish3(store)
    assert not lap_sectors(store, 3)


def test_delayed_previous_lap_sectors_cannot_certify_next_equal_duration_lap():
    store = baseline()
    for second, index, value in ((20, 0, 20), (50, 1, 30), (90, 2, 40)):
        apply(store, second, sector(index, value))
    # The display may estimate lap 2, but its inferred identity is not proof.
    assert [entry.cumulative_time_s for entry in live_sectors(store, 2)] == [110, 140, 180]
    apply(store, 180, {"NumberOfLaps": 2, "LastLapTime": {"Value": 90}})
    assert lap_numbers(store) == [1, 2]
    assert lap_sectors(store, 2) == []


def test_later_provisional_generation_cannot_certify_delayed_older_completion():
    store = baseline()
    apply(store, 180, {"NumberOfLaps": 2})
    apply(store, 215, sector(0, 29))
    apply(store, 247, sector(1, 32))
    apply(store, 281, {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 95},
        "Sectors": {"2": {"Value": 34}},
    })
    assert lap_numbers(store) == [1, 2]
    assert lap_sectors(store, 2) == []
    assert [entry.sector_time_s for entry in live_sectors(store, 3)] == [29, 32, 34]


@pytest.mark.parametrize("previous_counter", [False, True])
def test_first_known_completion_requires_independent_previous_boundary(previous_counter):
    store = LiveSessionStore()
    apply(store, 0, {"Status": "Started"}, topic="SessionStatus")
    if previous_counter:
        apply(store, 90, {"NumberOfLaps": 1})
    apply(store, 120, sector(0, 30))
    apply(store, 150, sector(1, 30))
    apply(store, 180, {
        "NumberOfLaps": 2, "LastLapTime": {"Value": 90},
        "Sectors": {"2": {"Value": 30}},
    })
    assert lap_numbers(store) == [2]  # No invented lap 1.
    assert [entry.cumulative_time_s for entry in lap_sectors(store, 2)] == (
        [120, 150, 180] if previous_counter else []
    )


def test_inferred_race_origin_is_not_previous_boundary_for_first_lap_sectors():
    store = LiveSessionStore()
    apply(store, 90, {
        "NumberOfLaps": 1, "LastLapTime": {"Value": 90},
        **tuple_patch((20, 30, 40)),
    })
    assert store._race_time_origin_s == 0  # Existing lap-axis behavior is retained.
    assert lap_sectors(store, 1) == []
