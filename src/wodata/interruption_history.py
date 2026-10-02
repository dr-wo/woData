"""Bounded persisted historical SC/Red Flag context; readers never acquire data."""
from __future__ import annotations

import json
import math

from . import get_data_root
from .event_operational import _atomic_write_json, _same_circuit

SCHEMA_VERSION = 1
ROLE = "historical_race_interruptions"


def same_venue(left, right):
    """Reuse operational history's circuit-ID / country-location / circuit policy."""
    return _same_circuit(left, right)


def history_path(season, round_number, data_root=None):
    return (get_data_root(data_root) / "wostrategy" / "interruption_history" / "schema_v1"
            / f"year={int(season)}" / f"round={int(round_number)}" / "history.json")


def unavailable_history(season, round_number, reason):
    return {"schema_version": SCHEMA_VERSION, "semantic_role": ROLE,
            "target": {"season": int(season), "round_number": int(round_number),
                       "event_id": f"{int(season):04d}-{int(round_number):02d}"},
            "status": "unavailable", "reason": str(reason), "races": []}


TRACK_CODES = {"1", "2", "4", "5", "6", "7"}
START_STATUSES = {"observed_transition", "start_transition_unavailable"}
END_STATUSES = {"observed_transition", "ambiguous_transition", "unavailable", "race_end"}


def _time(value, *, required=False):
    if value is None and not required:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid or missing event timestamp")
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _lap(mapping, timestamp):
    if not isinstance(mapping, dict) or mapping.get("status") not in {"approximate", "unavailable"} or mapping.get("method") != "raw_leader_lap_boundaries":
        raise ValueError("Unsupported lap mapping")
    lap = mapping.get("lap")
    if mapping["status"] == "approximate":
        if type(lap) is not int or lap < 1 or timestamp is None:
            raise ValueError("A mapped lap requires a supported timestamp")
    elif lap is not None:
        raise ValueError("Unavailable lap mapping must have a null lap")


def _fragment(fragment, code, race_start, race_end):
    if not isinstance(fragment, dict) or type(fragment.get("source_index")) is not int or fragment["source_index"] < 0:
        raise ValueError("Missing original transition source index")
    observed = _time(fragment.get("first_observed_seconds"), required=True)
    start = _time(fragment.get("start_seconds"))
    end = _time(fragment.get("end_seconds"))
    if race_start is None or observed < race_start or race_end is not None and observed > race_end:
        raise ValueError("Occurrence lacks established race-time admission")
    if fragment.get("start_status") not in START_STATUSES or fragment.get("end_status") not in END_STATUSES:
        raise ValueError("Unsupported transition status")
    if fragment["start_status"] == "observed_transition":
        if start != observed:
            raise ValueError("Observed deployment must retain its original timestamp")
    elif start is not None:
        raise ValueError("Unknown deployment boundary must have a null timestamp")
    _lap(fragment.get("start_lap"), start)
    _lap(fragment.get("end_lap"), end)
    ending = fragment.get("end_track_status")
    if fragment["end_status"] == "observed_transition":
        if end is None or ending not in TRACK_CODES or ending == code:
            raise ValueError("Observed exit requires a trusted different status and timestamp")
    elif fragment["end_status"] == "race_end":
        if race_end is None or end != race_end or ending is not None:
            raise ValueError("Race-end closure must match the official finish")
    elif end is not None or ending is not None:
        raise ValueError("Unknown exit must have null timestamp and status")
    if end is not None and (end < observed or race_end is not None and end > race_end):
        raise ValueError("Exit precedes entry or exceeds race-time coverage")
    uncertain = _time(fragment.get("uncertain_after_seconds"))
    if uncertain is not None and (fragment["end_status"] != "ambiguous_transition" or uncertain < observed or race_end is not None and uncertain > race_end):
        raise ValueError("Invalid unknown-transition boundary")
    start_lap, end_lap = fragment["start_lap"]["lap"], fragment["end_lap"]["lap"]
    # Zero-duration boundary updates must leave unsupported lap context null.
    if start_lap is not None and end_lap is not None and end_lap < start_lap:
        raise ValueError("Inconsistent lap range")
    return observed, fragment["source_index"]


def validate_history(value, season, round_number):
    """Reject contradictory canonical evidence; keep legitimate partial records."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION or value.get("semantic_role") != ROLE:
        raise ValueError("Unsupported historical interruption contract")
    target = value.get("target")
    if not isinstance(target, dict) or (target.get("season"), target.get("round_number")) != (int(season), int(round_number)):
        raise ValueError("Historical interruption target mismatch")
    if value.get("status") not in {"available", "incomplete", "unavailable"}:
        raise ValueError("Historical interruption availability missing")
    races = value.get("races")
    if not isinstance(races, list) or len(races) > 5:
        raise ValueError("History must contain at most five races")
    if races and not isinstance(target.get("event_identity"), dict):
        raise ValueError("Target venue identity missing")
    selection = value.get("selection", {})
    if not isinstance(selection, dict) or not isinstance(selection.get("warnings", []), list) or not all(isinstance(note, str) for note in selection.get("warnings", [])):
        raise ValueError("Selection warnings must be text")
    previous = (int(season), int(round_number))
    for race in races:
        if not isinstance(race, dict):
            raise ValueError("Historical race must be an object")
        key = (race.get("season"), race.get("round_number"))
        if any(type(n) is not int or n <= 0 for n in key) or key >= previous:
            raise ValueError("Historical races must be strictly prior and newest first")
        previous = key
        if race.get("event_id") != f"{key[0]:04d}-{key[1]:02d}" or not same_venue(race.get("event_identity"), target["event_identity"]):
            raise ValueError("Historical venue/identity mismatch")
        provenance = race.get("provenance")
        if not isinstance(provenance, dict) or not all(_text(provenance.get(field)) for field in ("source", "lap_source", "layout_policy")):
            raise ValueError("Required source provenance missing")
        digest = provenance.get("timeline_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Original timeline hash missing")
        notes = provenance.get("source_availability_notes")
        if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
            raise ValueError("Source availability notes missing")
        coverage = race.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("status") not in {"complete", "incomplete", "unavailable"} or not isinstance(coverage.get("reasons"), list) or not all(isinstance(note, str) for note in coverage["reasons"]):
            raise ValueError("Source coverage missing")
        integrity = coverage.get("track_status_integrity")
        if not isinstance(integrity, dict) or integrity.get("status") not in {"unknown", "partial", "verified_complete"}:
            raise ValueError("TrackStatus integrity evidence missing")
        verified = integrity["status"] == "verified_complete" and _text(integrity.get("evidence"))
        if integrity["status"] == "verified_complete" and not verified:
            raise ValueError("Affirmative integrity requires evidence")
        if integrity["status"] == "unknown" and integrity.get("evidence") is not None:
            raise ValueError("Unknown integrity cannot claim affirmative evidence")
        race_start = _time(coverage.get("official_start_seconds"))
        race_end = _time(coverage.get("race_end_seconds"))
        admission = coverage.get("start_admission")
        if not isinstance(admission, dict) or admission.get("status") not in {"established", "ambiguous"} or not _text(admission.get("reason")):
            raise ValueError("Race-start admission evidence missing")
        if admission["status"] == "established":
            proof = _time(admission.get("corroborated_at_seconds"), required=True)
            if race_start is None or proof <= race_start or race_end is not None and proof > race_end or admission.get("evidence") != "non_generated_race_lap_completion":
                raise ValueError("Established start lacks corroboration")
        elif race_start is not None or admission.get("evidence") is not None:
            raise ValueError("Ambiguous original start must remain null")
        if race_end is not None and (race_start is None or race_end <= race_start):
            raise ValueError("Invalid race lifecycle boundary")
        complete = coverage["status"] == "complete"
        if complete and (not verified or race_start is None or race_end is None or coverage["reasons"]):
            raise ValueError("Complete coverage requires affirmative integrity and lifecycle evidence")
        ordered = []
        for name, code in (("safety_car", "4"), ("red_flag", "5")):
            family = race.get(name)
            if not isinstance(family, dict):
                raise ValueError("Interruption family missing")
            events, count, occurred = family.get("occurrences"), family.get("count"), family.get("occurred")
            if not isinstance(events, list) or count is not None and (type(count) is not int or count != len(events)):
                raise ValueError("Invalid exact interruption count")
            if type(family.get("observed_count")) is not int or family["observed_count"] != len(events) or occurred is not None and type(occurred) is not bool:
                raise ValueError("Invalid minimum interruption count")
            if complete != (count is not None):
                raise ValueError("Exact counts require complete coverage")
            expected_state = True if events else False if complete else None
            if occurred is not expected_state:
                raise ValueError("Occurrence/count/state contradiction")
            previous_event = None
            for event in events:
                if not isinstance(event, dict) or type(event.get("sequence")) is not int or event["sequence"] < 1 or event.get("track_status") != code or type(event.get("boundary_ambiguous")) is not bool:
                    raise ValueError("Unsupported occurrence status/order")
                stamp = _fragment(event, code, race_start, race_end)
                fragments = event.get("fragments")
                if not isinstance(fragments, list) or not fragments:
                    raise ValueError("Observed occurrence fragments missing")
                last_stamp = None
                last_fragment = None
                ambiguous = len(fragments) > 1
                for fragment in fragments:
                    current = _fragment(fragment, code, race_start, race_end)
                    if last_stamp is not None and current <= last_stamp:
                        raise ValueError("Fragments are not in chronological source order")
                    if last_fragment is not None:
                        if last_fragment["end_status"] in {"observed_transition", "race_end"} or fragment["start_status"] != "start_transition_unavailable":
                            raise ValueError("Trusted exits/entries cannot be merged as uncertain fragments")
                        if last_fragment.get("uncertain_after_seconds", current[0]) > current[0]:
                            raise ValueError("Unknown fragment boundary follows the next observation")
                    last_stamp = current
                    last_fragment = fragment
                    ambiguous |= fragment["start_status"] != "observed_transition" or fragment["end_status"] == "ambiguous_transition"
                for field in ("source_index", "first_observed_seconds", "start_seconds", "start_status", "start_lap"):
                    if event[field] != fragments[0][field]:
                        raise ValueError("Occurrence entry contradicts its first fragment")
                for field in ("end_seconds", "end_status", "end_lap", "end_track_status"):
                    if event[field] != fragments[-1][field]:
                        raise ValueError("Occurrence exit contradicts its final fragment")
                if ambiguous and not event["boundary_ambiguous"]:
                    raise ValueError("Unknown fragments must retain ambiguity")
                if complete and (event["boundary_ambiguous"] or event["start_status"] != "observed_transition" or event["end_status"] not in {"observed_transition", "race_end"}):
                    raise ValueError("Complete coverage cannot have missing required deployment boundaries")
                exit_proof = event.get("exit_proof")
                if exit_proof is not None:
                    if not isinstance(exit_proof, dict) or exit_proof.get("track_status") not in TRACK_CODES or exit_proof["track_status"] == code or type(exit_proof.get("source_index")) is not int or exit_proof["source_index"] < 0:
                        raise ValueError("Invalid intervening trusted exit")
                    exit_stamp = (_time(exit_proof.get("observed_seconds"), required=True), exit_proof["source_index"])
                    if (exit_stamp <= last_stamp or
                            exit_stamp[0] < last_fragment.get("uncertain_after_seconds", last_stamp[0]) or
                            race_end is not None and exit_stamp[0] > race_end):
                        raise ValueError("Trusted exit contradicts fragment/race timing")
                    if event["end_status"] == "observed_transition" and (exit_proof["track_status"] != event["end_track_status"] or exit_stamp[0] != event["end_seconds"]):
                        raise ValueError("Trusted exit contradicts the termination")
                elif event["end_status"] == "observed_transition":
                    raise ValueError("Observed termination lacks trusted exit proof")
                if previous_event is not None:
                    proof = previous_event.get("exit_proof")
                    if proof is None or (proof["observed_seconds"], proof["source_index"]) >= stamp:
                        raise ValueError("Distinct occurrences require a trusted intervening exit")
                    if event["sequence"] <= previous_event["sequence"]:
                        raise ValueError("Occurrence sequence not chronological")
                previous_event = event
                ordered.append((event["sequence"], stamp, event))
        ordered.sort(key=lambda item: item[0])
        if len({seq for seq, _, _ in ordered}) != len(ordered) or any(ordered[i][1] >= ordered[i + 1][1] for i in range(len(ordered) - 1)):
            raise ValueError("Cross-family occurrence ordering contradicts timestamps/source order")
        for index in range(len(ordered) - 1):
            event, next_stamp = ordered[index][2], ordered[index + 1][1]
            last = event["fragments"][-1]
            if (last["first_observed_seconds"], last["source_index"]) >= next_stamp:
                raise ValueError("A trusted intervening occurrence forbids merging later fragments")
            if event["end_seconds"] is not None and event["end_seconds"] > next_stamp[0]:
                raise ValueError("Known active intervals overlap distinct interruptions")
            proof = event.get("exit_proof")
            if proof is not None and (proof["observed_seconds"], proof["source_index"]) > next_stamp:
                raise ValueError("Intervening exit proof is later than the next occurrence")
    return value


def write_history(value, data_root=None):
    target = value["target"]
    validate_history(value, target["season"], target["round_number"])
    path = history_path(target["season"], target["round_number"], data_root)
    _atomic_write_json(path, value)
    return path


def load_history(season, round_number, data_root=None):
    """Local-only read, with safe explicit degradation for legacy/missing records."""
    try:
        path = history_path(season, round_number, data_root)
        value = json.loads(path.read_text(encoding="utf-8"))
        return validate_history(value, season, round_number)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return unavailable_history(season, round_number, f"History unavailable: {exc}")


def schedule_races(schedule, season):
    """Adapt FastF1 schedule rows without display-name matching."""
    records = []
    for _, row in schedule.iterrows():
        number = int(row["RoundNumber"])
        if number < 1:
            continue
        identity = {key: str(row[source]) for key, source in (
            ("country", "Country"), ("location", "Location"), ("event_name", "EventName"),
            ("official_event_name", "OfficialEventName")) if source in row and row[source] is not None and str(row[source]) not in {"nan", "NaT", "<NA>"}}
        import pandas as pd
        race_start, precision = None, None
        for column in ("Session5DateUtc", "Session5Date", "EventDate"):
            timestamp = pd.to_datetime(row.get(column), utc=True, errors="coerce")
            if pd.notna(timestamp):
                precision = "date" if column == "EventDate" else "instant"
                race_start = timestamp.isoformat()
                break
        records.append({"season": int(season), "round_number": number,
                        "event_id": f"{int(season):04d}-{number:02d}",
                        "event_identity": identity,
                        "race_start": race_start, "race_start_precision": precision,
                        "identity_source": "fastf1_event_schedule"})
    return records


def load_race_source(season, round_number, *, data_root=None, session_loader=None):
    """Acquire original ordered transitions and raw session-time lap boundaries.

    Called only by the explicit preparation command. FastF1's parser cache is
    reused; no telemetry, lap-status reduction or racing-time normalization.
    """
    import fastf1
    from . import get_fastf1_raw_cache_dir
    if session_loader is None:
        cache = get_fastf1_raw_cache_dir(data_root)
        cache.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(cache))
        session = fastf1.get_session(int(season), int(round_number), "R")
        session.load(laps=True, telemetry=False, weather=False, messages=True)
    else:
        session = session_loader(int(season), int(round_number), "R")
    unavailable = []

    def available(name):
        try:
            return getattr(session, name)
        except Exception as exc:
            unavailable.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    timing_stream = getattr(session, "timing_stream", None)
    if session_loader is None:
        try:
            # Session.laps.Position is ranked from remaining completion times.
            # Retain the independent reported Position/leader-lap stream too.
            from fastf1 import _api
            _, timing_stream = _api.timing_data(session.api_path)
        except Exception as exc:
            unavailable.append(f"timing_stream: {type(exc).__name__}: {exc}")
    elif timing_stream is None:
        unavailable.append("timing_stream: original leader-lap evidence unavailable")

    return {
        "track_status": available("track_status"),
        "session_status": available("session_status"),
        "laps": available("laps"),
        "timing_stream": timing_stream,
        "official_start": available("session_start_time"),
        "race_control_messages": available("race_control_messages"),
        "t0_date": available("t0_date"),
        "source": "fastf1_original_track_status",
        "source_availability_notes": unavailable,
        "coverage_complete": None,
        "track_status_integrity": {
            "status": "unknown", "evidence": None,
            "assessment": "FastF1 parser-version cache and HTTP success do not attest TrackStatus continuity/completeness",
        },
    }
