# ==== MAIN.PY PART 1 ==== #

#!/usr/bin/env python3
import os
import json
import time
import subprocess
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
import threading
import re
import gi
import shutil
import weakref
from pathlib import Path
import sys

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Pango, GLib, Gdk, Graphene
from .discord_rpc import DiscordRPC
from .steam_native import (
    dayz_compatdata_dir,
    dayz_paths_summary,
    dayz_workshop_content_dir,
    is_native_steam_running,
    resolve_native_steam_cmd,
)
from .config import (
    APP_ID,
    WINDOW_DEFAULT_SIZE,
    LOGO_WIDTH_RATIO,
    LOGO_MIN_HEIGHT,
    DIVIDER_COLOR,
    SIDEBAR_WIDTH,
    SIDEBAR_INNER_PADDING,
    DISCLAIMER_GAP_ABOVE,
    DISCLAIMER_COLOR,
    PING_MAX,
    STARTUP_PING_FIRST_N,
    STARTUP_LIVE_REST_WORKERS,
    STARTUP_LIVE_REST_TIMEOUT_SECS,
    STARTUP_LIVE_FLUSH_MAX,
    STARTUP_LIVE_FLUSH_MS,
    MAX_WORKERS,
    HI_WORKERS,
    BROWSER_LIVE_INTERVAL_SECS,
    BROWSER_LIVE_AHEAD_ROWS,
    BROWSER_LIVE_AHEAD_PER_TICK,
    BROWSER_LIVE_WORKERS,
    BROWSER_LIVE_TIMEOUT_SECS,
    BROWSER_LIVE_PING_DAMPEN_MS,
    BROWSER_LIVE_FALLBACK_ROW_HEIGHT_PX,
    OFFLINE_RECHECK_SECS,
    REFRESH_RATE_LIMIT_SECS,
    IMAGES_DIR,
    DISCLAIMER_TEXT,
    DEAD_MAX_FAILS,
    APP_VERSION,
    RELEASES_URL,
    GITHUB_LATEST_API,
    STEAM_CURRENT_PLAYERS_URL,
    GLOBAL_PLAYERS_POLL_SECS,
    COMPANION_POLL_ONLINE_SECONDS,
    COMPANION_POLL_OFFLINE_SECONDS,
    COMPANION_ALERT_REARM_OFFLINE_SECONDS,
    TEST_SERVER_MARKERS,
)

from .storage import (
    load_favorites,
    save_favorites,
    load_last_played,
    save_last_played,
    human_last_played,
    load_last_companion_server,
    save_last_companion_server,
    clear_last_companion_server,
    load_companion_restart_learning,
    save_companion_restart_learning,
    load_dead_cache,
    save_dead_cache,
)
from .launcher_user_config import set_launcher_shutdown_mode
from .db import fetch_db_overwrite_local, read_servers_from_db
from .live import query_server_live, is_valid_hhmm
from .maps import standardize_map, map_choices_from_db_rows
from .ui_row import ServerObject, hr, attach_pointer_cursor
from .column_view import (
    build_server_column_view,
    refresh_column_view_sort_header_handlers,
    refresh_column_view_sort_indicators,
    set_sort_debug_bind_hook,
)

from .settings import (
    load_settings,
    save_settings,
    reset_settings,
    autodetect_steamcmd_path,
    autodetect_workshop_dir,
)

from .styles import get_app_css
from .update_ui import UpdateUI
from .settings_ui import SettingsUI
from .sidebar_ui import build_search_area, build_sidebar, build_sidebar_toolbar
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
from .steam_client_mods import run_steam_client_install
from .steamcmd_overlay_ui import SteamCMDOverlayUI
from .launcher_state import bootstrap_launcher_state
from .blocklist_utils import bl_normalize_key, bl_load_local, bl_status
from .join_prepare import join_prepare_and_launch
from .server_companion_ui import ServerCompanionPanel
from .launch_utils import launch_direct_steam_url
from .mod_metadata import mark_mods_used
from .mod_search import (
    build_server_mod_index,
    compact_mod_text,
    normalize_mod_text,
    parse_required_mod_query,
    server_matches_mod_query,
    split_mod_search_operator,
)
from .mod_suggestions import (
    SUGGESTION_SHORTHANDS,
    build_mod_suggestion_index,
    current_comma_token,
    replace_comma_token,
    suggest_mods,
)
from . import companion_restart_learning

SERVER_COMPANION_ALERT_SOUNDS = {
    "online": {
        "female": "OnlineF.mp3",
        "male": "OnlineM.mp3",
        "beep": "serverOnlineBeep.mp3",
    },
    "restart_warning": {
        "female": "RestartF.mp3",
        "male": "RestartM.mp3",
        "beep": "serverOnlineBeep.mp3",
    },
}
MOD_SUGGESTION_RESULT_LIMIT = 50
QUERY_VISIBLE_RECENT_PLAYERS_SECONDS = getattr(
    companion_restart_learning,
    "QUERY_VISIBLE_RECENT_PLAYERS_SECONDS",
    10 * 60,
)
PERF_LOG_ENABLED = os.environ.get("DZLL_PERF_LOG") == "1"
DEBUG_COLUMN_SORT = os.environ.get("DZLL_DEBUG_COLUMN_SORT") == "1"
DEBUG_SORT = os.environ.get("DZLL_DEBUG_SORT") == "1"
# Temporary opt-in timing for comparing sidebar filters with column sorting.
DEBUG_FILTER_TIMING = os.environ.get("DZLL_DEBUG_FILTER_TIMING") == "1"
DEBUG_MOD_SUGGESTIONS = os.environ.get("DZLL_DEBUG_MOD_SUGGESTIONS") == "1"
DEBUG_UI = os.environ.get("DZLL_DEBUG_UI") == "1"
DEBUG_BROWSER_REORDER = os.environ.get("DZLL_DEBUG_BROWSER_REORDER") == "1"
ENABLE_LEGACY_GTK_SORT_MODEL = os.environ.get("DZLL_ENABLE_LEGACY_GTK_SORT_MODEL") == "1"
DEBUG_SC_DOCK = os.environ.get("DZLL_DEBUG_SC_DOCK") == "1"
DEBUG_SC_ALERTS = os.environ.get("DZLL_DEBUG_SC_ALERTS") == "1"
DEBUG_STARTUP_LIVE = os.environ.get("DZLL_DEBUG_STARTUP_LIVE") == "1"
FAST_SCROLL_RENDER_ENABLED = os.environ.get("DZLL_FAST_SCROLL_RENDER") == "1"
INCREMENTAL_MODELS_DISABLED = os.environ.get("DZLL_DISABLE_INCREMENTAL_MODELS") == "1"
INCREMENTAL_MODELS_ENABLED = not INCREMENTAL_MODELS_DISABLED
PERF_STALL_INTERVAL_MS = 250
PERF_STALL_LATE_MS = 250
BROWSER_LIVE_SCROLL_PAUSE_SECONDS = 1.0


def enable_incremental_model_if_available(model, label: str) -> None:
    if not INCREMENTAL_MODELS_ENABLED:
        if PERF_LOG_ENABLED:
            print(f"[PERF] incremental {label} model disabled by env", flush=True)
        return
    setter = getattr(model, "set_incremental", None)
    if not callable(setter):
        if PERF_LOG_ENABLED:
            print(f"[PERF] incremental {label} model unavailable", flush=True)
        return
    try:
        setter(True)
        if PERF_LOG_ENABLED:
            print(f"[PERF] incremental {label} model enabled", flush=True)
    except Exception:
        if PERF_LOG_ENABLED:
            print(f"[PERF] incremental {label} model unavailable", flush=True)


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
      - installs icon to ~/.local/share/icons/hicolor/256x256/apps/com.bdingle.dzll.png
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
        desktop_path = user_apps / f"{app_id}.desktop"
        marker = "X-DZLL-AutoCreated=true"
        system_desktop_path = Path("/usr/share/applications") / f"{app_id}.desktop"

        if system_desktop_path.exists():
            try:
                if desktop_path.exists():
                    txt = desktop_path.read_text(encoding="utf-8", errors="replace")
                    if marker in txt:
                        desktop_path.unlink()
            except Exception:
                pass
            return

        user_apps.mkdir(parents=True, exist_ok=True)
        user_icons.mkdir(parents=True, exist_ok=True)

        # ----- icon -----
        icon_name = app_id
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
                icon_src_path=os.path.join(IMAGES_DIR, "com.bdingle.dzll.png"),
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
        self._perf_row_binds = 0
        self._perf_sort_calls = 0
        self._perf_sort_total = 0.0
        self._perf_sort_max = 0.0
        self._perf_scroll_events = 0
        self._perf_scroll_total = 0.0
        self._perf_scroll_max = 0.0
        self._row_widgets = weakref.WeakSet()
        self._fast_scroll_render_refresh_id = 0
        self._perf_stall_last = time.perf_counter()
        self._filter_timing_enabled = DEBUG_FILTER_TIMING
        if PERF_LOG_ENABLED:
            self._start_perf_diagnostics()
        if DEBUG_SORT:
            set_sort_debug_bind_hook(self._debug_sort_note_bind)

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
        self._clear_blocklist_runtime_state()

        # Steam global players state
        self._steam_global_players = None
        GLib.timeout_add_seconds(2, self._steam_global_players_startup_tick)
        GLib.timeout_add_seconds(GLOBAL_PLAYERS_POLL_SECS, self._steam_global_players_tick)

        # Update check state
        self._update_info = None
        self._update_card_dismissed = False
        self._server_db_update_inflight = False

        # Sort defaults
        self.sort_key = "ping"
        self.sort_asc = False  # default: highest ping / offline first
        self._sort_changed_by_user = False
        self._debug_sort_stats = None
        self._search_filter_debounce_id = 0
        self._scroll_to_top_idle_id = 0
        self._filter_state = {}
        self._filter_query = ""
        self._filter_refresh_suppress_depth = 0
        self._mod_suggestion_index = build_mod_suggestion_index(())
        self._mod_suggestion_selecting = False
        self._mod_suggestion_dismissed = None
        self._mod_suggestion_refresh_id = 0
        self._mod_suggestion_last_rows = ()
        self._mod_suggestion_keyboard_selected = False
        self._mod_search_mode_active = False
        self._mod_search_saved_normal_text = ""
        self._mod_search_entry_update_guard = False
        self._selected_mod_chips = []

        self.favorites = load_favorites()
        self.last_played = load_last_played()
        self._last_server_companion_saved = load_last_companion_server()
        self._companion_restart_learning = companion_restart_learning.normalize_state(
            load_companion_restart_learning()
        )
        self._pending_last_played_obj = None
        self._pending_join_mod_ids = []
        self._pending_join_mod_names_by_id = {}

        self.dead = load_dead_cache()
        self._prune_expired_dead()
        self._clamp_dead_cache()

        self._dead_session = set()

        # live flags used by filter only
        self.live = {}
        self._refresh_rl = {}
        self._obj_by_key = {}
        self._server_companion_rows_loaded = False

        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._db_executor = ThreadPoolExecutor(max_workers=1)
        self._update_executor = ThreadPoolExecutor(max_workers=1)
        self._hi_executor = ThreadPoolExecutor(max_workers=HI_WORKERS)
        self._browser_live_executor = ThreadPoolExecutor(max_workers=BROWSER_LIVE_WORKERS)
        self._startup_live_executor = ThreadPoolExecutor(max_workers=STARTUP_LIVE_REST_WORKERS)
        self._browser_live_timer_id = 0
        self._browser_live_inflight = False
        self._browser_live_token = 0
        self._browser_live_target_keys = set()
        self._browser_live_last_refresh = {}
        self._browser_live_offline_streaks = {}
        self._browser_live_scroll_active_until = 0.0
        self._browser_live_filter_cooldown_until = 0.0
        self._browser_live_scroll_pause_logged_until = 0.0
        self._browser_live_filter_cooldown_logged_until = 0.0
        self._browser_live_apply_skip_logged_until = 0.0
        self._startup_live_generation = 0
        self._startup_live_rest_queue = deque()
        self._startup_live_rest_inflight = 0
        self._startup_live_rest_buffer = []
        self._startup_live_rest_flush_id = 0
        self._startup_live_rest_total = 0
        self._startup_live_rest_completed = 0
        self._startup_live_rest_started_at = 0.0
        self._startup_live_rest_last_log_completed = 0
        self._status_refresh_generation = 0
        self._status_refresh_running = False
        self._status_refresh_queue = deque()
        self._status_refresh_inflight = 0
        self._status_refresh_buffer = []
        self._status_refresh_flush_id = 0
        self._status_refresh_total = 0
        self._status_refresh_completed = 0
        self._status_refresh_started_at = 0.0
        self._status_refresh_last_log_completed = 0

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
        self._run_steam_client_install_impl = run_steam_client_install
        self.run_steam_client_install = self._run_steam_client_install_with_stop_waiting
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
        self._steam_ugc_progress_timer_id = 0
        self._steam_ugc_installed_ids = set()
        self._steam_ugc_active_event = None
        self._steam_ugc_completed_count = 0
        self._steam_ugc_percent_label = None
        self._steam_ugc_last_error = ""

        # NEW: Cancel support
        self._steamcmd_cancel_event = threading.Event()
        self._steam_client_stop_waiting_event = threading.Event()
        self._steam_client_safe_cancel_requested = False
        self._steam_client_downloads_opened = False
        self._steam_client_open_downloads_btn = None
        self._steamcmd_install_in_progress = False
        self._mod_download_backend_active = ""
        self._join_steam_start_allowed = False

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

        # ----------------------------
        # START STEAM ON JOIN CONSENT OVERLAY
        # ----------------------------
        self.start_steam_join_scrim = Gtk.Box()
        self.start_steam_join_scrim.set_hexpand(True)
        self.start_steam_join_scrim.set_vexpand(True)
        self.start_steam_join_scrim.set_visible(False)
        self.start_steam_join_scrim.set_can_target(True)
        self.start_steam_join_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.start_steam_join_scrim)

        self.start_steam_join_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.start_steam_join_box.set_halign(Gtk.Align.CENTER)
        self.start_steam_join_box.set_valign(Gtk.Align.CENTER)
        self.start_steam_join_box.set_visible(False)
        self.start_steam_join_box.set_can_target(True)
        self.start_steam_join_box.add_css_class("warning-card")
        overlay.add_overlay(self.start_steam_join_box)

        self.start_steam_join_title = Gtk.Label(label="Start Steam to join server?")
        self.start_steam_join_title.set_xalign(0.0)
        self.start_steam_join_title.set_wrap(True)
        self.start_steam_join_title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.start_steam_join_title.add_css_class("steamcmd-heading")
        self.start_steam_join_box.append(self.start_steam_join_title)

        self.start_steam_join_text = Gtk.Label(
            label="DZLL needs Steam running and logged in before it can check, download, "
                  "or update required Workshop mods and launch DayZ."
        )
        self.start_steam_join_text.set_xalign(0.0)
        self.start_steam_join_text.set_wrap(True)
        self.start_steam_join_text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.start_steam_join_text.set_max_width_chars(76)
        self.start_steam_join_box.append(self.start_steam_join_text)

        start_steam_helper_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        start_steam_helper_group.set_margin_bottom(16)

        self.start_steam_join_check = Gtk.CheckButton(label="Always start Steam automatically")
        self.start_steam_join_check.set_halign(Gtk.Align.START)
        attach_pointer_cursor(self.start_steam_join_check)
        start_steam_helper_group.append(self.start_steam_join_check)

        self.start_steam_join_helper = Gtk.Label(label="You can change this later in Settings → Launch.")
        self.start_steam_join_helper.set_xalign(0.0)
        self.start_steam_join_helper.set_wrap(True)
        self.start_steam_join_helper.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.start_steam_join_helper.add_css_class("dim-label")
        self.start_steam_join_helper.set_margin_start(26)
        start_steam_helper_group.append(self.start_steam_join_helper)
        self.start_steam_join_box.append(start_steam_helper_group)

        start_steam_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        start_steam_btn_row.set_halign(Gtk.Align.CENTER)

        self.start_steam_join_cancel_btn = Gtk.Button(label="Cancel")
        self.start_steam_join_cancel_btn.add_css_class("warning-btn")
        attach_pointer_cursor(self.start_steam_join_cancel_btn)
        start_steam_btn_row.append(self.start_steam_join_cancel_btn)

        self.start_steam_join_start_btn = Gtk.Button(label="Start Steam")
        self.start_steam_join_start_btn.add_css_class("suggested-action")
        self.start_steam_join_start_btn.add_css_class("warning-btn")
        attach_pointer_cursor(self.start_steam_join_start_btn)
        start_steam_btn_row.append(self.start_steam_join_start_btn)

        self.start_steam_join_box.append(start_steam_btn_row)

        self._start_steam_join_decision = None
        self._start_steam_join_loop = None

        def _start_steam_join_finish(ok: bool):
            always = False
            try:
                always = bool(self.start_steam_join_check.get_active())
            except Exception:
                always = False
            self._start_steam_join_decision = (bool(ok), always)
            try:
                self.start_steam_join_box.set_visible(False)
                self.start_steam_join_scrim.set_visible(False)
            except Exception:
                pass
            try:
                if self._start_steam_join_loop:
                    self._start_steam_join_loop.quit()
            except Exception:
                pass

        start_steam_join_scrim_click = Gtk.GestureClick.new()
        start_steam_join_scrim_click.set_button(0)
        start_steam_join_scrim_click.connect("pressed", lambda *_: _start_steam_join_finish(False))
        self.start_steam_join_scrim.add_controller(start_steam_join_scrim_click)
        self.start_steam_join_cancel_btn.connect("clicked", lambda *_: _start_steam_join_finish(False))
        self.start_steam_join_start_btn.connect("clicked", lambda *_: _start_steam_join_finish(True))

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

        left_main_shell = Gtk.Overlay()
        left_main_shell.set_hexpand(True)
        left_main_shell.set_vexpand(True)
        self._search_area_overlay = left_main_shell
        root.append(left_main_shell)

        left_main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        left_main.set_hexpand(True)
        left_main.set_vexpand(True)
        left_main_shell.set_child(left_main)

        sidebar_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar_stack.set_size_request(SIDEBAR_WIDTH, -1)
        sidebar_stack.set_hexpand(False)
        sidebar_stack.set_vexpand(True)
        sidebar_stack.set_halign(Gtk.Align.START)
        left_main.append(sidebar_stack)

        sidebar_stack.append(build_sidebar_toolbar(self))

        sidebar_frame = build_sidebar(self, include_toolbar=False)
        self._install_header_mod_manager_button()
        try:
            self.refresh_status_btn.set_tooltip_text("Refresh all server status")
        except Exception:
            pass
        sidebar_stack.append(sidebar_frame)

        left_main.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        browser_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        browser_stack.set_hexpand(True)
        browser_stack.set_vexpand(True)
        left_main.append(browser_stack)

        self._search_area_overlay_margin_start = SIDEBAR_WIDTH + 1
        browser_stack.append(build_search_area(self))

        main_shell = Gtk.Overlay()
        main_shell.set_hexpand(True)
        main_shell.set_vexpand(True)
        self.main_browser_box = main_shell
        browser_stack.append(main_shell)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.set_hexpand(True)
        main.set_vexpand(True)
        main_shell.set_child(main)

        if DEBUG_UI:
            print("[UI] Column View enabled", flush=True)

        self.empty_label = Gtk.Label(label="No Servers Found.")
        self.empty_label.set_xalign(0.0)
        self.empty_label.set_margin_start(12)
        self.empty_label.set_margin_top(10)
        self.empty_label.set_margin_bottom(10)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.set_visible(False)
        main.append(self.empty_label)

        self.store = Gio.ListStore.new(ServerObject)

        if ENABLE_LEGACY_GTK_SORT_MODEL:
            self.combined_filter = Gtk.CustomFilter.new(self._combined_filter_func, None)
            self.filter_model = Gtk.FilterListModel.new(self.store, self.combined_filter)
            enable_incremental_model_if_available(self.filter_model, "filter")

            self.sorter = Gtk.CustomSorter.new(self._sort_func, None)
            self.sort_model = Gtk.SortListModel.new(self.filter_model, self.sorter)
            enable_incremental_model_if_available(self.sort_model, "sort")

            self.selection = Gtk.NoSelection.new(self.sort_model)
        else:
            self.combined_filter = None
            self.filter_model = None
            self.sorter = None
            self.sort_model = None
            self.selection = None

        if PERF_LOG_ENABLED:
            print("[PERF] column view enabled", flush=True)
        self.column_view_store = Gio.ListStore.new(ServerObject)
        self.column_view_selection = Gtk.NoSelection.new(self.column_view_store)
        self.list_view, column_view_features = build_server_column_view(
            self.column_view_selection,
            self._toggle_favorite_for_obj,
            self._monitor_server_companion_for_obj,
            self._join_server_for_obj,
            self._on_column_view_sort_header_clicked,
            self._is_server_companion_monitoring_obj,
        )
        refresh_column_view_sort_header_handlers(self.list_view)
        if PERF_LOG_ENABLED:
            enabled = []
            if column_view_features.get("column_separators"):
                enabled.append("column")
            if column_view_features.get("row_separators"):
                enabled.append("row")
            if enabled:
                print(f"[PERF] column view separators enabled: {', '.join(enabled)}", flush=True)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.list_view)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)

        self.server_list_overlay = Gtk.Overlay()
        self.server_list_overlay.set_child(self.scroller)
        self.server_list_overlay.set_vexpand(True)
        self.server_list_overlay.set_hexpand(True)

        self.browser_toast_label = Gtk.Label()
        self.browser_toast_label.add_css_class("browser-toast")
        self.browser_toast_label.set_halign(Gtk.Align.START)
        self.browser_toast_label.set_valign(Gtk.Align.START)
        self.browser_toast_label.set_margin_start(0)
        self.browser_toast_label.set_margin_top(0)
        self.browser_toast_label.set_visible(False)
        try:
            self.browser_toast_label.set_can_target(False)
            self.browser_toast_label.set_can_focus(False)
        except Exception:
            pass
        self._browser_toast_timeout_id = 0
        self.server_list_overlay.add_overlay(self.browser_toast_label)

        server_companion_show_icon = Gtk.Image.new_from_icon_name("system-shutdown-symbolic")
        server_companion_show_icon.set_pixel_size(16)
        server_companion_show_icon.add_css_class("server-companion-power-off-icon")
        self.server_companion_show_btn = Gtk.Button()
        self.server_companion_show_btn.set_can_focus(False)
        self.server_companion_show_btn.set_child(server_companion_show_icon)
        self.server_companion_show_btn.add_css_class("flat")
        self.server_companion_show_btn.add_css_class("server-companion-power-off-button")
        self.server_companion_show_btn.set_tooltip_text("Show Server Companion")
        self.server_companion_show_btn.set_size_request(28, 28)
        self.server_companion_show_btn.set_halign(Gtk.Align.END)
        self.server_companion_show_btn.set_valign(Gtk.Align.START)
        self.server_companion_show_btn.set_margin_top(4)
        self.server_companion_show_btn.set_margin_end(12)
        self.server_companion_show_btn.connect("clicked", lambda *_: self.set_server_companion_enabled(True))
        attach_pointer_cursor(self.server_companion_show_btn)
        self.server_list_overlay.add_overlay(self.server_companion_show_btn)

        main.append(self.server_list_overlay)
        try:
            vadj = self.scroller.get_vadjustment()
            if vadj:
                vadj.connect("value-changed", self._on_browser_scroll_value_changed)
        except Exception:
            pass

        self._server_companion_snapshot = None
        self._server_companion_obj = None
        self._server_companion_offline_since = None
        self._server_companion_offline_since_wall = None
        self._server_companion_alert_armed = False
        self._server_companion_restart_warning_fired = set()
        self._server_companion_poll_interval_secs = COMPANION_POLL_ONLINE_SECONDS
        self._server_companion_poll_timer_id = 0
        self._server_companion_poll_paused = False
        self._server_companion_poll_inflight = False
        self._server_companion_poll_token = 0
        self._server_companion_last_online = None
        self._server_companion_monitor_last_saved_at = {}
        self._server_companion_consecutive_offline_polls = 0
        self._server_companion_visible_snapshot = None
        self._server_companion_observation_samples = deque(maxlen=5000)
        self._server_companion_visible_zero_state = None
        self._pending_server_companion_obj = None
        self._server_companion_docked = True
        self._server_companion_undocked_window = None
        self._server_companion_undocked_close_handler_id = 0
        self._server_companion_reparenting = False
        self._server_companion_restart_alert_enabled = bool(
            self.settings.get("server_companion_restart_alert_enabled", False)
        )
        try:
            self._server_companion_alert_volume = max(
                0, min(100, int(self.settings.get("server_companion_alert_volume", 80)))
            )
        except Exception:
            self._server_companion_alert_volume = 80
        self._server_companion_alert_sound = str(
            self.settings.get("server_companion_alert_sound", "female") or "female"
        )
        if self._server_companion_alert_sound not in SERVER_COMPANION_ALERT_SOUNDS["online"]:
            self._server_companion_alert_sound = "female"

        self.server_companion_panel = ServerCompanionPanel()
        self.server_companion_panel.set_on_clear(self.clear_server_companion)
        self.server_companion_panel.set_on_play_pause(self.toggle_server_companion_polling)
        self.server_companion_panel.set_on_join(self.join_server_companion)
        self.server_companion_panel.set_on_restart_alert_toggled(self.set_server_companion_restart_alert_enabled)
        self.server_companion_panel.set_on_alert_sound_changed(self.set_server_companion_alert_sound)
        self.server_companion_panel.set_on_alert_volume_changed(self.set_server_companion_alert_volume)
        self.server_companion_panel.set_on_alert_test_clicked(self.test_server_companion_alert_sound)
        self.server_companion_panel.set_on_dock_toggle(self.toggle_server_companion_dock)
        self.server_companion_panel.set_on_power_off(lambda *_: self.set_server_companion_enabled(False))
        self.server_companion_panel.set_docked(True)
        self.server_companion_panel.set_restart_alert_enabled(self._server_companion_restart_alert_enabled)
        self.server_companion_panel.set_alert_sound(self._server_companion_alert_sound)
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
        self._start_browser_live_refresh()
        GLib.idle_add(self._begin_startup_update)

    def _start_perf_diagnostics(self):
        GLib.timeout_add(PERF_STALL_INTERVAL_MS, self._perf_main_loop_stall_tick)
        GLib.timeout_add_seconds(1, self._perf_row_bind_tick)
        GLib.timeout_add_seconds(1, self._perf_sort_tick)
        GLib.timeout_add_seconds(1, self._perf_scroll_tick)
        GLib.timeout_add_seconds(1, self._perf_model_pending_tick)

    def _perf_main_loop_stall_tick(self):
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False
        now = time.perf_counter()
        elapsed_ms = (now - float(getattr(self, "_perf_stall_last", now))) * 1000.0
        self._perf_stall_last = now
        if elapsed_ms > float(PERF_STALL_INTERVAL_MS + PERF_STALL_LATE_MS):
            print(f"[PERF] main loop stall: {elapsed_ms:.0f}ms", flush=True)
        return True

    def _perf_row_bind_tick(self):
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False
        count = int(getattr(self, "_perf_row_binds", 0) or 0)
        self._perf_row_binds = 0
        if count > 0:
            print(f"[PERF] row binds/sec={count}", flush=True)
        return True

    def _perf_sort_tick(self):
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False
        count = int(getattr(self, "_perf_sort_calls", 0) or 0)
        total = float(getattr(self, "_perf_sort_total", 0.0) or 0.0)
        max_one = float(getattr(self, "_perf_sort_max", 0.0) or 0.0)
        self._perf_sort_calls = 0
        self._perf_sort_total = 0.0
        self._perf_sort_max = 0.0
        if count > 0:
            print(f"[PERF] sort calls/sec={count} total={total * 1000.0:.1f}ms max={max_one * 1000.0:.1f}ms", flush=True)
        return True

    def _perf_scroll_tick(self):
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False
        count = int(getattr(self, "_perf_scroll_events", 0) or 0)
        total = float(getattr(self, "_perf_scroll_total", 0.0) or 0.0)
        max_one = float(getattr(self, "_perf_scroll_max", 0.0) or 0.0)
        self._perf_scroll_events = 0
        self._perf_scroll_total = 0.0
        self._perf_scroll_max = 0.0
        if count > 0:
            print(f"[PERF] scroll events/sec={count} total={total * 1000.0:.1f}ms max={max_one * 1000.0:.1f}ms", flush=True)
        return True

    def _perf_model_value(self, model, name: str):
        if model is None:
            return None
        getter = f"get_{name}"
        try:
            fn = getattr(model, getter, None)
            if callable(fn):
                return fn()
        except Exception:
            pass
        try:
            return model.get_property(name)
        except Exception:
            return None

    def _perf_model_pending_tick(self):
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False
        sort_pending = self._perf_model_value(getattr(self, "sort_model", None), "pending")
        filter_pending = self._perf_model_value(getattr(self, "filter_model", None), "pending")
        sort_incremental = self._perf_model_value(getattr(self, "sort_model", None), "incremental")
        filter_incremental = self._perf_model_value(getattr(self, "filter_model", None), "incremental")
        values = {
            "sort_pending": sort_pending,
            "filter_pending": filter_pending,
            "sort_incremental": sort_incremental,
            "filter_incremental": filter_incremental,
        }
        if any(v not in (None, 0, False) for v in values.values()):
            parts = [f"{k}={v}" for k, v in values.items() if v is not None]
            print(f"[PERF] model state: {' '.join(parts)}", flush=True)
        return True

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
    def _steam_global_players_startup_tick(self):
        self._steam_global_players_tick()
        return False

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

    def _set_server_companion_join_status(self, message: str | None, flash: bool = False):
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None and hasattr(panel, "set_join_status"):
            panel.set_join_status(message, flash=flash)

    def _show_steamcmd_auth_overlay(self, username_prefill: str = "", status: str = ""):
        self._set_server_companion_join_status(
            "SteamCMD Login Required",
            flash=True,
        )
        return self._steamcmd_overlay_ui._show_steamcmd_auth_overlay(username_prefill=username_prefill, status=status)

    def _show_steam_client_download_overlay(self, status: str = ""):
        self._mod_download_backend_active = "steam_client"
        self._steam_client_safe_cancel_requested = False
        self._steam_ugc_installed_ids = set()
        self._steam_ugc_active_event = None
        self._steam_ugc_completed_count = 0
        self._steam_ugc_last_error = ""
        try:
            self._steam_client_stop_waiting_event.clear()
        except Exception:
            self._steam_client_stop_waiting_event = threading.Event()
        self._steam_client_set_cancel_buttons(safe_cancel=False)
        self._set_server_companion_join_status("Checking/Updating Required Mods", flash=True)
        return self._show_steam_ugc_download_overlay()

    def _show_join_progress_overlay(self, status: str = ""):
        text = str(status or "").strip() or "Preparing required mods..."
        try:
            self._set_updating(False)
        except Exception:
            pass
        try:
            for widget in getattr(self, "_steamcmd_form_widgets", []):
                try:
                    widget.set_visible(False)
                except Exception:
                    pass
            self.steamcmd_login_btn.set_visible(False)
            self.steamcmd_cancel_btn.set_visible(False)
            self.steamcmd_auth_scrim.set_visible(True)
            self.steamcmd_auth_box.set_visible(True)
        except Exception:
            pass
        try:
            self._steam_ugc_render_status(text)
        except Exception:
            pass
        return False

    def _hide_steamcmd_auth_overlay(self):
        try:
            self._steam_ugc_stop_progress_timer()
            self._steam_ugc_set_layout_active(False)
            return self._steamcmd_overlay_ui._hide_steamcmd_auth_overlay()
        finally:
            self._set_server_companion_join_status(None)

    def _steamcmd_auth_submit(self):
        result = self._steamcmd_overlay_ui._steamcmd_auth_submit()
        req = getattr(self, "_steamcmd_auth_request", None)
        auth_result = getattr(self, "_steamcmd_auth_result", None)
        if req is None and isinstance(auth_result, dict) and auth_result.get("ok"):
            self._set_server_companion_join_status(None)
        return result

    def _steamcmd_auth_cancel(self):
        if getattr(self, "_mod_download_backend_active", "") == "steam_client":
            return self._steam_client_download_cancel_clicked()
        try:
            return self._steamcmd_overlay_ui._steamcmd_auth_cancel()
        finally:
            self._set_server_companion_join_status(None)

    def _request_steamcmd_credentials_blocking(self, username_prefill: str = "", status: str = ""):
        try:
            return self._steamcmd_overlay_ui._request_steamcmd_credentials_blocking(username_prefill=username_prefill, status=status)
        finally:
            req = getattr(self, "_steamcmd_auth_request", None)
            if req is None:
                GLib.idle_add(self._set_server_companion_join_status, None)

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
        try:
            self._steam_ugc_stop_progress_timer()
            self._steam_ugc_set_layout_active(False)
            self._steam_ugc_installed_ids = set()
            self._steam_ugc_active_event = None
            self._steam_ugc_completed_count = 0
            self._steam_ugc_last_error = ""
            self._steam_client_safe_cancel_requested = False
            self._steam_client_downloads_opened = False
            try:
                self._steam_client_stop_waiting_event.clear()
            except Exception:
                self._steam_client_stop_waiting_event = threading.Event()
            self._steam_client_set_cancel_buttons(safe_cancel=False)
        except Exception:
            pass
        return self._steamcmd_overlay_ui._steamcmd_reset_state_for_new_run()

    def _steam_client_set_cancel_buttons(self, safe_cancel: bool):
        try:
            self.steamcmd_cancel_btn.set_label("Cancel")
            self.steamcmd_cancel_btn.set_tooltip_text("Cancel download and unsubscribe unfinished mods.")
        except Exception:
            pass
        try:
            btn = getattr(self, "_steam_client_open_downloads_btn", None)
            if btn is not None:
                btn.set_visible(False)
                btn.set_sensitive(False)
        except Exception:
            pass

    def _steam_client_download_cancel_clicked(self):
        if bool(getattr(self, "_steam_client_safe_cancel_requested", False)):
            return self._steam_client_stop_waiting()

        self._steam_client_safe_cancel_requested = True
        try:
            self._steamcmd_cancel_event.set()
        except Exception:
            pass

        self._steam_client_set_cancel_buttons(safe_cancel=True)
        self._steam_ugc_render_cancelling()
        return None

    def _steam_client_stop_waiting(self):
        try:
            self._steamcmd_cancel_event.set()
        except Exception:
            pass
        try:
            self._steam_client_stop_waiting_event.set()
        except Exception:
            pass
        try:
            self._hide_steamcmd_auth_overlay()
        except Exception:
            pass
        return None

    def _steam_ugc_format_size(self, byte_count) -> str:
        try:
            total_bytes = int(byte_count or 0)
        except Exception:
            total_bytes = 0
        if total_bytes <= 0:
            return "Preparing download..."
        gib = 1024 * 1024 * 1024
        mib = 1024 * 1024
        if total_bytes >= gib:
            return f"{float(total_bytes) / float(gib):.1f} GB"
        return f"{round(float(total_bytes) / float(mib)):.0f} MB"

    def _steam_ugc_title(self) -> str:
        return "Checking/updating the required mods for this server"

    def _show_steam_ugc_download_overlay(self):
        try:
            for widget in getattr(self, "_steamcmd_form_widgets", []):
                try:
                    widget.set_visible(False)
                except Exception:
                    pass
            self.steamcmd_login_btn.set_visible(False)
            self.steamcmd_cancel_btn.set_visible(True)
            self.steamcmd_auth_scrim.set_visible(True)
            self.steamcmd_auth_box.set_visible(True)
        except Exception:
            pass
        return self._steam_ugc_render_preparing()

    def _steam_ugc_get_percent_label(self):
        label = getattr(self, "_steam_ugc_percent_label", None)
        if label is not None:
            return label
        try:
            label = Gtk.Label(label="0%")
            label.set_halign(Gtk.Align.CENTER)
            label.set_xalign(0.5)
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_foreground_new(0xC8C8, 0xC8C8, 0xC8C8))
            label.set_attributes(attrs)
            label.set_visible(False)
            parent = self.steamcmd_prog_bar.get_parent()
            if parent is not None:
                parent.append(label)
            self._steam_ugc_percent_label = label
            return label
        except Exception:
            self._steam_ugc_percent_label = None
            return None

    def _steam_ugc_set_layout_active(self, active: bool):
        try:
            self.steamcmd_line1.set_visible(not active)
        except Exception:
            pass
        try:
            self.steamcmd_prog_bar.set_margin_top(10 if active else 0)
        except Exception:
            pass
        try:
            btn_parent = self.steamcmd_cancel_btn.get_parent()
            if btn_parent is not None:
                btn_parent.set_margin_top(10 if active else 0)
        except Exception:
            pass
        label = self._steam_ugc_get_percent_label() if active else getattr(self, "_steam_ugc_percent_label", None)
        if label is not None:
            try:
                if active:
                    label.set_text(label.get_text() or "0%")
                    label.set_visible(True)
                else:
                    label.set_text("0%")
                    label.set_visible(False)
            except Exception:
                pass

    def _steam_ugc_render_preparing(self):
        try:
            self._steam_ugc_set_layout_active(True)
            self.steamcmd_task_heading.set_text(self._steam_ugc_title())
            self.steamcmd_line1.set_text("")
            self.steamcmd_line2.set_text("Steam is checking the required mods...")
        except Exception:
            pass
        try:
            self.steamcmd_spinner.set_visible(True)
            self.steamcmd_spinner.set_spinning(True)
        except Exception:
            pass
        try:
            self.steamcmd_prog_bar.set_visible(True)
            self.steamcmd_prog_bar.set_show_text(False)
            self.steamcmd_prog_bar.set_fraction(0.0)
        except Exception:
            pass
        label = self._steam_ugc_get_percent_label()
        if label is not None:
            try:
                label.set_text("0%")
                label.set_visible(True)
            except Exception:
                pass
        self._steam_ugc_start_progress_timer()
        return False

    def _steam_ugc_render_cancelling(self):
        try:
            self._steam_ugc_set_layout_active(True)
            self.steamcmd_task_heading.set_text(self._steam_ugc_title())
            self.steamcmd_line1.set_text("")
            self.steamcmd_line2.set_text("Cancelling download and cleaning up...")
        except Exception:
            pass
        try:
            self.steamcmd_spinner.set_visible(True)
            self.steamcmd_spinner.set_spinning(True)
        except Exception:
            pass
        try:
            self.steamcmd_prog_bar.set_visible(True)
            self.steamcmd_prog_bar.set_show_text(False)
        except Exception:
            pass
        return False

    def _steam_ugc_render_status(self, message: str, *, error: bool = False):
        text = str(message or "").strip()
        if not text:
            return False
        if error:
            self._steam_ugc_last_error = text
        try:
            self._steam_ugc_set_layout_active(True)
            self.steamcmd_task_heading.set_text(self._steam_ugc_title())
            self.steamcmd_line1.set_text("")
            self.steamcmd_line2.set_text(text)
        except Exception:
            pass
        try:
            self.steamcmd_spinner.set_visible(not bool(error))
            self.steamcmd_spinner.set_spinning(not bool(error))
        except Exception:
            pass
        try:
            self.steamcmd_prog_bar.set_visible(True)
            self.steamcmd_prog_bar.set_show_text(False)
            self.steamcmd_prog_bar.set_fraction(0.0)
            if not error:
                self._steam_ugc_start_progress_timer()
            else:
                self._steam_ugc_stop_progress_timer()
        except Exception:
            pass
        label = self._steam_ugc_get_percent_label()
        if label is not None:
            try:
                label.set_text("0%")
                label.set_visible(True)
            except Exception:
                pass
        try:
            self._set_server_companion_join_status(text, flash=bool(error))
        except Exception:
            pass
        return False

    def _steam_ugc_start_progress_timer(self):
        try:
            if int(getattr(self, "_steam_ugc_progress_timer_id", 0) or 0):
                return
            self._steam_ugc_progress_timer_id = GLib.timeout_add(500, self._steam_ugc_progress_pulse)
        except Exception:
            self._steam_ugc_progress_timer_id = 0

    def _steam_ugc_stop_progress_timer(self):
        try:
            tid = int(getattr(self, "_steam_ugc_progress_timer_id", 0) or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self._steam_ugc_progress_timer_id = 0
        except Exception:
            self._steam_ugc_progress_timer_id = 0

    def _steam_ugc_progress_pulse(self):
        try:
            if (
                getattr(self, "_mod_download_backend_active", "") != "steam_client"
                or not bool(getattr(self, "_steamcmd_install_in_progress", False))
            ):
                self._steam_ugc_progress_timer_id = 0
                return False
            active = getattr(self, "_steam_ugc_active_event", None) or {}
            total_bytes = int(active.get("total_bytes") or 0) if isinstance(active, dict) else 0
            if total_bytes <= 0:
                try:
                    self.steamcmd_prog_bar.set_visible(True)
                    self.steamcmd_prog_bar.set_show_text(False)
                    self.steamcmd_prog_bar.pulse()
                except Exception:
                    pass
            return True
        except Exception:
            return True

    def _steam_ugc_progress_from_worker(self, event: dict):
        try:
            GLib.idle_add(self._steam_ugc_progress_to_overlay, dict(event or {}))
        except Exception:
            pass

    def _steam_ugc_progress_to_overlay(self, event: dict):
        if bool(getattr(self, "_steam_client_safe_cancel_requested", False)):
            return False
        if not isinstance(event, dict) or event.get("backend") != "steam_ugc":
            return False

        event_type = str(event.get("type") or "")
        message = str(event.get("message") or "").strip()
        if event_type == "status" and message == "Checking/Updating Required Mods":
            try:
                self._set_server_companion_join_status(message, flash=False)
            except Exception:
                pass
            return False
        if event_type in ("preflight", "status", "error") and message:
            return self._steam_ugc_render_status(message, error=bool(event.get("error")) or event_type == "error")

        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        if mid <= 0:
            return False

        try:
            total_mods = int(event.get("total") or 0)
        except Exception:
            total_mods = 0

        installed = bool(event.get("installed", False))
        ready = bool(
            event.get(
                "ready",
                installed
                and not bool(event.get("needs_update", False))
                and not bool(event.get("downloading", False))
                and not bool(event.get("download_pending", False)),
            )
        )

        if ready:
            try:
                self._steam_ugc_installed_ids.add(mid)
            except Exception:
                pass
        try:
            event_completed = int(event.get("completed_count") or 0)
        except Exception:
            event_completed = 0
        try:
            set_completed = len(getattr(self, "_steam_ugc_installed_ids", set()) or set())
        except Exception:
            set_completed = 0
        self._steam_ugc_completed_count = max(
            int(getattr(self, "_steam_ugc_completed_count", 0) or 0),
            event_completed,
            set_completed,
        )

        download_bytes = int(event.get("download_bytes") or 0)
        total_bytes = int(event.get("total_bytes") or 0)
        is_active = (
            bool(event.get("downloading", False))
            or bool(event.get("download_pending", False))
            or (download_bytes > 0 and not ready)
        )
        current_active = getattr(self, "_steam_ugc_active_event", None)
        try:
            current_active_mid = int(current_active.get("id") or 0) if isinstance(current_active, dict) else 0
        except Exception:
            current_active_mid = 0
        if is_active or current_active is None or current_active_mid == mid:
            self._steam_ugc_active_event = dict(event)

        active = getattr(self, "_steam_ugc_active_event", None) or event
        try:
            mid = int(active.get("id") or mid)
            download_bytes = int(active.get("download_bytes") or 0)
            total_bytes = int(active.get("total_bytes") or 0)
        except Exception:
            pass
        active_installed = bool(active.get("installed", False))
        active_ready = bool(
            active.get(
                "ready",
                active_installed
                and not bool(active.get("needs_update", False))
                and not bool(active.get("downloading", False))
                and not bool(active.get("download_pending", False)),
            )
        )
        if active_ready and total_bytes > 0:
            download_bytes = total_bytes
        active_is_current = (
            bool(active.get("downloading", False))
            or bool(active.get("download_pending", False))
            or (download_bytes > 0 and not active_ready)
        )
        completed_count = int(getattr(self, "_steam_ugc_completed_count", 0) or 0)
        if active_is_current:
            display_index = min(completed_count + 1, total_mods) if total_mods > 0 else completed_count + 1
        else:
            display_index = min(completed_count, total_mods) if total_mods > 0 else completed_count

        raw_name = str(active.get("name") or "").strip()
        name = raw_name if raw_name else str(mid)
        size_text = self._steam_ugc_format_size(total_bytes)
        if active_ready:
            action_label = "Checking Mod"
        elif active_installed and bool(active.get("needs_update", False)):
            action_label = "Updating Mod"
        elif active_installed:
            action_label = "Checking Mod"
        else:
            action_label = "Downloading Mod"
        mod_line = f"{action_label}: {name} - {size_text} ({display_index}/{total_mods})"

        try:
            self._steam_ugc_set_layout_active(True)
            self.steamcmd_task_heading.set_text(self._steam_ugc_title())
            self.steamcmd_line1.set_text("")
            self.steamcmd_line2.set_text(mod_line)
        except Exception:
            pass

        try:
            self.steamcmd_spinner.set_visible(True)
            self.steamcmd_spinner.set_spinning(True)
        except Exception:
            pass

        percent_label = self._steam_ugc_get_percent_label()
        if total_bytes > 0:
            frac = max(0.0, min(1.0, float(download_bytes) / float(total_bytes)))
            pct = int(frac * 100)
            self._steam_ugc_stop_progress_timer()
            try:
                self.steamcmd_prog_bar.set_visible(True)
                self.steamcmd_prog_bar.set_show_text(False)
                self.steamcmd_prog_bar.set_fraction(frac)
            except Exception:
                pass
            if percent_label is not None:
                try:
                    percent_label.set_text(f"{pct}%")
                    percent_label.set_visible(True)
                except Exception:
                    pass
        else:
            try:
                self.steamcmd_prog_bar.set_visible(True)
                self.steamcmd_prog_bar.set_show_text(False)
                self.steamcmd_prog_bar.set_fraction(0.0)
            except Exception:
                pass
            if percent_label is not None:
                try:
                    percent_label.set_text("0%")
                    percent_label.set_visible(True)
                except Exception:
                    pass
            self._steam_ugc_start_progress_timer()

        return False

    def _open_steam_downloads(self):
        try:
            _steam_cmd = resolve_native_steam_cmd()
            if not _steam_cmd:
                print("[Steam client] Cannot open Steam Downloads: native Steam executable not found.", flush=True)
                return
            subprocess.Popen(
                [_steam_cmd, "steam://open/downloads"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            print(f"[Steam client] Failed to open Steam Downloads: {e}", flush=True)
        return None

    def _run_steam_client_install_with_stop_waiting(self, *args, **kwargs):
        done = threading.Event()
        result = {"ok": False}

        def worker():
            try:
                result["ok"] = bool(self._run_steam_client_install_impl(*args, **kwargs))
            except Exception as e:
                result["error"] = e
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()

        while not done.is_set():
            try:
                if self._steam_client_stop_waiting_event.is_set():
                    return False
            except Exception:
                pass
            done.wait(0.2)

        if "error" in result:
            raise result["error"]
        return bool(result.get("ok", False))

    # ----------------------------
    # Map dropdown update
    # ----------------------------
    def _set_map_choices(self, choices: list[str]):
        if not choices:
            choices = ["All Maps"]
        elif choices[0] == "All":
            choices = ["All Maps"] + list(choices[1:])
        try:
            old_idx = int(self.map_dropdown.get_selected())
        except Exception:
            old_idx = 0
        old_val = self.map_model.get_string(old_idx) if 0 <= old_idx < self.map_model.get_n_items() else "All Maps"
        if old_val == "All":
            old_val = "All Maps"

        n = self.map_model.get_n_items()
        self.map_model.splice(0, n, choices)

        new_idx = 0
        for i in range(self.map_model.get_n_items()):
            if self.map_model.get_string(i) == old_val:
                new_idx = i
                break
        try:
            self._push_filter_refresh_suppression()
            self.map_dropdown.set_selected(new_idx)
        except Exception:
            pass
        finally:
            self._pop_filter_refresh_suppression()

    # ----------------------------
    # Scroll
    # ----------------------------
    def _hide_browser_toast(self) -> bool:
        self._browser_toast_timeout_id = 0
        toast = getattr(self, "browser_toast_label", None)
        if toast is not None:
            try:
                toast.set_visible(False)
            except Exception:
                pass
        return False

    def _browser_toast_size(self, toast) -> tuple[int, int]:
        width = 84
        height = 30
        try:
            _min_w, nat_w, _min_base, _nat_base = toast.measure(Gtk.Orientation.HORIZONTAL, -1)
            _min_h, nat_h, _min_base, _nat_base = toast.measure(Gtk.Orientation.VERTICAL, -1)
            width = max(width, int(nat_w))
            height = max(height, int(nat_h))
        except Exception:
            pass
        return width, height

    def _position_browser_toast(self, source_widget=None, x=None, y=None) -> None:
        toast = getattr(self, "browser_toast_label", None)
        overlay = getattr(self, "server_list_overlay", None)
        if toast is None or overlay is None:
            return
        margin_x = 18
        margin_y = 18
        if source_widget is not None and x is not None and y is not None:
            try:
                ok, point = source_widget.compute_point(overlay, Graphene.Point().init(float(x), float(y)))
                if ok:
                    margin_x = int(round(float(point.x) + 10))
                    margin_y = int(round(float(point.y) + 10))
            except Exception:
                pass

        toast_w, toast_h = self._browser_toast_size(toast)
        try:
            overlay_w = int(overlay.get_allocated_width())
            overlay_h = int(overlay.get_allocated_height())
        except Exception:
            overlay_w = 0
            overlay_h = 0
        if overlay_w > 0:
            margin_x = max(0, min(margin_x, max(0, overlay_w - toast_w - 6)))
        if overlay_h > 0:
            margin_y = max(0, min(margin_y, max(0, overlay_h - toast_h - 6)))
        try:
            toast.set_margin_start(margin_x)
            toast.set_margin_top(margin_y)
        except Exception:
            pass

    def _show_browser_toast(self, text: str, timeout_ms: int = 700, source_widget=None, x=None, y=None) -> None:
        toast = getattr(self, "browser_toast_label", None)
        if toast is None:
            return
        tid = int(getattr(self, "_browser_toast_timeout_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
            self._browser_toast_timeout_id = 0
        try:
            toast.set_text(str(text or ""))
            self._position_browser_toast(source_widget=source_widget, x=x, y=y)
            toast.set_visible(True)
        except Exception:
            return
        self._browser_toast_timeout_id = GLib.timeout_add(int(timeout_ms), self._hide_browser_toast)

    def _on_browser_scroll_value_changed(self, *_args):
        start = time.perf_counter() if PERF_LOG_ENABLED else None
        try:
            if time.monotonic() < float(getattr(self, "_programmatic_scroll_to_top_active_until", 0.0) or 0.0):
                if DEBUG_COLUMN_SORT:
                    print("[COLUMN-SORT] ignore adjustment during programmatic top-scroll", flush=True)
                return
            tid = int(getattr(self, "_scroll_to_top_idle_id", 0) or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                self._scroll_to_top_idle_id = 0
            self._browser_live_scroll_active_until = time.monotonic() + BROWSER_LIVE_SCROLL_PAUSE_SECONDS
            self._invalidate_browser_live_targets()
            if FAST_SCROLL_RENDER_ENABLED:
                self._queue_fast_scroll_full_refresh()
        finally:
            if start is not None:
                duration = time.perf_counter() - start
                self._perf_scroll_events = int(getattr(self, "_perf_scroll_events", 0) or 0) + 1
                self._perf_scroll_total = float(getattr(self, "_perf_scroll_total", 0.0) or 0.0) + duration
                if duration > float(getattr(self, "_perf_scroll_max", 0.0) or 0.0):
                    self._perf_scroll_max = duration

    def _queue_fast_scroll_full_refresh(self):
        if int(getattr(self, "_fast_scroll_render_refresh_id", 0) or 0):
            return

        def finish_after_scroll_idle():
            now = time.monotonic()
            if now < float(getattr(self, "_browser_live_scroll_active_until", 0.0) or 0.0):
                return True
            for row in list(getattr(self, "_row_widgets", ()) or ()):
                try:
                    row.full_render_bound()
                except Exception:
                    pass
            self._fast_scroll_render_refresh_id = 0
            return False

        try:
            self._fast_scroll_render_refresh_id = GLib.timeout_add(250, finish_after_scroll_idle)
        except Exception:
            self._fast_scroll_render_refresh_id = 0

    def _queue_scroll_to_top(self, reason: str = "unknown"):
        self._force_scroll_to_top_after_rebuild(reason=reason)

    def _force_scroll_to_top_after_rebuild(self, reason: str = ""):
        tid = int(getattr(self, "_scroll_to_top_idle_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
            self._scroll_to_top_idle_id = 0
        self._programmatic_scroll_to_top_active_until = time.monotonic() + 0.35
        if DEBUG_COLUMN_SORT:
            print(f"[COLUMN-SORT] scroll top queued reason={reason}", flush=True)
        try:
            self._scroll_to_top_idle_id = GLib.idle_add(self._scroll_to_top, reason)
        except Exception:
            self._scroll_to_top_idle_id = 0

    def _scroll_to_top(self, reason: str = ""):
        self._scroll_to_top_idle_id = 0
        self._programmatic_scroll_to_top_active_until = time.monotonic() + 0.35
        flags = 0
        try:
            flags = Gtk.ListScrollFlags(0)
        except Exception:
            pass
        scroll_to = getattr(getattr(self, "list_view", None), "scroll_to", None)
        if callable(scroll_to):
            pos = 0
            try:
                count = int(getattr(self, "column_view_store", None).get_n_items())
            except Exception:
                count = 0
            if not (0 <= pos < count):
                self._debug_sort_note_scroll_to_skipped(pos, count, "out_of_range")
                self._set_scroller_to_top()
                try:
                    self._scroll_to_top_idle_id = GLib.idle_add(self._scroll_to_top_final, reason)
                except Exception:
                    self._scroll_to_top_idle_id = 0
                return False
            try:
                scroll_to(pos, None, flags, None)
            except TypeError:
                try:
                    scroll_to(pos, flags, None)
                except Exception:
                    pass
            except Exception:
                pass
        self._set_scroller_to_top()
        try:
            self._scroll_to_top_idle_id = GLib.idle_add(self._scroll_to_top_final, reason)
        except Exception:
            self._scroll_to_top_idle_id = 0
        if DEBUG_COLUMN_SORT:
            print(f"[COLUMN-SORT] scroll top applied reason={reason}", flush=True)
        return False

    def _scroll_to_top_final(self, reason: str = ""):
        self._scroll_to_top_idle_id = 0
        self._programmatic_scroll_to_top_active_until = time.monotonic() + 0.2
        self._set_scroller_to_top()
        if DEBUG_COLUMN_SORT:
            print(f"[COLUMN-SORT] scroll top final correction reason={reason}", flush=True)
        return False

    def _set_scroller_to_top(self):
        try:
            vadj = self.scroller.get_vadjustment()
            if vadj:
                lower = 0.0
                try:
                    lower = float(vadj.get_lower())
                except Exception:
                    lower = 0.0
                vadj.set_value(lower)
        except Exception:
            pass

    def open_mods_manager(self):
        try:
            self._settings_ui.open_mods_manager()
        except Exception as e:
            print(f"[MODS UI] Failed to open mods manager: {e}")

    def _install_header_mod_manager_button(self):
        if getattr(self, "mod_manager_header_btn", None) is not None:
            return
        refresh_btn = getattr(self, "refresh_status_btn", None)
        if refresh_btn is None:
            return
        try:
            parent = refresh_btn.get_parent()
        except Exception:
            parent = None
        if parent is None:
            return

        btn = Gtk.Button()
        btn.set_can_focus(False)
        btn.add_css_class("flat")
        btn.set_child(Gtk.Image.new_from_icon_name("view-list-symbolic"))
        btn.set_tooltip_text("Manage installed mods")
        btn.connect("clicked", lambda *_: self.open_mods_manager())
        attach_pointer_cursor(btn)
        try:
            parent.append(btn)
            self.mod_manager_header_btn = btn
        except Exception:
            pass

    def _on_close_request(self, *_args):
        self._shutdown_cleanup()
        return False

    def _shutdown_cleanup(self):
        if getattr(self, "_shutdown_cleanup_done", False):
            return
        self._shutdown_cleanup_done = True

        try:
            self._record_server_companion_monitor_ended("shutdown")
        except Exception:
            pass

        try:
            self._stop_server_companion_polling()
        except Exception:
            pass

        try:
            self._stop_browser_live_refresh()
        except Exception:
            pass
        try:
            self._startup_live_generation = int(getattr(self, "_startup_live_generation", 0) or 0) + 1
        except Exception:
            pass
        try:
            self._status_refresh_generation = int(getattr(self, "_status_refresh_generation", 0) or 0) + 1
        except Exception:
            pass

        try:
            self._destroy_server_companion_undocked_window(reattach=False)
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

        for name in ("_executor", "_db_executor", "_update_executor", "_hi_executor", "_browser_live_executor", "_startup_live_executor"):
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
            self._dock_server_companion()
            try:
                self.main_browser_box.set_size_request(int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1)
            except Exception:
                pass
            self.server_companion_revealer.set_visible(True)
            self.server_companion_revealer.set_reveal_child(True)
            self._restore_server_companion_if_enabled()
            self._start_server_companion_polling()
        else:
            if not bool(getattr(self, "_server_companion_docked", True)):
                self._destroy_server_companion_undocked_window(reattach=True)
            self.server_companion_revealer.set_reveal_child(False)
            self.server_companion_revealer.set_visible(False)
            self._collapse_server_companion_dock_space()
            self._record_server_companion_monitor_ended("paused")
            self._stop_server_companion_polling()
        self._refresh_server_companion_monitor_highlight()
        self._refresh_server_companion_power_controls()

    def _refresh_server_companion_power_controls(self):
        show_btn = getattr(self, "server_companion_show_btn", None)
        if show_btn is not None:
            show_btn.set_visible(not bool(self.settings.get("show_server_companion", False)))

    def toggle_server_companion(self):
        self.set_server_companion_enabled(
            not bool(self.server_companion_revealer.get_reveal_child())
        )

    def set_server_companion_enabled(self, enabled: bool):
        enabled = bool(enabled)
        self.settings["show_server_companion"] = enabled
        try:
            save_settings(self.settings)
        except Exception:
            pass

        widget = getattr(self, "_settings_widgets", {}).get("show_server_companion")
        if widget is not None:
            old_guard = bool(getattr(self, "_settings_update_guard", False))
            try:
                self._settings_update_guard = True
                widget.set_active(enabled)
            except Exception:
                pass
            finally:
                self._settings_update_guard = old_guard

        self.set_server_companion_visible(enabled)

    def _server_companion_identity_for_obj(self, obj: ServerObject | None) -> tuple[str, int] | None:
        if not isinstance(obj, ServerObject):
            return None
        ip = str(getattr(obj, "ip", "") or "").strip()
        try:
            gport = int(getattr(obj, "gport", 0) or 0)
        except Exception:
            gport = 0
        if not ip or gport <= 0:
            return None
        return ip, gport

    def _is_server_companion_monitoring_obj(self, obj: ServerObject | None) -> bool:
        if not bool(self.settings.get("show_server_companion", False)):
            return False
        current = self._server_companion_identity_for_obj(getattr(self, "_server_companion_obj", None))
        candidate = self._server_companion_identity_for_obj(obj)
        return current is not None and current == candidate

    def _refresh_server_companion_monitor_highlight(self):
        try:
            refresh = getattr(getattr(self, "list_view", None), "refresh_monitor_highlights", None)
            if callable(refresh):
                refresh()
        except Exception:
            pass

    def toggle_server_companion_dock(self):
        self._debug_server_companion_dock("dock toggle callback invoked")
        if bool(getattr(self, "_server_companion_docked", True)):
            self._undock_server_companion()
        else:
            self._dock_server_companion()

    def _debug_server_companion_dock(self, message: str):
        if not DEBUG_SC_DOCK:
            return
        try:
            print(f"[SC-DOCK] {message}", flush=True)
        except Exception:
            pass

    def _debug_server_companion_alert(self, message: str):
        if not DEBUG_SC_ALERTS:
            return
        try:
            print(f"[SC-ALERT] {message}", flush=True)
        except Exception:
            pass

    def _server_companion_alert_server_label(self, snapshot: dict | None = None) -> str:
        snapshot = snapshot if isinstance(snapshot, dict) else getattr(self, "_server_companion_snapshot", None)
        if not isinstance(snapshot, dict):
            snapshot = {}
        name = str(snapshot.get("name") or "").strip() or "<unknown>"
        ip = str(snapshot.get("ip") or "").strip()
        qport = str(snapshot.get("qport") or "").strip()
        address = f"{ip}:{qport}" if ip and qport else (ip or "<unknown>")
        return f"{name} ({address})"

    def _recover_server_companion_dock_after_undock_failure(self, panel=None, revealer=None, failed_window=None, error=None):
        print(f"[SC-DOCK] WARNING: undock failed; restoring docked Server Companion: {error!r}", flush=True)
        panel = panel or getattr(self, "server_companion_panel", None)
        revealer = revealer or getattr(self, "server_companion_revealer", None)
        win = failed_window or getattr(self, "_server_companion_undocked_window", None)

        if win is not None:
            handler_id = int(getattr(self, "_server_companion_undocked_close_handler_id", 0) or 0)
            if handler_id:
                try:
                    win.disconnect(handler_id)
                except Exception:
                    pass
            try:
                win.set_child(None)
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        self._server_companion_undocked_window = None
        self._server_companion_undocked_close_handler_id = 0
        self._server_companion_docked = True
        if panel is not None:
            try:
                panel.set_docked(True)
            except Exception:
                pass
        if panel is not None and revealer is not None:
            try:
                revealer.set_child(panel)
            except Exception as exc:
                print(f"[SC-DOCK] WARNING: failed to reattach Server Companion panel: {exc!r}", flush=True)
            if bool(self.settings.get("show_server_companion", False)):
                try:
                    revealer.set_visible(True)
                    revealer.set_reveal_child(True)
                    self.main_browser_box.set_size_request(int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1)
                except Exception:
                    pass
        self._refresh_server_companion_monitor_highlight()

    def _undock_server_companion(self):
        self._debug_server_companion_dock("starting undock")
        if not bool(self.settings.get("show_server_companion", False)):
            self._debug_server_companion_dock("undock skipped: Server Companion disabled")
            return
        if not bool(getattr(self, "_server_companion_docked", True)):
            win = getattr(self, "_server_companion_undocked_window", None)
            if win is not None:
                try:
                    self._debug_server_companion_dock("undock requested while already undocked; presenting existing window")
                    win.present()
                except Exception:
                    pass
            return

        panel = getattr(self, "server_companion_panel", None)
        revealer = getattr(self, "server_companion_revealer", None)
        if panel is None or revealer is None:
            self._debug_server_companion_dock("undock skipped: missing panel or revealer")
            return

        self._server_companion_reparenting = True
        win = None
        try:
            revealer.set_reveal_child(False)
            revealer.set_child(None)
            revealer.set_visible(False)

            win = Gtk.Window(application=self.get_application(), title="DZLL Server Companion")
            self._debug_server_companion_dock("detached window created")
            win.set_resizable(False)
            win.set_child(panel)
            handler_id = win.connect("close-request", self._on_server_companion_undocked_close_request)

            self._server_companion_undocked_window = win
            self._server_companion_undocked_close_handler_id = handler_id
            self._server_companion_docked = False
            panel.set_docked(False)
            self._update_server_companion_undocked_size()
            self._collapse_server_companion_dock_space()
            win.present()
            self._debug_server_companion_dock("present called")
            self._start_server_companion_polling()
        except Exception as exc:
            self._debug_server_companion_dock(f"undock exception/fallback: {exc!r}")
            self._recover_server_companion_dock_after_undock_failure(panel=panel, revealer=revealer, failed_window=win, error=exc)
        finally:
            self._server_companion_reparenting = False

    def _dock_server_companion(self):
        self._debug_server_companion_dock("docking/reattaching")
        if bool(getattr(self, "_server_companion_docked", True)):
            self._debug_server_companion_dock("dock skipped: already docked")
            return

        panel = getattr(self, "server_companion_panel", None)
        revealer = getattr(self, "server_companion_revealer", None)
        if panel is None or revealer is None:
            self._debug_server_companion_dock("dock skipped: missing panel or revealer")
            return

        self._destroy_server_companion_undocked_window(reattach=False)
        self._server_companion_reparenting = True
        try:
            revealer.set_child(panel)
            self._server_companion_docked = True
            panel.set_docked(True)
            if bool(self.settings.get("show_server_companion", False)):
                revealer.set_visible(True)
                revealer.set_reveal_child(True)
                try:
                    self.main_browser_box.set_size_request(int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1)
                except Exception:
                    pass
                self._start_server_companion_polling()
            else:
                revealer.set_reveal_child(False)
                revealer.set_visible(False)
        finally:
            self._server_companion_reparenting = False

    def _collapse_server_companion_dock_space(self):
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

    def _on_server_companion_undocked_close_request(self, *_args):
        if bool(getattr(self, "_server_companion_reparenting", False)):
            return False
        self._debug_server_companion_dock("undocked window close requested")
        self._dock_server_companion()
        return True

    def _update_server_companion_undocked_size(self) -> None:
        if bool(getattr(self, "_server_companion_docked", True)):
            return
        win = getattr(self, "_server_companion_undocked_window", None)
        panel = getattr(self, "server_companion_panel", None)
        if win is None or panel is None:
            return
        height = (
            ServerCompanionPanel.UNDOCKED_EXPANDED_HEIGHT
            if panel.restart_learning_visible()
            else ServerCompanionPanel.UNDOCKED_COMPACT_HEIGHT
        )
        try:
            win.set_default_size(ServerCompanionPanel.WIDTH, height)
            win.set_size_request(ServerCompanionPanel.WIDTH, height)
            win.queue_resize()
        except Exception:
            pass

    def _destroy_server_companion_undocked_window(self, reattach: bool = True):
        self._debug_server_companion_dock(f"destroy undocked window reattach={bool(reattach)}")
        win = getattr(self, "_server_companion_undocked_window", None)
        panel = getattr(self, "server_companion_panel", None)
        revealer = getattr(self, "server_companion_revealer", None)

        if win is not None:
            handler_id = int(getattr(self, "_server_companion_undocked_close_handler_id", 0) or 0)
            if handler_id:
                try:
                    win.disconnect(handler_id)
                except Exception:
                    pass
            try:
                win.set_child(None)
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        self._server_companion_undocked_window = None
        self._server_companion_undocked_close_handler_id = 0
        self._server_companion_docked = True
        if panel is not None:
            panel.set_docked(True)
        if reattach and panel is not None and revealer is not None:
            try:
                revealer.set_child(panel)
            except Exception:
                pass

    def _server_companion_persisted_from_obj(self, obj: ServerObject) -> dict:
        try:
            gport = int(getattr(obj, "gport", 0) or 0)
        except Exception:
            gport = 0
        try:
            qport = int(getattr(obj, "qport", 0) or 0)
        except Exception:
            qport = 0
        try:
            mod_count = int(getattr(obj, "mod_count", 0) or 0)
        except Exception:
            mod_count = 0
        try:
            players = int(getattr(obj, "players", 0) or 0)
        except Exception:
            players = 0
        try:
            max_players = int(getattr(obj, "max_players", 0) or 0)
        except Exception:
            max_players = 0
        try:
            ping = int(getattr(obj, "ping", -1) or -1)
        except Exception:
            ping = -1
        try:
            bm_rank = int(getattr(obj, "bm_rank", 999999999) or 999999999)
        except Exception:
            bm_rank = 999999999
        try:
            timewarp = float(getattr(obj, "timewarp", 1.0) or 1.0)
        except Exception:
            timewarp = 1.0

        return {
            "ip": str(getattr(obj, "ip", "") or ""),
            "gport": gport,
            "qport": qport,
            "name": str(getattr(obj, "name", "") or ""),
            "map_name": str(getattr(obj, "map_name", "") or ""),
            "map": str(getattr(obj, "map_name", "") or ""),
            "mods_json": str(getattr(obj, "mods_json", "") or ""),
            "mod_count": mod_count,
            "mods_preview": str(getattr(obj, "mods_preview", "") or ""),
            "password": bool(getattr(obj, "password", False)),
            "third_person": bool(getattr(obj, "third_person", False)),
            "country": str(getattr(obj, "country", "") or ""),
            "time": str(getattr(obj, "time", "") or ""),
            "timewarp": timewarp,
            "players": players,
            "max_players": max_players,
            "ping": ping,
            "bm_rank": bm_rank,
        }

    def _server_companion_obj_from_persisted(self, data: dict):
        if not isinstance(data, dict):
            return None
        ip = str(data.get("ip") or "").strip()
        if not ip:
            return None
        try:
            gport = int(data.get("gport", 0) or 0)
        except Exception:
            gport = 0
        if gport <= 0:
            return None
        try:
            qport = int(data.get("qport", 0) or 0)
        except Exception:
            qport = 0
        if qport <= 0:
            qport = gport + 1

        key = fav_key(ip, gport)
        current = self._obj_by_key.get(key)
        if isinstance(current, ServerObject):
            return current

        try:
            mod_count = int(data.get("mod_count", 0) or 0)
        except Exception:
            mod_count = 0
        try:
            players = int(data.get("players", 0) or 0)
        except Exception:
            players = 0
        try:
            max_players = int(data.get("max_players", 0) or 0)
        except Exception:
            max_players = 0
        try:
            ping = int(data.get("ping", -1) or -1)
        except Exception:
            ping = -1
        try:
            bm_rank = int(data.get("bm_rank", 999999999) or 999999999)
        except Exception:
            bm_rank = 999999999
        try:
            timewarp = float(data.get("timewarp", 1.0) or 1.0)
        except Exception:
            timewarp = 1.0

        lp_ts = self.last_played.get(key)
        played_disp = human_last_played(lp_ts) if lp_ts else ""

        return ServerObject(
            fav=bool(self.favorites.get(key, False)),
            password=bool(data.get("password", False)),
            third_person=bool(data.get("third_person", False)),
            name=str(data.get("name") or "Unknown Server"),
            country=str(data.get("country") or ""),
            ip=ip,
            gport=gport,
            qport=qport,
            mod_count=mod_count,
            mods_preview=str(data.get("mods_preview") or ""),
            mods_json=str(data.get("mods_json") or ""),
            time=str(data.get("time") or "--:--"),
            timewarp=timewarp,
            played=played_disp,
            map_name=str(data.get("map_name") or data.get("map") or ""),
            players=players,
            max_players=max_players,
            ping=ping,
            bm_rank=bm_rank,
        )

    def _restore_server_companion_if_enabled(self):
        if not bool(getattr(self, "_server_companion_rows_loaded", False)):
            return
        if getattr(self, "_server_companion_snapshot", None) is not None:
            return
        if not bool(self.settings.get("show_server_companion", False)):
            return
        panel = getattr(self, "server_companion_panel", None)
        if panel is None or panel.get_root() is None:
            return
        data = getattr(self, "_last_server_companion_saved", None)
        if not data:
            return
        obj = self._server_companion_obj_from_persisted(data)
        if obj is None:
            return
        self.set_server_companion_server(obj, persist=False)

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
            queue = int(getattr(obj, "queue", -1))
        except Exception:
            queue = -1
        try:
            mod_count = int(getattr(obj, "mod_count", 0))
        except Exception:
            mod_count = 0

        mode_parts = []
        mode_parts.append("3PP" if bool(getattr(obj, "third_person", False)) else "1PP")
        mode_parts.append("Password" if bool(getattr(obj, "password", False)) else "No password")
        mode_parts.append(f"Mods: {mod_count}")

        snapshot = {
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
        if queue >= 0:
            snapshot["queue"] = queue
        return snapshot

    def _server_companion_restart_learning_key(self):
        obj = getattr(self, "_server_companion_obj", None)
        if not isinstance(obj, ServerObject):
            return None
        return companion_restart_learning.make_server_key(
            str(getattr(obj, "ip", "") or ""),
            int(getattr(obj, "gport", 0) or 0),
        )

    def _record_server_companion_monitor_started(self, key: str | None = None) -> None:
        key = key or self._server_companion_restart_learning_key()
        if not key:
            return
        self._companion_restart_learning = companion_restart_learning.record_monitor_started(
            getattr(self, "_companion_restart_learning", None),
            key,
        )
        try:
            save_companion_restart_learning(self._companion_restart_learning)
        except Exception:
            pass
        last_saved = getattr(self, "_server_companion_monitor_last_saved_at", None)
        if not isinstance(last_saved, dict):
            last_saved = {}
            self._server_companion_monitor_last_saved_at = last_saved
        last_saved[str(key)] = int(time.time())

    def _record_server_companion_monitor_heartbeat(self, key: str | None = None) -> None:
        key = key or self._server_companion_restart_learning_key()
        if not key:
            return
        now = int(time.time())
        self._companion_restart_learning = companion_restart_learning.record_monitor_heartbeat(
            getattr(self, "_companion_restart_learning", None),
            key,
            now=now,
        )
        last_saved = getattr(self, "_server_companion_monitor_last_saved_at", None)
        if not isinstance(last_saved, dict):
            last_saved = {}
            self._server_companion_monitor_last_saved_at = last_saved
        previous_saved_at = self._safe_positive_int(last_saved.get(str(key)))
        if now - previous_saved_at < int(companion_restart_learning.MONITOR_HEARTBEAT_SECONDS):
            return
        try:
            save_companion_restart_learning(self._companion_restart_learning)
            last_saved[str(key)] = now
        except Exception:
            pass

    def _record_server_companion_monitor_ended(self, reason: str, key: str | None = None) -> None:
        key = key or self._server_companion_restart_learning_key()
        if not key:
            return
        self._companion_restart_learning = companion_restart_learning.record_monitor_ended(
            getattr(self, "_companion_restart_learning", None),
            key,
            reason,
        )
        try:
            save_companion_restart_learning(self._companion_restart_learning)
        except Exception:
            pass
        last_saved = getattr(self, "_server_companion_monitor_last_saved_at", None)
        if isinstance(last_saved, dict):
            last_saved[str(key)] = int(time.time())

    def _server_companion_restart_learning_summary(self):
        key = self._server_companion_restart_learning_key()
        if not key:
            return None
        snapshot = getattr(self, "_server_companion_snapshot", None)
        server_online = None
        if isinstance(snapshot, dict) and "online" in snapshot:
            server_online = bool(snapshot.get("online", False))
        return companion_restart_learning.summarize_server(
            getattr(self, "_companion_restart_learning", None),
            key,
            server_online=server_online,
            offline_since_at=getattr(self, "_server_companion_offline_since_wall", None),
        )

    def _server_companion_restart_alert_usability_summary(self):
        key = self._server_companion_restart_learning_key()
        return companion_restart_learning.summarize_alert_usability(
            getattr(self, "_companion_restart_learning", None),
            key,
        )

    def _refresh_server_companion_restart_learning_summary(self) -> None:
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_restart_learning_summary(self._server_companion_restart_learning_summary())
            if hasattr(panel, "set_restart_alert_usability"):
                panel.set_restart_alert_usability(self._server_companion_restart_alert_usability_summary())
            self._update_server_companion_undocked_size()

    def _record_server_companion_restart_learning_outage(
        self,
        *,
        offline_at: int,
        online_at: int,
        duration_seconds: int,
        snapshot: dict,
    ) -> int | None:
        key = self._server_companion_restart_learning_key()
        if not key:
            return None
        outage = {
            "offline_at": int(offline_at),
            "online_at": int(online_at),
            "duration_seconds": int(duration_seconds),
            "name": str((snapshot or {}).get("name") or ""),
            "map": str((snapshot or {}).get("map") or ""),
        }
        self._companion_restart_learning = companion_restart_learning.record_confirmed_outage(
            getattr(self, "_companion_restart_learning", None),
            key,
            outage,
        )
        try:
            save_companion_restart_learning(self._companion_restart_learning)
        except Exception:
            pass
        self._refresh_server_companion_restart_learning_summary()
        return companion_restart_learning.scheduled_outage_alert_threshold(
            self._companion_restart_learning,
            key,
            offline_at,
            online_at,
        )

    def _reset_server_companion_visible_zero_state(self) -> None:
        self._server_companion_visible_zero_state = None

    def _feed_server_companion_visible_restart_detector(self, players: int, snapshot: dict) -> bool:
        key = self._server_companion_restart_learning_key()
        if not key:
            self._reset_server_companion_visible_zero_state()
            self._debug_server_companion_alert("query-visible detector skipped: no restart-learning key")
            return False

        now_wall = int(time.time())
        try:
            players = max(0, int(players))
        except Exception:
            players = 0
        try:
            max_players = max(0, int((snapshot or {}).get("max_players", 0) or 0))
        except Exception:
            max_players = 0

        state = getattr(self, "_server_companion_visible_zero_state", None)
        if not isinstance(state, dict) or state.get("key") != str(key):
            self._debug_server_companion_alert(
                f"query-visible detector state reset: server={self._server_companion_alert_server_label(snapshot)} "
                f"players={players}/{max_players}"
            )
            state = {
                "key": str(key),
                "last_positive_at": 0,
                "last_positive_players": 0,
                "zero_start_at": 0,
                "pre_zero_players": 0,
            }

        zero_start_at = self._safe_positive_int(state.get("zero_start_at"))
        if players > 0:
            query_visible_alert_usable = False
            if zero_start_at > 0:
                duration_seconds = now_wall - zero_start_at
                min_zero = int(companion_restart_learning.MIN_QUERY_VISIBLE_ZERO_SECONDS)
                max_zero = int(companion_restart_learning.MAX_QUERY_VISIBLE_ZERO_SECONDS)
                self._debug_server_companion_alert(
                    f"query-visible players returned: server={self._server_companion_alert_server_label(snapshot)} "
                    f"players={players}/{max_players} zero_seconds={duration_seconds} "
                    f"valid_window={min_zero}-{max_zero}"
                )
                if min_zero <= duration_seconds <= max_zero:
                    self._companion_restart_learning = companion_restart_learning.record_query_visible_restart(
                        getattr(self, "_companion_restart_learning", None),
                        key,
                        {
                            "zero_start_at": zero_start_at,
                            "players_return_at": now_wall,
                            "duration_seconds": duration_seconds,
                            "pre_zero_players": self._safe_positive_int(state.get("pre_zero_players")),
                            "return_players": players,
                            "name": str((snapshot or {}).get("name") or ""),
                            "map": str((snapshot or {}).get("map") or ""),
                        },
                    )
                    try:
                        save_companion_restart_learning(self._companion_restart_learning)
                    except Exception:
                        pass
                    self._refresh_server_companion_restart_learning_summary()
                    alert_usability = self._server_companion_restart_alert_usability_summary()
                    query_visible_alert_usable = (
                        isinstance(alert_usability, dict)
                        and bool(alert_usability.get("usable", False))
                        and str(alert_usability.get("mode") or "") == "query_visible"
                    )
                    self._debug_server_companion_alert(
                        f"query-visible restart event recorded: usable={bool(query_visible_alert_usable)} "
                        f"mode={str((alert_usability or {}).get('mode') or '')!r} "
                        f"message={str((alert_usability or {}).get('message') or '')!r}"
                    )
                else:
                    self._debug_server_companion_alert("query-visible restart event not recorded: zero duration outside valid window")
            self._server_companion_visible_zero_state = {
                "key": str(key),
                "last_positive_at": now_wall,
                "last_positive_players": players,
                "zero_start_at": 0,
                "pre_zero_players": 0,
            }
            return query_visible_alert_usable

        if zero_start_at > 0:
            if now_wall - zero_start_at > int(companion_restart_learning.MAX_QUERY_VISIBLE_ZERO_SECONDS):
                self._debug_server_companion_alert(
                    f"query-visible zero state expired: zero_seconds={now_wall - zero_start_at}"
                )
                self._reset_server_companion_visible_zero_state()
            else:
                self._server_companion_visible_zero_state = state
                self._debug_server_companion_alert(
                    f"query-visible still zero: server={self._server_companion_alert_server_label(snapshot)} "
                    f"players={players}/{max_players} zero_seconds={now_wall - zero_start_at}"
                )
            return False

        last_positive_at = self._safe_positive_int(state.get("last_positive_at"))
        last_positive_players = self._safe_positive_int(state.get("last_positive_players"))
        if (
            last_positive_at > 0
            and last_positive_players > 0
            and now_wall - last_positive_at <= int(QUERY_VISIBLE_RECENT_PLAYERS_SECONDS)
        ):
            state["zero_start_at"] = now_wall
            state["pre_zero_players"] = last_positive_players
            self._debug_server_companion_alert(
                f"query-visible zero observed: server={self._server_companion_alert_server_label(snapshot)} "
                f"previous_players={last_positive_players} current_players={players}/{max_players}"
            )
        self._server_companion_visible_zero_state = state
        return False

    def _append_server_companion_observation_sample(self, key: str | None, online: bool) -> None:
        if not key:
            return
        samples = getattr(self, "_server_companion_observation_samples", None)
        if samples is None:
            samples = deque(maxlen=5000)
            self._server_companion_observation_samples = samples
        samples.append({
            "wall_at": int(time.time()),
            "online": bool(online),
            "key": str(key),
        })

    def _maybe_record_server_companion_expected_window_miss(self) -> None:
        key = self._server_companion_restart_learning_key()
        if not key or bool(getattr(self, "_server_companion_poll_paused", False)):
            return

        state = companion_restart_learning.normalize_state(
            getattr(self, "_companion_restart_learning", None)
        )
        server = (state.get("servers") or {}).get(str(key))
        if not isinstance(server, dict):
            return

        cycle_seconds = self._safe_positive_int(server.get("learned_cycle_seconds"))
        last_restart_at = self._safe_positive_int(server.get("last_restart_event_at"))
        confidence = self._safe_float_value(server.get("confidence"))
        generation = self._safe_positive_int(server.get("model_generation")) or 1
        if (
            cycle_seconds <= 0
            or last_restart_at <= 0
            or confidence < companion_restart_learning.MODEL_RESET_CONFIDENCE
        ):
            return

        now = int(time.time())
        tolerance = companion_restart_learning.projected_window_tolerance_seconds(cycle_seconds)
        expected_at = int(last_restart_at) + int(cycle_seconds)
        while expected_at + int(cycle_seconds) + tolerance <= now:
            expected_at += int(cycle_seconds)
        window_start = expected_at - tolerance
        window_end = expected_at + tolerance
        if window_end > now:
            return

        if self._server_companion_expected_miss_exists(server, expected_at, cycle_seconds, generation):
            return

        if self._server_companion_restart_event_overlaps(server, window_start, window_end):
            return

        persistent_coverage = companion_restart_learning.server_monitored_window(
            server,
            window_start,
            window_end,
        )
        if not bool(persistent_coverage.get("covered", False)):
            return

        max_gap = max(2 * int(COMPANION_POLL_ONLINE_SECONDS) + 10, 60)
        samples = [
            sample for sample in (getattr(self, "_server_companion_observation_samples", None) or [])
            if isinstance(sample, dict)
            and sample.get("key") == str(key)
            and window_start - max_gap <= self._safe_positive_int(sample.get("wall_at")) <= window_end + max_gap
        ]
        if len(samples) < 3:
            return

        samples.sort(key=lambda item: self._safe_positive_int(item.get("wall_at")))
        inside_samples = [
            sample for sample in samples
            if window_start <= self._safe_positive_int(sample.get("wall_at")) <= window_end
        ]
        if len(inside_samples) < 3:
            return
        if any(not bool(sample.get("online", False)) for sample in samples):
            return

        first_at = self._safe_positive_int(samples[0].get("wall_at"))
        last_at = self._safe_positive_int(samples[-1].get("wall_at"))
        if first_at > window_start + max_gap or last_at < window_end - max_gap:
            return

        max_seen_gap = 0
        for prev, cur in zip(samples, samples[1:]):
            gap = self._safe_positive_int(cur.get("wall_at")) - self._safe_positive_int(prev.get("wall_at"))
            max_seen_gap = max(max_seen_gap, gap)
        if max_seen_gap > max_gap:
            return

        covered_start = max(window_start, first_at)
        covered_end = min(window_end, last_at)
        coverage_ratio = (covered_end - covered_start) / float(max(1, window_end - window_start))
        if coverage_ratio < 0.75:
            return

        self._companion_restart_learning = companion_restart_learning.record_expected_window_miss(
            state,
            key,
            {
                "expected_at": expected_at,
                "window_start": window_start,
                "window_end": window_end,
                "cycle_seconds": cycle_seconds,
                "model_generation": generation,
                "observed_poll_count": len(inside_samples),
                "coverage_ratio": min(coverage_ratio, self._safe_float_value(persistent_coverage.get("coverage_ratio"))),
                "max_gap_seconds": max(max_seen_gap, self._safe_positive_int(persistent_coverage.get("max_gap_seconds"))),
            },
        )
        try:
            save_companion_restart_learning(self._companion_restart_learning)
        except Exception:
            pass

    def _server_companion_expected_miss_exists(
        self,
        server: dict,
        expected_at: int,
        cycle_seconds: int,
        generation: int,
    ) -> bool:
        for miss in server.get("expected_window_misses") or []:
            if not isinstance(miss, dict):
                continue
            if (
                self._safe_positive_int(miss.get("expected_at")) == int(expected_at)
                and self._safe_positive_int(miss.get("cycle_seconds")) == int(cycle_seconds)
                and self._safe_positive_int(miss.get("model_generation")) == int(generation)
            ):
                return True
        return False

    def _server_companion_restart_event_overlaps(self, server: dict, window_start: int, window_end: int) -> bool:
        for event in server.get("restart_events") or []:
            if not isinstance(event, dict):
                continue
            offline_at = self._safe_positive_int(event.get("offline_at"))
            online_at = self._safe_positive_int(event.get("online_at"))
            if offline_at <= 0 or online_at <= 0:
                continue
            if offline_at <= window_end and online_at >= window_start:
                return True
        return False

    def _safe_positive_int(self, value) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    def _safe_float_value(self, value) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def set_server_companion_server(self, obj: ServerObject, persist: bool = True):
        old_key = self._server_companion_restart_learning_key()
        snapshot = self._server_companion_snapshot_from_obj(obj)
        new_key = companion_restart_learning.make_server_key(
            str(getattr(obj, "ip", "") or ""),
            int(getattr(obj, "gport", 0) or 0),
        )
        if old_key and old_key != new_key:
            self._record_server_companion_monitor_ended("server_switch", old_key)
        self._server_companion_poll_token += 1
        self._server_companion_poll_paused = False
        self._server_companion_last_online = None
        self._server_companion_consecutive_offline_polls = 0
        self._server_companion_visible_snapshot = dict(snapshot)
        self._server_companion_observation_samples.clear()
        self._reset_server_companion_visible_zero_state()
        self._server_companion_offline_since = None
        self._server_companion_offline_since_wall = None
        self._server_companion_alert_armed = False
        self._set_server_companion_poll_interval(COMPANION_POLL_ONLINE_SECONDS)
        self._server_companion_obj = obj
        self._server_companion_snapshot = snapshot
        self._record_server_companion_monitor_started(new_key)
        if persist:
            data = self._server_companion_persisted_from_obj(obj)
            self._last_server_companion_saved = data
            try:
                save_last_companion_server(data)
            except Exception:
                pass
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_server_snapshot(snapshot)
            self._refresh_server_companion_restart_learning_summary()
            panel.set_polling_paused(False)
        self._refresh_server_companion_monitor_highlight()
        self._start_server_companion_polling()

    def clear_server_companion(self):
        self._server_companion_poll_token += 1
        self._record_server_companion_monitor_ended("clear")
        self._server_companion_snapshot = None
        self._server_companion_obj = None
        self._last_server_companion_saved = {}
        self._server_companion_last_online = None
        self._server_companion_consecutive_offline_polls = 0
        self._server_companion_visible_snapshot = None
        self._server_companion_observation_samples.clear()
        self._reset_server_companion_visible_zero_state()
        self._server_companion_offline_since = None
        self._server_companion_offline_since_wall = None
        self._server_companion_alert_armed = False
        self._server_companion_restart_warning_fired.clear()
        self._set_server_companion_poll_interval(COMPANION_POLL_ONLINE_SECONDS)
        self._stop_server_companion_polling()
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            if hasattr(panel, "set_restart_alert_usability"):
                panel.set_restart_alert_usability(self._server_companion_restart_alert_usability_summary())
            panel.clear_server()
        clear_last_companion_server()
        self._refresh_server_companion_monitor_highlight()

    def join_server_companion(self):
        obj = getattr(self, "_server_companion_obj", None)
        if not isinstance(obj, ServerObject):
            return
        try:
            key = fav_key(obj.ip, int(obj.gport))
            current = self._obj_by_key.get(key)
            if isinstance(current, ServerObject):
                obj = current
                self._server_companion_obj = current
        except Exception:
            pass
        self._join_server_for_obj(obj)

    def _server_companion_should_poll(self) -> bool:
        if not bool(self.settings.get("show_server_companion", False)):
            self._debug_server_companion_alert("poll skipped: Server Companion disabled")
            return False
        if getattr(self, "_server_companion_snapshot", None) is None:
            self._debug_server_companion_alert("poll skipped: no monitored server snapshot")
            return False
        if bool(getattr(self, "_server_companion_poll_paused", False)):
            self._debug_server_companion_alert("poll skipped: polling paused")
            return False
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None and panel.get_root() is not None:
            self._debug_server_companion_alert(
                f"poll allowed: panel has root docked={bool(getattr(self, '_server_companion_docked', True))}"
            )
            return True
        docked_revealed = bool(getattr(self, "_server_companion_docked", True)) and bool(
            getattr(getattr(self, "server_companion_revealer", None), "get_reveal_child", lambda: False)()
        )
        self._debug_server_companion_alert(f"poll {'allowed' if docked_revealed else 'skipped'}: docked revealer visible={docked_revealed}")
        return docked_revealed

    def _start_server_companion_polling(self):
        if not self._server_companion_should_poll():
            return
        if not self._server_companion_poll_timer_id:
            self._server_companion_poll_timer_id = GLib.timeout_add_seconds(
                self._server_companion_poll_interval_secs,
                self._server_companion_poll_tick,
            )
        self._submit_server_companion_poll()

    def _set_server_companion_poll_interval(self, interval_secs: int):
        interval_secs = int(interval_secs)
        if interval_secs == int(getattr(self, "_server_companion_poll_interval_secs", 0) or 0):
            return
        self._server_companion_poll_interval_secs = interval_secs
        timer_id = int(getattr(self, "_server_companion_poll_timer_id", 0) or 0)
        if timer_id:
            try:
                GLib.source_remove(timer_id)
            except Exception:
                pass
            self._server_companion_poll_timer_id = 0
        if self._server_companion_should_poll():
            self._server_companion_poll_timer_id = GLib.timeout_add_seconds(
                self._server_companion_poll_interval_secs,
                self._server_companion_poll_tick,
            )

    def _stop_server_companion_polling(self):
        self._server_companion_consecutive_offline_polls = 0
        self._server_companion_visible_snapshot = None
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
        now = time.monotonic()
        if bool((info or {}).get("ok", False)):
            self._debug_server_companion_alert(
                f"poll result: online name={str(snapshot.get('name') or '')!r} alert_enabled={bool(getattr(self, '_server_companion_restart_alert_enabled', False))}"
            )
            snapshot["ping"] = int((info or {}).get("ping_ms", snapshot.get("ping", -1)) or 0)
            snapshot["players"] = int((info or {}).get("players", snapshot.get("players", 0)) or 0)
            snapshot["max_players"] = int((info or {}).get("max_players", snapshot.get("max_players", 0)) or 0)
            queue = (info or {}).get("queue")
            if queue is None:
                snapshot.pop("queue", None)
                queue_value = -1
            else:
                queue_value = int(queue)
                snapshot["queue"] = queue_value
            companion_obj = getattr(self, "_server_companion_obj", None)
            if isinstance(companion_obj, ServerObject):
                companion_obj.queue = queue_value
            live_time = str((info or {}).get("time") or "")
            if live_time:
                snapshot["time"] = live_time
            snapshot["online"] = True
            query_visible_alert_usable = self._feed_server_companion_visible_restart_detector(
                snapshot.get("players", 0),
                snapshot,
            )
            query_visible_usability = self._server_companion_restart_alert_usability_summary()
            self._debug_server_companion_alert(
                f"query-visible alert decision: server={self._server_companion_alert_server_label(snapshot)} "
                f"players={int(snapshot.get('players', 0) or 0)}/{int(snapshot.get('max_players', 0) or 0)} "
                f"enabled={bool(getattr(self, '_server_companion_restart_alert_enabled', False))} "
                f"detector_usable={bool(query_visible_alert_usable)} "
                f"mode={str((query_visible_usability or {}).get('mode') or '')!r} "
                f"message={str((query_visible_usability or {}).get('message') or '')!r}"
            )
            offline_since = getattr(self, "_server_companion_offline_since", None)
            offline_long_enough = (
                offline_since is not None
                and now - float(offline_since) >= COMPANION_ALERT_REARM_OFFLINE_SECONDS
            )
            outage_alert_usable = False
            learned_restart_long_enough = False
            if offline_since is not None:
                online_at = int(time.time())
                duration_seconds = max(0, int(round(now - float(offline_since))))
                offline_since_wall = getattr(self, "_server_companion_offline_since_wall", None)
                try:
                    offline_at = int(float(offline_since_wall))
                except Exception:
                    offline_at = online_at - duration_seconds
                learned_threshold = self._record_server_companion_restart_learning_outage(
                    offline_at=offline_at,
                    online_at=online_at,
                    duration_seconds=duration_seconds,
                    snapshot=snapshot,
                )
                alert_usability = self._server_companion_restart_alert_usability_summary()
                outage_alert_usable = (
                    isinstance(alert_usability, dict)
                    and bool(alert_usability.get("usable", False))
                    and str(alert_usability.get("mode") or "") == "outage"
                )
                learned_restart_long_enough = (
                    learned_threshold is not None
                    and duration_seconds >= learned_threshold
                )
            online_again_alert_fired = False
            if (
                bool(getattr(self, "_server_companion_alert_armed", False))
                or offline_long_enough
                or learned_restart_long_enough
            ) and bool(
                getattr(self, "_server_companion_restart_alert_enabled", False)
            ):
                self._debug_server_companion_alert(
                    "back-online alert emitted: "
                    f"armed={bool(getattr(self, '_server_companion_alert_armed', False))} "
                    f"offline_long_enough={bool(offline_long_enough)} "
                    f"learned_restart_long_enough={bool(learned_restart_long_enough)}"
                )
                self._server_companion_alert_back_online(snapshot, alert_type="back online")
                online_again_alert_fired = True
            elif offline_since is not None:
                self._debug_server_companion_alert(
                    "back-online alert not emitted: "
                    f"enabled={bool(getattr(self, '_server_companion_restart_alert_enabled', False))} "
                    f"armed={bool(getattr(self, '_server_companion_alert_armed', False))} "
                    f"offline_seconds={now - float(offline_since):.1f} "
                    f"required_seconds={int(COMPANION_ALERT_REARM_OFFLINE_SECONDS)} "
                    f"learned_restart_long_enough={bool(learned_restart_long_enough)}"
                )
            if (
                not online_again_alert_fired
                and query_visible_alert_usable
                and bool(getattr(self, "_server_companion_restart_alert_enabled", False))
            ):
                self._debug_server_companion_alert("query-visible back-online alert emitted")
                self._server_companion_alert_back_online(snapshot, alert_type="query-visible rejoin")
            elif not online_again_alert_fired and not query_visible_alert_usable:
                self._debug_server_companion_alert("query-visible alert not emitted: detector/model not usable")
            elif query_visible_alert_usable and not bool(getattr(self, "_server_companion_restart_alert_enabled", False)):
                self._debug_server_companion_alert("query-visible alert suppressed: restart alert disabled")
            self._server_companion_alert_armed = False
            self._server_companion_offline_since = None
            self._server_companion_offline_since_wall = None
            self._server_companion_consecutive_offline_polls = 0
            self._set_server_companion_poll_interval(COMPANION_POLL_ONLINE_SECONDS)
        else:
            self._reset_server_companion_visible_zero_state()
            snapshot["ping"] = -1
            snapshot["online"] = False
            self._server_companion_consecutive_offline_polls = (
                int(getattr(self, "_server_companion_consecutive_offline_polls", 0) or 0) + 1
            )
            offline_since = getattr(self, "_server_companion_offline_since", None)
            if offline_since is None:
                offline_since = now
                self._server_companion_offline_since = offline_since
                self._server_companion_offline_since_wall = time.time()
            self._set_server_companion_poll_interval(COMPANION_POLL_OFFLINE_SECONDS)
            if now - float(offline_since) >= COMPANION_ALERT_REARM_OFFLINE_SECONDS:
                self._server_companion_alert_armed = True
            self._debug_server_companion_alert(
                f"poll result: offline consecutive={int(getattr(self, '_server_companion_consecutive_offline_polls', 0) or 0)} "
                f"offline_seconds={now - float(offline_since):.1f} "
                f"armed={bool(getattr(self, '_server_companion_alert_armed', False))}"
            )

        new_online = bool(snapshot.get("online", False))
        self._server_companion_last_online = new_online
        self._server_companion_snapshot = snapshot
        key = self._server_companion_restart_learning_key()
        self._record_server_companion_monitor_heartbeat(key)
        self._append_server_companion_observation_sample(key, new_online)
        self._maybe_record_server_companion_expected_window_miss()
        self._maybe_play_server_companion_restart_warning(snapshot)
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            visible_snapshot = snapshot
            if (
                not new_online
                and int(getattr(self, "_server_companion_consecutive_offline_polls", 0) or 0) < 3
            ):
                visible_snapshot = dict(getattr(self, "_server_companion_visible_snapshot", None) or snapshot)
            self._server_companion_visible_snapshot = dict(visible_snapshot)
            panel.set_server_snapshot(visible_snapshot)
            self._refresh_server_companion_restart_learning_summary()
            panel.set_polling_paused(False)
        return False

    def _maybe_play_server_companion_restart_warning(self, snapshot: dict | None):
        if not bool(getattr(self, "_server_companion_restart_alert_enabled", False)):
            self._debug_server_companion_alert("restart-warning skipped: restart alerts disabled")
            return
        if not bool((snapshot or {}).get("online", False)):
            self._debug_server_companion_alert("restart-warning skipped: server offline")
            return
        key = self._server_companion_restart_learning_key()
        if not key:
            self._debug_server_companion_alert("restart-warning skipped: no restart-learning key")
            return
        state = companion_restart_learning.normalize_state(
            getattr(self, "_companion_restart_learning", None)
        )
        server = (state.get("servers") or {}).get(str(key))
        if not isinstance(server, dict):
            self._debug_server_companion_alert("restart-warning skipped: no learned server state")
            return
        confidence = self._safe_float_value(server.get("confidence"))
        cycle_seconds = self._safe_positive_int(server.get("learned_cycle_seconds"))
        last_restart_at = self._safe_positive_int(server.get("last_restart_event_at"))
        if confidence < 0.80 or cycle_seconds <= 0 or last_restart_at <= 0:
            self._debug_server_companion_alert(
                "restart-warning skipped: model not ready "
                f"confidence={confidence:.3f} cycle_seconds={cycle_seconds} last_restart_at={last_restart_at}"
            )
            return
        now = int(time.time())
        next_restart_at = int(last_restart_at)
        while next_restart_at <= now:
            next_restart_at += int(cycle_seconds)
        seconds_until = next_restart_at - now
        self._debug_server_companion_alert(
            f"restart-warning check: server={self._server_companion_alert_server_label(snapshot)} "
            f"seconds_until={seconds_until} window=240-300 next_restart_at={next_restart_at}"
        )
        if seconds_until > 300:
            self._debug_server_companion_alert("restart-warning skipped: too early")
            return
        if seconds_until < 240:
            self._debug_server_companion_alert("restart-warning skipped: too late")
            return
        fired = getattr(self, "_server_companion_restart_warning_fired", None)
        if not isinstance(fired, set):
            fired = set()
            self._server_companion_restart_warning_fired = fired
        warning_key = f"{key}:{next_restart_at}"
        if warning_key in fired:
            self._debug_server_companion_alert(f"restart-warning skipped: already fired key={warning_key}")
            return
        self._debug_server_companion_alert(f"restart-warning playing now: key={warning_key} seconds_until={seconds_until}")
        ok, message = self._play_server_companion_restart_warning_sound()
        if ok:
            fired.add(warning_key)
        else:
            self._debug_server_companion_alert(f"restart-warning playback failed; not marking fired: {message}")
        self._set_server_companion_alert_audio_status(None if ok else message)

    def toggle_server_companion_polling(self):
        if getattr(self, "_server_companion_snapshot", None) is None:
            return
        self._server_companion_poll_paused = not bool(getattr(self, "_server_companion_poll_paused", False))
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None:
            panel.set_polling_paused(self._server_companion_poll_paused)
        if self._server_companion_poll_paused:
            self._record_server_companion_monitor_ended("paused")
            self._stop_server_companion_polling()
        else:
            self._record_server_companion_monitor_started()
            self._start_server_companion_polling()

    def set_server_companion_restart_alert_enabled(self, enabled: bool):
        self._server_companion_restart_alert_enabled = bool(enabled)
        self.settings["server_companion_restart_alert_enabled"] = self._server_companion_restart_alert_enabled
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def set_server_companion_alert_volume(self, volume: int):
        try:
            self._server_companion_alert_volume = max(0, min(100, int(volume)))
        except Exception:
            self._server_companion_alert_volume = 80
        self.settings["server_companion_alert_volume"] = self._server_companion_alert_volume
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def set_server_companion_alert_sound(self, sound: str):
        sound = str(sound or "female")
        if sound not in SERVER_COMPANION_ALERT_SOUNDS["online"]:
            sound = "female"
        self._server_companion_alert_sound = sound
        self.settings["server_companion_alert_sound"] = sound
        try:
            save_settings(self.settings)
        except Exception:
            pass

    def _set_server_companion_alert_audio_status(self, message: str | None):
        panel = getattr(self, "server_companion_panel", None)
        if panel is not None and hasattr(panel, "set_alert_audio_status"):
            panel.set_alert_audio_status(message)

    def test_server_companion_alert_sound(self, volume: int):
        self.set_server_companion_alert_volume(volume)
        ok, message = self._play_server_companion_online_sound()
        self._set_server_companion_alert_audio_status(None if ok else message)

    def _server_companion_alert_back_online(self, snapshot: dict, alert_type: str = "back online"):
        self._debug_server_companion_alert(
            f"alert firing: type={alert_type!r} sound_category='online' "
            f"server={self._server_companion_alert_server_label(snapshot)}"
        )
        ok, message = self._play_server_companion_online_sound()
        self._set_server_companion_alert_audio_status(None if ok else message)
        self._notify_server_companion_back_online(snapshot)

    def _play_server_companion_online_sound(self):
        return self._play_server_companion_alert_sound("online")

    def _play_server_companion_restart_warning_sound(self):
        return self._play_server_companion_alert_sound("restart_warning")

    def _play_server_companion_alert_sound(self, event: str):
        try:
            volume = max(0, min(100, int(getattr(self, "_server_companion_alert_volume", 80))))
        except Exception:
            volume = 80
        if volume <= 0:
            self._debug_server_companion_alert(f"audio skipped: event={event!r} volume={volume}")
            return True, None
        sound = str(getattr(self, "_server_companion_alert_sound", "female") or "female")
        event_sounds = SERVER_COMPANION_ALERT_SOUNDS.get(str(event or "online"), SERVER_COMPANION_ALERT_SOUNDS["online"])
        audio_name = event_sounds.get(sound, event_sounds["female"])
        audio_path = os.path.join(os.path.dirname(__file__), "audio", audio_name)
        self._debug_server_companion_alert(
            f"audio selected: event={str(event or 'online')!r} sound={sound!r} "
            f"filename={audio_name!r} path={audio_path!r} exists={os.path.exists(audio_path)}"
        )
        if not os.path.exists(audio_path):
            audio_path = os.path.join(os.path.dirname(__file__), "audio", event_sounds["female"])
            self._debug_server_companion_alert(
                f"audio fallback selected: filename={event_sounds['female']!r} "
                f"path={audio_path!r} exists={os.path.exists(audio_path)}"
            )
        if not os.path.exists(audio_path):
            self._debug_server_companion_alert("audio unavailable: selected/fallback file missing")
            return False, "Alert audio file missing"
        player = shutil.which("ffplay")
        cmd = None
        if player:
            cmd = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", f"volume={volume / 100:.2f}", audio_path]
        else:
            player = shutil.which("mpg123")
            if player:
                cmd = [player, "-g", str(volume), audio_path]
        if not cmd:
            self._debug_server_companion_alert("audio unavailable: no ffplay or mpg123 found")
            return False, "Alert audio unavailable: install FFmpeg / ffplay"
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._debug_server_companion_alert(f"audio process launched: player={cmd[0]!r} event={str(event or 'online')!r}")
            return True, None
        except Exception as exc:
            self._debug_server_companion_alert(f"audio process launch failed: {exc!r}")
            return False, "Alert audio unavailable: install FFmpeg / ffplay"

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
        if getattr(self, "_server_db_update_inflight", False):
            self._set_updating(True, "Updating The Server Database, Please Wait…")
            return False

        self._server_db_update_inflight = True
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

        def finish_startup(rows, ok, err=None):
            if err is not None:
                print(f"[DB] Startup server database update failed: {err}")
                if rows:
                    print("[DB] Startup server database update failed; using cached database")
                    try:
                        apply_db_rows_once(rows, False)
                    finally:
                        self._server_db_update_inflight = False
                    return False
                if getattr(self, "_startup_db_provisional", False):
                    self._startup_db_applied = True
                    self._set_updating(False)
                    self._apply_titlebar_counts()
                    self._server_db_update_inflight = False
                    return False
                try:
                    apply_db_rows_once([], False)
                finally:
                    self._server_db_update_inflight = False
                return False

            if not ok:
                print("[DB] Startup server database update failed: fetch returned false")
                if rows:
                    print("[DB] Startup server database update failed; using cached database")
                    if getattr(self, "_startup_db_provisional", False):
                        self._startup_db_applied = True
                        self._set_updating(False)
                        self._apply_titlebar_counts()
                        self._server_db_update_inflight = False
                        return False
                    try:
                        apply_db_rows_once(rows, False)
                    finally:
                        self._server_db_update_inflight = False
                    return False
                if getattr(self, "_startup_db_provisional", False):
                    self._startup_db_applied = True
                    self._set_updating(False)
                    self._apply_titlebar_counts()
                    self._server_db_update_inflight = False
                    return False
                try:
                    apply_db_rows_once([], False)
                finally:
                    self._server_db_update_inflight = False
                return False

            try:
                apply_db_rows_once(rows, ok)
            finally:
                self._server_db_update_inflight = False
            return False

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

            try:
                ok = fetch_db_overwrite_local()
                if not ok:
                    rows = read_servers_from_db()
                    GLib.idle_add(finish_startup, rows, False, None)
                    return

                rows = read_servers_from_db()

                self._clear_blocklist_runtime_state()

                GLib.idle_add(finish_startup, rows, ok, None)
            except Exception as e:
                GLib.idle_add(finish_startup, [], False, e)

        self._db_executor.submit(worker)
        return False

    def _manual_update_server_database(self):
        if getattr(self, "_server_db_update_inflight", False):
            self._set_updating(True, "Updating The Server Database, Please Wait…")
            return

        self._server_db_update_inflight = True
        self.sort_key = "ping"
        self.sort_asc = False
        self._sort_changed_by_user = False
        self._snapshot_all_sort_keys()
        try:
            sorter = getattr(self, "sorter", None)
            if sorter is not None:
                self._debug_sort_note_model_event("manual_db_sorter_changed")
                sorter.changed(Gtk.SorterChange.DIFFERENT)
        except Exception:
            pass
        self._rebuild_column_view_store(reorder_reason="manual-database-update")
        self._update_sort_indicators()
        self._queue_scroll_to_top()
        self._set_updating(True, "Updating The Server Database, Please Wait…")

        def finish(rows, ok, err=None):
            if err is not None:
                print(f"[DB] Manual server database update failed: {err}")
                self._set_updating(False)
                self._server_db_update_inflight = False
                return False

            if not ok:
                print("[DB] Manual server database update failed: fetch returned false")
                self._set_updating(False)
                self._server_db_update_inflight = False
                return False

            self._apply_db_rows(rows, ok)
            self._set_updating(False)
            self._server_db_update_inflight = False
            return False

        def worker():
            try:
                ok = fetch_db_overwrite_local()
                if not ok:
                    GLib.idle_add(finish, [], False, None)
                    return

                rows = read_servers_from_db()

                self._clear_blocklist_runtime_state()

                GLib.idle_add(finish, rows, ok, None)
            except Exception as e:
                GLib.idle_add(finish, [], False, e)

        self._db_executor.submit(worker)

    def _check_updates_worker(self):
        if not bool(self.settings.get("auto_check_updates", True)):
            return

        test_tag = os.environ.get("DZLL_TEST_UPDATE_TAG", "").strip()
        if test_tag:
            test_url = os.environ.get("DZLL_TEST_UPDATE_URL", "").strip() or RELEASES_URL
            self._update_info = {"tag": test_tag, "url": test_url}
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

    def _manual_check_for_updates(self, status_callback=None):
        def set_status(text: str):
            if status_callback:
                try:
                    status_callback(text)
                except Exception:
                    pass
            return False

        def finish(state: str, tag: str = "", url: str = ""):
            if state == "available":
                self._update_info = {"tag": tag, "url": url or RELEASES_URL}
                self._update_card_dismissed = False
                set_status("Update available")
                self._close_settings_panel()
                self.update_ui.maybe_show(ignore_suppression=True)
            elif state == "latest":
                self._update_info = None
                set_status("You have the latest version")
            elif state == "no_stable_release":
                self._update_info = None
                set_status("No stable release available yet")
            else:
                set_status("Could not check for updates")
            return False

        def worker():
            test_tag = os.environ.get("DZLL_TEST_UPDATE_TAG", "").strip()
            if test_tag:
                test_url = os.environ.get("DZLL_TEST_UPDATE_URL", "").strip() or RELEASES_URL
                GLib.idle_add(finish, "available", test_tag, test_url)
                return

            try:
                req = urllib.request.Request(GITHUB_LATEST_API, headers={"User-Agent": "DZLL"})
                with urllib.request.urlopen(req, timeout=3.0) as r:
                    raw = r.read().decode("utf-8", "replace")
                data = json.loads(raw)
                tag = str(data.get("tag_name") or "").strip()
                url = str(data.get("html_url") or RELEASES_URL).strip()
            except urllib.error.HTTPError as e:
                if getattr(e, "code", None) == 404:
                    GLib.idle_add(finish, "no_stable_release", "", "")
                else:
                    GLib.idle_add(finish, "failed", "", "")
                return
            except Exception:
                GLib.idle_add(finish, "failed", "", "")
                return

            try:
                self.settings["last_update_check_ts"] = int(time.time())
                self.settings["latest_release_tag"] = tag
                self.settings["latest_release_url"] = url
                save_settings(self.settings)
            except Exception:
                pass

            if tag and str(APP_VERSION).strip() not in ("", "dev") and tag != str(APP_VERSION).strip():
                GLib.idle_add(finish, "available", tag, url)
            else:
                GLib.idle_add(finish, "latest", "", "")

        self._update_executor.submit(worker)

    def _apply_db_rows(self, rows: list, fetched_ok: bool):
        try:
            choices = map_choices_from_db_rows(rows)
            self._set_map_choices(choices)
        except Exception:
            self._set_map_choices(["All Maps"])

        loaded = self._load_rows_into_store(rows)
        if not loaded:
            self._server_companion_rows_loaded = False
            msg = "No Servers Found."
            if not fetched_ok:
                msg = "No Servers Found (DB Fetch Failed And No Usable Local DB)."
            self.empty_label.set_text(msg)
            self.empty_label.set_visible(True)
            self._set_updating(False)
            self._apply_titlebar_counts()
            return False

        self._server_companion_rows_loaded = True
        self._startup_live_generation = int(getattr(self, "_startup_live_generation", 0) or 0) + 1
        self._restore_server_companion_if_enabled()
        self._set_updating(True, "Updating The Server Database, Please Wait…")
        if not fetched_ok:
            self._set_updating(False)
            self._apply_titlebar_counts()
            return False
        keys = sorted(self._obj_by_key.keys(), key=self._bm_live_group)
        first_n = min(int(STARTUP_PING_FIRST_N), len(keys))
        first_keys = keys[:first_n]
        rest_keys = keys[first_n:]
        if DEBUG_STARTUP_LIVE:
            print(
                f"[STARTUP-LIVE] first_n={first_n} rest={len(rest_keys)} "
                f"rest_workers={STARTUP_LIVE_REST_WORKERS}",
                flush=True,
            )
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
        self._browser_live_offline_streaks.clear()

        if not rows:
            self._rebuild_mod_suggestion_index()
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
            obj.name_lc = name.strip().lower()
            obj.ipport_lc = f"{ip}:{gport}".lower()
            obj.search_blob = f"{obj.name_lc}\n{obj.ipport_lc}"
            obj.filter_key = k
            obj.is_likely_test_server = self._is_likely_test_server_name(obj.name_lc)
            obj.mod_search_index = build_server_mod_index(mods_json)
            self._debug_sort_attach_notify_probe(obj)

            self.store.append(obj)
            self._obj_by_key[k] = obj
            self.live.setdefault(k, {"hide_high_ping": False, "offline": (ping_db < 0)})

        self._rebuild_mod_suggestion_index()
        self._snapshot_all_sort_keys()
        self._on_filter_changed(reason="load")
        try:
            sorter = getattr(self, "sorter", None)
            if sorter is not None:
                self._debug_sort_note_model_event("load_sorter_changed")
                sorter.changed(Gtk.SorterChange.DIFFERENT)
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
    def _set_int_property_if_changed(self, obj: ServerObject, prop_name: str, value: int) -> bool:
        try:
            new_value = int(value)
        except Exception:
            return False
        try:
            if int(getattr(obj, prop_name)) == new_value:
                return False
        except Exception:
            pass
        try:
            setattr(obj, prop_name, new_value)
            return True
        except Exception:
            return False

    def _update_row_sort_ping(self, obj: ServerObject) -> bool:
        try:
            ping = int(obj.ping)
        except Exception:
            ping = -1
        return self._set_int_property_if_changed(obj, "sort_ping", (PING_MAX + 999) if ping < 0 else ping)

    def _update_row_sort_players(self, obj: ServerObject) -> bool:
        try:
            players = int(obj.players)
        except Exception:
            players = 0
        return self._set_int_property_if_changed(obj, "sort_players", players)

    def _update_row_sort_played_days(self, obj: ServerObject, now_ts: int | None = None) -> bool:
        if now_ts is None:
            now_ts = int(time.time())
        k = fav_key(obj.ip, obj.gport)
        ts = self.last_played.get(k)
        if not ts:
            played_days = 999999
        else:
            try:
                played_days = max(0, int((int(now_ts) - int(ts)) // 86400))
            except Exception:
                played_days = 999999
        return self._set_int_property_if_changed(obj, "sort_played_days", played_days)

    def _snapshot_row_sort_keys(self, obj: ServerObject, now_ts: int):
        self._update_row_sort_ping(obj)
        self._update_row_sort_players(obj)
        self._update_row_sort_played_days(obj, now_ts)

    def _snapshot_all_sort_keys(self):
        now_ts = int(time.time())
        n = self.store.get_n_items()
        for i in range(n):
            obj = self.store.get_item(i)
            if isinstance(obj, ServerObject):
                self._snapshot_row_sort_keys(obj, now_ts)

    def _rebuild_column_view_store(self, timing_ctx: dict | None = None, reorder_reason: str | None = None):
        if DEBUG_FILTER_TIMING and timing_ctx is None:
            current_ctx = getattr(self, "_filter_timing_current_ctx", None)
            if isinstance(current_ctx, dict):
                timing_ctx = current_ctx
        self._debug_sort_note_model_event("column_view_rebuild")
        display_store = getattr(self, "column_view_store", None)
        if display_store is None:
            return

        reason = str(reorder_reason or "unknown")
        if self.sort_key == "players" and not self._browser_reorder_is_background_reason(reason):
            self._snapshot_all_sort_keys()
        debug_reorder = DEBUG_BROWSER_REORDER
        before_count = 0
        before_keys = []
        before_scroll_key = "-"
        live_filter_state = {}
        if debug_reorder:
            try:
                before_count = int(display_store.get_n_items())
            except Exception:
                before_count = 0
            before_keys = self._browser_reorder_visible_keys(5)
            before_scroll_key = self._browser_reorder_scroll_key()
            live_filter_state = self._browser_reorder_live_filter_state()
            self._debug_browser_reorder(
                "rebuild-start",
                mode="rebuild_sorted",
                reason=reason,
                background=self._browser_reorder_is_background_reason(reason),
                sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
                live_sensitive=self._active_filter_depends_on_live_values(),
                visible_before=before_count,
                first_before=before_keys,
                scroll_key=before_scroll_key,
                **live_filter_state,
            )

        timing_enabled = DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict)
        total_start = time.perf_counter() if (DEBUG_COLUMN_SORT or timing_enabled) else None
        filter_start = total_start
        rows = []
        try:
            n = int(self.store.get_n_items())
        except Exception:
            n = 0
        rejected_rows = 0
        filter_func_elapsed = 0.0

        for i in range(n):
            obj = self.store.get_item(i)
            if not isinstance(obj, ServerObject):
                rejected_rows += 1
                continue
            try:
                filter_func_start = time.perf_counter() if timing_enabled else None
                if not self._combined_filter_func(obj):
                    if filter_func_start is not None:
                        filter_func_elapsed += time.perf_counter() - filter_func_start
                    rejected_rows += 1
                    continue
                if filter_func_start is not None:
                    filter_func_elapsed += time.perf_counter() - filter_func_start
            except Exception:
                if filter_func_start is not None:
                    filter_func_elapsed += time.perf_counter() - filter_func_start
                rejected_rows += 1
                continue
            rows.append(obj)
        filter_elapsed = (time.perf_counter() - filter_start) if filter_start is not None else 0.0

        sort_start = time.perf_counter() if (DEBUG_COLUMN_SORT or DEBUG_SORT or timing_enabled) else None

        def bm_group_for_sort(obj: ServerObject) -> int:
            try:
                rank = int(getattr(obj, "bm_rank", 999999999))
            except Exception:
                return 3
            return 0 if rank <= 100 else (1 if rank <= 1000 else (2 if rank <= 2000 else 3))

        def row_sort_key(obj: ServerObject):
            key_start = time.perf_counter() if DEBUG_SORT else None
            try:
                fav_group = 0
                if bool(self.settings.get("pin_favorite_servers", False)):
                    fav_group = 0 if bool(getattr(obj, "fav", False)) else 1

                trusted_group = 0
                if bool(self.settings.get("prioritise_trusted_servers", False)):
                    trusted_group = bm_group_for_sort(obj)

                if self.sort_key == "ping":
                    try:
                        active_value = int(getattr(obj, "sort_ping", 999999))
                    except Exception:
                        active_value = 999999
                elif self.sort_key == "players":
                    try:
                        active_value = int(getattr(obj, "sort_players", 0))
                    except Exception:
                        active_value = 0
                elif self.sort_key == "played":
                    try:
                        active_value = int(getattr(obj, "sort_played_days", 999999))
                    except Exception:
                        active_value = 999999
                    played_group = 0 if active_value < 999999 and (getattr(obj, "played", "") or "").strip() else 1
                    if not self.sort_asc and played_group == 0:
                        active_value = -active_value
                    try:
                        gport = int(getattr(obj, "gport", 0))
                    except Exception:
                        gport = 0
                    return (
                        fav_group,
                        played_group,
                        active_value if played_group == 0 else 0,
                        trusted_group,
                        str(getattr(obj, "name", "") or "").lower(),
                        str(getattr(obj, "ip", "") or "").lower(),
                        gport,
                    )
                else:
                    active_value = 0
                if not self.sort_asc:
                    active_value = -active_value

                try:
                    gport = int(getattr(obj, "gport", 0))
                except Exception:
                    gport = 0
                return (
                    fav_group,
                    trusted_group,
                    active_value,
                    str(getattr(obj, "name", "") or "").lower(),
                    str(getattr(obj, "ip", "") or "").lower(),
                    gport,
                )
            finally:
                if key_start is not None:
                    self._debug_sort_note_key_build(time.perf_counter() - key_start)

        rows.sort(key=row_sort_key)
        sort_elapsed = (time.perf_counter() - sort_start) if sort_start is not None else 0.0

        replace_start = time.perf_counter() if (DEBUG_COLUMN_SORT or timing_enabled) else None
        old_count_start = time.perf_counter() if timing_enabled else None
        old_count = 0
        old_count_elapsed = 0.0
        store_update = "splice"
        prepare_elapsed = 0.0
        splice_call_elapsed = 0.0
        splice = getattr(display_store, "splice", None)
        if callable(splice):
            try:
                old_count = int(display_store.get_n_items())
            except Exception:
                old_count = 0
            old_count_elapsed = (time.perf_counter() - old_count_start) if old_count_start is not None else 0.0
            prepare_start = time.perf_counter() if timing_enabled else None
            replacement_rows = rows
            prepare_elapsed = (time.perf_counter() - prepare_start) if prepare_start is not None else 0.0
            splice_call_start = time.perf_counter() if timing_enabled else None
            try:
                self._debug_browser_reorder(
                    "splice-call",
                    mode="rebuild_sorted",
                    reason=reason,
                    update="splice",
                    removed=old_count,
                    inserted=len(replacement_rows),
                    visible_before=old_count,
                    first_before=before_keys,
                    scroll_key=before_scroll_key,
                )
                splice(0, old_count, replacement_rows)
            except Exception:
                splice = None
            splice_call_elapsed = (time.perf_counter() - splice_call_start) if splice_call_start is not None else 0.0
        else:
            old_count_elapsed = (time.perf_counter() - old_count_start) if old_count_start is not None else 0.0
        if not callable(splice):
            store_update = "fallback"
            fallback_start = time.perf_counter() if timing_enabled else None
            try:
                old_count = int(display_store.get_n_items())
            except Exception:
                old_count = 0
            try:
                display_store.remove_all()
            except Exception:
                try:
                    for i in range(int(display_store.get_n_items()) - 1, -1, -1):
                        display_store.remove(i)
                except Exception:
                    pass

            for obj in rows:
                try:
                    display_store.append(obj)
                except Exception:
                    pass
            splice_call_elapsed = (time.perf_counter() - fallback_start) if fallback_start is not None else splice_call_elapsed
        post_splice_start = time.perf_counter() if timing_enabled else None
        if timing_enabled:
            try:
                new_store_count = int(display_store.get_n_items())
            except Exception:
                new_store_count = len(rows)
        else:
            new_store_count = len(rows)
        post_splice_elapsed = (time.perf_counter() - post_splice_start) if post_splice_start is not None else 0.0
        replace_elapsed = (time.perf_counter() - replace_start) if replace_start is not None else 0.0

        if timing_enabled:
            timing_ctx["rebuild"] = {
                "total_ms": ((time.perf_counter() - total_start) * 1000.0) if total_start is not None else 0.0,
                "scan_ms": filter_elapsed * 1000.0,
                "filter_func_ms": filter_func_elapsed * 1000.0,
                "sort_ms": sort_elapsed * 1000.0,
                "splice_ms": replace_elapsed * 1000.0,
                "store_old_count_ms": old_count_elapsed * 1000.0,
                "store_prepare_ms": prepare_elapsed * 1000.0,
                "store_splice_call_ms": splice_call_elapsed * 1000.0,
                "store_post_splice_ms": post_splice_elapsed * 1000.0,
                "source_rows": n,
                "matched_rows": len(rows),
                "rejected_rows": rejected_rows,
                "spliced_rows": len(rows),
                "old_count": old_count,
                "new_count": new_store_count,
                "delta_count": new_store_count - old_count,
                "removed_count": old_count,
                "inserted_count": len(rows),
                "replace_all": True,
                "store_update": store_update,
                "sort_skipped": False,
            }

        if debug_reorder:
            try:
                after_count = int(display_store.get_n_items())
            except Exception:
                after_count = len(rows)
            self._debug_browser_reorder(
                "rebuild-finish",
                mode="rebuild_sorted",
                reason=reason,
                background=self._browser_reorder_is_background_reason(reason),
                sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
                live_sensitive=self._active_filter_depends_on_live_values(),
                visible_before=before_count,
                visible_after=after_count,
                removed=old_count,
                inserted=len(rows),
                delta=after_count - before_count,
                update=store_update,
                first_before=before_keys,
                first_after=self._browser_reorder_visible_keys(5),
                scroll_before=before_scroll_key,
                scroll_after=self._browser_reorder_scroll_key(),
                **live_filter_state,
            )

        if DEBUG_COLUMN_SORT:
            total_elapsed = time.perf_counter() - total_start
            print(
                f"[COLUMN-SORT] rebuild column store count={len(rows)} "
                f"filter={filter_elapsed:.3f}s sort={sort_elapsed:.3f}s "
                f"replace={replace_elapsed:.3f}s total={total_elapsed:.3f}s "
                f"sort_key={self.sort_key} asc={self.sort_asc}",
                flush=True,
            )

    def _reconcile_visible_store_membership_preserve_order(self, reason: str = "live"):
        display_store = getattr(self, "column_view_store", None)
        source_store = getattr(self, "store", None)
        if display_store is None or source_store is None:
            return

        before_keys = self._browser_reorder_visible_keys(5)
        before_scroll_key = self._browser_reorder_scroll_key()
        live_filter_state = self._browser_reorder_live_filter_state() if DEBUG_BROWSER_REORDER else {}
        try:
            old_count = int(display_store.get_n_items())
        except Exception:
            old_count = 0

        self._debug_browser_reorder(
            "membership-reconcile-start",
            mode="membership_reconcile_preserve_order",
            reason=reason,
            background=True,
            sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
            sort_skipped=True,
            visible_before=old_count,
            first_before=before_keys,
            scroll_key=before_scroll_key,
            **live_filter_state,
        )

        rows = []
        visible_keys = set()
        removed_count = 0
        for i in range(old_count):
            try:
                obj = display_store.get_item(i)
            except Exception:
                obj = None
            if not isinstance(obj, ServerObject):
                removed_count += 1
                continue
            try:
                if not self._combined_filter_func(obj):
                    removed_count += 1
                    continue
            except Exception:
                removed_count += 1
                continue
            rows.append(obj)
            visible_keys.add(self._browser_reorder_key_for_obj(obj))

        added_count = 0
        source_rows = 0
        rejected_rows = 0
        try:
            source_count = int(source_store.get_n_items())
        except Exception:
            source_count = 0
        for i in range(source_count):
            try:
                obj = source_store.get_item(i)
            except Exception:
                obj = None
            source_rows += 1
            if not isinstance(obj, ServerObject):
                rejected_rows += 1
                continue
            key = self._browser_reorder_key_for_obj(obj)
            if key in visible_keys:
                continue
            try:
                if not self._combined_filter_func(obj):
                    rejected_rows += 1
                    continue
            except Exception:
                rejected_rows += 1
                continue
            rows.append(obj)
            visible_keys.add(key)
            added_count += 1

        splice = getattr(display_store, "splice", None)
        if callable(splice):
            self._debug_browser_reorder(
                "splice-call",
                mode="membership_reconcile_preserve_order",
                reason=reason,
                update="splice",
                removed=old_count,
                inserted=len(rows),
                kept=len(rows) - added_count,
                removed_membership=removed_count,
                added=added_count,
                sort_skipped=True,
                first_before=before_keys,
                scroll_key=before_scroll_key,
            )
            try:
                splice(0, old_count, rows)
            except Exception:
                splice = None
        if not callable(splice):
            try:
                display_store.remove_all()
            except Exception:
                try:
                    for i in range(int(display_store.get_n_items()) - 1, -1, -1):
                        display_store.remove(i)
                except Exception:
                    pass
            for obj in rows:
                try:
                    display_store.append(obj)
                except Exception:
                    pass

        try:
            new_count = int(display_store.get_n_items())
        except Exception:
            new_count = len(rows)
        self._debug_browser_reorder(
            "membership-reconcile-finish",
            mode="membership_reconcile_preserve_order",
            reason=reason,
            background=True,
            sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
            sort_skipped=True,
            source_rows=source_rows,
            rejected=rejected_rows,
            kept=len(rows) - added_count,
            removed=removed_count,
            added=added_count,
            visible_before=old_count,
            visible_after=new_count,
            delta=new_count - old_count,
            first_before=before_keys,
            first_after=self._browser_reorder_visible_keys(5),
            scroll_before=before_scroll_key,
            scroll_after=self._browser_reorder_scroll_key(),
            **live_filter_state,
        )

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
        keys = list(keys or [])
        self._startup_live_generation = int(getattr(self, "_startup_live_generation", 0) or 0) + 1
        generation = self._startup_live_generation
        self._startup_live_rest_queue = deque(keys)
        self._startup_live_rest_inflight = 0
        self._startup_live_rest_buffer = []
        self._startup_live_rest_total = len(keys)
        self._startup_live_rest_completed = 0
        self._startup_live_rest_started_at = time.monotonic()
        self._startup_live_rest_last_log_completed = 0
        tid = int(getattr(self, "_startup_live_rest_flush_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._startup_live_rest_flush_id = 0
        if DEBUG_STARTUP_LIVE:
            print(
                f"[STARTUP-LIVE] rest start queued={len(keys)} workers={STARTUP_LIVE_REST_WORKERS} "
                f"timeout={STARTUP_LIVE_REST_TIMEOUT_SECS:.2f}s "
                f"flush_max={STARTUP_LIVE_FLUSH_MAX} flush_ms={STARTUP_LIVE_FLUSH_MS}",
                flush=True,
            )
        if not keys:
            return False
        self._pump_startup_rest_sweep(generation)
        return False

    def _query_startup_rest_one(self, generation: int, k, ip: str, qport: int):
        try:
            info = query_server_live(ip, qport, timeout=STARTUP_LIVE_REST_TIMEOUT_SECS)
        except Exception as e:
            info = {"ok": False, "err": str(e)}
        return generation, (k, info)

    def _pump_startup_rest_sweep(self, generation: int):
        if generation != int(getattr(self, "_startup_live_generation", 0) or 0):
            return False
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False

        cap = int(STARTUP_LIVE_REST_WORKERS)
        while int(getattr(self, "_startup_live_rest_inflight", 0) or 0) < cap:
            try:
                k = self._startup_live_rest_queue.popleft()
            except Exception:
                break
            obj = self._obj_by_key.get(k)
            if not obj:
                self._startup_live_rest_completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0) + 1
                continue
            try:
                ip = str(obj.ip)
                qport = int(obj.qport)
            except Exception:
                self._startup_live_rest_completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0) + 1
                continue

            self._startup_live_rest_inflight = int(getattr(self, "_startup_live_rest_inflight", 0) or 0) + 1
            try:
                fut = self._startup_live_executor.submit(self._query_startup_rest_one, generation, k, ip, qport)
                fut.add_done_callback(
                    lambda done_fut, gen=generation: GLib.idle_add(
                        self._on_startup_rest_future_done,
                        gen,
                        done_fut,
                    )
                )
            except Exception:
                self._startup_live_rest_inflight = max(0, int(getattr(self, "_startup_live_rest_inflight", 0) or 0) - 1)
                self._startup_live_rest_completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0) + 1

        self._maybe_finish_startup_rest_sweep(generation)
        return False

    def _on_startup_rest_future_done(self, generation: int, fut):
        if generation != int(getattr(self, "_startup_live_generation", 0) or 0):
            return False
        self._startup_live_rest_inflight = max(0, int(getattr(self, "_startup_live_rest_inflight", 0) or 0) - 1)
        self._startup_live_rest_completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0) + 1
        try:
            result_generation, result = fut.result()
        except Exception:
            result_generation, result = generation, None
        if result_generation == generation and result:
            self._startup_live_rest_buffer.append(result)
            if len(self._startup_live_rest_buffer) >= int(STARTUP_LIVE_FLUSH_MAX):
                self._flush_startup_rest_results(generation)
            elif not int(getattr(self, "_startup_live_rest_flush_id", 0) or 0):
                try:
                    self._startup_live_rest_flush_id = GLib.timeout_add(
                        int(STARTUP_LIVE_FLUSH_MS),
                        self._flush_startup_rest_results,
                        generation,
                    )
                except Exception:
                    self._startup_live_rest_flush_id = 0

        if DEBUG_STARTUP_LIVE:
            completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0)
            total = int(getattr(self, "_startup_live_rest_total", 0) or 0)
            last_logged = int(getattr(self, "_startup_live_rest_last_log_completed", 0) or 0)
            if completed == total or completed - last_logged >= 250:
                self._startup_live_rest_last_log_completed = completed
                elapsed = time.monotonic() - float(getattr(self, "_startup_live_rest_started_at", time.monotonic()) or time.monotonic())
                rate = (completed / elapsed) if elapsed > 0 else 0.0
                print(
                    f"[STARTUP-LIVE] rest progress completed={completed}/{total} "
                    f"elapsed={elapsed:.1f}s rate={rate:.1f}/s",
                    flush=True,
                )

        self._pump_startup_rest_sweep(generation)
        return False

    def _flush_startup_rest_results(self, generation: int):
        if generation != int(getattr(self, "_startup_live_generation", 0) or 0):
            return False
        self._startup_live_rest_flush_id = 0
        results = list(getattr(self, "_startup_live_rest_buffer", ()) or ())
        self._startup_live_rest_buffer = []
        if results:
            if DEBUG_STARTUP_LIVE:
                print(f"[STARTUP-LIVE] rest flush size={len(results)}", flush=True)
            self._apply_live_results(results, reason="startup-batch")
        self._maybe_finish_startup_rest_sweep(generation)
        return False

    def _maybe_finish_startup_rest_sweep(self, generation: int):
        if generation != int(getattr(self, "_startup_live_generation", 0) or 0):
            return False
        if int(getattr(self, "_startup_live_rest_inflight", 0) or 0) > 0:
            return False
        if len(getattr(self, "_startup_live_rest_queue", ()) or ()) > 0:
            return False
        if getattr(self, "_startup_live_rest_buffer", None):
            tid = int(getattr(self, "_startup_live_rest_flush_id", 0) or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                self._startup_live_rest_flush_id = 0
            self._flush_startup_rest_results(generation)
            return False
        if DEBUG_STARTUP_LIVE:
            total = int(getattr(self, "_startup_live_rest_total", 0) or 0)
            completed = int(getattr(self, "_startup_live_rest_completed", 0) or 0)
            elapsed = time.monotonic() - float(getattr(self, "_startup_live_rest_started_at", time.monotonic()) or time.monotonic())
            rate = (completed / elapsed) if elapsed > 0 else 0.0
            print(
                f"[STARTUP-LIVE] rest complete completed={completed}/{total} "
                f"elapsed={elapsed:.2f}s rate={rate:.1f}/s",
                flush=True,
            )
        return False

    def _apply_status_refresh_cursor_for_state(self, btn=None):
        btn = btn or getattr(self, "refresh_status_btn", None)
        if btn is None:
            return False
        try:
            if bool(getattr(self, "_status_refresh_running", False)):
                btn.set_cursor(None)
            else:
                btn.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        except Exception:
            pass
        return False

    def _ensure_status_refresh_cursor_controller(self, btn=None):
        btn = btn or getattr(self, "refresh_status_btn", None)
        if btn is None or bool(getattr(self, "_status_refresh_cursor_controller_attached", False)):
            return False

        motion = Gtk.EventControllerMotion.new()

        def _enter_or_motion(_ctrl, *_args):
            self._apply_status_refresh_cursor_for_state(btn)

        def _leave(_ctrl):
            try:
                btn.set_cursor(None)
            except Exception:
                pass

        motion.connect("enter", _enter_or_motion)
        motion.connect("motion", _enter_or_motion)
        motion.connect("leave", _leave)
        try:
            btn.add_controller(motion)
            self._status_refresh_cursor_controller_attached = True
        except Exception:
            pass
        return False

    def _set_status_refresh_button_busy(self, busy: bool, generation: int | None = None):
        if generation is not None and generation != int(getattr(self, "_status_refresh_generation", 0) or 0):
            return False
        btn = getattr(self, "refresh_status_btn", None)
        if btn is None:
            return False
        self._ensure_status_refresh_cursor_controller(btn)

        icon = getattr(self, "status_refresh_icon", None)
        if icon is None:
            icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
            self.status_refresh_icon = icon

        spinner = getattr(self, "status_refresh_spinner", None)
        if spinner is None:
            spinner = Gtk.Spinner()
            try:
                spinner.set_size_request(16, 16)
            except Exception:
                pass
            self.status_refresh_spinner = spinner

        try:
            if busy:
                spinner.start()
                btn.set_child(spinner)
                btn.set_sensitive(True)
                btn.set_tooltip_text("Refreshing server status...")
                self._apply_status_refresh_cursor_for_state(btn)
            else:
                try:
                    spinner.stop()
                except Exception:
                    pass
                btn.set_child(icon)
                btn.set_sensitive(True)
                btn.set_tooltip_text("Refresh all server status")
                self._apply_status_refresh_cursor_for_state(btn)
        except Exception:
            return False
        return False

    def _on_refresh_status_clicked(self, *_args):
        if bool(getattr(self, "_status_refresh_running", False)):
            return
        keys = list(getattr(self, "_obj_by_key", {}) or {})
        if not keys:
            return

        self._status_refresh_generation = int(getattr(self, "_status_refresh_generation", 0) or 0) + 1
        generation = self._status_refresh_generation
        self._status_refresh_running = True
        self._status_refresh_queue = deque(keys)
        self._status_refresh_inflight = 0
        self._status_refresh_buffer = []
        self._status_refresh_total = len(keys)
        self._status_refresh_completed = 0
        self._status_refresh_started_at = time.monotonic()
        self._status_refresh_last_log_completed = 0
        self._status_refresh_scroll_value = None
        try:
            vadj = self.scroller.get_vadjustment()
            if vadj:
                self._status_refresh_scroll_value = float(vadj.get_value())
        except Exception:
            self._status_refresh_scroll_value = None

        tid = int(getattr(self, "_status_refresh_flush_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._status_refresh_flush_id = 0

        self._set_status_refresh_button_busy(True, generation=generation)

        if DEBUG_STARTUP_LIVE:
            print(
                f"[STATUS-REFRESH] begin total={len(keys)} workers={STARTUP_LIVE_REST_WORKERS} "
                f"timeout={STARTUP_LIVE_REST_TIMEOUT_SECS:.2f}s",
                flush=True,
            )
        self._pump_status_refresh(generation)

    def _query_status_refresh_one(self, generation: int, k, ip: str, qport: int):
        try:
            info = query_server_live(ip, qport, timeout=STARTUP_LIVE_REST_TIMEOUT_SECS)
        except Exception as e:
            info = {"ok": False, "err": str(e)}
        return generation, (k, info)

    def _pump_status_refresh(self, generation: int):
        if generation != int(getattr(self, "_status_refresh_generation", 0) or 0):
            return False
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return False

        cap = int(STARTUP_LIVE_REST_WORKERS)
        while int(getattr(self, "_status_refresh_inflight", 0) or 0) < cap:
            try:
                k = self._status_refresh_queue.popleft()
            except Exception:
                break
            obj = self._obj_by_key.get(k)
            if not obj:
                self._status_refresh_completed = int(getattr(self, "_status_refresh_completed", 0) or 0) + 1
                continue
            try:
                ip = str(obj.ip)
                qport = int(obj.qport)
            except Exception:
                self._status_refresh_completed = int(getattr(self, "_status_refresh_completed", 0) or 0) + 1
                continue

            self._status_refresh_inflight = int(getattr(self, "_status_refresh_inflight", 0) or 0) + 1
            try:
                fut = self._startup_live_executor.submit(self._query_status_refresh_one, generation, k, ip, qport)
                fut.add_done_callback(
                    lambda done_fut, gen=generation: GLib.idle_add(
                        self._on_status_refresh_future_done,
                        gen,
                        done_fut,
                    )
                )
            except Exception:
                self._status_refresh_inflight = max(0, int(getattr(self, "_status_refresh_inflight", 0) or 0) - 1)
                self._status_refresh_completed = int(getattr(self, "_status_refresh_completed", 0) or 0) + 1

        self._maybe_finish_status_refresh(generation)
        return False

    def _on_status_refresh_future_done(self, generation: int, fut):
        if generation != int(getattr(self, "_status_refresh_generation", 0) or 0):
            return False
        self._status_refresh_inflight = max(0, int(getattr(self, "_status_refresh_inflight", 0) or 0) - 1)
        self._status_refresh_completed = int(getattr(self, "_status_refresh_completed", 0) or 0) + 1
        try:
            result_generation, result = fut.result()
        except Exception:
            result_generation, result = generation, None
        if result_generation == generation and result:
            self._status_refresh_buffer.append(result)
            if len(self._status_refresh_buffer) >= int(STARTUP_LIVE_FLUSH_MAX):
                self._flush_status_refresh_results(generation)
            elif not int(getattr(self, "_status_refresh_flush_id", 0) or 0):
                try:
                    self._status_refresh_flush_id = GLib.timeout_add(
                        int(STARTUP_LIVE_FLUSH_MS),
                        self._flush_status_refresh_results,
                        generation,
                    )
                except Exception:
                    self._status_refresh_flush_id = 0

        if DEBUG_STARTUP_LIVE:
            completed = int(getattr(self, "_status_refresh_completed", 0) or 0)
            total = int(getattr(self, "_status_refresh_total", 0) or 0)
            last_logged = int(getattr(self, "_status_refresh_last_log_completed", 0) or 0)
            if completed == total or completed - last_logged >= 250:
                self._status_refresh_last_log_completed = completed
                elapsed = time.monotonic() - float(getattr(self, "_status_refresh_started_at", time.monotonic()) or time.monotonic())
                rate = (completed / elapsed) if elapsed > 0 else 0.0
                print(
                    f"[STATUS-REFRESH] progress completed={completed}/{total} "
                    f"elapsed={elapsed:.1f}s rate={rate:.1f}/s",
                    flush=True,
                )

        self._pump_status_refresh(generation)
        return False

    def _flush_status_refresh_results(self, generation: int):
        if generation != int(getattr(self, "_status_refresh_generation", 0) or 0):
            return False
        self._status_refresh_flush_id = 0
        results = list(getattr(self, "_status_refresh_buffer", ()) or ())
        self._status_refresh_buffer = []
        if results:
            self._apply_live_results(results, reason="manual-status-refresh")
        self._maybe_finish_status_refresh(generation)
        return False

    def _maybe_finish_status_refresh(self, generation: int):
        if generation != int(getattr(self, "_status_refresh_generation", 0) or 0):
            return False
        if not bool(getattr(self, "_status_refresh_running", False)):
            return False
        if int(getattr(self, "_status_refresh_inflight", 0) or 0) > 0:
            return False
        if len(getattr(self, "_status_refresh_queue", ()) or ()) > 0:
            return False
        if getattr(self, "_status_refresh_buffer", None):
            tid = int(getattr(self, "_status_refresh_flush_id", 0) or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                self._status_refresh_flush_id = 0
            self._flush_status_refresh_results(generation)
            return False

        self._status_refresh_running = False
        try:
            combined_filter = getattr(self, "combined_filter", None)
            if combined_filter is not None:
                self._debug_sort_note_model_event("status_filter_changed")
                combined_filter.changed(Gtk.FilterChange.DIFFERENT)
        except Exception:
            pass
        self._snapshot_all_sort_keys()
        try:
            sorter = getattr(self, "sorter", None)
            if sorter is not None:
                self._debug_sort_note_model_event("status_sorter_changed")
                sorter.changed(Gtk.SorterChange.DIFFERENT)
        except Exception:
            pass
        self._debug_browser_reorder(
            "status-refresh-finish",
            calls_rebuild=True,
            calls_filter_changed=False,
            only_updates_rows=False,
        )
        self._rebuild_column_view_store(reorder_reason="status-refresh")
        scroll_value = getattr(self, "_status_refresh_scroll_value", None)
        if scroll_value is not None:
            def restore_status_refresh_scroll(value=scroll_value):
                try:
                    vadj = self.scroller.get_vadjustment()
                    if vadj:
                        vadj.set_value(value)
                except Exception:
                    pass
                return False

            GLib.idle_add(restore_status_refresh_scroll)
        self._set_status_refresh_button_busy(False, generation=generation)
        if DEBUG_STARTUP_LIVE:
            total = int(getattr(self, "_status_refresh_total", 0) or 0)
            completed = int(getattr(self, "_status_refresh_completed", 0) or 0)
            elapsed = time.monotonic() - float(getattr(self, "_status_refresh_started_at", time.monotonic()) or time.monotonic())
            rate = (completed / elapsed) if elapsed > 0 else 0.0
            print(
                f"[STATUS-REFRESH] complete completed={completed}/{total} "
                f"elapsed={elapsed:.2f}s rate={rate:.1f}/s",
                flush=True,
            )
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

    def _start_browser_live_refresh(self):
        if int(getattr(self, "_browser_live_timer_id", 0) or 0):
            return
        try:
            self._browser_live_timer_id = GLib.timeout_add_seconds(
                BROWSER_LIVE_INTERVAL_SECS,
                self._browser_live_tick,
            )
        except Exception as e:
            print(f"[BROWSER-LIVE] failed to start: {e!r}", flush=True)
            self._browser_live_timer_id = 0

    def _stop_browser_live_refresh(self):
        tid = int(getattr(self, "_browser_live_timer_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._browser_live_timer_id = 0
        self._browser_live_token += 1
        self._browser_live_target_keys = set()
        self._browser_live_inflight = False

    def _invalidate_browser_live_targets(self):
        self._browser_live_token += 1
        self._browser_live_target_keys = set()

    def _browser_live_should_pause(self) -> bool:
        if bool(getattr(self, "_shutdown_cleanup_done", False)):
            return True
        try:
            now = time.monotonic()
            if now < float(getattr(self, "_browser_live_filter_cooldown_until", 0.0) or 0.0):
                if PERF_LOG_ENABLED and now >= float(getattr(self, "_browser_live_filter_cooldown_logged_until", 0.0) or 0.0):
                    print("[PERF] browser-live paused: filter-cooldown", flush=True)
                    self._browser_live_filter_cooldown_logged_until = now + 1.0
                return True
            if now < float(getattr(self, "_browser_live_scroll_active_until", 0.0) or 0.0):
                if PERF_LOG_ENABLED and now >= float(getattr(self, "_browser_live_scroll_pause_logged_until", 0.0) or 0.0):
                    print("[PERF] browser-live paused: scrolling", flush=True)
                    self._browser_live_scroll_pause_logged_until = now + 1.0
                return True
        except Exception:
            pass
        try:
            if not bool(self.get_visible()):
                return True
        except Exception:
            pass
        try:
            if not bool(self.get_mapped()):
                return True
        except Exception:
            pass
        try:
            surface = self.get_surface()
            state = surface.get_state() if surface is not None else None
            if state is not None and bool(state & Gdk.ToplevelState.MINIMIZED):
                return True
        except Exception:
            pass
        if bool(getattr(self, "_steamcmd_install_in_progress", False)):
            return True
        if bool(getattr(self, "_mod_download_backend_active", "")):
            return True
        if getattr(self, "_steamcmd_auth_request", None) is not None:
            return True
        return False

    def _browser_live_candidate_groups(self):
        model = getattr(self, "column_view_store", None)
        if model is None and ENABLE_LEGACY_GTK_SORT_MODEL:
            model = getattr(self, "sort_model", None)
        if model is None:
            model = getattr(self, "store", None)
        scroller = getattr(self, "scroller", None)
        if model is None or scroller is None:
            return [], []

        try:
            total = int(model.get_n_items())
        except Exception:
            total = 0
        if total <= 0:
            return [], []

        row_h = int(BROWSER_LIVE_FALLBACK_ROW_HEIGHT_PX)
        try:
            row_h = max(1, int(row_h))
        except Exception:
            row_h = 64

        first = 0
        visible_count = 1
        try:
            vadj = scroller.get_vadjustment()
            if vadj:
                value = max(0.0, float(vadj.get_value()))
                page = max(1.0, float(vadj.get_page_size()))
                first = max(0, int(value // row_h))
                visible_count = max(1, int((page + row_h - 1) // row_h) + 1)
        except Exception:
            first = 0
            visible_count = 1

        start = min(max(0, first), max(0, total - 1))
        visible_end = min(total, start + visible_count)
        ahead_end = min(total, visible_end + int(BROWSER_LIVE_AHEAD_ROWS))

        def collect_range(first_idx, end_idx, seen):
            rows = []
            for idx in range(first_idx, end_idx):
                try:
                    obj = model.get_item(idx)
                except Exception:
                    obj = None
                if not isinstance(obj, ServerObject):
                    continue
                k = fav_key(obj.ip, obj.gport)
                if k in seen:
                    continue
                seen.add(k)
                rows.append((k, obj.ip, obj.qport))
            return rows

        seen = set()
        visible_rows = collect_range(start, visible_end, seen)
        ahead_rows = collect_range(visible_end, ahead_end, seen)
        return visible_rows, ahead_rows

    def _browser_live_tick(self):
        if self._browser_live_should_pause():
            return True
        if bool(getattr(self, "_browser_live_inflight", False)):
            return True

        now = time.monotonic()
        visible_rows, ahead_rows = self._browser_live_candidate_groups()
        visible_candidates = list(visible_rows)
        ahead_candidates = []
        for k, ip, qport in ahead_rows:
            last = float(self._browser_live_last_refresh.get(k, 0.0) or 0.0)
            if (now - last) < float(BROWSER_LIVE_INTERVAL_SECS):
                continue
            ahead_candidates.append((k, ip, qport))
            if len(ahead_candidates) >= int(BROWSER_LIVE_AHEAD_PER_TICK):
                break

        candidates = visible_candidates + ahead_candidates
        if not candidates:
            return True

        self._browser_live_token += 1
        token = self._browser_live_token
        target_keys = {k for k, _ip, _qport in candidates}
        self._browser_live_target_keys = set(target_keys)
        self._browser_live_inflight = True
        for k in target_keys:
            self._browser_live_last_refresh[k] = now

        def query_one(item):
            k, ip, qport = item
            try:
                return (k, query_server_live(ip, qport, timeout=BROWSER_LIVE_TIMEOUT_SECS))
            except Exception as e:
                return (k, {"ok": False, "err": str(e)})

        def worker():
            results = []
            not_done = set()
            deadline = max(2.0, float(BROWSER_LIVE_TIMEOUT_SECS) + 1.0)
            try:
                futures = [self._browser_live_executor.submit(query_one, item) for item in candidates]
                done, not_done = wait(futures, timeout=deadline)
                for fut in done:
                    try:
                        result = fut.result()
                        if result:
                            results.append(result)
                    except Exception:
                        pass
                for fut in not_done:
                    try:
                        fut.cancel()
                    except Exception:
                        pass
            finally:
                GLib.idle_add(self._apply_browser_live_results, token, target_keys, results)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._browser_live_inflight = False
        return True

    def _apply_browser_live_results(self, token, target_keys, results):
        start = time.perf_counter() if PERF_LOG_ENABLED else None
        applied_rows = 0
        try:
            try:
                now = time.monotonic()
                if now < float(getattr(self, "_browser_live_scroll_active_until", 0.0) or 0.0):
                    if PERF_LOG_ENABLED and now >= float(getattr(self, "_browser_live_apply_skip_logged_until", 0.0) or 0.0):
                        print("[PERF] browser-live apply skipped: scrolling", flush=True)
                        self._browser_live_apply_skip_logged_until = now + 1.0
                    return False
            except Exception:
                pass
            current_token = int(getattr(self, "_browser_live_token", 0) or 0)
            token_stale = token != current_token
            if token_stale:
                return False
            current_targets = set(getattr(self, "_browser_live_target_keys", set()) or set())
            target_stale = set(target_keys or set()) != current_targets
            if target_stale:
                return False
            filtered = []
            for k, info in (results or []):
                if k not in current_targets:
                    continue
                adjusted = dict(info or {})
                obj = self._obj_by_key.get(k)
                if obj is not None and adjusted.get("ok") and "ping_ms" in adjusted:
                    try:
                        current_ping = int(getattr(obj, "ping", -1))
                        new_ping = int(adjusted.get("ping_ms", -1))
                        if current_ping >= 0 and new_ping >= 0:
                            if abs(new_ping - current_ping) < int(BROWSER_LIVE_PING_DAMPEN_MS):
                                adjusted["ping_ms"] = current_ping
                    except Exception:
                        pass
                filtered.append((k, adjusted))
            if filtered:
                applied_rows = len(filtered)
                self._apply_live_results(filtered, reason="browser-live")
        finally:
            if PERF_LOG_ENABLED and applied_rows > 0 and start is not None:
                duration_ms = (time.perf_counter() - start) * 1000.0
                print(f"[PERF] browser-live apply: rows={applied_rows} duration={duration_ms:.1f}ms", flush=True)
            self._browser_live_inflight = False
        return False

    def _offline_recheck_tick(self):
        self._debug_browser_reorder("offline-recheck-tick", fired=True)
        offline_items = []

        for k, obj in self._obj_by_key.items():
            try:
                if int(obj.ping) < 0:
                    offline_items.append(k)
            except Exception:
                continue

        if not offline_items:
            self._debug_browser_reorder(
                "offline-recheck-tick",
                offline_rows=0,
                submits_recheck=False,
                does_nothing_visible=True,
                triggers_filter_or_rebuild=False,
            )
            return True

        start = int(getattr(self, "_offline_recheck_cursor", 0) or 0)
        if start >= len(offline_items):
            start = 0
        batch_size = 100
        ordered = offline_items[start:] + offline_items[:start]
        batch = ordered[:batch_size]
        self._offline_recheck_cursor = (start + len(batch)) % max(1, len(offline_items))
        self._debug_browser_reorder(
            "offline-recheck-submit",
            offline_rows=len(offline_items),
            batch=len(batch),
            submits_recheck=True,
            triggers_filter_or_rebuild=False,
        )
        self._submit_live_batch(batch, reason="offline-recheck")
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
        background_live_update = reason in ("batch", "browser-live", "offline-recheck")
        trigger_filter = reason not in ("manual-refresh", "batch", "browser-live", "offline-recheck")
        live_filter_active = background_live_update and self._active_filter_depends_on_live_values()
        live_filter_membership_changed = False
        live_filter_membership_changed_rows = 0
        manual_success = False
        startup_live = reason in ("startup-first", "startup-batch", "manual-status-refresh")
        debug_row_notifications = 0
        debug_ping_updates = 0
        debug_player_updates = 0
        self._debug_browser_reorder(
            "live-results-start",
            reason=reason,
            result_count=len(results or []),
            trigger_filter=trigger_filter,
            background_live_update=background_live_update,
            live_filter_active=live_filter_active,
            sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
        )

        for k, info in (results or []):
            obj = self._obj_by_key.get(k)
            if not obj:
                continue
            visible_before_live_update = None
            if live_filter_active:
                try:
                    visible_before_live_update = bool(self._combined_filter_func(obj))
                except Exception:
                    visible_before_live_update = None

            if not info or not info.get("ok"):
                streaks = getattr(self, "_browser_live_offline_streaks", {})
                streak = int(streaks.get(k, 0) or 0) + 1
                streaks[k] = streak
                self._browser_live_offline_streaks = streaks
                try:
                    was_visibly_online = int(getattr(obj, "ping", -1)) >= 0
                except Exception:
                    was_visibly_online = False
                if was_visibly_online and streak < 2:
                    continue

                obj.ping = -1
                self._update_row_sort_ping(obj)
                debug_row_notifications += 1
                debug_ping_updates += 1
                self.live.setdefault(k, {})["offline"] = True
                if live_filter_active:
                    self.live.setdefault(k, {})["hide_high_ping"] = False

                d = self.dead.get(k, {"fail_count": 0, "dead_until": 0, "last_fail": 0})
                d["last_fail"] = now

                if reason == "offline-recheck":
                    d["fail_count"] = int(d.get("fail_count", 0) or 0) + 1
                    if d["fail_count"] >= DEAD_MAX_FAILS:
                        d["fail_count"] = DEAD_MAX_FAILS

                d["dead_until"] = 0
                self.dead[k] = d
                try:
                    save_dead_cache(self.dead)
                except Exception:
                    pass

                changed_any = True
                if visible_before_live_update is not None:
                    try:
                        if visible_before_live_update != bool(self._combined_filter_func(obj)):
                            live_filter_membership_changed = True
                            live_filter_membership_changed_rows += 1
                    except Exception:
                        pass
                continue

            try:
                self._browser_live_offline_streaks.pop(k, None)
            except Exception:
                pass

            ping_ms = int(info.get("ping_ms", 0) or 0)
            players = int(info.get("players", obj.players) or 0)
            maxp = int(info.get("max_players", obj.max_players) or 0)
            t = (info.get("time") or "").strip()
            obj.time = t if is_valid_hhmm(t) else "--:--"
            debug_row_notifications += 1

            obj.players = players
            debug_row_notifications += 1
            debug_player_updates += 1
            if not background_live_update:
                self._update_row_sort_players(obj)
            obj.max_players = maxp
            debug_row_notifications += 1
            queue = info.get("queue")
            obj.queue = -1 if queue is None else int(queue)
            debug_row_notifications += 1
            try:
                obj.password = bool(info.get("password", obj.password))
                debug_row_notifications += 1
            except Exception:
                pass

            obj.ping = ping_ms
            self._update_row_sort_ping(obj)
            debug_row_notifications += 1
            debug_ping_updates += 1
            self.live.setdefault(k, {})["offline"] = False

            if trigger_filter or live_filter_active:
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
            if visible_before_live_update is not None:
                try:
                    if visible_before_live_update != bool(self._combined_filter_func(obj)):
                        live_filter_membership_changed = True
                        live_filter_membership_changed_rows += 1
                except Exception:
                    pass
            manual_success = (reason == "manual-refresh")

        called_filter_changed = False
        called_membership_reconcile = False
        skipped_stable_order = False
        if changed_any and background_live_update:
            if live_filter_active and live_filter_membership_changed:
                called_membership_reconcile = True
                self._reconcile_visible_store_membership_preserve_order(reason=reason)
            else:
                skipped_stable_order = True
            if DEBUG_FILTER_TIMING and skipped_stable_order:
                print(
                    f"[filter-timing] action={reason} live_rebuild=skipped stable_order "
                    f"live_filter_active={str(bool(live_filter_active)).lower()} "
                    f"membership_changed={str(bool(live_filter_membership_changed)).lower()} "
                    f"sort={self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
                    flush=True,
                )
        elif changed_any and (trigger_filter or manual_success) and not startup_live:
            called_filter_changed = True
            self._on_filter_changed(reason="live")

        self._debug_browser_reorder(
            "live-results-finish",
            reason=reason,
            changed_any=changed_any,
            trigger_filter=trigger_filter,
            background_live_update=background_live_update,
            live_filter_active=live_filter_active,
            membership_changed=live_filter_membership_changed,
            membership_changed_rows=live_filter_membership_changed_rows,
            calls_filter_changed=called_filter_changed,
            calls_membership_reconcile=called_membership_reconcile,
            calls_rebuild_directly=False,
            skipped_rebuild_stable_order=skipped_stable_order,
            row_property_updates=debug_row_notifications,
            ping_updates=debug_ping_updates,
            player_updates=debug_player_updates,
            sort=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
        )
        self._debug_sort_note_live_update(reason, row_notifications=debug_row_notifications, ping_updates=debug_ping_updates)
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

        state = getattr(self, "_filter_state", None) or {}
        if not state:
            state = self._build_filter_state()
            self._filter_state = state
        k = getattr(obj, "filter_key", None) or fav_key(obj.ip, obj.gport)
        is_fav = bool(obj.fav)

        live = state.get("live", {}).get(k)
        if (not is_fav) and live and bool(live.get("hide_high_ping", False)):
            try:
                if int(obj.ping) >= 0:
                    return False
            except Exception:
                pass

        q = str(state.get("query") or "")
        ipport = getattr(obj, "ipport_lc", "") or f"{obj.ip}:{obj.gport}".lower()

        if (not is_fav) and bool(state.get("hide_test_servers", True)) and bool(getattr(obj, "is_likely_test_server", False)):
            return False

        max_players_cutoff = int(state.get("max_players_cutoff", 0) or 0)
        if (not is_fav) and max_players_cutoff > 0 and int(obj.max_players) < max_players_cutoff:
            return False

        if bool(state.get("show_fav", False)) and not bool(obj.fav):
            return False

        # 1PP Only: reject servers that allow 3PP
        if bool(state.get("one_pp_only", False)) and bool(obj.third_person):
            return False
        # 3PP Only: reject servers that do NOT allow 3PP
        if bool(state.get("three_pp_only", False)) and (not bool(obj.third_person)):
            return False


        if bool(state.get("no_password", False)) and bool(obj.password):
            return False
        if bool(state.get("online_only", False)):
            try:
                if int(obj.ping) < 0:
                    return False
            except Exception:
                return False
        if bool(state.get("played_only", False)):
            if not (obj.played or "").strip():
                return False

        selected_map = str(state.get("selected_map") or "All Maps")
        if selected_map not in ("All", "All Maps"):
            if (obj.map_name or "") != selected_map:
                return False

        if q:
            if q not in (getattr(obj, "search_blob", "") or ""):
                return False

        mod_query = state.get("mod_query", ())
        if bool(state.get("mod_query_mode", False)):
            if not mod_query:
                return False
            if not server_matches_mod_query(getattr(obj, "mod_search_index", None), mod_query):
                return False

        return True

    def _build_filter_state(self):
        try:
            max_players_cutoff = int(self.settings.get("hide_below_max_players", 0) or 0)
        except Exception:
            max_players_cutoff = 0
        try:
            sel_idx = int(self.map_dropdown.get_selected())
        except Exception:
            sel_idx = 0
        try:
            selected_map = self.map_model.get_string(sel_idx) if 0 <= sel_idx < self.map_model.get_n_items() else "All Maps"
        except Exception:
            selected_map = "All Maps"

        def active(name: str) -> bool:
            try:
                return bool(getattr(self, name).get_active())
            except Exception:
                return False

        raw_query = (self._normal_search_filter_text() or "").strip().lower()
        normal_query, raw_mod_query, mod_query_mode = split_mod_search_operator(raw_query)
        selected_mod_terms = self._selected_mod_chip_query_terms()
        if selected_mod_terms:
            raw_mod_query = ",".join(selected_mod_terms)
            mod_query_mode = True
        mod_query = parse_required_mod_query(raw_mod_query) if mod_query_mode else ()
        active_query = normal_query if len(normal_query) >= 3 else ""
        search_key = f"text:{active_query}|mod:{repr(mod_query)}" if mod_query_mode else active_query
        return {
            "raw_query": raw_query,
            "query": active_query,
            "search_key": search_key,
            "mod_query_mode": mod_query_mode,
            "mod_query": mod_query,
            "now": int(time.time()),
            "live": self.live,
            "show_fav": active("cb_show_fav"),
            "one_pp_only": active("cb_1pp_only"),
            "three_pp_only": active("cb_3pp_only"),
            "no_password": active("cb_no_password"),
            "online_only": active("cb_online_only"),
            "played_only": active("cb_played_only"),
            "selected_map": selected_map,
            "hide_test_servers": bool(self.settings.get("hide_test_servers", True)),
            "max_players_cutoff": max_players_cutoff,
        }

    def _active_filter_depends_on_live_values(self) -> bool:
        state = getattr(self, "_filter_state", None) or {}
        if not state:
            state = self._build_filter_state()
            self._filter_state = state
        if bool(state.get("online_only", False)):
            return True
        if bool(state.get("no_password", False)):
            return True
        try:
            if int(state.get("max_players_cutoff", 0) or 0) > 0:
                return True
        except Exception:
            pass
        try:
            if int(getattr(self, "_ping_cutoff_ms", 0) or 0) > 0:
                return True
        except Exception:
            pass
        return False

    def _normal_search_filter_text(self) -> str:
        entry = getattr(self, "search_entry", None)
        if not entry:
            return ""
        if bool(getattr(self, "_mod_search_mode_active", False)):
            return str(getattr(self, "_mod_search_saved_normal_text", "") or "")
        return entry.get_text() or ""

    def _selected_mod_chip_query_terms(self) -> tuple[str, ...]:
        terms = []
        for chip in getattr(self, "_selected_mod_chips", []) or []:
            if not isinstance(chip, dict):
                continue
            term = str(chip.get("query_term") or "").strip()
            if term:
                terms.append(term)
        return tuple(terms)

    def _refresh_selected_mod_chip_ui(self):
        refresh = getattr(self, "_refresh_mod_search_chips", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

    def _selected_mod_chip_key(self, chip: dict) -> str:
        workshop_id = str(chip.get("workshop_id") or "").strip()
        if workshop_id:
            return f"id:{workshop_id}"
        return f"name:{normalize_mod_text(chip.get('query_term') or chip.get('display_name') or '')}"

    def _resolve_exact_mod_chip(self, text: str) -> dict | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        normalized = normalize_mod_text(raw)
        compact = compact_mod_text(raw)
        index = getattr(self, "_mod_suggestion_index", None)
        mods = index.get("mods", ()) if isinstance(index, dict) else ()
        exact_matches = []
        for entry in mods or ():
            if not isinstance(entry, dict):
                continue
            values = [
                entry.get("display_name") or "",
                entry.get("name") or "",
                *(entry.get("raw_names", ()) or ()),
            ]
            for value in values:
                if normalize_mod_text(value) == normalized or compact_mod_text(value) == compact:
                    exact_matches.append(entry)
                    break
        if not exact_matches:
            suggestions = suggest_mods(index, raw, limit=1)
            if suggestions:
                candidate = suggestions[0]
                values = [
                    candidate.get("display_name") or "",
                    candidate.get("name") or "",
                    *(candidate.get("raw_names", ()) or ()),
                ]
                if any(normalize_mod_text(value) == normalized or compact_mod_text(value) == compact for value in values):
                    exact_matches.append(candidate)
        if not exact_matches:
            return None
        entry = exact_matches[0]
        display_name = str(entry.get("display_name") or entry.get("name") or raw).strip() or raw
        workshop_id = str(entry.get("workshop_id") or "").strip()
        return {
            "display_name": display_name,
            "workshop_id": workshop_id,
            "query_term": workshop_id or display_name,
        }

    def _mod_chip_from_suggestion(self, name: str, suggestion_entry: dict | None = None) -> dict:
        raw = str(name or "").strip()
        entry = suggestion_entry if isinstance(suggestion_entry, dict) else None
        if entry is None:
            resolved = self._resolve_exact_mod_chip(raw)
            if resolved is not None:
                return resolved
            return {}
        display_name = str(entry.get("display_name") or entry.get("name") or raw).strip() or raw
        workshop_id = str(entry.get("workshop_id") or "").strip()
        return {
            "display_name": display_name,
            "workshop_id": workshop_id,
            "query_term": workshop_id or display_name,
        }

    def _add_selected_mod_chip(self, chip: dict) -> bool:
        if not isinstance(chip, dict):
            return False
        display_name = str(chip.get("display_name") or chip.get("query_term") or "").strip()
        query_term = str(chip.get("query_term") or display_name).strip()
        if not display_name:
            return False
        if not query_term:
            return False
        normalized_chip = {
            "display_name": display_name or query_term,
            "workshop_id": str(chip.get("workshop_id") or "").strip(),
            "query_term": query_term,
        }
        new_key = self._selected_mod_chip_key(normalized_chip)
        chips = []
        replaced = False
        for existing in getattr(self, "_selected_mod_chips", []) or []:
            if not isinstance(existing, dict):
                continue
            if self._selected_mod_chip_key(existing) == new_key:
                chips.append(normalized_chip)
                replaced = True
            else:
                chips.append(existing)
        if not replaced:
            chips.append(normalized_chip)
        self._selected_mod_chips = chips
        self._refresh_selected_mod_chip_ui()
        self._cancel_search_filter_debounce()
        self._apply_search_filter_changed(scroll=True, reason="search")
        return True

    def _remove_selected_mod_chip(self, chip_index: int) -> bool:
        try:
            index = int(chip_index)
        except Exception:
            return False
        chips = [chip for chip in (getattr(self, "_selected_mod_chips", []) or []) if isinstance(chip, dict)]
        if index < 0 or index >= len(chips):
            return False
        del chips[index]
        self._selected_mod_chips = chips
        self._refresh_selected_mod_chip_ui()
        self._cancel_search_filter_debounce()
        self._apply_search_filter_changed(scroll=True, reason="search")
        return True

    def _commit_mod_search_input_as_chip(self) -> bool:
        entry = getattr(self, "search_entry", None)
        if not entry or not bool(getattr(self, "_mod_search_mode_active", False)):
            return False
        text = (entry.get_text() or "").strip()
        if not text:
            return False
        chip = self._resolve_exact_mod_chip(text)
        if chip is None:
            try:
                self._mod_suggestion_dismissed = None
                self._refresh_mod_suggestions()
            except Exception:
                pass
            return True
        if not self._add_selected_mod_chip(chip):
            return False
        self._mod_search_entry_update_guard = True
        try:
            entry.set_text("")
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
        finally:
            self._mod_search_entry_update_guard = False
        try:
            self._cancel_mod_suggestion_refresh()
            self._hide_mod_suggestions("mod_search_commit")
        except Exception:
            pass
        self._restore_search_entry_cursor(0)
        GLib.idle_add(self._restore_search_entry_cursor, 0)
        return True

    def _exit_mod_search_mode_restore(self) -> bool:
        if not bool(getattr(self, "_mod_search_mode_active", False)):
            return False

        entry = getattr(self, "search_entry", None)
        saved_text = str(getattr(self, "_mod_search_saved_normal_text", "") or "")
        self._mod_search_mode_active = False
        self._mod_search_entry_update_guard = True
        try:
            if entry:
                entry.set_text(saved_text)
                entry.set_placeholder_text("Filter by name or IP. Click MOD for required mods.")
                icon = "edit-clear-symbolic" if saved_text else None
                entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, icon)
                try:
                    entry.remove_css_class("mod-search-entry-active")
                except Exception:
                    pass
        finally:
            self._mod_search_entry_update_guard = False

        sync_mod_search_ui = getattr(self, "_sync_mod_search_mode_ui", None)
        if callable(sync_mod_search_ui):
            sync_mod_search_ui(False)

        try:
            self._cancel_mod_suggestion_refresh()
            self._hide_mod_suggestions("mod_search_escape")
        except Exception:
            pass
        if entry:
            cursor_pos = len(saved_text)
            self._restore_search_entry_cursor(cursor_pos)
            GLib.idle_add(self._restore_search_entry_cursor, cursor_pos)
        try:
            self._cancel_search_filter_debounce()
            self._apply_search_filter_changed(scroll=True, reason="search")
        except Exception:
            pass
        return True

    def _cancel_search_filter_debounce(self):
        tid = int(getattr(self, "_search_filter_debounce_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._search_filter_debounce_id = 0

    def _rebuild_mod_suggestion_index(self):
        mods_json_values = []
        try:
            for obj in self._obj_by_key.values():
                mods_json_values.append(getattr(obj, "mods_json", "") or "")
        except Exception:
            mods_json_values = []
        self._mod_suggestion_index = build_mod_suggestion_index(mods_json_values)
        self._mod_suggestion_last_rows = ()

    def _log_mod_suggest(self, message: str):
        if not DEBUG_MOD_SUGGESTIONS:
            return
        try:
            print(f"[MODSUGGEST] {message}", flush=True)
        except Exception:
            pass

    def _cancel_mod_suggestion_refresh(self):
        tid = int(getattr(self, "_mod_suggestion_refresh_id", 0) or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._mod_suggestion_refresh_id = 0

    def _queue_mod_suggestions_refresh(self):
        entry = getattr(self, "search_entry", None)
        if not entry:
            return
        self._cancel_mod_suggestion_refresh()
        text = entry.get_text() or ""

        def run_refresh():
            self._mod_suggestion_refresh_id = 0
            current_entry = getattr(self, "search_entry", None)
            if not current_entry:
                return False
            if (current_entry.get_text() or "") != text:
                return False
            self._refresh_mod_suggestions()
            return False

        self._mod_suggestion_refresh_id = GLib.timeout_add(35, run_refresh)

    def _clear_mod_suggestion_rows(self):
        self._mod_suggestion_keyboard_selected = False
        suggestion_list = getattr(self, "mod_suggestion_list", None)
        if not suggestion_list:
            return
        child = suggestion_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            suggestion_list.remove(child)
            child = next_child

    def _hide_mod_suggestions(self, reason: str = "other"):
        panel = getattr(self, "mod_suggestion_panel", None)
        visible_before = False
        if panel:
            try:
                visible_before = bool(panel.get_visible())
            except Exception:
                visible_before = False
            panel.set_visible(False)
        self._clear_mod_suggestion_rows()
        self._mod_suggestion_last_rows = ()
        self._mod_suggestion_keyboard_selected = False
        if visible_before or reason not in ("ineligible_token", "no_operator", "empty_token"):
            self._log_mod_suggest(f"hide reason={reason} visible_before={visible_before}")
        return False

    def _mod_suggestions_visible(self) -> bool:
        try:
            return bool(self.mod_suggestion_panel.get_visible())
        except Exception:
            return False

    def _restore_search_entry_cursor(self, cursor_pos: int):
        entry = getattr(self, "search_entry", None)
        if not entry:
            return False
        try:
            entry.grab_focus()
        except Exception:
            pass
        try:
            entry.set_position(int(cursor_pos))
        except Exception:
            pass
        try:
            entry.select_region(int(cursor_pos), int(cursor_pos))
        except Exception:
            pass
        return False

    def _mod_suggestion_row_count(self) -> int:
        suggestion_list = getattr(self, "mod_suggestion_list", None)
        if not suggestion_list:
            return 0
        count = 0
        child = suggestion_list.get_first_child()
        while child is not None:
            count += 1
            child = child.get_next_sibling()
        return count

    def _scroll_mod_suggestion_row_into_view(self, row):
        if row is None:
            return False
        try:
            row.grab_focus()
        except Exception:
            pass
        scroller = getattr(self, "mod_suggestion_scroller", None)
        if not scroller:
            return False
        try:
            vadjustment = scroller.get_vadjustment()
        except Exception:
            vadjustment = None
        if vadjustment is None:
            return False
        try:
            row_top = float(row.get_allocation().y)
            row_bottom = row_top + float(row.get_allocated_height())
            visible_top = float(vadjustment.get_value())
            visible_bottom = visible_top + float(vadjustment.get_page_size())
            upper = float(vadjustment.get_upper())
            page_size = float(vadjustment.get_page_size())
        except Exception:
            return False
        new_value = visible_top
        if row_top < visible_top:
            new_value = row_top
        elif row_bottom > visible_bottom:
            new_value = row_bottom - page_size
        new_value = max(0.0, min(new_value, max(0.0, upper - page_size)))
        if new_value != visible_top:
            try:
                vadjustment.set_value(new_value)
            except Exception:
                pass
        return False

    def _select_mod_suggestion_delta(self, delta: int):
        suggestion_list = getattr(self, "mod_suggestion_list", None)
        if not suggestion_list:
            return
        count = self._mod_suggestion_row_count()
        if count <= 0:
            return
        selected = suggestion_list.get_selected_row()
        if selected is None:
            index = 0 if delta >= 0 else count - 1
        else:
            index = max(0, min(count - 1, selected.get_index() + delta))
        row = suggestion_list.get_row_at_index(index)
        if row is not None:
            suggestion_list.select_row(row)
            self._mod_suggestion_keyboard_selected = True
            self._scroll_mod_suggestion_row_into_view(row)
            GLib.idle_add(self._scroll_mod_suggestion_row_into_view, row)

    def _activate_selected_mod_suggestion(self) -> bool:
        suggestion_list = getattr(self, "mod_suggestion_list", None)
        if not suggestion_list:
            return False
        row = suggestion_list.get_selected_row()
        if row is None:
            row = suggestion_list.get_row_at_index(0)
        if row is None:
            return False
        name = str(getattr(row, "_mod_suggestion_name", "") or "").strip()
        if not name:
            return False
        self._apply_mod_suggestion(name)
        return True

    def _mod_suggestion_dismiss_key(self, text: str, start: int, compact_token: str) -> tuple[str, int, str]:
        return (str(text or "")[:start], int(start), str(compact_token or ""))

    def _current_mod_suggestion_token(self):
        entry = getattr(self, "search_entry", None)
        if not entry:
            return None
        text = entry.get_text() or ""
        cursor = entry.get_position()
        if bool(getattr(self, "_mod_search_mode_active", False)):
            try:
                cursor = int(cursor)
            except Exception:
                cursor = len(text)
            cursor = max(0, min(cursor, len(text)))
            start = 0
            end = len(text)
            token = text.strip()
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            compact_token = compact_mod_text(token)
            if not compact_token:
                return None
            if len(compact_token) < 2 and compact_token not in SUGGESTION_SHORTHANDS:
                return None
            return token, start, end, compact_token

        normal_query, raw_mod_query, mod_query_mode = split_mod_search_operator(text)
        if not mod_query_mode:
            return None
        mod_start = len(text) - len(raw_mod_query)
        if cursor < mod_start:
            return None
        token_bounds = current_comma_token(raw_mod_query, cursor - mod_start)
        token, start, end = token_bounds
        start += mod_start
        end += mod_start
        compact_token = compact_mod_text(token)
        if not compact_token:
            return None
        if len(compact_token) < 2 and compact_token not in SUGGESTION_SHORTHANDS:
            return None
        return token, start, end, compact_token

    def _refresh_mod_suggestions(self):
        panel = getattr(self, "mod_suggestion_panel", None)
        suggestion_list = getattr(self, "mod_suggestion_list", None)
        entry = getattr(self, "search_entry", None)
        if not panel or not suggestion_list or not entry:
            self._log_mod_suggest(
                f"refresh abort missing_widgets panel={bool(panel)} list={bool(suggestion_list)} entry={bool(entry)}"
            )
            return
        if bool(getattr(self, "_mod_suggestion_selecting", False)):
            return

        token_info = self._current_mod_suggestion_token()
        if token_info is None:
            self._hide_mod_suggestions("ineligible_token")
            return
        token, start, end, compact_token = token_info
        text = entry.get_text() or ""
        dismissed = self._mod_suggestion_dismiss_key(text, start, compact_token)
        if dismissed == getattr(self, "_mod_suggestion_dismissed", None):
            self._hide_mod_suggestions("dismissed_token")
            return

        try:
            if len(getattr(self, "_mod_suggestion_index", {}) or {}) <= 0 and getattr(self, "_obj_by_key", None):
                self._log_mod_suggest("index empty; rebuilding from loaded rows")
                self._rebuild_mod_suggestion_index()
        except Exception:
            pass
        index = getattr(self, "_mod_suggestion_index", None)
        try:
            index_count = len(index) if index is not None else -1
        except Exception:
            index_count = -1
        suggestions = suggest_mods(getattr(self, "_mod_suggestion_index", None), token, limit=MOD_SUGGESTION_RESULT_LIMIT)
        if not suggestions:
            self._log_mod_suggest(f"no suggestions token={token!r} index_count={index_count}")
            self._hide_mod_suggestions("no_suggestions")
            return
        first_names = [str(suggestion.get("display_name") or suggestion.get("name") or "") for suggestion in suggestions[:3]]
        row_entries = tuple(
            (str(suggestion.get("display_name") or suggestion.get("name") or "").strip(), suggestion)
            for suggestion in suggestions
        )
        row_entries = tuple((name, suggestion) for name, suggestion in row_entries if name)
        row_names = tuple(name for name, _suggestion in row_entries)
        visible_before = bool(panel.get_visible())
        if row_names == getattr(self, "_mod_suggestion_last_rows", ()):
            if suggestion_list.get_first_child() is None:
                self._mod_suggestion_last_rows = ()
            else:
                if suggestion_list.get_selected_row() is None:
                    first_row = suggestion_list.get_row_at_index(0)
                    if first_row is not None:
                        suggestion_list.select_row(first_row)
                self._mod_suggestion_keyboard_selected = bool(getattr(self, "_mod_search_mode_active", False))
                panel.set_visible(True)
                self._log_mod_suggest(
                    f"show token={token!r} count={len(row_names)} first={first_names!r} rows=unchanged visible_before={visible_before} visible_after={panel.get_visible()} "
                    f"panel_parent={bool(panel.get_parent())} panel_mapped={panel.get_mapped()} list_rows={self._mod_suggestion_row_count()}"
                )
                return

        self._clear_mod_suggestion_rows()

        for name, suggestion in row_entries:
            label = Gtk.Label(label=name, xalign=0.0)
            label.set_can_focus(False)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_ellipsize(Pango.EllipsizeMode.END)

            row = Gtk.ListBoxRow()
            row.set_can_focus(False)
            row._mod_suggestion_name = name
            row._mod_suggestion_entry = suggestion
            row.set_child(label)
            suggestion_list.append(row)

        if suggestion_list.get_first_child() is None:
            self._hide_mod_suggestions("empty_rows_after_build")
            return

        first_row = suggestion_list.get_row_at_index(0)
        if first_row is not None:
            suggestion_list.select_row(first_row)
        self._mod_suggestion_keyboard_selected = bool(getattr(self, "_mod_search_mode_active", False))
        self._mod_suggestion_last_rows = row_names
        panel.set_visible(True)
        self._log_mod_suggest(
            f"show token={token!r} count={len(row_names)} first={first_names!r} rows=rebuilt visible_before={visible_before} visible_after={panel.get_visible()} "
            f"panel_parent={bool(panel.get_parent())} panel_mapped={panel.get_mapped()} list_rows={self._mod_suggestion_row_count()}"
        )

    def _apply_mod_suggestion(self, name: str):
        entry = getattr(self, "search_entry", None)
        if not entry:
            return

        self._mod_suggestion_selecting = True
        try:
            text = entry.get_text() or ""
            cursor = entry.get_position()
            if bool(getattr(self, "_mod_search_mode_active", False)):
                suggestion_entry = None
                suggestion_list = getattr(self, "mod_suggestion_list", None)
                row = suggestion_list.get_selected_row() if suggestion_list else None
                if row is not None and str(getattr(row, "_mod_suggestion_name", "") or "") == name:
                    suggestion_entry = getattr(row, "_mod_suggestion_entry", None)
                chip = self._mod_chip_from_suggestion(name, suggestion_entry)
                if self._add_selected_mod_chip(chip):
                    self._mod_search_entry_update_guard = True
                    try:
                        entry.set_text("")
                        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                    finally:
                        self._mod_search_entry_update_guard = False
                    self._restore_search_entry_cursor(0)
                    GLib.idle_add(self._restore_search_entry_cursor, 0)
            else:
                _, raw_mod_query, mod_query_mode = split_mod_search_operator(text)
                if not mod_query_mode:
                    return
                mod_start = len(text) - len(raw_mod_query)
                if cursor < mod_start:
                    return
                token, token_start, token_end = current_comma_token(raw_mod_query, cursor - mod_start)
                append_next_separator = not raw_mod_query[token_end:].strip()
                new_mod_query, mod_cursor = replace_comma_token(raw_mod_query, cursor - mod_start, name)
                if append_next_separator:
                    new_mod_query = f"{new_mod_query}, "
                    mod_cursor += 2
                new_text = f"{text[:mod_start]}{new_mod_query}"
                new_cursor = mod_start + mod_cursor
                entry.set_text(new_text)
                self._restore_search_entry_cursor(new_cursor)
                GLib.idle_add(self._restore_search_entry_cursor, new_cursor)
                _, new_raw_mod_query, new_mod_query_mode = split_mod_search_operator(new_text)
                if new_mod_query_mode:
                    new_mod_start = len(new_text) - len(new_raw_mod_query)
                    next_token, start, _end = current_comma_token(new_raw_mod_query, new_cursor - new_mod_start)
                    start += new_mod_start
                    self._mod_suggestion_dismissed = self._mod_suggestion_dismiss_key(
                        new_text,
                        start,
                        compact_mod_text(next_token),
                    )
        finally:
            self._mod_suggestion_selecting = False
        self._hide_mod_suggestions("selection")

    def _on_mod_suggestion_row_activated(self, _listbox, row):
        if not row:
            return
        name = str(getattr(row, "_mod_suggestion_name", "") or "").strip()
        if name and bool(getattr(self, "_mod_search_mode_active", False)):
            chip = self._mod_chip_from_suggestion(name, getattr(row, "_mod_suggestion_entry", None))
            if self._add_selected_mod_chip(chip):
                entry = getattr(self, "search_entry", None)
                if entry:
                    self._mod_search_entry_update_guard = True
                    try:
                        entry.set_text("")
                        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                    finally:
                        self._mod_search_entry_update_guard = False
                    self._restore_search_entry_cursor(0)
                    GLib.idle_add(self._restore_search_entry_cursor, 0)
                self._hide_mod_suggestions("selection")
        elif name:
            self._apply_mod_suggestion(name)

    def _on_mod_suggestion_key_pressed(self, _controller, keyval, _keycode, _state):
        visible = self._mod_suggestions_visible()
        if bool(getattr(self, "_mod_search_mode_active", False)) and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if visible:
                self._activate_selected_mod_suggestion()
                return True
            return self._commit_mod_search_input_as_chip()
        if visible and keyval == Gdk.KEY_Down:
            self._select_mod_suggestion_delta(1)
            return True
        if visible and keyval == Gdk.KEY_Up:
            self._select_mod_suggestion_delta(-1)
            return True
        if visible and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._activate_selected_mod_suggestion()
            return True
        if visible and keyval == Gdk.KEY_Escape:
            token_info = self._current_mod_suggestion_token()
            if token_info is not None:
                _, start, end, compact_token = token_info
                text = self.search_entry.get_text() or ""
                self._mod_suggestion_dismissed = self._mod_suggestion_dismiss_key(text, start, compact_token)
            self._hide_mod_suggestions("escape")
            return True
        return False

    def _on_search_changed_debounced(self):
        self._cancel_search_filter_debounce()

        def apply_search_filter():
            self._search_filter_debounce_id = 0
            self._apply_search_filter_changed(scroll=True, reason="search")
            return False

        self._search_filter_debounce_id = GLib.timeout_add(200, apply_search_filter)

    def _push_filter_refresh_suppression(self):
        self._filter_refresh_suppress_depth = int(getattr(self, "_filter_refresh_suppress_depth", 0) or 0) + 1

    def _pop_filter_refresh_suppression(self):
        depth = int(getattr(self, "_filter_refresh_suppress_depth", 0) or 0)
        self._filter_refresh_suppress_depth = max(0, depth - 1)

    def _filter_refresh_is_suppressed(self) -> bool:
        return int(getattr(self, "_filter_refresh_suppress_depth", 0) or 0) > 0

    def _filter_timing_context(self, action: str, total_start: float | None = None) -> dict | None:
        if not DEBUG_FILTER_TIMING:
            return None
        return {
            "action": str(action or "unknown"),
            "total_start": float(total_start if total_start is not None else time.perf_counter()),
        }

    def _filter_timing_log(self, ctx: dict | None) -> None:
        if not DEBUG_FILTER_TIMING or not isinstance(ctx, dict):
            return
        now = time.perf_counter()
        total_start = float(ctx.get("total_start", now) or now)
        rebuild = ctx.get("rebuild") if isinstance(ctx.get("rebuild"), dict) else {}
        parts = [
            "[filter-timing]",
            f"action={ctx.get('action', 'unknown')}",
            f"total={(now - total_start) * 1000.0:.1f}ms",
        ]
        for key, label in (
            ("callback_entry_ms", "callback_entry"),
            ("on_filter_ms", "on_filter"),
            ("build_state_ms", "build_state"),
            ("apply_ms", "apply"),
            ("sort_set_ms", "set_sort"),
            ("sorter_invalidation_ms", "sorter_invalidation"),
            ("settings_save_ms", "settings_save"),
            ("runtime_effects_ms", "runtime_effects"),
        ):
            if key in ctx:
                parts.append(f"{label}={float(ctx.get(key, 0.0) or 0.0):.1f}ms")
        if "gtk_filter" in ctx:
            value = str(ctx.get("gtk_filter") or "")
            if "gtk_filter_ms" in ctx:
                value = f"{value}:{float(ctx.get('gtk_filter_ms', 0.0) or 0.0):.1f}ms"
            parts.append(f"gtk_filter={value}")
        if rebuild:
            scan_ms = float(rebuild.get("scan_ms", 0.0) or 0.0)
            filter_func_ms = float(rebuild.get("filter_func_ms", 0.0) or 0.0)
            iter_overhead_ms = max(0.0, scan_ms - filter_func_ms)
            parts.extend(
                (
                    f"rebuild={float(rebuild.get('total_ms', 0.0) or 0.0):.1f}ms",
                    f"scan={scan_ms:.1f}ms",
                    f"filter_func={filter_func_ms:.1f}ms",
                    f"iter_overhead={iter_overhead_ms:.1f}ms",
                    f"sort_ms={float(rebuild.get('sort_ms', 0.0) or 0.0):.1f}ms",
                    f"sort_skipped={str(bool(rebuild.get('sort_skipped', False))).lower()}",
                    f"splice={float(rebuild.get('splice_ms', 0.0) or 0.0):.1f}ms",
                    f"old_count_ms={float(rebuild.get('store_old_count_ms', 0.0) or 0.0):.1f}ms",
                    f"prepare={float(rebuild.get('store_prepare_ms', 0.0) or 0.0):.1f}ms",
                    f"splice_call={float(rebuild.get('store_splice_call_ms', 0.0) or 0.0):.1f}ms",
                    f"post_splice={float(rebuild.get('store_post_splice_ms', 0.0) or 0.0):.1f}ms",
                    f"update={rebuild.get('store_update', 'unknown')}",
                    f"replace_all={str(bool(rebuild.get('replace_all', False))).lower()}",
                    f"rows={int(rebuild.get('source_rows', 0) or 0)}",
                    f"matched={int(rebuild.get('matched_rows', 0) or 0)}",
                    f"rejected={int(rebuild.get('rejected_rows', 0) or 0)}",
                    f"spliced={int(rebuild.get('spliced_rows', 0) or 0)}",
                    f"old={int(rebuild.get('old_count', 0) or 0)}",
                    f"new={int(rebuild.get('new_count', 0) or 0)}",
                    f"delta={int(rebuild.get('delta_count', 0) or 0)}",
                    f"removed={int(rebuild.get('removed_count', 0) or 0)}",
                    f"inserted={int(rebuild.get('inserted_count', 0) or 0)}",
                )
            )
        if ctx.get("scroll_queued"):
            parts.append("scroll=queued")
        parts.append(f"sort={self.sort_key}:{'asc' if self.sort_asc else 'desc'}")
        print(" ".join(parts), flush=True)

    def _debug_browser_reorder(self, message: str, **fields) -> None:
        if not DEBUG_BROWSER_REORDER:
            return

        def fmt(value) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (list, tuple)):
                return "[" + ",".join(fmt(v) for v in value) + "]"
            text = str(value)
            if not text:
                return "-"
            return text.replace("\n", "\\n").replace(" ", "_")

        parts = [f"[browser-reorder] {message}"]
        parts.extend(f"{key}={fmt(value)}" for key, value in fields.items())
        print(" ".join(parts), flush=True)

    def _browser_reorder_key_for_obj(self, obj) -> str:
        if not isinstance(obj, ServerObject):
            return "-"
        ip = str(getattr(obj, "ip", "") or "")
        try:
            gport = int(getattr(obj, "gport", 0) or 0)
        except Exception:
            gport = 0
        try:
            qport = int(getattr(obj, "qport", 0) or 0)
        except Exception:
            qport = 0
        return f"{ip}:{gport}:{qport}"

    def _browser_reorder_visible_keys(self, limit: int = 5) -> list[str]:
        if not DEBUG_BROWSER_REORDER:
            return []
        store = getattr(self, "column_view_store", None)
        if store is None:
            return []
        try:
            count = min(max(0, int(limit)), int(store.get_n_items()))
        except Exception:
            count = 0
        keys = []
        for i in range(count):
            try:
                keys.append(self._browser_reorder_key_for_obj(store.get_item(i)))
            except Exception:
                keys.append("-")
        return keys

    def _browser_reorder_scroll_key(self) -> str:
        if not DEBUG_BROWSER_REORDER:
            return "-"
        store = getattr(self, "column_view_store", None)
        if store is None:
            return "-"
        try:
            vadj = self.scroller.get_vadjustment()
            if not vadj:
                return "-"
            row_h = max(1, int(BROWSER_LIVE_FALLBACK_ROW_HEIGHT_PX))
            index = int(max(0.0, float(vadj.get_value())) // row_h)
            count = int(store.get_n_items())
            if index >= count:
                index = max(0, count - 1)
            if index < 0 or count <= 0:
                return "-"
            return self._browser_reorder_key_for_obj(store.get_item(index))
        except Exception:
            return "-"

    def _browser_reorder_live_filter_state(self) -> dict:
        state = getattr(self, "_filter_state", None)
        if not isinstance(state, dict):
            state = {}
        try:
            ping_cutoff = int(getattr(self, "_ping_cutoff_ms", 0) or 0)
        except Exception:
            ping_cutoff = 0
        try:
            max_players_cutoff = int(state.get("max_players_cutoff", 0) or 0)
        except Exception:
            max_players_cutoff = 0
        return {
            "online_only": bool(state.get("online_only", False)),
            "no_password": bool(state.get("no_password", False)),
            "ping_cutoff": ping_cutoff,
            "ping_cutoff_active": ping_cutoff > 0,
            "max_players_cutoff": max_players_cutoff,
            "max_players_active": max_players_cutoff > 0,
        }

    def _browser_reorder_is_background_reason(self, reason: str) -> bool:
        return str(reason or "") in {"batch", "live", "browser-live", "offline-recheck"}

    def _on_filter_changed(
        self,
        *_args,
        scroll: bool = False,
        reason: str = "unknown",
        force: bool = False,
        skip_gtk_filter: bool = False,
        timing_ctx: dict | None = None,
    ):
        ctx = timing_ctx
        on_filter_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
        if DEBUG_FILTER_TIMING and ctx is None:
            current_ctx = getattr(self, "_filter_timing_current_ctx", None)
            if isinstance(current_ctx, dict):
                ctx = current_ctx
        if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
            ctx["action"] = str(ctx.get("action") or reason or "unknown")
            if "callback_start" in ctx:
                try:
                    ctx["callback_entry_ms"] = (on_filter_start - float(ctx.get("callback_start"))) * 1000.0
                except Exception:
                    pass
        if self._filter_refresh_is_suppressed() and not force:
            self._debug_browser_reorder(
                "filter-changed",
                reason=reason,
                skip_gtk_filter=skip_gtk_filter,
                force=force,
                suppressed=True,
                rebuild_visible_store=False,
            )
            if PERF_LOG_ENABLED:
                print(f"[PERF] filter refresh suppressed: reason={reason}", flush=True)
            if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
                ctx["on_filter_ms"] = (time.perf_counter() - on_filter_start) * 1000.0
                ctx["gtk_filter"] = "suppressed"
                if not bool(ctx.get("defer_log")):
                    self._filter_timing_log(ctx)
            return
        build_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
        new_state = self._build_filter_state()
        if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
            ctx["build_state_ms"] = (time.perf_counter() - build_start) * 1000.0
        state_unchanged = (
            not force
            and bool(getattr(self, "_filter_state", None))
            and new_state == getattr(self, "_filter_state", None)
        )
        self._debug_browser_reorder(
            "filter-changed",
            reason=reason,
            skip_gtk_filter=skip_gtk_filter,
            force=force,
            state_changed=not state_unchanged,
            unchanged_rebuild=state_unchanged,
            scroll=scroll,
        )
        if state_unchanged:
            self._filter_state = new_state
            self._filter_query = str(new_state.get("search_key") or new_state.get("query") or "")
            self._rebuild_column_view_store(timing_ctx=ctx, reorder_reason=reason)
            if scroll:
                if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
                    ctx["scroll_queued"] = True
                self._force_scroll_to_top_after_rebuild(reason=reason)
            if PERF_LOG_ENABLED:
                print(f"[PERF] filter refresh skipped: unchanged state reason={reason}", flush=True)
            if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
                ctx["on_filter_ms"] = (time.perf_counter() - on_filter_start) * 1000.0
                ctx["gtk_filter"] = "unchanged-skipped"
                if not bool(ctx.get("defer_log")):
                    self._filter_timing_log(ctx)
            return
        self._apply_filter_changed(
            Gtk.FilterChange.DIFFERENT,
            scroll=scroll,
            state=new_state,
            hint_name="DIFFERENT",
            reason=reason,
            invalidate_gtk_filter=not bool(skip_gtk_filter),
            timing_ctx=ctx,
        )
        if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
            ctx["on_filter_ms"] = (time.perf_counter() - on_filter_start) * 1000.0
            if not bool(ctx.get("defer_log")):
                self._filter_timing_log(ctx)

    def _apply_search_filter_changed(self, scroll: bool = False, reason: str = "search"):
        old_query = str(getattr(self, "_filter_query", "") or "")
        new_state = self._build_filter_state()
        new_query = str(new_state.get("search_key") or new_state.get("query") or "")
        raw_query = str(new_state.get("raw_query") or "")
        if new_query == old_query:
            self._filter_state = new_state
            self._filter_query = new_query
            if PERF_LOG_ENABLED and 0 < len(raw_query) < 3:
                print(f"[PERF] search refresh skipped: short query treated as inactive reason={reason}", flush=True)
            return

        change = Gtk.FilterChange.DIFFERENT
        hint_name = "DIFFERENT"
        if new_query.startswith(old_query):
            change = Gtk.FilterChange.MORE_STRICT
            hint_name = "MORE_STRICT"
        elif old_query.startswith(new_query):
            change = Gtk.FilterChange.LESS_STRICT
            hint_name = "LESS_STRICT"
        if 0 < len(raw_query) < 3:
            hint_name = f"{hint_name} inactive_query"

        self._apply_filter_changed(change, scroll=scroll, state=new_state, hint_name=hint_name, reason=reason)

    def _apply_filter_changed(
        self,
        change,
        *,
        scroll: bool = False,
        state: dict | None = None,
        hint_name: str = "DIFFERENT",
        reason: str = "unknown",
        invalidate_gtk_filter: bool = True,
        timing_ctx: dict | None = None,
    ):
        start = time.perf_counter() if PERF_LOG_ENABLED else None
        apply_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
        self._filter_state = state if isinstance(state, dict) else self._build_filter_state()
        self._filter_query = str(self._filter_state.get("search_key") or self._filter_state.get("query") or "")
        self._invalidate_browser_live_targets()
        if invalidate_gtk_filter:
            gtk_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
            try:
                combined_filter = getattr(self, "combined_filter", None)
                if combined_filter is not None:
                    self._debug_sort_note_model_event(f"filter_changed_{hint_name}")
                    combined_filter.changed(change)
            except Exception:
                pass
            if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
                timing_ctx["gtk_filter"] = "called" if getattr(self, "combined_filter", None) is not None else "no-legacy-model"
                timing_ctx["gtk_filter_ms"] = (time.perf_counter() - gtk_start) * 1000.0
        else:
            # The visible ColumnView is driven by column_view_store, rebuilt below.
            # Sidebar checkbox/map filters skip redundant GTK filter invalidation.
            self._debug_sort_note_model_event(f"filter_changed_{hint_name}_visible_store")
            if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
                timing_ctx["gtk_filter"] = "skipped"
                timing_ctx["gtk_filter_ms"] = 0.0
        self._debug_browser_reorder(
            "apply-filter",
            reason=reason,
            invalidate_gtk_filter=invalidate_gtk_filter,
            rebuild_visible_store=True,
            hint=hint_name,
        )
        self._rebuild_column_view_store(timing_ctx=timing_ctx, reorder_reason=reason)
        rows = 0
        try:
            if getattr(self, "column_view_store", None) is not None:
                rows = int(self.column_view_store.get_n_items())
            elif getattr(self, "filter_model", None) is not None:
                rows = int(self.filter_model.get_n_items())
            elif getattr(self, "store", None) is not None:
                rows = int(self.store.get_n_items())
        except Exception:
            rows = 0
        if reason in {"reset", "map", "perspective", "search", "load"} and rows >= 1000:
            self._browser_live_filter_cooldown_until = time.monotonic() + 2.0
        if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
            timing_ctx["apply_ms"] = (time.perf_counter() - apply_start) * 1000.0
        if PERF_LOG_ENABLED and start is not None:
            duration_ms = (time.perf_counter() - start) * 1000.0
            print(f"[PERF] filter refresh: rows={rows} duration={duration_ms:.1f}ms hint={hint_name} reason={reason}", flush=True)
        if scroll:
            if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
                timing_ctx["scroll_queued"] = True
            self._force_scroll_to_top_after_rebuild(reason=reason)

    def _on_reset_clicked(self, *_args):
        self._cancel_search_filter_debounce()
        try:
            self._push_filter_refresh_suppression()
            try:
                self.search_entry.set_text("")
                self.search_entry.set_placeholder_text("Filter by name or IP. Click MOD for required mods.")
                self.search_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)
                try:
                    self.search_entry.remove_css_class("mod-search-entry-active")
                except Exception:
                    pass
            except Exception:
                pass
            self._mod_search_mode_active = False
            self._mod_search_saved_normal_text = ""
            self._selected_mod_chips = []
            sync_mod_search_ui = getattr(self, "_sync_mod_search_mode_ui", None)
            if callable(sync_mod_search_ui):
                sync_mod_search_ui(False)
            self._refresh_selected_mod_chip_ui()
            try:
                self._cancel_mod_suggestion_refresh()
                self._hide_mod_suggestions("reset")
            except Exception:
                pass
            self._cancel_search_filter_debounce()
            try:
                self.map_dropdown.set_selected(0)
            except Exception:
                pass
            for cb in (self.cb_show_fav, self.cb_1pp_only, self.cb_3pp_only, self.cb_no_password, self.cb_online_only, self.cb_played_only):
                try:
                    cb.set_active(False)
                except Exception:
                    pass
        finally:
            self._pop_filter_refresh_suppression()
        self._cancel_search_filter_debounce()
        self._on_filter_changed(scroll=True, reason="reset", force=True)

    # ----------------------------
    # Sorting
    # ----------------------------
    def _debug_sort_row_count(self) -> int:
        try:
            if getattr(self, "column_view_store", None) is not None:
                return int(self.column_view_store.get_n_items())
        except Exception:
            pass
        try:
            if getattr(self, "filter_model", None) is not None:
                return int(self.filter_model.get_n_items())
        except Exception:
            pass
        try:
            if getattr(self, "store", None) is not None:
                return int(self.store.get_n_items())
        except Exception:
            pass
        return 0

    def _debug_sort_begin(self, key: str, direction: str, rows: int):
        if not DEBUG_SORT:
            return
        self._debug_sort_stats = {
            "active": True,
            "key": str(key or ""),
            "direction": str(direction or ""),
            "rows": int(rows or 0),
            "started_at": time.perf_counter(),
            "calls": 0,
            "total_time": 0.0,
            "max_call": 0.0,
            "cached_numeric": 0,
            "parsed_text": 0,
            "offline": 0,
            "live_ping_updates": 0,
            "row_notifications": 0,
            "notify_props": {},
            "row_binds": 0,
            "bind_kinds": {},
            "model_events": {},
            "scroll_to_skipped": 0,
            "visible_sort": "",
            "key_builds": 0,
            "key_time": 0.0,
        }
        print(f"[SORT] requested column={key} direction={direction} rows={int(rows or 0)}", flush=True)

    def _debug_sort_note_notify(self, prop_name: str):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        name = str(prop_name or "unknown")
        props = stats.setdefault("notify_props", {})
        props[name] = int(props.get(name, 0) or 0) + 1
        stats["row_notifications"] = int(stats.get("row_notifications", 0) or 0) + 1

    def _debug_sort_attach_notify_probe(self, obj: ServerObject):
        if not DEBUG_SORT or not isinstance(obj, ServerObject):
            return
        if bool(getattr(obj, "_dzll_sort_debug_notify_attached", False)):
            return
        try:
            def on_notify(_obj, pspec):
                try:
                    name = pspec.name
                except Exception:
                    name = "unknown"
                self._debug_sort_note_notify(name)

            obj.connect("notify", on_notify)
            obj._dzll_sort_debug_notify_attached = True
        except Exception:
            pass

    def _debug_sort_note_bind(self, kind: str):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        stats["row_binds"] = int(stats.get("row_binds", 0) or 0) + 1
        kinds = stats.setdefault("bind_kinds", {})
        name = str(kind or "unknown")
        kinds[name] = int(kinds.get(name, 0) or 0) + 1

    def _debug_sort_note_model_event(self, event: str):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        events = stats.setdefault("model_events", {})
        name = str(event or "unknown")
        events[name] = int(events.get(name, 0) or 0) + 1

    def _debug_sort_note_scroll_to_skipped(self, pos: int, count: int, reason: str):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if isinstance(stats, dict):
            stats["scroll_to_skipped"] = int(stats.get("scroll_to_skipped", 0) or 0) + 1
        print(f"[SORT] skip_scroll_to pos={int(pos)} count={int(count)} reason={reason}", flush=True)

    def _debug_sort_note_key_build(self, duration: float):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        stats["visible_sort"] = "key_tuple"
        stats["key_builds"] = int(stats.get("key_builds", 0) or 0) + 1
        stats["key_time"] = float(stats.get("key_time", 0.0) or 0.0) + float(duration or 0.0)

    def _debug_sort_note_compare(self, key: str, duration: float, a: ServerObject, b: ServerObject):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        if str(stats.get("key") or "") != str(key or ""):
            return
        stats["calls"] = int(stats.get("calls", 0) or 0) + 1
        stats["total_time"] = float(stats.get("total_time", 0.0) or 0.0) + float(duration or 0.0)
        if duration > float(stats.get("max_call", 0.0) or 0.0):
            stats["max_call"] = float(duration or 0.0)
        if key == "ping":
            stats["cached_numeric"] = int(stats.get("cached_numeric", 0) or 0) + 1
            try:
                if int(getattr(a, "ping", -1)) < 0 or int(getattr(b, "ping", -1)) < 0:
                    stats["offline"] = int(stats.get("offline", 0) or 0) + 1
            except Exception:
                pass
        elif key == "players":
            stats["cached_numeric"] = int(stats.get("cached_numeric", 0) or 0) + 1

    def _debug_sort_note_live_update(self, reason: str, row_notifications: int = 0, ping_updates: int = 0):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        if reason == "browser-live":
            stats["live_ping_updates"] = int(stats.get("live_ping_updates", 0) or 0) + int(ping_updates or 0)

    def _debug_sort_finish(self):
        if not DEBUG_SORT:
            return
        stats = getattr(self, "_debug_sort_stats", None)
        if not isinstance(stats, dict) or not stats.get("active"):
            return
        stats["active"] = False
        key = str(stats.get("key") or "")
        calls = int(stats.get("calls", 0) or 0)
        total_time = float(stats.get("total_time", 0.0) or 0.0)
        max_call = float(stats.get("max_call", 0.0) or 0.0)
        elapsed = time.perf_counter() - float(stats.get("started_at", time.perf_counter()) or time.perf_counter())
        print(
            f"[SORT] {key} calls={calls} total_key_time={total_time:.3f}s "
            f"max_call={max_call:.4f}s cached_numeric={int(stats.get('cached_numeric', 0) or 0)} "
            f"parsed_text={int(stats.get('parsed_text', 0) or 0)} offline={int(stats.get('offline', 0) or 0)}",
            flush=True,
        )
        props = dict(stats.get("notify_props", {}) or {})
        prop_text = " ".join(f"{name}={props[name]}" for name in sorted(props)) or "none=0"
        events = dict(stats.get("model_events", {}) or {})
        event_text = " ".join(f"{name}={events[name]}" for name in sorted(events)) or "none=0"
        bind_kinds = dict(stats.get("bind_kinds", {}) or {})
        bind_text = " ".join(f"{name}={bind_kinds[name]}" for name in sorted(bind_kinds)) or "none=0"
        visible_sort = str(stats.get("visible_sort") or "unknown")
        print(f"[SORT] notify_props {prop_text}", flush=True)
        print(
            f"[SORT] visible_sort={visible_sort} key_builds={int(stats.get('key_builds', 0) or 0)} "
            f"key_time={float(stats.get('key_time', 0.0) or 0.0):.3f}s "
            f"visible_comparator_calls=0 total_comparator_calls={calls}",
            flush=True,
        )
        print(
            f"[SORT] row_binds_during_sort={int(stats.get('row_binds', 0) or 0)} "
            f"bind_kinds {bind_text} scroll_to_skipped={int(stats.get('scroll_to_skipped', 0) or 0)}",
            flush=True,
        )
        print(f"[SORT] model_events {event_text}", flush=True)
        print(
            f"[SORT] apply_elapsed={elapsed:.3f}s "
            f"live_ping_updates_during_sort={int(stats.get('live_ping_updates', 0) or 0)} "
            f"row_notifications={int(stats.get('row_notifications', 0) or 0)}",
            flush=True,
        )

    def _sort_func(self, a: ServerObject, b: ServerObject, _data=None):
        debug_key = str(getattr(self, "sort_key", "") or "")
        start = time.perf_counter() if (PERF_LOG_ENABLED or DEBUG_SORT) else None
        try:
            return self._sort_func_impl(a, b, _data)
        finally:
            if start is not None:
                duration = time.perf_counter() - start
                if PERF_LOG_ENABLED:
                    self._perf_sort_calls = int(getattr(self, "_perf_sort_calls", 0) or 0) + 1
                    self._perf_sort_total = float(getattr(self, "_perf_sort_total", 0.0) or 0.0) + duration
                    if duration > float(getattr(self, "_perf_sort_max", 0.0) or 0.0):
                        self._perf_sort_max = duration
                self._debug_sort_note_compare(debug_key, duration, a, b)

    def _sort_func_impl(self, a: ServerObject, b: ServerObject, _data=None):
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

        def played_sort_value(obj: ServerObject) -> tuple[bool, int]:
            try:
                days = int(getattr(obj, "sort_played_days", 999999))
            except Exception:
                days = 999999
            has_history = days < 999999 and bool((getattr(obj, "played", "") or "").strip())
            return has_history, days

        if self.sort_key == "played":
            has_a, ka = played_sort_value(a)
            has_b, kb = played_sort_value(b)

            c = 0
            if bool(self.settings.get("pin_favorite_servers", False)):
                c = cmp_int(0 if bool(getattr(a, "fav", False)) else 1, 0 if bool(getattr(b, "fav", False)) else 1)
            if c == 0:
                c = cmp_int(0 if has_a else 1, 0 if has_b else 1)
            if c == 0 and has_a and has_b:
                c = cmp_int(ka, kb)
                if not self.sort_asc:
                    c = -c
            if c == 0 and bool(self.settings.get("prioritise_trusted_servers", False)):
                c = cmp_int(bm_group(a), bm_group(b))
            if c == 0:
                c = cmp_str(a.name, b.name)
            if c == 0:
                c = cmp_str(a.ip, b.ip)
            if c == 0:
                c = cmp_int(int(a.gport), int(b.gport))

            if c == 0:
                return Gtk.Ordering.EQUAL
            return Gtk.Ordering.SMALLER if c < 0 else Gtk.Ordering.LARGER

        if bool(self.settings.get("pin_favorite_servers", False)):
            ga = 0 if bool(getattr(a, "fav", False)) else 1
            gb = 0 if bool(getattr(b, "fav", False)) else 1
            c = cmp_int(ga, gb)

            if c != 0:
                return Gtk.Ordering.SMALLER if c < 0 else Gtk.Ordering.LARGER

        if bool(self.settings.get("prioritise_trusted_servers", False)):
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

    def _set_sort(self, key: str, user_initiated: bool = True, timing_ctx: dict | None = None):
        if key not in self.SORT_KEYS:
            return
        sort_set_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
        old_sort_key = getattr(self, "sort_key", "")
        old_sort_asc = bool(getattr(self, "sort_asc", True))
        self._invalidate_browser_live_targets()
        if user_initiated:
            self._sort_changed_by_user = True
        if self.sort_key == key:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_key = key
            self.sort_asc = True

        self._debug_browser_reorder(
            "set-sort",
            old=f"{old_sort_key}:{'asc' if old_sort_asc else 'desc'}",
            new=f"{self.sort_key}:{'asc' if self.sort_asc else 'desc'}",
            user_initiated=user_initiated,
            rebuild_visible_store=True,
        )

        direction = "asc" if self.sort_asc else "desc"
        self._debug_sort_begin(key, direction, self._debug_sort_row_count())
        try:
            if key == "played":
                self._snapshot_all_sort_keys()
            sorter_start = time.perf_counter() if DEBUG_FILTER_TIMING else None
            try:
                sorter = getattr(self, "sorter", None)
                if sorter is not None:
                    self._debug_sort_note_model_event("sorter_changed")
                    sorter.changed(Gtk.SorterChange.DIFFERENT)
            except Exception:
                pass
            if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
                timing_ctx["sorter_invalidation_ms"] = (time.perf_counter() - sorter_start) * 1000.0
            self._rebuild_column_view_store(timing_ctx=timing_ctx, reorder_reason=f"sort:{key}")
        finally:
            self._debug_sort_finish()

        self._update_sort_indicators()
        if DEBUG_FILTER_TIMING and isinstance(timing_ctx, dict):
            timing_ctx["sort_set_ms"] = (time.perf_counter() - sort_set_start) * 1000.0
            timing_ctx["scroll_queued"] = True
        self._force_scroll_to_top_after_rebuild(reason="sort")

    def _on_column_view_sort_header_clicked(self, key_or_title):
        ctx = self._filter_timing_context("sort", time.perf_counter()) if DEBUG_FILTER_TIMING else None
        key = str(key_or_title or "").strip().lower()
        title_to_key = {
            "played": "played",
            "players": "players",
            "ping": "ping",
        }
        key = title_to_key.get(key, key)
        if key in {"players", "ping", "played"}:
            if DEBUG_FILTER_TIMING and isinstance(ctx, dict):
                ctx["action"] = f"sort:{key}"
            self._set_sort(key, timing_ctx=ctx)
            self._filter_timing_log(ctx)

    def _auto_sort_lowest_ping_after_startup(self):
        if self._sort_changed_by_user or self.sort_key != "ping" or self.sort_asc:
            return False

        self._set_sort("ping", user_initiated=False)
        return False

    def _update_sort_indicators(self):
        try:
            refresh_column_view_sort_indicators(self.list_view, self.sort_key, self.sort_asc)
        except Exception:
            pass

    # ----------------------------
    # Favorites
    # ----------------------------
    def _toggle_favorite_for_obj(self, obj: ServerObject):
        scroll_value = None
        try:
            vadj = self.scroller.get_vadjustment()
            if vadj:
                scroll_value = float(vadj.get_value())
        except Exception:
            scroll_value = None

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
        self._on_filter_changed(reason="favourites")
        if scroll_value is not None:
            def restore_favourite_scroll(value=scroll_value):
                try:
                    vadj = self.scroller.get_vadjustment()
                    if vadj:
                        vadj.set_value(value)
                except Exception:
                    pass
                return False

            GLib.idle_add(restore_favourite_scroll)

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

    def _monitor_server_companion_for_obj(self, obj: ServerObject):
        self.set_server_companion_server(obj)
        self.set_server_companion_enabled(True)

    # ----------------------------
    # Prepare to Join Server
    # ----------------------------
    def _steam_launch_prefix(self) -> list[str]:
        # Native-only for now.
        steam_cmd = resolve_native_steam_cmd()
        if not steam_cmd:
            raise RuntimeError("Native Steam executable was not found.")
        print(f"[JOIN] Using Steam command: {steam_cmd}")
        return [steam_cmd, "-applaunch", "221100", "--"]

    def _get_dayz_proton_prefix(self) -> str:
        try:
            compatdata = dayz_compatdata_dir()
            if compatdata is not None:
                return str(compatdata / "pfx")
        except Exception:
            pass
        return os.path.expanduser("~/.local/share/Steam/steamapps/compatdata/221100/pfx")

    def _get_dayz_workshop_root(self) -> str:
        try:
            content_dir = dayz_workshop_content_dir()
            if content_dir is not None:
                return str(content_dir.parent.parent)
        except Exception:
            pass
        return ""

    def _log_join_resolved_dayz_paths(self, *, workshop_dir: str, proton_prefix: str) -> None:
        try:
            summary = dayz_paths_summary()
        except Exception as exc:
            print(f"[JOIN] DayZ Steam library not detected; using configured/default paths ({exc})")
            print(f"[JOIN] resolved workshop path: {workshop_dir!r}")
            print(f"[JOIN] resolved Proton prefix: {proton_prefix!r}")
            return
        dayz_library = str(summary.get("dayz_library") or "")
        if not dayz_library:
            print("[JOIN] DayZ Steam library not detected; using configured/default paths")
        else:
            print(f"[JOIN] resolved DayZ library: {dayz_library}")
        print(f"[JOIN] resolved workshop path: {workshop_dir!r}")
        print(f"[JOIN] resolved Proton prefix: {proton_prefix!r}")

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

    def _start_native_steam_for_join(self) -> tuple[bool, str]:
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
            return False, f"Failed to start native Steam: {exc}"
        return True, ""

    def _show_start_steam_join_consent_blocking(self) -> tuple[bool, bool]:
        try:
            self._start_steam_join_decision = None
            self._start_steam_join_loop = GLib.MainLoop()
            try:
                self.start_steam_join_check.set_active(False)
                self.start_steam_join_scrim.set_visible(True)
                self.start_steam_join_box.set_visible(True)
                self.start_steam_join_start_btn.grab_focus()
            except Exception:
                pass

            self._start_steam_join_loop.run()

            decided = self._start_steam_join_decision
            self._start_steam_join_loop = None
            if not isinstance(decided, tuple):
                return False, False
            return bool(decided[0]), bool(decided[1])
        except Exception:
            return False, False

    def _sync_start_steam_on_join_setting_widget(self) -> None:
        try:
            widget = getattr(self, "_settings_widgets", {}).get("start_steam_on_join")
            if isinstance(widget, Gtk.Switch):
                old_guard = bool(getattr(self, "_settings_update_guard", False))
                try:
                    self._settings_update_guard = True
                    widget.set_active(bool(self.settings.get("start_steam_on_join", False)))
                finally:
                    self._settings_update_guard = old_guard
        except Exception:
            pass

    def _ensure_join_steam_start_consent(self) -> bool:
        self._join_steam_start_allowed = False
        try:
            if is_native_steam_running():
                return True
        except Exception:
            pass

        if bool(self.settings.get("start_steam_on_join", False)):
            self._join_steam_start_allowed = True
            ok, error = self._start_native_steam_for_join()
            if not ok:
                print(f"[JOIN] Could not start Steam: {error}")
                self._set_updating(False, "Steam could not be started.")
                return False
            return True

        start_now, always = self._show_start_steam_join_consent_blocking()
        if not start_now:
            print("[JOIN] Join cancelled.")
            self._set_updating(False, "Join cancelled.")
            return False

        self._join_steam_start_allowed = True
        if always:
            self.settings["start_steam_on_join"] = True
            try:
                save_settings(self.settings)
            except Exception:
                pass
            self._sync_start_steam_on_join_setting_widget()

        ok, error = self._start_native_steam_for_join()
        if not ok:
            print(f"[JOIN] Could not start Steam: {error}")
            self._set_updating(False, "Steam could not be started.")
            return False
        return True

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
                    self._pending_join_mod_ids = []
                    self._pending_join_mod_names_by_id = {}
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
                self._pending_join_mod_ids = []
                self._pending_join_mod_names_by_id = {}
                return

            # GAME STARTED -> now count it as "played"
            try:
                obj = getattr(self, "_pending_last_played_obj", None)
                if obj is not None:
                    ts = int(time.time())

                    def _mark_last_played(played_obj=obj, played_ts=ts):
                        try:
                            k = fav_key(played_obj.ip, played_obj.gport)
                            self.last_played[k] = played_ts
                            played_obj.played = human_last_played(played_ts)
                            self._update_row_sort_played_days(played_obj, played_ts)
                            try:
                                save_last_played(self.last_played)
                            except Exception:
                                pass
                            if getattr(self, "_pending_last_played_obj", None) is played_obj:
                                self._pending_last_played_obj = None
                        except Exception:
                            pass
                        return False

                    GLib.idle_add(_mark_last_played)
            except Exception:
                pass

            # GAME STARTED -> mark this join's required mods as used.
            try:
                mod_ids = list(getattr(self, "_pending_join_mod_ids", []) or [])
                names_by_id = dict(getattr(self, "_pending_join_mod_names_by_id", {}) or {})
                if mod_ids:
                    try:
                        mark_mods_used(mod_ids, names_by_id=names_by_id)
                    except Exception as exc:
                        print(f"[MOD METADATA] Failed to mark joined mods used: {exc}")
                self._pending_join_mod_ids = []
                self._pending_join_mod_names_by_id = {}
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
        workshop_dir = self._get_dayz_workshop_root() or str(self.settings.get("workshop_dir") or "").strip()
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
        self._log_join_resolved_dayz_paths(workshop_dir=workshop_dir, proton_prefix=proton_prefix)

        use_steamcmd = bool(self.settings.get("enable_steamcmd_mod_handling", True))
        mod_download_backend = str(self.settings.get("mod_download_backend") or "steam_client")

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
            "mod_download_backend": mod_download_backend,
            "auto_install_missing": auto_install_missing,
            "auto_update_required": auto_update_required,
        }

    def _join_server_for_obj(self, obj: ServerObject):
        # --- Preflight hard-block warning (MUST run before SteamCMD/mods) ---
        # Run on GTK main thread and block until the user decides.
        if not self._preflight_block_warning_ui_blocking(obj):
            self._join_steam_start_allowed = False
            self._on_filter_changed(reason="join")
            return

        if not self._ensure_join_steam_start_consent():
            self._on_filter_changed(reason="join")
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

        try:
            self._pending_join_mod_ids = [int(mid) for mid, _name in (mods or []) if int(mid) > 0]
            self._pending_join_mod_names_by_id = {
                int(mid): str(name or "").strip()
                for mid, name in (mods or [])
                if int(mid) > 0 and str(name or "").strip()
            }
        except Exception:
            self._pending_join_mod_ids = []
            self._pending_join_mod_names_by_id = {}

        if not mods:
            self._pending_join_mod_ids = []
            self._pending_join_mod_names_by_id = {}
            try:
                proton_prefix = self._get_dayz_proton_prefix()
                watch_folder_linux = self._get_dzll_watch_folder_linux()
                self._log_join_resolved_dayz_paths(workshop_dir=self._get_dayz_workshop_root() or str(self.settings.get("workshop_dir") or "").strip(), proton_prefix=proton_prefix)

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
            self._on_filter_changed(reason="join")
            return

        workshop_dir = self._get_dayz_workshop_root() or str(self.settings.get("workshop_dir") or "").strip()
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
        self._log_join_resolved_dayz_paths(workshop_dir=workshop_dir, proton_prefix=proton_prefix)

        use_steamcmd = bool(self.settings.get("enable_steamcmd_mod_handling", True))
        mod_download_backend = str(self.settings.get("mod_download_backend") or "steam_client")

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
                mod_download_backend,
                auto_install_missing,
                auto_update_required,
            )

        self._show_join_progress_overlay("Preparing required mods...")
        self._hi_executor.submit(do_prepare_and_launch)

    # ----------------------------
    # Preflight Hard Block Warning Queue Jump (kept; not used currently)
    # ----------------------------
    def _preflight_block_warning_ui_blocking(self, obj) -> bool:
        return True

    # ----------------------------
    # Preflight Hard Block Warning
    # ----------------------------
    def _preflight_block_warning(self, obj) -> bool:
        return True

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

    def _clear_blocklist_runtime_state(self) -> None:
        self._bl_ok = False
        self.bl_ip_hard = set()
        self.bl_allow_exact = set()
        self.bl_soft = set()
        self.bl_hard = set()
        self._bl_soft = set()
        self._bl_hard = set()

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
