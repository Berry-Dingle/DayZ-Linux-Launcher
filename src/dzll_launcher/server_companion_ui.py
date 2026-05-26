#!/usr/bin/env python3
# server_companion_ui.py

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango


class ServerCompanionPanel(Gtk.Box):
    WIDTH = 300

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._snapshot = None
        self._on_play_pause = None
        self._on_restart_alert_toggled = None
        self._on_alert_volume_changed = None
        self._on_clear = None
        self.set_size_request(self.WIDTH, -1)
        self.set_hexpand(False)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.FILL)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        self.append(inner)

        self.empty_label = Gtk.Label(label="Join a server to begin monitoring")
        self.empty_label.set_wrap(True)
        self.empty_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.empty_label.set_xalign(0.0)
        self.empty_label.add_css_class("dim-label")
        inner.append(self.empty_label)

        self.server_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.server_box.set_visible(False)
        inner.append(self.server_box)

        self.name_label = Gtk.Label()
        self.name_label.set_wrap(True)
        self.name_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.name_label.set_xalign(0.0)
        self.name_label.add_css_class("server-name")
        self.server_box.append(self.name_label)

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0.0)
        self.server_box.append(self.status_label)

        self.map_label = Gtk.Label()
        self.map_label.set_xalign(0.0)
        self.server_box.append(self.map_label)

        self.players_label = Gtk.Label()
        self.players_label.set_xalign(0.0)
        self.server_box.append(self.players_label)

        self.ping_label = Gtk.Label()
        self.ping_label.set_xalign(0.0)
        self.server_box.append(self.ping_label)

        self.time_label = Gtk.Label()
        self.time_label.set_xalign(0.0)
        self.server_box.append(self.time_label)

        self.mode_label = Gtk.Label()
        self.mode_label.set_wrap(True)
        self.mode_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.mode_label.set_xalign(0.0)
        self.mode_label.add_css_class("dim-label")
        self.server_box.append(self.mode_label)

        self.restart_alert_cb = Gtk.CheckButton(label="Server Restart Alert")
        self.restart_alert_cb.connect("toggled", self._on_restart_alert_clicked)
        self.server_box.append(self.restart_alert_cb)

        volume_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        volume_row.set_hexpand(True)
        self.server_box.append(volume_row)

        volume_label = Gtk.Label(label="Alert Volume")
        volume_label.set_xalign(0.0)
        volume_row.append(volume_label)

        self.alert_volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.alert_volume_scale.set_value(80)
        self.alert_volume_scale.set_hexpand(True)
        self.alert_volume_scale.set_draw_value(False)
        self.alert_volume_scale.connect("value-changed", self._on_alert_volume_changed_cb)
        volume_row.append(self.alert_volume_scale)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.server_box.append(actions)

        self.play_pause_btn = Gtk.Button(label="Play/Pause")
        self.play_pause_btn.set_sensitive(False)
        self.play_pause_btn.connect("clicked", self._on_play_pause_clicked)
        actions.append(self.play_pause_btn)

        clear_btn = Gtk.Button(label="CLEAR")
        clear_btn.connect("clicked", self._on_clear_clicked)
        actions.append(clear_btn)

    def set_on_clear(self, callback):
        self._on_clear = callback

    def set_on_play_pause(self, callback):
        self._on_play_pause = callback

    def set_on_restart_alert_toggled(self, callback):
        self._on_restart_alert_toggled = callback

    def restart_alert_enabled(self) -> bool:
        return bool(self.restart_alert_cb.get_active())

    def set_on_alert_volume_changed(self, callback):
        self._on_alert_volume_changed = callback

    def alert_volume(self) -> int:
        try:
            return max(0, min(100, int(round(self.alert_volume_scale.get_value()))))
        except Exception:
            return 80

    def set_alert_volume(self, volume: int):
        self.alert_volume_scale.set_value(max(0, min(100, int(volume))))

    def set_polling_paused(self, paused: bool):
        self.play_pause_btn.set_label("Play" if paused else "Pause")
        self.play_pause_btn.set_sensitive(self._snapshot is not None)

    def _on_play_pause_clicked(self, _btn):
        if callable(self._on_play_pause):
            self._on_play_pause()

    def _on_restart_alert_clicked(self, _btn):
        if callable(self._on_restart_alert_toggled):
            self._on_restart_alert_toggled(self.restart_alert_enabled())

    def _on_alert_volume_changed_cb(self, _scale):
        if callable(self._on_alert_volume_changed):
            self._on_alert_volume_changed(self.alert_volume())

    def _on_clear_clicked(self, _btn):
        if callable(self._on_clear):
            self._on_clear()
        else:
            self.clear_server()

    def set_server_snapshot(self, snapshot: dict):
        self._snapshot = dict(snapshot or {})

        name = str(self._snapshot.get("name") or "Unknown Server")
        online = bool(self._snapshot.get("online", False))
        ping = int(self._snapshot.get("ping", -1) or -1)
        players = int(self._snapshot.get("players", 0) or 0)
        max_players = int(self._snapshot.get("max_players", 0) or 0)
        map_name = str(self._snapshot.get("map") or "Unknown")
        time_text = str(self._snapshot.get("time") or "--:--")
        mode_text = str(self._snapshot.get("mode") or "")

        self.name_label.set_text(name)
        self.status_label.set_text("Status: Online" if online else "Status: Offline")
        self.map_label.set_text(f"Map: {map_name}")
        self.players_label.set_text(f"Players: {players}/{max_players}")
        self.ping_label.set_text("Ping: OFFLINE" if ping < 0 else f"Ping: {ping}ms")
        self.time_label.set_text(f"Time: {time_text}")
        self.mode_label.set_text(mode_text)
        self.mode_label.set_visible(bool(mode_text))
        self.empty_label.set_visible(False)
        self.server_box.set_visible(True)
        self.play_pause_btn.set_sensitive(True)

    def clear_server(self):
        self._snapshot = None
        self.server_box.set_visible(False)
        self.empty_label.set_visible(True)
        self.play_pause_btn.set_label("Play/Pause")
        self.play_pause_btn.set_sensitive(False)
