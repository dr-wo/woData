from __future__ import annotations

import copy
import base64
import math
import json
import zlib
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from .models import (
    CompletedLap,
    CompletedSector,
    LiveDriver,
    LiveSessionSnapshot,
    LiveTimingMessage,
    LiveTelemetrySample,
    LivePositionSample,
)

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


def _sector_time_updates(value: object) -> dict[int, float]:
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
        sector_time = _lap_time_seconds(sector)
        if sector_time is not None:
            sector_times[index] = sector_time
    return sector_times


def _is_older(candidate: datetime | None, current: datetime | None) -> bool:
    return candidate is not None and current is not None and candidate < current


class LiveSessionStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._session_info: dict[str, object] = {}
        self._session_status: str | None = None
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._tyre_stints: dict[str, dict[int, dict]] = {}
        self._positions: dict[str, int] = {}
        self._laps: dict[str, dict[int, dict]] = {}
        self._pending: dict[str, dict] = {}
        self._telemetry_samples: dict[str, list[LiveTelemetrySample]] = {}
        self._position_samples: dict[str, list[LivePositionSample]] = {}
        self._last_message_at: datetime | None = None
        self._sequence = 0
        self.duplicate_completed_lap_count = 0
        self.corrected_completed_lap_count = 0
        self.invalid_lap_time_count = 0

    def apply(self, message: LiveTimingMessage) -> LiveSessionSnapshot:
        self._apply_message(message)
        return self.snapshot()

    def apply_many(self, messages: list[LiveTimingMessage]) -> LiveSessionSnapshot:
        for message in messages:
            self._apply_message(message)
        return self.snapshot()

    def _apply_message(self, message: LiveTimingMessage) -> None:
        self._sequence += 1
        self._last_message_at = message.received_at_utc
        payload = message.payload
        if message.topic == "SessionInfo" and isinstance(payload, Mapping):
            _deep_merge(self._session_info, payload)
        elif message.topic == "SessionStatus" and isinstance(payload, Mapping):
            status = payload.get("Status")
            if status is not None:
                self._session_status = str(status)
        elif message.topic == "DriverList" and isinstance(payload, Mapping):
            self._apply_driver_list(payload, message.source_timestamp_utc)
        elif message.topic == "TimingData" and isinstance(payload, Mapping):
            self._apply_timing_data(
                payload,
                message.source_timestamp_utc,
                message.session_time_seconds,
            )
        elif message.topic == "TimingAppData" and isinstance(payload, Mapping):
            self._apply_timing_app_data(payload, message.source_timestamp_utc)
        elif message.topic == "CarData.z":
            self._apply_car_data(message)
        elif message.topic == "Position.z":
            self._apply_position_data(message)

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
                samples = self._telemetry_samples.setdefault(number, [])
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
            sample_time = _sample_time_seconds(sample.get("Timestamp"), message)
            entries = sample.get("Entries", {})
            if sample_time is None or not isinstance(entries, Mapping):
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
                on_track = None if status is None else str(status).lower() in {"ontrack", "0"}
                self._position_samples.setdefault(str(number), []).append(
                    LivePositionSample(str(number), sample_time, x, y, z, on_track)
                )
                latest = self._position_samples[str(number)]
                cutoff = sample_time - 1200.0
                self._position_samples[str(number)] = [
                    item for item in latest if item.session_time_seconds >= cutoff
                ]

    def _trim_telemetry(self, number: str, current_lap: int) -> None:
        minimum_lap = max(current_lap - 12, 1)
        self._telemetry_samples[number] = [
            sample for sample in self._telemetry_samples[number]
            if sample.lap_number >= minimum_lap
        ]

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
            timing = self._timing.setdefault(number, {})
            _deep_merge(timing, patch)
            position = _as_int(patch.get("Position"))
            if position is not None and position > 0:
                self._positions[number] = position
            self._update_pending_lap(number, patch, timestamp, session_time_seconds)

    def _apply_timing_app_data(self, payload: Mapping, timestamp: datetime | None) -> None:
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
            driver_stints = self._tyre_stints.setdefault(number, {})
            for stint_index, patch in updates.items():
                if not isinstance(patch, Mapping):
                    continue
                parsed_index = _as_int(stint_index)
                if parsed_index is None or parsed_index < 0:
                    continue
                if parsed_index not in driver_stints:
                    completed_lap = max(self._laps.get(number, {}), default=0)
                    driver_stints[parsed_index] = {
                        "fitted_lap": completed_lap + 1,
                        "timestamp": timestamp,
                    }
                _deep_merge(driver_stints[parsed_index], patch)

    def _tyre_assignment(self, number: str, lap: int) -> dict[str, object]:
        candidates = [
            (index, stint)
            for index, stint in self._tyre_stints.get(number, {}).items()
            if int(stint.get("fitted_lap", 1)) <= lap
        ]
        if not candidates:
            return {"compound": None, "tyre_life": None, "stint": None, "fresh_tyre": None}
        stint_index, data = max(candidates, key=lambda item: item[0])
        start_laps = _as_int(data.get("StartLaps")) or 0
        stint_number = stint_index + 1
        completed_on_stint = sum(
            1
            for completed in self._laps.get(number, {}).values()
            if completed.get("stint") == stint_number
        )
        return {
            "compound": str(data["Compound"]).upper() if data.get("Compound") else None,
            "tyre_life": float(start_laps + completed_on_stint + 1),
            "stint": stint_number,
            "fresh_tyre": _as_bool(data.get("New")),
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
                "time": None,
                "time_seq": 0,
                "time_timestamp": None,
                "sectors": None,
                "sector_values": [None, None, None],
                "sectors_seq": 0,
                "sectors_timestamp": None,
                "session_time_seconds": None,
                "committed_seq": 0,
            },
        )
        if "NumberOfLaps" in patch and not _is_older(timestamp, pending["lap_timestamp"]):
            lap = _as_int(patch.get("NumberOfLaps"))
            if lap is not None and lap > 0:
                pending.update(lap=lap, lap_seq=self._sequence, lap_timestamp=timestamp)
        if "LastLapTime" in patch and not _is_older(timestamp, pending["time_timestamp"]):
            lap_time = _lap_time_seconds(patch.get("LastLapTime"))
            if lap_time is None:
                self.invalid_lap_time_count += 1
            else:
                pending.update(time=lap_time, time_seq=self._sequence, time_timestamp=timestamp)
        if "Sectors" in patch and not _is_older(timestamp, pending["sectors_timestamp"]):
            sector_updates = _sector_time_updates(patch.get("Sectors"))
            for index, sector_time in sector_updates.items():
                pending["sector_values"][index] = sector_time
            if all(value is not None for value in pending["sector_values"]):
                pending.update(
                    sectors=tuple(pending["sector_values"]),
                    sectors_seq=self._sequence,
                    sectors_timestamp=timestamp,
                )

        lap = pending["lap"]
        lap_time = pending["time"]
        if lap is None or lap_time is None:
            return
        driver_laps = self._laps.setdefault(number, {})
        latest_lap = max(driver_laps, default=0)
        if lap < latest_lap:
            return
        if lap == latest_lap and pending["time_seq"] > pending["committed_seq"]:
            existing = driver_laps[lap]
            if math.isclose(existing["lap_time_s"], lap_time, abs_tol=1e-9):
                self.duplicate_completed_lap_count += 1
            else:
                existing["lap_time_s"] = lap_time
                if pending["sectors_seq"] > pending["committed_seq"]:
                    existing["sector_times_s"] = pending["sectors"]
                existing["source_timestamp_utc"] = timestamp
                existing["session_time_seconds"] = session_time_seconds
                self.corrected_completed_lap_count += 1
                self._recompute_cumulative(number, lap)
            pending["committed_seq"] = self._sequence
            pending["time_seq"] = 0
            return
        if lap <= latest_lap:
            return
        if not (
            pending["lap_seq"] > pending["committed_seq"]
            and pending["time_seq"] > pending["committed_seq"]
        ):
            return
        driver_laps[lap] = {
            "lap_time_s": lap_time,
            "cumulative_time_s": 0.0,
            "sector_times_s": pending["sectors"],
            "position": self._positions.get(number),
            "source_timestamp_utc": max(
                (value for value in (pending["lap_timestamp"], pending["time_timestamp"]) if value),
                default=None,
            ),
            "session_time_seconds": session_time_seconds,
            **self._tyre_assignment(number, lap),
        }
        self._recompute_cumulative(number, lap)
        pending["committed_seq"] = self._sequence
        pending["lap_seq"] = 0
        pending["time_seq"] = 0
        pending["sectors"] = None
        pending["sector_values"] = [None, None, None]
        pending["sectors_seq"] = 0

    def _recompute_cumulative(self, number: str, start_lap: int) -> None:
        driver_laps = self._laps[number]
        cumulative = 0.0
        expected_lap = None
        has_gap = False
        for lap in sorted(driver_laps):
            if expected_lap is not None and lap != expected_lap:
                has_gap = True
            row = driver_laps[lap]
            row["sector_cumulative_times_s"] = None
            sector_times = row.get("sector_times_s")
            if sector_times is not None and not has_gap:
                sector_cumulative = []
                running = cumulative
                for sector_time in sector_times:
                    running += sector_time
                    sector_cumulative.append(running)
                row["sector_cumulative_times_s"] = tuple(sector_cumulative)
            cumulative += row["lap_time_s"]
            row["cumulative_time_s"] = None if has_gap else cumulative
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
                )
            )
        drivers.sort(key=lambda item: (item.position is None, item.position or 999, item.driver_number))

        completed_laps = []
        completed_sectors = []
        driver_lookup = {driver.driver_number: driver for driver in drivers}
        for number, laps in self._laps.items():
            driver = driver_lookup.get(number)
            code = driver.driver_code if driver else number
            team = driver.team if driver else ""
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
                    )
                )
                sector_times = data.get("sector_times_s")
                sector_cumulative_times = data.get("sector_cumulative_times_s")
                if sector_times is None:
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
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if message.session_time_seconds is not None:
        return float(message.session_time_seconds)
    if message.source_timestamp_utc is not None:
        return message.source_timestamp_utc.timestamp()
    return None


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
