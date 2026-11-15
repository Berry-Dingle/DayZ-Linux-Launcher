"""Retention R1/R1a: compact persistent learned state (synthetic only).

Every scenario drives synthetic restarts through the real Stage 8 pipeline
(schema-3 scorer, schema-4 continuity, ``evaluate_authority``) using the
Stage 8 baseline harness, then folds the same evidence into the compact
learned state and compares the compact evaluation with the raw authority.
R1a adds strictly bounded folding of arbitrarily long continuity chains
(partial-chain folds with an anchor and an open streak) and conservative
phase handling.  No runtime object, profile, learning file or network is
touched.
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
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_expected_windows as windows
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


def fold_events(step, events, state=None):
    """Fold whole chains (no partial folds)."""

    state = state or learned.empty_learned_state(s8.SERVER)
    for unit in learned.chain_fold_units(events):
        state = learned.fold_unit(state, events=unit, relationships=step.relationships)
    return state


def split(step, ring_chains):
    """Fold every chain except the newest ``ring_chains`` ones."""

    units = learned.chain_fold_units(step.events)
    keep = len(units) - ring_chains if ring_chains else len(units)
    folded = [event for unit in units[:keep] for event in unit]
    ring = tuple(event for unit in units[keep:] for event in unit)
    return fold_events(step, folded), ring


def stream(events, relationships, *, capacity, state=None, ring=(), batch=1):
    """Deliver events in time order, folding whenever the ring exceeds capacity."""

    state = state or learned.empty_learned_state(s8.SERVER)
    ring = tuple(ring)
    peak = len(ring)
    ordered = sorted(events, key=lambda item: (item.canonical_phase_at, item.event_id))
    for start in range(0, len(ordered), batch):
        plan = learned.plan_ring_folding(state, (*ring, *ordered[start:start + batch]), capacity=capacity)
        state = learned.apply_fold_plan(state, plan, relationships)
        ring = plan.ring
        peak = max(peak, len(ring))
        assert len(ring) <= capacity
    return state, ring, peak


def recent_events(events):
    """The schema-3 scorer's recent window (its divisor-resolution input)."""

    return scoring._recent_events(scoring._unique_events(events))


def fold_divisors(state, step):
    return learned.fold_divisor_resolution(state, step.score, recent_events(step.events))


def phase_match(left, right, period, tolerance=1.0):
    return authority._circular_distance(left, right, period) <= tolerance


def display(value):
    return consumers4.confidence_display_percent(value)


def high_candidates(step):
    return [item for item in step.decision.candidates if item.high_relationship_ids]


def compact_high(step, state, ring):
    return learned.compact_candidates(
        state, ring_events=ring, ring_relationships=step.relationships, normal_score=step.score
    )


def assert_high_parity(step, state, ring=(), conservative=False):
    compact = compact_high(step, state, ring)
    expected = high_candidates(step)
    assert len(compact) == len(expected), (compact, expected)
    for item in expected:
        period = item.candidate_period_seconds
        tolerance = learned.RUN_RESIDUAL_QUANTUM_SECONDS if conservative else 1.0
        match = [
            value
            for value in compact
            if value.period == period and phase_match(value.phase_offset, item.phase_offset, period, tolerance)
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
                verified=authority.VERIFIED_ESTABLISHMENT_REASON
                in step.decision.incumbent_shadow_regime.reason_codes,
            )
            value = replace(value, combined_confidence=confirmed)
        assert value.high_authority == pytest.approx(item.high_authority, abs=1e-6)
        assert value.relationship_count == len(item.high_relationship_ids)
        assert value.harmonic_blockers == item.harmonic_blockers
        assert value.first_evidence_at == item.first_evidence_at
        assert value.last_evidence_at == item.last_evidence_at
        gates = (value.h1_gate, value.h2_gate, value.h3_gate, value.h4_gate)
        raw_gates = (item.h1_gate, item.h2_gate, item.h3_gate, item.h4_gate)
        if conservative:
            assert value.phase_quality <= item.phase_quality + 1e-9
            assert all(raw or not compact_gate for compact_gate, raw in zip(gates, raw_gates)), (gates, raw_gates)
            assert value.combined_confidence <= item.combined_confidence + 1e-6
            continue
        assert value.phase_quality == pytest.approx(item.phase_quality, abs=1e-6)
        assert value.high_phase_authority == pytest.approx(item.high_phase_authority, abs=1e-6)
        assert value.aligned_normal_confidence == pytest.approx(item.aligned_normal_confidence, abs=1e-6)
        assert value.combined_confidence == pytest.approx(item.combined_confidence, abs=1e-6)
        assert gates == raw_gates
    return compact


def assert_sparse_parity(step, state, ring=(), prior=None, conservative=False):
    """Every candidate's sparse slot and SE1-SE5 structure match authority.

    ``conservative`` covers the documented approximations: the compact value
    may only be lower, and no structure may appear that authority lacks.
    """

    compact = compact_high(step, state, ring)
    sparse = authority._sparse_evidence(step.events, prior)
    for item in step.decision.normal_ledger.candidates:
        period = item.candidate_period_seconds
        high = [
            value
            for value in compact
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
            if conservative:
                assert support.effective <= raw.effective + 1e-6
                assert support.value <= raw.value + 1e-6
            else:
                assert support.consistency == pytest.approx(raw.consistency, abs=1e-6)
                assert support.effective == pytest.approx(raw.effective, abs=1e-6)
                assert support.aligned_count == len(raw.aligned)
                assert support.off_phase_weight == pytest.approx(raw.off_phase_weight, abs=1e-6)
        if conservative:
            assert value <= item.aligned_normal_confidence + 1e-6, item
            if authority.SPARSE_STRUCTURE_REASON in reasons:
                assert authority.SPARSE_STRUCTURE_REASON in item.reason_codes
            if authority.SPARSE_PERIOD_AMBIGUOUS_REASON in item.reason_codes:
                assert authority.SPARSE_STRUCTURE_REASON not in reasons
            continue
        assert value == pytest.approx(item.aligned_normal_confidence, abs=1e-6), item
        for code in (authority.SPARSE_STRUCTURE_REASON, authority.SPARSE_PERIOD_AMBIGUOUS_REASON):
            assert (code in reasons) == (code in item.reason_codes), (item, reasons)


def assert_phase_proposal_parity(step, state, ring=(), prior=None):
    sparse = authority._sparse_evidence(step.events, prior)
    for period in scoring.CANDIDATE_PERIODS:
        raw = scoring._phase_cluster(sparse.events, period)
        compact = learned.compact_phase_proposal(state, period=period, ring_events=ring)
        if raw[0] is None:
            assert compact[0] is None
            continue
        # Folded anchors are bin mid-points (half a bin from a raw anchor when
        # restarts share one UTC minute).  Authority uses only the proposed
        # phase (``_build_ledgers`` ignores the proposal's MAD); the phase's
        # support is then evaluated conservatively by compact_sparse_support.
        assert phase_match(compact[0], raw[0], period, learned.PHASE_BIN_SECONDS / 2)
        assert 1 <= compact[2] <= raw[2] + 1


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
    pipeline.sparse("jitter", [when + offset for when, offset in zip(s8.SPARSE_DISCRIMINATING, jitter)])
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


def _two_long_chains():
    pipeline = s8.Pipeline()
    pipeline.observe("first", [s8.at(0, 9) + index * P3 for index in range(7)])
    pipeline.observe("second", [s8.at(3, 0) + index * P3 for index in range(7)])
    return pipeline


def _chain_with_incomplete_restart():
    """An unobservable episode inside a chain: the next restart is a fresh
    streak start whose incoming interval is not high-eligible."""

    pipeline = s8.Pipeline()
    pipeline.observe("broken", [s8.at(0, 0) + index * P3 for index in range(8)])
    pipeline.events[3] = replace(pipeline.events[3], coverage_complete=False)
    return pipeline


SCENARIOS = {
    "chain_with_incomplete_restart": _chain_with_incomplete_restart,
    "eight_sparse": s8.sparse_pipeline,
    "same_clock": lambda: s8.sparse_pipeline(s8.SAME_CLOCK),
    "two_intervals": lambda: s8.three_hour_chain(3),
    "three_intervals": lambda: s8.three_hour_chain(4),
    "long_chain": lambda: s8.three_hour_chain(9),
    "two_long_chains": _two_long_chains,
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


def _folded(step, mode):
    if mode.startswith("ring_"):
        return split(step, int(mode[5:]))
    if mode == "all_folded":
        return split(step, 0)
    capacity = int(mode[len("stream_cap"):])
    state, ring, _peak = stream(step.events, step.relationships, capacity=capacity)
    return state, ring


@pytest.mark.parametrize("mode", ["all_folded", "ring_1", "ring_2", "stream_cap2", "stream_cap3", "stream_cap5"])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_compact_evaluation_matches_raw_authority(name, mode):
    step = SCENARIOS[name]().evaluate()
    state, ring = _folded(step, mode)
    state = fold_divisors(state, step)

    assert_high_parity(step, state, ring)
    assert_sparse_parity(step, state, ring)
    assert_phase_proposal_parity(step, state, ring)


# --------------------------------------------------------------------------
# Strictly bounded folding of long continuous chains
# --------------------------------------------------------------------------


def _continuous_chain(count, *, jitter=0, seed=0):
    rng = random.Random(seed)
    times = [s8.at(0, 9) + index * P3 + (rng.randint(-jitter, jitter) if jitter else 0) for index in range(count)]
    pipeline = s8.Pipeline()
    pipeline.observe("long", times)
    return pipeline


def _relationships(pipeline):
    events = tuple(sorted(pipeline.events, key=lambda item: item.canonical_phase_at))
    spans = windows.continuity_spans_from_coverage(pipeline.coverage, server_key=s8.SERVER)
    return events, continuity.extract_interval_relationships(events, spans)


@pytest.mark.parametrize("count", [80, 160, 1000])
def test_continuous_chains_fold_with_a_bounded_ring_and_raw_parity(count):
    pipeline = _continuous_chain(count)
    step = pipeline.evaluate()
    state, ring, peak = stream(step.events, step.relationships, capacity=learned.RECENT_RING_CAPACITY, batch=7)

    assert peak <= learned.RECENT_RING_CAPACITY
    assert len(ring) <= learned.RECENT_RING_CAPACITY
    if count > learned.RECENT_RING_CAPACITY:
        assert state.open_chain is not None and state.open_chain.run is not None
        assert ring[0].event_id == state.open_chain.anchor_event_id
    candidate = assert_high_parity(step, state, ring)[0]
    assert candidate.longest_streak == count - 1
    assert candidate.relationship_count == count - 1
    assert candidate.h3_gate and candidate.h4_gate
    # 0.97 internally; the panel caps the displayed value at 95 %.
    assert display(candidate.combined_confidence) >= 95
    assert learned.learned_state_entry_count(state) <= 20


@pytest.mark.parametrize("count", [5000])
def test_very_long_chain_stays_bounded_without_raw_history(count):
    pipeline = _continuous_chain(count)
    events, relationships = _relationships(pipeline)
    state, ring, peak = stream(events, relationships, capacity=learned.RECENT_RING_CAPACITY, batch=50)
    small, small_ring, _ = stream(*_relationships(_continuous_chain(200)), capacity=learned.RECENT_RING_CAPACITY, batch=50)
    candidate = learned.compact_candidates(state, ring_events=ring, ring_relationships=relationships)[0]

    assert peak <= learned.RECENT_RING_CAPACITY
    assert candidate.longest_streak == count - 1
    assert candidate.relationship_count == count - 1
    assert candidate.phase_offset == 0.0 and candidate.phase_quality == 1.0
    assert candidate.high_authority == authority.HIGH_AUTHORITY_CAP
    assert candidate.h4_gate and display(candidate.combined_confidence) >= 95
    assert learned.learned_state_entry_count(state) == learned.learned_state_entry_count(small)
    assert len(learned.learned_state_json_bytes(state)) < 2 * len(learned.learned_state_json_bytes(small))
    assert len(state.open_chain.run.residuals) == 1


@pytest.mark.parametrize("count,jitter", [(120, 20), (300, 45)])
def test_noisy_long_chain_is_never_promoted(count, jitter):
    pipeline = _continuous_chain(count, jitter=jitter, seed=count)
    step = pipeline.evaluate()
    state, ring, peak = stream(step.events, step.relationships, capacity=learned.RECENT_RING_CAPACITY, batch=9)

    assert peak <= learned.RECENT_RING_CAPACITY
    compact = assert_high_parity(step, state, ring, conservative=True)
    raw = high_candidates(step)
    assert max(item.longest_streak for item in compact) <= max(
        max((len(streak.relationship_ids) for streak in step.streaks), default=0), 1
    )
    # The quantised open streak loses at most a few seconds of phase quality.
    for value in compact:
        match = next(item for item in raw if phase_match(item.phase_offset, value.phase_offset, P3, 5))
        assert match.phase_quality - value.phase_quality <= 2 * learned.RUN_RESIDUAL_QUANTUM_SECONDS / 540


@pytest.mark.parametrize("seed", range(40))
def test_open_run_spread_is_an_upper_bound_of_the_exact_streak_spread(seed):
    rng = random.Random(seed)
    period = P3
    first = rng.uniform(0, period)
    folded = [rng.uniform(-200, 200) for _ in range(rng.randint(2, 60))]
    extra = [rng.uniform(-200, 200) for _ in range(rng.randint(1, 20))]
    run = learned._Run(period, "chain", "anchor", 0.0, 1.0, first, len(folded), 1.0, (), list(folded), 0.0, 0.0)
    seeded = learned._run_from_open(
        learned.OpenChain("chain", "anchor", 1.0, None, 0.0, (), False, run.to_open())
    )
    seeded.exact = list(extra)
    estimate, spread = seeded.phase()
    exact_estimate, exact_spread = continuity._phase_estimate(
        [first + value for value in (*folded, *extra)], period
    )

    assert spread >= exact_spread - 1e-9
    assert spread <= exact_spread + learned.RUN_RESIDUAL_QUANTUM_SECONDS + 1e-9
    assert authority._circular_distance(estimate, exact_estimate, period) <= learned.RUN_RESIDUAL_QUANTUM_SECONDS / 2 + 1e-9


@pytest.mark.parametrize("stop", [5, 6, 7])
def test_untagged_anchor_is_excluded_from_its_own_sparse_slot(stop):
    """The anchor's incoming interval is ineligible but its outgoing one is
    high: the anchor is the candidate's endpoint and leaves the sparse slot."""

    full = _chain_with_incomplete_restart()
    pipeline = s8.Pipeline(events=list(full.events[:stop]), coverage=list(full.coverage))
    step = pipeline.evaluate()
    state, ring, _ = stream(step.events, step.relationships, capacity=2)

    assert state.open_chain is not None
    assert_high_parity(step, state, ring)
    assert_sparse_parity(step, state, ring)


def test_open_streak_extends_across_partial_folds_and_round_trip():
    pipeline = _continuous_chain(40)
    step = pipeline.evaluate()
    ordered = sorted(step.events, key=lambda item: item.canonical_phase_at)
    state, ring, _ = stream(ordered[:25], step.relationships, capacity=4)
    assert state.open_chain is not None and state.open_chain.run.interval_count == 21
    reopened = learned.learned_state_from_json_bytes(learned.learned_state_json_bytes(state))
    assert reopened == state
    continued, ring2, _ = stream(ordered[25:], step.relationships, capacity=4, state=reopened, ring=ring)
    direct, ring3, _ = stream(ordered, step.relationships, capacity=4)

    assert continued == direct and ring2 == ring3
    assert_high_parity(step, continued, ring2)


def test_chain_end_closes_the_open_streak_without_joining_the_next_chain():
    step = _two_long_chains().evaluate()
    state, ring, _ = stream(step.events, step.relationships, capacity=3)

    compact = assert_high_parity(step, state, ring)
    assert max(item.longest_streak for item in compact) == 6
    assert sum(item.streak_count for item in state.intervals) == 1  # first chain closed


def test_unmonitored_gap_is_never_folded_as_continuity():
    pipeline = s8.Pipeline()
    pipeline.observe("before", [s8.at(0, 9) + index * P3 for index in range(5)])
    pipeline.observe("after", [s8.at(0, 9) + index * P3 for index in range(6, 11)])
    step = pipeline.evaluate()
    state, ring, _ = stream(step.events, step.relationships, capacity=2)

    compact = assert_high_parity(step, state, ring)
    assert all(item.longest_streak == 4 for item in compact)
    assert all(item.relationship_count == 8 for item in compact)


def test_plan_ring_folding_never_exceeds_capacity_and_keeps_the_anchor():
    pipeline = s8.Pipeline()
    for day in range(4):
        pipeline.observe(f"day-{day}", [s8.at(day, 0) + index * P3 for index in range(6)])
    state = learned.empty_learned_state(s8.SERVER)
    plan = learned.plan_ring_folding(state, pipeline.events, capacity=5)

    assert len(plan.ring) <= 5
    assert [unit.chain_open for unit in plan.units] == [False, False, False, True]
    assert plan.units[-1].events[-1] == plan.ring[0]
    state = learned.apply_fold_plan(state, plan, pipeline.evaluate().relationships)
    assert state.open_chain.anchor_event_id == plan.ring[0].event_id
    with pytest.raises(learned.LearnedStateError):
        learned.plan_ring_folding(state, pipeline.events, capacity=1)


# --------------------------------------------------------------------------
# Milestones
# --------------------------------------------------------------------------


def test_eight_sparse_observations_reach_the_pre_verification_ceiling():
    """The sparse statistic still saturates at the 80 % milestone; without a
    witnessed interval that is the pre-verification ceiling (Awaiting, 79 %)."""

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
    assert step.decision.state is authority.RegimeState.LIKELY
    assert authority.AWAITING_VERIFICATION_REASON in step.decision.reason_codes
    assert s8.percent(step) == 79


@pytest.mark.parametrize("ring", [False, True], ids=["folded", "ring"])
def test_sparse_plus_one_complete_interval_verifies_at_90_percent(ring):
    sparse_step, step = s8._sparse_plus_matching_interval()
    # Eight sparse restarts await verification; the witnessed interval
    # establishes, with the base interval weight (about 90 %).
    assert sparse_step.decision.state is authority.RegimeState.LIKELY
    assert authority.VERIFIED_ESTABLISHMENT_REASON in step.decision.incumbent_shadow_regime.reason_codes
    state = learned.observe_decision(fold_events(sparse_step, sparse_step.events), step.decision)
    assert authority.SPARSE_ESTABLISHMENT_REASON in state.incumbent.reason_codes
    new = tuple(item for item in step.events if item.event_id not in {e.event_id for e in sparse_step.events})
    ring_events = new if ring else ()
    if not ring:
        state = fold_events(step, new, state)
    candidate = next(item for item in compact_high(step, state, ring_events) if item.period == P3)
    confirmed = learned.compact_sparse_confirmation(
        state, candidate, ring_events=ring_events, ring_relationships=step.relationships, verified=True
    )
    incumbent = step.decision.incumbent_shadow_regime
    raw = next(
        item
        for item in step.decision.candidates
        if item.candidate_period_seconds == P3 and phase_match(item.phase_offset, incumbent.phase_offset, P3, 600)
    )

    assert confirmed == pytest.approx(raw.combined_confidence, abs=1e-6)
    assert display(confirmed) == 90
    assert s8.percent(step) == 90


def test_sparse_confirmation_through_a_partially_folded_chain():
    """A long confirming chain folded around an anchor still confirms once."""

    sparse_step = s8.settled(s8.sparse_pipeline())
    pipeline = s8.sparse_pipeline()
    pipeline.observe("long-confirming", [s8.at(8, 0) + index * P3 for index in range(12)])
    step = pipeline.evaluate(s8.persisted(sparse_step.decision))
    state = learned.observe_decision(fold_events(sparse_step, sparse_step.events), sparse_step.decision)
    new = [item for item in step.events if item.event_id not in {e.event_id for e in sparse_step.events}]
    state, ring, _ = stream(new, step.relationships, capacity=3, state=state)
    candidate = next(item for item in compact_high(step, state, ring) if item.period == P3)
    confirmed = learned.compact_sparse_confirmation(state, candidate, ring_events=ring, ring_relationships=step.relationships)
    incumbent = step.decision.incumbent_shadow_regime
    raw = next(
        item
        for item in step.decision.candidates
        if item.candidate_period_seconds == P3 and phase_match(item.phase_offset, incumbent.phase_offset, P3, 600)
    )

    assert confirmed == pytest.approx(raw.combined_confidence, abs=1e-6)
    assert display(confirmed) >= 95


@pytest.mark.parametrize("count,percent", [(3, 80), (4, 95)], ids=["two", "three"])
def test_consecutive_intervals_reach_80_and_95_percent(count, percent):
    step = s8.three_hour_chain(count).evaluate()
    for state, ring in (split(step, 0), stream(step.events, step.relationships, capacity=2)[:2]):
        candidate = assert_high_parity(step, state, ring)[0]
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
        assert state.pool_weight_total == pytest.approx(expected)
        assert state.watermark.folded_learning_event_count == len(step.events)


def test_repeated_clock_slots_strengthen_existing_bins_without_new_entries():
    small = _repeated_slots(8).evaluate()
    large = _repeated_slots(200).evaluate()
    small_state = fold_events(small, small.events)
    large_state = fold_events(large, large.events)

    assert len(small_state.pool) == len(large_state.pool) == 2
    assert [item.count for item in large_state.pool] == [200, 200]
    assert learned.learned_state_entry_count(small_state) == learned.learned_state_entry_count(large_state)
    small_support = learned.compact_sparse_support(small_state, period=P3, phase_offset=0.0)
    large_support = learned.compact_sparse_support(large_state, period=P3, phase_offset=0.0)
    assert large_support.effective > small_support.effective
    assert large_support.value >= small_support.value
    assert_sparse_parity(large, large_state)


@pytest.mark.parametrize(
    "times,three_hour_structure",
    [(s8.SPARSE_DISCRIMINATING, True), (s8.SAME_CLOCK, False)],
    ids=["discriminating", "same_clock"],
)
def test_three_hour_versus_six_hour_discrimination(times, three_hour_structure):
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
    if not three_hour_structure:
        assert all(authority.SPARSE_PERIOD_AMBIGUOUS_REASON in values for values in reasons.values())
    assert_sparse_parity(step, state)


# --------------------------------------------------------------------------
# Phase accuracy: clean and noisy boundaries never promote
# --------------------------------------------------------------------------


def _with_edge_restarts(offsets):
    """Eight aligned 3 h restarts plus extra restarts at given residuals."""

    pipeline = s8.sparse_pipeline()
    pipeline.sparse("edge", [s8.at(10 + index, 9) + offset for index, offset in enumerate(offsets)])
    return pipeline


@pytest.mark.parametrize(
    "offsets",
    [
        pytest.param((540, 540), id="clean_edge_same_minute"),
        pytest.param((541, 545), id="noisy_edge_same_minute"),
        pytest.param((535, 590), id="straddling_edge"),
        pytest.param((175, 179, 185), id="mad_band_edge"),
        pytest.param((-541, -545, 541), id="both_sides"),
    ],
)
def test_phase_boundaries_are_exact_or_conservative(offsets):
    step = _with_edge_restarts(offsets).evaluate()
    exact_state = split(step, len(offsets))  # the edge restarts unfolded: exact
    folded_state = split(step, 0)

    assert_sparse_parity(step, *exact_state)
    assert_sparse_parity(step, *folded_state, conservative=True)
    assert_high_parity(step, *folded_state)
    raw = authority._sparse_support(
        period=P3, phase_offset=0.0, sparse=authority._sparse_evidence(step.events, None), excluded_event_ids=frozenset()
    )
    compact = learned.compact_sparse_support(folded_state[0], period=P3, phase_offset=0.0)
    assert compact.value <= raw.value + 1e-9
    assert compact.effective <= raw.effective + 1e-9
    if offsets in ((540, 540),):
        assert compact.value == raw.value


def test_shared_minute_jitter_never_resolves_an_ambiguous_period():
    pipeline = s8.Pipeline()
    pipeline.sparse("jittered-same-clock", [s8.at(day, 9) + (day * 7) % 50 for day in range(10)])
    step = pipeline.evaluate()
    state = fold_events(step, step.events)

    assert_sparse_parity(step, state, conservative=True)
    for period in scoring.CANDIDATE_PERIODS:
        phase = learned.compact_phase_proposal(state, period=period)[0]
        support = learned.compact_sparse_support(state, period=period, phase_offset=phase)
        reasons = learned.compact_sparse_structure_reasons(
            state, period=period, support=support, normal_candidate=step.score.candidate(period)
        )
        assert authority.SPARSE_STRUCTURE_REASON not in reasons


def test_sparse_establishment_is_never_promoted_by_folding():
    """Seven aligned restarts plus one at the tolerance edge: below SE1 raw."""

    pipeline = s8.Pipeline()
    pipeline.sparse("seven", s8.SPARSE_DISCRIMINATING[:7])
    pipeline.sparse("edge", [s8.at(9, 15) + 545, s8.at(10, 15) + 541])
    step = pipeline.evaluate()
    state = fold_events(step, step.events)
    support = learned.compact_sparse_support(state, period=P3, phase_offset=0.0)
    raw = authority._sparse_support(
        period=P3, phase_offset=0.0, sparse=authority._sparse_evidence(step.events, None), excluded_event_ids=frozenset()
    )
    reasons = learned.compact_sparse_structure_reasons(
        state, period=P3, support=support, normal_candidate=step.score.candidate(P3)
    )
    raw_reasons = authority._sparse_structure_reasons(
        period=P3, support=raw, normal_candidate=step.score.candidate(P3), windows=()
    )

    assert support.value <= raw.value + 1e-9
    assert support.effective < raw.effective  # the same-minute edge restarts are withheld
    assert not (learned.sparse_milestone_reached(support.value) and not learned.sparse_milestone_reached(raw.value))
    assert not (authority.SPARSE_STRUCTURE_REASON in reasons and authority.SPARSE_STRUCTURE_REASON not in raw_reasons)


# --------------------------------------------------------------------------
# Anomalies and cycle change
# --------------------------------------------------------------------------


def test_recovered_off_phase_anomaly_matches_proportional_rule():
    # Stage 2: four aligned restarts and one admin restart (net three) are
    # uncertain; one more aligned restart reaches the saturated statistic.
    pipeline = s8.sparse_pipeline(s8.SPARSE_DISCRIMINATING[:4])
    pipeline.sparse("admin", [s8.at(6, 17, 20)])
    uncertain = pipeline.evaluate()
    pipeline.sparse("fifth", [s8.at(7, 9)])
    recovered = pipeline.evaluate()

    for step, percent in ((uncertain, 63), (recovered, 80)):
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

    state = learned.observe_decision(fold_events(established, established.events), established.decision)
    if folded_history:
        state = fold_events(step, extra, state)
        ring = (anomaly,)
    else:
        ring = (*extra, anomaly)
    expected = authority._isolated_off_phase_restart((anomaly.event_id,), incumbent=incumbent, events=step.events)

    assert learned.compact_isolated_off_phase_restart(state, anomaly, ring_events=ring) is expected
    assert expected is (second is None)


def test_last_aligned_evidence_survives_folding():
    pipeline = s8.sparse_pipeline()
    established = s8.settled(pipeline)
    pipeline.sparse("later-aligned", [s8.at(9, 12)])
    pipeline.sparse("admin", [s8.at(10, 17, 20)])
    step = pipeline.evaluate(s8.persisted(established.decision))
    incumbent = established.decision.incumbent_shadow_regime
    state = learned.observe_decision(fold_events(established, established.events), established.decision)
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
        for item in compact_high(step, state, ())
        if item.period == P4 and phase_match(item.phase_offset, candidate.phase_offset, P4)
    )
    assert compact.aligned_normal_confidence == pytest.approx(candidate.aligned_normal_confidence, abs=1e-6)
    without = learned.compact_sparse_support(replace(state, superseded=()), period=P4, phase_offset=candidate.phase_offset)
    with_history = learned.compact_sparse_support(state, period=P4, phase_offset=candidate.phase_offset)
    assert with_history.aligned_count < without.aligned_count
    # The 12:00 minute bin holds a superseded-explained restart before an
    # 8 h candidate's domain start and one inside it: conservative case.
    assert_sparse_parity(step, state, prior=prior, conservative=True)


@pytest.mark.parametrize("capacity", [None, 2])
def test_post_establishment_challenger_window_matches_stage6_view(capacity):
    pipeline = s8._boundary_pipeline()
    acquired = pipeline.evaluate()
    state = learned.observe_decision(fold_events(acquired, acquired.events), acquired.decision)
    pipeline.observe("new-four-hour", [s8.at(1, 10), s8.at(1, 14)])
    prior = s8.persisted(acquired.decision)
    step = pipeline.evaluate(prior)
    new = [item for item in step.events if item.event_id not in {e.event_id for e in acquired.events}]
    ring = ()
    if capacity is None:
        state = fold_events(step, new, state)
    else:
        state, ring, _ = stream(new, step.relationships, capacity=capacity, state=state)

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
        for item in learned.compact_candidates(
            state, ring_events=ring, ring_relationships=step.relationships, normal_score=step.score, challenger=True
        )
        if item.period == P4
    ]
    full = [item for item in compact_high(step, state, ring) if item.period == P4]

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


@pytest.mark.parametrize("capacity", [None, 2, 3])
def test_original_cycle_recovery_count_matches_stage6(capacity):
    pipeline = s8.three_hour_chain(4)
    established = s8.settled(pipeline)
    pipeline.observe("later", [s8.at(3, 9) + index * P3 for index in range(6)])
    step = pipeline.evaluate(s8.persisted(established.decision))
    state = learned.observe_decision(fold_events(established, established.events), established.decision)
    new = [item for item in step.events if item.event_id not in {e.event_id for e in established.events}]
    if capacity is None:
        folded, ring = fold_events(step, new, state), ()
    else:
        folded, ring, _ = stream(new, step.relationships, capacity=capacity, state=state)
    incumbent = established.decision.incumbent_shadow_regime
    event_map = {item.event_id: item for item in step.events}

    for boundary in (s8.at(0, 0), s8.at(3, 9), s8.at(3, 10), s8.at(3, 13), s8.at(3, 19), s8.at(3, 22)):
        context = SimpleNamespace(
            authorization_state=authority.ChallengerAuthorization.AUTHORIZED, last_evidence_at=boundary
        )
        expected = min(
            2,
            len(
                authority._original_cycle_recovery_confirmations(
                    incumbent=incumbent, context=context, relationships=step.relationships, event_map=event_map
                )
            ),
        )
        assert learned.compact_recovery_confirmations(
            folded, challenger_last_evidence_at=boundary, ring_events=ring, ring_relationships=step.relationships
        ) == expected
    assert len(folded.epoch.recovery) <= authority.ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS


# --------------------------------------------------------------------------
# Persistence across inactivity, replay protection
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
    state = fold_divisors(fold_events(step, step.events), step)
    state = learned.observe_decision(state, s8.settled(_mixed_sparse_and_chain()).decision)
    reopened = learned.learned_state_from_json_bytes(learned.learned_state_json_bytes(state))

    assert reopened == state
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
    state = fold_divisors(fold_events(step, step.events), step)
    assert {(item.period, item.divisor) for item in state.divisors} == {(P6, 2 * HOUR), (P6, P3)}
    expired = scoring.RestartScheduleScorer().score(
        step.events,
        scoring.CoverageTimeline(()),
        now=step.now + 180 * scoring.DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )

    assert authority._harmonic_blockers(P6, step.score.candidate(P6), ()) == ((), ())
    assert authority._harmonic_blockers(P6, expired.candidate(P6), ())[1]
    assert learned.compact_harmonic_blockers(state, P6, expired.candidate(P6)) == ((), ())
    six = [item for item in learned.compact_candidates(state, normal_score=expired) if item.period == P6]
    assert six and six[0].h3_gate
    assert learned.compact_harmonic_blockers(replace(state, divisors=()), P6, expired.candidate(P6)) == (
        authority._harmonic_blockers(P6, expired.candidate(P6), ())
    )


def test_genuine_misses_fold_once_and_resolve_harmonics():
    miss = s8.genuine_miss(2 * HOUR, s8.at(0, 2), key="m1")
    second = s8.genuine_miss(2 * HOUR, s8.at(0, 4), key="m2")
    state = learned.fold_windows(learned.empty_learned_state(s8.SERVER), [miss])

    assert learned.fold_windows(state, [miss]) == state
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

    assert fold_events(step, step.events, state) == state
    late = s8.physical_event(
        99, s8.at(2, 3), s8.Session("late", s8.at(2, 2), s8.at(2, 4)), outcome=Outcome.CORROBORATED_OFFLINE_RESTART
    )
    assert learned.fold_unit(state, events=[late]) == state
    phase = learned.compact_phase_proposal(state, period=P3)[0]
    alone = learned.compact_sparse_support(state, period=P3, phase_offset=phase)
    assert alone == learned.compact_sparse_support(state, period=P3, phase_offset=phase, ring_events=step.events)
    newer = s8.physical_event(
        100, s8.at(9, 6), s8.Session("newer", s8.at(9, 5), s8.at(9, 7)), outcome=Outcome.CORROBORATED_OFFLINE_RESTART
    )
    advanced = learned.fold_unit(state, events=[step.events[0], newer])
    assert advanced.watermark.folded_event_count == state.watermark.folded_event_count + 1
    assert sum(item.count for item in advanced.pool) == sum(item.count for item in state.pool) + 1


def test_replayed_long_chain_through_anchor_counts_once():
    step = _continuous_chain(30).evaluate()
    state, ring, _ = stream(step.events, step.relationships, capacity=4)
    again, ring_again, _ = stream(step.events, step.relationships, capacity=4, state=state, ring=ring)

    assert again == state and ring_again == ring


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


def test_epoch_resets_on_new_incumbent_and_divisors_reset_on_supersession():
    pipeline, replaced = s8._superseded_three_hour()
    original = s8.settled(s8.Pipeline(events=list(pipeline.events[:7]), coverage=list(pipeline.coverage[:1])))
    step = pipeline.evaluate(s8.persisted(replaced.decision))
    six = _six_hour_chain().evaluate()
    state = learned.observe_decision(fold_divisors(learned.empty_learned_state(s8.SERVER), six), original.decision)
    assert state.divisors and state.epoch.regime_id == original.decision.incumbent_shadow_regime.regime_id
    changed = learned.observe_decision(state, step.decision)

    assert changed.incumbent.regime_id != state.incumbent.regime_id
    assert changed.epoch.regime_id == changed.incumbent.regime_id
    assert changed.epoch.challenger == () and changed.epoch.recovery == ()
    assert changed.divisors == ()
    superseded_at = max(item.superseded_at for item in changed.superseded)
    assert changed.divisor_floor_at == superseded_at
    # A schema-3 window that started before the boundary cannot re-establish it.
    assert learned.fold_divisor_resolution(changed, six.score, recent_events(six.events)).divisors == ()


# --------------------------------------------------------------------------
# Storage bounds
# --------------------------------------------------------------------------


def test_storage_is_bounded_regardless_of_observation_count():
    rng = random.Random(20261106)
    pipeline = s8.Pipeline()
    pipeline.sparse("random", sorted(s8.at(day, rng.randrange(24), rng.randrange(60)) for day in range(3000)))
    state = learned.empty_learned_state(s8.SERVER)
    for event in pipeline.events:
        state = learned.fold_unit(state, events=[event])

    assert state.watermark.folded_learning_event_count == 3000
    assert len(state.pool) <= learned.DAY_BINS
    assert learned.learned_state_entry_count(state) <= learned.LEARNED_STATE_MAX_ENTRIES
    size = len(learned.learned_state_json_bytes(state))
    doubled = state
    for index, event in enumerate(pipeline.events):
        doubled = learned.fold_unit(
            doubled,
            events=[replace(event, event_id=f"repeat-{index}", canonical_phase_at=event.canonical_phase_at + 4000 * scoring.DAY)],
        )
    assert learned.learned_state_entry_count(doubled) == learned.learned_state_entry_count(state)
    assert len(learned.learned_state_json_bytes(doubled)) < size * 1.1
    for period in scoring.CANDIDATE_PERIODS:
        learned.compact_phase_proposal(doubled, period=period)


def test_entry_bound_is_compact():
    # Pool, follower presence (burst independence), six interval-bin key
    # spaces (including schema-5 verification), divisors, recovery, open-run
    # residuals, incumbent and chain.
    assert learned.LEARNED_STATE_MAX_ENTRIES == 1440 + 1440 + 6 * 420 + 9 + 2 + 721 + 2
    assert learned.LEARNED_STATE_MAX_ENTRIES < 6500


def _maximal_state():
    """Every key space full, every number at a long float representation."""

    long = 1.2345678901234567e-05
    time_value = 1900594800.1234567
    tags = tuple(learned.EndpointTag(period, 0.5, 1) for period in scoring.CANDIDATE_PERIODS)
    pool = tuple(
        learned.PhaseBin(
            index, 1.0 + long, 10**9, long, 59.123456789012345, time_value, time_value + 1,
            long, 299.12345678901234, 0.0, tags, learned.EndpointTag(P3, 0.5, 1),
        )
        for index in range(learned.DAY_BINS)
    )
    followers = tuple(
        learned.FollowerBin(
            index, 10**9, long, 59.123456789012345, time_value, time_value + 1,
            long, 299.12345678901234,
        )
        for index in range(learned.DAY_BINS)
    )
    streak = learned.StreakSummary(10**9, 10**9 + 1, long, 1234.5678901234567, long, time_value, time_value)

    def interval_bins():
        return tuple(
            learned.IntervalBin(
                period, index, 10**9, long, 299.12345678901234, long, 10**9, long,
                time_value, time_value, 10**9, 1234.5678901234567, 1234.5678901234567,
                -long, 1234.5678901234567, streak,
            )
            for period in scoring.CANDIDATE_PERIODS
            for index in range(period // learned.INTERVAL_BIN_SECONDS)
        )

    misses = tuple(
        learned.MissBin(period, index, long, 299.12345678901234, 10**9, time_value, time_value)
        for period in scoring.CANDIDATE_PERIODS
        for index in range(period // learned.INTERVAL_BIN_SECONDS)
    )
    superseded = tuple(
        learned.SupersededRegime(period, index * learned.INTERVAL_BIN_SECONDS + long, 900.1234567890123, time_value)
        for period in scoring.CANDIDATE_PERIODS
        for index in range(period // learned.INTERVAL_BIN_SECONDS)
    )
    residuals = tuple(
        learned.RunResidual(key, 10**9, -2.4999999999999996, 2.4999999999999996)
        for key in range(-learned.RUN_RESIDUAL_KEY_RADIUS, learned.RUN_RESIDUAL_KEY_RADIUS + 1)
    )
    text = "x" * learned.MAX_STRING_LENGTH
    return learned.LearnedState(
        schema_version=learned.LEARNED_STATE_SCHEMA_VERSION,
        server_key=text,
        watermark=learned.FoldWatermark(time_value, text, 10**15, 10**15, time_value),
        pool_weight_total=10**12 + long,
        pool=pool,
        followers=followers,
        intervals=interval_bins(),
        misses=misses,
        divisors=tuple(
            learned.DivisorEvidence(period, divisor, 10**9, 10**9, 1234.5678901234567, time_value)
            for period, divisor in learned.DIVISOR_KEYS
        ),
        normal_relationships=tuple(
            learned.NormalBin(period, index, 10**9, long, 299.12345678901234, time_value, time_value)
            for period in scoring.CANDIDATE_PERIODS
            for index in range(period // learned.INTERVAL_BIN_SECONDS)
        ),
        divisor_floor_at=time_value,
        incumbent=learned.RegimeKnowledge(
            text, P3, 1234.5678901234567, 540.1234567890123, text[:32], text[:32], text[:32],
            0.97, 0.97, 0.97, time_value, time_value, (text,) * 3, (text,) * learned.MAX_REASON_CODES,
        ),
        superseded=superseded,
        epoch=learned.EpochEvidence(text, time_value, interval_bins(), 10**9, long, (
            learned.RecoveryInterval(time_value, time_value + 1),
            learned.RecoveryInterval(time_value + 2, time_value + 3),
        ), long),
        verifications=tuple(
            learned.VerificationBin(
                period, index, 10**9, long, 299.12345678901234, time_value, time_value + 1
            )
            for period in scoring.CANDIDATE_PERIODS
            for index in range(period // learned.INTERVAL_BIN_SECONDS)
        ),
        open_chain=learned.OpenChain(
            text, text, time_value, 1439, 1.0, tuple(scoring.CANDIDATE_PERIODS), True,
            learned.OpenRun(P3, 10**9, long, time_value, time_value, 1234.5678901234567, residuals, -long, long),
        ),
    )


def test_maximal_state_respects_the_entry_and_byte_bounds():
    state = _maximal_state()
    payload = learned.learned_state_json_bytes(state)

    assert learned.learned_state_entry_count(state) == learned.LEARNED_STATE_MAX_ENTRIES
    assert len(payload) <= learned.LEARNED_STATE_MAX_JSON_BYTES
    assert learned.learned_state_from_json_bytes(payload) == state


def _typical_states():
    sparse = s8.settled(s8.sparse_pipeline())
    chain = _continuous_chain(1000).evaluate()
    chain_state, _ring, _ = stream(chain.events, chain.relationships, capacity=learned.RECENT_RING_CAPACITY, batch=25)
    return {
        "sparse": learned.observe_decision(fold_events(sparse, sparse.events), sparse.decision),
        "continuous_1000": chain_state,
    }


def test_typical_states_are_small():
    for name, state in _typical_states().items():
        assert len(learned.learned_state_json_bytes(state)) < 4096, name


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.update(schema_version=1), id="version"),
        pytest.param(lambda data: data["pool"].append(list(data["pool"][0])), id="duplicate_bin"),
        pytest.param(lambda data: data["pool"][0].__setitem__(1, float("nan")), id="nan"),
        pytest.param(lambda data: data["pool"][0].__setitem__(2, True), id="bool_count"),
        pytest.param(lambda data: data["pool"][0].__setitem__(0, learned.DAY_BINS), id="bin_range"),
        pytest.param(lambda data: data["pool"][0].__setitem__(3, 61.0), id="offset_range"),
        pytest.param(lambda data: data["pool"][0].__setitem__(10, [[0, 99.0, 1]]), id="tag_overweight"),
        pytest.param(lambda data: data["watermark"].__setitem__(1, None), id="half_watermark"),
        pytest.param(
            lambda data: data.update(pool=[[index, *data["pool"][0][1:]] for index in range(learned.DAY_BINS + 1)]),
            id="oversized",
        ),
        pytest.param(lambda data: data["epoch"].update(recovery=[[1, 2]] * 3), id="recovery_bound"),
        pytest.param(
            lambda data: data["open_chain"]["run"][8].append([10**6, 1, 0.0, 0.0]),
            id="residual_key_range",
        ),
    ],
)
def test_validation_rejects_malformed_or_unbounded_state(mutate):
    step = _continuous_chain(12).evaluate()
    state, _ring, _ = stream(step.events, step.relationships, capacity=3)
    data = json.loads(learned.learned_state_json_bytes(state))
    mutate(data)

    with pytest.raises(learned.LearnedStateError):
        learned.learned_state_from_mapping(data)


def test_oversized_payload_is_refused_before_parsing():
    with pytest.raises(learned.LearnedStateError):
        learned.learned_state_from_json_bytes(b" " * (learned.LEARNED_STATE_MAX_JSON_BYTES + 1))
