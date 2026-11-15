"""Burst independence (A1): sparse evidence counts independent occurrences.

A sparse-usable restart is an independently counted *onset* when it starts
strictly more than ``min(CANDIDATE_PERIODS) / 2`` (3600 s) after the last
counted onset; restarts inside that anchored window are *followers* with no
sparse weight, aligned or off-phase.  Followers keep their identity,
classification and every non-sparse use (suspicion, isolation, Time Shift
Detection, continuity, alerts).  The compact learned state applies the same
rule at fold time and keeps folded followers as presence-only bins.

Synthetic data only; no runtime, profile, learning file or network.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_restart_learned_state as compact_tests
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
MINUTE = 60
P2 = 2 * HOUR
P3 = 3 * HOUR
P4 = 4 * HOUR
WINDOW = authority.SPARSE_OCCURRENCE_WINDOW_SECONDS
Outcome = s8.Outcome


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def sparse_events(times, prefix="r", outcome=Outcome.CORROBORATED_OFFLINE_RESTART):
    pipeline = s8.Pipeline()
    return pipeline.sparse(prefix, times, outcome=outcome)


def onset_ids(events, **kwargs):
    return [item.event_id for item in authority.sparse_occurrence_onsets(events, **kwargs)[0]]


def support(events, period, phase):
    """The implemented (A1) sparse statistic."""

    return authority._sparse_support(
        period=period,
        phase_offset=phase,
        sparse=authority._sparse_evidence(tuple(events), None),
        excluded_event_ids=frozenset(),
    )


def every_event_support(events, period, phase):
    """The pre-A1 statistic: every sparse-usable restart independent."""

    usable = tuple(item for item in events if authority.sparse_usable_event(item))
    return authority._sparse_support(
        period=period,
        phase_offset=phase,
        sparse=authority._SparseEvidence(events=usable, historical=()),
        excluded_event_ids=frozenset(),
    )


def clean_history():
    """F: ten aligned 3 h restarts (phase 00:00) on ten separate days."""

    return s8.aligned_3h(10)


def burst_times(day=4, hour=15, minute=40, count=20, spacing=150):
    """A 50-minute off-phase burst (20 restarts, 2.5 min apart)."""

    start = s8.at(day, hour, minute)
    return [start + index * spacing for index in range(count)]


def crash_loop_times(day=6, hour=9, minute=20, count=36, spacing=10 * MINUTE):
    """A 6-hour crash loop, one restart every 10 minutes."""

    start = s8.at(day, hour, minute)
    return [start + index * spacing for index in range(count)]


def settled_with(pipeline):
    return s8.settled(pipeline)


# --------------------------------------------------------------------------
# The shared occurrence rule
# --------------------------------------------------------------------------


def test_window_is_half_the_shortest_candidate_period():
    assert WINDOW == min(scoring.CANDIDATE_PERIODS) / 2 == 3600
    # Never merges two scheduled occurrences of any learnable schedule, even
    # with the largest phase jitter the sparse statistic accepts.
    worst = min(scoring.CANDIDATE_PERIODS) - 2 * (
        scoring.candidate_phase_tolerance(min(scoring.CANDIDATE_PERIODS)) + scoring.PHASE_UNCERTAINTY_CAP
    )
    assert worst > WINDOW


def test_boundary_is_strict_and_followers_do_not_extend_the_window():
    base = s8.at(1, 10)
    events = sparse_events([base, base + 3599, base + 3600, base + 3601, base + 3601 + 3600])
    ids = [item.event_id for item in events]
    # +3599 and +3600 are followers (not strictly more than 3600 s); +3601
    # opens the next occurrence; +7201 is exactly 3600 s after that onset.
    assert onset_ids(events) == [ids[0], ids[3]]


def test_anchored_window_keeps_a_long_crash_loop_contributing_onsets():
    events = sparse_events(crash_loop_times())
    onsets = authority.sparse_occurrence_onsets(events)[0]
    # 10-minute restarts: an onset every 70 minutes, never one per loop.
    assert len(onsets) == 6
    gaps = [b.canonical_phase_at - a.canonical_phase_at for a, b in zip(onsets, onsets[1:])]
    assert gaps == [70 * MINUTE] * 5


def test_order_independence_and_tie_break_by_event_id():
    times = burst_times() + list(clean_history())
    events = sparse_events(times)
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)
    assert onset_ids(shuffled) == onset_ids(events)
    same = sparse_events([s8.at(2, 9), s8.at(2, 9)], prefix="tie")
    assert onset_ids(same) == [min(item.event_id for item in same)]
    duplicate = onset_ids([*events, *events])
    assert duplicate == onset_ids(events)


@pytest.mark.parametrize(
    "spoil",
    [
        pytest.param(lambda event: replace(event, coverage_complete=False), id="incomplete_coverage"),
        pytest.param(
            lambda event: replace(event, lifecycle_interruption=detection.LifecycleMarker.SLEEP_GAP),
            id="local_connectivity_neutralised",
        ),
        pytest.param(
            lambda event: replace(event, outcome=Outcome.SERVICE_INTERRUPTION, authenticity=0.2),
            id="service_interruption",
        ),
        pytest.param(
            lambda event: replace(event, outcome=Outcome.AMBIGUOUS_DRAIN, authenticity=0.35),
            id="ambiguous_drain",
        ),
        pytest.param(lambda event: replace(event, authenticity=0.4), id="low_authenticity"),
    ],
)
def test_ineligible_incidents_never_open_or_extend_an_occurrence(spoil):
    base = s8.at(1, 10)
    first, blocker, second, third = sparse_events(
        [base, base + 50 * MINUTE, base + 65 * MINUTE, base + 105 * MINUTE]
    )
    spoiled = spoil(blocker)
    assert not authority.sparse_usable_event(spoiled)
    # An ineligible incident before any restart does not open a window ...
    assert onset_ids([spoiled, second]) == [second.event_id]
    # ... and inside a window it does not extend it: +65 min is counted
    # because it is more than 3600 s after the onset, not after the incident.
    assert onset_ids([first, spoiled, second, third]) == [first.event_id, second.event_id]


def test_last_counted_onset_carries_across_a_boundary():
    base = s8.at(1, 10)
    early, follower, late = sparse_events([base, base + 30 * MINUTE, base + 61 * MINUTE])
    assert onset_ids([follower, late], last_onset_at=early.canonical_phase_at) == [late.event_id]
    assert onset_ids([follower, late]) == [follower.event_id]


# --------------------------------------------------------------------------
# Synthetic cases D1-D3, E, F, G, X (raw sparse statistic)
# --------------------------------------------------------------------------


def test_f_sporadic_independent_observations_are_unchanged():
    events = sparse_events(clean_history())
    assert onset_ids(events) == [item.event_id for item in events]
    now, before = support(events, P3, 0.0), every_event_support(events, P3, 0.0)
    assert now == before
    assert now.value == pytest.approx(0.95)


def test_d1_fifty_minute_burst_counts_as_one_correlated_contradiction():
    events = sparse_events([*clean_history(), *burst_times()])
    before = every_event_support(events, P3, 0.0)
    now = support(events, P3, 0.0)

    assert before.off_phase_weight == pytest.approx(20.0)
    assert before.value == 0.0
    assert now.effective == pytest.approx(10.0)
    assert now.off_phase_weight == pytest.approx(1.0)
    assert now.consistency == pytest.approx(10 / 11, abs=1e-6)
    # Stage 2: one correlated contradiction is within the one-anomaly
    # allowance (net nine occurrences, saturated).
    assert now.within_tolerance
    assert now.phase_confidence == pytest.approx(0.95)
    assert now.value == pytest.approx(0.95)
    # Every burst restart is still a persisted, classified, authentic event.
    assert all(authority._authentic_event(item) for item in events)


def test_d2_six_hour_crash_loop_keeps_multiple_independent_contradictions():
    events = sparse_events([*clean_history(), *crash_loop_times()])
    now = support(events, P3, 0.0)
    before = every_event_support(events, P3, 0.0)

    assert before.value == 0.0
    # Onsets 09:20, 10:30, 11:40, 12:50, 14:00, 15:10: all off the 3 h phase
    # (the loop's aligned 12:00 and 15:00 restarts are followers).
    assert now.off_phase_weight == pytest.approx(6.0)
    assert now.effective == pytest.approx(10.0)
    assert now.consistency == pytest.approx(10 / 16)
    # Six contradictions exceed the allowance: the proportional fall applies
    # and the value stays below the identification milestone.
    assert not now.within_tolerance
    assert 0.0 < now.value < 0.9
    # Still well below the sparse establishment precision (SE3).
    assert now.phase_confidence < authority.SPARSE_PHASE_CONFIDENCE_MINIMUM


def test_d3_crash_loop_alone_cannot_establish_a_cycle():
    loop = crash_loop_times(day=1)
    events = sparse_events(loop)
    for period in scoring.CANDIDATE_PERIODS:
        for anchor in loop:
            value = support(events, period, anchor % period)
            assert value.phase_confidence < authority.SPARSE_PHASE_CONFIDENCE_MINIMUM, period
    pipeline = s8.Pipeline()
    pipeline.observe("loop", loop)
    step = settled_with(pipeline)
    assert step.decision.incumbent_shadow_regime is None
    assert not s8.established(step)


def g_times():
    """Four discriminating 3 h occurrences, each followed by two quick
    re-restarts (2.5 and 5 minutes later)."""

    slots = [s8.at(0, 6), s8.at(1, 9), s8.at(2, 12), s8.at(3, 15)]
    return [when + offset for when in slots for offset in (0, 150, 300)]


def test_g_quick_restarts_cannot_inflate_aligned_evidence():
    events = sparse_events(g_times())
    before = every_event_support(events, P3, 6 * HOUR)
    now = support(events, P3, 6 * HOUR)

    # Four occurrences are no longer counted as twelve: exactly the support
    # of the four occurrences alone (Stage 2: the identification requirement).
    clean = support(sparse_events([s8.at(0, 6), s8.at(1, 9), s8.at(2, 12), s8.at(3, 15)]), P3, 6 * HOUR)
    assert before.effective == pytest.approx(12.0)
    assert now.effective == pytest.approx(4.0) == clean.effective
    assert now.value == pytest.approx(clean.value)
    assert now.effective == authority.sparse_identification_occurrences(P3) < before.effective


def test_g_four_occurrences_with_rerestarts_do_not_establish():
    # Stage 2: four independent occurrences identify the cycle exactly as
    # four clean ones do (the re-restarts add nothing); without a witnessed
    # interval it only awaits verification.
    pipeline = s8.Pipeline()
    pipeline.sparse("g", g_times())
    step = settled_with(pipeline)
    clean = s8.Pipeline()
    clean.sparse("clean", [s8.at(0, 6), s8.at(1, 9), s8.at(2, 12), s8.at(3, 15)])
    reference = settled_with(clean)
    assert not s8.established(step)
    assert step.decision.state is reference.decision.state is authority.RegimeState.LIKELY
    assert authority.AWAITING_VERIFICATION_REASON in step.decision.reason_codes
    assert step.decision.combined_confidence == reference.decision.combined_confidence


def test_x_unscheduled_restart_just_before_a_scheduled_one_is_conservative():
    history = list(clean_history())
    scheduled = history[3]
    events = sparse_events([*history, scheduled - 14 * MINUTE])
    now = support(events, P3, 0.0)
    # The scheduled restart becomes a follower of the earlier unscheduled
    # one: one aligned observation is lost (never gained).
    assert now.effective == pytest.approx(9.0)
    assert now.off_phase_weight == pytest.approx(1.0)
    assert now.value == pytest.approx(0.95)


def test_e_new_period_observed_sparsely_keeps_every_distinct_occurrence():
    old = list(clean_history())
    new = [s8.at(day, (1 + 4 * (day % 6)) % 24) for day in range(12, 22)]
    followers = [when + 3 * MINUTE for when in new]
    events = sparse_events([*old, *new, *followers])
    without = sparse_events([*old, *new], prefix="plain")
    for period, phase in ((P4, HOUR), (P3, 0.0)):
        assert support(events, period, phase).effective == pytest.approx(
            support(without, period, phase).effective
        )
        assert support(events, period, phase).value == pytest.approx(
            support(without, period, phase).value
        )


# --------------------------------------------------------------------------
# End to end (raw authority)
# --------------------------------------------------------------------------


def test_burst_counts_exactly_like_one_unscheduled_restart():
    """Eight discriminating sparse restarts plus a 50-minute burst behave as
    eight plus a single admin restart (Stage 2: identified, awaiting
    verification at 79 %)."""

    burst = s8.sparse_pipeline()
    burst.observe("burst", burst_times(day=7, hour=10, minute=40))
    single = s8.sparse_pipeline()
    single.sparse("admin", [burst_times(day=7, hour=10, minute=40)[0]])
    left, right = settled_with(burst), settled_with(single)

    assert s8.percent(left) == s8.percent(right) == 79
    assert not s8.established(left) and not s8.established(right)
    assert left.candidate(P3, 6 * HOUR).aligned_normal_confidence == pytest.approx(
        right.candidate(P3, 6 * HOUR).aligned_normal_confidence
    )


def test_burst_no_longer_blocks_sparse_establishment():
    pipeline = s8.verify(s8.Pipeline())
    pipeline.sparse("aligned", clean_history())
    # Mid-history, followed by aligned restarts (the real-world shape).
    pipeline.observe("burst", burst_times())
    before = every_event_support(pipeline.events, P3, 0.0)
    step = settled_with(pipeline)

    assert before.value == 0.0
    assert s8.established(step)
    regime = step.decision.incumbent_shadow_regime
    assert regime.candidate_period_seconds == P3
    assert authority.SPARSE_ESTABLISHMENT_REASON in regime.reason_codes
    assert s8.percent(step) == 80


def test_crash_loop_after_sparse_history_still_blocks_establishment():
    pipeline = s8.sparse_pipeline()
    pipeline.observe("loop", crash_loop_times(day=7, hour=9, minute=20))
    step = settled_with(pipeline)
    assert step.decision.incumbent_shadow_regime is None
    assert not s8.established(step)


def test_burst_after_establishment_still_counts_for_suspicion_and_isolation():
    """Stage 8.7a and suspicion read every authentic restart: a burst after a
    sparse establishment is level-one suspicion and never isolated, so the
    sparse countdown is withheld until the next aligned restart."""

    pipeline = s8.verified_sparse_pipeline()
    first = settled_with(pipeline)
    assert s8.established(first)
    pipeline.observe("burst", burst_times(day=7, hour=10, minute=40))
    step = pipeline.evaluate(s8.persisted(first.decision))

    assert step.decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in step.decision.reason_codes
    assert len(step.decision.active_suspicion_evidence_ids) == 20
    assert not step.consumer().countdown_visible
    assert step.decision.incumbent_shadow_regime.regime_id == first.decision.incumbent_shadow_regime.regime_id


def test_time_shift_detection_is_unaffected_by_followers():
    """Three shifted restarts (separate days) each followed 3 minutes later
    by a quick re-restart: the shift is adopted exactly as without them."""

    def scenario(with_followers):
        pipeline = s8.verified_sparse_pipeline()
        established = list(pipeline.events)
        shifted = [s8.at(8, 7), s8.at(9, 10), s8.at(10, 13)]
        batches = [established]
        for index, when in enumerate(shifted):
            times = [when, when + 3 * MINUTE] if with_followers else [when]
            batches.append(pipeline.observe(f"shift-{index}", times))
        return compact_authority.raw_steps(pipeline, batches)[-1]

    plain, noisy = scenario(False), scenario(True)
    for step in (plain, noisy):
        assert authority.TIME_SHIFT_ADOPTED_REASON in step.decision.incumbent_shadow_regime.reason_codes
    left, right = plain.decision.incumbent_shadow_regime, noisy.decision.incumbent_shadow_regime
    assert right.phase_offset == pytest.approx(left.phase_offset)
    assert right.established_at == left.established_at
    assert right.candidate_period_seconds == P3


# --------------------------------------------------------------------------
# Compact learned state: fold-time rule, presence, parity
# --------------------------------------------------------------------------


def test_fold_time_pool_holds_onsets_and_followers_keep_presence():
    events = sparse_events([*clean_history(), *burst_times()])
    state = learned.empty_learned_state(s8.SERVER)
    for event in sorted(events, key=lambda item: item.canonical_phase_at):
        state = learned.fold_unit(state, events=[event])

    assert sum(item.count for item in state.pool) == 11
    assert state.pool_weight_total == pytest.approx(11.0)
    assert sum(item.count for item in state.followers) == 19
    assert state.watermark.folded_learning_event_count == 30
    compact = learned.compact_sparse_support(state, period=P3, phase_offset=0.0)
    raw = support(events, P3, 0.0)
    assert compact.value == pytest.approx(raw.value)
    assert compact.effective == pytest.approx(raw.effective)
    assert compact.off_phase_weight == pytest.approx(raw.off_phase_weight)
    assert compact.phase_confidence == pytest.approx(raw.phase_confidence)


def test_folded_followers_stay_visible_to_incumbent_presence_helpers():
    onset, follower = sparse_events([s8.at(1, 6), s8.at(1, 6, 25)])
    state = learned.fold_unit(learned.empty_learned_state(s8.SERVER), events=[onset, follower])
    assert [item.count for item in state.pool] == [1]
    assert [item.count for item in state.followers] == [1]
    tolerance = scoring.candidate_phase_tolerance(P3)

    # The follower (25 min off the 06:00 phase) is a folded off-phase restart.
    assert learned.compact_folded_off_phase_ids(
        state, period=P3, phase_offset=0.0, tolerance=tolerance, since=onset.canonical_phase_at
    ) == (f"learned-off-phase:{learned._day_bin(follower.canonical_phase_at)[0]}",)
    assert learned.compact_folded_restart_after(
        state, period=P3, phase_offset=0.0, tolerance=tolerance,
        after=onset.canonical_phase_at, aligned=False,
    )
    # ... and a later restart at its phase is not an isolated anomaly.
    regime = s8.settled(s8.sparse_pipeline()).decision.incumbent_shadow_regime
    regime = replace(regime, phase_offset=0.0)
    knowledge = learned.knowledge_for_regime(state, regime)
    later = sparse_events([s8.at(2, 6, 25)], prefix="later")[0]
    assert not learned.compact_isolated_off_phase_restart(knowledge, later)
    # Last aligned evidence for a regime at the follower's own phase.
    shifted = replace(regime, phase_offset=(6 * HOUR + 25 * MINUTE) % P3)
    assert learned._folded_last_aligned(state, shifted) == follower.canonical_phase_at
    # It contributes nothing to the sparse statistic.
    assert learned.compact_sparse_support(
        state, period=P3, phase_offset=(6 * HOUR + 25 * MINUTE) % P3
    ).effective == 0.0


def test_ring_continues_the_occurrence_sequence_from_the_folded_onset():
    onset, ring_follower, ring_onset = sparse_events(
        [s8.at(1, 6), s8.at(1, 6, 30), s8.at(1, 7, 1)]
    )
    state = learned.fold_unit(learned.empty_learned_state(s8.SERVER), events=[onset])
    assert learned.last_counted_onset_at(state) == onset.canonical_phase_at
    assert [item.event_id for item in learned.ring_sparse_events(state, [ring_follower, ring_onset])] == [
        ring_onset.event_id
    ]


def burst_history():
    """Sparse 3 h history, a 20-restart burst in one session, more sparse."""

    pipeline = s8.sparse_pipeline()
    # Verified acquisition: a witnessed interval after the sparse restarts.
    s8.verify(pipeline, (s8.at(6, 18), s8.at(6, 21)))
    pipeline.observe("burst", burst_times(day=7, hour=10, minute=40))
    pipeline.sparse("after", [s8.at(8, 18), s8.at(9, 0), s8.at(10, 9)])
    return pipeline


@pytest.mark.parametrize("capacity", [2, 5, 12, 23, 40])
def test_compact_only_runner_matches_raw_when_folding_inside_and_after_a_burst(capacity):
    pipeline = burst_history()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    # The first burst restart follows the eight sparse restarts and the
    # witnessed interval (2).
    burst_chain = pipeline.events[10].continuity_chain_id
    burst_ids = {item.event_id for item in pipeline.events if item.continuity_chain_id == burst_chain}
    assert len(burst_ids) == 20
    inside = after = False
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        # Stage 4: the established display may read lower (never higher) on
        # the compact path once replication onsets fold into minute bins
        # straddling the establishment boundary; everything else is equal.
        compact_authority.assert_same_display_bounded(raw[index], compact, (capacity, index))
        ring_ids = {item.event_id for item in runner.ring}
        # A ring smaller than the burst chain folds a prefix of it (the
        # boundary falls inside the burst); later steps fold it entirely.
        inside |= bool(burst_ids - ring_ids) and bool(burst_ids & ring_ids)
        after |= burst_ids.isdisjoint(ring_ids)
    assert s8.established(raw[-1])
    followers = sum(item.count for item in runner.state.followers)
    if capacity < 20:
        assert inside and after
        assert followers == 19  # the burst onset is in the pool, 19 followers folded
    else:
        assert followers == 0  # the burst never left the ring


def _isolation_scenario():
    """A scheduled restart S followed 25 minutes later by an off-phase
    follower F (one session); two ineligible blips push both out of a
    two-event ring; then an off-phase restart B that is phase-coherent with
    F at 2 h (not at the incumbent's 3 h, so Time Shift Detection keeps them
    apart).  Full history sees F and B after the last aligned restart, so B
    is not an isolated anomaly."""

    pipeline = s8.verified_sparse_pipeline()
    established = list(pipeline.events)
    pair = pipeline.observe("pair", [s8.at(8, 6), s8.at(8, 6, 25)])
    pipeline.sparse("blip", [s8.at(8, 20, 30), s8.at(9, 1, 30)])
    for index in range(len(pipeline.events) - 2, len(pipeline.events)):
        pipeline.events[index] = replace(pipeline.events[index], coverage_complete=False)
    blips = pipeline.events[-2:]
    later = pipeline.sparse("later", [s8.at(9, 8, 25)])
    # Evaluate only once B has arrived: a folded *single* anomaly is a
    # pre-existing, conservative compact limitation unrelated to followers.
    return pipeline, pair, [established, pair, [*blips, *later]]


def _run_isolation(pipeline, batches):
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=2)
    steps = [runner.step(batch) for batch in batches]
    return raw, steps, runner


def test_follower_presence_survives_folding_for_stage_8_7a_isolation():
    pipeline, pair, batches = _isolation_scenario()
    raw, steps, runner = _run_isolation(pipeline, batches)
    for index, compact in enumerate(steps):
        # Stage 4: display bounded as in the folding parity test above.
        compact_authority.assert_same_display_bounded(raw[index], compact, index)
        assert (authority.ISOLATED_OFF_PHASE_RESTART_REASON in compact.decision.reason_codes) is (
            authority.ISOLATED_OFF_PHASE_RESTART_REASON in raw[index].decision.reason_codes
        ), index
    assert pair[1].event_id not in {item.event_id for item in runner.ring}
    assert sum(item.count for item in runner.state.followers) == 1
    assert raw[-1].decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in raw[-1].decision.reason_codes
    assert not raw[-1].consumer().countdown_visible


def test_isolation_parity_depends_on_follower_presence(monkeypatch):
    """Negative control: without folded follower presence the compact path
    would wrongly grant the isolation relief (and the sparse countdown)."""

    monkeypatch.setattr(learned, "_presence_bins", lambda state: tuple(state.pool))
    pipeline, _pair, batches = _isolation_scenario()
    raw, steps, _runner = _run_isolation(pipeline, batches)
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in raw[-1].decision.reason_codes
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in steps[-1].decision.reason_codes


def _production_burst_pipeline():
    pipeline = s8.verify(s8.Pipeline())
    pipeline.sparse("aligned", s8.aligned_3h(50))
    pipeline.observe("burst", burst_times(day=50, hour=10, minute=40))
    # Enough later restarts for the burst chain to leave the 80-event ring.
    pipeline.sparse("after", s8.aligned_3h(85, start_day=51))
    return pipeline


def test_production_rebuild_raw_and_compact_agree_across_retention(monkeypatch):
    factory = _production_burst_pipeline
    raw = compact_authority.run(factory(), every=10, monkeypatch=monkeypatch)
    compact = compact_authority.run(factory(), every=10, compact_from=0, monkeypatch=monkeypatch)
    assert len(factory().events) > learned.RECENT_RING_CAPACITY
    for index, (left, right) in enumerate(zip(raw, compact)):
        assert compact_authority.decision_summary(
            schema4.persisted_authority_decision(right)
        ) == compact_authority.decision_summary(schema4.persisted_authority_decision(left)), index
        assert compact_authority.consumer_summary(integration.record_step(right)) == (
            compact_authority.consumer_summary(integration.record_step(left))
        ), index
        assert not schema4._validate_server_record(s8.SERVER, right)
    final = schema4.persisted_learned_state(compact[-1])
    assert final.schema_version == learned.LEARNED_STATE_SCHEMA_VERSION == 5
    assert sum(item.count for item in final.followers) == 19
    assert s8.established(integration.record_step(compact[-1]))


def test_repeated_rebuild_and_reopen_are_fixed_points(monkeypatch):
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", True)
    pipeline = _production_burst_pipeline()
    record = integration.drive(pipeline, every=10)
    again = integration.rebuild(record, pipeline.events, pipeline.coverage)
    reopened = json.loads(schema4.canonical_json_bytes(again))
    third = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == record["authority_decision"]
    assert third["authority_decision"] == again["authority_decision"]
    assert third["learned_state"] == again["learned_state"]


# --------------------------------------------------------------------------
# Schema 4 and backward compatibility
# --------------------------------------------------------------------------


def _legacy_mapping(state, version):
    """The mapping an older build persisted: schema 4 has no verification
    bins and a three-value epoch; schema 3 also has no followers."""

    data = learned.learned_state_to_mapping(state)
    data["schema_version"] = version
    data.pop("verifications")
    data["epoch"]["values"] = data["epoch"]["values"][:3]
    if version == 3:
        del data["followers"]
    return data


def _state_with_followers():
    events = sparse_events([*clean_history(), *burst_times()])
    state = learned.empty_learned_state(s8.SERVER)
    for unit in learned.chain_fold_units(events):
        state = learned.fold_unit(state, events=unit)
    assert state.followers
    return state


def test_schema_4_round_trip_keeps_followers():
    state = _state_with_followers()
    payload = learned.learned_state_json_bytes(state)
    data = json.loads(payload)
    assert data["schema_version"] == learned.LEARNED_STATE_SCHEMA_VERSION
    assert len(data["followers"]) == len(state.followers)
    assert learned.learned_state_from_json_bytes(payload) == state


def test_schema_3_state_loads_unchanged_as_legacy():
    state = _state_with_followers()
    data = _legacy_mapping(replace(state, followers=()), 3)
    loaded = learned.learned_state_from_mapping(json.loads(json.dumps(data)))

    assert loaded.followers == ()
    assert loaded.pool == state.pool
    assert loaded.pool_weight_total == state.pool_weight_total
    assert loaded.watermark == state.watermark
    # Upgraded in memory only by the empty presence (and verification)
    # collections.
    assert loaded.schema_version == learned.LEARNED_STATE_SCHEMA_VERSION
    assert loaded.verifications == ()
    assert learned.learned_state_to_mapping(loaded)["followers"] == []
    # A legacy pool's latest restart is the conservative carried onset.
    assert learned.last_counted_onset_at(loaded) == max(item.last_at for item in state.pool)


def test_legacy_folded_bins_are_never_reinterpreted():
    """A schema-3 pool that counted a burst restart by restart keeps doing so
    (no reconstruction from aggregates); new folds use the rule."""

    events = sparse_events([*clean_history(), *burst_times()])
    legacy_pool = {}
    total = 0.0
    for event in sorted(events, key=lambda item: item.canonical_phase_at):
        index, offset = learned._day_bin(event.canonical_phase_at)
        existing = legacy_pool.get(index)
        legacy_pool[index] = learned.PhaseBin(
            index, 1.0, 1, offset, offset, event.canonical_phase_at, event.canonical_phase_at,
            3.0, 3.0, total,
        ) if existing is None else replace(
            existing, weight=existing.weight + 1.0, count=existing.count + 1,
            offset_min=min(existing.offset_min, offset), offset_max=max(existing.offset_max, offset),
            first_at=min(existing.first_at, event.canonical_phase_at),
            last_at=max(existing.last_at, event.canonical_phase_at),
        )
        total += 1.0
    last = max(events, key=lambda item: item.canonical_phase_at)
    legacy = replace(
        learned.empty_learned_state(s8.SERVER),
        pool=tuple(legacy_pool[key] for key in sorted(legacy_pool)),
        pool_weight_total=total,
        watermark=learned.FoldWatermark(last.canonical_phase_at, last.event_id, len(events), len(events)),
    )
    data = _legacy_mapping(legacy, 3)
    loaded = learned.learned_state_from_mapping(json.loads(json.dumps(data)))
    assert loaded.pool == legacy.pool
    legacy_support = learned.compact_sparse_support(loaded, period=P3, phase_offset=0.0)
    assert legacy_support.off_phase_weight == pytest.approx(20.0)
    assert legacy_support.value == 0.0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda data: data.update(schema_version=learned.LEARNED_STATE_SCHEMA_VERSION + 1),
            id="future_version",
        ),
        pytest.param(lambda data: data.update(schema_version=True), id="bool_version"),
        pytest.param(lambda data: data.pop("followers"), id="v4_missing_followers"),
        pytest.param(lambda data: data.update(schema_version=3), id="v3_with_followers"),
        pytest.param(lambda data: data["followers"][0].__setitem__(1, 0), id="zero_count"),
        pytest.param(lambda data: data["followers"][0].__setitem__(1, True), id="bool_count"),
        pytest.param(lambda data: data["followers"][0].__setitem__(2, 61.0), id="offset_range"),
        pytest.param(lambda data: data["followers"][0].__setitem__(5, 0.0), id="inverted_times"),
        pytest.param(lambda data: data["followers"][0].__setitem__(0, learned.DAY_BINS), id="bin_range"),
        pytest.param(lambda data: data["followers"].append(list(data["followers"][0])), id="duplicate_bin"),
        pytest.param(lambda data: data["followers"][0].append(1.0), id="extra_field"),
        pytest.param(
            lambda data: data.update(
                followers=[[index, *data["followers"][0][1:]] for index in range(learned.DAY_BINS + 1)]
            ),
            id="oversized",
        ),
    ],
)
def test_validation_rejects_malformed_follower_state(mutate):
    data = json.loads(learned.learned_state_json_bytes(_state_with_followers()))
    mutate(data)
    with pytest.raises(learned.LearnedStateError):
        learned.learned_state_from_mapping(data)


def test_follower_storage_is_bounded():
    times = [s8.at(day, 0) + index * 5 * MINUTE for day in range(4) for index in range(288)]
    pipeline = s8.Pipeline()
    events = pipeline.sparse("dense", times)
    state = learned.empty_learned_state(s8.SERVER)
    for event in events:
        state = learned.fold_unit(state, events=[event])
    assert len(state.followers) <= learned.DAY_BINS
    assert learned.learned_state_entry_count(state) <= learned.LEARNED_STATE_MAX_ENTRIES
    size = len(learned.learned_state_json_bytes(state))
    assert size <= learned.LEARNED_STATE_MAX_JSON_BYTES
    more = state
    for event in pipeline.sparse("dense-again", [when + 30 * scoring.DAY for when in times]):
        more = learned.fold_unit(more, events=[event])
    assert learned.learned_state_entry_count(more) == learned.learned_state_entry_count(state)
    assert len(learned.learned_state_json_bytes(more)) < size * 1.1


# --------------------------------------------------------------------------
# Upgrade compatibility: existing users keep their learning
# --------------------------------------------------------------------------


def _as_schema3(state):
    """The same learned state as an unreleased pre-A1 build persisted it."""

    assert state.followers == ()
    data = _legacy_mapping(state, 3)
    return learned.learned_state_from_mapping(json.loads(json.dumps(data)))


def _pre_a1(monkeypatch):
    monkeypatch.setattr(authority, "SPARSE_OCCURRENCE_WINDOW_SECONDS", -1.0)


def _a1(monkeypatch):
    monkeypatch.setattr(authority, "SPARSE_OCCURRENCE_WINDOW_SECONDS", min(scoring.CANDIDATE_PERIODS) / 2)


def test_released_format_record_upgrades_from_its_complete_history(monkeypatch):
    """v0.4.2-beta records carry no learned state and keep every raw event.
    The first rebuild under this code builds schema-4 learned state from the
    complete history with the occurrence rule: nothing is reset, the
    established regime keeps its identity and the decision equals the
    full-history raw authority."""

    pipeline = _production_burst_pipeline()
    monkeypatch.setattr(schema4, "LEARNED_STATE_PERSISTENCE_ENABLED", False)
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", False)
    released = integration.drive(pipeline, every=10)
    assert "learned_state" not in released or released.get("learned_state") is None
    assert len(released["physical_events"]) == len(pipeline.events) > learned.RECENT_RING_CAPACITY
    before = schema4.persisted_authority_decision(released)
    assert before.incumbent_shadow_regime is not None

    monkeypatch.setattr(schema4, "LEARNED_STATE_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", True)
    upgraded = integration.rebuild(released, pipeline.events, pipeline.coverage)
    settled = integration.rebuild(upgraded, pipeline.events, pipeline.coverage)
    after = schema4.persisted_authority_decision(settled)
    state = schema4.persisted_learned_state(settled)

    assert not schema4._validate_server_record(s8.SERVER, settled)
    assert state.schema_version == learned.LEARNED_STATE_SCHEMA_VERSION
    # Every retained restart is either still raw or folded exactly once.
    assert state.watermark.folded_event_count + len(
        schema4.learned_state_ring_events(settled, state)
    ) >= len(pipeline.events)
    assert sum(item.count for item in state.followers) == 19
    assert after.incumbent_shadow_regime.regime_id == before.incumbent_shadow_regime.regime_id
    assert after.incumbent_shadow_regime.established_at == before.incumbent_shadow_regime.established_at
    assert compact_authority.decision_summary(after) == compact_authority.decision_summary(before)
    assert compact_authority.consumer_summary(integration.record_step(settled)) == (
        compact_authority.consumer_summary(integration.record_step(released))
    )
    again = integration.rebuild(json.loads(schema4.canonical_json_bytes(settled)), pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == settled["authority_decision"]
    assert again["learned_state"] == settled["learned_state"]


def _legacy_runner(monkeypatch, pipeline, legacy_batches, *, capacity):
    """Fold ``legacy_batches`` as an unreleased pre-A1 build did, persist the
    learned state as schema 3, then reopen it with the current code."""

    _pre_a1(monkeypatch)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for batch in legacy_batches:
        runner.step(batch)
    runner.state = _as_schema3(runner.state)
    _a1(monkeypatch)
    return runner


def test_legacy_schema3_state_without_bursts_continues_exactly(monkeypatch):
    """Sparse history folded by a pre-A1 build has no followers, so after the
    upgrade the compact path continues exactly like the full-history raw
    authority, and later observations establish the cycle."""

    pipeline = s8.Pipeline()
    early = pipeline.sparse("early", s8.aligned_3h(5))
    later = [pipeline.sparse(f"later-{day}", [s8.at(day, (3 * day) % 24)]) for day in range(5, 10)]
    # Verified acquisition: a witnessed interval after the upgrade.
    witnessed = pipeline.observe(
        "witnessed", [s8.at(11, 3), s8.at(11, 6)], outcome=s8.Outcome.STRONG_QUERY_VISIBLE_RESTART
    )
    batches = [[event] for event in early] + later + [witnessed]
    runner = _legacy_runner(monkeypatch, pipeline, batches[:5], capacity=2)
    assert runner.state.pool and runner.state.followers == ()
    raw = compact_authority.raw_steps(pipeline, batches)
    for index in range(5, len(batches)):
        compact = runner.step(batches[index])
        compact_authority.assert_same(raw[index], compact, index)
    assert s8.established(compact)


def test_legacy_folded_burst_is_kept_and_continuous_evidence_still_establishes(monkeypatch):
    """A burst folded restart by restart into a schema-3 pool cannot be
    re-judged (no per-restart detail).  It keeps its legacy weight on the
    sparse path, exactly as before the upgrade, but it does not block
    learning: continuously observed restarts establish the cycle."""

    def scenario(upgrade):
        pipeline = s8.Pipeline()
        legacy = [
            *pipeline.sparse("early", s8.aligned_3h(3)),
            *pipeline.observe("burst", burst_times(day=3)),
        ]
        runner = _legacy_runner(monkeypatch, pipeline, [legacy], capacity=2)
        if not upgrade:
            _pre_a1(monkeypatch)
        sparse = pipeline.sparse("sparse", s8.aligned_3h(6, start_day=5))
        sparse_step = runner.step(sparse)
        continuous = pipeline.observe("continuous", [s8.at(12, 9) + index * P3 for index in range(4)])
        final = runner.step(continuous)
        _a1(monkeypatch)
        return runner, sparse_step, final

    runner, sparse_step, final = scenario(upgrade=True)
    legacy_off = learned.compact_sparse_support(runner.state, period=P3, phase_offset=0.0)
    # The two-event ring had folded 19 burst restarts restart by restart
    # (legacy, never reinterpreted); the last one was folded after the
    # upgrade, continuing from the legacy pool's latest restart: a follower.
    assert legacy_off.off_phase_weight == pytest.approx(19.0)
    assert sum(item.count for item in runner.state.followers) == 1
    assert not s8.established(sparse_step)  # sparse path still held back, as before
    assert s8.established(final)
    assert final.decision.incumbent_shadow_regime.candidate_period_seconds == P3

    _old, old_sparse, old_final = scenario(upgrade=False)
    assert s8.established(sparse_step) == s8.established(old_sparse)
    assert s8.established(final) == s8.established(old_final)


def test_upgraded_established_regime_keeps_identity_and_adapts(monkeypatch):
    """A sparse-established regime folded by a pre-A1 build survives the
    upgrade unchanged, absorbs a later burst as level-one suspicion, and
    still adopts a genuine clock shift through Time Shift Detection."""

    pipeline = s8.verified_sparse_pipeline()
    runner = _legacy_runner(monkeypatch, pipeline, [[event] for event in pipeline.events], capacity=2)
    before = runner.prior.incumbent_shadow_regime
    assert before is not None and runner.state.schema_version == learned.LEARNED_STATE_SCHEMA_VERSION
    # Time Shift Detection reads unfolded restarts only (pre-existing), so
    # continue with a ring that can hold its three observations.
    runner.capacity = 8

    aligned = runner.step(pipeline.sparse("aligned", [s8.at(7, 9)]))
    assert aligned.decision.incumbent_shadow_regime.regime_id == before.regime_id
    assert s8.established(aligned)

    burst = runner.step(pipeline.observe("burst", burst_times(day=8, hour=10, minute=40)))
    assert burst.decision.suspicion_level == 1
    assert burst.decision.incumbent_shadow_regime.regime_id == before.regime_id
    cleared = runner.step(pipeline.sparse("cleared", [s8.at(9, 12)]))
    assert cleared.decision.suspicion_level == 0 and s8.established(cleared)

    for index, when in enumerate([s8.at(10, 7), s8.at(11, 10), s8.at(12, 13)]):
        shifted = runner.step(pipeline.sparse(f"shift-{index}", [when]))
    regime = shifted.decision.incumbent_shadow_regime
    assert authority.TIME_SHIFT_ADOPTED_REASON in regime.reason_codes
    assert authority._circular_distance(regime.phase_offset, HOUR, P3) <= regime.phase_tolerance
    assert s8.established(shifted)
    # The post-upgrade burst folded under the occurrence rule.
    assert sum(item.count for item in runner.state.followers) == 19


def test_upgrade_preserves_latched_cycle_change_suspicion(monkeypatch):
    """An established 3 h cycle whose countdown a coherent 4 h challenger has
    suspended (level two, latched), folded by a pre-A1 build: after the
    upgrade the suspension and every later decision match the full-history
    raw authority."""

    pipeline = s8.three_hour_chain(4)
    pipeline.observe("four-a", [s8.at(1, 10), s8.at(1, 14)])
    pipeline.observe("four-b", [s8.at(2, 10), s8.at(2, 14)])
    pipeline.sparse("aligned", [s8.at(3 + day, (3 * day) % 24) for day in range(8)])
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    switch = 8  # the challenger intervals are in the legacy (folded) part
    runner = _legacy_runner(monkeypatch, pipeline, batches[:switch], capacity=4)
    assert raw[switch - 1].decision.suspicion_level == 2
    for index in range(switch, len(batches)):
        compact = runner.step(batches[index])
        compact_authority.assert_same(raw[index], compact, index)
    assert raw[-1].decision.suspicion_level == 2 and not raw[-1].decision.countdown_safe
