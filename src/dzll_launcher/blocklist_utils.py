#!/usr/bin/env python3
import json
import os


def bl_normalize_key(ip: str, port) -> str:
    return f"{str(ip).strip()}:{str(port).strip()}".lower()


def bl_load_local() -> dict:
    """
    Fail-open loader for local blocklist.json v2.

    Returns a dict with keys:
      bl_ip_hard, bl_allow_exact, bl_soft, bl_hard, _bl_ok, _bl_soft, _bl_hard
    """
    # fail-open defaults
    result = {
        "bl_ip_hard": set(),
        "bl_allow_exact": set(),
        "bl_soft": set(),
        "bl_hard": set(),
        "_bl_ok": False,
        "_bl_soft": set(),
        "_bl_hard": set(),
    }

    try:
        from .config import BL_LOCAL_PATH

        if not os.path.exists(BL_LOCAL_PATH):
            return result

        with open(BL_LOCAL_PATH, "r", encoding="utf-8") as f:
            j = json.load(f)

        if not isinstance(j, dict):
            return result
        if int(j.get("version", 0)) != 2:
            return result

        ip_hard = set()
        for ip in (j.get("ip_hard_blocked") or []):
            s = str(ip or "").strip()
            if s:
                ip_hard.add(s)

        allow_exact = set()
        for k in (j.get("allow_exact") or []):
            s = str(k or "").strip().lower()
            if s and (":" in s):
                allow_exact.add(s)

        soft = set()
        for e in (j.get("soft_blocked") or []):
            if isinstance(e, dict):
                k = str(e.get("key") or "").strip().lower()
                if k and (":" in k):
                    soft.add(k)

        hard = set()
        for e in (j.get("hard_blocked") or []):
            if isinstance(e, dict):
                k = str(e.get("key") or "").strip().lower()
                if k and (":" in k):
                    hard.add(k)

        result["bl_ip_hard"] = ip_hard
        result["bl_allow_exact"] = allow_exact
        result["bl_soft"] = soft
        result["bl_hard"] = hard
        result["_bl_soft"] = set(soft)
        result["_bl_hard"] = set(hard)
        result["_bl_ok"] = True
        return result

    except Exception:
        return result


def bl_status(
    key_lc: str,
    bl_allow_exact: set,
    bl_ip_hard: set,
    bl_hard: set,
    bl_soft: set,
) -> str:
    """
    Returns: allowed | ip_hard | hard | soft
    Implements the authoritative priority order.
    key_lc must be lowercase "ip:gport".
    """
    if not key_lc:
        return "allowed"

    if key_lc in (bl_allow_exact or set()):
        return "allowed"

    ip = key_lc.split(":", 1)[0]

    if ip in (bl_ip_hard or set()):
        return "ip_hard"

    if key_lc in (bl_hard or set()):
        return "hard"

    if key_lc in (bl_soft or set()):
        return "soft"

    return "allowed"