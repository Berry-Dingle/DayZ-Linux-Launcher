"""R8a: persistence worker liveness and non-blocking queue saturation.

Every file lives under pytest's ``tmp_path``; no real profile is touched and
no GUI is launched.  Background-mode tests pump completions explicitly on the
test (owner) thread, exactly as ``GLib.idle_add`` does in production.
Liveness limits are shortened per test; production limits are far longer.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import test_companion_sc_r7_background_persistence as r7
import test_companion_sc_r8_background_learning_rebuild as r8
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher.window import DZLLWindow


KEYS = r7.KEYS
STORE_SIZES = {"1": 1, "12": 3, "52": 13}  # copies of the 4-key store


def _fast_liveness(monkeypatch, *, stall=0.6, slow=0.3, shutdown=0.6, max_task=30.0):
    monkeypatch.setattr(live4, "WORKER_LIVENESS_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(live4, "WORKER_STALL_SECONDS", stall)
    monkeypatch.setattr(live4, "WORKER_SLOW_SECONDS", slow)
    monkeypatch.setattr(live4, "WORKER_SHUTDOWN_STALL_SECONDS", shutdown)
    monkeypatch.setattr(live4, "WORKER_MAX_TASK_SECONDS", max_task)
    monkeypatch.setattr(live4, "WORKER_STOP_AFTER_STALL_SECONDS", 0.2)


def _store(tmp_path, size="12"):
    if size == "1":
        return r7._store(tmp_path, keys=KEYS[:1])
    return r7._store(tmp_path, copies=STORE_SIZES[size])


def _open(tmp_path, size="12"):
    active, legacy, now = _store(tmp_path, size)
    value, pump = r8._open(active, legacy, now, "r8")
    pump.drain(value)
    return active, legacy, now, value, pump


class Blocker:
    """Hold one worker operation without any progress evidence (a hung disk)."""

    def __init__(self, monkeypatch, module, name, *, predicate=lambda *a, **k: True):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads = []
        real = getattr(module, name)

        def blocked(*args, **kwargs):
            if predicate(*args, **kwargs) and not self.release.is_set():
                self.threads.append(threading.get_ident())
                self.entered.set()
                self.release.wait(60)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, blocked)


def _pump_until(pump, condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not condition():
        pump.run()
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)
    pump.run()


def _timed(callbacks, fn, *args, **kwargs):
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        callbacks.append((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------
# 1. Slow but progressing work is never treated as a failure
# --------------------------------------------------------------------------


def test_slow_cpu_bound_rebuild_is_reported_slow_not_stalled(tmp_path, monkeypatch):
    _fast_liveness(monkeypatch)
    active, legacy, now, value, pump = _open(tmp_path, "1")
    real = live4.rebuild_schema4_server_record
    seen = []

    def slow_rebuild(*args, **kwargs):
        until = time.monotonic() + 1.5  # 2.5x the stall limit, but computing
        while time.monotonic() < until:
            sum(range(2000))
            seen.append(value.persistence_worker_liveness()["state"])
        return real(*args, **kwargs)

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", slow_rebuild)
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    pump.drain(value)
    assert "slow" in seen
    assert "stalled" not in seen
    backend = value._authoritative_schema4_backend
    assert backend.worker_stalled is None
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    sessions = backend._state["servers"][KEYS[0]]["legacy_schema3_record"]["monitoring_sessions"]
    assert sessions[-1]["ended_at"] is None  # the save point was installed
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)


def test_slow_disk_with_stage_progress_is_not_stalled(tmp_path, monkeypatch):
    _fast_liveness(monkeypatch)
    active, legacy, now, value, pump = _open(tmp_path, "1")
    backend = value._authoritative_schema4_backend
    real = live4._install_atomic_schema4

    def slow_disk(*args, **kwargs):
        # 1.6 s in total (well past the 0.6 s stall limit) but each stage
        # boundary arrives within it, as on a slow but working SSD.
        for _ in range(4):
            time.sleep(0.4)
            backend._worker.note_progress("slow_disk_stage")
        return real(*args, **kwargs)

    monkeypatch.setattr(live4, "_install_atomic_schema4", slow_disk)
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    pump.drain(value)
    assert backend.worker_stalled is None
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert not backend.dirty
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)


def test_a_hung_task_below_the_limit_completes_normally(tmp_path, monkeypatch):
    _fast_liveness(monkeypatch, stall=2.0)
    active, legacy, now, value, pump = _open(tmp_path, "1")
    blocker = Blocker(monkeypatch, live4, "_install_atomic_schema4")
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    _pump_until(pump, blocker.entered.is_set)
    threading.Timer(0.5, blocker.release.set).start()
    pump.drain(value)
    assert value._authoritative_schema4_backend.worker_stalled is None
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)


# --------------------------------------------------------------------------
# 2. A genuinely stalled worker is detected, reported and contained
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", sorted(STORE_SIZES))
def test_stalled_write_is_declared_and_never_dispatches_an_unreserved_alert(
    tmp_path, monkeypatch, size
):
    _fast_liveness(monkeypatch)
    active, legacy, now, value, pump = _open(tmp_path, size)
    backend = value._authoritative_schema4_backend
    before_bytes = active.read_bytes()
    statuses = []
    value.set_persistence_status_listener(
        lambda: statuses.append((threading.get_ident(), value.persistence_status))
    )
    occurrence = now + 40_000
    r7._eligible_warning(value, KEYS[0], occurrence)
    blocker = Blocker(monkeypatch, live4, "_install_atomic_schema4")
    fired, results, callbacks = [], [], []
    first = _timed(
        callbacks,
        value.dispatch_authority_consumer_action,
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True, on_result=results.append,
    )
    assert first.reason_codes[0] == "reservation_pending_durable_commit"
    assert blocker.entered.wait(10)
    _pump_until(pump, lambda: backend.worker_stalled is not None)
    # Reported, not hidden: disabled with a visible notice, on the owner thread.
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert "Schema4WorkerStalled" in value.persistence_error
    assert value.pending_notice.kind == "persistence_write_failed"
    assert statuses and all(ident == pump.owner for ident, _ in statuses)
    liveness = value.persistence_worker_liveness()
    assert liveness["declared_stalled"] and liveness["state"] == "stalled"
    # The reservation was never committed: never dispatched.
    assert fired == []
    assert results[-1].reason_codes[0] == "reservation_not_durable_action_not_dispatched"
    later = _timed(
        callbacks,
        value.dispatch_authority_consumer_action,
        KEYS[0], kind="scheduled_warning", now=occurrence - 500,
        action=lambda: fired.append("late") or True,
    )
    assert later.reason_codes[0] == "persistence_worker_stalled_action_not_dispatched"
    # No second writer: learning intake stops, nothing new is submitted.
    rejected = _timed(callbacks, r7._poll, value, KEYS[1], 1, 10, now)
    assert rejected.rejected_reason == "learning_disabled"
    assert _timed(
        callbacks,
        value.begin_monitoring,
        KEYS[1], wall_at=now + 11, monotonic_at=11, poll_generation=2,
    ) == ""
    assert not value.persistence_busy
    assert max(callbacks) < 100  # owner-thread calls never wait on the worker
    # Shutdown is bounded and keeps the database locked while the stalled
    # write may still be running.
    worker = backend._worker
    started = time.monotonic()
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)
    assert time.monotonic() - started < 5
    assert active.read_bytes() == before_bytes
    with pytest.raises(live4.Schema4WriterLockError):
        live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    # The stalled write finishes later: atomic, valid, and never dispatched.
    blocker.release.set()
    deadline = time.monotonic() + 20
    while worker._thread.is_alive():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    r7._strict(active)
    assert fired == []
    backend._writer_lock.release()
    again, again_pump = r8._open(active, legacy, now + 1, "r8")
    again_pump.drain(again)
    assert again.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    again.shutdown(wall_at=now + 10_000, monotonic_at=10_000)


def test_stalled_rebuild_is_abandoned_and_its_late_result_ignored(tmp_path, monkeypatch):
    _fast_liveness(monkeypatch)
    active, legacy, now, value, pump = _open(tmp_path, "12")
    backend = value._authoritative_schema4_backend
    installed = backend._state["servers"][KEYS[0]]
    blocker = Blocker(monkeypatch, live4, "rebuild_schema4_server_record")
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    assert blocker.entered.wait(10)
    window_work = []
    assert value.when_authority_current(KEYS[0], lambda: window_work.append(1)) is False
    _pump_until(pump, lambda: backend.worker_stalled is not None)
    # Ordered work behind the stalled rebuild is released (not stuck forever)
    # and the server is current again; learning is visibly not saved.
    assert window_work == [1]
    assert not value.authority_work_pending(KEYS[0])
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    blocker.release.set()
    deadline = time.monotonic() + 20
    while backend.worker_liveness()["late_completions_after_stall"] < 1:
        pump.run()
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert backend._state["servers"][KEYS[0]] is installed  # never installed
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)
    backend._writer_lock.release()


def test_shutdown_during_a_hang_uses_the_shorter_limit(tmp_path, monkeypatch):
    # Normal limit far away: only shutdown's no-progress limit can fire.
    _fast_liveness(monkeypatch, stall=60.0, slow=30.0, shutdown=0.6)
    active, legacy, now, value, pump = _open(tmp_path, "12")
    blocker = Blocker(monkeypatch, live4, "_install_atomic_schema4")
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    assert update.finalized_events
    _pump_until(pump, blocker.entered.is_set)
    worker = value._authoritative_schema4_backend._worker
    started = time.monotonic()
    value.shutdown(wall_at=now + 5000, monotonic_at=5000)
    elapsed = time.monotonic() - started
    assert elapsed < 6
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    backend = value._authoritative_schema4_backend
    assert backend.worker_stalled is not None
    with pytest.raises(live4.Schema4WriterLockError):
        live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    blocker.release.set()
    deadline = time.monotonic() + 20
    while worker._thread.is_alive():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    r7._strict(active)  # whatever completed is atomic and valid
    backend._writer_lock.release()


def test_runaway_task_is_stalled_by_the_absolute_limit(tmp_path, monkeypatch):
    _fast_liveness(monkeypatch, stall=60.0, slow=0.2, max_task=0.8)
    active, legacy, now, value, pump = _open(tmp_path, "1")
    backend = value._authoritative_schema4_backend
    stop = threading.Event()
    real = live4.rebuild_schema4_server_record

    def runaway(*args, **kwargs):
        while not stop.is_set():
            sum(range(2000))  # keeps consuming CPU: "progress" by CPU alone
        return real(*args, **kwargs)

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", runaway)
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    _pump_until(pump, lambda: backend.worker_stalled is not None)
    stop.set()
    value.shutdown(wall_at=now + 9_999, monotonic_at=9_999)
    if backend._writer_lock is not None:
        try:
            backend._writer_lock.release()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 3. Queue saturation pauses intake instead of waiting
# --------------------------------------------------------------------------


def test_saturation_pauses_intake_without_waiting_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_PAUSE_OPS", 12)
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_RESUME_OPS", 3)
    monkeypatch.setattr(runtime, "MAX_AUTHORITY_QUEUE_OPS", 24)
    active, legacy, now, value, pump = _open(tmp_path, "12")
    blocker = Blocker(monkeypatch, live4, "rebuild_schema4_server_record")
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    assert blocker.entered.wait(10)
    callbacks, reasons, refused = [], [], []
    at = 200
    for index in range(80):
        at += 10
        update = _timed(callbacks, driver.poll, at, players=12 if index % 3 else 0)
        reasons.append(update.rejected_reason)
        _timed(callbacks, value.decision, KEYS[0], now=now + at)
        try:
            _timed(callbacks, value.when_authority_current, KEYS[0], lambda: None)
        except runtime.AuthorityBacklogFull:
            refused.append(index)
    assert "learning_intake_paused" in reasons
    assert value.authority_intake_paused(KEYS[0])
    stats = value.authority_queue_stats()
    assert stats["intake_pauses"] >= 1
    assert stats["hard_bound_exceeded"] == 0
    assert stats["max_queue_depth"] <= 12 + 6  # pause mark + bounded mandatory work
    assert stats["coalesced_evaluations"] > 0
    assert refused and stats["external_refused"] == len(refused)
    assert max(callbacks) < 100  # never waited for the stuck worker
    # Mandatory work is still accepted while paused: the session end.
    driver.stop(at + 5)
    # A new session is refused while paused, and marked for re-ensure.
    assert value.begin_monitoring(
        KEYS[0], wall_at=now + at + 6, monotonic_at=at + 6, poll_generation=99
    ) == ""
    assert value.monitoring_session_pending_durability(KEYS[0])
    blocker.release.set()
    pump.drain(value)
    assert not value.authority_intake_paused(KEYS[0])
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert value.begin_monitoring(
        KEYS[0], wall_at=now + at + 50, monotonic_at=at + 50, poll_generation=100
    )
    assert not value.monitoring_session_pending_durability(KEYS[0])
    pump.drain(value)
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    disk = r7._strict(active)["servers"][KEYS[0]]
    sessions = disk["legacy_schema3_record"]["monitoring_sessions"]
    assert any(item.get("reason") == "pause" for item in sessions)


def test_switch_while_paused_saves_old_server_and_learns_on_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_PAUSE_OPS", 10)
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_RESUME_OPS", 2)
    active, legacy, now, value, pump = _open(tmp_path, "12")
    blocker = Blocker(
        monkeypatch,
        live4,
        "rebuild_schema4_server_record",
        predicate=lambda *a, **k: k.get("server_key") == KEYS[0],
    )
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    assert blocker.entered.wait(10)
    for index in range(40):
        driver.poll(2000 + index * 10)
    assert value.authority_intake_paused(KEYS[0])
    driver.select(KEYS[1], 3000)  # end on A is queued; B is independent
    assert not value.authority_intake_paused(KEYS[1])
    for offset in range(0, 100, 10):
        assert driver.poll(3010 + offset).accepted
    blocker.release.set()
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    disk = r7._strict(active)["servers"]
    if update.finalized_events:
        assert update.finalized_events[0].event_id in {
            item["event_id"] for item in disk[KEYS[0]]["physical_events"]
        }
    assert disk[KEYS[0]]["legacy_schema3_record"]["monitoring_sessions"][-1]["reason"] == "server_switch"


def test_pending_warning_reservation_survives_saturation_and_dispatches_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_PAUSE_OPS", 10)
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_RESUME_OPS", 2)
    active, legacy, now, value, pump = _open(tmp_path, "12")
    occurrence = now + 30_000
    r7._eligible_warning(value, KEYS[0], occurrence)
    gate = threading.Event()
    real = live4._install_atomic_schema4
    monkeypatch.setattr(
        live4,
        "_install_atomic_schema4",
        lambda *a, **k: (gate.wait(30), real(*a, **k))[1],
    )
    fired, results = [], []
    value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True, on_result=results.append,
    )
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    for index in range(40):
        driver.poll(200 + index * 10)
    assert value.authority_intake_paused(KEYS[0])
    assert fired == []
    gate.set()
    pump.drain(value)
    assert fired == [True]
    assert results[-1].reason_codes[0] == "external_action_emitted_once"
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_intake_pause_is_per_active_server_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_PAUSE_OPS", 6)
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_RESUME_OPS", 1)
    active, legacy, now, value, pump = _open(tmp_path, "1")
    driver = r8.Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    for _index in range(10):
        try:
            value.when_authority_current(KEYS[0], lambda: None)
        except runtime.AuthorityBacklogFull:
            pass  # refused once paused
    assert value.authority_intake_paused(KEYS[0])
    assert value.authority_queue_stats()["external_refused"] >= 1
    assert driver.poll(300).rejected_reason == "learning_intake_paused"
    pump.drain(value)
    assert not value.authority_intake_paused(KEYS[0])
    assert driver.poll(310).accepted
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 4. Window wiring (no GUI)
# --------------------------------------------------------------------------


def test_window_skips_poll_submission_while_intake_is_paused():
    paused = {"value": True}
    debug = []
    host = SimpleNamespace(
        _server_companion_poll_inflight=False,
        _server_companion_recovery_candidate=None,
        _server_companion_recovery_confirmation_inflight=False,
        _server_companion_should_poll=lambda: True,
        _server_companion_restart_learning_key=lambda: "k:1",
        _companion_restart_phase2=SimpleNamespace(
            authority_intake_paused=lambda key: paused["value"]
        ),
        _debug_server_companion_alert=debug.append,
        _server_companion_snapshot={},  # no endpoint: stops right after the gate
        _server_companion_poll_token=1,
    )
    DZLLWindow._submit_server_companion_poll(host)
    assert host._server_companion_poll_inflight is False
    assert any("intake paused" in line for line in debug)
    paused["value"] = False
    debug.clear()
    DZLLWindow._submit_server_companion_poll(host)
    assert not any("intake paused" in line for line in debug)


def test_window_coalesces_deferred_refreshes_and_reruns_after_settling():
    double = r8._QueueRuntime()
    host = r8._window_host(double)
    host.server_companion_panel = None
    double.pending[host.key] = []
    surfaced = []
    original = DZLLWindow._surface_restart_learning_persistence_notice
    try:
        DZLLWindow._surface_restart_learning_persistence_notice = (
            lambda self: surfaced.append(True) or False
        )
        host._refresh_server_companion_restart_learning_summary = (
            lambda: DZLLWindow._refresh_server_companion_restart_learning_summary(host)
        )
        for _ in range(5):
            DZLLWindow._refresh_server_companion_restart_learning_summary(host)
        assert len(double.pending[host.key]) == 1  # bounded: one queued refresh
        double.release(host.key)
        assert surfaced == [True]
        DZLLWindow._on_companion_authority_settled(host, host.key)
        assert surfaced == [True, True]  # the coalesced request re-presents once
        DZLLWindow._on_companion_authority_settled(host, host.key)
        assert surfaced == [True, True]
    finally:
        DZLLWindow._surface_restart_learning_persistence_notice = original


def test_window_drops_a_refused_poll_without_stalling_polling_or_reading_stale_state():
    applied = []

    def refuse(key, callback):
        raise runtime.AuthorityBacklogFull("full")

    host = SimpleNamespace(
        _server_companion_poll_token=4,
        _server_companion_poll_inflight=True,
        _shutdown_cleanup_done=False,
        _server_companion_restart_learning_key=lambda: "k:1",
        _companion_restart_phase2=SimpleNamespace(
            authority_work_pending=lambda key: True,
            when_authority_current=refuse,
        ),
        _debug_server_companion_alert=lambda *_args: None,
    )
    assert DZLLWindow._apply_server_companion_live_result(host, 4, {"ok": True}) is False
    assert host._server_companion_poll_inflight is False  # polling continues
    assert applied == []
    assert DZLLWindow._run_when_companion_authority_current(
        host, "k:1", lambda: applied.append("stale")
    ) is None
    assert applied == []
