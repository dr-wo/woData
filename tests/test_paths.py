from __future__ import annotations

from pathlib import Path

from wodata import get_cache_path, get_data_root


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
