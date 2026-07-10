#!/usr/bin/env python3
"""Native Steam UGC Workshop downloader for DayZ mods."""

from typing import Callable, List

from .steamcmd_mods import DAYZ_APPID
from .steam_ugc_backend import run_ugc_install

STEAM_CLIENT_STALL_TIMEOUT_S = 60 * 60


def run_steam_client_install(
    *,
    workshop_dir: str,
    mod_ids: List[int],
    names_by_id: dict | None = None,
    cancel_event=None,
    state_cb: Callable[[int, int, int], None] | None = None,
    progress_cb: Callable[[dict], None] | None = None,
    handoff_cb: Callable[[], None] | None = None,
    allow_start_steam: bool = True,
    log_fn=None,
) -> bool:
    """Queue Workshop downloads through native Steam, sequentially."""

    def log(message: str) -> None:
        if callable(log_fn):
            log_fn(message)
        else:
            print(message)

    ids = []
    seen = set()
    for mid in mod_ids:
        try:
            mid_i = int(mid)
        except (TypeError, ValueError):
            continue
        if mid_i > 0 and mid_i not in seen:
            ids.append(mid_i)
            seen.add(mid_i)

    total = len(ids)
    if total <= 0:
        log("[Steam UGC] All required Workshop items are ready")
        return True

    ready_seen: set[int] = set()
    index_by_id = {mid: idx for idx, mid in enumerate(ids, start=1)}
    completed_count = 0
    last_ugc_error = ""
    last_ugc_preflight_log = ""

    def ugc_progress(event: dict) -> None:
        nonlocal completed_count, last_ugc_error, last_ugc_preflight_log
        if event.get("type") != "session":
            forwarded = dict(event or {})
            forwarded["backend"] = "steam_ugc"
            if forwarded.get("type") == "preflight":
                message = str(forwarded.get("message") or "").strip()
                should_log = bool(message) and (
                    message != last_ugc_preflight_log
                    or bool(forwarded.get("error", False))
                    or forwarded.get("ok") is not None
                )
                if should_log:
                    log(f"[Steam UGC] {message}")
                    last_ugc_preflight_log = message
            if bool(forwarded.get("error", False)):
                last_ugc_error = str(forwarded.get("message") or "").strip()
            if callable(progress_cb):
                try:
                    progress_cb(forwarded)
                except Exception:
                    pass
            return
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        if mid <= 0:
            return
        installed = bool(event.get("installed", event.get("installed_now", False)))
        needs_update = bool(event.get("needs_update", False))
        ready = bool(
            event.get(
                "ready",
                installed
                and not needs_update
                and not bool(event.get("downloading", False))
                and not bool(event.get("download_pending", False)),
            )
        )
        if ready:
            ready_seen.add(mid)
            completed_count = max(completed_count, len(ready_seen))
        index = int(index_by_id.get(mid) or max(1, len(ready_seen)))
        rich_event = {
            "backend": "steam_ugc",
            "id": mid,
            "download_bytes": int(event.get("download_bytes") or 0),
            "total_bytes": int(event.get("total_bytes") or 0),
            "downloading": bool(event.get("downloading", False)),
            "download_pending": bool(event.get("download_pending", False)),
            "installed": installed,
            "needs_update": needs_update,
            "ready": ready,
            "state_names": list(event.get("state_names") or event.get("last_state_names") or []),
            "index": index,
            "total": total,
            "completed_count": completed_count,
        }
        if callable(progress_cb):
            try:
                progress_cb(rich_event)
            except Exception:
                pass
        if callable(state_cb):
            try:
                state_cb(mid, index, total)
            except Exception:
                pass

    log("[Steam UGC] Backend enabled")
    log(f"[Steam UGC] Checking/updating {total} required Workshop item(s)")
    ok = run_ugc_install(
        ids,
        appid=DAYZ_APPID,
        cancel_event=cancel_event,
        progress_cb=ugc_progress,
        names_by_id=names_by_id,
        allow_start_steam=bool(allow_start_steam),
        timeout=STEAM_CLIENT_STALL_TIMEOUT_S,
    )
    if ok:
        log("[Steam UGC] Finished")
        return True
    if cancel_event is not None and cancel_event.is_set():
        log("[Steam UGC] Cancelled")
    else:
        log(f"[Steam UGC] Failed: {last_ugc_error}" if last_ugc_error else "[Steam UGC] Failed")
    return False
