from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 1_900_000_000.0
SERVER = "schema4.example:2302"
BOB = "51.195.74.63:3058"


def _event(sequence: int, at: float, *, provenance: bool = True):
    event_id = f"event-{sequence}-{int(at)}"
    return detection.PhysicalRestartEvent(
        event_id=event_id,
        sequence=sequence,
        fingerprint=f"{SERVER}|monitor|{sequence}|fixture|restart",
        server_key=SERVER,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        authenticity=0.85,
        schedule_weight_suggestion=0.85,
        drain=detection.DrainSummary(
            None, None, None, None, False, None, None, 0, 0, False
        ),
        outage=detection.OutageSummary(
            at - 70,
            at - 60,
            3,
            (detection.InfoStatus.TIMEOUT,),
            at,
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("schema4_fixture",),
        app_session_id="app" if provenance else None,
        monitoring_session_id="monitor" if provenance else None,
        poll_generation=1 if provenance else None,
        continuity_chain_id="chain" if provenance else None,
        provenance_version=(
            continuity.CONTINUITY_PROVENANCE_VERSION if provenance else 0
        ),
    )


def _schema3_state(*, provenance: bool = True) -> dict:
    events = tuple(_event(index + 1, BASE + index * 3 * scoring.HOUR, provenance=provenance) for index in range(4))
    coverage = scoring.CoverageSegment(
        BASE,
        BASE + 9 * scoring.HOUR,
        scoring.CoverageKind.ONLINE_HEALTHY,
        10.0,
        server_key=SERVER if provenance else None,
        app_session_id="app" if provenance else None,
        monitoring_session_id="monitor" if provenance else None,
        poll_generation=1 if provenance else None,
        continuity_chain_id="chain" if provenance else None,
        provenance_version=(
            continuity.CONTINUITY_PROVENANCE_VERSION if provenance else 0
        ),
    )
    state = storage.new_phase2_state(
        now=int(BASE + 10 * scoring.HOUR),
        generation_id="00000000-0000-4000-8000-000000000004",
    )
    state["servers"] = {
        SERVER: {
            "runtime_state_version": 1,
            "scoring_semantics_version": 1,
            "aggregate_semantics_version": 1,
            "events": [runtime._serialize_event(item) for item in events],
            "event_seq": 4,
            "folded_through_event_seq": 0,
            "coverage_segments": [runtime._serialize_coverage(coverage)],
            "monitoring_sessions": ([{
                "session_id": "monitor",
                "app_session_id": "app",
                "poll_generation": 1,
                "continuity_chain_id": "chain",
                "continuity_chain_ids": ["chain"],
                "provenance_version": 1,
                "started_at": BASE - 120,
                "ended_at": BASE + 9 * scoring.HOUR + 120,
                "reason": "shutdown",
            }] if provenance else []),
            "expected_window_misses": [],
            "fired_keys": [],
            "routed_fingerprints": [item.fingerprint for item in events],
            "incumbent_period_seconds": None,
            "regime_generation": "fixture-regime",
            "aggregate": runtime._serialize_aggregate(scoring.LongTermAggregate()),
            "prior_regimes": [],
            "active_episode": None,
            "incomplete_episodes": [],
            "candidate_diagnostics": {},
            "consumer_adapter_version": 1,
        }
    }
    return state


def _schema3_state_with_retractions(
    count: int = 1,
    *,
    established: bool = False,
) -> dict:
    state = _schema3_state()
    record = state["servers"][SERVER]
    if established:
        fifth = _event(5, BASE + 12 * scoring.HOUR)
        record["events"].append(runtime._serialize_event(fifth))
        record["event_seq"] = 5
        record["routed_fingerprints"].append(fifth.fingerprint)
        record["coverage_segments"][0]["end_at"] = BASE + 12 * scoring.HOUR
        record["monitoring_sessions"][0]["ended_at"] = (
            BASE + 12 * scoring.HOUR + 120
        )
        state["updated_at"] = int(BASE + 13 * scoring.HOUR)
    record["expected_window_misses"] = []
    for index in range(count):
        expected = BASE + 10 * 60 + index * 10 * 60
        record["expected_window_misses"].append({
            "period_seconds": 3 * scoring.HOUR,
            "expected_at": expected,
            "key": f"fixture-outage-backed-miss-{index}",
            "weight": 1.0,
        })
        record["coverage_segments"].append(
            runtime._serialize_coverage(
                scoring.CoverageSegment(
                    expected - 60,
                    expected + 60,
                    scoring.CoverageKind.OFFLINE_OBSERVED,
                    3.0,
                    server_key=SERVER,
                    app_session_id="app",
                    monitoring_session_id="monitor",
                    poll_generation=1,
                    continuity_chain_id="chain",
                    provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
                )
            )
        )
    return state


def _schema3_state_with_retraction() -> dict:
    return _schema3_state_with_retractions()


def _source_bytes(*, provenance: bool = True) -> bytes:
    return schema4.canonical_json_bytes(_schema3_state(provenance=provenance))


def _migrated(*, provenance: bool = True) -> dict:
    raw = _source_bytes(provenance=provenance)
    return schema4.migrate_schema3_state(
        json.loads(raw),
        source_bytes=raw,
        source_mtime_ns=1_900_000_000_000_000_000,
    )


def _record(state: dict) -> dict:
    return state["servers"][SERVER]


def _write_source(path: Path, *, provenance: bool = True) -> bytes:
    raw = _source_bytes(provenance=provenance)
    path.write_bytes(raw)
    return raw


def _server_semantic_ids(state: dict, *, server_key: str = SERVER) -> dict:
    record = state["servers"][server_key]
    decision = schema4.persisted_authority_decision(record)
    consumer = consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=decision,
            now=1_784_800_000.0,
        )
    )
    revisions = tuple(
        sorted(
            item["result_id"]
            for item in record["expected_window_results"]
            if item["record_type"] == "ExpectedWindowRevision"
        )
    )
    return {
        "revisions": revisions,
        "authority": decision.decision_id,
        "consumer": consumer.decision_id,
        "regime": (
            None
            if decision.selected_shadow_regime is None
            else decision.selected_shadow_regime.regime_id
        ),
    }


def _assert_required_retractions(
    record: dict,
    required: dict[str, tuple],
) -> None:
    revisions = {
        item.reconciliation_revision_id: item
        for item in schema4.persisted_expected_window_ledger(record)
        if isinstance(item, windows.ExpectedWindowRevision)
    }
    assert required.keys() <= revisions.keys()
    for revision_id, semantic in required.items():
        item = revisions[revision_id]
        assert (
            item.supersedes_result_id,
            item.reconciliation_reason,
            item.outcome,
            item.candidate_period_seconds,
        ) == semantic


def test_schema4_round_trip_is_byte_equivalent():
    state = _migrated()
    payload = schema4.serialize_schema4_state(state)
    loaded = schema4.deserialize_schema4_bytes(payload, quarantine_invalid_servers=False)
    assert loaded.report.valid
    assert schema4.serialize_schema4_state(loaded.state) == payload


def test_schema4_serialization_has_stable_ordering():
    state = _migrated()
    reversed_state = copy.deepcopy(state)
    record = _record(reversed_state)
    for name in (
        "physical_events", "continuity_spans", "continuity_chains",
        "interval_relationships", "cadence_streaks", "authority_candidates",
        "challenger_contexts", "regimes", "authority_decisions",
    ):
        record[name].reverse()
    # Canonical migration, rather than arbitrary post-migration list order, is
    # the deterministic input-order contract.
    first = schema4.migrate_schema3_state(
        _schema3_state(), source_bytes=_source_bytes(), source_mtime_ns=7
    )
    source = _schema3_state()
    source["servers"] = dict(reversed(list(source["servers"].items())))
    second = schema4.migrate_schema3_state(
        source, source_bytes=_source_bytes(), source_mtime_ns=7
    )
    assert schema4.serialize_schema4_state(first) == schema4.serialize_schema4_state(second)


def test_reference_validation_accepts_every_persisted_collection():
    report = schema4.validate_schema4_state(_migrated())
    assert report.valid
    assert report.issues == ()


@pytest.mark.parametrize(
    ("collection", "id_field"),
    [
        ("physical_events", "event_id"),
        ("continuity_chains", "chain_id"),
        ("interval_relationships", "relationship_id"),
        ("cadence_streaks", "streak_id"),
        ("authority_candidates", "candidate_id"),
        ("regimes", "regime_id"),
    ],
)
def test_duplicate_ids_are_rejected(collection, id_field):
    state = _migrated()
    values = _record(state)[collection]
    assert values
    values.append(copy.deepcopy(values[0]))
    report = schema4.validate_schema4_state(state)
    assert any(item.code == "duplicate_object_id" for item in report.issues)


def test_broken_relationship_endpoint_is_rejected():
    state = _migrated()
    _record(state)["interval_relationships"][0]["left_event_id"] = "missing"
    assert any(item.code == "broken_relationship_endpoint" for item in schema4.validate_schema4_state(state).issues)


def test_broken_streak_relationship_is_rejected():
    state = _migrated()
    _record(state)["cadence_streaks"][0]["relationship_ids"][0] = "missing"
    assert any(item.code == "broken_streak_relationship_reference" for item in schema4.validate_schema4_state(state).issues)


def test_broken_revision_lineage_is_rejected():
    state = _migrated()
    record = _record(state)
    template = windows.import_legacy_expected_miss(
        server_key=SERVER, candidate_period_seconds=10800,
        expected_phase_offset=0, window_start_at=BASE, expected_at=BASE + 10,
        window_end_at=BASE + 20, model_revision_id="model", legacy_key="legacy",
    )
    value = {"record_type": "ExpectedWindowRevision", **schema4._primitive(replace(
        template, result_id="revision", supersedes_result_id="missing",
        outcome=windows.ExpectedWindowOutcome.RETRACTED_MISS,
        negative_penalty_active=False,
        reconciliation_reason=windows.ReconciliationReason.OBSERVED_OUTAGE,
        reconciled_at=BASE + 30, reconciliation_revision_id="reconcile",
    ))}
    record["expected_window_results"].append(value)
    assert any(item.code == "broken_revision_lineage" for item in schema4.validate_schema4_state(state).issues)


def test_broken_regime_evidence_reference_is_rejected():
    state = _migrated()
    _record(state)["regimes"][0]["defining_evidence_ids"] = ["missing"]
    assert any(item.code == "broken_regime_evidence_reference" for item in schema4.validate_schema4_state(state).issues)


def test_future_schema4_reader_fails_closed():
    state = _migrated()
    state["schema_version"] = 99
    with pytest.raises(schema4.Schema4VersionError):
        schema4.deserialize_schema4_state(state)


def test_schema3_code_refuses_to_write_schema4(tmp_path):
    active = tmp_path / "active.json"
    state = _migrated()
    active.write_bytes(schema4.serialize_schema4_state(state))
    before = active.read_bytes()
    with pytest.raises(storage.UnsupportedPhase2SchemaError):
        storage.atomic_write_phase2_state(active, storage.new_phase2_state())
    assert active.read_bytes() == before


def test_schema3_startup_preserves_schema4_and_disables_persistence(tmp_path):
    active = tmp_path / "active.json"
    legacy = tmp_path / "legacy.json"
    active.write_bytes(schema4.serialize_schema4_state(_migrated()))
    before = active.read_bytes()
    result = storage.initialize_phase2_state(active_path=active, legacy_path=legacy)
    assert result.failure is storage.Phase2MigrationFailure.ACTIVE_SCHEMA_INCOMPATIBLE
    assert not result.persistence_enabled
    assert active.read_bytes() == before


def test_schema4_does_not_silently_migrate_schema3():
    with pytest.raises(schema4.Schema4VersionError):
        schema4.deserialize_schema4_bytes(_source_bytes())


def test_dry_run_writes_only_prepared_backup_and_audit(tmp_path):
    source = tmp_path / "source.json"
    raw = _write_source(source)
    result = schema4.migrate_state_file(
        source_path=source, prepared_path=tmp_path / "prepared.json",
        audit_path=tmp_path / "audit.json", backup_path=tmp_path / "backup.json",
    )
    assert source.read_bytes() == raw
    assert result.prepared_path.exists() and result.audit_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["audit.json", "backup.json", "prepared.json", "source.json"]


def test_verified_backup_matches_source_size_and_hash(tmp_path):
    source = tmp_path / "source.json"
    raw = _write_source(source)
    result = schema4.migrate_state_file(
        source_path=source, prepared_path=tmp_path / "prepared.json",
        audit_path=tmp_path / "audit.json", backup_path=tmp_path / "backup.json",
    )
    assert result.backup_size == len(raw)
    assert result.backup_sha256 == hashlib.sha256(raw).hexdigest()


def test_two_dry_runs_are_byte_identical(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source)
    one = schema4.migrate_state_file(source_path=source, prepared_path=tmp_path / "one.json", audit_path=tmp_path / "one-audit.json", backup_path=tmp_path / "backup.json")
    two = schema4.migrate_state_file(source_path=source, prepared_path=tmp_path / "two.json", audit_path=tmp_path / "two-audit.json", backup_path=tmp_path / "backup.json")
    assert one.prepared_sha256 == two.prepared_sha256
    assert one.prepared_path.read_bytes() == two.prepared_path.read_bytes()


def test_schema4_migration_rerun_is_exact_noop(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source)
    first = schema4.migrate_state_file(source_path=source, prepared_path=tmp_path / "prepared.json", audit_path=tmp_path / "audit.json", backup_path=tmp_path / "backup.json")
    rerun = schema4.migrate_state_file(source_path=first.prepared_path, prepared_path=tmp_path / "prepared-again.json", audit_path=tmp_path / "audit-again.json", backup_path=None)
    assert rerun.idempotent_noop
    assert rerun.prepared_path.read_bytes() == first.prepared_path.read_bytes()


def test_source_filesystem_mtime_does_not_change_server_semantic_ids():
    state = _schema3_state_with_retraction()
    raw = schema4.canonical_json_bytes(state)
    first = schema4.migrate_schema3_state(
        copy.deepcopy(state), source_bytes=raw, source_mtime_ns=1
    )
    second = schema4.migrate_schema3_state(
        copy.deepcopy(state), source_bytes=raw, source_mtime_ns=9_999_999
    )
    assert _server_semantic_ids(first) == _server_semantic_ids(second)


def test_root_updated_at_does_not_change_reconciliation_or_authority_ids():
    first_source = _schema3_state_with_retraction()
    second_source = copy.deepcopy(first_source)
    second_source["updated_at"] += 31 * scoring.DAY
    first_raw = schema4.canonical_json_bytes(first_source)
    second_raw = schema4.canonical_json_bytes(second_source)
    first = schema4.migrate_schema3_state(
        first_source, source_bytes=first_raw, source_mtime_ns=1
    )
    second = schema4.migrate_schema3_state(
        second_source, source_bytes=second_raw, source_mtime_ns=2
    )
    assert _server_semantic_ids(first) == _server_semantic_ids(second)
    first_revision = next(
        item
        for item in _record(first)["expected_window_results"]
        if item["record_type"] == "ExpectedWindowRevision"
    )
    second_revision = next(
        item
        for item in _record(second)["expected_window_results"]
        if item["record_type"] == "ExpectedWindowRevision"
    )
    assert first_revision == second_revision


def test_unrelated_server_change_does_not_change_bob_semantic_ids():
    baseline_source = _schema3_state_with_retraction()
    changed_source = copy.deepcopy(baseline_source)
    unrelated = copy.deepcopy(changed_source["servers"][SERVER])
    for collection in (
        "events",
        "coverage_segments",
        "monitoring_sessions",
        "expected_window_misses",
        "routed_fingerprints",
        "incomplete_episodes",
    ):
        unrelated[collection] = []
    unrelated["event_seq"] = 0
    unrelated["runtime_bookkeeping_note"] = "unrelated-only"
    changed_source["servers"]["unrelated.example:2302"] = unrelated
    baseline_raw = schema4.canonical_json_bytes(baseline_source)
    changed_raw = schema4.canonical_json_bytes(changed_source)
    baseline = schema4.migrate_schema3_state(
        baseline_source, source_bytes=baseline_raw, source_mtime_ns=1
    )
    changed = schema4.migrate_schema3_state(
        changed_source, source_bytes=changed_raw, source_mtime_ns=2
    )
    assert _server_semantic_ids(baseline) == _server_semantic_ids(changed)


def test_unrelated_server_order_does_not_change_server_semantic_ids():
    source = _schema3_state_with_retraction()
    source["servers"]["empty.example:2302"] = {
        **copy.deepcopy(source["servers"][SERVER]),
        "events": [],
        "event_seq": 0,
        "coverage_segments": [],
        "monitoring_sessions": [],
        "expected_window_misses": [],
        "routed_fingerprints": [],
    }
    reversed_source = copy.deepcopy(source)
    reversed_source["servers"] = dict(reversed(list(source["servers"].items())))
    first_raw = json.dumps(source, separators=(",", ":")).encode()
    second_raw = json.dumps(reversed_source, separators=(",", ":")).encode()
    first = schema4.migrate_schema3_state(
        source, source_bytes=first_raw, source_mtime_ns=1
    )
    second = schema4.migrate_schema3_state(
        reversed_source, source_bytes=second_raw, source_mtime_ns=1
    )
    assert _server_semantic_ids(first) == _server_semantic_ids(second)


def test_nonsemantic_bob_collection_order_does_not_change_derived_ids():
    source = _schema3_state_with_retraction()
    reordered = copy.deepcopy(source)
    record = reordered["servers"][SERVER]
    for name in (
        "events",
        "coverage_segments",
        "monitoring_sessions",
        "expected_window_misses",
        "routed_fingerprints",
    ):
        record[name].reverse()
    first_raw = json.dumps(source, separators=(",", ":")).encode()
    second_raw = json.dumps(reordered, separators=(",", ":")).encode()
    first = schema4.migrate_schema3_state(
        source, source_bytes=first_raw, source_mtime_ns=1
    )
    second = schema4.migrate_schema3_state(
        reordered, source_bytes=second_raw, source_mtime_ns=1
    )
    assert _server_semantic_ids(first) == _server_semantic_ids(second)


@pytest.mark.parametrize(
    "mutation",
    [
        "legacy_placeholders",
        "neutral_open_session",
        "runtime_bookkeeping",
    ],
)
def test_non_evidential_schema3_additions_do_not_change_semantic_ids(mutation):
    source = _schema3_state_with_retraction()
    changed = copy.deepcopy(source)
    record = changed["servers"][SERVER]
    if mutation == "legacy_placeholders":
        record.update(
            {
                "legacy_app_session_id": None,
                "legacy_continuity_chain_ids": [],
                "legacy_provenance_version": 0,
            }
        )
    elif mutation == "neutral_open_session":
        record["monitoring_sessions"].append(
            {
                "session_id": "neutral-open-session",
                "app_session_id": "neutral-app",
                "poll_generation": 99,
                "continuity_chain_id": "neutral-chain",
                "continuity_chain_ids": [],
                "provenance_version": 1,
                "started_at": BASE + 20 * scoring.HOUR,
                "ended_at": None,
                "reason": None,
            }
        )
    else:
        record["last_poll_generation_seen"] = 99
        record["last_app_session_bookkeeping"] = "neutral"
    first_raw = schema4.canonical_json_bytes(source)
    second_raw = schema4.canonical_json_bytes(changed)
    first = schema4.migrate_schema3_state(
        source, source_bytes=first_raw, source_mtime_ns=1
    )
    second = schema4.migrate_schema3_state(
        changed, source_bytes=second_raw, source_mtime_ns=2
    )
    assert _server_semantic_ids(first) == _server_semantic_ids(second)


def test_reconciliation_evidence_identity_is_local_and_semantic():
    original = windows.import_legacy_expected_miss(
        server_key=SERVER,
        candidate_period_seconds=3 * scoring.HOUR,
        expected_phase_offset=100.0,
        window_start_at=BASE,
        expected_at=BASE + 100,
        window_end_at=BASE + 200,
        model_revision_id="model",
        legacy_key="lineage",
    )
    desired = replace(
        original,
        result_id="classification-a",
        coverage_classification=windows.WindowCoverageClassification.OUTAGE_ACTIVITY,
        healthy_throughout=False,
        outage_observed=True,
        overlapping_outage_episode_ids=("outage-a",),
        outcome=windows.ExpectedWindowOutcome.AMBIGUOUS,
        negative_penalty_active=False,
        reason_codes=("outage",),
    )
    same = schema4._migration_reconciliation_evidence_revision_id(
        original, desired, reconciled_at=BASE + 150
    )
    assert same == schema4._migration_reconciliation_evidence_revision_id(
        original,
        replace(desired, reason_codes=("different-diagnostic-order",)),
        reconciled_at=BASE + 150,
    )
    assert same != schema4._migration_reconciliation_evidence_revision_id(
        original,
        replace(desired, overlapping_outage_episode_ids=("outage-b",)),
        reconciled_at=BASE + 150,
    )
    hit_a = replace(
        desired,
        outcome=windows.ExpectedWindowOutcome.HIT,
        qualifying_finalized_event_id="event-a",
        overlapping_outage_episode_ids=(),
    )
    hit_b = replace(hit_a, qualifying_finalized_event_id="event-b")
    assert schema4._migration_reconciliation_evidence_revision_id(
        original, hit_a, reconciled_at=BASE + 150
    ) != schema4._migration_reconciliation_evidence_revision_id(
        original, hit_b, reconciled_at=BASE + 150
    )
    assert same != schema4._migration_reconciliation_evidence_revision_id(
        original, hit_a, reconciled_at=BASE + 150
    )
    other_server = replace(original, server_key="other.example:2302")
    assert same != schema4._migration_reconciliation_evidence_revision_id(
        other_server,
        replace(desired, server_key="other.example:2302"),
        reconciled_at=BASE + 150,
    )
    assert same != schema4._migration_reconciliation_evidence_revision_id(
        original,
        desired,
        reconciled_at=BASE + 151,
    )


def test_reconciliation_timestamp_is_derived_from_proving_evidence():
    state = _schema3_state_with_retraction()
    first_source = copy.deepcopy(state)
    second_source = copy.deepcopy(state)
    second_source["updated_at"] += 50 * scoring.DAY
    migrated = []
    for source in (first_source, second_source):
        raw = schema4.canonical_json_bytes(source)
        migrated.append(
            schema4.migrate_schema3_state(
                source, source_bytes=raw, source_mtime_ns=int(source["updated_at"])
            )
        )
    revisions = [
        next(
            item
            for item in _record(value)["expected_window_results"]
            if item["record_type"] == "ExpectedWindowRevision"
        )
        for value in migrated
    ]
    assert revisions[0]["result_id"] == revisions[1]["result_id"]
    assert revisions[0]["reconciled_at"] == revisions[1]["reconciled_at"]
    assert revisions[0] == revisions[1]


def test_changed_outage_set_changes_only_its_reconciliation_lineage():
    def lineage(key, expected):
        original = windows.import_legacy_expected_miss(
            server_key=SERVER,
            candidate_period_seconds=3 * scoring.HOUR,
            expected_phase_offset=expected % (3 * scoring.HOUR),
            window_start_at=expected - 100,
            expected_at=expected,
            window_end_at=expected + 100,
            model_revision_id=f"model-{key}",
            legacy_key=key,
        )
        desired = replace(
            original,
            result_id=f"classification-{key}",
            coverage_classification=windows.WindowCoverageClassification.OUTAGE_ACTIVITY,
            healthy_throughout=False,
            outage_observed=True,
            overlapping_outage_episode_ids=(f"outage-{key}",),
            outcome=windows.ExpectedWindowOutcome.AMBIGUOUS,
            negative_penalty_active=False,
            reason_codes=("outage",),
        )
        span = continuity.ContinuitySpan(
            provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
            reference_id=f"outage-{key}",
            server_key=SERVER,
            app_session_id="app",
            monitoring_session_id="monitor",
            poll_generation=1,
            continuity_chain_id="chain",
            start_at=expected - 10,
            end_at=expected + 10,
            start_monotonic=None,
            end_monotonic=None,
            kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
            cadence_seconds=3,
            reason_codes=("outage",),
        )
        return original, desired, span

    first = lineage("first", BASE + 1_000)
    second = lineage("second", BASE + 2_000)
    baseline = schema4._reconcile_migrated_expected_windows(
        original_windows=(first[0], second[0]),
        classifications=(first[1], second[1]),
        events=(),
        spans=(first[2], second[2]),
        episodes=(),
    )
    extra_span = replace(
        first[2],
        reference_id="outage-first-extra",
        start_at=first[2].start_at + 1,
    )
    changed = schema4._reconcile_migrated_expected_windows(
        original_windows=(first[0], second[0]),
        classifications=(
            replace(
                first[1],
                overlapping_outage_episode_ids=(
                    "outage-first",
                    "outage-first-extra",
                ),
            ),
            second[1],
        ),
        events=(),
        spans=(first[2], extra_span, second[2]),
        episodes=(),
    )

    def children(records):
        return {
            item.supersedes_result_id: item.result_id
            for item in records
            if isinstance(item, windows.ExpectedWindowRevision)
        }

    before = children(baseline)
    after = children(changed)
    assert before[first[0].result_id] != after[first[0].result_id]
    assert before[second[0].result_id] == after[second[0].result_id]


def test_prior_schema4_versions_remain_readable_but_are_not_apply_eligible(
    tmp_path,
):
    old = _migrated()
    old["version_manifest"]["migration_tool_version"] = 1
    old["migration_audit"]["migration_tool_version"] = 1
    old["migration_audit"]["semantic_version"] = 1
    old["scoring_algorithm_version"] = 2
    prepared = tmp_path / "old-readable-schema4.json"
    prepared.write_bytes(schema4.serialize_schema4_state(old))
    assert schema4.deserialize_schema4_bytes(
        prepared.read_bytes(), quarantine_invalid_servers=False
    ).report.valid
    active = tmp_path / "active.json"
    backup = tmp_path / "backup.json"
    raw = _write_source(active)
    backup.write_bytes(raw)
    with pytest.raises(schema4.Schema4VersionError, match="not eligible for apply"):
        schema4.apply_prepared_schema4(
            active_path=active,
            prepared_path=prepared,
            verified_backup_path=backup,
            expected_source_sha256=hashlib.sha256(raw).hexdigest(),
            approval_token=schema4.SCHEMA4_APPLY_APPROVAL_TOKEN,
        )
    assert active.read_bytes() == raw


def test_authoritative_schema4_rebuild_uses_inactivity_neutral_scoring():
    migrated = _migrated()
    existing = _record(migrated)
    schema3_record = _schema3_state()["servers"][SERVER]
    recent = schema4.rebuild_schema4_server_record(
        copy.deepcopy(existing),
        copy.deepcopy(schema3_record),
        server_key=SERVER,
        updated_at=BASE + 10 * scoring.HOUR,
    )
    stale = schema4.rebuild_schema4_server_record(
        copy.deepcopy(existing),
        copy.deepcopy(schema3_record),
        server_key=SERVER,
        updated_at=BASE + 40 * scoring.DAY,
    )

    def phase_confidence(record):
        return next(
            item["phase_confidence"]
            for item in record["normal_evidence_ledger"]["value"]["candidates"]
            if item["candidate_period_seconds"] == 3 * scoring.HOUR
        )

    assert phase_confidence(stale) == phase_confidence(recent)
    assert schema4.persisted_authority_decision(
        stale
    ).selected_shadow_regime.regime_id == schema4.persisted_authority_decision(
        recent
    ).selected_shadow_regime.regime_id


def test_unproven_history_remains_normal_only():
    state = _migrated(provenance=False)
    record = _record(state)
    assert record["interval_relationships"]
    assert not any(item["high_authority_eligible"] for item in record["interval_relationships"])
    assert record["cadence_streaks"] == []


def test_proven_continuity_becomes_durable_relationship_and_streak_evidence():
    record = _record(_migrated())
    assert sum(item["high_authority_eligible"] for item in record["interval_relationships"]) >= 3
    assert len(record["cadence_streaks"]) == 1
    assert schema4.persisted_interval_relationships(record)
    assert schema4.persisted_cadence_streaks(record)


def test_persisted_authority_reload_preserves_decision_and_state():
    record = _record(_migrated())
    decision = schema4.persisted_authority_decision(record)
    assert decision.decision_id == record["authority_decision"]["decision_id"]
    assert decision.state is authority.RegimeState.ESTABLISHED
    assert decision.prediction_usable


def test_compaction_preserves_authority_decision_and_defining_ids():
    state = _migrated()
    before = schema4.persisted_authority_decision(_record(state))
    compacted = schema4.compact_schema4_state(state)
    after = schema4.persisted_authority_decision(_record(compacted))
    assert after.decision_id == before.decision_id
    assert after.high_confidence == before.high_confidence
    assert schema4.validate_schema4_state(compacted).valid


def test_compaction_does_not_irreversibly_fold_window_lineages():
    state = _migrated()
    compacted = schema4.compact_schema4_state(state)
    assert _record(compacted)["expected_window_results"] == _record(state)["expected_window_results"]


def test_provisional_transition_state_survives_reload():
    state = _migrated()
    record = _record(state)
    current = schema4.persisted_authority_decision(record)
    provisional = replace(
        current, decision_id="provisional-decision", state=authority.RegimeState.NEW_REGIME_PROVISIONAL,
        prediction_usable=False, countdown_safe=False,
        reason_codes=("persisted_provisional_transition",),
    )
    record["authority_decisions"].append(schema4._decision_payload(provisional))
    record["authority_decision"] = schema4._decision_payload(provisional)
    loaded = schema4.deserialize_schema4_bytes(schema4.serialize_schema4_state(state), quarantine_invalid_servers=False)
    replayed = schema4.persisted_authority_decision(_record(loaded.state))
    assert replayed.state is authority.RegimeState.NEW_REGIME_PROVISIONAL
    assert not replayed.countdown_safe


def test_superseded_regime_remains_historical_and_inactive():
    state = _migrated()
    record = _record(state)
    current = schema4.persisted_authority_decision(record)
    old = current.selected_shadow_regime
    assert old is not None
    newer = replace(old, regime_id="newer-regime", phase_offset=(old.phase_offset + 120) % old.candidate_period_seconds)
    superseded = replace(old, status=authority.RegimeRecordStatus.SUPERSEDED, superseded_by_id=newer.regime_id, superseded_at=old.established_at)
    decision = replace(
        current, decision_id="superseded-history-decision", selected_shadow_regime=newer,
        incumbent_shadow_regime=newer, regimes=(superseded, newer),
    )
    record["regimes"] = [schema4._primitive(superseded), schema4._primitive(newer)]
    record["authority_decisions"].append(schema4._decision_payload(decision))
    record["authority_decision"] = schema4._decision_payload(decision)
    replayed = schema4.persisted_authority_decision(
        _record(schema4.deserialize_schema4_bytes(schema4.serialize_schema4_state(state), quarantine_invalid_servers=False).state)
    )
    assert replayed.selected_shadow_regime.regime_id == "newer-regime"
    assert any(item.status is authority.RegimeRecordStatus.SUPERSEDED for item in replayed.regimes)


def test_phase_distinct_regimes_do_not_merge():
    state = _migrated()
    record = _record(state)
    original = schema4._regime_from(record["regimes"][0])
    shifted = replace(original, regime_id="shifted-phase-regime", phase_offset=(original.phase_offset + 300) % original.candidate_period_seconds, status=authority.RegimeRecordStatus.HISTORICAL)
    record["regimes"].append(schema4._primitive(shifted))
    assert len({item["regime_id"] for item in record["regimes"]}) == 2
    assert len({item["phase_offset"] for item in record["regimes"]}) == 2


def test_rollback_restores_byte_identical_schema3_source(tmp_path):
    active = tmp_path / "active.json"
    raw = _write_source(active)
    result = schema4.migrate_state_file(source_path=active, prepared_path=tmp_path / "prepared.json", audit_path=tmp_path / "audit.json", backup_path=tmp_path / "backup.json")
    schema4.apply_prepared_schema4(
        active_path=active, prepared_path=result.prepared_path,
        verified_backup_path=result.backup_path,
        expected_source_sha256=result.source_sha256,
        approval_token=schema4.SCHEMA4_APPLY_APPROVAL_TOKEN,
    )
    schema4.rollback_whole_file(active_path=active, verified_backup_path=result.backup_path, expected_backup_sha256=result.source_sha256)
    assert active.read_bytes() == raw


def test_apply_without_exact_approval_token_fails_closed(tmp_path):
    source = tmp_path / "active.json"
    _write_source(source)
    result = schema4.migrate_state_file(source_path=source, prepared_path=tmp_path / "prepared.json", audit_path=tmp_path / "audit.json", backup_path=tmp_path / "backup.json")
    before = source.read_bytes()
    with pytest.raises(schema4.Schema4Error):
        schema4.apply_prepared_schema4(active_path=source, prepared_path=result.prepared_path, verified_backup_path=result.backup_path, expected_source_sha256=result.source_sha256, approval_token="no")
    assert source.read_bytes() == before


def test_one_malformed_server_is_quarantined_without_losing_valid_server():
    state = _migrated()
    state["servers"]["broken:2302"] = {"server_key": "broken:2302", "authority_status": "valid_shadow_only"}
    loaded = schema4.deserialize_schema4_state(state, quarantine_invalid_servers=True)
    assert SERVER in loaded.report.valid_server_keys
    assert "broken:2302" in loaded.report.quarantined_server_keys
    assert loaded.state["servers"][SERVER]["authority_status"] == "valid_shadow_only"
    assert loaded.state["servers"]["broken:2302"]["authority_status"] == "quarantined"


def test_migration_audit_identity_and_counts_are_stable():
    first = _migrated()
    second = _migrated()
    assert first["migration_audit"] == second["migration_audit"]
    audit = first["migration_audit"]["global_totals"]
    assert audit["source_event_count"] == 4
    assert audit["high_eligible_relationships"] >= 3


def test_current_production_scorer_value_is_unchanged_by_migration():
    raw = _schema3_state()["servers"][SERVER]
    server = runtime._deserialize_server(SERVER, raw, detection.DetectionConfig())
    before = scoring.RestartScheduleScorer().score(server.events, scoring.CoverageTimeline(server.coverage), now=BASE + 10 * scoring.HOUR)
    _migrated()
    after = scoring.RestartScheduleScorer().score(server.events, scoring.CoverageTimeline(server.coverage), now=BASE + 10 * scoring.HOUR)
    assert after == before


def test_current_consumer_output_is_unchanged_by_migration():
    raw = _schema3_state()["servers"][SERVER]
    server = runtime._deserialize_server(SERVER, raw, detection.DetectionConfig())
    score = scoring.RestartScheduleScorer().score(server.events, scoring.CoverageTimeline(server.coverage), now=BASE + 10 * scoring.HOUR)
    before = runtime.phase2_learning_summary(None, now=BASE + 10 * scoring.HOUR)
    _migrated()
    after = runtime.phase2_learning_summary(None, now=BASE + 10 * scoring.HOUR)
    assert after == before


def test_schema4_shadow_comparison_is_pure_and_read_only():
    state = _migrated()
    record = _record(state)
    decision = schema4.persisted_authority_decision(record)
    before = schema4.canonical_json_bytes(state)
    comparison = schema4.compare_persisted_authority(
        record=record, in_memory_decision=decision,
        in_memory_relationship_count=len(record["interval_relationships"]),
        in_memory_streak_count=len(record["cadence_streaks"]),
        in_memory_expected_window_count=len(record["expected_window_results"]),
    )
    assert comparison.decision_id_equal and comparison.object_counts_equal
    assert schema4.canonical_json_bytes(state) == before


def test_schema4_runtime_shadow_does_not_change_production_decision(tmp_path):
    schema3 = _source_bytes()
    schema4_state = _migrated()
    disabled_active = tmp_path / "disabled.json"
    enabled_active = tmp_path / "enabled.json"
    disabled_active.write_bytes(schema3)
    enabled_active.write_bytes(schema3)
    disabled = runtime.Phase2RestartRuntime.initialize(
        active_path=disabled_active, legacy_path=tmp_path / "disabled-legacy.json",
        now=BASE + 10 * scoring.HOUR, app_session_id="schema4-disabled",
    )
    enabled = runtime.Phase2RestartRuntime.initialize(
        active_path=enabled_active, legacy_path=tmp_path / "enabled-legacy.json",
        now=BASE + 10 * scoring.HOUR, app_session_id="schema4-enabled",
        continuity_authority_shadow_enabled=True,
        schema4_authority_shadow_enabled=True,
        schema4_authority_shadow_state=schema4_state,
    )
    disabled._server(SERVER).regime_generation = "shared-regime"
    enabled._server(SERVER).regime_generation = "shared-regime"
    disabled_decision = disabled.decision(SERVER, now=BASE + 10 * scoring.HOUR)
    enabled.decision(SERVER, now=BASE + 10 * scoring.HOUR)
    enabled_decision = enabled.decision(SERVER, now=BASE + 10 * scoring.HOUR)
    assert enabled._server(SERVER).score == disabled._server(SERVER).score
    assert enabled_decision == disabled_decision
    assert enabled.schema4_authority_shadow_snapshot(SERVER) is not None
    assert json.loads(enabled_active.read_text())["schema_version"] == 3


def test_no_schema4_collection_is_written_to_schema3_runtime_state(tmp_path):
    active = tmp_path / "active.json"
    legacy = tmp_path / "legacy.json"
    value = runtime.Phase2RestartRuntime.initialize(active_path=active, legacy_path=legacy, now=BASE)
    assert active.exists()
    persisted = json.loads(active.read_text())
    assert persisted["schema_version"] == 3
    assert "authority_decision" not in persisted
    assert "interval_relationships" not in persisted


def test_bob_detached_migration_preview_when_active_fixture_exists(tmp_path):
    baseline = _schema3_state_with_retractions(5, established=True)
    baseline_raw = schema4.canonical_json_bytes(baseline)
    baseline_migrated = schema4.migrate_schema3_state(
        json.loads(baseline_raw),
        source_bytes=baseline_raw,
        source_mtime_ns=1_900_000_000_000_000_000,
    )
    baseline_ledger = schema4.persisted_expected_window_ledger(
        baseline_migrated["servers"][SERVER]
    )
    required = {
        item.reconciliation_revision_id: (
            item.supersedes_result_id,
            item.reconciliation_reason,
            item.outcome,
            item.candidate_period_seconds,
        )
        for item in baseline_ledger
        if isinstance(item, windows.ExpectedWindowRevision)
    }
    assert len(required) == 5

    active = tmp_path / "deterministic-active-fixture.json"
    active_fixture = _schema3_state_with_retractions(11, established=True)
    active_fixture["servers"][SERVER]["expected_window_misses"].append({
        "period_seconds": 4 * scoring.HOUR,
        "expected_at": BASE + 4 * scoring.HOUR,
        "key": "unrelated-four-hour-legacy-claim",
        "weight": 1.0,
    })
    active.write_bytes(schema4.canonical_json_bytes(active_fixture))
    before_stat = active.stat()
    before = active.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    first = schema4.migrate_state_file(
        source_path=active,
        prepared_path=tmp_path / "prepared-a.json",
        audit_path=tmp_path / "audit-a.json",
        backup_path=tmp_path / "backup-a.json",
        server_key=SERVER,
    )
    second = schema4.migrate_state_file(
        source_path=active,
        prepared_path=tmp_path / "prepared-b.json",
        audit_path=tmp_path / "audit-b.json",
        backup_path=tmp_path / "backup-b.json",
        server_key=SERVER,
    )
    assert (tmp_path / "prepared-a.json").read_bytes() == (
        tmp_path / "prepared-b.json"
    ).read_bytes()
    assert first.state == second.state

    record = first.state["servers"][SERVER]
    ledger = schema4.persisted_expected_window_ledger(record)
    revisions = [item for item in ledger if isinstance(item, windows.ExpectedWindowRevision)]
    candidate = max(
        (item for item in record["authority_candidates"] if item["candidate_period_seconds"] == 10800),
        key=lambda item: item["high_authority"],
    )
    decision = schema4.persisted_authority_decision(record)
    assert len(revisions) == 11
    _assert_required_retractions(record, required)
    assert {item.reconciliation_reason for item in revisions} == {windows.ReconciliationReason.OBSERVED_OUTAGE}
    active_misses = windows.derive_active_miss_ledger(ledger).active_results
    selected_misses = [
        item for item in active_misses if item.candidate_period_seconds == 10800
    ]
    assert any(item.candidate_period_seconds == 14400 for item in active_misses)
    assert selected_misses == []
    assert sum(item.negative_penalty_active for item in selected_misses) == 0.0
    validation = schema4.validate_schema4_state(first.state)
    assert validation.valid
    assert not any(issue.code == "duplicate_object_id" for issue in validation.issues)
    assert candidate["high_authority"] == 0.97
    assert candidate["high_phase_authority"] >= 0.96
    assert candidate["h3_gate"] and candidate["h4_gate"]
    assert decision.state is authority.RegimeState.ESTABLISHED
    assert decision.cycle_visible and decision.prediction_usable
    compacted = schema4.compact_schema4_state(first.state)
    compacted_record = compacted["servers"][SERVER]
    compacted_decision = schema4.persisted_authority_decision(compacted_record)
    compacted_ledger = schema4.persisted_expected_window_ledger(compacted_record)
    compacted_candidate = max(
        (item for item in compacted_record["authority_candidates"] if item["candidate_period_seconds"] == 10800),
        key=lambda item: item["high_authority"],
    )
    assert compacted_decision.decision_id == decision.decision_id
    assert compacted_candidate["h3_gate"] and compacted_candidate["h4_gate"]
    assert not any(
        item.candidate_period_seconds == 10800
        for item in windows.derive_active_miss_ledger(
            compacted_ledger
        ).active_results
    )
    compacted_revision_ids = {
        item.reconciliation_revision_id
        for item in compacted_ledger
        if isinstance(item, windows.ExpectedWindowRevision)
    }
    assert required.keys() <= compacted_revision_ids
    after_stat = active.stat()
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (before_stat.st_size, before_stat.st_mtime_ns)
    assert hashlib.sha256(active.read_bytes()).hexdigest() == before_hash


def test_detached_preview_required_retraction_guard_rejects_missing_identity():
    source = _schema3_state_with_retractions(6, established=True)
    raw = schema4.canonical_json_bytes(source)
    migrated = schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1
    )
    record = migrated["servers"][SERVER]
    ledger = schema4.persisted_expected_window_ledger(record)
    required = {
        item.reconciliation_revision_id: (
            item.supersedes_result_id,
            item.reconciliation_reason,
            item.outcome,
            item.candidate_period_seconds,
        )
        for item in ledger
        if isinstance(item, windows.ExpectedWindowRevision)
    }
    missing_id = sorted(required)[0]
    record["expected_window_results"] = [
        item
        for item in record["expected_window_results"]
        if item.get("reconciliation_revision_id") != missing_id
    ]
    with pytest.raises(AssertionError):
        _assert_required_retractions(record, required)


def test_detached_preview_duplicate_immutable_id_fails_validation():
    source = _schema3_state_with_retractions(6, established=True)
    raw = schema4.canonical_json_bytes(source)
    migrated = schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1
    )
    record = migrated["servers"][SERVER]
    record["expected_window_results"].append(
        copy.deepcopy(record["expected_window_results"][0])
    )
    validation = schema4.validate_schema4_state(migrated)
    assert not validation.valid
    assert any(issue.code == "duplicate_object_id" for issue in validation.issues)


def test_historical_and_current_bob_sources_have_identical_corrected_semantics():
    state_dir = Path.home() / ".config/dzll"
    old_path = state_dir / (
        "companion_restart_learning_phase2.schema3.precutover."
        "20260723-082421-BST.json"
    )
    current_path = state_dir / (
        "companion_restart_learning_phase2.schema3.precutover."
        "20260723-091223-BST.json"
    )
    if not old_path.exists() or not current_path.exists():
        pytest.skip("detached Bob comparison sources are unavailable")

    migrated = []
    for path in (old_path, current_path):
        raw = path.read_bytes()
        migrated.append(
            schema4.migrate_schema3_state(
                json.loads(raw),
                source_bytes=raw,
                source_mtime_ns=path.stat().st_mtime_ns,
            )
        )
    old_record, current_record = (
        value["servers"][BOB] for value in migrated
    )
    old_ids = _server_semantic_ids(migrated[0], server_key=BOB)
    current_ids = _server_semantic_ids(migrated[1], server_key=BOB)
    assert old_ids == current_ids
    assert old_ids == {
        "revisions": (
            "05bf5006a418fd0ecc2ca67c6c0b0e6bf17bf65093a6429c319852ed3c246e32",
            "51cb378411d69449ead8cb881ea881d374b332fba9ba1fc9705166f015f8bf5f",
            "79a1efccad72c7cb990ecf3c39ea23cf0480712e51294e60904b6029cb31f4e9",
            "c7e8bf673e1c069923d3ca5704a2d2de3fc5fc9bc1c5bcc35d055d42bc197bf6",
            "f87c8fe1a6fcb4c596a21c8204d0e11ffbcd59b50ff92f133971bbc25527a5d9",
        ),
        "authority": (
            "37912e1f9461d2efe5202c5c7010cdccfbe617de2f7cfd7c1581c7fdc819db1d"
        ),
        "consumer": (
            "8310638c9f0dbc130f470a7c2686eb8cdb28c57690d11d0c6e355b425a28bae0"
        ),
        "regime": (
            "02aba494f6518c38e34202135ad9c7aad442dc824c1f0560f19ddfdbee9ee128"
        ),
    }
    assert [item["event_id"] for item in old_record["physical_events"]] == [
        item["event_id"] for item in current_record["physical_events"]
    ]
    assert [
        item["relationship_id"]
        for item in old_record["interval_relationships"]
        if item["candidate_period_seconds"] == 10800
        and item["high_authority_eligible"]
    ] == [
        item["relationship_id"]
        for item in current_record["interval_relationships"]
        if item["candidate_period_seconds"] == 10800
        and item["high_authority_eligible"]
    ]
    assert len(
        [
            item
            for item in current_record["interval_relationships"]
            if item["candidate_period_seconds"] == 10800
            and item["high_authority_eligible"]
        ]
    ) == 4
    assert [item["streak_id"] for item in old_record["cadence_streaks"]] == [
        item["streak_id"] for item in current_record["cadence_streaks"]
    ]
    original_ids = lambda record: sorted(
        item["result_id"]
        for item in record["expected_window_results"]
        if item["record_type"] == "ExpectedWindowResult"
    )
    assert original_ids(old_record) == original_ids(current_record)
    assert len(original_ids(current_record)) == 5

    decision = schema4.persisted_authority_decision(current_record)
    selected = decision.selected_shadow_regime
    assert selected is not None
    candidate = next(
        item
        for item in current_record["authority_candidates"]
        if item["candidate_period_seconds"] == selected.candidate_period_seconds
        and item["phase_offset"] == selected.phase_offset
    )
    normal = next(
        item
        for item in current_record["normal_evidence_ledger"]["value"]["candidates"]
        if item["candidate_period_seconds"] == 10800
    )
    ledger = schema4.persisted_expected_window_ledger(current_record)
    consumer = consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=decision,
            now=1_784_800_000.0,
        )
    )
    summary = consumers4.authority_consumer_summary(
        consumer, now=1_784_800_000.0
    )
    assert normal["phase_confidence"] == pytest.approx(0.771875)
    assert selected.candidate_period_seconds == 10800
    assert selected.phase_offset == pytest.approx(87.169854)
    assert candidate["base_authority"] == pytest.approx(0.937291)
    assert candidate["streak_bonus"] == pytest.approx(0.039985)
    assert candidate["high_authority"] == 0.97
    assert candidate["high_phase_authority"] == pytest.approx(0.968211)
    assert candidate["h3_gate"] and candidate["h4_gate"]
    assert decision.state is authority.RegimeState.ESTABLISHED
    assert decision.prediction_usable and decision.countdown_safe
    assert windows.derive_active_miss_ledger(ledger).active_results == ()
    assert windows.derive_active_miss_ledger(ledger).total_active_penalty == 0.0
    assert consumer.presentation_state is consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE
    assert consumer.confidence_display_value == 0.97
    assert consumer.countdown_visible and consumer.countdown_safe
    assert summary["cycle_text"] == "Confirmed: Every 3 hours"
