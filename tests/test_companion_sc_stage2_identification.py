"""Verified acquisition, Stage 2: faster independent-evidence identification.

The audited C1-C6 calibration, identification only: independent A1
occurrences identify a candidate from about four strong observations (five
with weaker weights or one isolated anomaly, six at 2 h), but only a fully
witnessed interval establishes it (Stage 1).  Regimes established before
verified acquisition keep the Stage 8.5 statistic and gates.

Synthetic data only; no runtime, profile, learning file or network.
"""

from __future__ import annotations

import contextlib

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_sc_historical_acquisition_recovery as har
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P2, P3, P4, P6, P8, P12 = (hours * HOUR for hours in (2, 3, 4, 6, 8, 12))
at = s8.at
State = authority.RegimeState
Outcome = s8.Outcome
OO = Outcome.CORROBORATED_OFFLINE_RESTART
CONFIRMED = Outcome.CONFIRMED_OFFLINE_RESTART
QV = Outcome.STRONG_QUERY_VISIBLE_RESTART
PROBABLE = Outcome.PROBABLE_QUERY_VISIBLE_RESTART
AWAITING = authority.AWAITING_VERIFICATION_REASON
VERIFIED = authority.VERIFIED_ESTABLISHMENT_REASON
SLOTS = s8.SPARSE_DISCRIMINATING
gm = s8.genuine_miss


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def identified(step):
    """(period hours or None, established?) as the learner presents it."""

    decision = step.decision
    regime = decision.selected_shadow_regime
    if decision.state is State.ESTABLISHED:
        return regime.candidate_period_seconds // HOUR, True
    if decision.state is State.LIKELY and AWAITING in decision.reason_codes:
        return regime.candidate_period_seconds // HOUR, False
    return None, False


def sparse(times, *, outcome=OO, ledger=(), extra=None):
    pipeline = s8.sparse_pipeline(tuple(times), outcome=outcome)
    if extra is not None:
        extra(pipeline)
    first = pipeline.evaluate(ledger=ledger)
    return pipeline.evaluate(s8.persisted(first.decision), ledger=ledger)


def period_slots(period, count, base_hour=1):
    """Discriminating occurrences: steps of ``period`` on separate days."""

    return [at(index * 2, 0) + base_hour * HOUR + (index % 4) * period + (index // 4) * period * 5 for index in range(count)]


def divisor_ledger(period, start_day=40, base_hour=1):
    ledger = []
    for divisor in [item for item in scoring.CANDIDATE_PERIODS if item < period and period % item == 0]:
        for offset in range(2):
            ledger.append(gm(divisor, at(start_day + offset, 0) + base_hour * HOUR + divisor, key=f"d{divisor}-{offset}"))
    return tuple(ledger)


@contextlib.contextmanager
def pre_verification_build(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(authority, "_witnessed_interval_verifies", lambda **_kwargs: True)
        patch.setattr(authority, "VERIFIED_ESTABLISHMENT_REASON", "regime_remains_reversible")
        patch.setattr(authority, "_identification_calibrated", lambda prior: False)
        yield


# --------------------------------------------------------------------------
# Thresholds, weights and the curve (identification independently of
# establishment)
# --------------------------------------------------------------------------


def test_identification_constants_and_curve():
    assert authority.SPARSE_IDENTIFICATION_OCCURRENCES == 4
    assert authority.sparse_identification_occurrences(P2) == 6
    for period in (P3, P4, P6, P8, P12):
        assert authority.sparse_identification_occurrences(period) == 4
    values = [
        authority.sparse_identification_magnitudes(
            effective=count, band=1.0, off_phase_weight=0.0, consistency=1.0, period=P3
        )[0]
        for count in (1, 2, 3, 4, 8)
    ]
    assert values == [0.25, 0.5, 0.75, 0.95, 0.95]
    # 2 h saturates at six occurrences.
    assert authority.sparse_identification_magnitudes(
        effective=4, band=1.0, off_phase_weight=0.0, consistency=1.0, period=P2
    )[0] < 0.95
    assert authority.sparse_identification_magnitudes(
        effective=6, band=1.0, off_phase_weight=0.0, consistency=1.0, period=P2
    )[0] == 0.95


def test_phase_dispersion_costs_evidence_instead_of_a_ceiling():
    def value(count, band):
        return authority.sparse_identification_magnitudes(
            effective=count, band=band, off_phase_weight=0.0, consistency=1.0, period=P3
        )[0]

    assert value(4, 0.9) < 0.95 and value(5, 0.9) == 0.95
    # Below the identification band the magnitude also falls (never 80 %).
    assert value(20, 0.75) == pytest.approx(0.95 * 0.75)


def test_single_anomaly_allowance_and_variant_b_count():
    within = authority.sparse_identification_magnitudes(
        effective=5, band=1.0, off_phase_weight=1.0, consistency=5 / 6, period=P3
    )
    beyond = authority.sparse_identification_magnitudes(
        effective=6, band=1.0, off_phase_weight=2.0, consistency=6 / 8, period=P3
    )
    assert within[2] and within[0] == 0.95
    assert not beyond[2] and beyond[0] < 0.95
    assert not authority.sparse_identification_count_met(effective=5, off_phase_weight=1.0, period=P3)
    assert authority.sparse_identification_count_met(effective=6, off_phase_weight=1.0, period=P3)
    assert authority.sparse_identification_count_met(effective=4, off_phase_weight=0.0, period=P3)
    assert not authority.sparse_identification_count_met(effective=5, off_phase_weight=0.0, period=P2)


# --------------------------------------------------------------------------
# 1-4, 13. Evidence counts and weights; awaiting, not established
# --------------------------------------------------------------------------


@pytest.mark.parametrize("period", [P3, P4])
def test_four_clean_corroborated_observations_identify_and_await(period):
    three = sparse(period_slots(period, 3))
    four = sparse(period_slots(period, 4))
    five = sparse(period_slots(period, 5))
    assert identified(three) == (None, False)
    assert identified(four) == (period // HOUR, False)
    assert identified(five) == (period // HOUR, False)
    for step in (four, five):
        assert s8.percent(step) == 79
        assert not step.consumer().countdown_visible
        assert not step.consumer().scheduled_warning_eligible


@pytest.mark.parametrize(
    "outcome,needed",
    [(CONFIRMED, 5), (QV, 5), (PROBABLE, 8)],
    ids=["confirmed_oo", "strong_qv", "probable_qv"],
)
def test_weaker_evidence_needs_more_occurrences(outcome, needed):
    slots = list(SLOTS) + [at(7, 9), at(8, 12)]
    assert identified(sparse(slots[: needed - 1], outcome=outcome)) == (None, False)
    assert identified(sparse(slots[:needed], outcome=outcome)) == (3, False)


def test_mixed_weights_add_up_to_the_requirement():
    def run(outcomes):
        pipeline = s8.Pipeline()
        for index, (when, outcome) in enumerate(zip(SLOTS, outcomes)):
            pipeline.observe(f"m{index}", [when], outcome=outcome)
        return s8.settled(pipeline)

    assert identified(run([OO, OO, OO, CONFIRMED])) == (None, False)  # 3.8
    assert identified(run([OO, OO, OO, CONFIRMED, CONFIRMED])) == (3, False)  # 4.6


def test_two_hour_candidates_need_six_occurrences():
    slots = [at(index * 2, 1) + (index % 4) * P2 + (index // 4) * 10 * HOUR for index in range(7)]
    assert identified(sparse(slots[:4])) == (None, False)
    assert identified(sparse(slots[:5])) == (None, False)
    assert identified(sparse(slots[:6])) == (2, False)


# --------------------------------------------------------------------------
# 5-8. Harmonic, daily and administrative-restart safeguards
# --------------------------------------------------------------------------


@pytest.mark.parametrize("period", [P6, P8, P12])
def test_genuine_long_cycles_need_their_divisors_resolved(period):
    slots = period_slots(period, 5)
    assert identified(sparse(slots)) == (None, False)
    assert identified(sparse(slots, ledger=divisor_ledger(period))) == (period // HOUR, False)


@pytest.mark.parametrize("period", [P8, P12])
def test_daily_same_clock_observations_never_identify_eight_or_twelve_hours(period):
    days = [at(day * 2, 20) for day in range(8)]
    ledger = divisor_ledger(period, base_hour=20)
    for count in (4, 8):
        step = sparse(days[:count], ledger=ledger)
        assert identified(step) == (None, False)
        assert authority.SPARSE_PERIOD_AMBIGUOUS_REASON in step.candidate(period).reason_codes


def test_six_hour_cycle_plus_one_midpoint_admin_restart_is_never_three_hours():
    six = [at(0, 0), at(1, 6), at(2, 12), at(3, 18), at(4, 0), at(5, 6), at(6, 12)]
    for count in (3, 7):
        step = sparse(six[:count] + [at(3, 3)])
        assert identified(step)[0] != 3
    # Even when the admin restart starts a witnessed 3 h interval.
    pipeline = s8.sparse_pipeline(six[:3])
    pipeline.observe("admin-interval", [at(3, 3), at(3, 6)])
    assert identified(s8.settled(pipeline))[0] != 3
    # Two coincidences at 3 h midpoints are accepted as evidence.
    assert identified(sparse(six + [at(3, 3), at(7, 9)]))[0] == 3


def test_witnessed_interval_endpoints_join_the_robust_refutation():
    # Three discriminating sparse restarts (06/09/12: a 2-1 split at 6 h,
    # not robust) plus a witnessed 3 h interval 15:00 -> 18:00: with the
    # endpoints the 6 h classes are 3-2 (robust), and the candidate is
    # identified (four sparse occurrences) and verified at once.
    pipeline = s8.sparse_pipeline(SLOTS[:4])
    pipeline.observe("witnessed", [at(9, 15), at(9, 18)])
    step = s8.settled(pipeline)
    assert identified(step) == (3, True)
    support = authority._sparse_support(
        period=P3,
        phase_offset=step.decision.incumbent_shadow_regime.phase_offset,
        sparse=authority._sparse_evidence(tuple(step.events), None),
        excluded_event_ids=frozenset(item.event_id for item in step.events if item.canonical_phase_at in (at(9, 15), at(9, 18))),
    )
    assert len(support.aligned) == 4 and len(support.refutation) == 6


def test_true_two_hour_cycle_cannot_be_established_as_four_hours():
    # Sampled only at one 4 h parity, it may await verification as 4 h ...
    # 01/05/09/13 on separate days: 4 h-aligned, odd hours (also 2 h-aligned).
    four_parity = [at(0, 1), at(2, 5), at(4, 9), at(6, 13)]
    awaiting = sparse(four_parity)
    assert identified(awaiting) == (4, False)
    # ... but the monitored 4 h span contains its 2 h restart: never a
    # witnessed 4 h interval, so it never establishes.
    pipeline = s8.sparse_pipeline(four_parity)
    pipeline.observe("watched", [at(20, 1), at(20, 3), at(20, 5)])
    step = s8.settled(pipeline)
    assert not any(
        item.candidate_period_seconds == P4
        and item.relationship_kind is authority.RelationshipKind.DIRECT
        and authority._full_interval_quality(item, {e.event_id: e for e in step.events}) > 0
        for item in step.relationships
    )
    assert identified(step) != (4, True)


# --------------------------------------------------------------------------
# 9-11. Anomalies, followers and phase dispersion
# --------------------------------------------------------------------------


def test_isolated_admin_restart_costs_one_further_occurrence():
    admin = at(2, 13, 37)
    assert identified(sparse(list(SLOTS[:5]) + [admin])) == (None, False)
    assert identified(sparse(list(SLOTS[:6]) + [admin])) == (3, False)
    # Two coherent off-phase restarts exceed the allowance.
    assert identified(sparse(list(SLOTS[:8]) + [admin, at(4, 13, 37)])) == (None, False)


def test_correlated_followers_never_count_as_independent_observations():
    clean = sparse(SLOTS[:3])
    with_followers = sparse([when + offset for when in SLOTS[:3] for offset in (0, 150, 300)])
    assert identified(clean) == identified(with_followers) == (None, False)
    burst = s8.sparse_pipeline(SLOTS[:5])
    burst.observe("burst", [at(2, 13, 40) + index * 150 for index in range(20)])
    single = s8.sparse_pipeline(SLOTS[:5])
    single.sparse("admin", [at(2, 13, 40)])
    assert identified(s8.settled(burst)) == identified(s8.settled(single)) == (None, False)


def test_phase_dispersion_at_the_tolerance_edge():
    tolerance = scoring.candidate_phase_tolerance(P3) + 3  # plus phase uncertainty
    inside = sparse(list(SLOTS[:4]) + [at(5, 21) + tolerance - 5])
    outside = sparse(list(SLOTS[:4]) + [at(5, 21) + tolerance + 5])
    assert identified(inside) == (3, False)
    # Outside the tolerance it is an isolated off-phase anomaly (net three).
    assert identified(outside) == (None, False)
    assert authority.SPARSE_PROPORTIONAL_ANOMALY_REASON in outside.candidate(P3, 0.0).reason_codes
    # Variant B: it costs one further aligned occurrence (six aligned).
    assert identified(sparse(list(SLOTS[:5]) + [at(5, 21) + tolerance + 5, at(6, 3)])) == (3, False)


def test_weak_phase_needs_five_and_poor_phase_never_identifies():
    def jitter(count, offset):
        signs = (0, 1, -1)
        return [when + offset * signs[0 if index == 0 else 1 + index % 2] for index, when in enumerate(SLOTS[:count])]

    assert identified(sparse(jitter(4, 5 * 60))) == (None, False)
    assert identified(sparse(jitter(5, 5 * 60))) == (3, False)
    assert identified(sparse(jitter(8, 9 * 60))) == (None, False)


# --------------------------------------------------------------------------
# 12-14. Stage 1 verification compatibility
# --------------------------------------------------------------------------


def test_interval_witnessed_before_identification_establishes_at_the_fourth():
    pipeline = s8.Pipeline()
    pipeline.observe("early", [at(-3, 3), at(-3, 6)])
    prior = None
    states = []
    for index, when in enumerate(SLOTS[:4]):
        pipeline.sparse(f"inc-{index}", [when])
        step = pipeline.evaluate(prior)
        prior = s8.persisted(step.decision)
        states.append(identified(step))
    assert states[:3] == [(None, False)] * 3
    assert states[3] == (3, True)
    assert s8.percent(step) == 90
    assert VERIFIED in step.decision.incumbent_shadow_regime.reason_codes


def test_invalid_witnessed_intervals_never_enable_establishment():
    pipeline = s8.sparse_pipeline(SLOTS[:4])
    pipeline.observe("before-gap", [at(9, 9)], tail=3600)
    pipeline.observe("after-gap", [at(9, 12)], lead=3600)
    assert identified(s8.settled(pipeline)) == (3, False)
    pipeline = s8.sparse_pipeline(SLOTS[:4])
    s8.observe_mixed(pipeline, "drain-end", [(at(9, 9), OO), (at(9, 12), Outcome.AMBIGUOUS_DRAIN)])
    assert identified(s8.settled(pipeline)) == (3, False)


def test_qv_verification_keeps_no_double_counting():
    pipeline = s8.sparse_pipeline(list(SLOTS[:5]), outcome=QV)
    awaiting = s8.settled(pipeline)
    assert identified(awaiting) == (3, False)
    s8.verify(pipeline, (at(9, 3), at(9, 6)))
    verified = pipeline.evaluate(s8.persisted(awaiting.decision))
    assert identified(verified) == (3, True)
    assert s8.percent(verified) == 80


# --------------------------------------------------------------------------
# 15, 17, 18. Existing users, real shapes and unchanged incumbents
# --------------------------------------------------------------------------


def test_existing_regime_keeps_the_stage_8_5_statistic(monkeypatch):
    pipeline = s8.sparse_pipeline()
    with pre_verification_build(monkeypatch):
        legacy = pipeline.evaluate()
        legacy = pipeline.evaluate(s8.persisted(legacy.decision))
    assert legacy.decision.state is State.ESTABLISHED
    assert VERIFIED not in legacy.decision.incumbent_shadow_regime.reason_codes
    pipeline.sparse("admin", [at(7, 17, 20)])
    step = pipeline.evaluate(s8.persisted(legacy.decision))
    candidate = step.candidate(P3, 0.0)
    # Stage 8.5: one anomaly against eight restarts scales by the ratio.
    expected = round(
        scoring._event_count_cap(7) * min(1.0, (8 / 9) / authority.SPARSE_CONSISTENCY_TOLERANCE), 6
    )
    assert candidate.aligned_normal_confidence == expected
    assert not authority._identification_calibrated(s8.persisted(step.decision))
    # The same regime under verified acquisition: the identification
    # statistic absorbs the isolated anomaly.
    assert authority._identification_calibrated(None)


def test_red_dawn_like_history_recovers_through_verified_period():
    step = har.settle(har.red_dawn_like(), har.red_dawn_like().evaluate())
    har.assert_recovered_four_hour(step)
    unverified = har.red_dawn_like(verified=False).evaluate()
    assert identified(unverified) == (4, False)


def test_continuous_h3_and_two_interval_establishment_are_unchanged():
    assert s8.percent(s8.three_hour_chain(3).evaluate()) == 80
    assert s8.percent(s8.three_hour_chain(4).evaluate()) == 95


def test_established_incumbent_suspicion_rules_are_unchanged():
    pipeline = s8.three_hour_chain(5)
    original = s8.settled(pipeline)
    pipeline.sparse("admin", [at(3, 13, 37)])
    step = pipeline.evaluate(s8.persisted(original.decision))
    assert step.decision.state is State.CHANGE_SUSPECTED and step.decision.suspicion_level == 1
    assert step.consumer().countdown_visible
    pipeline.observe("four", [at(5, 1), at(5, 5), at(5, 9)])
    provisional = pipeline.evaluate(s8.persisted(step.decision))
    assert provisional.decision.state is State.TRANSITION_CONFIRMED
    assert not provisional.consumer().countdown_visible


# --------------------------------------------------------------------------
# 16. Raw/compact parity across small rings
# --------------------------------------------------------------------------


def _parity_histories():
    with_interval = s8.sparse_pipeline(SLOTS[:4])
    with_interval.observe("witnessed", [at(8, 9), at(8, 12)])
    anomaly = s8.sparse_pipeline(SLOTS[:6])
    anomaly.sparse("admin", [at(6, 17, 20)])
    burst = s8.sparse_pipeline(SLOTS[:5])
    burst.observe("burst", [at(6, 13, 40) + index * 150 for index in range(8)])
    two_hour = s8.sparse_pipeline([at(index * 2, 1) + (index % 4) * P2 + (index // 4) * 10 * HOUR for index in range(6)])
    qv_first = s8.Pipeline()
    s8.verify(qv_first)
    qv_first.sparse("sparse", SLOTS[:5], outcome=QV)
    return {
        "interval_after": with_interval,
        "isolated_anomaly": anomaly,
        "burst": burst,
        "two_hour": two_hour,
        "qv_interval_first": qv_first,
    }


@pytest.mark.parametrize("name", sorted(_parity_histories()))
@pytest.mark.parametrize("capacity", [2, 3, 5])
def test_compact_identification_matches_raw(name, capacity):
    pipeline = _parity_histories()[name]
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert compact_authority.decision_summary(compact.decision) == (
            compact_authority.decision_summary(raw[index].decision)
        ), (name, capacity, index)
        assert identified(compact) == identified(raw[index]), (name, capacity, index)


def test_compact_robust_refutation_is_never_looser_than_raw():
    # Folded minute ranges are in a cluster unless they certainly are not.
    exact = [(21600.0, 21600.0, 1), (32400.0, 32400.0, 1), (43200.0, 43200.0, 1), (54000.0, 54000.0, 1)]
    assert learned.sparse_robust_refutation(exact, P3)
    base = [(0.0, 0.0, 1), (43200.0, 43200.0, 1), (10800.0, 10800.0, 1)]
    # A point 950 s from the 6 h class (tolerance 900 s) is outside it ...
    assert learned.sparse_robust_refutation(base + [(21600.0 + 950.0,) * 2 + (1,)], P3)
    # ... one 899 s away is inside (three of four in one class) ...
    assert not learned.sparse_robust_refutation(base + [(21600.0 + 899.0,) * 2 + (1,)], P3)
    # ... and a folded range that may reach inside counts as inside.
    assert not learned.sparse_robust_refutation(base + [(21600.0 + 890.0, 21600.0 + 950.0, 1)], P3)
    assert not learned.sparse_robust_refutation(exact[:1], P3)
    assert not learned.sparse_robust_refutation([(0.0, 0.0, 2), (10800.0, 10800.0, 1)], P3)
