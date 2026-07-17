from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from dzll_launcher.join_attempt import JoinAttemptTracker
from dzll_launcher import join_prepare, launch_utils


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
        self._steamcmd_cancel_event = threading.Event()
        self._steam_client_stop_waiting_event = threading.Event()
        self._steamcmd_install_in_progress = False
        self._mod_download_backend_active = ""
        self._discord = None
        self._steamcmd_form_widgets = []
        self.steamcmd_spinner = FakeWidget()
        self.steamcmd_login_btn = FakeWidget()
        self.steamcmd_cancel_btn = FakeWidget()
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

    def _steamcmd_reset_state_for_new_run(self):
        self._steamcmd_cancel_event = threading.Event()

    def _free_bytes_for_path(self, _path):
        return 20 * 1024 ** 3

    def fetch_workshop_sizes_bytes(self, *_args, **_kwargs):
        return {}

    def _show_steam_client_download_overlay(self, _status):
        return None

    def _steam_ugc_progress_from_worker(self, _event):
        return None

    def _steamcmd_refresh_active_download_line2(self):
        return None

    def _set_updating(self, *_args):
        return None

    def _hide_steamcmd_auth_overlay(self):
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
    monkeypatch.setattr(join_prepare, "query_ugc_state", lambda *_args, **_kwargs: states)
    monkeypatch.setattr(join_prepare.steamcmd_mods, "validate_selected_watch_symlinks", lambda **_kwargs: [])

    def install(**kwargs):
        kwargs["log_fn"]("helper synthetic start")
        total = len(kwargs["mod_ids"])
        for index, mid in enumerate(kwargs["mod_ids"], start=1):
            kwargs["progress_cb"]({"id": mid, "ready": True, "installed": True,
                                   "completed_count": index, "total": total})
        return True

    win.run_steam_client_install = install
    join_prepare.join_prepare_and_launch(
        win, obj, mods, "/workshop", "steamcmd", "", False, False,
        "/prefix", "/watch", True, "steam_client", True, False, attempt_id=1,
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
    result = launch_utils.launch_direct_steam_url(
        LaunchWin(), OBJ, popen_factory=lambda _cmd: SimpleNamespace(pid=4321)
    )
    assert result.submitted is True
    assert result.pid == 4321
    assert not hasattr(result, "launch_confirmed")


def test_captured_launch_mode_overrides_later_setting_change(monkeypatch):
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_args: None)
    win = LaunchWin()
    win.settings["skip_dayz_launcher"] = False
    commands = []
    result = launch_utils.launch_direct_steam_url(
        win,
        OBJ,
        popen_factory=lambda command: commands.append(list(command)) or SimpleNamespace(pid=4321),
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
    def fail(_cmd):
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
    assert launch_source.count("popen(cmd)") == 1
    assert "retry" not in launch_source.lower()
    join_section = window_source.split("def _join_server_for_obj", 1)[1]
    assert "Join already in progress" in join_section
    assert "time.sleep" not in join_section.split("def _preflight_block_warning", 1)[0]
