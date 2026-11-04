#!/usr/bin/env python3
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from .join_popup_presentation import JoinPopupPhase
from .preparation_presentation import PreparationProgressMode


class JoinPreparationOverlayUI:
    """Build and render the shared Join preparation overlay used by Steam Client UGC.

    Rendering is presentation only. Join attempt state, popup-phase requests,
    cancellation eligibility and the worker-to-GTK progress marshal stay in the
    window, which calls these methods on the GTK main thread.
    """

    def __init__(
        self,
        win,
        *,
        pulse_active: Callable[[], bool],
        active_download: Callable[[], Any],
    ):
        self.win = win
        self._pulse_active = pulse_active
        self._active_download = active_download
        self.progress_timer_id = 0
        self.percent_label = None
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
        # Reuse the application's standard action width and accent/disabled
        # action-button styling.  Sensitivity changes then remain visually
        # obvious without changing the button's footprint.
        self.join_preparation_cancel_button.add_css_class("warning-btn")
        self.join_preparation_cancel_button.add_css_class("suggested-action")
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
        self.stop_progress_timer()
        self.set_layout_active(False)
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

    # ------------------------------------------------------------- rendering

    @staticmethod
    def join_progress_phase(status: str = ""):
        """Map a Join progress status to its popup phase, canonical text and immediacy."""
        raw_text = str(status or "").strip()
        if raw_text.startswith("Waiting for Steam"):
            phase = JoinPopupPhase.WAITING_STEAM
            text = "Waiting for Steam…"
            immediate = True
        elif raw_text.startswith("All Mods Downloaded"):
            phase = JoinPopupPhase.LAUNCHING
            text = "All Mods Downloaded, Launching DayZ, Please Wait..."
            immediate = True
        elif raw_text.startswith("All Mods Ready"):
            phase = JoinPopupPhase.LAUNCHING
            text = "All Mods Ready, Launching DayZ, Please Wait..."
            immediate = True
        else:
            phase = JoinPopupPhase.CHECKING
            text = "Checking & Preparing Mods for Join..."
            immediate = True
        return phase, text, immediate

    def show_join_progress(self):
        try:
            self.join_preparation_cancel_button.set_label("Cancel")
            self.join_preparation_cancel_button.set_tooltip_text(
                "Cancel this Join preparation."
            )
            self.join_preparation_cancel_button.set_sensitive(True)
            self.join_preparation_cancel_button.set_visible(True)
            self.join_preparation_scrim.set_visible(True)
            self.join_preparation_card.set_visible(True)
        except Exception:
            pass

    def title(self) -> str:
        return "Checking/updating the required mods for this server"

    def show_preparing(self):
        try:
            self.join_preparation_cancel_button.set_visible(True)
            self.join_preparation_scrim.set_visible(True)
            self.join_preparation_card.set_visible(True)
        except Exception:
            pass
        try:
            self.set_layout_active(True)
            self.join_preparation_heading.set_text(self.title())
            self.join_preparation_detail_label.set_text("")
        except Exception:
            pass
        try:
            self.join_preparation_spinner.set_visible(True)
            self.join_preparation_spinner.set_spinning(True)
        except Exception:
            pass
        try:
            self.join_preparation_progress_bar.set_visible(True)
            self.join_preparation_progress_bar.set_show_text(False)
            self.join_preparation_progress_bar.set_fraction(0.0)
        except Exception:
            pass
        label = self.get_percent_label()
        if label is not None:
            try:
                label.set_text("0%")
                label.set_visible(True)
            except Exception:
                pass
        self.start_progress_timer()

    def get_percent_label(self):
        label = self.percent_label
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
            parent = self.join_preparation_progress_bar.get_parent()
            if parent is not None:
                parent.append(label)
            self.percent_label = label
            return label
        except Exception:
            self.percent_label = None
            return None

    def set_layout_active(self, active: bool):
        try:
            self.join_preparation_detail_label.set_visible(not active)
        except Exception:
            pass
        try:
            self.join_preparation_progress_bar.set_margin_top(10 if active else 0)
        except Exception:
            pass
        try:
            btn_parent = self.join_preparation_cancel_button.get_parent()
            if btn_parent is not None:
                btn_parent.set_margin_top(10 if active else 0)
        except Exception:
            pass
        label = self.get_percent_label() if active else self.percent_label
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

    def render_cancelling(self):
        try:
            self.set_layout_active(True)
            self.join_preparation_heading.set_text(self.title())
            self.join_preparation_detail_label.set_text("")
            self.join_preparation_status_label.set_text("Cancelling download and cleaning up...")
        except Exception:
            pass
        try:
            self.join_preparation_spinner.set_visible(True)
            self.join_preparation_spinner.set_spinning(True)
        except Exception:
            pass
        try:
            self.join_preparation_progress_bar.set_visible(True)
            self.join_preparation_progress_bar.set_show_text(False)
        except Exception:
            pass
        return False

    def render_status(self, text: str, *, error: bool = False):
        try:
            self.set_layout_active(True)
            self.join_preparation_heading.set_text(self.title())
            self.join_preparation_detail_label.set_text("")
            self.join_preparation_status_label.set_text(text)
        except Exception:
            pass
        try:
            self.join_preparation_spinner.set_visible(not bool(error))
            self.join_preparation_spinner.set_spinning(not bool(error))
        except Exception:
            pass
        try:
            self.join_preparation_progress_bar.set_visible(True)
            self.join_preparation_progress_bar.set_show_text(False)
            self.join_preparation_progress_bar.set_fraction(0.0)
            if not error:
                self.start_progress_timer()
            else:
                self.stop_progress_timer()
        except Exception:
            pass
        label = self.get_percent_label()
        if label is not None:
            try:
                label.set_text("0%")
                label.set_visible(True)
            except Exception:
                pass

    def render_active_text(self, text: str):
        """Update only the established active-transfer text, never its progress."""
        try:
            self.set_layout_active(True)
            self.join_preparation_heading.set_text(self.title())
            self.join_preparation_detail_label.set_text("")
            self.join_preparation_status_label.set_text(str(text or ""))
        except Exception:
            pass
        try:
            self.join_preparation_spinner.set_visible(True)
            self.join_preparation_spinner.set_spinning(True)
        except Exception:
            pass
        return False

    def render_progress(self, snapshot):
        """Render a reduced preparation snapshot's spinner, progress bar and percentage."""
        try:
            self.join_preparation_spinner.set_visible(True)
            self.join_preparation_spinner.set_spinning(True)
        except Exception:
            pass
        percent_label = self.get_percent_label()
        if snapshot.progress_mode is PreparationProgressMode.DETERMINATE:
            self.stop_progress_timer()
            fraction = float(snapshot.fraction or 0.0)
            try:
                self.join_preparation_progress_bar.set_visible(True)
                self.join_preparation_progress_bar.set_show_text(False)
                self.join_preparation_progress_bar.set_fraction(fraction)
            except Exception:
                pass
            if percent_label is not None:
                percent_label.set_text(f"{int(fraction * 100)}%")
                percent_label.set_visible(True)
        elif snapshot.progress_mode is PreparationProgressMode.INDETERMINATE:
            try:
                self.join_preparation_progress_bar.set_visible(True)
                self.join_preparation_progress_bar.set_show_text(False)
                self.join_preparation_progress_bar.set_fraction(0.0)
            except Exception:
                pass
            self.start_progress_timer()

    # ------------------------------------------------------------- indeterminate pulse

    def start_progress_timer(self):
        try:
            if int(self.progress_timer_id or 0):
                return
            self.progress_timer_id = GLib.timeout_add(500, self._progress_pulse)
        except Exception:
            self.progress_timer_id = 0

    def stop_progress_timer(self):
        try:
            tid = int(self.progress_timer_id or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self.progress_timer_id = 0
        except Exception:
            self.progress_timer_id = 0

    def _progress_pulse(self):
        try:
            if not self._pulse_active():
                self.progress_timer_id = 0
                return False
            active = self._active_download() or {}
            total_bytes = int(active.get("total_bytes") or 0) if isinstance(active, dict) else 0
            if total_bytes <= 0:
                try:
                    self.join_preparation_progress_bar.set_visible(True)
                    self.join_preparation_progress_bar.set_show_text(False)
                    self.join_preparation_progress_bar.pulse()
                except Exception:
                    pass
            return True
        except Exception:
            return True
