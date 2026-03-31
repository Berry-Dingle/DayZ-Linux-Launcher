#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .config import APP_ID
from .window import DZLLWindow


class DZLLApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = DZLLWindow(self)
        self.window.present()

    def do_shutdown(self):
        # Cleanly tear down Discord RPC if present
        try:
            w = getattr(self, "window", None)
            if w and getattr(w, "_discord", None):
                try:
                    w._discord.clear()
                except Exception:
                    pass
                try:
                    w._discord.disconnect()
                except Exception:
                    pass
        except Exception:
            pass

        Gtk.Application.do_shutdown(self)


def main():
    app = DZLLApp()
    raise SystemExit(app.run(None))


if __name__ == "__main__":
    main()