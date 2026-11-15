"""Stage 6: Server Companion cycle-change recovery (synthetic evidence only).

Every scenario uses in-memory synthetic restart evidence and explicit
timestamps.  No runtime, profile or learning file is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_900_000_000.0
SERVER = "stage6-recovery.example:2302"
HOUR = scoring.HOUR
P3 = 3 * HOUR
P4 = 4 * HOUR
State = authority.RegimeState


# --------------------------------------------------------------------------
# Synthetic evidence helpers
# --------------------------------------------------------------------------


def event(
    sequence,
    at,
    *,
    authenticity=0.85,
    outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
    coverage_complete=True,
    lifecycle=None,
    samples=(),
):
    identity = f"s6-event-{sequence}-{int(at)}"
    return detection.PhysicalRestartEvent(
        event_id=identity,
        sequence=sequence,
        fingerprint=f"fingerprint-{identity}",
        server_key=SERVER,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(
            None, None, None, None, False, None, None, 0, 0, False
        ),
        outage=detection.OutageSummary(
            at - 70, at - 60, 3, (detection.InfoStatus.TIMEOUT,), at
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=coverage_complete,
        lifecycle_interruption=lifecycle,
        samples=samples,
        reason_codes=("stage6_fixture",),
        app_session_id="app",
        monitoring_session_id=f"monitor-{identity}",
        poll_generation=1,
        continuity_chain_id="fixture-chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def relationship(left, right, period, *, chain, high=True, identity=None):
    residual = (right.canonical_phase_at - left.canonical_phase_at) - period
    tolerance = scoring.candidate_phase_tolerance(period)
    return continuity.IntervalRelationship(
        schema_version=continuity.CONTINUITY_SCHEMA_VERSION,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
        relationship_id=identity
        or f"s6-relationship-{period}-{left.event_id}-{right.event_id}",
        server_key=SERVER,
        candidate_period_seconds=period,
        left_event_id=left.event_id,
        left_event_sequence=left.sequence,
        right_event_id=right.event_id,
        right_event_sequence=right.sequence,
        left_at=left.canonical_phase_at,
        right_at=right.canonical_phase_at,
        observed_interval_seconds=right.canonical_phase_at - left.canonical_phase_at,
        signed_residual_seconds=residual,
        tolerance_seconds=tolerance,
        relationship_kind=continuity.RelationshipKind.DIRECT,
        multiplier=1,
        intervening_event_ids=(),
        coverage_quality=(
            continuity.CoverageQuality.COMPLETE
            if high
            else continuity.CoverageQuality.UNPROVEN
        ),
        observed_ratio=0.98 if high else 0.0,
        largest_unexplained_gap=0,
        monitoring_session_id=f"monitor-{chain}" if high else None,
        continuity_chain_id=chain if high else None,
        continuity_qualified=high,
        normal_support_diagnostic=0.8,
        high_authority_eligible=high,
        high_quality_diagnostic=1.0 if high else 0.0,
        reason_codes=("stage6_fixture_relationship",),
    )


class Evidence:
    """Accumulating synthetic evidence for one server."""

    def __init__(self):
        self.events = []
        self.relationships = []
        self.streaks = []
        self.windows = []
        self._sequence = 1

    def copy(self):
        value = Evidence()
        value.events = list(self.events)
        value.relationships = list(self.relationships)
        value.streaks = list(self.streaks)
        value.windows = list(self.windows)
        value._sequence = self._sequence
        return value

    def restart(self, at, **kwargs):
        value = event(self._sequence, at, **kwargs)
        self._sequence += 1
        self.events.append(value)
        return value

    def chain(self, period, times, *, chain, high=True, **kwargs):
        """One continuously observed session containing `times` restarts."""

        values = [self.restart(at, **kwargs) for at in times]
        relationships = [
            relationship(values[index], values[index + 1], period, chain=chain, high=high)
            for index in range(len(values) - 1)
        ]
        self.relationships.extend(relationships)
        self.streaks.extend(continuity.build_cadence_streaks(relationships))
        return values, relationships

    def evaluate(self, prior=None):
        return authority.evaluate_authority(
            server_key=SERVER,
            normal_score=empty_score(),
            relationships=tuple(self.relationships),
            streaks=tuple(self.streaks),
            expected_window_ledger=tuple(self.windows),
            events=tuple(self.events),
            prior_decision=prior,
        )


def empty_score():
    return scoring.RestartScheduleScorer().score(
        (), scoring.CoverageTimeline(()), now=BASE
    )


def miss(period, expected, *, key):
    tolerance = scoring.candidate_phase_tolerance(period)
    return windows.import_legacy_expected_miss(
        server_key=SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - tolerance,
        expected_at=expected,
        window_end_at=expected + tolerance,
        model_revision_id="stage6-model",
        legacy_key=key,
    )


# Established 3 h incumbent: seven restarts, six continuous intervals, so its
# high authority is already at the cap and "unchanged confidence" is exact.
T = BASE + 18 * HOUR


def at(hours, minutes=0):
    return T + hours * HOUR + minutes * 60


def established():
    evidence = Evidence()
    evidence.chain(P3, [BASE + index * P3 for index in range(7)], chain="incumbent")
    decision = evidence.evaluate()
    assert decision.state is State.ESTABLISHED
    assert decision.incumbent_shadow_regime.candidate_period_seconds == P3
    assert decision.incumbent_shadow_regime.established_at == T
    assert decision.combined_confidence == authority.HIGH_AUTHORITY_CAP
    return evidence, decision


def level_two_challenger(evidence, prior):
    """Two disconnected, complete 4 h intervals off the 3 h phase."""

    evidence.chain(P4, [at(1), at(5)], chain="challenger-a")
    evidence.chain(P4, [at(13), at(17)], chain="challenger-b")
    decision = evidence.evaluate(prior)
    assert decision.state is State.CHALLENGER_ACCUMULATING
    assert decision.suspicion_level == 2
    assert not decision.countdown_safe
    return decision


def h2_challenger(evidence, prior):
    """A continuous 4 h streak of two intervals (H2) after establishment."""

    evidence.chain(P4, [at(1), at(5), at(9)], chain="challenger-h2")
    decision = evidence.evaluate(prior)
    assert decision.state is State.TRANSITION_CONFIRMED
    assert decision.selected_shadow_regime.candidate_period_seconds == P4
    assert decision.incumbent_shadow_regime.candidate_period_seconds == P3
    assert not decision.prediction_usable and not decision.countdown_safe
    return decision


def restored(decision, original):
    return bool(
        decision.state is State.ESTABLISHED
        and decision.suspicion_level == 0
        and decision.prediction_usable
        and decision.countdown_safe
        and decision.selected_shadow_regime.regime_id
        == original.incumbent_shadow_regime.regime_id
        and decision.incumbent_shadow_regime.regime_id
        == original.incumbent_shadow_regime.regime_id
    )


def persisted(decision):
    """Round-trip a decision through the real schema-4 serializer as JSON."""

    record = {
        "authority_decision": schema4._decision_payload(decision),
        "authority_candidates": [schema4._primitive(item) for item in decision.candidates],
        "challenger_contexts": [
            schema4._primitive(item) for item in decision.challenger_contexts
        ],
        "regimes": [schema4._primitive(item) for item in decision.regimes],
        "normal_evidence_ledger": schema4._ledger_wrapper(
            "normal", decision.normal_ledger, decision.decision_id
        ),
        "high_evidence_ledger": schema4._ledger_wrapper(
            "high", decision.high_ledger, decision.decision_id
        ),
    }
    return schema4.persisted_authority_decision(
        json.loads(json.dumps(record, sort_keys=True))
    )


def progress(decision, count):
    return authority._recovery_progress_reason(count) in decision.reason_codes


# --------------------------------------------------------------------------
# 1-2: an established cycle is resilient to isolated deviations
# --------------------------------------------------------------------------


def test_01_isolated_off_phase_restart_keeps_established_prediction():
    evidence, original = established()
    evidence.restart(at(4))
    value = evidence.evaluate(original)
    assert value.state is State.CHANGE_SUSPECTED
    assert value.suspicion_level == 1
    assert value.prediction_usable and value.countdown_safe
    assert value.incumbent_shadow_regime == original.incumbent_shadow_regime
    assert value.combined_confidence == original.combined_confidence


def test_02_covered_misses_only_keep_established_prediction():
    # Stage 5 correction: a strongly established (E2) cycle keeps its
    # prediction through one genuine miss, and through misses that are not of
    # consecutive occurrences (here 03:00 and 09:00, 06:00 unmonitored).
    for times in ((at(3),), (at(3), at(9))):
        evidence, original = established()
        evidence.windows.extend(
            miss(P3, when, key=f"miss-{index}") for index, when in enumerate(times)
        )
        value = evidence.evaluate(original)
        assert value.state is State.CHANGE_SUSPECTED
        assert value.suspicion_level == 1
        assert value.prediction_usable and value.countdown_safe
        assert value.incumbent_shadow_regime == original.incumbent_shadow_regime
        assert value.combined_confidence == original.combined_confidence

    # Two consecutive genuinely monitored misses (03:00 and 06:00) withhold
    # the prediction (was: kept); the regime and its confidence are kept.
    evidence, original = established()
    evidence.windows.extend(
        (miss(P3, at(3), key="miss-1"), miss(P3, at(6), key="miss-2"))
    )
    value = evidence.evaluate(original)
    assert value.state is State.CHANGE_SUSPECTED
    assert value.suspicion_level == 1
    assert authority.CONSECUTIVE_MISSES_REASON in value.reason_codes
    assert not value.prediction_usable and not value.countdown_safe
    assert value.incumbent_shadow_regime == original.incumbent_shadow_regime
    assert value.combined_confidence == original.combined_confidence


# --------------------------------------------------------------------------
# 3-5: level-2 suspected change requires two original-cycle confirmations
# --------------------------------------------------------------------------


def test_03_level_two_one_aligned_event_does_not_restore():
    evidence, original = established()
    suspected = level_two_challenger(evidence, original)
    assert any(
        authority.COUNTDOWN_SUSPENSION_LATCH_REASON in item.reason_codes
        for item in suspected.challenger_contexts
    )
    evidence.restart(at(18))
    value = evidence.evaluate(suspected)
    assert value.state is State.CHALLENGER_ACCUMULATING
    assert value.suspicion_level == 2
    assert not value.countdown_safe
    assert progress(value, 0)


def test_04_level_two_one_complete_interval_does_not_restore():
    evidence, original = established()
    suspected = level_two_challenger(evidence, original)
    evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
    value = evidence.evaluate(suspected)
    assert value.state is State.CHALLENGER_ACCUMULATING
    assert value.suspicion_level == 2
    assert not value.countdown_safe
    assert progress(value, 1)


def test_05_level_two_two_independent_intervals_restore_original():
    evidence, original = established()
    suspected = level_two_challenger(evidence, original)
    evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
    pending = evidence.evaluate(suspected)
    assert not restored(pending, original)
    evidence.chain(P3, [at(27), at(30)], chain="recovery-2")
    value = evidence.evaluate(pending)
    assert restored(value, original)
    assert value.combined_confidence == original.combined_confidence


def test_05b_never_suspended_level_one_context_keeps_single_event_clearing():
    """Low-level suspicion that never hid the countdown is unchanged."""

    evidence, original = established()
    evidence.chain(P4, [at(1), at(5)], chain="single-clue")
    suspected = evidence.evaluate(original)
    assert suspected.state is State.CHANGE_SUSPECTED
    assert suspected.countdown_safe
    evidence.restart(at(6))
    value = evidence.evaluate(suspected)
    assert restored(value, original)


# --------------------------------------------------------------------------
# 6-7: H2 provisional challenger (fixes the permanently active context)
# --------------------------------------------------------------------------


def test_06_h2_one_complete_interval_does_not_restore():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    value = evidence.evaluate(transition)
    assert value.state in {State.TRANSITION_CONFIRMED, State.NEW_REGIME_PROVISIONAL}
    assert not value.countdown_safe
    assert progress(value, 1)


def test_07_h2_two_independent_intervals_restore_original():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    pending = evidence.evaluate(transition)
    evidence.chain(P3, [at(21), at(24)], chain="recovery-2")
    value = evidence.evaluate(pending)
    assert restored(value, original)
    assert (
        "provisional_challenger_resolved_by_original_cycle_confirmations"
        in value.reason_codes
    )


def test_07b_h2_recovery_also_applies_to_incumbent_without_establishment_time():
    """A legacy LIKELY-then-established incumbent has established_at None.

    Since Stage 8.1 a clean two-interval streak establishes at once with
    ``established_at`` set, so the pre-8.1 persisted shape (an ESTABLISHED
    regime promoted from LIKELY by a rebuild, ``established_at=None``) is
    reproduced explicitly.  Such incumbents remain valid and recoverable.
    """

    evidence = Evidence()
    values, _ = evidence.chain(
        P3, [BASE + index * P3 for index in range(3)], chain="incumbent"
    )
    current = evidence.evaluate()
    assert current.state is State.ESTABLISHED
    legacy_regime = replace(current.incumbent_shadow_regime, established_at=None)
    likely = replace(
        current,
        selected_shadow_regime=legacy_regime,
        incumbent_shadow_regime=legacy_regime,
        regimes=(legacy_regime,),
    )
    more = [evidence.restart(BASE + index * P3) for index in range(3, 7)]
    previous = values[-1]
    for value in more:
        evidence.relationships.append(
            relationship(previous, value, P3, chain="incumbent")
        )
        previous = value
    evidence.streaks = list(
        continuity.build_cadence_streaks(tuple(evidence.relationships))
    )
    original = evidence.evaluate(likely)
    assert original.state is State.ESTABLISHED
    assert original.incumbent_shadow_regime.established_at is None
    transition = h2_challenger(evidence, original)
    evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    pending = evidence.evaluate(transition)
    assert not pending.countdown_safe
    evidence.chain(P3, [at(21), at(24)], chain="recovery-2")
    value = evidence.evaluate(pending)
    assert restored(value, original)


# --------------------------------------------------------------------------
# 8-11: evidence integrity of recovery confirmations
# --------------------------------------------------------------------------


def test_08_confirmations_from_separate_monitoring_sessions_are_valid():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    first, _ = evidence.chain(P3, [at(12), at(15)], chain="session-a")
    second, _ = evidence.chain(P3, [at(30), at(33)], chain="session-b")
    assert first[0].monitoring_session_id != second[0].monitoring_session_id
    # The disconnected gap between sessions is not manufactured into a third
    # confirmation: a cross-session interval is never high-eligible.
    evidence.relationships.append(
        relationship(first[1], second[0], P3, chain="cross", high=False)
    )
    value = evidence.evaluate(transition)
    assert restored(value, original)


def test_09_overlapping_or_duplicated_intervals_count_once():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    _values, (interval,) = evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    evidence.relationships.append(
        replace(interval, relationship_id=interval.relationship_id + "-duplicate")
    )
    evidence.chain(P3, [at(12, 5), at(15, 5)], chain="recovery-overlap")
    value = evidence.evaluate(transition)
    assert not value.countdown_safe
    assert value.state in {State.TRANSITION_CONFIRMED, State.NEW_REGIME_PROVISIONAL}
    assert progress(value, 1)


def test_10_original_evidence_predating_challenger_cannot_recover():
    evidence, original = established()
    evidence.chain(P4, [at(1), at(5)], chain="challenger-a")
    # Two complete original-cycle intervals between the challenger intervals.
    evidence.chain(P3, [at(6), at(9)], chain="interleaved-a")
    evidence.chain(P3, [at(9), at(12)], chain="interleaved-b")
    evidence.chain(P4, [at(13), at(17)], chain="challenger-b")
    suspected = evidence.evaluate(original)
    assert suspected.suspicion_level == 2
    evidence.restart(at(18))
    value = evidence.evaluate(suspected)
    assert value.suspicion_level == 2
    assert not value.countdown_safe
    assert progress(value, 0)


def test_10b_new_challenger_evidence_resets_recovery_progress():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    pending = evidence.evaluate(transition)
    assert progress(pending, 1)
    # The challenger is seen again after the first confirmation (weaker
    # support, so it stays below H3 under the unchanged gates).
    evidence.chain(
        P4, [at(17), at(21)], chain="challenger-again", authenticity=0.6
    )
    renewed = evidence.evaluate(pending)
    assert not renewed.countdown_safe
    assert progress(renewed, 0)
    evidence.chain(P3, [at(24), at(27)], chain="recovery-2")
    value = evidence.evaluate(renewed)
    assert not value.countdown_safe
    assert progress(value, 1)


def test_11_unobservable_or_local_failure_intervals_cannot_recover():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    base = evidence.copy()

    # (a) Continuity broken (sleep gap / local connectivity neutralisation):
    # the interval is not high-eligible.
    unproven = base.copy()
    unproven.chain(P3, [at(12), at(15)], chain="gap-a", high=False)
    unproven.chain(P3, [at(21), at(24)], chain="gap-b", high=False)
    assert not unproven.evaluate(transition).countdown_safe

    # (b) Lifecycle-interrupted, incomplete endpoints (SLEEP_GAP finalize).
    interrupted = base.copy()
    for left, right, chain in ((12, 15, "sleep-a"), (21, 24, "sleep-b")):
        interrupted.chain(
            P3,
            [at(left), at(right)],
            chain=chain,
            coverage_complete=False,
            lifecycle=detection.LifecycleMarker.SLEEP_GAP,
        )
    assert not interrupted.evaluate(transition).countdown_safe

    # (c) QV drain-only "restarts" are not authentic endpoints.
    drains = base.copy()
    for left, right, chain in ((12, 15, "drain-a"), (21, 24, "drain-b")):
        drains.chain(
            P3,
            [at(left), at(right)],
            chain=chain,
            outcome=detection.EventOutcome.AMBIGUOUS_DRAIN,
            authenticity=0.9,
        )
    assert not drains.evaluate(transition).countdown_safe

    # (d) Real extraction with no observed coverage yields no high interval.
    unmonitored = base.copy()
    events = [unmonitored.restart(at(hours)) for hours in (12, 15, 21, 24)]
    extracted = continuity.extract_interval_relationships(events, ())
    assert extracted and not any(item.high_authority_eligible for item in extracted)
    unmonitored.relationships.extend(extracted)
    assert not unmonitored.evaluate(transition).countdown_safe


# --------------------------------------------------------------------------
# 12-14: establishment of a new regime ends the rollback privilege
# --------------------------------------------------------------------------


def replaced_by_four_hour():
    evidence, original = established()
    evidence.chain(P4, [at(1), at(5), at(9), at(13)], chain="new-4h")
    promoted = evidence.evaluate(original)
    assert promoted.state is State.NEW_REGIME_ESTABLISHED
    settled = evidence.evaluate(promoted)
    assert settled.state is State.ESTABLISHED
    assert settled.incumbent_shadow_regime.candidate_period_seconds == P4
    return evidence, original, settled


def test_12_legitimate_h3_challenger_becomes_established_default():
    evidence, original, settled = replaced_by_four_hour()
    assert settled.prediction_usable and settled.countdown_safe
    old = next(
        item
        for item in settled.regimes
        if item.regime_id == original.incumbent_shadow_regime.regime_id
    )
    assert old.status is authority.RegimeRecordStatus.SUPERSEDED
    assert settled.incumbent_shadow_regime.established_at == at(13)


def test_13_superseded_regime_gets_no_privileged_rollback():
    evidence, _original, settled = replaced_by_four_hour()
    separate = evidence.copy()
    separate.chain(P3, [at(15), at(18)], chain="old-a")
    separate.chain(P3, [at(27), at(30)], chain="old-b")
    value = separate.evaluate(settled)
    assert value.incumbent_shadow_regime.candidate_period_seconds == P4
    assert value.state is State.CHALLENGER_ACCUMULATING
    assert value.state is not State.NEW_REGIME_ESTABLISHED

    consecutive = evidence.copy()
    consecutive.chain(P3, [at(15), at(18), at(21)], chain="old-run")
    value = consecutive.evaluate(settled)
    # An ordinary H2 challenger: provisional, never an automatic restoration.
    assert value.state is State.TRANSITION_CONFIRMED
    assert value.incumbent_shadow_regime.candidate_period_seconds == P4
    assert HISTORICAL_REASON in value.strongest_challenger.reason_codes


HISTORICAL_REASON = authority.HISTORICAL_CHALLENGER_EVIDENCE_REASON


def test_14_former_regime_can_return_through_normal_rules():
    evidence, original, settled = replaced_by_four_hour()
    evidence.chain(P3, [at(15), at(18), at(21), at(24)], chain="old-returns")
    value = evidence.evaluate(settled)
    assert value.state is State.NEW_REGIME_ESTABLISHED
    assert value.incumbent_shadow_regime.candidate_period_seconds == P3
    assert (
        value.incumbent_shadow_regime.phase_offset
        == original.incumbent_shadow_regime.phase_offset
    )
    four = next(item for item in value.regimes if item.candidate_period_seconds == P4)
    assert four.status is authority.RegimeRecordStatus.SUPERSEDED
    # Historical learning is preserved in the re-established regime (since
    # Stage 8.3 aligned restarts outside its streaks add bounded sparse
    # support on top of the capped continuous authority).
    assert value.selected_shadow_regime.high_authority == authority.HIGH_AUTHORITY_CAP
    assert value.combined_confidence >= authority.HIGH_AUTHORITY_CAP


# --------------------------------------------------------------------------
# 15-16: restoration preserves confidence and challenger evidence
# --------------------------------------------------------------------------


def test_15_and_16_restoration_preserves_confidence_and_challenger_evidence():
    evidence, original = established()
    transition = h2_challenger(evidence, original)
    challenger_ids = set(transition.strongest_challenger.high_relationship_ids)
    evidence.chain(P3, [at(12), at(15)], chain="recovery-1")
    evidence.chain(P3, [at(21), at(24)], chain="recovery-2")
    value = evidence.evaluate(transition)
    assert restored(value, original)
    before = original.incumbent_shadow_regime
    after = value.incumbent_shadow_regime
    assert after.regime_id == before.regime_id
    assert after.candidate_period_seconds == before.candidate_period_seconds
    assert after.phase_offset == before.phase_offset
    assert after.established_at == before.established_at
    # Continuous authority is preserved exactly; since Stage 8.3 the 4 h
    # challenger restart on the 3 h grid adds bounded sparse support.
    assert after.high_authority == before.high_authority
    assert after.combined_confidence >= before.combined_confidence
    assert value.combined_confidence >= original.combined_confidence
    # Challenger evidence is retained, not deleted, after restoration.
    context = next(
        item for item in value.challenger_contexts if item.candidate_period_seconds == P4
    )
    assert set(context.high_relationship_ids) == challenger_ids
    assert context.high_confirmation_present
    assert challenger_ids <= set(value.high_ledger.high_relationship_ids)
    assert any(item.candidate_period_seconds == P4 for item in value.regimes)


# --------------------------------------------------------------------------
# 17-18: determinism, persistence and restart
# --------------------------------------------------------------------------


def trajectory(prior_transform=lambda decision: decision):
    evidence, original = established()
    decisions = [original]
    stages = [
        lambda: evidence.chain(P4, [at(1), at(5)], chain="challenger-a"),
        lambda: evidence.chain(P4, [at(13), at(17)], chain="challenger-b"),
        lambda: evidence.restart(at(18)),
        lambda: evidence.chain(P3, [at(21), at(24)], chain="recovery-1"),
        lambda: evidence.chain(P3, [at(30), at(33)], chain="recovery-2"),
        # A different 4 h phase, so it is a new H2 challenger rather than
        # further support for the first one.
        lambda: evidence.chain(P4, [at(38), at(42), at(46)], chain="h2"),
        lambda: evidence.chain(P3, [at(48), at(51)], chain="recovery-3"),
        lambda: evidence.chain(P3, [at(57), at(60)], chain="recovery-4"),
    ]
    for stage in stages:
        stage()
        decisions.append(evidence.evaluate(prior_transform(decisions[-1])))
    return evidence, decisions


def test_17_repeated_rebuilds_without_new_evidence_are_identical():
    evidence, original = established()
    # The first unknown-server establishment settles once into the ordinary
    # incumbent path on its next rebuild (pre-existing behaviour); from then
    # on every state must be a fixed point of rebuilding.
    original = evidence.evaluate(original)
    checkpoints = [(original, evidence.copy())]
    level_two = level_two_challenger(evidence, original)
    checkpoints.append((level_two, evidence.copy()))
    evidence.chain(P3, [at(18), at(21)], chain="recovery-1")
    checkpoints.append((evidence.evaluate(level_two), evidence.copy()))
    evidence.chain(P3, [at(27), at(30)], chain="recovery-2")
    checkpoints.append((evidence.evaluate(checkpoints[-1][0]), evidence.copy()))
    transition_evidence, transition_original = established()
    transition = h2_challenger(transition_evidence, transition_original)
    checkpoints.append((transition, transition_evidence.copy()))
    for decision, source in checkpoints:
        current = decision
        for _ in range(3):
            again = source.evaluate(current)
            assert again == current
            assert again.decision_id == current.decision_id
            current = again
    states = [item.state for item in trajectory()[1]]
    assert State.TRANSITION_CONFIRMED in states


def test_18_persistence_and_restart_preserve_recovery_progress_and_outcome():
    _evidence, in_memory = trajectory()
    _evidence, reloaded = trajectory(persisted)
    assert [item.decision_id for item in reloaded] == [
        item.decision_id for item in in_memory
    ]
    assert [item.state for item in in_memory] == [
        State.ESTABLISHED,
        State.CHANGE_SUSPECTED,
        State.CHALLENGER_ACCUMULATING,
        State.CHALLENGER_ACCUMULATING,
        State.CHALLENGER_ACCUMULATING,
        State.ESTABLISHED,
        State.TRANSITION_CONFIRMED,
        State.TRANSITION_CONFIRMED,
        State.ESTABLISHED,
    ]
    for decision in in_memory:
        restored_decision = persisted(decision)
        assert restored_decision.decision_id == decision.decision_id
        assert restored_decision.state is decision.state
        assert restored_decision.challenger_contexts == decision.challenger_contexts


# --------------------------------------------------------------------------
# 19-20: existing learning semantics are unchanged
# --------------------------------------------------------------------------


def test_19_harmonic_multiple_at_incumbent_phase_does_not_suspend():
    evidence, original = established()
    evidence.chain(2 * P3, [at(3), at(9)], chain="harmonic-a")
    evidence.chain(2 * P3, [at(15), at(21)], chain="harmonic-b")
    value = evidence.evaluate(original)
    assert restored(value, original)
    six = max(
        (item for item in value.candidates if item.candidate_period_seconds == 2 * P3),
        key=lambda item: item.high_authority,
    )
    assert not six.h2_gate
    assert six.harmonic_blockers


def test_20_sample_retention_does_not_change_authority_trajectory():
    _evidence, plain = trajectory()
    sample = detection.ObservationSample(
        wall_at=BASE,
        monotonic_at=1.0,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key=SERVER,
        info_status=detection.InfoStatus.HEALTHY,
        info_latency=0.04,
        player_status=detection.FieldStatus.PRESENT,
        players=12,
        max_players=60,
        queue_status=detection.FieldStatus.PRESENT,
        queue=0,
    )
    evidence, _decisions = trajectory()
    sampled = evidence.copy()
    sampled.events = [replace(item, samples=(sample,)) for item in evidence.events]
    stripped = evidence.copy()
    stripped.events = [replace(item, samples=()) for item in evidence.events]
    assert sampled.evaluate(plain[-2]) == stripped.evaluate(plain[-2])
    assert stripped.evaluate(plain[-2]).decision_id == plain[-1].decision_id
