from datetime import datetime, timezone
from pathlib import Path

import pytest

from wodata.weekend_runs import WeekendArtifactReference, WeekendRunStore


def test_create_version_and_lookup(tmp_path: Path) -> None:
    first = WeekendRunStore.create(
        event="2026-07", workflow="race_preparation", data_root=tmp_path,
        run_id="first", generated_at="2026-05-01T10:00:00+00:00",
    )
    first.save_json("config.input", {"value": 1})
    with pytest.raises(FileExistsError, match="immutable"):
        first.save_json("config.input", {"value": 2})
    first.save_manifest({"status": "SUCCESS"})

    second = WeekendRunStore.create(
        event="2026-07", workflow="race_preparation", data_root=tmp_path,
        run_id="second", generated_at="2026-05-01T11:00:00+00:00",
    )
    second.save_manifest({"status": "PARTIAL"})
    assert WeekendRunStore.load_latest(
        event="2026-07", workflow="race_preparation", data_root=tmp_path
    ).run_id == "second"
    assert WeekendRunStore.load(
        event="2026-07", workflow="race_preparation", run_id="first", data_root=tmp_path
    ).load_json("config.input") == {"value": 1}


def test_select_last_usable_run_before_start(tmp_path: Path) -> None:
    for run_id, timestamp, status in (
        ("good", "2026-05-01T10:00:00+00:00", "SUCCESS"),
        ("failed", "2026-05-01T11:00:00+00:00", "FAILED"),
        ("debug", "2026-05-01T13:00:00+00:00", "SUCCESS"),
    ):
        run = WeekendRunStore.create(
            event="event", workflow="race_preparation", data_root=tmp_path,
            run_id=run_id, generated_at=timestamp,
        )
        if run_id == "good":
            run.save_json("tyre_prediction", {"available": True})
        run.save_manifest({"status": status})
    selected = WeekendRunStore.select_before(
        event="event", workflow="race_preparation", data_root=tmp_path,
        before=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        required_artifacts=("tyre_prediction",),
    )
    assert selected.run_id == "good"


def test_artifact_metadata_records_hash_and_size(tmp_path: Path) -> None:
    run = WeekendRunStore.create(event="event", workflow="race", data_root=tmp_path)
    run.save_json("result", {"ok": True})
    metadata = run.artifact_metadata()["artifacts"]["result.json"]
    assert metadata["size_bytes"] > 0
    assert len(metadata["sha256"]) == 64


def test_missing_external_reference_never_substitutes_latest(tmp_path: Path) -> None:
    run = WeekendRunStore.create(event="event", workflow="post_race", data_root=tmp_path)
    run.save_reference("samples", WeekendArtifactReference(
        artifact_id="exact-bank", uri=str(tmp_path / "missing-bank.pkl"),
        recipe={"source_sessions": ["R"]},
    ))
    loaded = run.load_reference("samples")
    assert loaded["artifact_id"] == "exact-bank"
    assert loaded["available"] is False
    with pytest.raises(FileNotFoundError, match="no latest-cache substitution"):
        run.load_reference("samples", require_target=True)
