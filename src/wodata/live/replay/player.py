from __future__ import annotations

import ast
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..models import LiveConnectionStatus, LiveTimingMessage
from .faults import FaultStats, ReplayFaultConfig, apply_faults
from .schema import REPLAY_SOURCE, ReplayEvent, parse_timestamp, session_time_seconds


class ReplayConsumerError(RuntimeError):
    def __init__(self, event: ReplayEvent, cause: BaseException) -> None:
        super().__init__(
            f"consumer failed at sequence {event.sequence}, topic {event.topic}: {cause}"
        )
        self.event = event
        self.__cause__ = cause


@dataclass(frozen=True)
class ReplayResult:
    status: Literal["completed", "stopped", "failed"]
    messages_emitted: int
    topic_counts: dict[str, int]
    last_session_time_seconds: float | None
    fault_stats: FaultStats


def parse_recorded_line(line: str) -> tuple[str, object, datetime | None]:
    record = ast.literal_eval(line)
    if not isinstance(record, (list, tuple)) or len(record) != 3:
        raise ValueError("record must contain topic, payload and timestamp")
    topic, payload, timestamp = record
    if not isinstance(topic, str):
        raise ValueError("record topic must be a string")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    return topic, payload, parse_timestamp(timestamp)


def load_replay(path: str | Path, *, permissive: bool = False) -> tuple[list[ReplayEvent], int]:
    path = Path(path)
    events: list[ReplayEvent] = []
    invalid = 0
    is_jsonl = path.suffix.lower() == ".jsonl"
    recording_origin: datetime | None = None
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                if is_jsonl:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise TypeError("JSONL record must be an object")
                    if int(value.get("recording_schema_version", 0)) == 2:
                        received_at = parse_timestamp(value.get("received_at_utc"))
                        source_at = parse_timestamp(value.get("source_timestamp_utc"))
                        clock = received_at or source_at
                        if recording_origin is None and clock is not None:
                            recording_origin = clock
                        recorded_session_time = value.get("session_time_seconds")
                        timeline_seconds = (
                            session_time_seconds(recorded_session_time)
                            if recorded_session_time is not None
                            else (
                                max(0.0, (clock - recording_origin).total_seconds())
                                if clock is not None and recording_origin is not None
                                else float(len(events))
                            )
                        )
                        events.append(
                            ReplayEvent(
                                topic=str(value.get("topic") or ""),
                                payload=value.get("payload"),
                                session_time_seconds=timeline_seconds,
                                sequence=int(value.get("sequence", len(events) + 1)),
                                schema_version=int(value.get("message_schema_version", 1)),
                                source=str(value.get("source") or REPLAY_SOURCE),
                                original_timestamp=(
                                    str(value["source_timestamp_utc"])
                                    if value.get("source_timestamp_utc")
                                    else None
                                ),
                                original_received_at=(
                                    str(value["received_at_utc"])
                                    if value.get("received_at_utc")
                                    else None
                                ),
                                message_session_time_seconds=(
                                    session_time_seconds(recorded_session_time)
                                    if recorded_session_time is not None
                                    else None
                                ),
                                preserve_message_session_time=True,
                            )
                        )
                    else:
                        events.append(ReplayEvent.from_dict(value))
                else:
                    topic, payload, timestamp = parse_recorded_line(line.strip())
                    seconds = timestamp.timestamp() if timestamp is not None else float(len(events))
                    events.append(
                        ReplayEvent(
                            topic,
                            payload,
                            seconds,
                            len(events) + 1,
                            original_timestamp=(timestamp.isoformat() if timestamp else None),
                        )
                    )
            except (SyntaxError, ValueError, TypeError, json.JSONDecodeError) as exc:
                if not permissive:
                    raise ValueError(
                        f"malformed replay record on line {line_number} of {path}: {exc}"
                    ) from exc
                invalid += 1
    return events, invalid


class ReplayPlayer:
    def __init__(
        self,
        path: str | Path,
        *,
        speed: float = 1.0,
        fault_config: ReplayFaultConfig | None = None,
        permissive: bool = False,
        logger=None,
    ) -> None:
        if speed < 0:
            raise ValueError("Replay speed cannot be negative")
        self.path = Path(path)
        self.speed = float(speed)
        self.fault_config = fault_config or ReplayFaultConfig()
        self.permissive = permissive
        self.logger = logger or logging.getLogger(__name__)
        self.invalid_record_count = 0
        self._stop_event = threading.Event()
        self._condition = threading.Condition()
        self._paused = False

    def pause(self) -> None:
        with self._condition:
            self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def stop(self) -> None:
        self._stop_event.set()
        self.resume()

    def play(
        self,
        on_message: Callable[[LiveTimingMessage], None],
        *,
        speed: float | None = None,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> ReplayResult:
        playback_speed = self.speed if speed is None else float(speed)
        if playback_speed < 0:
            raise ValueError("Replay speed cannot be negative")
        if start_time_seconds is not None and end_time_seconds is not None:
            if start_time_seconds > end_time_seconds:
                raise ValueError("start time cannot be after end time")
        self._stop_event.clear()
        events, self.invalid_record_count = load_replay(self.path, permissive=self.permissive)
        events, fault_stats = apply_faults(events, self.fault_config)
        events = [
            event for event in events
            if (start_time_seconds is None or event.session_time_seconds >= start_time_seconds)
            and (end_time_seconds is None or event.session_time_seconds <= end_time_seconds)
        ]
        counts: Counter[str] = Counter()
        previous_time: float | None = None
        last_time: float | None = None
        for event in events:
            if self._is_stopped(stop_event):
                return ReplayResult(
                    "stopped", sum(counts.values()), dict(counts), last_time, fault_stats
                )
            if previous_time is not None and playback_speed > 0:
                delay = max(0.0, event.session_time_seconds - previous_time) / playback_speed
                if not self._wait(delay, stop_event):
                    return ReplayResult(
                        "stopped", sum(counts.values()), dict(counts), last_time, fault_stats
                    )
            elif not self._wait(0.0, stop_event):
                return ReplayResult(
                    "stopped", sum(counts.values()), dict(counts), last_time, fault_stats
                )
            message = LiveTimingMessage(
                topic=event.topic,
                payload=event.payload,
                source_timestamp_utc=parse_timestamp(event.original_timestamp),
                received_at_utc=(
                    parse_timestamp(event.original_received_at)
                    or datetime.now(timezone.utc)
                ),
                schema_version=event.schema_version,
                source=REPLAY_SOURCE,
                sequence=event.sequence,
                session_time_seconds=(
                    event.message_session_time_seconds
                    if event.preserve_message_session_time
                    else event.session_time_seconds
                ),
            )
            try:
                on_message(message)
            except Exception as exc:
                if self.permissive:
                    self.logger.warning("Ignoring %s", ReplayConsumerError(event, exc))
                else:
                    raise ReplayConsumerError(event, exc) from exc
            counts[event.topic] += 1
            previous_time = event.session_time_seconds
            last_time = event.session_time_seconds
        return ReplayResult("completed", sum(counts.values()), dict(counts), last_time, fault_stats)

    def _is_stopped(self, external: threading.Event | None) -> bool:
        return self._stop_event.is_set() or (external is not None and external.is_set())

    def _wait(self, seconds: float, external: threading.Event | None) -> bool:
        remaining = seconds
        while True:
            if self._is_stopped(external):
                return False
            with self._condition:
                while self._paused and not self._is_stopped(external):
                    self._condition.wait(timeout=0.1)
                if self._is_stopped(external):
                    return False
            if remaining <= 0:
                return True
            started = time.monotonic()
            wait_for = min(remaining, 0.1)
            self._stop_event.wait(wait_for)
            elapsed = time.monotonic() - started
            with self._condition:
                if not self._paused:
                    remaining -= elapsed


class ReplayLiveTimingSource:
    """Replay source compatible with the existing live controller contract."""

    def __init__(
        self,
        path: str | Path,
        speed: float = 0.0,
        logger=None,
        *,
        fault_config: ReplayFaultConfig | None = None,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
        permissive: bool = True,
    ) -> None:
        self.path = Path(path)
        self.speed = speed
        self.logger = logger or logging.getLogger(__name__)
        self.start_time_seconds = start_time_seconds
        self.end_time_seconds = end_time_seconds
        self.player = ReplayPlayer(
            path, speed=speed, fault_config=fault_config, permissive=permissive, logger=self.logger
        )

    @property
    def invalid_record_count(self) -> int:
        return self.player.invalid_record_count

    def run(self, emit_message, emit_status) -> None:
        emit_status(LiveConnectionStatus("connected", f"Replaying {self.path.name}"))
        received = False

        def emit(message: LiveTimingMessage) -> None:
            nonlocal received
            emit_message(message)
            if not received:
                emit_status(LiveConnectionStatus("receiving", "Replay messages received"))
                received = True

        try:
            result = self.player.play(
                emit,
                start_time_seconds=self.start_time_seconds,
                end_time_seconds=self.end_time_seconds,
            )
        except Exception as exc:
            emit_status(LiveConnectionStatus("error", f"Replay failed: {exc}"))
            return
        message = "Replay stopped" if result.status == "stopped" else "Replay completed"
        emit_status(LiveConnectionStatus("disconnected", message))

    def pause(self) -> None:
        self.player.pause()

    def resume(self) -> None:
        self.player.resume()

    def stop(self) -> None:
        self.player.stop()


# Clearer source name for new code; the legacy public class remains unchanged.
ArchiveReplaySource = ReplayLiveTimingSource
