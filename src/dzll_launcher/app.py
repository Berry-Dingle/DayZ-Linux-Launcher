#!/usr/bin/env python3
import os
import sys
from pathlib import Path
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .config import APP_ID
from .window import DZLLWindow


class DZLLApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.restart_requested = False

    def request_restart(self):
        self.restart_requested = True
        self.quit()

    def quit(self):
        try:
            w = getattr(self, "window", None)
            if w:
                w._finish_start_steam_join_consent(False, always=False)
        except Exception:
            pass
        return Gtk.Application.quit(self)

    def do_activate(self):
        if not self.window:
            self.window = DZLLWindow(self)
        self.window.present()

    def do_shutdown(self):
        # Cleanly tear down Discord RPC if present
        try:
            w = getattr(self, "window", None)
            if w:
                try:
                    w._shutdown_cleanup()
                except Exception:
                    pass
        except Exception:
            pass

        Gtk.Application.do_shutdown(self)


def main():
    app = DZLLApp()
    original_argv = list(sys.argv)
    status = app.run(original_argv)
    if app.restart_requested:
        args = original_argv[1:]
        if Path(original_argv[0]).name == "__main__.py":
            os.execv(sys.executable, [sys.executable, "-m", "dzll_launcher", *args])
        os.execv(sys.executable, [sys.executable, original_argv[0], *args])
    raise SystemExit(status)


if __name__ == "__main__":
    main()
