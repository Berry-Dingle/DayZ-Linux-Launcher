#!/usr/bin/env python3
# launcher_state.py
#
# DayZ Launcher state bootstrap/writer for DZLL (Proton prefix).
# Creates/updates:
#   - Local.json
#   - Presets/dayz.defaultpreset2
#
# Uses launcher-compatible schemas based on observed files.

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET


DAYZ_COMPAT_APPID = "221100"


def _norm_path(p: str) -> str:
    return os.path.abspath(os.path.expanduser((p or "").strip()))


def _default_proton_prefix() -> str:
    return os.path.expanduser(
        f"~/.local/share/Steam/steamapps/compatdata/{DAYZ_COMPAT_APPID}/pfx"
    )


def get_launcher_local_dir(proton_prefix: str = "") -> str:
    pfx = _norm_path(proton_prefix) if proton_prefix else _default_proton_prefix()
    return os.path.join(
        pfx,
        "drive_c",
        "users",
        "steamuser",
        "AppData",
        "Local",
        "DayZ Launcher",
    )


def get_launcher_presets_dir(proton_prefix: str = "") -> str:
    return os.path.join(get_launcher_local_dir(proton_prefix), "Presets")


def ensure_launcher_dirs(proton_prefix: str = "") -> Dict[str, str]:
    local_dir = get_launcher_local_dir(proton_prefix)
    presets_dir = os.path.join(local_dir, "Presets")
    os.makedirs(local_dir, exist_ok=True)
    os.makedirs(presets_dir, exist_ok=True)
    return {
        "local_dir": local_dir,
        "presets_dir": presets_dir,
        "local_json_path": os.path.join(local_dir, "Local.json"),
        "default_preset_path": os.path.join(presets_dir, "dayz.defaultpreset2"),
    }


def _json_read(path: str) -> Optional[dict]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return None


def _json_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def _ms_date_created() -> str:
    # Matches observed launcher style e.g. "/Date(1771890106705)/"
    ms = int(time.time() * 1000)
    return f"/Date({ms})/"


def _iso_now() -> str:
    # ISO with timezone offset; launcher accepted observed format with fractional seconds
    return datetime.now(timezone.utc).isoformat()


def linux_to_win_path_under_prefix(
    linux_path: str,
    proton_prefix: str = "",
    drive_c_root: str = "",
) -> Optional[str]:
    """
    Convert a Linux path under Proton pfx/drive_c to a Windows path.
    Example:
      .../pfx/drive_c/users/steamuser/DZLLMods/@CF
      -> C:\\users\\steamuser\\DZLLMods\\@CF
    """
    lp = _norm_path(linux_path)

    if drive_c_root:
        dc = _norm_path(drive_c_root)
    else:
        pfx = _norm_path(proton_prefix) if proton_prefix else _default_proton_prefix()
        dc = os.path.join(pfx, "drive_c")

    try:
        rel = os.path.relpath(lp, dc)
    except Exception:
        return None

    if rel.startswith(".."):
        return None

    rel = rel.replace("/", "\\")
    return f"C:\\{rel}"


def _dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def build_local_json(
    *,
    watch_folder_win: str,
    known_local_mods_win: List[str],
    existing: Optional[dict] = None,
) -> dict:
    """
    Local.json should represent launcher local inventory / directories.
    Preserves unknown keys where possible.
    """
    base = dict(existing) if isinstance(existing, dict) else {}

    # Preserve existing dateCreated if present; create if missing/invalid
    date_created = base.get("dateCreated")
    if not isinstance(date_created, str) or not date_created:
        date_created = _ms_date_created()

    autodirs = base.get("autodetectionDirectories")
    if not isinstance(autodirs, list):
        autodirs = []
    autodirs = [str(x) for x in autodirs if isinstance(x, (str, int, float))]
    autodirs.append(str(watch_folder_win))
    autodirs = _dedupe_keep_order([str(x) for x in autodirs])

    known = [str(x) for x in known_local_mods_win if x]
    known = _dedupe_keep_order(known)

    user_dirs = base.get("userDirectories")
    if not isinstance(user_dirs, list):
        user_dirs = []

    # Update canonical keys
    base["autodetectionDirectories"] = autodirs
    base["dateCreated"] = date_created
    base["knownLocalMods"] = known
    base["userDirectories"] = user_dirs

    return base


def write_local_json(
    *,
    local_json_path: str,
    watch_folder_win: str,
    known_local_mods_win: List[str],
) -> None:
    existing = _json_read(local_json_path)
    payload = build_local_json(
        watch_folder_win=watch_folder_win,
        known_local_mods_win=known_local_mods_win,
        existing=existing,
    )
    _json_write(local_json_path, payload)


def _preset_local_id_from_win_mod_path(win_mod_path: str) -> str:
    """
    Local preset IDs use format:
      local:C:\\USERS\\STEAMUSER\\DZLLMODS\\@CF\\
    """
    p = (win_mod_path or "").replace("/", "\\").strip()
    p = p.rstrip("\\")
    # Normalize for stability (launcher seems tolerant)
    p_up = p.upper()
    return f"local:{p_up}\\"


def write_default_preset(
    *,
    preset_path: str,
    selected_mods_win_paths: List[str],
) -> None:
    """
    Writes dayz.defaultpreset2 using observed schema:
    <addons-presets><last-update>...</last-update><published-ids>...</published-ids></addons-presets>
    """
    root = ET.Element("addons-presets")

    last_update = ET.SubElement(root, "last-update")
    last_update.text = _iso_now()

    published = ET.SubElement(root, "published-ids")

    for wp in _dedupe_keep_order([str(x) for x in selected_mods_win_paths if x]):
        id_el = ET.SubElement(published, "id")
        id_el.text = _preset_local_id_from_win_mod_path(wp)

    # Pretty-ish indent (Py3.9+)
    try:
        ET.indent(root, space="  ")
    except Exception:
        pass

    tree = ET.ElementTree(root)
    os.makedirs(os.path.dirname(preset_path), exist_ok=True)
    tree.write(preset_path, encoding="utf-8", xml_declaration=True)


def bootstrap_launcher_state(
    *,
    proton_prefix: str,
    watch_folder_linux: str,
    installed_mod_linux_paths: List[str],
    selected_mod_linux_paths: List[str],
) -> Dict[str, str]:
    """
    Creates/updates launcher dirs + Local.json + dayz.defaultpreset2.
    Returns paths used.
    """
    paths = ensure_launcher_dirs(proton_prefix)

    watch_win = linux_to_win_path_under_prefix(watch_folder_linux, proton_prefix=proton_prefix)
    if not watch_win:
        raise RuntimeError(f"Watch folder is not under Proton drive_c: {watch_folder_linux}")

    installed_win = []
    for p in installed_mod_linux_paths:
        wp = linux_to_win_path_under_prefix(p, proton_prefix=proton_prefix)
        if wp:
            installed_win.append(wp)

    selected_win = []
    for p in selected_mod_linux_paths:
        wp = linux_to_win_path_under_prefix(p, proton_prefix=proton_prefix)
        if wp:
            selected_win.append(wp)

    write_local_json(
        local_json_path=paths["local_json_path"],
        watch_folder_win=watch_win,
        known_local_mods_win=installed_win,
    )

    write_default_preset(
        preset_path=paths["default_preset_path"],
        selected_mods_win_paths=selected_win,
    )

    return paths