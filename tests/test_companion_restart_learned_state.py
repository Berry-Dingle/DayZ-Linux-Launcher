"""Retention Stage R1: compact persistent learned state (synthetic only).

Every scenario drives synthetic restarts through the real Stage 8 pipeline
(schema-3 scorer, schema-4 continuity, ``evaluate_authority``) using the
Stage 8 baseline harness, then folds the same evidence into the compact
learned state and compares the compact evaluation with the raw authority.
No runtime object, profile, learning file or network is touched.
"""

from __future__ import annotations

import inspect
import json
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P3 = 3 * HOUR
P4 = 4 * HOUR
P6 = 6 * HOUR
Outcome = s8.Outcome


# --------------------------------------------------------------------------
# Folding helpers
# --------------------------------------------------------------------------


def unit_items(step, unit):
    ids = {item.event_id for item in unit}
    relationships = [
        item
        for item in step.relationships
        if item.left_event_id in ids and item.right_event_id in ids
    ]
    streaks = [item for item in step.streaks if set(item.event_ids) <= ids]
    return relationships, streaks


def fold_events(step, events, state=None):
    state = state or learned.empty_learned_state(s8.SERVER)
    for unit in learned.chain_fold_units(events):
        relationships, streaks = unit_items(step, unit)
        state = learned.fold_unit(
            state, events=unit, relationships=relationships, streaks=streaks
        )
    return state


def split(step, ring_chains):
    """Fold every chain except the newest ``ring_chains`` ones."""

    units = learned.chain_fold_units(step.events)
    keep = len(units) - ring_chains if ring_chains else len(units)
    folded = [event for unit in units[:keep] for event in unit]
    ring = tuple(event for unit in units[keep:] for event in unit)
    return fold_events(step, folded), ring


def phase_match(left, right, period, tolerance=1.0):
    return authority._circular_distance(left, right, period) <= tolerance


def display(value):
    return consumers4.confidence_display_percent(value)


def high_candidates(step):
    return [item for item in step.decision.candidates if item.high_relationship_ids]


def assert_high_parity(step, state, ring=()):
    compact = learned.compact_candidates(
        state,
        ring_events=ring,
        ring_relationships=step.relationships,
        ring_streaks=step.streaks,
        normal_score=step.score,
    )
    expected = high_candidates(step)
    assert len(compact) == len(expected), (compact, expected)
    for item in expected:
        period = item.candidate_period_seconds
        match = [
            value
            for value in compact
            if value.period == period and phase_match(value.phase_offset, item.phase_offset, period)
        ]
        assert len(match) == 1, (item, compact)
        value = match[0]
        if authority.SPARSE_CONFIRMATION_REASON in item.reason_codes:
            # Acquisition established the sparse regime and confirmed it in
            # one decision: compare the compact Stage 8.5 confirmation.
            confirmed = learned.compact_sparse_confirmation(
                learned.observe_decision(state, step.decision),
                value,
                ring_events=ring,
                ring_relationships=step.relationships,
            )
            value = replace(value, combined_confidence=confirmed)
        assert value.high_authority == pytest.approx(item.high_authority, abs=1e-6)
        assert value.phase_quality == pytest.approx(item.phase_quality, abs=1e-6)
        assert value.high_phase_authority == pytest.approx(item.high_phase_authority, abs=1e-6)
        assert value.aligned_normal_confidence == pytest.approx(
            item.aligned_normal_confidence, abs=1e-6
        )
        assert value.combined_confidence == pytest.approx(item.combined_confidence, abs=1e-6)
        assert (value.h1_gate, value.h2_gate, value.h3_gate, value.h4_gate) == (
            item.h1_gate,
            item.h2_gate,
            item.h3_gate,
            item.h4_gate,
        )
        assert value.harmonic_blockers == item.harmonic_blockers
        assert value.relationship_count == len(item.high_relationship_ids)
        assert value.first_evidence_at == item.first_evidence_at
        assert value.last_evidence_at == item.last_evidence_at
    return compact


def assert_sparse_parity(step, state, ring=(), prior=None, conservative=False):
    """Every candidate's sparse slot and SE1-SE5 structure match authority.

    ``conservative`` covers the documented bin-straddle approximation: the
    compact value may only be lower (more off-phase weight), never higher.
    """

    compact_high = learned.compact_candidates(
        state,
        ring_events=ring,
        ring_relationships=step.relationships,
        ring_streaks=step.streaks,
        normal_score=step.score,
    )
    sparse = authority._sparse_evidence(step.events, prior)
    for item in step.decision.normal_ledger.candidates:
        period = item.candidate_period_seconds
        high = [
            value
            for value in compact_high
            if value.period == period and phase_match(value.phase_offset, item.phase_offset, period)
        ]
        if high:
            value = high[0].aligned_normal_confidence
            reasons = high[0].sparse_reason_codes
        else:
            support = learned.compact_sparse_support(
                state, period=period, phase_offset=item.phase_offset, ring_events=ring
            )
            value = support.value
            reasons = learned.compact_sparse_structure_reasons(
                state, period=period, support=support, normal_candidate=step.score.candidate(period)
            )
            raw = authority._sparse_support(
                period=period, phase_offset=item.phase_offset, sparse=sparse, excluded_event_ids=frozenset()
            )
            assert support.consistency == pytest.approx(raw.consistency, abs=1e-6)
            assert support.effective == pytest.approx(raw.effective, abs=1e-6)
            assert support.aligned_count == len(raw.aligned)
            if conservative:
                assert support.off_phase_weight >= raw.off_phase_weight - 1e-6
            else:
                assert support.off_phase_weight == pytest.approx(raw.off_phase_weight, abs=1e-6)
        if conservative:
            assert value <= item.aligned_normal_confidence + 1e-6, item
            if authority.SPARSE_STRUCTURE_REASON in reasons:
                assert authority.SPARSE_STRUCTURE_REASON in item.reason_codes
            continue
        assert value == pytest.approx(item.aligned_normal_confidence, abs=1e-6), item
        for code in (authority.SPARSE_STRUCTURE_REASON, authority.SPARSE_PERIOD_AMBIGUOUS_REASON):
            assert (code in reasons) == (code in item.reason_codes), (item, reasons)


def assert_phase_proposal_parity(step, state, ring=(), prior=None):
    sparse = authority._sparse_evidence(step.events, prior)
    for period in scoring.CANDIDATE_PERIODS:
        raw = scoring._phase_cluster(sparse.events, period)
        compact = learned.compact_phase_proposal(state, period=period, ring_events=ring)
        assert compact[2] == raw[2]
        if raw[0] is None:
            assert compact[0] is None
            continue
        # A folded anchor is its bin's mean time of day (at most half a bin
        # from a raw anchor when restarts share one UTC minute).
        assert phase_match(compact[0], raw[0], period, learned.PHASE_BIN_SECONDS / 2)
        # Restarts sharing one UTC minute are represented by their mean time
        # of day, so a MAD can move by at most half a bin; the quality band
        # (the only consumer of the MAD) must not change.
        assert compact[1] == pytest.approx(raw[1], abs=learned.PHASE_BIN_SECONDS / 2)
        assert scoring.phase_quality_band(compact[1]) == scoring.phase_quality_band(raw[1])
        assert compact[3] == raw[3]


# --------------------------------------------------------------------------
# Scenario library (all synthetic, Stage 8 harness)
# --------------------------------------------------------------------------


def _qv(outcome):
    def build():
        return s8.sparse_pipeline(outcome=outcome)

    return build


def _mixed_outcomes():
    pipeline = s8.Pipeline()
    for index, when in enumerate(s8.SPARSE_DISCRIMINATING):
        outcome = (
            Outcome.CORROBORATED_OFFLINE_RESTART,
            Outcome.CONFIRMED_OFFLINE_RESTART,
            Outcome.STRONG_QUERY_VISIBLE_RESTART,
            Outcome.PROBABLE_QUERY_VISIBLE_RESTART,
        )[index % 4]
        pipeline.sparse(f"mixed-{index}", [when], outcome=outcome)
    return pipeline


def _repeated_slots(days=8):
    pipeline = s8.Pipeline()
    pipeline.sparse("slot-a", [s8.at(day, 21) for day in range(days)])
    pipeline.sparse("slot-b", [s8.at(day, 0) for day in range(1, days + 1)])
    return pipeline


def _jittered_sparse():
    pipeline = s8.Pipeline()
    jitter = (0, 7, -12, 25, -3, 41, -38, 14)
    pipeline.sparse(
        "jitter", [when + offset for when, offset in zip(s8.SPARSE_DISCRIMINATING, jitter)]
    )
    return pipeline


def _sparse_then_interval():
    pipeline = s8.sparse_pipeline()
    pipeline.observe("complete", [s8.at(6, 18), s8.at(6, 21)])
    return pipeline


def _mixed_sparse_and_chain():
    pipeline = s8.three_hour_chain(3, day=7)
    pipeline.sparse("sparse", s8.SPARSE_DISCRIMINATING[:5])
    return pipeline


def _six_hour_chain():
    pipeline = s8.Pipeline()
    pipeline.observe("six", [s8.at(0, 0) + index * P6 for index in range(6)])
    return pipeline


def _occasional_anomalies():
    pipeline = s8.Pipeline()
    pipeline.sparse("aligned", s8.aligned_3h(20))
    pipeline.sparse("admin", s8.admin_restarts(2, (5, 13)))
    return pipeline


SCENARIOS = {
    "eight_sparse": s8.sparse_pipeline,
    "same_clock": lambda: s8.sparse_pipeline(s8.SAME_CLOCK),
    "two_intervals": lambda: s8.three_hour_chain(3),
    "three_intervals": lambda: s8.three_hour_chain(4),
    "long_chain": lambda: s8.three_hour_chain(9),
    "sparse_then_interval": _sparse_then_interval,
    "mixed_sparse_and_chain": _mixed_sparse_and_chain,
    "strong_qv": _qv(Outcome.STRONG_QUERY_VISIBLE_RESTART),
    "probable_qv": _qv(Outcome.PROBABLE_QUERY_VISIBLE_RESTART),
    "mixed_outcomes": _mixed_outcomes,
    "repeated_slots": _repeated_slots,
    "jittered_sparse": _jittered_sparse,
    "anomaly_mid_history": s8._anomaly_mid_history,
    "occasional_anomalies": _occasional_anomalies,
    "six_hour_chain": _six_hour_chain,
    "imprecise_h2": s8.imprecise_h2,
    "boundary": s8._boundary_pipeline,
}


@pytest.mark.parametrize("ring_chains", [0, 1, 2], ids=["all_folded", "ring_1", "ring_2"])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_compact_evaluation_matches_raw_authority(name, ring_chains):
    step = SCENARIOS[name]().evaluate()
    state, ring = split(step, ring_chains)
    state = learned.fold_divisor_resolution(state, step.score)

    assert_high_parity(step, state, ring)
    assert_sparse_parity(step, state, ring)
    assert_phase_proposal_parity(step, state, ring)


# --------------------------------------------------------------------------
# Milestones
# --------------------------------------------------------------------------


def test_eight_sparse_observations_reach_80_percent():
    pipeline = s8.sparse_pipeline()
    step = s8.settled(pipeline)
    state = fold_events(step, step.events)

    phase, _mad, aligned, _recent = learned.compact_phase_proposal(state, period=P3)
    support = learned.compact_sparse_support(state, period=P3, phase_offset=phase)
    reasons = learned.compact_sparse_structure_reasons(
        state, period=P3, support=support, normal_candidate=step.score.candidate(P3)
    )

    assert aligned == 8
    assert support.value == 0.95
    assert authority.SPARSE_STRUCTURE_REASON in reasons
    assert learned.sparse_milestone_reached(support.value)
    assert display(learned.compact_combined_confidence(0.0, support.value)) == 80
    assert s8.percent(step) == 80


@pytest.mark.parametrize("ring", [False, True], ids=["folded", "ring"])
def test_sparse_plus_one_complete_interval_reaches_95_percent(ring):
    sparse_step, step = s8._sparse_plus_matching_interval()
    state = learned.observe_decision(
        fold_events(sparse_step, sparse_step.events), sparse_step.decision
    )
    assert state.incumbent is not None
    assert authority.SPARSE_ESTABLISHMENT_REASON in state.incumbent.reason_codes
    new = tuple(
        item for item in step.events if item.event_id not in {e.event_id for e in sparse_step.events}
    )
    ring_events = new if ring else ()
    if not ring:
        state = fold_events(step, new, state)
    candidate = next(
        item
        for item in learned.compact_candidates(
            state,
            ring_events=ring_events,
            ring_relationships=step.relationships,
            ring_streaks=step.streaks,
            normal_score=step.score,
        )
        if item.period == P3
    )
    confirmed = learned.compact_sparse_confirmation(
        state, candidate, ring_events=ring_events, ring_relationships=step.relationships
    )
    incumbent = step.decision.incumbent_shadow_regime
    raw = next(
        item
        for item in step.decision.candidates
        if item.candidate_period_seconds == P3
        and phase_match(item.phase_offset, incumbent.phase_offset, P3, 600)
    )

    assert confirmed == pytest.approx(raw.combined_confidence, abs=1e-6)
    assert display(confirmed) == 95
    assert s8.percent(step) == 95


@pytest.mark.parametrize("count,percent", [(3, 80), (4, 95)], ids=["two", "three"])
def test_consecutive_intervals_reach_80_and_95_percent(count, percent):
    step = s8.three_hour_chain(count).evaluate()
    state = fold_events(step, step.events)
    candidate = assert_high_parity(step, state)[0]

    assert candidate.h2_gate
    assert candidate.h3_gate is (count == 4)
    assert display(candidate.combined_confidence) == percent
    assert s8.percent(s8.settled(s8.three_hour_chain(count))) == percent


def test_oo_and_qv_weights_accumulate_exactly():
    for outcome in (
        Outcome.CORROBORATED_OFFLINE_RESTART,
        Outcome.CONFIRMED_OFFLINE_RESTART,
        Outcome.STRONG_QUERY_VISIBLE_RESTART,
        Outcome.PROBABLE_QUERY_VISIBLE_RESTART,
    ):
        step = s8.sparse_pipeline(outcome=outcome).evaluate()
        state = fold_events(step, step.events)
        expected = sum(scoring._event_weight(item) for item in step.events)

        assert sum(item.weight for item in state.pool) == pytest.approx(expected)
        assert state.watermark.folded_learning_event_count == len(step.events)


def test_repeated_clock_slots_strengthen_existing_bins_without_new_entries():
    small = _repeated_slots(8).evaluate()
    large = _repeated_slots(200).evaluate()
    small_state = fold_events(small, small.events)
    large_state = fold_events(large, large.events)

    assert len(small_state.pool) == len(large_state.pool) == 2
    assert [item.count for item in large_state.pool] == [200, 200]
    assert learned.learned_state_entry_count(small_state) == learned.learned_state_entry_count(
        large_state
    )
    small_support = learned.compact_sparse_support(small_state, period=P3, phase_offset=0.0)
    large_support = learned.compact_sparse_support(large_state, period=P3, phase_offset=0.0)
    assert large_support.effective > small_support.effective
    assert large_support.value >= small_support.value
    assert_sparse_parity(large, large_state)


@pytest.mark.parametrize(
    "times,three_hour_structure,ambiguous_periods",
    [
        (s8.SPARSE_DISCRIMINATING, True, set()),
        (s8.SAME_CLOCK, False, set(scoring.CANDIDATE_PERIODS)),
    ],
    ids=["discriminating", "same_clock"],
)
def test_three_hour_versus_six_hour_discrimination(times, three_hour_structure, ambiguous_periods):
    step = s8.sparse_pipeline(times).evaluate()
    state = fold_events(step, step.events)
    reasons = {}
    for period in scoring.CANDIDATE_PERIODS:
        phase = learned.compact_phase_proposal(state, period=period)[0]
        support = learned.compact_sparse_support(state, period=period, phase_offset=phase)
        reasons[period] = learned.compact_sparse_structure_reasons(
            state, period=period, support=support, normal_candidate=step.score.candidate(period)
        )

    assert (authority.SPARSE_STRUCTURE_REASON in reasons[P3]) is three_hour_structure
    assert authority.SPARSE_STRUCTURE_REASON not in reasons[P6]
    assert {
        period
        for period, values in reasons.items()
        if authority.SPARSE_PERIOD_AMBIGUOUS_REASON in values
    } >= ambiguous_periods
    assert_sparse_parity(step, state)


# --------------------------------------------------------------------------
# Anomalies and cycle change
# --------------------------------------------------------------------------


def test_recovered_off_phase_anomaly_matches_proportional_rule():
    pipeline = s8.sparse_pipeline()
    pipeline.sparse("admin", [s8.at(6, 17, 20)])
    uncertain = pipeline.evaluate()
    pipeline.sparse("ninth", [s8.at(7, 9)])
    recovered = pipeline.evaluate()

    for step, percent in ((uncertain, 76), (recovered, 80)):
        state = fold_events(step, step.events)
        support = learned.compact_sparse_support(state, period=P3, phase_offset=0.0)
        assert support.off_phase_weight == pytest.approx(1.0)
        assert display(learned.compact_combined_confidence(0.0, support.value)) == percent
        assert_sparse_parity(step, state)


@pytest.mark.parametrize("second", [None, (17, 20), (14, 20)], ids=["isolated", "same_clock", "alternative_phase"])
@pytest.mark.parametrize("folded_history", [False, True], ids=["ring_history", "folded_history"])
def test_isolated_admin_restart_matches_authority(second, folded_history):
    pipeline = s8.sparse_pipeline()
    established = s8.settled(pipeline)
    extra = []
    if second is not None:
        extra = pipeline.sparse("first-admin", [s8.at(30, *second)])
    pipeline.sparse("admin", [s8.at(40, 17, 20)])
    step = pipeline.evaluate(s8.persisted(established.decision))
    anomaly = next(item for item in step.events if item.canonical_phase_at == s8.at(40, 17, 20))
    incumbent = established.decision.incumbent_shadow_regime

    state = learned.observe_decision(
        fold_events(established, established.events), established.decision
    )
    if folded_history:
        state = fold_events(step, extra, state)
        ring = (anomaly,)
    else:
        ring = (*extra, anomaly)
    expected = authority._isolated_off_phase_restart(
        (anomaly.event_id,), incumbent=incumbent, events=step.events
    )

    assert learned.compact_isolated_off_phase_restart(state, anomaly, ring_events=ring) is expected
    assert expected is (second is None)


def test_last_aligned_evidence_survives_folding():
    pipeline = s8.sparse_pipeline()
    established = s8.settled(pipeline)
    pipeline.sparse("later-aligned", [s8.at(9, 12)])
    pipeline.sparse("admin", [s8.at(10, 17, 20)])
    step = pipeline.evaluate(s8.persisted(established.decision))
    incumbent = established.decision.incumbent_shadow_regime
    state = learned.observe_decision(
        fold_events(established, established.events), established.decision
    )
    state = fold_events(step, [item for item in step.events if item not in established.events], state)

    assert state.incumbent.last_aligned_evidence_at == s8.at(9, 12)
    assert learned.compact_last_aligned_evidence_at(state) == authority._last_aligned_evidence_at(
        incumbent, step.events, ()
    )


def test_superseded_regime_evidence_is_separated_after_folding():
    pipeline, replaced = s8._superseded_three_hour()
    prior = s8.persisted(replaced.decision)
    step = pipeline.evaluate(prior)
    state = learned.observe_decision(fold_events(step, step.events), replaced.decision)

    assert any(item.period == P3 for item in state.superseded)
    candidate = step.candidate(P4)
    compact = next(
        item
        for item in learned.compact_candidates(state, normal_score=step.score)
        if item.period == P4 and phase_match(item.phase_offset, candidate.phase_offset, P4)
    )
    assert compact.aligned_normal_confidence == pytest.approx(
        candidate.aligned_normal_confidence, abs=1e-6
    )
    without = learned.compact_sparse_support(
        replace(state, superseded=()), period=P4, phase_offset=candidate.phase_offset
    )
    with_history = learned.compact_sparse_support(
        state, period=P4, phase_offset=candidate.phase_offset
    )
    assert with_history.aligned_count < without.aligned_count
    # The 12:00 minute bin holds a superseded-explained restart before the
    # 8 h candidate's domain start and one inside it: the documented
    # conservative straddle case.
    assert_sparse_parity(step, state, prior=prior, conservative=True)
    eight = next(
        item for item in step.decision.normal_ledger.candidates
        if item.candidate_period_seconds == 8 * HOUR and item.phase_offset == 4 * HOUR
    )
    straddle = learned.compact_sparse_support(state, period=8 * HOUR, phase_offset=4 * HOUR)
    raw = authority._sparse_support(
        period=8 * HOUR,
        phase_offset=4 * HOUR,
        sparse=authority._sparse_evidence(step.events, prior),
        excluded_event_ids=frozenset(),
    )
    assert (raw.off_phase_weight, straddle.off_phase_weight) == (1.0, 2.0)
    assert straddle.value <= eight.aligned_normal_confidence


def test_post_establishment_challenger_window_matches_stage6_view():
    pipeline = s8._boundary_pipeline()
    acquired = pipeline.evaluate()
    state = learned.observe_decision(
        fold_events(acquired, acquired.events), acquired.decision
    )
    pipeline.observe("new-four-hour", [s8.at(1, 10), s8.at(1, 14)])
    prior = s8.persisted(acquired.decision)
    step = pipeline.evaluate(prior)
    new = [item for item in step.events if item.event_id not in {e.event_id for e in acquired.events}]
    state = fold_events(step, new, state)

    lifetime = step.candidate(P4)
    view = authority._post_establishment_view(
        lifetime,
        established_at=acquired.decision.incumbent_shadow_regime.established_at,
        relationship_map={item.relationship_id: item for item in step.relationships},
        streaks=step.streaks,
        relationships=step.relationships,
        normal_score=step.score,
        windows=(),
        events=step.events,
        event_map={item.event_id: item for item in step.events},
        sparse=authority._sparse_evidence(step.events, prior),
    )
    challenger = [
        item
        for item in learned.compact_candidates(state, normal_score=step.score, challenger=True)
        if item.period == P4
    ]
    full = [
        item
        for item in learned.compact_candidates(state, normal_score=step.score)
        if item.period == P4
    ]

    assert len(lifetime.high_relationship_ids) == 2
    assert full[0].relationship_count == 2
    assert full[0].high_authority == pytest.approx(lifetime.high_authority, abs=1e-6)
    assert len(challenger) == 1 and challenger[0].relationship_count == 1
    assert challenger[0].high_authority == pytest.approx(view.high_authority, abs=1e-6)
    assert (challenger[0].h1_gate, challenger[0].h2_gate, challenger[0].h3_gate) == (
        view.h1_gate,
        view.h2_gate,
        view.h3_gate,
    )


def test_original_cycle_recovery_count_matches_stage6():
    pipeline = s8.three_hour_chain(4)
    established = s8.settled(pipeline)
    pipeline.observe("later", [s8.at(3, 9) + index * P3 for index in range(3)])
    step = pipeline.evaluate(s8.persisted(established.decision))
    state = learned.observe_decision(
        fold_events(established, established.events), established.decision
    )
    new = [item for item in step.events if item.event_id not in {e.event_id for e in established.events}]
    folded = fold_events(step, new, state)
    incumbent = established.decision.incumbent_shadow_regime
    event_map = {item.event_id: item for item in step.events}

    for boundary in (s8.at(0, 0), s8.at(3, 9), s8.at(3, 10), s8.at(3, 13)):
        context = SimpleNamespace(
            authorization_state=authority.ChallengerAuthorization.AUTHORIZED,
            last_evidence_at=boundary,
        )
        expected = min(
            2,
            len(
                authority._original_cycle_recovery_confirmations(
                    incumbent=incumbent,
                    context=context,
                    relationships=step.relationships,
                    event_map=event_map,
                )
            ),
        )
        assert learned.compact_recovery_confirmations(
            folded, challenger_last_evidence_at=boundary
        ) == expected
        assert learned.compact_recovery_confirmations(
            state,
            challenger_last_evidence_at=boundary,
            ring_events=new,
            ring_relationships=step.relationships,
        ) == expected
    assert len(folded.epoch.recovery) <= authority.ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS


# --------------------------------------------------------------------------
# Persistence across inactivity, replay protection, bounds
# --------------------------------------------------------------------------


def test_compact_evaluation_has_no_time_input():
    for function in (
        learned.compact_sparse_support,
        learned.compact_phase_proposal,
        learned.compact_candidates,
        learned.compact_harmonic_blockers,
        learned.compact_sparse_confirmation,
        learned.compact_last_aligned_evidence_at,
        learned.compact_isolated_off_phase_restart,
        learned.compact_recovery_confirmations,
    ):
        assert "now" not in inspect.signature(function).parameters


def test_learning_survives_round_trip_and_months_of_inactivity_unchanged():
    step = _mixed_sparse_and_chain().evaluate()
    state = learned.fold_divisor_resolution(fold_events(step, step.events), step.score)
    state = learned.observe_decision(state, s8.settled(_mixed_sparse_and_chain()).decision)
    reopened = learned.learned_state_from_mapping(
        json.loads(learned.learned_state_json_bytes(state).decode("utf-8"))
    )

    assert reopened == state
    assert learned.compact_candidates(reopened, normal_score=step.score) == learned.compact_candidates(
        state, normal_score=step.score
    )
    # Six months later the schema-3 coverage has expired: the scorer sees no
    # coverage.  Continuous authority, sparse support and phase come from the
    # learned state and are unchanged.
    expired = scoring.RestartScheduleScorer().score(
        step.events,
        scoring.CoverageTimeline(()),
        now=step.now + 180 * scoring.DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    before = learned.compact_candidates(state, normal_score=step.score)
    after = learned.compact_candidates(reopened, normal_score=expired)
    assert [(item.period, item.phase_offset, item.high_authority, item.combined_confidence, item.h3_gate) for item in after] == [
        (item.period, item.phase_offset, item.high_authority, item.combined_confidence, item.h3_gate) for item in before
    ]


def test_harmonic_divisor_resolution_persists_after_coverage_expiry():
    step = _six_hour_chain().evaluate()
    state = learned.fold_divisor_resolution(fold_events(step, step.events), step.score)
    expired = scoring.RestartScheduleScorer().score(
        step.events,
        scoring.CoverageTimeline(()),
        now=step.now + 180 * scoring.DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )

    assert authority._harmonic_blockers(P6, step.score.candidate(P6), ()) == ((), ())
    # Today: recomputation loses the resolution once coverage expires.
    assert authority._harmonic_blockers(P6, expired.candidate(P6), ())[1]
    # Learned state: the resolution is retained knowledge.
    assert learned.compact_harmonic_blockers(state, P6, expired.candidate(P6)) == ((), ())
    six = [item for item in learned.compact_candidates(state, normal_score=expired) if item.period == P6]
    assert six and six[0].h3_gate
    # Without folded resolution the compact view reports the same blockers as
    # the raw recomputation (nothing is invented).
    assert learned.compact_harmonic_blockers(
        replace(state, divisors=()), P6, expired.candidate(P6)
    ) == authority._harmonic_blockers(P6, expired.candidate(P6), ())


def test_genuine_misses_fold_once_and_resolve_harmonics():
    miss = s8.genuine_miss(2 * HOUR, s8.at(0, 2), key="m1")
    second = s8.genuine_miss(2 * HOUR, s8.at(0, 4), key="m2")
    state = learned.fold_windows(learned.empty_learned_state(s8.SERVER), [miss])
    again = learned.fold_windows(state, [miss])

    assert again == state
    assert state.misses[0].genuine_miss_count == 1
    assert learned.compact_harmonic_blockers(state, P6)[0] == ("unresolved_harmonic_divisor_10800",)
    both = learned.fold_windows(state, [miss, second])
    assert sum(item.genuine_miss_count for item in both.misses) == 2
    assert learned.compact_harmonic_blockers(both, P6)[1] == ("high_harmonic_divisor_unresolved_10800",)
    assert learned.compact_genuine_miss_since(
        both, period=2 * HOUR, phase_offset=0.0, phase_tolerance=600, since=s8.at(0, 3)
    )
    assert not learned.compact_genuine_miss_since(
        both, period=2 * HOUR, phase_offset=0.0, phase_tolerance=600, since=s8.at(0, 5)
    )


def test_replayed_and_late_events_never_count_twice():
    step = s8.sparse_pipeline().evaluate()
    state = fold_events(step, step.events)
    replayed = fold_events(step, step.events, state)

    assert replayed == state
    late = s8.physical_event(99, s8.at(2, 3), s8.Session("late", s8.at(2, 2), s8.at(2, 4)), outcome=Outcome.CORROBORATED_OFFLINE_RESTART)
    assert learned.fold_unit(state, events=[late]) == state
    # Folded events appearing again in the ring are ignored.
    phase = learned.compact_phase_proposal(state, period=P3)[0]
    alone = learned.compact_sparse_support(state, period=P3, phase_offset=phase)
    with_ring = learned.compact_sparse_support(
        state, period=P3, phase_offset=phase, ring_events=step.events
    )
    assert alone == with_ring
    newer = s8.physical_event(100, s8.at(9, 6), s8.Session("newer", s8.at(9, 5), s8.at(9, 7)), outcome=Outcome.CORROBORATED_OFFLINE_RESTART)
    advanced = learned.fold_unit(state, events=[step.events[0], newer])
    assert advanced.watermark.folded_event_count == state.watermark.folded_event_count + 1
    assert sum(item.count for item in advanced.pool) == sum(item.count for item in state.pool) + 1


def test_ineligible_events_add_no_evidence_but_advance_the_watermark():
    session = s8.Session("bad", s8.at(1, 0), s8.at(1, 2))
    incomplete = replace(
        s8.physical_event(1, s8.at(1, 1), session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART),
        coverage_complete=False,
    )
    drain = s8.physical_event(2, s8.at(1, 1, 30), session, outcome=Outcome.AMBIGUOUS_DRAIN)
    state = learned.fold_unit(learned.empty_learned_state(s8.SERVER), events=[incomplete, drain])

    assert state.pool == ()
    assert state.watermark.folded_event_count == 2
    assert state.watermark.folded_learning_event_count == 0


def test_storage_is_bounded_regardless_of_observation_count():
    rng = random.Random(20261106)
    pipeline = s8.Pipeline()
    times = sorted(
        s8.at(day, rng.randrange(24), rng.randrange(60))
        for day in range(3000)
    )
    pipeline.sparse("random", times)
    events = pipeline.events
    state = learned.empty_learned_state(s8.SERVER)
    for event in events:
        state = learned.fold_unit(state, events=[event])

    assert state.watermark.folded_learning_event_count == 3000
    assert len(state.pool) <= learned.DAY_BINS
    assert learned.learned_state_entry_count(state) <= learned.LEARNED_STATE_MAX_ENTRIES
    size = len(learned.learned_state_json_bytes(state))
    # Twice the observations at the same clock minutes: same entries.
    doubled = state
    for index, event in enumerate(events):
        doubled = learned.fold_unit(
            doubled,
            events=[replace(event, event_id=f"repeat-{index}", canonical_phase_at=event.canonical_phase_at + 4000 * scoring.DAY)],
        )
    assert learned.learned_state_entry_count(doubled) == learned.learned_state_entry_count(state)
    assert len(learned.learned_state_json_bytes(doubled)) < size * 1.1
    # Evaluation cost depends on bins, not on restarts.
    for period in scoring.CANDIDATE_PERIODS:
        learned.compact_phase_proposal(doubled, period=period)


def test_long_continuous_runs_fold_into_fixed_size_summaries():
    small = s8.three_hour_chain(9).evaluate()
    large = s8.three_hour_chain(60).evaluate()
    small_state = fold_events(small, small.events)
    large_state = fold_events(large, large.events)

    assert len(large_state.intervals) == len(small_state.intervals)
    assert_high_parity(large, large_state)


def test_ring_fold_units_never_split_a_chain():
    pipeline = s8.Pipeline()
    for day in range(6):
        pipeline.observe(f"day-{day}", [s8.at(day, 9) + index * P3 for index in range(4)])
    folded, ring = learned.ring_fold_units(pipeline.events, capacity=9)

    assert len(ring) == 8 and len(folded) == 4
    assert all(len({item.continuity_chain_id for item in unit}) == 1 for unit in folded)
    assert {item.continuity_chain_id for item in ring}.isdisjoint(
        {item.continuity_chain_id for unit in folded for item in unit}
    )


def test_epoch_resets_on_new_incumbent_and_divisors_reset_on_supersession():
    pipeline, replaced = s8._superseded_three_hour()
    original = s8.settled(s8.Pipeline(events=list(pipeline.events[:7]), coverage=list(pipeline.coverage[:1])))
    step = pipeline.evaluate(s8.persisted(replaced.decision))
    state = learned.observe_decision(
        learned.fold_divisor_resolution(learned.empty_learned_state(s8.SERVER), _six_hour_chain().evaluate().score),
        original.decision,
    )
    assert state.divisors and state.epoch.regime_id == original.decision.incumbent_shadow_regime.regime_id
    changed = learned.observe_decision(state, step.decision)

    assert changed.incumbent.regime_id != state.incumbent.regime_id
    assert changed.epoch.regime_id == changed.incumbent.regime_id
    assert changed.epoch.challenger == () and changed.epoch.recovery == ()
    assert changed.divisors == ()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(schema_version=2), id="version"),
        pytest.param(lambda data: data["pool"].append(dict(data["pool"][0])), id="duplicate_bin"),
        pytest.param(lambda data: data["pool"][0].update(weight=float("nan")), id="nan"),
        pytest.param(lambda data: data["pool"][0].update(count=True), id="bool_count"),
        pytest.param(lambda data: data["pool"][0].update(index=learned.DAY_BINS), id="bin_range"),
        pytest.param(lambda data: data["watermark"].update(event_id=None), id="half_watermark"),
        pytest.param(
            lambda data: data.update(pool=[dict(data["pool"][0], index=index) for index in range(learned.DAY_BINS + 1)]),
            id="oversized",
        ),
        pytest.param(lambda data: data["epoch"].update(recovery=[{"left_at": 1, "right_at": 2}] * 3), id="recovery_bound"),
    ],
)
def test_validation_rejects_malformed_or_unbounded_state(mutate):
    step = s8.sparse_pipeline().evaluate()
    data = json.loads(learned.learned_state_json_bytes(fold_events(step, step.events)))
    mutate(data)

    with pytest.raises(learned.LearnedStateError):
        learned.learned_state_from_mapping(data)
