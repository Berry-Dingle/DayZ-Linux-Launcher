#!/usr/bin/env python3
# settings_ui.py
#
# Settings panel UI extracted from main.py with zero behavior changes.
# (Updated per requested ordering + new launch toggle + tooltips + dimming autodetected fields.)

import os
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango, GLib, Gio

from .config import IMAGES_DIR, APP_VERSION, RELEASES_URL
from .settings import save_settings, reset_settings, autodetect_steamcmd_path, autodetect_workshop_dir
from .storage import save_last_played
from .ui_row import hr, attach_pointer_cursor
from .mods_ui import ModsManagerOverlay

class SettingsUI:
    def __init__(self, window):
        self._win = window

    def open_mods_manager(self):
        try:
            ov = getattr(self, "_main_overlay", None)
            if ov is None:
                ov = getattr(self._win, "_main_overlay", None)
            if ov is None:
                print("[MODS UI] No main overlay found on settings host.")
                return
            if not hasattr(self, "_mods_mgr_overlay") or self._mods_mgr_overlay is None:
                self._mods_mgr_overlay = ModsManagerOverlay(self, ov)

            def _ui_open():
                try:
                    self._mods_mgr_overlay.refresh()
                except Exception:
                    pass
                try:
                    self._mods_mgr_overlay.show()
                except Exception:
                    pass
                return False

            GLib.idle_add(_ui_open)

        except Exception as e:
            print(f"[MODS UI] Failed to open mods manager: {e}")

    def build_panel(self) -> Gtk.Widget:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.set_size_request(480, -1)
        panel.set_vexpand(True)
        panel.set_hexpand(False)
        panel.add_css_class("settings-panel")
        panel.set_margin_top(10)
        panel.set_margin_bottom(10)
        panel.set_margin_end(10)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_top(10)
        top.set_margin_bottom(8)

        title = Gtk.Label(label="Settings")
        title.add_css_class("settings-section-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        top.append(title)

        close_btn = Gtk.Button()
        close_btn.set_can_focus(False)
        close_btn.add_css_class("flat")
        close_btn.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        close_btn.set_tooltip_text("Close")
        close_btn.connect("clicked", lambda *_: self._win._close_settings_panel())
        attach_pointer_cursor(close_btn)
        top.append(close_btn)

        panel.append(top)
        panel.append(hr())

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)

        self._win.settings_stack = Gtk.Stack()
        self._win.settings_stack.set_vexpand(True)
        self._win.settings_stack.set_hexpand(True)
        self._win.settings_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._win.settings_stack.set_transition_duration(160)

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self._win.settings_stack)
        sidebar.set_vexpand(True)
        sidebar.set_hexpand(False)
        sidebar.set_size_request(160, -1)
        sidebar.set_margin_top(10)
        sidebar.set_margin_bottom(10)
        sidebar.set_margin_start(10)
        sidebar.set_margin_end(10)
        sidebar.add_css_class("settings-nav")

        # ----------------------------
        # POINTER CURSOR ON NAV ITEMS ONLY
        # ----------------------------
        def _find_listbox_row(w: Gtk.Widget):
            try:
                cur = w
                while cur is not None:
                    if isinstance(cur, Gtk.ListBoxRow):
                        return cur
                    cur = cur.get_parent()
            except Exception:
                return None
            return None

        def _nav_set_cursor_for_xy(x: float, y: float):
            try:
                picked = sidebar.pick(x, y, Gtk.PickFlags.DEFAULT)
            except Exception:
                picked = None

            row = _find_listbox_row(picked) if picked is not None else None
            try:
                if row is not None:
                    sidebar.set_cursor_from_name("pointer")
                else:
                    sidebar.set_cursor(None)
            except Exception:
                pass

        nav_motion = Gtk.EventControllerMotion()
        nav_motion.connect("enter", lambda _c, x, y: _nav_set_cursor_for_xy(x, y))
        nav_motion.connect("motion", lambda _c, x, y: _nav_set_cursor_for_xy(x, y))
        nav_motion.connect("leave", lambda *_: sidebar.set_cursor(None))
        sidebar.add_controller(nav_motion)
        # ----------------------------

        def wrap_page(inner: Gtk.Widget) -> Gtk.Widget:
            sc = Gtk.ScrolledWindow()
            sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sc.set_hexpand(True)
            sc.set_vexpand(True)

            pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            pad.set_margin_top(12)
            pad.set_margin_bottom(12)
            pad.set_margin_start(12)
            pad.set_margin_end(12)
            pad.set_hexpand(True)
            pad.set_vexpand(True)
            pad.append(inner)

            sc.set_child(pad)
            return sc

        self._win.settings_stack.add_titled(wrap_page(self._settings_page_general()), "general", "General")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_launch()), "launch", "Launch")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_mods()), "mods", "Mods")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_discord()), "discord", "Discord")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_about()), "about", "About")

        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nav.set_vexpand(True)
        nav.set_hexpand(False)
        nav.append(sidebar)
        nav.append(hr())

        reset_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        reset_wrap.set_margin_start(10)
        reset_wrap.set_margin_end(10)
        reset_wrap.set_margin_top(10)
        reset_wrap.set_margin_bottom(10)
        reset_wrap.set_hexpand(True)
        reset_wrap.set_halign(Gtk.Align.FILL)

        sp_l = Gtk.Box()
        sp_l.set_hexpand(True)
        sp_r = Gtk.Box()
        sp_r.set_hexpand(True)

        reset_btn = Gtk.Button(label="Reset To Defaults")
        reset_btn.set_can_focus(False)
        attach_pointer_cursor(reset_btn)
        reset_btn.set_halign(Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._on_reset_settings_clicked)

        reset_wrap.append(sp_l)
        reset_wrap.append(reset_btn)
        reset_wrap.append(sp_r)

        nav.append(reset_wrap)

        body.append(nav)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self._win.settings_stack)

        panel.append(body)

        # Apply runtime effects once panel is built (master toggles / dependent widgets / etc.)
        # If main.py doesn't recognize "settings_init", it's harmless.
        GLib.idle_add(lambda: (self._win._apply_setting_runtime_effects("settings_init"), False)[1])

        return panel

    def _settings_section_header(self, text: str) -> Gtk.Widget:
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        lbl.add_css_class("settings-section-title")
        return lbl

    def _settings_row_entry(
            self,
            title: str,
            key: str,
            placeholder: str = "",
            is_int: bool = False,
            tooltip: str | None = None,
            autodetect_fn=None,  # optional callable -> str
            user_set_flag: str | None = None,  # optional bool key to mark "user typed"
            browse: bool = False,  # NEW: show Browse… button
            browse_select_folder: bool = False,  # NEW: folder picker vs file picker
            show_autodetect_guess: bool = True,
    ) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        l = Gtk.Label(label=title)
        l.set_xalign(0)

        e = Gtk.Entry()
        if placeholder:
            e.set_placeholder_text(placeholder)
        if tooltip:
            e.set_tooltip_text(tooltip)

        cur = self._win.settings.get(key, "")
        cur_s = ("" if cur is None else str(cur)).strip()

        # Ensure user_set_flag exists if requested
        if user_set_flag and user_set_flag not in self._win.settings:
            self._win.settings[user_set_flag] = False
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        # If empty and autodetect available, display suggestion (dimmed) but do NOT mark user-set.
        # If empty and autodetect available, display suggestion (dimmed) but do NOT
        # write it into live settings. Runtime code must resolve the path itself.
        if (not cur_s) and callable(autodetect_fn) and show_autodetect_guess:
            guess = ""
            try:
                guess = (str(autodetect_fn() or "")).strip()
            except Exception:
                guess = ""
            if guess:
                try:
                    self._win._settings_update_guard = True
                    e.set_text(guess)
                finally:
                    self._win._settings_update_guard = False

                e.add_css_class("dimmed-entry")
            else:
                e.set_text(cur_s)
        else:
            e.set_text(cur_s)

            # If we have a value but it wasn't user-set, dim it.
            if user_set_flag and cur_s and (not bool(self._win.settings.get(user_set_flag, False))):
                e.add_css_class("dimmed-entry")

        def on_changed(_e):
            if getattr(self._win, "_settings_update_guard", False):
                return

            val = e.get_text()
            val_s = (val or "").strip()

            if user_set_flag:
                self._win.settings[user_set_flag] = bool(val_s)
                e.remove_css_class("dimmed-entry")

            if is_int:
                try:
                    val_i = int(val_s)
                except Exception:
                    val_i = int(self._win.settings.get(key, 0) or 0)
                self._win.settings[key] = val_i
            else:
                self._win.settings[key] = val_s

            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        e.connect("changed", on_changed)
        self._win._settings_widgets[key] = e

        # Entry row: Entry + optional Browse button
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.set_hexpand(True)
        e.set_hexpand(True)
        line.append(e)

        if browse:
            btn = Gtk.Button(label="Browse")
            btn.set_can_focus(False)
            attach_pointer_cursor(btn)
            line.append(btn)

            def _on_browse(_btn):
                try:
                    dlg = Gtk.FileChooserNative.new(
                        title=title,
                        parent=self._win,
                        action=Gtk.FileChooserAction.SELECT_FOLDER if browse_select_folder else Gtk.FileChooserAction.OPEN,
                        accept_label="Select",
                        cancel_label="Cancel",
                    )

                    def _done(_dlg, response):
                        try:
                            if response != Gtk.ResponseType.ACCEPT:
                                return
                            f = _dlg.get_file()
                            if not f:
                                return
                            path = f.get_path() or ""
                            if not path:
                                return

                            # mark as user set + undim
                            if user_set_flag:
                                self._win.settings[user_set_flag] = True
                            e.remove_css_class("dimmed-entry")

                            # set entry text without triggering recursive updates
                            self._win._settings_update_guard = True
                            try:
                                e.set_text(path)
                            finally:
                                self._win._settings_update_guard = False

                            # write value + persist
                            if is_int:
                                try:
                                    self._win.settings[key] = int(str(path).strip())
                                except Exception:
                                    self._win.settings[key] = int(self._win.settings.get(key, 0) or 0)
                            else:
                                self._win.settings[key] = (path or "").strip()

                            try:
                                save_settings(self._win.settings)
                            except Exception:
                                pass

                            try:
                                self._win._apply_setting_runtime_effects(key)
                            except Exception:
                                pass
                        except Exception:
                            pass

                    dlg.connect("response", _done)
                    dlg.show()
                except Exception:
                    pass

            btn.connect("clicked", _on_browse)

        row.append(l)
        row.append(line)
        return row

    def _settings_row_switch(self, title: str, key: str, default: bool = False, tooltip: str | None = None) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_hexpand(True)

        l = Gtk.Label(label=title)
        l.set_xalign(0)
        l.set_hexpand(True)

        if key not in self._win.settings:
            self._win.settings[key] = bool(default)
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        sw = Gtk.Switch()
        sw.set_active(bool(self._win.settings.get(key, False)))
        attach_pointer_cursor(sw)

        if tooltip:
            row.set_tooltip_text(tooltip)
            sw.set_tooltip_text(tooltip)

        def on_toggled(_sw, _pspec):
            if getattr(self._win, "_settings_update_guard", False):
                return

            self._win.settings[key] = bool(sw.get_active())
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        sw.connect("notify::active", on_toggled)
        self._win._settings_widgets[key] = sw

        row.append(l)
        row.append(sw)
        return row

    def _settings_row_backend_switch(self) -> Gtk.Widget:
        key = "mod_download_backend"
        tooltip = (
            "Use SteamCMD instead of the recommended Steam client downloader. "
            "Only needed for troubleshooting."
        )

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_hexpand(True)
        row.set_tooltip_text(tooltip)

        label = Gtk.Label(label="Advanced SteamCMD Fallback")
        label.set_xalign(0)
        label.set_hexpand(True)

        sw = Gtk.Switch()
        sw.set_active(str(self._win.settings.get(key) or "steam_client") == "steamcmd")
        sw.set_tooltip_text(tooltip)
        attach_pointer_cursor(sw)

        def on_toggled(_sw, _pspec):
            if getattr(self._win, "_settings_update_guard", False):
                return

            self._win.settings[key] = "steamcmd" if sw.get_active() else "steam_client"
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        sw.connect("notify::active", on_toggled)
        self._win._settings_widgets[key] = sw

        row.append(label)
        row.append(sw)
        return row

    def _settings_row_dropdown(self, title: str, key: str, options: list[tuple[str, str]], default_val: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        l = Gtk.Label(label=title)
        l.set_xalign(0)

        model = Gtk.StringList.new([label for _val, label in options])
        dd = Gtk.DropDown.new(model, None)
        attach_pointer_cursor(dd)

        cur = str(self._win.settings.get(key, default_val) or default_val)
        idx = 0
        for i, (v, _lab) in enumerate(options):
            if v == cur:
                idx = i
                break
        dd.set_selected(idx)

        def on_sel(_dd, _pspec):
            if getattr(self._win, "_settings_update_guard", False):
                return

            i = int(dd.get_selected())
            if 0 <= i < len(options):
                self._win.settings[key] = options[i][0]
                try:
                    save_settings(self._win.settings)
                except Exception:
                    pass

                try:
                    self._win._apply_setting_runtime_effects(key)
                except Exception:
                    pass

        dd.connect("notify::selected", on_sel)
        self._win._settings_widgets[key] = dd

        row.append(l)
        row.append(dd)
        return row

    def _settings_row_checkbox(self, title: str, key: str, default: bool = False) -> Gtk.Widget:
        cb = Gtk.CheckButton(label=title)
        attach_pointer_cursor(cb)

        if key not in self._win.settings:
            self._win.settings[key] = bool(default)
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        cb.set_active(bool(self._win.settings.get(key, False)))

        def on_toggled(_cb):
            if getattr(self._win, "_settings_update_guard", False):
                return

            self._win.settings[key] = bool(cb.get_active())
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        cb.connect("toggled", on_toggled)
        self._win._settings_widgets[key] = cb
        return cb

    def _settings_page_general(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("General"))

        box.append(self._settings_row_switch("Show Server Companion", "show_server_companion", default=False))
        box.append(self._settings_row_entry("Ingame Name", "ingame_name", "Required By Many Servers"))
        box.append(self._settings_row_switch("Hide Test Servers By Default", "hide_test_servers", default=True))
        # These controls are currently exposed in the sidebar; the underlying
        # settings are intentionally retained for persistence and future reuse.

        box.append(hr())
        box.append(self._settings_row_switch("Show Counts In Title Bar", "show_counts_in_title_bar", default=False))
        box.append(self._settings_row_checkbox("Servers Loaded", "show_counts_servers_loaded", default=True))
        box.append(self._settings_row_checkbox("Global Players", "show_counts_global_players", default=True))

        master = bool(self._win.settings.get("show_counts_in_title_bar", False))
        self._win._set_widget_sensitive("show_counts_servers_loaded", master)
        self._win._set_widget_sensitive("show_counts_global_players", master)

        box.append(hr())

        btn = Gtk.Button(label="Clear Played History")
        btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(btn)
        btn.connect("clicked", lambda _b: self._win._clear_played_history())
        box.append(btn)

        box.append(hr())
        box.append(self._settings_row_switch("Auto Check For Updates", "auto_check_updates", default=True))

        update_db_btn = Gtk.Button(label="Update Server Database")
        update_db_btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(update_db_btn)
        update_db_btn.connect("clicked", lambda _b: self._win._manual_update_server_database())
        box.append(update_db_btn)
        return box

    def _settings_page_launch(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Launch"))

        box.append(self._settings_row_entry(
            "Additional Launch Params",
            "additional_launch_params",
            "-nosplash -world=empty",
        ))

        box.append(hr())

        box.append(self._settings_row_switch(
            "Start Steam Automatically on Join",
            "start_steam_on_join",
            default=False,
            tooltip="Automatically start Steam when joining a server if Steam is closed.",
        ))

        box.append(self._settings_row_switch(
            "Skip DayZ Launcher",
            "skip_dayz_launcher",
            default=True,
        ))

        box.append(self._settings_row_switch(
            "Minimise DayZ Launcher",
            "minimize_dayz_launcher",
            default=False,
            tooltip="Minimise the DayZ launcher on game start. Helps prevent the launcher border being visible in-game.",
        ))

        box.append(self._settings_row_switch("No Splash", "no_splash", default=True))

        box.append(self._settings_row_switch(
            "Force Fullscreen",
            "force_fullscreen",
            default=False,
            tooltip="If enabled, Windowed Mode is disabled.",
        ))

        box.append(self._settings_row_switch(
            "Windowed Mode",
            "windowed_mode",
            default=False,
            tooltip="If enabled, Force Fullscreen is disabled.",
        ))

        return box

    def _settings_page_mods(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Mods"))

        box.append(self._settings_row_switch(
            "Enable DZLL Mod Management",
            "enable_steamcmd_mod_handling",
            default=True,
            tooltip="Master switch for DZLL required-mod handling, downloads, and link management.",
        ))

        box.append(self._settings_row_entry(
            "Workshop Directory",
            "workshop_dir",
            "Auto-detected if empty",
            tooltip="If empty, DZLL uses the auto-detected Steam Workshop directory.",
            autodetect_fn=autodetect_workshop_dir,
            user_set_flag="workshop_dir_user_set",
            browse=True,
            browse_select_folder=True,  # folder
            show_autodetect_guess=False,
        ))

        box.append(self._settings_row_entry(
            "Additional Mods (Id's)",
            "additional_mod_ids",
            "1559212036, 1234567890",
        ))

        box.append(self._settings_row_switch(
            "Auto-install Missing Mods",
            "auto_install_missing_mods",
            default=True,
            tooltip="When joining a server, automatically download required mods that are missing.",
        ))

        box.append(hr())
        box.append(self._settings_section_header("Advanced / Fallback (Not Recommended)"))

        box.append(self._settings_row_backend_switch())

        steamcmd_warning = Gtk.Label(
            label="SteamCMD fallback may close Steam during mod downloads to avoid login/session conflicts."
        )
        steamcmd_warning.set_xalign(0.0)
        steamcmd_warning.set_wrap(True)
        steamcmd_warning.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        steamcmd_warning.add_css_class("settings-warning-label")

        steamcmd_rows = [
            steamcmd_warning,
            self._settings_row_entry(
                "Steam Username",
                "steamcmd_username",
                "Username Only (No Password Stored)",
            ),
            self._settings_row_entry(
                "SteamCMD Install Path",
                "steamcmd_path",
                "Auto Detect If Empty",
                tooltip="If empty, DZLL attempts to autodetect SteamCMD.",
                autodetect_fn=autodetect_steamcmd_path,
                user_set_flag="steamcmd_path_user_set",
                browse=True,
                browse_select_folder=False,
            ),
            self._settings_row_switch(
                "Verify Mod Files (Slower)",
                "verify_mod_files",
                default=False,
                tooltip="SteamCMD only: validate and repair required workshop files.",
            ),
            self._settings_row_switch(
                "Auto Update Required Mods",
                "auto_update_required_mods",
                default=False,
                tooltip="SteamCMD fallback only: request all required mods again on join so SteamCMD can update them.",
            ),
        ]
        self._steamcmd_advanced_rows = steamcmd_rows
        for row in steamcmd_rows:
            box.append(row)

        box.append(hr())

        btn = Gtk.Button(label="Manage Installed Mods")
        btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(btn)
        box.append(btn)
        btn.connect("clicked", lambda *_: self.open_mods_manager())

        return box

    def _settings_page_discord(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Discord"))

        box.append(self._settings_row_switch("Enable Rich Presence", "discord_rich_presence", default=False))
        box.append(self._settings_row_switch(
            "Privacy Mode (Hide Server Details)",
            "discord_privacy_mode",
            default=False,
            tooltip="Only applies when Detail Level is set to 'On Server'.",
        ))

        box.append(self._settings_row_dropdown(
            "Detail Level",
            "discord_detail_level",
            options=[("menus", "In Menus"), ("ingame", "In Game"), ("server", "On Server")],
            default_val="menus",
        ))
        return box

    def _settings_page_about(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("About"))

        lbl = Gtk.Label(
            label=f"DayZ Linux Launcher Version: {APP_VERSION}\n\n"
                  "Unofficial Community Launcher.\n"
                  "Not Affiliated With Bohemia Interactive."
        )
        lbl.set_xalign(0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_halign(Gtk.Align.CENTER)
        box.append(lbl)

        update_status = Gtk.Label(label="")
        update_status.set_xalign(0.5)
        update_status.set_halign(Gtk.Align.CENTER)
        box.append(update_status)

        btn_updates = Gtk.Button(label="Check For Updates")
        btn_updates.set_can_focus(False)
        attach_pointer_cursor(btn_updates)
        btn_updates.connect(
            "clicked",
            lambda *_: (
                update_status.set_text("Checking for updates…"),
                self._win._manual_check_for_updates(update_status.set_text),
            ),
        )

        center_box = Gtk.Box(halign=Gtk.Align.CENTER)
        center_box.append(btn_updates)
        btn_updates.set_margin_start(10)
        btn_updates.set_margin_end(10)
        box.append(center_box)

        box.append(hr())
        box.append(self._settings_section_header("Support DZLL"))

        try:
            qr_path = os.path.join(IMAGES_DIR, "qr-code.png")
            if os.path.exists(qr_path):
                qr = Gtk.Picture.new_for_filename(qr_path)
                qr.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                qr.set_can_shrink(True)
                qr.set_halign(Gtk.Align.CENTER)
                qr.set_valign(Gtk.Align.START)
                qr.set_hexpand(False)
                box.append(qr)
        except Exception:
            pass

        try:
            bmc_path = os.path.join(IMAGES_DIR, "buy-coffee.png")
            if os.path.exists(bmc_path):
                bmc_btn = Gtk.Button()
                bmc_btn.set_can_focus(False)
                bmc_btn.add_css_class("flat")
                attach_pointer_cursor(bmc_btn)
                bmc_btn.set_tooltip_text("Support DZLL (Buy Me A Coffee)")
                bmc_btn.set_halign(Gtk.Align.CENTER)
                bmc_btn.set_valign(Gtk.Align.START)
                bmc_btn.set_hexpand(False)

                pic = Gtk.Picture.new_for_filename(bmc_path)
                pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                pic.set_can_shrink(True)
                pic.set_halign(Gtk.Align.CENTER)
                pic.set_valign(Gtk.Align.START)
                pic.set_hexpand(False)

                bmc_btn.set_child(pic)
                bmc_btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
                    "https://buymeacoffee.com/berry.dingle", None
                ))
                box.append(bmc_btn)
        except Exception:
            pass

        box.append(hr())
        box.append(self._settings_section_header("Links"))

        repo_url = RELEASES_URL
        try:
            if repo_url.endswith("/releases"):
                repo_url = repo_url[:-len("/releases")]
        except Exception:
            pass
        issues_url = repo_url.rstrip("/") + "/issues"

        links_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        links_row.set_halign(Gtk.Align.START)
        links_row.set_valign(Gtk.Align.END)

        def _icon_button(filename: str, tooltip: str) -> Gtk.Button:
            b = Gtk.Button()
            b.set_can_focus(False)
            b.add_css_class("flat")
            attach_pointer_cursor(b)
            b.set_tooltip_text(tooltip)

            p = os.path.join(IMAGES_DIR, filename)
            if os.path.exists(p):
                pic = Gtk.Picture.new_for_filename(p)
                pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                pic.set_can_shrink(True)
                b.set_child(pic)
                b.set_size_request(40, 32)  # icon hitbox
            else:
                b.set_label("")
            return b

        btn_repo = _icon_button("github-white-icon.png", "DZLL GitHub Repo")
        btn_repo.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(repo_url, None))
        links_row.append(btn_repo)

        invite = str(self._win.settings.get("discord_invite_url") or "").strip()
        btn_discord = _icon_button("discord-icon.png", "Join DZLL Discord Channel")
        if invite:
            btn_discord.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(invite, None))
        else:
            btn_discord.set_sensitive(False)
            btn_discord.set_tooltip_text("Join DZLL Discord Channel")
        links_row.append(btn_discord)

        btn_issues = _icon_button("zombie.png", "Github Issues Page")
        if btn_issues:
            btn_issues.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(issues_url, None))
        else:
            btn_issues.set_sensitive(False)
            btn_issues.set_tooltip_text("Github Issues Page")
        links_row.append(btn_issues)

        site_url = str(self._win.settings.get("website_url") or "").strip()
        btn_site = _icon_button("website.png", "Visit DZLL Website")
        if site_url:
            btn_site.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(site_url, None))
        else:
            btn_site.set_sensitive(False)
            btn_site.set_tooltip_text("Visit DZLL Website")
        links_row.append(btn_site)

        box.append(links_row)
        return box

    def _set_dropdown_to_value(self, dd: Gtk.DropDown, value: str):
        try:
            model = dd.get_model()
            if model is None:
                return

            # We only need this for the known settings dropdowns currently in DZLL
            key = None
            for k, w in self._win._settings_widgets.items():
                if w is dd:
                    key = k
                    break

            options_map = {
                "discord_detail_level": ["menus", "ingame", "server"],
            }

            vals = options_map.get(key, [])
            if not vals:
                return

            idx = 0
            for i, v in enumerate(vals):
                if v == str(value):
                    idx = i
                    break

            dd.set_selected(idx)
        except Exception:
            pass

    def _reset_entry_with_autodetect(self, key: str, autodetect_fn=None, user_set_flag: str | None = None):
        self._reset_entry_value(key, autodetect_fn=autodetect_fn, user_set_flag=user_set_flag, show_autodetect_guess=True)

    def _reset_entry_value(
            self,
            key: str,
            autodetect_fn=None,
            user_set_flag: str | None = None,
            show_autodetect_guess: bool = True,
    ):
        e = self._win._settings_widgets.get(key)
        if not isinstance(e, Gtk.Entry):
            return

        saved_val = str(self._win.settings.get(key, "") or "").strip()

        try:
            e.remove_css_class("dimmed-entry")
        except Exception:
            pass

        if saved_val:
            e.set_text(saved_val)
            if user_set_flag and not bool(self._win.settings.get(user_set_flag, False)):
                try:
                    e.add_css_class("dimmed-entry")
                except Exception:
                    pass
            return

        guess = ""
        if show_autodetect_guess and callable(autodetect_fn):
            try:
                guess = str(autodetect_fn() or "").strip()
            except Exception:
                guess = ""

        if guess:
            e.set_text(guess)
            try:
                e.add_css_class("dimmed-entry")
            except Exception:
                pass
        else:
            e.set_text("")

    def _on_reset_settings_clicked(self, *_args):
        # Reset DZLL settings only
        self._win.settings = reset_settings()

        try:
            self._win._ping_cutoff_ms = int(self._win.settings.get("high_ping_cutoff_ms", 250) or 250)
        except Exception:
            self._win._ping_cutoff_ms = 250

        try:
            self._win._settings_update_guard = True

            for key, w in self._win._settings_widgets.items():
                val = self._win.settings.get(key)

                if isinstance(w, Gtk.Entry):
                    # generic entries first; special autodetect entries handled below
                    if key not in ("steamcmd_path", "workshop_dir"):
                        w.set_text("" if val is None else str(val))
                        try:
                            w.remove_css_class("dimmed-entry")
                        except Exception:
                            pass

                elif isinstance(w, Gtk.Switch):
                    if key == "mod_download_backend":
                        w.set_active(str(val or "steam_client") == "steamcmd")
                    else:
                        w.set_active(bool(val))

                elif isinstance(w, Gtk.CheckButton):
                    w.set_active(bool(val))

                elif isinstance(w, Gtk.DropDown):
                    self._set_dropdown_to_value(w, val)

            # Rebuild autodetect suggestion entries WITHOUT committing them to settings
            self._reset_entry_with_autodetect(
                "steamcmd_path",
                autodetect_fn=autodetect_steamcmd_path,
                user_set_flag="steamcmd_path_user_set",
            )
            self._reset_entry_value(
                "workshop_dir",
                autodetect_fn=autodetect_workshop_dir,
                user_set_flag="workshop_dir_user_set",
                show_autodetect_guess=False,
            )

        except Exception:
            pass
        finally:
            self._win._settings_update_guard = False

        # Re-apply runtime-only effects from clean settings
        self._win._apply_setting_runtime_effects("high_ping_cutoff_ms")
        self._win._apply_setting_runtime_effects("hide_below_max_players")
        self._win._apply_setting_runtime_effects("hide_test_servers")
        self._win._apply_setting_runtime_effects("prioritise_trusted_servers")
        self._win._apply_setting_runtime_effects("pin_favorite_servers")
        self._win._apply_setting_runtime_effects("show_server_companion")
        self._win._apply_setting_runtime_effects("show_counts_in_title_bar")
        self._win._apply_setting_runtime_effects("show_counts_servers_loaded")
        self._win._apply_setting_runtime_effects("show_counts_global_players")
        self._win._apply_setting_runtime_effects("enable_steamcmd_mod_handling")
        self._win._apply_setting_runtime_effects("mod_download_backend")
        self._win._apply_setting_runtime_effects("skip_dayz_launcher")
        self._win._apply_setting_runtime_effects("minimize_dayz_launcher")
        self._win._apply_setting_runtime_effects("force_fullscreen")
        self._win._apply_setting_runtime_effects("windowed_mode")
        self._win._apply_setting_runtime_effects("discord_detail_level")
        self._win._apply_titlebar_counts()

    def _set_widget_sensitive(self, key: str, sensitive: bool):
        w = self._win._settings_widgets.get(key)
        if w is None:
            return
        try:
            w.set_sensitive(bool(sensitive))
        except Exception:
            pass

    def _sync_sidebar_setting_widget(self, key: str):
        widget = getattr(self._win, "_sidebar_settings_widgets", {}).get(key)
        if widget is None:
            return

        def _entry_has_focus(entry):
            try:
                return bool(entry.has_focus())
            except Exception:
                return False

        def _set_entry_text_if_changed(entry, text):
            text = "" if text is None else str(text)
            if (entry.get_text() or "") == text:
                return
            try:
                pos = int(entry.get_position())
            except Exception:
                pos = -1
            entry.set_text(text)
            if pos >= 0:
                try:
                    entry.set_position(min(pos, len(text)))
                except Exception:
                    pass

        old_guard = bool(getattr(self._win, "_settings_update_guard", False))
        try:
            self._win._settings_update_guard = True
            val = self._win.settings.get(key)
            if isinstance(widget, Gtk.Entry):
                if not _entry_has_focus(widget):
                    _set_entry_text_if_changed(widget, "" if val is None else str(val))
            elif isinstance(widget, Gtk.CheckButton):
                if bool(widget.get_active()) != bool(val):
                    widget.set_active(bool(val))
        except Exception:
            pass
        finally:
            self._win._settings_update_guard = old_guard

    def _apply_setting_runtime_effects(self, key: str):
        self._sync_sidebar_setting_widget(key)

        if key == "high_ping_cutoff_ms":
            try:
                self._win._ping_cutoff_ms = int(self._win.settings.get("high_ping_cutoff_ms", 250) or 250)
            except Exception:
                self._win._ping_cutoff_ms = 250

            for k, obj in self._win._obj_by_key.items():
                try:
                    p = int(obj.ping)
                except Exception:
                    p = -1
                if p >= 0:
                    self._win.live.setdefault(k, {})["hide_high_ping"] = (p > self._win._ping_cutoff_ms)
            self._win._on_filter_changed(reason="settings")

        if key == "hide_test_servers":
            self._win._on_filter_changed(reason="settings")

        if key == "hide_below_max_players":
            self._win._on_filter_changed(reason="settings")

        if key == "prioritise_trusted_servers":
            try:
                self._win._snapshot_all_sort_keys()
                sorter = getattr(self._win, "sorter", None)
                if sorter is not None:
                    sorter.changed(Gtk.SorterChange.DIFFERENT)
                GLib.idle_add(self._win._scroll_to_top)
            except Exception:
                pass

        if key == "pin_favorite_servers":
            scroll_value = None
            try:
                vadj = self._win.scroller.get_vadjustment()
                if vadj:
                    scroll_value = float(vadj.get_value())
            except Exception:
                scroll_value = None

            try:
                self._win._snapshot_all_sort_keys()
                sorter = getattr(self._win, "sorter", None)
                if sorter is not None:
                    sorter.changed(Gtk.SorterChange.DIFFERENT)
                self._win._rebuild_column_view_store(reorder_reason="settings:pin-favorite-servers")
            except Exception:
                pass

            if scroll_value is not None:
                def restore_pin_favorites_scroll(value=scroll_value):
                    try:
                        vadj = self._win.scroller.get_vadjustment()
                        if vadj:
                            vadj.set_value(value)
                    except Exception:
                        pass
                    return False

                GLib.idle_add(restore_pin_favorites_scroll)

        if key == "show_server_companion":
            self._win.set_server_companion_visible(bool(self._win.settings.get("show_server_companion", False)))

        if key in ("show_counts_in_title_bar", "show_counts_servers_loaded", "show_counts_global_players"):
            master = bool(self._win.settings.get("show_counts_in_title_bar", False))
            self._set_widget_sensitive("show_counts_servers_loaded", master)
            self._set_widget_sensitive("show_counts_global_players", master)
            self._win._apply_titlebar_counts()

        if key in ("enable_steamcmd_mod_handling", "mod_download_backend", "settings_init"):
            enabled = bool(self._win.settings.get("enable_steamcmd_mod_handling", True))
            backend = str(self._win.settings.get("mod_download_backend") or "steam_client")
            steamcmd_selected = backend == "steamcmd"

            for row in getattr(self, "_steamcmd_advanced_rows", []):
                try:
                    row.set_visible(steamcmd_selected)
                    row.set_sensitive(enabled and steamcmd_selected)
                except Exception:
                    pass

            for dep in (
                    "auto_install_missing_mods",
                    "workshop_dir",
                    "additional_mod_ids",
            ):
                self._set_widget_sensitive(dep, enabled)

        # Force Fullscreen <-> Windowed Mode (mutual exclusion, NO flicker)
        if key in ("force_fullscreen", "windowed_mode", "settings_init"):
            fs = bool(self._win.settings.get("force_fullscreen", False))
            wn = bool(self._win.settings.get("windowed_mode", False))

            # Always keep both clickable
            try:
                self._set_widget_sensitive("windowed_mode", True)
                self._set_widget_sensitive("force_fullscreen", True)
            except Exception:
                pass

            # Helper: set a key + switch without recursion
            def _set_switch(keyname: str, val: bool):
                try:
                    self._win._settings_update_guard = True
                    self._win.settings[keyname] = bool(val)
                    w = self._win._settings_widgets.get(keyname)
                    if w:
                        w.set_active(bool(val))
                    save_settings(self._win.settings)
                except Exception:
                    pass
                finally:
                    self._win._settings_update_guard = False

            # ---- Precedence: the switch the user just changed wins ----
            if key == "windowed_mode":
                # If user turned Windowed ON, force Fullscreen OFF first.
                if wn and fs:
                    _set_switch("force_fullscreen", False)

            elif key == "force_fullscreen":
                # If user turned Fullscreen ON, force Windowed OFF.
                if fs and wn:
                    _set_switch("windowed_mode", False)

            else:
                # settings_init: if both are True, pick one deterministically.
                # Prefer windowed (safer/less surprising).
                if fs and wn:
                    _set_switch("force_fullscreen", False)

        # Skip DayZ Launcher <-> Minimise DayZ Launcher
        if key in ("skip_dayz_launcher", "minimize_dayz_launcher", "settings_init"):
            skip = bool(self._win.settings.get("skip_dayz_launcher", True))
            mini = bool(self._win.settings.get("minimize_dayz_launcher", False))

            def _set_switch(keyname: str, val: bool):
                try:
                    self._win._settings_update_guard = True
                    self._win.settings[keyname] = bool(val)
                    w = self._win._settings_widgets.get(keyname)
                    if w:
                        w.set_active(bool(val))
                    save_settings(self._win.settings)
                except Exception:
                    pass
                finally:
                    self._win._settings_update_guard = False

            if key == "skip_dayz_launcher":
                if skip and mini:
                    _set_switch("minimize_dayz_launcher", False)
            elif key == "minimize_dayz_launcher":
                if mini and skip:
                    _set_switch("skip_dayz_launcher", False)
            elif skip and mini:
                _set_switch("minimize_dayz_launcher", False)

        # ---- Discord Rich Presence ----
        if key in ("discord_rich_presence", "discord_privacy_mode", "discord_detail_level", "settings_init"):
            # 1) Apply settings to RPC
            try:
                if hasattr(self._win, "_discord") and self._win._discord:
                    self._win._discord.apply_settings(self._win.settings)
            except Exception:
                pass

            # 2) Gate Privacy Mode toggle (only meaningful when RP ON + Detail=On Server)
            try:
                rp_on = bool(self._win.settings.get("discord_rich_presence", False))
                lvl = str(self._win.settings.get("discord_detail_level", "menus") or "menus").strip().lower()
                allow_priv = rp_on and (lvl == "server")
                self._set_widget_sensitive("discord_privacy_mode", allow_priv)

                # Optional: if it can't apply, force it OFF so settings stay logical
                if not allow_priv and bool(self._win.settings.get("discord_privacy_mode", False)):
                    try:
                        self._win._settings_update_guard = True
                        self._win.settings["discord_privacy_mode"] = False
                        w = self._win._settings_widgets.get("discord_privacy_mode")
                        if w:
                            w.set_active(False)
                        save_settings(self._win.settings)
                    finally:
                        self._win._settings_update_guard = False
            except Exception:
                pass

    def _clear_played_history(self) -> None:
        try:
            self._win.last_played = {}
            save_last_played(self._win.last_played)
        except Exception:
            pass

        try:
            for i in range(self._win.store.get_n_items()):
                obj = self._win.store.get_item(i)
                if obj is None:
                    continue
                obj.played = ""
                obj.sort_played_days = 999999
        except Exception:
            pass

        try:
            self._win._on_filter_changed(reason="settings")
        except Exception:
            pass

    def _open_settings_panel(self):
        if self._win._settings_open:
            return
        self._win._settings_open = True
        self._win.settings_scrim.set_visible(True)
        self._win.settings_revealer.set_reveal_child(True)

    def _close_settings_panel(self):
        if not self._win._settings_open:
            return
        self._win._settings_open = False
        self._win.settings_revealer.set_reveal_child(False)
        GLib.timeout_add(200, lambda: (self._win.settings_scrim.set_visible(False), False)[1])

    def _toggle_settings_panel(self):
        if self._win._settings_open:
            self._close_settings_panel()
        else:
            self._open_settings_panel()

    def _on_settings_clicked(self, *_args):
        self._toggle_settings_panel()

    def _add_pointer_cursor(self, w: Gtk.Widget):
        try:
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", lambda *_: w.set_cursor_from_name("pointer"))
            motion.connect("leave", lambda *_: w.set_cursor(None))
            w.add_controller(motion)
        except Exception:
            pass
