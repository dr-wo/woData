from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..reducer import LiveSessionStore
from .archive import OPTIONAL_TOPICS, REQUIRED_TOPICS, download_archive, resolve_archive_session
from .builder import build_replay
from .faults import ReplayFaultConfig
from .player import ReplayPlayer


def _topic_time(value: str) -> tuple[str, float]:
    try:
        topic, seconds = value.rsplit(":", 1)
        return topic, float(seconds)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected TOPIC:SECONDS") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download, build and play F1 archive replays")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="download raw per-topic archive streams")
    download.add_argument("--year", type=int, required=True)
    download.add_argument("--event", required=True)
    download.add_argument("--session", required=True)
    download.add_argument("--output-root", type=Path, default=Path("~/.wodata/live_archive"))
    download.add_argument("--topic", action="append", dest="topics")
    download.add_argument("--mandatory-topic", action="append", default=[])
    download.add_argument("--overwrite", action="store_true")

    build = commands.add_parser("build", help="merge raw topic streams deterministically")
    build.add_argument("--archive", type=Path, required=True)
    build.add_argument("--permissive", action="store_true")

    play = commands.add_parser("play", help="play a merged replay through shared reducers")
    play.add_argument("replay", type=Path)
    play.add_argument("--speed", type=float, default=1.0)
    play.add_argument("--start-time", type=float)
    play.add_argument("--end-time", "--stop-at-time", dest="end_time", type=float)
    play.add_argument("--exclude-topic", action="append", default=[])
    play.add_argument("--drop-topic-after", action="append", type=_topic_time, default=[])
    play.add_argument("--delay-topic", action="append", type=_topic_time, default=[])
    play.add_argument("--duplicate-rate", type=float, default=0.0)
    play.add_argument("--drop-rate", type=float, default=0.0)
    play.add_argument("--random-seed", type=int, default=42)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        event = int(args.event) if args.event.isdigit() else args.event
        session = resolve_archive_session(args.year, event, args.session)
        result = download_archive(
            session,
            output_root=args.output_root,
            topics=args.topics or (*REQUIRED_TOPICS, *OPTIONAL_TOPICS),
            mandatory_topics=args.mandatory_topic,
            overwrite=args.overwrite,
        )
        print(json.dumps({
            "archive": str(result.archive_dir),
            "available_topics": result.available_topics,
            "missing_topics": result.missing_topics,
            "reused": result.reused,
        }, indent=2))
        return 0
    if args.command == "build":
        result = build_replay(args.archive, strict=not args.permissive)
        print(json.dumps({
            "replay": str(result.replay_path),
            "events": result.event_count,
            "sha256": result.sha256,
            "skipped_records": result.skipped_records,
        }, indent=2))
        return 0

    faults = ReplayFaultConfig(
        excluded_topics=frozenset(args.exclude_topic),
        topic_drop_after=dict(args.drop_topic_after),
        topic_delay_seconds=dict(args.delay_topic),
        duplicate_rate=args.duplicate_rate,
        drop_rate=args.drop_rate,
        random_seed=args.random_seed,
    )
    store = LiveSessionStore()
    result = ReplayPlayer(args.replay, speed=args.speed, fault_config=faults).play(
        store.apply,
        start_time_seconds=args.start_time,
        end_time_seconds=args.end_time,
    )
    snapshot = store.snapshot()
    print(json.dumps({
        "status": result.status,
        "messages_emitted": result.messages_emitted,
        "topic_counts": result.topic_counts,
        "completed_laps": len(snapshot.completed_laps),
        "faults": {
            "dropped": result.fault_stats.dropped,
            "delayed": result.fault_stats.delayed,
            "duplicated": result.fault_stats.duplicated,
        },
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
