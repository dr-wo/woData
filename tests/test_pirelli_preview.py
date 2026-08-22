from __future__ import annotations

import json

import pytest

from wodata.pirelli_preview import (
    PirelliPreviewRecord,
    PreviewValidationError,
    ingest_previews,
    preview_csv_path,
    preview_json_path,
    rebuild_preview_csv,
)


VALID = {
    "event": "Example Grand Prix",
    "circuit": "Example Circuit",
    "tyre_stress": 4,
    "asphalt_abrasion": 3,
    "asphalt_grip": 2,
    "traction": 3,
    "lateral": 4,
    "hard_compound": "C2",
    "medium_compound": "C3",
    "soft_compound": "C4",
}


class FakeExtractor:
    model_name = "fake-vision"

    def __init__(self, values=None):
        self.values = values or VALID
        self.calls = 0

    def extract(self, image_path, *, season, round_number):
        self.calls += 1
        return self.values


def test_valid_structured_extraction_is_accepted():
    record = PirelliPreviewRecord.from_extraction(
        VALID, season=2026, round_number=1, source_file="preview.jpg",
        source_hash="abc", extraction_model="test",
    )
    assert record.review_status == "accepted"
    assert record.hard_compound == "C2"


@pytest.mark.parametrize("field", ["tyre_stress", "asphalt_abrasion", "asphalt_grip"])
def test_out_of_range_rating_is_rejected(field):
    values = {**VALID, field: 6}
    with pytest.raises(PreviewValidationError, match="between 1 and 5"):
        PirelliPreviewRecord.from_extraction(
            values, season=2026, round_number=1, source_file="x.jpg",
            source_hash="abc", extraction_model="test",
        )


def test_invalid_compound_assignment_is_rejected():
    with pytest.raises(PreviewValidationError, match="distinct"):
        PirelliPreviewRecord.from_extraction(
            {**VALID, "soft_compound": "C3"}, season=2026, round_number=1,
            source_file="x.jpg", source_hash="abc", extraction_model="test",
        )


def test_missing_values_remain_null_and_require_review():
    record = PirelliPreviewRecord.from_extraction(
        {**VALID, "tyre_stress": None}, season=2026, round_number=1,
        source_file="x.jpg", source_hash="abc", extraction_model="test",
    )
    assert record.tyre_stress is None
    assert record.review_status == "manual_review"


def test_hash_skip_change_replacement_and_deterministic_csv(tmp_path):
    year_dir = tmp_path / "pirelli_preview/year=2026"
    year_dir.mkdir(parents=True)
    image = year_dir / "1920_01-aa26-preview-en.jpg"
    image.write_bytes(b"first")
    extractor = FakeExtractor()

    first = ingest_previews(season=2026, extractor=extractor, data_root=tmp_path)
    first_csv = preview_csv_path(2026, tmp_path).read_bytes()
    assert first.extracted == 1
    assert first.accepted == 1
    assert extractor.calls == 1

    second = ingest_previews(season=2026, extractor=extractor, data_root=tmp_path)
    assert second.skipped_unchanged == 1
    assert extractor.calls == 1
    assert preview_csv_path(2026, tmp_path).read_bytes() == first_csv

    image.write_bytes(b"changed")
    third = ingest_previews(season=2026, extractor=extractor, data_root=tmp_path)
    assert third.extracted == 1
    assert extractor.calls == 2
    stored = json.loads(preview_json_path(2026, 1, tmp_path).read_text())
    assert stored["source_hash"] != "abc"
    rebuilt = preview_csv_path(2026, tmp_path).read_bytes()
    rebuild_preview_csv(season=2026, data_root=tmp_path)
    assert preview_csv_path(2026, tmp_path).read_bytes() == rebuilt
