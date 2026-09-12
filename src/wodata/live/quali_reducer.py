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
    QualiTyreStint, QualiCompletedLap,
    QualiLapEvidence,
)
from .replay.schema import parse_timestamp
from .quali_clock import (DEFAULT_SESSION_DURATIONS_S, STANDARD_QUALI_PARTS, SPRINT_QUALI_PARTS, countdown_seconds)
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
        self._explicit_part: str | None = None
        self._remaining_time_s: float | None = None
        self._clock_running = False
        self._clock_anchor_session_time_s: float | None = None
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._tyre_stints: dict[str, dict[int, dict]] = {}
        self._telemetry: dict[str, dict] = {}
        self._position: dict[str, dict] = {}
        self._last_message_at: datetime | None = None
        self._latest_session_time_s: float | None = None
        self._normal_finished = False
        self._normal_finish_source: str | None = None
        self._segment_started = False
        self._segment_duration_s: float | None = None
        self._history: dict[str, dict[str, float | None]] = {}
        self._historical_best_s: float | None = None
        self._completed_lap_sequence: list[QualiCompletedLap] = []
        self._lap_evidence_sequence: list[QualiLapEvidence] = []
        self._segment_start_time_s: float | None = None
        self._utc_offset_s: float | None = None
        self._snapshot = self._build_snapshot()

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
        if candidate_time is None:
            candidate_time = (message.source_timestamp_utc or message.received_at_utc).timestamp()
        if candidate_time is not None:
            self._latest_session_time_s = max(
                float(candidate_time), self._latest_session_time_s or -math.inf
            )
        payload = message.payload
        if message.topic == "SessionInfo" and isinstance(payload, Mapping):
            _deep_merge(self._session_info, payload)
            declared_part = _explicit_session_part(payload)
            if declared_part is not None:
                self._explicit_part = declared_part
            self._set_part(self._explicit_part or _session_part(self._session_info, self._session_part))
        elif message.topic == "SessionStatus" and isinstance(payload, Mapping):
            if payload.get("Status") is not None:
                self._freeze_clock()
                self._session_status = str(payload["Status"])
                self._clock_running = self._session_status.lower() in {"started", "active"}
                if self._clock_running:
                    self._segment_started = True
                elif self._session_status.lower() in {"finished", "finalised", "ends"}:
                    self._normal_finished = True
                    self._normal_finish_source = "session_status"
        elif message.topic == "ExtrapolatedClock" and isinstance(payload, Mapping):
            remaining = countdown_seconds(payload.get("Remaining"))
            if remaining is not None:
                declared_remaining = remaining
                clock_utc = parse_timestamp(payload.get("Utc"))
                if message.session_time_seconds is not None and message.source_timestamp_utc is None and clock_utc is not None:
                    # Archive outer timestamps are session-relative; telemetry is UTC.
                    self._utc_offset_s = clock_utc.timestamp() - candidate_time
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
                    self._explicit_part is None
                    and self._segment_started
                    and previous_remaining is not None
                    and previous_remaining <= 5.0
                    and remaining > previous_remaining + 60.0
                ):
                    self._set_part({
                        "Q1": "Q2",
                        "Q2": "Q3",
                        "SQ1": "SQ2",
                        "SQ2": "SQ3",
                    }.get(self._session_part, self._session_part))
                # A remaining-time value is never sufficient evidence for a
                # segment: Q1 naturally passes 15:00.  Segment changes need an
                # explicit feed part or the observed zero-to-reset transition
                # above.
                if not self._segment_started and declared_remaining > 0:
                    self._segment_duration_s = declared_remaining
                self._remaining_time_s = remaining
                self._clock_anchor_session_time_s = candidate_time
            if payload.get("Extrapolating") is not None:
                if remaining is None:
                    self._freeze_clock()
                self._clock_running = bool(_as_bool(payload.get("Extrapolating")))
                self._segment_started |= self._clock_running
                if (
                    self._clock_running and self._segment_start_time_s is None
                    and declared_remaining is not None
                    and declared_remaining >= DEFAULT_SESSION_DURATIONS_S[self._session_part] - 1
                ):
                    # A first observation mid-session is not the segment start.
                    self._segment_start_time_s = candidate_time
        elif message.topic == "RaceControlMessages" and isinstance(payload, Mapping):
            messages = payload.get("Messages", {})
            values = messages.values() if isinstance(messages, Mapping) else messages
            if isinstance(values, (list, tuple)) or isinstance(messages, Mapping):
                for item in values:
                    if isinstance(item, Mapping) and item.get("Flag") == "CHEQUERED" and item.get("Scope", "Track") == "Track":
                        self._normal_finished = True
                        self._normal_finish_source = "race_control"
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
        if self._segment_started and self._effective_remaining_time_s() == 0:
            self._normal_finished = True
            self._normal_finish_source = self._normal_finish_source or "clock_zero"
        # All inference and latches are advanced once per ordered message.
        self._snapshot = self._build_snapshot()

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
                timing["_pit_entry_pending"] = True
                timing.pop("_pit_queue", None)
                timing["_pit_boundary_s"] = self._latest_session_time_s
                timing["_outbound_exit_confirmed"] = False
                timing["_out_lap_boundary_seen"] = False
                timing.pop("_confirmed_lap_number", None)
                self._record_lap_evidence(number, "pit_entry", None)
            elif current_in_pit is False and previous_in_pit is True:
                timing.pop("_pit_arriving", None)
                timing["_outbound"] = True
                timing["_pit_boundary_s"] = self._latest_session_time_s
                self._record_lap_evidence(number, "pit_exit", None)
            if "BestLapTime" in patch or "BestLap" in patch:
                timing["_official_best"] = True
                best = _lap_time_seconds(patch.get("BestLapTime") or patch.get("BestLap"))
                if best is not None:
                    timing["_segment_best_s"] = best
            last = _lap_time_seconds(patch.get("LastLapTime"))
            if last is not None:
                self._record_lap_evidence(number, "last_lap_publication", _as_int(patch.get("NumberOfLaps")), last)
            lap_number = _as_int(timing.get("NumberOfLaps")) or 0
            previous_counter = timing.get("_last_recorded_lap", 0)
            counter_advanced = "NumberOfLaps" in patch and lap_number > previous_counter
            if counter_advanced:
                timing["_last_recorded_lap"] = lap_number
                timing["_counter_boundary_s"] = self._latest_session_time_s
                timing.pop("_sector_lap_counter", None)
                self._record_lap_evidence(number, "lap_counter", lap_number)
                if timing.get("_outbound_exit_confirmed"):
                    timing["_out_lap_boundary_seen"] = True
            if "Sectors" in patch:
                sector_updates = _sector_time_updates(patch["Sectors"])
                minimum_elapsed = min(sector_updates.values(), default=None)
                boundary_time = timing.get("_counter_boundary_s")
                sector_can_belong_to_current_lap = bool(sector_updates) and (
                    boundary_time is None
                    or (
                        self._latest_session_time_s is not None
                        and self._latest_session_time_s - boundary_time >= minimum_elapsed - 1.0
                    )
                )
                if not counter_advanced and sector_can_belong_to_current_lap:
                    timing["_sector_lap_counter"] = lap_number
                self._record_lap_evidence(number, "sector_update", lap_number)
            completed_lap = False
            rejection = None
            if counter_advanced and last is not None:
                inferred_start = (
                    self._latest_session_time_s - last
                    if self._latest_session_time_s is not None else None
                )
                boundary = timing.get("_pit_boundary_s")
                if inferred_start is None:
                    rejection = "missing_event_time"
                elif self._segment_start_time_s is not None and inferred_start < self._segment_start_time_s - 1:
                    rejection = "cross_segment_interval"
                elif boundary is not None and inferred_start <= boundary <= self._latest_session_time_s:
                    rejection = "pit_boundary_inside_interval"
                elif current_in_pit is True or timing.get("_pit_arriving"):
                    rejection = "pit_in_interval"
                else:
                    completed_lap = True
            elif counter_advanced:
                rejection = "counter_without_same_update_lap_time"
            elif last is not None:
                rejection = "lap_time_without_same_update_counter"
            if completed_lap:
                completed_lap = True
                timing["_segment_laps"] = timing.get("_segment_laps", 0) + 1
                timing["_confirmed_lap_number"] = lap_number
                self._completed_lap_sequence.append(QualiCompletedLap(
                    self._session_part, str(number), lap_number, last,
                    self._latest_session_time_s,
                ))
            if rejection is not None:
                self._record_lap_evidence(number, "unassociated_lap", lap_number, last, rejection)
            # TimingData may carry an untimed out/in lap as LastLapTime after
            # the official segment result has been cleared.  Such a lap is
            # not a qualifying result; retain only plausible push-lap times
            # when no explicit official best is present.
            plausible_unofficial = (
                last is not None
                and last <= 240.0
                and (
                    self._historical_best_s is None
                    or last <= self._historical_best_s * 1.25
                )
            )
            if completed_lap and plausible_unofficial and not timing.get("_official_best"):
                previous = timing.get("_best_seen_s")
                timing["_best_seen_s"] = last if previous is None else min(float(previous), last)

    def _record_lap_evidence(
        self, number: object, kind: str, lap_number: int | None,
        lap_time_s: float | None = None, reason: str | None = None,
    ) -> None:
        self._lap_evidence_sequence.append(QualiLapEvidence(
            self._session_part, str(number), kind, lap_number,
            self._latest_session_time_s, lap_time_s, reason,
        ))

    def _apply_car_data(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        entries = payload.get("Entries", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            sample_time = self._sample_time(entry.get("Utc"), message)
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
            sample_time = self._sample_time(sample.get("Timestamp"), message)
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

    def _sample_time(self, value, message) -> float | None:
        if self._utc_offset_s is not None and message.source_timestamp_utc is None:
            timestamp = parse_timestamp(value)
            if timestamp is not None:
                return timestamp.timestamp() - self._utc_offset_s
        return _sample_time_seconds(value, message)

    def snapshot(self) -> QualiRawSnapshot:
        """Read the last message state; polling never advances domain time."""
        return self._snapshot

    def _build_snapshot(self) -> QualiRawSnapshot:
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
            position_fresh = (
                now_s is not None and position.get("time") is not None
                and 0 <= now_s - position["time"] <= self.telemetry_freshness_s
            )
            progress = position.get("pit_progress") if position_fresh else None
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
                and position_fresh
                and progress is None
            )
            if departed_by_speed or departed_by_position:
                if timing.get("_outbound") or (in_pit is False and timing.get("_pit_context")):
                    timing["_outbound_exit_confirmed"] = True
                timing["_outbound"] = False
            if in_pit is True:
                timing["_pit_context"] = True
            status = str(position.get("status", "")).lower() if position_fresh else ""
            arriving = bool(timing.get("_pit_arriving"))
            inbound_outside_pit = arriving and (
                (
                    self.pit_lane_geometry is not None
                    and position_fresh
                    and progress is None
                )
                or (
                    self.pit_lane_geometry is None
                    and fresh_speed
                    and speed >= self.pit_exit_speed_kph
                )
            )
            pit_side = in_pit is True or timing.get("_outbound") or arriving or progress is not None
            if timing.pop("_pit_entry_pending", False) or inbound_outside_pit:
                pit_state = QualiPitState.IN_LAP
            elif arriving:
                # An inbound stop may be a weighbridge/hot-swap stop.  No
                # pit-box geometry means it remains pit-lane transit.
                pit_state = QualiPitState.PIT_LANE
            elif timing.get("_outbound") or (pit_side and moving_in_pit):
                if fresh_speed and speed == 0 and timing.get("_outbound"):
                    pit_state = QualiPitState.QUEUE
                else:
                    pit_state = QualiPitState.PIT_LANE
            elif pit_side and fresh_speed and speed == 0 and timing.get("_outbound"):
                pit_state = QualiPitState.QUEUE
            elif pit_side and fresh_rpm:
                pit_state = QualiPitState.FIRE_UP
            elif pit_side:
                pit_state = QualiPitState.NOT_ACTIVE
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
                _as_int(timing.get("Position")), timing.get("_segment_laps", 0),
                _lap_time_seconds(timing.get("LastLapTime")),
                _lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                or (None if timing.get("_official_best") else timing.get("_best_seen_s")),
                tuple(sectors), segments, in_pit, stopped, pit_state, progress,
                telemetry.get("rpm"), telemetry.get("speed"), telemetry.get("gear"),
                telemetry.get("time"), position.get("time"), self._tyre_history(number),
                tuple(self._history.get(number, {}).items()),
                bool(pit_state == QualiPitState.ON_TRACK and (
                    timing.get("_confirmed_lap_number") == (_as_int(timing.get("NumberOfLaps")) or 0)
                    or (
                        timing.get("_out_lap_boundary_seen")
                        and timing.get("_sector_lap_counter") == (_as_int(timing.get("NumberOfLaps")) or 0)
                        and any(value is not None for value in sectors)
                    ) or (
                        not timing.get("_outbound_exit_confirmed") and
                        (_as_int(timing.get("NumberOfLaps")) or 0) > 0 and
                        (
                            timing.get("_sector_lap_counter") == (_as_int(timing.get("NumberOfLaps")) or 0)
                            and any(value is not None for value in sectors)
                        )
                    )
                )),
                (_as_int(timing.get("NumberOfLaps")) or 0) + 1,
                pit_state,
                bool(timing.get("_outbound_exit_confirmed") and not timing.get("_out_lap_boundary_seen")),
            ))
        states.sort(key=lambda value: (value.position is None, value.position or 999, value.driver_code))
        return QualiRawSnapshot(
            MappingProxyType(copy.deepcopy(self._session_info)), self._session_status,
            self._session_part, self._effective_remaining_time_s(), self._clock_running,
            tuple(states), self._last_message_at,
            self._normal_finished, self._normal_finish_source,
            self._segment_duration_s or DEFAULT_SESSION_DURATIONS_S[self._session_part],
            tuple(self._completed_lap_sequence),
            self._clock_anchor_session_time_s,
            tuple(self._lap_evidence_sequence),
        )

    def _effective_remaining_time_s(self) -> float | None:
        remaining = self._remaining_time_s
        if (
            remaining is None
            or not self._clock_running
        ):
            return remaining
        elapsed_session_s = (
            self._latest_session_time_s - self._clock_anchor_session_time_s
            if self._latest_session_time_s is not None
            and self._clock_anchor_session_time_s is not None
            else 0.0
        )
        elapsed_s = max(0, math.floor(elapsed_session_s))
        return max(0.0, remaining - elapsed_s)

    def _freeze_clock(self) -> None:
        effective = self._effective_remaining_time_s()
        if effective is not None:
            self._remaining_time_s = effective
        self._clock_anchor_session_time_s = self._latest_session_time_s

    def _set_part(self, part: str) -> None:
        if part == self._session_part:
            return
        for number, timing in self._timing.items():
            best = (_lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                    or timing.get("_segment_best_s") or timing.get("_best_seen_s"))
            self._history.setdefault(number, {})[self._session_part] = best
            if best is not None:
                self._historical_best_s = (
                    best if self._historical_best_s is None
                    else min(self._historical_best_s, best)
                )
            # Preserve physical pit state and cumulative feed lap number only.
            retained = {key: value for key, value in timing.items() if key in {
                "InPit", "Stopped", "NumberOfLaps", "_pit_arriving", "_pit_queue",
                "_departure_unknown", "_pit_context", "_outbound",
                "_outbound_exit_confirmed", "_out_lap_boundary_seen",
            }}
            retained["_last_recorded_lap"] = _as_int(timing.get("NumberOfLaps")) or 0
            timing.clear()
            timing.update(retained)
        observed_previous_segment = self._segment_started or self._normal_finished
        self._session_part = part
        self._segment_start_time_s = (
            self._latest_session_time_s if observed_previous_segment else None
        )
        self._record_lap_evidence("", "segment_start", None)
        self._segment_started = False
        self._normal_finished = False
        self._normal_finish_source = None
        self._segment_duration_s = None
        self._remaining_time_s = None
        self._clock_running = False
        self._clock_anchor_session_time_s = self._latest_session_time_s


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
        return default if default.startswith("SQ") else "SQ1"
    return default


def _latest_sample_time(*sources: dict[str, dict]) -> float | None:
    values = [row.get("time") for source in sources for row in source.values() if row.get("time") is not None]
    return max(values) if values else None


def _explicit_session_part(payload: Mapping) -> str | None:
    """Only explicit segment metadata overrides duration/reset inference."""
    part = str(payload.get("SessionPart", "")).upper()
    if part in DEFAULT_SESSION_DURATIONS_S:
        return part
    name = str(payload.get("Name", "")).upper()
    if name in DEFAULT_SESSION_DURATIONS_S:
        return name
    return None
