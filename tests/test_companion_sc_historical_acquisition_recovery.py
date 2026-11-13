"""Historical acquisition recovery (operator decisions A1-A3).

A never-established server whose retained restarts before a confirmed time
shift would on their own have passed the unchanged sparse establishment gate
is established as the live runtime would have established it then; the later
restarts are judged by the unchanged established-regime rules, including
Time Shift Detection with its sparse confidence carrier.

The Red Dawn-like fixture mirrors the real record's shape: a 4 h cycle on
the 03/07/11/15/19/23 UTC grid observed sporadically, one unscheduled restart
that happens to sit on the later grid, then the same 4 h cycle one hour later
in UTC (00/04/08/12/16/20) with three unscheduled restarts mixed in.

Synthetic evidence only: the Stage 8 pipeline (serializer round trip), the
compact-only runner against full-history raw authority, and the production
rebuild with JSON reopen.  No runtime, profile or learning file is touched.
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
DAY = s8.DAY
P3 = s8.P3
P4 = 4 * HOUR
P6 = 6 * HOUR
at = s8.at
State = authority.RegimeState
Origin = authority.AuthorityOrigin
Presentation = ts.Presentation
CONFIRMED = s8.Outcome.CONFIRMED_OFFLINE_RESTART

HISTORICAL = authority.HISTORICAL_ACQUISITION_REASON
ADOPTED = authority.TIME_SHIFT_ADOPTED_REASON
SPARSE = authority.SPARSE_ESTABLISHMENT_REASON

# Old grid (03:00 mod 4 h UTC): ten sporadic restarts, four clock slots.
OLD = (
    at(0, 15), at(1, 19), at(2, 23), at(3, 11), at(4, 15),
    at(5, 23), at(6, 19), at(7, 11), at(8, 15), at(9, 19),
)
# An unscheduled restart (no population corroboration) that happens to fall
# five minutes from the later grid, before the schedule moved.
COINCIDENT = at(13, 20, 5)
# New grid (00:00 mod 4 h UTC).
NEW = (at(15, 0), at(15, 16), at(16, 0), at(16, 20), at(19, 0), at(21, 0), at(28, 12))
# Unscheduled restarts after the move, at three unrelated phases.
OFF = (at(15, 17, 45), at(18, 23, 25), at(19, 0, 54))


def red_dawn_like(*, old=OLD, coincident=(COINCIDENT,), new=NEW, off=OFF, intervals=False):
    pipeline = s8.Pipeline()
    pipeline.sparse("old", old)
    if intervals:
        # Three isolated, fully observed old-grid intervals in separate
        # sessions (never two consecutive), as Red Dawn has.
        for day in (10, 11, 12):
            pipeline.observe(f"pair-{day}", [at(day, 11), at(day, 15)])
    pipeline.sparse("coincident", list(coincident), outcome=CONFIRMED)
    pipeline.sparse("new", new)
    pipeline.sparse("off", off, outcome=CONFIRMED)
    return pipeline


def regime(step):
    return step.decision.incumbent_shadow_regime


def recovered(step):
    item = regime(step)
    return item is not None and HISTORICAL in item.reason_codes


def assert_not_recovered(step):
    assert not recovered(step)
    assert HISTORICAL not in step.decision.reason_codes
    assert not any(HISTORICAL in item.reason_codes for item in step.decision.regimes)


def phase_distance(value, target, period=P4):
    return abs(((value - target + period / 2) % period) - period / 2)


def assert_recovered_four_hour(step):
    item = regime(step)
    decision = step.decision
    assert decision.state is State.ESTABLISHED
    assert decision.suspicion_level == 0
    assert decision.prediction_usable and decision.countdown_safe
    assert item.candidate_period_seconds == P4
    assert phase_distance(item.phase_offset, 0.0) <= 60
    assert item.origin is Origin.NORMAL and item.authority_gate is authority.AuthorityGate.NORMAL
    assert {SPARSE, HISTORICAL, ADOPTED} <= set(item.reason_codes)
    assert HISTORICAL in decision.reason_codes
    value = step.consumer()
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert phase_distance(value.next_expected_restart_at, item.phase_offset) < 1e-3
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible and due.scheduled_pre_restart_alert_eligible
    return item


def pre_shift_value(step, boundary, *, excluded=frozenset()):
    sparse = authority._sparse_evidence(tuple(step.events), None)
    return authority._sparse_support(
        period=P4,
        phase_offset=3 * HOUR,
        sparse=sparse,
        excluded_event_ids=excluded,
        before_at=boundary,
    ).value


def settle(pipeline, step):
    again = pipeline.evaluate(s8.persisted(step.decision))
    assert again.decision == step.decision
    return again


# --------------------------------------------------------------------------
# A. Red Dawn-like recovery
# --------------------------------------------------------------------------


def test_preconditions_the_old_grid_alone_establishes_and_the_full_history_does_not_today():
    old_only = s8.settled(red_dawn_like(coincident=(), new=(), off=()))
    s8.assert_sparse_established(old_only, label="Every 4 Hours")
    assert phase_distance(regime(old_only).phase_offset, 3 * HOUR) <= 60

    # Neither grid passes on the full history: the new grid alone is held
    # down by the three unscheduled restarts in its short segment.
    pipeline = red_dawn_like()
    step = pipeline.evaluate()
    sparse = authority._sparse_evidence(tuple(step.events), None)
    for phase in (0.0, 3 * HOUR):
        value = authority._sparse_support(
            period=P4, phase_offset=phase, sparse=sparse, excluded_event_ids=frozenset()
        ).value
        assert round(authority.SPARSE_EVIDENCE_CEILING * value * 100) < 80


def test_retained_history_establishes_the_shifted_cycle_without_another_restart():
    pipeline = red_dawn_like()
    step = pipeline.evaluate()

    item = assert_recovered_four_hour(step)
    # Established at the third confirming restart, from evidence already held.
    assert item.established_at == NEW[1]
    assert max(event.canonical_phase_at for event in step.events) == NEW[-1]
    # A3: the existing formula, over the pre-shift restarts only.
    boundary = authority._time_shift_authority_until(item.reason_codes)
    assert boundary == COINCIDENT
    assert step.decision.combined_confidence == pytest.approx(
        authority.SPARSE_EVIDENCE_CEILING * pre_shift_value(step, boundary), abs=1e-6
    )
    assert s8.percent(step) == 80
    settle(pipeline, step)


def test_isolated_intervals_count_through_the_existing_combined_formula():
    pipeline = red_dawn_like(intervals=True)
    step = pipeline.evaluate()

    item = assert_recovered_four_hour(step)
    high = item.high_authority
    assert high > 0.8  # three isolated intervals, no streak: H1 only
    boundary = authority._time_shift_authority_until(item.reason_codes)
    carrier = next(c for c in step.decision.candidates if c.regime_id == item.regime_id)
    expected = high + (1 - high) * min(
        authority.SPARSE_EVIDENCE_CEILING,
        authority.SPARSE_EVIDENCE_CEILING * carrier.aligned_normal_confidence,
    )
    assert step.decision.combined_confidence == pytest.approx(expected, abs=1e-6)
    assert carrier.aligned_normal_confidence == pytest.approx(0.95)
    assert boundary == COINCIDENT
    assert s8.percent(step) == 95  # UI cap
    settle(pipeline, step)


def test_the_coincidental_pre_shift_restart_is_not_load_bearing():
    with_it = red_dawn_like().evaluate()
    without_it = red_dawn_like(coincident=()).evaluate()

    for step in (with_it, without_it):
        assert_recovered_four_hour(step)
    assert regime(with_it).phase_offset == pytest.approx(regime(without_it).phase_offset, abs=60)
    assert with_it.decision.combined_confidence == without_it.decision.combined_confidence
    # It never contributes confidence: the boundary excludes it.
    assert authority._time_shift_authority_until(regime(with_it).reason_codes) == COINCIDENT


def test_a_coincidental_restart_cannot_decide_the_longer_period_refutation(monkeypatch):
    # The confirming group alone: the coincidental 20:05 plus 00:00 and
    # 16:00.  Only the 20:05 restart separates it from an 8 h schedule.
    step = red_dawn_like(new=NEW[:2], off=()).evaluate()
    assert_not_recovered(step)
    assert step.decision.state is not State.ESTABLISHED
    assert _recovered_without_a2(monkeypatch, lambda: red_dawn_like(new=NEW[:2], off=()))

    # A genuine 20:00 restart on the new grid refutes 8 h on its own.
    step = red_dawn_like(new=NEW[:4], off=()).evaluate()
    assert_recovered_four_hour(step)


@pytest.mark.parametrize("capacity", [3, 6, 20])
def test_incremental_learning_establishes_before_the_shift_and_follows_it(capacity):
    """Live, restart-by-restart learning needs no recovery: the old grid
    establishes on its own and Time Shift Detection follows the move.  Raw
    and compact agree throughout."""

    pipeline = red_dawn_like()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        # Pre-establishment learning percentages differ between the raw and
        # compact runners for this fixture without historical recovery too
        # (outside this change); decisions are compared at every step.
        assert compact_authority.decision_summary(compact.decision) == compact_authority.decision_summary(
            raw[index].decision
        ), (index, capacity)
        assert not recovered(compact) and not recovered(raw[index])
        if regime(raw[index]) is not None:
            compact_authority.assert_same(raw[index], compact, (index, capacity))
    final = regime(raw[-1])
    assert final.candidate_period_seconds == P4 and ADOPTED in final.reason_codes
    assert phase_distance(final.phase_offset, 0.0) <= 60


@pytest.mark.parametrize("capacity", [11, 12, 20])
def test_recovery_over_folded_pre_shift_evidence_matches_raw(capacity):
    """First evaluated after the shift (history learned before the current
    rules): the pre-shift restarts are folded into the learned state, the
    shifted ones are still in the ring, and the boundary lies inside the
    folded history's time span."""

    pipeline = red_dawn_like()
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    batches = [ordered, []]
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        compact_authority.assert_same(raw[index], compact, (index, capacity))
        assert recovered(compact) and recovered(raw[index])
        assert compact.decision.combined_confidence == raw[index].decision.combined_confidence
        assert regime(compact).phase_offset == regime(raw[index]).phase_offset
        assert regime(compact).established_at == regime(raw[index]).established_at
    assert runner.state.watermark.folded_event_count == len(ordered) - capacity
    ring = {item.canonical_phase_at for item in runner.ring}
    assert OLD[0] not in ring and set(NEW) <= ring
    if capacity == 11:
        assert not ring & set(OLD)  # every pre-shift restart is folded


def test_production_rebuild_recovers_once_and_reopens_stably():
    production = ts.Production(red_dawn_like())
    first = production.rebuild()
    assert_recovered_four_hour(first)
    state = schema4.persisted_learned_state(production.record)
    assert state.incumbent.regime_id == regime(first).regime_id
    assert [item["regime_id"] for item in production.record["regimes"]] == [regime(first).regime_id]
    for _ in range(3):
        again = production.rebuild()
        assert again.decision.decision_id == first.decision.decision_id


def test_a_persisted_recovery_is_not_recomputed(monkeypatch):
    pipeline = red_dawn_like()
    step = pipeline.evaluate()
    assert recovered(step)

    def fail(**_kwargs):
        raise AssertionError("historical recovery must not run for an established server")

    monkeypatch.setattr(authority, "_historical_shift_acquisition", fail)
    again = pipeline.evaluate(s8.persisted(step.decision))
    assert again.decision == step.decision
    # Later restarts are ordinary incumbent evidence.
    pipeline.sparse("later", [at(29, 4), at(30, 8, 30)])
    later = pipeline.evaluate(s8.persisted(again.decision))
    assert regime(later).regime_id == regime(step).regime_id


def test_after_recovery_off_grid_restarts_are_ordinary_suspicion_not_disestablishment():
    pipeline = red_dawn_like(off=())
    step = pipeline.evaluate()
    assert recovered(step)
    pipeline.sparse("admin", [at(29, 13, 47)], outcome=CONFIRMED)
    suspected = pipeline.evaluate(s8.persisted(step.decision))
    assert regime(suspected).regime_id == regime(step).regime_id
    assert suspected.decision.state is State.CHANGE_SUSPECTED
    pipeline.sparse("aligned", [at(30, 8)])
    back = pipeline.evaluate(s8.persisted(suspected.decision))
    assert back.decision.state is State.ESTABLISHED
    assert back.decision.combined_confidence == step.decision.combined_confidence


# --------------------------------------------------------------------------
# B. False-establishment protection
# --------------------------------------------------------------------------


def test_random_administrative_restarts_establish_nothing():
    pipeline = s8.Pipeline()
    pipeline.sparse("admin", s8.admin_restarts(18, range(0, 36, 2)), outcome=CONFIRMED)
    step = pipeline.evaluate()
    assert_not_recovered(step)
    assert step.decision.state is not State.ESTABLISHED


def test_insufficient_pre_shift_history_is_not_recovered():
    # Five old-grid restarts cannot pass the sparse gate; nothing is invented.
    step = red_dawn_like(old=OLD[:5]).evaluate()
    assert_not_recovered(step)
    assert step.decision.state is not State.ESTABLISHED


def test_one_off_phase_matches_without_a_confirmed_group_are_not_recovered():
    singles = (at(15, 1), at(16, 2, 30), at(17, 23, 15), at(18, 17, 40), at(20, 9, 20))
    step = red_dawn_like(coincident=(), new=singles, off=()).evaluate()
    assert_not_recovered(step)


def test_a_noisy_suffix_that_fails_confirmation_is_not_recovered():
    noisy = (at(15, 0), at(16, 16), at(17, 2, 30), at(18, 13, 10), at(19, 21, 40))
    step = red_dawn_like(coincident=(), new=noisy, off=()).evaluate()
    assert_not_recovered(step)


def test_competing_repeated_phases_prevent_recovery():
    competing = (at(15, 0), at(15, 1), at(16, 4), at(16, 5), at(17, 8), at(17, 9))
    step = red_dawn_like(coincident=(), new=competing, off=()).evaluate()
    assert_not_recovered(step)


def test_clock_locked_shifted_observations_are_not_recovered(monkeypatch):
    # Every shifted restart at 00:00 or 12:00 UTC: also a 12 h schedule.
    locked = (at(15, 0), at(16, 12), at(17, 0), at(18, 12), at(19, 0), at(20, 12))
    step = red_dawn_like(coincident=(), new=locked, off=()).evaluate()
    assert_not_recovered(step)
    assert _recovered_without_a2(
        monkeypatch, lambda: red_dawn_like(coincident=(), new=locked, off=())
    )


def test_an_unsafe_final_state_is_not_recovered():
    # Recovered history whose latest restart is an unexplained repeat at a
    # second new phase: the replay ends suspected, so acquisition continues.
    extra = (at(29, 2), at(30, 6))
    step = red_dawn_like(off=extra).evaluate()
    assert_not_recovered(step)


# --------------------------------------------------------------------------
# C. Genuine cycle changes
# --------------------------------------------------------------------------


def _three_hour_history(days=16):
    # Heavy enough that the old 3 h phase is still a candidate afterwards.
    return tuple(at(day, (3 * day) % 24) for day in range(days))


def _old_three_hour_candidate(step):
    return [
        item
        for item in step.decision.candidates
        if item.candidate_period_seconds == P3 and phase_distance(item.phase_offset, 0.0, P3) < 600
    ]


def _recovered_without_a2(monkeypatch, factory):
    with monkeypatch.context() as patch:
        patch.setattr(
            authority, "_historical_shift_group_refutes_longer_periods", lambda *a, **k: True
        )
        return recovered(factory().evaluate())


def test_three_to_four_hour_change_is_not_recovered_as_a_three_hour_shift():
    def factory():
        pipeline = s8.Pipeline()
        pipeline.sparse("three", _three_hour_history())
        pipeline.sparse("four", tuple(at(18, 1, 30) + index * 7 * P4 for index in range(9)))
        return pipeline

    step = factory().evaluate()
    assert _old_three_hour_candidate(step)
    assert_not_recovered(step)
    assert not (regime(step) is not None and regime(step).candidate_period_seconds == P3)


def test_clock_locked_three_to_six_hour_change_is_not_recovered_as_a_shift(monkeypatch):
    def factory():
        pipeline = s8.Pipeline()
        pipeline.sparse("three", _three_hour_history())
        # A 6 h schedule at 01/07/13/19 UTC seen once a day: every restart is
        # also "3 h shifted by one hour", and none of them can refute 6 h.
        pipeline.sparse("six", tuple(at(day, (1, 7, 13, 19)[day % 4]) for day in range(18, 26)))
        return pipeline

    step = factory().evaluate()
    assert _old_three_hour_candidate(step)
    assert_not_recovered(step)
    # A2 is the safeguard that decides it.
    assert _recovered_without_a2(monkeypatch, factory)


def test_continuous_new_interval_keeps_ordinary_acquisition_precedence():
    pipeline = s8.Pipeline()
    pipeline.sparse("three", _three_hour_history())
    pipeline.observe("four", [at(12, 10) + index * P4 for index in range(5)])
    step = pipeline.evaluate()
    assert_not_recovered(step)
    assert step.decision.state is State.ESTABLISHED
    assert regime(step).candidate_period_seconds == P4


def test_established_regimes_are_never_reinterpreted_as_historical(monkeypatch):
    pipeline, original = ts.established()

    def fail(**_kwargs):
        raise AssertionError("historical recovery is acquisition only")

    monkeypatch.setattr(authority, "_historical_shift_acquisition", fail)
    steps = ts.observe_shift(pipeline, original, ts.SHIFTED_GRID)
    assert ADOPTED in regime(steps[2]).reason_codes
    assert all(not recovered(step) for step in steps)


# --------------------------------------------------------------------------
# D. Coverage and evidence safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ts.INELIGIBLE_SHAPES))
def test_ineligible_episodes_cannot_be_shift_observations(name):
    pipeline = red_dawn_like(coincident=(), new=(), off=())
    for index, when in enumerate(NEW):
        session, event = ts.shaped(f"bad-{index}", when, pipeline.sequence, **ts.INELIGIBLE_SHAPES[name])
        pipeline.sequence += 1
        pipeline.coverage.append(session.coverage())
        pipeline.events.append(event)
    step = pipeline.evaluate()
    assert_not_recovered(step)


def test_duplicate_deliveries_count_once():
    single = red_dawn_like()
    double = single.copy()
    double.events.extend(single.events)
    assert double.evaluate().decision == single.evaluate().decision


def test_unmonitored_time_is_neutral():
    pipeline = red_dawn_like()
    step = pipeline.evaluate()
    later = pipeline.evaluate(now=at(80, 0))
    assert later.decision.decision_id == step.decision.decision_id


def test_empty_population_offline_restarts_remain_eligible():
    # Every shifted restart observed only as offline-then-online, with no
    # player or queue corroboration (weight 0.8 under the existing rules).
    pipeline = red_dawn_like(coincident=(), off=())
    pipeline.events[:] = [
        replace(event, outcome=CONFIRMED) if event.canonical_phase_at >= NEW[0] else event
        for event in pipeline.events
    ]
    step = pipeline.evaluate()
    assert_recovered_four_hour(step)
