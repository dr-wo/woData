from __future__ import annotations

import logging
import queue
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .fastf1_source import FastF1LiveTimingSource
from .models import LiveConnectionStatus, LiveSessionSnapshot, LiveTimingMessage
from .recorder import LiveTimingRecorder, live_recording_path
from .reducer import LiveSessionStore
from .replay import ReplayLiveTimingSource


@dataclass(frozen=True)
class LiveConfig:
    year: int
    round_number: int
    session: str
    data_root: Path | None = None
    timeout: int = 60
    no_auth: bool = False


@dataclass(frozen=True)
class ReplayConfig:
    path: Path
    speed: float = 0.0


@dataclass(frozen=True)
class DrainResult:
    snapshot: LiveSessionSnapshot
    statuses: tuple[LiveConnectionStatus, ...]
    messages_processed: int
    changed: bool


class LiveTimingController:
    def __init__(self, *, logger=None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.store = LiveSessionStore()
        self._messages: queue.Queue[LiveTimingMessage] = queue.Queue()
        self._statuses: queue.Queue[LiveConnectionStatus] = queue.Queue()
        self._source = None
        self._thread: threading.Thread | None = None
        self._recorder: LiveTimingRecorder | None = None
        self._snapshot = self.store.snapshot()
        self.message_counts: Counter[str] = Counter()

    def start_live(self, config: LiveConfig) -> None:
        path = live_recording_path(
            year=config.year,
            round_number=config.round_number,
            session=config.session,
            data_root=config.data_root,
        )
        self._recorder = LiveTimingRecorder(path)
        self.logger.info("Recording live timing to %s", path)
        self._start_source(
            FastF1LiveTimingSource(
                timeout=config.timeout, no_auth=config.no_auth, logger=self.logger
            )
        )

    def start_replay(self, config: ReplayConfig) -> None:
        self._start_source(ReplayLiveTimingSource(config.path, speed=config.speed, logger=self.logger))

    def _start_source(self, source) -> None:
        if self.is_running:
            raise RuntimeError("A live timing source is already running")
        self.store.reset()
        self._snapshot = self.store.snapshot()
        self._source = source
        self._thread = threading.Thread(target=self._run_source, daemon=True, name="wodata-live")
        self._thread.start()

    def _run_source(self) -> None:
        try:
            self._source.run(self._enqueue_message, self._statuses.put)
        except Exception as exc:
            self.logger.exception("Fatal live timing source error")
            self._statuses.put(LiveConnectionStatus("error", f"Fatal source error: {exc}"))
        finally:
            if self._recorder is not None:
                self._recorder.close()

    def _enqueue_message(self, message: LiveTimingMessage) -> None:
        if self._recorder is not None:
            self._recorder.write(message)
        self._messages.put(message)

    def drain(self, max_messages: int = 500) -> DrainResult:
        processed = 0
        messages = []
        while processed < max_messages:
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                break
            self.message_counts[message.topic] += 1
            messages.append(message)
            processed += 1
        if messages:
            self._snapshot = self.store.apply_many(messages)
        statuses = []
        while True:
            try:
                statuses.append(self._statuses.get_nowait())
            except queue.Empty:
                break
        return DrainResult(self._snapshot, tuple(statuses), processed, processed > 0)

    def stop(self) -> None:
        if self._source is not None:
            self._source.stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self.logger.warning("Live timing worker did not stop within 5 seconds")
        if self._recorder is not None:
            self._recorder.close()
        self._statuses.put(LiveConnectionStatus("disconnected", "Disconnected by user"))

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
