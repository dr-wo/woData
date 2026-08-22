from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .models import LiveTimingMessage
from .quali_models import (
    QualiCheckpoint, QualiPitState, QualiRawDriverState, QualiRawSnapshot,
)
from .reducer import (
    _as_bool, _as_float, _as_int, _decode_compressed_payload, _deep_merge,
    _lap_time_seconds, _sample_time_seconds, _sector_time_updates,
)


@dataclass(frozen=True)
class PitLaneGeometry:
    """Circuit-specific pit-lane centreline in Position.z coordinates."""

    centreline: tuple[tuple[float, float], ...]
    maximum_distance: float = 400.0

    def project(self, x: float, y: float) -> tuple[float, float] | None:
        if len(self.centreline) < 2:
            return None
        lengths = []
        total = 0.0
        for first, second in zip(self.centreline, self.centreline[1:]):
            length = math.hypot(second[0] - first[0], second[1] - first[1])
            lengths.append(length)
            total += length
        if total <= 0:
            return None
        best: tuple[float, float] | None = None
        travelled = 0.0
        for (ax, ay), (bx, by), length in zip(
            self.centreline, self.centreline[1:], lengths
        ):
            if length <= 0:
                continue
            dx, dy = bx - ax, by - ay
            fraction = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length**2))
            px, py = ax + fraction * dx, ay + fraction * dy
            distance = math.hypot(x - px, y - py)
            progress = (travelled + fraction * length) / total
            if best is None or distance < best[1]:
                best = (progress, distance)
            travelled += length
        return best if best is not None and best[1] <= self.maximum_distance else None


class QualiSessionStore:
    """Qualifying-only raw reducer; strategy/projection remains in woPlanner."""

    def __init__(
        self, *, pit_lane_geometry: PitLaneGeometry | None = None,
        rpm_threshold: float = 500.0, telemetry_freshness_s: float = 5.0,
    ) -> None:
        self.pit_lane_geometry = pit_lane_geometry
        self.rpm_threshold = float(rpm_threshold)
        self.telemetry_freshness_s = float(telemetry_freshness_s)
        self.reset()

    def reset(self) -> None:
        self._session_info: dict[str, object] = {}
        self._session_status: str | None = None
        self._session_part = "Q1"
        self._remaining_time_s: float | None = None
        self._clock_running = False
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._telemetry: dict[str, dict] = {}
        self._position: dict[str, dict] = {}
        self._last_message_at: datetime | None = None
        self._latest_session_time_s: float | None = None

    def apply(self, message: LiveTimingMessage) -> QualiRawSnapshot:
        self._apply_message(message)
        return self.snapshot()

    def apply_many(self, messages: list[LiveTimingMessage]) -> QualiRawSnapshot:
        for message in messages:
            self._apply_message(message)
        return self.snapshot()

    def _apply_message(self, message: LiveTimingMessage) -> None:
        self._last_message_at = message.received_at_utc
        candidate_time = message.session_time_seconds
        if candidate_time is None and message.source_timestamp_utc is not None:
            candidate_time = message.source_timestamp_utc.timestamp()
        if candidate_time is not None:
            self._latest_session_time_s = max(
                float(candidate_time), self._latest_session_time_s or -math.inf
            )
        payload = message.payload
        if message.topic == "SessionInfo" and isinstance(payload, Mapping):
            _deep_merge(self._session_info, payload)
            self._session_part = _session_part(payload, self._session_part)
        elif message.topic == "SessionStatus" and isinstance(payload, Mapping):
            if payload.get("Status") is not None:
                self._session_status = str(payload["Status"])
                self._clock_running = self._session_status.lower() in {"started", "active"}
        elif message.topic == "ExtrapolatedClock" and isinstance(payload, Mapping):
            remaining = _lap_time_seconds(payload.get("Remaining"))
            if remaining is not None:
                if (
                    self._remaining_time_s is not None
                    and self._remaining_time_s <= 5.0
                    and remaining > self._remaining_time_s + 60.0
                ):
                    self._session_part = {"Q1": "Q2", "Q2": "Q3"}.get(
                        self._session_part, self._session_part
                    )
                self._remaining_time_s = remaining
            if payload.get("Extrapolating") is not None:
                self._clock_running = bool(_as_bool(payload.get("Extrapolating")))
        elif message.topic == "DriverList" and isinstance(payload, Mapping):
            lines = payload.get("Lines", payload)
            if isinstance(lines, Mapping):
                for number, patch in lines.items():
                    if isinstance(patch, Mapping):
                        _deep_merge(self._drivers.setdefault(str(number), {}), patch)
        elif message.topic in {"TimingData", "TimingStats"} and isinstance(payload, Mapping):
            self._apply_timing(payload)
        elif message.topic == "CarData.z":
            self._apply_car_data(message)
        elif message.topic == "Position.z":
            self._apply_position(message)

    def _apply_timing(self, payload: Mapping) -> None:
        lines = payload.get("Lines", payload)
        if not isinstance(lines, Mapping):
            return
        for number, patch in lines.items():
            if not isinstance(patch, Mapping):
                continue
            timing = self._timing.setdefault(str(number), {})
            _deep_merge(timing, patch)
            last = _lap_time_seconds(patch.get("LastLapTime"))
            if last is not None:
                previous = timing.get("_best_seen_s")
                timing["_best_seen_s"] = last if previous is None else min(float(previous), last)

    def _apply_car_data(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        entries = payload.get("Entries", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            sample_time = _sample_time_seconds(entry.get("Utc"), message)
            cars = entry.get("Cars", {})
            if sample_time is None or not isinstance(cars, Mapping):
                continue
            for number, car in cars.items():
                channels = car.get("Channels", {}) if isinstance(car, Mapping) else {}
                if not isinstance(channels, Mapping):
                    continue
                self._telemetry[str(number)] = {
                    "time": sample_time, "rpm": _as_float(channels.get("0")),
                    "speed": _as_float(channels.get("2")), "gear": _as_int(channels.get("3")),
                }

    def _apply_position(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        samples = payload.get("Position", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(samples, list):
            return
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            sample_time = _sample_time_seconds(sample.get("Timestamp"), message)
            entries = sample.get("Entries", {})
            if sample_time is None or not isinstance(entries, Mapping):
                continue
            for number, value in entries.items():
                if not isinstance(value, Mapping):
                    continue
                x, y = _as_float(value.get("X")), _as_float(value.get("Y"))
                if x is None or y is None:
                    continue
                projected = self.pit_lane_geometry.project(x, y) if self.pit_lane_geometry else None
                self._position[str(number)] = {
                    "time": sample_time, "x": x, "y": y, "status": value.get("Status"),
                    "pit_progress": projected[0] if projected else None,
                    "pit_distance": projected[1] if projected else None,
                }

    def snapshot(self) -> QualiRawSnapshot:
        now_s = self._latest_session_time_s
        sampled_now = _latest_sample_time(self._telemetry, self._position)
        if sampled_now is not None:
            now_s = max(now_s or -math.inf, sampled_now)
        states = []
        numbers = set(self._drivers) | set(self._timing) | set(self._telemetry) | set(self._position)
        for number in numbers:
            driver, timing = self._drivers.get(number, {}), self._timing.get(number, {})
            telemetry, position = self._telemetry.get(number, {}), self._position.get(number, {})
            in_pit = _as_bool(timing.get("InPit"))
            stopped = _as_bool(timing.get("Stopped"))
            progress = position.get("pit_progress")
            fresh_rpm = (
                telemetry.get("rpm") is not None and now_s is not None
                and now_s - telemetry.get("time", -math.inf) <= self.telemetry_freshness_s
                and telemetry["rpm"] > self.rpm_threshold
            )
            status = str(position.get("status", "")).lower()
            if in_pit and progress is not None:
                pit_state = QualiPitState.QUEUE
            elif in_pit and fresh_rpm:
                pit_state = QualiPitState.FIRED_UP
            elif in_pit:
                pit_state = QualiPitState.INACTIVE
            elif status in {"ontrack", "0"} or in_pit is False:
                pit_state = QualiPitState.ON_TRACK
            else:
                pit_state = QualiPitState.UNKNOWN
            sectors = [None, None, None]
            for index, value in _sector_time_updates(timing.get("Sectors", {})).items():
                sectors[index] = value
            segments = _segments(timing.get("Sectors", {}))
            states.append(QualiRawDriverState(
                number, str(driver.get("Tla") or driver.get("BroadcastName") or number),
                str(driver.get("TeamName") or driver.get("Team") or ""),
                _as_int(timing.get("Position")), _as_int(timing.get("NumberOfLaps")) or 0,
                _lap_time_seconds(timing.get("LastLapTime")),
                _lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                or timing.get("_best_seen_s"),
                tuple(sectors), segments, in_pit, stopped, pit_state, progress,
                telemetry.get("rpm"), telemetry.get("speed"), telemetry.get("gear"),
                telemetry.get("time"), position.get("time"),
            ))
        states.sort(key=lambda value: (value.position is None, value.position or 999, value.driver_code))
        return QualiRawSnapshot(
            MappingProxyType(copy.deepcopy(self._session_info)), self._session_status,
            self._session_part, self._remaining_time_s, self._clock_running,
            tuple(states), self._last_message_at,
        )


def _segments(sectors: object) -> tuple[QualiCheckpoint, ...]:
    output = []
    if not isinstance(sectors, (list, Mapping)):
        return ()
    values = sectors.items() if isinstance(sectors, Mapping) else enumerate(sectors)
    elapsed = 0.0
    for sector_index, sector in values:
        if not isinstance(sector, Mapping):
            continue
        sector_time = _lap_time_seconds(sector)
        if sector_time is not None:
            elapsed += sector_time
            output.append(QualiCheckpoint("sector", int(sector_index) + 1, elapsed))
        raw_segments = sector.get("Segments", {})
        segment_values = raw_segments.items() if isinstance(raw_segments, Mapping) else enumerate(raw_segments) if isinstance(raw_segments, list) else ()
        for segment_index, segment in segment_values:
            # Public TimingData commonly exposes colour/status codes in
            # ``Segments[*].Value``. Those are not elapsed times and must not
            # be fabricated into a projection checkpoint. Only explicit time
            # fields are accepted.
            value = _lap_time_seconds(
                segment.get("ElapsedTime") or segment.get("Time")
                if isinstance(segment, Mapping) else None
            )
            if value is not None:
                output.append(QualiCheckpoint("segment", int(segment_index), value))
    return tuple(output)


def _session_part(payload: Mapping, default: str) -> str:
    text = " ".join(str(payload.get(key, "")) for key in ("Name", "Type", "SessionPart"))
    for part in ("Q1", "Q2", "Q3"):
        if part in text.upper():
            return part
    return default


def _latest_sample_time(*sources: dict[str, dict]) -> float | None:
    values = [row.get("time") for source in sources for row in source.values() if row.get("time") is not None]
    return max(values) if values else None
