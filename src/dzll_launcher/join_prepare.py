#!/usr/bin/env python3
import logging
import os
import sys
import threading
import time
import traceback

from . import workshop_mods
from .launcher_state import linux_to_win_path_under_prefix
from .mod_metadata import clean_display_mod_name
from .preparation_contracts import (
    JoinPopupPreparationPresenter,
    NoOpPreparationPresenter,
    PreparationOutcome,
    PreparationProgressEvent,
    PreparationStatus,
)
from .settings import autodetect_workshop_dir
from .steam_native import dayz_paths_summary, dayz_workshop_content_dir
from .steam_ugc_backend import (
    _strict_cleanup_workshop_item_ids,
    CooperativeUGCSession,
    UGCHelperReapError,
    activate_ugc_session,
    cleanup_cancelled_ugc_subscriptions,
    deactivate_ugc_session,
    query_ugc_state_checked,
    register_owned_ugc_session,
    refresh_subscribed_ugc_state_checked,
    unregister_owned_ugc_session,
    ugc_item_ready,
    wait_for_ugc_ready,
)


logger = logging.getLogger(__name__)


def _resolve_path(path):
    path = str(path or "").strip()
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(path))


def _missing_ids_for(workshop_dir, mods):
    return {int(mid) for mid, _name in (workshop_mods.compute_missing_mods(workshop_dir, mods) or [])}


def _maybe_autodetect_workshop_dir():
    resolved = _resolved_dayz_workshop_dir()
    if resolved:
        return resolved
    try:
        return _resolve_path(autodetect_workshop_dir() or "")
    except Exception:
        return ""


def _resolved_dayz_workshop_dir():
    try:
        content_dir = dayz_workshop_content_dir()
        if content_dir is not None:
            return _resolve_path(content_dir.parent.parent)
    except Exception:
        pass
    return ""


def _choose_initial_workshop_dir(configured_workshop_dir, mods):
    configured = _resolve_path(configured_workshop_dir)
    resolved = _resolved_dayz_workshop_dir()
    autodetected = resolved or _maybe_autodetect_workshop_dir()
    effective = configured

    if resolved:
        effective = resolved
        if configured and os.path.realpath(configured) != os.path.realpath(resolved):
            logger.debug(
                "DayZ Steam library workshop path selected: %r (configured=%r)",
                resolved, configured,
            )
    elif autodetected and not effective:
        effective = autodetected
    elif autodetected and configured and os.path.realpath(autodetected) != os.path.realpath(configured):
        try:
            configured_missing = _missing_ids_for(configured, mods) if os.path.isdir(configured) else {int(mid) for mid, _ in (mods or [])}
            autodetected_missing = _missing_ids_for(autodetected, mods)
            if len(autodetected_missing) < len(configured_missing):
                logger.debug(
                    "Workshop path refresh selected: configured=%r "
                    "autodetected=%r configured_missing=%s autodetected_missing=%s",
                    configured, autodetected,
                    sorted(configured_missing), sorted(autodetected_missing),
                )
                effective = autodetected
        except Exception as e:
            logger.debug("Workshop path comparison failed: %s", e)

    return effective or configured


def _refresh_effective_workshop_dir_after_backend(current_workshop_dir, mods):
    current = _resolve_path(current_workshop_dir)

    autodetected = _maybe_autodetect_workshop_dir()
    if autodetected and current and os.path.realpath(autodetected) != os.path.realpath(current):
        try:
            current_missing = _missing_ids_for(current, mods) if os.path.isdir(current) else {int(mid) for mid, _ in (mods or [])}
            autodetected_missing = _missing_ids_for(autodetected, mods)
            if len(autodetected_missing) < len(current_missing):
                logger.debug(
                    "Effective workshop path refreshed after backend: %r -> %r "
                    "current_missing=%s autodetected_missing=%s",
                    current, autodetected,
                    sorted(current_missing), sorted(autodetected_missing),
                )
                return autodetected
        except Exception as e:
            logger.debug("Post-backend workshop path comparison failed: %s", e)

    return current


def prepare_required_mods(win, mods, workshop_dir, mod_management_enabled,
                          auto_install_missing, *, operation_id=0, presenter=None,
                          server_name="", server_identity="", is_operation_current=None,
                          manage_join_presence=True, manage_join_presentation=True,
                          allow_backend_steam_start=True, cancel_event=None):
    """Run the existing Join preparation and stop before Join-only continuation."""
    attempt_id = int(operation_id or 0)
    mods = [
        (mod_id, clean_display_mod_name(name, mod_id, fallback=False))
        for mod_id, name in (mods or [])
    ]
    presenter = presenter or NoOpPreparationPresenter()
    is_operation_current = is_operation_current or (lambda: True)
    operation_cancel_event = (
        cancel_event
        if cancel_event is not None
        else win._join_preparation_cancel_event
    )
    stop_waiting_event = getattr(
        win, "_steam_client_stop_waiting_event", None,
    )

    def deliver_event(event):
        if is_operation_current():
            presenter.on_event(event)
        return False
    ok = True
    err_msg = None
    backend = "steam_client"
    effective_workshop_dir = _resolve_path(workshop_dir)
    mods_for_launch = list(mods or [])
    did_work = False
    ugc_session = None
    helper_reap_error = None
    cancel_cleanup_ids = set()
    cancel_cleanup_lock = threading.Lock()

    def collect_cancel_cleanup_handoff(handoff):
        if not isinstance(handoff, dict):
            return
        collected = set(_strict_cleanup_workshop_item_ids(
            handoff.get("cleanup_candidates") or [],
        ))
        if collected:
            with cancel_cleanup_lock:
                cancel_cleanup_ids.update(collected)

    if not mods:
        outcome = PreparationOutcome(
            PreparationStatus.NO_REQUIRED_MODS,
            reason="no_required_mods",
            backend=backend,
            effective_workshop_path=effective_workshop_dir,
        )
        presenter.on_terminal(outcome)
        return outcome

    try:
        # Decide what the selected Workshop backend should do for this join.
        required_ids = []
        required_ids_seen = set()
        for mid, _name in (mods or []):
            try:
                mid_i = int(mid)
            except Exception:
                continue
            if mid_i > 0 and mid_i not in required_ids_seen:
                required_ids.append(mid_i)
                required_ids_seen.add(mid_i)
        if mod_management_enabled:
            ugc_session = CooperativeUGCSession(
                cancel_event=operation_cancel_event,
            )
            register_owned_ugc_session(ugc_session)
            activate_ugc_session(ugc_session)
        if attempt_id:
            win._join_log(attempt_id, "chosen backend", backend="Steam client UGC")
        configured_workshop_dir = _resolve_path(workshop_dir)
        effective_workshop_dir = _choose_initial_workshop_dir(configured_workshop_dir, mods)
        logger.debug("Selected Join mod backend: Steam client UGC")
        try:
            dayz_summary = dayz_paths_summary()
            dayz_library = str(dayz_summary.get("dayz_library") or "")
            if dayz_library:
                logger.debug("Resolved DayZ library: %s", dayz_library)
            else:
                logger.debug("DayZ Steam library not detected; using configured/default paths")
        except Exception as exc:
            logger.debug(
                "DayZ Steam library not detected; using configured/default paths: %s",
                exc,
            )
        logger.debug("Configured Workshop path: %r", configured_workshop_dir)
        logger.debug(
            "Effective Workshop path used for checks/downloads: %r",
            effective_workshop_dir,
        )

        download_ids = []
        status_msg = ""

        if mod_management_enabled:
            report_readiness = bool(
                not manage_join_presentation
                or getattr(win, "_join_steam_start_allowed", False)
            )
            show_readiness_card = bool(manage_join_presentation and report_readiness)
            readiness_overlay_shown = win.threading.Event()
            readiness_last_message = [""]

            def _ui_show_steam_ready_overlay():
                win._show_join_progress_overlay("Waiting for Steam…")
                readiness_overlay_shown.set()
                return False

            def _steam_ready_progress(event):
                if not isinstance(event, dict) or event.get("type") != "preflight":
                    return
                message = str(event.get("message") or "").strip()
                if message:
                    readiness_last_message[0] = message
                    owned_event = dict(event)
                    owned_event["join_attempt_id"] = int(attempt_id or 0)
                    owned_event["backend_owner"] = "steam_client"
                    progress_event = PreparationProgressEvent.from_authoritative_payload(owned_event)
                    win.GLib.idle_add(deliver_event, progress_event)

            if show_readiness_card:
                win.GLib.idle_add(_ui_show_steam_ready_overlay)
                readiness_overlay_shown.wait(timeout=2.0)

            readiness_started = time.monotonic()
            readiness_ok = wait_for_ugc_ready(
                required_ids,
                cancel_event=operation_cancel_event,
                progress_cb=_steam_ready_progress if report_readiness else None,
                allow_start_steam=bool(allow_backend_steam_start),
                launch_policy=(
                    "backend_allowed" if allow_backend_steam_start else "wait_only"
                ),
            )
            readiness_elapsed = time.monotonic() - readiness_started
            logger.debug(
                "Steam UGC readiness server=%s elapsed=%.3fs success=%s",
                server_identity or server_name or "<unknown>",
                readiness_elapsed,
                bool(readiness_ok),
            )
            if not readiness_ok:
                if operation_cancel_event.is_set():
                    err_msg = (
                        "Steam readiness check cancelled for "
                        f"{server_name or server_identity or 'server'}."
                    )
                else:
                    detail = readiness_last_message[0]
                    err_msg = (
                        "DZLL could not check mods with Steam for "
                        f"{server_name or server_identity or 'this server'}."
                    )
                    if detail:
                        err_msg = f"{err_msg} {detail}"
                logger.error(
                    "Steam UGC readiness failed before required mod state query: %s",
                    err_msg,
                )
                if show_readiness_card:
                    def _ui_show_steam_ready_error():
                        win._steam_ugc_render_status(err_msg, error=True)
                        win._mod_download_backend_active = ""
                        try:
                            win.steamcmd_cancel_btn.set_label("Close")
                        except Exception:
                            pass
                        return False

                    win.GLib.idle_add(_ui_show_steam_ready_error)
                elif manage_join_presentation:
                    win.GLib.idle_add(win._set_updating, False, err_msg)
                raise RuntimeError(err_msg)

            if show_readiness_card:
                win.GLib.idle_add(win._show_join_progress_overlay, "Checking & Preparing Mods for Join...")

            logger.debug(
                "Steam UGC checking required mod readiness: %d ids",
                len(required_ids),
            )
            initial_query_ok, ugc_state, refresh_result = (
                refresh_subscribed_ugc_state_checked(
                    required_ids,
                    cancel_event=operation_cancel_event,
                )
            )
            ugc_state = ugc_state if isinstance(ugc_state, dict) else {}
            refresh_result = (
                refresh_result if isinstance(refresh_result, dict) else {}
            )
            if attempt_id:
                win._join_log(
                    attempt_id,
                    "UGC subscribed metadata refresh completed",
                    success=bool(initial_query_ok),
                    count=len(ugc_state),
                    refreshed=list(refresh_result.get("refreshed") or []),
                    failed=list(refresh_result.get("failed") or []),
                    timed_out=list(refresh_result.get("timed_out") or []),
                )
            refreshed_ids = list(refresh_result.get("refreshed") or [])
            failed_ids = list(refresh_result.get("failed") or [])
            timed_out_ids = list(refresh_result.get("timed_out") or [])
            if initial_query_ok and (failed_ids or timed_out_ids):
                failure_reason_counts = {}
                for failure in refresh_result.get("failures") or []:
                    if not isinstance(failure, dict):
                        continue
                    reason = str(failure.get("reason") or "").strip()
                    if reason:
                        failure_reason_counts[reason] = (
                            failure_reason_counts.get(reason, 0) + 1
                        )
                reason_summary = ""
                if failure_reason_counts:
                    reason_summary = "; failure_reasons={%s}" % ", ".join(
                        f"{reason}: {count}"
                        for reason, count in sorted(
                            failure_reason_counts.items()
                        )
                    )
                logger.debug(
                    "Steam UGC metadata refresh partial: refreshed=%d/%d, "
                    "failed=%d, timed_out=%d; using complete final state%s",
                    len(refreshed_ids),
                    len(list(refresh_result.get("subscribed") or [])),
                    len(failed_ids),
                    len(timed_out_ids),
                    reason_summary,
                )
            if not initial_query_ok:
                logger.warning(
                    "Steam UGC authoritative final state incomplete: "
                    "received=%d/%d; preparation will fail closed",
                    len(ugc_state),
                    len(required_ids),
                )
                if operation_cancel_event.is_set():
                    raise RuntimeError("Steam UGC state check cancelled.")
                raise RuntimeError(
                    "Could not obtain Steam UGC state for required mod(s)."
                )
            blocked_missing = []
            blocked_unknown = []
            steam_client_work_ids = []
            steam_client_work_seen = set()
            queue_missing = []
            queue_subscribed_not_installed = []
            queue_needs_update = []

            def add_steam_client_work_id(mid):
                try:
                    mid_i = int(mid)
                except Exception:
                    return
                if mid_i > 0 and mid_i not in steam_client_work_seen:
                    steam_client_work_ids.append(mid_i)
                    steam_client_work_seen.add(mid_i)

            for mid in required_ids:
                state = ugc_state.get(int(mid))
                if state is None:
                    if auto_install_missing:
                        add_steam_client_work_id(mid)
                        queue_missing.append(int(mid))
                    else:
                        blocked_unknown.append(int(mid))
                    continue
                ready = ugc_item_ready(state)
                installed = bool(state.get("installed", False))
                needs_update = bool(state.get("needs_update", False))
                downloading = bool(state.get("downloading", False))
                download_pending = bool(state.get("download_pending", False))
                logger.debug(
                    "Steam UGC item %d ready=%s installed=%s needs_update=%s "
                    "downloading=%s pending=%s",
                    int(mid), ready, installed, needs_update, downloading,
                    download_pending,
                )
                if ready:
                    continue
                if not installed:
                    if auto_install_missing:
                        add_steam_client_work_id(mid)
                        if bool(state.get("subscribed", False)):
                            queue_subscribed_not_installed.append(int(mid))
                        else:
                            queue_missing.append(int(mid))
                    else:
                        blocked_missing.append(int(mid))
                elif needs_update or downloading or download_pending:
                    add_steam_client_work_id(mid)
                    queue_needs_update.append(int(mid))
                else:
                    add_steam_client_work_id(mid)

            if blocked_unknown:
                ok = False
                err_msg = (
                    "Could not verify Steam UGC readiness for required mod(s): "
                    f"{blocked_unknown}"
                )
            elif blocked_missing:
                ok = False
                err_msg = (
                    "Required Steam UGC mod(s) are not installed and auto-install is disabled: "
                    f"{blocked_missing}"
                )
            elif steam_client_work_ids:
                download_ids = steam_client_work_ids
                status_msg = f"Checking {len(download_ids)} required Steam UGC mod(s)…"
                logger.debug(
                    "Steam UGC required update/download ids: %s", download_ids,
                )
                kinds = []
                if queue_missing:
                    kinds.append("missing")
                if queue_subscribed_not_installed:
                    kinds.append("subscribed but not installed")
                if queue_needs_update:
                    kinds.append("needs update")
                classification = "mixed" if len(kinds) > 1 else (kinds[0] if kinds else "not ready")
                if attempt_id:
                    win._join_log(
                        attempt_id,
                        "UGC queue classified",
                        classification=classification,
                        missing=queue_missing,
                        subscribed_not_installed=queue_subscribed_not_installed,
                        needs_update=queue_needs_update,
                    )
            else:
                logger.debug(
                    "Steam UGC required mods already ready: %d ids",
                    len(required_ids),
                )
                status_msg = "No mod downloads required for this join."
                if attempt_id:
                    win._join_log(attempt_id, "UGC initial all-items-ready", count=len(required_ids))
        else:
            status_msg = "Mod download handling disabled."

        if ok and mod_management_enabled and download_ids:
            did_work = True
            logger.debug("Join %s download ids: %s", backend, download_ids)

            work_set_event = PreparationProgressEvent.from_authoritative_payload({
                "type": "presentation_work_set",
                "join_attempt_id": int(attempt_id or 0),
                "backend": "steam_ugc",
                "backend_owner": "steam_client",
                "work_ids": list(download_ids),
            })
            win.GLib.idle_add(deliver_event, work_set_event)

            if attempt_id and manage_join_presentation:
                win._join_popup_initialize_download_counter(
                    attempt_id,
                    download_ids,
                    backend=backend,
                )

            win._steamcmd_total_missing = int(len(download_ids))

            reset_done = win.threading.Event()

            def _ui_reset_steamcmd_state():
                try:
                    win._steamcmd_reset_state_for_new_run()
                finally:
                    reset_done.set()
                return False

            win.GLib.idle_add(_ui_reset_steamcmd_state)
            reset_done.wait(timeout=2.0)

            if manage_join_presentation:
                try:
                    win.GLib.idle_add(win.steamcmd_spinner.set_spinning, False)
                except Exception:
                    pass

            if manage_join_presentation:
                overlay_shown = win.threading.Event()

                def _ui_show_steam_client():
                    win._show_steam_client_download_overlay(status_msg)
                    overlay_shown.set()
                    return False

                win.GLib.idle_add(_ui_show_steam_client)
                overlay_shown.wait(timeout=2.0)

            if manage_join_presentation:
                win.GLib.idle_add(win._set_updating, False)

            free_b = win._free_bytes_for_path(effective_workshop_dir)
            if free_b > 0:
                free_gb = free_b / (1024 ** 3)
                if free_gb < 5.0:
                    ok = False
                    err_msg = f"Not enough free disk space in workshop drive ({free_gb:.1f} GB free)."
                    if manage_join_presentation:
                        def _ui_show_low_disk_error():
                            win._steam_ugc_render_status(err_msg, error=True)
                            return False

                        win.GLib.idle_add(_ui_show_low_disk_error)

            if ok:
                try:
                    if manage_join_presence and getattr(win, "_discord", None):
                        win._discord.set_installing_mods(server_name=str(server_name or ""))
                except Exception:
                    pass

                mod_names_by_id = {}
                try:
                    mod_names_by_id = {
                        int(mid): str(name or "").strip()
                        for mid, name in (mods or [])
                        if int(mid) > 0 and str(name or "").strip()
                    }
                except Exception:
                    mod_names_by_id = {}

                ugc_final_ready_logged = [False]

                def _steam_ugc_progress(event):
                    event = dict(event or {})
                    event["join_attempt_id"] = int(attempt_id or 0)
                    event["backend_owner"] = "steam_client"
                    try:
                        mid = int(event.get("id") or 0)
                    except Exception:
                        mid = 0
                    name = mod_names_by_id.get(mid, "")
                    if name:
                        event["name"] = name
                    try:
                        if (
                            not ugc_final_ready_logged[0]
                            and bool(event.get("ready", False))
                            and int(event.get("completed_count") or 0) >= int(event.get("total") or 0) > 0
                        ):
                            ugc_final_ready_logged[0] = True
                            win._join_log(attempt_id, "UGC final item ready", mod_id=mid)
                    except Exception:
                        pass
                    progress_event = PreparationProgressEvent.from_authoritative_payload(event)
                    win.GLib.idle_add(deliver_event, progress_event)

                win._steam_ugc_worker_in_progress = True
                win._mod_download_backend_active = "steam_client"
                try:
                    if attempt_id:
                        win._join_log(attempt_id, "UGC helper start", ids=list(download_ids))
                    ok = win.run_steam_client_install(
                        workshop_dir=effective_workshop_dir,
                        mod_ids=download_ids,
                        cancel_event=operation_cancel_event,
                        stop_waiting_event=stop_waiting_event,
                        progress_cb=_steam_ugc_progress,
                        handoff_cb=collect_cancel_cleanup_handoff,
                        allow_start_steam=bool(allow_backend_steam_start),
                        launch_policy=(
                            "backend_allowed" if allow_backend_steam_start else "wait_only"
                        ),
                        ugc_session=ugc_session,
                        log_fn=(lambda message: win._join_log(attempt_id, "UGC backend", message=message)) if attempt_id else None,
                    )
                    if attempt_id:
                        win._join_log(attempt_id, "UGC helper returned", success=bool(ok))
                finally:
                    win._steam_ugc_worker_in_progress = False
                    win._mod_download_backend_active = ""

            if not ok and manage_join_presentation:
                win.GLib.idle_add(win._hide_steamcmd_auth_overlay)
            win._mod_download_backend_active = ""

            if not ok:
                if bool(operation_cancel_event.is_set()):
                    err_msg = "Mod download cancelled"
                elif not err_msg:
                    err_msg = "Mod download failed"

                try:
                    if manage_join_presence and getattr(win, "_discord", None):
                        win._discord.set_menu()
                except Exception:
                    pass

        else:
            if mod_management_enabled:
                logger.debug("Mod download not needed; proceeding with local mods")
            else:
                logger.debug("Mod download handling disabled; proceeding with local mods")

        if ok:
            if attempt_id:
                win._join_log(attempt_id, "post-UGC continuation beginning")
            effective_workshop_dir = _refresh_effective_workshop_dir_after_backend(effective_workshop_dir, mods)
            logger.debug(
                "Effective Workshop path used for symlinks: %r",
                effective_workshop_dir,
            )

            if mod_management_enabled:
                terminal_failure_message = (
                    "Required mod updates could not be completed. Steam still reports "
                    "one or more required mods as outdated or unfinished. Open Steam "
                    "Downloads, allow the updates to finish, then try Join again."
                )

                def validate_terminal_ugc(phase):
                    reasons = {}
                    try:
                        query_ok, states = query_ugc_state_checked(required_ids)
                    except Exception as exc:
                        query_ok, states = False, {}
                        logger.error("%s Steam UGC validation failed: %s", phase, exc)
                    states = states if isinstance(states, dict) else {}
                    if not query_ok:
                        reasons = {mid: "query failure" for mid in required_ids}
                    else:
                        for mid in required_ids:
                            state = states.get(mid)
                            if not isinstance(state, dict):
                                reasons[mid] = "missing state" if state is None else "unknown"
                                continue
                            required_fields = {
                                "installed", "needs_update", "downloading", "download_pending",
                            }
                            if not required_fields.issubset(state):
                                reasons[mid] = "unknown"
                            elif not bool(state.get("installed", False)):
                                reasons[mid] = "not installed"
                            elif bool(state.get("needs_update", False)):
                                reasons[mid] = "needs update"
                            elif bool(state.get("downloading", False)):
                                reasons[mid] = "downloading"
                            elif bool(state.get("download_pending", False)):
                                reasons[mid] = "pending"
                            elif not ugc_item_ready(state):
                                reasons[mid] = "unknown"
                    unresolved = [mid for mid in required_ids if mid in reasons]
                    logger.debug(
                        "%s Steam UGC validation success=%s unresolved=%s reasons=%s",
                        phase, bool(query_ok and not unresolved), unresolved, reasons,
                    )
                    if attempt_id:
                        win._join_log(
                            attempt_id,
                            f"{phase} UGC validation",
                            success=bool(query_ok and not unresolved),
                            unresolved=unresolved,
                            reasons=reasons,
                        )
                    return bool(query_ok and not unresolved), unresolved

                terminal_current, unresolved_ids = validate_terminal_ugc(
                    "first terminal",
                )
                if not terminal_current:
                    retry_message = (
                        "Required mod update did not complete. Retrying once, please wait…"
                    )
                    retry_event = PreparationProgressEvent.from_authoritative_payload({
                        "type": "status",
                        "join_attempt_id": int(attempt_id or 0),
                        "backend": "steam_ugc",
                        "backend_owner": "steam_client",
                        "message": retry_message,
                    })
                    win.GLib.idle_add(deliver_event, retry_event)
                    if manage_join_presentation:
                        win.GLib.idle_add(win._show_join_progress_overlay, retry_message)
                    if attempt_id:
                        win._join_log(
                            attempt_id,
                            "terminal UGC retry starting",
                            unresolved=list(unresolved_ids),
                        )

                    retry_names_by_id = {
                        int(mid): str(name or "").strip()
                        for mid, name in (mods or [])
                        if int(mid) > 0 and str(name or "").strip()
                    }

                    def _retry_progress(event):
                        event = dict(event or {})
                        event["join_attempt_id"] = int(attempt_id or 0)
                        event["backend_owner"] = "steam_client"
                        try:
                            event_mid = int(event.get("id") or 0)
                        except Exception:
                            event_mid = 0
                        event_name = retry_names_by_id.get(event_mid, "")
                        if event_name:
                            event["name"] = event_name
                        win.GLib.idle_add(
                            deliver_event,
                            PreparationProgressEvent.from_authoritative_payload(event),
                        )

                    did_work = True
                    win._steam_ugc_worker_in_progress = True
                    win._mod_download_backend_active = "steam_client"
                    try:
                        retry_ok = win.run_steam_client_install(
                            workshop_dir=effective_workshop_dir,
                            mod_ids=unresolved_ids,
                            cancel_event=operation_cancel_event,
                            stop_waiting_event=stop_waiting_event,
                            progress_cb=_retry_progress,
                            handoff_cb=collect_cancel_cleanup_handoff,
                            allow_start_steam=bool(
                                allow_backend_steam_start
                            ),
                            launch_policy=(
                                "backend_allowed"
                                if allow_backend_steam_start else "wait_only"
                            ),
                            ugc_session=ugc_session,
                            log_fn=(
                                lambda message: win._join_log(
                                    attempt_id, "UGC retry backend", message=message,
                                )
                            ) if attempt_id else None,
                        )
                    finally:
                        win._steam_ugc_worker_in_progress = False
                        win._mod_download_backend_active = ""

                    if not retry_ok:
                        ok = False
                        if operation_cancel_event.is_set():
                            err_msg = "Mod download cancelled"
                        else:
                            err_msg = terminal_failure_message
                        if attempt_id:
                            win._join_log(
                                attempt_id,
                                "terminal UGC retry backend failed",
                                cancelled=bool(operation_cancel_event.is_set()),
                                unresolved=list(unresolved_ids),
                            )
                    else:
                        effective_workshop_dir = _refresh_effective_workshop_dir_after_backend(
                            effective_workshop_dir, mods,
                        )
                        logger.debug(
                            "Effective Workshop path refreshed after terminal retry: %r",
                            effective_workshop_dir,
                        )
                        final_current, final_unresolved = validate_terminal_ugc(
                            "final terminal",
                        )
                        if not final_current:
                            ok = False
                            err_msg = terminal_failure_message
                            if attempt_id:
                                win._join_log(
                                    attempt_id,
                                    "terminal UGC validation failed closed",
                                    unresolved=list(final_unresolved),
                                )

            if ok:
                missing_after = win.compute_missing_mods(effective_workshop_dir, mods)
                if attempt_id:
                    win._join_log(attempt_id, "post-download filesystem verification completed",
                                  missing=[int(mid) for mid, _name in (missing_after or [])])

                if missing_after:
                    ok = False
                    err_msg = f"Required mods still missing after install: {[mid for mid, _ in missing_after]}"

    except UGCHelperReapError as exc:
        helper_reap_error = exc
        ok = False
        err_msg = str(exc)
    except Exception as e:
        if not manage_join_presentation:
            print(
                "[BACKGROUND PREPARE] Shared preparation exception "
                f"server={server_identity or server_name or '<unknown>'}: {e}",
                file=sys.stderr,
            )
            traceback.print_exc()
        ok = False
        err_msg = str(e)

    ugc_shutdown_confirmed = False
    if ugc_session is not None:
        try:
            ugc_session.close()
            # CooperativeUGCSession owns this transition; keep the caller-side
            # discard as an idempotent compatibility guard for session doubles.
            unregister_owned_ugc_session(ugc_session)
            ugc_shutdown_confirmed = True
            if attempt_id:
                win._join_log(
                    attempt_id,
                    "UGC helper shutdown completed",
                    success=True,
                    cancelled=bool(operation_cancel_event.is_set()),
                )
        except UGCHelperReapError as exc:
            raise
        except Exception as exc:
            secondary_error = f"Steam UGC session shutdown failed: {exc}"
            print(
                f"[JOIN] Secondary cleanup diagnostic: {secondary_error}",
                file=sys.stderr,
            )
            if attempt_id:
                win._join_log(
                    attempt_id,
                    "secondary Steam UGC cleanup failure",
                    error=str(exc),
                    preparation_ok=bool(ok),
                )
            # The helper has already been reaped here.  Preserve a substantive
            # operation failure, and do not turn confirmed preparation success
            # into a false failure solely because teardown diagnostics failed.
        finally:
            deactivate_ugc_session(ugc_session)
    if operation_cancel_event.is_set():
        with cancel_cleanup_lock:
            deferred_cleanup_ids = sorted(cancel_cleanup_ids)
        if deferred_cleanup_ids and ugc_shutdown_confirmed:
            try:
                cleanup_result = cleanup_cancelled_ugc_subscriptions(
                    deferred_cleanup_ids,
                )
                logger.debug(
                    "Fresh-context Steam UGC cancel cleanup result: %s",
                    cleanup_result,
                )
                if attempt_id:
                    win._join_log(
                        attempt_id,
                        "fresh-context Steam UGC cancel cleanup completed",
                        **cleanup_result,
                    )
            except Exception as exc:
                logger.warning(
                    "Fresh-context Steam UGC cancel cleanup failed for ids=%s: %s",
                    deferred_cleanup_ids,
                    exc,
                )
                if attempt_id:
                    win._join_log(
                        attempt_id,
                        "fresh-context Steam UGC cancel cleanup failed",
                        ids=deferred_cleanup_ids,
                        error=str(exc),
                    )
        elif deferred_cleanup_ids:
            logger.warning(
                "Skipping fresh-context Steam UGC cancel cleanup because the "
                "downloader context did not confirm clean shutdown; ids=%s",
                deferred_cleanup_ids,
            )
            if attempt_id:
                win._join_log(
                    attempt_id,
                    "fresh-context Steam UGC cancel cleanup skipped",
                    ids=deferred_cleanup_ids,
                    reason="cooperative_shutdown_unconfirmed",
                )
    if helper_reap_error is not None:
        raise helper_reap_error

    if ok:
        outcome = PreparationOutcome(
            PreparationStatus.READY,
            reason="ready",
            backend=backend,
            effective_workshop_path=effective_workshop_dir,
            verified_mods=mods_for_launch,
            did_work=did_work,
        )
    else:
        cancelled = bool(operation_cancel_event.is_set())
        outcome = PreparationOutcome(
            PreparationStatus.CANCELLED if cancelled else PreparationStatus.FAILED,
            reason="cancelled" if cancelled else "failed",
            error=str(err_msg or ""),
            backend=backend,
            effective_workshop_path=effective_workshop_dir,
            did_work=did_work,
        )
    presenter.on_terminal(outcome)
    return outcome


def join_prepare_and_launch(win, obj, mods, workshop_dir, proton_prefix,
                            watch_folder_linux, mod_management_enabled,
                            auto_install_missing, *, attempt_id=0):
    consume_event = getattr(win, "_steam_ugc_progress_to_overlay", None)
    if consume_event is None:
        consume_event = win._steam_ugc_progress_from_worker
    join_presenter = JoinPopupPreparationPresenter(consume_event=consume_event)
    operation_cancel_event = win._join_preparation_cancel_event

    def continuation_cancelled() -> bool:
        if operation_cancel_event.is_set():
            return True
        return bool(
            attempt_id and not win._join_attempt_is_active(attempt_id)
        )

    outcome = prepare_required_mods(
        win, mods, workshop_dir, mod_management_enabled, auto_install_missing,
        operation_id=attempt_id,
        presenter=join_presenter,
        server_name=str(getattr(obj, "name", "") or ""),
        allow_backend_steam_start=bool(
            getattr(
                getattr(win, "_steam_start_consent_result", None),
                "backend_may_launch",
                False,
            )
        ),
        cancel_event=operation_cancel_event,
    )
    ok = outcome.status in (PreparationStatus.READY, PreparationStatus.NO_REQUIRED_MODS)
    err_msg = outcome.error or None
    selected_mod_win_paths_for_launch = []

    try:
        if continuation_cancelled():
            ok = False
            err_msg = err_msg or "Join cancelled."
        if outcome.status is PreparationStatus.READY:
            if not ok or continuation_cancelled():
                ok = False
                err_msg = err_msg or "Join cancelled."
                link_info = {
                    "created": [], "updated": [], "kept": [], "removed": [],
                    "errors": [], "selected_paths": [],
                }
                mods_for_launch = []
            else:
                mods_for_launch = list(outcome.verified_mods)
                effective_workshop_dir = outcome.effective_workshop_path
                link_info = win.ensure_watch_symlinks(
                    workshop_dir=effective_workshop_dir,
                    mods=mods_for_launch,
                    watch_folder=watch_folder_linux,
                    cleanup_stale=True,
                )
            if continuation_cancelled():
                ok = False
                err_msg = err_msg or "Join cancelled."
            logger.debug(
                "Join symlink result: created=%d updated=%d kept=%d removed=%d errors=%d",
                len(link_info["created"]), len(link_info["updated"]),
                len(link_info["kept"]), len(link_info["removed"]),
                len(link_info["errors"]),
            )
            if attempt_id:
                win._join_log(attempt_id, "symlink preparation completed",
                              created=len(link_info["created"]), updated=len(link_info["updated"]),
                              errors=len(link_info["errors"]))

            if link_info.get("errors"):
                selected_paths = link_info.get("selected_paths", [])
                if len(selected_paths) < len(mods_for_launch):
                    ok = False
                    err_msg = "Failed to create all watch-folder symlinks"
                else:
                    logger.warning("Join symlink warnings: %s", link_info.get("errors"))

            if ok and not continuation_cancelled():
                validation_errors = workshop_mods.validate_selected_watch_symlinks(
                    selected_paths=(link_info or {}).get("selected_paths", []),
                    mods=mods_for_launch,
                )
                if validation_errors:
                    for line in validation_errors:
                        logger.error("Invalid mod path before launch: %s", line)
                    ok = False
                    err_msg = "Invalid required mod path(s) before launch: " + "; ".join(validation_errors)

            if continuation_cancelled():
                ok = False
                err_msg = err_msg or "Join cancelled."

            if ok and not continuation_cancelled():
                installed_mods_for_local = win.scan_installed_mods_in_watch_folder(watch_folder_linux)
                selected_mods_for_preset = (link_info or {}).get("selected_paths", [])
                if continuation_cancelled():
                    ok = False
                    err_msg = err_msg or "Join cancelled."
                else:
                    paths = win.bootstrap_launcher_state(
                        proton_prefix=proton_prefix,
                        watch_folder_linux=watch_folder_linux,
                        installed_mod_linux_paths=installed_mods_for_local,
                        selected_mod_linux_paths=selected_mods_for_preset,
                    )
                    logger.debug("Join launcher state written")
                    if attempt_id:
                        win._join_log(
                            attempt_id,
                            "preset/launcher-state preparation completed",
                        )

                    selected_mod_win_paths_for_launch = [
                        wp
                        for wp in (
                            linux_to_win_path_under_prefix(
                                p, proton_prefix=proton_prefix,
                            )
                            for p in selected_mods_for_preset
                        )
                        if wp
                    ]
        elif outcome.status is PreparationStatus.NO_REQUIRED_MODS:
            if continuation_cancelled():
                ok = False
                err_msg = err_msg or "Join cancelled."
            else:
                installed_mods_for_local = win.scan_installed_mods_in_watch_folder(watch_folder_linux)
                if continuation_cancelled():
                    ok = False
                    err_msg = err_msg or "Join cancelled."
                else:
                    paths = win.bootstrap_launcher_state(
                        proton_prefix=proton_prefix,
                        watch_folder_linux=watch_folder_linux,
                        installed_mod_linux_paths=installed_mods_for_local,
                        selected_mod_linux_paths=[],
                    )
                    logger.debug("Join launcher state cleared for no-mod server")
                    if attempt_id:
                        win._join_log(
                            attempt_id,
                            "preset/launcher-state preparation completed",
                        )
    except Exception as e:
        ok = False
        err_msg = str(e)

    def after():
        if attempt_id and not win._join_attempt_is_active(attempt_id):
            return False
        if continuation_cancelled():
            try:
                win._steam_ugc_render_status(
                    err_msg or "Join cancelled.", error=True,
                )
                win._mod_download_backend_active = ""
                win.steamcmd_cancel_btn.set_label("Close")
                win.steamcmd_cancel_btn.set_visible(True)
            except Exception:
                pass
            if attempt_id:
                win._cleanup_join_attempt(
                    attempt_id, "cancelled before launch continuation",
                )
            win._set_updating(False)
            win._on_filter_changed()
            return False
        if attempt_id:
            win._join_log(attempt_id, "continuation executed")
        win._set_updating(False)

        if not ok:
            try:
                if getattr(win, "_discord", None):
                    win._discord.set_menu()
            except Exception:
                pass
            try:
                win._steam_ugc_render_status(err_msg or "Join aborted.", error=True)
                win._mod_download_backend_active = ""
                win.steamcmd_cancel_btn.set_label("Close")
                win.steamcmd_cancel_btn.set_visible(True)
            except Exception:
                pass
            logger.error("Aborting Join launch: %s", err_msg or "unknown error")
            if attempt_id:
                win._cleanup_join_attempt(attempt_id, f"preparation failed: {err_msg or 'unknown error'}")
            win._on_filter_changed()
            return False

        if continuation_cancelled():
            return False
        win._join_popup_show_launching(attempt_id)
        if continuation_cancelled():
            return False
        result = win._launch_direct_steam_url(obj, selected_mod_win_paths_for_launch, attempt_id=attempt_id)
        win._on_filter_changed()
        return False

    if attempt_id:
        win._join_log(attempt_id, "continuation scheduled", success=bool(ok))
    win.GLib.idle_add(after)
