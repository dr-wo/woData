"""Adversarial ownership regressions from the R16 blocker review."""
from datetime import datetime, timezone

import pytest

from wodata.live import LiveTimingMessage, QualiSessionStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SECTORS_A = {str(i): {"Value": str(value)} for i, value in enumerate((20, 30, 40))}


def apply(store, patch, seconds, *, topic="TimingData"):
    payload = {"Lines": {"31": patch}} if topic == "TimingData" else patch
    return store.apply(LiveTimingMessage(topic, payload, NOW, NOW,
                                        session_time_seconds=float(seconds)))


def complete(store, counter=3, seconds=100, *, sectors=None, alias=None):
    patch = {"InPit": False, "NumberOfLaps": counter,
             "LastLapTime": {"Value": "1:30"}, "BestLapTime": {"Value": "1:30"}}
    if alias is not None:
        patch["BestLapTime"]["Lap"] = alias
    if sectors is not None:
        patch["Sectors"] = sectors
    return apply(store, patch, seconds)


@pytest.mark.parametrize("original_source_time", [False, True])
@pytest.mark.parametrize("retract", [False, True])
@pytest.mark.parametrize("partial_duplicate", [False, True])
def test_owned_sector_redelivery_never_seeds_another_completed_event(
        original_source_time, retract, partial_duplicate):
    store = QualiSessionStore()
    first = complete(store, sectors=SECTORS_A)
    key = first.completed_lap_sequence[0].event_key
    if retract:
        deleted = apply(store, {"NumberOfLaps": 3, "LastLapTime": {"Value": ""}}, 101)
        assert deleted.drivers[0].best_lap_event_key is None
    apply(store, {"Position": 1}, 150)
    sectors = {"2": SECTORS_A["2"]} if partial_duplicate else SECTORS_A
    duplicate = apply(store, {"Sectors": sectors}, 100 if original_source_time else 151)
    assert duplicate.completed_lap_sequence[0].event_key == key
    assert duplicate.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    if retract:
        assert duplicate.drivers[0].best_lap_event_key is None
    later = complete(store, counter=4, seconds=200, alias=3)
    assert later.completed_lap_sequence[0].event_key == key
    assert later.completed_lap_sequence[1].event_key != key
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


@pytest.mark.parametrize("parts", [("Q1", "Q2"), ("Q2", "Q3")])
@pytest.mark.parametrize("original_source_time", [False, True])
def test_old_part_redelivery_cannot_own_new_part_event(parts, original_source_time):
    store = QualiSessionStore()
    apply(store, {"SessionPart": parts[0]}, 0, topic="SessionInfo")
    first = complete(store, sectors=SECTORS_A, alias=2)
    key = first.completed_lap_sequence[0].event_key
    apply(store, {"SessionPart": parts[1]}, 110, topic="SessionInfo")
    apply(store, {"Position": 1}, 150)
    apply(store, {"Sectors": SECTORS_A}, 100 if original_source_time else 151)
    later = complete(store, counter=4, seconds=220, alias=3)
    assert later.completed_lap_sequence[0].event_key == key
    assert later.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    assert later.completed_lap_sequence[1].event_key[0] == parts[1]
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


def test_missing_trailing_sector_enriches_the_bound_cycle_without_changing_key():
    store = QualiSessionStore()
    first = complete(store, sectors={"0": SECTORS_A["0"], "1": SECTORS_A["1"]})
    key = first.completed_lap_sequence[0].event_key
    # Advancing the reducer clock must not relabel the open sector cycle.
    apply(store, {"Position": 1}, 150)
    enriched = apply(store, {"Sectors": {"2": SECTORS_A["2"]}}, 151)
    assert enriched.completed_lap_sequence[0].event_key == key
    assert enriched.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    assert enriched.drivers[0].best_lap_event_key == key
    assert first.completed_lap_sequence[0].sectors_s == (20, 30, None)


def test_unanchored_partial_sector_packet_remains_unresolved():
    store = QualiSessionStore()
    complete(store, sectors=SECTORS_A)
    apply(store, {"Sectors": {"1": {"Value": "29"}, "2": {"Value": "41"}}}, 151)
    later = complete(store, counter=4, seconds=200, alias=3)
    assert later.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


def test_full_sectors_after_completion_without_an_observed_cycle_are_ambiguous():
    store = QualiSessionStore()
    first = complete(store)
    unknown = apply(store, {"Sectors": SECTORS_A}, 151)
    assert unknown.completed_lap_sequence[0].event_key == first.completed_lap_sequence[0].event_key
    assert unknown.completed_lap_sequence[0].sectors_s == (None, None, None)
    later = complete(store, counter=4, seconds=200, alias=3)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


def test_original_completion_source_can_enrich_a_sectorless_event_after_clock_advances():
    store = QualiSessionStore()
    first = complete(store)
    key = first.completed_lap_sequence[0].event_key
    apply(store, {"Position": 1}, 150)
    enriched = apply(store, {"Sectors": SECTORS_A}, 100)
    assert enriched.completed_lap_sequence[0].event_key == key
    assert enriched.completed_lap_sequence[0].sectors_s == (20, 30, 40)
    assert enriched.drivers[0].best_lap_event_key == key
    later = complete(store, counter=4, seconds=200, alias=3)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


def test_reset_retires_pending_records_instead_of_giving_them_a_later_owner():
    store = QualiSessionStore()
    apply(store, {"NumberOfLaps": 2}, 0)
    apply(store, {"Sectors": {"0": {"Value": "20"}}}, 26)
    apply(store, {"Sectors": {"0": {"Value": ""}}}, 30)
    apply(store, {"Sectors": {"0": {"Value": "25"}}}, 55)
    first = complete(store, sectors={"1": {"Value": "25"}, "2": {"Value": "40"}})
    key = first.completed_lap_sequence[0].event_key
    assert first.completed_lap_sequence[0].sectors_s == (25, 25, 40)
    duplicate = apply(store, {"Sectors": {"0": {"Value": "20"}}}, 151)
    assert duplicate.completed_lap_sequence[0].event_key == key
    assert duplicate.completed_lap_sequence[0].sectors_s == (25, 25, 40)
    later = complete(store, counter=4, seconds=200, alias=3)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


@pytest.mark.parametrize("original_source_time", [False, True])
def test_unseen_old_part_packet_without_a_new_cycle_anchor_is_unsupported(original_source_time):
    store = QualiSessionStore()
    complete(store, sectors=SECTORS_A)
    apply(store, {"SessionPart": "Q2"}, 110, topic="SessionInfo")
    # This packet was not observed in Q1, so no content ledger can identify it.
    apply(store, {"Sectors": {"0": {"Value": "21"}, "1": {"Value": "29"},
                              "2": {"Value": "40"}}}, 100 if original_source_time else 151)
    later = complete(store, counter=4, seconds=220, alias=3)
    assert later.completed_lap_sequence[1].sectors_s == (None, None, None)


def test_delayed_alias_enriches_established_best_even_with_equal_time_newer_event():
    store = QualiSessionStore()
    first = complete(store, sectors=SECTORS_A)
    key = first.completed_lap_sequence[0].event_key
    second = complete(store, counter=4, seconds=200, sectors={
        "0": {"Value": "25"}, "1": {"Value": "25"}, "2": {"Value": "40"}})
    assert second.drivers[0].best_lap_event_key == key
    enriched = apply(store, {"BestLapTime": {"Value": "1:30", "Lap": 2}}, 201)
    assert enriched.drivers[0].best_lap_event_key == key
    assert enriched.completed_lap_sequence[0].official_lap_number == 2
    assert enriched.completed_lap_sequence[1].official_lap_number is None
    assert enriched.completed_lap_sequence[1].sectors_s == (25, 25, 40)


def test_equal_time_alias_without_established_owner_stays_unresolved():
    store = QualiSessionStore()
    first = complete(store, sectors=SECTORS_A)
    complete(store, counter=4, seconds=200, sectors={
        "0": {"Value": "25"}, "1": {"Value": "25"}, "2": {"Value": "40"}})
    apply(store, {"BestLapTime": {"Value": ""}}, 201)
    unresolved = apply(store, {"BestLapTime": {"Value": "1:30", "Lap": 2}}, 202)
    assert unresolved.drivers[0].best_lap_event_key is None
    assert all(event.official_lap_number is None for event in unresolved.completed_lap_sequence)
    assert unresolved.completed_lap_sequence[0].event_key == first.completed_lap_sequence[0].event_key


def test_upward_counter_correction_keeps_separate_events_without_reusing_evidence():
    store = QualiSessionStore()
    first = complete(store, sectors=SECTORS_A)
    corrected = complete(store, counter=4, seconds=101)
    assert len(corrected.completed_lap_sequence) == 2  # Existing recognition contract.
    assert corrected.drivers[0].completed_laps == 2
    assert corrected.drivers[0].best_lap_event_key == first.completed_lap_sequence[0].event_key
    assert corrected.completed_lap_sequence[1].sectors_s == (None, None, None)
