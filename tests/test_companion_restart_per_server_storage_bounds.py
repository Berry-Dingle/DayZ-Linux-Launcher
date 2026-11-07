"""Retention R6: per-server storage bounds.

Regime history, decision-history payloads, expected-window bursts, the
on-disk schema-3 copy and quarantine wrappers.  Synthetic data only; every
file lives under pytest's ``tmp_path``.
"""

from __future__ import annotations

import copy
import json

import pytest

import test_companion_restart_compact_authority as r3
import test_companion_restart_compact_cutover as r4
import test_companion_restart_learned_state_integration as integration
import test_companion_restart_phase2_consumer_state as fx
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import companion_restart_phase2_scoring as scoring


HOUR = scoring.HOUR
P3 = 3 * HOUR
SUPERSEDED = authority.RegimeRecordStatus.SUPERSEDED.value


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def schedule_changes(changes, *, per=8, gap_hours=6, phases=23, step_minutes=7):
    """``changes`` genuine schedule changes: continuous 3 h segments whose phase
    moves by ``step_minutes`` each time, cycling through ``phases`` phases (so
    earlier schedules return and are re-acquired)."""

    pipeline = s8.Pipeline()
    start = s8.at(0, 0)
    for index in range(changes + 1):
        first = start + (index % phases) * step_minutes * 60
        pipeline.observe(f"segment-{index}", [first + step * P3 for step in range(per)])
        start = first + per * P3 + gap_hours * HOUR
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


def covered_superseded(record):
    state = schema4.persisted_learned_state(record)
    consumer = schema4.consumer_referenced_regime_ids(record["consumer_state"])
    return [
        item
        for item in record["regimes"]
        if item["regime_id"] not in consumer
        and schema4.superseded_regime_covered(schema4._regime_from(item), state)
    ]


def assert_storage_bounded(record, server=s8.SERVER):
    assert not schema4._validate_server_record(server, record)
    assert schema4.PER_SERVER_STORAGE_BOUNDED_REASON in record["reason_codes"]
    decision_regimes = set(record["authority_decision"]["regime_ids"])
    consumer = schema4.consumer_referenced_regime_ids(record["consumer_state"])
    assert {item["regime_id"] for item in record["regimes"]} <= decision_regimes | consumer
    assert len(covered_superseded(record)) <= schema4.REGIME_SUPERSEDED_TRIM_THRESHOLD + 1
    assert len(record["expected_window_results"]) <= schema4.EXPECTED_WINDOW_HARD_LIMIT
    assert len(record["physical_events"]) <= schema4.RAW_EVENT_RETENTION_LIMIT
    assert len(record["authority_decisions"]) <= schema4.AUTHORITY_DECISION_RETENTION_LIMIT
    for item in record["authority_decisions"]:
        assert item[schema4.HISTORY_COMPACTED_FIELD] is True
        assert "active_suspicion_evidence_ids" not in item
    assert isinstance(record["authority_decision"]["active_suspicion_evidence_ids"], list)


# --------------------------------------------------------------------------
# 1. Regime history across many genuine schedule changes
# --------------------------------------------------------------------------


def test_regime_history_stays_bounded_and_decisions_match_unbounded_history(monkeypatch):
    pipeline = schedule_changes(60)
    bounded = []
    drive(pipeline, every=8, check=lambda stop, record: bounded.append(copy.deepcopy(record)))

    monkeypatch.setattr(schema4, "bound_prior_regimes", lambda prior, state: prior)
    monkeypatch.setattr(schema4, "_per_server_storage_errors", lambda record, ids: [])
    unbounded = []
    drive(pipeline, every=8, check=lambda stop, record: unbounded.append(copy.deepcopy(record)))

    incumbents = []
    for left, right in zip(unbounded, bounded):
        assert_storage_bounded(right)
        a = schema4.persisted_authority_decision(left)
        b = schema4.persisted_authority_decision(right)
        assert r3.decision_summary(b) == r3.decision_summary(a)
        for field in ("incumbent_shadow_regime", "selected_shadow_regime"):
            assert (getattr(b, field) and getattr(b, field).regime_id) == (
                getattr(a, field) and getattr(a, field).regime_id
            )
        assert r4.consumer(right) == r4.consumer(left)
        assert r3.warning_key(r4.production_step(right)) == r3.warning_key(
            r4.production_step(left)
        )
        if b.incumbent_shadow_regime is not None and (
            not incumbents or incumbents[-1] != b.incumbent_shadow_regime.regime_id
        ):
            incumbents.append(b.incumbent_shadow_regime.regime_id)
    assert len(incumbents) >= 45  # genuine changes happened
    assert len(set(incumbents)) > schema4.REGIME_SUPERSEDED_TRIM_THRESHOLD + 4
    assert max(len(item["regimes"]) for item in bounded) <= (
        schema4.REGIME_SUPERSEDED_TRIM_THRESHOLD + 4
    )


def test_bound_prior_regimes_keeps_references_and_uncovered_history():
    state = learned.empty_learned_state(s8.SERVER)
    pipeline = schedule_changes(14)
    record = drive(pipeline, every=4)
    prior = schema4.persisted_authority_decision(record)
    superseded = [item for item in prior.regimes if item.status.value == SUPERSEDED]
    assert superseded
    # An empty learned state covers nothing: no superseded regime is dropped.
    assert schema4.bound_prior_regimes(prior, state).regimes == prior.regimes
    covered = schema4.persisted_learned_state(record)
    trimmed = schema4.bound_prior_regimes(prior, covered)
    kept = {item.regime_id for item in trimmed.regimes}
    assert prior.incumbent_shadow_regime.regime_id in kept
    assert prior.selected_shadow_regime.regime_id in kept
    named = {
        regime_id
        for context in prior.challenger_contexts
        for regime_id in (context.incumbent_regime_id, context.proposed_regime_id)
    }
    for item in prior.regimes:
        if item.regime_id in named and item.status.value != SUPERSEDED:
            assert item.regime_id in kept
    newest = sorted(superseded, key=lambda item: (item.superseded_at, item.regime_id))
    assert {
        item.regime_id for item in newest[-schema4.REGIME_SUPERSEDED_RETENTION:]
    } <= kept
    covered_count = sum(
        1 for item in superseded if schema4.superseded_regime_covered(item, covered)
    )
    if covered_count <= schema4.REGIME_SUPERSEDED_TRIM_THRESHOLD:
        assert trimmed.regimes == prior.regimes  # below the threshold nothing is trimmed


# --------------------------------------------------------------------------
# 2. Decision history payloads
# --------------------------------------------------------------------------


def test_decision_history_payloads_are_compacted_and_the_current_decision_is_intact():
    record = drive(r4.chain(120), every=10)
    assert_storage_bounded(record)
    current = record["authority_decision"]
    history = {item["decision_id"]: item for item in record["authority_decisions"]}
    entry = history[current["decision_id"]]
    for field, count_field in schema4.HISTORY_REFERENCE_FIELDS:
        assert entry[count_field] == len(current[field])
    assert entry == schema4.compact_decision_history_payload(current)
    assert schema4.compact_decision_history_payload(entry) == entry  # idempotent
    loaded = schema4.persisted_authority_decision(record)
    assert list(loaded.active_suspicion_evidence_ids) == current["active_suspicion_evidence_ids"]


@pytest.mark.parametrize(
    "mutation",
    ["restore_list", "negative_count", "bad_digest", "uncompacted_on_marked_record"],
)
def test_validation_rejects_malformed_or_missing_history_compaction(mutation):
    record = drive(r4.chain(60), every=10)
    entry = record["authority_decisions"][0]
    if mutation == "restore_list":
        entry["candidate_ids"] = []
    elif mutation == "negative_count":
        entry["candidate_count"] = -1
    elif mutation == "bad_digest":
        entry[schema4.HISTORY_REFERENCE_DIGEST_FIELD] = "not-a-digest"
    else:
        record["authority_decisions"][0] = {
            key: value
            for key, value in entry.items()
            if key not in {schema4.HISTORY_COMPACTED_FIELD}
        }
    issues = schema4._validate_server_record(s8.SERVER, record)
    assert any(
        item.code in {"invalid_compacted_decision_history", "decision_history_not_compacted"}
        for item in issues
    )


def test_stopped_schedule_history_shrinks_and_suspicion_is_unchanged(monkeypatch):
    pipeline = s8.three_hour_chain(20)
    events = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    last = events[-1].canonical_phase_at

    def run(compact_history):
        if not compact_history:
            monkeypatch.setattr(schema4, "compact_decision_history_payload", lambda item: dict(item))
            monkeypatch.setattr(schema4, "_per_server_storage_errors", lambda record, ids: [])
        record = integration.rebuild({}, events, pipeline.coverage)
        misses = []
        seen = []
        for index in range(1, 801):
            misses.append(scoring.CoveredExpectedMiss(P3, last + index * P3, f"dead-{index}"))
            if index % 40 == 0:
                record = schema4.rebuild_schema4_server_record(
                    record,
                    r4._schema3_with_misses(events, pipeline.coverage, misses),
                    server_key=s8.SERVER,
                    updated_at=last + index * P3 + HOUR,
                )
                seen.append(record)
        monkeypatch.undo()
        return seen

    compacted = run(True)
    full = run(False)
    for left, right in zip(full, compacted):
        assert_storage_bounded(right)
        a = schema4.persisted_authority_decision(left)
        b = schema4.persisted_authority_decision(right)
        assert (b.state, b.suspicion_level, b.prediction_usable, b.active_suspicion_evidence_ids) == (
            a.state,
            a.suspicion_level,
            a.prediction_usable,
            a.active_suspicion_evidence_ids,
        )
    before = len(schema4.canonical_json_bytes(full[-1]["authority_decisions"]))
    after = len(schema4.canonical_json_bytes(compacted[-1]["authority_decisions"]))
    assert after * 4 < before


# --------------------------------------------------------------------------
# 3. Expected-window burst
# --------------------------------------------------------------------------


def test_a_window_burst_inside_24_hours_cannot_exceed_the_hard_bound():
    state = learned.empty_learned_state(s8.SERVER)
    windows = [
        s8.genuine_miss(P3, s8.at(0, 0) + index * 60, key=f"burst-{index}") for index in range(700)
    ]
    now = windows[-1].window_end_at + 60
    settled = schema4.fold_expected_window_overflow(state, windows, now=now)
    assert settled == state  # the 24-hour settle rule alone folds nothing
    folded = schema4.fold_expected_window_burst(settled, windows)
    unfolded = [item for item in windows if item.window_end_at > folded.watermark.window_end_at]
    assert len(unfolded) <= schema4.EXPECTED_WINDOW_HARD_LIMIT
    # Every folded genuine miss keeps counting.
    assert sum(item.genuine_miss_count for item in folded.misses) == len(windows) - len(unfolded)
    assert schema4.fold_expected_window_burst(folded, windows) is folded
    assert schema4.fold_expected_window_burst(state, windows[:100]) is state


def test_a_burst_of_windows_in_a_rebuild_is_folded_marked_and_validated():
    pipeline = s8.three_hour_chain(20)
    events = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    last = events[-1].canonical_phase_at
    record = integration.rebuild({}, events, pipeline.coverage)
    misses = []
    for batch in range(12):
        misses.extend(
            scoring.CoveredExpectedMiss(P3, last + HOUR + (batch * 60 + index) * 60, f"b{batch}-{index}")
            for index in range(60)
        )
        record = schema4.rebuild_schema4_server_record(
            record,
            r4._schema3_with_misses(events, pipeline.coverage, misses[-60:]),
            server_key=s8.SERVER,
            updated_at=last + HOUR + (batch + 1) * 3600 + 60,
        )
        assert_storage_bounded(record)
    assert schema4.EXPECTED_WINDOW_BURST_FOLDED_REASON in record["reason_codes"]
    assert len(record["expected_window_results"]) <= schema4.EXPECTED_WINDOW_HARD_LIMIT
    over = copy.deepcopy(record)
    template = over["expected_window_results"][0]
    over["expected_window_results"] = over["expected_window_results"] + [
        dict(template, result_id=f"extra-{index}") for index in range(500)
    ]
    assert any(
        item.code == "expected_window_retention_exceeded"
        for item in schema4._validate_server_record(s8.SERVER, over)
    )


# --------------------------------------------------------------------------
# 4. One stored schema-3 copy
# --------------------------------------------------------------------------


def test_schema3_records_are_stored_once_and_restored_on_load(tmp_path):
    state = fx._state(two=True)
    raw = schema4.serialize_schema4_state(state)
    disk = json.loads(raw)
    assert disk["schema3_snapshot"]["servers"] == {}
    assert all(
        isinstance(record["legacy_schema3_record"], dict) for record in disk["servers"].values()
    )
    loaded = schema4.deserialize_schema4_bytes(raw, quarantine_invalid_servers=False)
    assert loaded.report.valid
    assert loaded.state["schema3_snapshot"] == state["schema3_snapshot"]
    assert schema4.serialize_schema4_state(loaded.state) == raw
    # A file written before R6 (both copies) still loads identically.
    legacy_bytes = schema4.canonical_json_bytes(state)
    assert len(legacy_bytes) > len(raw)
    old = schema4.deserialize_schema4_bytes(legacy_bytes, quarantine_invalid_servers=False)
    assert old.report.valid and old.state == loaded.state
    assert schema4.serialize_schema4_state(old.state) == raw
    # A snapshot entry that differs from its record is never dropped.
    differing = copy.deepcopy(state)
    differing["schema3_snapshot"]["servers"][fx.SERVER]["regime_generation"] = "differs"
    kept = json.loads(schema4.serialize_schema4_state(differing))
    assert kept["schema3_snapshot"]["servers"][fx.SERVER]["regime_generation"] == "differs"


def test_runtime_projection_rebuild_reopen_and_transfer_use_the_single_copy(tmp_path):
    backend, path, original = fx._backend(tmp_path, fx._state(two=True))
    try:
        projection = backend.schema3_projection()
        assert set(projection["servers"]) == {fx.SERVER, fx.OTHER}
        assert json.loads(path.read_bytes())["schema3_snapshot"]["servers"] == {}
        record = projection["servers"][fx.SERVER]
        assert backend.update_server_from_schema3(
            fx.SERVER, record, updated_at=fx.BASE + 11 * HOUR, durable_reason="r6_fixture"
        ) in {True, False}
        backend.flush()
        exported = backend.export_snapshot().canonical_bytes
    finally:
        backend.close()
    reopened = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        assert reopened.schema3_projection() == projection or set(
            reopened.schema3_projection()["servers"]
        ) == {fx.SERVER, fx.OTHER}
    finally:
        reopened.close(flush=False)
    validated = transfer.validate_external_learning_bytes(exported, source_filename="r6.json")
    assert validated.server_count == 2
    assert json.loads(validated.canonical_bytes)["schema3_snapshot"]["servers"] == {}
    # An import file in the pre-R6 layout (both copies) is still accepted.
    legacy = schema4.canonical_json_bytes(
        schema4.deserialize_schema4_bytes(exported, quarantine_invalid_servers=False).state
    )
    assert transfer.validate_external_learning_bytes(legacy).canonical_bytes == (
        validated.canonical_bytes
    )


# --------------------------------------------------------------------------
# 5. Quarantine wrappers
# --------------------------------------------------------------------------


def _broken(record, *, pad=0):
    value = copy.deepcopy(record)
    value["authority_decision"]["decision_id"] = "missing-decision"
    if pad:
        value["migration_audit"]["r6_padding"] = ["x" * 1000] * pad
    return value


def test_small_invalid_records_are_kept_verbatim_and_large_ones_are_summarised():
    state = fx._state(two=True)
    small = _broken(state["servers"][fx.SERVER])
    large = _broken(state["servers"][fx.OTHER], pad=3000)
    state["servers"][fx.SERVER] = small
    state["servers"][fx.OTHER] = large
    loaded = schema4.deserialize_schema4_state(state, quarantine_invalid_servers=True)
    assert set(loaded.report.quarantined_server_keys) == {fx.SERVER, fx.OTHER}
    kept = loaded.state["servers"][fx.SERVER]
    summarised = loaded.state["servers"][fx.OTHER]
    assert kept["quarantined_payload"] == small
    assert kept["quarantined_payload_bytes"] == len(schema4.canonical_json_bytes(small))
    assert "quarantined_payload" not in summarised
    assert summarised["quarantined_payload_bytes"] > schema4.QUARANTINE_PAYLOAD_RETENTION_BYTES
    assert summarised["quarantined_payload_sha256"] == schema4._sha256(
        schema4.canonical_json_bytes(large)
    )
    assert summarised["quarantined_payload_summary"]["fields"]["physical_events"]["kind"] == "list"
    assert "quarantined_payload_summarized" in summarised["reason_codes"]
    assert summarised["validation_errors"] and summarised["validation_error_count"] >= 1
    raw = schema4.serialize_schema4_state(loaded.state)
    assert len(raw) < schema4.QUARANTINE_PAYLOAD_RETENTION_BYTES + 2_000_000
    again = schema4.deserialize_schema4_bytes(raw)
    assert again.state["servers"][fx.OTHER] == summarised
    assert set(again.report.quarantined_server_keys) == {fx.SERVER, fx.OTHER}


def test_existing_oversized_wrappers_and_error_lists_are_bounded_on_load():
    state = fx._state(two=True)
    payload = _broken(state["servers"][fx.OTHER], pad=3000)
    state["servers"][fx.OTHER] = {
        "server_key": fx.OTHER,
        "authority_status": "quarantined",
        "schema_version": 4,
        "semantic_version": schema4.SCHEMA4_AUTHORITY_SEMANTICS_VERSION,
        "validation_errors": [{"code": "c", "detail": "d" * 5000}] * 500,
        "quarantined_payload": payload,
        "reason_codes": ["schema4_server_authority_quarantined"],
    }
    loaded = schema4.deserialize_schema4_state(state, quarantine_invalid_servers=True)
    wrapper = loaded.state["servers"][fx.OTHER]
    assert "quarantined_payload" not in wrapper
    assert len(wrapper["validation_errors"]) == schema4.QUARANTINE_VALIDATION_ERROR_LIMIT
    assert wrapper["validation_error_count"] == 500
    assert all(
        len(item["detail"]) <= schema4.QUARANTINE_DETAIL_CHARACTERS
        for item in wrapper["validation_errors"]
    )
    assert loaded.report.root_valid and fx.SERVER in loaded.report.valid_server_keys


def test_quarantined_servers_stay_untrusted_and_valid_servers_are_untouched(tmp_path):
    state = fx._state(two=True)
    valid_before = copy.deepcopy(state["servers"][fx.SERVER])
    state["servers"][fx.OTHER] = _broken(state["servers"][fx.OTHER], pad=3000)
    path = tmp_path / "quarantine-schema4.json"
    path.write_bytes(schema4.canonical_json_bytes(state))
    backend = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        assert backend.authority_decision(fx.OTHER) is None
        assert not backend.server_authority_valid(fx.OTHER)
        assert backend.consumer_fired_keys(fx.OTHER) == frozenset()
        with pytest.raises(schema4.Schema4ValidationError):
            backend.update_server_from_schema3(
                fx.OTHER,
                state["schema3_snapshot"]["servers"][fx.OTHER],
                updated_at=fx.BASE + 11 * HOUR,
                durable_reason="r6_fixture",
            )
        assert backend.state["servers"][fx.SERVER] == valid_before
        assert backend.authority_decision(fx.SERVER) is not None
    finally:
        backend.close(flush=False)


# --------------------------------------------------------------------------
# 6. Repeated rebuild and reopen cycles through the real runtime
# --------------------------------------------------------------------------


def test_repeated_rebuild_and_reopen_cycles_stay_bounded_and_byte_stable(tmp_path):
    active = tmp_path / "companion_restart_learning_phase2.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=active, legacy_path=tmp_path / "companion_restart_learning.json", now=s8.DAY0
    )
    pipeline = schedule_changes(30)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    sizes = []
    for stop in range(8, len(ordered) + 1, 8):
        backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
        try:
            backend.update_server_from_schema3(
                s8.SERVER,
                integration.schema3_record(ordered[:stop], pipeline.coverage),
                updated_at=ordered[stop - 1].finalized_at + 60,
                durable_reason="r6_fixture",
            )
            backend.flush()
            record = backend.state["servers"][s8.SERVER]
            assert_storage_bounded(record)
        finally:
            backend.close()
        disk = json.loads(active.read_bytes())
        assert disk["schema3_snapshot"]["servers"] == {}
        sizes.append(active.stat().st_size)
    # At most one settle rebuild (a regime trim), then a fixed point.
    for _ in range(2):
        reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
        try:
            reopened.update_server_from_schema3(
                s8.SERVER,
                integration.schema3_record(ordered, pipeline.coverage),
                updated_at=ordered[-1].finalized_at + 60,
                durable_reason="r6_fixture",
            )
        finally:
            reopened.close()
    before = active.read_bytes()
    reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        assert not reopened.update_server_from_schema3(
            s8.SERVER,
            integration.schema3_record(ordered, pipeline.coverage),
            updated_at=ordered[-1].finalized_at + 60,
            durable_reason="r6_fixture",
        )
        assert not reopened.dirty
    finally:
        reopened.close()
    assert active.read_bytes() == before
    assert max(sizes[-10:]) < 1.25 * min(sizes[-10:])
