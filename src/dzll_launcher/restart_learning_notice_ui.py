#!/usr/bin/env python3

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk, Pango

from .companion_restart_phase2_runtime import RuntimeNotice
from .ui_row import attach_pointer_cursor


class RestartLearningNoticeUI:
    def __init__(self, window):
        self._win = window
        self.scrim = None
        self.revealer = None
        self.title_label = None
        self.body_label = None
        self.path_label = None
        self.copy_button = None
        self.card = None

    def build(self, overlay: Gtk.Overlay) -> Gtk.Revealer:
        self.scrim = Gtk.Box()
        self.scrim.set_hexpand(True)
        self.scrim.set_vexpand(True)
        self.scrim.set_visible(False)
        self.scrim.set_can_target(True)
        self.scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.scrim)

        self.revealer = Gtk.Revealer()
        self.revealer.set_reveal_child(False)
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(180)
        self.revealer.set_halign(Gtk.Align.CENTER)
        self.revealer.set_valign(Gtk.Align.START)
        self.revealer.set_margin_top(10)
        self.revealer.set_child(self._build_card())
        overlay.add_overlay(self.revealer)
        return self.revealer

    def _build_card(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("update-card")
        card.add_css_class("restart-learning-notice-card")
        self.card = card
        card.set_size_request(620, -1)
        card.set_margin_start(10)
        card.set_margin_end(10)
        card.set_margin_top(6)
        card.set_margin_bottom(6)

        self.title_label = Gtk.Label()
        self.title_label.add_css_class("update-title")
        self.title_label.set_wrap(True)
        self.title_label.set_justify(Gtk.Justification.CENTER)
        card.append(self.title_label)

        self.body_label = Gtk.Label()
        self.body_label.add_css_class("update-subtitle")
        self.body_label.set_wrap(True)
        self.body_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.body_label.set_justify(Gtk.Justification.CENTER)
        self.body_label.set_max_width_chars(82)
        card.append(self.body_label)

        self.path_label = Gtk.Label()
        self.path_label.set_wrap(True)
        self.path_label.set_wrap_mode(Pango.WrapMode.CHAR)
        self.path_label.set_selectable(True)
        self.path_label.add_css_class("dim-label")
        card.append(self.path_label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_homogeneous(True)
        self.copy_button = Gtk.Button(label="COPY BACKUP PATH")
        self.copy_button.set_size_request(160, -1)
        self.copy_button.connect("clicked", self._copy_path)
        attach_pointer_cursor(self.copy_button)
        buttons.append(self.copy_button)
        ok_button = Gtk.Button(label="OK")
        ok_button.set_size_request(160, -1)
        ok_button.add_css_class("suggested-action")
        ok_button.connect("clicked", self.hide)
        attach_pointer_cursor(ok_button)
        buttons.append(ok_button)
        card.append(buttons)
        return card

    def show(self, notice: RuntimeNotice) -> bool:
        if not isinstance(notice, RuntimeNotice):
            return False
        if self.card is not None:
            for css_class in (
                "restart-notice-reset",
                "restart-notice-recovery",
                "restart-notice-error",
            ):
                self.card.remove_css_class(css_class)
            kind = str(notice.kind or "")
            if kind == "legacy_reset":
                self.card.add_css_class("restart-notice-reset")
            elif kind == "corrupt_state_recovered":
                self.card.add_css_class("restart-notice-recovery")
            elif kind in {"initialization_failed", "persistence_write_failed"}:
                self.card.add_css_class("restart-notice-error")
        self.title_label.set_text(notice.title)
        self.body_label.set_text(notice.body)
        path = str(notice.backup_path or "")
        self.path_label.set_text(path)
        self.path_label.set_visible(bool(path))
        self.copy_button.set_visible(bool(path))
        self.scrim.set_visible(True)
        self.revealer.set_reveal_child(True)
        return True

    def hide(self, *_args):
        self.revealer.set_reveal_child(False)
        self.scrim.set_visible(False)
        callback = getattr(self._win, "_on_restart_learning_notice_hidden", None)
        if callable(callback):
            callback()

    def visible(self) -> bool:
        return bool(self.revealer and self.revealer.get_reveal_child())

    def _copy_path(self, *_args):
        text = self.path_label.get_text() if self.path_label else ""
        if not text:
            return
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.get_clipboard().set(text)
        except Exception:
            pass
