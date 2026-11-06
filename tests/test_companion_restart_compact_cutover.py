"""Retention R4: compact authority enabled, folded raw evidence pruned.

Everything runs through the production ``rebuild_schema4_server_record``
path (schema-3 projection -> schema-4 record) with synthetic histories.  The
reference is the full-history raw authority (``COMPACT_AUTHORITY_ENABLED``
False, nothing pruned).  No runtime profile or learning file is touched; the
runtime tests use a pytest ``tmp_path``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as r3
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumer_state as state4
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P3 = 3 * HOUR
LIMIT = schema4.RAW_EVENT_RETENTION_LIMIT
COLLECTIONS = (
    "physical_events",
    "interval_relationships",
    "cadence_streaks",
    "expected_window_results",
    "continuity_chains",
    "continuity_spans",
    "expected_window_episode_evidence",
    "regimes",
    "authority_decisions",
)


def production_step(record):
    """``integration.record_step`` with the schema-3 runtime events.

    The production consumer counts events of the schema-3 runtime
    (``server.events``), not the schema-4 physical-event collection, which is
    now pruned to the ring.
    """

    server = runtime._deserialize_server(
        s8.SERVER, record["legacy_schema3_record"], detection.DetectionConfig()
    )
    return replace(integration.record_step(record), events=tuple(server.events))


def consumer(record):
    return r3.consumer_summary(production_step(record))


def counts(record):
    values = {name: len(record[name]) for name in COLLECTIONS}
    values["bytes"] = len(schema4.canonical_json_bytes(record))
    return values


def assert_pruned_record(record, *, limit=LIMIT):
    assert not schema4._validate_server_record(s8.SERVER, record)
    assert len(record["physical_events"]) <= limit
    assert record["cadence_streaks"] == []
    events = {item["event_id"] for item in record["physical_events"]}
    for item in record["interval_relationships"]:
        assert {item["left_event_id"], item["right_event_id"], *item["intervening_event_ids"]} <= events
    state = schema4.persisted_learned_state(record)
    ring = {item.event_id for item in schema4.learned_state_ring_events(record, state)}
    assert ring <= events
    if state.watermark.folded_event_count:
        assert schema4.RAW_EVIDENCE_PRUNED_REASON in record["reason_codes"]


def chain(count):
    pipeline = s8.Pipeline()
    pipeline.observe("long", [s8.at(0, 0) + index * P3 for index in range(count)])
    return pipeline


def sparse(count):
    pipeline = s8.Pipeline()
    pipeline.sparse("aligned", s8.aligned_3h(count))
    return pipeline


def drive(pipeline, *, every, check=None):
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = {}
    stops = list(range(every, len(ordered), every)) + [len(ordered)]
    for stop in stops:
        prefix = ordered[:stop]
        chains = {item.continuity_chain_id for item in prefix}
        coverage = [item for item in pipeline.coverage if item.continuity_chain_id in chains]
        record = integration.rebuild(record, prefix, coverage)
        if check is not None:
            check(stop, record)
    return record


@pytest.fixture
def tiny_ring(monkeypatch):
    """Patch the ring capacity so short scenarios fold and prune."""

    original = learned.plan_ring_folding

    def apply(capacity):
        monkeypatch.setattr(
            learned,
            "plan_ring_folding",
            lambda state, events, capacity_=None, **_: original(state, events, capacity=capacity),
        )

    return apply


# --------------------------------------------------------------------------
# 1. Compact authority is the production default and prunes
# --------------------------------------------------------------------------


def test_compact_authority_and_pruning_are_the_production_default():
    assert schema4.COMPACT_AUTHORITY_ENABLED is True
    assert schema4.LEARNED_STATE_PERSISTENCE_ENABLED is True
    record = drive(chain(200), every=7)

    assert_pruned_record(record)
    assert len(record["physical_events"]) == LIMIT
    state = schema4.persisted_learned_state(record)
    assert state.watermark.folded_event_count == 200 - (LIMIT - 1)
    assert state.open_chain.anchor_event_id in {item["event_id"] for item in record["physical_events"]}
    decision = schema4.persisted_authority_decision(record)
    assert decision.state.value == "established" and decision.countdown_safe


# --------------------------------------------------------------------------
# 2. Boundedness over long histories
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "length"),
    [("chain", 81), ("chain", 200), ("chain", 1000), ("chain", 5000), ("sparse", 200), ("sparse", 1000)],
)
def test_raw_evidence_and_json_size_stay_bounded(kind, length):
    pipeline = chain(length) if kind == "chain" else sparse(length)
    seen = []

    def check(stop, record):
        assert_pruned_record(record)
        seen.append((stop, counts(record)))

    record = drive(pipeline, every=40, check=check)
    decision = schema4.persisted_authority_decision(record)
    assert decision.incumbent_shadow_regime.candidate_period_seconds == P3
    peak = {name: max(item[name] for _stop, item in seen) for name in seen[0][1]}
    assert peak["physical_events"] <= LIMIT
    assert peak["cadence_streaks"] == 0
    assert peak["interval_relationships"] <= 4 * LIMIT
    # Chains and spans: the schema-3 projection's coverage horizon plus the
    # closure of retained objects, independent of history length.
    horizon = runtime.MAX_COVERAGE_SEGMENTS + LIMIT
    assert peak["continuity_chains"] <= horizon
    assert peak["continuity_spans"] <= 2 * horizon
    assert peak["bytes"] < 1_300_000
    if length >= 1000:
        # Steady state: once the 64-decision history is full, nothing grows.
        late = [item for stop, item in seen if stop >= 40 * 70]
        if late:
            assert max(item["bytes"] for item in late) - min(item["bytes"] for item in late) < 2_000
        middle = [item for stop, item in seen if stop >= length // 2]
        assert {item["interval_relationships"] for item in middle} == {
            middle[-1]["interval_relationships"]
        }
        state = schema4.persisted_learned_state(record)
        assert learned.learned_state_entry_count(state) <= 40


def test_uninterrupted_chain_ring_is_bounded_at_every_rebuild():
    pipeline = chain(400)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = {}
    for stop in range(1, len(ordered) + 1):
        record = integration.rebuild(record, ordered[:stop], pipeline.coverage)
        assert len(record["physical_events"]) == min(stop, LIMIT)
        if stop > LIMIT:
            state = schema4.persisted_learned_state(record)
            assert state.open_chain.anchor_event_id in {
                item["event_id"] for item in record["physical_events"]
            }
            assert state.watermark.folded_event_count == stop - (LIMIT - 1)
    assert_pruned_record(record)


def test_pruned_record_matches_full_history_raw_authority_at_every_rebuild(monkeypatch):
    """The R3 scenarios (pruned live now): the per-rebuild comparison holds."""

    for name in ("long_chain", "schedule_change", "mixed_history", "qv_sparse"):
        factory, every = r3.SCENARIOS[name]
        raw = r3.run(factory(), every=every, monkeypatch=monkeypatch)
        compact = r3.run(factory(), every=every, compact_from=0, monkeypatch=monkeypatch)
        for index, (left, right) in enumerate(zip(raw, compact)):
            assert r3.decision_summary(schema4.persisted_authority_decision(right)) == (
                r3.decision_summary(schema4.persisted_authority_decision(left))
            ), (name, index)
            assert consumer(right) == consumer(left), (name, index)
            assert_pruned_record(right)
        assert len(compact[-1]["physical_events"]) < len(raw[-1]["physical_events"])


# --------------------------------------------------------------------------
# 3. Reopen and replay never resurrect folded evidence
# --------------------------------------------------------------------------


def test_reopen_and_replayed_schema3_history_do_not_resurrect_folded_evidence():
    pipeline = chain(300)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = drive(pipeline, every=20)
    record = integration.rebuild(record, ordered, pipeline.coverage)  # settle
    reopened = json.loads(schema4.canonical_json_bytes(record))
    again = integration.rebuild(reopened, ordered, pipeline.coverage)
    assert schema4.canonical_json_bytes(again) == schema4.canonical_json_bytes(record)

    # A schema-3 projection holding only old, already folded restarts.
    replayed = integration.rebuild(record, ordered[:120], pipeline.coverage)
    ids = lambda value, name, field: {item[field] for item in value[name]}  # noqa: E731
    assert ids(replayed, "physical_events", "event_id") == ids(record, "physical_events", "event_id")
    assert ids(replayed, "interval_relationships", "relationship_id") <= ids(
        record, "interval_relationships", "relationship_id"
    )
    before = schema4.persisted_learned_state(record)
    after = schema4.persisted_learned_state(replayed)
    assert after.watermark == before.watermark
    assert after.pool == before.pool and after.intervals == before.intervals
    assert_pruned_record(replayed)


def test_pruned_record_without_usable_learned_state_is_rejected_not_relearned():
    record = drive(chain(200), every=10)
    missing = json.loads(schema4.canonical_json_bytes(record))
    missing.pop("learned_state")
    corrupt = json.loads(schema4.canonical_json_bytes(record))
    corrupt["learned_state"]["pool"][0][1] = -5

    codes = {item.code for item in schema4._validate_server_record(s8.SERVER, missing)}
    assert "learned_state_missing_after_pruning" in codes
    assert {item.code for item in schema4._validate_server_record(s8.SERVER, corrupt)} >= {
        "invalid_learned_state"
    }
    pipeline = chain(200)
    for value in (missing, corrupt):
        with pytest.raises(schema4.Schema4ValidationError, match="cannot be reconstructed"):
            integration.rebuild(value, pipeline.events, pipeline.coverage)
    # On load, the server is quarantined: no confidence, no prediction.
    state = {"servers": {s8.SERVER: missing}}
    loaded = schema4.deserialize_schema4_state(
        {**_empty_root(), **state}, quarantine_invalid_servers=True
    )
    assert loaded.state["servers"][s8.SERVER]["authority_status"] == "quarantined"

    oversized = json.loads(schema4.canonical_json_bytes(record))
    oversized["physical_events"] = oversized["physical_events"] * 2
    codes = {item.code for item in schema4._validate_server_record(s8.SERVER, oversized)}
    assert "raw_event_retention_exceeded" in codes


def _empty_root():
    from dzll_launcher import companion_restart_phase2_storage as storage

    phase3 = storage.new_phase2_state(now=s8.DAY0, generation_id="00000000-0000-4000-8000-0000000000a4")
    raw = schema4.canonical_json_bytes(phase3)
    return schema4.migrate_schema3_state(json.loads(raw), source_bytes=raw, source_mtime_ns=1)


def test_unpruned_raw_era_record_cuts_over_and_prunes_on_its_first_rebuild(monkeypatch):
    pipeline = chain(250)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", False)
    raw_record = drive(pipeline, every=25)
    raw_record = json.loads(schema4.canonical_json_bytes(raw_record))
    raw_record.pop("learned_state")  # a record written before R2
    assert len(raw_record["physical_events"]) == 250
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", True)
    cut = integration.rebuild(raw_record, ordered, pipeline.coverage)

    assert_pruned_record(cut)
    before = schema4.persisted_authority_decision(raw_record)
    after = schema4.persisted_authority_decision(cut)
    assert after.incumbent_shadow_regime.regime_id == before.incumbent_shadow_regime.regime_id
    assert r3.decision_summary(after) == r3.decision_summary(before)


# --------------------------------------------------------------------------
# 4. The learned schedule survives folding and pruning (tiny ring)
# --------------------------------------------------------------------------


MILESTONES = {
    # name: (factory, capacity, expected final displayed percent or None)
    "eight_sparse_80": (r3._sparse_eight, 2, 80),
    "sparse_then_interval_95": (r3._sparse_then_interval, 3, 95),
    "two_intervals_80": (lambda: s8.three_hour_chain(3), 2, 80),
    "three_intervals_95": (lambda: s8.three_hour_chain(4), 2, 95),
    "anomaly_recovers": (s8._anomaly_mid_history, 3, None),
    "qv_sparse": (lambda: integration._qv_sparse(40), 5, None),
    "boundary_challenger": (r3._boundary_challenger, 3, None),
    "stage6_recovery": (r3._stage6_recovery, 3, None),
    "schedule_change": (r3._schedule_change, 20, None),
}


@pytest.mark.parametrize("name", sorted(MILESTONES))
def test_learned_schedule_survives_pruning_with_a_tiny_ring(name, monkeypatch, tiny_ring):
    factory, capacity, percent = MILESTONES[name]
    every = 5 if name == "schedule_change" else 1
    raw = r3.run(factory(), every=every, monkeypatch=monkeypatch)
    tiny_ring(capacity)
    compact = r3.run(factory(), every=every, compact_from=0, monkeypatch=monkeypatch)

    for index, (left, right) in enumerate(zip(raw, compact)):
        assert r3.decision_summary(schema4.persisted_authority_decision(right)) == (
            r3.decision_summary(schema4.persisted_authority_decision(left))
        ), (name, index)
        assert consumer(right) == consumer(left), (name, index)
        assert_pruned_record(right, limit=capacity)
    final = schema4.persisted_learned_state(compact[-1])
    assert final.watermark.folded_event_count > 0
    if percent is not None:
        assert consumer(compact[-1])["percent"] == percent


def test_partial_learner_survives_months_and_improves_after_its_events_are_gone(
    monkeypatch, tiny_ring
):
    """76 %: eight sparse restarts plus one admin restart; all but two raw
    events are pruned, a year passes, one more aligned restart reaches 80 %."""

    def pipeline():
        value = s8.sparse_pipeline()
        value.sparse("admin", [s8.at(6, 17, 20)])
        return value

    tiny_ring(2)
    source = pipeline()
    record = drive(source, every=1)
    assert len(record["physical_events"]) == 2
    assert consumer(record)["percent"] == 76
    later_at = record["migration_audit"]["runtime_updated_at"] + 365 * scoring.DAY
    later = integration.rebuild(record, source.events, source.coverage, updated_at=later_at)
    assert consumer(later)["percent"] == 76
    assert schema4.persisted_authority_decision(later).state.value != "established"

    grown = pipeline()
    grown.sparse("one-more", [s8.at(9, 0)])
    improved = integration.rebuild(later, grown.events, grown.coverage)
    monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", False)
    reference = integration.rebuild(drive(pipeline(), every=1), grown.events, grown.coverage)

    assert len(improved["physical_events"]) == 2
    assert r3.decision_summary(schema4.persisted_authority_decision(improved)) == (
        r3.decision_summary(schema4.persisted_authority_decision(reference))
    )
    assert consumer(improved)["percent"] == 80


def test_harmonic_blocker_survives_pruning_of_the_six_hour_history(monkeypatch, tiny_ring):
    """6 h -> 3 h change: the persisted divisor resolution is invalidated and
    the 6 h harmonic stays blocked after the 6 h raw history is pruned."""

    six = s8.Pipeline()
    six.observe("six", [s8.at(0, 0) + index * 6 * HOUR for index in range(12)])
    six.observe("three", [s8.at(4, 3) + index * P3 for index in range(12)])
    tiny_ring(4)
    records = r3.run(six, every=1, compact_from=0, monkeypatch=monkeypatch)
    final = records[-1]
    assert_pruned_record(final, limit=4)
    state = schema4.persisted_learned_state(final)
    assert (6 * HOUR, P3) not in {(item.period, item.divisor) for item in state.divisors}
    decision = schema4.persisted_authority_decision(final)
    six_hour = [
        item for item in decision.candidates if item.candidate_period_seconds == 6 * HOUR
    ]
    assert all(item.harmonic_blockers or not item.h3_gate for item in six_hour)
    selected = decision.selected_shadow_regime
    assert selected is None or selected.candidate_period_seconds != 6 * HOUR


# --------------------------------------------------------------------------
# 5. Expected windows stay bounded when restarts stop
# --------------------------------------------------------------------------


def _schema3_with_misses(events, coverage, misses):
    server = runtime._new_server_runtime(s8.SERVER, detection.DetectionConfig())
    server.regime_generation = "r2-fixture-generation"
    server.events = list(events)[-runtime.MAX_FINALIZED_EVENTS:]
    server.coverage = list(coverage)[-runtime.MAX_COVERAGE_SEGMENTS:]
    server.event_seq = max(item.sequence for item in events)
    server.routed_fingerprints = {item.fingerprint for item in server.events}
    server.expected_misses = list(misses)[-60:]
    return runtime._serialize_server(server)


def test_expected_windows_stay_bounded_and_misses_keep_counting_when_restarts_stop(
    monkeypatch,
):
    pipeline = s8.three_hour_chain(20)
    events = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    last = events[-1].canonical_phase_at

    def run(compact):
        monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", compact)
        record = integration.rebuild({}, events, pipeline.coverage)
        misses = []
        seen = []
        for index in range(1, 601):
            misses.append(scoring.CoveredExpectedMiss(P3, last + index * P3, f"dead-{index}"))
            if index % 40 == 0:
                now = last + index * P3 + HOUR
                record = schema4.rebuild_schema4_server_record(
                    record,
                    _schema3_with_misses(events, pipeline.coverage, misses),
                    server_key=s8.SERVER,
                    updated_at=now,
                )
                seen.append(record)
        return seen

    raw = run(False)
    compact = run(True)
    assert len(raw[-1]["expected_window_results"]) == 600  # raw keeps every lineage
    for left, right in zip(raw, compact):
        assert len(right["expected_window_results"]) <= schema4.EXPECTED_WINDOW_RETENTION_LIMIT
        assert not schema4._validate_server_record(s8.SERVER, right)
        a = schema4.persisted_authority_decision(left)
        b = schema4.persisted_authority_decision(right)
        assert (b.state, b.suspicion_level, b.prediction_usable) == (
            a.state,
            a.suspicion_level,
            a.prediction_usable,
        )
    state = schema4.persisted_learned_state(compact[-1])
    assert state.watermark.window_end_at is not None
    assert sum(item.genuine_miss_count for item in state.misses) >= 600 - (
        schema4.EXPECTED_WINDOW_RETENTION_LIMIT
    )


@pytest.mark.parametrize("kind", ["chain", "sparse"])
def test_window_lineages_fold_with_their_restarts_and_references_stay_valid(kind, monkeypatch):
    """A window per restart (each retracted by its restart): lineages fold
    with the restarts; a retained window whose restart was pruned (a whole
    sparse chain folds past it) has that reference cleared."""

    pipeline = chain(200) if kind == "chain" else sparse(200)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)

    def run(compact):
        monkeypatch.setattr(schema4, "COMPACT_AUTHORITY_ENABLED", compact)
        record = {}
        seen = []
        for stop in range(10, 201, 10):
            prefix = ordered[:stop]
            misses = [
                scoring.CoveredExpectedMiss(P3, item.canonical_phase_at, f"hit-{item.event_id}")
                for item in prefix
            ]
            record = schema4.rebuild_schema4_server_record(
                record,
                _schema3_with_misses(prefix, pipeline.coverage, misses),
                server_key=s8.SERVER,
                updated_at=prefix[-1].finalized_at + 60,
            )
            seen.append(record)
        return seen

    raw = run(False)
    compact = run(True)
    for left, right in zip(raw, compact):
        assert_pruned_record(right)
        assert len(right["expected_window_results"]) <= 2 * (LIMIT + 1)
        assert r3.decision_summary(schema4.persisted_authority_decision(right)) == (
            r3.decision_summary(schema4.persisted_authority_decision(left))
        )
    assert len(raw[-1]["expected_window_results"]) == 2 * 200
    cleared = [
        item
        for item in compact[-1]["expected_window_results"]
        if schema4.FOLDED_REFERENCE_CLEARED_REASON in item["reason_codes"]
    ]
    if kind == "sparse":
        assert cleared and all(item["qualifying_finalized_event_id"] is None for item in cleared)


def test_window_overflow_fold_keeps_recent_unsettled_windows():
    state = learned.empty_learned_state(s8.SERVER)
    windows = [s8.genuine_miss(P3, s8.at(0, 0) + index * P3, key=f"w{index}") for index in range(300)]
    now = windows[-1].window_end_at + 600
    folded = schema4.fold_expected_window_overflow(state, windows, now=now)
    unfolded = [item for item in windows if item.window_end_at > folded.watermark.window_end_at]
    assert len(unfolded) <= schema4.EXPECTED_WINDOW_RETENTION_LIMIT
    assert sum(item.genuine_miss_count for item in folded.misses) == 300 - len(unfolded)
    # Nothing that ended within the settle period is folded.
    assert all(
        item.window_end_at > now - schema4.EXPECTED_WINDOW_FOLD_SETTLE_SECONDS
        for item in windows
        if item.window_end_at > folded.watermark.window_end_at
    ) or len(unfolded) == schema4.EXPECTED_WINDOW_RETENTION_LIMIT
    recent_only = schema4.fold_expected_window_overflow(
        state, windows, now=windows[-1].window_end_at, settle_seconds=400 * P3
    )
    assert recent_only == state
    assert schema4.fold_expected_window_overflow(state, windows[:100], now=now) == state


# --------------------------------------------------------------------------
# 6. Alert deduplication and strict references
# --------------------------------------------------------------------------


def _consumer_payload(record, *, generic_event_ids, occurrence):
    decision = schema4.persisted_authority_decision(record)
    regime_id = decision.selected_shadow_regime.regime_id
    consumer_id = "r4-consumer"
    alerts = [
        state4.alert_record(
            key=consumers4.AuthoritySuppressionKey(
                kind=consumers4.AuthoritySuppressionKind.SCHEDULED_WARNING,
                server_key=s8.SERVER,
                regime_id=regime_id,
                expected_occurrence_at=occurrence,
            ),
            authority_consumer_decision_id=consumer_id,
            created_at=s8.DAY0,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
        )
    ]
    for index, event_id in enumerate(generic_event_ids):
        reserved = state4.alert_record(
            key=consumers4.AuthoritySuppressionKey(
                kind=consumers4.AuthoritySuppressionKind.GENERIC_RECOVERY,
                server_key=s8.SERVER,
                event_id=event_id,
            ),
            authority_consumer_decision_id=consumer_id,
            created_at=s8.DAY0 + index,
            lifecycle=state4.AlertLifecycle.RESERVED,
            action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE,
        )
        emitted = state4.alert_record(
            key=reserved.suppression_key(),
            authority_consumer_decision_id=consumer_id,
            created_at=s8.DAY0 + index + 0.5,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
            supersedes_record_id=reserved.record_id,
        )
        alerts.extend((reserved, emitted))
    ledger = replace(
        state4.ledger_from_mapping(state4.empty_consumer_state(s8.SERVER), server_key=s8.SERVER),
        current_authority_consumer_decision_id=consumer_id,
        decisions=(
            state4.ConsumerDecisionRecord(
                schema_version=state4.CONSUMER_STATE_SCHEMA_VERSION,
                semantic_version=state4.CONSUMER_STATE_SEMANTIC_VERSION,
                decision_id=consumer_id,
                authority_decision_id=decision.decision_id,
                server_key=s8.SERVER,
                selected_regime_id=regime_id,
                presentation_state=consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE.value,
                cycle_period_seconds=P3,
                confidence_display_value=0.97,
                next_expected_restart_at=None,
                countdown_safe=True,
                prediction_suspended=False,
                reason_codes=("r4_fixture",),
            ),
        ),
        alert_records=tuple(alerts),
    )
    return state4.ledger_to_mapping(ledger)


def test_warnings_stay_suppressed_and_references_stay_valid_after_pruning():
    pipeline = chain(300)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    record = drive(chain(120), every=10)
    occurrence = int(ordered[130].canonical_phase_at)
    by_time = sorted(record["physical_events"], key=lambda item: item["canonical_phase_at"])
    oldest_kept = by_time[1]["event_id"]  # not the anchor
    newest = by_time[-1]["event_id"]
    record["consumer_state"] = _consumer_payload(
        record, generic_event_ids=(oldest_kept, newest), occurrence=occurrence
    )
    assert not schema4._validate_server_record(s8.SERVER, record)
    ledger = state4.ledger_from_mapping(record["consumer_state"], server_key=s8.SERVER)
    warning_key = next(
        item for item in state4.fired_suppression_keys(ledger)
        if item.kind is consumers4.AuthoritySuppressionKind.SCHEDULED_WARNING
    )
    regime_id = schema4.persisted_authority_decision(record).selected_shadow_regime.regime_id

    for stop in range(130, 301, 10):
        record = integration.rebuild(record, ordered[:stop], pipeline.coverage)
        assert_pruned_record(record)
        fired = state4.fired_suppression_keys(
            state4.ledger_from_mapping(record["consumer_state"], server_key=s8.SERVER)
        )
        assert warning_key in fired
        assert schema4.persisted_authority_decision(record).selected_shadow_regime.regime_id == regime_id
    retained = {item["event_id"] for item in record["physical_events"]}
    assert oldest_kept not in retained and newest not in retained
    alerts = record["consumer_state"]["alert_records"]
    assert all(item["namespace"] == "scheduled_warning" for item in alerts)
    assert record["consumer_state"]["compaction_metadata"]["compacted_record_count"] == 4
    # The key the consumer derives for that occurrence is the suppressed key:
    # same regime identity, same occurrence.
    assert warning_key == consumers4.AuthoritySuppressionKey(
        kind=consumers4.AuthoritySuppressionKind.SCHEDULED_WARNING,
        server_key=s8.SERVER,
        regime_id=regime_id,
        expected_occurrence_at=occurrence,
    )


def test_runtime_refuses_generic_recovery_for_a_pruned_event_and_exports_pruned_records(tmp_path):
    from dzll_launcher import companion_restart_phase2_schema4_runtime as live4

    active = tmp_path / "companion_restart_learning_phase2.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=active, legacy_path=tmp_path / "companion_restart_learning.json", now=s8.DAY0
    )
    pipeline = chain(160)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        for stop in range(40, 161, 40):
            backend.update_server_from_schema3(
                s8.SERVER,
                integration.schema3_record(ordered[:stop], pipeline.coverage),
                updated_at=ordered[stop - 1].finalized_at + 60,
                durable_reason="r4_fixture",
            )
            backend.flush()
        record = backend.state["servers"][s8.SERVER]
        assert_pruned_record(record)
        step = production_step(record)
        decision = s8.consume(step, now=step.now)
        pruned_key = consumers4.AuthoritySuppressionKey(
            kind=consumers4.AuthoritySuppressionKind.GENERIC_RECOVERY,
            server_key=s8.SERVER,
            event_id=ordered[0].event_id,
        )
        refused = backend.reserve_consumer_alert(
            decision=decision, key=pruned_key, created_at=step.now
        )
        assert not refused.reserved
        assert "generic_recovery_event_not_retained" in refused.reason_codes
        live_key = replace(pruned_key, event_id=ordered[-1].event_id)
        accepted = backend.reserve_consumer_alert(decision=decision, key=live_key, created_at=step.now)
        assert accepted.reserved
        again = backend.reserve_consumer_alert(decision=decision, key=live_key, created_at=step.now)
        assert not again.reserved
        exported = backend.export_snapshot().canonical_bytes
    finally:
        backend.close()

    validated = transfer.validate_external_learning_bytes(exported, source_filename="export.json")
    assert validated.server_count == 1
    reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        record = reopened.state["servers"][s8.SERVER]
    finally:
        reopened.close(flush=False)
    assert_pruned_record(record)
    assert len(record["physical_events"]) == LIMIT
