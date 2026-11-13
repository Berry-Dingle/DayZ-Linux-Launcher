"""Time Shift Detection for sparse-established regimes.

A sparse-established regime (NORMAL origin, established by the Stage 8.5
sparse gate) moves to a new time while its restart interval and earned
confidence are kept.  Its confidence is the sparse per-phase statistic, so
the earned value is that statistic at the authority phase over the restarts
strictly before the first shifted observation; it is carried until the
regime's own phase supports at least as much.

Synthetic evidence only, with the same drivers as the continuity suite
(``test_companion_sc_time_shift_detection``): the Stage 8 pipeline with the
prior decision round-tripped through the schema-4 serializer, the compact
only runner (folded raw history discarded, JSON reopened every step) against
full-history raw authority, and the production rebuild with the record
reopened from JSON.  No runtime, profile or learning file is touched.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_sc_learning_policy_stage8_baseline as s8
import test_companion_sc_time_shift_detection as ts
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_schema4 as schema4


HOUR = s8.HOUR
P3 = s8.P3
P4 = 4 * HOUR
P6 = 6 * HOUR
at = s8.at
State = authority.RegimeState
Origin = authority.AuthorityOrigin
Presentation = ts.Presentation

NOTICED = ts.NOTICED
SUSPECTED = ts.SUSPECTED
ADOPTED = ts.ADOPTED
SPARSE = authority.SPARSE_ESTABLISHMENT_REASON
PHASE_MARKER = authority.TIME_SHIFT_AUTHORITY_PHASE_PREFIX
UNTIL_MARKER = authority.TIME_SHIFT_AUTHORITY_UNTIL_PREFIX

# Eight sparse restarts (days 0-7, every 3 h slot) establish 3 h at 80 %:
# grid 00, 03, ... 21.  Moved one hour later the grid is 01, 04, ... 22.
SHIFT = (at(9, 1), at(10, 7), at(11, 16))
# The operator's example: three restarts six hours apart on one day.
SAME_DAY_SHIFT = (at(9, 1), at(9, 7), at(9, 13))
FURTHER_SHIFTED = (at(12, 4), at(13, 10), at(14, 13), at(15, 19), at(16, 22), at(17, 1))
PRE_SHIFT_CONFIDENCE = round(authority.SPARSE_EVIDENCE_CEILING * 0.95, 6)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def sparse_established():
    pipeline = s8.sparse_pipeline()
    step = s8.settled(pipeline)
    s8.assert_sparse_established(step)
    assert step.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert s8.percent(step) == 80
    return pipeline, step


def regime(step):
    return step.decision.incumbent_shadow_regime


def markers(step):
    return {
        item
        for item in regime(step).reason_codes
        if item.startswith(PHASE_MARKER) or item.startswith(UNTIL_MARKER)
    }


def assert_sparse_regime_unchanged(step, original):
    assert regime(step).regime_id == regime(original).regime_id
    assert regime(step).phase_offset == regime(original).phase_offset
    assert regime(step).candidate_period_seconds == P3
    assert ADOPTED not in regime(step).reason_codes
    assert not markers(step)


def assert_sparse_adopted(step, original, *, shift=HOUR, confirmed_at, first_observed_at):
    adopted = regime(step)
    before = regime(original)
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.suspicion_level == 0
    assert adopted.candidate_period_seconds == before.candidate_period_seconds
    assert adopted.origin is Origin.NORMAL
    assert adopted.authority_gate is before.authority_gate is authority.AuthorityGate.NORMAL
    assert SPARSE in adopted.reason_codes and ADOPTED in adopted.reason_codes
    assert adopted.phase_offset == pytest.approx((before.phase_offset + shift) % P3, abs=1.0)
    assert adopted.established_at == confirmed_at
    assert markers(step) == {
        f"{PHASE_MARKER}{before.phase_offset % P3:.6f}",
        f"{UNTIL_MARKER}{first_observed_at:.3f}",
    }
    # The earned confidence is exactly the pre-shift value.
    assert step.decision.combined_confidence == original.decision.combined_confidence
    assert adopted.combined_confidence == original.decision.combined_confidence
    assert s8.percent(step) == s8.percent(original) == 80
    value = step.consumer()
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert "safe_established_sparse_prediction_available" in value.reason_codes
    assert (value.next_expected_restart_at - adopted.phase_offset) % P3 == pytest.approx(0.0, abs=1e-3)


def assert_fixed_point(pipeline, step):
    again = pipeline.evaluate(s8.persisted(step.decision))
    assert again.decision.decision_id == step.decision.decision_id
    assert again.decision == step.decision
    return again


# --------------------------------------------------------------------------
# Notice, suspicion, adoption
# --------------------------------------------------------------------------


def test_one_observation_is_noticed_and_keeps_the_existing_safety_behaviour():
    pipeline, original = sparse_established()
    step = ts.advance(pipeline, original, "shift-0", SHIFT[0])

    assert ts.time_shift_reasons(step) == {NOTICED}
    assert step.decision.state is State.CHANGE_SUSPECTED
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
    assert_sparse_regime_unchanged(step, original)
    value = step.consumer()
    # Unchanged Stage 8.7a treatment: the isolated restart keeps the sparse
    # countdown and lowers the statistic, which withholds the warning.
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible
    assert step.decision.combined_confidence < PRE_SHIFT_CONFIDENCE
    assert s8.percent(step) < 80
    assert not step.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_eligible


@pytest.mark.parametrize("times", [SHIFT, SAME_DAY_SHIFT], ids=["days_apart", "same_day"])
def test_two_observations_show_possible_time_change_with_the_pre_shift_confidence(times):
    pipeline, original = sparse_established()
    _first, step = ts.observe_shift(pipeline, original, times[:2])

    assert ts.time_shift_reasons(step) == {SUSPECTED}
    assert step.decision.suspicion_level == 1
    assert not step.decision.prediction_usable and not step.decision.countdown_safe
    assert_sparse_regime_unchanged(step, original)
    value = step.consumer()
    assert value.presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert not value.countdown_visible
    assert value.cycle_period_seconds == P3
    assert value.scheduled_warning_suppression_key is None
    shown = ts.panel(step)
    assert shown["cycle_text"] == "Possible Time Change"
    assert shown["confidence_label"] == "Cycle Confidence:"
    # D2: the genuine shift does not reduce the earned confidence.
    assert step.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert s8.percent(step) == 80
    for occurrence in (at(11, 0), at(11, 3), at(11, 4)):
        assert not step.consumer(now=occurrence - 300).scheduled_warning_eligible
    assert_fixed_point(pipeline, step)


@pytest.mark.parametrize("times", [SHIFT, SAME_DAY_SHIFT], ids=["days_apart", "same_day"])
def test_third_observation_adopts_the_phase_keeping_period_and_confidence(times):
    pipeline, original = sparse_established()
    step = ts.observe_shift(pipeline, original, times)[-1]

    assert_sparse_adopted(step, original, confirmed_at=times[2], first_observed_at=times[0])
    assert [item.regime_id for item in step.decision.regimes] == [regime(step).regime_id]
    value = step.consumer()
    expected = times[2] + P3
    assert value.next_expected_restart_at == pytest.approx(expected, abs=1.0)
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible and due.scheduled_pre_restart_alert_eligible
    assert due.scheduled_warning_suppression_key.regime_id == regime(step).regime_id
    # No warning at the abandoned grid.
    assert not step.consumer(now=times[2] + 2 * HOUR - 300).scheduled_pre_restart_alert_eligible
    assert_fixed_point(pipeline, step)


def test_six_hour_spacing_on_one_shifted_phase_is_a_time_shift_of_the_established_period():
    """Time Shift Detection does not ask whether 01, 07 and 13 could be 6 h."""

    pipeline, original = sparse_established()
    step = ts.observe_shift(pipeline, original, SAME_DAY_SHIFT)[-1]

    assert regime(step).candidate_period_seconds == P3
    assert ADOPTED in regime(step).reason_codes


# --------------------------------------------------------------------------
# Confidence preservation
# --------------------------------------------------------------------------


def test_the_pre_shift_boundary_excludes_the_first_shifted_observation():
    pipeline, original = sparse_established()
    step = ts.observe_shift(pipeline, original, SHIFT)[-1]
    sparse = authority._sparse_evidence(tuple(step.events), None)

    def support(before_at):
        return authority._sparse_support(
            period=P3,
            phase_offset=regime(original).phase_offset,
            sparse=sparse,
            excluded_event_ids=frozenset(),
            before_at=before_at,
        )

    earned = support(SHIFT[0])
    assert earned.value == pytest.approx(0.95)
    assert earned.off_phase_weight == 0
    # Including the first shifted observation would already erode it.
    assert support(SHIFT[0] + 1).value < earned.value
    assert support(None).value < support(SHIFT[0] + 1).value
    assert authority._time_shift_authority_until(regime(step).reason_codes) == SHIFT[0]


def test_persisted_boundary_never_includes_the_first_observation():
    """A fractional first observation is stored rounded down, not to nearest."""

    pipeline, original = sparse_established()
    times = (SHIFT[0] + 0.7779, SHIFT[1], SHIFT[2])
    step = ts.observe_shift(pipeline, original, times)[-1]

    boundary = authority._time_shift_authority_until(regime(step).reason_codes)
    assert boundary == pytest.approx(SHIFT[0] + 0.777, abs=1e-6)
    assert boundary <= times[0]
    assert step.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    for _ in range(2):
        step = assert_fixed_point(pipeline, step)


def test_confidence_is_carried_until_the_new_phase_supports_it_independently():
    pipeline, original = sparse_established()
    steps = ts.observe_shift(pipeline, original, SHIFT + FURTHER_SHIFTED)

    for step in steps[1:]:
        assert step.decision.combined_confidence >= PRE_SHIFT_CONFIDENCE
        assert s8.percent(step) >= 80
        assert regime(step).candidate_period_seconds == P3
    for step in steps[2:]:
        assert step.decision.state is State.ESTABLISHED
        assert step.consumer().countdown_visible
        assert regime(step).regime_id == regime(steps[2]).regime_id
    carried = [bool(markers(step)) for step in steps[2:]]
    # The carrier lapses once and for all, at the eighth shifted restart (the
    # native statistic reaches 0.95 there), and the regime stands on its own.
    assert carried == [True] * 5 + [False] * 2
    assert ADOPTED in regime(steps[-1]).reason_codes
    assert_fixed_point(pipeline, steps[-1])


def test_shifting_back_to_the_original_phase_keeps_the_earned_confidence():
    pipeline, original = sparse_established()
    shifted = ts.observe_shift(pipeline, original, SHIFT)[-1]
    back = ts.observe_shift(pipeline, shifted, (at(12, 3), at(13, 9), at(14, 12)), name="back")

    assert SUSPECTED in ts.time_shift_reasons(back[1])
    assert back[1].consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert back[1].decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    step = back[-1]
    assert step.decision.state is State.ESTABLISHED
    assert regime(step).phase_offset == pytest.approx(regime(original).phase_offset, abs=1.0)
    assert regime(step).candidate_period_seconds == P3
    assert step.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert step.consumer(now=step.consumer().next_expected_restart_at - 300).scheduled_warning_eligible
    # The earned value is still the one before the first shift.
    assert authority._time_shift_authority_until(regime(step).reason_codes) == SHIFT[0]
    assert_fixed_point(pipeline, step)


# --------------------------------------------------------------------------
# Retained history, rebuilds and persistence
# --------------------------------------------------------------------------


def test_retained_observations_are_corrected_in_a_single_rebuild():
    pipeline, original = sparse_established()
    pipeline.sparse("retained", SHIFT + FURTHER_SHIFTED[:1])
    step = pipeline.evaluate(s8.persisted(original.decision))

    assert regime(step).established_at == SHIFT[2]
    assert regime(step).phase_offset == pytest.approx(HOUR, abs=1.0)
    assert step.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert step.decision.active_suspicion_evidence_ids == ()
    assert_fixed_point(pipeline, step)


@pytest.mark.parametrize("observations", [1, 2, 3, 4])
def test_repeated_rebuilds_of_identical_history_are_a_fixed_point(observations):
    pipeline, original = sparse_established()
    step = ts.observe_shift(pipeline, original, (SHIFT + FURTHER_SHIFTED)[:observations])[-1]
    for _ in range(3):
        step = assert_fixed_point(pipeline, step)


def test_markers_survive_the_serializer_round_trip():
    pipeline, original = sparse_established()
    step = ts.observe_shift(pipeline, original, SHIFT)[-1]
    restored = s8.persisted(step.decision)

    assert restored.incumbent_shadow_regime == regime(step)
    assert markers(step) <= set(restored.incumbent_shadow_regime.reason_codes)


def _production_sparse(times):
    production = ts.Production()
    for index, when in enumerate(times):
        session, event = ts.shaped(f"sparse-{index}", when, production.pipeline.sequence)
        production.pipeline.sequence += 1
        production.pipeline.coverage.append(session.coverage())
        production.pipeline.events.append(event)
    production.rebuild()
    original = production.rebuild()
    s8.assert_sparse_established(original)
    return production, original


def test_production_rebuild_follows_notice_suspect_adopt_and_reopens():
    assert schema4.COMPACT_AUTHORITY_ENABLED
    production, original = _production_sparse(s8.SPARSE_DISCRIMINATING)
    assert original.decision.combined_confidence == PRE_SHIFT_CONFIDENCE

    noticed = production.restart("shift-0", SHIFT[0])
    assert ts.time_shift_reasons(noticed) == {NOTICED}
    assert noticed.consumer().countdown_visible

    suspected = production.restart("shift-1", SHIFT[1])
    assert ts.time_shift_reasons(suspected) == {SUSPECTED}
    assert suspected.consumer().presentation_state is Presentation.POSSIBLE_TIME_CHANGE
    assert suspected.decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert production.rebuild().decision.decision_id == suspected.decision.decision_id

    adopted = production.restart("shift-2", SHIFT[2])
    assert_sparse_adopted(adopted, original, confirmed_at=SHIFT[2], first_observed_at=SHIFT[0])
    stored = [item for item in production.record["regimes"]]
    assert [item["regime_id"] for item in stored] == [regime(adopted).regime_id]
    assert markers(adopted) <= set(stored[0]["reason_codes"])
    state = schema4.persisted_learned_state(production.record)
    assert state.incumbent.regime_id == regime(adopted).regime_id
    assert state.incumbent.phase_offset == regime(adopted).phase_offset
    assert state.superseded == ()

    # Restarting the application (reopen, rebuild) keeps the adoption.
    for _ in range(3):
        again = production.rebuild()
        assert again.decision.decision_id == adopted.decision.decision_id
        assert regime(again) == regime(adopted)


def test_production_adoption_over_folded_pre_shift_evidence_is_stable():
    """The earned restarts are folded into the learned state before the shift."""

    history = s8.aligned_3h(schema4.RAW_EVENT_RETENTION_LIMIT + 10)
    production, original = _production_sparse(history)
    retained = {item["event_id"] for item in production.record["physical_events"]}
    assert len(retained) < len(history)
    start = int(history[-1] // s8.DAY) + 2
    times = (at(start, 1), at(start + 1, 7), at(start + 2, 16))

    for index, when in enumerate(times):
        step = production.restart(f"shift-{index}", when)
    assert step.decision.state is State.ESTABLISHED
    assert regime(step).phase_offset == pytest.approx(HOUR, abs=1.0)
    assert step.decision.combined_confidence == original.decision.combined_confidence
    assert authority._time_shift_authority_until(regime(step).reason_codes) == times[0]
    for _ in range(3):
        again = production.rebuild()
        assert again.decision.decision_id == step.decision.decision_id


# --------------------------------------------------------------------------
# Folded evidence: compact authority against full-history raw authority
# --------------------------------------------------------------------------


def _basic_shift():
    pipeline = s8.sparse_pipeline()
    pipeline.sparse("shift", SHIFT + FURTHER_SHIFTED[:2])
    return pipeline


def _long_shift():
    pipeline = s8.Pipeline()
    pipeline.sparse("aligned", s8.aligned_3h(30))
    pipeline.sparse("shift", (at(40, 1), at(41, 7), at(42, 16), at(43, 4), at(44, 10)))
    return pipeline


def _shift_back():
    pipeline = _basic_shift()
    pipeline.sparse("back", (at(14, 3), at(15, 9), at(16, 12)))
    return pipeline


@pytest.mark.parametrize("capacity", [3, 6, 20])
@pytest.mark.parametrize("factory", [_basic_shift, _long_shift, _shift_back], ids=["basic", "long", "back"])
def test_compact_authority_with_folded_pre_shift_evidence_matches_raw(factory, capacity):
    pipeline = factory()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        compact_authority.assert_same(raw[index], compact, (index, capacity))
        if regime(raw[index]) is not None:
            # Regime identities differ by design (compact identity carries no
            # relationship IDs); everything Time Shift Detection decides agrees.
            left, right = regime(compact), regime(raw[index])
            assert (left.candidate_period_seconds, left.phase_offset, left.origin) == (
                right.candidate_period_seconds,
                right.phase_offset,
                right.origin,
            )
            assert left.established_at == right.established_at
            assert markers(compact) == markers(raw[index])
            assert compact.decision.combined_confidence == raw[index].decision.combined_confidence
    assert ADOPTED in regime(raw[-1]).reason_codes
    assert raw[-1].decision.combined_confidence == PRE_SHIFT_CONFIDENCE
    assert runner.state.watermark.folded_event_count >= len(pipeline.events) - capacity


def test_compact_boundary_inside_a_folded_minute_bin_only_lowers_the_value():
    """A pre-shift restart in the same UTC minute as a later shifted one.

    The folded bin cannot be split at the boundary, so the compact value may
    be lower than the raw one; it is never higher.
    """

    pipeline = s8.sparse_pipeline()
    pipeline.sparse("admin", [at(7, 1)])
    pipeline.sparse("more", [at(8, 9)])
    pipeline.sparse("shift", SHIFT + FURTHER_SHIFTED[:2])
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=3)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        if regime(raw[index]) is not None and ADOPTED in regime(raw[index]).reason_codes:
            assert compact.decision.combined_confidence <= raw[index].decision.combined_confidence
            assert regime(compact).phase_offset == regime(raw[index]).phase_offset


def test_compact_sparse_support_boundary_defaults_to_the_existing_statistic():
    from dzll_launcher import companion_restart_phase2_learned_state as learned

    pipeline = _long_shift()
    runner = compact_authority.CompactOnly(pipeline, capacity=3)
    for batch in compact_authority.batches_of(pipeline, 1):
        runner.step(batch)
    state, ring = runner.state, runner.ring
    plain = learned.compact_sparse_support(state, period=P3, phase_offset=0.0, ring_events=ring)
    unbounded = learned.compact_sparse_support(
        state, period=P3, phase_offset=0.0, ring_events=ring, before_at=None
    )
    bounded = learned.compact_sparse_support(
        state, period=P3, phase_offset=0.0, ring_events=ring, before_at=at(40, 1)
    )
    assert plain == unbounded
    assert plain.off_phase_weight > 0
    assert bounded.off_phase_weight == 0
    assert bounded.value > plain.value


# --------------------------------------------------------------------------
# Unchanged rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,start,batch",
    [(P4, at(9, 10), 1), (P4, at(9, 10), 7), (P6, at(9, 1), 1)],
    ids=["four_hour", "four_hour_batched", "six_hour_on_one_shifted_phase"],
)
def test_continuously_observed_interval_change_keeps_cycle_change_precedence(period, start, batch):
    pipeline, original = sparse_established()
    session = s8.Session("changed", start - 1200, start + 7 * period + 1200)
    pipeline.coverage.append(session.coverage())
    restarts = [
        s8.physical_event(
            500 + index, start + index * period, session, outcome=s8.Outcome.CORROBORATED_OFFLINE_RESTART
        )
        for index in range(8)
    ]
    step = original
    for index in range(0, len(restarts), batch):
        step = ts.inject(pipeline, step, restarts[index : index + batch])
        assert ts._never_time_shifted(step)
    assert regime(step).candidate_period_seconds == period
    assert step.decision.state in {State.NEW_REGIME_ESTABLISHED, State.ESTABLISHED}
    superseded = [
        item for item in step.decision.regimes if item.status is authority.RegimeRecordStatus.SUPERSEDED
    ]
    assert [item.candidate_period_seconds for item in superseded] == [P3]


def test_competing_repeated_phases_still_prevent_adoption():
    pipeline, original = sparse_established()
    times = (at(9, 1), at(9, 2), at(10, 4), at(10, 5), at(11, 7), at(11, 8))
    pipeline.sparse("competing", times)
    step = pipeline.evaluate(s8.persisted(original.decision))

    assert ts.time_shift_reasons(step) == {SUSPECTED}
    assert not step.consumer().countdown_visible
    assert_sparse_regime_unchanged(step, original)


def _as_legacy(decision):
    def strip(item):
        if item is None:
            return None
        return replace(item, reason_codes=tuple(code for code in item.reason_codes if code != SPARSE))

    return replace(
        decision,
        incumbent_shadow_regime=strip(decision.incumbent_shadow_regime),
        selected_shadow_regime=strip(decision.selected_shadow_regime),
        regimes=tuple(strip(item) for item in decision.regimes),
    )


def test_legacy_schema3_origin_regimes_stay_excluded():
    pipeline, original = sparse_established()
    prior = _as_legacy(s8.persisted(original.decision))
    assert not authority._time_shift_eligible(prior.incumbent_shadow_regime)
    for index, when in enumerate(SHIFT + FURTHER_SHIFTED[:1]):
        pipeline.sparse(f"shift-{index}", [when])
        step = pipeline.evaluate(prior)
        assert ts.time_shift_reasons(step) == set()
        assert regime(step).phase_offset == regime(original).phase_offset
        assert not markers(step)
        prior = _as_legacy(s8.persisted(step.decision))


def test_continuity_regimes_never_record_the_sparse_boundary():
    pipeline, original = ts.established()
    steps = ts.observe_shift(pipeline, original, ts.SHIFTED_GRID)
    for step in steps:
        assert authority._time_shift_authority_until(regime(step).reason_codes) is None
        assert step.decision.combined_confidence == original.decision.combined_confidence
    ts.assert_adopted(steps[2], original, shift=HOUR)
