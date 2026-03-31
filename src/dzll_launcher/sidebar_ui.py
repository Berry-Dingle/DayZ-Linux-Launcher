#!/usr/bin/env python3
# sidebar_ui.py
#
# Sidebar UI extracted from main.py with ZERO behavior change.

import os

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Pango

from .config import (
    SIDEBAR_WIDTH,
    SIDEBAR_INNER_PADDING,
    LOGO_WIDTH_RATIO,
    LOGO_MIN_HEIGHT,
    DISCLAIMER_GAP_ABOVE,
    DISCLAIMER_TEXT,
    LOGO_PATH,
    IMAGES_DIR,
)

from .ui_row import hr, attach_pointer_cursor

def connect_mutually_exclusive_checkbuttons(cb_a, cb_b, on_change):
    """
    When cb_a is turned ON, cb_b is forced OFF (and vice versa).
    Calls on_change() after any toggle.
    """
    guard = {"busy": False}

    def _wrap(src, other):
        def _on_toggled(_cb):
            if guard["busy"]:
                try:
                    on_change()
                except Exception:
                    pass
                return
            try:
                guard["busy"] = True
                if src.get_active() and other.get_active():
                    other.set_active(False)
            except Exception:
                pass
            finally:
                guard["busy"] = False
            try:
                on_change()
            except Exception:
                pass
        return _on_toggled

    cb_a.connect("toggled", _wrap(cb_a, cb_b))
    cb_b.connect("toggled", _wrap(cb_b, cb_a))

def build_sidebar(window) -> Gtk.Widget:
    """
    Builds the entire sidebar and assigns the same widget refs onto `window`:
      - window.search_entry
      - window.map_model
      - window.map_dropdown
      - window.cb_show_fav / cb_1pp_only / cb_no_password / cb_online_only / cb_played_only
      - window.reset_btn
    """
    sidebar_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    sidebar_frame.add_css_class("sidebar-frame")
    sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)
    sidebar_frame.set_hexpand(False)
    sidebar_frame.set_vexpand(True)
    sidebar_frame.set_halign(Gtk.Align.START)

    sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    sidebar.set_margin_top(SIDEBAR_INNER_PADDING)
    sidebar.set_margin_bottom(SIDEBAR_INNER_PADDING)
    sidebar.set_margin_start(SIDEBAR_INNER_PADDING)
    sidebar.set_margin_end(SIDEBAR_INNER_PADDING)
    sidebar.set_hexpand(True)
    sidebar.set_halign(Gtk.Align.FILL)
    sidebar.set_vexpand(True)
    sidebar_frame.append(sidebar)

    sidebar.append(Gtk.Label(label="Search", xalign=0))
    window.search_entry = Gtk.Entry()
    window.search_entry.set_placeholder_text("Filter By Name Or IP")
    window.search_entry.connect("changed", window._on_filter_changed)
    sidebar.append(window.search_entry)

    sidebar.append(hr())

    sidebar.append(Gtk.Label(label="Map", xalign=0))
    window.map_model = Gtk.StringList.new(["All"])
    window.map_dropdown = Gtk.DropDown.new(window.map_model, None)
    window.map_dropdown.connect("notify::selected", window._on_filter_changed)
    sidebar.append(window.map_dropdown)

    window.cb_show_fav = Gtk.CheckButton(label="Show Favorites")
    window.cb_show_fav.connect("toggled", window._on_filter_changed)
    sidebar.append(window.cb_show_fav)

    window.cb_1pp_only = Gtk.CheckButton(label="1st Person Only")
    sidebar.append(window.cb_1pp_only)

    window.cb_3pp_only = Gtk.CheckButton(label="3rd Person Only")
    sidebar.append(window.cb_3pp_only)

    connect_mutually_exclusive_checkbuttons(
        window.cb_1pp_only,
        window.cb_3pp_only,
        window._on_filter_changed,
    )

    window.cb_no_password = Gtk.CheckButton(label="No Password")
    window.cb_no_password.connect("toggled", window._on_filter_changed)
    sidebar.append(window.cb_no_password)

    window.cb_online_only = Gtk.CheckButton(label="Online Only")
    window.cb_online_only.connect("toggled", window._on_filter_changed)
    sidebar.append(window.cb_online_only)

    window.cb_played_only = Gtk.CheckButton(label="Played (Has History)")
    window.cb_played_only.connect("toggled", window._on_filter_changed)
    sidebar.append(window.cb_played_only)

    sidebar.append(hr())

    window.reset_btn = Gtk.Button(label="RESET")
    window.reset_btn.connect("clicked", window._on_reset_clicked)
    sidebar.append(window.reset_btn)

    sidebar.append(hr())

    try:
        if os.path.exists(LOGO_PATH):
            logo = Gtk.Picture.new_for_filename(LOGO_PATH)
            logo.set_content_fit(Gtk.ContentFit.CONTAIN)
            logo.set_can_shrink(False)
            logo.set_halign(Gtk.Align.CENTER)
            logo.set_valign(Gtk.Align.START)
            logo.set_margin_top(0)
            logo.set_size_request(int(SIDEBAR_WIDTH * LOGO_WIDTH_RATIO), LOGO_MIN_HEIGHT)
            sidebar.append(logo)
    except Exception:
        pass

    # --- Buy Me A Coffee button (sidebar, under logo) ---
    try:
        bmc_path = os.path.join(IMAGES_DIR, "buy-coffee.png")
        if os.path.exists(bmc_path):
            bmc_btn = Gtk.Button()
            bmc_btn.set_can_focus(False)
            bmc_btn.add_css_class("flat")
            attach_pointer_cursor(bmc_btn)
            bmc_btn.set_tooltip_text("Support DZLL (Buy Me A Coffee)")

            pic = Gtk.Picture.new_for_filename(bmc_path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(False)
            pic.set_halign(Gtk.Align.CENTER)
            pic.set_valign(Gtk.Align.START)

            bmc_btn.set_child(pic)

            def _open_bmc(*_a):
                try:
                    Gio.AppInfo.launch_default_for_uri("https://buymeacoffee.com/berry.dingle", None)
                except Exception as e:
                    print(f"[BMC] Failed to open link: {e}")

            bmc_btn.connect("clicked", _open_bmc)
            sidebar.append(bmc_btn)
    except Exception:
        pass
    # -----------------------------------------------

    gap = Gtk.Box()
    gap.set_size_request(-1, DISCLAIMER_GAP_ABOVE)
    sidebar.append(gap)

    push = Gtk.Box()
    push.set_vexpand(True)
    sidebar.append(push)

    disclaimer = Gtk.Label(label=DISCLAIMER_TEXT)
    disclaimer.set_wrap(True)
    disclaimer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    disclaimer.set_xalign(0)
    disclaimer.set_halign(Gtk.Align.FILL)
    disclaimer.set_hexpand(True)
    effective = SIDEBAR_WIDTH - (SIDEBAR_INNER_PADDING * 2)
    disclaimer.set_size_request(effective, -1)
    disclaimer.add_css_class("disclaimer")
    sidebar.append(disclaimer)

    return sidebar_frame