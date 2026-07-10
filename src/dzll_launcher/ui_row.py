# ui_row.py
import os
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gtk, Pango, Gdk, GObject
from .country_codes import country_name_for
from .config import (
    FAV_STAR_WIDTH,
    ICON_COL_WIDTH,
    RIGHT_BLOCK_WIDTH,
    RIGHT_COL_PX,
    SIDEBAR_WIDTH,
    SIDEBAR_INNER_PADDING,
    NAME_BLOCK_MIN_WIDTH,
    PING_GOOD,
    PING_OK_GREENY,
    PING_MED_YELLOW,
    PING_MED_ORANGEY,
)

PING_CLASSES = ("ping-good", "ping-greeny", "ping-yellow", "ping-orange", "ping-bad", "ping-offline")
PING_MARKUP_COLORS = {
    "ping-good": "#37c871",
    "ping-greeny": "#9ad43a",
    "ping-yellow": "#e3c84a",
    "ping-orange": "#e19a3a",
    "ping-bad": "#e04b4b",
    "ping-offline": "#e04b4b",
}


def _monitor_icon_name() -> str:
    try:
        display = Gdk.Display.get_default()
        if display is not None:
            icon_theme = Gtk.IconTheme.get_for_display(display)
            if icon_theme.has_icon("view-visible-symbolic"):
                return "view-visible-symbolic"
    except Exception:
        pass
    return "view-reveal-symbolic"


PERF_LOG_ENABLED = os.environ.get("DZLL_PERF_LOG") == "1"
COMPLEX_ROWS_ENABLED = os.environ.get("DZLL_COMPLEX_ROWS") == "1"
SIMPLE_ROWS_ENABLED = (not COMPLEX_ROWS_ENABLED) and os.environ.get("DZLL_SIMPLE_ROWS") == "1"
LIGHT_ROWS_ENABLED = (not COMPLEX_ROWS_ENABLED) and (not SIMPLE_ROWS_ENABLED)
LIGHT_ROW_BUTTONS_ENABLED = LIGHT_ROWS_ENABLED
LIGHT_ROW_FAV_ENABLED = LIGHT_ROWS_ENABLED
LIGHT_ROW_LIVE_ENABLED = LIGHT_ROWS_ENABLED
LIGHT_ROW_MODS_ENABLED = LIGHT_ROWS_ENABLED and os.environ.get("DZLL_LIGHT_ROW_MODS") == "1"
LIGHT_ROW_FIXED_META_ENABLED = LIGHT_ROWS_ENABLED and os.environ.get("DZLL_LIGHT_ROW_FIXED_META") == "1"
LIGHT_ROW_FIXED_META_WIDTHS = {
    "time": 76,
    "played": 86,
    "map": 140,
    "players": 118,
    "ping": 76,
    "action": 34,
}
if PERF_LOG_ENABLED and SIMPLE_ROWS_ENABLED:
    print("[PERF] simple rows enabled", flush=True)
elif PERF_LOG_ENABLED and COMPLEX_ROWS_ENABLED:
    print("[PERF] complex server rows enabled", flush=True)
elif PERF_LOG_ENABLED and LIGHT_ROWS_ENABLED:
    print("[PERF] lightweight server rows enabled", flush=True)


def _read_fixed_row_height() -> int | None:
    raw = os.environ.get("DZLL_FIXED_ROW_HEIGHT")
    if raw is None:
        return None
    try:
        height = int(raw)
    except Exception:
        return None
    if height <= 0:
        return None
    return height


FIXED_ROW_HEIGHT = _read_fixed_row_height()
if PERF_LOG_ENABLED and FIXED_ROW_HEIGHT is not None:
    print(f"[PERF] fixed row height enabled: {FIXED_ROW_HEIGHT}", flush=True)

_PERF_ROW_RENDER_COUNT = 0
_PERF_ROW_RENDER_TOTAL = 0.0
_PERF_ROW_RENDER_MAX = 0.0
_PERF_ROW_RENDER_LAST = time.perf_counter()
_PERF_ROW_RENDER_SECTIONS = {}
_PERF_SLOW_ROW_RENDER_LAST = 0.0
_PERF_ROW_BIND_COUNT = 0
_PERF_ROW_BIND_TOTAL = 0.0
_PERF_ROW_BIND_DISCONNECT = 0.0
_PERF_ROW_BIND_CONNECT = 0.0
_PERF_ROW_BIND_RENDER = 0.0
_PERF_ROW_BIND_MAX = 0.0
_PERF_ROW_BIND_LAST = time.perf_counter()
_PERF_ROW_FAST_RENDER_COUNT = 0
_PERF_ROW_FAST_RENDER_LAST = time.perf_counter()


def _record_row_fast_render_perf() -> None:
    global _PERF_ROW_FAST_RENDER_COUNT, _PERF_ROW_FAST_RENDER_LAST
    if not PERF_LOG_ENABLED:
        return
    _PERF_ROW_FAST_RENDER_COUNT += 1
    now = time.perf_counter()
    if (now - _PERF_ROW_FAST_RENDER_LAST) < 1.0:
        return
    count = _PERF_ROW_FAST_RENDER_COUNT
    _PERF_ROW_FAST_RENDER_COUNT = 0
    _PERF_ROW_FAST_RENDER_LAST = now
    if count > 0:
        print(f"[PERF] row fast-render/sec={count}", flush=True)


def _record_row_bind_perf(total: float, disconnect: float, connect: float, render: float) -> None:
    global _PERF_ROW_BIND_COUNT, _PERF_ROW_BIND_TOTAL, _PERF_ROW_BIND_DISCONNECT
    global _PERF_ROW_BIND_CONNECT, _PERF_ROW_BIND_RENDER, _PERF_ROW_BIND_MAX, _PERF_ROW_BIND_LAST
    if not PERF_LOG_ENABLED:
        return
    _PERF_ROW_BIND_COUNT += 1
    _PERF_ROW_BIND_TOTAL += total
    _PERF_ROW_BIND_DISCONNECT += disconnect
    _PERF_ROW_BIND_CONNECT += connect
    _PERF_ROW_BIND_RENDER += render
    if total > _PERF_ROW_BIND_MAX:
        _PERF_ROW_BIND_MAX = total
    now = time.perf_counter()
    if (now - _PERF_ROW_BIND_LAST) < 1.0:
        return
    count = _PERF_ROW_BIND_COUNT
    total_ms = _PERF_ROW_BIND_TOTAL * 1000.0
    disconnect_ms = _PERF_ROW_BIND_DISCONNECT * 1000.0
    connect_ms = _PERF_ROW_BIND_CONNECT * 1000.0
    render_ms = _PERF_ROW_BIND_RENDER * 1000.0
    max_ms = _PERF_ROW_BIND_MAX * 1000.0
    _PERF_ROW_BIND_COUNT = 0
    _PERF_ROW_BIND_TOTAL = 0.0
    _PERF_ROW_BIND_DISCONNECT = 0.0
    _PERF_ROW_BIND_CONNECT = 0.0
    _PERF_ROW_BIND_RENDER = 0.0
    _PERF_ROW_BIND_MAX = 0.0
    _PERF_ROW_BIND_LAST = now
    if count > 0:
        print(
            "[PERF] row bind detail/sec="
            f"{count} bind={total_ms:.1f}ms disconnect={disconnect_ms:.1f}ms "
            f"connect={connect_ms:.1f}ms render={render_ms:.1f}ms max={max_ms:.1f}ms",
            flush=True,
        )


def _record_row_render_perf(duration: float, sections: dict | None, obj=None) -> None:
    global _PERF_ROW_RENDER_COUNT, _PERF_ROW_RENDER_TOTAL, _PERF_ROW_RENDER_MAX
    global _PERF_ROW_RENDER_LAST, _PERF_ROW_RENDER_SECTIONS, _PERF_SLOW_ROW_RENDER_LAST
    if not PERF_LOG_ENABLED:
        return
    section_times = sections or {}
    _PERF_ROW_RENDER_COUNT += 1
    _PERF_ROW_RENDER_TOTAL += duration
    for name, value in section_times.items():
        _PERF_ROW_RENDER_SECTIONS[name] = float(_PERF_ROW_RENDER_SECTIONS.get(name, 0.0) or 0.0) + float(value or 0.0)
    if duration > _PERF_ROW_RENDER_MAX:
        _PERF_ROW_RENDER_MAX = duration
    now = time.perf_counter()
    if duration > 0.020 and now >= (_PERF_SLOW_ROW_RENDER_LAST + 1.0):
        largest = "unknown"
        if section_times:
            largest = max(section_times.items(), key=lambda item: item[1])[0]
        server = ""
        try:
            server = (getattr(obj, "name", "") or "").strip()
        except Exception:
            server = ""
        if not server:
            try:
                server = f"{getattr(obj, 'ip', '')}:{getattr(obj, 'gport', '')}".strip(":")
            except Exception:
                server = ""
        print(f"[PERF] slow row render: total={duration * 1000.0:.1f}ms section={largest} server={server}", flush=True)
        _PERF_SLOW_ROW_RENDER_LAST = now
    if (now - _PERF_ROW_RENDER_LAST) < 1.0:
        return
    count = _PERF_ROW_RENDER_COUNT
    total_ms = _PERF_ROW_RENDER_TOTAL * 1000.0
    max_ms = _PERF_ROW_RENDER_MAX * 1000.0
    detail = dict(_PERF_ROW_RENDER_SECTIONS)
    _PERF_ROW_RENDER_COUNT = 0
    _PERF_ROW_RENDER_TOTAL = 0.0
    _PERF_ROW_RENDER_MAX = 0.0
    _PERF_ROW_RENDER_SECTIONS = {}
    _PERF_ROW_RENDER_LAST = now
    if count > 0:
        print(f"[PERF] row render/sec={count} total={total_ms:.1f}ms max={max_ms:.1f}ms", flush=True)
        order = ("name", "country", "fav", "perspective", "players", "ping", "meta", "tooltips", "css", "buttons", "other")
        parts = []
        for name in order:
            value = float(detail.get(name, 0.0) or 0.0)
            if value > 0.0:
                parts.append(f"{name}={value * 1000.0:.1f}ms")
        for name, value in detail.items():
            if name not in order and float(value or 0.0) > 0.0:
                parts.append(f"{name}={float(value) * 1000.0:.1f}ms")
        if parts:
            print(f"[PERF] row render detail/sec={count} {' '.join(parts)} max={max_ms:.1f}ms", flush=True)


def ping_class(ms: int) -> str:
    try:
        ms = int(ms)
    except Exception:
        return "ping-offline"

    if ms < 0:
        return "ping-offline"
    if ms <= PING_GOOD:
        return "ping-good"
    if ms <= PING_OK_GREENY:
        return "ping-greeny"
    if ms <= PING_MED_YELLOW:
        return "ping-yellow"
    if ms <= PING_MED_ORANGEY:
        return "ping-orange"
    return "ping-bad"


def flag_for(cc: str) -> str:
    cc = (cc or "").upper()
    if cc == "UK":
        cc = "GB"
    if len(cc) != 2 or not cc.isalpha():
        return "🏳️"
    return (
        chr(0x1F1E6 + (ord(cc[0]) - ord("A"))) +
        chr(0x1F1E6 + (ord(cc[1]) - ord("A")))
    )


def fmt_timewarp(x: float) -> str:
    """
    Display as (x12) with NO decimals.
    If value is invalid, show (x?).
    """
    try:
        xf = float(x)
        if xf <= 0:
            return "(x?)"
        return f"(x{int(round(xf))})"
    except Exception:
        return "(x?)"


def row_stable_display(obj) -> tuple[str, str, str, str]:
    key = (
        getattr(obj, "country", ""),
        getattr(obj, "ip", ""),
        int(getattr(obj, "gport", 0) or 0),
        int(getattr(obj, "mod_count", 0) or 0),
        getattr(obj, "mods_preview", ""),
    )
    if getattr(obj, "_row_stable_cache_key", None) == key:
        return (
            getattr(obj, "_row_flag_text", ""),
            getattr(obj, "_row_country_tooltip", ""),
            getattr(obj, "_row_ipport_plain", ""),
            getattr(obj, "_row_meta_text", ""),
        )

    country = (key[0] or "").upper()
    ipport = f"{key[1]}:{key[2]}"
    mod_count = key[3]
    mods_preview = (key[4] or "").strip()
    if mod_count > 0:
        mods_txt = f"Mods: {mod_count} ({mods_preview})" if mods_preview else f"Mods: {mod_count}"
    else:
        mods_txt = "Mods: 0"

    obj._row_stable_cache_key = key
    obj._row_flag_text = flag_for(country)
    obj._row_country_tooltip = country_name_for(country)
    obj._row_ipport_plain = ipport
    obj._row_meta_text = f"{ipport}   {mods_txt}"
    return obj._row_flag_text, obj._row_country_tooltip, obj._row_ipport_plain, obj._row_meta_text


def row_time_display(obj) -> tuple[str, str]:
    try:
        timewarp = float(getattr(obj, "timewarp", 1.0))
    except Exception:
        timewarp = 1.0
    key = (getattr(obj, "time", ""), timewarp)
    if getattr(obj, "_row_time_cache_key", None) == key:
        return getattr(obj, "_row_time_text", ""), getattr(obj, "_row_timewarp_text", "")
    time_text = (key[0] or "").strip() or "--:--"
    timewarp_text = fmt_timewarp(key[1]).strip()
    obj._row_time_cache_key = key
    obj._row_time_text = time_text
    obj._row_timewarp_text = timewarp_text
    return time_text, timewarp_text


def row_players_display(obj) -> tuple[str, str]:
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
    key = (
        players,
        max_players,
        queue,
    )
    if getattr(obj, "_row_players_cache_key", None) == key:
        return getattr(obj, "_row_players_text", ""), getattr(obj, "_row_queue_text", "")
    obj._row_players_cache_key = key
    obj._row_players_text = f"{key[0]}/{key[1]}"
    obj._row_queue_text = f"(Q{key[2]})" if key[2] >= 0 else ""
    return obj._row_players_text, obj._row_queue_text


def row_ping_display(obj) -> tuple[str, str]:
    try:
        ping = int(getattr(obj, "ping", -1))
    except Exception:
        ping = -1
    key = (ping,)
    if getattr(obj, "_row_ping_cache_key", None) == key:
        return getattr(obj, "_row_ping_text", ""), getattr(obj, "_row_ping_class", "ping-offline")
    obj._row_ping_cache_key = key
    obj._row_ping_text = "OFFLINE" if ping < 0 else f"{ping}ms"
    obj._row_ping_class = ping_class(ping)
    return obj._row_ping_text, obj._row_ping_class


def hr() -> Gtk.Widget:
    b = Gtk.Box()
    b.set_size_request(-1, 1)
    b.set_hexpand(True)
    b.add_css_class("hr")
    return b


def attach_pointer_cursor(widget: Gtk.Widget):
    try:
        widget.add_css_class("pointer")
    except Exception:
        pass

    motion = Gtk.EventControllerMotion.new()

    def _enter(_ctrl, _x, _y):
        try:
            widget.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        except Exception:
            pass

    def _leave(_ctrl):
        try:
            widget.set_cursor(None)
        except Exception:
            pass

    motion.connect("enter", _enter)
    motion.connect("leave", _leave)
    widget.add_controller(motion)


class ServerObject(GObject.Object):
    __gtype_name__ = "ServerObject"

    # Core display fields
    fav = GObject.Property(type=bool, default=False)
    password = GObject.Property(type=bool, default=False)
    third_person = GObject.Property(type=bool, default=False)

    name = GObject.Property(type=str, default="")
    country = GObject.Property(type=str, default="")
    ip = GObject.Property(type=str, default="")
    gport = GObject.Property(type=int, default=0)
    qport = GObject.Property(type=int, default=0)

    mod_count = GObject.Property(type=int, default=0)
    mods_preview = GObject.Property(type=str, default="")
    mods_json = GObject.Property(type=str, default="")  # raw DB JSON for workshop IDs

    time = GObject.Property(type=str, default="")
    timewarp = GObject.Property(type=float, default=1.0)

    played = GObject.Property(type=str, default="")
    map_name = GObject.Property(type=str, default="")

    players = GObject.Property(type=int, default=0)
    max_players = GObject.Property(type=int, default=0)
    queue = GObject.Property(type=int, default=-1)

    ping = GObject.Property(type=int, default=-1)  # <0 = offline
    bm_rank = GObject.Property(type=int, default=999999999)

    # Snapshot sort keys (stable until user clicks sort)
    sort_ping = GObject.Property(type=int, default=999999)
    sort_players = GObject.Property(type=int, default=0)
    sort_played_days = GObject.Property(type=int, default=999999)

    def __init__(self, **kwargs):
        super().__init__()
        # Set provided fields via properties (emits notify where appropriate)
        for k, v in kwargs.items():
            try:
                setattr(self, k, v)
            except Exception:
                pass
        self.name_lc = (self.name or "").strip().lower()
        self.ipport_lc = f"{self.ip}:{int(self.gport)}".lower()
        self.search_blob = f"{self.name_lc}\n{self.ipport_lc}"
        self.filter_key = self.ipport_lc
        self.is_likely_test_server = False


class ServerRowWidget(Gtk.Box):
    """
    Binds to a ServerObject (GObject properties).
    Updates in-place via notify:: signals (NO store.splice needed).
    """

    def __init__(self, col_groups, on_toggle_fav, on_monitor, on_join):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._col_groups = col_groups
        self._on_toggle_fav = on_toggle_fav
        self._on_monitor = on_monitor
        self._on_join = on_join
        self._bound_obj = None
        self._notify_ids = []
        self._fav_css_class = None
        self._fav_state = None
        self._perspective_css_class = None
        self._ping_css_class = None
        self._label_text_cache = {}
        self._button_label_cache = {}
        self._tooltip_text_cache = {}
        self._opacity_cache = {}

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(10)
        self.set_margin_end(10)

        MIN_WIN_WIDTH = (
            SIDEBAR_WIDTH
            + 1
            + SIDEBAR_INNER_PADDING * 2
            + NAME_BLOCK_MIN_WIDTH
            + RIGHT_BLOCK_WIDTH
            + 80
        )
        self.set_size_request(MIN_WIN_WIDTH, FIXED_ROW_HEIGHT or -1)

        if SIMPLE_ROWS_ENABLED:
            self.simple_label = Gtk.Label(xalign=0)
            self.simple_label.set_hexpand(True)
            self.simple_label.set_halign(Gtk.Align.FILL)
            self.simple_label.set_single_line_mode(True)
            self.simple_label.set_ellipsize(Pango.EllipsizeMode.END)
            self.append(self.simple_label)
            return

        if LIGHT_ROWS_ENABLED:
            self.set_margin_top(7)
            self.set_margin_bottom(7)
            if LIGHT_ROW_FAV_ENABLED:
                self.light_fav_btn = Gtk.Button()
                self.light_fav_btn.set_can_focus(False)
                self.light_fav_btn.add_css_class("flat")
                self.light_fav_btn.add_css_class("fav-star")
                self.light_fav_btn.set_size_request(28, -1)
                self.light_fav_label = Gtk.Label(label="☆")
                self.light_fav_btn.set_child(self.light_fav_label)
                self.light_fav_btn.connect("clicked", self._light_star_clicked)
                self.append(self.light_fav_btn)

            status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            status_box.set_halign(Gtk.Align.CENTER)
            status_box.set_valign(Gtk.Align.CENTER)
            self.light_lock_label = Gtk.Label(xalign=0.5)
            self.light_lock_label.set_single_line_mode(True)
            self.light_lock_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            self.light_lock_label.set_width_chars(2)
            self.light_lock_label.set_size_request(-1, 16)
            self.light_perspective_label = Gtk.Label(xalign=0.5)
            self.light_perspective_label.set_single_line_mode(True)
            self.light_perspective_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            self.light_perspective_label.set_halign(Gtk.Align.CENTER)
            self.light_perspective_label.set_size_request(-1, 16)
            self.light_perspective_label.add_css_class("perspective-badge")
            self._light_perspective_css_class = None
            status_box.append(self.light_lock_label)
            status_box.append(self.light_perspective_label)

            name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            name_box.set_hexpand(True)
            name_box.set_halign(Gtk.Align.FILL)
            name_box.set_valign(Gtk.Align.CENTER)
            self.light_name_label = self._mk_light_label(expand=True)
            second_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            second_line.set_hexpand(True)
            second_line.set_halign(Gtk.Align.FILL)
            self.light_flag_label = self._mk_light_label(width_chars=3)
            self.light_address_label = self._mk_light_label(expand=True)
            self.light_address_label.add_css_class("dim-label")
            name_box.append(self.light_name_label)
            second_line.append(self.light_flag_label)
            second_line.append(self.light_address_label)
            name_box.append(second_line)
            self.light_time_label = self._mk_light_label(width_chars=12)
            self.light_played_label = self._mk_light_label(width_chars=12)
            self.light_map_label = self._mk_light_label(width_chars=18)
            self.light_players_label = self._mk_light_label(width_chars=10)
            self.light_ping_label = self._mk_light_label(width_chars=7)
            if LIGHT_ROW_FIXED_META_ENABLED:
                self._apply_light_fixed_meta_label(self.light_time_label, "time")
                self._apply_light_fixed_meta_label(self.light_played_label, "played")
                self._apply_light_fixed_meta_label(self.light_map_label, "map")
                self._apply_light_fixed_meta_label(self.light_players_label, "players")
                self._apply_light_fixed_meta_label(self.light_ping_label, "ping")
            self.append(status_box)
            self.append(name_box)
            self.append(self.light_time_label)
            self.append(self.light_played_label)
            self.append(self.light_map_label)
            self.append(self.light_players_label)
            self.append(self.light_ping_label)
            if LIGHT_ROW_BUTTONS_ENABLED:
                self.light_monitor_btn = Gtk.Button()
                self.light_monitor_btn.set_can_focus(False)
                self.light_monitor_btn.add_css_class("flat")
                self.light_monitor_btn.add_css_class("monitor-btn")
                self.light_monitor_btn.set_child(Gtk.Image.new_from_icon_name(_monitor_icon_name()))
                self.light_monitor_btn.set_size_request(34, -1)
                self.light_monitor_btn.connect("clicked", self._monitor_clicked)
                self.light_join_btn = Gtk.Button()
                self.light_join_btn.set_can_focus(False)
                self.light_join_btn.add_css_class("flat")
                self.light_join_btn.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
                self.light_join_btn.set_size_request(34, -1)
                self.light_join_btn.connect("clicked", self._join_clicked)
                if LIGHT_ROW_FIXED_META_ENABLED:
                    self.light_monitor_btn.set_size_request(LIGHT_ROW_FIXED_META_WIDTHS["action"], -1)
                    self.light_join_btn.set_size_request(LIGHT_ROW_FIXED_META_WIDTHS["action"], -1)
                self.append(self.light_monitor_btn)
                self.append(self.light_join_btn)
            return

        # ☆ / ★ Favorites
        self.star_btn = Gtk.Button(label="☆")
        self.star_btn.set_can_focus(False)
        self.star_btn.add_css_class("flat")
        self.star_btn.add_css_class("fav-off")
        self.star_btn.add_css_class("fav-star")
        self.star_btn.set_size_request(FAV_STAR_WIDTH, -1)
        self.star_btn.set_tooltip_text("Favorite")
        attach_pointer_cursor(self.star_btn)
        self.star_btn.connect("clicked", self._star_clicked)
        self.append(self.star_btn)

        # Left block (flex)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.set_hexpand(True)
        left.set_halign(Gtk.Align.FILL)

        # line 1: lock + name
        line1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.lock_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.lock_wrap.set_size_request(ICON_COL_WIDTH, -1)
        self.lock_wrap.set_margin_start(6)
        self.lock_lbl = Gtk.Label(label="🔒")
        self.lock_lbl.set_tooltip_text("Password Protected")
        self.lock_wrap.append(self.lock_lbl)
        line1.append(self.lock_wrap)

        self.name_label = Gtk.Label(xalign=0)
        self.name_label.set_hexpand(True)
        self.name_label.set_halign(Gtk.Align.FILL)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_single_line_mode(True)
        self.name_label.add_css_class("server-name")
        line1.append(self.name_label)
        left.append(line1)

        # line 2: perspective + meta (SINGLE LABEL: IP always visible, mods ellipsize)
        line2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.tp_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.tp_wrap.set_size_request(ICON_COL_WIDTH, -1)
        self.tp_wrap.set_margin_start(6)
        self.tp_wrap.set_valign(Gtk.Align.CENTER)
        self.tp_lbl = Gtk.Label(label="")
        self.tp_lbl.set_valign(Gtk.Align.CENTER)
        self.tp_lbl.add_css_class("perspective-badge")
        self.tp_wrap.append(self.tp_lbl)
        line2.append(self.tp_wrap)

        self.flag_label = Gtk.Label(xalign=0)
        self.flag_label.set_halign(Gtk.Align.START)
        self.flag_label.set_single_line_mode(True)
        self.flag_label.set_margin_end(6)

        self.meta_label = Gtk.Label(xalign=0)
        self.meta_label.set_hexpand(True)
        self.meta_label.set_halign(Gtk.Align.FILL)
        self.meta_label.set_single_line_mode(True)
        self.meta_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.meta_label.add_css_class("dim-label")
        self.meta_label.set_tooltip_text("Right-click to copy IP:PORT")
        try:
            self.meta_label.set_selectable(True)
        except Exception:
            pass

        self._ipport_plain = ""

        gesture = Gtk.GestureClick.new()
        gesture.set_button(3)  # right mouse button
        gesture.connect("pressed", self._on_meta_right_click)
        self.meta_label.add_controller(gesture)

        line2.append(self.flag_label)
        line2.append(self.meta_label)
        left.append(line2)
        self.append(left)

        # Right block
        right_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_wrap.add_css_class("rightblock")
        right_wrap.set_valign(Gtk.Align.FILL)
        right_wrap.set_vexpand(True)
        right_wrap.set_halign(Gtk.Align.END)
        right_wrap.set_size_request(RIGHT_BLOCK_WIDTH, -1)

        right_wrap.set_hexpand(False)

        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        stats_row.set_halign(Gtk.Align.FILL)
        stats_row.set_valign(Gtk.Align.FILL)
        stats_row.set_vexpand(True)
        stats_row.set_hexpand(True)
        stats_row.set_size_request(RIGHT_BLOCK_WIDTH, -1)

        # Time split (timewarp smaller) — cap requests so SizeGroup never grows
        self.time_main = Gtk.Label()
        self.time_main.set_xalign(1.0)
        self.time_main.set_halign(Gtk.Align.CENTER)
        self.time_main.set_single_line_mode(True)
        self.time_main.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            self.time_main.set_width_chars(5)
            self.time_main.set_max_width_chars(5)
        except Exception:
            pass

        self.timewarp_lbl = Gtk.Label()
        self.timewarp_lbl.set_xalign(0.0)
        self.timewarp_lbl.set_halign(Gtk.Align.CENTER)
        self.timewarp_lbl.set_single_line_mode(True)
        self.timewarp_lbl.add_css_class("timewarp")
        self.timewarp_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        try:
            self.timewarp_lbl.set_width_chars(5)
            self.timewarp_lbl.set_max_width_chars(5)
        except Exception:
            pass

        time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        time_box.set_halign(Gtk.Align.CENTER)
        time_box.set_valign(Gtk.Align.CENTER)
        time_box.append(self.time_main)
        time_box.append(self.timewarp_lbl)

        # Fixed column labels (cap width requests to stop SizeGroup drift)
        self.played_label = self._mk_center_label("Last played (local)", max_chars=12)
        self.map_label = self._mk_center_label("Map", max_chars=14)
        self.players_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.players_box.set_hexpand(True)
        self.players_box.set_halign(Gtk.Align.FILL)
        self.players_box.add_css_class("players-cell")
        self.players_label = Gtk.Label(xalign=1.0)
        self.players_label.set_halign(Gtk.Align.END)
        self.players_label.set_hexpand(False)
        self.players_label.set_single_line_mode(True)
        self.players_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.players_label.set_tooltip_text("Players / Max")
        try:
            self.players_label.set_width_chars(7)
            self.players_label.set_max_width_chars(7)
        except Exception:
            pass
        self.queue_label = Gtk.Label(xalign=1.0)
        self.queue_label.set_halign(Gtk.Align.END)
        self.queue_label.set_hexpand(True)
        self.queue_label.set_single_line_mode(True)
        self.queue_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.queue_label.set_tooltip_text("Queue")
        try:
            self.queue_label.set_width_chars(5)
            self.queue_label.set_max_width_chars(5)
        except Exception:
            pass
        self.players_box.append(self.players_label)
        self.players_box.append(self.queue_label)
        self.ping_label = self._mk_center_label("Ping (ms)", max_chars=7)

        self.monitor_btn = Gtk.Button()
        self.monitor_btn.set_can_focus(False)
        self.monitor_btn.add_css_class("flat")
        self.monitor_btn.add_css_class("monitor-btn")
        self.monitor_btn.set_child(Gtk.Image.new_from_icon_name(_monitor_icon_name()))
        self.monitor_btn.set_tooltip_text("Monitor in Server Companion")
        self.monitor_btn.set_size_request(34, -1)
        attach_pointer_cursor(self.monitor_btn)
        self.monitor_btn.connect("clicked", self._monitor_clicked)

        self.join_btn = Gtk.Button()
        self.join_btn.set_can_focus(False)
        self.join_btn.add_css_class("flat")
        self.join_btn.set_child(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        self.join_btn.set_tooltip_text("Join Server")
        self.join_btn.set_size_request(34, -1)
        attach_pointer_cursor(self.join_btn)
        self.join_btn.connect("clicked", self._join_clicked)

        # All fixed widths (from config)
        stats_row.append(self._cell(0, time_box, first=True,  expand=False, width_px=RIGHT_COL_PX[0]))
        stats_row.append(self._cell(1, self.played_label, first=False, expand=False, width_px=RIGHT_COL_PX[1]))
        stats_row.append(self._cell(2, self.map_label,    first=False, expand=False, width_px=RIGHT_COL_PX[2]))
        stats_row.append(self._cell(3, self.players_box,  first=False, expand=False, width_px=RIGHT_COL_PX[3]))
        stats_row.append(self._cell(4, self.ping_label,   first=False, expand=False, width_px=RIGHT_COL_PX[4]))
        stats_row.append(self._cell(5, self.monitor_btn,  first=False, expand=False, width_px=RIGHT_COL_PX[5]))
        stats_row.append(self._cell(6, self.join_btn,     first=False, expand=False, width_px=RIGHT_COL_PX[6]))

        right_wrap.append(stats_row)
        self.append(right_wrap)

    def _mk_center_label(self, tooltip: str, max_chars: int | None = None) -> Gtk.Label:
        l = Gtk.Label()
        l.set_xalign(0.5)
        l.set_halign(Gtk.Align.CENTER)
        l.set_single_line_mode(True)
        l.set_ellipsize(Pango.EllipsizeMode.END)
        l.set_tooltip_text(tooltip)
        if max_chars is not None:
            try:
                l.set_width_chars(int(max_chars))
                l.set_max_width_chars(int(max_chars))
            except Exception:
                pass
        return l

    def _cell(self, col_idx: int, widget: Gtk.Widget, first: bool, expand: bool,
              width_px: int | None = None) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_valign(Gtk.Align.FILL)
        outer.set_vexpand(True)
        outer.set_hexpand(bool(expand))
        outer.add_css_class("cell")
        if first:
            outer.add_css_class("cell-first")
        if width_px is not None:
            outer.set_size_request(int(width_px), -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(widget)

        outer.append(inner)
        self._col_groups[col_idx].add_widget(outer)
        return outer

    def _mk_light_label(self, width_chars: int | None = None, expand: bool = False) -> Gtk.Label:
        label = Gtk.Label(xalign=0)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(bool(expand))
        label.set_halign(Gtk.Align.FILL if expand else Gtk.Align.START)
        if width_chars is not None:
            try:
                label.set_width_chars(int(width_chars))
                label.set_max_width_chars(int(width_chars))
            except Exception:
                pass
        return label

    def _apply_light_fixed_meta_label(self, label: Gtk.Label, key: str) -> None:
        width = int(LIGHT_ROW_FIXED_META_WIDTHS.get(key, 0) or 0)
        if width <= 0:
            return
        label.set_size_request(width, -1)
        label.set_hexpand(False)
        label.set_halign(Gtk.Align.CENTER)
        label.set_xalign(0.5)

    def _star_clicked(self, _btn):
        if self._bound_obj and callable(self._on_toggle_fav):
            self._on_toggle_fav(self._bound_obj)

    def _light_star_clicked(self, _btn):
        if self._bound_obj and callable(self._on_toggle_fav):
            self._on_toggle_fav(self._bound_obj)
            self._render_light_fav(self._bound_obj)

    def _monitor_clicked(self, _btn):
        if self._bound_obj and callable(self._on_monitor):
            self._on_monitor(self._bound_obj)

    def _join_clicked(self, _btn):
        if self._bound_obj and callable(self._on_join):
            self._on_join(self._bound_obj)

    def _copy_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            display = self.get_display()
            cb = display.get_clipboard()

            # Try the simple path first
            try:
                cb.set(text)  # works on many GI builds
                return
            except Exception:
                pass

            # Fallback: content provider
            try:
                provider = Gdk.ContentProvider.new_for_value(text)
                cb.set_content(provider)
            except Exception:
                pass
        except Exception:
            pass

    def _on_meta_right_click(self, _gesture, _n_press, _x, _y):
        # Copy plain ip:port (no flag)
        self._copy_text(self._ipport_plain)

    def _disconnect_notifies(self):
        if self._bound_obj and self._notify_ids:
            for hid in self._notify_ids:
                try:
                    self._bound_obj.disconnect(hid)
                except Exception:
                    pass
        self._notify_ids = []

    def _connect_notifies(self, obj: ServerObject):
        def on_any(_o, _pspec):
            self._render_from_obj(obj)

        props = [
            "fav", "password", "third_person", "name", "country", "ip", "gport",
            "mod_count", "mods_preview", "time", "timewarp", "played",
            "map_name", "players", "max_players", "queue", "ping",
        ]
        for p in props:
            try:
                hid = obj.connect(f"notify::{p}", on_any)
                self._notify_ids.append(hid)
            except Exception:
                pass

    def _set_label_text_if_changed(self, label, text):
        text = "" if text is None else str(text)
        key = id(label)
        if self._label_text_cache.get(key) == text:
            return
        try:
            label.set_text(text)
            self._label_text_cache[key] = text
        except Exception:
            pass

    def _set_button_label_if_changed(self, button, text):
        text = "" if text is None else str(text)
        key = id(button)
        if self._button_label_cache.get(key) == text:
            return
        try:
            button.set_label(text)
            self._button_label_cache[key] = text
        except Exception:
            pass

    def _set_tooltip_if_changed(self, widget, text):
        text = None if text is None else str(text)
        key = id(widget)
        if self._tooltip_text_cache.get(key) == text:
            return
        try:
            widget.set_tooltip_text(text)
            self._tooltip_text_cache[key] = text
        except Exception:
            pass

    def _set_opacity_if_changed(self, widget, value):
        try:
            value = float(value)
        except Exception:
            value = 1.0
        key = id(widget)
        if self._opacity_cache.get(key) == value:
            return
        try:
            widget.set_opacity(value)
            self._opacity_cache[key] = value
        except Exception:
            pass

    def _set_single_css_class(self, widget, cache_attr: str, new_class: str, possible_classes):
        old_class = getattr(self, cache_attr, None)
        if old_class == new_class:
            return
        try:
            if old_class:
                widget.remove_css_class(old_class)
            else:
                for css_class in possible_classes:
                    widget.remove_css_class(css_class)
            if new_class:
                widget.add_css_class(new_class)
            setattr(self, cache_attr, new_class)
        except Exception:
            pass

    def _render_from_obj(self, obj: ServerObject):
        start = time.perf_counter() if PERF_LOG_ENABLED else None
        last = start
        sections = {} if PERF_LOG_ENABLED else None

        def mark(section: str) -> None:
            nonlocal last
            if last is None or sections is None:
                return
            now = time.perf_counter()
            sections[section] = float(sections.get(section, 0.0) or 0.0) + (now - last)
            last = now

        try:
            fav = bool(obj.fav)
            self._set_button_label_if_changed(self.star_btn, "★" if fav else "☆")
            mark("buttons")
            self._set_single_css_class(self.star_btn, "_fav_css_class", "fav-on" if fav else "fav-off", ("fav-on", "fav-off"))
            mark("css")
            self._set_tooltip_if_changed(self.star_btn, "Unfavorite" if fav else "Favorite")
            self._fav_state = fav
            mark("fav")

            self._set_opacity_if_changed(self.lock_lbl, 1.0 if obj.password else 0.0)
            mark("other")
            if bool(obj.third_person):
                self._set_label_text_if_changed(self.tp_lbl, "3P")
                mark("perspective")
                self._set_tooltip_if_changed(self.tp_lbl, "Third-person allowed")
                mark("tooltips")
                self._set_single_css_class(
                    self.tp_lbl,
                    "_perspective_css_class",
                    "perspective-badge-3pp",
                    ("perspective-badge-1pp", "perspective-badge-3pp"),
                )
                mark("css")
            else:
                self._set_label_text_if_changed(self.tp_lbl, "1P")
                mark("perspective")
                self._set_tooltip_if_changed(self.tp_lbl, "First-person only")
                mark("tooltips")
                self._set_single_css_class(
                    self.tp_lbl,
                    "_perspective_css_class",
                    "perspective-badge-1pp",
                    ("perspective-badge-1pp", "perspective-badge-3pp"),
                )
                mark("css")
            self._set_opacity_if_changed(self.tp_lbl, 1.0)
            mark("perspective")

            full_name = obj.name or ""
            self._set_label_text_if_changed(self.name_label, full_name)
            mark("name")
            self._set_tooltip_if_changed(self.name_label, full_name if full_name else None)
            mark("tooltips")

            flag, country_tooltip, ipport_plain, meta_text = row_stable_display(obj)
            self._ipport_plain = ipport_plain
            mark("country")

            # SINGLE STRING => shrink ellipsizes mods first, IP stays readable
            self._set_label_text_if_changed(self.flag_label, flag)
            mark("country")
            self._set_tooltip_if_changed(self.flag_label, country_tooltip)
            mark("tooltips")
            self._set_label_text_if_changed(self.meta_label, meta_text)
            mark("meta")

            time_text, timewarp_text = row_time_display(obj)
            self._set_label_text_if_changed(self.time_main, time_text)
            self._set_label_text_if_changed(self.timewarp_lbl, timewarp_text)
            mark("other")

            self._set_label_text_if_changed(self.played_label, obj.played or "")
            self._set_label_text_if_changed(self.map_label, obj.map_name or "")
            players_text, queue_text = row_players_display(obj)
            self._set_label_text_if_changed(self.players_label, players_text)
            self._set_label_text_if_changed(self.queue_label, queue_text)
            mark("players")

            ping_text, ping_css_class = row_ping_display(obj)
            self._set_label_text_if_changed(self.ping_label, ping_text)
            mark("ping")
            self._set_single_css_class(self.ping_label, "_ping_css_class", ping_css_class, PING_CLASSES)
            mark("css")
        finally:
            if start is not None:
                _record_row_render_perf(time.perf_counter() - start, sections, obj)

    def _render_fast_scroll_from_obj(self, obj: ServerObject):
        fav = bool(obj.fav)
        self._set_button_label_if_changed(self.star_btn, "★" if fav else "☆")
        self._set_single_css_class(self.star_btn, "_fav_css_class", "fav-on" if fav else "fav-off", ("fav-on", "fav-off"))
        self._fav_state = fav

        self._set_opacity_if_changed(self.lock_lbl, 1.0 if obj.password else 0.0)
        if bool(obj.third_person):
            self._set_label_text_if_changed(self.tp_lbl, "3P")
            self._set_single_css_class(
                self.tp_lbl,
                "_perspective_css_class",
                "perspective-badge-3pp",
                ("perspective-badge-1pp", "perspective-badge-3pp"),
            )
        else:
            self._set_label_text_if_changed(self.tp_lbl, "1P")
            self._set_single_css_class(
                self.tp_lbl,
                "_perspective_css_class",
                "perspective-badge-1pp",
                ("perspective-badge-1pp", "perspective-badge-3pp"),
            )
        self._set_opacity_if_changed(self.tp_lbl, 1.0)

        full_name = obj.name or ""
        self._set_label_text_if_changed(self.name_label, full_name)

        flag, _country_tooltip, ipport_plain, _meta_text = row_stable_display(obj)
        self._ipport_plain = ipport_plain
        self._set_label_text_if_changed(self.flag_label, flag)
        self._set_label_text_if_changed(self.meta_label, ipport_plain)

        self._set_label_text_if_changed(self.time_main, "")
        self._set_label_text_if_changed(self.timewarp_lbl, "")
        self._set_label_text_if_changed(self.played_label, "")
        self._set_label_text_if_changed(self.map_label, obj.map_name or "")
        self._set_label_text_if_changed(self.players_label, "…")
        self._set_label_text_if_changed(self.queue_label, "")
        self._set_label_text_if_changed(self.ping_label, "…")
        _record_row_fast_render_perf()

    def _render_simple_from_obj(self, obj: ServerObject):
        try:
            players = int(getattr(obj, "players", 0) or 0)
        except Exception:
            players = 0
        try:
            max_players = int(getattr(obj, "max_players", 0) or 0)
        except Exception:
            max_players = 0
        try:
            ping = int(getattr(obj, "ping", -1))
        except Exception:
            ping = -1
        ping_text = "OFFLINE" if ping < 0 else f"{ping}ms"
        name = (getattr(obj, "name", "") or "").strip() or "(unnamed)"
        map_name = (getattr(obj, "map_name", "") or "").strip() or "Unknown"
        self.simple_label.set_text(f"{name} - {map_name} - {players}/{max_players} - {ping_text}")

    def _render_light_from_obj(self, obj: ServerObject):
        perspective = "3P" if bool(getattr(obj, "third_person", False)) else "1P"
        perspective_class = "perspective-badge-3pp" if perspective == "3P" else "perspective-badge-1pp"
        self._render_light_password(obj)
        if self._light_perspective_css_class != perspective_class:
            if self._light_perspective_css_class:
                self.light_perspective_label.remove_css_class(self._light_perspective_css_class)
            self.light_perspective_label.add_css_class(perspective_class)
            self._light_perspective_css_class = perspective_class
        self.light_perspective_label.set_text(perspective)
        self.light_name_label.set_text((getattr(obj, "name", "") or "").strip() or "(unnamed)")
        self._render_light_address(obj)
        self._render_light_time(obj)
        self._render_light_played(obj)
        self._render_light_map(obj)
        self._render_light_players(obj)
        self._render_light_ping(obj)
        self._render_light_fav(obj)
        self._render_light_mods(obj)

    def _render_light_players(self, obj: ServerObject):
        try:
            players = int(getattr(obj, "players", 0) or 0)
        except Exception:
            players = 0
        try:
            max_players = int(getattr(obj, "max_players", 0) or 0)
        except Exception:
            max_players = 0
        try:
            queue = int(getattr(obj, "queue", -1))
        except Exception:
            queue = -1
        players_text = f"{players}/{max_players}"
        if queue >= 0:
            players_text = f"{players_text} (Q{queue})"
        self.light_players_label.set_text(players_text)

    def _render_light_ping(self, obj: ServerObject):
        ping_text, ping_css_class = row_ping_display(obj)
        if ping_text == "OFFLINE":
            ping_text = "-"
        color = PING_MARKUP_COLORS.get(ping_css_class)
        if color:
            self.light_ping_label.set_markup(f'<span foreground="{color}">{ping_text}</span>')
        else:
            self.light_ping_label.set_text(ping_text)

    def _render_light_time(self, obj: ServerObject):
        time_text, timewarp_text = row_time_display(obj)
        self.light_time_label.set_text(f"{time_text} {timewarp_text}".strip())

    def _render_light_played(self, obj: ServerObject):
        self.light_played_label.set_text((getattr(obj, "played", "") or "").strip())

    def _render_light_map(self, obj: ServerObject):
        self.light_map_label.set_text((getattr(obj, "map_name", "") or "").strip())

    def _render_light_password(self, obj: ServerObject):
        self.light_lock_label.set_text("🔒" if bool(getattr(obj, "password", False)) else "")

    def _render_light_address(self, obj: ServerObject):
        country = (getattr(obj, "country", "") or "").strip().upper()
        flag_text = flag_for(country) if len(country) == 2 else ""
        self.light_flag_label.set_text(flag_text)
        ip = (getattr(obj, "ip", "") or "").strip()
        try:
            gport = int(getattr(obj, "gport", 0) or 0)
        except Exception:
            gport = 0
        address_text = f"{ip}:{gport}" if ip and gport > 0 else ip
        if LIGHT_ROW_MODS_ENABLED:
            _flag, _country_tooltip, _ipport_plain, meta_text = row_stable_display(obj)
            marker = "   Mods: "
            if marker in meta_text:
                mods_text = "Mods: " + meta_text.split(marker, 1)[1].strip()
                if mods_text:
                    address_text = f"{address_text}  {mods_text}" if address_text else mods_text
        self.light_address_label.set_text(address_text)

    def _render_light_mods(self, obj: ServerObject):
        return

    def _render_light_fav(self, obj: ServerObject):
        if LIGHT_ROW_FAV_ENABLED:
            if bool(getattr(obj, "fav", False)):
                self.light_fav_label.set_markup('<span foreground="#f5c542">★</span>')
            else:
                self.light_fav_label.set_markup('<span foreground="#7a7a7a">☆</span>')

    def _on_light_notify(self, obj: ServerObject, pspec):
        name = getattr(pspec, "name", "")
        if name in ("players", "max_players", "max-players", "queue"):
            self._render_light_players(obj)
        elif name == "ping":
            self._render_light_ping(obj)
        elif name in ("time", "timewarp"):
            self._render_light_time(obj)
        elif name == "played":
            self._render_light_played(obj)
        elif name in ("map_name", "map-name"):
            self._render_light_map(obj)
        elif name == "fav":
            self._render_light_fav(obj)

    def _connect_light_notifies(self, obj: ServerObject):
        props = ["players", "max_players", "queue", "ping", "time", "timewarp", "played", "map_name"]
        if LIGHT_ROW_FAV_ENABLED:
            props.append("fav")
        for prop in props:
            try:
                hid = obj.connect(f"notify::{prop}", self._on_light_notify)
                self._notify_ids.append(hid)
            except Exception:
                pass

    def full_render_bound(self):
        obj = self._bound_obj
        if isinstance(obj, ServerObject):
            if SIMPLE_ROWS_ENABLED:
                self._render_simple_from_obj(obj)
                return
            if LIGHT_ROWS_ENABLED:
                self._render_light_from_obj(obj)
                return
            self._render_from_obj(obj)

    def bind(self, obj: ServerObject, fast_scroll: bool = False):
        if SIMPLE_ROWS_ENABLED:
            self._bound_obj = obj
            self._render_simple_from_obj(obj)
            return
        if LIGHT_ROWS_ENABLED:
            if LIGHT_ROW_LIVE_ENABLED:
                self._disconnect_notifies()
            self._bound_obj = obj
            self._render_light_from_obj(obj)
            if LIGHT_ROW_LIVE_ENABLED:
                self._connect_light_notifies(obj)
            return

        if PERF_LOG_ENABLED:
            bind_start = time.perf_counter()
            disconnect_time = 0.0
            connect_time = 0.0
            render_time = 0.0
            try:
                part_start = time.perf_counter()
                self._disconnect_notifies()
                disconnect_time = time.perf_counter() - part_start

                self._bound_obj = obj

                part_start = time.perf_counter()
                if fast_scroll:
                    self._render_fast_scroll_from_obj(obj)
                else:
                    self._render_from_obj(obj)
                render_time = time.perf_counter() - part_start

                part_start = time.perf_counter()
                self._connect_notifies(obj)
                connect_time = time.perf_counter() - part_start
            finally:
                _record_row_bind_perf(
                    time.perf_counter() - bind_start,
                    disconnect_time,
                    connect_time,
                    render_time,
                )
            return

        self._disconnect_notifies()
        self._bound_obj = obj
        if fast_scroll:
            self._render_fast_scroll_from_obj(obj)
        else:
            self._render_from_obj(obj)
        self._connect_notifies(obj)
