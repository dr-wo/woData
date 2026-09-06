"""Canonical Formula 1 event identity and metadata resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.resources import files
import json
import re
from typing import Any, Callable, Mapping

import pandas as pd


EVENT_ID_PATTERN = re.compile(r"^(?P<season>\d{4})-(?P<round>\d{1,2})(?:-.+)?$")


@dataclass(frozen=True)
class EventMetadata:
    event_id: str
    season: int
    round_number: int
    event_name: str
    official_event_name: str | None
    country: str | None
    location: str | None
    event_format: str | None
    is_sprint: bool
    session_names: tuple[str, ...]
    race_start: str | None
    total_laps: int | None
    total_laps_source: str | None
    source: str = "fastf1_event_schedule"
    scheduled_race_laps: int | None = None
    scheduled_race_laps_source: str | None = None
    scheduled_race_laps_provenance: Mapping[str, Any] | None = None
    session_total_laps: int | None = None
    session_total_laps_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_event_id(value: str) -> tuple[str, int, int]:
    match = EVENT_ID_PATTERN.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(
            f"Invalid event identifier {value!r}; expected YYYY-RR, for example 2026-07"
        )
    season = int(match.group("season"))
    round_number = int(match.group("round"))
    if round_number < 1 or round_number > 99:
        raise ValueError("Event round number must be between 1 and 99")
    return f"{season:04d}-{round_number:02d}", season, round_number


def resolve_event(
    event: str,
    *,
    include_race_distance: bool = True,
    schedule_loader: Callable[..., Any] | None = None,
    session_loader: Callable[[int, int, str], Any] | None = None,
    scheduled_metadata_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> EventMetadata:
    event_id, season, round_number = parse_event_id(event)
    if schedule_loader is None:
        import fastf1

        schedule_loader = fastf1.get_event_schedule
    schedule = schedule_loader(season, include_testing=False)
    rows = schedule.loc[pd.to_numeric(schedule["RoundNumber"], errors="coerce").eq(round_number)]
    if rows.empty:
        raise LookupError(f"Round {round_number} is unavailable in the {season} FastF1 schedule")
    row = rows.iloc[0]
    sessions = tuple(
        str(row.get(f"Session{index}") or "").strip()
        for index in range(1, 6)
        if str(row.get(f"Session{index}") or "").strip()
    )
    race_start = _iso_time(
        row.get("Session5DateUtc")
        if not _missing(row.get("Session5DateUtc"))
        else row.get("Session5Date")
    )
    scheduled_race_laps = None
    scheduled_source = None
    scheduled_provenance = None
    session_total_laps = None
    session_source = None
    if include_race_distance:
        scheduled_race_laps, scheduled_source, scheduled_provenance = (
            _resolve_scheduled_race_laps(
                event_id, metadata_loader=scheduled_metadata_loader,
            )
        )
        if scheduled_race_laps is None:
            session_total_laps, session_source = _resolve_session_total_laps(
                season, round_number, session_loader=session_loader
            )
    total_laps = scheduled_race_laps or session_total_laps
    total_laps_source = (
        "event_metadata" if scheduled_race_laps is not None
        else "fastf1_session" if session_total_laps is not None
        else None
    )
    event_format = _optional_text(row.get("EventFormat"))
    return EventMetadata(
        event_id=event_id,
        season=season,
        round_number=round_number,
        event_name=_optional_text(row.get("EventName")) or event_id,
        official_event_name=_optional_text(row.get("OfficialEventName")),
        country=_optional_text(row.get("Country")),
        location=_optional_text(row.get("Location")),
        event_format=event_format,
        is_sprint=bool(event_format and "sprint" in event_format.lower()),
        session_names=sessions,
        race_start=race_start,
        total_laps=total_laps,
        total_laps_source=total_laps_source,
        scheduled_race_laps=scheduled_race_laps,
        scheduled_race_laps_source=scheduled_source,
        scheduled_race_laps_provenance=scheduled_provenance,
        session_total_laps=session_total_laps,
        session_total_laps_source=session_source,
    )


def resolve_race_distance(
    metadata: EventMetadata, *, manual_override: int | None = None,
) -> tuple[int | None, str | None]:
    """Resolve race laps with explicit pre-race-safe precedence and provenance."""
    if manual_override is not None:
        return int(manual_override), "manual_override"
    if metadata.scheduled_race_laps is not None:
        return int(metadata.scheduled_race_laps), "event_metadata"
    if metadata.session_total_laps is not None:
        return int(metadata.session_total_laps), "fastf1_session"
    # Compatibility for EventMetadata supplied by older callers/adapters.
    if metadata.total_laps is not None:
        return int(metadata.total_laps), metadata.total_laps_source
    return None, None


def _resolve_scheduled_race_laps(event_id: str, *, metadata_loader=None):
    if metadata_loader is None:
        metadata_loader = _load_scheduled_event_metadata
    payload = metadata_loader()
    schema_version = str(payload.get("schema_version") or "")
    events = payload.get("events", {})
    if not isinstance(events, Mapping):
        raise ValueError("Scheduled event metadata 'events' must be an object")
    record = events.get(event_id)
    if not isinstance(record, Mapping):
        return None, None, None
    raw_laps = record.get("scheduled_race_laps")
    if raw_laps is None:
        return None, None, None
    laps = int(raw_laps)
    if laps <= 0:
        raise ValueError(f"Scheduled race laps for {event_id} must be positive")
    source = f"wodata_event_metadata_{schema_version or 'unversioned'}"
    provenance = {
        "metadata_schema_version": schema_version or None,
        "record_source": record.get("source"),
        "source_url": record.get("source_url"),
        "verified_at": record.get("verified_at"),
    }
    return laps, source, provenance


def _load_scheduled_event_metadata() -> Mapping[str, Any]:
    resource = files("wodata").joinpath("data/event_metadata/schema_v1/events.json")
    with resource.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("Scheduled event metadata must be a JSON object")
    return payload


def _resolve_session_total_laps(season: int, round_number: int, *, session_loader=None):
    if session_loader is None:
        import fastf1

        def session_loader(year, race, name):
            session = fastf1.get_session(year, race, name)
            session.load(laps=True, telemetry=False, weather=False, messages=False)
            return session
    try:
        session = session_loader(season, round_number, "R")
    except Exception:
        return None, None
    try:
        session_total_laps = getattr(session, "total_laps", None)
    except Exception:
        session_total_laps = None
    if session_total_laps is not None and int(session_total_laps) > 0:
        return int(session_total_laps), "fastf1_session_total_laps"
    try:
        results = getattr(session, "results", None)
    except Exception:
        results = None
    if results is not None and "Laps" in results:
        laps = pd.to_numeric(results["Laps"], errors="coerce").dropna()
        if not laps.empty and int(laps.max()) > 0:
            return int(laps.max()), "fastf1_race_results_laps"
    try:
        laps_frame = getattr(session, "laps", None)
    except Exception:
        laps_frame = None
    if laps_frame is not None and "LapNumber" in laps_frame:
        laps = pd.to_numeric(laps_frame["LapNumber"], errors="coerce").dropna()
        if not laps.empty and int(laps.max()) > 0:
            return int(laps.max()), "fastf1_timing_laps"
    return None, None


def _missing(value: object) -> bool:
    return value is None or bool(pd.isna(value))


def _optional_text(value: object) -> str | None:
    return None if _missing(value) else str(value)


def _iso_time(value: object) -> str | None:
    if _missing(value):
        return None
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(timezone.utc)
    else:
        parsed = parsed.tz_convert(timezone.utc)
    return parsed.isoformat()


__all__ = ["EventMetadata", "parse_event_id", "resolve_event", "resolve_race_distance"]
