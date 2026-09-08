# settings.py
import json
import os
from typing import Any, Dict

from .atomic_json import atomic_write_json
from .config import CFG_DIR

SETTINGS_PATH = os.path.join(CFG_DIR, "settings.json")

DEFAULTS: Dict[str, Any] = {
    # General
    "show_server_companion": False,
    "show_background_download_buttons": False,
    "ingame_name": "",
    "high_ping_cutoff_ms": 250,
    "hide_below_max_players": 0,
    "hide_test_servers": True,
    "prioritise_trusted_servers": False,
    "pin_favorite_servers": False,
    # Title bar counts
    "show_counts_in_title_bar": False,
    "show_counts_servers_loaded": True,
    "show_counts_global_players": True,

    # Server Companion
    "server_companion_restart_alert_enabled": False,
    "server_companion_alert_sound": "female",
    "server_companion_alert_volume": 30,

    # Updates
    "auto_check_updates": True,
    "last_update_check_ts": 0,
    "latest_release_tag": "",
    "latest_release_url": "",
    "update_remind_after_ts": 0,
    "skipped_release_tag": "",

    # Launch
    "steam_install_type": "auto",  # legacy: auto | native
    "skip_dayz_launcher": True,
    "start_steam_on_join": False,
    "no_splash": True,
    "windowed_mode": False,
    "additional_launch_params": "",
    "warn_on_blocked_join": True,
    "minimize_dayz_launcher": False,
    "force_fullscreen": True,

    # Mods
    "enable_steamcmd_mod_handling": True,
    "auto_install_update_mods": True,
    "auto_install_missing_mods": True,
    "workshop_dir": "",
    "workshop_dir_user_set": False,
    "additional_mod_ids": "",
    "restart_steam_after_local_cleanup": False,

    # Discord
    "discord_rich_presence": True,
    "discord_privacy_mode": False,
    "discord_detail_level": "ingame",  # menus | ingame | server

    # Discord invite (used by Settings > About)
    "discord_invite_url": "https://discord.gg/CNJ9xDTgAM",

    # Website (used by Settings > About)
    "website_url": "https://dzllauncher.uk",
}

_INT_RANGES = {
    "server_companion_alert_volume": (0, 100),
}


def _validated_persisted_value(key: str, value: Any) -> Any:
    """Return a known setting value, or its default when its JSON type is invalid."""

    default = DEFAULTS[key]
    if type(default) is bool:
        return value if type(value) is bool else default
    if type(default) is int:
        if type(value) is not int:
            return default
        bounds = _INT_RANGES.get(key)
        if bounds is not None and not bounds[0] <= value <= bounds[1]:
            return default
        return value
    if type(default) is str:
        return value if type(value) is str else default
    return value if isinstance(value, type(default)) else default

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
    atomic_write_json(path, data, indent=2, sort_keys=True)

def load_settings() -> Dict[str, Any]:
    raw = _read_json(SETTINGS_PATH)
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS:
            out[k] = _validated_persisted_value(k, v)
    if (
        "auto_install_update_mods" in raw
        and "auto_install_missing_mods" not in raw
    ):
        legacy = out["auto_install_update_mods"]
        out["auto_install_missing_mods"] = legacy
    if out.get("steam_install_type") not in ("auto", "native"):
        out["steam_install_type"] = "auto"
    if bool(out.get("skip_dayz_launcher", True)) and bool(out.get("minimize_dayz_launcher", False)):
        out["minimize_dayz_launcher"] = False
    return out

def save_settings(settings: Dict[str, Any]) -> None:
    clean = dict(DEFAULTS)
    for k, v in (settings or {}).items():
        if k in DEFAULTS:
            clean[k] = v
    if clean.get("steam_install_type") not in ("auto", "native"):
        clean["steam_install_type"] = "auto"
    _write_json(SETTINGS_PATH, clean)

def reset_settings() -> Dict[str, Any]:
    s = dict(DEFAULTS)
    save_settings(s)
    return s

def autodetect_workshop_dir() -> str:
    candidates = [
        os.path.expanduser("~/.local/share/Steam/steamapps/workshop"),
        os.path.expanduser("~/.steam/steam/steamapps/workshop"),
    ]
    for p in candidates:
        try:
            if os.path.exists(p) and os.path.isdir(p):
                return p
        except Exception:
            continue
    return ""
