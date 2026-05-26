# settings.py
import os
import json
from typing import Any, Dict

from .config import CFG_DIR

SETTINGS_PATH = os.path.join(CFG_DIR, "settings.json")

DEFAULTS: Dict[str, Any] = {
    # General
    "show_server_companion": False,
    "ingame_name": "",
    "high_ping_cutoff_ms": 250,
    "hide_below_max_players": 0,
    "hide_test_servers": True,
    "enable_blocklist_filter": True,

    # Title bar counts
    "show_counts_in_title_bar": False,
    "show_counts_servers_loaded": True,
    "show_counts_global_players": True,

    # Updates
    "auto_check_updates": True,
    "last_update_check_ts": 0,
    "latest_release_tag": "",
    "latest_release_url": "",
    "update_remind_after_ts": 0,

    # Launch
    "steam_install_type": "auto",  # auto | native | flatpak
    "skip_dayz_launcher": True,
    "no_splash": True,
    "windowed_mode": False,
    "additional_launch_params": "",
    "warn_on_blocked_join": True,
    "minimize_dayz_launcher": False,
    "force_fullscreen": True,

    # Mods
    "enable_steamcmd_mod_handling": True,
    "steamcmd_username": "",
    "steamcmd_path": "",
    "steamcmd_path_user_set": False,
    "auto_install_update_mods": True,
    "auto_install_missing_mods": True,
    "auto_update_required_mods": False,
    "workshop_dir": "",
    "workshop_dir_user_set": False,
    "additional_mod_ids": "",
    "verify_mod_files": False,

    # Discord
    "discord_rich_presence": True,
    "discord_privacy_mode": False,
    "discord_detail_level": "ingame",  # menus | ingame | server

    # Discord invite (used by Settings > About)
    "discord_invite_url": "https://discord.gg/CNJ9xDTgAM",

    # Website (used by Settings > About)
    "website_url": "https://dzllauncher.uk",
}

def _read_json(path: str) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {}

def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)

def load_settings() -> Dict[str, Any]:
    raw = _read_json(SETTINGS_PATH)
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS:
            out[k] = v
    if (
        "auto_install_update_mods" in raw
        and "auto_install_missing_mods" not in raw
        and "auto_update_required_mods" not in raw
    ):
        legacy = bool(raw["auto_install_update_mods"])
        out["auto_install_missing_mods"] = legacy
        out["auto_update_required_mods"] = legacy
    if bool(out.get("skip_dayz_launcher", True)) and bool(out.get("minimize_dayz_launcher", False)):
        out["minimize_dayz_launcher"] = False
    return out

def save_settings(settings: Dict[str, Any]) -> None:
    clean = dict(DEFAULTS)
    for k, v in (settings or {}).items():
        if k in DEFAULTS:
            clean[k] = v
    _write_json(SETTINGS_PATH, clean)

def reset_settings() -> Dict[str, Any]:
    s = dict(DEFAULTS)
    save_settings(s)
    return s

def autodetect_steamcmd_path() -> str:
    """
    Attempt to locate a valid SteamCMD executable.

    Order matters:
    - Prefer user-level wrappers (~/bin)
    - Then common manual installs (~/SteamCMD, ~/steamcmd)
    - Then local-share installs
    - Then system package installs
    """

    candidates = [
        # --- User preferred / wrappers ---
        os.path.expanduser("~/bin/steamcmd"),

        # --- Common manual installs ---
        os.path.expanduser("~/SteamCMD/steamcmd.sh"),
        os.path.expanduser("~/SteamCMD/linux32/steamcmd"),
        os.path.expanduser("~/steamcmd/steamcmd.sh"),
        os.path.expanduser("~/steamcmd/linux32/steamcmd"),

        # --- Local share installs ---
        os.path.expanduser("~/.local/share/steamcmd/steamcmd.sh"),
        os.path.expanduser("~/.local/share/steamcmd/linux32/steamcmd"),

        # --- System installs ---
        "/usr/bin/steamcmd",
        "/usr/games/steamcmd",
    ]

    for p in candidates:
        try:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        except Exception:
            continue

    return ""

def autodetect_workshop_dir() -> str:
    candidates = [
        os.path.expanduser("~/.local/share/Steam/steamapps/workshop"),
        os.path.expanduser("~/.steam/steam/steamapps/workshop"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/data/Steam/steamapps/workshop"),
    ]
    for p in candidates:
        try:
            if os.path.exists(p) and os.path.isdir(p):
                return p
        except Exception:
            continue
    return ""
