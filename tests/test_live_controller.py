from pathlib import Path
import time

from wodata.live import LiveTimingController, ReplayConfig


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
