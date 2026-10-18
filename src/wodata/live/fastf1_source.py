from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from .models import LiveConnectionStatus, LiveTimingMessage
from .replay import parse_timestamp

LIVE_PHASE_1_TOPICS = (
    "Heartbeat",
    "DriverList",
    "SessionInfo",
    "SessionStatus",
    "ExtrapolatedClock",
    "TimingData",
    "TimingStats",
    "TimingAppData",
    "TrackStatus",
    "LapCount",
    "CarData.z",
    "Position.z",
)


class FastF1LiveTimingSource:
    def __init__(self, *, timeout: int = 0, no_auth: bool = False, logger=None, extra_topics=()) -> None:
        self.timeout = timeout
        self.no_auth = no_auth
        self.logger = logger
        self.extra_topics = tuple(extra_topics)
        self._client = None

    def run(self, emit_message, emit_status) -> None:
        try:
            from fastf1.livetiming.client import SignalRClient
        except ImportError as exc:
            raise RuntimeError(
                "Live timing requires the wodata live extra: pip install 'wodata[live]'"
            ) from exc

        received = False
        sequence = 0

        def on_message(record) -> None:
            nonlocal received, sequence
            receipt_monotonic = time.monotonic()
            receipt_utc = datetime.now(timezone.utc)
            topic, payload, source_timestamp = record
            sequence += 1
            emit_message(
                LiveTimingMessage(
                    str(topic),
                    payload,
                    parse_timestamp(source_timestamp),
                    receipt_utc,
                    source="signalr_live",
                    sequence=sequence,
                    received_at_monotonic_s=receipt_monotonic,
                )
            )
            if not received:
                received = True
                emit_status(LiveConnectionStatus("receiving", "Live timing messages received"))

        class StatusSignalRClient(SignalRClient):
            def __init__(inner_self) -> None:
                # FastF1 owns the SignalR connection but its public client only
                # supports recording to a file.  Keep the file sink harmless and
                # adapt incoming records to wodata's in-process callback.
                super().__init__(
                    filename=os.devnull,
                    timeout=self.timeout,
                    logger=self.logger,
                    no_auth=self.no_auth,
                )
                inner_self.topics = list((*LIVE_PHASE_1_TOPICS, *self.extra_topics))
                inner_self._stop_requested = threading.Event()

            def _on_connect(inner_self) -> None:
                super()._on_connect()
                emit_status(LiveConnectionStatus("connected", "Connected; waiting for messages"))

            def _on_message(inner_self, message) -> None:
                inner_self._t_last_message = time.time()
                result = getattr(message, "result", None)
                if isinstance(result, dict):
                    # Subscribe returns topic snapshots, not timestamped feed
                    # occurrences. Iteration/sequence records delivery order
                    # only; neither it nor local arrival dates these values.
                    for topic, payload in result.items():
                        on_message((topic, payload, None))
                    return
                if isinstance(message, (list, tuple)) and len(message) == 3:
                    on_message(message)
                    return
                inner_self.logger.error(
                    "Unknown live timing message format: %s", type(message).__name__
                )

            def _supervise(inner_self) -> None:
                inner_self._t_last_message = time.time()
                while not inner_self._stop_requested.wait(1.0):
                    if (
                        inner_self.timeout != 0
                        and time.time() - inner_self._t_last_message > inner_self.timeout
                    ):
                        inner_self.logger.warning(
                            "Timeout - received no data for more than %s seconds!",
                            inner_self.timeout,
                        )
                        inner_self.stop()
                        return

            def stop(inner_self) -> None:
                inner_self._stop_requested.set()
                if inner_self._connection is not None:
                    inner_self._connection.stop()
                if inner_self._output_file is not None:
                    inner_self._output_file.close()

        emit_status(LiveConnectionStatus("authenticating", "F1 authentication required"))
        self._client = StatusSignalRClient()
        emit_status(LiveConnectionStatus("connecting", "Connecting to F1 live timing"))
        try:
            self._client.start()
        except Exception as exc:
            emit_status(LiveConnectionStatus("error", f"Live source failed: {exc}"))
            return
        emit_status(LiveConnectionStatus("disconnected", "Live source disconnected"))

    def stop(self) -> None:
        if self._client is not None:
            self._client.stop()
