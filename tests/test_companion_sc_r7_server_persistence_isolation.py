"""R7 Part A (F3): one unwritable server never disables persistence globally."""

from __future__ import annotations

import json
import logging

import pytest

from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4


BASE = 1_960_000_000.0
A = "isolation-a.example:2302"
B = "isolation-b.example:2302"
C = "isolation-c.example:2302"


def _open(active, legacy, now):
    return runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=now,
        generation_id="77777777-7777-4777-8777-777777777777",
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
        key,
        poll_generation=generation,
        info=info,
        wall_at=BASE + at,
        monotonic_at=at,
    )


def _restart(value, key, generation, start):
    """One conventional, fully observed restart (one finalized event)."""

    assert value.begin_monitoring(
        key, wall_at=BASE + start, monotonic_at=start, poll_generation=generation
    )
    for offset, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        _poll(value, key, generation, start + offset, players=players)
    for offset in (90, 96, 102, 108, 114):
        _poll(value, key, generation, start + offset, ok=False)
    _poll(value, key, generation, start + 120, players=3, queue=2)
    update = value.tick(key, wall_at=BASE + start + 150, monotonic_at=start + 150)
    return update


def _end(value, key, at):
    return value.end_monitoring(
        key,
        marker=runtime.LifecycleMarker.PAUSE,
        wall_at=BASE + at,
        monotonic_at=at,
    )


def _strict(path):
    return schema4.deserialize_schema4_bytes(
        path.read_bytes(), quarantine_invalid_servers=False
    )


def _event_count(state, key):
    return len(state["servers"][key]["physical_events"])


def _three_server_store(tmp_path):
    active = tmp_path / "companion_restart_learning_phase2.json"
    legacy = tmp_path / "legacy.json"
    value = _open(active, legacy, BASE)
    generation = 1
    for index, key in enumerate((A, B, C)):
        _restart(value, key, generation, 1000 * index)
        _end(value, key, 1000 * index + 200)
        generation += 1
    value.shutdown(wall_at=BASE + 5000, monotonic_at=5000)
    loaded = _strict(active)
    assert loaded.report.valid
    assert all(_event_count(loaded.state, key) == 1 for key in (A, B, C))
    return active, legacy


def _quarantine_on_disk(active, key):
    state = json.loads(active.read_text(encoding="utf-8"))
    record = state["servers"][key]
    record["server_key"] = "someone-else.example:2302"
    payload = schema4.canonical_json_bytes(state)
    active.write_bytes(payload)
    return state


def test_quarantined_server_is_isolated_and_healthy_servers_keep_persisting(
    tmp_path, monkeypatch, caplog
):
    active, legacy = _three_server_store(tmp_path)
    _quarantine_on_disk(active, C)
    value = _open(active, legacy, BASE + 10_000)
    backend = value._authoritative_schema4_backend
    assert backend.snapshot().quarantined_server_keys == (C,)
    quarantined_wrapper = json.dumps(
        backend.state["servers"][C], sort_keys=True
    )

    rebuild_calls = []
    real_update = backend.update_server_from_schema3

    def counting_update(key, *args, **kwargs):
        rebuild_calls.append(key)
        return real_update(key, *args, **kwargs)

    monkeypatch.setattr(backend, "update_server_from_schema3", counting_update)

    with caplog.at_level(logging.WARNING, logger=runtime.logger.name):
        # Monitoring the quarantined server is refused without any write.
        assert value.begin_monitoring(
            C, wall_at=BASE + 10_001, monotonic_at=10_001, poll_generation=10
        ) == ""
        assert value.ensure_monitoring_session(
            C, wall_at=BASE + 10_002, monotonic_at=10_002, poll_generation=10
        ) == ("", False)
        refused = _poll(value, C, 10, 10_003)
    assert not refused.accepted
    assert refused.rejected_reason == "server_persistence_blocked"
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert value.persistence_enabled
    assert value.pending_notice is None
    assert C not in rebuild_calls
    block = value.server_persistence_blocks[C]
    assert block.reason_code == "server_quarantined"
    assert "server_key_mismatch" in block.validation_codes
    warnings = [item for item in caplog.records if C in item.getMessage()]
    assert len(warnings) == 1
    assert "server_quarantined" in warnings[0].getMessage()
    assert "server_key_mismatch" in warnings[0].getMessage()

    # Healthy servers keep recording, flushing and surviving a reopen.
    writes_before = backend.snapshot().write_count
    _restart(value, A, 11, 20_000)
    _end(value, A, 20_200)
    _restart(value, B, 12, 30_000)
    _end(value, B, 30_200)
    # Repeated attempts on the quarantined server stay refused and unwritten.
    for attempt in range(3):
        assert value.begin_monitoring(
            C,
            wall_at=BASE + 40_000 + attempt,
            monotonic_at=40_000 + attempt,
            poll_generation=13,
        ) == ""
    assert C not in rebuild_calls
    assert backend.snapshot().write_count > writes_before
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=BASE + 50_000, monotonic_at=50_000)

    loaded = schema4.deserialize_schema4_bytes(active.read_bytes())
    assert loaded.report.quarantined_server_keys == (C,)
    assert _event_count(loaded.state, A) == 2
    assert _event_count(loaded.state, B) == 2
    # The quarantined record is preserved, never rebuilt or reset.
    assert json.dumps(loaded.state["servers"][C], sort_keys=True) == quarantined_wrapper

    again = _open(active, legacy, BASE + 60_000)
    assert again.persistence_enabled
    assert again.begin_monitoring(
        A, wall_at=BASE + 60_001, monotonic_at=60_001, poll_generation=20
    )
    assert again.begin_monitoring(
        C, wall_at=BASE + 60_002, monotonic_at=60_002, poll_generation=21
    ) == ""
    again.shutdown(wall_at=BASE + 60_010, monotonic_at=60_010)
    final = schema4.deserialize_schema4_bytes(active.read_bytes())
    assert final.report.quarantined_server_keys == (C,)
    assert json.dumps(final.state["servers"][C], sort_keys=True) == quarantined_wrapper


def test_server_rebuild_rejection_blocks_only_that_server(tmp_path, monkeypatch):
    active, legacy = _three_server_store(tmp_path)
    value = _open(active, legacy, BASE + 10_000)
    backend = value._authoritative_schema4_backend
    stored_b = json.dumps(backend.state["servers"][B], sort_keys=True)
    real_rebuild = live4.rebuild_schema4_server_record
    calls = []

    def rejecting_rebuild(existing, record, *, server_key, updated_at):
        calls.append(server_key)
        if server_key == B:
            raise schema4.Schema4ValidationError(
                "broken_regime_evidence_reference: injected"
            )
        return real_rebuild(
            existing, record, server_key=server_key, updated_at=updated_at
        )

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", rejecting_rebuild)

    assert value.begin_monitoring(
        B, wall_at=BASE + 10_001, monotonic_at=10_001, poll_generation=30
    ) == ""
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    block = value.server_persistence_blocks[B]
    assert block.reason_code == "server_rebuild_rejected"
    assert "broken_regime_evidence_reference" in block.detail
    b_attempts = calls.count(B)
    assert b_attempts == 1

    _restart(value, A, 31, 20_000)
    _end(value, A, 20_200)
    for attempt in range(3):
        assert value.begin_monitoring(
            B,
            wall_at=BASE + 21_000 + attempt,
            monotonic_at=21_000 + attempt,
            poll_generation=32,
        ) == ""
    assert calls.count(B) == b_attempts
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=BASE + 30_000, monotonic_at=30_000)

    loaded = _strict(active)
    assert loaded.report.valid
    assert _event_count(loaded.state, A) == 2
    assert json.dumps(loaded.state["servers"][B], sort_keys=True) == stored_b


def test_mid_session_record_validation_rejection_is_isolated(tmp_path, monkeypatch):
    active, legacy = _three_server_store(tmp_path)
    value = _open(active, legacy, BASE + 10_000)
    backend = value._authoritative_schema4_backend
    assert value.begin_monitoring(
        B, wall_at=BASE + 10_001, monotonic_at=10_001, poll_generation=40
    )
    real_rebuild = live4.rebuild_schema4_server_record

    def invalid_rebuild(existing, record, *, server_key, updated_at):
        rebuilt = real_rebuild(
            existing, record, server_key=server_key, updated_at=updated_at
        )
        if server_key == B:
            rebuilt = dict(rebuilt)
            rebuilt["physical_events"] = "not-a-list"
        return rebuilt

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", invalid_rebuild)
    for offset in (0, 10, 20):
        _poll(value, B, 40, 10_010 + offset)
    for offset in (40, 46, 52, 58):
        _poll(value, B, 40, 10_010 + offset, ok=False)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    block = value.server_persistence_blocks[B]
    assert block.reason_code == "server_record_validation_failed"
    assert "missing_collection" in block.validation_codes
    stored_b = json.dumps(backend.state["servers"][B], sort_keys=True)
    later = _poll(value, B, 40, 10_100)
    assert later.rejected_reason == "server_persistence_blocked"

    _restart(value, A, 41, 20_000)
    _end(value, A, 20_200)
    value.shutdown(wall_at=BASE + 30_000, monotonic_at=30_000)
    loaded = _strict(active)
    assert loaded.report.valid
    assert _event_count(loaded.state, A) == 2
    assert json.dumps(loaded.state["servers"][B], sort_keys=True) == stored_b


def test_global_failures_remain_global(tmp_path, monkeypatch):
    active, legacy = _three_server_store(tmp_path)
    value = _open(active, legacy, BASE + 10_000)
    backend = value._authoritative_schema4_backend

    def failing_install(*_args, **_kwargs):
        raise live4.Schema4WriteFailure(
            live4.Schema4WriteFailurePhase.POST_COMMIT_OR_AMBIGUOUS,
            OSError("injected I/O failure"),
        )

    monkeypatch.setattr(live4, "_install_atomic_schema4", failing_install)
    assert value.begin_monitoring(
        A, wall_at=BASE + 10_001, monotonic_at=10_001, poll_generation=50
    ) == ""
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    )
    assert value.server_persistence_blocks == {}
    assert value.begin_monitoring(
        B, wall_at=BASE + 10_002, monotonic_at=10_002, poll_generation=51
    ) == ""
    backend.close(flush=False)


def test_backend_rejections_name_the_server(tmp_path):
    active, legacy = _three_server_store(tmp_path)
    _quarantine_on_disk(active, C)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        assert backend.server_write_block(A) is None
        assert backend.server_write_block("unknown.example:2302") is None
        reason, codes = backend.server_write_block(C)
        assert reason == "server_quarantined"
        assert "server_key_mismatch" in codes
        with pytest.raises(live4.Schema4ServerWriteRejected) as raised:
            backend.update_server_from_schema3(
                C, {}, updated_at=BASE, durable_reason="test"
            )
        assert raised.value.server_key == C
        assert raised.value.reason_code == "server_quarantined"
        # Still a validation error for existing callers.
        assert isinstance(raised.value, schema4.Schema4ValidationError)
        assert not backend.dirty
    finally:
        backend.close(flush=False)
