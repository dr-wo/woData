from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .models import LiveTimingMessage
from .quali_models import (
    QualiCheckpoint, QualiPitState, QualiRawDriverState, QualiRawSnapshot,
    QualiTyreStint,
)
from .replay.schema import parse_timestamp
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
        pit_moving_speed_kph: float = 1.0, pit_exit_speed_kph: float = 100.0,
    ) -> None:
        self.pit_lane_geometry = pit_lane_geometry
        self.rpm_threshold = float(rpm_threshold)
        self.telemetry_freshness_s = float(telemetry_freshness_s)
        self.pit_moving_speed_kph = float(pit_moving_speed_kph)
        self.pit_exit_speed_kph = float(pit_exit_speed_kph)
        self.reset()

    def reset(self) -> None:
        self._session_info: dict[str, object] = {}
        self._session_status: str | None = None
        self._session_part = "Q1"
        self._remaining_time_s: float | None = None
        self._clock_running = False
        self._clock_anchor_monotonic_s: float | None = None
        self._clock_anchor_session_time_s: float | None = None
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._tyre_stints: dict[str, dict[int, dict]] = {}
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
                self._freeze_clock()
                self._session_status = str(payload["Status"])
                self._clock_running = self._session_status.lower() in {"started", "active"}
                self._clock_anchor_monotonic_s = time.monotonic()
        elif message.topic == "ExtrapolatedClock" and isinstance(payload, Mapping):
            remaining = _lap_time_seconds(payload.get("Remaining"))
            if remaining is not None:
                declared_remaining = remaining
                if message.source == "signalr_live":
                    clock_utc = parse_timestamp(payload.get("Utc"))
                    observed_utc = (
                        message.source_timestamp_utc or message.received_at_utc
                    )
                    if clock_utc is not None:
                        remaining = max(
                            0.0,
                            remaining - max(
                                0.0, (observed_utc - clock_utc).total_seconds()
                            ),
                        )
                previous_remaining = self._effective_remaining_time_s()
                if (
                    previous_remaining is not None
                    and previous_remaining <= 5.0
                    and remaining > previous_remaining + 60.0
                ):
                    self._session_part = {
                        "Q1": "Q2",
                        "Q2": "Q3",
                        "SQ1": "SQ2",
                        "SQ2": "SQ3",
                    }.get(self._session_part, self._session_part)
                self._infer_session_part_from_clock_duration(declared_remaining)
                self._remaining_time_s = remaining
                self._clock_anchor_monotonic_s = time.monotonic()
                self._clock_anchor_session_time_s = candidate_time
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
        elif message.topic == "TimingAppData" and isinstance(payload, Mapping):
            self._apply_timing_app_data(payload)
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
            previous_in_pit = _as_bool(timing.get("InPit"))
            _deep_merge(timing, patch)
            current_in_pit = _as_bool(timing.get("InPit"))
            if current_in_pit is True and previous_in_pit is False:
                timing["_pit_arriving"] = True
                timing.pop("_pit_queue", None)
            elif current_in_pit is False and previous_in_pit is True:
                # InPit can turn false while the car is still queued in the
                # fast lane. Keep the departure latched until position or
                # speed confirms that the car has left pit lane.
                timing.pop("_pit_arriving", None)
                timing["_pit_queue"] = True
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

    def _apply_timing_app_data(self, payload: Mapping) -> None:
        lines = payload.get("Lines", payload)
        if not isinstance(lines, Mapping):
            return
        for number, driver_patch in lines.items():
            if not isinstance(driver_patch, Mapping):
                continue
            updates = driver_patch.get("Stints")
            if isinstance(updates, list):
                updates = {str(index): value for index, value in enumerate(updates)}
            if not isinstance(updates, Mapping):
                continue
            number = str(number)
            stints = self._tyre_stints.setdefault(number, {})
            for raw_index, stint_patch in updates.items():
                if not isinstance(stint_patch, Mapping):
                    continue
                index = _as_int(raw_index)
                if index is None or index < 0:
                    continue
                _deep_merge(stints.setdefault(index, {}), stint_patch)

    def _tyre_history(self, number: str) -> tuple[QualiTyreStint, ...]:
        history = []
        for _, stint in sorted(self._tyre_stints.get(number, {}).items()):
            start_laps = _as_int(stint.get("StartLaps"))
            if start_laps is None:
                continue
            compound = str(stint.get("Compound") or "UNKNOWN").upper()
            history.append(
                QualiTyreStint(
                    compound,
                    start_laps,
                    _as_bool(stint.get("New")),
                )
            )
        return tuple(history)

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
            speed = telemetry.get("speed")
            fresh_rpm = (
                telemetry.get("rpm") is not None and now_s is not None
                and now_s - telemetry.get("time", -math.inf) <= self.telemetry_freshness_s
                and telemetry["rpm"] > self.rpm_threshold
            )
            fresh_speed = (
                speed is not None and now_s is not None
                and now_s - telemetry.get("time", -math.inf) <= self.telemetry_freshness_s
            )
            moving_in_pit = fresh_speed and speed > self.pit_moving_speed_kph
            departed_by_speed = (
                in_pit is False
                and fresh_speed
                and speed >= self.pit_exit_speed_kph
            )
            departed_by_position = (
                in_pit is False
                and self.pit_lane_geometry is not None
                and position.get("time") is not None
                and progress is None
            )
            if departed_by_speed or departed_by_position:
                timing.pop("_pit_queue", None)
            elif (
                in_pit is True
                and moving_in_pit
                and not timing.get("_pit_arriving")
            ):
                timing["_pit_queue"] = True
            status = str(position.get("status", "")).lower()
            arriving = bool(timing.get("_pit_arriving"))
            inbound_outside_pit = arriving and (
                (
                    self.pit_lane_geometry is not None
                    and position.get("time") is not None
                    and progress is None
                )
                or (
                    self.pit_lane_geometry is None
                    and fresh_speed
                    and speed >= self.pit_exit_speed_kph
                )
            )
            # Position is authoritative while the car is physically in the
            # pit lane. TimingData may flip InPit before the car crosses the
            # pit exit line, which previously misclassified queued cars as
            # already on track.
            if inbound_outside_pit:
                pit_state = QualiPitState.IN_LAP
            elif arriving:
                pit_state = QualiPitState.IN_PIT
            elif progress is not None:
                timing["_pit_queue"] = True
                pit_state = QualiPitState.QUEUE
            elif timing.get("_pit_queue"):
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
                telemetry.get("time"), position.get("time"), self._tyre_history(number),
            ))
        states.sort(key=lambda value: (value.position is None, value.position or 999, value.driver_code))
        return QualiRawSnapshot(
            MappingProxyType(copy.deepcopy(self._session_info)), self._session_status,
            self._session_part, self._effective_remaining_time_s(), self._clock_running,
            tuple(states), self._last_message_at,
        )

    def _effective_remaining_time_s(self) -> float | None:
        remaining = self._remaining_time_s
        if (
            remaining is None
            or not self._clock_running
            or self._clock_anchor_monotonic_s is None
        ):
            return remaining
        elapsed_monotonic_s = time.monotonic() - self._clock_anchor_monotonic_s
        elapsed_session_s = (
            self._latest_session_time_s - self._clock_anchor_session_time_s
            if self._latest_session_time_s is not None
            and self._clock_anchor_session_time_s is not None
            else 0.0
        )
        elapsed_s = max(0, math.floor(max(elapsed_monotonic_s, elapsed_session_s)))
        return max(0.0, remaining - elapsed_s)

    def _freeze_clock(self) -> None:
        effective = self._effective_remaining_time_s()
        if effective is not None:
            self._remaining_time_s = effective
        self._clock_anchor_monotonic_s = time.monotonic()
        self._clock_anchor_session_time_s = self._latest_session_time_s

    def _infer_session_part_from_clock_duration(self, remaining: float) -> None:
        session_text = " ".join(
            str(self._session_info.get(key, "")) for key in ("Name", "Type")
        ).upper()
        sprint = "SPRINT" in session_text or "SHOOTOUT" in session_text
        durations = (
            (("SQ1", 12 * 60), ("SQ2", 10 * 60), ("SQ3", 8 * 60))
            if sprint
            else (("Q1", 18 * 60), ("Q2", 15 * 60), ("Q3", 12 * 60))
        )
        candidate = next(
            (part for part, duration in durations if abs(remaining - duration) <= 2.0),
            None,
        )
        parts = tuple(part for part, _ in durations)
        if (
            candidate is not None
            and self._session_part in parts
            and parts.index(candidate) >= parts.index(self._session_part)
        ):
            self._session_part = candidate


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
    upper = text.upper()
    for part in ("SQ1", "SQ2", "SQ3", "Q1", "Q2", "Q3"):
        if part in upper:
            return part
    if "SPRINT" in upper and ("QUALI" in upper or "SHOOTOUT" in upper):
        return "SQ1"
    return default


def _latest_sample_time(*sources: dict[str, dict]) -> float | None:
    values = [row.get("time") for source in sources for row in source.values() if row.get("time") is not None]
    return max(values) if values else None
