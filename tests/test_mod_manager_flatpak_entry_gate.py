import threading
import inspect
from types import SimpleNamespace

import pytest

from dzll_launcher import mods_ui, settings_ui, steam_ugc_backend
from dzll_launcher.mods_ui import InventoryValidity, ModsManagerOverlay
from dzll_launcher.settings_ui import SettingsUI
from dzll_launcher.steam_native import SteamClientState, SteamRuntimeEvidence


def _ui_gate():
    ui = SettingsUI.__new__(SettingsUI)
    ui._win = SimpleNamespace()
    ui._mods_manager_entry_check_running = False
    ui._mods_manager_flatpak_warning = None
    ui._mods_manager_recovery_running = False
    ui._mods_manager_recovery_generation = 0
    ui._mods_manager_recovery_cancel_event = None
    ui.opened = []
    ui.dialogs = []
    ui.progress = []
    ui.hidden = 0
    ui._open_mods_manager_after_gate = (
        lambda **kwargs: ui.opened.append(dict(kwargs))
    )
    ui._show_flatpak_mod_manager_recovery = (
        lambda **kwargs: ui.dialogs.append({"mode": "flatpak", **kwargs})
    )
    ui._show_mixed_steam_mod_manager_gate = (
        lambda **kwargs: ui.dialogs.append({"mode": "mixed", **kwargs})
    )
    ui._hide_flatpak_mod_manager_recovery = (
        lambda: setattr(ui, "hidden", ui.hidden + 1)
    )
    ui._set_flatpak_recovery_progress = lambda message: ui.progress.append(message)
    return ui


def _install_immediate_threads(monkeypatch):
    monkeypatch.setattr(
        settings_ui.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        settings_ui.threading,
        "Thread",
        lambda target, daemon: SimpleNamespace(start=target),
    )


def _run_gate_immediately(monkeypatch, ui, runtime):
    monkeypatch.setattr(settings_ui, "resolve_steam_runtime_state", lambda: runtime)
    _install_immediate_threads(monkeypatch)
    ui.open_mods_manager()


def test_flatpak_only_runtime_shows_continue_recovery(monkeypatch):
    ui = _ui_gate()
    _run_gate_immediately(monkeypatch, ui, SteamRuntimeEvidence(
        SteamClientState.FLATPAK,
        flatpak_process_running=True,
        flatpak_client_running=True,
    ))
    assert ui.opened == []
    assert ui.dialogs == [{"mode": "flatpak"}]


def test_mixed_runtime_shows_manual_recheck_gate(monkeypatch):
    ui = _ui_gate()
    _run_gate_immediately(monkeypatch, ui, SteamRuntimeEvidence(
        SteamClientState.UNKNOWN,
        native_client_running=True,
        flatpak_process_running=True,
        flatpak_client_running=True,
    ))
    assert ui.opened == []
    assert ui.dialogs == [{"mode": "mixed"}]


@pytest.mark.parametrize(
    "runtime",
    [
        SteamRuntimeEvidence(
            SteamClientState.NATIVE,
            native_client_running=True,
        ),
        SteamRuntimeEvidence(SteamClientState.OFFLINE),
        SteamRuntimeEvidence(SteamClientState.UNKNOWN),
    ],
)
def test_native_offline_and_unproven_non_flatpak_preserve_normal_open(
        monkeypatch, runtime):
    ui = _ui_gate()
    _run_gate_immediately(monkeypatch, ui, runtime)
    assert ui.opened == [{
        "verified_native": runtime.state is SteamClientState.NATIVE,
    }]
    assert ui.dialogs == []


def test_flatpak_gate_prevents_overlay_construction_inventory_and_ugc(monkeypatch):
    ui = _ui_gate()
    monkeypatch.setattr(
        settings_ui,
        "ModsManagerOverlay",
        lambda *_args, **_kwargs: pytest.fail("Mod Manager must not be constructed"),
    )
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda *_args, **_kwargs: pytest.fail("UGC enumeration must not begin"),
    )
    _run_gate_immediately(
        monkeypatch,
        ui,
        SteamRuntimeEvidence(
            SteamClientState.FLATPAK,
            flatpak_process_running=True,
        ),
    )
    assert ui.opened == []


def test_cancel_before_continue_only_closes_dialog(monkeypatch):
    ui = _ui_gate()
    monkeypatch.setattr(
        settings_ui,
        "request_graceful_flatpak_steam_shutdown",
        lambda: pytest.fail("Cancel must not shut down Steam"),
    )
    monkeypatch.setattr(
        settings_ui,
        "launch_native_steam",
        lambda: pytest.fail("Cancel must not launch Steam"),
    )
    ui._cancel_flatpak_mod_manager_recovery()
    assert ui.hidden == 1
    assert ui.opened == []


def test_normal_mod_manager_open_works_after_recovery_cancel(monkeypatch):
    ui = _ui_gate()
    ui._mods_manager_recovery_cancel_event = threading.Event()
    ui._cancel_flatpak_mod_manager_recovery()
    _run_gate_immediately(
        monkeypatch,
        ui,
        SteamRuntimeEvidence(
            SteamClientState.NATIVE,
            native_client_running=True,
        ),
    )
    assert ui.opened == [{"verified_native": True}]


def _patch_successful_recovery(monkeypatch, events):
    runtimes = iter((
        SteamRuntimeEvidence(
            SteamClientState.FLATPAK,
            flatpak_process_running=True,
            flatpak_client_running=True,
        ),
        SteamRuntimeEvidence(SteamClientState.OFFLINE),
    ))
    monkeypatch.setattr(
        settings_ui,
        "resolve_steam_runtime_state",
        lambda: next(runtimes),
    )
    monkeypatch.setattr(
        settings_ui,
        "request_graceful_flatpak_steam_shutdown",
        lambda _runtime=None: events.append("shutdown") or [],
    )
    monkeypatch.setattr(
        settings_ui,
        "wait_for_flatpak_steam_exit",
        lambda _cancel, **_kwargs: events.append("exited") or (True, ""),
    )
    monkeypatch.setattr(
        settings_ui,
        "close_active_ugc_sessions",
        lambda: events.append("helpers_reset") or [],
    )
    monkeypatch.setattr(
        settings_ui,
        "launch_native_steam",
        lambda: events.append("launch") or (True, ""),
    )
    monkeypatch.setattr(
        settings_ui,
        "wait_for_native_mod_manager_ready",
        lambda _cancel, **_kwargs: events.append("ready") or (True, ""),
    )


def test_continue_orders_shutdown_reset_launch_readiness_and_open(monkeypatch):
    ui = _ui_gate()
    events = []
    old_cancel = threading.Event()
    ui._win._join_preparation_cancel_event = old_cancel
    ui._win._background_prepare_ui_generation = 3
    ui._win._background_prepare_controller = SimpleNamespace(
        cancel=lambda: events.append("background_cancel"),
    )
    manager = SimpleNamespace(
        reset_for_steam_recovery=lambda: events.append("ui_reset"),
        accept_recovered_native_authority=lambda: events.append("authority"),
        prepare_recovered_inventory=lambda callback: (
            events.append("inventory"), callback(True)
        ),
        show_recovered_inventory=lambda: events.append("show"),
    )
    ui._mods_mgr_overlay = manager
    _patch_successful_recovery(monkeypatch, events)
    _install_immediate_threads(monkeypatch)

    assert ui._start_flatpak_mod_manager_recovery()
    assert events == [
        "shutdown", "exited", "background_cancel", "ui_reset", "helpers_reset",
        "launch", "ready", "authority", "inventory", "show",
    ]
    assert old_cancel.is_set()
    assert ui._win._join_preparation_cancel_event is not old_cancel
    assert not ui._win._join_preparation_cancel_event.is_set()
    assert ui._win._background_prepare_ui_generation == 4
    assert ui.opened == []
    assert ui.hidden == 1
    assert ui.progress == [
        "Closing Steam…",
        "Starting native Steam…",
        "Waiting for Steam login…",
        "Opening Mod Manager…",
    ]


def test_recovery_blocks_native_start_for_owned_unconfirmed_helper(monkeypatch):
    class Process:
        alive = True
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None if self.alive else 0

    ui = _ui_gate()
    failures = []
    old_cancel = threading.Event()
    ui._win._join_preparation_cancel_event = old_cancel
    ui._win._background_prepare_ui_generation = 0
    ui._win._background_prepare_controller = None
    ui._mods_mgr_overlay = None
    ui._finish_flatpak_recovery_failure = (
        lambda _generation, _cancel, message: failures.append(message)
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    process = Process()
    close_calls = []
    session._proc = process

    def fail_close():
        close_calls.append("close")
        raise steam_ugc_backend.UGCHelperReapError(
            "owned helper still alive", process=process,
        )

    session._close_session = fail_close
    steam_ugc_backend.register_owned_ugc_session(session)
    steam_ugc_backend.activate_ugc_session(session)
    inner_done = threading.Event()

    def inner():
        steam_ugc_backend.activate_ugc_session(session)
        assert steam_ugc_backend.deactivate_ugc_session(session)
        inner_done.set()

    inner_worker = threading.Thread(target=inner)
    inner_worker.start()
    assert inner_done.wait(2)
    inner_worker.join(timeout=2)
    assert session in steam_ugc_backend._ACTIVE_UGC_SESSIONS
    monkeypatch.setattr(
        settings_ui, "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(
        settings_ui, "launch_native_steam",
        lambda: pytest.fail("native Steam must remain blocked"),
    )
    _install_immediate_threads(monkeypatch)
    try:
        assert ui._start_flatpak_mod_manager_recovery()
        assert old_cancel.is_set()
        assert close_calls == ["close"]
        assert session in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        assert failures == ["DZLL could not reset Steam. Retry."]
    finally:
        process.alive = False
        session.close()
        steam_ugc_backend.deactivate_ugc_session(session)
        steam_ugc_backend.unregister_owned_ugc_session(session)


def test_recovery_may_start_native_after_owned_session_closes(monkeypatch):
    ui = _ui_gate()
    events = []
    ui._win._join_preparation_cancel_event = threading.Event()
    ui._win._background_prepare_ui_generation = 0
    ui._win._background_prepare_controller = None
    ui._mods_mgr_overlay = None
    ui._finish_flatpak_recovery_failure = (
        lambda _generation, _cancel, message: events.append(("failure", message))
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    steam_ugc_backend.register_owned_ugc_session(session)
    monkeypatch.setattr(
        settings_ui, "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(SteamClientState.OFFLINE),
    )

    def launch():
        assert session._owned_resources_release_confirmed()
        assert session not in steam_ugc_backend._ACTIVE_UGC_SESSIONS
        events.append("launch")
        return False, "deliberate test stop"

    monkeypatch.setattr(settings_ui, "launch_native_steam", launch)
    _install_immediate_threads(monkeypatch)
    assert ui._start_flatpak_mod_manager_recovery()
    assert events[0] == "launch"
    assert session not in steam_ugc_backend._ACTIVE_UGC_SESSIONS


def test_shutdown_timeout_keeps_manager_closed_and_offers_retry(monkeypatch):
    ui = _ui_gate()
    monkeypatch.setattr(
        settings_ui,
        "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(
            SteamClientState.FLATPAK,
            flatpak_process_running=True,
            flatpak_client_running=True,
        ),
    )
    monkeypatch.setattr(
        settings_ui, "request_graceful_flatpak_steam_shutdown", lambda _runtime=None: [],
    )
    monkeypatch.setattr(
        settings_ui,
        "launch_native_steam",
        lambda: pytest.fail("native Steam must not launch while old Steam remains"),
    )
    monkeypatch.setattr(
        settings_ui,
        "wait_for_flatpak_steam_exit",
        lambda _cancel, **_kwargs: (
            False,
            "Steam is still running. Close it, then Retry.",
        ),
    )
    _install_immediate_threads(monkeypatch)
    ui._start_flatpak_mod_manager_recovery()
    assert ui.opened == []
    assert ui.dialogs[-1]["retry"] is True
    assert "still running" in ui.dialogs[-1]["message"]


def test_retry_after_flatpak_has_exited_resumes_reset_and_native_launch(
        monkeypatch):
    ui = _ui_gate()
    events = []
    ui._mods_mgr_overlay = SimpleNamespace(
        reset_for_steam_recovery=lambda: events.append("reset"),
        accept_recovered_native_authority=lambda: events.append("authority"),
        prepare_recovered_inventory=lambda callback: callback(True),
        show_recovered_inventory=lambda: events.append("show"),
    )
    monkeypatch.setattr(
        settings_ui,
        "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(
        settings_ui,
        "request_graceful_flatpak_steam_shutdown",
        lambda *_args: pytest.fail("gone Flatpak must not receive shutdown"),
    )
    monkeypatch.setattr(
        settings_ui, "close_active_ugc_sessions", lambda: events.append("helpers") or [],
    )
    monkeypatch.setattr(
        settings_ui, "launch_native_steam", lambda: events.append("launch") or (True, ""),
    )
    monkeypatch.setattr(
        settings_ui,
        "wait_for_native_mod_manager_ready",
        lambda *_args, **_kwargs: (True, ""),
    )
    _install_immediate_threads(monkeypatch)
    assert ui._start_flatpak_mod_manager_recovery()
    assert "reset" in events
    assert events.count("launch") == 1
    assert events.count("show") == 1


def test_failed_recovery_dialog_cannot_be_replaced_by_initial_continue_copy():
    ui = SettingsUI.__new__(SettingsUI)
    ui._mods_manager_recovery_running = False
    ui._mods_manager_recovery_phase = settings_ui.ModManagerRecoveryPhase.FAILED
    ui._ensure_flatpak_mod_manager_warning = lambda: (_ for _ in ()).throw(
        AssertionError("failed Retry dialog must not be rebuilt as initial Continue")
    )
    assert ui._show_flatpak_mod_manager_recovery() is False


def test_native_launch_failure_is_actionable_and_does_not_open(monkeypatch):
    ui = _ui_gate()
    events = []
    _patch_successful_recovery(monkeypatch, events)
    monkeypatch.setattr(
        settings_ui,
        "launch_native_steam",
        lambda: (False, "native executable missing"),
    )
    _install_immediate_threads(monkeypatch)
    ui._start_flatpak_mod_manager_recovery()
    assert ui.opened == []
    assert ui.dialogs[-1]["retry"] is True
    assert "could not start" in ui.dialogs[-1]["message"]


def test_native_readiness_timeout_is_actionable_and_does_not_open(monkeypatch):
    ui = _ui_gate()
    events = []
    _patch_successful_recovery(monkeypatch, events)
    monkeypatch.setattr(
        settings_ui,
        "wait_for_native_mod_manager_ready",
        lambda _cancel, **_kwargs: (
            False,
            "Native Steam is not ready. Sign in, then Retry.",
        ),
    )
    _install_immediate_threads(monkeypatch)
    ui._start_flatpak_mod_manager_recovery()
    assert ui.opened == []
    message = ui.dialogs[-1]["message"]
    assert "Native Steam is not ready" in message
    assert "Sign in" in message
    assert "Steam Issue" not in message


def test_flatpak_after_launch_fails_once_without_close_relaunch_loop(monkeypatch):
    ui = _ui_gate()
    events = []
    _patch_successful_recovery(monkeypatch, events)
    monkeypatch.setattr(
        settings_ui,
        "wait_for_native_mod_manager_ready",
        lambda _cancel, **_kwargs: (
            False,
            "Native Steam could not be started.\nFlatpak Steam opened instead.",
        ),
    )
    _install_immediate_threads(monkeypatch)
    ui._start_flatpak_mod_manager_recovery()
    assert events.count("shutdown") == 1
    assert events.count("launch") == 1
    assert ui.opened == []
    assert ui.dialogs[-1] == {
        "mode": "flatpak",
        "message": (
            "Native Steam could not be started.\n"
            "Flatpak Steam opened instead."
        ),
        "retry": True,
    }


def test_duplicate_continue_or_retry_does_not_start_overlapping_recovery(
        monkeypatch):
    ui = _ui_gate()
    workers = []

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(settings_ui.threading, "Thread", DeferredThread)
    assert ui._start_flatpak_mod_manager_recovery()
    assert not ui._start_flatpak_mod_manager_recovery()
    assert len(workers) == 1


def test_cancel_during_recovery_invalidates_stale_success_callback(monkeypatch):
    ui = _ui_gate()
    workers = []
    events = []
    _patch_successful_recovery(monkeypatch, events)
    monkeypatch.setattr(
        settings_ui.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(settings_ui.threading, "Thread", DeferredThread)
    ui._start_flatpak_mod_manager_recovery()
    ui._cancel_flatpak_mod_manager_recovery()
    workers[0]()
    assert ui.opened == []


def test_dialog_copy_matches_requested_recovery_flow():
    text = SettingsUI._flatpak_recovery_intro_text()
    assert text == "DZLL will close Flatpak Steam and start native Steam."
    assert SettingsUI._mixed_steam_gate_text() == (
        "DZLL cannot safely manage Workshop mods while both Steam clients "
        "are running.\n\n"
        "Close both Steam clients, start native Steam only, then click Recheck."
    )


def test_recovery_dialog_uses_one_existing_standard_width_for_all_states():
    build_source = inspect.getsource(SettingsUI._ensure_flatpak_mod_manager_warning)
    show_source = inspect.getsource(SettingsUI._show_flatpak_mod_manager_recovery)
    progress_source = inspect.getsource(SettingsUI._set_flatpak_recovery_progress)
    assert settings_ui.RECOVERY_DIALOG_WIDTH == 520
    assert "card.set_size_request(RECOVERY_DIALOG_WIDTH, -1)" in build_source
    assert "set_size_request" not in show_source
    assert "set_size_request" not in progress_source


def test_flatpak_start_while_open_invalidates_and_replaces_mod_manager(
        monkeypatch):
    host_events = []
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay.host = SimpleNamespace(
        _on_mod_manager_flatpak_detected=lambda runtime: host_events.append(runtime),
    )
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._steam_state_generation = 1
    overlay._steam_state_probe_running = False
    overlay._steam_state_probe_refresh_on_change = False
    overlay._last_mod_state_query_ok = True
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._subscription_snapshot = object()
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._loaded_items = []
    overlay._set_steam_status_pill = lambda _status: None
    overlay._update_batch_action_buttons = lambda: None
    overlay._mod_manager_is_visible = lambda: True
    overlay.refresh = lambda **_kwargs: pytest.fail("mixed state must not refresh inventory")
    monkeypatch.setattr(
        mods_ui,
        "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(
            SteamClientState.UNKNOWN,
            native_client_running=True,
            flatpak_process_running=True,
        ),
    )
    monkeypatch.setattr(
        mods_ui.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        mods_ui.threading,
        "Thread",
        lambda target, daemon: SimpleNamespace(start=target),
    )
    overlay._request_authoritative_steam_state_probe(refresh_on_change=True)
    assert overlay._authoritative_steam_state is SteamClientState.UNKNOWN
    assert overlay._steam_management_verified is False
    assert overlay._native_session_handoff_valid is False
    assert overlay._inventory_validity is InventoryValidity.STALE
    assert len(host_events) == 1
    assert host_events[0].native_client_running is True
    assert host_events[0].flatpak_process_running is True


def test_host_takeover_closes_manager_before_showing_recovery():
    events = []
    manager = SimpleNamespace(block_for_flatpak=lambda: events.append("closed"))
    ui = SettingsUI.__new__(SettingsUI)
    ui._mods_mgr_overlay = manager
    ui._show_mixed_steam_mod_manager_gate = (
        lambda: events.append("mixed")
    )
    ui._on_mod_manager_flatpak_detected(SteamRuntimeEvidence(
        SteamClientState.UNKNOWN,
        native_client_running=True,
        flatpak_process_running=True,
    ))
    assert events == ["closed", "mixed"]


class _SensitivityButton:
    def __init__(self):
        self.sensitive = True

    def set_sensitive(self, value):
        self.sensitive = bool(value)


class _TextWidget(_SensitivityButton):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.visible = False

    def set_text(self, value):
        self.text = str(value)

    def set_label(self, value):
        self.text = str(value)

    def set_visible(self, value):
        self.visible = bool(value)


def _prepare_immediate_mixed_recheck(monkeypatch, ui, runtime, events):
    idle = threading.Event()
    idle.set()
    button = _SensitivityButton()
    ui._mods_manager_recheck_running = False
    ui._mods_manager_recheck_generation = 0
    ui._mods_manager_recheck_cancel_event = None
    ui._mods_manager_recovery_running = False
    ui._mods_mgr_overlay = SimpleNamespace(
        reset_for_steam_recovery=lambda: events.append("reset") or idle,
    )
    ui._ensure_flatpak_mod_manager_warning = lambda: {
        "continue_button": button,
    }
    monkeypatch.setattr(
        settings_ui,
        "resolve_steam_runtime_state",
        lambda: events.append("detect") or runtime,
    )
    _install_immediate_threads(monkeypatch)
    return button


def test_mixed_recheck_resets_only_dzll_and_keeps_gate_when_both_remain(
        monkeypatch):
    ui = _ui_gate()
    events = []
    runtime = SteamRuntimeEvidence(
        SteamClientState.UNKNOWN,
        native_client_running=True,
        flatpak_process_running=True,
        flatpak_client_running=True,
    )
    button = _prepare_immediate_mixed_recheck(
        monkeypatch, ui, runtime, events,
    )
    assert ui._start_mixed_mod_manager_recheck()
    assert events == ["reset", "detect"]
    assert ui.dialogs == [{"mode": "mixed"}]
    assert ui.opened == []
    assert button.sensitive is False


def test_dialog_titles_copy_and_primary_buttons_are_state_specific():
    ui = SettingsUI.__new__(SettingsUI)
    widgets = {
        "scrim": _TextWidget(),
        "card": _TextWidget(),
        "title": _TextWidget(),
        "message": _TextWidget(),
        "cancel_button": _TextWidget(),
        "continue_button": _TextWidget(),
    }
    ui._mods_manager_recovery_running = False
    ui._mods_manager_recovery_phase = settings_ui.ModManagerRecoveryPhase.IDLE
    ui._mods_manager_recheck_running = False
    ui._ensure_flatpak_mod_manager_warning = lambda: widgets
    ui._show_flatpak_mod_manager_recovery()
    assert widgets["title"].text == "Flatpak Steam detected"
    assert widgets["message"].text == (
        "DZLL will close Flatpak Steam and start native Steam."
    )
    assert widgets["continue_button"].text == "Continue"
    ui._show_mixed_steam_mod_manager_gate()
    assert widgets["title"].text == "Both Steam clients detected"
    assert widgets["message"].text == SettingsUI._mixed_steam_gate_text()
    assert widgets["continue_button"].text == "Recheck"
    ui._show_mixed_steam_mod_manager_gate(
        message=(
            "Flatpak Steam is still running.\n\n"
            "Close Flatpak Steam, start native Steam only, then click Recheck."
        )
    )
    assert widgets["title"].text == "Both Steam clients detected"
    assert widgets["continue_button"].text == "Recheck"
    assert ui._mods_manager_gate_mode == "mixed"


def test_mixed_recheck_flatpak_only_remains_manual_recheck_without_steam_control(
        monkeypatch):
    ui = _ui_gate()
    events = []
    runtime = SteamRuntimeEvidence(
        SteamClientState.FLATPAK,
        flatpak_process_running=True,
        flatpak_client_running=True,
    )
    _prepare_immediate_mixed_recheck(monkeypatch, ui, runtime, events)
    monkeypatch.setattr(
        settings_ui,
        "request_graceful_flatpak_steam_shutdown",
        lambda *_args: pytest.fail("Recheck must not start recovery"),
    )
    monkeypatch.setattr(
        settings_ui,
        "launch_native_steam",
        lambda: pytest.fail("Recheck must not launch native Steam"),
    )
    assert ui._start_mixed_mod_manager_recheck()
    assert ui.dialogs[0]["mode"] == "mixed"
    assert ui.dialogs[0]["message"] == (
        "Flatpak Steam is still running.\n\n"
        "Close Flatpak Steam, start native Steam only, then click Recheck."
    )
    assert ui.opened == []


@pytest.mark.parametrize(
    ("runtime", "verified"),
    [
        (
            SteamRuntimeEvidence(
                SteamClientState.NATIVE, native_client_running=True,
            ),
            True,
        ),
        (SteamRuntimeEvidence(SteamClientState.OFFLINE), False),
    ],
)
def test_mixed_recheck_uses_normal_native_or_offline_entry(
        monkeypatch, runtime, verified):
    ui = _ui_gate()
    events = []
    _prepare_immediate_mixed_recheck(monkeypatch, ui, runtime, events)
    assert ui._start_mixed_mod_manager_recheck()
    assert ui.opened == [{"verified_native": verified}]
    assert ui.dialogs == []


def test_duplicate_mixed_recheck_does_not_overlap(monkeypatch):
    ui = _ui_gate()
    workers = []
    button = _SensitivityButton()
    ui._mods_manager_recheck_running = False
    ui._mods_manager_recheck_generation = 0
    ui._mods_manager_recheck_cancel_event = None
    ui._mods_manager_recovery_running = False
    ui._mods_mgr_overlay = None
    ui._ensure_flatpak_mod_manager_warning = lambda: {
        "continue_button": button,
    }

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(settings_ui.threading, "Thread", DeferredThread)
    assert ui._start_mixed_mod_manager_recheck()
    assert not ui._start_mixed_mod_manager_recheck()
    assert len(workers) == 1


def test_cancelled_mixed_recheck_cannot_apply_stale_detection(monkeypatch):
    ui = _ui_gate()
    workers = []
    ui._mods_manager_recheck_running = False
    ui._mods_manager_recheck_generation = 0
    ui._mods_manager_recheck_cancel_event = None
    ui._mods_manager_recovery_running = False
    ui._mods_manager_recovery_generation = 0
    ui._mods_manager_recovery_cancel_event = None
    ui._mods_mgr_overlay = None
    ui._ensure_flatpak_mod_manager_warning = lambda: {
        "continue_button": _SensitivityButton(),
    }

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(settings_ui.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        settings_ui,
        "resolve_steam_runtime_state",
        lambda: pytest.fail("cancelled Recheck must not detect or apply"),
    )
    assert ui._start_mixed_mod_manager_recheck()
    ui._cancel_flatpak_mod_manager_recovery()
    workers[0]()
    assert ui.opened == []
    assert ui.dialogs == []


def test_flatpak_start_without_native_shows_flatpak_only_gate():
    events = []
    manager = SimpleNamespace(block_for_flatpak=lambda: events.append("closed"))
    ui = SettingsUI.__new__(SettingsUI)
    ui._mods_mgr_overlay = manager
    ui._show_flatpak_mod_manager_recovery = (
        lambda **_kwargs: events.append("flatpak")
    )
    ui._on_mod_manager_flatpak_detected(SteamRuntimeEvidence(
        SteamClientState.FLATPAK,
        flatpak_process_running=True,
        flatpak_client_running=True,
    ))
    assert events == ["closed", "flatpak"]
