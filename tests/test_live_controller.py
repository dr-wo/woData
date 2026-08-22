import os
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

from wodata.live import (
    LiveTimingController,
    LiveTimingMessage,
    QualiSessionStore,
    ReplayConfig,
)
from wodata.live.fastf1_source import FastF1LiveTimingSource, LIVE_PHASE_1_TOPICS


FIXTURE = Path(__file__).parent / "fixtures" / "live_phase_1.txt"


def test_controller_replay_drains_on_consumer_thread():
    controller = LiveTimingController()
    controller.start_replay(ReplayConfig(FIXTURE, speed=0))
    deadline = time.monotonic() + 2
    while controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    result = controller.drain()
    assert len(result.snapshot.completed_laps) == 6
    assert result.messages_processed == 13
    controller.stop()


def test_controller_can_replay_twice_without_leaking_queues_or_recorder():
    controller = LiveTimingController()
    for _ in range(2):
        controller.start_replay(ReplayConfig(FIXTURE, speed=0))
        deadline = time.monotonic() + 2
        while controller.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        result = controller.drain()
        assert result.messages_processed == 13
        assert len(result.snapshot.completed_laps) == 6
        assert sum(controller.message_counts.values()) == 13
        controller.stop()


def test_replay_flow_control_is_never_applied_to_live_ingress():
    controller = LiveTimingController()
    calls = []
    controller.set_replay_flow_control(lambda _cancel: calls.append("replay"))
    controller.start_replay(ReplayConfig(FIXTURE, speed=0))
    deadline = time.monotonic() + 2
    while controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(calls) == 13
    controller.stop()

    controller._source = FastF1LiveTimingSource()
    now = datetime.now(timezone.utc)
    controller._enqueue_message(LiveTimingMessage("Heartbeat", {}, now, now))

    assert len(calls) == 13


def test_controller_refreshes_extrapolated_clock_without_new_messages(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("wodata.live.quali_reducer.time.monotonic", lambda: now[0])
    controller = LiveTimingController(store=QualiSessionStore())
    timestamp = datetime.now(timezone.utc)
    controller._enqueue_message(
        LiveTimingMessage(
            "ExtrapolatedClock",
            {"Remaining": "1:00", "Extrapolating": True},
            timestamp,
            timestamp,
        )
    )
    assert controller.drain().snapshot.remaining_time_s == 60

    now[0] += 2
    result = controller.drain()

    assert result.messages_processed == 0
    assert result.snapshot.remaining_time_s == 58
    controller.stop()


def test_fastf1_source_adapts_legacy_client_to_callbacks(monkeypatch):
    clients = []

    class FakeSignalRClient:
        def __init__(
            self,
            filename,
            filemode="w",
            debug=False,
            timeout=60,
            logger=None,
            no_auth=False,
        ):
            self.filename = filename
            self.timeout = timeout
            self.logger = logger
            self.no_auth = no_auth
            self.topics = ["FastF1Default"]
            self._connection = None
            self._output_file = None
            clients.append(self)

        def _on_connect(self):
            pass

        def start(self):
            self._on_connect()
            self._on_message(["TimingData", {"Lines": {}}, "2026-01-01T00:00:00Z"])

    fastf1_module = types.ModuleType("fastf1")
    livetiming_module = types.ModuleType("fastf1.livetiming")
    client_module = types.ModuleType("fastf1.livetiming.client")
    client_module.SignalRClient = FakeSignalRClient
    monkeypatch.setitem(sys.modules, "fastf1", fastf1_module)
    monkeypatch.setitem(sys.modules, "fastf1.livetiming", livetiming_module)
    monkeypatch.setitem(sys.modules, "fastf1.livetiming.client", client_module)

    messages = []
    statuses = []
    FastF1LiveTimingSource(timeout=15, no_auth=True).run(messages.append, statuses.append)

    assert clients[0].filename == os.devnull
    assert clients[0].timeout == 15
    assert clients[0].no_auth is True
    assert clients[0].topics == list(LIVE_PHASE_1_TOPICS)
    assert messages[0].topic == "TimingData"
    assert messages[0].payload == {"Lines": {}}
    assert messages[0].source_timestamp_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert [status.state for status in statuses] == [
        "authenticating",
        "connecting",
        "connected",
        "receiving",
        "disconnected",
    ]
