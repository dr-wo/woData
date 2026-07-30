from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .schema import ArchiveRecord, REPLAY_SCHEMA_VERSION, ReplayEvent


DEFAULT_TOPIC_PRIORITY = (
    "SessionInfo", "DriverList", "SessionStatus", "TrackStatus", "LapCount",
    "TimingAppData", "TimingData", "RaceControlMessages", "WeatherData", "Heartbeat",
)
BUILDER_VERSION = "1.0"


@dataclass(frozen=True)
class ReplayBuildResult:
    replay_path: Path
    summary_path: Path
    event_count: int
    sha256: str
    skipped_records: int = 0


def merge_records(
    records: Iterable[ArchiveRecord], topic_priority: Iterable[str] = DEFAULT_TOPIC_PRIORITY
) -> list[ReplayEvent]:
    priorities = {topic: index for index, topic in enumerate(topic_priority)}
    ordered = sorted(
        records,
        key=lambda record: (
            record.session_time_seconds,
            priorities.get(record.topic, len(priorities)),
            record.topic,
            record.source_order,
        ),
    )
    return [
        ReplayEvent(
            topic=record.topic,
            payload=record.payload,
            session_time_seconds=record.session_time_seconds,
            sequence=index,
            archive_topic_index=record.source_order,
            original_timestamp=record.original_timestamp,
        )
        for index, record in enumerate(ordered, 1)
    ]


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_replay(
    archive_dir: str | Path,
    *,
    strict: bool = True,
    topic_priority: Iterable[str] = DEFAULT_TOPIC_PRIORITY,
) -> ReplayBuildResult:
    archive_dir = Path(archive_dir)
    manifest_path = archive_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"archive manifest not found: {manifest_path}") from exc
    records: list[ArchiveRecord] = []
    skipped = 0
    for topic in manifest.get("available_topics", []):
        raw_path = archive_dir / "raw" / f"{topic}.json"
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            values = raw.get("records", raw) if isinstance(raw, dict) else raw
            if not isinstance(values, list):
                raise TypeError("raw topic content must be a record list")
            for index, value in enumerate(values):
                try:
                    if not isinstance(value, dict):
                        raise TypeError("record must be an object")
                    records.append(ArchiveRecord.from_dict(value, topic=topic))
                except (TypeError, ValueError, KeyError) as exc:
                    if strict:
                        raise ValueError(
                            f"malformed {topic} record {index} in {raw_path}: {exc}"
                        ) from exc
                    skipped += 1
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            if strict:
                raise ValueError(
                    f"cannot read archive topic {topic} from {raw_path}: {exc}"
                ) from exc
            skipped += 1
    events = merge_records(records, topic_priority)
    content = b"".join(
        (
            json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        for event in events
    )
    digest = hashlib.sha256(content).hexdigest()
    replay_path = archive_dir / "replay" / f"merged_replay_v{REPLAY_SCHEMA_VERSION}.jsonl"
    _atomic_write(replay_path, content)
    counts = Counter(event.topic for event in events)
    summary = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "event_count": len(events),
        "first_session_time_seconds": events[0].session_time_seconds if events else None,
        "last_session_time_seconds": events[-1].session_time_seconds if events else None,
        "topic_counts": dict(sorted(counts.items())),
        "ordering_policy": "session_time_topic_priority_source_order",
        "topic_priority": list(topic_priority),
        "builder_version": BUILDER_VERSION,
        "skipped_records": skipped,
        "sha256": digest,
    }
    summary_path = replay_path.with_name("build_summary_v1.json")
    _atomic_write(summary_path, (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode())
    manifest["replay"] = {"path": str(replay_path.relative_to(archive_dir)), **summary}
    _atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return ReplayBuildResult(replay_path, summary_path, len(events), digest, skipped)
