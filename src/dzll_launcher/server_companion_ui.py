#!/usr/bin/env python3
# server_companion_ui.py

import math
import time
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango, GLib
from .ui_row import PING_CLASSES, attach_pointer_cursor, ping_class


EMPTY_TEXT = "Join a server to begin monitoring"
EMPTY_BREATHE_COLORS = (
    (255, 255, 255),
    (188, 95, 255),
    (143, 125, 255),
    (79, 214, 200),
    (255, 255, 255),
)
ALERT_SOUND_OPTIONS = (
    ("female", "Female Voice"),
    ("male", "Male Voice"),
    ("beep", "Beep Alarm"),
)


class ServerCompanionPanel(Gtk.Box):
    WIDTH = 280
    UNDOCKED_COMPACT_HEIGHT = 470
    UNDOCKED_EXPANDED_HEIGHT = 550

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._snapshot = None
        self._on_play_pause = None
        self._on_join = None
        self._on_restart_alert_toggled = None
        self._on_alert_sound_changed = None
        self._on_alert_volume_changed = None
        self._on_alert_test_clicked = None
        self._on_clear = None
        self._on_dock_toggle = None
        self._on_power_off = None
        self._alert_sound_value = "female"
        self._restart_alert_usable = False
        self._empty_breathe_timer_id = 0
        self._empty_clock_timer_id = 0
        self._restart_countdown_colon_timer_id = 0
        self._join_status_flash_timer_id = 0
        self._join_status_flash_step = 0
        self._empty_clock_colon_visible = True
        self._restart_countdown_colon_visible = True
        self._empty_breathe_phase = 0.0
        self.set_size_request(self.WIDTH, -1)
        self.set_hexpand(False)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.FILL)
        self.add_css_class("server-companion-panel")
        self.add_css_class("server-companion-panel-docked")

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        self.append(inner)

        heading_row = Gtk.CenterBox()
        heading_row.set_hexpand(True)

        heading_label = Gtk.Label(label="DZLL Server Companion")
        heading_label.set_xalign(0.5)
        heading_label.add_css_class("settings-section-title")
        heading_row.set_center_widget(heading_label)

        power_off_icon = Gtk.Image.new_from_icon_name("system-shutdown-symbolic")
        power_off_icon.set_pixel_size(16)
        self.power_off_btn = Gtk.Button()
        self.power_off_btn.set_can_focus(False)
        self.power_off_btn.set_child(power_off_icon)
        self.power_off_btn.add_css_class("flat")
        self.power_off_btn.add_css_class("server-companion-power-on-button")
        self.power_off_btn.set_tooltip_text("Turn off Server Companion")
        self.power_off_btn.set_size_request(28, 28)
        self.power_off_btn.connect("clicked", self._on_power_off_clicked)
        attach_pointer_cursor(self.power_off_btn)
        heading_row.set_start_widget(self.power_off_btn)

        self.dock_toggle_btn = Gtk.Button(label="↗")
        self.dock_toggle_btn.add_css_class("flat")
        self.dock_toggle_btn.set_tooltip_text("Undock Companion")
        self.dock_toggle_btn.set_size_request(28, 28)
        self.dock_toggle_btn.connect("clicked", self._on_dock_toggle_clicked)
        attach_pointer_cursor(self.dock_toggle_btn)
        heading_row.set_end_widget(self.dock_toggle_btn)

        inner.append(heading_row)
        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.empty_label = Gtk.Label(label=EMPTY_TEXT)
        self.empty_label.set_wrap(True)
        self.empty_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.empty_label.set_xalign(0.5)
        self.empty_label.set_justify(Gtk.Justification.CENTER)
        self.empty_label.add_css_class("companion-empty-breathe")
        inner.append(self.empty_label)
        self._start_empty_breathe()

        self.empty_clock_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.empty_clock_box.set_halign(Gtk.Align.CENTER)
        self.empty_clock_hour_label = Gtk.Label()
        self.empty_clock_colon_label = Gtk.Label()
        self.empty_clock_colon_label.set_margin_end(2)
        self.empty_clock_minute_label = Gtk.Label()
        self.empty_clock_box.append(self.empty_clock_hour_label)
        self.empty_clock_box.append(self.empty_clock_colon_label)
        self.empty_clock_box.append(self.empty_clock_minute_label)
        inner.append(self.empty_clock_box)
        self._start_empty_clock()

        self.server_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.server_box.set_visible(False)
        inner.append(self.server_box)

        self.name_label = Gtk.Label()
        self.name_label.set_wrap(False)
        self.name_label.set_single_line_mode(True)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_max_width_chars(28)
        self.name_label.set_hexpand(True)
        self.name_label.set_xalign(0.0)
        self.name_label.add_css_class("server-name")
        self.server_box.append(self.name_label)

        self.server_details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.server_box.append(self.server_details_box)

        self.map_label = Gtk.Label()
        self.map_label.set_xalign(0.0)
        self.server_details_box.append(self.map_label)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.server_details_box.append(status_row)

        self.status_label = Gtk.Label(label="Status:")
        self.status_label.set_xalign(0.0)
        status_row.append(self.status_label)

        self.status_value_label = Gtk.Label()
        self.status_value_label.set_xalign(0.0)
        status_row.append(self.status_value_label)

        ping_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.server_details_box.append(ping_row)

        self.ping_label = Gtk.Label(label="Ping:")
        self.ping_label.set_xalign(0.0)
        ping_row.append(self.ping_label)

        self.ping_value_label = Gtk.Label()
        self.ping_value_label.set_xalign(0.0)
        ping_row.append(self.ping_value_label)

        self.players_label = Gtk.Label()
        self.players_label.set_xalign(0.0)
        self.server_details_box.append(self.players_label)

        self.queue_label = Gtk.Label()
        self.queue_label.set_xalign(0.0)
        self.queue_label.set_visible(False)
        self.server_details_box.append(self.queue_label)

        self.time_label = Gtk.Label()
        self.time_label.set_xalign(0.0)
        self.server_details_box.append(self.time_label)

        self.restart_learning_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.restart_learning_box.add_css_class("companion-restart-learning")
        self.restart_learning_box.set_visible(False)
        self.server_box.append(self.restart_learning_box)

        def append_restart_learning_row(label_text: str) -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            label = Gtk.Label(label=label_text)
            label.set_xalign(0.0)
            row.append(label)
            value_label = Gtk.Label()
            value_label.set_xalign(0.0)
            value_label.add_css_class("companion-restart-learning-value")
            row.append(value_label)
            self.restart_learning_box.append(row)
            return row, label, value_label

        (
            self.restart_cycle_row,
            self.restart_cycle_label,
            self.restart_cycle_value_label,
        ) = append_restart_learning_row("Restart Cycle:")
        (
            self.restart_next_row,
            self.restart_next_label,
            self.restart_next_value_label,
        ) = append_restart_learning_row("Next Restart:")
        (
            self.restart_countdown_row,
            self.restart_countdown_label,
            self.restart_countdown_value_label,
        ) = append_restart_learning_row("Countdown:")
        self.restart_countdown_row.remove(self.restart_countdown_value_label)
        self.restart_countdown_value_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.restart_countdown_hh_label = Gtk.Label()
        self.restart_countdown_colon_label = Gtk.Label(label=":")
        self.restart_countdown_mm_label = Gtk.Label()
        for label in (
            self.restart_countdown_hh_label,
            self.restart_countdown_colon_label,
            self.restart_countdown_mm_label,
        ):
            label.set_xalign(0.0)
            label.add_css_class("companion-restart-learning-value")
            self.restart_countdown_value_box.append(label)
        self.restart_countdown_row.append(self.restart_countdown_value_box)
        (
            self.restart_confidence_row,
            self.restart_confidence_label,
            self.restart_confidence_value_label,
        ) = append_restart_learning_row("Confidence:")

        self.server_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        restart_alert_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        restart_alert_row.set_hexpand(True)
        self.server_box.append(restart_alert_row)

        restart_alert_label = Gtk.Label(label="Server Shutdown/Restart Alert")
        restart_alert_label.set_xalign(0.0)
        restart_alert_label.set_hexpand(True)
        restart_alert_row.append(restart_alert_label)

        self.restart_alert_switch = Gtk.Switch()
        self.restart_alert_switch.set_active(False)
        self.restart_alert_switch.connect("notify::active", self._on_restart_alert_clicked)
        restart_alert_row.append(self.restart_alert_switch)

        self.sound_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sound_row.set_hexpand(True)
        self.server_box.append(self.sound_row)

        sound_label = Gtk.Label(label="Alert Sound")
        sound_label.set_xalign(0.0)
        sound_label.set_hexpand(True)
        self.sound_row.append(sound_label)

        self.alert_sound_button = Gtk.MenuButton()
        if hasattr(self.alert_sound_button, "set_has_frame"):
            self.alert_sound_button.set_has_frame(False)
        self.alert_sound_button.add_css_class("companion-flat-menu")
        self.alert_sound_button.set_sensitive(False)
        attach_pointer_cursor(self.alert_sound_button)

        self.alert_sound_label = Gtk.Label()
        self.alert_sound_button.set_child(self.alert_sound_label)

        self.alert_sound_popover = Gtk.Popover()
        sound_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sound_menu.add_css_class("companion-sound-popover")
        for value, label in ALERT_SOUND_OPTIONS:
            option_btn = Gtk.Button(label=label)
            option_btn.add_css_class("flat")
            option_btn.add_css_class("companion-flat-menu-option")
            option_label = option_btn.get_child()
            if isinstance(option_label, Gtk.Label):
                option_label.set_xalign(0.0)
            option_btn.connect("clicked", self._on_alert_sound_option_clicked, value)
            attach_pointer_cursor(option_btn)
            sound_menu.append(option_btn)
        self.alert_sound_popover.set_child(sound_menu)
        self.alert_sound_button.set_popover(self.alert_sound_popover)
        self.set_alert_sound("female")
        self.sound_row.append(self.alert_sound_button)

        self.volume_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.volume_row.set_hexpand(True)
        self.server_box.append(self.volume_row)

        volume_label = Gtk.Label(label="Alert Volume")
        volume_label.set_xalign(0.0)
        self.volume_row.append(volume_label)

        self.alert_volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.alert_volume_scale.set_value(80)
        self.alert_volume_scale.set_hexpand(True)
        self.alert_volume_scale.set_draw_value(False)
        self.alert_volume_scale.set_sensitive(False)
        self.alert_volume_scale.connect("value-changed", self._on_alert_volume_changed_cb)
        self.volume_row.append(self.alert_volume_scale)

        self.alert_volume_percent_label = Gtk.Label(label="30%")
        self.alert_volume_percent_label.set_sensitive(False)
        self.volume_row.append(self.alert_volume_percent_label)

        self.alert_test_button = Gtk.Button(label="▶")
        self.alert_test_button.add_css_class("flat")
        self.alert_test_button.set_sensitive(False)
        self.alert_test_button.connect("clicked", self._on_alert_test_clicked_cb)
        attach_pointer_cursor(self.alert_test_button)
        self.volume_row.append(self.alert_test_button)

        self.restart_alert_info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.restart_alert_info_box.add_css_class("companion-alert-info-box")
        self.restart_alert_info_box.set_visible(False)
        self.server_box.append(self.restart_alert_info_box)

        self.restart_alert_info_label = Gtk.Label()
        self.restart_alert_info_label.set_xalign(0.0)
        self.restart_alert_info_label.set_wrap(True)
        self.restart_alert_info_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.restart_alert_info_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.restart_alert_info_label.set_lines(4)
        self.restart_alert_info_label.set_max_width_chars(34)
        self.restart_alert_info_label.set_hexpand(True)
        self.restart_alert_info_box.append(self.restart_alert_info_label)

        self.alert_audio_status_label = Gtk.Label()
        self.alert_audio_status_label.set_xalign(0.0)
        self.alert_audio_status_label.set_wrap(True)
        self.alert_audio_status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.alert_audio_status_label.set_opacity(0.72)
        self.alert_audio_status_label.set_visible(False)
        self.server_box.append(self.alert_audio_status_label)

        self.server_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.join_status_label = Gtk.Label()
        self.join_status_label.set_xalign(0.5)
        self.join_status_label.set_justify(Gtk.Justification.CENTER)
        self.join_status_label.set_wrap(True)
        self.join_status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.join_status_label.add_css_class("ping-offline")
        self.join_status_label.set_visible(False)
        self.server_box.append(self.join_status_label)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)
        actions.set_margin_top(18)
        self.server_box.append(actions)

        self.play_pause_btn = Gtk.Button(label="Pause")
        self.play_pause_btn.set_size_request(80, 34)
        self.play_pause_btn.set_sensitive(False)
        self.play_pause_btn.connect("clicked", self._on_play_pause_clicked)
        actions.append(self.play_pause_btn)

        self.join_btn = Gtk.Button(label="Join")
        self.join_btn.set_size_request(80, 34)
        self.join_btn.set_sensitive(False)
        self.join_btn.connect("clicked", self._on_join_clicked)
        actions.append(self.join_btn)

        clear_btn = Gtk.Button(label="CLEAR")
        clear_btn.set_size_request(80, 34)
        clear_btn.connect("clicked", self._on_clear_clicked)
        actions.append(clear_btn)

    def set_on_clear(self, callback):
        self._on_clear = callback

    def set_on_dock_toggle(self, callback):
        self._on_dock_toggle = callback

    def set_on_power_off(self, callback):
        self._on_power_off = callback

    def set_docked(self, docked: bool):
        if bool(docked):
            self.add_css_class("server-companion-panel-docked")
            self.dock_toggle_btn.set_label("↗")
            self.dock_toggle_btn.set_tooltip_text("Undock Companion")
        else:
            self.remove_css_class("server-companion-panel-docked")
            self.dock_toggle_btn.set_label("↙")
            self.dock_toggle_btn.set_tooltip_text("Dock Companion")

    def restart_learning_visible(self) -> bool:
        return bool(self.restart_learning_box.get_visible())

    def set_on_play_pause(self, callback):
        self._on_play_pause = callback

    def set_on_join(self, callback):
        self._on_join = callback

    def set_on_restart_alert_toggled(self, callback):
        self._on_restart_alert_toggled = callback

    def restart_alert_enabled(self) -> bool:
        return bool(self.restart_alert_switch.get_active())

    def set_restart_alert_enabled(self, enabled: bool):
        self.restart_alert_switch.set_active(bool(enabled))
        self.alert_sound_button.set_sensitive(bool(enabled))
        self.alert_volume_scale.set_sensitive(bool(enabled))
        self.alert_volume_percent_label.set_sensitive(bool(enabled))
        self.alert_test_button.set_sensitive(bool(enabled))

    def set_restart_alert_usability(self, summary: dict | None):
        summary = summary if isinstance(summary, dict) else {}
        usable = bool(summary.get("usable", False))
        message = str(summary.get("message") or "").strip()
        self._restart_alert_usable = usable
        self.sound_row.set_visible(True)
        self.volume_row.set_visible(True)
        self.restart_alert_info_label.set_text(message)
        self.restart_alert_info_box.set_visible(not usable and bool(message))

    def set_on_alert_sound_changed(self, callback):
        self._on_alert_sound_changed = callback

    def set_on_alert_volume_changed(self, callback):
        self._on_alert_volume_changed = callback

    def set_on_alert_test_clicked(self, callback):
        self._on_alert_test_clicked = callback

    def alert_volume(self) -> int:
        try:
            return max(0, min(100, int(round(self.alert_volume_scale.get_value()))))
        except Exception:
            return 80

    def alert_sound(self) -> str:
        value = self._alert_sound_value
        if value in {v for v, _label in ALERT_SOUND_OPTIONS}:
            return value
        return "female"

    def set_alert_sound(self, value: str):
        if value not in {v for v, _label in ALERT_SOUND_OPTIONS}:
            value = "female"
        self._alert_sound_value = value
        label = next((label for v, label in ALERT_SOUND_OPTIONS if v == value), "Female Voice")
        self.alert_sound_label.set_text(f"{label} ∨")

    def set_alert_volume(self, volume: int):
        volume = max(0, min(100, int(volume)))
        self.alert_volume_scale.set_value(volume)
        self.alert_volume_percent_label.set_text(f"{volume}%")

    def set_alert_audio_status(self, message: str | None):
        message = str(message or "").strip()
        self.alert_audio_status_label.set_text(message)
        self.alert_audio_status_label.set_visible(bool(message))

    def set_join_status(self, message: str | None, flash: bool = False):
        message = str(message or "").strip()
        if self._join_status_flash_timer_id:
            GLib.source_remove(self._join_status_flash_timer_id)
            self._join_status_flash_timer_id = 0
        self._join_status_flash_step = 0
        self.join_status_label.set_opacity(1.0)
        self.join_status_label.set_text(message)
        self.join_status_label.set_visible(bool(message))
        if message and flash:
            self._join_status_flash_timer_id = GLib.timeout_add(180, self._join_status_flash_tick)

    def _join_status_flash_tick(self):
        self._join_status_flash_step += 1
        if self._join_status_flash_step >= 4:
            self.join_status_label.set_opacity(1.0)
            self._join_status_flash_timer_id = 0
            return False
        self.join_status_label.set_opacity(0.25 if self._join_status_flash_step % 2 else 1.0)
        return True

    def set_polling_paused(self, paused: bool):
        self.play_pause_btn.set_label("Resume" if paused else "Pause")
        self.play_pause_btn.set_sensitive(self._snapshot is not None)

    def _on_play_pause_clicked(self, _btn):
        if callable(self._on_play_pause):
            self._on_play_pause()

    def _on_join_clicked(self, _btn):
        if self._snapshot is not None and callable(self._on_join):
            self._on_join()

    def _on_restart_alert_clicked(self, *_args):
        enabled = self.restart_alert_enabled()
        self.alert_sound_button.set_sensitive(enabled)
        self.alert_volume_scale.set_sensitive(enabled)
        self.alert_volume_percent_label.set_sensitive(enabled)
        self.alert_test_button.set_sensitive(enabled)
        if callable(self._on_restart_alert_toggled):
            self._on_restart_alert_toggled(enabled)

    def _on_alert_sound_option_clicked(self, _btn, value: str):
        self.set_alert_sound(value)
        self.alert_sound_popover.popdown()
        if callable(self._on_alert_sound_changed):
            self._on_alert_sound_changed(value)

    def _on_alert_volume_changed_cb(self, _scale):
        self.alert_volume_percent_label.set_text(f"{self.alert_volume()}%")
        if callable(self._on_alert_volume_changed):
            self._on_alert_volume_changed(self.alert_volume())

    def _on_alert_test_clicked_cb(self, _btn):
        if callable(self._on_alert_test_clicked):
            self._on_alert_test_clicked(self.alert_volume())

    def _on_clear_clicked(self, _btn):
        if callable(self._on_clear):
            self._on_clear()
        else:
            self.clear_server()

    def _on_dock_toggle_clicked(self, _btn):
        if callable(self._on_dock_toggle):
            self._on_dock_toggle()

    def _on_power_off_clicked(self, _btn):
        if callable(self._on_power_off):
            self._on_power_off()

    def _start_empty_breathe(self):
        if self._empty_breathe_timer_id:
            return
        self._empty_breathe_phase = 0.0
        self._empty_breathe_tick()
        self._empty_breathe_timer_id = GLib.timeout_add(80, self._empty_breathe_tick)

    def _stop_empty_breathe(self):
        if self._empty_breathe_timer_id:
            GLib.source_remove(self._empty_breathe_timer_id)
            self._empty_breathe_timer_id = 0
        self.empty_label.set_text(EMPTY_TEXT)

    def _start_empty_clock(self):
        self.empty_clock_box.set_visible(True)
        self._empty_clock_colon_visible = True
        self._update_empty_clock()
        if self._empty_clock_timer_id:
            return
        self._empty_clock_timer_id = GLib.timeout_add_seconds(1, self._update_empty_clock)

    def _stop_empty_clock(self):
        if self._empty_clock_timer_id:
            GLib.source_remove(self._empty_clock_timer_id)
            self._empty_clock_timer_id = 0
        self.empty_clock_box.set_visible(False)

    def _update_empty_clock(self):
        hour, minute = time.strftime("%H:%M").split(":", 1)
        hour = GLib.markup_escape_text(hour)
        minute = GLib.markup_escape_text(minute)
        self.empty_clock_hour_label.set_markup(
            f'<span foreground="#d0d0d0" alpha="70%" weight="semibold" size="248%">{hour}</span>'
        )
        self.empty_clock_colon_label.set_markup(
            '<span foreground="#d0d0d0" alpha="70%" weight="semibold" size="248%">:</span>'
        )
        self.empty_clock_minute_label.set_markup(
            f'<span foreground="#d0d0d0" alpha="70%" weight="semibold" size="248%">{minute}</span>'
        )
        self.empty_clock_colon_label.set_opacity(1.0 if self._empty_clock_colon_visible else 0.0)
        self._empty_clock_colon_visible = not self._empty_clock_colon_visible
        return bool(self.empty_clock_box.get_visible())

    def _empty_breathe_tick(self):
        self._empty_breathe_phase = (self._empty_breathe_phase + 0.08) % 8.0
        pos = (self._empty_breathe_phase / 8.0) * (len(EMPTY_BREATHE_COLORS) - 1)
        idx = min(int(pos), len(EMPTY_BREATHE_COLORS) - 2)
        t = pos - idx
        t = (1.0 - math.cos(t * math.pi)) / 2.0
        c1 = EMPTY_BREATHE_COLORS[idx]
        c2 = EMPTY_BREATHE_COLORS[idx + 1]
        r = round(c1[0] + (c2[0] - c1[0]) * t)
        g = round(c1[1] + (c2[1] - c1[1]) * t)
        b = round(c1[2] + (c2[2] - c1[2]) * t)
        text = GLib.markup_escape_text(EMPTY_TEXT)
        self.empty_label.set_markup(
            f'<span foreground="#{r:02x}{g:02x}{b:02x}" weight="semibold" size="105%">{text}</span>'
        )
        return bool(self.empty_label.get_visible())

    def _start_restart_countdown_colon(self):
        if self._restart_countdown_colon_timer_id:
            return
        self._restart_countdown_colon_visible = True
        self.restart_countdown_colon_label.set_opacity(1.0)
        self._restart_countdown_colon_timer_id = GLib.timeout_add_seconds(
            1, self._restart_countdown_colon_tick
        )

    def _stop_restart_countdown_colon(self):
        if self._restart_countdown_colon_timer_id:
            GLib.source_remove(self._restart_countdown_colon_timer_id)
            self._restart_countdown_colon_timer_id = 0
        self._restart_countdown_colon_visible = True
        self.restart_countdown_colon_label.set_opacity(1.0)

    def _restart_countdown_colon_tick(self):
        if not self.restart_learning_box.get_visible() or not self.restart_countdown_row.get_visible():
            self._restart_countdown_colon_timer_id = 0
            self._restart_countdown_colon_visible = True
            self.restart_countdown_colon_label.set_opacity(1.0)
            return False
        self.restart_countdown_colon_label.set_opacity(
            1.0 if self._restart_countdown_colon_visible else 0.0
        )
        self._restart_countdown_colon_visible = not self._restart_countdown_colon_visible
        return True

    def set_restart_learning_summary(self, summary: dict | None):
        if summary is None:
            self._stop_restart_countdown_colon()
            self.restart_learning_box.set_visible(False)
            return

        try:
            confidence_percent = int(summary.get("confidence_percent", 0) or 0)
        except Exception:
            confidence_percent = 0
        if confidence_percent < 80:
            self._stop_restart_countdown_colon()
            self.restart_learning_box.set_visible(False)
            return

        self.restart_cycle_label.set_text("Restart Cycle:")
        self.restart_next_label.set_text("Next Restart:")
        self.restart_countdown_label.set_text("Countdown:")
        self.restart_confidence_label.set_text("Confidence:")
        self.restart_cycle_value_label.set_text(str(summary.get("cycle_text") or "--"))
        self.restart_next_value_label.set_text(str(summary.get("next_text") or "--"))
        countdown_text = str(summary.get("countdown_text") or "--")
        countdown_hh, countdown_mm = (
            countdown_text.split(":", 1) if ":" in countdown_text else (countdown_text, "")
        )
        self.restart_countdown_hh_label.set_text(countdown_hh)
        self.restart_countdown_mm_label.set_text(countdown_mm)
        self.restart_confidence_value_label.set_text(f"{confidence_percent}%")
        self.restart_cycle_row.set_visible(True)
        self.restart_next_row.set_visible(True)
        self.restart_countdown_row.set_visible(True)
        self.restart_confidence_row.set_visible(True)
        for c in PING_CLASSES:
            self.restart_confidence_value_label.remove_css_class(c)
        self.restart_confidence_value_label.add_css_class(
            "ping-good" if confidence_percent >= 90 else "ping-greeny"
        )
        self.restart_learning_box.set_visible(True)
        self._start_restart_countdown_colon()

    def set_server_snapshot(self, snapshot: dict):
        self._stop_empty_breathe()
        self._stop_empty_clock()
        self._snapshot = dict(snapshot or {})

        name = str(self._snapshot.get("name") or "Unknown Server")
        online = bool(self._snapshot.get("online", False))
        ping = int(self._snapshot.get("ping", -1) or -1)
        players = int(self._snapshot.get("players", 0) or 0)
        max_players = int(self._snapshot.get("max_players", 0) or 0)
        queue = self._snapshot.get("queue")
        map_name = str(self._snapshot.get("map") or "Unknown")
        time_text = str(self._snapshot.get("time") or "--:--")

        self.name_label.set_text(name)
        self.name_label.set_tooltip_text(name)
        self.status_value_label.set_text("ONLINE" if online else "OFFLINE")
        for c in PING_CLASSES:
            self.status_value_label.remove_css_class(c)
        self.status_value_label.add_css_class("ping-good" if online else "ping-offline")
        self.map_label.set_text(f"Map: {map_name}")
        self.players_label.set_text(f"Players: {players}/{max_players}")
        if queue is None:
            self.queue_label.set_visible(False)
        else:
            self.queue_label.set_text(f"Queue: {int(queue)}")
            self.queue_label.set_visible(True)
        self.ping_value_label.set_text("OFFLINE" if ping < 0 else f"{ping}ms")
        for c in PING_CLASSES:
            self.ping_value_label.remove_css_class(c)
        self.ping_value_label.add_css_class(ping_class(ping))
        self.time_label.set_text(f"Time: {time_text}")
        self.empty_label.set_visible(False)
        self.server_box.set_visible(True)
        self.play_pause_btn.set_sensitive(True)
        self.join_btn.set_sensitive(True)

    def clear_server(self):
        self._snapshot = None
        self.name_label.set_tooltip_text(None)
        self.set_join_status(None)
        self.set_restart_learning_summary(None)
        self.server_box.set_visible(False)
        self.empty_label.set_visible(True)
        self._start_empty_breathe()
        self._start_empty_clock()
        self.play_pause_btn.set_label("Pause")
        self.play_pause_btn.set_sensitive(False)
        self.join_btn.set_sensitive(False)
