from __future__ import annotations

import copy
import base64
import math
import json
import zlib
from collections import deque
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from .models import (
    CompletedLap,
    CompletedSector,
    ProvisionalSector,
    LiveTimingState,
    LiveDriver,
    LiveSessionSnapshot,
    LiveTimingMessage,
    LiveTelemetrySample,
    LivePositionSample,
)

from .race_control import RaceControlHistory

SECTOR_NAMES = ("S1", "S2", "S3")


def _deep_merge(target: dict, patch: Mapping) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _as_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _lap_time_seconds(value: object) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("Value")
    if value is None:
        return None
    try:
        if isinstance(value, timedelta):
            seconds = value.total_seconds()
        elif isinstance(value, (int, float)):
            seconds = float(value)
        else:
            parts = str(value).strip().split(":")
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60.0 + float(part)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _sector_time_updates(
    value: object, *, include_clears: bool = False
) -> dict[int, float | None]:
    if isinstance(value, list):
        items = enumerate(value[:3])
    elif isinstance(value, Mapping):
        items = value.items()
    else:
        return {}
    sector_times = {}
    for index, sector in items:
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if index not in (0, 1, 2):
            continue
        # A missing Value (for example a Segments/PreviousValue patch) leaves
        # the duration unchanged. F1 explicitly resets it with Value="".
        raw_value = sector.get("Value") if isinstance(sector, Mapping) else sector
        if raw_value == "":
            if include_clears:
                sector_times[index] = None
            continue
        sector_time = _lap_time_seconds(sector)
        if sector_time is not None:
            sector_times[index] = sector_time
    return sector_times


def _is_older(candidate: datetime | None, current: datetime | None) -> bool:
    return candidate is not None and current is not None and candidate < current


def _combined_track_status(statuses: set[str]) -> str | None:
    values = sorted({str(value).strip() for value in statuses if str(value).strip()})
    if not values:
        return None
    return "".join(values)


class LiveSessionStore:
    def __init__(
        self,
        *,
        telemetry_lap_window: int | None = 12,
        position_sample_limit: int | None = None,
        allow_retirement_reactivation: bool = False,
    ) -> None:
        self.telemetry_lap_window = telemetry_lap_window
        self.position_sample_limit = position_sample_limit
        self.allow_retirement_reactivation = bool(allow_retirement_reactivation)
        self.reset()

    def reset(self) -> None:
        self._session_info: dict[str, object] = {}
        self._race_control_history = RaceControlHistory()
        self._session_status: str | None = None
        self._current_track_status: str | None = None
        self._scheduled_total_laps: int | None = None
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._live_registers: dict[str, dict] = {}
        self._latest_source_time_s: float | None = None
        self._tyre_stints: dict[str, dict[int, dict]] = {}
        self._physical_stints: dict[str, list[dict]] = {}
        self._tyre_pit_boundary: dict[str, int] = {}
        self._tyre_pit_boundary_time: dict[str, float | None] = {}
        self._tyre_pit_state: dict[str, dict] = {}
        self._racing_pit_laps: dict[str, dict[int, int]] = {}
        self._positions: dict[str, int] = {}
        self._laps: dict[str, dict[int, dict]] = {}
        self._retired_after_lap: dict[str, int] = {}
        self._pending: dict[str, dict] = {}
        self._telemetry_samples: dict[str, deque[LiveTelemetrySample]] = {}
        self._position_samples: dict[str, deque[LivePositionSample]] = {}
        self._last_message_at: datetime | None = None
        self._sequence = 0
        # Source/session time is the shared race clock; Started/Aborted pairs
        # define the intervals that do not belong on the racing-time axis.
        self._race_time_origin_s: float | None = None
        self._suspension_started_s: float | None = None
        self._suspensions: list[tuple[float, float]] = []
        self._active_suspension_lap_boundary: dict[str, int] | None = None
        self._suspension_lap_boundaries: list[
            tuple[float, float, dict[str, int]]
        ] = []
        self._session_status_state: str | None = None
        self._last_session_status_event_s: float | None = None
        self._earliest_timing_event_s: float | None = None
        self._has_observed_abort = False
        self.duplicate_completed_lap_count = 0
        self.corrected_completed_lap_count = 0
        self.invalid_lap_time_count = 0

    def apply(self, message: LiveTimingMessage) -> LiveSessionSnapshot:
        self.reduce(message)
        return self.snapshot()

    def apply_many(self, messages: list[LiveTimingMessage]) -> LiveSessionSnapshot:
        for message in messages:
            self.reduce(message)
        return self.snapshot()

    def reduce(self, message: LiveTimingMessage) -> None:
        """Apply one message without materialising a potentially large snapshot."""
        self._apply_message(message)

    def _apply_message(self, message: LiveTimingMessage) -> None:
        self._sequence += 1
        self._last_message_at = message.received_at_utc
        payload = message.payload
        source_time = _event_time_seconds(message.source_timestamp_utc, message.session_time_seconds)
        if source_time is not None:
            self._latest_source_time_s = max(source_time, self._latest_source_time_s or source_time)
        if message.topic == "SessionInfo" and isinstance(payload, Mapping):
            _deep_merge(self._session_info, payload)
        elif message.topic == "SessionStatus" and isinstance(payload, Mapping):
            status = payload.get("Status")
            if status is not None and self._apply_session_status(
                str(status),
                message.source_timestamp_utc,
                message.session_time_seconds,
            ):
                self._session_status = str(status)
        elif message.topic == "LapCount" and isinstance(payload, Mapping):
            total_laps = _as_int(payload.get("TotalLaps"))
            if (
                self._scheduled_total_laps is None
                and total_laps is not None
                and total_laps > 0
            ):
                self._scheduled_total_laps = total_laps
        elif message.topic == "TrackStatus" and isinstance(payload, Mapping):
            status = payload.get("Status")
            if status is not None:
                self._current_track_status = str(status)
                if "5" in str(status):
                    for state in self._tyre_pit_state.values():
                        state["racing_entry"] = False
                for pending in self._pending.values():
                    pending.setdefault("track_statuses", set()).add(str(status))
        elif message.topic == "RaceControlMessages" and isinstance(payload, Mapping):
            # Retain the original source coordinate; receipt/playback clocks
            # cannot order corrections to historical notices.
            publication_time = (
                ("session", float(message.session_time_seconds))
                if message.session_time_seconds is not None else
                ("utc", message.source_timestamp_utc.timestamp())
                if message.source_timestamp_utc is not None else None
            )
            self._race_control_history.apply(payload, publication_time)
        elif message.topic == "DriverList" and isinstance(payload, Mapping):
            self._apply_driver_list(payload, message.source_timestamp_utc)
        elif message.topic == "TimingData" and isinstance(payload, Mapping):
            self._apply_timing_data(
                payload,
                message.source_timestamp_utc,
                message.session_time_seconds,
            )
        elif message.topic == "TimingAppData" and isinstance(payload, Mapping):
            self._apply_timing_app_data(payload, source_time)
        elif message.topic == "CarData.z":
            self._apply_car_data(message)
        elif message.topic == "Position.z":
            self._apply_position_data(message)

    def _apply_session_status(
        self,
        status: str,
        timestamp: datetime | None,
        session_time_seconds: float | None,
    ) -> bool:
        normalized = status.strip().lower()
        if normalized not in {"aborted", "started"}:
            return True
        event_time_s = _event_time_seconds(timestamp, session_time_seconds)
        if event_time_s is None:
            return False
        late_initial_start = bool(
            normalized == "started"
            and self._race_time_origin_s is None
            and not self._has_observed_abort
            and self._earliest_timing_event_s is not None
            and event_time_s < self._earliest_timing_event_s
        )
        if late_initial_start:
            self._race_time_origin_s = event_time_s
            if self._session_status_state is None:
                self._session_status_state = "running"
            self._last_session_status_event_s = max(
                event_time_s,
                (
                    event_time_s
                    if self._last_session_status_event_s is None
                    else self._last_session_status_event_s
                ),
            )
            self._recompute_all_cumulative()
            return True
        if (
            self._last_session_status_event_s is not None
            and event_time_s < self._last_session_status_event_s
        ):
            return False
        if normalized == "aborted":
            self._has_observed_abort = True
            for state in self._tyre_pit_state.values():
                state["racing_entry"] = False
            if self._session_status_state == "suspended":
                self._last_session_status_event_s = event_time_s
                return True
            self._session_status_state = "suspended"
            self._suspension_started_s = event_time_s
            self._active_suspension_lap_boundary = {
                number: max(laps, default=0)
                for number, laps in self._laps.items()
            }
            self._last_session_status_event_s = event_time_s
            return True
        if self._session_status_state == "running":
            self._last_session_status_event_s = event_time_s
            return True
        if self._session_status_state == "suspended":
            if (
                self._suspension_started_s is not None
                and event_time_s >= self._suspension_started_s
            ):
                self._suspension_lap_boundaries.append(
                    (
                        self._suspension_started_s,
                        event_time_s,
                        dict(self._active_suspension_lap_boundary or {}),
                    )
                )
                self._add_suspension(self._suspension_started_s, event_time_s)
            self._suspension_started_s = None
            self._active_suspension_lap_boundary = None
            self._session_status_state = "running"
            self._last_session_status_event_s = event_time_s
            self._recompute_all_cumulative()
            return True

        # A first observed Started after timing evidence is a restart in a
        # truncated stream, not proof of the original race start.
        has_lap_evidence = bool(self._laps) or any(
            pending.get("lap") for pending in self._pending.values()
        )
        if self._race_time_origin_s is None and not has_lap_evidence:
            self._race_time_origin_s = event_time_s
        self._session_status_state = "running"
        self._last_session_status_event_s = event_time_s
        return True

    def _add_suspension(self, start_s: float, end_s: float) -> None:
        if end_s <= start_s:
            return
        periods = sorted((*self._suspensions, (start_s, end_s)))
        coalesced: list[tuple[float, float]] = []
        for candidate_start, candidate_end in periods:
            if not coalesced or candidate_start > coalesced[-1][1]:
                coalesced.append((candidate_start, candidate_end))
                continue
            previous_start, previous_end = coalesced[-1]
            coalesced[-1] = (previous_start, max(previous_end, candidate_end))
        self._suspensions = coalesced

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
                speed = _as_float(channels.get("2"))
                if speed is None or speed < 0:
                    continue
                number = str(number)
                current_lap = max(self._laps.get(number, {}), default=0) + 1
                samples = self._telemetry_samples.setdefault(number, deque())
                samples.append(
                    LiveTelemetrySample(
                        number,
                        sample_time,
                        current_lap,
                        speed,
                        message.source_timestamp_utc,
                    )
                )
                self._trim_telemetry(number, current_lap)

    def _apply_position_data(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        positions = payload.get("Position", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(positions, list):
            return
        for sample in positions:
            if not isinstance(sample, Mapping):
                continue
            # Position batches require their embedded shared clock. Falling back
            # to a packet timestamp invents simultaneity for undated entries.
            try:
                timestamp = datetime.fromisoformat(str(sample.get("Timestamp")).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    continue
            except (TypeError, ValueError):
                continue
            sample_time = _sample_time_seconds(sample.get("Timestamp"), message)
            entries = sample.get("Entries", {})
            if sample_time is None or not math.isfinite(sample_time) or not isinstance(entries, Mapping):
                continue
            for number, position in entries.items():
                if not isinstance(position, Mapping):
                    continue
                x = _as_float(position.get("X"))
                y = _as_float(position.get("Y"))
                z = _as_float(position.get("Z"))
                if x is None or y is None or z is None:
                    continue
                status = position.get("Status")
                normalized_status = (
                    str(status).strip().lower()
                    if isinstance(status, (str, int)) and not isinstance(status, bool)
                    else ""
                )
                on_track = {
                    "ontrack": True, "0": True, "offtrack": False, "1": False,
                }.get(normalized_status)
                latest = self._position_samples.setdefault(str(number), deque())
                value = LivePositionSample(str(number), sample_time, x, y, z, on_track)
                # Embedded sample time, not arrival order, controls retention. A
                # delayed batch must not evict newer positions or rewind the map.
                if not latest or sample_time > latest[-1].session_time_seconds:
                    latest.append(value)
                else:
                    by_time = {item.session_time_seconds: item for item in latest}
                    previous = by_time.get(sample_time)
                    if previous is not None and previous != value:
                        # Conflicting values at one timestamp cannot establish a
                        # common-time position. Preserve an unknown-status marker.
                        value = LivePositionSample(str(number), sample_time, x, y, z, None)
                    by_time[sample_time] = value
                    latest.clear()
                    latest.extend(by_time[key] for key in sorted(by_time))
                cutoff = latest[-1].session_time_seconds - 1200.0
                while latest and latest[0].session_time_seconds < cutoff:
                    latest.popleft()
                if self.position_sample_limit is not None:
                    limit = max(int(self.position_sample_limit), 1)
                    while len(latest) > limit:
                        latest.popleft()

    def _trim_telemetry(self, number: str, current_lap: int) -> None:
        if self.telemetry_lap_window is None:
            return
        minimum_lap = max(current_lap - int(self.telemetry_lap_window), 1)
        samples = self._telemetry_samples[number]
        while samples and samples[0].lap_number < minimum_lap:
            samples.popleft()

    def _apply_driver_list(self, payload: Mapping, timestamp: datetime | None) -> None:
        lines = payload.get("Lines", payload)
        if not isinstance(lines, Mapping):
            return
        for number, patch in lines.items():
            if not isinstance(patch, Mapping):
                continue
            driver = self._drivers.setdefault(str(number), {})
            _deep_merge(driver, patch)
            driver["_timestamp"] = timestamp

    def _apply_timing_data(
        self,
        payload: Mapping,
        timestamp: datetime | None,
        session_time_seconds: float | None,
    ) -> None:
        lines = payload.get("Lines", {})
        if not isinstance(lines, Mapping):
            return
        for number, patch in lines.items():
            if not isinstance(patch, Mapping):
                continue
            number = str(number)
            registers = self._live_registers.setdefault(number, {})
            event_time = _event_time_seconds(timestamp, session_time_seconds)
            for field in ("NumberOfLaps", "Position", "GapToLeader", "IntervalToPositionAhead"):
                if field not in patch:
                    continue
                previous_time = registers.get(field + "_time")
                if event_time is not None and previous_time is not None and event_time < previous_time:
                    continue
                registers[field] = copy.deepcopy(patch[field])
                registers[field + "_time"] = event_time
            timing = self._timing.setdefault(number, {})
            exited_pit = self._update_tyre_pit_boundary(number, patch, event_time)
            _deep_merge(timing, patch)
            position = _as_int(patch.get("Position"))
            if position is not None and position > 0:
                self._positions[number] = position
            if _as_bool(patch.get("Retired")) is True:
                completed_lap = max(
                    max(self._laps.get(number, {}), default=0),
                    _as_int(patch.get("NumberOfLaps")) or 0,
                )
                if completed_lap > 0:
                    self._retired_after_lap.setdefault(number, completed_lap)
            elif (
                self.allow_retirement_reactivation
                and "Retired" in patch
                and _as_bool(patch.get("Retired")) is False
            ):
                self._retired_after_lap.pop(number, None)
            self._update_pending_lap(number, patch, timestamp, session_time_seconds)
            if exited_pit:
                self._tyre_pit_boundary[number] = max(
                    max(self._laps.get(number, {}), default=0),
                    _as_int(registers.get("NumberOfLaps")) or 0,
                ) + 1
                state = self._tyre_pit_state[number]
                if state.pop("racing_exit", False):
                    in_lap = self._tyre_pit_boundary[number] - 1
                    if in_lap > 0:
                        evidence = self._racing_pit_laps.setdefault(number, {})
                        evidence[in_lap] = state["visit"]
                        evidence[in_lap + 1] = state["visit"]
                for slot in self._tyre_stints.get(number, {}).values():
                    if slot.get("fitting_candidate") is not None:
                        self._reconcile_tyre_slot(number, slot, {}, event_time)

    def _update_tyre_pit_boundary(
        self, number: str, patch: Mapping, event_time: float | None,
    ) -> bool:
        state = self._tyre_pit_state.setdefault(number, {})
        entered = exited = False
        for field in ("InPit", "PitOut"):
            if field not in patch:
                continue
            previous_time = state.get(field + "_time")
            if event_time is not None and previous_time is not None and event_time < previous_time:
                continue
            previous = state.get(field)
            value = _as_bool(patch[field])
            entered |= field == "InPit" and value is True and previous is not True
            exited |= ((field == "PitOut" and value is True and previous is not True)
                       or (field == "InPit" and value is False and previous is True))
            state[field] = value
            state[field + "_time"] = event_time
        if entered or (exited and not state.get("awaiting_exit")):
            state["visit"] = self._sequence
            self._tyre_pit_boundary_time[number] = event_time
            self._tyre_pit_boundary[number] = max(self._laps.get(number, {}), default=0) + 1
        if entered:
            state["awaiting_exit"] = True
            state["racing_entry"] = bool(
                event_time is not None
                and self._session_status_state == "running"
                and "5" not in str(self._current_track_status or "")
            )
        if exited:
            state["racing_exit"] = bool(
                state.get("racing_entry")
                and event_time is not None
                and self._session_status_state == "running"
                and "5" not in str(self._current_track_status or "")
            )
            state["racing_entry"] = False
            state["awaiting_exit"] = False
        return exited

    def _apply_timing_app_data(
        self, payload: Mapping, event_time: float | None = None,
    ) -> None:
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
            slots = self._tyre_stints.setdefault(number, {})
            for index, patch in updates.items():
                index = _as_int(index)
                if index is None or index < 0 or not isinstance(patch, Mapping):
                    continue
                slot = slots.setdefault(index, {})
                before = slot.copy()
                field_times = slot.setdefault("field_times", {})
                patch = {
                    key: value for key, value in patch.items()
                    if event_time is None or field_times.get(key) is None
                    or event_time >= field_times[key]
                }
                if not patch:
                    continue
                _deep_merge(slot, patch)
                if any(key in patch for key in ("Compound", "StartLaps", "New", "TotalLaps")):
                    slot.setdefault("introduced_lap", max(self._laps.get(number, {}), default=0) + 1)
                    slot.setdefault("introduced_time", event_time)
                for key in patch:
                    field_times[key] = event_time
                total = _as_int(patch.get("TotalLaps"))
                previous_total = _as_int(before.get("TotalLaps"))
                if total is not None and (not before or
                        (previous_total is not None and total < previous_total)):
                    slot["reset_pending"] = True
                self._reconcile_tyre_slot(number, slot, patch, event_time)

    def _reconcile_tyre_slot(
        self, number: str, slot: dict, patch: Mapping, event_time: float | None,
    ) -> None:
        # Slots are mutable metadata views, never permanent physical identities.
        compound = str(slot.get("Compound") or "").upper()
        generations = self._physical_stints.setdefault(number, [])
        current = generations[-1] if generations else None
        completed = max(self._laps.get(number, {}), default=0)
        start = _as_int(slot.get("StartLaps"))
        total = _as_int(slot.get("TotalLaps"))
        boundary = self._tyre_pit_boundary.get(number)
        boundary = boundary if current and boundary and boundary > current["fitted_lap"] else None
        evidence_time = (self._tyre_pit_boundary_time.get(number) if boundary is not None
                         else current.get("event_time") if current else None)
        if event_time is not None and evidence_time is not None and event_time < evidence_time:
            # A previously missing historical field may merge into its slot,
            # but it is not evidence for a later fitting boundary.
            return
        metadata = any(key in patch for key in ("Compound", "StartLaps", "New", "TyresNotChanged"))
        if boundary is not None:
            candidate = slot.get("fitting_candidate")
            visit = self._tyre_pit_state.get(number, {}).get("visit")
            if candidate is None or candidate["visit"] != visit:
                candidate = {"visit": visit, "totals": set(), "metadata": False}
                slot["fitting_candidate"] = candidate
            candidate["metadata"] |= metadata
            if "TyresNotChanged" in patch:
                candidate["no_change"] = _as_bool(patch["TyresNotChanged"]) is True
            if "TotalLaps" in patch and total is not None:
                candidate["totals"].add(total)
        else:
            candidate = None
        fitted = start is not None and (
            start in candidate["totals"] if candidate is not None else total == start
        )
        reset = fitted and slot.get("reset_pending", False)
        if compound in ("", "UNKNOWN"):
            return
        if current is not None and self._tyre_pit_state.get(number, {}).get("awaiting_exit"):
            return
        no_change = (_as_bool(patch.get("TyresNotChanged")) is True
                     or (candidate is not None and candidate.get("no_change", False)))
        # An explicit no-change patch is evidence about this visit only; an old
        # merged flag must not veto later reuse of the slot.
        if no_change:
            self._tyre_pit_boundary.pop(number, None)
            slot["reset_pending"] = False
        unowned = "generation" not in slot
        transition = current is None or (
            not no_change and completed >= current["fitted_lap"]
            and ((fitted and candidate is not None and candidate["metadata"])
                 or (reset and metadata)
                 or (unowned and (metadata or (candidate is not None and candidate["metadata"]))
                     and total is None and start is not None))
        )
        if transition:
            fitted_lap = boundary or (
                slot.get("introduced_lap", completed + 1) if unowned and total is None
                else completed + 1
            )
            current = {
                "stint": len(generations) + 1,
                "event_time": (self._tyre_pit_boundary_time.get(number) if boundary is not None
                               else slot.get("introduced_time") if unowned else event_time),
                "fitted_lap": fitted_lap,
                "Compound": compound,
                "StartLaps": start,
                "New": slot.get("New"),
            }
            generations.append(current)
            slot["generation"] = current["stint"]
            slot["reset_pending"] = False
            self._tyre_pit_boundary.pop(number, None)
            if len(generations) > 1 and fitted_lap - 1 in self._laps.get(number, {}):
                self._laps[number][fitted_lap - 1]["pit_in"] = True
        elif current is not None:
            # Compatible remappings (including lower slots) alias the current
            # physical stint. TotalLaps can arrive before its timing counter.
            age = (current.get("StartLaps") or 0) + completed - current["fitted_lap"] + 1
            compatible = compound == current["Compound"] and total in (age, age + 1)
            if metadata and compatible:
                slot["generation"] = current["stint"]
                slot["reset_pending"] = False
            if slot.get("generation") != current["stint"] or not metadata:
                return
            # A reset/UNKNOWN staging patch awaits fitting metadata. Do not
            # rewrite the previous stint using a partially described new tyre.
            if boundary is not None or (slot.get("reset_pending") and not no_change):
                return
            current["Compound"] = compound
            for key in ("StartLaps", "New"):
                if key in patch:
                    current[key] = _as_int(patch[key]) if key == "StartLaps" else patch[key]
        # Delayed metadata repairs only the owned interval, never earlier tyres.
        if current is not None:
            for lap, data in self._laps.get(number, {}).items():
                if lap >= current["fitted_lap"]:
                    data.update(self._tyre_assignment(number, lap))

    def _tyre_assignment(self, number: str, lap: int) -> dict[str, object]:
        candidates = [data for data in self._physical_stints.get(number, [])
                      if data["fitted_lap"] <= lap]
        if not candidates:
            return {"compound": None, "tyre_life": None, "stint": None, "fresh_tyre": None}
        data = candidates[-1]
        start_laps = data.get("StartLaps")
        return {
            "compound": data["Compound"],
            "tyre_life": (float(start_laps + lap - data["fitted_lap"] + 1)
                          if start_laps is not None else None),
            "stint": data["stint"],
            "fresh_tyre": _as_bool(data.get("New")),
            "pit_out": bool(data["stint"] > 1 and data["fitted_lap"] == lap),
        }

    def _update_pending_lap(
        self,
        number: str,
        patch: Mapping,
        timestamp: datetime | None,
        session_time_seconds: float | None,
    ) -> None:
        pending = self._pending.setdefault(
            number,
            {
                "lap": None,
                "lap_seq": 0,
                "lap_timestamp": None,
                "lap_event_time_s": None,
                "time": None,
                "time_seq": 0,
                "time_timestamp": None,
                "time_event_time_s": None,
                "sectors": None,
                "sector_values": [None, None, None],
                "sector_event_times_s": [None, None, None],
                "sector_lap": None,
                "counter_events": {},
                "time_lap": None,
                "sector_clears_s": [None, None, None],
                "sector_started_after_s": None,
                "sectors_seq": 0,
                "sectors_timestamp": None,
                "sectors_session_time_seconds": None,
                "sectors_event_time_s": None,
                "session_time_seconds": None,
                "committed_seq": 0,
                "track_statuses": (
                    {self._current_track_status} if self._current_track_status else set()
                ),
            },
        )
        event_time_s = _event_time_seconds(timestamp, session_time_seconds)
        has_timing_evidence = bool(
            (_as_int(patch.get("NumberOfLaps")) or 0) > 0
            or _lap_time_seconds(patch.get("LastLapTime")) is not None
            or _sector_time_updates(patch.get("Sectors"))
        )
        if event_time_s is not None and has_timing_evidence:
            self._earliest_timing_event_s = min(
                event_time_s,
                (
                    event_time_s
                    if self._earliest_timing_event_s is None
                    else self._earliest_timing_event_s
                ),
            )
        patch_lap = _as_int(patch.get("NumberOfLaps"))
        if patch_lap is not None and patch_lap > 0:
            counters = pending["counter_events"]
            if patch_lap not in counters:
                counters[patch_lap] = (event_time_s, timestamp, self._sequence)
            if pending["lap"] is None or patch_lap > pending["lap"]:
                pending.update(lap=patch_lap, lap_seq=self._sequence,
                               lap_timestamp=timestamp, lap_event_time_s=event_time_s)
        fresh_time = _lap_time_seconds(patch.get("LastLapTime"))
        if "LastLapTime" in patch:
            if fresh_time is None:
                self.invalid_lap_time_count += 1
            else:
                target_lap = patch_lap
                if target_lap is None and event_time_s is not None:
                    eligible = [(stamp, lap) for lap, (stamp, _, _) in
                                pending["counter_events"].items()
                                if stamp is not None and stamp <= event_time_s]
                    if eligible:
                        target_lap = max(eligible)[1]
                if target_lap is None:
                    target_lap = pending["lap"]
                # Preserve each counter's identity when an older completion is
                # published after a newer counter. This is completion routing,
                # not a claim that publication timestamps are line crossings.
                existing = self._laps.get(number, {}).get(target_lap, {})
                existing_time = existing.get("time_event_time_s")
                if not (event_time_s is not None and existing_time is not None
                        and event_time_s < existing_time):
                    pending.update(time=fresh_time, time_seq=self._sequence,
                                   time_timestamp=timestamp, time_event_time_s=event_time_s,
                                   time_lap=target_lap)
        self._update_provisional_sectors(number, pending, patch, timestamp,
                                         session_time_seconds, event_time_s)
        lap = pending["time_lap"] if fresh_time is not None else pending["lap"]
        if lap is None:
            lap = pending["lap"]
        lap_time = pending["time"]
        if lap is None or lap_time is None:
            return
        counter = pending["counter_events"].get(lap)
        if counter is None:
            return
        previous_lap = self._laps.get(number, {}).get(lap - 1)
        previous_boundary = pending["counter_events"].get(lap - 1, (None,))[0]
        if previous_boundary is None and previous_lap is not None:
            previous_boundary = previous_lap.get("lap_end_time_s")
        if lap == 1 and not self._has_observed_abort:
            # An observed initial Started can bound lap 1; a race origin
            # inferred by subtracting its duration cannot certify its sectors.
            previous_boundary = self._last_session_status_event_s
        sectors = pending["sectors"]
        sector_times = pending["sector_event_times_s"]
        reconciled_sectors = None
        if (sectors is not None and math.isclose(sum(sectors), lap_time, abs_tol=0.01)
                and previous_boundary is not None and counter[0] is not None
                and all(value is not None and previous_boundary < value <= counter[0]
                        for value in sector_times)):
            # Certification uses independently observed publication boundaries,
            # never provisional identity or a boundary extended by a late LLT.
            # Sum equality is only a consistency check within that interval.
            reconciled_sectors = sectors
        if previous_lap is not None and not previous_lap.get("is_valid", True):
            # A later endpoint may retro-anchor an unavailable restart lap.
            # That recovery does not establish the intervening sector history.
            reconciled_sectors = None
        retirement_lap = self._retired_after_lap.get(number)
        if retirement_lap is not None and lap > retirement_lap:
            return
        driver_laps = self._laps.setdefault(number, {})
        latest_lap = max(driver_laps, default=0)
        if lap in driver_laps and pending["time_seq"] > pending["committed_seq"]:
            existing = driver_laps[lap]
            if math.isclose(
                existing.get("source_lap_time_s", existing["lap_time_s"]),
                lap_time,
                abs_tol=1e-9,
            ):
                self.duplicate_completed_lap_count += 1
            else:
                existing["source_lap_time_s"] = lap_time
                existing["lap_time_s"] = lap_time
                if reconciled_sectors is not None:
                    existing["sector_times_s"] = reconciled_sectors
                existing["time_event_time_s"] = pending["time_event_time_s"]
                existing["source_timestamp_utc"] = timestamp
                existing["session_time_seconds"] = session_time_seconds
                self.corrected_completed_lap_count += 1
                self._recompute_cumulative(number, lap)
            pending["committed_seq"] = self._sequence
            pending["time_seq"] = 0
            return
        if lap in driver_laps:
            return
        fresh_time = (pending["time_seq"] > pending["committed_seq"]
                      and pending["time_lap"] in (None, lap))
        # Differential patches omit unchanged lap times. Reuse requires a
        # complete generation ending by this counter's publication; a later
        # ambiguous tuple is insufficient.
        reuse_merged_time = not fresh_time and self._can_reuse_merged_lap_time(
            pending, lap=lap, latest_lap=latest_lap, lap_time_s=lap_time,
            latest_lap_end_s=driver_laps.get(latest_lap, {}).get("lap_end_time_s"),
        )
        if not (fresh_time or reuse_merged_time):
            return
        evidence_timestamps = [counter[1]]
        evidence_event_times = [counter[0]]
        if fresh_time:
            evidence_timestamps.append(pending["time_timestamp"])
            evidence_event_times.append(pending["time_event_time_s"])
        if reuse_merged_time:
            evidence_timestamps.append(pending["sectors_timestamp"])
            evidence_event_times.append(pending["sectors_event_time_s"])
        lap_end_time_s = max(
            (value for value in evidence_event_times if value is not None),
            default=None,
        )
        driver_laps[lap] = {
            "source_lap_time_s": lap_time,
            "lap_time_s": lap_time,
            "cumulative_time_s": 0.0,
            "sector_times_s": reconciled_sectors,
            "time_event_time_s": pending["time_event_time_s"],
            "sector_event_time_s": pending.get("sectors_event_time_s"),
            "position": self._positions.get(number),
            "source_timestamp_utc": max(
                (value for value in evidence_timestamps if value),
                default=None,
            ),
            "lap_end_time_s": lap_end_time_s,
            "session_time_seconds": session_time_seconds,
            "track_status": _combined_track_status(pending.get("track_statuses", set())),
            "pit_in": _as_bool(self._timing.get(number, {}).get("InPit")) is True,
            **self._tyre_assignment(number, lap),
        }
        self._recompute_cumulative(number, lap)
        pending["committed_seq"] = self._sequence
        pending["lap_seq"] = 0
        pending["time_seq"] = 0
        if pending["sector_lap"] is None or pending["sector_lap"] <= lap:
            pending["sectors"] = None
            pending["sector_values"] = [None, None, None]
            pending["sector_event_times_s"] = [None, None, None]
            pending["sector_lap"] = None
            pending["sectors_seq"] = 0
            pending["sectors_timestamp"] = None
            pending["sectors_session_time_seconds"] = None
            pending["sectors_event_time_s"] = None
        pending["track_statuses"] = (
            {self._current_track_status} if self._current_track_status else set()
        )

    def _update_provisional_sectors(
        self, number: str, pending: dict, patch: Mapping, timestamp: datetime | None,
        session_time_seconds: float | None, event_time_s: float | None,
    ) -> None:
        updates = _sector_time_updates(patch.get("Sectors"), include_clears=True)
        if not updates:
            return
        counter_lap = pending["lap"] or 0
        counter_time = pending["lap_event_time_s"]
        last_committed = max(self._laps.get(number, {}), default=0)
        explicit_completion = (_as_int(patch.get("NumberOfLaps")) is not None
                               and _lap_time_seconds(patch.get("LastLapTime")) is not None)
        after_counter = (counter_time is not None and event_time_s is not None
                         and event_time_s > counter_time)
        # Current display identity may be revised at completion. It is never
        # exported through completed_sectors until separately reconciled.
        desired_lap = (_as_int(patch.get("NumberOfLaps")) if explicit_completion else
                       counter_lap + 1 if after_counter else
                       counter_lap if counter_lap > last_committed else last_committed + 1)
        if pending["sector_lap"] is None:
            pending["sector_lap"] = desired_lap
            pending["sector_started_after_s"] = counter_time if after_counter else None
        elif updates.get(0) is not None and (desired_lap > pending["sector_lap"]
                                              or pending["sectors"] is not None):
            previous_s1 = pending["sector_event_times_s"][0]
            if previous_s1 is None or event_time_s is None or event_time_s > previous_s1:
                pending["sector_lap"] = desired_lap
                pending["sector_started_after_s"] = counter_time if after_counter else None
                pending["sector_values"] = [None, None, None]
                pending["sector_event_times_s"] = [None, None, None]
                # A reset delivered before an older S1 still protects the
                # addressed fields from older values arriving afterwards.
                pending["sector_clears_s"] = [
                    clear if (clear is not None and event_time_s is not None
                              and clear >= event_time_s) else None
                    for clear in pending["sector_clears_s"]
                ]
        if explicit_completion and len(_sector_time_updates(patch.get("Sectors"))) == 3:
            pending["sector_lap"] = desired_lap
        accepted = False
        for index, value in updates.items():
            generation_start = pending["sector_started_after_s"]
            if (generation_start is not None and event_time_s is not None
                    and event_time_s <= generation_start and not explicit_completion):
                continue
            previous = pending["sector_event_times_s"][index]
            clear = pending["sector_clears_s"][index]
            watermark = max((t for t in (previous, clear) if t is not None), default=None)
            if event_time_s is not None and watermark is not None and event_time_s < watermark:
                continue
            if value is not None and clear is not None and event_time_s is not None and event_time_s <= clear:
                continue
            if value is None:
                pending["sector_clears_s"][index] = event_time_s
            pending["sector_values"][index] = value
            pending["sector_event_times_s"][index] = event_time_s
            accepted = True
        if accepted and (pending["sectors_event_time_s"] is None or event_time_s is None
                         or event_time_s >= pending["sectors_event_time_s"]):
            pending.update(sectors_seq=self._sequence, sectors_timestamp=timestamp,
                           sectors_session_time_seconds=session_time_seconds,
                           sectors_event_time_s=event_time_s)
        pending["sectors"] = (tuple(pending["sector_values"])
                              if all(value is not None for value in pending["sector_values"])
                              else None)

    @staticmethod
    def _can_reuse_merged_lap_time(
        pending: dict, *, lap: int, latest_lap: int, lap_time_s: float,
        latest_lap_end_s: float | None,
    ) -> bool:
        sectors = pending.get("sectors")
        boundary = pending["counter_events"].get(lap, (None,))[0]
        return bool(
            lap == latest_lap + 1 and sectors is not None
            and pending.get("sector_lap") == lap
            and boundary is not None
            and all(value is not None and value <= boundary
                    and (latest_lap_end_s is None or value > latest_lap_end_s)
                    for value in pending["sector_event_times_s"])
            and math.isclose(sum(sectors), lap_time_s, abs_tol=0.01)
        )

    def _recompute_all_cumulative(self) -> None:
        for number in self._laps:
            self._recompute_cumulative(number, 1)

    def _suspended_seconds_between(self, start_s: float, end_s: float) -> float:
        if end_s <= start_s:
            return 0.0
        periods = list(self._suspensions)
        if self._suspension_started_s is not None:
            periods.append((self._suspension_started_s, end_s))
        return sum(
            max(0.0, min(end_s, suspension_end) - max(start_s, suspension_start))
            for suspension_start, suspension_end in periods
        )

    def _racing_elapsed(self, event_time_s: float | None) -> float | None:
        if event_time_s is None or self._race_time_origin_s is None:
            return None
        if event_time_s < self._race_time_origin_s:
            return None
        return (
            event_time_s
            - self._race_time_origin_s
            - self._suspended_seconds_between(self._race_time_origin_s, event_time_s)
        )

    def _unanchored_suspension_laps(
        self, number: str, driver_laps: Mapping[int, dict]
    ) -> set[int]:
        unanchored = set()
        lap_numbers = sorted(driver_laps)
        for _start_s, _end_s, boundaries in self._suspension_lap_boundaries:
            boundary_lap = boundaries.get(number, 0)
            candidate = next((lap for lap in lap_numbers if lap > boundary_lap), None)
            if (
                candidate is not None
                and driver_laps[candidate].get("lap_end_time_s") is None
            ):
                unanchored.add(candidate)
        return unanchored

    def _is_pending_suspension_lap(self, number: str, pending_lap: int) -> bool:
        committed_laps = self._laps.get(number, {})
        for _start_s, _end_s, boundaries in self._suspension_lap_boundaries:
            boundary_lap = boundaries.get(number, 0)
            if pending_lap <= boundary_lap:
                continue
            if not any(lap > boundary_lap for lap in committed_laps):
                return True
        return False

    def _recompute_cumulative(self, number: str, _start_lap: int) -> None:
        driver_laps = self._laps[number]
        if (
            self._race_time_origin_s is None
            and 1 in driver_laps
            and self._session_status_state != "suspended"
        ):
            first = driver_laps[1]
            first_end_s = first.get("lap_end_time_s")
            if first_end_s is not None:
                self._race_time_origin_s = first_end_s - first["source_lap_time_s"]
                self._recompute_all_cumulative()
                return
        lap_numbers = sorted(driver_laps)
        unanchored_suspension_laps = self._unanchored_suspension_laps(
            number, driver_laps
        )
        axis_lap_times: dict[int, float | None] = {}
        previous_end_s = None
        expected_lap = None
        for lap in lap_numbers:
            row = driver_laps[lap]
            source_lap_time_s = row.get("source_lap_time_s", row["lap_time_s"])
            lap_end_time_s = row.get("lap_end_time_s")
            interval_start_s = (
                previous_end_s
                if previous_end_s is not None and expected_lap == lap
                else (
                    lap_end_time_s - source_lap_time_s
                    if lap_end_time_s is not None
                    else None
                )
            )
            corrected_lap_time_s = source_lap_time_s
            if lap in unanchored_suspension_laps:
                corrected_lap_time_s = None
            elif interval_start_s is not None and lap_end_time_s is not None:
                corrected_lap_time_s -= self._suspended_seconds_between(
                    interval_start_s, lap_end_time_s
                )
                if corrected_lap_time_s <= 0.0:
                    corrected_lap_time_s = None
            axis_lap_times[lap] = corrected_lap_time_s
            expected_lap = lap + 1
            previous_end_s = lap_end_time_s

        # An unavailable restart lap can be recovered backwards from the next
        # absolute endpoint when every intervening duration is known.
        retroactive_endpoints: dict[int, float] = {}
        for unanchored_lap in unanchored_suspension_laps:
            following_duration_s = 0.0
            expected_lap = unanchored_lap + 1
            for lap in lap_numbers:
                if lap <= unanchored_lap:
                    continue
                if lap != expected_lap or axis_lap_times[lap] is None:
                    break
                following_duration_s += axis_lap_times[lap]
                absolute_endpoint = self._racing_elapsed(
                    driver_laps[lap].get("lap_end_time_s")
                )
                if absolute_endpoint is not None:
                    retroactive_endpoints[unanchored_lap] = (
                        absolute_endpoint - following_duration_s
                    )
                    break
                expected_lap = lap + 1

        cumulative = 0.0
        starts_at_race_origin = min(driver_laps, default=0) == 1
        expected_lap = None
        has_gap = False
        previous_canonical = None
        for lap in lap_numbers:
            if expected_lap is not None and lap != expected_lap:
                has_gap = True
            row = driver_laps[lap]
            corrected_lap_time_s = axis_lap_times[lap]
            row["lap_time_s"] = (
                corrected_lap_time_s
                if corrected_lap_time_s is not None
                else float("nan")
            )
            lap_time_axis_valid = corrected_lap_time_s is not None
            lap_end_time_s = row.get("lap_end_time_s")
            absolute_cumulative = self._racing_elapsed(lap_end_time_s)
            retroactive_endpoint = retroactive_endpoints.get(lap)
            if (
                retroactive_endpoint is not None
                and previous_canonical is not None
                and retroactive_endpoint > previous_canonical
            ):
                row["lap_time_s"] = retroactive_endpoint - previous_canonical
                lap_time_axis_valid = True
                canonical_cumulative = retroactive_endpoint
            elif not lap_time_axis_valid or self._race_time_origin_s is None:
                canonical_cumulative = None
            # Complete histories retain precise published lap durations. A
            # late/internal gap is anchored to the shared source clock.
            elif absolute_cumulative is not None and (has_gap or not starts_at_race_origin):
                canonical_cumulative = absolute_cumulative
            elif has_gap:
                canonical_cumulative = None
            else:
                canonical_cumulative = cumulative + row["lap_time_s"]
            if (
                canonical_cumulative is not None
                and previous_canonical is not None
                and canonical_cumulative < previous_canonical
            ):
                canonical_cumulative = None
                lap_time_axis_valid = False
            row["sector_cumulative_times_s"] = None
            sector_times = row.get("sector_times_s")
            sector_total_s = sum(sector_times) if sector_times is not None else None
            if (
                sector_times is not None
                and canonical_cumulative is not None
                and math.isclose(sector_total_s, row["lap_time_s"], abs_tol=0.01)
            ):
                running = canonical_cumulative - sector_total_s
                sector_cumulative = []
                for sector_time in sector_times:
                    running += sector_time
                    sector_cumulative.append(running)
                sector_cumulative[-1] = canonical_cumulative
                row["sector_cumulative_times_s"] = tuple(sector_cumulative)
            if canonical_cumulative is not None:
                cumulative = canonical_cumulative
                previous_canonical = canonical_cumulative
            elif lap_time_axis_valid:
                cumulative += row["lap_time_s"]
            else:
                has_gap = True
            row["cumulative_time_s"] = canonical_cumulative
            row["is_valid"] = lap_time_axis_valid
            expected_lap = lap + 1

    def snapshot(self) -> LiveSessionSnapshot:
        drivers = []
        for number, data in self._drivers.items():
            code = str(data.get("Tla") or data.get("BroadcastName") or number)
            full_name = str(
                data.get("FullName")
                or " ".join(filter(None, (data.get("FirstName"), data.get("LastName"))))
                or code
            )
            drivers.append(
                LiveDriver(
                    number,
                    code,
                    str(data.get("TeamName") or data.get("Team") or ""),
                    full_name,
                    self._positions.get(number),
                    data.get("_timestamp"),
                    number in self._retired_after_lap,
                    self._retired_after_lap.get(number),
                )
            )
        drivers.sort(key=lambda item: (item.position is None, item.position or 999, item.driver_number))

        completed_laps = []
        completed_sectors = []
        provisional_sectors = []
        live_timing = []
        driver_lookup = {driver.driver_number: driver for driver in drivers}
        timing_numbers = set(self._laps).union(self._pending)
        for number in sorted(timing_numbers):
            laps = self._laps.get(number, {})
            driver = driver_lookup.get(number)
            code = driver.driver_code if driver else number
            team = driver.team if driver else ""
            registers = self._live_registers.get(number, {})
            interval = registers.get("IntervalToPositionAhead")
            if isinstance(interval, Mapping):
                interval = interval.get("Value")
            gap = _as_float(registers.get("GapToLeader"))
            position = _as_int(registers.get("Position"))
            if (position == 1 and gap is None
                    and str(registers.get("GapToLeader", "")).upper().startswith("LAP ")):
                gap = 0.0
            live_timing.append(LiveTimingState(
                number, code, _as_int(registers.get("NumberOfLaps")), position,
                gap, _as_float(interval), registers.get("Position_time"),
                registers.get("GapToLeader_time"), registers.get("IntervalToPositionAhead_time"),
                self._latest_source_time_s,
            ))
            for lap, data in sorted(laps.items()):
                completed_laps.append(
                    CompletedLap(
                        number,
                        code,
                        team,
                        lap,
                        data["lap_time_s"],
                        data["cumulative_time_s"],
                        data["position"],
                        data["source_timestamp_utc"],
                        data.get("compound"),
                        data.get("tyre_life"),
                        data.get("stint"),
                        data.get("fresh_tyre"),
                        data.get("session_time_seconds"),
                        data.get("track_status"),
                        bool(data.get("pit_out", False)),
                        bool(data.get("pit_in", False)),
                        bool(data.get("is_valid", True)),
                        self._racing_pit_laps.get(number, {}).get(lap),
                    )
                )
                sector_times = data.get("sector_times_s")
                sector_cumulative_times = data.get("sector_cumulative_times_s")
                if sector_times is None or sector_cumulative_times is None:
                    continue
                for sector_index, (sector, sector_time) in enumerate(
                    zip(SECTOR_NAMES, sector_times, strict=False),
                    start=1,
                ):
                    cumulative_time = (
                        sector_cumulative_times[sector_index - 1]
                        if sector_cumulative_times is not None
                        else None
                    )
                    completed_sectors.append(
                        CompletedSector(
                            number,
                            code,
                            team,
                            lap,
                            sector,
                            lap * 3 - 3 + sector_index,
                            sector_time,
                            cumulative_time,
                            data["position"],
                            data["source_timestamp_utc"],
                            data.get("compound"),
                            data.get("tyre_life"),
                            data.get("stint"),
                            data.get("fresh_tyre"),
                            data.get("session_time_seconds"),
                        )
                    )
            pending = self._pending.get(number, {})
            pending_lap = pending.get("sector_lap")
            retirement_lap = self._retired_after_lap.get(number)
            if (
                pending_lap is None
                or pending_lap in laps
                or (retirement_lap is not None and pending_lap > retirement_lap)
            ):
                continue
            previous_lap = laps.get(pending_lap - 1)
            base_cumulative = (
                previous_lap.get("cumulative_time_s") if previous_lap else None
            )
            if (
                pending_lap == 1
                and not pending.get("lap")
                and self._race_time_origin_s is not None
            ):
                base_cumulative = 0.0
            pause_unsafe = self._is_pending_suspension_lap(number, pending_lap)
            previous_end = previous_lap.get("lap_end_time_s") if previous_lap else None
            sector_events = pending.get("sector_event_times_s", ())
            for pause_start, pause_end in self._suspensions:
                if ((previous_end is None or previous_end < pause_end)
                        and any(value is not None and value >= pause_start for value in sector_events)):
                    pause_unsafe = True
            if self._session_status_state == "suspended":
                pause_unsafe = True
            if pause_unsafe:
                base_cumulative = None
            running = base_cumulative
            tyre = self._tyre_assignment(number, pending_lap)
            for sector_index, sector_time in enumerate(
                pending.get("sector_values", ()), start=1
            ):
                if sector_time is None:
                    break
                if running is not None:
                    running += sector_time
                elif not pause_unsafe:
                    # Publication-clock estimate is useful for live progress
                    # but is never a completed historical coordinate.
                    running = self._racing_elapsed(sector_events[sector_index - 1])
                provisional_sectors.append(
                    ProvisionalSector(
                        number,
                        code,
                        team,
                        pending_lap,
                        SECTOR_NAMES[sector_index - 1],
                        pending_lap * 3 - 3 + sector_index,
                        sector_time,
                        running,
                        self._positions.get(number),
                        pending.get("sectors_timestamp"),
                        tyre.get("compound"),
                        tyre.get("tyre_life"),
                        tyre.get("stint"),
                        tyre.get("fresh_tyre"),
                        pending.get("sectors_session_time_seconds"),
                        pending.get("sector_event_times_s", [None] * 3)[sector_index - 1],
                    )
                )
        completed_laps.sort(key=lambda item: (item.lap_number, item.driver_number))
        completed_sectors.sort(key=lambda item: (item.segment_index, item.driver_number))
        return LiveSessionSnapshot(
            MappingProxyType(copy.deepcopy(self._session_info)),
            self._session_status,
            tuple(drivers),
            tuple(completed_laps),
            MappingProxyType(dict(self._positions)),
            self._last_message_at,
            tuple(completed_sectors),
            tuple(
                sample
                for number in sorted(self._telemetry_samples)
                for sample in self._telemetry_samples[number]
            ),
            tuple(
                sample
                for number in sorted(self._position_samples)
                for sample in self._position_samples[number]
            ),
            self._scheduled_total_laps,
            tuple(provisional_sectors),
            tuple(live_timing),
            self._race_control_history.snapshot(),
        )


def _decode_compressed_payload(payload: object) -> Mapping:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, str):
        return {}
    try:
        decoded = zlib.decompress(base64.b64decode(payload), -zlib.MAX_WBITS)
        value = json.loads(decoded.decode("utf-8-sig"))
    except (ValueError, TypeError, zlib.error, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _sample_time_seconds(value: object, message: LiveTimingMessage) -> float | None:
    if value is not None:
        try:
            sample_timestamp = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            if (
                message.session_time_seconds is not None
                and message.source_timestamp_utc is not None
            ):
                return float(message.session_time_seconds) + (
                    sample_timestamp - message.source_timestamp_utc
                ).total_seconds()
            return sample_timestamp.timestamp()
        except (TypeError, ValueError):
            pass
    if message.session_time_seconds is not None:
        return float(message.session_time_seconds)
    if message.source_timestamp_utc is not None:
        return message.source_timestamp_utc.timestamp()
    return None


def _event_time_seconds(
    timestamp: datetime | None, session_time_seconds: float | None
) -> float | None:
    if session_time_seconds is not None:
        try:
            parsed = float(session_time_seconds)
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(parsed):
                return parsed
    return timestamp.timestamp() if timestamp is not None else None


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
