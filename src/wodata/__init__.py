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
    "get_driver_color",
    "get_driver_colour",
    "get_team_color",
    "get_team_colour",
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

    return Path.cwd() / "woData"


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
