#!/usr/bin/env python3
"""Small helpers for resolving native Steam paths/process state."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


DAYZ_APPID = 221100
_VDF_TOKEN_RE = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def _vdf_unescape(value: str) -> str:
    try:
        return bytes(str(value), "utf-8").decode("unicode_escape")
    except Exception:
        return str(value or "")


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
        if "flatpak run" in snippet and "com.valvesoftware.steam" in snippet:
            return None
        return str(resolved)

    seen: set[str] = set()
    candidates = [Path("/usr/bin/steam"), shutil.which("steam"), Path("/usr/games/steam")]
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        steam = valid_native_steam_cmd(raw)
        if steam:
            return steam
    return None

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
    match = re.search(r'"installdir"\s*"([^"]+)"', text, re.IGNORECASE)
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
    return {
        "steam_cmd": cmd,
        "steam_root": str(root) if root is not None else None,
        "libraries": [str(path) for path in libraries],
        "dayz_library": str(dayz_library) if dayz_library is not None else None,
        "dayz_install_dir": str(install_dir) if install_dir is not None else None,
        "workshop_content": str(workshop_content) if workshop_content is not None else None,
        "workshop_downloads": str(workshop_downloads) if workshop_downloads is not None else None,
        "compatdata": str(compatdata) if compatdata is not None else None,
        "flatpak_running": is_flatpak_steam_running(),
    }


def is_flatpak_steam_running() -> bool:
    needles = ("com.valvesoftware.Steam", "flatpak")
    try:
        proc = subprocess.run(
            ["pgrep", "-af", "com.valvesoftware.Steam|flatpak.*steam|steam.*flatpak"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    except Exception:
        pass

    proc_root = Path("/proc")
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except Exception:
                continue
            text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
            low = text.lower()
            if "com.valvesoftware.steam" in low:
                return True
            if all(needle.lower() in low for needle in needles):
                return True
    except Exception:
        pass
    return False


def is_native_steam_running() -> bool:
    """
    Return True when native Steam client processes appear to be running.

    Flatpak Steam and steamcmd are intentionally ignored; callers use this to
    wait for /usr/bin/steam -shutdown before touching native Workshop state.
    """
    proc_root = Path("/proc")
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except Exception:
                continue
            if not raw:
                continue
            text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
            low = text.lower()
            if "com.valvesoftware.steam" in low or "flatpak" in low:
                continue
            if "steamcmd" in low:
                continue
            if "steamwebhelper" in low:
                return True
            parts = [part for part in text.split(" ") if part]
            if not parts:
                continue
            exe_name = Path(parts[0]).name.lower()
            if exe_name == "steam" or exe_name.startswith("steam-runtime"):
                return True
    except Exception:
        pass
    return False


def native_steam_summary() -> dict:
    cmd = resolve_native_steam_cmd()
    root = resolve_native_steam_root()
    workshop = resolve_native_workshop_path()
    return {
        "command": cmd,
        "root": str(root) if root is not None else None,
        "workshop": str(workshop) if workshop is not None else None,
        "native_running": is_native_steam_running(),
        "flatpak_running": is_flatpak_steam_running(),
    }
