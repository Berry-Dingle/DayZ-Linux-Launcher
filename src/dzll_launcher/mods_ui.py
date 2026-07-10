# mods_ui.py
import os
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from .ui_row import attach_pointer_cursor
from gi.repository import Gtk, GLib, Pango

from .config import CACHE_DIR
from .settings import autodetect_workshop_dir, save_settings
from .steam_ugc_backend import delete_ugc_mod_local_files_after_unsubscribe
from .steam_ugc_backend import query_ugc_state_checked
from .steam_ugc_backend import request_unsubscribe_ugc_items
from .steam_native import dayz_steam_library, dayz_workshop_content_dir, is_native_steam_running, native_steam_libraries, resolve_native_steam_cmd
from .steamcmd_mods import remove_dzll_symlinks_for_mod
from .mod_metadata import clean_display_mod_name, load_mod_metadata
from .mod_name_resolver import resolve_best_mod_names

APPID = "221100"
MOD_WORKSHOP_COLUMN_CHARS = 18
MOD_ID_COLUMN_CHARS = 11
MOD_ID_COLUMN_WIDTH = 132
MOD_SIZE_COLUMN_CHARS = 9
MOD_LAST_USED_COLUMN_CHARS = 12
MOD_SELECT_COLUMN_WIDTH = 42
MOD_MANAGER_CARD_WIDTH = 960
BATCH_UNSUBSCRIBE_ITEM_TIMEOUT_S = 45
BATCH_UNSUBSCRIBE_VERIFY_TIMEOUT_S = 12
BATCH_UNSUBSCRIBE_REQUEST_TIMEOUT_S = 12
BATCH_UNSUBSCRIBE_POLL_INTERVAL_S = 1.0
BATCH_UNSUBSCRIBE_POLL_QUERY_TIMEOUT_S = 4
BATCH_UNSUBSCRIBE_SETTLE_TIMEOUT_S = 4.0
PASSIVE_STEAM_WATCH_INTERVAL_S = 4
PASSIVE_STEAM_WATCH_READY_TIMEOUT_S = 90
PASSIVE_STEAM_WATCH_SAMPLE_SIZE = 3
PENDING_MOD_DELETES_PATH = os.path.join(CACHE_DIR, "pending_mod_deletes.json")
LEGACY_PENDING_DELETE_STATUS = "Old cleanup state cleared · Use Clean Up Local Files if needed"


def _wait_for_native_steam_stopped(timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if not is_native_steam_running():
            return True
        time.sleep(0.5)
    return not is_native_steam_running()


def _wait_for_native_steam_running(timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if is_native_steam_running():
            return True
        time.sleep(0.5)
    return is_native_steam_running()


def _launch_native_steam_silent() -> tuple[bool, str]:
    steam_cmd = resolve_native_steam_cmd()
    if not steam_cmd:
        return False, "Native Steam executable was not found."
    try:
        subprocess.Popen(
            [steam_cmd, "-silent"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _migrate_legacy_pending_delete_file() -> tuple[bool, str]:
    path = Path(PENDING_MOD_DELETES_PATH)
    try:
        if not path.is_file():
            return False, ""
    except Exception:
        return False, ""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(100):
        suffix = f".{index}" if index else ""
        backup = path.with_name(f"pending_mod_deletes.legacy-{stamp}{suffix}.json")
        try:
            if backup.exists():
                continue
            os.replace(str(path), str(backup))
            return True, str(backup)
        except FileNotFoundError:
            return False, ""
        except Exception as exc:
            try:
                print(f"[MOD MANAGER] Could not rename legacy pending delete state: {exc}", flush=True)
            except Exception:
                pass
            return True, ""

    return True, ""


def _format_mod_size(byte_count) -> str:
    try:
        n = int(byte_count or 0)
    except Exception:
        n = 0
    if n <= 0:
        return "—"
    kb = 1024
    mb = 1024 * 1024
    gb = 1024 * 1024 * 1024
    if n < mb:
        return f"{max(1, round(n / kb))} KB"
    if n >= gb:
        return f"{n / gb:.1f} GB"
    return f"{round(n / mb)} MB"


def _format_last_used(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Never"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).date()
        days = (today - dt.astimezone(timezone.utc).date()).days
    except Exception:
        return "Never"
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


def _last_used_sort_value(value) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _paths(workshop_dir: str = "", proton_prefix: str = ""):
    home = Path.home()
    steamapps = home / ".local/share/Steam/steamapps"
    workshop = Path(workshop_dir).expanduser() if workshop_dir else steamapps / "workshop"
    pfx = Path(proton_prefix).expanduser() if proton_prefix else steamapps / f"compatdata/{APPID}/pfx"
    pfx_user = pfx / "drive_c/users/steamuser"
    return {
        "steamapps": steamapps,
        "workshop": workshop,
        "acf": workshop / f"appworkshop_{APPID}.acf",
        "content_app": workshop / f"content/{APPID}",
        "downloads": workshop / "downloads",
        "temp": workshop / "temp",
        "watch_1": pfx_user / "DZLLMods",
        "watch_2": pfx_user / "Documents/Templates",
        "launcher_state": pfx_user / "AppData/Local/DayZ Launcher",
        # Hard safety guard targets (MUST NOT TOUCH)
        "game_manifest": steamapps / f"appmanifest_{APPID}.acf",
        "game_dir": steamapps / "common/DayZ",
    }


def _read_installed_mod_ids(workshop_dir: str = "") -> list[int]:
    p = _paths(workshop_dir=workshop_dir)["acf"]
    if not p.is_file():
        return []
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    key = '"WorkshopItemsInstalled"'
    i = txt.find(key)
    if i < 0:
        return []
    ob = txt.find("{", i)
    if ob < 0:
        return []

    depth = 0
    end = -1
    for j in range(ob, len(txt)):
        c = txt[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end < 0:
        return []

    block = txt[ob:end + 1]
    out: list[int] = []
    seen = set()
    for line in block.splitlines():
        s = line.strip()
        if len(s) >= 3 and s[0] == '"' and s[-1] == '"' and s[1:-1].isdigit():
            mid = int(s[1:-1])
            if mid > 0 and mid not in seen:
                out.append(mid)
                seen.add(mid)
    return out


def _read_content_mod_ids(workshop_dir: str = "") -> list[int]:
    p = _paths(workshop_dir=workshop_dir)["content_app"]
    if not p.is_dir():
        return []
    out: list[int] = []
    try:
        for child in p.iterdir():
            try:
                if child.name.isdigit() and child.is_dir() and not child.is_symlink():
                    mid = int(child.name)
                    if mid > 0:
                        out.append(mid)
            except Exception:
                continue
    except Exception:
        return []
    out.sort()
    return out


def _normalize_workshop_root(path) -> Path | None:
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return None
    low = str(resolved).lower()
    if "flatpak" in low or "com.valvesoftware.steam" in low:
        return None
    if resolved.name == str(APPID) and resolved.parent.name == "content" and resolved.parent.parent.name == "workshop":
        return resolved.parent.parent
    if resolved.name == "content" and resolved.parent.name == "workshop":
        return resolved.parent
    if resolved.name == "workshop":
        return resolved
    return None


def _candidate_workshop_roots(workshop_dir: str = "") -> list[Path]:
    candidates: list[Path] = []
    try:
        library = dayz_steam_library()
        if library is not None:
            candidates.append(Path(library) / "steamapps/workshop")
    except Exception:
        pass
    try:
        content_dir = dayz_workshop_content_dir()
        if content_dir is not None:
            candidates.append(Path(content_dir).parent.parent.parent)
    except Exception:
        pass
    try:
        for library in native_steam_libraries():
            candidates.append(Path(library) / "steamapps/workshop")
    except Exception:
        pass
    if workshop_dir:
        candidates.append(Path(workshop_dir).expanduser())
    else:
        try:
            detected = autodetect_workshop_dir() or ""
            if detected:
                candidates.append(Path(detected).expanduser())
        except Exception:
            pass

    out: list[Path] = []
    seen = set()
    for path in candidates:
        resolved = _normalize_workshop_root(path)
        if resolved is None:
            continue
        key = str(resolved)
        if key not in seen:
            out.append(resolved)
            seen.add(key)
    return out


def _read_installed_mod_ids_from_roots(workshop_roots: list[Path]) -> list[int]:
    out: list[int] = []
    seen = set()
    for root in workshop_roots or []:
        for mid in _read_installed_mod_ids(workshop_dir=str(root)):
            if mid not in seen:
                out.append(mid)
                seen.add(mid)
        for mid in _read_content_mod_ids(workshop_dir=str(root)):
            if mid not in seen:
                out.append(mid)
                seen.add(mid)
    return out


def _read_acf_mod_ids_from_roots(workshop_roots: list[Path]) -> list[int]:
    out: list[int] = []
    seen = set()
    for root in workshop_roots or []:
        for mid in _read_installed_mod_ids(workshop_dir=str(root)):
            if mid not in seen:
                out.append(mid)
                seen.add(mid)
    return out


def _content_folder_for_mod(workshop_roots: list[Path], mod_id: int) -> Path | None:
    for root in workshop_roots or []:
        path = Path(root) / "content" / str(APPID) / str(int(mod_id))
        try:
            if path.is_dir() and not path.is_symlink():
                return path
        except Exception:
            continue
    return None


def _has_local_workshop_state(workshop_roots: list[Path], mod_id: int) -> bool:
    try:
        mid = int(mod_id)
    except Exception:
        return False
    if mid <= 0:
        return False
    if _content_folder_for_mod(workshop_roots, mid) is not None:
        return True
    try:
        return mid in set(_read_acf_mod_ids_from_roots(workshop_roots))
    except Exception:
        return False


def _name_map_from_symlinks(proton_prefix: str = "") -> dict[int, str]:
    watch = _paths(proton_prefix=proton_prefix)["watch_1"]
    mp: dict[int, str] = {}
    if not watch.is_dir():
        return mp
    try:
        for nm in os.listdir(str(watch)):
            lp = watch / nm
            if not lp.is_symlink():
                continue
            try:
                tgt = Path(os.path.realpath(str(lp)))
            except Exception:
                continue
            if tgt.name.isdigit():
                mid = int(tgt.name)
                if mid > 0:
                    mp[mid] = nm
    except Exception:
        pass
    return mp


class ModsManagerOverlay:
    def __init__(self, host, overlay: Gtk.Overlay):
        self.host = host
        self.overlay = overlay

        self.scrim = Gtk.Box()
        self.scrim.set_hexpand(True)
        self.scrim.set_vexpand(True)
        self.scrim.set_visible(False)
        self.scrim.set_can_target(True)
        self.scrim.add_css_class("settings-scrim")

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card.set_halign(Gtk.Align.CENTER)
        self.card.set_valign(Gtk.Align.CENTER)
        self.card.set_visible(False)
        self.card.set_can_target(True)

        # Keep existing card layout/padding styling
        self.card.add_css_class("steamcmd-auth-card")
        # Add mods-specific styling hook (bg/border/radius)
        self.card.add_css_class("mods-card")

        self.card.set_margin_start(40)
        self.card.set_margin_end(40)
        self.card.set_margin_top(40)
        self.card.set_margin_bottom(40)
        self.card.set_size_request(MOD_MANAGER_CARD_WIDTH, -1)

        overlay.add_overlay(self.scrim)
        overlay.add_overlay(self.card)

        # ----------------------------
        # Confirm overlay (reuse warning-card styling)
        # ----------------------------
        self.confirm_scrim = Gtk.Box()
        self.confirm_scrim.set_hexpand(True)
        self.confirm_scrim.set_vexpand(True)
        self.confirm_scrim.set_visible(False)
        self.confirm_scrim.set_can_target(True)
        self.confirm_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.confirm_scrim)

        self.confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.confirm_box.set_halign(Gtk.Align.CENTER)
        self.confirm_box.set_valign(Gtk.Align.CENTER)
        self.confirm_box.set_visible(False)
        self.confirm_box.set_can_target(True)
        self.confirm_box.add_css_class("warning-card")
        overlay.add_overlay(self.confirm_box)

        # Title + body
        self.confirm_title = Gtk.Label(label="")
        self.confirm_title.set_xalign(0.0)
        self.confirm_title.set_wrap(True)
        self.confirm_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.confirm_title.add_css_class("steamcmd-heading")
        self.confirm_box.append(self.confirm_title)

        self.confirm_text = Gtk.Label(label="")
        self.confirm_text.set_xalign(0.0)
        self.confirm_text.set_wrap(True)
        self.confirm_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.confirm_text.set_max_width_chars(76)
        self.confirm_box.append(self.confirm_text)

        self.confirm_check = Gtk.CheckButton(label="")
        self.confirm_check.set_visible(False)
        self.confirm_check.set_halign(Gtk.Align.START)
        self.confirm_box.append(self.confirm_check)

        # Buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        btn_row.set_halign(Gtk.Align.CENTER)

        self.confirm_cancel_btn = Gtk.Button(label="Cancel")
        self.confirm_cancel_btn.add_css_class("warning-btn")
        attach_pointer_cursor(self.confirm_cancel_btn)
        btn_row.append(self.confirm_cancel_btn)

        self.confirm_ok_btn = Gtk.Button(label="OK")
        self.confirm_ok_btn.add_css_class("suggested-action")
        self.confirm_ok_btn.add_css_class("warning-btn")
        attach_pointer_cursor(self.confirm_ok_btn)
        btn_row.append(self.confirm_ok_btn)

        self.confirm_box.append(btn_row)

        # Click scrim to cancel
        confirm_scrim_click = Gtk.GestureClick.new()
        confirm_scrim_click.set_button(0)
        confirm_scrim_click.connect("pressed", lambda *_: self._confirm_hide_and_fire(False))
        self.confirm_scrim.add_controller(confirm_scrim_click)

        # Hook up buttons
        self._confirm_cb = None
        self.confirm_ok_btn.connect("clicked", lambda *_: self._confirm_hide_and_fire(True))
        self.confirm_cancel_btn.connect("clicked", lambda *_: self._confirm_hide_and_fire(False))

        self._rows_cache = []
        self._loaded_items = []
        self._selected_mod_ids = set()
        self._mod_operation_running = False
        self._legacy_pending_delete_migrated, _ = _migrate_legacy_pending_delete_file()
        self._batch_unsubscribe_running = False
        self._batch_unsubscribe_stop_requested = False
        self._batch_unsubscribe_queue_count = 0
        self._mod_operation_pending = False
        self._steam_management_verified = False
        self._last_mod_state_query_ok = False
        self._host_close_request_handler_id = 0
        self._passive_steam_watch_timer_id = 0
        self._passive_steam_watch_probe_running = False
        self._passive_steam_watch_check_started_at = 0.0
        self._passive_steam_watch_checking_status_shown = False
        self._passive_steam_was_ready_this_session = False
        self._passive_steam_shutdown_status_shown = False
        self._suppress_start_steam_manage_prompt_for_operation = False
        self._steam_status_pill_state = "checking"
        self.sort_key = "name"
        self.sort_ascending = True
        self._sort_header_labels = {}
        self._build()
        self._connect_host_close_request()
        GLib.idle_add(self._show_legacy_pending_delete_migration_status)

    def _confirm_hide_and_fire(self, ok: bool):
        try:
            self.confirm_box.set_visible(False)
            self.confirm_scrim.set_visible(False)
        except Exception:
            pass

        cb = getattr(self, "_confirm_cb", None)
        self._confirm_cb = None
        if callable(cb):
            try:
                cb(ok)
            except Exception:
                pass
        return False

    def _confirm_show(
        self,
        title: str,
        body: str,
        ok_label: str,
        cb,
        show_cancel: bool = True,
        cancel_label: str = "Cancel",
        checkbox_label: str = "",
        checkbox_active: bool = False,
    ):
        # store cb and set labels
        self._confirm_cb = cb
        try:
            self.confirm_title.set_text(title or "")
        except Exception:
            pass
        try:
            self.confirm_text.set_text(body or "")
        except Exception:
            pass
        try:
            self.confirm_ok_btn.set_label(ok_label or "OK")
        except Exception:
            pass
        try:
            self.confirm_check.set_label(str(checkbox_label or ""))
            self.confirm_check.set_active(bool(checkbox_active))
            self.confirm_check.set_visible(bool(checkbox_label))
        except Exception:
            pass
        try:
            self.confirm_cancel_btn.set_label(cancel_label or "Cancel")
            self.confirm_cancel_btn.set_visible(bool(show_cancel))
        except Exception:
            pass

        # show
        try:
            self.confirm_scrim.set_visible(True)
            self.confirm_box.set_visible(True)
        except Exception:
            pass
        return False

    def _connect_host_close_request(self):
        try:
            win = getattr(self.host, "_win", None)
            if win is not None and not int(getattr(self, "_host_close_request_handler_id", 0) or 0):
                self._host_close_request_handler_id = int(win.connect("close-request", self._on_host_close_request) or 0)
        except Exception:
            self._host_close_request_handler_id = 0
        return False

    def _on_host_close_request(self, *_):
        self._stop_passive_steam_watch()
        return False

    def _show_legacy_pending_delete_migration_status(self):
        if bool(getattr(self, "_legacy_pending_delete_migrated", False)):
            self._set_mod_operation_status(LEGACY_PENDING_DELETE_STATUS, running=False)
        return False

    def _is_one_shot_action_status(self, text: str) -> bool:
        status = str(text or "").strip().casefold()
        if not status:
            return False
        prefixes = (
            "unsubscribed ",
            "unsubscribed:",
            "stopped. unsubscribed:",
            "stopped ·",
            "stopped after current.",
            "unsubscribe timed out.",
            "cleaned up ",
            "cleaned up:",
            "cleanup failed.",
            "steam closed ·",
            "steam issue ·",
            "selected mods are no longer safe to clean up.",
            "dzll could not check",
            "no local files selected for cleanup.",
            "only local only mods can be cleaned up.",
            "no workshop mods to unsubscribe.",
            "no subscribed workshop mods found.",
        )
        return any(status.startswith(prefix) for prefix in prefixes)

    def _is_transient_action_status(self, text: str) -> bool:
        status = str(text or "").strip().casefold()
        transient_statuses = {
            "cleanup cancelled.",
            "unsubscribe cancelled.",
            "cancelled.",
            "checking with steam...",
        }
        return status in transient_statuses

    def _is_clearable_action_status(self, text: str) -> bool:
        status = str(text or "").strip().casefold()
        return self._is_one_shot_action_status(status) or self._is_transient_action_status(status)

    def _clear_one_shot_action_status_if_idle(self) -> None:
        try:
            current_status = str(self.operation_status_label.get_text() or "")
        except Exception:
            current_status = ""
        if (
            not bool(getattr(self, "_batch_unsubscribe_running", False))
            and not bool(getattr(self, "_mod_operation_running", False))
            and not bool(getattr(self, "_mod_operation_pending", False))
            and self._is_clearable_action_status(current_status)
        ):
            self._set_mod_operation_status("", running=False)

    def _clear_transient_action_status_if_idle(self) -> None:
        try:
            current_status = str(self.operation_status_label.get_text() or "").strip().casefold()
        except Exception:
            current_status = ""
        if (
            self._is_transient_action_status(current_status)
            and not bool(getattr(self, "_batch_unsubscribe_running", False))
            and not bool(getattr(self, "_mod_operation_running", False))
            and not bool(getattr(self, "_mod_operation_pending", False))
        ):
            self._set_mod_operation_status("", running=False)

    def show(self):
        self.scrim.set_visible(True)
        self.card.set_visible(True)
        self._clear_one_shot_action_status_if_idle()
        try:
            self.search.grab_focus()
        except Exception:
            pass
        self._maybe_start_passive_steam_watch()

    def hide(self):
        self._hide_now()
        return False

    def _hide_now(self):
        self._stop_passive_steam_watch()
        self.scrim.set_visible(False)
        self.card.set_visible(False)
        return False

    def _mod_manager_is_visible(self) -> bool:
        try:
            return bool(self.card.get_visible())
        except Exception:
            return False

    def _app_session_owner(self):
        return getattr(self.host, "_win", None) or self.host

    def _settings_owner(self):
        return getattr(self.host, "_win", None) or self.host

    def _settings_dict(self) -> dict:
        owner = self._settings_owner()
        settings = getattr(owner, "settings", None)
        return settings if isinstance(settings, dict) else {}

    def _restart_steam_after_local_cleanup_pref(self) -> bool:
        return bool(self._settings_dict().get("restart_steam_after_local_cleanup", False))

    def _save_restart_steam_after_local_cleanup_pref(self, enabled: bool) -> None:
        settings = self._settings_dict()
        if not isinstance(settings, dict):
            return
        settings["restart_steam_after_local_cleanup"] = bool(enabled)
        try:
            save_settings(settings)
        except Exception as exc:
            try:
                print(f"[MOD MANAGER] Could not save cleanup restart preference: {exc}", flush=True)
            except Exception:
                pass

    def _start_steam_manage_reminder_was_shown(self) -> bool:
        try:
            return bool(getattr(self._app_session_owner(), "_dzll_mods_manage_steam_reminder_shown", False))
        except Exception:
            return True

    def _mark_start_steam_manage_reminder_shown(self) -> None:
        try:
            setattr(self._app_session_owner(), "_dzll_mods_manage_steam_reminder_shown", True)
        except Exception:
            pass

    def _suppress_start_steam_manage_prompt_for_current_operation(self) -> None:
        self._suppress_start_steam_manage_prompt_for_operation = True

    def _clear_operation_start_steam_prompt_suppression_if_ready(self) -> None:
        if self._steam_management_is_verified():
            self._suppress_start_steam_manage_prompt_for_operation = False

    def _maybe_show_start_steam_manage_reminder(self) -> None:
        if not self._mod_manager_is_visible():
            return
        if self._steam_management_is_verified():
            return
        if bool(getattr(self, "_suppress_start_steam_manage_prompt_for_operation", False)):
            return
        if not self._loaded_mod_ids():
            return
        if bool(getattr(self, "_mod_operation_pending", False)):
            return
        if self._start_steam_manage_reminder_was_shown():
            return
        self._show_start_steam_manage_prompt()

    def _show_batch_steam_offline_interruption(self) -> None:
        self._confirm_show(
            "Steam went offline",
            "Steam closed or became unavailable while DZLL was unsubscribing mods. "
            "DZLL stopped the task safely. Some mods may not have been processed. "
            "Please check Steam is online and try again.",
            "Close",
            lambda _ok: None,
            show_cancel=False,
        )

    def _show_batch_steam_issue_interruption(self) -> None:
        self._confirm_show(
            "Steam issue",
            "DZLL could not check mods with Steam while unsubscribing. "
            "DZLL stopped the task safely. Some mods may not have been processed. "
            "Please check Steam is working and try again.",
            "Close",
            lambda _ok: None,
            show_cancel=False,
        )

    def _has_steam_offline_rows(self) -> bool:
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            if str(state.get("category") or "") == "steam_offline":
                return True
        return False

    def _has_steam_checked_rows(self) -> bool:
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            if bool(state.get("requires_verification", False)):
                continue
            if str(state.get("category") or "") in ("workshop", "local_only"):
                return True
        return False

    def _steam_status_from_loaded_items(self) -> str:
        items = list(getattr(self, "_loaded_items", []) or [])
        if not items:
            try:
                return "online" if is_native_steam_running() else "offline"
            except Exception:
                return "offline"
        has_issue = False
        for item in items:
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            category = str(state.get("category") or "")
            if category == "steam_offline":
                return "offline"
            if bool(state.get("requires_verification", False)):
                has_issue = True
                continue
            if category in ("workshop", "local_only"):
                return "online"
        if has_issue:
            return "issue"
        try:
            return "offline" if not is_native_steam_running() else "issue"
        except Exception:
            return "offline"

    def _compute_steam_management_verified(self) -> bool:
        try:
            if not bool(is_native_steam_running()):
                return False
        except Exception:
            return False
        if not bool(getattr(self, "_last_mod_state_query_ok", False)):
            return False

        items = list(getattr(self, "_loaded_items", []) or [])
        if not items:
            return True
        return self._has_steam_checked_rows()

    def _steam_management_is_verified(self) -> bool:
        return bool(getattr(self, "_steam_management_verified", False))

    def _set_steam_status_pill(self, status: str) -> None:
        status_key = str(status or "issue").strip().lower()
        if status_key not in ("online", "offline", "checking", "issue"):
            status_key = "issue"

        label_by_status = {
            "online": "Steam Online",
            "offline": "Steam Offline",
            "checking": "Checking Steam",
            "issue": "Steam Issue",
        }
        tooltip_by_status = {
            "online": "DZLL can check Workshop subscriptions.",
            "offline": "Start Steam to check Workshop mods.",
            "checking": "Waiting for Steam to be ready.",
            "issue": "DZLL could not check mods with Steam.",
        }

        self._steam_status_pill_state = status_key
        try:
            self.steam_status_text.set_text(label_by_status[status_key])
        except Exception:
            pass
        try:
            self.steam_status_pill.set_tooltip_text(tooltip_by_status[status_key])
        except Exception:
            pass
        try:
            for css_class in ("steam-status-online", "steam-status-offline", "steam-status-checking", "steam-status-issue"):
                self.steam_status_dot.remove_css_class(css_class)
            self.steam_status_dot.add_css_class(f"steam-status-{status_key}")
        except Exception:
            pass

    def _passive_steam_watch_sample_ids(self) -> list[int]:
        items = list(getattr(self, "_loaded_items", []) or [])
        sample = []
        seen = set()

        def add_item_id(item) -> None:
            if len(sample) >= PASSIVE_STEAM_WATCH_SAMPLE_SIZE:
                return
            try:
                mid = int(item[1])
            except Exception:
                return
            if mid <= 0 or mid in seen:
                return
            seen.add(mid)
            sample.append(mid)

        for item in items:
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            if str(state.get("category") or "") == "steam_offline":
                add_item_id(item)
        for item in items:
            add_item_id(item)
        return sample

    def _maybe_start_passive_steam_watch(self) -> None:
        should_watch = (
            self._mod_manager_is_visible()
            and (
                self._has_steam_offline_rows()
                or bool(getattr(self, "_passive_steam_was_ready_this_session", False))
            )
        )
        if not should_watch:
            self._stop_passive_steam_watch()
            return
        if int(getattr(self, "_passive_steam_watch_timer_id", 0) or 0):
            return
        self._passive_steam_watch_probe_running = False
        self._passive_steam_watch_check_started_at = 0.0
        self._passive_steam_watch_checking_status_shown = False
        self._passive_steam_shutdown_status_shown = False
        try:
            self._passive_steam_watch_timer_id = int(
                GLib.timeout_add_seconds(
                    PASSIVE_STEAM_WATCH_INTERVAL_S,
                    self._passive_steam_watch_tick,
                )
                or 0
            )
        except Exception:
            self._passive_steam_watch_timer_id = 0

    def _operation_status_is_protected(self) -> bool:
        if (
            bool(getattr(self, "_mod_operation_pending", False))
            or bool(getattr(self, "_mod_operation_running", False))
            or bool(getattr(self, "_batch_unsubscribe_running", False))
        ):
            return True
        try:
            current_status = str(self.operation_status_label.get_text() or "")
        except Exception:
            current_status = ""
        return self._is_one_shot_action_status(current_status)

    def _set_mod_operation_pending(self, pending: bool) -> None:
        self._mod_operation_pending = bool(pending)
        try:
            self._update_batch_action_buttons()
        except Exception:
            pass

    def _stop_passive_steam_watch(self, *, remove_source: bool = True) -> None:
        timer_id = int(getattr(self, "_passive_steam_watch_timer_id", 0) or 0)
        if timer_id and remove_source:
            try:
                GLib.source_remove(timer_id)
            except Exception:
                pass
        self._passive_steam_watch_timer_id = 0
        self._passive_steam_watch_probe_running = False
        self._passive_steam_watch_check_started_at = 0.0
        self._passive_steam_watch_checking_status_shown = False
        self._passive_steam_shutdown_status_shown = False

    def _passive_steam_watch_tick(self):
        if not self._mod_manager_is_visible():
            self._stop_passive_steam_watch(remove_source=False)
            return False

        try:
            steam_running = bool(is_native_steam_running())
        except Exception:
            steam_running = False
        if not steam_running:
            if bool(getattr(self, "_passive_steam_was_ready_this_session", False)) and not bool(
                getattr(self, "_passive_steam_shutdown_status_shown", False)
            ):
                self._passive_steam_shutdown_status_shown = True
                self._last_mod_state_query_ok = False
                self._steam_management_verified = False
                self._set_steam_status_pill("offline")
                self._update_batch_action_buttons()
                if not self._operation_status_is_protected():
                    self._set_mod_operation_status("Steam closed.", running=False)
            self._passive_steam_watch_check_started_at = 0.0
            self._passive_steam_watch_checking_status_shown = False
            return True

        steam_was_observed_closed = bool(getattr(self, "_passive_steam_shutdown_status_shown", False))
        self._passive_steam_shutdown_status_shown = False
        if bool(getattr(self, "_passive_steam_was_ready_this_session", False)) and not self._has_steam_offline_rows():
            if not steam_was_observed_closed:
                verified = self._compute_steam_management_verified()
                self._steam_management_verified = verified
                self._update_batch_action_buttons()
                if verified:
                    self._clear_operation_start_steam_prompt_suppression_if_ready()
                    self._set_steam_status_pill("online")
                    self._passive_steam_watch_check_started_at = 0.0
                    self._passive_steam_watch_checking_status_shown = False
                    return True

        if not float(getattr(self, "_passive_steam_watch_check_started_at", 0.0) or 0.0):
            self._passive_steam_watch_check_started_at = time.monotonic()
        if not bool(getattr(self, "_passive_steam_watch_checking_status_shown", False)):
            self._passive_steam_watch_checking_status_shown = True
            self._set_steam_status_pill("checking")
            if not self._operation_status_is_protected():
                self._set_mod_operation_status("Steam started. Checking mods...", running=False)

        started_at = float(getattr(self, "_passive_steam_watch_check_started_at", 0.0) or time.monotonic())
        if time.monotonic() - started_at >= PASSIVE_STEAM_WATCH_READY_TIMEOUT_S:
            self._steam_management_verified = False
            self._set_steam_status_pill("issue")
            self._update_batch_action_buttons()
            if not self._operation_status_is_protected():
                self._set_mod_operation_status("Could not check mods with Steam.", running=False)
            self._stop_passive_steam_watch(remove_source=False)
            return False

        if bool(getattr(self, "_passive_steam_watch_probe_running", False)):
            return True

        sample_ids = self._passive_steam_watch_sample_ids()
        if not sample_ids:
            self._last_mod_state_query_ok = True
            self._set_steam_status_pill("online")
            self._steam_management_verified = True
            self._clear_operation_start_steam_prompt_suppression_if_ready()
            self._update_batch_action_buttons()
            self._passive_steam_was_ready_this_session = True
            self._stop_passive_steam_watch(remove_source=False)
            return False

        self._passive_steam_watch_probe_running = True

        def worker(ids: list[int]):
            try:
                ok, states = query_ugc_state_checked(ids, appid=int(APPID), timeout=8)
            except Exception:
                ok = False
                states = {}
            ready = bool(ok) and bool(states)

            def done():
                self._passive_steam_watch_probe_running = False
                if not self._mod_manager_is_visible():
                    self._stop_passive_steam_watch()
                    return False
                if ready:
                    protected_status = self._operation_status_is_protected()
                    self._stop_passive_steam_watch()
                    self.refresh(
                        completion_status=None if protected_status else "Mod Manager refreshed.",
                        preserve_status=protected_status,
                    )
                    return False
                started = float(getattr(self, "_passive_steam_watch_check_started_at", 0.0) or time.monotonic())
                if time.monotonic() - started >= PASSIVE_STEAM_WATCH_READY_TIMEOUT_S:
                    self._steam_management_verified = False
                    self._set_steam_status_pill("issue")
                    self._update_batch_action_buttons()
                    if not self._operation_status_is_protected():
                        self._set_mod_operation_status("Could not check mods with Steam.", running=False)
                    self._stop_passive_steam_watch()
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, args=(sample_ids,), daemon=True).start()
        return True

    def _build(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_hexpand(True)

        heading = Gtk.Label(label="Manage Installed Mods")
        heading.set_xalign(0.0)
        heading.add_css_class("steamcmd-heading")
        heading.set_hexpand(True)
        header.append(heading)

        self.steam_status_pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.steam_status_pill.add_css_class("steam-status-pill")
        self.steam_status_pill.set_halign(Gtk.Align.END)
        self.steam_status_pill.set_valign(Gtk.Align.CENTER)
        self.steam_status_pill.set_margin_end(8)
        header.append(self.steam_status_pill)

        self.steam_status_dot = Gtk.Label(label="●")
        self.steam_status_dot.add_css_class("steam-status-dot")
        self.steam_status_pill.append(self.steam_status_dot)

        self.steam_status_text = Gtk.Label(label="")
        self.steam_status_text.add_css_class("steam-status-text")
        self.steam_status_pill.append(self.steam_status_text)
        self._set_steam_status_pill("checking")

        self.btn_close_x = Gtk.Button()
        self.btn_close_x.set_can_focus(False)
        self.btn_close_x.add_css_class("flat")
        self.btn_close_x.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        self.btn_close_x.set_tooltip_text("Close")
        attach_pointer_cursor(self.btn_close_x)
        self.btn_close_x.connect("clicked", lambda *_: self.hide())
        header.append(self.btn_close_x)

        self.card.append(header)

        self.card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search mods by name or ID…")
        self.search.set_hexpand(True)
        self.card.append(self.search)

        self.card.append(self._make_column_header())

        sc = Gtk.ScrolledWindow()
        sc.add_css_class("mods-list")
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_min_content_height(320)
        self.card.append(sc)

        self.list_overlay = Gtk.Overlay()
        self.list_overlay.set_hexpand(True)
        self.list_overlay.set_vexpand(True)
        self.list_overlay.set_size_request(-1, 320)
        sc.set_child(self.list_overlay)

        self.lb = Gtk.ListBox()
        self.lb.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_overlay.set_child(self.lb)

        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_halign(Gtk.Align.CENTER)
        self.loading_spinner.set_valign(Gtk.Align.CENTER)
        self.loading_spinner.set_size_request(48, 48)
        self.loading_spinner.set_visible(False)
        self.list_overlay.add_overlay(self.loading_spinner)

        self.empty_state = Gtk.Label(label="No installed mods found.\nJoin a modded server to install required mods.")
        self.empty_state.set_halign(Gtk.Align.CENTER)
        self.empty_state.set_valign(Gtk.Align.CENTER)
        self.empty_state.set_justify(Gtk.Justification.CENTER)
        self.empty_state.set_xalign(0.5)
        self.empty_state.set_yalign(0.5)
        self.empty_state.set_visible(False)
        self.empty_state.add_css_class("mods-empty-state")
        self.list_overlay.add_overlay(self.empty_state)

        action_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        action_area.set_hexpand(True)

        selection_status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        selection_status_row.set_hexpand(True)
        action_area.append(selection_status_row)

        self.selected_mod_count_label = Gtk.Label(label="Selected: 0")
        self.selected_mod_count_label.set_xalign(0.0)
        self.selected_mod_count_label.set_hexpand(False)
        self.selected_mod_count_label.set_max_width_chars(96)
        self.selected_mod_count_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.selected_mod_count_label.add_css_class("dim-label")
        selection_status_row.append(self.selected_mod_count_label)

        self.clear_selection_separator = Gtk.Label(label="·")
        self.clear_selection_separator.set_xalign(0.0)
        self.clear_selection_separator.add_css_class("dim-label")
        self.clear_selection_separator.set_visible(False)
        selection_status_row.append(self.clear_selection_separator)

        self.clear_selection_link = Gtk.Label(label="Clear")
        self.clear_selection_link.set_xalign(0.0)
        self.clear_selection_link.set_halign(Gtk.Align.START)
        self.clear_selection_link.set_tooltip_text("Clear selected mods")
        self.clear_selection_link.add_css_class("mods-clear-selection-link")
        self.clear_selection_link.set_visible(False)
        clear_selection_click = Gtk.GestureClick.new()
        clear_selection_click.set_button(0)
        clear_selection_click.connect("pressed", self._on_clear_selection_clicked)
        self.clear_selection_link.add_controller(clear_selection_click)
        attach_pointer_cursor(self.clear_selection_link)
        selection_status_row.append(self.clear_selection_link)

        self.operation_status_label = Gtk.Label(label="")
        self.operation_status_label.set_xalign(0.0)
        self.operation_status_label.set_halign(Gtk.Align.FILL)
        self.operation_status_label.set_hexpand(True)
        self.operation_status_label.set_max_width_chars(120)
        self.operation_status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.operation_status_label.add_css_class("dim-label")
        action_area.append(self.operation_status_label)

        selected_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        selected_btn_row.set_halign(Gtk.Align.END)
        selected_btn_row.set_hexpand(True)

        self.btn_unsubscribe_selected = Gtk.Button(label="Unsubscribe Selected")
        self.btn_unsubscribe_selected.set_sensitive(False)
        self.btn_unsubscribe_selected.set_size_request(170, -1)
        attach_pointer_cursor(self.btn_unsubscribe_selected)
        selected_btn_row.append(self.btn_unsubscribe_selected)

        self.btn_cleanup_local_files = Gtk.Button(label="Clean Up Local Files")
        self.btn_cleanup_local_files.set_sensitive(False)
        self.btn_cleanup_local_files.set_size_request(170, -1)
        attach_pointer_cursor(self.btn_cleanup_local_files)
        selected_btn_row.append(self.btn_cleanup_local_files)

        action_area.append(selected_btn_row)

        self.batch_secondary_action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.batch_secondary_action_row.set_halign(Gtk.Align.END)
        self.batch_secondary_action_row.set_hexpand(True)

        self.btn_unsubscribe_all_workshop = Gtk.Button(label="Unsubscribe All Workshop Mods")
        self.btn_unsubscribe_all_workshop.add_css_class("mods-danger-action")
        self.btn_unsubscribe_all_workshop.set_sensitive(False)
        self.btn_unsubscribe_all_workshop.set_size_request(350, -1)
        attach_pointer_cursor(self.btn_unsubscribe_all_workshop)
        self.batch_secondary_action_row.append(self.btn_unsubscribe_all_workshop)

        self.btn_stop_batch_unsubscribe = Gtk.Button(label="Stop After Current")
        self.btn_stop_batch_unsubscribe.add_css_class("mods-stop-action")
        self.btn_stop_batch_unsubscribe.set_visible(False)
        self.btn_stop_batch_unsubscribe.set_sensitive(False)
        self.btn_stop_batch_unsubscribe.set_size_request(350, -1)
        attach_pointer_cursor(self.btn_stop_batch_unsubscribe)
        self.batch_secondary_action_row.append(self.btn_stop_batch_unsubscribe)

        action_area.append(self.batch_secondary_action_row)
        self.card.append(action_area)

        self.lb.set_filter_func(self._filter_row)
        self.search.connect("search-changed", self._on_search_changed)

        self.btn_unsubscribe_selected.connect("clicked", lambda *_: self._on_batch_action_clicked("unsubscribe"))
        self.btn_cleanup_local_files.connect("clicked", lambda *_: self._on_batch_action_clicked("cleanup_local_files"))
        self.btn_unsubscribe_all_workshop.connect("clicked", self._on_unsubscribe_all_workshop_clicked)
        self.btn_stop_batch_unsubscribe.connect("clicked", self._on_stop_batch_unsubscribe_clicked)

        # Click scrim to close
        scrim_click = Gtk.GestureClick.new()
        scrim_click.set_button(0)  # any button
        scrim_click.connect("pressed", lambda *_: self.hide())
        self.scrim.add_controller(scrim_click)

        self.refresh()

    def _make_column_header(self) -> Gtk.Box:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_hexpand(True)
        header.set_margin_start(7)
        header.set_margin_end(7)
        header.add_css_class("mods-column-header")

        select_lbl = Gtk.Label(label="")
        select_lbl.set_size_request(MOD_SELECT_COLUMN_WIDTH, -1)
        select_lbl.set_tooltip_text("Select mods")
        header.append(select_lbl)

        name_lbl = self._make_sort_header_label("Mod Name", "name", xalign=0.0)
        name_lbl.set_hexpand(True)
        header.append(name_lbl)

        header_tail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_tail.set_margin_start(-3)
        header.append(header_tail)

        header_tail.append(self._make_column_separator())

        workshop_lbl = self._make_sort_header_label("Workshop", "workshop", xalign=0.0)
        workshop_lbl.set_width_chars(MOD_WORKSHOP_COLUMN_CHARS)
        header_tail.append(workshop_lbl)

        header_tail.append(self._make_column_separator())

        id_lbl = self._make_sort_header_label("ID", "id", xalign=0.0)
        id_lbl.set_size_request(MOD_ID_COLUMN_WIDTH, -1)
        header_tail.append(id_lbl)

        header_tail.append(self._make_column_separator())

        size_lbl = self._make_sort_header_label("Size", "size", xalign=1.0)
        size_lbl.set_width_chars(MOD_SIZE_COLUMN_CHARS)
        header_tail.append(size_lbl)

        header_tail.append(self._make_column_separator())

        used_lbl = self._make_sort_header_label("Last Used", "last_used", xalign=0.0)
        used_lbl.set_width_chars(MOD_LAST_USED_COLUMN_CHARS)
        header_tail.append(used_lbl)

        return header

    def _make_sort_header_label(self, title: str, sort_key: str, *, xalign: float) -> Gtk.Label:
        label = Gtk.Label(label=self._sort_header_text(title, sort_key))
        label.set_xalign(xalign)
        label.add_css_class("dim-label")
        label.set_tooltip_text(f"Sort by {title}")
        self._sort_header_labels[sort_key] = (label, title)
        click = Gtk.GestureClick.new()
        click.set_button(0)
        click.connect("pressed", lambda *_: self._on_sort_header_clicked(sort_key))
        label.add_controller(click)
        attach_pointer_cursor(label)
        return label

    def _sort_header_text(self, title: str, sort_key: str) -> str:
        if self.sort_key != sort_key:
            return title
        return f"{title} {'▲' if self.sort_ascending else '▼'}"

    def _update_sort_headers(self):
        for key, (label, title) in getattr(self, "_sort_header_labels", {}).items():
            try:
                label.set_text(self._sort_header_text(title, key))
            except Exception:
                pass

    def _on_sort_header_clicked(self, sort_key: str):
        if self.sort_key == sort_key:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_key = sort_key
            self.sort_ascending = True
        self._update_sort_headers()
        self._render_loaded_items()
        return False

    def _make_column_separator(self) -> Gtk.Separator:
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.add_css_class("mods-column-separator")
        return sep

    def _workshop_dir(self) -> str:
        try:
            win = getattr(self.host, "_win", None)
            val = str(win.settings.get("workshop_dir") or "").strip() if win else ""
            return val or autodetect_workshop_dir() or ""
        except Exception:
            return ""

    def _proton_prefix(self) -> str:
        try:
            win = getattr(self.host, "_win", None)
            if win and hasattr(win, "_get_dayz_proton_prefix"):
                return str(win._get_dayz_proton_prefix() or "").strip()
        except Exception:
            pass
        return ""

    def _clear_list(self):
        child = self.lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lb.remove(child)
            child = nxt

    def _set_loading(self, loading: bool):
        try:
            self.loading_spinner.set_visible(bool(loading))
            if loading:
                self.loading_spinner.start()
            else:
                self.loading_spinner.stop()
        except Exception:
            pass
        if loading:
            self._set_empty_state(False)
        self._update_batch_action_buttons()

    def refresh(self, completion_status: str | None = None, *, preserve_status: bool = False):
        workshop_dir = self._workshop_dir()
        proton_prefix = self._proton_prefix()
        self._steam_management_verified = False
        self._set_steam_status_pill("checking")
        self._clear_list()
        self._set_loading(True)

        def worker():
            try:
                items = self._load_installed_items(workshop_dir, proton_prefix)
            except Exception as exc:
                GLib.idle_add(self._apply_load_error, str(exc), bool(completion_status))
                return
            GLib.idle_add(self._apply_items, items, completion_status, preserve_status)

        threading.Thread(target=worker, daemon=True).start()

    def _load_installed_items(self, workshop_dir: str, proton_prefix: str):
        workshop_roots = _candidate_workshop_roots(workshop_dir=workshop_dir)
        try:
            print(f"[MOD MANAGER] workshop roots: {[str(root) for root in workshop_roots]}", flush=True)
        except Exception:
            pass
        ids = sorted(set(_read_installed_mod_ids_from_roots(workshop_roots)))
        name_map = _name_map_from_symlinks(proton_prefix=proton_prefix)
        try:
            metadata = load_mod_metadata().get("mods") or {}
        except Exception:
            metadata = {}
        try:
            steam_running_for_status = bool(is_native_steam_running())
        except Exception:
            steam_running_for_status = False
        try:
            state_query_ok, state_by_id = query_ugc_state_checked(ids) if ids else (True, {})
        except Exception:
            state_query_ok = False
            state_by_id = {}
        self._last_mod_state_query_ok = bool(state_query_ok)
        try:
            resolved_names = resolve_best_mod_names(
                ids,
                metadata=metadata,
                workshop_dir=workshop_dir,
                symlink_names=name_map,
            )
        except Exception:
            resolved_names = {}

        items = []
        for mid in ids:
            state = state_by_id.get(mid) or state_by_id.get(str(mid)) or {}
            meta = metadata.get(str(mid)) or metadata.get(mid) or {}
            mod_folder = _content_folder_for_mod(workshop_roots, int(mid))
            local_folder_exists = False
            try:
                local_folder_exists = mod_folder is not None and mod_folder.is_dir() and not mod_folder.is_symlink()
            except Exception:
                local_folder_exists = False
            if state_query_ok and state and not bool(state.get("installed", False)) and not local_folder_exists:
                continue

            nm = (
                resolved_names.get(mid)
                or clean_display_mod_name("", mid)
            )

            subscribed = bool(state.get("subscribed", False))
            installed = bool(state.get("installed", False)) or bool(local_folder_exists)
            unchecked_status = "Could not check" if steam_running_for_status else "Steam Offline"
            unchecked_category = "check_failed" if steam_running_for_status else "steam_offline"
            selection_state = {
                "category": unchecked_category,
                "workshop_confirmed": False,
                "local_delete_candidate": False,
                "requires_verification": True,
                "has_error": False,
            }
            if not state_query_ok:
                workshop_status = unchecked_status
            elif state:
                workshop_status = "Subscribed" if subscribed else "Local only"
                selection_state = {
                    "category": "workshop" if subscribed else "local_only",
                    "workshop_confirmed": subscribed,
                    "local_delete_candidate": not subscribed,
                    "requires_verification": False,
                    "has_error": False,
                }
            else:
                workshop_status = "Could not check"
                selection_state = {
                    "category": "check_failed",
                    "workshop_confirmed": False,
                    "local_delete_candidate": False,
                    "requires_verification": True,
                    "has_error": False,
                }
            size_bytes = 0
            for value in (
                (meta or {}).get("size_bytes") if isinstance(meta, dict) else 0,
                state.get("size_on_disk") if isinstance(state, dict) else 0,
                state.get("total_bytes") if isinstance(state, dict) else 0,
            ):
                try:
                    size_i = int(value or 0)
                except Exception:
                    size_i = 0
                if size_i > 0:
                    size_bytes = size_i
                    break
            last_used_at = str((meta or {}).get("last_used_at") or "").strip() if isinstance(meta, dict) else ""
            items.append((
                nm,
                mid,
                subscribed,
                installed,
                _format_mod_size(size_bytes),
                _format_last_used(last_used_at),
                size_bytes,
                _last_used_sort_value(last_used_at),
                workshop_status,
                selection_state,
            ))
        return items

    def _apply_items(self, items, completion_status: str | None = None, preserve_status: bool = False):
        self._set_loading(False)
        self._loaded_items = list(items or [])
        self._prune_selected_mod_ids(self._loaded_items)
        self._render_loaded_items()
        if self._has_steam_checked_rows():
            self._passive_steam_was_ready_this_session = True
        self._set_steam_status_pill(self._steam_status_from_loaded_items())
        self._steam_management_verified = self._compute_steam_management_verified()
        self._clear_operation_start_steam_prompt_suppression_if_ready()
        self._update_batch_action_buttons()
        self._maybe_show_start_steam_manage_reminder()
        if completion_status:
            self._set_mod_operation_status(str(completion_status), running=False)
        elif preserve_status:
            pass
        else:
            current_status = ""
            try:
                current_status = str(self.operation_status_label.get_text() or "")
            except Exception:
                current_status = ""
            self._clear_one_shot_action_status_if_idle()
        self._maybe_start_passive_steam_watch()
        return False

    def _render_loaded_items(self):
        self._rows_cache.clear()
        self._clear_list()

        for item in self._sorted_items(self._loaded_items):
            try:
                nm, mid, subscribed, installed, size_text, last_used_text, _size_bytes, _last_used_sort, workshop_status, *_ = item
            except Exception:
                nm, mid = item[:2]
                subscribed = False
                installed = False
                size_text = "—"
                last_used_text = "Never"
                workshop_status = "Subscribed" if bool(subscribed) else "Steam Offline"
            row = self._make_row(
                nm,
                mid,
                subscribed=subscribed,
                installed=installed,
                size_text=size_text,
                last_used_text=last_used_text,
                workshop_status=workshop_status,
            )
            self.lb.append(row)
            self._rows_cache.append((row, nm.lower(), str(mid)))

        self.lb.invalidate_filter()
        self._update_empty_state()
        self._sync_selection_ui()
        return False

    def _on_search_changed(self, *_):
        self.lb.invalidate_filter()
        GLib.idle_add(self._update_empty_state)

    def _set_empty_state(self, visible: bool):
        try:
            self.empty_state.set_visible(bool(visible))
        except Exception:
            pass
        return False

    def _update_empty_state(self):
        try:
            if self.loading_spinner.get_visible():
                self._set_empty_state(False)
                return False
        except Exception:
            pass
        visible_rows = 0
        child = self.lb.get_first_child()
        while child is not None:
            try:
                if child.get_visible() and child.get_child() is not None:
                    visible_rows += 1
                    break
            except Exception:
                pass
            child = child.get_next_sibling()
        self._set_empty_state(visible_rows == 0)
        return False

    def _sorted_items(self, items):
        rows = list(items or [])
        key = self.sort_key
        ascending = bool(self.sort_ascending)
        reverse_known = not ascending
        if key == "last_used":
            reverse_known = ascending

        def known_and_value(item):
            try:
                nm, mid, subscribed, _installed, _size_text, last_used_text, size_bytes, last_used_sort, workshop_status, *_ = item
            except Exception:
                nm, mid = item[:2]
                subscribed = False
                last_used_text = ""
                size_bytes = 0
                last_used_sort = None
                workshop_status = "Subscribed" if bool(subscribed) else "Steam Offline"

            if key == "name":
                return True, (str(nm or "").casefold(), int(mid))
            if key == "workshop":
                text = str(workshop_status or ("Subscribed" if bool(subscribed) else "Steam Offline"))
                return True, (text.casefold(), str(nm or "").casefold(), int(mid))
            if key == "id":
                return True, int(mid)
            if key == "size":
                try:
                    size_i = int(size_bytes or 0)
                except Exception:
                    size_i = 0
                return size_i > 0, size_i
            if key == "last_used":
                if last_used_sort is None:
                    last_used_sort = _last_used_sort_value(last_used_text)
                if last_used_sort is None:
                    return True, float("-inf")
                return True, float(last_used_sort)
            return True, (str(nm or "").casefold(), int(mid))

        known = []
        unknown = []
        for item in rows:
            is_known, value = known_and_value(item)
            (known if is_known else unknown).append((value, item))

        known.sort(key=lambda pair: pair[0], reverse=reverse_known)
        unknown.sort(key=lambda pair: self._item_name_id(pair[1]))
        return [item for _value, item in known] + [item for _value, item in unknown]

    def _item_name_id(self, item):
        try:
            nm, mid = item[:2]
            return (str(nm or "").casefold(), int(mid))
        except Exception:
            return ("", 0)

    def _apply_load_error(self, message: str, started_from_start_steam: bool = False):
        self._set_loading(False)
        self._loaded_items = []
        self._prune_selected_mod_ids(self._loaded_items)
        self._rows_cache.clear()
        self._clear_list()
        self._set_empty_state(False)
        self._stop_passive_steam_watch()
        self._last_mod_state_query_ok = False
        self._steam_management_verified = False
        self._set_steam_status_pill("issue")
        if started_from_start_steam:
            self._set_mod_operation_status("Mod Manager refresh failed.", running=False)
        self._confirm_show(
            "Mod Manager refresh failed",
            message or "The installed mod list could not be loaded.",
            "OK",
            lambda _ok: None,
        )
        return False

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        q = (self.search.get_text() or "").strip().lower()
        if not q:
            return True
        for r, nm_lc, mid_s in self._rows_cache:
            if r is row:
                return (q in nm_lc) or (q in mid_s)
        return True

    def _make_workshop_link_button(self, mod_id: int) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("mod-workshop-link-btn")
        btn.set_tooltip_text("Open this mod in Steam Workshop")
        btn.set_child(Gtk.Image.new_from_icon_name("external-link-symbolic"))
        btn.set_size_request(24, 24)
        attach_pointer_cursor(btn)
        btn.connect("clicked", lambda *_: self._open_workshop_page(mod_id))
        return btn

    def _open_workshop_page(self, mod_id: int):
        steam_cmd = resolve_native_steam_cmd()
        if not steam_cmd:
            self._confirm_show(
                "Steam not found",
                "Native Steam could not be found, so the Workshop page could not be opened.",
                "OK",
                lambda _ok: None,
            )
            return
        url = f"steam://url/CommunityFilePage/{int(mod_id)}"
        try:
            subprocess.Popen([steam_cmd, url])
        except Exception as exc:
            self._confirm_show(
                "Could not open Steam Workshop",
                str(exc) or "Steam could not open the Workshop page.",
                "OK",
                lambda _ok: None,
            )

    def _make_row(
        self,
        mod_name: str,
        mod_id: int,
        *,
        subscribed: bool = False,
        installed: bool = False,
        size_text: str = "—",
        last_used_text: str = "Never",
        workshop_status: str | None = None,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)
        outer.set_margin_start(7)
        outer.set_margin_end(7)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.set_hexpand(True)

        select_check = Gtk.CheckButton()
        select_check.set_tooltip_text("Select this mod")
        select_check.set_size_request(MOD_SELECT_COLUMN_WIDTH, -1)
        try:
            select_check._dzll_mod_id = int(mod_id)
        except Exception:
            pass
        select_check.set_active(int(mod_id) in getattr(self, "_selected_mod_ids", set()))
        attach_pointer_cursor(select_check)
        select_check.connect("toggled", self._on_mod_selection_toggled, int(mod_id))
        line.append(select_check)

        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        name_box.set_hexpand(True)

        lbl = Gtk.Label(label=str(mod_name or clean_display_mod_name("", mod_id)))
        lbl.set_xalign(0.0)
        lbl.set_hexpand(True)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_box.append(lbl)

        line.append(name_box)

        line.append(self._make_column_separator())

        workshop_text = str(workshop_status or ("Subscribed" if subscribed else "Steam Offline"))
        workshop_lbl = Gtk.Label(label=workshop_text)
        workshop_lbl.set_xalign(0.0)
        workshop_lbl.set_width_chars(MOD_WORKSHOP_COLUMN_CHARS)
        workshop_lbl.set_max_width_chars(MOD_WORKSHOP_COLUMN_CHARS)
        workshop_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        workshop_tooltip = workshop_text
        if workshop_text == "Steam Offline":
            workshop_tooltip = "Start Steam to check this mod."
        elif workshop_text in ("Could not check", "Could not check with Steam"):
            workshop_tooltip = "Steam must be running and logged in so DZLL can check this mod."
        workshop_lbl.set_tooltip_text(workshop_tooltip)
        workshop_lbl.add_css_class("dim-label")
        line.append(workshop_lbl)

        line.append(self._make_column_separator())

        id_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        id_box.set_size_request(MOD_ID_COLUMN_WIDTH, -1)

        id_lbl = Gtk.Label(label=str(int(mod_id)))
        id_lbl.set_xalign(0.0)
        id_lbl.set_width_chars(MOD_ID_COLUMN_CHARS)
        id_lbl.add_css_class("dim-label")
        id_box.append(id_lbl)
        id_box.append(self._make_workshop_link_button(mod_id))
        line.append(id_box)

        line.append(self._make_column_separator())

        size_lbl = Gtk.Label(label=str(size_text or "—"))
        size_lbl.set_xalign(1.0)
        size_lbl.set_width_chars(MOD_SIZE_COLUMN_CHARS)
        size_lbl.add_css_class("dim-label")
        line.append(size_lbl)

        line.append(self._make_column_separator())

        used_lbl = Gtk.Label(label=str(last_used_text or "Never"))
        used_lbl.set_xalign(0.0)
        used_lbl.set_width_chars(MOD_LAST_USED_COLUMN_CHARS)
        used_lbl.add_css_class("dim-label")
        line.append(used_lbl)

        outer.append(line)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        row.set_child(outer)

        return row

    def _loaded_item_id_set(self, items=None) -> set[int]:
        ids = set()
        for item in list(self._loaded_items if items is None else (items or [])):
            try:
                mid = int(item[1])
            except Exception:
                continue
            if mid > 0:
                ids.add(mid)
        return ids

    def _prune_selected_mod_ids(self, items=None) -> None:
        loaded_ids = self._loaded_item_id_set(items)
        self._selected_mod_ids = {
            int(mid)
            for mid in getattr(self, "_selected_mod_ids", set())
            if int(mid) in loaded_ids
        }
        self._sync_selection_ui()

    def _selection_state_for_item(self, item) -> dict:
        try:
            state = item[9]
        except Exception:
            state = {}
        if isinstance(state, dict):
            category = str(state.get("category") or "unknown").strip() or "unknown"
            return {
                "category": category,
                "workshop_confirmed": bool(state.get("workshop_confirmed", False)),
                "local_delete_candidate": bool(state.get("local_delete_candidate", False)),
                "requires_verification": bool(state.get("requires_verification", category == "unknown")),
                "has_error": bool(state.get("has_error", False)),
            }

        try:
            subscribed = bool(item[2])
        except Exception:
            subscribed = False
        if subscribed:
            return {
                "category": "workshop",
                "workshop_confirmed": True,
                "local_delete_candidate": False,
                "requires_verification": False,
                "has_error": False,
            }
        return {
            "category": "unknown",
            "workshop_confirmed": False,
            "local_delete_candidate": False,
            "requires_verification": True,
            "has_error": False,
        }

    def _selected_mod_classification(self) -> dict:
        selected_ids = set(getattr(self, "_selected_mod_ids", set()) or set())
        counts = {
            "total": 0,
            "workshop": 0,
            "local": 0,
            "steam_offline": 0,
            "could_not_check": 0,
            "unsubscribe_candidates": 0,
            "local_cleanup_candidates": 0,
            "requires_verification": 0,
        }
        if not selected_ids:
            return counts

        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                mid = int(item[1])
            except Exception:
                continue
            if mid not in selected_ids:
                continue

            counts["total"] += 1
            state = self._selection_state_for_item(item)
            category = str(state.get("category") or "unknown")
            if bool(state.get("requires_verification", False)):
                if category == "steam_offline":
                    counts["steam_offline"] += 1
                else:
                    counts["could_not_check"] += 1
                counts["requires_verification"] += 1
                continue
            if bool(state.get("workshop_confirmed", False)) or category == "workshop":
                counts["workshop"] += 1
                counts["unsubscribe_candidates"] += 1
            elif category in ("local_only", "pending_cleanup", "failed"):
                counts["local"] += 1
                counts["local_cleanup_candidates"] += 1
            else:
                counts["could_not_check"] += 1
                counts["requires_verification"] += 1
        return counts

    def _loaded_verified_local_cleanup_candidate_count(self) -> int:
        if not self._steam_management_is_verified():
            return 0
        count = 0
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            category = str(state.get("category") or "")
            if bool(state.get("requires_verification", False)):
                continue
            if bool(state.get("workshop_confirmed", False)) or category == "workshop":
                continue
            if category == "local_only" and bool(state.get("local_delete_candidate", False)):
                count += 1
        return count

    def _selection_summary_text(self, classification: dict | None = None) -> str:
        data = classification or self._selected_mod_classification()
        total = int(data.get("total", 0) or 0)
        if total <= 0:
            return "Selected: 0"
        parts = [f"Selected: {total}"]
        labels = (
            ("workshop", "Workshop"),
            ("local", "Local"),
            ("steam_offline", "Steam Offline"),
            ("could_not_check", "Could not check"),
        )
        for key, label in labels:
            value = int(data.get(key, 0) or 0)
            if value:
                parts.append(f"{label}: {value}")
        return " · ".join(parts)

    def _update_selection_summary(self):
        classification = self._selected_mod_classification()
        try:
            self.selected_mod_count_label.set_text(self._selection_summary_text(classification))
        except Exception:
            pass
        try:
            has_selection = int(classification.get("total", 0) or 0) > 0
            self.clear_selection_separator.set_visible(has_selection)
            self.clear_selection_link.set_visible(has_selection)
        except Exception:
            pass
        return False

    def _update_batch_action_buttons(self):
        try:
            loading = bool(self.loading_spinner.get_visible())
        except Exception:
            loading = False
        batch_running = bool(getattr(self, "_batch_unsubscribe_running", False))
        operation_pending = bool(getattr(self, "_mod_operation_pending", False))
        blocked = loading or bool(getattr(self, "_mod_operation_running", False)) or batch_running or operation_pending
        classification = self._selected_mod_classification()
        total = int(classification.get("total", 0) or 0)
        has_workshop = int(classification.get("unsubscribe_candidates", 0) or 0) > 0
        has_local_cleanup_candidate = int(classification.get("local_cleanup_candidates", 0) or 0) > 0
        management_verified = self._steam_management_is_verified()
        cleanup_visible = management_verified and self._loaded_verified_local_cleanup_candidate_count() > 0
        loaded_workshop_count = 0
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                state = self._selection_state_for_item(item)
            except Exception:
                state = {}
            if bool(state.get("workshop_confirmed", False)) or str(state.get("category") or "") == "workshop":
                loaded_workshop_count += 1
        unsubscribe_enabled = (not blocked) and management_verified and total > 0 and has_workshop
        cleanup_enabled = (not blocked) and cleanup_visible and total > 0 and has_local_cleanup_candidate
        unsubscribe_all_enabled = (not blocked) and management_verified and loaded_workshop_count > 0
        unsubscribe_width = 170 if cleanup_visible else 350
        try:
            self.btn_unsubscribe_selected.set_sensitive(unsubscribe_enabled)
            self.btn_unsubscribe_selected.set_size_request(unsubscribe_width, -1)
        except Exception:
            pass
        try:
            self.btn_cleanup_local_files.set_visible(cleanup_visible)
            self.btn_cleanup_local_files.set_sensitive(cleanup_enabled)
        except Exception:
            pass
        try:
            self.btn_unsubscribe_all_workshop.set_sensitive(unsubscribe_all_enabled)
        except Exception:
            pass
        self._update_batch_unsubscribe_stop_button()
        return False

    def _update_batch_unsubscribe_stop_button(self):
        running = bool(getattr(self, "_batch_unsubscribe_running", False))
        stop_requested = bool(getattr(self, "_batch_unsubscribe_stop_requested", False))
        queue_count = int(getattr(self, "_batch_unsubscribe_queue_count", 0) or 0)
        stop_visible = running and queue_count > 1
        try:
            self.btn_stop_batch_unsubscribe.set_visible(stop_visible)
            self.btn_stop_batch_unsubscribe.set_sensitive(stop_visible and not stop_requested)
            self.btn_unsubscribe_all_workshop.set_visible(not stop_visible)
        except Exception:
            pass
        return False

    def _on_mod_selection_toggled(self, check: Gtk.CheckButton, mod_id: int):
        try:
            mid = int(mod_id)
        except Exception:
            return False
        if mid <= 0:
            return False
        selected = set(getattr(self, "_selected_mod_ids", set()) or set())
        try:
            active = bool(check.get_active())
        except Exception:
            active = False
        if active:
            selected.add(mid)
        else:
            selected.discard(mid)
        self._selected_mod_ids = selected
        self._sync_selection_ui()
        return False

    def _on_clear_selection_clicked(self, *_):
        self._selected_mod_ids = set()
        self._sync_row_checkbox_states()
        self._sync_selection_ui()
        return False

    def _sync_row_checkbox_states(self) -> None:
        selected_ids = set(getattr(self, "_selected_mod_ids", set()) or set())

        def walk(widget):
            if widget is None:
                return
            if isinstance(widget, Gtk.CheckButton):
                try:
                    mid = int(getattr(widget, "_dzll_mod_id", 0) or 0)
                except Exception:
                    mid = 0
                if mid > 0:
                    try:
                        widget.set_active(mid in selected_ids)
                    except Exception:
                        pass
            child = None
            try:
                child = widget.get_first_child()
            except Exception:
                child = None
            while child is not None:
                walk(child)
                try:
                    child = child.get_next_sibling()
                except Exception:
                    child = None

        for row, _nm_lc, _mid_s in list(getattr(self, "_rows_cache", []) or []):
            try:
                walk(row.get_child())
            except Exception:
                pass

    def _sync_selection_ui(self):
        self._update_selection_summary()
        self._update_batch_action_buttons()
        self._clear_transient_action_status_if_idle()
        return False

    def _selected_mod_ids_snapshot(self) -> list[int]:
        ids = []
        seen = set()
        for raw_mid in sorted(getattr(self, "_selected_mod_ids", set()) or set()):
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        return ids

    def _selected_local_cleanup_candidate_ids(self) -> list[int]:
        selected_ids = set(getattr(self, "_selected_mod_ids", set()) or set())
        if not selected_ids:
            return []

        ids = []
        seen = set()
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                mid = int(item[1])
            except Exception:
                continue
            if mid <= 0 or mid not in selected_ids or mid in seen:
                continue
            state = self._selection_state_for_item(item)
            category = str(state.get("category") or "")
            if bool(state.get("requires_verification", False)):
                continue
            if bool(state.get("workshop_confirmed", False)) or category == "workshop":
                continue
            if not bool(state.get("local_delete_candidate", False)):
                continue
            if category != "local_only":
                continue
            seen.add(mid)
            ids.append(mid)
        ids.sort()
        return ids

    def _verify_local_cleanup_candidates(self, candidate_ids: list[int], *, timeout: int = 20) -> tuple[bool, dict, str]:
        result = {
            "safe_ids": [],
            "rejected_ids": [],
        }
        ids = []
        seen = set()
        for raw_mid in list(candidate_ids or []):
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)

        if not ids:
            return True, result, ""

        try:
            ok, states = query_ugc_state_checked(ids, appid=int(APPID), timeout=int(timeout))
        except Exception:
            return False, result, "DZLL could not check the selected mods with Steam."
        if not ok:
            return False, result, "DZLL could not check the selected mods with Steam."

        workshop_roots = _candidate_workshop_roots(workshop_dir=self._workshop_dir())
        for mid in ids:
            state = states.get(mid) or states.get(str(mid)) or {}
            if not state:
                result["rejected_ids"].append(int(mid))
                continue
            if bool(state.get("subscribed", False)):
                result["rejected_ids"].append(int(mid))
                continue
            if not _has_local_workshop_state(workshop_roots, int(mid)):
                result["rejected_ids"].append(int(mid))
                continue
            result["safe_ids"].append(int(mid))

        return True, result, ""

    def _show_start_steam_manage_prompt(self) -> None:
        if bool(getattr(self, "_mod_operation_pending", False)):
            return
        if self._start_steam_manage_reminder_was_shown():
            self._set_mod_operation_status("Start Steam to manage Workshop mods.", running=False)
            return
        self._mark_start_steam_manage_reminder_shown()
        self._set_mod_operation_pending(True)

        def after(ok: bool):
            if ok:
                self._start_native_steam_for_manage()
            else:
                self._set_mod_operation_pending(False)
                self._set_mod_operation_status("Start Steam to manage Workshop mods.", running=False)

        self._confirm_show(
            "Start Steam to manage mods?",
            "DZLL needs Steam running and logged in before it can check or manage Workshop mods.",
            "Start Steam",
            after,
            show_cancel=True,
            cancel_label="Cancel",
        )

    def _start_native_steam_for_manage(self) -> None:
        start_ok, start_error = _launch_native_steam_silent()
        if not start_ok and start_error == "Native Steam executable was not found.":
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Native Steam not found.", running=False)
            return
        if not start_ok:
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Steam could not be started.", running=False)
            return

        self._set_mod_operation_status("Waiting for Steam...", running=False)

        def worker():
            ready = False
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                try:
                    if not is_native_steam_running():
                        time.sleep(2.0)
                        continue
                except Exception:
                    time.sleep(2.0)
                    continue

                candidate_ids = self._loaded_mod_ids()
                if not candidate_ids:
                    ready = True
                    break
                try:
                    ok, states = query_ugc_state_checked(candidate_ids[:3], appid=int(APPID), timeout=8)
                except Exception:
                    ok = False
                    states = {}
                if bool(ok) and bool(states):
                    ready = True
                    break
                time.sleep(2.0)

            def done():
                self._set_mod_operation_pending(False)
                if ready:
                    self.refresh(completion_status="Steam checked Workshop mods.")
                else:
                    self._set_mod_operation_status("DZLL could not check mods with Steam.", running=False)
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_action_clicked(self, action: str):
        if action == "unsubscribe":
            self._start_batch_unsubscribe_selected()
            return False

        self._start_batch_local_cleanup_selected()
        return False

    def _start_batch_local_cleanup_selected(self) -> None:
        if (
            bool(getattr(self, "_mod_operation_running", False))
            or bool(getattr(self, "_batch_unsubscribe_running", False))
            or bool(getattr(self, "_mod_operation_pending", False))
        ):
            return
        if not self._steam_management_is_verified():
            self._show_start_steam_manage_prompt()
            return

        cleanup_ids = self._selected_local_cleanup_candidate_ids()
        if not cleanup_ids:
            selected_ids = self._selected_mod_ids_snapshot()
            if selected_ids:
                self._set_mod_operation_status("Only Local only mods can be cleaned up.", running=False)
            else:
                self._set_mod_operation_status("No local files selected for cleanup.", running=False)
            return

        self._set_mod_operation_pending(True)
        self._set_mod_operation_status("Checking with Steam...", running=False)

        def worker():
            ready, plan, error = self._verify_local_cleanup_candidates(cleanup_ids, timeout=20)

            def done():
                if not ready:
                    self._set_mod_operation_pending(False)
                    self._set_mod_operation_status(
                        error or "DZLL could not check the selected mods with Steam.",
                        running=False,
                    )
                    return False

                safe_ids = list(plan.get("safe_ids") or [])
                if not safe_ids:
                    self._set_mod_operation_pending(False)
                    self._set_mod_operation_status("Selected mods are no longer safe to clean up.", running=False)
                    return False

                try:
                    steam_running = bool(is_native_steam_running())
                except Exception:
                    steam_running = False

                if steam_running:
                    title = "Close Steam and clean up local files?"
                    body = "DZLL will close Steam before deleting selected local-only Workshop files."
                    ok_label = "Close Steam and Clean Up"
                    close_steam = True
                    show_restart_checkbox = True
                else:
                    title = "Clean up selected local files?"
                    body = "DZLL will delete selected local-only Workshop files."
                    ok_label = "Clean Up"
                    close_steam = False
                    show_restart_checkbox = False

                def after(ok: bool):
                    if ok:
                        restart_steam = False
                        if close_steam:
                            try:
                                restart_steam = bool(self.confirm_check.get_active())
                            except Exception:
                                restart_steam = False
                            self._mark_start_steam_manage_reminder_shown()
                            self._save_restart_steam_after_local_cleanup_pref(restart_steam)
                        self._set_mod_operation_pending(False)
                        self._run_batch_local_cleanup(
                            safe_ids,
                            close_steam=close_steam,
                            restart_steam=restart_steam,
                        )
                    else:
                        self._set_mod_operation_pending(False)
                        self._set_mod_operation_status("Cleanup cancelled.", running=False)

                self._confirm_show(
                    title,
                    body,
                    ok_label,
                    after,
                    show_cancel=True,
                    cancel_label="Cancel",
                    checkbox_label="Restart Steam after cleanup" if show_restart_checkbox else "",
                    checkbox_active=self._restart_steam_after_local_cleanup_pref() if show_restart_checkbox else False,
                )
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _run_batch_local_cleanup(self, mod_ids: list[int], *, close_steam: bool, restart_steam: bool = False) -> None:
        ids = []
        seen = set()
        for raw_mid in list(mod_ids or []):
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        if not ids:
            self._set_mod_operation_status("No local files selected for cleanup.", running=False)
            return

        self._set_mod_operation_status("Closing Steam..." if close_steam else "Cleaning up local files...", running=True)

        def worker():
            failures = []
            cleaned_ids = []
            steam_closed_by_cleanup = False
            cleanup_attempted = False
            restart_attempted = False
            restart_ok = False

            ready, plan, error = self._verify_local_cleanup_candidates(ids, timeout=20)
            if not ready:
                failures.append((0, error or "DZLL could not check the selected mods with Steam."))
            else:
                ids[:] = list(plan.get("safe_ids") or [])
                if not ids:
                    failures.append((0, "Selected mods are no longer safe to clean up."))

            if not failures and close_steam:
                steam_cmd = resolve_native_steam_cmd()
                if not steam_cmd:
                    failures.append((0, "Native Steam could not be found. Local files were preserved."))
                else:
                    try:
                        subprocess.run(
                            [steam_cmd, "-shutdown"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                        )
                    except Exception as exc:
                        failures.append((0, f"Steam could not be closed: {exc}"))
                    if not failures and not _wait_for_native_steam_stopped(timeout_s=45.0):
                        failures.append((0, "Steam did not close in time. Local files were preserved."))
                    elif not failures:
                        steam_closed_by_cleanup = True

            try:
                steam_running_now = bool(is_native_steam_running())
            except Exception:
                steam_running_now = True
            if not failures and steam_running_now:
                failures.append((0, "Steam is running. Local files were preserved."))

            total = len(ids)
            if not failures:
                cleanup_attempted = True
                for index, mid in enumerate(ids, start=1):
                    try:
                        if is_native_steam_running():
                            failures.append((mid, "Steam is running. Local files were preserved."))
                            break
                    except Exception:
                        failures.append((mid, "Could not confirm Steam is closed. Local files were preserved."))
                        break

                    GLib.idle_add(self._set_mod_operation_status, f"Cleaning up {index}/{total}...", True)
                    try:
                        result = delete_ugc_mod_local_files_after_unsubscribe(int(mid), appid=int(APPID), log_fn=print)
                    except Exception as exc:
                        result = {"ok": False, "error": str(exc)}
                    if bool(result.get("ok", False)):
                        cleaned_ids.append(int(mid))
                    else:
                        failures.append((mid, str(result.get("error") or "Delete failed.")))

            if steam_closed_by_cleanup and bool(restart_steam) and cleanup_attempted:
                restart_attempted = True
                steam_cmd = resolve_native_steam_cmd()
                if steam_cmd:
                    try:
                        subprocess.Popen(
                            [steam_cmd, "-silent"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        restart_ok = True
                    except Exception:
                        restart_ok = False

            def done():
                cleaned = len(cleaned_ids)
                failed = len(failures)
                if cleaned and failed:
                    summary = f"Cleaned up: {cleaned} · Failed: {failed}"
                elif cleaned:
                    summary = f"Cleaned up: {cleaned}"
                elif failed:
                    try:
                        first_mid, first_error = failures[0]
                    except Exception:
                        first_mid, first_error = 0, ""
                    if int(first_mid or 0) == 0 and str(first_error or "").strip():
                        summary = str(first_error or "").strip()
                    else:
                        summary = "Cleanup failed."
                else:
                    summary = "No local files selected for cleanup."
                if steam_closed_by_cleanup:
                    self._suppress_start_steam_manage_prompt_for_current_operation()

                if restart_attempted and restart_ok:
                    summary = f"Restarting Steam · {summary}"
                elif steam_closed_by_cleanup:
                    summary = f"Steam closed · {summary}"
                    if restart_attempted and not restart_ok:
                        summary = f"{summary} · Could not restart Steam"

                self._set_mod_operation_status(summary, running=False)
                self.refresh(completion_status=summary)
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _verify_selected_mods_for_unsubscribe(self, selected_ids: list[int], *, timeout: int = 30) -> tuple[bool, dict, str]:
        result = {
            "total": 0,
            "workshop_ids": [],
            "skip_ids": [],
            "unknown_ids": [],
        }
        ids_to_query = []
        for mid in selected_ids:
            result["total"] += 1
            ids_to_query.append(int(mid))

        states = {}
        if ids_to_query:
            try:
                ok, states = query_ugc_state_checked(ids_to_query, appid=int(APPID), timeout=int(timeout))
            except Exception as exc:
                return False, result, str(exc) or "DZLL could not check the selected mods with Steam."
            if not ok:
                return False, result, "DZLL could not check the selected mods with Steam."

        for mid in ids_to_query:
            state = states.get(mid) or states.get(str(mid)) or {}
            if not state:
                result["unknown_ids"].append(int(mid))
            elif bool(state.get("subscribed", False)):
                result["workshop_ids"].append(int(mid))
            else:
                result["skip_ids"].append(int(mid))

        if result["unknown_ids"]:
            return False, result, "DZLL could not check the selected mods with Steam."
        return True, result, ""

    def _unsubscribe_plan_from_loaded_selection(self, selected_ids: list[int]) -> tuple[bool, dict, str]:
        selected = {int(mid) for mid in selected_ids if int(mid) > 0}
        result = {
            "total": 0,
            "workshop_ids": [],
            "skip_ids": [],
            "unknown_ids": [],
        }
        if not selected:
            return True, result, ""

        loaded_by_id = {}
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                loaded_by_id[int(item[1])] = item
            except Exception:
                continue

        for mid in sorted(selected):
            item = loaded_by_id.get(int(mid))
            if item is None:
                continue
            result["total"] += 1
            state = self._selection_state_for_item(item)
            category = str(state.get("category") or "unknown")
            if bool(state.get("requires_verification", False)):
                result["unknown_ids"].append(int(mid))
            elif bool(state.get("workshop_confirmed", False)) or category == "workshop":
                result["workshop_ids"].append(int(mid))
            else:
                result["skip_ids"].append(int(mid))

        if result["unknown_ids"]:
            return False, result, "DZLL could not check the selected mods with Steam."
        return True, result, ""

    def _start_batch_unsubscribe_selected(self):
        if (
            bool(getattr(self, "_batch_unsubscribe_running", False))
            or bool(getattr(self, "_mod_operation_running", False))
            or bool(getattr(self, "_mod_operation_pending", False))
        ):
            return False
        if not self._steam_management_is_verified():
            self._show_start_steam_manage_prompt()
            return False

        selected_ids = self._selected_mod_ids_snapshot()
        if not selected_ids:
            self._set_mod_operation_status("Select one or more mods first.", running=False)
            return False

        self._set_mod_operation_pending(True)
        self._set_mod_operation_status("Checking with Steam...", running=False)

        def worker():
            ready, verification, error = self._verify_selected_mods_for_unsubscribe(selected_ids, timeout=20)

            def done():
                if ready:
                    self._confirm_batch_unsubscribe(verification)
                else:
                    self._show_batch_unsubscribe_start_steam_prompt(selected_ids, error)
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _on_unsubscribe_all_workshop_clicked(self, *_):
        if (
            bool(getattr(self, "_batch_unsubscribe_running", False))
            or bool(getattr(self, "_mod_operation_running", False))
            or bool(getattr(self, "_mod_operation_pending", False))
        ):
            return False
        if not self._steam_management_is_verified():
            self._show_start_steam_manage_prompt()
            return False
        loaded_ids = self._loaded_mod_ids()
        if not loaded_ids:
            self._set_mod_operation_status("No subscribed Workshop mods found.", running=False)
            return False
        self._set_mod_operation_pending(True)
        self._set_mod_operation_status("Checking with Steam...", running=False)

        def worker():
            ready, plan, error = self._build_unsubscribe_all_workshop_plan(loaded_ids, timeout=20)

            def done():
                if ready:
                    self._confirm_unsubscribe_all_workshop(plan)
                else:
                    self._show_unsubscribe_all_start_steam_prompt(error)
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _build_unsubscribe_all_workshop_plan(self, candidate_ids: list[int], *, timeout: int = 20) -> tuple[bool, dict, str]:
        result = {
            "workshop_ids": [],
            "skip_ids": [],
        }
        ids = []
        seen = set()
        for raw_mid in list(candidate_ids or []):
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid <= 0 or mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        if not ids:
            return True, result, ""

        try:
            ok, states = query_ugc_state_checked(ids, appid=int(APPID), timeout=int(timeout))
        except Exception as exc:
            return False, result, str(exc) or "DZLL could not check mods with Steam."
        if not ok:
            return False, result, "DZLL could not check mods with Steam."

        for mid in ids:
            state = states.get(mid) or states.get(str(mid)) or {}
            if state and bool(state.get("subscribed", False)):
                result["workshop_ids"].append(int(mid))
            else:
                result["skip_ids"].append(int(mid))
        result["workshop_ids"].sort()
        result["skip_ids"].sort()
        return True, result, ""

    def _show_unsubscribe_all_start_steam_prompt(self, error: str = "") -> None:
        body = "DZLL needs Steam running and logged in before it can check or unsubscribe Workshop mods."

        def after(ok: bool):
            if ok:
                self._start_native_steam_for_unsubscribe_all()
            else:
                self._set_mod_operation_pending(False)
                self._set_mod_operation_status("Unsubscribe cancelled.", running=False)

        self._confirm_show(
            "Start Steam?",
            body,
            "Start Steam",
            after,
            show_cancel=True,
            cancel_label="Cancel",
        )

    def _start_native_steam_for_unsubscribe_all(self) -> None:
        start_ok, start_error = _launch_native_steam_silent()
        if not start_ok and start_error == "Native Steam executable was not found.":
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Native Steam not found.", running=False)
            self._confirm_show(
                "Steam not found",
                "Native Steam could not be found, so DZLL could not check Workshop mods.",
                "OK",
                lambda _ok: None,
                show_cancel=False,
            )
            return
        if not start_ok:
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Steam could not be started.", running=False)
            self._confirm_show("Steam could not be started", str(start_error), "OK", lambda _ok: None, show_cancel=False)
            return

        self._set_mod_operation_status("Waiting for Steam...", running=False)

        def worker():
            ready = False
            error = ""
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                try:
                    if not is_native_steam_running():
                        time.sleep(2.0)
                        continue
                except Exception:
                    time.sleep(2.0)
                    continue

                candidate_ids = self._loaded_mod_ids()
                ready, _plan, error = self._build_unsubscribe_all_workshop_plan(candidate_ids, timeout=8)
                if ready:
                    break
                time.sleep(2.0)

            def done():
                if ready:
                    self._refresh_then_confirm_unsubscribe_all_workshop()
                else:
                    self._set_mod_operation_pending(False)
                    self._set_mod_operation_status("DZLL could not check mods with Steam.", running=False)
                    self._confirm_show(
                        "Could not check with Steam",
                        error or "DZLL could not check mods with Steam. No mods were unsubscribed.",
                        "OK",
                        lambda _ok: None,
                        show_cancel=False,
                    )
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_then_confirm_unsubscribe_all_workshop(self) -> None:
        self._set_mod_operation_status("Refreshing Mod Manager...", running=False)
        workshop_dir = self._workshop_dir()
        proton_prefix = self._proton_prefix()
        self._set_loading(True)

        def worker():
            try:
                items = self._load_installed_items(workshop_dir, proton_prefix)
            except Exception as exc:
                GLib.idle_add(self._set_mod_operation_pending, False)
                GLib.idle_add(self._apply_load_error, str(exc), False)
                return

            def done():
                self._apply_items(items, "Steam checked Workshop mods.")
                loaded_ids = self._loaded_mod_ids()

                def plan_worker():
                    ready, plan, error = self._build_unsubscribe_all_workshop_plan(loaded_ids, timeout=20)

                    def plan_done():
                        if ready:
                            self._confirm_unsubscribe_all_workshop(plan)
                        else:
                            self._set_mod_operation_pending(False)
                            self._set_mod_operation_status("DZLL could not check mods with Steam.", running=False)
                            self._confirm_show(
                                "Could not check with Steam",
                                error or "DZLL could not check mods with Steam. No mods were unsubscribed.",
                                "OK",
                                lambda _ok: None,
                                show_cancel=False,
                            )
                        return False

                    GLib.idle_add(plan_done)

                threading.Thread(target=plan_worker, daemon=True).start()
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_unsubscribe_all_workshop(self, plan: dict) -> None:
        workshop_ids = [int(mid) for mid in plan.get("workshop_ids", []) if int(mid) > 0]
        skip_count = len(plan.get("skip_ids", []) or [])
        count = len(workshop_ids)
        if count <= 0:
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("No subscribed Workshop mods found.", running=False)
            return

        def after(ok: bool):
            if ok:
                self._set_mod_operation_pending(False)
                self._run_batch_unsubscribe(workshop_ids, skip_count)
            else:
                self._set_mod_operation_pending(False)
                self._set_mod_operation_status("Unsubscribe cancelled.", running=False)

        self._confirm_show(
            "Unsubscribe all Workshop mods?",
            f"DZLL found {count} subscribed Workshop mod(s). "
            "DZLL will not delete local files, but Steam may remove unsubscribed Workshop content.",
            "Continue",
            after,
            show_cancel=True,
            cancel_label="Cancel",
        )

    def _show_batch_unsubscribe_start_steam_prompt(self, selected_ids: list[int], error: str = "") -> None:
        body = "DZLL needs Steam running and logged in before it can check or unsubscribe Workshop mods."

        def after(ok: bool):
            if ok:
                self._start_native_steam_for_batch_unsubscribe(selected_ids)
            else:
                self._set_mod_operation_pending(False)
                self._set_mod_operation_status("Unsubscribe cancelled.", running=False)

        self._confirm_show(
            "Start Steam?",
            body,
            "Start Steam",
            after,
            show_cancel=True,
            cancel_label="Cancel",
        )

    def _start_native_steam_for_batch_unsubscribe(self, selected_ids: list[int]) -> None:
        start_ok, start_error = _launch_native_steam_silent()
        if not start_ok and start_error == "Native Steam executable was not found.":
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Native Steam not found.", running=False)
            self._confirm_show(
                "Steam not found",
                "Native Steam could not be found, so DZLL could not check the selected mods.",
                "OK",
                lambda _ok: None,
                show_cancel=False,
            )
            return
        if not start_ok:
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status("Steam could not be started.", running=False)
            self._confirm_show("Steam could not be started", str(start_error), "OK", lambda _ok: None, show_cancel=False)
            return

        self._set_mod_operation_status("Waiting for Steam...", running=False)

        def worker():
            ready = False
            verification = {}
            error = ""
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                try:
                    if not is_native_steam_running():
                        time.sleep(2.0)
                        continue
                except Exception:
                    time.sleep(2.0)
                    continue

                ready, verification, error = self._verify_selected_mods_for_unsubscribe(selected_ids, timeout=8)
                if ready:
                    break
                time.sleep(2.0)

            def done():
                if ready:
                    self._refresh_then_confirm_batch_unsubscribe(selected_ids)
                else:
                    self._set_mod_operation_pending(False)
                    self._set_mod_operation_status("Could not check with Steam.", running=False)
                    self._confirm_show(
                        "Could not check with Steam",
                        error or "DZLL could not check the selected mods with Steam. No mods were unsubscribed.",
                        "OK",
                        lambda _ok: None,
                        show_cancel=False,
                    )
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_then_confirm_batch_unsubscribe(self, selected_ids: list[int]) -> None:
        self._set_mod_operation_status("Refreshing Mod Manager...", running=False)
        workshop_dir = self._workshop_dir()
        proton_prefix = self._proton_prefix()
        self._set_loading(True)

        def worker():
            try:
                items = self._load_installed_items(workshop_dir, proton_prefix)
            except Exception as exc:
                GLib.idle_add(self._set_mod_operation_pending, False)
                GLib.idle_add(self._apply_load_error, str(exc), False)
                return

            def done():
                self._apply_items(items, "Steam checked Workshop mods.")
                ok, plan, error = self._unsubscribe_plan_from_loaded_selection(selected_ids)
                if ok:
                    self._confirm_batch_unsubscribe(plan)
                else:
                    self._set_mod_operation_pending(False)
                    self._set_mod_operation_status("Could not check with Steam.", running=False)
                    self._confirm_show(
                        "Could not check with Steam",
                        error or "DZLL could not check the selected mods with Steam. No mods were unsubscribed.",
                        "OK",
                        lambda _ok: None,
                        show_cancel=False,
                    )
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_batch_unsubscribe(self, verification: dict) -> None:
        workshop_ids = [int(mid) for mid in verification.get("workshop_ids", [])]
        skip_count = len(verification.get("skip_ids", []) or [])

        if not workshop_ids:
            self._set_mod_operation_pending(False)
            self._set_mod_operation_status(f"No Workshop mods to unsubscribe. Skipped {skip_count}.", running=False)
            return

        def after(ok: bool):
            if ok:
                self._set_mod_operation_pending(False)
                self._run_batch_unsubscribe(workshop_ids, skip_count)
            else:
                self._set_mod_operation_pending(False)
                self._set_mod_operation_status("Unsubscribe cancelled.", running=False)

        self._confirm_show(
            "Unsubscribe selected mods?",
            "DZLL will unsubscribe selected Workshop mods. Local-only mods will be skipped. "
            "DZLL will not delete local files, but Steam may remove unsubscribed Workshop content.",
            "Continue",
            after,
            show_cancel=True,
            cancel_label="Cancel",
        )

    def _attempt_batch_unsubscribe_one(self, mod_id: int, workshop_roots: list[Path]) -> str:
        try:
            mid = int(mod_id)
        except Exception:
            return "failed"
        if mid <= 0:
            return "failed"

        had_local_state_before = _has_local_workshop_state(workshop_roots, mid)
        try:
            steam_running = bool(is_native_steam_running())
        except Exception:
            steam_running = False
        if not steam_running:
            return "steam_closed_before_request"

        try:
            request_ok, _snapshots = request_unsubscribe_ugc_items(
                [mid],
                appid=int(APPID),
                timeout=BATCH_UNSUBSCRIBE_REQUEST_TIMEOUT_S,
            )
        except Exception:
            try:
                steam_running = bool(is_native_steam_running())
            except Exception:
                steam_running = False
            return "steam_issue" if steam_running else "steam_closed_unconfirmed"
        if not request_ok:
            try:
                steam_running = bool(is_native_steam_running())
            except Exception:
                steam_running = False
            return "steam_issue" if steam_running else "steam_closed_unconfirmed"

        deadline = time.monotonic() + BATCH_UNSUBSCRIBE_ITEM_TIMEOUT_S
        while time.monotonic() < deadline:
            local_state_present = _has_local_workshop_state(workshop_roots, mid)
            try:
                steam_running = bool(is_native_steam_running())
            except Exception:
                steam_running = False
            if not steam_running:
                if not local_state_present and had_local_state_before:
                    return "removed"
                return "steam_closed_unconfirmed"
            try:
                ok, states = query_ugc_state_checked(
                    [mid],
                    appid=int(APPID),
                    timeout=BATCH_UNSUBSCRIBE_POLL_QUERY_TIMEOUT_S,
                )
            except Exception:
                ok = False
                states = {}
            if not ok:
                try:
                    steam_running = bool(is_native_steam_running())
                except Exception:
                    steam_running = False
                if not steam_running:
                    if not _has_local_workshop_state(workshop_roots, mid) and had_local_state_before:
                        return "removed"
                    return "steam_closed_unconfirmed"
                return "steam_issue"

            state = states.get(mid) or states.get(str(mid)) or {}
            if ok and state and not bool(state.get("subscribed", False)):
                if not local_state_present:
                    return "removed"
                return self._settle_unsubscribed_local_state(mid, workshop_roots)
            if not local_state_present:
                if had_local_state_before or (ok and not state) or (ok and bool(state)):
                    return "removed"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(BATCH_UNSUBSCRIBE_POLL_INTERVAL_S, remaining))

        return "timed_out"

    def _settle_unsubscribed_local_state(self, mod_id: int, workshop_roots: list[Path]) -> str:
        try:
            mid = int(mod_id)
        except Exception:
            return "unsubscribed"
        deadline = time.monotonic() + BATCH_UNSUBSCRIBE_SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            if not _has_local_workshop_state(workshop_roots, mid):
                return "removed"
            try:
                steam_running = bool(is_native_steam_running())
            except Exception:
                steam_running = False
            if not steam_running:
                return "unsubscribed"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(BATCH_UNSUBSCRIBE_POLL_INTERVAL_S, remaining))
        return "removed" if not _has_local_workshop_state(workshop_roots, mid) else "unsubscribed"

    def _remove_dzll_symlinks_after_unsubscribe(self, mod_id: int) -> bool:
        try:
            remove_dzll_symlinks_for_mod(int(mod_id), proton_prefix=self._proton_prefix(), log_fn=print)
            return True
        except Exception as exc:
            try:
                print(f"[MOD MANAGER] DZLL symlink cleanup after unsubscribe failed for {int(mod_id)}: {exc}", flush=True)
            except Exception:
                pass
            return False

    def _run_batch_unsubscribe(self, workshop_ids: list[int], skip_count: int) -> None:
        if bool(getattr(self, "_batch_unsubscribe_running", False)):
            return
        ids = [int(mid) for mid in workshop_ids if int(mid) > 0]
        if not ids:
            self._set_mod_operation_status(f"No Workshop mods to unsubscribe. Skipped {int(skip_count)}.", running=False)
            return

        self._batch_unsubscribe_running = True
        self._batch_unsubscribe_stop_requested = False
        self._batch_unsubscribe_queue_count = len(ids)
        self._set_mod_operation_status(f"Unsubscribing 0/{len(ids)}...", running=True)
        self._update_batch_unsubscribe_stop_button()
        workshop_roots = _candidate_workshop_roots(workshop_dir=self._workshop_dir())

        def worker():
            unsubscribed = 0
            removed = 0
            failures = 0
            timed_out = 0
            symlink_failures = 0
            stopped = False
            steam_closed = False
            steam_issue = False
            current_unconfirmed = False
            remaining = 0
            confirmed_ids = []
            try:
                for index, mid in enumerate(ids, start=1):
                    if bool(getattr(self, "_batch_unsubscribe_stop_requested", False)):
                        stopped = True
                        remaining = len(ids) - index + 1
                        break
                    GLib.idle_add(self._set_mod_operation_status, f"Unsubscribing {index}/{len(ids)}...", True)
                    result = self._attempt_batch_unsubscribe_one(int(mid), workshop_roots)
                    if result == "removed":
                        unsubscribed += 1
                        removed += 1
                        confirmed_ids.append(int(mid))
                        if not self._remove_dzll_symlinks_after_unsubscribe(int(mid)):
                            symlink_failures += 1
                    elif result == "unsubscribed":
                        unsubscribed += 1
                        confirmed_ids.append(int(mid))
                        if not self._remove_dzll_symlinks_after_unsubscribe(int(mid)):
                            symlink_failures += 1
                    elif result == "steam_closed_before_request":
                        steam_closed = True
                        remaining = len(ids) - index + 1
                        break
                    elif result == "steam_closed_unconfirmed":
                        steam_closed = True
                        current_unconfirmed = True
                        remaining = len(ids) - index + 1
                        break
                    elif result == "steam_issue":
                        steam_issue = True
                        current_unconfirmed = True
                        remaining = len(ids) - index + 1
                        break
                    elif result == "timed_out":
                        timed_out += 1
                    else:
                        failures += 1

                if not stopped and not steam_closed and not steam_issue:
                    remaining = 0
            except Exception:
                failures += 1

            def done():
                self._batch_unsubscribe_running = False
                self._batch_unsubscribe_stop_requested = False
                self._batch_unsubscribe_queue_count = 0
                self._update_batch_unsubscribe_stop_button()
                if steam_closed:
                    self._suppress_start_steam_manage_prompt_for_current_operation()
                    self._last_mod_state_query_ok = False
                    self._steam_management_verified = False
                    self._set_steam_status_pill("offline")
                    self._selected_mod_ids.difference_update(set(confirmed_ids))
                    self._update_batch_action_buttons()
                    parts = ["Steam closed"]
                    if unsubscribed:
                        parts.append(f"Unsubscribed: {unsubscribed}")
                    if current_unconfirmed:
                        parts.append("Check remaining mods")
                    else:
                        parts.append(f"Left: {remaining}")
                    if removed:
                        parts.append(f"Steam removed: {removed}")
                    if skip_count:
                        parts.append(f"Skipped local: {skip_count}")
                    if symlink_failures:
                        parts.append("Shortcut cleanup failed")
                    summary = " · ".join(parts)
                    self._show_batch_steam_offline_interruption()
                elif steam_issue:
                    self._suppress_start_steam_manage_prompt_for_current_operation()
                    self._last_mod_state_query_ok = False
                    self._steam_management_verified = False
                    self._set_steam_status_pill("issue")
                    self._selected_mod_ids.difference_update(set(confirmed_ids))
                    self._update_batch_action_buttons()
                    parts = ["Steam issue", f"Unsubscribed: {unsubscribed}", f"Left: {remaining}"]
                    if removed:
                        parts.append(f"Steam removed: {removed}")
                    if skip_count:
                        parts.append(f"Skipped local: {skip_count}")
                    if symlink_failures:
                        parts.append("Shortcut cleanup failed")
                    summary = " · ".join(parts)
                    self._show_batch_steam_issue_interruption()
                elif stopped:
                    parts = ["Stopped", f"Unsubscribed: {unsubscribed}"]
                    if skip_count:
                        parts.append(f"Skipped: {skip_count}")
                    if removed:
                        parts.append(f"Steam removed: {removed}")
                    if timed_out:
                        parts.append(f"Timed out: {timed_out}")
                    if failures:
                        parts.append(f"Failed: {failures}")
                    parts.append(f"Left: {remaining}")
                    if symlink_failures:
                        parts.append("Shortcut cleanup failed")
                    summary = " · ".join(parts)
                else:
                    parts = [f"Unsubscribed: {unsubscribed}"]
                    if removed:
                        parts.append(f"Steam removed: {removed}")
                    if skip_count:
                        parts.append(f"Skipped local: {skip_count}")
                    if timed_out:
                        parts.append("Timed out")
                        parts.append("Refreshed")
                    if failures:
                        parts.append(f"Failed: {failures}")
                    if symlink_failures:
                        parts.append("Shortcut cleanup failed")
                    summary = " · ".join(parts)
                self.refresh(completion_status=summary)
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop_batch_unsubscribe_clicked(self, *_):
        if bool(getattr(self, "_batch_unsubscribe_running", False)):
            self._batch_unsubscribe_stop_requested = True
            self._set_mod_operation_status("Stopping after current...", running=True)
            self._update_batch_unsubscribe_stop_button()
        return False

    def _loaded_mod_ids(self) -> list[int]:
        seen = set()
        ids = []
        for item in list(getattr(self, "_loaded_items", []) or []):
            try:
                mid = int(item[1])
            except Exception:
                continue
            if mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        return ids

    def _set_mod_operation_status(self, text: str = "", running: bool = False):
        self._mod_operation_running = bool(running)
        try:
            self._update_batch_action_buttons()
        except Exception:
            pass
        try:
            self.operation_status_label.set_text(text or "")
        except Exception:
            pass
        return False
