"""Docking and undocking of the Server Companion panel.

The panel is shown either docked in the main window's revealer or alone in
a separate window. This controller owns that state, the reparenting between
the two, and the delayed main-window shrink after an undock. Everything runs
on the GTK main thread.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from .config import SIDEBAR_WIDTH, WINDOW_DEFAULT_SIZE
from .server_companion_ui import ServerCompanionPanel

DEBUG_SC_DOCK = os.environ.get("DZLL_DEBUG_SC_DOCK") == "1"
SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS = 200


class ServerCompanionDockController:
    """Owns dock state and moves the Companion panel between the revealer and its own window.

    Widgets are read through accessors because the window creates the panel,
    revealer and browser box after this controller and may lack them during
    construction failure. Window geometry and the Companion callbacks are
    injected as callables.
    """

    def __init__(
        self,
        *,
        panel: Callable[[], Any],
        revealer: Callable[[], Any],
        main_browser_box: Callable[[], Any],
        get_application: Callable[[], Any],
        get_window_height: Callable[[], int],
        set_window_default_size: Callable[[int, int], None],
        is_enabled: Callable[[], bool],
        start_polling: Callable[[], None],
        refresh_power_controls: Callable[[], None],
        refresh_monitor_highlight: Callable[[], None],
    ) -> None:
        self._panel = panel
        self._revealer = revealer
        self._main_browser_box = main_browser_box
        self._get_application = get_application
        self._get_window_height = get_window_height
        self._set_window_default_size = set_window_default_size
        self._is_enabled = is_enabled
        self._start_polling = start_polling
        self._refresh_power_controls = refresh_power_controls
        self._refresh_monitor_highlight = refresh_monitor_highlight
        self.docked = True
        self.undocked_window = None
        self.undocked_close_handler_id = 0
        self.reparenting = False
        self.shrink_source_id = 0

    def _debug(self, message: str):
        if not DEBUG_SC_DOCK:
            return
        try:
            print(f"[SC-DOCK] {message}", flush=True)
        except Exception:
            pass

    def _expand_dock_space(self) -> None:
        self._main_browser_box().set_size_request(int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1)

    def toggle(self):
        self._debug("dock toggle callback invoked")
        if bool(self.docked):
            self.undock()
        else:
            self.dock()

    def _recover_after_undock_failure(self, panel=None, revealer=None, failed_window=None, error=None):
        print(f"[SC-DOCK] WARNING: undock failed; restoring docked Server Companion: {error!r}", flush=True)
        panel = panel or self._panel()
        revealer = revealer or self._revealer()
        win = failed_window or self.undocked_window

        if win is not None:
            handler_id = int(self.undocked_close_handler_id or 0)
            if handler_id:
                try:
                    win.disconnect(handler_id)
                except Exception:
                    pass
            try:
                win.set_child(None)
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        self.undocked_window = None
        self.undocked_close_handler_id = 0
        self.docked = True
        if panel is not None:
            try:
                panel.set_docked(True)
            except Exception:
                pass
        if panel is not None and revealer is not None:
            try:
                revealer.set_child(panel)
            except Exception as exc:
                print(f"[SC-DOCK] WARNING: failed to reattach Server Companion panel: {exc!r}", flush=True)
            if self._is_enabled():
                try:
                    revealer.set_visible(True)
                    revealer.set_reveal_child(True)
                    self._expand_dock_space()
                except Exception:
                    pass
        self._refresh_monitor_highlight()
        self._refresh_power_controls()

    def undock(self):
        self._debug("starting undock")
        if not self._is_enabled():
            self._debug("undock skipped: Server Companion disabled")
            return
        if not bool(self.docked):
            win = self.undocked_window
            if win is not None:
                try:
                    self._debug("undock requested while already undocked; presenting existing window")
                    win.present()
                except Exception:
                    pass
            return

        panel = self._panel()
        revealer = self._revealer()
        if panel is None or revealer is None:
            self._debug("undock skipped: missing panel or revealer")
            return

        self.reparenting = True
        win = None
        try:
            revealer.set_reveal_child(False)
            revealer.set_child(None)
            revealer.set_visible(False)

            win = Gtk.Window(application=self._get_application(), title="DZLL Server Companion")
            self._debug("detached window created")
            win.set_resizable(False)
            win.set_child(panel)
            handler_id = win.connect("close-request", self.on_undocked_close_request)

            self.undocked_window = win
            self.undocked_close_handler_id = handler_id
            self.docked = False
            panel.set_docked(False)
            self.update_undocked_size()
            win.present()
            self._debug("present called")
            self.schedule_post_undock_shrink()
            self._start_polling()
        except Exception as exc:
            self._debug(f"undock exception/fallback: {exc!r}")
            self._recover_after_undock_failure(panel=panel, revealer=revealer, failed_window=win, error=exc)
        finally:
            self.reparenting = False
            self._refresh_power_controls()

    def dock(self):
        self._debug("docking/reattaching")
        self.cancel_post_undock_shrink()
        if bool(self.docked):
            self._debug("dock skipped: already docked")
            self._refresh_power_controls()
            return

        panel = self._panel()
        revealer = self._revealer()
        if panel is None or revealer is None:
            self._debug("dock skipped: missing panel or revealer")
            return

        self.destroy_undocked_window(reattach=False)
        self.reparenting = True
        try:
            revealer.set_child(panel)
            self.docked = True
            panel.set_docked(True)
            if self._is_enabled():
                revealer.set_visible(True)
                revealer.set_reveal_child(True)
                try:
                    self._expand_dock_space()
                except Exception:
                    pass
            else:
                revealer.set_reveal_child(False)
                revealer.set_visible(False)
        finally:
            self.reparenting = False
            self._refresh_power_controls()

    def cancel_post_undock_shrink(self) -> None:
        source_id = int(self.shrink_source_id or 0)
        self.shrink_source_id = 0
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

    def schedule_post_undock_shrink(self) -> None:
        if int(self.shrink_source_id or 0):
            return
        try:
            self.shrink_source_id = GLib.timeout_add(
                SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS,
                self.apply_post_undock_shrink,
            )
        except Exception:
            self.shrink_source_id = 0

    def apply_post_undock_shrink(self) -> bool:
        self.shrink_source_id = 0
        if bool(self.docked):
            return False
        if not self._is_enabled():
            return False
        if self.undocked_window is None:
            return False
        self.collapse_dock_space()
        return False

    def collapse_dock_space(self):
        try:
            self._main_browser_box().set_size_request(-1, -1)
        except Exception:
            pass
        try:
            width = int(WINDOW_DEFAULT_SIZE[0])
            try:
                height = int(self._get_window_height())
            except Exception:
                height = int(WINDOW_DEFAULT_SIZE[1])
            if height <= 0:
                height = int(WINDOW_DEFAULT_SIZE[1])
            self._set_window_default_size(width, height)
        except Exception:
            pass

    def on_undocked_close_request(self, *_args):
        if bool(self.reparenting):
            return False
        self._debug("undocked window close requested")
        self.dock()
        return True

    def update_undocked_size(self) -> None:
        if bool(self.docked):
            return
        win = self.undocked_window
        panel = self._panel()
        if win is None or panel is None:
            return
        height = (
            ServerCompanionPanel.UNDOCKED_EXPANDED_HEIGHT
            if panel.restart_learning_visible()
            else ServerCompanionPanel.UNDOCKED_COMPACT_HEIGHT
        )
        try:
            win.set_default_size(ServerCompanionPanel.WIDTH, height)
            win.set_size_request(ServerCompanionPanel.WIDTH, height)
            win.queue_resize()
        except Exception:
            pass

    def destroy_undocked_window(self, reattach: bool = True):
        self._debug(f"destroy undocked window reattach={bool(reattach)}")
        win = self.undocked_window
        panel = self._panel()
        revealer = self._revealer()

        if win is not None:
            handler_id = int(self.undocked_close_handler_id or 0)
            if handler_id:
                try:
                    win.disconnect(handler_id)
                except Exception:
                    pass
            try:
                win.set_child(None)
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        self.undocked_window = None
        self.undocked_close_handler_id = 0
        self.docked = True
        if panel is not None:
            panel.set_docked(True)
        if reattach and panel is not None and revealer is not None:
            try:
                revealer.set_child(panel)
            except Exception:
                pass
