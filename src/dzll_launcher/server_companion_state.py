"""Pure conversions between ServerObject rows and Server Companion dicts.

``persisted_from_obj`` produces the last-Companion-server record written by
``storage.save_last_companion_server``. Its keys and defaults are a
persisted shape and must stay stable.
"""

from __future__ import annotations

from typing import Callable, Mapping

from .server_endpoint import ServerEndpointValidationError, normalize_server_endpoint
from .storage import human_last_played
from .ui_row import ServerObject


def persisted_from_obj(obj) -> dict:
    """Return the persisted last-Companion-server record for ``obj``.

    Raises ``ServerEndpointValidationError`` for an invalid endpoint.
    """
    ip, gport, qport = normalize_server_endpoint(
        getattr(obj, "ip", None),
        getattr(obj, "gport", None),
        getattr(obj, "qport", None),
    )
    try:
        mod_count = int(getattr(obj, "mod_count", 0) or 0)
    except Exception:
        mod_count = 0
    try:
        players = int(getattr(obj, "players", 0) or 0)
    except Exception:
        players = 0
    try:
        max_players = int(getattr(obj, "max_players", 0) or 0)
    except Exception:
        max_players = 0
    try:
        ping = int(getattr(obj, "ping", -1) or -1)
    except Exception:
        ping = -1
    try:
        bm_rank = int(getattr(obj, "bm_rank", 999999999) or 999999999)
    except Exception:
        bm_rank = 999999999
    try:
        timewarp = float(getattr(obj, "timewarp", 1.0) or 1.0)
    except Exception:
        timewarp = 1.0

    return {
        "ip": ip,
        "gport": gport,
        "qport": qport,
        "name": str(getattr(obj, "name", "") or ""),
        "map_name": str(getattr(obj, "map_name", "") or ""),
        "map": str(getattr(obj, "map_name", "") or ""),
        "mods_json": str(getattr(obj, "mods_json", "") or ""),
        "mod_count": mod_count,
        "mods_preview": str(getattr(obj, "mods_preview", "") or ""),
        "password": bool(getattr(obj, "password", False)),
        "third_person": bool(getattr(obj, "third_person", False)),
        "country": str(getattr(obj, "country", "") or ""),
        "time": str(getattr(obj, "time", "") or ""),
        "timewarp": timewarp,
        "players": players,
        "max_players": max_players,
        "ping": ping,
        "bm_rank": bm_rank,
    }


def obj_from_persisted(
    data,
    *,
    key_for: Callable[[str, int], str],
    objects_by_key: Mapping,
    favorites: Mapping,
    last_played: Mapping,
) -> ServerObject | None:
    """Rebuild a ServerObject from a persisted record.

    Returns the live browser row when one exists for the endpoint, ``None``
    for invalid data, and otherwise a new detached row.
    """
    if not isinstance(data, dict):
        return None
    try:
        ip, gport, qport = normalize_server_endpoint(
            data.get("ip"),
            data.get("gport"),
            data.get("qport"),
            allow_missing_query_port=True,
        )
    except ServerEndpointValidationError:
        return None

    key = key_for(ip, gport)
    current = objects_by_key.get(key)
    if isinstance(current, ServerObject):
        return current

    try:
        mod_count = int(data.get("mod_count", 0) or 0)
    except Exception:
        mod_count = 0
    try:
        players = int(data.get("players", 0) or 0)
    except Exception:
        players = 0
    try:
        max_players = int(data.get("max_players", 0) or 0)
    except Exception:
        max_players = 0
    try:
        ping = int(data.get("ping", -1) or -1)
    except Exception:
        ping = -1
    try:
        bm_rank = int(data.get("bm_rank", 999999999) or 999999999)
    except Exception:
        bm_rank = 999999999
    try:
        timewarp = float(data.get("timewarp", 1.0) or 1.0)
    except Exception:
        timewarp = 1.0

    lp_ts = last_played.get(key)
    played_disp = human_last_played(lp_ts) if lp_ts else ""

    return ServerObject(
        fav=bool(favorites.get(key, False)),
        password=bool(data.get("password", False)),
        third_person=bool(data.get("third_person", False)),
        name=str(data.get("name") or "Unknown Server"),
        country=str(data.get("country") or ""),
        ip=ip,
        gport=gport,
        qport=qport,
        mod_count=mod_count,
        mods_preview=str(data.get("mods_preview") or ""),
        mods_json=str(data.get("mods_json") or ""),
        time=str(data.get("time") or "--:--"),
        timewarp=timewarp,
        played=played_disp,
        map_name=str(data.get("map_name") or data.get("map") or ""),
        players=players,
        max_players=max_players,
        ping=ping,
        bm_rank=bm_rank,
    )


def snapshot_from_obj(obj) -> dict:
    """Return the Companion panel snapshot for ``obj``."""
    try:
        ping = int(getattr(obj, "ping", -1))
    except Exception:
        ping = -1
    try:
        players = int(getattr(obj, "players", 0))
    except Exception:
        players = 0
    try:
        max_players = int(getattr(obj, "max_players", 0))
    except Exception:
        max_players = 0
    try:
        queue = int(getattr(obj, "queue", -1))
    except Exception:
        queue = -1
    try:
        mod_count = int(getattr(obj, "mod_count", 0))
    except Exception:
        mod_count = 0

    mode_parts = []
    mode_parts.append("3PP" if bool(getattr(obj, "third_person", False)) else "1PP")
    mode_parts.append("Password" if bool(getattr(obj, "password", False)) else "No password")
    mode_parts.append(f"Mods: {mod_count}")

    snapshot = {
        "name": str(getattr(obj, "name", "") or ""),
        "map": str(getattr(obj, "map_name", "") or ""),
        "ping": ping,
        "players": players,
        "max_players": max_players,
        "time": str(getattr(obj, "time", "") or ""),
        "online": ping >= 0,
        "mode": " / ".join(mode_parts),
        "ip": str(getattr(obj, "ip", "") or ""),
        "qport": int(getattr(obj, "qport", 0) or 0),
    }
    if queue >= 0:
        snapshot["queue"] = queue
    return snapshot
