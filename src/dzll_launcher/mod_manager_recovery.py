"""Asynchronous-worker helpers for safe Mod Manager Steam recovery."""

from __future__ import annotations

import shutil
import subprocess
import time

from .steam_native import (
    NativeSteamLaunchResult,
    launch_native_steam_silent_tracked,
    native_steam_ready_for_mod_manager,
    resolve_steam_runtime_state,
)
from .steam_ugc_backend import probe_native_mod_manager_readiness
FLATPAK_STEAM_APP_ID = "com.valvesoftware.Steam"
STEAM_CLOSE_TIMEOUT_S = 75.0
STEAM_READY_TIMEOUT_S = 300.0
STEAM_RECOVERY_POLL_S = 0.5
STEAM_READY_PROBE_TIMEOUT_S = 8.0
STEAM_READY_SETTLE_S = 1.0
STEAM_READY_MATCHES_REQUIRED = 2


def request_graceful_flatpak_steam_shutdown(runtime=None) -> list[str]:
    """Ask a verified Flatpak Steam main client to shut down normally."""
    errors: list[str] = []
    if runtime is None:
        try:
            runtime = resolve_steam_runtime_state()
        except Exception as exc:
            return [f"Could not inspect running Steam clients: {exc}"]

    if runtime.flatpak_client_running:
        flatpak_cmd = shutil.which("flatpak")
        if not flatpak_cmd:
            errors.append("Flatpak Steam is running, but the Flatpak command was not found.")
        else:
            try:
                subprocess.run(
                    [flatpak_cmd, "run", FLATPAK_STEAM_APP_ID, "-shutdown"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except Exception as exc:
                errors.append(f"Could not request Flatpak Steam shutdown: {exc}")
    return errors


def wait_for_flatpak_steam_exit(
    cancel_event,
    *,
    timeout_s: float = STEAM_CLOSE_TIMEOUT_S,
) -> tuple[bool, str]:
    """Wait for current Flatpak Steam process evidence to disappear.

    Shutdown is deliberately not reissued here: residual container evidence is
    exit authority, but is not authority to run ``flatpak ... -shutdown``
    because that command can relaunch an already-closed client.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        if cancel_event.is_set():
            return False, "cancelled"
        try:
            runtime = resolve_steam_runtime_state()
        except Exception:
            runtime = None
        if runtime is not None and not runtime.flatpak_process_running:
            return True, ""
        if time.monotonic() >= deadline:
            return False, "Flatpak Steam is still running. Close it, then Retry."
        cancel_event.wait(STEAM_RECOVERY_POLL_S)


def wait_for_native_mod_manager_ready(
    cancel_event,
    *,
    timeout_s: float = STEAM_READY_TIMEOUT_S,
    launched_pid: int = 0,
) -> tuple[bool, str]:
    """Wait for stable native process, login, identity, and UGC authority."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    previous_steam_id = 0
    matching_ready_checks = 0
    while time.monotonic() < deadline:
        if cancel_event.is_set():
            return False, "cancelled"
        try:
            runtime = resolve_steam_runtime_state()
        except Exception:
            runtime = None
        if runtime is None or not native_steam_ready_for_mod_manager(runtime):
            if runtime is not None and runtime.flatpak_process_running:
                return (
                    False,
                    "Native Steam could not be started.\n"
                    "Flatpak Steam opened instead.",
                )
            previous_steam_id = 0
            matching_ready_checks = 0
            cancel_event.wait(STEAM_RECOVERY_POLL_S)
            continue
        remaining = max(0.1, deadline - time.monotonic())
        readiness = probe_native_mod_manager_readiness(
            timeout=min(STEAM_READY_PROBE_TIMEOUT_S, remaining),
            cancel_event=cancel_event,
        )
        if cancel_event.is_set():
            return False, "cancelled"
        try:
            end_runtime = resolve_steam_runtime_state()
        except Exception:
            end_runtime = None
        if (
            readiness.valid
            and end_runtime is not None
            and native_steam_ready_for_mod_manager(end_runtime)
        ):
            if int(readiness.steam_id) == previous_steam_id:
                matching_ready_checks += 1
            else:
                previous_steam_id = int(readiness.steam_id)
                matching_ready_checks = 1
            if matching_ready_checks >= STEAM_READY_MATCHES_REQUIRED:
                return True, ""
        else:
            previous_steam_id = 0
            matching_ready_checks = 0
        delay = min(STEAM_READY_SETTLE_S, max(0.0, deadline - time.monotonic()))
        if delay > 0:
            cancel_event.wait(delay)
    return (
        False,
        "Native Steam is not ready. Sign in, then Retry.",
    )


def launch_native_steam() -> NativeSteamLaunchResult:
    return launch_native_steam_silent_tracked()
