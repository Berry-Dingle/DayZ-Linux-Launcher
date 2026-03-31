#!/usr/bin/env python3


def join_prepare_and_launch(win, obj, mods, workshop_dir, steamcmd_path, steam_user, validate, dry,
                            proton_prefix, watch_folder_linux, use_steamcmd,
                            auto_install_missing, auto_update_required):
    ok = True
    err_msg = None

    try:
        # Decide what SteamCMD should do for THIS join
        required_ids = [mid for (mid, _name) in (mods or [])]
        missing = win.compute_missing_mods(workshop_dir, mods)
        missing_ids = [mid for (mid, _name) in (missing or [])]

        steamcmd_ids = []
        status_msg = ""

        if use_steamcmd:
            if validate:
                steamcmd_ids = required_ids
                status_msg = f"Verifying {len(steamcmd_ids)} mod(s) (full validation)…"
            elif auto_update_required:
                steamcmd_ids = required_ids
                status_msg = f"Updating {len(steamcmd_ids)} required mod(s) via SteamCMD…"
            elif auto_install_missing:
                steamcmd_ids = missing_ids
                status_msg = f"{len(steamcmd_ids)} required mod(s) need downloading via SteamCMD."
            else:
                steamcmd_ids = []
                status_msg = "SteamCMD not required for this join."
        else:
            steamcmd_ids = []
            status_msg = "SteamCMD mod handling disabled."

        if use_steamcmd and steamcmd_ids:
            print(f"[JOIN] steamcmd ids: {steamcmd_ids}")

            win._steamcmd_total_missing = int(len(steamcmd_ids))
            win._steamcmd_done_missing = 0
            win._steamcmd_started_missing = 0
            win._steamcmd_seen_mod_ids = set()

            # Reset auth/overlay state every run
            win._steamcmd_auth_request = None
            win._steamcmd_auth_result = None
            win._steamcmd_auth_wait_count = 0
            win._steamcmd_auth_event = None

            # Fresh cancel event every run (avoid sticky carry-over)
            win.threading.Event  # sanity keep reference style stable
            win._steamcmd_reset_state_for_new_run()

            # Ensure login form is visible
            try:
                win.GLib.idle_add(win.steamcmd_spinner.set_spinning, False)
            except Exception:
                pass

            creds = win._request_steamcmd_credentials_blocking(
                username_prefill=steam_user,
                status=status_msg,
            )

            if not creds.get("ok"):
                ok = False
                err_msg = "SteamCMD login cancelled"
                win.GLib.idle_add(win._hide_steamcmd_auth_overlay)
            else:
                steam_user_run = str(creds.get("username") or "").strip()
                steam_pass_run = str(creds.get("password") or "")

                win.GLib.idle_add(win._set_updating, False)

                free_b = win._free_bytes_for_path(workshop_dir)
                if free_b > 0:
                    free_gb = free_b / (1024 ** 3)
                    if free_gb < 5.0:
                        ok = False
                        err_msg = f"Not enough free disk space in workshop drive ({free_gb:.1f} GB free)."
                        win.GLib.idle_add(
                            win._steamcmd_overlay_render,
                            "Downloading Required Mods…",
                            "Not enough disk space.",
                            f"{free_gb:.1f} GB free in workshop location.",
                            False,
                        )

                if ok:
                    # Clear totals at start of a run (UI thread)
                    win.GLib.idle_add(lambda: setattr(win, "_steamcmd_total_sizes", {}) or False)

                    # Fetch total sizes for progress bar (%). Worker thread (safe).
                    try:
                        sizes = win.fetch_workshop_sizes_bytes(list(steamcmd_ids or []), appid=221100, timeout_s=20)
                    except Exception as e:
                        print(f"[SIZES] fetch_workshop_sizes_bytes failed: {e}")
                        sizes = {}

                    def _ui_set_sizes():
                        try:
                            win._steamcmd_total_sizes = {int(k): int(v) for k, v in (sizes or {}).items()}
                        except Exception:
                            win._steamcmd_total_sizes = {}

                        # Force L2 refresh so "- 8GB" appears immediately
                        win.GLib.idle_add(win._steamcmd_refresh_active_download_line2)
                        return False

                    win.GLib.idle_add(_ui_set_sizes)

                    # Discord status: installing mods
                    try:
                        if getattr(win, "_discord", None):
                            win._discord.set_installing_mods(server_name=str(obj.name or ""))
                    except Exception:
                        pass

                    # NOW start SteamCMD
                    win._steamcmd_install_in_progress = True
                    try:
                        ok = win.run_steamcmd_install(
                            steamcmd_path=steamcmd_path,
                            steam_username=steam_user_run,
                            steam_password=steam_pass_run,
                            workshop_dir=workshop_dir,
                            mod_ids=steamcmd_ids,
                            validate=validate,
                            max_concurrent=1,
                            dry_run=dry,
                            log_fn=None,
                            line_cb=win._steamcmd_install_line_from_worker,
                            cancel_event=win._steamcmd_cancel_event,
                        )
                    finally:
                        win._steamcmd_install_in_progress = False

                win.GLib.idle_add(win._hide_steamcmd_auth_overlay)

                if not ok:
                    if bool(win._steamcmd_cancel_event.is_set()):
                        err_msg = "SteamCMD cancelled"
                    else:
                        err_msg = "SteamCMD install failed"

                    # Discord: reset back to menu on failure/cancel
                    try:
                        if getattr(win, "_discord", None):
                            win._discord.set_menu()
                    except Exception:
                        pass

        else:
            if use_steamcmd:
                print("[JOIN] SteamCMD not needed for this join; proceeding with local mods only")
            else:
                print("[JOIN] SteamCMD mod handling disabled; proceeding with local mods only")

        if ok:
            missing_after = win.compute_missing_mods(workshop_dir, mods)
            if missing_after:
                ok = False
                err_msg = f"Required mods still missing after install: {[mid for mid, _ in missing_after]}"

        link_info = None
        if ok:
            link_info = win.ensure_watch_symlinks(
                workshop_dir=workshop_dir,
                mods=mods,
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
                if len(selected_paths) < len(mods):
                    ok = False
                    err_msg = "Failed to create all watch-folder symlinks"

        if ok:
            installed_mods_for_local = win.scan_installed_mods_in_watch_folder(watch_folder_linux)
            selected_mods_for_preset = (link_info or {}).get("selected_paths", [])
            paths = win.bootstrap_launcher_state(
                proton_prefix=proton_prefix,
                watch_folder_linux=watch_folder_linux,
                installed_mod_linux_paths=installed_mods_for_local,
                selected_mod_linux_paths=selected_mods_for_preset,
            )
            print(f"[JOIN] launcher state written: {paths}")

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
            print(f"[JOIN] Aborting launch: {err_msg or 'unknown error'}")
            win._on_filter_changed()
            return False

        win._launch_direct_steam_url(obj)
        win._on_filter_changed()
        return False

    win.GLib.idle_add(after)