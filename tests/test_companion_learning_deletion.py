"""Staged Server Companion learning-data deletion (applied at startup)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dzll_launcher import companion_learning_deletion as deletion
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4


BASE = 1_960_000_000.0
A = "delete-a.example:2302"
B = "delete-b.example:2302"
C = "delete-c.example:2302"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _open(active, legacy, now):
    return runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=now,
        generation_id="88888888-8888-4888-8888-888888888888",
        authoritative_schema4_runtime_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
        schema4_authority_production_cutover_enabled=True,
    )


def _poll(value, key, generation, at, *, ok=True, players=12, queue=None):
    info = {"ok": ok}
    if ok:
        info.update(players=players, max_players=60, ping_ms=40)
        if queue is not None:
            info["queue"] = queue
    else:
        info["err"] = "timeout"
    return value.ingest_live_result(
        key, poll_generation=generation, info=info, wall_at=BASE + at, monotonic_at=at
    )


def _restart(value, key, generation, start):
    assert value.begin_monitoring(
        key, wall_at=BASE + start, monotonic_at=start, poll_generation=generation
    )
    for offset, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        _poll(value, key, generation, start + offset, players=players)
    for offset in (90, 96, 102, 108, 114):
        _poll(value, key, generation, start + offset, ok=False)
    _poll(value, key, generation, start + 120, players=3, queue=2)
    value.tick(key, wall_at=BASE + start + 150, monotonic_at=start + 150)
    value.end_monitoring(
        key,
        marker=runtime.LifecycleMarker.PAUSE,
        wall_at=BASE + start + 200,
        monotonic_at=start + 200,
    )


def _store(tmp_path, *, quarantine=None):
    """A real three-server database; optionally one server quarantined."""

    active = tmp_path / "companion_restart_learning_phase2.json"
    legacy = tmp_path / "legacy.json"
    value = _open(active, legacy, BASE)
    for index, key in enumerate((A, B, C)):
        _restart(value, key, index + 1, 1000 * index)
    value.shutdown(wall_at=BASE + 5000, monotonic_at=5000)
    if quarantine is not None:
        state = json.loads(active.read_text(encoding="utf-8"))
        state["servers"][quarantine]["server_key"] = "someone-else.example:2302"
        active.write_bytes(schema4.canonical_json_bytes(state))
        backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
        try:
            exported = backend.export_snapshot()
        finally:
            backend.close(flush=False)
        assert exported.quarantined_server_keys == (quarantine,)
        active.write_bytes(exported.canonical_bytes)
    lkg = active.with_name(active.name + ".last-known-good")
    lkg.write_bytes(active.read_bytes())
    return active, legacy, lkg


def _records(raw):
    loaded = schema4.deserialize_schema4_bytes(raw, quarantine_invalid_servers=True)
    return loaded.state


def _canonical_record(state, key):
    return schema4.canonical_json_bytes(state["servers"][key])


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------


def test_stage_read_merge_and_cancel(tmp_path):
    staged = deletion.stage_pending_deletion(
        config_dir=tmp_path, scope="servers", server_keys=[B], now=NOW
    )
    assert staged.scope == "servers" and staged.server_keys == (B,)
    merged = deletion.stage_pending_deletion(
        config_dir=tmp_path, scope="servers", server_keys=[A, B], now=NOW
    )
    assert merged.server_keys == (A, B)
    everything = deletion.stage_pending_deletion(config_dir=tmp_path, scope="all", now=NOW)
    assert everything.scope == "all" and everything.server_keys == ()
    # Delete-all is never narrowed by a later single-server request.
    still_all = deletion.stage_pending_deletion(
        config_dir=tmp_path, scope="servers", server_keys=[C], now=NOW
    )
    assert still_all.scope == "all"
    assert deletion.cancel_pending_deletion(tmp_path) is True
    assert deletion.read_pending_deletion(tmp_path) is None
    assert deletion.cancel_pending_deletion(tmp_path) is False


@pytest.mark.parametrize(
    ("scope", "keys"),
    [("servers", []), ("servers", ["no-port"]), ("servers", ["host:0"]), ("all", [A]), ("odd", [])],
)
def test_stage_rejects_invalid_requests(tmp_path, scope, keys):
    with pytest.raises(deletion.PendingDeletionError):
        deletion.stage_pending_deletion(config_dir=tmp_path, scope=scope, server_keys=keys)
    assert not deletion.pending_deletion_exists(tmp_path)


def test_tampered_request_is_rejected(tmp_path):
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[A])
    path = deletion.pending_deletion_path(tmp_path)
    value = json.loads(path.read_text())
    value["server_keys"] = [B]
    path.write_text(json.dumps(value))
    with pytest.raises(deletion.PendingDeletionError, match="SHA-256"):
        deletion.read_pending_deletion(tmp_path)


def test_deletion_and_import_staging_are_mutually_exclusive(tmp_path):
    payload, _metadata = transfer.pending_import_paths(tmp_path)
    payload.write_bytes(b"{}")
    with pytest.raises(deletion.PendingDeletionError, match="replacement is pending"):
        deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    payload.unlink()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    validated = transfer.validate_external_learning_bytes(
        deletion.fresh_schema4_bytes(now=BASE), source_filename="x.json"
    )
    with pytest.raises(transfer.PendingImportError, match="deletion is pending"):
        transfer.stage_pending_import(validated, config_dir=tmp_path)


# --------------------------------------------------------------------------
# Startup application
# --------------------------------------------------------------------------


def test_delete_all_installs_fresh_state_and_overwrites_last_known_good_only(tmp_path):
    active, _legacy, lkg = _store(tmp_path)
    user_backup = tmp_path / "companion_restart_learning_phase2.my-own-backup.json"
    dzll_backup = tmp_path / "companion_restart_learning_phase2.import-backup-20260101-000000.json"
    user_backup.write_bytes(active.read_bytes())
    dzll_backup.write_bytes(b"backup bytes")
    before = {path.name for path in tmp_path.iterdir()}
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")

    result = deletion.apply_pending_deletion_at_startup(
        config_dir=tmp_path, live_path=active, now=BASE + 9000
    )

    assert result.status == "applied" and result.scope == "all"
    new = active.read_bytes()
    assert lkg.read_bytes() == new
    loaded = schema4.deserialize_schema4_bytes(new, quarantine_invalid_servers=False)
    assert loaded.report.valid and loaded.state["servers"] == {}
    for key in (A, B, C):
        assert not deletion.server_key_referenced(new.decode(), key)
    # No backup is created and no other file is touched.
    assert user_backup.read_bytes() != new and json.loads(user_backup.read_bytes())["servers"]
    assert dzll_backup.read_bytes() == b"backup bytes"
    assert {path.name for path in tmp_path.iterdir()} == before - {deletion.PENDING_DELETION_NAME}
    assert not deletion.pending_deletion_exists(tmp_path)


def test_single_server_deletion_scrubs_every_reference_and_keeps_others(tmp_path):
    active, legacy, lkg = _store(tmp_path)
    prior = _records(active.read_bytes())
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[A])

    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)

    assert result.status == "applied" and result.server_keys == (A,)
    new = active.read_bytes()
    assert lkg.read_bytes() == new
    assert not deletion.server_key_referenced(new.decode(), A)
    state = _records(new)
    assert set(state["servers"]) == {B, C}
    assert A not in state["schema3_snapshot"]["servers"]
    audit_keys = [item.get("server_key") for item in state["migration_audit"].get("servers", [])]
    assert A not in audit_keys
    for key in (B, C):
        assert _canonical_record(state, key) == _canonical_record(prior, key)

    # The runtime opens on the result: A is fresh (not "server_unavailable").
    value = _open(active, legacy, BASE + 20_000)
    try:
        backend = value._authoritative_schema4_backend
        assert backend.server_write_block(A) is None
        assert not backend.has_server_record(A)
        assert backend.has_server_record(B)
        assert value.has_learning_for(B)
        assert not value.has_learning_for(A)
    finally:
        value.shutdown(wall_at=BASE + 20_100, monotonic_at=20_100)


def test_quarantined_server_can_be_deleted(tmp_path):
    active, _legacy, lkg = _store(tmp_path, quarantine=C)
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[C])

    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)

    assert result.status == "applied"
    loaded = schema4.deserialize_schema4_bytes(active.read_bytes(), quarantine_invalid_servers=False)
    assert loaded.report.valid
    assert set(loaded.state["servers"]) == {A, B}
    assert lkg.read_bytes() == active.read_bytes()


def test_recovery_after_deletion_never_restores_deleted_servers(tmp_path):
    active, legacy, _lkg = _store(tmp_path, quarantine=C)
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[A, C])
    assert deletion.apply_pending_deletion_at_startup(
        config_dir=tmp_path, live_path=active
    ).status == "applied"
    active.write_bytes(b"{ damaged")

    value = _open(active, legacy, BASE + 30_000)
    try:
        backend = value._authoritative_schema4_backend
        assert backend.snapshot().recovery.restored_last_known_good
        assert not backend.has_server_record(A)
        assert not backend.has_server_record(C)
        assert backend.has_server_record(B)
    finally:
        value.shutdown(wall_at=BASE + 30_100, monotonic_at=30_100)


def test_missing_key_is_already_deleted(tmp_path):
    active, _legacy, lkg = _store(tmp_path)
    before = active.read_bytes()
    deletion.stage_pending_deletion(
        config_dir=tmp_path, scope="servers", server_keys=["never-seen.example:2302"]
    )
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "applied"
    assert active.read_bytes() == before
    assert lkg.read_bytes() == before


def test_last_known_good_failure_keeps_request_and_retry_completes(tmp_path, monkeypatch):
    active, _legacy, lkg = _store(tmp_path)
    old_lkg = lkg.read_bytes()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[A])
    real_write = deletion._atomic_write_verified

    def failing_for_lkg(destination, payload):
        if destination == lkg:
            raise OSError("disk full")
        return real_write(destination, payload)

    monkeypatch.setattr(deletion, "_atomic_write_verified", failing_for_lkg)
    first = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert first.status == "failed" and "disk full" in first.error
    assert deletion.pending_deletion_exists(tmp_path)
    assert lkg.read_bytes() == old_lkg
    assert not deletion.server_key_referenced(active.read_bytes().decode(), A)

    monkeypatch.setattr(deletion, "_atomic_write_verified", real_write)
    second = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert second.status == "applied"
    assert lkg.read_bytes() == active.read_bytes()
    assert not deletion.pending_deletion_exists(tmp_path)


def test_active_install_failure_leaves_both_files_unchanged(tmp_path, monkeypatch):
    active, _legacy, lkg = _store(tmp_path)
    before_active, before_lkg = active.read_bytes(), lkg.read_bytes()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    real_write = deletion._atomic_write_verified
    calls = []

    def failing_first_active_write(destination, payload):
        calls.append(destination)
        if destination == active and len(calls) == 1:
            raise OSError("read-only filesystem")
        return real_write(destination, payload)

    monkeypatch.setattr(deletion, "_atomic_write_verified", failing_first_active_write)
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "failed"
    assert active.read_bytes() == before_active
    assert lkg.read_bytes() == before_lkg
    assert deletion.pending_deletion_exists(tmp_path)


def test_lock_contention_applies_nothing(tmp_path):
    active, _legacy, lkg = _store(tmp_path)
    before = active.read_bytes()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    with live4.Schema4WriterLock(active):
        result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "failed"
    assert active.read_bytes() == before and lkg.read_bytes() == before
    assert deletion.pending_deletion_exists(tmp_path)


def test_conflict_with_pending_import_applies_neither(tmp_path):
    active, _legacy, _lkg = _store(tmp_path)
    before = active.read_bytes()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    payload, metadata = transfer.pending_import_paths(tmp_path)
    payload.write_bytes(b"{}")
    metadata.write_bytes(b"{}")
    assert deletion.pending_learning_changes_conflict(tmp_path)
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "conflict"
    assert "neither was applied" in result.notice_body
    assert active.read_bytes() == before
    assert deletion.pending_deletion_exists(tmp_path)


def test_unreadable_request_is_removed_without_deleting(tmp_path):
    active, _legacy, _lkg = _store(tmp_path)
    before = active.read_bytes()
    deletion.pending_deletion_path(tmp_path).write_text("{ nope")
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "pending_invalid"
    assert "nothing was deleted" in result.notice_body
    assert active.read_bytes() == before
    assert not deletion.pending_deletion_exists(tmp_path)


def test_no_active_file_still_scrubs_last_known_good(tmp_path):
    active, _legacy, lkg = _store(tmp_path)
    active.unlink()
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="servers", server_keys=[B])
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "applied"
    assert not active.exists()
    assert not deletion.server_key_referenced(lkg.read_bytes().decode(), B)


def test_nothing_pending_is_a_no_op(tmp_path):
    active, _legacy, _lkg = _store(tmp_path)
    result = deletion.apply_pending_deletion_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "none" and result.notice_title is None


def test_server_key_reference_matches_whole_identities_only():
    assert deletion.server_key_referenced('"1.2.3.4:2302"', "1.2.3.4:2302")
    assert deletion.server_key_referenced("1.2.3.4:2302|session|1", "1.2.3.4:2302")
    assert not deletion.server_key_referenced('"11.2.3.4:2302"', "1.2.3.4:2302")
    assert not deletion.server_key_referenced('"1.2.3.4:23020"', "1.2.3.4:2302")
    assert not deletion.server_key_referenced('"a.1.2.3.4:2302"', "1.2.3.4:2302")


def test_notices_describe_the_outcome():
    applied_all = deletion.StartupDeletionResult("applied", scope="all")
    assert applied_all.notice_title == "Server Companion learning data deleted"
    assert "All Server Companion restart-learning data was permanently deleted" in applied_all.notice_body
    one = deletion.StartupDeletionResult("applied", scope="servers", server_keys=(A,))
    assert "for 1 server was permanently deleted" in one.notice_body
    two = deletion.StartupDeletionResult("applied", scope="servers", server_keys=(A, B))
    assert "for 2 servers" in two.notice_body
    failed = deletion.StartupDeletionResult("failed", error="boom")
    assert failed.notice_title == "Server Companion learning data deletion not completed"
    assert "try again at the next start" in failed.notice_body and "boom" in failed.notice_body
