from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Mapping


@dataclass(frozen=True)
class LiveTimingMessage:
    topic: str
    payload: object
    source_timestamp_utc: datetime | None
    received_at_utc: datetime


@dataclass(frozen=True)
class LiveDriver:
    driver_number: str
    driver_code: str
    team: str
    full_name: str
    position: int | None
    source_timestamp_utc: datetime | None


@dataclass(frozen=True)
class CompletedLap:
    driver_number: str
    driver_code: str
    team: str
    lap_number: int
    lap_time_s: float
    cumulative_time_s: float | None
    position: int | None
    source_timestamp_utc: datetime | None


@dataclass(frozen=True)
class CompletedSector:
    driver_number: str
    driver_code: str
    team: str
    lap_number: int
    sector: str
    segment_index: int
    sector_time_s: float
    cumulative_time_s: float | None
    position: int | None
    source_timestamp_utc: datetime | None


@dataclass(frozen=True)
class LiveConnectionStatus:
    state: Literal[
        "idle",
        "authenticating",
        "connecting",
        "connected",
        "receiving",
        "disconnected",
        "error",
    ]
    message: str


@dataclass(frozen=True)
class LiveSessionSnapshot:
    session_info: Mapping[str, object]
    session_status: str | None
    drivers: tuple[LiveDriver, ...]
    completed_laps: tuple[CompletedLap, ...]
    latest_position_by_driver: Mapping[str, int]
    last_message_at_utc: datetime | None
    completed_sectors: tuple[CompletedSector, ...] = ()

    @classmethod
    def empty(cls) -> "LiveSessionSnapshot":
        return cls(MappingProxyType({}), None, (), (), MappingProxyType({}), None, ())
