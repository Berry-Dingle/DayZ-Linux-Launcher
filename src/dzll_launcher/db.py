# db.py
import os, requests
import sqlite3
import urllib.request

from .config import DB_URL, DB_LOCAL_DIR, DB_LOCAL_PATH, BL_URL, BL_LOCAL_DIR, BL_LOCAL_PATH

def fetch_db_overwrite_local() -> bool:
    os.makedirs(DB_LOCAL_DIR, exist_ok=True)
    tmp_path = DB_LOCAL_PATH + ".tmp"
    try:
        req = urllib.request.Request(DB_URL, headers={"User-Agent": "DZLL"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
        if not data or len(data) < 1024:
            raise RuntimeError("Downloaded DB looks too small")
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, DB_LOCAL_PATH)
        return True
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"[DB] Fetch failed: {e}")
        return False

def fetch_bl_overwrite_local(timeout=12) -> bool:
    try:
        os.makedirs(BL_LOCAL_DIR, exist_ok=True)
        tmp = BL_LOCAL_PATH + ".tmp"

        r = requests.get(BL_URL, timeout=timeout)
        r.raise_for_status()
        data = r.text or ""

        # sanity-check "JSON-ish" (after whitespace must start with '{')
        if not data.lstrip().startswith("{"):
            raise ValueError("Blocklist not JSON object")

        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)

        os.replace(tmp, BL_LOCAL_PATH)
        return True

    except Exception as e:
        print(f"[BL] Fetch failed: {e}")
        try:
            tmp = BL_LOCAL_PATH + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def read_servers_from_db() -> list:
    if not os.path.exists(DB_LOCAL_PATH):
        return []
    try:
        con = sqlite3.connect(DB_LOCAL_PATH)
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
                country, ping
            FROM servers
            ORDER BY players DESC, ping ASC
        """)
        rows = cur.fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[DB] Read failed: {e}")
        return []
