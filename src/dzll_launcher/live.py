# live.py
import time
import re

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_QUEUE_RE = re.compile(r"^lqs(\d+)$", re.IGNORECASE)

def is_valid_hhmm(s: str) -> bool:
    return bool(_TIME_RE.match((s or "").strip()))

def extract_queue_from_keywords(keywords) -> int | None:
    if not keywords:
        return None
    try:
        for token in str(keywords).split(","):
            match = _QUEUE_RE.fullmatch(token.strip())
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None

def extract_time_from_keywords(keywords: str) -> str:
    """
    Scan keywords and return the first valid HH:MM token (from the end).
    Prevents ports / random numeric tokens from being treated as time.
    """
    if not keywords:
        return ""
    try:
        parts = [p.strip() for p in str(keywords).split(",") if p.strip()]
        # Walk backwards so we prefer the last time token (as Tracky/servers often append it at the end)
        for token in reversed(parts):
            if is_valid_hhmm(token):
                return token
    except Exception:
        pass
    return ""

def query_server_live(ip: str, qport: int, timeout: float = 2.0) -> dict:
    try:
        from . import a2s
    except Exception:
        return {"ok": False, "err": "vendored a2s unavailable"}

    addr = (ip, int(qport))
    try:
        t0 = time.time()
        info = a2s.info(addr, timeout=float(timeout))
        t1 = time.time()

        ping_s = None
        try:
            ping_s = float(getattr(info, "ping", None))
        except Exception:
            ping_s = None
        if ping_s is None or ping_s <= 0:
            ping_s = max(0.0, t1 - t0)
        ping_ms = int(round(ping_s * 1000))

        players = int(getattr(info, "player_count", 0) or 0)
        maxp = int(getattr(info, "max_players", 0) or 0)

        kw = getattr(info, "keywords", "") or ""
        t = extract_time_from_keywords(kw)
        queue = extract_queue_from_keywords(kw)

        pw = bool(getattr(info, "password_protected", False))

        return {
            "ok": True,
            "ping_ms": ping_ms,
            "players": players,
            "max_players": maxp,
            "queue": queue,
            "time": t,
            "password": pw,
        }
    except Exception as e:
        return {"ok": False, "err": str(e)}
