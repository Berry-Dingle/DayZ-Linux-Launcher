from types import SimpleNamespace
import time

import pytest

from dzll_launcher import mods_ui, steam_ugc_backend, steam_ugc_helper
from dzll_launcher.mods_ui import InventoryValidity, ModsManagerOverlay
from dzll_launcher.steam_native import SteamClientState
from dzll_launcher.steam_ugc_backend import UGCSubscriptionSnapshot


class Widget:
    def __init__(self, *, visible=False, active=False):
        self.visible = visible
        self.active = active
        self.sensitive = None

    def get_visible(self):
        return self.visible

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def get_active(self):
        return self.active

    def set_size_request(self, *_args):
        return None


def _snapshot(
    ids,
    subscribed=(),
    *,
    valid=True,
    logged_on=True,
    created_monotonic=None,
):
    requested = frozenset(int(mid) for mid in ids)
    subscribed_ids = frozenset(int(mid) for mid in subscribed)
    states = {
        mid: {
            "id": mid,
            "subscribed": mid in subscribed_ids,
            "installed": True,
            "state_names": ["Subscribed", "Installed"] if mid in subscribed_ids else ["Installed"],
        }
        for mid in requested
    }
    return UGCSubscriptionSnapshot(
        valid=valid,
        requested_ids=requested,
        subscribed_ids=subscribed_ids if valid else frozenset(),
        states=states if valid else {},
        logged_on=logged_on,
        steam_id=76561198000000001 if logged_on else 0,
        native_attachment_verified=bool(valid and logged_on),
        created_monotonic=(
            time.monotonic()
            if created_monotonic is None
            else float(created_monotonic)
        ),
    )


def _patch_inventory_files(monkeypatch, tmp_path, ids):
    monkeypatch.setattr(
        mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.NATIVE,
    )
    monkeypatch.setattr(mods_ui.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mods_ui, "_candidate_workshop_roots", lambda **_kwargs: [tmp_path])
    monkeypatch.setattr(mods_ui, "_read_installed_mod_ids_from_roots", lambda _roots: list(ids))
    monkeypatch.setattr(mods_ui, "_name_map_from_symlinks", lambda **_kwargs: {})
    monkeypatch.setattr(mods_ui, "load_mod_metadata", lambda: {"mods": {}})
    monkeypatch.setattr(
        mods_ui,
        "resolve_best_mod_names",
        lambda values, **_kwargs: {int(mid): f"Mod {mid}" for mid in values},
    )
    monkeypatch.setattr(mods_ui, "_content_folder_for_mod", lambda *_args: tmp_path)


def test_native_inventory_requires_complete_authoritative_subscription_snapshot(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda ids, **_kwargs: calls.append(list(ids)) or _snapshot(ids, subscribed=[10]),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)

    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )

    assert calls == [[10, 20], [10, 20]]
    assert items.inventory_validity is InventoryValidity.VALID
    assert items[0][8] == "Subscribed"
    assert items[0][9]["category"] == "workshop"
    assert items[1][8] == "Local only"
    assert items[1][9]["local_delete_candidate"] is True


def test_bounded_native_retry_accepts_only_two_matching_complete_snapshots(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    results = iter((
        _snapshot([10, 20], valid=False, logged_on=False),
        _snapshot([10, 20], subscribed=[10, 20]),
        _snapshot([10, 20], subscribed=[10, 20]),
    ))
    calls = []

    def query(ids, **_kwargs):
        calls.append(list(ids))
        return next(results)

    monkeypatch.setattr(mods_ui, "query_ugc_inventory_checked", query)
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )
    assert len(calls) == 3
    assert items.inventory_validity is InventoryValidity.VALID
    assert all(item[8] == "Subscribed" for item in items)


def test_native_authority_change_during_retry_invalidates_inventory(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    states = iter((SteamClientState.NATIVE, SteamClientState.UNKNOWN))
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: next(states))
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda ids, **_kwargs: _snapshot(ids, subscribed=[10, 20]),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )
    assert items.inventory_validity is InventoryValidity.FAILED
    assert all(item[8] == "Could not check" for item in items)


def test_steam_account_identity_change_during_retry_invalidates_inventory(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    first = _snapshot([10, 20], subscribed=[10, 20])
    second = UGCSubscriptionSnapshot(
        valid=True,
        requested_ids=first.requested_ids,
        subscribed_ids=first.subscribed_ids,
        states=first.states,
        logged_on=True,
        steam_id=76561198000000002,
        native_attachment_verified=True,
        created_monotonic=2.0,
    )
    results = iter((first, second))
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda _ids, **_kwargs: next(results),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )
    assert items.inventory_validity is InventoryValidity.FAILED


def test_mixed_runtime_never_queries_or_classifies_partial_native_inventory(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda *_args, **_kwargs: pytest.fail("mixed runtime must not query UGC"),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.UNKNOWN,
    )
    assert items.inventory_validity is InventoryValidity.FAILED
    assert all(item[8] == "Steam Offline" for item in items)
    assert not any(item[9]["local_delete_candidate"] for item in items)


@pytest.mark.parametrize("valid", [False])
def test_failed_or_incomplete_native_inventory_is_neutral_and_non_destructive(
        monkeypatch, tmp_path, valid):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda ids, **_kwargs: _snapshot(ids, valid=valid, logged_on=False),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)

    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )

    assert items.inventory_validity is InventoryValidity.FAILED
    assert all(item[8] == "Could not check" for item in items)
    assert all(item[9]["requires_verification"] for item in items)
    assert not any(item[9]["local_delete_candidate"] for item in items)


def test_empty_subscription_inventory_never_turns_all_disk_mods_into_local_only(
        monkeypatch, tmp_path):
    _patch_inventory_files(monkeypatch, tmp_path, [10, 20])
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_inventory_checked",
        lambda ids, **_kwargs: _snapshot(ids, subscribed=[]),
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)

    items = overlay._load_installed_items(
        str(tmp_path), "", steam_state=SteamClientState.NATIVE,
    )

    assert all(item[8] == "Could not check" for item in items)
    assert not any(item[9]["local_delete_candidate"] for item in items)


def test_stale_local_only_row_becomes_neutral_as_soon_as_native_snapshot_is_loading():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.LOADING
    stale_local = (
        "Old", 10, False, True, "1 MB", "Today", 1, 1.0, "Local only",
        {
            "category": "local_only",
            "workshop_confirmed": False,
            "local_delete_candidate": True,
            "requires_verification": False,
            "has_error": False,
        },
    )

    state = overlay._selection_state_for_item(stale_local)
    assert state["category"] == "check_failed"
    assert state["requires_verification"] is True
    assert state["local_delete_candidate"] is False


def _refresh_overlay():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._steam_state_generation = 3
    overlay._inventory_refresh_generation = 0
    overlay._inventory_refresh_active = False
    overlay._inventory_refresh_pending = False
    overlay._inventory_refresh_pending_status = None
    overlay._inventory_refresh_pending_preserve_status = False
    overlay._loaded_items = []
    overlay._workshop_dir = lambda: "/workshop"
    overlay._proton_prefix = lambda: "/prefix"
    overlay._set_steam_status_pill = lambda _status: None
    overlay._set_loading = lambda _loading: None
    overlay._render_loaded_items = lambda: None
    overlay._mod_manager_is_visible = lambda: True
    return overlay


def test_refresh_requests_are_coalesced_and_only_one_inventory_worker_starts(
        monkeypatch):
    overlay = _refresh_overlay()
    workers = []

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(mods_ui.threading, "Thread", DeferredThread)
    overlay.refresh()
    overlay.refresh(preserve_status=True)
    overlay.refresh(completion_status="Latest")

    assert len(workers) == 1
    assert overlay._inventory_refresh_active is True
    assert overlay._inventory_refresh_pending is True
    assert overlay._inventory_refresh_pending_status == "Latest"


def test_flatpak_to_native_transition_runs_one_fresh_native_inventory_load(
        monkeypatch):
    overlay = _refresh_overlay()
    overlay._authoritative_steam_state = SteamClientState.FLATPAK
    overlay._steam_state_generation = 1
    loads = []
    applied = []

    def load(_workshop, _prefix, *, steam_state, cancel_event=None):
        loads.append(steam_state)
        return mods_ui._LoadedModItems(
            [],
            steam_state=steam_state,
            state_query_ok=False,
            inventory_validity=InventoryValidity.FAILED,
            observed_end_state=steam_state,
        )

    overlay._load_installed_items = load
    overlay._apply_items = lambda items, *_args: applied.append(items.steam_state) or False
    monkeypatch.setattr(
        mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.NATIVE,
    )
    monkeypatch.setattr(
        mods_ui.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        mods_ui.threading,
        "Thread",
        lambda target, daemon: SimpleNamespace(start=target),
    )

    changed = overlay._set_authoritative_steam_state(SteamClientState.NATIVE)
    assert changed
    overlay.refresh()
    assert loads == [SteamClientState.NATIVE]
    assert applied == [SteamClientState.NATIVE]


def test_runtime_watcher_coalesces_process_scans_and_never_scans_in_row_callback(
        monkeypatch):
    overlay = _refresh_overlay()
    overlay._steam_state_probe_running = False
    overlay._steam_state_probe_refresh_on_change = False
    workers = []

    class DeferredThread:
        def __init__(self, *, target, daemon):
            workers.append(target)

        def start(self):
            return None

    monkeypatch.setattr(mods_ui.threading, "Thread", DeferredThread)
    overlay._request_authoritative_steam_state_probe(refresh_on_change=True)
    overlay._request_authoritative_steam_state_probe(refresh_on_change=True)
    assert len(workers) == 1

    monkeypatch.setattr(
        mods_ui,
        "detect_steam_client_state",
        lambda: pytest.fail("ordinary row callback must not scan Steam processes"),
    )
    overlay._loaded_items = []
    overlay._selected_mod_ids = set()
    overlay._steam_management_verified = False
    overlay._native_session_handoff_valid = False
    overlay._inventory_validity = InventoryValidity.STALE
    overlay._sync_selection_ui = lambda: None
    overlay._on_mod_selection_toggled(Widget(active=True), 10)
    assert overlay._selected_mod_ids == {10}


def test_backend_inventory_rejects_missing_partial_and_inconsistent_results(monkeypatch):
    cases = [
        ([
            {"type": "item", "id": 10, "subscribed": False},
            {
                "type": "done", "ok": True, "logged_on": True,
                "subscription_inventory_complete": True,
                "subscribed_item_ids": [],
            },
        ], True),
        ([
            {"type": "item", "id": 10, "subscribed": False},
            {"type": "item", "id": 20, "subscribed": False},
            {
                "type": "done", "ok": True, "logged_on": True,
                "subscription_inventory_complete": True,
                "subscribed_item_ids": [10],
            },
        ], True),
        ([
            {"type": "item", "id": 10, "subscribed": True},
            {"type": "item", "id": 20, "subscribed": False},
            {
                "type": "done", "ok": False, "logged_on": True,
                "subscription_inventory_complete": True,
                "subscribed_item_ids": [10],
            },
        ], False),
        ([
            {"type": "item", "id": 10, "subscribed": True},
            {"type": "item", "id": 20, "subscribed": False},
            {
                "type": "done", "ok": True, "logged_on": False,
                "subscription_inventory_complete": True,
                "subscribed_item_ids": [10],
            },
        ], True),
    ]

    for events, command_ok in cases:
        def run(_command, *, on_event, **_kwargs):
            for event in events:
                on_event(dict(event))
            return command_ok, 0

        monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", run)
        result = steam_ugc_backend.query_ugc_inventory_checked([10, 20])
        assert result.valid is False
        assert result.states == {}
        assert result.subscribed_ids == frozenset()


def test_backend_inventory_accepts_complete_consistent_native_result(monkeypatch):
    def run(_command, *, on_event, **_kwargs):
        on_event({
            "type": "init", "ok": True, "appid": 221100,
            "steam_root": "/native/Steam",
            "lib": "/native/Steam/steamrt64/libsteam_api.so",
            "flatpak_environment": False,
        })
        on_event({"type": "item", "id": 10, "subscribed": True})
        on_event({"type": "item", "id": 20, "subscribed": False})
        on_event({
            "type": "done", "ok": True, "logged_on": True,
            "subscription_inventory_complete": True,
            "subscription_count": 1,
            "subscribed_item_ids": [10],
            "steam_id": 76561198000000001,
        })
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", run)
    result = steam_ugc_backend.query_ugc_inventory_checked([10, 20])
    assert result.valid is True
    assert result.subscribed_ids == frozenset({10})
    assert result.proves_unsubscribed(20)
    assert not result.proves_unsubscribed(10)


def test_backend_rejects_unproven_or_flatpak_helper_attachment(monkeypatch):
    def run(_command, *, on_event, **_kwargs):
        on_event({
            "type": "init", "ok": True, "appid": 221100,
            "steam_root": "/home/user/.var/app/com.valvesoftware.Steam",
            "lib": "/home/user/.var/app/com.valvesoftware.Steam/libsteam_api.so",
            "flatpak_environment": True,
        })
        on_event({"type": "item", "id": 10, "subscribed": True})
        on_event({
            "type": "done", "ok": True, "logged_on": True,
            "steam_id": 76561198000000001,
            "subscription_inventory_complete": True,
            "subscription_count": 1,
            "subscribed_item_ids": [10],
        })
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", run)
    result = steam_ugc_backend.query_ugc_inventory_checked([10])
    assert result.valid is False
    assert result.native_attachment_verified is False


def test_helper_rejects_short_subscription_buffer_as_partial():
    helper = steam_ugc_helper.SteamUGC.__new__(steam_ugc_helper.SteamUGC)
    helper.ugc = object()
    helper.GetNumSubscribedItems = lambda _ugc: 5

    def get_items(_ugc, values, _maximum):
        values[0] = 10
        values[1] = 20
        return 2

    helper.GetSubscribedItems = get_items
    with pytest.raises(steam_ugc_helper.SteamInitError, match="partial.*2/5"):
        helper.subscribed_item_ids()


def test_helper_environment_removes_flatpak_and_runtime_contamination(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "com.valvesoftware.Steam")
    monkeypatch.setenv("FLATPAK_TTY_PROGRESS", "1")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/lib")
    monkeypatch.setenv("STEAM_RUNTIME", "/app/steam-runtime")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/u/.var/app/com.valvesoftware.Steam/data")
    env = steam_ugc_backend._helper_env(strict_native_attachment=True)
    assert "FLATPAK_ID" not in env
    assert "FLATPAK_TTY_PROGRESS" not in env
    assert "LD_LIBRARY_PATH" not in env
    assert "STEAM_RUNTIME" not in env
    assert "XDG_DATA_HOME" not in env


def test_helper_requires_stable_authenticated_subscription_enumeration(monkeypatch):
    class Steam:
        def __init__(self):
            self.samples = iter(([10], [10], [10]))
            self.callback_count = 0

        def run_callbacks(self):
            self.callback_count += 1

        def is_logged_on(self):
            return True

        def steam_id(self):
            return 76561198000000001

        def subscribed_item_ids(self):
            return list(next(self.samples))

    monkeypatch.setattr(steam_ugc_helper.time, "sleep", lambda _seconds: None)
    steam = Steam()
    logged_on, complete, subscribed, steam_id = (
        steam_ugc_helper._capture_subscription_inventory(steam)
    )
    assert logged_on is True
    assert complete is True
    assert subscribed == [10]
    assert steam_id == 76561198000000001
    assert steam.callback_count == 3


def test_backend_local_delete_requires_authoritative_target_snapshot(monkeypatch):
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_steam_roots",
        lambda: pytest.fail("filesystem deletion must not begin"),
    )
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        20,
        steam_absence_verified=True,
        subscription_snapshot=None,
    )
    assert result["ok"] is False
    assert "authoritative subscription snapshot" in result["error"]


def _local_cleanup_overlay():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._require_supported_native_steam = lambda: True
    overlay._set_mod_operation_status_calls = []
    overlay._set_mod_operation_status = (
        lambda text="", running=False: overlay._set_mod_operation_status_calls.append(
            (text, running)
        )
    )
    overlay._suppress_start_steam_manage_prompt_for_current_operation = lambda: None
    overlay.refresh_calls = []
    overlay.refresh = lambda **kwargs: overlay.refresh_calls.append(kwargs)
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._steam_state_generation = 1
    overlay._local_cleanup_transaction_generation = 0
    overlay._local_cleanup_authorization_request = None
    overlay._local_cleanup_transaction = None
    return overlay


def _cleanup_transaction(overlay, snapshot, ids=(20,)):
    request = overlay._begin_local_cleanup_authorization(list(ids))
    assert request is not None
    transaction = overlay._mint_local_cleanup_transaction(
        request,
        list(ids),
        snapshot,
    )
    assert transaction is not None
    return transaction


def _run_threads_inline(monkeypatch):
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


def test_local_cleanup_running_shutdown_uses_fresh_offline_evidence(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20], subscribed=[10])
    state = {"value": SteamClientState.NATIVE}
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: state["value"])
    overlay._verify_local_cleanup_candidates = lambda ids, timeout=20: (
        True,
        {"safe_ids": list(ids), "subscription_snapshot": snapshot},
        "",
    )
    monkeypatch.setattr(mods_ui, "resolve_native_steam_cmd", lambda: "/fake/steam")
    monkeypatch.setattr(
        mods_ui.subprocess,
        "run",
        lambda *_args, **_kwargs: state.__setitem__("value", SteamClientState.OFFLINE),
    )
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **kwargs: calls.append((mid, kwargs)) or {"ok": True},
    )
    transaction = _cleanup_transaction(overlay, snapshot)

    overlay._run_batch_local_cleanup(
        [20], close_steam=True, cleanup_transaction=transaction,
    )

    assert [mid for mid, _kwargs in calls] == [20]
    assert calls[0][1]["steam_absence_verified"] is True
    assert calls[0][1]["subscription_snapshot"] is snapshot
    assert overlay.refresh_calls[-1]["completion_status"] == "Steam closed · Cleaned up: 1"


def test_local_cleanup_already_stopped_with_authoritative_snapshot_is_allowed(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20], subscribed=[10])
    monkeypatch.setattr(
        mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.OFFLINE,
    )
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **kwargs: calls.append((mid, kwargs)) or {"ok": True},
    )
    transaction = _cleanup_transaction(overlay, snapshot)

    overlay._run_batch_local_cleanup(
        [20],
        close_steam=False,
        cleanup_transaction=transaction,
    )

    assert [mid for mid, _kwargs in calls] == [20]
    assert calls[0][1]["steam_absence_verified"] is True
    assert overlay.refresh_calls[-1]["completion_status"] == "Cleaned up: 1"


def test_cleanup_transaction_survives_watcher_offline_authority_invalidation(
        monkeypatch, tmp_path):
    _run_threads_inline(monkeypatch)
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    snapshot = _snapshot([10, 20], subscribed=[10])
    overlay._subscription_snapshot = snapshot
    overlay._mod_operation_running = False
    overlay._batch_unsubscribe_running = False
    overlay._mod_operation_pending = False
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._last_mod_state_query_ok = True
    overlay._steam_state_generation = 1
    overlay._local_cleanup_transaction_generation = 0
    overlay._local_cleanup_authorization_request = None
    overlay._local_cleanup_transaction = None
    overlay._selected_local_cleanup_candidate_ids = lambda: [20]
    overlay._selected_mod_ids_snapshot = lambda: [20]
    overlay._set_mod_operation_pending = lambda value: setattr(
        overlay, "_mod_operation_pending", bool(value)
    )
    overlay._set_mod_operation_status = lambda *_args, **_kwargs: None
    overlay._workshop_dir = lambda: str(tmp_path)
    overlay._set_steam_status_pill = lambda _status: None
    overlay._update_batch_action_buttons = lambda: None
    overlay._suppress_start_steam_manage_prompt_for_current_operation = lambda: None
    overlay.refresh_calls = []
    overlay.refresh = lambda **kwargs: overlay.refresh_calls.append(kwargs)
    state = {"value": SteamClientState.NATIVE}
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: state["value"])
    monkeypatch.setattr(mods_ui, "query_ugc_inventory_checked", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(mods_ui, "_candidate_workshop_roots", lambda **_kwargs: [tmp_path])
    monkeypatch.setattr(mods_ui, "_has_local_workshop_state", lambda _roots, _mid: True)
    backend_calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **kwargs: backend_calls.append((mid, kwargs)) or {"ok": True},
    )
    global_authority_after_offline = []
    original_mint = overlay._mint_local_cleanup_transaction

    def mint_then_observe_offline(*args, **kwargs):
        transaction = original_mint(*args, **kwargs)
        state["value"] = SteamClientState.OFFLINE
        overlay._set_authoritative_steam_state(SteamClientState.OFFLINE)
        global_authority_after_offline.append(overlay._steam_management_is_verified())
        return transaction

    overlay._mint_local_cleanup_transaction = mint_then_observe_offline
    overlay._confirm_show = lambda _title, _body, _ok, callback, **_kwargs: callback(True)

    overlay._start_batch_local_cleanup_selected()

    assert global_authority_after_offline == [False]
    assert overlay._subscription_snapshot is None
    assert [mid for mid, _kwargs in backend_calls] == [20]
    assert backend_calls[0][1]["subscription_snapshot"] is snapshot
    assert overlay._local_cleanup_transaction is None


def test_offline_only_session_cannot_begin_cleanup_authorization(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    overlay._authoritative_steam_state = SteamClientState.OFFLINE
    overlay._steam_management_verified = False
    overlay._native_session_handoff_valid = False
    overlay._inventory_validity = InventoryValidity.STALE
    overlay._mod_operation_running = False
    overlay._batch_unsubscribe_running = False
    overlay._mod_operation_pending = False
    prompts = []
    overlay._show_start_steam_manage_prompt = lambda: prompts.append(True)

    assert overlay._begin_local_cleanup_authorization([20]) is None
    overlay._start_batch_local_cleanup_selected()
    assert overlay._local_cleanup_authorization_request is None
    assert overlay._local_cleanup_transaction is None
    assert prompts == [True]


def test_cleanup_without_transaction_fails_before_local_deletion(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda *_args, **_kwargs: pytest.fail("local deletion must not begin"),
    )
    overlay._run_batch_local_cleanup([20], close_steam=False)
    assert "No current authorized cleanup transaction" in (
        overlay._set_mod_operation_status_calls[-1][0]
    )


def test_local_cleanup_accepts_independent_exact_offline_exit_during_wait(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20], subscribed=[10])
    state = {"value": SteamClientState.NATIVE}
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: state["value"])
    overlay._verify_local_cleanup_candidates = lambda ids, timeout=20: (
        True,
        {"safe_ids": list(ids), "subscription_snapshot": snapshot},
        "",
    )
    monkeypatch.setattr(mods_ui, "resolve_native_steam_cmd", lambda: "/fake/steam")
    monkeypatch.setattr(mods_ui.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mods_ui,
        "_wait_for_native_steam_stopped",
        lambda **_kwargs: state.__setitem__("value", SteamClientState.OFFLINE) or True,
    )
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **kwargs: calls.append((mid, kwargs)) or {"ok": True},
    )
    transaction = _cleanup_transaction(overlay, snapshot)

    overlay._run_batch_local_cleanup(
        [20], close_steam=True, cleanup_transaction=transaction,
    )

    assert [mid for mid, _kwargs in calls] == [20]
    assert calls[0][1]["steam_absence_verified"] is True


def test_local_cleanup_shutdown_timeout_preserves_files(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20], subscribed=[10])
    monkeypatch.setattr(
        mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.NATIVE,
    )
    overlay._verify_local_cleanup_candidates = lambda ids, timeout=20: (
        True,
        {"safe_ids": list(ids), "subscription_snapshot": snapshot},
        "",
    )
    monkeypatch.setattr(mods_ui, "resolve_native_steam_cmd", lambda: "/fake/steam")
    monkeypatch.setattr(mods_ui.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mods_ui, "_wait_for_native_steam_stopped", lambda **_kwargs: False)
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda *_args, **_kwargs: pytest.fail("local deletion must not begin"),
    )
    transaction = _cleanup_transaction(overlay, snapshot)

    overlay._run_batch_local_cleanup(
        [20], close_steam=True, cleanup_transaction=transaction,
    )

    assert "Steam did not close in time" in overlay.refresh_calls[-1]["completion_status"]


@pytest.mark.parametrize(
    "state",
    [SteamClientState.UNKNOWN, SteamClientState.FLATPAK],
)
def test_local_cleanup_ambiguous_or_flatpak_state_fails_closed(monkeypatch, state):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: state)
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda *_args, **_kwargs: pytest.fail("local deletion must not begin"),
    )
    transaction = _cleanup_transaction(
        overlay, _snapshot([10, 20], subscribed=[10]),
    )

    overlay._run_batch_local_cleanup(
        [20],
        close_steam=False,
        cleanup_transaction=transaction,
    )

    assert "Could not verify that Steam is stopped" in (
        overlay.refresh_calls[-1]["completion_status"]
    )


@pytest.mark.parametrize(
    "state",
    [SteamClientState.UNKNOWN, SteamClientState.FLATPAK],
)
def test_native_shutdown_wait_accepts_only_exact_offline(monkeypatch, state):
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: state)
    assert mods_ui._wait_for_native_steam_stopped(timeout_s=0) is False


def test_backend_local_delete_requires_explicit_absence_proof(monkeypatch):
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_steam_roots",
        lambda: pytest.fail("filesystem deletion must not begin"),
    )
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        20,
        subscription_snapshot=_snapshot([10, 20], subscribed=[10]),
    )
    assert result["ok"] is False
    assert "authoritative proof" in result["error"]


def test_destructive_cleanup_snapshot_freshness_contract():
    now = time.monotonic()
    fresh = _snapshot(
        [10, 20],
        subscribed=[10],
        created_monotonic=now,
    )
    stale = _snapshot(
        [10, 20],
        subscribed=[10],
        created_monotonic=(
            now
            - steam_ugc_backend.UGC_DESTRUCTIVE_CLEANUP_SNAPSHOT_MAX_AGE_S
            - 1.0
        ),
    )
    assert fresh.is_fresh_for_destructive_cleanup(now_monotonic=now)
    assert not stale.is_fresh_for_destructive_cleanup(now_monotonic=now)


def test_backend_stale_snapshot_preserves_guarded_tree(monkeypatch, tmp_path):
    mod_id = 20
    content = (
        tmp_path
        / "steamapps/workshop/content"
        / str(steam_ugc_backend.DAYZ_APPID)
        / str(mod_id)
    )
    content.mkdir(parents=True)
    (content / "synthetic.pbo").write_bytes(b"preserve")
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [tmp_path])
    stale = _snapshot(
        [10, mod_id],
        subscribed=[10],
        created_monotonic=(
            time.monotonic()
            - steam_ugc_backend.UGC_DESTRUCTIVE_CLEANUP_SNAPSHOT_MAX_AGE_S
            - 1.0
        ),
    )
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        mod_id,
        steam_absence_verified=True,
        subscription_snapshot=stale,
    )
    assert result["ok"] is False
    assert "current authoritative subscription snapshot" in result["error"]
    assert content.is_dir()
    assert (content / "synthetic.pbo").read_bytes() == b"preserve"


def test_cleanup_transaction_fails_closed_if_snapshot_expires_before_use(
        monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    now = {"value": 1000.0}
    monkeypatch.setattr(
        steam_ugc_backend.time,
        "monotonic",
        lambda: now["value"],
    )
    snapshot = _snapshot(
        [10, 20], subscribed=[10], created_monotonic=now["value"],
    )
    transaction = _cleanup_transaction(overlay, snapshot)
    now["value"] += (
        steam_ugc_backend.UGC_DESTRUCTIVE_CLEANUP_SNAPSHOT_MAX_AGE_S + 1.0
    )
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda *_args, **_kwargs: pytest.fail("local deletion must not begin"),
    )
    overlay._run_batch_local_cleanup(
        [20], close_steam=False, cleanup_transaction=transaction,
    )
    assert "authorized cleanup transaction" in (
        overlay._set_mod_operation_status_calls[-1][0]
    )


def test_cleanup_transaction_is_one_shot_and_target_bound(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20, 30], subscribed=[10])
    monkeypatch.setattr(
        mods_ui, "_mod_manager_steam_state", lambda: SteamClientState.OFFLINE,
    )
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **_kwargs: calls.append(mid) or {"ok": True},
    )
    transaction = _cleanup_transaction(overlay, snapshot, ids=(20,))
    overlay._run_batch_local_cleanup(
        [30], close_steam=False, cleanup_transaction=transaction,
    )
    assert calls == []

    transaction = _cleanup_transaction(overlay, snapshot, ids=(20,))
    overlay._run_batch_local_cleanup(
        [20], close_steam=False, cleanup_transaction=transaction,
    )
    overlay._run_batch_local_cleanup(
        [20], close_steam=False, cleanup_transaction=transaction,
    )
    assert calls == [20]
    assert "authorized cleanup transaction" in (
        overlay._set_mod_operation_status_calls[-1][0]
    )


def test_multi_target_cleanup_stops_when_steam_becomes_unsafe(monkeypatch):
    _run_threads_inline(monkeypatch)
    overlay = _local_cleanup_overlay()
    snapshot = _snapshot([10, 20, 30], subscribed=[10])
    states = iter((
        SteamClientState.OFFLINE,
        SteamClientState.OFFLINE,
        SteamClientState.OFFLINE,
        SteamClientState.NATIVE,
    ))
    monkeypatch.setattr(mods_ui, "_mod_manager_steam_state", lambda: next(states))
    calls = []
    monkeypatch.setattr(
        mods_ui,
        "delete_ugc_mod_local_files_after_unsubscribe",
        lambda mid, **_kwargs: calls.append(mid) or {"ok": True},
    )
    transaction = _cleanup_transaction(overlay, snapshot, ids=(20, 30))
    overlay._run_batch_local_cleanup(
        [20, 30], close_steam=False, cleanup_transaction=transaction,
    )
    assert calls == [20]
    assert overlay.refresh_calls[-1]["completion_status"] == "Cleaned up: 1 · Failed: 1"


def test_backend_local_delete_independently_rejects_running_native_steam(monkeypatch):
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (True, SteamClientState.NATIVE),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_steam_roots",
        lambda: pytest.fail("filesystem deletion must not begin"),
    )
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        20,
        steam_absence_verified=True,
        subscription_snapshot=_snapshot([10, 20], subscribed=[10]),
    )
    assert result["ok"] is False
    assert "Steam is stopped" in result["error"]


def test_backend_local_delete_retains_safe_path_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [tmp_path])
    monkeypatch.setattr(
        steam_ugc_backend,
        "_safe_native_workshop_path_for_root",
        lambda *_args, **_kwargs: None,
    )
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        20,
        steam_absence_verified=True,
        subscription_snapshot=_snapshot([10, 20], subscribed=[10]),
    )
    assert result["ok"] is False
    assert "refusing unsafe install folder" in result["error"]


def test_backend_rejects_non_dayz_appid_without_touching_matching_tree(
        monkeypatch, tmp_path):
    wrong_appid = 999999
    mod_id = 20
    content = (
        tmp_path
        / "steamapps/workshop/content"
        / str(wrong_appid)
        / str(mod_id)
    )
    content.mkdir(parents=True)
    payload = content / "synthetic.pbo"
    payload.write_bytes(b"preserve")
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [tmp_path])
    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        mod_id,
        appid=wrong_appid,
        steam_absence_verified=True,
        subscription_snapshot=_snapshot([10, mod_id], subscribed=[10]),
    )
    assert result["ok"] is False
    assert "unsupported app id" in result["error"]
    assert content.is_dir()
    assert payload.read_bytes() == b"preserve"


def test_backend_exact_offline_evidence_deletes_only_guarded_fake_tree(
        monkeypatch, tmp_path):
    mod_id = 20
    content = (
        tmp_path
        / "steamapps/workshop/content"
        / str(steam_ugc_backend.DAYZ_APPID)
        / str(mod_id)
    )
    content.mkdir(parents=True)
    (content / "synthetic.pbo").write_bytes(b"test-only")
    monkeypatch.setattr(
        steam_ugc_backend,
        "_supported_native_steam_mutation_state",
        lambda: (False, SteamClientState.OFFLINE),
    )
    monkeypatch.setattr(steam_ugc_backend, "_native_steam_roots", lambda: [tmp_path])
    monkeypatch.setattr(steam_ugc_backend, "_native_appworkshop_acf_paths", lambda _appid: [])
    monkeypatch.setattr(steam_ugc_backend, "_mark_metadata_deleted", lambda _mid: None)
    monkeypatch.setattr(
        steam_ugc_backend,
        "_delete_file_if_present",
        lambda path, **_kwargs: path.unlink(missing_ok=True) or True,
    )
    from dzll_launcher import workshop_mods
    monkeypatch.setattr(workshop_mods, "remove_dzll_symlinks_for_mod", lambda *_args, **_kwargs: [])

    result = steam_ugc_backend.delete_ugc_mod_local_files_after_unsubscribe(
        mod_id,
        steam_absence_verified=True,
        subscription_snapshot=_snapshot([10, mod_id], subscribed=[10]),
    )

    assert result["ok"] is True
    assert result["deleted_folder"] is True
    assert not content.exists()


def test_badge_state_can_be_native_while_inventory_stays_non_mutating():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.LOADING
    overlay._steam_management_verified = False
    overlay._native_session_handoff_valid = False
    assert overlay._steam_management_is_verified() is False


def test_repair_button_remains_enabled_for_valid_native_inventory():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._authoritative_steam_state = SteamClientState.NATIVE
    overlay._inventory_validity = InventoryValidity.VALID
    overlay._steam_management_verified = True
    overlay._native_session_handoff_valid = True
    button = Widget()
    control = SimpleNamespace(
        _dzll_repair_button=button,
        _dzll_repair_spinner=SimpleNamespace(stop=lambda: None),
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
