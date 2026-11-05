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
  implementation stage (``STAGE_8_5`` and so on).  ``strict=True`` makes an
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

# Planned implementation stages (design audit section 13).
STAGE_8_5 = (
    "Stage 8.5 (sparse ceiling S and SE1-SE7 sparse establishment; "
    "needs the Stage 8.3 long-horizon per-event sparse input)"
)
STAGE_8_6 = "Stage 8.6 (QV refinements: queue return, ambiguous-drain learning)"

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


def consume(step, *, now, restart_alert_enabled=True, fired_keys=frozenset(), physical_recovery_event_id=None):
    """The schema-4 consumer with runtime-equivalent normal-scorer inputs.

    Mirrors ``Phase2RestartRuntime._evaluate_authority_consumer_shadow`` and
    the schema-3 consumer input it is derived from.
    """

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


@expected_failure(STAGE_8_5, "sparse-only evidence never establishes; the state stays NORMAL_LEARNING with no countdown")
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


@expected_failure(STAGE_8_5, "a complete interval on top of sparse evidence stays H1/NORMAL_LEARNING; no establishment, no 95 %")
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


@expected_failure(STAGE_8_6, "a queue >= 1 return after a sustained zero is ignored; the episode times out as AMBIGUOUS_DRAIN")
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


@expected_failure(STAGE_8_6, "learning-eligible AMBIGUOUS_DRAIN events become schema-4 interval-relationship endpoints (normal-only, persisted)")
def test_s12_unconfirmed_drains_are_not_interval_relationship_endpoints():
    pipeline = Pipeline()
    pipeline.sparse("drain", SPARSE_DISCRIMINATING, outcome=Outcome.AMBIGUOUS_DRAIN)
    step = pipeline.evaluate()

    assert step.relationships == (), len(step.relationships)


@expected_failure(STAGE_8_6, "AMBIGUOUS_DRAIN keeps an eligible hint weight; aligned drains raise the 3 h fundamental 0.45 -> 0.48 and combined 0.809 -> 0.8096")
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


@expected_failure(STAGE_8_5, "QV gets a countdown only once schema-3 schedule existence is 0.95; no QV state is established at the 80 % milestone")
def test_s14_continuous_qv_reaches_80_percent_milestone_before_95():
    observed = [
        (step.consumer().countdown_visible, percent(step)) for step in _qv_trajectory()
    ]

    assert any(
        countdown and shown is not None and 80 <= shown < DISPLAY_CEILING_PERCENT
        for countdown, shown in observed
    ), observed
    assert observed[-1] == (True, DISPLAY_CEILING_PERCENT)


@expected_failure(STAGE_8_5, "sparse QV evidence never establishes (no relationships, NORMAL_LEARNING)")
def test_s14_sparse_qv_establishes_at_80_percent():
    step = settled(sparse_pipeline(outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART))
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
    "normal_origin_qv": lambda: three_hour_chain(9, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
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
    "normal_origin_qv": lambda: three_hour_chain(9, outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART),
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
    assert value.presentation_state is Presentation.LIKELY_CYCLE
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
    pipeline = three_hour_chain(9, name="qv", outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
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
    pipeline = three_hour_chain(9, name="qv", outcome=Outcome.STRONG_QUERY_VISIBLE_RESTART)
    normal = settled(pipeline)
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
