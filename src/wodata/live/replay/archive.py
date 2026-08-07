from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .schema import ArchiveRecord, REPLAY_SCHEMA_VERSION, session_time_seconds


REQUIRED_TOPICS = (
    "SessionInfo", "DriverList", "SessionStatus", "LapCount", "TimingData",
    "TimingAppData", "TrackStatus",
)
OPTIONAL_TOPICS = ("WeatherData", "RaceControlMessages", "Heartbeat")
TELEMETRY_TOPICS = ("CarData.z", "Position.z")
LARGE_TOPICS = frozenset(("CarData.z", "Position.z", "TeamRadio", "TimingStats"))

_FASTF1_PAGE_NAMES = {
    "SessionInfo": "session_info",
    "DriverList": "driver_list",
    "SessionStatus": "session_status",
    "LapCount": "lap_count",
    "TimingData": "timing_data",
    "TimingAppData": "timing_app_data",
    "TrackStatus": "track_status",
    "WeatherData": "weather_data",
    "RaceControlMessages": "race_control_messages",
    "Heartbeat": "heartbeat",
    "CarData.z": "car_data",
    "Position.z": "position",
    "TeamRadio": "team_radio",
    "TimingStats": "timing_stats",
}


class ArchiveClient(Protocol):
    def fetch_topic(self, api_path: str, topic: str) -> list[ArchiveRecord]: ...


class FastF1ArchiveClient:
    """Isolate access to FastF1's low-level archive endpoint."""

    def fetch_topic(self, api_path: str, topic: str) -> list[ArchiveRecord]:
        try:
            from fastf1 import _api
        except ImportError as exc:
            raise RuntimeError(
                "Archive download requires the wodata live extra: pip install 'wodata[live]'"
            ) from exc
        try:
            page_name = _FASTF1_PAGE_NAMES[topic]
        except KeyError as exc:
            raise ValueError(f"unsupported archive topic: {topic}") from exc
        response = _api.fetch_page(api_path, page_name)
        if response is None:
            return []
        if not isinstance(response, list):
            response = [[0.0, response]]
        records = []
        for index, item in enumerate(response):
            if topic in TELEMETRY_TOPICS and isinstance(item, str):
                # FastF1 intentionally leaves the large .z streams compressed
                # in fetch_page; its higher-level parsers decode each record on
                # demand to avoid retaining a second full copy in memory.
                timestamp = item[:12]
                payload = _api.parse(item[12:], zipped=True)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                timestamp, payload = item[0], item[1]
            else:
                raise ValueError(f"malformed FastF1 {topic} archive record {index}")
            if not isinstance(payload, dict):
                raise TypeError(f"FastF1 {topic} payload {index} is not an object")
            records.append(
                ArchiveRecord(
                    topic=topic,
                    session_time_seconds=session_time_seconds(timestamp),
                    payload=payload,
                    source_order=index,
                    original_timestamp=str(timestamp),
                )
            )
        return records


@dataclass(frozen=True)
class ArchiveSession:
    year: int
    round_number: int
    event_name: str
    session_name: str
    session_code: str
    api_path: str


@dataclass(frozen=True)
class ArchiveDownloadResult:
    archive_dir: Path
    available_topics: tuple[str, ...]
    missing_topics: tuple[str, ...]
    reused: bool = False


def resolve_archive_session(year: int, event: str | int, session: str) -> ArchiveSession:
    try:
        import fastf1
    except ImportError as exc:
        raise RuntimeError(
            "Archive discovery requires the wodata live extra: pip install 'wodata[live]'"
        ) from exc
    resolved = fastf1.get_session(year, event, session)
    event_name = str(resolved.event.get("EventName") or resolved.event.get("OfficialEventName"))
    return ArchiveSession(
        year=int(year),
        round_number=int(resolved.event["RoundNumber"]),
        event_name=event_name,
        session_name=str(resolved.name),
        session_code=_session_code(str(session), str(resolved.name)),
        api_path=str(resolved.api_path),
    )


def _session_code(identifier: str, resolved_name: str) -> str:
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
    identifier = identifier.strip().upper()
    return aliases.get(identifier, aliases.get(resolved_name.strip().upper(), identifier))


def archive_path(output_root: str | Path, session: ArchiveSession) -> Path:
    return (
        Path(output_root).expanduser()
        / f"schema_v{REPLAY_SCHEMA_VERSION}"
        / f"year={session.year}"
        / f"round={session.round_number}"
        / f"session={session.session_code}"
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, indent=2, sort_keys=True, ensure_ascii=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def download_archive(
    session: ArchiveSession,
    *,
    output_root: str | Path,
    topics: Iterable[str] = (*REQUIRED_TOPICS, *OPTIONAL_TOPICS, *TELEMETRY_TOPICS),
    mandatory_topics: Iterable[str] = (),
    overwrite: bool = False,
    update_existing: bool = False,
    client: ArchiveClient | None = None,
) -> ArchiveDownloadResult:
    requested = tuple(dict.fromkeys(topics))
    unsupported = [topic for topic in requested if topic not in _FASTF1_PAGE_NAMES]
    if unsupported:
        raise ValueError(f"unsupported archive topics: {', '.join(unsupported)}")
    destination = archive_path(output_root, session)
    manifest_path = destination / "manifest.json"
    existing: dict[str, object] | None = None
    if manifest_path.exists() and not overwrite:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("requested_topics") == list(requested):
            mandatory_missing = sorted(
                set(mandatory_topics).intersection(existing.get("missing_topics", ()))
            )
            if mandatory_missing:
                raise RuntimeError(
                    "mandatory archive topics unavailable: " + ", ".join(mandatory_missing)
                )
            return ArchiveDownloadResult(
                destination,
                tuple(existing.get("available_topics", ())),
                tuple(existing.get("missing_topics", ())),
                reused=True,
            )
        if not update_existing:
            raise FileExistsError(
                f"archive already exists with different topics: {destination}; "
                "use update_existing=True or overwrite=True"
            )
    client = client or FastF1ArchiveClient()
    existing_available = set(existing.get("available_topics", ())) if existing else set()
    existing_requested = set(existing.get("requested_topics", ())) if existing else set()
    existing_missing = set(existing.get("missing_topics", ())) if existing else set()
    available: list[str] = [
        topic
        for topic in requested
        if topic in existing_available and (destination / "raw" / f"{topic}.json").is_file()
    ]
    # Updating adds newly requested topics. Previously attempted missing topics
    # are preserved without another network request; overwrite=True explicitly
    # requests a full retry.
    missing: list[str] = [
        topic
        for topic in requested
        if topic in existing_requested and topic in existing_missing
    ]
    existing_counts = existing.get("topic_counts", {}) if existing else {}
    counts: dict[str, int] = {
        topic: int(existing_counts.get(topic, 0))
        for topic in available
    }
    for topic in requested:
        if topic in available or topic in missing:
            continue
        records = client.fetch_topic(session.api_path, topic)
        if not records:
            missing.append(topic)
            counts[topic] = 0
            continue
        _atomic_json(
            destination / "raw" / f"{topic}.json",
            {"schema_version": REPLAY_SCHEMA_VERSION, "topic": topic,
             "records": [record.to_dict() for record in records]},
        )
        available.append(topic)
        counts[topic] = len(records)
    available_set = set(available)
    missing_set = set(missing)
    available = [topic for topic in requested if topic in available_set]
    missing = [topic for topic in requested if topic in missing_set]
    mandatory_missing = sorted(set(mandatory_topics).intersection(missing))
    manifest = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "year": session.year,
        "round": session.round_number,
        "event_name": session.event_name,
        "session_name": session.session_name,
        "session_code": session.session_code,
        "api_path": session.api_path,
        "created_utc": (
            existing.get("created_utc")
            if existing and existing.get("created_utc")
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fastf1_version": _fastf1_version(),
        "requested_topics": list(requested),
        "available_topics": available,
        "missing_topics": missing,
        "topic_counts": counts,
        "source": "f1_archive",
    }
    _atomic_json(manifest_path, manifest)
    if mandatory_missing:
        raise RuntimeError(f"mandatory archive topics unavailable: {', '.join(mandatory_missing)}")
    return ArchiveDownloadResult(destination, tuple(available), tuple(missing))


def _fastf1_version() -> str | None:
    try:
        import fastf1
    except ImportError:
        return None
    return str(getattr(fastf1, "__version__", "unknown"))
