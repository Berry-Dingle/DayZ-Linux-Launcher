#!/usr/bin/env python3
"""
Steam Client UGC backend for DayZ Workshop items.

This is the default required-mod backend. SteamCMD remains the advanced
fallback for troubleshooting.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


DAYZ_APPID = 221100
UGC_PREFLIGHT_TIMEOUT_S = 60.0
UGC_PREFLIGHT_RETRY_S = 3.0
UGC_PREFLIGHT_PROBE_TIMEOUT_S = 8.0


@dataclass
class UGCModSession:
    id: int
    was_subscribed_before: bool = False
    was_installed_before: bool = False
    subscribed_by_dzll_this_join: bool = False
    installed_now: bool = False
    needs_update: bool = False
    last_state_names: list[str] | None = None
    download_bytes: int = 0
    total_bytes: int = 0
    size_on_disk: int = 0
    downloading: bool = False
    download_pending: bool = False
    install_folder: str | None = None

    def update_from_item(self, event: dict) -> None:
        self.installed_now = bool(event.get("installed", False))
        self.needs_update = bool(event.get("needs_update", False))
        self.last_state_names = list(event.get("state_names") or [])
        self.downloading = bool(event.get("downloading", False))
        self.download_pending = bool(event.get("download_pending", False))
        try:
            self.download_bytes = int(event.get("download_bytes") or 0)
        except Exception:
            self.download_bytes = 0
        try:
            self.total_bytes = int(event.get("total_bytes") or 0)
        except Exception:
            self.total_bytes = 0
        try:
            self.size_on_disk = int(event.get("size_on_disk") or 0)
        except Exception:
            self.size_on_disk = 0
        folder = event.get("install_folder")
        self.install_folder = str(folder) if folder else None

    def event(self) -> dict:
        data = asdict(self)
        data["type"] = "session"
        data["installed"] = bool(self.installed_now)
        data["ready"] = ugc_item_ready(data)
        data["state_names"] = list(self.last_state_names or [])
        return data


def ugc_item_ready(item) -> bool:
    if isinstance(item, UGCModSession):
        return (
            bool(item.installed_now)
            and not bool(item.needs_update)
            and not bool(item.downloading)
            and not bool(item.download_pending)
        )
    event = dict(item or {})
    return (
        bool(event.get("installed", event.get("installed_now", False)))
        and not bool(event.get("needs_update", False))
        and not bool(event.get("downloading", False))
        and not bool(event.get("download_pending", False))
    )


def emit(event: dict) -> None:
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _dedupe_sorted_ids(mod_ids: Iterable[int]) -> list[int]:
    out = []
    seen = set()
    for raw in mod_ids or []:
        try:
            mid = int(raw)
        except Exception:
            continue
        if mid > 0 and mid not in seen:
            out.append(mid)
            seen.add(mid)
    out.sort()
    return out


def _helper_env() -> dict:
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = package_root if not current else f"{package_root}:{current}"
    return env


def _helper_cmd(command: str, *, appid: int, timeout: float | None, mod_ids: list[int]) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "dzll_launcher.steam_ugc_helper",
        command,
        "--appid",
        str(int(appid)),
    ]
    if timeout is not None and command in ("subscribe-download", "unsubscribe"):
        cmd.extend(["--timeout", str(int(max(0, timeout)))])
    cmd.extend(str(int(mid)) for mid in mod_ids)
    return cmd


def _log_event(progress_cb, message: str, **extra) -> None:
    eprint(message)
    event = {"type": "log", "message": message}
    event.update(extra)
    _progress(progress_cb, event)


def _stop_helper_process(proc: subprocess.Popen, *, command: str, progress_cb=None) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
    except Exception:
        pass
    _log_event(progress_cb, "[Steam UGC] Stopped helper subprocess", command=command)


def _run_helper_json_lines(
    command: str,
    *,
    appid: int,
    timeout: float | None,
    mod_ids: list[int],
    cancel_event=None,
    on_event: Callable[[dict], None] | None = None,
    progress_cb=None,
) -> tuple[bool, int | None]:
    cmd = _helper_cmd(command, appid=appid, timeout=timeout, mod_ids=mod_ids)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_helper_env(),
        )
    except OSError as exc:
        eprint(f"[steam-ugc-backend] failed to start helper: {exc}")
        return False, None

    stdout_q: queue.Queue[str] = queue.Queue()
    stderr_tail: list[str] = []

    def read_stdout():
        try:
            for line in proc.stdout or []:
                stdout_q.put(str(line or ""))
        except Exception:
            pass

    def read_stderr():
        try:
            for line in proc.stderr or []:
                text = str(line or "").rstrip()
                if text:
                    stderr_tail.append(text)
                    del stderr_tail[:-8]
        except Exception:
            pass

    stdout_t = threading.Thread(target=read_stdout, daemon=True)
    stderr_t = threading.Thread(target=read_stderr, daemon=True)
    stdout_t.start()
    stderr_t.start()

    deadline = time.monotonic() + float(timeout) if timeout is not None else None
    ok = False
    done_seen = False

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _log_event(progress_cb, "[Steam UGC] Cancel requested", command=command)
                _stop_helper_process(proc, command=command, progress_cb=progress_cb)
                return False, proc.poll()

            if deadline is not None and time.monotonic() >= deadline:
                _stop_helper_process(proc, command=command, progress_cb=progress_cb)
                eprint(f"[steam-ugc-backend] helper {command} timed out")
                return False, proc.poll()

            try:
                line = stdout_q.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and stdout_q.empty():
                    break
                continue

            raw = str(line or "").strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                eprint(f"[steam-ugc-backend] ignoring malformed helper output: {raw[:160]}")
                continue
            if callable(on_event):
                on_event(event)
            if event.get("type") == "done":
                done_seen = True
                ok = bool(event.get("ok", False))

        rc = proc.poll()
        try:
            stdout_t.join(timeout=0.5)
            stderr_t.join(timeout=0.5)
        except Exception:
            pass
        if rc != 0 or not done_seen:
            for line in stderr_tail:
                eprint(f"[steam-ugc-backend][helper-stderr] {line}")
            return False, rc
        return bool(ok), rc
    finally:
        if proc.poll() is None:
            _stop_helper_process(proc, command=command, progress_cb=progress_cb)


def _progress(progress_cb, event: dict) -> None:
    if callable(progress_cb):
        try:
            progress_cb(event)
        except Exception:
            pass


def _ugc_preflight_event(progress_cb, message: str, *, ok=None, reason: str = "", error: bool = False) -> None:
    event = {
        "backend": "steam_ugc",
        "type": "preflight",
        "message": str(message or ""),
    }
    if ok is not None:
        event["ok"] = bool(ok)
    if reason:
        event["reason"] = str(reason)
    if error:
        event["error"] = True
    _progress(progress_cb, event)


def _run_ugc_native_steam_preflight(
    ids: list[int],
    *,
    appid: int,
    cancel_event=None,
    progress_cb=None,
    allow_start_steam: bool = True,
    timeout_s: float = UGC_PREFLIGHT_TIMEOUT_S,
) -> bool:
    try:
        from .steam_native import is_flatpak_steam_running, is_native_steam_running, resolve_native_steam_cmd
    except Exception as exc:
        message = f"Steam preflight failed: {exc}"
        _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_native_unavailable", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    try:
        if is_flatpak_steam_running():
            message = "Flatpak Steam is running. DZLL UGC install requires native Steam."
            _ugc_preflight_event(progress_cb, message, ok=False, reason="flatpak_steam_running", error=True)
            eprint(f"[Steam UGC] {message}")
            return False
    except Exception as exc:
        message = f"Could not check Steam process type: {exc}"
        _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_process_check_failed", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    steam_cmd = resolve_native_steam_cmd()
    if not steam_cmd:
        message = "Native Steam executable was not found. DZLL UGC install cannot continue."
        _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_not_found", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    try:
        native_running = is_native_steam_running()
    except Exception:
        native_running = False

    if not native_running:
        if not bool(allow_start_steam):
            message = "Native Steam is not running. Start Steam before joining, or enable Start Steam on Join."
            _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_not_running", error=True)
            eprint(f"[Steam UGC] {message}")
            return False
        _ugc_preflight_event(progress_cb, "Starting Steam...")
        try:
            subprocess.Popen(
                [steam_cmd, "-silent"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            message = f"Failed to start native Steam: {exc}"
            _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_start_failed", error=True)
            eprint(f"[Steam UGC] {message}")
            return False
    else:
        _ugc_preflight_event(progress_cb, "Checking Steam...")

    probe_ids = ids[:1] if ids else []
    if not probe_ids:
        message = "No Workshop item was available for Steam readiness probing."
        _ugc_preflight_event(progress_cb, message, ok=False, reason="no_probe_item", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    deadline = time.monotonic() + max(1.0, float(timeout_s))
    last_message_at = 0.0
    attempt = 0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            _ugc_preflight_event(progress_cb, "Steam startup cancelled.", ok=False, reason="cancelled", error=True)
            return False

        now = time.monotonic()
        if now - last_message_at >= UGC_PREFLIGHT_RETRY_S:
            try:
                native_running = is_native_steam_running()
            except Exception:
                native_running = False
            _ugc_preflight_event(progress_cb, "Waiting for Steam login..." if native_running else "Waiting for Steam...")
            last_message_at = now

        remaining = max(0.1, deadline - time.monotonic())
        probe_timeout = min(UGC_PREFLIGHT_PROBE_TIMEOUT_S, remaining)
        attempt += 1
        ok, _rc = _run_helper_json_lines(
            "state",
            appid=int(appid),
            timeout=probe_timeout,
            mod_ids=probe_ids,
            cancel_event=cancel_event,
            on_event=None,
            progress_cb=None,
        )
        if ok:
            _ugc_preflight_event(progress_cb, "Steam is ready.", ok=True, reason="ready")
            return True

        sleep_for = min(UGC_PREFLIGHT_RETRY_S, max(0.0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)

    message = "Steam did not become ready. Please make sure native Steam is running and logged in."
    _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_not_ready", error=True)
    eprint(f"[Steam UGC] {message}")
    return False


def _normalize_ugc_snapshot(event: dict) -> dict:
    normalized = dict(event or {})
    if not bool(normalized.get("installed", False)):
        return normalized

    install_folder = str(normalized.get("install_folder") or "").strip()
    if not install_folder:
        return normalized

    installed_dir_exists = False
    try:
        path = Path(install_folder).expanduser()
        installed_dir_exists = path.is_dir() and not path.is_symlink()
    except Exception:
        installed_dir_exists = False

    if installed_dir_exists:
        return normalized

    normalized["installed"] = False
    normalized["install_folder"] = None
    normalized["size_on_disk"] = 0
    try:
        state_names = [str(name) for name in (normalized.get("state_names") or []) if str(name) != "Installed"]
        normalized["state_names"] = state_names
    except Exception:
        normalized["state_names"] = []
    return normalized


def _cache_ugc_state(state_by_id: dict[int, dict], *, names_by_id=None) -> None:
    if not state_by_id:
        return
    try:
        from .mod_metadata import upsert_many_from_ugc_state

        normalized = {int(mid): _normalize_ugc_snapshot(state) for mid, state in (state_by_id or {}).items()}
        upsert_many_from_ugc_state(normalized, names_by_id=names_by_id)
    except Exception as exc:
        eprint(f"[Steam UGC] Metadata cache update failed: {exc}")


def _cleanup_subscriptions(sessions: dict[int, UGCModSession], *, appid: int, progress_cb=None) -> None:
    cleanup_ids = sorted(
        mid
        for mid, session in sessions.items()
        if session.subscribed_by_dzll_this_join
        and not session.was_subscribed_before
        and not session.installed_now
    )
    _log_event(progress_cb, f"[Steam UGC] Cleanup unsubscribe ids: {cleanup_ids}", cleanup_ids=cleanup_ids)
    if not cleanup_ids:
        _progress(progress_cb, {"type": "cleanup", "ok": True, "unsubscribed": [], "skipped": True})
        _log_event(progress_cb, "[Steam UGC] Cancel cleanup complete", ok=True, cleanup_ids=cleanup_ids)
        return

    _progress(progress_cb, {"type": "cleanup", "ok": None, "unsubscribing": cleanup_ids})
    ok, _rc = _run_helper_json_lines(
        "unsubscribe",
        appid=appid,
        timeout=180,
        mod_ids=cleanup_ids,
        cancel_event=None,
        on_event=lambda event: _progress(progress_cb, {"type": "cleanup_event", "event": event}),
        progress_cb=progress_cb,
    )
    _progress(progress_cb, {"type": "cleanup", "ok": bool(ok), "unsubscribed": cleanup_ids})
    _log_event(progress_cb, "[Steam UGC] Cancel cleanup complete", ok=bool(ok), cleanup_ids=cleanup_ids)


def _refresh_current_state(sessions: dict[int, UGCModSession], *, appid: int, progress_cb=None, names_by_id=None) -> None:
    ids = sorted(sessions)
    if not ids:
        return

    def on_refresh_event(event: dict) -> None:
        if event.get("type") != "item":
            _progress(progress_cb, {"type": "refresh_event", "event": event})
            return
        event = _normalize_ugc_snapshot(event)
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        session = sessions.get(mid)
        if session is None:
            return
        session.update_from_item(event)
        _cache_ugc_state({mid: event}, names_by_id=names_by_id)
        _progress(progress_cb, session.event())

    _run_helper_json_lines(
        "state",
        appid=appid,
        timeout=120,
        mod_ids=ids,
        cancel_event=None,
        on_event=on_refresh_event,
        progress_cb=progress_cb,
    )


def query_ugc_state_checked(mod_ids, *, appid=DAYZ_APPID, timeout=60) -> tuple[bool, dict[int, dict]]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return True, snapshots

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        if mid > 0:
            snapshots[mid] = dict(event)

    ok, _rc = _run_helper_json_lines(
        "state",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return bool(ok), snapshots


def query_ugc_state(mod_ids, *, appid=DAYZ_APPID, timeout=60) -> dict[int, dict]:
    _ok, snapshots = query_ugc_state_checked(mod_ids, appid=appid, timeout=timeout)
    return snapshots


def wait_for_ugc_ready(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    cancel_event=None,
    progress_cb=None,
    allow_start_steam: bool = True,
    timeout_s: float = UGC_PREFLIGHT_TIMEOUT_S,
) -> bool:
    ids = _dedupe_sorted_ids(mod_ids)
    if not ids:
        return True
    return _run_ugc_native_steam_preflight(
        ids,
        appid=int(appid),
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        allow_start_steam=bool(allow_start_steam),
        timeout_s=float(timeout_s),
    )


def unsubscribe_ugc_items(mod_ids, *, appid=DAYZ_APPID, timeout=120) -> dict[int, dict]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return snapshots

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        if mid > 0:
            snapshots[mid] = dict(event)

    _run_helper_json_lines(
        "unsubscribe",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return snapshots


def request_unsubscribe_ugc_items(mod_ids, *, appid=DAYZ_APPID, timeout=12) -> tuple[bool, dict[int, dict]]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return True, snapshots

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        if mid > 0:
            snapshots[mid] = dict(event)

    ok, _rc = _run_helper_json_lines(
        "unsubscribe-request",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return bool(ok), snapshots


def _native_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        from .steam_native import native_steam_libraries, resolve_native_steam_root

        roots.extend(Path(path) for path in native_steam_libraries())

        root = resolve_native_steam_root()
        if root is not None:
            roots.append(Path(root))
    except Exception:
        pass
    roots.extend((Path.home() / ".local/share/Steam", Path.home() / ".steam/steam"))

    out: list[Path] = []
    seen = set()
    for root in roots:
        try:
            resolved = Path(root).expanduser().resolve()
        except Exception:
            continue
        key = str(resolved)
        if key not in seen:
            out.append(resolved)
            seen.add(key)
    return out


def _safe_workshop_content_path(path: str, *, mod_id: int, appid: int) -> Path | None:
    if not path:
        return None
    try:
        raw = Path(path).expanduser()
        if raw.name != str(int(mod_id)):
            return None
        if raw.is_symlink():
            return None
        resolved = raw.resolve()
        for root in _native_steam_roots():
            allowed = (root / "steamapps/workshop/content" / str(int(appid)) / str(int(mod_id))).resolve()
            if resolved == allowed:
                return raw
    except Exception:
        return None
    return None


def _safe_workshop_download_dir(path: Path, *, mod_id: int, appid: int) -> Path | None:
    try:
        raw = Path(path).expanduser()
        if raw.name != str(int(mod_id)):
            return None
        if raw.is_symlink():
            return None
        resolved = raw.resolve()
        for root in _native_steam_roots():
            allowed = (root / "steamapps/workshop/downloads" / str(int(appid)) / str(int(mod_id))).resolve()
            if resolved == allowed:
                return raw
    except Exception:
        return None
    return None


def _safe_workshop_patch_file(path: Path, *, mod_id: int, appid: int) -> Path | None:
    expected_name = f"state_{int(appid)}_{int(appid)}_{int(mod_id)}.patch"
    try:
        raw = Path(path).expanduser()
        if raw.name != expected_name:
            return None
        if raw.is_symlink():
            return None
        resolved = raw.resolve()
        for root in _native_steam_roots():
            allowed_paths = (
                root / "steamapps/workshop/downloads" / expected_name,
                root / "steamapps/workshop" / expected_name,
            )
            for allowed_path in allowed_paths:
                if resolved == allowed_path.resolve():
                    return raw
    except Exception:
        return None
    return None


def _candidate_content_dirs(mod_id: int, appid: int, before: dict, after: dict) -> list[Path]:
    candidates: list[Path] = []
    for state in (before or {}, after or {}):
        folder = str(state.get("install_folder") or "").strip()
        safe = _safe_workshop_content_path(folder, mod_id=mod_id, appid=appid)
        if safe is not None:
            candidates.append(safe)
    for root in _native_steam_roots():
        candidates.append(root / "steamapps/workshop/content" / str(int(appid)) / str(int(mod_id)))

    out: list[Path] = []
    seen = set()
    for path in candidates:
        safe = _safe_workshop_content_path(str(path), mod_id=mod_id, appid=appid)
        if safe is None:
            continue
        key = str(safe)
        if key not in seen:
            out.append(safe)
            seen.add(key)
    return out


def _delete_dir_if_present(path: Path, *, log_fn=None) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(log_fn):
        log_fn(f"[Steam UGC] deleting local mod folder: {path}")
    shutil.rmtree(path, ignore_errors=False)
    return True


def _delete_file_if_present(path: Path, *, log_fn=None) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(log_fn):
        log_fn(f"[Steam UGC] deleting local staging file: {path}")
    path.unlink()
    return True


def _mark_metadata_deleted(mod_id: int) -> None:
    try:
        from .mod_metadata import upsert_mod_metadata

        upsert_mod_metadata(
            int(mod_id),
            subscribed=False,
            installed=False,
            clear_fields=["install_folder"],
        )
    except Exception as exc:
        eprint(f"[Steam UGC] Metadata cache delete update failed: {exc}")


def _native_appworkshop_acf_paths(appid: int) -> list[Path]:
    out: list[Path] = []
    seen = set()
    for root in _native_steam_roots():
        path = root / "steamapps/workshop" / f"appworkshop_{int(appid)}.acf"
        key = str(path)
        if key not in seen:
            out.append(path)
            seen.add(key)
    return out


def _find_acf_section_bounds(text: str, section_name: str) -> tuple[int, int, int] | None:
    match = re.search(rf'"{re.escape(section_name)}"\s*\{{', text)
    if not match:
        return None

    start_brace = text.find("{", match.start())
    if start_brace < 0:
        return None

    depth = 0
    for i in range(start_brace, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return match.start(), start_brace, i
    return None


def _extract_workshop_acf_ids_from_text(text: str) -> list[int]:
    ids: list[int] = []
    seen = set()
    for section_name in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        bounds = _find_acf_section_bounds(text, section_name)
        if bounds is None:
            continue
        _section_start, start_brace, end_brace = bounds
        block = text[start_brace : end_brace + 1]
        for _start, _end, key in _direct_acf_child_entries(block):
            try:
                mid = int(key)
            except Exception:
                continue
            if mid > 0 and mid not in seen:
                ids.append(mid)
                seen.add(mid)
    ids.sort()
    return ids


def _direct_acf_child_entries(block: str) -> list[tuple[int, int, str]]:
    entries: list[tuple[int, int, str]] = []
    depth = 0
    i = 0
    while i < len(block):
        c = block[i]
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        if depth != 1 or c != '"':
            i += 1
            continue

        key_start = i
        key_end = block.find('"', key_start + 1)
        if key_end < 0:
            break
        key = block[key_start + 1 : key_end]
        if not key.isdigit():
            i = key_end + 1
            continue

        value_start = key_end + 1
        while value_start < len(block) and block[value_start] in " \t\r\n":
            value_start += 1
        if value_start >= len(block):
            break

        line_start = block.rfind("\n", 0, key_start)
        cut_start = 0 if line_start < 0 else line_start + 1

        if block[value_start] == "{":
            d = 0
            value_end = -1
            for j in range(value_start, len(block)):
                cj = block[j]
                if cj == "{":
                    d += 1
                elif cj == "}":
                    d -= 1
                    if d == 0:
                        value_end = j + 1
                        break
            if value_end < 0:
                i = value_start + 1
                continue
            cut_end = value_end
        elif block[value_start] == '"':
            newline = block.find("\n", value_start)
            cut_end = len(block) if newline < 0 else newline + 1
        else:
            i = value_start + 1
            continue

        if cut_end < len(block) and block[cut_end] == "\r":
            cut_end += 1
        if cut_end < len(block) and block[cut_end] == "\n":
            cut_end += 1
        entries.append((cut_start, cut_end, key))
        i = cut_end
    return entries


def _remove_workshop_acf_ids_from_text(text: str, mod_ids: Iterable[int]) -> tuple[str, set[int]]:
    target_ids = {str(mid) for mid in _dedupe_sorted_ids(mod_ids)}
    if not target_ids:
        return text, set()

    removed: set[int] = set()
    new_text = text

    def _remove_from_section(full_text: str, section_name: str) -> tuple[str, set[int]]:
        bounds = _find_acf_section_bounds(full_text, section_name)
        if bounds is None:
            return full_text, set()

        _section_start, start_brace, end_brace = bounds
        block = full_text[start_brace : end_brace + 1]
        spans: list[tuple[int, int, int]] = []

        for start, end, mid_str in _direct_acf_child_entries(block):
            if mid_str not in target_ids:
                continue
            spans.append((start, end, int(mid_str)))

        if not spans:
            return full_text, set()

        spans.sort(key=lambda item: item[0])
        merged: list[tuple[int, int, int]] = []
        for start, end, mid in spans:
            if merged and start < merged[-1][1]:
                continue
            merged.append((start, end, mid))

        new_block = block
        section_removed: set[int] = set()
        for start, end, mid in reversed(merged):
            new_block = new_block[:start] + new_block[end:]
            section_removed.add(mid)

        return full_text[:start_brace] + new_block + full_text[end_brace + 1 :], section_removed

    for section in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        new_text, section_removed = _remove_from_section(new_text, section)
        removed.update(section_removed)

    return new_text, removed


def _write_acf_atomically_with_backup(path: Path, original_bytes: bytes, new_text: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{ts}.{os.getpid()}"
    backup = path.with_name(f"{path.name}.bak.{suffix}")
    tmp = path.with_name(f"{path.name}.tmp.{suffix}")
    backup.write_bytes(original_bytes)
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return str(backup)


def _has_real_native_workshop_folder(mod_id: int, *, appid: int, states: Iterable[dict] = ()) -> bool:
    candidates: list[Path] = []
    for state in states or ():
        folder = str((state or {}).get("install_folder") or "").strip()
        safe = _safe_workshop_content_path(folder, mod_id=int(mod_id), appid=int(appid))
        if safe is not None:
            candidates.append(safe)
    for root in _native_steam_roots():
        candidates.append(root / "steamapps/workshop/content" / str(int(appid)) / str(int(mod_id)))

    for path in candidates:
        safe = _safe_workshop_content_path(str(path), mod_id=int(mod_id), appid=int(appid))
        if safe is None:
            continue
        try:
            if safe.exists() and safe.is_dir() and not safe.is_symlink():
                return True
        except Exception:
            continue
    return False


def _remove_workshop_acf_entries_from_paths(paths: Iterable[Path], mod_ids: Iterable[int], *, log_fn=None) -> dict:
    ids = _dedupe_sorted_ids(mod_ids)
    result = {
        "ok": False,
        "paths": [],
        "removed_ids": [],
        "missing_ids": list(ids),
        "backups": [],
        "error": "",
    }
    if not ids:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    removed_any: set[int] = set()
    for raw_path in paths or ():
        path = Path(raw_path).expanduser()
        result["paths"].append(str(path))
        try:
            if not path.is_file():
                continue
            original_bytes = path.read_bytes()
            text = original_bytes.decode("utf-8", "replace")
            new_text, removed = _remove_workshop_acf_ids_from_text(text, ids)
            if not removed:
                continue
            backup = _write_acf_atomically_with_backup(path, original_bytes, new_text)
            result["backups"].append(backup)
            removed_any.update(removed)
            try:
                if callable(log_fn):
                    log_fn(f"[Steam UGC] removed ACF entries from {path}: {sorted(removed)}")
            except Exception:
                pass
        except Exception as exc:
            result["error"] = str(exc)
            return result

    result["removed_ids"] = sorted(removed_any)
    result["missing_ids"] = [mid for mid in ids if mid not in removed_any]
    result["ok"] = True
    return result


def remove_workshop_acf_entries(mod_ids, *, appid=DAYZ_APPID, log_fn=None) -> dict:
    """
    Remove exact DayZ Workshop item records from native appworkshop_<appid>.acf.

    The helper only removes keyed entries matching the supplied IDs inside known
    Workshop item sections. It skips IDs Steam still reports as subscribed and
    IDs that still have a real native workshop content folder.
    """
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
        except Exception:
            pass

    ids = _dedupe_sorted_ids(mod_ids)
    result = {
        "ok": False,
        "appid": int(appid),
        "paths": [],
        "removed_ids": [],
        "missing_ids": list(ids),
        "skipped_subscribed_ids": [],
        "skipped_installed_ids": [],
        "backups": [],
        "error": "",
    }
    if not ids:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    states: dict[int, dict] = {}
    try:
        states = query_ugc_state(ids, appid=int(appid), timeout=20)
    except Exception as exc:
        log(f"[Steam UGC] ACF cleanup state check failed; continuing with folder guards only: {exc}")
        states = {}

    removable: list[int] = []
    for mid in ids:
        state = dict(states.get(mid) or {})
        if bool(state.get("subscribed", False)):
            result["skipped_subscribed_ids"].append(mid)
            continue
        if _has_real_native_workshop_folder(mid, appid=int(appid), states=[state]):
            result["skipped_installed_ids"].append(mid)
            continue
        removable.append(mid)

    if not removable:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    path_result = _remove_workshop_acf_entries_from_paths(
        _native_appworkshop_acf_paths(int(appid)),
        removable,
        log_fn=log,
    )
    if not bool(path_result.get("ok", False)):
        result["paths"] = list(path_result.get("paths") or [])
        result["backups"] = list(path_result.get("backups") or [])
        result["error"] = str(path_result.get("error") or "")
        return result

    removed_sorted = list(path_result.get("removed_ids") or [])
    protected = set(result["skipped_subscribed_ids"]) | set(result["skipped_installed_ids"])
    result["paths"] = list(path_result.get("paths") or [])
    result["backups"] = list(path_result.get("backups") or [])
    result["removed_ids"] = removed_sorted
    result["missing_ids"] = [mid for mid in ids if mid not in set(removed_sorted) and mid not in protected]
    result["ok"] = True
    return result


def scrub_stale_dayz_workshop_acf(
    *,
    appid=DAYZ_APPID,
    remove_non_subscribed_installed=False,
    log_fn=None,
) -> dict:
    """
    Repair stale native DayZ appworkshop records without touching current mods.

    By default, removes only ACF item IDs that Steam reports unsubscribed and
    that do not have a real native workshop content folder.

    When remove_non_subscribed_installed=True, treats every non-subscribed
    direct ACF item as repair debris and removes it through delete_ugc_mod(),
    preserving subscribed items.
    """
    result = {
        "ok": False,
        "appid": int(appid),
        "remove_non_subscribed_installed": bool(remove_non_subscribed_installed),
        "acf_ids": [],
        "removed_ids": [],
        "removed_count": 0,
        "deleted_ids": [],
        "deleted_count": 0,
        "kept_ids": [],
        "kept_count": 0,
        "kept_subscribed_ids": [],
        "failures": [],
        "cleanup": {},
        "error": "",
    }

    acf_ids: set[int] = set()
    try:
        for path in _native_appworkshop_acf_paths(int(appid)):
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            acf_ids.update(_extract_workshop_acf_ids_from_text(text))
    except Exception as exc:
        result["error"] = str(exc)
        return result

    ids = sorted(acf_ids)
    result["acf_ids"] = ids
    if not ids:
        result["ok"] = True
        return result

    try:
        states = query_ugc_state(ids, appid=int(appid), timeout=60)
    except Exception as exc:
        result["error"] = f"failed to query UGC state: {exc}"
        return result

    if bool(remove_non_subscribed_installed):
        kept_subscribed: list[int] = []
        delete_candidates: list[int] = []
        for mid in ids:
            state = dict(states.get(mid) or {})
            if bool(state.get("subscribed", False)):
                kept_subscribed.append(mid)
            else:
                delete_candidates.append(mid)

        deleted: list[int] = []
        failures: list[dict] = []
        for mid in delete_candidates:
            try:
                delete_result = delete_ugc_mod(mid, appid=int(appid), log_fn=log_fn)
            except Exception as exc:
                failures.append({"id": mid, "error": str(exc)})
                continue
            if bool(delete_result.get("ok", False)):
                deleted.append(mid)
            else:
                failures.append(
                    {
                        "id": mid,
                        "error": str(delete_result.get("error") or "delete failed"),
                        "result": delete_result,
                    }
                )

        failed_ids = {int(item.get("id")) for item in failures if item.get("id") is not None}
        result["deleted_ids"] = deleted
        result["deleted_count"] = len(deleted)
        result["removed_ids"] = deleted
        result["removed_count"] = len(deleted)
        result["kept_subscribed_ids"] = kept_subscribed
        result["kept_ids"] = sorted(set(kept_subscribed) | failed_ids)
        result["kept_count"] = len(result["kept_ids"])
        result["failures"] = failures
        result["ok"] = not failures
        if failures:
            result["error"] = f"failed to delete {len(failures)} non-subscribed workshop item(s)"
        return result

    stale: list[int] = []
    kept: list[int] = []
    kept_subscribed: list[int] = []
    for mid in ids:
        state = dict(states.get(mid) or {})
        subscribed = bool(state.get("subscribed", False))
        has_real_folder = _has_real_native_workshop_folder(mid, appid=int(appid), states=[state])
        if subscribed:
            kept.append(mid)
            kept_subscribed.append(mid)
            continue
        if has_real_folder:
            kept.append(mid)
            continue
        stale.append(mid)

    cleanup = remove_workshop_acf_entries(stale, appid=int(appid), log_fn=log_fn)
    result["cleanup"] = cleanup
    result["removed_ids"] = list(cleanup.get("removed_ids") or [])
    result["removed_count"] = len(result["removed_ids"])
    kept_set = set(kept) | (set(stale) - set(result["removed_ids"]))
    result["kept_ids"] = sorted(kept_set)
    result["kept_count"] = len(result["kept_ids"])
    result["kept_subscribed_ids"] = kept_subscribed
    result["ok"] = bool(cleanup.get("ok", False))
    result["error"] = str(cleanup.get("error") or "")
    return result


def delete_ugc_mod(mod_id, *, appid=DAYZ_APPID, timeout=120, log_fn=None) -> dict:
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
            else:
                print(message)
        except Exception:
            pass

    result = {
        "id": None,
        "unsubscribed": False,
        "deleted_folder": False,
        "acf_cleanup": {},
        "removed_symlinks": [],
        "before": {},
        "after": {},
        "ok": False,
        "error": "",
    }

    try:
        mid = int(mod_id)
    except Exception:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result
    result["id"] = mid
    if mid <= 0:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result

    try:
        before_by_id = query_ugc_state([mid], appid=appid, timeout=min(float(timeout), 60.0))
        before = dict(before_by_id.get(mid) or {})
        result["before"] = before

        if bool(before.get("subscribed", False)):
            unsubscribe_ugc_items([mid], appid=appid, timeout=timeout)
            after_unsub = query_ugc_state([mid], appid=appid, timeout=min(float(timeout), 60.0)).get(mid) or {}
            if bool(after_unsub.get("subscribed", False)):
                result["after"] = dict(after_unsub)
                result["error"] = "Steam still reports item subscribed after unsubscribe"
                return result
            result["unsubscribed"] = True

        after_by_id = query_ugc_state([mid], appid=appid, timeout=min(float(timeout), 60.0))
        after = dict(after_by_id.get(mid) or {})
        result["after"] = after

        for state in (before, after):
            reported_folder = str(state.get("install_folder") or "").strip()
            if reported_folder and _safe_workshop_content_path(reported_folder, mod_id=mid, appid=int(appid)) is None:
                result["error"] = f"refusing unsafe install folder: {reported_folder}"
                return result

        deleted_any_folder = False
        for content_dir in _candidate_content_dirs(mid, int(appid), before, after):
            safe = _safe_workshop_content_path(str(content_dir), mod_id=mid, appid=int(appid))
            if safe is None:
                result["error"] = f"refusing unsafe install folder: {content_dir}"
                return result
            if _delete_dir_if_present(safe, log_fn=log):
                deleted_any_folder = True

        for root in _native_steam_roots():
            downloads_dir = root / "steamapps/workshop/downloads" / str(int(appid)) / str(mid)
            safe_dir = _safe_workshop_download_dir(downloads_dir, mod_id=mid, appid=int(appid))
            if safe_dir is not None:
                _delete_dir_if_present(safe_dir, log_fn=log)
            patch_file = root / "steamapps/workshop/downloads" / f"state_{int(appid)}_{int(appid)}_{mid}.patch"
            safe_patch = _safe_workshop_patch_file(patch_file, mod_id=mid, appid=int(appid))
            if safe_patch is not None:
                _delete_file_if_present(safe_patch, log_fn=log)
            root_patch_file = root / "steamapps/workshop" / f"state_{int(appid)}_{int(appid)}_{mid}.patch"
            safe_root_patch = _safe_workshop_patch_file(root_patch_file, mod_id=mid, appid=int(appid))
            if safe_root_patch is not None:
                _delete_file_if_present(safe_root_patch, log_fn=log)

        result["deleted_folder"] = bool(deleted_any_folder)

        if deleted_any_folder and bool(after.get("installed", False)):
            after = _normalize_ugc_snapshot(after)
            result["after"] = after
            _cache_ugc_state({mid: after})

        try:
            from .steamcmd_mods import remove_dzll_symlinks_for_mod

            result["removed_symlinks"] = remove_dzll_symlinks_for_mod(mid, log_fn=log)
        except Exception as exc:
            result["error"] = f"failed to remove DZLL symlinks: {exc}"
            return result

        acf_cleanup = remove_workshop_acf_entries([mid], appid=int(appid), log_fn=log)
        result["acf_cleanup"] = acf_cleanup
        if not bool(acf_cleanup.get("ok", False)):
            result["error"] = str(acf_cleanup.get("error") or "failed to clean appworkshop ACF")
            return result

        _mark_metadata_deleted(mid)
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def delete_ugc_mod_local_files_after_unsubscribe(mod_id, *, appid=DAYZ_APPID, log_fn=None) -> dict:
    """
    Delete local DayZ Workshop state for an already-unsubscribed item.

    Intended for Delete All after Steam has been politely stopped. This does not
    call Steam UGC APIs; it only removes guarded native DayZ workshop paths,
    exact ACF entries, DZLL-owned symlinks, and cached metadata.
    """
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
            else:
                print(message)
        except Exception:
            pass

    result = {
        "id": None,
        "deleted_folder": False,
        "deleted_staging": False,
        "acf_cleanup": {},
        "removed_symlinks": [],
        "ok": False,
        "error": "",
    }

    try:
        mid = int(mod_id)
    except Exception:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result
    result["id"] = mid
    if mid <= 0:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result

    try:
        deleted_any_folder = False
        deleted_any_staging = False

        for root in _native_steam_roots():
            content_dir = root / "steamapps/workshop/content" / str(int(appid)) / str(mid)
            safe_content = _safe_workshop_content_path(str(content_dir), mod_id=mid, appid=int(appid))
            if safe_content is None:
                result["error"] = f"refusing unsafe install folder: {content_dir}"
                return result
            if _delete_dir_if_present(safe_content, log_fn=log):
                deleted_any_folder = True

            downloads_dir = root / "steamapps/workshop/downloads" / str(int(appid)) / str(mid)
            safe_downloads = _safe_workshop_download_dir(downloads_dir, mod_id=mid, appid=int(appid))
            if safe_downloads is not None and _delete_dir_if_present(safe_downloads, log_fn=log):
                deleted_any_staging = True

            patch_file = root / "steamapps/workshop/downloads" / f"state_{int(appid)}_{int(appid)}_{mid}.patch"
            safe_patch = _safe_workshop_patch_file(patch_file, mod_id=mid, appid=int(appid))
            if safe_patch is not None and _delete_file_if_present(safe_patch, log_fn=log):
                deleted_any_staging = True
            root_patch_file = root / "steamapps/workshop" / f"state_{int(appid)}_{int(appid)}_{mid}.patch"
            safe_root_patch = _safe_workshop_patch_file(root_patch_file, mod_id=mid, appid=int(appid))
            if safe_root_patch is not None and _delete_file_if_present(safe_root_patch, log_fn=log):
                deleted_any_staging = True

        result["deleted_folder"] = bool(deleted_any_folder)
        result["deleted_staging"] = bool(deleted_any_staging)

        try:
            from .steamcmd_mods import remove_dzll_symlinks_for_mod

            result["removed_symlinks"] = remove_dzll_symlinks_for_mod(mid, log_fn=log)
        except Exception as exc:
            result["error"] = f"failed to remove DZLL symlinks: {exc}"
            return result

        acf_cleanup = _remove_workshop_acf_entries_from_paths(
            _native_appworkshop_acf_paths(int(appid)),
            [mid],
            log_fn=log,
        )
        result["acf_cleanup"] = acf_cleanup
        if not bool(acf_cleanup.get("ok", False)):
            result["error"] = str(acf_cleanup.get("error") or "failed to clean appworkshop ACF")
            return result

        _mark_metadata_deleted(mid)
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def run_ugc_install(
    mod_ids,
    *,
    appid: int = DAYZ_APPID,
    cancel_event=None,
    progress_cb=None,
    names_by_id=None,
    allow_start_steam: bool = True,
    timeout: float = 3600,
) -> bool:
    ids = _dedupe_sorted_ids(mod_ids)
    if not ids:
        return True

    sessions = {mid: UGCModSession(id=mid) for mid in ids}
    _progress(progress_cb, {"type": "start", "appid": int(appid), "items": ids})
    _log_event(progress_cb, f"[JOIN] Steam UGC checking required mod readiness: {len(ids)} ids", ids=ids)

    def on_state_event(event: dict) -> None:
        if event.get("type") != "item":
            _progress(progress_cb, {"type": "helper_event", "event": event})
            return
        event = _normalize_ugc_snapshot(event)
        try:
            mid = int(event.get("id") or 0)
        except Exception:
            mid = 0
        session = sessions.get(mid)
        if session is None:
            return
        session.was_subscribed_before = bool(event.get("subscribed", False))
        session.was_installed_before = bool(event.get("installed", False))
        session.update_from_item(event)
        _cache_ugc_state({mid: event}, names_by_id=names_by_id)
        _progress(progress_cb, session.event())

    try:
        if not _run_ugc_native_steam_preflight(
            ids,
            appid=int(appid),
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            allow_start_steam=bool(allow_start_steam),
            timeout_s=UGC_PREFLIGHT_TIMEOUT_S,
        ):
            _progress(progress_cb, {"type": "done", "ok": False, "reason": "preflight_failed", "sessions": [s.event() for s in sessions.values()]})
            return False

        ok, _rc = _run_helper_json_lines(
            "state",
            appid=appid,
            timeout=min(float(timeout), 120.0),
            mod_ids=ids,
            cancel_event=cancel_event,
            on_event=on_state_event,
            progress_cb=progress_cb,
        )
        if not ok:
            _progress(progress_cb, {"type": "done", "ok": False, "reason": "initial_state_failed", "sessions": [s.event() for s in sessions.values()]})
            return False

        for mid, session in sorted(sessions.items()):
            _log_event(
                progress_cb,
                "[Steam UGC] item "
                f"{mid} ready={ugc_item_ready(session)} "
                f"installed={bool(session.installed_now)} "
                f"needs_update={bool(session.needs_update)} "
                f"downloading={bool(session.downloading)} "
                f"pending={bool(session.download_pending)}",
                id=mid,
                ready=ugc_item_ready(session),
                installed=bool(session.installed_now),
                needs_update=bool(session.needs_update),
                downloading=bool(session.downloading),
                download_pending=bool(session.download_pending),
            )

        not_ready = sorted(mid for mid, session in sessions.items() if not ugc_item_ready(session))
        if not not_ready:
            _log_event(progress_cb, f"[Steam UGC] All required Workshop items are ready: {len(ids)} ids", ids=ids)
            _progress(progress_cb, {"type": "done", "ok": True, "installed": ids, "ready": ids, "failed": [], "sessions": [s.event() for s in sessions.values()]})
            return True

        _log_event(progress_cb, f"[Steam UGC] required update/download ids: {not_ready}", ids=not_ready)

        for mid in not_ready:
            session = sessions[mid]
            if not session.was_subscribed_before:
                session.subscribed_by_dzll_this_join = True

        _progress(progress_cb, {"backend": "steam_ugc", "type": "status", "message": "Checking/Updating Required Mods"})

        def on_install_event(event: dict) -> None:
            event_type = event.get("type")
            if event_type in ("item", "request"):
                event = _normalize_ugc_snapshot(event)
                try:
                    mid = int(event.get("id") or 0)
                except Exception:
                    mid = 0
                session = sessions.get(mid)
                if session is not None:
                    session.update_from_item(event)
                    _cache_ugc_state({mid: event}, names_by_id=names_by_id)
                    _progress(progress_cb, session.event())
                _progress(progress_cb, {"type": "helper_event", "event": event})
                return
            if event_type == "done":
                for mid_raw in event.get("ready") or event.get("installed") or []:
                    try:
                        mid = int(mid_raw)
                    except Exception:
                        continue
                    session = sessions.get(mid)
                    if session is not None:
                        session.installed_now = True
                        session.needs_update = False
                        session.downloading = False
                        session.download_pending = False
                _progress(progress_cb, {"type": "helper_event", "event": event})
                return
            _progress(progress_cb, {"type": "helper_event", "event": event})

        ok, _rc = _run_helper_json_lines(
            "subscribe-download",
            appid=appid,
            timeout=float(timeout),
            mod_ids=not_ready,
            cancel_event=cancel_event,
            on_event=on_install_event,
            progress_cb=progress_cb,
        )

        if cancel_event is not None and cancel_event.is_set():
            _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
            _cleanup_subscriptions(sessions, appid=appid, progress_cb=progress_cb)
            installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
            ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
            failed = sorted(mid for mid in ids if mid not in ready)
            _progress(progress_cb, {"type": "done", "ok": False, "cancelled": True, "installed": installed, "ready": ready, "failed": failed, "sessions": [s.event() for s in sessions.values()]})
            return False

        installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
        ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
        failed = sorted(mid for mid in ids if mid not in ready)
        if ok and not failed:
            _progress(progress_cb, {"type": "done", "ok": True, "installed": installed, "ready": ready, "failed": [], "sessions": [s.event() for s in sessions.values()]})
            return True

        _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
        installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
        ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
        failed = sorted(mid for mid in ids if mid not in ready)
        _cleanup_subscriptions(sessions, appid=appid, progress_cb=progress_cb)
        _progress(progress_cb, {"type": "done", "ok": False, "installed": installed, "ready": ready, "failed": failed, "sessions": [s.event() for s in sessions.values()]})
        return False
    except KeyboardInterrupt:
        _progress(progress_cb, {"type": "cancelled", "reason": "keyboard_interrupt"})
        _log_event(progress_cb, "[Steam UGC] Cancel requested", reason="keyboard_interrupt")
        _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
        _cleanup_subscriptions(sessions, appid=appid, progress_cb=progress_cb)
        return False
    except Exception as exc:
        _progress(progress_cb, {"type": "error", "ok": False, "message": str(exc)})
        _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
        _cleanup_subscriptions(sessions, appid=appid, progress_cb=progress_cb)
        return False


def _parse_item_id(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dzll_launcher.steam_ugc_backend",
        description="Steam Client UGC backend for DayZ Workshop items.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="subscribe/download missing items and wait until installed")
    install.add_argument("--appid", type=int, default=DAYZ_APPID)
    install.add_argument("--timeout", type=float, default=3600.0)
    install.add_argument("item_ids", nargs="+", type=_parse_item_id)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "install":
        try:
            ok = run_ugc_install(
                args.item_ids,
                appid=int(args.appid),
                timeout=float(args.timeout),
                progress_cb=emit,
            )
        except KeyboardInterrupt:
            emit({"type": "cancelled", "reason": "keyboard_interrupt"})
            return 4
        return 0 if ok else 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
