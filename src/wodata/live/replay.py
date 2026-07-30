from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from .models import LiveConnectionStatus, LiveTimingMessage


def parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


class ReplayLiveTimingSource:
    def __init__(self, path: str | Path, speed: float = 0.0, logger=None) -> None:
        if speed < 0:
            raise ValueError("Replay speed cannot be negative")
        self.path = Path(path)
        self.speed = float(speed)
        self.logger = logger or logging.getLogger(__name__)
        self.invalid_record_count = 0
        self._stop_event = Event()

    def run(self, emit_message, emit_status) -> None:
        self._stop_event.clear()
        emit_status(LiveConnectionStatus("connected", f"Replaying {self.path.name}"))
        previous_timestamp = None
        received = False
        try:
            with self.path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if self._stop_event.is_set():
                        break
                    try:
                        topic, payload, timestamp = parse_recorded_line(line.strip())
                    except (SyntaxError, ValueError, TypeError):
                        self.invalid_record_count += 1
                        self.logger.warning("Ignoring malformed replay record on line %d", line_number)
                        continue
                    if self.speed > 0 and timestamp is not None and previous_timestamp is not None:
                        delay = max((timestamp - previous_timestamp).total_seconds(), 0.0)
                        if self._stop_event.wait(delay / self.speed):
                            break
                    now = datetime.now(timezone.utc)
                    emit_message(LiveTimingMessage(topic, payload, timestamp, now))
                    if not received:
                        emit_status(LiveConnectionStatus("receiving", "Replay messages received"))
                        received = True
                    if timestamp is not None:
                        previous_timestamp = timestamp
        except Exception as exc:
            emit_status(LiveConnectionStatus("error", f"Replay failed: {exc}"))
            return
        message = "Replay stopped" if self._stop_event.is_set() else "Replay completed"
        emit_status(LiveConnectionStatus("disconnected", message))

    def stop(self) -> None:
        self._stop_event.set()
