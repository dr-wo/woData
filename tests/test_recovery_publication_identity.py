"""Recovery identities preserve lifecycle publications without disabling overlap dedup."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from wodata.live import LiveTimingMessage
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.recovery import load_recording, merge_recordings, recover_replay_directory
from wodata.live.replay import ReplayPlayer

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CLEAR = {"Lines": {"31": {"Sectors": {"0": {"Value": ""}}}}}


def message(publication, *, sequence=1, received=200, session=False, payload=CLEAR):
    return LiveTimingMessage("TimingData", payload,
        NOW + timedelta(seconds=publication) if publication is not None and not session else None,
        NOW + timedelta(seconds=received), sequence=sequence,
        session_time_seconds=publication if session else None)


def record(path, messages):
    recorder = LiveTimingRecorder(path)
    for m in messages:
        recorder.write(m)
    recorder.close()
    return load_recording(path)


@pytest.mark.parametrize("session", [False, True])
def test_distinct_known_coordinates_and_known_vs_unknown_survive(session, tmp_path):
    values = [message(None, sequence=1), message(110, sequence=2, session=session),
              message(190, sequence=3, session=session)]
    source = record(tmp_path / "a.jsonl", values)
    merged = merge_recordings([source])
    assert [m.message.publication_time for m in merged] == [m.publication_time for m in values]


def test_equal_numeric_coordinates_of_different_types_are_not_duplicates(tmp_path):
    utc = message(100)
    relative = message(utc.source_timestamp_utc.timestamp(), session=True)
    assert len(merge_recordings([record(tmp_path / "a.jsonl", [utc, relative])])) == 2


@pytest.mark.parametrize("format", ["envelope", "legacy", "mixed"])
def test_overlapping_fragments_deduplicate_same_publications(format, tmp_path):
    values = [message(t, sequence=i, received=t) for i, t in enumerate((100, 110, 190, 200), 1)]
    sources = []
    for i, fragment in enumerate((values[:3], values[1:], values[:3])):
        suffix = ".txt" if format == "legacy" or format == "mixed" and i == 0 else ".jsonl"
        path = tmp_path / f"fragment{i}{suffix}"
        if suffix == ".txt":
            path.write_text("".join(str([m.topic, m.payload, m.source_timestamp_utc.isoformat()]) + "\n" for m in fragment))
            sources.append(load_recording(path))
        else:
            sources.append(record(path, fragment))
    merged = merge_recordings(sources)
    assert sorted(m.message.publication_time.seconds for m in merged) == sorted(m.publication_time.seconds for m in values)
    assert len(merged) == 4


def test_true_publication_duplicate_ignores_delivery_sequence_and_playback(tmp_path):
    a = message(100)
    b = replace(a, sequence=999, received_at_utc=NOW + timedelta(seconds=400), playback_time_seconds=600)
    sources = [record(tmp_path / "a.jsonl", [a]), record(tmp_path / "b.jsonl", [b])]
    merged = merge_recordings(sources)
    assert len(merged) == 1
    assert merged[0].message.publication_time == a.publication_time


def test_unknown_envelope_copies_deduplicate_only_across_inputs(tmp_path):
    unknown = message(None)
    source = record(tmp_path / "a.jsonl", [unknown, unknown])
    copy = record(tmp_path / "b.jsonl", [replace(unknown, playback_time_seconds=600)] * 2)
    assert len(merge_recordings([source])) == 2
    assert len(merge_recordings([source, copy, source])) == 2
    distinct = record(tmp_path / "c.jsonl", [replace(unknown, sequence=2, received_at_utc=NOW + timedelta(seconds=201))])
    assert len(merge_recordings([source, copy, distinct])) == 3


@pytest.mark.parametrize("missing", ["sequence", "received_at_utc", "invalid_received_at"])
def test_unknown_incomplete_envelopes_have_no_cross_file_dedup_identity(missing, tmp_path):
    path = tmp_path / "a.jsonl"
    record(path, [message(None)])
    raw = json.loads(path.read_text())
    if missing == "invalid_received_at":
        raw["received_at_utc"] = "not-a-recorded-time"
    else:
        raw.pop(missing)
    path.write_text(json.dumps(raw) + "\n")
    other = tmp_path / "b.jsonl"
    other.write_text(path.read_text())
    source = load_recording(path)
    assert len(merge_recordings([source, load_recording(other)])) == 2
    assert len(merge_recordings([source, load_recording(path)])) == 1


def test_unknown_legacy_content_alone_does_not_deduplicate(tmp_path):
    sources = []
    for name in ("a.txt", "b.txt"):
        path = tmp_path / name
        path.write_text(str(["TimingData", CLEAR, ""]) + "\n")
        sources.append(load_recording(path))
    assert len(merge_recordings(sources)) == 2
    assert len(merge_recordings([sources[0], sources[0]])) == 1


def test_old_content_only_recovery_cache_is_rebuilt(tmp_path):
    identity = LiveTimingMessage("SessionInfo", {"Name": "Qualifying", "StartDate": NOW.isoformat(),
        "Meeting": {"RoundNumber": 16}}, NOW, NOW)
    record(tmp_path / "live_timing_raw.jsonl", [identity, message(110), message(190, sequence=2)])
    first = recover_replay_directory(tmp_path, year=2026, round_number=16, session="Q")
    manifest = json.loads(first.manifest_path.read_text())
    manifest["recovery_schema_version"] = 2
    first.manifest_path.write_text(json.dumps(manifest))
    first.path.write_text("")  # An obsolete cached output must not be reused.
    rebuilt = recover_replay_directory(tmp_path, year=2026, round_number=16, session="Q")
    assert not rebuilt.reused
    messages = []
    ReplayPlayer(rebuilt.path, speed=0).play(messages.append)
    assert [m.publication_time for m in messages[1:]] == [message(110).publication_time, message(190).publication_time]
    assert recover_replay_directory(tmp_path, year=2026, round_number=16, session="Q").reused
