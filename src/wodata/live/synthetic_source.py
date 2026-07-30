from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from threading import Event

from .models import LiveConnectionStatus, LiveTimingMessage


class SyntheticLiveTimingSource:
    """Small deterministic source for tests that uses the shared live contract."""

    def __init__(self, messages: Iterable[LiveTimingMessage]) -> None:
        self._messages = tuple(messages)
        self._stop_event = Event()

    def run(self, emit_message, emit_status) -> None:
        self._stop_event.clear()
        emit_status(LiveConnectionStatus("connected", "Synthetic source connected"))
        for sequence, message in enumerate(self._messages, 1):
            if self._stop_event.is_set():
                break
            emit_message(replace(message, source="synthetic_test", sequence=sequence))
        status = "stopped" if self._stop_event.is_set() else "completed"
        emit_status(LiveConnectionStatus("disconnected", f"Synthetic source {status}"))

    def stop(self) -> None:
        self._stop_event.set()
