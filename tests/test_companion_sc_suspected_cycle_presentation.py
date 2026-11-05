"""Stage 7: suspected-cycle presentation and regime-ID candidate matching.

Synthetic evidence only.  Authority trajectories reuse the Stage 6 fixtures;
nothing here reads or writes real learning state (conftest isolates HOME).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import test_companion_sc_cycle_change_recovery as s6
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumer_state as consumer_state
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher.server_companion_ui import (
    MAX_USER_VISIBLE_RESTART_CONFIDENCE_PERCENT,
    RESTART_PRESENTATION_TOOLTIPS,
    companion_display_confidence_summary,
    restart_learning_presentation,
)


State = authority.RegimeState
Key = consumers4.AuthorityPresentationKey
HOUR = s6.HOUR
P3 = s6.P3
P4 = s6.P4
at = s6.at
NOW = at(70)
CHALLENGER_REASON = consumers4.SUSPECTED_CHANGE_CHALLENGER_CONFIDENCE_REASON
UNAVAILABLE_REASON = consumers4.SUSPECTED_CHANGE_CONFIDENCE_UNAVAILABLE_REASON


def consume(decision, *, now=NOW, **kwargs):
    return consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=decision, now=now, **kwargs
        )
    )


def panel_summary(value, *, now=NOW):
    """The dictionary the SC panel renders, through the real UI helpers."""

    summary = {
        **consumers4.authority_consumer_summary(value, now=now),
        "cycle_period_seconds": value.cycle_period_seconds,
    }
    summary = companion_display_confidence_summary(
        summary, None, period_seconds=value.cycle_period_seconds
    )
    return restart_learning_presentation(summary)


def latched(decision):
    return [
        item
        for item in decision.challenger_contexts
        if authority.COUNTDOWN_SUSPENSION_LATCH_REASON in item.reason_codes
    ]


def expected_challenger_confidence(context, decision):
    candidate = next(
        item
        for item in decision.candidates
        if item.regime_id == context.proposed_regime_id
    )
    return round(context.high_authority * candidate.phase_quality, 6)


def assert_normal_incumbent(value, original):
    candidate = authority._matching_candidate(
        original.incumbent_shadow_regime, original.candidates
    )
    assert value.presentation_state is Key.CONFIRMED_CYCLE
    assert value.cycle_period_seconds == P3
    assert value.countdown_visible and value.countdown_safe
    assert not value.prediction_suspended
    assert value.confidence_display_value == candidate.combined_confidence
    assert CHALLENGER_REASON not in value.reason_codes
    assert UNAVAILABLE_REASON not in value.reason_codes


def renamed_incumbent(decision, identity="durable-original-regime-id"):
    """The incumbent keeps an older ID than the candidate that now defines it."""

    incumbent = decision.incumbent_shadow_regime
    renamed = replace(incumbent, regime_id=identity)
    selected = decision.selected_shadow_regime
    return replace(
        decision,
        decision_id=f"{decision.decision_id}-renamed",
        incumbent_shadow_regime=renamed,
        selected_shadow_regime=(
            renamed if selected.regime_id == incumbent.regime_id else selected
        ),
        regimes=tuple(
            renamed if item.regime_id == incumbent.regime_id else item
            for item in decision.regimes
        ),
        challenger_contexts=tuple(
            replace(item, incumbent_regime_id=identity)
            for item in decision.challenger_contexts
        ),
    )


# --------------------------------------------------------------------------
# 1-4: established, level 1, level 2 and stronger level 2
# --------------------------------------------------------------------------


def test_01_established_three_hour_cycle_display_unchanged():
    _evidence, original = s6.established()
    value = consume(original)
    assert_normal_incumbent(value, original)
    assert value.confidence_display_value == authority.HIGH_AUTHORITY_CAP
    shown = panel_summary(value)
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_percent"] == MAX_USER_VISIBLE_RESTART_CONFIDENCE_PERCENT
    assert shown["countdown_visible"] and shown["countdown_text"] != "--"


def test_02_level_one_anomaly_keeps_normal_countdown():
    evidence, original = s6.established()
    evidence.restart(at(4))
    suspicious = evidence.evaluate(original)
    assert suspicious.state is State.CHANGE_SUSPECTED
    value = consume(suspicious)
    assert_normal_incumbent(value, original)
    assert panel_summary(value)["countdown_visible"]


def test_03_level_two_shows_suspected_change_with_challenger_confidence():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    value = consume(suspected)
    (context,) = latched(suspected)
    expected = expected_challenger_confidence(context, suspected)

    assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    assert value.prediction_suspended
    assert not value.countdown_visible and not value.countdown_safe
    assert value.next_expected_restart_at is None
    assert value.confidence_display_value == expected
    # Two complete, perfectly aligned 4 h intervals: 1 - (1 - 0.5)^2.
    assert expected == 0.75
    assert value.confidence_display_value < original.combined_confidence
    assert CHALLENGER_REASON in value.reason_codes

    # Internal incumbent confidence is preserved.
    assert suspected.incumbent_shadow_regime == original.incumbent_shadow_regime
    assert suspected.combined_confidence == original.combined_confidence

    shown = panel_summary(value)
    assert shown["cycle_text"] == "Suspected Cycle Change"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_percent"] == 75
    assert shown["confidence_visible"]
    assert not shown["countdown_visible"]
    assert shown["countdown_text"] == "--"
    assert shown["next_text"] == "Prediction suspended"
    assert shown["presentation_key"] == "schedule_change_suspected"
    tooltip = RESTART_PRESENTATION_TOOLTIPS[shown["presentation_key"]]
    assert "Cycle Confidence" in tooltip and "paused" in tooltip


def test_04_stronger_challenger_evidence_increases_display_confidence():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    first = consume(suspected).confidence_display_value
    evidence.chain(P4, [at(25), at(29)], chain="challenger-c")
    stronger = evidence.evaluate(suspected)
    assert stronger.state is State.CHALLENGER_ACCUMULATING
    assert stronger.suspicion_level == 2
    value = consume(stronger)
    assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    assert value.confidence_display_value > first
    assert value.confidence_display_value == 0.875
    assert not value.countdown_visible


def test_04b_weaker_challenger_evidence_shows_lower_confidence():
    evidence, original = s6.established()
    evidence.chain(P4, [at(1), at(5)], chain="weak-a", authenticity=0.60)
    evidence.chain(P4, [at(13), at(17)], chain="weak-b", authenticity=0.60)
    weak = evidence.evaluate(original)
    assert weak.state is State.CHALLENGER_ACCUMULATING
    assert weak.suspicion_level == 2
    value = consume(weak)
    assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    assert 0 < value.confidence_display_value < 0.75
    (context,) = latched(weak)
    assert value.confidence_display_value == expected_challenger_confidence(
        context, weak
    )


# --------------------------------------------------------------------------
# 5: multiple challengers
# --------------------------------------------------------------------------


def two_challengers(order):
    evidence, original = s6.established()
    stages = {
        # 4 h, phase +1 h: two disconnected intervals.
        "a": lambda: (
            evidence.chain(P4, [at(1), at(5)], chain="multi-a1"),
            evidence.chain(P4, [at(13), at(17)], chain="multi-a2"),
        ),
        # 4 h, phase +2 h: three disconnected intervals (stronger).
        "b": lambda: (
            evidence.chain(P4, [at(10), at(14)], chain="multi-b1"),
            evidence.chain(P4, [at(22), at(26)], chain="multi-b2"),
            evidence.chain(P4, [at(34), at(38)], chain="multi-b3"),
        ),
    }
    for name in order:
        stages[name]()
    decision = evidence.evaluate(original)
    assert decision.state is State.CHALLENGER_ACCUMULATING
    assert decision.suspicion_level == 2
    assert len(latched(decision)) == 2
    return decision


def test_05_multiple_challengers_select_strongest_deterministically():
    forward = two_challengers("ab")
    reverse = two_challengers("ba")
    for decision in (forward, reverse):
        strongest = max(latched(decision), key=lambda item: item.high_authority)
        assert strongest.phase_offset % P4 == pytest.approx((at(10) % P4))
        value = consume(decision)
        assert value.confidence_display_value == expected_challenger_confidence(
            strongest, decision
        )
        assert value.confidence_display_value == 0.875
    assert (
        consume(forward).confidence_display_value
        == consume(reverse).confidence_display_value
    )
    # Rebuilding with identical evidence gives an identical consumer decision.
    assert consume(forward) == consume(forward)


def test_05b_equal_challengers_tie_break_is_stable():
    evidence, original = s6.established()
    evidence.chain(P4, [at(1), at(5)], chain="tie-a1")
    evidence.chain(P4, [at(13), at(17)], chain="tie-a2")
    evidence.chain(P4, [at(10), at(14)], chain="tie-b1")
    evidence.chain(P4, [at(22), at(26)], chain="tie-b2")
    decision = evidence.evaluate(original)
    contexts = latched(decision)
    assert len(contexts) == 2
    assert contexts[0].high_authority == contexts[1].high_authority
    chosen = max(contexts, key=authority._context_rank)
    expected = expected_challenger_confidence(chosen, decision)
    shuffled = replace(
        decision, challenger_contexts=tuple(reversed(decision.challenger_contexts))
    )
    assert consume(decision).confidence_display_value == expected
    assert consume(shuffled).confidence_display_value == expected


# --------------------------------------------------------------------------
# 6-9: Stage 6 recovery, H2 provisional and H3 new cycle
# --------------------------------------------------------------------------


def test_06_one_original_cycle_confirmation_keeps_suspected_state():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
    pending = evidence.evaluate(suspected)
    assert s6.progress(pending, 1)
    value = consume(pending)
    assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    assert not value.countdown_visible
    # Incumbent evidence is not challenger evidence: the display is unchanged.
    assert value.confidence_display_value == consume(suspected).confidence_display_value


def test_07_two_confirmations_restore_original_presentation():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
    pending = evidence.evaluate(suspected)
    evidence.chain(P3, [at(27), at(30)], chain="recovery-2")
    recovered = evidence.evaluate(pending)
    assert s6.restored(recovered, original)
    value = consume(recovered)
    assert_normal_incumbent(value, original)
    assert value.selected_regime_id == original.incumbent_shadow_regime.regime_id
    assert value.confidence_display_value == consume(original).confidence_display_value
    shown = panel_summary(value)
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["countdown_visible"]


def test_08_h2_provisional_presentation_unchanged():
    evidence, original = s6.established()
    transition = s6.h2_challenger(evidence, original)
    value = consume(transition)
    selected = transition.selected_shadow_regime
    candidate = next(
        item for item in transition.candidates if item.regime_id == selected.regime_id
    )
    assert value.presentation_state is Key.LIKELY_NEW_CYCLE
    assert value.cycle_period_seconds == P4
    assert value.prediction_suspended and not value.countdown_visible
    assert value.confidence_display_value == candidate.combined_confidence
    assert CHALLENGER_REASON not in value.reason_codes
    assert UNAVAILABLE_REASON not in value.reason_codes
    shown = panel_summary(value)
    assert shown["cycle_text"] == "Likely new: Every 4 hours"
    assert shown["confidence_label"] == "Cycle Confidence:"


def test_09_h3_new_cycle_displays_new_schedule_and_confidence():
    evidence, original = s6.established()
    evidence.chain(P4, [at(1), at(5), at(9), at(13)], chain="new-4h")
    promoted = evidence.evaluate(original)
    assert promoted.state is State.NEW_REGIME_ESTABLISHED
    settled = evidence.evaluate(promoted)
    for decision, key in (
        (promoted, Key.CONFIRMED_NEW_CYCLE),
        (settled, Key.CONFIRMED_CYCLE),
    ):
        value = consume(decision)
        regime = decision.selected_shadow_regime
        candidate = authority._matching_candidate(regime, decision.candidates)
        assert value.presentation_state is key
        assert value.cycle_period_seconds == P4
        assert value.countdown_visible and value.countdown_safe
        assert value.selected_regime_id == regime.regime_id
        assert value.confidence_display_value == candidate.combined_confidence
        assert (value.next_expected_restart_at - regime.phase_offset) % P4 == 0


# --------------------------------------------------------------------------
# 10-13: regime-ID matching and harmonic blockers
# --------------------------------------------------------------------------


def test_10_matching_ids_preserve_existing_candidate_selection():
    _evidence, original = s6.established()
    regime = original.incumbent_shadow_regime
    by_id = [item for item in original.candidates if item.regime_id == regime.regime_id]
    assert by_id
    assert consumers4._candidate_for_regime(original, regime) == max(
        by_id, key=lambda item: item.high_authority
    )


@pytest.mark.parametrize("stage", ["established", "level_one", "recovered"])
def test_11_mismatched_ids_match_same_candidate_by_period_and_phase(stage):
    evidence, original = s6.established()
    decision = original
    if stage == "level_one":
        evidence.restart(at(4))
        decision = evidence.evaluate(original)
    elif stage == "recovered":
        suspected = s6.level_two_challenger(evidence, original)
        evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
        pending = evidence.evaluate(suspected)
        evidence.chain(P3, [at(27), at(30)], chain="recovery-2")
        decision = evidence.evaluate(pending)
    renamed = renamed_incumbent(decision)
    matched = consumers4._candidate_for_regime(decision, decision.incumbent_shadow_regime)
    fallback = consumers4._candidate_for_regime(renamed, renamed.incumbent_shadow_regime)
    assert matched is not None and fallback == matched
    assert fallback == authority._matching_candidate(
        renamed.incumbent_shadow_regime, renamed.candidates
    )
    same, other = consume(decision), consume(renamed)
    assert other.presentation_state is same.presentation_state
    assert other.countdown_visible == same.countdown_visible
    assert other.next_expected_restart_at == same.next_expected_restart_at
    assert other.confidence_display_value == same.confidence_display_value
    assert other.phase_available == same.phase_available


def test_11b_mismatched_ids_keep_level_two_challenger_display():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    renamed = renamed_incumbent(suspected)
    value = consume(renamed)
    assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    assert value.confidence_display_value == consume(suspected).confidence_display_value


def test_12_mismatched_ids_never_select_unrelated_candidates():
    _evidence, original = s6.established()
    regime = original.incumbent_shadow_regime
    source = authority._matching_candidate(regime, original.candidates)
    tolerance = regime.phase_tolerance
    unrelated = (
        # Same period, phase just outside tolerance.
        replace(
            source,
            regime_id="near-phase",
            candidate_id="near-phase",
            phase_offset=(regime.phase_offset + tolerance + 60) % P3,
        ),
        # Same phase, different period.
        replace(
            source,
            regime_id="other-period",
            candidate_id="other-period",
            candidate_period_seconds=P4,
        ),
    )
    renamed = replace(
        renamed_incumbent(original), candidates=unrelated
    )
    assert consumers4._candidate_for_regime(renamed, renamed.incumbent_shadow_regime) is None
    # Without a matching candidate the stored regime gate is used, as before.
    value = consume(renamed)
    assert value.selected_regime_id == "durable-original-regime-id"


def harmonic_incumbent(period, *, regime_id=None, blockers=None):
    """An established continuity-H2 incumbent at a harmonic period."""

    evidence = s6.Evidence()
    evidence.chain(period, [s6.BASE + index * period for index in range(4)], chain="harm")
    source = evidence.evaluate()
    candidate = max(
        (item for item in source.candidates if item.candidate_period_seconds == period),
        key=lambda item: item.high_authority,
    )
    # Real candidates at these periods carry unresolved divisor blockers.
    assert candidate.harmonic_blockers
    h3_only = tuple(
        item for item in candidate.harmonic_blockers if item.startswith("high_harmonic")
    )
    candidate = replace(
        candidate,
        h2_gate=True,
        h3_gate=False,
        h4_gate=False,
        harmonic_blockers=h3_only if blockers is None else blockers,
    )
    regime = authority._regime_from_candidate(
        candidate, status=authority.RegimeRecordStatus.ESTABLISHED
    )
    if regime_id is not None:
        regime = replace(regime, regime_id=regime_id)
    return replace(
        source,
        decision_id=f"harmonic-{period}-{regime.regime_id}-{bool(candidate.harmonic_blockers)}",
        state=State.ESTABLISHED,
        selected_shadow_regime=regime,
        incumbent_shadow_regime=regime,
        regimes=(regime,),
        candidates=(candidate,),
        cycle_visible=True,
        prediction_usable=True,
        countdown_safe=True,
        suspicion_level=0,
        active_suspicion_evidence_ids=(),
        combined_confidence=candidate.combined_confidence,
    )


@pytest.mark.parametrize("hours", [6, 8, 12])
@pytest.mark.parametrize("regime_id", [None, "durable-original-regime-id"])
def test_13_harmonic_blockers_effective_for_matching_and_mismatched_ids(hours, regime_id):
    period = hours * HOUR
    blocked = consume(harmonic_incumbent(period, regime_id=regime_id))
    assert not blocked.countdown_visible
    assert not blocked.countdown_safe
    assert not blocked.scheduled_warning_eligible
    assert blocked.presentation_state not in {Key.CONFIRMED_CYCLE, Key.CONFIRMED_NEW_CYCLE}
    resolved = consume(harmonic_incumbent(period, regime_id=regime_id, blockers=()))
    assert resolved.presentation_state is Key.CONFIRMED_CYCLE
    assert resolved.countdown_visible


def test_13b_mismatched_id_previously_bypassed_harmonic_blockers(monkeypatch):
    """Documents the defect: ID-only lookup let a blocked 6 h incumbent count down."""

    decision = harmonic_incumbent(6 * HOUR, regime_id="durable-original-regime-id")

    def id_only(decision, regime):
        if regime is None:
            return None
        matches = [item for item in decision.candidates if item.regime_id == regime.regime_id]
        return max(matches, key=lambda item: item.high_authority, default=None)

    assert not consume(decision).countdown_visible
    monkeypatch.setattr(consumers4, "_candidate_for_regime", id_only)
    assert consume(decision).countdown_visible


@pytest.mark.parametrize("period", [P3, P4])
def test_13c_three_and_four_hour_countdowns_preserved_with_mismatched_ids(period):
    evidence = s6.Evidence()
    evidence.chain(period, [s6.BASE + index * period for index in range(7)], chain="short")
    decision = evidence.evaluate()
    assert decision.state is State.ESTABLISHED
    renamed = renamed_incumbent(decision)
    value = consume(renamed)
    assert value.presentation_state is Key.CONFIRMED_CYCLE
    assert value.countdown_visible
    assert value.next_expected_restart_at == consume(decision).next_expected_restart_at


# --------------------------------------------------------------------------
# 14-17: no fabrication, determinism, alerts, retention
# --------------------------------------------------------------------------


def test_14_no_fabricated_confidence_without_credible_challenger():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    stripped = replace(
        suspected,
        challenger_contexts=tuple(
            replace(
                item,
                reason_codes=tuple(
                    code
                    for code in item.reason_codes
                    if code != authority.COUNTDOWN_SUSPENSION_LATCH_REASON
                ),
            )
            for item in suspected.challenger_contexts
        ),
    )
    for decision in (stripped, replace(suspected, challenger_contexts=())):
        value = consume(decision)
        assert value.presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
        assert value.confidence_display_value == 0.0
        assert UNAVAILABLE_REASON in value.reason_codes
        assert CHALLENGER_REASON not in value.reason_codes
        shown = panel_summary(value)
        assert not shown["confidence_visible"]
        assert not shown["countdown_visible"]
        assert shown["cycle_text"] == "Suspected Cycle Change"


def test_15_consumer_deterministic_after_rebuild_and_reload():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    rebuilt = evidence.evaluate(suspected)
    reloaded = s6.persisted(suspected)
    assert consume(reloaded) == consume(suspected)
    assert consume(rebuilt).confidence_display_value == consume(suspected).confidence_display_value
    assert consume(rebuilt).presentation_state is Key.SCHEDULE_CHANGE_SUSPECTED
    reloaded_rebuild = evidence.evaluate(s6.persisted(rebuilt))
    assert reloaded_rebuild.decision_id == evidence.evaluate(rebuilt).decision_id
    assert consume(reloaded_rebuild) == consume(evidence.evaluate(rebuilt))


def test_16_no_alerts_and_display_value_outside_identity():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    value = consume(suspected, restart_alert_enabled=True, server_online_healthy=True)
    assert not value.scheduled_warning_eligible
    assert value.scheduled_warning_suppression_key is None
    assert not value.scheduled_pre_restart_alert_eligible
    assert not value.outage_relaxation_eligible
    # Challenger confidence is display-only: changing it changes neither the
    # consumer identity nor any alert key.
    boosted = replace(
        suspected,
        challenger_contexts=tuple(
            replace(item, high_authority=0.5) for item in suspected.challenger_contexts
        ),
    )
    other = consume(boosted, restart_alert_enabled=True, server_online_healthy=True)
    assert other.confidence_display_value != value.confidence_display_value
    assert other.decision_id == value.decision_id
    ledger = consumer_state.ledger_from_mapping(
        consumer_state.empty_consumer_state(s6.SERVER), server_key=s6.SERVER
    )
    ledger = consumer_state.with_decision(ledger, value)
    ledger = consumer_state.with_decision(ledger, other)
    assert len(ledger.decisions) == 1
    assert not ledger.alert_records
    assert consumer_state.fired_suppression_keys(ledger) == frozenset()
    # Authority never sees the display value.
    assert evidence.evaluate(suspected) == evidence.evaluate(suspected)


def test_17_sample_retention_does_not_change_presentation():
    evidence, original = s6.established()
    suspected = s6.level_two_challenger(evidence, original)
    sample = detection.ObservationSample(
        wall_at=s6.BASE,
        monotonic_at=1.0,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key=s6.SERVER,
        info_status=detection.InfoStatus.HEALTHY,
        info_latency=0.04,
        player_status=detection.FieldStatus.PRESENT,
        players=12,
        max_players=60,
        queue_status=detection.FieldStatus.PRESENT,
        queue=0,
    )
    sampled = evidence.copy()
    sampled.events = [replace(item, samples=(sample,)) for item in evidence.events]
    stripped = evidence.copy()
    stripped.events = [replace(item, samples=()) for item in evidence.events]
    assert consume(sampled.evaluate(suspected)) == consume(stripped.evaluate(suspected))


def test_18_full_trajectory_presentation():
    """Stage 6 trajectory through the consumer: presentation per stage."""

    _evidence, decisions = s6.trajectory()
    keys = [consume(item).presentation_state for item in decisions]
    assert keys == [
        Key.CONFIRMED_CYCLE,  # established
        Key.CONFIRMED_CYCLE,  # one high relationship: level 1
        Key.SCHEDULE_CHANGE_SUSPECTED,  # two high relationships: level 2
        Key.SCHEDULE_CHANGE_SUSPECTED,  # one aligned restart
        Key.SCHEDULE_CHANGE_SUSPECTED,  # one confirmation
        Key.CONFIRMED_CYCLE,  # two confirmations
        Key.LIKELY_NEW_CYCLE,  # new H2 challenger
        Key.LIKELY_NEW_CYCLE,  # one confirmation
        Key.CONFIRMED_CYCLE,  # two confirmations
    ]
    suspected = [
        consume(item).confidence_display_value
        for item, key in zip(decisions, keys)
        if key is Key.SCHEDULE_CHANGE_SUSPECTED
    ]
    assert suspected == [0.75, 0.75, 0.75]
    assert all(
        not consume(item).countdown_visible
        for item, key in zip(decisions, keys)
        if key is not Key.CONFIRMED_CYCLE
    )
