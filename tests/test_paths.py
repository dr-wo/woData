from __future__ import annotations

from pathlib import Path

from wodata import (
    get_cache_path,
    get_data_root,
    get_fastf1_raw_cache_dir,
    get_fastf1_session_laps_path,
    get_fastf1_telemetry_cache_dir,
    get_race_performance_review_event_dir,
    get_race_performance_review_range_dir,
    get_race_performance_review_root,
)


def test_get_data_root_prefers_explicit_root(monkeypatch, tmp_path):
    monkeypatch.setenv("WODATA_ROOT", str(tmp_path / "env"))

    assert get_data_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_get_data_root_uses_wodata_root(monkeypatch, tmp_path):
    monkeypatch.setenv("WODATA_ROOT", str(tmp_path / "wodata"))
    monkeypatch.setenv("WOSTRATEGY_DATA_ROOT", str(tmp_path / "legacy"))

    assert get_data_root() == tmp_path / "wodata"


def test_get_data_root_uses_legacy_env_when_primary_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("WODATA_ROOT", raising=False)
    monkeypatch.setenv("WOSTRATEGY_DATA_ROOT", str(tmp_path / "legacy"))

    assert get_data_root() == tmp_path / "legacy"


def test_get_data_root_defaults_to_cwd_wodata(monkeypatch, tmp_path):
    monkeypatch.delenv("WODATA_ROOT", raising=False)
    monkeypatch.delenv("WOSTRATEGY_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert get_data_root() == tmp_path / "woData"


def test_get_data_root_finds_sibling_from_project_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("WODATA_ROOT", raising=False)
    monkeypatch.delenv("WOSTRATEGY_DATA_ROOT", raising=False)
    (tmp_path / "woData").mkdir()
    (tmp_path / "woStrategy").mkdir()
    monkeypatch.chdir(tmp_path / "woStrategy")

    assert get_data_root() == tmp_path / "woData"


def test_get_cache_path_uses_partitioned_layout(tmp_path):
    path = get_cache_path(
        "wostrategy",
        "enriched_laps",
        "schema_v1",
        2026,
        12,
        "R",
        "laps.parquet",
        data_root=tmp_path,
    )

    assert path == Path(
        tmp_path,
        "wostrategy",
        "enriched_laps",
        "schema_v1",
        "year=2026",
        "round=12",
        "session=R",
        "laps.parquet",
    )


def test_fastf1_paths_are_owned_by_wodata(tmp_path):
    assert get_fastf1_raw_cache_dir(tmp_path) == tmp_path / "fastf1/raw_cache/schema_v1"
    assert get_fastf1_telemetry_cache_dir(tmp_path) == tmp_path / "fastf1/telemetry/schema_v1"
    assert get_fastf1_session_laps_path(
        year=2026, round_number=12, session="fp2", data_root=tmp_path
    ) == tmp_path / "fastf1/session_laps/schema_v1/year=2026/round=12/session=FP2/laps.pkl"


def test_race_performance_review_paths_are_partitioned_under_wodata(tmp_path):
    root = tmp_path / "wostrategy/race_performance_review/schema_v1"
    assert get_race_performance_review_root(tmp_path) == root
    assert get_race_performance_review_event_dir(
        year=2026, round_number=11, session="r", data_root=tmp_path
    ) == root / "year=2026/round=11/session=R"
    assert get_race_performance_review_range_dir(
        year=2026, range_label="1-11", session="r", data_root=tmp_path
    ) == root / "year=2026/range=1-11/session=R"
