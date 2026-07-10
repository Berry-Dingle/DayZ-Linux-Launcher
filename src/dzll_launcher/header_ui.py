#!/usr/bin/env python3
# header_ui.py
#
# Header builder extracted from main.py with zero behavior changes.

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from .config import (
    RIGHT_BLOCK_WIDTH,
    RIGHT_COL_PX,
    NAME_BLOCK_MIN_WIDTH,
    ICON_COL_WIDTH,
)

from .ui_row import attach_pointer_cursor


class HeaderUI:
    def __init__(self, window, col_groups):
        self._win = window
        self._col_groups = col_groups

    def _hdr_cell_button(
        self,
        col_idx: int,
        text: str,
        first: bool,
        expand: bool,
        on_click=None,
        noborder_left: bool = False,
        width_px: int | None = None,
        max_chars: int | None = None,
    ):
        btn = Gtk.Button()
        btn.set_can_focus(False)
        btn.add_css_class("flat")
        attach_pointer_cursor(btn)
        if on_click is not None:
            btn.connect("clicked", on_click)

        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_single_line_mode(True)
        lbl.add_css_class("colhdr")
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        if max_chars is not None:
            try:
                lbl.set_width_chars(int(max_chars))
                lbl.set_max_width_chars(int(max_chars))
            except Exception:
                pass
        btn.set_child(lbl)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_valign(Gtk.Align.FILL)
        outer.set_vexpand(True)
        outer.set_hexpand(bool(expand))
        outer.add_css_class("cell")
        if first:
            outer.add_css_class("cell-first")
        if noborder_left:
            outer.add_css_class("cell-noborder-left")
        if width_px is not None:
            outer.set_size_request(int(width_px), -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(btn)

        outer.append(inner)
        self._col_groups[col_idx].add_widget(outer)
        return outer, btn, lbl

    def _hdr_cell_label(
        self,
        col_idx: int,
        text: str,
        first: bool,
        expand: bool,
        noborder_left: bool = False,
        width_px: int | None = None,
        max_chars: int | None = None,
    ):
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_single_line_mode(True)
        lbl.add_css_class("colhdr")
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        if max_chars is not None:
            try:
                lbl.set_width_chars(int(max_chars))
                lbl.set_max_width_chars(int(max_chars))
            except Exception:
                pass

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_halign(Gtk.Align.FILL)
        outer.set_valign(Gtk.Align.FILL)
        outer.set_vexpand(True)
        outer.set_hexpand(bool(expand))
        outer.add_css_class("cell")
        if first:
            outer.add_css_class("cell-first")
        if noborder_left:
            outer.add_css_class("cell-noborder-left")
        if width_px is not None:
            outer.set_size_request(int(width_px), -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(lbl)

        outer.append(inner)
        self._col_groups[col_idx].add_widget(outer)
        return outer, lbl

    def build(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(10)
        header.set_margin_end(10)
        header.set_margin_top(2)
        header.set_margin_bottom(0)
        header.set_valign(Gtk.Align.CENTER)
        header.set_vexpand(False)

        fav_hdr = Gtk.Label(label="FAV")
        fav_hdr.add_css_class("colhdr")
        fav_hdr.add_css_class("fav-hdr")
        fav_hdr.set_xalign(0.5)
        fav_hdr.set_halign(Gtk.Align.CENTER)
        fav_hdr.set_size_request(40, -1)
        header.append(fav_hdr)

        icon_ph = Gtk.Label(label="")
        icon_ph.set_size_request(ICON_COL_WIDTH, -1)
        header.append(icon_ph)

        left_title = Gtk.Label(label="NAME / IP / MODS", xalign=0)
        left_title.add_css_class("colhdr")
        left_title.set_hexpand(True)
        left_title.set_valign(Gtk.Align.CENTER)
        header.append(left_title)
        left_title.set_size_request(NAME_BLOCK_MIN_WIDTH, -1)

        stats_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        stats_hdr.add_css_class("rightblock")
        stats_hdr.set_size_request(RIGHT_BLOCK_WIDTH, -1)
        stats_hdr.set_hexpand(False)
        stats_hdr.set_halign(Gtk.Align.END)
        stats_hdr.set_valign(Gtk.Align.CENTER)
        stats_hdr.set_vexpand(False)

        w, self._win.hdr_time_lbl = self._hdr_cell_label(0, "TIME", True, False, width_px=RIGHT_COL_PX[0], max_chars=7)
        stats_hdr.append(w)

        w, self._win.hdr_played_btn, self._win.hdr_played_lbl = self._hdr_cell_button(
            1, "PLAYED", False, False, lambda *_: self._win._set_sort("played"),
            width_px=RIGHT_COL_PX[1], max_chars=9
        )
        stats_hdr.append(w)

        w, self._win.hdr_map_lbl = self._hdr_cell_label(2, "MAP", False, False, width_px=RIGHT_COL_PX[2], max_chars=7)
        stats_hdr.append(w)

        w, self._win.hdr_players_btn, self._win.hdr_players_lbl = self._hdr_cell_button(
            3, "PLAYERS", False, False, lambda *_: self._win._set_sort("players"),
            width_px=RIGHT_COL_PX[3], max_chars=10
        )
        stats_hdr.append(w)

        w, self._win.hdr_ping_btn, self._win.hdr_ping_lbl = self._hdr_cell_button(
            4, "PING", False, False, lambda *_: self._win._set_sort("ping"),
            width_px=RIGHT_COL_PX[4], max_chars=8
        )
        stats_hdr.append(w)

        w, _ = self._hdr_cell_label(5, "", False, False, width_px=RIGHT_COL_PX[5], max_chars=1)
        stats_hdr.append(w)
        w, _ = self._hdr_cell_label(6, "", False, False, noborder_left=True, width_px=RIGHT_COL_PX[6], max_chars=6)
        stats_hdr.append(w)

        header.append(stats_hdr)
        return header
