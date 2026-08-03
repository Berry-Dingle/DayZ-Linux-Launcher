from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage
from dzll_launcher import window as window_module
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher.window import DZLLWindow


BASE = 1_900_000_000.0
SERVER = "stage3a.example:2302"


def _event(sequence: int, at: float) -> detection.PhysicalRestartEvent:
    return detection.PhysicalRestartEvent(
        event_id=f"event-{sequence}-{int(at)}",
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
            at - 70, at - 60, 3, (detection.InfoStatus.TIMEOUT,), at
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("stage3a_fixture",),
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        continuity_chain_id="chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _schema3_state() -> dict:
    events = tuple(_event(index + 1, BASE + index * 3 * scoring.HOUR) for index in range(4))
    covered = scoring.CoverageSegment(
        BASE,
        BASE + 9 * scoring.HOUR,
        scoring.CoverageKind.ONLINE_HEALTHY,
        10.0,
        server_key=SERVER,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        continuity_chain_id="chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )
    state = storage.new_phase2_state(
        now=BASE + 10 * scoring.HOUR,
        generation_id="00000000-0000-4000-8000-000000000034",
    )
    state["servers"] = {SERVER: {
        "runtime_state_version": 1,
        "scoring_semantics_version": 1,
        "aggregate_semantics_version": 1,
        "events": [runtime._serialize_event(item) for item in events],
        "event_seq": 4,
        "folded_through_event_seq": 0,
        "coverage_segments": [runtime._serialize_coverage(covered)],
        "monitoring_sessions": [{
            "session_id": "monitor",
            "app_session_id": "app",
            "poll_generation": 1,
            "continuity_chain_id": "chain",
            "continuity_chain_ids": ["chain"],
            "provenance_version": 1,
            "started_at": BASE - 120,
            "ended_at": BASE + 9 * scoring.HOUR + 120,
            "reason": "shutdown",
        }],
        "expected_window_misses": [],
        "fired_keys": [],
        "routed_fingerprints": [item.fingerprint for item in events],
        "incumbent_period_seconds": None,
        "regime_generation": "stage3a-fixture",
        "aggregate": runtime._serialize_aggregate(scoring.LongTermAggregate()),
        "prior_regimes": [],
        "active_episode": None,
        "incomplete_episodes": [],
        "candidate_diagnostics": {},
        "consumer_adapter_version": 1,
    }}
    return state


def _state() -> dict:
    raw = schema4.canonical_json_bytes(_schema3_state())
    return schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1_900_000_000_000_000_000
    )


def _write(path: Path, state: dict | None = None) -> bytes:
    payload = schema4.serialize_schema4_state(_state() if state is None else state)
    path.write_bytes(payload)
    return payload


def _open(tmp_path: Path, *, state: dict | None = None, crash=None):
    path = tmp_path / "authoritative-schema4.json"
    original = _write(path, state)
    value = live4.AuthoritativeSchema4Runtime.open(
        path, enabled=True, crash_injector=crash
    )
    return value, path, original


def _changed_record(value: live4.AuthoritativeSchema4Runtime, marker: str = "changed") -> dict:
    record = value.state["servers"][SERVER]
    record["reason_codes"] = sorted(set(record["reason_codes"]) | {marker})
    return record


def test_feature_default_is_disabled():
    assert live4.AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED_DEFAULT is False


def test_disabled_runtime_does_not_open_or_create_file(tmp_path):
    path = tmp_path / "absent.json"
    value = live4.AuthoritativeSchema4Runtime.open(path)
    assert value.snapshot().enabled is False
    assert not path.exists()


def test_valid_authoritative_load_restores_decision(tmp_path):
    value, _path, _raw = _open(tmp_path)
    snap = value.snapshot()
    assert snap.enabled and snap.valid_server_keys == (SERVER,)
    assert snap.decision_ids[0][1]
    value.close(flush=False)


@pytest.mark.parametrize("version", [3, 5])
def test_unsupported_root_fails_closed(tmp_path, version):
    path = tmp_path / "state.json"
    path.write_bytes(schema4.canonical_json_bytes({"schema_version": version}))
    before = path.read_bytes()
    with pytest.raises(schema4.Schema4Error):
        live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert path.read_bytes() == before


def test_invalid_root_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(live4.Schema4RuntimeError):
        live4.AuthoritativeSchema4Runtime.open(path, enabled=True)


def test_one_invalid_server_is_quarantined_only(tmp_path):
    state = _state()
    state["servers"]["bad.example:1"] = {"server_key": "bad.example:1"}
    path = tmp_path / "state.json"
    path.write_bytes(schema4.canonical_json_bytes(state))
    value = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    snap = value.snapshot()
    assert snap.valid_server_keys == (SERVER,)
    assert snap.quarantined_server_keys == ("bad.example:1",)
    value.close(flush=False)


def test_valid_server_can_persist_while_other_server_stays_quarantined(tmp_path):
    state = _state()
    state["servers"]["bad.example:1"] = {"server_key": "bad.example:1"}
    path = tmp_path / "state.json"
    path.write_bytes(schema4.canonical_json_bytes(state))
    value = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    value.replace_server_record(SERVER, _changed_record(value), reason="quarantine-safe")
    assert value.flush().wrote
    loaded = schema4.deserialize_schema4_bytes(path.read_bytes(), quarantine_invalid_servers=True)
    assert loaded.report.valid_server_keys == (SERVER,)
    assert loaded.report.quarantined_server_keys == ("bad.example:1",)
    value.close(flush=False)


def test_unchanged_load_flush_is_no_write(tmp_path):
    value, path, raw = _open(tmp_path)
    stat = path.stat()
    result = value.flush()
    assert not result.wrote and path.read_bytes() == raw
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    value.close(flush=False)


def test_export_snapshot_is_locked_canonical_and_includes_dirty_memory(tmp_path):
    value, path, raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value, "export-dirty"), reason="export")
    before = path.read_bytes()
    snapshot = value.export_snapshot()
    assert snapshot.schema_version == 4
    assert snapshot.server_count == 1
    assert snapshot.sha256 == hashlib.sha256(snapshot.canonical_bytes).hexdigest()
    assert b"export-dirty" in snapshot.canonical_bytes
    assert path.read_bytes() == before == raw
    assert value.snapshot().dirty_server_keys == (SERVER,)
    value.close(flush=False)


def test_phase2_export_proxy_does_not_interrupt_monitoring(tmp_path):
    path = tmp_path / "export-phase2.json"
    _write(path)
    value = _cutover_runtime(path)
    session, _created = value.ensure_monitoring_session(
        SERVER, wall_at=BASE, monotonic_at=0, poll_generation=7
    )
    snapshot = value.export_authoritative_schema4_snapshot()
    assert snapshot.server_count == 1
    assert value.active_monitoring_session_id(SERVER) == session
    value.shutdown(wall_at=BASE + 1, monotonic_at=1)


def test_startup_import_then_runtime_write_advances_imported_generation(tmp_path):
    live = tmp_path / "companion_restart_learning_phase2.json"
    old_state = _state()
    old_state["runtime_write_generation"] = 3
    live.write_bytes(schema4.serialize_schema4_state(old_state))
    imported_state = _state()
    imported_state["runtime_write_generation"] = 40
    imported_state["reason_codes"] = sorted(
        set(imported_state["reason_codes"]) | {"imported-generation"}
    )
    imported = schema4.serialize_schema4_state(imported_state)
    validated = transfer.validate_external_learning_bytes(
        imported, source_filename="imported.json"
    )
    transfer.stage_pending_import(validated, config_dir=tmp_path)
    applied = transfer.apply_pending_import_at_startup(
        config_dir=tmp_path, live_path=live
    )
    assert applied.status == "applied"

    value = _cutover_runtime(live)
    backend = value._authoritative_schema4_backend
    assert backend.snapshot().generation == 40
    backend.replace_server_record(
        SERVER, _changed_record(backend, "post-import-write"), reason="post-import"
    )
    result = backend.flush()
    assert result.wrote and result.generation == 41
    assert b"post-import-write" in live.read_bytes()
    value.shutdown(wall_at=BASE + 1, monotonic_at=1)


def _cutover_runtime(path: Path) -> runtime.Phase2RestartRuntime:
    return runtime.Phase2RestartRuntime.initialize(
        active_path=path,
        legacy_path=path.with_name("legacy.json"),
        now=BASE + 10 * scoring.HOUR,
        authoritative_schema4_runtime_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
        schema4_authority_production_cutover_enabled=True,
    )


def test_real_schema4_confirmed_offline_summary_is_safe_ephemeral_and_reloadable(
    tmp_path, monkeypatch
):
    path = tmp_path / "confirmed-offline-schema4.json"
    _write(path)
    now = BASE + 10 * scoring.HOUR + 100
    value = _cutover_runtime(path)
    backend = value._authoritative_schema4_backend
    messages = []
    host = SimpleNamespace(
        _server_companion_snapshot={"online": True},
        _server_companion_consecutive_offline_polls=1,
        _server_companion_restart_alert_enabled=True,
        _server_companion_restart_learning_key=lambda: SERVER,
        _debug_server_companion_alert=messages.append,
        _companion_restart_phase2=value,
    )
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True
    )
    monkeypatch.setattr(window_module.time, "time", lambda: now)

    first_strike = DZLLWindow._server_companion_restart_learning_summary(host)
    assert first_strike["authority_consumer"]
    assert first_strike["presentation_key"] == "confirmed_cycle"
    assert first_strike["countdown_text"] != "00:00"
    assert first_strike["countdown_safe"] and first_strike["countdown_visible"]

    # The online evaluation is a durable consumer-decision boundary.  Persist it
    # before measuring the availability-only confirmed-offline transition.
    assert backend.snapshot().dirty_server_keys == (SERVER,)
    assert backend.flush().wrote
    durable_online = path.read_bytes()
    durable_online_stat = path.stat()

    host._server_companion_snapshot = {"online": False}
    host._server_companion_consecutive_offline_polls = 2
    offline = DZLLWindow._server_companion_restart_learning_summary(host)
    repeated = DZLLWindow._server_companion_restart_learning_summary(host)

    assert offline == repeated
    assert offline["cycle_text"] == first_strike["cycle_text"]
    assert offline["confidence_percent"] == first_strike["confidence_percent"]
    assert offline["next_text"] == first_strike["next_text"]
    assert offline["presentation_key"] == "confirmed_cycle"
    assert offline["countdown_text"] == "00:00"
    assert offline["countdown_safe"] and offline["countdown_visible"]
    assert offline["prediction_usable"]
    assert "confirmed_offline_countdown_held_at_zero" in offline["reason_codes"]
    assert backend.snapshot().dirty_server_keys == ()
    assert backend.snapshot().write_count == 1
    assert path.read_bytes() == durable_online
    assert path.stat().st_mtime_ns == durable_online_stat.st_mtime_ns

    actions = []
    warning = value.dispatch_authority_consumer_action(
        SERVER,
        kind="scheduled_warning",
        now=now,
        action=lambda: actions.append("warning") or True,
    )
    assert warning.source == "schema4" and not warning.attempted
    assert actions == []
    assert backend.snapshot().dirty_server_keys == ()
    backend.close(flush=False)

    reloaded = _cutover_runtime(path)
    host._companion_restart_phase2 = reloaded
    after_reload = DZLLWindow._server_companion_restart_learning_summary(host)
    assert after_reload == offline
    assert reloaded._authoritative_schema4_backend.snapshot().dirty_server_keys == ()
    assert path.read_bytes() == durable_online

    host._server_companion_snapshot = {"online": True}
    host._server_companion_consecutive_offline_polls = 0
    recovered = DZLLWindow._server_companion_restart_learning_summary(host)
    assert recovered["countdown_text"] != "00:00"
    assert recovered["countdown_visible"] and recovered["countdown_safe"]
    reloaded._authoritative_schema4_backend.close(flush=False)


def test_online_to_offline_health_only_consumer_change_does_not_dirty_or_write(
    tmp_path
):
    path = tmp_path / "health-only-schema4.json"
    _write(path)
    value = _cutover_runtime(path)
    backend = value._authoritative_schema4_backend
    now = BASE + 10 * scoring.HOUR + 100

    value.decision(
        SERVER,
        now=now,
        server_online_healthy=True,
        restart_alert_enabled=True,
    )
    online = value._authority_consumer_shadow_decisions[SERVER]
    assert backend.snapshot().dirty_server_keys == (SERVER,)
    assert backend.flush().wrote
    persisted = path.read_bytes()
    persisted_stat = path.stat()

    value.decision(
        SERVER,
        now=now,
        server_online_healthy=False,
        restart_alert_enabled=True,
    )
    offline = value._authority_consumer_shadow_decisions[SERVER]

    assert online.decision_id != offline.decision_id
    assert online.scheduled_warning_eligible
    assert not offline.scheduled_warning_eligible
    assert offline.countdown_safe and offline.countdown_visible
    assert value.authority_consumer_resolution(SERVER).source is consumers4.CutoverSource.SCHEMA4
    assert backend.snapshot().dirty_server_keys == ()
    assert backend.snapshot().write_count == 1
    assert path.read_bytes() == persisted
    assert path.stat().st_mtime_ns == persisted_stat.st_mtime_ns
    backend.close(flush=False)


def test_schema4_recovery_alert_remains_independent_and_exactly_once(tmp_path):
    path = tmp_path / "recovery-schema4.json"
    _write(path)
    value = _cutover_runtime(path)
    backend = value._authoritative_schema4_backend
    now = BASE + 9 * scoring.HOUR
    # Use a retained physical event: suppression-key validation deliberately
    # rejects recovery keys that do not reference durable physical evidence.
    event = _event(4, now)

    value.decision(
        SERVER,
        now=now,
        server_online_healthy=True,
        restart_alert_enabled=True,
        event=event,
        generic_duration_ok=True,
    )
    consumer = value._authority_consumer_shadow_decisions[SERVER]
    assert consumer.generic_recovery_alert_eligible
    actions = []
    first = value.dispatch_authority_consumer_action(
        SERVER,
        kind="generic_recovery",
        now=now,
        action=lambda: actions.append(event.event_id) or True,
    )
    second = value.dispatch_authority_consumer_action(
        SERVER,
        kind="generic_recovery",
        now=now,
        action=lambda: actions.append(event.event_id) or True,
    )

    assert first.source == "schema4" and first.emitted
    assert second.source == "schema4" and not second.emitted
    assert actions == [event.event_id]
    backend.close(flush=False)


def test_atomic_write_success_and_last_known_good(tmp_path):
    value, path, raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="atomic")
    result = value.flush()
    assert result.wrote and result.generation == 1
    assert value.last_known_good_path.read_bytes() == raw
    assert schema4.deserialize_schema4_bytes(path.read_bytes()).report.valid
    value.close(flush=False)


@pytest.mark.parametrize(
    "point",
    [
        live4.Schema4CrashPoint.BEFORE_TEMP_COMPLETE,
        live4.Schema4CrashPoint.AFTER_TEMP_FSYNC_BEFORE_REPLACE,
    ],
)
def test_failure_before_replace_preserves_active(tmp_path, point):
    def crash(current):
        if current is point:
            raise RuntimeError(point.value)

    value, path, raw = _open(tmp_path, crash=crash)
    value.replace_server_record(SERVER, _changed_record(value), reason="crash")
    with pytest.raises(RuntimeError):
        value.flush()
    assert path.read_bytes() == raw
    value.close(flush=False)


def test_crash_after_replace_leaves_complete_new_file(tmp_path):
    def crash(current):
        if current is live4.Schema4CrashPoint.AFTER_REPLACE:
            raise RuntimeError("after replace")

    value, path, raw = _open(tmp_path, crash=crash)
    value.replace_server_record(SERVER, _changed_record(value), reason="after")
    with pytest.raises(RuntimeError):
        value.flush()
    assert path.read_bytes() != raw
    assert schema4.deserialize_schema4_bytes(path.read_bytes()).report.valid
    value.close(flush=False)


def test_invalid_prepared_state_never_replaces_active(tmp_path, monkeypatch):
    value, path, raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="invalid")
    monkeypatch.setattr(live4, "serialize_schema4_state", lambda _state: b"{}\n")
    with pytest.raises(schema4.Schema4Error):
        value.flush()
    assert path.read_bytes() == raw
    value.close(flush=False)


def test_file_and_directory_fsync_are_exercised(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    value, _path, _raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="fsync")
    value.flush()
    assert len(calls) >= 3
    value.close(flush=False)


def test_stale_temporary_file_is_removed(tmp_path):
    path = tmp_path / "state.json"
    _write(path)
    stale = tmp_path / f".{path.name}.schema4-runtime-dead.tmp"
    stale.write_bytes(b"partial")
    value = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert not stale.exists()
    assert str(stale) in value.snapshot().recovery.stale_temporary_files_removed
    value.close(flush=False)


def test_valid_last_known_good_recovers_invalid_active(tmp_path):
    value, path, raw = _open(tmp_path)
    backup = value.last_known_good_path
    backup.write_bytes(raw)
    value.close(flush=False)
    path.write_bytes(b"broken")
    recovered = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert path.read_bytes() == raw
    assert recovered.snapshot().recovery.restored_last_known_good
    recovered.close(flush=False)


def test_invalid_last_known_good_is_never_restored(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"broken-active")
    path.with_name(path.name + ".last-known-good").write_bytes(b"broken-backup")
    with pytest.raises(live4.Schema4RuntimeError):
        live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert path.read_bytes() == b"broken-active"


def test_healthy_poll_does_not_dirty_or_write(tmp_path):
    value, path, raw = _open(tmp_path)
    for _ in range(500):
        value.note_healthy_poll(SERVER)
    assert not value.dirty and value.snapshot().write_count == 0
    assert path.read_bytes() == raw
    value.close(flush=False)


def test_server_semantic_change_marks_only_that_server_dirty(tmp_path):
    value, _path, _raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="event")
    assert value.snapshot().dirty_server_keys == (SERVER,)
    value.close(flush=False)


@pytest.mark.parametrize(
    "reason",
    [
        "finalized_physical_event",
        "relationship_created",
        "streak_extended",
        "expected_window_completed",
        "expected_window_reconciled",
        "regime_transition",
        "chain_lifecycle_completed",
    ],
)
def test_durable_triggers_produce_one_coalesced_write(tmp_path, reason):
    value, _path, _raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value, reason), reason=reason)
    first = value.flush()
    second = value.flush()
    assert first.wrote and not second.wrote and value.snapshot().write_count == 1
    value.close(flush=False)


def test_shutdown_close_flushes_pending_state(tmp_path):
    value, path, raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="shutdown")
    value.close(flush=True)
    assert path.read_bytes() != raw


def test_replay_identical_server_record_deduplicates(tmp_path):
    value, _path, _raw = _open(tmp_path)
    record = _changed_record(value)
    assert value.replace_server_record(SERVER, record, reason="replay")
    value.flush()
    assert not value.replace_server_record(SERVER, value.state["servers"][SERVER], reason="replay")
    value.close(flush=False)


def test_generation_conflict_prevents_stale_overwrite(tmp_path):
    value, path, _raw = _open(tmp_path)
    value.replace_server_record(SERVER, _changed_record(value), reason="conflict")
    external = path.read_bytes() + b" "
    path.write_bytes(external)
    with pytest.raises(live4.Schema4GenerationConflict):
        value.flush()
    assert path.read_bytes() == external
    value.close(flush=False)


def test_second_runtime_writer_is_rejected(tmp_path):
    value, path, _raw = _open(tmp_path)
    with pytest.raises(live4.Schema4WriterLockError):
        live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    value.close(flush=False)


def test_migration_apply_conflicts_with_runtime_writer(tmp_path):
    source = tmp_path / "source-schema3.json"
    source.write_bytes(schema4.canonical_json_bytes(_schema3_state()))
    prepared = tmp_path / "prepared.json"
    backup = tmp_path / "backup.json"
    audit = tmp_path / "audit.json"
    result = schema4.migrate_state_file(
        source_path=source,
        prepared_path=prepared,
        audit_path=audit,
        backup_path=backup,
    )
    owner = live4.Schema4WriterLock(source)
    owner.acquire()
    try:
        with pytest.raises(live4.Schema4WriterLockError):
            schema4.apply_prepared_schema4(
                active_path=source,
                prepared_path=prepared,
                verified_backup_path=backup,
                expected_source_sha256=result.source_sha256,
                approval_token=schema4.SCHEMA4_APPLY_APPROVAL_TOKEN,
            )
    finally:
        owner.release()


def test_compaction_cannot_race_with_second_writer(tmp_path):
    value, path, _raw = _open(tmp_path)
    value.compact()
    with pytest.raises(live4.Schema4WriterLockError):
        live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    value.flush()
    value.close(flush=False)


def test_compaction_preserves_authority_decision_and_references(tmp_path):
    value, _path, _raw = _open(tmp_path)
    before = value.snapshot().decision_ids
    value.compact()
    value.flush()
    after = value.snapshot().decision_ids
    assert after == before
    assert schema4.validate_schema4_state(value.state).valid
    value.close(flush=False)


def test_reload_preserves_decision_and_generation(tmp_path):
    value, path, _raw = _open(tmp_path)
    before = value.snapshot().decision_ids
    value.replace_server_record(SERVER, _changed_record(value), reason="reload")
    value.flush()
    generation = value.snapshot().generation
    value.close(flush=False)
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    assert again.snapshot().decision_ids == before
    assert again.snapshot().generation == generation
    again.close(flush=False)


def test_retracted_miss_stays_inactive_after_reload(tmp_path):
    value, path, _raw = _open(tmp_path)
    ledger = schema4.persisted_expected_window_ledger(value.state["servers"][SERVER])
    assert not any(item.negative_penalty_active for item in ledger)
    value.close(flush=False)
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    ledger2 = schema4.persisted_expected_window_ledger(again.state["servers"][SERVER])
    assert ledger2 == ledger
    again.close(flush=False)


def test_long_inactivity_does_not_change_authority(tmp_path):
    value, path, raw = _open(tmp_path)
    before = value.snapshot().decision_ids
    for _ in range(10_000):
        value.note_healthy_poll(SERVER)
    assert value.snapshot().decision_ids == before and path.read_bytes() == raw
    value.close(flush=False)


def test_schema3_projection_is_read_only_copy(tmp_path):
    value, _path, _raw = _open(tmp_path)
    projected = value.schema3_projection()
    projected["servers"].clear()
    assert SERVER in value.schema3_projection()["servers"]
    value.close(flush=False)


def test_phase2_feature_disabled_keeps_schema3_bytes(tmp_path):
    active = tmp_path / "phase2.json"
    legacy = tmp_path / "legacy.json"
    active.write_bytes(schema4.canonical_json_bytes(_schema3_state()))
    before = active.read_bytes()
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE
    )
    assert not value.authoritative_schema4_runtime_enabled
    assert value.authoritative_schema4_snapshot() is None
    assert active.read_bytes() == before


def test_phase2_feature_enabled_loads_schema4_without_schema3_migration(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    before = active.read_bytes()
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR,
        app_session_id="stage3a",
        authoritative_schema4_runtime_enabled=True,
    )
    assert value.authoritative_schema4_snapshot().enabled
    assert active.read_bytes() == before
    value.shutdown(wall_at=BASE + 11 * scoring.HOUR, monotonic_at=1)


def test_phase2_enabled_healthy_poll_soak_is_bounded(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR,
        app_session_id="stage3a-soak",
        authoritative_schema4_runtime_enabled=True,
    )
    start = BASE + 11 * scoring.HOUR
    value.begin_monitoring(SERVER, wall_at=start, monotonic_at=0, poll_generation=2)
    writes = value.authoritative_schema4_snapshot().write_count
    for index in range(1, 40):
        value.ingest_live_result(
            SERVER,
            poll_generation=2,
            info={"ok": True, "players": 12, "max_players": 60},
            wall_at=start + index * 10,
            monotonic_at=index * 10,
        )
    # At most one completed expected-window decision may become durable during
    # the soak; the 39 ordinary poll edges themselves are coalesced.
    assert value.authoritative_schema4_snapshot().write_count <= writes + 1
    value.shutdown(wall_at=start + 400, monotonic_at=400)


def test_active_episode_signature_uses_semantic_threshold_buckets():
    config = detection.DetectionConfig()
    episode = SimpleNamespace(
        event_id="episode",
        drain_wall=BASE,
        low_start_wall=BASE,
        low_start_mono=0.0,
        low_samples=[0.0],
        low_sample_count=1,
        zero_reached=True,
        confirmed_offline_wall=None,
        info_return_wall=None,
        first_queue_wall=None,
        first_queue_mono=None,
        first_player_wall=None,
        first_player_mono=None,
        stable_recovery_wall=None,
        stable_recovery_mono=None,
        recovery_bounced=False,
        recovery_snapshot_unchanged=False,
        coverage_complete=True,
        lifecycle_interruption=None,
    )
    initial = runtime._active_episode_persistence_signature(
        episode, config=config
    )
    episode.low_sample_count = 2
    episode.low_samples.append(20.0)
    assert runtime._active_episode_persistence_signature(
        episode, config=config
    ) == initial
    episode.low_sample_count = 3
    episode.low_samples.append(config.low_state_minimum)
    threshold = runtime._active_episode_persistence_signature(
        episode, config=config
    )
    assert threshold != initial
    episode.low_sample_count = 20
    episode.low_samples.extend([60.0, 90.0, 120.0])
    assert runtime._active_episode_persistence_signature(
        episode, config=config
    ) == threshold


def test_confirmed_offline_counter_polls_coalesce_and_finalize_once(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR,
        app_session_id="offline-coalesce",
        authoritative_schema4_runtime_enabled=True,
    )
    start = BASE + 20 * scoring.HOUR
    value.begin_monitoring(
        SERVER, wall_at=start, monotonic_at=0, poll_generation=7
    )

    def poll(at, *, healthy=False, players=3):
        info = (
            (
                {"ok": True, "players": players, "max_players": 60}
                if players is not None
                else {"ok": True}
            )
            if healthy
            else {"ok": False, "err": "timeout"}
        )
        return value.ingest_live_result(
            SERVER,
            poll_generation=7,
            info=info,
            wall_at=start + at,
            monotonic_at=at,
        )

    poll(0, healthy=True, players=12)
    assert not poll(10).persisted
    boundary = poll(20)
    assert boundary.persisted
    episode = value._servers[SERVER].engine.active_episode
    assert value._servers[SERVER].engine.state is detection.EpisodeState.OFFLINE
    assert episode is not None and episode.failure_count == 2
    event_id = episode.event_id
    snapshot = value.authoritative_schema4_snapshot()
    generation = snapshot.generation
    writes = snapshot.write_count
    before = active.read_bytes()
    before_stat = active.stat()

    for index in range(50):
        update = poll(23 + index * 3)
        assert not update.persisted
        assert not update.state_changed
    episode = value._servers[SERVER].engine.active_episode
    assert episode is not None and episode.failure_count == 52
    assert episode.event_id == event_id
    after = value.authoritative_schema4_snapshot()
    assert after.generation == generation
    assert after.write_count == writes
    assert active.read_bytes() == before
    assert (active.stat().st_size, active.stat().st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )
    assert not any(item.event_id == event_id for item in value._servers[SERVER].events)

    recovery = poll(180, healthy=True, players=None)
    assert recovery.persisted
    completed = value.tick(
        SERVER, wall_at=start + 211, monotonic_at=211
    )
    assert completed.persisted
    assert len(completed.finalized_events) == 1
    event = completed.finalized_events[0]
    assert event.event_id == event_id
    assert event.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert event.outage.failure_count == 52
    assert event.authenticity == 0.85
    assert event.canonical_phase_at == start + 180
    assert sum(item.event_id == event_id for item in value._servers[SERVER].events) == 1
    assert schema4.validate_schema4_state(
        schema4.deserialize_schema4_bytes(active.read_bytes()).state
    ).valid
    value._authoritative_schema4_backend.close(flush=False)


def test_confirmed_offline_boundary_reloads_without_counter_write_churn(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    start = BASE + 20 * scoring.HOUR
    first = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=start,
        app_session_id="offline-before-crash",
        authoritative_schema4_runtime_enabled=True,
    )
    first.begin_monitoring(
        SERVER, wall_at=start, monotonic_at=0, poll_generation=8
    )

    def ingest(value, generation, at, *, healthy=False, players=3):
        return value.ingest_live_result(
            SERVER,
            poll_generation=generation,
            info=(
                (
                    {"ok": True, "players": players, "max_players": 60}
                    if players is not None
                    else {"ok": True}
                )
                if healthy
                else {"ok": False, "err": "timeout"}
            ),
            wall_at=start + at,
            monotonic_at=at,
        )

    ingest(first, 8, 0, healthy=True, players=12)
    ingest(first, 8, 10)
    assert ingest(first, 8, 20).persisted
    event_id = first._servers[SERVER].engine.active_episode.event_id
    first._authoritative_schema4_backend.close(flush=False)

    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=start + 30,
        app_session_id="offline-after-crash",
        authoritative_schema4_runtime_enabled=True,
    )
    restored = again._servers[SERVER].engine.active_episode
    assert restored is not None and restored.event_id == event_id
    assert again._servers[SERVER].engine.state is detection.EpisodeState.OFFLINE
    again.begin_monitoring(
        SERVER, wall_at=start + 30, monotonic_at=30, poll_generation=9
    )
    # A reload may first persist an independently completed expected-window
    # boundary; measure counter coalescing after that semantic decision.
    ingest(again, 9, 33)
    boundary = again.authoritative_schema4_snapshot()
    raw = active.read_bytes()
    for index in range(50):
        assert not ingest(again, 9, 36 + index * 3).persisted
    assert again._servers[SERVER].engine.active_episode.failure_count == 53
    settled = again.authoritative_schema4_snapshot()
    assert settled.generation == boundary.generation
    assert settled.write_count == boundary.write_count
    assert active.read_bytes() == raw

    assert ingest(again, 9, 200, healthy=True, players=None).persisted
    completed = again.tick(
        SERVER, wall_at=start + 231, monotonic_at=231
    )
    assert len(completed.finalized_events) == 1
    assert completed.finalized_events[0].event_id == event_id
    assert completed.finalized_events[0].outcome is (
        detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    )
    assert sum(
        item.event_id == event_id for item in again._servers[SERVER].events
    ) == 1
    again._authoritative_schema4_backend.close(flush=False)


def test_phase2_finalized_event_rebuilds_and_persists_schema4(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR,
        app_session_id="stage3a-event",
        authoritative_schema4_runtime_enabled=True,
    )
    start = BASE + 20 * scoring.HOUR
    value.begin_monitoring(SERVER, wall_at=start, monotonic_at=0, poll_generation=3)

    def poll(at, *, ok=True, players=12):
        info = {"ok": ok}
        if ok:
            info.update(players=players, max_players=60)
        else:
            info["err"] = "timeout"
        return value.ingest_live_result(
            SERVER,
            poll_generation=3,
            info=info,
            wall_at=start + at,
            monotonic_at=at,
        )

    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        poll(at, players=players)
    poll(90, ok=False)
    poll(100, ok=False)
    poll(130, players=3)
    completed = value.tick(SERVER, wall_at=start + 170, monotonic_at=170)
    assert completed.finalized_events
    persisted = schema4.deserialize_schema4_bytes(active.read_bytes()).state
    assert len(persisted["servers"][SERVER]["physical_events"]) == 5
    decision_id = persisted["servers"][SERVER]["authority_decision"]["decision_id"]
    value.shutdown(wall_at=start + 180, monotonic_at=180)
    again = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    assert again.snapshot().decision_ids == ((SERVER, decision_id),)
    again.close(flush=False)


def test_phase2_enabled_replay_restart_preserves_production_decision(tmp_path):
    active = tmp_path / "schema4.json"
    _write(active)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR,
        authoritative_schema4_runtime_enabled=True,
    )
    decision_before = value.decision(SERVER, now=BASE + 10 * scoring.HOUR)
    authority_id = value.authoritative_schema4_snapshot().decision_ids
    value.shutdown(wall_at=BASE + 10 * scoring.HOUR + 1, monotonic_at=1)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "unused.json",
        now=BASE + 10 * scoring.HOUR + 2,
        authoritative_schema4_runtime_enabled=True,
    )
    decision_after = again.decision(SERVER, now=BASE + 10 * scoring.HOUR + 2)
    assert decision_after.model_status == decision_before.model_status
    assert again.authoritative_schema4_snapshot().decision_ids == authority_id
    again.shutdown(wall_at=BASE + 10 * scoring.HOUR + 3, monotonic_at=2)


def test_enabled_schema4_does_not_modify_separate_schema3_file(tmp_path):
    schema3_active = tmp_path / "real-schema3.json"
    schema3_active.write_bytes(schema4.canonical_json_bytes(_schema3_state()))
    before = schema3_active.read_bytes()
    schema4_active = tmp_path / "schema4.json"
    _write(schema4_active)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=schema4_active,
        legacy_path=tmp_path / "unused.json",
        authoritative_schema4_runtime_enabled=True,
    )
    value.shutdown(wall_at=BASE + 20 * scoring.HOUR, monotonic_at=1)
    assert schema3_active.read_bytes() == before


def test_temporary_fixture_rollback_restores_exact_pre_soak_sha(tmp_path):
    value, path, raw = _open(tmp_path)
    original_hash = hashlib.sha256(raw).hexdigest()
    rollback = tmp_path / "rollback.json"
    rollback.write_bytes(raw)
    value.replace_server_record(SERVER, _changed_record(value), reason="soak")
    value.flush()
    value.close(flush=False)
    path.write_bytes(rollback.read_bytes())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_deterministic_soak_harness_restarts_compacts_and_rolls_back(tmp_path):
    path = tmp_path / "soak-schema4.json"
    original = _write(path)
    harness = live4.Schema4RuntimeSoakHarness(path)
    first = harness.start()
    harness.healthy_polls(SERVER, 1000)
    harness.install_server_record(
        SERVER, _changed_record(first, "soak-durable"), reason="soak-durable"
    )
    harness.flush()
    before_restart = first.snapshot().decision_ids
    second = harness.restart()
    assert second.snapshot().decision_ids == before_restart
    harness.compact()
    harness.flush()
    result = harness.result()
    assert result.start_count == 2 and result.healthy_poll_count == 1000
    assert "temporary_schema4_fixture_only" in result.reason_codes
    assert harness.rollback_fixture() == hashlib.sha256(original).hexdigest()


def test_bob_prepared_authoritative_runtime_isolated_soak_when_available(tmp_path):
    prepared = Path(tempfile.gettempdir()) / "bob_phase2_schema4_prepared_20260722.json"
    active = Path.home() / ".config/dzll/companion_restart_learning_phase2.json"
    if not prepared.exists() or not active.exists():
        pytest.skip("detached Bob prepared state is unavailable")
    active_stat = active.stat()
    active_hash = hashlib.sha256(active.read_bytes()).hexdigest()
    fixture = tmp_path / "bob-prepared-schema4.json"
    fixture.write_bytes(prepared.read_bytes())
    original = fixture.read_bytes()
    harness = live4.Schema4RuntimeSoakHarness(fixture)
    backend = harness.start()
    decision = schema4.persisted_authority_decision(backend.state["servers"]["51.195.74.63:3058"])
    selected = decision.selected_shadow_regime
    candidate = next(item for item in decision.candidates if item.regime_id == selected.regime_id)
    assert selected.candidate_period_seconds == 3 * scoring.HOUR
    assert candidate.high_authority == 0.97
    assert candidate.high_phase_authority == pytest.approx(0.968211)
    assert candidate.h3_gate and candidate.h4_gate
    assert decision.cycle_visible and decision.prediction_usable
    assert not backend.flush().wrote
    harness.healthy_polls("51.195.74.63:3058", 1000)
    assert backend.snapshot().write_count == 0
    decision_id = decision.decision_id
    assert schema4.persisted_authority_decision(
        harness.restart().state["servers"]["51.195.74.63:3058"]
    ).decision_id == decision_id
    harness.compact()
    harness.flush()
    assert schema4.persisted_authority_decision(
        harness.restart().state["servers"]["51.195.74.63:3058"]
    ).decision_id == decision_id
    assert harness.rollback_fixture() == hashlib.sha256(original).hexdigest()
    after_stat = active.stat()
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (
        active_stat.st_size, active_stat.st_mtime_ns
    )
    assert hashlib.sha256(active.read_bytes()).hexdigest() == active_hash
