#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango


class JoinPreparationOverlayUI:
    """Build the shared Join preparation overlay used by Steam Client UGC."""

    def __init__(self, win):
        self.win = win
        self.steamcmd_auth_scrim = None
        self.steamcmd_auth_box = None
        self.steamcmd_task_heading = None
        self.steamcmd_spinner = None
        self.steamcmd_line1 = None
        self.steamcmd_line2 = None
        self.steamcmd_prog_bar = None
        self.steamcmd_cancel_btn = None

    def build(self, overlay: Gtk.Overlay):
        self.steamcmd_auth_scrim = Gtk.Box()
        self.steamcmd_auth_scrim.set_hexpand(True)
        self.steamcmd_auth_scrim.set_vexpand(True)
        self.steamcmd_auth_scrim.set_visible(False)
        self.steamcmd_auth_scrim.set_can_target(True)
        self.steamcmd_auth_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.steamcmd_auth_scrim)

        self.steamcmd_auth_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        self.steamcmd_auth_box.set_halign(Gtk.Align.CENTER)
        self.steamcmd_auth_box.set_valign(Gtk.Align.CENTER)
        self.steamcmd_auth_box.set_visible(False)
        self.steamcmd_auth_box.set_can_target(True)
        self.steamcmd_auth_box.set_margin_start(40)
        self.steamcmd_auth_box.set_margin_end(40)
        self.steamcmd_auth_box.set_margin_top(40)
        self.steamcmd_auth_box.set_margin_bottom(40)
        self.steamcmd_auth_box.set_size_request(520, -1)
        self.steamcmd_auth_box.add_css_class("steamcmd-auth-card")

        self.steamcmd_task_heading = Gtk.Label(label="")
        self.steamcmd_task_heading.set_xalign(0.0)
        self.steamcmd_task_heading.add_css_class("steamcmd-heading")
        self.steamcmd_auth_box.append(self.steamcmd_task_heading)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("steamcmd-hr")
        self.steamcmd_auth_box.append(separator)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_halign(Gtk.Align.FILL)
        log_box.set_hexpand(True)

        self.steamcmd_line1 = Gtk.Label(label="")
        self.steamcmd_line1.set_xalign(0.0)
        self.steamcmd_line1.set_wrap(True)
        self.steamcmd_line1.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.steamcmd_line1.add_css_class("steamcmd-log")
        log_box.append(self.steamcmd_line1)

        line2_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line2_row.set_halign(Gtk.Align.START)
        self.steamcmd_spinner = Gtk.Spinner()
        self.steamcmd_spinner.set_spinning(False)
        line2_row.append(self.steamcmd_spinner)

        self.steamcmd_line2 = Gtk.Label(label="")
        self.steamcmd_line2.set_xalign(0.0)
        self.steamcmd_line2.set_wrap(True)
        self.steamcmd_line2.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.steamcmd_line2.add_css_class("steamcmd-log")
        line2_row.append(self.steamcmd_line2)
        log_box.append(line2_row)

        self.steamcmd_prog_bar = Gtk.ProgressBar()
        self.steamcmd_prog_bar.add_css_class("steamcmd-progress")
        self.steamcmd_prog_bar.set_fraction(0.0)
        self.steamcmd_prog_bar.set_show_text(False)
        self.steamcmd_prog_bar.set_pulse_step(0.05)
        self.steamcmd_prog_bar.set_visible(False)
        log_box.append(self.steamcmd_prog_bar)
        self.steamcmd_auth_box.append(log_box)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.steamcmd_cancel_btn = Gtk.Button(label="Cancel")
        self.steamcmd_cancel_btn.connect(
            "clicked",
            lambda *_: self.win._steamcmd_auth_cancel(),
        )
        buttons.append(self.steamcmd_cancel_btn)
        self.steamcmd_auth_box.append(buttons)
        overlay.add_overlay(self.steamcmd_auth_box)

        self.win.steamcmd_auth_scrim = self.steamcmd_auth_scrim
        self.win.steamcmd_auth_box = self.steamcmd_auth_box
        self.win.steamcmd_task_heading = self.steamcmd_task_heading
        self.win.steamcmd_line1 = self.steamcmd_line1
        self.win.steamcmd_spinner = self.steamcmd_spinner
        self.win.steamcmd_line2 = self.steamcmd_line2
        self.win.steamcmd_prog_bar = self.steamcmd_prog_bar
        self.win.steamcmd_cancel_btn = self.steamcmd_cancel_btn

    def _hide_steamcmd_auth_overlay(self):
        try:
            self.steamcmd_spinner.set_spinning(False)
            self.steamcmd_spinner.set_visible(False)
        except Exception:
            pass
        try:
            self.steamcmd_prog_bar.set_fraction(0.0)
            self.steamcmd_prog_bar.set_show_text(False)
            self.steamcmd_prog_bar.set_text("")
            self.steamcmd_prog_bar.set_visible(False)
        except Exception:
            pass
        self.steamcmd_auth_box.set_visible(False)
        self.steamcmd_auth_scrim.set_visible(False)

    def _steamcmd_reset_state_for_new_run(self):
        try:
            self.steamcmd_spinner.set_spinning(False)
            self.steamcmd_spinner.set_visible(False)
            self.steamcmd_task_heading.set_label("")
            self.steamcmd_line1.set_label("")
            self.steamcmd_line2.set_label("")
        except Exception:
            pass
        self.win._steamcmd_install_in_progress = False
