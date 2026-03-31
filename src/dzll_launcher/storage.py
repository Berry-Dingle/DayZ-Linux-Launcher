# storage.py
import os
import json
import time

from .config import (
    FAV_PATH,
    LAST_PLAYED_PATH,
    LAST_PLAYED_PRUNE_DAYS,
    DEAD_PATH,
    CACHE_DIR,
)

def load_json_dict(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}

def save_json_dict(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)

def load_favorites() -> dict:
    raw = load_json_dict(FAV_PATH)
    out = {}
    for k, v in raw.items():
        out[str(k)] = bool(v)
    return out

def save_favorites(favs: dict) -> None:
    clean = {k: True for k, v in (favs or {}).items() if v}
    save_json_dict(FAV_PATH, clean)

def load_last_played() -> dict:
    now = int(time.time())
    cutoff = LAST_PLAYED_PRUNE_DAYS * 86400
    raw = load_json_dict(LAST_PLAYED_PATH)
    out = {}
    for k, v in raw.items():
        try:
            ts = int(v)
        except Exception:
            continue
        if now - ts <= cutoff:
            out[str(k)] = ts
    save_last_played(out)  # persist prune
    return out

def save_last_played(lp: dict) -> None:
    now = int(time.time())
    cutoff = LAST_PLAYED_PRUNE_DAYS * 86400
    clean = {}
    for k, ts in (lp or {}).items():
        try:
            its = int(ts)
        except Exception:
            continue
        if now - its <= cutoff:
            clean[str(k)] = its
    save_json_dict(LAST_PLAYED_PATH, clean)

def human_last_played(ts: int) -> str:
    try:
        ts = int(ts)
    except Exception:
        return ""
    now = int(time.time())
    days = max(0, int((now - ts) // 86400))
    if days <= 0:
        return "Today"
    if days == 1:
        return "1 Day ago"
    return f"{days} Days ago"

def load_dead_cache() -> dict:
    raw = load_json_dict(DEAD_PATH)
    out = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(k)] = {
                "fail_count": int(v.get("fail_count", 0) or 0),
                "dead_until": int(v.get("dead_until", 0) or 0),
                "last_fail": int(v.get("last_fail", 0) or 0),
            }
        except Exception:
            continue
    return out

def save_dead_cache(dead: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    save_json_dict(DEAD_PATH, dead or {})
