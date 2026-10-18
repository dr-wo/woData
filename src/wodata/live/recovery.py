from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from .models import LiveTimingMessage
from .replay.player import parse_recorded_line
from .replay.schema import parse_timestamp

RECOVERY_SCHEMA_VERSION = 3
CANONICAL_REPLAY_NAME = "recovered_replay.jsonl"
RECOVERY_MANIFEST_NAME = "recovered_replay_manifest.json"


@dataclass(frozen=True)
class RecordedMessage:
    message: LiveTimingMessage
    path: Path
    source_order: int
    fingerprint: str
    has_envelope: bool = False
    envelope_copy_fingerprint: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    paths: tuple[Path, ...] = ()
    accepted_paths: tuple[Path, ...] = ()
    rejected_paths: tuple[tuple[Path, str], ...] = ()
    input_messages: int = 0
    merged_messages: int = 0


@dataclass(frozen=True)
class CanonicalRecovery:
    path: Path
    manifest_path: Path
    report: RecoveryReport
    reused: bool


def discover_live_recordings(directory: str | Path) -> tuple[Path, ...]:
    root = Path(directory)
    paths = set(root.glob("live_timing_raw*.txt"))
    paths.update(root.glob("live_timing_raw*.jsonl"))
    return tuple(sorted(path for path in paths if path.is_file() and path.stat().st_size))


def recover_replay_directory(
    directory: str | Path, *, year: int, round_number: int, session: str
) -> CanonicalRecovery:
    """Materialize the existing recovery merge as one cacheable replay stream."""
    root = Path(directory)
    paths = discover_live_recordings(root)
    if not paths:
        raise ValueError(f"No raw live timing recordings found in {root}")
    sources = [_source_identity(path) for path in paths]
    canonical = root / CANONICAL_REPLAY_NAME
    manifest_path = root / RECOVERY_MANIFEST_NAME
    expected = {
        "recovery_schema_version": RECOVERY_SCHEMA_VERSION,
        "event": {"year": int(year), "round_number": int(round_number), "session": _session_name(session)},
        "sources": sources,
        "canonical_replay_path": str(canonical.resolve()),
    }
    if canonical.is_file() and manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing = None
        if existing is not None and all(existing.get(key) == value for key, value in expected.items()):
            report = RecoveryReport(paths=paths, accepted_paths=paths)
            return CanonicalRecovery(canonical, manifest_path, report, True)

    accepted: list[list[RecordedMessage]] = []
    rejected: list[tuple[Path, str]] = []
    input_messages = 0
    for path in paths:
        try:
            messages = load_recording(path)
            mismatch = validate_recording_identity(
                messages, year=year, round_number=round_number, session=session
            )
            if mismatch:
                rejected.append((path, mismatch))
                continue
            accepted.append(messages)
            input_messages += len(messages)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            rejected.append((path, str(exc)))
    if not accepted:
        details = "; ".join(f"{path.name}: {reason}" for path, reason in rejected)
        raise ValueError(f"No recordings match the requested event/session. {details}")
    merged = merge_recordings(accepted)
    _write_canonical(canonical, merged)
    report = RecoveryReport(
        paths=paths,
        accepted_paths=tuple(values[0].path for values in accepted),
        rejected_paths=tuple(rejected),
        input_messages=input_messages,
        merged_messages=len(merged),
    )
    manifest = dict(expected)
    manifest.update({
        "accepted_sources": [str(path.resolve()) for path in report.accepted_paths],
        "rejected_sources": [{"path": str(path.resolve()), "reason": reason} for path, reason in rejected],
        "input_messages": input_messages,
        "merged_messages": len(merged),
    })
    _atomic_json(manifest_path, manifest)
    return CanonicalRecovery(canonical, manifest_path, report, False)


def _source_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "sha256": digest.hexdigest()}


def _write_canonical(path: Path, messages: list[RecordedMessage]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        previous = 0.0
        for sequence, item in enumerate(messages, 1):
            message = item.message
            seconds = message.playback_time_seconds
            if seconds is None:
                seconds = message.session_time_seconds
            if seconds is None and message.source_timestamp_utc is not None:
                seconds = message.source_timestamp_utc.timestamp()
            seconds = max(float(seconds if seconds is not None else sequence), previous)
            previous = seconds
            record = {
                "schema_version": 1, "topic": message.topic,
                "payload": message.payload, "session_time_seconds": seconds,
                "sequence": sequence, "source": "recovered_live",
                "message_session_time_seconds": message.session_time_seconds,
                "preserve_message_session_time": True,
                "original_received_at": message.received_at_utc.isoformat(),
                "received_at_is_trusted": (
                    message.received_at_is_trusted if message.received_at_is_trusted is not None
                    else message.source == "signalr_live"
                ),
            }
            if message.source_timestamp_utc is not None:
                record["original_timestamp"] = message.source_timestamp_utc.isoformat()
            target.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
    temporary.replace(path)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_recording(path: str | Path) -> list[RecordedMessage]:
    path = Path(path)
    messages: list[RecordedMessage] = []
    is_jsonl = path.suffix.lower() == ".jsonl"
    with path.open(encoding="utf-8") as source:
        for source_order, line in enumerate(source, 1):
            if not line.strip():
                continue
            envelope_copy_fingerprint = None
            if is_jsonl:
                payload = json.loads(line)
                if not isinstance(payload, Mapping):
                    raise TypeError(f"JSONL record {source_order} in {path} is not an object")
                # Only actual recorded metadata can certify an envelope copy;
                # synthesized receive times/line numbers cannot do so.
                recorded_sequence = payload.get("sequence")
                if (parse_timestamp(payload.get("received_at_utc")) is not None
                        and isinstance(recorded_sequence, int)
                        and not isinstance(recorded_sequence, bool)):
                    envelope_copy_fingerprint = _fingerprint({
                        key: value for key, value in payload.items()
                        if key != "playback_time_seconds"
                    })
                topic = str(payload["topic"])
                value = payload.get("payload")
                source_timestamp = parse_timestamp(payload.get("source_timestamp_utc"))
                received_at = (
                    parse_timestamp(payload.get("received_at_utc"))
                    or source_timestamp
                    or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                )
                message = LiveTimingMessage(
                    topic,
                    value,
                    source_timestamp,
                    received_at,
                    schema_version=int(payload.get("message_schema_version", 1)),
                    source=str(payload.get("source") or "signalr_live"),
                    sequence=int(payload.get("sequence") or source_order),
                    session_time_seconds=_optional_float(payload.get("session_time_seconds")),
                    playback_time_seconds=_optional_float(payload.get("playback_time_seconds")),
                    received_at_is_trusted=(payload.get("received_at_is_trusted")
                                           if parse_timestamp(payload.get("received_at_utc")) is not None
                                           else False),
                )
            else:
                topic, value, source_timestamp = parse_recorded_line(line.strip())
                message = LiveTimingMessage(
                    topic,
                    value,
                    source_timestamp,
                    source_timestamp
                    or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                    source="signalr_live",
                    sequence=source_order,
                    received_at_is_trusted=False,
                )
            messages.append(
                RecordedMessage(
                    message,
                    path,
                    source_order,
                    message_fingerprint(message),
                    has_envelope=is_jsonl,
                    envelope_copy_fingerprint=envelope_copy_fingerprint,
                )
            )
    return messages


def message_fingerprint(message: LiveTimingMessage) -> str:
    """Content in its genuine source coordinate, never its delivery schedule."""
    publication = message.publication_time
    return _fingerprint({
        "topic": message.topic, "payload": message.payload,
        "publication": ((publication.coordinate, publication.seconds) if publication else None),
    })


def _recovery_identity(item: RecordedMessage) -> tuple:
    if item.message.publication_time is not None:
        return ("publication", item.fingerprint)
    if item.envelope_copy_fingerprint is not None:
        # This identifies copies across recovery inputs, not a historical
        # occurrence. Repetitions within any one input are retained by counts.
        return ("envelope_copy", item.envelope_copy_fingerprint)
    # Without original time or a complete recorded envelope, only another copy
    # of this same physical record is known to be redundant.
    return ("record", str(item.path.resolve()), item.source_order)


def _fingerprint(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_recording_identity(
    messages: Iterable[RecordedMessage],
    *,
    year: int,
    round_number: int,
    session: str,
) -> str | None:
    expected_session = _session_name(session)
    identities = []
    for item in messages:
        if item.message.topic != "SessionInfo" or not isinstance(item.message.payload, Mapping):
            continue
        payload = item.message.payload
        meeting = payload.get("Meeting")
        meeting = meeting if isinstance(meeting, Mapping) else {}
        actual_round = meeting.get("RoundNumber", meeting.get("Number"))
        start = str(payload.get("StartDate") or "")
        actual_year = int(start[:4]) if len(start) >= 4 and start[:4].isdigit() else None
        actual_session = _session_name(payload.get("Name") or payload.get("Type") or "")
        identities.append((actual_year, actual_round, actual_session))
    if not identities:
        return "recording contains no SessionInfo identity"
    for actual_year, actual_round, actual_session in identities:
        if actual_year is not None and actual_year != int(year):
            return f"year {actual_year} does not match {year}"
        if actual_round is not None and int(actual_round) != int(round_number):
            return f"round {actual_round} does not match {round_number}"
        if actual_session and actual_session != expected_session:
            return f"session {actual_session} does not match {expected_session}"
    return None


def merge_recordings(recordings: Iterable[list[RecordedMessage]]) -> list[RecordedMessage]:
    sources = [values for values in recordings if values]
    if not sources:
        return []
    sources.sort(key=lambda values: (-len(values), str(values[0].path)))
    if all(item.has_envelope for source in sources for item in source):
        return _merge_envelope_recordings(sources)
    phased = [_phase_messages(source) for source in sources]
    phases = sorted({phase for buckets in phased for phase in buckets})
    output: list[RecordedMessage] = []
    for phase in phases:
        backbone = phased[0].get(phase, ())
        emitted = Counter(_recovery_identity(item) for item in backbone)
        # Add only observations not already represented the same number of
        # times in the canonical capture. Occurrence counts retain A,B,A-style
        # corrections while removing cross-process duplicates.
        additions: list[RecordedMessage] = []
        for buckets in reversed(phased[1:]):
            occurrences: Counter[tuple] = Counter()
            for item in buckets.get(phase, ()):
                identity = _recovery_identity(item)
                occurrences[identity] += 1
                if occurrences[identity] <= emitted[identity]:
                    continue
                additions.append(item)
                emitted[identity] += 1
        # Secondary observations are applied first so the longest capture's
        # state at this race phase remains authoritative while unique partial
        # fields from secondary processes survive its incremental patches.
        output.extend(additions)
        output.extend(backbone)
    return output


def _merge_envelope_recordings(
    sources: list[list[RecordedMessage]],
) -> list[RecordedMessage]:
    """Merge v2 sources by receive time without violating any source order."""
    ordered = []
    for source_index, source in enumerate(sources):
        previous_time = float("-inf")
        for item in source:
            received_time = item.message.received_at_utc.timestamp()
            monotonic_time = max(received_time, previous_time)
            previous_time = monotonic_time
            ordered.append(
                (monotonic_time, source_index, item.source_order, item)
            )
    ordered.sort(key=lambda value: value[:3])
    output = []
    emitted: Counter[tuple] = Counter()
    occurrences = [Counter() for _source in sources]
    for _time, source_index, _order, item in ordered:
        identity = _recovery_identity(item)
        occurrences[source_index][identity] += 1
        occurrence = occurrences[source_index][identity]
        # Deduplicate overlapping inputs, not observations within one capture.
        # Content equality alone cannot remove a clear/boundary. Even identical
        # envelopes repeated within a capture retain their observed multiplicity.
        if occurrence <= emitted[identity]:
            continue
        emitted[identity] = occurrence
        output.append(item)
    return output


def _phase_messages(
    source: list[RecordedMessage],
) -> dict[int, list[RecordedMessage]]:
    phase = 0
    buckets: dict[int, list[RecordedMessage]] = {}
    for item in source:
        phase = max(phase, _observed_lap(item.message))
        buckets.setdefault(phase, []).append(item)
    return buckets


def _observed_lap(message: LiveTimingMessage) -> int:
    payload = message.payload
    if not isinstance(payload, Mapping):
        return 0
    values = []
    if message.topic == "LapCount":
        values.append(payload.get("CurrentLap"))
    elif message.topic == "TimingData":
        lines = payload.get("Lines", payload)
        if isinstance(lines, Mapping):
            values.extend(
                row.get("NumberOfLaps")
                for row in lines.values()
                if isinstance(row, Mapping)
            )
    parsed = []
    for value in values:
        try:
            parsed.append(int(value))
        except (TypeError, ValueError):
            pass
    return max(parsed, default=0)


def _session_name(value: object) -> str:
    normalized = str(value or "").strip().upper()
    return {"R": "RACE", "Q": "QUALIFYING", "S": "SPRINT"}.get(normalized, normalized)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
