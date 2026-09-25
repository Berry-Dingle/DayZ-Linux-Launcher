#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango


class JoinPreparationOverlayUI:
    """Build the shared Join preparation overlay used by Steam Client UGC."""

    def __init__(self, win):
        self.win = win
        self.join_preparation_scrim = None
        self.join_preparation_card = None
        self.join_preparation_heading = None
        self.join_preparation_spinner = None
        self.join_preparation_detail_label = None
        self.join_preparation_status_label = None
        self.join_preparation_progress_bar = None
        self.join_preparation_cancel_button = None

    def build(self, overlay: Gtk.Overlay):
        self.join_preparation_scrim = Gtk.Box()
        self.join_preparation_scrim.set_hexpand(True)
        self.join_preparation_scrim.set_vexpand(True)
        self.join_preparation_scrim.set_visible(False)
        self.join_preparation_scrim.set_can_target(True)
        self.join_preparation_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.join_preparation_scrim)

        self.join_preparation_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        self.join_preparation_card.set_halign(Gtk.Align.CENTER)
        self.join_preparation_card.set_valign(Gtk.Align.CENTER)
        self.join_preparation_card.set_visible(False)
        self.join_preparation_card.set_can_target(True)
        self.join_preparation_card.set_margin_start(40)
        self.join_preparation_card.set_margin_end(40)
        self.join_preparation_card.set_margin_top(40)
        self.join_preparation_card.set_margin_bottom(40)
        self.join_preparation_card.set_size_request(520, -1)
        self.join_preparation_card.add_css_class("dzll-overlay-card")

        self.join_preparation_heading = Gtk.Label(label="")
        self.join_preparation_heading.set_xalign(0.0)
        self.join_preparation_heading.add_css_class("dzll-overlay-heading")
        self.join_preparation_card.append(self.join_preparation_heading)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("join-preparation-separator")
        self.join_preparation_card.append(separator)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_halign(Gtk.Align.FILL)
        log_box.set_hexpand(True)

        self.join_preparation_detail_label = Gtk.Label(label="")
        self.join_preparation_detail_label.set_xalign(0.0)
        self.join_preparation_detail_label.set_wrap(True)
        self.join_preparation_detail_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.join_preparation_detail_label.add_css_class("join-preparation-status")
        log_box.append(self.join_preparation_detail_label)

        line2_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line2_row.set_halign(Gtk.Align.START)
        self.join_preparation_spinner = Gtk.Spinner()
        self.join_preparation_spinner.set_spinning(False)
        line2_row.append(self.join_preparation_spinner)

        self.join_preparation_status_label = Gtk.Label(label="")
        self.join_preparation_status_label.set_xalign(0.0)
        self.join_preparation_status_label.set_wrap(True)
        self.join_preparation_status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.join_preparation_status_label.add_css_class("join-preparation-status")
        line2_row.append(self.join_preparation_status_label)
        log_box.append(line2_row)

        self.join_preparation_progress_bar = Gtk.ProgressBar()
        self.join_preparation_progress_bar.add_css_class("join-preparation-progress")
        self.join_preparation_progress_bar.set_fraction(0.0)
        self.join_preparation_progress_bar.set_show_text(False)
        self.join_preparation_progress_bar.set_pulse_step(0.05)
        self.join_preparation_progress_bar.set_visible(False)
        log_box.append(self.join_preparation_progress_bar)
        self.join_preparation_card.append(log_box)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.join_preparation_cancel_button = Gtk.Button(label="Cancel")
        self.join_preparation_cancel_button.connect(
            "clicked",
            lambda *_: self.win._join_preparation_cancel_clicked(),
        )
        buttons.append(self.join_preparation_cancel_button)
        self.join_preparation_card.append(buttons)
        overlay.add_overlay(self.join_preparation_card)

        self.win.join_preparation_scrim = self.join_preparation_scrim
        self.win.join_preparation_card = self.join_preparation_card
        self.win.join_preparation_heading = self.join_preparation_heading
        self.win.join_preparation_detail_label = self.join_preparation_detail_label
        self.win.join_preparation_spinner = self.join_preparation_spinner
        self.win.join_preparation_status_label = self.join_preparation_status_label
        self.win.join_preparation_progress_bar = self.join_preparation_progress_bar
        self.win.join_preparation_cancel_button = self.join_preparation_cancel_button

    def _hide_join_preparation_overlay(self):
        try:
            self.join_preparation_spinner.set_spinning(False)
            self.join_preparation_spinner.set_visible(False)
        except Exception:
            pass
        try:
            self.join_preparation_progress_bar.set_fraction(0.0)
            self.join_preparation_progress_bar.set_show_text(False)
            self.join_preparation_progress_bar.set_text("")
            self.join_preparation_progress_bar.set_visible(False)
        except Exception:
            pass
        self.join_preparation_card.set_visible(False)
        self.join_preparation_scrim.set_visible(False)

    def _reset_join_preparation_overlay(self):
        try:
            self.join_preparation_spinner.set_spinning(False)
            self.join_preparation_spinner.set_visible(False)
            self.join_preparation_heading.set_label("")
            self.join_preparation_detail_label.set_label("")
            self.join_preparation_status_label.set_label("")
        except Exception:
            pass
        self.win._steam_ugc_worker_in_progress = False
