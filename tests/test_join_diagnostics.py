from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import Future
import threading

import pytest

from dzll_launcher.join_attempt import JoinAttemptTracker
from dzll_launcher import (
    join_prepare, launch_utils, steam_client_mods, steam_ugc_backend,
    window as window_module,
)
from dzll_launcher.background_prepare import preparation_operation_gate
from dzll_launcher.background_prepare_queue import BackgroundPreparationQueue
from dzll_launcher.join_preparation_busy import shared_join_preparation_busy
from dzll_launcher.window import DZLLWindow


class Clock:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 10.0

    def wall_time(self):
        return self.wall

    def monotonic(self):
        return self.mono


def tracker():
    clock = Clock()
    lines = []
    value = JoinAttemptTracker(log_sink=lines.append, wall_clock=clock.wall_time,
                               monotonic_clock=clock.monotonic)
    return value, clock, lines


def begin(value, *, name="Original"):
    return value.begin(ip="127.0.0.1", game_port=2302, query_port=27016, name=name)


def test_unique_attempt_id_allocated_on_join_click():
    value, _clock, _lines = tracker()
    first = begin(value)
    assert value.cleanup(first.attempt_id, "done")
    second = begin(value)
    assert (first.attempt_id, second.attempt_id) == (1, 2)


def test_default_join_attempt_diagnostics_are_quiet(capsys):
    value = JoinAttemptTracker()
    attempt = begin(value)
    value.log(attempt.attempt_id, "synthetic success", ready=True)
    assert value.active.transitions[-1][0] == "synthetic success"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ugc_lifecycle_events_reach_callback_without_terminal_chatter(capsys):
    events = []
    steam_ugc_backend._log_event(
        events.append,
        "[Steam UGC] Cooperative helper session exited",
        helper_pid=4321,
    )
    assert events == [{
        "type": "log",
        "message": "[Steam UGC] Cooperative helper session exited",
        "helper_pid": 4321,
    }]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_successful_steam_client_install_is_quiet_by_default(monkeypatch, capsys):
    monkeypatch.setattr(steam_client_mods, "run_ugc_install", lambda *_a, **_k: True)
    assert steam_client_mods.run_steam_client_install(
        workshop_dir="/unused",
        mod_ids=[101],
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_immutable_server_key_survives_replacement():
    value, _clock, _lines = tracker()
    attempt = begin(value)
    replacement = SimpleNamespace(ip="10.0.0.2", gport=2402, qport=27017, name="Replacement")
    assert replacement.ip != attempt.server.ip
    assert attempt.server.key == "127.0.0.1:2302"
    with pytest.raises(Exception):
        attempt.server.ip = replacement.ip


def test_second_click_during_active_attempt_allocates_no_worker_identity():
    value, _clock, _lines = tracker()
    first = begin(value)
    assert begin(value, name="Second") is None
    assert value.active is first


def test_mod_repair_gate_owner_blocks_join_before_attempt_creation():
    attempts = JoinAttemptTracker()
    statuses = []
    host = SimpleNamespace(
        _join_attempts=attempts,
        _background_prepare_queue=None,
        _set_server_companion_join_status=(
            lambda message, flash=False: statuses.append((message, flash))
        ),
        _join_log=lambda *_a, **_k: None,
    )
    gate = preparation_operation_gate(host)
    lease = gate.try_acquire("mod_repair")

    DZLLWindow._join_server_for_obj(
        host,
        SimpleNamespace(ip="192.0.2.1", gport=2302, qport=27016, name="Server"),
    )

    assert attempts.active is None
    assert statuses == [("Mod preparation already in progress…", True)]
    assert gate.release(lease)


def test_duplicate_click_does_not_replace_active_state():
    value, _clock, _lines = tracker()
    first = begin(value)
    value.set_phase(first.attempt_id, "ugc")
    assert begin(value, name="Second") is None
    assert value.active.attempt_id == first.attempt_id
    assert value.active.phase == "ugc"


class ImmediateGLib:
    @staticmethod
    def idle_add(callback, *args):
        callback(*args)
        return 1


class FakeWidget:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class JoinHarness:
    def __init__(self, attempt_id=1):
        self.logs = []
        self.launches = 0
        self.active = True
        self.GLib = ImmediateGLib()
        self.threading = threading
        self._join_steam_start_allowed = False
        self._join_preparation_cancel_event = threading.Event()
        self._steam_client_stop_waiting_event = threading.Event()
        self._steam_ugc_worker_in_progress = False
        self._mod_download_backend_active = ""
        self._discord = None
        self._steamcmd_form_widgets = []
        self.join_preparation_spinner = FakeWidget()
        self.steamcmd_login_btn = FakeWidget()
        self.join_preparation_cancel_button = FakeWidget()
        self._pending_server_companion_obj = object()
        self.attempt_id = attempt_id
        self.launching_messages = 0

    def _join_log(self, attempt_id, event, **fields):
        assert attempt_id == self.attempt_id
        self.logs.append((event, fields))
        return True

    def _join_attempt_is_active(self, attempt_id):
        return self.active and attempt_id == self.attempt_id

    def _cleanup_join_attempt(self, attempt_id, reason):
        self._join_log(attempt_id, "active attempt cleanup", reason=reason)
        self.active = False

    def compute_missing_mods(self, _workshop, _mods):
        return []

    def _reset_join_preparation_overlay(self):
        self._join_preparation_cancel_event = threading.Event()

    def _free_bytes_for_path(self, _path):
        return 20 * 1024 ** 3

    def _show_steam_client_download_overlay(self, _status):
        return None

    def _steam_ugc_progress_from_worker(self, _event):
        return None

    def _set_updating(self, *_args):
        return None

    def _hide_join_preparation_overlay(self):
        return None

    def _show_join_progress_overlay(self, *_args):
        return None

    def _join_popup_show_launching(self, attempt_id):
        assert attempt_id == self.attempt_id
        self.launching_messages += 1
        return True

    def _join_popup_initialize_download_counter(self, attempt_id, mod_ids, *, backend):
        assert attempt_id == self.attempt_id
        assert backend in ("steam_client", "steamcmd")
        self.logs.append(("download presentation counter initialized", {"total": len(set(mod_ids))}))
        return True

    def ensure_watch_symlinks(self, **_kwargs):
        return {"created": [], "updated": [], "kept": [], "removed": [],
                "errors": [], "selected_paths": []}

    def scan_installed_mods_in_watch_folder(self, _path):
        return []

    def bootstrap_launcher_state(self, **_kwargs):
        return {"preset": "synthetic"}

    def _launch_direct_steam_url(self, _obj, _paths, *, attempt_id):
        assert attempt_id == self.attempt_id
        self.launches += 1
        return SimpleNamespace(submitted=True)

    def _on_filter_changed(self, *_args, **_kwargs):
        return None

    def _steam_ugc_render_status(self, *_args, **_kwargs):
        return None


def run_join_route(monkeypatch, states):
    ids = sorted(states)
    mods = [(mid, f"Mod {mid}") for mid in ids]
    win = JoinHarness()
    obj = SimpleNamespace(name="Synthetic", ip="127.0.0.1", gport=2302)
    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_args: "/workshop")
    monkeypatch.setattr(join_prepare, "_refresh_effective_workshop_dir_after_backend", lambda *args: args[0])
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", lambda *_args, **_kwargs: True)
    terminal_states = {
        mid: {
            "installed": True,
            "subscribed": True,
            "needs_update": False,
            "downloading": False,
            "download_pending": False,
        }
        for mid in ids
    }
    state_results = iter(((True, states), (True, terminal_states)))
    monkeypatch.setattr(
        join_prepare, "refresh_subscribed_ugc_state_checked",
        lambda *_args, **_kwargs: (*next(state_results), {}),
    )
    monkeypatch.setattr(
        join_prepare, "query_ugc_state_checked",
        lambda *_args, **_kwargs: next(state_results),
    )
    monkeypatch.setattr(join_prepare.workshop_mods, "validate_selected_watch_symlinks", lambda **_kwargs: [])

    def install(**kwargs):
        kwargs["log_fn"]("helper synthetic start")
        total = len(kwargs["mod_ids"])
        for index, mid in enumerate(kwargs["mod_ids"], start=1):
            kwargs["progress_cb"]({"id": mid, "ready": True, "installed": True,
                                   "completed_count": index, "total": total})
        return True

    win.run_steam_client_install = install
    join_prepare.join_prepare_and_launch(
        win, obj, mods, "/workshop", "/prefix", "/watch", True, True,
        attempt_id=1,
    )
    return win


def event_names(win):
    return [name for name, _fields in win.logs]


def test_all_ready_ugc_logs_continuation_and_one_handoff(monkeypatch):
    ready = {1: {"installed": True, "subscribed": True, "needs_update": False}}
    win = run_join_route(monkeypatch, ready)
    assert "UGC initial all-items-ready" in event_names(win)
    assert "continuation executed" in event_names(win)
    assert win.launches == 1


def test_missing_mod_logs_final_ready_shutdown_and_one_handoff(monkeypatch):
    win = run_join_route(monkeypatch, {1: {"installed": False, "subscribed": False}})
    assert "UGC final item ready" in event_names(win)
    assert "UGC helper shutdown completed" in event_names(win)
    assert win.launches == 1


def test_needs_update_logs_final_ready_shutdown_and_one_handoff(monkeypatch):
    win = run_join_route(monkeypatch, {1: {"installed": True, "subscribed": True, "needs_update": True}})
    classified = dict(next(fields for name, fields in win.logs if name == "UGC queue classified"))
    assert classified["classification"] == "needs update"
    assert win.launches == 1


def test_mixed_queue_classification_and_one_handoff(monkeypatch):
    states = {
        1: {"installed": False, "subscribed": False},
        2: {"installed": True, "subscribed": True, "needs_update": True},
    }
    win = run_join_route(monkeypatch, states)
    classified = next(fields for name, fields in win.logs if name == "UGC queue classified")
    assert classified["classification"] == "mixed"
    assert win.launches == 1


class LaunchWin:
    def __init__(self, extra=""):
        self.settings = {"additional_launch_params": extra, "skip_dayz_launcher": True}
        self._discord = None

    def _steam_launch_prefix(self):
        return ["/usr/bin/steam", "-applaunch", "221100", "--"]


OBJ = SimpleNamespace(ip="127.0.0.1", gport=2302, name="Synthetic", map_name="Chernarus")


def test_popen_success_means_handoff_submitted_not_confirmed_launch(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    calls = []
    result = launch_utils.launch_direct_steam_url(
        LaunchWin(), OBJ,
        popen_factory=lambda command, **kwargs: (
            calls.append((list(command), dict(kwargs)))
            or SimpleNamespace(pid=4321)
        ),
    )
    assert result.submitted is True
    assert result.pid == 4321
    assert not hasattr(result, "launch_confirmed")
    assert calls[0][1] == {
        "stdin": launch_utils.subprocess.DEVNULL,
        "stdout": launch_utils.subprocess.DEVNULL,
        "stderr": launch_utils.subprocess.DEVNULL,
    }


def test_dispatch_guard_runs_immediately_before_and_can_prevent_popen(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    order = []

    result = launch_utils.launch_direct_steam_url(
        LaunchWin(),
        OBJ,
        before_dispatch=lambda: order.append("guard") or False,
        popen_factory=lambda _cmd, **_kwargs: order.append("popen") or SimpleNamespace(pid=4321),
    )

    assert not result.submitted
    assert result.error_kind == "dispatch_guard"
    assert order == ["guard"]


def test_captured_launch_mode_overrides_later_setting_change(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    win = LaunchWin()
    win.settings["skip_dayz_launcher"] = False
    commands = []
    result = launch_utils.launch_direct_steam_url(
        win,
        OBJ,
        popen_factory=lambda command, **_kwargs: commands.append(list(command)) or SimpleNamespace(pid=4321),
        skip_dayz_launcher=True,
    )
    assert result.submitted
    assert "-nolauncher" in commands[0]


def test_command_construction_failure_is_structured(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    result = launch_utils.launch_direct_steam_url(LaunchWin(extra="'unterminated"), OBJ)
    assert not result.submitted
    assert result.error_kind == "command_construction"


def test_popen_exception_is_structured(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    def fail(_cmd, **_kwargs):
        raise OSError("synthetic failure")
    result = launch_utils.launch_direct_steam_url(LaunchWin(), OBJ, popen_factory=fail)
    assert not result.submitted
    assert result.error_kind == "popen"
    assert "synthetic failure" in result.error


def test_successful_handoff_starts_existing_watcher_source_contract():
    source = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    success = source.split("def _launch_direct_steam_url", 1)[1].split("def _start_native_steam", 1)[0]
    assert "if not result.submitted:" in success
    assert "self._start_dayz_session_watch(attempt_id=attempt_id)" in success


def test_matching_attempt_cleanup_consumes_exactly_once():
    value, _clock, _lines = tracker()
    attempt = begin(value)
    assert value.cleanup(attempt.attempt_id, "DayZ detected")
    assert not value.cleanup(attempt.attempt_id, "duplicate")


def test_watcher_timeout_cleanup_is_matching_only():
    value, _clock, _lines = tracker()
    old = begin(value)
    assert value.cleanup(old.attempt_id, "old complete")
    new = begin(value)
    assert not value.cleanup(old.attempt_id, "stale watcher timeout")
    assert value.active is new


def test_stale_callback_cannot_clear_newer_attempt():
    value, _clock, _lines = tracker()
    old = begin(value)
    value.cleanup(old.attempt_id, "old")
    new = begin(value)
    assert not value.matches(old.attempt_id)
    assert value.matches(new.attempt_id)


def test_stop_waiting_path_cleans_and_stale_continuation_cannot_launch():
    source = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    assert 'self._cleanup_active_join_attempt("stop waiting/cancel")' in source
    prepare = Path("src/dzll_launcher/join_prepare.py").read_text(encoding="utf-8")
    assert "if attempt_id and not win._join_attempt_is_active(attempt_id):" in prepare


def test_application_shutdown_invalidates_pending_callbacks():
    value, _clock, _lines = tracker()
    attempt = begin(value)
    closed = value.close()
    assert closed is attempt
    assert not value.matches(attempt.attempt_id)
    assert begin(value) is None


def test_no_automatic_retry_delay_or_second_handoff_added():
    launch_source = Path("src/dzll_launcher/launch_utils.py").read_text(encoding="utf-8")
    window_source = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    assert launch_source.count("proc = popen(") == 1
    assert "retry" not in launch_source.lower()
    join_section = window_source.split("def _join_server_for_obj", 1)[1]
    assert "Join already in progress" in join_section
    assert "time.sleep" not in join_section.split("def _prune_expired_dead", 1)[0]


class QueuedGLib:
    def __init__(self):
        self.calls = []

    def idle_add(self, callback, *args):
        self.calls.append((callback, args))
        return len(self.calls)


def worker_exception_host(*, active_attempt=1, shutting_down=False):
    scheduled = QueuedGLib()
    events = []
    host = SimpleNamespace(
        GLib=scheduled,
        _shutdown_cleanup_done=shutting_down,
        _discord=SimpleNamespace(set_menu=lambda: events.append(("discord", "menu"))),
        _join_attempt_is_active=lambda attempt_id: int(attempt_id) == int(active_attempt),
        _show_join_preparation_error=(
            lambda attempt_id, message: events.append(("error", attempt_id, message))
        ),
        _cleanup_join_attempt=(
            lambda attempt_id, reason: events.append(("cleanup", attempt_id, reason))
        ),
        _set_updating=lambda value: events.append(("updating", value)),
        _on_filter_changed=lambda **kwargs: events.append(("filter", kwargs)),
    )
    host._join_preparation_leases = {}
    if int(active_attempt) == 1:
        lease = preparation_operation_gate(host).try_acquire("foreground_join")
        host._join_preparation_leases[1] = lease
    host._schedule_preparation_reap_recovery_poll = lambda: scheduled.idle_add(
        lambda: events.append(("recovery",)) or False
    )
    host._handle_join_worker_exception = (
        lambda attempt_id, message: DZLLWindow._handle_join_worker_exception(
            host, attempt_id, message,
        )
    )
    return host, scheduled, events


def failed_future(error):
    future = Future()
    future.set_exception(error)
    return future


def test_normal_join_worker_completion_schedules_no_exception_handling():
    host, scheduled, events = worker_exception_host()
    future = Future()
    future.set_result(None)

    DZLLWindow._join_worker_future_done(host, 1, future)

    assert scheduled.calls == []
    assert events == []
    assert preparation_operation_gate(host).active_owner == ""


def test_cancelled_join_worker_releases_foreground_lease():
    host, scheduled, events = worker_exception_host()
    future = Future()
    assert future.cancel()

    DZLLWindow._join_worker_future_done(host, 1, future)

    assert preparation_operation_gate(host).active_owner == ""
    assert scheduled.calls == []
    assert events == []


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (
            steam_ugc_backend.UGCHelperReapError("helper remained alive"),
            "Steam preparation could not shut down cleanly. Join was aborted.",
        ),
        (
            RuntimeError("unexpected synthetic failure"),
            "Join preparation failed unexpectedly. Please try again.",
        ),
    ],
)
def test_uncaught_join_worker_exception_is_marshaled_and_fails_current_attempt(
        error, expected_message):
    host, scheduled, events = worker_exception_host()

    DZLLWindow._join_worker_future_done(host, 1, failed_future(error))

    assert events == []
    ui_calls = [
        (callback, args)
        for callback, args in scheduled.calls
        if args == (1, expected_message)
    ]
    assert len(ui_calls) == 1
    callback, args = ui_calls[0]
    assert args == (1, expected_message)
    assert callback(*args) is False
    assert events == [
        ("discord", "menu"),
        ("error", 1, expected_message),
        ("cleanup", 1, "uncaught Join worker exception"),
        ("updating", False),
        ("filter", {"reason": "join"}),
    ]
    gate = preparation_operation_gate(host)
    if isinstance(error, steam_ugc_backend.UGCHelperReapError):
        assert gate.blocked_reap_failure
    else:
        assert gate.active_owner == ""


def test_foreground_unconfirmed_reap_failure_blocks_later_join_until_recovery():
    class Process:
        alive = True

        def poll(self):
            return None if self.alive else 0

    process = Process()
    host, scheduled, events = worker_exception_host()
    error = steam_ugc_backend.UGCHelperReapError(
        "helper remained alive", process=process,
    )
    DZLLWindow._join_worker_future_done(host, 1, failed_future(error))
    gate = preparation_operation_gate(host)
    assert gate.blocked_reap_failure
    assert gate.active_owner == "foreground_join"
    assert shared_join_preparation_busy(host)
    assert len(scheduled.calls) == 2
    recovery_callback, recovery_args = scheduled.calls[0]
    assert recovery_callback(*recovery_args) is False
    callback, args = scheduled.calls[1]
    assert callback(*args) is False
    assert any(event[0] == "cleanup" for event in events)
    assert ("recovery",) in events
    assert shared_join_preparation_busy(host)
    process.alive = False
    assert gate.try_recover_reap_failure()
    assert not shared_join_preparation_busy(host)


def test_foreground_reader_only_failure_does_not_poison_shared_gate():
    host, scheduled, _events = worker_exception_host()
    error = steam_ugc_backend.UGCHelperReapError(
        "reader survived",
        helper_process_confirmed_dead=True,
        helper_process_may_be_alive=False,
        reader_cleanup_only=True,
    )
    DZLLWindow._join_worker_future_done(host, 1, failed_future(error))
    assert not preparation_operation_gate(host).blocked_reap_failure
    assert preparation_operation_gate(host).active_owner == ""
    callback, args = scheduled.calls[0]
    assert callback(*args) is False


def test_stale_foreground_reap_ui_does_not_suppress_recovery_lifecycle(
        monkeypatch):
    class Process:
        def poll(self):
            return None

    host, scheduled, events = worker_exception_host()
    host._background_prepare_queue = BackgroundPreparationQueue()
    host._preparation_reap_recovery_source_id = 0
    host._preparation_reap_recovery_generation = 0
    host._refresh_background_prepare_action_states = lambda: None
    host._schedule_preparation_reap_recovery_poll = lambda: (
        DZLLWindow._schedule_preparation_reap_recovery_poll(host)
    )
    host._start_preparation_reap_recovery_poll = lambda: (
        DZLLWindow._start_preparation_reap_recovery_poll(host)
    )
    host._ensure_preparation_reap_recovery_poll = lambda: (
        DZLLWindow._ensure_preparation_reap_recovery_poll(host)
    )
    host._poll_preparation_reap_recovery = lambda generation=0: (
        DZLLWindow._poll_preparation_reap_recovery(host, generation)
    )
    timers = []
    monkeypatch.setattr(
        window_module.GLib,
        "timeout_add",
        lambda delay, callback, *args: (
            timers.append((delay, callback, args)) or len(timers)
        ),
    )
    error = steam_ugc_backend.UGCHelperReapError(
        "helper unresolved", process=(process := Process()),
    )
    DZLLWindow._join_worker_future_done(host, 1, failed_future(error))
    assert preparation_operation_gate(host).blocked_reap_failure
    assert len(scheduled.calls) == 2

    host._join_attempt_is_active = lambda _attempt_id: False
    recovery_callback, recovery_args = scheduled.calls[0]
    ui_callback, ui_args = scheduled.calls[1]
    assert ui_callback(*ui_args) is False
    assert events == []
    assert recovery_callback(*recovery_args) is False
    assert len(timers) == 1
    assert preparation_operation_gate(host).blocked_reap_failure
    process.poll = lambda: 0
    _delay, timer_callback, timer_args = timers[0]
    assert timer_callback(*timer_args) is False
    assert not preparation_operation_gate(host).blocked_reap_failure


def test_stale_join_worker_exception_cannot_touch_newer_attempt():
    host, scheduled, events = worker_exception_host(active_attempt=2)

    DZLLWindow._join_worker_future_done(
        host, 1, failed_future(RuntimeError("late old failure")),
    )
    callback, args = scheduled.calls[0]
    assert callback(*args) is False
    assert events == []


def test_join_worker_exception_during_shutdown_is_not_presented():
    host, scheduled, events = worker_exception_host(shutting_down=True)

    DZLLWindow._join_worker_future_done(
        host, 1, failed_future(RuntimeError("shutdown race")),
    )
    callback, args = scheduled.calls[0]
    assert callback(*args) is False
    assert events == []


def test_cancelled_join_worker_future_is_not_an_exception_error():
    host, scheduled, events = worker_exception_host()
    future = Future()
    assert future.cancel()

    DZLLWindow._join_worker_future_done(host, 1, future)

    assert scheduled.calls == []
    assert events == []


def test_join_submission_attaches_identity_captured_future_observer():
    source = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    submission = source.split("def _join_server_for_obj", 1)[1].split(
        "def _prune_expired_dead", 1,
    )[0]
    assert "future = self._hi_executor.submit(do_prepare_and_launch)" in submission
    assert "future.add_done_callback(" in submission
    assert "owner=attempt_id" in submission
    assert "self._join_worker_future_done(" in submission
