from pathlib import Path
from types import SimpleNamespace
import logging
import threading

import pytest

from dzll_launcher import (
    join_prepare,
    steam_client_mods,
    steam_native,
    steam_ugc_backend,
)
from dzll_launcher import window as window_module
from dzll_launcher.join_preparation_overlay_ui import SteamCMDOverlayUI
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


def test_overlay_reset_preserves_join_cancel_event_identity():
    cancel_event = threading.Event()
    cancel_event.set()
    win = SimpleNamespace(
        _steamcmd_cancel_event=cancel_event,
        _steamcmd_form_widgets=[],
        _steamcmd_heading="old",
        _steamcmd_l1="old",
        _steamcmd_l2="old",
        _steamcmd_install_in_progress=True,
        steamcmd_spinner=Widget(),
        steamcmd_task_heading=Widget(),
        steamcmd_line1=Widget(),
        steamcmd_line2=Widget(),
        _set_steamcmd_busy=lambda _busy: None,
    )
    overlay = object.__new__(SteamCMDOverlayUI)
    overlay.win = win
    overlay._steamcmd_reset_state_for_new_run()
    assert win._steamcmd_cancel_event is cancel_event
    assert cancel_event.is_set() is True


def test_join_cancel_sets_only_matching_active_attempt_event():
    cancel_event = threading.Event()
    active = SimpleNamespace(attempt_id=9)
    win = SimpleNamespace(
        _join_attempts=SimpleNamespace(active=active),
        _steamcmd_cancel_event=cancel_event,
        _steam_client_safe_cancel_requested=False,
        _steam_client_set_cancel_buttons=lambda **_kwargs: None,
        _steam_ugc_render_cancelling=lambda: None,
    )
    window_module.DZLLWindow._steam_client_download_cancel_clicked(
        win, attempt_id=8,
    )
    assert cancel_event.is_set() is False
    window_module.DZLLWindow._steam_client_download_cancel_clicked(
        win, attempt_id=9,
    )
    assert cancel_event.is_set() is True


class CharacterizationHarness:
    def __init__(self, *, backend_ok=True,
                 cancelled=False, initial_missing=None, final_missing=None):
        self.GLib = ImmediateGLib()
        self.threading = threading
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
        self.steamcmd_spinner = Widget()
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
        self.missing_calls += 1
        return list(self.final_missing)

    def _join_popup_initialize_download_counter(self, _attempt_id, mod_ids, *, backend):
        self.events.append(("counter", backend, tuple(mod_ids)))
        return True

    def _steamcmd_reset_state_for_new_run(self):
        return None

    def _free_bytes_for_path(self, _path):
        return 20 * 1024 ** 3

    def _show_steam_client_download_overlay(self, _status):
        self.events.append(("steam_client_overlay",))

    def _steam_ugc_progress_from_worker(self, event):
        self.events.append(("ugc_event", dict(event)))

    def run_steam_client_install(self, **kwargs):
        assert kwargs["stop_waiting_event"] is self._steam_client_stop_waiting_event
        self.events.append(("steam_client", tuple(kwargs["mod_ids"])))
        if self.cancelled:
            self._steamcmd_cancel_event.set()
        return self.backend_ok

    def _set_updating(self, *_args):
        return None

    def _hide_steamcmd_auth_overlay(self):
        self.events.append(("hide_overlay",))

    def _show_join_progress_overlay(self, *_args):
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


def run_characterized(monkeypatch, *, backend_ok=True,
                      cancelled=False, initially_ready=False, final_missing=False,
                      free_bytes=20 * 1024 ** 3):
    mod = (101, "Mod 101")
    initial_missing = [] if initially_ready else [mod]
    missing_after = [mod] if final_missing else []
    win = CharacterizationHarness(
        backend_ok=backend_ok, cancelled=cancelled,
        initial_missing=initial_missing, final_missing=missing_after,
    )
    win._free_bytes_for_path = lambda _path: free_bytes
    obj = SimpleNamespace(name="Synthetic", ip="127.0.0.1", gport=2302)
    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_args: "/before")
    monkeypatch.setattr(
        join_prepare, "_refresh_effective_workshop_dir_after_backend",
        lambda *_args: "/after",
    )
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", lambda *_args, **_kwargs: True)
    state = (
        {101: {
            "installed": True, "subscribed": True, "needs_update": False,
            "downloading": False, "download_pending": False,
        }}
        if initially_ready else
        {101: {
            "installed": False, "subscribed": False, "needs_update": False,
            "downloading": False, "download_pending": False,
        }}
    )
    terminal_state = {
        101: {
            "installed": True, "subscribed": True, "needs_update": False,
            "downloading": False, "download_pending": False,
        },
    }
    state_results = iter(((True, state), (True, terminal_state)))
    monkeypatch.setattr(
        join_prepare, "refresh_subscribed_ugc_state_checked",
        lambda *_args, **_kwargs: (*next(state_results), {}),
    )
    monkeypatch.setattr(
        join_prepare, "query_ugc_state_checked",
        lambda *_args, **_kwargs: next(state_results),
    )
    monkeypatch.setattr(
        join_prepare.workshop_mods, "validate_selected_watch_symlinks",
        lambda **_kwargs: win.events.append(("validate_symlinks",)) or [],
    )
    join_prepare.join_prepare_and_launch(
        win, obj, [mod], "/configured", "/prefix", "/watch", True, True,
        attempt_id=1,
    )
    return win


def prepare_characterized(monkeypatch, *, mods=None,
                          backend_ok=True, cancelled=False, initially_ready=False,
                          final_missing=False, use_mod_management=True):
    mod_list = [(101, "Mod 101")] if mods is None else list(mods)
    initial_missing = [] if initially_ready else list(mod_list)
    missing_after = list(mod_list) if final_missing else []
    win = CharacterizationHarness(
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
            "downloading": False,
            "download_pending": False,
        }
        for mid, _name in mod_list
    }
    terminal_state = {
        int(mid): {
            "installed": True,
            "subscribed": True,
            "needs_update": False,
            "downloading": False,
            "download_pending": False,
        }
        for mid, _name in mod_list
    }
    state_results = iter(((True, state), (True, terminal_state)))
    monkeypatch.setattr(
        join_prepare, "refresh_subscribed_ugc_state_checked",
        lambda *_args, **_kwargs: (*next(state_results), {}),
    )
    monkeypatch.setattr(
        join_prepare, "query_ugc_state_checked",
        lambda *_args, **_kwargs: next(state_results),
    )
    outcome = join_prepare.prepare_required_mods(
        win, mod_list, "/configured", use_mod_management, True, operation_id=1,
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


def test_ugc_backend_success_reaches_symlink_preset_then_launch(monkeypatch):
    win = run_characterized(monkeypatch)
    names = event_names(win)
    assert "steam_client" in names
    assert names.index("steam_client") < names.index("verify", 1)
    assert names.index("verify", 1) < names.index("symlinks")
    assert names.index("symlinks") < names.index("validate_symlinks")
    assert names.index("validate_symlinks") < names.index("preset")
    assert names.index("preset") < names.index("launch")
    assert ("symlinks", "/after") in win.events
    assert win.launches == 1


@pytest.mark.parametrize("cancelled", [False, True])
def test_ugc_backend_failure_or_cancel_never_reaches_symlinks_or_launch(
        monkeypatch, cancelled):
    win = run_characterized(
        monkeypatch, backend_ok=False, cancelled=cancelled,
    )
    assert "symlinks" not in event_names(win)
    assert "preset" not in event_names(win)
    assert "launch" not in event_names(win)
    assert win.launches == 0
    expected = "Mod download cancelled" if cancelled else "Mod download failed"
    assert any(expected in error for error in win.errors)


def test_foreground_preset_cancel_stops_before_steam_links_and_launch(monkeypatch):
    mod = (101, "Mod 101")
    win = CharacterizationHarness(initial_missing=[mod])
    win._steamcmd_cancel_event.set()
    obj = SimpleNamespace(name="Cancelled", ip="127.0.0.1", gport=2302)
    steam_calls = []

    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_a: "/workshop")
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(
        steam_native, "is_flatpak_steam_running",
        lambda: steam_calls.append("flatpak") or False,
    )
    monkeypatch.setattr(
        steam_native, "resolve_native_steam_cmd",
        lambda: steam_calls.append("resolve") or "/native/steam",
    )
    monkeypatch.setattr(
        steam_native, "is_native_steam_running",
        lambda: steam_calls.append("running") or False,
    )
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen",
        lambda *_a, **_k: steam_calls.append("popen") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines",
        lambda *_a, **_k: steam_calls.append("helper") or (True, 0),
    )

    join_prepare.join_prepare_and_launch(
        win, obj, [mod], "/workshop", "/prefix", "/watch", True, True,
        attempt_id=1,
    )

    assert steam_calls == []
    assert not {
        "steam_client", "symlinks", "preset", "launch",
    } & set(event_names(win))
    assert win.launches == 0
    assert any("cancel" in error.lower() for error in win.errors)


def test_cancel_closes_cooperative_context_before_fresh_cleanup(monkeypatch):
    order = []

    class FakeSession:
        def close(self):
            order.extend(("steamapi_shutdown", "helper_exited"))

    monkeypatch.setattr(
        join_prepare, "CooperativeUGCSession", lambda **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(
        join_prepare, "cleanup_cancelled_ugc_subscriptions",
        lambda ids: order.append(("fresh_cleanup", list(ids))) or {
            "candidates": list(ids), "attempted": list(ids),
            "confirmed_unsubscribed": list(ids), "retained_installed": [],
            "already_unsubscribed": [], "failed": [], "timed_out": [],
        },
    )

    def cancelled_install(self, **kwargs):
        order.append("cooperative_cancel_result")
        kwargs["handoff_cb"]({"cleanup_candidates": [101]})
        self._steamcmd_cancel_event.set()
        return False

    monkeypatch.setattr(
        CharacterizationHarness, "run_steam_client_install", cancelled_install,
    )
    _win, outcome = prepare_characterized(
        monkeypatch, backend_ok=False, cancelled=True,
    )
    assert outcome.status is PreparationStatus.CANCELLED
    assert order == [
        "cooperative_cancel_result",
        "steamapi_shutdown",
        "helper_exited",
        ("fresh_cleanup", [101]),
    ]


def test_terminal_cancel_race_exports_before_close_then_fresh_cleanup(
        monkeypatch):
    order = []
    initial = {
        "type": "item", "id": 101, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False,
        "state_names": [],
    }

    class FakeSession:
        def close(self):
            order.extend(("steamapi_shutdown", "helper_exited"))

    monkeypatch.setattr(
        join_prepare, "CooperativeUGCSession", lambda **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "active_ugc_session", lambda: None,
    )

    def helper(command, **kwargs):
        callback = kwargs.get("on_event")
        if command == "state":
            callback(dict(initial))
            return True, 0
        if command == "subscribe-download":
            callback({
                **initial, "type": "request",
                "subscribe_call_result": 1,
                "download_requested": True,
            })
            callback({
                "type": "command_result", "ok": False,
                "reason": "timeout",
            })
            order.append("normal_terminal_result")
            kwargs["cancel_event"].set()
            return False, 0
        if command == "unsubscribe":
            pytest.fail("downloader context must not unsubscribe directly")
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    def run_install(self, **kwargs):
        def record_handoff(handoff):
            order.append((
                "handoff", list(handoff["cleanup_candidates"]),
            ))
            kwargs["handoff_cb"](handoff)

        return steam_ugc_backend.run_ugc_install(
            kwargs["mod_ids"],
            cancel_event=kwargs["cancel_event"],
            handoff_cb=record_handoff,
            progress_cb=kwargs.get("progress_cb"),
            allow_start_steam=False,
        )

    monkeypatch.setattr(
        CharacterizationHarness, "run_steam_client_install", run_install,
    )
    monkeypatch.setattr(
        join_prepare, "cleanup_cancelled_ugc_subscriptions",
        lambda ids: order.append(("fresh_cleanup", list(ids))) or {
            "candidates": list(ids), "attempted": list(ids),
            "confirmed_unsubscribed": list(ids), "retained_installed": [],
            "already_unsubscribed": [], "failed": [], "timed_out": [],
        },
    )

    _win, outcome = prepare_characterized(
        monkeypatch, backend_ok=False, cancelled=True,
    )
    assert outcome.status is PreparationStatus.CANCELLED
    assert order == [
        "normal_terminal_result",
        ("handoff", [101]),
        "steamapi_shutdown",
        "helper_exited",
        ("fresh_cleanup", [101]),
    ]


def test_cancel_skips_fresh_cleanup_without_confirmed_shutdown(monkeypatch):
    class FailedSession:
        def close(self):
            steam_ugc_backend.unregister_owned_ugc_session(self)
            raise RuntimeError("shutdown not confirmed")

    monkeypatch.setattr(
        join_prepare, "CooperativeUGCSession", lambda **_kwargs: FailedSession(),
    )
    monkeypatch.setattr(
        join_prepare, "cleanup_cancelled_ugc_subscriptions",
        lambda _ids: pytest.fail("fresh context must not overlap failed shutdown"),
    )

    def cancelled_install(self, **kwargs):
        kwargs["handoff_cb"]({"cleanup_candidates": [101]})
        self._steamcmd_cancel_event.set()
        return False

    monkeypatch.setattr(
        CharacterizationHarness, "run_steam_client_install", cancelled_install,
    )
    _win, outcome = prepare_characterized(
        monkeypatch, backend_ok=False, cancelled=True,
    )
    assert outcome.status is PreparationStatus.CANCELLED


def test_cancel_handoff_collector_rejects_coercible_item_ids(monkeypatch):
    cleanup_calls = []

    class CoercibleItemId:
        def __int__(self):
            return 303

    class FakeSession:
        def close(self):
            return None

    monkeypatch.setattr(
        join_prepare, "CooperativeUGCSession", lambda **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(
        join_prepare, "cleanup_cancelled_ugc_subscriptions",
        lambda ids: cleanup_calls.append(list(ids)) or {
            "candidates": list(ids), "attempted": list(ids),
            "confirmed_unsubscribed": list(ids), "retained_installed": [],
            "already_unsubscribed": [], "failed": [], "timed_out": [],
        },
    )

    def cancelled_install(self, **kwargs):
        kwargs["handoff_cb"]({
            "cleanup_candidates": [
                101, 202.9, "303", True, CoercibleItemId(),
            ],
        })
        self._steamcmd_cancel_event.set()
        return False

    monkeypatch.setattr(
        CharacterizationHarness, "run_steam_client_install",
        cancelled_install,
    )
    _win, outcome = prepare_characterized(
        monkeypatch, backend_ok=False, cancelled=True,
    )
    assert outcome.status is PreparationStatus.CANCELLED
    assert cleanup_calls == [[101]]


def test_initial_and_terminal_retry_calls_both_collect_cancel_handoff():
    assert JOIN_SOURCE.count(
        "handoff_cb=collect_cancel_cleanup_handoff"
    ) == 2


def test_late_ready_result_after_join_cancel_cannot_continue(monkeypatch):
    win = CharacterizationHarness()
    obj = SimpleNamespace(name="Cancelled", ip="127.0.0.1", gport=2302)

    def late_ready(_win, *_args, **_kwargs):
        win._steamcmd_cancel_event.set()
        return join_prepare.PreparationOutcome(
            PreparationStatus.READY,
            reason="ready",
            effective_workshop_path="/workshop",
            verified_mods=[(101, "Required")],
        )

    monkeypatch.setattr(join_prepare, "prepare_required_mods", late_ready)
    join_prepare.join_prepare_and_launch(
        win, obj, [(101, "Required")], "/workshop", "/prefix", "/watch",
        True, True, attempt_id=1,
    )
    assert "symlinks" not in event_names(win)
    assert "preset" not in event_names(win)
    assert "launch" not in event_names(win)
    assert win.launches == 0
    assert any("cancel" in error.lower() for error in win.errors)


def test_final_filesystem_verification_failure_suppresses_continuation(monkeypatch):
    win = run_characterized(monkeypatch, final_missing=True)
    assert win.events.count(("verify", "/after")) == 1
    assert "symlinks" not in event_names(win)
    assert "launch" not in event_names(win)
    assert any("still missing after install" in error for error in win.errors)


def test_no_required_mods_uses_shared_outcome_and_preserves_empty_preset(monkeypatch):
    win = CharacterizationHarness()
    obj = SimpleNamespace(name="No Mods", ip="127.0.0.1", gport=2302)
    join_prepare.join_prepare_and_launch(
        win, obj, [], "/configured", "/prefix", "/watch", True, True,
        attempt_id=1,
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

    def _show_start_steam_join_consent_blocking(self, *, caller="join"):
        assert caller == "join"
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
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: False)
    monkeypatch.setattr(window_module, "save_settings", lambda _settings: None)
    harness = ConsentHarness((accepted, False))
    result = harness._ensure_join_steam_start_consent(1)
    assert bool(result) is accepted
    assert result.steam_start_submitted is accepted
    assert harness.started == (1 if accepted else 0)
    assert harness._join_steam_start_allowed is accepted


def test_server_companion_and_joined_state_remain_post_launch_only():
    entry = WINDOW_SOURCE.split("def _join_server_for_obj", 1)[1].split(
        "def _prune_expired_dead", 1
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


def test_master_toggle_still_disables_dzll_mod_preparation(monkeypatch):
    win, outcome = prepare_characterized(
        monkeypatch, use_mod_management=False,
    )
    assert outcome.status is PreparationStatus.READY
    assert "steam_client" not in event_names(win)
    assert not any(event == "initial_query" for event, _fields in win.logs)


def test_no_steamcmd_backend_selector_or_dispatch_remains():
    assert 'backend = "steam_client"' in JOIN_SOURCE
    assert '"mod_download_backend"' not in JOIN_SOURCE
    assert "run_steamcmd_install" not in JOIN_SOURCE


def test_shared_preparation_ugc_success_returns_ready(monkeypatch):
    win, outcome = prepare_characterized(monkeypatch)
    assert outcome.status is PreparationStatus.READY
    assert outcome.backend == "steam_client"
    assert outcome.effective_workshop_path == "/after"
    assert outcome.verified_mods == ((101, "Mod 101"),)
    assert outcome.did_work is True
    assert "symlinks" not in event_names(win)
    assert "preset" not in event_names(win)
    assert "launch" not in event_names(win)


@pytest.mark.parametrize(
    ("cancelled", "status", "message"),
    [
        (False, PreparationStatus.FAILED, "Mod download failed"),
        (True, PreparationStatus.CANCELLED, "Mod download cancelled"),
    ],
)
def test_shared_preparation_ugc_non_success_maps_terminal_outcome(
        monkeypatch, cancelled, status, message):
    win, outcome = prepare_characterized(
        monkeypatch, backend_ok=False, cancelled=cancelled,
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


def test_low_disk_foreground_ugc_failure_is_rendered_and_cannot_launch(monkeypatch):
    terminal_outcomes = []

    class RecordingPresenter:
        def __init__(self, **_kwargs):
            pass

        def on_event(self, _event):
            pass

        def on_terminal(self, outcome):
            terminal_outcomes.append(outcome)

    monkeypatch.setattr(
        join_prepare, "JoinPopupPreparationPresenter", RecordingPresenter,
    )
    win = run_characterized(
        monkeypatch,
        free_bytes=4 * 1024 ** 3,
    )

    message = "Not enough free disk space in workshop drive (4.0 GB free)."
    assert terminal_outcomes[-1].status is PreparationStatus.FAILED
    assert terminal_outcomes[-1].error == message
    assert win.errors and set(win.errors) == {message}
    assert ("steam_client_overlay",) in win.events
    assert any(event[0] == "counter" for event in win.events)
    assert "steam_client" not in event_names(win)
    assert "launch" not in event_names(win)


@pytest.mark.parametrize("backend_ok", [False, True], ids=["operation-failed", "ready"])
def test_ugc_teardown_error_is_secondary_to_operation_or_confirmed_success(
        monkeypatch, capsys, backend_ok):
    class TeardownFailingSession:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            steam_ugc_backend.unregister_owned_ugc_session(self)
            raise RuntimeError("synthetic teardown failure")

    monkeypatch.setattr(join_prepare, "CooperativeUGCSession", TeardownFailingSession)
    win, outcome = prepare_characterized(
        monkeypatch, backend_ok=backend_ok,
    )
    captured = capsys.readouterr()
    assert "Secondary cleanup diagnostic" in captured.err
    if backend_ok:
        assert outcome.status is PreparationStatus.READY
        assert not outcome.error
    else:
        assert outcome.status is PreparationStatus.FAILED
        assert outcome.error == "Mod download failed"
    assert "shutdown failed" not in str(outcome.error or "").lower()


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


def ugc_state(*, installed=True, needs_update=False, downloading=False, pending=False):
    return {
        "installed": installed,
        "subscribed": installed,
        "needs_update": needs_update,
        "downloading": downloading,
        "download_pending": pending,
    }


def run_terminal_validation_route(
        monkeypatch, *, mods, initial_states, terminal_results,
        backend_results=(), final_missing=None, background=False,
        refresh_details=None, initial_ok=True, backend_install=None,
        ownership_probe=None):
    win = CharacterizationHarness(
        initial_missing=[],
        final_missing=list(final_missing or []),
    )
    obj = SimpleNamespace(name="Terminal Validation", ip="127.0.0.1", gport=2302)
    trace = []
    backend_results = list(backend_results)
    checked_results = [(bool(initial_ok), dict(initial_states)), *list(terminal_results)]
    checked_call_count = 0

    monkeypatch.setattr(join_prepare, "_choose_initial_workshop_dir", lambda *_args: "/workshop")
    monkeypatch.setattr(
        join_prepare, "_refresh_effective_workshop_dir_after_backend",
        lambda *_args: "/workshop",
    )
    monkeypatch.setattr(join_prepare, "dayz_paths_summary", lambda: {})
    monkeypatch.setattr(join_prepare, "wait_for_ugc_ready", lambda *_args, **_kwargs: True)

    def checked_query(ids, **_kwargs):
        nonlocal checked_call_count
        phase = "initial_query" if checked_call_count == 0 else "terminal_query"
        checked_call_count += 1
        if callable(ownership_probe):
            ownership_probe(phase)
        trace.append((phase, tuple(ids)))
        assert checked_results
        return checked_results.pop(0)

    def refreshed_query(ids, **kwargs):
        ok, states = checked_query(ids, **kwargs)
        return ok, states, dict(refresh_details or {})

    def install(**kwargs):
        assert kwargs.pop("stop_waiting_event") is win._steam_client_stop_waiting_event
        if callable(ownership_probe):
            ownership_probe("backend")
        trace.append(("backend", tuple(kwargs["mod_ids"])))
        if callable(backend_install):
            return bool(backend_install(kwargs))
        assert backend_results
        result = backend_results.pop(0)
        if result == "cancel":
            win._steamcmd_cancel_event.set()
            return False
        return bool(result)

    monkeypatch.setattr(
        join_prepare, "refresh_subscribed_ugc_state_checked", refreshed_query,
    )
    monkeypatch.setattr(join_prepare, "query_ugc_state_checked", checked_query)
    monkeypatch.setattr(
        join_prepare.workshop_mods, "validate_selected_watch_symlinks",
        lambda **_kwargs: trace.append(("validate_symlinks",)) or [],
    )
    win.run_steam_client_install = install

    original_symlinks = win.ensure_watch_symlinks
    original_preset = win.bootstrap_launcher_state
    original_launch = win._launch_direct_steam_url

    def symlinks(**kwargs):
        trace.append(("symlinks",))
        return original_symlinks(**kwargs)

    def preset(**kwargs):
        trace.append(("preset",))
        return original_preset(**kwargs)

    def launch(*args, **kwargs):
        trace.append(("launch",))
        return original_launch(*args, **kwargs)

    win.ensure_watch_symlinks = symlinks
    win.bootstrap_launcher_state = preset
    win._launch_direct_steam_url = launch

    if background:
        presenter = JoinPopupPreparationPresenter(consume_event=win._steam_ugc_progress_to_overlay)
        outcome = join_prepare.prepare_required_mods(
            win, mods, "/workshop", True, True,
            presenter=presenter,
            server_name="Terminal Validation",
            manage_join_presence=False,
            manage_join_presentation=False,
        )
    else:
        join_prepare.join_prepare_and_launch(
            win, obj, mods, "/workshop", "/prefix", "/watch", True, True,
            attempt_id=1,
        )
        outcome = None
    assert not backend_results
    assert not checked_results
    return win, trace, outcome


def test_outdated_mod_requires_fresh_terminal_currentness_before_launch(monkeypatch):
    mod = (101, "Outdated")
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[mod],
        initial_states={101: ugc_state(needs_update=True)},
        backend_results=[True],
        terminal_results=[(True, {101: ugc_state()})],
    )
    assert trace == [
        ("initial_query", (101,)),
        ("backend", (101,)),
        ("terminal_query", (101,)),
        ("symlinks",),
        ("validate_symlinks",),
        ("preset",),
        ("launch",),
    ]
    assert win.launches == 1


def test_locally_present_mod_still_runs_steam_install_when_ugc_reports_update(
        monkeypatch):
    mod = (101, "Locally Present But Stale")
    ugc_install_calls = []

    def run_ugc_install(ids, **_kwargs):
        ugc_install_calls.append(tuple(ids))
        return True

    monkeypatch.setattr(
        steam_client_mods, "run_ugc_install", run_ugc_install,
    )
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[mod],
        # The harness reports no local filesystem miss. Steam remains
        # authoritative for whether the Workshop item needs update work.
        initial_states={101: ugc_state(needs_update=True)},
        terminal_results=[(True, {101: ugc_state()})],
        backend_install=lambda kwargs: (
            steam_client_mods.run_steam_client_install(**kwargs)
        ),
    )

    assert trace[:3] == [
        ("initial_query", (101,)),
        ("backend", (101,)),
        ("terminal_query", (101,)),
    ]
    assert ugc_install_calls == [(101,)]
    assert win.launches == 1


def test_single_retry_uses_only_unresolved_ids_then_validates_every_original_id(
        monkeypatch):
    mods = [
        (1, "Current"), (2, "Missing"), (3, "Outdated"), (4, "Pending"),
    ]
    first_terminal = {
        1: ugc_state(),
        2: ugc_state(),
        3: ugc_state(needs_update=True),
        4: ugc_state(pending=True),
    }
    final_terminal = {mid: ugc_state() for mid in range(1, 5)}
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=mods,
        initial_states={
            1: ugc_state(),
            2: ugc_state(installed=False),
            3: ugc_state(needs_update=True),
            4: ugc_state(pending=True),
        },
        backend_results=[True, True],
        terminal_results=[(True, first_terminal), (True, final_terminal)],
    )
    assert trace[:5] == [
        ("initial_query", (1, 2, 3, 4)),
        ("backend", (2, 3, 4)),
        ("terminal_query", (1, 2, 3, 4)),
        ("backend", (3, 4)),
        ("terminal_query", (1, 2, 3, 4)),
    ]
    assert trace[5:] == [
        ("symlinks",), ("validate_symlinks",), ("preset",), ("launch",),
    ]
    retry_events = [
        event[1] for event in win.events
        if (
            len(event) == 2
            and event[0] == "ugc_event"
            and "Retrying once" in str(event[1].get("message") or "")
        )
    ]
    assert len(retry_events) == 1
    assert win.launches == 1


def test_owned_session_is_continuous_through_terminal_validation_and_retry(
        monkeypatch):
    registered = []
    ownership_phases = []
    transitions = {"register": 0, "unregister": 0}
    original_register = steam_ugc_backend.register_owned_ugc_session
    original_unregister = steam_ugc_backend.unregister_owned_ugc_session

    def register(session):
        was_owned = session in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        original_register(session)
        registered.append(session)
        transitions["register"] += int(not was_owned)

    def unregister(session):
        was_owned = session in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        original_unregister(session)
        transitions["unregister"] += int(was_owned)

    def probe(phase):
        assert len(registered) == 1
        assert registered[0] in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        ownership_phases.append(phase)

    monkeypatch.setattr(join_prepare, "register_owned_ugc_session", register)
    monkeypatch.setattr(join_prepare, "unregister_owned_ugc_session", unregister)
    monkeypatch.setattr(
        steam_ugc_backend, "unregister_owned_ugc_session", unregister,
    )
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Required")],
        initial_states={101: ugc_state(needs_update=True)},
        backend_results=[True, True],
        terminal_results=[
            (True, {101: ugc_state(needs_update=True)}),
            (True, {101: ugc_state()}),
        ],
        ownership_probe=probe,
    )
    assert ownership_phases == [
        "initial_query", "backend", "terminal_query", "backend",
        "terminal_query",
    ]
    assert [item for item in trace if item[0] == "backend"] == [
        ("backend", (101,)), ("backend", (101,)),
    ]
    assert transitions == {"register": 1, "unregister": 1}
    assert registered[0] not in steam_ugc_backend._ACTIVE_UGC_SESSIONS
    assert win.launches == 1


def test_outer_activation_failure_closes_owned_session_without_clearing_other(
        monkeypatch):
    other = object()
    registered = []
    original_register = join_prepare.register_owned_ugc_session

    def register(session):
        registered.append(session)
        original_register(session)

    monkeypatch.setattr(join_prepare, "register_owned_ugc_session", register)
    steam_ugc_backend.activate_ugc_session(other)
    try:
        _win, outcome = prepare_characterized(monkeypatch)
        assert outcome.status is PreparationStatus.FAILED
        assert "already active" in str(outcome.error)
        assert len(registered) == 1
        assert registered[0]._owned_resources_release_confirmed()
        assert registered[0] not in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        assert steam_ugc_backend.active_ugc_session() is other
    finally:
        steam_ugc_backend.deactivate_ugc_session(other)


@pytest.mark.parametrize(
    ("final_ok", "final_states"),
    [
        (True, {101: ugc_state(needs_update=True)}),
        (True, {101: ugc_state(downloading=True)}),
        (True, {101: ugc_state(pending=True)}),
        (True, {}),
        (False, {}),
        (True, {101: {"installed": True}}),
    ],
    ids=[
        "needs-update", "downloading", "pending", "missing-state",
        "query-failure", "malformed-state",
    ],
)
def test_single_retry_final_noncurrent_state_fails_closed(
        monkeypatch, final_ok, final_states):
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Required")],
        initial_states={101: ugc_state()},
        backend_results=[True],
        terminal_results=[
            (True, {101: ugc_state(needs_update=True)}),
            (final_ok, final_states),
        ],
    )
    assert [item for item in trace if item[0] == "backend"] == [("backend", (101,))]
    assert len([item for item in trace if item[0] == "terminal_query"]) == 2
    assert not {"symlinks", "preset", "launch"} & {item[0] for item in trace}
    assert win.launches == 0
    assert any("Steam still reports" in error for error in win.errors)


@pytest.mark.parametrize(
    ("retry_result", "expected_message"),
    [(False, "Steam still reports"), ("cancel", "cancelled")],
)
def test_single_retry_backend_failure_or_cancellation_never_continues(
        monkeypatch, retry_result, expected_message):
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Required")],
        initial_states={101: ugc_state()},
        backend_results=[retry_result],
        terminal_results=[(True, {101: ugc_state(needs_update=True)})],
    )
    assert [item for item in trace if item[0] == "backend"] == [("backend", (101,))]
    assert len([item for item in trace if item[0] == "terminal_query"]) == 1
    assert not {"symlinks", "preset", "launch"} & {item[0] for item in trace}
    assert win.launches == 0
    assert any(expected_message.lower() in error.lower() for error in win.errors)


def test_initially_ready_mods_are_revalidated_before_ready(monkeypatch):
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Current")],
        initial_states={101: ugc_state()},
        terminal_results=[(True, {101: ugc_state()})],
    )
    assert trace == [
        ("initial_query", (101,)),
        ("terminal_query", (101,)),
        ("symlinks",),
        ("validate_symlinks",),
        ("preset",),
        ("launch",),
    ]
    assert win.launches == 1


@pytest.mark.parametrize("detail_key", ["failed", "timed_out"])
def test_partial_subscribed_refresh_failure_is_non_blocking_and_does_not_download(
        monkeypatch, caplog, detail_key):
    details = {
        "subscribed": [101],
        "refreshed": [],
        "failed": [],
        "timed_out": [],
        "failures": [],
    }
    details[detail_key] = [101]
    if detail_key == "failed":
        details["failures"] = [{"id": 101, "reason": "api_call_failed"}]
    with caplog.at_level(logging.DEBUG, logger=join_prepare.__name__):
        win, trace, _outcome = run_terminal_validation_route(
            monkeypatch,
            mods=[(101, "Current")],
            initial_states={101: ugc_state()},
            terminal_results=[(True, {101: ugc_state()})],
            refresh_details=details,
        )
    assert not [item for item in trace if item[0] == "backend"]
    assert trace[-1] == ("launch",)
    assert win.launches == 1
    assert not any(
        "subscribed metadata refresh was incomplete" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        (
            "Steam UGC metadata refresh partial: refreshed=0/1, "
            f"failed={int(detail_key == 'failed')}, "
            f"timed_out={int(detail_key == 'timed_out')}; "
            "using complete final state"
        ) in record.getMessage()
        for record in caplog.records
    )
    reason_text = "failure_reasons={api_call_failed: 1}"
    assert any(reason_text in record.getMessage() for record in caplog.records) is (
        detail_key == "failed"
    )


def test_initial_ugc_protocol_failure_cannot_be_classified_as_missing(
        monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=join_prepare.__name__):
        win, trace, _outcome = run_terminal_validation_route(
            monkeypatch,
            mods=[(101, "Unknown")],
            initial_states={},
            terminal_results=[],
            initial_ok=False,
        )
    assert trace == [("initial_query", (101,))]
    assert not [item for item in trace if item[0] == "backend"]
    assert win.launches == 0
    assert any(
        (
            "Steam UGC authoritative final state incomplete: received=0/1; "
            "preparation will fail closed"
        ) in record.getMessage()
        for record in caplog.records
    )


def test_refreshed_mixed_states_preserve_existing_work_classification(
        monkeypatch):
    states = {
        1: ugc_state(),
        2: ugc_state(needs_update=True),
        3: {
            **ugc_state(installed=False),
            "subscribed": True,
        },
        4: {
            **ugc_state(installed=False),
            "subscribed": False,
        },
    }
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(mid, f"Mod {mid}") for mid in range(1, 5)],
        initial_states=states,
        backend_results=[True],
        terminal_results=[
            (True, {mid: ugc_state() for mid in range(1, 5)}),
        ],
    )
    assert [item for item in trace if item[0] == "backend"] == [
        ("backend", (2, 3, 4)),
    ]
    assert win.launches == 1


def test_initially_ready_snapshot_cannot_launch_after_terminal_state_changes(
        monkeypatch):
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Changed")],
        initial_states={101: ugc_state()},
        backend_results=[True],
        terminal_results=[
            (True, {101: ugc_state(downloading=True)}),
            (True, {101: ugc_state()}),
        ],
    )
    assert trace[:4] == [
        ("initial_query", (101,)),
        ("terminal_query", (101,)),
        ("backend", (101,)),
        ("terminal_query", (101,)),
    ]
    assert trace[-1] == ("launch",)
    assert win.launches == 1


def test_current_ugc_state_still_requires_filesystem_presence(monkeypatch):
    mod = (101, "Missing Directory")
    win, trace, _outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[mod],
        initial_states={101: ugc_state()},
        terminal_results=[(True, {101: ugc_state()})],
        final_missing=[mod],
    )
    assert trace == [
        ("initial_query", (101,)),
        ("terminal_query", (101,)),
    ]
    assert win.launches == 0
    assert any("still missing after install" in error for error in win.errors)


@pytest.mark.parametrize(
    ("final_state", "expected_status"),
    [
        (ugc_state(), PreparationStatus.READY),
        (ugc_state(needs_update=True), PreparationStatus.FAILED),
    ],
)
def test_background_shared_engine_applies_bounded_terminal_gate(
        monkeypatch, final_state, expected_status):
    terminal_results = [
        (True, {101: ugc_state(needs_update=True)}),
        (True, {101: final_state}),
    ]
    win, trace, outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Background")],
        initial_states={101: ugc_state()},
        backend_results=[True],
        terminal_results=terminal_results,
        background=True,
    )
    assert outcome.status is expected_status
    assert [item for item in trace if item[0] == "backend"] == [("backend", (101,))]
    assert len([item for item in trace if item[0] == "terminal_query"]) == 2
    assert "launch" not in {item[0] for item in trace}
    assert win.launches == 0


def test_background_shared_engine_retry_cancellation_is_cancelled(monkeypatch):
    win, trace, outcome = run_terminal_validation_route(
        monkeypatch,
        mods=[(101, "Background")],
        initial_states={101: ugc_state()},
        backend_results=["cancel"],
        terminal_results=[(True, {101: ugc_state(pending=True)})],
        background=True,
    )
    assert outcome.status is PreparationStatus.CANCELLED
    assert [item for item in trace if item[0] == "backend"] == [("backend", (101,))]
    assert "launch" not in {item[0] for item in trace}
    assert win.launches == 0
