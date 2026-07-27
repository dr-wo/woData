from wodata.colours import (
    DRIVER_COLOURS,
    DRIVER_TEAMS,
    F1_TEAM_COLORS,
    TEAM_COLOURS,
    get_driver_colour,
    get_team_colour,
)


def test_2026_fastf1_team_colours_are_centralised():
    assert TEAM_COLOURS == {
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


def test_all_2026_drivers_in_driver_list_use_their_team_colour():
    assert len(DRIVER_TEAMS) == 22
    assert DRIVER_COLOURS == {
        driver: TEAM_COLOURS[team] for driver, team in DRIVER_TEAMS.items()
    }
    assert get_driver_colour("nor") == "#F47600"
    assert get_driver_colour("PER") == "#909090"


def test_legacy_team_names_resolve_to_current_colours():
    assert F1_TEAM_COLORS["Red Bull"] == "#4781D7"
    assert F1_TEAM_COLORS["Haas"] == "#9C9FA2"
    assert get_team_colour("RB") == "#6C98FF"
