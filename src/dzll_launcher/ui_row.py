# ui_row.py
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

    ping = GObject.Property(type=int, default=-1)  # <0 = offline

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


class ServerRowWidget(Gtk.Box):
    """
    Binds to a ServerObject (GObject properties).
    Updates in-place via notify:: signals (NO store.splice needed).
    """

    def __init__(self, col_groups, on_toggle_fav, on_refresh, on_join):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._col_groups = col_groups
        self._on_toggle_fav = on_toggle_fav
        self._on_refresh = on_refresh
        self._on_join = on_join
        self._bound_obj = None
        self._notify_ids = []

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(10)
        self.set_margin_end(10)

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

        # line 2: 3pp + meta (SINGLE LABEL: IP always visible, mods ellipsize)
        line2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.tp_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.tp_wrap.set_size_request(ICON_COL_WIDTH, -1)
        self.tp_wrap.set_margin_start(6)
        self.tp_lbl = Gtk.Label(label="👤")
        self.tp_lbl.set_tooltip_text("Third Person Enabled")
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

        MIN_WIN_WIDTH = (
            SIDEBAR_WIDTH
            + 1
            + SIDEBAR_INNER_PADDING * 2
            + NAME_BLOCK_MIN_WIDTH
            + RIGHT_BLOCK_WIDTH
            + 80
        )
        self.set_size_request(MIN_WIN_WIDTH, -1)
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
        self.players_label = self._mk_center_label("Players / Max", max_chars=7)
        self.ping_label = self._mk_center_label("Ping (ms)", max_chars=7)

        self.refresh_btn = Gtk.Button()
        self.refresh_btn.set_can_focus(False)
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.set_child(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        self.refresh_btn.set_tooltip_text("Refresh This Server")
        self.refresh_btn.set_size_request(34, -1)
        attach_pointer_cursor(self.refresh_btn)
        self.refresh_btn.connect("clicked", self._refresh_clicked)

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
        stats_row.append(self._cell(3, self.players_label,first=False, expand=False, width_px=RIGHT_COL_PX[3]))
        stats_row.append(self._cell(4, self.ping_label,   first=False, expand=False, width_px=RIGHT_COL_PX[4]))
        stats_row.append(self._cell(5, self.refresh_btn,  first=False, expand=False, width_px=RIGHT_COL_PX[5]))
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

    def _star_clicked(self, _btn):
        if self._bound_obj and callable(self._on_toggle_fav):
            self._on_toggle_fav(self._bound_obj)

    def _refresh_clicked(self, _btn):
        if self._bound_obj and callable(self._on_refresh):
            self._on_refresh(self._bound_obj)

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
            "map_name", "players", "max_players", "ping",
        ]
        for p in props:
            try:
                hid = obj.connect(f"notify::{p}", on_any)
                self._notify_ids.append(hid)
            except Exception:
                pass

    def _render_from_obj(self, obj: ServerObject):
        self.star_btn.set_label("★" if obj.fav else "☆")
        self.star_btn.remove_css_class("fav-on")
        self.star_btn.remove_css_class("fav-off")
        self.star_btn.add_css_class("fav-on" if obj.fav else "fav-off")
        self.star_btn.set_tooltip_text("Unfavorite" if obj.fav else "Favorite")

        self.lock_lbl.set_opacity(1.0 if obj.password else 0.0)
        self.tp_lbl.set_opacity(1.0 if obj.third_person else 0.0)

        full_name = obj.name or ""
        self.name_label.set_text(full_name)
        self.name_label.set_tooltip_text(full_name if full_name else None)

        cc = (obj.country or "").upper()
        flag = flag_for(cc)
        self._ipport_plain = f"{obj.ip}:{obj.gport}"

        # mods preview (already truncated + "..." in main.py)
        if int(obj.mod_count or 0) > 0:
            pv = (obj.mods_preview or "").strip()
            mods_txt = f"Mods: {int(obj.mod_count)} ({pv})" if pv else f"Mods: {int(obj.mod_count)}"
        else:
            mods_txt = "Mods: 0"

        # SINGLE STRING => shrink ellipsizes mods first, IP stays readable
        self.flag_label.set_text(flag)
        self.flag_label.set_tooltip_text(country_name_for(cc))
        self.meta_label.set_text(f"{self._ipport_plain}   {mods_txt}")

        self.time_main.set_text((obj.time or "").strip() if (obj.time or "").strip() else "--:--")
        self.timewarp_lbl.set_text(fmt_timewarp(obj.timewarp).strip())

        self.played_label.set_text(obj.played or "")
        self.map_label.set_text(obj.map_name or "")
        self.players_label.set_text(f"{int(obj.players)}/{int(obj.max_players)}")

        try:
            p = int(obj.ping)
        except Exception:
            p = -1

        self.ping_label.set_text("OFFLINE" if p < 0 else f"{p}ms")
        for c in PING_CLASSES:
            self.ping_label.remove_css_class(c)
        self.ping_label.add_css_class(ping_class(p))

    def bind(self, obj: ServerObject):
        self._disconnect_notifies()
        self._bound_obj = obj
        self._render_from_obj(obj)
        self._connect_notifies(obj)
