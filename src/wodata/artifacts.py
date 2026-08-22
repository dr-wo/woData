from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from . import get_data_root
from .contracts import ModelSnapshot, SCHEMA_VERSION, StrategyRecord, canonical_hash


def pre_quali_root(
    year: int, round_number: int, data_root: str | Path | None = None
) -> Path:
    return (
        get_data_root(data_root) / "wostrategy" / "pre_quali" / SCHEMA_VERSION
        / f"year={int(year)}" / f"round={int(round_number)}"
    )


def write_pre_quali_analysis(
    laps, durations, track_progression, *, year: int, round_number: int,
    push_threshold_fraction: float, data_root: str | Path | None = None,
) -> Path:
    """Persist planner-facing pre-qualifying tables using woData path ownership."""
    root = pre_quali_root(year, round_number, data_root)
    root.mkdir(parents=True, exist_ok=True)
    laps.to_csv(root / "lap_roles.csv", index=False)
    durations.to_csv(root / "duration_summary.csv", index=False)
    track_progression.to_csv(root / "track_progression.csv", index=False)
    _write_json(root / "manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "year": int(year), "round": int(round_number),
        "push_threshold_fraction": float(push_threshold_fraction),
        "lap_count": int(len(laps)), "created_at": datetime.now().isoformat(),
    })
    return root


def read_pre_quali_analysis(
    *, year: int, round_number: int, data_root: str | Path | None = None,
):
    import pandas as pd
    root = pre_quali_root(year, round_number, data_root)
    def read_table(name: str):
        path = root / name
        if not path.read_text(encoding="utf-8").strip():
            return pd.DataFrame()
        return pd.read_csv(path)

    return (
        read_table("lap_roles.csv"),
        read_table("duration_summary.csv"),
        read_table("track_progression.csv"),
    )


def pit_lane_geometry_path(
    *, year: int, round_number: int, data_root: str | Path | None = None,
) -> Path:
    return (
        get_data_root(data_root) / "wodata" / "pit_lane_geometry" / SCHEMA_VERSION
        / f"year={int(year)}" / f"round={int(round_number)}" / "geometry.json"
    )


def write_pit_lane_geometry(
    centreline: Iterable[tuple[float, float]], *, year: int, round_number: int,
    maximum_distance: float = 400.0, data_root: str | Path | None = None,
) -> Path:
    path = pit_lane_geometry_path(
        year=year, round_number=round_number, data_root=data_root
    )
    points = [[float(x), float(y)] for x, y in centreline]
    if len(points) < 2:
        raise ValueError("Pit-lane geometry requires at least two centreline points")
    _write_json(path, {
        "schema_version": SCHEMA_VERSION, "centreline": points,
        "maximum_distance": float(maximum_distance),
    })
    return path


def read_pit_lane_geometry(
    *, year: int, round_number: int, data_root: str | Path | None = None,
) -> dict[str, object]:
    path = pit_lane_geometry_path(
        year=year, round_number=round_number, data_root=data_root
    )
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def weekend_model_root(
    year: int, round_number: int, data_root: str | Path | None = None
) -> Path:
    return (
        get_data_root(data_root)
        / "wostrategy"
        / "weekend_model"
        / SCHEMA_VERSION
        / f"year={int(year)}"
        / f"round={int(round_number)}"
    )


def strategy_search_root(
    year: int,
    round_number: int,
    target: str = "global",
    data_root: str | Path | None = None,
) -> Path:
    return (
        get_data_root(data_root)
        / "wostrategy"
        / "strategy_search"
        / SCHEMA_VERSION
        / f"year={int(year)}"
        / f"round={int(round_number)}"
        / f"target={target}"
    )


def write_model_snapshot(
    snapshot: ModelSnapshot,
    *,
    year: int,
    round_number: int,
    data_root: str | Path | None = None,
) -> Path:
    root = weekend_model_root(year, round_number, data_root)
    scope = snapshot.source_scope.upper()
    destination = root / ("aggregate" if scope == "AGGREGATE" else f"sessions/{scope}")
    destination.mkdir(parents=True, exist_ok=True)
    rows = [_csv_row(asdict(parameter)) for parameter in snapshot.parameters]
    _write_csv(destination / "latest_parameters.csv", rows)
    support_columns = (
        "analysis_id", "parameter", "compound", "team", "support_status",
        "usable_lap_count", "usable_run_count", "ess", "weighted_rmse",
        "bound_weight_low", "bound_weight_high", "input_fingerprint",
    )
    _write_csv(
        destination / "latest_support.csv",
        [{column: row.get(column, "") for column in support_columns} for row in rows],
    )
    manifest = {
        "schema_version": snapshot.schema_version,
        "analysis_id": snapshot.analysis_id,
        "sample_bank_id": snapshot.sample_bank_id,
        "source_scope": snapshot.source_scope,
        "as_of_leader_lap": snapshot.as_of_leader_lap,
        "input_fingerprint": snapshot.input_fingerprint,
        "config_hash": snapshot.config_hash,
        "created_at": snapshot.created_at.isoformat(),
        "status": snapshot.status,
        "stale_reason": snapshot.stale_reason,
        "source_analysis_ids": list(snapshot.source_analysis_ids),
        "contributing_sessions": list(snapshot.contributing_sessions),
        "excluded_sessions": dict(snapshot.excluded_sessions),
        "joint_weighted_cost": snapshot.joint_weighted_cost,
        "session_weighted_costs": dict(snapshot.session_weighted_costs),
        "sampler_quality": dict(snapshot.sampler_quality),
        "parameter_count": len(rows),
    }
    _write_json(root / "manifest.json", manifest)
    _write_json(destination / "manifest.json", manifest)
    history = root / "history"
    history.mkdir(parents=True, exist_ok=True)
    _append_csv(history / "parameter_history.csv", rows)
    _append_csv(history / "analysis_runs.csv", [_csv_row(manifest)])
    return destination


def read_latest_model_snapshot(
    *,
    year: int,
    round_number: int,
    source_scope: str = "aggregate",
    data_root: str | Path | None = None,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    root = weekend_model_root(year, round_number, data_root)
    scope = source_scope.upper()
    directory = root / ("aggregate" if scope == "AGGREGATE" else f"sessions/{scope}")
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        manifest_path = root / "manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    with (directory / "latest_parameters.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return manifest, rows


def write_weekend_model_config(
    config: Mapping[str, object], *, year: int, round_number: int, data_root=None
) -> Path:
    """Persist the reproducible sample-bank and clean-lap configuration."""
    path = weekend_model_root(year, round_number, data_root) / "model_config.json"
    _write_json(path, dict(config))
    return path


def read_weekend_model_config(
    *, year: int, round_number: int, data_root=None
) -> dict[str, object]:
    path = weekend_model_root(year, round_number, data_root) / "model_config.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_joint_candidate_costs(
    rows: Iterable[Mapping[str, object]],
    *,
    year: int,
    round_number: int,
    data_root: str | Path | None = None,
) -> Path:
    """Persist total and per-session candidate costs for joint diagnostics."""
    path = weekend_model_root(year, round_number, data_root) / "aggregate/candidate_costs.csv"
    _write_csv(path, [dict(row) for row in rows])
    return path


def write_strategy_records(
    records: Iterable[StrategyRecord],
    *,
    year: int,
    round_number: int,
    target: str = "global",
    data_root: str | Path | None = None,
) -> Path:
    records = tuple(records)
    root = strategy_search_root(year, round_number, target, data_root)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    stint_rows = []
    for record in records:
        row = asdict(record)
        row["compound_sequence_json"] = json.dumps(row.pop("compound_sequence"))
        row["pit_laps_json"] = json.dumps(row.pop("pit_laps"))
        rows.append(_csv_row(row))
        for index, compound in enumerate(record.compound_sequence):
            stint_rows.append(
                {
                    "search_id": record.search_id,
                    "strategy_id": record.strategy_id,
                    "stint_index": index,
                    "compound": compound,
                    "pit_lap_after_stint": (
                        record.pit_laps[index] if index < len(record.pit_laps) else ""
                    ),
                }
            )
    _write_csv(root / "strategies.csv", rows)
    _write_csv(root / "stints.csv", stint_rows)
    revaluation_rows = [
        row
        for row in rows
        if row.get("search_type") in {"pre_race_fixed", "same_sequence", "full"}
    ]
    _write_csv(root / "revaluation.csv", revaluation_rows)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(rows),
        "target": target,
        "content_hash": canonical_hash(rows),
        "created_at": max((r.created_at for r in records), default=datetime.now()).isoformat(),
    }
    _write_json(root / "manifest.json", manifest)
    _append_csv(root / "search_runs.csv", [_csv_row(manifest)])
    return root


def read_strategy_records(
    *, year: int, round_number: int, target: str = "global", data_root=None
) -> list[dict[str, str]]:
    path = strategy_search_root(year, round_number, target, data_root) / "strategies.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _csv_row(row: dict) -> dict:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
