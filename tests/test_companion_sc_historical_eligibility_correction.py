"""Historical high-eligibility correction.

Schema-4 interval relationships are immutable by identity, so a relationship
persisted before a rule existed keeps its original ``high_authority_eligible``
flag.  Derived evaluation (folding and authority, raw or compact) now judges
stored high eligibility by the current event-based rules; the persisted
record is never rewritten.

"Legacy" records are simulated by setting the stored flag of a relationship
that the current rules reject (an unobservable service interruption between
its endpoints) back to eligible, as it was stored before the rule.

Synthetic evidence only; no runtime, profile or learning file is touched.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_sc_historical_acquisition_recovery as har
import test_companion_sc_learning_policy_stage8_baseline as s8
import test_companion_sc_time_shift_detection as ts
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_schema4 as schema4


HOUR = s8.HOUR
P3 = s8.P3
P4 = 4 * HOUR
at = s8.at
Outcome = s8.Outcome
State = s8.State
WITHDRAWN = continuity.HIGH_ELIGIBILITY_WITHDRAWN_REASON
UNOBSERVABLE = "high_ineligible_intervening_unobservable_episode"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def session_with(pipeline, name, restarts, *, interruptions=(), shape="service_interruption"):
    """One continuous session holding restarts and ineligible episodes."""

    times = sorted([*restarts, *interruptions])
    session = s8.Session(name, min(times) - 1200, max(times) + 1200)
    pipeline.coverage.append(session.coverage())
    for when in times:
        event = s8.physical_event(
            pipeline.sequence, when, session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART
        )
        if when in interruptions:
            event = replace(event, **ts.INELIGIBLE_SHAPES[shape])
        pipeline.events.append(event)
        pipeline.sequence += 1


def interrupted_chain(pipeline=None, *, day=0):
    """09-12-15-18 in one session; a service interruption inside 09->12.

    Under the current rules 09->12 is high-ineligible and 12->15->18 is a
    two-interval streak; with 09->12 eligible (as stored before the rule) the
    run would be three intervals.
    """

    pipeline = pipeline if pipeline is not None else s8.Pipeline()
    session_with(
        pipeline,
        f"chain-{day}",
        [at(day, 9), at(day, 12), at(day, 15), at(day, 18)],
        interruptions=[at(day, 10, 40)],
    )
    return pipeline


def legacy_copies(relationships):
    """The relationships as stored before the unobservable-episode rule."""

    values = []
    for item in relationships:
        if UNOBSERVABLE in item.reason_codes and set(
            code for code in item.reason_codes if code.startswith("high_ineligible")
        ) == {UNOBSERVABLE}:
            item = replace(
                item,
                high_authority_eligible=True,
                reason_codes=tuple(
                    sorted((set(item.reason_codes) - {UNOBSERVABLE}) | {"high_authority_eligible"})
                ),
            )
        values.append(item)
    return tuple(values)


def tamper(record):
    """Restore pre-rule stored flags in a persisted record (in place)."""

    count = 0
    for item in record["interval_relationships"]:
        ineligible = {code for code in item["reason_codes"] if code.startswith("high_ineligible")}
        if ineligible == {UNOBSERVABLE}:
            item["high_authority_eligible"] = True
            item["reason_codes"] = sorted((set(item["reason_codes"]) - {UNOBSERVABLE}) | {"high_authority_eligible"})
            count += 1
    return count


@pytest.fixture
def without_correction(monkeypatch):
    """The behaviour before this correction (stored flags trusted)."""

    def disable():
        monkeypatch.setattr(schema4, "reassess_high_eligibility", lambda relationships, events: tuple(relationships))
        monkeypatch.setattr(schema4, "reassess_cadence_streaks", lambda streaks, relationships, chains=(): tuple(streaks))

    return disable


def summary(step):
    decision = step.decision
    regime = decision.incumbent_shadow_regime
    return (
        decision.state,
        decision.suspicion_level,
        decision.combined_confidence,
        decision.high_confidence,
        regime and (regime.regime_id, regime.candidate_period_seconds, regime.phase_offset, regime.established_at),
        tuple(sorted(item.context_id for item in decision.challenger_contexts)),
    )


# --------------------------------------------------------------------------
# Relationship reassessment
# --------------------------------------------------------------------------


def test_a_stored_flag_spanning_a_service_interruption_is_withdrawn():
    step = interrupted_chain().evaluate()
    stored = legacy_copies(step.relationships)
    assert sum(item.high_authority_eligible for item in stored) > sum(
        item.high_authority_eligible for item in step.relationships
    )

    effective = continuity.reassess_high_eligibility(stored, step.events)
    assert [item.relationship_id for item in effective] == [item.relationship_id for item in stored]
    for before, current, after in zip(stored, step.relationships, effective):
        assert after.high_authority_eligible == current.high_authority_eligible
        if before.high_authority_eligible and not after.high_authority_eligible:
            assert {WITHDRAWN, UNOBSERVABLE} <= set(after.reason_codes)
            assert after.high_quality_diagnostic == 0.0
            assert replace(after, high_authority_eligible=True, high_quality_diagnostic=before.high_quality_diagnostic, reason_codes=before.reason_codes) == before
        else:
            assert after == before


def test_legitimate_high_relationships_are_returned_unchanged():
    step = s8.three_hour_chain(4).evaluate()
    assert any(item.high_authority_eligible for item in step.relationships)
    assert continuity.reassess_high_eligibility(step.relationships, step.events) == tuple(step.relationships)


def test_reassessment_never_grants_eligibility():
    step = interrupted_chain().evaluate()
    ineligible = tuple(replace(item, high_authority_eligible=False) for item in step.relationships)
    assert continuity.reassess_high_eligibility(ineligible, step.events) == ineligible


def test_a_relationship_that_cannot_be_judged_keeps_its_stored_verdict():
    step = interrupted_chain().evaluate()
    stored = legacy_copies(step.relationships)
    withdrawn = [item for item in continuity.reassess_high_eligibility(stored, step.events) if WITHDRAWN in item.reason_codes]
    assert withdrawn
    left = withdrawn[0].left_event_id
    retained = tuple(item for item in step.events if item.event_id != left)
    kept = {item.relationship_id: item for item in continuity.reassess_high_eligibility(stored, retained)}
    assert kept[withdrawn[0].relationship_id].high_authority_eligible  # nothing invented


def test_a_single_uncertain_a2s_blip_is_not_a_new_connectivity_assumption():
    # Established policy: one failed poll cannot hide a restart, so it does
    # not block high eligibility; reassessment keeps that policy.
    pipeline = s8.Pipeline()
    session_with(
        pipeline, "blip", [at(0, 9), at(0, 12), at(0, 15)], interruptions=[at(0, 10, 40)], shape="uncertain_a2s"
    )
    step = pipeline.evaluate()
    eligible = [item for item in step.relationships if item.high_authority_eligible]
    assert eligible
    assert continuity.reassess_high_eligibility(step.relationships, step.events) == tuple(step.relationships)


def test_streaks_are_split_around_a_withdrawn_relationship():
    pipeline = s8.Pipeline()
    session_with(
        pipeline, "five", [at(0, 9) + index * P3 for index in range(5)], interruptions=[at(0, 13, 10)]
    )
    step = pipeline.evaluate()
    stored = legacy_copies(step.relationships)
    stored_streaks = continuity.build_cadence_streaks(stored)
    assert [item.interval_count for item in stored_streaks if item.candidate_period_seconds == P3] == [4]

    effective = continuity.reassess_high_eligibility(stored, step.events)
    streaks = continuity.reassess_cadence_streaks(stored_streaks, effective)
    assert streaks == continuity.build_cadence_streaks(step.relationships)
    assert [item.interval_count for item in streaks if item.candidate_period_seconds == P3] == [2]
    # Unaffected streaks are kept as stored.
    clean = continuity.build_cadence_streaks(step.relationships)
    assert continuity.reassess_cadence_streaks(clean, step.relationships) == clean


# --------------------------------------------------------------------------
# Production rebuild: authority uses the corrected eligibility
# --------------------------------------------------------------------------


def _legacy_production(pipeline):
    production = ts.Production(pipeline)
    production.rebuild()
    clean = production.rebuild()
    assert tamper(production.record) >= 1
    return production, clean


def test_obsolete_stored_eligibility_cannot_restore_rejected_authority(without_correction):
    production, clean = _legacy_production(interrupted_chain())
    stored = copy.deepcopy(production.record)

    corrected = production.rebuild()
    assert summary(corrected) == summary(clean)

    # Without the correction the obsolete flag restores a three-interval run.
    without_correction()
    legacy = ts.Production(interrupted_chain())
    legacy.record = copy.deepcopy(stored)
    legacy_step = legacy.rebuild()
    assert legacy_step.decision.high_confidence > clean.decision.high_confidence


def test_persisted_relationships_are_not_rewritten_and_rebuilds_are_stable():
    production, _clean = _legacy_production(interrupted_chain())
    tampered = {item["relationship_id"]: item for item in production.record["interval_relationships"]}
    first = production.rebuild()
    for _ in range(3):
        again = production.rebuild()
        assert again.decision.decision_id == first.decision.decision_id
    stored = {item["relationship_id"]: item for item in production.record["interval_relationships"]}
    assert {key: stored[key] for key in tampered} == tampered  # source records untouched
    assert not schema4._validate_server_record(s8.SERVER, production.record)


@pytest.mark.parametrize("compact_enabled", [False, True])
def test_raw_and_compact_authority_agree_on_corrected_eligibility(monkeypatch, compact_enabled):
    def run(enabled):
        monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", enabled)
        production, clean = _legacy_production(interrupted_chain())
        corrected = production.rebuild()
        return clean, corrected

    clean, corrected = run(compact_enabled)
    assert summary(corrected) == summary(clean)
    other_clean, other = run(not compact_enabled)
    assert compact_authority.decision_summary(corrected.decision) == compact_authority.decision_summary(other.decision)


def test_folding_never_receives_obsolete_eligibility(without_correction):
    def folded(pipeline_factory, *, tampered, disable=False):
        production = ts.Production(pipeline_factory())
        production.rebuild()
        if tampered:
            tamper(production.record)
        if disable:
            without_correction()
        production.pipeline.sparse("later", s8.aligned_3h(schema4.RAW_EVENT_RETENTION_LIMIT + 10, start_day=2))
        production.rebuild()
        production.rebuild()
        state = schema4.persisted_learned_state(production.record)
        assert state.watermark.folded_event_count > 0
        return production.record["learned_state"]["intervals"]

    clean = folded(interrupted_chain, tampered=False)
    corrected = folded(interrupted_chain, tampered=True)
    assert corrected == clean
    legacy = folded(interrupted_chain, tampered=True, disable=True)
    assert legacy != clean


def _red_dawn_like_with_interrupted_pairs():
    # Like Red Dawn: no qualifying witnessed interval (only interrupted pairs).
    pipeline = har.red_dawn_like(intervals=False, verified=False)
    for day in (10, 11, 12):
        session_with(pipeline, f"pair-{day}", [at(day, 11), at(day, 15)], interruptions=[at(day, 13, 20)])
    return pipeline


def _awaiting_four_hours(step):
    regime = step.decision.incumbent_shadow_regime
    assert step.decision.state is State.LIKELY
    assert authority.AWAITING_VERIFICATION_REASON in step.decision.reason_codes
    assert regime.candidate_period_seconds == 4 * s8.HOUR
    assert not har.recovered(step)
    assert not step.consumer().countdown_visible


def test_time_shift_carrier_and_historical_recovery_use_corrected_authority(without_correction):
    # Verified acquisition: under the current rules the interrupted pairs are
    # not witnessed intervals, so the recovered 4 h cycle awaits verification
    # at the sparse-supported value (a new history; established regimes are
    # grandfathered, see the next test).
    production = ts.Production(_red_dawn_like_with_interrupted_pairs())
    clean = production.rebuild()
    _awaiting_four_hours(clean)
    assert clean.decision.high_confidence == 0.0
    assert clean.decision.combined_confidence == pytest.approx(0.798, abs=1e-6)
    assert tamper(production.record) == 3
    stored = copy.deepcopy(production.record)

    # With the correction the obsolete stored flags change nothing: they
    # neither verify nor inflate.
    corrected = production.rebuild()
    _awaiting_four_hours(corrected)
    assert corrected.decision.combined_confidence == pytest.approx(0.798, abs=1e-6)
    assert corrected.decision.high_confidence == 0.0

    # Trusting them (the previous behaviour) would inflate the carried value.
    without_correction()
    legacy = ts.Production(_red_dawn_like_with_interrupted_pairs())
    legacy.record = copy.deepcopy(stored)
    inflated = legacy.rebuild()
    assert inflated.decision.combined_confidence > 0.9


def test_an_established_regime_keeps_its_identity_when_its_authority_is_corrected(monkeypatch):
    # Persisted while the obsolete flags were trusted, then corrected: same
    # regime, same establishment, no new regime or warning identity.
    production = ts.Production(_red_dawn_like_with_interrupted_pairs())
    production.rebuild()
    tamper(production.record)
    with monkeypatch.context() as patch:
        patch.setattr(schema4, "reassess_high_eligibility", lambda relationships, events: tuple(relationships))
        patch.setattr(schema4, "reassess_cadence_streaks", lambda streaks, relationships, chains=(): tuple(streaks))
        inflated = production.rebuild()
    corrected = production.rebuild()
    before, after = inflated.decision.incumbent_shadow_regime, corrected.decision.incumbent_shadow_regime
    assert inflated.decision.combined_confidence > 0.9
    assert corrected.decision.combined_confidence == pytest.approx(0.798, abs=1e-6)
    assert corrected.decision.state is State.ESTABLISHED and corrected.decision.suspicion_level == 0
    assert (after.regime_id, after.phase_offset, after.established_at) == (
        before.regime_id,
        before.phase_offset,
        before.established_at,
    )
    assert [item["regime_id"] for item in production.record["regimes"]] == [after.regime_id]
    value = corrected.consumer()
    due = corrected.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible
    assert due.scheduled_warning_suppression_key.regime_id == before.regime_id
    for _ in range(2):
        assert production.rebuild().decision.decision_id == corrected.decision.decision_id


def test_a_challenger_cannot_use_obsolete_authority(without_correction):
    def factory():
        pipeline = s8.three_hour_chain(4)
        # A 4 h run whose intervals each span a service interruption.
        session_with(
            pipeline,
            "four",
            [at(2, 10), at(2, 14), at(2, 18)],
            interruptions=[at(2, 11, 30), at(2, 15, 30)],
        )
        return pipeline

    production, clean = _legacy_production(factory())
    stored = copy.deepcopy(production.record)
    corrected = production.rebuild()
    assert summary(corrected) == summary(clean)
    assert corrected.decision.state is State.ESTABLISHED
    assert corrected.decision.incumbent_shadow_regime.candidate_period_seconds == P3

    # Trusting the obsolete flags would confirm a false 4 h transition.
    without_correction()
    legacy = ts.Production(factory())
    legacy.record = stored
    transition = legacy.rebuild()
    assert transition.decision.state is State.TRANSITION_CONFIRMED
    assert transition.decision.strongest_challenger.candidate_period_seconds == P4


def test_a_current_local_outage_does_not_reduce_earned_confidence():
    production = ts.Production(s8.three_hour_chain(4))
    production.rebuild()
    established = production.rebuild()
    assert established.decision.state is State.ESTABLISHED
    # A neutralised local outage: an episode finalised with a lifecycle
    # interruption in its own (new) session.
    production.restart("local-outage", at(1, 4, 20), **ts.INELIGIBLE_SHAPES["lifecycle_interrupted_restart"])
    after = production.rebuild()
    assert after.decision.state is State.ESTABLISHED and after.decision.suspicion_level == 0
    assert after.decision.combined_confidence == established.decision.combined_confidence
    assert after.decision.incumbent_shadow_regime == established.decision.incumbent_shadow_regime


def test_ineligible_events_remain_ineligible_through_rebuilds():
    pipeline = interrupted_chain()
    production = ts.Production(pipeline)
    production.restart("incomplete", at(1, 2), **ts.INELIGIBLE_SHAPES["incomplete"])
    production.restart("sleep", at(1, 5), **ts.INELIGIBLE_SHAPES["lifecycle_interrupted_restart"])
    first = {item["event_id"]: item for item in production.record["physical_events"]}
    for _ in range(2):
        production.rebuild()
    after = {item["event_id"]: item for item in production.record["physical_events"]}
    fields = ("outcome", "authenticity", "coverage_complete", "lifecycle_interruption", "canonical_phase_at")
    for key, item in first.items():
        assert all(after[key][field] == item[field] for field in fields)
    ineligible = [item for item in after.values() if not item["coverage_complete"] or item["lifecycle_interruption"]]
    assert len(ineligible) == 2
