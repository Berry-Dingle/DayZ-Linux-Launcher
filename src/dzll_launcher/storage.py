# storage.py
import json
import logging
import os
import time
from datetime import datetime

from .atomic_json import atomic_write_json
from .config import (
    FAV_PATH,
    LAST_PLAYED_PATH,
    LAST_PLAYED_PRUNE_DAYS,
    LAST_COMPANION_SERVER_PATH,
    COMPANION_RESTART_LEARNING_PATH,
    DEAD_PATH,
    DEAD_MAX_FAILS,
    CACHE_DIR,
)

logger = logging.getLogger(__name__)

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
    atomic_write_json(path, data, indent=2, sort_keys=True)

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
    if out != raw:
        try:
            save_last_played(out)  # persist prune
        except Exception:
            try:
                logger.exception("Could not persist pruned last-played history")
            except Exception:
                # Startup pruning is best-effort; diagnostics must not turn a
                # recoverable write failure back into a startup failure.
                pass
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

def load_last_companion_server() -> dict:
    return load_json_dict(LAST_COMPANION_SERVER_PATH)

def save_last_companion_server(data: dict) -> None:
    clean = {}
    for k, v in (data or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[str(k)] = v
    save_json_dict(LAST_COMPANION_SERVER_PATH, clean)

def clear_last_companion_server() -> None:
    try:
        if os.path.exists(LAST_COMPANION_SERVER_PATH):
            os.remove(LAST_COMPANION_SERVER_PATH)
    except Exception:
        pass

def load_companion_restart_learning() -> dict:
    return load_json_dict(COMPANION_RESTART_LEARNING_PATH)

def save_companion_restart_learning(data: dict) -> None:
    save_json_dict(COMPANION_RESTART_LEARNING_PATH, data or {})

def local_days_ago(ts: int, *, now_ts: int | None = None) -> int | None:
    try:
        joined_date = datetime.fromtimestamp(int(ts)).date()
        current_date = datetime.fromtimestamp(
            time.time() if now_ts is None else now_ts
        ).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return max(0, (current_date - joined_date).days)


def human_last_played(ts: int, *, now_ts: int | None = None) -> str:
    days = local_days_ago(ts, now_ts=now_ts)
    if days is None:
        return ""
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} Days Ago"

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

DEAD_EXPIRED_RETENTION_SECONDS = 7 * 86400
DEAD_LAST_FAIL_MAX_AGE_SECONDS = 30 * 86400


def prune_expired_dead_entries(dead: dict, *, now: int) -> bool:
    """Drop entries whose dead period ended over a week ago. Mutates ``dead``; returns True if changed."""
    changed = False
    for k in list(dead.keys()):
        d = dead.get(k, {})
        dead_until = int(d.get("dead_until", 0) or 0)
        if dead_until and dead_until < (now - DEAD_EXPIRED_RETENTION_SECONDS):
            dead.pop(k, None)
            changed = True
    return changed


def clamp_dead_cache_entries(dead: dict, *, now: int, max_fails: int = DEAD_MAX_FAILS) -> bool:
    """Drop entries with no failure in 30 days and cap ``fail_count``. Mutates ``dead``; returns True if changed."""
    cutoff = now - DEAD_LAST_FAIL_MAX_AGE_SECONDS
    changed = False

    for k in list(dead.keys()):
        d = dead.get(k, {})
        try:
            lf = int(d.get("last_fail", 0) or 0)
        except Exception:
            lf = 0

        if lf and lf < cutoff:
            dead.pop(k, None)
            changed = True
            continue

        try:
            fc = int(d.get("fail_count", 0) or 0)
        except Exception:
            fc = 0

        if fc > max_fails:
            d["fail_count"] = max_fails
            dead[k] = d
            changed = True

    return changed
