from pathlib import Path
import threading

import pytest

from dzll_launcher import mods_ui, steam_ugc_backend, workshop_mods
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.steam_native import SteamClientState


APPID = steam_ugc_backend.DAYZ_APPID
MOD_ID = 424242


class CoercibleWorkshopItemId:
    def __int__(self):
        return MOD_ID


def test_cleanup_workshop_identity_requires_exact_uint64():
    maximum = 0xFFFFFFFFFFFFFFFF
    assert steam_ugc_backend._strict_cleanup_workshop_item_ids([
        1, maximum, 0, -1, maximum + 1, True, "1", 1.0,
        CoercibleWorkshopItemId(),
    ]) == [1, maximum]


def _state(subscribed, *, mod_id=MOD_ID, **extra):
    return {
        int(mod_id): {
            "type": "item",
            "id": int(mod_id),
            "subscribed": subscribed,
            **extra,
        }
    }


def _patch_local_delete_tree(monkeypatch, tmp_path, *, mod_id=MOD_ID):
    content = (
        tmp_path
        / "steamapps/workshop/content"
        / str(APPID)
        / str(mod_id)
    )
    content.mkdir(parents=True)
    (content / "synthetic.pbo").write_bytes(b"preserve unless authorized")
    monkeypatch.setattr(
        steam_ugc_backend, "_native_steam_roots", lambda: [tmp_path],
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_native_appworkshop_acf_paths", lambda _appid: [],
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_mark_metadata_deleted", lambda _mid: None,
    )
    monkeypatch.setattr(
        workshop_mods, "remove_dzll_symlinks_for_mod", lambda *_args, **_kwargs: [],
    )
    return content


def _acf_file(tmp_path, *, mod_id=MOD_ID):
    path = tmp_path / f"steamapps/workshop/appworkshop_{APPID}.acf"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"AppWorkshop"\n'
        "{\n"
        '\t"WorkshopItemsInstalled"\n'
        "\t{\n"
        f'\t\t"{mod_id}"\n'
        "\t\t{\n\t\t\t\"manifest\"\t\"1\"\n\t\t}\n"
        "\t}\n"
        '\t"WorkshopItemDetails"\n'
        "\t{\n"
        f'\t\t"{mod_id}"\n'
        "\t\t{\n\t\t\t\"manifest\"\t\"1\"\n\t\t}\n"
        "\t}\n"
        "}\n",
        encoding="utf-8",
    )
    return path


def _inventory_snapshot(monkeypatch, target_event):
    def run(_command, *, on_event, **_kwargs):
        events = [
            {
                "type": "init",
                "ok": True,
                "appid": APPID,
                "steam_root": "/native/Steam",
                "lib": "/native/Steam/steamrt64/libsteam_api.so",
                "flatpak_environment": False,
            },
            {"type": "item", "id": 10, "subscribed": True},
            dict(target_event),
            {
                "type": "done",
                "ok": True,
                "logged_on": True,
                "steam_id": 76561198000000001,
                "subscription_inventory_complete": True,
                "subscription_count": 1,
                "subscribed_item_ids": [10],
            },
        ]
        for event in events:
            on_event(event)
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", run)
    return steam_ugc_backend.query_ugc_inventory_checked([10, MOD_ID])


@pytest.mark.parametrize(
    "target_event",
    [
        {"type": "item", "id": MOD_ID, "installed": True},
        {"type": "item", "id": MOD_ID, "subscribed": None},
        {"type": "item", "id": MOD_ID, "subscribed": "false"},
        {"type": "item", "id": MOD_ID, "subscribed": 0},
        {"type": "item", "id": MOD_ID, "subscribed": 1},
    ],
)
def test_inventory_rejects_non_boolean_or_missing_subscription_state(
        monkeypatch, target_event):
    snapshot = _inventory_snapshot(monkeypatch, target_event)

    assert snapshot.valid is False
    assert snapshot.proves_unsubscribed(MOD_ID) is False


@pytest.mark.parametrize(
    "malformed_id",
    [MOD_ID + 0.9, str(MOD_ID), True, CoercibleWorkshopItemId()],
)
def test_inventory_rejects_coercible_item_identity(monkeypatch, malformed_id):
    snapshot = _inventory_snapshot(
        monkeypatch,
        {
            "type": "item", "id": malformed_id,
            "subscribed": False, "installed": True,
        },
    )

    assert snapshot.valid is False
    assert snapshot.proves_unsubscribed(MOD_ID) is False


@pytest.mark.parametrize(
    "malformed_id",
    [MOD_ID + 0.9, str(MOD_ID), True, CoercibleWorkshopItemId()],
)
def test_checked_state_query_rejects_coercible_item_identity(
        monkeypatch, malformed_id):
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def helper(_command, **kwargs):
        kwargs["on_event"]({
            "type": "item", "id": malformed_id,
            "subscribed": False, "installed": False,
        })
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    ok, states = steam_ugc_backend.query_ugc_state_checked([MOD_ID])
    assert ok is True
    assert states == {}


def test_malformed_inventory_cannot_reach_modm_local_deletion(monkeypatch):
    snapshot = _inventory_snapshot(
        monkeypatch,
        {"type": "item", "id": MOD_ID, "installed": True},
    )
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
        MOD_ID,
        steam_absence_verified=True,
        subscription_snapshot=snapshot,
    )

    assert result["ok"] is False
    assert "authoritative subscription snapshot" in result["error"]


def test_malformed_inventory_cannot_mint_modm_cleanup_transaction(monkeypatch):
    snapshot = _inventory_snapshot(
        monkeypatch,
        {"type": "item", "id": MOD_ID, "installed": True},
    )
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._steam_management_is_verified = lambda: True
    overlay._local_cleanup_transaction_generation = 0
    overlay._steam_state_generation = 1
    overlay._local_cleanup_authorization_request = None
    overlay._local_cleanup_transaction = None

    request = overlay._begin_local_cleanup_authorization([MOD_ID])
    transaction = overlay._mint_local_cleanup_transaction(
        request,
        [MOD_ID],
        snapshot,
    )

    assert transaction is None
    assert overlay._local_cleanup_transaction is None


@pytest.mark.parametrize(
    "query_result",
    [
        None,
        (False, {}),
        (True, {}),
        (True, {999999: {"id": 999999, "subscribed": False}}),
        (True, {MOD_ID: {"id": MOD_ID, "installed": True}}),
        (True, {MOD_ID: {"id": MOD_ID, "subscribed": None}}),
        (True, {MOD_ID: {"id": MOD_ID, "subscribed": "false"}}),
        (True, {MOD_ID: {"id": MOD_ID, "subscribed": 0}}),
    ],
)
def test_legacy_delete_preserves_content_without_explicit_authority(
        monkeypatch, tmp_path, query_result):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        steam_ugc_backend, "query_ugc_state_checked", lambda *_a, **_k: query_result,
    )

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is False
    assert content.is_dir()
    assert (content / "synthetic.pbo").read_bytes() == b"preserve unless authorized"


def test_legacy_delete_preserves_content_when_query_raises(monkeypatch, tmp_path):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)

    def fail_query(*_args, **_kwargs):
        raise RuntimeError("synthetic query failure")

    monkeypatch.setattr(steam_ugc_backend, "query_ugc_state_checked", fail_query)

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is False
    assert "synthetic query failure" in result["error"]
    assert content.is_dir()


@pytest.mark.parametrize(
    "post_result",
    [
        (False, {}),
        (True, {}),
        (True, {999999: {"id": 999999, "subscribed": False}}),
        (True, {MOD_ID: {"id": MOD_ID, "installed": False}}),
    ],
)
def test_post_unsubscribe_unknown_state_never_authorizes_deletion(
        monkeypatch, tmp_path, post_result):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    responses = iter(((True, _state(True)), post_result))
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: next(responses),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "unsubscribe_ugc_items_checked",
        lambda *_a, **_k: (True, {}),
    )

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is False
    assert result["unsubscribed"] is False
    assert content.is_dir()


def test_failed_unsubscribe_command_never_authorizes_deletion(monkeypatch, tmp_path):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: (True, _state(True)),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "unsubscribe_ugc_items_checked",
        lambda *_a, **_k: (False, {}),
    )

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is False
    assert content.is_dir()


def test_explicit_unsubscribed_state_allows_guarded_legacy_deletion(
        monkeypatch, tmp_path):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: (True, _state(False)),
    )

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is True
    assert result["deleted_folder"] is True
    assert not content.exists()


def test_explicit_post_unsubscribe_state_allows_guarded_legacy_deletion(
        monkeypatch, tmp_path):
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    responses = iter((
        (True, _state(True)),
        (True, _state(False)),
        (True, _state(False)),
        (True, _state(False)),
    ))
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: next(responses),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "unsubscribe_ugc_items_checked",
        lambda *_a, **_k: (True, _state(False)),
    )

    result = steam_ugc_backend.delete_ugc_mod(MOD_ID)

    assert result["ok"] is True
    assert result["unsubscribed"] is True
    assert not content.exists()


@pytest.mark.parametrize("query_result", [None, (False, {}), (True, {})])
def test_acf_cleanup_preserves_records_without_authoritative_state(
        monkeypatch, tmp_path, query_result):
    acf = _acf_file(tmp_path)
    original = acf.read_bytes()
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_appworkshop_acf_paths",
        lambda _appid: [acf],
    )
    monkeypatch.setattr(
        steam_ugc_backend, "query_ugc_state_checked", lambda *_a, **_k: query_result,
    )

    result = steam_ugc_backend.remove_workshop_acf_entries([MOD_ID])

    assert result["ok"] is False
    assert result["removed_ids"] == []
    assert acf.read_bytes() == original
    assert not list(acf.parent.glob(f"{acf.name}.bak*"))


def test_acf_cleanup_query_exception_preserves_record(monkeypatch, tmp_path):
    acf = _acf_file(tmp_path)
    original = acf.read_bytes()
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_appworkshop_acf_paths",
        lambda _appid: [acf],
    )

    def fail_query(*_args, **_kwargs):
        raise RuntimeError("synthetic query failure")

    monkeypatch.setattr(steam_ugc_backend, "query_ugc_state_checked", fail_query)

    result = steam_ugc_backend.remove_workshop_acf_entries([MOD_ID])

    assert result["ok"] is False
    assert acf.read_bytes() == original


def test_aggressive_scrub_protocol_failure_preserves_content_and_acf(
        monkeypatch, tmp_path):
    acf = _acf_file(tmp_path)
    original_acf = acf.read_bytes()
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_appworkshop_acf_paths",
        lambda _appid: [acf],
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: (False, {}),
    )

    result = steam_ugc_backend.scrub_stale_dayz_workshop_acf(
        remove_non_subscribed_installed=True,
    )

    assert result["ok"] is False
    assert result["deleted_ids"] == []
    assert content.is_dir()
    assert acf.read_bytes() == original_acf


def test_aggressive_scrub_missing_target_preserves_content_and_acf(
        monkeypatch, tmp_path):
    acf = _acf_file(tmp_path)
    original_acf = acf.read_bytes()
    content = _patch_local_delete_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(
        steam_ugc_backend,
        "_native_appworkshop_acf_paths",
        lambda _appid: [acf],
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "query_ugc_state_checked",
        lambda *_a, **_k: (True, {}),
    )

    result = steam_ugc_backend.scrub_stale_dayz_workshop_acf(
        remove_non_subscribed_installed=True,
    )

    assert result["ok"] is False
    assert content.is_dir()
    assert acf.read_bytes() == original_acf


@pytest.mark.parametrize(
    ("query_result", "local_state_present", "expected"),
    [
        ((False, {}), True, "steam_issue"),
        ((True, {}), False, "steam_issue"),
        (
            (True, {999999: {"id": 999999, "subscribed": False}}),
            False,
            "steam_issue",
        ),
        ((True, {MOD_ID: {"id": MOD_ID, "installed": True}}), True, "steam_issue"),
        ((True, _state(None)), True, "steam_issue"),
        ((True, _state("false")), True, "steam_issue"),
        ((True, _state("true")), True, "steam_issue"),
        ((True, _state(0)), True, "steam_issue"),
        ((True, _state(1)), True, "steam_issue"),
        ((True, _state([])), True, "steam_issue"),
        ((True, _state({})), True, "steam_issue"),
        ((True, _state(True)), False, "timed_out"),
        ((True, _state(False)), True, "unsubscribed"),
        ((True, _state(False)), False, "removed"),
    ],
)
def test_batch_unsubscribe_requires_explicit_false_subscription_state(
        monkeypatch, tmp_path, query_result, local_state_present, expected):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._settle_unsubscribed_local_state = (
        lambda *_args, **_kwargs: "unsubscribed"
    )
    clock = iter((0.0, 0.0, 46.0))
    monkeypatch.setattr(mods_ui.time, "monotonic", lambda: next(clock, 46.0))
    monkeypatch.setattr(mods_ui.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        mods_ui, "_supported_native_steam_running", lambda: True,
    )
    monkeypatch.setattr(
        mods_ui,
        "request_unsubscribe_ugc_items",
        lambda *_args, **_kwargs: (True, {}),
    )
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_state_checked",
        lambda *_args, **_kwargs: query_result,
    )
    monkeypatch.setattr(
        mods_ui,
        "_has_local_workshop_state",
        lambda _roots, _mid: bool(local_state_present),
    )

    result = overlay._attempt_batch_unsubscribe_one(MOD_ID, [tmp_path])

    assert result == expected


def test_batch_unsubscribe_cleans_only_independently_verified_target(
        monkeypatch, tmp_path):
    verified_id = MOD_ID
    malformed_id = MOD_ID + 1
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._batch_unsubscribe_running = False
    overlay._require_supported_native_steam = lambda: True
    overlay._set_mod_operation_status = lambda *_args, **_kwargs: None
    overlay._update_batch_unsubscribe_stop_button = lambda: None
    overlay._workshop_dir = lambda: str(tmp_path)
    overlay._settle_unsubscribed_local_state = (
        lambda *_args, **_kwargs: "unsubscribed"
    )
    overlay._suppress_start_steam_manage_prompt_for_current_operation = lambda: None
    overlay._set_steam_status_pill = lambda _status: None
    overlay._selected_mod_ids = {verified_id, malformed_id}
    overlay._update_batch_action_buttons = lambda: None
    overlay._show_batch_steam_issue_interruption = lambda: None
    completions = []
    overlay.refresh = lambda **kwargs: completions.append(kwargs)
    symlink_cleanup = []
    overlay._remove_dzll_symlinks_after_unsubscribe = (
        lambda mid: symlink_cleanup.append(int(mid)) or True
    )

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(mods_ui.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(mods_ui.GLib, "idle_add", lambda fn, *args: fn(*args))
    monkeypatch.setattr(
        mods_ui, "_candidate_workshop_roots", lambda **_kwargs: [tmp_path],
    )
    monkeypatch.setattr(
        mods_ui, "_supported_native_steam_running", lambda: True,
    )
    monkeypatch.setattr(
        mods_ui,
        "request_unsubscribe_ugc_items",
        lambda *_args, **_kwargs: (True, {}),
    )

    def query(ids, **_kwargs):
        mid = int(ids[0])
        if mid == verified_id:
            return True, _state(False, mod_id=mid)
        return True, {mid: {"id": mid, "installed": True}}

    monkeypatch.setattr(mods_ui, "query_ugc_state_checked", query)
    monkeypatch.setattr(
        mods_ui, "_has_local_workshop_state", lambda _roots, _mid: True,
    )

    overlay._run_batch_unsubscribe([verified_id, malformed_id], 0)

    assert symlink_cleanup == [verified_id]
    assert overlay._selected_mod_ids == {malformed_id}
    assert completions
    assert "Steam issue" in completions[-1]["completion_status"]


def test_batch_unsubscribe_local_absence_during_steam_exit_is_unconfirmed(
        monkeypatch, tmp_path):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    steam_running = iter((True, False))
    monkeypatch.setattr(
        mods_ui,
        "_supported_native_steam_running",
        lambda: next(steam_running, False),
    )
    monkeypatch.setattr(
        mods_ui,
        "request_unsubscribe_ugc_items",
        lambda *_args, **_kwargs: (True, {}),
    )
    monkeypatch.setattr(
        mods_ui, "_has_local_workshop_state", lambda _roots, _mid: False,
    )

    result = overlay._attempt_batch_unsubscribe_one(MOD_ID, [tmp_path])

    assert result == "steam_closed_unconfirmed"


def test_batch_unsubscribe_failed_query_then_local_absence_is_unconfirmed(
        monkeypatch, tmp_path):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    steam_running = iter((True, True, False))
    monkeypatch.setattr(
        mods_ui,
        "_supported_native_steam_running",
        lambda: next(steam_running, False),
    )
    monkeypatch.setattr(
        mods_ui,
        "request_unsubscribe_ugc_items",
        lambda *_args, **_kwargs: (True, {}),
    )
    monkeypatch.setattr(
        mods_ui,
        "query_ugc_state_checked",
        lambda *_args, **_kwargs: (False, {}),
    )
    monkeypatch.setattr(
        mods_ui, "_has_local_workshop_state", lambda _roots, _mid: False,
    )

    result = overlay._attempt_batch_unsubscribe_one(MOD_ID, [tmp_path])

    assert result == "steam_closed_unconfirmed"


_MISSING_SUBSCRIPTION = object()


def _run_failed_install_with_initial_subscription(
        monkeypatch, subscribed, *, failure="return", helper_attempted=False):
    calls = []
    progress = []
    initial = {
        "type": "item",
        "id": MOD_ID,
        "installed": False,
        "needs_update": False,
        "downloading": False,
        "download_pending": False,
        "state_names": [],
    }
    if subscribed is not _MISSING_SUBSCRIPTION:
        initial["subscribed"] = subscribed

    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "active_ugc_session", lambda: None,
    )

    def helper(command, **kwargs):
        calls.append({
            "command": command,
            "mod_ids": list(kwargs.get("mod_ids") or []),
            "cancel_cleanup_ids": list(kwargs.get("cancel_cleanup_ids") or []),
        })
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            if helper_attempted:
                kwargs["on_event"]({
                    **initial,
                    "type": "request",
                    "subscribe_call_result": 1,
                    "download_requested": False,
                })
            if failure == "raise":
                raise RuntimeError("synthetic install helper failure")
            return False, 1
        if command == "unsubscribe":
            return True, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    result = steam_ugc_backend.run_ugc_install(
        [MOD_ID], progress_cb=progress.append, allow_start_steam=False,
    )
    return result, calls, progress


@pytest.mark.parametrize(
    "subscribed",
    [
        _MISSING_SUBSCRIPTION,
        None,
        "false",
        "true",
        0,
        1,
        [],
        {},
    ],
)
def test_install_malformed_initial_subscription_never_authorizes_cleanup(
        monkeypatch, subscribed):
    result, calls, progress = _run_failed_install_with_initial_subscription(
        monkeypatch, subscribed, helper_attempted=True,
    )

    assert result is False
    assert [call["command"] for call in calls] == [
        "state", "subscribe-download", "state",
    ]
    assert calls[1]["cancel_cleanup_ids"] == []
    assert not any(call["command"] == "unsubscribe" for call in calls)
    sessions = [event for event in progress if event.get("type") == "session"]
    assert sessions
    assert all(event["was_subscribed_before"] is None for event in sessions)
    assert all(event["subscribed_by_dzll_this_join"] is False for event in sessions)


def test_install_helper_exception_with_unknown_initial_state_cannot_unsubscribe(
        monkeypatch):
    result, calls, _progress = _run_failed_install_with_initial_subscription(
        monkeypatch, _MISSING_SUBSCRIPTION, failure="raise",
    )

    assert result is False
    assert [call["command"] for call in calls] == [
        "state", "subscribe-download", "state",
    ]
    assert not any(call["command"] == "unsubscribe" for call in calls)


def test_install_preexisting_subscription_never_authorizes_cleanup(monkeypatch):
    result, calls, progress = _run_failed_install_with_initial_subscription(
        monkeypatch, True, helper_attempted=True,
    )

    assert result is False
    assert calls[1]["cancel_cleanup_ids"] == []
    assert not any(call["command"] == "unsubscribe" for call in calls)
    sessions = [event for event in progress if event.get("type") == "session"]
    assert sessions[0]["was_subscribed_before"] is True


def test_install_explicit_unsubscribed_without_helper_attempt_skips_cleanup(
        monkeypatch):
    result, calls, progress = _run_failed_install_with_initial_subscription(
        monkeypatch, False,
    )

    assert result is False
    assert calls[1]["cancel_cleanup_ids"] == [MOD_ID]
    assert not any(call["command"] == "unsubscribe" for call in calls)
    sessions = [event for event in progress if event.get("type") == "session"]
    assert sessions[0]["was_subscribed_before"] is False
    assert sessions[-1]["subscribed_by_dzll_this_join"] is True
    assert sessions[-1]["helper_subscribe_attempted"] is False


def test_install_explicit_unsubscribed_with_helper_attempt_retains_cleanup(
        monkeypatch):
    result, calls, progress = _run_failed_install_with_initial_subscription(
        monkeypatch, False, helper_attempted=True,
    )

    assert result is False
    assert calls[1]["cancel_cleanup_ids"] == [MOD_ID]
    assert calls[-1] == {
        "command": "unsubscribe",
        "mod_ids": [MOD_ID],
        "cancel_cleanup_ids": [],
    }
    sessions = [event for event in progress if event.get("type") == "session"]
    assert sessions[0]["was_subscribed_before"] is False
    assert sessions[-1]["subscribed_by_dzll_this_join"] is True
    assert sessions[-1]["helper_subscribe_attempted"] is True


@pytest.mark.parametrize("subscribed", [_MISSING_SUBSCRIPTION, None, 0, [], {}])
def test_install_cancel_handoff_excludes_unknown_initial_subscription(
        monkeypatch, subscribed):
    cancel = threading.Event()
    handoffs = []
    calls = []
    initial = {
        "type": "item", "id": MOD_ID, "installed": False,
        "needs_update": False, "downloading": False,
        "download_pending": False, "state_names": [],
    }
    if subscribed is not _MISSING_SUBSCRIPTION:
        initial["subscribed"] = subscribed

    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state", lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        calls.append((command, list(kwargs.get("cancel_cleanup_ids") or [])))
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            cancel.set()
            kwargs["on_event"]({
                "type": "command_result",
                "ok": False,
                "cancelled": True,
                "cancel_handoff": {
                    "parent_allowlisted": [],
                    "helper_subscribe_attempted": [MOD_ID],
                    "cleanup_candidates": [],
                },
            })
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert calls == [("state", []), ("subscribe-download", [])]
    assert handoffs == [{
        "parent_allowlisted": [],
        "helper_subscribe_attempted": [MOD_ID],
        "cleanup_candidates": [],
    }]


@pytest.mark.parametrize(
    ("helper_attempted", "expected_candidates"),
    [(False, []), (True, [MOD_ID])],
)
def test_install_cancel_handoff_requires_both_provenance_factors(
        monkeypatch, helper_attempted, expected_candidates):
    cancel = threading.Event()
    handoffs = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False, "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state", lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            assert kwargs["cancel_cleanup_ids"] == [MOD_ID]
            cancel.set()
            attempted = [MOD_ID] if helper_attempted else []
            kwargs["on_event"]({
                "type": "command_result",
                "ok": False,
                "cancelled": True,
                "cancel_handoff": {
                    "parent_allowlisted": [MOD_ID],
                    "helper_subscribe_attempted": attempted,
                    "cleanup_candidates": attempted,
                },
            })
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == [{
        "parent_allowlisted": [MOD_ID],
        "helper_subscribe_attempted": expected_candidates,
        "cleanup_candidates": expected_candidates,
    }]


def test_install_failed_terminal_cancel_race_exports_two_factor_handoff(
        monkeypatch):
    attempted_id = MOD_ID
    unattempted_id = MOD_ID + 1
    preexisting_id = MOD_ID + 2
    unknown_id = MOD_ID + 3
    wrong_id = MOD_ID + 99
    ids = [attempted_id, unattempted_id, preexisting_id, unknown_id]
    subscribed = {
        attempted_id: False,
        unattempted_id: False,
        preexisting_id: True,
    }
    cancel = threading.Event()
    handoffs = []
    calls = []

    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def item_event(mid, *, event_type="item"):
        event = {
            "type": event_type,
            "id": mid,
            "installed": False,
            "needs_update": False,
            "downloading": False,
            "download_pending": False,
            "state_names": [],
        }
        if mid in subscribed:
            event["subscribed"] = subscribed[mid]
        if event_type == "request":
            event["subscribe_call_result"] = 1
            event["download_requested"] = True
        return event

    def helper(command, **kwargs):
        calls.append(command)
        callback = kwargs.get("on_event")
        if command == "state":
            for mid in ids:
                callback(item_event(mid))
            return True, 0
        if command == "subscribe-download":
            assert kwargs["cancel_cleanup_ids"] == [
                attempted_id, unattempted_id,
            ]
            callback(item_event(attempted_id, event_type="request"))
            callback(item_event(attempted_id, event_type="request"))
            callback(item_event(preexisting_id, event_type="request"))
            callback(item_event(unknown_id, event_type="request"))
            callback(item_event(wrong_id, event_type="request"))
            callback({
                "type": "command_result", "ok": False,
                "reason": "timeout",
            })
            # Reproduce cancellation arriving after the session selected the
            # normal terminal result, but before run_ugc_install branches on it.
            cancel.set()
            return False, 0
        if command == "unsubscribe":
            pytest.fail("cancel cleanup must be deferred until session close")
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    assert steam_ugc_backend.run_ugc_install(
        ids, cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert calls == ["state", "subscribe-download"]
    assert handoffs == [{
        "parent_allowlisted": [attempted_id, unattempted_id],
        "helper_subscribe_attempted": [
            attempted_id, preexisting_id, unknown_id,
        ],
        "cleanup_candidates": [attempted_id],
    }]


def test_install_failed_terminal_cancel_race_without_attempt_exports_nothing(
        monkeypatch):
    cancel = threading.Event()
    handoffs = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False,
        "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            kwargs["on_event"]({"type": "command_result", "ok": False})
            cancel.set()
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == []


def test_install_success_racing_cancel_does_not_export_fallback_cleanup(
        monkeypatch):
    cancel = threading.Event()
    handoffs = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False,
        "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
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
                "type": "done", "ok": True,
                "ready": [MOD_ID], "installed": [MOD_ID],
            })
            cancel.set()
            return True, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == []


@pytest.mark.parametrize(
    "malformed_id",
    [
        pytest.param(MOD_ID + 0.9, id="float"),
        pytest.param(str(MOD_ID), id="numeric-string"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(CoercibleWorkshopItemId(), id="coercible-object"),
    ],
)
def test_install_terminal_cancel_fallback_rejects_coercible_attempt_ids(
        monkeypatch, malformed_id):
    cancel = threading.Event()
    handoffs = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False,
        "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        callback = kwargs.get("on_event")
        if command == "state":
            callback(dict(initial))
            return True, 0
        if command == "subscribe-download":
            callback({
                **initial, "type": "request", "id": malformed_id,
                "subscribe_call_result": 1,
                "download_requested": True,
            })
            callback({"type": "command_result", "ok": False})
            cancel.set()
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == []


@pytest.mark.parametrize(
    "malformed_id",
    [
        pytest.param(MOD_ID + 0.9, id="float"),
        pytest.param(str(MOD_ID), id="numeric-string"),
        pytest.param(True, id="bool"),
        pytest.param(CoercibleWorkshopItemId(), id="coercible-object"),
    ],
)
def test_install_genuine_cancel_handoff_rejects_coercible_attempt_ids(
        monkeypatch, malformed_id):
    cancel = threading.Event()
    handoffs = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False,
        "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            cancel.set()
            kwargs["on_event"]({
                "type": "command_result", "ok": False,
                "cancelled": True,
                "cancel_handoff": {
                    "parent_allowlisted": [MOD_ID],
                    "helper_subscribe_attempted": [malformed_id],
                    "cleanup_candidates": [malformed_id],
                },
            })
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    assert steam_ugc_backend.run_ugc_install(
        [MOD_ID], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == [{
        "parent_allowlisted": [MOD_ID],
        "helper_subscribe_attempted": [],
        "cleanup_candidates": [],
    }]


def test_install_terminal_cancel_fallback_keeps_only_exact_integer_identity(
        monkeypatch):
    exact_id = MOD_ID
    float_target = MOD_ID + 1
    string_target = MOD_ID + 2
    bool_target = 1
    ids = [exact_id, float_target, string_target, bool_target]
    cancel = threading.Event()
    handoffs = []
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def state(mid, *, event_type="item"):
        event = {
            "type": event_type, "id": mid, "subscribed": False,
            "installed": False, "needs_update": False,
            "downloading": False, "download_pending": False,
            "state_names": [],
        }
        if event_type == "request":
            event.update(
                subscribe_call_result=1, download_requested=True,
            )
        return event

    def helper(command, **kwargs):
        callback = kwargs.get("on_event")
        if command == "state":
            for mid in ids:
                callback(state(mid))
            return True, 0
        if command == "subscribe-download":
            for raw in (
                    exact_id, float(float_target), str(string_target), True,
                    CoercibleWorkshopItemId()):
                callback(state(raw, event_type="request"))
            callback({"type": "command_result", "ok": False})
            cancel.set()
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    assert steam_ugc_backend.run_ugc_install(
        ids, cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert handoffs == [{
        "parent_allowlisted": sorted(ids),
        "helper_subscribe_attempted": [exact_id],
        "cleanup_candidates": [exact_id],
    }]


def test_fresh_cancel_cleanup_rejects_coercible_candidate_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        steam_ugc_backend, "_supported_native_steam_mutation_state",
        lambda: (True, None),
    )

    def helper(command, **kwargs):
        calls.append((command, list(kwargs.get("mod_ids") or [])))
        kwargs["on_event"]({
            "type": "done", "candidates": kwargs["mod_ids"],
            "attempted": [], "confirmed_unsubscribed": [],
            "retained_installed": kwargs["mod_ids"],
            "already_unsubscribed": [], "failed": [], "timed_out": [],
        })
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    result = steam_ugc_backend.cleanup_cancelled_ugc_subscriptions([
        MOD_ID, MOD_ID + 0.9, str(MOD_ID), True,
        CoercibleWorkshopItemId(),
    ])
    assert calls == [("cancel-cleanup-unsubscribe", [MOD_ID])]
    assert result["candidates"] == [MOD_ID]


def test_install_failure_cleanup_intersects_exact_per_item_attempts(monkeypatch):
    attempted_id = MOD_ID
    unattempted_id = MOD_ID + 1
    preexisting_id = MOD_ID + 2
    wrong_id = MOD_ID + 99
    ids = [attempted_id, unattempted_id, preexisting_id]
    initial = {
        attempted_id: {"subscribed": False},
        unattempted_id: {"subscribed": False},
        preexisting_id: {"subscribed": True},
    }
    calls = []

    def item_event(mid, *, event_type="item", subscribe_call_result=None):
        event = {
            "type": event_type,
            "id": mid,
            "subscribed": initial.get(mid, {"subscribed": False})["subscribed"],
            "installed": False,
            "needs_update": False,
            "downloading": False,
            "download_pending": False,
            "state_names": [],
        }
        if event_type == "request":
            event["subscribe_call_result"] = subscribe_call_result
            event["download_requested"] = False
        return event

    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state", lambda *_args, **_kwargs: None,
    )

    def helper(command, **kwargs):
        calls.append((command, list(kwargs.get("mod_ids") or [])))
        callback = kwargs.get("on_event")
        if command == "state":
            for mid in ids:
                callback(item_event(mid))
            return True, 0
        if command == "subscribe-download":
            assert kwargs["cancel_cleanup_ids"] == [
                attempted_id, unattempted_id,
            ]
            callback(item_event(
                attempted_id, event_type="request", subscribe_call_result=1,
            ))
            callback(item_event(
                attempted_id, event_type="request", subscribe_call_result=1,
            ))
            callback(item_event(
                preexisting_id, event_type="request", subscribe_call_result=1,
            ))
            callback(item_event(
                wrong_id, event_type="request", subscribe_call_result=1,
            ))
            return False, 1
        if command == "unsubscribe":
            return True, 0
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    assert steam_ugc_backend.run_ugc_install(ids) is False
    unsubscribe_calls = [call for call in calls if call[0] == "unsubscribe"]
    assert unsubscribe_calls == [("unsubscribe", [attempted_id])]


def test_install_reap_failure_without_subscribe_attempt_creates_no_cleanup_owner(
        monkeypatch):
    calls = []
    initial = {
        "type": "item", "id": MOD_ID, "subscribed": False,
        "installed": False, "needs_update": False,
        "downloading": False, "download_pending": False, "state_names": [],
    }
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "active_ugc_session", lambda: None,
    )

    def helper(command, **kwargs):
        calls.append(command)
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, 0
        if command == "subscribe-download":
            raise steam_ugc_backend.UGCHelperReapError(
                "synthetic unresolved helper",
                helper_process_may_be_alive=True,
            )
        if command == "unsubscribe":
            pytest.fail("reap failure without helper attempt cannot unsubscribe")
        raise AssertionError(command)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)

    assert steam_ugc_backend.run_ugc_install([MOD_ID]) is False
    assert calls == ["state", "subscribe-download", "state"]
