from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class QualiPitState(str, Enum):
    UNKNOWN = "UNKNOWN"
    INACTIVE = "INACTIVE"
    FIRED_UP = "FIRED_UP"
    QUEUE = "QUEUE"
    ON_TRACK = "ON_TRACK"


@dataclass(frozen=True)
class QualiCheckpoint:
    kind: str
    index: int
    elapsed_s: float


@dataclass(frozen=True)
class QualiRawDriverState:
    driver_number: str
    driver_code: str
    team: str
    position: int | None = None
    completed_laps: int = 0
    last_lap_s: float | None = None
    best_lap_s: float | None = None
    sectors_s: tuple[float | None, float | None, float | None] = (None, None, None)
    segments: tuple[QualiCheckpoint, ...] = ()
    in_pit: bool | None = None
    stopped: bool | None = None
    pit_state: QualiPitState = QualiPitState.UNKNOWN
    pit_lane_progress: float | None = None
    rpm: float | None = None
    speed_kph: float | None = None
    gear: int | None = None
    latest_telemetry_time_s: float | None = None
    latest_position_time_s: float | None = None


@dataclass(frozen=True)
class QualiRawSnapshot:
    session_info: Mapping[str, object]
    session_status: str | None
    session_part: str
    remaining_time_s: float | None
    clock_running: bool
    drivers: tuple[QualiRawDriverState, ...]
    last_message_at_utc: datetime | None

    @classmethod
    def empty(cls) -> "QualiRawSnapshot":
        return cls(MappingProxyType({}), None, "Q1", None, False, (), None)
