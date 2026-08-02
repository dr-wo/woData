from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "schema_v1"


class DataAvailability(str, Enum):
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    SUBSCRIBED_BUT_NEVER_RECEIVED = "SUBSCRIBED_BUT_NEVER_RECEIVED"
    AVAILABLE = "AVAILABLE"
    RECEIVED_BUT_STALE = "RECEIVED_BUT_STALE"
    PARTIAL = "PARTIAL"
    CORRECTED = "CORRECTED"


class AnalysisStatus(str, Enum):
    UPDATED = "UPDATED"
    SKIPPED_NO_NEW_USABLE_LAPS = "SKIPPED_NO_NEW_USABLE_LAPS"
    SKIPPED_MISSING_TYRE_DATA = "SKIPPED_MISSING_TYRE_DATA"
    SKIPPED_INSUFFICIENT_SUPPORT = "SKIPPED_INSUFFICIENT_SUPPORT"
    SKIPPED_MODEL_RUNNING = "SKIPPED_MODEL_RUNNING"
    SKIPPED_UNCHANGED_FINGERPRINT = "SKIPPED_UNCHANGED_FINGERPRINT"
    FAILED = "FAILED"
    STALE_LAST_SUCCESSFUL = "STALE_LAST_SUCCESSFUL"


@dataclass(frozen=True)
class CompletedLap:
    """Analysis-ready completed lap; unknown tyre fields remain explicit."""

    driver_number: str
    lap_number: int
    lap_time_s: float
    compound: str | None = None
    tyre_life: int | None = None
    run_id: str | None = None
    team: str | None = None
    clean_air: bool | None = None
    pit_in: bool = False
    pit_out: bool = False
    track_status: str | None = None
    source_timestamp_utc: datetime | None = None
    data_status: str = DataAvailability.AVAILABLE.value


@dataclass(frozen=True)
class TyreStintState:
    driver_number: str
    source_stint_index: int
    compound: str | None = None
    new: bool | None = None
    start_laps: int | None = None
    total_laps: int | None = None
    tyres_not_changed: str | bool | int | None = None
    first_update_time: datetime | None = None
    last_update_time: datetime | None = None
    data_status: str = DataAvailability.PARTIAL.value


@dataclass(frozen=True)
class LapTyreInfo:
    driver_number: str
    lap_number: int
    source_stint_index: int | None
    tyre_stint_id: int | None
    compound: str | None
    tyre_life: int | None
    fresh_tyre: bool | None
    data_status: str
    provenance: str


@dataclass(frozen=True)
class ParameterEstimate:
    analysis_id: str
    sample_bank_id: str
    source_scope: str
    as_of_leader_lap: int
    parameter: str
    session: str
    team: str | None
    compound: str | None
    reference_compound: str | None
    p10: float
    median: float
    p90: float
    unit: str
    support_status: str
    usable_lap_count: int
    usable_run_count: int
    ess: float
    weighted_rmse: float
    bound_weight_low: float
    bound_weight_high: float
    input_fingerprint: str
    config_hash: str
    created_at: datetime


@dataclass(frozen=True)
class ModelSnapshot:
    analysis_id: str
    sample_bank_id: str
    source_scope: str
    as_of_leader_lap: int
    parameters: tuple[ParameterEstimate, ...]
    input_fingerprint: str
    config_hash: str
    created_at: datetime
    schema_version: str = SCHEMA_VERSION
    status: str = AnalysisStatus.UPDATED.value
    stale_reason: str | None = None
    source_analysis_ids: tuple[str, ...] = ()
    contributing_sessions: tuple[str, ...] = ()
    excluded_sessions: tuple[tuple[str, str], ...] = ()
    joint_weighted_cost: float | None = None
    session_weighted_costs: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class StrategySearchState:
    current_completed_lap: int
    total_laps: int
    current_compound: str
    current_tyre_age: int
    selected_driver: str | None = None
    max_future_stops: int = 2
    provenance: str = "model"

    @property
    def laps_remaining(self) -> int:
        return max(int(self.total_laps) - int(self.current_completed_lap), 0)


@dataclass(frozen=True)
class StrategyRecord:
    search_id: str
    strategy_id: str
    rank: int
    search_type: str
    selected_driver: str | None
    as_of_completed_lap: int
    laps_remaining: int
    current_compound: str
    current_tyre_age: int
    future_stop_count: int
    compound_sequence: tuple[str, ...]
    pit_laps: tuple[int, ...]
    formatted_strategy: str
    remaining_cost: float
    delta_to_best: float
    model_analysis_id: str
    model_version: str
    input_state_hash: str
    status: str
    created_at: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    """Return a deterministic SHA-256 hash for contracts and fingerprints."""
    payload = json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Sequence):
        return [_json_value(item) for item in value]
    return str(value)
