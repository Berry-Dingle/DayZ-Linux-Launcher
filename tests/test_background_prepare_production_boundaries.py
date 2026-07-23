from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from types import SimpleNamespace
import threading
import time

import pytest

from dzll_launcher import background_prepare, join_prepare
from dzll_launcher import window as window_module
from dzll_launcher.background_prepare import (
    BackgroundConsentResult,
    BackgroundConsentStatus,
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    SingleServerBackgroundPreparation,
)
from dzll_launcher.background_prepare_queue import BackgroundPreparationQueue
from dzll_launcher.preparation_contracts import (
    PreparationEventKind,
    PreparationOutcome,
    PreparationStatus,
    RecordingPreparationPresenter,
)


class Widget:
    def __init__(self):
        self.text = ""
        self.visible = False
        self.sensitive = True

    def set_text(self, value):
        self.text = str(value)

    def set_label(self, value):
        self.text = str(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = value

    def set_fraction(self, _value):
        return None

    def pulse(self):
        return None


class MainThreadScheduler:
    def __init__(self):
        self.callbacks = Queue()
        self.main_thread = threading.get_ident()
        self.executed_threads = []

    def idle_add(self, callback, *args):
        self.callbacks.put((callback, args))
        return 1

    def drain_one(self, timeout=0.05):
        try:
            callback, args = self.callbacks.get(timeout=timeout)
        except Empty:
            return False
        self.executed_threads.append(threading.get_ident())
        callback(*args)
        return True

    def drain_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                while self.drain_one(timeout=0.0):
                    pass
                return
            self.drain_one()
        pytest.fail("production callback boundary did not complete before watchdog")


class RecordingExecutor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.futures = []

    def submit(self, callback):
        future = self.executor.submit(callback)
        self.futures.append(future)
        return future

    def close(self):
        self.executor.shutdown(wait=True)


class ProductionStartHarness:
    _background_prepare_download_available = (
        window_module.DZLLWindow._background_prepare_download_available
    )
    _background_prepare_is_current = window_module.DZLLWindow._background_prepare_is_current
    _background_prepare_request_is_current = (
        window_module.DZLLWindow._background_prepare_request_is_current
    )
    _background_prepare_resolve_frozen = (
        window_module.DZLLWindow._background_prepare_resolve_frozen
    )
    _background_prepare_for_obj = window_module.DZLLWindow._background_prepare_for_obj
    _background_prepare_apply_queue_snapshot = (
        window_module.DZLLWindow._background_prepare_apply_queue_snapshot
    )
    _background_prepare_start_frozen_request = (
        window_module.DZLLWindow._background_prepare_start_frozen_request
    )
    _background_prepare_render_request_snapshot = (
        window_module.DZLLWindow._background_prepare_render_request_snapshot
    )
    _background_prepare_observe_terminal = (
        window_module.DZLLWindow._background_prepare_observe_terminal
    )
    _background_prepare_worker_returned = (
        window_module.DZLLWindow._background_prepare_worker_returned
    )
    _background_prepare_cancel_consent_ui = (
        window_module.DZLLWindow._background_prepare_cancel_consent_ui
    )
    _background_prepare_render_batch_summary = (
        window_module.DZLLWindow._background_prepare_render_batch_summary
    )
    _background_prepare_log_request = staticmethod(
        window_module.DZLLWindow._background_prepare_log_request
    )
    _background_prepare_steam_consent_blocking = (
        window_module.DZLLWindow._background_prepare_steam_consent_blocking
    )
    _background_prepare_render_snapshot = (
        window_module.DZLLWindow._background_prepare_render_snapshot
    )
    _background_prepare_render_cancelling = (
        window_module.DZLLWindow._background_prepare_render_cancelling
    )
    _background_prepare_start_pulse = window_module.DZLLWindow._background_prepare_start_pulse
    _background_prepare_stop_pulse = window_module.DZLLWindow._background_prepare_stop_pulse
    _refresh_background_prepare_action_states = (
        window_module.DZLLWindow._refresh_background_prepare_action_states
    )

    def __init__(self, executor):
        self._hi_executor = executor
        self._join_attempts = SimpleNamespace(active=None)
        self._steamcmd_cancel_event = threading.Event()
        self._join_steam_start_allowed = False
        self._background_prepare_ui_generation = 0
        self._background_prepare_active = False
        self._background_prepare_cancel_requested = False
        self._background_prepare_terminal_handled = False
        self._background_prepare_controller = None
        self._background_prepare_snapshot = None
        self._background_prepare_pulse_id = 0
        self._shutdown_cleanup_done = False
        self._start_steam_join_loop = None
        self._background_prepare_queue = BackgroundPreparationQueue()
        self.list_view = None
        self.background_prepare_server_label = Widget()
        self.background_prepare_detail_label = Widget()
        self.background_prepare_count_label = Widget()
        self.background_prepare_progress = Widget()
        self.background_prepare_action_btn = Widget()
        self.background_prepare_retry_btn = Widget()
        self.background_prepare_failed_label = Widget()
        self.background_prepare_status = Widget()

    def _resolve_join_mods(self, _obj):
        return [(101, "One")]

    def _resolve_join_runtime(self, _mods):
        return {
            "workshop_dir": "/workshop",
            "steamcmd_path": "/steamcmd",
            "steam_user": "",
            "validate": False,
            "dry": False,
            "use_steamcmd": True,
            "mod_download_backend": "steam_client",
            "auto_install_missing": True,
            "auto_update_required": False,
        }

    def _ensure_join_steam_start_consent(self, attempt_id):
        assert attempt_id == 0
        return True


def snapshot(name="Production Server"):
    return BackgroundServerPreparationSnapshot(
        "10.0.0.1", 2302, 27016, name, ((101, "One"),),
    )


def runtime():
    return BackgroundPreparationRuntime(
        workshop_dir="/workshop",
        steamcmd_path="/steamcmd",
        steam_user="",
        validate=False,
        dry_run=False,
        use_steamcmd=True,
        mod_download_backend="steam_client",
        auto_install_missing=True,
        auto_update_required=False,
    )


def test_production_one_server_path_reaches_controller_and_engine_once(monkeypatch):
    scheduler = MainThreadScheduler()
    executor = RecordingExecutor()
    host = ProductionStartHarness(executor)
    controller_runs = []
    engine_calls = []
    real_run = SingleServerBackgroundPreparation.run

    def observed_run(self, *args, **kwargs):
        controller_runs.append(self)
        return real_run(self, *args, **kwargs)

    def engine_boundary(*_args, **kwargs):
        engine_calls.append(kwargs)
        outcome = PreparationOutcome(PreparationStatus.READY, reason="ready")
        kwargs["presenter"].on_terminal(outcome)
        return outcome

    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(background_prepare.SingleServerBackgroundPreparation, "run", observed_run)
    monkeypatch.setattr(background_prepare, "prepare_required_mods", engine_boundary)
    row = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016,
        name="Production Server", mods_json="[]",
    )

    try:
        host._background_prepare_for_obj(row)
        scheduler.drain_until(lambda: executor.futures and executor.futures[0].done())
        assert executor.futures[0].result().status is PreparationStatus.READY
    finally:
        executor.close()

    assert len(controller_runs) == 1
    assert len(engine_calls) == 1
    assert scheduler.executed_threads
    assert set(scheduler.executed_threads) == {scheduler.main_thread}


def test_production_multiworker_path_runs_two_servers_fifo_with_one_engine_active(
        monkeypatch):
    scheduler = MainThreadScheduler()
    executor = RecordingExecutor()
    host = ProductionStartHarness(executor)
    entered_a = threading.Event()
    release_a = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    order = []

    def engine(_win, _mods, *_args, **kwargs):
        nonlocal active, max_active
        name = kwargs["server_name"]
        with lock:
            active += 1
            max_active = max(max_active, active)
            order.append(name)
        if name == "A":
            entered_a.set()
            assert release_a.wait(timeout=2.0)
        outcome = PreparationOutcome(PreparationStatus.READY, reason="ready")
        kwargs["presenter"].on_terminal(outcome)
        with lock:
            active -= 1
        return outcome

    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(background_prepare, "prepare_required_mods", engine)
    a = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016, name="A", mods_json="[]",
    )
    b = SimpleNamespace(
        ip="10.0.0.2", gport=2302, qport=27016, name="B", mods_json="[]",
    )
    try:
        host._background_prepare_for_obj(a)
        scheduler.drain_until(entered_a.is_set)
        host._background_prepare_for_obj(b)
        assert len(executor.futures) == 1
        assert [request.display_name for request in host._background_prepare_queue.snapshot().pending] == ["B"]
        release_a.set()
        scheduler.drain_until(
            lambda: len(executor.futures) == 2
            and all(future.done() for future in executor.futures),
        )
    finally:
        release_a.set()
        executor.close()
    assert order == ["A", "B"]
    assert max_active == 1
    assert host._background_prepare_queue.snapshot().completed_batch.ready_count == 2


def test_production_batch_cancel_interrupts_consent_without_engine_entry(
        monkeypatch):
    scheduler = MainThreadScheduler()
    executor = RecordingExecutor()
    host = ProductionStartHarness(executor)
    host._background_prepare_consent_timeout_s = 30.0
    engine_calls = []
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(
        background_prepare, "prepare_required_mods",
        lambda *_a, **_k: engine_calls.append(1),
    )
    row = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016,
        name="Consent Wait", mods_json="[]",
    )
    started = time.monotonic()
    try:
        host._background_prepare_for_obj(row)
        deadline = time.monotonic() + 1.0
        while scheduler.callbacks.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not scheduler.callbacks.empty()
        window_module.DZLLWindow._background_prepare_action_clicked(host, None)
        scheduler.drain_until(lambda: executor.futures[0].done())
    finally:
        executor.close()
    assert time.monotonic() - started < 1.0
    assert engine_calls == []
    snapshot = host._background_prepare_queue.snapshot()
    assert not snapshot.busy
    assert snapshot.completed_batch.cancelled_count == 1


def test_production_batch_cancel_waits_for_cleanup_and_never_dispatches_next(
        monkeypatch):
    scheduler = MainThreadScheduler()
    executor = RecordingExecutor()
    host = ProductionStartHarness(executor)
    entered = threading.Event()
    saw_cancel = threading.Event()
    release_cleanup = threading.Event()

    def engine(win, _mods, *_args, **kwargs):
        entered.set()
        assert win._steamcmd_cancel_event.wait(timeout=2.0)
        saw_cancel.set()
        assert release_cleanup.wait(timeout=2.0)
        outcome = PreparationOutcome(
            PreparationStatus.CANCELLED, reason="cancelled", error="cancelled safely",
        )
        kwargs["presenter"].on_terminal(outcome)
        return outcome

    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    monkeypatch.setattr(background_prepare, "prepare_required_mods", engine)
    a = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016, name="A", mods_json="[]",
    )
    b = SimpleNamespace(
        ip="10.0.0.2", gport=2302, qport=27016, name="B", mods_json="[]",
    )
    try:
        host._background_prepare_for_obj(a)
        scheduler.drain_until(entered.is_set)
        host._background_prepare_for_obj(b)
        window_module.DZLLWindow._background_prepare_action_clicked(host, None)
        assert saw_cancel.wait(timeout=0.5)
        assert host._background_prepare_queue.busy
        assert not host._background_prepare_queue.accepting
        assert len(executor.futures) == 1
        assert not executor.futures[0].done()
        release_cleanup.set()
        scheduler.drain_until(lambda: executor.futures[0].done())
    finally:
        release_cleanup.set()
        executor.close()
    assert len(executor.futures) == 1
    assert not host._background_prepare_queue.busy
    assert host._background_prepare_queue.accepting
    assert host._background_prepare_queue.record_for("10.0.0.2:2302").state.value == "idle"


def test_production_setup_exception_becomes_visible_failed_batch(monkeypatch, capsys):
    executor = RecordingExecutor()
    host = ProductionStartHarness(executor)
    monkeypatch.setattr(
        window_module, "BrowserPreparationPresenter",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("presenter setup exploded")),
    )
    row = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016,
        name="Setup Failure", mods_json="[]",
    )
    try:
        host._background_prepare_for_obj(row)
    finally:
        executor.close()
    captured = capsys.readouterr()
    batch = host._background_prepare_queue.snapshot().completed_batch
    assert batch.failed_count == 1
    assert "presenter setup exploded" in batch.failed_entries[0].error
    assert "Traceback" in captured.err
    assert host.background_prepare_server_label.text == "Background mod preparation finished"


class ConsentHarness:
    _background_prepare_is_current = window_module.DZLLWindow._background_prepare_is_current
    _background_prepare_steam_consent_blocking = (
        window_module.DZLLWindow._background_prepare_steam_consent_blocking
    )

    def __init__(self, decision=True):
        self._background_prepare_ui_generation = 7
        self._shutdown_cleanup_done = False
        self._steamcmd_cancel_event = threading.Event()
        self._background_prepare_consent_timeout_s = 0.2
        self.decision = decision
        self.calls = 0

    def _ensure_join_steam_start_consent(self, attempt_id):
        assert attempt_id == 0
        self.calls += 1
        if isinstance(self.decision, BaseException):
            raise self.decision
        return bool(self.decision)


def run_consent(scheduler, host, generation=7):
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            host._background_prepare_steam_consent_blocking(generation)
        )
    )
    worker.start()
    scheduler.drain_until(lambda: not worker.is_alive())
    worker.join(timeout=0.2)
    assert not worker.is_alive()
    return result[0]


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (True, BackgroundConsentStatus.ALLOWED),
        (False, BackgroundConsentStatus.DECLINED),
    ],
)
def test_real_consent_callback_completes_allowed_and_declined(
        monkeypatch, decision, expected):
    scheduler = MainThreadScheduler()
    host = ConsentHarness(decision)
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    result = run_consent(scheduler, host)
    assert result.status is expected
    assert host.calls == 1
    assert scheduler.executed_threads == [scheduler.main_thread]


def test_real_consent_callback_completes_stale_without_prompt(monkeypatch):
    scheduler = MainThreadScheduler()
    host = ConsentHarness(True)
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    result = run_consent(scheduler, host, generation=6)
    assert result.status is BackgroundConsentStatus.STALE
    assert host.calls == 0


def test_real_consent_callback_exception_completes_and_logs_traceback(
        monkeypatch, capsys):
    scheduler = MainThreadScheduler()
    host = ConsentHarness(RuntimeError("consent exploded"))
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    result = run_consent(scheduler, host)
    captured = capsys.readouterr()
    assert result.status is BackgroundConsentStatus.ERROR
    assert "consent exploded" in result.error
    assert "Traceback" in captured.err


def test_consent_cancel_interrupts_wait_promptly_and_late_callback_is_safe(monkeypatch):
    scheduler = MainThreadScheduler()
    host = ConsentHarness(True)
    host._background_prepare_consent_timeout_s = 10.0
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(host._background_prepare_steam_consent_blocking(7))
    )
    started = time.monotonic()
    worker.start()
    deadline = time.monotonic() + 1.0
    while scheduler.callbacks.empty() and time.monotonic() < deadline:
        time.sleep(0.005)
    host._steamcmd_cancel_event.set()
    worker.join(timeout=0.5)
    elapsed = time.monotonic() - started
    assert not worker.is_alive()
    assert elapsed < 0.5
    assert result[0].status is BackgroundConsentStatus.CANCELLED
    assert scheduler.drain_one()
    assert host.calls == 0


def test_batch_cancel_dismisses_an_open_background_consent_loop_only():
    class Loop:
        def __init__(self):
            self.quit_calls = 0

        def quit(self):
            self.quit_calls += 1

    loop = Loop()
    host = SimpleNamespace(
        _start_steam_join_loop=loop,
        _start_steam_join_decision=None,
        start_steam_join_box=Widget(),
        start_steam_join_scrim=Widget(),
    )
    window_module.DZLLWindow._background_prepare_cancel_consent_ui(host)
    assert host._start_steam_join_decision == (False, False)
    assert not host.start_steam_join_box.visible
    assert not host.start_steam_join_scrim.visible
    assert loop.quit_calls == 1


def test_consent_timeout_is_not_reported_as_decline(monkeypatch):
    scheduler = MainThreadScheduler()
    host = ConsentHarness(True)
    host._background_prepare_consent_timeout_s = 0.03
    monkeypatch.setattr(window_module.GLib, "idle_add", scheduler.idle_add)
    result = []
    worker = threading.Thread(
        target=lambda: result.append(host._background_prepare_steam_consent_blocking(7))
    )
    worker.start()
    worker.join(timeout=0.5)
    assert not worker.is_alive()
    assert result[0].status is BackgroundConsentStatus.TIMEOUT
    assert "timed out" in result[0].error.lower()
    assert scheduler.drain_one()
    assert host.calls == 0


@pytest.mark.parametrize(
    ("consent_status", "outcome_status", "reason"),
    [
        (BackgroundConsentStatus.DECLINED, PreparationStatus.CANCELLED, "steam_start_declined"),
        (BackgroundConsentStatus.CANCELLED, PreparationStatus.CANCELLED, "consent_cancelled"),
        (BackgroundConsentStatus.STALE, PreparationStatus.CANCELLED, "consent_stale"),
        (BackgroundConsentStatus.TIMEOUT, PreparationStatus.FAILED, "steam_start_timeout"),
        (BackgroundConsentStatus.ERROR, PreparationStatus.FAILED, "steam_start_error"),
    ],
)
def test_controller_maps_explicit_consent_result_to_actionable_outcome(
        monkeypatch, consent_status, outcome_status, reason):
    host = SimpleNamespace(
        _steamcmd_cancel_event=threading.Event(),
        _join_steam_start_allowed=False,
        _join_attempts=SimpleNamespace(active=None),
    )
    monkeypatch.setattr(
        background_prepare,
        "prepare_required_mods",
        lambda *_a, **_k: pytest.fail("engine must not run without allowed consent"),
    )
    outcome = SingleServerBackgroundPreparation(host).run(
        snapshot(), runtime(), RecordingPreparationPresenter(),
        ensure_steam_consent=lambda: BackgroundConsentResult(
            consent_status, error="specific consent detail",
        ),
    )
    assert outcome.status is outcome_status
    assert outcome.reason == reason
    assert "specific consent detail" in outcome.error


class ReadinessHarness:
    def __init__(self):
        self.GLib = SimpleNamespace(idle_add=lambda callback, *args: callback(*args) or 1)
        self.threading = threading
        self._join_steam_start_allowed = False
        self._steamcmd_cancel_event = threading.Event()
        self._steamcmd_install_in_progress = False
        self._mod_download_backend_active = ""

    def compute_missing_mods(self, _path, _mods):
        return []

    def _show_join_progress_overlay(self, *_args):
        return None

    def _set_updating(self, *_args):
        return None


def test_background_ugc_readiness_receives_cancel_event_and_visible_progress(monkeypatch):
    host = ReadinessHarness()
    presenter = RecordingPreparationPresenter()
    seen = {}

    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_a: "/workshop")
    monkeypatch.setattr(join_prepare, "_refresh_effective_workshop_dir_after_backend", lambda *_a: "/workshop")
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "query_ugc_state", lambda _ids: {
        101: {"installed": True, "subscribed": True, "needs_update": False},
    })

    def readiness(_ids, **kwargs):
        seen.update(kwargs)
        kwargs["progress_cb"]({"type": "preflight", "message": "Checking Steam..."})
        return True

    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", readiness)
    outcome = join_prepare.prepare_required_mods(
        host, [(101, "One")], "/workshop", "", "", False, False,
        True, "steam_client", True, False,
        presenter=presenter, server_name="Readiness Server",
        manage_join_presence=False, manage_join_presentation=False,
    )
    assert outcome.status is PreparationStatus.READY
    assert seen["cancel_event"] is host._steamcmd_cancel_event
    assert callable(seen["progress_cb"])
    assert any(event.kind is PreparationEventKind.STAGE for event in presenter.events)


def test_background_ugc_readiness_cancel_exits_promptly(monkeypatch):
    host = ReadinessHarness()
    presenter = RecordingPreparationPresenter()
    entered = threading.Event()

    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_a: "/workshop")
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})

    def readiness(_ids, **kwargs):
        entered.set()
        assert kwargs["cancel_event"].wait(timeout=0.5)
        return False

    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", readiness)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(
        join_prepare.prepare_required_mods(
            host, [(101, "One")], "/workshop", "", "", False, False,
            True, "steam_client", True, False,
            presenter=presenter, server_name="Readiness Server",
            manage_join_presence=False, manage_join_presentation=False,
        )
    ))
    worker.start()
    assert entered.wait(timeout=0.5)
    host._steamcmd_cancel_event.set()
    worker.join(timeout=0.5)
    assert not worker.is_alive()
    assert outcomes[0].status is PreparationStatus.CANCELLED


def test_background_ugc_readiness_timeout_is_actionable_and_visible(monkeypatch):
    host = ReadinessHarness()
    presenter = RecordingPreparationPresenter()
    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_a: "/workshop")
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})

    def readiness(_ids, **kwargs):
        kwargs["progress_cb"]({
            "type": "preflight",
            "message": "Steam did not become ready. Please make sure native Steam is running and logged in.",
            "error": True,
        })
        return False

    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", readiness)
    outcome = join_prepare.prepare_required_mods(
        host, [(101, "One")], "/workshop", "", "", False, False,
        True, "steam_client", True, False,
        presenter=presenter, server_name="Readiness Server",
        manage_join_presence=False, manage_join_presentation=False,
    )
    assert outcome.status is PreparationStatus.FAILED
    assert "could not check mods with Steam" in outcome.error
    assert presenter.terminal is outcome
    assert presenter.events
