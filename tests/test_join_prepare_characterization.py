from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from dzll_launcher import join_prepare
from dzll_launcher import window as window_module
from dzll_launcher.preparation_contracts import (
    JoinPopupPreparationPresenter,
    PreparationStatus,
)


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
JOIN_SOURCE = (ROOT / "src/dzll_launcher/join_prepare.py").read_text(encoding="utf-8")


class ImmediateGLib:
    @staticmethod
    def idle_add(callback, *args):
        callback(*args)
        return 1


class Widget:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class CharacterizationHarness:
    def __init__(self, *, backend="steam_client", backend_ok=True,
                 cancelled=False, initial_missing=None, final_missing=None):
        self.GLib = ImmediateGLib()
        self.threading = threading
        self.backend = backend
        self.backend_ok = backend_ok
        self.cancelled = cancelled
        self.initial_missing = list(initial_missing or [])
        self.final_missing = list(final_missing or [])
        self.missing_calls = 0
        self.active = True
        self.attempt_id = 1
        self.events = []
        self.logs = []
        self.launches = 0
        self.errors = []
        self._join_steam_start_allowed = False
        self._steamcmd_cancel_event = threading.Event()
        self._steam_client_stop_waiting_event = threading.Event()
        self._steamcmd_install_in_progress = False
        self._mod_download_backend_active = ""
        self._discord = None
        self._steamcmd_form_widgets = []
        self.steamcmd_spinner = Widget()
        self.steamcmd_login_btn = Widget()
        self.steamcmd_cancel_btn = Widget()
        self._join_preparation_presenter = JoinPopupPreparationPresenter(
            consume_event=self._steam_ugc_progress_to_overlay,
        )

    def _steam_ugc_progress_to_overlay(self, event):
        self.events.append(("ugc_event", dict(event)))

    def _join_log(self, attempt_id, event, **fields):
        assert attempt_id == self.attempt_id
        self.logs.append((event, fields))
        return True

    def _join_attempt_is_active(self, attempt_id):
        return self.active and int(attempt_id) == self.attempt_id

    def _cleanup_join_attempt(self, attempt_id, reason):
        self.events.append(("cleanup", reason))
        self.active = False

    def compute_missing_mods(self, workshop_dir, _mods):
        self.events.append(("verify", workshop_dir))
        result = self.initial_missing if self.missing_calls == 0 else self.final_missing
        self.missing_calls += 1
        return list(result)

    def _join_popup_initialize_download_counter(self, _attempt_id, mod_ids, *, backend):
        self.events.append(("counter", backend, tuple(mod_ids)))
        return True

    def _steamcmd_reset_state_for_new_run(self):
        return None

    def _free_bytes_for_path(self, _path):
        return 20 * 1024 ** 3

    def fetch_workshop_sizes_bytes(self, *_args, **_kwargs):
        return {}

    def _steamcmd_refresh_active_download_line2(self):
        return None

    def _request_steamcmd_credentials_blocking(self, **_kwargs):
        self.events.append(("steamcmd_credentials",))
        return {"ok": True, "username": "", "password": ""}

    def _show_steam_client_download_overlay(self, _status):
        self.events.append(("steam_client_overlay",))

    def _steam_ugc_progress_from_worker(self, event):
        self.events.append(("ugc_event", dict(event)))

    def _steamcmd_install_line_from_worker(self, line):
        self.events.append(("steamcmd_line", line))

    def run_steam_client_install(self, **kwargs):
        self.events.append(("steam_client", tuple(kwargs["mod_ids"])))
        if self.cancelled:
            self._steamcmd_cancel_event.set()
        return self.backend_ok

    def run_steamcmd_install(self, **kwargs):
        self.events.append(("steamcmd", tuple(kwargs["mod_ids"])))
        if self.cancelled:
            self._steamcmd_cancel_event.set()
        return self.backend_ok

    def _set_updating(self, *_args):
        return None

    def _hide_steamcmd_auth_overlay(self):
        self.events.append(("hide_overlay",))

    def _show_join_progress_overlay(self, *_args):
        return None

    def _steamcmd_overlay_render(self, *_args):
        return None

    def ensure_watch_symlinks(self, **kwargs):
        self.events.append(("symlinks", kwargs["workshop_dir"]))
        return {
            "created": [], "updated": [], "kept": [], "removed": [],
            "errors": [], "selected_paths": [],
        }

    def scan_installed_mods_in_watch_folder(self, _path):
        self.events.append(("scan_watch",))
        return []

    def bootstrap_launcher_state(self, **_kwargs):
        self.events.append(("preset",))
        return {"preset": "synthetic"}

    def _join_popup_show_launching(self, _attempt_id):
        self.events.append(("popup_launching",))
        return True

    def _launch_direct_steam_url(self, _obj, _paths, *, attempt_id):
        assert attempt_id == self.attempt_id
        self.events.append(("launch",))
        self.launches += 1
        return SimpleNamespace(submitted=True)

    def _steam_ugc_render_status(self, message, *, error=False):
        if error:
            self.errors.append(str(message))

    def _on_filter_changed(self, *_args, **_kwargs):
        return None


def run_characterized(monkeypatch, *, backend="steam_client", backend_ok=True,
                      cancelled=False, initially_ready=False, final_missing=False):
    mod = (101, "Mod 101")
    initial_missing = [] if initially_ready else [mod]
    missing_after = [mod] if final_missing else []
    win = CharacterizationHarness(
        backend=backend, backend_ok=backend_ok, cancelled=cancelled,
        initial_missing=initial_missing, final_missing=missing_after,
    )
    obj = SimpleNamespace(name="Synthetic", ip="127.0.0.1", gport=2302)
    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_args: "/before")
    monkeypatch.setattr(
        join_prepare, "_refresh_effective_workshop_dir_after_backend",
        lambda *_args: "/after",
    )
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", lambda *_args, **_kwargs: True)
    state = (
        {101: {"installed": True, "subscribed": True, "needs_update": False}}
        if initially_ready else
        {101: {"installed": False, "subscribed": False, "needs_update": False}}
    )
    monkeypatch.setattr(join_prepare, "query_ugc_state", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(
        join_prepare.steamcmd_mods, "validate_selected_watch_symlinks",
        lambda **_kwargs: win.events.append(("validate_symlinks",)) or [],
    )
    join_prepare.join_prepare_and_launch(
        win, obj, [mod], "/configured", "/steamcmd", "", False, False,
        "/prefix", "/watch", True, backend, True, False, attempt_id=1,
    )
    return win


def prepare_characterized(monkeypatch, *, mods=None, backend="steam_client",
                          backend_ok=True, cancelled=False, initially_ready=False,
                          final_missing=False):
    mod_list = [(101, "Mod 101")] if mods is None else list(mods)
    initial_missing = [] if initially_ready else list(mod_list)
    missing_after = list(mod_list) if final_missing else []
    win = CharacterizationHarness(
        backend=backend,
        backend_ok=backend_ok,
        cancelled=cancelled,
        initial_missing=initial_missing,
        final_missing=missing_after,
    )
    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_args: "/before")
    monkeypatch.setattr(
        join_prepare, "_refresh_effective_workshop_dir_after_backend",
        lambda *_args: "/after",
    )
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", lambda *_args, **_kwargs: True)
    state = {
        int(mid): {
            "installed": bool(initially_ready),
            "subscribed": bool(initially_ready),
            "needs_update": False,
        }
        for mid, _name in mod_list
    }
    monkeypatch.setattr(join_prepare, "query_ugc_state", lambda *_args, **_kwargs: state)
    outcome = join_prepare.prepare_required_mods(
        win, mod_list, "/configured", "/steamcmd", "", False, False,
        True, backend, True, False, operation_id=1,
        presenter=win._join_preparation_presenter,
        server_name="Synthetic",
    )
    return win, outcome


def event_names(win):
    return [event[0] for event in win.events]


def test_all_required_mods_ready_skips_backend_and_continues(monkeypatch):
    win = run_characterized(monkeypatch, initially_ready=True)
    assert "steam_client" not in event_names(win)
    names = event_names(win)
    assert names.index("symlinks") < names.index("validate_symlinks")
    assert names.index("validate_symlinks") < names.index("scan_watch")
    assert names.index("scan_watch") < names.index("preset")
    assert names.index("preset") < names.index("popup_launching") < names.index("launch")
    assert win.launches == 1


@pytest.mark.parametrize("backend", ["steam_client", "steamcmd"])
def test_backend_success_reaches_symlink_preset_then_launch(monkeypatch, backend):
    win = run_characterized(monkeypatch, backend=backend)
    names = event_names(win)
    backend_name = "steam_client" if backend == "steam_client" else "steamcmd"
    assert backend_name in names
    assert names.index(backend_name) < names.index("verify", 1)
    assert names.index("verify", 1) < names.index("symlinks")
    assert names.index("symlinks") < names.index("validate_symlinks")
    assert names.index("validate_symlinks") < names.index("preset")
    assert names.index("preset") < names.index("launch")
    assert ("symlinks", "/after") in win.events
    assert win.launches == 1


@pytest.mark.parametrize("backend", ["steam_client", "steamcmd"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_backend_failure_or_cancel_never_reaches_symlinks_or_launch(
        monkeypatch, backend, cancelled):
    win = run_characterized(
        monkeypatch, backend=backend, backend_ok=False, cancelled=cancelled,
    )
    assert "symlinks" not in event_names(win)
    assert "preset" not in event_names(win)
    assert "launch" not in event_names(win)
    assert win.launches == 0
    expected = "Mod download cancelled" if cancelled else "Mod download failed"
    assert any(expected in error for error in win.errors)


def test_final_filesystem_verification_failure_suppresses_continuation(monkeypatch):
    win = run_characterized(monkeypatch, final_missing=True)
    assert win.events.count(("verify", "/before")) == 1
    assert win.events.count(("verify", "/after")) == 1
    assert "symlinks" not in event_names(win)
    assert "launch" not in event_names(win)
    assert any("still missing after install" in error for error in win.errors)


def test_no_required_mods_uses_shared_outcome_and_preserves_empty_preset(monkeypatch):
    win = CharacterizationHarness()
    obj = SimpleNamespace(name="No Mods", ip="127.0.0.1", gport=2302)
    join_prepare.join_prepare_and_launch(
        win, obj, [], "/configured", "/steamcmd", "", False, False,
        "/prefix", "/watch", True, "steam_client", True, False, attempt_id=1,
    )
    names = event_names(win)
    assert "steam_client" not in names
    assert "steamcmd" not in names
    assert "symlinks" not in names
    assert names.index("scan_watch") < names.index("preset") < names.index("launch")
    assert win.launches == 1


class ConsentHarness:
    _ensure_join_steam_start_consent = window_module.DZLLWindow._ensure_join_steam_start_consent

    def __init__(self, decision):
        self.settings = {"start_steam_on_join": False}
        self.decision = decision
        self.started = 0
        self._join_steam_start_allowed = False

    def _join_log(self, *_args, **_kwargs):
        return True

    def _show_start_steam_join_consent_blocking(self):
        return self.decision

    def _start_native_steam_for_join(self):
        self.started += 1
        return True, ""

    def _set_updating(self, *_args):
        return None

    def _sync_start_steam_on_join_setting_widget(self):
        return None


@pytest.mark.parametrize("accepted", [True, False])
def test_existing_steam_start_consent_accept_and_decline(monkeypatch, accepted):
    monkeypatch.setattr(window_module, "is_native_steam_running", lambda: False)
    monkeypatch.setattr(window_module, "save_settings", lambda _settings: None)
    harness = ConsentHarness((accepted, False))
    assert harness._ensure_join_steam_start_consent(1) is accepted
    assert harness.started == (1 if accepted else 0)
    assert harness._join_steam_start_allowed is accepted


def test_server_companion_and_joined_state_remain_post_launch_only():
    entry = WINDOW_SOURCE.split("def _join_server_for_obj", 1)[1].split(
        "def _preflight_block_warning_ui_blocking", 1
    )[0]
    watcher = WINDOW_SOURCE.split("# GAME STARTED -> activate Companion", 1)[1].split(
        "# GAME STARTED -> set Discord", 1
    )[0]
    assert "self._pending_server_companion_obj = obj" in entry
    assert "set_server_companion_server" not in entry
    assert "set_server_companion_server" in watcher


def test_join_popup_and_cancellation_paths_remain_textually_unchanged():
    assert '_show_join_progress_overlay("Checking & Preparing Mods for Join...")' in WINDOW_SOURCE
    assert "_steam_client_download_cancel_clicked" in WINDOW_SOURCE
    assert "_steamcmd_cancel_event.set()" in WINDOW_SOURCE
    assert "_cleanup_subscriptions(sessions" not in JOIN_SOURCE  # remains backend-owned


def test_shared_preparation_no_required_mods_is_success_without_backend(monkeypatch):
    win, outcome = prepare_characterized(monkeypatch, mods=[])
    assert outcome.status is PreparationStatus.NO_REQUIRED_MODS
    assert outcome.verified_mods == ()
    assert "steam_client" not in event_names(win)
    assert "steamcmd" not in event_names(win)


def test_shared_preparation_all_ready_returns_verified_ready_without_backend(monkeypatch):
    win, outcome = prepare_characterized(monkeypatch, initially_ready=True)
    assert outcome.status is PreparationStatus.READY
    assert outcome.verified_mods == ((101, "Mod 101"),)
    assert outcome.effective_workshop_path == "/after"
    assert outcome.did_work is False
    assert "steam_client" not in event_names(win)


@pytest.mark.parametrize("backend", ["steam_client", "steamcmd"])
def test_shared_preparation_backend_success_returns_ready(monkeypatch, backend):
    win, outcome = prepare_characterized(monkeypatch, backend=backend)
    assert outcome.status is PreparationStatus.READY
    assert outcome.backend == backend
    assert outcome.effective_workshop_path == "/after"
    assert outcome.verified_mods == ((101, "Mod 101"),)
    assert outcome.did_work is True
    assert "symlinks" not in event_names(win)
    assert "preset" not in event_names(win)
    assert "launch" not in event_names(win)


@pytest.mark.parametrize("backend", ["steam_client", "steamcmd"])
@pytest.mark.parametrize(
    ("cancelled", "status", "message"),
    [
        (False, PreparationStatus.FAILED, "Mod download failed"),
        (True, PreparationStatus.CANCELLED, "Mod download cancelled"),
    ],
)
def test_shared_preparation_backend_non_success_maps_terminal_outcome(
        monkeypatch, backend, cancelled, status, message):
    win, outcome = prepare_characterized(
        monkeypatch, backend=backend, backend_ok=False, cancelled=cancelled,
    )
    assert outcome.status is status
    assert outcome.error == message
    assert "symlinks" not in event_names(win)
    assert "launch" not in event_names(win)


def test_shared_preparation_final_verification_failure_is_failed(monkeypatch):
    win, outcome = prepare_characterized(monkeypatch, final_missing=True)
    assert outcome.status is PreparationStatus.FAILED
    assert "Required mods still missing after install" in outcome.error
    assert "symlinks" not in event_names(win)


def test_shared_preparation_source_has_no_join_only_operations():
    shared_source = JOIN_SOURCE.split("def prepare_required_mods", 1)[1].split(
        "def join_prepare_and_launch", 1
    )[0]
    for forbidden in (
        "ensure_watch_symlinks", "bootstrap_launcher_state",
        "linux_to_win_path_under_prefix", "_launch_direct_steam_url",
    ):
        assert forbidden not in shared_source


def test_join_wrapper_calls_shared_operation_before_symlinks_and_launch():
    wrapper = JOIN_SOURCE.split("def join_prepare_and_launch", 1)[1]
    assert wrapper.index("prepare_required_mods(") < wrapper.index("ensure_watch_symlinks(")
    assert wrapper.index("ensure_watch_symlinks(") < wrapper.index("bootstrap_launcher_state(")
    assert wrapper.index("bootstrap_launcher_state(") < wrapper.index("_launch_direct_steam_url(")
