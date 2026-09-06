from __future__ import annotations

import os
from pathlib import Path

from wodata.colours import (
    DRIVER_COLORS,
    DRIVER_COLOURS,
    DRIVER_TEAMS,
    F1_TEAM_COLORS,
    TEAM_COLORS,
    TEAM_COLOURS,
    get_driver_color,
    get_driver_colour,
    get_team_color,
    get_team_colour,
)

__all__ = [
    "DRIVER_COLORS",
    "DRIVER_COLOURS",
    "DRIVER_TEAMS",
    "F1_TEAM_COLORS",
    "TEAM_COLORS",
    "TEAM_COLOURS",
    "get_cache_path",
    "get_data_root",
    "get_fastf1_raw_cache_dir",
    "get_fastf1_session_laps_path",
    "get_fastf1_telemetry_cache_dir",
    "get_race_performance_review_event_dir",
    "get_race_performance_review_range_dir",
    "get_race_performance_review_root",
    "get_driver_color",
    "get_driver_colour",
    "get_team_color",
    "get_team_colour",
    "WeekendArtifactReference",
    "WeekendRun",
    "WeekendRunStore",
    "EventMetadata",
    "parse_event_id",
    "resolve_event",
    "resolve_race_distance",
]


def get_data_root(explicit_root: str | Path | None = None) -> Path:
    """Resolve the shared dr-wo data root."""
    if explicit_root is not None:
        return Path(explicit_root).expanduser()

    wodata_root = os.environ.get("WODATA_ROOT")
    if wodata_root:
        return Path(wodata_root).expanduser()

    legacy_root = os.environ.get("WOSTRATEGY_DATA_ROOT")
    if legacy_root:
        return Path(legacy_root).expanduser()

    cwd = Path.cwd()
    sibling = cwd.parent / "woData"
    if cwd.name in {"woStrategy", "woPlanner"} and sibling.is_dir():
        return sibling
    return cwd / "woData"


def get_cache_path(
    namespace: str,
    dataset: str,
    schema: str,
    year: int,
    round_number: int,
    session: str,
    filename: str,
    data_root: Path | None = None,
) -> Path:
    """Return a normalized cache path under the shared data root."""
    return (
        get_data_root(data_root)
        / namespace
        / dataset
        / schema
        / f"year={year}"
        / f"round={round_number}"
        / f"session={session}"
        / filename
    )


def get_fastf1_raw_cache_dir(data_root: str | Path | None = None) -> Path:
    """Directory used by FastF1's own HTTP/parser cache."""
    return get_data_root(data_root) / "fastf1" / "raw_cache" / "schema_v1"


def get_fastf1_telemetry_cache_dir(data_root: str | Path | None = None) -> Path:
    """Directory for woStrategy's derived per-sample telemetry cache."""
    return get_data_root(data_root) / "fastf1" / "telemetry" / "schema_v1"


def get_fastf1_session_laps_path(
    *,
    year: int,
    round_number: int,
    session: str,
    data_root: str | Path | None = None,
) -> Path:
    """Canonical cache path for FastF1 session laps enriched for analysis."""
    return get_cache_path(
        namespace="fastf1",
        dataset="session_laps",
        schema="schema_v1",
        year=year,
        round_number=round_number,
        session=str(session).upper(),
        filename="laps.pkl",
        data_root=Path(data_root) if data_root is not None else None,
    )


def get_race_performance_review_root(
    data_root: str | Path | None = None,
) -> Path:
    """Canonical root for woStrategy post-race performance artifacts."""
    return (
        get_data_root(data_root)
        / "wostrategy"
        / "race_performance_review"
        / "schema_v1"
    )


def get_race_performance_review_event_dir(
    *,
    year: int,
    round_number: int,
    session: str,
    data_root: str | Path | None = None,
) -> Path:
    return (
        get_race_performance_review_root(data_root)
        / f"year={int(year)}"
        / f"round={int(round_number)}"
        / f"session={str(session).upper()}"
    )


def get_race_performance_review_range_dir(
    *,
    year: int,
    range_label: str,
    session: str,
    data_root: str | Path | None = None,
) -> Path:
    return (
        get_race_performance_review_root(data_root)
        / f"year={int(year)}"
        / f"range={range_label}"
        / f"session={str(session).upper()}"
    )


# Imported after path helpers to avoid a module-initialisation cycle.
from wodata.weekend_runs import (  # noqa: E402
    WeekendArtifactReference,
    WeekendRun,
    WeekendRunStore,
)
from wodata.events import (  # noqa: E402
    EventMetadata,
    parse_event_id,
    resolve_event,
    resolve_race_distance,
)
