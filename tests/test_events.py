import pandas as pd

from wodata.events import parse_event_id, resolve_event


def _schedule(*args, **kwargs):
    return pd.DataFrame([{
        "RoundNumber": 7, "EventName": "Monaco Grand Prix",
        "OfficialEventName": "FORMULA 1 GRAND PRIX DE MONACO 2026",
        "Country": "Monaco", "Location": "Monte Carlo", "EventFormat": "conventional",
        "Session1": "Practice 1", "Session2": "Practice 2", "Session3": "Practice 3",
        "Session4": "Qualifying", "Session5": "Race",
        "Session5DateUtc": pd.Timestamp("2026-06-07T13:00:00Z"),
    }])


class _Race:
    results = pd.DataFrame({"Laps": [78, 77]})
    laps = pd.DataFrame()


def test_event_id_canonicalizes_legacy_suffix() -> None:
    assert parse_event_id("2026-07") == ("2026-07", 2026, 7)
    assert parse_event_id("2026-07-Monaco") == ("2026-07", 2026, 7)


def test_resolve_event_includes_canonical_schedule_metadata() -> None:
    metadata = resolve_event(
        "2026-07-Monaco", schedule_loader=_schedule,
        session_loader=lambda *_: _Race(),
    )
    assert metadata.event_id == "2026-07"
    assert metadata.event_name == "Monaco Grand Prix"
    assert metadata.race_start == "2026-06-07T13:00:00+00:00"
    assert metadata.total_laps == 78
    assert metadata.total_laps_source == "fastf1_session"
    assert metadata.session_total_laps == 78
    assert metadata.session_total_laps_source == "fastf1_race_results_laps"


def test_scheduled_race_laps_precede_session_loading() -> None:
    def should_not_load(*args):
        raise AssertionError("session timing must not be loaded when metadata is available")

    metadata = resolve_event(
        "2026-07", schedule_loader=_schedule, session_loader=should_not_load,
        scheduled_metadata_loader=lambda: {
            "schema_version": "schema_test",
            "events": {"2026-07": {"scheduled_race_laps": 79, "source": "test"}},
        },
    )
    assert metadata.scheduled_race_laps == 79
    assert metadata.total_laps == 79
    assert metadata.total_laps_source == "event_metadata"
    assert metadata.scheduled_race_laps_source == "wodata_event_metadata_schema_test"
    assert metadata.session_total_laps is None


def test_session_total_laps_is_fallback_when_scheduled_metadata_is_missing() -> None:
    class RaceSession:
        total_laps = 66

    metadata = resolve_event(
        "2026-07", schedule_loader=_schedule,
        session_loader=lambda *_: RaceSession(),
        scheduled_metadata_loader=lambda: {"schema_version": "schema_test", "events": {}},
    )
    assert metadata.scheduled_race_laps is None
    assert metadata.session_total_laps == 66
    assert metadata.session_total_laps_source == "fastf1_session_total_laps"
    assert metadata.total_laps == 66
    assert metadata.total_laps_source == "fastf1_session"


def test_race_laps_remain_unavailable_when_all_sources_are_missing() -> None:
    class EmptySession:
        results = pd.DataFrame()
        laps = pd.DataFrame()

    metadata = resolve_event(
        "2026-07", schedule_loader=_schedule,
        session_loader=lambda *_: EmptySession(),
        scheduled_metadata_loader=lambda: {"schema_version": "schema_test", "events": {}},
    )
    assert metadata.total_laps is None
    assert metadata.total_laps_source is None
