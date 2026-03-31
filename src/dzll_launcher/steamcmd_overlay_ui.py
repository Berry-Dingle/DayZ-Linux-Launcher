#!/usr/bin/env python3
import os
import re
import threading
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango

from .ui_row import attach_pointer_cursor
from .settings import save_settings, autodetect_workshop_dir
from .live import is_valid_hhmm  # if needed elsewhere later; safe to leave for now


class SteamCMDOverlayUI:
    """
    Owns the SteamCMD auth/progress overlay UI and state.
    DZLLWindow delegates to this to avoid bloating window.py.
    """

    def __init__(self, win):
        # win is DZLLWindow; we call back into a few helper methods and settings
        self.win = win

        # ---- state moved from DZLLWindow ----
        self._steamcmd_auth_request = None
        self._steamcmd_auth_event = None
        self._steamcmd_auth_result = None

        self._steamcmd_l1 = ""
        self._steamcmd_l2 = ""
        self._steamcmd_heading = ""
        self._steamcmd_auth_wait_count = 0

        self._steamcmd_total_missing = 0
        self._steamcmd_done_missing = 0
        self._steamcmd_seen_mod_ids = set()
        self._steamcmd_started_missing = 0

        self._steamcmd_mod_sizes = {}
        self._steamcmd_active_mid = None
        self._steamcmd_total_sizes = {}
        self._steamcmd_progress_timer_id = 0
        self._steamcmd_last_progress_bytes = 0

        self._steamcmd_cancel_event = threading.Event()
        self._steamcmd_install_in_progress = False

        # ---- widgets (created in build()) ----
        self.steamcmd_auth_scrim = None
        self.steamcmd_auth_box = None
        self.steamcmd_task_heading = None
        self._steamcmd_form_widgets = []

        self.steamcmd_user_entry = None
        self.steamcmd_pass_entry = None
        self.steamcmd_show_pass_cb = None
        self.steamcmd_spinner = None
        self.steamcmd_line1 = None
        self.steamcmd_line2 = None
        self.steamcmd_cancel_btn = None
        self.steamcmd_login_btn = None

    def build(self, overlay: Gtk.Overlay):
        # ----------------------------
        # SteamCMD AUTH OVERLAY (your 3-line layout)
        # ----------------------------
        self.steamcmd_auth_scrim = Gtk.Box()
        self.steamcmd_auth_scrim.set_hexpand(True)
        self.steamcmd_auth_scrim.set_vexpand(True)
        self.steamcmd_auth_scrim.set_visible(False)
        self.steamcmd_auth_scrim.set_can_target(True)
        self.steamcmd_auth_scrim.add_css_class("settings-scrim")
        overlay.add_overlay(self.steamcmd_auth_scrim)

        self.steamcmd_auth_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.steamcmd_auth_box.set_halign(Gtk.Align.CENTER)
        self.steamcmd_auth_box.set_valign(Gtk.Align.CENTER)
        self.steamcmd_auth_box.set_visible(False)
        self.steamcmd_auth_box.set_can_target(True)

        # OUTER padding (margin around the card area)
        self.steamcmd_auth_box.set_margin_start(40)
        self.steamcmd_auth_box.set_margin_end(40)
        self.steamcmd_auth_box.set_margin_top(40)
        self.steamcmd_auth_box.set_margin_bottom(40)

        self.steamcmd_auth_box.set_size_request(520, -1)
        self.steamcmd_auth_box.add_css_class("steamcmd-auth-card")

        # TOP heading (dynamic)
        self.steamcmd_task_heading = Gtk.Label(label="SteamCMD")
        self.steamcmd_task_heading.set_xalign(0.0)
        self.steamcmd_task_heading.add_css_class("steamcmd-heading")
        self.steamcmd_auth_box.append(self.steamcmd_task_heading)

        # HR under TOP
        steamcmd_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        steamcmd_sep.add_css_class("steamcmd-hr")
        self.steamcmd_auth_box.append(steamcmd_sep)

        # Group login-form widgets so we can hide them while busy
        self._steamcmd_form_widgets = []

        auth_hint = Gtk.Label(
            label="Enter your Steam login to download required workshop mods.\nPassword is not saved."
        )
        auth_hint.set_xalign(0.0)
        auth_hint.set_wrap(True)
        auth_hint.add_css_class("dim-label")
        self.steamcmd_auth_box.append(auth_hint)
        self._steamcmd_form_widgets.append(auth_hint)

        user_lbl = Gtk.Label(label="Steam ID")
        user_lbl.set_xalign(0.0)
        self.steamcmd_auth_box.append(user_lbl)
        self._steamcmd_form_widgets.append(user_lbl)

        self.steamcmd_user_entry = Gtk.Entry()
        self.steamcmd_user_entry.set_hexpand(True)
        self.steamcmd_user_entry.set_placeholder_text("Steam username")
        self.steamcmd_auth_box.append(self.steamcmd_user_entry)
        self._steamcmd_form_widgets.append(self.steamcmd_user_entry)

        pass_lbl = Gtk.Label(label="Password")
        pass_lbl.set_xalign(0.0)
        self.steamcmd_auth_box.append(pass_lbl)
        self._steamcmd_form_widgets.append(pass_lbl)

        self.steamcmd_pass_entry = Gtk.Entry()
        self.steamcmd_pass_entry.set_hexpand(True)
        self.steamcmd_pass_entry.set_visibility(False)
        self.steamcmd_pass_entry.set_invisible_char("•")
        self.steamcmd_pass_entry.set_placeholder_text("Password (not saved)")
        self.steamcmd_pass_entry.connect("activate", lambda *_: self.win._steamcmd_auth_submit())
        self.steamcmd_auth_box.append(self.steamcmd_pass_entry)
        self._steamcmd_form_widgets.append(self.steamcmd_pass_entry)

        # Show password toggle
        self.steamcmd_show_pass_cb = Gtk.CheckButton(label="Show password")
        self.steamcmd_show_pass_cb.set_halign(Gtk.Align.START)
        self.steamcmd_show_pass_cb.connect("toggled", self.win._on_steamcmd_show_password_toggled)
        self.steamcmd_auth_box.append(self.steamcmd_show_pass_cb)
        self._steamcmd_form_widgets.append(self.steamcmd_show_pass_cb)

        # ----------- LN1 + LN2 row (LN2 has spinner) -----------
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
        try:
            self.steamcmd_spinner.set_spinning(False)
        except Exception:
            pass
        line2_row.append(self.steamcmd_spinner)

        self.steamcmd_line2 = Gtk.Label(label="")
        self.steamcmd_line2.set_xalign(0.0)
        self.steamcmd_line2.set_wrap(True)
        self.steamcmd_line2.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.steamcmd_line2.add_css_class("steamcmd-log")
        line2_row.append(self.steamcmd_line2)

        log_box.append(line2_row)

        # ----------- Progress bar -----------
        self.steamcmd_prog_bar = Gtk.ProgressBar()
        self.steamcmd_prog_bar.add_css_class("steamcmd-progress")
        self.steamcmd_prog_bar.set_fraction(0.0)
        self.steamcmd_prog_bar.set_show_text(False)
        self.steamcmd_prog_bar.set_pulse_step(0.05)
        self.steamcmd_prog_bar.set_visible(False)
        log_box.append(self.steamcmd_prog_bar)

        self.steamcmd_auth_box.append(log_box)

        # Buttons row (Cancel always visible; Login hidden while busy)
        auth_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        auth_btns.set_halign(Gtk.Align.END)

        self.steamcmd_cancel_btn = Gtk.Button(label="Cancel")
        self.steamcmd_cancel_btn.connect("clicked", lambda *_: self.win._steamcmd_auth_cancel())
        auth_btns.append(self.steamcmd_cancel_btn)

        self.steamcmd_login_btn = Gtk.Button(label="Login")
        self.steamcmd_login_btn.add_css_class("suggested-action")
        self.steamcmd_login_btn.connect("clicked", lambda *_: self.win._steamcmd_auth_submit())
        auth_btns.append(self.steamcmd_login_btn)

        self.steamcmd_auth_box.append(auth_btns)
        overlay.add_overlay(self.steamcmd_auth_box)

        # Mirror refs back onto window so existing methods keep working unchanged
        self.win.steamcmd_auth_scrim = self.steamcmd_auth_scrim
        self.win.steamcmd_auth_box = self.steamcmd_auth_box
        self.win.steamcmd_task_heading = self.steamcmd_task_heading
        self.win._steamcmd_form_widgets = self._steamcmd_form_widgets
        self.win.steamcmd_user_entry = self.steamcmd_user_entry
        self.win.steamcmd_pass_entry = self.steamcmd_pass_entry
        self.win.steamcmd_show_pass_cb = self.steamcmd_show_pass_cb
        self.win.steamcmd_line1 = self.steamcmd_line1
        self.win.steamcmd_spinner = self.steamcmd_spinner
        self.win.steamcmd_line2 = self.steamcmd_line2
        self.win.steamcmd_prog_bar = self.steamcmd_prog_bar
        self.win.steamcmd_cancel_btn = self.steamcmd_cancel_btn
        self.win.steamcmd_login_btn = self.steamcmd_login_btn

    # ---- moved methods (we paste implementations next step) ----
    def _on_steamcmd_show_password_toggled(self, btn):
        try:
            show = bool(btn.get_active())
            self.steamcmd_pass_entry.set_visibility(show)
        except Exception:
            pass

    def _steamcmd_overlay_render(self, heading: str, line1: str, line2: str, spinning: bool):
        """
        [TOP] heading
        [LN1] previous step/info
        [LN2] [spinner] literal current activity
        """
        try:
            self.steamcmd_task_heading.set_text(heading or "")
        except Exception:
            pass

        try:
            self.steamcmd_line1.set_text(line1 or "")
        except Exception:
            pass

        try:
            self.steamcmd_line2.set_text(line2 or "")
        except Exception:
            pass

        try:
            self.steamcmd_spinner.set_spinning(bool(spinning))
            self.steamcmd_spinner.set_visible(bool(spinning))
        except Exception:
            pass

    def _steamcmd_set_state(self, heading: str, line1: str, line2: str, spinning: bool):
        # Keep authoritative state on the window (other code reads it there)
        self.win._steamcmd_heading = heading or getattr(self.win, "_steamcmd_heading", "")
        self.win._steamcmd_l1 = line1 if line1 is not None else getattr(self.win, "_steamcmd_l1", "")
        self.win._steamcmd_l2 = line2 if line2 is not None else getattr(self.win, "_steamcmd_l2", "")

        self._steamcmd_overlay_render(self.win._steamcmd_heading, self.win._steamcmd_l1, self.win._steamcmd_l2,
                                      spinning)

        # Busy => hide login form to become a pure 3-line log view
        try:
            busy = bool(spinning)
            if busy:
                self.win._steamcmd_start_progress_timer()
            else:
                self.win._steamcmd_stop_progress_timer()
                try:
                    self.steamcmd_prog_bar.set_visible(False)
                except Exception:
                    pass

            for w in getattr(self.win, "_steamcmd_form_widgets", []):
                try:
                    w.set_visible(not busy)
                except Exception:
                    pass

            # Cancel always visible; Login only while idle
            try:
                self.steamcmd_cancel_btn.set_visible(True)
            except Exception:
                pass
            try:
                self.steamcmd_login_btn.set_visible(not busy)
            except Exception:
                pass

            # Prevent edits while busy
            try:
                self.steamcmd_user_entry.set_sensitive(not busy)
                self.steamcmd_pass_entry.set_sensitive(not busy)
            except Exception:
                pass
        except Exception:
            pass

    def _steamcmd_install_line_from_worker(self, line: str):
        try:
            GLib.idle_add(self._steamcmd_line_to_overlay, str(line or ""))
        except Exception:
            pass

    def _show_steamcmd_auth_overlay(self, username_prefill: str = "", status: str = ""):
        self.win._steamcmd_auth_wait_count = 0

        try:
            self.steamcmd_user_entry.set_text((username_prefill or "").strip())
        except Exception:
            pass
        try:
            self.steamcmd_pass_entry.set_text("")
        except Exception:
            pass

        self.win._steamcmd_heading = "SteamCMD Login"
        self.win._steamcmd_l1 = status or ""
        self.win._steamcmd_l2 = ""
        self.win._steamcmd_overlay_render(self.win._steamcmd_heading, self.win._steamcmd_l1, self.win._steamcmd_l2, False)

        self.steamcmd_auth_scrim.set_visible(True)
        self.steamcmd_auth_box.set_visible(True)

        try:
            if (username_prefill or "").strip():
                self.steamcmd_pass_entry.grab_focus()
            else:
                self.steamcmd_user_entry.grab_focus()
        except Exception:
            pass

    def _hide_steamcmd_auth_overlay(self):
        try:
            self.steamcmd_pass_entry.set_text("")
        except Exception:
            pass
        self.steamcmd_auth_box.set_visible(False)
        self.steamcmd_auth_scrim.set_visible(False)

    def _steamcmd_auth_submit(self):
        req = getattr(self.win, "_steamcmd_auth_request", None)
        ev = getattr(self.win, "_steamcmd_auth_event", None)
        if req is None or ev is None:
            self._hide_steamcmd_auth_overlay()
            return

        username = ""
        password = ""
        try:
            username = (self.steamcmd_user_entry.get_text() or "").strip()
        except Exception:
            pass
        try:
            password = self.steamcmd_pass_entry.get_text() or ""
        except Exception:
            pass

        if not username:
            self.win._steamcmd_overlay_render("SteamCMD Login", "Steam ID is required.", "", False)
            return

        if not password:
            self.win._steamcmd_overlay_render("SteamCMD Login", "Password is required.", "", False)
            return

        # Case 1 start
        self.win._steamcmd_set_state(
            heading="Logging Into SteamCMD…",
            line1="Logging in using username & password…",
            line2=f"Logging in user '{username}'",
            spinning=True,
        )

        self.win._steamcmd_auth_result = {"ok": True, "username": username, "password": password}

        # Save username if changed
        try:
            old_user = str(self.win.settings.get("steamcmd_username") or "").strip()
            if username != old_user:
                self.win.settings["steamcmd_username"] = username
                save_settings(self.win.settings)

                w = self.win._settings_widgets.get("steamcmd_username")
                if isinstance(w, Gtk.Entry):
                    self.win._settings_update_guard = True
                    try:
                        w.set_text(username)
                    finally:
                        self.win._settings_update_guard = False
        except Exception:
            pass

        # Keep overlay open; it becomes the progress UI
        self.win._steamcmd_auth_request = None

        try:
            ev.set()
        finally:
            # Clear after releasing the waiter to avoid any race
            self.win._steamcmd_auth_event = None

    def _steamcmd_auth_cancel(self):
        """
        Two modes:
        - If SteamCMD is running: cancel it gracefully and keep overlay visible until it exits.
        - If not running: cancel login overlay immediately.

        Critical: always release any pending auth wait to avoid deadlocks.
        """
        try:
            GLib.idle_add(self.win._steamcmd_reset_state_for_new_run)
        except Exception:
            pass

        ev = getattr(self.win, "_steamcmd_auth_event", None)
        req = getattr(self.win, "_steamcmd_auth_request", None)
        auth_pending = bool(req) and isinstance(req, dict) and req.get("pending")

        # If install running => set cancel event and show cancelling UI; do NOT hide overlay here.
        try:
            if bool(getattr(self.win, "_steamcmd_install_in_progress", False)):
                try:
                    self.win._steamcmd_cancel_event.set()
                except Exception:
                    pass

                self.win._steamcmd_set_state(
                    heading="Cancelling SteamCMD…",
                    line1=getattr(self.win, "_steamcmd_l1", "") or "",
                    line2="Stopping SteamCMD…",
                    spinning=True,
                )

                # ALSO release a pending auth wait if one exists (stale state protection)
                if auth_pending and isinstance(ev, threading.Event):
                    self.win._steamcmd_auth_result = {"ok": False, "cancelled": True}
                    self.win._steamcmd_auth_request = None
                    self.win._steamcmd_auth_event = None
                    try:
                        ev.set()
                    except Exception:
                        pass
                return
        except Exception:
            pass

        # Login cancel (no install in progress)
        self.win._steamcmd_auth_result = {"ok": False, "cancelled": True}

        try:
            self.steamcmd_spinner.set_spinning(False)
        except Exception:
            pass

        self._hide_steamcmd_auth_overlay()
        self.win._steamcmd_auth_request = None
        self.win._steamcmd_auth_event = None

        if isinstance(ev, threading.Event):
            try:
                ev.set()
            except Exception:
                pass

    def _request_steamcmd_credentials_blocking(self, username_prefill: str = "", status: str = ""):
        # ===== DEBUGGING ===== #
        print("[STEAMCMD AUTH] blocking credentials request starting")
        # ===================== #
        """
        Show SteamCMD auth overlay and block (from a worker thread) until user submits/cancels.

        Robustness:
        - Cancels any prior pending auth wait (defensive).
        - Forces overlay out of "busy" mode on the GTK thread before showing.
        - Handshake event confirms GTK callback executed.
        - Timeout prevents silent deadlock.
        """
        # Defensive: release any old pending waiter
        try:
            old_ev = getattr(self.win, "_steamcmd_auth_event", None)
            if isinstance(old_ev, threading.Event) and not old_ev.is_set():
                self.win._steamcmd_auth_result = {"ok": False, "cancelled": True}
                old_ev.set()
        except Exception:
            pass

        ev = threading.Event()
        shown = threading.Event()

        self.win._steamcmd_auth_event = ev
        self.win._steamcmd_auth_request = {"pending": True}
        self.win._steamcmd_auth_result = None

        def _ui_show():
            # CRITICAL: full reset so login widgets reappear after Cancel/previous busy state
            try:
                self.win._steamcmd_reset_state_for_new_run()
            except Exception:
                pass

            # Extra belt: ensure spinner is off
            try:
                self.steamcmd_spinner.set_spinning(False)
            except Exception:
                pass

            try:
                self.win._set_updating(False)
            except Exception:
                pass

            try:
                self.win._show_steamcmd_auth_overlay(username_prefill, status)
            except Exception:
                # If UI show fails, unblock worker as cancelled
                try:
                    self.win._steamcmd_auth_result = {"ok": False, "cancelled": True}
                except Exception:
                    pass
                try:
                    ev.set()
                except Exception:
                    pass

            try:
                shown.set()
            except Exception:
                pass

            return False

        GLib.idle_add(_ui_show)

        # Wait briefly for GTK callback to run (prevents deadlock if GTK loop is blocked)
        if not shown.wait(timeout=2.0):
            self.win._steamcmd_auth_result = {"ok": False, "cancelled": True, "ui_timeout": True}
            return self.win._steamcmd_auth_result

        # Wait for user submit/cancel
        if not ev.wait(timeout=300.0):
            self.win._steamcmd_auth_result = {"ok": False, "cancelled": True, "timeout": True}
            try:
                GLib.idle_add(self.win._hide_steamcmd_auth_overlay)
            except Exception:
                pass
            return self.win._steamcmd_auth_result

        result = self.win._steamcmd_auth_result or {"ok": False, "cancelled": True}
        self.win._steamcmd_auth_result = None
        return result

    def _steamcmd_mark_started(self, mod_id: str) -> tuple[int, int]:
        """
        Increment started counter when we FIRST see a mod_id in output.
        This drives the X/Y counter, per your requirement.
        """
        mod_id = str(mod_id or "").strip()
        if not mod_id:
            try:
                return (
                    int(getattr(self.win, "_steamcmd_started_missing", 0) or 0),
                    int(getattr(self.win, "_steamcmd_total_missing", 0) or 0),
                )
            except Exception:
                return (0, 0)

        seen = getattr(self.win, "_steamcmd_seen_mod_ids", None)
        if seen is None:
            self.win._steamcmd_seen_mod_ids = set()
            seen = self.win._steamcmd_seen_mod_ids

        if mod_id not in seen:
            seen.add(mod_id)
            try:
                self.win._steamcmd_started_missing = int(getattr(self.win, "_steamcmd_started_missing", 0) or 0) + 1
            except Exception:
                self.win._steamcmd_started_missing = 1

        try:
            started = int(getattr(self.win, "_steamcmd_started_missing", 0) or 0)
        except Exception:
            started = 0
        try:
            total = int(getattr(self.win, "_steamcmd_total_missing", 0) or 0)
        except Exception:
            total = 0
        return started, total

    def _steamcmd_start_progress_timer(self):
        try:
            if int(getattr(self.win, "_steamcmd_progress_timer_id", 0) or 0) != 0:
                return
            # tick every 500ms
            self.win._steamcmd_progress_timer_id = GLib.timeout_add(500, self._steamcmd_progress_tick)
        except Exception:
            self.win._steamcmd_progress_timer_id = 0

    def _steamcmd_stop_progress_timer(self):
        try:
            tid = int(getattr(self.win, "_steamcmd_progress_timer_id", 0) or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
            self.win._steamcmd_progress_timer_id = 0
        except Exception:
            self.win._steamcmd_progress_timer_id = 0

    def _steamcmd_progress_tick(self):
        """
        GTK thread. Updates progress bar based on bytes on disk for active mod.
        If total size unknown, pulses bar.
        """
        try:
            # Stop/cleanup if we're not installing
            if not bool(getattr(self.win, "_steamcmd_install_in_progress", False)):
                try:
                    self.win.steamcmd_prog_bar.set_visible(False)
                except Exception:
                    pass
                self._steamcmd_stop_progress_timer()
                return False

            mid = getattr(self.win, "_steamcmd_active_mid", None)
            if not mid:
                # No active mod yet -> indeterminate pulse
                try:
                    self.win.steamcmd_prog_bar.set_visible(True)
                    self.win.steamcmd_prog_bar.set_show_text(False)
                    self.win.steamcmd_prog_bar.pulse()
                except Exception:
                    pass
                return True

            mid_i = int(mid)

            # read bytes on disk (downloads + content)
            workshop_dir = str(self.win.settings.get("workshop_dir") or "").strip()
            if not workshop_dir:
                workshop_dir = autodetect_workshop_dir() or ""
            workshop_dir = os.path.expanduser(workshop_dir)

            dl_dir = os.path.join(workshop_dir, "downloads", "221100", str(mid_i))
            ct_dir = os.path.join(workshop_dir, "content", "221100", str(mid_i))

            def dir_bytes(p: str) -> int:
                total = 0
                try:
                    for root, _dirs, files in os.walk(p):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                total += os.path.getsize(fp)
                            except Exception:
                                pass
                except Exception:
                    return 0
                return int(total)

            have = dir_bytes(dl_dir) + dir_bytes(ct_dir)

            # clamp monotonic (avoid "going backwards" during file moves)
            last = int(getattr(self.win, "_steamcmd_last_progress_bytes", 0) or 0)
            if have < last:
                have = last
            self.win._steamcmd_last_progress_bytes = have

            total_map = getattr(self.win, "_steamcmd_total_sizes", {}) or {}
            total = int(total_map.get(mid_i) or 0)

            if total > 0:
                frac = max(0.0, min(1.0, float(have) / float(total)))
                try:
                    self.win.steamcmd_prog_bar.set_visible(True)
                    self.win.steamcmd_prog_bar.set_show_text(True)
                    self.win.steamcmd_prog_bar.set_fraction(frac)
                    self.win.steamcmd_prog_bar.set_text(f"{int(frac * 100)}%")
                except Exception:
                    pass
            else:
                # Unknown total -> pulse
                try:
                    self.win.steamcmd_prog_bar.set_visible(True)
                    self.win.steamcmd_prog_bar.set_show_text(False)
                    self.win.steamcmd_prog_bar.pulse()
                except Exception:
                    pass

            return True
        except Exception:
            return True

    def _steamcmd_line_to_overlay(self, line: str):
        s = str(line or "")
        if not s:
            return False

        try:
            s = self.win._ANSI_RE.sub("", s)
        except Exception:
            pass

        s = s.strip()
        if not s:
            return False

        low = s.lower()

        # ---- Case 2 (Steam Guard authorisation) ----
        if "this account is protected by a steam guard mobile authenticator" in low:
            self.win._steamcmd_auth_wait_count = 0
            self.win._steamcmd_set_state(
                heading="Waiting for Steam Authorisation…",
                line1="This account is protected by a Steam Guard mobile authenticator",
                line2="Please confirm the login in the Steam Mobile app on your phone",
                spinning=True,
            )
            return False

        if "please confirm the login in the steam mobile app" in low:
            self.win._steamcmd_auth_wait_count = 0
            self.win._steamcmd_set_state(
                heading="Waiting for Steam Authorisation…",
                line1="This account is protected by a Steam Guard mobile authenticator",
                line2="Please confirm the login in the Steam Mobile app on your phone",
                spinning=True,
            )
            return False

        if "waiting for confirmation" in low:
            self.win._steamcmd_auth_wait_count += 1
            dots = "." * ((self.win._steamcmd_auth_wait_count % 3) + 1)
            self.win._steamcmd_set_state(
                heading="Waiting for Steam Authorisation…",
                line1="This account is protected by a Steam Guard mobile authenticator",
                line2=f"Waiting for confirmation{dots}",
                spinning=True,
            )
            return False

        if ("timed out waiting for confirmation" in low) or ("wait for confirmation timed out" in low) or (
                "error (timeout)" in low):
            self.win._steamcmd_set_state(
                heading="Waiting for Steam Authorisation…",
                line1="Steam Guard confirmation timed out.",
                line2="Please try again.",
                spinning=False,
            )
            return False

        # ---- Login phase updates (Case 1) ----
        if "logging in using username/password" in low:
            self.win._steamcmd_set_state(
                heading="Logging Into SteamCMD…",
                line1="Logging in using username & password…",
                line2=self.win._steamcmd_l2 or "",
                spinning=True,
            )
            return False

        if "logging in user" in low and "steam public" in low:
            self.win._steamcmd_set_state(
                heading="Logging Into SteamCMD…",
                line1="Logging in using username & password…",
                line2=s,
                spinning=True,
            )
            return False

        if "waiting for client config" in low:
            self.win._steamcmd_set_state(
                heading="Logging Into SteamCMD…",
                line1="Login approved. Continuing…",
                line2="Waiting for client config…",
                spinning=True,
            )
            return False

        if "waiting for user info" in low:
            self.win._steamcmd_set_state(
                heading="Logging Into SteamCMD…",
                line1="Login approved. Continuing…",
                line2="Waiting for user info…",
                spinning=True,
            )
            return False

        # ---- Case 3 (Downloading mods) ----
        processed_success = False

        # Success line: "Success. Downloaded item <id> ... (123 bytes)"
        if ("success" in low) and ("downloaded item" in low):
            done_id = ""
            bytes_str = ""
            try:
                m_id = re.search(r"\bdownloaded item\s+(\d+)\b", s, flags=re.IGNORECASE)
                if m_id:
                    done_id = m_id.group(1)
                m_b = re.search(r"\((\d+)\s+bytes\)", s, flags=re.IGNORECASE)
                if m_b:
                    bytes_str = m_b.group(1)
            except Exception:
                done_id = ""
                bytes_str = ""

            size_str = ""
            if done_id and bytes_str and bytes_str.isdigit():
                size_str = self._format_bytes_human(int(bytes_str))
                try:
                    self.win._steamcmd_mod_sizes[str(done_id)] = size_str
                except Exception:
                    pass

            try:
                self.win._steamcmd_done_missing = int(getattr(self.win, "_steamcmd_done_missing", 0) or 0) + 1
            except Exception:
                pass

            size_suffix = f" ({size_str})" if size_str else ""
            if done_id:
                l1 = f"Done: Mod {done_id} Successfully Downloaded{size_suffix}"
            else:
                l1 = "Done: Mod Successfully Downloaded"

            self.win._steamcmd_set_state(
                heading="Downloading Required Mods, This Might Take a While…",
                line1=l1,
                line2=self.win._steamcmd_l2 or "",
                spinning=True,
            )
            processed_success = True

            # If the active download just finished, clear active mid (lets bar reset/pulse until next)
            try:
                if done_id and done_id.isdigit() and getattr(self.win, "_steamcmd_active_mid", None) == int(done_id):
                    self.win._steamcmd_active_mid = None
            except Exception:
                pass

            # continue parsing (do not return yet; SteamCMD may print next command on same tick)

        # Script line: "workshop_download_item 221100 <id>"
        if "workshop_download_item" in low:
            mod_id = "?"
            try:
                toks = s.replace(")", " ").replace("(", " ").replace("\t", " ").split()
                for i, t in enumerate(toks):
                    if t.lower() == "workshop_download_item":
                        if i + 2 < len(toks):
                            cand = toks[i + 2].strip().strip(",;")
                            if cand.isdigit():
                                mod_id = cand
                            else:
                                cand2 = "".join(ch for ch in cand if ch.isdigit())
                                if cand2:
                                    mod_id = cand2
                        break
            except Exception:
                mod_id = "?"

            # Mark this as the active download for bar
            try:
                if mod_id.isdigit():
                    self.win._steamcmd_active_mid = int(mod_id)
                    self.win._steamcmd_last_progress_bytes = 0
            except Exception:
                pass

            # Size suffix from Steam API map (optional)
            size_suffix = ""
            try:
                if mod_id.isdigit():
                    tb = int((getattr(self.win, "_steamcmd_total_sizes", {}) or {}).get(int(mod_id)) or 0)
                    if tb > 0:
                        size_suffix = f" - {self._format_bytes_human(tb)}"
            except Exception:
                size_suffix = ""

            started, total = self._steamcmd_mark_started(mod_id)
            frac = f"{started}/{total}" if total > 0 else ""
            suffix = f" ({frac})" if frac else ""

            self.win._steamcmd_set_state(
                heading="Downloading Required Mods, This Might Take a While…",
                line1=self.win._steamcmd_l1 or "",
                line2=f"Downloading: Mod {mod_id}{size_suffix}{suffix}",
                spinning=True,
            )
            return False

        # Output line: "Downloading item <id> ..."
        if low.startswith("downloading item "):
            parts = s.split()
            mod_id = parts[2] if len(parts) >= 3 else "?"

            # Mark this as the active download for bar
            try:
                if str(mod_id).isdigit():
                    self.win._steamcmd_active_mid = int(mod_id)
                    self.win._steamcmd_last_progress_bytes = 0
            except Exception:
                pass

            # Size suffix from Steam API map (optional)
            size_suffix = ""
            try:
                if str(mod_id).isdigit():
                    tb = int((getattr(self.win, "_steamcmd_total_sizes", {}) or {}).get(int(mod_id)) or 0)
                    if tb > 0:
                        size_suffix = f" - {self._format_bytes_human(tb)}"
            except Exception:
                size_suffix = ""

            started, total = self._steamcmd_mark_started(mod_id)
            frac = f"{started}/{total}" if total > 0 else ""
            suffix = f" - {frac}..." if frac else "..."

            self.win._steamcmd_set_state(
                heading="Downloading Required Mods, This Might Take a While…",
                line1=self.win._steamcmd_l1 or "",
                line2=f"Downloading: Mod {mod_id}{size_suffix}{suffix}",
                spinning=True,
            )
            return False

        if "error! timeout downloading item" in low:
            self.win._steamcmd_set_state(
                heading="Downloading Required Mods…",
                line1="SteamCMD download timed out.",
                line2="Check disk space / connection and try again.",
                spinning=False,
            )
            return False

        if "error! download item" in low and "failed" in low:
            self.win._steamcmd_set_state(
                heading="Downloading Required Mods…",
                line1="SteamCMD failed to download a mod.",
                line2="Check disk space / connection and try again.",
                spinning=False,
            )
            return False

        if "unloading steam api...ok" in low:
            self.win._steamcmd_set_state(
                heading="SteamCMD…",
                line1="Finishing SteamCMD step…",
                line2="",
                spinning=True,
            )
            return False

        if processed_success:
            return False

        return False

    def _steamcmd_refresh_active_download_line2(self):
        """
        Re-render L2 for the currently active mod so the total size appears
        even if the SteamCMD 'Downloading item ...' line arrived before sizes did.
        """
        try:
            mid = getattr(self.win, "_steamcmd_active_mid", None)
            if not mid:
                return False
            mid_i = int(mid)

            tb = int((getattr(self.win, "_steamcmd_total_sizes", {}) or {}).get(mid_i) or 0)
            if tb <= 0:
                return False  # nothing to show yet

            started = int(getattr(self.win, "_steamcmd_started_missing", 0) or 0)
            total = int(getattr(self.win, "_steamcmd_total_missing", 0) or 0)
            frac = f"{started}/{total}" if total > 0 else ""
            suffix = f" ({frac})" if frac else ""  # your requested format

            size_suffix = f" - {self._format_bytes_human(tb)}"

            # EXACT L2 format you asked for
            self.steamcmd_line2.set_text(f"Downloading mod: {mid_i}{size_suffix}{suffix}")
            return False
        except Exception:
            return False

    def _format_bytes_human(self, n: int) -> str:
        try:
            n = int(n)
        except Exception:
            return ""
        if n <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        f = float(n)
        for u in units:
            if f < 1000.0:
                if u == "B":
                    return f"{int(f)} {u}"
                return f"{f:.2f} {u}"
            f /= 1000.0
        return f"{f:.2f} EB"

    def _steamcmd_reset_state_for_new_run(self):
        # Clear “busy” UI so the login form is visible again
        try:
            self.win.steamcmd_spinner.set_spinning(False)
        except Exception:
            pass

        # If you have a helper that toggles form visibility based on spinning, call it:
        try:
            self.win._set_steamcmd_busy(False)  # or whatever your function is named
        except Exception:
            # Fallback: make sure form widgets are visible/sensitive
            try:
                for w in getattr(self.win, "_steamcmd_form_widgets", []):
                    w.set_visible(True)
                if hasattr(self.win, "steamcmd_login_btn"):
                    self.win.steamcmd_login_btn.set_visible(True)
                if hasattr(self.win, "steamcmd_user_entry"):
                    self.win.steamcmd_user_entry.set_sensitive(True)
                if hasattr(self.win, "steamcmd_pass_entry"):
                    self.win.steamcmd_pass_entry.set_sensitive(True)
            except Exception:
                pass

        # Reset overlay strings (optional but nice)
        self.win._steamcmd_heading = ""
        self.win._steamcmd_l1 = ""
        self.win._steamcmd_l2 = ""
        try:
            self.win.steamcmd_task_heading.set_label("SteamCMD")
            self.win.steamcmd_line1.set_label("")
            self.win.steamcmd_line2.set_label("")
        except Exception:
            pass

        # IMPORTANT: new cancel event every run (never reuse old one)
        import threading
        self.win._steamcmd_cancel_event = threading.Event()

        # IMPORTANT: reset “in progress” gate
        self.win._steamcmd_install_in_progress = False