from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping

from .models import (
    CompletedLap,
    CompletedSector,
    LiveDriver,
    LiveSessionSnapshot,
    LiveTimingMessage,
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


def _sector_times_seconds(value: object) -> tuple[float, float, float] | None:
    if not isinstance(value, list) or len(value) < 3:
        return None
    sector_times = []
    for sector in value[:3]:
        sector_time = _lap_time_seconds(sector)
        if sector_time is None:
            return None
        sector_times.append(sector_time)
    return tuple(sector_times)


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
        self._positions: dict[str, int] = {}
        self._laps: dict[str, dict[int, dict]] = {}
        self._pending: dict[str, dict] = {}
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
            self._apply_timing_data(payload, message.source_timestamp_utc)

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

    def _apply_timing_data(self, payload: Mapping, timestamp: datetime | None) -> None:
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
            self._update_pending_lap(number, patch, timestamp)

    def _update_pending_lap(
        self, number: str, patch: Mapping, timestamp: datetime | None
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
                "sectors_seq": 0,
                "sectors_timestamp": None,
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
            sector_times = _sector_times_seconds(patch.get("Sectors"))
            if sector_times is not None:
                pending.update(
                    sectors=sector_times,
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
        }
        self._recompute_cumulative(number, lap)
        pending["committed_seq"] = self._sequence
        pending["lap_seq"] = 0
        pending["time_seq"] = 0
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
        )
