"""Retention R3: schema-4 authority evaluated from compact learned state.

Two comparisons, synthetic data only:

* The production rebuild path (``rebuild_schema4_server_record``) run with
  the raw-history authority and with ``COMPACT_AUTHORITY_ENABLED``: decisions
  and consumer outputs must agree at every rebuild.
* A compact-only runner that never keeps folded raw history (the ring, the
  learned state and the bounded schema-3 horizon only), reopening its JSON
  state at every step, against the full-history raw authority.

No runtime, profile or learning file is touched.
"""

from __future__ import annotations

import json
import random

import pytest

import test_companion_restart_learned_state as compact_tests
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_expected_windows as expected_windows
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P3 = 3 * HOUR
P4 = 4 * HOUR
Outcome = s8.Outcome


# --------------------------------------------------------------------------
# Summaries compared between raw and compact authority
# --------------------------------------------------------------------------


def decision_summary(decision):
    incumbent = decision.incumbent_shadow_regime
    selected = decision.selected_shadow_regime

    def regime(value):
        if value is None:
            return None
        return (
            value.candidate_period_seconds,
            round(value.phase_offset),
            value.status.value,
            value.origin.value,
            value.authority_gate.value,
            value.established_at,
        )

    learning = decision.state.value in {"normal_learning", "unknown"}
    return {
        "state": decision.state.value,
        "prediction_usable": decision.prediction_usable,
        "countdown_safe": decision.countdown_safe,
        "suspicion_level": decision.suspicion_level,
        # Without a selected regime, ``combined_confidence`` is the value of
        # ``ranked[0]``, whose final tie-break among equally ranked
        # never-established candidates is the regime-ID string (hash order).
        # Compact identities change that arbitrary pick, never the displayed
        # Learning confidence (the consumer ranks by combined confidence
        # first), which ``consumer_summary`` compares.
        "combined": None if learning else round(decision.combined_confidence, 6),
        "best_learning": round(
            max((item.combined_confidence for item in decision.candidates), default=0.0), 6
        ),
        "incumbent": regime(incumbent),
        "selected": regime(selected),
    }


def consumer_summary(step):
    value = s8.consume(step, now=step.now)
    warning = None
    if value.next_expected_restart_at is not None:
        before = s8.consume(step, now=value.next_expected_restart_at - 300)
        warning = (
            before.scheduled_warning_eligible,
            None
            if before.scheduled_warning_suppression_key is None
            else before.scheduled_warning_suppression_key.expected_occurrence_at,
        )
    return {
        "presentation": value.presentation_state.value,
        "visible": value.visible_cycle,
        "period": value.cycle_period_seconds,
        "countdown": (value.countdown_visible, value.countdown_safe),
        "suspended": value.prediction_suspended,
        "percent": consumers4.confidence_display_percent(value.confidence_display_value),
        "next": value.next_expected_restart_at,
        "warning": warning,
    }


def warning_key(step):
    value = s8.consume(step, now=step.now)
    if value.next_expected_restart_at is None:
        return None
    return s8.consume(step, now=value.next_expected_restart_at - 300).scheduled_warning_suppression_key


# --------------------------------------------------------------------------
# Production rebuild driver (raw / compact / cutover)
# --------------------------------------------------------------------------


def run(pipeline, *, every, compact_from=None, monkeypatch):
    """Rebuild after every ``every`` restarts; compact authority from step
    ``compact_from`` on (``None``: raw throughout).  Returns the record after
    each rebuild (the last one is an identical settle rebuild)."""

    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    stops = list(range(every, len(ordered), every)) + [len(ordered), len(ordered)]
    record = {}
    records = []
    for index, stop in enumerate(stops):
        monkeypatch.setattr(
            schema4, "COMPACT_AUTHORITY_ENABLED", compact_from is not None and index >= compact_from
        )
        prefix = ordered[:stop]
        chains = {item.continuity_chain_id for item in prefix}
        coverage = [item for item in pipeline.coverage if item.continuity_chain_id in chains]
        record = integration.rebuild(record, prefix, coverage)
        records.append(record)
    return records


def _sparse_eight():
    return s8.sparse_pipeline()


def _sparse_then_interval():
    return compact_tests._sparse_then_interval()


def _long_sparse_with_admin():
    pipeline = s8.Pipeline()
    pipeline.sparse("aligned", s8.aligned_3h(100))
    pipeline.sparse("admin", [s8.at(101, 17, 20)])
    pipeline.sparse("after", s8.aligned_3h(3, start_day=102))
    return pipeline


def _schedule_change():
    pipeline = s8.Pipeline()
    pipeline.observe("three", [s8.at(0, 0) + index * P3 for index in range(100)])
    pipeline.observe("four", [s8.at(14, 2) + index * P4 for index in range(30)])
    return pipeline


def _stage6_recovery():
    pipeline = s8.three_hour_chain(4)
    pipeline.observe("four", [s8.at(1, 10), s8.at(1, 14), s8.at(1, 18)])
    pipeline.observe("back", [s8.at(2, 9) + index * P3 for index in range(4)])
    return pipeline


def _boundary_challenger():
    pipeline = s8._boundary_pipeline()
    pipeline.observe("new-four-hour", [s8.at(1, 10), s8.at(1, 14)])
    return pipeline


SCENARIOS = {
    "eight_sparse": (_sparse_eight, 1),
    "sparse_then_interval": (_sparse_then_interval, 1),
    "two_intervals": (lambda: s8.three_hour_chain(3), 1),
    "three_intervals": (lambda: s8.three_hour_chain(4), 1),
    "anomaly_mid_history": (s8._anomaly_mid_history, 1),
    "boundary_challenger": (_boundary_challenger, 1),
    "stage6_recovery": (_stage6_recovery, 1),
    "long_chain": (integration._long_chain, 10),
    "sparse_with_anomalies": (integration._sparse_with_anomalies, 10),
    "repeated_slots": (integration._repeated_slots, 10),
    "qv_sparse": (integration._qv_sparse, 10),
    "mixed_history": (integration._mixed_history, 10),
    "long_sparse_with_admin": (_long_sparse_with_admin, 10),
    "schedule_change": (_schedule_change, 5),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_compact_authority_matches_raw_at_every_rebuild(name, monkeypatch):
    factory, every = SCENARIOS[name]
    raw = run(factory(), every=every, monkeypatch=monkeypatch)
    compact = run(factory(), every=every, compact_from=0, monkeypatch=monkeypatch)

    assert len(raw) == len(compact)
    for index, (left, right) in enumerate(zip(raw, compact)):
        assert decision_summary(schema4.persisted_authority_decision(right)) == decision_summary(
            schema4.persisted_authority_decision(left)
        ), (name, index)
        assert consumer_summary(integration.record_step(right)) == consumer_summary(
            integration.record_step(left)
        ), (name, index)
        assert not schema4._validate_server_record(s8.SERVER, right)
        assert len(schema4.learned_state_ring_events(right, schema4.persisted_learned_state(right))) <= (
            learned.RECENT_RING_CAPACITY
        )


@pytest.mark.parametrize("name", ["long_chain", "sparse_with_anomalies", "schedule_change", "stage6_recovery"])
def test_cutover_keeps_regime_and_warning_identities(name, monkeypatch):
    factory, every = SCENARIOS[name]
    raw = run(factory(), every=every, monkeypatch=monkeypatch)
    switch = max(1, len(raw) // 2)
    cutover = run(factory(), every=every, compact_from=switch, monkeypatch=monkeypatch)

    for index in range(switch, len(raw)):
        left = schema4.persisted_authority_decision(raw[index])
        right = schema4.persisted_authority_decision(cutover[index])
        assert decision_summary(right) == decision_summary(left), (name, index)
        assert (right.incumbent_shadow_regime is None) == (left.incumbent_shadow_regime is None)
        if left.incumbent_shadow_regime is not None and left.incumbent_shadow_regime.regime_id in {
            item.regime_id for item in schema4.persisted_authority_decision(raw[switch - 1]).regimes
        }:
            # Regimes that existed before the cutover keep their identity.
            assert right.incumbent_shadow_regime.regime_id == left.incumbent_shadow_regime.regime_id
            assert warning_key(integration.record_step(cutover[index])) == warning_key(
                integration.record_step(raw[index])
            )


@pytest.mark.parametrize("name", ["long_chain", "sparse_with_anomalies", "repeated_slots", "schedule_change"])
def test_compact_identities_are_stable_across_rebuilds_and_reopen(name, monkeypatch):
    factory, every = SCENARIOS[name]
    records = run(factory(), every=every, compact_from=0, monkeypatch=monkeypatch)
    seen: dict[tuple, str] = {}
    for record in records:
        decision = schema4.persisted_authority_decision(record)
        for regime in decision.regimes:
            key = (regime.candidate_period_seconds, regime.established_at)
            if regime.status.value in {"established", "superseded"} and regime.established_at is not None:
                assert seen.setdefault(key, regime.regime_id) == regime.regime_id
    final = records[-1]
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", True)
    reopened = json.loads(schema4.canonical_json_bytes(final))
    pipeline = factory()
    again = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == final["authority_decision"]
    assert again["learned_state"] == final["learned_state"]


def test_noisy_boundaries_never_create_unsupported_establishment_or_warning(monkeypatch):
    def factory():
        rng = random.Random(7)
        pipeline = s8.Pipeline()
        pipeline.sparse(
            "noisy", [s8.at(day, (3 * day) % 24) + rng.choice((-541, -300, 0, 300, 541, 545)) for day in range(110)]
        )
        return pipeline

    raw = run(factory(), every=10, monkeypatch=monkeypatch)
    compact = run(factory(), every=10, compact_from=0, monkeypatch=monkeypatch)
    for left, right in zip(raw, compact):
        raw_view = consumer_summary(integration.record_step(left))
        compact_view = consumer_summary(integration.record_step(right))
        if compact_view["countdown"][0]:
            assert raw_view["countdown"][0]
        if compact_view["warning"] and compact_view["warning"][0]:
            assert raw_view["warning"] and raw_view["warning"][0]
        if compact_view["percent"] is not None and raw_view["percent"] is not None:
            assert compact_view["percent"] <= raw_view["percent"]


# --------------------------------------------------------------------------
# Independence from raw history: a compact-only runner
# --------------------------------------------------------------------------


class CompactOnly:
    """Keeps only the learned state (reopened from JSON each step), the ring
    and the bounded schema-3 horizon.  Folded raw events are discarded."""

    def __init__(self, pipeline, *, capacity=learned.RECENT_RING_CAPACITY):
        self.pipeline = pipeline
        self.capacity = capacity
        self.state = learned.empty_learned_state(s8.SERVER)
        self.ring = ()
        self.prior = None
        self.previous_score = None
        self.incumbent_period = None
        self.seen = []

    def step(self, new_events, *, now=None, windows=()):
        self.seen.extend(new_events)
        known = tuple(sorted({item.event_id: item for item in (*self.ring, *new_events)}.values(), key=lambda item: item.canonical_phase_at))
        spans = expected_windows.continuity_spans_from_coverage(self.pipeline.coverage, server_key=s8.SERVER)
        relationships = continuity.extract_interval_relationships(known, spans)
        horizon = sorted(self.seen, key=lambda item: item.canonical_phase_at)[-runtime.MAX_FINALIZED_EVENTS:]
        now = (max(item.finalized_at for item in self.seen) + 60) if now is None else now
        coverage = self.pipeline.coverage if now - max(item.finalized_at for item in self.seen) < 60 * scoring.DAY else ()
        score = scoring.RestartScheduleScorer().score(
            horizon,
            scoring.CoverageTimeline(tuple(coverage)),
            now=now,
            phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
            previous=self.previous_score,
            incumbent_period_seconds=self.incumbent_period,
        )
        self.previous_score = score
        self.incumbent_period = score.selected_period_seconds
        state = self.state
        if self.prior is not None:
            state = learned.observe_decision(state, self.prior)
        plan = learned.plan_ring_folding(state, known, capacity=self.capacity)
        state = learned.apply_fold_plan(state, plan, relationships)
        state = learned.invalidate_contradicted_divisors(state, plan.ring)
        state = learned.fold_divisor_resolution(state, score, compact_tests.recent_events(horizon))
        decision = schema4.evaluate_compact_authority(
            state,
            server_key=s8.SERVER,
            normal_score=score,
            relationships=relationships,
            windows=windows,
            events=known,
            prior=self.prior,
        )
        state = schema4.advance_learned_state_after_decision(state, decision=decision, windows=windows)
        # Reopen: the saved compact state is all that survives the step.
        self.state = learned.learned_state_from_json_bytes(learned.learned_state_json_bytes(state))
        self.ring = plan.ring
        self.prior = s8.persisted(decision)
        assert len(self.ring) <= self.capacity
        return s8.Step(now, score, relationships, (), decision, known)


def raw_steps(pipeline, batches, *, nows=None, ledgers=None):
    full = s8.Pipeline(coverage=list(pipeline.coverage))
    prior = None
    steps = []
    for index, batch in enumerate(batches):
        full.events.extend(batch)
        now = None if nows is None else nows[index]
        ledger = () if ledgers is None else ledgers[index]
        step = full.evaluate(prior, now=now, ledger=ledger)
        prior = s8.persisted(step.decision)
        steps.append(step)
    return steps


def batches_of(pipeline, size):
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    return [ordered[index:index + size] for index in range(0, len(ordered), size)]


def assert_same(raw_step, compact_step, label):
    assert decision_summary(compact_step.decision) == decision_summary(raw_step.decision), label
    assert consumer_summary(compact_step) == consumer_summary(raw_step), label


INDEPENDENCE = {
    "sparse_eighty": (lambda: s8.sparse_pipeline(), 1, 3),
    "sparse_then_interval": (_sparse_then_interval, 1, 3),
    "three_intervals": (lambda: s8.three_hour_chain(4), 1, 2),
    "repeated_slots": (lambda: integration._repeated_slots(40), 5, 20),
    "long_chain": (lambda: integration._long_chain(160), 8, learned.RECENT_RING_CAPACITY),
    "qv_sparse": (lambda: integration._qv_sparse(60), 5, 20),
    "anomalies": (lambda: integration._sparse_with_anomalies(60), 5, 20),
    "schedule_change": (_schedule_change, 5, learned.RECENT_RING_CAPACITY),
}


@pytest.mark.parametrize("name", sorted(INDEPENDENCE))
def test_compact_only_runner_matches_full_history_raw_authority(name):
    factory, size, capacity = INDEPENDENCE[name]
    pipeline = factory()
    batches = batches_of(pipeline, size)
    raw = raw_steps(pipeline, batches)
    runner = CompactOnly(pipeline, capacity=capacity)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert_same(raw[index], compact, (name, index))
    assert runner.state.watermark.folded_event_count >= len(pipeline.events) - capacity


def test_partial_learner_survives_six_months_then_improves():
    """76 % partial learning (eight sparse restarts and one admin restart),
    fully folded (raw discarded), then six months without observation, then
    one more aligned restart reaches the 80 % milestone, as with full history."""

    pipeline = s8.sparse_pipeline()
    pipeline.sparse("admin", [s8.at(6, 17, 20)])
    runner = CompactOnly(pipeline, capacity=2)
    batches = batches_of(pipeline, 1)
    raw = raw_steps(pipeline, batches)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert_same(raw[index], compact, index)
    assert s8.percent(compact) == 76
    assert runner.state.watermark.folded_event_count == len(pipeline.events) - runner.capacity

    later = max(item.finalized_at for item in pipeline.events) + 182 * scoring.DAY
    idle = runner.step((), now=later)
    assert decision_summary(idle.decision) == decision_summary(compact.decision)
    assert s8.percent(idle) == 76

    ninth = pipeline.sparse("ninth", [s8.at(190, 9)])
    improved = runner.step(ninth)
    raw_improved = raw_steps(pipeline, [*batches, ninth])[-1]
    assert_same(raw_improved, improved, "improved")
    assert s8.percent(improved) == 80
    assert integration.compact_tests.display(improved.decision.combined_confidence) == 80


def test_challenger_detection_after_old_history_is_discarded():
    """An established 3 h cycle (100 restarts, almost all folded and
    discarded) meets a genuine 4 h schedule: suspicion, transition and
    supersession follow the full-history raw authority step by step."""

    pipeline = _schedule_change()
    batches = batches_of(pipeline, 5)
    raw = raw_steps(pipeline, batches)
    runner = CompactOnly(pipeline)
    states = []
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert_same(raw[index], compact, index)
        states.append(compact.decision.state.value)
    assert "new_regime_established" in states
    assert compact.decision.incumbent_shadow_regime.candidate_period_seconds == P4


def test_harmonic_resolution_follows_a_six_to_three_hour_change_without_raw_history():
    pipeline = s8.Pipeline()
    pipeline.observe("six", [s8.at(0, 0) + index * 6 * HOUR for index in range(12)])
    pipeline.observe("three", [s8.at(4, 0) + index * P3 for index in range(40)])
    batches = batches_of(pipeline, 1)
    raw = raw_steps(pipeline, batches)
    runner = CompactOnly(pipeline, capacity=20)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        raw_six = [item for item in raw[index].decision.candidates if item.candidate_period_seconds == 6 * HOUR]
        compact_six = [item for item in compact.decision.candidates if item.candidate_period_seconds == 6 * HOUR]
        # Never less blocked than raw, and never an H3 the raw authority lacks.
        assert {code for item in raw_six for code in item.harmonic_blockers} <= {
            code for item in compact_six for code in item.harmonic_blockers
        } or not raw_six, index
        assert not any(item.h3_gate for item in compact_six) or any(item.h3_gate for item in raw_six), index
        assert_same(raw[index], compact, index)
    assert not any(item.period == 6 * HOUR and item.divisor == P3 for item in runner.state.divisors)


def test_latched_level_two_challenger_survives_folding_of_its_intervals():
    """Two disconnected coherent 4 h intervals suspend an established 3 h
    countdown (level 2, latched).  Later aligned sparse 3 h restarts (never
    full intervals, so never recovery confirmations) push those intervals out
    of a small ring: the folded relationships are still counted, so the
    suspension holds exactly as with full history."""

    pipeline = s8.three_hour_chain(4)
    pipeline.observe("four-a", [s8.at(1, 10), s8.at(1, 14)])
    pipeline.observe("four-b", [s8.at(2, 10), s8.at(2, 14)])
    pipeline.sparse("aligned", [s8.at(3 + day, (3 * day) % 24) for day in range(12)])
    batches = batches_of(pipeline, 1)
    raw = raw_steps(pipeline, batches)
    runner = CompactOnly(pipeline, capacity=4)
    folded_counts = []
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert_same(raw[index], compact, index)
        folded_counts.extend(
            item.folded_high_relationship_count for item in compact.decision.challenger_contexts
        )
    assert raw[-1].decision.suspicion_level == 2 and not raw[-1].decision.countdown_safe
    assert max(folded_counts) >= 1  # the latched intervals were folded and still counted


def test_folded_off_phase_restart_still_prevents_the_isolation_relief():
    """Admin restart A is folded; only admin restart B (and an ineligible
    episode) remain in a two-event ring.  Full history sees two off-phase
    restarts after the last aligned one, so B is not an isolated anomaly; the
    compact path must reach the same conclusion through the folded A."""

    from dataclasses import replace as dc_replace

    pipeline = s8.sparse_pipeline()
    established = list(pipeline.events)
    first_admin = pipeline.sparse("admin-a", [s8.at(8, *s8.ADMIN_RESTARTS[0])])
    ineligible = pipeline.sparse("blip", [s8.at(9, 2, 30)])
    pipeline.events[-1] = dc_replace(pipeline.events[-1], coverage_complete=False)
    ineligible = [pipeline.events[-1]]
    second_admin = pipeline.sparse("admin-b", [s8.at(10, *s8.ADMIN_RESTARTS[1])])
    batches = [established, first_admin, ineligible, second_admin]
    raw = raw_steps(pipeline, batches)
    runner = CompactOnly(pipeline, capacity=2)
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        assert_same(raw[index], compact, index)
        assert (authority.ISOLATED_OFF_PHASE_RESTART_REASON in compact.decision.reason_codes) is (
            authority.ISOLATED_OFF_PHASE_RESTART_REASON in raw[index].decision.reason_codes
        ), index
    assert raw[-1].decision.suspicion_level == 1
    assert authority.ISOLATED_OFF_PHASE_RESTART_REASON not in raw[-1].decision.reason_codes
    assert first_admin[0].event_id not in {item.event_id for item in runner.ring}


def test_folded_genuine_miss_keeps_incumbent_suspicion():
    """A genuine miss at the established cycle's next slot, followed only by
    ineligible episodes: the miss window folds (it ends before the event
    watermark) and must still hold level-one suspicion, as with full history."""

    from dataclasses import replace as dc_replace

    pipeline = s8.three_hour_chain(4)
    chain = list(pipeline.events)
    miss = s8.genuine_miss(P3, s8.at(0, 21), key="r3-miss")
    blips = []
    for index in range(4):
        values = pipeline.sparse(f"blip-{index}", [s8.at(1, 2 + index, 10)])
        pipeline.events[-1] = dc_replace(pipeline.events[-1], coverage_complete=False)
        blips.append([pipeline.events[-1]])
    batches = [chain, *blips]
    ledgers = [()] + [(miss,)] * len(blips)
    raw = raw_steps(pipeline, batches, ledgers=ledgers)
    runner = CompactOnly(pipeline, capacity=2)
    for index, batch in enumerate(batches):
        compact = runner.step(batch, windows=ledgers[index])
        assert_same(raw[index], compact, index)
    assert raw[-1].decision.suspicion_level == 1
    assert runner.state.watermark.window_end_at is not None  # the miss was folded
    assert any(item.genuine_miss_count for item in runner.state.misses)


def test_in_phase_genuine_miss_withholds_two_interval_establishment_compactly():
    """Stage 8.1: an active in-phase genuine miss withholds two-interval
    establishment; with the miss folded the compact path withholds it too."""

    pipeline = s8.three_hour_chain(3)
    miss = s8.genuine_miss(P3, s8.at(0, 18), key="r3-in-phase-miss")
    batches = [list(pipeline.events)]
    raw = raw_steps(pipeline, batches, ledgers=[(miss,)])
    runner = CompactOnly(pipeline, capacity=2)
    compact = runner.step(batches[0], windows=(miss,))
    assert_same(raw[0], compact, "miss")
    assert compact.decision.state.value != "established"


@pytest.mark.parametrize("compact_enabled", [False, True], ids=["raw", "compact"])
def test_authoritative_runtime_with_compact_authority(tmp_path, monkeypatch, compact_enabled):
    from dzll_launcher import companion_restart_phase2_schema4_runtime as live4

    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", compact_enabled)
    active = tmp_path / "companion_restart_learning_phase2.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=active, legacy_path=tmp_path / "companion_restart_learning.json", now=s8.DAY0
    )
    pipeline = integration._sparse_with_anomalies(100)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        for stop in (30, 60, 90, len(ordered), len(ordered)):
            prefix = ordered[:stop]
            chains = {item.continuity_chain_id for item in prefix}
            coverage = [item for item in pipeline.coverage if item.continuity_chain_id in chains]
            backend.update_server_from_schema3(
                s8.SERVER,
                integration.schema3_record(prefix, coverage),
                updated_at=max(item.finalized_at for item in prefix) + 60,
                durable_reason="r3_fixture",
            )
            backend.flush()
    finally:
        backend.close()
    reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        record = reopened.state["servers"][s8.SERVER]
    finally:
        reopened.close(flush=False)

    assert not schema4._validate_server_record(s8.SERVER, record)
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", False)
    expected = {}
    for stop in (30, 60, 90, len(ordered), len(ordered)):
        prefix = ordered[:stop]
        chains = {item.continuity_chain_id for item in prefix}
        expected = integration.rebuild(
            expected, prefix, [item for item in pipeline.coverage if item.continuity_chain_id in chains]
        )
    assert decision_summary(schema4.persisted_authority_decision(record)) == decision_summary(
        schema4.persisted_authority_decision(expected)
    )
    assert consumer_summary(integration.record_step(record)) == consumer_summary(
        integration.record_step(expected)
    )
    assert schema4.persisted_authority_decision(record).incumbent_shadow_regime.candidate_period_seconds == P3
