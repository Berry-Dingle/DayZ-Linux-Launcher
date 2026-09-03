from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping

from .companion_restart_phase2_detection import (
    DetectionConfig,
    EventOutcome,
    FieldStatus,
    InfoStatus,
    LifecycleMarker,
    ObservationSample,
    PhysicalRestartEvent,
)
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    COMPATIBLE_MULTIPLE_BASE_WEIGHT,
    CoverageKind,
    CoverageSegment,
    MAX_COMPATIBLE_MULTIPLIER,
    PHASE_UNCERTAINTY_CAP,
    SKIP_OVER_WEIGHT_FACTOR,
    candidate_phase_tolerance,
    event_learning_eligible,
)


CONTINUITY_PROVENANCE_VERSION = 1
CONTINUITY_SCHEMA_VERSION = 1
HIGH_AUTHENTICITY_MINIMUM = 0.85
HIGH_COVERAGE_RATIO_MINIMUM = 0.98
MAX_INTERVENING_EVENTS = 1


class ContinuitySpanKind(str, Enum):
    ONLINE_HEALTHY = "online_healthy"
    OFFLINE_OBSERVED = "offline_observed"
    HEALTHY_TO_FAILURE = "healthy_to_failure_transition"
    FAILURE_TO_HEALTHY = "failure_to_healthy_transition"
    PARTIAL = "partial"
    QUERY_HEALTH_GAP = "query_health_gap"
    MISSING_FIELD_GAP = "missing_field_gap"
    UNRESOLVED_EPISODE = "unresolved_episode"
    SLEEP_GAP = "sleep_like_gap"
    CLOCK_REVERSAL = "clock_reversal"
    PAUSE = "pause"
    CLEAR = "clear"
    SHUTDOWN = "shutdown"
    SERVER_SWITCH = "server_switch"
    MONITOR_REPLACEMENT = "monitor_replacement"
    STALE_POLL_REJECTION = "stale_poll_rejection"
    APP_STOPPED = "app_stopped"
    UI_ROW_RECYCLED = "ui_row_recycled"
    UI_SORTED = "ui_sorted"
    UI_FILTERED = "ui_filtered"
    UI_DOCKED = "ui_docked"
    UI_UNDOCKED = "ui_undocked"
    UI_WIDGET_REPLACED = "ui_widget_replaced"


class ChainLifecycle(str, Enum):
    ACTIVE = "active"
    HISTORICAL = "historical"


class CoverageQuality(str, Enum):
    COMPLETE = "complete_same_chain"
    PARTIAL = "partial_same_chain"
    BLOCKED = "blocked"
    UNPROVEN = "historical_continuity_unproven"


class RelationshipKind(str, Enum):
    DIRECT = "direct"
    COMPATIBLE_MULTIPLE = "compatible_multiple"
    SKIP_OVER = "skip_over"


class StreakLifecycle(str, Enum):
    ACTIVE = "active"
    HISTORICAL = "historical"


_UI_ONLY_KINDS = {
    ContinuitySpanKind.UI_ROW_RECYCLED,
    ContinuitySpanKind.UI_SORTED,
    ContinuitySpanKind.UI_FILTERED,
    ContinuitySpanKind.UI_DOCKED,
    ContinuitySpanKind.UI_UNDOCKED,
    ContinuitySpanKind.UI_WIDGET_REPLACED,
}

_BOUNDARY_KINDS = {
    ContinuitySpanKind.PAUSE,
    ContinuitySpanKind.CLEAR,
    ContinuitySpanKind.SHUTDOWN,
    ContinuitySpanKind.SERVER_SWITCH,
    ContinuitySpanKind.MONITOR_REPLACEMENT,
    ContinuitySpanKind.STALE_POLL_REJECTION,
    ContinuitySpanKind.APP_STOPPED,
    ContinuitySpanKind.SLEEP_GAP,
    ContinuitySpanKind.CLOCK_REVERSAL,
    ContinuitySpanKind.QUERY_HEALTH_GAP,
    ContinuitySpanKind.MISSING_FIELD_GAP,
    ContinuitySpanKind.UNRESOLVED_EPISODE,
}

@dataclass(frozen=True)
class ContinuitySpan:
    provenance_version: int
    reference_id: str
    server_key: str
    app_session_id: str | None
    monitoring_session_id: str | None
    poll_generation: int | None
    continuity_chain_id: str | None
    start_at: float
    end_at: float
    start_monotonic: float | None
    end_monotonic: float | None
    kind: ContinuitySpanKind
    cadence_seconds: float
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContinuityChain:
    schema_version: int
    provenance_version: int
    chain_id: str
    server_key: str
    app_session_id: str
    monitoring_session_id: str
    poll_generation: int
    start_at: float
    end_at: float
    termination_reason: str
    reference_ids: tuple[str, ...]
    observed_span_count: int
    provenance_proven: bool
    query_health_adequate: bool
    blocker_kinds: tuple[str, ...]
    lifecycle: ChainLifecycle
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ContinuityAssessment:
    start_at: float
    end_at: float
    continuity_chain_id: str | None
    monitoring_session_id: str | None
    observed_ratio: float
    largest_unexplained_gap: float
    transition_edge_durations: tuple[float, ...]
    blocker_kind: str | None
    continuity_qualified: bool
    query_health_adequate: bool
    coverage_quality: CoverageQuality
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class IntervalRelationship:
    schema_version: int
    provenance_version: int
    relationship_id: str
    server_key: str
    candidate_period_seconds: int
    left_event_id: str
    left_event_sequence: int
    right_event_id: str
    right_event_sequence: int
    left_at: float
    right_at: float
    observed_interval_seconds: float
    signed_residual_seconds: float
    tolerance_seconds: float
    relationship_kind: RelationshipKind
    multiplier: int
    intervening_event_ids: tuple[str, ...]
    coverage_quality: CoverageQuality
    observed_ratio: float
    largest_unexplained_gap: float
    monitoring_session_id: str | None
    continuity_chain_id: str | None
    continuity_qualified: bool
    normal_support_diagnostic: float
    high_authority_eligible: bool
    high_quality_diagnostic: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CadenceStreak:
    schema_version: int
    provenance_version: int
    streak_id: str
    candidate_period_seconds: int
    event_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    interval_count: int
    event_count: int
    start_at: float
    end_at: float
    server_key: str
    monitoring_session_id: str
    continuity_chain_id: str
    complete_coverage_start: float
    complete_coverage_end: float
    minimum_observed_ratio: float
    maximum_unexplained_gap: float
    query_health_quality: float
    phase_estimate: float
    phase_spread: float
    lifecycle: StreakLifecycle
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalProvenanceResult:
    event: PhysicalRestartEvent
    proven: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ContinuityShadowSnapshot:
    chains: tuple[ContinuityChain, ...]
    relationships: tuple[IntervalRelationship, ...]
    streaks: tuple[CadenceStreak, ...]


def deterministic_continuity_chain_id(
    server_key: str,
    app_session_id: str,
    monitoring_session_id: str,
    poll_generation: int,
    started_at: float,
) -> str:
    return _stable_id(
        "continuity-chain",
        CONTINUITY_PROVENANCE_VERSION,
        server_key,
        app_session_id,
        monitoring_session_id,
        poll_generation,
        round(float(started_at), 6),
    )


def span_from_observations(
    previous: ObservationSample,
    current: ObservationSample,
    *,
    config: DetectionConfig | None = None,
) -> ContinuitySpan:
    """Map one observed poll edge without consulting any learned model."""

    config = config or DetectionConfig()
    identity = (
        previous.server_key == current.server_key
        and previous.app_session_id == current.app_session_id
        and previous.monitoring_session_id == current.monitoring_session_id
        and previous.poll_generation == current.poll_generation
        and previous.continuity_chain_id == current.continuity_chain_id
    )
    cadence = (
        config.offline_poll_interval
        if previous.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}
        else config.online_poll_interval
    )
    reason_codes: list[str] = []
    if current.lifecycle is not LifecycleMarker.NORMAL:
        kind = {
            LifecycleMarker.PAUSE: ContinuitySpanKind.PAUSE,
            LifecycleMarker.CLEAR: ContinuitySpanKind.CLEAR,
            LifecycleMarker.SHUTDOWN: ContinuitySpanKind.SHUTDOWN,
            LifecycleMarker.SERVER_SWITCH: ContinuitySpanKind.SERVER_SWITCH,
        }.get(current.lifecycle, ContinuitySpanKind.MONITOR_REPLACEMENT)
        reason_codes.append(f"lifecycle_{current.lifecycle.value}")
    elif current.wall_at < previous.wall_at or current.monotonic_at < previous.monotonic_at:
        kind = ContinuitySpanKind.CLOCK_REVERSAL
        reason_codes.append("clock_reversal")
    elif not identity:
        kind = ContinuitySpanKind.MONITOR_REPLACEMENT
        reason_codes.append("observation_identity_changed")
    else:
        elapsed = current.wall_at - previous.wall_at
        maximum = (
            max(12.0, 4.0 * config.offline_poll_interval)
            if previous.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}
            else max(30.0, 3.0 * config.online_poll_interval)
        )
        if elapsed > maximum:
            kind = ContinuitySpanKind.SLEEP_GAP
            reason_codes.append("poll_gap_exceeds_active_cadence")
        elif _healthy_complete(previous) and _healthy_complete(current):
            kind = ContinuitySpanKind.ONLINE_HEALTHY
            reason_codes.append("healthy_poll_edge")
        elif _healthy_info(previous) and _qualifying_failure(current):
            kind = ContinuitySpanKind.HEALTHY_TO_FAILURE
            cadence = config.online_poll_interval
            reason_codes.append("observed_healthy_to_failure")
        elif _qualifying_failure(previous) and _healthy_info(current):
            kind = ContinuitySpanKind.FAILURE_TO_HEALTHY
            cadence = config.offline_poll_interval
            reason_codes.append("observed_failure_to_healthy")
        elif _qualifying_failure(previous) and _qualifying_failure(current):
            kind = ContinuitySpanKind.OFFLINE_OBSERVED
            reason_codes.append("observed_failure_poll_edge")
        elif (
            previous.info_status in {InfoStatus.NEUTRAL, InfoStatus.ERROR, InfoStatus.MISSING}
            or current.info_status in {InfoStatus.NEUTRAL, InfoStatus.ERROR, InfoStatus.MISSING}
        ):
            kind = ContinuitySpanKind.QUERY_HEALTH_GAP
            reason_codes.append("query_health_inadequate")
        elif (
            previous.player_status is not FieldStatus.PRESENT
            or current.player_status is not FieldStatus.PRESENT
        ):
            kind = ContinuitySpanKind.MISSING_FIELD_GAP
            reason_codes.append("player_field_missing")
        else:
            kind = ContinuitySpanKind.PARTIAL
            reason_codes.append("poll_edge_partial")
    return ContinuitySpan(
        provenance_version=min(previous.provenance_version, current.provenance_version),
        reference_id=_stable_id(
            "poll-edge",
            previous.server_key,
            previous.monitoring_session_id,
            previous.poll_generation,
            round(previous.wall_at, 6),
            round(current.wall_at, 6),
            kind.value,
        ),
        server_key=previous.server_key,
        app_session_id=previous.app_session_id,
        monitoring_session_id=previous.monitoring_session_id,
        poll_generation=previous.poll_generation,
        continuity_chain_id=previous.continuity_chain_id,
        start_at=previous.wall_at,
        end_at=current.wall_at,
        start_monotonic=previous.monotonic_at,
        end_monotonic=current.monotonic_at,
        kind=kind,
        cadence_seconds=cadence,
        reason_codes=tuple(reason_codes),
    )


def build_continuity_chains(
    spans: Iterable[ContinuitySpan],
) -> tuple[ContinuityChain, ...]:
    """Build maximal chains in capture order; UI-only records are inert."""

    ordered = _dedupe_spans(spans)
    chains: list[ContinuityChain] = []
    active: list[ContinuitySpan] = []
    active_reasons: set[str] = set()

    def close(reason: str, *, lifecycle: ChainLifecycle) -> None:
        nonlocal active, active_reasons
        if not active:
            return
        chains.append(_chain_from_spans(active, reason, lifecycle, active_reasons))
        active = []
        active_reasons = set()

    for span in ordered:
        if span.kind in _UI_ONLY_KINDS:
            if active:
                active_reasons.add("ui_lifecycle_ignored")
            continue
        if span.kind in _BOUNDARY_KINDS or span.kind is ContinuitySpanKind.PARTIAL:
            close(span.kind.value, lifecycle=ChainLifecycle.HISTORICAL)
            continue
        if active:
            identity_reason = _identity_break_reason(active[-1], span)
            if identity_reason is not None:
                close(identity_reason, lifecycle=ChainLifecycle.HISTORICAL)
            elif (
                span.start_at < active[-1].start_at
                or (
                    span.start_monotonic is not None
                    and active[-1].start_monotonic is not None
                    and span.start_monotonic < active[-1].start_monotonic
                )
            ):
                close("clock_reversal", lifecycle=ChainLifecycle.HISTORICAL)
            else:
                gap = span.start_at - active[-1].end_at
                maximum = max(30.0, 3.0 * max(active[-1].cadence_seconds, span.cadence_seconds))
                if gap > maximum:
                    close("sleep_like_gap", lifecycle=ChainLifecycle.HISTORICAL)
        active.append(span)
        active_reasons.update(span.reason_codes)
    close("end_of_input", lifecycle=ChainLifecycle.ACTIVE)
    return tuple(chains)


def continuity_span_terminates_chain(span: ContinuitySpan) -> bool:
    """Return whether a physical/health boundary ends uninterrupted observation."""

    return span.kind in _BOUNDARY_KINDS or span.kind is ContinuitySpanKind.PARTIAL


def assess_interval_continuity(
    *,
    start_at: float,
    end_at: float,
    server_key: str,
    app_session_id: str | None,
    monitoring_session_id: str | None,
    poll_generation: int | None,
    continuity_chain_id: str | None,
    spans: Iterable[ContinuitySpan],
) -> ContinuityAssessment:
    reasons: set[str] = set()
    if (
        not app_session_id
        or not monitoring_session_id
        or poll_generation is None
        or not continuity_chain_id
    ):
        return ContinuityAssessment(
            start_at,
            end_at,
            continuity_chain_id,
            monitoring_session_id,
            0.0,
            end_at - start_at,
            (),
            "historical_continuity_unproven",
            False,
            False,
            CoverageQuality.UNPROVEN,
            ("historical_continuity_unproven",),
        )

    relevant: list[tuple[float, float]] = []
    relevant_spans: list[tuple[float, float, ContinuitySpanKind, float]] = []
    blockers: set[str] = set()
    transitions: list[float] = []
    legacy_cadence_proven = False
    for span in _dedupe_spans(spans):
        overlaps = span.end_at > start_at and span.start_at < end_at
        point_inside = span.start_at == span.end_at and start_at <= span.start_at <= end_at
        if not overlaps and not point_inside:
            continue
        identity_matches = (
            span.server_key == server_key
            and span.app_session_id == app_session_id
            and span.monitoring_session_id == monitoring_session_id
            and span.poll_generation == poll_generation
            and span.continuity_chain_id == continuity_chain_id
        )
        if not identity_matches:
            blockers.add("continuity_identity_mismatch")
            continue
        if span.provenance_version < CONTINUITY_PROVENANCE_VERSION:
            blockers.add("historical_continuity_unproven")
            continue
        if span.kind in _UI_ONLY_KINDS:
            reasons.add("ui_lifecycle_ignored")
            continue
        if span.kind in _BOUNDARY_KINDS or span.kind is ContinuitySpanKind.PARTIAL:
            blockers.add(span.kind.value)
            continue
        if span.kind in {
            ContinuitySpanKind.HEALTHY_TO_FAILURE,
            ContinuitySpanKind.FAILURE_TO_HEALTHY,
        }:
            duration = span.end_at - span.start_at
            transitions.append(duration)
            limit = (
                max(30.0, 3.0 * span.cadence_seconds)
                if span.kind is ContinuitySpanKind.HEALTHY_TO_FAILURE
                else max(12.0, 4.0 * span.cadence_seconds)
            )
            if duration > limit:
                blockers.add("transition_edge_exceeds_cadence")
                continue
            reasons.add("observed_transition_edge_accepted")
        legacy_cadence_proven = legacy_cadence_proven or (
            "legacy_coverage_cadence_proven" in span.reason_codes
        )
        clipped_start = max(start_at, span.start_at)
        clipped_end = min(end_at, span.end_at)
        relevant.append((clipped_start, clipped_end))
        relevant_spans.append(
            (clipped_start, clipped_end, span.kind, span.cadence_seconds)
        )

    merged = _merge_intervals(relevant)
    observed = sum(right - left for left, right in merged)
    ratio = min(1.0, observed / max(1e-9, end_at - start_at))
    gap_details = _interval_gap_details(start_at, end_at, relevant_spans)
    unexplained_gaps: list[float] = []
    for gap, left_kind, left_cadence, right_kind, right_cadence in gap_details:
        if gap <= 2.0:
            reasons.add("endpoint_poll_jitter_accepted")
            continue
        adjacent_kinds = {item for item in (left_kind, right_kind) if item is not None}
        adjacent_cadences = [
            item for item in (left_cadence, right_cadence) if item is not None
        ]
        offline_adjacent = ContinuitySpanKind.OFFLINE_OBSERVED in adjacent_kinds
        offline_cadences = [
            cadence
            for kind, cadence in (
                (left_kind, left_cadence),
                (right_kind, right_cadence),
            )
            if kind is ContinuitySpanKind.OFFLINE_OBSERVED and cadence is not None
        ]
        cadence_limit = (
            max(12.0, 4.0 * max(offline_cadences, default=3.0))
            if offline_adjacent
            else max(30.0, 3.0 * max(adjacent_cadences, default=10.0))
        )
        if gap <= cadence_limit:
            reasons.add("poll_gap_within_active_cadence")
            if legacy_cadence_proven:
                reasons.add("legacy_coverage_cadence_proven")
            continue
        unexplained_gaps.append(gap)
    unexplained = max(unexplained_gaps, default=0.0)
    if unexplained > 0:
        blockers.add("unexplained_polling_gap")
    if ratio < HIGH_COVERAGE_RATIO_MINIMUM:
        blockers.add("coverage_ratio_below_high_minimum")

    query_adequate = not blockers.intersection(
        {
            ContinuitySpanKind.QUERY_HEALTH_GAP.value,
            ContinuitySpanKind.MISSING_FIELD_GAP.value,
            "continuity_identity_mismatch",
            "historical_continuity_unproven",
        }
    )
    qualified = bool(merged) and not blockers
    if qualified:
        quality = CoverageQuality.COMPLETE
        reasons.add("complete_same_chain_coverage")
    elif "historical_continuity_unproven" in blockers:
        quality = CoverageQuality.UNPROVEN
    elif blockers:
        quality = CoverageQuality.BLOCKED
    else:
        quality = CoverageQuality.PARTIAL
    return ContinuityAssessment(
        start_at=start_at,
        end_at=end_at,
        continuity_chain_id=continuity_chain_id,
        monitoring_session_id=monitoring_session_id,
        observed_ratio=round(ratio, 6),
        largest_unexplained_gap=round(unexplained, 6),
        transition_edge_durations=tuple(round(value, 6) for value in transitions),
        blocker_kind=sorted(blockers)[0] if blockers else None,
        continuity_qualified=qualified,
        query_health_adequate=query_adequate,
        coverage_quality=quality,
        reason_codes=tuple(sorted(reasons.union(blockers))),
    )


def extract_interval_relationships(
    events: Iterable[PhysicalRestartEvent],
    spans: Iterable[ContinuitySpan],
    *,
    candidate_periods: Iterable[int] = CANDIDATE_PERIODS,
) -> tuple[IntervalRelationship, ...]:
    """Extract raw candidate interpretations with no incumbent/model inputs."""

    periods = tuple(sorted(set(int(value) for value in candidate_periods)))
    unique = _unique_events(events)
    span_values = tuple(spans)
    relationships: dict[str, IntervalRelationship] = {}
    for left_index, left in enumerate(unique):
        if (
            not event_learning_eligible(left)
            or _schedule_neutral_attribution_anomaly(left)
        ):
            continue
        left_at = _event_at(left)
        if left_at is None:
            continue
        upper = min(len(unique), left_index + MAX_INTERVENING_EVENTS + 2)
        for right_index in range(left_index + 1, upper):
            right = unique[right_index]
            if (
                not event_learning_eligible(right)
                or _schedule_neutral_attribution_anomaly(right)
            ):
                continue
            right_at = _event_at(right)
            if right_at is None or right_at <= left_at:
                continue
            intervening = tuple(unique[left_index + 1:right_index])
            authentic_intervening = tuple(
                item for item in intervening if _authentic_physical_event(item)
            )
            gap = right_at - left_at
            same_identity = _same_event_continuity(left, right)
            assessment = assess_interval_continuity(
                start_at=left_at,
                end_at=right_at,
                server_key=left.server_key,
                app_session_id=left.app_session_id if same_identity else None,
                monitoring_session_id=left.monitoring_session_id if same_identity else None,
                poll_generation=left.poll_generation if same_identity else None,
                continuity_chain_id=left.continuity_chain_id if same_identity else None,
                spans=span_values,
            )
            for period in periods:
                multiplier = max(1, round(gap / period))
                if multiplier > MAX_COMPATIBLE_MULTIPLIER:
                    continue
                tolerance = candidate_phase_tolerance(period) + min(
                    PHASE_UNCERTAINTY_CAP,
                    max(0.0, left.phase_uncertainty, right.phase_uncertainty),
                )
                residual = gap - multiplier * period
                if abs(residual) > tolerance:
                    continue
                if authentic_intervening:
                    kind = RelationshipKind.SKIP_OVER
                elif multiplier == 1:
                    kind = RelationshipKind.DIRECT
                else:
                    kind = RelationshipKind.COMPATIBLE_MULTIPLE
                relationship_id = _stable_id(
                    "interval-relationship",
                    CONTINUITY_SCHEMA_VERSION,
                    left.server_key,
                    period,
                    left.event_id,
                    right.event_id,
                    kind.value,
                )
                normal_support = _normal_support(left, right, kind, multiplier)
                high_reasons = _high_eligibility_reasons(
                    left,
                    right,
                    kind,
                    residual,
                    tolerance,
                    authentic_intervening,
                    assessment,
                )
                eligible = not high_reasons
                reasons = {
                    "candidate_interval_match",
                    f"relationship_{kind.value}",
                    *assessment.reason_codes,
                    *high_reasons,
                }
                if eligible:
                    reasons.add("high_authority_eligible")
                high_quality = 0.0
                if eligible:
                    authenticity_quality = min(
                        1.0,
                        min(left.authenticity, right.authenticity)
                        / HIGH_AUTHENTICITY_MINIMUM,
                    )
                    coverage_quality = min(
                        1.0,
                        assessment.observed_ratio / HIGH_COVERAGE_RATIO_MINIMUM,
                    )
                    phase_fit = 1.0 - 0.20 * min(1.0, abs(residual) / tolerance)
                    high_quality = min(
                        authenticity_quality,
                        coverage_quality,
                        phase_fit,
                    )
                relationship = IntervalRelationship(
                    schema_version=CONTINUITY_SCHEMA_VERSION,
                    provenance_version=min(
                        left.provenance_version,
                        right.provenance_version,
                    ),
                    relationship_id=relationship_id,
                    server_key=left.server_key,
                    candidate_period_seconds=period,
                    left_event_id=left.event_id,
                    left_event_sequence=left.sequence,
                    right_event_id=right.event_id,
                    right_event_sequence=right.sequence,
                    left_at=left_at,
                    right_at=right_at,
                    observed_interval_seconds=round(gap, 6),
                    signed_residual_seconds=round(residual, 6),
                    tolerance_seconds=round(tolerance, 6),
                    relationship_kind=kind,
                    multiplier=multiplier,
                    intervening_event_ids=tuple(
                        item.event_id for item in authentic_intervening
                    ),
                    coverage_quality=assessment.coverage_quality,
                    observed_ratio=assessment.observed_ratio,
                    largest_unexplained_gap=assessment.largest_unexplained_gap,
                    monitoring_session_id=(
                        left.monitoring_session_id if same_identity else None
                    ),
                    continuity_chain_id=(
                        left.continuity_chain_id if same_identity else None
                    ),
                    continuity_qualified=assessment.continuity_qualified,
                    normal_support_diagnostic=round(normal_support, 6),
                    high_authority_eligible=eligible,
                    high_quality_diagnostic=round(high_quality, 6),
                    reason_codes=tuple(sorted(reasons)),
                )
                relationships.setdefault(relationship_id, relationship)
    return tuple(
        sorted(
            relationships.values(),
            key=lambda item: (
                item.left_at,
                item.right_at,
                item.candidate_period_seconds,
                item.relationship_id,
            ),
        )
    )


def build_cadence_streaks(
    relationships: Iterable[IntervalRelationship],
    *,
    chains: Iterable[ContinuityChain] = (),
) -> tuple[CadenceStreak, ...]:
    """Emit only maximal runs of at least two eligible direct intervals."""

    eligible = {
        item.relationship_id: item
        for item in relationships
        if item.high_authority_eligible
        and item.relationship_kind is RelationshipKind.DIRECT
        and item.continuity_chain_id
        and not item.intervening_event_ids
    }
    ordered = sorted(
        eligible.values(),
        key=lambda item: (
            item.candidate_period_seconds,
            item.continuity_chain_id,
            item.left_at,
            item.right_at,
            item.relationship_id,
        ),
    )
    chain_map = {item.chain_id: item for item in chains}
    runs: list[list[IntervalRelationship]] = []
    active: list[IntervalRelationship] = []

    def close() -> None:
        nonlocal active
        if len(active) >= 2:
            runs.append(active)
        active = []

    for relationship in ordered:
        if not active:
            active = [relationship]
            continue
        previous = active[-1]
        joins = (
            relationship.candidate_period_seconds
            == previous.candidate_period_seconds
            and relationship.continuity_chain_id
            == previous.continuity_chain_id
            and relationship.left_event_id == previous.right_event_id
            and relationship.left_at == previous.right_at
            and not relationship.intervening_event_ids
        )
        proposed = [*active, relationship]
        if joins and _relationship_phase_coherent(proposed):
            active.append(relationship)
        else:
            close()
            active = [relationship]
    close()

    streaks: dict[str, CadenceStreak] = {}
    for run in runs:
        period = run[0].candidate_period_seconds
        event_ids = (run[0].left_event_id, *(item.right_event_id for item in run))
        relationship_ids = tuple(item.relationship_id for item in run)
        phases = (run[0].left_at, *(item.right_at for item in run))
        phase, spread = _phase_estimate(phases, period)
        chain_id = run[0].continuity_chain_id
        assert chain_id is not None
        chain = chain_map.get(chain_id)
        lifecycle = (
            StreakLifecycle.HISTORICAL
            if chain is not None and chain.lifecycle is ChainLifecycle.HISTORICAL
            else StreakLifecycle.ACTIVE
        )
        streak_id = _stable_id(
            "cadence-streak",
            CONTINUITY_SCHEMA_VERSION,
            run[0].server_key,
            period,
            chain_id,
            *relationship_ids,
        )
        streaks[streak_id] = CadenceStreak(
            schema_version=CONTINUITY_SCHEMA_VERSION,
            provenance_version=min(item.provenance_version for item in run),
            streak_id=streak_id,
            candidate_period_seconds=period,
            event_ids=event_ids,
            relationship_ids=relationship_ids,
            interval_count=len(run),
            event_count=len(event_ids),
            start_at=run[0].left_at,
            end_at=run[-1].right_at,
            server_key=run[0].server_key,
            monitoring_session_id=run[0].monitoring_session_id or "",
            continuity_chain_id=chain_id,
            complete_coverage_start=run[0].left_at,
            complete_coverage_end=run[-1].right_at,
            minimum_observed_ratio=min(item.observed_ratio for item in run),
            maximum_unexplained_gap=max(
                item.largest_unexplained_gap for item in run
            ),
            query_health_quality=1.0,
            phase_estimate=round(phase, 6),
            phase_spread=round(spread, 6),
            lifecycle=lifecycle,
            reason_codes=(
                "coherent_phase_cluster",
                "maximal_continuity_streak",
                "no_overlapping_substreaks_emitted",
            ),
        )
    return tuple(sorted(streaks.values(), key=lambda item: (item.start_at, item.streak_id)))


def reconstruct_historical_event_provenance(
    event: PhysicalRestartEvent,
    monitoring_sessions: Iterable[Mapping[str, object]],
) -> HistoricalProvenanceResult:
    """Conservative detached helper; never changes persisted state."""

    if (
        event.provenance_version >= CONTINUITY_PROVENANCE_VERSION
        and event.app_session_id
        and event.monitoring_session_id
        and event.poll_generation is not None
        and event.continuity_chain_id
    ):
        return HistoricalProvenanceResult(
            event, True, ("explicit_event_continuity_provenance",)
        )
    sessions = {
        str(item.get("session_id") or ""): item
        for item in monitoring_sessions
        if isinstance(item, Mapping) and item.get("session_id")
    }
    monitor_id = None
    source_reason = None
    sample_identities = {
        (
            item.app_session_id,
            item.monitoring_session_id,
            item.poll_generation,
        )
        for item in event.samples
    }
    if len(sample_identities) == 1:
        _app, monitor_id, _poll = next(iter(sample_identities))
        source_reason = "provenance_from_persisted_samples"
    else:
        parts = event.fingerprint.split("|")
        if len(parts) >= 5 and parts[0] == event.server_key and parts[1] in sessions:
            monitor_id = parts[1]
            source_reason = "provenance_from_v1_fingerprint_and_session"
    session = sessions.get(str(monitor_id or ""))
    if session is None:
        return HistoricalProvenanceResult(
            event, False, ("historical_continuity_unproven",)
        )
    app_id = str(session.get("app_session_id") or "")
    poll_generation = session.get("poll_generation")
    started_at = _finite_or_none(session.get("started_at"))
    ended_at = _finite_or_none(session.get("ended_at"))
    event_at = _event_at(event)
    if (
        not app_id
        or type(poll_generation) is not int
        or poll_generation < 0
        or started_at is None
        or event_at is None
        or event_at < started_at
        or (ended_at is not None and event_at > ended_at)
    ):
        return HistoricalProvenanceResult(
            event, False, ("historical_session_crosscheck_failed",)
        )
    chain_id = deterministic_continuity_chain_id(
        event.server_key,
        app_id,
        str(monitor_id),
        poll_generation,
        started_at,
    )
    reconstructed = replace(
        event,
        app_session_id=app_id,
        monitoring_session_id=str(monitor_id),
        poll_generation=poll_generation,
        continuity_chain_id=chain_id,
        provenance_version=CONTINUITY_PROVENANCE_VERSION,
    )
    return HistoricalProvenanceResult(
        reconstructed,
        True,
        tuple(sorted({source_reason or "historical_continuity_proven", "session_bounds_crosschecked"})),
    )


def legacy_coverage_spans(
    coverage: Iterable[CoverageSegment],
    *,
    server_key: str,
    monitoring_session: Mapping[str, object],
) -> tuple[ContinuitySpan, ...]:
    """Create detached shadow spans only within one persisted session."""

    session_id = str(monitoring_session.get("session_id") or "")
    app_id = str(monitoring_session.get("app_session_id") or "")
    poll = monitoring_session.get("poll_generation")
    started = _finite_or_none(monitoring_session.get("started_at"))
    ended = _finite_or_none(monitoring_session.get("ended_at"))
    if not session_id or not app_id or type(poll) is not int or started is None:
        return ()
    session_end = math.inf if ended is None else ended
    chain_id = deterministic_continuity_chain_id(
        server_key, app_id, session_id, poll, started
    )
    kind_map = {
        CoverageKind.ONLINE_HEALTHY: ContinuitySpanKind.ONLINE_HEALTHY,
        CoverageKind.OFFLINE_OBSERVED: ContinuitySpanKind.OFFLINE_OBSERVED,
        CoverageKind.QUERY_HEALTH_GAP: ContinuitySpanKind.QUERY_HEALTH_GAP,
        CoverageKind.MISSING_PLAYERS: ContinuitySpanKind.MISSING_FIELD_GAP,
        CoverageKind.MISSING_INFO: ContinuitySpanKind.MISSING_FIELD_GAP,
        CoverageKind.UNRESOLVED_EPISODE: ContinuitySpanKind.UNRESOLVED_EPISODE,
        CoverageKind.SLEEP_GAP: ContinuitySpanKind.SLEEP_GAP,
        CoverageKind.PAUSED: ContinuitySpanKind.PAUSE,
        CoverageKind.SERVER_SWITCH: ContinuitySpanKind.SERVER_SWITCH,
        CoverageKind.APP_STOPPED: ContinuitySpanKind.APP_STOPPED,
        CoverageKind.UNMONITORED: ContinuitySpanKind.PARTIAL,
        CoverageKind.PARTIAL: ContinuitySpanKind.PARTIAL,
    }
    values = []
    for index, segment in enumerate(coverage):
        start = max(started, segment.start_at)
        end = min(session_end, segment.end_at)
        if end <= start:
            continue
        kind = kind_map.get(segment.kind, ContinuitySpanKind.PARTIAL)
        values.append(
            ContinuitySpan(
                provenance_version=CONTINUITY_PROVENANCE_VERSION,
                reference_id=_stable_id(
                    "legacy-coverage", server_key, session_id, index, start, end, kind.value
                ),
                server_key=server_key,
                app_session_id=app_id,
                monitoring_session_id=session_id,
                poll_generation=poll,
                continuity_chain_id=chain_id,
                start_at=start,
                end_at=end,
                start_monotonic=start - started,
                end_monotonic=end - started,
                kind=kind,
                cadence_seconds=segment.cadence,
                reason_codes=(
                    "detached_legacy_coverage",
                    "legacy_coverage_cadence_proven",
                ),
            )
        )
    reason = str(monitoring_session.get("reason") or "").strip().lower()
    boundary_kind = {
        "pause": ContinuitySpanKind.PAUSE,
        "clear": ContinuitySpanKind.CLEAR,
        "shutdown": ContinuitySpanKind.SHUTDOWN,
        "server_switch": ContinuitySpanKind.SERVER_SWITCH,
        "app_restart": ContinuitySpanKind.APP_STOPPED,
        "monitor_replacement": ContinuitySpanKind.MONITOR_REPLACEMENT,
        "stale_poll_rejection": ContinuitySpanKind.STALE_POLL_REJECTION,
    }.get(reason)
    if ended is not None and boundary_kind is not None:
        values.append(
            ContinuitySpan(
                provenance_version=CONTINUITY_PROVENANCE_VERSION,
                reference_id=_stable_id(
                    "legacy-session-boundary",
                    server_key,
                    session_id,
                    ended,
                    boundary_kind.value,
                ),
                server_key=server_key,
                app_session_id=app_id,
                monitoring_session_id=session_id,
                poll_generation=poll,
                continuity_chain_id=chain_id,
                start_at=ended,
                end_at=ended,
                start_monotonic=ended - started,
                end_monotonic=ended - started,
                kind=boundary_kind,
                cadence_seconds=10.0,
                reason_codes=("persisted_monitoring_session_boundary",),
            )
        )
    return tuple(values)


def observed_transition_spans_from_events(
    events: Iterable[PhysicalRestartEvent],
) -> tuple[ContinuitySpan, ...]:
    values: dict[str, ContinuitySpan] = {}
    for event in events:
        if (
            event.provenance_version < CONTINUITY_PROVENANCE_VERSION
            or not event.app_session_id
            or not event.monitoring_session_id
            or event.poll_generation is None
            or not event.continuity_chain_id
            or not event.samples
        ):
            continue
        samples = sorted(event.samples, key=lambda item: item.wall_at)
        first_failure = event.outage.first_failure_at
        confirmed = event.outage.confirmed_offline_at
        returned = event.outage.info_return_at
        if first_failure is not None and confirmed is not None:
            healthy = [
                item.wall_at
                for item in samples
                if item.wall_at <= first_failure and item.info_status is InfoStatus.HEALTHY
            ]
            if healthy and confirmed > healthy[-1]:
                span = _event_transition_span(
                    event,
                    start=healthy[-1],
                    end=confirmed,
                    kind=ContinuitySpanKind.HEALTHY_TO_FAILURE,
                    cadence=10.0,
                )
                values[span.reference_id] = span
        if returned is not None:
            failures = [
                item.wall_at
                for item in samples
                if item.wall_at < returned and _qualifying_failure(item)
            ]
            if failures and returned > failures[-1]:
                span = _event_transition_span(
                    event,
                    start=failures[-1],
                    end=returned,
                    kind=ContinuitySpanKind.FAILURE_TO_HEALTHY,
                    cadence=3.0,
                )
                values[span.reference_id] = span
    return tuple(sorted(values.values(), key=lambda item: (item.start_at, item.reference_id)))


class ContinuityShadowTracker:
    """Optional in-memory adapter. It has no persistence or policy authority."""

    def __init__(self, config: DetectionConfig | None = None) -> None:
        self.config = config or DetectionConfig()
        self._last_sample: ObservationSample | None = None
        self._spans: list[ContinuitySpan] = []

    @property
    def spans(self) -> tuple[ContinuitySpan, ...]:
        return tuple(self._spans)

    def observe(self, sample: ObservationSample) -> None:
        if self._last_sample is not None:
            self._spans.append(
                span_from_observations(self._last_sample, sample, config=self.config)
            )
        self._last_sample = sample if sample.lifecycle is LifecycleMarker.NORMAL else None

    def begin_chain(self, sample: ObservationSample) -> None:
        """Start after a recorded boundary without inventing an observed edge."""

        self._last_sample = sample

    def snapshot(
        self, events: Iterable[PhysicalRestartEvent]
    ) -> ContinuityShadowSnapshot:
        chains = build_continuity_chains(self._spans)
        relationships = extract_interval_relationships(events, self._spans)
        streaks = build_cadence_streaks(relationships, chains=chains)
        return ContinuityShadowSnapshot(chains, relationships, streaks)


def _chain_from_spans(
    spans: list[ContinuitySpan],
    termination_reason: str,
    lifecycle: ChainLifecycle,
    extra_reasons: set[str],
) -> ContinuityChain:
    first = spans[0]
    provenance = all(
        item.provenance_version >= CONTINUITY_PROVENANCE_VERSION
        and item.server_key
        and item.app_session_id
        and item.monitoring_session_id
        and item.poll_generation is not None
        and item.continuity_chain_id
        for item in spans
    )
    chain_id = first.continuity_chain_id or _stable_id(
        "unproven-chain", first.server_key, first.reference_id
    )
    blockers = tuple(
        sorted({item.kind.value for item in spans if item.kind in _BOUNDARY_KINDS})
    )
    reasons = set(extra_reasons)
    reasons.add("continuity_chain_maximal")
    reasons.add("explicit_continuity_provenance" if provenance else "historical_continuity_unproven")
    return ContinuityChain(
        schema_version=CONTINUITY_SCHEMA_VERSION,
        provenance_version=min(item.provenance_version for item in spans),
        chain_id=chain_id,
        server_key=first.server_key,
        app_session_id=first.app_session_id or "",
        monitoring_session_id=first.monitoring_session_id or "",
        poll_generation=first.poll_generation or 0,
        start_at=spans[0].start_at,
        end_at=max(item.end_at for item in spans),
        termination_reason=termination_reason,
        reference_ids=tuple(item.reference_id for item in spans),
        observed_span_count=len(spans),
        provenance_proven=provenance,
        query_health_adequate=not blockers,
        blocker_kinds=blockers,
        lifecycle=lifecycle,
        reason_codes=tuple(sorted(reasons)),
    )


def _identity_break_reason(left: ContinuitySpan, right: ContinuitySpan) -> str | None:
    if left.server_key != right.server_key:
        return "canonical_server_changed"
    if left.app_session_id != right.app_session_id:
        return "app_session_changed"
    if left.monitoring_session_id != right.monitoring_session_id:
        return "monitoring_session_changed"
    if left.poll_generation != right.poll_generation:
        return "poll_generation_changed"
    if left.continuity_chain_id != right.continuity_chain_id:
        return "continuity_chain_changed"
    return None


def _high_eligibility_reasons(
    left: PhysicalRestartEvent,
    right: PhysicalRestartEvent,
    kind: RelationshipKind,
    residual: float,
    tolerance: float,
    intervening: tuple[PhysicalRestartEvent, ...],
    assessment: ContinuityAssessment,
) -> tuple[str, ...]:
    reasons = []
    if kind is not RelationshipKind.DIRECT:
        reasons.append("high_ineligible_non_direct_relationship")
    if left.outcome not in {
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
    } or right.outcome not in {
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
    }:
        reasons.append("high_ineligible_endpoint_class")
    if min(left.authenticity, right.authenticity) < HIGH_AUTHENTICITY_MINIMUM:
        reasons.append("high_ineligible_authenticity")
    if not _direct_outage_contract(left) or not _direct_outage_contract(right):
        reasons.append("high_ineligible_direct_outage_contract")
    if not _same_event_continuity(left, right):
        reasons.append("high_ineligible_continuity_identity")
    if intervening:
        reasons.append("high_ineligible_intervening_authentic_restart")
    if abs(residual) > tolerance:
        reasons.append("high_ineligible_residual")
    if not assessment.continuity_qualified:
        reasons.append("high_ineligible_coverage")
    if not assessment.query_health_adequate:
        reasons.append("high_ineligible_query_health")
    return tuple(sorted(set(reasons)))


def _direct_outage_contract(event: PhysicalRestartEvent) -> bool:
    return bool(
        event.coverage_complete
        and event.query_health.continuous
        and event.outage.first_failure_at is not None
        and event.outage.confirmed_offline_at is not None
        and event.outage.info_return_at is not None
        and event.recovery.stable_recovery_at is not None
        and event.outage.first_failure_at <= event.outage.confirmed_offline_at
        <= event.outage.info_return_at
    )


def _normal_support(
    left: PhysicalRestartEvent,
    right: PhysicalRestartEvent,
    kind: RelationshipKind,
    multiplier: int,
) -> float:
    support = min(left.schedule_weight_suggestion, right.schedule_weight_suggestion)
    if kind in {RelationshipKind.COMPATIBLE_MULTIPLE, RelationshipKind.SKIP_OVER}:
        support *= COMPATIBLE_MULTIPLE_BASE_WEIGHT / math.sqrt(max(1, multiplier - 1))
    if kind is RelationshipKind.SKIP_OVER:
        support *= SKIP_OVER_WEIGHT_FACTOR
    return max(0.0, support)


def _same_event_continuity(
    left: PhysicalRestartEvent, right: PhysicalRestartEvent
) -> bool:
    return bool(
        left.provenance_version >= CONTINUITY_PROVENANCE_VERSION
        and right.provenance_version >= CONTINUITY_PROVENANCE_VERSION
        and left.server_key == right.server_key
        and left.app_session_id
        and left.app_session_id == right.app_session_id
        and left.monitoring_session_id
        and left.monitoring_session_id == right.monitoring_session_id
        and left.poll_generation is not None
        and left.poll_generation == right.poll_generation
        and left.continuity_chain_id
        and left.continuity_chain_id == right.continuity_chain_id
    )


def _authentic_physical_event(event: PhysicalRestartEvent) -> bool:
    return event.authenticity >= 0.50 and event.outcome not in {
        EventOutcome.AMBIGUOUS_DRAIN,
        EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
        EventOutcome.INCOMPLETE,
        EventOutcome.EXPIRED,
        EventOutcome.SERVICE_INTERRUPTION,
    }


def _schedule_neutral_attribution_anomaly(event: PhysicalRestartEvent) -> bool:
    return bool(
        event.outcome is EventOutcome.INCOMPLETE
        and "query_visible_close_collapse_attribution_anomaly"
        in event.reason_codes
    )


def _event_transition_span(
    event: PhysicalRestartEvent,
    *,
    start: float,
    end: float,
    kind: ContinuitySpanKind,
    cadence: float,
) -> ContinuitySpan:
    return ContinuitySpan(
        provenance_version=event.provenance_version,
        reference_id=_stable_id("event-transition", event.event_id, kind.value, start, end),
        server_key=event.server_key,
        app_session_id=event.app_session_id,
        monitoring_session_id=event.monitoring_session_id,
        poll_generation=event.poll_generation,
        continuity_chain_id=event.continuity_chain_id,
        start_at=start,
        end_at=end,
        start_monotonic=None,
        end_monotonic=None,
        kind=kind,
        cadence_seconds=cadence,
        reason_codes=("transition_proven_by_persisted_event_samples",),
    )


def _relationship_phase_coherent(values: list[IntervalRelationship]) -> bool:
    period = values[0].candidate_period_seconds
    times = (values[0].left_at, *(item.right_at for item in values))
    _phase, spread = _phase_estimate(times, period)
    return spread <= candidate_phase_tolerance(period)


def _phase_estimate(times: Iterable[float], period: int) -> tuple[float, float]:
    phases = [float(value) % period for value in times]
    anchor = phases[0]
    residuals = [((value - anchor + period / 2) % period) - period / 2 for value in phases]
    center = statistics.median(residuals)
    estimate = (anchor + center) % period
    spread = max(
        abs(((value - estimate + period / 2) % period) - period / 2)
        for value in phases
    )
    return estimate, spread


def _unique_events(
    events: Iterable[PhysicalRestartEvent],
) -> tuple[PhysicalRestartEvent, ...]:
    by_id: dict[str, PhysicalRestartEvent] = {}
    for event in events:
        by_id.setdefault(event.event_id, event)
    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (_event_at(item) if _event_at(item) is not None else math.inf, item.sequence, item.event_id),
        )
    )


def _dedupe_spans(spans: Iterable[ContinuitySpan]) -> tuple[ContinuitySpan, ...]:
    values: dict[str, ContinuitySpan] = {}
    order: list[str] = []
    for span in spans:
        if span.reference_id not in values:
            values[span.reference_id] = span
            order.append(span.reference_id)
    return tuple(values[item] for item in order)


def _merge_intervals(values: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(values):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def _interval_gap_details(
    start_at: float,
    end_at: float,
    spans: Iterable[tuple[float, float, ContinuitySpanKind, float]],
) -> list[
    tuple[
        float,
        ContinuitySpanKind | None,
        float | None,
        ContinuitySpanKind | None,
        float | None,
    ]
]:
    """Return uncovered gaps plus the actual poll cadence on each edge."""

    ordered = sorted(item for item in spans if item[1] > item[0])
    if not ordered:
        return [(end_at - start_at, None, None, None, None)]
    values = []
    cursor = start_at
    left_kind: ContinuitySpanKind | None = None
    left_cadence: float | None = None
    for index, (span_start, span_end, kind, cadence) in enumerate(ordered):
        if span_start > cursor:
            values.append(
                (
                    span_start - cursor,
                    left_kind,
                    left_cadence,
                    kind,
                    cadence,
                )
            )
        if span_end > cursor:
            cursor = span_end
            left_kind = kind
            left_cadence = cadence
        if cursor >= end_at:
            break
    if cursor < end_at:
        values.append((end_at - cursor, left_kind, left_cadence, None, None))
    return values


def _event_at(event: PhysicalRestartEvent) -> float | None:
    value = event.canonical_phase_at
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return value


def _healthy_info(sample: ObservationSample) -> bool:
    return sample.info_status is InfoStatus.HEALTHY


def _healthy_complete(sample: ObservationSample) -> bool:
    return _healthy_info(sample) and sample.player_status is FieldStatus.PRESENT


def _qualifying_failure(sample: ObservationSample) -> bool:
    return sample.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _stable_id(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
