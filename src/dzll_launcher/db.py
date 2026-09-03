# db.py
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import APP_VERSION, DB_URL, DB_LOCAL_DIR, DB_LOCAL_PATH

DB_USER_AGENT = f"DZLL/{str(APP_VERSION or '0.2').lstrip('v')} (+https://github.com/Berry-Dingle/DayZ-Linux-Launcher)"
DB_FETCH_ATTEMPTS = 3
DB_FETCH_TRANSIENT_HTTP = {400, 408, 429, 500, 502, 503, 504}


def _remove_tmp(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _log_db_http_error(err: urllib.error.HTTPError, attempt: int, attempts: int) -> None:
    status = getattr(err, "code", None)
    reason = getattr(err, "reason", "") or ""
    url = getattr(err, "url", "") or DB_URL
    print(f"[DB] Fetch HTTP error attempt {attempt}/{attempts}: status={status} reason={reason} url={url}")

    headers = getattr(err, "headers", None)
    if headers is not None:
        parts = []
        for name in ("date", "server", "content-type", "x-github-request-id", "x-cache", "via"):
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value:
                parts.append(f"{name}={value}")
        if parts:
            print(f"[DB] Fetch HTTP headers: {' '.join(parts)}")

    try:
        body = err.read(512)
    except Exception:
        body = b""
    if body:
        print(f"[DB] Fetch HTTP body: {body.decode('utf-8', errors='replace')}")


def _fetch_db_bytes_with_retries() -> bytes | None:
    backoffs = (0.5, 1.5)
    last_error = None
    for attempt in range(1, DB_FETCH_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(DB_URL, headers={"User-Agent": DB_USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_error = e
            _log_db_http_error(e, attempt, DB_FETCH_ATTEMPTS)
            if getattr(e, "code", None) not in DB_FETCH_TRANSIENT_HTTP or attempt >= DB_FETCH_ATTEMPTS:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            print(f"[DB] Fetch network error attempt {attempt}/{DB_FETCH_ATTEMPTS}: {type(e).__name__}: {e}")
            if attempt >= DB_FETCH_ATTEMPTS:
                break

        if attempt < DB_FETCH_ATTEMPTS:
            time.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])

    if last_error is not None:
        print(f"[DB] Fetch failed: {last_error}")
    return None

def fetch_db_overwrite_local() -> bool:
    os.makedirs(DB_LOCAL_DIR, exist_ok=True)
    tmp_path = DB_LOCAL_PATH + ".tmp"
    try:
        _remove_tmp(tmp_path)
        data = _fetch_db_bytes_with_retries()
        if data is None:
            return False
        if not data or len(data) < 1024:
            raise RuntimeError("Downloaded DB looks too small")
        with open(tmp_path, "wb") as f:
            f.write(data)
        con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            if cur.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Downloaded DB failed integrity check")
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='servers'")
            if cur.fetchone() is None:
                raise RuntimeError("Downloaded DB missing servers table")
            required_columns = {
                "ip", "gport", "qport",
                "name", "map",
                "players", "maxPlayers",
                "password", "mods", "modCount",
                "third_person",
                "timeWarp", "time",
                "country", "ping", "bm_rank",
            }
            cur.execute("PRAGMA table_info(servers)")
            columns = {row[1] for row in cur.fetchall()}
            missing_columns = sorted(required_columns - columns)
            if missing_columns:
                raise RuntimeError(f"Downloaded DB servers table missing columns: {', '.join(missing_columns)}")
            row_count = cur.execute("SELECT COUNT(*) FROM servers").fetchone()[0]
            if row_count <= 0:
                raise RuntimeError("Downloaded DB servers table is empty")
        finally:
            con.close()
        os.replace(tmp_path, DB_LOCAL_PATH)
        return True
    except Exception as e:
        _remove_tmp(tmp_path)
        print(f"[DB] Fetch failed: {e}")
        return False

def read_servers_from_db() -> list:
    if not os.path.exists(DB_LOCAL_PATH):
        return []
    con = None
    try:
        db_path = Path(DB_LOCAL_PATH).expanduser().resolve()
        db_uri = f"{db_path.as_uri()}?mode=ro"
        con = sqlite3.connect(db_uri, uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT
                ip, gport, qport,
                name, map,
                players, maxPlayers,
                password, mods, modCount,
                third_person,
                timeWarp, time,
                country, ping, bm_rank
            FROM servers
            ORDER BY players DESC, ping ASC
        """)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB] Read failed: {e}")
        return []
    finally:
        if con is not None:
            con.close()
