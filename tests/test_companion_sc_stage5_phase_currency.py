"""Server Companion verified acquisition, Stage 5: DST phase currency
(operator correction: protected suspicion).

One restart one hour from the established UTC phase is an internal suspicion
only: the established cycle, its countdown and scheduled warnings stay.  The
prediction is withheld when genuine monitored misses of the established
phase follow (E1: one; E2: two consecutive), never because a window passed
unobserved.  A second displaced restart adopts the new phase when the pair
rules out a change of interval (existing Time Shift Detection otherwise).
An aligned restart clears the suspicion.  Period, history, confidence and
graduated authority are always kept.
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
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
DAY = 24 * HOUR
P3 = s8.P3
P4 = 4 * HOUR
at = s8.at
State = authority.RegimeState
Status = authority.RegimeRecordStatus
Presentation = consumers4.AuthorityPresentationKey
Outcome = s8.Outcome

DISPLACED = authority.TIME_SHIFT_DISPLACED_REASON
SUSPECTED = authority.TIME_SHIFT_SUSPECTED_REASON
NOTICED = authority.TIME_SHIFT_NOTICED_REASON
ADOPTED = authority.TIME_SHIFT_ADOPTED_REASON
SEVERAL = authority.TIME_SHIFT_SEVERAL_PHASES_REASON
CONSECUTIVE = authority.CONSECUTIVE_MISSES_REASON
UNKNOWN = st3.windows.ExpectedWindowOutcome.UNKNOWN
AMBIGUOUS = st3.windows.ExpectedWindowOutcome.AMBIGUOUS
STRONG = authority.REGIME_STRONG_REASON

regime = st3.regime
advance = st3.advance
add_sparse = st3.add_sparse
tiny_ring = r4.tiny_ring


def unobserved(step, days=30):
    """The first scheduled occurrence ``days`` after the step (3 h grid)."""

    return (int((step.now + days * DAY) // P3) + 1) * P3


def observe(pipeline, step, when, name, *, ledger=(), **kwargs):
    add_sparse(pipeline, [when], name, **kwargs)
    return advance(pipeline, step, ledger=ledger)


def miss(when, key):
    return st3.miss(P3, when, key)


def unknown(when, outcome=UNKNOWN):
    return st3.neutral(P3, when, outcome)


def prediction(step):
    value = step.consumer()
    due = (
        step.consumer(now=value.next_expected_restart_at - 300)
        if value.next_expected_restart_at is not None
        else value
    )
    return (
        value.presentation_state,
        value.countdown_visible,
        value.next_expected_restart_at,
        due.scheduled_warning_eligible,
        due.scheduled_warning_suppression_key,
        value.confidence_display_value,
    )


def assert_unchanged_by_suspicion(monkeypatch, build, steps):
    """The suspicion is internal: replaying the same evidence with the
    one-hour displacement rule switched off gives the same presentation,
    countdown, scheduled warning and key (whatever earlier stages decide for
    this cycle)."""

    actual = steps(*build())
    with monkeypatch.context() as patch:
        patch.setattr(authority, "_displaced_observation", lambda event, **kwargs: False)
        baseline = steps(*build())
    assert [prediction(item) for item in actual] == [prediction(item) for item in baseline]
    return actual


def assert_protected(step, original):
    """Internal suspicion at most: the established cycle, its phase and
    confidence are kept and the prediction is exactly as without it."""

    value = step.consumer()
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).phase_offset == regime(original).phase_offset
    assert regime(step).status is Status.ESTABLISHED
    assert step.decision.combined_confidence >= original.decision.combined_confidence - 1e-9
    assert (STRONG in regime(step).reason_codes) is (STRONG in regime(original).reason_codes)
    assert SUSPECTED not in step.decision.reason_codes
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert ADOPTED not in regime(step).reason_codes
    return value


def assert_withheld(step, original):
    """Unsafe phase: the cycle, period, phase and confidence are kept; no
    countdown, next restart, scheduled warning or warning key."""

    value = step.consumer()
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).candidate_period_seconds == regime(original).candidate_period_seconds
    assert regime(step).phase_offset == regime(original).phase_offset
    assert regime(step).status is Status.ESTABLISHED
    assert step.decision.combined_confidence >= original.decision.combined_confidence - 1e-9
    assert not step.decision.prediction_usable and not step.decision.countdown_safe
    assert not value.countdown_visible and value.next_expected_restart_at is None
    assert value.scheduled_warning_suppression_key is None
    for offset in (60, HOUR, 2 * HOUR, 3 * HOUR):
        later = step.consumer(now=step.now + offset)
        assert not later.scheduled_warning_eligible and not later.scheduled_pre_restart_alert_eligible
    assert value.cycle_period_seconds == regime(original).candidate_period_seconds
    return value


def assert_predicting(step, phase):
    value = step.consumer()
    period = regime(step).candidate_period_seconds
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert (value.next_expected_restart_at - phase) % period == pytest.approx(0.0, abs=1.0)
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_suppression_key.regime_id == regime(step).regime_id
    return value


def four_hour_e2():
    pipeline = s8.Pipeline()
    pipeline.observe("four", [at(0, 2) + index * P4 for index in range(5)])
    step = s8.settled(pipeline)
    assert regime(step).candidate_period_seconds == P4 and STRONG in regime(step).reason_codes
    return pipeline, step


# --------------------------------------------------------------------------
# 1-2, 14. First displaced restart: internal suspicion only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sign", [1, -1], ids=["one_hour_late", "one_hour_early"])
@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_one_displaced_restart_keeps_the_established_prediction(monkeypatch, build, sign):
    def steps(pipeline, original):
        return [observe(pipeline, original, at(8, 6) + sign * HOUR, "admin")]

    (step,) = assert_unchanged_by_suspicion(monkeypatch, build, steps)
    pipeline, original = build()
    step = observe(pipeline, original, at(8, 6) + sign * HOUR, "admin")
    assert DISPLACED in step.decision.reason_codes
    assert_protected(step, original)
    if build is st3.e2 or sign < 0:
        # The established 06:00/09:00 prediction, countdown and warning stay.
        # (An E1 cycle whose 07:00 restart also lines up with earlier history
        # at another interval already hides its countdown before Stage 5,
        # through the existing latent-challenger rule; unchanged above.)
        assert_predicting(step, 0.0)
    st3.assert_fixed_point(pipeline, step)
    # A: the expected restart happens; the suspicion clears.
    back = observe(pipeline, step, at(9, 9), "scheduled")
    assert DISPLACED not in back.decision.reason_codes
    assert back.decision.state is State.ESTABLISHED
    assert_protected(back, original)
    assert_predicting(back, 0.0)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_extra_restart_one_hour_after_an_observed_scheduled_restart_is_not_a_displacement(build):
    pipeline, original = build()
    scheduled = observe(pipeline, original, at(8, 9), "scheduled")
    extra = observe(pipeline, scheduled, at(8, 10, 1), "extra")
    assert DISPLACED not in extra.decision.reason_codes


# --------------------------------------------------------------------------
# 3-6. Genuine monitored misses: E1 one, E2 two consecutive
# --------------------------------------------------------------------------


def test_e1_displacement_then_one_genuine_miss_withholds_until_reconfirmed():
    pipeline, original = st3.e1()
    ledger = (miss(at(8, 6), "miss-0600"),)
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert {DISPLACED, SUSPECTED} <= set(step.decision.reason_codes)
    assert step.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert_withheld(step, original)
    st3.assert_fixed_point(pipeline, step, ledger=ledger)
    back = observe(pipeline, step, at(8, 9), "scheduled", ledger=ledger)
    assert_protected(back, original)
    assert_predicting(back, 0.0)


def test_e2_displacement_then_one_genuine_miss_keeps_the_prediction():
    pipeline, original = st3.e2()
    ledger = (miss(at(8, 6), "miss-0600"),)
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert DISPLACED in step.decision.reason_codes
    assert authority.STRONG_ANOMALY_TOLERANCE_REASON in step.decision.reason_codes
    assert_protected(step, original)
    assert_predicting(step, 0.0)


def test_e2_displacement_then_two_consecutive_genuine_misses_withholds():
    pipeline, original = st3.e2()
    ledger = (miss(at(8, 6), "miss-0600"), miss(at(8, 9), "miss-0900"))
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert {DISPLACED, SUSPECTED} <= set(step.decision.reason_codes)
    assert step.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert_withheld(step, original)
    assert STRONG in regime(step).reason_codes


def test_e2_two_consecutive_misses_withhold_even_without_an_alternative():
    pipeline, original = st3.e2()
    ledger = (miss(at(8, 6), "miss-0600"), miss(at(8, 9), "miss-0900"))
    step = advance(pipeline, original, ledger=ledger)
    assert CONSECUTIVE in step.decision.reason_codes
    assert step.decision.state is State.CHANGE_SUSPECTED and STRONG in regime(step).reason_codes
    assert_withheld(step, original)
    # The original schedule resumes: prediction back, nothing lost.
    back = observe(pipeline, step, at(8, 12), "scheduled", ledger=ledger)
    assert_protected(back, original)
    assert_predicting(back, 0.0)


@pytest.mark.parametrize("outcome", [UNKNOWN, AMBIGUOUS], ids=["unmonitored", "outage"])
def test_e2_misses_separated_by_an_unknown_window_are_not_consecutive(outcome):
    pipeline, original = st3.e2()
    ledger = (miss(at(8, 6), "miss-0600"), unknown(at(8, 9), outcome), miss(at(8, 12), "miss-1200"))
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert CONSECUTIVE not in step.decision.reason_codes
    assert_protected(step, original)
    assert_predicting(step, 0.0)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_admin_restart_then_skipped_scheduled_restart_then_original_schedule(monkeypatch, build):
    def until_admin(pipeline, original):
        scheduled = observe(pipeline, original, at(8, 3), "0300")
        return [scheduled, observe(pipeline, scheduled, at(8, 5), "0500")]

    assert_unchanged_by_suspicion(monkeypatch, build, until_admin)
    pipeline, original = build()
    _scheduled, admin = until_admin(pipeline, original)
    assert DISPLACED in admin.decision.reason_codes
    assert_protected(admin, original)
    if build is st3.e2:
        assert_predicting(admin, 0.0)
    ledger = (miss(at(8, 6), "skipped-0600"),)
    skipped = advance(pipeline, admin, ledger=ledger)
    if build is st3.e1:
        # E1: one genuine miss after the displacement withholds.
        assert_withheld(skipped, original)
    else:
        # E2 rides through one skipped automatic restart.
        assert_protected(skipped, original)
        assert_predicting(skipped, 0.0)
    resumed = observe(pipeline, skipped, at(8, 9), "0900", ledger=ledger)
    assert DISPLACED not in resumed.decision.reason_codes
    assert_protected(resumed, original)
    assert_predicting(resumed, 0.0)
    assert regime(resumed).regime_id == regime(original).regime_id


# --------------------------------------------------------------------------
# 7-9. Unknown windows and corroboration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [UNKNOWN, AMBIGUOUS], ids=["unmonitored", "outage"])
@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_unmonitored_expected_window_is_neutral_then_the_original_phase_returns(build, outcome):
    pipeline, original = build()
    ledger = (unknown(at(8, 6), outcome),)
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert DISPLACED in step.decision.reason_codes
    assert_protected(step, original)
    assert_predicting(step, 0.0)
    # Days without monitoring: still pending, still predicting, unchanged.
    later = pipeline.evaluate(s8.persisted(step.decision), ledger=ledger, now=step.now + 5 * DAY)
    assert later.decision.decision_id == step.decision.decision_id
    back = observe(pipeline, step, at(12, 9), "scheduled", ledger=ledger)
    assert DISPLACED not in back.decision.reason_codes
    assert_predicting(back, 0.0)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_unmonitored_window_then_a_second_shifted_restart_adopts(build):
    pipeline, original = build()
    ledger = (unknown(at(8, 6)),)
    first = observe(pipeline, original, at(8, 5), "0500", ledger=ledger)
    assert_protected(first, original)
    second = observe(pipeline, first, at(8, 8), "0800", ledger=ledger)
    assert ADOPTED in regime(second).reason_codes
    assert regime(second).phase_offset == pytest.approx(P3 - HOUR, abs=1.0)
    assert regime(second).candidate_period_seconds == P3
    assert second.decision.combined_confidence == original.decision.combined_confidence
    assert (STRONG in regime(second).reason_codes) is (STRONG in regime(original).reason_codes)
    assert_predicting(second, P3 - HOUR)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_s13_established_cycle_shifted_after_30_unobserved_days(build):
    pipeline, original = build()
    later = original.consumer(now=original.now + 30 * DAY)
    assert later.countdown_visible
    first_at = unobserved(original) + HOUR
    first = observe(pipeline, original, first_at, "shift-1")
    # Deliberate (operator correction): the first displaced restart is an
    # internal suspicion, so the old prediction stays visible.
    assert DISPLACED in first.decision.reason_codes
    assert_protected(first, original)
    assert_predicting(first, 0.0)
    # The second, 15 h later (no 2 h, 4 h or 8 h grid holds both): adopted.
    second = observe(pipeline, first, first_at + 5 * P3, "shift-2")
    assert ADOPTED in regime(second).reason_codes
    assert regime(second).phase_offset == HOUR
    assert regime(second).established_at == first_at + 5 * P3
    assert second.decision.combined_confidence == original.decision.combined_confidence
    assert (STRONG in regime(second).reason_codes) is (STRONG in regime(original).reason_codes)
    assert_predicting(second, HOUR)
    st3.assert_fixed_point(pipeline, second)


def test_s13_with_a_genuine_miss_withholds_an_e1_cycle_at_the_first_restart():
    pipeline, original = st3.e1()
    first_at = unobserved(original) + HOUR
    ledger = (miss(first_at - HOUR, "missed-old-phase"),)
    first = observe(pipeline, original, first_at, "shift-1", ledger=ledger)
    assert {DISPLACED, SUSPECTED} <= set(first.decision.reason_codes)
    assert_withheld(first, original)


# --------------------------------------------------------------------------
# 10-11, 13, 15. Harmonics, repeated admins, unrelated admins, period change
# --------------------------------------------------------------------------


def test_four_hour_cycle_keeps_harmonic_protection():
    pipeline, original = four_hour_e2()
    first = observe(pipeline, original, at(10, 1), "earlier-1")
    assert_protected(first, original)
    # A 4 h pair cannot rule out 2 h without a witnessed interval: withheld
    # (two observations, existing rule), adopted at the third.
    second = observe(pipeline, first, at(11, 9), "earlier-2")
    assert ADOPTED not in regime(second).reason_codes
    assert_withheld(second, original)
    third = observe(pipeline, second, at(12, 17), "earlier-3")
    assert ADOPTED in regime(third).reason_codes
    assert regime(third).phase_offset == pytest.approx(HOUR, abs=1.0)


def test_four_hour_pair_with_a_witnessed_interval_adopts_at_the_second():
    pipeline, original = four_hour_e2()
    first = observe(pipeline, original, at(10, 1), "earlier-1")
    pipeline.observe("witnessed", [at(11, 1), at(11, 5)])
    step = advance(pipeline, first)
    assert ADOPTED in regime(step).reason_codes
    assert_predicting(step, HOUR)


def test_six_hour_pair_without_a_witnessed_interval_never_adopts_at_two():
    pipeline = s8.Pipeline()
    pipeline.observe("six", [at(0, 0) + index * 6 * HOUR for index in range(5)])
    original = s8.settled(pipeline)
    assert regime(original).candidate_period_seconds == 6 * HOUR
    first = observe(pipeline, original, at(8, 7), "late-1")
    assert_protected(first, original)
    second = observe(pipeline, first, at(9, 13) + 6 * HOUR * 0, "late-2")
    assert ADOPTED not in regime(second).reason_codes


def test_repeated_admin_restarts_at_one_alternative_phase_are_withheld_not_adopted():
    pipeline, original = st3.e2()
    first = observe(pipeline, original, at(8, 10), "admin-1")
    assert_protected(first, original)
    # A second at the same alternative phase 24 h later supports a shift but
    # cannot rule out another interval: existing two-observation rule.
    second = observe(pipeline, first, at(9, 10), "admin-2")
    assert ADOPTED not in regime(second).reason_codes
    assert second.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert_withheld(second, original)
    back = observe(pipeline, second, at(9, 15), "scheduled")
    assert regime(back).phase_offset == regime(original).phase_offset
    assert_predicting(back, 0.0)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_two_unrelated_admin_restarts_one_hour_either_side(build):
    pipeline, original = build()
    first = observe(pipeline, original, at(8, 10), "late")
    second = observe(pipeline, first, at(9, 14), "early")
    assert ADOPTED not in regime(second).reason_codes
    assert SEVERAL not in second.decision.reason_codes
    assert SUSPECTED not in second.decision.reason_codes
    if build is st3.e2:
        # E2 tolerates unrelated anomalies.
        assert_predicting(second, 0.0)


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_genuine_period_change_is_never_adopted_as_a_phase_shift(build):
    pipeline, original = build()
    step = original
    for index, when in enumerate(st3.FOUR_HOUR):
        step = observe(pipeline, step, when, f"four-{index}")
        assert not any(ADOPTED in item.reason_codes for item in step.decision.regimes)
    if build is st3.e1:
        st3.assert_awaiting(step, P4, regime(original).regime_id)
    else:
        assert authority.CHALLENGER_SUSPENDS_STRONG_REASON in step.decision.reason_codes
        assert not step.consumer().countdown_visible


def test_red_dawn_like_coincidental_restart_keeps_the_grid_phase():
    for pipeline in (har.red_dawn_like(), har.red_dawn_like(new=har.NEW[:4], off=())):
        prior = None
        full = s8.Pipeline(coverage=list(pipeline.coverage))
        adopted = []
        for event in sorted(pipeline.events, key=lambda item: item.canonical_phase_at):
            full.events.append(event)
            step = full.evaluate(prior)
            prior = s8.persisted(step.decision)
            if regime(step) is not None and ADOPTED in regime(step).reason_codes:
                adopted.append(step)
        assert adopted
        assert all(har.phase_distance(regime(item).phase_offset, 0.0) <= 300 for item in adopted)
        assert regime(adopted[-1]).phase_offset == 0.0


def test_midnight_wraparound():
    pipeline = s8.Pipeline()
    pipeline.observe("late", [at(0, 0, 30) + index * P3 for index in range(4)])
    original = s8.settled(pipeline)
    first = observe(pipeline, original, at(9, 23, 30), "early-1")
    assert DISPLACED in first.decision.reason_codes
    assert_protected(first, original)
    second = observe(pipeline, first, at(10, 23, 30) + 3 * P3, "early-2")
    assert ADOPTED in regime(second).reason_codes
    assert regime(second).phase_offset == pytest.approx(P3 - 30 * 60, abs=1.0)


@pytest.mark.parametrize("delta,displaced", [(-60, True), (60, False)])
def test_jitter_around_the_displacement_tolerance(delta, displaced):
    pipeline, original = st3.e2()
    tolerance = scoring.candidate_phase_tolerance(P3)
    probe = s8.sparse_pipeline([at(0, 0)]).events[0]
    edge = tolerance + min(scoring.PHASE_UNCERTAINTY_CAP, probe.phase_uncertainty)
    step = observe(pipeline, original, at(8, 10) + edge + delta, "edge")
    assert (DISPLACED in step.decision.reason_codes) is displaced
    assert step.consumer().countdown_visible


# --------------------------------------------------------------------------
# 16-18. Evidence safeguards
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"coverage_complete": False, "lifecycle_interruption": s8.detection.LifecycleMarker.SLEEP_GAP},
            id="local_outage_sleep_gap",
        ),
        pytest.param({"coverage_complete": False}, id="incomplete_oo"),
        pytest.param({"outcome": Outcome.AMBIGUOUS_DRAIN, "authenticity": 0.25}, id="qv_drain_without_repopulation"),
        pytest.param({"outcome": Outcome.SERVICE_INTERRUPTION, "authenticity": 0.2}, id="service_interruption"),
        pytest.param({"authenticity": 0.2}, id="inauthentic"),
    ],
)
def test_non_qualifying_episodes_never_suspect_escalate_or_corroborate(changes):
    pipeline, original = st3.e1()
    ledger = (miss(at(8, 6), "miss-0600"),)
    step = original
    for index, when in enumerate((at(8, 5), at(8, 8))):
        (event,) = pipeline.observe(f"invalid-{index}", [when])
        pipeline.events[pipeline.events.index(event)] = replace(event, **changes)
        step = advance(pipeline, step, ledger=ledger)
        assert not {SUSPECTED, DISPLACED}.intersection(step.decision.reason_codes)
        assert ADOPTED not in regime(step).reason_codes


def test_a1_follower_one_hour_off_is_not_a_displacement():
    pipeline, original = st3.e2()
    step = observe(pipeline, original, at(8, 9, 25), "onset")
    step = observe(pipeline, step, at(8, 10), "follower")
    assert DISPLACED not in step.decision.reason_codes


def test_generic_recovery_alert_stays_independent():
    pipeline, original = st3.e1()
    ledger = (miss(at(8, 6), "miss-0600"),)
    step = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    value = s8.consume(step, now=step.now, physical_recovery_event_id=step.events[-1].event_id)
    assert not value.countdown_visible
    assert value.generic_recovery_suppression_key is not None


# --------------------------------------------------------------------------
# 20, 22-23. Reopen, warning keys, disproved hypotheses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
def test_pending_suspicion_survives_reopen_and_is_resolved_by_new_evidence(build):
    pipeline, original = build()
    step = observe(pipeline, original, at(8, 5), "admin")
    reopened = pipeline.evaluate(s8.persisted(step.decision))
    assert reopened.decision == step.decision
    assert DISPLACED in reopened.decision.reason_codes
    # Reopened, then another shifted restart (3 h later): adopted.
    shifted_pipeline = pipeline.copy()
    shifted = observe(shifted_pipeline, reopened, at(8, 8), "0800")
    assert ADOPTED in regime(shifted).reason_codes
    # Reopened, then an original-phase restart: cleared.
    cleared = observe(pipeline, reopened, at(9, 9), "0900")
    assert DISPLACED not in cleared.decision.reason_codes
    assert_predicting(cleared, 0.0)


def test_warning_keys_are_never_stale_or_duplicated():
    pipeline, original = st3.e2()
    first_at = unobserved(original) + HOUR
    first = observe(pipeline, original, first_at, "shift-1")
    protected_value = first.consumer()
    protected_key = first.consumer(now=protected_value.next_expected_restart_at - 300).scheduled_warning_suppression_key
    assert protected_key.regime_id == regime(original).regime_id
    second = observe(pipeline, first, first_at + 5 * P3, "shift-2")
    value = second.consumer()
    key = second.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_suppression_key
    assert key.regime_id == regime(second).regime_id != protected_key.regime_id
    assert (key.expected_occurrence_at - HOUR) % P3 == 0
    reopened = pipeline.evaluate(s8.persisted(second.decision))
    again = reopened.consumer(now=value.next_expected_restart_at - 300, fired_keys=frozenset({key}))
    assert again.scheduled_warning_suppression_key == key
    assert not again.scheduled_pre_restart_alert_eligible
    # While an E1 cycle is withheld no key exists at all.
    e1_pipeline, e1 = st3.e1()
    withheld = observe(e1_pipeline, e1, at(8, 5), "admin", ledger=(miss(at(8, 6), "m"),))
    for offset in range(0, 3 * P3, 600):
        assert withheld.consumer(now=withheld.now + offset).scheduled_warning_suppression_key is None


def test_shifted_hypothesis_disproved_by_the_original_schedule():
    pipeline, original = st3.e1()
    ledger = (miss(at(8, 6), "miss-0600"),)
    withheld = observe(pipeline, original, at(8, 5), "admin", ledger=ledger)
    assert_withheld(withheld, original)
    back = observe(pipeline, withheld, at(8, 9), "0900", ledger=ledger)
    later = observe(pipeline, back, at(9, 11, 0) + HOUR, "1200", ledger=ledger)
    assert regime(later).phase_offset == regime(original).phase_offset
    assert_predicting(later, 0.0)


# --------------------------------------------------------------------------
# 21. Raw/compact parity and production persistence
# --------------------------------------------------------------------------


def _s13_pipeline():
    pipeline = s8.verified_sparse_pipeline()
    base = at(40, 1)
    add_sparse(pipeline, [base, base + 5 * P3, base + 11 * P3], "shifted")
    return pipeline


def _admin_pipeline():
    pipeline = s8.verified_sparse_pipeline()
    add_sparse(pipeline, [at(9, 5), at(9, 9)], "admin-then-original")
    return pipeline


def _unobserved_then_shifted_pipeline():
    pipeline = s8.verified_sparse_pipeline()
    add_sparse(pipeline, [at(9, 5), at(9, 8)], "shifted-pair")
    return pipeline


PARITY_LEDGER = (miss(at(9, 6), "parity-miss"),)


@pytest.mark.parametrize("capacity", [2, 3, 5, 20])
@pytest.mark.parametrize(
    "factory,ledger",
    [
        (_s13_pipeline, ()),
        (_admin_pipeline, ()),
        (_admin_pipeline, PARITY_LEDGER),
        (_unobserved_then_shifted_pipeline, (unknown(at(9, 6)),)),
    ],
    ids=["s13", "admin", "admin_with_miss", "unknown_then_shifted"],
)
def test_raw_and_compact_agree_on_phase_currency(factory, ledger, capacity):
    pipeline = factory()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches, ledgers=[ledger] * len(batches))
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch, windows=ledger)
        label = (capacity, index)
        compact_authority.assert_same_display_bounded(
            raw[index], compact, label, consumer=regime(raw[index]) is not None
        )
        for reason in (DISPLACED, SUSPECTED):
            assert (reason in compact.decision.reason_codes) is (reason in raw[index].decision.reason_codes), label
        if regime(raw[index]) is not None:
            assert (ADOPTED in regime(compact).reason_codes) is (ADOPTED in regime(raw[index]).reason_codes)


def test_production_lifecycle_validates_and_reopens_identically(monkeypatch, tiny_ring):
    tiny_ring(3)
    pipeline = _s13_pipeline()
    records = compact_authority.run(pipeline, every=1, compact_from=0, monkeypatch=monkeypatch)
    for record in records:
        assert not schema4._validate_server_record(s8.SERVER, record)
    final = records[-1]
    assert schema4.persisted_learned_state(final).schema_version == 5
    assert ADOPTED in schema4.persisted_authority_decision(final).incumbent_shadow_regime.reason_codes
    reopened = json.loads(schema4.canonical_json_bytes(final))
    again = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == final["authority_decision"]
    assert again["learned_state"] == final["learned_state"]
