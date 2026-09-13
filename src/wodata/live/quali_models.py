from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class QualiPitState(str, Enum):
    UNKNOWN = "UNKNOWN"
    NOT_ACTIVE = "NOT ACTIVE"
    FIRE_UP = "FIRE-UP"
    PIT_LANE = "PIT-LANE"
    INACTIVE = "INACTIVE"
    FIRED_UP = "FIRED_UP"
    QUEUE = "QUEUE"
    ON_TRACK = "ON_TRACK"
    IN_LAP = "INLAP"
    IN_PIT = "IN-PIT"


@dataclass(frozen=True)
class QualiCheckpoint:
    kind: str
    index: int
    elapsed_s: float


@dataclass(frozen=True)
class QualiTyreStint:
    compound: str
    start_laps: int
    fresh: bool | None = None
    stint_index: int | None = None


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
    tyre_history: tuple[QualiTyreStint, ...] = ()
    historical_results: tuple[tuple[str, float | None], ...] = ()
    timed_lap_eligible: bool = False
    current_lap_identity: int | None = None
    structural_state: QualiPitState = QualiPitState.UNKNOWN
    out_lap_confirmed: bool = False


@dataclass(frozen=True)
class QualiCompletedLap:
    session_part: str
    driver_number: str
    lap_number: int
    lap_time_s: float
    session_time_s: float | None
    classification: str = "normal_timed"


@dataclass(frozen=True)
class QualiLapEvidence:
    session_part: str
    driver_number: str
    kind: str
    lap_number: int | None
    session_time_s: float | None
    lap_time_s: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class QualiPositionObservation:
    driver_number: str
    session_time_s: float
    status: str | None
    pit_side: bool | None = None


@dataclass(frozen=True)
class QualiRawSnapshot:
    session_info: Mapping[str, object]
    session_status: str | None
    session_part: str
    remaining_time_s: float | None
    clock_running: bool
    drivers: tuple[QualiRawDriverState, ...]
    last_message_at_utc: datetime | None

    normal_finished: bool = False
    normal_finish_source: str | None = None
    segment_duration_s: float | None = None
    completed_lap_sequence: tuple[QualiCompletedLap, ...] = ()
    # Presentation metadata only. It must never participate in domain state.
    clock_anchor_session_time_s: float | None = None
    lap_evidence_sequence: tuple[QualiLapEvidence, ...] = ()
    # Only the samples carried by the most recently processed Position message.
    position_observations: tuple[QualiPositionObservation, ...] = ()

    @classmethod
    def empty(cls) -> "QualiRawSnapshot":
        return cls(MappingProxyType({}), None, "Q1", None, False, (), None)
