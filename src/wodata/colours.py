"""Shared Formula 1 team and driver colours.

The values are the ``TeamColour`` values supplied by the FastF1 live timing
DriverList for the 2026 season.  FastF1 assigns drivers their team's colour,
so the driver map is derived from the same source of truth.
"""

from __future__ import annotations


TEAM_COLOURS: dict[str, str] = {
    "McLaren": "#F47600",
    "Ferrari": "#ED1131",
    "Red Bull Racing": "#4781D7",
    "Mercedes": "#00D7B6",
    "Racing Bulls": "#6C98FF",
    "Audi": "#F50537",
    "Alpine": "#00A1E8",
    "Haas F1 Team": "#9C9FA2",
    "Aston Martin": "#229971",
    "Williams": "#1868DB",
    "Cadillac": "#909090",
}

# Names used by historic FastF1 data and elsewhere in the dr-wo packages.
TEAM_COLOUR_ALIASES: dict[str, str] = {
    "Red Bull": "Red Bull Racing",
    "RB": "Racing Bulls",
    "Haas": "Haas F1 Team",
    "Kick Sauber": "Audi",
    "Sauber": "Audi",
}

DRIVER_TEAMS: dict[str, str] = {
    "NOR": "McLaren",
    "PIA": "McLaren",
    "LEC": "Ferrari",
    "HAM": "Ferrari",
    "VER": "Red Bull Racing",
    "HAD": "Red Bull Racing",
    "RUS": "Mercedes",
    "ANT": "Mercedes",
    "LIN": "Racing Bulls",
    "LAW": "Racing Bulls",
    "HUL": "Audi",
    "BOR": "Audi",
    "GAS": "Alpine",
    "COL": "Alpine",
    "OCO": "Haas F1 Team",
    "BEA": "Haas F1 Team",
    "ALO": "Aston Martin",
    "STR": "Aston Martin",
    "SAI": "Williams",
    "ALB": "Williams",
    "BOT": "Cadillac",
    "PER": "Cadillac",
}

DRIVER_COLOURS: dict[str, str] = {
    driver: TEAM_COLOURS[team] for driver, team in DRIVER_TEAMS.items()
}

# American-spelling compatibility for plotting code and FastF1's public API.
TEAM_COLORS = TEAM_COLOURS
DRIVER_COLORS = DRIVER_COLOURS
F1_TEAM_COLORS: dict[str, str] = {
    **TEAM_COLOURS,
    **{
        alias: TEAM_COLOURS[canonical]
        for alias, canonical in TEAM_COLOUR_ALIASES.items()
    },
}


def get_team_colour(team: str) -> str | None:
    """Return a team's colour, accepting canonical and legacy team names."""
    canonical = TEAM_COLOUR_ALIASES.get(team, team)
    return TEAM_COLOURS.get(canonical)


def get_driver_colour(driver: str) -> str | None:
    """Return a driver's team colour by three-letter abbreviation."""
    return DRIVER_COLOURS.get(driver.strip().upper())


get_team_color = get_team_colour
get_driver_color = get_driver_colour


__all__ = [
    "DRIVER_COLORS",
    "DRIVER_COLOURS",
    "DRIVER_TEAMS",
    "F1_TEAM_COLORS",
    "TEAM_COLORS",
    "TEAM_COLOUR_ALIASES",
    "TEAM_COLOURS",
    "get_driver_color",
    "get_driver_colour",
    "get_team_color",
    "get_team_colour",
]
