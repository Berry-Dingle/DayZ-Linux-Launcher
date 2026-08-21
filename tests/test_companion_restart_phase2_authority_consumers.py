from __future__ import annotations

import hashlib
import tempfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_consumers as consumers3
from dzll_launcher.companion_restart_phase2_runtime import (
    Phase2RestartRuntime,
    RecoveryAlertWindowStatus,
)
from dzll_launcher.server_companion_ui import restart_learning_presentation


BASE = 1_900_000_000.0
SERVER = "consumer.example:2302"
HOUR = 3600


def _event(sequence, at, *, server=SERVER):
    return detection.PhysicalRestartEvent(
        event_id=f"event-{server}-{sequence}-{int(at)}",
        sequence=sequence,
        fingerprint=f"fingerprint-{server}-{sequence}-{int(at)}",
        server_key=server,
        episode_started_at=at - 90,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        authenticity=0.85,
        schedule_weight_suggestion=0.85,
        drain=detection.DrainSummary(None, None, None, None, False, None, None, 0, 0, False),
        outage=detection.OutageSummary(at - 70, at - 60, 3, (detection.InfoStatus.TIMEOUT,), at),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("consumer_fixture",),
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        continuity_chain_id="chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _relationship(left, right, period, *, chain="chain"):
    tolerance = scoring.candidate_phase_tolerance(period)
    residual = right.canonical_phase_at - left.canonical_phase_at - period
    return continuity.IntervalRelationship(
        schema_version=continuity.CONTINUITY_SCHEMA_VERSION,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
        relationship_id=f"relationship-{period}-{left.event_id}-{right.event_id}",
        server_key=left.server_key,
        candidate_period_seconds=period,
        left_event_id=left.event_id,
        left_event_sequence=left.sequence,
        right_event_id=right.event_id,
        right_event_sequence=right.sequence,
        left_at=left.canonical_phase_at,
        right_at=right.canonical_phase_at,
        observed_interval_seconds=right.canonical_phase_at - left.canonical_phase_at,
        signed_residual_seconds=residual,
        tolerance_seconds=tolerance,
        relationship_kind=continuity.RelationshipKind.DIRECT,
        multiplier=1,
        intervening_event_ids=(),
        coverage_quality=continuity.CoverageQuality.COMPLETE,
        observed_ratio=0.98,
        largest_unexplained_gap=0,
        monitoring_session_id="monitor",
        continuity_chain_id=chain,
        continuity_qualified=True,
        normal_support_diagnostic=0.8,
        high_authority_eligible=True,
        high_quality_diagnostic=1.0,
        reason_codes=("consumer_fixture_relationship",),
    )


def _empty_score():
    return scoring.RestartScheduleScorer().score(
        (), scoring.CoverageTimeline(()), now=BASE
    )


def _authority(event_count, period=3 * HOUR, *, server=SERVER, start=BASE):
    events = tuple(_event(index + 1, start + index * period, server=server) for index in range(event_count))
    relationships = tuple(
        _relationship(events[index], events[index + 1], period)
        for index in range(max(0, event_count - 1))
    )
    streaks = continuity.build_cadence_streaks(relationships)
    return authority.evaluate_authority(
        server_key=server,
        normal_score=_empty_score(),
        relationships=relationships,
        streaks=streaks,
        events=events,
    )


def _policy(decision, *, now=BASE + 100, **kwargs):
    return consumers4.AuthorityConsumerPolicyInput(
        authority_decision=decision,
        now=now,
        **kwargs,
    )


def _schema3_decision():
    return consumers3.evaluate_consumers(
        consumers3.ConsumerPolicyInput(
            score=_empty_score(),
            server_key=SERVER,
            regime_generation="schema3-regime",
            now=BASE,
            independent_authentic_event_count=0,
        )
    )


def _transition(h3, challenger, *, state):
    incumbent = h3.selected_shadow_regime
    selected = challenger.selected_shadow_regime
    assert incumbent and selected
    selected = replace(selected, status=authority.RegimeRecordStatus.PROVISIONAL)
    return replace(
        challenger,
        decision_id=f"transition-{state.value}-{challenger.decision_id}",
        selected_shadow_regime=selected,
        incumbent_shadow_regime=incumbent,
        regimes=(incumbent, selected),
        candidates=tuple((*h3.candidates, *challenger.candidates)),
        state=state,
        prediction_usable=False,
        countdown_safe=False,
        suspicion_level=2,
    )


def _normal_established(period=4 * HOUR):
    source = _authority(2, period)
    source_candidate = next(
        item
        for item in source.candidates
        if item.candidate_period_seconds == period and item.high_relationship_ids
    )
    candidate = replace(
        source_candidate,
        high_authority=0.0,
        high_phase_authority=0.0,
        combined_confidence=0.05,
        h1_gate=False,
        h2_gate=False,
        h3_gate=False,
        h4_gate=False,
        high_relationship_ids=(),
        maximal_streak_ids=(),
    )
    regime = authority._regime_from_candidate(
        candidate,
        status=authority.RegimeRecordStatus.ESTABLISHED,
        origin=authority.AuthorityOrigin.NORMAL,
    )
    return replace(
        source,
        decision_id=f"normal-established-{source.decision_id}",
        state=authority.RegimeState.ESTABLISHED,
        selected_shadow_regime=regime,
        incumbent_shadow_regime=regime,
        regimes=(regime,),
        candidates=(candidate,),
        cycle_visible=True,
        prediction_usable=True,
        countdown_safe=True,
    )


def _normal_prediction_evidence(
    decision,
    *,
    now=BASE + 100,
    schedule=0.95,
    fundamental=0.650058,
    phase=0.95,
    established=True,
    direct=3,
    competitor=False,
    harmonic=False,
    stable=True,
    latest_phase_at=None,
    predicted_at=None,
    anomaly=False,
    selected_period=None,
    incumbent_period=None,
):
    regime = decision.selected_shadow_regime
    assert regime is not None
    period = regime.candidate_period_seconds
    return consumers4.NormalPredictionEvidence(
        selected_period_seconds=(period if selected_period is None else selected_period),
        incumbent_period_seconds=(period if incumbent_period is None else incumbent_period),
        schedule_existence_confidence=schedule,
        fundamental_period_confidence=fundamental,
        phase_confidence=phase,
        candidate_established=established,
        strict_direct_relationship_count=direct,
        unresolved_competitor=competitor,
        unresolved_divisor_or_harmonic=harmonic,
        regime_stable=stable,
        latest_aligned_phase_at=(
            now - 60 if latest_phase_at is None else latest_phase_at
        ),
        raw_predicted_occurrence_at=(
            consumers4.next_phase_occurrence(
                period_seconds=period,
                phase_offset=regime.phase_offset,
                now=now,
            )
            if predicted_at is None
            else predicted_at
        ),
        recent_strong_anomaly=anomaly,
    )


def _new_regime(h3, challenger):
    selected = challenger.selected_shadow_regime
    assert selected
    return replace(
        challenger,
        decision_id=f"new-regime-{challenger.decision_id}",
        selected_shadow_regime=selected,
        incumbent_shadow_regime=selected,
        state=authority.RegimeState.NEW_REGIME_ESTABLISHED,
        prediction_usable=True,
        countdown_safe=True,
    )


def test_frozen_consumer_objects():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(4)))
    with pytest.raises(FrozenInstanceError):
        value.visible_cycle = False
    evidence = _normal_prediction_evidence(_normal_established())
    with pytest.raises(FrozenInstanceError):
        evidence.phase_confidence = 0.0


def test_unknown_has_no_schedule_consumers():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(0)))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.NONE
    assert not value.visible_cycle and not value.countdown_visible
    assert not value.scheduled_warning_eligible and not value.outage_relaxation_eligible


def test_normal_learning_can_preserve_pattern_only_without_cycle():
    value = consumers4.evaluate_authority_consumers(
        _policy(_authority(2), normal_pattern_supported=True, normal_pattern_confidence=0.78)
    )
    assert value.presentation_state is consumers4.AuthorityPresentationKey.PATTERN_ONLY
    assert value.confidence_display_value == 0.78
    assert value.cycle_period_seconds is None and not value.countdown_visible


def test_normal_established_cycle_confidence_alone_does_not_enable_prediction():
    value = consumers4.evaluate_authority_consumers(
        _policy(
            _normal_established(),
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
        )
    )
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)
    presentation = restart_learning_presentation(summary)

    assert value.presentation_state is consumers4.AuthorityPresentationKey.LIKELY_CYCLE
    assert value.cycle_period_seconds == 4 * HOUR
    assert value.confidence_display_value == 0.95
    assert "normal_schedule_confidence_display" in value.reason_codes
    assert not value.countdown_visible
    assert not value.countdown_safe
    assert value.next_expected_restart_at is None
    assert summary["confidence_percent"] == 95
    assert summary["confidence_kind"] == "schedule"
    assert summary["confidence_label"] == "Confidence:"
    assert not summary["prediction_usable"]
    assert not summary["countdown_visible"]
    assert summary["cycle_text"] == "Likely: Every 4 hours"
    assert presentation["confidence_percent"] == 95
    assert presentation["confidence_percent"] != 5


def test_crowbar_established_normal_schedule_exposes_prediction_and_warning():
    decision = _normal_established()
    regime = decision.selected_shadow_regime
    now = BASE + 100
    evidence = _normal_prediction_evidence(decision, now=now)
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            restart_alert_enabled=True,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
        )
    )

    assert regime is not None
    assert value.next_expected_restart_at == evidence.raw_predicted_occurrence_at
    assert value.countdown_visible and value.countdown_safe
    assert value.scheduled_warning_eligible
    assert value.scheduled_warning_at == value.next_expected_restart_at - 300
    assert "safe_established_normal_schedule_available" in value.reason_codes
    assert not any(item.h1_gate or item.h2_gate or item.h3_gate or item.h4_gate for item in decision.candidates)
    assert not value.outage_relaxation_eligible


def test_established_normal_prediction_uses_learned_phase_not_wall_clock_rounding():
    decision = _normal_established()
    regime = decision.selected_shadow_regime
    assert regime is not None
    regime = replace(regime, phase_offset=(regime.phase_offset + 137.25) % (4 * HOUR))
    decision = replace(
        decision,
        selected_shadow_regime=regime,
        incumbent_shadow_regime=regime,
        regimes=(regime,),
    )
    now = BASE + 123
    learned = consumers4.next_phase_occurrence(
        period_seconds=4 * HOUR,
        phase_offset=regime.phase_offset,
        now=now,
    )
    evidence = _normal_prediction_evidence(
        decision, now=now, predicted_at=learned
    )
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
        )
    )

    assert value.next_expected_restart_at == learned
    assert (learned - regime.phase_offset) % (4 * HOUR) == 0
    assert learned % 300 != 0


@pytest.mark.parametrize(
    "override",
    [
        {"established": False},
        {"phase": 0.849999},
        {"direct": 2},
        {"competitor": True},
        {"harmonic": True},
        {"stable": False},
        {"fundamental": 0.649999},
    ],
)
def test_normal_schedule_safeguards_each_block_countdown_and_warning(override):
    decision = _normal_established()
    now = BASE + 100
    evidence = _normal_prediction_evidence(decision, now=now, **override)
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_normal_schedule_confidence_below_maximum_does_not_enter_new_path():
    decision = _normal_established()
    now = BASE + 100
    evidence = _normal_prediction_evidence(decision, now=now, schedule=0.949999)
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.949999,
            normal_prediction_evidence=evidence,
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


@pytest.mark.parametrize("inactive_days", [7, 31])
def test_safe_established_normal_schedule_retains_prediction_through_inactivity(
    inactive_days,
):
    decision = _normal_established()
    now = BASE + inactive_days * 24 * HOUR
    evidence = _normal_prediction_evidence(
        decision,
        now=now,
        latest_phase_at=BASE,
    )
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            restart_alert_enabled=True,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
        )
    )

    assert value.next_expected_restart_at == evidence.raw_predicted_occurrence_at
    assert value.next_expected_restart_at > now
    assert value.countdown_visible and value.countdown_safe
    assert value.scheduled_warning_eligible


@pytest.mark.parametrize(
    "state,suspicion",
    [
        (authority.RegimeState.CHANGE_SUSPECTED, 0),
        (authority.RegimeState.CHALLENGER_ACCUMULATING, 0),
        (authority.RegimeState.NEW_REGIME_PROVISIONAL, 0),
        (authority.RegimeState.TRANSITION_CONFIRMED, 0),
        (authority.RegimeState.ESTABLISHED, 1),
    ],
)
def test_normal_schedule_authority_state_invalidations_suppress_prediction(
    state, suspicion
):
    decision = replace(
        _normal_established(),
        state=state,
        suspicion_level=suspicion,
    )
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision, now=now
            ),
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


@pytest.mark.parametrize(
    "evidence_override",
    [
        {"anomaly": True},
        {"selected_period": 3 * HOUR},
        {"incumbent_period": 3 * HOUR},
        {"predicted_at": BASE + 100},
    ],
)
def test_normal_schedule_runtime_evidence_invalidations_suppress_prediction(
    evidence_override
):
    decision = _normal_established()
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision, now=now, **evidence_override
            ),
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_future_normal_phase_timestamp_remains_invalid():
    decision = _normal_established()
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            restart_alert_enabled=True,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision,
                now=now,
                latest_phase_at=now + 1,
            ),
        )
    )

    assert value.next_expected_restart_at is None
    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_normal_schedule_selected_and_incumbent_regimes_must_match():
    decision = _normal_established()
    distinct_incumbent = replace(
        decision.incumbent_shadow_regime,
        regime_id="distinct-normal-incumbent",
    )
    decision = replace(decision, incumbent_shadow_regime=distinct_incumbent)
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision, now=now
            ),
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


@pytest.mark.parametrize(
    "authority_override",
    [
        {"prediction_usable": False},
        {"countdown_safe": False},
    ],
)
def test_normal_schedule_respects_authority_prediction_safety_flags(
    authority_override
):
    decision = replace(_normal_established(), **authority_override)
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision, now=now
            ),
        )
    )

    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_normal_change_suspected_cycle_uses_schedule_confidence_not_combined_authority():
    live_shape = replace(
        _normal_established(),
        decision_id="crowbar-live-change-suspected",
        state=authority.RegimeState.CHANGE_SUSPECTED,
        combined_confidence=0.045009,
        prediction_usable=False,
        countdown_safe=False,
    )
    value = consumers4.evaluate_authority_consumers(
        _policy(
            live_shape,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
        )
    )
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)
    presentation = restart_learning_presentation(summary)

    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.confidence_display_value == 0.95
    assert "normal_schedule_confidence_display" in value.reason_codes
    assert not value.countdown_visible
    assert not value.countdown_safe
    assert value.next_expected_restart_at is None
    assert summary["confidence_kind"] == "schedule"
    assert summary["confidence_label"] == "Confidence:"
    assert summary["confidence_percent"] == 95
    assert presentation["confidence_percent"] == 95
    assert presentation["confidence_label"] == "Confidence:"


def test_unknown_authority_summary_still_renders_no_cycle_or_confidence():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(0)))
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)
    presentation = restart_learning_presentation(summary)

    assert summary["cycle_text"] == "--"
    assert not summary["confidence_visible"]
    assert presentation["cycle_text"] == "--"
    assert not presentation["confidence_visible"]


def test_h1_does_not_show_likely_even_at_fifty_percent():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(2)))
    assert value.confidence_display_value == 0.5
    assert value.presentation_state is consumers4.AuthorityPresentationKey.NONE


def test_h2_is_likely_at_seventy_nine_percent_without_countdown():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(3)))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.LIKELY_CYCLE
    assert value.confidence_display_value == 0.79
    assert value.visible_cycle and not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_h3_is_confirmed_with_safe_countdown():
    decision = _authority(4)
    value = consumers4.evaluate_authority_consumers(_policy(decision))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert value.next_expected_restart_at > BASE + 100


def test_offline_h3_retains_prediction_metadata_but_disables_warning():
    value = consumers4.evaluate_authority_consumers(
        _policy(
            _authority(4),
            server_online_healthy=False,
            restart_alert_enabled=True,
            physical_recovery_event_id="physical-recovery",
            physical_recovery_eligible=True,
        )
    )
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)

    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.countdown_safe and value.countdown_visible
    assert value.next_expected_restart_at > BASE + 100
    assert not value.scheduled_warning_eligible
    assert value.generic_recovery_alert_eligible
    assert summary["countdown_safe"] and summary["countdown_visible"]
    assert summary["prediction_usable"]
    assert summary["presentation_key"] == "confirmed_cycle"
    assert summary["reason_codes"] == value.reason_codes


@pytest.mark.parametrize(
    "decision,expected_presentation",
    [
        (_authority(3), consumers4.AuthorityPresentationKey.LIKELY_CYCLE),
        (
            _transition(
                _authority(4),
                _authority(3, 4 * HOUR, start=BASE + 1234),
                state=authority.RegimeState.TRANSITION_CONFIRMED,
            ),
            consumers4.AuthorityPresentationKey.LIKELY_NEW_CYCLE,
        ),
        (
            replace(
                _authority(4),
                state=authority.RegimeState.CHALLENGER_ACCUMULATING,
                suspicion_level=2,
                prediction_usable=False,
                countdown_safe=False,
            ),
            consumers4.AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED,
        ),
    ],
)
def test_offline_unsafe_authority_never_gains_countdown(
    decision, expected_presentation
):
    value = consumers4.evaluate_authority_consumers(
        _policy(decision, server_online_healthy=False, restart_alert_enabled=True)
    )
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)

    assert value.presentation_state is expected_presentation
    assert not value.countdown_safe
    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible
    assert not summary["prediction_usable"]
    assert summary["countdown_text"] == "--"


def test_offline_newly_established_h3_uses_only_new_regime_prediction():
    decision = _new_regime(
        _authority(4), _authority(4, 4 * HOUR, start=BASE + 1234)
    )
    value = consumers4.evaluate_authority_consumers(
        _policy(decision, server_online_healthy=False, restart_alert_enabled=True)
    )

    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE
    assert value.cycle_period_seconds == 4 * HOUR
    assert value.countdown_safe and value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_h4_has_no_stronger_wording_than_h3():
    h3 = _authority(4)
    candidate = h3.candidates[0]
    h4_candidate = replace(candidate, h4_gate=True, high_authority=0.97, combined_confidence=0.97)
    regime = replace(h3.selected_shadow_regime, authority_gate=authority.AuthorityGate.H4, high_authority=0.97, combined_confidence=0.97)
    h4 = replace(h3, candidates=(h4_candidate,), selected_shadow_regime=regime, incumbent_shadow_regime=regime)
    value = consumers4.evaluate_authority_consumers(_policy(h4))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert "h4" not in consumers4.authority_consumer_summary(value, now=BASE)["cycle_text"].lower()


def test_level_one_suspicion_retains_confirmed_and_countdown():
    suspect = replace(_authority(4), state=authority.RegimeState.CHANGE_SUSPECTED, suspicion_level=1)
    value = consumers4.evaluate_authority_consumers(_policy(suspect))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.countdown_visible


def test_unsafe_challenger_suspends_countdown_and_warning():
    suspect = replace(
        _authority(4),
        state=authority.RegimeState.CHALLENGER_ACCUMULATING,
        suspicion_level=2,
        prediction_usable=False,
        countdown_safe=False,
    )
    value = consumers4.evaluate_authority_consumers(_policy(suspect))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.SCHEDULE_CHANGE_SUSPECTED
    assert value.prediction_suspended and not value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_weak_challenger_cannot_show_new_cycle():
    suspect = replace(_authority(4), state=authority.RegimeState.CHALLENGER_ACCUMULATING, suspicion_level=1)
    value = consumers4.evaluate_authority_consumers(_policy(suspect))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE


@pytest.mark.parametrize("state", [authority.RegimeState.TRANSITION_CONFIRMED, authority.RegimeState.NEW_REGIME_PROVISIONAL])
def test_h2_challenger_is_likely_new_and_suspends_old_prediction(state):
    value = consumers4.evaluate_authority_consumers(
        _policy(_transition(_authority(4), _authority(3, 4 * HOUR, start=BASE + 1234), state=state))
    )
    assert value.presentation_state is consumers4.AuthorityPresentationKey.LIKELY_NEW_CYCLE
    assert value.cycle_period_seconds == 4 * HOUR
    assert not value.countdown_visible and not value.scheduled_warning_eligible


def test_h3_challenger_is_confirmed_new_and_resumes_prediction():
    value = consumers4.evaluate_authority_consumers(
        _policy(_new_regime(_authority(4), _authority(4, 4 * HOUR, start=BASE + 1234)))
    )
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE
    assert value.cycle_period_seconds == 4 * HOUR and value.countdown_visible


def test_provisional_rollback_restores_incumbent():
    h3 = _authority(4)
    rollback = replace(h3, decision_id="rollback", state=authority.RegimeState.ESTABLISHED)
    value = consumers4.evaluate_authority_consumers(_policy(rollback))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.cycle_period_seconds == 3 * HOUR and value.countdown_visible


@pytest.mark.parametrize("new_state,key", [
    (authority.RegimeState.TRANSITION_CONFIRMED, consumers4.AuthorityPresentationKey.LIKELY_NEW_CYCLE),
    (authority.RegimeState.NEW_REGIME_ESTABLISHED, consumers4.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE),
])
def test_same_period_phase_transition_mapping(new_state, key):
    base = _authority(4)
    shifted = _authority(3 if new_state is authority.RegimeState.TRANSITION_CONFIRMED else 4, start=BASE + 40 * 60)
    value_decision = _transition(base, shifted, state=new_state) if new_state is authority.RegimeState.TRANSITION_CONFIRMED else _new_regime(base, shifted)
    value = consumers4.evaluate_authority_consumers(_policy(value_decision))
    assert value.presentation_state is key
    assert value.cycle_period_seconds == 3 * HOUR


def test_unresolved_harmonic_blocks_prediction():
    base = _authority(4)
    candidate = replace(base.candidates[0], harmonic_blockers=("unresolved_divisor",))
    value = consumers4.evaluate_authority_consumers(
        _policy(
            replace(base, candidates=(candidate,)),
            server_online_healthy=False,
        )
    )
    assert not value.phase_available and not value.countdown_visible
    assert not value.countdown_safe


@pytest.mark.parametrize("state", [authority.RegimeState.ESTABLISHED, authority.RegimeState.CHANGE_SUSPECTED])
def test_ambiguous_and_unknown_windows_do_not_change_safe_mapping(state):
    value = consumers4.evaluate_authority_consumers(_policy(replace(_authority(4), state=state)))
    assert value.visible_cycle and value.countdown_visible


def test_aligned_hit_clear_and_month_inactivity_leave_presentation_unchanged():
    stable = _authority(4)
    earlier = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 100))
    later = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 31 * 24 * HOUR))
    assert earlier.presentation_state == later.presentation_state
    assert earlier.confidence_display_value == later.confidence_display_value


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_next_occurrence_strict_boundary(offset):
    phase = 100.0
    now = phase + 3 * HOUR + offset
    value = consumers4.next_phase_occurrence(period_seconds=3 * HOUR, phase_offset=phase, now=now)
    assert value > now
    assert (value - phase) % (3 * HOUR) == 0


@pytest.mark.parametrize("offset", [0, 1])
def test_offline_h3_next_occurrence_advances_strictly_after_boundary(offset):
    decision = _authority(4)
    regime = decision.selected_shadow_regime
    now = regime.phase_offset + 3 * HOUR + offset
    value = consumers4.evaluate_authority_consumers(
        _policy(decision, now=now, server_online_healthy=False)
    )

    assert value.next_expected_restart_at > now
    assert value.countdown_safe and value.countdown_visible
    assert not value.scheduled_warning_eligible


def test_next_occurrence_crosses_cycle_without_confidence_change():
    stable = _authority(4)
    a = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 1))
    b = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 3 * HOUR + 1))
    assert b.next_expected_restart_at - a.next_expected_restart_at == 3 * HOUR
    assert a.confidence_display_value == b.confidence_display_value


def test_safe_h3_warning_due_and_one_shot_suppression():
    stable = _authority(4)
    first = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 3 * HOUR - 300, restart_alert_enabled=True))
    assert first.scheduled_pre_restart_alert_eligible
    again = consumers4.evaluate_authority_consumers(
        _policy(stable, now=BASE + 3 * HOUR - 300, restart_alert_enabled=True, fired_keys=frozenset({first.scheduled_warning_suppression_key}))
    )
    assert not again.scheduled_pre_restart_alert_eligible


def test_safe_normal_warning_due_uses_existing_deduplication():
    decision = _normal_established()
    regime = decision.selected_shadow_regime
    assert regime is not None
    occurrence = consumers4.next_phase_occurrence(
        period_seconds=regime.candidate_period_seconds,
        phase_offset=regime.phase_offset,
        now=BASE + 100,
    )
    due = occurrence - 300
    evidence = _normal_prediction_evidence(
        decision,
        now=due,
        latest_phase_at=occurrence - regime.candidate_period_seconds,
        predicted_at=occurrence,
    )
    first = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=due,
            restart_alert_enabled=True,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
        )
    )
    again = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=due,
            restart_alert_enabled=True,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=evidence,
            fired_keys=frozenset({first.scheduled_warning_suppression_key}),
        )
    )

    assert first.next_expected_restart_at == occurrence
    assert first.scheduled_pre_restart_alert_eligible
    assert not again.scheduled_pre_restart_alert_eligible
    assert again.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key


def test_safe_normal_schedule_never_enables_outage_relaxation_or_high_gates():
    decision = _normal_established()
    regime = decision.selected_shadow_regime
    assert regime is not None
    now = BASE + 100
    value = consumers4.evaluate_authority_consumers(
        _policy(
            decision,
            now=now,
            observed_outage_at=regime.phase_offset,
            normal_pattern_supported=True,
            normal_pattern_confidence=0.95,
            normal_prediction_evidence=_normal_prediction_evidence(
                decision, now=now
            ),
        )
    )

    assert value.countdown_visible and value.scheduled_warning_eligible
    assert not value.outage_relaxation_eligible
    assert value.restart_classification_policy is consumers4.AuthorityRestartClassification.UNSCHEDULED_RESTART
    assert all(
        not candidate.h1_gate
        and not candidate.h2_gate
        and not candidate.h3_gate
        and not candidate.h4_gate
        for candidate in decision.candidates
    )


def test_warning_key_rotates_with_regime_and_old_key_does_not_block_new():
    old = _authority(4)
    old_value = consumers4.evaluate_authority_consumers(_policy(old, now=BASE + 3 * HOUR - 300))
    new = _new_regime(old, _authority(4, 4 * HOUR, start=BASE + 1234))
    new_regime = new.selected_shadow_regime
    now = consumers4.next_phase_occurrence(period_seconds=4 * HOUR, phase_offset=new_regime.phase_offset, now=BASE) - 300
    new_value = consumers4.evaluate_authority_consumers(_policy(new, now=now, fired_keys=frozenset({old_value.scheduled_warning_suppression_key})))
    assert new_value.scheduled_warning_suppression_key != old_value.scheduled_warning_suppression_key
    assert new_value.scheduled_pre_restart_alert_eligible


@pytest.mark.parametrize("event_count", [0, 2, 3, 4])
def test_generic_recovery_is_schedule_independent(event_count):
    value = consumers4.evaluate_authority_consumers(
        _policy(_authority(event_count), physical_recovery_event_id="physical-1", physical_recovery_eligible=True)
    )
    assert value.generic_recovery_alert_eligible
    assert value.generic_recovery_suppression_key.regime_id is None


def test_generic_recovery_suppression_survives_replay_and_is_distinct():
    stable = _authority(4)
    first = consumers4.evaluate_authority_consumers(
        _policy(stable, now=BASE + 3 * HOUR - 300, physical_recovery_event_id="physical-1", physical_recovery_eligible=True)
    )
    fired = frozenset({first.generic_recovery_suppression_key, first.scheduled_warning_suppression_key})
    second = consumers4.evaluate_authority_consumers(
        _policy(stable, now=BASE + 3 * HOUR - 300, physical_recovery_event_id="physical-1", physical_recovery_eligible=True, fired_keys=fired)
    )
    assert first.generic_recovery_suppression_key != first.scheduled_warning_suppression_key
    assert not second.generic_recovery_alert_eligible
    assert not second.scheduled_pre_restart_alert_eligible


@pytest.mark.parametrize("events,eligible", [(2, False), (3, False), (4, True)])
def test_outage_relaxation_only_safe_h3(events, eligible):
    decision = _authority(events)
    regime = decision.selected_shadow_regime
    observed = regime.phase_offset if regime else BASE
    value = consumers4.evaluate_authority_consumers(_policy(decision, observed_outage_at=observed))
    assert value.outage_relaxation_eligible is eligible


def test_outage_relaxation_does_not_mutate_evidence():
    decision = _authority(4)
    before = decision.decision_id
    consumers4.evaluate_authority_consumers(_policy(decision, observed_outage_at=decision.selected_shadow_regime.phase_offset))
    assert decision.decision_id == before


def test_comparison_ids_are_deterministic_and_change_only_keyed():
    schema3 = _schema3_decision()
    schema4_value = consumers4.evaluate_authority_consumers(_policy(_authority(4)))
    a = consumers4.compare_authority_consumers(schema3, schema4_value)
    b = consumers4.compare_authority_consumers(schema3, schema4_value)
    assert a == b and a.comparison_id == b.comparison_id and a.significant_difference


def test_poll_only_time_change_within_one_cycle_keeps_comparison_id_stable():
    schema3 = _schema3_decision()
    stable = _authority(4)
    first = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 10))
    second = consumers4.evaluate_authority_consumers(_policy(stable, now=BASE + 20))
    assert first.decision_id == second.decision_id
    assert consumers4.compare_authority_consumers(schema3, first).comparison_id == consumers4.compare_authority_consumers(schema3, second).comparison_id


def test_cutover_disabled_always_returns_schema3():
    schema3 = _schema3_decision()
    schema4_value = consumers4.evaluate_authority_consumers(_policy(_authority(4)))
    value = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=schema4_value,
        production_cutover_enabled=False,
        authoritative_schema4_runtime_enabled=True,
        schema4_server_valid=True,
    )
    assert value.source is consumers4.CutoverSource.SCHEMA3 and value.selected_output is schema3


def test_valid_explicit_cutover_selects_complete_schema4_decision():
    schema3 = _schema3_decision()
    schema4_value = consumers4.evaluate_authority_consumers(_policy(_authority(4)))
    value = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=schema4_value,
        production_cutover_enabled=True,
        authoritative_schema4_runtime_enabled=True,
        schema4_server_valid=True,
    )
    assert value.source is consumers4.CutoverSource.SCHEMA4 and value.selected_output is schema4_value


@pytest.mark.parametrize("runtime_enabled,valid,decision_present", [(False, True, True), (True, False, True), (True, True, False)])
def test_invalid_schema4_uses_whole_schema3_safe_fallback(runtime_enabled, valid, decision_present):
    schema3 = _schema3_decision()
    schema4_value = consumers4.evaluate_authority_consumers(_policy(_authority(4))) if decision_present else None
    value = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=schema4_value,
        production_cutover_enabled=True,
        authoritative_schema4_runtime_enabled=runtime_enabled,
        schema4_server_valid=valid,
    )
    assert value.source is consumers4.CutoverSource.SCHEMA3_SAFE_FALLBACK
    assert value.selected_output is schema3


def test_transitionally_unsafe_schema4_never_falls_back_to_schema3_countdown():
    schema3 = _schema3_decision()
    transition = _transition(_authority(4), _authority(3, 4 * HOUR, start=BASE + 1234), state=authority.RegimeState.TRANSITION_CONFIRMED)
    schema4_value = consumers4.evaluate_authority_consumers(_policy(transition))
    value = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=schema4_value,
        production_cutover_enabled=True,
        authoritative_schema4_runtime_enabled=True,
        schema4_server_valid=True,
    )
    assert value.selected_output is schema4_value and not schema4_value.countdown_visible


def _runtime_recovery_window(authority_decision, consumer_decision, restart_started_at):
    schema3 = _schema3_decision()
    resolution = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=consumer_decision,
        production_cutover_enabled=True,
        authoritative_schema4_runtime_enabled=True,
        schema4_server_valid=True,
    )
    value = object.__new__(Phase2RestartRuntime)
    value._servers = {
        SERVER: SimpleNamespace(
            engine=SimpleNamespace(
                active_episode=SimpleNamespace(
                    event_id="physical-event",
                    first_failure_wall=restart_started_at,
                )
            )
        )
    }
    value._authority_consumer_resolutions = {SERVER: resolution}
    value._authoritative_schema4_backend = SimpleNamespace(
        authority_decision=lambda _key: authority_decision
    )
    return value.recovery_alert_expected_window(SERVER)


def test_runtime_recovery_window_uses_safe_schema4_restart_phase():
    decision = _authority(4)
    consumer = consumers4.evaluate_authority_consumers(_policy(decision))
    expected = consumer.next_expected_restart_at
    assert expected is not None

    inside = _runtime_recovery_window(decision, consumer, expected - 600)
    outside = _runtime_recovery_window(decision, consumer, expected - 600.001)

    assert inside.status is RecoveryAlertWindowStatus.INSIDE_WINDOW
    assert inside.residual_seconds == 600
    assert outside.status is RecoveryAlertWindowStatus.OUTSIDE_WINDOW
    assert outside.residual_seconds == pytest.approx(600.001)


@pytest.mark.parametrize(
    "state",
    (
        authority.RegimeState.CHANGE_SUSPECTED,
        authority.RegimeState.TRANSITION_CONFIRMED,
    ),
)
def test_runtime_recovery_window_fails_open_for_change_or_transition_state(state):
    incumbent = _authority(4)
    changed = _transition(
        incumbent,
        _authority(3, 4 * HOUR, start=BASE + 1234),
        state=state,
    )
    consumer = consumers4.evaluate_authority_consumers(_policy(changed))
    restart_started_at = (
        consumer.next_expected_restart_at - 900
        if consumer.next_expected_restart_at is not None
        else BASE + 900
    )

    value = _runtime_recovery_window(changed, consumer, restart_started_at)

    assert value.status is RecoveryAlertWindowStatus.NO_SAFE_EXPECTATION


def test_authority_summary_reuses_current_ui_presentation_shape():
    value = consumers4.evaluate_authority_consumers(_policy(_authority(4)))
    summary = consumers4.authority_consumer_summary(value, now=BASE + 100)
    presentation = restart_learning_presentation(summary)
    assert presentation["authority_consumer"]
    assert summary["cycle_text"] == "Confirmed: Every 3 hours"
    assert presentation["cycle_text"] == "Every 3 Hours"
    assert presentation["countdown_visible"]
    assert presentation["countdown_safe"]
    assert presentation["reason_codes"] == value.reason_codes


def test_production_switches_are_enabled():
    from dzll_launcher import config

    assert config.SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED is True
    assert config.SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED is True
    assert consumers4.SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED_DEFAULT is False
    assert consumers4.SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED_DEFAULT is False


def test_both_switches_false_do_not_execute_or_create_schema4(tmp_path):
    active = tmp_path / "phase2.json"
    legacy = tmp_path / "legacy.json"
    runtime = Phase2RestartRuntime.initialize(active_path=active, legacy_path=legacy, now=BASE)
    runtime.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=1, poll_generation=1)
    current = runtime.decision(SERVER, now=BASE + 1)
    assert isinstance(current, consumers3.ConsumerDecision)
    assert runtime.authority_consumer_shadow_snapshot(SERVER) is None
    assert runtime.authority_consumer_resolution(SERVER) is None
    assert not any(tmp_path.glob("*schema4*"))


def test_shadow_mode_keeps_returned_production_decision_schema3(tmp_path):
    active = tmp_path / "phase2.json"
    legacy = tmp_path / "legacy.json"
    runtime = Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE,
        continuity_shadow_enabled=True,
        continuity_authority_shadow_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
    )
    runtime.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=1, poll_generation=1)
    current = runtime.decision(SERVER, now=BASE + 1)
    assert isinstance(current, consumers3.ConsumerDecision)


def test_bob_prepared_consumer_projection_and_active_integrity_when_available(tmp_path):
    prepared = Path(tempfile.gettempdir()) / "bob_phase2_schema4_prepared_20260722.json"
    active = Path.home() / ".config/dzll/companion_restart_learning_phase2.json"
    if not prepared.exists() or not active.exists():
        pytest.skip("detached Bob inputs unavailable")
    active_before = (active.stat().st_size, active.stat().st_mtime_ns, hashlib.sha256(active.read_bytes()).hexdigest())
    original = prepared.read_bytes()
    fixture = tmp_path / "bob-schema4.json"
    fixture.write_bytes(original)
    state = schema4.deserialize_schema4_bytes(fixture.read_bytes()).state
    decision = schema4.persisted_authority_decision(state["servers"]["51.195.74.63:3058"])
    value = consumers4.evaluate_authority_consumers(_policy(decision, now=1_784_800_000.0))
    assert value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert value.cycle_period_seconds == 3 * HOUR
    assert round(value.confidence_display_value, 2) == 0.97
    assert value.countdown_visible and value.countdown_safe
    synthetic_h2 = _transition(
        decision,
        _authority(3, 4 * HOUR, server="51.195.74.63:3058", start=BASE + 1234),
        state=authority.RegimeState.TRANSITION_CONFIRMED,
    )
    h2_value = consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=synthetic_h2,
            now=1_784_800_000.0,
        )
    )
    assert h2_value.presentation_state is consumers4.AuthorityPresentationKey.LIKELY_NEW_CYCLE
    assert h2_value.cycle_period_seconds == 4 * HOUR
    assert not h2_value.countdown_visible
    synthetic_h3 = _new_regime(
        decision,
        _authority(4, 4 * HOUR, server="51.195.74.63:3058", start=BASE + 1234),
    )
    h3_value = consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=synthetic_h3,
            now=1_784_800_000.0,
        )
    )
    assert h3_value.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE
    assert h3_value.cycle_period_seconds == 4 * HOUR
    assert h3_value.countdown_visible
    assert fixture.read_bytes() == original
    assert active_before == (active.stat().st_size, active.stat().st_mtime_ns, hashlib.sha256(active.read_bytes()).hexdigest())
