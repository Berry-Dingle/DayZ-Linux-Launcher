"""Graduated authority (Stage 3): fast correction, protection and restoration.

E1 (newly established): verified, but without strong post-establishment
evidence.  An identified different-period challenger on evidence after the
establishment boundary withdraws it to Awaiting Cycle Verification, or
replaces it once the challenger has a witnessed interval.  Monitored
contradictions without a challenger (two genuine misses plus two independent
off-phase onsets) withdraw it to learning.

E2 (strongly established, a persisted regime reason latch): H3, two witnessed
intervals, or one interval plus post-establishment replication.  Unrelated
anomalies keep its prediction, a coherent identified challenger suspends the
countdown, and only symmetric strong evidence replaces it.

Superseded regimes keep their verification and return on fresh evidence after
the supersession boundary, keeping their regime ID.

Synthetic data only; no runtime, profile, learning file or network.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_restart_compact_cutover as r4
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_historical_acquisition_recovery as har
import test_companion_sc_learning_policy_stage8_baseline as s8
import test_companion_sc_verified_acquisition as va
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumer_state as consumer_state
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import server_companion_ui


HOUR = scoring.HOUR
P3 = s8.P3
P4 = 4 * HOUR
P6 = 6 * HOUR
at = s8.at
State = authority.RegimeState
Status = authority.RegimeRecordStatus
Presentation = consumers4.AuthorityPresentationKey
Outcome = s8.Outcome
QV = Outcome.STRONG_QUERY_VISIBLE_RESTART
OO = Outcome.CORROBORATED_OFFLINE_RESTART

STRONG = authority.REGIME_STRONG_REASON
WITHDRAWN_CHALLENGER = authority.INCUMBENT_WITHDRAWN_CHALLENGER_REASON
WITHDRAWN_CONTRADICTION = authority.INCUMBENT_WITHDRAWN_CONTRADICTION_REASON
SUPERSEDES = authority.VERIFIED_CHALLENGER_SUPERSEDES_REASON
SUSPENDS = authority.CHALLENGER_SUSPENDS_STRONG_REASON
TOLERATES = authority.STRONG_ANOMALY_TOLERANCE_REASON
RESTORED = authority.SUPERSEDED_CYCLE_RESTORED_REASON
AWAITING = authority.AWAITING_VERIFICATION_REASON
VERIFIED = authority.VERIFIED_ESTABLISHMENT_REASON
STAGE3_TRANSITIONS = {
    WITHDRAWN_CHALLENGER,
    WITHDRAWN_CONTRADICTION,
    SUPERSEDES,
    SUSPENDS,
    RESTORED,
}

# Coherent 4 h schedule (01:00 mod 4 h) after the E1 boundary (day 6 15:00).
# None of these restarts lies on the 3 h grid.
FOUR_HOUR = (at(10, 1), at(11, 5), at(12, 13), at(13, 17), at(14, 1))
FOUR_HOUR_INTERVAL = (at(16, 1), at(16, 5))
SECOND_FOUR_HOUR_INTERVAL = (at(18, 9), at(18, 13))
# Return of the 3 h grid after a 4 h swap: none of these is on the 4 h grid.
THREE_HOUR_RETURN = (at(18, 0), at(19, 3), at(20, 6))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def e1():
    """Verified sparse 3 h regime (one witnessed QV interval): E1."""

    pipeline = s8.verified_sparse_pipeline()
    step = s8.settled(pipeline)
    regime = step.decision.incumbent_shadow_regime
    assert step.decision.state is State.ESTABLISHED
    assert STRONG not in regime.reason_codes
    return pipeline, step


def e2():
    """Continuity H3 3 h regime: E2."""

    pipeline = s8.three_hour_chain(4)
    step = s8.settled(pipeline)
    assert STRONG in step.decision.incumbent_shadow_regime.reason_codes
    return pipeline, step


def advance(pipeline, step, *, ledger=()):
    """Evaluate with the persisted prior, then settle once (runtime-like)."""

    first = pipeline.evaluate(s8.persisted(step.decision), ledger=tuple(ledger))
    return pipeline.evaluate(s8.persisted(first.decision), ledger=tuple(ledger))


def add_sparse(pipeline, times, prefix="sparse", **kwargs):
    for index, when in enumerate(times):
        pipeline.observe(f"{prefix}-{index}-{when}", [when], **kwargs)


def walk(pipeline, step, additions, *, ledger=()):
    steps = []
    for apply in additions:
        apply(pipeline)
        step = advance(pipeline, step, ledger=ledger)
        steps.append(step)
    return steps


def miss(period, expected, key):
    tolerance = scoring.candidate_phase_tolerance(period)
    return windows.import_legacy_expected_miss(
        server_key=s8.SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - tolerance,
        expected_at=expected,
        window_end_at=expected + tolerance,
        model_revision_id="stage3-model",
        legacy_key=key,
    )


def neutral(period, expected, outcome):
    original = miss(period, expected, f"neutral-{outcome.value}-{expected}")
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
    )


def regime(step):
    return step.decision.incumbent_shadow_regime


def strong(step):
    return STRONG in regime(step).reason_codes


def stage3(step):
    return STAGE3_TRANSITIONS.intersection(step.decision.reason_codes)


def assert_established(step, period=P3, *, regime_id=None):
    value = step.consumer()
    assert step.decision.state in {State.ESTABLISHED, State.CHANGE_SUSPECTED}, step.decision.state
    assert regime(step).candidate_period_seconds == period
    assert regime(step).status is Status.ESTABLISHED
    assert step.decision.selected_shadow_regime.regime_id == regime(step).regime_id
    if regime_id is not None:
        assert regime(step).regime_id == regime_id
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe


def assert_awaiting(step, period, incumbent_id):
    value = step.consumer()
    shown = s8.panel(step)
    decision = step.decision
    assert decision.state is State.LIKELY
    assert WITHDRAWN_CHALLENGER in decision.reason_codes and AWAITING in decision.reason_codes
    assert decision.selected_shadow_regime.status is Status.LIKELY
    assert decision.selected_shadow_regime.candidate_period_seconds == period
    # The old regime is retained, still ESTABLISHED, for restoration.
    assert regime(step).regime_id == incumbent_id
    assert regime(step).status is Status.ESTABLISHED
    assert value.presentation_state is Presentation.AWAITING_CYCLE_VERIFICATION
    assert value.awaiting_period_seconds == period
    assert shown["cycle_text"] == "Awaiting Cycle Verification"
    assert shown["confidence_label"] == "Learning Confidence:"
    assert server_companion_ui.restart_presentation_tooltip(shown) == (
        f"Likely every {period // HOUR} hours - Keep monitoring through one complete "
        "restart interval to confirm this schedule."
    )
    assert not value.countdown_visible and not value.countdown_safe
    assert value.next_expected_restart_at is None
    assert not step.consumer(now=step.now + 60).scheduled_warning_eligible


def assert_no_warning_ever(step):
    value = step.consumer()
    assert not value.countdown_visible
    assert not value.scheduled_warning_eligible
    assert value.scheduled_warning_suppression_key is None


def assert_fixed_point(pipeline, step, *, ledger=()):
    """No-new-evidence rebuilds (each reopened through the real schema-4
    JSON serializer) reproduce the decision."""

    persisted = s8.persisted(step.decision)
    again = step
    for _ in range(2):
        again = pipeline.evaluate(s8.persisted(again.decision), ledger=tuple(ledger))
        assert s8.persisted(again.decision) == persisted
    return again


def withdrawn_to_four_hour():
    pipeline, original = e1()
    add_sparse(pipeline, FOUR_HOUR, "four")
    step = advance(pipeline, original)
    assert_awaiting(step, P4, regime(original).regime_id)
    return pipeline, original, step


def swapped_to_four_hour():
    pipeline, original, withdrawn = withdrawn_to_four_hour()
    pipeline.observe("four-interval", FOUR_HOUR_INTERVAL)
    first = pipeline.evaluate(s8.persisted(withdrawn.decision))
    swapped = pipeline.evaluate(s8.persisted(first.decision))
    return pipeline, original, withdrawn, first, swapped


CONTRADICTIONS = (
    lambda p: add_sparse(p, [at(7, 13, 37)], "off-1"),
    lambda p: None,
    lambda p: add_sparse(p, [at(8, 7, 11)], "off-2"),
    lambda p: None,
)
TWO_MISSES = (miss(P3, at(7, 15), "miss-1"), miss(P3, at(8, 18), "miss-2"))


def contradicted_e1():
    pipeline, original = e1()
    ledger = list(TWO_MISSES)
    step = original
    for apply in CONTRADICTIONS:
        apply(pipeline)
        step = advance(pipeline, step, ledger=ledger)
    return pipeline, original, step, ledger


# --------------------------------------------------------------------------
# 1-2. E1 promotion to E2
# --------------------------------------------------------------------------


def test_01_e1_promoted_to_e2_by_a_second_independent_witnessed_interval():
    pipeline, original = e1()
    pipeline.observe("second", (at(8, 9), at(8, 12)))
    step = advance(pipeline, original)
    assert_established(step, regime_id=regime(original).regime_id)
    assert strong(step)
    # Internal only: the display is the existing established display.
    shown = s8.panel(step)
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert_fixed_point(pipeline, step)


@pytest.mark.parametrize("count, promoted", [(3, False), (4, True)])
def test_02_e1_promoted_by_later_independent_sporadic_replication(count, promoted):
    # Sporadic monitoring after the verifying interval earns E2 through
    # replication: N_P (4) independent aligned onsets after establishment.
    pipeline, original = e1()
    add_sparse(pipeline, [at(8 + index, (9 + 3 * index) % 24) for index in range(count)], "rep")
    step = advance(pipeline, original)
    assert_established(step, regime_id=regime(original).regime_id)
    assert strong(step) is promoted


def test_02_a1_followers_never_count_toward_replication():
    pipeline, original = e1()
    add_sparse(pipeline, [at(8, 9), at(9, 12)], "rep")
    add_sparse(pipeline, [at(8, 9, 20), at(9, 12, 20)], "follower")
    step = advance(pipeline, original)
    assert not strong(step)


def test_02_the_latch_is_set_at_establishment_and_survives_rebuilds():
    # Two intervals at acquisition: latched on the first decision already,
    # so the first decision equals every evidence-identical rebuild.
    pipeline = s8.three_hour_chain(3)
    first = pipeline.evaluate()
    assert STRONG in regime(first).reason_codes
    settled = s8.settled(pipeline)
    assert regime(settled) == regime(first)
    assert_fixed_point(pipeline, settled)


# --------------------------------------------------------------------------
# 3-6. E1 fast correction
# --------------------------------------------------------------------------


def test_03_e1_with_one_isolated_admin_restart_is_not_withdrawn():
    pipeline, original = e1()
    add_sparse(pipeline, [at(9, 17, 20)], "admin")
    step = advance(pipeline, original)
    assert_established(step, regime_id=regime(original).regime_id)
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
    assert not stage3(step)


def test_04_e1_withdraws_to_awaiting_for_an_identified_unverified_challenger():
    pipeline, original, step = withdrawn_to_four_hour()
    assert regime(step).candidate_period_seconds == P3
    assert_no_warning_ever(step)
    # Retained, not superseded, and the Awaiting state is a fixed point.
    assert all(item.status is not Status.SUPERSEDED for item in step.decision.regimes)
    assert_fixed_point(pipeline, step)


def test_04_three_coherent_onsets_do_not_identify_a_challenger():
    pipeline, original = e1()
    add_sparse(pipeline, FOUR_HOUR[:3], "four")
    step = advance(pipeline, original)
    assert WITHDRAWN_CHALLENGER not in step.decision.reason_codes
    assert regime(step).regime_id == step.decision.selected_shadow_regime.regime_id


def test_05_e1_swaps_to_a_verified_challenger_and_keeps_provenance():
    pipeline, original, withdrawn, first, swapped = swapped_to_four_hour()
    assert first.decision.state is State.NEW_REGIME_ESTABLISHED
    assert SUPERSEDES in first.decision.reason_codes
    assert_established(swapped, P4)
    new = regime(swapped)
    # The challenger keeps the identity it had while awaiting verification.
    assert new.regime_id == withdrawn.decision.selected_shadow_regime.regime_id
    assert VERIFIED in new.reason_codes and authority.SPARSE_ESTABLISHMENT_REASON in new.reason_codes
    old = next(item for item in swapped.decision.regimes if item.regime_id == regime(original).regime_id)
    assert old.status is Status.SUPERSEDED
    assert old.superseded_by_id == new.regime_id
    assert old.superseded_at == new.established_at
    assert VERIFIED in old.reason_codes
    # As at acquisition, the new regime is established at its latest aligned
    # identification restart (the interval endpoints verify it).
    assert new.established_at == FOUR_HOUR[-1]
    assert {item.status for item in swapped.decision.regimes} == {Status.ESTABLISHED, Status.SUPERSEDED}
    assert_fixed_point(pipeline, swapped)


def test_05_direct_swap_without_an_intermediate_awaiting_state():
    pipeline, original = e1()
    add_sparse(pipeline, FOUR_HOUR, "four")
    pipeline.observe("four-interval", FOUR_HOUR_INTERVAL)
    step = advance(pipeline, original)
    assert_established(step, P4)
    assert any(
        item.regime_id == regime(original).regime_id and item.status is Status.SUPERSEDED
        for item in step.decision.regimes
    )


@pytest.mark.parametrize(
    "ledger, offs, withdrawn",
    [
        pytest.param(TWO_MISSES[:1], 2, False, id="one_miss_two_offs"),
        pytest.param(TWO_MISSES, 1, False, id="two_misses_one_off"),
        pytest.param(TWO_MISSES, 2, True, id="two_misses_two_offs"),
    ],
)
def test_06_e1_withdraws_to_learning_only_after_misses_and_off_phase_onsets(ledger, offs, withdrawn):
    pipeline, original = e1()
    add_sparse(pipeline, [at(7, 13, 37), at(8, 7, 11)][:offs], "off")
    step = advance(pipeline, original, ledger=ledger)
    assert (WITHDRAWN_CONTRADICTION in step.decision.reason_codes) is withdrawn
    if withdrawn:
        value = step.consumer()
        assert step.decision.state is State.NORMAL_LEARNING
        assert step.decision.selected_shadow_regime is None
        assert regime(step).regime_id == regime(original).regime_id
        assert regime(step).status is Status.ESTABLISHED
        assert s8.panel(step)["cycle_text"] == "Learning Restart Pattern"
        assert_no_warning_ever(step)
        assert_fixed_point(pipeline, step, ledger=ledger)
    else:
        assert regime(step).regime_id == step.decision.selected_shadow_regime.regime_id


@pytest.mark.parametrize(
    "outcome",
    [windows.ExpectedWindowOutcome.AMBIGUOUS, windows.ExpectedWindowOutcome.UNKNOWN],
)
def test_06_unmonitored_or_outage_windows_never_count_as_misses(outcome):
    pipeline, original = e1()
    add_sparse(pipeline, [at(7, 13, 37), at(8, 7, 11)], "off")
    ledger = (neutral(P3, at(7, 15), outcome), neutral(P3, at(8, 18), outcome))
    step = advance(pipeline, original, ledger=ledger)
    assert WITHDRAWN_CONTRADICTION not in step.decision.reason_codes
    assert regime(step).regime_id == step.decision.selected_shadow_regime.regime_id


def test_06_a_single_new_phase_of_the_same_period_is_left_to_time_shift_detection():
    pipeline, original = e1()
    add_sparse(pipeline, [at(7, 13, 30), at(8, 7, 30)], "shifted")
    step = advance(pipeline, original, ledger=TWO_MISSES)
    assert WITHDRAWN_CONTRADICTION not in step.decision.reason_codes
    assert regime(step).regime_id == regime(original).regime_id


# --------------------------------------------------------------------------
# 7-11. E2 protection and symmetric replacement
# --------------------------------------------------------------------------


def test_07_e2_survives_two_unrelated_admin_restarts_with_its_prediction():
    pipeline, original = e2()
    steps = walk(
        pipeline,
        original,
        [lambda p: add_sparse(p, [at(3, 13, 37)], "a1"), lambda p: add_sparse(p, [at(6, 7, 11)], "a2")],
    )
    for step in steps:
        assert_established(step, regime_id=regime(original).regime_id)
        assert TOLERATES in step.decision.reason_codes
        assert step.decision.suspicion_level == 1
        assert s8.percent(step) == s8.percent(original)
        value = step.consumer()
        assert step.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_eligible


def test_08_e2_keeps_establishment_while_a_weak_challenger_accumulates():
    pipeline, original = e2()
    add_sparse(pipeline, [at(2, 1), at(3, 5)], "weak")
    step = advance(pipeline, original)
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).status is Status.ESTABLISHED
    assert step.decision.selected_shadow_regime.regime_id == regime(original).regime_id
    assert not stage3(step)
    assert step.decision.state not in {State.LIKELY, State.NORMAL_LEARNING}


def test_09_e2_suspends_its_countdown_for_a_coherent_identified_challenger():
    pipeline, original = e2()
    add_sparse(pipeline, FOUR_HOUR, "four")
    step = advance(pipeline, original)
    value = step.consumer()
    assert step.decision.state is State.CHALLENGER_ACCUMULATING
    assert SUSPENDS in step.decision.reason_codes and step.decision.suspicion_level == 2
    assert regime(step).regime_id == step.decision.selected_shadow_regime.regime_id == regime(original).regime_id
    assert value.presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert value.cycle_period_seconds == P3
    assert_no_warning_ever(step)
    assert_fixed_point(pipeline, step)


def test_09_e2_is_never_withdrawn_to_learning_by_misses():
    pipeline, original = e2()
    ledger = (miss(P3, at(3, 0), "e2-miss-1"), miss(P3, at(4, 3), "e2-miss-2"))
    add_sparse(pipeline, [at(3, 13, 37), at(4, 7, 11)], "off")
    step = advance(pipeline, original, ledger=ledger)
    assert WITHDRAWN_CONTRADICTION not in step.decision.reason_codes
    assert step.decision.state is not State.NORMAL_LEARNING
    assert regime(step).regime_id == regime(original).regime_id


@pytest.mark.parametrize("intervals, switched", [(1, False), (2, True)])
def test_10_e2_switches_only_with_two_witnessed_challenger_intervals(intervals, switched):
    pipeline, original = e2()
    add_sparse(pipeline, FOUR_HOUR, "four")
    for index, times in enumerate((FOUR_HOUR_INTERVAL, SECOND_FOUR_HOUR_INTERVAL)[:intervals]):
        pipeline.observe(f"four-interval-{index}", times)
    step = advance(pipeline, original)
    if switched:
        assert_established(step, P4)
        old = next(item for item in step.decision.regimes if item.regime_id == regime(original).regime_id)
        assert old.status is Status.SUPERSEDED
        assert strong(step)
    else:
        assert SUSPENDS in step.decision.reason_codes
        assert regime(step).regime_id == regime(original).regime_id
        assert_no_warning_ever(step)


def test_11_e2_adapts_to_sporadic_evidence_plus_one_interval_and_replication():
    # Not only uninterrupted chains: ten sporadic 4 h onsets (identification
    # plus replication) and one witnessed interval replace a strong cycle.
    pipeline, original = e2()
    sporadic = FOUR_HOUR + (at(15, 5), at(16, 13), at(17, 17), at(18, 21), at(19, 1))
    add_sparse(pipeline, sporadic, "four")
    suspended = advance(pipeline, original)
    assert SUSPENDS in suspended.decision.reason_codes
    pipeline.observe("four-interval", (at(21, 1), at(21, 5)))
    step = advance(pipeline, suspended)
    assert_established(step, P4)


# --------------------------------------------------------------------------
# 12-14. False challengers, restoration and no oscillation
# --------------------------------------------------------------------------


def test_12_false_challenger_then_the_original_schedule_returns():
    pipeline, original, withdrawn = withdrawn_to_four_hour()
    add_sparse(pipeline, [at(15, 0)], "back-1")
    still = advance(pipeline, withdrawn)
    add_sparse(pipeline, [at(16, 3)], "back-2")
    step = advance(pipeline, still)
    assert_established(step, regime_id=regime(original).regime_id)
    assert not any(item.status is Status.SUPERSEDED for item in step.decision.regimes)
    # Same regime: the warning key is continuous with the original.
    assert_fixed_point(pipeline, step)


def test_12_withdrawn_cycle_needs_two_fresh_confirmations_to_return():
    pipeline, original, withdrawn, ledger = contradicted_e1()
    assert WITHDRAWN_CONTRADICTION in withdrawn.decision.reason_codes
    add_sparse(pipeline, [at(9, 12)], "back-1")
    one = advance(pipeline, withdrawn, ledger=ledger)
    assert WITHDRAWN_CONTRADICTION in one.decision.reason_codes
    assert one.decision.selected_shadow_regime is None
    add_sparse(pipeline, [at(10, 15)], "back-2")
    two = advance(pipeline, one, ledger=ledger)
    assert_established(two, regime_id=regime(original).regime_id)


def test_13_previous_e1_is_restored_without_relearning_and_keeps_its_id():
    pipeline, original, _withdrawn, _first, swapped = swapped_to_four_hour()
    steps = walk(pipeline, swapped, [lambda p, t=t: add_sparse(p, [t], f"return-{t}") for t in THREE_HOUR_RETURN[:2]])
    restored = steps[-1]
    assert_established(restored, P3, regime_id=regime(original).regime_id)
    item = regime(restored)
    assert RESTORED in item.reason_codes and VERIFIED in item.reason_codes
    assert item.established_at == THREE_HOUR_RETURN[1]
    four = next(value for value in restored.decision.regimes if value.candidate_period_seconds == P4)
    assert four.status is Status.SUPERSEDED and four.superseded_by_id == item.regime_id
    # One fresh onset is not enough.
    assert regime(steps[0]).candidate_period_seconds == P4


def test_13_restoration_needs_evidence_after_the_supersession_boundary():
    # The 3 h history before the swap is plentiful but never fresh evidence.
    pipeline, _original, _withdrawn, _first, swapped = swapped_to_four_hour()
    step = assert_fixed_point(pipeline, swapped)
    assert regime(step).candidate_period_seconds == P4


def test_13_restarts_on_both_grids_never_restore():
    # 09:00 and 21:00 are on both the 3 h and the 4 h grid.
    pipeline, _original, _withdrawn, _first, swapped = swapped_to_four_hour()
    steps = walk(pipeline, swapped, [lambda p: add_sparse(p, [at(18, 9)], "both-1"), lambda p: add_sparse(p, [at(19, 21)], "both-2")])
    assert regime(steps[-1]).candidate_period_seconds == P4


def test_13_restoration_against_e2_needs_symmetric_strength():
    pipeline, original = e2()
    add_sparse(pipeline, FOUR_HOUR, "four")
    pipeline.observe("a", FOUR_HOUR_INTERVAL)
    pipeline.observe("b", SECOND_FOUR_HOUR_INTERVAL)
    swapped = advance(pipeline, original)
    assert strong(swapped) and regime(swapped).candidate_period_seconds == P4
    add_sparse(pipeline, [at(20, 0), at(21, 3), at(22, 6)], "return")
    step = advance(pipeline, swapped)
    assert regime(step).candidate_period_seconds == P4
    assert RESTORED not in regime(step).reason_codes


def test_14_no_oscillation_or_evidence_reuse_across_rebuilds_and_reopen():
    pipeline, original, withdrawn, first, swapped = swapped_to_four_hour()
    assert_fixed_point(pipeline, swapped)
    steps = walk(pipeline, swapped, [lambda p, t=t: add_sparse(p, [t], f"return-{t}") for t in THREE_HOUR_RETURN[:2]])
    restored = assert_fixed_point(pipeline, steps[-1])
    # The superseded 4 h regime does not come back on its old evidence.
    for _ in range(3):
        restored = pipeline.evaluate(s8.persisted(restored.decision))
        assert regime(restored).candidate_period_seconds == P3
        assert regime(restored).regime_id == regime(original).regime_id


# --------------------------------------------------------------------------
# 15-17. Period ambiguity and administrative noise
# --------------------------------------------------------------------------


def test_15_genuine_six_hour_cycle_with_an_isolated_three_hour_admin_restart():
    pipeline = s8.Pipeline()
    pipeline.observe("six", [at(0, 0) + index * P6 for index in range(5)])
    original = s8.settled(pipeline)
    assert strong(original) and regime(original).candidate_period_seconds == P6
    add_sparse(pipeline, [at(4, 9)], "admin")
    step = advance(pipeline, original)
    assert_established(step, P6, regime_id=regime(original).regime_id)
    assert not stage3(step)


@pytest.mark.parametrize("build", [e1, e2], ids=["e1", "e2"])
def test_16_admin_restarts_coincidentally_supporting_a_shorter_period(build):
    pipeline, original = build()
    add_sparse(pipeline, [at(8, 1), at(9, 5), at(10, 11)], "admin")
    step = advance(pipeline, original)
    assert not stage3(step)
    assert regime(step).regime_id == step.decision.selected_shadow_regime.regime_id == regime(original).regime_id
    assert regime(step).status is Status.ESTABLISHED


def test_17_true_two_hour_cycle_seen_like_four_hours_is_never_established_unverified():
    pipeline = s8.Pipeline()
    s8.verify(pipeline, (at(-2, 1), at(-2, 5)))
    add_sparse(pipeline, [at(0, 1), at(1, 5), at(2, 13), at(3, 17), at(4, 21)], "four")
    original = s8.settled(pipeline)
    assert regime(original).candidate_period_seconds == P4
    add_sparse(pipeline, [at(6, 3), at(7, 7), at(8, 11), at(9, 15), at(10, 19), at(11, 23), at(12, 3)], "two")
    step = advance(pipeline, original)
    pipeline.observe("two-interval", (at(13, 3), at(13, 5)))
    later = advance(pipeline, step)
    for value in (step, later):
        # (The odd-hour restarts look like a 4 h phase move to the existing
        # Time Shift Detection; that is not a Stage 3 transition.)
        assert not stage3(value)
        assert regime(value).candidate_period_seconds == P4
        assert all(
            item.candidate_period_seconds != 2 * HOUR or item.status is not Status.ESTABLISHED
            for item in value.decision.regimes
        )


# --------------------------------------------------------------------------
# 18-19. Time Shift Detection
# --------------------------------------------------------------------------


def test_18_same_period_phase_shift_follows_time_shift_detection():
    pipeline, original = e1()
    step = original
    for index, when in enumerate((at(9, 1), at(10, 7), at(11, 16))):
        add_sparse(pipeline, [when], f"shift-{index}")
        step = advance(pipeline, step)
        assert not stage3(step)
    item = regime(step)
    assert authority.TIME_SHIFT_ADOPTED_REASON in item.reason_codes
    assert item.candidate_period_seconds == P3 and VERIFIED in item.reason_codes
    assert step.decision.state is State.ESTABLISHED and step.consumer().countdown_visible


def _disable_stage3(patch):
    patch.setattr(authority, "_sparse_challenger", lambda **_kwargs: None)
    patch.setattr(authority, "_contradiction_withdrawal_held", lambda **_kwargs: False)
    patch.setattr(authority, "E1_WITHDRAWAL_GENUINE_MISSES", 10**6)


def _authority_signature(step):
    item = regime(step)
    return (
        step.decision.state,
        None if item is None else (item.regime_id, item.candidate_period_seconds, item.phase_offset, item.established_at, item.status),
        step.decision.combined_confidence,
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: har.red_dawn_like(), id="full_fixture"),
        pytest.param(lambda: har.red_dawn_like(new=har.NEW[:4], off=()), id="coincident_handoff"),
    ],
)
def test_19_red_dawn_like_coincidental_handoff_is_not_amplified(monkeypatch, build):
    # Stage 2's coincidental 20:05 phase hand-off (a Stage 4 issue) must not
    # become a Stage 3 swap, withdrawal or new authority: restart by restart
    # with the persisted prior, Stage 3 changes nothing but the latch.
    def incremental():
        pipeline = build()
        steps = []
        prior = None
        full = s8.Pipeline(coverage=list(pipeline.coverage))
        for event in sorted(pipeline.events, key=lambda item: item.canonical_phase_at):
            full.events.append(event)
            step = full.evaluate(prior)
            prior = s8.persisted(step.decision)
            steps.append(step)
        return steps

    current = incremental()
    with monkeypatch.context() as patch:
        _disable_stage3(patch)
        baseline = incremental()
    assert [_authority_signature(step) for step in current] == [_authority_signature(step) for step in baseline]
    for step in current:
        assert not stage3(step)
    final = regime(current[-1])
    assert final.candidate_period_seconds == P4
    assert har.phase_distance(final.phase_offset, 0.0) <= scoring.candidate_phase_tolerance(P4)


# --------------------------------------------------------------------------
# 20-22. Evidence validity: connectivity, A1 and QV
# --------------------------------------------------------------------------


def test_20_connectivity_gap_never_verifies_a_challenger():
    pipeline, _original, withdrawn = withdrawn_to_four_hour()
    pipeline.observe("before-gap", [FOUR_HOUR_INTERVAL[0]], tail=60 * 60)
    pipeline.observe("after-gap", [FOUR_HOUR_INTERVAL[1]], lead=60 * 60)
    step = advance(pipeline, withdrawn)
    assert step.decision.state is State.LIKELY and WITHDRAWN_CHALLENGER in step.decision.reason_codes


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"coverage_complete": False}, id="incomplete"),
        pytest.param(
            {"coverage_complete": False, "lifecycle_interruption": detection.LifecycleMarker.SLEEP_GAP},
            id="lifecycle_interrupted",
        ),
        pytest.param({"authenticity": 0.2}, id="inauthentic"),
    ],
)
def test_20_invalid_restarts_never_identify_a_challenger(changes):
    pipeline, original = e1()
    add_sparse(pipeline, FOUR_HOUR[:3], "four")
    (event,) = pipeline.observe("invalid", [FOUR_HOUR[3]])
    pipeline.events[pipeline.events.index(event)] = replace(event, **changes)
    step = advance(pipeline, original)
    assert WITHDRAWN_CHALLENGER not in step.decision.reason_codes
    assert regime(step).regime_id == regime(original).regime_id


@pytest.mark.parametrize("minutes", [10, 30])
def test_21_a1_followers_are_not_independent_challenger_evidence(minutes):
    pipeline, original = e1()
    add_sparse(pipeline, FOUR_HOUR[:3], "four")
    add_sparse(pipeline, [when + minutes * 60 for when in FOUR_HOUR[:3]], "follower")
    step = advance(pipeline, original)
    assert WITHDRAWN_CHALLENGER not in step.decision.reason_codes
    assert regime(step).regime_id == regime(original).regime_id


@pytest.mark.parametrize("outcome", [QV, OO], ids=["qv", "oo"])
def test_22_witnessed_qv_or_oo_challenger_interval_verifies(outcome):
    pipeline, original, withdrawn = withdrawn_to_four_hour()
    pipeline.observe("four-interval", FOUR_HOUR_INTERVAL, outcome=outcome)
    step = advance(pipeline, withdrawn)
    assert_established(step, P4)


def test_22_drain_only_endpoints_never_verify_a_challenger():
    pipeline, _original, withdrawn = withdrawn_to_four_hour()
    s8.observe_mixed(
        pipeline,
        "drains",
        [(FOUR_HOUR_INTERVAL[0], Outcome.AMBIGUOUS_DRAIN), (FOUR_HOUR_INTERVAL[1], Outcome.AMBIGUOUS_DRAIN)],
    )
    step = advance(pipeline, withdrawn)
    assert step.decision.state is State.LIKELY
    assert_no_warning_ever(step)


# --------------------------------------------------------------------------
# 23-24. Raw/compact parity, schema-5 reopen and persistence
# --------------------------------------------------------------------------


def _lifecycle_pipeline():
    """E1, withdrawal, swap to 4 h, then restoration of 3 h."""

    pipeline = s8.verified_sparse_pipeline()
    add_sparse(pipeline, FOUR_HOUR, "four")
    pipeline.observe("four-interval", FOUR_HOUR_INTERVAL)
    add_sparse(pipeline, THREE_HOUR_RETURN[:2], "return")
    return pipeline


def _lifecycle_batches(pipeline):
    """The E1 history in one batch (so it is newly established, as in the
    runtime after its first rebuild), then one restart per rebuild."""

    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    base = len(s8.VERIFYING_INTERVAL) + len(s8.SPARSE_DISCRIMINATING)
    return [ordered[:base]] + [[item] for item in ordered[base:]]


@pytest.mark.parametrize("capacity", [3, 6, 20])
def test_23_raw_and_compact_agree_through_withdrawal_swap_and_restoration(capacity):
    pipeline = _lifecycle_pipeline()
    batches = _lifecycle_batches(pipeline)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    seen = set()
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        label = (capacity, index)
        assert compact_authority.decision_summary(compact.decision) == (
            compact_authority.decision_summary(raw[index].decision)
        ), label
        assert stage3(compact) == stage3(raw[index]), label
        seen.update(stage3(raw[index]))
    assert {WITHDRAWN_CHALLENGER, SUPERSEDES} <= seen
    assert regime(raw[-1]).candidate_period_seconds == P3
    assert RESTORED in regime(raw[-1]).reason_codes


@pytest.mark.parametrize("capacity", [3, 6, 20])
def test_23_restart_by_restart_parity_and_no_spurious_common_period(capacity):
    # Restart by restart the E1 regime earns E2 by replication before the
    # 4 h restarts arrive.  The post-boundary 3 h restarts at odd hours plus
    # the 4 h ones are all on one 2 h grid: the incumbent explains the former,
    # so 2 h is never a challenger (regression), and compact equals raw.
    pipeline = _lifecycle_pipeline()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        label = (capacity, index)
        # Stage 4: the established display may read lower (never higher) on
        # the compact path once replication onsets fold into minute bins
        # that straddle the establishment boundary; decisions are equal.
        compact_authority.assert_same_display_bounded(raw[index], compact, label, consumer=False)
        assert stage3(compact) == stage3(raw[index]), label
        for step in (raw[index], compact):
            assert all(item.candidate_period_seconds != 2 * HOUR for item in step.decision.regimes), label
    assert any(SUSPENDS in step.decision.reason_codes for step in raw)


def test_24_production_rebuild_reopen_and_validation_through_the_lifecycle(monkeypatch, tiny_ring):
    tiny_ring(4)
    pipeline = _lifecycle_pipeline()
    records = compact_authority.run(pipeline, every=1, compact_from=0, monkeypatch=monkeypatch)
    final = records[-1]
    state = schema4.persisted_learned_state(final)
    assert state.schema_version == 5
    decision = schema4.persisted_authority_decision(final)
    assert decision.incumbent_shadow_regime.candidate_period_seconds == P3
    for record in records:
        assert not schema4._validate_server_record(s8.SERVER, record)
    reopened = json.loads(schema4.canonical_json_bytes(final))
    again = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == final["authority_decision"]
    assert again["learned_state"] == final["learned_state"]
    assert not schema4._validate_server_record(s8.SERVER, again)


tiny_ring = r4.tiny_ring


# --------------------------------------------------------------------------
# 25. Existing histories: grandfathered, never downgraded
# --------------------------------------------------------------------------


def test_25_legacy_e1_rebuild_without_new_evidence_is_unchanged(monkeypatch):
    pipeline = va.awaiting_pipeline()
    legacy = va._legacy_sparse_established(monkeypatch, pipeline)
    before = regime(legacy)
    step = legacy
    for _ in range(3):
        step = pipeline.evaluate(s8.persisted(step.decision))
    item = regime(step)
    assert (item.regime_id, item.candidate_period_seconds, item.phase_offset, item.established_at) == (
        before.regime_id, before.candidate_period_seconds, before.phase_offset, before.established_at,
    )
    assert step.decision.combined_confidence == legacy.decision.combined_confidence
    assert STRONG not in item.reason_codes and not stage3(step)
    assert step.consumer().countdown_visible


def test_25_legacy_regime_gains_e2_from_actual_evidence(monkeypatch):
    pipeline = va.awaiting_pipeline()
    legacy = va._legacy_sparse_established(monkeypatch, pipeline)
    va.with_interval(pipeline, (at(9, 9), at(9, 12)))
    va.with_interval(pipeline, (at(10, 12), at(10, 15)), name="second")
    step = advance(pipeline, legacy)
    assert regime(step).regime_id == regime(legacy).regime_id
    assert strong(step)


def test_25_legacy_red_dawn_like_recovery_is_unchanged(monkeypatch):
    pipeline = har.red_dawn_like(verified=False)
    legacy = va._legacy_sparse_established(monkeypatch, pipeline)
    step = pipeline.evaluate(s8.persisted(legacy.decision))
    step = pipeline.evaluate(s8.persisted(step.decision))
    assert _authority_signature(step)[1][:4] == _authority_signature(legacy)[1][:4]
    assert not stage3(step) and step.consumer().countdown_visible


def test_25_production_upgrade_of_an_e2_record_adds_only_the_latch(monkeypatch):
    pipeline = s8.three_hour_chain(6)
    with monkeypatch.context() as patch:
        patch.setattr(authority, "_incumbent_is_strong", lambda **_kwargs: False)
        record = integration.drive(pipeline, every=3)
    before = schema4.persisted_authority_decision(record)
    upgraded = integration.rebuild(record, pipeline.events, pipeline.coverage)
    after = schema4.persisted_authority_decision(upgraded)
    old, new = before.incumbent_shadow_regime, after.incumbent_shadow_regime
    assert STRONG in new.reason_codes and STRONG not in old.reason_codes
    assert replace(new, reason_codes=old.reason_codes) == old
    assert after.combined_confidence == before.combined_confidence
    assert not schema4._validate_server_record(s8.SERVER, upgraded)


# --------------------------------------------------------------------------
# 26. Consumer-state and alert-key continuity
# --------------------------------------------------------------------------


def test_26_regime_identity_is_continuous_across_withdrawal_and_return():
    # Warning keys are (regime ID, occurrence): the returned cycle is the same
    # regime, so a warning fired before the withdrawal is never repeated.
    # (Its displayed confidence after the false challenger is the existing
    # formula's; confidence progression is Stage 4.)
    pipeline, original, withdrawn = withdrawn_to_four_hour()
    assert withdrawn.consumer().scheduled_warning_suppression_key is None
    add_sparse(pipeline, [at(15, 0)], "back-1")
    still = advance(pipeline, withdrawn)
    add_sparse(pipeline, [at(16, 3)], "back-2")
    returned = advance(pipeline, still)
    assert regime(returned).regime_id == regime(original).regime_id
    assert returned.consumer().countdown_visible


def test_26_warning_key_is_stable_while_e2_tolerates_anomalies():
    pipeline, original = e2()
    due = original.consumer().next_expected_restart_at - 300
    first = original.consumer(now=due)
    assert first.scheduled_warning_eligible
    fired = frozenset({first.scheduled_warning_suppression_key})
    add_sparse(pipeline, [at(0, 19, 37)], "admin")
    step = advance(pipeline, original)
    assert TOLERATES in step.decision.reason_codes
    value = step.consumer(now=due, fired_keys=fired)
    assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
    assert not value.scheduled_pre_restart_alert_eligible


def test_26_awaiting_and_suspended_states_persist_in_the_consumer_ledger():
    _pipeline, _original, withdrawn = withdrawn_to_four_hour()
    pipeline, original = e2()
    add_sparse(pipeline, FOUR_HOUR, "four")
    suspended = advance(pipeline, original)
    ledger = consumer_state.ledger_from_mapping(None, server_key=s8.SERVER)
    for step in (withdrawn, suspended):
        ledger = consumer_state.with_decision(ledger, step.consumer())
    data = json.loads(json.dumps(consumer_state.ledger_to_mapping(ledger)))
    restored = consumer_state.ledger_from_mapping(data, server_key=s8.SERVER)
    states = [item.presentation_state for item in restored.decisions]
    assert "awaiting_cycle_verification" in states and "schedule_change_suspected" in states
    assert all(item.next_expected_restart_at is None for item in restored.decisions)
    assert consumer_state.fired_suppression_keys(restored) == frozenset()


def test_26_a_fired_warning_is_not_repeated_after_a_swap_settles_or_reopens():
    pipeline, _original, _withdrawn, _first, swapped = swapped_to_four_hour()
    occurrence = swapped.consumer().next_expected_restart_at
    due = occurrence - 300
    first = swapped.consumer(now=due)
    assert first.scheduled_warning_eligible
    assert first.scheduled_warning_suppression_key.regime_id == regime(swapped).regime_id
    fired = frozenset({first.scheduled_warning_suppression_key})
    again = pipeline.evaluate(s8.persisted(swapped.decision))
    value = again.consumer(now=due, fired_keys=fired)
    assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
    assert not value.scheduled_pre_restart_alert_eligible
