#!/usr/bin/env python3
import subprocess
import threading

from .launcher_user_config import set_launcher_shutdown_mode
from .maps import standardize_map


def launch_direct_steam_url(win, obj):
    ip_port = f"{obj.ip}:{int(obj.gport)}"
    try:
        set_launcher_shutdown_mode(win.settings.get("minimize_dayz_launcher", True))

        cmd = win._steam_launch_prefix()

        # Window/fullscreen toggles (windowed wins if both are set)
        if bool(win.settings.get("windowed_mode", False)):
            cmd.append("-window")
        elif bool(win.settings.get("force_fullscreen", False)):
            cmd.append("-fullscreen")

        if bool(win.settings.get("no_splash", False)):
            cmd.append("-nosplash")

        nm = str(win.settings.get("ingame_name") or "").strip()
        if nm:
            cmd.append(f"-name={nm}")

        extra = str(win.settings.get("additional_launch_params") or "").strip()
        if extra:
            cmd.extend(extra.split())

        cmd.append(f"-connect={ip_port}")

        # Discord: Joining (BEFORE launching)
        try:
            if getattr(win, "_discord", None):
                win._discord.set_joining(server_name=str(obj.name or ""))
        except Exception:
            pass

        # Save info for watcher to apply once the GAME actually starts
        try:
            raw_map = str(getattr(obj, "map_name", "") or getattr(obj, "map", "") or "")
            win._discord_last_join = {
                "server_name": str(obj.name or ""),
                "server_map": standardize_map(raw_map),
                "ip_port": f"{obj.ip}:{int(obj.gport)}",
            }
        except Exception:
            win._discord_last_join = None

        # Launch DayZ via Steam
        subprocess.Popen(cmd)

        # Watch DayZ lifecycle (start -> set playing; exit -> reset)
        try:
            threading.Thread(target=win._discord_watch_dayz_until_exit, daemon=True).start()
        except Exception:
            pass

    except Exception as e:
        print(f"[JOIN] Failed To Launch Steam/DayZ: {e}")

        # Discord: reset back to menus on failure
        try:
            if getattr(win, "_discord", None):
                win._discord.set_menu()
        except Exception:
            pass