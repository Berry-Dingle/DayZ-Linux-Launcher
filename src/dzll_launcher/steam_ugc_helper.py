#!/usr/bin/env python3
"""
Native Steamworks UGC helper for DayZ Workshop items.

This subprocess-friendly JSON-lines CLI is used by the Steam Client backend,
the default required-mod path. SteamCMD remains the advanced fallback.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_STEAM_INIT_FAILED = 2
EXIT_MISSING_SYMBOLS = 3
EXIT_TIMEOUT = 4
EXIT_UNEXPECTED = 5

DAYZ_APPID = 221100
SESSION_INIT_RETRY_S = 0.25


_protocol_stdout = None
_protocol_write_lock = threading.Lock()

ITEM_STATE_BITS = (
    (1, "Subscribed"),
    (2, "LegacyItem"),
    (4, "Installed"),
    (8, "NeedsUpdate"),
    (16, "Downloading"),
    (32, "DownloadPending"),
)


class CliError(Exception):
    exit_code = EXIT_BAD_ARGS


class SteamInitError(Exception):
    exit_code = EXIT_STEAM_INIT_FAILED


class MissingSymbolsError(Exception):
    exit_code = EXIT_MISSING_SYMBOLS


class HelperArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


@dataclass(frozen=True)
class SteamPaths:
    root: Path
    libsteam_api: Path
    library_dirs: tuple[Path, ...]


@dataclass
class ItemSnapshot:
    item_id: int
    state: int
    state_names: list[str]
    subscribed: bool
    installed: bool
    needs_update: bool
    downloading: bool
    download_pending: bool
    download_bytes: int
    total_bytes: int
    size_on_disk: int
    install_folder: str | None

    @property
    def ready(self) -> bool:
        return (
            bool(self.installed)
            and not bool(self.needs_update)
            and not bool(self.downloading)
            and not bool(self.download_pending)
        )

    def event(self) -> dict:
        return {
            "type": "item",
            "id": self.item_id,
            "state": self.state,
            "state_names": self.state_names,
            "subscribed": self.subscribed,
            "installed": self.installed,
            "needs_update": self.needs_update,
            "downloading": self.downloading,
            "download_pending": self.download_pending,
            "ready": self.ready,
            "download_bytes": self.download_bytes,
            "total_bytes": self.total_bytes,
            "size_on_disk": self.size_on_disk,
            "install_folder": self.install_folder,
        }

    def progress_key(self) -> tuple:
        return (
            self.state,
            tuple(self.state_names),
            self.download_bytes,
            self.total_bytes,
            self.size_on_disk,
            self.install_folder,
        )


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit(event: dict) -> None:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    stream = _protocol_stdout if _protocol_stdout is not None else sys.stdout
    with _protocol_write_lock:
        stream.write(payload + "\n")
        stream.flush()


_default_emit = emit


def _isolate_protocol_stdout() -> None:
    """Reserve the original stdout pipe for protocol frames only.

    Steamworks and any accidentally inherited process output continue to use
    file descriptor 1, which is redirected to stderr after a private duplicate
    of the parent protocol pipe has been retained for :func:`emit`.
    """
    global _protocol_stdout
    if _protocol_stdout is not None:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        ctypes.CDLL(None).fflush(None)
    except Exception:
        pass
    protocol_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(protocol_fd, False)
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        _protocol_stdout = os.fdopen(
            protocol_fd,
            "w",
            encoding=getattr(sys.stdout, "encoding", None) or "utf-8",
            errors="backslashreplace",
            buffering=1,
        )
    except Exception:
        os.close(protocol_fd)
        raise


def emit_error(code: str, message: str) -> None:
    emit({"type": "error", "ok": False, "code": code, "message": message})


def parse_item_id(raw: str) -> int:
    try:
        item_id = int(str(raw).strip(), 10)
    except Exception:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    if item_id <= 0:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    if item_id > 0xFFFFFFFFFFFFFFFF:
        raise argparse.ArgumentTypeError(f"item id is too large: {raw!r}")
    return item_id


def dedupe_sorted_item_ids(item_ids: Iterable[int]) -> list[int]:
    out = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
    if not out:
        raise CliError("at least one valid Workshop item id is required")
    return out


def likely_steam_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".local/share/Steam",
        home / ".steam/steam",
    ]


def detect_steam_paths() -> SteamPaths:
    candidates: list[tuple[Path, Path]] = []
    for root in likely_steam_roots():
        candidates.append((root, root / "steamrt64/libsteam_api.so"))
    for root in likely_steam_roots():
        candidates.append((root, root / "steamapps/common/DayZServer/libsteam_api.so"))

    for root, lib in candidates:
        try:
            if lib.is_file():
                dirs = (
                    root / "linux64",
                    root / "steamrt64",
                    root / "steamapps/common/DayZServer",
                )
                existing_dirs = tuple(path for path in dirs if path.is_dir())
                return SteamPaths(root=root, libsteam_api=lib, library_dirs=existing_dirs)
        except Exception:
            continue
    raise CliError("could not find native Steam libsteam_api.so")


def ensure_ld_library_path(paths: SteamPaths) -> None:
    if os.environ.get("DZLL_STEAM_UGC_HELPER_REEXEC") == "1":
        return

    wanted = [str(path) for path in paths.library_dirs]
    current = os.environ.get("LD_LIBRARY_PATH", "")
    current_parts = [part for part in current.split(":") if part]
    merged = []
    seen = set()
    for part in wanted + current_parts:
        if part and part not in seen:
            merged.append(part)
            seen.add(part)

    if merged == current_parts:
        return

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(merged)
    env["DZLL_STEAM_UGC_HELPER_REEXEC"] = "1"
    eprint(f"[steam-ugc-helper] re-exec with LD_LIBRARY_PATH={env['LD_LIBRARY_PATH']}")
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


def setup_temp_appid(appid: int) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="dzll_steam_ugc_")
    Path(tmp.name, "steam_appid.txt").write_text(f"{int(appid)}\n", encoding="utf-8")
    os.environ["SteamAppId"] = str(int(appid))
    os.environ["SteamGameId"] = str(int(appid))
    os.chdir(tmp.name)
    return tmp


def nm_symbols(lib_path: Path) -> set[str]:
    try:
        proc = subprocess.run(
            ["nm", "-D", str(lib_path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except Exception:
        return set()
    if proc.returncode != 0:
        return set()
    symbols = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts:
            symbols.add(parts[-1])
    return symbols


def find_ugc_accessor(lib, lib_path: Path) -> str:
    symbols = nm_symbols(lib_path)
    candidates = []
    for name in symbols:
        m = re.fullmatch(r"SteamAPI_SteamUGC_v(\d+)", name)
        if m:
            candidates.append((int(m.group(1)), name))

    if not candidates:
        for version in range(99, 0, -1):
            name = f"SteamAPI_SteamUGC_v{version:03d}"
            try:
                getattr(lib, name)
                candidates.append((version, name))
                break
            except AttributeError:
                continue

    if not candidates:
        raise MissingSymbolsError("missing SteamAPI_SteamUGC_vXXX accessor")
    return max(candidates)[1]


def find_steam_user_accessor(lib, lib_path: Path) -> str | None:
    symbols = nm_symbols(lib_path)
    candidates = []
    for name in symbols:
        match = re.fullmatch(r"SteamAPI_SteamUser_v(\d+)", name)
        if match:
            candidates.append((int(match.group(1)), name))
    if not candidates:
        for version in range(99, 0, -1):
            name = f"SteamAPI_SteamUser_v{version:03d}"
            try:
                getattr(lib, name)
                candidates.append((version, name))
                break
            except AttributeError:
                continue
    return max(candidates)[1] if candidates else None


class SteamUGC:
    def __init__(self, paths: SteamPaths):
        self.paths = paths
        self.lib = ctypes.CDLL(str(paths.libsteam_api))
        self.ugc_accessor_name = find_ugc_accessor(self.lib, paths.libsteam_api)
        self.steam_user_accessor_name = find_steam_user_accessor(
            self.lib, paths.libsteam_api,
        )
        self.ugc = None
        self.steam_user = None
        self.init_ok = False
        self._bind_required()
        self._bind_optional()

    def _required(self, name: str):
        try:
            return getattr(self.lib, name)
        except AttributeError:
            raise MissingSymbolsError(f"missing required Steamworks symbol: {name}")

    def _optional(self, name: str):
        try:
            return getattr(self.lib, name)
        except AttributeError:
            return None

    def _bind_required(self) -> None:
        self.InitSafe = self._required("SteamAPI_InitSafe")
        self.InitSafe.argtypes = []
        self.InitSafe.restype = ctypes.c_bool

        self.Shutdown = self._required("SteamAPI_Shutdown")
        self.Shutdown.argtypes = []
        self.Shutdown.restype = None

        self.RunCallbacks = self._required("SteamAPI_RunCallbacks")
        self.RunCallbacks.argtypes = []
        self.RunCallbacks.restype = None

        self.SteamUGC = self._required(self.ugc_accessor_name)
        self.SteamUGC.argtypes = []
        self.SteamUGC.restype = ctypes.c_void_p

        self.GetItemState = self._required("SteamAPI_ISteamUGC_GetItemState")
        self.GetItemState.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.GetItemState.restype = ctypes.c_uint32

        self.SubscribeItem = self._required("SteamAPI_ISteamUGC_SubscribeItem")
        self.SubscribeItem.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.SubscribeItem.restype = ctypes.c_uint64

        self.UnsubscribeItem = self._required("SteamAPI_ISteamUGC_UnsubscribeItem")
        self.UnsubscribeItem.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self.UnsubscribeItem.restype = ctypes.c_uint64

        self.DownloadItem = self._required("SteamAPI_ISteamUGC_DownloadItem")
        self.DownloadItem.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_bool]
        self.DownloadItem.restype = ctypes.c_bool

    def _bind_optional(self) -> None:
        self.GetNumSubscribedItems = self._optional(
            "SteamAPI_ISteamUGC_GetNumSubscribedItems"
        )
        if self.GetNumSubscribedItems is not None:
            self.GetNumSubscribedItems.argtypes = [ctypes.c_void_p]
            self.GetNumSubscribedItems.restype = ctypes.c_uint32

        self.GetSubscribedItems = self._optional(
            "SteamAPI_ISteamUGC_GetSubscribedItems"
        )
        if self.GetSubscribedItems is not None:
            self.GetSubscribedItems.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.c_uint32,
            ]
            self.GetSubscribedItems.restype = ctypes.c_uint32

        self.SteamUser = None
        self.BLoggedOn = self._optional("SteamAPI_ISteamUser_BLoggedOn")
        self.GetSteamID = self._optional("SteamAPI_ISteamUser_GetSteamID")
        if self.steam_user_accessor_name:
            self.SteamUser = self._optional(self.steam_user_accessor_name)
        if self.SteamUser is not None:
            self.SteamUser.argtypes = []
            self.SteamUser.restype = ctypes.c_void_p
        if self.BLoggedOn is not None:
            self.BLoggedOn.argtypes = [ctypes.c_void_p]
            self.BLoggedOn.restype = ctypes.c_bool
        if self.GetSteamID is not None:
            self.GetSteamID.argtypes = [ctypes.c_void_p]
            self.GetSteamID.restype = ctypes.c_uint64

        self.GetItemDownloadInfo = self._optional("SteamAPI_ISteamUGC_GetItemDownloadInfo")
        if self.GetItemDownloadInfo is not None:
            self.GetItemDownloadInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint64,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
            self.GetItemDownloadInfo.restype = ctypes.c_bool

        self.GetItemInstallInfo = self._optional("SteamAPI_ISteamUGC_GetItemInstallInfo")
        if self.GetItemInstallInfo is not None:
            self.GetItemInstallInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint64,
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            self.GetItemInstallInfo.restype = ctypes.c_bool

    def init(self) -> None:
        if not self.InitSafe():
            raise SteamInitError("SteamAPI_InitSafe returned false")
        self.init_ok = True
        self.ugc = self.SteamUGC()
        if not self.ugc:
            raise MissingSymbolsError(f"{self.ugc_accessor_name} returned null")
        if self.SteamUser is not None:
            self.steam_user = self.SteamUser()

    def shutdown(self) -> None:
        if self.init_ok:
            try:
                self.Shutdown()
            finally:
                self.init_ok = False

    def run_callbacks(self) -> None:
        try:
            self.RunCallbacks()
        except Exception:
            pass

    def subscribe(self, item_id: int) -> int:
        return int(self.SubscribeItem(self.ugc, ctypes.c_uint64(int(item_id))))

    def unsubscribe(self, item_id: int) -> int:
        return int(self.UnsubscribeItem(self.ugc, ctypes.c_uint64(int(item_id))))

    def download(self, item_id: int, high_priority: bool = True) -> bool:
        return bool(self.DownloadItem(self.ugc, ctypes.c_uint64(int(item_id)), ctypes.c_bool(bool(high_priority))))

    def is_logged_on(self) -> bool:
        if self.BLoggedOn is None or not self.steam_user:
            return False
        try:
            return bool(self.BLoggedOn(self.steam_user))
        except Exception:
            return False

    def steam_id(self) -> int:
        if self.GetSteamID is None or not self.steam_user:
            return 0
        try:
            return int(self.GetSteamID(self.steam_user))
        except Exception:
            return 0

    def subscribed_item_ids(self) -> list[int]:
        if self.GetNumSubscribedItems is None or self.GetSubscribedItems is None:
            raise MissingSymbolsError(
                "Steam UGC subscription enumeration symbols are unavailable"
            )
        count = int(self.GetNumSubscribedItems(self.ugc))
        if count <= 0:
            return []
        values = (ctypes.c_uint64 * count)()
        returned = int(
            self.GetSubscribedItems(
                self.ugc, values, ctypes.c_uint32(count),
            )
        )
        if returned != count:
            raise SteamInitError(
                "Steam returned a partial subscription inventory "
                f"({returned}/{count})"
            )
        return sorted({int(values[index]) for index in range(returned) if int(values[index]) > 0})

    def snapshot(self, item_id: int) -> ItemSnapshot:
        state = int(self.GetItemState(self.ugc, ctypes.c_uint64(int(item_id))))
        download_bytes = 0
        total_bytes = 0
        size_on_disk_value = 0
        install_folder = None

        if self.GetItemDownloadInfo is not None:
            try:
                downloaded = ctypes.c_uint64(0)
                total = ctypes.c_uint64(0)
                if self.GetItemDownloadInfo(self.ugc, int(item_id), ctypes.byref(downloaded), ctypes.byref(total)):
                    download_bytes = int(downloaded.value)
                    total_bytes = int(total.value)
            except Exception:
                download_bytes = 0
                total_bytes = 0

        if self.GetItemInstallInfo is not None:
            try:
                size_on_disk = ctypes.c_uint64(0)
                folder_buf = ctypes.create_string_buffer(4096)
                timestamp = ctypes.c_uint32(0)
                if self.GetItemInstallInfo(
                    self.ugc,
                    int(item_id),
                    ctypes.byref(size_on_disk),
                    folder_buf,
                    ctypes.c_uint32(len(folder_buf)),
                    ctypes.byref(timestamp),
                ):
                    size_on_disk_value = int(size_on_disk.value)
                    raw = folder_buf.value
                    if raw:
                        install_folder = raw.decode("utf-8", "replace")
            except Exception:
                size_on_disk_value = 0
                install_folder = None

        names = [name for bit, name in ITEM_STATE_BITS if state & bit]
        return ItemSnapshot(
            item_id=int(item_id),
            state=state,
            state_names=names,
            subscribed=bool(state & 1),
            installed=bool(state & 4),
            needs_update=bool(state & 8),
            downloading=bool(state & 16),
            download_pending=bool(state & 32),
            download_bytes=download_bytes,
            total_bytes=total_bytes,
            size_on_disk=size_on_disk_value,
            install_folder=install_folder,
        )


def init_steam(appid: int) -> tuple[SteamUGC, SteamPaths, tempfile.TemporaryDirectory]:
    paths = detect_steam_paths()
    ensure_ld_library_path(paths)
    tmp = setup_temp_appid(appid)
    steam = None
    try:
        steam = SteamUGC(paths)
        steam.init()
        emit(
            {
                "type": "init",
                "ok": True,
                "appid": int(appid),
                "steam_root": str(paths.root),
                "lib": str(paths.libsteam_api),
                "ugc_accessor": steam.ugc_accessor_name,
                "flatpak_environment": any(
                    "com.valvesoftware.steam" in f"{key}={value}".casefold()
                    or str(key).upper().startswith("FLATPAK_")
                    for key, value in os.environ.items()
                ),
            }
        )
        return steam, paths, tmp
    except Exception:
        if steam is not None:
            steam.shutdown()
        tmp.cleanup()
        raise


def emit_snapshots(steam: SteamUGC, item_ids: list[int], last_seen: dict[int, tuple] | None = None) -> None:
    for item_id in item_ids:
        snap = steam.snapshot(item_id)
        key = snap.progress_key()
        if last_seen is not None and last_seen.get(item_id) == key:
            continue
        if last_seen is not None:
            last_seen[item_id] = key
        emit(snap.event())


def _session_emit(request_id, event_type: str, **values) -> None:
    event = {"type": str(event_type), "request_id": request_id}
    event.update(values)
    emit(event)


def _session_item_event(request_id, snap: ItemSnapshot) -> None:
    event = snap.event()
    event["request_id"] = request_id
    emit(event)


def _capture_subscription_inventory(steam: SteamUGC) -> tuple[bool, bool, list[int], int]:
    """Require stable account identity and three matching full enumerations."""
    run_callbacks = getattr(steam, "run_callbacks", None)
    is_logged_on = getattr(steam, "is_logged_on", None)
    steam_id_fn = getattr(steam, "steam_id", None)
    subscribed_item_ids = getattr(steam, "subscribed_item_ids", None)
    if not all(
        callable(fn)
        for fn in (run_callbacks, is_logged_on, steam_id_fn, subscribed_item_ids)
    ):
        return False, False, [], 0
    previous: tuple[int, ...] | None = None
    previous_steam_id = 0
    matching_samples = 0
    logged_on = False
    for attempt in range(5):
        run_callbacks()
        logged_on = bool(is_logged_on())
        steam_id = int(steam_id_fn() or 0)
        if not logged_on or steam_id <= 0:
            previous = None
            previous_steam_id = 0
            matching_samples = 0
        else:
            try:
                current = tuple(subscribed_item_ids())
            except Exception:
                return logged_on, False, [], steam_id
            if previous == current and previous_steam_id == steam_id:
                matching_samples += 1
                if matching_samples >= 2:
                    return True, True, list(current), steam_id
            else:
                matching_samples = 0
            previous = current
            previous_steam_id = steam_id
        if attempt < 4:
            time.sleep(0.35)
    return logged_on, False, list(previous or ()), previous_steam_id


def _capture_native_readiness(steam: SteamUGC) -> tuple[bool, bool, int]:
    """Prove a stable logged-on Steam identity without querying Workshop IDs."""
    run_callbacks = getattr(steam, "run_callbacks", None)
    is_logged_on = getattr(steam, "is_logged_on", None)
    steam_id_fn = getattr(steam, "steam_id", None)
    if not all(callable(fn) for fn in (run_callbacks, is_logged_on, steam_id_fn)):
        return False, False, 0
    previous_steam_id = 0
    matching_samples = 0
    logged_on = False
    for attempt in range(4):
        run_callbacks()
        logged_on = bool(is_logged_on())
        steam_id = int(steam_id_fn() or 0)
        if not logged_on or steam_id <= 0:
            previous_steam_id = 0
            matching_samples = 0
        elif steam_id == previous_steam_id:
            matching_samples += 1
            if matching_samples >= 2:
                return True, True, steam_id
        else:
            previous_steam_id = steam_id
            matching_samples = 0
        if attempt < 3:
            time.sleep(0.35)
    return logged_on, False, previous_steam_id


class _SessionCancelled(Exception):
    def __init__(self, reason: str, *, control_request_id=None):
        super().__init__(reason)
        self.reason = str(reason)
        self.control_request_id = control_request_id


def _session_check_control(
    commands: queue.Queue,
    *,
    active_request_id,
) -> None:
    """Consume cancellation/shutdown controls while a long command is active."""
    while True:
        try:
            message = commands.get_nowait()
        except queue.Empty:
            return
        if message is None:
            raise _SessionCancelled("parent_eof")
        if not isinstance(message, dict):
            _session_emit(None, "recoverable_error", error="malformed command")
            continue
        command = str(message.get("command") or "")
        request_id = message.get("request_id")
        if command == "cancel":
            target = message.get("target_request_id")
            if target in (None, active_request_id):
                _session_emit(request_id, "cancellation_ack", target_request_id=active_request_id)
                raise _SessionCancelled("cancelled")
            _session_emit(request_id, "recoverable_error", error="cancel target is not active")
        elif command == "shutdown":
            _session_emit(request_id, "command_accepted", command="shutdown")
            raise _SessionCancelled(
                "shutdown", control_request_id=request_id,
            )
        else:
            _session_emit(request_id, "recoverable_error", error="another command is active")


def _session_query_state(steam: SteamUGC, request_id, item_ids: list[int]) -> None:
    for item_id in item_ids:
        _session_item_event(request_id, steam.snapshot(item_id))
    _session_emit(request_id, "command_result", ok=True, command="query_state", items=item_ids)


def _session_subscribe_download(
    steam: SteamUGC,
    commands: queue.Queue,
    request_id,
    item_ids: list[int],
    timeout: float,
) -> None:
    last_seen: dict[int, tuple] = {}
    for item_id in item_ids:
        snap = steam.snapshot(item_id)
        subscribe_call_result = None
        if not snap.subscribed:
            subscribe_call_result = steam.subscribe(item_id)
        download_requested = steam.download(item_id, True)
        event = snap.event()
        event.update(
            {
                "type": "request",
                "request_id": request_id,
                "subscribe_call_result": subscribe_call_result,
                "download_requested": bool(download_requested),
                "high_priority": True,
            }
        )
        emit(event)

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        _session_check_control(commands, active_request_id=request_id)
        steam.run_callbacks()
        ready = []
        installed = []
        for item_id in item_ids:
            snap = steam.snapshot(item_id)
            key = snap.progress_key()
            if last_seen.get(item_id) != key:
                last_seen[item_id] = key
                _session_item_event(request_id, snap)
            if snap.installed:
                installed.append(item_id)
            if snap.ready:
                ready.append(item_id)
        failed = [item_id for item_id in item_ids if item_id not in ready]
        if not failed:
            _session_emit(
                request_id, "command_result", ok=True,
                command="subscribe_download", installed=installed, ready=ready, failed=[],
            )
            return
        if time.monotonic() >= deadline:
            _session_emit(
                request_id, "command_result", ok=False,
                command="subscribe_download", installed=installed, ready=ready,
                failed=failed, reason="timeout",
            )
            return
        time.sleep(0.1)


def _session_unsubscribe(
    steam: SteamUGC,
    commands: queue.Queue,
    request_id,
    item_ids: list[int],
    timeout: float,
) -> None:
    for item_id in item_ids:
        if steam.snapshot(item_id).subscribed:
            steam.unsubscribe(item_id)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        _session_check_control(commands, active_request_id=request_id)
        steam.run_callbacks()
        snapshots = [steam.snapshot(item_id) for item_id in item_ids]
        for snap in snapshots:
            _session_item_event(request_id, snap)
        failed = [snap.item_id for snap in snapshots if snap.subscribed]
        if not failed:
            _session_emit(
                request_id, "command_result", ok=True,
                command="unsubscribe", unsubscribed=item_ids, failed=[],
            )
            return
        if time.monotonic() >= deadline:
            _session_emit(
                request_id, "command_result", ok=False,
                command="unsubscribe",
                unsubscribed=[
                    snap.item_id for snap in snapshots if not snap.subscribed
                ],
                failed=failed, reason="timeout",
            )
            return
        time.sleep(0.1)


def command_session(args) -> int:
    """Serve sequential JSON-line requests under one SteamAPI initialization."""
    commands: queue.Queue = queue.Queue()

    def read_commands() -> None:
        try:
            for raw in sys.stdin:
                try:
                    value = json.loads(raw)
                except Exception:
                    commands.put({"command": "__malformed__", "request_id": None})
                    continue
                commands.put(value)
        finally:
            commands.put(None)

    threading.Thread(target=read_commands, daemon=True).start()
    original_cwd = os.getcwd()
    paths = detect_steam_paths()
    ensure_ld_library_path(paths)
    # Tests which replace emit with an in-process collector do not own a real
    # protocol pipe.  The executable helper always uses the default emitter.
    if emit is _default_emit:
        _isolate_protocol_stdout()
    tmp = setup_temp_appid(args.appid)
    _session_emit(
        None, "session_starting", appid=int(args.appid),
        temp_dir=str(tmp.name), helper_pid=int(os.getpid()),
    )
    steam = None
    shutdown_reason = "normal"
    shutdown_request_id = None
    pending_messages: list[dict] = []
    accepted_request_ids: set = set()
    try:
        steam = SteamUGC(paths)
        while True:
            try:
                steam.init()
                break
            except SteamInitError:
                try:
                    message = commands.get(timeout=SESSION_INIT_RETRY_S)
                except queue.Empty:
                    _session_emit(None, "session_waiting", reason="steam_not_ready")
                    continue
                if message is None:
                    shutdown_reason = "parent_eof"
                    return EXIT_OK
                if not isinstance(message, dict):
                    _session_emit(None, "recoverable_error", error="malformed command")
                    continue
                command = str(message.get("command") or "")
                request_id = message.get("request_id")
                if command == "shutdown":
                    _session_emit(request_id, "command_accepted", command=command)
                    shutdown_reason = command
                    shutdown_request_id = request_id
                    return EXIT_OK
                if command == "cancel":
                    target = message.get("target_request_id")
                    removed = False
                    for pending in list(pending_messages):
                        if pending.get("request_id") == target:
                            pending_messages.remove(pending)
                            removed = True
                    _session_emit(
                        request_id, "cancellation_ack",
                        target_request_id=target, pending=removed,
                    )
                    if removed:
                        _session_emit(
                            target, "command_result", ok=False, cancelled=True,
                            reason="cancelled_before_session_ready",
                        )
                    continue
                if not any(
                    pending.get("request_id") == request_id
                    for pending in pending_messages
                ):
                    pending_messages.append(message)
                    _session_emit(request_id, "command_accepted", command=command)
                    accepted_request_ids.add(request_id)
        _session_emit(
            None, "session_ready", appid=int(args.appid),
            helper_pid=int(os.getpid()), ugc_accessor=steam.ugc_accessor_name,
        )

        while True:
            message = pending_messages.pop(0) if pending_messages else commands.get()
            if message is None:
                shutdown_reason = "parent_eof"
                break
            if not isinstance(message, dict):
                _session_emit(None, "recoverable_error", error="malformed command")
                continue
            command = str(message.get("command") or "")
            request_id = message.get("request_id")
            if not request_id:
                _session_emit(None, "recoverable_error", error="request_id is required")
                continue
            if command == "__malformed__":
                _session_emit(request_id, "recoverable_error", error="malformed JSON")
                continue
            if command == "shutdown":
                _session_emit(request_id, "command_accepted", command=command)
                shutdown_reason = "shutdown"
                shutdown_request_id = request_id
                break
            if command == "cancel":
                _session_emit(request_id, "cancellation_ack", reason="no_active_command")
                continue
            if command not in {"query_state", "subscribe_download", "unsubscribe"}:
                _session_emit(request_id, "recoverable_error", error=f"unknown command: {command}")
                continue
            try:
                item_ids = dedupe_sorted_item_ids(message.get("item_ids") or [])
                timeout = max(0.0, float(message.get("timeout", 120.0)))
            except Exception as exc:
                _session_emit(request_id, "command_result", ok=False, error=str(exc))
                continue
            if request_id not in accepted_request_ids:
                _session_emit(request_id, "command_accepted", command=command)
                accepted_request_ids.add(request_id)
            try:
                if command == "query_state":
                    _session_query_state(steam, request_id, item_ids)
                elif command == "subscribe_download":
                    _session_subscribe_download(
                        steam, commands, request_id, item_ids, timeout,
                    )
                else:
                    _session_unsubscribe(steam, commands, request_id, item_ids, timeout)
            except _SessionCancelled as exc:
                if exc.reason in ("shutdown", "parent_eof"):
                    shutdown_reason = exc.reason
                    shutdown_request_id = exc.control_request_id
                    break
                _session_emit(
                    request_id, "command_result", ok=False, cancelled=True,
                    command=command, reason=exc.reason,
                )
            except Exception as exc:
                _session_emit(
                    request_id, "fatal_session_error", ok=False,
                    command=command, error=str(exc),
                )
                return EXIT_UNEXPECTED
        return EXIT_OK
    finally:
        steamapi_shutdown = False
        temp_cleanup = False
        shutdown_error = ""
        try:
            if steam is not None:
                steam.shutdown()
            steamapi_shutdown = True
        except Exception as exc:
            shutdown_error = f"SteamAPI shutdown failed: {exc}"
        try:
            os.chdir(original_cwd)
            tmp.cleanup()
            temp_cleanup = not Path(tmp.name).exists()
        except Exception as exc:
            if not shutdown_error:
                shutdown_error = f"temporary AppID cleanup failed: {exc}"
        _session_emit(
            shutdown_request_id,
            "shutdown_complete",
            ok=bool(steamapi_shutdown and temp_cleanup and not shutdown_error),
            reason=shutdown_reason,
            steamapi_shutdown=steamapi_shutdown,
            temp_cleanup=temp_cleanup,
            error=shutdown_error,
        )
        if shutdown_error:
            raise RuntimeError(shutdown_error)


def command_state(args) -> int:
    steam = None
    tmp = None
    try:
        item_ids = dedupe_sorted_item_ids(args.item_ids)
        steam, _paths, tmp = init_steam(args.appid)
        logged_on, inventory_complete, subscribed_ids, steam_id = (
            _capture_subscription_inventory(steam)
        )
        emit_snapshots(steam, item_ids)
        emit({
            "type": "done",
            "ok": True,
            "items": item_ids,
            "failed": [],
            "logged_on": logged_on,
            "steam_id": steam_id,
            "subscription_inventory_complete": inventory_complete,
            "subscription_count": len(subscribed_ids),
            "subscribed_item_ids": subscribed_ids,
        })
        return EXIT_OK
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def command_readiness(args) -> int:
    """Check native SteamAPI/UGC and account readiness without an item query."""
    steam = None
    tmp = None
    try:
        steam, _paths, tmp = init_steam(args.appid)
        logged_on, identity_stable, steam_id = _capture_native_readiness(steam)
        ok = bool(logged_on and identity_stable and steam_id > 0)
        emit({
            "type": "done",
            "ok": ok,
            "logged_on": logged_on,
            "identity_stable": identity_stable,
            "steam_id": steam_id,
            "ugc_ready": bool(steam.ugc),
        })
        return EXIT_OK if ok else EXIT_STEAM_INIT_FAILED
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def command_subscribe(args) -> int:
    steam = None
    tmp = None
    try:
        item_ids = dedupe_sorted_item_ids(args.item_ids)
        timeout = max(0.0, float(args.timeout))
        steam, _paths, tmp = init_steam(args.appid)
        last_seen: dict[int, tuple] = {}

        emit_snapshots(steam, item_ids, last_seen)
        for item_id in item_ids:
            snap = steam.snapshot(item_id)
            if not snap.subscribed:
                steam.subscribe(item_id)
        deadline = time.monotonic() + timeout

        while True:
            steam.run_callbacks()
            emit_snapshots(steam, item_ids, last_seen)
            installed = []
            ready = []
            failed = []
            for item_id in item_ids:
                snap = steam.snapshot(item_id)
                if snap.installed:
                    installed.append(item_id)
                if snap.ready:
                    ready.append(item_id)
                else:
                    failed.append(item_id)
            if not failed:
                emit({"type": "done", "ok": True, "installed": installed, "ready": ready, "failed": []})
                return EXIT_OK
            if time.monotonic() >= deadline:
                emit({"type": "done", "ok": False, "installed": installed, "ready": ready, "failed": failed})
                return EXIT_TIMEOUT
            time.sleep(1.0)
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def command_subscribe_download(args) -> int:
    steam = None
    tmp = None
    try:
        item_ids = dedupe_sorted_item_ids(args.item_ids)
        timeout = max(0.0, float(args.timeout))
        steam, _paths, tmp = init_steam(args.appid)
        last_seen: dict[int, tuple] = {}

        emit_snapshots(steam, item_ids, last_seen)
        for item_id in item_ids:
            snap = steam.snapshot(item_id)
            subscribe_call_result = None
            if not snap.subscribed:
                subscribe_call_result = steam.subscribe(item_id)
            download_requested = steam.download(item_id, True)
            event = snap.event()
            event.update(
                {
                    "type": "request",
                    "id": item_id,
                    "subscribe_call_result": subscribe_call_result,
                    "download_requested": bool(download_requested),
                    "high_priority": True,
                }
            )
            emit(event)

        deadline = time.monotonic() + timeout

        while True:
            steam.run_callbacks()
            emit_snapshots(steam, item_ids, last_seen)
            installed = []
            ready = []
            failed = []
            for item_id in item_ids:
                snap = steam.snapshot(item_id)
                if snap.installed:
                    installed.append(item_id)
                if snap.ready:
                    ready.append(item_id)
                else:
                    failed.append(item_id)
            if not failed:
                emit({"type": "done", "ok": True, "installed": installed, "ready": ready, "failed": []})
                return EXIT_OK
            if time.monotonic() >= deadline:
                emit({"type": "done", "ok": False, "installed": installed, "ready": ready, "failed": failed})
                return EXIT_TIMEOUT
            time.sleep(1.0)
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def command_unsubscribe(args) -> int:
    steam = None
    tmp = None
    try:
        item_ids = dedupe_sorted_item_ids(args.item_ids)
        timeout = max(0.0, float(args.timeout))
        steam, _paths, tmp = init_steam(args.appid)
        last_seen: dict[int, tuple] = {}

        emit_snapshots(steam, item_ids, last_seen)
        for item_id in item_ids:
            snap = steam.snapshot(item_id)
            if snap.subscribed:
                steam.unsubscribe(item_id)
        deadline = time.monotonic() + timeout

        while True:
            steam.run_callbacks()
            emit_snapshots(steam, item_ids, last_seen)
            unsubscribed = []
            failed = []
            for item_id in item_ids:
                snap = steam.snapshot(item_id)
                if not snap.subscribed:
                    unsubscribed.append(item_id)
                else:
                    failed.append(item_id)
            if not failed:
                emit({"type": "done", "ok": True, "unsubscribed": unsubscribed, "failed": []})
                return EXIT_OK
            if time.monotonic() >= deadline:
                emit({"type": "done", "ok": False, "unsubscribed": unsubscribed, "failed": failed})
                return EXIT_TIMEOUT
            time.sleep(1.0)
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def command_unsubscribe_request(args) -> int:
    steam = None
    tmp = None
    try:
        item_ids = dedupe_sorted_item_ids(args.item_ids)
        steam, _paths, tmp = init_steam(args.appid)
        last_seen: dict[int, tuple] = {}

        emit_snapshots(steam, item_ids, last_seen)
        requested = []
        already_unsubscribed = []
        for item_id in item_ids:
            snap = steam.snapshot(item_id)
            if snap.subscribed:
                call_result = steam.unsubscribe(item_id)
                requested.append(item_id)
                emit({"type": "request", "id": item_id, "unsubscribe_call_result": call_result})
            else:
                already_unsubscribed.append(item_id)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            steam.run_callbacks()
            emit_snapshots(steam, item_ids, last_seen)
            time.sleep(0.1)

        emit(
            {
                "type": "done",
                "ok": True,
                "requested": requested,
                "already_unsubscribed": already_unsubscribed,
                "failed": [],
            }
        )
        return EXIT_OK
    finally:
        if steam is not None:
            steam.shutdown()
        if tmp is not None:
            tmp.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = HelperArgumentParser(
        prog="python -m dzll_launcher.steam_ugc_helper",
        description="Steamworks UGC helper for the Steam Client backend.",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=HelperArgumentParser)

    state = sub.add_parser("state", help="print current Workshop item state")
    state.add_argument("--appid", type=int, default=DAYZ_APPID)
    state.add_argument("item_ids", nargs="+", type=parse_item_id)
    state.set_defaults(func=command_state)

    readiness = sub.add_parser(
        "readiness", help="check native Steam login and UGC readiness",
    )
    readiness.add_argument("--appid", type=int, default=DAYZ_APPID)
    readiness.set_defaults(func=command_readiness)

    subscribe = sub.add_parser("subscribe", help="subscribe and wait until installed")
    subscribe.add_argument("--appid", type=int, default=DAYZ_APPID)
    subscribe.add_argument("--timeout", type=float, default=3600.0)
    subscribe.add_argument("item_ids", nargs="+", type=parse_item_id)
    subscribe.set_defaults(func=command_subscribe)

    subscribe_download = sub.add_parser("subscribe-download", help="subscribe, request high-priority download, and wait until installed")
    subscribe_download.add_argument("--appid", type=int, default=DAYZ_APPID)
    subscribe_download.add_argument("--timeout", type=float, default=3600.0)
    subscribe_download.add_argument("item_ids", nargs="+", type=parse_item_id)
    subscribe_download.set_defaults(func=command_subscribe_download)

    unsubscribe = sub.add_parser("unsubscribe", help="unsubscribe and wait until no longer subscribed")
    unsubscribe.add_argument("--appid", type=int, default=DAYZ_APPID)
    unsubscribe.add_argument("--timeout", type=float, default=120.0)
    unsubscribe.add_argument("item_ids", nargs="+", type=parse_item_id)
    unsubscribe.set_defaults(func=command_unsubscribe)

    unsubscribe_request = sub.add_parser("unsubscribe-request", help="send unsubscribe request without waiting for final state")
    unsubscribe_request.add_argument("--appid", type=int, default=DAYZ_APPID)
    unsubscribe_request.add_argument("item_ids", nargs="+", type=parse_item_id)
    unsubscribe_request.set_defaults(func=command_unsubscribe_request)

    session = sub.add_parser("session", help="serve cooperative UGC requests over JSON lines")
    session.add_argument("--appid", type=int, default=DAYZ_APPID)
    session.set_defaults(func=command_session)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if int(args.appid) <= 0:
            raise CliError("appid must be a positive integer")
        return int(args.func(args))
    except CliError as e:
        emit_error("bad_arguments", str(e))
        return e.exit_code
    except SteamInitError as e:
        emit_error("steam_init_failed", str(e))
        return e.exit_code
    except MissingSymbolsError as e:
        emit_error("missing_symbols", str(e))
        return e.exit_code
    except Exception as e:
        emit_error("unexpected_exception", str(e))
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
