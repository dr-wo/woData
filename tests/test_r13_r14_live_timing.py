import math
from pathlib import Path

import pytest

from wodata.live import LiveSessionStore, load_recording


ARCHIVE_ROOT = (
    Path(__file__).parents[1]
    / "wodata"
    / "live_timing"
    / "schema_v1"
    / "year=2026"
)


def _replay(round_number: int):
    path = (
        ARCHIVE_ROOT
        / f"round={round_number}"
        / "session=R"
        / "live_timing_raw.jsonl"
    )
    if not path.exists():
        pytest.skip(f"saved R{round_number} live timing archive is unavailable")
    store = LiveSessionStore()
    for recorded in load_recording(path):
        store.reduce(recorded.message)
    return store.snapshot()


@pytest.fixture(scope="module")
def r13_snapshot():
    return _replay(13)


@pytest.fixture(scope="module")
def r14_snapshot():
    return _replay(14)


def test_r13_restart_laps_do_not_leak_suspension_through_sectors(r13_snapshot):
    laps = {
        (lap.driver_code, lap.lap_number): lap
        for lap in r13_snapshot.completed_laps
    }
    sectors = {}
    for sector in r13_snapshot.completed_sectors:
        sectors.setdefault((sector.driver_code, sector.lap_number), []).append(sector)

    assert laps[("RUS", 3)].cumulative_time_s == pytest.approx(251.826)
    assert laps[("ANT", 3)].cumulative_time_s == pytest.approx(251.826)
    # Their archived sector durations include the suspension, so publishing
    # them would restore the exact leak the canonical lap axis removed.
    assert sectors.get(("RUS", 3), []) == []
    assert sectors.get(("ANT", 3), []) == []

    # Every sectorised lap terminates on the same canonical endpoint as its
    # completed-lap fallback would have used.
    for key, values in sectors.items():
        s3 = next((value for value in values if value.sector == "S3"), None)
        if s3 is not None and key in laps:
            assert s3.cumulative_time_s == pytest.approx(
                laps[key].cumulative_time_s, abs=1e-9
            )


def test_r13_pairwise_order_is_preserved_through_the_real_crossing(r13_snapshot):
    laps = {
        (lap.driver_code, lap.lap_number): lap.cumulative_time_s
        for lap in r13_snapshot.completed_laps
    }
    assert laps[("ANT", 17)] - laps[("RUS", 17)] == pytest.approx(0.391)
    assert laps[("ANT", 18)] - laps[("RUS", 18)] == pytest.approx(-0.581)


def test_r14_pairwise_order_and_equal_last_lap_recovery(r14_snapshot):
    laps = {
        (lap.driver_code, lap.lap_number): lap
        for lap in r14_snapshot.completed_laps
    }
    assert (
        laps[("PIA", 30)].cumulative_time_s
        - laps[("RUS", 30)].cumulative_time_s
    ) == pytest.approx(2.636)
    assert laps[("VER", 35)].lap_time_s == pytest.approx(97.691)
    assert laps[("VER", 36)].lap_time_s == pytest.approx(97.691)
    assert laps[("VER", 36)].lap_time_s == laps[("VER", 35)].lap_time_s
    assert laps[("VER", 37)].lap_time_s == pytest.approx(97.818)
    assert math.isfinite(laps[("VER", 36)].cumulative_time_s)
    assert math.isfinite(laps[("VER", 37)].cumulative_time_s)
