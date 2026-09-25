#!/usr/bin/env python3
import logging
import subprocess
import shlex
from dataclasses import dataclass

from .launcher_user_config import set_launcher_shutdown_mode
from .maps import standardize_map
from .server_endpoint import (
    ServerEndpointValidationError,
    normalize_ipv4_address,
    validate_server_port,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SteamLaunchResult:
    submitted: bool
    pid: int | None = None
    error_kind: str = ""
    error: str = ""
    sanitized_command: tuple[str, ...] = ()


def _sanitize_launch_command(cmd) -> tuple[str, ...]:
    safe = []
    for index, arg in enumerate(cmd or []):
        value = str(arg)
        if value.startswith("-name="):
            safe.append("-name=<redacted>")
        elif value.startswith("-mod="):
            count = len([part for part in value[5:].split(";") if part])
            safe.append(f"-mod=<{count} paths>")
        elif value.startswith("-connect=") or value in {
            "-applaunch", "221100", "--", "-nolauncher", "-window", "-fullscreen", "-nosplash"
        } or index == 0:
            safe.append(value)
        else:
            safe.append("<extra-arg>")
    return tuple(safe)


def launch_direct_steam_url(win, obj, mod_win_paths=None, *, popen_factory=None,
                            skip_dayz_launcher=None, before_dispatch=None):
    popen = popen_factory or subprocess.Popen
    try:
        try:
            ip = normalize_ipv4_address(getattr(obj, "ip", None))
            game_port = validate_server_port(
                getattr(obj, "gport", None), field_name="game port"
            )
        except ServerEndpointValidationError as exc:
            logger.error("Refusing Steam launch for invalid server endpoint: %s", exc)
            return SteamLaunchResult(
                False,
                error_kind="invalid_endpoint",
                error=str(exc),
            )
        ip_port = f"{ip}:{game_port}"
        set_launcher_shutdown_mode(win.settings.get("minimize_dayz_launcher", False))

        cmd = win._steam_launch_prefix()

        if skip_dayz_launcher is None:
            skip_dayz_launcher = bool(win.settings.get("skip_dayz_launcher", True))
        else:
            skip_dayz_launcher = bool(skip_dayz_launcher)
        if skip_dayz_launcher:
            cmd.append("-nolauncher")
            if mod_win_paths:
                cmd.append(f"-mod={';'.join(mod_win_paths)}")

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
            try:
                cmd.extend(shlex.split(extra))
            except ValueError as e:
                logger.error("Failed to parse additional launch parameters: %s", e)
                return SteamLaunchResult(False, error_kind="command_construction", error=str(e))

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
                "ip_port": ip_port,
            }
        except Exception:
            win._discord_last_join = None

        # Launch DayZ via Steam
        sanitized = _sanitize_launch_command(cmd)
        if callable(before_dispatch) and not bool(before_dispatch()):
            try:
                if getattr(win, "_discord", None):
                    win._discord.set_menu()
            except Exception:
                pass
            try:
                win._discord_last_join = None
            except Exception:
                pass
            return SteamLaunchResult(
                False,
                error_kind="dispatch_guard",
                error="Join was cancelled or became stale before Steam dispatch.",
                sanitized_command=sanitized,
            )
        try:
            proc = popen(cmd)
        except Exception as e:
            logger.error("Failed to submit Steam launch request: %s", e)
            try:
                if getattr(win, "_discord", None):
                    win._discord.set_menu()
            except Exception:
                pass
            return SteamLaunchResult(False, error_kind="popen", error=str(e), sanitized_command=sanitized)
        return SteamLaunchResult(True,
                                 pid=int(proc.pid) if getattr(proc, "pid", None) is not None else None,
                                 sanitized_command=sanitized)

    except Exception as e:
        logger.error("Failed to launch Steam/DayZ: %s", e)

        # Discord: reset back to menus on failure
        try:
            if getattr(win, "_discord", None):
                win._discord.set_menu()
        except Exception:
            pass
        return SteamLaunchResult(False, error_kind="command_construction", error=str(e))
