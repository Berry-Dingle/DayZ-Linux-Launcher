#!/usr/bin/env python3
# update_ui.py
#
# Update toast / banner UI extracted from main.py with zero behavior changes,
# except: SKIP THIS VERSION persists the current tag, REMIND is 24h persisted.

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
        self.scrim: Gtk.Widget | None = None
        self.revealer: Gtk.Revealer | None = None
        self.update_title_lbl: Gtk.Label | None = None
        self.update_sub_lbl: Gtk.Label | None = None

    def build(self, overlay: Gtk.Overlay) -> Gtk.Revealer:
        scrim = Gtk.Box()
        scrim.set_hexpand(True)
        scrim.set_vexpand(True)
        scrim.set_visible(False)
        scrim.set_can_target(True)
        scrim.add_css_class("settings-scrim")
        overlay.add_overlay(scrim)

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

        self.scrim = scrim
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

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_row.set_halign(Gtk.Align.FILL)
        header_row.set_hexpand(True)

        header_spacer = Gtk.Box()
        header_spacer.set_size_request(34, -1)
        header_row.append(header_spacer)

        self.update_title_lbl = Gtk.Label(label="")
        self.update_title_lbl.add_css_class("update-title")
        self.update_title_lbl.set_xalign(0.5)
        self.update_title_lbl.set_halign(Gtk.Align.CENTER)
        self.update_title_lbl.set_hexpand(True)
        self.update_title_lbl.set_wrap(True)
        self.update_title_lbl.set_justify(Gtk.Justification.CENTER)
        header_row.append(self.update_title_lbl)

        close_btn = Gtk.Button()
        close_btn.set_can_focus(False)
        close_btn.add_css_class("flat")
        close_btn.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        close_btn.set_tooltip_text("Close")
        close_btn.set_halign(Gtk.Align.END)
        attach_pointer_cursor(close_btn)
        close_btn.connect("clicked", self.close_for_session)
        header_row.append(close_btn)
        card.append(header_row)

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

        dl_btn = Gtk.Button(label="UPDATE")
        dl_btn.set_can_focus(False)
        attach_pointer_cursor(dl_btn)
        dl_btn.connect("clicked", self.open_update_url)
        btn_row.append(dl_btn)

        later_btn = Gtk.Button(label="REMIND ME LATER")
        later_btn.set_can_focus(False)
        attach_pointer_cursor(later_btn)
        later_btn.connect("clicked", self.remind_later)
        btn_row.append(later_btn)

        skip_btn = Gtk.Button(label="SKIP THIS VERSION")
        skip_btn.set_can_focus(False)
        attach_pointer_cursor(skip_btn)
        skip_btn.connect("clicked", self.skip_this_version)
        btn_row.append(skip_btn)

        card.append(btn_row)
        return card

    def open_update_url(self, *_args):
        url = str((self._win._update_info or {}).get("url") or RELEASES_URL).strip() or RELEASES_URL
        Gio.AppInfo.launch_default_for_uri(url, None)

    def _set_visible(self, show: bool):
        if self.scrim:
            self.scrim.set_visible(bool(show))
        if self.revealer:
            self.revealer.set_reveal_child(bool(show))

    def close_for_session(self, *_args):
        try:
            self._win._update_card_dismissed = True
        except Exception:
            pass
        try:
            self._set_visible(False)
        except Exception:
            pass

    def remind_later(self, *_args):
        # Persist 24h suppression
        try:
            self._win.settings["update_remind_after_ts"] = int(time.time()) + 86400  # 24h
            save_settings(self._win.settings)
        except Exception:
            pass
        try:
            self._set_visible(False)
        except Exception:
            pass

    def skip_this_version(self, *_args):
        try:
            tag = str((self._win._update_info or {}).get("tag") or "").strip()
            if tag:
                self._win.settings["skipped_release_tag"] = tag
                save_settings(self._win.settings)
        except Exception:
            pass
        try:
            self._set_visible(False)
        except Exception:
            pass

    def maybe_show(self, ignore_suppression: bool = False):
        try:
            if not isinstance(self._win._update_info, dict):
                return False

            # Session-only suppress after dismiss
            if not ignore_suppression and bool(getattr(self._win, "_update_card_dismissed", False)):
                return False

            tag = str(self._win._update_info.get("tag") or "").strip()
            if not tag:
                return False
            if not ignore_suppression and tag == str(self._win.settings.get("skipped_release_tag", "") or "").strip():
                return False

            if str(APP_VERSION).strip() in ("", "dev"):
                return False
            if tag == str(APP_VERSION).strip():
                return False

            # NOTE: dismissed_release_tag is no longer used for hiding.
            # It may exist in settings.json from older runs; we intentionally ignore it.

            remind_after = int(self._win.settings.get("update_remind_after_ts", 0) or 0)
            if not ignore_suppression and remind_after and int(time.time()) < remind_after:
                return False

            if self.update_title_lbl:
                self.update_title_lbl.set_text("A New Version Of DZLL Is Available:")
            if self.update_sub_lbl:
                self.update_sub_lbl.set_text(f"Version {tag} can be downloaded via the button below.")
            self._set_visible(True)
        except Exception:
            pass
        return False
