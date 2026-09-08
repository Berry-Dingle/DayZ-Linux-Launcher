#!/usr/bin/env python3
# settings_ui.py
#
# Settings panel UI extracted from main.py with zero behavior changes.
# (Updated per requested ordering + new launch toggle + tooltips + dimming autodetected fields.)

import os
import threading
import time
from enum import IntEnum
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango, GLib, Gio

from .config import IMAGES_DIR, APP_VERSION, RELEASES_URL
from .settings import save_settings, reset_settings, autodetect_workshop_dir
from .storage import save_last_played
from .ui_row import hr, attach_pointer_cursor
from .mods_ui import ModsManagerOverlay
from .mod_manager_recovery import (
    launch_native_steam,
    request_graceful_flatpak_steam_shutdown,
    wait_for_flatpak_steam_exit,
    wait_for_native_mod_manager_ready,
)
from .steam_native import (
    SteamClientState,
    native_steam_ready_for_mod_manager,
    resolve_steam_runtime_state,
)
from .steam_ugc_backend import close_active_ugc_sessions

RECOVERY_DIALOG_WIDTH = 520
RECOVERY_INVENTORY_ATTEMPTS = 3
RECOVERY_INVENTORY_RETRY_DELAY_MS = 1500


class ModManagerRecoveryPhase(IntEnum):
    IDLE = 0
    CLOSING_STEAM = 10
    RESETTING = 20
    STARTING_NATIVE = 30
    WAITING_FOR_NATIVE = 40
    PREPARING_INVENTORY = 50
    OPENING_MOD_MANAGER = 60
    COMPLETE = 70
    FAILED = 80

class SettingsUI:
    def __init__(self, window):
        self._win = window
        self._mods_manager_entry_check_running = False
        self._mods_manager_flatpak_warning = None
        self._mods_manager_recovery_running = False
        self._mods_manager_recovery_generation = 0
        self._mods_manager_recovery_cancel_event = None
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.IDLE
        self._mods_manager_recovery_retry_source_id = 0
        self._mods_manager_recovery_launch_generation = 0
        self._mods_manager_gate_mode = ""
        self._mods_manager_recheck_running = False
        self._mods_manager_recheck_generation = 0
        self._mods_manager_recheck_cancel_event = None
        try:
            self._mods_manager_recovery_close_handler_id = int(
                window.connect("close-request", self._on_recovery_host_close) or 0
            )
        except Exception:
            self._mods_manager_recovery_close_handler_id = 0

    def open_mods_manager(self):
        self._start_mod_manager_entry_check()

    def _start_mod_manager_entry_check(self) -> bool:
        if bool(getattr(self, "_mods_manager_entry_check_running", False)):
            return False
        self._mods_manager_entry_check_running = True

        def worker():
            try:
                runtime = resolve_steam_runtime_state()
            except Exception:
                runtime = None
            GLib.idle_add(
                self._complete_mod_manager_entry_check,
                runtime,
            )

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _complete_mod_manager_entry_check(self, runtime):
        self._mods_manager_entry_check_running = False
        if bool(getattr(runtime, "flatpak_process_running", False)):
            if self._runtime_has_native_processes(runtime):
                self._show_mixed_steam_mod_manager_gate()
            else:
                self._show_flatpak_mod_manager_recovery()
            return False
        self._hide_flatpak_mod_manager_recovery()
        self._open_mods_manager_after_gate(
            verified_native=bool(
                runtime is not None
                and native_steam_ready_for_mod_manager(runtime)
            ),
        )
        return False

    @staticmethod
    def _runtime_has_native_processes(runtime) -> bool:
        return bool(
            runtime is not None
            and (
                getattr(runtime, "native_client_running", False)
                or getattr(runtime, "native_helper_running", False)
            )
        )

    def _open_mods_manager_after_gate(self, *, verified_native: bool = False):
        try:
            manager = self._ensure_mods_manager_overlay()
            if manager is None:
                return
            if verified_native:
                manager.accept_recovered_native_authority()

            def _ui_open():
                try:
                    manager.refresh()
                except Exception:
                    pass
                try:
                    manager.show()
                except Exception:
                    pass
                return False

            GLib.idle_add(_ui_open)

        except Exception as e:
            print(f"[MODS UI] Failed to open mods manager: {e}")

    def _ensure_mods_manager_overlay(self):
        existing = getattr(self, "_mods_mgr_overlay", None)
        if existing is not None:
            return existing
        ov = getattr(self, "_main_overlay", None)
        if ov is None:
            ov = getattr(self._win, "_main_overlay", None)
        if ov is None:
            print("[MODS UI] No main overlay found on settings host.")
            return None
        self._mods_mgr_overlay = ModsManagerOverlay(self, ov)
        return self._mods_mgr_overlay

    def _ensure_flatpak_mod_manager_warning(self):
        existing = getattr(self, "_mods_manager_flatpak_warning", None)
        if existing is not None:
            return existing
        overlay = getattr(self, "_main_overlay", None)
        if overlay is None:
            overlay = getattr(self._win, "_main_overlay", None)
        if overlay is None:
            return None

        scrim = Gtk.Box()
        scrim.set_hexpand(True)
        scrim.set_vexpand(True)
        scrim.set_visible(False)
        scrim.set_can_target(True)
        scrim.add_css_class("settings-scrim")
        overlay.add_overlay(scrim)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_visible(False)
        card.set_can_target(True)
        card.set_size_request(RECOVERY_DIALOG_WIDTH, -1)
        card.add_css_class("warning-card")
        overlay.add_overlay(card)

        title = Gtk.Label(label="Flatpak Steam detected")
        title.set_xalign(0.0)
        title.set_wrap(True)
        title.add_css_class("steamcmd-heading")
        title.add_css_class("confirmation-title")
        card.append(title)

        message = Gtk.Label(
            label=self._flatpak_recovery_intro_text()
        )
        message.set_xalign(0.0)
        message.set_wrap(True)
        message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        message.set_max_width_chars(60)
        message.add_css_class("confirmation-body")
        card.append(message)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(12)
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.add_css_class("warning-btn")
        attach_pointer_cursor(cancel_button)
        buttons.append(cancel_button)
        continue_button = Gtk.Button(label="Continue")
        continue_button.add_css_class("suggested-action")
        continue_button.add_css_class("warning-btn")
        attach_pointer_cursor(continue_button)
        buttons.append(continue_button)
        card.append(buttons)

        warning = {
            "scrim": scrim,
            "card": card,
            "title": title,
            "message": message,
            "cancel_button": cancel_button,
            "continue_button": continue_button,
        }
        self._mods_manager_flatpak_warning = warning
        cancel_button.connect(
            "clicked", lambda *_: self._cancel_flatpak_mod_manager_recovery(),
        )
        continue_button.connect(
            "clicked", lambda *_: self._activate_mod_manager_gate_primary(),
        )
        return warning

    @staticmethod
    def _flatpak_recovery_intro_text() -> str:
        return "DZLL will close Flatpak Steam and start native Steam."

    @staticmethod
    def _mixed_steam_gate_text() -> str:
        return (
            "DZLL cannot safely manage Workshop mods while both Steam clients "
            "are running.\n\n"
            "Close both Steam clients, start native Steam only, then click Recheck."
        )

    def _show_flatpak_mod_manager_recovery(
        self,
        *,
        message: str | None = None,
        retry: bool = False,
    ):
        if message is None and (
            bool(getattr(self, "_mods_manager_recovery_running", False))
            or getattr(self, "_mods_manager_recovery_phase", None)
            is ModManagerRecoveryPhase.FAILED
        ):
            return False
        warning = self._ensure_flatpak_mod_manager_warning()
        if warning is None:
            return False
        self._mods_manager_gate_mode = "flatpak"
        warning["title"].set_text("Flatpak Steam detected")
        warning["message"].set_text(
            str(message) if message else self._flatpak_recovery_intro_text()
        )
        warning["continue_button"].set_label("Retry" if retry else "Continue")
        warning["continue_button"].set_sensitive(
            not bool(getattr(self, "_mods_manager_recovery_running", False))
        )
        warning["cancel_button"].set_sensitive(True)
        warning["scrim"].set_visible(True)
        warning["card"].set_visible(True)
        return False

    def _show_mixed_steam_mod_manager_gate(self, *, message: str | None = None):
        warning = self._ensure_flatpak_mod_manager_warning()
        if warning is None:
            return False
        self._mods_manager_gate_mode = "mixed"
        warning["title"].set_text("Both Steam clients detected")
        warning["message"].set_text(
            str(message) if message else self._mixed_steam_gate_text()
        )
        warning["continue_button"].set_label("Recheck")
        warning["continue_button"].set_sensitive(
            not bool(getattr(self, "_mods_manager_recheck_running", False))
        )
        warning["cancel_button"].set_sensitive(True)
        warning["scrim"].set_visible(True)
        warning["card"].set_visible(True)
        return False

    def _activate_mod_manager_gate_primary(self):
        if getattr(self, "_mods_manager_gate_mode", "") == "mixed":
            return self._start_mixed_mod_manager_recheck()
        return self._start_flatpak_mod_manager_recovery()

    def _set_flatpak_recovery_progress(self, message: str):
        warning = getattr(self, "_mods_manager_flatpak_warning", None)
        if warning is None:
            return False
        warning["message"].set_text(str(message or ""))
        warning["continue_button"].set_label("Continue")
        warning["continue_button"].set_sensitive(False)
        warning["cancel_button"].set_sensitive(True)
        return False

    def _hide_flatpak_mod_manager_recovery(self):
        warning = getattr(self, "_mods_manager_flatpak_warning", None)
        if warning is None:
            return False
        warning["scrim"].set_visible(False)
        warning["card"].set_visible(False)
        self._mods_manager_gate_mode = ""
        warning["continue_button"].set_label("Continue")
        warning["continue_button"].set_sensitive(True)
        warning["cancel_button"].set_sensitive(True)
        return False

    def _recovery_callback_is_current(self, generation: int, cancel_event) -> bool:
        return bool(
            int(generation) == int(
                getattr(self, "_mods_manager_recovery_generation", 0) or 0
            )
            and cancel_event is getattr(
                self, "_mods_manager_recovery_cancel_event", None
            )
            and not cancel_event.is_set()
        )

    def _advance_flatpak_recovery_phase(
        self,
        generation: int,
        cancel_event,
        phase: ModManagerRecoveryPhase,
        message: str | None = None,
    ) -> bool:
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        next_phase = ModManagerRecoveryPhase(phase)
        current = ModManagerRecoveryPhase(
            getattr(
                self, "_mods_manager_recovery_phase",
                ModManagerRecoveryPhase.IDLE,
            )
        )
        if next_phase < current:
            return False
        self._mods_manager_recovery_phase = next_phase
        if message is not None:
            self._set_flatpak_recovery_progress(message)
        return True

    def _clear_flatpak_recovery_retry_timer(self) -> None:
        source_id = int(
            getattr(self, "_mods_manager_recovery_retry_source_id", 0) or 0
        )
        self._mods_manager_recovery_retry_source_id = 0
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

    def _start_mixed_mod_manager_recheck(self) -> bool:
        """Reset DZLL state and re-detect; never control either Steam client."""
        if bool(getattr(self, "_mods_manager_recheck_running", False)):
            return False
        if bool(getattr(self, "_mods_manager_recovery_running", False)):
            return False
        self._mods_manager_recheck_generation = int(
            getattr(self, "_mods_manager_recheck_generation", 0) or 0
        ) + 1
        generation = self._mods_manager_recheck_generation
        cancel_event = threading.Event()
        self._mods_manager_recheck_cancel_event = cancel_event
        self._mods_manager_recheck_running = True
        warning = self._ensure_flatpak_mod_manager_warning()
        if warning is not None:
            warning["continue_button"].set_sensitive(False)

        manager = getattr(self, "_mods_mgr_overlay", None)
        inventory_idle = None
        if manager is not None:
            try:
                inventory_idle = manager.reset_for_steam_recovery()
            except Exception:
                inventory_idle = None

        def worker():
            if inventory_idle is not None:
                deadline = time.monotonic() + 20.0
                while not inventory_idle.wait(0.1):
                    if cancel_event.is_set():
                        return
                    if time.monotonic() >= deadline:
                        GLib.idle_add(
                            self._complete_mixed_mod_manager_recheck,
                            generation,
                            cancel_event,
                            None,
                        )
                        return
            if cancel_event.is_set():
                return
            try:
                runtime = resolve_steam_runtime_state()
            except Exception:
                runtime = None
            GLib.idle_add(
                self._complete_mixed_mod_manager_recheck,
                generation,
                cancel_event,
                runtime,
            )

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _complete_mixed_mod_manager_recheck(
        self, generation: int, cancel_event, runtime,
    ):
        if (
            int(generation) != int(
                getattr(self, "_mods_manager_recheck_generation", 0) or 0
            )
            or cancel_event is not getattr(
                self, "_mods_manager_recheck_cancel_event", None
            )
            or cancel_event.is_set()
        ):
            return False
        if runtime is None:
            self._finish_mixed_mod_manager_recheck(cancel_event)
            return self._show_mixed_steam_mod_manager_gate()
        if bool(getattr(runtime, "flatpak_process_running", False)):
            self._finish_mixed_mod_manager_recheck(cancel_event)
            if self._runtime_has_native_processes(runtime):
                return self._show_mixed_steam_mod_manager_gate()
            return self._show_mixed_steam_mod_manager_gate(
                message=(
                    "Flatpak Steam is still running.\n\n"
                    "Close Flatpak Steam, start native Steam only, then click "
                    "Recheck."
                ),
            )
        self._finish_mixed_mod_manager_recheck(cancel_event)
        self._hide_flatpak_mod_manager_recovery()
        self._open_mods_manager_after_gate(
            verified_native=bool(
                runtime is not None
                and native_steam_ready_for_mod_manager(runtime)
            ),
        )
        return False

    def _finish_mixed_mod_manager_recheck(self, cancel_event) -> None:
        cancel_event.set()
        self._mods_manager_recheck_generation = int(
            getattr(self, "_mods_manager_recheck_generation", 0) or 0
        ) + 1
        self._mods_manager_recheck_running = False
        self._mods_manager_recheck_cancel_event = None

    def _start_flatpak_mod_manager_recovery(self) -> bool:
        if bool(getattr(self, "_mods_manager_recovery_running", False)):
            return False
        self._mods_manager_recovery_generation = int(
            getattr(self, "_mods_manager_recovery_generation", 0) or 0
        ) + 1
        generation = self._mods_manager_recovery_generation
        cancel_event = threading.Event()
        self._mods_manager_recovery_cancel_event = cancel_event
        self._mods_manager_recovery_running = True
        self._clear_flatpak_recovery_retry_timer()
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.CLOSING_STEAM
        self._set_flatpak_recovery_progress("Closing Steam…")

        def status(
            phase: ModManagerRecoveryPhase, message: str | None = None,
        ) -> None:
            GLib.idle_add(
                self._apply_flatpak_recovery_status,
                generation,
                cancel_event,
                phase,
                message,
            )

        def worker() -> None:
            try:
                try:
                    initial_runtime = resolve_steam_runtime_state()
                except Exception:
                    initial_runtime = None
                if initial_runtime is None:
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        "DZLL could not check Steam. Retry.",
                    )
                    return
                if (
                    initial_runtime.flatpak_process_running
                    and self._runtime_has_native_processes(initial_runtime)
                ):
                    GLib.idle_add(
                        self._switch_flatpak_recovery_to_mixed_gate,
                        generation,
                        cancel_event,
                    )
                    return
                if (
                    not initial_runtime.flatpak_process_running
                    and self._runtime_has_native_processes(initial_runtime)
                ):
                    GLib.idle_add(
                        self._finish_flatpak_recovery_to_normal_entry,
                        generation,
                        cancel_event,
                        initial_runtime,
                    )
                    return
                if initial_runtime.flatpak_process_running:
                    shutdown_errors = request_graceful_flatpak_steam_shutdown(
                        initial_runtime
                    )
                    if cancel_event.is_set():
                        return
                    exited, exit_error = wait_for_flatpak_steam_exit(cancel_event)
                    if not exited:
                        if exit_error != "cancelled":
                            detail = exit_error or (
                                "Flatpak Steam is still running. Close it, then Retry."
                            )
                            if shutdown_errors:
                                print(
                                    "[MOD MANAGER] Steam shutdown diagnostic: "
                                    f"{shutdown_errors[0]}",
                                    flush=True,
                                )
                            GLib.idle_add(
                                self._finish_flatpak_recovery_failure,
                                generation,
                                cancel_event,
                                detail,
                            )
                        return
                    if cancel_event.is_set():
                        return

                status(ModManagerRecoveryPhase.RESETTING)
                reset_done = threading.Event()
                inventory_idle = []
                GLib.idle_add(
                    self._apply_flatpak_recovery_reset,
                    generation,
                    cancel_event,
                    reset_done,
                    inventory_idle,
                )
                while not reset_done.wait(0.1):
                    if cancel_event.is_set():
                        return
                if cancel_event.is_set():
                    return
                helper_errors = close_active_ugc_sessions()
                if helper_errors:
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        "DZLL could not reset Steam. Retry.",
                    )
                    return
                idle_event = inventory_idle[0] if inventory_idle else None
                idle_deadline = time.monotonic() + 20.0
                while idle_event is not None and not idle_event.wait(0.1):
                    if cancel_event.is_set():
                        return
                    if time.monotonic() >= idle_deadline:
                        GLib.idle_add(
                            self._finish_flatpak_recovery_failure,
                            generation,
                            cancel_event,
                            "DZLL could not stop its Steam check. Retry.",
                        )
                        return

                if cancel_event.is_set():
                    return
                try:
                    prelaunch_runtime = resolve_steam_runtime_state()
                except Exception:
                    prelaunch_runtime = None
                if (
                    prelaunch_runtime is None
                    or prelaunch_runtime.state is not SteamClientState.OFFLINE
                    or prelaunch_runtime.native_client_running
                    or prelaunch_runtime.native_helper_running
                    or prelaunch_runtime.flatpak_process_running
                ):
                    message = (
                        "Native Steam could not be started.\n"
                        "Flatpak Steam opened instead."
                        if prelaunch_runtime is not None
                        and prelaunch_runtime.flatpak_process_running
                        else "Steam started again before recovery completed. Retry."
                    )
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        message,
                    )
                    return
                status(
                    ModManagerRecoveryPhase.STARTING_NATIVE,
                    "Starting native Steam…",
                )
                if int(getattr(
                    self, "_mods_manager_recovery_launch_generation", 0,
                ) or 0) == int(generation):
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        "Native Steam could not be started. Retry.",
                    )
                    return
                self._mods_manager_recovery_launch_generation = int(generation)
                launch_result = launch_native_steam()
                launched = bool(getattr(launch_result, "ok", False))
                launch_error = str(getattr(launch_result, "error", "") or "")
                launched_pid = int(getattr(launch_result, "pid", 0) or 0)
                launched_executable = str(
                    getattr(launch_result, "executable", "") or ""
                )
                # Compatibility for deterministic doubles retained by older
                # tests and third-party integrations.
                if isinstance(launch_result, tuple):
                    launched = bool(launch_result[0]) if launch_result else False
                    launch_error = (
                        str(launch_result[1] or "")
                        if len(launch_result) > 1 else ""
                    )
                    launched_pid = 0
                    launched_executable = ""
                if not launched:
                    if launch_error:
                        print(
                            "[MOD MANAGER] Native Steam launch diagnostic: "
                            f"{launch_error}",
                            flush=True,
                        )
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        "Native Steam could not start. Check it is installed, then Retry.",
                    )
                    return
                status(
                    ModManagerRecoveryPhase.WAITING_FOR_NATIVE,
                    "Waiting for Steam login…",
                )
                ready, ready_error = wait_for_native_mod_manager_ready(
                    cancel_event, launched_pid=launched_pid,
                )
                if not ready:
                    if ready_error != "cancelled":
                        GLib.idle_add(
                            self._finish_flatpak_recovery_failure,
                            generation,
                            cancel_event,
                            ready_error,
                        )
                    return
                status(ModManagerRecoveryPhase.PREPARING_INVENTORY)
                GLib.idle_add(
                    self._begin_flatpak_recovery_inventory_handoff,
                    generation,
                    cancel_event,
                    1,
                )
            except Exception:
                if not cancel_event.is_set():
                    GLib.idle_add(
                        self._finish_flatpak_recovery_failure,
                        generation,
                        cancel_event,
                        "Steam recovery failed. Close Steam, then Retry.",
                    )
            finally:
                GLib.idle_add(
                    self._flatpak_recovery_worker_exited,
                    cancel_event,
                )

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _apply_flatpak_recovery_status(
        self,
        generation: int,
        cancel_event,
        phase: ModManagerRecoveryPhase,
        message: str | None,
    ):
        self._advance_flatpak_recovery_phase(
            generation, cancel_event, phase, message,
        )
        return False

    def _apply_flatpak_recovery_reset(
        self, generation: int, cancel_event, reset_done, inventory_idle,
    ):
        try:
            if self._recovery_callback_is_current(generation, cancel_event):
                win = getattr(self, "_win", None)
                if win is not None:
                    old_cancel = getattr(win, "_steamcmd_cancel_event", None)
                    if old_cancel is not None:
                        try:
                            old_cancel.set()
                        except Exception:
                            pass
                    try:
                        win._steamcmd_cancel_event = threading.Event()
                    except Exception:
                        pass
                    controller = getattr(
                        win, "_background_prepare_controller", None
                    )
                    if controller is not None:
                        try:
                            controller.cancel()
                        except Exception:
                            pass
                    try:
                        win._background_prepare_ui_generation = int(
                            getattr(win, "_background_prepare_ui_generation", 0)
                            or 0
                        ) + 1
                    except Exception:
                        pass
                manager = getattr(self, "_mods_mgr_overlay", None)
                if manager is not None:
                    inventory_idle.append(manager.reset_for_steam_recovery())
        finally:
            reset_done.set()
        return False

    def _finish_flatpak_recovery_failure(
        self, generation: int, cancel_event, message: str,
    ):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        self._clear_flatpak_recovery_retry_timer()
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.FAILED
        self._mods_manager_recovery_running = False
        self._show_flatpak_mod_manager_recovery(message=message, retry=True)
        return False

    def _switch_flatpak_recovery_to_mixed_gate(
        self, generation: int, cancel_event,
    ):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        cancel_event.set()
        self._clear_flatpak_recovery_retry_timer()
        self._mods_manager_recovery_running = False
        self._mods_manager_recovery_cancel_event = None
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.IDLE
        self._show_mixed_steam_mod_manager_gate()
        return False

    def _finish_flatpak_recovery_to_normal_entry(
        self, generation: int, cancel_event, runtime,
    ):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.COMPLETE
        self._mods_manager_recovery_running = False
        self._mods_manager_recovery_cancel_event = None
        self._hide_flatpak_mod_manager_recovery()
        self._open_mods_manager_after_gate(
            verified_native=native_steam_ready_for_mod_manager(runtime),
        )
        return False

    def _finish_flatpak_recovery_success(self, generation: int, cancel_event):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        if (
            getattr(self, "_mods_manager_recovery_phase", None)
            is not ModManagerRecoveryPhase.OPENING_MOD_MANAGER
        ):
            return False
        manager = getattr(self, "_mods_mgr_overlay", None)
        if manager is None:
            return self._finish_flatpak_recovery_failure(
                generation, cancel_event,
                "Mod Manager could not open. Retry.",
            )
        self._clear_flatpak_recovery_retry_timer()
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.COMPLETE
        self._mods_manager_recovery_running = False
        self._mods_manager_recovery_cancel_event = None
        self._hide_flatpak_mod_manager_recovery()
        manager.show_recovered_inventory()
        return False

    def _begin_flatpak_recovery_inventory_handoff(
        self, generation: int, cancel_event, attempt: int,
    ):
        """Preload one authoritative inventory while the recovery UI stays up."""
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        try:
            manager = self._ensure_mods_manager_overlay()
            if manager is None:
                raise RuntimeError("Mod Manager overlay is unavailable")
            manager.accept_recovered_native_authority()
            manager.prepare_recovered_inventory(
                lambda success: self._complete_flatpak_recovery_inventory_handoff(
                    generation, cancel_event, attempt, bool(success),
                )
            )
        except Exception:
            return self._finish_flatpak_recovery_failure(
                generation, cancel_event,
                "Mod Manager could not check Steam. Retry.",
            )
        return False

    def _complete_flatpak_recovery_inventory_handoff(
        self, generation: int, cancel_event, attempt: int, success: bool,
    ):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        if success:
            if not self._advance_flatpak_recovery_phase(
                generation,
                cancel_event,
                ModManagerRecoveryPhase.OPENING_MOD_MANAGER,
                "Opening Mod Manager…",
            ):
                return False
            self._clear_flatpak_recovery_retry_timer()
            GLib.idle_add(
                self._finish_flatpak_recovery_success,
                generation,
                cancel_event,
            )
            return False
        if int(attempt) >= RECOVERY_INVENTORY_ATTEMPTS:
            return self._finish_flatpak_recovery_failure(
                generation,
                cancel_event,
                "Native Steam is not ready. Finish logging in, then Retry.",
            )
        self._mods_manager_recovery_retry_source_id = int(GLib.timeout_add(
            RECOVERY_INVENTORY_RETRY_DELAY_MS,
            self._retry_flatpak_recovery_inventory_handoff,
            generation,
            cancel_event,
            int(attempt) + 1,
        ) or 0)
        return False

    def _retry_flatpak_recovery_inventory_handoff(
        self, generation: int, cancel_event, attempt: int,
    ):
        if not self._recovery_callback_is_current(generation, cancel_event):
            return False
        self._mods_manager_recovery_retry_source_id = 0
        return self._begin_flatpak_recovery_inventory_handoff(
            generation, cancel_event, attempt,
        )

    def _flatpak_recovery_worker_exited(self, cancel_event):
        if cancel_event is getattr(
            self, "_mods_manager_recovery_cancel_event", None
        ) and cancel_event.is_set():
            self._mods_manager_recovery_running = False
            warning = getattr(self, "_mods_manager_flatpak_warning", None)
            if warning is not None:
                warning["continue_button"].set_sensitive(True)
        return False

    def _cancel_flatpak_mod_manager_recovery(self):
        cancel_event = getattr(self, "_mods_manager_recovery_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        recheck_cancel = getattr(
            self, "_mods_manager_recheck_cancel_event", None
        )
        if recheck_cancel is not None:
            recheck_cancel.set()
        self._mods_manager_recovery_generation = int(
            getattr(self, "_mods_manager_recovery_generation", 0) or 0
        ) + 1
        self._mods_manager_recheck_generation = int(
            getattr(self, "_mods_manager_recheck_generation", 0) or 0
        ) + 1
        self._mods_manager_recheck_running = False
        self._mods_manager_recheck_cancel_event = None
        self._clear_flatpak_recovery_retry_timer()
        self._mods_manager_recovery_phase = ModManagerRecoveryPhase.IDLE
        manager = getattr(self, "_mods_mgr_overlay", None)
        if manager is not None and bool(
            getattr(manager, "_recovery_inventory_handoff_active", False)
        ):
            try:
                manager.reset_for_steam_recovery()
            except Exception:
                pass
        self._hide_flatpak_mod_manager_recovery()
        return False

    def _on_recovery_host_close(self, *_args):
        self._cancel_flatpak_mod_manager_recovery()
        return False

    def _on_mod_manager_flatpak_detected(self, runtime=None):
        manager = getattr(self, "_mods_mgr_overlay", None)
        if manager is not None:
            try:
                manager.block_for_flatpak()
            except Exception:
                pass
        if runtime is None:
            self._start_mod_manager_entry_check()
        elif self._runtime_has_native_processes(runtime):
            self._show_mixed_steam_mod_manager_gate()
        else:
            self._show_flatpak_mod_manager_recovery()
        return False

    def build_panel(self) -> Gtk.Widget:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.set_size_request(480, -1)
        panel.set_vexpand(True)
        panel.set_hexpand(False)
        panel.add_css_class("settings-panel")
        panel.set_margin_top(10)
        panel.set_margin_bottom(10)
        panel.set_margin_end(10)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_top(10)
        top.set_margin_bottom(8)

        title = Gtk.Label(label="Settings")
        title.add_css_class("settings-section-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        top.append(title)

        close_btn = Gtk.Button()
        close_btn.set_can_focus(False)
        close_btn.add_css_class("flat")
        close_btn.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
        close_btn.set_tooltip_text("Close")
        close_btn.connect("clicked", lambda *_: self._win._close_settings_panel())
        attach_pointer_cursor(close_btn)
        top.append(close_btn)

        panel.append(top)
        panel.append(hr())

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)

        self._win.settings_stack = Gtk.Stack()
        self._win.settings_stack.add_css_class("settings-content")
        self._win.settings_stack.set_vexpand(True)
        self._win.settings_stack.set_hexpand(True)
        self._win.settings_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._win.settings_stack.set_transition_duration(160)

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(self._win.settings_stack)
        sidebar.set_vexpand(True)
        sidebar.set_hexpand(False)
        sidebar.set_size_request(160, -1)
        sidebar.set_margin_top(10)
        sidebar.set_margin_bottom(10)
        sidebar.set_margin_start(10)
        sidebar.set_margin_end(10)
        sidebar.add_css_class("settings-nav")

        # ----------------------------
        # POINTER CURSOR ON NAV ITEMS ONLY
        # ----------------------------
        def _find_listbox_row(w: Gtk.Widget):
            try:
                cur = w
                while cur is not None:
                    if isinstance(cur, Gtk.ListBoxRow):
                        return cur
                    cur = cur.get_parent()
            except Exception:
                return None
            return None

        def _nav_set_cursor_for_xy(x: float, y: float):
            try:
                picked = sidebar.pick(x, y, Gtk.PickFlags.DEFAULT)
            except Exception:
                picked = None

            row = _find_listbox_row(picked) if picked is not None else None
            try:
                if row is not None:
                    sidebar.set_cursor_from_name("pointer")
                else:
                    sidebar.set_cursor(None)
            except Exception:
                pass

        nav_motion = Gtk.EventControllerMotion()
        nav_motion.connect("enter", lambda _c, x, y: _nav_set_cursor_for_xy(x, y))
        nav_motion.connect("motion", lambda _c, x, y: _nav_set_cursor_for_xy(x, y))
        nav_motion.connect("leave", lambda *_: sidebar.set_cursor(None))
        sidebar.add_controller(nav_motion)
        # ----------------------------

        def wrap_page(inner: Gtk.Widget) -> Gtk.Widget:
            sc = Gtk.ScrolledWindow()
            sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sc.set_hexpand(True)
            sc.set_vexpand(True)

            pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            pad.set_margin_top(12)
            pad.set_margin_bottom(12)
            pad.set_margin_start(12)
            pad.set_margin_end(12)
            pad.set_hexpand(True)
            pad.set_vexpand(True)
            pad.append(inner)

            sc.set_child(pad)
            return sc

        self._win.settings_stack.add_titled(wrap_page(self._settings_page_general()), "general", "General")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_launch()), "launch", "Launch")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_mods()), "mods", "Mods")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_discord()), "discord", "Discord")
        self._win.settings_stack.add_titled(wrap_page(self._settings_page_about()), "about", "About")

        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nav.set_vexpand(True)
        nav.set_hexpand(False)
        nav.append(sidebar)
        nav.append(hr())

        reset_wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        reset_wrap.set_margin_start(10)
        reset_wrap.set_margin_end(10)
        reset_wrap.set_margin_top(10)
        reset_wrap.set_margin_bottom(10)
        reset_wrap.set_hexpand(True)
        reset_wrap.set_halign(Gtk.Align.FILL)

        sp_l = Gtk.Box()
        sp_l.set_hexpand(True)
        sp_r = Gtk.Box()
        sp_r.set_hexpand(True)

        reset_btn = Gtk.Button(label="Reset To Defaults")
        reset_btn.set_can_focus(False)
        attach_pointer_cursor(reset_btn)
        reset_btn.set_halign(Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._on_reset_settings_clicked)

        reset_wrap.append(sp_l)
        reset_wrap.append(reset_btn)
        reset_wrap.append(sp_r)

        nav.append(reset_wrap)

        body.append(nav)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        body.append(self._win.settings_stack)

        panel.append(body)

        # Apply runtime effects once panel is built (master toggles / dependent widgets / etc.)
        # If main.py doesn't recognize "settings_init", it's harmless.
        def apply_initial_runtime_effects():
            self._win._settings_init_idle_id = 0
            if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
                return False
            self._win._apply_setting_runtime_effects("settings_init")
            return False

        self._win._settings_init_idle_id = int(
            GLib.idle_add(apply_initial_runtime_effects) or 0
        )

        return panel

    def _settings_section_header(self, text: str) -> Gtk.Widget:
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        lbl.add_css_class("settings-section-title")
        return lbl

    def _settings_row_entry(
            self,
            title: str,
            key: str,
            placeholder: str = "",
            is_int: bool = False,
            tooltip: str | None = None,
            autodetect_fn=None,  # optional callable -> str
            user_set_flag: str | None = None,  # optional bool key to mark "user typed"
            browse: bool = False,  # NEW: show Browse… button
            browse_select_folder: bool = False,  # NEW: folder picker vs file picker
            show_autodetect_guess: bool = True,
    ) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        l = Gtk.Label(label=title)
        l.set_xalign(0)

        e = Gtk.Entry()
        if placeholder:
            e.set_placeholder_text(placeholder)
        if tooltip:
            e.set_tooltip_text(tooltip)

        cur = self._win.settings.get(key, "")
        cur_s = ("" if cur is None else str(cur)).strip()

        # Ensure user_set_flag exists if requested
        if user_set_flag and user_set_flag not in self._win.settings:
            self._win.settings[user_set_flag] = False
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        # If empty and autodetect available, display suggestion (dimmed) but do NOT mark user-set.
        # If empty and autodetect available, display suggestion (dimmed) but do NOT
        # write it into live settings. Runtime code must resolve the path itself.
        if (not cur_s) and callable(autodetect_fn) and show_autodetect_guess:
            guess = ""
            try:
                guess = (str(autodetect_fn() or "")).strip()
            except Exception:
                guess = ""
            if guess:
                try:
                    self._win._settings_update_guard = True
                    e.set_text(guess)
                finally:
                    self._win._settings_update_guard = False

                e.add_css_class("dimmed-entry")
            else:
                e.set_text(cur_s)
        else:
            e.set_text(cur_s)

            # If we have a value but it wasn't user-set, dim it.
            if user_set_flag and cur_s and (not bool(self._win.settings.get(user_set_flag, False))):
                e.add_css_class("dimmed-entry")

        def on_changed(_e):
            if getattr(self._win, "_settings_update_guard", False):
                return

            val = e.get_text()
            val_s = (val or "").strip()

            if user_set_flag:
                self._win.settings[user_set_flag] = bool(val_s)
                e.remove_css_class("dimmed-entry")

            if is_int:
                try:
                    val_i = int(val_s)
                except Exception:
                    val_i = int(self._win.settings.get(key, 0) or 0)
                self._win.settings[key] = val_i
            else:
                self._win.settings[key] = val_s

            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        e.connect("changed", on_changed)
        self._win._settings_widgets[key] = e

        # Entry row: Entry + optional Browse button
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.set_hexpand(True)
        e.set_hexpand(True)
        line.append(e)

        if browse:
            btn = Gtk.Button(label="Browse")
            btn.set_can_focus(False)
            attach_pointer_cursor(btn)
            line.append(btn)

            def _on_browse(_btn):
                try:
                    dlg = Gtk.FileChooserNative.new(
                        title=title,
                        parent=self._win,
                        action=Gtk.FileChooserAction.SELECT_FOLDER if browse_select_folder else Gtk.FileChooserAction.OPEN,
                        accept_label="Select",
                        cancel_label="Cancel",
                    )

                    def _done(_dlg, response):
                        try:
                            if response != Gtk.ResponseType.ACCEPT:
                                return
                            f = _dlg.get_file()
                            if not f:
                                return
                            path = f.get_path() or ""
                            if not path:
                                return

                            # mark as user set + undim
                            if user_set_flag:
                                self._win.settings[user_set_flag] = True
                            e.remove_css_class("dimmed-entry")

                            # set entry text without triggering recursive updates
                            self._win._settings_update_guard = True
                            try:
                                e.set_text(path)
                            finally:
                                self._win._settings_update_guard = False

                            # write value + persist
                            if is_int:
                                try:
                                    self._win.settings[key] = int(str(path).strip())
                                except Exception:
                                    self._win.settings[key] = int(self._win.settings.get(key, 0) or 0)
                            else:
                                self._win.settings[key] = (path or "").strip()

                            try:
                                save_settings(self._win.settings)
                            except Exception:
                                pass

                            try:
                                self._win._apply_setting_runtime_effects(key)
                            except Exception:
                                pass
                        except Exception:
                            pass

                    dlg.connect("response", _done)
                    dlg.show()
                except Exception:
                    pass

            btn.connect("clicked", _on_browse)

        row.append(l)
        row.append(line)
        return row

    def _settings_row_switch(self, title: str, key: str, default: bool = False, tooltip: str | None = None) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_hexpand(True)

        l = Gtk.Label(label=title)
        l.set_xalign(0)
        l.set_hexpand(True)

        if key not in self._win.settings:
            self._win.settings[key] = bool(default)
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        sw = Gtk.Switch()
        sw.set_active(bool(self._win.settings.get(key, False)))
        attach_pointer_cursor(sw)

        if tooltip:
            row.set_tooltip_text(tooltip)
            sw.set_tooltip_text(tooltip)

        def on_toggled(_sw, _pspec):
            if getattr(self._win, "_settings_update_guard", False):
                return

            self._win.settings[key] = bool(sw.get_active())
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        sw.connect("notify::active", on_toggled)
        self._win._settings_widgets[key] = sw

        row.append(l)
        row.append(sw)
        return row

    def _settings_row_dropdown(self, title: str, key: str, options: list[tuple[str, str]], default_val: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        l = Gtk.Label(label=title)
        l.set_xalign(0)

        model = Gtk.StringList.new([label for _val, label in options])
        dd = Gtk.DropDown.new(model, None)
        dd.add_css_class("dzll-dropdown")
        attach_pointer_cursor(dd)

        cur = str(self._win.settings.get(key, default_val) or default_val)
        idx = 0
        for i, (v, _lab) in enumerate(options):
            if v == cur:
                idx = i
                break
        dd.set_selected(idx)

        def on_sel(_dd, _pspec):
            if getattr(self._win, "_settings_update_guard", False):
                return

            i = int(dd.get_selected())
            if 0 <= i < len(options):
                self._win.settings[key] = options[i][0]
                try:
                    save_settings(self._win.settings)
                except Exception:
                    pass

                try:
                    self._win._apply_setting_runtime_effects(key)
                except Exception:
                    pass

        dd.connect("notify::selected", on_sel)
        self._win._settings_widgets[key] = dd

        row.append(l)
        row.append(dd)
        return row

    def _settings_row_checkbox(self, title: str, key: str, default: bool = False) -> Gtk.Widget:
        cb = Gtk.CheckButton(label=title)
        attach_pointer_cursor(cb)

        if key not in self._win.settings:
            self._win.settings[key] = bool(default)
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

        cb.set_active(bool(self._win.settings.get(key, False)))

        def on_toggled(_cb):
            if getattr(self._win, "_settings_update_guard", False):
                return

            self._win.settings[key] = bool(cb.get_active())
            try:
                save_settings(self._win.settings)
            except Exception:
                pass

            try:
                self._win._apply_setting_runtime_effects(key)
            except Exception:
                pass

        cb.connect("toggled", on_toggled)
        self._win._settings_widgets[key] = cb
        return cb

    def _settings_page_general(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("General"))

        box.append(self._settings_row_entry("Ingame Name", "ingame_name", "Required By Many Servers"))
        box.append(self._settings_row_switch(
            "Show Background Download Buttons",
            "show_background_download_buttons",
            default=False,
        ))
        box.append(self._settings_row_switch("Hide Test Servers By Default", "hide_test_servers", default=True))
        # These controls are currently exposed in the sidebar; the underlying
        # settings are intentionally retained for persistence and future reuse.

        box.append(hr())
        box.append(self._settings_row_switch("Show Server Companion", "show_server_companion", default=False))

        learning_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        learning_heading = self._settings_section_header("Server Companion Learning Data")
        learning_heading.add_css_class("companion-learning-data-title")
        learning_box.append(learning_heading)

        learning_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        learning_actions.set_halign(Gtk.Align.START)
        export_learning_btn = Gtk.Button(label="Export Data")
        export_learning_btn.set_tooltip_text("Export restart-learning data.")
        import_learning_btn = Gtk.Button(label="Import Data")
        import_learning_btn.set_tooltip_text("Replace restart-learning data.")
        for action in (export_learning_btn, import_learning_btn):
            attach_pointer_cursor(action)
            learning_actions.append(action)
        learning_box.append(learning_actions)

        pending_learning_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pending_learning_box.set_visible(False)
        pending_learning_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        pending_warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        pending_warning_icon.set_pixel_size(16)
        pending_warning_icon.add_css_class("companion-pending-import-warning")
        pending_learning_line.append(pending_warning_icon)
        pending_learning_text = Gtk.Label(label=" Learning database replacement pending - ")
        pending_learning_text.add_css_class("companion-pending-import-warning")
        pending_learning_line.append(pending_learning_text)
        cancel_pending_btn = Gtk.Button(label="Cancel")
        cancel_pending_btn.add_css_class("flat")
        cancel_pending_btn.add_css_class("companion-pending-import-cancel")
        attach_pointer_cursor(cancel_pending_btn)
        pending_learning_line.append(cancel_pending_btn)
        pending_learning_box.append(pending_learning_line)
        pending_restart_note = Gtk.Label(label="Will be applied on next restart.")
        pending_restart_note.set_xalign(0.0)
        pending_restart_note.set_margin_start(21)
        pending_restart_note.set_wrap(True)
        pending_restart_note.add_css_class("dim-label")
        pending_learning_box.append(pending_restart_note)
        learning_box.append(pending_learning_box)

        learning_status_label = Gtk.Label()
        learning_status_label.set_xalign(0.0)
        learning_status_label.set_wrap(True)
        learning_status_label.add_css_class("dim-label")
        learning_box.append(learning_status_label)
        box.append(learning_box)

        transfer = self._win.companion_learning_transfer
        export_learning_btn.connect("clicked", transfer.choose_export)
        import_learning_btn.connect("clicked", transfer.choose_import)
        cancel_pending_btn.connect("clicked", transfer.cancel_pending)
        transfer.bind_settings_widgets(
            export_button=export_learning_btn,
            import_button=import_learning_btn,
            cancel_button=cancel_pending_btn,
            pending_container=pending_learning_box,
            status_label=learning_status_label,
        )

        box.append(hr())
        box.append(self._settings_row_switch("Show Counts In Title Bar", "show_counts_in_title_bar", default=False))
        box.append(self._settings_row_checkbox("Servers Loaded", "show_counts_servers_loaded", default=True))
        box.append(self._settings_row_checkbox("Global Players", "show_counts_global_players", default=True))

        master = bool(self._win.settings.get("show_counts_in_title_bar", False))
        self._win._set_widget_sensitive("show_counts_servers_loaded", master)
        self._win._set_widget_sensitive("show_counts_global_players", master)

        box.append(hr())

        btn = Gtk.Button(label="Clear Played History")
        btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(btn)
        btn.connect("clicked", lambda _b: self._win._clear_played_history())
        box.append(btn)

        box.append(hr())
        box.append(self._settings_row_switch("Auto Check For Updates", "auto_check_updates", default=True))

        update_db_btn = Gtk.Button(label="Update Server Database")
        update_db_btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(update_db_btn)
        update_db_btn.connect("clicked", lambda _b: self._win._manual_update_server_database())
        box.append(update_db_btn)

        self._general_action_button_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._general_action_button_size_group.add_widget(btn)
        self._general_action_button_size_group.add_widget(update_db_btn)
        return box

    def _settings_page_launch(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Launch"))

        box.append(self._settings_row_entry(
            "Additional Launch Params",
            "additional_launch_params",
            "-nosplash -world=empty",
        ))

        box.append(hr())

        box.append(self._settings_row_switch(
            "Start Steam Automatically on Join",
            "start_steam_on_join",
            default=False,
            tooltip="Automatically start Steam when joining a server if Steam is closed.",
        ))

        box.append(self._settings_row_switch(
            "Skip DayZ Launcher",
            "skip_dayz_launcher",
            default=True,
        ))

        box.append(self._settings_row_switch(
            "Minimise DayZ Launcher",
            "minimize_dayz_launcher",
            default=False,
            tooltip="Minimise the DayZ launcher on game start. Helps prevent the launcher border being visible in-game.",
        ))

        box.append(self._settings_row_switch("No Splash", "no_splash", default=True))

        box.append(self._settings_row_switch(
            "Force Fullscreen",
            "force_fullscreen",
            default=False,
            tooltip="If enabled, Windowed Mode is disabled.",
        ))

        box.append(self._settings_row_switch(
            "Windowed Mode",
            "windowed_mode",
            default=False,
            tooltip="If enabled, Force Fullscreen is disabled.",
        ))

        return box

    def _settings_page_mods(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Mods"))

        box.append(self._settings_row_switch(
            "Enable DZLL Mod Management",
            "enable_steamcmd_mod_handling",
            default=True,
            tooltip="Master switch for DZLL required-mod handling, downloads, and link management.",
        ))

        box.append(self._settings_row_entry(
            "Workshop Directory",
            "workshop_dir",
            "Auto-detected if empty",
            tooltip="If empty, DZLL uses the auto-detected Steam Workshop directory.",
            autodetect_fn=autodetect_workshop_dir,
            user_set_flag="workshop_dir_user_set",
            browse=True,
            browse_select_folder=True,  # folder
            show_autodetect_guess=False,
        ))

        box.append(self._settings_row_entry(
            "Additional Mods (Id's)",
            "additional_mod_ids",
            "1559212036, 1234567890",
        ))

        box.append(self._settings_row_switch(
            "Auto-install Missing Mods",
            "auto_install_missing_mods",
            default=True,
            tooltip="When joining a server, automatically download required mods that are missing.",
        ))

        box.append(hr())

        btn = Gtk.Button(label="Manage Installed Mods")
        btn.set_halign(Gtk.Align.START)
        attach_pointer_cursor(btn)
        box.append(btn)
        btn.connect("clicked", lambda *_: self.open_mods_manager())

        return box

    def _settings_page_discord(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("Discord"))

        box.append(self._settings_row_switch("Enable Rich Presence", "discord_rich_presence", default=False))
        box.append(self._settings_row_switch(
            "Privacy Mode (Hide Server Details)",
            "discord_privacy_mode",
            default=False,
            tooltip="Only applies when Detail Level is set to 'On Server'.",
        ))

        box.append(self._settings_row_dropdown(
            "Detail Level",
            "discord_detail_level",
            options=[("menus", "In Menus"), ("ingame", "In Game"), ("server", "On Server")],
            default_val="menus",
        ))
        return box

    def _settings_page_about(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._settings_section_header("About"))

        lbl = Gtk.Label(
            label=f"DayZ Linux Launcher Version: {APP_VERSION}\n\n"
                  "Unofficial Community Launcher.\n"
                  "Not Affiliated With Bohemia Interactive."
        )
        lbl.set_xalign(0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_halign(Gtk.Align.CENTER)
        box.append(lbl)

        update_status = Gtk.Label(label="")
        update_status.set_xalign(0.5)
        update_status.set_halign(Gtk.Align.CENTER)
        box.append(update_status)

        btn_updates = Gtk.Button(label="Check For Updates")
        btn_updates.set_can_focus(False)
        attach_pointer_cursor(btn_updates)
        btn_updates.connect(
            "clicked",
            lambda *_: (
                update_status.set_text("Checking for updates…"),
                self._win._manual_check_for_updates(update_status.set_text),
            ),
        )

        center_box = Gtk.Box(halign=Gtk.Align.CENTER)
        center_box.append(btn_updates)
        btn_updates.set_margin_start(10)
        btn_updates.set_margin_end(10)
        box.append(center_box)

        box.append(hr())
        box.append(self._settings_section_header("Support DZLL"))

        try:
            qr_path = os.path.join(IMAGES_DIR, "qr-code.png")
            if os.path.exists(qr_path):
                qr = Gtk.Picture.new_for_filename(qr_path)
                qr.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                qr.set_can_shrink(True)
                qr.set_halign(Gtk.Align.CENTER)
                qr.set_valign(Gtk.Align.START)
                qr.set_hexpand(False)
                box.append(qr)
        except Exception:
            pass

        try:
            bmc_path = os.path.join(IMAGES_DIR, "buy-coffee.png")
            if os.path.exists(bmc_path):
                bmc_btn = Gtk.Button()
                bmc_btn.set_can_focus(False)
                bmc_btn.add_css_class("flat")
                attach_pointer_cursor(bmc_btn)
                bmc_btn.set_tooltip_text("Support DZLL (Buy Me A Coffee)")
                bmc_btn.set_halign(Gtk.Align.CENTER)
                bmc_btn.set_valign(Gtk.Align.START)
                bmc_btn.set_hexpand(False)

                pic = Gtk.Picture.new_for_filename(bmc_path)
                pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                pic.set_can_shrink(True)
                pic.set_halign(Gtk.Align.CENTER)
                pic.set_valign(Gtk.Align.START)
                pic.set_hexpand(False)

                bmc_btn.set_child(pic)
                bmc_btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
                    "https://buymeacoffee.com/berry.dingle", None
                ))
                box.append(bmc_btn)
        except Exception:
            pass

        box.append(hr())
        box.append(self._settings_section_header("Links"))

        repo_url = RELEASES_URL
        try:
            if repo_url.endswith("/releases"):
                repo_url = repo_url[:-len("/releases")]
        except Exception:
            pass
        issues_url = repo_url.rstrip("/") + "/issues"

        links_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        links_row.set_halign(Gtk.Align.START)
        links_row.set_valign(Gtk.Align.END)

        def _icon_button(filename: str, tooltip: str) -> Gtk.Button:
            b = Gtk.Button()
            b.set_can_focus(False)
            b.add_css_class("flat")
            attach_pointer_cursor(b)
            b.set_tooltip_text(tooltip)

            p = os.path.join(IMAGES_DIR, filename)
            if os.path.exists(p):
                pic = Gtk.Picture.new_for_filename(p)
                pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                pic.set_can_shrink(True)
                b.set_child(pic)
                b.set_size_request(40, 32)  # icon hitbox
            else:
                b.set_label("")
            return b

        btn_repo = _icon_button("github-white-icon.png", "DZLL GitHub Repo")
        btn_repo.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(repo_url, None))
        links_row.append(btn_repo)

        invite = str(self._win.settings.get("discord_invite_url") or "").strip()
        btn_discord = _icon_button("discord-icon.png", "Join DZLL Discord Channel")
        if invite:
            btn_discord.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(invite, None))
        else:
            btn_discord.set_sensitive(False)
            btn_discord.set_tooltip_text("Join DZLL Discord Channel")
        links_row.append(btn_discord)

        btn_issues = _icon_button("zombie.png", "Github Issues Page")
        if btn_issues:
            btn_issues.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(issues_url, None))
        else:
            btn_issues.set_sensitive(False)
            btn_issues.set_tooltip_text("Github Issues Page")
        links_row.append(btn_issues)

        site_url = str(self._win.settings.get("website_url") or "").strip()
        btn_site = _icon_button("website.png", "Visit DZLL Website")
        if site_url:
            btn_site.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(site_url, None))
        else:
            btn_site.set_sensitive(False)
            btn_site.set_tooltip_text("Visit DZLL Website")
        links_row.append(btn_site)

        box.append(links_row)
        return box

    def _set_dropdown_to_value(self, dd: Gtk.DropDown, value: str):
        try:
            model = dd.get_model()
            if model is None:
                return

            # We only need this for the known settings dropdowns currently in DZLL
            key = None
            for k, w in self._win._settings_widgets.items():
                if w is dd:
                    key = k
                    break

            options_map = {
                "discord_detail_level": ["menus", "ingame", "server"],
            }

            vals = options_map.get(key, [])
            if not vals:
                return

            idx = 0
            for i, v in enumerate(vals):
                if v == str(value):
                    idx = i
                    break

            dd.set_selected(idx)
        except Exception:
            pass

    def _reset_entry_with_autodetect(self, key: str, autodetect_fn=None, user_set_flag: str | None = None):
        self._reset_entry_value(key, autodetect_fn=autodetect_fn, user_set_flag=user_set_flag, show_autodetect_guess=True)

    def _reset_entry_value(
            self,
            key: str,
            autodetect_fn=None,
            user_set_flag: str | None = None,
            show_autodetect_guess: bool = True,
    ):
        e = self._win._settings_widgets.get(key)
        if not isinstance(e, Gtk.Entry):
            return

        saved_val = str(self._win.settings.get(key, "") or "").strip()

        try:
            e.remove_css_class("dimmed-entry")
        except Exception:
            pass

        if saved_val:
            e.set_text(saved_val)
            if user_set_flag and not bool(self._win.settings.get(user_set_flag, False)):
                try:
                    e.add_css_class("dimmed-entry")
                except Exception:
                    pass
            return

        guess = ""
        if show_autodetect_guess and callable(autodetect_fn):
            try:
                guess = str(autodetect_fn() or "").strip()
            except Exception:
                guess = ""

        if guess:
            e.set_text(guess)
            try:
                e.add_css_class("dimmed-entry")
            except Exception:
                pass
        else:
            e.set_text("")

    def _on_reset_settings_clicked(self, *_args):
        # Reset DZLL settings only
        self._win.settings = reset_settings()

        try:
            self._win._ping_cutoff_ms = int(self._win.settings.get("high_ping_cutoff_ms", 250) or 250)
        except Exception:
            self._win._ping_cutoff_ms = 250

        try:
            self._win._settings_update_guard = True

            for key, w in self._win._settings_widgets.items():
                val = self._win.settings.get(key)

                if isinstance(w, Gtk.Entry):
                    # generic entries first; special autodetect entries handled below
                    if key != "workshop_dir":
                        w.set_text("" if val is None else str(val))
                        try:
                            w.remove_css_class("dimmed-entry")
                        except Exception:
                            pass

                elif isinstance(w, Gtk.Switch):
                    w.set_active(bool(val))

                elif isinstance(w, Gtk.CheckButton):
                    w.set_active(bool(val))

                elif isinstance(w, Gtk.DropDown):
                    self._set_dropdown_to_value(w, val)

            # Rebuild autodetect suggestion entries WITHOUT committing them to settings
            self._reset_entry_value(
                "workshop_dir",
                autodetect_fn=autodetect_workshop_dir,
                user_set_flag="workshop_dir_user_set",
                show_autodetect_guess=False,
            )

        except Exception:
            pass
        finally:
            self._win._settings_update_guard = False

        # Re-apply runtime-only effects from clean settings
        self._win._apply_setting_runtime_effects("high_ping_cutoff_ms")
        self._win._apply_setting_runtime_effects("hide_below_max_players")
        self._win._apply_setting_runtime_effects("hide_test_servers")
        self._win._apply_setting_runtime_effects("prioritise_trusted_servers")
        self._win._apply_setting_runtime_effects("pin_favorite_servers")
        self._win._apply_setting_runtime_effects("show_server_companion")
        self._win._apply_setting_runtime_effects("show_background_download_buttons")
        self._win._apply_setting_runtime_effects("show_counts_in_title_bar")
        self._win._apply_setting_runtime_effects("show_counts_servers_loaded")
        self._win._apply_setting_runtime_effects("show_counts_global_players")
        self._win._apply_setting_runtime_effects("enable_steamcmd_mod_handling")
        self._win._apply_setting_runtime_effects("skip_dayz_launcher")
        self._win._apply_setting_runtime_effects("minimize_dayz_launcher")
        self._win._apply_setting_runtime_effects("force_fullscreen")
        self._win._apply_setting_runtime_effects("windowed_mode")
        self._win._apply_setting_runtime_effects("discord_detail_level")
        self._win._apply_titlebar_counts()

    def _set_widget_sensitive(self, key: str, sensitive: bool):
        w = self._win._settings_widgets.get(key)
        if w is None:
            return
        try:
            w.set_sensitive(bool(sensitive))
        except Exception:
            pass

    def _sync_sidebar_setting_widget(self, key: str):
        widget = getattr(self._win, "_sidebar_settings_widgets", {}).get(key)
        if widget is None:
            return

        def _entry_has_focus(entry):
            try:
                return bool(entry.has_focus())
            except Exception:
                return False

        def _set_entry_text_if_changed(entry, text):
            text = "" if text is None else str(text)
            if (entry.get_text() or "") == text:
                return
            try:
                pos = int(entry.get_position())
            except Exception:
                pos = -1
            entry.set_text(text)
            if pos >= 0:
                try:
                    entry.set_position(min(pos, len(text)))
                except Exception:
                    pass

        old_guard = bool(getattr(self._win, "_settings_update_guard", False))
        try:
            self._win._settings_update_guard = True
            val = self._win.settings.get(key)
            if isinstance(widget, Gtk.Entry):
                if not _entry_has_focus(widget):
                    _set_entry_text_if_changed(widget, "" if val is None else str(val))
            elif isinstance(widget, Gtk.CheckButton):
                if bool(widget.get_active()) != bool(val):
                    widget.set_active(bool(val))
        except Exception:
            pass
        finally:
            self._win._settings_update_guard = old_guard

    def _apply_setting_runtime_effects(self, key: str):
        self._sync_sidebar_setting_widget(key)

        if key == "high_ping_cutoff_ms":
            try:
                self._win._ping_cutoff_ms = int(self._win.settings.get("high_ping_cutoff_ms", 250) or 250)
            except Exception:
                self._win._ping_cutoff_ms = 250

            for k, obj in self._win._obj_by_key.items():
                try:
                    p = int(obj.ping)
                except Exception:
                    p = -1
                if p >= 0:
                    self._win.live.setdefault(k, {})["hide_high_ping"] = (p > self._win._ping_cutoff_ms)
            self._win._on_filter_changed(reason="settings")

        if key == "hide_test_servers":
            self._win._on_filter_changed(reason="settings")

        if key == "hide_below_max_players":
            self._win._on_filter_changed(reason="settings")

        if key == "prioritise_trusted_servers":
            try:
                self._win._snapshot_all_sort_keys()
                sorter = getattr(self._win, "sorter", None)
                if sorter is not None:
                    sorter.changed(Gtk.SorterChange.DIFFERENT)
                GLib.idle_add(self._win._scroll_to_top)
            except Exception:
                pass

        if key == "pin_favorite_servers":
            scroll_value = None
            try:
                vadj = self._win.scroller.get_vadjustment()
                if vadj:
                    scroll_value = float(vadj.get_value())
            except Exception:
                scroll_value = None

            try:
                self._win._snapshot_all_sort_keys()
                sorter = getattr(self._win, "sorter", None)
                if sorter is not None:
                    sorter.changed(Gtk.SorterChange.DIFFERENT)
                self._win._rebuild_column_view_store(reorder_reason="settings:pin-favorite-servers")
            except Exception:
                pass

            if scroll_value is not None:
                def restore_pin_favorites_scroll(value=scroll_value):
                    try:
                        vadj = self._win.scroller.get_vadjustment()
                        if vadj:
                            vadj.set_value(value)
                    except Exception:
                        pass
                    return False

                GLib.idle_add(restore_pin_favorites_scroll)

        if key == "show_server_companion":
            self._win.set_server_companion_visible(bool(self._win.settings.get("show_server_companion", False)))

        if key == "show_background_download_buttons":
            self._win._set_background_download_column_visible(
                bool(self._win.settings.get("show_background_download_buttons", False))
            )

        if key in ("show_counts_in_title_bar", "show_counts_servers_loaded", "show_counts_global_players"):
            master = bool(self._win.settings.get("show_counts_in_title_bar", False))
            self._set_widget_sensitive("show_counts_servers_loaded", master)
            self._set_widget_sensitive("show_counts_global_players", master)
            self._win._apply_titlebar_counts()

        if key in ("enable_steamcmd_mod_handling", "settings_init"):
            enabled = bool(self._win.settings.get("enable_steamcmd_mod_handling", True))

            for dep in (
                    "auto_install_missing_mods",
                    "workshop_dir",
                    "additional_mod_ids",
            ):
                self._set_widget_sensitive(dep, enabled)

        # Force Fullscreen <-> Windowed Mode (mutual exclusion, NO flicker)
        if key in ("force_fullscreen", "windowed_mode", "settings_init"):
            fs = bool(self._win.settings.get("force_fullscreen", False))
            wn = bool(self._win.settings.get("windowed_mode", False))

            # Always keep both clickable
            try:
                self._set_widget_sensitive("windowed_mode", True)
                self._set_widget_sensitive("force_fullscreen", True)
            except Exception:
                pass

            # Helper: set a key + switch without recursion
            def _set_switch(keyname: str, val: bool):
                try:
                    self._win._settings_update_guard = True
                    self._win.settings[keyname] = bool(val)
                    w = self._win._settings_widgets.get(keyname)
                    if w:
                        w.set_active(bool(val))
                    save_settings(self._win.settings)
                except Exception:
                    pass
                finally:
                    self._win._settings_update_guard = False

            # ---- Precedence: the switch the user just changed wins ----
            if key == "windowed_mode":
                # If user turned Windowed ON, force Fullscreen OFF first.
                if wn and fs:
                    _set_switch("force_fullscreen", False)

            elif key == "force_fullscreen":
                # If user turned Fullscreen ON, force Windowed OFF.
                if fs and wn:
                    _set_switch("windowed_mode", False)

            else:
                # settings_init: if both are True, pick one deterministically.
                # Prefer windowed (safer/less surprising).
                if fs and wn:
                    _set_switch("force_fullscreen", False)

        # Skip DayZ Launcher <-> Minimise DayZ Launcher
        if key in ("skip_dayz_launcher", "minimize_dayz_launcher", "settings_init"):
            skip = bool(self._win.settings.get("skip_dayz_launcher", True))
            mini = bool(self._win.settings.get("minimize_dayz_launcher", False))

            def _set_switch(keyname: str, val: bool):
                try:
                    self._win._settings_update_guard = True
                    self._win.settings[keyname] = bool(val)
                    w = self._win._settings_widgets.get(keyname)
                    if w:
                        w.set_active(bool(val))
                    save_settings(self._win.settings)
                except Exception:
                    pass
                finally:
                    self._win._settings_update_guard = False

            if key == "skip_dayz_launcher":
                if skip and mini:
                    _set_switch("minimize_dayz_launcher", False)
            elif key == "minimize_dayz_launcher":
                if mini and skip:
                    _set_switch("skip_dayz_launcher", False)
            elif skip and mini:
                _set_switch("minimize_dayz_launcher", False)

        # ---- Discord Rich Presence ----
        if key in ("discord_rich_presence", "discord_privacy_mode", "discord_detail_level", "settings_init"):
            # 1) Apply settings to RPC
            try:
                if hasattr(self._win, "_discord") and self._win._discord:
                    self._win._discord.apply_settings(self._win.settings)
            except Exception:
                pass

            # 2) Gate Privacy Mode toggle (only meaningful when RP ON + Detail=On Server)
            try:
                rp_on = bool(self._win.settings.get("discord_rich_presence", False))
                lvl = str(self._win.settings.get("discord_detail_level", "menus") or "menus").strip().lower()
                allow_priv = rp_on and (lvl == "server")
                self._set_widget_sensitive("discord_privacy_mode", allow_priv)

                # Optional: if it can't apply, force it OFF so settings stay logical
                if not allow_priv and bool(self._win.settings.get("discord_privacy_mode", False)):
                    try:
                        self._win._settings_update_guard = True
                        self._win.settings["discord_privacy_mode"] = False
                        w = self._win._settings_widgets.get("discord_privacy_mode")
                        if w:
                            w.set_active(False)
                        save_settings(self._win.settings)
                    finally:
                        self._win._settings_update_guard = False
            except Exception:
                pass

    def _clear_played_history(self) -> None:
        try:
            self._win.last_played = {}
            save_last_played(self._win.last_played)
        except Exception:
            pass

        try:
            for i in range(self._win.store.get_n_items()):
                obj = self._win.store.get_item(i)
                if obj is None:
                    continue
                obj.played = ""
                obj.sort_played_days = 999999
        except Exception:
            pass

        try:
            self._win._on_filter_changed(reason="settings")
        except Exception:
            pass

    def _open_settings_panel(self):
        if self._win._settings_open:
            return
        self._win._settings_open = True
        self._win.settings_scrim.set_visible(True)
        self._win.settings_revealer.set_reveal_child(True)

    def _close_settings_panel(self):
        if not self._win._settings_open:
            return
        self._win._settings_open = False
        self._win.settings_revealer.set_reveal_child(False)
        GLib.timeout_add(200, lambda: (self._win.settings_scrim.set_visible(False), False)[1])

    def _toggle_settings_panel(self):
        if self._win._settings_open:
            self._close_settings_panel()
        else:
            self._open_settings_panel()

    def _on_settings_clicked(self, *_args):
        self._toggle_settings_panel()
