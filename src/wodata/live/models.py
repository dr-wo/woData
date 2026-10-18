from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Mapping


from .race_control import RaceControlEvent

@dataclass(frozen=True)
class LiveTimingMessage:
    topic: str
    payload: object
    source_timestamp_utc: datetime | None
    received_at_utc: datetime
    schema_version: int = 1
    source: Literal["signalr_live", "f1_archive_replay", "synthetic_test"] = "signalr_live"
    sequence: int = 0
    session_time_seconds: float | None = None


# Source-neutral name used by new integrations; retain LiveTimingMessage for
# existing woPlanner and recording callers.
LiveMessage = LiveTimingMessage


@dataclass(frozen=True)
class LiveDriver:
    driver_number: str
    driver_code: str
    team: str
    full_name: str
    position: int | None
    source_timestamp_utc: datetime | None
    retired: bool = False
    retirement_lap: int | None = None


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
    compound: str | None = None
    tyre_life: float | None = None
    stint: int | None = None
    fresh_tyre: bool | None = None
    session_time_seconds: float | None = None
    track_status: str | None = None
    pit_out: bool = False
    pit_in: bool = False
    is_valid: bool = True
    # Paired adjacent in/out laps from an observed racing InPit visit. Inferred
    # tyre-change pit flags alone never establish this evidence.
    racing_pit_stop_id: int | None = None


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
    compound: str | None = None
    tyre_life: float | None = None
    stint: int | None = None
    fresh_tyre: bool | None = None
    session_time_seconds: float | None = None


@dataclass(frozen=True)
class ProvisionalSector(CompletedSector):
    """Revisable display estimate; never part of canonical sector history."""

    source_time_s: float | None = None


@dataclass(frozen=True)
class LiveTimingState:
    """Current official registers and their publication chronology for the UI."""

    driver_number: str
    driver_code: str
    completed_laps: int | None = None
    position: int | None = None
    gap_to_leader_s: float | None = None
    interval_to_position_ahead_s: float | None = None
    position_source_time_s: float | None = None
    gap_source_time_s: float | None = None
    interval_source_time_s: float | None = None
    latest_source_time_s: float | None = None


@dataclass(frozen=True)
class LiveTelemetrySample:
    driver_number: str
    session_time_seconds: float
    lap_number: int
    speed_kph: float
    source_timestamp_utc: datetime | None = None


@dataclass(frozen=True)
class LivePositionSample:
    driver_number: str
    session_time_seconds: float
    x: float
    y: float
    z: float
    on_track: bool | None = None


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
    telemetry_samples: tuple[LiveTelemetrySample, ...] = ()
    position_samples: tuple[LivePositionSample, ...] = ()
    scheduled_total_laps: int | None = None
    provisional_sectors: tuple[ProvisionalSector, ...] = ()
    live_timing: tuple[LiveTimingState, ...] = ()
    race_control_events: tuple[RaceControlEvent, ...] = ()

    @classmethod
    def empty(cls) -> "LiveSessionSnapshot":
        return cls(MappingProxyType({}), None, (), (), MappingProxyType({}), None, ())
