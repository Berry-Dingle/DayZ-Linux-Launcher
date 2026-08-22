#!/usr/bin/env python3
"""Small helpers for resolving native Steam paths/process state."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


DAYZ_APPID = 221100
_VDF_TOKEN_RE = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


class SteamClientState(str, Enum):
    NATIVE = "native"
    FLATPAK = "flatpak"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SteamRuntimeEvidence:
    state: SteamClientState
    native_client_running: bool = False
    native_helper_running: bool = False
    flatpak_process_running: bool = False
    flatpak_client_running: bool = False


_FLATPAK_STEAM_APP_ID = "com.valvesoftware.steam"
_FLATPAK_STEAM_PROCESS_NAMES = {
    "steam",
    "steamwebhelper",
    "bwrap",
    "pv-bwrap",
    "pressure-vessel-wrap",
    "reaper",
}


def _vdf_unescape(value: str) -> str:
    """Decode quoted VDF escapes without reinterpreting filesystem text."""

    raw = "" if value is None else str(value)
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\" or index + 1 >= len(raw):
            out.append(char)
            index += 1
            continue

        escaped = raw[index + 1]
        if escaped in {'"', "\\"}:
            out.append(escaped)
        else:
            # KeyValues files used here only need quoted backslash and quote
            # handling. Preserve all other sequences literally; in particular,
            # filesystem paths must not acquire Python-style control characters.
            out.extend(("\\", escaped))
        index += 2
    return "".join(out)


def _tokenize_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _VDF_TOKEN_RE.finditer(text or ""):
        if match.group(2):
            tokens.append(match.group(2))
        else:
            tokens.append(_vdf_unescape(match.group(1) or ""))
    return tokens


def _parse_vdf_object(tokens: list[str], index: int = 0) -> tuple[dict, int]:
    out: dict = {}
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            return out, index + 1
        if token == "{":
            index += 1
            continue

        key = token
        index += 1
        if index >= len(tokens):
            break
        value = tokens[index]
        if value == "{":
            child, index = _parse_vdf_object(tokens, index + 1)
            out[key] = child
        elif value == "}":
            out[key] = ""
            return out, index + 1
        else:
            out[key] = value
            index += 1
    return out, index


def _parse_vdf(text: str) -> dict:
    tokens = _tokenize_vdf(text)
    parsed, _index = _parse_vdf_object(tokens, 0)
    return parsed


def _normalize_native_library_path(path) -> Path | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        candidate = Path(raw).expanduser()
        resolved = candidate.resolve()
    except Exception:
        return None
    low = str(resolved).lower()
    if "flatpak" in low or "com.valvesoftware.steam" in low:
        return None
    return resolved


def _libraryfolders_sort_key(item) -> tuple[int, object]:
    key = str(item[0])
    try:
        return (0, int(key))
    except Exception:
        return (1, key)


def resolve_native_steam_cmd() -> str | None:
    def valid_native_steam_cmd(raw_path) -> str | None:
        raw = str(raw_path or "").strip()
        if not raw:
            return None
        try:
            candidate = Path(raw).expanduser()
            resolved = candidate.resolve()
        except Exception:
            return None
        low_path = str(resolved).lower()
        if resolved.name.lower() == "flatpak" or "flatpak" in low_path or "com.valvesoftware.steam" in low_path:
            return None
        try:
            if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
                return None
        except Exception:
            return None
        try:
            snippet = resolved.read_bytes()[:8192].decode("utf-8", "ignore").lower()
        except Exception:
            snippet = ""
        forbidden_launcher_markers = (
            "flatpak",
            "com.valvesoftware.steam",
            "xdg-open",
            "gtk-launch",
            "steam://",
        )
        if any(marker in snippet for marker in forbidden_launcher_markers):
            return None
        return str(resolved)

    seen: set[str] = set()
    # Never use PATH here: a user-level `steam` command can be a Flatpak,
    # desktop-handler, or URI wrapper.  Only supported system-native entries
    # are eligible, and each is resolved and inspected above.
    candidates = [Path("/usr/bin/steam"), Path("/usr/games/steam")]
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        steam = valid_native_steam_cmd(raw)
        if steam:
            return steam
    return None


def native_steam_launch_environment(source=None) -> dict[str, str]:
    """Return an environment that cannot inherit Flatpak Steam routing."""
    env = dict(os.environ if source is None else source)
    for name in tuple(env):
        upper = str(name).upper()
        value = str(env.get(name, "") or "").casefold()
        if upper == "PATH":
            safe_parts = [
                part for part in str(env.get(name, "") or "").split(":")
                if part
                and not part.casefold().startswith("/app/")
                and "com.valvesoftware.steam" not in part.casefold()
                and "/.var/app/" not in part.casefold()
            ]
            env[name] = ":".join(safe_parts)
            continue
        if upper.startswith("FLATPAK_"):
            env.pop(name, None)
            continue
        if (
            "com.valvesoftware.steam" in value
            or "/.var/app/com.valvesoftware.steam/" in value
            or value.startswith("/app/")
        ):
            env.pop(name, None)
    return env


@dataclass(frozen=True)
class NativeSteamLaunchResult:
    ok: bool
    error: str = ""
    executable: str = ""
    pid: int = 0


def launch_native_steam_silent_tracked() -> NativeSteamLaunchResult:
    steam_cmd = resolve_native_steam_cmd()
    if not steam_cmd or not os.path.isabs(str(steam_cmd)):
        return NativeSteamLaunchResult(
            False, "Native Steam executable was not found.",
        )
    try:
        proc = subprocess.Popen(
            [steam_cmd, "-silent"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=native_steam_launch_environment(),
        )
    except Exception as exc:
        return NativeSteamLaunchResult(False, str(exc), executable=steam_cmd)
    return NativeSteamLaunchResult(
        True,
        "",
        executable=steam_cmd,
        pid=int(getattr(proc, "pid", 0) or 0),
    )


def launch_native_steam_silent() -> tuple[bool, str]:
    result = launch_native_steam_silent_tracked()
    return result.ok, result.error

def resolve_native_steam_root() -> Path | None:
    for path in (
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
    ):
        try:
            if path.exists():
                return path
        except Exception:
            continue
    return None


def resolve_native_workshop_path() -> Path | None:
    root = resolve_native_steam_root()
    if root is None:
        return None
    workshop = root / "steamapps/workshop"
    try:
        if workshop.exists():
            return workshop
    except Exception:
        pass
    return None


def parse_steam_libraryfolders_vdf(path: Path) -> list[Path]:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    try:
        data = _parse_vdf(text)
    except Exception:
        data = {}
    root = data.get("libraryfolders") if isinstance(data, dict) else {}
    if not isinstance(root, dict):
        root = data if isinstance(data, dict) else {}

    out: list[Path] = []
    seen = set()

    def add(raw_path) -> None:
        lib = _normalize_native_library_path(raw_path)
        if lib is None:
            return
        key = str(lib)
        if key in seen:
            return
        out.append(lib)
        seen.add(key)

    for _key, value in sorted(root.items(), key=_libraryfolders_sort_key):
        if isinstance(value, dict):
            add(value.get("path"))
        else:
            add(value)
    return out


def native_steam_libraries() -> list[Path]:
    root = resolve_native_steam_root()
    candidates: list[Path] = []
    if root is not None:
        normalized_root = _normalize_native_library_path(root)
        if normalized_root is not None:
            candidates.append(normalized_root)
            candidates.extend(parse_steam_libraryfolders_vdf(normalized_root / "steamapps/libraryfolders.vdf"))

    out: list[Path] = []
    seen = set()
    for path in candidates:
        lib = _normalize_native_library_path(path)
        if lib is None:
            continue
        try:
            if not (lib / "steamapps").is_dir():
                continue
        except Exception:
            continue
        key = str(lib)
        if key not in seen:
            out.append(lib)
            seen.add(key)
    return out


def find_steam_app_library(appid: int) -> Path | None:
    try:
        appid_i = int(appid)
    except Exception:
        return None
    if appid_i <= 0:
        return None
    manifest_name = f"appmanifest_{appid_i}.acf"
    for library in native_steam_libraries():
        try:
            if (library / "steamapps" / manifest_name).is_file():
                return library
        except Exception:
            continue
    return None


def dayz_steam_library() -> Path | None:
    return find_steam_app_library(DAYZ_APPID)


def _steam_app_installdir(library: Path, appid: int) -> str:
    try:
        text = (Path(library) / "steamapps" / f"appmanifest_{int(appid)}.acf").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return ""
    try:
        data = _parse_vdf(text)
    except Exception:
        data = {}
    app_state = data.get("AppState") if isinstance(data, dict) else {}
    if isinstance(app_state, dict):
        value = str(app_state.get("installdir") or "").strip()
        if value:
            return value
    match = re.search(
        r'"installdir"\s*"((?:\\.|[^"\\])*)"',
        text,
        re.IGNORECASE,
    )
    return _vdf_unescape(match.group(1)).strip() if match else ""


def dayz_install_dir() -> Path | None:
    library = dayz_steam_library()
    if library is None:
        return None
    installdir = _steam_app_installdir(library, DAYZ_APPID) or "DayZ"
    return library / "steamapps/common" / installdir


def dayz_workshop_content_dir() -> Path | None:
    library = dayz_steam_library()
    if library is None:
        return None
    return library / "steamapps/workshop/content" / str(DAYZ_APPID)


def dayz_workshop_downloads_dir() -> Path | None:
    library = dayz_steam_library()
    if library is None:
        return None
    return library / "steamapps/workshop/downloads" / str(DAYZ_APPID)


def dayz_compatdata_dir() -> Path | None:
    library = dayz_steam_library()
    if library is None:
        return None
    return library / "steamapps/compatdata" / str(DAYZ_APPID)


def dayz_paths_summary() -> dict:
    cmd = resolve_native_steam_cmd()
    root = resolve_native_steam_root()
    libraries = native_steam_libraries()
    dayz_library = dayz_steam_library()
    install_dir = dayz_install_dir()
    workshop_content = dayz_workshop_content_dir()
    workshop_downloads = dayz_workshop_downloads_dir()
    compatdata = dayz_compatdata_dir()
    client_state = detect_steam_client_state()
    return {
        "steam_cmd": cmd,
        "steam_root": str(root) if root is not None else None,
        "libraries": [str(path) for path in libraries],
        "dayz_library": str(dayz_library) if dayz_library is not None else None,
        "dayz_install_dir": str(install_dir) if install_dir is not None else None,
        "workshop_content": str(workshop_content) if workshop_content is not None else None,
        "workshop_downloads": str(workshop_downloads) if workshop_downloads is not None else None,
        "compatdata": str(compatdata) if compatdata is not None else None,
        "steam_client_state": client_state.value,
        "flatpak_running": client_state is SteamClientState.FLATPAK,
    }


def _flatpak_steam_process_family(cmdline: str, resolved_executable: str) -> bool:
    parts = [part for part in str(cmdline or "").split(" ") if part]
    command_name = Path(parts[0]).name.lower() if parts else ""
    resolved_name = Path(str(resolved_executable or "")).name.lower()
    names = {command_name, resolved_name}
    if names & _FLATPAK_STEAM_PROCESS_NAMES:
        return True
    return any(
        name.startswith(("steam-runtime", "pressure-vessel", "srt-"))
        for name in names
        if name
    )


def _process_has_flatpak_steam_identity(entry: Path, cmdline: str) -> bool:
    low = str(cmdline or "").lower()
    parts = [part for part in str(cmdline or "").split(" ") if part]
    executable_path = str(parts[0] if parts else "").lower()
    executable = Path(parts[0]).name.lower() if parts else ""
    if (
        executable == "flatpak"
        and "run" in {part.lower() for part in parts[1:]}
        and _FLATPAK_STEAM_APP_ID in low
    ):
        return True
    if executable == "steam" and (
        executable_path.startswith("/app/")
        or "/.var/app/com.valvesoftware.steam/" in executable_path
        or "/flatpak/" in executable_path
    ):
        return True
    resolved_executable = ""
    try:
        resolved_executable = os.readlink(entry / "exe").lower()
    except Exception:
        pass
    if not _flatpak_steam_process_family(low, resolved_executable):
        return False
    if (
        _FLATPAK_STEAM_APP_ID in low
        or _FLATPAK_STEAM_APP_ID in resolved_executable
    ):
        return True
    try:
        environ = (entry / "environ").read_bytes().lower()
        if b"flatpak_id=com.valvesoftware.steam" in environ:
            return True
    except Exception:
        pass
    try:
        info = (entry / "root/.flatpak-info").read_text(
            encoding="utf-8", errors="replace",
        ).lower()
        if _FLATPAK_STEAM_APP_ID in info:
            return True
    except Exception:
        pass
    try:
        cgroup = (entry / "cgroup").read_text(
            encoding="utf-8", errors="replace",
        ).lower()
        if "app-flatpak-com.valvesoftware.steam" in cgroup:
            return True
    except Exception:
        pass
    return False


def _is_native_steam_client_cmdline(text: str) -> bool:
    low = str(text or "").lower()
    if not low or "com.valvesoftware.steam" in low or "flatpak" in low:
        return False
    if "steamcmd" in low:
        return False
    parts = [part for part in str(text).split(" ") if part]
    if not parts:
        return False
    return Path(parts[0]).name.lower() == "steam"


def _is_native_steam_process_cmdline(text: str) -> bool:
    low = str(text or "").lower()
    if not low or "com.valvesoftware.steam" in low or "flatpak" in low:
        return False
    if "steamcmd" in low:
        return False
    parts = [part for part in str(text).split(" ") if part]
    if not parts:
        return False
    exe_name = Path(parts[0]).name.lower()
    return (
        exe_name in {"steam", "steamwebhelper"}
        or exe_name.startswith("steam-runtime")
    )


def resolve_steam_runtime_state(proc_root: Path | None = None) -> SteamRuntimeEvidence:
    """Resolve runtime truth, failing closed when both client families remain."""
    root = Path("/proc") if proc_root is None else proc_root
    native_client_seen = False
    native_helper_seen = False
    flatpak_seen = False
    flatpak_client_seen = False
    try:
        entries = list(root.iterdir())
    except Exception:
        return SteamRuntimeEvidence(SteamClientState.UNKNOWN)

    for entry in entries:
        if not str(getattr(entry, "name", "")).isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except Exception:
            continue
        if not raw:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        # Do not open environ/root namespace files for every process on the
        # machine.  Steam clients, helpers, runtimes, and Flatpak wrappers all
        # carry one of these cheap command-line markers.
        low = text.lower()
        if not any(
            marker in low
            for marker in (
                "steam", "flatpak", "/app/", "bwrap", "pressure-vessel",
                "pv-bwrap", "reaper", "srt-",
            )
        ):
            continue
        if _process_has_flatpak_steam_identity(entry, text):
            # A PID can disappear and be reused while /proc is being scanned.
            # Only accept corroborating evidence if the command line still
            # belongs to the process whose identity was inspected.
            try:
                if (entry / "cmdline").read_bytes() != raw:
                    continue
            except Exception:
                continue
            flatpak_seen = True
            parts = [part for part in text.split(" ") if part]
            executable_name = Path(parts[0]).name.casefold() if parts else ""
            if executable_name == "steam" or (
                executable_name == "flatpak"
                and "run" in {part.casefold() for part in parts[1:]}
                and _FLATPAK_STEAM_APP_ID in low
            ):
                flatpak_client_seen = True
        elif _is_native_steam_client_cmdline(text):
            native_client_seen = True
        elif _is_native_steam_process_cmdline(text):
            native_helper_seen = True

    # SteamAPI does not expose a client-process identity that DZLL can match to
    # a specific native/Flatpak PID.  With both families alive, process truth is
    # therefore insufficient to prove which IPC endpoint a new helper selects.
    if native_client_seen and flatpak_seen:
        state = SteamClientState.UNKNOWN
    elif native_client_seen:
        state = SteamClientState.NATIVE
    elif flatpak_seen:
        state = SteamClientState.FLATPAK
    elif native_helper_seen:
        state = SteamClientState.UNKNOWN
    else:
        state = SteamClientState.OFFLINE
    return SteamRuntimeEvidence(
        state=state,
        native_client_running=native_client_seen,
        native_helper_running=native_helper_seen,
        flatpak_process_running=flatpak_seen,
        flatpak_client_running=flatpak_client_seen,
    )


def detect_steam_client_state(proc_root: Path | None = None) -> SteamClientState:
    return resolve_steam_runtime_state(proc_root).state


def native_steam_ready_for_mod_manager(
    runtime: SteamRuntimeEvidence | None = None,
) -> bool:
    """Return the native runtime condition used to enter Mod Manager."""
    evidence = resolve_steam_runtime_state() if runtime is None else runtime
    return bool(
        evidence.state is SteamClientState.NATIVE
        and evidence.native_client_running
        and not evidence.flatpak_process_running
    )


def is_flatpak_steam_running() -> bool:
    return detect_steam_client_state() is SteamClientState.FLATPAK


def is_native_steam_client_running() -> bool:
    """Return True only for the native Steam client, not surviving helpers."""
    return resolve_steam_runtime_state().native_client_running


def is_native_steam_running() -> bool:
    """
    Return True when native Steam client processes appear to be running.

    Flatpak Steam and steamcmd are intentionally ignored; callers use this to
    wait for /usr/bin/steam -shutdown before touching native Workshop state.
    """
    return detect_steam_client_state() is SteamClientState.NATIVE


def native_steam_summary() -> dict:
    cmd = resolve_native_steam_cmd()
    root = resolve_native_steam_root()
    workshop = resolve_native_workshop_path()
    runtime = resolve_steam_runtime_state()
    client_state = runtime.state
    return {
        "command": cmd,
        "root": str(root) if root is not None else None,
        "workshop": str(workshop) if workshop is not None else None,
        "client_state": client_state.value,
        "native_running": client_state is SteamClientState.NATIVE,
        "flatpak_running": client_state is SteamClientState.FLATPAK,
        "native_client_running": runtime.native_client_running,
        "native_helper_running": runtime.native_helper_running,
        "flatpak_process_running": runtime.flatpak_process_running,
    }
