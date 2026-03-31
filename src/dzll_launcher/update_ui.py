#!/usr/bin/env python3
# update_ui.py
#
# Update toast / banner UI extracted from main.py with zero behavior changes,
# except: DISMISS is session-only (per user requirement), REMIND is 24h persisted.

import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio

from .config import APP_VERSION, RELEASES_URL
from .settings import save_settings
from .ui_row import attach_pointer_cursor


class UpdateUI:
    def __init__(self, window):
        self._win = window
        self.revealer: Gtk.Revealer | None = None
        self.update_title_lbl: Gtk.Label | None = None
        self.update_sub_lbl: Gtk.Label | None = None

    def build(self, overlay: Gtk.Overlay) -> Gtk.Revealer:
        revealer = Gtk.Revealer()
        revealer.set_reveal_child(False)
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(180)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.START)
        revealer.set_hexpand(False)
        revealer.set_vexpand(False)
        revealer.set_margin_top(10)

        card = self._build_update_card()
        revealer.set_child(card)
        overlay.add_overlay(revealer)

        self.revealer = revealer
        return revealer

    def _build_update_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("update-card")
        card.set_margin_start(10)
        card.set_margin_end(10)
        card.set_margin_top(6)
        card.set_margin_bottom(6)
        card.set_size_request(560, -1)
        card.set_hexpand(False)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.START)

        self.update_title_lbl = Gtk.Label(label="")
        self.update_title_lbl.add_css_class("update-title")
        self.update_title_lbl.set_xalign(0.5)
        self.update_title_lbl.set_halign(Gtk.Align.CENTER)
        self.update_title_lbl.set_wrap(True)
        self.update_title_lbl.set_justify(Gtk.Justification.CENTER)
        card.append(self.update_title_lbl)

        self.update_sub_lbl = Gtk.Label(label="")
        self.update_sub_lbl.add_css_class("update-subtitle")
        self.update_sub_lbl.set_xalign(0.5)
        self.update_sub_lbl.set_halign(Gtk.Align.CENTER)
        self.update_sub_lbl.set_wrap(True)
        self.update_sub_lbl.set_justify(Gtk.Justification.CENTER)
        card.append(self.update_sub_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        btn_row.set_valign(Gtk.Align.CENTER)

        dl_btn = Gtk.Button(label="DOWNLOAD")
        dl_btn.set_can_focus(False)
        attach_pointer_cursor(dl_btn)
        dl_btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(RELEASES_URL, None))
        btn_row.append(dl_btn)

        later_btn = Gtk.Button(label="REMIND ME LATER")
        later_btn.set_can_focus(False)
        attach_pointer_cursor(later_btn)
        later_btn.connect("clicked", self.remind_later)
        btn_row.append(later_btn)

        dismiss_btn = Gtk.Button(label="DISMISS")
        dismiss_btn.set_can_focus(False)
        attach_pointer_cursor(dismiss_btn)
        dismiss_btn.connect("clicked", self.dismiss)
        btn_row.append(dismiss_btn)

        card.append(btn_row)
        return card

    def remind_later(self, *_args):
        # Persist 24h suppression
        try:
            self._win.settings["update_remind_after_ts"] = int(time.time()) + 86400  # 24h
            save_settings(self._win.settings)
        except Exception:
            pass
        try:
            if self.revealer:
                self.revealer.set_reveal_child(False)
        except Exception:
            pass

    def dismiss(self, *_args):
        # Session-only dismiss (does NOT persist)
        try:
            self._win._update_card_dismissed = True
        except Exception:
            pass
        try:
            if self.revealer:
                self.revealer.set_reveal_child(False)
        except Exception:
            pass

    def maybe_show(self):
        try:
            if not isinstance(self._win._update_info, dict):
                return False

            # Session-only suppress after dismiss
            if bool(getattr(self._win, "_update_card_dismissed", False)):
                return False

            tag = str(self._win._update_info.get("tag") or "").strip()
            if not tag:
                return False

            if str(APP_VERSION).strip() in ("", "dev"):
                return False
            if tag == str(APP_VERSION).strip():
                return False

            # NOTE: dismissed_release_tag is no longer used for hiding.
            # It may exist in settings.json from older runs; we intentionally ignore it.

            remind_after = int(self._win.settings.get("update_remind_after_ts", 0) or 0)
            if remind_after and int(time.time()) < remind_after:
                return False

            if self.update_title_lbl:
                self.update_title_lbl.set_text("A New Version Of DZLL Is Available:")
            if self.update_sub_lbl:
                self.update_sub_lbl.set_text(f"Version {tag} can be downloaded via the button below.")
            if self.revealer:
                self.revealer.set_reveal_child(True)
        except Exception:
            pass
        return False