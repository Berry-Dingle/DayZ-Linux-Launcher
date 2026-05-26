# ==== MAIN.PY PART 1 ==== #

#!/usr/bin/env python3
import os
import json
import time
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
import threading
import re
import gi
import shutil
from pathlib import Path
import sys

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Pango, GLib, Gdk
from .discord_rpc import DiscordRPC
from .config import (
    APP_ID,
    WINDOW_DEFAULT_SIZE,
    RIGHT_BLOCK_WIDTH,
    RIGHT_COL_PX,
    NAME_BLOCK_MIN_WIDTH,
    LOGO_WIDTH_RATIO,
    LOGO_MIN_HEIGHT,
    DIVIDER_COLOR,
    SIDEBAR_WIDTH,
    SIDEBAR_INNER_PADDING,
    DISCLAIMER_GAP_ABOVE,
    DISCLAIMER_COLOR,
    ICON_COL_WIDTH,
    PING_MAX,
    BATCH_SIZE,
    MAX_WORKERS,
    HI_WORKERS,
    OFFLINE_RECHECK_SECS,
    REFRESH_RATE_LIMIT_SECS,
    LOGO_PATH,
    IMAGES_DIR,
    DISCLAIMER_TEXT,
    DEAD_MAX_FAILS,
    APP_VERSION,
    RELEASES_URL,
    GITHUB_LATEST_API,
    STEAM_CURRENT_PLAYERS_URL,
    GLOBAL_PLAYERS_POLL_SECS,
    TEST_SERVER_MARKERS,
)

from .storage import (
    load_favorites,
    save_favorites,
    load_last_played,
    save_last_played,
    human_last_played,
    load_dead_cache,
    save_dead_cache,
)
from .launcher_user_config import set_launcher_shutdown_mode
from .db import fetch_db_overwrite_local, read_servers_from_db
from .live import query_server_live, is_valid_hhmm
from .maps import standardize_map, map_choices_from_db_rows
from .ui_row import ServerObject, ServerRowWidget, hr, attach_pointer_cursor

from .settings import (
    load_settings,
    save_settings,
    reset_settings,
    autodetect_steamcmd_path,
    autodetect_workshop_dir,
)

from .styles import get_app_css
from .update_ui import UpdateUI
from .header_ui import HeaderUI
from .settings_ui import SettingsUI
from .sidebar_ui import build_sidebar
from .startup_ui import build_startup_overlay

from .steamcmd_mods import (
    parse_mods_from_db,
    compute_missing_mods,
    run_steamcmd_install,
    parse_additional_mod_ids,
    merge_mod_lists_with_additional,
    ensure_watch_symlinks,
    scan_installed_mods_in_watch_folder,
    fetch_workshop_sizes_bytes,
)
from .steamcmd_overlay_ui import SteamCMDOverlayUI
from .launcher_state import bootstrap_launcher_state
from .blocklist_utils import bl_normalize_key, bl_load_local, bl_status
from .join_prepare import join_prepare_and_launch
from .server_companion_ui import ServerCompanionPanel
from .launch_utils import launch_direct_steam_url
from .block_warning import preflight_block_warning_ui_blocking, preflight_block_warning

def ensure_user_desktop_integration(
    *,
    app_id: str,
    app_name: str,
    icon_src_path: str,
    categories: str = "Game;",
) -> None:
    """
    Best-effort desktop integration for source/venv usage via: python -m dzll_launcher

    Behavior:
      - installs icon to ~/.local/share/icons/hicolor/256x256/apps/dzll.png
      - creates ~/.local/share/applications/<app_id>.desktop
      - launches via project venv python if available
      - supports src-layout projects
      - derives paths from the actual installed package location

    SAFETY:
      - will NOT overwrite an existing desktop file unless it contains the DZLL marker
      - intended for non-packaged runs; packaged installs should ship their own .desktop/icon
    """
    try:
        home = Path.home()
        user_apps = home / ".local/share/applications"
        user_icons = home / ".local/share/icons/hicolor/256x256/apps"
        user_apps.mkdir(parents=True, exist_ok=True)
        user_icons.mkdir(parents=True, exist_ok=True)

        # ----- icon -----
        icon_name = "dzll2"
        icon_dst = user_icons / f"{icon_name}.png"
        src = Path(icon_src_path).expanduser().resolve()

        if src.is_file():
            need_copy = not icon_dst.exists()
            if not need_copy:
                try:
                    need_copy = (
                        icon_dst.stat().st_size != src.stat().st_size
                        or int(icon_dst.stat().st_mtime) != int(src.stat().st_mtime)
                    )
                except Exception:
                    need_copy = True

            if need_copy:
                try:
                    shutil.copy2(src, icon_dst)
                except Exception as e:
                    print(f"[DZLL] Failed to copy desktop icon: {e}")

        # ----- desktop entry -----
        desktop_path = user_apps / f"{app_id}.desktop"
        marker = "X-DZLL-AutoCreated=true"

        # Do not overwrite non-DZLL desktop entries (e.g. packaged installs)
        if desktop_path.exists():
            try:
                txt = desktop_path.read_text(encoding="utf-8", errors="replace")
                if marker not in txt:
                    return
            except Exception:
                return

        package_dir = Path(__file__).resolve().parent

        # src layout:
        #   /some/path/project/src/dzll_launcher
        # normal layout:
        #   /some/path/project/dzll_launcher
        if package_dir.parent.name == "src":
            run_path = package_dir.parent  # .../project/src
            project_root = run_path.parent  # .../project
        else:
            run_path = package_dir.parent  # .../project
            project_root = run_path

        # Prefer project-local venv python
        venv_python = project_root / ".venv" / "bin" / "python3"
        if venv_python.is_file():
            python_exe = venv_python
        else:
            python_exe = Path(sys.executable) if sys.executable else Path("/usr/bin/python3")

        exec_line = f'"{python_exe}" -m dzll_launcher'

        desktop_text = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={app_name}\n"
            f"Comment={app_name}\n"
            f"Exec={exec_line}\n"
            f"Path={run_path}\n"
            f"Icon={icon_name}\n"
            "Terminal=false\n"
            f"Categories={categories}\n"
            "StartupNotify=true\n"
            f"{marker}\n"
        )

        try:
            old = ""
            if desktop_path.exists():
                old = desktop_path.read_text(encoding="utf-8", errors="replace")
            if old != desktop_text:
                desktop_path.write_text(desktop_text, encoding="utf-8")
                os.chmod(desktop_path, 0o755)
        except Exception as e:
            print(f"[DZLL] Failed to write desktop entry: {e}")
            return

        # Refresh desktop/icon caches best-effort
        try:
            subprocess.run(
                ["update-desktop-database", str(user_apps)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        try:
            subprocess.run(
                ["gtk-update-icon-cache", "-f", "-t", str(home / ".local/share/icons/hicolor")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    except Exception as e:
        print(f"[DZLL] Desktop integration skipped: {e}")

def fav_key(ip: str, gport: int) -> str:
    return f"{ip}:{int(gport)}"


def parse_mods_preview(mods_json: str, max_names: int = 8) -> tuple[int, str]:
    """
    Returns:
      (total_mod_count, preview_string)

    preview_string:
      - up to max_names mod names
      - adds ", ..." if truncated
    """
    if not mods_json:
        return 0, ""
    try:
        arr = json.loads(mods_json)
        if not isinstance(arr, list):
            return 0, ""

        names = []
        for it in arr:
            if isinstance(it, dict):
                nm = (it.get("name") or "").strip()
                if nm:
                    names.append(nm)

        total = len(names)
        if total == 0:
            return 0, ""

        preview = ", ".join(names[:max_names])
        if total > max_names:
            preview += ", ..."
        return total, preview
    except Exception:
        return 0, ""

class DZLLWindow(Gtk.ApplicationWindow):
    SORT_KEYS = ("ping", "players", "played")  # (list sorting keys only)
    _ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="DayZ Linux Launcher")

        # ---- Optional desktop integration for venv/source runs ----
        try:
            ensure_user_desktop_integration(
                app_id=APP_ID,
                app_name="DayZ Linux Launcher",
                icon_src_path=os.path.join(IMAGES_DIR, "dzll2.png"),
                categories="Game;",
            )
        except Exception:
            pass
        # ----------------------------------------------------------

        # Titlebar count state
        self._base_title = "DayZ Linux Launcher"

        # Window default size (from config.py)
        try:
            w, h = WINDOW_DEFAULT_SIZE
        except Exception:
            w, h = (1200, 690)
        self.set_default_size(int(w), int(h))

        self._shutdown_cleanup_done = False
        self.connect("close-request", self._on_close_request)

        # SETTINGS (persisted)
        self.settings = load_settings()
        self._ping_cutoff_ms = int(self.settings.get("high_ping_cutoff_ms", 250) or 250)

        # --- Blocklist state (v2) ---
        self._bl_ok = False
        self.bl_ip_hard = set()
        self.bl_allow_exact = set()
        self.bl_soft = set()
        self.bl_hard = set()

        # Optional compatibility mirrors (not used for v2 logic, but kept)
        self._bl_soft = set()
        self._bl_hard = set()

        # Steam global players state
        self._steam_global_players = None
        GLib.timeout_add_seconds(2, self._steam_global_players_tick)
        GLib.timeout_add_seconds(GLOBAL_PLAYERS_POLL_SECS, self._steam_global_players_tick)

        # Update check state
        self._update_info = None
        self._update_card_dismissed = False

        # Sort defaults
        self.sort_key = "ping"
        self.sort_asc = False  # default: highest ping / offline first
        self._sort_changed_by_user = False

        self.favorites = load_favorites()
        self.last_played = load_last_played()
        self._pending_last_played_obj = None

        self.dead = load_dead_cache()
        self._prune_expired_dead()
        self._clamp_dead_cache()

        self._dead_session = set()

        # live flags used by filter only
        self.live = {}
        self._refresh_rl = {}
        self._obj_by_key = {}

        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._hi_executor = ThreadPoolExecutor(max_workers=HI_WORKERS)

        self._col_groups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in range(7)]

        self._settings_open = False
        self._settings_widgets = {}
        self._settings_update_guard = False

        # SteamCMD calcs
        self.compute_missing_mods = compute_missing_mods
        self.ensure_watch_symlinks = ensure_watch_symlinks
        self.scan_installed_mods_in_watch_folder = scan_installed_mods_in_watch_folder
        self.bootstrap_launcher_state = bootstrap_launcher_state
        self.run_steamcmd_install = run_steamcmd_install
        self.fetch_workshop_sizes_bytes = fetch_workshop_sizes_bytes
        self.GLib = GLib
        self.threading = threading

        # SteamCMD auth overlay state (password is never saved)
        self._steamcmd_auth_request = None
        self._steamcmd_auth_event = None
        self._steamcmd_auth_result = None

        # Two-line overlay state (TOP + LN1 + LN2) = your 3-line model
        self._steamcmd_l1 = ""
        self._steamcmd_l2 = ""
        self._steamcmd_heading = ""

        self._steamcmd_auth_wait_count = 0

        # Progress counters
        self._steamcmd_total_missing = 0
        self._steamcmd_done_missing = 0

        # NEW: progress-by-detection (increment when NEW mod id is detected)
        self._steamcmd_seen_mod_ids = set()
        self._steamcmd_started_missing = 0

        # Mod sizes cache (string id -> "12.3 GB")
        self._steamcmd_mod_sizes = {}

        # Progress Bar
        self._steamcmd_active_mid = None
        self._steamcmd_total_sizes = {}  # later: mid -> total bytes from API
        self._steamcmd_progress_timer_id = 0
        self._steamcmd_last_progress_bytes = 0

        # NEW: Cancel support
        self._steamcmd_cancel_event = threading.Event()
        self._steamcmd_install_in_progress = False

        css = get_app_css(
            DIVIDER_COLOR=DIVIDER_COLOR,
            SIDEBAR_WIDTH=SIDEBAR_WIDTH,
            DISCLAIMER_COLOR=DISCLAIMER_COLOR,
        )

        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        overlay = Gtk.Overlay()
        self._main_overlay = overlay
        self.set_child(overlay)

        self.content_root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overlay.set_child(self.content_root)

        # Update card (extracted)
        self.update_ui = UpdateUI(self)
        self.update_revealer = self.update_ui.build(overlay)

        # SETTINGS SLIDE-OUT
        self.settings_scrim = Gtk.Box()
        self.settings_scrim.set_hexpand(True)
        self.settings_scrim.set_vexpand(True)
        self.settings_scrim.set_visible(False)
        self.settings_scrim.set_can_target(True)
        self.settings_scrim.add_css_class("settings-scrim")

        scrim_click = Gtk.GestureClick.new()
        scrim_click.set_button(0)
        scrim_click.connect("pressed", lambda *_: self._close_settings_panel())
        self.settings_scrim.add_controller(scrim_click)
        overlay.add_overlay(self.settings_scrim)

        self.settings_revealer = Gtk.Revealer()
        self.settings_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.settings_revealer.set_transition_duration(180)
        self.settings_revealer.set_reveal_child(False)
        self.settings_revealer.set_halign(Gtk.Align.END)
        self.settings_revealer.set_valign(Gtk.Align.FILL)
        self.settings_revealer.set_vexpand(True)

        self._settings_ui = SettingsUI(self)
        self._settings_ui._main_overlay = self._main_overlay
        panel = self._settings_ui.build_panel()
        self.settings_revealer.set_child(panel)
        overlay.add_overlay(self.settings_revealer)

        # DISCORD RICH FEATURES
        self._discord_last_join = None
        self._discord = DiscordRPC()
        # apply initial settings (safe even if not available)
        try:
            self._discord.apply_settings(self.settings)
        except Exception:
            pass
        self._discord_watch_lock = threading.Lock()
        self._discord_watch_active = False

        # ----------------------------
        # SteamCMD AUTH OVERLAY (3-line layout)
        # ----------------------------
        self._steamcmd_overlay_ui = SteamCMDOverlayUI(self)
        self._steamcmd_overlay_ui.build(overlay)

        # ----------------------------
        # BLOCKED SERVER WARNING OVERLAY (scrim + card)
        # ----------------------------
        self.warn_scrim = Gtk.Box()
        self.warn_scrim.set_hexpand(True)
        self.warn_scrim.set_vexpand(True)
        self.warn_scrim.set_visible(False)
        self.warn_scrim.set_can_target(True)
        self.warn_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.warn_scrim)

        self.warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.warn_box.set_halign(Gtk.Align.CENTER)
        self.warn_box.set_valign(Gtk.Align.CENTER)
        self.warn_box.set_visible(False)
        self.warn_box.set_can_target(True)
        self.warn_box.add_css_class("warning-card")
        overlay.add_overlay(self.warn_box)

        # Icon + WARNING (centered)
        self.warn_icon = Gtk.Label(label="⚠️")
        self.warn_icon.set_halign(Gtk.Align.CENTER)
        self.warn_icon.add_css_class("warning-icon")
        self.warn_box.append(self.warn_icon)

        self.warn_title = Gtk.Label(label="WARNING")
        self.warn_title.set_halign(Gtk.Align.CENTER)
        self.warn_title.add_css_class("warning-title")
        self.warn_box.append(self.warn_title)

        # Warning text
        self.warn_text = Gtk.Label(
            label="This server originates from an infrastructure source associated\n"
                  "with fraudulent or unverifiable servers. Continue at your own risk."
        )
        self.warn_text.set_wrap(True)
        self.warn_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.warn_text.set_xalign(0.0)
        self.warn_box.append(self.warn_text)

        # IP:PORT
        self.warn_ip = Gtk.Label(label="")
        self.warn_ip.set_xalign(0.0)
        self.warn_ip.add_css_class("dim-label")
        self.warn_box.append(self.warn_ip)

        # Question
        self.warn_q = Gtk.Label(label="Do you still want to join?")
        self.warn_q.set_xalign(0.0)
        self.warn_box.append(self.warn_q)

        # Buttons row: centered, 20px gap, equal width
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        btn_row.set_halign(Gtk.Align.CENTER)

        self.warn_join_btn = Gtk.Button(label="Join")
        self.warn_join_btn.add_css_class("warning-btn")
        btn_row.append(self.warn_join_btn)

        self.warn_cancel_btn = Gtk.Button(label="Cancel")
        self.warn_cancel_btn.add_css_class("suggested-action")
        self.warn_cancel_btn.add_css_class("warning-btn")
        btn_row.append(self.warn_cancel_btn)

        self.warn_box.append(btn_row)

        # Internal state for blocking confirm
        self._warn_decided = None
        self._warn_loop = None

        def _warn_finish(ok: bool):
            self._warn_decided = bool(ok)
            try:
                self.warn_box.set_visible(False)
                self.warn_scrim.set_visible(False)
            except Exception:
                pass
            try:
                if self._warn_loop:
                    self._warn_loop.quit()
            except Exception:
                pass

        self.warn_cancel_btn.connect("clicked", lambda *_: _warn_finish(False))
        self.warn_join_btn.connect("clicked", lambda *_: _warn_finish(True))

        # ESC closes settings
        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        # startup overlay (extracted)
        build_startup_overlay(self, overlay)

        # MAIN UI
        self.content_root.append(hr())

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.set_hexpand(True)
        root.set_vexpand(True)
        self.content_root.append(root)

        sidebar_frame = build_sidebar(self)
        root.append(sidebar_frame)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)
        self.main_browser_box = main
        root.append(main)

        self.header_widget = HeaderUI(self, self._col_groups).build()
        main.append(self.header_widget)
        main.append(hr())

        self.empty_label = Gtk.Label(label="No Servers Found.")
        self.empty_label.set_xalign(0.0)
        self.empty_label.set_margin_start(12)
        self.empty_label.set_margin_top(10)
        self.empty_label.set_margin_bottom(10)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_visible(False)
        main.append(self.empty_label)

        self.store = Gio.ListStore.new(ServerObject)

        self.combined_filter = Gtk.CustomFilter.new(self._combined_filter_func, None)
        self.filter_model = Gtk.FilterListModel.new(self.store, self.combined_filter)

        self.sorter = Gtk.CustomSorter.new(self._sort_func, None)
        self.sort_model = Gtk.SortListModel.new(self.filter_model, self.sorter)

        self.selection = Gtk.NoSelection.new(self.sort_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_setup_list_item)
        factory.connect("bind", self.on_bind_list_item)

        self.list_view = Gtk.ListView(model=self.selection, factory=factory)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.list_view)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)
        main.append(self.scroller)

        self._server_companion_snapshot = None
        self._server_companion_poll_interval_secs = 15
        self._server_companion_poll_timer_id = 0
        self._server_companion_poll_paused = False
        self._server_companion_poll_inflight = False
        self._server_companion_poll_token = 0
        self._server_companion_last_online = None
        self._pending_server_companion_obj = None
        self._server_companion_restart_alert_enabled = False
        self._server_companion_alert_volume = 80

        self.server_companion_panel = ServerCompanionPanel()
        self.server_companion_panel.set_on_clear(self.clear_server_companion)
        self.server_companion_panel.set_on_play_pause(self.toggle_server_companion_polling)
        self.server_companion_panel.set_on_restart_alert_toggled(self.set_server_companion_restart_alert_enabled)
        self.server_companion_panel.set_on_alert_volume_changed(self.set_server_companion_alert_volume)
        self.server_companion_panel.set_alert_volume(self._server_companion_alert_volume)

        self.server_companion_revealer = Gtk.Revealer()
        self.server_companion_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.server_companion_revealer.set_transition_duration(180)
        self.server_companion_revealer.set_reveal_child(False)
        self.server_companion_revealer.set_hexpand(False)
        self.server_companion_revealer.set_vexpand(True)
        self.server_companion_revealer.set_child(self.server_companion_panel)
        root.append(self.server_companion_revealer)

        self.set_server_companion_visible(bool(self.settings.get("show_server_companion", False)))

        self._update_sort_indicators()
        self._apply_titlebar_counts()
        GLib.idle_add(self._begin_startup_update)

    # ----------------------------
    # Titlebar counts
    # ----------------------------
    def _apply_titlebar_counts(self):
        show_master = bool(self.settings.get("show_counts_in_title_bar", False))
        show_servers = bool(self.settings.get("show_counts_servers_loaded", False))
        show_global = bool(self.settings.get("show_counts_global_players", False))

        if not show_master or (not show_servers and not show_global):
            self.set_title(self._base_title)
            return

        parts = []

        if show_servers:
            try:
                parts.append(f"Servers: {int(self.store.get_n_items())}")
            except Exception:
                parts.append("Servers: 0")

        if show_global:
            if isinstance(getattr(self, "_steam_global_players", None), int):
                parts.append(f"Global Players: {int(self._steam_global_players)}")
            else:
                parts.append("Global Players: --")

        suffix = " / ".join(parts)
        self.set_title(f"{self._base_title} — {suffix}")

    # ----------------------------
    # Steam Global Players
    # ----------------------------
    def _steam_global_players_tick(self):
        if not bool(self.settings.get("show_counts_in_title_bar", False)):
            return True
        if not bool(self.settings.get("show_counts_global_players", True)):
            return True

        def worker():
            val = self._fetch_steam_global_players()
            GLib.idle_add(self._apply_steam_global_players, val)

        try:
            self._executor.submit(worker)
        except Exception:
            pass

        return True

    def _fetch_steam_global_players(self):
        try:
            req = urllib.request.Request(
                STEAM_CURRENT_PLAYERS_URL,
                headers={"User-Agent": "DZLL"},
            )
            with urllib.request.urlopen(req, timeout=3.0) as r:
                raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw)
            resp = data.get("response") if isinstance(data, dict) else None
            if isinstance(resp, dict):
                pc = resp.get("player_count")
                return int(pc)
        except Exception:
            pass
        return None

    def _apply_steam_global_players(self, val):
        if isinstance(val, int) and val >= 0:
            self._steam_global_players = val
        try:
            self._apply_titlebar_counts()
        except Exception:
            pass
        return False

    # ----------------------------
    # Overlay
    # ----------------------------
    def _set_updating(self, is_updating: bool, text: str | None = None):
        if text:
            self.startup_label.set_text(text)
        self.startup_dimmer.set_visible(bool(is_updating))
        try:
            self.startup_spinner.set_spinning(bool(is_updating))
        except Exception:
            pass

    # ----------------------------
    # SteamCMD overlay layout helpers (3-case layout)
    # ----------------------------
    def _on_steamcmd_show_password_toggled(self, btn):
        return self._steamcmd_overlay_ui._on_steamcmd_show_password_toggled(btn)

    def _steamcmd_overlay_render(self, heading: str, line1: str, line2: str, spinning: bool):
        return self._steamcmd_overlay_ui._steamcmd_overlay_render(heading, line1, line2, spinning)

    def _steamcmd_set_state(self, heading: str, line1: str, line2: str, spinning: bool):
        return self._steamcmd_overlay_ui._steamcmd_set_state(heading, line1, line2, spinning)

    def _steamcmd_install_line_from_worker(self, line: str):
        return self._steamcmd_overlay_ui._steamcmd_install_line_from_worker(line)

    def _show_steamcmd_auth_overlay(self, username_prefill: str = "", status: str = ""):
        return self._steamcmd_overlay_ui._show_steamcmd_auth_overlay(username_prefill=username_prefill, status=status)

    def _hide_steamcmd_auth_overlay(self):
        return self._steamcmd_overlay_ui._hide_steamcmd_auth_overlay()

    def _steamcmd_auth_submit(self):
        return self._steamcmd_overlay_ui._steamcmd_auth_submit()

    def _steamcmd_auth_cancel(self):
        return self._steamcmd_overlay_ui._steamcmd_auth_cancel()

    def _request_steamcmd_credentials_blocking(self, username_prefill: str = "", status: str = ""):
        return self._steamcmd_overlay_ui._request_steamcmd_credentials_blocking(username_prefill=username_prefill, status=status)

    # ----------------------------
    # SteamCMD progress helpers
    # ----------------------------
    def _steamcmd_mark_started(self, *a, **kw):
        return self._steamcmd_overlay_ui._steamcmd_mark_started(*a, **kw)

    # ----------------------------
    # SteamCMD Progress Bar Helpers
    # ----------------------------
    def _steamcmd_start_progress_timer(self, *a, **kw):
        return self._steamcmd_overlay_ui._steamcmd_start_progress_timer(*a, **kw)

    def _steamcmd_stop_progress_timer(self, *a, **kw):
        return self._steamcmd_overlay_ui._steamcmd_stop_progress_timer(*a, **kw)

    def _steamcmd_progress_tick(self, *a, **kw):
        return self._steamcmd_overlay_ui._steamcmd_progress_tick(*a, **kw)

    # ----------------------------
    # SteamCMD output parsing -> 3-case layout
    # ----------------------------
    def _steamcmd_line_to_overlay(self, line: str):
        return self._steamcmd_overlay_ui._steamcmd_line_to_overlay(line)

    def _steamcmd_refresh_active_download_line2(self):
        return self._steamcmd_overlay_ui._steamcmd_refresh_active_download_line2()

    # ----------------------------
    # SteamCMD New Run Reset
    # ----------------------------
    def _steamcmd_reset_state_for_new_run(self):
        return self._steamcmd_overlay_ui._steamcmd_reset_state_for_new_run()

    # ----------------------------
    # Map dropdown update
    # ----------------------------
    def _set_map_choices(self, choices: list[str]):
        if not choices:
            choices = ["All"]
        try:
            old_idx = int(self.map_dropdown.get_selected())
        except Exception:
            old_idx = 0
        old_val = self.map_model.get_string(old_idx) if 0 <= old_idx < self.map_model.get_n_items() else "All"

        n = self.map_model.get_n_items()
        self.map_model.splice(0, n, choices)

        new_idx = 0
        for i in range(self.map_model.get_n_items()):
            if self.map_model.get_string(i) == old_val:
                new_idx = i
                break
        try:
            self.map_dropdown.set_selected(new_idx)
        except Exception:
            pass

    # ----------------------------
    # Scroll
    # ----------------------------
    def _scroll_to_top(self):
        try:
            vadj = self.scroller.get_vadjustment()
            if vadj:
                vadj.set_value(0.0)
        except Exception:
            pass
        return False

    def _on_close_request(self, *_args):
        self._shutdown_cleanup()
        return False

    def _shutdown_cleanup(self):
        if getattr(self, "_shutdown_cleanup_done", False):
            return
        self._shutdown_cleanup_done = True

        try:
            self._stop_server_companion_polling()
        except Exception:
            pass

        try:
            self._steamcmd_stop_progress_timer()
        except Exception:
            try:
                tid = int(getattr(self, "_steamcmd_progress_timer_id", 0) or 0)
                if tid:
                    GLib.source_remove(tid)
            except Exception:
                pass
            self._steamcmd_progress_timer_id = 0

        try:
            cancel_event = getattr(self, "_steamcmd_cancel_event", None)
            if isinstance(cancel_event, threading.Event):
                cancel_event.set()
        except Exception:
            pass

        try:
            self._steamcmd_auth_result = {"ok": False, "cancelled": True, "shutdown": True}
            self._steamcmd_auth_request = None
            ev = getattr(self, "_steamcmd_auth_event", None)
            if isinstance(ev, threading.Event):
                ev.set()
            self._steamcmd_auth_event = None
        except Exception:
            pass

        try:
            if getattr(self, "_discord", None):
                self._discord.clear()
                self._discord.disconnect()
        except Exception:
            pass

        for name in ("_executor", "_hi_executor"):
            try:
                executor = getattr(self, name, None)
                if executor:
                    executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    # ----------------------------
    # Server companion panel
    # ----------------------------
    def set_server_companion_visible(self, visible: bool):
        visible = bool(visible)
        if visible:
            try:
                self.main_browser_box.set_size_request(int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1)
            except Exception:
                pass
            self.server_companion_revealer.set_visible(True)
            self.server_companion_revealer.set_reveal_child(True)
            self._start_server_companion_polling()
        else:
            self.server_companion_revealer.set_reveal_child(False)
            self.server_companion_revealer.set_visible(False)
            try:
                self.main_browser_box.set_size_request(-1, -1)
            except Exception:
                pass
            try:
                width = int(WINDOW_DEFAULT_SIZE[0])
                try:
                    height = int(self.get_height())
                except Exception:
                    height = int(WINDOW_DEFAULT_SIZE[1])
                if height <= 0:
                    height = int(WINDOW_DEFAULT_SIZE[1])
                self.set_default_size(width, height)
            except Exception:
                pass
            self._stop_server_companion_polling()

    def toggle_server_companion(self):
        self.set_server_companion_visible(
            not bool(self.server_companion_revealer.get_reveal_child())
        )

    def _server_companion_snapshot_from_obj(self, obj: ServerObject) -> dict:
        try:
            ping = int(getattr(obj, "ping", -1))
        except Exception:
            ping = -1
        try:
            players = int(getattr(obj, "players", 0))
        except Exception:
            players = 0
        try:
            max_players = int(getattr(obj, "max_players", 0))
        except Exception:
            max_players = 0
        try:
            mod_count = int(getattr(obj, "mod_count", 0))
        except Exception:
            mod_count = 0

        mode_parts = []
        mode_parts.append("3PP" if bool(getattr(obj, "third_person", False)) else "1PP")
        mode_parts.append("Password" if bool(getattr(obj, "password", False)) else "No password")
        mode_parts.append(f"Mods: {mod_count}")

        return {
            "name": str(getattr(obj, "name", "") or ""),
            "map": str(getattr(obj, "map_name", "") or ""),
            "ping": ping,
            "players": players,
            "max_players": max_players,
            "time": str(getattr(obj, "time", "") or ""),
            "online": ping >= 0,
            "mode": " / ".join(mode_parts),
            "ip": str(getattr(obj, "ip", "") or ""),
            "qport": int(getattr(obj, "qport", 0) or 0),
        }

    def set_server_companion_server(self, obj: ServerObject):
        snapshot = self._server_companion_snapshot_from_obj(obj)
        self._server_companion_poll_token += 1
        self._server_companion_poll_paused = False
        self._server_companion_last_online = None
        self._server_companion_snapshot = snapshot
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_server_snapshot(snapshot)
            panel.set_polling_paused(False)
        self._start_server_companion_polling()

    def clear_server_companion(self):
        self._server_companion_poll_token += 1
        self._server_companion_snapshot = None
        self._server_companion_last_online = None
        self._stop_server_companion_polling()
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.clear_server()

    def _server_companion_should_poll(self) -> bool:
        return (
            bool(self.settings.get("show_server_companion", False))
            and getattr(self, "server_companion_revealer", None) is not None
            and bool(self.server_companion_revealer.get_reveal_child())
            and getattr(self, "_server_companion_snapshot", None) is not None
            and not bool(getattr(self, "_server_companion_poll_paused", False))
        )

    def _start_server_companion_polling(self):
        if not self._server_companion_should_poll():
            return
        if not self._server_companion_poll_timer_id:
            self._server_companion_poll_timer_id = GLib.timeout_add_seconds(
                self._server_companion_poll_interval_secs,
                self._server_companion_poll_tick,
            )
        self._submit_server_companion_poll()

    def _stop_server_companion_polling(self):
        timer_id = int(getattr(self, "_server_companion_poll_timer_id", 0) or 0)
        if timer_id:
            try:
                GLib.source_remove(timer_id)
            except Exception:
                pass
        self._server_companion_poll_timer_id = 0

    def _server_companion_poll_tick(self):
        if not self._server_companion_should_poll():
            self._server_companion_poll_timer_id = 0
            return False
        self._submit_server_companion_poll()
        return True

    def _submit_server_companion_poll(self):
        if self._server_companion_poll_inflight or not self._server_companion_should_poll():
            return
        snapshot = dict(self._server_companion_snapshot or {})
        ip = str(snapshot.get("ip") or "")
        qport = int(snapshot.get("qport") or 0)
        if not ip or qport <= 0:
            return

        token = int(self._server_companion_poll_token)
        self._server_companion_poll_inflight = True

        def worker():
            info = query_server_live(ip, qport)
            GLib.idle_add(self._apply_server_companion_live_result, token, info)

        try:
            self._hi_executor.submit(worker)
        except Exception:
            self._server_companion_poll_inflight = False

    def _apply_server_companion_live_result(self, token: int, info: dict):
        self._server_companion_poll_inflight = False
        if token != int(getattr(self, "_server_companion_poll_token", 0)):
            return False
        if not self._server_companion_should_poll():
            return False

        snapshot = dict(self._server_companion_snapshot or {})
        prev_online = self._server_companion_last_online
        if bool((info or {}).get("ok", False)):
            snapshot["ping"] = int((info or {}).get("ping_ms", snapshot.get("ping", -1)) or 0)
            snapshot["players"] = int((info or {}).get("players", snapshot.get("players", 0)) or 0)
            snapshot["max_players"] = int((info or {}).get("max_players", snapshot.get("max_players", 0)) or 0)
            live_time = str((info or {}).get("time") or "")
            if live_time:
                snapshot["time"] = live_time
            snapshot["online"] = True
        else:
            snapshot["ping"] = -1
            snapshot["online"] = False

        new_online = bool(snapshot.get("online", False))
        if (
            prev_online is False
            and new_online
            and bool(getattr(self, "_server_companion_restart_alert_enabled", False))
        ):
            self._server_companion_alert_back_online(snapshot)
        self._server_companion_last_online = new_online
        self._server_companion_snapshot = snapshot
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_server_snapshot(snapshot)
            panel.set_polling_paused(False)
        return False

    def toggle_server_companion_polling(self):
        if getattr(self, "_server_companion_snapshot", None) is None:
            return
        self._server_companion_poll_paused = not bool(getattr(self, "_server_companion_poll_paused", False))
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_polling_paused(self._server_companion_poll_paused)
        if self._server_companion_poll_paused:
            self._stop_server_companion_polling()
        else:
            self._start_server_companion_polling()

    def set_server_companion_restart_alert_enabled(self, enabled: bool):
        self._server_companion_restart_alert_enabled = bool(enabled)

    def set_server_companion_alert_volume(self, volume: int):
        try:
            self._server_companion_alert_volume = max(0, min(100, int(volume)))
        except Exception:
            self._server_companion_alert_volume = 80

    def _server_companion_alert_back_online(self, snapshot: dict):
        self._play_server_companion_online_sound()
        self._notify_server_companion_back_online(snapshot)

    def _play_server_companion_online_sound(self):
        try:
            volume = max(0, min(100, int(getattr(self, "_server_companion_alert_volume", 80))))
        except Exception:
            volume = 80
        if volume <= 0:
            return
        audio_path = os.path.join(os.path.dirname(__file__), "audio", "serverOnline.mp3")
        if not os.path.exists(audio_path):
            return
        player = shutil.which("ffplay")
        cmd = None
        if player:
            cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", f"volume={volume / 100:.2f}", audio_path]
        else:
            player = shutil.which("mpg123")
            if player:
                cmd = [player, "-g", str(volume), audio_path]
        if not cmd:
            return
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _notify_server_companion_back_online(self, snapshot: dict):
        try:
            app = self.get_application()
            if app is None:
                return
            notification = Gio.Notification.new("Server back online")
            name = str((snapshot or {}).get("name") or "").strip()
            if name:
                notification.set_body(name)
            app.send_notification("dzll-server-companion-back-online", notification)
        except Exception:
            pass

    # ----------------------------
    # Startup sequence
    # ----------------------------
    def _begin_startup_update(self):
        self._set_updating(True, "Updating The Server Database, Please Wait…")
        self.empty_label.set_visible(False)
        self._startup_db_applied = False
        self._startup_db_provisional = False

        def apply_db_rows_once(rows, ok, provisional=False):
            if getattr(self, "_startup_db_applied", False):
                return False
            if provisional:
                if getattr(self, "_startup_db_provisional", False):
                    return False
                self._startup_db_provisional = True
                return self._apply_db_rows(rows, ok)
            self._startup_db_applied = True
            return self._apply_db_rows(rows, ok)

        def db_watchdog():
            if getattr(self, "_startup_db_applied", False):
                return False
            rows = read_servers_from_db()
            apply_db_rows_once(rows, False, True)
            return False

        GLib.timeout_add_seconds(30, db_watchdog)

        def worker():
            try:
                self._check_updates_worker()
            except Exception:
                pass

            ok = fetch_db_overwrite_local()
            rows = read_servers_from_db()

            # --- Blocklist startup (fail-open) ---
            if bool(self.settings.get("enable_blocklist_filter", True)):
                try:
                    from .db import fetch_bl_overwrite_local
                    fetch_bl_overwrite_local()
                except Exception as e:
                    print(f"[BL] Fetch failed: {e}")

                self._bl_load_local()
            else:
                # Reset ALL v2 sets when disabled
                self._bl_ok = False
                self.bl_ip_hard = set()
                self.bl_allow_exact = set()
                self.bl_soft = set()
                self.bl_hard = set()
                self._bl_soft = set()
                self._bl_hard = set()

            GLib.idle_add(apply_db_rows_once, rows, ok)

        self._executor.submit(worker)
        return False

    def _check_updates_worker(self):
        if not bool(self.settings.get("auto_check_updates", True)):
            return

        forced = os.environ.get("DZLL_FORCE_LATEST_TAG", "").strip()
        if forced:
            self._update_info = {"tag": forced, "url": RELEASES_URL}
            return

        now = int(time.time())
        last = int(self.settings.get("last_update_check_ts", 0) or 0)

        if last and (now - last) < 86400:
            tag = str(self.settings.get("latest_release_tag") or "").strip()
            url = str(self.settings.get("latest_release_url") or RELEASES_URL).strip()
            if tag:
                self._update_info = {"tag": tag, "url": url}
            return

        tag = ""
        url = ""
        try:
            req = urllib.request.Request(GITHUB_LATEST_API, headers={"User-Agent": "DZLL"})
            with urllib.request.urlopen(req, timeout=3.0) as r:
                raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw)
            tag = str(data.get("tag_name") or "").strip()
            url = str(data.get("html_url") or RELEASES_URL).strip()
        except Exception:
            tag = ""
            url = ""

        try:
            self.settings["last_update_check_ts"] = now
            self.settings["latest_release_tag"] = tag
            self.settings["latest_release_url"] = url
            save_settings(self.settings)
        except Exception:
            pass

        if tag:
            self._update_info = {"tag": tag, "url": url}

    def _apply_db_rows(self, rows: list, fetched_ok: bool):
        try:
            choices = map_choices_from_db_rows(rows)
            self._set_map_choices(choices)
        except Exception:
            self._set_map_choices(["All"])

        loaded = self._load_rows_into_store(rows)
        if not loaded:
            msg = "No Servers Found."
            if not fetched_ok:
                msg = "No Servers Found (DB Fetch Failed And No Usable Local DB)."
            self.empty_label.set_text(msg)
            self.empty_label.set_visible(True)
            self._set_updating(False)
            self._apply_titlebar_counts()
            return False

        self._set_updating(True, "Updating The Server Database, Please Wait…")
        if not fetched_ok:
            self._set_updating(False)
            self._apply_titlebar_counts()
            return False
        keys = sorted(self._obj_by_key.keys(), key=self._bm_live_group)
        first_n = min(100, len(keys))
        first_keys = keys[:first_n]
        rest_keys = keys[first_n:]
        self._submit_live_first_n_then_hide_band(first_n, first_keys, rest_keys)

        GLib.timeout_add_seconds(OFFLINE_RECHECK_SECS, self._offline_recheck_tick)
        self._apply_titlebar_counts()
        return False

    # ----------------------------
    # Load rows
    # ----------------------------
    def _load_rows_into_store(self, rows: list) -> bool:
        n = self.store.get_n_items()
        if n:
            self.store.splice(0, n, [])
        self._obj_by_key.clear()

        if not rows:
            return False

        for dbrow in rows:
            ip = (dbrow.get("ip") or "").strip()
            if not ip:
                continue

            try:
                gport = int(dbrow.get("gport"))
            except Exception:
                continue

            try:
                qport = int(dbrow.get("qport"))
            except Exception:
                qport = gport + 1

            name = (dbrow.get("name") or "").strip()
            raw_map = (dbrow.get("map") or "").strip()
            map_name = standardize_map(raw_map)

            try:
                players = int(dbrow.get("players") or 0)
            except Exception:
                players = 0
            try:
                maxp = int(dbrow.get("maxPlayers") or 0)
            except Exception:
                maxp = 0

            password = bool(int(dbrow.get("password") or 0))
            third_person = bool(int(dbrow.get("third_person") or 0))

            mods_json = dbrow.get("mods") or ""
            mod_count_db = dbrow.get("modCount")
            cnt, preview = parse_mods_preview(mods_json, max_names=8)
            if mod_count_db is not None:
                try:
                    cnt = int(mod_count_db)
                except Exception:
                    pass

            try:
                timewarp = float(dbrow.get("timeWarp")) if dbrow.get("timeWarp") is not None else 1.0
            except Exception:
                timewarp = 1.0
            time_str = (dbrow.get("time") or "").strip()
            if not is_valid_hhmm(time_str):
                time_str = "--:--"

            country = (dbrow.get("country") or "").strip()
            try:
                ping_db = int(dbrow.get("ping")) if dbrow.get("ping") is not None else -1
            except Exception:
                ping_db = -1
            try:
                bm_rank = int(dbrow.get("bm_rank")) if dbrow.get("bm_rank") is not None else 999999999
            except Exception:
                bm_rank = 999999999

            k = fav_key(ip, gport)
            fav = bool(self.favorites.get(k, False))

            lp_ts = self.last_played.get(k)
            played_disp = human_last_played(lp_ts) if lp_ts else ""

            obj = ServerObject(
                fav=fav,
                password=password,
                third_person=third_person,
                name=name,
                country=country,
                ip=ip,
                gport=gport,
                qport=qport,
                mod_count=cnt,
                mods_preview=preview,
                mods_json=mods_json,
                time=time_str,
                timewarp=timewarp,
                played=played_disp,
                map_name=map_name,
                players=players,
                max_players=maxp,
                ping=ping_db,
                bm_rank=bm_rank,
            )

            self.store.append(obj)
            self._obj_by_key[k] = obj
            self.live.setdefault(k, {"hide_high_ping": False, "offline": (ping_db < 0)})

        self._snapshot_all_sort_keys()
        self._on_filter_changed()
        try:
            self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        except Exception:
            pass

        self._apply_titlebar_counts()

        GLib.timeout_add_seconds(1, self._auto_sort_lowest_ping_after_startup)

        # ---- Init Discord Status ----
        try:
            if getattr(self, "_discord", None):
                self._discord.set_menu()
        except Exception:
            pass
        # -----------------------------

        return self.store.get_n_items() > 0

    # ----------------------------
    # Snapshot sort keys
    # ----------------------------
    def _snapshot_row_sort_keys(self, obj: ServerObject, now_ts: int):
        try:
            p = int(obj.ping)
        except Exception:
            p = -1
        obj.sort_ping = (PING_MAX + 999) if p < 0 else p

        try:
            obj.sort_players = int(obj.players)
        except Exception:
            obj.sort_players = 0

        k = fav_key(obj.ip, obj.gport)
        ts = self.last_played.get(k)
        if not ts:
            obj.sort_played_days = 999999
        else:
            try:
                obj.sort_played_days = max(0, int((now_ts - int(ts)) // 86400))
            except Exception:
                obj.sort_played_days = 999999

    def _snapshot_all_sort_keys(self):
        now_ts = int(time.time())
        n = self.store.get_n_items()
        for i in range(n):
            obj = self.store.get_item(i)
            if isinstance(obj, ServerObject):
                self._snapshot_row_sort_keys(obj, now_ts)

    # ----------------------------
    # Live refresh
    # ----------------------------
    def _bm_live_group(self, k) -> int:
        obj = self._obj_by_key.get(k)
        try:
            rank = int(getattr(obj, "bm_rank", 999999999))
        except Exception:
            return 3
        return 0 if rank <= 100 else (1 if rank <= 1000 else (2 if rank <= 2000 else 3))

    def _submit_startup_rest_batches(self, keys):
        for i in range(0, len(keys or []), BATCH_SIZE):
            self._submit_live_batch(keys[i:i + BATCH_SIZE], reason="startup-batch")
        return False

    def _submit_live_first_n_then_hide_band(self, n: int, ordered_keys=None, rest_keys=None):
        keys = list(ordered_keys or self._obj_by_key.keys())[:max(0, int(n))]
        if not keys:
            self._set_updating(False)
            GLib.idle_add(self._submit_startup_rest_batches, rest_keys)
            return

        def query_one(k):
            try:
                obj = self._obj_by_key.get(k)
                if not obj:
                    return None
                return (k, query_server_live(obj.ip, obj.qport))
            except Exception as e:
                return (k, {"ok": False, "err": str(e)})

        def apply_late_result(fut):
            try:
                result = fut.result()
                if result:
                    GLib.idle_add(self._apply_live_results, [result], "startup-first")
            except Exception:
                pass

        def worker():
            futures = [self._executor.submit(query_one, k) for k in keys]
            target = max(1, (len(futures) * 65 + 99) // 100)
            deadline = time.monotonic() + 6.0
            done = set()
            pending = set(futures)
            while pending and len(done) < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                newly_done, pending = wait(pending, timeout=min(0.2, remaining))
                done.update(newly_done)
            results = []
            for fut in done:
                try:
                    result = fut.result()
                    if result:
                        results.append(result)
                except Exception:
                    pass
            for fut in pending:
                fut.add_done_callback(apply_late_result)
            GLib.idle_add(self._apply_live_results_and_hide_band, results)
            GLib.idle_add(self._submit_startup_rest_batches, rest_keys)

        self._executor.submit(worker)

    def _submit_live_batch(self, keys, reason="batch"):
        if not keys:
            return

        def worker():
            results = []
            for k in keys:
                obj = self._obj_by_key.get(k)
                if not obj:
                    continue
                info = query_server_live(obj.ip, obj.qport)
                results.append((k, info))
            GLib.idle_add(self._apply_live_results, results, reason)

        self._executor.submit(worker)

    def _offline_recheck_tick(self):
        now = int(time.time())
        offline_keys = []

        for k, obj in self._obj_by_key.items():
            if k in self._dead_session:
                continue
            try:
                if int(obj.ping) < 0:
                    d = self.dead.get(k)
                    if d and int(d.get("fail_count", 0) or 0) >= DEAD_MAX_FAILS:
                        self._dead_session.add(k)
                        continue
                    if d and int(d.get("dead_until", 0)) > now:
                        continue
                    offline_keys.append(k)
            except Exception:
                continue

        if not offline_keys:
            self._on_filter_changed()
            self._apply_titlebar_counts()
            return True

        self._submit_live_batch(offline_keys[:100], reason="offline-recheck")
        return True

    def _apply_live_results_and_hide_band(self, results):
        self._apply_live_results(results, reason="startup-first")
        self._set_updating(False)
        GLib.idle_add(self.update_ui.maybe_show)
        self._apply_titlebar_counts()
        return False

    def _apply_live_results(self, results, reason="batch"):
        now = int(time.time())
        changed_any = False
        trigger_filter = (reason != "manual-refresh")
        manual_success = False
        startup_live = reason in ("startup-first", "startup-batch")

        for k, info in (results or []):
            obj = self._obj_by_key.get(k)
            if not obj:
                continue

            if not info or not info.get("ok"):
                obj.ping = -1
                self.live.setdefault(k, {})["offline"] = True

                d = self.dead.get(k, {"fail_count": 0, "dead_until": 0, "last_fail": 0})
                d["last_fail"] = now

                if reason == "offline-recheck":
                    d["fail_count"] = int(d.get("fail_count", 0) or 0) + 1
                    if d["fail_count"] >= DEAD_MAX_FAILS:
                        d["fail_count"] = DEAD_MAX_FAILS
                        self._dead_session.add(k)

                d["dead_until"] = 0
                self.dead[k] = d
                try:
                    save_dead_cache(self.dead)
                except Exception:
                    pass

                changed_any = True
                continue

            ping_ms = int(info.get("ping_ms", 0) or 0)
            players = int(info.get("players", obj.players) or 0)
            maxp = int(info.get("max_players", obj.max_players) or 0)
            t = (info.get("time") or "").strip()
            obj.time = t if is_valid_hhmm(t) else "--:--"

            obj.players = players
            obj.max_players = maxp
            try:
                obj.password = bool(info.get("password", obj.password))
            except Exception:
                pass

            obj.ping = ping_ms
            self.live.setdefault(k, {})["offline"] = False

            if trigger_filter:
                cut = int(getattr(self, "_ping_cutoff_ms", 250) or 250)
                self.live.setdefault(k, {})["hide_high_ping"] = (ping_ms > cut)

            if k in self.dead:
                self.dead.pop(k, None)
                try:
                    save_dead_cache(self.dead)
                except Exception:
                    pass

            if k in self._dead_session:
                self._dead_session.discard(k)

            changed_any = True
            manual_success = (reason == "manual-refresh")

        if changed_any and (trigger_filter or manual_success) and not startup_live:
            self._on_filter_changed()

        self._apply_titlebar_counts()
        return False

    def _is_likely_test_server_name(self, name: str) -> bool:
        s = str(name or "").strip().lower()
        if not s:
            return False

        # Normalize separators/punctuation to spaces
        s = re.sub(r"[_\-\.\[\]\(\)#*/!]+", " ", s)

        # Split joined alpha/number boundaries: test2 -> test 2, dev1 -> dev 1
        s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
        s = re.sub(r"(\d)([a-z])", r"\1 \2", s)

        # Collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            return False

        tokens = set(s.split())

        for marker in TEST_SERVER_MARKERS:
            m = marker.strip().lower()
            if not m:
                continue

            if " " in m:
                if m in s:
                    return True
            else:
                if m in tokens:
                    return True

                # Allow marker glued to start/end of a token, but not buried in the middle
                for tok in tokens:
                    if tok.startswith(m) or tok.endswith(m):
                        return True

        return False

# ==== MAIN.PY PART 3 ==== #
    # ----------------------------
    # Filters
    # ----------------------------
    def _combined_filter_func(self, obj, _data=None) -> bool:
        if not obj:
            return False

        k = fav_key(obj.ip, obj.gport)
        is_fav = bool(obj.fav)
        now = int(time.time())

        if (not is_fav) and k in self._dead_session:
            return False

        d = self.dead.get(k)
        if (not is_fav) and d and int(d.get("dead_until", 0)) > now:
            return False

        live = self.live.get(k)
        if (not is_fav) and live and bool(live.get("hide_high_ping", False)):
            return False

        q = (self.search_entry.get_text() or "").strip().lower()
        nm = (obj.name or "").strip().lower()
        ipport = f"{obj.ip}:{obj.gport}".lower()

        # --- Blocklist v2 filtering (authoritative priority) ---
        if bool(self.settings.get("enable_blocklist_filter", True)) and bool(getattr(self, "_bl_ok", False)):
            status = self._bl_status(ipport)

            # Hide blocked servers from normal list; allow exact-search reveal only
            if status in ("ip_hard", "hard", "soft"):
                if q != ipport:
                    return False

        if (not is_fav) and bool(self.settings.get("hide_test_servers", True)) and self._is_likely_test_server_name(nm):
            return False

        try:
            max_players_cutoff = int(self.settings.get("hide_below_max_players", 0) or 0)
        except Exception:
            max_players_cutoff = 0
        if (not is_fav) and max_players_cutoff > 0 and int(obj.max_players) < max_players_cutoff:
            return False

        if self.cb_show_fav.get_active() and not bool(obj.fav):
            return False

        # Mutual exclusion: 1PP Only vs 3PP Only (avoid empty/contradictory state)
        try:
            if self.cb_1pp_only.get_active() and self.cb_3pp_only.get_active():
                # Prefer the most recently clicked? We don't have that signal here,
                # so default: uncheck 3PP if 1PP is enabled.
                self.cb_3pp_only.set_active(False)
        except Exception:
            pass
        # 1PP Only: reject servers that allow 3PP
        if self.cb_1pp_only.get_active() and bool(obj.third_person):
            return False
        # 3PP Only: reject servers that do NOT allow 3PP
        if self.cb_3pp_only.get_active() and (not bool(obj.third_person)):
            return False


        if self.cb_no_password.get_active() and bool(obj.password):
            return False
        if self.cb_online_only.get_active():
            try:
                if int(obj.ping) < 0:
                    return False
            except Exception:
                return False
        if self.cb_played_only.get_active():
            if not (obj.played or "").strip():
                return False

        try:
            sel_idx = int(self.map_dropdown.get_selected())
        except Exception:
            sel_idx = 0
        selected_map = self.map_model.get_string(sel_idx) if 0 <= sel_idx < self.map_model.get_n_items() else "All"
        if selected_map != "All":
            if (obj.map_name or "") != selected_map:
                return False

        if q:
            if q not in nm and q not in ipport:
                return False

        return True

    def _on_filter_changed(self, *_args, scroll: bool = False):
        try:
            self.combined_filter.changed(Gtk.FilterChange.DIFFERENT)
        except Exception:
            pass
        if scroll:
            GLib.idle_add(self._scroll_to_top)

    def _on_reset_clicked(self, *_args):
        try:
            self.search_entry.set_text("")
        except Exception:
            pass
        try:
            self.map_dropdown.set_selected(0)
        except Exception:
            pass
        for cb in (self.cb_show_fav, self.cb_1pp_only, self.cb_3pp_only, self.cb_no_password, self.cb_online_only, self.cb_played_only):
            try:
                cb.set_active(False)
            except Exception:
                pass
        self._on_filter_changed(scroll=True)

    # ----------------------------
    # Sorting
    # ----------------------------
    def _sort_func(self, a: ServerObject, b: ServerObject, _data=None):
        def cmp_int(x: int, y: int) -> int:
            return -1 if x < y else (1 if x > y else 0)

        def cmp_str(x: str, y: str) -> int:
            x = (x or "").lower()
            y = (y or "").lower()
            return -1 if x < y else (1 if x > y else 0)

        def bm_group(obj: ServerObject) -> int:
            try:
                rank = int(getattr(obj, "bm_rank", 999999999))
            except Exception:
                return 3
            return 0 if rank <= 100 else (1 if rank <= 1000 else (2 if rank <= 2000 else 3))

        ga = bm_group(a)
        gb = bm_group(b)
        c = cmp_int(ga, gb)

        if c != 0:
            return Gtk.Ordering.SMALLER if c < 0 else Gtk.Ordering.LARGER

        if self.sort_key == "ping":
            ka = int(getattr(a, "sort_ping", 999999))
            kb = int(getattr(b, "sort_ping", 999999))
            c = cmp_int(ka, kb)
            if not self.sort_asc:
                c = -c
        elif self.sort_key == "players":
            ka = int(getattr(a, "sort_players", 0))
            kb = int(getattr(b, "sort_players", 0))
            c = cmp_int(ka, kb)
            if not self.sort_asc:
                c = -c
        elif self.sort_key == "played":
            ka = int(getattr(a, "sort_played_days", 999999))
            kb = int(getattr(b, "sort_played_days", 999999))
            c = cmp_int(ka, kb)
            if not self.sort_asc:
                c = -c
        else:
            c = 0

        if c == 0:
            c = cmp_str(a.name, b.name)
        if c == 0:
            c = cmp_str(a.ip, b.ip)
        if c == 0:
            c = cmp_int(int(a.gport), int(b.gport))

        if c == 0:
            return Gtk.Ordering.EQUAL
        return Gtk.Ordering.SMALLER if c < 0 else Gtk.Ordering.LARGER

    def _set_sort(self, key: str, user_initiated: bool = True):
        if key not in self.SORT_KEYS:
            return
        if user_initiated:
            self._sort_changed_by_user = True
        if self.sort_key == key:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_key = key
            self.sort_asc = True

        self._snapshot_all_sort_keys()
        try:
            self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        except Exception:
            pass

        self._update_sort_indicators()
        GLib.idle_add(self._scroll_to_top)

    def _auto_sort_lowest_ping_after_startup(self):
        if self._sort_changed_by_user or self.sort_key != "ping" or self.sort_asc:
            return False

        self._set_sort("ping", user_initiated=False)
        return False

    def _update_sort_indicators(self):
        def with_arrow(base: str, key: str) -> str:
            if self.sort_key != key:
                return base
            return f"{base} {'▲' if self.sort_asc else '▼'}"

        self.hdr_time_lbl.set_text("TIME")
        self.hdr_map_lbl.set_text("MAP")
        self.hdr_played_lbl.set_text(with_arrow("PLAYED", "played"))
        self.hdr_players_lbl.set_text(with_arrow("PLAYERS", "players"))
        self.hdr_ping_lbl.set_text(with_arrow("PING", "ping"))

    # ----------------------------
    # List factory
    # ----------------------------
    def on_setup_list_item(self, _factory, list_item: Gtk.ListItem):
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        row = ServerRowWidget(
            self._col_groups,
            self._toggle_favorite_for_obj,
            self._refresh_server_for_obj,
            self._join_server_for_obj,
        )
        container.append(row)
        container.append(hr())
        list_item.set_child(container)

    def on_bind_list_item(self, _factory, list_item: Gtk.ListItem):
        obj = list_item.get_item()
        container = list_item.get_child()
        if not isinstance(obj, ServerObject):
            return
        if not isinstance(container, Gtk.Box):
            return
        row = container.get_first_child()
        if isinstance(row, ServerRowWidget):
            row.bind(obj)

    # ----------------------------
    # Favorites
    # ----------------------------
    def _toggle_favorite_for_obj(self, obj: ServerObject):
        k = fav_key(obj.ip, obj.gport)
        obj.fav = not bool(obj.fav)
        if obj.fav:
            self.favorites[k] = True
        else:
            self.favorites.pop(k, None)
        try:
            save_favorites(self.favorites)
        except Exception:
            pass
        self._on_filter_changed()

    # ----------------------------
    # Manual Refresh
    # ----------------------------
    def _refresh_server_for_obj(self, obj: ServerObject):
        k = fav_key(obj.ip, obj.gport)

        now = time.time()
        last = float(self._refresh_rl.get(k, 0.0) or 0.0)
        if (now - last) < REFRESH_RATE_LIMIT_SECS:
            return
        self._refresh_rl[k] = now

        def worker():
            info = query_server_live(obj.ip, obj.qport)
            GLib.idle_add(self._apply_live_results, [(k, info)], "manual-refresh")

        self._hi_executor.submit(worker)

    # ----------------------------
    # Prepare to Join Server
    # ----------------------------
    def _steam_launch_prefix(self) -> list[str]:
        # Native-only for now.
        steam_cmd = shutil.which("steam") or "steam"
        print(f"[JOIN] Using Steam command: {steam_cmd}")
        return [steam_cmd, "-applaunch", "221100", "--"]

    def _get_dayz_proton_prefix(self) -> str:
        return os.path.expanduser("~/.local/share/Steam/steamapps/compatdata/221100/pfx")

    def _free_bytes_for_path(self, path: str) -> int:
        try:
            st = os.statvfs(path)
            return int(st.f_bavail) * int(st.f_frsize)
        except Exception:
            return 0

    def _get_dzll_watch_folder_linux(self) -> str:
        return os.path.join(self._get_dayz_proton_prefix(), "drive_c", "users", "steamuser", "DZLLMods")

    def _launch_direct_steam_url(self, obj: ServerObject, mod_win_paths=None):
        result = launch_direct_steam_url(self, obj, mod_win_paths=mod_win_paths)
        if result is False:
            if getattr(self, "_pending_server_companion_obj", None) is obj:
                self._pending_server_companion_obj = None
            return result
        self._start_dayz_session_watch()
        return result

    # ----------------------------
    # Watch Steam Game State
    # ----------------------------
    def _start_dayz_session_watch(self) -> None:
        try:
            with self._discord_watch_lock:
                if self._discord_watch_active:
                    return
        except Exception:
            pass
        try:
            threading.Thread(target=self._watch_dayz_session_until_exit, daemon=True).start()
        except Exception:
            pass

    def _discord_watch_dayz_until_exit(self) -> None:
        self._watch_dayz_session_until_exit()

    def _watch_dayz_session_until_exit(self) -> None:
        """
        Watch for actual DayZ game process (launcher may remain open).
        When the game starts, activate Companion and update Discord if available.
        When the game exits, reset Discord back to menus.

        Edge case handled:
        - if the DayZ Launcher appears
        - but the actual game never starts
        - and the launcher is then closed
        -> reset Discord back to menus
        """

        # Prevent multiple watcher threads
        try:
            with self._discord_watch_lock:
                if self._discord_watch_active:
                    return
                self._discord_watch_active = True
        except Exception:
            # If locking fails for any reason, fail-open (still try to run)
            pass

        try:
            def _game_running() -> bool:
                try:
                    for pat in ("DayZ_x64.exe", "DayZ.exe"):
                        r = subprocess.run(
                            ["pgrep", "-fa", pat],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                        if r.returncode == 0:
                            return True
                    return False
                except Exception:
                    return False

            def _launcher_running() -> bool:
                try:
                    # Match practical launcher name variants under Wine/Proton
                    for pat in ("DayZ Launcher", "DayZLauncher", "Launcher"):
                        r = subprocess.run(
                            ["pgrep", "-fa", pat],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                        if r.returncode == 0:
                            return True
                    return False
                except Exception:
                    return False

            saw_launcher = False
            saw_game = False

            # Wait up to 120s for the game to appear, while also tracking launcher presence.
            t0 = time.time()
            while (time.time() - t0) < 120.0:
                if _launcher_running():
                    saw_launcher = True

                if _game_running():
                    saw_game = True
                    break

                # Launcher was opened, game never started, and launcher is now gone
                if saw_launcher and not _launcher_running():
                    try:
                        if getattr(self, "_discord", None):
                            GLib.idle_add(self._discord.set_menu)
                    except Exception:
                        pass
                    self._pending_server_companion_obj = None
                    return

                time.sleep(1.0)

            # If the game never appeared, reset presence back to menus
            if not _game_running():
                try:
                    if getattr(self, "_discord", None):
                        GLib.idle_add(self._discord.set_menu)
                except Exception:
                    pass
                self._pending_server_companion_obj = None
                return

            # GAME STARTED -> now count it as "played"
            try:
                obj = getattr(self, "_pending_last_played_obj", None)
                if obj is not None:
                    k = fav_key(obj.ip, obj.gport)
                    ts = int(time.time())
                    self.last_played[k] = ts
                    obj.played = human_last_played(ts)
                    try:
                        save_last_played(self.last_played)
                    except Exception:
                        pass
                    self._pending_last_played_obj = None
            except Exception:
                pass

            # GAME STARTED -> activate Companion for the pending join target
            try:
                obj = getattr(self, "_pending_server_companion_obj", None)
                if obj is not None:
                    self._pending_server_companion_obj = None
                    GLib.idle_add(self.set_server_companion_server, obj)
            except Exception:
                self._pending_server_companion_obj = None

            # GAME STARTED -> set Discord "playing" state according to user setting
            try:
                if getattr(self, "_discord", None):
                    detail = str(self.settings.get("discord_detail_level", "menus") or "menus").strip().lower()

                    if detail == "menus":
                        GLib.idle_add(self._discord.set_menu)

                    elif detail == "ingame":
                        def _set_ingame():
                            try:
                                self._discord._mode = "ingame"
                                self._discord.update()
                            except Exception:
                                pass
                            return False

                        GLib.idle_add(_set_ingame)

                    else:
                        # detail == "server" (privacy handled inside discord_rpc.py)
                        def _set_server_from_info():
                            try:
                                info = self._discord_last_join or {}
                                self._discord.set_server(
                                    server_name=str(info.get("server_name") or ""),
                                    server_map=str(info.get("server_map") or ""),
                                    players="",
                                    ip_port=str(info.get("ip_port") or ""),
                                )
                            except Exception:
                                pass
                            return False

                        GLib.idle_add(_set_server_from_info)
            except Exception:
                pass

            # Wait until the actual game exits
            while _game_running():
                time.sleep(5.0)

            # GAME EXITED -> reset presence
            try:
                if getattr(self, "_discord", None):
                    GLib.idle_add(self._discord.set_menu)
            except Exception:
                pass

        finally:
            # Always release the watcher flag
            try:
                with self._discord_watch_lock:
                    self._discord_watch_active = False
            except Exception:
                pass

    # ----------------------------
    # Join server
    # ----------------------------
    def _resolve_join_mods(self, obj: ServerObject):
        raw_mods_json = getattr(obj, "mods_json", "") or ""
        server_mods = parse_mods_from_db(raw_mods_json)
        print(f"[JOIN] server mods parsed: {len(server_mods)}")

        extra_ids = parse_additional_mod_ids(str(self.settings.get("additional_mod_ids") or ""))
        mods = merge_mod_lists_with_additional(server_mods, extra_ids)
        print(f"[JOIN] total mods after extras: {len(mods)}")
        return mods

    def _resolve_join_runtime(self, mods):
        workshop_dir = str(self.settings.get("workshop_dir") or "").strip()
        if not workshop_dir:
            workshop_dir = autodetect_workshop_dir() or ""

        steamcmd_path = str(self.settings.get("steamcmd_path") or "").strip()
        if not steamcmd_path:
            steamcmd_path = autodetect_steamcmd_path() or ""
            print(f"[JOIN][DEBUG] workshop_dir={workshop_dir!r}")
            print(f"[JOIN][DEBUG] steamcmd_path={steamcmd_path!r}")

        steam_user = str(self.settings.get("steamcmd_username") or "").strip()
        validate = bool(self.settings.get("verify_mod_files", False))
        dry = bool(self.settings.get("steamcmd_dry_run", False))  # kept for legacy; UI removed

        proton_prefix = self._get_dayz_proton_prefix()
        watch_folder_linux = self._get_dzll_watch_folder_linux()

        use_steamcmd = bool(self.settings.get("enable_steamcmd_mod_handling", True))

        # New split toggles (with backwards compatibility to old combined toggle)
        auto_install_missing = bool(self.settings.get("auto_install_missing_mods", True))
        auto_update_required = bool(self.settings.get("auto_update_required_mods", False))
        if ("auto_install_missing_mods" not in self.settings) and ("auto_update_required_mods" not in self.settings):
            legacy = bool(self.settings.get("auto_install_update_mods", True))
            auto_install_missing = legacy
            auto_update_required = legacy

        return {
            "workshop_dir": workshop_dir,
            "steamcmd_path": steamcmd_path,
            "steam_user": steam_user,
            "validate": validate,
            "dry": dry,
            "proton_prefix": proton_prefix,
            "watch_folder_linux": watch_folder_linux,
            "use_steamcmd": use_steamcmd,
            "auto_install_missing": auto_install_missing,
            "auto_update_required": auto_update_required,
        }

    def _join_server_for_obj(self, obj: ServerObject):
        # --- Preflight hard-block warning (MUST run before SteamCMD/mods) ---
        # Run on GTK main thread and block until the user decides.
        if not self._preflight_block_warning_ui_blocking(obj):
            self._on_filter_changed()
            return

        self._pending_server_companion_obj = obj

        # Defer "last played" until DayZ process is actually detected.
        try:
            self._pending_last_played_obj = obj
        except Exception:
            self._pending_last_played_obj = None

        raw_mods_json = getattr(obj, "mods_json", "") or ""
        server_mods = parse_mods_from_db(raw_mods_json)
        print(f"[JOIN] server mods parsed: {len(server_mods)}")

        extra_ids = parse_additional_mod_ids(str(self.settings.get("additional_mod_ids") or ""))
        mods = merge_mod_lists_with_additional(server_mods, extra_ids)
        print(f"[JOIN] total mods after extras: {len(mods)}")

        if not mods:
            try:
                proton_prefix = self._get_dayz_proton_prefix()
                watch_folder_linux = self._get_dzll_watch_folder_linux()

                installed_mods_for_local = scan_installed_mods_in_watch_folder(watch_folder_linux)
                paths = bootstrap_launcher_state(
                    proton_prefix=proton_prefix,
                    watch_folder_linux=watch_folder_linux,
                    installed_mod_linux_paths=installed_mods_for_local,
                    selected_mod_linux_paths=[],
                )
                print(f"[JOIN] launcher state cleared for no-mod server: {paths}")
            except Exception as e:
                print(f"[JOIN] failed to clear launcher state for no-mod server: {e}")
                if getattr(self, "_pending_server_companion_obj", None) is obj:
                    self._pending_server_companion_obj = None
                return

            self._launch_direct_steam_url(obj)
            self._on_filter_changed()
            return

        workshop_dir = str(self.settings.get("workshop_dir") or "").strip()
        if not workshop_dir:
            workshop_dir = autodetect_workshop_dir() or ""

        steamcmd_path = str(self.settings.get("steamcmd_path") or "").strip()
        if not steamcmd_path:
            steamcmd_path = autodetect_steamcmd_path() or ""

        steam_user = str(self.settings.get("steamcmd_username") or "").strip()
        validate = bool(self.settings.get("verify_mod_files", False))
        dry = bool(self.settings.get("steamcmd_dry_run", False))  # kept for legacy; UI removed

        proton_prefix = self._get_dayz_proton_prefix()
        watch_folder_linux = self._get_dzll_watch_folder_linux()

        use_steamcmd = bool(self.settings.get("enable_steamcmd_mod_handling", True))

        # New split toggles (with backwards compatibility to old combined toggle)
        auto_install_missing = bool(self.settings.get("auto_install_missing_mods", True))
        auto_update_required = bool(self.settings.get("auto_update_required_mods", False))
        if ("auto_install_missing_mods" not in self.settings) and ("auto_update_required_mods" not in self.settings):
            legacy = bool(self.settings.get("auto_install_update_mods", True))
            auto_install_missing = legacy
            auto_update_required = legacy

        def do_prepare_and_launch():
            return join_prepare_and_launch(
                self,
                obj,
                mods,
                workshop_dir,
                steamcmd_path,
                steam_user,
                validate,
                dry,
                proton_prefix,
                watch_folder_linux,
                use_steamcmd,
                auto_install_missing,
                auto_update_required,
            )

        self._set_updating(True, "Preparing Mods And Launcher State, Please Wait…")
        self._hi_executor.submit(do_prepare_and_launch)

    # ----------------------------
    # Preflight Hard Block Warning Queue Jump (kept; not used currently)
    # ----------------------------
    def _preflight_block_warning_ui_blocking(self, obj) -> bool:
        return preflight_block_warning_ui_blocking(self, obj)

    # ----------------------------
    # Preflight Hard Block Warning
    # ----------------------------
    def _preflight_block_warning(self, obj) -> bool:
        return preflight_block_warning(self, obj)

    # ----------------------------
    # Hard Block Warning
    # ----------------------------
    def _confirm_blocked_server(self, ip_port: str) -> bool:
        """
        Returns True if user confirms Join, False otherwise.
        Uses the custom warning overlay card (scrim + centered card).
        Blocks via nested GLib.MainLoop, but UI remains responsive.
        fail-open preserved.
        """
        try:
            # Populate
            try:
                self.warn_ip.set_text(ip_port)
            except Exception:
                pass

            # Show
            self._warn_decided = None
            self._warn_loop = GLib.MainLoop()

            try:
                self.warn_scrim.set_visible(True)
                self.warn_box.set_visible(True)
            except Exception:
                pass

            # Focus default action
            try:
                self.warn_join_btn.grab_focus()
            except Exception:
                pass

            # Block until user clicks
            self._warn_loop.run()

            decided = self._warn_decided
            self._warn_loop = None

            # If somehow undecided, treat as cancel (safer)
            if decided is None:
                return False
            return bool(decided)

        except Exception:
            # fail-open
            return True

    # ----------------------------
    # Dead cache prune / clamp
    # ----------------------------
    def _prune_expired_dead(self):
        now = int(time.time())
        changed = False
        for k in list(self.dead.keys()):
            d = self.dead.get(k, {})
            dead_until = int(d.get("dead_until", 0) or 0)
            if dead_until and dead_until < (now - 7 * 86400):
                self.dead.pop(k, None)
                changed = True
        if changed:
            try:
                save_dead_cache(self.dead)
            except Exception:
                pass

    def _clamp_dead_cache(self):
        now = int(time.time())
        cutoff = now - (30 * 86400)
        changed = False

        for k in list(self.dead.keys()):
            d = self.dead.get(k, {})
            try:
                lf = int(d.get("last_fail", 0) or 0)
            except Exception:
                lf = 0

            if lf and lf < cutoff:
                self.dead.pop(k, None)
                changed = True
                continue

            try:
                fc = int(d.get("fail_count", 0) or 0)
            except Exception:
                fc = 0

            if fc > DEAD_MAX_FAILS:
                d["fail_count"] = DEAD_MAX_FAILS
                self.dead[k] = d
                changed = True

        if changed:
            try:
                save_dead_cache(self.dead)
            except Exception:
                pass

    # ----------------------------
    # Settings helpers (required by settings_ui.py)
    # ----------------------------
    def _set_widget_sensitive(self, *a, **kw):
        return self._settings_ui._set_widget_sensitive(*a, **kw)

    def _bl_normalize_key(self, ip: str, port) -> str:
        return bl_normalize_key(ip, port)

    def _bl_load_local(self) -> None:
        data = bl_load_local()
        self.bl_ip_hard = data["bl_ip_hard"]
        self.bl_allow_exact = data["bl_allow_exact"]
        self.bl_soft = data["bl_soft"]
        self.bl_hard = data["bl_hard"]
        self._bl_ok = data["_bl_ok"]
        self._bl_soft = data["_bl_soft"]
        self._bl_hard = data["_bl_hard"]

    def _bl_status(self, key_lc: str) -> str:
        return bl_status(
            key_lc,
            getattr(self, "bl_allow_exact", set()),
            getattr(self, "bl_ip_hard", set()),
            getattr(self, "bl_hard", set()),
            getattr(self, "bl_soft", set()),
        )

    def _apply_setting_runtime_effects(self, *a, **kw):
        return self._settings_ui._apply_setting_runtime_effects(*a, **kw)

    def _clear_played_history(self, *a, **kw):
        return self._settings_ui._clear_played_history(*a, **kw)

    # ----------------------------
    # Settings panel open/close
    # ----------------------------
    def _open_settings_panel(self, *a, **kw):
        return self._settings_ui._open_settings_panel(*a, **kw)

    def _close_settings_panel(self, *a, **kw):
        return self._settings_ui._close_settings_panel(*a, **kw)

    def _toggle_settings_panel(self, *a, **kw):
        return self._settings_ui._toggle_settings_panel(*a, **kw)

    def _on_settings_clicked(self, *a, **kw):
        return self._settings_ui._on_settings_clicked(*a, **kw)

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state):
        try:
            if keyval == Gdk.KEY_Escape and self._settings_open:
                self._close_settings_panel()
                return True
        except Exception:
            pass
        return False
