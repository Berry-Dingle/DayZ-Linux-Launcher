from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable

from .companion_restart_phase2_continuity import (
    CadenceStreak,
    IntervalRelationship,
    RelationshipKind,
    relationship_endpoint_outcome_eligible,
)
from .companion_restart_phase2_detection import EventOutcome, PhysicalRestartEvent
from .companion_restart_phase2_expected_windows import (
    ExpectedWindowLedgerRecord,
    ExpectedWindowOutcome,
)
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    GATES as SCHEDULE_GATES,
    PHASE_UNCERTAINTY_CAP,
    CandidateScore,
    ScheduleScore,
    _event_count_cap,
    _event_tolerance,
    _event_weight,
    _phase_cluster,
    _signed_phase_residual,
    candidate_phase_tolerance,
    event_learning_eligible,
    phase_quality_band,
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
# Stage 8.2 continuous confidence milestones.  The maximal-streak bonus is
# ``coefficient * (1 - 2^-(intervals - 1)) * minimum quality``.  At 0.10 a
# clean two-interval streak has authority 0.80 and a clean three-interval
# streak 0.95 (the user-facing milestones).
STREAK_BONUS_COEFFICIENT = 0.10
# The H1-H4 gates keep the calibration they were defined with (the v1 bonus
# coefficient 0.08).  Gates are products of authority and phase quality, so
# evaluating them on the raised magnitude would silently loosen their phase
# precision (an imprecise three-interval challenger would supersede an
# established incumbent instead of becoming provisional).
GATE_STREAK_BONUS_COEFFICIENT = 0.08
# Stage 6 cycle-change recovery.  A credible challenger (H2, or a level-2
# context that has suspended the countdown) is resolved in favour of the
# incumbent only by this many independent, complete, high-eligible incumbent
# intervals that start strictly after the challenger's latest high evidence.
ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS = 2
COUNTDOWN_SUSPENSION_LATCH_REASON = (
    "credible_challenger_suspension_requires_original_cycle_confirmations"
)
HISTORICAL_CHALLENGER_EVIDENCE_REASON = (
    "pre_establishment_high_evidence_excluded_from_challenger_authorization"
)
# Stage 8.1 two-interval establishment (initial acquisition only).  The phase
# precision is the H3-equivalent one: H3 phase authority 0.85 divided by the
# authority of a clean three-interval streak (0.935), so a two-interval
# establishment is never looser on phase than today's three-interval H3.
TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM = 0.909
TWO_INTERVAL_ESTABLISHMENT_REASON = "unknown_server_established_by_two_interval_streak"
# Persistent marker on a regime whose origin was upgraded NORMAL -> CONTINUITY.
ORIGIN_UPGRADE_REASON = "normal_origin_upgraded_to_continuity"
# Stage 8.3 long-horizon sparse evidence (the ``aligned_normal`` input of the
# unchanged combined formula).
LONG_HORIZON_SPARSE_REASON = "long_horizon_per_event_sparse_statistic_v1"
# Stage 8.5 sparse ceiling S in ``combined = 1 - (1 - high)(1 - S * a)``.  It is
# the smallest two-decimal value for which eight strong, consistent sparse
# restarts (a = event-count cap 0.95, saturated) reach the displayed 80 %
# milestone: 0.84 * 0.95 = 0.798.  Sparse evidence alone can never exceed
# 0.798, so it never reaches the 95 % (strong confirmation) milestone.
SPARSE_EVIDENCE_CEILING = 0.84
# Stage 8.5 sparse establishment (initial acquisition only, D8-7).
SPARSE_ESTABLISHMENT_MINIMUM_PERCENT = 80
SPARSE_PHASE_CONFIDENCE_MINIMUM = 0.85
SPARSE_PHASE_QUALITY_BAND_MINIMUM = 0.9
SPARSE_COMPETITOR_MARGIN = 0.05
SPARSE_STRUCTURE_REASON = "sparse_establishment_structure_satisfied"
SPARSE_PERIOD_AMBIGUOUS_REASON = "sparse_period_or_harmonic_ambiguity_unresolved"
# Persistent marker on a regime (and its acquisition decision) established
# from sparse evidence.
SPARSE_ESTABLISHMENT_REASON = "unknown_server_established_by_sparse_evidence"
# Stage 8.5 strong confirmation of a sparse-established regime.  A fully
# observed matching interval that was not establishment evidence carries the
# weight the continuous path gives the interval that extends an established
# two-interval streak to three: authority 0.80 -> 0.95 removes 75 % of the
# remaining uncertainty.
SPARSE_CONFIRMATION_INTERVAL_WEIGHT = 0.75
SPARSE_CONFIRMATION_REASON = "sparse_regime_confirmed_by_full_interval"
# Stage 8.7a proportional off-phase tolerance.  The existing SE3 phase gate
# (``event-count cap x MAD band x consistency >= 0.85``) already accepts a
# saturated, best-band candidate down to this consistency (0.85 / 0.95, about
# 10.5 % off-phase weight).  Within it, off-phase restarts are deducted from
# the aligned count as net evidence instead of scaling the magnitude, so the
# 80 % milestone (which is the saturation asymptote of the sparse slot) stays
# reachable and recovers as aligned restarts accumulate; below it, the
# magnitude falls in proportion, as before.
SPARSE_CONSISTENCY_TOLERANCE = SPARSE_PHASE_CONFIDENCE_MINIMUM / _event_count_cap(8)
SPARSE_PROPORTIONAL_ANOMALY_REASON = "sparse_off_phase_restarts_within_proportional_tolerance"
# Stage 8.7a: an established incumbent's only level-one contradiction is one
# authentic off-phase restart that is phase-coherent with no other off-phase
# restart.  It is learning evidence, not a challenger: safe H2/sparse
# countdowns are kept (authority already marks the state prediction-usable).
ISOLATED_OFF_PHASE_RESTART_REASON = "isolated_off_phase_restart_does_not_affect_prediction"
# Stage 8.7 two-interval establishment against substantial conflicting
# sparse evidence: the streak stays provisional (LIKELY) until one further
# independent genuine restart confirms its period and phase.
TWO_INTERVAL_SPARSE_CONFLICT_REASON = (
    "two_interval_establishment_awaits_confirmation_against_sparse_conflict"
)
# Time Shift Detection.  An established continuity regime whose restarts move
# to another phase of the *same* period (a DST change, or an administrator
# moving the restart times) keeps its period and earned authority.  The
# evidence is independent authentic restarts after the regime's latest
# aligned evidence that agree on one new phase within the regime tolerance:
# one is noticed, two withhold the prediction, three adopt the phase.  The
# state is derived from the retained events on every rebuild; only the
# adopted phase is persisted (on the regime).  Period changes stay with the
# challenger rules above.
TIME_SHIFT_SUSPECT_OBSERVATIONS = 2
TIME_SHIFT_CONFIRM_OBSERVATIONS = 3
TIME_SHIFT_NOTICED_REASON = "possible_time_shift_noticed"
TIME_SHIFT_SUSPECTED_REASON = "possible_time_shift_prediction_withheld"
# Persistent marker on a regime whose phase was adopted from a time shift.
TIME_SHIFT_ADOPTED_REASON = "time_shift_adopted_period_and_authority_retained"
# Persistent marker ``<prefix><phase>`` on a shifted regime (and on the
# candidate that carries its authority): the phase at which the regime's
# earned high evidence was observed.  It is dropped once the evidence at the
# regime's own phase is at least as strong.
TIME_SHIFT_AUTHORITY_PHASE_PREFIX = "time_shift_authority_phase:"
# Persistent marker ``<prefix><time>`` beside the authority phase on a shifted
# sparse-established regime (and its carrying candidate): the first shifted
# observation.  A sparse regime's confidence is a per-phase statistic over
# its retained restarts, so its earned value is that statistic at the
# authority phase over the restarts strictly before this time (the shifted
# observations are excluded).  Continuity regimes never carry it.
TIME_SHIFT_AUTHORITY_UNTIL_PREFIX = "time_shift_authority_until:"
# Historical acquisition recovery (acquisition only).  A never-established
# server whose retained restarts before a confirmed time shift would on their
# own have passed the unchanged sparse establishment gate is established as
# the live runtime would have established it then, and the later restarts are
# judged by the established-regime rules (Time Shift Detection included).
# Persistent provenance marker on the regime (and so on its decisions).
HISTORICAL_ACQUISITION_REASON = "established_from_retained_history_before_time_shift"
# A full interval that is not high-eligible (query-visible endpoints) may still
# confirm a sparse-established regime when its only high-ineligibility is the
# endpoint evidence class; continuity, coverage, query health, identity,
# residual and intervening-episode requirements all still apply.
_FULL_INTERVAL_PERMITTED_HIGH_INELIGIBILITY = frozenset(
    {
        "high_ineligible_endpoint_class",
        "high_ineligible_direct_outage_contract",
        "high_ineligible_authenticity",
    }
)


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
    # Retention R3: relationships folded into the compact learned state are
    # counted here (their raw IDs are no longer referenced).  Always zero on
    # the raw-history path.
    folded_high_relationship_count: int = 0
    folded_normal_relationship_count: int = 0


def context_high_relationship_count(context: "ChallengerContext") -> int:
    """High relationships behind a challenger context, folded ones included."""

    return len(context.high_relationship_ids) + context.folded_high_relationship_count


COMPACT_EVIDENCE_REASON = "compact_learned_state_evidence"


@dataclass
class _Compact:
    """Compact-authority evaluation context (Retention R3).

    ``state`` is the learned state (folded knowledge); the ring tuples are the
    bounded unfolded raw evidence.  ``candidates`` maps each compact high
    candidate's ID to its compact evaluation, so later stages can read folded
    evidence that has no raw IDs.
    """

    state: object
    ring_events: tuple
    ring_relationships: tuple
    ring_windows: tuple
    normal_score: object
    candidates: dict = field(default_factory=dict)
    challenger_view: tuple | None = None


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
    learned_state: object | None = None,
) -> AuthorityDecision:
    """Build one immutable shadow decision without mutating any input state.

    With ``learned_state`` (Retention R3) the evidence arguments are the
    bounded ring (unfolded events including the open-chain anchor, unfolded
    relationships and windows); folded evidence is read from the learned
    state through the compact evaluators.  The decision state machine is the
    same in both modes.
    """

    if not isinstance(server_key, str) or not server_key:
        raise ValueError("server_key must be a non-empty string")
    event_values = _unique_events(events, server_key)
    relationship_values = _endpoint_eligible_relationships(
        _unique_relationships(relationships, server_key), event_values
    )
    streak_values = _unique_streaks(streaks, server_key)
    window_values = _latest_window_records(expected_window_ledger, server_key)
    compact = None
    if learned_state is not None:
        compact = _Compact(
            state=learned_state,
            ring_events=event_values,
            ring_relationships=relationship_values,
            ring_windows=window_values,
            normal_score=normal_score,
        )
    sparse = _sparse_evidence(event_values, prior_decision, compact=compact)
    if compact is None:
        candidates, normal_ledger, high_ledger = _build_ledgers(
            server_key=server_key,
            normal_score=normal_score,
            relationships=relationship_values,
            streaks=streak_values,
            windows=window_values,
            events=event_values,
            sparse=sparse,
        )
    else:
        candidates, normal_ledger, high_ledger = _compact_ledgers(
            server_key=server_key,
            normal_score=normal_score,
            relationships=relationship_values,
            windows=window_values,
            events=event_values,
            sparse=sparse,
            compact=compact,
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
        relationships=relationship_values,
        streaks=streak_values,
        sparse=sparse,
        compact=compact,
    )


@dataclass(frozen=True)
class _SparseEvidence:
    """Persisted physical restarts usable as independent sparse evidence.

    ``events`` are the authentic, learning-eligible restarts with a positive
    schedule weight, each physical restart once (``_unique_events``), over the
    whole persisted horizon.  ``historical`` maps each superseded regime to
    the restarts it explained up to its supersession: they may not strengthen
    a conflicting regime.
    """

    events: tuple[PhysicalRestartEvent, ...]
    historical: tuple[tuple[RegimeRecord, frozenset[str]], ...]
    compact: "_Compact | None" = None


def _sparse_evidence(
    events: tuple[PhysicalRestartEvent, ...],
    prior: AuthorityDecision | None,
    compact: "_Compact | None" = None,
) -> _SparseEvidence:
    usable = tuple(
        item
        for item in events
        if _authentic_event(item)
        and _event_weight(item) > 0
        and _event_at(item) is not None
    )
    if compact is not None:
        # Superseded-regime separation lives in the learned state.
        return _SparseEvidence(events=usable, historical=(), compact=compact)
    historical = []
    for regime in prior.regimes if prior is not None else ():
        if (
            regime.status is not RegimeRecordStatus.SUPERSEDED
            or regime.superseded_at is None
        ):
            continue
        explained = frozenset(
            item.event_id
            for item in usable
            if _event_at(item) <= regime.superseded_at
            and _circular_distance(
                _event_at(item),
                regime.phase_offset,
                regime.candidate_period_seconds,
            )
            <= regime.phase_tolerance
        )
        historical.append((regime, explained))
    return _SparseEvidence(
        events=usable,
        historical=tuple(sorted(historical, key=lambda item: item[0].regime_id)),
    )


@dataclass(frozen=True)
class _SparseSupport:
    value: float
    aligned: tuple[PhysicalRestartEvent, ...]
    effective: float
    quality_band: float
    # The unchanged Stage 8.3 statistic (cap(effective) x band x consistency),
    # still used by the SE3 phase gate.
    phase_confidence: float = 0.0
    off_phase_weight: float = 0.0
    consistency: float = 0.0
    # Compact path: aligned-time bounds and the compact support itself.
    bounds: tuple[float, float] | None = None
    compact_support: object | None = None
    compact: "_Compact | None" = None


def _support_bounds(support: "_SparseSupport") -> tuple[float, float] | None:
    """(first, last) aligned restart time, or ``None`` when nothing aligns."""

    if support.compact_support is not None:
        return support.bounds
    if not support.aligned:
        return None
    times = [_event_at(item) for item in support.aligned]
    return min(times), max(times)


def _long_horizon_sparse_confidence(
    *,
    period: int,
    phase_offset: float,
    sparse: _SparseEvidence,
    excluded_event_ids: frozenset[str],
) -> float:
    return _sparse_support(
        period=period,
        phase_offset=phase_offset,
        sparse=sparse,
        excluded_event_ids=excluded_event_ids,
    ).value


def _sparse_support(
    *,
    period: int,
    phase_offset: float,
    sparse: _SparseEvidence,
    excluded_event_ids: frozenset[str],
    candidate: "AuthorityCandidate | None" = None,
    before_at: float | None = None,
) -> _SparseSupport:
    """Per-event sparse support for one candidate (period, phase).

    Reuses the schema-3 per-event phase statistic: event-count cap x MAD
    quality band x consistency, with effective counts weighted by the
    existing schedule weights.  Every physical restart counts at most once;
    the candidate's own high-relationship endpoints are excluded so no
    restart is counted in both the continuous and the sparse slot.  Gaps
    are never interpreted (no relationships, no inferred restarts), so
    observations days apart remain independent evidence.  Consistency is
    measured over the restarts from the first aligned one onwards.

    Stage 8.7a: the returned magnitude is
    ``cap(effective - off_phase) x band x min(1, consistency / tolerance)``.
    Off-phase restarts stay evidence: each cancels an aligned restart of equal
    weight (net evidence), and when they exceed the proportion the SE3 phase
    gate accepts (``SPARSE_CONSISTENCY_TOLERANCE``) the magnitude also falls
    proportionally.  With no off-phase restart this is exactly the Stage 8.3
    statistic, which is still returned as ``phase_confidence`` for SE3.

    ``before_at`` limits the evaluation to restarts strictly earlier than it
    (Time Shift Detection's pre-shift value); ``None`` uses every restart.
    """

    if sparse.compact is not None:
        return _compact_sparse_support(
            period=period,
            phase_offset=phase_offset,
            compact=sparse.compact,
            excluded_event_ids=excluded_event_ids,
            candidate=candidate,
            before_at=before_at,
        )
    historical: set[str] = set()
    for regime, explained in sparse.historical:
        if (
            period != regime.candidate_period_seconds
            or _circular_distance(phase_offset, regime.phase_offset, period)
            > regime.phase_tolerance
        ):
            historical.update(explained)
    pool = tuple(
        item
        for item in sparse.events
        if item.event_id not in excluded_event_ids
        and (before_at is None or _event_at(item) < before_at)
    )
    aligned = tuple(
        item
        for item in pool
        if item.event_id not in historical
        and abs(_signed_phase_residual(_event_at(item), phase_offset, period))
        <= _event_tolerance(period, item)
    )
    if not aligned:
        return _SparseSupport(0.0, (), 0.0, 0.0)
    first = min(_event_at(item) for item in aligned)
    effective = sum(_event_weight(item) for item in aligned)
    domain = tuple(item for item in pool if _event_at(item) >= first)
    total = sum(_event_weight(item) for item in domain)
    off_phase_weight = sum(
        _event_weight(item)
        for item in domain
        if _sparse_off_phase(item, period=period, phase_offset=phase_offset)
    )
    mad = statistics.median(
        max(
            abs(_signed_phase_residual(_event_at(item), phase_offset, period)),
            min(PHASE_UNCERTAINTY_CAP, max(0.0, item.phase_uncertainty)),
        )
        for item in aligned
    )
    band = phase_quality_band(mad)
    consistency = effective / total if total > 0 else 0.0
    phase_confidence = _event_count_cap(effective) * band * consistency
    value = (
        _event_count_cap(max(0.0, effective - off_phase_weight))
        * band
        * min(1.0, consistency / SPARSE_CONSISTENCY_TOLERANCE)
    )
    return _SparseSupport(
        round(min(1.0, max(0.0, value)), 6),
        aligned,
        effective,
        band,
        round(min(1.0, max(0.0, phase_confidence)), 6),
        round(off_phase_weight, 6),
        round(consistency, 6),
    )


def _sparse_off_phase(
    item: PhysicalRestartEvent, *, period: int, phase_offset: float
) -> bool:
    return (
        abs(_signed_phase_residual(_event_at(item), phase_offset, period))
        > _event_tolerance(period, item)
    )


def _phase_coherent(left: PhysicalRestartEvent, right: PhysicalRestartEvent) -> bool:
    """Whether two restarts share a phase at any candidate period."""

    return any(
        abs(_signed_phase_residual(_event_at(left), _event_at(right), period))
        <= max(_event_tolerance(period, left), _event_tolerance(period, right))
        for period in CANDIDATE_PERIODS
    )


def _sparse_structure_reasons(
    *,
    period: int,
    support: _SparseSupport,
    normal_candidate: CandidateScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
) -> tuple[str, ...]:
    """Per-candidate sparse establishment structure (SE1-SE5).

    SE1 effective aligned restarts and SE2 aligned span use the existing
    schema-3 establishment gate table.  SE3 needs the existing phase
    precision.  SE4 requires the aligned restarts to refute every longer
    candidate period that is a multiple of this one (same-clock repeats can
    never do that), and SE5 reuses the existing H3 harmonic divisor blockers.
    SE4/SE5 failures mark the period as ambiguous.
    """

    if support.compact_support is not None:
        from .companion_restart_phase2_learned_state import compact_sparse_structure_reasons

        compact = support.compact
        return compact_sparse_structure_reasons(
            compact.state,
            period=period,
            support=support.compact_support,
            normal_candidate=normal_candidate,
            ring_windows=compact.ring_windows,
            ring_events=compact.ring_events,
            phase_offset=None,
        )
    if not support.aligned:
        return ()
    times = sorted(_event_at(item) for item in support.aligned)
    refuted = all(
        any(
            _circular_distance(value, times[0], multiple)
            > candidate_phase_tolerance(multiple)
            for value in times
        )
        for multiple in CANDIDATE_PERIODS
        if multiple > period and multiple % period == 0
    )
    _h2_harmonic, h3_harmonic = _harmonic_blockers(period, normal_candidate, windows)
    if not refuted or h3_harmonic:
        return (SPARSE_PERIOD_AMBIGUOUS_REASON,)
    gate = SCHEDULE_GATES[period]
    if (
        support.effective >= gate.established_events
        and times[-1] - times[0] >= gate.established_span
        and support.quality_band >= SPARSE_PHASE_QUALITY_BAND_MINIMUM
        and support.phase_confidence >= SPARSE_PHASE_CONFIDENCE_MINIMUM
    ):
        return (SPARSE_STRUCTURE_REASON,)
    return ()


def _relationship_endpoint_ids(
    relationships: Iterable[IntervalRelationship],
) -> frozenset[str]:
    return frozenset(
        value
        for item in relationships
        for value in (item.left_event_id, item.right_event_id)
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
    sparse: _SparseEvidence,
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
                sparse=sparse,
            )
            candidate_values.append(candidate)
            normal_values.append(normal)

        # Normal-only candidates: the schema-3 recent phase (unchanged
        # identity) and the long-horizon phase of the persisted sparse
        # restarts, each only where no candidate of this period already sits.
        normal_candidate = normal_score.candidate(period)
        phases = []
        if normal_candidate.phase_offset is not None and bool(
            normal_candidate.fundamental_period_confidence > 0
            or normal_candidate.phase_confidence > 0
            or any(item.candidate_period_seconds == period for item in relationships)
        ):
            phases.append(float(normal_candidate.phase_offset) % period)
        long_horizon_phase, _mad, aligned_count, _recent = _phase_cluster(
            sparse.events, period
        )
        if long_horizon_phase is not None and aligned_count > 0:
            phases.append(float(long_horizon_phase) % period)
        for phase in phases:
            if any(
                _circular_distance(item.phase_offset, phase, period)
                <= item.phase_tolerance
                for item in candidate_values
                if item.candidate_period_seconds == period
            ):
                continue
            candidate, normal = _normal_only_candidate(
                server_key=server_key,
                normal_candidate=normal_candidate,
                phase=phase,
                relationships=relationships,
                windows=windows,
                events=events,
                sparse=sparse,
            )
            candidate_values.append(candidate)
            normal_values.append(normal)

    return _assemble_ledgers(
        server_key=server_key,
        candidate_values=candidate_values,
        normal_values=normal_values,
        windows=windows,
        high=high,
        streaks=streaks,
    )


def _assemble_ledgers(
    *,
    server_key: str,
    candidate_values: list,
    normal_values: list,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    high: tuple[IntervalRelationship, ...],
    streaks: tuple[CadenceStreak, ...],
) -> tuple[
    tuple[AuthorityCandidate, ...], NormalEvidenceLedger, HighEvidenceLedger
]:
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
    sparse: _SparseEvidence,
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
    gate_bonus = 0.0
    for streak in chosen_streaks:
        minimum_quality = min(
            quality_map[item] for item in streak.relationship_ids
        )
        shape = (1.0 - 2.0 ** -(streak.interval_count - 1)) * minimum_quality
        streak_bonus_values.append(
            (streak.streak_id, round(STREAK_BONUS_COEFFICIENT * shape, 6))
        )
        gate_bonus += round(GATE_STREAK_BONUS_COEFFICIENT * shape, 6)
    streak_bonus = sum(value for _identity, value in streak_bonus_values)
    high_authority = min(HIGH_AUTHORITY_CAP, base + streak_bonus)
    gate_authority = min(HIGH_AUTHORITY_CAP, base + gate_bonus)

    phase_streak = _phase_defining_streak(chosen_streaks)
    if phase_streak is not None:
        phase_offset = phase_streak.phase_estimate % period
        phase_spread = phase_streak.phase_spread
    else:
        phase_offset, phase_spread = _relationship_phase(component, period)
    phase_quality = max(0.0, 1.0 - phase_spread / tolerance)
    phase_authority = high_authority * phase_quality
    gate_phase_authority = gate_authority * phase_quality
    normal = _normal_candidate_evidence(
        period=period,
        phase_offset=phase_offset,
        tolerance=tolerance,
        normal_candidate=normal_candidate,
        relationships=all_relationships,
        windows=windows,
        events=events,
        sparse=sparse,
        excluded_event_ids=_relationship_endpoint_ids(component),
    )
    aligned_normal = normal.aligned_normal_confidence
    combined = high_authority + (1.0 - high_authority) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * aligned_normal
    )

    h2_harmonic, h3_harmonic = _harmonic_blockers(
        period, normal_candidate, windows
    )
    h1 = len(component) >= 1 and gate_authority >= H1_AUTHORITY_MINIMUM
    has_h2_streak = any(
        item.interval_count >= 2 and item.event_count >= 3
        for item in chosen_streaks
    )
    h2 = bool(
        has_h2_streak
        and gate_authority >= H2_AUTHORITY_MINIMUM
        and gate_phase_authority >= H2_PHASE_AUTHORITY_MINIMUM
        and not h2_harmonic
    )
    h3_structure = bool(
        any(item.interval_count >= 3 for item in chosen_streaks)
        or (has_h2_streak and len(component) >= 3)
    )
    h3 = bool(
        h3_structure
        and gate_authority >= H3_AUTHORITY_MINIMUM
        and gate_phase_authority >= H3_PHASE_AUTHORITY_MINIMUM
        and not h3_harmonic
    )
    h4 = bool(
        h3 and len(component) >= 4 and gate_authority >= H4_AUTHORITY_MINIMUM
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
    reasons.update(_sparse_reason_codes(normal))
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
    phase: float,
    relationships: tuple[IntervalRelationship, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    sparse: _SparseEvidence,
    compact: "_Compact | None" = None,
) -> tuple[AuthorityCandidate, NormalCandidateEvidence]:
    period = normal_candidate.period_seconds
    phase = float(phase) % period
    tolerance = candidate_phase_tolerance(period)
    normal = _normal_candidate_evidence(
        period=period,
        phase_offset=phase,
        tolerance=tolerance,
        normal_candidate=normal_candidate,
        relationships=relationships,
        windows=windows,
        events=events,
        sparse=sparse,
        excluded_event_ids=frozenset(),
    )
    times = [
        value
        for item in relationships
        if item.relationship_id in normal.aligned_relationship_ids
        for value in (item.left_at, item.right_at)
    ]
    if compact is None:
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
    else:
        # Compact identity: stable under folding (no relationship IDs).
        from .companion_restart_phase2_learned_state import compact_folded_normal_relationships

        defining = ()
        regime_id = _stable_id(
            "authority-compact-normal-regime",
            AUTHORITY_SEMANTIC_VERSION,
            server_key,
            period,
            _normalized_phase_cluster(phase, tolerance, period),
        )
        candidate_id = _stable_id(
            "authority-compact-normal-candidate",
            AUTHORITY_SEMANTIC_VERSION,
            regime_id,
            round(phase, 6),
            normal.aligned_normal_confidence,
        )
        folded = compact_folded_normal_relationships(
            compact.state, period=period, phase_offset=phase, tolerance=tolerance
        )
        times.extend(value for value in (folded.first_at, folded.last_at) if value is not None)
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
                min(
                    SPARSE_EVIDENCE_CEILING,
                    SPARSE_EVIDENCE_CEILING * normal.aligned_normal_confidence,
                ),
                6,
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
                *_sparse_reason_codes(normal),
            ),
        ),
        normal,
    )


def _sparse_reason_codes(normal: NormalCandidateEvidence) -> tuple[str, ...]:
    return tuple(
        item
        for item in normal.reason_codes
        if item
        in {
            SPARSE_STRUCTURE_REASON,
            SPARSE_PERIOD_AMBIGUOUS_REASON,
            SPARSE_PROPORTIONAL_ANOMALY_REASON,
        }
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
    sparse: _SparseEvidence,
    excluded_event_ids: frozenset[str],
    compact_support: tuple[float, tuple[str, ...], bool] | None = None,
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
    # Stage 8.3: the sparse input is the long-horizon per-event statistic over
    # persisted restarts, not schema-3's recent-window (7 day / 16 event)
    # pair-derived confidence, which also re-counted continuous intervals.
    if compact_support is not None:
        aligned_confidence, sparse_reasons, proportional = compact_support
    else:
        support = _sparse_support(
            period=period,
            phase_offset=phase_offset,
            sparse=sparse,
            excluded_event_ids=excluded_event_ids,
        )
        aligned_confidence = support.value
        sparse_reasons = _sparse_structure_reasons(
            period=period,
            support=support,
            normal_candidate=normal_candidate,
            windows=windows,
        )
        proportional = bool(
            support.off_phase_weight > 0
            and support.consistency >= SPARSE_CONSISTENCY_TOLERANCE
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
            LONG_HORIZON_SPARSE_REASON,
            *sparse_reasons,
            *((SPARSE_PROPORTIONAL_ANOMALY_REASON,) if proportional else ()),
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
    relationships: tuple[IntervalRelationship, ...] = (),
    streaks: tuple[CadenceStreak, ...] = (),
    sparse: _SparseEvidence | None = None,
    compact: "_Compact | None" = None,
) -> AuthorityDecision:
    if sparse is None:
        sparse = _sparse_evidence(events, prior)
    ranked = tuple(sorted(candidates, key=_candidate_rank, reverse=True))
    previous_incumbent = prior.incumbent_shadow_regime if prior is not None else None
    event_map = {item.event_id: item for item in events}
    relationship_map = {item.relationship_id: item for item in relationships}

    if (
        previous_incumbent is None
        or previous_incumbent.status is RegimeRecordStatus.LIKELY
    ):
        # A LIKELY regime was never established: it is re-acquired from the
        # evidence on every rebuild (keeping its identity when the same
        # period and phase is selected) and never promoted by a rebuild alone.
        return _acquisition_decision(
            server_key=server_key,
            normal_score=normal_score,
            candidates=candidates,
            ranked=ranked,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            windows=windows,
            events=events,
            event_map=event_map,
            relationship_map=relationship_map,
            streaks=streaks,
            previous=previous_incumbent,
            sparse=sparse,
            compact=compact,
            relationships=relationships,
            prior=prior,
        )

    candidates, incumbent_candidate = _time_shift_incumbent_candidate(
        previous_incumbent,
        candidates,
        _time_shift_authority_phase(previous_incumbent.reason_codes),
        authority_until=_time_shift_authority_until(previous_incumbent.reason_codes),
        sparse=sparse,
        relationship_map=relationship_map,
    )
    carried = bool(
        incumbent_candidate is not None
        and _time_shift_authority_phase(incumbent_candidate.reason_codes) is not None
    )
    if carried:
        ranked = tuple(sorted(candidates, key=_candidate_rank, reverse=True))
    incumbent = previous_incumbent
    confirmation_reasons: tuple[str, ...] = ()
    # The candidates before any sparse confirmation of the incumbent's
    # candidate: an adopted time shift re-anchors the regime, after which that
    # confirmation (of the previous phase) no longer applies.
    unconfirmed_candidates = candidates
    if incumbent_candidate is not None:
        # A carrier's evidence is at the authority phase, not the regime's:
        # it neither upgrades the origin nor confirms a sparse regime (both
        # are judged at the regime's own phase once the native candidate
        # takes over).
        upgrade = bool(
            previous_incumbent.origin is AuthorityOrigin.NORMAL
            and not carried
            and (
                _continuity_upgrade_qualifies(
                    incumbent_candidate,
                    streaks=streaks,
                    relationship_map=relationship_map,
                    event_map=event_map,
                )
                if compact is None
                else _compact_continuity_upgrade_qualifies(incumbent_candidate, compact)
            )
        )
        if (
            not upgrade
            and not carried
            and previous_incumbent.origin is AuthorityOrigin.NORMAL
            and SPARSE_ESTABLISHMENT_REASON in previous_incumbent.reason_codes
        ):
            confirmed = (
                _sparse_confirmed_candidate(
                    incumbent_candidate,
                    regime=previous_incumbent,
                    relationships=relationships,
                    relationship_map=relationship_map,
                    event_map=event_map,
                    sparse=sparse,
                )
                if compact is None
                else _compact_sparse_confirmed_candidate(
                    incumbent_candidate, regime=previous_incumbent, compact=compact
                )
            )
            if confirmed is not incumbent_candidate:
                candidates = tuple(
                    confirmed
                    if item.candidate_id == incumbent_candidate.candidate_id
                    else item
                    for item in candidates
                )
                ranked = tuple(sorted(candidates, key=_candidate_rank, reverse=True))
                incumbent_candidate = confirmed
                confirmation_reasons = (SPARSE_CONFIRMATION_REASON,)
        incumbent = _regime_from_candidate(
            incumbent_candidate,
            status=RegimeRecordStatus.ESTABLISHED,
            existing=previous_incumbent,
            upgrade_origin=upgrade,
        )
    upgrade_reasons = (
        (ORIGIN_UPGRADE_REASON,)
        if ORIGIN_UPGRADE_REASON in incumbent.reason_codes
        else ()
    ) + (
        (TIME_SHIFT_ADOPTED_REASON,)
        if TIME_SHIFT_ADOPTED_REASON in incumbent.reason_codes
        else ()
    ) + (
        (HISTORICAL_ACQUISITION_REASON,)
        if HISTORICAL_ACQUISITION_REASON in incumbent.reason_codes
        else ()
    ) + confirmation_reasons

    arguments = dict(
        server_key=server_key,
        normal_score=normal_score,
        candidates=candidates,
        ranked=ranked,
        normal_ledger=normal_ledger,
        high_ledger=high_ledger,
        windows=windows,
        events=events,
        event_map=event_map,
        relationship_map=relationship_map,
        prior=prior,
        relationships=relationships,
        streaks=streaks,
        incumbent=incumbent,
        incumbent_candidate=incumbent_candidate,
        upgrade_reasons=upgrade_reasons,
        sparse=sparse,
        compact=compact,
    )
    return _resolve_time_shift(
        _incumbent_decision(**arguments),
        arguments,
        unconfirmed_candidates=unconfirmed_candidates,
    )


def _acquisition_decision(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    candidates: tuple[AuthorityCandidate, ...],
    ranked: tuple[AuthorityCandidate, ...],
    normal_ledger: NormalEvidenceLedger,
    high_ledger: HighEvidenceLedger,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    event_map: dict[str, PhysicalRestartEvent],
    relationship_map: dict[str, IntervalRelationship],
    streaks: tuple[CadenceStreak, ...],
    previous: RegimeRecord | None,
    sparse: _SparseEvidence | None = None,
    compact: "_Compact | None" = None,
    relationships: tuple[IntervalRelationship, ...] = (),
    prior: AuthorityDecision | None = None,
) -> AuthorityDecision:
    """Initial acquisition for an unknown server or a never-established LIKELY one.

    The result depends on the evidence only, so an evidence-identical
    rebuild reproduces it.  A prior LIKELY regime keeps its regime ID (and
    defining evidence) when the same period and phase is selected again.
    When H3, sparse and two-interval establishment all fail, historical
    acquisition recovery is tried before any LIKELY or learning outcome.
    """

    recovery: list[AuthorityDecision | None] = []

    def historical_recovery() -> AuthorityDecision | None:
        if not recovery:
            recovery.append(
                _historical_shift_acquisition(
                    server_key=server_key,
                    normal_score=normal_score,
                    candidates=candidates,
                    ranked=ranked,
                    normal_ledger=normal_ledger,
                    high_ledger=high_ledger,
                    windows=windows,
                    events=events,
                    relationships=relationships or tuple(relationship_map.values()),
                    relationship_map=relationship_map,
                    streaks=streaks,
                    prior=prior,
                    sparse=sparse,
                    compact=compact,
                )
            )
        return recovery[0]

    high_h3 = tuple(item for item in ranked if item.h3_gate)
    high_h2 = tuple(item for item in ranked if item.h2_gate)

    def regime_for(
        candidate: AuthorityCandidate,
        *,
        status: RegimeRecordStatus,
        origin: AuthorityOrigin = AuthorityOrigin.CONTINUITY,
        established_at: float | None = None,
        extra_reasons: tuple[str, ...] = (),
    ) -> RegimeRecord:
        regime = _regime_from_candidate(candidate, status=status, origin=origin)
        if established_at is not None and regime.established_at is None:
            regime = replace(regime, established_at=established_at)
        if extra_reasons:
            regime = replace(
                regime,
                reason_codes=tuple(
                    [*regime.reason_codes]
                    + [item for item in extra_reasons if item not in regime.reason_codes]
                ),
            )
        if previous is not None and not _candidate_conflicts_regime(candidate, previous):
            reasons = list(regime.reason_codes)
            if (
                (
                    previous.origin is AuthorityOrigin.NORMAL
                    and origin is AuthorityOrigin.CONTINUITY
                )
                or ORIGIN_UPGRADE_REASON in previous.reason_codes
            ) and ORIGIN_UPGRADE_REASON not in reasons:
                reasons.append(ORIGIN_UPGRADE_REASON)
            regime = replace(
                regime,
                regime_id=previous.regime_id,
                defining_evidence_ids=previous.defining_evidence_ids,
                reason_codes=tuple(reasons),
            )
        return regime

    def selected_decision(
        *,
        state: RegimeState,
        selected: RegimeRecord,
        selected_candidate: AuthorityCandidate,
        usable: bool,
        reasons: tuple[str, ...],
    ) -> AuthorityDecision:
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
            prediction_usable=usable,
            countdown_safe=usable,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=selected_candidate,
            reasons=reasons,
        )

    if high_h3:
        selected_candidate = high_h3[0]
        return selected_decision(
            state=RegimeState.ESTABLISHED,
            selected=regime_for(selected_candidate, status=RegimeRecordStatus.ESTABLISHED),
            selected_candidate=selected_candidate,
            usable=True,
            reasons=("unknown_server_established_by_h3",),
        )

    sparse_choice = (
        _sparse_establishing_candidate(
            candidates=candidates,
            windows=windows,
            relationship_map=relationship_map,
            sparse=sparse,
        )
        if sparse is not None
        else None
    )

    def sparse_decision() -> AuthorityDecision:
        sparse_candidate, sparse_established_at = sparse_choice

        def sparse_regime(candidate: AuthorityCandidate) -> RegimeRecord:
            return regime_for(
                candidate,
                status=RegimeRecordStatus.ESTABLISHED,
                origin=AuthorityOrigin.NORMAL,
                established_at=sparse_established_at,
                extra_reasons=(SPARSE_ESTABLISHMENT_REASON,),
            )

        # Intervals that were never establishment evidence confirm at once,
        # so the first decision equals every later identical rebuild.
        confirmed = (
            _sparse_confirmed_candidate(
                sparse_candidate,
                regime=sparse_regime(sparse_candidate),
                relationships=tuple(relationship_map.values()),
                relationship_map=relationship_map,
                event_map=event_map,
                sparse=sparse,
            )
            if compact is None
            else _compact_sparse_confirmed_candidate(
                sparse_candidate, regime=sparse_regime(sparse_candidate), compact=compact
            )
        )
        reasons: tuple[str, ...] = (SPARSE_ESTABLISHMENT_REASON,)
        decision_candidates = candidates
        if confirmed is not sparse_candidate:
            reasons = (SPARSE_ESTABLISHMENT_REASON, SPARSE_CONFIRMATION_REASON)
            decision_candidates = tuple(
                confirmed if item.candidate_id == sparse_candidate.candidate_id else item
                for item in candidates
            )
        selected = sparse_regime(confirmed)
        return _decision(
            server_key=server_key,
            state=RegimeState.ESTABLISHED,
            selected=selected,
            incumbent=selected,
            regimes=(selected,),
            candidates=decision_candidates,
            contexts=(),
            strongest=None,
            normal_ledger=normal_ledger,
            high_ledger=high_ledger,
            cycle_visible=True,
            prediction_usable=True,
            countdown_safe=True,
            suspicion_level=0,
            suspicion_ids=(),
            selected_candidate=confirmed,
            reasons=reasons,
        )

    if high_h2:
        selected_candidate = high_h2[0]
        contradictions = _AcquisitionContradictions(
            candidates=candidates, windows=windows, events=events
        )
        streak = (
            _two_interval_establishing_streak(
                selected_candidate,
                streaks=streaks,
                relationship_map=relationship_map,
                event_map=event_map,
                contradictions=contradictions,
            )
            if compact is None
            else _compact_two_interval_streak(selected_candidate, compact, contradictions)
        )
        conflict_pending = bool(
            streak is not None
            and sparse is not None
            and _two_interval_sparse_conflict_pending(
                selected_candidate,
                streak=streak,
                candidates=candidates,
                normal_score=normal_score,
                windows=windows,
                relationship_map=relationship_map,
                events=events,
                sparse=sparse,
            )
        )
        if streak is not None and not conflict_pending:
            return selected_decision(
                state=RegimeState.ESTABLISHED,
                selected=regime_for(
                    selected_candidate,
                    status=RegimeRecordStatus.ESTABLISHED,
                    established_at=streak.end_at,
                ),
                selected_candidate=selected_candidate,
                usable=True,
                reasons=(TWO_INTERVAL_ESTABLISHMENT_REASON,),
            )
        if sparse_choice is not None:
            return sparse_decision()
        recovered = historical_recovery()
        if recovered is not None:
            return recovered
        return selected_decision(
            state=RegimeState.LIKELY,
            selected=regime_for(selected_candidate, status=RegimeRecordStatus.LIKELY),
            selected_candidate=selected_candidate,
            usable=False,
            reasons=(
                "unknown_server_likely_by_h2",
                *((TWO_INTERVAL_SPARSE_CONFLICT_REASON,) if conflict_pending else ()),
            ),
        )

    if sparse_choice is not None:
        return sparse_decision()

    recovered = historical_recovery()
    if recovered is not None:
        return recovered

    if normal_score.selected_period_seconds is not None:
        selected_candidate = _normal_selected_candidate(normal_score, ranked)
        if selected_candidate is not None:
            # Stage 8.5: a schema-3 selection identifies a LIKELY normal
            # regime only.  Normal-origin establishment comes from the
            # schema-4 sparse establishment gate above, so its confidence,
            # countdown and warning follow one model.
            return selected_decision(
                state=RegimeState.LIKELY,
                selected=regime_for(
                    selected_candidate,
                    status=RegimeRecordStatus.LIKELY,
                    origin=AuthorityOrigin.NORMAL,
                ),
                selected_candidate=selected_candidate,
                usable=False,
                reasons=("normal_scorer_remains_available_for_unknown_server",),
            )

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


@dataclass(frozen=True)
class _AcquisitionContradictions:
    candidates: tuple[AuthorityCandidate, ...]
    windows: tuple[ExpectedWindowLedgerRecord, ...]
    events: tuple[PhysicalRestartEvent, ...]


def _two_interval_establishing_streak(
    candidate: AuthorityCandidate,
    *,
    streaks: tuple[CadenceStreak, ...],
    relationship_map: dict[str, IntervalRelationship],
    event_map: dict[str, PhysicalRestartEvent],
    contradictions: _AcquisitionContradictions | None,
) -> CadenceStreak | None:
    """The streak that establishes ``candidate`` on two intervals, if any.

    Requires the existing H2 gate (quality, streak, phase authority, no H2
    harmonic blocker), no harmonic blocker at all (so 6/8/12 h still need
    their divisors resolved for H3), H3-equivalent phase precision, and a
    maximal streak of at least two consecutive high-eligible DIRECT intervals
    in one continuity chain whose (at least three) endpoints are all
    authentic.  With ``contradictions`` it also requires no competing H2
    candidate, no active genuine miss in phase from the streak onwards and no
    authentic off-phase restart after the streak.
    """

    if (
        not candidate.h2_gate
        or candidate.harmonic_blockers
        or candidate.phase_quality < TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM
    ):
        return None
    streak_map = {item.streak_id: item for item in streaks}
    qualifying = []
    for identity in candidate.maximal_streak_ids:
        streak = streak_map.get(identity)
        if streak is None or streak.interval_count < 2 or streak.event_count < 3:
            continue
        relationships = tuple(
            relationship_map.get(item) for item in streak.relationship_ids
        )
        if any(item is None for item in relationships):
            continue
        if not all(
            item.high_authority_eligible
            and item.relationship_kind is RelationshipKind.DIRECT
            and not item.intervening_event_ids
            and item.continuity_qualified
            and item.continuity_chain_id == streak.continuity_chain_id
            for item in relationships
        ):
            continue
        endpoint_ids = {
            value
            for item in relationships
            for value in (item.left_event_id, item.right_event_id)
        }
        endpoints = tuple(event_map.get(item) for item in sorted(endpoint_ids))
        if len(endpoints) < 3 or any(
            item is None or not _authentic_event(item) for item in endpoints
        ):
            continue
        if any(
            relationship_authority_quality(item, endpoints) <= 0.0
            for item in relationships
        ):
            continue
        qualifying.append(streak)
    streak = max(qualifying, key=lambda item: (item.end_at, item.streak_id), default=None)
    if streak is None or contradictions is None:
        return streak
    period = candidate.candidate_period_seconds
    tolerance = candidate.phase_tolerance
    if any(
        item.h2_gate
        and item.candidate_id != candidate.candidate_id
        and (
            item.candidate_period_seconds != period
            or _circular_distance(item.phase_offset, candidate.phase_offset, period)
            > tolerance
        )
        for item in contradictions.candidates
    ):
        return None
    if any(
        item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and item.candidate_period_seconds == period
        and _circular_distance(item.expected_phase_offset, candidate.phase_offset, period)
        <= tolerance
        and item.expected_at >= streak.start_at
        for item in contradictions.windows
    ):
        return None
    if any(
        _authentic_event(item)
        and _event_at(item) is not None
        and _event_at(item) > streak.end_at
        and _circular_distance(_event_at(item), candidate.phase_offset, period)
        > tolerance
        for item in contradictions.events
    ):
        return None
    return streak


def _two_interval_sparse_conflict_pending(
    candidate: AuthorityCandidate,
    *,
    streak: CadenceStreak,
    candidates: tuple[AuthorityCandidate, ...],
    normal_score: ScheduleScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    relationship_map: dict[str, IntervalRelationship],
    events: tuple[PhysicalRestartEvent, ...],
    sparse: _SparseEvidence,
) -> bool:
    """Stage 8.7: a two-interval streak against substantial sparse conflict.

    The conflict is substantial when a conflicting candidate (another
    period, or a phase outside tolerance) is credible sparse evidence in its
    own right, measured without the streak's restarts: it meets the existing
    sparse establishment structure (SE1-SE5: count, span, phase precision,
    period refutation and no harmonic blocker, so weak or ambiguous evidence
    never qualifies) and has no active in-phase genuine miss since its first
    aligned restart (SE6).  This is the competitor test the sparse gate
    itself applies, mirrored.

    The streak is then still provisional until one more independent genuine
    restart confirms its period and phase: a third consecutive interval, or
    an authentic restart observed after the streak, aligned with its phase,
    that is physically distinct from every streak endpoint.  Restarts are
    never inferred across unobserved gaps.
    """

    endpoint_ids = frozenset(
        value
        for identity in streak.relationship_ids
        if identity in relationship_map
        for value in (
            relationship_map[identity].left_event_id,
            relationship_map[identity].right_event_id,
        )
    )
    if not _substantial_sparse_conflict(
        candidate,
        excluded_event_ids=endpoint_ids,
        candidates=candidates,
        normal_score=normal_score,
        windows=windows,
        relationship_map=relationship_map,
        sparse=sparse,
    ):
        return False
    if streak.interval_count >= 3:
        return False
    period = candidate.candidate_period_seconds
    endpoint_times = tuple(
        _event_at(item)
        for item in events
        if item.event_id in endpoint_ids and _event_at(item) is not None
    )
    for item in events:
        when = _event_at(item)
        if (
            item.event_id in endpoint_ids
            or when is None
            or when <= streak.end_at
            or not _authentic_event(item)
            or _event_weight(item) <= 0
            or _sparse_off_phase(item, period=period, phase_offset=candidate.phase_offset)
        ):
            continue
        tolerance = _event_tolerance(period, item)
        if all(abs(when - value) > tolerance for value in endpoint_times):
            return False
    return True


def _substantial_sparse_conflict(
    candidate: AuthorityCandidate,
    *,
    excluded_event_ids: frozenset[str],
    candidates: tuple[AuthorityCandidate, ...],
    normal_score: ScheduleScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    relationship_map: dict[str, IntervalRelationship],
    sparse: _SparseEvidence,
) -> bool:
    for item in candidates:
        if item.candidate_id == candidate.candidate_id or not _candidates_conflict(
            item, candidate
        ):
            continue
        period = item.candidate_period_seconds
        support = _sparse_support(
            period=period,
            phase_offset=item.phase_offset,
            sparse=sparse,
            excluded_event_ids=excluded_event_ids
            | _relationship_endpoint_ids(
                relationship_map[value]
                for value in item.high_relationship_ids
                if value in relationship_map
            ),
            candidate=item,
        )
        bounds = _support_bounds(support)
        if bounds is None or SPARSE_STRUCTURE_REASON not in _sparse_structure_reasons(
            period=period,
            support=support,
            normal_candidate=normal_score.candidate(period),
            windows=windows,
        ):
            continue
        first = bounds[0]
        if _compact_miss_since(
            sparse.compact,
            period=period,
            phase_offset=item.phase_offset,
            tolerance=item.phase_tolerance,
            since=first,
        ):
            continue
        if any(
            window.outcome is ExpectedWindowOutcome.GENUINE_MISS
            and window.negative_penalty_active
            and window.candidate_period_seconds == period
            and window.expected_at >= first
            and _circular_distance(window.expected_phase_offset, item.phase_offset, period)
            <= item.phase_tolerance
            for window in windows
        ):
            continue
        return True
    return False


def _continuity_upgrade_qualifies(
    candidate: AuthorityCandidate,
    *,
    streaks: tuple[CadenceStreak, ...],
    relationship_map: dict[str, IntervalRelationship],
    event_map: dict[str, PhysicalRestartEvent],
) -> bool:
    """A NORMAL-origin incumbent's matching candidate earns continuity authority.

    H3, or H2 meeting the two-interval establishment evidence contract; never
    a candidate with any harmonic blocker.  Contradictions are handled by the
    ordinary incumbent suspicion and challenger logic.
    """

    if candidate.harmonic_blockers:
        return False
    if candidate.h3_gate:
        return True
    return (
        _two_interval_establishing_streak(
            candidate,
            streaks=streaks,
            relationship_map=relationship_map,
            event_map=event_map,
            contradictions=None,
        )
        is not None
    )


def _sparse_establishing_candidate(
    *,
    candidates: tuple[AuthorityCandidate, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    relationship_map: dict[str, IntervalRelationship],
    sparse: _SparseEvidence,
    before_at: float | None = None,
) -> tuple[AuthorityCandidate, float] | None:
    """Stage 8.5 sparse establishment gate (acquisition only).

    A candidate qualifies when its sparse structure holds (SE1-SE5, encoded
    as a candidate reason code), it has no harmonic blocker, no active
    genuine miss at its period and phase since its first aligned restart
    (SE6), and its sparse slot alone (``S * a``) displays at the 80 %
    milestone (SE7).  Continuous intervals never help establish a sparse
    regime; they confirm it.  It is withheld when any conflicting schedule
    is H2, or another conflicting candidate meets the sparse structure with
    comparable support.  ``established_at`` is its latest aligned sparse
    restart.  ``before_at`` (historical acquisition recovery) evaluates the
    aligned restarts strictly before it; the candidates must then carry the
    matching pre-boundary values (``_historical_prefix_candidate``).  SE6
    still counts every genuine miss from the first aligned restart on.
    """

    qualifying = []
    for candidate in candidates:
        if (
            SPARSE_STRUCTURE_REASON not in candidate.reason_codes
            or candidate.harmonic_blockers
            or int(
                round(
                    SPARSE_EVIDENCE_CEILING
                    * min(1.0, candidate.aligned_normal_confidence)
                    * 100
                )
            )
            < SPARSE_ESTABLISHMENT_MINIMUM_PERCENT
        ):
            continue
        period = candidate.candidate_period_seconds
        support = _sparse_support(
            period=period,
            phase_offset=candidate.phase_offset,
            sparse=sparse,
            excluded_event_ids=_relationship_endpoint_ids(
                relationship_map[item]
                for item in candidate.high_relationship_ids
                if item in relationship_map
            ),
            candidate=candidate,
            before_at=before_at,
        )
        bounds = _support_bounds(support)
        if bounds is None:
            continue
        times = list(bounds)
        contradicted = _compact_miss_since(
            sparse.compact,
            period=period,
            phase_offset=candidate.phase_offset,
            tolerance=candidate.phase_tolerance,
            since=min(times),
        ) or any(
            item.outcome is ExpectedWindowOutcome.GENUINE_MISS
            and item.negative_penalty_active
            and item.candidate_period_seconds == period
            and item.expected_at >= min(times)
            and _circular_distance(item.expected_phase_offset, candidate.phase_offset, period)
            <= candidate.phase_tolerance
            for item in windows
        )
        if contradicted:
            continue
        qualifying.append((candidate, max(times)))
    if not qualifying:
        return None
    best, established_at = max(
        qualifying,
        key=lambda item: (
            item[0].combined_confidence,
            item[0].aligned_normal_confidence,
            item[0].regime_id,
        ),
    )
    for item in candidates:
        if item.candidate_id == best.candidate_id or not _candidates_conflict(item, best):
            continue
        if item.h2_gate:
            return None
        if (
            SPARSE_STRUCTURE_REASON in item.reason_codes
            and item.aligned_normal_confidence
            >= best.aligned_normal_confidence - SPARSE_COMPETITOR_MARGIN
        ):
            return None
    return best, established_at


def _candidates_conflict(left: AuthorityCandidate, right: AuthorityCandidate) -> bool:
    period = right.candidate_period_seconds
    return bool(
        left.candidate_period_seconds != period
        or _circular_distance(left.phase_offset, right.phase_offset, period)
        > right.phase_tolerance
    )


def _full_interval_quality(
    relationship: IntervalRelationship,
    event_map: dict[str, PhysicalRestartEvent],
) -> float:
    """Quality of a fully observed DIRECT interval between genuine restarts.

    High-eligible (offline-observed) intervals qualify as they are.  An
    interval that is high-ineligible only because of its endpoint evidence
    class (query-visible restarts) qualifies when everything else holds:
    one continuity chain, continuous healthy coverage and query health, no
    intervening authentic or unobservable episode, residual within tolerance
    and both endpoints individually authentic, learning-eligible restarts.
    It never becomes high-eligible.  The quality is the existing
    relationship formula (authenticity, coverage ratio, phase fit).
    """

    left = event_map.get(relationship.left_event_id)
    right = event_map.get(relationship.right_event_id)
    if (
        left is None
        or right is None
        or relationship.relationship_kind is not RelationshipKind.DIRECT
        or relationship.multiplier != 1
        or relationship.intervening_event_ids
        or not relationship.continuity_qualified
        or relationship.tolerance_seconds <= 0
        or not all(_authentic_event(item) and _event_weight(item) > 0 for item in (left, right))
    ):
        return 0.0
    if not relationship.high_authority_eligible:
        ineligible = {
            item
            for item in relationship.reason_codes
            if item.startswith("high_ineligible_")
        }
        if not ineligible or not ineligible <= _FULL_INTERVAL_PERMITTED_HIGH_INELIGIBILITY:
            return 0.0
    auth_quality = min(1.0, min(left.authenticity, right.authenticity) / 0.85)
    coverage_quality = min(1.0, relationship.observed_ratio / 0.98)
    phase_fit = 1.0 - 0.20 * min(
        1.0, abs(relationship.signed_residual_seconds) / relationship.tolerance_seconds
    )
    return round(max(0.0, min(auth_quality, coverage_quality, phase_fit)), 6)


def _sparse_confirmed_candidate(
    candidate: AuthorityCandidate,
    *,
    regime: RegimeRecord,
    relationships: tuple[IntervalRelationship, ...],
    relationship_map: dict[str, IntervalRelationship],
    event_map: dict[str, PhysicalRestartEvent],
    sparse: _SparseEvidence,
) -> AuthorityCandidate:
    """Strong confirmation of a sparse-established regime (display magnitude).

    Fully observed matching intervals that are not establishment evidence
    add ``1 - prod(1 - 0.75 q)`` in the continuous slot (in place of, never
    added to, the candidate's own high authority).  High-eligible intervals
    never enter the sparse slot (their endpoints are excluded), so they are
    never establishment evidence and always confirm.  Other full intervals
    (query-visible endpoints, which are sparse evidence) confirm only when
    they end after establishment.  Confirming endpoints leave the sparse
    slot, so no restart counts in both.  Two matching timestamps across an
    unobserved gap are not an interval and never confirm.  Gates, high
    authority and identities are unchanged.
    """

    established_at = regime.established_at
    if established_at is None:
        return candidate
    period = regime.candidate_period_seconds
    confirming = []
    for item in relationships:
        if (
            item.candidate_period_seconds != period
            or (not item.high_authority_eligible and item.right_at <= established_at)
            or _circular_distance(
                _relationship_phase_value(item, period), regime.phase_offset, period
            )
            > regime.phase_tolerance
        ):
            continue
        quality = _full_interval_quality(item, event_map)
        if quality > 0:
            confirming.append((item, quality))
    if not confirming:
        return candidate
    residual = 1.0
    for _item, quality in confirming:
        residual *= 1.0 - SPARSE_CONFIRMATION_INTERVAL_WEIGHT * quality
    confirmation = min(HIGH_AUTHORITY_CAP, 1.0 - residual)
    excluded = _relationship_endpoint_ids(
        [
            *(
                relationship_map[value]
                for value in candidate.high_relationship_ids
                if value in relationship_map
            ),
            *(item for item, _quality in confirming),
        ]
    )
    support = _sparse_support(
        period=period,
        phase_offset=candidate.phase_offset,
        sparse=sparse,
        excluded_event_ids=excluded,
    )
    continuous = max(candidate.high_authority, confirmation)
    combined = continuous + (1.0 - continuous) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * support.value
    )
    combined = round(max(candidate.combined_confidence, combined), 6)
    return replace(
        candidate,
        combined_confidence=combined,
        reason_codes=tuple(sorted({*candidate.reason_codes, SPARSE_CONFIRMATION_REASON})),
    )


def _incumbent_decision(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    candidates: tuple[AuthorityCandidate, ...],
    ranked: tuple[AuthorityCandidate, ...],
    normal_ledger: NormalEvidenceLedger,
    high_ledger: HighEvidenceLedger,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    event_map: dict[str, PhysicalRestartEvent],
    relationship_map: dict[str, IntervalRelationship],
    prior: AuthorityDecision | None,
    relationships: tuple[IntervalRelationship, ...],
    streaks: tuple[CadenceStreak, ...],
    incumbent: RegimeRecord,
    incumbent_candidate: AuthorityCandidate | None,
    upgrade_reasons: tuple[str, ...],
    sparse: _SparseEvidence,
    compact: "_Compact | None" = None,
) -> AuthorityDecision:
    """The established-incumbent path: suspicion, challengers and transitions."""

    last_aligned_at, suspicion_events, incumbent_state = _incumbent_alignment(
        incumbent, events, windows, compact
    )
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
        for item in suspicion_events
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
    if compact is not None:
        from .companion_restart_phase2_learned_state import (
            compact_folded_miss_ids,
            compact_folded_off_phase_ids,
        )

        folded_arguments = dict(
            period=incumbent.candidate_period_seconds,
            phase_offset=incumbent.phase_offset,
            tolerance=incumbent.phase_tolerance,
            since=last_aligned_at,
        )
        active_misses = (
            *active_misses,
            *compact_folded_miss_ids(incumbent_state, **folded_arguments),
        )
        active_off_phase = (
            *active_off_phase,
            *compact_folded_off_phase_ids(incumbent_state, **folded_arguments),
        )
        contexts = _compact_challenger_contexts(
            incumbent=incumbent,
            ranked=ranked,
            normal_ledger=normal_ledger,
            normal_score=normal_score,
            active_misses=active_misses,
            events=events,
            compact=compact,
        )
        recovery = {
            item.context_id: _compact_recovery_confirmations(item, incumbent_state, compact)
            for item in contexts
        }
    else:
        contexts = None
        recovery = None
    contexts = contexts if contexts is not None else tuple(
        _challenger_context(
            incumbent=incumbent,
            candidate=_post_establishment_view(
                item,
                established_at=incumbent.established_at,
                relationship_map=relationship_map,
                streaks=streaks,
                relationships=relationships,
                normal_score=normal_score,
                windows=windows,
                events=events,
                event_map=event_map,
                sparse=sparse,
            ),
            normal=_normal_for_candidate(normal_ledger, item),
            normal_context_strength=_schema3_context_strength(normal_score, item),
            active_miss_ids=active_misses,
            events=events,
        )
        for item in ranked
        if _candidate_conflicts_regime(item, incumbent)
    )
    recovery = recovery if recovery is not None else {
        item.context_id: _original_cycle_recovery_confirmations(
            incumbent=incumbent,
            context=item,
            relationships=relationships,
            event_map=event_map,
        )
        for item in contexts
    }
    activity = {
        item.context_id: _context_activity(
            item,
            incumbent=incumbent,
            last_aligned_at=last_aligned_at,
            confirmations=len(recovery[item.context_id]),
            prior=prior,
        )
        for item in contexts
    }
    contexts = tuple(
        _annotate_context_recovery(
            item,
            incumbent=incumbent,
            active=activity[item.context_id],
            confirmations=len(recovery[item.context_id]),
        )
        for item in contexts
    )
    active_contexts = tuple(
        item for item in contexts if activity[item.context_id]
    )
    # Evidence-only, so repeated rebuilds keep the same decision identity.
    original_cycle_reconfirmed = any(
        not activity[item.context_id]
        and item.authorization_state is ChallengerAuthorization.AUTHORIZED
        and item.last_evidence_at > (incumbent.established_at or 0.0)
        and len(recovery[item.context_id]) >= ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS
        for item in contexts
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
        if new_regime.established_at is None:
            # The challenger passed H3 on its post-establishment evidence; the
            # establishment boundary must exist so the superseded regime is an
            # ordinary challenger afterwards.
            new_regime = replace(
                new_regime, established_at=authorized_h3.last_evidence_at
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
        state = _stable_transition_state(prior, incumbent, authorized_h2)
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
                _recovery_progress_reason(
                    len(recovery.get(authorized_h2.context_id, ()))
                ),
                *upgrade_reasons,
            ),
        )

    active_high_relationships = max(
        (context_high_relationship_count(item) for item in active_contexts), default=0
    )
    coherent_strong_contradictions = active_high_relationships >= 2
    has_normal_context = any(
        item.normal_context_strength > 0
        or item.folded_normal_relationship_count > 0
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
                    and activity.get(strongest.context_id, False)
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
            _recovery_progress_reason(
                min(
                    (
                        len(recovery[item.context_id])
                        for item in active_contexts
                        if context_high_relationship_count(item) >= 2
                    ),
                    default=0,
                )
            ),
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
        if (
            not active_misses
            and active_high_relationships == 0
            and (
                _isolated_off_phase_restart(
                    active_off_phase, incumbent=incumbent, events=events
                )
                if compact is None
                else _compact_isolated_off_phase_restart(active_off_phase, incumbent_state, compact)
            )
        ):
            reasons = (*reasons, ISOLATED_OFF_PHASE_RESTART_REASON)
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
    if original_cycle_reconfirmed:
        reasons = (
            *reasons,
            "provisional_challenger_resolved_by_original_cycle_confirmations",
        )
    reasons = (*reasons, *upgrade_reasons)
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


def _incumbent_alignment(
    incumbent: RegimeRecord,
    events: tuple[PhysicalRestartEvent, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    compact: "_Compact | None",
) -> tuple[float, tuple[PhysicalRestartEvent, ...], object]:
    """Latest evidence aligned with ``incumbent`` and the events judged after it.

    Returns ``(last_aligned_at, suspicion_events, compact_incumbent_state)``.
    On the compact path the events are the unfolded ring and the state is the
    learned state describing ``incumbent`` (``None`` on the raw path).
    """

    if compact is None:
        return _last_aligned_evidence_at(incumbent, events, windows), events, None
    from .companion_restart_phase2_learned_state import (
        compact_last_aligned_evidence_at,
        knowledge_for_regime,
    )

    incumbent_state = knowledge_for_regime(compact.state, incumbent)
    last_aligned_at = compact_last_aligned_evidence_at(
        incumbent_state, ring_events=compact.ring_events, ring_windows=compact.ring_windows
    )
    return last_aligned_at, _compact_unfolded_events(compact), incumbent_state


# --------------------------------------------------------------------------
# Time Shift Detection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _TimeShift:
    """Independent restarts agreeing on one new phase of the incumbent period.

    ``event_ids`` are in time order and ``first_observed_at`` is the time of
    the first.  ``confirmed_at`` is the time of the confirming observation,
    or ``None`` while the shift is unconfirmed.
    """

    phase_offset: float
    event_ids: tuple[str, ...]
    confirmed_at: float | None
    first_observed_at: float


def _time_shift_assessment(
    incumbent: RegimeRecord,
    events: tuple[PhysicalRestartEvent, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    *,
    last_aligned_at: float,
) -> _TimeShift | None:
    """The time-shift hypothesis supported by the retained events, if any.

    Observations are authentic, learning-eligible restarts later than the
    incumbent's latest aligned evidence and outside its phase tolerance, so
    a restart back on the original phase restarts the count.  They are
    grouped in time order by proposed phase: a restart joins the first group
    whose first restart it matches within the incumbent tolerance, and counts
    only as a distinct scheduled occurrence.  A restart matching no group
    (an off-grid administrative restart) starts its own and is neutral to the
    others.  A genuine expected-window miss at a group's phase discards that
    group's earlier restarts.  Unmonitored time contributes nothing.

    Any repeated phase withholds the prediction.  Confirmation additionally
    needs the confirming restarts to be later than the latest restart of
    every other *repeated* phase: restarts that keep returning to several
    phases do not describe one shifted schedule (they are what a different
    interval looks like when seen sparsely) and stay with the cycle-change
    rules.  The first group confirmed wins and is described by exactly its
    confirming restarts, so identical history always yields the same phase
    and adoption time.
    """

    period = incumbent.candidate_period_seconds
    tolerance = incumbent.phase_tolerance
    observations = sorted(
        (_event_at(item), item.event_id)
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _event_at(item) > last_aligned_at
        and _circular_distance(_event_at(item), incumbent.phase_offset, period) > tolerance
    )
    groups: list[list[tuple[float, str]]] = []
    for at, event_id in observations:
        group = next(
            (
                item
                for item in groups
                if _circular_distance(at, item[0][0], period) <= tolerance
            ),
            None,
        )
        if group is None:
            groups.append([(at, event_id)])
        elif all(abs(at - other) > period / 2 for other, _event in group):
            group.append((at, event_id))

    def uncontradicted(group: list[tuple[float, str]]) -> list[tuple[float, str]]:
        anchor = group[0][0]
        missed_at = max(
            (
                item.expected_at
                for item in windows
                if item.outcome is ExpectedWindowOutcome.GENUINE_MISS
                and item.negative_penalty_active
                and item.candidate_period_seconds == period
                and item.expected_at > anchor
                and _circular_distance(item.expected_phase_offset, anchor, period)
                <= tolerance
            ),
            default=None,
        )
        if missed_at is None:
            return group
        return [item for item in group if item[0] > missed_at]

    groups = [item for item in map(uncontradicted, groups) if item]
    if not groups:
        return None

    def unchallenged(group: list[tuple[float, str]]) -> list[tuple[float, str]]:
        rival_at = max(
            (
                item[-1][0]
                for item in groups
                if item is not group and len(item) >= TIME_SHIFT_SUSPECT_OBSERVATIONS
            ),
            default=None,
        )
        if rival_at is None:
            return group
        return [item for item in group if item[0] > rival_at]

    confirmed = [
        item
        for item in map(unchallenged, groups)
        if len(item) >= TIME_SHIFT_CONFIRM_OBSERVATIONS
    ]
    if confirmed:
        members = min(
            confirmed,
            key=lambda item: (item[TIME_SHIFT_CONFIRM_OBSERVATIONS - 1][0], item[0][0]),
        )[:TIME_SHIFT_CONFIRM_OBSERVATIONS]
    else:
        members = max(groups, key=lambda item: (len(item), -item[0][0]))
    phase, _spread = _phase_estimate((at for at, _event in members), period)
    return _TimeShift(
        phase_offset=round(phase, 6) % period,
        event_ids=tuple(event_id for _at, event_id in members),
        confirmed_at=members[-1][0] if confirmed else None,
        first_observed_at=members[0][0],
    )


def _time_shift_authority_phase(reason_codes: Iterable[str]) -> float | None:
    """The authority phase recorded by a time-shift marker, if present."""

    for item in reason_codes:
        if item.startswith(TIME_SHIFT_AUTHORITY_PHASE_PREFIX):
            try:
                value = float(item[len(TIME_SHIFT_AUTHORITY_PHASE_PREFIX) :])
            except ValueError:
                return None
            return value if math.isfinite(value) and value >= 0 else None
    return None


def _time_shift_authority_until(reason_codes: Iterable[str]) -> float | None:
    """The pre-shift evidence boundary recorded on a sparse regime, if present."""

    for item in reason_codes:
        if item.startswith(TIME_SHIFT_AUTHORITY_UNTIL_PREFIX):
            try:
                value = float(item[len(TIME_SHIFT_AUTHORITY_UNTIL_PREFIX) :])
            except ValueError:
                return None
            return value if math.isfinite(value) and value >= 0 else None
    return None


def _time_shift_boundary(first_observed_at: float) -> float:
    """The persisted pre-shift boundary: never later than the first observation.

    Stored to the millisecond, rounded down, so the first shifted observation
    stays excluded and the value read back from the reason code is the value
    used when it was written.
    """

    return float(f"{math.floor(first_observed_at * 1000) / 1000:.3f}")


def _time_shift_markers(reason_codes: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        item
        for item in reason_codes
        if item.startswith(TIME_SHIFT_AUTHORITY_PHASE_PREFIX)
        or item.startswith(TIME_SHIFT_AUTHORITY_UNTIL_PREFIX)
    )


def _time_shift_sparse_regime(regime: RegimeRecord) -> bool:
    return bool(
        regime.origin is AuthorityOrigin.NORMAL
        and SPARSE_ESTABLISHMENT_REASON in regime.reason_codes
    )


def _time_shift_eligible(regime: RegimeRecord) -> bool:
    """Established regimes Time Shift Detection applies to.

    Continuity-origin and sparse-established regimes predict from their own
    phase.  Legacy schema-3-origin regimes predict from the schema-3 phase,
    so they stay excluded.
    """

    return bool(
        regime.status is RegimeRecordStatus.ESTABLISHED
        and (regime.origin is AuthorityOrigin.CONTINUITY or _time_shift_sparse_regime(regime))
    )


def _time_shift_preserved_candidate(
    source: AuthorityCandidate,
    *,
    before_at: float,
    sparse: _SparseEvidence,
    relationship_map: dict[str, IntervalRelationship],
) -> AuthorityCandidate:
    """``source`` with its sparse slot evaluated on restarts before ``before_at``.

    The existing sparse statistic and combination, at the source's own phase
    and with the source's own endpoint exclusions; only the restarts it is
    computed over change.  Gates, high authority and identity are unchanged.
    """

    support = _sparse_support(
        period=source.candidate_period_seconds,
        phase_offset=source.phase_offset,
        sparse=sparse,
        excluded_event_ids=_relationship_endpoint_ids(
            relationship_map[item]
            for item in source.high_relationship_ids
            if item in relationship_map
        ),
        candidate=source,
        before_at=before_at,
    )
    combined = source.high_authority + (1.0 - source.high_authority) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * support.value
    )
    return replace(
        source,
        aligned_normal_confidence=round(support.value, 6),
        combined_confidence=round(combined, 6),
    )


def _time_shift_incumbent_candidate(
    regime: RegimeRecord,
    candidates: tuple[AuthorityCandidate, ...],
    authority_phase: float | None,
    *,
    authority_until: float | None = None,
    sparse: _SparseEvidence | None = None,
    relationship_map: dict[str, IntervalRelationship] | None = None,
) -> tuple[tuple[AuthorityCandidate, ...], AuthorityCandidate | None]:
    """The candidate describing ``regime``, carrying shifted authority if needed.

    Without an authority phase this is the ordinary matching candidate.  A
    time-shifted regime earned its high evidence at ``authority_phase``; the
    interval evidence there still proves the period, so the best candidate
    at that phase is carried to the regime's phase (same evidence, gates and
    confidence, under the shifted regime's own identity) until the evidence
    at the regime's own phase is at least as strong.  Returns the candidates
    (with the carrier appended when it is used) and the selected candidate.

    A sparse-established regime earned a sparse statistic, not interval
    authority: the carrier's sparse slot is that statistic at the authority
    phase over the restarts before ``authority_until`` (the first shifted
    observation), and it is carried while the native candidate's confidence
    is lower and its gate no higher.  That includes a regime that shifted
    back to its authority phase: the native statistic there still counts the
    shifted restarts as off-phase.
    """

    native = _matching_candidate(regime, candidates)
    period = regime.candidate_period_seconds
    sparse_regime = _time_shift_sparse_regime(regime)
    if (
        authority_phase is None
        or not (regime.origin is AuthorityOrigin.CONTINUITY or sparse_regime)
        or (sparse_regime and (authority_until is None or sparse is None))
        or (
            not sparse_regime
            and _circular_distance(authority_phase, regime.phase_offset, period)
            <= regime.phase_tolerance
        )
    ):
        return candidates, native
    source = max(
        (
            item
            for item in candidates
            if item.candidate_period_seconds == period
            and _circular_distance(item.phase_offset, authority_phase, period)
            <= regime.phase_tolerance
        ),
        key=_candidate_rank,
        default=None,
    )
    markers = {f"{TIME_SHIFT_AUTHORITY_PHASE_PREFIX}{authority_phase % period:.6f}"}
    if sparse_regime and source is not None:
        source = _time_shift_preserved_candidate(
            source,
            before_at=authority_until,
            sparse=sparse,
            relationship_map=relationship_map or {},
        )
        markers.add(f"{TIME_SHIFT_AUTHORITY_UNTIL_PREFIX}{authority_until:.3f}")
    if source is None or (
        native is not None
        and (
            _candidate_rank(native)[:1] > _candidate_rank(source)[:1]
            or native.combined_confidence >= source.combined_confidence
            if sparse_regime
            else _candidate_rank(native)[:2] >= _candidate_rank(source)[:2]
        )
    ):
        return candidates, native
    phase = round(regime.phase_offset, 6) % period
    carrier = replace(
        source,
        candidate_id=_stable_id(
            "authority-time-shift-candidate",
            AUTHORITY_SEMANTIC_VERSION,
            source.candidate_id,
            phase,
        ),
        regime_id=regime.regime_id,
        phase_offset=phase,
        reason_codes=tuple(
            sorted({*source.reason_codes, TIME_SHIFT_ADOPTED_REASON, *markers})
        ),
    )
    return (*candidates, carrier), carrier


def _cycle_change_takes_precedence(
    base: AuthorityDecision, incumbent: RegimeRecord, shift: _TimeShift
) -> bool:
    """Whether the ordinary cycle-change rules own this decision.

    They do whenever a challenger has suspended or replaced the incumbent
    (level-two suspicion, a provisional transition or a new regime), unless
    every such challenger is the time shift itself: the incumbent period at
    the proposed phase.
    """

    if base.state in {
        RegimeState.TRANSITION_CONFIRMED,
        RegimeState.NEW_REGIME_PROVISIONAL,
        RegimeState.NEW_REGIME_ESTABLISHED,
    }:
        drivers = (base.strongest_challenger,)
    elif base.state is RegimeState.CHALLENGER_ACCUMULATING and base.suspicion_level >= 2:
        drivers = tuple(
            item
            for item in base.challenger_contexts
            if COUNTDOWN_SUSPENSION_LATCH_REASON in item.reason_codes
        ) or (base.strongest_challenger,)
    else:
        return False
    period = incumbent.candidate_period_seconds
    return any(
        item is None
        or item.candidate_period_seconds != period
        or _circular_distance(item.phase_offset, shift.phase_offset, period)
        > incumbent.phase_tolerance
        for item in drivers
    )


def _resolve_time_shift(
    base: AuthorityDecision,
    arguments: dict,
    *,
    unconfirmed_candidates: tuple[AuthorityCandidate, ...] | None = None,
) -> AuthorityDecision:
    """Apply Time Shift Detection to an established-incumbent decision.

    ``arguments`` are the ``_incumbent_decision`` arguments that produced
    ``base``.  A confirmed shift re-evaluates the decision with the incumbent
    re-anchored at the new phase (and is then assessed again, so later shifts
    in the retained history are followed; each adoption moves the regime's
    establishment boundary forward, which bounds the recursion).  An
    unconfirmed shift only annotates ``base``.
    """

    incumbent = arguments["incumbent"]
    if not _time_shift_eligible(incumbent):
        return base
    last_aligned_at, suspicion_events, _state = _incumbent_alignment(
        incumbent, arguments["events"], arguments["windows"], arguments["compact"]
    )
    shift = _time_shift_assessment(
        incumbent, suspicion_events, arguments["windows"], last_aligned_at=last_aligned_at
    )
    if shift is None or _cycle_change_takes_precedence(base, incumbent, shift):
        return base
    if shift.confirmed_at is not None:
        adopted = _time_shift_adoption(
            arguments, shift, unconfirmed_candidates=unconfirmed_candidates
        )
        return _resolve_time_shift(_incumbent_decision(**adopted), adopted)
    selected = base.selected_shadow_regime
    if (
        base.state not in {RegimeState.CHANGE_SUSPECTED, RegimeState.CHALLENGER_ACCUMULATING}
        or selected is None
        or selected.regime_id != incumbent.regime_id
    ):
        return base
    suspected = len(shift.event_ids) >= TIME_SHIFT_SUSPECT_OBSERVATIONS
    reasons = set(base.reason_codes)
    if suspected:
        reasons.discard(ISOLATED_OFF_PHASE_RESTART_REASON)
        reasons.add(TIME_SHIFT_SUSPECTED_REASON)
    else:
        reasons.add(TIME_SHIFT_NOTICED_REASON)
    selected_candidate = arguments["incumbent_candidate"]
    candidates = base.candidates
    regimes = base.regimes
    incumbent_record = base.incumbent_shadow_regime
    if (
        suspected
        and _time_shift_sparse_regime(incumbent)
        and selected_candidate is not None
        and _time_shift_authority_phase(selected_candidate.reason_codes) is None
    ):
        # A sparse regime's statistic at its own phase counts the shifted
        # restarts as off-phase.  While the shift is suspected the earned
        # pre-shift value is shown instead: the same statistic over the
        # restarts before the first shifted observation.  Nothing persists.
        preserved = _time_shift_preserved_candidate(
            selected_candidate,
            before_at=_time_shift_boundary(shift.first_observed_at),
            sparse=arguments["sparse"],
            relationship_map=arguments["relationship_map"],
        )
        if preserved.combined_confidence > selected_candidate.combined_confidence:
            candidates = tuple(
                preserved if item.candidate_id == selected_candidate.candidate_id else item
                for item in candidates
            )
            selected_candidate = preserved

            def preserve(regime: RegimeRecord | None) -> RegimeRecord | None:
                if regime is None or regime.regime_id != incumbent.regime_id:
                    return regime
                return replace(regime, combined_confidence=preserved.combined_confidence)

            selected = preserve(selected)
            incumbent_record = preserve(incumbent_record)
            regimes = tuple(preserve(item) for item in regimes)
    return _decision(
        server_key=base.server_key,
        state=base.state,
        selected=selected,
        incumbent=incumbent_record,
        regimes=regimes,
        candidates=candidates,
        contexts=base.challenger_contexts,
        strongest=base.strongest_challenger,
        normal_ledger=base.normal_ledger,
        high_ledger=base.high_ledger,
        cycle_visible=base.cycle_visible,
        prediction_usable=base.prediction_usable and not suspected,
        countdown_safe=base.countdown_safe and not suspected,
        suspicion_level=base.suspicion_level,
        suspicion_ids=base.active_suspicion_evidence_ids,
        selected_candidate=selected_candidate,
        reasons=tuple(reasons),
    )


def _time_shift_adoption(
    arguments: dict,
    shift: _TimeShift,
    *,
    unconfirmed_candidates: tuple[AuthorityCandidate, ...] | None = None,
) -> dict:
    """``_incumbent_decision`` arguments with the incumbent at the new phase.

    The regime keeps its period, origin and earned authority.  Its phase
    becomes the confirmed phase and its establishment boundary the confirming
    restart, so evidence at the previous phase is historical: it cannot
    authorize a transition back, which then needs new evidence.

    Regime identity is period and phase, and the candidate at the previous
    phase may still carry the previous identity, so the re-anchored regime
    gets its own (derived from the previous one and the confirmed shift) and
    replaces the previous record instead of superseding it: nothing about
    the period was disproved, so no supersession boundary is recorded.
    """

    incumbent = arguments["incumbent"]
    prior = arguments["prior"]
    period = incumbent.candidate_period_seconds
    phase = round(shift.phase_offset, 6) % period
    authority_phase = _time_shift_authority_phase(incumbent.reason_codes)
    authority_until = _time_shift_authority_until(incumbent.reason_codes)
    if authority_phase is None:
        authority_phase = incumbent.phase_offset
        authority_until = None
    if authority_until is None and _time_shift_sparse_regime(incumbent):
        authority_until = _time_shift_boundary(shift.first_observed_at)
    shifted = replace(
        incumbent,
        regime_id=_stable_id(
            "authority-time-shift-regime",
            AUTHORITY_SEMANTIC_VERSION,
            incumbent.regime_id,
            period,
            phase,
            round(shift.confirmed_at, 3),
        ),
        phase_offset=phase,
        established_at=shift.confirmed_at,
        reason_codes=tuple(
            sorted(
                {
                    *(
                        item
                        for item in incumbent.reason_codes
                        if item not in _time_shift_markers(incumbent.reason_codes)
                    ),
                    TIME_SHIFT_ADOPTED_REASON,
                }
            )
        ),
    )
    candidates, candidate = _time_shift_incumbent_candidate(
        shifted,
        tuple(
            item
            for item in (
                arguments["candidates"]
                if unconfirmed_candidates is None
                else unconfirmed_candidates
            )
            if _time_shift_authority_phase(item.reason_codes) is None
        ),
        authority_phase,
        authority_until=authority_until,
        sparse=arguments["sparse"],
        relationship_map=arguments["relationship_map"],
    )
    if candidate is not None:
        shifted = _regime_from_candidate(
            candidate, status=RegimeRecordStatus.ESTABLISHED, existing=shifted
        )
    if prior is not None:
        prior = replace(
            prior,
            regimes=tuple(
                item for item in prior.regimes if item.regime_id != incumbent.regime_id
            ),
        )
    # A sparse confirmation of the previous phase's candidate does not carry
    # over to the re-anchored regime (the next rebuild judges confirmation at
    # the regime's own phase, and never for a carrier).
    upgrade_reasons = tuple(
        item for item in arguments["upgrade_reasons"] if item != SPARSE_CONFIRMATION_REASON
    )
    return {
        **arguments,
        "prior": prior,
        "candidates": candidates,
        "ranked": tuple(sorted(candidates, key=_candidate_rank, reverse=True)),
        "incumbent": shifted,
        "incumbent_candidate": candidate,
        "upgrade_reasons": tuple(dict.fromkeys((*upgrade_reasons, TIME_SHIFT_ADOPTED_REASON))),
    }


# --------------------------------------------------------------------------
# Historical acquisition recovery
# --------------------------------------------------------------------------


def _historical_regime(candidate: AuthorityCandidate) -> RegimeRecord:
    """A sparse-established regime at ``candidate`` (period, phase, tolerance)."""

    regime = _regime_from_candidate(
        candidate, status=RegimeRecordStatus.ESTABLISHED, origin=AuthorityOrigin.NORMAL
    )
    return replace(
        regime,
        reason_codes=tuple(
            dict.fromkeys(
                (*regime.reason_codes, SPARSE_ESTABLISHMENT_REASON, HISTORICAL_ACQUISITION_REASON)
            )
        ),
    )


def _historical_shift_group_refutes_longer_periods(
    regime: RegimeRecord,
    shift: _TimeShift,
    events: tuple[PhysicalRestartEvent, ...],
    *,
    last_aligned_at: float,
) -> bool:
    """A2: the shifted restarts cannot be a longer multiple of the period.

    SE4's rule applied to the shifted schedule's restarts: the authentic
    restarts after the last aligned one that fall on the confirmed phase (one
    per scheduled occurrence; the three confirming restarts and any later
    ones) must refute every longer candidate period that is a multiple of
    the regime's, and must still do so with any single restart removed, so
    one coincidental restart can never decide it.
    """

    period = regime.candidate_period_seconds
    multiples = tuple(
        item for item in CANDIDATE_PERIODS if item > period and item % period == 0
    )
    if not multiples:
        return True
    members: list[float] = []
    for at in sorted(
        _event_at(item)
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _event_at(item) > last_aligned_at
        and _circular_distance(_event_at(item), shift.phase_offset, period)
        <= regime.phase_tolerance
    ):
        if all(abs(at - other) > period / 2 for other in members):
            members.append(at)

    def refutes(times: list[float]) -> bool:
        return bool(times) and all(
            any(
                _circular_distance(value, times[0], multiple)
                > candidate_phase_tolerance(multiple)
                for value in times
            )
            for multiple in multiples
        )

    return refutes(members) and all(
        refutes(members[:index] + members[index + 1 :]) for index in range(len(members))
    )


def _historical_prefix_candidate(
    candidate: AuthorityCandidate,
    *,
    before_at: float,
    normal_score: ScheduleScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    relationship_map: dict[str, IntervalRelationship],
    sparse: _SparseEvidence,
) -> AuthorityCandidate:
    """``candidate`` with its sparse slot and structure as of ``before_at``.

    The existing sparse statistic, structure rules (SE1-SE5) and combined
    formula, over the restarts strictly before the boundary, with the
    candidate's own endpoint exclusions.  Gates, high authority and identity
    are unchanged.
    """

    period = candidate.candidate_period_seconds
    support = _sparse_support(
        period=period,
        phase_offset=candidate.phase_offset,
        sparse=sparse,
        excluded_event_ids=_relationship_endpoint_ids(
            relationship_map[item]
            for item in candidate.high_relationship_ids
            if item in relationship_map
        ),
        candidate=candidate,
        before_at=before_at,
    )
    try:
        normal_candidate = normal_score.candidate(period)
    except KeyError:
        structure: tuple[str, ...] = ()
    else:
        structure = _sparse_structure_reasons(
            period=period,
            support=support,
            normal_candidate=normal_candidate,
            windows=windows,
        )
    proportional = bool(
        support.off_phase_weight > 0 and support.consistency >= SPARSE_CONSISTENCY_TOLERANCE
    )
    sparse_reasons = {
        SPARSE_STRUCTURE_REASON,
        SPARSE_PERIOD_AMBIGUOUS_REASON,
        SPARSE_PROPORTIONAL_ANOMALY_REASON,
    }
    combined = candidate.high_authority + (1.0 - candidate.high_authority) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * support.value
    )
    return replace(
        candidate,
        aligned_normal_confidence=round(support.value, 6),
        combined_confidence=round(combined, 6),
        reason_codes=tuple(
            sorted(
                {
                    *(item for item in candidate.reason_codes if item not in sparse_reasons),
                    *structure,
                    *((SPARSE_PROPORTIONAL_ANOMALY_REASON,) if proportional else ()),
                }
            )
        ),
    )


def _historical_shift_proposal(
    candidate: AuthorityCandidate,
    *,
    normal_score: ScheduleScore,
    candidates: tuple[AuthorityCandidate, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    relationship_map: dict[str, IntervalRelationship],
    sparse: _SparseEvidence,
    compact: "_Compact | None",
) -> tuple[AuthorityCandidate, float] | None:
    """The pre-shift sparse establishment of ``candidate``, if the evidence has one.

    The boundary is not searched for: it is the first observation of the
    time shift that the existing assessment confirms against a regime at the
    candidate's phase (after its latest aligned restart).  The unchanged
    sparse gate (SE1-SE7 and the competitor rule) must select exactly this
    candidate from the evidence before that boundary.
    """

    if candidate.harmonic_blockers:
        return None
    hypothetical = _historical_regime(candidate)
    last_aligned_at, suspicion_events, _state = _incumbent_alignment(
        hypothetical, events, windows, compact
    )
    shift = _time_shift_assessment(
        hypothetical, suspicion_events, windows, last_aligned_at=last_aligned_at
    )
    if shift is None or shift.confirmed_at is None:
        return None
    if not _historical_shift_group_refutes_longer_periods(
        hypothetical, shift, suspicion_events, last_aligned_at=last_aligned_at
    ):
        return None
    boundary = _time_shift_boundary(shift.first_observed_at)
    view = tuple(
        _historical_prefix_candidate(
            item,
            before_at=boundary,
            normal_score=normal_score,
            windows=windows,
            relationship_map=relationship_map,
            sparse=sparse,
        )
        for item in candidates
    )
    choice = _sparse_establishing_candidate(
        candidates=view,
        windows=windows,
        relationship_map=relationship_map,
        sparse=sparse,
        before_at=boundary,
    )
    if choice is None or choice[0].candidate_id != candidate.candidate_id:
        return None
    return choice


def _historical_shift_acquisition(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    candidates: tuple[AuthorityCandidate, ...],
    ranked: tuple[AuthorityCandidate, ...],
    normal_ledger: NormalEvidenceLedger,
    high_ledger: HighEvidenceLedger,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    relationships: tuple[IntervalRelationship, ...],
    relationship_map: dict[str, IntervalRelationship],
    streaks: tuple[CadenceStreak, ...],
    prior: AuthorityDecision | None,
    sparse: _SparseEvidence | None,
    compact: "_Compact | None",
) -> AuthorityDecision | None:
    """Historical acquisition recovery (operator decisions A1-A3).

    Establishes what the live runtime would have established on the retained
    evidence before a confirmed time shift, then evaluates every later
    restart with the unchanged established-regime rules (suspicion,
    challengers, cycle-change precedence and Time Shift Detection with its
    sparse confidence carrier).  The result is used only when exactly one
    candidate qualifies and the outcome is an established, adopted and
    prediction-safe regime; otherwise acquisition continues unchanged.
    Confidence is the existing combined formula throughout.
    """

    if sparse is None:
        return None
    proposals: dict[str, tuple[AuthorityCandidate, float]] = {}
    for candidate in ranked:
        proposal = _historical_shift_proposal(
            candidate,
            normal_score=normal_score,
            candidates=candidates,
            windows=windows,
            events=events,
            relationship_map=relationship_map,
            sparse=sparse,
            compact=compact,
        )
        if proposal is not None:
            proposals[proposal[0].candidate_id] = proposal
    if len(proposals) != 1:
        return None
    (prefix_candidate, established_at), = proposals.values()
    regime = _historical_regime(prefix_candidate)
    if regime.established_at is None:
        regime = replace(regime, established_at=established_at)
    superseded = tuple(
        item
        for item in (prior.regimes if prior is not None else ())
        if item.status is RegimeRecordStatus.SUPERSEDED
    )
    historical_prior = _decision(
        server_key=server_key,
        state=RegimeState.ESTABLISHED,
        selected=regime,
        incumbent=regime,
        regimes=(regime, *superseded),
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
        selected_candidate=prefix_candidate,
        reasons=(SPARSE_ESTABLISHMENT_REASON, HISTORICAL_ACQUISITION_REASON),
    )
    decision = _transition_decision(
        server_key=server_key,
        normal_score=normal_score,
        candidates=candidates,
        normal_ledger=normal_ledger,
        high_ledger=high_ledger,
        windows=windows,
        events=events,
        prior=historical_prior,
        relationships=relationships,
        streaks=streaks,
        sparse=sparse,
        compact=compact,
    )
    incumbent = decision.incumbent_shadow_regime
    if not (
        decision.state is RegimeState.ESTABLISHED
        and incumbent is not None
        and decision.selected_shadow_regime is not None
        and decision.selected_shadow_regime.regime_id == incumbent.regime_id
        and TIME_SHIFT_ADOPTED_REASON in incumbent.reason_codes
        and HISTORICAL_ACQUISITION_REASON in incumbent.reason_codes
        and decision.cycle_visible
        and decision.prediction_usable
        and decision.countdown_safe
        and (
            decision.suspicion_level == 0
            or (
                decision.suspicion_level == 1
                and ISOLATED_OFF_PHASE_RESTART_REASON in decision.reason_codes
            )
        )
    ):
        return None
    return decision


def _isolated_off_phase_restart(
    active_off_phase: tuple[str, ...],
    *,
    incumbent: RegimeRecord,
    events: tuple[PhysicalRestartEvent, ...],
) -> bool:
    """Stage 8.7a: the level-one contradiction is one random admin restart.

    Exactly one authentic off-phase restart since the incumbent's latest
    aligned evidence, phase-coherent (at any candidate period) with no other
    authentic restart that is off the incumbent's phase anywhere in the
    horizon.  Coherent off-phase restarts are possible alternative-schedule
    evidence and keep the existing level-one consumer treatment.
    """

    if len(active_off_phase) != 1:
        return False
    off_phase = tuple(
        item
        for item in events
        if _authentic_event(item)
        and _event_at(item) is not None
        and _circular_distance(
            _event_at(item), incumbent.phase_offset, incumbent.candidate_period_seconds
        )
        > incumbent.phase_tolerance
    )
    anomaly = next(
        (item for item in off_phase if item.event_id == active_off_phase[0]), None
    )
    if anomaly is None:
        return False
    return not any(
        item.event_id != anomaly.event_id and _phase_coherent(item, anomaly)
        for item in off_phase
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
    normal_context_strength: float,
    active_miss_ids: tuple[str, ...],
    events: tuple[PhysicalRestartEvent, ...],
    folded_high_relationship_count: int = 0,
    folded_normal_relationship_count: int = 0,
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
    if HISTORICAL_CHALLENGER_EVIDENCE_REASON in candidate.reason_codes:
        reasons.add(HISTORICAL_CHALLENGER_EVIDENCE_REASON)
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
        normal_context_strength=normal_context_strength,
        high_authority=candidate.high_authority,
        high_confirmation_present=candidate.h2_gate or candidate.h3_gate,
        authorization_state=authorization,
        regime_change_status=status,
        first_evidence_at=candidate.first_evidence_at,
        last_evidence_at=candidate.last_evidence_at,
        reason_codes=tuple(sorted(reasons)),
        folded_high_relationship_count=folded_high_relationship_count,
        folded_normal_relationship_count=folded_normal_relationship_count,
    )


def _schema3_context_strength(
    normal_score: ScheduleScore, candidate: AuthorityCandidate
) -> float:
    """Pattern-level normal context of a challenger (unchanged by Stage 8.3).

    Challenger classification keeps its schema-3 basis (recent fundamental
    and phase confidence at a compatible phase), so long-horizon sparse
    evidence never escalates or relabels challenger contexts (D8-14).
    """

    period = candidate.candidate_period_seconds
    normal = normal_score.candidate(period)
    if (
        normal.phase_offset is None
        or _circular_distance(normal.phase_offset, candidate.phase_offset, period)
        > candidate.phase_tolerance
    ):
        return 0.0
    return round(
        min(normal.fundamental_period_confidence, normal.phase_confidence), 6
    )


def _post_establishment_view(
    candidate: AuthorityCandidate,
    *,
    established_at: float | None,
    relationship_map: dict[str, IntervalRelationship],
    streaks: tuple[CadenceStreak, ...],
    relationships: tuple[IntervalRelationship, ...],
    normal_score: ScheduleScore,
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    event_map: dict[str, PhysicalRestartEvent],
    sparse: _SparseEvidence,
) -> AuthorityCandidate:
    """Return the challenger as seen against an established incumbent.

    High evidence that starts before the incumbent's establishment is
    historical: it is retained (and still contributes to the regime's own
    confidence if it is ever re-established), but it cannot authorize a
    transition away from a newer established regime.  The unchanged H1-H4
    gates are recomputed on the post-establishment subset only.  Identity
    (regime, period, phase) stays that of the full candidate.
    """

    if established_at is None or not candidate.high_relationship_ids:
        return candidate
    retained = tuple(
        relationship_map[identity]
        for identity in candidate.high_relationship_ids
        if identity in relationship_map
        and relationship_map[identity].left_at >= established_at
    )
    if len(retained) == len(candidate.high_relationship_ids):
        return candidate
    reasons = tuple(
        sorted({*candidate.reason_codes, HISTORICAL_CHALLENGER_EVIDENCE_REASON})
    )
    if not retained:
        return replace(
            candidate,
            high_relationship_ids=(),
            maximal_streak_ids=(),
            high_authority=0.0,
            high_phase_authority=0.0,
            h1_gate=False,
            h2_gate=False,
            h3_gate=False,
            h4_gate=False,
            reason_codes=reasons,
        )
    retained_ids = {item.relationship_id for item in retained}
    period = candidate.candidate_period_seconds
    view, _normal = _authority_candidate(
        server_key=candidate.server_key,
        period=period,
        component=tuple(
            sorted(
                retained,
                key=lambda item: (item.left_at, item.right_at, item.relationship_id),
            )
        ),
        streaks=tuple(
            item
            for item in streaks
            if item.candidate_period_seconds == period
            and set(item.relationship_ids).issubset(retained_ids)
        ),
        all_relationships=relationships,
        normal_candidate=normal_score.candidate(period),
        windows=windows,
        events=events,
        event_map=event_map,
        sparse=sparse,
    )
    return replace(
        candidate,
        high_relationship_ids=view.high_relationship_ids,
        maximal_streak_ids=view.maximal_streak_ids,
        high_authority=view.high_authority,
        high_phase_authority=view.high_phase_authority,
        h1_gate=view.h1_gate,
        h2_gate=view.h2_gate,
        h3_gate=view.h3_gate,
        h4_gate=view.h4_gate,
        first_evidence_at=view.first_evidence_at,
        last_evidence_at=view.last_evidence_at,
        reason_codes=reasons,
    )


def _original_cycle_recovery_confirmations(
    *,
    incumbent: RegimeRecord,
    context: ChallengerContext,
    relationships: tuple[IntervalRelationship, ...],
    event_map: dict[str, PhysicalRestartEvent],
) -> tuple[str, ...]:
    """Independent incumbent intervals observed after the challenger evidence.

    Each confirmation is an existing high-eligible DIRECT relationship
    (complete same-chain continuity, authentic endpoints, no intervening
    restart) at the incumbent's period whose phase and both endpoints lie
    within the incumbent's tolerance, and which starts strictly after the
    challenger's latest high evidence (a restart shared with the challenger
    cannot open a confirmation).  Overlapping or duplicated intervals
    count once; adjacent intervals sharing one restart are independent.
    """

    if context.authorization_state is ChallengerAuthorization.ESTABLISHED:
        return ()
    period = incumbent.candidate_period_seconds
    tolerance = incumbent.phase_tolerance
    eligible = []
    for item in relationships:
        if (
            not item.high_authority_eligible
            or item.relationship_kind is not RelationshipKind.DIRECT
            or item.intervening_event_ids
            or item.candidate_period_seconds != period
            or item.left_at <= context.last_evidence_at
            or _circular_distance(
                _relationship_phase_value(item, period),
                incumbent.phase_offset,
                period,
            )
            > tolerance
        ):
            continue
        left = event_map.get(item.left_event_id)
        right = event_map.get(item.right_event_id)
        if left is None or right is None:
            continue
        if not all(
            _authentic_event(value)
            and _event_at(value) is not None
            and _circular_distance(_event_at(value), incumbent.phase_offset, period)
            <= tolerance
            for value in (left, right)
        ):
            continue
        if relationship_authority_quality(item, (left, right)) <= 0.0:
            continue
        eligible.append(item)
    selected: list[str] = []
    pairs: set[tuple[str, str]] = set()
    latest_end = -math.inf
    for item in sorted(
        eligible, key=lambda value: (value.right_at, value.left_at, value.relationship_id)
    ):
        pair = (item.left_event_id, item.right_event_id)
        if pair in pairs or item.left_at < latest_end:
            continue
        selected.append(item.relationship_id)
        pairs.add(pair)
        latest_end = item.right_at
    return tuple(selected)


def _credible_suspension_context(context: ChallengerContext) -> bool:
    return bool(
        context.authorization_state is not ChallengerAuthorization.ESTABLISHED
        and (
            context.high_confirmation_present
            or context_high_relationship_count(context) >= 2
        )
    )


def _context_activity(
    context: ChallengerContext,
    *,
    incumbent: RegimeRecord,
    last_aligned_at: float,
    confirmations: int,
    prior: AuthorityDecision | None,
) -> bool:
    recent = context.last_evidence_at > last_aligned_at
    post_establishment = context.last_evidence_at > (incumbent.established_at or 0.0)
    recovered = confirmations >= ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS
    if context.authorization_state is ChallengerAuthorization.ESTABLISHED:
        # An H3 challenger is promoted immediately; there is no rollback
        # privilege to apply.
        return bool(post_establishment or recent)
    if context.high_confirmation_present:
        return bool((post_establishment or recent) and not recovered)
    if context_high_relationship_count(context) >= 2:
        # A level-2 context that has actually suspended the countdown stays
        # credible until the original cycle is reconfirmed twice; a single
        # aligned restart no longer clears it.  Contexts that never suspended
        # the countdown keep the original latest-aligned rule.
        return bool(
            recent
            or (
                _prior_suspension_latched(prior, incumbent, context)
                and not recovered
            )
        )
    return recent


def _prior_suspension_latched(
    prior: AuthorityDecision | None,
    incumbent: RegimeRecord,
    context: ChallengerContext,
) -> bool:
    if (
        prior is None
        or prior.incumbent_shadow_regime is None
        or prior.incumbent_shadow_regime.regime_id != incumbent.regime_id
    ):
        return False
    period = context.candidate_period_seconds
    return any(
        COUNTDOWN_SUSPENSION_LATCH_REASON in item.reason_codes
        and item.incumbent_regime_id == incumbent.regime_id
        and item.candidate_period_seconds == period
        and _circular_distance(item.phase_offset, context.phase_offset, period)
        <= context.phase_tolerance
        for item in prior.challenger_contexts
    )


def _annotate_context_recovery(
    context: ChallengerContext,
    *,
    incumbent: RegimeRecord,
    active: bool,
    confirmations: int,
) -> ChallengerContext:
    if not _credible_suspension_context(context):
        return context
    reasons = set(context.reason_codes)
    if context.high_confirmation_present:
        if context.last_evidence_at <= (incumbent.established_at or 0.0):
            return context
        reasons.add(_recovery_progress_reason(confirmations))
    elif active:
        reasons.add(COUNTDOWN_SUSPENSION_LATCH_REASON)
        reasons.add(_recovery_progress_reason(confirmations))
    else:
        return context
    return replace(context, reason_codes=tuple(sorted(reasons)))


def _recovery_progress_reason(confirmations: int) -> str:
    count = min(int(confirmations), ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS)
    return (
        f"original_cycle_recovery_confirmations_{count}_of_"
        f"{ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS}"
    )


def _stable_transition_state(
    prior: AuthorityDecision | None,
    incumbent: RegimeRecord,
    challenger: ChallengerContext,
) -> RegimeState:
    """Deterministic H2 transition state.

    Entry is TRANSITION_CONFIRMED.  The same challenger moves to
    NEW_REGIME_PROVISIONAL only when it gains newer high evidence; with
    identical evidence the prior transition state is kept, so rebuilds no
    longer alternate (and no longer mint a new decision identity).
    """

    previous = prior.strongest_challenger if prior is not None else None
    period = challenger.candidate_period_seconds
    same_transition = bool(
        prior is not None
        and prior.state
        in {RegimeState.TRANSITION_CONFIRMED, RegimeState.NEW_REGIME_PROVISIONAL}
        and prior.incumbent_shadow_regime is not None
        and prior.incumbent_shadow_regime.regime_id == incumbent.regime_id
        and previous is not None
        and previous.candidate_period_seconds == period
        and _circular_distance(previous.phase_offset, challenger.phase_offset, period)
        <= challenger.phase_tolerance
    )
    if not same_transition:
        return RegimeState.TRANSITION_CONFIRMED
    assert prior is not None and previous is not None
    if challenger.last_evidence_at > previous.last_evidence_at:
        return RegimeState.NEW_REGIME_PROVISIONAL
    return prior.state


def _regime_from_candidate(
    candidate: AuthorityCandidate,
    *,
    status: RegimeRecordStatus,
    origin: AuthorityOrigin = AuthorityOrigin.CONTINUITY,
    existing: RegimeRecord | None = None,
    upgrade_origin: bool = False,
) -> RegimeRecord:
    established = candidate.last_evidence_at if candidate.h3_gate else None
    regime_id = candidate.regime_id
    defining = candidate.defining_evidence_ids
    reasons = [
        "period_and_phase_regime_identity",
        "high_authority_does_not_decay_with_inactivity",
        "regime_remains_reversible",
    ]
    if existing is not None:
        regime_id = existing.regime_id
        established = existing.established_at
        defining = existing.defining_evidence_ids
        origin = existing.origin
        if upgrade_origin and origin is AuthorityOrigin.NORMAL:
            # Same regime identity (and so the same alert keys); the matching
            # candidate's high gates now govern it.  ``established_at`` is
            # kept: the regime was already established, so evidence between
            # its normal establishment and this upgrade is not historical.
            origin = AuthorityOrigin.CONTINUITY
            reasons.append(ORIGIN_UPGRADE_REASON)
        elif ORIGIN_UPGRADE_REASON in existing.reason_codes:
            reasons.append(ORIGIN_UPGRADE_REASON)
        if SPARSE_ESTABLISHMENT_REASON in existing.reason_codes:
            reasons.append(SPARSE_ESTABLISHMENT_REASON)
        if HISTORICAL_ACQUISITION_REASON in existing.reason_codes:
            reasons.append(HISTORICAL_ACQUISITION_REASON)
        if TIME_SHIFT_ADOPTED_REASON in existing.reason_codes:
            reasons.append(TIME_SHIFT_ADOPTED_REASON)
            # The authority-phase marker lives on the carrying candidate, so
            # it lapses with the carrier once the regime supports itself.
            reasons.extend(_time_shift_markers(candidate.reason_codes))
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
        reason_codes=tuple(reasons),
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


def _endpoint_eligible_relationships(
    relationships: tuple[IntervalRelationship, ...],
    events: tuple[PhysicalRestartEvent, ...],
) -> tuple[IntervalRelationship, ...]:
    """Stage 8.7: ignore relationships whose endpoint is a non-authentic outcome.

    Extraction no longer creates them, but schema-4 relationships are
    immutable by ID and unioned on rebuild, so legacy persisted records
    (created before Stages 8.6/8.7) may still name an unconfirmed drain,
    uncertain A2S interruption, service interruption, incomplete or expired
    episode as an endpoint.  They stay persisted as diagnostics; authority
    simply never reads them as normal interval or challenger evidence.  Such
    relationships can never be high-eligible (endpoint class), so high
    evidence and streaks are unaffected.  The intervening-episode blocking
    of other relationships is carried on those relationships and unchanged.
    """

    event_by_id = {item.event_id: item for item in events}
    return tuple(
        item
        for item in relationships
        if all(
            event_by_id.get(value) is None
            or relationship_endpoint_outcome_eligible(event_by_id[value])
            for value in (item.left_event_id, item.right_event_id)
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


# --------------------------------------------------------------------------
# Compact-authority evidence access (Retention R3)
# --------------------------------------------------------------------------


def _compact_unfolded_events(compact: _Compact) -> tuple[PhysicalRestartEvent, ...]:
    """Ring events not yet folded (the open chain's anchor is folded)."""

    mark = compact.state.watermark
    if mark.at is None:
        return compact.ring_events
    key = (mark.at, mark.event_id or "")
    return tuple(
        item
        for item in compact.ring_events
        if _event_at(item) is not None and (_event_at(item), item.event_id) > key
    )


def _compact_sparse_support(
    *,
    period: int,
    phase_offset: float,
    compact: _Compact,
    excluded_event_ids: frozenset[str],
    candidate: "AuthorityCandidate | None",
    before_at: float | None = None,
) -> _SparseSupport:
    from .companion_restart_phase2_learned_state import compact_sparse_support

    info = compact.candidates.get(candidate.candidate_id) if candidate is not None else None
    open_chain = compact.state.open_chain
    anchor = open_chain.anchor_event_id if open_chain is not None else None
    excluded = frozenset(excluded_event_ids)
    value = compact_sparse_support(
        compact.state,
        period=period,
        phase_offset=phase_offset,
        ring_events=compact.ring_events,
        excluded_ring_event_ids=excluded,
        exclude_folded_high_endpoints=bool(info is not None and info.has_folded_evidence),
        exclude_anchor=anchor is not None and anchor in excluded,
        before_at=before_at,
    )
    return _SparseSupport(
        value=value.value,
        aligned=(),
        effective=value.effective,
        quality_band=value.quality_band,
        phase_confidence=value.phase_confidence,
        off_phase_weight=value.off_phase_weight,
        consistency=value.consistency,
        bounds=(
            (value.first_aligned_at, value.last_aligned_at) if value.aligned_count else None
        ),
        compact_support=value,
        compact=compact,
    )


def _compact_miss_since(
    compact: "_Compact | None",
    *,
    period: int,
    phase_offset: float,
    tolerance: float,
    since: float,
) -> bool:
    """A folded genuine miss possibly in phase at or after ``since``."""

    if compact is None:
        return False
    from .companion_restart_phase2_learned_state import compact_genuine_miss_since

    return compact_genuine_miss_since(
        compact.state,
        period=period,
        phase_offset=phase_offset,
        phase_tolerance=tolerance,
        since=since,
    )


def _compact_ledgers(
    *,
    server_key: str,
    normal_score: ScheduleScore,
    relationships: tuple[IntervalRelationship, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    sparse: _SparseEvidence,
    compact: _Compact,
) -> tuple[
    tuple[AuthorityCandidate, ...], NormalEvidenceLedger, HighEvidenceLedger
]:
    """``_build_ledgers`` over folded knowledge plus the ring."""

    from .companion_restart_phase2_learned_state import compact_candidates, compact_phase_proposal

    state = compact.state
    candidate_values: list[AuthorityCandidate] = []
    normal_values: list[NormalCandidateEvidence] = []
    for info in compact_candidates(
        state,
        ring_events=events,
        ring_relationships=relationships,
        normal_score=normal_score,
        ring_windows=windows,
    ):
        candidate, normal = _authority_candidate_from_compact(
            server_key=server_key,
            info=info,
            normal_score=normal_score,
            relationships=relationships,
            windows=windows,
            events=events,
            sparse=sparse,
            compact=compact,
        )
        compact.candidates[candidate.candidate_id] = info
        candidate_values.append(candidate)
        normal_values.append(normal)
    for period in CANDIDATE_PERIODS:
        normal_candidate = normal_score.candidate(period)
        has_relationships = bool(
            any(item.candidate_period_seconds == period for item in relationships)
            or any(item.period == period for item in state.normal_relationships)
            or any(item.period == period for item in state.intervals)
        )
        phases = []
        if normal_candidate.phase_offset is not None and bool(
            normal_candidate.fundamental_period_confidence > 0
            or normal_candidate.phase_confidence > 0
            or has_relationships
        ):
            phases.append(float(normal_candidate.phase_offset) % period)
        long_horizon_phase, _mad, aligned_count, _recent = compact_phase_proposal(
            state, period=period, ring_events=events
        )
        if long_horizon_phase is not None and aligned_count > 0:
            phases.append(float(long_horizon_phase) % period)
        for phase in phases:
            if any(
                _circular_distance(item.phase_offset, phase, period) <= item.phase_tolerance
                for item in candidate_values
                if item.candidate_period_seconds == period
            ):
                continue
            candidate, normal = _normal_only_candidate(
                server_key=server_key,
                normal_candidate=normal_candidate,
                phase=phase,
                relationships=relationships,
                windows=windows,
                events=events,
                sparse=sparse,
                compact=compact,
            )
            candidate_values.append(candidate)
            normal_values.append(normal)
    high = tuple(
        item
        for item in relationships
        if item.high_authority_eligible and item.relationship_kind is RelationshipKind.DIRECT
    )
    return _assemble_ledgers(
        server_key=server_key,
        candidate_values=candidate_values,
        normal_values=normal_values,
        windows=windows,
        high=high,
        streaks=(),
    )


def _authority_candidate_from_compact(
    *,
    server_key: str,
    info,
    normal_score: ScheduleScore,
    relationships: tuple[IntervalRelationship, ...],
    windows: tuple[ExpectedWindowLedgerRecord, ...],
    events: tuple[PhysicalRestartEvent, ...],
    sparse: _SparseEvidence,
    compact: _Compact,
) -> tuple[AuthorityCandidate, NormalCandidateEvidence]:
    """An ``AuthorityCandidate`` for one compact high candidate.

    Numbers come from the compact evaluator (exact running products, streak
    bonuses and gates).  References are ring-only; identity is compact:
    period, normalised phase cluster and the earliest evidence time, which
    folding never changes.
    """

    from .companion_restart_phase2_learned_state import compact_sparse_support

    period = info.period
    tolerance = candidate_phase_tolerance(period)
    phase = info.phase_offset
    support = compact_sparse_support(
        compact.state,
        period=period,
        phase_offset=phase,
        ring_events=events,
        excluded_ring_event_ids=info.ring_endpoint_ids,
        exclude_folded_high_endpoints=info.has_folded_evidence,
        exclude_anchor=info.excludes_anchor,
    )
    normal = _normal_candidate_evidence(
        period=period,
        phase_offset=phase,
        tolerance=tolerance,
        normal_candidate=normal_score.candidate(period),
        relationships=relationships,
        windows=windows,
        events=events,
        sparse=sparse,
        excluded_event_ids=info.ring_endpoint_ids,
        compact_support=(
            info.aligned_normal_confidence,
            info.sparse_reason_codes,
            bool(
                support.off_phase_weight > 0
                and support.consistency >= SPARSE_CONSISTENCY_TOLERANCE
            ),
        ),
    )
    regime_id = _stable_id(
        "authority-compact-regime",
        AUTHORITY_SEMANTIC_VERSION,
        server_key,
        period,
        _normalized_phase_cluster(phase, tolerance, period),
        round(info.first_evidence_at or 0.0, 3),
    )
    gates = (info.h1_gate, info.h2_gate, info.h3_gate, info.h4_gate)
    candidate_id = _stable_id(
        "authority-compact-candidate",
        AUTHORITY_SEMANTIC_VERSION,
        regime_id,
        round(phase, 6),
        info.relationship_count,
        info.base_authority,
        info.streak_bonus,
        info.aligned_normal_confidence,
        info.harmonic_blockers,
        gates,
    )
    reasons = {
        "bounded_high_authority_formula_v1",
        "normal_reinforcement_remaining_uncertainty_only",
        "phase_authority_separate",
        "maximal_streak_bonus_only",
        COMPACT_EVIDENCE_REASON,
        *_sparse_reason_codes(normal),
        *info.harmonic_blockers,
    }
    for name, passed in zip(("h1", "h2", "h3", "h4"), gates):
        if passed:
            reasons.add(f"{name}_gate_passed")
    candidate = AuthorityCandidate(
        schema_version=AUTHORITY_SCHEMA_VERSION,
        semantic_version=AUTHORITY_SEMANTIC_VERSION,
        candidate_id=candidate_id,
        regime_id=regime_id,
        server_key=server_key,
        candidate_period_seconds=period,
        phase_offset=round(phase, 6),
        phase_spread=round(info.phase_spread, 6),
        phase_tolerance=round(tolerance, 6),
        high_relationship_ids=info.ring_relationship_ids,
        maximal_streak_ids=(),
        relationship_quality_values=info.ring_relationship_qualities,
        base_authority=info.base_authority,
        streak_bonus=info.streak_bonus,
        streak_bonus_values=(),
        high_authority=info.high_authority,
        phase_quality=info.phase_quality,
        high_phase_authority=info.high_phase_authority,
        aligned_normal_confidence=info.aligned_normal_confidence,
        combined_confidence=info.combined_confidence,
        h1_gate=info.h1_gate,
        h2_gate=info.h2_gate,
        h3_gate=info.h3_gate,
        h4_gate=info.h4_gate,
        harmonic_blockers=info.harmonic_blockers,
        defining_evidence_ids=(),
        first_evidence_at=info.first_evidence_at if info.first_evidence_at is not None else 0.0,
        last_evidence_at=info.last_evidence_at if info.last_evidence_at is not None else 0.0,
        reason_codes=tuple(sorted(reasons)),
    )
    return candidate, normal


@dataclass(frozen=True)
class _CompactStreak:
    start_at: float
    end_at: float
    interval_count: int
    relationship_ids: tuple[str, ...]


def _compact_two_interval_streak(
    candidate: AuthorityCandidate,
    compact: _Compact,
    contradictions: "_AcquisitionContradictions | None",
) -> _CompactStreak | None:
    """``_two_interval_establishing_streak`` over compact streak summaries.

    Streaks are the candidate's ring runs, the open run and folded closed
    streaks (all exact interval counts and minimum qualities).  Folded
    contradictions are conservative: a folded miss or restart that may be
    in phase (or off phase) counts.
    """

    if (
        not candidate.h2_gate
        or candidate.harmonic_blockers
        or candidate.phase_quality < TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM
    ):
        return None
    info = compact.candidates.get(candidate.candidate_id)
    if info is None:
        return None
    qualifying = [
        item
        for item in info.streaks
        if item.interval_count >= 2 and item.event_count >= 3 and item.min_quality > 0.0
    ]
    best = max(qualifying, key=lambda item: (item.end_at, item.interval_count), default=None)
    if best is None:
        return None
    relationship_map = {item.relationship_id: item for item in compact.ring_relationships}
    streak = _CompactStreak(
        best.start_at,
        best.end_at,
        best.interval_count,
        tuple(
            identity
            for identity in info.ring_relationship_ids
            if identity in relationship_map
            and best.start_at <= relationship_map[identity].left_at
            and relationship_map[identity].right_at <= best.end_at
        ),
    )
    if contradictions is None:
        return streak
    period = candidate.candidate_period_seconds
    tolerance = candidate.phase_tolerance
    if any(
        item.h2_gate
        and item.candidate_id != candidate.candidate_id
        and (
            item.candidate_period_seconds != period
            or _circular_distance(item.phase_offset, candidate.phase_offset, period) > tolerance
        )
        for item in contradictions.candidates
    ):
        return None
    if any(
        item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and item.candidate_period_seconds == period
        and _circular_distance(item.expected_phase_offset, candidate.phase_offset, period)
        <= tolerance
        and item.expected_at >= streak.start_at
        for item in contradictions.windows
    ) or _compact_miss_since(
        compact,
        period=period,
        phase_offset=candidate.phase_offset,
        tolerance=tolerance,
        since=streak.start_at,
    ):
        return None
    from .companion_restart_phase2_learned_state import compact_folded_restart_after

    if any(
        _authentic_event(item)
        and _event_at(item) is not None
        and _event_at(item) > streak.end_at
        and _circular_distance(_event_at(item), candidate.phase_offset, period) > tolerance
        for item in contradictions.events
    ) or compact_folded_restart_after(
        compact.state,
        period=period,
        phase_offset=candidate.phase_offset,
        tolerance=tolerance,
        after=streak.end_at,
        aligned=False,
    ):
        return None
    return streak


def _compact_continuity_upgrade_qualifies(candidate: AuthorityCandidate, compact: _Compact) -> bool:
    if candidate.harmonic_blockers:
        return False
    if candidate.h3_gate:
        return True
    return _compact_two_interval_streak(candidate, compact, None) is not None


def _compact_sparse_confirmed_candidate(
    candidate: AuthorityCandidate, *, regime: RegimeRecord, compact: _Compact
) -> AuthorityCandidate:
    """``_sparse_confirmed_candidate`` via the compact confirmation."""

    from .companion_restart_phase2_learned_state import (
        EpochEvidence,
        compact_sparse_confirmation_details,
        knowledge_for_regime,
    )

    info = compact.candidates.get(candidate.candidate_id)
    if info is None or regime.established_at is None:
        return candidate
    state = knowledge_for_regime(compact.state, regime)
    epoch_key = (state.epoch.regime_id, state.epoch.established_at)
    if epoch_key != (regime.regime_id, regime.established_at):
        state = replace(state, epoch=EpochEvidence(regime.regime_id, regime.established_at))
    value, confirming = compact_sparse_confirmation_details(
        state, info, ring_events=compact.ring_events, ring_relationships=compact.ring_relationships
    )
    if not confirming:
        return candidate
    return replace(
        candidate,
        combined_confidence=round(max(candidate.combined_confidence, value), 6),
        reason_codes=tuple(sorted({*candidate.reason_codes, SPARSE_CONFIRMATION_REASON})),
    )


def _compact_challenger_view(compact: _Compact) -> tuple:
    if compact.challenger_view is None:
        from .companion_restart_phase2_learned_state import compact_candidates

        compact.challenger_view = compact_candidates(
            compact.state,
            ring_events=compact.ring_events,
            ring_relationships=compact.ring_relationships,
            normal_score=compact.normal_score,
            ring_windows=compact.ring_windows,
            challenger=True,
        )
    return compact.challenger_view


def _compact_post_establishment_view(
    candidate: AuthorityCandidate, *, incumbent: RegimeRecord, compact: _Compact
) -> tuple[AuthorityCandidate, int]:
    """``_post_establishment_view`` from the epoch challenger evidence.

    Returns the view and the number of its high relationships that are
    folded (counted, not referenced).
    """

    info = compact.candidates.get(candidate.candidate_id)
    full_count = (
        info.relationship_count if info is not None else len(candidate.high_relationship_ids)
    )
    folded_full = full_count - len(candidate.high_relationship_ids)
    established_at = incumbent.established_at
    if established_at is None or full_count == 0:
        return candidate, folded_full
    epoch = compact.state.epoch
    view_values = (
        _compact_challenger_view(compact)
        if (epoch.regime_id, epoch.established_at) == (incumbent.regime_id, established_at)
        else ()
    )
    period = candidate.candidate_period_seconds
    matches = [
        item
        for item in view_values
        if item.period == period
        and _circular_distance(item.phase_offset, candidate.phase_offset, period)
        <= candidate.phase_tolerance
    ]
    reasons = tuple(sorted({*candidate.reason_codes, HISTORICAL_CHALLENGER_EVIDENCE_REASON}))
    if not matches:
        return (
            replace(
                candidate,
                high_relationship_ids=(),
                maximal_streak_ids=(),
                high_authority=0.0,
                high_phase_authority=0.0,
                h1_gate=False,
                h2_gate=False,
                h3_gate=False,
                h4_gate=False,
                reason_codes=reasons,
            ),
            0,
        )
    view = max(matches, key=lambda item: (item.relationship_count, item.high_authority))
    if view.relationship_count >= full_count:
        return candidate, folded_full
    return (
        replace(
            candidate,
            high_relationship_ids=view.ring_relationship_ids,
            maximal_streak_ids=(),
            high_authority=view.high_authority,
            high_phase_authority=view.high_phase_authority,
            h1_gate=view.h1_gate,
            h2_gate=view.h2_gate,
            h3_gate=view.h3_gate,
            h4_gate=view.h4_gate,
            first_evidence_at=(
                view.first_evidence_at
                if view.first_evidence_at is not None
                else candidate.first_evidence_at
            ),
            last_evidence_at=(
                view.last_evidence_at
                if view.last_evidence_at is not None
                else candidate.last_evidence_at
            ),
            reason_codes=reasons,
        ),
        view.relationship_count - len(view.ring_relationship_ids),
    )


def _compact_challenger_contexts(
    *,
    incumbent: RegimeRecord,
    ranked: tuple[AuthorityCandidate, ...],
    normal_ledger: NormalEvidenceLedger,
    normal_score: ScheduleScore,
    active_misses: tuple[str, ...],
    events: tuple[PhysicalRestartEvent, ...],
    compact: _Compact,
) -> tuple[ChallengerContext, ...]:
    from .companion_restart_phase2_learned_state import compact_folded_normal_relationships

    contexts = []
    for item in ranked:
        if not _candidate_conflicts_regime(item, incumbent):
            continue
        view, folded_high = _compact_post_establishment_view(
            item, incumbent=incumbent, compact=compact
        )
        folded_normal = compact_folded_normal_relationships(
            compact.state,
            period=item.candidate_period_seconds,
            phase_offset=item.phase_offset,
            tolerance=item.phase_tolerance,
        )
        contexts.append(
            _challenger_context(
                incumbent=incumbent,
                candidate=view,
                normal=_normal_for_candidate(normal_ledger, item),
                normal_context_strength=_schema3_context_strength(normal_score, item),
                active_miss_ids=active_misses,
                events=events,
                folded_high_relationship_count=max(0, folded_high),
                folded_normal_relationship_count=folded_normal.count,
            )
        )
    return tuple(contexts)


def _compact_recovery_confirmations(
    context: ChallengerContext, incumbent_state, compact: _Compact
) -> tuple[str, ...]:
    if context.authorization_state is ChallengerAuthorization.ESTABLISHED:
        return ()
    from .companion_restart_phase2_learned_state import compact_recovery_confirmations

    count = compact_recovery_confirmations(
        incumbent_state,
        challenger_last_evidence_at=context.last_evidence_at,
        ring_events=compact.ring_events,
        ring_relationships=compact.ring_relationships,
    )
    return tuple(f"learned-recovery:{index}" for index in range(count))


def _compact_isolated_off_phase_restart(
    active_off_phase: tuple[str, ...], incumbent_state, compact: _Compact
) -> bool:
    if len(active_off_phase) != 1:
        return False
    anomaly = next(
        (
            item
            for item in _compact_unfolded_events(compact)
            if item.event_id == active_off_phase[0]
        ),
        None,
    )
    if anomaly is None:
        return False
    from .companion_restart_phase2_learned_state import compact_isolated_off_phase_restart

    return compact_isolated_off_phase_restart(
        incumbent_state, anomaly, ring_events=compact.ring_events
    )
