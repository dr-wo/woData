"""Durable event-scoped history of operational race assumptions.

These records mirror values adopted through the editable current workflow
configuration.  They are not empirical observations and are never a live
configuration source.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import get_data_root


SCHEMA_VERSION = "schema_v1"
SEMANTIC_ROLE = "event_operational_assumptions"
_IDENTITY_KEYS = (
    "event_name",
    "official_event_name",
    "country",
    "location",
    "circuit",
    "circuit_id",
)
_UNSET = object()


def event_operational_inputs_path(
    *, season: int, round_number: int, data_root: str | Path | None = None,
) -> Path:
    return (
        get_data_root(data_root)
        / "woweekend"
        / "event_operational_inputs"
        / SCHEMA_VERSION
        / f"year={_positive_int(season, 'season')}"
        / f"round={_positive_int(round_number, 'round_number')}"
        / "operational_inputs.json"
    )


def write_event_operational_inputs(
    *,
    season: int,
    round_number: int,
    total_laps: int | None | object = _UNSET,
    green_pit_loss: float | None | object = _UNSET,
    sc_pit_loss: float | None | object = _UNSET,
    vsc_pit_loss: float | None | object = _UNSET,
    legacy_sc_vsc_pit_loss: float | None | object = _UNSET,
    event_identity: Mapping[str, object] | None = None,
    source: str = "woWeekend/configs/race.json",
    updated_via: str = "woPlanner",
    updated_at: str | datetime | None = None,
    data_root: str | Path | None = None,
) -> Path:
    """Atomically replace the latest durable mirror for one event.

    The current editable configuration remains authoritative. Repeated writes
    update only this event's record, so moving the editable configuration to a
    later round cannot erase the earlier event history.
    """
    event_season = _positive_int(season, "season")
    event_round = _positive_int(round_number, "round_number")
    path = event_operational_inputs_path(
        season=event_season, round_number=event_round, data_root=data_root,
    )
    existing: Mapping[str, Any] = {}
    if path.exists():
        existing = read_event_operational_inputs(
            season=event_season, round_number=event_round, data_root=data_root,
        )
    identity = _event_identity(existing.get("event_identity"), event_identity)
    if not has_stable_event_identity(identity):
        raise ValueError(
            "Durable event operational history requires a stable circuit identity "
            "(circuit ID, country/location, or circuit)"
        )
    existing_operational = existing.get("operational", {})
    if not isinstance(existing_operational, Mapping):
        existing_operational = {}
    existing_race = existing_operational.get("race", {})
    if not isinstance(existing_race, Mapping):
        existing_race = {}
    existing_pit = existing_operational.get("pit_loss", {})
    if not isinstance(existing_pit, Mapping):
        existing_pit = {}
    resolved_total_laps = _merged_value(
        total_laps,
        existing_race.get("total_laps"),
        lambda value: _optional_positive_int(value, "total_laps"),
    )
    resolved_pit_losses = {
        state: _merged_value(
            value,
            _nested_total(existing_pit, stored_key),
            lambda item, label=label: _optional_pit_loss(item, label),
        )
        for state, stored_key, value, label in (
            ("green", "green", green_pit_loss, "green_pit_loss"),
            ("sc", "sc", sc_pit_loss, "sc_pit_loss"),
            ("vsc", "vsc", vsc_pit_loss, "vsc_pit_loss"),
            (
                "legacy_sc_vsc",
                "legacy_sc_vsc",
                legacy_sc_vsc_pit_loss,
                "legacy_sc_vsc_pit_loss",
            ),
        )
    }
    timestamp = _utc_timestamp(updated_at)
    created_at = str(existing.get("created_at") or timestamp)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "semantic_role": SEMANTIC_ROLE,
        "operational_assumptions_not_empirical": True,
        "event_id": f"{event_season:04d}-{event_round:02d}",
        "season": event_season,
        "round_number": event_round,
        "event_identity": identity,
        "event_identity_status": "available",
        "operational": {
            "race": {
                "total_laps": resolved_total_laps,
            },
            "pit_loss": {
                "green": {"total": resolved_pit_losses["green"]},
                "sc": {"total": resolved_pit_losses["sc"]},
                "vsc": {"total": resolved_pit_losses["vsc"]},
                "legacy_sc_vsc": {
                    "total": resolved_pit_losses["legacy_sc_vsc"],
                    "semantic_role": "legacy_combined_context_only",
                    "not_separate_sc_or_vsc": True,
                },
            },
        },
        "provenance": {
            "source": str(source),
            "updated_via": str(updated_via),
            "authority": "mirror_of_current_editable_workflow_configuration",
            "operational_input": True,
            "empirical_observation": False,
        },
        "created_at": created_at,
        "updated_at": timestamp,
    }
    _atomic_write_json(path, payload)
    return path


def event_identity_from_metadata(metadata: object) -> dict[str, str]:
    """Extract the durable event/circuit identity already present in metadata."""
    source = metadata if isinstance(metadata, Mapping) else {
        key: getattr(metadata, key, None) for key in _IDENTITY_KEYS
    }
    return _event_identity(None, source)


def resolve_event_operational_identity(
    *,
    season: int,
    round_number: int,
    event_identity: Mapping[str, object] | None = None,
    event_identity_event_key: tuple[int, int] | None = None,
    resolver=None,
) -> dict[str, str]:
    """Return identity proven to belong to the requested season and round."""
    requested_key = (
        _positive_int(season, "season"),
        _positive_int(round_number, "round_number"),
    )
    identity = _event_identity(None, event_identity)
    if (
        _event_key(event_identity_event_key) == requested_key
        and has_stable_event_identity(identity)
    ):
        return identity
    if resolver is None:
        from .events import resolve_event

        resolver = resolve_event
    metadata = resolver(
        f"{requested_key[0]:04d}-{requested_key[1]:02d}",
        include_race_distance=False,
    )
    canonical_identity = event_identity_from_metadata(metadata)
    if not has_stable_event_identity(canonical_identity):
        raise ValueError(
            "Canonical event metadata did not provide a stable circuit identity "
            "(circuit ID, country/location, or circuit)"
        )
    if _stable_identity_conflicts(identity, canonical_identity):
        return canonical_identity
    return _event_identity(identity, canonical_identity)


def has_stable_event_identity(identity: object) -> bool:
    if not isinstance(identity, Mapping):
        return False
    if _identity_text(identity.get("circuit_id")):
        return True
    if _identity_text(identity.get("country")) and _identity_text(identity.get("location")):
        return True
    return bool(_identity_text(identity.get("circuit")))


def read_event_operational_inputs(
    *, season: int, round_number: int, data_root: str | Path | None = None,
) -> dict[str, Any]:
    path = event_operational_inputs_path(
        season=season, round_number=round_number, data_root=data_root,
    )
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Event operational record must be a JSON object: {path}")
    if value.get("semantic_role") != SEMANTIC_ROLE:
        raise ValueError(f"Unexpected event operational semantic role: {path}")
    return value


def find_event_operational_inputs_by_identity(
    *,
    season: int,
    event_identity: Mapping[str, object],
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return one strong same-circuit match, never a display-name guess."""
    root = (
        get_data_root(data_root)
        / "woweekend"
        / "event_operational_inputs"
        / SCHEMA_VERSION
        / f"year={_positive_int(season, 'season')}"
    )
    matches = []
    for path in sorted(root.glob("round=*/operational_inputs.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(record, dict) and _same_circuit(
            record.get("event_identity"), event_identity,
        ):
            matches.append(record)
    if len(matches) > 1:
        raise ValueError(
            f"Multiple {season} operational records match the supplied circuit identity"
        )
    return matches[0] if matches else None


def _event_identity(
    existing: object, update: Mapping[str, object] | None,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for candidate in (existing, update):
        if not isinstance(candidate, Mapping):
            continue
        for key in _IDENTITY_KEYS:
            value = str(candidate.get(key) or "").strip()
            if value:
                output[key] = value
    return output


def _nested_total(pit_loss: Mapping[str, Any], state: str) -> object:
    value = pit_loss.get(state)
    return value.get("total") if isinstance(value, Mapping) else None


def _merged_value(value: object, existing: object, validator) -> object:
    return validator(existing if value is _UNSET else value)


def _same_circuit(left: object, right: Mapping[str, object]) -> bool:
    if not isinstance(left, Mapping):
        return False
    left_circuit_id = _identity_text(left.get("circuit_id"))
    right_circuit_id = _identity_text(right.get("circuit_id"))
    if left_circuit_id and right_circuit_id:
        return left_circuit_id == right_circuit_id
    left_country = _identity_text(left.get("country"))
    right_country = _identity_text(right.get("country"))
    left_location = _identity_text(left.get("location"))
    right_location = _identity_text(right.get("location"))
    if all((left_country, right_country, left_location, right_location)):
        return left_country == right_country and left_location == right_location
    left_circuit = _identity_text(left.get("circuit"))
    right_circuit = _identity_text(right.get("circuit"))
    return bool(left_circuit and right_circuit and left_circuit == right_circuit)


def _identity_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _event_key(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    try:
        return _positive_int(value[0], "identity season"), _positive_int(
            value[1], "identity round_number"
        )
    except ValueError:
        return None


def _stable_identity_conflicts(left: object, right: object) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    for key in ("circuit_id", "circuit"):
        left_value = _identity_text(left.get(key))
        right_value = _identity_text(right.get(key))
        if left_value and right_value and left_value != right_value:
            return True
    left_country = _identity_text(left.get("country"))
    right_country = _identity_text(right.get("country"))
    left_location = _identity_text(left.get("location"))
    right_location = _identity_text(right.get("location"))
    return bool(
        left_country
        and right_country
        and left_location
        and right_location
        and (left_country, left_location) != (right_country, right_location)
    )


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _optional_positive_int(value: object, label: str) -> int | None:
    return None if value is None else _positive_int(value, label)


def _optional_pit_loss(value: object, label: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric or null") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 120.0:
        raise ValueError(f"{label} must be finite and between 0 and 120 seconds")
    return parsed


def _utc_timestamp(value: str | datetime | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "SCHEMA_VERSION",
    "SEMANTIC_ROLE",
    "event_identity_from_metadata",
    "event_operational_inputs_path",
    "find_event_operational_inputs_by_identity",
    "has_stable_event_identity",
    "read_event_operational_inputs",
    "resolve_event_operational_identity",
    "write_event_operational_inputs",
]
