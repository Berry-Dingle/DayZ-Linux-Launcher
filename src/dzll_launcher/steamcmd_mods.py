#!/usr/bin/env python3
# steamcmd_mods.py
#
# SteamCMD workshop mod installer for DZLL (DayZ appid 221100).
# Pure helper module; no UI changes here.

import json
import os
import requests
import re
import subprocess
import tempfile
import time
import pty
import select
import shutil
from pathlib import Path
from typing import Dict, List, Tuple
from .settings import autodetect_workshop_dir, autodetect_steamcmd_path

DAYZ_APPID = 221100

# Kill ONLY if SteamCMD produces no output for too long (stall detection).
# This avoids breaking huge mods (5–17GB+) that legitimately take a long time.
STEAMCMD_STALL_TIMEOUT_S = 60 * 60  # 60 minutes

STEAMCMD_DISK_ACTIVITY_POLL_S = 5.0  # how often we check disk progress while SteamCMD is quiet

# Get mod sizes
STEAM_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAM_WORKSHOP_BATCH_MAX = 100

LAST_ACCESS_DENIED_IDS: set[int] = set()

def _best_effort_shutdown_steam(log_fn=None, timeout_s: float = 10.0) -> None:
    """
    Best-effort attempt to stop the Steam client before running SteamCMD.
    - Tries `steam -shutdown` first (graceful).
    - If still running, sends SIGTERM to `steam` as a fallback.
    - Never raises; fail-open.
    """
    def log(msg: str):
        try:
            if callable(log_fn):
                log_fn(msg)
            else:
                print(msg)
        except Exception:
            pass

    def _steam_running() -> bool:
        try:
            # pgrep -x steam returns 0 if at least one process named exactly 'steam' exists
            r = subprocess.run(
                ["pgrep", "-x", "steam"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return r.returncode == 0
        except Exception:
            return False

    try:
        if not _steam_running():
            return

        log("[SteamCMD] Steam client detected; requesting shutdown…")

        # Graceful shutdown request
        try:
            subprocess.run(
                ["steam", "-shutdown"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        t0 = time.time()
        while _steam_running() and (time.time() - t0) < timeout_s:
            time.sleep(0.25)

        if not _steam_running():
            log("[SteamCMD] Steam client shut down.")
            return

        # Fallback: gentle terminate
        log("[SteamCMD] Steam still running; sending SIGTERM…")
        try:
            subprocess.run(
                ["pkill", "-TERM", "-x", "steam"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

        t1 = time.time()
        while _steam_running() and (time.time() - t1) < 2.0:
            time.sleep(0.25)

        if _steam_running():
            log("[SteamCMD] Steam still running; continuing anyway (fail-open).")
        else:
            log("[SteamCMD] Steam terminated.")
    except Exception:
        # Fail-open
        return


def parse_mods_from_db(mods_json: str) -> List[Tuple[int, str]]:
    """
    Parse DB 'mods' column (TEXT JSON):
      [{"name": "...", "steamWorkshopId": 123}, ...]
    Returns ordered list of (id, name). De-dupes IDs, keeps first occurrence order.
    """
    if not mods_json:
        return []
    try:
        arr = json.loads(mods_json)
    except Exception:
        return []
    if not isinstance(arr, list) or not arr:
        return []

    out: List[Tuple[int, str]] = []
    seen = set()

    for it in arr:
        if not isinstance(it, dict):
            continue
        sid = it.get("steamWorkshopId")
        if sid is None:
            continue
        try:
            sid_i = int(sid)
        except Exception:
            continue
        if sid_i <= 0 or sid_i in seen:
            continue
        nm = (it.get("name") or "").strip()
        out.append((sid_i, nm))
        seen.add(sid_i)

    return out


def workshop_mod_path(workshop_dir: str, mod_id: int) -> str:
    # workshop_dir is expected to be ".../steamapps/workshop"
    return os.path.join(workshop_dir, "content", str(DAYZ_APPID), str(int(mod_id)))


def _has_real_mod_content(mod_path: str) -> bool:
    """
    Heuristic to avoid false 'installed' after interrupted downloads.
    DayZ workshop mods normally contain .pbo files (usually under addons/).
    Fallback: require at least one non-zero file anywhere in the folder.
    """
    try:
        if not os.path.isdir(mod_path):
            return False

        # Fast path: look for .pbo in addons/
        addons = os.path.join(mod_path, "addons")
        if os.path.isdir(addons):
            try:
                for name in os.listdir(addons):
                    if name.lower().endswith(".pbo"):
                        p = os.path.join(addons, name)
                        try:
                            if os.path.getsize(p) > 0:
                                return True
                        except Exception:
                            return True
            except Exception:
                pass

        # Slower path: scan for any .pbo anywhere
        for root, _dirs, files in os.walk(mod_path):
            for fn in files:
                if fn.lower().endswith(".pbo"):
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fp) > 0:
                            return True
                    except Exception:
                        return True

        # Fallback: at least one non-zero file anywhere (catches non-pbo content)
        for root, _dirs, files in os.walk(mod_path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    if os.path.getsize(fp) > 0:
                        return True
                except Exception:
                    pass

    except Exception:
        return False

    return False


def is_mod_installed(workshop_dir: str, mod_id: int) -> bool:
    p = workshop_mod_path(workshop_dir, mod_id)
    return _has_real_mod_content(p)


def compute_missing_mods(workshop_dir: str, mods: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
    missing: List[Tuple[int, str]] = []
    for mid, name in mods:
        if not is_mod_installed(workshop_dir, mid):
            missing.append((mid, name))
    return missing


def _resolve_path(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    return os.path.abspath(os.path.expanduser(p))


def _looks_executable(p: str) -> bool:
    try:
        return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)
    except Exception:
        return False


def fetch_workshop_sizes_bytes(workshop_ids: List[int], *, appid: int = DAYZ_APPID, timeout_s: int = 20) -> Dict[int, int]:
    """
    Returns dict: {publishedfileid(int): file_size_bytes(int)}.
    Fail-open: any errors => returns partial/empty dict.
    """
    out: Dict[int, int] = {}

    # de-dupe, keep order
    ids: List[int] = []
    seen = set()
    for wid in (workshop_ids or []):
        try:
            w = int(wid)
        except Exception:
            continue
        if w > 0 and w not in seen:
            seen.add(w)
            ids.append(w)

    def _chunks(lst: List[int], n: int):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    for batch in _chunks(ids, STEAM_WORKSHOP_BATCH_MAX):
        data = {"itemcount": str(len(batch))}
        for i, wid in enumerate(batch):
            data[f"publishedfileids[{i}]"] = str(wid)

        try:
            r = requests.post(STEAM_DETAILS_URL, data=data, timeout=timeout_s)
            r.raise_for_status()
            payload = r.json()
        except Exception:
            continue

        details = (payload.get("response") or {}).get("publishedfiledetails") or []
        for item in details:
            try:
                if int(item.get("result") or 0) != 1:
                    continue
                wid = int(item.get("publishedfileid") or 0)
                if wid <= 0:
                    continue

                # optional sanity: DayZ app id match if present
                consumer = int(item.get("consumer_appid") or 0)
                creator = int(item.get("creator_appid") or 0)
                if appid and consumer not in (0, appid) and creator not in (0, appid):
                    continue

                sz = int(item.get("file_size") or 0)
                if sz > 0:
                    out[wid] = sz
            except Exception:
                continue

    return out


def run_steamcmd_install(
    *,
    steamcmd_path: str,
    steam_username: str,
    steam_password: str = "",
    workshop_dir: str,
    mod_ids: List[int],
    validate: bool,
    max_concurrent: int,  # kept for API compatibility; intentionally ignored
    dry_run: bool,        # kept for API compatibility; intentionally ignored (no-op)
    log_fn=None,
    line_cb=None,  # live SteamCMD output callback (per line)
    continue_on_batch_failure: bool = False,  # kept for API compatibility; intentionally ignored
    cancel_event=None,  # threading.Event set by UI to cancel
) -> bool:
    """
    Installs mods via SteamCMD using +runscript.

    Behavior:
      - We allow SteamCMD to continue past failures within a run (@ShutdownOnFailedCommand 0)
        so it attempts the full queue.
      - We then do controlled multi-pass recovery (default 4 passes total) so the user
        usually sees 2 Steam Guard prompts on a "poison" case (1st run fails + heal + 2nd run fixes),
        and extra prompts only happen on genuine repeated failures/timeouts.

    Returns True on success, False on any failure/cancel/stall after retries.
    """

    global LAST_ACCESS_DENIED_IDS
    LAST_ACCESS_DENIED_IDS = set()

    raw_steamcmd_path = (steamcmd_path or "").strip()
    detected_steamcmd_path = ""

    if raw_steamcmd_path:
        steamcmd_path = raw_steamcmd_path
    else:
        detected_steamcmd_path = (autodetect_steamcmd_path() or "").strip()
        steamcmd_path = detected_steamcmd_path

    steamcmd_path = _resolve_path(steamcmd_path)
    workshop_dir = _resolve_path(workshop_dir)

    def log(msg: str):
        if callable(log_fn):
            log_fn(msg)
        else:
            print(msg)

    def emit_line(msg: str):
        if callable(line_cb):
            try:
                line_cb(str(msg or ""))
            except Exception:
                pass

    def is_cancelled() -> bool:
        try:
            return bool(cancel_event) and bool(cancel_event.is_set())
        except Exception:
            return False

    def _refresh_workshop_dir(current: str) -> str:
        """
        Re-resolve the workshop root while SteamCMD is running/retrying.

        This specifically covers first-run / fresh-install cases where the
        workshop root may only become valid after Steam/SteamCMD has initialized.
        """
        try:
            fresh = str(autodetect_workshop_dir() or "").strip()
            if fresh:
                fresh = _resolve_path(fresh)
                if fresh != current:
                    log(f"[SteamCMD][FIX] workshop_dir refreshed: {current!r} -> {fresh!r}")
                    return fresh
        except Exception:
            pass
        return current

    # First refresh before any validation/checks
    workshop_dir = _refresh_workshop_dir(workshop_dir)

    try:
        print(f"[SteamCMD][DEBUG] raw_steamcmd_path={raw_steamcmd_path!r}")
        print(f"[SteamCMD][DEBUG] detected_steamcmd_path={detected_steamcmd_path!r}")
        print(f"[SteamCMD][DEBUG] steamcmd_path={steamcmd_path!r}")
        print(f"[SteamCMD][DEBUG] workshop_dir={workshop_dir!r}")
    except Exception:
        pass

    if not _looks_executable(steamcmd_path):
        log(f"[SteamCMD] Invalid steamcmd_path: {steamcmd_path}")
        return False

    if not workshop_dir or not os.path.isdir(workshop_dir):
        log(f"[SteamCMD] Invalid workshop_dir: {workshop_dir}")
        return False

    if not mod_ids:
        log("[SteamCMD] No mods to install.")
        return True

    # De-dupe incoming mod IDs while preserving order
    deduped_mod_ids: List[int] = []
    seen_ids = set()
    for mid in mod_ids:
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i <= 0 or mid_i in seen_ids:
            continue
        seen_ids.add(mid_i)
        deduped_mod_ids.append(mid_i)

    if not deduped_mod_ids:
        log("[SteamCMD] No valid mod IDs to install.")
        return True

    log(
        f"[SteamCMD] Single-run mode: {len(deduped_mod_ids)} mods "
        f"(max_concurrent_ignored={max_concurrent}) validate={bool(validate)} dry_run_ignored={bool(dry_run)}"
    )

    timeout_re = re.compile(r"Timeout downloading item\s+(\d+)", re.IGNORECASE)

    # Track inflight/completed (for cancel cleanup)
    start_re = re.compile(r"\bworkshop_download_item\s+221100\s+(\d+)\b", re.IGNORECASE)
    success_re = re.compile(r"\bSuccess\.\s+Downloaded item\s+(\d+)\b", re.IGNORECASE)
    access_denied_re = re.compile(
        r"ERROR!\s+Download item\s+(\d+)\s+failed\s+\(Access Denied\)",
        re.IGNORECASE,
    )

    # Workshop log auto-heal (Missing game files -> poisoned MID)
    workshop_log_path = os.path.join(str(Path.home()), ".local/share/Steam/logs/workshop_log.txt")
    missing_game_files_re = re.compile(
        r"\(Missing game files\).*?/workshop/content/221100/(\d+)/",
        re.IGNORECASE,
    )

    def _workshop_log_marker() -> tuple[int, float]:
        try:
            st = os.stat(workshop_log_path)
            return (int(st.st_size), float(st.st_mtime))
        except Exception:
            return (0, 0.0)

    def _read_workshop_log_new_since(marker: tuple[int, float], max_bytes: int = 200_000) -> str:
        try:
            old_size, _old_mtime = marker
            st = os.stat(workshop_log_path)
            new_size = int(st.st_size)
            if new_size <= old_size:
                return ""

            start = max(int(old_size), max(0, new_size - int(max_bytes)))

            with open(workshop_log_path, "rb") as f:
                f.seek(start)
                data = f.read()

            txt = data.decode("utf-8", "replace")

            # If we started at a capped position beyond old_size, drop first partial line
            if start > old_size:
                nl = txt.find("\n")
                if nl >= 0:
                    txt = txt[nl + 1:]

            return txt
        except Exception:
            return ""

    def _extract_poisoned_mids_from_workshop_log(text: str) -> set[int]:
        out = set()
        for m in missing_game_files_re.finditer(text or ""):
            try:
                out.add(int(m.group(1)))
            except Exception:
                pass
        return out

    def _maybe_heal_poison(marker: tuple[int, float]) -> bool:
        """
        Poll for a short window because workshop_log can lag the SteamCMD failure output.
        Returns True if we deleted at least one poisoned mod.
        """
        try:
            poisoned: set[int] = set()
            t0 = time.time()

            # Even if cancelled, do at least one read so we can clean up a poisoned mod
            while (time.time() - t0) < 8.0:
                tail = _read_workshop_log_new_since(marker)
                poisoned = _extract_poisoned_mids_from_workshop_log(tail)
                if poisoned:
                    break

                # If user cancelled, don't keep waiting/sleeping — exit after the one-shot check
                if is_cancelled():
                    break

                time.sleep(0.4)

            if not poisoned:
                return False

            log(f"[SteamCMD] Auto-heal: poisoned mods detected in workshop log: {sorted(poisoned)}")
            emit_line("[DZLL] Fixing broken workshop mod(s)…")
            for mid in sorted(poisoned):
                delete_single_mod(mid, workshop_dir=workshop_dir, log_fn=log_fn)
            return True
        except Exception:
            return False

    # Disk-activity progress tracking (for silent SteamCMD downloads)
    def _dir_signature_one_level(p: str) -> tuple[int, int]:
        try:
            st = os.stat(p)
        except Exception:
            return (0, 0)

        max_m = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        total = 0
        try:
            for name in os.listdir(p):
                fp = os.path.join(p, name)
                try:
                    st2 = os.stat(fp)
                except Exception:
                    continue
                m2 = getattr(st2, "st_mtime_ns", int(st2.st_mtime * 1e9))
                if m2 > max_m:
                    max_m = m2
                total += int(getattr(st2, "st_size", 0) or 0)
        except Exception:
            pass

        return (int(max_m), int(total))

    def _mods_progress_signature(mod_ids: List[int]) -> str:
        parts = []
        for mid in mod_ids:
            mid_s = str(int(mid))
            p1 = os.path.join(workshop_dir, "content", str(DAYZ_APPID), mid_s)
            p2 = os.path.join(workshop_dir, "downloads", str(DAYZ_APPID), mid_s)
            m1, s1 = _dir_signature_one_level(p1)
            m2, s2 = _dir_signature_one_level(p2)
            if m1 or s1 or m2 or s2:
                parts.append(f"{mid_s}:{m1}:{s1}:{m2}:{s2}")
        return "|".join(parts)

    def _missing_from_batch(batch: List[int]) -> List[int]:
        out: List[int] = []
        for mid in batch:
            try:
                if not is_mod_installed(workshop_dir, int(mid)):
                    out.append(int(mid))
            except Exception:
                out.append(int(mid))
        return out

    def _run_once(batch: List[int]) -> tuple[bool, int, set[int], set[int]]:
        """
        Returns (ok, rc, timed_out_ids, access_denied_ids).
        ok True means rc==0.
        """
        if is_cancelled():
            emit_line("[DZLL] SteamCMD cancelled by user.")
            try:
                _maybe_heal_poison((0, 0.0))
            except Exception:
                pass
            return (False, -1, set(), set())

        timed_out_ids: set[int] = set()
        access_denied_ids: set[int] = set()
        inflight_ids: List[int] = []
        completed_ids: set[int] = set()

        def _scan_line(line: str) -> None:
            # timeout tracking
            try:
                m = timeout_re.search(line)
                if m:
                    timed_out_ids.add(int(m.group(1)))
            except Exception:
                pass

            # access denied tracking
            try:
                m = access_denied_re.search(line)
                if m:
                    access_denied_ids.add(int(m.group(1)))
            except Exception:
                pass

            # cancel cleanup tracking
            try:
                m = start_re.search(line)
                if m:
                    inflight_ids.append(int(m.group(1)))
            except Exception:
                pass
            try:
                m = success_re.search(line)
                if m:
                    completed_ids.add(int(m.group(1)))
            except Exception:
                pass

        script_lines: List[str] = []
        script_lines.append("@ShutdownOnFailedCommand 0")

        user = (steam_username or "").strip()
        pwd = (steam_password or "").strip()

        if user and pwd:
            user_q = user.replace('"', r'\"')
            pwd_q = pwd.replace('"', r'\"')
            script_lines.append(f'login "{user_q}" "{pwd_q}"')
        elif user:
            script_lines.append(f"login {user}")
        else:
            script_lines.append("login")

        for mid in batch:
            if validate:
                script_lines.append(f"workshop_download_item {DAYZ_APPID} {int(mid)} validate")
            else:
                script_lines.append(f"workshop_download_item {DAYZ_APPID} {int(mid)}")

        script_lines.append("quit")
        script_text = "\n".join(script_lines) + "\n"

        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            encoding="utf-8",
            prefix="dzll_steamcmd_",
            suffix=".txt",
        ) as tf:
            tf.write(script_text)
            script_path = tf.name

        try:
            cmd = [steamcmd_path, "+runscript", script_path]
            log(f"[SteamCMD] Running: {cmd}")
            emit_line("[DZLL] SteamCMD starting…")

            _best_effort_shutdown_steam(log_fn=log_fn, timeout_s=10.0)

            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                )
            finally:
                try:
                    os.close(slave_fd)
                except Exception:
                    pass

            last_output_ts = time.time()
            buf = ""
            last_disk_poll_ts = 0.0
            last_sig = ""

            while True:
                # Cancel
                if is_cancelled():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

                    emit_line("[DZLL] SteamCMD cancelled by user.")

                    # Cancel cleanup: delete mods started but not completed this run
                    try:
                        seen = set()
                        inflight_unique = [m for m in inflight_ids if not (m in seen or seen.add(m))]
                        poison = [m for m in inflight_unique if m not in completed_ids]
                        emit_line(f"[DZLL] Cancel cleanup: removed {len(poison)} incomplete mod(s).")
                        for mid in poison:
                            log(f"[SteamCMD] Cancel cleanup: deleting in-flight mod {mid}")
                            delete_single_mod(mid, workshop_dir=workshop_dir, log_fn=log_fn)
                    except Exception as e:
                        log(f"[SteamCMD] Cancel cleanup failed: {e}")

                    try:
                        os.close(master_fd)
                    except Exception:
                        pass
                    return (False, -1, timed_out_ids, access_denied_ids)

                # Disk activity can keep SteamCMD "alive" even if it prints nothing.
                now = time.time()
                if (now - last_disk_poll_ts) >= STEAMCMD_DISK_ACTIVITY_POLL_S:
                    last_disk_poll_ts = now
                    try:
                        sig = _mods_progress_signature(batch)
                        if sig and sig != last_sig:
                            last_sig = sig
                            last_output_ts = now
                    except Exception:
                        pass

                # Stall timeout: kill only if SteamCMD is silent for too long AND no disk activity
                if (time.time() - last_output_ts) > STEAMCMD_STALL_TIMEOUT_S:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    log(f"[SteamCMD] stalled (no output/disk activity) for {STEAMCMD_STALL_TIMEOUT_S}s")
                    emit_line("[DZLL] SteamCMD stalled (no output/disk activity).")
                    try:
                        os.close(master_fd)
                    except Exception:
                        pass
                    return (False, -2, timed_out_ids, access_denied_ids)

                rlist, _, _ = select.select([master_fd], [], [], 0.20)

                if master_fd in rlist:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        last_output_ts = time.time()
                        text = chunk.decode("utf-8", "replace")
                        buf += text
                        while True:
                            nl = buf.find("\n")
                            if nl < 0:
                                break
                            line = buf[:nl].rstrip("\r")
                            buf = buf[nl + 1:]
                            if line:
                                log(line)
                                emit_line(line)
                                _scan_line(line)

                rc = proc.poll()
                if rc is not None:
                    # Drain trailing PTY data
                    while True:
                        try:
                            rlist, _, _ = select.select([master_fd], [], [], 0)
                        except Exception:
                            rlist = []
                        if master_fd not in rlist:
                            break
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        last_output_ts = time.time()
                        buf += chunk.decode("utf-8", "replace")

                    if buf:
                        for raw in buf.splitlines():
                            s = (raw or "").rstrip("\r\n")
                            if s:
                                log(s)
                                emit_line(s)
                                _scan_line(s)
                        buf = ""

                    try:
                        os.close(master_fd)
                    except Exception:
                        pass

                    return (rc == 0, int(rc), timed_out_ids, access_denied_ids)

        finally:
            try:
                os.unlink(script_path)
            except Exception:
                pass

    # ----------------------------
    # Main flow: up to 4 passes per Join click
    # ----------------------------
    MAX_PASSES = 4

    for attempt in range(1, MAX_PASSES + 1):
        if is_cancelled():
            try:
                # Best-effort heal using the most recent workshop log marker position (if any)
                # We don't have a marker here, so just read the tail since 0.
                _maybe_heal_poison((0, 0.0))
            except Exception:
                pass
            return False

        # Always refresh before each pass in case Steam/SteamCMD has just initialized the workshop path
        workshop_dir = _refresh_workshop_dir(workshop_dir)

        if attempt == 1:
            batch = list(deduped_mod_ids)
            emit_line(f"[DZLL] SteamCMD attempt {attempt}/{MAX_PASSES}: downloading required mods…")
        else:
            workshop_dir = _refresh_workshop_dir(workshop_dir)
            batch = _missing_from_batch(deduped_mod_ids)
            if not batch:
                log("[SteamCMD] All SteamCMD downloads completed successfully.")
                emit_line("[DZLL] All SteamCMD downloads completed.")
                return True
            emit_line(f"[DZLL] SteamCMD attempt {attempt}/{MAX_PASSES}: downloading missing mods ({len(batch)})…")

        marker = _workshop_log_marker()
        ok, rc, timed_out, access_denied = _run_once(batch)

        workshop_dir = _refresh_workshop_dir(workshop_dir)
        missing_now = _missing_from_batch(deduped_mod_ids)

        try:
            print(f"[SteamCMD][DEBUG] missing_now={missing_now}")
            print(f"[SteamCMD][DEBUG] access_denied={sorted(access_denied)}")
            for _mid in deduped_mod_ids:
                try:
                    _path = workshop_mod_path(workshop_dir, int(_mid))
                    _installed = is_mod_installed(workshop_dir, int(_mid))
                    print(f"[SteamCMD][DEBUG] mid={int(_mid)} path={_path!r} installed={_installed}")
                except Exception as _e:
                    print(f"[SteamCMD][DEBUG] mid={_mid!r} check failed: {_e}")
        except Exception:
            pass

        if access_denied:
            denied_sorted = sorted(access_denied)
            log(f"[SteamCMD] Skipping inaccessible workshop mod(s): {denied_sorted}")
            LAST_ACCESS_DENIED_IDS = set(access_denied)
            emit_line(f"[DZLL] Skipping inaccessible mod(s): {', '.join(str(x) for x in denied_sorted)}")

            deduped_mod_ids = [mid for mid in deduped_mod_ids if mid not in access_denied]

            workshop_dir = _refresh_workshop_dir(workshop_dir)
            missing_now = _missing_from_batch(deduped_mod_ids)

            if not deduped_mod_ids:
                log("[SteamCMD] No remaining downloadable mods required for this pass.")
                emit_line("[DZLL] Continuing without inaccessible mod(s).")
                return True

            if not missing_now:
                log("[SteamCMD] Remaining accessible mods are present.")
                emit_line("[DZLL] Remaining accessible mods are ready.")
                return True

            emit_line("[DZLL] Retrying with inaccessible mods removed…")
            continue

        if ok and not missing_now:
            log("[SteamCMD] All SteamCMD downloads completed successfully.")
            emit_line("[DZLL] All SteamCMD downloads completed.")
            return True

        # Heal poison if present (does not consume an extra attempt by itself)
        healed = _maybe_heal_poison(marker)

        # If timed out, just proceed to next attempt (this may trigger another Steam Guard prompt)
        if rc == 10 and timed_out:
            log(f"[SteamCMD] Attempt {attempt} hit timeouts: {sorted(timed_out)}")
            emit_line("[DZLL] SteamCMD timed out on one or more items; retrying…")
            continue

        # If failed and we healed, retry next loop (missing list will include healed mod)
        if (not ok) and healed:
            emit_line("[DZLL] Fixed broken mod(s); retrying missing mods…")
            continue

        # Any other failure or "ok but still missing": retry next loop until MAX_PASSES
        if not ok:
            log(f"[SteamCMD] Attempt {attempt} failed (rc={rc}); missing now: {missing_now}")
            emit_line("[DZLL] SteamCMD failed; retrying missing mods…")
            continue

        if missing_now:
            log(f"[SteamCMD] Attempt {attempt} completed but still missing: {missing_now}")
            emit_line("[DZLL] Some mods still missing; retrying…")
            continue

    workshop_dir = _refresh_workshop_dir(workshop_dir)
    final_missing = _missing_from_batch(deduped_mod_ids)
    log(f"[SteamCMD] Still missing after {MAX_PASSES} attempt(s): {final_missing}")
    emit_line("[DZLL] Some mods are still missing after several attempts. Please press Join again to retry.")
    return False


def parse_additional_mod_ids(text: str) -> List[int]:
    """
    Parses additional mod IDs from settings string.
    Accepts commas/spaces/newlines/semicolons.
    De-dupes, preserves order.
    """
    if not text:
        return []
    raw = str(text)
    for ch in [",", ";", "\n", "\t"]:
        raw = raw.replace(ch, " ")
    out: List[int] = []
    seen = set()
    for tok in raw.split():
        try:
            mid = int(tok.strip())
        except Exception:
            continue
        if mid <= 0 or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def merge_mod_lists_with_additional(
    mods: List[Tuple[int, str]],
    additional_ids: List[int],
) -> List[Tuple[int, str]]:
    """
    Keeps server mods first, appends extra IDs not already present.
    Extra names are blank.
    """
    out: List[Tuple[int, str]] = []
    seen = set()
    for mid, name in (mods or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i <= 0 or mid_i in seen:
            continue
        out.append((mid_i, name or ""))
        seen.add(mid_i)

    for mid in (additional_ids or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i <= 0 or mid_i in seen:
            continue
        out.append((mid_i, ""))
        seen.add(mid_i)

    return out


def ensure_dir(path: str) -> str:
    p = _resolve_path(path)
    os.makedirs(p, exist_ok=True)
    return p


def symlink_name_for_mod(mod_name: str, mod_id: int) -> str:
    """
    Prefer launcher-friendly @Name__<id> if available, fallback @<id>.
    We keep spaces (launcher sample shows '@Code Lock').
    """
    nm = (mod_name or "").strip()
    if nm:
        nm = nm.replace("/", "_").replace("\x00", "")
        if not nm.startswith("@"):
            nm = "@" + nm
        return f"{nm}__{int(mod_id)}"
    return f"@{int(mod_id)}"


def ensure_watch_symlinks(
    *,
    workshop_dir: str,
    mods: List[Tuple[int, str]],
    watch_folder: str,
    cleanup_stale: bool = True,
) -> Dict[str, List[str]]:
    """
    Creates symlinks in watch_folder -> workshop content/221100/<id>.
    Returns dict with created/updated/kept/removed/selected_paths/errors.
    selected_paths are Linux paths to the link entries (for current join set).
    """
    workshop_dir = _resolve_path(workshop_dir)
    watch_folder = ensure_dir(watch_folder)

    result = {
        "created": [],
        "updated": [],
        "kept": [],
        "removed": [],
        "selected_paths": [],
        "errors": [],
    }

    desired_links: Dict[str, str] = {}  # link_path -> target_path

    for mid, name in (mods or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        target = workshop_mod_path(workshop_dir, mid_i)
        if not os.path.isdir(target):
            result["errors"].append(f"missing target for mod {mid_i}: {target}")
            continue
        link_name = symlink_name_for_mod(name, mid_i)
        link_path = os.path.join(watch_folder, link_name)
        desired_links[link_path] = target

    # Create/update desired links
    for link_path, target in desired_links.items():
        try:
            if os.path.islink(link_path):
                cur = os.readlink(link_path)
                cur_abs = os.path.abspath(os.path.join(os.path.dirname(link_path), cur))
                tgt_abs = os.path.abspath(target)
                if cur_abs == tgt_abs:
                    result["kept"].append(link_path)
                else:
                    os.unlink(link_path)
                    os.symlink(target, link_path)
                    result["updated"].append(link_path)
            elif os.path.exists(link_path):
                result["errors"].append(f"path exists and is not symlink: {link_path}")
                continue
            else:
                os.symlink(target, link_path)
                result["created"].append(link_path)

            result["selected_paths"].append(link_path)
        except Exception as e:
            result["errors"].append(f"{link_path}: {e}")

    if cleanup_stale:
        try:
            desired_set = set(desired_links.keys())
            for name in os.listdir(watch_folder):
                p = os.path.join(watch_folder, name)
                if p in desired_set:
                    continue
                if os.path.islink(p):
                    try:
                        os.unlink(p)
                        result["removed"].append(p)
                    except Exception as e:
                        result["errors"].append(f"remove stale {p}: {e}")
        except Exception as e:
            result["errors"].append(f"cleanup stale failed: {e}")

    return result


def scan_installed_mods_in_watch_folder(watch_folder: str) -> List[str]:
    """
    Returns Linux paths of symlink entries in watch_folder (sorted).
    These become Local.json knownLocalMods after Proton path conversion.
    """
    watch_folder = _resolve_path(watch_folder)
    out: List[str] = []
    if not os.path.isdir(watch_folder):
        return out
    try:
        for name in sorted(os.listdir(watch_folder), key=lambda s: s.lower()):
            p = os.path.join(watch_folder, name)
            if os.path.islink(p):
                out.append(p)
    except Exception:
        return out
    return out


# =============================
# Delete Individual Mods Helper
# =============================
def remove_mid_from_appworkshop_acf(acf_path: str, mid: int) -> bool:
    """
    Remove one workshop item block from known sections in appworkshop_221100.acf:
      - "WorkshopItemsInstalled"
      - "WorkshopItemDetails" (common extra reference)

    Returns True if anything was removed.
    """
    mid_str = str(int(mid))
    acf_path = os.path.abspath(os.path.expanduser(acf_path))
    if not os.path.isfile(acf_path):
        return False

    with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    def _remove_from_section(full_text: str, section_name: str) -> tuple[str, bool]:
        m = re.search(rf'"{re.escape(section_name)}"\s*\{{', full_text)
        if not m:
            return full_text, False

        start_brace = full_text.find("{", m.start())
        if start_brace < 0:
            return full_text, False

        depth = 0
        end_brace = -1
        for i in range(start_brace, len(full_text)):
            c = full_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end_brace = i
                    break
        if end_brace < 0:
            return full_text, False

        block = full_text[start_brace:end_brace + 1]

        mid_pat = re.search(rf'^\s*"{re.escape(mid_str)}"\s*\{{', block, flags=re.MULTILINE)
        if not mid_pat:
            return full_text, False

        mid_open = block.find("{", mid_pat.start())
        if mid_open < 0:
            return full_text, False

        d = 0
        mid_end = -1
        for j in range(mid_open, len(block)):
            cj = block[j]
            if cj == "{":
                d += 1
            elif cj == "}":
                d -= 1
                if d == 0:
                    mid_end = j
                    break
        if mid_end < 0:
            return full_text, False

        line_start = block.rfind("\n", 0, mid_pat.start())
        if line_start < 0:
            line_start = 0
        else:
            line_start += 1

        cut_start = line_start
        cut_end = mid_end + 1
        while cut_end < len(block) and block[cut_end] in " \t\r\n":
            cut_end += 1

        new_block = block[:cut_start] + block[cut_end:]
        new_full = full_text[:start_brace] + new_block + full_text[end_brace + 1:]
        return new_full, True

    new_text = text
    changed_any = False
    for section in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        new_text, changed = _remove_from_section(new_text, section)
        changed_any = changed_any or changed

    if not changed_any:
        return False

    ts = int(time.time())
    bak = f"{acf_path}.bak.{ts}"
    with open(bak, "w", encoding="utf-8") as f:
        f.write(text)

    tmp = f"{acf_path}.tmp.{ts}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, acf_path)
    return True


def delete_single_mod(mid: int, *, workshop_dir: str = "", proton_prefix: str = "", log_fn=None) -> bool:
    """
    Delete ONE DayZ workshop mod safely:
      - remove from workshop content + staging
      - remove from appworkshop_221100.acf (surgical)
      - remove DZLL watch-folder symlink(s) pointing to this mod

    Returns True if it made changes (or mod not present but ACF entry removed), False on hard failure.
    """
    def log(msg: str):
        if callable(log_fn):
            log_fn(msg)
        else:
            print(msg)

    mid = int(mid)
    if mid <= 0:
        return False

    home = str(Path.home())
    steamapps = os.path.join(home, ".local/share/Steam/steamapps")
    workshop = _resolve_path(workshop_dir) if workshop_dir else os.path.join(steamapps, "workshop")

    content_dir = os.path.join(workshop, "content", "221100", str(mid))
    downloads_dir = os.path.join(workshop, "downloads", "221100", str(mid))
    patch_file = os.path.join(workshop, "downloads", f"state_221100_221100_{mid}.patch")
    acf_path = os.path.join(workshop, "appworkshop_221100.acf")

    pfx = _resolve_path(proton_prefix) if proton_prefix else os.path.join(steamapps, "compatdata/221100/pfx")
    pfx_user = os.path.join(pfx, "drive_c/users/steamuser")
    watch_folders = [
        os.path.join(pfx_user, "DZLLMods"),
        os.path.join(pfx_user, "Documents", "Templates"),
    ]

    # Best-effort: stop steamcmd if running (prevents it from re-writing state mid-delete)
    try:
        subprocess.run(
            ["pkill", "-f", "steamcmd"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    changed = False

    # 1) Remove symlink(s) in watch folders pointing to this mod
    for wf in watch_folders:
        if not os.path.isdir(wf):
            continue
        try:
            for name in os.listdir(wf):
                p = os.path.join(wf, name)
                if not os.path.islink(p):
                    continue
                try:
                    tgt = os.path.realpath(p)
                except Exception:
                    continue
                if tgt == content_dir:
                    log(f"[MOD DELETE] removing symlink: {p} -> {tgt}")
                    try:
                        os.unlink(p)
                        changed = True
                    except Exception as e:
                        log(f"[MOD DELETE] failed to remove symlink {p}: {e}")
        except Exception:
            pass

    # 2) Remove workshop content/staging for this mod
    for path in (content_dir, downloads_dir):
        if os.path.exists(path):
            log(f"[MOD DELETE] removing dir: {path}")
            try:
                shutil.rmtree(path, ignore_errors=False)
                changed = True
            except Exception as e:
                log(f"[MOD DELETE] failed to remove {path}: {e}")
                return False

    if os.path.exists(patch_file):
        log(f"[MOD DELETE] removing patch: {patch_file}")
        try:
            os.remove(patch_file)
            changed = True
        except Exception as e:
            log(f"[MOD DELETE] failed to remove {patch_file}: {e}")
            return False

    # 3) Remove from appworkshop_221100.acf (surgical)
    ok_acf = remove_mid_from_appworkshop_acf(acf_path, mid)
    if ok_acf:
        log(f"[MOD DELETE] removed {mid} from appworkshop_221100.acf")
        changed = True
    else:
        log(f"[MOD DELETE] note: {mid} not found/removed in appworkshop_221100.acf")

    return changed
