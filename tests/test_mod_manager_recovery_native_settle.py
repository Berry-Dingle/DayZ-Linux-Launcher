import inspect
import threading
from types import SimpleNamespace

from dzll_launcher import (
    mod_manager_recovery,
    mods_ui,
    settings_ui,
    steam_ugc_backend,
    steam_ugc_helper,
)
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.settings_ui import SettingsUI
from dzll_launcher.steam_native import SteamClientState, SteamRuntimeEvidence


NATIVE_RUNTIME = SteamRuntimeEvidence(
    SteamClientState.NATIVE,
    native_client_running=True,
)


def test_readiness_probe_proves_login_identity_ugc_and_native_provenance(monkeypatch):
    def run(command, *, on_event, mod_ids, strict_native_environment, **_kwargs):
        assert command == "readiness"
        assert mod_ids == []
        assert strict_native_environment is True
        on_event({
            "type": "init",
            "ok": True,
            "appid": 221100,
            "steam_root": "/native/Steam",
            "lib": "/native/Steam/steamrt64/libsteam_api.so",
            "flatpak_environment": False,
        })
        on_event({
            "type": "done",
            "ok": True,
            "logged_on": True,
            "identity_stable": True,
            "steam_id": 76561198000000001,
            "ugc_ready": True,
        })
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", run)
    result = steam_ugc_backend.probe_native_mod_manager_readiness()
    assert result.valid is True
    assert result.logged_on is True
    assert result.steam_id == 76561198000000001
    assert result.native_attachment_verified is True


def test_helper_readiness_does_not_enumerate_or_query_mod_ids(monkeypatch):
    class Steam:
        ugc = object()

        def __init__(self):
            self.callbacks = 0

        def run_callbacks(self):
            self.callbacks += 1

        @staticmethod
        def is_logged_on():
            return True

        @staticmethod
        def steam_id():
            return 76561198000000001

        @staticmethod
        def subscribed_item_ids():
            raise AssertionError("readiness must not enumerate subscriptions")

        @staticmethod
        def snapshot(_item_id):
            raise AssertionError("readiness must not query an empty/fake item ID")

    monkeypatch.setattr(steam_ugc_helper.time, "sleep", lambda _seconds: None)
    steam = Steam()
    logged_on, stable, steam_id = steam_ugc_helper._capture_native_readiness(steam)
    assert (logged_on, stable, steam_id) == (
        True, True, 76561198000000001,
    )
    assert steam.callbacks == 3


def test_readiness_helper_command_accepts_no_item_ids():
    args = steam_ugc_helper.build_parser().parse_args(["readiness"])
    assert args.command == "readiness"
    assert args.appid == 221100


def test_native_process_without_logged_on_ugc_does_not_finish_recovery(monkeypatch):
    cancel = threading.Event()
    calls = []
    monkeypatch.setattr(
        mod_manager_recovery, "resolve_steam_runtime_state", lambda: NATIVE_RUNTIME,
    )
    monkeypatch.setattr(
        mod_manager_recovery,
        "native_steam_ready_for_mod_manager",
        lambda _runtime: True,
    )

    def not_ready(**_kwargs):
        calls.append("probe")
        cancel.set()
        return SimpleNamespace(valid=False, steam_id=0)

    monkeypatch.setattr(
        mod_manager_recovery, "probe_native_mod_manager_readiness", not_ready,
    )
    assert mod_manager_recovery.wait_for_native_mod_manager_ready(
        cancel, timeout_s=5,
    ) == (False, "cancelled")
    assert calls == ["probe"]


def test_recovery_requires_consecutive_matching_ready_identities(monkeypatch):
    identities = iter((11, 22, 22))
    monkeypatch.setattr(
        mod_manager_recovery, "resolve_steam_runtime_state", lambda: NATIVE_RUNTIME,
    )
    monkeypatch.setattr(
        mod_manager_recovery,
        "native_steam_ready_for_mod_manager",
        lambda _runtime: True,
    )
    monkeypatch.setattr(
        mod_manager_recovery,
        "probe_native_mod_manager_readiness",
        lambda **_kwargs: SimpleNamespace(valid=True, steam_id=next(identities)),
    )
    monkeypatch.setattr(mod_manager_recovery, "STEAM_READY_SETTLE_S", 0)
    assert mod_manager_recovery.wait_for_native_mod_manager_ready(
        threading.Event(), timeout_s=5,
    ) == (True, "")


def _recovery_ui(manager):
    ui = SettingsUI.__new__(SettingsUI)
    ui._win = SimpleNamespace(_main_overlay=object())
    ui._main_overlay = ui._win._main_overlay
    ui._mods_mgr_overlay = manager
    ui._mods_manager_recovery_generation = 7
    ui._mods_manager_recovery_running = True
    ui._mods_manager_recovery_cancel_event = threading.Event()
    ui._mods_manager_flatpak_warning = None
    ui.messages = []
    ui.hidden = 0
    ui._set_flatpak_recovery_progress = lambda message: ui.messages.append(message)
    ui._hide_flatpak_mod_manager_recovery = (
        lambda: setattr(ui, "hidden", ui.hidden + 1)
    )
    ui._show_flatpak_mod_manager_recovery = lambda **_kwargs: False
    return ui


def test_hidden_inventory_handoff_opens_exactly_once_after_success(monkeypatch):
    events = []

    class Manager:
        def accept_recovered_native_authority(self):
            events.append("authority")

        def prepare_recovered_inventory(self, callback):
            events.append("inventory")
            callback(True)

        def show_recovered_inventory(self):
            events.append("show")

    ui = _recovery_ui(Manager())
    ui._mods_manager_recovery_phase = settings_ui.ModManagerRecoveryPhase.PREPARING_INVENTORY
    monkeypatch.setattr(
        settings_ui.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    ui._begin_flatpak_recovery_inventory_handoff(
        7, ui._mods_manager_recovery_cancel_event, 1,
    )
    assert events == ["authority", "inventory", "show"]
    assert ui.hidden == 1
    assert ui._mods_manager_recovery_running is False


def test_transient_first_inventory_failure_retries_hidden_and_bounded(monkeypatch):
    events = []
    outcomes = iter((False, True))

    class Manager:
        def accept_recovered_native_authority(self):
            events.append("authority")

        def prepare_recovered_inventory(self, callback):
            events.append("inventory")
            callback(next(outcomes))

        def show_recovered_inventory(self):
            events.append("show")

    monkeypatch.setattr(
        settings_ui.GLib, "timeout_add",
        lambda _delay, callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        settings_ui.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    ui = _recovery_ui(Manager())
    ui._mods_manager_recovery_phase = settings_ui.ModManagerRecoveryPhase.PREPARING_INVENTORY
    ui._begin_flatpak_recovery_inventory_handoff(
        7, ui._mods_manager_recovery_cancel_event, 1,
    )
    assert events == [
        "authority", "inventory", "authority", "inventory", "show",
    ]
    assert ui.messages == ["Opening Mod Manager…"]
    assert ui.hidden == 1


def test_late_waiting_status_cannot_regress_opening_phase():
    manager = SimpleNamespace()
    ui = _recovery_ui(manager)
    cancel = ui._mods_manager_recovery_cancel_event
    ui._mods_manager_recovery_phase = (
        settings_ui.ModManagerRecoveryPhase.OPENING_MOD_MANAGER
    )
    ui._apply_flatpak_recovery_status(
        7,
        cancel,
        settings_ui.ModManagerRecoveryPhase.WAITING_FOR_NATIVE,
        "Waiting for Steam login…",
    )
    assert ui.messages == []
    assert ui._mods_manager_recovery_phase is (
        settings_ui.ModManagerRecoveryPhase.OPENING_MOD_MANAGER
    )


def test_opening_phase_is_not_entered_until_inventory_preload_succeeds(monkeypatch):
    callbacks = []
    manager = SimpleNamespace(
        accept_recovered_native_authority=lambda: None,
        prepare_recovered_inventory=lambda callback: callbacks.append(callback),
        show_recovered_inventory=lambda: None,
    )
    ui = _recovery_ui(manager)
    ui._mods_manager_recovery_phase = (
        settings_ui.ModManagerRecoveryPhase.PREPARING_INVENTORY
    )
    monkeypatch.setattr(
        settings_ui.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    ui._begin_flatpak_recovery_inventory_handoff(
        7, ui._mods_manager_recovery_cancel_event, 1,
    )
    assert ui.messages == []
    callbacks[0](True)
    assert ui.messages == ["Opening Mod Manager…"]


def test_cancel_removes_preload_retry_and_blocks_late_timer(monkeypatch):
    removed = []
    manager = SimpleNamespace(
        _recovery_inventory_handoff_active=True,
        reset_for_steam_recovery=lambda: None,
    )
    ui = _recovery_ui(manager)
    ui._mods_manager_recovery_retry_source_id = 81
    monkeypatch.setattr(
        settings_ui.GLib, "source_remove", lambda source_id: removed.append(source_id),
    )
    generation = ui._mods_manager_recovery_generation
    cancel = ui._mods_manager_recovery_cancel_event
    ui._cancel_flatpak_mod_manager_recovery()
    assert removed == [81]
    assert ui._retry_flatpak_recovery_inventory_handoff(
        generation, cancel, 2,
    ) is False
    assert ui.messages == []


def test_cancel_invalidates_pending_inventory_completion():
    callbacks = []
    events = []

    class Manager:
        _recovery_inventory_handoff_active = True

        @staticmethod
        def accept_recovered_native_authority():
            return None

        @staticmethod
        def prepare_recovered_inventory(callback):
            callbacks.append(callback)

        @staticmethod
        def reset_for_steam_recovery():
            events.append("reset")

        @staticmethod
        def show_recovered_inventory():
            events.append("show")

    ui = _recovery_ui(Manager())
    cancel = ui._mods_manager_recovery_cancel_event
    ui._begin_flatpak_recovery_inventory_handoff(7, cancel, 1)
    ui._cancel_flatpak_mod_manager_recovery()
    callbacks[0](True)
    assert events == ["reset"]


def test_recovery_handoff_suppresses_normal_start_steam_prompt():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._recovery_inventory_handoff_active = True
    overlay._show_start_steam_manage_prompt = lambda: (_ for _ in ()).throw(
        AssertionError("normal Steam prompt must stay hidden during recovery")
    )
    overlay._maybe_show_start_steam_manage_reminder()


def test_empty_disk_inventory_uses_readiness_not_an_empty_item_query(monkeypatch):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    monkeypatch.setattr(mods_ui, "_candidate_workshop_roots", lambda **_kwargs: [])
    monkeypatch.setattr(mods_ui, "_read_installed_mod_ids_from_roots", lambda _roots: [])
    monkeypatch.setattr(mods_ui, "_name_map_from_symlinks", lambda **_kwargs: {})
    monkeypatch.setattr(mods_ui, "load_mod_metadata", lambda: {"mods": {}})
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.NATIVE)
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda _ids, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an empty item query must not be used")
        ),
    )
    monkeypatch.setattr(
        mods_ui,
        "probe_native_mod_manager_readiness",
        lambda **_kwargs: SimpleNamespace(
            valid=True, steam_id=76561198000000001,
        ),
    )
    monkeypatch.setattr(mods_ui.time, "sleep", lambda _seconds: None)
    items = overlay._load_installed_items(
        "/unused", "/unused",
        steam_state=SteamClientState.NATIVE,
        cancel_event=threading.Event(),
    )
    assert items.state_query_ok is True
    assert items.subscription_snapshot.valid is True
    assert items.subscription_snapshot.requested_ids == frozenset()


def test_progress_uses_actual_widget_insensitivity():
    class Button:
        def __init__(self):
            self.sensitive = None
            self.label = None

        def set_sensitive(self, value):
            self.sensitive = bool(value)

        def set_label(self, value):
            self.label = value

    message = SimpleNamespace(set_text=lambda _value: None)
    ui = SettingsUI.__new__(SettingsUI)
    ui._mods_manager_flatpak_warning = {
        "message": message,
        "continue_button": Button(),
        "cancel_button": Button(),
    }
    ui._set_flatpak_recovery_progress("Waiting for Steam login…")
    assert ui._mods_manager_flatpak_warning["continue_button"].sensitive is False
    assert ui._mods_manager_flatpak_warning["cancel_button"].sensitive is True


def test_recovery_dialog_spacing_width_and_disabled_style_are_consistent():
    source = inspect.getsource(SettingsUI._ensure_flatpak_mod_manager_warning)
    assert "card.set_size_request(RECOVERY_DIALOG_WIDTH, -1)" in source
    assert "buttons.set_margin_top(12)" in source
    assert settings_ui.RECOVERY_DIALOG_WIDTH == 520
    style_source = inspect.getsource(__import__(
        "dzll_launcher.styles", fromlist=["build_css"]
    ))
    assert "button.suggested-action:disabled" in style_source
    assert "button.suggested-action:disabled label" in style_source
