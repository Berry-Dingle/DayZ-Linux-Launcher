"""R7 Part B (F2): persistence off the GTK thread, incremental whole-file writes.

Every file lives under pytest's ``tmp_path``; no real profile is touched and
no GUI is launched.  Background-mode tests pump completions explicitly on the
test (owner) thread, exactly as ``GLib.idle_add`` does in production.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import test_companion_restart_compact_cutover as cutover
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_authority_consumers as c4
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4


KEYS = tuple(f"r7-bg-{index}.example:2302" for index in range(4))
P3 = 3 * 3600

# --------------------------------------------------------------------------
# Fixtures: a small store of real, pruned, established schema-4 records
# --------------------------------------------------------------------------

_RECORD_TEXT: dict[str, str] = {}


def _scenario_text(name: str) -> str:
    if name not in _RECORD_TEXT:
        pipeline = cutover.chain(14) if name == "chain" else cutover.sparse(10)
        record = cutover.drive(pipeline, every=7)
        _RECORD_TEXT[name] = schema4.canonical_json_bytes(record).decode("utf-8")
    return _RECORD_TEXT[name]


def _store(tmp_path, keys=KEYS, *, copies: int = 1):
    """Write a schema-4 file whose servers are real records under ``keys``."""

    active = tmp_path / "companion_restart_learning_phase2.json"
    legacy = tmp_path / "legacy.json"
    scratch = tmp_path / "fresh.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=scratch, legacy_path=tmp_path / "none.json", now=s8.DAY0
    )
    state = schema4.deserialize_schema4_bytes(scratch.read_bytes()).state
    scratch.unlink()
    names = ("chain", "sparse")
    all_keys = list(keys)
    for extra in range(1, copies):
        all_keys.extend(
            key.replace(".example:", f"-c{extra}.example:") for key in keys
        )
    for index, key in enumerate(all_keys):
        record = json.loads(_scenario_text(names[index % 2]).replace(s8.SERVER, key))
        state["servers"][key] = record
        state["schema3_snapshot"]["servers"][key] = copy.deepcopy(
            record["legacy_schema3_record"]
        )
    active.write_bytes(schema4.serialize_schema4_state(state))
    now = max(
        item["finalized_at"]
        for record in state["servers"].values()
        for item in record["physical_events"]
    ) + 3600
    return active, legacy, now


class Pump:
    """Owner-thread completion queue standing in for ``GLib.idle_add``."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        self.owner = threading.get_ident()
        self.posted_from = set()

    def post(self, callback):
        with self.lock:
            self.items.append(callback)
            self.posted_from.add(threading.get_ident())
        return 1

    def run(self):
        assert threading.get_ident() == self.owner
        with self.lock:
            items, self.items = self.items, []
        for callback in items:
            callback()
        return len(items)

    def drain(self, value, timeout=30.0):
        deadline = time.monotonic() + timeout
        while True:
            self.run()
            if not value.persistence_busy:
                self.run()
                if not value.persistence_busy:
                    return
            assert time.monotonic() < deadline, "persistence did not drain"
            time.sleep(0.002)


def _open_runtime(active, legacy, now, *, background=True):
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=now,
        authoritative_schema4_runtime_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
        schema4_authority_production_cutover_enabled=True,
    )
    pump = Pump()
    if background:
        assert value.enable_background_persistence(post_to_owner=pump.post)
    return value, pump


def _poll(value, key, generation, at, now, *, ok=True, players=12, queue=None):
    info = {"ok": ok}
    if ok:
        info.update(players=players, max_players=60, ping_ms=40)
        if queue is not None:
            info["queue"] = queue
    else:
        info["err"] = "timeout"
    return value.ingest_live_result(
        key, poll_generation=generation, info=info, wall_at=now + at, monotonic_at=at
    )


def _restart(value, key, generation, start, now, pump=None):
    session = value.begin_monitoring(
        key, wall_at=now + start, monotonic_at=start, poll_generation=generation
    )
    if not session and pump is not None:
        pump.drain(value)
        session = value.begin_monitoring(
            key, wall_at=now + start, monotonic_at=start, poll_generation=generation
        )
    assert session
    for offset, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        _poll(value, key, generation, start + offset, now, players=players)
    for offset in (90, 96, 102, 108, 114):
        _poll(value, key, generation, start + offset, now, ok=False)
    _poll(value, key, generation, start + 120, now, players=3, queue=2)
    update = value.tick(key, wall_at=now + start + 150, monotonic_at=start + 150)
    value.end_monitoring(
        key,
        marker=runtime.LifecycleMarker.PAUSE,
        wall_at=now + start + 160,
        monotonic_at=start + 160,
    )
    return update


def _strict(path):
    loaded = schema4.deserialize_schema4_bytes(
        path.read_bytes(), quarantine_invalid_servers=False
    )
    assert loaded.report.valid
    return loaded.state


def _gated_install(monkeypatch):
    """Make every atomic install wait until released (slow disk)."""

    gate = threading.Event()
    started = []
    real = live4._install_atomic_schema4

    def slow(*args, **kwargs):
        started.append(threading.get_ident())
        assert gate.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(live4, "_install_atomic_schema4", slow)
    return gate, started


def _eligible_warning(value, key, occurrence):
    """Force a schema-4 scheduled-warning opportunity (policy is not under test)."""

    value.decision(key, now=occurrence - 600)
    resolution = value._authority_consumer_resolutions[key]
    decision = resolution.selected_output
    assert isinstance(decision, c4.AuthorityConsumerDecision)
    assert decision.selected_regime_id
    warning = replace(
        decision,
        scheduled_pre_restart_alert_eligible=True,
        scheduled_warning_suppression_key=c4.AuthoritySuppressionKey(
            kind=c4.AuthoritySuppressionKind.SCHEDULED_WARNING,
            server_key=key,
            regime_id=decision.selected_regime_id,
            expected_occurrence_at=int(occurrence),
        ),
    )
    value._authority_consumer_resolutions[key] = replace(
        resolution, selected_output=warning
    )
    return warning.scheduled_warning_suppression_key


# --------------------------------------------------------------------------
# 1. Incremental canonical serialization is byte-identical
# --------------------------------------------------------------------------


def _pre_r7_serialize(state):
    loaded = schema4.deserialize_schema4_state(state, quarantine_invalid_servers=False)
    return schema4.canonical_json_bytes(schema4._deduplicate_schema3_snapshot(loaded.state))


def test_fragment_assembly_is_byte_identical_to_whole_document_encoding(tmp_path):
    active, _legacy, _now = _store(tmp_path)
    state = schema4.deserialize_schema4_bytes(active.read_bytes()).state
    assert schema4.serialize_schema4_state(state) == _pre_r7_serialize(state)
    assert schema4.serialize_schema4_state(state) == active.read_bytes()

    unicode_state = copy.deepcopy(state)
    record = unicode_state["servers"].pop(KEYS[0])
    snapshot = unicode_state["schema3_snapshot"]["servers"].pop(KEYS[0])
    odd = "ünïcødé-サーバー.example:2302"
    text = json.dumps(record).replace(KEYS[0], odd)
    unicode_state["servers"][odd] = json.loads(text)
    unicode_state["schema3_snapshot"]["servers"][odd] = json.loads(
        json.dumps(snapshot).replace(KEYS[0], odd)
    )
    assert schema4.serialize_schema4_state(unicode_state) == _pre_r7_serialize(unicode_state)

    empty = copy.deepcopy(state)
    empty["servers"] = {}
    empty["schema3_snapshot"]["servers"] = {}
    assert schema4.serialize_schema4_state(empty) == _pre_r7_serialize(empty)

    wrapped = copy.deepcopy(state)
    wrapped["servers"][KEYS[1]] = schema4.quarantine_wrapper(
        KEYS[1], {"broken": True}, [{"code": "x", "detail": "y"}], reason_codes=["t"]
    )
    assert schema4.serialize_schema4_state(wrapped) == _pre_r7_serialize(wrapped)


def test_layout_verification_rejects_any_deviation(tmp_path):
    active, _legacy, _now = _store(tmp_path)
    state = schema4.deserialize_schema4_bytes(active.read_bytes()).state
    disk = schema4.deduplicate_schema4_snapshot(state)
    fragments = {
        key: schema4.encode_schema4_server_fragment(record)
        for key, record in disk["servers"].items()
    }
    payload = schema4.assemble_schema4_payload(disk, fragments)
    schema4.verify_schema4_payload_layout(payload, disk, fragments)
    for broken in (payload[:-1], payload + b" ", payload.replace(b'"servers": {', b'"servers":  {', 1)):
        with pytest.raises(schema4.Schema4ValidationError):
            schema4.verify_schema4_payload_layout(broken, disk, fragments)
    with pytest.raises(schema4.Schema4ValidationError):
        schema4.validate_schema4_server_fragment(KEYS[0], b'{"server_key": 1}')


# --------------------------------------------------------------------------
# 2. Copy-on-write authoritative memory
# --------------------------------------------------------------------------


def test_installed_records_are_never_mutated_and_writes_match_full_serialization(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, _pump = _open_runtime(active, legacy, now, background=False)
    backend = value._authoritative_schema4_backend
    before = {
        key: (record, schema4.canonical_json_bytes(record))
        for key, record in backend._state["servers"].items()
    }
    _restart(value, KEYS[0], 1, 1000, now)
    for key in KEYS[1:]:
        record, encoded = before[key]
        assert backend._state["servers"][key] is record or key in backend._state["servers"]
        assert schema4.canonical_json_bytes(record) == encoded
    # The old object of the rebuilt server was never modified in place.
    old_record, old_bytes = before[KEYS[0]]
    assert schema4.canonical_json_bytes(old_record) == old_bytes
    assert backend._state["servers"][KEYS[0]] is not old_record
    # The incrementally written file is exactly the canonical serialization.
    on_disk = active.read_bytes()
    assert schema4.serialize_schema4_state(backend.state) == on_disk
    value.shutdown(wall_at=now + 9000, monotonic_at=9000)
    assert schema4.serialize_schema4_state(_strict(active)) == active.read_bytes()


def test_rebuild_does_not_mutate_the_existing_record(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, _pump = _open_runtime(active, legacy, now, background=False)
    backend = value._authoritative_schema4_backend
    existing = backend._state["servers"][KEYS[0]]
    encoded = schema4.canonical_json_bytes(existing)
    server = value._servers[KEYS[0]]
    schema4.rebuild_schema4_server_record(
        existing,
        runtime._serialize_server(server),
        server_key=KEYS[0],
        updated_at=now,
    )
    assert schema4.canonical_json_bytes(existing) == encoded
    value.shutdown(wall_at=now + 1, monotonic_at=1)


# --------------------------------------------------------------------------
# 3. Ordered, bounded, coalescing background writes
# --------------------------------------------------------------------------


def test_background_writes_are_ordered_coalesced_and_never_stale(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    backend = value._authoritative_schema4_backend
    pump.drain(value)  # fragment cache warm-up
    gate, started = _gated_install(monkeypatch)
    generation_before = backend._generation

    tickets = []
    for index, key in enumerate(KEYS):
        decision = value.decision(key, now=now + index)
        assert decision is not None
        record = dict(backend._state["servers"][key])
        record["reason_codes"] = sorted(set(record["reason_codes"]) | {f"r7-{index}"})
        backend.replace_server_record(key, record, reason=f"r7-order-{index}")
        tickets.append(backend.flush_async())
    # One write running, the rest coalesced into one waiting write.
    assert backend._worker.coalesced_requests >= len(KEYS) - 2
    assert all(not ticket.done for ticket in tickets)
    assert value.persistence_busy
    gate.set()
    pump.drain(value)
    assert all(ticket.done and ticket.error is None for ticket in tickets)
    writes = backend._generation - generation_before
    assert 1 <= writes <= 2
    assert all(ident != pump.owner for ident in started)
    state = _strict(active)
    for index, key in enumerate(KEYS):
        assert f"r7-{index}" in state["servers"][key]["reason_codes"]
    assert state["runtime_write_generation"] == backend._generation
    assert not backend.dirty
    value.shutdown(wall_at=now + 10, monotonic_at=10)


def test_owner_thread_never_encodes_validates_or_writes_in_background_mode(
    tmp_path, monkeypatch
):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    owner = threading.get_ident()
    calls: dict[str, set[int]] = {}

    def spy(module, name):
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            calls.setdefault(name, set()).add(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    for name in (
        "encode_schema4_server_fragment",
        "validate_schema4_server_fragment",
        "assemble_schema4_payload",
        "verify_schema4_payload_layout",
        "_install_atomic_schema4",
        "_sha256_file",
    ):
        spy(live4, name)
    monkeypatch.setattr(
        live4.copy,
        "deepcopy",
        _owner_guarded_deepcopy(owner, live4.copy.deepcopy, calls),
    )
    for index, key in enumerate(KEYS[:2]):
        _restart(value, key, 10 + index, 1000 + index * 10800, now, pump)
        pump.run()
    occurrence = now + 50_000
    _eligible_warning(value, KEYS[0], occurrence)
    fired = []
    value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(threading.get_ident()) or True,
    )
    pump.drain(value)
    assert fired == [owner]
    assert calls.get("_install_atomic_schema4")
    for name, threads in calls.items():
        if name == "deepcopy_whole_state":
            pytest.fail("whole-state deep copy on the owner thread")
        assert owner not in threads, name
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def _owner_guarded_deepcopy(owner, real, calls):
    def guarded(value, *args, **kwargs):
        if (
            threading.get_ident() == owner
            and isinstance(value, dict)
            and "servers" in value
            and "schema3_snapshot" in value
        ):
            calls.setdefault("deepcopy_whole_state", set()).add(owner)
        return real(value, *args, **kwargs)

    return guarded


# --------------------------------------------------------------------------
# 4. Alert durability and at-most-once ordering
# --------------------------------------------------------------------------


def test_warning_is_dispatched_only_after_its_reservation_is_durable(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    occurrence = now + 40_000
    key = _eligible_warning(value, KEYS[0], occurrence)
    gate, _started = _gated_install(monkeypatch)
    fired, results = [], []

    def action():
        # The reservation is on disk before the external action runs.
        loaded = schema4.deserialize_schema4_bytes(active.read_bytes())
        record = loaded.state["servers"][KEYS[0]]
        ledger = live4.consumer_ledger_from_mapping(record["consumer_state"], server_key=KEYS[0])
        assert live4.key_suppressed(
            ledger, key, event_finalized_at=live4._event_finalized_at(record)
        )
        fired.append(True)
        return True

    first = value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=action, on_result=results.append,
    )
    assert first.attempted and not first.emitted
    assert first.reason_codes[0] == "reservation_pending_durable_commit"
    # A repeat while the commit is in flight is suppressed in memory.
    repeat = value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 590,
        action=lambda: fired.append("duplicate") or True,
    )
    assert repeat.reason_codes[0] == "suppressed_already_fired"
    time.sleep(0.05)
    pump.run()
    assert fired == []  # never before the durable commit
    gate.set()
    pump.drain(value)
    assert fired == [True]
    assert results[-1].emitted
    assert results[-1].reason_codes[0] == "external_action_emitted_once"
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)

    again, _pump = _open_runtime(active, legacy, now + 1, background=False)
    _eligible_warning(again, KEYS[0], occurrence)
    replay = again.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 500,
        action=lambda: fired.append("replay") or True,
    )
    assert replay.reason_codes[0] == "suppressed_already_fired"
    assert fired == [True]
    record = _strict(active)["servers"][KEYS[0]]
    lifecycles = [item["lifecycle"] for item in record["consumer_state"]["alert_records"]]
    assert "emitted" in lifecycles
    again.shutdown(wall_at=now + 100_000, monotonic_at=100_000)


def test_failed_reservation_write_never_dispatches_and_stays_suppressed(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    statuses = []
    value.set_persistence_status_listener(
        lambda: statuses.append((threading.get_ident(), value.persistence_status))
    )
    occurrence = now + 60_000
    key = _eligible_warning(value, KEYS[1], occurrence)
    real_replace = live4.os.replace
    failures = []

    def failing(source, destination):
        if str(destination) == str(active) and not failures:
            failures.append(destination)
            raise OSError(errno.ENOSPC, "injected pre-commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(live4.os, "replace", failing)
    fired, results = [], []
    value.dispatch_authority_consumer_action(
        KEYS[1], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True, on_result=results.append,
    )
    pump.drain(value)
    assert fired == []
    assert results[-1].reason_codes[0] == "reservation_not_durable_action_not_dispatched"
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    assert value.pending_notice.kind == "persistence_write_failed"
    assert statuses and all(ident == pump.owner for ident, _ in statuses)
    # Still suppressed in memory: never dispatched later in this session.
    retry = value.dispatch_authority_consumer_action(
        KEYS[1], kind="scheduled_warning", now=occurrence - 590,
        action=lambda: fired.append("late") or True,
    )
    assert retry.reason_codes[0] == "suppressed_already_fired"
    # The next save point writes the retained reservation; status recovers.
    _restart(value, KEYS[2], 30, 2000, now, pump)
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert value.pending_notice is None
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    record = _strict(active)["servers"][KEYS[1]]
    ledger = live4.consumer_ledger_from_mapping(record["consumer_state"], server_key=KEYS[1])
    assert live4.key_suppressed(ledger, key, event_finalized_at=live4._event_finalized_at(record))
    assert fired == []


def test_shutdown_before_commit_never_dispatches_and_keeps_reservation(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    occurrence = now + 70_000
    key = _eligible_warning(value, KEYS[0], occurrence)
    gate, _started = _gated_install(monkeypatch)
    fired, results = [], []
    value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True, on_result=results.append,
    )
    threading.Timer(0.1, gate.set).start()
    value.shutdown(wall_at=now + 1000, monotonic_at=1000)
    assert fired == []
    assert results[-1].reason_codes[0] == "shutdown_before_dispatch"
    record = _strict(active)["servers"][KEYS[0]]
    ledger = live4.consumer_ledger_from_mapping(record["consumer_state"], server_key=KEYS[0])
    assert live4.key_suppressed(ledger, key, event_finalized_at=live4._event_finalized_at(record))


def test_alert_no_longer_wanted_is_not_dispatched(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    occurrence = now + 80_000
    _eligible_warning(value, KEYS[0], occurrence)
    fired, results = [], []
    value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True, on_result=results.append,
        still_wanted=lambda: False,
    )
    pump.drain(value)
    assert fired == []
    assert results[-1].reason_codes[0] == "external_action_no_longer_wanted_after_reservation"
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 5. Failures, recovery, shutdown and reopen
# --------------------------------------------------------------------------


def test_ambiguous_failure_stops_persistence_visibly_and_attempts_no_more_writes(
    tmp_path, monkeypatch
):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    notified = []
    value.set_persistence_status_listener(lambda: notified.append(value.persistence_status))
    backend._crash_injector = lambda point: (
        (_ for _ in ()).throw(RuntimeError("injected after replace"))
        if point is live4.Schema4CrashPoint.AFTER_REPLACE
        else None
    )
    assert value.begin_monitoring(KEYS[0], wall_at=now + 1, monotonic_at=1, poll_generation=1)
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert notified == [runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED]
    assert value.pending_notice.kind == "persistence_write_failed"
    installed = active.read_bytes()
    writes = backend.snapshot().write_count
    rejected = _poll(value, KEYS[0], 1, 10, now)
    assert rejected.rejected_reason == "learning_disabled"
    assert value.begin_monitoring(KEYS[1], wall_at=now + 2, monotonic_at=2, poll_generation=2) == ""
    value.shutdown(wall_at=now + 3, monotonic_at=3)
    assert active.read_bytes() == installed
    assert backend.snapshot().write_count == writes


def test_dead_worker_disables_persistence_instead_of_pretending(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    worker = backend._worker
    worker.stop(discard_pending=False)
    assert not worker.alive
    value.begin_monitoring(KEYS[0], wall_at=now + 1, monotonic_at=1, poll_generation=1)
    pump.run()
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert "persistence worker" in value.persistence_error
    value.shutdown(wall_at=now + 2, monotonic_at=2)


def test_shutdown_drains_pending_writes_and_rapid_reopen_succeeds(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    gate, _started = _gated_install(monkeypatch)
    _restart(value, KEYS[0], 1, 1000, now, None)
    _restart(value, KEYS[1], 2, 2000, now, None)
    assert value.persistence_busy
    threading.Timer(0.05, gate.set).start()
    value.shutdown(wall_at=now + 5000, monotonic_at=5000)
    state = _strict(active)
    assert len(state["servers"][KEYS[0]]["physical_events"]) > len(
        json.loads(_scenario_text("chain").replace(s8.SERVER, KEYS[0]))["physical_events"]
    ) - 1
    for cycle in range(3):
        again, again_pump = _open_runtime(active, legacy, now + 10 + cycle)
        _restart(again, KEYS[2], 10 + cycle, 10_000 + cycle * 10_800, now, again_pump)
        again.shutdown(wall_at=now + 50_000 + cycle, monotonic_at=50_000 + cycle)
    final = _strict(active)
    sessions = final["servers"][KEYS[2]]["legacy_schema3_record"]["monitoring_sessions"]
    assert sum(1 for item in sessions if item.get("reason") == "pause") >= 3


def test_quarantined_server_is_isolated_in_background_mode(tmp_path):
    active, legacy, now = _store(tmp_path)
    state = json.loads(active.read_text(encoding="utf-8"))
    state["servers"][KEYS[3]]["server_key"] = "elsewhere.example:2302"
    active.write_bytes(schema4.canonical_json_bytes(state))
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    assert value.begin_monitoring(KEYS[3], wall_at=now + 1, monotonic_at=1, poll_generation=1) == ""
    for index, key in enumerate(KEYS[:3]):
        _restart(value, key, 20 + index, 1000 + index * 10800, now, pump)
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert set(value.server_persistence_blocks) == {KEYS[3]}
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    loaded = schema4.deserialize_schema4_bytes(active.read_bytes())
    assert loaded.report.quarantined_server_keys == (KEYS[3],)


# --------------------------------------------------------------------------
# 6. Last-known-good rotation without copying
# --------------------------------------------------------------------------


def test_last_known_good_is_the_exact_previous_file_via_hard_link(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, _pump = _open_runtime(active, legacy, now, background=False)
    previous = active.read_bytes()
    assert value.begin_monitoring(KEYS[0], wall_at=now + 1, monotonic_at=1, poll_generation=1)
    lkg = value._authoritative_schema4_backend.last_known_good_path
    assert lkg.read_bytes() == previous
    assert os.stat(lkg).st_ino != os.stat(active).st_ino
    assert not list(tmp_path.glob(".*.link-*.tmp"))
    value.shutdown(wall_at=now + 2, monotonic_at=2)


def test_last_known_good_falls_back_to_copy_without_hard_links(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path)
    value, _pump = _open_runtime(active, legacy, now, background=False)
    previous = active.read_bytes()

    def no_links(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(live4.os, "link", no_links)
    assert value.begin_monitoring(KEYS[0], wall_at=now + 1, monotonic_at=1, poll_generation=1)
    assert value._authoritative_schema4_backend.last_known_good_path.read_bytes() == previous
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=now + 2, monotonic_at=2)


def test_external_modification_is_still_a_generation_conflict(tmp_path):
    active, legacy, now = _store(tmp_path)
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    active.write_bytes(active.read_bytes() + b" ")
    value.begin_monitoring(KEYS[0], wall_at=now + 1, monotonic_at=1, poll_generation=1)
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert "Schema4GenerationConflict" in value.persistence_error
    assert not list(tmp_path.glob(".*.link-*.tmp"))
    value.shutdown(wall_at=now + 2, monotonic_at=2)


def test_stale_link_temporaries_are_removed_at_open(tmp_path):
    active, legacy, now = _store(tmp_path)
    stale = tmp_path / f".{active.name}.last-known-good.link-1-dead.tmp"
    stale.write_bytes(b"partial")
    value, _pump = _open_runtime(active, legacy, now, background=False)
    assert not stale.exists()
    value.shutdown(wall_at=now + 1, monotonic_at=1)


# --------------------------------------------------------------------------
# 7. Export, import and large databases
# --------------------------------------------------------------------------


def test_export_runs_on_the_worker_and_large_databases_round_trip(tmp_path, monkeypatch):
    active, legacy, now = _store(tmp_path, copies=16)
    assert active.stat().st_size > 8 * 1024 * 1024
    value, pump = _open_runtime(active, legacy, now)
    pump.drain(value)
    _restart(value, KEYS[0], 1, 1000, now, pump)
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    threads = []
    real = backend._export_from
    monkeypatch.setattr(
        backend, "_export_from", lambda root: (threads.append(threading.get_ident()), real(root))[1]
    )
    received = []
    value.export_authoritative_schema4_snapshot_async(
        lambda snapshot, error: received.append((snapshot, error, threading.get_ident()))
    )
    assert received == []
    pump.drain(value)
    deadline = time.monotonic() + 30
    while not received:
        pump.run()
        assert time.monotonic() < deadline
        time.sleep(0.002)
    snapshot, error, ident = received[0]
    assert error is None and ident == pump.owner
    assert threads and pump.owner not in threads
    assert snapshot.canonical_bytes == schema4.serialize_schema4_state(backend.state)
    validated = transfer.validate_external_learning_bytes(snapshot.canonical_bytes)
    assert validated.canonical_bytes == snapshot.canonical_bytes
    assert validated.server_count == len(backend.state["servers"])
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 8. Window and transfer UI wiring (no GUI)
# --------------------------------------------------------------------------


def test_window_enables_background_persistence_through_glib_idle(monkeypatch):
    from dzll_launcher import window as window_module

    calls = []
    fake_runtime = SimpleNamespace(
        set_persistence_status_listener=lambda listener: calls.append(("listener", listener)),
        enable_background_persistence=lambda post_to_owner: calls.append(("post", post_to_owner)),
    )
    host = SimpleNamespace(_companion_restart_phase2=fake_runtime)
    window_module.DZLLWindow._enable_companion_background_persistence(host)
    assert ("post", window_module.GLib.idle_add) in calls
    assert any(name == "listener" for name, _ in calls)


def test_window_installs_loaded_runtime_and_resumes_learning(monkeypatch):
    from dzll_launcher import window as window_module

    W = window_module.DZLLWindow
    placeholder = runtime.Phase2RestartRuntime.loading_placeholder(active_path="/nonexistent/x.json")
    assert placeholder.loading and not placeholder.persistence_enabled
    assert placeholder.pending_notice is None
    started, refreshed, notices = [], [], []
    loaded = SimpleNamespace(
        pending_notice=runtime.RuntimeNotice(kind="legacy_reset", title="t", body="b"),
        set_persistence_status_listener=lambda listener: None,
        enable_background_persistence=lambda post_to_owner: True,
    )
    coordinator = SimpleNamespace(
        pending_notice=None,
        notice_showing=False,
        try_show_notice=lambda: notices.append(True),
    )
    host = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _companion_restart_phase2=placeholder,
        _startup_presentation=coordinator,
        _server_companion_poll_timer_id=7,
        _server_companion_should_poll=lambda: True,
        _server_companion_restart_learning_key=lambda: "a.example:2302",
        _record_server_companion_monitor_started=lambda key=None: started.append(key),
        _refresh_server_companion_restart_learning_summary=lambda: refreshed.append(True),
        _server_companion_import_startup_notice=lambda: None,
        _debug_server_companion_alert=lambda message: None,
        _companion_learning_load_lock=threading.Lock(),
        _companion_learning_load_result=(None, loaded),
        _companion_learning_load_installed=False,
    )
    assert W._install_companion_restart_learning(host) is False
    assert host._companion_restart_phase2 is loaded
    assert started == ["a.example:2302"] and refreshed == [True]
    assert coordinator.pending_notice is loaded.pending_notice and notices == [True]
    # A second (stale) install is a no-op.
    host._companion_restart_phase2 = "sentinel"
    W._install_companion_restart_learning(host)
    assert host._companion_restart_phase2 == "sentinel"


def test_window_shutdown_closes_a_loaded_but_uninstalled_runtime(tmp_path):
    from dzll_launcher import window as window_module

    active, legacy, now = _store(tmp_path)
    loaded, _pump = _open_runtime(active, legacy, now, background=False)
    host = SimpleNamespace(
        _companion_learning_load_lock=threading.Lock(),
        _companion_learning_load_result=(None, loaded),
        _companion_learning_load_installed=False,
        _companion_learning_load_thread=None,
    )
    window_module.DZLLWindow._finish_companion_restart_learning_load_for_shutdown(host)
    # The writer lock was released: the store opens again at once.
    again, _ = _open_runtime(active, legacy, now, background=False)
    again.shutdown(wall_at=now + 1, monotonic_at=1)


def test_window_startup_loads_learning_off_the_gtk_thread():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/dzll_launcher/window.py"
    ).read_text(encoding="utf-8")
    init = source.split("class DZLLWindow", 1)[1].split("self._pending_last_played_obj", 1)[0]
    assert "Phase2RestartRuntime.loading_placeholder(" in init
    assert "_start_companion_restart_learning_load(" in init
    loader = init.split("def load_phase2_learning_runtime", 1)[1]
    assert loader.index("apply_pending_import_at_startup(") < loader.index(
        "_initialize_companion_restart_runtime_for_window("
    )


def test_transfer_ui_completes_background_work_on_the_gtk_thread(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from dzll_launcher import companion_learning_transfer_ui as ui

    idle = []
    monkeypatch.setattr(ui.GLib, "idle_add", lambda callback, *args: idle.append(callback) or 1)
    executor = ThreadPoolExecutor(max_workers=1)
    win = SimpleNamespace(_shutdown_cleanup_done=False, _hi_executor=executor)
    controller = ui.CompanionLearningTransferController(win)
    owner = threading.get_ident()
    seen = []
    release = threading.Event()

    def work():
        assert release.wait(5)
        return threading.get_ident()

    controller._run_off_gtk_thread(
        work,
        lambda result, error: seen.append((result, error, threading.get_ident())),
    )
    release.set()
    executor.shutdown(wait=True)
    deadline = time.monotonic() + 5
    while not idle:
        assert time.monotonic() < deadline
        time.sleep(0.002)
    assert seen == []
    idle[0]()
    result, error, ident = seen[0]
    assert error is None and result != owner and ident == owner
