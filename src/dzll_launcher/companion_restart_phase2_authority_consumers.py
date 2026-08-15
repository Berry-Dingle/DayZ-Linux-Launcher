from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum

from .companion_restart_phase2_authority import (
    AuthorityDecision,
    AuthorityGate,
    AuthorityOrigin,
    RegimeRecord,
    RegimeState,
)
from .companion_restart_phase2_consumers import ConsumerDecision


AUTHORITY_CONSUMER_SEMANTIC_VERSION = 1
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
    presentation, visible, suspended, suspension_reason = _presentation(decision, gate, policy)
    period = regime.candidate_period_seconds if regime is not None and visible else None
    label = _cycle_label(period) if period is not None else None
    safe_h3 = bool(
        h3
        and phase_available
        and decision.prediction_usable
        and decision.countdown_safe
        and decision.state in {RegimeState.ESTABLISHED, RegimeState.CHANGE_SUSPECTED, RegimeState.NEW_REGIME_ESTABLISHED}
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
        <= 3 * regime.candidate_period_seconds
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
        if safe_h3 and regime is not None
        else normal.raw_predicted_occurrence_at
        if safe_established_normal_schedule and normal is not None
        else None
    )
    countdown_visible = prediction is not None
    safe_schedule = safe_h3 or safe_established_normal_schedule

    warning_key = None
    warning_at = None
    warning_window = None
    warning_model_eligible = bool(
        safe_schedule
        and policy.restart_alert_enabled
        and policy.server_online_healthy
        and prediction is not None
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
        and policy.normal_pattern_supported
        and presentation
        in {
            AuthorityPresentationKey.LIKELY_CYCLE,
            AuthorityPresentationKey.CONFIRMED_CYCLE,
        }
    )
    confidence = (
        policy.normal_pattern_confidence
        if normal_schedule_confidence_display
        else candidate.combined_confidence
        if candidate is not None and regime is not None
        else (
            policy.normal_pattern_confidence
            if presentation is AuthorityPresentationKey.PATTERN_ONLY
            else decision.combined_confidence
        )
    )
    reasons = {
        *decision.reason_codes,
        f"presentation:{presentation.value}",
        "structural_gate_controls_presentation",
        "generic_recovery_independent_of_schedule",
    }
    if normal_schedule_confidence_display:
        reasons.add("normal_schedule_confidence_display")
    if safe_h3 and countdown_visible:
        reasons.add("safe_h3_prediction_available")
    if safe_established_normal_schedule and countdown_visible:
        reasons.add("safe_established_normal_schedule_available")
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
        phase_available=phase_available or safe_established_normal_schedule,
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
    return {
        "confidence_percent": int(round(decision.confidence_display_value * 100)),
        "confidence_kind": (
            "schedule"
            if normal_schedule_confidence
            else ("period" if decision.visible_cycle else "pattern")
        ),
        "confidence_label": (
            "Confidence:"
            if normal_schedule_confidence or decision.visible_cycle
            else "Pattern Confidence:"
        ),
        "confidence_visible": decision.presentation_state is not AuthorityPresentationKey.NONE,
        "cycle_text": _presentation_text(decision),
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
) -> tuple[AuthorityPresentationKey, bool, bool, str | None]:
    state = decision.state
    if state is RegimeState.UNKNOWN:
        return AuthorityPresentationKey.NONE, False, False, None
    if state is RegimeState.NORMAL_LEARNING:
        if policy.normal_pattern_supported:
            return AuthorityPresentationKey.PATTERN_ONLY, True, False, None
        return AuthorityPresentationKey.NONE, False, False, None
    if state is RegimeState.LIKELY and gate is AuthorityGate.H2:
        return AuthorityPresentationKey.LIKELY_CYCLE, True, False, None
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


def _candidate_for_regime(
    decision: AuthorityDecision, regime: RegimeRecord | None
):
    if regime is None:
        return None
    matches = [item for item in decision.candidates if item.regime_id == regime.regime_id]
    return max(matches, key=lambda item: item.high_authority, default=None)


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
        AuthorityPresentationKey.PATTERN_ONLY: "Recurring timing observed",
        AuthorityPresentationKey.LIKELY_CYCLE: f"Likely: {label}",
        AuthorityPresentationKey.CONFIRMED_CYCLE: f"Confirmed: {label}",
        AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED: "Schedule change suspected",
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
