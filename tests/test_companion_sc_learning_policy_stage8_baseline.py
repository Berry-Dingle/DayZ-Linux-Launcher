"""Stage 8.0: learning-policy test baseline (synthetic evidence only).

This module is a *baseline*, not an implementation.  It drives in-memory
synthetic restart evidence through the real production pipeline:

    physical events + explicit provenance coverage
      -> schema-3 ``RestartScheduleScorer`` (INACTIVITY_NEUTRAL, as runtime)
      -> schema-4 continuity spans, relationships, chains and streaks
      -> ``evaluate_authority`` (with an optional persisted prior decision)
      -> schema-4 authority consumer, with the same normal-scorer evidence
         mapping that ``Phase2RestartRuntime`` builds

No runtime object, profile, learning file or network is touched.

Status of every test is explicit:

* Unmarked tests describe behaviour that already holds and must keep
  holding through every Stage 8 sub-stage.
* ``pytest.mark.xfail(strict=True, raises=AssertionError)`` tests describe
  approved Stage 8 policy that does not hold yet.  Each names the planned
  implementation stage (for example ``STAGE_8_7``).  ``strict=True`` makes an
  unexpected pass fail, so the marker must be removed in the stage that
  implements the behaviour.  ``raises=AssertionError`` keeps harness errors
  visible as real failures.

Scenario numbers (S01 to S19) follow the Stage 8.0 task brief.  Cases that
are already covered by existing suites are not duplicated here; the Stage
8.0 report maps them to those tests.

Stage 8.1 (deterministic two-interval establishment, LIKELY guard and
NORMAL -> CONTINUITY origin upgrade) converted S01 (establishment), S08 and
both S09 cases into ordinary tests and added the ``test_s81_*`` section.
Stage 8.2 (continuous confidence milestones: streak-bonus magnitude 0.10,
H1-H4 gates kept on their 0.08 calibration) converted the S01 warning and S02
95 % tests and added the ``test_s82_*`` section.
Stage 8.3 (long-horizon per-event sparse input, ceiling unchanged) added the
``test_s83_*`` section.  Stage 8.3a (scheduled warning follows the displayed
80 % milestone, internal >= 0.795) converted the realistic-jitter warning test.
Stage 8.5 (sparse establishment gate, sparse ceiling 0.84 and strong
confirmation by a full interval) converted the four Stage 8.5 expected
failures and added the ``test_s85_*`` section.
Stage 8.6 (QV queue/one-player recovery after a sustained zero; unconfirmed
drains neutral in schema-3 hints and schema-4 relationship endpoints)
converted the last three expected failures and added the ``test_s86_*``
section.  No expected failures remain.
Stage 8.7 (recoverable isolated off-phase anomaly in the sparse consistency
domain; two-interval establishment held provisional against substantial
conflicting sparse evidence until one more independent confirmation; every
non-authentic outcome excluded as a relationship endpoint, including legacy
persisted relationships) added the ``test_s87_*`` section.
Stage 8.7a (proportional anomaly tolerance: off-phase restarts netted
against aligned ones within the proportion the unchanged SE3 phase gate
accepts; an isolated admin restart keeps safe H2/sparse countdowns) replaced
the Stage 8.7 single-anomaly tests with the ``test_s87a_*`` section and
updated three Stage 8.3 tests that encoded the ratio-only magnitude.
Stage 8.4 (unified confidence presentation: Learning shows the evaluated
schema-4 candidate's capped ``combined_confidence``; acquisition LIKELY is
presented as Learning) added the ``test_s84_*`` section and updated the one
LIKELY presentation assertion that encoded the old named-cycle wording.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumers as consumers3
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher.server_companion_ui import (
    companion_display_confidence_summary,
    restart_learning_presentation,
)


SERVER = "stage8-baseline.example:2302"
HOUR = scoring.HOUR
DAY = scoring.DAY
P3 = 3 * HOUR
# A UTC midnight, so ``at(day, hour)`` reads as a wall-clock slot.  Phase is
# epoch modulo period (no timezone model), which every candidate period
# divides evenly from midnight.
DAY0 = 1_900_022_400.0
assert DAY0 % DAY == 0

State = authority.RegimeState
Gate = authority.AuthorityGate
Origin = authority.AuthorityOrigin
Outcome = detection.EventOutcome
Presentation = consumers4.AuthorityPresentationKey

# The user-facing ceiling (``server_companion_ui`` caps at 95 %).
DISPLAY_CEILING_PERCENT = 95


def at(day, hour, minute=0):
    return DAY0 + day * DAY + hour * HOUR + minute * 60


def expected_failure(stage, why):
    return pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=f"Intended Stage 8 policy, not implemented: {why} -> {stage}",
    )


# --------------------------------------------------------------------------
# Synthetic evidence
# --------------------------------------------------------------------------


_AUTHENTICITY = {
    Outcome.CORROBORATED_OFFLINE_RESTART: 0.97,
    Outcome.CONFIRMED_OFFLINE_RESTART: 0.85,
    Outcome.STRONG_QUERY_VISIBLE_RESTART: 0.90,
    Outcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.60,
    Outcome.AMBIGUOUS_DRAIN: 0.25,
}
_DIRECT_OUTAGE = {
    Outcome.CORROBORATED_OFFLINE_RESTART,
    Outcome.CONFIRMED_OFFLINE_RESTART,
}


@dataclass(frozen=True)
class Session:
    """One continuously observed monitoring session (one continuity chain)."""

    name: str
    start: float
    end: float

    @property
    def app(self):
        return f"app-{self.name}"

    @property
    def monitor(self):
        return f"monitor-{self.name}"

    @property
    def poll(self):
        return 1

    @property
    def chain(self):
        return continuity.deterministic_continuity_chain_id(
            SERVER, self.app, self.monitor, self.poll, self.start
        )

    def coverage(self):
        return scoring.CoverageSegment(
            start_at=self.start,
            end_at=self.end,
            kind=scoring.CoverageKind.ONLINE_HEALTHY,
            cadence=10.0,
            server_key=SERVER,
            app_session_id=self.app,
            monitoring_session_id=self.monitor,
            poll_generation=self.poll,
            continuity_chain_id=self.chain,
            provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
        )


def physical_event(sequence, when, session, *, outcome):
    authenticity = _AUTHENTICITY[outcome]
    direct = outcome in _DIRECT_OUTAGE
    query_visible = outcome in {
        Outcome.STRONG_QUERY_VISIBLE_RESTART,
        Outcome.PROBABLE_QUERY_VISIBLE_RESTART,
        Outcome.AMBIGUOUS_DRAIN,
    }
    identity = f"s8-{sequence}-{int(when)}"
    sources = (
        {detection.SignalSource.INFO_OUTAGE, detection.SignalSource.INFO_RETURN}
        if direct
        else {detection.SignalSource.VISIBLE_LOW, detection.SignalSource.PLAYER_RECOVERY}
    )
    return detection.PhysicalRestartEvent(
        event_id=identity,
        sequence=sequence,
        fingerprint=f"fingerprint-{identity}",
        server_key=SERVER,
        episode_started_at=when - 180,
        finalized_at=when + 60,
        canonical_phase_at=when,
        phase_uncertainty=3,
        sources=frozenset(sources),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=(0.15 if outcome is Outcome.AMBIGUOUS_DRAIN else authenticity),
        drain=detection.DrainSummary(
            12 if query_visible else None,
            when - 120 if query_visible else None,
            when - 60 if query_visible else None,
            when if query_visible else None,
            query_visible,
            0 if query_visible else None,
            1.0 if query_visible else None,
            6 if query_visible else 0,
            60.0 if query_visible else 0.0,
            False,
        ),
        outage=detection.OutageSummary(
            when - 70 if direct else None,
            when - 60 if direct else None,
            10 if direct else 0,
            (detection.InfoStatus.TIMEOUT,) if direct else (),
            when if direct else None,
        ),
        recovery=detection.RecoverySummary(
            None,
            None if outcome is Outcome.AMBIGUOUS_DRAIN else when + 30,
            None if outcome is Outcome.AMBIGUOUS_DRAIN else when + 40,
            0 if outcome is Outcome.AMBIGUOUS_DRAIN else 2,
            False,
        ),
        query_health=detection.QueryHealthSummary(20, 3 if direct else 0, 0, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("stage8_baseline_fixture",),
        app_session_id=session.app,
        monitoring_session_id=session.monitor,
        poll_generation=session.poll,
        continuity_chain_id=session.chain,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


@dataclass
class Step:
    """One pipeline evaluation."""

    now: float
    score: scoring.ScheduleScore
    relationships: tuple
    streaks: tuple
    decision: authority.AuthorityDecision
    events: tuple

    def candidate(self, period, phase=None):
        values = [
            item
            for item in self.decision.candidates
            if item.candidate_period_seconds == period
        ]
        if not values:
            return None
        if phase is None:
            return max(values, key=lambda item: item.high_authority)
        return min(
            values,
            key=lambda item: abs(
                ((item.phase_offset - phase + period / 2) % period) - period / 2
            ),
        )

    def high_direct(self, period):
        return [
            item
            for item in self.relationships
            if item.candidate_period_seconds == period
            and item.relationship_kind is continuity.RelationshipKind.DIRECT
            and item.high_authority_eligible
        ]

    def consumer(self, *, now=None, **kwargs):
        return consume(self, now=self.now if now is None else now, **kwargs)


@dataclass
class Pipeline:
    """Accumulating synthetic evidence for one server, evaluated end to end."""

    events: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    sequence: int = 1
    # Runtime keeps the schema-3 selection sticky between evaluations
    # (``Phase2RestartRuntime._evaluate``); mirror that state here.
    previous_score: scoring.ScheduleScore | None = None
    incumbent_period: int | None = None

    def copy(self):
        return Pipeline(
            list(self.events),
            list(self.coverage),
            self.sequence,
            self.previous_score,
            self.incumbent_period,
        )

    def observe(self, name, times, *, outcome=Outcome.CORROBORATED_OFFLINE_RESTART, lead=20 * 60, tail=20 * 60):
        """One continuous monitoring session containing ``times`` restarts.

        Coverage runs from ``lead`` before the first restart to ``tail``
        after the last one, so every adjacent pair inside the session is a
        fully observed interval, and nothing outside it is observed.
        """

        times = list(times)
        session = Session(name, min(times) - lead, max(times) + tail)
        self.coverage.append(session.coverage())
        values = []
        for when in times:
            values.append(physical_event(self.sequence, when, session, outcome=outcome))
            self.sequence += 1
        self.events.extend(values)
        return values

    def sparse(self, prefix, times, **kwargs):
        """Each restart seen in its own short, separate monitoring session."""

        return [
            value
            for index, when in enumerate(times)
            for value in self.observe(f"{prefix}-{index}", [when], **kwargs)
        ]

    def evaluate(self, prior=None, *, now=None, ledger=()):
        events = tuple(sorted(self.events, key=lambda item: item.canonical_phase_at))
        now = (max(item.finalized_at for item in events) + 60) if now is None else now
        score = scoring.RestartScheduleScorer().score(
            events,
            scoring.CoverageTimeline(tuple(self.coverage)),
            now=now,
            phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
            previous=self.previous_score,
            incumbent_period_seconds=self.incumbent_period,
        )
        self.previous_score = score
        self.incumbent_period = score.selected_period_seconds
        spans = windows.continuity_spans_from_coverage(self.coverage, server_key=SERVER)
        chains = continuity.build_continuity_chains(spans)
        relationships = continuity.extract_interval_relationships(events, spans)
        streaks = continuity.build_cadence_streaks(relationships, chains=chains)
        decision = authority.evaluate_authority(
            server_key=SERVER,
            normal_score=score,
            relationships=relationships,
            streaks=streaks,
            expected_window_ledger=ledger,
            events=events,
            prior_decision=prior,
        )
        return Step(now, score, relationships, streaks, decision, events)


def schema3_consumer(step, *, now):
    """The schema-3 consumer decision, its selected candidate and prediction."""

    score = step.score
    selected = (
        score.candidate(score.selected_period_seconds)
        if score.selected_period_seconds is not None
        else None
    )
    predicted = None
    if selected is not None and selected.phase_offset is not None:
        period = float(selected.period_seconds)
        value = now - ((now - selected.phase_offset) % period) + period
        predicted = value if math.isfinite(value) and value > now else None
    schema3 = consumers3.evaluate_consumers(
        consumers3.ConsumerPolicyInput(
            score=score,
            server_key=SERVER,
            regime_generation="stage8-baseline",
            now=now,
            independent_authentic_event_count=sum(
                scoring.event_learning_eligible(item)
                and item.authenticity >= 0.50
                and item.outcome
                not in {
                    Outcome.AMBIGUOUS_DRAIN,
                    Outcome.UNCERTAIN_A2S_INTERRUPTION,
                    Outcome.INCOMPLETE,
                    Outcome.EXPIRED,
                    Outcome.SERVICE_INTERRUPTION,
                }
                for item in step.events
            ),
            predicted_restart_at=predicted,
            recent_covered_confirmation_count=(
                selected.strict_direct_interval_count if selected is not None else 0
            ),
        )
    )
    return schema3, selected, predicted


def consume(step, *, now, restart_alert_enabled=True, fired_keys=frozenset(), physical_recovery_event_id=None):
    """The schema-4 consumer with runtime-equivalent normal-scorer inputs.

    Mirrors ``Phase2RestartRuntime._evaluate_authority_consumer_shadow`` and
    the schema-3 consumer input it is derived from.
    """

    score = step.score
    schema3, selected, predicted = schema3_consumer(step, now=now)
    normal = None
    if selected is not None:
        unresolved_divisor = bool(
            scoring.GATES[selected.period_seconds].require_divisors
            and (
                not selected.divisor_resolution
                or not all(item.established_resolved for item in selected.divisor_resolution)
            )
        )
        normal = consumers4.NormalPredictionEvidence(
            selected_period_seconds=score.selected_period_seconds,
            incumbent_period_seconds=score.incumbent_period_seconds,
            schedule_existence_confidence=score.schedule_existence_confidence,
            fundamental_period_confidence=selected.fundamental_period_confidence,
            phase_confidence=selected.phase_confidence,
            candidate_established=selected.establishment_gates_passed,
            strict_direct_relationship_count=selected.strict_direct_interval_count,
            unresolved_competitor=bool(score.unresolved_competitors),
            unresolved_divisor_or_harmonic=unresolved_divisor,
            regime_stable=score.regime_status is scoring.RegimeStatus.STABLE,
            latest_aligned_phase_at=selected.recent_phase_observation_at,
            raw_predicted_occurrence_at=predicted,
            recent_strong_anomaly=bool(
                selected.strong_covered_contradiction
                or consumers3.BlockReason.RECENT_STRONG_ANOMALY
                in schema3.prediction_reasons
            ),
        )
    return consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=step.decision,
            now=now,
            restart_alert_enabled=restart_alert_enabled,
            physical_recovery_event_id=physical_recovery_event_id,
            physical_recovery_eligible=physical_recovery_event_id is not None,
            fired_keys=fired_keys,
            normal_pattern_supported=(
                schema3.model_status is not consumers3.ConsumerModelStatus.NO_PATTERN
            ),
            normal_pattern_confidence=schema3.schedule_existence_confidence,
            normal_prediction_evidence=normal,
        )
    )


def ui(step, consumer, now):
    """The user-facing panel presentation.

    Mirrors the window summary path: the schema-4 cutover summary (with its
    cycle period), the schema-3 learning-candidate projection for learning
    states, ``companion_display_confidence_summary`` and the UI ceiling.
    """

    summary = {
        **consumers4.authority_consumer_summary(consumer, now=now),
        "cycle_period_seconds": consumer.cycle_period_seconds,
    }
    period = summary["cycle_period_seconds"]
    learning = period is None and (
        summary.get("presentation_key") in {"none", "pattern_only"}
        or summary.get("cycle_text") in {"Still Learning", "Learning Restart Pattern"}
    )
    candidate = (
        runtime.presentation_period_candidate(step.score, period_seconds=None)
        if learning
        else None
    )
    return restart_learning_presentation(
        companion_display_confidence_summary(summary, candidate, period_seconds=period)
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


def established(step):
    """A user-facing established schedule: countdown on a learned cycle."""

    value = step.consumer()
    return bool(
        value.presentation_state
        in {Presentation.CONFIRMED_CYCLE, Presentation.CONFIRMED_NEW_CYCLE}
        and value.countdown_visible
        and value.countdown_safe
    )


def percent(step):
    value = step.consumer()
    presentation = ui(step, value, step.now)
    if not presentation.get("confidence_visible"):
        return None
    return presentation["confidence_percent"]


# --------------------------------------------------------------------------
# Shared evidence shapes
# --------------------------------------------------------------------------


def three_hour_chain(count, *, day=0, start_hour=9, name="continuous", outcome=Outcome.CORROBORATED_OFFLINE_RESTART):
    pipeline = Pipeline()
    pipeline.observe(
        name,
        [at(day, start_hour) + index * P3 for index in range(count)],
        outcome=outcome,
    )
    return pipeline


# Eight sparse 3 h-grid restarts, each in its own session, all more than
# 8 periods (24 h) apart except the last pair, all inside the schema-3 7-day
# horizon.  Slots 06/09/12/15/18/21/03 cover odd and even 3 h steps, so they
# refute 6, 12, 4, 8 and 2 h.  15:00 repeats on a different day.
SPARSE_DISCRIMINATING = (
    at(0, 6),
    at(1, 9),
    at(2, 12),
    at(3, 15),
    at(4, 18),
    at(5, 21),
    at(6, 3),
    at(6, 15),
)
# Eight observations at the same clock time: consistent with every
# candidate period, so the period is unresolved.
SAME_CLOCK = tuple(at(day, 9) for day in range(8))


def sparse_pipeline(times=SPARSE_DISCRIMINATING, *, outcome=Outcome.CORROBORATED_OFFLINE_RESTART):
    pipeline = Pipeline()
    pipeline.sparse("sparse", times, outcome=outcome)
    return pipeline


def settled(pipeline):
    """Evaluate, then rebuild once with the persisted prior (runtime-like)."""

    first = pipeline.evaluate()
    return pipeline.evaluate(persisted(first.decision))


# --------------------------------------------------------------------------
# S01-S03: continuous evidence
# --------------------------------------------------------------------------


def test_s01_precondition_two_consecutive_intervals_are_one_high_eligible_streak():
    """Existing: 09->12->15 is two high-eligible DIRECT intervals, one streak, H2."""

    step = three_hour_chain(3).evaluate()
    direct = step.high_direct(P3)
    candidate = step.candidate(P3)

    assert len(direct) == 2
    assert len({item.continuity_chain_id for item in direct}) == 1
    assert [(item.candidate_period_seconds, item.interval_count) for item in step.streaks] == [(P3, 2)]
    assert candidate.h2_gate and not candidate.h3_gate
    assert candidate.high_authority == 0.80
    assert candidate.phase_quality == 1.0
    assert not candidate.harmonic_blockers


def test_s01_two_consecutive_intervals_establish_immediately_at_about_80_percent():
    step = three_hour_chain(3).evaluate()
    value = step.consumer()
    shown = percent(step)

    assert step.decision.state is State.ESTABLISHED, step.decision.state
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE, value.presentation_state
    assert value.countdown_visible and value.countdown_safe
    assert value.cycle_period_seconds == P3
    assert shown is not None and 80 <= shown <= 85, shown


def test_s01_establishment_immediately_enables_scheduled_warning():
    step = three_hour_chain(3).evaluate()
    occurrence = step.consumer().next_expected_restart_at
    assert occurrence is not None, "no countdown at establishment"
    due = step.consumer(now=occurrence - 300)

    assert due.scheduled_warning_eligible
    assert due.scheduled_pre_restart_alert_eligible


def test_s02_three_consecutive_intervals_establish_with_countdown_and_warning():
    """Existing: a third consecutive interval (18:00) is H3 ESTABLISHED at once."""

    step = three_hour_chain(4).evaluate()
    value = step.consumer()
    occurrence = value.next_expected_restart_at
    due = step.consumer(now=occurrence - 300)

    assert step.decision.state is State.ESTABLISHED
    assert step.candidate(P3).h3_gate
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert due.scheduled_warning_eligible and due.scheduled_pre_restart_alert_eligible
    assert percent(step) >= 90


def test_s02_three_consecutive_intervals_reach_95_percent_ceiling():
    step = three_hour_chain(4).evaluate()

    assert percent(step) == DISPLAY_CEILING_PERCENT, percent(step)


def test_s03_two_independent_complete_intervals_after_establishment_reach_95_percent():
    """Existing: two more fully observed intervals, separate sessions and days,
    repeating earlier clock slots, raise an established cycle to the ceiling."""

    pipeline = three_hour_chain(3)
    established_step = settled(pipeline)
    assert established(established_step)

    pipeline.observe("day-1", [at(1, 9), at(1, 12)])
    one_more = pipeline.evaluate(established_step.decision)
    pipeline.observe("day-2", [at(2, 21), at(3, 0)])
    two_more = pipeline.evaluate(one_more.decision)

    assert len(two_more.high_direct(P3)) == 4
    assert len({item.continuity_chain_id for item in two_more.high_direct(P3)}) == 3
    assert established(two_more)
    assert percent(two_more) == DISPLAY_CEILING_PERCENT
    assert (
        two_more.decision.incumbent_shadow_regime.regime_id
        == established_step.decision.incumbent_shadow_regime.regime_id
    )


# --------------------------------------------------------------------------
# S04-S07: sparse evidence
# --------------------------------------------------------------------------


def test_s04_precondition_sparse_events_are_independent_and_discriminating():
    """Existing: eight separate-session events, no complete interval, and the
    phase evidence singles out 3 h (unlike the same-clock set)."""

    step = sparse_pipeline().evaluate()
    three = step.score.candidate(P3)

    assert len({item.event_id for item in step.events}) == 8
    assert len({item.monitoring_session_id for item in step.events}) == 8
    assert all(scoring.event_learning_eligible(item) for item in step.events)
    assert not any(item.high_authority_eligible for item in step.relationships)
    assert three.strict_direct_interval_count == 0
    assert three.aligned_event_count == 8
    assert all(
        three.phase_confidence > step.score.candidate(period).phase_confidence
        for period in scoring.CANDIDATE_PERIODS
        if period != P3
    )


def test_s04_eight_discriminating_sparse_events_establish_at_80_percent():
    step = settled(sparse_pipeline())
    value = step.consumer()
    shown = percent(step)

    assert established(step), (step.decision.state, value.presentation_state)
    assert value.cycle_period_seconds == P3
    assert shown is not None and 80 <= shown < DISPLAY_CEILING_PERCENT, shown


def _sparse_plus_matching_interval():
    pipeline = sparse_pipeline()
    sparse_step = settled(pipeline)
    pipeline.observe("complete", [at(6, 18), at(6, 21)])
    return sparse_step, pipeline.evaluate(sparse_step.decision)


def test_s05_precondition_matching_interval_is_high_eligible_at_sparse_phase():
    """Existing: the added interval is one high-eligible DIRECT 3 h interval
    whose phase matches the sparse events."""

    _sparse_step, step = _sparse_plus_matching_interval()
    direct = step.high_direct(P3)
    phase = direct[0].left_at % P3

    assert len(direct) == 1
    assert all(
        min(abs((item.canonical_phase_at - phase) % P3), P3 - abs((item.canonical_phase_at - phase) % P3)) <= 1
        for item in step.events
    )
    assert step.candidate(P3).h1_gate


def test_s05_sparse_establishment_plus_one_complete_interval_reaches_95_percent():
    _sparse_step, step = _sparse_plus_matching_interval()

    assert established(step), step.decision.state
    assert percent(step) == DISPLAY_CEILING_PERCENT, percent(step)


def test_s06_repeated_clock_slots_on_different_days_are_new_physical_events():
    """Existing: 21:00 and 00:00 repeated on later days are new evidence."""

    first_days = [at(0, 21), at(2, 0), at(3, 21), at(5, 0)]
    repeats = [at(1, 21), at(3, 0), at(4, 21), at(6, 0)]
    pipeline = sparse_pipeline(first_days)
    before = pipeline.evaluate()
    pipeline.sparse("repeat", repeats)
    after = pipeline.evaluate(before.decision)

    assert len({item.event_id for item in after.events}) == 8
    assert before.score.candidate(P3).aligned_event_count == 4
    assert after.score.candidate(P3).aligned_event_count == 8
    assert after.score.schedule_existence_confidence > before.score.schedule_existence_confidence


def test_s06_same_physical_restart_delivered_twice_counts_once():
    """Existing: one physical restart is one event in both schemas."""

    once = three_hour_chain(3)
    twice = once.copy()
    twice.events.extend(once.events)
    single = once.evaluate()
    double = twice.evaluate()

    assert double.decision == single.decision
    assert double.score == single.score
    assert double.relationships == single.relationships


def test_s07_same_clock_observations_never_establish_or_name_a_period():
    """Guard (holds now, must keep holding after Stage 8.5): identical clock
    times alone never resolve the period."""

    step = settled(sparse_pipeline(SAME_CLOCK))
    value = step.consumer()
    shown = ui(step, value, step.now)

    phase_values = {
        step.score.candidate(period).phase_confidence for period in scoring.CANDIDATE_PERIODS
    }
    assert len(phase_values) == 1  # the evidence cannot discriminate periods
    assert step.decision.state in {State.NORMAL_LEARNING, State.UNKNOWN}
    assert step.decision.selected_shadow_regime is None
    assert value.cycle_period_seconds is None
    assert not value.countdown_visible and not value.scheduled_warning_eligible
    assert shown["cycle_text"] == "Learning Restart Pattern"


# --------------------------------------------------------------------------
# S08-S09: origin lock and rebuild-driven promotion
# --------------------------------------------------------------------------


def _sparse_first_then_continuous():
    """Schema-3 selects 3 h from query-visible restarts before any H2; later
    continuous offline restarts on the same 3 h phase reach H3."""

    pipeline = three_hour_chain(5, name="qv-first", outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    normal_step = settled(pipeline)
    pipeline.observe("continuous", [at(1, 9) + index * P3 for index in range(4)])
    return normal_step, pipeline.evaluate(normal_step.decision)


def test_s08_precondition_normal_origin_incumbent_and_h3_continuous_candidate():
    normal_step, step = _sparse_first_then_continuous()
    incumbent = normal_step.decision.incumbent_shadow_regime
    candidate = step.candidate(P3, incumbent.phase_offset)

    assert incumbent.origin is Origin.NORMAL
    assert incumbent.candidate_period_seconds == P3
    assert candidate.h3_gate and candidate.high_phase_authority >= 0.85
    assert not candidate.harmonic_blockers
    assert step.decision.incumbent_shadow_regime.regime_id == incumbent.regime_id


def test_s08_continuous_evidence_upgrades_sparse_first_normal_regime():
    _normal_step, step = _sparse_first_then_continuous()
    regime = step.decision.selected_shadow_regime
    value = step.consumer()

    assert regime.origin is Origin.CONTINUITY, regime.origin
    assert regime.authority_gate in {Gate.H3, Gate.H4}, regime.authority_gate
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE, value.presentation_state
    assert "safe_h3_prediction_available" in value.reason_codes


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: three_hour_chain(3), id="h2_continuity_likely"),
        pytest.param(
            lambda: three_hour_chain(5, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
            id="normal_origin_likely",
        ),
    ],
)
def test_s09_likely_state_does_not_become_established_on_evidence_free_rebuild(build):
    pipeline = build()
    first = pipeline.evaluate()
    rebuilt = pipeline.evaluate(persisted(first.decision))

    assert rebuilt.decision.state is first.decision.state, (
        first.decision.state,
        rebuilt.decision.state,
    )
    assert (
        rebuilt.decision.selected_shadow_regime.status
        is first.decision.selected_shadow_regime.status
    )
    assert rebuilt.consumer().presentation_state is first.consumer().presentation_state


# --------------------------------------------------------------------------
# S10-S14: query-visible (QV) evidence
#
# S10 (drain -> zero -> player return), S12 detection/alert (no return:
# AMBIGUOUS_DRAIN, no alert) and S13 (polling gap / local outage) are covered
# by existing suites; see the Stage 8.0 report.
# --------------------------------------------------------------------------


def _qv_sample(offset, players, *, queue=None):
    return detection.ObservationSample(
        wall_at=at(0, 9) + offset,
        monotonic_at=float(offset),
        app_session_id="app-qv",
        monitoring_session_id="monitor-qv",
        poll_generation=1,
        server_key=SERVER,
        info_status=detection.InfoStatus.HEALTHY,
        player_status=detection.FieldStatus.PRESENT,
        players=players,
        max_players=60,
        queue_status=(
            detection.FieldStatus.PRESENT if queue is not None else detection.FieldStatus.MISSING
        ),
        queue=queue,
        continuity_chain_id="chain-qv",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _qv_drain_to_sustained_zero():
    return [
        _qv_sample(0, 12),
        _qv_sample(10, 12),
        _qv_sample(20, 12),
        _qv_sample(40, 11),
        _qv_sample(60, 9),
        _qv_sample(80, 7),
        _qv_sample(100, 5),
        _qv_sample(120, 3),
        *[_qv_sample(offset, 0) for offset in range(140, 201, 10)],
    ]


def test_s11_drain_zero_then_queue_return_is_confirmed_restart():
    engine = detection.PhysicalEpisodeEngine()
    events = []
    for value in [
        *_qv_drain_to_sustained_zero(),
        *[_qv_sample(offset, 0, queue=2) for offset in range(210, 741, 10)],
    ]:
        events.extend(engine.ingest(value))

    assert len(events) == 1
    event = events[0]
    assert event.outcome in {
        Outcome.STRONG_QUERY_VISIBLE_RESTART,
        Outcome.PROBABLE_QUERY_VISIBLE_RESTART,
    }, event.outcome
    assert event.recovery.first_queue_at is not None
    assert scoring.event_learning_eligible(event)


def test_s12_unconfirmed_drains_alone_create_no_schedule_or_presentation():
    """Existing: AMBIGUOUS_DRAIN events are never authentic schedule evidence."""

    pipeline = Pipeline()
    pipeline.sparse("drain", SPARSE_DISCRIMINATING, outcome=Outcome.AMBIGUOUS_DRAIN)
    step = pipeline.evaluate()
    value = step.consumer()

    assert not any(item.high_authority_eligible for item in step.relationships)
    assert step.decision.state is State.UNKNOWN
    assert step.decision.candidates == ()
    assert step.score.schedule_existence_confidence == 0
    assert value.presentation_state is Presentation.NONE
    assert not value.countdown_visible


def test_s12_unconfirmed_drains_are_not_interval_relationship_endpoints():
    pipeline = Pipeline()
    pipeline.sparse("drain", SPARSE_DISCRIMINATING, outcome=Outcome.AMBIGUOUS_DRAIN)
    step = pipeline.evaluate()

    assert step.relationships == (), len(step.relationships)


def test_s12_unconfirmed_drains_contribute_no_learning():
    plain = three_hour_chain(3)
    with_drains = plain.copy()
    with_drains.sparse(
        "drain",
        [at(3, 9), at(3, 21), at(4, 12), at(5, 18)],
        outcome=Outcome.AMBIGUOUS_DRAIN,
    )
    before = plain.evaluate()
    after = with_drains.evaluate()

    for period in scoring.CANDIDATE_PERIODS:
        assert (
            after.score.candidate(period).fundamental_period_confidence
            == before.score.candidate(period).fundamental_period_confidence
        ), period
    assert after.score.schedule_existence_confidence == before.score.schedule_existence_confidence
    assert after.candidate(P3).combined_confidence == before.candidate(P3).combined_confidence
    assert scoring._hint_event_weight(with_drains.events[-1]) == 0


def _qv_trajectory(count=12):
    """Continuous strong-QV restarts; one schema-4 rebuild per new restart."""

    times = [at(0, 0) + index * P3 for index in range(count)]
    prior = None
    previous_score = None
    incumbent = None
    steps = []
    for size in range(2, count + 1):
        pipeline = Pipeline(previous_score=previous_score, incumbent_period=incumbent)
        pipeline.observe("qv", times[:size], outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
        step = pipeline.evaluate(prior)
        prior = persisted(step.decision)
        previous_score, incumbent = pipeline.previous_score, pipeline.incumbent_period
        steps.append(step)
    return steps


def test_s14_continuous_qv_eventually_reaches_95_percent_countdown():
    """Existing: continuous QV reaches a countdown at the ceiling via the
    normal-schedule path, and never gains high authority."""

    steps = _qv_trajectory()
    final = steps[-1]

    assert not any(item.high_authority_eligible for step in steps for item in step.relationships)
    assert final.consumer().countdown_visible
    assert percent(final) == DISPLAY_CEILING_PERCENT


def test_s14_continuous_qv_reaches_80_percent_milestone_before_95():
    observed = [
        (step.consumer().countdown_visible, percent(step)) for step in _qv_trajectory()
    ]

    assert any(
        countdown and shown is not None and 80 <= shown < DISPLAY_CEILING_PERCENT
        for countdown, shown in observed
    ), observed
    assert observed[-1] == (True, DISPLAY_CEILING_PERCENT)


# Ten sparse strong-QV restarts: QV keeps its stricter internal weight (0.8
# per restart), so it needs ten observations for the effective count of
# eight that saturates the sparse statistic, then shows the same 80 %.
SPARSE_QV_TEN = SPARSE_DISCRIMINATING + (at(7, 9), at(8, 12))


def test_s14_sparse_qv_establishes_at_80_percent():
    step = settled(sparse_pipeline(SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART))
    shown = percent(step)

    assert established(step), step.decision.state
    assert shown is not None and shown >= 80, shown


# --------------------------------------------------------------------------
# S15-S16: change protection
#
# Stage 6 recovery and Stage 7 presentation are covered by the existing
# ``test_companion_sc_cycle_change_recovery.py``,
# ``test_companion_restart_phase2_stage6.py`` and
# ``test_companion_sc_suspected_cycle_presentation.py`` suites.  This guard
# pins the sparse/challenger boundary that Stage 8.5 must not cross (D8-14).
# --------------------------------------------------------------------------


def test_s15_sparse_off_phase_evidence_never_escalates_against_established_cycle():
    pipeline = three_hour_chain(4)
    original = settled(pipeline)
    pipeline.sparse(
        "off-phase",
        [at(1, 10, 30), at(2, 14, 30), at(3, 18, 30), at(4, 22, 30), at(5, 2, 30), at(5, 14, 30)],
    )
    step = pipeline.evaluate(original.decision)
    value = step.consumer()

    assert step.decision.suspicion_level <= 1
    assert all(
        item.authorization_state is authority.ChallengerAuthorization.LATENT
        and not item.high_relationship_ids
        for item in step.decision.challenger_contexts
    )
    assert (
        step.decision.incumbent_shadow_regime.regime_id
        == original.decision.incumbent_shadow_regime.regime_id
    )
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe


# --------------------------------------------------------------------------
# S17-S19: identity, alert de-duplication and alert independence
# --------------------------------------------------------------------------


STATE_BUILDERS = {
    "continuity_h3": lambda: three_hour_chain(4),
    "sparse_learning": lambda: sparse_pipeline(),
    "normal_origin_qv": lambda: three_hour_chain(10, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
}


@pytest.mark.parametrize("name", sorted(STATE_BUILDERS))
def test_s17_rebuild_and_reopen_with_unchanged_evidence_are_stable(name):
    pipeline = STATE_BUILDERS[name]()
    first = pipeline.evaluate()
    second = pipeline.evaluate(persisted(first.decision))
    third = pipeline.evaluate(persisted(second.decision))
    fourth = pipeline.evaluate(third.decision)

    assert second.decision.state is first.decision.state
    regimes = [
        item.decision.selected_shadow_regime for item in (first, second, third, fourth)
    ]
    assert len({None if item is None else item.regime_id for item in regimes}) == 1
    # Unknown-server first decisions settle once into the incumbent path
    # (pre-existing); from the first rebuild on, rebuilding is a fixed point.
    assert third.decision == second.decision
    assert fourth.decision == second.decision
    assert third.consumer().decision_id == second.consumer().decision_id


def test_s18_scheduled_warning_is_at_most_once_per_predicted_occurrence():
    pipeline = three_hour_chain(4)
    step = settled(pipeline)
    occurrence = step.consumer().next_expected_restart_at
    due = occurrence - 300
    first = step.consumer(now=due)
    fired = frozenset({first.scheduled_warning_suppression_key})

    reopened = pipeline.evaluate(persisted(step.decision))
    pipeline.sparse("aligned", [at(1, 3)])
    more = pipeline.evaluate(reopened.decision)

    assert first.scheduled_pre_restart_alert_eligible
    for later in (step, reopened, more):
        value = later.consumer(now=due, fired_keys=fired)
        assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
        assert not value.scheduled_pre_restart_alert_eligible
    following = step.consumer(now=due + P3, fired_keys=fired)
    assert following.scheduled_warning_suppression_key != first.scheduled_warning_suppression_key
    assert following.scheduled_pre_restart_alert_eligible


GENERIC_RECOVERY_STATES = {
    "single_event": lambda: three_hour_chain(1),
    "h1_one_interval": lambda: three_hour_chain(2),
    "h2_likely": lambda: three_hour_chain(3),
    "sparse_learning": lambda: sparse_pipeline(),
    "same_clock": lambda: sparse_pipeline(SAME_CLOCK),
    "normal_origin_qv": lambda: three_hour_chain(10, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
    "continuity_h3": lambda: three_hour_chain(4),
}


@pytest.mark.parametrize("name", sorted(GENERIC_RECOVERY_STATES))
def test_s19_generic_online_alert_is_independent_of_schedule_confidence(name):
    step = settled(GENERIC_RECOVERY_STATES[name]())
    first = step.consumer(physical_recovery_event_id="physical-restart-1")
    again = step.consumer(
        physical_recovery_event_id="physical-restart-1",
        fired_keys=frozenset({first.generic_recovery_suppression_key}),
    )

    assert first.generic_recovery_alert_eligible
    assert first.generic_recovery_suppression_key.regime_id is None
    assert not again.generic_recovery_alert_eligible


# --------------------------------------------------------------------------
# Stage 8.1 focused tests: deterministic establishment and origin upgrade
# --------------------------------------------------------------------------


def genuine_miss(period, expected, *, key):
    tolerance = scoring.candidate_phase_tolerance(period)
    return windows.import_legacy_expected_miss(
        server_key=SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - tolerance,
        expected_at=expected,
        window_end_at=expected + tolerance,
        model_revision_id="stage8-model",
        legacy_key=key,
    )


def imprecise_h2():
    """H2 (existing gates pass) with a 50 s phase deviation: pq < 0.909."""

    pipeline = Pipeline()
    pipeline.observe("imprecise", [at(0, 9), at(0, 12) + 50, at(0, 15)])
    return pipeline


def rebuilds(pipeline, first, count=3, **kwargs):
    values = [first]
    for _ in range(count):
        values.append(pipeline.evaluate(persisted(values[-1].decision), **kwargs))
    return values


def test_s81_two_interval_establishment_sets_established_at_to_streak_end():
    step = three_hour_chain(3).evaluate()
    regime = step.decision.incumbent_shadow_regime
    (streak,) = step.streaks

    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert regime.status is authority.RegimeRecordStatus.ESTABLISHED
    assert regime.origin is Origin.CONTINUITY
    assert regime.authority_gate is Gate.H2
    assert regime.established_at == streak.end_at == at(0, 15)
    assert step.decision.combined_confidence == step.candidate(P3).combined_confidence


def test_s81_two_interval_regime_identity_is_stable_across_rebuilds():
    pipeline = three_hour_chain(3)
    values = rebuilds(pipeline, pipeline.evaluate())

    assert len({item.decision.incumbent_shadow_regime.regime_id for item in values}) == 1
    assert len({item.decision.incumbent_shadow_regime.established_at for item in values}) == 1
    assert all(item.decision.state is State.ESTABLISHED for item in values)
    # One settle into the incumbent path (pre-existing), then a fixed point.
    assert values[1].decision == values[2].decision == values[3].decision
    assert all(established(item) for item in values)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(imprecise_h2, id="imprecise_phase"),
        pytest.param(
            lambda: three_hour_chain(5, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
            id="normal_origin",
        ),
    ],
)
def test_s81_likely_is_a_fixed_point_of_identical_rebuilds(build):
    pipeline = build()
    first = pipeline.evaluate()
    values = rebuilds(pipeline, first)

    assert first.decision.state is State.LIKELY
    assert all(item.decision == first.decision for item in values)
    assert all(not item.consumer().countdown_visible for item in values)


def test_s81_imprecise_h2_stays_likely_without_countdown():
    step = imprecise_h2().evaluate()
    candidate = step.candidate(P3)
    value = step.consumer()

    assert candidate.h2_gate and not candidate.h3_gate
    assert candidate.phase_quality < authority.TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM
    assert step.decision.state is State.LIKELY
    # Stage 8.4: LIKELY is presented as Learning (no named period).
    assert value.presentation_state is Presentation.PATTERN_ONLY
    assert value.cycle_period_seconds is None
    assert not value.countdown_visible


def test_s81_harmonic_blocked_h2_stays_likely():
    """6 h H2 with its divisors H2-resolved (one healthy miss each) but not
    H3-resolved: the H3 harmonic blocker withholds two-interval establishment."""

    pipeline = Pipeline()
    pipeline.observe("six-hour", [at(0, 0), at(0, 6), at(0, 12)])
    ledger = (
        genuine_miss(3 * HOUR, at(1, 3), key="divisor-3h"),
        genuine_miss(2 * HOUR, at(1, 2), key="divisor-2h"),
    )
    step = pipeline.evaluate(ledger=ledger)
    candidate = step.candidate(6 * HOUR)

    assert candidate.h2_gate
    assert any(item.startswith("high_harmonic_divisor_unresolved_") for item in candidate.harmonic_blockers)
    assert step.decision.state is State.LIKELY
    again = pipeline.evaluate(persisted(step.decision), ledger=ledger)
    assert again.decision == step.decision


def test_s81_competing_h2_candidate_withholds_two_interval_establishment():
    pipeline = Pipeline()
    pipeline.observe("three-hour", [at(0, 9), at(0, 12), at(0, 15)])
    pipeline.observe("four-hour", [at(1, 1), at(1, 5), at(1, 9)])
    step = pipeline.evaluate()

    assert step.candidate(P3).h2_gate and step.candidate(4 * HOUR).h2_gate
    assert step.decision.state is State.LIKELY


def test_s81_active_genuine_miss_in_phase_withholds_two_interval_establishment():
    pipeline = three_hour_chain(3)
    ledger = (genuine_miss(P3, at(0, 21), key="in-phase-miss"),)
    step = pipeline.evaluate(ledger=ledger)

    assert step.candidate(P3).h2_gate
    assert step.decision.state is State.LIKELY


def test_s81_off_phase_restart_after_streak_withholds_two_interval_establishment():
    pipeline = three_hour_chain(3)
    pipeline.sparse("off-phase", [at(0, 19, 30)])
    step = pipeline.evaluate()

    assert step.candidate(P3).h2_gate
    assert step.decision.state is State.LIKELY


def test_s81_likely_established_by_later_evidence_keeps_identity_and_sets_established_at():
    pipeline = imprecise_h2()
    likely = pipeline.evaluate()
    pipeline.observe("later", [at(1, 9) + index * P3 for index in range(4)])
    step = pipeline.evaluate(persisted(likely.decision))
    regime = step.decision.incumbent_shadow_regime

    assert step.decision.state is State.ESTABLISHED
    assert regime.regime_id == likely.decision.incumbent_shadow_regime.regime_id
    assert regime.established_at == step.candidate(P3, regime.phase_offset).last_evidence_at
    assert established(step)


def test_s81_legacy_established_h2_incumbent_is_preserved():
    """A pre-8.1 rebuild-promoted H2 incumbent (ESTABLISHED, no
    ``established_at``) is not revoked by the LIKELY guard."""

    pipeline = imprecise_h2()
    likely = pipeline.evaluate()
    regime = replace(
        likely.decision.incumbent_shadow_regime,
        status=authority.RegimeRecordStatus.ESTABLISHED,
    )
    legacy = replace(
        likely.decision,
        state=State.ESTABLISHED,
        selected_shadow_regime=regime,
        incumbent_shadow_regime=regime,
        regimes=(regime,),
        prediction_usable=True,
        countdown_safe=True,
    )
    step = pipeline.evaluate(persisted(legacy))

    assert step.decision.state is State.ESTABLISHED
    assert step.decision.incumbent_shadow_regime.regime_id == regime.regime_id
    assert step.decision.incumbent_shadow_regime.established_at is None
    assert established(step)


def _normal_established_then_continuous():
    pipeline = three_hour_chain(10, name="qv", outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    normal = settled(pipeline)
    assert normal.decision.state is State.ESTABLISHED
    assert normal.decision.incumbent_shadow_regime.origin is Origin.NORMAL
    pipeline.observe("continuous", [at(2, 0) + index * P3 for index in range(4)])
    return pipeline, normal, pipeline.evaluate(persisted(normal.decision))


def test_s81_established_normal_regime_upgrades_with_identity_and_alert_key():
    pipeline, normal, upgraded = _normal_established_then_continuous()
    before = normal.decision.incumbent_shadow_regime
    after = upgraded.decision.incumbent_shadow_regime
    occurrence = upgraded.consumer().next_expected_restart_at
    due = occurrence - 300
    first = normal.consumer(now=due)
    fired = frozenset({first.scheduled_warning_suppression_key})
    value = upgraded.consumer(now=due, fired_keys=fired)

    assert after.regime_id == before.regime_id
    assert after.origin is Origin.CONTINUITY
    assert after.authority_gate in {Gate.H3, Gate.H4}
    assert after.established_at == before.established_at
    assert authority.ORIGIN_UPGRADE_REASON in upgraded.decision.reason_codes
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert "safe_h3_prediction_available" in value.reason_codes
    assert first.scheduled_pre_restart_alert_eligible
    assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
    assert not value.scheduled_pre_restart_alert_eligible
    # The upgrade is persistent and a fixed point of identical rebuilds.
    again = pipeline.evaluate(persisted(upgraded.decision))
    assert again.decision == upgraded.decision


def test_s81_established_normal_regime_does_not_upgrade_on_weak_h2():
    pipeline = three_hour_chain(10, name="qv", outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    normal = settled(pipeline)
    assert normal.decision.state is State.ESTABLISHED
    pipeline.observe("weak", [at(2, 0), at(2, 3) + 50, at(2, 6)])
    step = pipeline.evaluate(persisted(normal.decision))
    regime = step.decision.incumbent_shadow_regime

    assert step.candidate(P3, regime.phase_offset).h2_gate
    assert regime.origin is Origin.NORMAL
    assert regime.regime_id == normal.decision.incumbent_shadow_regime.regime_id
    assert authority.ORIGIN_UPGRADE_REASON not in step.decision.reason_codes


def test_s81_harmonically_blocked_candidate_never_upgrades_origin():
    step = three_hour_chain(4).evaluate()
    candidate = step.candidate(P3)
    kwargs = {
        "streaks": step.streaks,
        "relationship_map": {item.relationship_id: item for item in step.relationships},
        "event_map": {item.event_id: item for item in step.events},
    }

    assert authority._continuity_upgrade_qualifies(candidate, **kwargs)
    blocked = replace(candidate, harmonic_blockers=("high_harmonic_divisor_unresolved_3",))
    assert not authority._continuity_upgrade_qualifies(blocked, **kwargs)


def _boundary_pipeline():
    pipeline = Pipeline()
    # A 4 h interval the day before the 3 h cycle is acquired.
    pipeline.observe("old-four-hour", [at(-1, 10), at(-1, 14)])
    pipeline.observe("acquire", [at(0, 9), at(0, 12), at(0, 15)])
    return pipeline


def test_s81_pre_establishment_challenger_evidence_is_historical():
    pipeline = _boundary_pipeline()
    acquired = pipeline.evaluate()
    assert acquired.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert acquired.decision.incumbent_shadow_regime.established_at == at(0, 15)
    pipeline.observe("new-four-hour", [at(1, 10), at(1, 14)])
    step = pipeline.evaluate(persisted(acquired.decision))

    # Only the post-establishment interval counts: level 1, prediction kept
    # by authority.  (The consumer's continuity-H2 countdown path requires
    # suspicion 0, a pre-existing presentation rule outside Stage 8.1.)
    assert step.decision.suspicion_level == 1
    assert step.decision.prediction_usable and step.decision.countdown_safe
    # The same evidence against an incumbent without an establishment
    # boundary (the legacy shape) counts both intervals: level 2.
    legacy_regime = replace(acquired.decision.incumbent_shadow_regime, established_at=None)
    legacy = replace(
        acquired.decision,
        selected_shadow_regime=legacy_regime,
        incumbent_shadow_regime=legacy_regime,
        regimes=(legacy_regime,),
    )
    unbounded = pipeline.evaluate(persisted(legacy))
    assert unbounded.decision.suspicion_level == 2
    assert not unbounded.decision.countdown_safe
    assert not unbounded.consumer().countdown_visible


def test_s81_two_interval_rule_does_not_promote_challengers():
    """Initial acquisition only: a clean two-interval challenger against an
    established cycle stays a provisional transition (Stage 6 unchanged)."""

    pipeline = three_hour_chain(7)
    original = settled(pipeline)
    pipeline.observe("challenger", [at(2, 1), at(2, 5), at(2, 9)])
    step = pipeline.evaluate(persisted(original.decision))

    assert step.candidate(4 * HOUR).h2_gate and not step.candidate(4 * HOUR).h3_gate
    assert step.decision.state is State.TRANSITION_CONFIRMED
    assert (
        step.decision.incumbent_shadow_regime.regime_id
        == original.decision.incumbent_shadow_regime.regime_id
    )


# --------------------------------------------------------------------------
# Stage 8.2 focused tests: continuous confidence milestones
# --------------------------------------------------------------------------


def test_s82_clean_streak_milestone_magnitudes():
    two = three_hour_chain(3).evaluate()
    three = three_hour_chain(4).evaluate()

    assert two.candidate(P3).streak_bonus == 0.05
    assert two.candidate(P3).high_authority == 0.80
    assert two.decision.combined_confidence >= 0.80
    assert 80 <= percent(two) <= 85
    assert three.candidate(P3).streak_bonus == 0.075
    assert three.candidate(P3).high_authority == 0.95
    assert percent(three) == DISPLAY_CEILING_PERCENT


def test_s82_imperfect_establishing_streak_stays_below_warning_threshold():
    """A 48 s phase deviation still establishes (phase quality >= 0.909), but
    relationship quality below 1 keeps the genuine magnitude under the 0.80
    milestone: countdown yes, scheduled warning no (unchanged warning rule)."""

    pipeline = Pipeline()
    pipeline.observe("imperfect", [at(0, 9), at(0, 12) + 48, at(0, 15)])
    step = pipeline.evaluate()
    value = step.consumer()
    due = step.consumer(now=value.next_expected_restart_at - 300)

    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert step.candidate(P3).high_authority < 0.80
    assert step.decision.combined_confidence < 0.80
    assert value.countdown_visible
    assert not due.scheduled_warning_eligible


def _gate_scenarios():
    def chain(name, times):
        pipeline = Pipeline()
        pipeline.observe(name, times)
        return pipeline.evaluate()

    def challenger(times):
        pipeline = three_hour_chain(7)
        original = settled(pipeline)
        pipeline.observe("challenger", times)
        return pipeline.evaluate(persisted(original.decision))

    return {
        "clean_two": chain("a", [at(0, 9), at(0, 12), at(0, 15)]),
        "imprecise_two_50s": chain("a", [at(0, 9), at(0, 12) + 50, at(0, 15)]),
        "imprecise_two_60s": chain("a", [at(0, 9), at(0, 12) + 60, at(0, 15)]),
        "clean_three": chain("a", [at(0, 9) + index * P3 for index in range(4)]),
        "challenger_three_50s": challenger([at(2, 1), at(2, 5) + 50, at(2, 9), at(2, 13)]),
        "challenger_three_66s": challenger([at(2, 1), at(2, 5) + 66, at(2, 9), at(2, 13)]),
        "challenger_two": challenger([at(2, 1), at(2, 5), at(2, 9)]),
    }


def _gate_signature(step):
    regime = step.decision.incumbent_shadow_regime
    selected = step.decision.selected_shadow_regime
    return (
        step.decision.state,
        step.decision.suspicion_level,
        None if regime is None else regime.regime_id,
        None if selected is None else selected.regime_id,
        tuple(
            (item.candidate_period_seconds, item.phase_offset, item.h1_gate, item.h2_gate, item.h3_gate, item.h4_gate)
            for item in step.decision.candidates
        ),
    )


def test_s82_gates_states_and_regime_ids_are_independent_of_magnitude_coefficient(monkeypatch):
    """The raised milestone magnitude never moves an H1-H4 gate: every gate,
    state, suspicion level and regime ID equals the v1 (0.08) calibration."""

    current = {name: _gate_signature(step) for name, step in _gate_scenarios().items()}
    monkeypatch.setattr(
        authority, "STREAK_BONUS_COEFFICIENT", authority.GATE_STREAK_BONUS_COEFFICIENT
    )
    calibrated = {name: _gate_signature(step) for name, step in _gate_scenarios().items()}

    assert current == calibrated


def test_s82_imprecise_challenger_stays_provisional():
    """A three-interval challenger 66 s off phase stays provisional; with the
    raised magnitude used for gates it would have superseded the incumbent."""

    scenarios = _gate_scenarios()

    assert scenarios["challenger_three_66s"].decision.state is State.TRANSITION_CONFIRMED
    assert scenarios["challenger_three_50s"].decision.state is State.NEW_REGIME_ESTABLISHED
    assert scenarios["challenger_two"].decision.state is State.TRANSITION_CONFIRMED
    assert scenarios["imprecise_two_60s"].decision.state is State.NORMAL_LEARNING


def test_s82_level_two_disconnected_challenger_confidence_unchanged():
    """Stage 7: disconnected (streak-free) challenger intervals keep 75 %."""

    pipeline = three_hour_chain(7)
    original = settled(pipeline)
    pipeline.observe("challenger-a", [at(2, 1), at(2, 5)])
    pipeline.observe("challenger-b", [at(2, 13), at(2, 17)])
    step = pipeline.evaluate(persisted(original.decision))
    value = step.consumer()

    assert step.decision.state is State.CHALLENGER_ACCUMULATING
    assert value.presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert value.confidence_display_value == 0.75


def test_s82_two_interval_warning_is_at_most_once_across_settle_and_reopen():
    pipeline = three_hour_chain(3)
    first_step = pipeline.evaluate()
    occurrence = first_step.consumer().next_expected_restart_at
    due = occurrence - 300
    first = first_step.consumer(now=due)
    fired = frozenset({first.scheduled_warning_suppression_key})
    settled_step = pipeline.evaluate(persisted(first_step.decision))
    reopened = pipeline.evaluate(persisted(settled_step.decision))

    assert first.scheduled_warning_eligible and first.scheduled_pre_restart_alert_eligible
    for later in (first_step, settled_step, reopened):
        value = later.consumer(now=due, fired_keys=fired)
        assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
        assert not value.scheduled_pre_restart_alert_eligible


# --------------------------------------------------------------------------
# Stage 8.3 focused tests: long-horizon sparse evidence
# --------------------------------------------------------------------------


def sparse_support(step, period, phase=None):
    candidate = step.candidate(period, phase)
    return candidate.aligned_normal_confidence


def sparse_aligned_times(step, period, *, prior, phase=None):
    """Canonical times of the restarts in a candidate's sparse aligned set."""

    candidate = step.candidate(period, phase)
    relationships = {item.relationship_id: item for item in step.relationships}
    support = authority._sparse_support(
        period=period,
        phase_offset=candidate.phase_offset,
        sparse=authority._sparse_evidence(step.events, prior),
        excluded_event_ids=authority._relationship_endpoint_ids(
            relationships[item] for item in candidate.high_relationship_ids
        ),
    )
    return {item.canonical_phase_at for item in support.aligned}


def test_s83_sparse_observations_accumulate_across_days_and_sessions():
    supports = []
    for count in (3, 5, 8):
        step = sparse_pipeline(SPARSE_DISCRIMINATING[:count]).evaluate()
        supports.append(sparse_support(step, P3))
    final = sparse_pipeline().evaluate()
    candidate = final.candidate(P3)

    assert supports == [
        round(scoring._event_count_cap(3), 6),
        round(scoring._event_count_cap(5), 6),
        round(scoring._event_count_cap(8), 6),
    ]
    assert candidate.high_authority == 0
    assert candidate.combined_confidence == round(
        authority.SPARSE_EVIDENCE_CEILING * supports[-1], 6
    )
    assert len({item.monitoring_session_id for item in final.events}) == 8


def test_s83_long_horizon_evidence_survives_the_schema3_recent_window():
    """Eight observations spread over 14 days: schema-3 sees only its recent
    7-day window, schema-4 sparse support counts every one."""

    times = [at(day * 2, hour) for day, hour in enumerate((6, 9, 12, 15, 18, 21, 3, 15))]
    step = sparse_pipeline(times).evaluate()

    assert times[-1] - times[0] > 7 * DAY
    assert step.score.candidate(P3).aligned_event_count < 8
    assert sparse_support(step, P3) == round(scoring._event_count_cap(8), 6)


def test_s83_repeated_clock_slots_on_different_days_count_separately():
    first_days = [at(0, 21), at(2, 0), at(3, 21), at(5, 0)]
    repeats = [at(1, 21), at(3, 0), at(4, 21), at(6, 0)]
    before = sparse_pipeline(first_days).evaluate()
    pipeline = sparse_pipeline(first_days)
    pipeline.sparse("repeat", repeats)
    after = pipeline.evaluate()

    assert sparse_support(before, P3) == round(scoring._event_count_cap(4), 6)
    assert sparse_support(after, P3) == round(scoring._event_count_cap(8), 6)


def test_s83_one_physical_restart_delivered_twice_counts_once():
    once = sparse_pipeline(SPARSE_DISCRIMINATING[:4])
    twice = once.copy()
    twice.events.extend(once.events)

    assert twice.evaluate().decision == once.evaluate().decision
    assert sparse_support(twice.evaluate(), P3) == round(scoring._event_count_cap(4), 6)


def test_s83_no_relationships_are_fabricated_across_unmonitored_gaps():
    step = sparse_pipeline().evaluate()
    three = [item for item in step.relationships if item.candidate_period_seconds == P3]
    candidate = step.candidate(P3)

    # Consecutive observations are more than 8 periods apart (or separated
    # by unmonitored time): no 3 h interval is inferred, high or otherwise.
    assert all(not item.high_authority_eligible for item in step.relationships)
    assert all(item.relationship_kind is not continuity.RelationshipKind.DIRECT for item in three)
    assert not candidate.high_relationship_ids and not candidate.h1_gate
    assert candidate.aligned_normal_confidence > 0


def test_s83_normal_only_phase_comes_from_long_horizon_evidence():
    """Ten restarts at one 3 h phase over 10 days, then two restarts an hour
    off that phase 10 days later: schema-3's recent window only sees the two;
    the long-horizon phase candidate is still derived and supported."""

    old = [at(day, 9) for day in range(10)]
    recent = [at(20, 10), at(21, 13)]
    pipeline = sparse_pipeline(old)
    pipeline.sparse("recent", recent)
    step = pipeline.evaluate()
    schema3_phase = step.score.candidate(P3).phase_offset
    long_horizon = step.candidate(P3, at(0, 9) % P3)
    recent_candidate = step.candidate(P3, at(20, 10) % P3)

    assert abs(((schema3_phase - at(20, 10)) + P3 / 2) % P3 - P3 / 2) < 60
    assert abs(((long_horizon.phase_offset - at(0, 9)) + P3 / 2) % P3 - P3 / 2) < 1
    # Stage 8.7a: the two off-phase restarts are net evidence against, and
    # 10/12 consistency is below the SE3-accepted proportion.
    assert long_horizon.aligned_normal_confidence == round(
        scoring._event_count_cap(10 - 2)
        * min(1.0, (10 / 12) / authority.SPARSE_CONSISTENCY_TOLERANCE),
        6,
    )
    assert recent_candidate.regime_id != long_horizon.regime_id


def test_s83_sparse_and_continuous_evidence_reinforce_one_candidate():
    pipeline = three_hour_chain(3, day=7)
    continuous = pipeline.evaluate()
    pipeline.sparse("sparse", SPARSE_DISCRIMINATING[:5])
    mixed = pipeline.evaluate()
    before = continuous.candidate(P3)
    after = mixed.candidate(P3)

    assert after.regime_id == before.regime_id
    assert after.high_authority == before.high_authority == 0.80
    assert before.aligned_normal_confidence == 0
    assert after.aligned_normal_confidence == round(scoring._event_count_cap(5), 6)
    assert after.combined_confidence == round(
        0.80 + 0.20 * authority.SPARSE_EVIDENCE_CEILING * after.aligned_normal_confidence, 6
    )
    assert mixed.decision.state is State.ESTABLISHED


def test_s83_own_high_endpoints_are_excluded_from_sparse_support():
    pipeline = three_hour_chain(4)
    only_continuous = pipeline.evaluate()
    pipeline.sparse("extra", [at(2, 9)])
    with_one_sparse = pipeline.evaluate()

    assert sparse_support(only_continuous, P3) == 0
    assert only_continuous.candidate(P3).combined_confidence == 0.95
    assert sparse_support(with_one_sparse, P3) == round(scoring._event_count_cap(1), 6)


def test_s83_alternative_periods_evaluate_the_same_restarts_independently():
    step = three_hour_chain(4).evaluate()
    six = step.candidate(P3 * 2, at(0, 12) % (P3 * 2))

    # The 3 h candidate's own endpoints give it no sparse support, but the
    # same four restarts are evaluated independently for 6 h: the long-horizon
    # 6 h phase is 12:00/18:00 (2 aligned of the 3 restarts from 12:00 on).
    assert sparse_support(step, P3) == 0
    assert abs(((six.phase_offset - at(0, 12)) + P3) % (2 * P3) - P3) < 1
    assert six.aligned_normal_confidence == round(
        scoring._event_count_cap(2 - 1)
        * min(1.0, (2 / 3) / authority.SPARSE_CONSISTENCY_TOLERANCE),
        6,
    )
    assert not six.h1_gate and not six.h2_gate
    assert step.decision.selected_shadow_regime.candidate_period_seconds == P3


def _superseded_three_hour():
    pipeline = Pipeline()
    pipeline.observe("three", [at(0, 0) + index * P3 for index in range(7)])
    original = settled(pipeline)
    pipeline.observe("four", [at(1, 0) + index * 4 * HOUR for index in range(4)])
    replaced = pipeline.evaluate(persisted(original.decision))
    return pipeline, replaced


def test_s83_superseded_regime_evidence_does_not_strengthen_a_conflicting_regime():
    pipeline, replaced = _superseded_three_hour()
    assert replaced.decision.state is State.NEW_REGIME_ESTABLISHED
    old = next(
        item for item in replaced.decision.regimes if item.candidate_period_seconds == P3
    )
    assert old.status is authority.RegimeRecordStatus.SUPERSEDED
    without_history = pipeline.copy().evaluate()
    with_history = pipeline.evaluate(persisted(replaced.decision))

    # Day-0 00:00 and 12:00 lie on the 4 h grid, but they were explained by
    # the superseded 3 h regime: they are aligned 4 h evidence only without
    # that history (Stage 8.7a nets the off-phase day-0 restarts, so the
    # magnitude itself is zero either way).
    day0 = {at(0, 0), at(0, 12)}
    assert day0 <= sparse_aligned_times(without_history, 4 * HOUR, prior=None)
    assert not day0 & sparse_aligned_times(
        with_history, 4 * HOUR, prior=persisted(replaced.decision)
    )
    assert sparse_support(with_history, 4 * HOUR) == 0
    assert (
        with_history.decision.incumbent_shadow_regime.regime_id
        == replaced.decision.incumbent_shadow_regime.regime_id
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: sparse_pipeline(), id="sparse"),
        pytest.param(
            lambda: (lambda p: (p.sparse("sparse", SPARSE_DISCRIMINATING[:5]), p)[1])(
                three_hour_chain(3, day=7)
            ),
            id="mixed",
        ),
    ],
)
def test_s83_rebuild_and_reload_are_stable(build):
    pipeline = build()
    first = pipeline.evaluate()
    values = rebuilds(pipeline, first)

    assert len({item.decision.state for item in values}) == 1
    assert values[1].decision == values[2].decision == values[3].decision
    assert len({sparse_support(item, P3) for item in values}) == 1


def test_s83_superseded_history_rebuild_is_stable():
    pipeline, replaced = _superseded_three_hour()
    values = rebuilds(pipeline, replaced)

    assert values[1].decision == values[2].decision == values[3].decision


def test_s83_sparse_only_evidence_establishes_only_through_the_sparse_gate():
    # Stage 8.5 replaced the pre-8.5 guard: sparse-only evidence establishes
    # through the authority sparse gate, never above the sparse ceiling.
    step = settled(sparse_pipeline(SPARSE_DISCRIMINATING + tuple(at(day, 0) for day in (7, 8, 9))))
    value = step.consumer()
    regime = step.decision.incumbent_shadow_regime

    assert step.decision.state is State.ESTABLISHED
    assert regime.origin is Origin.NORMAL
    assert authority.SPARSE_ESTABLISHMENT_REASON in regime.reason_codes
    assert all(
        item.combined_confidence <= authority.SPARSE_EVIDENCE_CEILING * 0.95 + 1e-9
        for item in step.decision.candidates
    )
    assert value.countdown_visible and value.countdown_safe


def test_s83_two_interval_establishment_with_realistic_jitter_enables_warning():
    """Regression (Stage 8.3a): 5 s timing jitter gives combined 0.798986,
    displayed 80 %, and the warning follows the displayed milestone."""

    pipeline = Pipeline()
    pipeline.observe("jitter", [at(0, 9), at(0, 12) + 5, at(0, 15)])
    step = pipeline.evaluate()
    occurrence = step.consumer().next_expected_restart_at
    due = step.consumer(now=occurrence - 300)

    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert step.decision.combined_confidence == 0.798986
    assert percent(step) == 80
    assert due.scheduled_warning_eligible, step.decision.combined_confidence
    assert due.scheduled_pre_restart_alert_eligible


def test_s83a_jitter_warning_is_at_most_once_across_settle_and_reopen():
    pipeline = Pipeline()
    pipeline.observe("jitter", [at(0, 9), at(0, 12) + 5, at(0, 15)])
    first_step = pipeline.evaluate()
    occurrence = first_step.consumer().next_expected_restart_at
    due = occurrence - 300
    first = first_step.consumer(now=due)
    fired = frozenset({first.scheduled_warning_suppression_key})
    settled_step = pipeline.evaluate(persisted(first_step.decision))
    reopened = pipeline.evaluate(persisted(settled_step.decision))

    assert first.scheduled_pre_restart_alert_eligible
    for later in (first_step, settled_step, reopened):
        value = later.consumer(now=due, fired_keys=fired)
        assert value.scheduled_warning_eligible
        assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
        assert not value.scheduled_pre_restart_alert_eligible
    following = reopened.consumer(now=due + P3, fired_keys=fired)
    assert following.scheduled_warning_suppression_key != first.scheduled_warning_suppression_key
    assert following.scheduled_pre_restart_alert_eligible


@pytest.mark.parametrize(
    "contaminate",
    [
        pytest.param(lambda item: replace(item, coverage_complete=False), id="coverage_incomplete"),
        pytest.param(
            lambda item: replace(
                item,
                coverage_complete=False,
                lifecycle_interruption=detection.LifecycleMarker.SLEEP_GAP,
            ),
            id="sleep_gap_or_local_outage",
        ),
        pytest.param(
            lambda item: replace(item, outcome=Outcome.AMBIGUOUS_DRAIN, authenticity=0.25),
            id="unconfirmed_drain",
        ),
        pytest.param(
            lambda item: replace(item, outcome=Outcome.INCOMPLETE, authenticity=0.10),
            id="incomplete_episode",
        ),
    ],
)
def test_s83_ineligible_or_unconfirmed_restarts_add_no_sparse_support(contaminate):
    clean = sparse_pipeline(SPARSE_DISCRIMINATING[:4])
    contaminated = clean.copy()
    contaminated.sparse("extra", SPARSE_DISCRIMINATING[4:])
    contaminated.events[4:] = [contaminate(item) for item in contaminated.events[4:]]

    assert sparse_support(contaminated.evaluate(), P3, at(0, 6) % P3) == sparse_support(
        clean.evaluate(), P3, at(0, 6) % P3
    )


# --------------------------------------------------------------------------
# Stage 8.4 focused tests: unified confidence presentation
# --------------------------------------------------------------------------


LEARNING_TEXT = "Learning Restart Pattern"


def panel(step, *, now=None, **kwargs):
    now = step.now if now is None else now
    return ui(step, step.consumer(now=now, **kwargs), now)


def assert_learning(shown):
    assert shown["cycle_text"] == LEARNING_TEXT, shown["cycle_text"]
    assert shown["confidence_label"] == "Learning Confidence:"
    assert not shown["countdown_visible"] and not shown["next_visible"]
    assert not shown["prediction_usable"]


def test_s84_sparse_learning_shows_its_long_horizon_schema4_candidate():
    # Seven sparse restarts (Stage 8.5: one short of establishment).
    step = settled(sparse_pipeline(SPARSE_DISCRIMINATING[:7]))
    value = step.consumer()
    shown = panel(step)
    leading = max(step.decision.candidates, key=lambda item: item.combined_confidence)
    schema3 = runtime.presentation_period_candidate(step.score, period_seconds=None)

    assert step.decision.state is State.NORMAL_LEARNING
    assert leading.candidate_period_seconds == P3
    assert not leading.high_relationship_ids  # a sparse (normal-only) candidate
    assert leading.aligned_normal_confidence == round(scoring._event_count_cap(7), 6)
    assert_learning(shown)
    assert shown["confidence_visible"]
    assert value.confidence_display_value == leading.combined_confidence == 0.77
    assert shown["confidence_percent"] == 77
    # Not the schema-3 recent-window fundamental (16 %).
    assert schema3 == (P3, 0.1625)
    assert consumers4.LEARNING_CONFIDENCE_REASON in value.reason_codes


def test_s84_learning_confidence_never_comes_from_a_different_period():
    step = settled(sparse_pipeline(SAME_CLOCK))
    shown = panel(step)
    schema3 = runtime.presentation_period_candidate(step.score, period_seconds=None)
    best = max(item.combined_confidence for item in step.decision.candidates)
    reordered = replace(step.decision, candidates=tuple(reversed(step.decision.candidates)))
    again = consume(replace(step, decision=reordered), now=step.now)

    # The schema-3 projection would name a 12 h period.  Schema-4 shows its
    # own leading candidate, capped at 59 % because same-clock evidence
    # leaves the period unresolved (Stage 8.5 SE4/SE5 ambiguity marker).
    assert schema3 == (12 * HOUR, 0.59)
    assert best == 0.798
    assert all(
        authority.SPARSE_PERIOD_AMBIGUOUS_REASON in item.reason_codes
        for item in step.decision.candidates
    )
    assert_learning(shown)
    assert step.consumer().confidence_display_value == min(0.59, best)
    assert consumers4.LEARNING_CONFIDENCE_AMBIGUITY_CAP_REASON in step.consumer().reason_codes
    assert again.confidence_display_value == step.consumer().confidence_display_value

    # Discriminating evidence: the leading candidate is the supported period.
    discriminating = settled(sparse_pipeline(SPARSE_DISCRIMINATING[:7]))
    lead = max(discriminating.decision.candidates, key=lambda item: item.combined_confidence)
    assert lead.candidate_period_seconds == P3
    assert discriminating.consumer().confidence_display_value == lead.combined_confidence

    # A LIKELY regime shows its own candidate, not the best other period.
    likely = imprecise_h2().evaluate()
    regime = likely.decision.selected_shadow_regime
    own = consumers4._candidate_for_regime(likely.decision, regime)
    assert own.candidate_period_seconds == regime.candidate_period_seconds == P3
    assert likely.consumer().confidence_display_value == min(0.79, own.combined_confidence)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(imprecise_h2, id="h2_continuity_likely"),
        pytest.param(
            lambda: three_hour_chain(5, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
            id="normal_origin_likely",
        ),
    ],
)
def test_s84_unknown_server_likely_consistently_shows_learning(build):
    pipeline = build()
    first = pipeline.evaluate()
    values = rebuilds(pipeline, first)
    values.append(pipeline.evaluate(values[-1].decision))  # in-memory reopen
    occurrence = values[0].candidate(P3).phase_offset + 100 * P3

    for step in values:
        value = step.consumer()
        shown = panel(step)
        warning = step.consumer(now=occurrence - 300)
        assert step.decision.state is State.LIKELY
        assert value.presentation_state is Presentation.PATTERN_ONLY
        assert not value.visible_cycle and value.cycle_period_seconds is None
        assert_learning(shown)
        assert shown["confidence_visible"]
        assert shown["confidence_percent"] <= 79
        assert not warning.scheduled_warning_eligible
        assert not warning.countdown_visible
    assert len({panel(step)["confidence_percent"] for step in values}) == 1
    assert len({step.consumer().decision_id for step in values}) == 1


def test_s84_two_interval_established_cycle_shows_80_percent_with_countdown_and_warning():
    pipeline = three_hour_chain(3)
    first = pipeline.evaluate()
    for step in (first, pipeline.evaluate(persisted(first.decision))):
        value = step.consumer()
        shown = panel(step)
        due = step.consumer(now=value.next_expected_restart_at - 300)

        assert step.decision.state is State.ESTABLISHED
        assert shown["cycle_text"] == "Every 3 Hours"
        assert shown["confidence_label"] == "Cycle Confidence:"
        assert shown["confidence_percent"] == 80
        assert value.confidence_display_value == step.candidate(P3).combined_confidence
        assert shown["countdown_visible"] and value.countdown_safe
        assert due.scheduled_warning_eligible and due.scheduled_pre_restart_alert_eligible


def test_s84_three_interval_established_cycle_shows_95_percent():
    step = three_hour_chain(4).evaluate()
    shown = panel(step)

    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_percent"] == DISPLAY_CEILING_PERCENT
    assert shown["countdown_visible"]


def _harmonic_likely():
    pipeline = Pipeline()
    pipeline.observe("six-hour", [at(0, 0), at(0, 6), at(0, 12)])
    ledger = (
        genuine_miss(3 * HOUR, at(1, 3), key="divisor-3h"),
        genuine_miss(2 * HOUR, at(1, 2), key="divisor-2h"),
    )
    return pipeline, {"ledger": ledger}


def _competing_likely():
    pipeline = Pipeline()
    pipeline.observe("three-hour", [at(0, 9), at(0, 12), at(0, 15)])
    pipeline.observe("four-hour", [at(1, 1), at(1, 5), at(1, 9)])
    return pipeline, {}


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(_harmonic_likely, id="harmonic_blocker"),
        pytest.param(_competing_likely, id="period_ambiguity"),
    ],
)
def test_s84_harmonic_blockers_and_period_ambiguity_cap_learning_at_59_percent(build):
    pipeline, kwargs = build()
    first = pipeline.evaluate(**kwargs)
    again = pipeline.evaluate(persisted(first.decision), **kwargs)
    candidate = consumers4._candidate_for_regime(
        first.decision, first.decision.selected_shadow_regime
    )

    assert first.decision.state is State.LIKELY
    assert candidate.combined_confidence > 0.59
    for step in (first, again):
        value = step.consumer()
        shown = panel(step)
        assert_learning(shown)
        assert value.confidence_display_value == 0.59
        assert shown["confidence_percent"] == 59
        assert consumers4.LEARNING_CONFIDENCE_AMBIGUITY_CAP_REASON in value.reason_codes


def test_s84_structurally_incomplete_learning_is_capped_at_79_percent():
    # A clean two-interval streak (0.80) withheld from establishment by an
    # in-phase genuine miss: LIKELY, no harmonic blocker, no competitor.
    step = three_hour_chain(3).evaluate(ledger=(genuine_miss(P3, at(0, 21), key="in-phase-miss"),))
    value = step.consumer()
    imprecise = imprecise_h2().evaluate()

    assert step.decision.state is State.LIKELY
    assert step.candidate(P3).combined_confidence == 0.80
    assert not step.candidate(P3).harmonic_blockers
    assert value.confidence_display_value == 0.79
    assert_learning(panel(step))
    assert panel(step)["confidence_percent"] == 79
    assert consumers4.LEARNING_CONFIDENCE_STRUCTURE_CAP_REASON in value.reason_codes
    # Below the cap the actual value is shown unchanged (0.789786 -> 79 %).
    assert imprecise.consumer().confidence_display_value == imprecise.candidate(P3).combined_confidence
    assert panel(imprecise)["confidence_percent"] == 79


def test_s84_stage7_suspected_change_presentation_unchanged():
    pipeline = three_hour_chain(7)
    original = settled(pipeline)
    pipeline.observe("challenger-a", [at(2, 1), at(2, 5)])
    pipeline.observe("challenger-b", [at(2, 13), at(2, 17)])
    step = pipeline.evaluate(persisted(original.decision))
    value = step.consumer()
    shown = panel(step)

    assert value.presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert value.confidence_display_value == 0.75
    assert shown["cycle_text"] == "Suspected Cycle Change"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_percent"] == 75
    assert not shown["countdown_visible"]
    assert consumers4.LEARNING_CONFIDENCE_REASON not in value.reason_codes


def test_s84_provisional_new_cycle_keeps_likely_new_with_challenger_confidence():
    step = _gate_scenarios()["challenger_two"]
    value = step.consumer()
    shown = panel(step)
    selected = step.decision.selected_shadow_regime
    challenger = next(
        item for item in step.decision.candidates if item.regime_id == selected.regime_id
    )

    assert step.decision.state is State.TRANSITION_CONFIRMED
    assert selected.candidate_period_seconds == 4 * HOUR
    assert value.presentation_state is Presentation.LIKELY_NEW_CYCLE
    assert value.confidence_display_value == challenger.combined_confidence
    assert shown["cycle_text"] == "Likely new: Every 4 hours"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["confidence_percent"] == consumers4.confidence_display_percent(
        challenger.combined_confidence
    )
    assert not shown["countdown_visible"]


def test_s84_legacy_schema3_fallback_when_schema4_is_invalid():
    step = settled(sparse_pipeline())
    schema3, _selected, _predicted = schema3_consumer(step, now=step.now)
    resolution = consumers4.resolve_authority_consumer_cutover(
        schema3_decision=schema3,
        schema4_decision=step.consumer(),
        production_cutover_enabled=True,
        authoritative_schema4_runtime_enabled=True,
        schema4_server_valid=False,
    )
    # The window path for a schema-3 source: schema-3 summary plus the
    # schema-3 learning-candidate projection.
    summary = runtime.phase2_learning_summary(schema3, now=step.now)
    candidate = runtime.presentation_period_candidate(step.score, period_seconds=None)
    shown = restart_learning_presentation(
        companion_display_confidence_summary(
            summary, candidate, period_seconds=schema3.selected_period_seconds
        )
    )

    assert resolution.source is consumers4.CutoverSource.SCHEMA3_SAFE_FALLBACK
    assert resolution.selected_output is schema3
    assert "learning_confidence_source" not in (summary or {})
    assert shown["cycle_text"] == LEARNING_TEXT
    assert shown["confidence_label"] == "Learning Confidence:"
    assert shown["confidence_percent"] == round(candidate[1] * 100) == 26


PRESENTATION_BUILDERS = {
    "sparse_learning": lambda: (sparse_pipeline(), {}),
    "h2_likely": lambda: (imprecise_h2(), {}),
    "harmonic_likely": _harmonic_likely,
    "two_interval_established": lambda: (three_hour_chain(3), {}),
    "three_interval_established": lambda: (three_hour_chain(4), {}),
    "normal_origin_established": lambda: (
        three_hour_chain(10, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
        {},
    ),
}


@pytest.mark.parametrize("name", sorted(PRESENTATION_BUILDERS))
def test_s84_presentation_is_stable_across_rebuild_and_reopen(name):
    pipeline, kwargs = PRESENTATION_BUILDERS[name]()
    first = pipeline.evaluate(**kwargs)
    second = pipeline.evaluate(persisted(first.decision), **kwargs)
    third = pipeline.evaluate(persisted(second.decision), **kwargs)
    fourth = pipeline.evaluate(third.decision, **kwargs)

    shown = [panel(step) for step in (first, second, third, fourth)]
    visible = [
        (
            item["cycle_text"],
            item["confidence_label"],
            item["confidence_visible"],
            item["confidence_percent"],
            item["presentation_key"],
        )
        for item in shown
    ]
    # The user-visible cycle and confidence never change with identical
    # evidence.  First decisions settle once (pre-existing: diagnostic reason
    # codes, and the schema-3 sticky incumbent period that a NORMAL-origin
    # countdown needs); from the first rebuild on the panel is a fixed point.
    assert all(item == visible[0] for item in visible), visible
    assert shown[2] == shown[1] and shown[3] == shown[1]
    assert third.consumer().decision_id == second.consumer().decision_id
    assert fourth.consumer().decision_id == second.consumer().decision_id


@pytest.mark.parametrize(
    "times",
    [
        pytest.param(SPARSE_DISCRIMINATING[:7], id="seven_discriminating"),
        pytest.param(SAME_CLOCK + tuple(at(day, 9) for day in range(8, 12)), id="same_clock_twelve"),
    ],
)
def test_s84_no_unapproved_sparse_only_establishment(times):
    # Stage 8.5: insufficient or ambiguous sparse evidence never establishes,
    # and the sparse value is shown as it is, never scaled up.
    pipeline = sparse_pipeline(times)
    values = rebuilds(pipeline, pipeline.evaluate())

    for step in values:
        value = step.consumer()
        shown = panel(step)
        assert step.decision.state is State.NORMAL_LEARNING
        assert step.decision.selected_shadow_regime is None
        assert not established(step)
        assert value.cycle_period_seconds is None and not value.countdown_visible
        assert_learning(shown)
        assert all(
            item.combined_confidence <= authority.SPARSE_EVIDENCE_CEILING * 0.95 + 1e-9
            for item in step.decision.candidates
        )
        assert shown["confidence_percent"] <= 79


@pytest.mark.parametrize(
    "jitter,displayed,eligible",
    [
        pytest.param(5, 80, True, id="5s"),
        pytest.param(24, 80, True, id="24s"),
        pytest.param(25, 79, False, id="25s"),
    ],
)
def test_s84_stage8_3a_rounding_and_warning_deduplication_unchanged(jitter, displayed, eligible):
    pipeline = Pipeline()
    pipeline.observe("jitter", [at(0, 9), at(0, 12) + jitter, at(0, 15)])
    step = pipeline.evaluate()
    occurrence = step.consumer().next_expected_restart_at
    due = step.consumer(now=occurrence - 300)
    shown = panel(step)

    assert step.decision.state is State.ESTABLISHED
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_percent"] == displayed
    assert due.scheduled_warning_eligible is eligible
    if eligible:
        fired = frozenset({due.scheduled_warning_suppression_key})
        reopened = pipeline.evaluate(persisted(step.decision))
        again = reopened.consumer(now=occurrence - 300, fired_keys=fired)
        assert again.scheduled_warning_suppression_key == due.scheduled_warning_suppression_key
        assert not again.scheduled_pre_restart_alert_eligible


# --------------------------------------------------------------------------
# Stage 8.5 focused tests: sparse cycle establishment
# --------------------------------------------------------------------------


# Separate days and weeks, discriminating 3 h slots, one restart per session.
SPARSE_WEEKS = (
    at(0, 6), at(3, 9), at(7, 12), at(10, 15), at(14, 18), at(17, 21), at(21, 3), at(27, 15),
)
# Only two clock slots, each repeated on four different days.
TWO_SLOTS = tuple(at(day, 6) for day in (0, 2, 4, 6)) + tuple(at(day, 9) for day in (1, 3, 5, 7))
# A true 6 h server observed sparsely: also consistent with 3 h and 2 h.
SIX_HOUR_SPARSE = tuple(
    at(day, hour)
    for day, hour in ((0, 0), (1, 6), (2, 12), (3, 18), (4, 0), (5, 6), (6, 12), (7, 18), (8, 0), (9, 6))
)


def observe_mixed(pipeline, name, values):
    """One continuous session whose restarts have individual outcomes."""

    times = [when for when, _outcome in values]
    session = Session(name, min(times) - 20 * 60, max(times) + 20 * 60)
    pipeline.coverage.append(session.coverage())
    for when, outcome in values:
        pipeline.events.append(physical_event(pipeline.sequence, when, session, outcome=outcome))
        pipeline.sequence += 1


def incremental(pipeline_factory, times, **kwargs):
    """Runtime-like: one evaluation (with the persisted prior) per restart."""

    pipeline = Pipeline()
    prior = None
    steps = []
    for index, when in enumerate(times):
        pipeline.sparse(f"inc-{index}", [when], **kwargs)
        step = pipeline.evaluate(prior)
        prior = persisted(step.decision)
        steps.append(step)
    return pipeline, steps


def assert_sparse_established(step, *, label="Every 3 Hours"):
    value = step.consumer()
    shown = panel(step)
    regime = step.decision.incumbent_shadow_regime

    assert step.decision.state is State.ESTABLISHED, step.decision.state
    assert regime.origin is Origin.NORMAL
    assert authority.SPARSE_ESTABLISHMENT_REASON in regime.reason_codes
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert shown["cycle_text"] == label
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert value.countdown_visible and value.countdown_safe
    assert "safe_established_sparse_prediction_available" in value.reason_codes
    return value, shown


def test_s85_sparse_evidence_accumulates_across_days_and_weeks():
    _pipeline, steps = incremental(Pipeline, SPARSE_WEEKS)

    assert SPARSE_WEEKS[-1] - SPARSE_WEEKS[0] > 3 * 7 * DAY
    assert not any(item.high_authority_eligible for item in steps[-1].relationships)
    for step in steps[:-1]:
        assert step.decision.state is not State.ESTABLISHED
        assert not step.consumer().countdown_visible
        assert panel(step)["confidence_percent"] <= 79
    _value, shown = assert_sparse_established(steps[-1])
    assert shown["confidence_percent"] == 80


def test_s85_repeated_clock_times_are_independent_observations():
    step = settled(sparse_pipeline(TWO_SLOTS))
    _value, shown = assert_sparse_established(step)

    assert len({item.canonical_phase_at % DAY for item in step.events}) == 2
    assert len({item.event_id for item in step.events}) == 8
    assert shown["confidence_percent"] == 80


@pytest.mark.parametrize("count", [7, 8])
def test_s85_duplicate_physical_restart_is_counted_once(count):
    single = sparse_pipeline(SPARSE_DISCRIMINATING[:count])
    double = single.copy()
    double.events.extend(single.events)

    assert double.evaluate().decision == single.evaluate().decision
    assert (single.evaluate().decision.state is State.ESTABLISHED) is (count == 8)


@pytest.mark.parametrize(
    "times,outcome,ambiguous",
    [
        pytest.param(SPARSE_DISCRIMINATING[:7], Outcome.CORROBORATED_OFFLINE_RESTART, False, id="seven_restarts"),
        pytest.param(SPARSE_DISCRIMINATING, Outcome.CONFIRMED_OFFLINE_RESTART, False, id="eight_weaker_restarts"),
        pytest.param(SAME_CLOCK + tuple(at(day, 9) for day in range(8, 12)), Outcome.CORROBORATED_OFFLINE_RESTART, True, id="same_clock"),
        pytest.param(SIX_HOUR_SPARSE, Outcome.CORROBORATED_OFFLINE_RESTART, True, id="harmonic_six_hour"),
    ],
)
def test_s85_insufficient_or_ambiguous_sparse_evidence_withholds_establishment(times, outcome, ambiguous):
    pipeline = sparse_pipeline(times, outcome=outcome)
    for step in rebuilds(pipeline, pipeline.evaluate()):
        value = step.consumer()
        shown = panel(step)
        assert step.decision.state is State.NORMAL_LEARNING
        assert not value.countdown_visible and not value.scheduled_warning_eligible
        assert_learning(shown)
        assert shown["confidence_percent"] <= (59 if ambiguous else 79)
        assert authority.SPARSE_ESTABLISHMENT_REASON not in step.decision.reason_codes


def test_s85_eight_strong_sparse_restarts_establish_with_prediction_and_warning():
    step = settled(sparse_pipeline())
    value, shown = assert_sparse_established(step)
    regime = step.decision.incumbent_shadow_regime
    occurrence = value.next_expected_restart_at
    due = step.consumer(now=occurrence - 300)

    assert shown["confidence_percent"] == 80
    assert step.decision.combined_confidence == round(authority.SPARSE_EVIDENCE_CEILING * 0.95, 6)
    assert regime.established_at == max(SPARSE_DISCRIMINATING)
    assert regime.candidate_period_seconds == P3
    assert abs(((occurrence - regime.phase_offset + P3 / 2) % P3) - P3 / 2) < 1
    assert due.scheduled_warning_eligible and due.scheduled_pre_restart_alert_eligible


def test_s85_sparse_established_plus_one_full_interval_reaches_95_percent():
    sparse_step, step = _sparse_plus_matching_interval()
    value, shown = assert_sparse_established(step)
    interval = step.high_direct(P3)[0]
    seen_slots = {item % DAY for item in SPARSE_DISCRIMINATING}

    # Both interval endpoints repeat clock slots the sparse evidence had seen.
    assert {interval.left_at % DAY, interval.right_at % DAY} <= seen_slots
    assert shown["confidence_percent"] == DISPLAY_CEILING_PERCENT
    assert authority.SPARSE_CONFIRMATION_REASON in step.decision.reason_codes
    assert (
        step.decision.incumbent_shadow_regime.regime_id
        == sparse_step.decision.incumbent_shadow_regime.regime_id
    )
    # Gates are untouched: one interval is still only H1.
    candidate = step.candidate(P3)
    assert candidate.h1_gate and not candidate.h2_gate and candidate.high_authority == 0.5
    assert value.scheduled_warning_eligible or value.next_expected_restart_at is not None


def test_s85_strong_confirmation_does_not_depend_on_arrival_order():
    later = _sparse_plus_matching_interval()[1]
    together = sparse_pipeline()
    together.observe("complete", [at(6, 18), at(6, 21)])
    seven = Pipeline()
    seven.sparse("sparse", SPARSE_DISCRIMINATING[:7])
    seven.observe("complete", [at(6, 18), at(6, 21)])
    before_eighth = seven.evaluate()
    seven.sparse("eighth", [SPARSE_DISCRIMINATING[7]])
    after_eighth = seven.evaluate(persisted(before_eighth.decision))

    assert panel(later)["confidence_percent"] == DISPLAY_CEILING_PERCENT
    assert panel(settled(together))["confidence_percent"] == DISPLAY_CEILING_PERCENT
    # Seven sparse restarts plus an interval: still learning, capped.
    assert before_eighth.decision.state is not State.ESTABLISHED
    assert panel(before_eighth)["confidence_percent"] <= 79
    assert panel(after_eighth)["confidence_percent"] == DISPLAY_CEILING_PERCENT


def test_s85_matching_timestamps_across_an_unobserved_gap_never_confirm():
    pipeline = sparse_pipeline()
    established_step = settled(pipeline)
    pipeline.sparse("gap", [at(9, 9), at(9, 12)])
    step = pipeline.evaluate(persisted(established_step.decision))

    assert not step.high_direct(P3)
    assert authority.SPARSE_CONFIRMATION_REASON not in step.decision.reason_codes
    assert panel(step)["confidence_percent"] == 80


@pytest.mark.parametrize(
    "outcome,times",
    [
        pytest.param(Outcome.CORROBORATED_OFFLINE_RESTART, SPARSE_DISCRIMINATING, id="oo_corroborated"),
        pytest.param(Outcome.CONFIRMED_OFFLINE_RESTART, SPARSE_QV_TEN, id="oo_confirmed"),
        pytest.param(Outcome.STRONG_QUERY_VISIBLE_RESTART, SPARSE_QV_TEN, id="qv_strong"),
    ],
)
def test_s85_oo_and_qv_reach_the_same_80_and_95_percent_milestones(outcome, times):
    pipeline = sparse_pipeline(times, outcome=outcome)
    sparse_step = settled(pipeline)
    _value, shown = assert_sparse_established(sparse_step)
    assert shown["confidence_percent"] == 80

    pipeline.observe("full-interval", [at(9, 18), at(9, 21)], outcome=outcome)
    step = pipeline.evaluate(persisted(sparse_step.decision))
    assert_sparse_established(step)
    assert panel(step)["confidence_percent"] == DISPLAY_CEILING_PERCENT
    assert authority.SPARSE_CONFIRMATION_REASON in step.decision.reason_codes
    if outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART:
        # The QV interval confirms without becoming high-eligible.
        assert not step.high_direct(P3)
        assert step.candidate(P3).high_authority == 0


def test_s85_continuous_qv_milestones_80_then_95_percent():
    steps = _qv_trajectory()
    by_count = {index + 2: step for index, step in enumerate(steps)}

    assert not any(item.high_authority_eligible for step in steps for item in step.relationships)
    for count in range(2, 10):
        assert by_count[count].decision.state is not State.ESTABLISHED
        assert not by_count[count].consumer().countdown_visible
    assert_sparse_established(by_count[10])
    assert panel(by_count[10])["confidence_percent"] == 80
    occurrence = by_count[10].consumer().next_expected_restart_at
    assert by_count[10].consumer(now=occurrence - 300).scheduled_warning_eligible
    assert 80 < panel(by_count[11])["confidence_percent"] < DISPLAY_CEILING_PERCENT
    assert panel(by_count[12])["confidence_percent"] == DISPLAY_CEILING_PERCENT


def test_s85_unconfirmed_qv_events_contribute_no_qualifying_learning():
    nine = SPARSE_QV_TEN[:9]
    clean = sparse_pipeline(nine, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    with_drains = clean.copy()
    with_drains.sparse("drain", [at(day, 3) for day in (10, 11, 12, 13, 14)], outcome=Outcome.AMBIGUOUS_DRAIN)
    before, after = clean.evaluate(), with_drains.evaluate()

    assert after.decision.state is before.decision.state is not State.ESTABLISHED
    assert after.candidate(P3, before.candidate(P3).phase_offset).combined_confidence == (
        before.candidate(P3).combined_confidence
    )

    # An unconfirmed drain cannot be the endpoint of a confirming interval.
    pipeline = sparse_pipeline(SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    established_step = settled(pipeline)
    observe_mixed(
        pipeline,
        "drain-interval",
        [(at(9, 18), Outcome.STRONG_QUERY_VISIBLE_RESTART), (at(9, 21), Outcome.AMBIGUOUS_DRAIN)],
    )
    step = pipeline.evaluate(persisted(established_step.decision))
    assert authority.SPARSE_CONFIRMATION_REASON not in step.decision.reason_codes
    assert panel(step)["confidence_percent"] == 80


def test_s85_period_alternatives_and_harmonic_divisors():
    unresolved = settled(sparse_pipeline(SIX_HOUR_SPARSE))
    three = unresolved.candidate(P3)
    six = unresolved.candidate(6 * HOUR)

    # 3 h cannot refute 6 h; 6 h keeps its harmonic divisor blockers.
    assert authority.SPARSE_PERIOD_AMBIGUOUS_REASON in three.reason_codes
    assert authority.SPARSE_PERIOD_AMBIGUOUS_REASON in six.reason_codes
    assert unresolved.decision.state is State.NORMAL_LEARNING

    # Monitored quiet divisor midpoints (two genuine misses each at 3 h and
    # 2 h) resolve the harmonics: 6 h establishes; 3 h stays contradicted.
    ledger = (
        genuine_miss(3 * HOUR, at(10, 3), key="divisor-3h-a"),
        genuine_miss(3 * HOUR, at(11, 9), key="divisor-3h-b"),
        genuine_miss(2 * HOUR, at(10, 2), key="divisor-2h-a"),
        genuine_miss(2 * HOUR, at(11, 4), key="divisor-2h-b"),
    )
    pipeline = sparse_pipeline(SIX_HOUR_SPARSE)
    first = pipeline.evaluate(ledger=ledger)
    again = pipeline.evaluate(persisted(first.decision), ledger=ledger)
    for step in (first, again):
        assert_sparse_established(step, label="Every 6 Hours")
        assert step.decision.incumbent_shadow_regime.candidate_period_seconds == 6 * HOUR


def test_s85_monitored_contradictions_and_competing_schedules_withhold_establishment():
    missed = sparse_pipeline().evaluate(ledger=(genuine_miss(P3, at(3, 0), key="in-phase-miss"),))
    assert missed.decision.state is State.NORMAL_LEARNING
    assert not missed.consumer().countdown_visible

    competing = sparse_pipeline()
    competing.observe("imprecise-four-hour", [at(-2, 1), at(-2, 5) + 68, at(-2, 9)])
    step = competing.evaluate()
    assert step.candidate(4 * HOUR).h2_gate
    # The 3 h sparse candidate meets the sparse structure and the 80 % sparse
    # milestone on its own; only the conflicting H2 schedule withholds it.
    three = max(
        (item for item in step.decision.candidates if item.candidate_period_seconds == P3),
        key=lambda item: item.aligned_normal_confidence,
    )
    assert authority.SPARSE_STRUCTURE_REASON in three.reason_codes
    assert three.aligned_normal_confidence == 0.95
    assert step.decision.state is State.LIKELY
    assert step.decision.selected_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert authority.SPARSE_ESTABLISHMENT_REASON not in step.decision.reason_codes


def test_s85_sparse_evidence_never_authorises_challengers_or_replaces_an_incumbent():
    pipeline = three_hour_chain(4)
    original = settled(pipeline)
    four_hour = (
        at(1, 1), at(2, 5), at(3, 9), at(4, 13), at(5, 17),
        at(6, 21), at(7, 1), at(8, 5), at(9, 9), at(10, 13),
    )
    pipeline.sparse("four-hour", four_hour)
    step = pipeline.evaluate(persisted(original.decision))
    value = step.consumer()

    assert step.decision.incumbent_shadow_regime.regime_id == original.decision.incumbent_shadow_regime.regime_id
    assert step.decision.suspicion_level <= 1
    assert all(
        item.authorization_state is authority.ChallengerAuthorization.LATENT
        and not item.high_relationship_ids
        for item in step.decision.challenger_contexts
    )
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible


def test_s85_stage6_and_stage7_protections_apply_to_sparse_incumbents():
    def with_challenger(*sessions):
        pipeline = sparse_pipeline()
        incumbent = settled(pipeline)
        for index, times in enumerate(sessions):
            pipeline.observe(f"challenger-{index}", times)
        return incumbent, pipeline.evaluate(persisted(incumbent.decision))

    _incumbent, level_two = with_challenger([at(9, 1), at(9, 5)], [at(9, 13), at(9, 17)])
    assert level_two.decision.state is State.CHALLENGER_ACCUMULATING
    assert level_two.consumer().presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert level_two.consumer().confidence_display_value == 0.75
    assert not level_two.consumer().countdown_visible

    incumbent, provisional = with_challenger([at(9, 1), at(9, 5), at(9, 9)])
    assert provisional.decision.state is State.TRANSITION_CONFIRMED
    assert provisional.consumer().presentation_state is Presentation.LIKELY_NEW_CYCLE
    assert not provisional.consumer().countdown_visible
    assert provisional.decision.incumbent_shadow_regime.regime_id == incumbent.decision.incumbent_shadow_regime.regime_id

    _incumbent, superseded = with_challenger([at(9, 1), at(9, 5), at(9, 9), at(9, 13)])
    assert superseded.decision.state is State.NEW_REGIME_ESTABLISHED
    assert superseded.decision.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR
    old = next(
        item for item in superseded.decision.regimes
        if item.regime_id == incumbent.decision.incumbent_shadow_regime.regime_id
    )
    assert old.status is authority.RegimeRecordStatus.SUPERSEDED
    # Later sparse 3 h restarts never re-establish the superseded regime by
    # the sparse gate (acquisition only): ordinary gates decide.
    pipeline = sparse_pipeline()
    pipeline_incumbent = settled(pipeline)
    pipeline.observe("challenger", [at(9, 1), at(9, 5), at(9, 9), at(9, 13)])
    after = pipeline.evaluate(persisted(pipeline_incumbent.decision))
    pipeline.sparse("late-three-hour", [at(11, 9), at(12, 12), at(13, 15)])
    later = pipeline.evaluate(persisted(after.decision))
    assert later.decision.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR


def test_s85_continuous_milestones_are_unchanged():
    assert three_hour_chain(3).evaluate().decision.combined_confidence == 0.80
    assert three_hour_chain(4).evaluate().decision.combined_confidence == 0.95
    assert percent(three_hour_chain(3).evaluate()) == 80
    assert percent(three_hour_chain(4).evaluate()) == DISPLAY_CEILING_PERCENT
    pipeline = three_hour_chain(3)
    established_step = settled(pipeline)
    pipeline.observe("day-1", [at(1, 9), at(1, 12)])
    one_more = pipeline.evaluate(established_step.decision)
    pipeline.observe("day-2", [at(2, 21), at(3, 0)])
    two_more = pipeline.evaluate(one_more.decision)
    assert one_more.decision.combined_confidence == 0.925
    assert two_more.decision.combined_confidence == 0.97
    assert authority.STREAK_BONUS_COEFFICIENT == 0.10
    assert authority.GATE_STREAK_BONUS_COEFFICIENT == 0.08
    assert consumers4.SCHEDULED_WARNING_MINIMUM_CONFIDENCE_PERCENT == 80


SPARSE_PRESENTATION_BUILDERS = {
    "sparse_learning_seven": lambda: sparse_pipeline(SPARSE_DISCRIMINATING[:7]),
    "sparse_established": lambda: sparse_pipeline(),
    "sparse_confirmed": lambda: _sparse_plus_matching_interval_pipeline(),
    "qv_sparse_established": lambda: sparse_pipeline(
        SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART
    ),
}


def _sparse_plus_matching_interval_pipeline():
    pipeline = sparse_pipeline()
    pipeline.observe("complete", [at(6, 18), at(6, 21)])
    return pipeline


@pytest.mark.parametrize("name", sorted(SPARSE_PRESENTATION_BUILDERS))
def test_s85_sparse_presentation_is_stable_across_rebuild_and_reopen(name):
    pipeline = SPARSE_PRESENTATION_BUILDERS[name]()
    first = pipeline.evaluate()
    second = pipeline.evaluate(persisted(first.decision))
    third = pipeline.evaluate(persisted(second.decision))
    fourth = pipeline.evaluate(third.decision)
    steps = (first, second, third, fourth)

    visible = {
        (
            panel(step)["cycle_text"],
            panel(step)["confidence_percent"],
            panel(step)["countdown_visible"],
        )
        for step in steps
    }
    assert len(visible) == 1, visible
    assert len({
        None if step.decision.incumbent_shadow_regime is None
        else step.decision.incumbent_shadow_regime.regime_id
        for step in steps
    }) == 1
    assert third.decision == second.decision and fourth.decision == second.decision
    assert panel(third) == panel(second)


def test_s85_sparse_warning_is_at_most_once_across_settle_reopen_and_confirmation():
    pipeline = sparse_pipeline()
    first_step = pipeline.evaluate()
    occurrence = first_step.consumer().next_expected_restart_at
    due = occurrence - 300
    first = first_step.consumer(now=due)
    fired = frozenset({first.scheduled_warning_suppression_key})
    settled_step = pipeline.evaluate(persisted(first_step.decision))
    reopened = pipeline.evaluate(persisted(settled_step.decision))
    pipeline.observe("confirming", [at(6, 18), at(6, 21)])
    confirmed = pipeline.evaluate(persisted(reopened.decision))

    assert first.scheduled_warning_eligible and first.scheduled_pre_restart_alert_eligible
    for later in (first_step, settled_step, reopened, confirmed):
        value = later.consumer(now=due, fired_keys=fired)
        assert value.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
        assert not value.scheduled_pre_restart_alert_eligible
    following = confirmed.consumer(now=due + P3, fired_keys=fired)
    assert following.scheduled_warning_suppression_key != first.scheduled_warning_suppression_key
    assert following.scheduled_pre_restart_alert_eligible


# --------------------------------------------------------------------------
# Stage 8.6 focused tests: QV restart confirmation and drain neutrality
# --------------------------------------------------------------------------


def detect(values):
    engine = detection.PhysicalEpisodeEngine()
    events = []
    for value in values:
        events.extend(engine.ingest(value))
    return engine, events


def _zeros(start, stop, **kwargs):
    return [_qv_sample(offset, 0, **kwargs) for offset in range(start, stop, 10)]


def assert_confirmed_qv(event):
    assert event.outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART, event.outcome
    assert event.recovery.stable_recovery_at is not None
    assert event.drain.zero_reached and event.drain.low_duration >= 45
    # The restart phase is the drain start, not the recovery.
    assert event.canonical_phase_at == event.drain.low_started_at
    assert event.coverage_complete and event.lifecycle_interruption is None
    assert scoring.event_learning_eligible(event)
    assert authority._authentic_event(event)
    assert scoring._event_weight(event) == 0.8


def test_s86_sustained_zero_then_one_queued_player_confirms_restart():
    _engine, events = detect([
        *_qv_drain_to_sustained_zero(),
        _qv_sample(210, 0, queue=1),
        _qv_sample(220, 0, queue=1),
    ])

    (event,) = events
    assert_confirmed_qv(event)
    assert event.recovery.first_queue_at == at(0, 9) + 210
    assert event.recovery.first_player_at is None
    assert event.recovery.stable_recovery_at == at(0, 9) + 220
    assert detection.QUEUE_RECOVERY_REASON in event.reason_codes
    assert detection.SUSTAINED_ZERO_RECOVERY_REASON in event.reason_codes


def test_s86_sustained_zero_then_one_returning_player_confirms_restart():
    _engine, events = detect([
        *_qv_drain_to_sustained_zero(),
        _qv_sample(210, 1),
        _qv_sample(220, 1),
    ])

    (event,) = events
    assert_confirmed_qv(event)
    assert event.recovery.first_player_at == at(0, 9) + 210
    assert event.recovery.first_queue_at is None
    assert event.recovery.positive_sample_count == 2


@pytest.mark.parametrize(
    "recovery",
    [
        pytest.param([_qv_sample(210, 0, queue=1)], id="single_queue_sample"),
        pytest.param([_qv_sample(210, 1)], id="single_player_sample"),
    ],
)
def test_s86_single_recovery_sample_does_not_confirm(recovery):
    _engine, events = detect([*_qv_drain_to_sustained_zero(), *recovery, *_zeros(220, 760)])

    assert len(events) == 1
    assert events[0].outcome is not Outcome.STRONG_QUERY_VISIBLE_RESTART
    assert not authority._authentic_event(events[0])
    assert scoring._event_weight(events[0]) == 0


def test_s86_queue_observed_before_sustained_zero_never_confirms():
    # A queued player appears 10 s into the zero, before the 45 s sustained
    # zero; later queue observations must not validate the candidate.
    values = [
        *_qv_drain_to_sustained_zero()[:9],
        _qv_sample(150, 0, queue=1),
        *_zeros(160, 210),
        _qv_sample(210, 0, queue=1),
        _qv_sample(220, 0, queue=1),
        *_zeros(230, 760),
    ]
    _engine, events = detect(values)

    (event,) = events
    assert event.outcome is Outcome.AMBIGUOUS_DRAIN
    assert event.recovery.stable_recovery_at is None


def test_s86_queue_populated_throughout_never_confirms():
    drain = [
        _qv_sample(offset, players, queue=3)
        for offset, players in zip((0, 10, 20, 40, 60, 80, 100, 120), (12, 12, 12, 11, 9, 7, 5, 3))
    ]
    _engine, events = detect([*drain, *_zeros(140, 760, queue=3)])

    (event,) = events
    assert event.outcome is Outcome.AMBIGUOUS_DRAIN
    assert event.recovery.stable_recovery_at is None


def test_s86_drain_without_queue_or_player_return_stays_unconfirmed():
    _engine, events = detect([*_qv_drain_to_sustained_zero(), *_zeros(210, 760)])

    (event,) = events
    assert event.outcome is Outcome.AMBIGUOUS_DRAIN
    assert "query_visible_repopulation_timeout" in event.reason_codes
    assert not authority._authentic_event(event)
    assert scoring._event_weight(event) == 0
    assert scoring._hint_event_weight(event) == 0


def test_s86_polling_gap_invalidates_the_candidate():
    values = [
        *_qv_drain_to_sustained_zero()[:-2],
        _qv_sample(260, 0),  # 70 s unobserved gap inside the zero
        _qv_sample(270, 0, queue=1),
        _qv_sample(280, 0, queue=1),
        *_zeros(290, 760),
    ]
    _engine, events = detect(values)

    assert not any(item.outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART for item in events)
    assert not any(authority._authentic_event(item) for item in events)


def test_s86_local_outage_lifecycle_boundary_invalidates_the_candidate():
    # The window neutralises local connectivity loss with a SLEEP_GAP
    # lifecycle boundary (Stages 3+4); recovery after it cannot confirm.
    boundary = replace(
        _qv_sample(205, 0), lifecycle=detection.LifecycleMarker.SLEEP_GAP
    )
    _engine, events = detect([
        *_qv_drain_to_sustained_zero(),
        boundary,
        _qv_sample(210, 0, queue=1),
        _qv_sample(220, 0, queue=1),
        _qv_sample(230, 1),
        _qv_sample(240, 1),
    ])

    assert not any(item.outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART for item in events)
    assert not any(
        scoring.event_learning_eligible(item) and authority._authentic_event(item)
        for item in events
    )


@pytest.mark.parametrize(
    "late",
    [
        pytest.param([_qv_sample(900, 0, queue=2), _qv_sample(910, 0, queue=2)], id="queue"),
        pytest.param([_qv_sample(900, 1), _qv_sample(910, 1)], id="player"),
    ],
)
def test_s86_late_return_after_candidate_expiry_never_confirms(late):
    _engine, events = detect([*_qv_drain_to_sustained_zero(), *_zeros(210, 760), *late])

    (event,) = events
    assert event.outcome is Outcome.AMBIGUOUS_DRAIN
    assert event.recovery.stable_recovery_at is None


def test_s86_ambiguous_drain_never_becomes_a_relationship_endpoint_but_still_blocks():
    pipeline = Pipeline()
    observe_mixed(
        pipeline,
        "mixed",
        [
            (at(0, 9), Outcome.CORROBORATED_OFFLINE_RESTART),
            (at(0, 12), Outcome.AMBIGUOUS_DRAIN),
            (at(0, 15), Outcome.CORROBORATED_OFFLINE_RESTART),
        ],
    )
    step = pipeline.evaluate()
    drain_id = next(item.event_id for item in step.events if item.outcome is Outcome.AMBIGUOUS_DRAIN)
    spanning = [item for item in step.relationships if item.left_at < at(0, 12) < item.right_at]

    assert all(drain_id not in (item.left_event_id, item.right_event_id) for item in step.relationships)
    assert spanning
    # Neutral blocking: an interval across the drain is never high evidence.
    assert not any(item.high_authority_eligible for item in spanning)
    assert all(
        "high_ineligible_intervening_unobservable_episode" in item.reason_codes
        or item.relationship_kind is not continuity.RelationshipKind.DIRECT
        for item in spanning
    )
    assert not step.high_direct(P3)
    assert step.decision.state is not State.ESTABLISHED


def test_s86_ambiguous_drains_add_no_learning_and_keep_rebuilds_stable():
    plain = sparse_pipeline()
    with_drains = plain.copy()
    with_drains.sparse("drain", [at(day, 3) for day in (7, 8, 9, 10, 11)], outcome=Outcome.AMBIGUOUS_DRAIN)
    before = settled(plain)
    after = settled(with_drains)
    again = with_drains.evaluate(persisted(after.decision))

    assert not any(
        item.event_id in {value.left_event_id for value in after.relationships}
        | {value.right_event_id for value in after.relationships}
        for item in after.events
        if item.outcome is Outcome.AMBIGUOUS_DRAIN
    )
    for period in scoring.CANDIDATE_PERIODS:
        # Hints from the genuine sparse restarts only; drains add none.
        assert after.score.candidate(period).hints == before.score.candidate(period).hints
        assert (
            after.score.candidate(period).fundamental_period_confidence
            == before.score.candidate(period).fundamental_period_confidence
        )
    assert after.decision.state is before.decision.state is State.ESTABLISHED
    assert after.decision.incumbent_shadow_regime.regime_id == before.decision.incumbent_shadow_regime.regime_id
    assert after.decision.combined_confidence == before.decision.combined_confidence
    assert panel(after)["confidence_percent"] == panel(before)["confidence_percent"] == 80
    assert again.decision == after.decision


def test_s86_queue_confirmed_restart_has_the_genuine_qv_learning_profile():
    """A queue-confirmed restart is a genuine strong-QV restart: the same
    outcome, weight and authenticity range the Stage 8.5 QV milestones use."""

    _engine, events = detect([
        *_qv_drain_to_sustained_zero(),
        _qv_sample(210, 0, queue=1),
        _qv_sample(220, 0, queue=1),
    ])
    (event,) = events

    assert event.outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART
    assert 0.84 <= event.authenticity <= 0.95
    assert scoring._event_weight(event) == scoring._event_weight(
        physical_event(1, at(0, 9), Session("s", at(0, 8), at(0, 10)), outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    )
    # Stage 8.5 milestones for genuine QV restarts are unchanged.
    sparse_step = settled(sparse_pipeline(SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART))
    assert panel(sparse_step)["confidence_percent"] == 80
    steps = _qv_trajectory()
    assert panel(steps[8])["confidence_percent"] == 80
    assert panel(steps[10])["confidence_percent"] == DISPLAY_CEILING_PERCENT


# --------------------------------------------------------------------------
# Stage 8.7 focused tests: learning-policy integration corrections
# --------------------------------------------------------------------------


def _off_phase_ids(step, period=P3):
    return {
        value
        for item in step.decision.normal_ledger.candidates
        if item.candidate_period_seconds == period
        for value in item.off_phase_event_ids
    }


def _three_hour_sparse(step):
    return max(
        (item for item in step.decision.candidates if item.candidate_period_seconds == P3),
        key=lambda item: item.aligned_normal_confidence,
    )


# Stage 8.7a: proportional anomaly tolerance (replaces the Stage 8.7
# single-recovered-anomaly rule).  Off-phase restarts stay evidence; they are
# netted against aligned restarts within the proportion the existing SE3 phase
# gate accepts, and scale the magnitude below it.

# Genuine one-off administrator restarts, off the 3 h grid and pairwise
# phase-incoherent at every candidate period.
ADMIN_RESTARTS = ((17, 20), (13, 47), (22, 8))
ADMIN_RESTART = at(3, 17, 20)


def aligned_3h(count, start_day=0):
    """``count`` sparse 3 h restarts, one per day, cycling all eight slots."""

    return tuple(at(start_day + index, (3 * index) % 24) for index in range(count))


def admin_restarts(count, days):
    return tuple(at(day, *ADMIN_RESTARTS[index % 3]) for index, day in zip(range(count), days))


def _anomaly_mid_history():
    """Nine aligned 3 h restarts with one admin restart after the fourth."""

    pipeline = Pipeline()
    pipeline.sparse("aligned-a", SPARSE_DISCRIMINATING[:4])
    pipeline.sparse("admin", [ADMIN_RESTART])
    pipeline.sparse("aligned-b", SPARSE_DISCRIMINATING[4:] + (at(7, 9),))
    return pipeline


def test_s87a_admin_restarts_are_pairwise_incoherent():
    events = [
        physical_event(index, at(20 + index, hour, minute), Session(f"c{index}", 0, 1), outcome=Outcome.CORROBORATED_OFFLINE_RESTART)
        for index, (hour, minute) in enumerate(ADMIN_RESTARTS)
    ]
    assert not any(
        authority._phase_coherent(left, right)
        for index, left in enumerate(events)
        for right in events[index + 1:]
    )
    assert authority.SPARSE_CONSISTENCY_TOLERANCE == pytest.approx(0.85 / 0.95)


def test_s87a_eight_perfectly_aligned_sparse_restarts_establish_normally():
    step = settled(sparse_pipeline())
    _value, shown = assert_sparse_established(step)
    candidate = _three_hour_sparse(step)

    assert shown["confidence_percent"] == 80
    # No off-phase restart: exactly the Stage 8.3/8.5 statistic.
    assert candidate.aligned_normal_confidence == 0.95
    assert authority.SPARSE_PROPORTIONAL_ANOMALY_REASON not in candidate.reason_codes


def test_s87a_one_anomaly_then_further_aligned_evidence_establishes():
    pipeline = sparse_pipeline()
    pipeline.sparse("admin", [at(6, 17, 20)])
    uncertain = settled(pipeline)
    assert uncertain.decision.state is State.NORMAL_LEARNING
    assert_learning(panel(uncertain))
    assert panel(uncertain)["confidence_percent"] == 76

    pipeline.sparse("ninth", [at(7, 9)])
    step = pipeline.evaluate(persisted(uncertain.decision))
    value, shown = assert_sparse_established(step)
    anomaly = next(item for item in step.events if item.canonical_phase_at == at(6, 17, 20))
    candidate = _three_hour_sparse(step)

    assert shown["confidence_percent"] == 80
    assert step.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_eligible
    assert authority.SPARSE_PROPORTIONAL_ANOMALY_REASON in candidate.reason_codes
    # The genuine off-phase restart is preserved as evidence and diagnostics.
    assert anomaly.event_id in _off_phase_ids(step)
    assert len(step.events) == 10
    assert not any(item.high_authority_eligible for item in step.relationships)


@pytest.mark.parametrize(
    "aligned_count,anomaly_count",
    [(9, 1), (20, 1), (20, 2), (50, 2), (98, 2)],
)
def test_s87a_occasional_anomalies_never_veto_a_strongly_supported_cycle(aligned_count, anomaly_count):
    pipeline = Pipeline()
    pipeline.sparse("aligned", aligned_3h(aligned_count))
    pipeline.sparse("admin", admin_restarts(anomaly_count, (2, 5)))
    step = settled(pipeline)
    value, shown = assert_sparse_established(step)
    regime = step.decision.incumbent_shadow_regime
    occurrence = value.next_expected_restart_at

    assert shown["confidence_percent"] == 80
    assert regime.candidate_period_seconds == P3 and regime.phase_offset == 0.0
    assert step.consumer(now=occurrence - 300).scheduled_warning_eligible
    assert len(_off_phase_ids(step) & {item.event_id for item in step.events}) >= anomaly_count


def test_s87a_large_histories_stay_usable_without_quadratic_pair_construction():
    count = 400
    pipeline = Pipeline()
    pipeline.sparse("aligned", aligned_3h(count))
    pipeline.sparse("admin", admin_restarts(6, (3, 50, 100, 150, 200, 300)))
    step = pipeline.evaluate()

    assert_sparse_established(step)
    # Relationship extraction is bounded per event (lookahead window), so it
    # grows linearly with the history.
    assert len(step.relationships) < count

    # The sparse statistic itself is one linear pass.  Twenty thousand aligned
    # restarts with 2 % unrelated admin restarts saturate at the milestone,
    # where the unchanged ratio statistic alone would be stuck below it.
    session = Session("bulk", 0, 1)
    base = physical_event(1, at(0, 0), session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART)
    events = tuple(
        replace(base, event_id=f"aligned-{index}", canonical_phase_at=at(index // 8, (3 * index) % 24))
        for index in range(20_000)
    ) + tuple(
        replace(base, event_id=f"admin-{index}", canonical_phase_at=at(5 * index + 1, 17, 20))
        for index in range(400)
    )
    support = authority._sparse_support(
        period=P3,
        phase_offset=0.0,
        sparse=authority._SparseEvidence(events=events, historical=()),
        excluded_event_ids=frozenset(),
    )
    assert support.value == 0.95
    assert round(authority.SPARSE_EVIDENCE_CEILING * support.value * 100) == 80
    assert support.phase_confidence == round(0.95 * 20_000 / 20_400, 6)
    assert round(authority.SPARSE_EVIDENCE_CEILING * support.phase_confidence * 100) < 80


def test_s87a_anomalies_before_and_after_initial_establishment():
    before = settled(_anomaly_mid_history())
    assert_sparse_established(before)
    assert panel(before)["confidence_percent"] == 80

    pipeline = _anomaly_mid_history()
    established_step = settled(pipeline)
    pipeline.sparse("admin-after", [at(9, 13, 47)])
    after = pipeline.evaluate(persisted(established_step.decision))
    regime = after.decision.incumbent_shadow_regime

    assert after.decision.state is State.CHANGE_SUSPECTED
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in after.decision.reason_codes
    assert regime.regime_id == established_step.decision.incumbent_shadow_regime.regime_id
    assert after.consumer().countdown_visible
    # Two anomalies against ten aligned restarts (17 %) exceed the tolerated
    # proportion: the cycle is kept, but below the 80 % warning milestone.
    pipeline.sparse("aligned-after", [at(10, 12)])
    partial = pipeline.evaluate(persisted(after.decision))
    assert partial.decision.state is State.ESTABLISHED and partial.consumer().countdown_visible
    assert panel(partial)["confidence_percent"] < 80
    # Further aligned evidence restores the proportion in place.
    pipeline.sparse("aligned-more", aligned_3h(8, start_day=11))
    recovered = pipeline.evaluate(persisted(partial.decision))
    assert_sparse_established(recovered)
    assert panel(recovered)["confidence_percent"] == 80
    assert recovered.decision.incumbent_shadow_regime.regime_id == regime.regime_id


def test_s87a_unrelated_anomalies_over_weeks_each_recover_without_reset():
    pipeline = Pipeline()
    pipeline.sparse("aligned", aligned_3h(24))
    steps = [settled(pipeline)]
    regime = steps[0].decision.incumbent_shadow_regime
    schedule = []
    for week, (hour, minute) in enumerate(ADMIN_RESTARTS):
        day = 28 + 7 * week
        schedule.append((f"admin-{week}", at(day, hour, minute), True))
        schedule += [
            (f"aligned-{week}-{index}", at(day + 1 + index, (3 * index) % 24), False)
            for index in range(4)
        ]
    for name, when, anomaly in schedule:
        pipeline.sparse(name, [when])
        step = pipeline.evaluate(persisted(steps[-1].decision))
        steps.append(step)
        current = step.decision.incumbent_shadow_regime
        assert current.regime_id == regime.regime_id
        assert current.established_at == regime.established_at
        assert (current.candidate_period_seconds, current.phase_offset) == (P3, 0.0)
        assert step.consumer().countdown_visible
        if anomaly:
            assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
        else:
            assert step.decision.state is State.ESTABLISHED
            assert panel(step)["confidence_percent"] == 80
    assert len(_off_phase_ids(steps[-1]) & {item.event_id for item in steps[-1].events}) == 3


@pytest.mark.parametrize("aligned_count,anomaly_count", [(10, 2), (20, 4), (30, 6)])
def test_s87a_persistent_significant_disagreement_prevents_establishment(aligned_count, anomaly_count):
    days = tuple(1 + (aligned_count // anomaly_count) * index for index in range(anomaly_count))
    pipeline = Pipeline()
    pipeline.sparse("aligned", aligned_3h(aligned_count))
    pipeline.sparse("admin", admin_restarts(anomaly_count, days))
    for step in rebuilds(pipeline, pipeline.evaluate()):
        candidate = _three_hour_sparse(step)
        assert step.decision.state is State.NORMAL_LEARNING
        assert not step.consumer().countdown_visible
        assert panel(step)["confidence_percent"] <= 79
        # The unchanged SE3 phase gate refuses this proportion.
        assert authority.SPARSE_STRUCTURE_REASON not in candidate.reason_codes
        assert authority.SPARSE_PROPORTIONAL_ANOMALY_REASON not in candidate.reason_codes

    # Proportion, not lifetime count, decides: enough later aligned evidence
    # brings the same anomalies within tolerance.
    pipeline.sparse("later", aligned_3h(aligned_count, start_day=aligned_count + 1))
    assert_sparse_established(settled(pipeline))


def _h2_incumbent():
    return three_hour_chain(3)


def _sparse_thirty():
    pipeline = Pipeline()
    pipeline.sparse("aligned", aligned_3h(30))
    return pipeline


INCUMBENT_BUILDERS = {
    "sparse_eight": (sparse_pipeline, 76),
    "sparse_thirty": (_sparse_thirty, 80),
    "two_interval_h2": (_h2_incumbent, 80),
    "three_interval_h3": (lambda: three_hour_chain(4), DISPLAY_CEILING_PERCENT),
}


@pytest.mark.parametrize("name", sorted(INCUMBENT_BUILDERS))
def test_s87a_established_regime_keeps_period_phase_and_predictions_after_isolated_admin_restart(name):
    builder, after_percent = INCUMBENT_BUILDERS[name]
    pipeline = builder()
    established_step = settled(pipeline)
    before = established_step.decision.incumbent_shadow_regime
    pipeline.sparse("admin", [at(40, 17, 20)])
    step = pipeline.evaluate(persisted(established_step.decision))
    value = step.consumer()
    regime = step.decision.incumbent_shadow_regime

    # Learning evidence, not a challenger: bounded level one, nothing erased.
    assert step.decision.state is State.CHANGE_SUSPECTED and step.decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
    assert (regime.regime_id, regime.candidate_period_seconds, regime.phase_offset) == (
        before.regime_id, before.candidate_period_seconds, before.phase_offset,
    )
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible and value.countdown_safe
    assert panel(step)["confidence_percent"] == after_percent
    warning = step.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_eligible
    # The displayed-80 % warning rule (8.3a) is unchanged.
    assert warning is (after_percent >= 80)

    # The next aligned restart clears the suspicion; the cycle recovers in
    # place (same regime, no re-acquisition).
    pipeline.sparse("aligned", [at(41, 9)])
    recovered = pipeline.evaluate(persisted(step.decision))
    assert recovered.decision.state is State.ESTABLISHED and recovered.decision.suspicion_level == 0
    assert recovered.decision.incumbent_shadow_regime.regime_id == before.regime_id
    assert percent(recovered) >= 80
    assert recovered.consumer(now=recovered.consumer().next_expected_restart_at - 300).scheduled_warning_eligible


@pytest.mark.parametrize(
    "second",
    [pytest.param((17, 20), id="same_clock"), pytest.param((14, 20), id="three_hour_alternative_phase")],
)
@pytest.mark.parametrize("builder", [_h2_incumbent, sparse_pipeline], ids=["h2", "sparse"])
def test_s87a_coherent_or_repeated_off_phase_restarts_keep_existing_suspicion(builder, second):
    # Coherent: a second off-grid restart sharing a phase with the first (the
    # same clock time, or the same alternative 3 h phase).  It is possible
    # alternative-schedule evidence, so the existing level-one treatment
    # (H2/sparse countdown withheld) applies.
    pipeline = builder()
    steps = [settled(pipeline)]
    for name, when in (("admin-1", at(10, 17, 20)), ("aligned-1", at(11, 9)), ("admin-2", at(12, *second))):
        pipeline.sparse(name, [when])
        steps.append(pipeline.evaluate(persisted(steps[-1].decision)))
    coherent = steps[-1]
    assert coherent.decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in coherent.decision.reason_codes
    assert not coherent.consumer().countdown_visible
    pipeline.sparse("aligned-2", [at(13, 9)])
    recovered = pipeline.evaluate(persisted(coherent.decision))
    assert recovered.decision.state is State.ESTABLISHED and recovered.consumer().countdown_visible

    # Two off-phase restarts with no aligned restart between them.
    pipeline = builder()
    steps = [settled(pipeline)]
    for name, when in (("admin-1", at(10, 17, 20)), ("admin-2", at(11, 13, 47))):
        pipeline.sparse(name, [when])
        steps.append(pipeline.evaluate(persisted(steps[-1].decision)))
    assert steps[-1].decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in steps[-1].decision.reason_codes
    assert not steps[-1].consumer().countdown_visible


def test_s87a_isolated_admin_restart_never_relaxes_other_suspicion():
    # An in-phase genuine miss (not an off-phase restart) keeps the existing
    # H2 treatment: level one withholds the H2 countdown.
    pipeline = _h2_incumbent()
    established_step = settled(pipeline)
    ledger = (genuine_miss(P3, at(1, 0), key="incumbent-miss"),)
    missed = pipeline.evaluate(persisted(established_step.decision), ledger=ledger)
    assert missed.decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in missed.decision.reason_codes
    assert not missed.consumer().countdown_visible


def test_s87a_genuine_recent_challenger_is_detected_despite_a_large_incumbent_history():
    def with_challenger(*sessions):
        pipeline = Pipeline()
        pipeline.sparse("history", aligned_3h(100))
        incumbent = settled(pipeline)
        assert_sparse_established(incumbent)
        for index, times in enumerate(sessions):
            pipeline.observe(f"challenger-{index}", times)
        return incumbent, pipeline.evaluate(persisted(incumbent.decision))

    _incumbent, level_two = with_challenger([at(101, 1), at(101, 5)], [at(101, 13), at(101, 17)])
    assert level_two.decision.state is State.CHALLENGER_ACCUMULATING and level_two.decision.suspicion_level == 2
    assert level_two.consumer().presentation_state is Presentation.SCHEDULE_CHANGE_SUSPECTED
    assert not level_two.consumer().countdown_visible

    incumbent, provisional = with_challenger([at(101, 1), at(101, 5), at(101, 9)])
    assert provisional.decision.state is State.TRANSITION_CONFIRMED
    assert provisional.consumer().presentation_state is Presentation.LIKELY_NEW_CYCLE
    assert not provisional.consumer().countdown_visible
    assert provisional.decision.incumbent_shadow_regime.regime_id == incumbent.decision.incumbent_shadow_regime.regime_id

    _incumbent, superseded = with_challenger([at(101, 1), at(101, 5), at(101, 9), at(101, 13)])
    assert superseded.decision.state is State.NEW_REGIME_ESTABLISHED
    assert superseded.decision.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR


def test_s87a_continuous_versus_sparse_conflict_safeguard_holds_with_large_sparse_history():
    pipeline = Pipeline()
    pipeline.sparse("history", aligned_3h(100))
    pipeline.observe("four-hour", [at(101, 8), at(101, 12), at(101, 16)])
    pending = settled(pipeline)
    assert pending.decision.state is State.LIKELY
    assert authority.TWO_INTERVAL_SPARSE_CONFLICT_REASON in pending.decision.reason_codes
    assert not pending.consumer().countdown_visible

    pipeline.sparse("confirmation", [at(102, 8)])
    step = pipeline.evaluate(persisted(pending.decision))
    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert step.decision.incumbent_shadow_regime.regime_id == pending.decision.selected_shadow_regime.regime_id


def test_s87a_clean_milestones_and_evidence_weights_are_unchanged():
    clean = _three_hour_sparse(settled(sparse_pipeline()))
    assert clean.aligned_normal_confidence == 0.95
    assert panel(_sparse_plus_matching_interval()[1])["confidence_percent"] == DISPLAY_CEILING_PERCENT
    qv = settled(sparse_pipeline(SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART))
    assert panel(qv)["confidence_percent"] == 80
    assert panel(settled(sparse_pipeline(SPARSE_QV_TEN[:9], outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)))[
        "confidence_percent"
    ] <= 79
    assert percent(three_hour_chain(3).evaluate()) == 80
    assert percent(three_hour_chain(4).evaluate()) == DISPLAY_CEILING_PERCENT
    session = Session("w", 0, 1)
    weights = {
        outcome: scoring._event_weight(physical_event(1, at(0, 0), session, outcome=outcome))
        for outcome in _AUTHENTICITY
    }
    assert weights == {
        Outcome.CORROBORATED_OFFLINE_RESTART: 1.0,
        Outcome.CONFIRMED_OFFLINE_RESTART: 0.8,
        Outcome.STRONG_QUERY_VISIBLE_RESTART: 0.8,
        Outcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.5,
        Outcome.AMBIGUOUS_DRAIN: 0.0,
    }


@pytest.mark.parametrize("contamination", ["sleep_gap", "onset_unobserved"])
def test_s87a_unobserved_or_interrupted_off_phase_incidents_stay_neutral(contamination):
    pipeline = sparse_pipeline()
    clean = settled(pipeline)
    (event,) = pipeline.sparse("incident", [at(6, 17, 20)])
    if contamination == "sleep_gap":
        changed = replace(event, coverage_complete=False, lifecycle_interruption=detection.LifecycleMarker.SLEEP_GAP)
    else:
        changed = replace(event, coverage_complete=False)
    pipeline.events[-1] = changed
    step = settled(pipeline)

    assert not scoring.event_learning_eligible(changed)
    assert_sparse_established(step)
    assert _three_hour_sparse(step).aligned_normal_confidence == _three_hour_sparse(clean).aligned_normal_confidence


def test_s87a_warning_deduplication_and_regime_identity_survive_an_anomaly():
    pipeline = _sparse_thirty()
    established_step = settled(pipeline)
    occurrence = established_step.consumer().next_expected_restart_at
    due = occurrence - 300
    first = established_step.consumer(now=due)
    assert first.scheduled_pre_restart_alert_eligible
    fired = frozenset({first.scheduled_warning_suppression_key})

    # An isolated admin restart between the last aligned restart and the
    # warning: same regime, same occurrence key, already fired.
    pipeline.sparse("admin", [occurrence - 100 * 60])
    step = pipeline.evaluate(persisted(established_step.decision))
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON in step.decision.reason_codes
    again = step.consumer(now=due, fired_keys=fired)
    assert again.scheduled_warning_suppression_key == first.scheduled_warning_suppression_key
    assert not again.scheduled_pre_restart_alert_eligible
    following = step.consumer(now=due + P3, fired_keys=fired)
    assert following.scheduled_warning_suppression_key != first.scheduled_warning_suppression_key
    assert following.scheduled_pre_restart_alert_eligible


FOUR_HOUR_STREAK = (at(9, 8), at(9, 12), at(9, 16))


def _sparse_conflict_pipeline(sparse_times=SPARSE_DISCRIMINATING, **kwargs):
    """Substantial 3 h sparse history, then a clean 4 h two-interval streak."""

    pipeline = Pipeline()
    if sparse_times:
        pipeline.sparse("sparse", sparse_times, **kwargs)
    pipeline.observe("four-hour", FOUR_HOUR_STREAK)
    return pipeline


def assert_two_interval_established(step, period=4 * HOUR):
    regime = step.decision.incumbent_shadow_regime
    assert step.decision.state is State.ESTABLISHED
    assert regime.candidate_period_seconds == period
    assert regime.origin is Origin.CONTINUITY
    assert step.consumer().countdown_visible
    return regime


def assert_conflict_pending(step):
    value = step.consumer()
    assert step.decision.state is State.LIKELY
    assert step.decision.selected_shadow_regime.candidate_period_seconds == 4 * HOUR
    assert authority.TWO_INTERVAL_SPARSE_CONFLICT_REASON in step.decision.reason_codes
    assert not value.countdown_visible and not value.scheduled_warning_eligible
    assert_learning(panel(step))
    assert panel(step)["confidence_percent"] <= 79


@pytest.mark.parametrize(
    "sparse_times",
    [pytest.param((), id="no_sparse_history"), pytest.param(SPARSE_DISCRIMINATING[:4], id="four_sparse")],
)
def test_s87_two_clean_intervals_without_substantial_sparse_conflict_establish_immediately(sparse_times):
    first = _sparse_conflict_pipeline(sparse_times).evaluate()

    assert first.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    regime = assert_two_interval_established(first)
    assert regime.established_at == FOUR_HOUR_STREAK[-1]
    assert 80 <= percent(first) < DISPLAY_CEILING_PERCENT


def test_s87_two_intervals_against_substantial_sparse_conflict_remain_provisional():
    pipeline = _sparse_conflict_pipeline()
    first = pipeline.evaluate()
    steps = rebuilds(pipeline, first)
    four = first.candidate(4 * HOUR)

    # The streak itself is unchanged strong evidence (H2, two-interval quality).
    assert four.h2_gate and four.phase_quality >= authority.TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM
    assert four.high_authority == 0.80
    for step in steps:
        assert_conflict_pending(step)
    assert len({item.decision.selected_shadow_regime.regime_id for item in steps}) == 1
    assert steps[-1].decision == steps[-2].decision


@pytest.mark.parametrize(
    "confirmation",
    [
        pytest.param(at(10, 8), id="previously_seen_clock_08"),
        pytest.param(at(11, 16), id="previously_seen_clock_16"),
        pytest.param(at(12, 0), id="new_clock_00"),
    ],
)
def test_s87_conflict_plus_one_independent_matching_sparse_restart_establishes(confirmation):
    pipeline = _sparse_conflict_pipeline()
    pending = settled(pipeline)
    assert_conflict_pending(pending)
    pipeline.sparse("confirmation", [confirmation])
    step = pipeline.evaluate(persisted(pending.decision))
    again = pipeline.evaluate(persisted(step.decision))

    regime = assert_two_interval_established(step)
    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert regime.regime_id == pending.decision.selected_shadow_regime.regime_id
    assert regime.established_at == FOUR_HOUR_STREAK[-1]
    # No interval was inferred across the gap to the confirming restart.
    assert len(step.high_direct(4 * HOUR)) == 2
    assert 80 <= percent(step) < DISPLAY_CEILING_PERCENT
    assert again.decision.incumbent_shadow_regime.regime_id == regime.regime_id
    assert again.decision.state is State.ESTABLISHED


def test_s87_conflict_plus_third_consecutive_interval_establishes():
    pipeline = _sparse_conflict_pipeline()
    pending = settled(pipeline)
    assert_conflict_pending(pending)
    # The same monitoring session continues and observes the next restart.
    session = Session("four-hour", FOUR_HOUR_STREAK[0] - 20 * 60, at(9, 20) + 20 * 60)
    pipeline.coverage[-1] = session.coverage()
    pipeline.events.append(
        physical_event(pipeline.sequence, at(9, 20), session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART)
    )
    step = pipeline.evaluate(persisted(pending.decision))

    assert_two_interval_established(step)
    assert percent(step) == DISPLAY_CEILING_PERCENT
    assert authority.TWO_INTERVAL_SPARSE_CONFLICT_REASON not in step.decision.reason_codes


def test_s87_already_counted_endpoint_never_satisfies_the_extra_confirmation():
    pipeline = _sparse_conflict_pipeline()
    pending = settled(pipeline)
    endpoint = next(item for item in pending.events if item.canonical_phase_at == FOUR_HOUR_STREAK[-1])

    # The same physical restart delivered again counts once.
    duplicate = pipeline.copy()
    duplicate.events.append(endpoint)
    assert_conflict_pending(duplicate.evaluate(persisted(pending.decision)))

    # The same endpoint re-observed under another identity is not distinct.
    reobserved = pipeline.copy()
    reobserved.sparse("endpoint-again", [FOUR_HOUR_STREAK[-1]])
    step = reobserved.evaluate(persisted(pending.decision))
    assert step.decision.state is not State.ESTABLISHED
    assert not step.consumer().countdown_visible

    # A restart before the streak (aligned with both schedules) is part of
    # the conflicting history, not a new confirmation.
    assert any(
        abs(((item.canonical_phase_at + 2 * HOUR) % (4 * HOUR)) - 2 * HOUR) < 60
        for item in pending.events
        if item.canonical_phase_at < FOUR_HOUR_STREAK[0]
    )


@pytest.mark.parametrize(
    "sparse_times,outcome,ledger",
    [
        pytest.param(SPARSE_DISCRIMINATING[:4], Outcome.CORROBORATED_OFFLINE_RESTART, (), id="insufficient"),
        pytest.param(SAME_CLOCK, Outcome.CORROBORATED_OFFLINE_RESTART, (), id="ambiguous_same_clock"),
        pytest.param(SPARSE_DISCRIMINATING, Outcome.AMBIGUOUS_DRAIN, (), id="unconfirmed_drains"),
        pytest.param(SPARSE_DISCRIMINATING, Outcome.PROBABLE_QUERY_VISIBLE_RESTART, (), id="weak_probable_qv"),
        pytest.param(
            SPARSE_DISCRIMINATING,
            Outcome.CORROBORATED_OFFLINE_RESTART,
            (genuine_miss(P3, at(7, 0), key="three-hour-miss"),),
            id="contradicted_by_genuine_miss",
        ),
    ],
)
def test_s87_weak_ambiguous_or_contradicted_sparse_evidence_creates_no_conflict_gate(sparse_times, outcome, ledger):
    step = _sparse_conflict_pipeline(sparse_times, outcome=outcome).evaluate(ledger=ledger)

    assert step.decision.reason_codes == (authority.TWO_INTERVAL_ESTABLISHMENT_REASON,)
    assert_two_interval_established(step)


def test_s87_established_regimes_keep_stage6_challenger_and_recovery_rules():
    # A sparse-established 3 h incumbent: a 4 h two-interval challenger is
    # judged by the unchanged Stage 6 rules (provisional, countdown hidden),
    # never by the acquisition-only conflict gate.
    pipeline = sparse_pipeline()
    incumbent = settled(pipeline)
    pipeline.observe("challenger", [at(9, 1), at(9, 5), at(9, 9)])
    provisional = pipeline.evaluate(persisted(incumbent.decision))
    assert provisional.decision.state is State.TRANSITION_CONFIRMED
    assert provisional.consumer().presentation_state is Presentation.LIKELY_NEW_CYCLE
    assert not provisional.consumer().countdown_visible
    assert authority.TWO_INTERVAL_SPARSE_CONFLICT_REASON not in provisional.decision.reason_codes

    # Two independent complete 3 h intervals after the challenger resolve it
    # in favour of the original cycle (Stage 6 recovery).
    pipeline.observe("recovery", [at(10, 3), at(10, 6), at(10, 9)])
    recovered = pipeline.evaluate(persisted(provisional.decision))
    assert recovered.decision.state is State.ESTABLISHED
    assert recovered.decision.incumbent_shadow_regime.regime_id == incumbent.decision.incumbent_shadow_regime.regime_id
    assert recovered.consumer().countdown_visible

    # A continuous incumbent with a three-interval challenger supersedes by H3.
    pipeline = three_hour_chain(4)
    original = settled(pipeline)
    pipeline.observe("challenger", [at(9, 1), at(9, 5), at(9, 9), at(9, 13)])
    superseded = pipeline.evaluate(persisted(original.decision))
    assert superseded.decision.state is State.NEW_REGIME_ESTABLISHED
    assert superseded.decision.incumbent_shadow_regime.candidate_period_seconds == 4 * HOUR


NON_AUTHENTIC_OUTCOMES = (
    Outcome.AMBIGUOUS_DRAIN,
    Outcome.UNCERTAIN_A2S_INTERRUPTION,
    Outcome.SERVICE_INTERRUPTION,
    Outcome.INCOMPLETE,
    Outcome.EXPIRED,
)


def non_authentic_event(sequence, when, session, *, outcome):
    """A coverage-complete, learning-eligible but non-authentic incident."""

    base = physical_event(sequence, when, session, outcome=Outcome.AMBIGUOUS_DRAIN)
    return replace(base, outcome=outcome, authenticity=0.20, schedule_weight_suggestion=0.20)


def _observe_incidents(pipeline, name, times, outcome):
    session = Session(name, min(times) - 20 * 60, max(times) + 20 * 60)
    pipeline.coverage.append(session.coverage())
    values = []
    for when in times:
        values.append(non_authentic_event(pipeline.sequence, when, session, outcome=outcome))
        pipeline.sequence += 1
    pipeline.events.extend(values)
    return values


@pytest.mark.parametrize("outcome", NON_AUTHENTIC_OUTCOMES, ids=lambda item: item.name.lower())
def test_s87_non_authentic_outcomes_are_never_relationship_endpoints_but_still_block(outcome):
    pipeline = Pipeline()
    session = Session("mixed", at(0, 8), at(0, 16))
    pipeline.coverage.append(session.coverage())
    pipeline.events.extend(
        [
            physical_event(1, at(0, 9), session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART),
            non_authentic_event(2, at(0, 12), session, outcome=outcome),
            physical_event(3, at(0, 15), session, outcome=Outcome.CORROBORATED_OFFLINE_RESTART),
        ]
    )
    pipeline.sequence = 4
    step = pipeline.evaluate()
    incident = next(item for item in step.events if item.outcome is outcome)
    spanning = [item for item in step.relationships if item.left_at < at(0, 12) < item.right_at]

    assert scoring.event_learning_eligible(incident)
    assert all(incident.event_id not in (item.left_event_id, item.right_event_id) for item in step.relationships)
    assert not step.high_direct(P3)
    assert step.decision.state is not State.ESTABLISHED
    # Diagnostics keep the incident.
    assert incident in step.events
    assert spanning
    if outcome is Outcome.UNCERTAIN_A2S_INTERRUPTION:
        # An uncertain single-poll blip keeps its existing neutral role: the
        # spanning interval is judged exactly as without it.
        without = Pipeline(
            [item for item in pipeline.events if item.outcome is not outcome],
            list(pipeline.coverage),
            pipeline.sequence,
        ).evaluate()
        assert [item.high_authority_eligible for item in spanning] == [
            item.high_authority_eligible
            for item in without.relationships
            if item.left_at < at(0, 12) < item.right_at
        ]
    else:
        # Neutral blocking: an interval across the incident is never high.
        assert not any(item.high_authority_eligible for item in spanning)
        assert all(
            "high_ineligible_intervening_unobservable_episode" in item.reason_codes
            for item in spanning
            if item.relationship_kind is continuity.RelationshipKind.DIRECT
        )


@pytest.mark.parametrize("outcome", NON_AUTHENTIC_OUTCOMES[1:], ids=lambda item: item.name.lower())
def test_s87_legacy_non_authentic_relationships_add_no_challenger_evidence(outcome, monkeypatch):
    pipeline = three_hour_chain(4)
    established_step = settled(pipeline)
    incidents = _observe_incidents(pipeline, "incidents", [at(2, hour) for hour in (2, 6, 10, 14)], outcome)
    incident_ids = {item.event_id for item in incidents}
    current = pipeline.evaluate(persisted(established_step.decision))

    # Relationships extracted before Stage 8.7 (still persisted, immutable).
    monkeypatch.setattr(
        continuity,
        "relationship_endpoint_outcome_eligible",
        lambda event: event.outcome is not Outcome.AMBIGUOUS_DRAIN,
    )
    legacy = pipeline.evaluate(persisted(established_step.decision))
    monkeypatch.undo()
    legacy_endpoints = [
        item for item in legacy.relationships if {item.left_event_id, item.right_event_id} & incident_ids
    ]
    assert legacy_endpoints and not any(item.high_authority_eligible for item in legacy_endpoints)

    # Authority never reads them: identical decision, no latent challenger.
    assert legacy.decision == current.decision
    assert legacy.decision.state is State.ESTABLISHED
    assert legacy.decision.suspicion_level == 0
    assert all(
        not set(item.normal_relationship_ids) & {value.relationship_id for value in legacy_endpoints}
        for item in legacy.decision.challenger_contexts
    )
    assert legacy.consumer().countdown_visible
    # Without the authority filter they would raise a latent challenger.
    monkeypatch.setattr(authority, "relationship_endpoint_outcome_eligible", lambda event: True)
    unfiltered = authority.evaluate_authority(
        server_key=SERVER,
        normal_score=legacy.score,
        relationships=legacy.relationships,
        streaks=legacy.streaks,
        events=legacy.events,
        prior_decision=persisted(established_step.decision),
    )
    assert unfiltered.suspicion_level == 1
    assert "latent_normal_challenger_context" in unfiltered.reason_codes


def test_s87_genuine_qv_and_oo_events_still_form_relationships():
    pipeline = Pipeline()
    observe_mixed(
        pipeline,
        "genuine",
        [
            (at(0, 3), Outcome.CORROBORATED_OFFLINE_RESTART),
            (at(0, 6), Outcome.CONFIRMED_OFFLINE_RESTART),
            (at(0, 9), Outcome.STRONG_QUERY_VISIBLE_RESTART),
            (at(0, 12), Outcome.PROBABLE_QUERY_VISIBLE_RESTART),
            (at(0, 15), Outcome.STRONG_QUERY_VISIBLE_RESTART),
        ],
    )
    step = pipeline.evaluate()
    endpoints = {value for item in step.relationships for value in (item.left_event_id, item.right_event_id)}
    direct = [
        item for item in step.relationships
        if item.candidate_period_seconds == P3 and item.relationship_kind is continuity.RelationshipKind.DIRECT
    ]

    assert {item.event_id for item in step.events} <= endpoints
    assert len(direct) == 4
    assert [item.high_authority_eligible for item in sorted(direct, key=lambda item: item.left_at)] == [
        True, False, False, False,
    ]
    # Continuous QV and OO trajectories keep their milestones.
    assert percent(three_hour_chain(3).evaluate()) == 80
    assert panel(_qv_trajectory()[10])["confidence_percent"] == DISPLAY_CEILING_PERCENT


STAGE87_BUILDERS = {
    "proportional_anomaly": _anomaly_mid_history,
    "isolated_admin_after_establishment": lambda: _with(_sparse_thirty(), "admin", [at(40, 17, 20)]),
    "conflict_pending": _sparse_conflict_pipeline,
    "conflict_confirmed": lambda: _with(_sparse_conflict_pipeline(), "confirmation", [at(10, 8)]),
    "legacy_incidents": lambda: _with_incidents(three_hour_chain(4)),
}


def _with(pipeline, name, times):
    pipeline.sparse(name, times)
    return pipeline


def _with_incidents(pipeline):
    _observe_incidents(pipeline, "incidents", [at(2, hour) for hour in (2, 6, 10, 14)], Outcome.SERVICE_INTERRUPTION)
    return pipeline


@pytest.mark.parametrize("name", sorted(STAGE87_BUILDERS))
def test_s87_persistence_and_rebuild_are_stable(name):
    pipeline = STAGE87_BUILDERS[name]()
    first = pipeline.evaluate()
    second = pipeline.evaluate(persisted(first.decision))
    third = pipeline.evaluate(persisted(second.decision))
    fourth = pipeline.evaluate(persisted(third.decision))

    assert third.decision == fourth.decision
    assert persisted(third.decision) == third.decision
    assert (
        second.decision.selected_shadow_regime is None
        or second.decision.selected_shadow_regime.regime_id == fourth.decision.selected_shadow_regime.regime_id
    )
    assert panel(third) == panel(fourth)


def test_s87_existing_milestones_warnings_and_presentation_are_unchanged():
    # Sparse: eight strong restarts 80 %, plus one full interval 95 %.
    assert panel(settled(sparse_pipeline()))["confidence_percent"] == 80
    assert panel(_sparse_plus_matching_interval()[1])["confidence_percent"] == DISPLAY_CEILING_PERCENT
    assert panel(settled(sparse_pipeline(SPARSE_QV_TEN, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)))[
        "confidence_percent"
    ] == 80
    # Continuous: two intervals 80 % with warning, three 95 %.
    two = three_hour_chain(3).evaluate()
    assert percent(two) == 80 and two.decision.combined_confidence == 0.80
    occurrence = two.consumer().next_expected_restart_at
    assert two.consumer(now=occurrence - 300).scheduled_warning_eligible
    assert percent(three_hour_chain(4).evaluate()) == DISPLAY_CEILING_PERCENT
    # Calibration constants.
    assert authority.SPARSE_EVIDENCE_CEILING == 0.84
    assert authority.STREAK_BONUS_COEFFICIENT == 0.10
    assert authority.GATE_STREAK_BONUS_COEFFICIENT == 0.08
    assert authority.TWO_INTERVAL_ESTABLISHMENT_PHASE_QUALITY_MINIMUM == 0.909
    assert consumers4.SCHEDULED_WARNING_MINIMUM_CONFIDENCE_PERCENT == 80
    assert authority.ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS == 2
