"""Retention R2: compact learned state maintained by the schema-4 rebuild.

Synthetic restart histories are driven through the production
``rebuild_schema4_server_record`` path (schema-3 record -> schema-4 record).
The persisted ``learned_state`` plus its bounded ring is then evaluated with
the compact evaluators and compared with the raw-history authority decision
that the same rebuild produced.  Old raw evidence is removed from synthetic
fixtures only.  No runtime, profile or learning file is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import test_companion_restart_learned_state as compact_tests
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P3 = 3 * HOUR
P4 = 4 * HOUR
Outcome = s8.Outcome


# --------------------------------------------------------------------------
# Production rebuild driver
# --------------------------------------------------------------------------


def schema3_record(events, coverage):
    server = runtime._new_server_runtime(s8.SERVER, detection.DetectionConfig())
    # One runtime per server in production: a fixed generation keeps the
    # synthetic schema-3 projection deterministic across rebuilds.
    server.regime_generation = "r2-fixture-generation"
    ordered = sorted(events, key=lambda item: (item.canonical_phase_at, item.event_id))
    server.events = ordered[-runtime.MAX_FINALIZED_EVENTS:]
    server.coverage = sorted(coverage, key=lambda item: (item.start_at, item.end_at))[
        -runtime.MAX_COVERAGE_SEGMENTS:
    ]
    server.event_seq = max((item.sequence for item in ordered), default=0)
    server.routed_fingerprints = {item.fingerprint for item in server.events}
    return runtime._serialize_server(server)


def rebuild(record, events, coverage, *, updated_at=None):
    updated_at = updated_at if updated_at is not None else max(item.finalized_at for item in events) + 60
    return schema4.rebuild_schema4_server_record(
        record, schema3_record(events, coverage), server_key=s8.SERVER, updated_at=updated_at
    )


def drive(pipeline, *, every=10):
    """Rebuild after every ``every`` restarts (in time order), runtime-like."""

    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = {}
    stops = list(range(every, len(ordered), every)) + [len(ordered)]
    for stop in stops:
        prefix = ordered[:stop]
        chains = {item.continuity_chain_id for item in prefix}
        coverage = [item for item in pipeline.coverage if item.continuity_chain_id in chains]
        record = rebuild(record, prefix, coverage)
    # Settle: one more identical rebuild (the prior decision moves once).
    return rebuild(record, ordered, pipeline.coverage)


def compact_view(record):
    state = schema4.persisted_learned_state(record)
    return state, schema4.learned_state_ring_events(record, state)


def raw_score(record):
    runtime_server = runtime._deserialize_server(
        s8.SERVER, record["legacy_schema3_record"], detection.DetectionConfig()
    )
    return scoring.RestartScheduleScorer().score(
        runtime_server.events,
        scoring.CoverageTimeline(tuple(runtime_server.coverage)),
        now=record["migration_audit"]["runtime_updated_at"],
        incumbent_period_seconds=runtime_server.incumbent_period_seconds,
        aggregate=runtime_server.aggregate,
        expected_misses=runtime_server.expected_misses,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )


def record_step(record):
    return s8.Step(
        now=record["migration_audit"]["runtime_updated_at"],
        score=raw_score(record),
        relationships=schema4.persisted_interval_relationships(record),
        streaks=schema4.persisted_cadence_streaks(record),
        decision=schema4.persisted_authority_decision(record),
        events=schema4.persisted_physical_events(record),
    )


def parity_state(state):
    """Compare like with like: live divisor resolution only (the persisted
    best resolution is the pending operator decision, tested separately)."""

    return replace(state, divisors=())


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def _long_chain(count=200):
    pipeline = s8.Pipeline()
    pipeline.observe("long", [s8.at(0, 9) + index * P3 for index in range(count)])
    return pipeline


def _sparse_with_anomalies(count=120):
    pipeline = s8.Pipeline()
    pipeline.sparse("aligned", s8.aligned_3h(count))
    pipeline.sparse("admin", s8.admin_restarts(3, (17, 55, 101)))
    return pipeline


def _repeated_slots(days=60):
    return compact_tests._repeated_slots(days)


def _qv_sparse(count=100):
    pipeline = s8.Pipeline()
    outcomes = (Outcome.STRONG_QUERY_VISIBLE_RESTART, Outcome.PROBABLE_QUERY_VISIBLE_RESTART, Outcome.CORROBORATED_OFFLINE_RESTART)
    for index, when in enumerate(s8.aligned_3h(count)):
        pipeline.sparse(f"qv-{index}", [when], outcome=outcomes[index % 3])
    return pipeline


def _mixed_history():
    pipeline = s8.Pipeline()
    pipeline.sparse("early", s8.aligned_3h(60))
    pipeline.observe("chain-a", [s8.at(70, 0) + index * P3 for index in range(30)])
    pipeline.sparse("late", s8.aligned_3h(30, start_day=90))
    return pipeline


SCENARIOS = {
    "long_chain": _long_chain,
    "sparse_with_anomalies": _sparse_with_anomalies,
    "repeated_slots": _repeated_slots,
    "qv_sparse": _qv_sparse,
    "mixed_history": _mixed_history,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_rebuild_maintains_bounded_learned_state_matching_raw_authority(name):
    pipeline = SCENARIOS[name]()
    record = drive(pipeline)
    state, ring = compact_view(record)
    step = record_step(record)

    assert len(record["physical_events"]) == len(pipeline.events)  # nothing pruned yet
    assert len(ring) <= learned.RECENT_RING_CAPACITY
    assert state.watermark.folded_event_count == len(pipeline.events) - len(
        [item for item in ring if item.event_id != getattr(state.open_chain, "anchor_event_id", None)]
    )
    compact_tests.assert_high_parity(step, parity_state(state), ring)
    compact_tests.assert_sparse_parity(step, parity_state(state), ring)
    assert learned.learned_state_entry_count(state) <= 40
    assert len(learned.learned_state_json_bytes(state)) < 6000


def test_long_chain_keeps_the_open_anchor_across_rebuilds():
    pipeline = _long_chain(200)
    record = drive(pipeline, every=7)
    state, ring = compact_view(record)

    assert state.open_chain is not None and state.open_chain.run is not None
    assert ring[0].event_id == state.open_chain.anchor_event_id
    assert len(ring) == learned.RECENT_RING_CAPACITY
    candidate = learned.compact_candidates(
        parity_state(state), ring_events=ring, ring_relationships=schema4.persisted_interval_relationships(record)
    )[0]
    assert candidate.longest_streak == 199 and candidate.h4_gate


def test_noisy_shared_minute_history_is_conservative_never_promoted():
    pipeline = s8.Pipeline()
    pipeline.sparse("noisy", [s8.at(day, (3 * day) % 24) + ((day * 37) % 50) for day in range(120)])
    record = drive(pipeline)
    state, ring = compact_view(record)
    step = record_step(record)

    compact_tests.assert_sparse_parity(step, parity_state(state), ring, conservative=True)


# --------------------------------------------------------------------------
# Rebuild, reopen and inactivity stability
# --------------------------------------------------------------------------


def test_identical_rebuilds_are_a_fixed_point_and_survive_json_reopen():
    pipeline = _mixed_history()
    record = drive(pipeline)
    again = rebuild(record, pipeline.events, pipeline.coverage)
    reopened = json.loads(schema4.canonical_json_bytes(record))
    from_disk = rebuild(reopened, pipeline.events, pipeline.coverage)

    assert again["learned_state"] == record["learned_state"]
    assert schema4.canonical_json_bytes(again) == schema4.canonical_json_bytes(record)
    assert from_disk["learned_state"] == record["learned_state"]
    assert not schema4._validate_server_record(s8.SERVER, record)


def test_months_of_inactivity_change_nothing():
    pipeline = _sparse_with_anomalies()
    record = drive(pipeline)
    later = rebuild(record, pipeline.events, pipeline.coverage, updated_at=record["migration_audit"]["runtime_updated_at"] + 200 * scoring.DAY)

    assert later["learned_state"] == record["learned_state"]
    assert later["authority_decision"]["combined_confidence"] == record["authority_decision"]["combined_confidence"]
    assert later["regimes"] == record["regimes"]


def test_events_are_folded_once_and_the_watermark_only_advances():
    pipeline = _sparse_with_anomalies(100)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = {}
    marks = []
    for stop in range(10, len(ordered) + 1, 10):
        prefix = ordered[:stop]
        chains = {item.continuity_chain_id for item in prefix}
        record = rebuild(record, prefix, [item for item in pipeline.coverage if item.continuity_chain_id in chains])
        state = schema4.persisted_learned_state(record)
        marks.append(state.watermark.folded_event_count)
        assert state.watermark.folded_event_count == max(0, stop - learned.RECENT_RING_CAPACITY)
    assert marks == sorted(marks)
    # Rebuilding an older prefix (replayed history) credits nothing again.
    replayed = rebuild(record, ordered[:50], pipeline.coverage)
    assert schema4.persisted_learned_state(replayed).watermark == schema4.persisted_learned_state(record).watermark
    assert schema4.persisted_learned_state(replayed).pool == schema4.persisted_learned_state(record).pool


def test_missing_or_corrupt_learned_state_is_rebuilt_from_retained_history():
    pipeline = _mixed_history()
    record = drive(pipeline)
    missing = dict(record)
    missing.pop("learned_state")
    corrupt = json.loads(schema4.canonical_json_bytes(record))
    corrupt["learned_state"]["pool"][0][1] = -5

    assert schema4._validate_server_record(s8.SERVER, corrupt)
    rebuilt_missing = rebuild(missing, pipeline.events, pipeline.coverage)
    rebuilt_corrupt = rebuild(corrupt, pipeline.events, pipeline.coverage)
    assert schema4.LEARNED_STATE_REBUILT_REASON in rebuilt_corrupt["reason_codes"]
    assert schema4.LEARNED_STATE_REBUILT_REASON not in rebuilt_missing["reason_codes"]
    for value in (rebuilt_missing, rebuilt_corrupt):
        state, ring = compact_view(value)
        assert not schema4._validate_server_record(s8.SERVER, value)
        compact_tests.assert_sparse_parity(record_step(value), parity_state(state), ring)


def test_validation_rejects_foreign_or_oversized_learned_state():
    record = drive(_sparse_with_anomalies(90))
    foreign = json.loads(schema4.canonical_json_bytes(record))
    foreign["learned_state"]["server_key"] = "other.example:2302"
    oversized = json.loads(schema4.canonical_json_bytes(record))
    oversized["learned_state"]["pool"] = oversized["learned_state"]["pool"] * 2000

    codes = {issue.code for issue in schema4._validate_server_record(s8.SERVER, foreign)}
    assert "learned_state_server_mismatch" in codes
    codes = {issue.code for issue in schema4._validate_server_record(s8.SERVER, oversized)}
    assert "invalid_learned_state" in codes


# --------------------------------------------------------------------------
# Confidence preservation when old raw evidence is removed (fixtures only)
# --------------------------------------------------------------------------


def _without_folded_raw(record):
    """Remove raw events already folded into learned state (fixture only)."""

    state = schema4.persisted_learned_state(record)
    keep = {item.event_id for item in schema4.learned_state_ring_events(record, state)}
    events = tuple(item for item in schema4.persisted_physical_events(record) if item.event_id in keep)
    relationships = tuple(
        item
        for item in schema4.persisted_interval_relationships(record)
        if item.left_event_id in keep and item.right_event_id in keep
    )
    return state, events, relationships


@pytest.mark.parametrize("name", ["sparse_with_anomalies", "repeated_slots", "long_chain", "qv_sparse"])
def test_compact_authority_keeps_confidence_when_folded_raw_is_deleted(name):
    pipeline = SCENARIOS[name]()
    record = drive(pipeline)
    full = record_step(record)
    state, events, relationships = _without_folded_raw(record)
    period = P3
    raw_full = max(
        (item for item in full.decision.candidates if item.candidate_period_seconds == period),
        key=lambda item: item.combined_confidence,
    )
    # Raw authority over the remaining (ring-only) evidence: what deleting
    # history would do today.
    ring_only = authority.evaluate_authority(
        server_key=s8.SERVER,
        normal_score=full.score,
        relationships=relationships,
        streaks=(),
        events=events,
    )
    ring_only_best = max(
        (item.combined_confidence for item in ring_only.candidates if item.candidate_period_seconds == period),
        default=0.0,
    )
    compact = learned.compact_candidates(
        parity_state(state), ring_events=events, ring_relationships=relationships, normal_score=full.score
    )
    high = [item for item in compact if item.period == period]
    if high:
        compact_value = max(item.combined_confidence for item in high)
    else:
        phase = learned.compact_phase_proposal(state, period=period, ring_events=events)[0]
        support = learned.compact_sparse_support(state, period=period, phase_offset=phase, ring_events=events)
        compact_value = learned.compact_combined_confidence(0.0, support.value)

    assert compact_value == pytest.approx(raw_full.combined_confidence, abs=1e-6)
    assert ring_only_best <= raw_full.combined_confidence + 1e-9


def test_partial_learning_progress_survives_folding_deletion_and_inactivity(monkeypatch):
    """A 76 % partially learned cycle (eight sparse restarts and one admin
    restart) is folded completely, its raw evidence deleted and months pass:
    the compact state still carries the same support."""

    original_plan = learned.plan_ring_folding
    monkeypatch.setattr(
        learned,
        "plan_ring_folding",
        lambda state, events, capacity=learned.RECENT_RING_CAPACITY: original_plan(state, events, capacity=2),
    )
    pipeline = s8.sparse_pipeline()
    pipeline.sparse("admin", [s8.at(6, 17, 20)])
    record = drive(pipeline, every=3)
    later = rebuild(record, pipeline.events, pipeline.coverage, updated_at=record["migration_audit"]["runtime_updated_at"] + 365 * scoring.DAY)
    raw = record_step(record)
    raw_value = max(
        item.aligned_normal_confidence
        for item in raw.decision.normal_ledger.candidates
        if item.candidate_period_seconds == P3
    )
    state, events, _relationships = _without_folded_raw(later)

    assert state.watermark.folded_event_count >= len(pipeline.events) - 2
    support = learned.compact_sparse_support(state, period=P3, phase_offset=0.0, ring_events=events)
    assert support.value == pytest.approx(raw_value, abs=1e-6)
    assert compact_tests.display(learned.compact_combined_confidence(0.0, support.value)) == 76


def test_challenger_epoch_evidence_is_maintained_by_rebuild(monkeypatch):
    original_plan = learned.plan_ring_folding
    monkeypatch.setattr(
        learned,
        "plan_ring_folding",
        lambda state, events, capacity=learned.RECENT_RING_CAPACITY: original_plan(state, events, capacity=2),
    )
    pipeline = s8._boundary_pipeline()
    record = drive(pipeline, every=2)
    acquired = schema4.persisted_authority_decision(record)
    pipeline.observe("new-four-hour", [s8.at(1, 10), s8.at(1, 14)])
    pipeline.observe("later-three-hour", [s8.at(2, 9), s8.at(2, 12)])
    record = rebuild(record, pipeline.events, pipeline.coverage)
    record = rebuild(record, pipeline.events, pipeline.coverage)
    state, ring = compact_view(record)
    step = record_step(record)
    lifetime = step.candidate(P4)
    view = authority._post_establishment_view(
        lifetime,
        established_at=acquired.incumbent_shadow_regime.established_at,
        relationship_map={item.relationship_id: item for item in step.relationships},
        streaks=step.streaks,
        relationships=step.relationships,
        normal_score=step.score,
        windows=(),
        events=step.events,
        event_map={item.event_id: item for item in step.events},
        sparse=authority._sparse_evidence(step.events, None),
    )
    challenger = [
        item
        for item in learned.compact_candidates(
            state, ring_events=ring, ring_relationships=step.relationships, challenger=True
        )
        if item.period == P4
    ]

    assert state.epoch.established_at == acquired.incumbent_shadow_regime.established_at
    assert len(challenger) == 1
    assert challenger[0].high_authority == pytest.approx(view.high_authority, abs=1e-6)
    assert challenger[0].relationship_count == 1


def test_established_regime_identity_and_alert_keys_are_untouched():
    pipeline = s8.three_hour_chain(4)
    with_state = drive(pipeline, every=2)
    original = schema4.LEARNED_STATE_PERSISTENCE_ENABLED
    try:
        schema4.LEARNED_STATE_PERSISTENCE_ENABLED = False
        without = drive(pipeline, every=2)
    finally:
        schema4.LEARNED_STATE_PERSISTENCE_ENABLED = original
    for key in ("authority_decision", "regimes", "authority_candidates", "consumer_state", "challenger_contexts"):
        assert with_state[key] == without[key], key
    state = schema4.persisted_learned_state(with_state)
    assert state.incumbent.regime_id == with_state["authority_decision"]["incumbent_shadow_regime_id"]


def test_authoritative_runtime_persists_and_reopens_learned_state(tmp_path):
    from dzll_launcher import companion_restart_phase2_schema4_runtime as live4

    active = tmp_path / "companion_restart_learning_phase2.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=active, legacy_path=tmp_path / "companion_restart_learning.json", now=s8.DAY0
    )
    pipeline = _sparse_with_anomalies(100)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
        for stop in (40, 100, len(ordered)):
            prefix = ordered[:stop]
            chains = {item.continuity_chain_id for item in prefix}
            coverage = [item for item in pipeline.coverage if item.continuity_chain_id in chains]
            backend.update_server_from_schema3(
                s8.SERVER,
                schema3_record(prefix, coverage),
                updated_at=max(item.finalized_at for item in prefix) + 60,
                durable_reason="r2_fixture",
            )
            backend.flush()
        written = backend.state["servers"][s8.SERVER]["learned_state"]

        def identical_update():
            return backend.update_server_from_schema3(
                s8.SERVER,
                schema3_record(ordered, pipeline.coverage),
                updated_at=max(item.finalized_at for item in ordered) + 60,
                durable_reason="r2_fixture",
            )

        # The pre-existing settle: the first identical rebuild moves the prior
        # decision once; the learned state itself is already a fixed point.
        identical_update()
        assert backend.state["servers"][s8.SERVER]["learned_state"] == written
        unchanged = identical_update()
        backend.flush()
    finally:
        backend.close()
    reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        record = reopened.state["servers"][s8.SERVER]
        state = schema4.persisted_learned_state(record)
    finally:
        reopened.close(flush=False)

    assert record["learned_state"] == written
    assert state.watermark.folded_event_count == len(pipeline.events) - learned.RECENT_RING_CAPACITY
    assert not unchanged


def test_persisted_divisor_resolution_diverges_after_a_cycle_change():
    """Blocker evidence (R2): the persisted best divisor resolution is not yet
    safe for authority.  After a 6 h regime is superseded by a 3 h schedule,
    raw recomputation re-blocks the 6 h harmonic; the persisted maximum keeps
    it resolved.  Authority therefore does not consume it (operator decision
    and a contradiction rule are required)."""

    pipeline = s8.Pipeline()
    pipeline.observe("six", [s8.at(0, 0) + index * 6 * HOUR for index in range(8)])
    record = drive(pipeline, every=4)
    three = [s8.at(3, 0) + index * P3 for index in range(24)]
    for count in range(1, len(three) + 1):  # one rebuild per new restart, runtime-like
        step = s8.Pipeline(events=list(pipeline.events), coverage=list(pipeline.coverage), sequence=pipeline.sequence)
        step.observe("three", three[:count])
        record = rebuild(record, step.events, step.coverage)
    state = schema4.persisted_learned_state(record)
    score = raw_score(record)
    incumbent = schema4.persisted_authority_decision(record).incumbent_shadow_regime

    assert incumbent.candidate_period_seconds == P3
    raw_blockers = authority._harmonic_blockers(6 * HOUR, score.candidate(6 * HOUR), ())
    compact_blockers = learned.compact_harmonic_blockers(state, 6 * HOUR, score.candidate(6 * HOUR))
    # Raw recomputation blocks the 6 h harmonic again; the persisted maximum
    # (re-folded from schema-3's still-stale window after the supersession
    # reset) does not.  Consuming it would unblock an ambiguous period.
    assert raw_blockers[1]
    assert not compact_blockers[1]
    # Authority output is unaffected: it is the raw recomputation.
    assert record["authority_decision"]["state"] in {"established", "new_regime_established"}
