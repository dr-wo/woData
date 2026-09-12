"""Shared qualifying segment defaults and countdown parsing (zero is valid)."""
from types import MappingProxyType
import math

STANDARD_QUALI_PARTS = ("Q1", "Q2", "Q3")
SPRINT_QUALI_PARTS = ("SQ1", "SQ2", "SQ3")
DEFAULT_SESSION_DURATIONS_S = MappingProxyType({
    "Q1": 1080.0, "Q2": 900.0, "Q3": 780.0,
    "SQ1": 720.0, "SQ2": 600.0, "SQ3": 480.0,
})


def countdown_seconds(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = 0.0
        for part in str(value).split(":"):
            seconds = seconds * 60 + float(part)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
