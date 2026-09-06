from datetime import datetime, timezone
import json

import pytest

from wodata.artifacts import (
    read_latest_model_snapshot,
    read_strategy_records,
    write_model_snapshot,
    write_joint_candidate_costs,
    write_strategy_records,
)
from wodata.contracts import (
    ModelSnapshot,
    ParameterEstimate,
    StrategyRecord,
    StrategySearchState,
    canonical_hash,
)


def test_canonical_hash_is_order_independent_for_mappings():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_strategy_state_laps_remaining_never_negative():
    state = StrategySearchState(60, 58, "HARD", 12)
    assert state.laps_remaining == 0


def test_model_and_strategy_artifacts_round_trip(tmp_path):
    created = datetime(2026, 7, 30, tzinfo=timezone.utc)
    estimate = ParameterEstimate(
        "analysis-1", "bank-1", "FP2", 0, "degradation", "FP2", None,
        "MEDIUM", "HARD", 0.07, 0.08, 0.09, "s/lap", "measured", 12, 3,
        50.0, 0.2, 0.0, 0.0, "input", "config", created,
    )
    snapshot = ModelSnapshot(
        "analysis-1", "bank-1", "FP2", 0, (estimate,), "input", "config", created
    )
    destination = write_model_snapshot(snapshot, year=2026, round_number=4, data_root=tmp_path)
    assert (destination / "latest_support.csv").is_file()
    assert (destination / "manifest.json").is_file()
    manifest, rows = read_latest_model_snapshot(
        year=2026, round_number=4, source_scope="FP2", data_root=tmp_path
    )
    assert manifest["analysis_id"] == "analysis-1"
    assert manifest["included_sessions"] == ["FP2"]
    assert manifest["max_information_session"] == "FP2"
    assert manifest["information_cutoff"] == "FP2"
    assert rows[0]["compound"] == "MEDIUM"
    costs_path = write_joint_candidate_costs(
        [{"SampleId": 0, "TotalCost": 1.2, "FP2Cost": 1.2}],
        year=2026, round_number=4, data_root=tmp_path,
    )
    assert "FP2Cost" in costs_path.read_text()

    record = StrategyRecord(
        "search-1", "strategy-1", 1, "continuation", "44", 20, 38, "MEDIUM", 8,
        1, ("MEDIUM", "HARD"), (25,), "M-(25)H", 12.5, 0.0, "analysis-1",
        "v1", "state", "current", created,
    )
    write_strategy_records(
        [record], year=2026, round_number=4, target="44", data_root=tmp_path
    )
    assert read_strategy_records(
        year=2026, round_number=4, target="44", data_root=tmp_path
    )[0]["formatted_strategy"] == "M-(25)H"


@pytest.mark.parametrize(
    ("scope", "contributors"),
    [
        ("FP1", ("FP1", "FP2")),
        ("FP1", ("FP3",)),
        ("FP1", ("R",)),
        ("FP2", ("FP2", "FP3")),
        ("FP2", ("R",)),
        ("FP3", ("FP3", "R")),
    ],
)
def test_session_snapshot_rejects_information_beyond_cutoff(
    tmp_path, scope, contributors
):
    created = datetime(2026, 7, 30, tzinfo=timezone.utc)
    snapshot = ModelSnapshot(
        "analysis-1", "bank-1", scope, 0, (), "input", "config", created,
        contributing_sessions=contributors,
    )
    with pytest.raises(ValueError, match="information beyond its cutoff"):
        write_model_snapshot(
            snapshot, year=2026, round_number=4, data_root=tmp_path
        )


def test_aggregate_snapshot_retains_full_information_state(tmp_path):
    created = datetime(2026, 7, 30, tzinfo=timezone.utc)
    snapshot = ModelSnapshot(
        "analysis-1", "bank-1", "aggregate", 0, (), "input", "config", created,
        contributing_sessions=("FP1", "FP2", "FP3", "R"),
    )
    destination = write_model_snapshot(
        snapshot, year=2026, round_number=4, data_root=tmp_path
    )
    manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["included_sessions"] == ["FP1", "FP2", "FP3", "R"]
    assert manifest["information_cutoff"] == "latest"
