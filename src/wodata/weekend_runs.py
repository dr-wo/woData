"""Generic immutable storage for small weekend workflow artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

from . import get_data_root


SCHEMA_VERSION = "schema_v1"
USABLE_STATUSES = frozenset({"SUCCESS", "PARTIAL"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_component(value: str, *, label: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("._")
    if not normalized:
        raise ValueError(f"{label} must contain at least one letter or number")
    return normalized


def weekend_runs_root(data_root: str | Path | None = None) -> Path:
    return get_data_root(data_root) / "woweekend" / "runs" / SCHEMA_VERSION


@dataclass(frozen=True)
class WeekendArtifactReference:
    artifact_id: str
    uri: str
    content_hash: str | None = None
    producer: str | None = None
    producer_version: str | None = None
    recipe: Mapping[str, Any] | None = None
    inputs: Mapping[str, Any] | None = None


@dataclass
class WeekendRun:
    event: str
    workflow: str
    run_id: str
    path: Path
    generated_at: datetime

    def save_json(self, name: str, value: Any) -> Path:
        return self._save_bytes(_artifact_filename(name, ".json"), _json_bytes(value))

    def save_text(self, name: str, value: str, *, suffix: str = ".txt") -> Path:
        return self._save_bytes(_artifact_filename(name, suffix), value.encode("utf-8"))

    def save_bytes(self, name: str, value: bytes, *, suffix: str = "") -> Path:
        return self._save_bytes(_artifact_filename(name, suffix), value)

    def save_reference(self, name: str, reference: WeekendArtifactReference) -> Path:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "classification": "heavyweight_reproducible_cache_reference",
            "artifact_id": reference.artifact_id,
            "uri": reference.uri,
            "content_hash": reference.content_hash,
            "producer": reference.producer,
            "producer_version": reference.producer_version,
            "recipe": reference.recipe,
            "inputs": reference.inputs,
        }
        return self.save_json(f"{name}.reference", payload)

    def load_json(self, name: str) -> Any:
        path = self.path / _artifact_filename(name, ".json")
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def load_reference(self, name: str, *, require_target: bool = False) -> dict[str, Any]:
        payload = self.load_json(f"{name}.reference")
        target = Path(str(payload["uri"]))
        payload["available"] = target.exists()
        if require_target and not target.exists():
            raise FileNotFoundError(
                f"Referenced artifact {payload['artifact_id']!r} is missing at {target}; "
                "no latest-cache substitution was attempted."
            )
        return payload

    def save_manifest(self, manifest: Mapping[str, Any]) -> Path:
        payload = dict(manifest)
        payload.setdefault("workflow", self.workflow)
        payload.setdefault("event", self.event)
        payload.setdefault("run_id", self.run_id)
        payload.setdefault("generated_at", self.generated_at.isoformat())
        payload.setdefault("inputs", {})
        payload.setdefault("outputs", {})
        payload.setdefault("warnings", [])
        status = str(payload.get("status", "FAILED")).upper()
        if status not in {"SUCCESS", "PARTIAL", "FAILED"}:
            raise ValueError("manifest status must be SUCCESS, PARTIAL, or FAILED")
        payload["status"] = status
        destination = self.save_json("manifest", payload)
        _atomic_write_json(
            self.path.parent.parent / "latest.json",
            {"run_id": self.run_id, "generated_at": self.generated_at.isoformat(), "status": status},
        )
        return destination

    def _save_bytes(self, filename: str, value: bytes) -> Path:
        destination = self.path / filename
        if destination.exists():
            raise FileExistsError(f"Run artifacts are immutable: {destination} already exists")
        _atomic_write_bytes(destination, value)
        self._record_artifact(filename, value)
        return destination

    def _record_artifact(self, filename: str, value: bytes) -> None:
        index_path = self.path / ".artifacts.json"
        if index_path.exists():
            with index_path.open(encoding="utf-8") as handle:
                index = json.load(handle)
        else:
            index = {"schema_version": SCHEMA_VERSION, "artifacts": {}}
        index["artifacts"][filename] = {
            "classification": "lightweight_run_result",
            "size_bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
        _atomic_write_json(index_path, index)

    def artifact_metadata(self) -> dict[str, Any]:
        with (self.path / ".artifacts.json").open(encoding="utf-8") as handle:
            return json.load(handle)


class WeekendRunStore:
    """Factory and lookup facade for generic weekend workflow runs."""

    @classmethod
    def create(
        cls,
        *,
        event: str,
        workflow: str,
        data_root: str | Path | None = None,
        run_id: str | None = None,
        generated_at: str | datetime | None = None,
    ) -> WeekendRun:
        timestamp = _utc_now() if generated_at is None else _parse_time(generated_at)
        event_key = _safe_component(event, label="event")
        workflow_key = _safe_component(workflow, label="workflow")
        identifier = run_id or f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex[:10]}"
        identifier = _safe_component(identifier, label="run_id")
        path = (
            weekend_runs_root(data_root)
            / f"event={event_key}"
            / f"workflow={workflow_key}"
            / "runs"
            / identifier
        )
        path.mkdir(parents=True, exist_ok=False)
        _atomic_write_json(path / "run.json", {
            "schema_version": SCHEMA_VERSION,
            "event": str(event),
            "workflow": str(workflow),
            "run_id": identifier,
            "generated_at": timestamp.isoformat(),
        })
        return WeekendRun(str(event), str(workflow), identifier, path, timestamp)

    @classmethod
    def load(
        cls, *, event: str, workflow: str, run_id: str, data_root: str | Path | None = None
    ) -> WeekendRun:
        directory = cls._workflow_root(event, workflow, data_root) / "runs" / _safe_component(run_id, label="run_id")
        return _load_run(directory)

    @classmethod
    def load_latest(
        cls, *, event: str, workflow: str, data_root: str | Path | None = None
    ) -> WeekendRun:
        root = cls._workflow_root(event, workflow, data_root)
        pointer = root / "latest.json"
        if pointer.exists():
            with pointer.open(encoding="utf-8") as handle:
                run_id = json.load(handle)["run_id"]
            return _load_run(root / "runs" / run_id)
        runs = cls.list_runs(event=event, workflow=workflow, data_root=data_root)
        if not runs:
            raise FileNotFoundError(f"No {workflow!r} runs exist for event {event!r}")
        return runs[-1]

    @classmethod
    def select_before(
        cls,
        *,
        event: str,
        workflow: str,
        before: str | datetime,
        data_root: str | Path | None = None,
        usable_only: bool = True,
        required_artifacts: tuple[str, ...] = (),
    ) -> WeekendRun:
        cutoff = _parse_time(before)
        candidates = []
        for run in cls.list_runs(event=event, workflow=workflow, data_root=data_root):
            if run.generated_at > cutoff:
                continue
            if usable_only:
                try:
                    status = str(run.load_json("manifest").get("status", "")).upper()
                except FileNotFoundError:
                    continue
                if status not in USABLE_STATUSES:
                    continue
            if any(
                not (run.path / _artifact_filename(name, ".json")).exists()
                for name in required_artifacts
            ):
                continue
            candidates.append(run)
        if not candidates:
            raise FileNotFoundError(
                f"No usable {workflow!r} run for {event!r} exists before {cutoff.isoformat()}"
            )
        return candidates[-1]

    @classmethod
    def list_runs(
        cls, *, event: str, workflow: str, data_root: str | Path | None = None
    ) -> list[WeekendRun]:
        runs_root = cls._workflow_root(event, workflow, data_root) / "runs"
        runs = [_load_run(path) for path in runs_root.iterdir() if path.is_dir()] if runs_root.exists() else []
        return sorted(runs, key=lambda run: (run.generated_at, run.run_id))

    @staticmethod
    def _workflow_root(event: str, workflow: str, data_root: str | Path | None) -> Path:
        return (
            weekend_runs_root(data_root)
            / f"event={_safe_component(event, label='event')}"
            / f"workflow={_safe_component(workflow, label='workflow')}"
        )


def _load_run(path: Path) -> WeekendRun:
    with (path / "run.json").open(encoding="utf-8") as handle:
        metadata = json.load(handle)
    return WeekendRun(
        str(metadata["event"]), str(metadata["workflow"]), str(metadata["run_id"]),
        path, _parse_time(str(metadata["generated_at"])),
    )


def _artifact_filename(name: str, suffix: str) -> str:
    value = str(name).strip()
    if not value or value.startswith(".") or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Invalid artifact name: {name!r}")
    return value if suffix and value.endswith(suffix) else value + suffix


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "SCHEMA_VERSION", "WeekendArtifactReference", "WeekendRun", "WeekendRunStore",
    "file_sha256", "weekend_runs_root",
]
