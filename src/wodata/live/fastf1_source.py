from __future__ import annotations

from datetime import datetime, timezone

from .models import LiveConnectionStatus, LiveTimingMessage
from .replay import parse_timestamp

LIVE_PHASE_1_TOPICS = (
    "Heartbeat",
    "DriverList",
    "SessionInfo",
    "SessionStatus",
    "TimingData",
    "TimingAppData",
    "TrackStatus",
    "LapCount",
    "CarData.z",
    "Position.z",
)


class FastF1LiveTimingSource:
    def __init__(self, *, timeout: int = 60, no_auth: bool = False, logger=None) -> None:
        self.timeout = timeout
        self.no_auth = no_auth
        self.logger = logger
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
            topic, payload, source_timestamp = record
            sequence += 1
            emit_message(
                LiveTimingMessage(
                    str(topic),
                    payload,
                    parse_timestamp(source_timestamp),
                    datetime.now(timezone.utc),
                    source="signalr_live",
                    sequence=sequence,
                )
            )
            if not received:
                received = True
                emit_status(LiveConnectionStatus("receiving", "Live timing messages received"))

        class StatusSignalRClient(SignalRClient):
            def _on_connect(inner_self) -> None:
                super()._on_connect()
                emit_status(LiveConnectionStatus("connected", "Connected; waiting for messages"))

        emit_status(LiveConnectionStatus("authenticating", "F1 authentication required"))
        self._client = StatusSignalRClient(
            filename=None,
            timeout=self.timeout,
            logger=self.logger,
            no_auth=self.no_auth,
            topics=LIVE_PHASE_1_TOPICS,
            on_message=on_message,
        )
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
