from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import mods_ui, steam_native, steam_ugc_backend
from dzll_launcher.mods_ui import InventoryValidity, ModsManagerOverlay
from dzll_launcher.steam_native import SteamClientState


class Widget:
    def __init__(self, *, visible=False):
        self.sensitive = None
        self.visible = bool(visible)
        self.size = None

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def get_visible(self):
        return self.visible

    def set_size_request(self, width, height):
        self.size = (width, height)

    def stop(self):
        return None


def _write_process(
        root: Path,
        pid: int,
        cmdline: bytes,
        *,
        environ=b"",
        flatpak_info="",
        executable="",
):
    process = root / str(pid)
    (process / "root").mkdir(parents=True)
    (process / "cmdline").write_bytes(cmdline)
    (process / "environ").write_bytes(environ)
    if executable:
        (process / "exe").symlink_to(executable)
    if flatpak_info:
        (process / "root/.flatpak-info").write_text(flatpak_info, encoding="utf-8")


def _write_cgroup(root: Path, pid: int, value: str):
    (root / str(pid) / "cgroup").write_text(value, encoding="utf-8")


def test_authoritative_steam_client_state_native_flatpak_offline_and_ambiguous(tmp_path):
    native_root = tmp_path / "native"
    native_root.mkdir()
    _write_process(native_root, 10, b"/usr/bin/steam\x00-silent\x00")
    assert steam_native.detect_steam_client_state(native_root) is SteamClientState.NATIVE

    flatpak_root = tmp_path / "flatpak"
    flatpak_root.mkdir()
    _write_process(
        flatpak_root,
        20,
        b"/app/bin/steam\x00-silent\x00",
        environ=b"FLATPAK_ID=com.valvesoftware.Steam\x00",
    )
    assert steam_native.detect_steam_client_state(flatpak_root) is SteamClientState.FLATPAK

    offline_root = tmp_path / "offline"
    offline_root.mkdir()
    assert steam_native.detect_steam_client_state(offline_root) is SteamClientState.OFFLINE

    ambiguous_root = tmp_path / "ambiguous"
    ambiguous_root.mkdir()
    _write_process(ambiguous_root, 30, b"/usr/bin/steam\x00")
    _write_process(
        ambiguous_root,
        31,
        b"/usr/bin/flatpak\x00run\x00com.valvesoftware.Steam\x00",
    )
    assert steam_native.detect_steam_client_state(ambiguous_root) is SteamClientState.UNKNOWN

    incomplete_root = tmp_path / "incomplete"
    incomplete_root.mkdir()
    _write_process(incomplete_root, 40, b"/usr/bin/steamwebhelper\x00")
    assert steam_native.detect_steam_client_state(incomplete_root) is SteamClientState.UNKNOWN


def test_steam_client_detection_failure_is_unknown():
    class UnreadableProc:
        def iterdir(self):
            raise OSError("unavailable")

    assert steam_native.detect_steam_client_state(UnreadableProc()) is SteamClientState.UNKNOWN


def test_flatpak_started_first_then_native_remains_ambiguous_until_flatpak_exits(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/usr/bin/flatpak\x00run\x00com.valvesoftware.Steam\x00",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.FLATPAK

    _write_process(proc_root, 20, b"/usr/bin/steam\x00-silent\x00")
    runtime = steam_native.resolve_steam_runtime_state(proc_root)
    assert runtime.state is SteamClientState.UNKNOWN
    assert runtime.native_client_running
    assert runtime.flatpak_process_running


def test_stale_generic_flatpak_wrapper_keeps_native_ugc_authority_ambiguous(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 10, b"/usr/bin/steam\x00-silent\x00")
    _write_process(proc_root, 11, b"/usr/bin/bwrap\x00--new-session\x00")
    _write_cgroup(
        proc_root,
        11,
        "0::/user.slice/app-flatpak-com.valvesoftware.Steam-123.scope\n",
    )
    runtime = steam_native.resolve_steam_runtime_state(proc_root)
    assert runtime.state is SteamClientState.UNKNOWN
    assert runtime.native_client_running
    assert runtime.flatpak_process_running


def test_generic_flatpak_infrastructure_and_portals_do_not_count_as_steam(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/usr/libexec/flatpak-session-helper\x00",
        executable="/usr/lib/flatpak/flatpak-session-helper",
    )
    _write_process(
        proc_root,
        11,
        b"/usr/libexec/xdg-desktop-portal\x00",
        executable="/usr/libexec/xdg-desktop-portal",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.OFFLINE


def test_another_flatpak_application_does_not_count_as_flatpak_steam(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/app/bin/firefox\x00",
        environ=b"FLATPAK_ID=org.mozilla.firefox\x00",
        flatpak_info="[Application]\nname=org.mozilla.firefox\n",
        executable="/app/bin/firefox",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.OFFLINE


def test_flatpak_installation_and_stale_runtime_files_do_not_count_as_running(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "flatpak-com.valvesoftware.Steam.lock").write_text(
        "stale", encoding="utf-8",
    )
    (proc_root / "com.valvesoftware.Steam.socket").mkdir()
    _write_process(proc_root, 10, b"/usr/bin/steam\x00")
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.NATIVE


def test_reused_pid_with_unrelated_flatpak_identity_does_not_block(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/usr/libexec/flatpak-session-helper\x00",
        executable="/usr/lib/flatpak/flatpak-session-helper",
    )
    _write_cgroup(
        proc_root,
        10,
        "0::/user.slice/app-flatpak-com.valvesoftware.Steam-stale.scope\n",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.OFFLINE


def test_genuine_flatpak_steam_residual_blocks_only_while_process_is_live(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/app/steam/ubuntu12_64/steamwebhelper\x00",
        executable="/app/steam/ubuntu12_64/steamwebhelper",
    )
    _write_cgroup(
        proc_root,
        10,
        "0::/user.slice/app-flatpak-com.valvesoftware.Steam-123.scope\n",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.FLATPAK

    (proc_root / "10/cmdline").write_bytes(b"")
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.OFFLINE


def test_native_plus_genuine_flatpak_steam_residual_remains_blocked(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 10, b"/usr/bin/steam\x00")
    _write_process(
        proc_root,
        11,
        b"/app/steam/ubuntu12_64/steamwebhelper\x00",
        environ=b"FLATPAK_ID=com.valvesoftware.Steam\x00",
        executable="/app/steam/ubuntu12_64/steamwebhelper",
    )
    runtime = steam_native.resolve_steam_runtime_state(proc_root)
    assert runtime.state is SteamClientState.UNKNOWN
    assert runtime.native_client_running
    assert runtime.flatpak_process_running


def test_installation_presence_does_not_override_running_process_truth(tmp_path):
    installations = tmp_path / "installations"
    (installations / "native").mkdir(parents=True)
    (installations / "flatpak/com.valvesoftware.Steam").mkdir(parents=True)

    flatpak_only = tmp_path / "flatpak-only-proc"
    flatpak_only.mkdir()
    _write_process(
        flatpak_only,
        10,
        b"/app/bin/steam\x00",
        environ=b"FLATPAK_ID=com.valvesoftware.Steam\x00",
    )
    assert steam_native.detect_steam_client_state(flatpak_only) is SteamClientState.FLATPAK

    native_only = tmp_path / "native-only-proc"
    native_only.mkdir()
    _write_process(native_only, 20, b"/usr/bin/steam\x00")
    assert steam_native.detect_steam_client_state(native_only) is SteamClientState.NATIVE


def test_resolver_recomputes_mixed_unknown_across_reopen_or_restart(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root,
        10,
        b"/usr/bin/flatpak\x00run\x00com.valvesoftware.Steam\x00",
    )
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.FLATPAK
    _write_process(proc_root, 20, b"/usr/bin/steam\x00")
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.UNKNOWN
    assert steam_native.detect_steam_client_state(proc_root) is SteamClientState.UNKNOWN


def _item(mid: int, *, local=False):
    state = {
        "category": "local_only" if local else "workshop",
        "workshop_confirmed": not local,
        "local_delete_candidate": local,
        "requires_verification": False,
        "has_error": False,
    }
    return (
        f"Mod {mid}", mid, not local, True, "1 MB", "Today", 1024, 1.0,
        "Local only" if local else "Subscribed", state,
    )


def _action_overlay():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._loaded_items = [_item(1), _item(2, local=True)]
    overlay._selected_mod_ids = {1, 2}
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._mod_operation_running = False
    overlay._mod_operation_pending = False
    overlay._batch_unsubscribe_running = False
    overlay._batch_unsubscribe_stop_requested = False
    overlay._batch_unsubscribe_queue_count = 0
    overlay._repair_state_by_id = {}
    overlay._rows_cache = []
    overlay.loading_spinner = Widget(visible=False)
    overlay.btn_unsubscribe_selected = Widget()
    overlay.btn_cleanup_local_files = Widget()
    overlay.btn_unsubscribe_all_workshop = Widget(visible=True)
    overlay.btn_stop_batch_unsubscribe = Widget()
    overlay._set_steam_status_pill = lambda _status: None
    overlay._render_loaded_items = lambda: None
    return overlay


def test_native_enables_existing_mod_manager_mutation_actions(monkeypatch):
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: SteamClientState.NATIVE,
    )
    overlay = _action_overlay()
    overlay._update_batch_action_buttons()
    assert overlay.btn_unsubscribe_selected.sensitive is True
    assert overlay.btn_cleanup_local_files.visible is True
    assert overlay.btn_cleanup_local_files.sensitive is True
    assert overlay.btn_unsubscribe_all_workshop.sensitive is True


@pytest.mark.parametrize(
    "state",
    [SteamClientState.FLATPAK, SteamClientState.OFFLINE, SteamClientState.UNKNOWN],
)
def test_unsupported_or_ambiguous_steam_exposes_no_destructive_actions(
        monkeypatch, state):
    monkeypatch.setattr(mods_ui, "detect_steam_client_state", lambda: state)
    overlay = _action_overlay()
    overlay._set_authoritative_steam_state(state)
    overlay._update_batch_action_buttons()
    assert overlay.btn_unsubscribe_selected.sensitive is False
    assert overlay.btn_cleanup_local_files.visible is False
    assert overlay.btn_cleanup_local_files.sensitive is False
    assert overlay.btn_unsubscribe_all_workshop.sensitive is False
    classification = overlay._selected_mod_classification()
    assert classification["local_cleanup_candidates"] == 0
    assert classification["unsubscribe_candidates"] == 0
    assert classification["steam_offline"] == 2


def test_switching_client_state_refreshes_action_availability(monkeypatch):
    current = {"state": SteamClientState.NATIVE}
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: current["state"],
    )
    overlay = _action_overlay()

    overlay._update_batch_action_buttons()
    assert overlay.btn_cleanup_local_files.sensitive is True

    current["state"] = SteamClientState.FLATPAK
    overlay._set_authoritative_steam_state(current["state"])
    overlay._update_batch_action_buttons()
    assert overlay.btn_cleanup_local_files.visible is False
    assert overlay.btn_unsubscribe_selected.sensitive is False

    current["state"] = SteamClientState.OFFLINE
    overlay._set_authoritative_steam_state(current["state"])
    overlay._update_batch_action_buttons()
    assert overlay.btn_unsubscribe_all_workshop.sensitive is False

    current["state"] = SteamClientState.NATIVE
    overlay._set_authoritative_steam_state(current["state"])
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._update_batch_action_buttons()
    assert overlay.btn_cleanup_local_files.sensitive is True
    assert overlay.btn_unsubscribe_selected.sensitive is True


def _transition_overlay(initial_state):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._authoritative_steam_state = initial_state
    overlay._steam_state_generation = 0
    overlay._inventory_refresh_generation = 0
    overlay._steam_management_verified = initial_state is SteamClientState.NATIVE
    overlay._native_session_handoff_valid = initial_state is SteamClientState.NATIVE
    overlay._last_mod_state_query_ok = initial_state is SteamClientState.NATIVE
    overlay._inventory_validity = (
        InventoryValidity.VALID
        if initial_state is SteamClientState.NATIVE else InventoryValidity.STALE
    )
    overlay._subscription_snapshot = None
    overlay._inventory_refresh_active = False
    overlay._inventory_refresh_pending = False
    overlay._steam_state_probe_running = False
    overlay._steam_state_probe_refresh_on_change = False
    overlay._loaded_items = []
    overlay._set_steam_status_pill = lambda _state: None
    overlay._render_loaded_items = lambda: None
    overlay._update_batch_action_buttons = lambda: None
    overlay._mod_manager_is_visible = lambda: True
    overlay.refreshes = []
    overlay.refresh = lambda **kwargs: overlay.refreshes.append(dict(kwargs))
    return overlay


def test_native_disappearance_invalidates_handoff_and_refreshes_once(monkeypatch):
    current = {"state": SteamClientState.NATIVE}
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: current["state"],
    )
    overlay = _transition_overlay(SteamClientState.NATIVE)

    assert not overlay._set_authoritative_steam_state(current["state"])
    assert overlay.refreshes == []

    current["state"] = SteamClientState.FLATPAK
    assert overlay._set_authoritative_steam_state(current["state"])
    overlay.refresh(preserve_status=True)
    assert overlay._authoritative_steam_state is SteamClientState.FLATPAK
    assert overlay._steam_management_verified is False
    assert overlay._native_session_handoff_valid is False
    assert len(overlay.refreshes) == 1


def test_reopening_mod_manager_observes_current_state_instead_of_stale_flatpak(
        monkeypatch):
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: SteamClientState.NATIVE,
    )
    monkeypatch.setattr(
        mods_ui,
        "resolve_steam_runtime_state",
        lambda: steam_native.SteamRuntimeEvidence(
            SteamClientState.NATIVE,
            native_client_running=True,
        ),
    )
    overlay = _transition_overlay(SteamClientState.FLATPAK)
    overlay.scrim = Widget()
    overlay.card = Widget()
    overlay.search = SimpleNamespace(grab_focus=lambda: None)
    overlay._clear_one_shot_action_status_if_idle = lambda: None
    overlay._maybe_start_passive_steam_watch = lambda: None
    overlay._sync_repair_presentations = lambda: None
    overlay._repair_refresh_needed = False
    monkeypatch.setattr(
        mods_ui.threading,
        "Thread",
        lambda target, daemon: SimpleNamespace(start=target),
    )
    monkeypatch.setattr(
        mods_ui.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    overlay.show()
    assert overlay._authoritative_steam_state is SteamClientState.NATIVE
    assert len(overlay.refreshes) == 1

    assert not overlay._set_authoritative_steam_state(SteamClientState.NATIVE)
    assert len(overlay.refreshes) == 1


def test_stale_inventory_result_is_discarded_after_runtime_transition(monkeypatch):
    current = {"state": SteamClientState.FLATPAK}
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: current["state"],
    )
    overlay = _transition_overlay(SteamClientState.NATIVE)
    overlay._inventory_refresh_generation = 7
    applied = []
    overlay._apply_items = lambda *_args: applied.append(True)
    items = mods_ui._LoadedModItems(
        [_item(1)],
        steam_state=SteamClientState.NATIVE,
        state_query_ok=True,
        observed_end_state=SteamClientState.FLATPAK,
    )
    overlay._apply_items_if_current(
        items,
        None,
        False,
        7,
        SteamClientState.NATIVE,
    )
    assert applied == []
    assert overlay._authoritative_steam_state is SteamClientState.FLATPAK
    assert overlay._native_session_handoff_valid is False
    assert len(overlay.refreshes) == 1


def test_closing_mod_manager_stops_runtime_state_watcher(monkeypatch):
    removed = []
    monkeypatch.setattr(
        mods_ui.GLib, "source_remove", lambda timer_id: removed.append(timer_id),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._passive_steam_watch_timer_id = 42
    overlay._stop_passive_steam_watch()
    assert removed == [42]
    assert overlay._passive_steam_watch_timer_id == 0


def test_flatpak_disables_individual_repair_control(monkeypatch):
    current = {"state": SteamClientState.NATIVE}
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: current["state"],
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.VALID
    button = Widget()
    control = SimpleNamespace(
        _dzll_repair_button=button,
        _dzll_repair_spinner=Widget(),
        _dzll_repair_percent=Widget(),
        set_visible_child_name=lambda _name: None,
    )
    row = SimpleNamespace(
        _dzll_repair_progress_class="",
        _dzll_repair_control=control,
        _dzll_select_check=None,
        remove_css_class=lambda _name: None,
        add_css_class=lambda _name: None,
    )
    overlay._apply_repair_state_to_row(row, None)
    assert button.sensitive is True
    current["state"] = SteamClientState.FLATPAK
    overlay._set_authoritative_steam_state(current["state"])
    overlay._apply_repair_state_to_row(row, None)
    assert button.sensitive is False


def test_flatpak_load_uses_neutral_offline_rows_and_never_queries_ugc(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: SteamClientState.FLATPAK,
    )
    monkeypatch.setattr(mods_ui, "_candidate_workshop_roots", lambda **_kwargs: [tmp_path])
    monkeypatch.setattr(mods_ui, "_read_installed_mod_ids_from_roots", lambda _roots: [123])
    monkeypatch.setattr(mods_ui, "_name_map_from_symlinks", lambda **_kwargs: {})
    monkeypatch.setattr(mods_ui, "load_mod_metadata", lambda: {"mods": {}})
    monkeypatch.setattr(mods_ui, "resolve_best_mod_names", lambda *_args, **_kwargs: {123: "Flatpak Mod"})
    monkeypatch.setattr(mods_ui, "_content_folder_for_mod", lambda *_args: None)
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_state_checked",
        lambda *_args, **_kwargs: pytest.fail("Flatpak state must not be queried"),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._last_mod_state_query_ok = True
    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.FLATPAK,
    )
    assert len(items) == 1
    assert items[0][8] == "Steam Offline"
    assert items[0][9] == {
        "category": "steam_offline",
        "workshop_confirmed": False,
        "local_delete_candidate": False,
        "requires_verification": True,
        "has_error": False,
    }
    assert items.state_query_ok is False


@pytest.mark.parametrize("state", [SteamClientState.FLATPAK, SteamClientState.UNKNOWN])
def test_backend_mutation_boundaries_reject_unsupported_steam(
        monkeypatch, state):
    monkeypatch.setattr(steam_native, "detect_steam_client_state", lambda: state)
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_helper_json_lines",
        lambda *_args, **_kwargs: pytest.fail("helper mutation must not start"),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_steam_roots",
        lambda: pytest.fail("local deletion must not start"),
    )

    assert steam_ugc_backend.request_unsubscribe_ugc_items([123])[0] is False
    repair = steam_ugc_backend.repair_ugc_item(123)
    assert repair["reason"] == "unsupported_steam"
    cleanup = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        123, native_session_verified=True,
    )
    assert cleanup["ok"] is False
    assert "refusing local Workshop cleanup" in cleanup["error"]


def test_native_backend_unsubscribe_request_preserves_existing_path(monkeypatch):
    monkeypatch.setattr(
        steam_native, "detect_steam_client_state", lambda: SteamClientState.NATIVE,
    )
    calls = []
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_helper_json_lines",
        lambda command, **kwargs: calls.append((command, kwargs)) or (True, 0),
    )
    ok, _states = steam_ugc_backend.request_unsubscribe_ugc_items([123])
    assert ok
    assert calls[0][0] == "unsubscribe-request"
    assert calls[0][1]["mod_ids"] == [123]


def test_repair_aborts_if_native_client_becomes_unsupported_before_reinstall(
        monkeypatch):
    states = iter(
        [
            (True, SteamClientState.NATIVE),
            (False, SteamClientState.FLATPAK),
        ]
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: next(states),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "request_unsubscribe_ugc_items",
        lambda *_args, **_kwargs: (True, {}),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda ids, **_kwargs: (
            True,
            {int(ids[0]): {"subscribed": False, "installed": False}},
        ),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "run_ugc_install",
        lambda *_args, **_kwargs: pytest.fail("reinstall must not start"),
    )
    result = steam_ugc_backend.repair_ugc_item(
        123,
        content_exists_fn=lambda _mid, _state: False,
    )
    assert result["reason"] == "unsupported_steam"
    assert result["not_installed"] is True


def test_programmatic_mod_manager_mutation_paths_fail_closed_for_flatpak(monkeypatch):
    monkeypatch.setattr(
        mods_ui, "detect_steam_client_state", lambda: SteamClientState.FLATPAK,
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay.host = SimpleNamespace(_win=SimpleNamespace())
    overlay._steam_management_verified = True
    overlay._last_mod_state_query_ok = True
    overlay._mod_operation_running = False
    overlay._mod_operation_pending = False
    overlay._batch_unsubscribe_running = False
    overlay._repair_lease = None
    overlay._release_repair_lease = lambda: None
    overlay._set_steam_status_pill = lambda _status: None
    overlay._update_batch_action_buttons = lambda: None
    statuses = []
    overlay._set_mod_operation_status = (
        lambda text="", running=False: statuses.append((text, running))
    )

    overlay._start_repair(123)
    overlay._run_batch_unsubscribe([123], 0)
    overlay._run_batch_local_cleanup([123], close_steam=True)
    assert len(statuses) == 3
    assert all("native Steam" in text for text, _running in statuses)
