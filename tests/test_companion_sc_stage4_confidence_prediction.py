"""Server Companion verified acquisition, Stage 4.

Confidence integrity, phase accuracy and prediction safety:

- established Cycle Confidence from retained independent evidence (resolved
  administrative anomalies, retained verification, post-establishment
  replication toward 95 %), display only;
- a withdrawn or suspended incumbent's prediction waits for reconfirmation;
- the normal-only phase proposal moves to its most central restart (the Red
  Dawn-like 20:05 hand-off);
- restarts interleaving several new phases are a suspected cycle change,
  not a possible time change.
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
import test_companion_sc_stage3_graduated_authority as st3
import test_companion_sc_verified_acquisition as va
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import server_companion_ui


HOUR = scoring.HOUR
P3 = s8.P3
P4 = 4 * HOUR
at = s8.at
State = authority.RegimeState
Status = authority.RegimeRecordStatus
Presentation = consumers4.AuthorityPresentationKey
OO = s8.Outcome.CORROBORATED_OFFLINE_RESTART
QV = s8.Outcome.STRONG_QUERY_VISIBLE_RESTART

DISPLAY = authority.ESTABLISHED_DISPLAY_REASON
PENDING = authority.RECONFIRMATION_PENDING_REASON
SEVERAL = authority.TIME_SHIFT_SEVERAL_PHASES_REASON
RESTORED = authority.SUPERSEDED_CYCLE_RESTORED_REASON
SUSPENDS = authority.CHALLENGER_SUSPENDS_STRONG_REASON
WITHDRAWN_CONTRADICTION = authority.INCUMBENT_WITHDRAWN_CONTRADICTION_REASON
TIME_SHIFT_SUSPECTED = authority.TIME_SHIFT_SUSPECTED_REASON
ADOPTED = authority.TIME_SHIFT_ADOPTED_REASON

regime = st3.regime
advance = st3.advance
add_sparse = st3.add_sparse
tiny_ring = r4.tiny_ring


def percent(step):
    return s8.panel(step)["confidence_percent"]


def shown(step):
    return step.decision.selected_shadow_regime


def assert_consistent(step):
    """Countdown, warning and the displayed value derive from one decision."""

    value = step.consumer()
    if not value.countdown_visible:
        assert not value.countdown_safe or value.next_expected_restart_at is None
        assert not value.scheduled_warning_eligible
        assert value.scheduled_warning_suppression_key is None
        return value
    if value.presentation_state is Presentation.CONFIRMED_CYCLE:
        # The displayed percentage is the authority's combined confidence,
        # which also gates the warning (H2 and sparse regimes).
        assert value.confidence_display_value == step.decision.combined_confidence
    due = step.consumer(now=value.next_expected_restart_at - 300)
    if due.scheduled_warning_eligible:
        assert due.scheduled_warning_suppression_key.regime_id == shown(step).regime_id
    return value


def assert_countdown(step, *, warning=True):
    value = assert_consistent(step)
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible is warning
    return value


def assert_withheld(step):
    value = assert_consistent(step)
    assert not value.countdown_visible
    assert value.next_expected_restart_at is None
    assert not step.consumer(now=step.now + 60).scheduled_warning_eligible
    return value


def oo_verified_e1():
    """Sparse 3 h regime verified by one offline-observed interval (90 %)."""

    pipeline = s8.Pipeline()
    s8.verify(pipeline, outcome=OO)
    pipeline.sparse("sparse", s8.SPARSE_DISCRIMINATING)
    step = s8.settled(pipeline)
    assert percent(step) == 90 and not st3.strong(step)
    return pipeline, step


def replication(pipeline, step, count, *, prefix="rep"):
    steps = []
    for index in range(count):
        add_sparse(pipeline, [at(8 + index, (3 * index) % 24)], f"{prefix}-{index}")
        step = advance(pipeline, step)
        steps.append(step)
    return steps


# --------------------------------------------------------------------------
# Confidence after withdrawal, restoration and false challengers
# --------------------------------------------------------------------------


def test_false_challenger_return_waits_for_reconfirmation_then_recovers_confidence():
    pipeline, original, withdrawn = st3.withdrawn_to_four_hour()
    original_percent = percent(original)
    assert original_percent == 80

    add_sparse(pipeline, [at(15, 0)], "back-1")
    held = advance(pipeline, withdrawn)
    # One aligned restart no longer identifies the challenger, but it does
    # not reconfirm the cycle after it: the establishment and its earned
    # confidence are kept, the prediction waits (Stage 3 restored it here).
    assert held.decision.state is State.CHANGE_SUSPECTED
    assert PENDING in held.decision.reason_codes
    assert regime(held).regime_id == regime(original).regime_id
    assert regime(held).status is Status.ESTABLISHED
    assert assert_withheld(held).presentation_state is Presentation.CONFIRMED_CYCLE
    # The unresolved coherent 4 h group still weighs (coherent contradictions
    # are never ignored), so the confidence is below the warning milestone.
    assert percent(held) == 61
    st3.assert_fixed_point(pipeline, held)

    add_sparse(pipeline, [at(16, 3)], "back-2")
    returned = advance(pipeline, held)
    assert returned.decision.state is State.ESTABLISHED
    assert PENDING not in returned.decision.reason_codes
    assert regime(returned).regime_id == regime(original).regime_id
    assert_countdown(returned)
    # The false challenger is resolved by the reconfirmation and no longer
    # dilutes the cycle; the two fresh onsets replicate its prediction.
    assert percent(returned) == 85 > original_percent
    assert DISPLAY in returned.decision.reason_codes or any(
        DISPLAY in item.reason_codes for item in returned.decision.candidates
    )
    st3.assert_fixed_point(pipeline, returned)


def test_contradiction_withdrawal_returns_with_its_earned_confidence():
    pipeline, original, withdrawn, ledger = st3.contradicted_e1()
    assert WITHDRAWN_CONTRADICTION in withdrawn.decision.reason_codes
    add_sparse(pipeline, [at(9, 12)], "back-1")
    one = advance(pipeline, withdrawn, ledger=ledger)
    assert one.decision.selected_shadow_regime is None
    assert_withheld(one)
    add_sparse(pipeline, [at(10, 15)], "back-2")
    two = advance(pipeline, one, ledger=ledger)
    st3.assert_established(two, regime_id=regime(original).regime_id)
    # Stage 3 showed 76 % (the contradictions still diluted it) and so
    # withheld the warning; the resolved contradictions no longer weigh.
    assert percent(two) == 85
    assert_countdown(two)


def test_restored_e1_shows_its_verified_history_from_the_restoration_decision():
    pipeline, original, _withdrawn, _first, swapped = st3.swapped_to_four_hour()
    add_sparse(pipeline, [st3.THREE_HOUR_RETURN[0]], "return-0")
    step = advance(pipeline, swapped)
    add_sparse(pipeline, [st3.THREE_HOUR_RETURN[1]], "return-1")
    first = pipeline.evaluate(s8.persisted(step.decision))
    assert first.decision.state is State.NEW_REGIME_ESTABLISHED
    assert RESTORED in first.decision.reason_codes
    restored = regime(first)
    assert restored.regime_id == regime(original).regime_id
    # Stage 3: 31 % (the restoring onsets alone); now its retained
    # identification evidence counts and the superseding cycle's restarts,
    # reconfirmed against, no longer dilute it.
    assert first.decision.combined_confidence == restored.combined_confidence == 0.798
    settled = pipeline.evaluate(s8.persisted(first.decision))
    assert settled.decision.state is State.ESTABLISHED
    assert settled.decision.combined_confidence == first.decision.combined_confidence
    assert_countdown(settled)
    # Fresh replication raises it further (Stage 3: 44 %).
    add_sparse(pipeline, [st3.THREE_HOUR_RETURN[2]], "return-2")
    later = advance(pipeline, settled)
    assert regime(later).regime_id == regime(original).regime_id
    assert percent(later) == 82
    st3.assert_fixed_point(pipeline, later)


def test_restored_oo_verified_cycle_keeps_its_interval_credit():
    pipeline, original = oo_verified_e1()
    add_sparse(pipeline, st3.FOUR_HOUR, "four")
    step = advance(pipeline, original)
    pipeline.observe("four-interval", st3.FOUR_HOUR_INTERVAL)
    step = advance(pipeline, step)
    assert regime(step).candidate_period_seconds == P4
    add_sparse(pipeline, [st3.THREE_HOUR_RETURN[0]], "return-0")
    step = advance(pipeline, step)
    add_sparse(pipeline, [st3.THREE_HOUR_RETURN[1]], "return-1")
    first = pipeline.evaluate(s8.persisted(step.decision))
    assert RESTORED in first.decision.reason_codes
    assert regime(first).regime_id == regime(original).regime_id
    # The offline-observed interval is verification at any time: 90 % from
    # the restoration decision, as before the mistaken replacement.
    assert first.decision.combined_confidence == original.decision.combined_confidence
    settled = pipeline.evaluate(s8.persisted(first.decision))
    assert percent(settled) == 90
    assert_countdown(settled)


def test_restoration_keeps_alert_key_continuity_and_never_restores_without_fresh_evidence():
    pipeline, original, _withdrawn, _first, swapped = st3.swapped_to_four_hour()
    swapped_value = assert_countdown(swapped)
    assert swapped_value.next_expected_restart_at is not None
    # No fresh evidence: the superseded cycle stays superseded however much
    # history it retains, and the replacement's values are unchanged.
    again = st3.assert_fixed_point(pipeline, swapped)
    assert regime(again).candidate_period_seconds == P4
    assert percent(again) == percent(swapped) == 90
    steps = st3.walk(
        pipeline,
        swapped,
        [lambda p, t=t: add_sparse(p, [t], f"return-{t}") for t in st3.THREE_HOUR_RETURN[:2]],
    )
    restored = steps[-1]
    value = assert_countdown(restored)
    due = restored.consumer(now=value.next_expected_restart_at - 300)
    original_key = s8.consume(original, now=original.now).scheduled_warning_suppression_key
    assert due.scheduled_warning_suppression_key.regime_id == original_key.regime_id


def test_no_confidence_oscillation_across_the_lifecycle():
    pipeline, original, withdrawn = st3.withdrawn_to_four_hour()
    step = withdrawn
    seen = []
    for name, when in (("back-1", at(15, 0)), ("back-2", at(16, 3)), ("more", at(17, 6))):
        add_sparse(pipeline, [when], name)
        step = advance(pipeline, step)
        # Repeated identical rebuilds, each reopened through the real schema-4
        # JSON serializer, reproduce the decision and its confidence.
        again = st3.assert_fixed_point(pipeline, step)
        assert again.decision.combined_confidence == step.decision.combined_confidence
        seen.append(step.decision.combined_confidence)
    assert seen == sorted(seen)
    assert regime(step).regime_id == regime(original).regime_id


# --------------------------------------------------------------------------
# Mature schedules
# --------------------------------------------------------------------------


@pytest.mark.parametrize("build", [st3.e1, st3.e2, oo_verified_e1], ids=["e1_qv", "e2", "e1_oo"])
def test_one_and_two_unrelated_admin_restarts_never_lower_established_confidence(build):
    pipeline, original = build()
    before = original.decision.combined_confidence
    step = original
    for index, when in enumerate((at(7, 13, 37), at(8, 7, 11))):
        add_sparse(pipeline, [when], f"admin-{index}")
        step = advance(pipeline, step)
        assert regime(step).regime_id == regime(original).regime_id
        assert step.decision.combined_confidence >= before
        add_sparse(pipeline, [at(9 + index, 3 + 3 * index)], f"aligned-{index}")
        step = advance(pipeline, step)
        assert step.decision.combined_confidence >= before
        assert_countdown(step)


def test_coherent_alternative_period_suspends_e2_prediction_but_keeps_its_evidence():
    pipeline, original = st3.e2()
    add_sparse(pipeline, st3.FOUR_HOUR, "four")
    step = advance(pipeline, original)
    assert SUSPENDS in step.decision.reason_codes
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).status is Status.ESTABLISHED
    # The established evidence confidence is retained by authority while the
    # countdown and warning are withheld.
    assert step.decision.combined_confidence == original.decision.combined_confidence
    value = assert_withheld(step)
    assert value.presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED


def test_strong_cycle_returning_after_suspension_waits_for_reconfirmation():
    pipeline, original = st3.e2()
    add_sparse(pipeline, st3.FOUR_HOUR, "four")
    step = advance(pipeline, original)
    add_sparse(pipeline, [at(15, 3)], "back-1")
    held = advance(pipeline, step)
    assert PENDING in held.decision.reason_codes and st3.strong(held)
    assert regime(held).regime_id == regime(original).regime_id
    assert_withheld(held)
    add_sparse(pipeline, [at(16, 6)], "back-2")
    returned = advance(pipeline, held)
    assert PENDING not in returned.decision.reason_codes
    assert_countdown(returned)


def test_genuine_misses_still_withhold_and_never_raise_confidence():
    pipeline, original = st3.e1()
    step = advance(pipeline, original, ledger=st3.TWO_MISSES)
    assert step.decision.suspicion_level >= 1
    assert step.decision.combined_confidence <= original.decision.combined_confidence + 1e-9
    assert_withheld(step)


@pytest.mark.parametrize(
    "outcome",
    [st3.windows.ExpectedWindowOutcome.UNKNOWN, st3.windows.ExpectedWindowOutcome.AMBIGUOUS],
    ids=["unmonitored", "outage"],
)
def test_unknown_windows_are_never_misses_and_change_nothing(outcome):
    pipeline, original = st3.e1()
    ledger = (st3.neutral(P3, at(7, 15), outcome), st3.neutral(P3, at(8, 18), outcome))
    step = advance(pipeline, original, ledger=ledger)
    assert step.decision.suspicion_level == 0
    assert step.decision.combined_confidence == original.decision.combined_confidence
    assert_countdown(step)


def test_verified_replacement_shows_its_own_post_boundary_values():
    _pipeline, _original, _withdrawn, first, swapped = st3.swapped_to_four_hour()
    assert regime(first).candidate_period_seconds == P4
    assert first.decision.combined_confidence == swapped.decision.combined_confidence == 0.899
    assert_countdown(swapped)


def test_sparse_replication_progresses_toward_95_percent():
    pipeline, step = oo_verified_e1()
    oo_steps = replication(pipeline, step, 6)
    assert [percent(item) for item in oo_steps] == [91, 93, 94, 95, 95, 95]
    # The authority value itself stops at the two-interval level (the panel
    # also caps the display at 95 %).
    assert [item.decision.combined_confidence for item in oo_steps] == [
        0.912289, 0.925579, 0.938868, 0.9495, 0.9495, 0.9495,
    ]
    pipeline, step = st3.e1()
    steps = replication(pipeline, step, 10)
    qv = [percent(item) for item in steps]
    assert qv == [82, 85, 88, 90, 91, 93, 94, 95, 95, 95]
    # Never above the two-interval level from sporadic evidence alone.
    assert max(item.decision.combined_confidence for item in steps) == 0.9495
    for item in steps:
        assert_countdown(item)
    # Stage 3's protection latch and the display are independent: E2 after
    # four replication onsets, displayed 90 % (not mapped to 95 %).
    assert st3.strong(steps[3]) and percent(steps[3]) == 90


def test_replication_needs_a_witnessed_interval(monkeypatch):
    pipeline = va.awaiting_pipeline()
    legacy = va._legacy_sparse_established(monkeypatch, pipeline)
    before = legacy.decision.combined_confidence
    step = legacy
    for index in range(4):
        add_sparse(pipeline, [at(8 + index, 3 * index)], f"rep-{index}")
        step = pipeline.evaluate(s8.persisted(step.decision))
    assert step.decision.combined_confidence == before
    assert not any(DISPLAY in item.reason_codes for item in step.decision.candidates)


@pytest.mark.parametrize("minutes", [10, 30])
def test_a1_followers_never_count_as_replication(minutes):
    clean_pipeline, clean = st3.e1()
    add_sparse(clean_pipeline, [at(8, 0), at(9, 3)], "rep")
    clean = advance(clean_pipeline, clean)
    burst_pipeline, burst = st3.e1()
    add_sparse(burst_pipeline, [at(8, 0), at(9, 3)], "rep")
    add_sparse(burst_pipeline, [at(8, 0) + minutes * 60, at(9, 3) + minutes * 60], "follower")
    burst = advance(burst_pipeline, burst)
    assert burst.decision.combined_confidence == clean.decision.combined_confidence == 0.851158


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"coverage_complete": False}, id="incomplete"),
        pytest.param({"authenticity": 0.2}, id="inauthentic"),
    ],
)
def test_invalid_restarts_never_replicate(changes):
    pipeline, original = st3.e1()
    (event,) = pipeline.observe("invalid", [at(9, 9)])
    pipeline.events[pipeline.events.index(event)] = replace(event, **changes)
    step = advance(pipeline, original)
    assert step.decision.combined_confidence == original.decision.combined_confidence


def test_unobserved_time_changes_neither_confidence_nor_safety():
    pipeline, original = st3.e1()
    later = original.consumer(now=original.now + 30 * 86400)
    assert later.confidence_display_value == original.consumer().confidence_display_value
    add_sparse(pipeline, [at(40, 9)], "after-a-month")
    step = advance(pipeline, original)
    assert step.decision.combined_confidence >= original.decision.combined_confidence
    assert_countdown(step)


# --------------------------------------------------------------------------
# Phase estimation
# --------------------------------------------------------------------------


def _incremental(pipeline):
    prior = None
    full = s8.Pipeline(coverage=list(pipeline.coverage))
    steps = []
    for event in sorted(pipeline.events, key=lambda item: item.canonical_phase_at):
        full.events.append(event)
        step = full.evaluate(prior)
        prior = s8.persisted(step.decision)
        steps.append(step)
    return steps


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: har.red_dawn_like(), id="full_fixture"),
        pytest.param(lambda: har.red_dawn_like(new=har.NEW[:4], off=()), id="coincident_handoff"),
    ],
)
def test_red_dawn_like_coincidental_restart_never_becomes_the_phase(build):
    steps = _incremental(build())
    adopted = [step for step in steps if regime(step) is not None and ADOPTED in regime(step).reason_codes]
    assert adopted
    for step in adopted:
        # Stage 2/3: the hand-off took the 20:05 coincidental restart's phase
        # (300 s); the grid phase is kept.
        assert regime(step).phase_offset == 0.0
        assert regime(step).candidate_period_seconds == P4
    assert regime(steps[-1]).phase_offset == 0.0


def _events(times, uncertainty=3.0):
    base = s8.sparse_pipeline(times[:1]).events[0]
    return tuple(
        replace(base, event_id=f"phase-{index}", canonical_phase_at=when, phase_uncertainty=uncertainty)
        for index, when in enumerate(times)
    )


def test_refined_phase_moves_off_a_coincidental_anchor_only():
    period = P4
    grid = [at(day, 0) for day in range(4)]
    coincident = at(5, 20, 5)
    # The anchor is the coincidental restart (+5 min): the medoid is the grid.
    assert authority._refined_sparse_phase(coincident % period, period, _events([coincident, *grid])) == 0.0
    # A representative anchor never moves (Stage 2 jitter calibration).
    jitter = [at(0, 0), at(1, 0) - 300, at(2, 0) + 300, at(3, 0) - 300]
    assert authority._refined_sparse_phase(0.0, period, _events(jitter)) == 0.0
    # A restart just inside the tolerance does not pull a supported phase.
    tolerance = scoring.candidate_phase_tolerance(period)
    edge = [*grid, at(6, 4) + tolerance - 5]
    assert authority._refined_sparse_phase(0.0, period, _events(edge)) == 0.0
    # A genuine one-hour move is outside the tolerance: never merged.
    moved = [at(day, 1) for day in range(8, 12)]
    assert authority._refined_sparse_phase(0.0, period, _events([*grid, *moved])) == 0.0
    assert authority._refined_sparse_phase(HOUR, period, _events([*grid, *moved])) == HOUR


def test_refined_phase_wraps_around_midnight():
    period = P3
    times = [at(1, 0) - 60, at(2, 0) - 50, at(3, 0) + 40, at(4, 0) - 55]
    refined = authority._refined_sparse_phase(times[2] % period, period, _events(times))
    assert refined == times[0] % period or refined == times[1] % period or refined == times[3] % period
    assert min(refined, period - refined) <= 60


def test_compact_refined_phase_matches_raw_for_ring_onsets():
    events = _events([at(13, 20, 5), at(15, 0), at(15, 16), at(16, 0), at(16, 20)])
    state = learned.empty_learned_state(events[0].server_key)
    raw = authority._refined_sparse_phase(300.0, P4, events)
    compact = learned.compact_refined_phase(state, period=P4, phase=300.0, ring_events=events)
    assert raw == compact == 0.0


def test_genuine_one_hour_time_shift_is_still_adopted_at_the_new_phase():
    pipeline, original = st3.e2()
    step = original
    for index, when in enumerate((at(8, 10), at(9, 13), at(10, 16))):
        add_sparse(pipeline, [when], f"shift-{index}")
        step = advance(pipeline, step)
    assert ADOPTED in regime(step).reason_codes
    assert regime(step).phase_offset == HOUR
    assert_countdown(step)


# --------------------------------------------------------------------------
# Early cycle-change presentation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_interleaved_new_phases_present_a_suspected_cycle_change(build):
    pipeline, original = build()
    step = original
    for index, when in enumerate((at(10, 1), at(11, 2), at(12, 1), at(13, 2))):
        add_sparse(pipeline, [when], f"off-{index}")
        step = advance(pipeline, step)
        if index == 2:
            # One repeated phase is still a possible time change.
            assert step.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert SEVERAL in step.decision.reason_codes and TIME_SHIFT_SUSPECTED in step.decision.reason_codes
    value = assert_withheld(step)
    assert value.presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert s8.panel(step)["cycle_text"] == "Suspected Cycle Change"
    # The establishment is untouched.
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).status is Status.ESTABLISHED


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_successive_phases_stay_a_possible_time_change(build):
    pipeline, original = build()
    step = original
    for index, when in enumerate((at(10, 1), at(11, 1), at(12, 2), at(13, 2))):
        add_sparse(pipeline, [when], f"off-{index}")
        step = advance(pipeline, step)
    assert SEVERAL not in step.decision.reason_codes
    value = assert_withheld(step)
    assert value.presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert s8.panel(step)["cycle_text"] == "Possible Time Change"


def test_awaiting_and_learning_presentations_are_unchanged():
    _pipeline, original, withdrawn = st3.withdrawn_to_four_hour()
    st3.assert_awaiting(withdrawn, P4, regime(original).regime_id)
    learning = s8.settled(s8.sparse_pipeline(s8.SPARSE_DISCRIMINATING[:3]))
    assert s8.panel(learning)["cycle_text"] == "Learning Restart Pattern"
    assert s8.panel(learning)["confidence_label"] == "Learning Confidence:"
    awaiting = s8.settled(va.awaiting_pipeline())
    shown_panel = s8.panel(awaiting)
    assert shown_panel["cycle_text"] == "Awaiting Cycle Verification"
    assert server_companion_ui.restart_presentation_tooltip(shown_panel) == (
        "Likely every 3 hours - Keep monitoring through one complete restart interval to "
        "confirm this schedule."
    )


# --------------------------------------------------------------------------
# Raw/compact parity and persistence
# --------------------------------------------------------------------------


def _lifecycle():
    pipeline = s8.verified_sparse_pipeline()
    add_sparse(pipeline, st3.FOUR_HOUR, "four")
    add_sparse(pipeline, [at(15, 0), at(16, 3), at(17, 6)], "back")
    return pipeline


@pytest.mark.parametrize("capacity", [3, 5, 20, 80])
@pytest.mark.parametrize("factory", [_lifecycle, st3._lifecycle_pipeline], ids=["false_challenger", "restoration"])
def test_compact_display_is_never_higher_than_raw(factory, capacity):
    pipeline = factory()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        # Before establishment the harness's schema-3 consumer counts ring
        # events only (a known harness limitation at tiny rings), so the
        # consumer is compared once a cycle is selected.
        compact_authority.assert_same_display_bounded(
            raw[index], compact, (capacity, index), consumer=shown(raw[index]) is not None
        )
        assert (PENDING in compact.decision.reason_codes) is (PENDING in raw[index].decision.reason_codes)
        if capacity >= len(pipeline.events):
            # Nothing folded: exact.
            compact_authority.assert_same(raw[index], compact, (capacity, index))


@pytest.mark.parametrize("factory", [_lifecycle, st3._lifecycle_pipeline], ids=["false_challenger", "restoration"])
def test_two_event_ring_reaches_the_same_stage4_outcomes(factory):
    # At a two-event ring Time Shift Detection sees only one unfolded
    # observation (pre-existing: it reads the ring only), so intermediate
    # time-shift annotations can differ; the Stage 4 hold, the return and the
    # final display agree (compact never higher).
    pipeline = factory()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=2)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert (PENDING in compact.decision.reason_codes) is (
            PENDING in raw[index].decision.reason_codes
        ), index
    left, right = regime(compact), regime(raw[-1])
    assert (left.candidate_period_seconds, round(left.phase_offset), left.status) == (
        right.candidate_period_seconds, round(right.phase_offset), right.status,
    )
    assert compact.decision.state is raw[-1].decision.state
    assert compact.decision.combined_confidence <= raw[-1].decision.combined_confidence


def test_least_restart_weight_is_a_strict_lower_bound():
    assert learned._least_restart_weight(1.0, 1) == 1.0
    assert learned._least_restart_weight(2.0, 2) == 1.0
    assert learned._least_restart_weight(1.6, 2) == 0.8
    assert learned._least_restart_weight(1.8, 2) == 0.8
    assert learned._least_restart_weight(1.5, 2) == 0.5
    assert learned._least_restart_weight(2.6, 3) == 0.8
    assert learned._least_restart_weight(0.7, 2) == 0.0


def test_production_lifecycle_validates_and_reopens_identically(monkeypatch, tiny_ring):
    tiny_ring(3)
    pipeline = _lifecycle()
    records = compact_authority.run(pipeline, every=1, compact_from=0, monkeypatch=monkeypatch)
    final = records[-1]
    assert schema4.persisted_learned_state(final).schema_version == 5
    for record in records:
        assert not schema4._validate_server_record(s8.SERVER, record)
    reopened = json.loads(schema4.canonical_json_bytes(final))
    again = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == final["authority_decision"]
    assert again["learned_state"] == final["learned_state"]
    decision = schema4.persisted_authority_decision(final)
    assert decision.incumbent_shadow_regime.candidate_period_seconds == P3


def test_legacy_regimes_rebuild_without_any_change(monkeypatch):
    pipeline = va.awaiting_pipeline()
    legacy = va._legacy_sparse_established(monkeypatch, pipeline)
    step = legacy
    for _ in range(3):
        step = pipeline.evaluate(s8.persisted(step.decision))
    before, after = regime(legacy), regime(step)
    assert (after.regime_id, after.candidate_period_seconds, after.phase_offset, after.established_at) == (
        before.regime_id, before.candidate_period_seconds, before.phase_offset, before.established_at,
    )
    assert set(after.reason_codes) == set(before.reason_codes)
    assert step.decision.combined_confidence == legacy.decision.combined_confidence
    unverified = har.red_dawn_like(verified=False)
    first = s8.settled(unverified)
    again = unverified.evaluate(s8.persisted(first.decision))
    assert again.decision == first.decision
