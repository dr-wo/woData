"""Presentation-only Race Control history from the existing F1 topic.

Source indices identify notices across subscription snapshots and sparse patches.
No notice changes timing, penalties, classification, or strategy calculations.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


def _utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RaceControlEvent:
    event_id: str
    utc: datetime | None
    fields: Mapping[str, object]

    @property
    def message(self) -> str:
        return str(self.fields.get("Message") or "")

    @property
    def kind(self) -> str:
        text = self.message.upper()
        if re.search(r"\b(?:TIME|LAP)\b.*\bDELETED\b", text):
            return "Lap time deleted"
        if "PENALTY" in text or "PENALTIES" in text:
            return "Penalty"
        return str(self.fields.get("Category") or "Other")

    @property
    def driver_number(self) -> str:
        number = self.fields.get("RacingNumber")
        if number is not None and str(number).strip():
            return str(number)
        match = re.search(r"\bCAR\s+(\d+)\b", self.message, re.IGNORECASE)
        return match.group(1) if match else ""


class RaceControlHistory:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._field_times: dict[tuple[str, str], tuple[str, float]] = {}
        self._cached: tuple[RaceControlEvent, ...] | None = ()

    def apply(self, payload: Mapping, publication_time: tuple[str, float] | None = None) -> None:
        messages = payload.get("Messages", {})
        if isinstance(messages, list):
            messages = dict(enumerate(messages))
        if not isinstance(messages, Mapping):
            return
        for index, patch in messages.items():
            if not isinstance(patch, Mapping):
                continue
            event_id = str(index)
            item = self._items.setdefault(event_id, {})
            for key, value in patch.items():
                field_key = (event_id, str(key))
                previous = self._field_times.get(field_key)
                current = publication_time
                # Older deliveries can fill missing fields but cannot undo a
                # newer correction in the same original source coordinate.
                if previous and current and previous[0] == current[0] and current[1] < previous[1]:
                    continue
                if key not in item or item[key] != value:
                    item[key] = copy.deepcopy(value)
                    self._cached = None
                if current:
                    self._field_times[field_key] = current

    def snapshot(self) -> tuple[RaceControlEvent, ...]:
        if self._cached is not None:
            return self._cached
        events = []
        seen = set()
        for event_id, fields in self._items.items():
            utc = _utc(fields.get("Utc"))
            signature = json.dumps(fields, sort_keys=True, default=str)
            # Equal text without occurrence time can describe separate notices.
            if utc is not None and signature in seen:
                continue
            if utc is not None:
                seen.add(signature)
            events.append(RaceControlEvent(event_id, utc, _freeze(fields)))
        # Unknown event times remain last in source insertion order. Never use
        # replay/receipt wall time to fabricate a historical occurrence time.
        events.sort(key=lambda event: (event.utc is None, event.utc or datetime.max.replace(tzinfo=timezone.utc)))
        self._cached = tuple(events)
        return self._cached
