from pathlib import Path
import threading

import pytest

from dzll_launcher import window as window_module
from dzll_launcher.join_attempt import JoinAttemptTracker


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
JOIN_SOURCE = (ROOT / "src/dzll_launcher/join_prepare.py").read_text(encoding="utf-8")


class FakeButton:
    def __init__(self):
        self.label = "Cancel"
        self.visible = False
        self.sensitive = True
        self.tooltip = ""

    def set_label(self, value):
        self.label = str(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = str(value)


class LifecycleHarness:
    def __init__(self, *, skip_launcher=True):
        self.logs = []
        self.messages = []
        self.errors = []
        self.close_count = 0
        self.join_preparation_cancel_button = FakeButton()
        self._join_attempts = JoinAttemptTracker(log_sink=self.logs.append)
        self._join_popup_item_activity = type("Activity", (), {"clear_attempt": lambda *_: None})()
        self._pending_join_attempt_id = 0
        self._pending_server_companion_obj = None
        self._pending_last_played_obj = None
        self._pending_join_mod_ids = []
        self._pending_join_mod_names_by_id = {}
        self._join_preparation_cancel_event = None
        self._steam_client_stop_waiting_event = None
        self._steam_client_safe_cancel_requested = False
        self._steam_ugc_worker_in_progress = False
        self._mod_download_backend_active = ""
        self.background_refreshes = 0
        self.attempt = self._join_attempts.begin(
            ip="127.0.0.1", game_port=2302, query_port=27016,
            name="Synthetic", skip_dayz_launcher=skip_launcher,
        )
        self._pending_join_attempt_id = self.attempt.attempt_id

    def _join_attempt_is_active(self, attempt_id):
        return self._join_attempts.matches(attempt_id)

    def _join_log(self, attempt_id, event, **fields):
        return self._join_attempts.log(attempt_id, event, **fields)

    def _show_join_progress_overlay(self, message):
        self.messages.append(str(message))
        return False

    def _hide_join_preparation_overlay(self):
        self.close_count += 1
        return False

    def _steam_ugc_render_status(self, message, *, error=False):
        if error:
            self.errors.append(str(message))
        else:
            self.messages.append(str(message))
        return False

    def _clear_join_pending_state(self, attempt_id):
        if self._pending_join_attempt_id != int(attempt_id):
            return False
        self._pending_server_companion_obj = None
        self._pending_last_played_obj = None
        self._pending_join_mod_ids = []
        self._pending_join_mod_names_by_id = {}
        self._pending_join_attempt_id = 0
        return True

    def _cleanup_join_attempt(self, attempt_id, reason, *, clear_pending=True):
        return window_module.DZLLWindow._cleanup_join_attempt(
            self, attempt_id, reason, clear_pending=clear_pending,
        )

    def _refresh_background_prepare_action_states(self):
        self.background_refreshes += 1


for method_name in (
    "_join_popup_enter_checking",
    "_join_popup_initialize_download_counter",
    "_join_popup_note_genuine_transfer",
    "_join_popup_show_launching",
    "_join_popup_process_detected",
    "_join_popup_watcher_failure",
    "_show_join_launch_error",
    "_join_preparation_cancel_clicked",
):
    setattr(LifecycleHarness, method_name, getattr(window_module.DZLLWindow, method_name))


def test_join_click_immediately_enters_required_checking_message():
    harness = LifecycleHarness()
    assert harness._join_popup_enter_checking(harness.attempt.attempt_id)
    assert harness.messages == ["Checking & Preparing Mods for Join..."]
    assert harness.attempt.popup_state == "checking"


def test_all_ready_sequence_stays_open_after_handoff_until_dayz():
    harness = LifecycleHarness(skip_launcher=True)
    attempt_id = harness.attempt.attempt_id
    harness._join_popup_enter_checking(attempt_id)
    harness._join_popup_show_launching(attempt_id)
    assert harness.messages == [
        "Checking & Preparing Mods for Join...",
        "All Mods Ready, Launching DayZ, Please Wait...",
    ]
    assert harness.close_count == 0
    assert harness._join_popup_process_detected(attempt_id, "DayZ Launcher") is False
    assert harness._join_popup_process_detected(attempt_id, "Steam") is False
    assert harness._join_popup_process_detected(attempt_id, "Proton") is False
    assert harness.close_count == 0
    assert harness._join_popup_process_detected(attempt_id, "DayZ") is True
    assert harness.close_count == 1


def test_skip_launcher_off_closes_for_launcher_or_direct_dayz():
    launcher = LifecycleHarness(skip_launcher=False)
    attempt_id = launcher.attempt.attempt_id
    launcher._join_popup_enter_checking(attempt_id)
    launcher._join_popup_show_launching(attempt_id)
    assert launcher._join_popup_process_detected(attempt_id, "DayZ Launcher")
    assert launcher.close_count == 1

    direct = LifecycleHarness(skip_launcher=False)
    direct_id = direct.attempt.attempt_id
    direct._join_popup_enter_checking(direct_id)
    direct._join_popup_show_launching(direct_id)
    assert direct._join_popup_process_detected(direct_id, "DayZ")
    assert direct.close_count == 1


def test_all_ready_skip_launcher_off_exact_sequence():
    harness = LifecycleHarness(skip_launcher=False)
    attempt_id = harness.attempt.attempt_id
    harness._join_popup_enter_checking(attempt_id)
    harness._join_popup_show_launching(attempt_id)
    assert harness.messages == [
        "Checking & Preparing Mods for Join...",
        "All Mods Ready, Launching DayZ, Please Wait...",
    ]
    assert harness._join_popup_process_detected(attempt_id, "DayZ Launcher")
    assert harness.close_count == 1


def test_popup_closes_exactly_once():
    harness = LifecycleHarness(skip_launcher=False)
    attempt_id = harness.attempt.attempt_id
    assert harness._join_popup_process_detected(attempt_id, "DayZ Launcher")
    assert not harness._join_popup_process_detected(attempt_id, "DayZ Launcher")
    assert not harness._join_popup_process_detected(attempt_id, "DayZ")
    assert harness.close_count == 1


def test_stale_process_callback_cannot_close_new_attempt():
    harness = LifecycleHarness(skip_launcher=True)
    old_id = harness.attempt.attempt_id
    assert harness._cleanup_join_attempt(old_id, "synthetic old")
    new_attempt = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="New", skip_dayz_launcher=True,
    )
    harness._pending_join_attempt_id = new_attempt.attempt_id
    assert not harness._join_popup_process_detected(old_id, "DayZ")
    assert harness.close_count == 0
    assert harness._join_attempts.active is new_attempt


def test_genuine_download_sequence_uses_downloaded_launch_message():
    harness = LifecycleHarness(skip_launcher=True)
    attempt_id = harness.attempt.attempt_id
    harness._join_popup_enter_checking(attempt_id)
    assert harness._join_popup_initialize_download_counter(
        attempt_id, [100, 200], backend="steam_client"
    )
    first = harness._join_popup_note_genuine_transfer(
        attempt_id, 100, backend="steam_client"
    )
    second = harness._join_popup_note_genuine_transfer(
        attempt_id, 200, backend="steam_client"
    )
    assert (first.status, first.transition, first.display_ordinal) == ("assigned", "first", 1)
    assert (second.status, second.transition, second.display_ordinal) == ("assigned", "changed", 2)
    harness._join_popup_show_launching(attempt_id)
    assert harness.messages[-1] == "All Mods Downloaded, Launching DayZ, Please Wait..."
    assert harness.attempt.genuine_mod_work is True
    assert harness.attempt.active_mod_id == 200
    assert harness.close_count == 0


def test_watcher_timeout_is_visible_and_cleans_matching_attempt():
    harness = LifecycleHarness()
    attempt_id = harness.attempt.attempt_id
    old_cancel_event = threading.Event()
    old_cancel_event.set()
    old_stop_event = threading.Event()
    old_stop_event.set()
    harness._join_preparation_cancel_event = old_cancel_event
    harness._steam_client_stop_waiting_event = old_stop_event
    harness._steam_client_safe_cancel_requested = True
    harness._steam_ugc_worker_in_progress = True
    harness._mod_download_backend_active = "steam_client"
    harness._pending_server_companion_obj = object()
    harness._pending_last_played_obj = object()
    harness._pending_join_mod_ids = [101]
    harness._pending_join_mod_names_by_id = {101: "Synthetic"}
    harness.join_preparation_cancel_button.set_sensitive(False)
    assert harness._join_popup_watcher_failure(attempt_id, "Synthetic launch timeout")
    assert harness.errors == ["Synthetic launch timeout"]
    assert harness.join_preparation_cancel_button.label == "Close"
    assert harness.join_preparation_cancel_button.visible is True
    assert harness.join_preparation_cancel_button.sensitive is True
    assert not harness._join_attempts.matches(attempt_id)
    assert harness._pending_join_attempt_id == 0
    assert harness._pending_server_companion_obj is None
    assert harness._pending_last_played_obj is None
    assert harness._pending_join_mod_ids == []
    assert harness._pending_join_mod_names_by_id == {}
    assert harness._join_preparation_cancel_event is not old_cancel_event
    assert not harness._join_preparation_cancel_event.is_set()
    assert harness._steam_client_stop_waiting_event is not old_stop_event
    assert not harness._steam_client_stop_waiting_event.is_set()
    assert harness._steam_client_safe_cancel_requested is False
    assert harness._steam_ugc_worker_in_progress is False
    assert harness._mod_download_backend_active == ""
    assert harness.close_count == 0

    harness._join_preparation_cancel_clicked()
    assert harness.close_count == 1


def test_fresh_join_after_timeout_gets_fresh_cancel_state():
    harness = LifecycleHarness()
    old_attempt_id = harness.attempt.attempt_id
    old_cancel_event = harness._join_preparation_cancel_event
    harness.join_preparation_cancel_button.set_sensitive(False)

    assert harness._join_popup_watcher_failure(old_attempt_id, "Synthetic timeout")
    harness._join_preparation_cancel_clicked()

    new_attempt = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="Fresh", skip_dayz_launcher=True,
    )
    harness._pending_join_attempt_id = new_attempt.attempt_id
    harness._join_preparation_cancel_event = threading.Event()
    window_module.DZLLWindow._show_join_progress_overlay(
        harness, "Checking & Preparing Mods for Join...",
    )

    assert new_attempt.attempt_id != old_attempt_id
    assert harness._join_preparation_cancel_event is not old_cancel_event
    assert not harness._join_preparation_cancel_event.is_set()
    assert harness.join_preparation_cancel_button.label == "Cancel"
    assert harness.join_preparation_cancel_button.sensitive is True


def test_immediate_launch_submission_error_remains_visible():
    harness = LifecycleHarness()
    attempt_id = harness.attempt.attempt_id
    harness._show_join_launch_error(attempt_id, "Synthetic Popen failure")
    assert harness.errors == [
        "DZLL could not submit the launch request to Steam. Synthetic Popen failure"
    ]
    assert harness.join_preparation_cancel_button.label == "Close"
    assert harness.join_preparation_cancel_button.visible is True


def test_launch_mode_is_immutable_attempt_state():
    harness = LifecycleHarness(skip_launcher=False)
    assert harness.attempt.skip_dayz_launcher is False
    # Later settings changes are intentionally irrelevant to the captured attempt.
    assert harness._join_popup_process_detected(
        harness.attempt.attempt_id, "DayZ Launcher"
    )


def test_popen_success_path_does_not_hide_popup():
    after = JOIN_SOURCE.split("def after():", 1)[1].split("win.GLib.idle_add(after)", 1)[0]
    assert "result = win._launch_direct_steam_url" in after
    assert "_hide_join_preparation_overlay" not in after
    launch = WINDOW_SOURCE.split("def _launch_direct_steam_url", 1)[1]
    launch = launch.split("def _start_native_steam_for_join", 1)[0]
    assert "_start_dayz_session_watch" in launch
    assert "_hide_join_preparation_overlay" not in launch


def test_watcher_process_matchers_use_shared_strict_snapshot():
    process_helpers = WINDOW_SOURCE.split("def _dayz_process_snapshot", 1)[1]
    process_helpers = process_helpers.split("def _join_watcher_ui_call", 1)[0]
    assert "scan_dayz_processes()" in process_helpers
    assert "pgrep" not in process_helpers
    assert "DayZ Launcher" not in process_helpers


def test_one_bounded_mod_retry_cannot_add_a_second_launch_handoff():
    assert JOIN_SOURCE.count("win._launch_direct_steam_url(") == 1
    shared = JOIN_SOURCE.split("def prepare_required_mods", 1)[1].split(
        "def join_prepare_and_launch", 1
    )[0]
    assert shared.count("retry_ok = win.run_steam_client_install(") == 1
    assert "mod_ids=unresolved_ids" in shared
    assert "prepare_required_mods(" not in shared
    assert "while " not in shared
    assert "ensure_watch_symlinks" not in shared
    assert "bootstrap_launcher_state" not in shared
    assert "_launch_direct_steam_url" not in shared
    after = JOIN_SOURCE.split("def after():", 1)[1].split(
        "win.GLib.idle_add(after)", 1
    )[0]
    assert after.index("_join_attempt_is_active") < after.index(
        "_launch_direct_steam_url"
    )


@pytest.mark.parametrize("skip_launcher,process,expected", [
    (True, "DayZ", True),
    (True, "DayZ Launcher", False),
    (False, "DayZ Launcher", True),
    (False, "DayZ", True),
    (False, "Steam", False),
    (False, "Proton", False),
])
def test_fake_process_snapshots_obey_captured_launch_mode(skip_launcher, process, expected):
    harness = LifecycleHarness(skip_launcher=skip_launcher)
    result = harness._join_popup_process_detected(harness.attempt.attempt_id, process)
    assert result is expected
    assert harness.close_count == int(expected)


class WatcherClock:
    def __init__(self, advances, *, before_sleep=None):
        self.wall = 0.0
        self.monotonic_value = 0.0
        self.advances = list(advances)
        self.before_sleep = before_sleep

    def time(self):
        return self.wall

    def monotonic(self):
        return self.monotonic_value

    def sleep(self, _seconds):
        if self.before_sleep is not None:
            self.before_sleep(self)
        if not self.advances:
            raise AssertionError("watcher performed an unexpected extra polling cycle")
        delta = float(self.advances.pop(0))
        self.wall += delta
        self.monotonic_value += delta


class IndependentWatcherClock:
    def __init__(self, wall_after_first_sleep, monotonic_advances):
        self.wall = 100.0
        self.monotonic_value = 0.0
        self.wall_after_first_sleep = wall_after_first_sleep
        self.monotonic_advances = list(monotonic_advances)
        self.sleeps = 0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.monotonic_value

    def sleep(self, _seconds):
        self.sleeps += 1
        if self.sleeps == 1:
            self.wall = self.wall_after_first_sleep
        if not self.monotonic_advances:
            raise AssertionError("watcher performed an unexpected extra polling cycle")
        self.monotonic_value += float(self.monotonic_advances.pop(0))


class SessionWatcherHarness(LifecycleHarness):
    def __init__(self, *, skip_launcher, launcher_results, game_results):
        super().__init__(skip_launcher=skip_launcher)
        self._discord_watch_lock = threading.Lock()
        self._discord_watch_active = False
        self._discord_watch_generation = 0
        self._discord_committed_watch_generation = None
        self._dayz_watch_shutdown_event = threading.Event()
        self._discord = None
        self._join_preparation_cancel_event = threading.Event()
        self._steam_client_stop_waiting_event = threading.Event()
        self.launcher_results = list(launcher_results)
        self.game_results = list(game_results)
        self.launcher_scans = 0
        self.game_scans = 0
        self.watcher_ui_calls = []
        self.cleanup_reasons = []

    @staticmethod
    def _next_result(results):
        if len(results) > 1:
            return bool(results.pop(0))
        return bool(results[0])

    def _dayz_process_snapshot(self):
        self.launcher_scans += 1
        self.game_scans += 1
        return window_module.DayZProcessSnapshot(
            launcher_running=self._next_result(self.launcher_results),
            dayz_running=self._next_result(self.game_results),
        )

    def _join_watcher_ui_call(self, callback, *args):
        self.watcher_ui_calls.append(callback.__name__)
        return callback(*args)

    def _cleanup_join_attempt(self, attempt_id, reason, *, clear_pending=True):
        self.cleanup_reasons.append(str(reason))
        return super()._cleanup_join_attempt(
            attempt_id, reason, clear_pending=clear_pending,
        )

    def _cleanup_active_join_attempt(self, reason):
        return window_module.DZLLWindow._cleanup_active_join_attempt(self, reason)


def run_session_watcher(monkeypatch, harness, clock, attempt_id=None):
    monkeypatch.setattr(window_module.time, "time", clock.time)
    monkeypatch.setattr(window_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(window_module.time, "sleep", clock.sleep)
    window_module.DZLLWindow._watch_dayz_session_until_exit(
        harness, harness.attempt.attempt_id if attempt_id is None else attempt_id,
    )


@pytest.mark.parametrize("skip_launcher", [False, True])
def test_existing_initial_timeout_remains_120_seconds(monkeypatch, skip_launcher):
    harness = SessionWatcherHarness(
        skip_launcher=skip_launcher,
        launcher_results=[False],
        game_results=[False],
    )
    clock = WatcherClock([120.0])

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.wall == 120.0
    assert harness.errors == [
        "DZLL could not detect the expected DayZ process before the launch wait timed out."
    ]
    assert harness.cleanup_reasons == ["watcher terminal failure"]
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False


@pytest.mark.parametrize("wall_after_first_sleep", [10000.0, -10000.0])
def test_initial_timeout_uses_monotonic_time_when_wall_clock_jumps(
    monkeypatch, wall_after_first_sleep,
):
    harness = SessionWatcherHarness(
        skip_launcher=True,
        launcher_results=[False],
        game_results=[False],
    )
    clock = IndependentWatcherClock(wall_after_first_sleep, [1.0] * 120)

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.monotonic_value == 120.0
    assert clock.sleeps == 120
    assert harness.cleanup_reasons == ["watcher terminal failure"]
    assert harness.errors == [
        "DZLL could not detect the expected DayZ process before the launch wait timed out."
    ]


def test_launcher_deadline_remains_monotonic_when_wall_clock_jumps(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False],
    )
    clock = IndependentWatcherClock(-10000.0, [1.0, 1799.0])

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.monotonic_value == 1800.0
    assert harness.errors == [
        "DayZ did not start after waiting 30 minutes for DayZ Launcher. "
        "DZLL stopped waiting; you can try joining again."
    ]


def test_launcher_deadline_is_absolute_and_launcher_callback_is_one_shot(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False],
    )

    def verify_below_deadline(clock):
        if clock.monotonic_value == 1799.0:
            assert harness.errors == []
            assert harness._join_attempts.active is harness.attempt

    clock = WatcherClock([121.0, 1678.0, 1.0], before_sleep=verify_below_deadline)

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.monotonic_value == 1800.0
    assert harness.launcher_scans == 4
    assert harness.game_scans == 4
    assert harness.watcher_ui_calls.count("_join_popup_process_detected") == 1
    assert harness.watcher_ui_calls.count("_join_popup_watcher_failure") == 1
    assert harness.errors == [
        "DayZ did not start after waiting 30 minutes for DayZ Launcher. "
        "DZLL stopped waiting; you can try joining again."
    ]
    assert harness.cleanup_reasons == ["watcher terminal failure"]
    assert harness._pending_join_attempt_id == 0
    assert harness._pending_server_companion_obj is None
    assert harness._discord_watch_active is False


def test_dayz_can_start_well_after_initial_timeout_before_outer_deadline(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False, True, False],
    )
    clock = WatcherClock([600.0])

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.monotonic_value == 600.0
    assert harness.errors == []
    assert harness.cleanup_reasons == ["DayZ detected"]
    assert harness._pending_join_attempt_id == 0
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False
    assert harness.watcher_ui_calls == [
        "_join_popup_process_detected",
        "_join_popup_process_detected",
    ]
    assert harness.close_count == 1


def test_launcher_exit_before_dayz_keeps_immediate_cleanup(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True, False],
        game_results=[False],
    )
    clock = WatcherClock([10.0])

    run_session_watcher(monkeypatch, harness, clock)

    assert clock.monotonic_value == 10.0
    assert harness.cleanup_reasons == ["launcher exited before DayZ"]
    assert harness.errors == []
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False


@pytest.mark.parametrize("stop_kind", ["stop-waiting", "shutdown"])
def test_attempt_invalidation_stops_launcher_watcher(monkeypatch, stop_kind):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False],
    )

    def invalidate(_clock):
        if stop_kind == "stop-waiting":
            window_module.DZLLWindow._steam_client_stop_waiting(harness)
        else:
            harness._join_attempts.close("application shutdown")

    clock = WatcherClock([1.0], before_sleep=invalidate)

    run_session_watcher(monkeypatch, harness, clock)

    assert harness.launcher_scans == 1
    assert harness.game_scans == 1
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False


def test_launcher_timeout_releases_attempt_for_subsequent_join(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False],
    )
    clock = WatcherClock([1800.0])

    run_session_watcher(monkeypatch, harness, clock)

    second = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="Second", skip_dayz_launcher=False,
    )
    assert second is not None
    assert second.attempt_id != harness.attempt.attempt_id


def test_stale_launcher_watcher_cannot_clean_new_attempt(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=False,
        launcher_results=[True],
        game_results=[False],
    )
    replacement = []

    def replace_attempt(_clock):
        assert harness._join_attempts.cleanup(harness.attempt.attempt_id, "old complete")
        replacement.append(harness._join_attempts.begin(
            ip="127.0.0.2", game_port=2402, query_port=27017,
            name="Replacement", skip_dayz_launcher=False,
        ))

    clock = WatcherClock([1800.0], before_sleep=replace_attempt)

    run_session_watcher(monkeypatch, harness, clock)

    assert replacement[0] is not None
    assert harness._join_attempts.active is replacement[0]
    assert harness.errors == []
    assert harness.cleanup_reasons == []


class PostDetectionBarrier(threading.Event):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self, _timeout):
        self.entered.set()
        return self.release.wait(2.0)

    def is_set(self):
        return self.release.is_set()

    def set(self):
        self.release.set()


def test_post_detection_monitor_does_not_block_new_watcher(monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=True, launcher_results=[False], game_results=[True],
    )
    barrier = PostDetectionBarrier()
    harness._dayz_watch_shutdown_event = barrier
    watcher_a = threading.Thread(
        target=window_module.DZLLWindow._watch_dayz_session_until_exit,
        args=(harness, harness.attempt.attempt_id), daemon=True,
    )
    watcher_a.start()
    assert barrier.entered.wait(2.0)
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False

    attempt_b = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="Second", skip_dayz_launcher=True,
    )
    assert attempt_b is not None
    harness._pending_join_attempt_id = attempt_b.attempt_id

    started = threading.Event()
    finished = threading.Event()
    seen_ids = []
    original_watcher = window_module.DZLLWindow._watch_dayz_session_until_exit

    def run_b(attempt_id):
        seen_ids.append(attempt_id)
        started.set()
        try:
            original_watcher(harness, attempt_id)
        finally:
            finished.set()

    harness._watch_dayz_session_until_exit = run_b
    monkeypatch.setattr(
        window_module.DZLLWindow, "_watch_dayz_session_until_exit", run_b,
    )
    window_module.DZLLWindow._start_dayz_session_watch(
        harness, attempt_id=attempt_b.attempt_id,
    )
    assert started.wait(2.0)
    assert seen_ids == [attempt_b.attempt_id]

    barrier.set()
    watcher_a.join(2.0)
    assert finished.wait(2.0)
    assert not watcher_a.is_alive()
    assert harness._join_attempts.active is None
    assert harness._discord_watch_active is False


def test_stale_post_detection_discord_reset_cannot_overwrite_newer_watcher(
        monkeypatch):
    harness = SessionWatcherHarness(
        skip_launcher=True, launcher_results=[False], game_results=[True, False],
    )
    queued = []
    monkeypatch.setattr(
        window_module.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )

    window_module.DZLLWindow._watch_dayz_session_until_exit(
        harness, harness.attempt.attempt_id,
    )

    reset_callbacks = [callback for callback, _args in queued
                       if callback.__name__ == "reset_discord_if_current"]
    assert len(reset_callbacks) == 1
    with harness._discord_watch_lock:
        harness._discord_committed_watch_generation = 2
    assert reset_callbacks[0]() is False


def test_shutdown_interrupts_post_detection_monitor_without_reset_callback():
    harness = SessionWatcherHarness(
        skip_launcher=True, launcher_results=[False], game_results=[True],
    )
    shutdown = PostDetectionBarrier()
    harness._dayz_watch_shutdown_event = shutdown
    watcher = threading.Thread(
        target=window_module.DZLLWindow._watch_dayz_session_until_exit,
        args=(harness, harness.attempt.attempt_id), daemon=True,
    )
    watcher.start()
    assert shutdown.entered.wait(2.0)
    harness._shutdown_cleanup_done = True
    shutdown.set()
    watcher.join(2.0)
    assert not watcher.is_alive()
    assert harness._join_attempts.active is None


class DiscordAuthorityProbe:
    def __init__(self):
        self._mode = "menus"
        self.state = "menus"
        self.menu_resets = 0
        self.playing_updates = 0

    def update(self):
        self.state = self._mode
        if self._mode == "ingame":
            self.playing_updates += 1

    def set_menu(self):
        self._mode = "menus"
        self.state = "menus"
        self.menu_resets += 1


def _authority_harness(*, game_results):
    harness = SessionWatcherHarness(
        skip_launcher=True, launcher_results=[False], game_results=game_results,
    )
    harness.settings = {"discord_detail_level": "ingame"}
    harness._discord = DiscordAuthorityProbe()
    return harness


def _cancel_attempt_before_next_poll(harness, attempt_id):
    assert harness._cleanup_join_attempt(attempt_id, "synthetic cancellation")


def test_cancelled_provisional_watcher_preserves_committed_owner(monkeypatch):
    harness = _authority_harness(game_results=[True, False])
    queued = []
    monkeypatch.setattr(
        window_module.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    run_session_watcher(monkeypatch, harness, WatcherClock([]))
    for callback, args in queued:
        if callback.__name__ == "apply":
            callback(*args)
    assert harness._discord.state == "ingame"
    assert harness._discord_committed_watch_generation == 1

    attempt_b = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="B", skip_dayz_launcher=True,
    )
    assert attempt_b is not None
    harness._pending_join_attempt_id = attempt_b.attempt_id
    harness.game_results = [False]

    reset_a = next(callback for callback, _args in queued
                   if callback.__name__ == "reset_discord_if_current")

    def reset_a_while_b_is_provisional(_clock):
        reset_a()
        _cancel_attempt_before_next_poll(harness, attempt_b.attempt_id)

    clock = WatcherClock(
        [1.0],
        before_sleep=reset_a_while_b_is_provisional,
    )
    run_session_watcher(monkeypatch, harness, clock, attempt_b.attempt_id)

    assert harness._discord_committed_watch_generation is None
    assert harness._discord.state == "menus"
    assert harness._discord.menu_resets == 1
    assert harness._join_attempts.active is None


def test_successful_new_watcher_commits_and_supersedes_old_owner(monkeypatch):
    harness = _authority_harness(game_results=[True, False])
    queued = []
    monkeypatch.setattr(
        window_module.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    run_session_watcher(monkeypatch, harness, WatcherClock([]))
    assert harness._discord_committed_watch_generation == 1

    attempt_b = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="B", skip_dayz_launcher=True,
    )
    assert attempt_b is not None
    harness._pending_join_attempt_id = attempt_b.attempt_id
    harness.game_results = [True, False]
    run_session_watcher(monkeypatch, harness, WatcherClock([]), attempt_b.attempt_id)
    assert harness._discord_committed_watch_generation == 2

    resets = [callback for callback, _args in queued
              if callback.__name__ == "reset_discord_if_current"]
    assert len(resets) == 2
    resets[0]()
    assert harness._discord.menu_resets == 0
    resets[1]()
    assert harness._discord.menu_resets == 1
    assert harness._discord_committed_watch_generation is None


def test_cancelled_watcher_without_prior_owner_leaves_no_discord_authority(
        monkeypatch):
    harness = _authority_harness(game_results=[False])
    clock = WatcherClock(
        [1.0],
        before_sleep=lambda _clock: _cancel_attempt_before_next_poll(
            harness, harness.attempt.attempt_id,
        ),
    )
    run_session_watcher(monkeypatch, harness, clock)
    assert harness._discord_committed_watch_generation is None
    assert harness._discord.state == "menus"
    assert harness._discord.menu_resets == 0


def test_cancelled_provisional_then_successful_third_watcher(monkeypatch):
    harness = _authority_harness(game_results=[True, False])
    queued = []
    monkeypatch.setattr(
        window_module.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    run_session_watcher(monkeypatch, harness, WatcherClock([]))
    assert harness._discord_committed_watch_generation == 1

    attempt_b = harness._join_attempts.begin(
        ip="127.0.0.2", game_port=2402, query_port=27017,
        name="B", skip_dayz_launcher=True,
    )
    assert attempt_b is not None
    harness._pending_join_attempt_id = attempt_b.attempt_id
    harness.game_results = [False]
    run_session_watcher(
        monkeypatch,
        harness,
        WatcherClock(
            [1.0],
            before_sleep=lambda _clock: _cancel_attempt_before_next_poll(
                harness, attempt_b.attempt_id,
            ),
        ),
        attempt_b.attempt_id,
    )
    assert harness._discord_committed_watch_generation == 1

    attempt_c = harness._join_attempts.begin(
        ip="127.0.0.3", game_port=2502, query_port=28017,
        name="C", skip_dayz_launcher=True,
    )
    assert attempt_c is not None
    harness._pending_join_attempt_id = attempt_c.attempt_id
    harness.game_results = [True, False]
    run_session_watcher(monkeypatch, harness, WatcherClock([]), attempt_c.attempt_id)
    assert harness._discord_committed_watch_generation == 3

    resets = [callback for callback, _args in queued
              if callback.__name__ == "reset_discord_if_current"]
    assert len(resets) == 2
    resets[0]()
    assert harness._discord.menu_resets == 0
    resets[1]()
    assert harness._discord.menu_resets == 1
    assert harness._discord_committed_watch_generation is None
