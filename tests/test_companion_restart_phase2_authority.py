from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_900_000_000.0
SERVER = "authority.example:2302"
HOUR = scoring.HOUR


def event(sequence, at, *, event_id=None, authenticity=0.85):
    identity = event_id or f"event-{sequence}-{int(at)}"
    return detection.PhysicalRestartEvent(
        event_id=identity,
        sequence=sequence,
        fingerprint=f"fingerprint-{identity}",
        server_key=SERVER,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(
            None, None, None, None, False, None, None, 0, 0, False
        ),
        outage=detection.OutageSummary(
            at - 70,
            at - 60,
            3,
            (detection.InfoStatus.TIMEOUT,),
            at,
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("authority_fixture",),
        app_session_id="app",
        monitoring_session_id=f"monitor-{identity}",
        poll_generation=1,
        continuity_chain_id="fixture-chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def relationship(left, right, period, *, chain="chain", high=True, observed=0.98):
    residual = (right.canonical_phase_at - left.canonical_phase_at) - period
    tolerance = scoring.candidate_phase_tolerance(period)
    identity = f"relationship-{period}-{left.event_id}-{right.event_id}"
    return continuity.IntervalRelationship(
        schema_version=continuity.CONTINUITY_SCHEMA_VERSION,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
        relationship_id=identity,
        server_key=SERVER,
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
        coverage_quality=(
            continuity.CoverageQuality.COMPLETE
            if high
            else continuity.CoverageQuality.UNPROVEN
        ),
        observed_ratio=observed,
        largest_unexplained_gap=0,
        monitoring_session_id=f"monitor-{chain}" if high else None,
        continuity_chain_id=chain if high else None,
        continuity_qualified=high,
        normal_support_diagnostic=0.8,
        high_authority_eligible=high,
        high_quality_diagnostic=(
            min(
                1.0,
                min(left.authenticity, right.authenticity) / 0.85,
                observed / 0.98,
                1 - 0.20 * min(1.0, abs(residual) / tolerance),
            )
            if high
            else 0.0
        ),
        reason_codes=("authority_fixture_relationship",),
    )


def cadence(period, event_count, *, start=BASE, chain="chain", sequence=1):
    events = tuple(event(sequence + index, start + index * period) for index in range(event_count))
    relationships = tuple(
        relationship(events[index], events[index + 1], period, chain=chain)
        for index in range(event_count - 1)
    )
    streaks = continuity.build_cadence_streaks(relationships)
    return events, relationships, streaks


def disconnected(period, *, start, count=2, sequence=100):
    values = []
    relationships = []
    for index in range(count):
        left = event(sequence + index * 2, start + index * period * 2)
        right = event(sequence + index * 2 + 1, left.canonical_phase_at + period)
        values.extend((left, right))
        relationships.append(
            relationship(left, right, period, chain=f"disconnected-{index}")
        )
    return tuple(values), tuple(relationships), ()


def normal_score(
    values=(),
    *,
    selected=None,
    prediction=False,
    schedule_confidence=0.0,
):
    base = scoring.RestartScheduleScorer().score(
        (), scoring.CoverageTimeline(()), now=BASE
    )
    by_period = {item.period_seconds: item for item in base.candidates}
    for period, phase, fundamental, phase_confidence, divisors in values:
        by_period[period] = replace(
            by_period[period],
            phase_offset=phase % period,
            phase_confidence=phase_confidence,
            fundamental_period_confidence=fundamental,
            divisor_resolution=tuple(divisors),
            establishment_gates_passed=fundamental >= 0.65,
        )
    return replace(
        base,
        candidates=tuple(by_period[period] for period in scoring.CANDIDATE_PERIODS),
        schedule_existence_confidence=schedule_confidence,
        selected_period_seconds=selected,
        incumbent_period_seconds=selected,
        prediction_usable=prediction,
    )


def score_for(period, phase, fundamental=0.0, phase_confidence=0.0, divisors=()):
    return normal_score(((period, phase, fundamental, phase_confidence, divisors),))


def evaluate(events, relationships, streaks=(), *, score=None, ledger=(), prior=None):
    return authority.evaluate_authority(
        server_key=SERVER,
        normal_score=score or normal_score(),
        relationships=relationships,
        streaks=streaks,
        expected_window_ledger=ledger,
        events=events,
        prior_decision=prior,
    )


def candidate(decision, period, *, phase=None):
    values = [item for item in decision.candidates if item.candidate_period_seconds == period]
    if phase is None:
        return max(values, key=lambda item: item.high_authority)
    return min(
        values,
        key=lambda item: abs(
            ((item.phase_offset - phase + period / 2) % period) - period / 2
        ),
    )


def legacy_miss(period, expected, *, key):
    tolerance = scoring.candidate_phase_tolerance(period)
    return windows.import_legacy_expected_miss(
        server_key=SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - tolerance,
        expected_at=expected,
        window_end_at=expected + tolerance,
        model_revision_id="authority-test-model",
        legacy_key=key,
    )


def neutral_window(period, expected, outcome):
    original = legacy_miss(period, expected, key=f"neutral-{outcome.value}-{expected}")
    return replace(
        original,
        result_id=f"result-{outcome.value}-{period}-{expected}",
        outcome=outcome,
        negative_penalty_active=False,
        healthy_throughout=False,
        coverage_classification=(
            windows.WindowCoverageClassification.OUTAGE_ACTIVITY
            if outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
            else windows.WindowCoverageClassification.UNMONITORED
        ),
        outage_observed=outcome is windows.ExpectedWindowOutcome.AMBIGUOUS,
        overlapping_outage_episode_ids=(
            ("outage-evidence",)
            if outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
            else ()
        ),
    )


def combine(*collections):
    return tuple(item for collection in collections for item in collection)


def test_one_clean_interval_is_h1_only_and_not_usable():
    events, relationships, streaks = cadence(3 * HOUR, 2)
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert result.relationship_quality_values == ((relationships[0].relationship_id, 1.0),)
    assert result.base_authority == 0.5
    assert result.streak_bonus == 0
    assert result.high_authority == 0.5
    assert result.h1_gate and not result.h2_gate and not result.h3_gate
    assert value.state is authority.RegimeState.NORMAL_LEARNING
    assert not value.cycle_visible and not value.prediction_usable


def test_two_matching_intervals_are_h2_likely():
    events, relationships, streaks = cadence(3 * HOUR, 3)
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert result.base_authority == 0.75
    assert result.streak_bonus == 0.04
    assert result.high_authority == 0.79
    assert result.h1_gate and result.h2_gate and not result.h3_gate
    assert value.state is authority.RegimeState.LIKELY
    assert value.cycle_visible and not value.prediction_usable


def test_three_matching_intervals_are_h3_established_and_usable():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert result.base_authority == 0.875
    assert result.streak_bonus == 0.06
    assert result.high_authority == 0.935
    assert result.high_phase_authority == 0.935
    assert result.h3_gate and not result.h4_gate
    assert value.state is authority.RegimeState.ESTABLISHED
    assert value.cycle_visible and value.prediction_usable and value.countdown_safe


def test_disconnected_matching_intervals_cannot_impersonate_h2_or_h3():
    events, relationships, streaks = disconnected(3 * HOUR, start=BASE, count=3)
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert result.base_authority == 0.875
    assert result.streak_bonus == 0
    assert result.h1_gate and not result.h2_gate and not result.h3_gate
    assert value.state is authority.RegimeState.NORMAL_LEARNING


def bob_shape():
    period = 3 * HOUR
    early_events, early_relationships, _ = disconnected(
        period, start=BASE, count=2, sequence=10
    )
    streak_events, streak_relationships, streaks = cadence(
        period, 3, start=BASE + 12 * period, chain="bob-recent", sequence=20
    )
    return (
        combine(early_events, streak_events),
        combine(early_relationships, streak_relationships),
        streaks,
    )


def test_h2_streak_plus_four_high_relationships_is_h3_and_h4():
    events, relationships, streaks = bob_shape()
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert len(result.high_relationship_ids) == 4
    assert result.base_authority == 0.9375
    assert result.streak_bonus == 0.04
    assert result.high_authority == 0.97
    assert result.h2_gate and result.h3_gate and result.h4_gate
    assert value.state is authority.RegimeState.ESTABLISHED


def sanitized_bob_evidence():
    period = 3 * HOUR
    event4 = event(4, 1_784_365_287.0092795, event_id="sanitized-bob-4")
    event5 = event(5, 1_784_376_088.035271, event_id="sanitized-bob-5")
    event15 = event(15, 1_784_667_687.6997728, event_id="sanitized-bob-15", authenticity=0.97)
    event16 = event(16, 1_784_678_494.7111816, event_id="sanitized-bob-16", authenticity=0.97)
    event19 = event(19, 1_784_710_887.2002127, event_id="sanitized-bob-19", authenticity=0.97)
    event20 = event(20, 1_784_721_687.1698542, event_id="sanitized-bob-20")
    event21 = event(21, 1_784_732_486.1738646, event_id="sanitized-bob-21", authenticity=0.97)
    relationships = tuple(
        replace(value, tolerance_seconds=value.tolerance_seconds + 3)
        for value in (
            relationship(event4, event5, period, chain="sanitized-bob-early"),
            relationship(event15, event16, period, chain="sanitized-bob-middle"),
            relationship(event19, event20, period, chain="sanitized-bob-recent"),
            relationship(event20, event21, period, chain="sanitized-bob-recent"),
        )
    )
    streaks = continuity.build_cadence_streaks(relationships)
    return (
        (event4, event5, event15, event16, event19, event20, event21),
        relationships,
        streaks,
    )


def test_sanitized_bob_projection_matches_exact_quality_and_authority_values():
    events, relationships, streaks = sanitized_bob_evidence()
    value = evaluate(events, relationships, streaks)
    result = candidate(value, 3 * HOUR)
    assert tuple(item[1] for item in result.relationship_quality_values) == (
        0.999622,
        0.997418,
        0.999989,
        0.999633,
    )
    assert result.base_authority == 0.937291
    assert result.streak_bonus == 0.039985
    assert result.high_authority == 0.97
    assert result.phase_spread == 0.99599
    assert result.high_phase_authority == 0.968211
    assert result.h4_gate


def test_sanitized_bob_sequence_22_suspicion_is_cleared_by_aligned_sequence_23():
    events, relationships, streaks = sanitized_bob_evidence()
    incumbent = evaluate(events, relationships, streaks)
    sequence22 = event(22, 1_784_735_860.1699753, event_id="sanitized-bob-22", authenticity=0.97)
    suspected = evaluate(
        (*events, sequence22), relationships, streaks, prior=incumbent
    )
    assert suspected.state is authority.RegimeState.CHANGE_SUSPECTED
    assert suspected.suspicion_level == 1

    sequence23 = event(23, 1_784_743_288.1709867, event_id="sanitized-bob-23", authenticity=0.97)
    two_hour_clue = relationship(
        sequence22, sequence23, 2 * HOUR, chain="sanitized-bob-22-23"
    )
    cleared = evaluate(
        (*events, sequence22, sequence23),
        (*relationships, two_hour_clue),
        streaks,
        prior=suspected,
    )
    assert cleared.state is authority.RegimeState.ESTABLISHED
    assert cleared.suspicion_level == 0
    assert cleared.prediction_usable
    assert any(
        two_hour_clue.relationship_id in item.high_relationship_ids
        for item in cleared.challenger_contexts
    )


def test_incomplete_confirmed_off_phase_event_is_not_suspicion_evidence():
    events, relationships, streaks = sanitized_bob_evidence()
    incumbent = evaluate(events, relationships, streaks)
    incomplete = replace(
        event(
            22,
            1_784_735_860.1699753,
            event_id="incomplete-confirmed-off-phase",
            authenticity=0.97,
        ),
        coverage_complete=False,
    )

    value = evaluate(
        (*events, incomplete), relationships, streaks, prior=incumbent
    )

    assert not scoring.event_learning_eligible(incomplete)
    assert value.state is authority.RegimeState.ESTABLISHED
    assert value.suspicion_level == 0
    assert incomplete.event_id not in value.active_suspicion_evidence_ids
    assert all(
        incomplete.event_id not in item.off_phase_event_ids
        for item in value.normal_ledger.candidates
    )


def test_false_legacy_misses_cannot_cancel_bob_high_authority():
    events, relationships, streaks = bob_shape()
    period = 3 * HOUR
    expected_times = (BASE, BASE + period, BASE + 2 * period)
    misses = tuple(
        legacy_miss(candidate_period, expected_at, key=f"bob-miss-{candidate_period}-{expected_at}")
        for candidate_period, expected_at in (
            (period, expected_times[0]),
            (6 * HOUR, expected_times[0]),
            (period, expected_times[1]),
            (period, expected_times[2]),
            (6 * HOUR, expected_times[2]),
        )
    )
    value = evaluate(events, relationships, streaks, ledger=misses)
    assert candidate(value, period).high_authority == 0.97
    assert value.state is authority.RegimeState.ESTABLISHED
    assert value.prediction_usable
    assert set(value.normal_ledger.active_genuine_miss_ids) == {
        item.result_id for item in misses
    }


def test_retracted_bob_misses_clean_normal_ledger_without_changing_high_model():
    events, relationships, streaks = bob_shape()
    period = 3 * HOUR
    expected_times = (BASE, BASE + period, BASE + 2 * period)
    originals = tuple(
        legacy_miss(candidate_period, expected_at, key=f"bob-retract-{candidate_period}-{expected_at}")
        for candidate_period, expected_at in (
            (period, expected_times[0]),
            (6 * HOUR, expected_times[0]),
            (period, expected_times[1]),
            (period, expected_times[2]),
            (6 * HOUR, expected_times[2]),
        )
    )
    classifications = tuple(
        neutral_window(
            item.candidate_period_seconds,
            item.expected_at,
            windows.ExpectedWindowOutcome.AMBIGUOUS,
        )
        for item in originals
    )
    ledger = windows.reconcile_expected_window_ledger(
        originals,
        classifications,
        reconciled_at=BASE + 20 * period,
        evidence_revision_id="bob-retraction-evidence",
    )
    before = evaluate(events, relationships, streaks, ledger=originals)
    after = evaluate(events, relationships, streaks, ledger=ledger)
    assert candidate(before, period).high_authority == candidate(after, period).high_authority
    assert before.selected_shadow_regime == after.selected_shadow_regime
    assert before.state == after.state == authority.RegimeState.ESTABLISHED
    assert before.normal_ledger.active_genuine_miss_ids
    assert not after.normal_ledger.active_genuine_miss_ids


def test_one_month_unmonitored_is_exactly_neutral():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    initial = evaluate(events, relationships, streaks)
    later = evaluate(events, relationships, streaks, prior=initial)
    assert candidate(initial, 3 * HOUR) == candidate(later, 3 * HOUR)
    assert initial.incumbent_shadow_regime == later.incumbent_shadow_regime
    assert later.state is authority.RegimeState.ESTABLISHED


def test_spotty_aligned_normal_evidence_only_refines_remaining_uncertainty():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    base = evaluate(events, relationships, streaks)
    phase = candidate(base, 3 * HOUR).phase_offset
    score = score_for(3 * HOUR, phase, 0.9, 0.9)
    reinforced = evaluate(events, relationships, streaks, score=score, prior=base)
    original = candidate(base, 3 * HOUR)
    updated = candidate(reinforced, 3 * HOUR)
    assert updated.high_authority == original.high_authority
    assert updated.combined_confidence == pytest.approx(
        original.high_authority + (1 - original.high_authority) * 0.09,
        abs=1e-6,
    )
    assert updated.combined_confidence - updated.high_authority <= 0.10


def test_normal_correct_spacing_at_shifted_phase_does_not_reinforce_incumbent():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    initial = evaluate(events, relationships, streaks)
    incumbent_candidate = candidate(initial, 3 * HOUR)
    shifted_phase = incumbent_candidate.phase_offset + 40 * 60
    score = score_for(3 * HOUR, shifted_phase, 0.9, 0.9)
    value = evaluate(events, relationships, streaks, score=score, prior=initial)
    retained = candidate(value, 3 * HOUR, phase=incumbent_candidate.phase_offset)
    assert retained.aligned_normal_confidence == 0
    assert retained.combined_confidence == retained.high_authority
    assert value.incumbent_shadow_regime.regime_id == initial.incumbent_shadow_regime.regime_id


def test_one_genuine_miss_sets_level_one_suspicion_but_keeps_prediction():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    initial = evaluate(events, relationships, streaks)
    expected = events[-1].canonical_phase_at + 3 * HOUR
    miss = legacy_miss(3 * HOUR, expected, key="one-real-miss")
    value = evaluate(events, relationships, streaks, ledger=(miss,), prior=initial)
    assert value.state is authority.RegimeState.CHANGE_SUSPECTED
    assert value.suspicion_level == 1
    assert value.incumbent_shadow_regime.regime_id == initial.incumbent_shadow_regime.regime_id
    assert value.prediction_usable and value.countdown_safe
    assert candidate(value, 3 * HOUR).high_authority == 0.935


def test_later_aligned_hit_clears_level_one_suspicion():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    initial = evaluate(events, relationships, streaks)
    expected = events[-1].canonical_phase_at + 3 * HOUR
    miss = legacy_miss(3 * HOUR, expected, key="cleared-miss")
    suspected = evaluate(events, relationships, streaks, ledger=(miss,), prior=initial)
    aligned = event(99, expected + 3 * HOUR)
    cleared = evaluate(
        (*events, aligned), relationships, streaks, ledger=(miss,), prior=suspected
    )
    assert cleared.state is authority.RegimeState.ESTABLISHED
    assert cleared.suspicion_level == 0
    assert not cleared.active_suspicion_evidence_ids


def test_incomplete_confirmed_aligned_event_cannot_clear_suspicion():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    incumbent = evaluate(events, relationships, streaks)
    expected = events[-1].canonical_phase_at + 3 * HOUR
    miss = legacy_miss(3 * HOUR, expected, key="incomplete-aligned-miss")
    suspected = evaluate(events, relationships, streaks, ledger=(miss,), prior=incumbent)
    incomplete = replace(event(99, expected + 3 * HOUR), coverage_complete=False)

    value = evaluate(
        (*events, incomplete),
        relationships,
        streaks,
        ledger=(miss,),
        prior=suspected,
    )

    assert not scoring.event_learning_eligible(incomplete)
    assert value.state is authority.RegimeState.CHANGE_SUSPECTED
    assert value.suspicion_level == 1
    assert value.active_suspicion_evidence_ids == (miss.result_id,)


def established_three_hour():
    values = cadence(3 * HOUR, 4, start=BASE, chain="incumbent-3h", sequence=1)
    return values, evaluate(*values)


def normal_hint_period(period, *, start, count=2, sequence=200):
    events = tuple(event(sequence + index, start + index * period) for index in range(count))
    relationships = tuple(
        relationship(
            events[index], events[index + 1], period, chain="normal", high=False
        )
        for index in range(count - 1)
    )
    return events, relationships


def test_sparse_four_hour_hints_remain_latent_and_cannot_switch_h3():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + 40 * 60
    events4, hints4 = normal_hint_period(4 * HOUR, start=start, count=3)
    score = score_for(4 * HOUR, start, 0.35, 0.6)
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, hints4),
        streaks3,
        score=score,
        prior=incumbent,
    )
    assert value.incumbent_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert value.selected_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert value.strongest_challenger.authorization_state is authority.ChallengerAuthorization.LATENT
    assert value.state is authority.RegimeState.CHALLENGER_ACCUMULATING
    assert value.prediction_usable


def test_h2_four_hour_challenger_authorizes_provisional_transition():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + HOUR
    events4, relationships4, streaks4 = cadence(
        4 * HOUR, 3, start=start, chain="challenger-4h", sequence=200
    )
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    assert value.state is authority.RegimeState.TRANSITION_CONFIRMED
    assert value.selected_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert value.incumbent_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert value.strongest_challenger.high_confirmation_present
    assert not value.prediction_usable and not value.countdown_safe


def test_repeated_h2_evaluation_enters_new_regime_provisional():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + HOUR
    events4, relationships4, streaks4 = cadence(4 * HOUR, 3, start=start, sequence=200)
    transition = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    provisional = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=transition,
    )
    assert provisional.state is authority.RegimeState.NEW_REGIME_PROVISIONAL
    assert provisional.incumbent_shadow_regime.candidate_period_seconds == 3 * HOUR


def test_disconfirmed_provisional_challenger_rolls_back_to_old_incumbent():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + HOUR
    events4, relationships4, streaks4 = cadence(4 * HOUR, 3, start=start, sequence=200)
    transition = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    rolled_back = evaluate(events3, relationships3, streaks3, prior=transition)
    assert rolled_back.state is authority.RegimeState.ESTABLISHED
    assert rolled_back.selected_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert rolled_back.incumbent_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert rolled_back.prediction_usable


def test_h3_four_hour_challenger_supersedes_three_hour_incumbent():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + HOUR
    events4, relationships4, streaks4 = cadence(
        4 * HOUR, 4, start=start, chain="challenger-4h", sequence=200
    )
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    assert value.state is authority.RegimeState.NEW_REGIME_ESTABLISHED
    assert value.selected_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert value.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert value.prediction_usable and value.countdown_safe
    old = next(item for item in value.regimes if item.candidate_period_seconds == 3 * HOUR)
    assert old.status is authority.RegimeRecordStatus.SUPERSEDED
    assert old.superseded_by_id == value.incumbent_shadow_regime.regime_id

    settled = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=value,
    )
    assert settled.state is authority.RegimeState.ESTABLISHED
    assert settled.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR


def test_h4_is_durable_but_not_locked_against_newer_symmetric_h3():
    events3, relationships3, streaks3 = bob_shape()
    incumbent = evaluate(events3, relationships3, streaks3)
    assert candidate(incumbent, 3 * HOUR).h4_gate
    start = max(item.canonical_phase_at for item in events3) + HOUR
    events4, relationships4, streaks4 = cadence(4 * HOUR, 4, start=start, sequence=300)
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    assert candidate(value, 4 * HOUR).h3_gate
    assert value.state is authority.RegimeState.NEW_REGIME_ESTABLISHED
    assert value.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR


def test_established_four_hour_is_symmetrically_superseded_by_h3_three_hour():
    events4, relationships4, streaks4 = cadence(4 * HOUR, 4, start=BASE, sequence=1)
    incumbent = evaluate(events4, relationships4, streaks4)
    start = events4[-1].canonical_phase_at + 2 * HOUR
    events3, relationships3, streaks3 = cadence(3 * HOUR, 4, start=start, sequence=100)
    value = evaluate(
        combine(events4, events3),
        combine(relationships4, relationships3),
        combine(streaks4, streaks3),
        prior=incumbent,
    )
    assert value.state is authority.RegimeState.NEW_REGIME_ESTABLISHED
    assert value.selected_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert "symmetric_high_gate_transition" in value.reason_codes


@pytest.mark.parametrize(
    ("event_count", "expected_state"),
    [
        (2, authority.RegimeState.CHANGE_SUSPECTED),
        (3, authority.RegimeState.TRANSITION_CONFIRMED),
        (4, authority.RegimeState.NEW_REGIME_ESTABLISHED),
    ],
)
def test_same_period_shifted_phase_is_distinct_reversible_regime(event_count, expected_state):
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    shifted_start = events3[-1].canonical_phase_at + 3 * HOUR + 40 * 60
    shifted_events, shifted_relationships, shifted_streaks = cadence(
        3 * HOUR,
        event_count,
        start=shifted_start,
        chain="shifted-phase",
        sequence=200,
    )
    value = evaluate(
        combine(events3, shifted_events),
        combine(relationships3, shifted_relationships),
        combine(streaks3, shifted_streaks),
        prior=incumbent,
    )
    assert value.state is expected_state
    assert value.strongest_challenger.proposed_regime_id != incumbent.incumbent_shadow_regime.regime_id
    assert "same_period_distinct_phase_regime" in value.strongest_challenger.reason_codes
    if event_count == 2:
        assert value.selected_shadow_regime.regime_id == incumbent.selected_shadow_regime.regime_id
    elif event_count == 3:
        assert not value.countdown_safe
    else:
        assert value.incumbent_shadow_regime.phase_offset != incumbent.incumbent_shadow_regime.phase_offset


@pytest.mark.parametrize(
    "outcome",
    [windows.ExpectedWindowOutcome.AMBIGUOUS, windows.ExpectedWindowOutcome.UNKNOWN],
)
def test_ambiguous_and_unknown_expected_windows_are_neutral(outcome):
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    expected = events3[-1].canonical_phase_at + 3 * HOUR
    result = neutral_window(3 * HOUR, expected, outcome)
    value = evaluate(events3, relationships3, streaks3, ledger=(result,), prior=incumbent)
    assert value.state is authority.RegimeState.ESTABLISHED
    assert value.suspicion_level == 0
    assert value.prediction_usable
    assert result.result_id in value.normal_ledger.neutral_window_ids


def test_two_isolated_misses_separated_by_aligned_hits_do_not_accumulate():
    events3, relationships3, streaks3 = cadence(3 * HOUR, 4)
    incumbent = evaluate(events3, relationships3, streaks3)
    miss1_at = events3[-1].canonical_phase_at + 3 * HOUR
    miss2_at = miss1_at + 6 * HOUR
    misses = (
        legacy_miss(3 * HOUR, miss1_at, key="isolated-1"),
        legacy_miss(3 * HOUR, miss2_at, key="isolated-2"),
    )
    latest_hit = event(100, miss2_at + 3 * HOUR)
    value = evaluate(
        (*events3, latest_hit), relationships3, streaks3, ledger=misses, prior=incumbent
    )
    assert value.state is authority.RegimeState.ESTABLISHED
    assert value.suspicion_level == 0
    assert value.prediction_usable


def test_two_disconnected_strong_contradictions_accumulate_without_switch():
    (events3, relationships3, streaks3), incumbent = established_three_hour()
    start = events3[-1].canonical_phase_at + HOUR
    events4, relationships4, _ = disconnected(4 * HOUR, start=start, count=2, sequence=200)
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        streaks3,
        prior=incumbent,
    )
    assert value.state is authority.RegimeState.CHALLENGER_ACCUMULATING
    assert value.incumbent_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert value.selected_shadow_regime.candidate_period_seconds == 3 * HOUR
    assert value.suspicion_level == 2
    assert not value.countdown_safe


def test_newer_equal_h3_wins_only_after_passing_same_gate():
    events3, relationships3, streaks3 = cadence(3 * HOUR, 4, start=BASE)
    incumbent = evaluate(events3, relationships3, streaks3)
    events4, relationships4, streaks4 = cadence(
        4 * HOUR, 4, start=BASE + 20 * HOUR, sequence=100
    )
    value = evaluate(
        combine(events3, events4),
        combine(relationships3, relationships4),
        combine(streaks3, streaks4),
        prior=incumbent,
    )
    assert candidate(value, 3 * HOUR).high_authority == candidate(value, 4 * HOUR).high_authority
    assert value.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert value.state is authority.RegimeState.NEW_REGIME_ESTABLISHED


def test_replaying_identical_evidence_is_byte_value_equivalent():
    events, relationships, streaks = bob_shape()
    first = evaluate(events, relationships, streaks)
    second = evaluate(tuple(reversed(events)), tuple(reversed(relationships)), tuple(reversed(streaks)))
    assert first == second
    assert first.decision_id == second.decision_id
    assert first.candidates == second.candidates
    assert first.regimes == second.regimes


def test_authority_records_are_frozen():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    value = evaluate(events, relationships, streaks)
    with pytest.raises(FrozenInstanceError):
        value.state = authority.RegimeState.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        candidate(value, 3 * HOUR).high_authority = 0


def test_long_harmonic_h3_is_blocked_without_divisor_resolution():
    period = 6 * HOUR
    events, relationships, streaks = cadence(period, 4)
    value = evaluate(events, relationships, streaks, score=score_for(period, events[0].canonical_phase_at))
    result = candidate(value, period)
    assert result.high_authority == 0.935
    assert not result.h2_gate and not result.h3_gate
    assert "unresolved_harmonic_divisor_7200" in result.harmonic_blockers
    assert "unresolved_harmonic_divisor_10800" in result.harmonic_blockers
    assert not value.prediction_usable


def test_healthy_divisor_resolution_allows_long_harmonic_h3():
    period = 6 * HOUR
    events, relationships, streaks = cadence(period, 4)
    divisors = (
        scoring.DivisorResolution(2 * HOUR, 2, 2, True, True),
        scoring.DivisorResolution(3 * HOUR, 2, 2, True, True),
    )
    score = score_for(period, events[0].canonical_phase_at, divisors=divisors)
    value = evaluate(events, relationships, streaks, score=score)
    result = candidate(value, period)
    assert not result.harmonic_blockers
    assert result.h3_gate
    assert value.prediction_usable


def test_period_phase_regime_id_changes_for_material_phase_shift_only():
    events, relationships, streaks = cadence(3 * HOUR, 4)
    first = evaluate(events, relationships, streaks)
    shifted_events, shifted_relationships, shifted_streaks = cadence(
        3 * HOUR, 4, start=BASE + 20 * HOUR + 40 * 60, sequence=100
    )
    shifted = evaluate(shifted_events, shifted_relationships, shifted_streaks)
    assert first.incumbent_shadow_regime.regime_id != shifted.incumbent_shadow_regime.regime_id
    assert first.incumbent_shadow_regime.candidate_period_seconds == shifted.incumbent_shadow_regime.candidate_period_seconds


def test_normal_and_high_ledgers_are_explicitly_separate():
    events, relationships, streaks = cadence(3 * HOUR, 3)
    value = evaluate(events, relationships, streaks)
    assert value.high_ledger.high_relationship_ids == tuple(
        sorted(item.relationship_id for item in relationships)
    )
    assert value.normal_ledger.candidates
    assert "normal_negative_evidence_not_subtracted_from_high_authority" in value.normal_ledger.reason_codes
    assert "incumbent_independent_high_evidence" in value.high_ledger.reason_codes


def make_runtime(tmp_path, *, shadow=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return runtime.Phase2RestartRuntime.initialize(
        active_path=tmp_path / "active.json",
        legacy_path=tmp_path / "legacy.json",
        now=BASE,
        app_session_id="authority-runtime-app",
        generation_id="22222222-2222-4222-8222-222222222222",
        continuity_authority_shadow_enabled=shadow,
    )


def test_runtime_authority_shadow_disabled_has_no_snapshot_or_log(tmp_path, caplog):
    value = make_runtime(tmp_path / "disabled", shadow=False)
    with caplog.at_level("DEBUG"):
        value.decision(SERVER, now=BASE)
    assert value.continuity_authority_shadow_snapshot(SERVER) is None
    assert "restart authority shadow" not in caplog.text


def test_runtime_authority_shadow_enabled_does_not_change_current_score_or_consumer(tmp_path):
    disabled = make_runtime(tmp_path / "disabled", shadow=False)
    enabled = make_runtime(tmp_path / "enabled", shadow=True)
    disabled_server = disabled._server(SERVER)
    enabled_server = enabled._server(SERVER)
    disabled_server.regime_generation = "shared-current-regime"
    enabled_server.regime_generation = "shared-current-regime"

    disabled_decision = disabled.decision(SERVER, now=BASE)
    enabled_decision = enabled.decision(SERVER, now=BASE)

    assert disabled_server.score == enabled_server.score
    assert disabled_decision == enabled_decision
    snapshot = enabled.continuity_authority_shadow_snapshot(SERVER)
    assert snapshot is not None
    assert snapshot.state is authority.RegimeState.UNKNOWN


def test_authority_shadow_is_never_serialized_into_schema_three(tmp_path):
    value = make_runtime(tmp_path / "enabled", shadow=True)
    server = value._server(SERVER)
    value.decision(SERVER, now=BASE)
    serialized = runtime._serialize_server(server)
    assert value.continuity_authority_shadow_snapshot(SERVER) is not None
    assert not any("authority" in key for key in serialized)
    assert serialized["runtime_state_version"] == runtime.RUNTIME_STATE_VERSION
