from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping

from .models import PublicationTime


# Existing reducer/service event identity, scoped to one session store lifetime.
QualiCompletedLapKey = tuple[str, str, int, float | None]


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
    best_lap_event_key: QualiCompletedLapKey | None = None
    knocked_out: bool | None = None


@dataclass(frozen=True)
class QualiCompletedLap:
    session_part: str
    driver_number: str
    # NumberOfLaps at the accepted boundary; not the accepted-event count.
    lap_number: int
    lap_time_s: float
    session_time_s: float | None
    classification: str = "normal_timed"
    sectors_s: tuple[float | None, float | None, float | None] = (None, None, None)
    # Feed BestLapTime.Lap metadata, when causally attached to this event.
    # It is deliberately separate from the NumberOfLaps boundary above.
    official_lap_number: int | None = None

    @property
    def event_key(self) -> QualiCompletedLapKey:
        return (self.session_part, self.driver_number, self.lap_number, self.session_time_s)


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
    # Boundary comparisons require the same independently known coordinate.
    acquisition_coordinate: Literal["utc", "session"] | None = None
    received_at_monotonic_s: float | None = None


@dataclass(frozen=True)
class QualiSpeedObservation:
    """A received speed sample, separate from the live telemetry register."""

    driver_number: str
    session_time_s: float
    speed_kph: float
    # None for archive/scheduler clocks that do not preserve actual reception.
    received_at_utc: datetime | None = None
    acquisition_coordinate: Literal["utc", "session"] | None = None
    received_at_monotonic_s: float | None = None


@dataclass(frozen=True)
class QualiTrafficEvidence:
    """Physical-traffic evidence; existing operational event keys stay unchanged."""

    driver_number: str
    kind: str
    publication_time: PublicationTime | None
    lap_number: int | None = None
    completion_key: QualiCompletedLapKey | None = None


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
    traffic_evidence_sequence: tuple[QualiTrafficEvidence, ...] = ()
    # False while Q1/SQ1 is only the reducer's bootstrap default.
    session_part_established: bool = True
    # Received samples ordered within each acquisition domain, for causal matching.
    # None identifies a legacy producer without causal history support.
    speed_history: Mapping[str, tuple[QualiSpeedObservation, ...]] | None = None
    # Only new, distinct samples from the most recent CarData message.
    speed_observations: tuple[QualiSpeedObservation, ...] = ()
    speed_freshness_s: float = 5.0
    message_source: str | None = None
    traffic_clock_time_s: float | None = None
    traffic_clock_kind: str | None = None
    receive_time_trusted: bool = False
    receive_monotonic_s: float | None = None
    # Independent paired source/session metadata, never a receipt/scheduler offset.
    speed_coordinate_offset_s: float | None = None

    @classmethod
    def empty(cls) -> "QualiRawSnapshot":
        return cls(MappingProxyType({}), None, "Q1", None, False, (), None,
                   session_part_established=False)
