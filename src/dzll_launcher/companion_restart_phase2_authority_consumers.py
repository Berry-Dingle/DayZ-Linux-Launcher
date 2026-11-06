from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum

from .companion_restart_phase2_authority import (
    COUNTDOWN_SUSPENSION_LATCH_REASON,
    SPARSE_ESTABLISHMENT_REASON,
    SPARSE_PERIOD_AMBIGUOUS_REASON,
    AuthorityCandidate,
    AuthorityDecision,
    AuthorityGate,
    AuthorityOrigin,
    ChallengerAuthorization,
    ChallengerContext,
    RegimeRecord,
    RegimeRecordStatus,
    RegimeState,
    # Shared with authority so the consumer selects exactly what authority
    # selected: incumbent candidate by period and phase, challenger by rank.
    _candidate_rank,
    _context_rank,
    _matching_candidate,
)
from .companion_restart_phase2_consumers import ConsumerDecision


AUTHORITY_CONSUMER_SEMANTIC_VERSION = 1
SUSPECTED_CHANGE_CHALLENGER_CONFIDENCE_REASON = (
    "suspected_change_challenger_confidence_display"
)
SUSPECTED_CHANGE_CONFIDENCE_UNAVAILABLE_REASON = (
    "suspected_change_challenger_confidence_unavailable"
)
# Stage 8.3a: an established continuity-H2 cycle is warning-eligible at the
# displayed 80 % milestone.  Eligibility uses the panel's own rounding
# (``confidence_display_percent``), so internal confidence >= 0.795 (79.5 %,
# which rounds to 80) qualifies.  Stored confidence is never rounded.
SCHEDULED_WARNING_MINIMUM_CONFIDENCE_PERCENT = 80
# Stage 8.4: learning presentation (NONE / PATTERN_ONLY, which includes an
# acquisition LIKELY regime) shows the schema-4 ``combined_confidence`` of the
# candidate authority is actually evaluating, never a schema-3 leading period.
# Pre-establishment evidence is capped: 79 % while establishment structure is
# unmet, 59 % while harmonic blockers or a competing schedule remain.
LEARNING_CONFIDENCE_SOURCE = "schema4_candidate"
LEARNING_CONFIDENCE_REASON = "learning_confidence_schema4_candidate"
LEARNING_CONFIDENCE_UNAVAILABLE_REASON = "learning_confidence_unavailable"
LEARNING_CONFIDENCE_STRUCTURE_CAP = 0.79
LEARNING_CONFIDENCE_AMBIGUITY_CAP = 0.59
LEARNING_CONFIDENCE_STRUCTURE_CAP_REASON = (
    "learning_confidence_ceiling_structure_incomplete"
)
LEARNING_CONFIDENCE_AMBIGUITY_CAP_REASON = (
    "learning_confidence_ceiling_period_ambiguity_or_harmonic"
)
SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED_DEFAULT = False
SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED_DEFAULT = False


class AuthorityPresentationKey(str, Enum):
    NONE = "none"
    PATTERN_ONLY = "pattern_only"
    LIKELY_CYCLE = "likely_cycle"
    CONFIRMED_CYCLE = "confirmed_cycle"
    SCHEDULE_CHANGE_SUSPECTED = "schedule_change_suspected"
    LIKELY_NEW_CYCLE = "likely_new_cycle"
    CONFIRMED_NEW_CYCLE = "confirmed_new_cycle"
    PREDICTION_TEMPORARILY_SUSPENDED = "prediction_temporarily_suspended"


class AuthoritySuppressionKind(str, Enum):
    SCHEDULED_WARNING = "scheduled_warning"
    GENERIC_RECOVERY = "generic_recovery"


class AuthorityRestartClassification(str, Enum):
    NOT_CLASSIFIED = "not_classified"
    SCHEDULED_RESTART = "scheduled_restart"
    UNSCHEDULED_RESTART = "unscheduled_restart"


class CutoverSource(str, Enum):
    SCHEMA3 = "schema3"
    SCHEMA4 = "schema4"
    SCHEMA3_SAFE_FALLBACK = "schema3_safe_fallback"


@dataclass(frozen=True)
class AuthoritySuppressionKey:
    kind: AuthoritySuppressionKind
    server_key: str
    regime_id: str | None = None
    expected_occurrence_at: int | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AuthoritySuppressionKind):
            raise ValueError("kind must be AuthoritySuppressionKind")
        if not self.server_key:
            raise ValueError("server_key is required")
        if self.kind is AuthoritySuppressionKind.SCHEDULED_WARNING:
            if not self.regime_id or self.expected_occurrence_at is None:
                raise ValueError("scheduled warning keys require regime and occurrence")
        if self.kind is AuthoritySuppressionKind.GENERIC_RECOVERY and not self.event_id:
            raise ValueError("generic recovery keys require an event")

    def serialize(self) -> str:
        return "|".join(
            (
                self.kind.value,
                self.server_key,
                self.regime_id or "-",
                str(self.expected_occurrence_at if self.expected_occurrence_at is not None else -1),
                self.event_id or "-",
            )
        )


@dataclass(frozen=True)
class NormalPredictionEvidence:
    """Non-persisted normal-scorer facts needed for safe schedule display."""

    selected_period_seconds: int | None
    incumbent_period_seconds: int | None
    schedule_existence_confidence: float
    fundamental_period_confidence: float
    phase_confidence: float
    candidate_established: bool
    strict_direct_relationship_count: int
    unresolved_competitor: bool
    unresolved_divisor_or_harmonic: bool
    regime_stable: bool
    latest_aligned_phase_at: float | None
    raw_predicted_occurrence_at: float | None
    recent_strong_anomaly: bool

    def __post_init__(self) -> None:
        for name in ("selected_period_seconds", "incumbent_period_seconds"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        for name in (
            "schedule_existence_confidence",
            "fundamental_period_confidence",
            "phase_confidence",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if (
            type(self.strict_direct_relationship_count) is not int
            or self.strict_direct_relationship_count < 0
        ):
            raise ValueError("strict_direct_relationship_count must be non-negative")
        for name in (
            "candidate_established",
            "unresolved_competitor",
            "unresolved_divisor_or_harmonic",
            "regime_stable",
            "recent_strong_anomaly",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in ("latest_aligned_phase_at", "raw_predicted_occurrence_at"):
            value = getattr(self, name)
            if value is not None:
                _finite_nonnegative(name, value)


@dataclass(frozen=True)
class AuthorityConsumerPolicyInput:
    authority_decision: AuthorityDecision
    now: float
    server_online_healthy: bool = True
    restart_alert_enabled: bool = True
    warning_lead_seconds: float = 300.0
    warning_tolerance_seconds: float = 30.0
    scheduled_outage_tolerance_seconds: float | None = None
    observed_outage_at: float | None = None
    physical_recovery_event_id: str | None = None
    physical_recovery_eligible: bool = False
    fired_keys: frozenset[AuthoritySuppressionKey] = frozenset()
    normal_pattern_supported: bool = False
    normal_pattern_confidence: float = 0.0
    normal_prediction_evidence: NormalPredictionEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority_decision, AuthorityDecision):
            raise ValueError("authority_decision must be AuthorityDecision")
        _finite_nonnegative("now", self.now)
        _finite_positive("warning_lead_seconds", self.warning_lead_seconds)
        _finite_nonnegative("warning_tolerance_seconds", self.warning_tolerance_seconds)
        if self.scheduled_outage_tolerance_seconds is not None:
            _finite_nonnegative(
                "scheduled_outage_tolerance_seconds",
                self.scheduled_outage_tolerance_seconds,
            )
        if self.observed_outage_at is not None:
            _finite_nonnegative("observed_outage_at", self.observed_outage_at)
        if not 0 <= self.normal_pattern_confidence <= 1:
            raise ValueError("normal_pattern_confidence must be between zero and one")
        if (
            self.normal_prediction_evidence is not None
            and not isinstance(self.normal_prediction_evidence, NormalPredictionEvidence)
        ):
            raise ValueError("normal_prediction_evidence must be NormalPredictionEvidence")
        if any(not isinstance(item, AuthoritySuppressionKey) for item in self.fired_keys):
            raise ValueError("fired_keys must contain AuthoritySuppressionKey values")


@dataclass(frozen=True)
class AuthorityConsumerDecision:
    semantic_version: int
    decision_id: str
    authority_decision_id: str
    server_key: str
    selected_regime_id: str | None
    presentation_state: AuthorityPresentationKey
    visible_cycle: bool
    cycle_label: str | None
    cycle_period_seconds: int | None
    confidence_display_value: float
    phase_available: bool
    next_expected_restart_at: float | None
    countdown_visible: bool
    countdown_safe: bool
    prediction_suspended: bool
    suspension_reason: str | None
    wording_key: AuthorityPresentationKey
    scheduled_warning_eligible: bool
    scheduled_warning_at: float | None
    scheduled_warning_window: tuple[float, float] | None
    scheduled_warning_suppression_key: AuthoritySuppressionKey | None
    scheduled_pre_restart_alert_eligible: bool
    outage_relaxation_eligible: bool
    restart_classification_policy: AuthorityRestartClassification
    generic_recovery_alert_eligible: bool
    generic_recovery_suppression_key: AuthoritySuppressionKey | None
    transition_change_suspected: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityConsumerComparison:
    comparison_id: str
    schema3_decision: ConsumerDecision
    schema4_decision: AuthorityConsumerDecision
    schema3_selected_period_seconds: int | None
    schema4_selected_period_seconds: int | None
    schema3_presentation_key: str
    schema4_presentation_key: str
    schema3_cycle_visible: bool
    schema4_cycle_visible: bool
    schema3_countdown_visible: bool
    schema4_countdown_visible: bool
    schema3_next_restart_at: float | None
    schema4_next_restart_at: float | None
    schema3_warning_eligible: bool
    schema4_warning_eligible: bool
    schema3_outage_relaxation_eligible: bool
    schema4_outage_relaxation_eligible: bool
    schema3_generic_recovery_eligible: bool
    schema4_generic_recovery_eligible: bool
    reason_code_differences: tuple[str, ...]
    significant_difference: bool


@dataclass(frozen=True)
class AuthorityConsumerResolution:
    source: CutoverSource
    schema3_decision: ConsumerDecision
    schema4_decision: AuthorityConsumerDecision | None
    selected_output: ConsumerDecision | AuthorityConsumerDecision
    diagnostic: str


def confidence_display_percent(value: float) -> int:
    """The user-facing whole-number confidence percentage.

    This is the rounding the panel summary has always used (Python ``round``,
    half to even).  The scheduled-warning milestone compares this same value,
    so the warning and the displayed percentage cannot disagree.
    """

    return int(round(value * 100))


def next_phase_occurrence(*, period_seconds: int, phase_offset: float, now: float) -> float:
    """Return the first phase occurrence strictly after ``now``."""

    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    _finite_nonnegative("phase_offset", phase_offset)
    _finite_nonnegative("now", now)
    phase = phase_offset % period_seconds
    cycle = math.floor((now - phase) / period_seconds) + 1
    result = phase + cycle * period_seconds
    if result <= now:
        result += period_seconds
    return float(result)


def evaluate_authority_consumers(
    policy: AuthorityConsumerPolicyInput,
) -> AuthorityConsumerDecision:
    """Map immutable authority into presentation and alert policy only."""

    decision = policy.authority_decision
    regime = decision.selected_shadow_regime
    candidate = _candidate_for_regime(decision, regime)
    gate = (
        regime.authority_gate
        if regime is not None and regime.origin is AuthorityOrigin.NORMAL
        else (
            candidate.highest_gate
            if candidate is not None
            else (
                regime.authority_gate
                if regime is not None
                else AuthorityGate.NONE
            )
        )
    )
    h3 = gate in {AuthorityGate.H3, AuthorityGate.H4}
    phase_available = bool(
        regime is not None
        and math.isfinite(regime.phase_offset)
        and regime.phase_authority >= 0.85
        and not (candidate and candidate.harmonic_blockers)
    )
    safe_established_continuity_h2 = _safe_established_continuity_h2(
        decision,
        regime,
        candidate,
        gate,
    )
    sparse_regime = _sparse_established_regime(regime)
    safe_established_sparse = _safe_established_sparse(decision, regime, candidate)
    presentation, visible, suspended, suspension_reason = _presentation(
        decision,
        gate,
        policy,
        safe_established_continuity_h2=safe_established_continuity_h2,
        sparse_regime=sparse_regime,
        safe_established_sparse=safe_established_sparse,
    )
    period = regime.candidate_period_seconds if regime is not None and visible else None
    label = _cycle_label(period) if period is not None else None
    safe_h3 = bool(
        h3
        and phase_available
        and decision.prediction_usable
        and decision.countdown_safe
        and decision.state in {
            RegimeState.ESTABLISHED,
            RegimeState.CHANGE_SUSPECTED,
            RegimeState.CHALLENGER_ACCUMULATING,
            RegimeState.NEW_REGIME_ESTABLISHED,
        }
    )
    normal = policy.normal_prediction_evidence
    safe_established_normal_schedule = bool(
        gate is AuthorityGate.NORMAL
        and regime is not None
        and regime.origin is AuthorityOrigin.NORMAL
        and decision.state is RegimeState.ESTABLISHED
        and decision.selected_shadow_regime is not None
        and decision.incumbent_shadow_regime is not None
        and decision.selected_shadow_regime.regime_id
        == decision.incumbent_shadow_regime.regime_id
        and decision.suspicion_level == 0
        and decision.prediction_usable
        and decision.countdown_safe
        and policy.normal_pattern_supported
        and policy.normal_pattern_confidence >= 0.95
        and normal is not None
        and normal.selected_period_seconds == regime.candidate_period_seconds
        and normal.incumbent_period_seconds == regime.candidate_period_seconds
        and normal.schedule_existence_confidence >= 0.95
        and normal.fundamental_period_confidence >= 0.65
        and normal.candidate_established
        and normal.strict_direct_relationship_count >= 3
        and not normal.unresolved_competitor
        and not normal.unresolved_divisor_or_harmonic
        and not (candidate and candidate.harmonic_blockers)
        and normal.regime_stable
        and normal.phase_confidence >= 0.85
        and math.isfinite(regime.phase_offset)
        and normal.latest_aligned_phase_at is not None
        and 0 <= policy.now - normal.latest_aligned_phase_at
        and normal.raw_predicted_occurrence_at is not None
        and math.isfinite(normal.raw_predicted_occurrence_at)
        and normal.raw_predicted_occurrence_at > policy.now
        and not normal.recent_strong_anomaly
    )
    prediction = (
        next_phase_occurrence(
            period_seconds=regime.candidate_period_seconds,
            phase_offset=regime.phase_offset,
            now=policy.now,
        )
        if (safe_h3 or safe_established_continuity_h2 or safe_established_sparse)
        and regime is not None
        else normal.raw_predicted_occurrence_at
        if safe_established_normal_schedule and normal is not None
        else None
    )
    countdown_visible = prediction is not None
    safe_schedule = (
        safe_h3
        or safe_established_continuity_h2
        or safe_established_normal_schedule
        or safe_established_sparse
    )

    warning_key = None
    warning_at = None
    warning_window = None
    warning_model_eligible = bool(
        safe_schedule
        and policy.restart_alert_enabled
        and policy.server_online_healthy
        and prediction is not None
        and (
            not (safe_established_continuity_h2 or safe_established_sparse)
            or confidence_display_percent(decision.combined_confidence)
            >= SCHEDULED_WARNING_MINIMUM_CONFIDENCE_PERCENT
        )
    )
    warning_due = False
    if warning_model_eligible and regime is not None and prediction is not None:
        warning_at = prediction - policy.warning_lead_seconds
        warning_window = (
            warning_at - policy.warning_tolerance_seconds,
            warning_at + policy.warning_tolerance_seconds,
        )
        warning_key = AuthoritySuppressionKey(
            kind=AuthoritySuppressionKind.SCHEDULED_WARNING,
            server_key=decision.server_key,
            regime_id=regime.regime_id,
            expected_occurrence_at=int(round(prediction)),
        )
        warning_due = (
            warning_window[0] <= policy.now <= warning_window[1]
            and warning_key not in policy.fired_keys
        )

    outage_relaxation = False
    if safe_h3 and regime is not None and policy.observed_outage_at is not None:
        tolerance = (
            policy.scheduled_outage_tolerance_seconds
            if policy.scheduled_outage_tolerance_seconds is not None
            else regime.phase_tolerance
        )
        outage_relaxation = _phase_distance(
            policy.observed_outage_at,
            regime.phase_offset,
            regime.candidate_period_seconds,
        ) <= tolerance

    generic_key = None
    generic_eligible = False
    if policy.physical_recovery_event_id:
        generic_key = AuthoritySuppressionKey(
            kind=AuthoritySuppressionKind.GENERIC_RECOVERY,
            server_key=decision.server_key,
            event_id=policy.physical_recovery_event_id,
        )
        generic_eligible = bool(
            policy.restart_alert_enabled
            and policy.physical_recovery_eligible
            and generic_key not in policy.fired_keys
        )

    normal_schedule_confidence_display = bool(
        gate is AuthorityGate.NORMAL
        and not sparse_regime
        and policy.normal_pattern_supported
        and presentation
        in {
            AuthorityPresentationKey.LIKELY_CYCLE,
            AuthorityPresentationKey.CONFIRMED_CYCLE,
        }
    )
    challenger_confidence = (
        _suspected_change_challenger_confidence(decision)
        if presentation is AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED
        else None
    )
    learning_presentation = presentation in {
        AuthorityPresentationKey.NONE,
        AuthorityPresentationKey.PATTERN_ONLY,
    }
    learning_confidence, learning_reasons = (
        _learning_confidence(decision, regime, candidate)
        if learning_presentation
        else (0.0, ())
    )
    confidence = (
        policy.normal_pattern_confidence
        if normal_schedule_confidence_display
        else (challenger_confidence or 0.0)
        if presentation is AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED
        else learning_confidence
        if learning_presentation
        else candidate.combined_confidence
        if candidate is not None and regime is not None
        else decision.combined_confidence
    )
    reasons = {
        *decision.reason_codes,
        f"presentation:{presentation.value}",
        "structural_gate_controls_presentation",
        "generic_recovery_independent_of_schedule",
    }
    if normal_schedule_confidence_display:
        reasons.add("normal_schedule_confidence_display")
    reasons.update(learning_reasons)
    if presentation is AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED:
        reasons.add(
            SUSPECTED_CHANGE_CHALLENGER_CONFIDENCE_REASON
            if challenger_confidence is not None
            else SUSPECTED_CHANGE_CONFIDENCE_UNAVAILABLE_REASON
        )
    if safe_h3 and countdown_visible:
        reasons.add("safe_h3_prediction_available")
    if safe_established_normal_schedule and countdown_visible:
        reasons.add("safe_established_normal_schedule_available")
    if safe_established_continuity_h2 and countdown_visible:
        reasons.add("safe_established_continuity_h2_prediction_available")
    if safe_established_sparse and countdown_visible:
        reasons.add("safe_established_sparse_prediction_available")
    elif gate is AuthorityGate.H2:
        reasons.add("h2_countdown_withheld")
    if suspended:
        reasons.add("unsafe_prediction_suspended")
    if outage_relaxation:
        reasons.add("outage_inside_safe_h3_window")

    payload = {
        "authority": decision.decision_id,
        "regime": regime.regime_id if regime else None,
        "presentation": presentation.value,
        "prediction": prediction,
        "warning_key": warning_key.serialize() if warning_key else None,
        "warning_due": warning_due,
        "generic_key": generic_key.serialize() if generic_key else None,
        "generic": generic_eligible,
        "outage": outage_relaxation,
    }
    consumer_id = _identity("authority-consumer", payload)
    return AuthorityConsumerDecision(
        semantic_version=AUTHORITY_CONSUMER_SEMANTIC_VERSION,
        decision_id=consumer_id,
        authority_decision_id=decision.decision_id,
        server_key=decision.server_key,
        selected_regime_id=regime.regime_id if regime else None,
        presentation_state=presentation,
        visible_cycle=visible,
        cycle_label=label,
        cycle_period_seconds=period,
        confidence_display_value=max(0.0, min(1.0, confidence)),
        phase_available=(
            phase_available
            or safe_established_continuity_h2
            or safe_established_normal_schedule
            or safe_established_sparse
        ),
        next_expected_restart_at=prediction,
        countdown_visible=countdown_visible,
        countdown_safe=safe_schedule,
        prediction_suspended=suspended,
        suspension_reason=suspension_reason,
        wording_key=presentation,
        scheduled_warning_eligible=warning_model_eligible,
        scheduled_warning_at=warning_at,
        scheduled_warning_window=warning_window,
        scheduled_warning_suppression_key=warning_key,
        scheduled_pre_restart_alert_eligible=warning_due,
        outage_relaxation_eligible=outage_relaxation,
        restart_classification_policy=(
            AuthorityRestartClassification.SCHEDULED_RESTART
            if outage_relaxation
            else (
                AuthorityRestartClassification.UNSCHEDULED_RESTART
                if policy.observed_outage_at is not None
                else AuthorityRestartClassification.NOT_CLASSIFIED
            )
        ),
        generic_recovery_alert_eligible=generic_eligible,
        generic_recovery_suppression_key=generic_key,
        transition_change_suspected=decision.state in {
            RegimeState.CHANGE_SUSPECTED,
            RegimeState.CHALLENGER_ACCUMULATING,
            RegimeState.TRANSITION_CONFIRMED,
            RegimeState.NEW_REGIME_PROVISIONAL,
        },
        reason_codes=tuple(sorted(reasons)),
    )


def compare_authority_consumers(
    schema3: ConsumerDecision, schema4: AuthorityConsumerDecision
) -> AuthorityConsumerComparison:
    if not isinstance(schema3, ConsumerDecision):
        raise ValueError("schema3 must be ConsumerDecision")
    schema3_key = _schema3_presentation_key(schema3)
    diffs = []
    pairs = (
        ("selected_period", schema3.selected_period_seconds, schema4.cycle_period_seconds),
        ("presentation", schema3_key, schema4.presentation_state.value),
        ("countdown", schema3.countdown_usable, schema4.countdown_visible),
        ("warning", schema3.warning_eligible, schema4.scheduled_warning_eligible),
        (
            "outage_relaxation",
            schema3.prediction_usable,
            schema4.outage_relaxation_eligible,
        ),
        (
            "generic_recovery",
            schema3.generic_recovery_eligible,
            schema4.generic_recovery_alert_eligible,
        ),
    )
    for name, left, right in pairs:
        if left != right:
            diffs.append(name)
    payload = {
        "schema3": {
            "period": schema3.selected_period_seconds,
            "presentation": schema3_key,
            "countdown": schema3.countdown_usable,
            "next": schema3.prediction_at,
            "warning": schema3.warning_eligible,
            "generic": schema3.generic_recovery_eligible,
        },
        "schema4": {
            "id": schema4.decision_id,
            "period": schema4.cycle_period_seconds,
            "presentation": schema4.presentation_state.value,
            "countdown": schema4.countdown_visible,
            "next": schema4.next_expected_restart_at,
            "warning": schema4.scheduled_warning_eligible,
            "generic": schema4.generic_recovery_alert_eligible,
        },
        "differences": diffs,
    }
    identity = _identity("authority-consumer-comparison", payload)
    return AuthorityConsumerComparison(
        comparison_id=identity,
        schema3_decision=schema3,
        schema4_decision=schema4,
        schema3_selected_period_seconds=schema3.selected_period_seconds,
        schema4_selected_period_seconds=schema4.cycle_period_seconds,
        schema3_presentation_key=schema3_key,
        schema4_presentation_key=schema4.presentation_state.value,
        schema3_cycle_visible=schema3_key != AuthorityPresentationKey.NONE.value,
        schema4_cycle_visible=schema4.visible_cycle,
        schema3_countdown_visible=schema3.countdown_usable,
        schema4_countdown_visible=schema4.countdown_visible,
        schema3_next_restart_at=schema3.prediction_at,
        schema4_next_restart_at=schema4.next_expected_restart_at,
        schema3_warning_eligible=schema3.warning_eligible,
        schema4_warning_eligible=schema4.scheduled_warning_eligible,
        schema3_outage_relaxation_eligible=schema3.prediction_usable,
        schema4_outage_relaxation_eligible=schema4.outage_relaxation_eligible,
        schema3_generic_recovery_eligible=schema3.generic_recovery_eligible,
        schema4_generic_recovery_eligible=schema4.generic_recovery_alert_eligible,
        reason_code_differences=tuple(diffs),
        significant_difference=bool(diffs),
    )


def resolve_authority_consumer_cutover(
    *,
    schema3_decision: ConsumerDecision,
    schema4_decision: AuthorityConsumerDecision | None,
    production_cutover_enabled: bool,
    authoritative_schema4_runtime_enabled: bool,
    schema4_server_valid: bool,
) -> AuthorityConsumerResolution:
    """Select exactly one model; unavailable schema 4 falls back as a whole."""

    if not production_cutover_enabled:
        return AuthorityConsumerResolution(
            CutoverSource.SCHEMA3,
            schema3_decision,
            schema4_decision,
            schema3_decision,
            "schema4_production_cutover_disabled",
        )
    if authoritative_schema4_runtime_enabled and schema4_server_valid and schema4_decision:
        return AuthorityConsumerResolution(
            CutoverSource.SCHEMA4,
            schema3_decision,
            schema4_decision,
            schema4_decision,
            "validated_schema4_consumer_selected",
        )
    return AuthorityConsumerResolution(
        CutoverSource.SCHEMA3_SAFE_FALLBACK,
        schema3_decision,
        schema4_decision,
        schema3_decision,
        "schema4_unavailable_entire_schema3_consumer_fallback",
    )


def authority_consumer_summary(
    decision: AuthorityConsumerDecision, *, now: float
) -> dict:
    """Return the existing summary shape without inspecting GTK state."""

    _finite_nonnegative("now", now)
    prediction = decision.next_expected_restart_at
    remaining = max(0, int(prediction - now)) if prediction is not None else 0
    normal_schedule_confidence = (
        "normal_schedule_confidence_display" in decision.reason_codes
    )
    suspected_change = (
        decision.presentation_state
        is AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED
    )
    learning = decision.presentation_state in {
        AuthorityPresentationKey.NONE,
        AuthorityPresentationKey.PATTERN_ONLY,
    }
    learning_visible = learning and LEARNING_CONFIDENCE_REASON in decision.reason_codes
    learning_fields = (
        {"learning_confidence_source": LEARNING_CONFIDENCE_SOURCE} if learning else {}
    )
    return {
        "confidence_percent": confidence_display_percent(
            decision.confidence_display_value
        ),
        "confidence_kind": (
            "period"
            if learning
            else "schedule"
            if normal_schedule_confidence
            else ("period" if decision.visible_cycle else "pattern")
        ),
        "confidence_label": (
            "Learning Confidence:"
            if learning
            else "Cycle Confidence:"
            if suspected_change
            else "Confidence:"
            if normal_schedule_confidence or decision.visible_cycle
            else "Pattern Confidence:"
        ),
        "confidence_visible": (
            learning_visible
            if learning
            else SUSPECTED_CHANGE_CONFIDENCE_UNAVAILABLE_REASON
            not in decision.reason_codes
        ),
        **learning_fields,
        "cycle_text": (
            "Learning Restart Pattern"
            if learning_visible
            else _presentation_text(decision)
        ),
        "next_text": "Prediction suspended" if decision.prediction_suspended else (
            str(int(prediction)) if prediction is not None else "--"
        ),
        "next_restart_at": prediction,
        "countdown_text": (
            f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}"
            if decision.countdown_visible
            else "--"
        ),
        "next_visible": decision.countdown_visible or decision.prediction_suspended,
        "countdown_visible": decision.countdown_visible,
        "countdown_safe": decision.countdown_safe,
        "prediction_usable": decision.countdown_visible,
        "presentation_key": decision.presentation_state.value,
        "reason_codes": decision.reason_codes,
        "authority_consumer": True,
    }


def _presentation(
    decision: AuthorityDecision,
    gate: AuthorityGate,
    policy: AuthorityConsumerPolicyInput,
    *,
    safe_established_continuity_h2: bool,
    sparse_regime: bool = False,
    safe_established_sparse: bool = False,
) -> tuple[AuthorityPresentationKey, bool, bool, str | None]:
    state = decision.state
    if state is RegimeState.UNKNOWN:
        return AuthorityPresentationKey.NONE, False, False, None
    if state is RegimeState.NORMAL_LEARNING:
        if policy.normal_pattern_supported:
            return AuthorityPresentationKey.PATTERN_ONLY, True, False, None
        return AuthorityPresentationKey.NONE, False, False, None
    if state is RegimeState.LIKELY:
        # Stage 8.4: LIKELY exists only in acquisition (an unknown server or a
        # never-established regime).  It is not an established cycle, so it
        # is presented as Learning with no named period; its evaluated
        # candidate supplies the (capped) learning confidence.  Provisional
        # changes use TRANSITION_CONFIRMED / NEW_REGIME_PROVISIONAL, below.
        return AuthorityPresentationKey.PATTERN_ONLY, False, False, None
    if safe_established_continuity_h2:
        return AuthorityPresentationKey.CONFIRMED_CYCLE, True, False, None
    if safe_established_sparse:
        return AuthorityPresentationKey.CONFIRMED_CYCLE, True, False, None
    if (
        sparse_regime
        and state is RegimeState.ESTABLISHED
        and gate is AuthorityGate.NORMAL
    ):
        # Stage 8.5: a sparse-established regime never falls back to the
        # legacy schema-3 schedule display; an unsafe one is suspended.
        return (
            AuthorityPresentationKey.PREDICTION_TEMPORARILY_SUSPENDED,
            True,
            True,
            "sparse_established_prediction_unsafe",
        )
    if (
        state in {RegimeState.LIKELY, RegimeState.ESTABLISHED}
        and gate is AuthorityGate.NORMAL
        and policy.normal_pattern_supported
    ):
        return AuthorityPresentationKey.LIKELY_CYCLE, True, False, None
    if state is RegimeState.NEW_REGIME_ESTABLISHED:
        return AuthorityPresentationKey.CONFIRMED_NEW_CYCLE, True, False, None
    if state in {RegimeState.TRANSITION_CONFIRMED, RegimeState.NEW_REGIME_PROVISIONAL}:
        return (
            AuthorityPresentationKey.LIKELY_NEW_CYCLE,
            True,
            True,
            "provisional_regime_transition",
        )
    if state is RegimeState.CHALLENGER_ACCUMULATING and not decision.countdown_safe:
        return (
            AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED,
            True,
            True,
            "coherent_challenger_countdown_unsafe",
        )
    if state is RegimeState.CHANGE_SUSPECTED or state is RegimeState.CHALLENGER_ACCUMULATING:
        return AuthorityPresentationKey.CONFIRMED_CYCLE, True, False, None
    if state is RegimeState.ESTABLISHED and gate in {AuthorityGate.H3, AuthorityGate.H4}:
        if decision.countdown_safe:
            return AuthorityPresentationKey.CONFIRMED_CYCLE, True, False, None
        return (
            AuthorityPresentationKey.PREDICTION_TEMPORARILY_SUSPENDED,
            True,
            True,
            "authority_decision_countdown_unsafe",
        )
    return AuthorityPresentationKey.NONE, False, False, None


def _safe_established_continuity_h2(
    decision: AuthorityDecision,
    regime: RegimeRecord | None,
    candidate: AuthorityCandidate | None,
    gate: AuthorityGate,
) -> bool:
    """Accept a durable incumbent at H2 only under the authority safety contract."""

    incumbent = decision.incumbent_shadow_regime
    return bool(
        gate is AuthorityGate.H2
        and regime is not None
        and regime.origin is AuthorityOrigin.CONTINUITY
        and regime.status is RegimeRecordStatus.ESTABLISHED
        and decision.state is RegimeState.ESTABLISHED
        and incumbent is not None
        and regime.regime_id == incumbent.regime_id
        and decision.suspicion_level == 0
        and decision.cycle_visible
        and decision.prediction_usable
        and decision.countdown_safe
        and regime.candidate_period_seconds > 0
        and math.isfinite(regime.phase_offset)
        and not (candidate and candidate.harmonic_blockers)
    )


def _sparse_established_regime(regime: RegimeRecord | None) -> bool:
    return bool(
        regime is not None
        and regime.origin is AuthorityOrigin.NORMAL
        and SPARSE_ESTABLISHMENT_REASON in regime.reason_codes
    )


def _safe_established_sparse(
    decision: AuthorityDecision,
    regime: RegimeRecord | None,
    candidate: AuthorityCandidate | None,
) -> bool:
    """Stage 8.5: a sparse-established incumbent under the H2 safety contract.

    The regime was established by the authority sparse gate (persistent
    reason code); its matching candidate must still be unambiguous (no
    harmonic blocker, period alternatives refuted) and there must be no
    suspicion.  The countdown uses the regime's own period and phase.
    """

    incumbent = decision.incumbent_shadow_regime
    return bool(
        _sparse_established_regime(regime)
        and regime.status is RegimeRecordStatus.ESTABLISHED
        and decision.state is RegimeState.ESTABLISHED
        and incumbent is not None
        and regime.regime_id == incumbent.regime_id
        and decision.suspicion_level == 0
        and decision.cycle_visible
        and decision.prediction_usable
        and decision.countdown_safe
        and regime.candidate_period_seconds > 0
        and math.isfinite(regime.phase_offset)
        and candidate is not None
        and not candidate.harmonic_blockers
        and SPARSE_PERIOD_AMBIGUOUS_REASON not in candidate.reason_codes
    )


def _learning_confidence(
    decision: AuthorityDecision,
    regime: RegimeRecord | None,
    candidate: AuthorityCandidate | None,
) -> tuple[float, tuple[str, ...]]:
    """Display-only learning confidence from the evaluated schema-4 candidate.

    With a selected regime (an acquisition LIKELY regime, or an incumbent
    whose presentation fell back to Learning) the evaluated candidate is that
    regime's candidate.  Without one (NORMAL_LEARNING) it is the leading
    candidate: authority's structural rank (gate, then high authority), with
    ``combined_confidence`` deciding among equally structured candidates, so
    sparse-only candidates are compared by their actual support.  The value
    is that candidate's existing ``combined_confidence``: nothing is rescaled
    (the sparse ceiling stays 0.10).  Learning is pre-establishment, so it is
    capped at 0.79, or 0.59 while harmonic blockers or a competing schedule at
    an equal structural gate remain.  Nothing here feeds back into authority.
    """

    if regime is not None:
        evaluated = candidate
    else:
        evaluated = max(decision.candidates, key=_learning_candidate_rank, default=None)
    if evaluated is None and regime is None:
        return 0.0, (LEARNING_CONFIDENCE_UNAVAILABLE_REASON,)
    value = (
        evaluated.combined_confidence
        if evaluated is not None
        else regime.combined_confidence
    )
    ambiguous = evaluated is not None and bool(
        evaluated.harmonic_blockers
        or SPARSE_PERIOD_AMBIGUOUS_REASON in evaluated.reason_codes
        or _competing_schedule(decision, evaluated)
    )
    cap, cap_reason = (
        (LEARNING_CONFIDENCE_AMBIGUITY_CAP, LEARNING_CONFIDENCE_AMBIGUITY_CAP_REASON)
        if ambiguous
        else (LEARNING_CONFIDENCE_STRUCTURE_CAP, LEARNING_CONFIDENCE_STRUCTURE_CAP_REASON)
    )
    if not math.isfinite(value):
        return 0.0, (LEARNING_CONFIDENCE_UNAVAILABLE_REASON,)
    value = round(max(0.0, min(cap, value)), 6)
    if confidence_display_percent(value) < 1:
        return value, (LEARNING_CONFIDENCE_UNAVAILABLE_REASON, cap_reason)
    return value, (LEARNING_CONFIDENCE_REASON, cap_reason)


def _learning_candidate_rank(candidate: AuthorityCandidate) -> tuple[object, ...]:
    gate_rank, high_authority, *rest = _candidate_rank(candidate)
    return (gate_rank, high_authority, candidate.combined_confidence, *rest)


def _competing_schedule(
    decision: AuthorityDecision, evaluated: AuthorityCandidate
) -> bool:
    """Another schedule (period, or phase outside tolerance) is structurally
    as well supported (same or higher H gate, at least H1)."""

    threshold = max(1, _candidate_rank(evaluated)[0])
    period = evaluated.candidate_period_seconds
    return any(
        _candidate_rank(item)[0] >= threshold
        and (
            item.candidate_period_seconds != period
            or _phase_distance(item.phase_offset, evaluated.phase_offset, period)
            > evaluated.phase_tolerance
        )
        for item in decision.candidates
        if item.candidate_id != evaluated.candidate_id
    )


def _candidate_for_regime(
    decision: AuthorityDecision, regime: RegimeRecord | None
):
    if regime is None:
        return None
    matches = [item for item in decision.candidates if item.regime_id == regime.regime_id]
    if matches:
        return max(matches, key=lambda item: item.high_authority)
    # A durable incumbent keeps its original regime ID while candidate IDs
    # follow their defining evidence.  Authority matches the incumbent's
    # candidate by exact period and phase tolerance; use the same rule so the
    # harmonic blockers of the real candidate are never skipped.
    return _matching_candidate(regime, decision.candidates)


def _suspected_change_challenger_confidence(
    decision: AuthorityDecision,
) -> float | None:
    """Display-only confidence in the alternative behind a level-2 suspicion.

    The relevant challenger is the active, credible level-2 context: the one
    authority latched as suspending the countdown.  Ties resolve with
    authority's own challenger rank.  The value is that challenger's
    phase-qualified high authority (authority's ``high_authority *
    phase_quality``), using the post-establishment evidence that authority
    itself uses for the challenger.  Nothing here feeds back into authority.
    """

    incumbent = decision.incumbent_shadow_regime
    if incumbent is None:
        return None
    relevant = [
        item
        for item in decision.challenger_contexts
        if COUNTDOWN_SUSPENSION_LATCH_REASON in item.reason_codes
        and item.incumbent_regime_id == incumbent.regime_id
        and item.authorization_state is not ChallengerAuthorization.ESTABLISHED
        and len(item.high_relationship_ids) >= 2
    ]
    context = max(relevant, key=_context_rank, default=None)
    if context is None:
        return None
    candidate = _challenger_candidate(decision, context)
    if candidate is None:
        return None
    value = context.high_authority * candidate.phase_quality
    if not math.isfinite(value) or value <= 0:
        return None
    return round(min(1.0, value), 6)


def _challenger_candidate(
    decision: AuthorityDecision, context: ChallengerContext
) -> AuthorityCandidate | None:
    return next(
        (
            item
            for item in decision.candidates
            if item.regime_id == context.proposed_regime_id
            and item.candidate_period_seconds == context.candidate_period_seconds
        ),
        None,
    )


def _cycle_label(period: int | None) -> str | None:
    if period is None:
        return None
    hours = period / 3600
    unit = "hour" if hours == 1 else "hours"
    return f"Every {hours:g} {unit}"


def _presentation_text(decision: AuthorityConsumerDecision) -> str:
    label = decision.cycle_label or ""
    return {
        AuthorityPresentationKey.NONE: "--",
        AuthorityPresentationKey.PATTERN_ONLY: "Learning Restart Pattern",
        AuthorityPresentationKey.LIKELY_CYCLE: f"Likely: {label}",
        AuthorityPresentationKey.CONFIRMED_CYCLE: f"Confirmed: {label}",
        AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED: "Suspected Cycle Change",
        AuthorityPresentationKey.LIKELY_NEW_CYCLE: f"Likely new: {label}",
        AuthorityPresentationKey.CONFIRMED_NEW_CYCLE: f"Confirmed new: {label}",
        AuthorityPresentationKey.PREDICTION_TEMPORARILY_SUSPENDED: "Prediction temporarily suspended",
    }[decision.presentation_state]


def _schema3_presentation_key(decision: ConsumerDecision) -> str:
    status = decision.model_status.value
    if status == "no_pattern":
        return AuthorityPresentationKey.NONE.value
    if status == "pattern_observed_period_unknown":
        return AuthorityPresentationKey.PATTERN_ONLY.value
    if status == "likely_period":
        return AuthorityPresentationKey.LIKELY_CYCLE.value
    if status == "confirmed_period":
        return AuthorityPresentationKey.CONFIRMED_CYCLE.value
    if status == "schedule_change_suspected":
        return AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED.value
    return AuthorityPresentationKey.PREDICTION_TEMPORARILY_SUSPENDED.value


def _phase_distance(at: float, phase: float, period: int) -> float:
    return abs(((at - phase + period / 2) % period) - period / 2)


def _identity(namespace: str, payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"{namespace}|{raw}".encode("utf-8")).hexdigest()


def _finite_nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _finite_positive(name: str, value: object) -> None:
    _finite_nonnegative(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
