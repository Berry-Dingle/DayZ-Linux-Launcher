from pathlib import Path

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

    def set_label(self, value):
        self.label = str(value)

    def set_visible(self, value):
        self.visible = bool(value)


class LifecycleHarness:
    def __init__(self, *, skip_launcher=True):
        self.logs = []
        self.messages = []
        self.errors = []
        self.close_count = 0
        self.steamcmd_cancel_btn = FakeButton()
        self._join_attempts = JoinAttemptTracker(log_sink=self.logs.append)
        self._join_popup_item_activity = type("Activity", (), {"clear_attempt": lambda *_: None})()
        self._pending_join_attempt_id = 0
        self._pending_server_companion_obj = None
        self._pending_last_played_obj = None
        self._pending_join_mod_ids = []
        self._pending_join_mod_names_by_id = {}
        self._steamcmd_cancel_event = None
        self._steam_client_stop_waiting_event = None
        self._steam_client_safe_cancel_requested = False
        self._steamcmd_install_in_progress = False
        self._mod_download_backend_active = ""
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

    def _hide_steamcmd_auth_overlay(self):
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
        self._pending_join_attempt_id = 0
        return True

    def _cleanup_join_attempt(self, attempt_id, reason, *, clear_pending=True):
        cleaned = self._join_attempts.cleanup(attempt_id, reason)
        if cleaned and clear_pending:
            self._clear_join_pending_state(attempt_id)
        return cleaned


for method_name in (
    "_join_popup_enter_checking",
    "_join_popup_initialize_download_counter",
    "_join_popup_note_genuine_transfer",
    "_join_popup_show_launching",
    "_join_popup_process_detected",
    "_join_popup_watcher_failure",
    "_show_join_launch_error",
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
    assert harness._join_popup_watcher_failure(attempt_id, "Synthetic launch timeout")
    assert harness.errors == ["Synthetic launch timeout"]
    assert harness.steamcmd_cancel_btn.label == "Close"
    assert harness.steamcmd_cancel_btn.visible is True
    assert not harness._join_attempts.matches(attempt_id)
    assert harness.close_count == 0


def test_immediate_launch_submission_error_remains_visible():
    harness = LifecycleHarness()
    attempt_id = harness.attempt.attempt_id
    harness._show_join_launch_error(attempt_id, "Synthetic Popen failure")
    assert harness.errors == [
        "DZLL could not submit the launch request to Steam. Synthetic Popen failure"
    ]
    assert harness.steamcmd_cancel_btn.label == "Close"
    assert harness.steamcmd_cancel_btn.visible is True


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
    assert "_hide_steamcmd_auth_overlay" not in after
    launch = WINDOW_SOURCE.split("def _launch_direct_steam_url", 1)[1]
    launch = launch.split("def _start_native_steam_for_join", 1)[0]
    assert "_start_dayz_session_watch" in launch
    assert "_hide_steamcmd_auth_overlay" not in launch


def test_watcher_process_matchers_exclude_generic_proton_and_launcher():
    launcher = WINDOW_SOURCE.split("def _dayz_launcher_running", 1)[1]
    launcher = launcher.split("def _join_watcher_ui_call", 1)[0]
    assert '"DayZ Launcher"' in launcher
    assert '"DayZLauncher"' in launcher
    assert '("Launcher")' not in launcher
    assert '"Proton"' not in launcher


def test_no_retry_or_second_handoff_added():
    assert JOIN_SOURCE.count("win._launch_direct_steam_url(") == 1
    assert "retry" not in JOIN_SOURCE.lower()


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
