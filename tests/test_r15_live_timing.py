from pathlib import Path

import pytest

from wodata.live import LiveSessionStore, load_recording


FIXTURE = Path(__file__).parent / "fixtures" / "r15_first_laps.jsonl"


@pytest.fixture(scope="module")
def r15_messages():
    return [recorded.message for recorded in load_recording(FIXTURE)]


def replay_to(messages, cut):
    store = LiveSessionStore()
    for message in messages:
        if message.sequence > cut:
            break
        store.reduce(message)
    return store


@pytest.mark.parametrize("number,cut,s1,old_s2,old_s3", [
    ("63", 3187, 39.418, 44.470, 25.661),
    ("81", 3206, 39.531, 44.719, 25.653),
    ("16", 3216, 39.909, 45.031, 25.243),
])
def test_r15_new_s1_resets_uncommitted_lap1_without_fabricating_history(
    r15_messages, number, cut, s1, old_s2, old_s3
):
    store = replay_to(r15_messages, cut - 1)
    pending = store._pending[number]
    assert pending["lap"] == pending["sector_lap"] == 1
    assert pending["sector_values"] == [None, old_s2, old_s3]
    assert pending["time"] is None
    assert not [
        sector for sector in store.snapshot().provisional_sectors
        if sector.driver_number == number
    ]

    transition = next(message for message in r15_messages if message.sequence == cut)
    store.reduce(transition)
    pending = store._pending[number]
    assert pending["lap"] == 1
    assert pending["sector_lap"] == 2
    assert pending["sector_values"] == [s1, None, None]
    assert pending["sectors"] is None
    snapshot = store.snapshot()
    assert snapshot.completed_laps == ()
    assert [
        (sector.lap_number, sector.sector, sector.sector_time_s, sector.cumulative_time_s)
        for sector in snapshot.provisional_sectors if sector.driver_number == number
    ] == [(2, "S1", s1, pytest.approx((transition.source_timestamp_utc - next(m.source_timestamp_utc for m in r15_messages if m.sequence == 1163)).total_seconds()))]

    # Duplicate transitions and older lap-1 sector publications cannot restore
    # the stale S2/S3 values or advance the logical lap again.
    store.reduce(transition)
    old_s2_patch = next(
        message for message in r15_messages
        if message.topic == "TimingData"
        and message.payload["Lines"].get(number, {}).get("Sectors", {})
        .get("1", {}).get("Value") == f"{old_s2:.3f}"
    )
    store.reduce(old_s2_patch)
    assert store._pending[number]["sector_lap"] == 2
    assert store._pending[number]["sector_values"] == [s1, None, None]


@pytest.mark.parametrize("number,cut,durations,cumulative", [
    ("63", 4213, [39.418, 44.492, 25.625], [151.826, 196.318, 221.943]),
    ("81", 4246, [39.531, 44.561, 25.715], [153.167, 197.728, 223.443]),
    ("16", 4269, [39.909, 45.123, 25.351], [153.984, 199.107, 224.458]),
])
def test_r15_first_commit_is_lap2_on_shared_started_clock(
    r15_messages, number, cut, durations, cumulative
):
    store = replay_to(r15_messages, cut - 1)
    before = store.snapshot()
    assert not [lap for lap in before.completed_laps if lap.driver_number == number]
    pending_sectors = [
        sector for sector in before.provisional_sectors if sector.driver_number == number
    ]
    assert [sector.sector for sector in pending_sectors] == ["S1", "S2"]
    assert {sector.lap_number for sector in pending_sectors} == {2}
    assert all(sector.cumulative_time_s is not None for sector in pending_sectors)
    assert not [sector for sector in before.completed_sectors if sector.driver_number == number]

    commit = next(message for message in r15_messages if message.sequence == cut)
    assert commit.payload["Lines"][number]["BestLapTime"]["Lap"] == 2
    snapshot = store.apply(commit)
    laps = [lap for lap in snapshot.completed_laps if lap.driver_number == number]
    assert [lap.lap_number for lap in laps] == [2]
    assert laps[0].lap_time_s == pytest.approx(sum(durations))
    assert laps[0].cumulative_time_s == pytest.approx(cumulative[-1])
    start = next(message for message in r15_messages if message.sequence == 1163)
    assert laps[0].cumulative_time_s == pytest.approx(
        (commit.source_timestamp_utc - start.source_timestamp_utc).total_seconds()
    )
    sectors = [
        sector for sector in snapshot.completed_sectors if sector.driver_number == number
    ]
    assert {sector.lap_number for sector in sectors} == {2}
    assert [sector.segment_index for sector in sectors] == [4, 5, 6]
    assert [sector.sector_time_s for sector in sectors] == pytest.approx(durations)
    assert [sector.cumulative_time_s for sector in sectors] == pytest.approx(cumulative)


@pytest.mark.parametrize("cut,ahead,behind,ahead_gap,behind_gap", [
    (3314, "16", "1", 2.299, 2.959),
    (3334, "6", "3", 3.401, 4.319),
])
def test_r15_source_backed_order_cannot_be_inverted_by_unanchored_sector_times(
    r15_messages, cut, ahead, behind, ahead_gap, behind_gap
):
    store = replay_to(r15_messages, cut)
    gaps = {
        number: float(store._timing[number]["GapToLeader"])
        for number in (ahead, behind)
    }
    assert gaps == {ahead: ahead_gap, behind: behind_gap}
    assert gaps[ahead] < gaps[behind]
    assert float(store._timing[behind]["IntervalToPositionAhead"]["Value"]) == (
        pytest.approx(behind_gap - ahead_gap)
    )
    gap_messages = {
        number: next(
            message for message in reversed(r15_messages)
            if message.sequence <= cut and message.topic == "TimingData"
            and "GapToLeader" in message.payload["Lines"].get(number, {})
        )
        for number in (ahead, behind)
    }
    assert (
        gap_messages[behind].source_timestamp_utc - gap_messages[ahead].source_timestamp_utc
    ).total_seconds() < 1.0

    sectors = {
        sector.driver_number: sector for sector in store.snapshot().provisional_sectors
        if sector.driver_number in (ahead, behind) and sector.sector == "S1"
    }
    assert set(sectors) == {ahead, behind}
    assert {sector.lap_number for sector in sectors.values()} == {2}
    # Raw S1 durations have the opposite order. Neither is a race-time anchor.
    assert sectors[ahead].sector_time_s > sectors[behind].sector_time_s
    assert all(sector.cumulative_time_s is not None for sector in sectors.values())
    assert not store.snapshot().completed_sectors
