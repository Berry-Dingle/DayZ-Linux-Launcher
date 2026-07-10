#!/usr/bin/env python3
import os

from . import steamcmd_mods
from .launcher_state import linux_to_win_path_under_prefix
from .steam_native import dayz_paths_summary, dayz_workshop_content_dir
from .steam_ugc_backend import query_ugc_state, ugc_item_ready, wait_for_ugc_ready


def _resolve_path(path):
    path = str(path or "").strip()
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(path))


def _missing_ids_for(workshop_dir, mods):
    return {int(mid) for mid, _name in (steamcmd_mods.compute_missing_mods(workshop_dir, mods) or [])}


def _maybe_autodetect_workshop_dir():
    resolved = _resolved_dayz_workshop_dir()
    if resolved:
        return resolved
    try:
        return _resolve_path(steamcmd_mods.autodetect_workshop_dir() or "")
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


def _choose_initial_workshop_dir(configured_workshop_dir, mods, backend):
    configured = _resolve_path(configured_workshop_dir)
    resolved = _resolved_dayz_workshop_dir()
    autodetected = resolved or _maybe_autodetect_workshop_dir()
    effective = configured

    if resolved:
        effective = resolved
        if configured and os.path.realpath(configured) != os.path.realpath(resolved):
            print(f"[JOIN] DayZ Steam library workshop path selected for {backend}: {resolved!r} (configured={configured!r})")
    elif autodetected and not effective:
        effective = autodetected
    elif autodetected and configured and os.path.realpath(autodetected) != os.path.realpath(configured):
        try:
            configured_missing = _missing_ids_for(configured, mods) if os.path.isdir(configured) else {int(mid) for mid, _ in (mods or [])}
            autodetected_missing = _missing_ids_for(autodetected, mods)
            if len(autodetected_missing) < len(configured_missing):
                print(
                    f"[JOIN] Workshop path refresh selected for {backend}: "
                    f"configured={configured!r} autodetected={autodetected!r} "
                    f"configured_missing={sorted(configured_missing)} "
                    f"autodetected_missing={sorted(autodetected_missing)}"
                )
                effective = autodetected
        except Exception as e:
            print(f"[JOIN] Workshop path comparison failed: {e}")

    return effective or configured


def _refresh_effective_workshop_dir_after_backend(current_workshop_dir, mods, backend):
    current = _resolve_path(current_workshop_dir)

    if backend == "steamcmd":
        candidate = _resolve_path(getattr(steamcmd_mods, "LAST_EFFECTIVE_WORKSHOP_DIR", "") or "")
        if candidate and os.path.isdir(candidate):
            if current and os.path.realpath(candidate) != os.path.realpath(current):
                print(f"[JOIN] SteamCMD effective workshop path propagated: {current!r} -> {candidate!r}")
            return candidate

    autodetected = _maybe_autodetect_workshop_dir()
    if autodetected and current and os.path.realpath(autodetected) != os.path.realpath(current):
        try:
            current_missing = _missing_ids_for(current, mods) if os.path.isdir(current) else {int(mid) for mid, _ in (mods or [])}
            autodetected_missing = _missing_ids_for(autodetected, mods)
            if len(autodetected_missing) < len(current_missing):
                print(
                    f"[JOIN] {backend} effective workshop path refreshed after backend: "
                    f"{current!r} -> {autodetected!r} "
                    f"current_missing={sorted(current_missing)} "
                    f"autodetected_missing={sorted(autodetected_missing)}"
                )
                return autodetected
        except Exception as e:
            print(f"[JOIN] Post-backend workshop path comparison failed: {e}")

    return current


def join_prepare_and_launch(win, obj, mods, workshop_dir, steamcmd_path, steam_user, validate, dry,
                            proton_prefix, watch_folder_linux, use_steamcmd, mod_download_backend,
                            auto_install_missing, auto_update_required):
    ok = True
    err_msg = None
    selected_mod_win_paths_for_launch = []

    try:
        # Decide what the selected Workshop backend should do for this join.
        required_ids = [mid for (mid, _name) in (mods or [])]
        backend = mod_download_backend if mod_download_backend in ("steam_client", "steamcmd") else "steam_client"
        configured_workshop_dir = _resolve_path(workshop_dir)
        effective_workshop_dir = _choose_initial_workshop_dir(configured_workshop_dir, mods, backend)
        print(f"[JOIN] selected backend: {backend}")
        try:
            dayz_summary = dayz_paths_summary()
            dayz_library = str(dayz_summary.get("dayz_library") or "")
            if dayz_library:
                print(f"[JOIN] resolved DayZ library: {dayz_library}")
            else:
                print("[JOIN] DayZ Steam library not detected; using configured/default paths")
        except Exception as exc:
            print(f"[JOIN] DayZ Steam library not detected; using configured/default paths ({exc})")
        print(f"[JOIN] configured workshop path: {configured_workshop_dir!r}")
        print(f"[JOIN] effective workshop path used for checks/downloads: {effective_workshop_dir!r}")
        print(f"[JOIN] resolved Proton prefix: {_resolve_path(proton_prefix)!r}")

        missing = win.compute_missing_mods(effective_workshop_dir, mods)
        missing_ids = [mid for (mid, _name) in (missing or [])]

        download_ids = []
        status_msg = ""

        if use_steamcmd:
            if backend == "steam_client":
                show_readiness_card = bool(getattr(win, "_join_steam_start_allowed", False))
                readiness_overlay_shown = win.threading.Event()

                def _ui_show_steam_ready_overlay():
                    win._show_join_progress_overlay("Waiting for Steam...")
                    readiness_overlay_shown.set()
                    return False

                def _steam_ready_progress(event):
                    if not isinstance(event, dict) or event.get("type") != "preflight":
                        return
                    message = str(event.get("message") or "").strip()
                    if message:
                        win._steam_ugc_progress_from_worker(event)

                if show_readiness_card:
                    win.GLib.idle_add(_ui_show_steam_ready_overlay)
                    readiness_overlay_shown.wait(timeout=2.0)

                if not wait_for_ugc_ready(
                    required_ids,
                    progress_cb=_steam_ready_progress if show_readiness_card else None,
                    allow_start_steam=bool(getattr(win, "_join_steam_start_allowed", False)),
                ):
                    err_msg = "DZLL could not check mods with Steam."
                    print(f"[JOIN] Steam UGC readiness failed before required mod state query: {err_msg}")
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
                    else:
                        win.GLib.idle_add(win._set_updating, False, err_msg)
                    raise RuntimeError(err_msg)

                if show_readiness_card:
                    win.GLib.idle_add(win._hide_steamcmd_auth_overlay)
                    win.GLib.idle_add(win._show_join_progress_overlay, "Preparing required mods...")

                print(f"[JOIN] Steam UGC checking required mod readiness: {len(required_ids)} ids")
                ugc_state = query_ugc_state(required_ids)
                blocked_missing = []
                blocked_unknown = []
                steam_client_work_ids = []
                steam_client_work_seen = set()

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
                        else:
                            blocked_unknown.append(int(mid))
                        continue
                    ready = ugc_item_ready(state)
                    installed = bool(state.get("installed", False))
                    needs_update = bool(state.get("needs_update", False))
                    downloading = bool(state.get("downloading", False))
                    download_pending = bool(state.get("download_pending", False))
                    print(
                        f"[Steam UGC] item {int(mid)} ready={ready} installed={installed} "
                        f"needs_update={needs_update} downloading={downloading} pending={download_pending}"
                    )
                    if ready:
                        continue
                    if not installed:
                        if auto_install_missing:
                            add_steam_client_work_id(mid)
                        else:
                            blocked_missing.append(int(mid))
                    elif needs_update or downloading or download_pending:
                        add_steam_client_work_id(mid)
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
                    print(f"[Steam UGC] required update/download ids: {download_ids}")
                else:
                    print(f"[JOIN] Steam UGC required mods already ready: {len(required_ids)} ids")
                    status_msg = "No mod downloads required for this join."
            elif backend == "steamcmd" and validate:
                download_ids = required_ids
                status_msg = f"Validating {len(download_ids)} required mod(s)…"
            elif auto_update_required:
                download_ids = required_ids
                status_msg = f"Updating {len(download_ids)} required mod(s)…"
            elif auto_install_missing:
                download_ids = missing_ids
                status_msg = f"{len(download_ids)} required mod(s) need downloading."
            else:
                status_msg = "No mod downloads required for this join."
        else:
            status_msg = "Mod download handling disabled."

        if ok and use_steamcmd and download_ids:
            print(f"[JOIN] {backend} download ids: {download_ids}")

            win._steamcmd_total_missing = int(len(download_ids))
            win._steamcmd_done_missing = 0
            win._steamcmd_started_missing = 0
            win._steamcmd_seen_mod_ids = set()

            win._steamcmd_auth_request = None
            win._steamcmd_auth_result = None
            win._steamcmd_auth_wait_count = 0
            win._steamcmd_auth_event = None

            reset_done = win.threading.Event()

            def _ui_reset_steamcmd_state():
                try:
                    win._steamcmd_reset_state_for_new_run()
                finally:
                    reset_done.set()
                return False

            win.GLib.idle_add(_ui_reset_steamcmd_state)
            reset_done.wait(timeout=2.0)

            try:
                win.GLib.idle_add(win.steamcmd_spinner.set_spinning, False)
            except Exception:
                pass

            creds = {"ok": True, "username": "", "password": ""}
            if backend == "steamcmd":
                creds = win._request_steamcmd_credentials_blocking(
                    username_prefill=steam_user,
                    status=status_msg,
                )
            else:
                overlay_shown = win.threading.Event()

                def _ui_show_steam_client():
                    win._show_steam_client_download_overlay(status_msg)
                    overlay_shown.set()
                    return False

                win.GLib.idle_add(_ui_show_steam_client)
                overlay_shown.wait(timeout=2.0)

            if not creds.get("ok"):
                ok = False
                err_msg = "SteamCMD login cancelled"
                win.GLib.idle_add(win._hide_steamcmd_auth_overlay)
            else:
                steam_user_run = str(creds.get("username") or "").strip()
                steam_pass_run = str(creds.get("password") or "")

                win.GLib.idle_add(win._set_updating, False)

                free_b = win._free_bytes_for_path(effective_workshop_dir)
                if free_b > 0:
                    free_gb = free_b / (1024 ** 3)
                    if free_gb < 5.0:
                        ok = False
                        err_msg = f"Not enough free disk space in workshop drive ({free_gb:.1f} GB free)."
                        win.GLib.idle_add(
                            win._steamcmd_overlay_render,
                            "Checking/Updating Required Mods…",
                            "Not enough disk space.",
                            f"{free_gb:.1f} GB free in workshop location.",
                            False,
                        )

                if ok:
                    win.GLib.idle_add(lambda: setattr(win, "_steamcmd_total_sizes", {}) or False)

                    try:
                        sizes = win.fetch_workshop_sizes_bytes(list(download_ids or []), appid=221100, timeout_s=20)
                    except Exception as e:
                        print(f"[SIZES] fetch_workshop_sizes_bytes failed: {e}")
                        sizes = {}

                    def _ui_set_sizes():
                        try:
                            win._steamcmd_total_sizes = {int(k): int(v) for k, v in (sizes or {}).items()}
                        except Exception:
                            win._steamcmd_total_sizes = {}
                        win.GLib.idle_add(win._steamcmd_refresh_active_download_line2)
                        return False

                    win.GLib.idle_add(_ui_set_sizes)

                    try:
                        if getattr(win, "_discord", None):
                            win._discord.set_installing_mods(server_name=str(obj.name or ""))
                    except Exception:
                        pass

                    win._steamcmd_install_in_progress = True
                    win._mod_download_backend_active = backend
                    try:
                        if backend == "steamcmd":
                            ok = win.run_steamcmd_install(
                                steamcmd_path=steamcmd_path,
                                steam_username=steam_user_run,
                                steam_password=steam_pass_run,
                                workshop_dir=effective_workshop_dir,
                                mod_ids=download_ids,
                                validate=validate,
                                max_concurrent=1,
                                dry_run=dry,
                                log_fn=None,
                                line_cb=win._steamcmd_install_line_from_worker,
                                cancel_event=win._steamcmd_cancel_event,
                            )
                        else:
                            mod_names_by_id = {}
                            try:
                                mod_names_by_id = {
                                    int(mid): str(name or "").strip()
                                    for mid, name in (mods or [])
                                    if int(mid) > 0 and str(name or "").strip()
                                }
                            except Exception:
                                mod_names_by_id = {}

                            def _steam_client_state(mid, index, total):
                                win._steamcmd_active_mid = int(mid)
                                win._steamcmd_last_progress_bytes = 0

                            def _steam_ugc_progress(event):
                                try:
                                    mid = int(event.get("id") or 0)
                                except Exception:
                                    mid = 0
                                name = mod_names_by_id.get(mid, "")
                                if name:
                                    event = dict(event)
                                    event["name"] = name
                                win._steam_ugc_progress_from_worker(event)

                            ok = win.run_steam_client_install(
                                workshop_dir=effective_workshop_dir,
                                mod_ids=download_ids,
                                cancel_event=win._steamcmd_cancel_event,
                                state_cb=_steam_client_state,
                                progress_cb=_steam_ugc_progress,
                                handoff_cb=None,
                                allow_start_steam=bool(getattr(win, "_join_steam_start_allowed", False)),
                            )
                    finally:
                        win._steamcmd_install_in_progress = False
                        win._mod_download_backend_active = ""

                win.GLib.idle_add(win._hide_steamcmd_auth_overlay)
                win._mod_download_backend_active = ""

                if not ok:
                    if bool(win._steamcmd_cancel_event.is_set()):
                        err_msg = "Mod download cancelled"
                    else:
                        err_msg = "Mod download failed"

                    try:
                        if getattr(win, "_discord", None):
                            win._discord.set_menu()
                    except Exception:
                        pass

        else:
            if use_steamcmd:
                print("[JOIN] Mod download not needed for this join; proceeding with local mods only")
            else:
                print("[JOIN] Mod download handling disabled; proceeding with local mods only")

        mods_for_launch = mods

        if ok:
            win.GLib.idle_add(win._show_join_progress_overlay, "Preparing required mods...")
            effective_workshop_dir = _refresh_effective_workshop_dir_after_backend(effective_workshop_dir, mods, backend)
            print(f"[JOIN] effective workshop path used for symlinks: {effective_workshop_dir!r}")

            missing_after = win.compute_missing_mods(effective_workshop_dir, mods)

            if missing_after:
                denied_ids = (
                    set(getattr(steamcmd_mods, "LAST_ACCESS_DENIED_IDS", set()))
                    if backend == "steamcmd"
                    else set()
                )

                unresolved = [pair for pair in missing_after if int(pair[0]) not in denied_ids]

                if unresolved:
                    ok = False
                    err_msg = f"Required mods still missing after install: {[mid for mid, _ in unresolved]}"
                else:
                    mods_for_launch = [pair for pair in mods if int(pair[0]) not in denied_ids]
                    print(f"[JOIN] Skipping inaccessible mods: {sorted(denied_ids)}")

        link_info = None
        if ok:
            win.GLib.idle_add(win._show_join_progress_overlay, "Preparing required mods...")
            link_info = win.ensure_watch_symlinks(
                workshop_dir=effective_workshop_dir,
                mods=mods_for_launch,
                watch_folder=watch_folder_linux,
                cleanup_stale=True,
            )
            print(
                f"[JOIN] symlink result: created={len(link_info['created'])} "
                f"updated={len(link_info['updated'])} kept={len(link_info['kept'])} "
                f"removed={len(link_info['removed'])} errors={len(link_info['errors'])}"
            )

            if link_info.get("errors"):
                selected_paths = link_info.get("selected_paths", [])
                if len(selected_paths) < len(mods_for_launch):
                    ok = False
                    err_msg = "Failed to create all watch-folder symlinks"
                else:
                    print(f"[JOIN] Symlink warnings: {link_info.get('errors')}")

        if ok:
            validation_errors = steamcmd_mods.validate_selected_watch_symlinks(
                selected_paths=(link_info or {}).get("selected_paths", []),
                mods=mods_for_launch,
            )
            if validation_errors:
                for line in validation_errors:
                    print(f"[JOIN] Invalid mod path before launch: {line}")
                ok = False
                err_msg = "Invalid required mod path(s) before launch: " + "; ".join(validation_errors)

        if ok:
            win.GLib.idle_add(win._show_join_progress_overlay, "Preparing required mods...")
            installed_mods_for_local = win.scan_installed_mods_in_watch_folder(watch_folder_linux)
            selected_mods_for_preset = (link_info or {}).get("selected_paths", [])
            paths = win.bootstrap_launcher_state(
                proton_prefix=proton_prefix,
                watch_folder_linux=watch_folder_linux,
                installed_mod_linux_paths=installed_mods_for_local,
                selected_mod_linux_paths=selected_mods_for_preset,
            )
            print(f"[JOIN] launcher state written: {paths}")

            selected_mod_win_paths_for_launch = [
                wp
                for wp in (
                    linux_to_win_path_under_prefix(p, proton_prefix=proton_prefix)
                    for p in selected_mods_for_preset
                )
                if wp
            ]

    except Exception as e:
        ok = False
        err_msg = str(e)

    def after():
        win._set_updating(False)

        if not ok:
            try:
                if getattr(win, "_discord", None):
                    win._discord.set_menu()
            except Exception:
                pass
            if getattr(win, "_pending_server_companion_obj", None) is obj:
                win._pending_server_companion_obj = None
            try:
                win._steam_ugc_render_status(err_msg or "Join aborted.", error=True)
                win._mod_download_backend_active = ""
                win.steamcmd_cancel_btn.set_label("Close")
                win.steamcmd_cancel_btn.set_visible(True)
            except Exception:
                pass
            print(f"[JOIN] Aborting launch: {err_msg or 'unknown error'}")
            win._on_filter_changed()
            return False

        win._show_join_progress_overlay("Launching DayZ...")
        win._launch_direct_steam_url(obj, selected_mod_win_paths_for_launch)
        win._hide_steamcmd_auth_overlay()
        win._on_filter_changed()
        return False

    win.GLib.idle_add(after)
