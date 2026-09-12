"""Helpers for interpreting live timing session metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class SessionIdentity:
    year: int | None
    round_number: int | None
    event_name: str
    session_name: str

    @property
    def is_qualifying(self) -> bool | None:
        return is_qualifying_session(self.session_name)


def parse_session_identity(
    session_info: Mapping[str, object], *, fallback_year: int | None = None
) -> SessionIdentity:
    meeting = session_info.get("Meeting")
    meeting = meeting if isinstance(meeting, Mapping) else {}
    year = _positive_int(session_info.get("Year") or meeting.get("Year"))
    if year is None:
        year = _year_from_date(session_info.get("StartDate")) or fallback_year
    round_number = _positive_int(
        session_info.get("RoundNumber")
        or meeting.get("RoundNumber")
        or meeting.get("Number")
    )
    event_name = str(
        meeting.get("Name")
        or meeting.get("OfficialName")
        or session_info.get("EventName")
        or ""
    ).strip()
    session_name = str(
        session_info.get("Name") or session_info.get("Type") or ""
    ).strip()
    return SessionIdentity(year, round_number, event_name, session_name)


def is_qualifying_session(session_name: object) -> bool | None:
    text = str(session_name or "").strip().upper()
    if not text:
        return None
    return (
        text in {"Q", "Q1", "Q2", "Q3"}
        or "QUALI" in text
        or "SHOOTOUT" in text
    )


def is_sprint_qualifying_session(session_name: object) -> bool:
    text = str(session_name or "").strip().upper()
    return is_qualifying_session(text) is True and (
        "SPRINT" in text or "SHOOTOUT" in text
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _year_from_date(value: object) -> int | None:
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).year
    except ValueError:
        return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None
