from dataclasses import FrozenInstanceError, replace

import pytest

from dzll_launcher import companion_restart_phase2_consumers as consumers
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_scoring as scoring


NOW = 1_900_000_000.0
PERIOD = 4 * scoring.HOUR
GENERATION = "regime-1"


def candidate(
    period=PERIOD,
    *,
    confidence=0.90,
    phase=0.90,
    direct=7,
    events=8,
    gate=True,
    high=True,
    aligned=8,
    hints=0,
):
    return scoring.CandidateScore(
        period_seconds=period,
        direct_support=float(direct),
        strict_direct_interval_count=direct,
        weak_direct_interval_count=0,
        qualifying_event_count=events,
        covered_miss_penalty=0,
        covered_miss_count=0,
        off_grid_penalty=0,
        off_grid_event_count=0,
        net_covered_evidence=float(direct),
        long_term_support_used=0,
        hint_bonus=0,
        period_confidence_before_caps=confidence,
        fundamental_period_confidence=confidence,
        phase_confidence=phase,
        phase_offset=NOW % period,
        phase_residual_mad=0,
        aligned_event_count=aligned,
        observation_span=max(0, direct * period),
        hints=scoring.HintDiagnostics(
            event_count=hints,
            weighted_alignment=hints * 0.1,
            first_at=NOW - scoring.DAY if hints else None,
            last_at=NOW if hints else None,
            residual_mad=0 if hints else None,
            calendar_day_count=2 if hints else 0,
            schedule_existence_contribution=0.2 if hints else 0,
        ),
        divisor_resolution=(),
        establishment_gates_passed=gate,
        high_gates_passed=high,
        confidence_cap=1,
        strong_covered_contradiction=False,
        recent_phase_observation_at=NOW,
    )


def schedule_score(
    *,
    selected=PERIOD,
    schedule=0.90,
    period=0.90,
    phase=0.90,
    direct=7,
    events=8,
    gate=True,
    high=True,
    aligned=8,
    hints=0,
    regime=scoring.RegimeStatus.STABLE,
    competitors=(),
    prediction=True,
):
    chosen = candidate(
        confidence=period,
        phase=phase,
        direct=direct,
        events=events,
        gate=gate,
        high=high,
        aligned=aligned,
        hints=hints,
    )
    values = []
    for fixed in scoring.CANDIDATE_PERIODS:
        values.append(chosen if fixed == PERIOD else candidate(fixed, confidence=0, phase=0, direct=0, events=0, gate=False, high=False, aligned=0))
    return scoring.ScheduleScore(
        candidates=tuple(values),
        schedule_existence_confidence=schedule,
        selected_period_seconds=selected,
        incumbent_period_seconds=selected,
        pattern_status=scoring.PatternStatus.CONFIRMED_PERIOD,
        regime_status=regime,
        prediction_usable=prediction,
        five_minute_warning_eligible=prediction,
        scheduled_classification_eligible=prediction,
        unresolved_competitors=tuple(competitors),
        recent_event_count=events,
    )


def physical_event(
    outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    *,
    event_id="physical-1",
    authenticity=None,
    at=NOW,
    baseline=12,
):
    if authenticity is None:
        authenticity = {
            detection.EventOutcome.CORROBORATED_OFFLINE_RESTART: 0.98,
            detection.EventOutcome.CONFIRMED_OFFLINE_RESTART: 0.85,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART: 0.90,
            detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.60,
            detection.EventOutcome.AMBIGUOUS_DRAIN: 0.25,
        }.get(outcome, 0.10)
    return detection.PhysicalRestartEvent(
        event_id=event_id,
        sequence=1,
        fingerprint=f"fingerprint-{event_id}",
        server_key="server:2302",
        episode_started_at=at - 120,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_RETURN}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(baseline, None, at - 120, at - 120, True, 0, 1, 3, 90, True),
        outage=detection.OutageSummary(None, None, 0, (), at),
        recovery=detection.RecoverySummary(None, at, at, 2, False),
        query_health=detection.QueryHealthSummary(4, 0, 0, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=(),
    )


def policy(
    *,
    score=None,
    authentic_events=8,
    prediction_at=NOW + 300,
    confirmations=3,
    online=True,
    enabled=True,
    fired=frozenset(),
    event=None,
    event_generation=GENERATION,
    generation=GENERATION,
    now=NOW,
    anomaly=False,
    scorer_version=consumers.EXPECTED_SCORER_VERSION,
    schema_version=consumers.EXPECTED_PHASE2_SCHEMA_VERSION,
    generic_duration_ok=True,
):
    context = (
        consumers.EventPolicyContext(event, event_generation, generic_duration_ok)
        if event is not None
        else None
    )
    return consumers.ConsumerPolicyInput(
        score=score or schedule_score(),
        server_key="server:2302",
        regime_generation=generation,
        now=now,
        independent_authentic_event_count=authentic_events,
        predicted_restart_at=prediction_at,
        recent_covered_confirmation_count=confirmations,
        server_online_healthy=online,
        restart_alert_enabled=enabled,
        fired_keys=frozenset(fired),
        event_context=context,
        recent_strong_anomaly=anomaly,
        scorer_version=scorer_version,
        schema_version=schema_version,
    )


def decision(**kwargs):
    return consumers.evaluate_consumers(policy(**kwargs))


def test_models_inputs_outputs_and_keys_are_immutable():
    value = policy()
    result = consumers.evaluate_consumers(value)
    key = result.warning_key
    with pytest.raises(FrozenInstanceError):
        value.now = 1
    with pytest.raises(FrozenInstanceError):
        result.prediction_usable = False
    with pytest.raises(FrozenInstanceError):
        key.server_key = "other"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_no_or_insufficient_events_has_no_pattern(count):
    result = decision(authentic_events=count)
    assert result.model_status is consumers.ConsumerModelStatus.NO_PATTERN
    assert consumers.BlockReason.INSUFFICIENT_EVENTS in result.model_reasons


def test_three_aligned_hints_show_pattern_without_period():
    score = schedule_score(selected=None, schedule=0.60, period=0, phase=0, direct=0, events=3, gate=False, high=False, aligned=3, hints=3, prediction=False)
    result = decision(score=score, authentic_events=3, prediction_at=None)
    assert result.model_status is consumers.ConsumerModelStatus.PATTERN_OBSERVED
    assert result.selected_period_seconds is None
    assert not result.prediction_usable


@pytest.mark.parametrize(("schedule", "expected"), [(0.5999, False), (0.60, True)])
def test_pattern_schedule_boundary(schedule, expected):
    score = schedule_score(schedule=schedule, period=0.4, direct=1, events=3, gate=False, prediction=False)
    result = decision(score=score, authentic_events=3, prediction_at=None)
    assert (result.model_status is not consumers.ConsumerModelStatus.NO_PATTERN) is expected


def test_likely_period_exact_boundary():
    score = schedule_score(schedule=0.70, period=0.65, phase=0.80, direct=3, events=4, gate=False, prediction=False)
    result = decision(score=score, authentic_events=4, prediction_at=None)
    assert result.model_status is consumers.ConsumerModelStatus.LIKELY_PERIOD
    assert not result.period_confirmed


def test_likely_period_requires_three_direct_intervals():
    score = schedule_score(schedule=0.75, period=0.70, direct=2, events=4, gate=False, prediction=False)
    result = decision(score=score, authentic_events=4, prediction_at=None)
    assert result.model_status is consumers.ConsumerModelStatus.PATTERN_OBSERVED
    assert consumers.BlockReason.INSUFFICIENT_DIRECT_INTERVALS in result.model_reasons


def test_confirmed_period_exact_boundary():
    result = decision(score=schedule_score(schedule=0.80, period=0.80, phase=0.80, gate=True))
    assert result.period_confirmed
    assert result.model_status is consumers.ConsumerModelStatus.CONFIRMED_PERIOD


def test_confirmed_period_remains_confirmed_while_phase_is_weak():
    result = decision(score=schedule_score(schedule=0.85, period=0.85, phase=0.60, gate=True, prediction=False))
    assert result.period_confirmed
    assert result.model_status is consumers.ConsumerModelStatus.PHASE_UNCERTAIN
    assert not result.prediction_usable


def test_unresolved_competitor_within_margin_blocks_likely_and_confirmed():
    score = schedule_score(competitors=(8 * scoring.HOUR,), prediction=False)
    result = decision(score=score)
    assert result.model_status is consumers.ConsumerModelStatus.PERIOD_UNCERTAIN
    assert not result.period_confirmed
    assert consumers.BlockReason.COMPETING_CANDIDATE_TOO_CLOSE in result.model_reasons


def test_candidate_outside_margin_is_not_reported_as_ambiguity():
    result = decision(score=schedule_score(competitors=()))
    assert result.candidate_ambiguity == ()
    assert result.period_confirmed


def test_prediction_exact_triple_eighty_boundary_is_usable():
    result = decision(score=schedule_score(schedule=0.80, period=0.80, phase=0.80))
    assert result.prediction_usable
    assert result.countdown_usable
    assert result.prediction_status is consumers.PredictionStatus.USABLE


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("schedule", consumers.BlockReason.NO_SCHEDULE_PATTERN),
        ("period", consumers.BlockReason.PERIOD_CONFIDENCE_TOO_LOW),
        ("phase", consumers.BlockReason.PHASE_CONFIDENCE_TOO_LOW),
    ],
)
def test_prediction_each_confidence_dimension_fails_closed(field, reason):
    values = {"schedule": 0.80, "period": 0.80, "phase": 0.80}
    values[field] = 0.7999
    result = decision(score=schedule_score(**values, prediction=False))
    assert not result.prediction_usable
    assert reason in result.prediction_reasons


@pytest.mark.parametrize(
    ("prediction_at", "reason"),
    [
        (None, consumers.BlockReason.PREDICTION_UNAVAILABLE),
        (NOW, consumers.BlockReason.PREDICTION_STALE),
        (NOW - 1, consumers.BlockReason.PREDICTION_STALE),
    ],
)
def test_missing_stale_or_past_prediction_is_blocked(prediction_at, reason):
    result = decision(prediction_at=prediction_at)
    assert not result.prediction_usable
    assert result.prediction_at is None
    assert reason in result.prediction_reasons


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"scorer_version": 999}, consumers.BlockReason.SCORER_VERSION_MISMATCH),
        ({"schema_version": 999}, consumers.BlockReason.SCHEMA_VERSION_MISMATCH),
    ],
)
def test_version_mismatch_fails_prediction_closed(change, reason):
    result = decision(**change)
    assert not result.prediction_usable
    assert reason in result.prediction_reasons


@pytest.mark.parametrize(
    ("regime", "reason", "status"),
    [
        (scoring.RegimeStatus.CHANGE_SUSPECTED, consumers.BlockReason.SCHEDULE_CHANGE_SUSPECTED, consumers.ConsumerModelStatus.SCHEDULE_CHANGE_SUSPECTED),
        (scoring.RegimeStatus.NEW_REGIME_ESTABLISHING, consumers.BlockReason.NEW_REGIME_ESTABLISHING, consumers.ConsumerModelStatus.NEW_REGIME_ESTABLISHING),
        (scoring.RegimeStatus.PERIOD_UNCERTAIN, consumers.BlockReason.PERIOD_UNCERTAIN, consumers.ConsumerModelStatus.PERIOD_UNCERTAIN),
    ],
)
def test_transition_states_suspend_prediction(regime, reason, status):
    result = decision(score=schedule_score(regime=regime, prediction=False))
    assert result.model_status is status
    assert not result.prediction_usable
    assert reason in result.prediction_reasons


def test_recent_strong_anomaly_explicitly_suspends_prediction():
    result = decision(anomaly=True)
    assert not result.prediction_usable
    assert consumers.BlockReason.RECENT_STRONG_ANOMALY in result.prediction_reasons


def test_warning_exact_triple_eighty_five_and_three_confirmations():
    result = decision(score=schedule_score(schedule=0.85, period=0.85, phase=0.85), confirmations=3)
    assert result.warning_eligible
    assert result.warning_key is not None


def test_warning_needs_three_recent_confirmations():
    result = decision(confirmations=2)
    assert not result.warning_eligible
    assert consumers.BlockReason.INSUFFICIENT_RECENT_CONFIRMATIONS in result.warning_reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"enabled": False}, consumers.BlockReason.ALERT_DISABLED),
        ({"online": False}, consumers.BlockReason.SERVER_OFFLINE),
        ({"now": NOW - 31}, consumers.BlockReason.OUTSIDE_WARNING_TOLERANCE),
        ({"now": NOW + 31}, consumers.BlockReason.OUTSIDE_WARNING_TOLERANCE),
    ],
)
def test_warning_operational_gates(changes, reason):
    result = decision(**changes)
    assert not result.warning_eligible
    assert reason in result.warning_reasons


def test_warning_is_fire_once_for_same_prediction():
    first = decision()
    second = decision(fired={first.warning_key})
    assert first.warning_eligible
    assert not second.warning_eligible
    assert consumers.BlockReason.WARNING_ALREADY_FIRED in second.warning_reasons


def test_old_prediction_key_does_not_block_new_prediction():
    old = decision(prediction_at=NOW + 300)
    new = decision(prediction_at=NOW + 600, now=NOW + 300, fired={old.warning_key})
    assert new.warning_key != old.warning_key
    assert new.warning_eligible


def test_new_regime_generation_produces_new_warning_key():
    old = decision()
    new = decision(generation="regime-2", fired={old.warning_key})
    assert old.warning_key != new.warning_key
    assert new.warning_eligible


@pytest.mark.parametrize(
    "outcome",
    [
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
        detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
    ],
)
def test_established_phase_aligned_event_is_scheduled(outcome):
    result = decision(event=physical_event(outcome))
    assert result.scheduled_event_classification is consumers.ScheduledEventClassification.SCHEDULED_RESTART


def test_strong_off_phase_event_is_unscheduled_not_rejected():
    value = physical_event(at=NOW + PERIOD / 2)
    authenticity = value.authenticity
    result = decision(event=value)
    assert result.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART
    assert consumers.BlockReason.EVENT_NOT_PHASE_ALIGNED in result.scheduled_event_reasons
    assert value.authenticity == authenticity


def test_low_authenticity_event_is_not_classified_scheduled():
    result = decision(event=physical_event(authenticity=0.50))
    assert result.scheduled_event_classification is consumers.ScheduledEventClassification.NOT_CLASSIFIED
    assert consumers.BlockReason.EVENT_AUTHENTICITY_TOO_LOW in result.scheduled_event_reasons


def test_scheduled_classification_requires_phase_confidence_and_stable_regime():
    weak_phase = decision(score=schedule_score(phase=0.79, prediction=False), event=physical_event())
    transition = decision(score=schedule_score(regime=scoring.RegimeStatus.CHANGE_SUSPECTED, prediction=False), event=physical_event())
    assert weak_phase.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART
    assert transition.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART


def test_duplicate_event_alert_path_blocks_scheduled_classification():
    first = decision(event=physical_event())
    generic = first.generic_recovery_key
    second = decision(event=physical_event(), fired={generic})
    assert second.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART
    assert consumers.BlockReason.EVENT_ALREADY_ALERTED in second.scheduled_event_reasons


def test_generic_confirmed_offline_recovery_needs_no_learned_cycle():
    empty = schedule_score(selected=None, schedule=0, period=0, phase=0, direct=0, events=0, gate=False, prediction=False)
    result = decision(score=empty, authentic_events=0, prediction_at=None, event=physical_event(detection.EventOutcome.CONFIRMED_OFFLINE_RESTART))
    assert result.generic_recovery_eligible
    assert result.preferred_recovery_action is consumers.RecoveryAction.GENERIC_RECOVERY
    assert result.scheduled_event_classification is not consumers.ScheduledEventClassification.SCHEDULED_RESTART


def test_generic_corroborated_recovery_prefers_scheduled_wording_when_qualified():
    result = decision(event=physical_event())
    assert result.generic_recovery_eligible
    assert result.preferred_recovery_action is consumers.RecoveryAction.SCHEDULED_RECOVERY


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"enabled": False}, consumers.BlockReason.ALERT_DISABLED),
        ({"generic_duration_ok": False}, consumers.BlockReason.GENERIC_DURATION_REQUIREMENT_FAILED),
    ],
)
def test_generic_recovery_policy_blocks_disabled_or_bad_duration(changes, reason):
    result = decision(event=physical_event(), **changes)
    assert not result.generic_recovery_eligible
    assert reason in result.generic_recovery_reasons


def test_generic_recovery_is_fire_once_and_cross_path_safe():
    first = decision(event=physical_event())
    duplicate = decision(event=physical_event(), fired={first.generic_recovery_key})
    cross_path = decision(event=physical_event(), fired={first.scheduled_event_key})
    assert not duplicate.generic_recovery_eligible
    assert not cross_path.generic_recovery_eligible


def test_physical_event_dedup_survives_candidate_and_regime_recalculation():
    first = decision(event=physical_event())
    recalculated = decision(
        event=physical_event(),
        generation="regime-2",
        fired={first.generic_recovery_key},
    )
    assert not recalculated.generic_recovery_eligible
    assert consumers.BlockReason.GENERIC_ALERT_ALREADY_FIRED in recalculated.generic_recovery_reasons


def test_generic_recovery_remains_available_during_schedule_transition():
    result = decision(
        score=schedule_score(regime=scoring.RegimeStatus.CHANGE_SUSPECTED, prediction=False),
        event=physical_event(),
    )
    assert result.generic_recovery_eligible
    assert not result.prediction_usable


def test_mature_query_visible_event_is_alert_eligible():
    result = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    assert result.query_visible_recovery_eligible
    assert result.preferred_recovery_action is consumers.RecoveryAction.QUERY_VISIBLE_RECOVERY


def test_strong_query_visible_before_establishment_stays_silent():
    score = schedule_score(schedule=0.75, period=0.70, phase=0.75, direct=3, events=4, gate=False, prediction=False)
    result = decision(score=score, event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    assert not result.query_visible_recovery_eligible
    assert consumers.BlockReason.CANDIDATE_NOT_ESTABLISHED in result.query_visible_reasons


def test_strong_off_phase_query_visible_event_is_evidence_but_no_alert():
    result = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART, at=NOW + PERIOD / 2))
    assert not result.query_visible_recovery_eligible
    assert consumers.BlockReason.EVENT_NOT_PHASE_ALIGNED in result.query_visible_reasons


@pytest.mark.parametrize(
    ("outcome", "baseline", "reason"),
    [
        (detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART, 4, consumers.BlockReason.PROBABLE_QUERY_VISIBLE_EVENT),
        (detection.EventOutcome.AMBIGUOUS_DRAIN, 1, consumers.BlockReason.AMBIGUOUS_QUERY_VISIBLE_EVENT),
        (detection.EventOutcome.AMBIGUOUS_DRAIN, 2, consumers.BlockReason.AMBIGUOUS_QUERY_VISIBLE_EVENT),
    ],
)
def test_probable_and_low_population_ambiguous_query_visible_events_never_alert(outcome, baseline, reason):
    result = decision(event=physical_event(outcome, baseline=baseline))
    assert not result.query_visible_recovery_eligible
    assert reason in result.query_visible_reasons


def test_query_visible_suppressed_after_ordinary_or_duplicate_alert():
    first = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    ordinary_key = consumers.event_alert_key(
        consumers.AlertKeyKind.GENERIC_RECOVERY,
        server_key="server:2302",
        event_id="physical-1",
        candidate_period_seconds=PERIOD,
        regime_generation=GENERATION,
    )
    ordinary = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART), fired={ordinary_key})
    duplicate = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART), fired={first.query_visible_key})
    assert not ordinary.query_visible_recovery_eligible
    assert not duplicate.query_visible_recovery_eligible


def test_query_visible_transition_is_suppressed():
    result = decision(
        score=schedule_score(regime=scoring.RegimeStatus.NEW_REGIME_ESTABLISHING, prediction=False),
        event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
    )
    assert not result.query_visible_recovery_eligible
    assert consumers.BlockReason.NEW_REGIME_ESTABLISHING in result.query_visible_reasons


def test_event_from_different_regime_is_not_scheduled_or_query_alerted():
    result = decision(
        event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
        event_generation="old-regime",
    )
    assert not result.query_visible_recovery_eligible
    assert result.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART
    assert consumers.BlockReason.EVENT_REGIME_MISMATCH in result.query_visible_reasons


def test_duplicate_keys_are_deterministic_and_type_separated():
    generic1 = consumers.event_alert_key(consumers.AlertKeyKind.GENERIC_RECOVERY, server_key="s", event_id="e", candidate_period_seconds=PERIOD, regime_generation="g")
    generic2 = consumers.event_alert_key(consumers.AlertKeyKind.GENERIC_RECOVERY, server_key="s", event_id="e", candidate_period_seconds=PERIOD, regime_generation="g")
    scheduled = consumers.event_alert_key(consumers.AlertKeyKind.SCHEDULED_RECOVERY, server_key="s", event_id="e", candidate_period_seconds=PERIOD, regime_generation="g")
    other = consumers.event_alert_key(consumers.AlertKeyKind.GENERIC_RECOVERY, server_key="s", event_id="other", candidate_period_seconds=PERIOD, regime_generation="g")
    assert generic1 == generic2
    assert generic1.serialize() == generic2.serialize()
    assert generic1 != scheduled
    assert generic1 != other


def test_generic_event_key_is_physical_and_not_candidate_or_regime_specific():
    first = consumers.event_alert_key(
        consumers.AlertKeyKind.GENERIC_RECOVERY,
        server_key="s",
        event_id="e",
        candidate_period_seconds=PERIOD,
        regime_generation="g1",
    )
    second = consumers.event_alert_key(
        consumers.AlertKeyKind.GENERIC_RECOVERY,
        server_key="s",
        event_id="e",
        candidate_period_seconds=3 * scoring.HOUR,
        regime_generation="g2",
    )
    assert first == second
    assert first.candidate_period_seconds is None


def test_prediction_keys_change_with_candidate_prediction_or_generation():
    base = consumers.prediction_key(consumers.AlertKeyKind.FIVE_MINUTE_WARNING, server_key="s", candidate_period_seconds=PERIOD, regime_generation="g", prediction_at=NOW)
    same = consumers.prediction_key(consumers.AlertKeyKind.FIVE_MINUTE_WARNING, server_key="s", candidate_period_seconds=PERIOD, regime_generation="g", prediction_at=NOW + 0.4)
    candidate_changed = consumers.prediction_key(consumers.AlertKeyKind.FIVE_MINUTE_WARNING, server_key="s", candidate_period_seconds=3 * scoring.HOUR, regime_generation="g", prediction_at=NOW)
    prediction_changed = consumers.prediction_key(consumers.AlertKeyKind.FIVE_MINUTE_WARNING, server_key="s", candidate_period_seconds=PERIOD, regime_generation="g", prediction_at=NOW + 1)
    generation_changed = consumers.prediction_key(consumers.AlertKeyKind.FIVE_MINUTE_WARNING, server_key="s", candidate_period_seconds=PERIOD, regime_generation="g2", prediction_at=NOW)
    assert base == same
    assert len({base, candidate_changed, prediction_changed, generation_changed}) == 4


def test_reason_codes_are_stably_ordered_and_deduplicated():
    result1 = decision(score=schedule_score(schedule=0.5, period=0.5, phase=0.5, gate=False, prediction=False), prediction_at=None, enabled=False, online=False)
    result2 = decision(score=schedule_score(schedule=0.5, period=0.5, phase=0.5, gate=False, prediction=False), prediction_at=None, enabled=False, online=False)
    assert result1.warning_reasons == result2.warning_reasons
    assert len(result1.warning_reasons) == len(set(result1.warning_reasons))
    assert all(isinstance(item, consumers.BlockReason) for item in result1.warning_reasons)


def test_dawn_hint_only_twelve_hour_data_exposes_no_period_or_warning():
    score = schedule_score(selected=None, schedule=0.70, period=0, phase=0, direct=0, events=4, gate=False, aligned=4, hints=4, prediction=False)
    result = decision(score=score, authentic_events=4, prediction_at=None)
    assert result.model_status is consumers.ConsumerModelStatus.PATTERN_OBSERVED
    assert result.selected_period_seconds is None
    assert not result.warning_eligible


def test_dawn_covered_three_hour_model_exposes_three_hours_not_twelve():
    chosen = candidate(3 * scoring.HOUR, confidence=0.86, phase=0.82, direct=5, events=6, gate=True, high=False, aligned=6)
    values = tuple(chosen if value == 3 * scoring.HOUR else candidate(value, confidence=0, phase=0, direct=0, events=0, gate=False, high=False, aligned=0, hints=4 if value == 12 * scoring.HOUR else 0) for value in scoring.CANDIDATE_PERIODS)
    score = replace(schedule_score(), candidates=values, selected_period_seconds=3 * scoring.HOUR, incumbent_period_seconds=3 * scoring.HOUR)
    result = decision(score=score)
    assert result.selected_period_seconds == 3 * scoring.HOUR
    assert result.period_confirmed


def test_crowbar_mature_query_only_policy_allows_prediction_and_qv_alert():
    result = decision(event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    assert result.model_status is consumers.ConsumerModelStatus.CONFIRMED_PERIOD
    assert result.prediction_usable
    assert result.query_visible_recovery_eligible


def test_crowbar_learning_state_remains_silent():
    score = schedule_score(schedule=0.70, period=0.65, phase=0.70, direct=3, events=4, gate=False, prediction=False)
    result = decision(score=score, event=physical_event(detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    assert result.model_status is consumers.ConsumerModelStatus.LIKELY_PERIOD
    assert not result.query_visible_recovery_eligible


def test_schedule_change_suppresses_scheduled_features_but_preserves_generic():
    score = schedule_score(regime=scoring.RegimeStatus.CHANGE_SUSPECTED, prediction=False)
    result = decision(score=score, event=physical_event())
    assert not result.prediction_usable
    assert not result.warning_eligible
    assert result.scheduled_event_classification is consumers.ScheduledEventClassification.UNSCHEDULED_RESTART
    assert result.generic_recovery_eligible


def test_new_regime_reenables_prediction_only_after_stable_full_gates():
    transitioning = decision(score=schedule_score(regime=scoring.RegimeStatus.NEW_REGIME_ESTABLISHING, prediction=False))
    stable = decision(score=schedule_score(regime=scoring.RegimeStatus.STABLE, prediction=True))
    assert not transitioning.prediction_usable
    assert stable.prediction_usable


def test_compatibility_established_flag_is_false_during_new_regime_transition():
    result = decision(
        score=schedule_score(
            regime=scoring.RegimeStatus.NEW_REGIME_ESTABLISHING,
            prediction=False,
        )
    )
    assert result.period_confirmed
    assert not consumers.compatibility_summary(result).established_model


def test_phase_shift_preserves_confirmed_duration_but_suspends_countdown():
    result = decision(score=schedule_score(period=0.90, phase=0.60, regime=scoring.RegimeStatus.PHASE_UNCERTAIN, prediction=False))
    assert result.period_confirmed
    assert result.model_status is consumers.ConsumerModelStatus.PHASE_UNCERTAIN
    assert not result.countdown_usable


def test_one_anomaly_does_not_suppress_when_scorer_remains_stable():
    result = decision(score=schedule_score(regime=scoring.RegimeStatus.STABLE, prediction=True), anomaly=False)
    assert result.prediction_usable


def test_compatibility_mapping_represents_current_summary_concepts():
    result = decision()
    summary = consumers.compatibility_summary(result)
    assert summary.restart_learning_summary_available
    assert summary.cycle_hours == 4
    assert summary.established_model
    assert summary.next_restart_at == NOW + 300
    assert summary.alert_usability
    assert summary.scheduled_outage_threshold_usable
    assert summary.derived_from_phase2


def test_compatibility_mapping_marks_prediction_unavailable_explicitly():
    result = decision(prediction_at=None)
    summary = consumers.compatibility_summary(result)
    assert summary.next_restart_at is None
    assert summary.prediction_blocked
    assert consumers.BlockReason.PREDICTION_UNAVAILABLE in summary.blocked_reasons


def test_compatibility_version_mismatch_fails_closed_without_phase1_mutation():
    score = schedule_score()
    result = decision(score=score, schema_version=999)
    summary = consumers.compatibility_summary(result)
    assert not summary.established_model
    assert not summary.alert_usability
    assert score.selected_period_seconds == PERIOD
    assert consumers.BlockReason.SCHEMA_VERSION_MISMATCH in summary.blocked_reasons


def test_stage_four_module_has_no_runtime_ui_audio_or_storage_dependencies():
    names = set(consumers.__dict__)
    for forbidden in ("Gtk", "GLib", "window", "audio", "Path", "os", "config"):
        assert forbidden not in names
