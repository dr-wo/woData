from __future__ import annotations

from pathlib import Path
from threading import Lock
import json

from wodata import get_cache_path

from .models import LiveTimingMessage


def live_recording_path(
    *,
    year: int,
    round_number: int,
    session: str,
    data_root: Path | None = None,
) -> Path:
    return get_cache_path(
        namespace="wodata",
        dataset="live_timing",
        schema="schema_v1",
        year=year,
        round_number=round_number,
        session=session,
        filename="live_timing_raw.jsonl",
        data_root=data_root,
    )


class LiveTimingRecorder:
    def __init__(self, path: str | Path, filemode: str = "w") -> None:
        self.path = Path(path)
        self.filemode = filemode
        self._file = None
        self._lock = Lock()

    def write(self, message: LiveTimingMessage) -> None:
        timestamp = (
            message.source_timestamp_utc.isoformat().replace("+00:00", "Z")
            if message.source_timestamp_utc is not None
            else ""
        )
        with self._lock:
            if self._file is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self.path.open(self.filemode, encoding="utf-8")
            if self.path.suffix.lower() == ".jsonl":
                received = message.received_at_utc.isoformat().replace("+00:00", "Z")
                self._file.write(
                    json.dumps(
                        {
                            "recording_schema_version": 2,
                            "message_schema_version": message.schema_version,
                            "topic": message.topic,
                            "payload": message.payload,
                            "source_timestamp_utc": timestamp or None,
                            "received_at_utc": received,
                            "source": message.source,
                            "sequence": message.sequence,
                            "session_time_seconds": message.session_time_seconds,
                            "playback_time_seconds": message.playback_time_seconds,
                            "received_at_is_trusted": message.received_at_is_trusted,
                        },
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            else:
                self._file.write(str([message.topic, message.payload, timestamp]) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.flush()
                self._file.close()


def unused_recording_path(path: str | Path) -> Path:
    """Return a recording path that will not replace an existing non-empty capture."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists() or candidate.stat().st_size == 0:
            return candidate
        index += 1


class FeedIdentityRecorder(LiveTimingRecorder):
    """Qualifying capture with exclusive filenames and feed-identity provenance.

    A capture starts provisionally. Once complete feed identity arrives, the
    same file is linked into the authoritative directory without replacement.
    The source is removed only after the destination exists.
    """

    def __init__(self, *, year, round_number, session, data_root=None, logger):
        from uuid import uuid4

        provisional = live_recording_path(
            year=year, round_number=round_number, session=session, data_root=data_root
        )
        self.capture_id = uuid4().hex
        path = provisional.with_name(f"live_timing_{self.capture_id}.jsonl")
        super().__init__(path, filemode="x")
        self.data_root = data_root
        self.logger = logger
        self._session_info = {}
        self.provisional_identity = {"year": year, "round_number": round_number, "session": session}
        self.validated_identity = None
        self.identity_mismatch = False

    def write(self, message):
        from dataclasses import asdict
        from collections.abc import Mapping
        from .session_identity import parse_session_identity
        from .reducer import _deep_merge

        super().write(message)
        if message.topic == "SessionInfo" and isinstance(message.payload, Mapping):
            _deep_merge(self._session_info, message.payload)
            identity = parse_session_identity(self._session_info)
            sessions = {
                "QUALIFYING": "Q", "Q": "Q", "SPRINT QUALIFYING": "SQ",
                "SPRINT SHOOTOUT": "SQ", "PRACTICE 1": "FP1",
                "PRACTICE 2": "FP2", "PRACTICE 3": "FP3", "RACE": "R",
                "SPRINT": "S",
            }
            session = sessions.get(identity.session_name.upper())
            if identity.year and identity.round_number and identity.event_name and session:
                validated = {**asdict(identity), "session": session}
                if self.validated_identity and validated != self.validated_identity:
                    self.logger.error("Capture %s received conflicting validated identity: %s", self.capture_id, validated)
                    raise ValueError("Live capture changed validated event/session identity; reconnect for a new capture")
                if self.validated_identity is None:
                    destination = live_recording_path(
                        year=identity.year, round_number=identity.round_number,
                        session=session, data_root=self.data_root,
                    ).with_name(self.path.name)
                    with self._lock:
                        if destination != self.path:
                            self._relocate_exclusively(destination)
                        self.validated_identity = validated
                        self.identity_mismatch = any(
                            self.provisional_identity[key] != value for key, value in {
                                "year": identity.year, "round_number": identity.round_number,
                                "session": session,
                            }.items()
                        )
                        if self.identity_mismatch:
                            self.logger.warning("Capture identity corrected: provisional=%s validated=%s path=%s", self.provisional_identity, validated, self.path)
        if message.topic == "SessionInfo" or not self.path.with_suffix(".identity.json").exists():
            self._write_provenance()

    def _relocate_exclusively(self, destination):
        import os
        from uuid import uuid4

        destination.parent.mkdir(parents=True, exist_ok=True)
        self._file.flush()
        while True:
            try:
                os.link(self.path, destination)
                break
            except FileExistsError:
                destination = destination.with_name(f"live_timing_{uuid4().hex}.jsonl")
        old_path = self.path
        self.path = destination
        old_path.unlink()
        old_path.with_suffix(".identity.json").unlink(missing_ok=True)

    def _write_provenance(self):
        # Unique capture path; replace only this capture's own sidecar atomically.
        import os
        path = self.path.with_suffix(".identity.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "capture_id": self.capture_id,
            "provisional_identity": self.provisional_identity,
            "validated_identity": self.validated_identity,
            "identity_mismatch": self.identity_mismatch,
            "identity_status": "validated" if self.validated_identity else "provisional_unresolved",
            "recording_path": str(self.path),
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
