"""Physical tyre ownership must survive mutable TimingAppData slot views."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from wodata.live import LiveSessionStore, LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.replay.player import ReplayPlayer
from wodata.live.replay.schema import parse_timestamp

BASE = datetime(2026, 9, 27, tzinfo=timezone.utc)


class Stream:
    def __init__(self):
        self.store = LiveSessionStore()
        self.messages = []

    def send(self, topic, fields):
        stamp = BASE + timedelta(seconds=len(self.messages))
        msg = LiveTimingMessage(topic, {"Lines": {"1": fields}}, stamp, stamp,
                                session_time_seconds=float(len(self.messages)))
        self.messages.append(msg)
        self.store.reduce(msg)

    def tyre(self, slot, **fields):
        self.send("TimingAppData", {"Stints": {str(slot): fields}})

    def lap(self, lap):
        self.send("TimingData", {"NumberOfLaps": lap, "LastLapTime": {"Value": 90 + lap / 10}})
        return self.store.snapshot().completed_laps[-1]

    def pit(self):
        self.send("TimingData", {"InPit": True, "PitOut": False})
        self.send("TimingData", {"InPit": False, "PitOut": True})


def tyre_tuple(lap):
    return lap.compound, lap.tyre_life, lap.stint, lap.fresh_tyre


def test_reused_slot_staging_does_not_inherit_old_laps():
    s = Stream()
    s.tyre(1, Compound="SOFT", StartLaps=0, TotalLaps=0, New="true")
    for lap in range(1, 32):
        s.lap(lap)
    original = s.store.snapshot().completed_laps
    s.pit()
    s.tyre(1, Compound="UNKNOWN", New="false", TotalLaps=0)
    assert s.store.snapshot().completed_laps == original
    s.tyre(1, Compound="MEDIUM", StartLaps=3, TotalLaps=3, TyresNotChanged="0")
    assert tyre_tuple(s.lap(32)) == ("MEDIUM", 4, 2, False)
    assert s.store.snapshot().completed_laps[:30] == original[:30]
    assert all(x.compound == "SOFT" and x.stint == 1 for x in s.store.snapshot().completed_laps[:31])


def test_aliases_preserve_age_and_stale_slots_cannot_reclaim_current():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    for lap in range(1, 10):
        s.lap(lap)
    s.tyre(3, Compound="MEDIUM", StartLaps=0, TotalLaps=0)
    for lap in range(10, 29):
        s.lap(lap)
    for slot in (2, 1):
        s.tyre(slot, Compound="MEDIUM", StartLaps=0, TotalLaps=19)
    for lap in range(29, 33):
        last = s.lap(lap)
    assert tyre_tuple(last)[:3] == ("MEDIUM", 23, 2)
    s.pit()
    s.tyre(2, Compound="SOFT", StartLaps=3, TotalLaps=3, New="false")
    assert tyre_tuple(s.lap(33)) == ("SOFT", 4, 3, False)
    s.tyre(3, LapNumber=34, LapTime="1:32.1")
    s.tyre(0, TotalLaps=9)
    assert tyre_tuple(s.lap(34))[:3] == ("SOFT", 5, 3)


@pytest.mark.parametrize("prior_use", [3, 22, 50])
def test_used_tyres_preserve_any_valid_start_laps(prior_use):
    s = Stream()
    s.tyre(0, Compound="MEDIUM", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.pit()
    s.tyre(0, Compound="HARD", StartLaps=prior_use, TotalLaps=prior_use, New="false")
    assert tyre_tuple(s.lap(2)) == ("HARD", prior_use + 1, 2, False)


def test_same_compound_new_set_and_explicit_no_change():
    s = Stream()
    s.tyre(4, Compound="SOFT", StartLaps=3, TotalLaps=3, New="false")
    s.lap(1)
    s.pit()
    s.tyre(2, Compound="SOFT", StartLaps=3, TotalLaps=4, TyresNotChanged="1")
    assert tyre_tuple(s.lap(2)) == ("SOFT", 5, 1, False)
    s.pit()
    s.tyre(4, Compound="SOFT", StartLaps=3, TotalLaps=3, New="false")
    assert tyre_tuple(s.lap(3)) == ("SOFT", 4, 2, False)
    # Identical repeated full metadata is not another physical replacement.
    s.tyre(4, Compound="SOFT", StartLaps=3, TotalLaps=3, New="false")
    assert tyre_tuple(s.lap(4)) == ("SOFT", 5, 2, False)


def test_delayed_split_metadata_repairs_only_new_owned_interval():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.pit()
    s.tyre(1, TotalLaps=3, New="false")
    s.lap(2)
    s.tyre(1, Compound="MEDIUM")
    s.tyre(1, StartLaps=3)
    laps = s.store.snapshot().completed_laps
    assert tyre_tuple(laps[0])[:3] == ("SOFT", 1, 1)
    assert tyre_tuple(laps[1]) == ("MEDIUM", 4, 2, False)
    assert laps[0].pit_in and laps[1].pit_out
    s.tyre(1, New="false")
    assert tyre_tuple(s.lap(3)) == ("MEDIUM", 5, 2, False)


def test_metadata_enrichment_and_compound_correction_are_not_new_tyres():
    s = Stream()
    s.tyre(0, Compound="SOFT")
    s.lap(1)
    s.tyre(0, StartLaps=22, New="false")
    assert tyre_tuple(s.lap(2)) == ("SOFT", 24, 1, False)
    s.tyre(0, Compound="MEDIUM")
    assert tyre_tuple(s.lap(3)) == ("MEDIUM", 25, 1, False)


def test_counter_reset_exposes_same_compound_replacement_without_pit_feed():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=3, TotalLaps=3)
    s.lap(1)
    s.tyre(0, TotalLaps=4)
    s.tyre(0, TotalLaps=3)
    s.tyre(0, Compound="SOFT", StartLaps=3)
    assert tyre_tuple(s.lap(2))[:3] == ("SOFT", 4, 2)


def test_old_source_patch_cannot_rewind_slot_metadata():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    old = s.messages[-1]
    s.lap(1)
    s.pit()
    s.tyre(0, Compound="MEDIUM", StartLaps=3, TotalLaps=3)
    s.store.reduce(old)
    assert tyre_tuple(s.lap(2))[:3] == ("MEDIUM", 4, 2)


def test_reset_and_recorded_replay_match_every_live_checkpoint(tmp_path):
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.pit()
    s.tyre(0, Compound="UNKNOWN", TotalLaps=0)
    s.tyre(0, Compound="MEDIUM", StartLaps=50, TotalLaps=50, New="false")
    s.lap(2)
    path = tmp_path / "tyres.jsonl"
    recorder = LiveTimingRecorder(path)
    expected = []
    s.store.reset()
    for msg in s.messages:
        recorder.write(msg)
        expected.append(s.store.apply(msg).completed_laps)
    recorder.close()
    replay = LiveSessionStore()
    actual = []
    result = ReplayPlayer(path, speed=0).play(lambda msg: actual.append(replay.apply(msg).completed_laps))
    assert result.status == "completed"
    assert actual == expected
    assert tyre_tuple(actual[-1][-1]) == ("MEDIUM", 51, 2, False)


ROOT = Path(__file__).resolve().parents[1]
R16 = ROOT / "wodata/live_timing/schema_v1/year=2026/round=16/session=R/live_timing_raw.jsonl"
ARCHIVE = ROOT / "wodata/live_archive/schema_v1/year=2026/round=16/session=R/replay/merged_replay_v1.jsonl"


@pytest.mark.parametrize("path", [ROOT / "tests/fixtures/r16_mutable_tyres.jsonl", R16, ARCHIVE],
                         ids=["portable-packets", "original-live", "archive-replay"])
def test_actual_r16_ham_rus_full_evolution(path):
    if not path.exists():
        pytest.skip("local R16 recording is not installed")
    store = LiveSessionStore()
    checkpoints = {}
    wanted = {("44", 32): ("MEDIUM", 4), ("63", 32): ("MEDIUM", 23),
              ("63", 33): ("SOFT", 4), ("63", 45): ("SOFT", 4)}

    def accept(msg):
        if msg.topic not in ("TimingData", "TimingAppData", "DriverList", "SessionStatus"):
            return
        store.reduce(msg)
        for key in wanted:
            number, lap = key
            if key not in checkpoints and lap in store._laps.get(number, {}):
                data = store._laps[number][lap]
                checkpoints[key] = data["compound"], data["tyre_life"]

    if path == ARCHIVE:
        assert ReplayPlayer(path, speed=0).play(accept).status == "completed"
    else:
        for line in path.open():
            event = json.loads(line)
            accept(LiveTimingMessage(
                event["topic"], event["payload"], parse_timestamp(event.get("source_timestamp_utc")),
                parse_timestamp(event["received_at_utc"]),
                session_time_seconds=event.get("session_time_seconds"),
            ))
    assert checkpoints == wanted
    laps = {(lap.driver_number, lap.lap_number): lap for lap in store.snapshot().completed_laps}
    assert laps["44", 31].compound == "SOFT"
    assert laps["44", 31].stint != laps["44", 32].stint
    assert laps["63", 28].stint == laps["63", 32].stint
    assert laps["63", 32].stint != laps["63", 33].stint
    assert laps["63", 44].stint != laps["63", 45].stint
    assert laps["63", 35].compound == "SOFT"  # stale slot3 timing update
    assert laps["44", 32].tyre_life - 1 == 3
    assert laps["63", 32].tyre_life - 1 == 22


def test_used_tyre_metadata_before_age_counter():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.tyre(0, TotalLaps=1)
    s.pit()
    s.tyre(0, Compound="HARD", StartLaps=22, New="false")
    s.tyre(0, TotalLaps=22)
    assert tyre_tuple(s.lap(2)) == ("HARD", 23, 2, False)


def test_delayed_metadata_after_fitted_counter_has_advanced():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.pit()
    s.tyre(1, TotalLaps=3, New="false")
    s.lap(2)
    s.tyre(1, TotalLaps=4)
    s.tyre(1, Compound="MEDIUM", StartLaps=3)
    assert tyre_tuple(s.store.snapshot().completed_laps[-1]) == ("MEDIUM", 4, 2, False)
    assert tyre_tuple(s.lap(3)) == ("MEDIUM", 5, 2, False)


def test_new_slot_without_total_can_resolve_across_partial_patches():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0)
    s.lap(1)
    s.tyre(1, Compound="HARD")
    s.lap(2)
    s.tyre(1, StartLaps=22, New="false")
    assert tyre_tuple(s.store.snapshot().completed_laps[-1]) == ("HARD", 23, 2, False)


def test_older_missing_field_patch_survives_newer_counter_patch():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.pit()
    s.tyre(1, Compound="HARD", TotalLaps=22)
    stamp = BASE + timedelta(seconds=len(s.messages))
    delayed = LiveTimingMessage("TimingAppData", {"Lines": {"1": {"Stints": {
        "1": {"StartLaps": 22, "New": "false"}
    }}}}, stamp, stamp, session_time_seconds=float(len(s.messages)))
    s.lap(2)
    s.tyre(1, TotalLaps=23)
    s.store.reduce(delayed)
    assert tyre_tuple(s.store.snapshot().completed_laps[-1]) == ("HARD", 23, 2, False)


def test_old_missing_metadata_is_not_evidence_for_a_later_pit():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.tyre(1, Compound="MEDIUM", StartLaps=0, TotalLaps=0)
    s.lap(2)
    s.pit()
    stamp = BASE + timedelta(seconds=0.5)
    s.store.reduce(LiveTimingMessage("TimingAppData", {"Lines": {"1": {"Stints": {
        "0": {"New": "false"}
    }}}}, stamp, BASE + timedelta(seconds=7), session_time_seconds=0.5))
    assert tyre_tuple(s.lap(3))[:3] == ("MEDIUM", 2, 2)


def test_missing_new_flag_does_not_reactivate_historical_zero_age():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.tyre(1, Compound="MEDIUM", StartLaps=0, TotalLaps=0)
    s.lap(2)
    s.pit()
    s.tyre(0, New="false")
    assert tyre_tuple(s.lap(3))[:3] == ("MEDIUM", 2, 2)


@pytest.mark.parametrize("delayed", [False, True])
def test_metadata_during_pit_visit_uses_exit_lap_not_publication_lap(delayed):
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0, TotalLaps=0)
    s.lap(1)
    s.send("TimingData", {"InPit": True, "PitOut": False})
    s.tyre(1, Compound="HARD", TotalLaps=22)
    stamp = BASE + timedelta(seconds=len(s.messages))
    msg = LiveTimingMessage("TimingAppData", {"Lines": {"1": {"Stints": {
        "1": {"StartLaps": 22, "New": "false"}
    }}}}, stamp, stamp, session_time_seconds=float(len(s.messages)))
    if not delayed:
        s.store.reduce(msg)
    # The in-lap still belongs to the old tyre even if metadata arrived earlier.
    assert tyre_tuple(s.lap(2))[:3] == ("SOFT", 2, 1)
    s.send("TimingData", {"InPit": False, "PitOut": True})
    if delayed:
        s.store.reduce(msg)
    assert tyre_tuple(s.lap(3)) == ("HARD", 23, 2, False)


def test_complete_new_slot_without_total_during_pit_resolves_at_exit():
    s = Stream()
    s.tyre(0, Compound="SOFT", StartLaps=0)
    s.lap(1)
    s.send("TimingData", {"InPit": True, "PitOut": False})
    s.tyre(1, Compound="HARD", StartLaps=22, New="false")
    s.send("TimingData", {"InPit": False, "PitOut": True})
    assert tyre_tuple(s.lap(2)) == ("HARD", 23, 2, False)
