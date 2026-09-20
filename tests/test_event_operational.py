from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from wodata.event_operational import (
    event_operational_inputs_path,
    find_event_operational_inputs_by_identity,
    read_event_operational_inputs,
    resolve_event_operational_identity,
    write_event_operational_inputs,
)


def test_new_durable_record_requires_stable_event_identity(tmp_path):
    with pytest.raises(ValueError, match="requires a stable circuit identity"):
        write_event_operational_inputs(
            season=2026,
            round_number=14,
            total_laps=57,
            green_pit_loss=24.2,
            event_identity={"event_name": "Display name is not stable identity"},
            data_root=tmp_path,
        )


def test_stable_supplied_identity_is_revalidated_for_requested_event():
    calls = []

    class CanonicalMetadata:
        event_name = "British Grand Prix"
        official_event_name = None
        country = "Great Britain"
        location = "Silverstone"
        circuit = "Silverstone"
        circuit_id = "2"

    def resolver(event, *, include_race_distance):
        calls.append((event, include_race_distance))
        return CanonicalMetadata()

    identity = resolve_event_operational_identity(
        season=2026,
        round_number=15,
        event_identity={
            "event_name": "Spanish Grand Prix",
            "country": "Spain",
            "location": "Madrid",
        },
        resolver=resolver,
    )

    assert calls == [("2026-15", False)]
    assert identity == {
        "event_name": "British Grand Prix",
        "country": "Great Britain",
        "location": "Silverstone",
        "circuit": "Silverstone",
        "circuit_id": "2",
    }


def test_stable_identity_owned_by_requested_event_reuses_without_resolution():
    def resolver(*_args, **_kwargs):
        raise AssertionError("same-event canonical identity should be reused")

    identity = resolve_event_operational_identity(
        season=2026,
        round_number=15,
        event_identity={"country": "Great Britain", "location": "Silverstone"},
        event_identity_event_key=(2026, 15),
        resolver=resolver,
    )

    assert identity == {"country": "Great Britain", "location": "Silverstone"}


def test_event_operational_record_is_latest_per_event_and_semantically_explicit(tmp_path):
    first_time = datetime(2026, 9, 19, tzinfo=timezone.utc)
    second_time = datetime(2026, 9, 20, tzinfo=timezone.utc)
    path = write_event_operational_inputs(
        season=2026,
        round_number=14,
        total_laps=57,
        green_pit_loss=24.2,
        sc_pit_loss=None,
        vsc_pit_loss=13.0,
        legacy_sc_vsc_pit_loss=12.5,
        event_identity={"country": "Testland", "location": "Test Circuit"},
        updated_at=first_time,
        data_root=tmp_path,
    )
    write_event_operational_inputs(
        season=2026,
        round_number=14,
        total_laps=57,
        green_pit_loss=24.2,
        sc_pit_loss=None,
        vsc_pit_loss=12.0,
        legacy_sc_vsc_pit_loss=12.5,
        updated_at=second_time,
        data_root=tmp_path,
    )

    assert path == event_operational_inputs_path(
        season=2026, round_number=14, data_root=tmp_path,
    )
    record = read_event_operational_inputs(
        season=2026, round_number=14, data_root=tmp_path,
    )
    assert record["semantic_role"] == "event_operational_assumptions"
    assert record["operational_assumptions_not_empirical"] is True
    assert record["operational"]["pit_loss"]["vsc"]["total"] == 12.0
    assert record["operational"]["pit_loss"]["sc"]["total"] is None
    assert record["operational"]["pit_loss"]["legacy_sc_vsc"] == {
        "not_separate_sc_or_vsc": True,
        "semantic_role": "legacy_combined_context_only",
        "total": 12.5,
    }
    assert record["event_identity"] == {
        "country": "Testland", "location": "Test Circuit",
    }
    assert record["created_at"] == first_time.isoformat()
    assert record["updated_at"] == second_time.isoformat()


def test_cross_season_lookup_uses_circuit_identity_not_display_name(tmp_path):
    common = dict(
        season=2025, total_laps=57, green_pit_loss=24.2,
        sc_pit_loss=None, vsc_pit_loss=12.0, data_root=tmp_path,
    )
    write_event_operational_inputs(
        round_number=4,
        event_identity={
            "event_name": "Renamed Grand Prix",
            "country": "Testland",
            "location": "Test Circuit",
        },
        **common,
    )

    record = find_event_operational_inputs_by_identity(
        season=2025,
        event_identity={
            "event_name": "New Sponsor Grand Prix",
            "country": "Testland",
            "location": "Test Circuit",
        },
        data_root=tmp_path,
    )

    assert record is not None
    assert record["event_id"] == "2025-04"
    assert find_event_operational_inputs_by_identity(
        season=2025,
        event_identity={"event_name": "Renamed Grand Prix"},
        data_root=tmp_path,
    ) is None


def test_cross_season_lookup_rejects_ambiguous_circuit_identity(tmp_path):
    for round_number in (4, 8):
        write_event_operational_inputs(
            season=2025,
            round_number=round_number,
            total_laps=57,
            green_pit_loss=24.2,
            sc_pit_loss=None,
            vsc_pit_loss=12.0,
            event_identity={"country": "Testland", "location": "Test Circuit"},
            data_root=tmp_path,
        )

    with pytest.raises(ValueError, match="Multiple 2025 operational records"):
        find_event_operational_inputs_by_identity(
            season=2025,
            event_identity={"country": "Testland", "location": "Test Circuit"},
            data_root=tmp_path,
        )


def test_r14_migration_preserves_unresolved_separate_values_and_legacy_context():
    path = (
        Path(__file__).resolve().parents[1]
        / "woweekend/event_operational_inputs/schema_v1/year=2026/round=14"
        / "operational_inputs.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["operational"]["race"]["total_laps"] == 57
    assert record["operational"]["pit_loss"]["green"]["total"] == 24.2
    assert record["operational"]["pit_loss"]["sc"]["total"] is None
    assert record["operational"]["pit_loss"]["vsc"]["total"] is None
    assert record["operational"]["pit_loss"]["legacy_sc_vsc"] == {
        "not_separate_sc_or_vsc": True,
        "semantic_role": "legacy_combined_context_only",
        "total": 13.0,
    }
    assert record["event_identity"] == {
        "country": "Spain",
        "event_name": "Spanish Grand Prix",
        "location": "Madrid",
        "official_event_name": "FORMULA 1 TAG HEUER GRAN PREMIO DE ESPAÑA 2026",
    }
    assert record["event_identity_status"] == "available"
    discovered = find_event_operational_inputs_by_identity(
        season=2026,
        event_identity={"country": "Spain", "location": "Madrid"},
        data_root=Path(__file__).resolve().parents[1],
    )
    assert discovered is not None
    assert discovered["event_id"] == "2026-14"


def test_partial_updates_merge_without_erasing_other_operational_fields(tmp_path):
    identity = {"country": "Spain", "location": "Madrid"}
    write_event_operational_inputs(
        season=2026,
        round_number=14,
        green_pit_loss=24.2,
        vsc_pit_loss=12.0,
        event_identity=identity,
        data_root=tmp_path,
    )
    write_event_operational_inputs(
        season=2026,
        round_number=14,
        total_laps=57,
        event_identity={"event_name": "Spanish Grand Prix"},
        updated_via="woWeekend race preparation",
        data_root=tmp_path,
    )
    write_event_operational_inputs(
        season=2026,
        round_number=14,
        vsc_pit_loss=11.5,
        data_root=tmp_path,
    )

    record = read_event_operational_inputs(
        season=2026, round_number=14, data_root=tmp_path,
    )
    assert record["operational"]["race"]["total_laps"] == 57
    assert record["operational"]["pit_loss"]["green"]["total"] == 24.2
    assert record["operational"]["pit_loss"]["vsc"]["total"] == 11.5
    assert record["event_identity"] == {
        "country": "Spain",
        "event_name": "Spanish Grand Prix",
        "location": "Madrid",
    }
