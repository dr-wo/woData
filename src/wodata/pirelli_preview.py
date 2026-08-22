from __future__ import annotations

import base64
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

from . import get_data_root


SCHEMA_VERSION = "schema_v1"
REVIEW_ACCEPTED = "accepted"
REVIEW_REQUIRED = "manual_review"
VALID_COMPOUNDS = frozenset({"C1", "C2", "C3", "C4", "C5"})
RATING_FIELDS = (
    "tyre_stress",
    "asphalt_abrasion",
    "asphalt_grip",
    "traction",
    "lateral",
)
CSV_COLUMNS = (
    "schema_version",
    "season",
    "round",
    "event",
    "circuit",
    *RATING_FIELDS,
    "hard_compound",
    "medium_compound",
    "soft_compound",
    "source_file",
    "source_hash",
    "extraction_timestamp",
    "extraction_model",
    "review_status",
)
_ROUND_PATTERN = re.compile(r"(?:^|_)(\d{2})(?:-|_)")


class PreviewValidationError(ValueError):
    pass


class PreviewVisionExtractor(Protocol):
    @property
    def model_name(self) -> str:
        ...

    def extract(self, image_path: Path, *, season: int, round_number: int) -> Mapping:
        ...


@dataclass(frozen=True)
class PirelliPreviewRecord:
    schema_version: str
    season: int
    round: int
    event: str | None
    circuit: str | None
    tyre_stress: int | None
    asphalt_abrasion: int | None
    asphalt_grip: int | None
    traction: int | None
    lateral: int | None
    hard_compound: str | None
    medium_compound: str | None
    soft_compound: str | None
    source_file: str
    source_hash: str
    extraction_timestamp: str
    extraction_model: str
    review_status: str

    @classmethod
    def from_extraction(
        cls,
        values: Mapping,
        *,
        season: int,
        round_number: int,
        source_file: str,
        source_hash: str,
        extraction_model: str,
        extraction_timestamp: str | None = None,
    ) -> "PirelliPreviewRecord":
        ratings = {name: _optional_rating(values.get(name), name) for name in RATING_FIELDS}
        compounds = {
            name: _optional_compound(values.get(name), name)
            for name in ("hard_compound", "medium_compound", "soft_compound")
        }
        present_compounds = [value for value in compounds.values() if value is not None]
        if len(present_compounds) != len(set(present_compounds)):
            raise PreviewValidationError("Hard, medium, and soft compounds must be distinct.")
        event = _optional_text(values.get("event"))
        circuit = _optional_text(values.get("circuit"))
        required = (
            event,
            circuit,
            ratings["tyre_stress"],
            ratings["asphalt_abrasion"],
            ratings["asphalt_grip"],
            compounds["hard_compound"],
            compounds["medium_compound"],
            compounds["soft_compound"],
        )
        review_status = (
            REVIEW_ACCEPTED
            if all(value is not None for value in required)
            else REVIEW_REQUIRED
        )
        return cls(
            schema_version=SCHEMA_VERSION,
            season=int(season),
            round=int(round_number),
            event=event,
            circuit=circuit,
            **ratings,
            **compounds,
            source_file=str(source_file),
            source_hash=str(source_hash),
            extraction_timestamp=extraction_timestamp or _utc_now(),
            extraction_model=str(extraction_model),
            review_status=review_status,
        )

    @classmethod
    def from_json(cls, values: Mapping) -> "PirelliPreviewRecord":
        missing = set(CSV_COLUMNS).difference(values)
        if missing:
            raise PreviewValidationError(f"Preview record is missing fields: {sorted(missing)}")
        record = cls.from_extraction(
            values,
            season=int(values["season"]),
            round_number=int(values["round"]),
            source_file=str(values["source_file"]),
            source_hash=str(values["source_hash"]),
            extraction_model=str(values["extraction_model"]),
            extraction_timestamp=str(values["extraction_timestamp"]),
        )
        if values["schema_version"] != SCHEMA_VERSION:
            raise PreviewValidationError(
                f"Unsupported preview schema {values['schema_version']!r}."
            )
        if values["review_status"] != record.review_status:
            raise PreviewValidationError("review_status does not match the extracted values.")
        return record

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PreviewIngestionReport:
    discovered: int
    accepted: int
    extracted: int
    skipped_unchanged: int
    manual_review: int
    rejected: tuple[str, ...]
    csv_path: Path


def preview_year_dir(season: int, data_root: str | Path | None = None) -> Path:
    return get_data_root(data_root) / "pirelli_preview" / f"year={int(season)}"


def preview_csv_path(season: int, data_root: str | Path | None = None) -> Path:
    return preview_year_dir(season, data_root) / f"pirelli_preview_{int(season)}.csv"


def preview_json_path(
    season: int, round_number: int, data_root: str | Path | None = None
) -> Path:
    return preview_year_dir(season, data_root) / "extracted" / f"round={int(round_number)}.json"


def infer_round_from_filename(path: str | Path) -> int:
    match = _ROUND_PATTERN.search(Path(path).name)
    if match is None:
        raise PreviewValidationError(f"Cannot determine round from filename {Path(path).name!r}.")
    value = int(match.group(1))
    if value <= 0:
        raise PreviewValidationError("Round must be positive.")
    return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_preview_images(directory: str | Path) -> list[Path]:
    suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(
        path for path in Path(directory).iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    )


def ingest_previews(
    *,
    season: int,
    extractor: PreviewVisionExtractor | None,
    data_root: str | Path | None = None,
) -> PreviewIngestionReport:
    directory = preview_year_dir(season, data_root)
    images = scan_preview_images(directory)
    extracted_count = 0
    skipped_count = 0
    rejected: list[str] = []
    seen_rounds: set[int] = set()
    for image_path in images:
        try:
            round_number = infer_round_from_filename(image_path)
            if round_number in seen_rounds:
                raise PreviewValidationError(f"Duplicate preview image for round {round_number}.")
            seen_rounds.add(round_number)
            source_hash = sha256_file(image_path)
            artifact_path = preview_json_path(season, round_number, data_root)
            existing = read_preview_json(artifact_path) if artifact_path.exists() else None
            if existing is not None and existing.source_hash == source_hash:
                skipped_count += 1
                continue
            if extractor is None:
                raise PreviewValidationError(
                    f"{image_path.name} is new or changed but no vision extractor is configured."
                )
            values = extractor.extract(
                image_path, season=int(season), round_number=round_number
            )
            record = PirelliPreviewRecord.from_extraction(
                values,
                season=season,
                round_number=round_number,
                source_file=image_path.name,
                source_hash=source_hash,
                extraction_model=extractor.model_name,
            )
            write_preview_json(record, artifact_path)
            extracted_count += 1
        except (
            OSError,
            PreviewValidationError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            rejected.append(f"{image_path.name}: {exc}")

    records = rebuild_preview_csv(season=season, data_root=data_root)
    return PreviewIngestionReport(
        discovered=len(images),
        accepted=sum(record.review_status == REVIEW_ACCEPTED for record in records),
        extracted=extracted_count,
        skipped_unchanged=skipped_count,
        manual_review=sum(record.review_status == REVIEW_REQUIRED for record in records),
        rejected=tuple(rejected),
        csv_path=preview_csv_path(season, data_root),
    )


def read_preview_json(path: str | Path) -> PirelliPreviewRecord:
    with Path(path).open(encoding="utf-8") as handle:
        return PirelliPreviewRecord.from_json(json.load(handle))


def write_preview_json(record: PirelliPreviewRecord, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)


def rebuild_preview_csv(
    *, season: int, data_root: str | Path | None = None
) -> list[PirelliPreviewRecord]:
    extracted_dir = preview_year_dir(season, data_root) / "extracted"
    records = (
        [read_preview_json(path) for path in sorted(extracted_dir.glob("round=*.json"))]
        if extracted_dir.exists()
        else []
    )
    keys = [(record.season, record.round) for record in records]
    if len(keys) != len(set(keys)):
        raise PreviewValidationError("season + round must uniquely identify a preview event.")
    records.sort(key=lambda record: (record.season, record.round))
    destination = preview_csv_path(season, data_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(record.to_dict() for record in records)
    temporary.replace(destination)
    return records


def read_preview_csv(
    season: int, data_root: str | Path | None = None
) -> list[dict[str, str]]:
    with preview_csv_path(season, data_root).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class OpenAIPreviewVisionExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        self._model = model or os.environ.get("WODATA_PIRELLI_VISION_MODEL")
        if not self._model:
            raise ValueError(
                "Set --vision-model or WODATA_PIRELLI_VISION_MODEL for new image extraction."
            )
        self._api_key = api_key

    @property
    def model_name(self) -> str:
        return self._model

    def extract(self, image_path: Path, *, season: int, round_number: int) -> Mapping:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI vision extraction requires the optional 'openai' package."
            ) from exc
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        client = OpenAI(api_key=self._api_key)
        response = client.responses.create(
            model=self._model,
            input=[{
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Extract the explicitly printed Pirelli preview values. "
                            f"Season={season} and round={round_number} come from metadata. "
                            "Never guess: use null for ambiguous or absent values. Ignore "
                            "longitudinal/lateral energy heatmaps. Compound values must be C1-C5."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime_type};base64,{encoded}",
                    },
                ],
            }],
            text={"format": {
                "type": "json_schema",
                "name": "pirelli_preview",
                "strict": True,
                "schema": _vision_json_schema(),
            }},
        )
        return json.loads(response.output_text)


def _vision_json_schema() -> dict[str, object]:
    nullable_rating = {"type": ["integer", "null"], "minimum": 1, "maximum": 5}
    nullable_text = {"type": ["string", "null"]}
    nullable_compound = {
        "anyOf": [{"type": "string", "enum": sorted(VALID_COMPOUNDS)}, {"type": "null"}]
    }
    properties = {
        "event": nullable_text,
        "circuit": nullable_text,
        **{name: nullable_rating for name in RATING_FIELDS},
        **{
            name: nullable_compound
            for name in ("hard_compound", "medium_compound", "soft_compound")
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _optional_rating(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise PreviewValidationError(f"{field} must be an integer from 1 to 5 or null.")
    try:
        rating = int(value)
    except (TypeError, ValueError) as exc:
        raise PreviewValidationError(f"{field} must be an integer from 1 to 5 or null.") from exc
    if float(value) != rating or not 1 <= rating <= 5:
        raise PreviewValidationError(f"{field} must be between 1 and 5.")
    return rating


def _optional_compound(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    compound = str(value).strip().upper()
    if compound not in VALID_COMPOUNDS:
        raise PreviewValidationError(f"{field} must be one of {sorted(VALID_COMPOUNDS)}.")
    return compound


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
