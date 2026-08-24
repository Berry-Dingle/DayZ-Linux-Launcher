#!/usr/bin/env python3
import os
import sys
import logging
from pathlib import Path
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from .config import APP_ID
from .window import DZLLWindow

logger = logging.getLogger(__name__)


def _report_startup_exception(message: str, exc: BaseException) -> None:
    """Best-effort diagnostics which can never control startup cleanup."""
    try:
        logger.error(
            message,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return
    except Exception:
        pass
    try:
        print(f"{message}: {type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception:
        pass


class DZLLApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.restart_requested = False
        self._startup_failure_window = None
        self._startup_failed = False

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
        self._destroy_startup_failure_window()
        return Gtk.Application.quit(self)

    def _destroy_startup_failure_window(self) -> None:
        fatal = getattr(self, "_startup_failure_window", None)
        self._startup_failure_window = None
        if fatal is None or not self._window_was_present(fatal, self.get_windows()):
            return
        try:
            fatal.destroy()
        except Exception:
            pass

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        if self._startup_failed:
            fatal = getattr(self, "_startup_failure_window", None)
            if fatal is not None and self._window_was_present(fatal, self.get_windows()):
                try:
                    fatal.present()
                except Exception as exc:
                    self._startup_failure_window = None
                    _report_startup_exception(
                        "Could not re-present the DZLL startup failure window", exc
                    )
                    Gtk.Application.quit(self)
            else:
                self._startup_failure_window = None
                Gtk.Application.quit(self)
            return

        existing_windows = tuple(self.get_windows())
        try:
            candidate = DZLLWindow(self)
        except Exception as exc:
            self._handle_window_startup_failure(existing_windows, exc)
            return

        self.window = candidate
        try:
            candidate.present()
        except Exception as exc:
            self.window = None
            self._cleanup_failed_window(candidate)
            self._handle_window_startup_failure(tuple(self.get_windows()), exc)

    @staticmethod
    def _window_was_present(window, windows) -> bool:
        return any(window is existing for existing in windows)

    def _cleanup_failed_window(self, window) -> None:
        cleanup = getattr(window, "_shutdown_cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception as exc:
                _report_startup_exception(
                    "Failed to clean up a partially constructed DZLL window", exc
                )
        try:
            window.destroy()
        except Exception as exc:
            _report_startup_exception(
                "Failed to destroy a partially constructed DZLL window", exc
            )

    def _handle_window_startup_failure(self, existing_windows, exc: Exception) -> None:
        self.window = None
        self._startup_failed = True
        _report_startup_exception("DZLL window construction failed", exc)
        for window in tuple(self.get_windows()):
            if self._window_was_present(window, existing_windows):
                continue
            self._cleanup_failed_window(window)
        self._present_startup_failure()

    def _present_startup_failure(self) -> None:
        fatal = getattr(self, "_startup_failure_window", None)
        if fatal is not None:
            if self._window_was_present(fatal, self.get_windows()):
                fatal.present()
            else:
                self._startup_failure_window = None
                Gtk.Application.quit(self)
            return
        try:
            fatal = Gtk.ApplicationWindow(
                application=self,
                title="DZLL Startup Error",
            )
            fatal.set_default_size(520, 190)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            box.set_margin_top(24)
            box.set_margin_bottom(24)
            box.set_margin_start(24)
            box.set_margin_end(24)
            message = Gtk.Label(
                label=(
                    "DZLL could not start.\n\n"
                    "Technical details were written to the application log."
                )
            )
            message.set_wrap(True)
            message.set_xalign(0.0)
            button = Gtk.Button(label="Close")
            button.set_halign(Gtk.Align.END)
            box.append(message)
            box.append(button)
            fatal.set_child(box)
            self._startup_failure_window = fatal
            button.connect("clicked", lambda *_: self._dismiss_startup_failure(fatal))
            fatal.connect("close-request", self._on_startup_failure_close_requested)
            fatal.connect("unrealize", self._on_startup_failure_unrealized)
            fatal.present()
        except Exception as exc:
            self._startup_failure_window = None
            _report_startup_exception(
                "Could not present the DZLL startup failure window", exc
            )
            Gtk.Application.quit(self)

    def _dismiss_startup_failure(self, fatal) -> None:
        if fatal is not getattr(self, "_startup_failure_window", None):
            return
        self._startup_failure_window = None
        try:
            fatal.destroy()
        finally:
            Gtk.Application.quit(self)

    def _on_startup_failure_close_requested(self, fatal, *_args):
        if fatal is getattr(self, "_startup_failure_window", None):
            self._startup_failure_window = None
            Gtk.Application.quit(self)
        return False

    def _on_startup_failure_unrealized(self, fatal, *_args):
        if fatal is getattr(self, "_startup_failure_window", None):
            self._startup_failure_window = None
            Gtk.Application.quit(self)

    def do_shutdown(self):
        self._destroy_startup_failure_window()
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
