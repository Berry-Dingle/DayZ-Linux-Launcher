#!/usr/bin/env python3
from gi.repository import GLib


def preflight_block_warning_ui_blocking(win, obj) -> bool:
    """
    Run preflight warning on the GTK main thread and block until user responds.
    - If we're already on the GTK main thread: call directly (NO deadlock).
    - If we're on a worker thread: bounce to UI via idle_add and wait.
    """
    try:
        # If current thread owns the default main context, we are on the UI thread.
        try:
            if GLib.MainContext.default().is_owner():
                return bool(win._preflight_block_warning(obj))
        except Exception:
            # If is_owner isn't available for some reason, fall back to direct call.
            # In DZLL, join click is normally on UI thread anyway.
            return bool(win._preflight_block_warning(obj))

        # Worker thread path: marshal to UI thread and wait.
        import threading as _threading
        done = _threading.Event()
        result = {"ok": True}

        def _run_on_ui():
            try:
                result["ok"] = bool(win._preflight_block_warning(obj))
            except Exception:
                result["ok"] = True  # fail-open
            finally:
                done.set()
            return False

        GLib.idle_add(_run_on_ui)
        done.wait()
        return bool(result["ok"])
    except Exception:
        return True  # fail-open


def preflight_block_warning(win, obj) -> bool:
    """
    Returns True to continue, False to abort.
    MUST be called as the FIRST step on Join click (before SteamCMD/mods).
    Warns EVERY time for ip_hard/hard.
    """
    try:
        # We always allow fail-open if blocklist didn't load.
        # But we DO NOT tie join-warnings to the list-filter toggle.
        if not bool(getattr(win, "_bl_ok", False)):
            return True

        key = f"{obj.ip}:{int(obj.gport)}".strip().lower()

        # PRIORITY 1: allow_exact escape hatch (always allowed, no warning)
        if key in getattr(win, "bl_allow_exact", set()):
            return True

        status = win._bl_status(key)  # allowed | ip_hard | hard | soft

        if status in ("ip_hard", "hard"):
            return bool(win._confirm_blocked_server(f"{obj.ip}:{int(obj.gport)}"))

        return True
    except Exception:
        return True