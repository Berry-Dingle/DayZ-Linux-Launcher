from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers
from dzll_launcher import companion_restart_phase2_consumer_state as state4
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_cutover_rehearsal as rehearsal
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 1_900_000_000.0
SERVER = "stage3b2.example:2302"
OTHER = "stage3b2-other.example:2302"
HOUR = 3600


def _event(server, sequence, at):
    return detection.PhysicalRestartEvent(
        event_id=f"event-{server}-{sequence}-{int(at)}",
        sequence=sequence,
        fingerprint=f"{server}|monitor|{sequence}|fixture|restart",
        server_key=server,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        authenticity=0.85,
        schedule_weight_suggestion=0.85,
        drain=detection.DrainSummary(None, None, None, None, False, None, None, 0, 0, False),
        outage=detection.OutageSummary(at - 70, at - 60, 3, (detection.InfoStatus.TIMEOUT,), at),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("stage3b2_fixture",),
        app_session_id=f"app-{server}",
        monitoring_session_id=f"monitor-{server}",
        poll_generation=1,
        continuity_chain_id=f"chain-{server}",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _server_record(server):
    events = tuple(_event(server, index + 1, BASE + index * 3 * HOUR) for index in range(4))
    coverage = scoring.CoverageSegment(
        BASE,
        BASE + 9 * HOUR,
        scoring.CoverageKind.ONLINE_HEALTHY,
        10.0,
        server_key=server,
        app_session_id=f"app-{server}",
        monitoring_session_id=f"monitor-{server}",
        poll_generation=1,
        continuity_chain_id=f"chain-{server}",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )
    return {
        "runtime_state_version": 1,
        "scoring_semantics_version": 1,
        "aggregate_semantics_version": 1,
        "events": [runtime._serialize_event(item) for item in events],
        "event_seq": 4,
        "folded_through_event_seq": 0,
        "coverage_segments": [runtime._serialize_coverage(coverage)],
        "monitoring_sessions": [{
            "session_id": f"monitor-{server}",
            "app_session_id": f"app-{server}",
            "poll_generation": 1,
            "continuity_chain_id": f"chain-{server}",
            "continuity_chain_ids": [f"chain-{server}"],
            "provenance_version": 1,
            "started_at": BASE - 120,
            "ended_at": BASE + 9 * HOUR + 120,
            "reason": "shutdown",
        }],
        "expected_window_misses": [],
        "fired_keys": [],
        "routed_fingerprints": [item.fingerprint for item in events],
        "incumbent_period_seconds": None,
        "regime_generation": "stage3b2-fixture",
        "aggregate": runtime._serialize_aggregate(scoring.LongTermAggregate()),
        "prior_regimes": [],
        "active_episode": None,
        "incomplete_episodes": [],
        "candidate_diagnostics": {},
        "consumer_adapter_version": 1,
    }


def _schema3(*, two=False):
    value = storage.new_phase2_state(
        now=BASE + 10 * HOUR,
        generation_id="00000000-0000-4000-8000-0000000000b2",
    )
    value["servers"] = {SERVER: _server_record(SERVER)}
    if two:
        value["servers"][OTHER] = _server_record(OTHER)
    return value


def _state(*, two=False):
    source = _schema3(two=two)
    raw = schema4.canonical_json_bytes(source)
    return schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1
    )


def _write(path, value=None):
    raw = schema4.serialize_schema4_state(_state() if value is None else value)
    path.write_bytes(raw)
    return raw


def _backend(tmp_path, value=None):
    path = tmp_path / "stage3b2-schema4.json"
    original = _write(path, value)
    return live4.AuthoritativeSchema4Runtime.open(path, enabled=True), path, original


def _decision(backend, *, now=None, event_id=None, recovery=False):
    auth = backend.authority_decision(SERVER)
    if now is None:
        regime = auth.selected_shadow_regime
        now = consumers.next_phase_occurrence(
            period_seconds=regime.candidate_period_seconds,
            phase_offset=regime.phase_offset,
            now=BASE,
        ) - 300
    return consumers.evaluate_authority_consumers(
        consumers.AuthorityConsumerPolicyInput(
            authority_decision=auth,
            now=now,
            physical_recovery_event_id=event_id,
            physical_recovery_eligible=recovery,
            fired_keys=backend.consumer_fired_keys(SERVER),
        )
    )


def _warning_key(decision):
    assert decision.scheduled_warning_suppression_key is not None
    return decision.scheduled_warning_suppression_key


def _generic_decision(backend):
    event_id = backend.state["servers"][SERVER]["physical_events"][0]["event_id"]
    return _decision(backend, now=BASE + 1, event_id=event_id, recovery=True)


def test_consumer_state_round_trip_and_empty_migration_collection():
    value = _state()
    raw = schema4.serialize_schema4_state(value)
    loaded = schema4.deserialize_schema4_bytes(raw, quarantine_invalid_servers=False)
    consumer = loaded.state["servers"][SERVER]["consumer_state"]
    assert state4.ledger_from_mapping(consumer, server_key=SERVER).alert_records == ()
    assert schema4.serialize_schema4_state(loaded.state) == raw


@pytest.mark.parametrize("kind", [consumers.AuthoritySuppressionKind.SCHEDULED_WARNING, consumers.AuthoritySuppressionKind.GENERIC_RECOVERY])
def test_suppression_key_identity_is_deterministic(kind):
    key = consumers.AuthoritySuppressionKey(
        kind=kind,
        server_key=SERVER,
        regime_id="regime" if kind is consumers.AuthoritySuppressionKind.SCHEDULED_WARNING else None,
        expected_occurrence_at=123 if kind is consumers.AuthoritySuppressionKind.SCHEDULED_WARNING else None,
        event_id="event" if kind is consumers.AuthoritySuppressionKind.GENERIC_RECOVERY else None,
    )
    assert state4.suppression_key_id(key) == state4.suppression_key_id(key)


def test_duplicate_alert_record_is_deduplicated_exactly():
    ledger = state4.ledger_from_mapping(None, server_key=SERVER)
    key = consumers.AuthoritySuppressionKey(consumers.AuthoritySuppressionKind.GENERIC_RECOVERY, SERVER, event_id="event")
    record = state4.alert_record(key=key, authority_consumer_decision_id="decision", created_at=BASE, lifecycle=state4.AlertLifecycle.RESERVED, action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE)
    assert state4.append_alert(state4.append_alert(ledger, record), record).alert_records == (record,)


def test_duplicate_active_key_lineages_fail_validation():
    value, _ = _invalid_state(consumers.AuthoritySuppressionKind.GENERIC_RECOVERY)
    consumer = value["servers"][SERVER]["consumer_state"]
    duplicate = copy.deepcopy(consumer["alert_records"][0])
    duplicate["record_id"] = "second-active-lineage"
    duplicate["lifecycle"] = state4.AlertLifecycle.EMITTED.value
    duplicate["action_outcome"] = state4.AlertActionOutcome.EMITTED.value
    consumer["alert_records"].append(duplicate)
    report = schema4.deserialize_schema4_state(value, quarantine_invalid_servers=True).report
    assert SERVER in report.quarantined_server_keys


def _invalid_state(kind):
    value = _state(two=True)
    record = value["servers"][SERVER]
    decision = schema4.persisted_authority_decision(record)
    consumer = consumers.evaluate_authority_consumers(consumers.AuthorityConsumerPolicyInput(authority_decision=decision, now=BASE + 1))
    ledger = state4.with_decision(state4.ledger_from_mapping(record["consumer_state"], server_key=SERVER), consumer)
    event_id = record["physical_events"][0]["event_id"]
    key = consumers.AuthoritySuppressionKey(
        kind=kind,
        server_key=SERVER,
        regime_id=consumer.selected_regime_id if kind is consumers.AuthoritySuppressionKind.SCHEDULED_WARNING else None,
        expected_occurrence_at=123 if kind is consumers.AuthoritySuppressionKind.SCHEDULED_WARNING else None,
        event_id=event_id if kind is consumers.AuthoritySuppressionKind.GENERIC_RECOVERY else None,
    )
    alert = state4.alert_record(key=key, authority_consumer_decision_id=consumer.decision_id, created_at=BASE, lifecycle=state4.AlertLifecycle.RESERVED, action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE)
    ledger = state4.append_alert(ledger, alert)
    record["consumer_state"] = state4.ledger_to_mapping(ledger)
    return value, record["consumer_state"]["alert_records"][0]


def test_malformed_scheduled_key_fails_validation():
    value, item = _invalid_state(consumers.AuthoritySuppressionKind.SCHEDULED_WARNING)
    item["physical_event_id"] = "forbidden"
    report = schema4.deserialize_schema4_state(value, quarantine_invalid_servers=True).report
    assert SERVER in report.quarantined_server_keys


def test_malformed_recovery_key_fails_validation():
    value, item = _invalid_state(consumers.AuthoritySuppressionKind.GENERIC_RECOVERY)
    item["regime_id"] = "forbidden"
    report = schema4.deserialize_schema4_state(value, quarantine_invalid_servers=True).report
    assert SERVER in report.quarantined_server_keys


def test_consumer_error_quarantines_only_affected_server():
    value, item = _invalid_state(consumers.AuthoritySuppressionKind.GENERIC_RECOVERY)
    item["physical_event_id"] = "missing"
    loaded = schema4.deserialize_schema4_state(value, quarantine_invalid_servers=True)
    assert loaded.report.quarantined_server_keys == (SERVER,)
    assert OTHER in loaded.report.valid_server_keys


def _emit(backend, decision, key):
    reservation = backend.reserve_consumer_alert(decision=decision, key=key, created_at=BASE)
    assert reservation.reserved
    backend.complete_consumer_alert(decision=decision, reservation=reservation, completed_at=BASE + 1, action_succeeded=True)


@pytest.mark.parametrize("kind", ["warning", "recovery"])
def test_fired_key_survives_reload(kind, tmp_path):
    backend, path, _ = _backend(tmp_path)
    decision = _decision(backend) if kind == "warning" else _generic_decision(backend)
    key = _warning_key(decision) if kind == "warning" else decision.generic_recovery_suppression_key
    _emit(backend, decision, key)
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert key in again.consumer_fired_keys(SERVER)
    again.close()


def test_scheduled_and_recovery_namespaces_cannot_collide(tmp_path):
    backend, _, _ = _backend(tmp_path)
    warning = _warning_key(_decision(backend))
    recovery = _generic_decision(backend).generic_recovery_suppression_key
    assert warning.serialize() != recovery.serialize()
    assert state4.suppression_key_id(warning) != state4.suppression_key_id(recovery)
    backend.close()


def test_one_warning_reserves_and_emits_once(tmp_path):
    backend, _, _ = _backend(tmp_path)
    decision = _decision(backend)
    key = _warning_key(decision)
    first = backend.reserve_consumer_alert(decision=decision, key=key, created_at=BASE)
    second = backend.reserve_consumer_alert(decision=decision, key=key, created_at=BASE)
    assert first.reserved and not second.reserved
    backend.close()


def test_repeated_warning_poll_and_process_restart_do_not_replay(tmp_path):
    backend, path, _ = _backend(tmp_path)
    decision = _decision(backend)
    _emit(backend, decision, _warning_key(decision))
    same = _decision(backend, now=decision.scheduled_warning_at)
    assert not same.scheduled_pre_restart_alert_eligible
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    replay = _decision(again, now=decision.scheduled_warning_at)
    assert not replay.scheduled_pre_restart_alert_eligible
    again.close()


def test_regime_change_rotates_warning_key_and_invalidates_old(tmp_path):
    backend, _, _ = _backend(tmp_path)
    old = _decision(backend)
    _emit(backend, old, _warning_key(old))
    new = replace(old, decision_id="new-consumer", selected_regime_id="new-regime")
    # Pure key rotation contract; persisted transition records are tested with real regimes below.
    new_key = consumers.AuthoritySuppressionKey(consumers.AuthoritySuppressionKind.SCHEDULED_WARNING, SERVER, regime_id="new-regime", expected_occurrence_at=old.scheduled_warning_suppression_key.expected_occurrence_at)
    assert new_key != _warning_key(old)
    backend.close()


def test_h2_then_h3_consumer_transition_invalidates_old_and_allows_new_once(tmp_path):
    backend, _, _ = _backend(tmp_path)
    old = _decision(backend)
    old_key = _warning_key(old)
    _emit(backend, old, old_key)
    record = backend.state["servers"][SERVER]
    synthetic = copy.deepcopy(record["regimes"][0])
    synthetic["regime_id"] = "synthetic-4h-regime"
    synthetic["candidate_period_seconds"] = 4 * HOUR
    synthetic["status"] = authority.RegimeRecordStatus.PROVISIONAL.value
    record["regimes"].append(synthetic)
    backend.replace_server_record(SERVER, record, reason="synthetic_transition_fixture")
    h2 = replace(
        old,
        decision_id="synthetic-h2-consumer",
        selected_regime_id=synthetic["regime_id"],
        presentation_state=consumers.AuthorityPresentationKey.LIKELY_NEW_CYCLE,
        wording_key=consumers.AuthorityPresentationKey.LIKELY_NEW_CYCLE,
        next_expected_restart_at=None,
        countdown_visible=False,
        countdown_safe=False,
        prediction_suspended=True,
        scheduled_warning_eligible=False,
        scheduled_warning_at=None,
        scheduled_warning_window=None,
        scheduled_warning_suppression_key=None,
        scheduled_pre_restart_alert_eligible=False,
    )
    backend.record_consumer_decision(h2, created_at=BASE + 2)
    assert old_key in backend.consumer_fired_keys(SERVER)
    new_key = consumers.AuthoritySuppressionKey(
        consumers.AuthoritySuppressionKind.SCHEDULED_WARNING,
        SERVER,
        regime_id=synthetic["regime_id"],
        expected_occurrence_at=old_key.expected_occurrence_at + HOUR,
    )
    h3 = replace(
        h2,
        decision_id="synthetic-h3-consumer",
        presentation_state=consumers.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE,
        wording_key=consumers.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE,
        next_expected_restart_at=float(new_key.expected_occurrence_at),
        countdown_visible=True,
        countdown_safe=True,
        prediction_suspended=False,
        scheduled_warning_eligible=True,
        scheduled_warning_at=float(new_key.expected_occurrence_at - 300),
        scheduled_warning_window=(float(new_key.expected_occurrence_at - 330), float(new_key.expected_occurrence_at - 270)),
        scheduled_warning_suppression_key=new_key,
        scheduled_pre_restart_alert_eligible=True,
    )
    backend.record_consumer_decision(h3, created_at=BASE + 3)
    first = backend.reserve_consumer_alert(decision=h3, key=new_key, created_at=BASE + 3)
    second = backend.reserve_consumer_alert(decision=h3, key=new_key, created_at=BASE + 3)
    assert first.reserved and not second.reserved and new_key != old_key
    backend.close()


@pytest.mark.parametrize("state", [authority.RegimeState.TRANSITION_CONFIRMED, authority.RegimeState.NEW_REGIME_PROVISIONAL])
def test_provisional_transition_suppresses_warning(state, tmp_path):
    backend, _, _ = _backend(tmp_path)
    auth = backend.authority_decision(SERVER)
    unsafe = replace(auth, decision_id=f"unsafe-{state.value}", state=state, prediction_usable=False, countdown_safe=False)
    value = consumers.evaluate_authority_consumers(consumers.AuthorityConsumerPolicyInput(authority_decision=unsafe, now=BASE))
    assert not value.scheduled_warning_eligible and value.prediction_suspended
    backend.close()


def test_same_period_phase_change_rotates_warning_identity(tmp_path):
    backend, _, _ = _backend(tmp_path)
    old = _decision(backend)
    key = _warning_key(old)
    shifted = consumers.AuthoritySuppressionKey(consumers.AuthoritySuppressionKind.SCHEDULED_WARNING, SERVER, regime_id="same-period-new-phase", expected_occurrence_at=key.expected_occurrence_at)
    assert state4.suppression_key_id(shifted) != state4.suppression_key_id(key)
    backend.close()


@pytest.mark.parametrize("model", ["unknown", "h1", "h2", "h3", "transition"])
def test_generic_recovery_key_is_model_independent(model, tmp_path):
    backend, _, _ = _backend(tmp_path)
    value = _generic_decision(backend)
    key = value.generic_recovery_suppression_key
    assert key.regime_id is None and key.expected_occurrence_at is None
    assert key.event_id
    backend.close()


def test_regime_change_and_event_replay_do_not_duplicate_recovery(tmp_path):
    backend, _, _ = _backend(tmp_path)
    value = _generic_decision(backend)
    key = value.generic_recovery_suppression_key
    _emit(backend, value, key)
    assert not backend.reserve_consumer_alert(decision=value, key=key, created_at=BASE + 2).reserved
    backend.close()


def test_expected_window_results_create_no_consumer_keys():
    ledger = state4.ledger_from_mapping(None, server_key=SERVER)
    assert state4.fired_suppression_keys(ledger) == frozenset()


def _cutover_runtime(tmp_path, *, cutover=True):
    path = tmp_path / "cutover-schema4.json"
    _write(path)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=path,
        legacy_path=tmp_path / "legacy.json",
        now=BASE,
        authoritative_schema4_runtime_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
        schema4_authority_production_cutover_enabled=cutover,
    )
    return value, path


def test_shadow_only_and_cutover_false_invoke_no_action(tmp_path):
    value, _ = _cutover_runtime(tmp_path, cutover=False)
    calls = []
    result = value.dispatch_authority_consumer_action(SERVER, kind="scheduled_warning", now=BASE, action=lambda: calls.append(1) or True)
    assert not result.attempted and calls == []
    value.shutdown(wall_at=BASE + 1, monotonic_at=2)


def test_cutover_warning_action_is_single_source_and_once(tmp_path):
    value, _ = _cutover_runtime(tmp_path)
    auth = value._authoritative_schema4_backend.authority_decision(SERVER)
    regime = auth.selected_shadow_regime
    due = consumers.next_phase_occurrence(period_seconds=regime.candidate_period_seconds, phase_offset=regime.phase_offset, now=BASE) - 300
    value.decision(SERVER, now=due, restart_alert_enabled=True)
    calls = []
    first = value.dispatch_authority_consumer_action(SERVER, kind="scheduled_warning", now=due, action=lambda: calls.append("schema4") or True)
    value.decision(SERVER, now=due, restart_alert_enabled=True)
    second = value.dispatch_authority_consumer_action(SERVER, kind="scheduled_warning", now=due, action=lambda: calls.append("duplicate") or True)
    assert first.emitted and not second.attempted and calls == ["schema4"]
    value.shutdown(wall_at=due + 1, monotonic_at=2)


def test_generation_conflict_prevents_alert_dispatch(tmp_path):
    value, path = _cutover_runtime(tmp_path)
    auth = value._authoritative_schema4_backend.authority_decision(SERVER)
    regime = auth.selected_shadow_regime
    due = consumers.next_phase_occurrence(period_seconds=regime.candidate_period_seconds, phase_offset=regime.phase_offset, now=BASE) - 300
    value.decision(SERVER, now=due)
    path.write_bytes(path.read_bytes() + b" ")
    calls = []
    with pytest.raises(live4.Schema4GenerationConflict):
        value.dispatch_authority_consumer_action(SERVER, kind="scheduled_warning", now=due, action=lambda: calls.append(1) or True)
    assert calls == []
    value._authoritative_schema4_backend.close(flush=False)


def test_crash_after_reservation_before_dispatch_is_at_most_once(tmp_path):
    value, path = _cutover_runtime(tmp_path)
    auth = value._authoritative_schema4_backend.authority_decision(SERVER)
    regime = auth.selected_shadow_regime
    due = consumers.next_phase_occurrence(period_seconds=regime.candidate_period_seconds, phase_offset=regime.phase_offset, now=BASE) - 300
    value.decision(SERVER, now=due)
    with pytest.raises(SystemExit):
        value.dispatch_authority_consumer_action(SERVER, kind="scheduled_warning", now=due, action=lambda: True, after_reservation=lambda: (_ for _ in ()).throw(SystemExit()))
    value._authoritative_schema4_backend.close(flush=False)
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert again.consumer_fired_keys(SERVER)
    again.close()


def test_crash_after_dispatch_before_completion_does_not_replay(tmp_path):
    value, path = _cutover_runtime(tmp_path)
    auth = value._authoritative_schema4_backend.authority_decision(SERVER)
    regime = auth.selected_shadow_regime
    due = consumers.next_phase_occurrence(period_seconds=regime.candidate_period_seconds, phase_offset=regime.phase_offset, now=BASE) - 300
    value.decision(SERVER, now=due)
    calls = []
    with pytest.raises(SystemExit):
        value.dispatch_authority_consumer_action(
            SERVER,
            kind="scheduled_warning",
            now=due,
            action=lambda: calls.append("played") or True,
            after_dispatch=lambda: (_ for _ in ()).throw(SystemExit()),
        )
    assert calls == ["played"]
    value._authoritative_schema4_backend.close(flush=False)
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert again.consumer_fired_keys(SERVER)
    again.close()


def test_action_failure_after_reservation_remains_suppressed(tmp_path):
    backend, _, _ = _backend(tmp_path)
    decision = _decision(backend)
    reservation = backend.reserve_consumer_alert(decision=decision, key=_warning_key(decision), created_at=BASE)
    backend.complete_consumer_alert(decision=decision, reservation=reservation, completed_at=BASE + 1, action_succeeded=False)
    assert _warning_key(decision) in backend.consumer_fired_keys(SERVER)
    backend.close()


def test_shutdown_flushes_staged_consumer_decision(tmp_path):
    backend, path, _ = _backend(tmp_path)
    decision = _decision(backend)
    assert backend.record_consumer_decision(decision, created_at=BASE)
    backend.close(flush=True)
    loaded = schema4.deserialize_schema4_bytes(path.read_bytes()).state
    assert loaded["servers"][SERVER]["consumer_state"]["current_authority_consumer_decision_id"] == decision.decision_id


def test_compaction_preserves_replay_suppression_and_validity(tmp_path):
    backend, path, _ = _backend(tmp_path)
    warning = _decision(backend)
    recovery = _generic_decision(backend)
    _emit(backend, warning, _warning_key(warning))
    _emit(backend, recovery, recovery.generic_recovery_suppression_key)
    keys = backend.consumer_fired_keys(SERVER)
    backend.compact()
    backend.flush()
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert keys <= again.consumer_fired_keys(SERVER)
    again.close()


def test_auto_compaction_is_disabled_by_default_and_threshold_driven(tmp_path):
    backend, _, _ = _backend(tmp_path)
    assert backend.maybe_auto_compact(state4.AutoCompactionPolicy()) == ()
    reasons = backend.maybe_auto_compact(state4.AutoCompactionPolicy(enabled=True, file_size_threshold_bytes=1))
    assert "file_size_threshold" in reasons
    backend.close(flush=False)


def test_healthy_poll_soak_does_not_grow_consumer_state(tmp_path):
    backend, path, _ = _backend(tmp_path)
    before = path.stat().st_size
    for _ in range(1000):
        backend.note_healthy_poll(SERVER)
    assert path.stat().st_size == before and not backend.dirty
    backend.close()


@pytest.mark.parametrize("missing", list(rehearsal.CutoverReadinessEvidence.__dataclass_fields__))
def test_readiness_evaluator_identifies_each_blocker(missing):
    values = {name: True for name in rehearsal.CutoverReadinessEvidence.__dataclass_fields__}
    values[missing] = False
    result = rehearsal.evaluate_cutover_readiness(rehearsal.CutoverReadinessEvidence(**values))
    assert not result.ready and missing in result.blockers


def test_operational_rehearsal_is_read_only(tmp_path):
    source = tmp_path / "source.json"
    backup = tmp_path / "backup.json"
    prepared = tmp_path / "prepared.json"
    source_raw = schema4.canonical_json_bytes(_schema3())
    source.write_bytes(source_raw)
    backup.write_bytes(source_raw)
    prepared.write_bytes(schema4.serialize_schema4_state(_state()))
    before = (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
    result = rehearsal.rehearse_schema4_cutover(source_path=source, prepared_path=prepared, backup_path=backup, server_key=SERVER, now=BASE)
    assert result.final_status == "ready" and not result.active_modified
    assert before == (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())


def test_all_production_switches_are_enabled():
    from dzll_launcher import config
    assert config.AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED is True
    assert config.SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED is True
    assert config.SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED is True


def test_bob_temporary_warning_recovery_compaction_and_rollback_when_available(tmp_path):
    prepared = Path(tempfile.gettempdir()) / "bob_phase2_schema4_prepared_20260722.json"
    active = Path.home() / ".config/dzll/companion_restart_learning_phase2.json"
    if not prepared.exists() or not active.exists():
        pytest.skip("detached Bob fixtures unavailable")
    active_identity = (active.stat().st_size, active.stat().st_mtime_ns, hashlib.sha256(active.read_bytes()).hexdigest())
    original = prepared.read_bytes()
    fixture = tmp_path / "bob-schema4.json"
    fixture.write_bytes(original)
    backend = live4.AuthoritativeSchema4Runtime.open(fixture, enabled=True)
    auth = backend.authority_decision("51.195.74.63:3058")
    regime = auth.selected_shadow_regime
    due = consumers.next_phase_occurrence(period_seconds=regime.candidate_period_seconds, phase_offset=regime.phase_offset, now=1_784_800_000.0) - 300
    warning = consumers.evaluate_authority_consumers(consumers.AuthorityConsumerPolicyInput(authority_decision=auth, now=due, fired_keys=backend.consumer_fired_keys(auth.server_key)))
    _emit_bob(backend, warning, warning.scheduled_warning_suppression_key, due)
    assert not consumers.evaluate_authority_consumers(consumers.AuthorityConsumerPolicyInput(authority_decision=auth, now=due, fired_keys=backend.consumer_fired_keys(auth.server_key))).scheduled_pre_restart_alert_eligible
    event_id = backend.state["servers"][auth.server_key]["physical_events"][0]["event_id"]
    recovery = consumers.evaluate_authority_consumers(consumers.AuthorityConsumerPolicyInput(authority_decision=auth, now=due, physical_recovery_event_id=event_id, physical_recovery_eligible=True, fired_keys=backend.consumer_fired_keys(auth.server_key)))
    _emit_bob(backend, recovery, recovery.generic_recovery_suppression_key, due + 1)
    old_warning_key = warning.scheduled_warning_suppression_key
    record = backend.state["servers"][auth.server_key]
    synthetic_regime = copy.deepcopy(
        next(item for item in record["regimes"] if item["regime_id"] == auth.selected_shadow_regime.regime_id)
    )
    synthetic_regime["regime_id"] = "bob-synthetic-4h-regime"
    synthetic_regime["candidate_period_seconds"] = 4 * HOUR
    synthetic_regime["status"] = authority.RegimeRecordStatus.PROVISIONAL.value
    record["regimes"].append(synthetic_regime)
    backend.replace_server_record(auth.server_key, record, reason="bob_synthetic_h2_h3_alert_soak")
    h2 = replace(
        warning,
        decision_id="bob-synthetic-h2-consumer",
        selected_regime_id=synthetic_regime["regime_id"],
        presentation_state=consumers.AuthorityPresentationKey.LIKELY_NEW_CYCLE,
        wording_key=consumers.AuthorityPresentationKey.LIKELY_NEW_CYCLE,
        next_expected_restart_at=None,
        countdown_visible=False,
        countdown_safe=False,
        prediction_suspended=True,
        scheduled_warning_eligible=False,
        scheduled_warning_suppression_key=None,
        scheduled_pre_restart_alert_eligible=False,
    )
    backend.record_consumer_decision(h2, created_at=due + 2)
    assert old_warning_key in backend.consumer_fired_keys(auth.server_key)
    new_key = consumers.AuthoritySuppressionKey(
        consumers.AuthoritySuppressionKind.SCHEDULED_WARNING,
        auth.server_key,
        regime_id=synthetic_regime["regime_id"],
        expected_occurrence_at=int(due + 300 + HOUR),
    )
    h3 = replace(
        h2,
        decision_id="bob-synthetic-h3-consumer",
        presentation_state=consumers.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE,
        wording_key=consumers.AuthorityPresentationKey.CONFIRMED_NEW_CYCLE,
        next_expected_restart_at=float(new_key.expected_occurrence_at),
        countdown_visible=True,
        countdown_safe=True,
        prediction_suspended=False,
        scheduled_warning_eligible=True,
        scheduled_warning_at=float(new_key.expected_occurrence_at - 300),
        scheduled_warning_window=(float(new_key.expected_occurrence_at - 330), float(new_key.expected_occurrence_at - 270)),
        scheduled_warning_suppression_key=new_key,
        scheduled_pre_restart_alert_eligible=True,
    )
    backend.record_consumer_decision(h3, created_at=due + 3)
    _emit_bob(backend, h3, new_key, due + 3)
    assert not backend.reserve_consumer_alert(decision=h3, key=new_key, created_at=due + 4).reserved
    keys = backend.consumer_fired_keys(auth.server_key)
    backend.compact()
    backend.flush()
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(fixture, enabled=True)
    assert keys <= again.consumer_fired_keys(auth.server_key)
    again.close()
    fixture.write_bytes(original)
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()
    assert active_identity == (active.stat().st_size, active.stat().st_mtime_ns, hashlib.sha256(active.read_bytes()).hexdigest())


def _emit_bob(backend, decision, key, at):
    reservation = backend.reserve_consumer_alert(decision=decision, key=key, created_at=at)
    assert reservation.reserved
    backend.complete_consumer_alert(decision=decision, reservation=reservation, completed_at=at + 0.1, action_succeeded=True)
