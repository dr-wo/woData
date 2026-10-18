from __future__ import annotations

import copy
import math
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from .models import LiveTimingMessage, PublicationTime
from .quali_models import (
    QualiCheckpoint, QualiPitState, QualiRawDriverState, QualiRawSnapshot,
    QualiTyreStint, QualiCompletedLap, QualiCompletedLapKey,
    QualiLapEvidence, QualiPositionObservation, QualiSpeedObservation, QualiTrafficEvidence,
)
from .replay.schema import parse_timestamp
from .quali_clock import (DEFAULT_SESSION_DURATIONS_S, STANDARD_QUALI_PARTS, SPRINT_QUALI_PARTS, countdown_seconds)
from .reducer import (
    _as_bool, _as_float, _as_int, _decode_compressed_payload, _deep_merge,
    _lap_time_seconds, _sample_time_seconds, _sector_time_updates,
)


@dataclass(frozen=True)
class PitLaneGeometry:
    """Circuit-specific pit-lane centreline in Position.z coordinates."""

    centreline: tuple[tuple[float, float], ...]
    maximum_distance: float = 400.0

    def project(self, x: float, y: float) -> tuple[float, float] | None:
        if len(self.centreline) < 2:
            return None
        lengths = []
        total = 0.0
        for first, second in zip(self.centreline, self.centreline[1:]):
            length = math.hypot(second[0] - first[0], second[1] - first[1])
            lengths.append(length)
            total += length
        if total <= 0:
            return None
        best: tuple[float, float] | None = None
        travelled = 0.0
        for (ax, ay), (bx, by), length in zip(
            self.centreline, self.centreline[1:], lengths
        ):
            if length <= 0:
                continue
            dx, dy = bx - ax, by - ay
            fraction = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length**2))
            px, py = ax + fraction * dx, ay + fraction * dy
            distance = math.hypot(x - px, y - py)
            progress = (travelled + fraction * length) / total
            if best is None or distance < best[1]:
                best = (progress, distance)
            travelled += length
        return best if best is not None and best[1] <= self.maximum_distance else None


@dataclass(eq=False)
class _ProjectionSectorCycle:
    """One observed sector cycle; an accepted owner is assigned at most once."""

    session_part: str
    started_at: PublicationTime | None
    sectors: dict[int, float] = field(default_factory=dict)
    event_key: QualiCompletedLapKey | None = None
    closed: bool = False
    requires_anchor: bool = False


@dataclass(frozen=True)
class _RaceControlEpoch:
    point: PublicationTime
    green: PublicationTime | None
    closed: PublicationTime | None
    end: float
    green_end: float | None
    closed_end: float | None
    # Known boundaries with no comparable source date remain evidence. Their
    # indices identify events, but do not manufacture an occurrence order.
    unresolved: frozenset[tuple[str, str]]


class QualiSessionStore:
    """Qualifying-only raw reducer; strategy/projection remains in woPlanner."""

    def __init__(
        self, *, pit_lane_geometry: PitLaneGeometry | None = None,
        rpm_threshold: float = 500.0, telemetry_freshness_s: float = 5.0,
        pit_moving_speed_kph: float = 1.0, pit_exit_speed_kph: float = 100.0,
    ) -> None:
        self.pit_lane_geometry = pit_lane_geometry
        self.rpm_threshold = float(rpm_threshold)
        self.telemetry_freshness_s = float(telemetry_freshness_s)
        self.pit_moving_speed_kph = float(pit_moving_speed_kph)
        self.pit_exit_speed_kph = float(pit_exit_speed_kph)
        self.reset()

    def reset(self) -> None:
        self._session_info: dict[str, object] = {}
        self._session_status: str | None = None
        self._session_part = "Q1"
        self._part_established = False
        self._part_started_at: PublicationTime | None = None
        self._race_control_messages: dict[str, dict] = {}
        self._race_control_source_times: dict[str, PublicationTime] = {}
        self._race_control_flags: set[tuple[str, PublicationTime, str, float]] = set()
        self._race_control_order: dict[tuple[str, PublicationTime, str, float], int] = {}
        self._race_control_owners: dict[tuple[str, str], str] = {}
        self._race_control_provisional: set[tuple[str, PublicationTime, str, float]] = set()
        self._race_control_publications: dict[tuple[str, str], PublicationTime | None] = {}
        self._race_control_epoch_cache_key: tuple | None = None
        self._race_control_epochs: dict[tuple[str, str], _RaceControlEpoch] = {}
        self._race_control_undated: set[tuple[str, str]] = set()
        self._race_control_ambiguities: set[tuple[str, str]] = set()
        # Qualifying part labels remain bounded by the three source parts.
        # Generic activity must never advance these identification ranges.
        self._part_identifications: dict[str, dict[str, tuple[float, float]]] = {}
        self._part_epoch_transitions: dict[str, PublicationTime] = {}
        self._part_inferred_epochs: dict[str, PublicationTime] = {}
        self._part_epoch_preparations: dict[str, PublicationTime] = {}
        self._undated_part_identifications: set[str] = set()
        self._epoch_activity: dict[str, float] = {}
        self._race_control_floor: dict[str, float] = {}
        self._active_clock_at: PublicationTime | None = None
        self._race_control_finish_owned = False
        self._part_activity_at: PublicationTime | None = None
        self._clock_publications: dict[str, float] = {}
        self._last_clock_publication: PublicationTime | None = None
        self._clock_coordinate_offset_s: float | None = None
        self._clock_coordinate_mapping_conflicted = False
        self._countdown_publications: dict[str, float] = {}
        self._countdown_anchor: PublicationTime | None = None
        self._declared_clock_remaining: float | None = None
        self._remaining_time_s: float | None = None
        self._clock_running = False
        self._clock_anchor_session_time_s: float | None = None
        self._drivers: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self._tyre_stints: dict[str, dict[int, dict]] = {}
        self._telemetry: dict[str, dict] = {}
        self._speed_history: dict[str, list[QualiSpeedObservation]] = {}
        self._speed_history_snapshot: dict[str, tuple[QualiSpeedObservation, ...]] = {}
        self._speed_observations: tuple[QualiSpeedObservation, ...] = ()
        self._speed_history_anchors: dict[str | None, float] = {}
        self._speed_coordinate_offset_s: float | None = None
        self._speed_coordinate_mapping_conflicted = False
        self._speed_receipt_ordinals: dict[tuple[str, str | None, float], int] = {}
        self._speed_receipt_ordinal = 0
        self._receive_monotonic_s: float | None = None
        self._message_source: str | None = None
        self._traffic_clock_time_s: float | None = None
        self._traffic_clock_kind: str | None = None
        self._receive_time_trusted = False
        self._position: dict[str, dict] = {}
        self._last_message_at: datetime | None = None
        self._latest_session_time_s: float | None = None
        self._normal_finished = False
        self._normal_finish_source: str | None = None
        self._segment_started = False
        # A replay may start with a stale zero-countdown packet.  A genuine
        # segment must first have supplied a positive countdown before zero is
        # allowed to establish a clock-based normal finish.
        self._segment_has_positive_countdown = False
        self._segment_duration_s: float | None = None
        self._history: dict[str, dict[str, float | None]] = {}
        self._historical_best_s: float | None = None
        self._completed_lap_sequence: list[QualiCompletedLap] = []
        self._lap_evidence_sequence: list[QualiLapEvidence] = []
        self._position_observations: list[QualiPositionObservation] = []
        self._traffic_evidence_sequence: list[QualiTrafficEvidence] = []
        self._segment_start_time_s: float | None = None
        self._utc_offset_s: float | None = None
        # These ledgers survive part changes and retractions. Timing's active
        # cycle may be cleared, but redelivery must retain its original owner.
        self._projection_sector_packets: dict[tuple, _ProjectionSectorCycle] = {}
        self._projection_sector_values: dict[tuple, set[_ProjectionSectorCycle]] = {}
        self._projection_completion_sources: dict[tuple, set[_ProjectionSectorCycle]] = {}
        self._projection_part_started_at: PublicationTime | None = None
        self._projection_part_changed = False
        self._snapshot = self._build_snapshot()

    def apply(self, message: LiveTimingMessage) -> QualiRawSnapshot:
        self._apply_message(message)
        return self.snapshot()

    def apply_many(self, messages: list[LiveTimingMessage]) -> QualiRawSnapshot:
        for message in messages:
            self._apply_message(message)
        return self.snapshot()

    def _apply_message(self, message: LiveTimingMessage) -> None:
        # Clock.Utc dates subscription snapshots too. Arrival and replay clocks
        # cannot certify freshness. Reject redelivery before advancing any state.
        clock_publication = None
        clock_is_newer = False
        if message.topic == "ExtrapolatedClock" and isinstance(message.payload, Mapping):
            clock_utc = parse_timestamp(message.payload.get("Utc"))
            clock_publication = (
                PublicationTime("utc", clock_utc.timestamp())
                if clock_utc is not None else message.publication_time
            )
            if clock_publication is not None:
                # Ordinary clock comparison has its own bridge. Do not change
                # acquisition-coordinate mapping or telemetry history here.
                offset = self._clock_coordinate_offset_s
                if offset is None and not self._clock_coordinate_mapping_conflicted:
                    offset = self._utc_offset_s
                paired_utc = message.source_timestamp_utc or clock_utc
                proposed = (paired_utc.timestamp() - message.session_time_seconds
                            if paired_utc is not None and message.session_time_seconds is not None
                            else None)
                conflicted = self._clock_coordinate_mapping_conflicted
                if proposed is not None:
                    if offset is not None and not math.isclose(offset, proposed, rel_tol=0, abs_tol=.001):
                        conflicted = True
                    elif not conflicted:
                        offset = proposed
                if conflicted:
                    offset = None

                def comparable(time: PublicationTime) -> PublicationTime:
                    if offset is not None and time.coordinate == "session":
                        # Compare at UTC magnitude: subtracting an epoch offset
                        # can make an identical fractional session time newer.
                        return PublicationTime("utc", time.seconds + offset)
                    return time

                incoming = comparable(clock_publication)
                for coordinate, seconds in self._clock_publications.items():
                    previous = comparable(PublicationTime(coordinate, seconds))
                    if incoming.coordinate == previous.coordinate and incoming.seconds <= previous.seconds:
                        return
                # A same-domain watermark alone cannot establish chronology
                # relative to the countdown last accepted in another domain.
                last = self._last_clock_publication
                if last is not None:
                    last = comparable(last)
                    clock_is_newer = (last.coordinate == incoming.coordinate
                                      and incoming.seconds > last.seconds)
                self._clock_coordinate_mapping_conflicted = conflicted
                self._clock_coordinate_offset_s = offset
                self._last_clock_publication = clock_publication
                self._clock_publications[clock_publication.coordinate] = clock_publication.seconds
        # Preserve the ordinary countdown's legacy arrival anchor for
        # undated subscription registers. Arrival never certifies freshness
        # or event ownership; those use the original publication bridge above.
        countdown_publication = message.publication_time or PublicationTime(
            "utc", message.received_at_utc.timestamp())
        if countdown_publication is not None:
            coordinate = countdown_publication.coordinate
            self._countdown_publications[coordinate] = max(
                countdown_publication.seconds,
                self._countdown_publications.get(coordinate, -math.inf))
        self._position_observations = []
        self._speed_observations = ()
        self._last_message_at = message.received_at_utc
        self._message_source = message.source
        receipt_monotonic = getattr(message, "received_at_monotonic_s", None)
        self._receive_monotonic_s = (
            float(receipt_monotonic)
            if receipt_monotonic is not None and math.isfinite(float(receipt_monotonic)) else None
        )
        declared_trust = getattr(message, "received_at_is_trusted", None)
        self._receive_time_trusted = (
            bool(declared_trust) if declared_trust is not None else message.source == "signalr_live"
        )
        if self._receive_time_trusted:
            clock = message.received_at_utc.timestamp()
            clock_kind = "receive"
        else:
            clock = (message.playback_time_seconds if message.playback_time_seconds is not None
                     else message.session_time_seconds)
            clock_kind = "playback"
        self._traffic_clock_time_s = (
            float(clock) if clock is not None and math.isfinite(float(clock)) else None
        )
        self._traffic_clock_kind = clock_kind if self._traffic_clock_time_s is not None else None
        if self._traffic_clock_time_s is None:
            self._receive_time_trusted = False
        candidate_time = message.session_time_seconds
        if candidate_time is None:
            candidate_time = (message.source_timestamp_utc or message.received_at_utc).timestamp()
        if candidate_time is not None:
            self._latest_session_time_s = max(
                float(candidate_time), self._latest_session_time_s or -math.inf
            )
        payload = message.payload
        if message.topic == "SessionInfo" and isinstance(payload, Mapping):
            _deep_merge(self._session_info, payload)
            declared_part = _explicit_session_part(payload, self._session_part)
            if declared_part is not None:
                self._accept_part(declared_part, message.publication_time)
            else:
                self._set_part(_session_part(payload, self._session_part),
                               message.publication_time, bootstrap=not self._part_established)
        elif message.topic == "SessionStatus" and isinstance(payload, Mapping):
            preparing = (
                self._part_established and not self._segment_started
                and not self._clock_running
                and self._declared_clock_remaining == DEFAULT_SESSION_DURATIONS_S[self._session_part]
            )
            previous_part_end = (
                preparing and message.publication_time is None
                and str(payload.get("Status", "")).lower() in {"finished", "finalised", "ends"}
            )
            if payload.get("Status") is not None and not previous_part_end:
                self._freeze_clock()
                self._session_status = str(payload["Status"])
                self._clock_running = self._session_status.lower() in {"started", "active"}
                if self._clock_running:
                    self._segment_started = True
                    self._part_activity_at = message.publication_time
                    self._record_epoch_activity(message.publication_time)
                    self._reconcile_race_control_ownership()
                elif self._session_status.lower() in {"finished", "finalised", "ends"}:
                    self._normal_finished = True
                    self._normal_finish_source = "session_status"
        elif message.topic == "ExtrapolatedClock" and isinstance(payload, Mapping):
            remaining = countdown_seconds(payload.get("Remaining"))
            if remaining is not None:
                declared_remaining = remaining
                clock_utc = parse_timestamp(payload.get("Utc"))
                if message.session_time_seconds is not None and message.source_timestamp_utc is None and clock_utc is not None:
                    # Archive outer timestamps are session-relative; telemetry is UTC.
                    self._utc_offset_s = clock_utc.timestamp() - candidate_time
                    self._align_speed_history_offset(self._utc_offset_s)
                if message.source == "signalr_live":
                    clock_utc = parse_timestamp(payload.get("Utc"))
                    observed_utc = (
                        message.source_timestamp_utc or message.received_at_utc
                    )
                    if clock_utc is not None:
                        remaining = max(
                            0.0,
                            remaining - max(
                                0.0, (observed_utc - clock_utc).total_seconds()
                            ),
                        )
                previous_remaining = self._effective_remaining_time_s()
                if (
                    clock_is_newer
                    and self._segment_started
                    and previous_remaining is not None
                    and previous_remaining <= 5.0
                    and remaining > previous_remaining + 60.0
                ):
                    self._set_part({
                        "Q1": "Q2",
                        "Q2": "Q3",
                        "SQ1": "SQ2",
                        "SQ2": "SQ3",
                    }.get(self._session_part, self._session_part), message.publication_time,
                                   epoch_identification=clock_publication)
                # A remaining-time value is never sufficient evidence for a
                # segment: Q1 naturally passes 15:00.  Segment changes need an
                # explicit feed part or the observed zero-to-reset transition
                # above.
                if not self._segment_started and declared_remaining > 0:
                    self._segment_duration_s = declared_remaining
                if declared_remaining > 0:
                    self._part_activity_at = clock_publication
                    self._record_epoch_activity(clock_publication)
                    if (self._part_established and not self._segment_started
                            and _as_bool(payload.get("Extrapolating")) is False
                            and clock_publication is not None):
                        self._part_epoch_preparations[self._session_part] = clock_publication
                    self._segment_has_positive_countdown = True
                    if self._active_clock_at is None and clock_publication is not None:
                        # Establish ownership once, including a paused pre-start
                        # countdown. Ordinary progression cannot revoke a finish.
                        self._active_clock_at = clock_publication
                        self._apply_race_control({}, None, establish_boundary=True)
                    self._reconcile_race_control_ownership()
                self._declared_clock_remaining = declared_remaining
                self._remaining_time_s = remaining
                self._clock_anchor_session_time_s = candidate_time
                self._countdown_anchor = (
                    PublicationTime("utc", observed_utc.timestamp())
                    if message.source == "signalr_live" and clock_utc is not None
                    else countdown_publication
                )
            if payload.get("Extrapolating") is not None:
                if remaining is None:
                    self._freeze_clock()
                self._clock_running = bool(_as_bool(payload.get("Extrapolating")))
                self._segment_started |= self._clock_running
                if (
                    self._clock_running and self._segment_start_time_s is None
                    and declared_remaining is not None
                    and declared_remaining >= DEFAULT_SESSION_DURATIONS_S[self._session_part] - 1
                ):
                    # A first observation mid-session is not the segment start.
                    self._segment_start_time_s = candidate_time
        elif message.topic == "RaceControlMessages" and isinstance(payload, Mapping):
            self._apply_race_control(payload, message.publication_time)
        elif message.topic == "DriverList" and isinstance(payload, Mapping):
            lines = payload.get("Lines", payload)
            if isinstance(lines, Mapping):
                for number, patch in lines.items():
                    if isinstance(patch, Mapping):
                        _deep_merge(self._drivers.setdefault(str(number), {}), patch)
        elif message.topic in {"TimingData", "TimingStats"} and isinstance(payload, Mapping):
            declared_part = _explicit_session_part(payload, self._session_part)
            # SessionPart is a top-level state register, not a per-driver lap
            # occurrence. Repeated values must not reset clocks or finish state.
            if declared_part is None or self._accept_part(declared_part, message.publication_time):
                self._apply_timing(payload, message.publication_time)
            if message.topic == "TimingData" and payload.get("Lines"):
                self._part_established = True
        elif message.topic == "TimingAppData" and isinstance(payload, Mapping):
            self._apply_timing_app_data(payload)
        elif message.topic == "CarData.z":
            self._apply_car_data(message)
        elif message.topic == "Position.z":
            self._apply_position(message)
        if (
            self._segment_started
            # Live late joins retain their established finish semantics.  The
            # positive-countdown bootstrap guard is only for archive replay.
            and (message.source != "f1_archive_replay"
                 or self._segment_has_positive_countdown)
            and self._effective_remaining_time_s() == 0
        ):
            self._normal_finished = True
            self._normal_finish_source = self._normal_finish_source or "clock_zero"
        # All inference and latches are advanced once per ordered message.
        self._snapshot = self._build_snapshot()

    def _apply_timing(self, payload: Mapping, publication_time: PublicationTime | None) -> None:
        lines = payload.get("Lines", payload)
        if not isinstance(lines, Mapping):
            return
        for number, patch in lines.items():
            if not isinstance(patch, Mapping):
                continue
            timing = self._timing.setdefault(str(number), {})
            previous_best = (
                _lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                or (None if timing.get("_official_best") else timing.get("_best_seen_s"))
            )
            previous_counter_value = _as_int(timing.get("NumberOfLaps")) or 0
            previous_in_pit = _as_bool(timing.get("InPit"))
            _deep_merge(timing, patch)
            # BestLapTimes is indexed by qualifying part. Late joins need the
            # completed-part context, without inventing completed lap events.
            bests = timing.get("BestLapTimes", {})
            if isinstance(bests, Mapping):
                entries = bests.items()
            else:
                entries = enumerate(bests) if isinstance(bests, list) else ()
            for index, best in entries:
                part_number = _as_int(index)
                if (part_number is not None
                        and 0 <= part_number < int(self._session_part[-1]) - 1
                        and isinstance(best, Mapping) and "Value" in best):
                    prefix = "SQ" if self._session_part.startswith("SQ") else "Q"
                    self._history.setdefault(str(number), {})[
                        f"{prefix}{part_number + 1}"
                    ] = _lap_time_seconds(best)
            current_in_pit = _as_bool(timing.get("InPit"))
            if "InPit" in patch and current_in_pit is not None:
                self._traffic_evidence_sequence.append(QualiTrafficEvidence(
                    str(number), "off_track" if current_in_pit else "on_track",
                    publication_time,
                ))
            if current_in_pit is True and previous_in_pit is False:
                timing["_pit_arriving"] = True
                timing["_pit_entry_pending"] = True
                timing.pop("_pit_queue", None)
                timing["_pit_boundary_s"] = self._latest_session_time_s
                timing["_outbound_exit_confirmed"] = False
                timing["_out_lap_boundary_seen"] = False
                timing.pop("_confirmed_lap_number", None)
                self._record_lap_evidence(number, "pit_entry", None)
            elif current_in_pit is False and previous_in_pit is True:
                timing.pop("_pit_arriving", None)
                timing["_outbound"] = True
                timing["_pit_boundary_s"] = self._latest_session_time_s
                self._record_lap_evidence(number, "pit_exit", None)
            if "BestLapTime" in patch or "BestLap" in patch:
                timing["_official_best"] = True
                best_patch = patch.get("BestLapTime", patch.get("BestLap"))
                best = _lap_time_seconds(best_patch)
                if best is not None:
                    timing["_segment_best_s"] = best
                elif not isinstance(best_patch, Mapping) or "Value" in best_patch:
                    # This register belongs only to completed-part history.
                    # A next-part TimingData clear can precede the clock reset;
                    # legacy feeds also clear both lap fields after chequered.
                    # A best-only deletion is a corrected unavailable result,
                    # not a reason to resurrect the last nonempty best.
                    part_number = _as_int(payload.get("SessionPart"))
                    declared_part = _explicit_session_part(payload)
                    next_part_clear = (
                        part_number is not None and part_number > int(self._session_part[-1])
                    ) or (
                        declared_part not in (None, self._session_part)
                    )
                    last_patch = patch.get("LastLapTime")
                    # Explicit current-part scope remains a result correction
                    # after chequered. Only unscoped legacy packets may infer
                    # a rollover from simultaneous empty lap fields.
                    legacy_part_clear = (
                        part_number is None and declared_part is None
                        and self._normal_finished and isinstance(last_patch, Mapping)
                        and last_patch.get("Value") == ""
                    )
                    if not (next_part_clear or legacy_part_clear):
                        timing["_segment_best_s"] = None
            last = _lap_time_seconds(patch.get("LastLapTime"))
            if last is not None:
                self._record_lap_evidence(number, "last_lap_publication", _as_int(patch.get("NumberOfLaps")), last)
            lap_number = _as_int(timing.get("NumberOfLaps")) or 0
            previous_counter = timing.get("_last_recorded_lap", 0)
            counter_advanced = "NumberOfLaps" in patch and lap_number > previous_counter
            if counter_advanced:
                timing["_last_recorded_lap"] = lap_number
                timing["_counter_boundary_s"] = self._latest_session_time_s
                timing.pop("_sector_lap_counter", None)
                self._record_lap_evidence(number, "lap_counter", lap_number)
                self._traffic_evidence_sequence.append(QualiTrafficEvidence(
                    str(number), "lap_counter", publication_time, lap_number,
                ))
                if timing.get("_outbound_exit_confirmed"):
                    timing["_out_lap_boundary_seen"] = True
            if "Sectors" in patch:
                sector_updates = _sector_time_updates(patch["Sectors"])
                minimum_elapsed = min(sector_updates.values(), default=None)
                boundary_time = timing.get("_counter_boundary_s")
                sector_can_belong_to_current_lap = bool(sector_updates) and (
                    boundary_time is None
                    or (
                        self._latest_session_time_s is not None
                        and self._latest_session_time_s - boundary_time >= minimum_elapsed - 1.0
                    )
                )
                if not counter_advanced and sector_can_belong_to_current_lap:
                    timing["_sector_lap_counter"] = lap_number
                self._record_lap_evidence(number, "sector_update", lap_number)
            completed_lap = False
            rejection = None
            if counter_advanced and last is not None:
                inferred_start = (
                    self._latest_session_time_s - last
                    if self._latest_session_time_s is not None else None
                )
                boundary = timing.get("_pit_boundary_s")
                if inferred_start is None:
                    rejection = "missing_event_time"
                elif self._segment_start_time_s is not None and inferred_start < self._segment_start_time_s - 1:
                    rejection = "cross_segment_interval"
                elif boundary is not None and inferred_start <= boundary <= self._latest_session_time_s:
                    rejection = "pit_boundary_inside_interval"
                elif current_in_pit is True or timing.get("_pit_arriving"):
                    rejection = "pit_in_interval"
                else:
                    completed_lap = True
            elif counter_advanced:
                rejection = "counter_without_same_update_lap_time"
            elif last is not None:
                rejection = "lap_time_without_same_update_counter"
            if completed_lap:
                completed_lap = True
                timing["_segment_laps"] = timing.get("_segment_laps", 0) + 1
                timing["_confirmed_lap_number"] = lap_number
                self._completed_lap_sequence.append(QualiCompletedLap(
                    self._session_part, str(number), lap_number, last,
                    self._latest_session_time_s,
                ))
                self._traffic_evidence_sequence.append(QualiTrafficEvidence(
                    str(number), "completion", publication_time, lap_number,
                    self._completed_lap_sequence[-1].event_key,
                ))
            if rejection is not None:
                self._record_lap_evidence(number, "unassociated_lap", lap_number, last, rejection)
            # TimingData may carry an untimed out/in lap as LastLapTime after
            # the official segment result has been cleared.  Such a lap is
            # not a qualifying result; retain only plausible push-lap times
            # when no explicit official best is present.
            plausible_unofficial = (
                last is not None
                and last <= 240.0
                and (
                    self._historical_best_s is None
                    or last <= self._historical_best_s * 1.25
                )
            )
            if completed_lap and plausible_unofficial and not timing.get("_official_best"):
                previous = timing.get("_best_seen_s")
                timing["_best_seen_s"] = last if previous is None else min(float(previous), last)
            self._associate_projection_evidence(
                str(number), timing, patch, counter_advanced, completed_lap,
                previous_best, previous_counter_value, publication_time,
            )

    def _associate_sector_cycle(
        self, number, timing, patch, counter_advanced, completed_lap,
        previous_counter_value, publication_time,
    ) -> None:
        updates = _sector_time_updates(patch.get("Sectors"), include_clears=True)
        values = {index: value for index, value in updates.items() if value is not None}
        # A value signature is not an occurrence ID. Preserve the publication
        # coordinate (including for clears); repeated values in a later observed
        # cycle must not resolve to the first occurrence of those values.
        signature = (number, publication_time, tuple(sorted(updates.items())))
        cycle = timing.get("_projection_sector_cycle")
        completion_cycle = None
        if publication_time is None:
            # Source-unknown snapshots may update live registers/checkpoints,
            # but cannot attach historical sectors. A clear/boundary retires a
            # prospective cycle without losing its last genuine source bound.
            if (any(value is None for value in updates.values()) or counter_advanced
                    or (_as_int(timing.get("NumberOfLaps")) or 0) < previous_counter_value):
                if cycle is not None:
                    cycle.closed = True
                timing["_projection_sector_cycle"] = _ProjectionSectorCycle(
                    self._session_part, cycle.started_at if cycle is not None else None,
                    closed=True, requires_anchor=True)
            return
        packet_cycle = (self._projection_sector_packets.get(signature)
                        if updates and publication_time is not None else None)
        if packet_cycle is None and values and publication_time is not None:
            source_owners = self._projection_completion_sources.get((number, publication_time), set())
            if len(source_owners) == 1:
                # The original publication timestamp identifies the accepted
                # source packet, even after the reducer clock/part has advanced.
                packet_cycle = next(iter(source_owners))
            elif source_owners:
                packet_cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)
        # Never compare session-relative seconds with UTC seconds. An unknown
        # transition boundary cannot authorize cross-part historical evidence.
        bounds = [self._projection_part_started_at, cycle.started_at if cycle else None]
        stale = any(bound is not None and (
            publication_time.coordinate != bound.coordinate or publication_time.seconds < bound.seconds
        ) for bound in bounds)
        stale |= self._projection_part_changed and self._projection_part_started_at is None
        if packet_cycle is None and stale:
            packet_cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)

        rejected_boundary = not completed_lap and (
            counter_advanced or (_as_int(timing.get("NumberOfLaps")) or 0) < previous_counter_value
        )
        if rejected_boundary and not stale:
            # Lifecycle work is independent of packet ownership. A historical
            # duplicate in this update cannot keep the rejected lap eligible.
            if cycle is not None and cycle.event_key is None:
                cycle.closed = True
            cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)
            timing["_projection_sector_cycle"] = cycle
            if packet_cycle is None:
                packet_cycle = cycle

        if packet_cycle is None:
            # Resets open a fresh prospective cycle; they never clear durations
            # already owned by an accepted event.
            clears = {index for index, value in updates.items() if value is None}
            if clears:
                if cycle is not None and cycle.event_key is None:
                    cycle.closed = True
                cycle = _ProjectionSectorCycle(self._session_part, publication_time)
            timing["_projection_sector_cycle"] = cycle

            # An observed open cycle provides causal context for repeated
            # durations. Without it, equality only establishes ambiguity.
            combined = (cycle.sectors | values) if cycle is not None else {}
            continuation = (
                cycle is not None and not cycle.closed
                and (cycle.event_key is None or (bool(cycle.sectors) and 0 not in values))
                and bool(combined) and set(combined) == set(range(max(combined) + 1))
                and not any(index in cycle.sectors and cycle.sectors[index] != value
                            for index, value in values.items())
            )
            distinct_completion = completed_lap and not stale and not self._projection_completion_sources.get(
                (number, publication_time))
            if values and not continuation and not distinct_completion:
                matches = [self._projection_sector_values.get((number, index, value), set())
                           for index, value in values.items()]
                if set.intersection(*matches) or (not completed_lap and any(matches)):
                    packet_cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)

        if packet_cycle is None:
            if completed_lap:
                if (cycle is None or cycle.event_key is not None or cycle.closed
                        or (_as_int(timing.get("NumberOfLaps")) or 0) != previous_counter_value + 1
                        or any(index in cycle.sectors and cycle.sectors[index] != value
                               for index, value in values.items())):
                    if cycle is not None and cycle.event_key is None:
                        cycle.closed = True
                    cycle = _ProjectionSectorCycle(self._session_part, publication_time)
                packet_cycle = cycle
                completion_cycle = cycle
            elif values:
                if ((cycle is None and self._projection_part_started_at is None)
                        or (cycle is not None and cycle.closed and not cycle.requires_anchor)) and 0 in values:
                    cycle = _ProjectionSectorCycle(self._session_part, publication_time)
                # Only an observed contiguous continuation can enrich an open
                # cycle. A new/mismatched value without a reset is unresolved.
                combined = (cycle.sectors | values) if cycle is not None else {}
                contiguous = bool(combined) and set(combined) == set(range(max(combined) + 1))
                conflicting = cycle is not None and any(
                    index in cycle.sectors and cycle.sectors[index] != value
                    for index, value in values.items())
                continuation = cycle is not None and not cycle.closed and (
                    cycle.event_key is None or (bool(cycle.sectors) and 0 not in values))
                if continuation and contiguous and not conflicting:
                    packet_cycle = cycle
                else:
                    packet_cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)
            timing["_projection_sector_cycle"] = cycle

        if values:
            # Even an original publication match cannot overwrite conflicting
            # evidence already attached to its accepted event.
            if any(index in packet_cycle.sectors and packet_cycle.sectors[index] != value
                   for index, value in values.items()):
                packet_cycle = _ProjectionSectorCycle(self._session_part, publication_time, closed=True)
            packet_cycle.sectors.update(values)
            for index, value in values.items():
                self._projection_sector_values.setdefault((number, index, value), set()).add(packet_cycle)
        if updates and publication_time is not None:
            self._projection_sector_packets.setdefault(signature, packet_cycle or cycle)

        if completed_lap and not stale:
            # Bind the prospective cycle once to this exact accepted event.
            # A duplicate packet's older cycle is never rebound to this event.
            event = self._completed_lap_sequence[-1]
            cycle = timing.get("_projection_sector_cycle")
            if completion_cycle is not None:
                cycle = completion_cycle
            elif (cycle is None or cycle.event_key is not None or cycle.closed
                  or event.lap_number != previous_counter_value + 1):
                if cycle is not None and cycle.event_key is None:
                    cycle.closed = True
                cycle = _ProjectionSectorCycle(self._session_part, publication_time)
            cycle.event_key = event.event_key
            timing["_projection_sector_cycle"] = cycle
            if publication_time is not None:
                self._projection_completion_sources.setdefault((number, publication_time), set()).add(cycle)
        for owned_cycle in {cycle, packet_cycle} - {None}:
            if owned_cycle.event_key is None:
                continue
            for index, event in enumerate(self._completed_lap_sequence):
                if event.event_key == owned_cycle.event_key:
                    sectors = tuple(owned_cycle.sectors.get(index) for index in range(3))
                    if sectors != event.sectors_s:
                        self._completed_lap_sequence[index] = replace(event, sectors_s=sectors)
                    break

    def _associate_projection_evidence(
        self, number, timing, patch, counter_advanced, completed_lap,
        previous_best, previous_counter_value, publication_time,
    ) -> None:
        """Attach durations to accepted events using their observed boundary.

        NumberOfLaps labels a boundary, not an accepted-completion count or
        BestLapTime.Lap. Per-sector provenance is recorded before merging
        durations into an event; carried-over sectors never acquire a new ID.
        This bookkeeping does not change operational lap recognition.
        """
        counter = _as_int(timing.get("NumberOfLaps")) or 0
        prior_source_owners = set(self._projection_completion_sources.get((number, publication_time), set()))
        self._associate_sector_cycle(number, timing, patch, counter_advanced, completed_lap,
                                     previous_counter_value, publication_time)

        best_payload = timing.get("BestLapTime") or timing.get("BestLap")
        best = _lap_time_seconds(best_payload)
        if not timing.get("_official_best"):
            best = timing.get("_best_seen_s")
        best_patch = patch.get("BestLapTime", patch.get("BestLap"))
        official_lap = timing.get("_projection_official_lap")
        if isinstance(best_patch, Mapping) and "Lap" in best_patch:
            official_lap = _as_int(best_patch["Lap"])
        elif best != previous_best:
            # Deep-merged old Lap metadata does not identify a new value.
            official_lap = None
        value_changed = best != previous_best
        alias_changed = (
            isinstance(best_patch, Mapping) and "Lap" in best_patch
            and official_lap != timing.get("_projection_official_lap")
        )
        key = timing.get("_projection_best_event_key")
        invalid_keys = timing.setdefault("_projection_invalid_event_keys", set())
        previous_invalid_keys = set(invalid_keys)
        if counter < previous_counter_value:
            invalid_keys.update(event.event_key for event in self._completed_lap_sequence
                                if event.session_part == self._session_part
                                and event.driver_number == number and event.lap_number > counter)
        if ("LastLapTime" in patch and "NumberOfLaps" in patch and not completed_lap
                and _as_bool(timing.get("InPit")) is not True):
            last = _lap_time_seconds(timing.get("LastLapTime"))
            # LastLapTime also publishes untimed pit/out laps without an
            # accepted boundary. Only an explicit same-boundary correction
            # arriving before another lap can complete retracts this event.
            invalid_keys.update(event.event_key for event in self._completed_lap_sequence
                                if event.session_part == self._session_part
                                and event.driver_number == number and event.lap_number == counter
                                and event.session_time_s is not None
                                and self._latest_session_time_s - event.session_time_s < event.lap_time_s - 1.0
                                and (last is None or not math.isclose(event.lap_time_s, last, abs_tol=.01, rel_tol=0)))
        # Export only explicit traversal invalidations, never best-register
        # changes. The ledger survives part changes just like accepted events.
        for invalid_key in sorted(invalid_keys - previous_invalid_keys):
            self._traffic_evidence_sequence.append(QualiTrafficEvidence(
                str(number), "completion_invalidated", publication_time,
                invalid_key[2], invalid_key,
            ))
        if best is None or value_changed or counter < previous_counter_value:
            key = None
        if key in invalid_keys:
            key = None

        driver_events = [event for event in self._completed_lap_sequence
                         if event.session_part == self._session_part and event.driver_number == number]
        candidates = [event for event in driver_events if event.event_key not in invalid_keys]
        published_best = isinstance(best_patch, Mapping) and ("Value" in best_patch or "Lap" in best_patch)
        # A snapshot's receive time/sequence cannot establish an alias or a
        # historical best. Existing uncontradicted references remain available.
        if publication_time is None:
            if "BestLapTime" in patch or "BestLap" in patch:
                # Current unproven best metadata invalidates the recovery token,
                # even when the value is unchanged. Historical aliases remain
                # on their events for a later positively identified publication.
                key = None
                timing["_projection_official_lap"] = None
            timing["_projection_best_event_key"] = key
            return
        source_owners = (prior_source_owners if completed_lap else
                         self._projection_completion_sources.get((number, publication_time), set()))
        if published_best and source_owners:
            # Resolve positive ORIGINAL-publication evidence before every alias
            # path, including established-owner enrichment and known-alias
            # recovery. Retraction removes eligibility, never historical origin.
            source_keys = {cycle.event_key for cycle in source_owners}
            identified = next((event for event in self._completed_lap_sequence
                               if event.driver_number == number and source_keys == {event.event_key}), None)
            explicit_alias = _as_int(best_patch.get("Lap")) if "Lap" in best_patch else None
            compatible = identified is not None and best is not None and math.isclose(
                identified.lap_time_s, best, abs_tol=.01, rel_tol=0)
            if compatible and explicit_alias is not None:
                compatible = identified.official_lap_number in (None, explicit_alias) and not any(
                    event.event_key != identified.event_key and event.official_lap_number == explicit_alias
                    for event in self._completed_lap_sequence
                    if event.driver_number == number and event.session_part == identified.session_part)
            if compatible:
                if explicit_alias is not None and identified.official_lap_number is None:
                    index = self._completed_lap_sequence.index(identified)
                    self._completed_lap_sequence[index] = replace(identified, official_lap_number=explicit_alias)
                if key is None and identified in candidates:
                    key = identified.event_key
                if key == identified.event_key:
                    timing["_projection_official_lap"] = explicit_alias or identified.official_lap_number
            timing["_projection_best_event_key"] = key
            return
        # An official feed alias already attached to an accepted event can
        # identify a corrected/reverted best without using duration as a join.
        matched = [event for event in driver_events if official_lap is not None
                   and event.official_lap_number == official_lap]
        owner = next((event for event in candidates if event.event_key == key), None)
        if owner is not None and alias_changed:
            if (owner.official_lap_number not in (None, official_lap)
                    or any(event.event_key != key for event in matched)):
                key = None
            elif official_lap is not None:
                index = self._completed_lap_sequence.index(owner)
                self._completed_lap_sequence[index] = replace(owner, official_lap_number=official_lap)
        if key is None and best is not None and matched:
            if (len(matched) == 1 and matched[0].event_key not in invalid_keys
                    and math.isclose(matched[0].lap_time_s, best, abs_tol=.01, rel_tol=0)):
                key = matched[0].event_key
        if (key is None and best is not None and candidates and not matched
                and counter >= previous_counter_value):
            # First association needs an accepted publication, not selection
            # of the latest (or only still-valid) equal-time event.
            published_best = isinstance(best_patch, Mapping) and "Value" in best_patch
            # Retraction removes reference eligibility, not the occurrence
            # from the evidence history. Unknown aliases on equal-time events
            # remain competing explanations, including at a new completion.
            equal_time = [event for event in driver_events
                          if math.isclose(event.lap_time_s, best, abs_tol=.01, rel_tol=0)]
            source_owners = self._projection_completion_sources.get((number, publication_time), set())
            source_keys = {cycle.event_key for cycle in source_owners}
            same_publication = completed_lap and (published_best or not timing.get("_official_best"))
            identified = self._completed_lap_sequence[-1] if same_publication else next(
                (event for event in candidates if published_best and source_keys == {event.event_key}), None)
            competing = [event for event in equal_time if event != identified
                         and (official_lap is None or event.official_lap_number in (None, official_lap))]
            original_publication = identified is not None and source_keys == {identified.event_key}
            # A new completion's own timestamp is not independent evidence
            # that carried/delayed equal-time best metadata belongs to it.
            associated = not competing or (not completed_lap and original_publication)
            if (identified is not None and (same_publication or original_publication)
                    and associated
                    and identified.official_lap_number in (None, official_lap)
                    and math.isclose(identified.lap_time_s, best, abs_tol=.01, rel_tol=0)):
                key = identified.event_key
                if official_lap is not None and identified.official_lap_number is None:
                    index = self._completed_lap_sequence.index(identified)
                    self._completed_lap_sequence[index] = replace(identified, official_lap_number=official_lap)
        timing["_projection_best_event_key"] = key
        timing["_projection_official_lap"] = official_lap

    def _record_lap_evidence(
        self, number: object, kind: str, lap_number: int | None,
        lap_time_s: float | None = None, reason: str | None = None,
    ) -> None:
        self._lap_evidence_sequence.append(QualiLapEvidence(
            self._session_part, str(number), kind, lap_number,
            self._latest_session_time_s, lap_time_s, reason,
        ))

    def _apply_car_data(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        entries = payload.get("Entries", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(entries, list):
            return
        if message.source_timestamp_utc is not None and message.session_time_seconds is not None:
            self._align_speed_history_offset(
                message.source_timestamp_utc.timestamp() - message.session_time_seconds
            )
        self._advance_speed_history_anchor(
            self._speed_publication_time(message), self._sample_coordinate(None, message)
        )
        observations: dict[tuple[str, str | None, float], QualiSpeedObservation] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            sample_time = self._sample_time(entry.get("Utc"), message)
            cars = entry.get("Cars", {})
            if sample_time is None or not isinstance(cars, Mapping):
                continue
            for number, car in cars.items():
                channels = car.get("Channels", {}) if isinstance(car, Mapping) else {}
                if not isinstance(channels, Mapping):
                    continue
                self._telemetry[str(number)] = {
                    "time": sample_time, "rpm": _as_float(channels.get("0")),
                    "speed": _as_float(channels.get("2")), "gear": _as_int(channels.get("3")),
                }
                speed = _as_float(channels.get("2"))
                if not math.isfinite(sample_time) or speed is None or not math.isfinite(speed) or speed < 0:
                    continue
                number = str(number)
                observation = QualiSpeedObservation(
                    number, sample_time, speed,
                    message.received_at_utc if self._receive_time_trusted else None,
                    self._sample_coordinate(entry.get("Utc"), message),
                    self._receive_monotonic_s,
                )
                if self._retain_speed(observation):
                    observations.setdefault(self._speed_identity(observation), observation)
        self._prune_speed_history()
        # Restrict current-message diagnostics as well as retained history.
        batch: dict[str, list[QualiSpeedObservation]] = {}
        for observation in observations.values():
            batch.setdefault(observation.driver_number, []).append(observation)
        self._speed_observations = tuple(
            observation
            for number in sorted(batch)[:64]
            for observation in sorted(batch[number], key=self._speed_order)[-512:]
        )
        self._speed_history_snapshot = {
            number: tuple(samples) for number, samples in self._speed_history.items()
        }

    @staticmethod
    def _speed_order(sample: QualiSpeedObservation) -> tuple[str, float]:
        return (sample.acquisition_coordinate or "", sample.session_time_s)

    @staticmethod
    def _speed_identity(sample: QualiSpeedObservation) -> tuple[str, str | None, float]:
        return (sample.driver_number, sample.acquisition_coordinate, sample.session_time_s)

    def _refresh_speed_history_snapshot(self) -> None:
        self._speed_history_snapshot = {
            number: tuple(samples) for number, samples in self._speed_history.items()
        }
        retained = {self._speed_identity(sample)
                    for samples in self._speed_history.values() for sample in samples}
        self._speed_receipt_ordinals = {
            key: ordinal for key, ordinal in self._speed_receipt_ordinals.items() if key in retained
        }

    def _retain_speed(self, observation: QualiSpeedObservation) -> bool:
        """Bound all domains together by receipt order, each domain's age separately.

        A UTC epoch never advances a session-coordinate retention floor. First
        reception wins within (driver, coordinate, timestamp), and malformed
        future rows cannot expire or consume the budget of causal samples.
        """
        coordinate = observation.acquisition_coordinate
        sample_time = observation.session_time_s
        anchor = self._speed_history_anchors.get(coordinate)
        horizon = max(20.0, self.telemetry_freshness_s + 4.0)
        floor = anchor - horizon if anchor is not None else -math.inf
        samples = self._speed_history.get(observation.driver_number)
        order = self._speed_order(observation)
        index = bisect_left(samples, order, key=self._speed_order) if samples else 0
        if samples and index < len(samples) and self._speed_order(samples[index]) == order:
            return False
        if sample_time < floor:
            # Surface newly received old samples for bounded delay diagnostics.
            return True
        if samples is None:
            if len(self._speed_history) >= 64:
                oldest = min(self._speed_history, key=lambda number: max(
                    self._speed_receipt_ordinals[self._speed_identity(sample)]
                    for sample in self._speed_history[number]))
                del self._speed_history[oldest]
            samples = self._speed_history.setdefault(observation.driver_number, [])
        self._speed_receipt_ordinal += 1
        self._speed_receipt_ordinals[self._speed_identity(observation)] = self._speed_receipt_ordinal
        samples.insert(index, observation)
        if len(samples) > 512:
            future = [sample for sample in samples
                      if (sample_anchor := self._speed_history_anchors.get(sample.acquisition_coordinate))
                      is not None and sample.session_time_s > sample_anchor]
            if future:
                # Discard excess future evidence by receipt order, never by
                # unlike numerical timestamps.
                victim = max(future, key=lambda sample:
                             self._speed_receipt_ordinals[self._speed_identity(sample)])
            else:
                victim = min(samples, key=lambda sample:
                             self._speed_receipt_ordinals[self._speed_identity(sample)])
            samples.remove(victim)
        return True

    def _speed_publication_time(self, message: LiveTimingMessage) -> float | None:
        # With explicit domains the envelope may advance its own coordinate
        # without claiming to date any differently coordinated payload UTC.
        return self._sample_time(None, message)

    def _align_speed_history_offset(self, offset_s: float) -> None:
        """Expose a verified bridge without rewriting either history domain.

        Conflicting independent mappings make conversion unavailable until
        reset. Keep the original samples and per-domain anchors intact: later
        evidence must never make an earlier inferred conversion irreversible.
        Local envelope conversions and ordinary qualifying clocks keep their
        existing behaviour.
        """
        if not math.isfinite(offset_s) or self._speed_coordinate_mapping_conflicted:
            return
        previous = self._speed_coordinate_offset_s
        if previous is not None and not math.isclose(previous, offset_s, rel_tol=0, abs_tol=.001):
            self._speed_coordinate_mapping_conflicted = True
            self._speed_coordinate_offset_s = None
            return
        self._speed_coordinate_offset_s = offset_s

    def _advance_speed_history_anchor(self, time_s: float | None, coordinate: str | None) -> None:
        if time_s is None or not math.isfinite(time_s) or coordinate is None:
            return
        previous = self._speed_history_anchors.get(coordinate)
        self._speed_history_anchors[coordinate] = max(
            time_s, previous if previous is not None else -math.inf)

    def _prune_speed_history(self) -> None:
        horizon = max(20.0, self.telemetry_freshness_s + 4.0)
        for number, samples in list(self._speed_history.items()):
            samples[:] = [sample for sample in samples
                          if (anchor := self._speed_history_anchors.get(sample.acquisition_coordinate))
                          is None or sample.session_time_s >= anchor - horizon]
            if not samples:
                del self._speed_history[number]
        self._refresh_speed_history_snapshot()

    def _apply_timing_app_data(self, payload: Mapping) -> None:
        lines = payload.get("Lines", payload)
        if not isinstance(lines, Mapping):
            return
        for number, driver_patch in lines.items():
            if not isinstance(driver_patch, Mapping):
                continue
            updates = driver_patch.get("Stints")
            if isinstance(updates, list):
                updates = {str(index): value for index, value in enumerate(updates)}
            if not isinstance(updates, Mapping):
                continue
            number = str(number)
            stints = self._tyre_stints.setdefault(number, {})
            for raw_index, stint_patch in updates.items():
                if not isinstance(stint_patch, Mapping):
                    continue
                index = _as_int(raw_index)
                if index is None or index < 0:
                    continue
                _deep_merge(stints.setdefault(index, {}), stint_patch)

    def _tyre_history(self, number: str) -> tuple[QualiTyreStint, ...]:
        history = []
        for stint_index, stint in sorted(self._tyre_stints.get(number, {}).items()):
            start_laps = _as_int(stint.get("StartLaps"))
            if start_laps is None:
                continue
            compound = str(stint.get("Compound") or "UNKNOWN").upper()
            history.append(
                QualiTyreStint(
                    compound,
                    start_laps,
                    _as_bool(stint.get("New")),
                    stint_index,
                )
            )
        return tuple(history)

    def _apply_position(self, message: LiveTimingMessage) -> None:
        payload = _decode_compressed_payload(message.payload)
        samples = payload.get("Position", ()) if isinstance(payload, Mapping) else ()
        if not isinstance(samples, list):
            return
        if message.source_timestamp_utc is not None and message.session_time_seconds is not None:
            self._align_speed_history_offset(
                message.source_timestamp_utc.timestamp() - message.session_time_seconds
            )
        publication_time = self._speed_publication_time(message)
        self._advance_speed_history_anchor(publication_time, self._sample_coordinate(None, message))
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            sample_time = self._sample_time(sample.get("Timestamp"), message)
            entries = sample.get("Entries", {})
            if sample_time is None or not isinstance(entries, Mapping):
                continue
            sample_coordinate = self._sample_coordinate(sample.get("Timestamp"), message)
            if (sample.get("Timestamp") is not None
                    and (publication_time is None
                         or sample_coordinate != self._sample_coordinate(None, message))):
                # Position can independently advance its acquisition domain even
                # if the envelope uses a different, unmapped source coordinate.
                self._advance_speed_history_anchor(sample_time, sample_coordinate)
            for number, value in entries.items():
                if not isinstance(value, Mapping):
                    continue
                x, y = _as_float(value.get("X")), _as_float(value.get("Y"))
                if x is None or y is None:
                    continue
                projected = self.pit_lane_geometry.project(x, y) if self.pit_lane_geometry else None
                self._position_observations.append(QualiPositionObservation(
                    str(number), sample_time,
                    str(value.get("Status")) if value.get("Status") is not None else None,
                    projected is not None if self.pit_lane_geometry else None,
                    acquisition_coordinate=self._sample_coordinate(sample.get("Timestamp"), message),
                    received_at_monotonic_s=self._receive_monotonic_s,
                ))
                self._position[str(number)] = {
                    "time": sample_time, "x": x, "y": y, "status": value.get("Status"),
                    "pit_progress": projected[0] if projected else None,
                    "pit_distance": projected[1] if projected else None,
                }
        self._prune_speed_history()
        self._speed_history_snapshot = {
            number: tuple(samples) for number, samples in self._speed_history.items()
        }

    def _sample_time(self, value, message) -> float | None:
        if self._utc_offset_s is not None and message.source_timestamp_utc is None:
            timestamp = parse_timestamp(value)
            if timestamp is not None:
                return timestamp.timestamp() - self._utc_offset_s
        return _sample_time_seconds(value, message)

    def _sample_coordinate(self, value, message) -> str | None:
        """Describe the conversion actually used by _sample_time.

        An archive envelope's session time alone does not map a payload UTC
        timestamp. Only its independent offset or paired source UTC can do so.
        """
        if parse_timestamp(value) is not None:
            if (self._utc_offset_s is not None and message.source_timestamp_utc is None
                    or message.session_time_seconds is not None and message.source_timestamp_utc is not None):
                return "session"
            return "utc"
        if message.session_time_seconds is not None and math.isfinite(message.session_time_seconds):
            return "session"
        if message.source_timestamp_utc is not None:
            return "utc"
        return None

    def snapshot(self) -> QualiRawSnapshot:
        """Read the last message state; polling never advances domain time."""
        return self._snapshot

    def _build_snapshot(self) -> QualiRawSnapshot:
        now_s = self._latest_session_time_s
        sampled_now = _latest_sample_time(self._telemetry, self._position)
        if sampled_now is not None:
            now_s = max(now_s or -math.inf, sampled_now)
        states = []
        # Position packets can contain numeric placeholders for cars absent
        # from this event. Only identity or timing evidence creates a timing
        # row; telemetry/position can enrich an already identified car.
        numbers = set(self._drivers) | set(self._timing)
        for number in numbers:
            driver, timing = self._drivers.get(number, {}), self._timing.get(number, {})
            telemetry, position = self._telemetry.get(number, {}), self._position.get(number, {})
            in_pit = _as_bool(timing.get("InPit"))
            stopped = _as_bool(timing.get("Stopped"))
            position_fresh = (
                now_s is not None and position.get("time") is not None
                and 0 <= now_s - position["time"] <= self.telemetry_freshness_s
            )
            progress = position.get("pit_progress") if position_fresh else None
            speed = telemetry.get("speed")
            fresh_rpm = (
                telemetry.get("rpm") is not None and now_s is not None
                and now_s - telemetry.get("time", -math.inf) <= self.telemetry_freshness_s
                and telemetry["rpm"] > self.rpm_threshold
            )
            fresh_speed = (
                speed is not None and now_s is not None
                and now_s - telemetry.get("time", -math.inf) <= self.telemetry_freshness_s
            )
            moving_in_pit = fresh_speed and speed > self.pit_moving_speed_kph
            departed_by_speed = (
                in_pit is False
                and fresh_speed
                and speed >= self.pit_exit_speed_kph
            )
            departed_by_position = (
                in_pit is False
                and self.pit_lane_geometry is not None
                and position_fresh
                and progress is None
            )
            if departed_by_speed or departed_by_position:
                if timing.get("_outbound") or (in_pit is False and timing.get("_pit_context")):
                    timing["_outbound_exit_confirmed"] = True
                timing["_outbound"] = False
            if in_pit is True:
                timing["_pit_context"] = True
            status = str(position.get("status", "")).lower() if position_fresh else ""
            arriving = bool(timing.get("_pit_arriving"))
            inbound_outside_pit = arriving and (
                (
                    self.pit_lane_geometry is not None
                    and position_fresh
                    and progress is None
                )
                or (
                    self.pit_lane_geometry is None
                    and fresh_speed
                    and speed >= self.pit_exit_speed_kph
                )
            )
            pit_side = in_pit is True or timing.get("_outbound") or arriving or progress is not None
            if timing.pop("_pit_entry_pending", False) or inbound_outside_pit:
                pit_state = QualiPitState.IN_LAP
            elif arriving:
                # An inbound stop may be a weighbridge/hot-swap stop.  No
                # pit-box geometry means it remains pit-lane transit.
                pit_state = QualiPitState.PIT_LANE
            elif timing.get("_outbound") or (pit_side and moving_in_pit):
                if fresh_speed and speed == 0 and timing.get("_outbound"):
                    pit_state = QualiPitState.QUEUE
                else:
                    pit_state = QualiPitState.PIT_LANE
            elif pit_side and fresh_speed and speed == 0 and timing.get("_outbound"):
                pit_state = QualiPitState.QUEUE
            elif pit_side and fresh_rpm:
                pit_state = QualiPitState.FIRE_UP
            elif pit_side:
                pit_state = QualiPitState.NOT_ACTIVE
            elif status in {"ontrack", "0"} or in_pit is False:
                pit_state = QualiPitState.ON_TRACK
            else:
                pit_state = QualiPitState.UNKNOWN
            sectors = [None, None, None]
            for index, value in _sector_time_updates(timing.get("Sectors", {})).items():
                sectors[index] = value
            segments = _segments(timing.get("Sectors", {}))
            states.append(QualiRawDriverState(
                number, str(driver.get("Tla") or driver.get("BroadcastName") or number),
                str(driver.get("TeamName") or driver.get("Team") or ""),
                _as_int(timing.get("Position")), timing.get("_segment_laps", 0),
                _lap_time_seconds(timing.get("LastLapTime")),
                _lap_time_seconds(timing.get("BestLapTime") or timing.get("BestLap"))
                or (None if timing.get("_official_best") else timing.get("_best_seen_s")),
                tuple(sectors), segments, in_pit, stopped, pit_state, progress,
                telemetry.get("rpm"), telemetry.get("speed"), telemetry.get("gear"),
                telemetry.get("time"), position.get("time"), self._tyre_history(number),
                tuple(self._history.get(number, {}).items()),
                bool(pit_state == QualiPitState.ON_TRACK and (
                    timing.get("_confirmed_lap_number") == (_as_int(timing.get("NumberOfLaps")) or 0)
                    or (
                        timing.get("_out_lap_boundary_seen")
                        and timing.get("_sector_lap_counter") == (_as_int(timing.get("NumberOfLaps")) or 0)
                        and any(value is not None for value in sectors)
                    ) or (
                        not timing.get("_outbound_exit_confirmed") and
                        (_as_int(timing.get("NumberOfLaps")) or 0) > 0 and
                        (
                            timing.get("_sector_lap_counter") == (_as_int(timing.get("NumberOfLaps")) or 0)
                            and any(value is not None for value in sectors)
                        )
                    )
                )),
                (_as_int(timing.get("NumberOfLaps")) or 0) + 1,
                pit_state,
                bool(timing.get("_outbound_exit_confirmed") and not timing.get("_out_lap_boundary_seen")),
                best_lap_event_key=timing.get("_projection_best_event_key"),
                knocked_out=_as_bool(timing.get("KnockedOut")),
            ))
        states.sort(key=lambda value: (value.position is None, value.position or 999, value.driver_code))
        return QualiRawSnapshot(
            MappingProxyType(copy.deepcopy(self._session_info)), self._session_status,
            self._session_part, self._effective_remaining_time_s(), self._clock_running,
            tuple(states), self._last_message_at,
            self._normal_finished, self._normal_finish_source,
            self._segment_duration_s or DEFAULT_SESSION_DURATIONS_S[self._session_part],
            tuple(self._completed_lap_sequence),
            self._clock_anchor_session_time_s,
            tuple(self._lap_evidence_sequence),
            tuple(self._position_observations),
            tuple(self._traffic_evidence_sequence),
            session_part_established=self._part_established,
            speed_history=MappingProxyType(self._speed_history_snapshot),
            speed_observations=self._speed_observations,
            speed_freshness_s=self.telemetry_freshness_s,
            message_source=self._message_source,
            traffic_clock_time_s=self._traffic_clock_time_s,
            traffic_clock_kind=self._traffic_clock_kind,
            receive_time_trusted=self._receive_time_trusted,
            receive_monotonic_s=self._receive_monotonic_s,
            speed_coordinate_offset_s=self._speed_coordinate_offset_s,
        )

    def _effective_remaining_time_s(self) -> float | None:
        remaining = self._remaining_time_s
        if (
            remaining is None
            or not self._clock_running
        ):
            return remaining
        anchor = self._countdown_anchor
        if anchor is None:
            return remaining
        now = self._countdown_time(anchor)
        return max(0.0, remaining - max(0, math.floor(now - anchor.seconds)))

    def _countdown_time(self, anchor: PublicationTime) -> float:
        now = max(anchor.seconds, self._countdown_publications.get(anchor.coordinate, anchor.seconds))
        offset = self._clock_coordinate_offset_s
        if offset is not None and not self._clock_coordinate_mapping_conflicted:
            other = "utc" if anchor.coordinate == "session" else "session"
            seconds = self._countdown_publications.get(other)
            if seconds is not None:
                now = max(now, seconds - offset if other == "utc" else seconds + offset)
        return now

    def _freeze_clock(self) -> None:
        effective = self._effective_remaining_time_s()
        if effective is not None:
            self._remaining_time_s = effective
        if self._countdown_anchor is not None:
            self._countdown_anchor = PublicationTime(
                self._countdown_anchor.coordinate, self._countdown_time(self._countdown_anchor))
        self._clock_anchor_session_time_s = self._latest_session_time_s

    def _accept_part(self, part: str, publication_time: PublicationTime | None) -> bool:
        # Qualifying parts only advance within one store lifetime. A delayed
        # earlier-part packet must not restore its timing registers either.
        if int(part[-1]) < int(self._session_part[-1]):
            return False
        self._set_part(part, publication_time, bootstrap=not self._part_established)
        self._part_established = True
        self._record_part_identification(part, publication_time)
        self._reconcile_race_control_ownership()
        return True

    def _record_part_identification(self, part: str, time: PublicationTime | None) -> None:
        if time is None:
            if part in self._part_identifications:
                return
            # A current subscription register can be paired with its dated
            # clock register. Receipt time never supplies this source date.
            time = self._active_clock_at
            if time is None:
                self._undated_part_identifications.add(part)
                return
        self._undated_part_identifications.discard(part)
        if part in self._part_inferred_epochs and part not in self._part_identifications:
            self._part_epoch_transitions[part] = time
        values = self._part_identifications.setdefault(part, {})
        low, high = values.get(time.coordinate, (time.seconds, time.seconds))
        values[time.coordinate] = (min(low, time.seconds), max(high, time.seconds))
        if not self._segment_started and not self._clock_running and self._active_clock_at is not None:
            self._part_epoch_preparations[part] = self._active_clock_at

    def _record_epoch_activity(self, time: PublicationTime | None) -> None:
        if time is None:
            return
        self._epoch_activity[time.coordinate] = max(
            time.seconds, self._epoch_activity.get(time.coordinate, -math.inf))
        if self._session_part in self._undated_part_identifications:
            self._record_part_identification(self._session_part, time)

    def _flag_time(self, time: PublicationTime) -> PublicationTime:
        if time.coordinate == "utc" and self._utc_offset_s is not None:
            return PublicationTime("session", time.seconds - self._utc_offset_s)
        return time

    def _race_control_item_time(self, index: str) -> tuple[PublicationTime, float] | None:
        stamp = self._race_control_messages.get(index, {}).get("Utc")
        utc = parse_timestamp(stamp)
        if utc is None:
            return None
        # The item's current own-Utc and precision supersede legacy envelope
        # dates and coarser variants of this same unowned source identity.
        fraction = str(stamp).split(".", 1)[1] if "." in str(stamp) else ""
        digits = len(fraction.split("Z")[0].split("+")[0].split("-")[0])
        return PublicationTime("utc", utc.timestamp()), 10.0 ** -digits if digits else 1.0

    def _race_control_event_time(self, event: tuple) -> PublicationTime | None:
        index, time, flag, precision = event
        owner = self._race_control_owners.get((index, flag))
        if owner is not None and owner != self._session_part:
            return None
        if owner is None:
            item_time = self._race_control_item_time(index)
            if item_time is not None and (time, precision) != item_time:
                return None
            if (index, flag) in self._race_control_ambiguities:
                return None
        time = self._flag_time(time)
        if owner == self._session_part:
            return time
        if flag == "GREEN":
            # Retaining an owned finish also retains its later restart order.
            # A first clock after the restart must not hide only the GREEN and
            # resurrect the older finish that bypasses the bootstrap floor.
            for previous in self._race_control_flags:
                if self._race_control_owners.get((previous[0], previous[2])) == self._session_part:
                    finish = self._flag_time(previous[1])
                    if finish.coordinate == time.coordinate and finish.seconds <= time.seconds:
                        return time
        floor = self._race_control_floor.get(time.coordinate, -math.inf)
        for boundary in (self._part_started_at, self._active_clock_at):
            if boundary is not None:
                boundary = self._flag_time(boundary)
                if boundary.coordinate == time.coordinate:
                    floor = max(floor, boundary.seconds)
        if time.seconds < floor and time.seconds + precision <= floor:
            return None
        return time

    def _race_control_epoch_facts(self) -> dict[tuple[str, str], _RaceControlEpoch]:
        key = (len(self._race_control_flags), self._utc_offset_s)
        if self._race_control_epoch_cache_key == key:
            return self._race_control_epochs
        # Enriched item Utc supersedes the legacy envelope date for unowned
        # evidence. Certified identity, however, is never replaced.
        events: dict[tuple[str, str], tuple] = {}
        for event in self._race_control_flags:
            identity = (event[0], event[2])
            item_time = self._race_control_item_time(event[0])
            if item_time is not None and (event[1], event[3]) != item_time:
                continue
            events[identity] = event
        ordered: dict[str, list[tuple]] = {}
        for identity, event in events.items():
            time = self._flag_time(event[1])
            start = time.seconds
            end = time.seconds + event[3]
            publication = self._race_control_publications.get(identity)
            if publication is not None:
                publication = self._flag_time(publication)
                # A source publication inside the printed precision interval
                # supplies a source-order witness at a subsecond boundary.
                if (publication.coordinate == time.coordinate
                        and time.seconds <= publication.seconds < time.seconds + event[3]):
                    time = publication
                    end = time.seconds
            order = int(event[0]) if event[0].isdecimal() else self._race_control_order[event]
            ordered.setdefault(time.coordinate, []).append((start, order, identity, time, end))
        facts = {}
        for coordinate, entries in ordered.items():
            unresolved = frozenset(self._race_control_undated | {
                identity for other, values in ordered.items() if other != coordinate
                for _, _, identity, _, _ in values
            })
            green = None
            green_end = None
            closed = None
            closed_end = None
            for start, _, identity, time, end in sorted(entries):
                if identity[1] == "GREEN":
                    green = time
                    green_end = max(green_end if green_end is not None else -math.inf, end)
                else:
                    # Sorting printed lower bounds does not order overlapping
                    # precision intervals. Such a boundary may precede the
                    # finish after enrichment, even if sorted after it today.
                    # Equal printed times retain the existing numeric source
                    # sequence witness; ingress order is never such a proof.
                    overlapping = frozenset(
                        other for other_start, _, other, other_time, other_end in entries
                        if other != identity
                        and (other_start != start or not (
                            other[0].isdecimal() and identity[0].isdecimal()))
                        and other_time.seconds < end and other_end > time.seconds
                    )
                    facts[identity] = _RaceControlEpoch(
                        time, green, closed, end, green_end, closed_end,
                        unresolved | overlapping)
                    closed = time
                    # Earlier overlapping closures may end after the one
                    # with the greatest printed start. Keep the whole frontier.
                    closed_end = max(closed_end if closed_end is not None else -math.inf, end)
        self._race_control_epoch_cache_key = key
        self._race_control_epochs = facts
        return facts

    def _certify_race_control_owner(self, event: tuple) -> None:
        if event[2] != "CHEQUERED":
            return
        identity = (event[0], event[2])
        if identity in self._race_control_owners:
            self._race_control_provisional.discard(event)
            return
        fact = self._race_control_epoch_facts().get(identity)
        if fact is None:
            self._race_control_provisional.add(event)
            return
        point, green, closed = fact.point, fact.green, fact.closed
        end, green_end = fact.end, fact.green_end
        lower = closed.seconds if closed is not None else -math.inf
        closed_end = fact.closed_end if fact.closed_end is not None else -math.inf
        # Certification must survive every placement of already-known undated
        # boundaries. A GREEN may follow any pre-finish label; a closure may
        # follow any transition/preparation. These are proof bounds, not dates
        # assigned to those events. Source spans remain independent evidence.
        if any(flag == "GREEN" for _, flag in fact.unresolved):
            green_end = end
        if any(flag == "CHEQUERED" for _, flag in fact.unresolved):
            lower = point.seconds
            closed_end = max(closed_end, end)

        def after_closure(seconds: float) -> bool:
            # Printed precision intervals are half-open. A witness at their
            # endpoint is after the event; an exact simultaneous one is not.
            return seconds > lower and seconds >= closed_end

        ranges: dict[str, tuple[float, float]] = {}
        for part, coordinates in self._part_identifications.items():
            for coordinate, (low, high) in coordinates.items():
                first = self._flag_time(PublicationTime(coordinate, low))
                last = self._flag_time(PublicationTime(coordinate, high))
                if first.coordinate == point.coordinate:
                    a, b = ranges.get(part, (math.inf, -math.inf))
                    ranges[part] = (min(a, first.seconds), max(b, last.seconds))
        # Undated coarse history crossing a known part observation/boundary
        # cannot be made precise by receipt order or a current display label.
        if any(point.seconds < first < end for first, _ in ranges.values()):
            self._race_control_ambiguities.add(identity)
            self._race_control_provisional.add(event)
            return
        self._race_control_ambiguities.discard(identity)
        activity = -math.inf
        for coordinate, seconds in self._epoch_activity.items():
            observed = self._flag_time(PublicationTime(coordinate, seconds))
            if observed.coordinate == point.coordinate:
                activity = max(activity, observed.seconds)
        authoritative = set(ranges)
        for part, witness in self._part_inferred_epochs.items():
            witness = self._flag_time(witness)
            if witness.coordinate == point.coordinate and part not in ranges:
                ranges[part] = (witness.seconds, witness.seconds)
        candidates = []
        for part, (first, last) in ranges.items():
            source_first = first
            inferred = self._part_inferred_epochs.get(part)
            if inferred is not None:
                inferred = self._flag_time(inferred)
                if inferred.coordinate == point.coordinate:
                    first = min(first, inferred.seconds)
            upper = min((a for p, (a, _) in ranges.items()
                         if p in authoritative and int(p[-1]) > int(part[-1])), default=math.inf)
            if (point.seconds >= upper
                    or (point.seconds < first and end <= first)):
                continue
            if any(int(p[-1]) > int(part[-1])
                   and self._flag_time(w).coordinate == point.coordinate
                   and self._flag_time(w).seconds <= point.seconds
                   for p, w in self._part_inferred_epochs.items()):
                continue
            # A later part bounds this candidate, but does not identify every
            # preceding GREEN epoch. That still needs a scoped positive witness.
            bounded = upper < math.inf
            source_span = first <= point.seconds and end <= last
            # A label within a coarse GREEN's interval may still precede it.
            # An original publication inside that interval already narrows its
            # source-order witness in _race_control_epoch_facts().
            identified_in_epoch = after_closure(last) and (green_end is None or last >= green_end)
            reaffirmed = source_first < last and identified_in_epoch
            # A same-part label in the closed-to-GREEN gap may still describe
            # the completed part. Only an actual transition or preparation can
            # identify the following epoch from before its GREEN.
            epoch_witness = False
            for witness in (self._part_epoch_transitions.get(part),
                            self._part_epoch_preparations.get(part)):
                if witness is not None:
                    witness = self._flag_time(witness)
                    epoch_witness |= (witness.coordinate == point.coordinate
                                      and after_closure(witness.seconds) and witness.seconds <= point.seconds)
            transition = self._part_epoch_transitions.get(part)
            transition_proof = False
            if transition is not None:
                transition = self._flag_time(transition)
                transition_proof = (transition.coordinate == point.coordinate
                                    and after_closure(transition.seconds) and transition.seconds <= point.seconds)
            if inferred is not None:
                if (inferred.coordinate == point.coordinate
                        and after_closure(inferred.seconds) and inferred.seconds <= point.seconds):
                    epoch_witness = transition_proof = True
            epoch_identified = source_span or identified_in_epoch or epoch_witness
            # Preserve the established bootstrap floor for precise old flags.
            # A source-dated real transition can independently identify
            # historical current-part evidence after a delayed label.
            if (part == self._session_part and not transition_proof and not source_span and not reaffirmed
                    and self._active_clock_at is not None):
                anchor = self._flag_time(self._active_clock_at)
                if anchor.coordinate == point.coordinate and point.seconds < anchor.seconds and end <= anchor.seconds:
                    continue
            active = green is None or activity >= green.seconds or first >= green.seconds
            if epoch_identified and (bounded or source_span or active or transition_proof):
                candidates.append(part)
        if len(candidates) == 1:
            self._race_control_owners[identity] = candidates[0]
            self._race_control_provisional.discard(event)
            self._race_control_finish_owned |= (
                candidates[0] == self._session_part and self._normal_finish_source == "race_control")
        else:
            self._race_control_provisional.add(event)

    def _reconcile_race_control_ownership(self) -> None:
        if not self._race_control_provisional:
            return
        before = len(self._race_control_owners)
        # Ordinary activity revisits only unresolved finishes, not flag history.
        for event in tuple(self._race_control_provisional):
            self._certify_race_control_owner(event)
        if len(self._race_control_owners) != before:
            self._apply_race_control({}, None)

    def _apply_race_control(
        self, payload: Mapping, publication_time: PublicationTime | None,
        *, establish_boundary: bool = False,
    ) -> None:
        messages = payload.get("Messages", {})
        if isinstance(messages, Mapping):
            entries = messages.items()
        else:
            entries = enumerate(messages) if isinstance(messages, (list, tuple)) else ()
        for index, patch in entries:
            if not isinstance(patch, Mapping):
                continue
            item = self._race_control_messages.setdefault(str(index), {})
            previous_flag = item.get("Flag")
            previous_time = self._race_control_item_time(str(index))
            _deep_merge(item, patch)
            flag = item.get("Flag")
            if flag not in {"GREEN", "CHEQUERED"} or item.get("Scope", "Track") != "Track":
                continue
            item_time = self._race_control_item_time(str(index))
            time, precision = item_time if item_time is not None else (None, 0.0)
            identity = (str(index), str(flag))
            first_seen = identity not in self._race_control_publications
            self._race_control_publications.setdefault(identity, publication_time)
            if time is None:
                # A legacy delta's ORIGINAL publication is usable. An unknown
                # historical origin stays unknown on later dated redelivery.
                origin = self._race_control_publications[identity]
                if first_seen and origin is not None:
                    self._race_control_source_times[str(index)] = origin
                time = origin
            was_undated = identity in self._race_control_undated
            if time is None:
                self._race_control_undated.add(identity)
            else:
                self._race_control_undated.discard(identity)
            if (first_seen or previous_flag != flag or previous_time != item_time
                    or was_undated != (time is None)):
                self._race_control_epoch_cache_key = None
            # Unknown-origin snapshots retain their identity without a date.
            if time is not None:
                event = (str(index), time, str(flag), precision)
                self._race_control_flags.add(event)
                self._race_control_order.setdefault(event, len(self._race_control_order))
        # Source message indices settle events printed within the same second;
        # stable first-ingress order is the fallback for nonnumeric identities.
        latest: dict[str, tuple[float, int, str]] = {}
        for event in self._race_control_flags:
            self._certify_race_control_owner(event)
            time = self._race_control_event_time(event)
            if time is None:
                continue
            flag = event[2]
            index = event[0]
            order = int(index) if index.isdecimal() else self._race_control_order[event]
            candidate = (time.seconds, order, flag)
            if candidate > latest.get(time.coordinate, (-math.inf, -1, "")):
                latest[time.coordinate] = candidate
        if any(flag == "CHEQUERED" for _, _, flag in latest.values()):
            self._normal_finished = True
            self._normal_finish_source = "race_control"
            self._race_control_finish_owned |= any(
                owner == self._session_part for owner in self._race_control_owners.values())
        elif self._normal_finish_source == "race_control" and (
            (establish_boundary and not self._race_control_finish_owned)
            or any(flag == "GREEN" for _, _, flag in latest.values())
        ):
            self._normal_finished = False
            self._normal_finish_source = None
            self._race_control_finish_owned = False

    def _set_part(
        self, part: str, publication_time: PublicationTime | None = None,
        *, bootstrap: bool = False, epoch_identification: PublicationTime | None = None,
    ) -> None:
        if part == self._session_part or int(part[-1]) < int(self._session_part[-1]):
            return
        # Certify supported previous-part evidence before discarding activity.
        self._reconcile_race_control_ownership()
        if bootstrap:
            # Topic snapshots can deliver the running clock before TimingData.
            # Initial discovery is not a transition out of an observed Q1.
            self._session_part = part
            self._race_control_finish_owned = False
            self._part_started_at = publication_time
            if (self._declared_clock_remaining == DEFAULT_SESSION_DURATIONS_S[part]
                    and not self._clock_running):
                # Full, paused countdown plus explicit new-part metadata is
                # preparation, even if SessionStatus still describes Q2's end.
                self._normal_finished = False
                self._normal_finish_source = None
            self._apply_race_control({}, None, establish_boundary=True)
            return
        self._part_established = True
        self._part_started_at = publication_time
        proof_time = epoch_identification or publication_time
        if proof_time is not None:
            if epoch_identification is not None:
                self._part_inferred_epochs[part] = proof_time
            else:
                self._part_epoch_transitions[part] = proof_time
                self._record_part_identification(part, proof_time)
        self._active_clock_at = None
        self._part_activity_at = None
        self._countdown_anchor = None
        self._race_control_finish_owned = False
        boundary = self._flag_time(publication_time) if publication_time is not None else None
        for _, time, _, precision in self._race_control_flags:
            time = self._flag_time(time)
            if boundary is None or time.coordinate != boundary.coordinate:
                # Without a UTC/session mapping, known flags retain ownership
                # of the observed previous part; receive time cannot redate them.
                self._race_control_floor[time.coordinate] = max(
                    self._race_control_floor.get(time.coordinate, -math.inf),
                    math.nextafter(time.seconds + precision, math.inf),
                )
        self._projection_part_started_at = publication_time
        self._projection_part_changed = True
        for number, timing in self._timing.items():
            cycle = timing.get("_projection_sector_cycle")
            if cycle is not None:
                cycle.closed = True
            best = (timing.get("_segment_best_s") if timing.get("_official_best")
                    else timing.get("_best_seen_s"))
            self._history.setdefault(number, {})[self._session_part] = best
            if best is not None:
                self._historical_best_s = (
                    best if self._historical_best_s is None
                    else min(self._historical_best_s, best)
                )
            # Preserve physical pit state and cumulative feed lap number only.
            retained = {key: value for key, value in timing.items() if key in {
                "InPit", "Stopped", "NumberOfLaps", "_pit_arriving", "_pit_queue",
                "_departure_unknown", "_pit_context", "_outbound",
                "_outbound_exit_confirmed", "_out_lap_boundary_seen",
            }}
            retained["_last_recorded_lap"] = _as_int(timing.get("NumberOfLaps")) or 0
            timing.clear()
            timing.update(retained)
        observed_previous_segment = self._segment_started or self._normal_finished
        self._session_part = part
        self._segment_start_time_s = (
            self._latest_session_time_s if observed_previous_segment else None
        )
        self._record_lap_evidence("", "segment_start", None)
        self._segment_started = False
        self._segment_has_positive_countdown = False
        self._normal_finished = False
        self._normal_finish_source = None
        self._segment_duration_s = None
        self._declared_clock_remaining = None
        self._remaining_time_s = None
        self._clock_running = False
        self._clock_anchor_session_time_s = self._latest_session_time_s
        if publication_time is not None:
            # A delayed part packet may arrive after that part's own flags.
            self._apply_race_control({}, None)


def _segments(sectors: object) -> tuple[QualiCheckpoint, ...]:
    output = []
    if not isinstance(sectors, (list, Mapping)):
        return ()
    values = sectors.items() if isinstance(sectors, Mapping) else enumerate(sectors)
    elapsed = 0.0
    for sector_index, sector in values:
        if not isinstance(sector, Mapping):
            continue
        sector_time = _lap_time_seconds(sector)
        if sector_time is not None:
            elapsed += sector_time
            output.append(QualiCheckpoint("sector", int(sector_index) + 1, elapsed))
        raw_segments = sector.get("Segments", {})
        segment_values = raw_segments.items() if isinstance(raw_segments, Mapping) else enumerate(raw_segments) if isinstance(raw_segments, list) else ()
        for segment_index, segment in segment_values:
            # Public TimingData commonly exposes colour/status codes in
            # ``Segments[*].Value``. Those are not elapsed times and must not
            # be fabricated into a projection checkpoint. Only explicit time
            # fields are accepted.
            value = _lap_time_seconds(
                segment.get("ElapsedTime") or segment.get("Time")
                if isinstance(segment, Mapping) else None
            )
            if value is not None:
                output.append(QualiCheckpoint("segment", int(segment_index), value))
    return tuple(output)


def _session_part(payload: Mapping, default: str) -> str:
    text = " ".join(str(payload.get(key, "")) for key in ("Name", "Type", "SessionPart"))
    upper = text.upper()
    for part in ("SQ1", "SQ2", "SQ3", "Q1", "Q2", "Q3"):
        if part in upper:
            return part
    if "SPRINT" in upper and ("QUALI" in upper or "SHOOTOUT" in upper):
        return default if default.startswith("SQ") else "SQ1"
    return default


def _latest_sample_time(*sources: dict[str, dict]) -> float | None:
    values = [row.get("time") for source in sources for row in source.values() if row.get("time") is not None]
    return max(values) if values else None


def _explicit_session_part(payload: Mapping, current_part: str = "Q1") -> str | None:
    """Only explicit segment metadata overrides duration/reset inference."""
    part = str(payload.get("SessionPart", "")).upper()
    if part in DEFAULT_SESSION_DURATIONS_S:
        return part
    if part in {"1", "2", "3"}:
        return ("SQ" if current_part.startswith("SQ") else "Q") + part
    name = str(payload.get("Name", "")).upper()
    if name in DEFAULT_SESSION_DURATIONS_S:
        return name
    return None
