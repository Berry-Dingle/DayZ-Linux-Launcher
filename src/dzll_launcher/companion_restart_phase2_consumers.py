from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .companion_restart_phase2_detection import EventOutcome, PhysicalRestartEvent
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    PHASE_UNCERTAINTY_CAP,
    SCORING_SEMANTICS_VERSION,
    CandidateScore,
    RegimeStatus,
    ScheduleScore,
    candidate_phase_tolerance,
)


EXPECTED_PHASE2_SCHEMA_VERSION = 3
EXPECTED_SCORER_VERSION = SCORING_SEMANTICS_VERSION
CONSUMER_ADAPTER_VERSION = 1


class ConsumerModelStatus(str, Enum):
    NO_PATTERN = "no_pattern"
    PATTERN_OBSERVED = "pattern_observed_period_unknown"
    LIKELY_PERIOD = "likely_period"
    CONFIRMED_PERIOD = "confirmed_period"
    PERIOD_UNCERTAIN = "period_uncertain"
    PHASE_UNCERTAIN = "phase_uncertain"
    SCHEDULE_CHANGE_SUSPECTED = "schedule_change_suspected"
    NEW_REGIME_ESTABLISHING = "new_regime_establishing"


class PredictionStatus(str, Enum):
    USABLE = "prediction_usable"
    SUSPENDED = "prediction_suspended"
    UNAVAILABLE = "prediction_unavailable"


class ScheduledEventClassification(str, Enum):
    SCHEDULED_RESTART = "scheduled_restart"
    UNSCHEDULED_RESTART = "unscheduled_restart"
    NOT_CLASSIFIED = "not_classified"


class RecoveryAction(str, Enum):
    NO_ALERT = "no_alert_action"
    GENERIC_RECOVERY = "generic_recovery"
    SCHEDULED_RECOVERY = "scheduled_recovery"
    QUERY_VISIBLE_RECOVERY = "query_visible_recovery"


class AlertKeyKind(str, Enum):
    GENERIC_RECOVERY = "generic_recovery"
    SCHEDULED_RECOVERY = "scheduled_recovery"
    QUERY_VISIBLE_RECOVERY = "query_visible_recovery"
    FIVE_MINUTE_WARNING = "five_minute_warning"
    PREDICTED_RESTART_CYCLE = "predicted_restart_cycle"


class BlockReason(str, Enum):
    INSUFFICIENT_EVENTS = "insufficient_events"
    NO_SCHEDULE_PATTERN = "no_schedule_pattern"
    PERIOD_CONFIDENCE_TOO_LOW = "period_confidence_too_low"
    PHASE_CONFIDENCE_TOO_LOW = "phase_confidence_too_low"
    CANDIDATE_NOT_ESTABLISHED = "candidate_not_established"
    INSUFFICIENT_DIRECT_INTERVALS = "insufficient_direct_intervals"
    UNRESOLVED_HARMONIC = "unresolved_harmonic"
    COMPETING_CANDIDATE_TOO_CLOSE = "competing_candidate_too_close"
    SCHEDULE_CHANGE_SUSPECTED = "schedule_change_suspected"
    NEW_REGIME_ESTABLISHING = "new_regime_establishing"
    PERIOD_UNCERTAIN = "period_uncertain"
    PREDICTION_UNAVAILABLE = "prediction_unavailable"
    PREDICTION_STALE = "prediction_stale"
    SCORER_VERSION_MISMATCH = "scorer_version_mismatch"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    SCORER_PREDICTION_SUSPENDED = "scorer_prediction_suspended"
    RECENT_STRONG_ANOMALY = "recent_strong_anomaly"
    SERVER_OFFLINE = "server_offline"
    ALERT_DISABLED = "alert_disabled"
    OUTSIDE_WARNING_TOLERANCE = "outside_warning_tolerance"
    INSUFFICIENT_RECENT_CONFIRMATIONS = "insufficient_recent_confirmations"
    WARNING_ALREADY_FIRED = "warning_already_fired"
    EVENT_AUTHENTICITY_TOO_LOW = "event_authenticity_too_low"
    EVENT_NOT_PHASE_ALIGNED = "event_not_phase_aligned"
    EVENT_REGIME_MISMATCH = "event_regime_mismatch"
    EVENT_ALREADY_ALERTED = "event_already_alerted"
    GENERIC_ALERT_ALREADY_FIRED = "generic_alert_already_fired"
    QUERY_VISIBLE_ALERT_ALREADY_FIRED = "query_visible_alert_already_fired"
    EVENT_NOT_OFFLINE_RECOVERY = "event_not_offline_recovery"
    EVENT_NOT_QUERY_VISIBLE = "event_not_query_visible"
    PROBABLE_QUERY_VISIBLE_EVENT = "probable_query_visible_event"
    AMBIGUOUS_QUERY_VISIBLE_EVENT = "ambiguous_query_visible_event"
    GENERIC_DURATION_REQUIREMENT_FAILED = "generic_duration_requirement_failed"


@dataclass(frozen=True)
class SuppressionKey:
    kind: AlertKeyKind
    server_key: str
    event_id: str | None = None
    candidate_period_seconds: int | None = None
    regime_generation: str = ""
    prediction_at: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AlertKeyKind):
            raise ValueError("kind must be AlertKeyKind")
        if not isinstance(self.server_key, str) or not self.server_key:
            raise ValueError("server_key must be non-empty")
        if self.event_id is not None and (not isinstance(self.event_id, str) or not self.event_id):
            raise ValueError("event_id must be null or non-empty")
        if self.candidate_period_seconds is not None and self.candidate_period_seconds not in CANDIDATE_PERIODS:
            raise ValueError("candidate period is unsupported")
        if not isinstance(self.regime_generation, str) or not self.regime_generation:
            raise ValueError("regime_generation must be non-empty")
        if self.prediction_at is not None and (type(self.prediction_at) is not int or self.prediction_at < 0):
            raise ValueError("prediction_at must be a non-negative integer or null")

    def serialize(self) -> str:
        return "|".join(
            (
                self.kind.value,
                self.server_key,
                self.event_id or "-",
                str(self.candidate_period_seconds or 0),
                self.regime_generation,
                str(self.prediction_at if self.prediction_at is not None else -1),
            )
        )


@dataclass(frozen=True)
class EventPolicyContext:
    event: PhysicalRestartEvent
    regime_generation: str
    generic_duration_ok: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.event, PhysicalRestartEvent):
            raise ValueError("event must be PhysicalRestartEvent")
        if not isinstance(self.regime_generation, str) or not self.regime_generation:
            raise ValueError("regime_generation must be non-empty")
        if type(self.generic_duration_ok) is not bool:
            raise ValueError("generic_duration_ok must be boolean")


@dataclass(frozen=True)
class ConsumerPolicyInput:
    score: ScheduleScore
    server_key: str
    regime_generation: str
    now: float
    independent_authentic_event_count: int
    predicted_restart_at: float | None = None
    recent_covered_confirmation_count: int = 0
    server_online_healthy: bool = True
    restart_alert_enabled: bool = True
    warning_lead_seconds: float = 300.0
    warning_tolerance_seconds: float = 30.0
    fired_keys: frozenset[SuppressionKey] = frozenset()
    event_context: EventPolicyContext | None = None
    recent_strong_anomaly: bool = False
    scorer_version: int = EXPECTED_SCORER_VERSION
    schema_version: int = EXPECTED_PHASE2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.score, ScheduleScore):
            raise ValueError("score must be ScheduleScore")
        for name in ("server_key", "regime_generation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")
        _finite_nonnegative("now", self.now)
        if self.predicted_restart_at is not None:
            _finite_nonnegative("predicted_restart_at", self.predicted_restart_at)
        for name in ("independent_authentic_event_count", "recent_covered_confirmation_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _finite_positive("warning_lead_seconds", self.warning_lead_seconds)
        _finite_nonnegative("warning_tolerance_seconds", self.warning_tolerance_seconds)
        for name in ("server_online_healthy", "restart_alert_enabled", "recent_strong_anomaly"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if any(not isinstance(item, SuppressionKey) for item in self.fired_keys):
            raise ValueError("fired_keys must contain SuppressionKey values")


@dataclass(frozen=True)
class ConsumerDecision:
    selected_period_seconds: int | None
    schedule_existence_confidence: float
    fundamental_period_confidence: float
    phase_confidence: float
    model_status: ConsumerModelStatus
    period_confirmed: bool
    prediction_status: PredictionStatus
    prediction_at: float | None
    prediction_usable: bool
    countdown_usable: bool
    warning_eligible: bool
    scheduled_event_classification: ScheduledEventClassification
    generic_recovery_eligible: bool
    query_visible_recovery_eligible: bool
    preferred_recovery_action: RecoveryAction
    model_reasons: tuple[BlockReason, ...]
    prediction_reasons: tuple[BlockReason, ...]
    warning_reasons: tuple[BlockReason, ...]
    scheduled_event_reasons: tuple[BlockReason, ...]
    generic_recovery_reasons: tuple[BlockReason, ...]
    query_visible_reasons: tuple[BlockReason, ...]
    candidate_ambiguity: tuple[int, ...]
    regime_status: RegimeStatus
    selected_candidate_identity: str | None
    scorer_version: int
    schema_version: int
    adapter_version: int
    regime_generation: str
    prediction_cycle_key: SuppressionKey | None
    warning_key: SuppressionKey | None
    scheduled_event_key: SuppressionKey | None
    generic_recovery_key: SuppressionKey | None
    query_visible_key: SuppressionKey | None


@dataclass(frozen=True)
class CompatibilitySummary:
    restart_learning_summary_available: bool
    cycle_hours: float | None
    confidence: float
    next_restart_at: float | None
    established_model: bool
    alert_usability: bool
    scheduled_outage_threshold_usable: bool
    warning_eligible: bool
    prediction_blocked: bool
    blocked_reasons: tuple[BlockReason, ...]
    derived_from_phase2: bool
    scorer_version: int
    schema_version: int
    adapter_version: int


def event_alert_key(
    kind: AlertKeyKind,
    *,
    server_key: str,
    event_id: str,
    candidate_period_seconds: int | None,
    regime_generation: str,
) -> SuppressionKey:
    if kind not in {
        AlertKeyKind.GENERIC_RECOVERY,
        AlertKeyKind.SCHEDULED_RECOVERY,
        AlertKeyKind.QUERY_VISIBLE_RECOVERY,
    }:
        raise ValueError("event alert key requires an event alert kind")
    if kind is AlertKeyKind.GENERIC_RECOVERY:
        # Generic recovery belongs to the physical event, not a learned regime.
        candidate_period_seconds = None
        regime_generation = "physical-event"
    return SuppressionKey(
        kind=kind,
        server_key=server_key,
        event_id=event_id,
        candidate_period_seconds=candidate_period_seconds,
        regime_generation=regime_generation,
    )


def prediction_key(
    kind: AlertKeyKind,
    *,
    server_key: str,
    candidate_period_seconds: int,
    regime_generation: str,
    prediction_at: float,
) -> SuppressionKey:
    if kind not in {AlertKeyKind.FIVE_MINUTE_WARNING, AlertKeyKind.PREDICTED_RESTART_CYCLE}:
        raise ValueError("prediction key requires a prediction key kind")
    _finite_nonnegative("prediction_at", prediction_at)
    return SuppressionKey(
        kind=kind,
        server_key=server_key,
        candidate_period_seconds=candidate_period_seconds,
        regime_generation=regime_generation,
        prediction_at=int(round(prediction_at)),
    )


def evaluate_consumers(policy: ConsumerPolicyInput) -> ConsumerDecision:
    selected = _selected_candidate(policy.score)
    schedule_confidence = policy.score.schedule_existence_confidence
    period_confidence = selected.fundamental_period_confidence if selected else 0.0
    phase_confidence = selected.phase_confidence if selected else 0.0
    version_reasons = _version_reasons(policy)
    transition_reasons = _transition_reasons(policy.score)
    ambiguity_reasons = (
        (BlockReason.COMPETING_CANDIDATE_TOO_CLOSE,)
        if policy.score.unresolved_competitors
        else ()
    )

    meaningful_pattern = any(
        candidate.aligned_event_count >= 3
        or candidate.strict_direct_interval_count >= 1
        or candidate.hints.event_count >= 3
        for candidate in policy.score.candidates
    )
    pattern_observed = (
        schedule_confidence >= 0.60
        and policy.independent_authentic_event_count >= 3
        and meaningful_pattern
        and not version_reasons
    )
    likely = bool(
        pattern_observed
        and selected
        and schedule_confidence >= 0.70
        and period_confidence >= 0.65
        and selected.strict_direct_interval_count >= 3
        and not ambiguity_reasons
        and policy.score.regime_status is RegimeStatus.STABLE
    )
    period_confirmed = bool(
        pattern_observed
        and selected
        and schedule_confidence >= 0.80
        and period_confidence >= 0.80
        and selected.establishment_gates_passed
        and not ambiguity_reasons
        and policy.score.regime_status
        not in {RegimeStatus.CHANGE_SUSPECTED, RegimeStatus.PERIOD_UNCERTAIN}
    )
    model_reasons: list[BlockReason] = [*version_reasons]
    if policy.independent_authentic_event_count < 3:
        model_reasons.append(BlockReason.INSUFFICIENT_EVENTS)
    if schedule_confidence < 0.60 or not meaningful_pattern:
        model_reasons.append(BlockReason.NO_SCHEDULE_PATTERN)
    if selected is not None and period_confidence < 0.65:
        model_reasons.append(BlockReason.PERIOD_CONFIDENCE_TOO_LOW)
    if selected is not None and selected.strict_direct_interval_count < 3:
        model_reasons.append(BlockReason.INSUFFICIENT_DIRECT_INTERVALS)
    model_reasons.extend(ambiguity_reasons)
    model_reasons.extend(transition_reasons)
    if selected and not selected.establishment_gates_passed and period_confidence >= 0.80:
        model_reasons.append(BlockReason.CANDIDATE_NOT_ESTABLISHED)
        if selected.divisor_resolution and not all(
            item.established_resolved for item in selected.divisor_resolution
        ):
            model_reasons.append(BlockReason.UNRESOLVED_HARMONIC)

    model_status = _model_status(
        pattern_observed,
        likely,
        period_confirmed,
        phase_confidence,
        policy.score.regime_status,
        bool(ambiguity_reasons),
    )

    prediction_reasons = _prediction_reasons(
        policy,
        selected,
        schedule_confidence,
        period_confidence,
        phase_confidence,
        version_reasons,
        transition_reasons,
        ambiguity_reasons,
    )
    prediction_usable = not prediction_reasons
    prediction_at = policy.predicted_restart_at if prediction_usable else None
    if prediction_usable:
        prediction_status = PredictionStatus.USABLE
    elif policy.predicted_restart_at is None or selected is None:
        prediction_status = PredictionStatus.UNAVAILABLE
    else:
        prediction_status = PredictionStatus.SUSPENDED

    cycle_key = None
    warning_key = None
    if selected is not None and policy.predicted_restart_at is not None:
        cycle_key = prediction_key(
            AlertKeyKind.PREDICTED_RESTART_CYCLE,
            server_key=policy.server_key,
            candidate_period_seconds=selected.period_seconds,
            regime_generation=policy.regime_generation,
            prediction_at=policy.predicted_restart_at,
        )
        warning_key = prediction_key(
            AlertKeyKind.FIVE_MINUTE_WARNING,
            server_key=policy.server_key,
            candidate_period_seconds=selected.period_seconds,
            regime_generation=policy.regime_generation,
            prediction_at=policy.predicted_restart_at,
        )
    warning_reasons = _warning_reasons(
        policy,
        selected,
        schedule_confidence,
        period_confidence,
        phase_confidence,
        prediction_reasons,
        warning_key,
    )

    scheduled_classification, scheduled_reasons, scheduled_key = _scheduled_event_decision(
        policy,
        selected,
        schedule_confidence,
        period_confidence,
        phase_confidence,
        version_reasons,
        transition_reasons,
        ambiguity_reasons,
    )
    generic_eligible, generic_reasons, generic_key = _generic_recovery_decision(
        policy, selected
    )
    query_eligible, query_reasons, query_key = _query_visible_decision(
        policy,
        selected,
        schedule_confidence,
        period_confidence,
        phase_confidence,
        version_reasons,
        transition_reasons,
        ambiguity_reasons,
    )

    if scheduled_classification is ScheduledEventClassification.SCHEDULED_RESTART and generic_eligible:
        preferred_action = RecoveryAction.SCHEDULED_RECOVERY
    elif query_eligible:
        preferred_action = RecoveryAction.QUERY_VISIBLE_RECOVERY
    elif generic_eligible:
        preferred_action = RecoveryAction.GENERIC_RECOVERY
    else:
        preferred_action = RecoveryAction.NO_ALERT

    identity = (
        f"period:{selected.period_seconds}:regime:{policy.regime_generation}"
        if selected is not None
        else None
    )
    return ConsumerDecision(
        selected_period_seconds=selected.period_seconds if selected else None,
        schedule_existence_confidence=schedule_confidence,
        fundamental_period_confidence=period_confidence,
        phase_confidence=phase_confidence,
        model_status=model_status,
        period_confirmed=period_confirmed,
        prediction_status=prediction_status,
        prediction_at=prediction_at,
        prediction_usable=prediction_usable,
        countdown_usable=prediction_usable,
        warning_eligible=not warning_reasons,
        scheduled_event_classification=scheduled_classification,
        generic_recovery_eligible=generic_eligible,
        query_visible_recovery_eligible=query_eligible,
        preferred_recovery_action=preferred_action,
        model_reasons=_reasons(model_reasons),
        prediction_reasons=_reasons(prediction_reasons),
        warning_reasons=_reasons(warning_reasons),
        scheduled_event_reasons=_reasons(scheduled_reasons),
        generic_recovery_reasons=_reasons(generic_reasons),
        query_visible_reasons=_reasons(query_reasons),
        candidate_ambiguity=policy.score.unresolved_competitors,
        regime_status=policy.score.regime_status,
        selected_candidate_identity=identity,
        scorer_version=policy.scorer_version,
        schema_version=policy.schema_version,
        adapter_version=CONSUMER_ADAPTER_VERSION,
        regime_generation=policy.regime_generation,
        prediction_cycle_key=cycle_key,
        warning_key=warning_key,
        scheduled_event_key=scheduled_key,
        generic_recovery_key=generic_key,
        query_visible_key=query_key,
    )


def compatibility_summary(decision: ConsumerDecision) -> CompatibilitySummary:
    if not isinstance(decision, ConsumerDecision):
        raise ValueError("decision must be ConsumerDecision")
    established = decision.period_confirmed and decision.model_status in {
        ConsumerModelStatus.CONFIRMED_PERIOD,
        ConsumerModelStatus.PHASE_UNCERTAIN,
    }
    return CompatibilitySummary(
        restart_learning_summary_available=decision.model_status is not ConsumerModelStatus.NO_PATTERN,
        cycle_hours=(decision.selected_period_seconds / 3600 if decision.selected_period_seconds else None),
        confidence=decision.fundamental_period_confidence,
        next_restart_at=decision.prediction_at,
        established_model=established,
        alert_usability=decision.prediction_usable,
        scheduled_outage_threshold_usable=decision.prediction_usable,
        warning_eligible=decision.warning_eligible,
        prediction_blocked=not decision.prediction_usable,
        blocked_reasons=decision.prediction_reasons,
        derived_from_phase2=True,
        scorer_version=decision.scorer_version,
        schema_version=decision.schema_version,
        adapter_version=decision.adapter_version,
    )


def _selected_candidate(score: ScheduleScore) -> CandidateScore | None:
    if score.selected_period_seconds is None:
        return None
    try:
        return score.candidate(score.selected_period_seconds)
    except KeyError:
        return None


def _version_reasons(policy: ConsumerPolicyInput) -> tuple[BlockReason, ...]:
    reasons = []
    if policy.scorer_version != EXPECTED_SCORER_VERSION:
        reasons.append(BlockReason.SCORER_VERSION_MISMATCH)
    if policy.schema_version != EXPECTED_PHASE2_SCHEMA_VERSION:
        reasons.append(BlockReason.SCHEMA_VERSION_MISMATCH)
    return tuple(reasons)


def _transition_reasons(score: ScheduleScore) -> tuple[BlockReason, ...]:
    mapping = {
        RegimeStatus.CHANGE_SUSPECTED: BlockReason.SCHEDULE_CHANGE_SUSPECTED,
        RegimeStatus.PERIOD_UNCERTAIN: BlockReason.PERIOD_UNCERTAIN,
        RegimeStatus.NEW_REGIME_ESTABLISHING: BlockReason.NEW_REGIME_ESTABLISHING,
        RegimeStatus.PHASE_UNCERTAIN: BlockReason.PHASE_CONFIDENCE_TOO_LOW,
    }
    reason = mapping.get(score.regime_status)
    return (reason,) if reason else ()


def _model_status(
    pattern: bool,
    likely: bool,
    confirmed: bool,
    phase_confidence: float,
    regime: RegimeStatus,
    ambiguous: bool,
) -> ConsumerModelStatus:
    if regime is RegimeStatus.CHANGE_SUSPECTED:
        return ConsumerModelStatus.SCHEDULE_CHANGE_SUSPECTED
    if regime is RegimeStatus.NEW_REGIME_ESTABLISHING:
        return ConsumerModelStatus.NEW_REGIME_ESTABLISHING
    if regime is RegimeStatus.PERIOD_UNCERTAIN or ambiguous:
        return ConsumerModelStatus.PERIOD_UNCERTAIN
    if not pattern:
        return ConsumerModelStatus.NO_PATTERN
    if confirmed and phase_confidence < 0.80:
        return ConsumerModelStatus.PHASE_UNCERTAIN
    if confirmed:
        return ConsumerModelStatus.CONFIRMED_PERIOD
    if likely:
        return ConsumerModelStatus.LIKELY_PERIOD
    return ConsumerModelStatus.PATTERN_OBSERVED


def _prediction_reasons(
    policy: ConsumerPolicyInput,
    candidate: CandidateScore | None,
    schedule: float,
    period: float,
    phase: float,
    version_reasons: tuple[BlockReason, ...],
    transition_reasons: tuple[BlockReason, ...],
    ambiguity_reasons: tuple[BlockReason, ...],
) -> tuple[BlockReason, ...]:
    reasons: list[BlockReason] = [*version_reasons, *transition_reasons, *ambiguity_reasons]
    if schedule < 0.80:
        reasons.append(BlockReason.NO_SCHEDULE_PATTERN)
    if candidate is None or period < 0.80:
        reasons.append(BlockReason.PERIOD_CONFIDENCE_TOO_LOW)
    if candidate is None or phase < 0.80:
        reasons.append(BlockReason.PHASE_CONFIDENCE_TOO_LOW)
    if candidate is None or not candidate.establishment_gates_passed:
        reasons.append(BlockReason.CANDIDATE_NOT_ESTABLISHED)
    if not policy.score.prediction_usable:
        reasons.append(BlockReason.SCORER_PREDICTION_SUSPENDED)
    if policy.recent_strong_anomaly:
        reasons.append(BlockReason.RECENT_STRONG_ANOMALY)
    if policy.predicted_restart_at is None:
        reasons.append(BlockReason.PREDICTION_UNAVAILABLE)
    elif policy.predicted_restart_at <= policy.now:
        reasons.append(BlockReason.PREDICTION_STALE)
    return _reasons(reasons)


def _warning_reasons(
    policy: ConsumerPolicyInput,
    candidate: CandidateScore | None,
    schedule: float,
    period: float,
    phase: float,
    prediction_reasons: tuple[BlockReason, ...],
    key: SuppressionKey | None,
) -> tuple[BlockReason, ...]:
    reasons: list[BlockReason] = list(prediction_reasons)
    if schedule < 0.85:
        reasons.append(BlockReason.NO_SCHEDULE_PATTERN)
    if period < 0.85:
        reasons.append(BlockReason.PERIOD_CONFIDENCE_TOO_LOW)
    if phase < 0.85:
        reasons.append(BlockReason.PHASE_CONFIDENCE_TOO_LOW)
    if candidate is None or not candidate.establishment_gates_passed:
        reasons.append(BlockReason.CANDIDATE_NOT_ESTABLISHED)
    if policy.recent_covered_confirmation_count < 3:
        reasons.append(BlockReason.INSUFFICIENT_RECENT_CONFIRMATIONS)
    if not policy.server_online_healthy:
        reasons.append(BlockReason.SERVER_OFFLINE)
    if not policy.restart_alert_enabled:
        reasons.append(BlockReason.ALERT_DISABLED)
    if policy.predicted_restart_at is not None:
        target = policy.predicted_restart_at - policy.warning_lead_seconds
        if abs(policy.now - target) > policy.warning_tolerance_seconds:
            reasons.append(BlockReason.OUTSIDE_WARNING_TOLERANCE)
    if key is not None and key in policy.fired_keys:
        reasons.append(BlockReason.WARNING_ALREADY_FIRED)
    return _reasons(reasons)


def _scheduled_event_decision(
    policy: ConsumerPolicyInput,
    candidate: CandidateScore | None,
    schedule: float,
    period: float,
    phase: float,
    version_reasons: tuple[BlockReason, ...],
    transition_reasons: tuple[BlockReason, ...],
    ambiguity_reasons: tuple[BlockReason, ...],
) -> tuple[ScheduledEventClassification, tuple[BlockReason, ...], SuppressionKey | None]:
    context = policy.event_context
    if context is None:
        return ScheduledEventClassification.NOT_CLASSIFIED, (BlockReason.PREDICTION_UNAVAILABLE,), None
    event = context.event
    key = event_alert_key(
        AlertKeyKind.SCHEDULED_RECOVERY,
        server_key=policy.server_key,
        event_id=event.event_id,
        candidate_period_seconds=candidate.period_seconds if candidate else None,
        regime_generation=policy.regime_generation,
    )
    reasons: list[BlockReason] = [*version_reasons, *transition_reasons, *ambiguity_reasons]
    if event.authenticity < 0.80:
        reasons.append(BlockReason.EVENT_AUTHENTICITY_TOO_LOW)
    if schedule < 0.80:
        reasons.append(BlockReason.NO_SCHEDULE_PATTERN)
    if candidate is None or period < 0.80:
        reasons.append(BlockReason.PERIOD_CONFIDENCE_TOO_LOW)
    if candidate is None or phase < 0.80:
        reasons.append(BlockReason.PHASE_CONFIDENCE_TOO_LOW)
    if candidate is None or not candidate.establishment_gates_passed:
        reasons.append(BlockReason.CANDIDATE_NOT_ESTABLISHED)
    aligned = bool(candidate and _event_phase_aligned(event, candidate))
    if not aligned:
        reasons.append(BlockReason.EVENT_NOT_PHASE_ALIGNED)
    if context.regime_generation != policy.regime_generation:
        reasons.append(BlockReason.EVENT_REGIME_MISMATCH)
    if _event_alert_fired(
        policy,
        event.event_id,
        {
            AlertKeyKind.GENERIC_RECOVERY,
            AlertKeyKind.SCHEDULED_RECOVERY,
            AlertKeyKind.QUERY_VISIBLE_RECOVERY,
        },
    ):
        reasons.append(BlockReason.EVENT_ALREADY_ALERTED)
    blocked = _reasons(reasons)
    if not blocked:
        return ScheduledEventClassification.SCHEDULED_RESTART, (), key
    if event.authenticity >= 0.80:
        return ScheduledEventClassification.UNSCHEDULED_RESTART, blocked, key
    return ScheduledEventClassification.NOT_CLASSIFIED, blocked, key


def _generic_recovery_decision(
    policy: ConsumerPolicyInput, candidate: CandidateScore | None
) -> tuple[bool, tuple[BlockReason, ...], SuppressionKey | None]:
    context = policy.event_context
    if context is None:
        return False, (BlockReason.EVENT_NOT_OFFLINE_RECOVERY,), None
    event = context.event
    key = event_alert_key(
        AlertKeyKind.GENERIC_RECOVERY,
        server_key=policy.server_key,
        event_id=event.event_id,
        candidate_period_seconds=candidate.period_seconds if candidate else None,
        regime_generation=policy.regime_generation,
    )
    reasons = []
    if event.outcome not in {
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
    }:
        reasons.append(BlockReason.EVENT_NOT_OFFLINE_RECOVERY)
    if event.authenticity < 0.80:
        reasons.append(BlockReason.EVENT_AUTHENTICITY_TOO_LOW)
    if not context.generic_duration_ok:
        reasons.append(BlockReason.GENERIC_DURATION_REQUIREMENT_FAILED)
    if not policy.restart_alert_enabled:
        reasons.append(BlockReason.ALERT_DISABLED)
    if _event_alert_fired(policy, event.event_id, {AlertKeyKind.GENERIC_RECOVERY}):
        reasons.append(BlockReason.GENERIC_ALERT_ALREADY_FIRED)
    if _event_alert_fired(
        policy,
        event.event_id,
        {AlertKeyKind.SCHEDULED_RECOVERY, AlertKeyKind.QUERY_VISIBLE_RECOVERY},
    ):
        reasons.append(BlockReason.EVENT_ALREADY_ALERTED)
    blocked = _reasons(reasons)
    return not blocked, blocked, key


def _query_visible_decision(
    policy: ConsumerPolicyInput,
    candidate: CandidateScore | None,
    schedule: float,
    period: float,
    phase: float,
    version_reasons: tuple[BlockReason, ...],
    transition_reasons: tuple[BlockReason, ...],
    ambiguity_reasons: tuple[BlockReason, ...],
) -> tuple[bool, tuple[BlockReason, ...], SuppressionKey | None]:
    context = policy.event_context
    if context is None:
        return False, (BlockReason.EVENT_NOT_QUERY_VISIBLE,), None
    event = context.event
    key = event_alert_key(
        AlertKeyKind.QUERY_VISIBLE_RECOVERY,
        server_key=policy.server_key,
        event_id=event.event_id,
        candidate_period_seconds=candidate.period_seconds if candidate else None,
        regime_generation=policy.regime_generation,
    )
    reasons: list[BlockReason] = [*version_reasons, *transition_reasons, *ambiguity_reasons]
    if event.outcome is EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART:
        reasons.append(BlockReason.PROBABLE_QUERY_VISIBLE_EVENT)
    elif event.outcome is EventOutcome.AMBIGUOUS_DRAIN:
        reasons.append(BlockReason.AMBIGUOUS_QUERY_VISIBLE_EVENT)
    elif event.outcome is not EventOutcome.STRONG_QUERY_VISIBLE_RESTART:
        reasons.append(BlockReason.EVENT_NOT_QUERY_VISIBLE)
    if event.authenticity < 0.85:
        reasons.append(BlockReason.EVENT_AUTHENTICITY_TOO_LOW)
    if schedule < 0.80:
        reasons.append(BlockReason.NO_SCHEDULE_PATTERN)
    if candidate is None or period < 0.80:
        reasons.append(BlockReason.PERIOD_CONFIDENCE_TOO_LOW)
    if candidate is None or phase < 0.80:
        reasons.append(BlockReason.PHASE_CONFIDENCE_TOO_LOW)
    if candidate is None or not candidate.establishment_gates_passed:
        reasons.append(BlockReason.CANDIDATE_NOT_ESTABLISHED)
    if candidate is None or not _event_phase_aligned(event, candidate):
        reasons.append(BlockReason.EVENT_NOT_PHASE_ALIGNED)
    if context.regime_generation != policy.regime_generation:
        reasons.append(BlockReason.EVENT_REGIME_MISMATCH)
    if not policy.restart_alert_enabled:
        reasons.append(BlockReason.ALERT_DISABLED)
    if _event_alert_fired(policy, event.event_id, {AlertKeyKind.QUERY_VISIBLE_RECOVERY}):
        reasons.append(BlockReason.QUERY_VISIBLE_ALERT_ALREADY_FIRED)
    if _event_alert_fired(policy, event.event_id, {AlertKeyKind.GENERIC_RECOVERY}):
        reasons.append(BlockReason.GENERIC_ALERT_ALREADY_FIRED)
    if _event_alert_fired(policy, event.event_id, {AlertKeyKind.SCHEDULED_RECOVERY}):
        reasons.append(BlockReason.EVENT_ALREADY_ALERTED)
    blocked = _reasons(reasons)
    return not blocked, blocked, key


def _event_phase_aligned(event: PhysicalRestartEvent, candidate: CandidateScore) -> bool:
    at = event.canonical_phase_at
    offset = candidate.phase_offset
    if at is None or offset is None or not math.isfinite(at) or not math.isfinite(offset):
        return False
    tolerance = candidate_phase_tolerance(candidate.period_seconds) + min(
        PHASE_UNCERTAINTY_CAP, max(0.0, event.phase_uncertainty)
    )
    residual = abs(((at - offset + candidate.period_seconds / 2) % candidate.period_seconds) - candidate.period_seconds / 2)
    return residual <= tolerance


def _event_alert_fired(
    policy: ConsumerPolicyInput,
    event_id: str,
    kinds: set[AlertKeyKind],
) -> bool:
    return any(
        item.kind in kinds
        and item.server_key == policy.server_key
        and item.event_id == event_id
        for item in policy.fired_keys
    )


def _reasons(values: list[BlockReason] | tuple[BlockReason, ...]) -> tuple[BlockReason, ...]:
    return tuple(dict.fromkeys(values))


def _finite_nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _finite_positive(name: str, value: object) -> None:
    _finite_nonnegative(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
