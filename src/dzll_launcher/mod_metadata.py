#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Iterable

from .config import CACHE_DIR


MOD_METADATA_PATH = os.path.join(CACHE_DIR, "mod_metadata.json")
SCHEMA_VERSION = 1
_ID_SUFFIX_RE = re.compile(r"^(?P<name>.+)__\d+$")
_CLEARABLE_FIELDS = {"install_folder"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mod_key(mod_id) -> str:
    return str(int(mod_id))


def _fallback_name(mod_id) -> str:
    return f"@Mod-ID - {int(mod_id)}"


def _is_fallback_name(name: str, mod_id=None) -> bool:
    clean = str(name or "").strip()
    if not clean:
        return True
    if mod_id is not None:
        try:
            return clean == _fallback_name(mod_id)
        except Exception:
            pass
    return clean.startswith("@Mod-ID - ")


def load_mod_metadata() -> dict:
    try:
        if os.path.exists(MOD_METADATA_PATH):
            with open(MOD_METADATA_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    mods = data.get("mods")
                    if isinstance(mods, dict):
                        return {"version": int(data.get("version") or SCHEMA_VERSION), "mods": mods}
    except Exception:
        pass
    return {"version": SCHEMA_VERSION, "mods": {}}


def save_mod_metadata(data: dict) -> None:
    clean = data if isinstance(data, dict) else {}
    mods = clean.get("mods")
    if not isinstance(mods, dict):
        mods = {}
    out = {"version": SCHEMA_VERSION, "mods": mods}
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="mod_metadata.", suffix=".tmp", dir=CACHE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(out, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, MOD_METADATA_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def clean_display_mod_name(name, mod_id=None) -> str:
    raw = str(name or "").strip()
    if raw:
        match = _ID_SUFFIX_RE.match(raw)
        if match:
            raw = match.group("name").strip()
    if raw:
        return raw
    if mod_id is not None:
        try:
            return _fallback_name(mod_id)
        except Exception:
            pass
    return ""


def upsert_mod_metadata(
    mod_id,
    *,
    name=None,
    size_bytes=None,
    install_folder=None,
    subscribed=None,
    installed=None,
    updated_at=None,
    clear_fields=None,
) -> None:
    key = _mod_key(mod_id)
    mid = int(mod_id)
    now = updated_at or _utc_now()
    data = load_mod_metadata()
    mods = data.setdefault("mods", {})
    existing = mods.get(key) if isinstance(mods.get(key), dict) else {}
    entry = dict(existing)
    entry["id"] = mid

    for field in clear_fields or []:
        if field in _CLEARABLE_FIELDS:
            entry[field] = None

    cleaned_name = clean_display_mod_name(name, mid) if name is not None else None
    if cleaned_name:
        existing_name = str(entry.get("name") or "").strip()
        if not (_is_fallback_name(cleaned_name, mid) and existing_name and not _is_fallback_name(existing_name, mid)):
            entry["name"] = cleaned_name

    if size_bytes is not None:
        try:
            size_i = int(size_bytes)
            if size_i > 0:
                entry["size_bytes"] = size_i
        except Exception:
            pass
    if install_folder is not None:
        folder = str(install_folder or "").strip()
        if folder:
            entry["install_folder"] = folder
    if subscribed is not None:
        entry["subscribed"] = bool(subscribed)
    if installed is not None:
        entry["installed"] = bool(installed)
    entry["updated_at"] = str(now)

    mods[key] = entry
    save_mod_metadata(data)


def upsert_many_from_ugc_state(state_by_id, *, names_by_id=None) -> None:
    names = names_by_id or {}
    data = load_mod_metadata()
    mods = data.setdefault("mods", {})
    now = _utc_now()
    for raw_mid, state in (state_by_id or {}).items():
        if not isinstance(state, dict):
            continue
        try:
            mid = int(raw_mid)
        except Exception:
            try:
                mid = int(state.get("id") or 0)
            except Exception:
                mid = 0
        if mid <= 0:
            continue
        key = str(mid)
        existing = mods.get(key) if isinstance(mods.get(key), dict) else {}
        entry = dict(existing)
        entry["id"] = mid

        name = names.get(mid) or names.get(str(mid)) if isinstance(names, dict) else None
        cleaned_name = clean_display_mod_name(name, mid) if name is not None else None
        if cleaned_name:
            existing_name = str(entry.get("name") or "").strip()
            if not (_is_fallback_name(cleaned_name, mid) and existing_name and not _is_fallback_name(existing_name, mid)):
                entry["name"] = cleaned_name

        size_bytes = 0
        for field in ("size_on_disk", "total_bytes"):
            try:
                candidate = int(state.get(field) or 0)
            except Exception:
                candidate = 0
            if candidate > 0:
                size_bytes = candidate
                break
        if size_bytes > 0:
            entry["size_bytes"] = size_bytes

        folder = str(state.get("install_folder") or "").strip()
        if folder:
            entry["install_folder"] = folder
        elif state.get("install_folder") is None and bool(state.get("installed")) is False:
            entry["install_folder"] = None
        if "subscribed" in state:
            entry["subscribed"] = bool(state.get("subscribed"))
        if "installed" in state:
            entry["installed"] = bool(state.get("installed"))
        entry["updated_at"] = now
        mods[key] = entry
    save_mod_metadata(data)


def reconcile_mod_metadata(*, subscribed_ids=None, mod_ids=None, force_unsubscribed: bool = False) -> dict:
    """
    Reconcile cached metadata with local install folders and known subscription state.

    Existing descriptive fields such as name, size_bytes, last_used_at, and unchanged
    updated_at values are preserved. Entries changed by reconciliation get a fresh
    updated_at timestamp.
    """
    subscribed_set = None
    if subscribed_ids is not None:
        subscribed_set = set()
        for raw_mid in subscribed_ids or []:
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid > 0:
                subscribed_set.add(mid)

    target_set = None
    if mod_ids is not None:
        target_set = set()
        for raw_mid in mod_ids or []:
            try:
                mid = int(raw_mid)
            except Exception:
                continue
            if mid > 0:
                target_set.add(mid)

    data = load_mod_metadata()
    mods = data.setdefault("mods", {})
    now = _utc_now()
    changed = 0
    cleared_installed = 0
    cleared_subscribed = 0

    for raw_key, raw_entry in list(mods.items()):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        try:
            mid = int(entry.get("id") or raw_key)
        except Exception:
            continue
        if mid <= 0:
            continue

        entry_changed = False
        folder = str(entry.get("install_folder") or "").strip()
        folder_exists = False
        if folder:
            try:
                expanded_folder = os.path.expanduser(folder)
                folder_exists = os.path.isdir(expanded_folder) and not os.path.islink(expanded_folder)
            except Exception:
                folder_exists = False

        if bool(entry.get("installed", False)) and not folder_exists:
            entry["installed"] = False
            entry["install_folder"] = None
            entry_changed = True
            cleared_installed += 1
        elif folder and not folder_exists:
            entry["install_folder"] = None
            entry_changed = True

        should_reconcile_subscription = target_set is None or mid in target_set
        should_clear_subscription = (
            force_unsubscribed
            or (should_reconcile_subscription and subscribed_set is not None and mid not in subscribed_set)
        )
        if should_clear_subscription:
            if bool(entry.get("subscribed", False)):
                entry["subscribed"] = False
                entry_changed = True
                cleared_subscribed += 1

        if entry_changed:
            entry["id"] = mid
            entry["updated_at"] = now
            mods[str(mid)] = entry
            if str(raw_key) != str(mid):
                try:
                    del mods[raw_key]
                except Exception:
                    pass
            changed += 1

    if changed:
        save_mod_metadata(data)

    return {
        "changed": changed,
        "cleared_installed": cleared_installed,
        "cleared_subscribed": cleared_subscribed,
    }


def mark_mods_used(mod_ids: Iterable[int], *, names_by_id=None, used_at=None) -> None:
    data = load_mod_metadata()
    mods = data.setdefault("mods", {})
    timestamp = used_at or _utc_now()
    names = names_by_id or {}
    for raw_mid in mod_ids or []:
        try:
            mid = int(raw_mid)
        except Exception:
            continue
        if mid <= 0:
            continue
        key = str(mid)
        existing = mods.get(key) if isinstance(mods.get(key), dict) else {}
        entry = dict(existing)
        entry["id"] = mid
        name = names.get(mid) or names.get(str(mid)) if isinstance(names, dict) else None
        cleaned_name = clean_display_mod_name(name, mid) if name is not None else None
        if cleaned_name:
            existing_name = str(entry.get("name") or "").strip()
            if not (_is_fallback_name(cleaned_name, mid) and existing_name and not _is_fallback_name(existing_name, mid)):
                entry["name"] = cleaned_name
        entry["last_used_at"] = str(timestamp)
        entry["updated_at"] = str(timestamp)
        mods[key] = entry
    save_mod_metadata(data)
