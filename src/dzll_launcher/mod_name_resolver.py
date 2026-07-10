#!/usr/bin/env python3
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .config import DB_LOCAL_PATH
from .mod_metadata import clean_display_mod_name, upsert_mod_metadata
from .steamcmd_mods import parse_mods_from_db


_CPP_NAME_RE = re.compile(r"\b(?:name|title)\s*=\s*(['\"])(?P<value>.*?)\1\s*;", re.IGNORECASE | re.DOTALL)


def _is_weak_name(name: str, mod_id=None) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    if text.startswith("@Mod-ID - "):
        return True
    if mod_id is not None:
        try:
            return text in (str(int(mod_id)), f"@{int(mod_id)}")
        except Exception:
            pass
    return False


def _clean_candidate(name, mod_id) -> str:
    cleaned = clean_display_mod_name(name, mod_id)
    return "" if _is_weak_name(cleaned, mod_id) else cleaned


def names_from_server_db(mod_ids) -> dict[int, str]:
    wanted = set()
    for raw_mid in mod_ids or []:
        try:
            mid = int(raw_mid)
        except Exception:
            continue
        if mid > 0:
            wanted.add(mid)
    if not wanted:
        return {}

    out: dict[int, str] = {}
    try:
        if not Path(DB_LOCAL_PATH).is_file():
            return {}
        con = sqlite3.connect(DB_LOCAL_PATH)
        try:
            cur = con.cursor()
            cur.execute("SELECT mods FROM servers WHERE mods IS NOT NULL AND mods != ''")
            for (mods_json,) in cur.fetchall():
                for mid, name in parse_mods_from_db(mods_json):
                    try:
                        mid_i = int(mid)
                    except Exception:
                        continue
                    if mid_i not in wanted or mid_i in out:
                        continue
                    cleaned = _clean_candidate(name, mid_i)
                    if cleaned:
                        out[mid_i] = cleaned
                if len(out) >= len(wanted):
                    break
        finally:
            con.close()
    except Exception:
        return out
    return out


def name_from_local_metadata(mod_id, *, workshop_dir: str = "") -> str:
    try:
        mid = int(mod_id)
    except Exception:
        return ""
    if mid <= 0:
        return ""

    root = Path(workshop_dir).expanduser() if workshop_dir else Path.home() / ".local/share/Steam/steamapps/workshop"
    mod_dir = root / "content" / "221100" / str(mid)
    try:
        if not mod_dir.is_dir() or mod_dir.is_symlink():
            return ""
    except Exception:
        return ""

    for filename in ("meta.cpp", "mod.cpp"):
        path = mod_dir / filename
        try:
            if not path.is_file() or path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")[:32768]
        except Exception:
            continue
        for match in _CPP_NAME_RE.finditer(text):
            cleaned = _clean_candidate(match.group("value"), mid)
            if cleaned:
                return cleaned
    return ""


def resolve_best_mod_names(mod_ids, *, metadata=None, workshop_dir: str = "", symlink_names=None) -> dict[int, str]:
    ids = []
    seen = set()
    for raw_mid in mod_ids or []:
        try:
            mid = int(raw_mid)
        except Exception:
            continue
        if mid > 0 and mid not in seen:
            ids.append(mid)
            seen.add(mid)

    metadata = metadata or {}
    symlink_names = symlink_names or {}
    db_names = names_from_server_db(ids)
    out: dict[int, str] = {}

    for mid in ids:
        meta = metadata.get(str(mid)) if isinstance(metadata, dict) else {}
        meta_name = str((meta or {}).get("name") or "").strip() if isinstance(meta, dict) else ""
        for candidate in (
            meta_name,
            db_names.get(mid, ""),
            name_from_local_metadata(mid, workshop_dir=workshop_dir),
            symlink_names.get(mid, "") if isinstance(symlink_names, dict) else "",
            "",
        ):
            cleaned = clean_display_mod_name(candidate, mid)
            if not _is_weak_name(cleaned, mid):
                out[mid] = cleaned
                break
        if mid not in out:
            out[mid] = clean_display_mod_name("", mid)

    for mid, name in out.items():
        if not _is_weak_name(name, mid):
            try:
                upsert_mod_metadata(mid, name=name)
            except Exception:
                pass
    return out
