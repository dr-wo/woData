from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


REPLAY_SCHEMA_VERSION = 1
REPLAY_SOURCE = "f1_archive_replay"


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO timestamp as UTC; return None for session-relative timestamps."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def session_time_seconds(value: object) -> float:
    """Convert F1 archive timestamps (HH:MM:SS.mmm or numeric) to seconds."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid session timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        parts = text.split(":")
        if not text or len(parts) > 4:
            raise ValueError(f"invalid session timestamp: {value!r}")
        try:
            if len(parts) == 4:
                # FastF1 archive streams use HH:MM:SS:mmm.
                hours, minutes, whole_seconds, milliseconds = parts
                seconds = (
                    float(hours) * 3600.0
                    + float(minutes) * 60.0
                    + float(whole_seconds)
                    + float(milliseconds) / 1000.0
                )
            else:
                seconds = 0.0
                for part in parts:
                    seconds = seconds * 60.0 + float(part)
        except ValueError as exc:
            raise ValueError(f"invalid session timestamp: {value!r}") from exc
    if seconds < 0 or seconds == float("inf") or seconds != seconds:
        raise ValueError(f"invalid session timestamp: {value!r}")
    return seconds


@dataclass(frozen=True)
class ArchiveRecord:
    topic: str
    session_time_seconds: float
    payload: dict[str, Any]
    source_order: int
    original_timestamp: str | None = None

    def __post_init__(self) -> None:
        if not self.topic:
            raise ValueError("archive record topic cannot be empty")
        if not isinstance(self.payload, dict):
            raise TypeError("archive record payload must be a dictionary")
        session_time_seconds(self.session_time_seconds)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "topic": self.topic,
            "session_time_seconds": self.session_time_seconds,
            "payload": self.payload,
            "source_order": self.source_order,
        }
        if self.original_timestamp is not None:
            result["original_timestamp"] = self.original_timestamp
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, topic: str | None = None) -> "ArchiveRecord":
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        return cls(
            topic=str(value.get("topic") or topic or ""),
            session_time_seconds=session_time_seconds(value.get("session_time_seconds")),
            payload=payload,
            source_order=int(value.get("source_order", 0)),
            original_timestamp=(
                str(value["original_timestamp"]) if value.get("original_timestamp") else None
            ),
        )


@dataclass(frozen=True)
class ReplayEvent:
    topic: str
    payload: Any
    session_time_seconds: float
    sequence: int
    schema_version: int = REPLAY_SCHEMA_VERSION
    source: str = REPLAY_SOURCE
    archive_topic_index: int | None = None
    original_timestamp: str | None = None
    message_session_time_seconds: float | None = None
    preserve_message_session_time: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReplayEvent":
        required = ("topic", "payload", "session_time_seconds", "sequence")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"missing replay fields: {', '.join(missing)}")
        payload = value["payload"]
        version = int(value.get("schema_version", REPLAY_SCHEMA_VERSION))
        if version != REPLAY_SCHEMA_VERSION:
            raise ValueError(f"unsupported replay schema version: {version}")
        return cls(
            topic=str(value["topic"]),
            payload=payload,
            session_time_seconds=session_time_seconds(value["session_time_seconds"]),
            sequence=int(value["sequence"]),
            schema_version=version,
            source=str(value.get("source", REPLAY_SOURCE)),
            archive_topic_index=(
                int(value["archive_topic_index"])
                if value.get("archive_topic_index") is not None
                else None
            ),
            original_timestamp=(
                str(value["original_timestamp"]) if value.get("original_timestamp") else None
            ),
            message_session_time_seconds=(
                session_time_seconds(value["message_session_time_seconds"])
                if value.get("message_session_time_seconds") is not None else None
            ),
            preserve_message_session_time=bool(value.get("preserve_message_session_time", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "topic": self.topic,
            "session_time_seconds": self.session_time_seconds,
            "sequence": self.sequence,
            "payload": self.payload,
            "source": self.source,
        }
        if self.archive_topic_index is not None:
            result["archive_topic_index"] = self.archive_topic_index
        if self.original_timestamp is not None:
            result["original_timestamp"] = self.original_timestamp
        if self.message_session_time_seconds is not None:
            result["message_session_time_seconds"] = self.message_session_time_seconds
        if self.preserve_message_session_time:
            result["preserve_message_session_time"] = True
        return result
