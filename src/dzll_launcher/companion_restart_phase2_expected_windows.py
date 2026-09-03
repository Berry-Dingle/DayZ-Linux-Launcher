from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, TypeAlias

from .companion_restart_phase2_continuity import (
    CONTINUITY_PROVENANCE_VERSION,
    ContinuitySpan,
    ContinuitySpanKind,
    assess_interval_continuity,
    legacy_coverage_spans,
)
from .companion_restart_phase2_detection import EventOutcome, PhysicalRestartEvent
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    CoverageKind,
    CoverageSegment,
    event_learning_eligible,
)


EXPECTED_WINDOW_SCHEMA_VERSION = 1
EXPECTED_WINDOW_SEMANTIC_VERSION = 2


class ExpectedWindowOutcome(str, Enum):
    HIT = "hit"
    GENUINE_MISS = "genuine_miss"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"
    RETRACTED_MISS = "retracted_miss"


class WindowCoverageClassification(str, Enum):
    COMPLETE_HEALTHY = "complete_healthy"
    OUTAGE_ACTIVITY = "outage_activity"
    INCOMPLETE = "incomplete"
    UNHEALTHY = "unhealthy"
    LIFECYCLE_GAP = "lifecycle_gap"
    UNMONITORED = "unmonitored"
    UNPROVEN = "continuity_unproven"
    LEGACY_V1_MISS = "legacy_v1_miss_claim"


class ReconciliationReason(str, Enum):
    FINALIZED_EVENT = "retracted_finalized_event"
    OBSERVED_OUTAGE = "retracted_observed_outage"


@dataclass(frozen=True)
class ExpectedWindowEpisodeEvidence:
    evidence_id: str
    start_at: float
    end_at: float | None
    outage_observed: bool
    recovery_observed: bool
    unresolved: bool
    restart_like: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _finite_nonnegative("start_at", self.start_at)
        if self.end_at is not None:
            _finite_nonnegative("end_at", self.end_at)
            if self.end_at < self.start_at:
                raise ValueError("episode evidence end cannot precede start")
        if not isinstance(self.evidence_id, str) or not self.evidence_id:
            raise ValueError("evidence_id must be a non-empty string")


@dataclass(frozen=True)
class ExpectedWindowRecord:
    schema_version: int
    semantic_version: int
    result_id: str
    server_key: str
    candidate_period_seconds: int
    expected_phase_offset: float
    window_start_at: float
    expected_at: float
    window_end_at: float
    model_revision_id: str
    regime_interpretation_id: str | None
    continuity_chain_id: str | None
    coverage_classification: WindowCoverageClassification
    observed_ratio: float
    largest_unexplained_gap: float
    blocker_kind: str | None
    healthy_throughout: bool
    outage_observed: bool
    overlapping_outage_episode_ids: tuple[str, ...]
    unresolved_episode: bool
    qualifying_finalized_event_id: str | None
    outcome: ExpectedWindowOutcome
    negative_penalty_active: bool
    reconciliation_reason: ReconciliationReason | None
    reconciled_at: float | None
    reconciliation_revision_id: str | None
    reason_codes: tuple[str, ...]
    supersedes_result_id: str | None

    def __post_init__(self) -> None:
        if self.schema_version != EXPECTED_WINDOW_SCHEMA_VERSION:
            raise ValueError("unsupported expected-window schema version")
        if self.semantic_version != EXPECTED_WINDOW_SEMANTIC_VERSION:
            raise ValueError("unsupported expected-window semantic version")
        for name in ("result_id", "server_key", "model_revision_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if self.candidate_period_seconds not in CANDIDATE_PERIODS:
            raise ValueError("unsupported candidate period")
        for name in (
            "expected_phase_offset",
            "window_start_at",
            "expected_at",
            "window_end_at",
            "observed_ratio",
            "largest_unexplained_gap",
        ):
            _finite_nonnegative(name, getattr(self, name))
        if not self.window_start_at <= self.expected_at <= self.window_end_at:
            raise ValueError("expected timestamp must lie inside its window")
        if not 0.0 <= self.observed_ratio <= 1.0:
            raise ValueError("observed_ratio must be within [0, 1]")
        if self.negative_penalty_active != (
            self.outcome is ExpectedWindowOutcome.GENUINE_MISS
        ):
            raise ValueError("only a genuine miss may carry an active penalty")
        if self.outcome is ExpectedWindowOutcome.RETRACTED_MISS:
            if self.reconciliation_reason is None or not self.supersedes_result_id:
                raise ValueError("retracted misses require a reason and superseded result")
            if self.reconciled_at is None or not self.reconciliation_revision_id:
                raise ValueError("retracted misses require reconciliation identity")


@dataclass(frozen=True)
class ExpectedWindowResult(ExpectedWindowRecord):
    """An immutable initial classification."""


@dataclass(frozen=True)
class ExpectedWindowRevision(ExpectedWindowRecord):
    """An immutable append-only replacement linked to an earlier result."""


ExpectedWindowLedgerRecord: TypeAlias = ExpectedWindowResult | ExpectedWindowRevision


@dataclass(frozen=True)
class ActiveMissLedger:
    active_results: tuple[ExpectedWindowLedgerRecord, ...]
    total_active_penalty: float
    reason_codes: tuple[str, ...]


_QUALIFYING_OUTCOMES = {
    EventOutcome.CONFIRMED_OFFLINE_RESTART,
    EventOutcome.CORROBORATED_OFFLINE_RESTART,
    EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
    EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
}

_OUTAGE_SPAN_KINDS = {
    ContinuitySpanKind.OFFLINE_OBSERVED,
    ContinuitySpanKind.HEALTHY_TO_FAILURE,
    ContinuitySpanKind.FAILURE_TO_HEALTHY,
}

_UI_SPAN_KINDS = {
    ContinuitySpanKind.UI_ROW_RECYCLED,
    ContinuitySpanKind.UI_SORTED,
    ContinuitySpanKind.UI_FILTERED,
    ContinuitySpanKind.UI_DOCKED,
    ContinuitySpanKind.UI_UNDOCKED,
    ContinuitySpanKind.UI_WIDGET_REPLACED,
}

_LIFECYCLE_SPAN_KINDS = {
    ContinuitySpanKind.PAUSE,
    ContinuitySpanKind.CLEAR,
    ContinuitySpanKind.SHUTDOWN,
    ContinuitySpanKind.SERVER_SWITCH,
    ContinuitySpanKind.MONITOR_REPLACEMENT,
    ContinuitySpanKind.STALE_POLL_REJECTION,
    ContinuitySpanKind.APP_STOPPED,
    ContinuitySpanKind.SLEEP_GAP,
    ContinuitySpanKind.CLOCK_REVERSAL,
}

_UNHEALTHY_SPAN_KINDS = {
    ContinuitySpanKind.QUERY_HEALTH_GAP,
    ContinuitySpanKind.MISSING_FIELD_GAP,
}


def deterministic_model_revision_id(
    server_key: str,
    candidate_period_seconds: int,
    expected_phase_offset: float,
    source_revision: str,
) -> str:
    return _stable_id(
        "expected-window-model-v2",
        server_key,
        candidate_period_seconds,
        round(expected_phase_offset, 6),
        source_revision,
    )


def classify_expected_window(
    *,
    server_key: str,
    candidate_period_seconds: int,
    expected_phase_offset: float,
    window_start_at: float,
    expected_at: float,
    window_end_at: float,
    model_revision_id: str,
    regime_interpretation_id: str | None = None,
    events: Iterable[PhysicalRestartEvent] = (),
    spans: Iterable[ContinuitySpan] = (),
    episodes: Iterable[ExpectedWindowEpisodeEvidence] = (),
    population_transition_observable: bool | None = None,
) -> ExpectedWindowResult:
    """Classify one completed expected window without consulting a model answer."""

    _validate_window_inputs(
        server_key,
        candidate_period_seconds,
        expected_phase_offset,
        window_start_at,
        expected_at,
        window_end_at,
        model_revision_id,
    )
    event_values = _unique_events(events)
    span_values = _unique_spans(spans)
    episode_values = _unique_episodes(episodes)
    diagnostics = _coverage_diagnostics(
        server_key=server_key,
        start_at=window_start_at,
        end_at=window_end_at,
        spans=span_values,
    )

    qualifying = tuple(
        event
        for event in event_values
        if _qualifying_event(event)
        and _event_overlaps(event, window_start_at, window_end_at)
    )
    outage_ids = set(
        span.reference_id
        for span in span_values
        if span.kind in _OUTAGE_SPAN_KINDS
        and _span_overlaps(span, window_start_at, window_end_at)
    )
    unresolved_span_ids = {
        span.reference_id
        for span in span_values
        if span.kind is ContinuitySpanKind.UNRESOLVED_EPISODE
        and _span_overlaps(span, window_start_at, window_end_at)
    }
    outage_ids.update(unresolved_span_ids)
    positive_outage_observed = bool(outage_ids - unresolved_span_ids)
    unresolved = bool(unresolved_span_ids)
    ambiguous_reasons: set[str] = set()
    if unresolved_span_ids:
        ambiguous_reasons.add("overlapping_unresolved_episode_coverage")
    for episode in episode_values:
        if not _episode_overlaps(episode, window_start_at, window_end_at):
            continue
        if episode.outage_observed or episode.recovery_observed or episode.restart_like:
            outage_ids.add(episode.evidence_id)
            ambiguous_reasons.update(episode.reason_codes)
            ambiguous_reasons.add("overlapping_restart_like_episode")
        if episode.outage_observed or episode.recovery_observed:
            positive_outage_observed = True
        unresolved = unresolved or episode.unresolved
        if episode.unresolved:
            outage_ids.add(episode.evidence_id)
            ambiguous_reasons.add("overlapping_unresolved_episode")
    for event in event_values:
        if event in qualifying or not _event_overlaps(
            event, window_start_at, window_end_at
        ):
            continue
        learning_eligible = event_learning_eligible(event)
        if _event_has_restart_like_activity(event):
            outage_ids.add(event.event_id)
            ambiguous_reasons.add("overlapping_restart_like_event")
            if learning_eligible and _event_has_direct_outage_or_recovery(event):
                positive_outage_observed = True
        if not learning_eligible or event.outcome in {
            EventOutcome.AMBIGUOUS_DRAIN,
            EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
            EventOutcome.INCOMPLETE,
            EventOutcome.EXPIRED,
        }:
            unresolved = True
            outage_ids.add(event.event_id)
            ambiguous_reasons.add("overlapping_unresolved_event")

    result_identity = (
        server_key,
        candidate_period_seconds,
        round(expected_phase_offset, 6),
        round(window_start_at, 6),
        round(expected_at, 6),
        round(window_end_at, 6),
        model_revision_id,
        regime_interpretation_id,
    )
    if qualifying:
        event = sorted(
            qualifying,
            key=lambda item: (_event_reference_at(item), item.event_id),
        )[0]
        reasons = {
            "classification_order_hit_first",
            "qualifying_finalized_event_overlap",
            *diagnostics[6],
        }
        return _initial_result(
            result_identity,
            continuity_chain_id=diagnostics[0],
            coverage_classification=diagnostics[1],
            observed_ratio=diagnostics[2],
            largest_unexplained_gap=diagnostics[3],
            blocker_kind=diagnostics[4],
            healthy_throughout=False,
            outage_observed=(
                positive_outage_observed
                or _event_has_direct_outage_or_recovery(event)
            ),
            outage_ids=tuple(sorted(outage_ids)),
            unresolved_episode=unresolved,
            finalized_event_id=event.event_id,
            outcome=ExpectedWindowOutcome.HIT,
            reasons=reasons,
        )

    if outage_ids or ambiguous_reasons or unresolved:
        reasons = {
            "classification_order_ambiguous_before_miss",
            *ambiguous_reasons,
            *diagnostics[6],
        }
        if outage_ids:
            reasons.add("observed_outage_or_recovery_overlap")
        return _initial_result(
            result_identity,
            continuity_chain_id=diagnostics[0],
            coverage_classification=WindowCoverageClassification.OUTAGE_ACTIVITY,
            observed_ratio=diagnostics[2],
            largest_unexplained_gap=diagnostics[3],
            blocker_kind=diagnostics[4],
            healthy_throughout=False,
            outage_observed=positive_outage_observed,
            outage_ids=tuple(sorted(outage_ids)),
            unresolved_episode=unresolved,
            finalized_event_id=None,
            outcome=ExpectedWindowOutcome.AMBIGUOUS,
            reasons=reasons,
        )

    healthy_proven = diagnostics[5]
    if healthy_proven and population_transition_observable is False:
        return _initial_result(
            result_identity,
            continuity_chain_id=diagnostics[0],
            coverage_classification=WindowCoverageClassification.COMPLETE_HEALTHY,
            observed_ratio=diagnostics[2],
            largest_unexplained_gap=diagnostics[3],
            blocker_kind=None,
            healthy_throughout=True,
            outage_observed=False,
            outage_ids=(),
            unresolved_episode=False,
            finalized_event_id=None,
            outcome=ExpectedWindowOutcome.UNKNOWN,
            reasons={
                "classification_order_population_observability_before_miss",
                "query_visible_population_transition_unobservable",
                *diagnostics[6],
            },
        )
    if healthy_proven:
        return _initial_result(
            result_identity,
            continuity_chain_id=diagnostics[0],
            coverage_classification=WindowCoverageClassification.COMPLETE_HEALTHY,
            observed_ratio=diagnostics[2],
            largest_unexplained_gap=diagnostics[3],
            blocker_kind=None,
            healthy_throughout=True,
            outage_observed=False,
            outage_ids=(),
            unresolved_episode=False,
            finalized_event_id=None,
            outcome=ExpectedWindowOutcome.GENUINE_MISS,
            reasons={
                "classification_order_genuine_miss_after_no_physical_activity",
                "complete_single_chain_healthy_observation",
                *diagnostics[6],
            },
        )

    return _initial_result(
        result_identity,
        continuity_chain_id=diagnostics[0],
        coverage_classification=diagnostics[1],
        observed_ratio=diagnostics[2],
        largest_unexplained_gap=diagnostics[3],
        blocker_kind=diagnostics[4],
        healthy_throughout=False,
        outage_observed=False,
        outage_ids=(),
        unresolved_episode=False,
        finalized_event_id=None,
        outcome=ExpectedWindowOutcome.UNKNOWN,
        reasons={"classification_order_unknown_fallback", *diagnostics[6]},
    )


def import_legacy_expected_miss(
    *,
    server_key: str,
    candidate_period_seconds: int,
    expected_phase_offset: float,
    window_start_at: float,
    expected_at: float,
    window_end_at: float,
    model_revision_id: str,
    legacy_key: str,
    regime_interpretation_id: str | None = None,
) -> ExpectedWindowResult:
    """Represent a schema-3 miss claim without asserting v2 continuity proof."""

    _validate_window_inputs(
        server_key,
        candidate_period_seconds,
        expected_phase_offset,
        window_start_at,
        expected_at,
        window_end_at,
        model_revision_id,
    )
    result_id = _stable_id(
        "legacy-expected-window-miss-v2",
        server_key,
        candidate_period_seconds,
        round(expected_at, 6),
        legacy_key,
        model_revision_id,
    )
    return ExpectedWindowResult(
        schema_version=EXPECTED_WINDOW_SCHEMA_VERSION,
        semantic_version=EXPECTED_WINDOW_SEMANTIC_VERSION,
        result_id=result_id,
        server_key=server_key,
        candidate_period_seconds=candidate_period_seconds,
        expected_phase_offset=expected_phase_offset,
        window_start_at=window_start_at,
        expected_at=expected_at,
        window_end_at=window_end_at,
        model_revision_id=model_revision_id,
        regime_interpretation_id=regime_interpretation_id,
        continuity_chain_id=None,
        coverage_classification=WindowCoverageClassification.LEGACY_V1_MISS,
        observed_ratio=0.0,
        largest_unexplained_gap=window_end_at - window_start_at,
        blocker_kind="legacy_v1_claim_requires_v2_revalidation",
        healthy_throughout=False,
        outage_observed=False,
        overlapping_outage_episode_ids=(),
        unresolved_episode=False,
        qualifying_finalized_event_id=None,
        outcome=ExpectedWindowOutcome.GENUINE_MISS,
        negative_penalty_active=True,
        reconciliation_reason=None,
        reconciled_at=None,
        reconciliation_revision_id=None,
        reason_codes=("imported_legacy_v1_miss_claim",),
        supersedes_result_id=None,
    )


def reconcile_expected_window_ledger(
    records: Iterable[ExpectedWindowLedgerRecord],
    classifications: Iterable[ExpectedWindowResult],
    *,
    reconciled_at: float,
    evidence_revision_id: str,
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    """Append deterministic retractions; never mutate or delete history."""

    _finite_nonnegative("reconciled_at", reconciled_at)
    if not isinstance(evidence_revision_id, str) or not evidence_revision_id:
        raise ValueError("evidence_revision_id must be a non-empty string")
    ledger = list(_unique_records(records))
    current_by_window = {
        _window_key(item): item for item in _unique_records(classifications)
    }
    existing_ids = {item.result_id for item in ledger}
    active = _latest_records(ledger)
    revisions: list[ExpectedWindowRevision] = []
    for latest in active:
        if (
            latest.outcome is not ExpectedWindowOutcome.GENUINE_MISS
            or not latest.negative_penalty_active
        ):
            continue
        desired = current_by_window.get(_window_key(latest))
        if desired is None:
            continue
        reason = None
        if desired.outcome is ExpectedWindowOutcome.HIT:
            reason = ReconciliationReason.FINALIZED_EVENT
        elif (
            desired.outcome is ExpectedWindowOutcome.AMBIGUOUS
            and desired.outage_observed
        ):
            reason = ReconciliationReason.OBSERVED_OUTAGE
        if reason is None:
            continue
        root_id = _lineage_root_id(latest, ledger)
        revision_id = _stable_id(
            "expected-window-revision-v2",
            root_id,
            latest.result_id,
            reason.value,
            desired.qualifying_finalized_event_id,
            desired.overlapping_outage_episode_ids,
            evidence_revision_id,
        )
        if revision_id in existing_ids:
            continue
        revision = ExpectedWindowRevision(
            schema_version=EXPECTED_WINDOW_SCHEMA_VERSION,
            semantic_version=EXPECTED_WINDOW_SEMANTIC_VERSION,
            result_id=revision_id,
            server_key=latest.server_key,
            candidate_period_seconds=latest.candidate_period_seconds,
            expected_phase_offset=latest.expected_phase_offset,
            window_start_at=latest.window_start_at,
            expected_at=latest.expected_at,
            window_end_at=latest.window_end_at,
            model_revision_id=latest.model_revision_id,
            regime_interpretation_id=latest.regime_interpretation_id,
            continuity_chain_id=desired.continuity_chain_id,
            coverage_classification=desired.coverage_classification,
            observed_ratio=desired.observed_ratio,
            largest_unexplained_gap=desired.largest_unexplained_gap,
            blocker_kind=desired.blocker_kind,
            healthy_throughout=False,
            outage_observed=desired.outage_observed,
            overlapping_outage_episode_ids=desired.overlapping_outage_episode_ids,
            unresolved_episode=desired.unresolved_episode,
            qualifying_finalized_event_id=desired.qualifying_finalized_event_id,
            outcome=ExpectedWindowOutcome.RETRACTED_MISS,
            negative_penalty_active=False,
            reconciliation_reason=reason,
            reconciled_at=reconciled_at,
            reconciliation_revision_id=revision_id,
            reason_codes=tuple(
                sorted(
                    {
                        *desired.reason_codes,
                        reason.value,
                        "append_only_reconciliation_revision",
                    }
                )
            ),
            supersedes_result_id=latest.result_id,
        )
        revisions.append(revision)
        existing_ids.add(revision_id)
    return tuple((*ledger, *sorted(revisions, key=_record_sort_key)))


def derive_active_miss_ledger(
    records: Iterable[ExpectedWindowLedgerRecord],
) -> ActiveMissLedger:
    values = _unique_records(records)
    superseded = {
        item.supersedes_result_id
        for item in values
        if item.supersedes_result_id is not None
    }
    latest = tuple(
        item
        for item in values
        if item.result_id not in superseded
        and item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
    )
    return ActiveMissLedger(
        active_results=tuple(sorted(latest, key=_record_sort_key)),
        total_active_penalty=float(len(latest)),
        reason_codes=(
            "latest_lineage_revision_only",
            "retracted_and_non_miss_outcomes_zero_penalty",
        ),
    )


def continuity_spans_from_coverage(
    coverage: Iterable[CoverageSegment],
    *,
    server_key: str,
    monitoring_sessions: Iterable[Mapping[str, object]] = (),
    window_start_at: float | None = None,
    window_end_at: float | None = None,
) -> tuple[ContinuitySpan, ...]:
    """Adapt explicit coverage or conservatively reconstruct schema-3 spans."""

    segments = tuple(coverage)
    explicit = []
    legacy = []
    for segment in segments:
        if (
            segment.provenance_version >= CONTINUITY_PROVENANCE_VERSION
            and segment.server_key == server_key
            and segment.app_session_id
            and segment.monitoring_session_id
            and segment.poll_generation is not None
            and segment.continuity_chain_id
        ):
            explicit.append(_span_from_coverage_segment(segment))
        else:
            legacy.append(segment)
    values = list(explicit)
    if legacy:
        for session in monitoring_sessions:
            if not isinstance(session, Mapping):
                continue
            started = _finite_or_none(session.get("started_at"))
            ended = _finite_or_none(session.get("ended_at"))
            if started is None:
                continue
            effective_end = math.inf if ended is None else ended
            if window_start_at is not None and effective_end < window_start_at:
                continue
            if window_end_at is not None and started > window_end_at:
                continue
            values.extend(
                legacy_coverage_spans(
                    legacy,
                    server_key=server_key,
                    monitoring_session=session,
                )
            )
    return tuple(
        sorted(
            {item.reference_id: item for item in values}.values(),
            key=lambda item: (item.start_at, item.end_at, item.reference_id),
        )
    )


def _coverage_diagnostics(
    *,
    server_key: str,
    start_at: float,
    end_at: float,
    spans: tuple[ContinuitySpan, ...],
) -> tuple[
    str | None,
    WindowCoverageClassification,
    float,
    float,
    str | None,
    bool,
    tuple[str, ...],
]:
    overlapping = tuple(
        item
        for item in spans
        if _span_overlaps(item, start_at, end_at)
        and item.kind not in _UI_SPAN_KINDS
    )
    if not overlapping:
        return (
            None,
            WindowCoverageClassification.UNMONITORED,
            0.0,
            end_at - start_at,
            "unmonitored",
            False,
            ("expected_window_unmonitored",),
        )
    identities = {
        (
            item.app_session_id,
            item.monitoring_session_id,
            item.poll_generation,
            item.continuity_chain_id,
        )
        for item in overlapping
        if item.kind not in _UI_SPAN_KINDS
    }
    provenance = all(
        item.provenance_version >= CONTINUITY_PROVENANCE_VERSION
        and item.server_key == server_key
        and item.app_session_id
        and item.monitoring_session_id
        and item.poll_generation is not None
        and item.continuity_chain_id
        for item in overlapping
    )
    if not provenance or len(identities) != 1:
        ratio = _observed_ratio(start_at, end_at, overlapping)
        return (
            None,
            WindowCoverageClassification.UNPROVEN,
            ratio,
            end_at - start_at if ratio == 0 else 0.0,
            "historical_continuity_unproven",
            False,
            ("historical_continuity_unproven",),
        )
    app_id, monitor_id, poll, chain_id = next(iter(identities))
    assessment = assess_interval_continuity(
        start_at=start_at,
        end_at=end_at,
        server_key=server_key,
        app_session_id=app_id,
        monitoring_session_id=monitor_id,
        poll_generation=poll,
        continuity_chain_id=chain_id,
        spans=overlapping,
    )
    kinds = {item.kind for item in overlapping}
    healthy_only = kinds == {ContinuitySpanKind.ONLINE_HEALTHY}
    healthy_proven = assessment.continuity_qualified and healthy_only
    if healthy_proven:
        classification = WindowCoverageClassification.COMPLETE_HEALTHY
    elif kinds.intersection(_OUTAGE_SPAN_KINDS):
        classification = WindowCoverageClassification.OUTAGE_ACTIVITY
    elif kinds.intersection(_LIFECYCLE_SPAN_KINDS):
        classification = WindowCoverageClassification.LIFECYCLE_GAP
    elif kinds.intersection(_UNHEALTHY_SPAN_KINDS):
        classification = WindowCoverageClassification.UNHEALTHY
    else:
        classification = WindowCoverageClassification.INCOMPLETE
    reasons = set(assessment.reason_codes)
    if healthy_proven:
        reasons.add("healthy_throughout_proven")
    else:
        reasons.add("healthy_throughout_not_proven")
    return (
        chain_id,
        classification,
        assessment.observed_ratio,
        assessment.largest_unexplained_gap,
        assessment.blocker_kind,
        healthy_proven,
        tuple(sorted(reasons)),
    )


def _initial_result(
    identity: tuple[object, ...],
    *,
    continuity_chain_id: str | None,
    coverage_classification: WindowCoverageClassification,
    observed_ratio: float,
    largest_unexplained_gap: float,
    blocker_kind: str | None,
    healthy_throughout: bool,
    outage_observed: bool,
    outage_ids: tuple[str, ...],
    unresolved_episode: bool,
    finalized_event_id: str | None,
    outcome: ExpectedWindowOutcome,
    reasons: Iterable[str],
) -> ExpectedWindowResult:
    (
        server_key,
        period,
        phase,
        window_start,
        expected,
        window_end,
        model_revision,
        regime_id,
    ) = identity
    result_id = _stable_id("expected-window-result-v2", *identity, outcome.value)
    return ExpectedWindowResult(
        schema_version=EXPECTED_WINDOW_SCHEMA_VERSION,
        semantic_version=EXPECTED_WINDOW_SEMANTIC_VERSION,
        result_id=result_id,
        server_key=str(server_key),
        candidate_period_seconds=int(period),
        expected_phase_offset=float(phase),
        window_start_at=float(window_start),
        expected_at=float(expected),
        window_end_at=float(window_end),
        model_revision_id=str(model_revision),
        regime_interpretation_id=(str(regime_id) if regime_id is not None else None),
        continuity_chain_id=continuity_chain_id,
        coverage_classification=coverage_classification,
        observed_ratio=round(observed_ratio, 6),
        largest_unexplained_gap=round(largest_unexplained_gap, 6),
        blocker_kind=blocker_kind,
        healthy_throughout=healthy_throughout,
        outage_observed=outage_observed,
        overlapping_outage_episode_ids=tuple(sorted(set(outage_ids))),
        unresolved_episode=unresolved_episode,
        qualifying_finalized_event_id=finalized_event_id,
        outcome=outcome,
        negative_penalty_active=outcome is ExpectedWindowOutcome.GENUINE_MISS,
        reconciliation_reason=None,
        reconciled_at=None,
        reconciliation_revision_id=None,
        reason_codes=tuple(sorted(set(reasons))),
        supersedes_result_id=None,
    )


def _span_from_coverage_segment(segment: CoverageSegment) -> ContinuitySpan:
    kind = {
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
    }.get(segment.kind, ContinuitySpanKind.PARTIAL)
    return ContinuitySpan(
        provenance_version=segment.provenance_version,
        reference_id=_stable_id(
            "coverage-span-v2",
            segment.server_key,
            segment.monitoring_session_id,
            segment.continuity_chain_id,
            round(segment.start_at, 6),
            round(segment.end_at, 6),
            kind.value,
        ),
        server_key=segment.server_key or "",
        app_session_id=segment.app_session_id,
        monitoring_session_id=segment.monitoring_session_id,
        poll_generation=segment.poll_generation,
        continuity_chain_id=segment.continuity_chain_id,
        start_at=segment.start_at,
        end_at=segment.end_at,
        start_monotonic=None,
        end_monotonic=None,
        kind=kind,
        cadence_seconds=segment.cadence,
        reason_codes=("explicit_coverage_provenance",),
    )


def _qualifying_event(event: PhysicalRestartEvent) -> bool:
    return bool(
        event_learning_eligible(event)
        and event.outcome in _QUALIFYING_OUTCOMES
        and event.authenticity >= 0.50
    )


def _event_has_restart_like_activity(event: PhysicalRestartEvent) -> bool:
    return bool(
        event.outage.first_failure_at is not None
        or event.outage.confirmed_offline_at is not None
        or event.outage.info_return_at is not None
        or event.outage.failure_count > 0
        or event.recovery.stable_recovery_at is not None
        or event.outcome
        in {
            EventOutcome.AMBIGUOUS_DRAIN,
            EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
            EventOutcome.INCOMPLETE,
            EventOutcome.EXPIRED,
            EventOutcome.SERVICE_INTERRUPTION,
        }
    )


def _event_has_direct_outage_or_recovery(event: PhysicalRestartEvent) -> bool:
    return bool(
        event.outage.first_failure_at is not None
        or event.outage.confirmed_offline_at is not None
        or event.outage.info_return_at is not None
        or event.outage.failure_count > 0
        or event.recovery.stable_recovery_at is not None
    )


def _event_overlaps(event: PhysicalRestartEvent, start_at: float, end_at: float) -> bool:
    starts = [
        value
        for value in (
            event.episode_started_at,
            event.outage.first_failure_at,
            event.outage.confirmed_offline_at,
            event.canonical_phase_at,
        )
        if value is not None
    ]
    ends = [
        value
        for value in (
            event.recovery.stable_recovery_at,
            event.outage.info_return_at,
            event.canonical_phase_at,
            event.finalized_at,
        )
        if value is not None
    ]
    event_start = min(starts, default=event.episode_started_at)
    event_end = max(ends, default=event.finalized_at)
    return event_end >= start_at and event_start <= end_at


def _event_reference_at(event: PhysicalRestartEvent) -> float:
    return float(
        event.canonical_phase_at
        if event.canonical_phase_at is not None
        else event.episode_started_at
    )


def _span_overlaps(span: ContinuitySpan, start_at: float, end_at: float) -> bool:
    return (
        span.end_at > start_at and span.start_at < end_at
    ) or (
        span.start_at == span.end_at and start_at <= span.start_at <= end_at
    )


def _episode_overlaps(
    episode: ExpectedWindowEpisodeEvidence, start_at: float, end_at: float
) -> bool:
    episode_end = math.inf if episode.end_at is None else episode.end_at
    return episode_end >= start_at and episode.start_at <= end_at


def _observed_ratio(
    start_at: float, end_at: float, spans: Iterable[ContinuitySpan]
) -> float:
    intervals = sorted(
        (max(start_at, item.start_at), min(end_at, item.end_at))
        for item in spans
        if item.end_at > start_at and item.start_at < end_at
    )
    merged: list[list[float]] = []
    for left, right in intervals:
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    covered = sum(right - left for left, right in merged)
    return round(min(1.0, covered / (end_at - start_at)), 6)


def _window_key(record: ExpectedWindowRecord) -> tuple[str, int, int]:
    return (
        record.server_key,
        record.candidate_period_seconds,
        int(round(record.expected_at)),
    )


def _latest_records(
    records: Iterable[ExpectedWindowLedgerRecord],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values = _unique_records(records)
    superseded = {
        item.supersedes_result_id
        for item in values
        if item.supersedes_result_id is not None
    }
    return tuple(item for item in values if item.result_id not in superseded)


def _lineage_root_id(
    record: ExpectedWindowLedgerRecord,
    records: Iterable[ExpectedWindowLedgerRecord],
) -> str:
    by_id = {item.result_id: item for item in records}
    current = record
    visited = set()
    while current.supersedes_result_id and current.supersedes_result_id in by_id:
        if current.result_id in visited:
            raise ValueError("expected-window revision cycle")
        visited.add(current.result_id)
        current = by_id[current.supersedes_result_id]
    return current.result_id


def _unique_records(
    records: Iterable[ExpectedWindowLedgerRecord],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values: dict[str, ExpectedWindowLedgerRecord] = {}
    for item in records:
        if not isinstance(item, (ExpectedWindowResult, ExpectedWindowRevision)):
            raise ValueError("ledger contains an invalid record")
        values.setdefault(item.result_id, item)
    return tuple(values.values())


def _unique_events(
    events: Iterable[PhysicalRestartEvent],
) -> tuple[PhysicalRestartEvent, ...]:
    values = {}
    for item in events:
        values.setdefault(item.event_id, item)
    return tuple(values.values())


def _unique_spans(spans: Iterable[ContinuitySpan]) -> tuple[ContinuitySpan, ...]:
    values = {}
    for item in spans:
        values.setdefault(item.reference_id, item)
    return tuple(values.values())


def _unique_episodes(
    episodes: Iterable[ExpectedWindowEpisodeEvidence],
) -> tuple[ExpectedWindowEpisodeEvidence, ...]:
    values = {}
    for item in episodes:
        values.setdefault(item.evidence_id, item)
    return tuple(values.values())


def _record_sort_key(record: ExpectedWindowRecord) -> tuple[object, ...]:
    return (
        record.expected_at,
        record.candidate_period_seconds,
        record.reconciled_at if record.reconciled_at is not None else -1.0,
        record.result_id,
    )


def _validate_window_inputs(
    server_key: str,
    period: int,
    phase: float,
    start: float,
    expected: float,
    end: float,
    model_revision_id: str,
) -> None:
    if not isinstance(server_key, str) or not server_key:
        raise ValueError("server_key must be a non-empty string")
    if period not in CANDIDATE_PERIODS:
        raise ValueError("unsupported candidate period")
    for name, value in (
        ("expected_phase_offset", phase),
        ("window_start_at", start),
        ("expected_at", expected),
        ("window_end_at", end),
    ):
        _finite_nonnegative(name, value)
    if not start <= expected <= end or end <= start:
        raise ValueError("invalid expected window")
    if not isinstance(model_revision_id, str) or not model_revision_id:
        raise ValueError("model_revision_id must be a non-empty string")


def _finite_nonnegative(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _stable_id(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
