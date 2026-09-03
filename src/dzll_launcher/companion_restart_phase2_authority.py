from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from .companion_restart_phase2_continuity import (
    CadenceStreak,
    IntervalRelationship,
    RelationshipKind,
)
from .companion_restart_phase2_detection import EventOutcome, PhysicalRestartEvent
from .companion_restart_phase2_expected_windows import (
    ExpectedWindowLedgerRecord,
    ExpectedWindowOutcome,
)
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    CandidateScore,
    ScheduleScore,
    candidate_phase_tolerance,
    event_learning_eligible,
)


AUTHORITY_SCHEMA_VERSION = 1
AUTHORITY_SEMANTIC_VERSION = 1
H1_AUTHORITY_MINIMUM = 0.45
H2_AUTHORITY_MINIMUM = 0.72
H2_PHASE_AUTHORITY_MINIMUM = 0.70
H3_AUTHORITY_MINIMUM = 0.88
H3_PHASE_AUTHORITY_MINIMUM = 0.85
H4_AUTHORITY_MINIMUM = 0.94
HIGH_AUTHORITY_CAP = 0.97


class AuthorityGate(str, Enum):
    NONE = "none"
    NORMAL = "normal"
    H1 = "h1_strong_clue"
    H2 = "h2_likely"
    H3 = "h3_established"
    H4 = "h4_durable"


class AuthorityOrigin(str, Enum):
    NORMAL = "normal"
    CONTINUITY = "continuity"


class RegimeState(str, Enum):
    UNKNOWN = "unknown"
    NORMAL_LEARNING = "normal_learning"
    LIKELY = "likely"
    ESTABLISHED = "established"
    CHANGE_SUSPECTED = "change_suspected"
    CHALLENGER_ACCUMULATING = "challenger_accumulating"
    TRANSITION_CONFIRMED = "transition_confirmed"
    NEW_REGIME_PROVISIONAL = "new_regime_provisional"
    NEW_REGIME_ESTABLISHED = "new_regime_established"


class RegimeRecordStatus(str, Enum):
    LIKELY = "likely"
    ESTABLISHED = "established"
    PROVISIONAL = "provisional"
    SUPERSEDED = "superseded"
    HISTORICAL = "historical"


class ChallengerAuthorization(str, Enum):
    LATENT = "latent"
    AUTHORIZED = "authorized_by_h2"
    ESTABLISHED = "established_by_h3"
    REJECTED = "rejected"


class ChallengerStatus(str, Enum):
    ACCUMULATING = "accumulating"
    TRANSITION_CONFIRMED = "transition_confirmed"
    PROVISIONAL = "provisional"
    ESTABLISHED = "established"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class NormalCandidateEvidence:
    candidate_period_seconds: int
    phase_offset: float
    fundamental_confidence: float
    phase_confidence: float
    aligned_normal_confidence: float
    aligned_relationship_ids: tuple[str, ...]
    compatible_multiple_relationship_ids: tuple[str, ...]
    off_phase_event_ids: tuple[str, ...]
    active_genuine_miss_ids: tuple[str, ...]
    compatible_hit_ids: tuple[str, ...]
    neutral_window_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class NormalEvidenceLedger:
    schema_version: int
    semantic_version: int
    server_key: str
    candidates: tuple[NormalCandidateEvidence, ...]
    active_genuine_miss_ids: tuple[str, ...]
    neutral_window_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityCandidate:
    schema_version: int
    semantic_version: int
    candidate_id: str
    regime_id: str
    server_key: str
    candidate_period_seconds: int
    phase_offset: float
    phase_spread: float
    phase_tolerance: float
    high_relationship_ids: tuple[str, ...]
    maximal_streak_ids: tuple[str, ...]
    relationship_quality_values: tuple[tuple[str, float], ...]
    base_authority: float
    streak_bonus: float
    streak_bonus_values: tuple[tuple[str, float], ...]
    high_authority: float
    phase_quality: float
    high_phase_authority: float
    aligned_normal_confidence: float
    combined_confidence: float
    h1_gate: bool
    h2_gate: bool
    h3_gate: bool
    h4_gate: bool
    harmonic_blockers: tuple[str, ...]
    defining_evidence_ids: tuple[str, ...]
    first_evidence_at: float
    last_evidence_at: float
    reason_codes: tuple[str, ...]

    @property
    def highest_gate(self) -> AuthorityGate:
        if self.h4_gate:
            return AuthorityGate.H4
        if self.h3_gate:
            return AuthorityGate.H3
        if self.h2_gate:
            return AuthorityGate.H2
        if self.h1_gate:
            return AuthorityGate.H1
        return AuthorityGate.NONE


@dataclass(frozen=True)
class HighEvidenceLedger:
    schema_version: int
    semantic_version: int
    server_key: str
    high_relationship_ids: tuple[str, ...]
    maximal_streak_ids: tuple[str, ...]
    candidates: tuple[AuthorityCandidate, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ChallengerContext:
    schema_version: int
    semantic_version: int
    context_id: str
    server_key: str
    incumbent_regime_id: str
    proposed_regime_id: str
    candidate_period_seconds: int
    phase_offset: float
    phase_tolerance: float
    normal_relationship_ids: tuple[str, ...]
    off_phase_event_ids: tuple[str, ...]
    active_genuine_miss_ids: tuple[str, ...]
    high_relationship_ids: tuple[str, ...]
    streak_ids: tuple[str, ...]
    normal_context_strength: float
    high_authority: float
    high_confirmation_present: bool
    authorization_state: ChallengerAuthorization
    regime_change_status: ChallengerStatus
    first_evidence_at: float
    last_evidence_at: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RegimeRecord:
    schema_version: int
    semantic_version: int
    regime_id: str
    server_key: str
    candidate_period_seconds: int
    phase_offset: float
    phase_tolerance: float
    origin: AuthorityOrigin
    authority_gate: AuthorityGate
    high_authority: float
    phase_authority: float
    combined_confidence: float
    status: RegimeRecordStatus
    defining_evidence_ids: tuple[str, ...]
    established_at: float | None
    superseded_at: float | None
    superseded_by_id: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityDecision:
    schema_version: int
    semantic_version: int
    decision_id: str
    server_key: str
    selected_shadow_regime: RegimeRecord | None
    incumbent_shadow_regime: RegimeRecord | None
    strongest_challenger: ChallengerContext | None
    regimes: tuple[RegimeRecord, ...]
    candidates: tuple[AuthorityCandidate, ...]
    challenger_contexts: tuple[ChallengerContext, ...]
    normal_ledger: NormalEvidenceLedger
    high_ledger: HighEvidenceLedger
    state: RegimeState
    cycle_visible: bool
    prediction_usable: bool
    countdown_safe: bool
    suspicion_level: int
    active_suspicion_evidence_ids: tuple[str, ...]
    normal_confidence: float
    high_confidence: float
    combined_confidence: float
    reason_codes: tuple[str, ...]


def evaluate_authority(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    relationships: Iterable[IntervalRelationship] = (),
    streaks: Iterable[CadenceStreak] = (),
    expected_window_ledger: Iterable[ExpectedWindowLedgerRecord] = (),
    events: Iterable[PhysicalRestartEvent] = (),
    prior_decision: AuthorityDecision | None = None,
) -> AuthorityDecision:
    """Build one immutable shadow decision without mutating any input state."""

    if not isinstance(server_key, str) or not server_key:
        raise ValueError("server_key must be a non-empty string")
    relationship_values = _unique_relationships(relationships, server_key)
    streak_values = _unique_streaks(streaks, server_key)
    event_values = _unique_events(events, server_key)
    window_values = _latest_window_records(expected_window_ledger, server_key)
    candidates, normal_ledger, high_ledger = _build_ledgers(
        server_key=server_key,
        normal_score=normal_score,
        relationships=relationship_values,
        streaks=streak_values,
        windows=window_values,
        events=event_values,
    )
    return _transition_decision(
        server_key=server_key,
        normal_score=normal_score,
        candidates=candidates,
        normal_ledger=normal_ledger,
        high_ledger=high_ledger,
        windows=window_values,
        events=event_values,
        prior=prior_decision,
    )


def relationship_authority_quality(
    relationship: IntervalRelationship,
    events: Iterable[PhysicalRestartEvent],
) -> float:
    """Recompute the normative Stage 2B1 relationship quality formula."""

    by_id = {item.event_id: item for item in events}
    left = by_id.get(relationship.left_event_id)
    right = by_id.get(relationship.right_event_id)
    if (
        not relationship.high_authority_eligible
        or relationship.relationship_kind is not RelationshipKind.DIRECT
        or left is None
        or right is None
        or relationship.tolerance_seconds <= 0
    ):
        return 0.0
    auth_quality = min(1.0, min(left.authenticity, right.authenticity) / 0.85)
    coverage_quality = min(1.0, relationship.observed_ratio / 0.98)
    fit_fraction = min(
        1.0,
        abs(relationship.signed_residual_seconds)
        / relationship.tolerance_seconds,
    )
    phase_fit = 1.0 - 0.20 * fit_fraction
    return round(min(auth_quality, coverage_quality, phase_fit), 6)


def _build_ledgers(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    relationships: tuple[IntervalRelationship, ...],
    streaks: tuple[CadenceStreak, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
) -> tuple[
    tuple[AuthorityCandidate, ...], NormalEvidenceLedger, HighEvidenceLedger
]:
    high = tuple(
        item
        for item in relationships
        if item.high_authority_eligible
        and item.relationship_kind is RelationshipKind.DIRECT
    )
    event_map = {item.event_id: item for item in events}
    candidate_values: list[AuthorityCandidate] = []
    normal_values: list[NormalCandidateEvidence] = []

    for period in CANDIDATE_PERIODS:
        period_high = tuple(
            item for item in high if item.candidate_period_seconds == period
        )
        components = _phase_components(period_high, period)
        for component in components:
            associated_streaks = tuple(
                item
                for item in streaks
                if item.candidate_period_seconds == period
                and set(item.relationship_ids).issubset(
                    {value.relationship_id for value in component}
                )
            )
            candidate, normal = _authority_candidate(
                server_key=server_key,
                period=period,
                component=component,
                streaks=associated_streaks,
                all_relationships=relationships,
                normal_candidate=normal_score.candidate(period),
                windows=windows,
                events=events,
                event_map=event_map,
            )
            candidate_values.append(candidate)
            normal_values.append(normal)

        normal_candidate = normal_score.candidate(period)
        if normal_candidate.phase_offset is None:
            continue
        has_normal_evidence = bool(
            normal_candidate.fundamental_period_confidence > 0
            or normal_candidate.phase_confidence > 0
            or any(item.candidate_period_seconds == period for item in relationships)
        )
        if not has_normal_evidence:
            continue
        if any(
            _circular_distance(
                item.phase_offset, normal_candidate.phase_offset, period
            )
            <= item.phase_tolerance
            for item in candidate_values
            if item.candidate_period_seconds == period
        ):
            continue
        candidate, normal = _normal_only_candidate(
            server_key=server_key,
            normal_candidate=normal_candidate,
            relationships=relationships,
            windows=windows,
            events=events,
        )
        candidate_values.append(candidate)
        normal_values.append(normal)

    candidates = tuple(sorted(candidate_values, key=_candidate_sort_key))
    normal_candidates = tuple(
        sorted(
            normal_values,
            key=lambda item: (item.candidate_period_seconds, item.phase_offset),
        )
    )
    active_misses = tuple(
        sorted(
            item.result_id
            for item in windows
            if item.outcome is ExpectedWindowOutcome.GENUINE_MISS
            and item.negative_penalty_active
        )
    )
    neutral_windows = tuple(
        sorted(
            item.result_id
            for item in windows
            if item.outcome
            in {
                ExpectedWindowOutcome.AMBIGUOUS,
                ExpectedWindowOutcome.UNKNOWN,
                ExpectedWindowOutcome.RETRACTED_MISS,
            }
        )
    )
    normal_ledger = NormalEvidenceLedger(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        server_key=server_key,
        candidates=normal_candidates,
        active_genuine_miss_ids=active_misses,
        neutral_window_ids=neutral_windows,
        reason_codes=(
            "normal_evidence_separate_from_high_authority",
            "ambiguous_unknown_retracted_windows_neutral",
            "normal_negative_evidence_not_subtracted_from_high_authority",
        ),
    )
    high_ledger = HighEvidenceLedger(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        server_key=server_key,
        high_relationship_ids=tuple(
            sorted(item.relationship_id for item in high)
        ),
        maximal_streak_ids=tuple(sorted(item.streak_id for item in streaks)),
        candidates=tuple(item for item in candidates if item.high_relationship_ids),
        reason_codes=(
            "high_ledger_direct_continuity_relationships_only",
            "maximal_streaks_only",
            "incumbent_independent_high_evidence",
        ),
    )
    return candidates, normal_ledger, high_ledger


def _authority_candidate(
    *,
    server_key: str,
    period: int,
    component: tuple[IntervalRelationship, ...],
    streaks: tuple[CadenceStreak, ...],
    all_relationships: tuple[IntervalRelationship, ...],
    normal_candidate: CandidateScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    event_map: dict[str, PhysicalRestartEvent],
) -> tuple[AuthorityCandidate, NormalCandidateEvidence]:
    tolerance = candidate_phase_tolerance(period)
    quality_values = tuple(
        (item.relationship_id, relationship_authority_quality(item, events))
        for item in component
    )
    quality_map = dict(quality_values)
    base = 1.0
    for _relationship_id, quality in quality_values:
        base *= 1.0 - 0.50 * quality
    base = 1.0 - base
    chosen_streaks = _non_overlapping_streaks(streaks)
    streak_bonus_values = []
    for streak in chosen_streaks:
        minimum_quality = min(
            quality_map[item] for item in streak.relationship_ids
        )
        bonus = (
            0.08
            * (1.0 - 2.0 ** -(streak.interval_count - 1))
            * minimum_quality
        )
        streak_bonus_values.append((streak.streak_id, round(bonus, 6)))
    streak_bonus = sum(value for _identity, value in streak_bonus_values)
    high_authority = min(HIGH_AUTHORITY_CAP, base + streak_bonus)

    phase_streak = _phase_defining_streak(chosen_streaks)
    if phase_streak is not None:
        phase_offset = phase_streak.phase_estimate % period
        phase_spread = phase_streak.phase_spread
    else:
        phase_offset, phase_spread = _relationship_phase(component, period)
    phase_quality = max(0.0, 1.0 - phase_spread / tolerance)
    phase_authority = high_authority * phase_quality
    normal = _normal_candidate_evidence(
        period=period,
        phase_offset=phase_offset,
        tolerance=tolerance,
        normal_candidate=normal_candidate,
        relationships=all_relationships,
        windows=windows,
        events=events,
    )
    aligned_normal = normal.aligned_normal_confidence
    combined = high_authority + (1.0 - high_authority) * min(
        0.10, 0.10 * aligned_normal
    )

    h2_harmonic, h3_harmonic = _harmonic_blockers(
        period, normal_candidate, windows
    )
    h1 = len(component) >= 1 and high_authority >= H1_AUTHORITY_MINIMUM
    has_h2_streak = any(
        item.interval_count >= 2 and item.event_count >= 3
        for item in chosen_streaks
    )
    h2 = bool(
        has_h2_streak
        and high_authority >= H2_AUTHORITY_MINIMUM
        and phase_authority >= H2_PHASE_AUTHORITY_MINIMUM
        and not h2_harmonic
    )
    h3_structure = bool(
        any(item.interval_count >= 3 for item in chosen_streaks)
        or (has_h2_streak and len(component) >= 3)
    )
    h3 = bool(
        h3_structure
        and high_authority >= H3_AUTHORITY_MINIMUM
        and phase_authority >= H3_PHASE_AUTHORITY_MINIMUM
        and not h3_harmonic
    )
    h4 = bool(
        h3 and len(component) >= 4 and high_authority >= H4_AUTHORITY_MINIMUM
    )
    defining = _defining_evidence(component, chosen_streaks, h2=h2, h3=h3)
    normalized_phase = _normalized_phase_cluster(phase_offset, tolerance, period)
    regime_id = _stable_id(
        "authority-regime",
        AUTHORITY_SEMANTIC_VERSION,
        server_key,
        period,
        normalized_phase,
        defining,
    )
    candidate_id = _stable_id(
        "authority-candidate",
        AUTHORITY_SEMANTIC_VERSION,
        regime_id,
        quality_values,
        tuple(streak_bonus_values),
        round(aligned_normal, 6),
        tuple(sorted(set(h2_harmonic + h3_harmonic))),
        h1,
        h2,
        h3,
        h4,
    )
    reasons = {
        "bounded_high_authority_formula_v1",
        "normal_reinforcement_remaining_uncertainty_only",
        "phase_authority_separate",
        "maximal_streak_bonus_only",
    }
    if h1:
        reasons.add("h1_gate_passed")
    if h2:
        reasons.add("h2_gate_passed")
    if h3:
        reasons.add("h3_gate_passed")
    if h4:
        reasons.add("h4_gate_passed")
    reasons.update(h2_harmonic)
    reasons.update(h3_harmonic)
    if any(
        abs(quality_map[item.relationship_id] - item.high_quality_diagnostic)
        <= 0.000001
        for item in component
    ):
        reasons.add("relationship_quality_recomputed")
    return (
        AuthorityCandidate(
            schema_version=AUTHORITY_SCHEMA_VERSION,
            semantic_version=AUTHORITY_SEMANTIC_VERSION,
            candidate_id=candidate_id,
            regime_id=regime_id,
            server_key=server_key,
            candidate_period_seconds=period,
            phase_offset=round(phase_offset, 6),
            phase_spread=round(phase_spread, 6),
            phase_tolerance=round(tolerance, 6),
            high_relationship_ids=tuple(
                item.relationship_id for item in component
            ),
            maximal_streak_ids=tuple(item.streak_id for item in chosen_streaks),
            relationship_quality_values=quality_values,
            base_authority=round(base, 6),
            streak_bonus=round(streak_bonus, 6),
            streak_bonus_values=tuple(streak_bonus_values),
            high_authority=round(high_authority, 6),
            phase_quality=round(phase_quality, 6),
            high_phase_authority=round(phase_authority, 6),
            aligned_normal_confidence=round(aligned_normal, 6),
            combined_confidence=round(combined, 6),
            h1_gate=h1,
            h2_gate=h2,
            h3_gate=h3,
            h4_gate=h4,
            harmonic_blockers=tuple(sorted(set(h2_harmonic + h3_harmonic))),
            defining_evidence_ids=defining,
            first_evidence_at=min(item.left_at for item in component),
            last_evidence_at=max(item.right_at for item in component),
            reason_codes=tuple(sorted(reasons)),
        ),
        normal,
    )


def _normal_only_candidate(
    *,
    server_key: str,
    normal_candidate: CandidateScore,
    relationships: tuple[IntervalRelationship, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
) -> tuple[AuthorityCandidate, NormalCandidateEvidence]:
    period = normal_candidate.period_seconds
    phase = float(normal_candidate.phase_offset or 0.0) % period
    tolerance = candidate_phase_tolerance(period)
    normal = _normal_candidate_evidence(
        period=period,
        phase_offset=phase,
        tolerance=tolerance,
        normal_candidate=normal_candidate,
        relationships=relationships,
        windows=windows,
        events=events,
    )
    defining = normal.aligned_relationship_ids or (
        f"normal-candidate-{period}-{round(phase, 6)}",
    )
    regime_id = _stable_id(
        "authority-normal-regime",
        AUTHORITY_SEMANTIC_VERSION,
        server_key,
        period,
        _normalized_phase_cluster(phase, tolerance, period),
        defining,
    )
    candidate_id = _stable_id(
        "authority-normal-candidate",
        AUTHORITY_SEMANTIC_VERSION,
        regime_id,
        normal.aligned_normal_confidence,
        normal.aligned_relationship_ids,
    )
    times = [
        value
        for item in relationships
        if item.relationship_id in normal.aligned_relationship_ids
        for value in (item.left_at, item.right_at)
    ]
    return (
        AuthorityCandidate(
            schema_version=AUTHORITY_SCHEMA_VERSION,
            semantic_version=AUTHORITY_SEMANTIC_VERSION,
            candidate_id=candidate_id,
            regime_id=regime_id,
            server_key=server_key,
            candidate_period_seconds=period,
            phase_offset=round(phase, 6),
            phase_spread=round(normal_candidate.phase_residual_mad or tolerance, 6),
            phase_tolerance=round(tolerance, 6),
            high_relationship_ids=(),
            maximal_streak_ids=(),
            relationship_quality_values=(),
            base_authority=0.0,
            streak_bonus=0.0,
            streak_bonus_values=(),
            high_authority=0.0,
            phase_quality=0.0,
            high_phase_authority=0.0,
            aligned_normal_confidence=round(normal.aligned_normal_confidence, 6),
            combined_confidence=round(
                min(0.10, 0.10 * normal.aligned_normal_confidence), 6
            ),
            h1_gate=False,
            h2_gate=False,
            h3_gate=False,
            h4_gate=False,
            harmonic_blockers=(),
            defining_evidence_ids=tuple(defining),
            first_evidence_at=min(times, default=0.0),
            last_evidence_at=max(times, default=0.0),
            reason_codes=(
                "normal_only_candidate",
                "normal_evidence_cannot_manufacture_high_gate",
            ),
        ),
        normal,
    )


def _normal_candidate_evidence(
    *,
    period: int,
    phase_offset: float,
    tolerance: float,
    normal_candidate: CandidateScore,
    relationships: tuple[IntervalRelationship, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
) -> NormalCandidateEvidence:
    aligned_relationships = tuple(
        item
        for item in relationships
        if item.candidate_period_seconds == period
        and _circular_distance(
            _relationship_phase_value(item, period), phase_offset, period
        )
        <= tolerance
    )
    normal_phase_compatible = bool(
        normal_candidate.phase_offset is not None
        and _circular_distance(
            normal_candidate.phase_offset, phase_offset, period
        )
        <= tolerance
    )
    aligned_confidence = (
        min(
            normal_candidate.fundamental_period_confidence,
            normal_candidate.phase_confidence,
        )
        if normal_phase_compatible
        else 0.0
    )
    misses = tuple(
        item.result_id
        for item in windows
        if item.candidate_period_seconds == period
        and item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and _circular_distance(
            item.expected_phase_offset, phase_offset, period
        )
        <= tolerance
    )
    hits = tuple(
        item.result_id
        for item in windows
        if item.candidate_period_seconds == period
        and item.outcome is ExpectedWindowOutcome.HIT
        and _circular_distance(
            item.expected_phase_offset, phase_offset, period
        )
        <= tolerance
    )
    neutral = tuple(
        item.result_id
        for item in windows
        if item.candidate_period_seconds == period
        and item.outcome
        in {
            ExpectedWindowOutcome.AMBIGUOUS,
            ExpectedWindowOutcome.UNKNOWN,
            ExpectedWindowOutcome.RETRACTED_MISS,
        }
    )
    off_phase = tuple(
        item.event_id
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _circular_distance(_event_at(item), phase_offset, period) > tolerance
    )
    return NormalCandidateEvidence(
        candidate_period_seconds=period,
        phase_offset=round(phase_offset, 6),
        fundamental_confidence=normal_candidate.fundamental_period_confidence,
        phase_confidence=normal_candidate.phase_confidence,
        aligned_normal_confidence=round(aligned_confidence, 6),
        aligned_relationship_ids=tuple(
            sorted(item.relationship_id for item in aligned_relationships)
        ),
        compatible_multiple_relationship_ids=tuple(
            sorted(
                item.relationship_id
                for item in aligned_relationships
                if item.relationship_kind
                in {RelationshipKind.COMPATIBLE_MULTIPLE, RelationshipKind.SKIP_OVER}
            )
        ),
        off_phase_event_ids=tuple(sorted(off_phase)),
        active_genuine_miss_ids=tuple(sorted(misses)),
        compatible_hit_ids=tuple(sorted(hits)),
        neutral_window_ids=tuple(sorted(neutral)),
        reason_codes=(
            "normal_phase_and_period_compatibility_required",
            "normal_reinforcement_bounded",
            "off_phase_spacing_not_incumbent_reinforcement",
        ),
    )


def _transition_decision(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    candidates: tuple[AuthorityCandidate, ...],
    normal_ledger: NormalEvidenceLedger,
    high_ledger: HighEvidenceLedger,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    prior: AuthorityDecision | None,
) -> AuthorityDecision:
    ranked = tuple(sorted(candidates, key=_candidate_rank, reverse=True))
    high_h3 = tuple(item for item in ranked if item.h3_gate)
    high_h2 = tuple(item for item in ranked if item.h2_gate)
    previous_incumbent = prior.incumbent_shadow_regime if prior is not None else None

    incumbent_candidate = None
    incumbent = previous_incumbent
    if previous_incumbent is not None:
        incumbent_candidate = _matching_candidate(previous_incumbent, candidates)
        if incumbent_candidate is not None:
            incumbent = _regime_from_candidate(
                incumbent_candidate,
                status=RegimeRecordStatus.ESTABLISHED,
                existing=previous_incumbent,
            )

    if incumbent is None and high_h3:
        selected_candidate = high_h3[0]
        incumbent = _regime_from_candidate(
            selected_candidate, status=RegimeRecordStatus.ESTABLISHED
        )
        return _decision(
            server_key=server_key,
            state=RegimeState.ESTABLISHED,
            selected=incumbent,
            incumbent=incumbent,
            regimes=(incumbent,),
            candidates=candidates,
            contexts=(),
            strongest=None,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=True,
            prediction_usable=True,
            countdown_safe=True,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=selected_candidate,
            reasons=("unknown_server_established_by_h3",),
        )

    if incumbent is None and high_h2:
        selected_candidate = high_h2[0]
        selected = _regime_from_candidate(
            selected_candidate, status=RegimeRecordStatus.LIKELY
        )
        return _decision(
            server_key=server_key,
            state=RegimeState.LIKELY,
            selected=selected,
            incumbent=selected,
            regimes=(selected,),
            candidates=candidates,
            contexts=(),
            strongest=None,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=True,
            prediction_usable=False,
            countdown_safe=False,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=selected_candidate,
            reasons=("unknown_server_likely_by_h2",),
        )

    if incumbent is None and normal_score.selected_period_seconds is not None:
        selected_candidate = _normal_selected_candidate(normal_score, ranked)
        if selected_candidate is not None:
            status = (
                RegimeRecordStatus.ESTABLISHED
                if normal_score.prediction_usable
                else RegimeRecordStatus.LIKELY
            )
            selected = _regime_from_candidate(
                selected_candidate,
                status=status,
                origin=AuthorityOrigin.NORMAL,
            )
            state = (
                RegimeState.ESTABLISHED
                if status is RegimeRecordStatus.ESTABLISHED
                else RegimeState.LIKELY
            )
            return _decision(
                server_key=server_key,
                state=state,
                selected=selected,
                incumbent=selected,
                regimes=(selected,),
                candidates=candidates,
                contexts=(),
                strongest=None,
                normal_ledger=normal_ledger,
                high_ledger=high_ledger,
                cycle_visible=True,
                prediction_usable=normal_score.prediction_usable,
                countdown_safe=normal_score.prediction_usable,
                suspicion_level=0,
                suspicion_ids=(),
                selected_candidate=selected_candidate,
                reasons=("normal_scorer_remains_available_for_unknown_server",),
            )

    if incumbent is None:
        state = RegimeState.NORMAL_LEARNING if candidates else RegimeState.UNKNOWN
        reasons = (
            "evidence_retained_without_structural_selection"
            if candidates
            else "no_candidate_evidence"
        )
        return _decision(
            server_key=server_key,
            state=state,
            selected=None,
            incumbent=None,
            regimes=(),
            candidates=candidates,
            contexts=(),
            strongest=None,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=False,
            prediction_usable=False,
            countdown_safe=False,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=ranked[0] if ranked else None,
            reasons=(reasons,),
        )

    last_aligned_at = _last_aligned_evidence_at(incumbent, events, windows)
    active_misses = tuple(
        item.result_id
        for item in windows
        if item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and item.candidate_period_seconds == incumbent.candidate_period_seconds
        and _circular_distance(
            item.expected_phase_offset,
            incumbent.phase_offset,
            incumbent.candidate_period_seconds,
        )
        <= incumbent.phase_tolerance
        and item.expected_at > last_aligned_at
    )
    active_off_phase = tuple(
        item.event_id
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _event_at(item) > last_aligned_at
        and _circular_distance(
            _event_at(item),
            incumbent.phase_offset,
            incumbent.candidate_period_seconds,
        )
        > incumbent.phase_tolerance
    )
    contexts = tuple(
        _challenger_context(
            incumbent=incumbent,
            candidate=item,
            normal=_normal_for_candidate(normal_ledger, item),
            active_miss_ids=active_misses,
            events=events,
        )
        for item in ranked
        if _candidate_conflicts_regime(item, incumbent)
    )
    active_contexts = tuple(
        item
        for item in contexts
        if (
            (
                item.high_confirmation_present
                and item.last_evidence_at > (incumbent.established_at or 0.0)
            )
            or item.last_evidence_at > last_aligned_at
        )
    )
    strongest = max(active_contexts or contexts, key=_context_rank, default=None)
    authorized_h3 = max(
        (
            item
            for item in active_contexts
            if item.authorization_state is ChallengerAuthorization.ESTABLISHED
        ),
        key=_context_rank,
        default=None,
    )
    authorized_h2 = max(
        (
            item
            for item in active_contexts
            if item.authorization_state is ChallengerAuthorization.AUTHORIZED
        ),
        key=_context_rank,
        default=None,
    )

    if authorized_h3 is not None:
        challenger_candidate = next(
            item for item in candidates if item.regime_id == authorized_h3.proposed_regime_id
        )
        new_regime = _regime_from_candidate(
            challenger_candidate, status=RegimeRecordStatus.ESTABLISHED
        )
        old_regime = replace(
            incumbent,
            status=RegimeRecordStatus.SUPERSEDED,
            superseded_at=challenger_candidate.last_evidence_at,
            superseded_by_id=new_regime.regime_id,
            reason_codes=tuple(
                sorted({*incumbent.reason_codes, "superseded_by_symmetric_h3"})
            ),
        )
        updated_contexts = tuple(
            replace(
                item,
                regime_change_status=(
                    ChallengerStatus.ESTABLISHED
                    if item.context_id == authorized_h3.context_id
                    else item.regime_change_status
                ),
            )
            for item in contexts
        )
        return _decision(
            server_key=server_key,
            state=RegimeState.NEW_REGIME_ESTABLISHED,
            selected=new_regime,
            incumbent=new_regime,
            regimes=_merge_regimes(prior, old_regime, new_regime),
            candidates=candidates,
            contexts=updated_contexts,
            strongest=authorized_h3,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=True,
            prediction_usable=True,
            countdown_safe=True,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=challenger_candidate,
            reasons=(
                "newer_conflicting_h3_supersedes_incumbent",
                "symmetric_high_gate_transition",
            ),
        )

    if authorized_h2 is not None:
        challenger_candidate = next(
            item for item in candidates if item.regime_id == authorized_h2.proposed_regime_id
        )
        provisional = _regime_from_candidate(
            challenger_candidate, status=RegimeRecordStatus.PROVISIONAL
        )
        previous_state = prior.state if prior is not None else None
        state = (
            RegimeState.NEW_REGIME_PROVISIONAL
            if previous_state is RegimeState.TRANSITION_CONFIRMED
            else RegimeState.TRANSITION_CONFIRMED
        )
        context_status = (
            ChallengerStatus.PROVISIONAL
            if state is RegimeState.NEW_REGIME_PROVISIONAL
            else ChallengerStatus.TRANSITION_CONFIRMED
        )
        updated_contexts = tuple(
            replace(
                item,
                regime_change_status=(
                    context_status
                    if item.context_id == authorized_h2.context_id
                    else item.regime_change_status
                ),
            )
            for item in contexts
        )
        return _decision(
            server_key=server_key,
            state=state,
            selected=provisional,
            incumbent=incumbent,
            regimes=_merge_regimes(prior, incumbent, provisional),
            candidates=candidates,
            contexts=updated_contexts,
            strongest=authorized_h2,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=True,
            prediction_usable=False,
            countdown_safe=False,
            suspicion_level=2,
            suspicion_ids=tuple(
                sorted(
                    {
                        *authorized_h2.high_relationship_ids,
                        *active_misses,
                        *active_off_phase,
                    }
                )
            ),
            selected_candidate=challenger_candidate,
            reasons=(
                "h2_challenger_authorizes_provisional_transition",
                "old_incumbent_retained_for_rollback",
                "shadow_countdown_unsafe_during_transition",
            ),
        )

    active_high_relationships = max(
        (len(item.high_relationship_ids) for item in active_contexts), default=0
    )
    coherent_strong_contradictions = active_high_relationships >= 2
    has_normal_context = any(
        item.normal_context_strength > 0
        or bool(
            set(item.normal_relationship_ids).difference(
                item.high_relationship_ids
            )
        )
        for item in active_contexts
    )
    suspicion_ids = tuple(
        sorted(
            {
                *active_misses,
                *active_off_phase,
                *(
                    strongest.high_relationship_ids
                    if strongest is not None
                    and strongest.last_evidence_at > last_aligned_at
                    else ()
                ),
            }
        )
    )
    if coherent_strong_contradictions:
        state = RegimeState.CHALLENGER_ACCUMULATING
        suspicion_level = 2
        prediction_usable = False
        countdown_safe = False
        reasons = (
            "two_coherent_strong_contradictions_without_h2",
            "incumbent_retained",
            "shadow_countdown_unsafe",
        )
    elif has_normal_context:
        state = RegimeState.CHALLENGER_ACCUMULATING
        suspicion_level = 1
        prediction_usable = True
        countdown_safe = True
        reasons = (
            "latent_normal_challenger_context",
            "normal_context_cannot_switch_h3_incumbent",
        )
    elif active_misses or active_off_phase or active_high_relationships == 1:
        state = RegimeState.CHANGE_SUSPECTED
        suspicion_level = 1
        prediction_usable = True
        countdown_safe = True
        reasons = (
            "bounded_level_one_suspicion",
            "incumbent_high_authority_unchanged",
            "one_contradiction_does_not_hide_prediction",
        )
    else:
        state = (
            RegimeState.ESTABLISHED
            if prior is None
            or prior.state
            not in {
                RegimeState.NEW_REGIME_ESTABLISHED,
                RegimeState.NEW_REGIME_PROVISIONAL,
                RegimeState.TRANSITION_CONFIRMED,
            }
            else RegimeState.ESTABLISHED
        )
        suspicion_level = 0
        prediction_usable = True
        countdown_safe = True
        reasons = (
            "established_incumbent_stable",
            "aligned_evidence_clears_active_level_one_suspicion",
        )
    return _decision(
        server_key=server_key,
        state=state,
        selected=incumbent,
        incumbent=incumbent,
        regimes=_merge_regimes(prior, incumbent),
        candidates=candidates,
        contexts=contexts,
        strongest=strongest,
        normal_ledger=normal_ledger,
        high_ledger=high_ledger,
        cycle_visible=True,
        prediction_usable=prediction_usable,
        countdown_safe=countdown_safe,
        suspicion_level=suspicion_level,
        suspicion_ids=suspicion_ids,
        selected_candidate=incumbent_candidate,
        reasons=reasons,
    )


def _decision(
    *,
    server_key: str,
    state: RegimeState,
    selected: RegimeRecord | None,
    incumbent: RegimeRecord | None,
    regimes: tuple[RegimeRecord, ...],
    candidates: tuple[AuthorityCandidate, ...],
    contexts: tuple[ChallengerContext, ...],
    strongest: ChallengerContext | None,
    normal_ledger: NormalEvidenceLedger,
    high_ledger: HighEvidenceLedger,
    cycle_visible: bool,
    prediction_usable: bool,
    countdown_safe: bool,
    suspicion_level: int,
    suspicion_ids: tuple[str, ...],
    selected_candidate: AuthorityCandidate | None,
    reasons: tuple[str, ...],
) -> AuthorityDecision:
    normal_confidence = (
        selected_candidate.aligned_normal_confidence
        if selected_candidate is not None
        else 0.0
    )
    high_confidence = (
        selected_candidate.high_authority if selected_candidate is not None else 0.0
    )
    combined_confidence = (
        selected_candidate.combined_confidence
        if selected_candidate is not None
        else (selected.combined_confidence if selected is not None else 0.0)
    )
    reason_values = tuple(sorted(set(reasons)))
    decision_id = _stable_id(
        "authority-decision",
        AUTHORITY_SEMANTIC_VERSION,
        server_key,
        state.value,
        selected.regime_id if selected else None,
        incumbent.regime_id if incumbent else None,
        tuple(item.regime_id for item in regimes),
        tuple(item.candidate_id for item in candidates),
        tuple(item.context_id for item in contexts),
        normal_ledger.active_genuine_miss_ids,
        normal_ledger.neutral_window_ids,
        tuple(
            (
                item.candidate_period_seconds,
                item.phase_offset,
                item.aligned_normal_confidence,
                item.aligned_relationship_ids,
                item.compatible_hit_ids,
            )
            for item in normal_ledger.candidates
        ),
        high_ledger.high_relationship_ids,
        high_ledger.maximal_streak_ids,
        suspicion_level,
        tuple(sorted(suspicion_ids)),
        reason_values,
    )
    return AuthorityDecision(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        decision_id=decision_id,
        server_key=server_key,
        selected_shadow_regime=selected,
        incumbent_shadow_regime=incumbent,
        strongest_challenger=strongest,
        regimes=tuple(sorted(regimes, key=lambda item: item.regime_id)),
        candidates=candidates,
        challenger_contexts=tuple(sorted(contexts, key=lambda item: item.context_id)),
        normal_ledger=normal_ledger,
        high_ledger=high_ledger,
        state=state,
        cycle_visible=cycle_visible,
        prediction_usable=prediction_usable,
        countdown_safe=countdown_safe,
        suspicion_level=suspicion_level,
        active_suspicion_evidence_ids=tuple(sorted(set(suspicion_ids))),
        normal_confidence=round(normal_confidence, 6),
        high_confidence=round(high_confidence, 6),
        combined_confidence=round(combined_confidence, 6),
        reason_codes=reason_values,
    )


def _challenger_context(
    *,
    incumbent: RegimeRecord,
    candidate: AuthorityCandidate,
    normal: NormalCandidateEvidence,
    active_miss_ids: tuple[str, ...],
    events: tuple[PhysicalRestartEvent, ...],
) -> ChallengerContext:
    if candidate.h3_gate:
        authorization = ChallengerAuthorization.ESTABLISHED
        status = ChallengerStatus.ESTABLISHED
    elif candidate.h2_gate:
        authorization = ChallengerAuthorization.AUTHORIZED
        status = ChallengerStatus.ACCUMULATING
    else:
        authorization = ChallengerAuthorization.LATENT
        status = ChallengerStatus.ACCUMULATING
    off_phase_event_ids = tuple(
        sorted(
            item.event_id
            for item in events
            if _authentic_event(item)
            and _event_at(item) is not None
            and candidate.first_evidence_at <= _event_at(item) <= candidate.last_evidence_at
            and _circular_distance(
                _event_at(item),
                candidate.phase_offset,
                candidate.candidate_period_seconds,
            )
            <= candidate.phase_tolerance
            and _circular_distance(
                _event_at(item),
                incumbent.phase_offset,
                incumbent.candidate_period_seconds,
            )
            > incumbent.phase_tolerance
        )
    )
    context_id = _stable_id(
        "challenger-context",
        AUTHORITY_SEMANTIC_VERSION,
        incumbent.regime_id,
        candidate.regime_id,
        normal.aligned_relationship_ids,
        off_phase_event_ids,
        active_miss_ids,
        candidate.high_relationship_ids,
        candidate.maximal_streak_ids,
    )
    reasons = {
        "period_and_phase_keyed_challenger",
        "normal_context_latent_until_high_confirmation",
    }
    if candidate.h2_gate:
        reasons.add("h2_high_confirmation_present")
    if candidate.h3_gate:
        reasons.add("h3_high_confirmation_present")
    if candidate.candidate_period_seconds == incumbent.candidate_period_seconds:
        reasons.add("same_period_distinct_phase_regime")
    return ChallengerContext(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        context_id=context_id,
        server_key=incumbent.server_key,
        incumbent_regime_id=incumbent.regime_id,
        proposed_regime_id=candidate.regime_id,
        candidate_period_seconds=candidate.candidate_period_seconds,
        phase_offset=candidate.phase_offset,
        phase_tolerance=candidate.phase_tolerance,
        normal_relationship_ids=normal.aligned_relationship_ids,
        off_phase_event_ids=off_phase_event_ids,
        active_genuine_miss_ids=tuple(sorted(active_miss_ids)),
        high_relationship_ids=candidate.high_relationship_ids,
        streak_ids=candidate.maximal_streak_ids,
        normal_context_strength=normal.aligned_normal_confidence,
        high_authority=candidate.high_authority,
        high_confirmation_present=candidate.h2_gate or candidate.h3_gate,
        authorization_state=authorization,
        regime_change_status=status,
        first_evidence_at=candidate.first_evidence_at,
        last_evidence_at=candidate.last_evidence_at,
        reason_codes=tuple(sorted(reasons)),
    )


def _regime_from_candidate(
    candidate: AuthorityCandidate,
    *,
    status: RegimeRecordStatus,
    origin: AuthorityOrigin = AuthorityOrigin.CONTINUITY,
    existing: RegimeRecord | None = None,
) -> RegimeRecord:
    established = candidate.last_evidence_at if candidate.h3_gate else None
    regime_id = candidate.regime_id
    defining = candidate.defining_evidence_ids
    if existing is not None:
        regime_id = existing.regime_id
        established = existing.established_at
        defining = existing.defining_evidence_ids
        origin = existing.origin
    gate = candidate.highest_gate if origin is AuthorityOrigin.CONTINUITY else AuthorityGate.NORMAL
    return RegimeRecord(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        regime_id=regime_id,
        server_key=candidate.server_key,
        candidate_period_seconds=candidate.candidate_period_seconds,
        phase_offset=candidate.phase_offset,
        phase_tolerance=candidate.phase_tolerance,
        origin=origin,
        authority_gate=gate,
        high_authority=candidate.high_authority,
        phase_authority=candidate.high_phase_authority,
        combined_confidence=candidate.combined_confidence,
        status=status,
        defining_evidence_ids=defining,
        established_at=established,
        superseded_at=None,
        superseded_by_id=None,
        reason_codes=(
            "period_and_phase_regime_identity",
            "high_authority_does_not_decay_with_inactivity",
            "regime_remains_reversible",
        ),
    )


def _harmonic_blockers(
    period: int,
    normal_candidate: CandidateScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if period not in {6 * 3600, 8 * 3600, 12 * 3600}:
        return (), ()
    resolutions = {
        item.divisor_seconds: item for item in normal_candidate.divisor_resolution
    }
    required = tuple(
        value for value in CANDIDATE_PERIODS if value < period and period % value == 0
    )
    h2 = []
    h3 = []
    for divisor in required:
        result = resolutions.get(divisor)
        healthy_midpoints = sum(
            item.candidate_period_seconds == divisor
            and item.outcome is ExpectedWindowOutcome.GENUINE_MISS
            and item.negative_penalty_active
            for item in windows
        )
        established = bool(
            (result is not None and result.established_resolved)
            or healthy_midpoints >= 1
        )
        high = bool(
            (result is not None and result.high_resolved)
            or healthy_midpoints >= 2
        )
        if not established:
            h2.append(f"unresolved_harmonic_divisor_{divisor}")
        if not high:
            h3.append(f"high_harmonic_divisor_unresolved_{divisor}")
    return tuple(h2), tuple(h3)


def _phase_components(
    relationships: tuple[IntervalRelationship, ...], period: int
) -> tuple[tuple[IntervalRelationship, ...], ...]:
    if not relationships:
        return ()
    tolerance = candidate_phase_tolerance(period)
    remaining = set(range(len(relationships)))
    components = []
    while remaining:
        seed = min(remaining)
        pending = [seed]
        component = set()
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            phase = _relationship_phase_value(relationships[index], period)
            for other in tuple(remaining):
                if other in component:
                    continue
                other_phase = _relationship_phase_value(relationships[other], period)
                if _circular_distance(phase, other_phase, period) <= tolerance:
                    pending.append(other)
        remaining.difference_update(component)
        values = tuple(
            sorted(
                (relationships[index] for index in component),
                key=lambda item: (item.left_at, item.right_at, item.relationship_id),
            )
        )
        components.append(values)
    return tuple(
        sorted(components, key=lambda values: (values[0].left_at, values[0].relationship_id))
    )


def _relationship_phase(
    relationships: tuple[IntervalRelationship, ...], period: int
) -> tuple[float, float]:
    values = [relationships[0].left_at]
    values.extend(item.right_at for item in relationships)
    return _phase_estimate(values, period)


def _phase_estimate(values: Iterable[float], period: int) -> tuple[float, float]:
    phases = [float(value) % period for value in values]
    anchor = phases[0]
    residuals = [
        ((value - anchor + period / 2) % period) - period / 2
        for value in phases
    ]
    center = statistics.median(residuals)
    estimate = (anchor + center) % period
    spread = max(
        abs(((value - estimate + period / 2) % period) - period / 2)
        for value in phases
    )
    return estimate, spread


def _relationship_phase_value(
    relationship: IntervalRelationship, period: int
) -> float:
    left = relationship.left_at % period
    residual = (
        (relationship.right_at - relationship.left_at)
        - relationship.multiplier * period
    )
    return (left + residual / 2.0) % period


def _non_overlapping_streaks(
    streaks: Iterable[CadenceStreak],
) -> tuple[CadenceStreak, ...]:
    selected = []
    used = set()
    for streak in sorted(
        {item.streak_id: item for item in streaks}.values(),
        key=lambda item: (-item.interval_count, item.end_at, item.streak_id),
    ):
        if used.intersection(streak.relationship_ids):
            continue
        selected.append(streak)
        used.update(streak.relationship_ids)
    return tuple(sorted(selected, key=lambda item: (item.start_at, item.streak_id)))


def _phase_defining_streak(
    streaks: tuple[CadenceStreak, ...]
) -> CadenceStreak | None:
    return max(
        streaks,
        key=lambda item: (item.interval_count, item.end_at, item.streak_id),
        default=None,
    )


def _defining_evidence(
    relationships: tuple[IntervalRelationship, ...],
    streaks: tuple[CadenceStreak, ...],
    *,
    h2: bool,
    h3: bool,
) -> tuple[str, ...]:
    ordered_relationships = tuple(item.relationship_id for item in relationships)
    if h3:
        three = min(
            (item for item in streaks if item.interval_count >= 3),
            key=lambda item: (item.end_at, item.streak_id),
            default=None,
        )
        if three is not None:
            return three.relationship_ids[:3]
        h2_streak = min(
            (item for item in streaks if item.interval_count >= 2),
            key=lambda item: (item.end_at, item.streak_id),
        )
        values = list(h2_streak.relationship_ids)
        for identity in ordered_relationships:
            if identity not in values:
                values.append(identity)
            if len(values) >= 3:
                break
        return tuple(values)
    if h2:
        h2_streak = min(
            (item for item in streaks if item.interval_count >= 2),
            key=lambda item: (item.end_at, item.streak_id),
        )
        return h2_streak.relationship_ids
    return ordered_relationships[:1]


def _latest_window_records(
    records: Iterable[ExpectedWindowLedgerRecord], server_key: str
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values = {
        item.result_id: item
        for item in records
        if item.server_key == server_key
    }
    superseded = {
        item.supersedes_result_id
        for item in values.values()
        if item.supersedes_result_id is not None
    }
    return tuple(
        sorted(
            (item for item in values.values() if item.result_id not in superseded),
            key=lambda item: (
                item.expected_at,
                item.candidate_period_seconds,
                item.result_id,
            ),
        )
    )


def _last_aligned_evidence_at(
    incumbent: RegimeRecord,
    events: tuple[PhysicalRestartEvent, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
) -> float:
    values = [incumbent.established_at or 0.0]
    values.extend(
        _event_at(item)
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _circular_distance(
            _event_at(item),
            incumbent.phase_offset,
            incumbent.candidate_period_seconds,
        )
        <= incumbent.phase_tolerance
    )
    values.extend(
        item.expected_at
        for item in windows
        if item.outcome is ExpectedWindowOutcome.HIT
        and item.candidate_period_seconds == incumbent.candidate_period_seconds
        and _circular_distance(
            item.expected_phase_offset,
            incumbent.phase_offset,
            incumbent.candidate_period_seconds,
        )
        <= incumbent.phase_tolerance
    )
    return max(values)


def _matching_candidate(
    regime: RegimeRecord, candidates: tuple[AuthorityCandidate, ...]
) -> AuthorityCandidate | None:
    return max(
        (
            item
            for item in candidates
            if item.candidate_period_seconds == regime.candidate_period_seconds
            and _circular_distance(
                item.phase_offset,
                regime.phase_offset,
                regime.candidate_period_seconds,
            )
            <= regime.phase_tolerance
        ),
        key=_candidate_rank,
        default=None,
    )


def _normal_selected_candidate(
    score: ScheduleScore, candidates: tuple[AuthorityCandidate, ...]
) -> AuthorityCandidate | None:
    period = score.selected_period_seconds
    if period is None:
        return None
    normal = score.candidate(period)
    return min(
        (item for item in candidates if item.candidate_period_seconds == period),
        key=lambda item: _circular_distance(
            item.phase_offset, normal.phase_offset or 0.0, period
        ),
        default=None,
    )


def _normal_for_candidate(
    ledger: NormalEvidenceLedger, candidate: AuthorityCandidate
) -> NormalCandidateEvidence:
    return min(
        (
            item
            for item in ledger.candidates
            if item.candidate_period_seconds == candidate.candidate_period_seconds
        ),
        key=lambda item: _circular_distance(
            item.phase_offset,
            candidate.phase_offset,
            candidate.candidate_period_seconds,
        ),
    )


def _candidate_conflicts_regime(
    candidate: AuthorityCandidate, regime: RegimeRecord
) -> bool:
    return bool(
        candidate.candidate_period_seconds != regime.candidate_period_seconds
        or _circular_distance(
            candidate.phase_offset,
            regime.phase_offset,
            regime.candidate_period_seconds,
        )
        > regime.phase_tolerance
    )


def _merge_regimes(
    prior: AuthorityDecision | None, *records: RegimeRecord
) -> tuple[RegimeRecord, ...]:
    values = {
        item.regime_id: item for item in (prior.regimes if prior is not None else ())
    }
    for item in records:
        values[item.regime_id] = item
    return tuple(sorted(values.values(), key=lambda item: item.regime_id))


def _candidate_rank(candidate: AuthorityCandidate) -> tuple[object, ...]:
    gate_rank = {
        AuthorityGate.NONE: 0,
        AuthorityGate.NORMAL: 0,
        AuthorityGate.H1: 1,
        AuthorityGate.H2: 2,
        AuthorityGate.H3: 3,
        AuthorityGate.H4: 4,
    }[candidate.highest_gate]
    return (
        gate_rank,
        candidate.high_authority,
        candidate.high_phase_authority,
        candidate.last_evidence_at,
        candidate.regime_id,
    )


def _candidate_sort_key(candidate: AuthorityCandidate) -> tuple[object, ...]:
    return (
        candidate.candidate_period_seconds,
        candidate.phase_offset,
        candidate.regime_id,
    )


def _context_rank(context: ChallengerContext) -> tuple[object, ...]:
    authorization = {
        ChallengerAuthorization.LATENT: 0,
        ChallengerAuthorization.REJECTED: 0,
        ChallengerAuthorization.AUTHORIZED: 1,
        ChallengerAuthorization.ESTABLISHED: 2,
    }[context.authorization_state]
    return (
        authorization,
        context.high_authority,
        context.normal_context_strength,
        context.last_evidence_at,
        context.context_id,
    )


def _unique_relationships(
    relationships: Iterable[IntervalRelationship], server_key: str
) -> tuple[IntervalRelationship, ...]:
    values = {
        item.relationship_id: item
        for item in relationships
        if item.server_key == server_key
    }
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (
                item.left_at,
                item.right_at,
                item.candidate_period_seconds,
                item.relationship_id,
            ),
        )
    )


def _unique_streaks(
    streaks: Iterable[CadenceStreak], server_key: str
) -> tuple[CadenceStreak, ...]:
    values = {
        item.streak_id: item for item in streaks if item.server_key == server_key
    }
    return tuple(sorted(values.values(), key=lambda item: (item.start_at, item.streak_id)))


def _unique_events(
    events: Iterable[PhysicalRestartEvent], server_key: str
) -> tuple[PhysicalRestartEvent, ...]:
    values = {item.event_id: item for item in events if item.server_key == server_key}
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (_event_at(item) or math.inf, item.sequence, item.event_id),
        )
    )


def _authentic_event(event: PhysicalRestartEvent) -> bool:
    return bool(
        event_learning_eligible(event)
        and event.authenticity >= 0.50
        and event.outcome
        not in {
            EventOutcome.AMBIGUOUS_DRAIN,
            EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
            EventOutcome.INCOMPLETE,
            EventOutcome.EXPIRED,
            EventOutcome.SERVICE_INTERRUPTION,
        }
    )


def _event_at(event: PhysicalRestartEvent) -> float | None:
    return event.canonical_phase_at


def _normalized_phase_cluster(phase: float, tolerance: float, period: int) -> int:
    bucket_count = max(1, int(round(period / tolerance)))
    return int(math.floor((phase % period + tolerance / 2.0) / tolerance)) % bucket_count


def _circular_distance(left: float, right: float, period: int) -> float:
    return abs(((float(left) - float(right) + period / 2) % period) - period / 2)


def _stable_id(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
