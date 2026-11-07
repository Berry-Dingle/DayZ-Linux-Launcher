"""R8: per-server learning rebuilds off the GTK thread.

Every file lives under pytest's ``tmp_path``; no real profile is touched and
no GUI is launched.  Background-mode tests pump completions explicitly on the
test (owner) thread, exactly as ``GLib.idle_add`` does in production.

The synchronous reference is the pre-R8 behaviour: background (R7) writes,
but every save point rebuilds the server record before returning.  It is
reproduced here by disabling background rebuilds on an otherwise identical
runtime.
"""

from __future__ import annotations

import itertools
import threading
import time
from types import SimpleNamespace

import pytest

import test_companion_sc_r7_background_persistence as r7
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher.window import DZLLWindow


KEYS = r7.KEYS
APP_SESSION = "r8-app-session"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def _deterministic_uuids(monkeypatch):
    counter = itertools.count(1)
    monkeypatch.setattr(
        runtime,
        "uuid",
        SimpleNamespace(uuid4=lambda: f"00000000-0000-4000-8000-{next(counter):012d}"),
    )


def _open(active, legacy, now, mode):
    """mode: 'r8' (background rebuilds), 'sync' (pre-R8 reference), 'inline'."""

    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=now,
        app_session_id=APP_SESSION,
        authoritative_schema4_runtime_enabled=True,
        schema4_authority_consumer_shadow_enabled=True,
        schema4_authority_production_cutover_enabled=True,
    )
    pump = r7.Pump()
    if mode in {"r8", "sync"}:
        assert value.enable_background_persistence(post_to_owner=pump.post)
    if mode == "sync":
        value._background_rebuilds_enabled = lambda: False
    return value, pump


class Driver:
    """Drives one actively monitored server the way window.py does."""

    def __init__(self, value, pump, now):
        self.value = value
        self.pump = pump
        self.now = now
        self.current_key = None
        self.actions: list[tuple] = []
        self.results: list[tuple] = []
        self.generation = 0
        self.collected: list = []

    # -- lifecycle ---------------------------------------------------------

    def select(self, key, at):
        if self.current_key and self.current_key != key:
            self.value.end_monitoring(
                self.current_key,
                marker=runtime.LifecycleMarker.SERVER_SWITCH,
                wall_at=self.now + at,
                monotonic_at=at,
            )
        self.current_key = key
        self.generation += 1
        session = self.value.begin_monitoring(
            key, wall_at=self.now + at, monotonic_at=at, poll_generation=self.generation
        )
        assert session
        return session

    def stop(self, at, marker=runtime.LifecycleMarker.PAUSE):
        return self.value.end_monitoring(
            self.current_key, marker=marker, wall_at=self.now + at, monotonic_at=at
        )

    def poll(self, at, *, ok=True, players=12, queue=None):
        update = r7._poll(
            self.value,
            self.current_key,
            self.generation,
            at,
            self.now,
            ok=ok,
            players=players,
            queue=queue,
        )
        self.collected.extend(update.finalized_events)
        return update

    def tick(self, at):
        update = self.value.tick(
            self.current_key, wall_at=self.now + at, monotonic_at=at
        )
        self.collected.extend(update.finalized_events)
        events, self.collected = tuple(self.collected), []
        return SimpleNamespace(finalized_events=events)

    def drain(self):
        if self.value._authoritative_schema4_backend.background_writes_enabled:
            self.pump.drain(self.value)

    # -- alerts (window._handle_phase2_finalized_events / warning) --------

    def _wanted(self, key):
        return lambda: self.current_key == key

    def _record(self, label, result):
        self.results.append((label, result.source, result.reason_codes[0]))

    def dispatch(self, kind, at, label):
        key = self.current_key
        final = []

        def on_result(result):
            final.append(result)
            self._record(label, result)

        immediate = self.value.dispatch_authority_consumer_action(
            key,
            kind=kind,
            now=self.now + at,
            action=lambda: self.actions.append((label, key, kind)) or True,
            on_result=on_result,
            still_wanted=self._wanted(key),
        )
        return immediate, final

    def finalized(self, update, at):
        for event in update.finalized_events:
            self.value.decision(
                self.current_key,
                now=self.now + at,
                server_online_healthy=True,
                restart_alert_enabled=True,
                event=event,
                generic_duration_ok=True,
            )
            self.dispatch("generic_recovery", at, f"recovery:{event.event_id}")

    def warning(self, at):
        self.value.decision(
            self.current_key,
            now=self.now + at,
            server_online_healthy=True,
            restart_alert_enabled=True,
        )
        return self.dispatch("scheduled_warning", at, f"warning:{int(at)}")

    # -- restart shapes -----------------------------------------------------

    def oo_restart(self, start):
        for offset, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
            self.poll(start + offset, players=players)
        for offset in (90, 96, 102, 108, 114):
            self.poll(start + offset, ok=False)
        self.poll(start + 120, players=3, queue=2)
        for offset in range(130, 400, 30):
            self.poll(start + offset, players=8)
        return self.tick(start + 420)

    def qv_restart(self, start):
        for offset in range(0, 60, 10):
            self.poll(start + offset, players=20)
        for offset in range(60, 300, 10):
            self.poll(start + offset, players=0)
        for offset in range(300, 700, 20):
            self.poll(start + offset, players=15)
        return self.tick(start + 720)

    def gap_restart(self, start):
        # A monitoring gap (unobserved interval) in the middle of an outage.
        for offset, players in ((0, 12), (10, 12)):
            self.poll(start + offset, players=players)
        self.poll(start + 40, ok=False)
        self.poll(start + 46, ok=False)
        self.poll(start + 400, players=10)  # long unobserved gap
        for offset in range(420, 600, 30):
            self.poll(start + offset, players=10)
        return self.tick(start + 620)


def _scenario(driver: Driver, *, settle_each_step: bool):
    """One deterministic single-active-server lifecycle.

    With ``settle_each_step`` False nothing is pumped until the very end, so
    every rebuild completion is withheld and all ordered work queues up.
    """

    def settle():
        if settle_each_step:
            driver.drain()

    driver.select(KEYS[0], 100)
    settle()
    base = 1000
    for cycle in range(3):
        start = base + cycle * r7.P3
        update = driver.oo_restart(start)
        driver.finalized(update, start + 421)
        settle()
        driver.warning(start + r7.P3 - 300)
        driver.warning(start + r7.P3 - 290)  # duplicate in the same window
        settle()
    update = driver.qv_restart(base + 3 * r7.P3)
    driver.finalized(update, base + 3 * r7.P3 + 721)
    settle()
    update = driver.gap_restart(base + 4 * r7.P3)
    driver.finalized(update, base + 4 * r7.P3 + 621)
    settle()
    # Local connectivity interruption: the window ends the session with the
    # SLEEP_GAP marker mid-outage and starts a fresh one.
    start = base + 5 * r7.P3
    driver.poll(start, players=12)
    driver.poll(start + 30, ok=False)
    driver.stop(start + 36, marker=runtime.LifecycleMarker.SLEEP_GAP)
    driver.select(KEYS[0], start + 60)
    settle()
    # Switch servers, restart there, switch back.
    driver.select(KEYS[1], start + 200)
    update = driver.oo_restart(start + 300)
    driver.finalized(update, start + 721)
    settle()
    driver.select(KEYS[0], start + 900)
    update = driver.oo_restart(base + 6 * r7.P3)
    driver.finalized(update, base + 6 * r7.P3 + 421)
    settle()
    driver.stop(base + 6 * r7.P3 + 500)
    driver.drain()


def _run(tmp_path, monkeypatch, mode, *, settle_each_step, name):
    directory = tmp_path / name
    directory.mkdir()
    active, legacy, now = r7._store(directory)
    _deterministic_uuids(monkeypatch)
    value, pump = _open(active, legacy, now, mode)
    driver = Driver(value, pump, now)
    driver.drain()
    _scenario(driver, settle_each_step=settle_each_step)
    status = value.persistence_status
    knowledge = _knowledge(value)
    value.shutdown(wall_at=now + 10 * r7.P3, monotonic_at=10 * r7.P3)
    state = r7._strict(active)
    return SimpleNamespace(
        driver=driver,
        status=status,
        knowledge=knowledge,
        servers=state["servers"],
        stats=value.authority_queue_stats(),
        active=active,
        legacy=legacy,
        now=now,
    )


def _knowledge(value):
    backend = value._authoritative_schema4_backend
    out = {}
    for key in KEYS:
        decision = backend.authority_decision(key)
        resolution = value.authority_consumer_resolution(key)
        selected = getattr(resolution, "selected_output", None)
        out[key] = (
            None if decision is None else decision.state,
            None if decision is None or decision.selected_shadow_regime is None else decision.selected_shadow_regime.regime_id,
            None if decision is None else round(decision.combined_confidence, 9),
            getattr(selected, "countdown_safe", None),
            getattr(selected, "next_expected_restart_at", None),
            getattr(selected, "decision_id", None),
            tuple(sorted(item.serialize() for item in value._servers[key].fired_keys))
            if key in value._servers
            else (),
        )
    return out


# --------------------------------------------------------------------------
# 1. Parity with the synchronous reference
# --------------------------------------------------------------------------


def test_background_rebuilds_match_the_synchronous_reference_exactly(tmp_path, monkeypatch):
    """Rebuilds deferred within every step; lifecycle changes after settling."""

    reference = _run(tmp_path, monkeypatch, "sync", settle_each_step=True, name="sync")
    background = _run(tmp_path, monkeypatch, "r8", settle_each_step=True, name="r8")
    # The reference really was synchronous, the R8 run really deferred.
    assert reference.stats["background_rebuilds"] == 0
    assert background.stats["background_rebuilds"] > 0
    assert background.stats["deferred_owner_ops"] > 0
    assert reference.status is background.status is runtime.RuntimePersistenceStatus.ENABLED
    # Authority, confidence, prediction, consumer decision and fired keys.
    assert background.knowledge == reference.knowledge
    # Emitted alerts and every final dispatch outcome, in order.
    assert background.driver.actions == reference.driver.actions
    assert background.driver.results == reference.driver.results
    # Every persisted server record is byte-identical.
    for key in reference.servers:
        assert schema4.canonical_json_bytes(
            background.servers[key]
        ) == schema4.canonical_json_bytes(reference.servers[key]), key
    # The scenario really emitted recoveries (some were requested while the
    # finalizing rebuild was still pending).
    assert any(label.startswith("recovery:") for label, _k, _t in reference.driver.actions)


LEARNING_FIELDS = (
    "authority_decision",
    "learned_state",
    "physical_events",
    "interval_relationships",
    "cadence_streaks",
    "expected_window_results",
    "continuity_chains",
    "legacy_schema3_record",
)


def test_withheld_rebuilds_learn_identically_and_never_alert_an_ended_session(
    tmp_path, monkeypatch
):
    """Nothing is installed until the end: every rebuild and its dependants queue.

    Learning must be identical.  Alerts requested in a monitoring session
    that has ended (stop, switch, connectivity restart) before their
    rebuild was installed are never dispatched.
    """

    # Ordering under a deep queue (not capacity): keep R8a's intake pause
    # out of the way here; it is covered in the R8a module.
    monkeypatch.setattr(runtime, "AUTHORITY_INTAKE_PAUSE_OPS", 10_000)
    monkeypatch.setattr(runtime, "MAX_AUTHORITY_QUEUE_OPS", 20_000)
    reference = _run(tmp_path, monkeypatch, "sync", settle_each_step=False, name="sync")
    background = _run(tmp_path, monkeypatch, "r8", settle_each_step=False, name="r8")
    assert background.stats["max_queue_depth"] > 10
    assert background.status is runtime.RuntimePersistenceStatus.ENABLED
    for key in reference.servers:
        for field in LEARNING_FIELDS:
            assert background.servers[key].get(field) == reference.servers[key].get(field), (
                key,
                field,
            )
    for key in KEYS:
        assert background.knowledge[key][:5] == reference.knowledge[key][:5], key
        assert background.knowledge[key][6] == reference.knowledge[key][6], key
    assert background.driver.actions == []
    assert {reason for _l, _s, reason in background.driver.results} == {
        "monitoring_session_changed_before_dispatch"
    }
    assert len(background.driver.results) == len(reference.driver.results)


def test_background_rebuilds_match_inline_learning(tmp_path, monkeypatch):
    inline = _run(tmp_path, monkeypatch, "inline", settle_each_step=True, name="inline")
    background = _run(tmp_path, monkeypatch, "r8", settle_each_step=True, name="r8")
    assert background.knowledge == inline.knowledge
    assert background.driver.actions == inline.driver.actions
    assert [item[2] for item in background.driver.results] == [
        item[2] for item in inline.driver.results
    ]
    for key in inline.servers:
        for field in LEARNING_FIELDS + ("regimes", "consumer_state", "authority_decisions"):
            assert background.servers[key].get(field) == inline.servers[key].get(field), (
                key,
                field,
            )
    # Reopen continues learning from the saved state.
    reopened, pump = _open(background.active, background.legacy, background.now + 1, "r8")
    driver = Driver(reopened, pump, background.now)
    driver.select(KEYS[0], 20 * r7.P3)
    update = driver.oo_restart(20 * r7.P3 + 100)
    driver.drain()
    assert update.finalized_events
    reopened.shutdown(wall_at=background.now + 30 * r7.P3, monotonic_at=30 * r7.P3)
    final = r7._strict(background.active)["servers"][KEYS[0]]
    assert len(final["physical_events"]) <= runtime.MAX_FINALIZED_EVENTS
    assert update.finalized_events[0].event_id in {
        item["event_id"] for item in final["physical_events"]
    }


# --------------------------------------------------------------------------
# 2. No learning rebuild on the owner thread
# --------------------------------------------------------------------------


def test_owner_thread_never_rebuilds_validates_or_serializes(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    owner = threading.get_ident()
    calls: dict[str, set[int]] = {}

    def spy(module, name):
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            calls.setdefault(name, set()).add(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    # Per-record validation of a consumer-ledger install (R7, ~0.2 ms) stays
    # on the owner thread by design; the rebuild path's validation is
    # covered by spying the whole rebuild/compare/validate step.
    real_rebuild_step = live4.AuthoritativeSchema4Runtime._rebuild_server_record.__func__

    def rebuild_step(cls, *args, **kwargs):
        calls.setdefault("_rebuild_server_record", set()).add(threading.get_ident())
        return real_rebuild_step(cls, *args, **kwargs)

    monkeypatch.setattr(
        live4.AuthoritativeSchema4Runtime, "_rebuild_server_record", classmethod(rebuild_step)
    )
    for name in (
        "rebuild_schema4_server_record",
        "encode_schema4_server_fragment",
        "validate_schema4_server_fragment",
        "assemble_schema4_payload",
        "verify_schema4_payload_layout",
        "_install_atomic_schema4",
    ):
        spy(live4, name)
    real_canonical = live4.canonical_json_bytes

    def canonical(value_, *args, **kwargs):
        if threading.get_ident() == owner and isinstance(value_, dict) and (
            "physical_events" in value_ or "authority_decision" in value_
        ):
            calls.setdefault("canonical_record_on_owner", set()).add(owner)
        return real_canonical(value_, *args, **kwargs)

    monkeypatch.setattr(live4, "canonical_json_bytes", canonical)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    for cycle in range(2):
        start = 1000 + cycle * r7.P3
        update = driver.oo_restart(start)
        driver.finalized(update, start + 421)
        driver.warning(start + r7.P3 - 300)
        pump.run()
    driver.stop(1000 + 2 * r7.P3)
    pump.drain(value)
    assert calls.get("rebuild_schema4_server_record")
    for name, threads in calls.items():
        assert owner not in threads, name
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 3. Authority and consumer ordering
# --------------------------------------------------------------------------


def test_finalized_recovery_is_evaluated_against_the_rebuilt_authority(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    assert update.finalized_events
    event_id = update.finalized_events[0].event_id
    # Rebuild withheld: the installed record does not yet hold the event.
    assert value.authority_work_pending(KEYS[0])
    assert event_id not in {
        item["event_id"] for item in backend._state["servers"][KEYS[0]]["physical_events"]
    }
    value.decision(KEYS[0], now=now + 1421, event=update.finalized_events[0])
    immediate, final = driver.dispatch("generic_recovery", 1421, "recovery")
    assert immediate.reason_codes[0] == "authority_rebuild_pending"
    assert final == [] and driver.actions == []
    pump.drain(value)
    # Synchronous semantics: never "event not retained", dispatched once.
    assert final and final[-1].emitted
    assert final[-1].reason_codes[0] == "external_action_emitted_once"
    assert driver.actions == [("recovery", KEYS[0], "generic_recovery")]
    again, _final = driver.dispatch("generic_recovery", 1422, "recovery-repeat")
    pump.drain(value)
    assert driver.actions == [("recovery", KEYS[0], "generic_recovery")]
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_consumer_state_changes_during_a_rebuild_are_never_lost(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    gate, _started = r7._gated_install(monkeypatch)
    occurrence = now + 40_000
    key = r7._eligible_warning(value, KEYS[0], occurrence)
    fired = []
    value.dispatch_authority_consumer_action(
        KEYS[0], kind="scheduled_warning", now=occurrence - 600,
        action=lambda: fired.append(True) or True,
    )
    # While the reservation write is held, a save point queues a rebuild.
    driver = Driver(value, pump, now)
    driver.current_key = KEYS[0]
    driver.generation = 7
    value.begin_monitoring(KEYS[0], wall_at=now + 50, monotonic_at=50, poll_generation=7)
    driver.poll(60)
    gate.set()
    pump.drain(value)
    assert fired == [True]
    record = backend._state["servers"][KEYS[0]]
    lifecycles = [item["lifecycle"] for item in record["consumer_state"]["alert_records"]]
    assert "reserved" in lifecycles and "emitted" in lifecycles
    ledger = live4.consumer_ledger_from_mapping(record["consumer_state"], server_key=KEYS[0])
    assert live4.key_suppressed(ledger, key, event_finalized_at=live4._event_finalized_at(record))
    assert backend.persistence_stats()["rebuild_rebases"] == 0
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    disk = r7._strict(active)["servers"][KEYS[0]]
    assert "emitted" in [item["lifecycle"] for item in disk["consumer_state"]["alert_records"]]


def test_warning_requested_during_a_rebuild_is_dispatched_once_in_order(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    occurrence = now + 45_000
    r7._eligible_warning(value, KEYS[0], occurrence)
    forced = value._authority_consumer_resolutions[KEYS[0]]
    # A save point queues a rebuild; the warning requests wait behind it.
    value.mark_fired(
        KEYS[0],
        runtime.SuppressionKey(
            kind=runtime.AlertKeyKind.GENERIC_RECOVERY,
            server_key=KEYS[0],
            event_id="r8-marker",
            candidate_period_seconds=None,
            regime_generation="r8",
        ),
        now=now + 1,
    )
    assert value.authority_work_pending(KEYS[0])
    fired, results = [], []
    for repeat in range(2):
        immediate = value.dispatch_authority_consumer_action(
            KEYS[0], kind="scheduled_warning", now=occurrence - 600 + repeat,
            action=lambda: fired.append(True) or True, on_result=results.append,
        )
        assert immediate.reason_codes[0] == "authority_rebuild_pending"
    # The forced resolution is still in force when they run (no evaluation
    # was queued between), so the first is reserved and dispatched once.
    assert value._authority_consumer_resolutions[KEYS[0]] is forced
    pump.drain(value)
    assert fired == [True]
    assert [item.reason_codes[0] for item in results] == [
        "suppressed_already_fired",
        "external_action_emitted_once",
    ] or [item.reason_codes[0] for item in results] == [
        "external_action_emitted_once",
        "suppressed_already_fired",
    ]
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 4. Single-active-server lifecycle: switch, stop, shutdown, reopen
# --------------------------------------------------------------------------


def test_switch_while_rebuild_pending_saves_old_event_and_never_alerts_new_session(
    tmp_path, monkeypatch
):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    event_id = update.finalized_events[0].event_id
    driver.finalized(update, 1421)
    driver.warning(1500)
    # The user switches before anything was installed.
    driver.select(KEYS[1], 1430)
    for offset in range(0, 100, 10):
        driver.poll(1440 + offset)
    pump.drain(value)
    assert driver.actions == []
    reasons = {reason for _label, _source, reason in driver.results}
    assert reasons <= {
        "no_longer_wanted_before_reservation",
        "monitoring_session_changed_before_dispatch",
        "schema4_action_not_eligible",
    }
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    disk = r7._strict(active)["servers"]
    assert event_id in {item["event_id"] for item in disk[KEYS[0]]["physical_events"]}
    sessions = disk[KEYS[0]]["legacy_schema3_record"]["monitoring_sessions"]
    assert sessions[-1]["reason"] == "server_switch"
    assert disk[KEYS[1]]["legacy_schema3_record"]["monitoring_sessions"][-1]["reason"] == "shutdown"


def test_stop_and_restart_during_rebuild_drops_the_earlier_session_request(
    tmp_path, monkeypatch
):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    driver.finalized(update, 1421)
    driver.stop(1425)
    driver.select(KEYS[0], 1430)  # same server, new monitoring session
    pump.drain(value)
    assert driver.actions == []
    assert [reason for _l, _s, reason in driver.results] == [
        "monitoring_session_changed_before_dispatch"
    ] * len(update.finalized_events)
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_shutdown_with_pending_rebuilds_saves_everything_and_dispatches_nothing(
    tmp_path, monkeypatch
):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    gate, _started = r7._gated_install(monkeypatch)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    driver.finalized(update, 1421)
    driver.warning(1500)
    window_work = []
    assert value.when_authority_current(KEYS[0], lambda: window_work.append(1)) is False
    assert value.authority_work_pending(KEYS[0])
    threading.Timer(0.1, gate.set).start()
    value.shutdown(wall_at=now + 5000, monotonic_at=5000)
    assert driver.actions == []
    assert window_work == []  # queued window work never runs at shutdown
    assert {reason for _l, _s, reason in driver.results} <= {
        "shutdown_before_dispatch",
        "schema4_action_not_eligible",
    }
    disk = r7._strict(active)["servers"][KEYS[0]]
    assert update.finalized_events[0].event_id in {
        item["event_id"] for item in disk["physical_events"]
    }
    assert disk["legacy_schema3_record"]["monitoring_sessions"][-1]["reason"] == "shutdown"
    again, again_pump = _open(active, legacy, now + 1, "r8")
    again_pump.drain(again)
    assert again.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    again.shutdown(wall_at=now + 6000, monotonic_at=6000)


# --------------------------------------------------------------------------
# 5. Failures, isolation and rebase
# --------------------------------------------------------------------------


def test_worker_rebuild_failure_quarantines_only_that_server(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    stored_b = r7._strict(active)["servers"][KEYS[1]]
    real = live4.rebuild_schema4_server_record
    threads = []

    def failing(existing, schema3_record, *, server_key, updated_at):
        if server_key == KEYS[1]:
            threads.append(threading.get_ident())
            raise schema4.Schema4ValidationError("injected deterministic rebuild failure")
        return real(existing, schema3_record, server_key=server_key, updated_at=updated_at)

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", failing)
    notices = []
    value.set_persistence_status_listener(lambda: notices.append(value.persistence_status))
    driver = Driver(value, pump, now)
    driver.select(KEYS[1], 100)
    driver.oo_restart(1000)
    pump.drain(value)
    assert threads and threading.get_ident() not in threads
    block = value.server_persistence_block(KEYS[1])
    assert block is not None and block.reason_code == "server_rebuild_rejected"
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert driver.poll(2000).rejected_reason == "server_persistence_blocked"
    driver.select(KEYS[0], 3000)
    update = driver.oo_restart(4000)
    pump.drain(value)
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)
    disk = r7._strict(active)["servers"]
    assert disk[KEYS[1]] == stored_b
    assert update.finalized_events[0].event_id in {
        item["event_id"] for item in disk[KEYS[0]]["physical_events"]
    }


def test_unexpected_worker_error_disables_persistence_visibly(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    monkeypatch.setattr(
        live4,
        "rebuild_schema4_server_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(MemoryError("injected")),
    )
    notified = []
    value.set_persistence_status_listener(
        lambda: notified.append((threading.get_ident(), value.persistence_status))
    )
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    pump.drain(value)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert notified and all(ident == pump.owner for ident, _ in notified)
    assert value.pending_notice.kind == "persistence_write_failed"
    assert not value.authority_work_pending_any
    value.shutdown(wall_at=now + 2, monotonic_at=2)


def test_record_changed_outside_the_queue_is_rebuilt_on_the_new_base(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    backend = value._authoritative_schema4_backend
    gate = threading.Event()
    real = live4.rebuild_schema4_server_record

    def slow(*args, **kwargs):
        assert gate.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(live4, "rebuild_schema4_server_record", slow)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    assert value.authority_work_pending(KEYS[0])
    record = dict(backend._state["servers"][KEYS[0]])
    record["reason_codes"] = sorted(set(record["reason_codes"]) | {"r8-maintenance"})
    backend.replace_server_record(KEYS[0], record, reason="r8-maintenance")
    gate.set()
    pump.drain(value)
    assert backend.persistence_stats()["rebuild_rebases"] >= 1
    installed = backend._state["servers"][KEYS[0]]
    assert "r8-maintenance" in installed["reason_codes"]
    sessions = installed["legacy_schema3_record"]["monitoring_sessions"]
    assert sessions[-1]["ended_at"] is None  # the queued save point is installed
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_backlog_drains_in_order_across_short_main_loop_callbacks(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    monkeypatch.setattr(runtime, "AUTHORITY_DRAIN_SLICE_SECONDS", 0.0)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)  # a rebuild is now in flight
    ran = []
    for index in range(6):
        value.when_authority_current(KEYS[0], lambda index=index: ran.append(index))
    deadline = time.monotonic() + 30
    callbacks = 0
    while value.authority_work_pending(KEYS[0]):
        callbacks += pump.run()
        assert time.monotonic() < deadline
        time.sleep(0.002)
    assert ran == list(range(6))
    # One entry per main-loop callback with a zero slice: never one batch.
    assert value.authority_queue_stats()["drain_continuations"] >= 5
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_export_waits_for_pending_rebuilds(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "r8")
    pump.drain(value)
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    received = []
    value.export_authoritative_schema4_snapshot_async(
        lambda snapshot, error: received.append((snapshot, error))
    )
    deadline = time.monotonic() + 30
    while not received:
        pump.run()
        assert time.monotonic() < deadline
        time.sleep(0.002)
    snapshot, error = received[0]
    assert error is None
    exported = schema4.deserialize_schema4_bytes(snapshot.canonical_bytes).state
    assert update.finalized_events[0].event_id in {
        item["event_id"] for item in exported["servers"][KEYS[0]]["physical_events"]
    }
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


def test_inline_mode_never_queues(tmp_path, monkeypatch):
    active, legacy, now = r7._store(tmp_path)
    value, pump = _open(active, legacy, now, "inline")
    driver = Driver(value, pump, now)
    driver.select(KEYS[0], 100)
    update = driver.oo_restart(1000)
    driver.finalized(update, 1421)
    assert not value.authority_work_pending_any
    stats = value.authority_queue_stats()
    assert stats["background_rebuilds"] == 0 and stats["deferred_owner_ops"] == 0
    assert driver.actions  # dispatched synchronously, as before R8
    value.shutdown(wall_at=now + 99_999, monotonic_at=99_999)


# --------------------------------------------------------------------------
# 6. Window wiring (no GUI)
# --------------------------------------------------------------------------


class _QueueRuntime:
    """Minimal runtime double with R8's ordered-work contract."""

    def __init__(self):
        self.pending = {}

    def authority_work_pending(self, key):
        return key in self.pending

    def when_authority_current(self, key, callback):
        if key not in self.pending:
            callback()
            return True
        self.pending[key].append(callback)
        return False

    def release(self, key):
        queue = self.pending.pop(key, [])
        while queue:
            queue.pop(0)()


def _window_host(runtime_double, key="r8.example:2302"):
    host = SimpleNamespace(
        _companion_restart_phase2=runtime_double,
        _server_companion_poll_token=5,
        _shutdown_cleanup_done=False,
        _server_companion_restart_learning_key=lambda: host.key,
        _debug_server_companion_alert=lambda *_args: None,
        key=key,
        finished=[],
        applied=[],
    )
    return host


def test_window_finishes_a_poll_only_once_authority_is_current(monkeypatch):
    double = _QueueRuntime()
    host = _window_host(double)
    monkeypatch.setattr(
        DZLLWindow,
        "_finish_server_companion_live_result",
        lambda self, **kwargs: self.finished.append(kwargs["token"]),
    )
    kwargs = dict(
        info={}, snapshot={}, now=1.0, now_wall=2.0, disposition=None,
        confirmed_offline=False, confirmed_recovery=None, guard=None, phase2_update=None,
    )
    DZLLWindow._finish_server_companion_live_result_when_current(
        host, token=5, key=host.key, **kwargs
    )
    assert host.finished == [5]  # nothing pending: synchronous, as before
    double.pending[host.key] = []
    DZLLWindow._finish_server_companion_live_result_when_current(
        host, token=5, key=host.key, **kwargs
    )
    assert host.finished == [5]
    double.release(host.key)
    assert host.finished == [5, 5]
    # Superseded by a switch (token change): dropped, never alerts.
    double.pending[host.key] = []
    DZLLWindow._finish_server_companion_live_result_when_current(
        host, token=5, key=host.key, **kwargs
    )
    host._server_companion_poll_token = 6
    double.release(host.key)
    assert host.finished == [5, 5]
    # Shutdown: dropped.
    host._server_companion_poll_token = 5
    double.pending[host.key] = []
    DZLLWindow._finish_server_companion_live_result_when_current(
        host, token=5, key=host.key, **kwargs
    )
    host._shutdown_cleanup_done = True
    double.release(host.key)
    assert host.finished == [5, 5]


def test_window_queues_a_whole_poll_behind_pending_work(monkeypatch):
    double = _QueueRuntime()
    host = _window_host(double)
    host._server_companion_poll_inflight = True
    double.pending[host.key] = [lambda: host.applied.append("earlier-finish")]
    calls = []
    real = DZLLWindow._apply_server_companion_live_result

    def recording(self, token, info, *, confirmed_recovery=None):
        calls.append(token)
        if len(calls) == 1:
            return real(self, token, info, confirmed_recovery=confirmed_recovery)
        self.applied.append("poll")
        return False

    monkeypatch.setattr(DZLLWindow, "_apply_server_companion_live_result", recording)
    assert DZLLWindow._apply_server_companion_live_result(host, 5, {"ok": True}) is False
    assert host._server_companion_poll_inflight is True  # still in flight
    assert host.applied == []
    double.release(host.key)
    assert host.applied == ["earlier-finish", "poll"]


def test_window_runs_schema3_fallback_only_for_a_deferred_non_schema4_result(monkeypatch):
    from dzll_launcher import window as window_module

    monkeypatch.setattr(window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True)
    fallbacks = []
    monkeypatch.setattr(
        DZLLWindow,
        "_play_phase2_schema3_warning",
        lambda self, key, decision: fallbacks.append(key),
    )
    held = {}

    def dispatch(key, *, kind, now, action, on_result=None, still_wanted=None):
        held["on_result"] = on_result
        return runtime.AuthorityConsumerActionResult(
            False, False, "schema4", None, None, ("authority_rebuild_pending",)
        )

    host = SimpleNamespace(
        _companion_restart_phase2=SimpleNamespace(
            decision=lambda *args, **kwargs: SimpleNamespace(),
            dispatch_authority_consumer_action=dispatch,
        ),
        _server_companion_restart_learning_key=lambda: "k:1",
        _server_companion_restart_alert_enabled=True,
        _debug_server_companion_alert=lambda *_args: None,
        _set_server_companion_alert_audio_status=lambda *_args: None,
    )
    DZLLWindow._maybe_play_server_companion_restart_warning(host, {"online": True})
    assert fallbacks == []
    held["on_result"](
        runtime.AuthorityConsumerActionResult(
            False, False, "schema3_safe_fallback", None, None, ("schema4_consumer_not_selected",)
        )
    )
    assert fallbacks == ["k:1"]


def test_window_does_not_run_fallback_twice_for_an_immediate_result(monkeypatch):
    from dzll_launcher import window as window_module

    monkeypatch.setattr(window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True)
    fallbacks = []
    monkeypatch.setattr(
        DZLLWindow,
        "_play_phase2_schema3_warning",
        lambda self, key, decision: fallbacks.append(key),
    )

    def dispatch(key, *, kind, now, action, on_result=None, still_wanted=None):
        result = runtime.AuthorityConsumerActionResult(
            False, False, "schema3_safe_fallback", None, None, ("schema4_consumer_not_selected",)
        )
        on_result(result)  # the runtime reports immediate outcomes too
        return result

    host = SimpleNamespace(
        _companion_restart_phase2=SimpleNamespace(
            decision=lambda *args, **kwargs: SimpleNamespace(),
            dispatch_authority_consumer_action=dispatch,
        ),
        _server_companion_restart_learning_key=lambda: "k:1",
        _server_companion_restart_alert_enabled=True,
        _debug_server_companion_alert=lambda *_args: None,
        _set_server_companion_alert_audio_status=lambda *_args: None,
    )
    DZLLWindow._maybe_play_server_companion_restart_warning(host, {"online": True})
    assert fallbacks == ["k:1"]


def test_window_defers_presentation_refresh_until_authority_is_current():
    double = _QueueRuntime()
    surfaced = []
    host = _window_host(double)
    host.server_companion_panel = None
    host._companion_restart_phase2 = double
    double.pending_notice = None
    double.pending[host.key] = []

    import dzll_launcher.window as window_module

    original = DZLLWindow._surface_restart_learning_persistence_notice
    try:
        DZLLWindow._surface_restart_learning_persistence_notice = (
            lambda self: surfaced.append(True) or False
        )
        DZLLWindow._refresh_server_companion_restart_learning_summary(host)
        assert surfaced == []
        double.release(host.key)
        assert surfaced == [True]
    finally:
        DZLLWindow._surface_restart_learning_persistence_notice = original
    assert window_module  # imported for the patch target
