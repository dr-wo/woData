"""Fetch, build, and optionally play completed-session replay data.

Edit ``SCRIPT_CONFIG`` and run:

    python -m wodata.script.fetch_replay_data
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from wodata import get_data_root
from wodata.live import LiveConfig, LiveSessionStore, LiveTimingController
from wodata.live.recorder import live_recording_path
from wodata.live.replay import (
    OPTIONAL_TOPICS,
    REQUIRED_TOPICS,
    TELEMETRY_TOPICS,
    ArchiveSession,
    ReplayFaultConfig,
    ReplayPlayer,
    archive_path,
    build_replay,
    download_archive,
    resolve_archive_session,
)

FETCH_REPLAY_DATA = "fetch_replay_data"
ARCHIVE_REPLAY = "archive_replay"  # Backwards-compatible workflow name.
LIVE_RECORD = "live_record"
WORKFLOWS = (FETCH_REPLAY_DATA, ARCHIVE_REPLAY, LIVE_RECORD)
PROJECT_DATA_ROOT = Path(__file__).resolve().parents[3]


SCRIPT_CONFIG = {
    # Choose "fetch_replay_data" for a completed session or "live_record" for SignalR.
    "workflow": FETCH_REPLAY_DATA,
    "year": 2026,
    # Round number is the normal identifier; an event name is not required.
    "round_number": 14,
    # Optional fallback when round_number=None, e.g. "Hungary".
    "event": None,
    "session": "Q",
    # None uses WODATA_ROOT, WOSTRATEGY_DATA_ROOT, then this woData project root.
    "data_root": None,
    # Archive stages. Any combination is allowed.
    "download": True,
    "build": True,
    "play": False,
    # FastF1 returns the two large .z topics as compressed archive records;
    # woData decodes and stores them as ordinary replay events.
    "topics": (*REQUIRED_TOPICS, *OPTIONAL_TOPICS, *TELEMETRY_TOPICS),
    "mandatory_topics": TELEMETRY_TOPICS,
    # Add newly requested topics to an archive without re-fetching existing files.
    "update_existing": True,
    "overwrite": False,
    "strict_build": True,
    # Replay controls: 1=real time, 20=20x, 0=maximum speed.
    "speed": 0,
    "start_time_seconds": None,
    "end_time_seconds": None,
    # Deterministic replay fault controls.
    "excluded_topics": (),
    "topic_drop_after": {},
    "topic_delay_seconds": {},
    "duplicate_rate": 0.0,
    "drop_rate": 0.0,
    "random_seed": 42,
    # Live recording controls, used only when workflow="live_record".
    "live_timeout": 60,
    "live_no_auth": False,
    "poll_interval_seconds": 0.1,
}


@dataclass(frozen=True)
class ArchiveWorkflowResult:
    archive_dir: Path
    replay_path: Path | None
    downloaded: bool
    reused_download: bool
    built: bool
    played: bool
    messages_emitted: int
    completed_laps: int


def default_archive_output_root(data_root: str | Path | None = None) -> Path:
    """Return the designated archive root below the shared woData data root."""
    return _script_data_root(data_root) / "wodata" / "live_archive"


def run_fetch_replay_data(config: Mapping[str, object]) -> ArchiveWorkflowResult:
    """Download, build, and/or play one archive according to a config dictionary."""
    year = int(config["year"])
    event = _event_identifier(config.get("event"))
    round_number = config.get("round_number")
    session_identifier = str(config["session"])
    should_download = bool(config.get("download", True))
    should_build = bool(config.get("build", True))
    should_play = bool(config.get("play", True))
    if not (should_download or should_build or should_play):
        raise ValueError("At least one of download, build, or play must be enabled")

    output_root = default_archive_output_root(config.get("data_root"))
    if should_download or round_number is None:
        session_event = int(round_number) if round_number is not None else event
        if session_event is None:
            raise ValueError("Set round_number, or provide event as a fallback")
        archive_session = resolve_archive_session(year, session_event, session_identifier)
    else:
        archive_session = ArchiveSession(
            year=year,
            round_number=int(round_number),
            event_name=str(config.get("event") or f"Round {round_number}"),
            session_name=session_identifier,
            session_code=_configured_session_code(session_identifier),
            api_path="",
        )
    destination = archive_path(output_root, archive_session)
    print(f"Archive directory: {destination}")

    reused = False
    if should_download:
        download_result = download_archive(
            archive_session,
            output_root=output_root,
            topics=tuple(
                config.get(
                    "topics",
                    (*REQUIRED_TOPICS, *OPTIONAL_TOPICS, *TELEMETRY_TOPICS),
                )
            ),
            mandatory_topics=tuple(config.get("mandatory_topics", ())),
            overwrite=bool(config.get("overwrite", False)),
            update_existing=bool(config.get("update_existing", True)),
        )
        destination = download_result.archive_dir
        reused = download_result.reused
        print(
            f"Archive topics: {len(download_result.available_topics)} available, "
            f"{len(download_result.missing_topics)} missing"
        )

    replay_path = destination / "replay" / "merged_replay_v1.jsonl"
    if should_build:
        build_result = build_replay(
            destination,
            strict=bool(config.get("strict_build", True)),
        )
        replay_path = build_result.replay_path
        print(f"Replay file: {replay_path}")
        print(f"Replay events: {build_result.event_count}; sha256: {build_result.sha256}")
    elif should_play and not replay_path.exists():
        raise FileNotFoundError(
            f"Replay file does not exist: {replay_path}. Enable build or check the config."
        )

    messages_emitted = 0
    completed_laps = 0
    if should_play:
        faults = ReplayFaultConfig(
            excluded_topics=frozenset(config.get("excluded_topics", ())),
            topic_drop_after=dict(config.get("topic_drop_after", {})),
            topic_delay_seconds=dict(config.get("topic_delay_seconds", {})),
            duplicate_rate=float(config.get("duplicate_rate", 0.0)),
            drop_rate=float(config.get("drop_rate", 0.0)),
            random_seed=int(config.get("random_seed", 42)),
        )
        store = LiveSessionStore()
        play_result = ReplayPlayer(
            replay_path,
            speed=float(config.get("speed", 1.0)),
            fault_config=faults,
        ).play(
            store.apply,
            start_time_seconds=_optional_float(config.get("start_time_seconds")),
            end_time_seconds=_optional_float(config.get("end_time_seconds")),
        )
        messages_emitted = play_result.messages_emitted
        completed_laps = len(store.snapshot().completed_laps)
        summary_path = replay_path.with_name("last_playback_summary.json")
        summary_path.write_text(
            json.dumps(
                {
                    "status": play_result.status,
                    "messages_emitted": messages_emitted,
                    "completed_laps": completed_laps,
                    "topic_counts": play_result.topic_counts,
                    "fault_stats": asdict(play_result.fault_stats),
                    "last_session_time_seconds": play_result.last_session_time_seconds,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Playback status: {play_result.status}; messages: {messages_emitted}")
        print(f"Playback summary: {summary_path}")

    return ArchiveWorkflowResult(
        archive_dir=destination,
        replay_path=replay_path if should_build or should_play else None,
        downloaded=should_download,
        reused_download=reused,
        built=should_build,
        played=should_play,
        messages_emitted=messages_emitted,
        completed_laps=completed_laps,
    )


def run_live_record(config: Mapping[str, object]) -> Path:
    """Record a live SignalR session until it ends or the user presses Ctrl-C."""
    round_number = config.get("round_number")
    if round_number is None:
        raise ValueError("round_number is required for live_record")
    year = int(config["year"])
    session = str(config["session"])
    data_root = _script_data_root(config.get("data_root"))
    configured_path = live_recording_path(
        year=year,
        round_number=int(round_number),
        session=session,
        data_root=data_root,
    )
    controller = LiveTimingController()
    controller.start_live(
        LiveConfig(
            year=year,
            round_number=int(round_number),
            session=session,
            data_root=data_root,
            timeout=int(config.get("live_timeout", 60)),
            no_auth=bool(config.get("live_no_auth", False)),
        )
    )
    path = controller.recording_path or configured_path
    print(f"Recording live timing to: {path}")
    messages_received = 0
    try:
        while controller.is_running:
            messages_received += controller.drain().messages_processed
            time.sleep(float(config.get("poll_interval_seconds", 0.1)))
        messages_received += controller.drain().messages_processed
    except KeyboardInterrupt:
        print("Stopping live recording...")
    finally:
        controller.stop()
    if messages_received:
        print(f"Live recording saved to: {path} ({messages_received} messages)")
    else:
        print("No live messages received; no new recording data was written.")
    return path


def run(config: Mapping[str, object] = SCRIPT_CONFIG):
    workflow = str(config.get("workflow", FETCH_REPLAY_DATA))
    if workflow in (FETCH_REPLAY_DATA, ARCHIVE_REPLAY):
        return run_fetch_replay_data(config)
    if workflow == LIVE_RECORD:
        return run_live_record(config)
    raise ValueError(f"Unknown workflow {workflow!r}; expected one of {WORKFLOWS}")


def main() -> None:
    args = _parse_args()
    config = dict(SCRIPT_CONFIG)
    config.update(
        workflow=args.workflow,
        year=args.year,
        event=args.event,
        round_number=args.round_number,
        session=args.session,
        data_root=args.data_root,
        download=args.download,
        build=args.build,
        play=args.play,
        overwrite=args.overwrite,
        update_existing=args.update_existing,
        speed=args.speed,
    )
    run(config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch, build, and optionally play completed-session replay data."
    )
    parser.add_argument("--workflow", choices=WORKFLOWS, default=SCRIPT_CONFIG["workflow"])
    parser.add_argument("--year", type=int, default=SCRIPT_CONFIG["year"])
    parser.add_argument("--event", default=SCRIPT_CONFIG["event"])
    parser.add_argument("--round-number", type=int, default=SCRIPT_CONFIG["round_number"])
    parser.add_argument("--session", default=SCRIPT_CONFIG["session"])
    parser.add_argument("--data-root", type=Path, default=SCRIPT_CONFIG["data_root"])
    parser.add_argument(
        "--download", action=argparse.BooleanOptionalAction, default=SCRIPT_CONFIG["download"]
    )
    parser.add_argument(
        "--build", action=argparse.BooleanOptionalAction, default=SCRIPT_CONFIG["build"]
    )
    parser.add_argument(
        "--play", action=argparse.BooleanOptionalAction, default=SCRIPT_CONFIG["play"]
    )
    parser.add_argument("--overwrite", action="store_true", default=SCRIPT_CONFIG["overwrite"])
    parser.add_argument(
        "--update-existing",
        action=argparse.BooleanOptionalAction,
        default=SCRIPT_CONFIG["update_existing"],
    )
    parser.add_argument("--speed", type=float, default=SCRIPT_CONFIG["speed"])
    return parser.parse_args()


def _event_identifier(value: object) -> int | str | None:
    if value is None:
        return None
    text = str(value)
    return int(text) if text.isdigit() else text


def _configured_session_code(value: str) -> str:
    aliases = {
        "PRACTICE 1": "FP1",
        "PRACTICE 2": "FP2",
        "PRACTICE 3": "FP3",
        "QUALIFYING": "Q",
        "SPRINT QUALIFYING": "SQ",
        "SPRINT SHOOTOUT": "SS",
        "SPRINT": "S",
        "RACE": "R",
    }
    normalized = value.strip().upper()
    return aliases.get(normalized, normalized)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _script_data_root(value: object) -> Path:
    if value is not None:
        return get_data_root(str(value))
    if os.environ.get("WODATA_ROOT") or os.environ.get("WOSTRATEGY_DATA_ROOT"):
        return get_data_root()
    return PROJECT_DATA_ROOT


# Compatibility for callers that used the old function name.
run_archive_replay = run_fetch_replay_data


if __name__ == "__main__":
    main()
