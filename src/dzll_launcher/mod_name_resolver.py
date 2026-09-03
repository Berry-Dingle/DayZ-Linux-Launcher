#!/usr/bin/env python3
from __future__ import annotations

import re
import sqlite3
import logging
from pathlib import Path

from .config import DB_LOCAL_PATH
from .mod_metadata import clean_display_mod_name, upsert_many_names
from .steamcmd_mods import parse_mods_from_db


_CPP_NAME_RE = re.compile(r"\b(?:name|title)\s*=\s*(['\"])(?P<value>.*?)\1\s*;", re.IGNORECASE | re.DOTALL)
logger = logging.getLogger(__name__)


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


def _local_metadata_roots(*, workshop_dir: str = "", workshop_roots=None) -> tuple[Path, ...]:
    if workshop_roots is None:
        root = (
            Path(workshop_dir).expanduser()
            if workshop_dir
            else Path.home() / ".local/share/Steam/steamapps/workshop"
        )
        return (root,)

    out: list[Path] = []
    seen = set()
    for raw_root in workshop_roots:
        try:
            root = Path(raw_root).expanduser()
        except Exception:
            continue
        key = str(root)
        if key in seen:
            continue
        out.append(root)
        seen.add(key)
    return tuple(out)


def name_from_local_metadata(
    mod_id,
    *,
    workshop_dir: str = "",
    workshop_roots=None,
) -> str:
    try:
        mid = int(mod_id)
    except Exception:
        return ""
    if mid <= 0:
        return ""

    roots = _local_metadata_roots(
        workshop_dir=workshop_dir,
        workshop_roots=workshop_roots,
    )
    for root in roots:
        try:
            canonical_root = root.resolve()
        except Exception:
            continue
        mod_dir = root / "content" / "221100" / str(mid)
        try:
            if not mod_dir.is_dir() or mod_dir.is_symlink():
                continue
        except Exception:
            continue

        for filename in ("meta.cpp", "mod.cpp"):
            path = mod_dir / filename
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                canonical_path = path.resolve()
                try:
                    canonical_path.relative_to(canonical_root)
                except ValueError:
                    logger.debug(
                        "Skipping local mod metadata outside Workshop root for mod %s",
                        mid,
                    )
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")[:32768]
            except Exception:
                continue
            for match in _CPP_NAME_RE.finditer(text):
                cleaned = _clean_candidate(match.group("value"), mid)
                if cleaned:
                    return cleaned
    return ""


def resolve_best_mod_names(
    mod_ids,
    *,
    metadata=None,
    workshop_dir: str = "",
    workshop_roots=None,
    symlink_names=None,
) -> dict[int, str]:
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
    local_roots = _local_metadata_roots(
        workshop_dir=workshop_dir,
        workshop_roots=workshop_roots,
    )
    out: dict[int, str] = {}

    for mid in ids:
        meta = metadata.get(str(mid)) if isinstance(metadata, dict) else {}
        meta_name = str((meta or {}).get("name") or "").strip() if isinstance(meta, dict) else ""
        for candidate in (
            meta_name,
            db_names.get(mid, ""),
            name_from_local_metadata(mid, workshop_roots=local_roots),
            symlink_names.get(mid, "") if isinstance(symlink_names, dict) else "",
            "",
        ):
            cleaned = clean_display_mod_name(candidate, mid)
            if not _is_weak_name(cleaned, mid):
                out[mid] = cleaned
                break
        if mid not in out:
            out[mid] = clean_display_mod_name("", mid)

    strong_names = {
        mid: name for mid, name in out.items()
        if not _is_weak_name(name, mid)
    }
    if strong_names:
        try:
            # The cache is non-authoritative: persistence is one best-effort,
            # all-or-nothing batch and must never hide successfully resolved names.
            upsert_many_names(strong_names)
        except Exception:
            pass
    return out
