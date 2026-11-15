"""Time Shift Detection: an established schedule moves to a new time while
its restart interval and earned confidence are kept.

Synthetic evidence only.  Two drivers are used:

* the Stage 8 pipeline (raw-history authority) with the previous decision
  round-tripped through the real schema-4 serializer, and
* the production rebuild (``rebuild_schema4_server_record``, compact
  authority, pruned raw evidence) with the record reopened from JSON.

No runtime, profile or learning file is touched.

Rules under test.  Observations are independent, authentic, learning-eligible
restarts later than the regime's latest aligned evidence that agree on one
new phase of the established period, within the regime tolerance:

1. one observation is noticed (nothing else changes);
2. two withhold the prediction ("Possible Time Change");
3. the third adopts the phase; period, authority and confidence are retained.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import test_companion_restart_compact_cutover as r4
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import server_companion_ui


HOUR = s8.HOUR
DAY = s8.DAY
P3 = s8.P3
P4 = 4 * HOUR
at = s8.at
Outcome = s8.Outcome
State = authority.RegimeState
Presentation = consumers4.AuthorityPresentationKey

NOTICED = authority.TIME_SHIFT_NOTICED_REASON
SUSPECTED = authority.TIME_SHIFT_SUSPECTED_REASON
ADOPTED = authority.TIME_SHIFT_ADOPTED_REASON
DISPLACED = authority.TIME_SHIFT_DISPLACED_REASON

# A 3 h cycle observed at 09:00, 12:00, 15:00, 18:00 UTC: restart grid
# 00, 03, ... 21.  Moved one hour later the grid is 01, 04, ... 22.
# Stage 5: the first two observations are 12 h apart, so they cannot rule out
# a change to a 4 h (or 2 h) interval and the third observation confirms, as
# before; two restarts 3 h apart corroborate at once (Stage 5 tests).
SHIFTED_GRID = (at(3, 22), at(4, 10), at(5, 16), at(6, 10))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def established(count=4, *, period=P3, start_hour=9):
    """An established continuity regime and its persisted decision.

    Four consecutive restarts pass H3 (95 %); three pass H2 through the
    two-interval rule (80 %), the gate whose countdown is withheld at level
    one unless the contradiction is one isolated restart.
    """

    pipeline = s8.Pipeline()
    pipeline.observe("original", [at(0, start_hour) + index * period for index in range(count)])
    step = s8.settled(pipeline)
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.incumbent_shadow_regime.origin is authority.AuthorityOrigin.CONTINUITY
    return pipeline, step


def advance(pipeline, step, name, when, **kwargs):
    """One more restart in its own short session, then a runtime-like rebuild."""

    pipeline.sparse(name, [when], **kwargs)
    return pipeline.evaluate(s8.persisted(step.decision))


def observe_shift(pipeline, step, times, *, name="shift", **kwargs):
    steps = []
    for index, when in enumerate(times):
        step = advance(pipeline, step, f"{name}-{index}", when, **kwargs)
        steps.append(step)
    return steps


def inject(pipeline, step, events):
    """Add prepared events (already carrying their own session) and rebuild."""

    pipeline.events.extend(events)
    return pipeline.evaluate(s8.persisted(step.decision))


def shaped(name, when, sequence, **changes):
    """A restart-like episode in its own session, with fields overridden."""

    session = s8.Session(name, when - 20 * 60, when + 20 * 60)
    event = s8.physical_event(
        sequence, when, session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART
    )
    return session, replace(event, **changes)


def regime(step):
    return step.decision.incumbent_shadow_regime


def phase(step):
    return regime(step).phase_offset


def time_shift_reasons(step):
    return {item for item in step.decision.reason_codes if item in {NOTICED, SUSPECTED, ADOPTED}}


def panel(step, *, now=None):
    now = step.now if now is None else now
    return s8.ui(step, step.consumer(now=now), now)


def assert_unshifted(step, original):
    """The established regime is exactly the one before the observations."""

    assert regime(step).regime_id == regime(original).regime_id
    assert phase(step) == phase(original)
    assert regime(step).candidate_period_seconds == regime(original).candidate_period_seconds
    assert ADOPTED not in regime(step).reason_codes


def assert_authority_retained(step, original):
    before, after = regime(original), regime(step)
    assert after.candidate_period_seconds == before.candidate_period_seconds
    assert after.origin is before.origin
    assert after.status is authority.RegimeRecordStatus.ESTABLISHED
    assert after.authority_gate is before.authority_gate
    assert after.high_authority == before.high_authority
    # Later aligned restarts may add to the confidence; nothing is ever lost.
    assert step.decision.combined_confidence >= original.decision.combined_confidence
    assert s8.percent(step) >= s8.percent(original)


def assert_adopted(step, original, *, shift, tolerance=1.0):
    period = regime(original).candidate_period_seconds
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.suspicion_level == 0
    assert ADOPTED in regime(step).reason_codes
    assert phase(step) == pytest.approx((phase(original) + shift) % period, abs=tolerance)
    assert_authority_retained(step, original)
    value = step.consumer()
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert (value.next_expected_restart_at - phase(step)) % period == pytest.approx(0.0, abs=1e-3)


# --------------------------------------------------------------------------
# 1-3. Notice, suspect, confirm
# --------------------------------------------------------------------------


def test_one_matching_off_phase_observation_is_noticed_but_cannot_confirm():
    pipeline, original = established()
    (step,) = observe_shift(pipeline, original, SHIFTED_GRID[:1])

    assert time_shift_reasons(step) == {NOTICED}
    # Stage 5: recorded internally as a possible one-hour displacement only.
    assert DISPLACED in step.decision.reason_codes
    assert step.decision.state is State.CHANGE_SUSPECTED
    assert step.decision.suspicion_level == 1
    assert_unshifted(step, original)
    assert_authority_retained(step, original)
    # Existing safety behaviour is untouched: an H3 regime keeps its countdown
    # at level one, on the original grid.
    value = step.consumer()
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible
    assert (value.next_expected_restart_at - phase(original)) % P3 == 0
    assert panel(step)["cycle_text"] == "Every 3 Hours"


def test_one_observation_keeps_the_existing_h2_isolated_restart_behaviour():
    pipeline, original = established(3)
    assert regime(original).authority_gate is authority.AuthorityGate.H2
    (step,) = observe_shift(pipeline, original, SHIFTED_GRID[:1])

    assert time_shift_reasons(step) == {NOTICED}
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
    assert step.consumer().countdown_visible
    assert_unshifted(step, original)


@pytest.mark.parametrize("count", [3, 4])
def test_two_independent_observations_enter_possible_time_change(count):
    pipeline, original = established(count)
    _first, step = observe_shift(pipeline, original, SHIFTED_GRID[:2])

    assert time_shift_reasons(step) == {SUSPECTED}
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in step.decision.reason_codes
    assert not step.decision.prediction_usable
    assert not step.decision.countdown_safe
    # The proposed phase is not adopted and nothing earned is lost.
    assert_unshifted(step, original)
    assert_authority_retained(step, original)

    value = step.consumer()
    assert value.presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert value.visible_cycle
    assert value.cycle_period_seconds == P3
    assert not value.countdown_visible
    assert not value.countdown_safe
    assert value.next_expected_restart_at is None
    assert not value.scheduled_warning_eligible
    assert value.scheduled_warning_suppression_key is None

    shown = panel(step)
    assert shown["cycle_text"] == "Possible Time Change"
    assert shown["presentation_key"] == "possible_time_change"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_visible"]
    assert shown["confidence_percent"] == s8.percent(original)
    assert not shown["countdown_visible"]
    assert not shown["next_visible"]


@pytest.mark.parametrize("count", [3, 4])
def test_three_independent_observations_adopt_the_new_phase(count):
    pipeline, original = established(count)
    _first, _second, step = observe_shift(pipeline, original, SHIFTED_GRID[:3])

    assert_adopted(step, original, shift=HOUR)
    assert time_shift_reasons(step) == {ADOPTED}
    assert regime(step).established_at == SHIFTED_GRID[2]
    # One established regime: the previous record is replaced, not superseded.
    assert [item.status for item in step.decision.regimes] == [
        authority.RegimeRecordStatus.ESTABLISHED
    ]
    assert panel(step)["cycle_text"] == "Every 3 Hours"
    assert panel(step)["countdown_visible"]


def test_suspected_state_withholds_scheduled_warnings_at_every_old_occurrence():
    pipeline, original = established()
    _first, step = observe_shift(pipeline, original, SHIFTED_GRID[:2])

    for occurrence in (at(4, 3), at(4, 4), at(4, 6)):
        value = step.consumer(now=occurrence - 300)
        assert not value.scheduled_warning_eligible
        assert not value.scheduled_pre_restart_alert_eligible


def test_confirmed_state_restores_countdown_and_scheduled_warnings_on_the_new_grid():
    pipeline, original = established()
    step = observe_shift(pipeline, original, SHIFTED_GRID[:3])[-1]

    expected = at(5, 19)  # the first shifted occurrence after 16:00
    value = step.consumer()
    assert value.next_expected_restart_at == pytest.approx(expected, abs=1.0)
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible
    assert due.scheduled_pre_restart_alert_eligible
    assert due.scheduled_warning_suppression_key.regime_id == regime(step).regime_id
    assert due.scheduled_warning_suppression_key.expected_occurrence_at == round(
        value.next_expected_restart_at
    )
    # No warning is due at the abandoned grid.
    assert not step.consumer(now=at(5, 18) - 300).scheduled_pre_restart_alert_eligible


@pytest.mark.parametrize("observations", [1, 2, 3])
def test_back_online_alerts_stay_independent(observations):
    pipeline, original = established()
    step = observe_shift(pipeline, original, SHIFTED_GRID[:observations])[-1]

    value = step.consumer(physical_recovery_event_id="recovery-event")
    assert value.generic_recovery_alert_eligible
    assert "generic_recovery_independent_of_schedule" in value.reason_codes


# --------------------------------------------------------------------------
# 4-6. Grid, direction, size and modular arithmetic
# --------------------------------------------------------------------------


def test_nonconsecutive_observations_on_the_same_repeating_grid_qualify():
    pipeline, original = established()
    # 01:00, 07:00 and 16:00: six and nine hours apart, on different days,
    # each seen alone.  Nothing between them was monitored.
    times = (at(3, 1), at(4, 7), at(6, 16))
    assert {(when - times[0]) % P3 for when in times} == {0}
    step = observe_shift(pipeline, original, times)[-1]

    assert_adopted(step, original, shift=HOUR)


@pytest.mark.parametrize("shift_hours", [1, -1, 2, 5, -4])
def test_shifts_of_any_size_and_direction_are_followed(shift_hours):
    pipeline, original = established()
    times = tuple(when + shift_hours * HOUR for when in (at(3, 21), at(4, 0), at(5, 15)))
    step = observe_shift(pipeline, original, times)[-1]

    assert_adopted(step, original, shift=shift_hours * HOUR)


def test_shift_is_evaluated_modulo_the_established_period():
    # +5 h on a 4 h cycle is +1 h of phase.
    pipeline, original = established(5, period=P4, start_hour=8)
    times = tuple(when + 5 * HOUR for when in (at(3, 8), at(4, 16), at(6, 4)))
    step = observe_shift(pipeline, original, times)[-1]

    assert_adopted(step, original, shift=HOUR)
    assert regime(step).candidate_period_seconds == P4


def test_shift_by_a_whole_number_of_periods_changes_nothing():
    pipeline, original = established()
    times = tuple(when + 2 * P3 for when in (at(3, 21), at(4, 0), at(5, 15)))
    step = observe_shift(pipeline, original, times)[-1]

    assert time_shift_reasons(step) == set()
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.suspicion_level == 0
    assert_unshifted(step, original)


def test_observations_must_agree_within_the_established_tolerance():
    pipeline, original = established()
    tolerance = regime(original).phase_tolerance
    # The third restart is on another phase (outside tolerance of the first
    # two): it is a different hypothesis and cannot confirm theirs.
    times = (at(3, 22), at(4, 10), at(5, 16) + tolerance + 60)
    step = observe_shift(pipeline, original, times)[-1]

    assert time_shift_reasons(step) == {SUSPECTED}
    assert_unshifted(step, original)

    # Inside the tolerance it does.
    pipeline, original = established()
    times = (at(3, 22), at(4, 10) + 120, at(5, 16) + tolerance - 60)
    step = observe_shift(pipeline, original, times)[-1]
    assert ADOPTED in regime(step).reason_codes


# --------------------------------------------------------------------------
# 7-8. Off-grid restarts and contradictions
# --------------------------------------------------------------------------


def test_off_grid_extra_restart_neither_confirms_nor_invalidates():
    pipeline, original = established()
    off_grid = at(4, 14, 45)
    steps = observe_shift(pipeline, original, (at(3, 22), at(4, 10), off_grid))

    # Two matching restarts plus an unrelated one: still only suspected.
    assert time_shift_reasons(steps[-1]) == {SUSPECTED}
    assert_unshifted(steps[-1], original)

    # The next matching restart is the third and confirms.
    step = advance(pipeline, steps[-1], "third", at(4, 16))
    assert_adopted(step, original, shift=HOUR)
    assert regime(step).established_at == at(4, 16)


def test_restart_back_on_the_original_phase_restarts_the_count():
    pipeline, original = established()
    steps = observe_shift(pipeline, original, (at(3, 22), at(4, 10)))
    assert time_shift_reasons(steps[-1]) == {SUSPECTED}

    contradicted = advance(pipeline, steps[-1], "original-phase", at(4, 15))
    assert time_shift_reasons(contradicted) == set()
    assert contradicted.decision.state is State.ESTABLISHED
    assert contradicted.consumer().countdown_visible
    assert_unshifted(contradicted, original)

    # A later shifted restart is observation one again, not three.
    again = advance(pipeline, contradicted, "after", at(5, 16))
    assert time_shift_reasons(again) == {NOTICED}
    assert_unshifted(again, original)


def test_genuine_miss_at_the_proposed_phase_discards_earlier_observations():
    pipeline, original = established()
    steps = observe_shift(pipeline, original, (at(3, 22), at(4, 10)))
    # The proposed 13:00 restart window was observed and nothing restarted.
    miss = s8.genuine_miss(P3, at(4, 13), key="proposed-phase-miss")
    pipeline.sparse("third", [at(5, 16)])
    step = pipeline.evaluate(s8.persisted(steps[-1].decision), ledger=(miss,))

    assert ADOPTED not in regime(step).reason_codes
    assert time_shift_reasons(step) == {NOTICED}
    assert_unshifted(step, original)

    # Without the miss the same restart confirms.
    confirmed = pipeline.evaluate(s8.persisted(steps[-1].decision))
    assert ADOPTED in regime(confirmed).reason_codes


def test_competing_repeated_phases_prevent_false_adoption():
    pipeline, original = established()
    # Restarts keep returning to two different new phases.  That is not one
    # shifted 3 h schedule, however many of them there are.
    times = (
        at(3, 22),  # +1 h
        at(3, 23),  # +2 h
        at(4, 1),  # +1 h
        at(4, 2),  # +2 h
        at(4, 16),  # +1 h: its third, but +2 h repeated in between
        at(4, 17),  # +2 h: likewise
    )
    pipeline.sparse("competing", times)
    step = pipeline.evaluate(s8.persisted(original.decision))

    assert time_shift_reasons(step) == {SUSPECTED}
    assert step.decision.suspicion_level == 1
    assert not step.consumer().countdown_visible
    assert_unshifted(step, original)

    # Three further restarts on one phase, with the other no longer seen,
    # are a coherent shift and confirm.
    steps = observe_shift(pipeline, step, (at(5, 19), at(6, 1), at(6, 7)), name="settled")
    assert time_shift_reasons(steps[1]) == {SUSPECTED}
    assert_adopted(steps[2], original, shift=HOUR)
    assert regime(steps[2]).established_at == at(6, 7)


def test_sparsely_observed_different_interval_is_not_adopted_as_a_shift():
    pipeline, original = established()
    # A 4 h cycle seen once every 28 h: against the 3 h grid its restarts
    # cycle through three phases, each of them repeating.
    times = tuple(at(1, 10, 30) + index * 7 * P4 for index in range(9))
    for step in observe_shift(pipeline, original, times, name="four-hour"):
        assert_unshifted(step, original)
        # Stage 3: once the 4 h schedule is identified it suspends the
        # strongly established countdown as a cycle change, never a shift.
        assert step.decision.suspicion_level <= 1 or (
            authority.CHALLENGER_SUSPENDS_STRONG_REASON in step.decision.reason_codes
        )
    assert ADOPTED not in time_shift_reasons(step)
    assert authority.CHALLENGER_SUSPENDS_STRONG_REASON in step.decision.reason_codes
    assert step.consumer().presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED


# --------------------------------------------------------------------------
# 9. Interval changes stay with the cycle-change rules
# --------------------------------------------------------------------------


def _never_time_shifted(step):
    return not any(
        ADOPTED in item.reason_codes for item in step.decision.regimes
    ) and ADOPTED not in step.decision.reason_codes


@pytest.mark.parametrize("batch", [1, 7])
def test_genuine_interval_change_is_left_to_cycle_change_rules(batch):
    pipeline, original = established()
    # A continuously observed 4 h cycle.  Its 1st, 4th and 7th restarts share
    # one phase of the old 3 h cycle (12 h apart), so they would look like
    # three time-shift observations if the interval change were ignored.
    session = s8.Session("four-hour", at(3, 10) - 1200, at(3, 10) + 6 * P4 + 1200)
    pipeline.coverage.append(session.coverage())
    restarts = [
        s8.physical_event(
            500 + index,
            at(3, 10) + index * P4,
            session,
            outcome=Outcome.CORROBORATED_OFFLINE_RESTART,
        )
        for index in range(7)
    ]
    assert {(item.canonical_phase_at - at(3, 10)) % P3 for item in restarts[::3]} == {0}

    step = original
    for index in range(0, len(restarts), batch):
        step = inject(pipeline, step, restarts[index : index + batch])
        assert _never_time_shifted(step)

    assert regime(step).candidate_period_seconds == P4
    assert step.decision.state in {State.NEW_REGIME_ESTABLISHED, State.ESTABLISHED}
    superseded = [
        item
        for item in step.decision.regimes
        if item.status is authority.RegimeRecordStatus.SUPERSEDED
    ]
    assert [item.candidate_period_seconds for item in superseded] == [P3]
    assert superseded[0].phase_offset == phase(original)


# --------------------------------------------------------------------------
# 10-11. Evidence rules
# --------------------------------------------------------------------------


def test_unmonitored_time_is_neither_confirmation_nor_contradiction():
    pipeline, original = established()
    steps = observe_shift(pipeline, original, (at(3, 22), at(10, 22)))
    suspected = steps[-1]
    assert time_shift_reasons(suspected) == {SUSPECTED}

    # Weeks pass with no monitoring: rebuilding much later changes nothing.
    later = pipeline.evaluate(s8.persisted(suspected.decision), now=at(60, 0))
    assert later.decision.decision_id == suspected.decision.decision_id
    assert time_shift_reasons(later) == {SUSPECTED}
    assert_unshifted(later, original)

    # Healthy monitoring that contains no restart is not an observation.
    pipeline.coverage.append(s8.Session("quiet", at(61, 1, 20), at(61, 2, 40)).coverage())
    quiet = pipeline.evaluate(s8.persisted(later.decision))
    assert time_shift_reasons(quiet) == {SUSPECTED}
    assert_unshifted(quiet, original)

    # The third genuine restart still confirms, however long the gaps were.
    step = advance(pipeline, quiet, "third", at(70, 16))
    assert_adopted(step, original, shift=HOUR)


INELIGIBLE_SHAPES = {
    "service_interruption": dict(
        outcome=Outcome.SERVICE_INTERRUPTION, authenticity=0.2, schedule_weight_suggestion=0.0
    ),
    "uncertain_a2s": dict(
        outcome=Outcome.UNCERTAIN_A2S_INTERRUPTION,
        authenticity=0.2,
        schedule_weight_suggestion=0.0,
    ),
    "incomplete": dict(
        outcome=Outcome.INCOMPLETE,
        authenticity=0.1,
        schedule_weight_suggestion=0.0,
        coverage_complete=False,
        lifecycle_interruption=s8.detection.LifecycleMarker.SHUTDOWN,
    ),
    "expired": dict(outcome=Outcome.EXPIRED, authenticity=0.1, schedule_weight_suggestion=0.0),
    "coverage_incomplete_restart": dict(coverage_complete=False),
    "lifecycle_interrupted_restart": dict(
        coverage_complete=False, lifecycle_interruption=s8.detection.LifecycleMarker.SLEEP_GAP
    ),
    "unconfirmed_drain": dict(
        outcome=Outcome.AMBIGUOUS_DRAIN, authenticity=0.25, schedule_weight_suggestion=0.15
    ),
}


@pytest.mark.parametrize("name", sorted(INELIGIBLE_SHAPES))
def test_non_qualifying_episodes_never_count_as_observations(name):
    pipeline, original = established()
    steps = observe_shift(pipeline, original, (at(3, 22), at(4, 10)))
    assert time_shift_reasons(steps[-1]) == {SUSPECTED}

    # Exactly on the proposed phase, repeatedly, but never a qualifying restart.
    step = steps[-1]
    for index, when in enumerate((at(4, 16), at(5, 10), at(6, 1))):
        session, event = shaped(f"{name}-{index}", when, 700 + index, **INELIGIBLE_SHAPES[name])
        pipeline.coverage.append(session.coverage())
        step = inject(pipeline, step, [event])
        assert time_shift_reasons(step) == {SUSPECTED}
        assert_unshifted(step, original)


def test_non_qualifying_episodes_alone_are_not_even_noticed():
    pipeline, original = established()
    step = original
    for index, when in enumerate(SHIFTED_GRID):
        session, event = shaped(
            f"blip-{index}", when, 700 + index, **INELIGIBLE_SHAPES["service_interruption"]
        )
        pipeline.coverage.append(session.coverage())
        step = inject(pipeline, step, [event])

    assert time_shift_reasons(step) == set()
    assert step.decision.state is State.ESTABLISHED
    assert_unshifted(step, original)


def test_duplicate_delivery_and_same_occurrence_count_once():
    pipeline, original = established()
    steps = observe_shift(pipeline, original, (at(3, 22), at(4, 10)))
    suspected = steps[-1]

    # The same physical events delivered again (same identities).
    pipeline.events.extend(list(pipeline.events[-2:]))
    replayed = pipeline.evaluate(s8.persisted(suspected.decision))
    assert replayed.decision.decision_id == suspected.decision.decision_id
    assert time_shift_reasons(replayed) == {SUSPECTED}

    # A second episode inside the same scheduled occurrence (two minutes
    # later, a distinct identity) is not an independent observation.
    step = advance(pipeline, replayed, "same-occurrence", at(4, 10, 2))
    assert time_shift_reasons(step) == {SUSPECTED}
    assert_unshifted(step, original)

    # A different occurrence is.
    step = advance(pipeline, step, "third", at(4, 16))
    assert ADOPTED in regime(step).reason_codes


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome.CORROBORATED_OFFLINE_RESTART,
        Outcome.CONFIRMED_OFFLINE_RESTART,
        Outcome.STRONG_QUERY_VISIBLE_RESTART,
        Outcome.PROBABLE_QUERY_VISIBLE_RESTART,
    ],
)
def test_offline_observed_and_qualifying_query_visible_restarts_count(outcome):
    pipeline, original = established()
    step = observe_shift(pipeline, original, SHIFTED_GRID[:3], outcome=outcome)[-1]

    assert_adopted(step, original, shift=HOUR)


def test_unconfirmed_query_visible_drains_do_not_count():
    pipeline, original = established()
    step = observe_shift(
        pipeline, original, SHIFTED_GRID[:3], outcome=Outcome.AMBIGUOUS_DRAIN
    )[-1]

    assert time_shift_reasons(step) == set()
    assert_unshifted(step, original)


# --------------------------------------------------------------------------
# 12-14. Idempotence, persistence, retained authority
# --------------------------------------------------------------------------


@pytest.mark.parametrize("observations", [1, 2, 3, 4])
def test_repeated_rebuilds_of_identical_history_are_a_fixed_point(observations):
    pipeline, original = established()
    step = observe_shift(pipeline, original, SHIFTED_GRID[:observations])[-1]

    first = pipeline.evaluate(s8.persisted(step.decision))
    second = pipeline.evaluate(s8.persisted(first.decision))
    third = pipeline.evaluate(s8.persisted(second.decision))
    assert first.decision.decision_id == second.decision.decision_id == third.decision.decision_id
    assert regime(third) == regime(first)
    assert time_shift_reasons(third) == time_shift_reasons(step)
    assert s8.consume(third, now=step.now) == s8.consume(first, now=step.now)
    if observations >= 3:
        # The adoption happened once: its boundary never moves again.
        assert regime(third).established_at == SHIFTED_GRID[2]
        assert phase(third) == phase(step)


def test_an_adopted_phase_is_the_established_phase_for_later_observations():
    pipeline, original = established()
    adopted = observe_shift(pipeline, original, SHIFTED_GRID[:3])[-1]

    # More restarts on the adopted grid are ordinary aligned evidence.
    aligned = advance(pipeline, adopted, "aligned", at(6, 10))
    assert aligned.decision.state is State.ESTABLISHED
    assert regime(aligned).regime_id == regime(adopted).regime_id
    assert regime(aligned).established_at == regime(adopted).established_at
    assert phase(aligned) == phase(adopted)

    # A restart on the abandoned grid is now the off-phase one: observation
    # one of a possible move back, not a reason to drop the adopted phase.
    back = advance(pipeline, aligned, "old-grid", at(7, 9))
    assert time_shift_reasons(back) == {NOTICED, ADOPTED}
    assert phase(back) == phase(adopted)
    assert back.consumer().countdown_visible


def test_schedule_can_shift_back_and_keeps_its_authority():
    pipeline, original = established()
    adopted = observe_shift(pipeline, original, SHIFTED_GRID[:3])[-1]
    back = observe_shift(
        pipeline, adopted, (at(20, 21), at(21, 9), at(22, 15)), name="back"
    )[-1]

    assert_adopted(back, original, shift=0)
    assert regime(back).established_at == at(22, 15)
    # The original evidence is at this phase again: nothing is carried.
    assert authority._time_shift_authority_phase(regime(back).reason_codes) is None


def test_original_period_and_earned_confidence_are_unchanged_throughout():
    pipeline, original = established()
    for step in observe_shift(pipeline, original, SHIFTED_GRID):
        assert_authority_retained(step, original)
        assert step.consumer().cycle_period_seconds == P3


def test_prior_decision_state_survives_a_serializer_round_trip():
    pipeline, original = established()
    steps = observe_shift(pipeline, original, SHIFTED_GRID[:3])
    for step in steps:
        reopened = s8.persisted(step.decision)
        assert reopened.decision_id == step.decision.decision_id
        assert reopened.reason_codes == step.decision.reason_codes
        assert reopened.incumbent_shadow_regime == regime(step)
        assert reopened.prediction_usable == step.decision.prediction_usable
        assert reopened.countdown_safe == step.decision.countdown_safe


# --------------------------------------------------------------------------
# 18. Retained history corrects the phase in one rebuild
# --------------------------------------------------------------------------


def test_retained_observations_correct_the_phase_in_a_single_rebuild():
    pipeline, original = established()
    # All four shifted restarts are already in history when the build with
    # Time Shift Detection first evaluates it.
    pipeline.sparse("retained", SHIFTED_GRID)
    step = pipeline.evaluate(s8.persisted(original.decision))

    assert_adopted(step, original, shift=HOUR)
    # Adopted at the third observation; the fourth is aligned evidence.
    assert regime(step).established_at == SHIFTED_GRID[2]
    assert step.decision.active_suspicion_evidence_ids == ()
    # ... and identically when the same history is rebuilt again.
    again = pipeline.evaluate(s8.persisted(step.decision))
    assert again.decision.decision_id == step.decision.decision_id


# --------------------------------------------------------------------------
# Production rebuild (compact authority, pruned evidence, JSON reopen)
# --------------------------------------------------------------------------


def reopen(record):
    return json.loads(json.dumps(record, sort_keys=True))


class Production:
    """The production rebuild path with the record reopened at every step."""

    def __init__(self, pipeline=None):
        self.pipeline = pipeline if pipeline is not None else s8.Pipeline()
        self.record = {}

    def rebuild(self):
        events = sorted(self.pipeline.events, key=lambda item: item.canonical_phase_at)
        self.record = reopen(
            integration.rebuild(reopen(self.record), events, self.pipeline.coverage)
        )
        assert not schema4._validate_server_record(s8.SERVER, self.record)
        return r4.production_step(self.record)

    def restart(self, name, when, **changes):
        session, event = shaped(name, when, self.pipeline.sequence, **changes)
        self.pipeline.sequence += 1
        self.pipeline.coverage.append(session.coverage())
        self.pipeline.events.append(event)
        return self.rebuild()


def production_established(count=4):
    production = Production()
    production.pipeline.observe("original", [at(0, 9) + index * P3 for index in range(count)])
    production.rebuild()
    original = production.rebuild()
    assert original.decision.state is State.ESTABLISHED
    return production, original


def test_production_rebuild_follows_notice_suspect_confirm_and_reopens():
    assert schema4.COMPACT_AUTHORITY_ENABLED
    production, original = production_established()

    noticed = production.restart("shift-0", SHIFTED_GRID[0])
    assert time_shift_reasons(noticed) == {NOTICED}
    assert noticed.consumer().countdown_visible

    suspected = production.restart("shift-1", SHIFTED_GRID[1])
    assert time_shift_reasons(suspected) == {SUSPECTED}
    assert suspected.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert not suspected.consumer().countdown_visible
    # Reopened and rebuilt with nothing new: the suspicion is still there.
    settled = production.rebuild()
    assert settled.decision.decision_id == suspected.decision.decision_id
    assert time_shift_reasons(settled) == {SUSPECTED}
    assert_unshifted(settled, original)

    adopted = production.restart("shift-2", SHIFTED_GRID[2])
    assert_adopted(adopted, original, shift=HOUR)
    assert regime(adopted).established_at == SHIFTED_GRID[2]
    # Only the adopted regime is stored and the learned state follows it.
    assert [item["regime_id"] for item in production.record["regimes"]] == [
        regime(adopted).regime_id
    ]
    state = schema4.persisted_learned_state(production.record)
    assert state.incumbent.regime_id == regime(adopted).regime_id
    assert state.incumbent.phase_offset == phase(adopted)
    assert state.superseded == ()

    # Restarting the application (reopen, rebuild) keeps the adoption.
    for _ in range(3):
        again = production.rebuild()
        assert again.decision.decision_id == adopted.decision.decision_id
        assert regime(again) == regime(adopted)
    state = schema4.persisted_learned_state(production.record)
    assert (state.epoch.regime_id, state.epoch.established_at) == (
        regime(adopted).regime_id,
        SHIFTED_GRID[2],
    )

    later = production.restart("shift-3", SHIFTED_GRID[3])
    assert later.decision.state is State.ESTABLISHED
    assert phase(later) == phase(adopted)
    assert later.consumer().countdown_visible


def test_production_adoption_survives_folding_of_the_confirming_restarts():
    production, original = production_established()
    for index, when in enumerate(SHIFTED_GRID[:3]):
        step = production.restart(f"shift-{index}", when)
    adopted = step
    assert ADOPTED in regime(adopted).reason_codes

    # A long run on the adopted grid pushes every earlier restart (the
    # original evidence and the three confirming observations) out of the
    # bounded ring into the learned state.
    first = at(8, 1)
    count = schema4.RAW_EVENT_RETENTION_LIMIT + 12
    production.pipeline.observe("long-run", [first + index * P3 for index in range(count)])
    step = production.rebuild()
    step = production.rebuild()

    retained = {item["event_id"] for item in production.record["physical_events"]}
    assert not retained & {item.event_id for item in adopted.events}
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.suspicion_level == 0
    assert regime(step).regime_id == regime(adopted).regime_id
    assert phase(step) == pytest.approx(phase(adopted), abs=1.0)
    assert regime(step).candidate_period_seconds == P3
    assert regime(step).high_authority >= regime(original).high_authority
    assert step.consumer().countdown_visible
    # The regime now stands on evidence at its own phase.
    assert authority._time_shift_authority_phase(regime(step).reason_codes) is None


# --------------------------------------------------------------------------
# Dawn of the Bobs regression (synthetic copy of the necessary evidence)
# --------------------------------------------------------------------------
#
# The real server: an established 3 h cycle at 00/03/.../21 UTC.  After the
# October DST change its restarts continued every 3 h, one hour later in UTC.
# Day 0 here is 24 October; restarts are offset a few seconds as observed.

BOBS_ORIGINAL = (at(0, 15) + 8, at(0, 18) + 5, at(0, 21) + 5, at(1, 0) + 10)
BOBS_SHIFTED = {
    "31 Oct 22:00": at(7, 22) + 5,
    "1 Nov 01:00": at(8, 1) + 1,
    "1 Nov 16:00": at(8, 16) + 6,
    "9 Nov 10:00": at(16, 10) + 7,
}
BOBS_OFF_GRID = at(8, 14, 45) + 26  # 1 Nov 14:45, an extra restart
BOBS_INELIGIBLE = (at(11, 22) + 6, at(15, 1) + 4)  # 4 Nov 22:00, 8 Nov 01:00
BOBS_SERVICE_INTERRUPTIONS = (
    at(7, 22, 25),
    at(7, 23, 25),
    at(7, 23, 33),
    at(20, 1) + 9,  # 13 Nov 01:00, recorded as a service interruption
)
SERVICE_INTERRUPTION = INELIGIBLE_SHAPES["service_interruption"]


def test_dawn_of_the_bobs_history_is_corrected_at_the_third_qualifying_restart():
    # Stage 5 (was: corrected at the third qualifying restart, 1 Nov 16:00):
    # the first displaced restart is an internal suspicion only (the
    # established countdown stays), and 1 Nov 01:00, three hours after 31 Oct
    # 22:00 (which no 2 h, 4 h or 8 h grid contains), corroborates it.
    production, original = production_established_at(BOBS_ORIGINAL)
    assert phase(original) == pytest.approx(8.0, abs=5.0)  # the old UTC phase
    confidence = s8.percent(original)

    first = production.restart("oct31-2200", BOBS_SHIFTED["31 Oct 22:00"])
    assert time_shift_reasons(first) == {NOTICED}
    assert DISPLACED in first.decision.reason_codes
    for index, when in enumerate(BOBS_SERVICE_INTERRUPTIONS[:3]):
        step = production.restart(f"blip-{index}", when, **SERVICE_INTERRUPTION)
        assert time_shift_reasons(step) == {NOTICED}
        assert_unshifted(step, original)
    shown = panel(step)
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_percent"] == confidence
    assert shown["countdown_visible"]

    second = production.restart("nov01-0100", BOBS_SHIFTED["1 Nov 01:00"])
    assert_adopted(second, original, shift=HOUR, tolerance=10.0)
    assert phase(second) == pytest.approx(HOUR + 3, abs=5.0)  # XX:00:0x on 01/04/.../22
    assert regime(second).established_at == BOBS_SHIFTED["1 Nov 01:00"]
    assert s8.percent(second) == confidence

    # The off-grid extra restart (not one hour off) only notices; the
    # adopted grid keeps its countdown.
    extra = production.restart("nov01-1445", BOBS_OFF_GRID)
    assert time_shift_reasons(extra) == {NOTICED, ADOPTED}
    assert regime(extra).regime_id == regime(second).regime_id
    assert extra.consumer().countdown_visible

    third = production.restart("nov01-1600", BOBS_SHIFTED["1 Nov 16:00"])
    assert third.decision.state is State.ESTABLISHED
    assert regime(third).regime_id == regime(second).regime_id
    assert third.consumer().next_expected_restart_at == pytest.approx(at(8, 19), abs=10.0)

    # Coverage-ineligible restarts stay ineligible; they change nothing.
    for index, when in enumerate(BOBS_INELIGIBLE):
        step = production.restart(f"ineligible-{index}", when, coverage_complete=False)
        assert regime(step) == regime(third)
    fourth = production.restart("nov09-1000", BOBS_SHIFTED["9 Nov 10:00"])
    blip = production.restart("nov13-0100", BOBS_SERVICE_INTERRUPTIONS[3], **SERVICE_INTERRUPTION)

    assert blip.decision.state is State.ESTABLISHED
    assert blip.decision.suspicion_level == 0
    assert regime(blip).regime_id == regime(third).regime_id
    assert regime(blip).candidate_period_seconds == P3
    assert s8.percent(blip) == confidence
    # 13 Nov, after the 01:00 restart: the next restart is 04:00 UTC, with
    # its scheduled warning five minutes earlier.
    now = at(20, 1, 30)
    value = blip.consumer(now=now)
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.next_expected_restart_at == pytest.approx(at(20, 4), abs=10.0)
    assert panel(blip, now=now)["cycle_text"] == "Every 3 Hours"
    assert panel(blip, now=now)["countdown_text"] == "02:30"
    due = blip.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_pre_restart_alert_eligible
    assert fourth.decision.state is State.ESTABLISHED


def production_established_at(times):
    production = Production()
    production.pipeline.observe("original", list(times))
    production.rebuild()
    original = production.rebuild()
    assert original.decision.state is State.ESTABLISHED
    return production, original


def test_dawn_of_the_bobs_retained_history_is_corrected_by_one_rebuild():
    """The upgrade case: the whole history predates Time Shift Detection."""

    production, original = production_established_at(BOBS_ORIGINAL)
    record_before = reopen(production.record)
    pipeline = production.pipeline

    def add(name, when, **changes):
        session, event = shaped(name, when, pipeline.sequence, **changes)
        pipeline.sequence += 1
        pipeline.coverage.append(session.coverage())
        pipeline.events.append(event)

    for name, when in BOBS_SHIFTED.items():
        add(name, when)
    add("off-grid", BOBS_OFF_GRID)
    for index, when in enumerate(BOBS_INELIGIBLE):
        add(f"ineligible-{index}", when, coverage_complete=False)
    for index, when in enumerate(BOBS_SERVICE_INTERRUPTIONS):
        add(f"blip-{index}", when, **SERVICE_INTERRUPTION)

    step = production.rebuild()
    assert_adopted(step, original, shift=HOUR, tolerance=10.0)
    # Stage 5: the corroborated pair adopts at 1 Nov 01:00 (was the third
    # qualifying restart, 1 Nov 16:00), exactly as live learning does.  The
    # ineligible restarts and the off-grid one are not counted.
    assert regime(step).established_at == BOBS_SHIFTED["1 Nov 01:00"]
    assert step.decision.active_suspicion_evidence_ids == ()
    assert step.consumer(now=at(20, 1, 30)).next_expected_restart_at == pytest.approx(
        at(20, 4), abs=10.0
    )
    assert production.rebuild().decision.decision_id == step.decision.decision_id
    assert record_before["regimes"][0]["phase_offset"] == phase(original)


def test_dawn_of_the_bobs_ineligible_restarts_cannot_be_the_third_observation():
    # Stage 5: after the first displaced restart, ineligible restarts on the
    # new grid can be neither the corroborating second nor a third: the
    # suspicion stays internal and the established countdown stays.
    production, original = production_established_at(BOBS_ORIGINAL)
    step = production.restart("oct31-2200", BOBS_SHIFTED["31 Oct 22:00"])
    for index, when in enumerate(BOBS_INELIGIBLE):
        step = production.restart(f"ineligible-{index}", when, coverage_complete=False)
    step = production.restart("nov13-0100", BOBS_SERVICE_INTERRUPTIONS[3], **SERVICE_INTERRUPTION)

    assert time_shift_reasons(step) == {NOTICED}
    assert DISPLACED in step.decision.reason_codes
    assert_unshifted(step, original)
    assert step.consumer().presentation_state is Presentation.CONFIRMED_CYCLE
    assert step.consumer().countdown_visible


# --------------------------------------------------------------------------
# UI wording
# --------------------------------------------------------------------------


def test_possible_time_change_wording_and_tooltip():
    assert consumers4.AuthorityPresentationKey.POSSIBLE_TIME_CHANGE.value == "possible_time_change"
    assert server_companion_ui.RESTART_PRESENTATION_TOOLTIPS["possible_time_change"] == (
        "The server appears to be restarting at different times while keeping its "
        "established restart interval. Predictions will resume when the new schedule "
        "is confirmed."
    )
    pipeline, original = established()
    step = observe_shift(pipeline, original, SHIFTED_GRID[:2])[-1]
    label = panel(step)["cycle_text"]
    assert label == "Possible Time Change"
    # The approved short form: it must not be longer than the labels the
    # fixed-width panel already shows in this row.
    assert len(label) <= len("Suspected Cycle Change")
    assert server_companion_ui.ServerCompanionPanel.WIDTH == 280
