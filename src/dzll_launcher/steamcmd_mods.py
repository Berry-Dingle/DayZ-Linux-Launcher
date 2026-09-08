#!/usr/bin/env python3
# steamcmd_mods.py
#
# Shared DayZ Workshop mod helpers.

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple
from .mod_metadata import clean_display_mod_name


logger = logging.getLogger(__name__)

DAYZ_APPID = 221100

def parse_mods_from_db(mods_json: str) -> List[Tuple[int, str]]:
    """
    Parse DB 'mods' column (TEXT JSON):
      [{"name": "...", "steamWorkshopId": 123}, ...]
    Returns ordered list of (id, name). De-dupes IDs, keeps first occurrence order.
    """
    if not mods_json:
        return []
    try:
        arr = json.loads(mods_json)
    except Exception:
        return []
    if not isinstance(arr, list) or not arr:
        return []

    out: List[Tuple[int, str]] = []
    seen = set()

    for it in arr:
        if not isinstance(it, dict):
            continue
        sid = it.get("steamWorkshopId")
        if sid is None:
            continue
        try:
            sid_i = int(sid)
        except Exception:
            continue
        if sid_i <= 0 or sid_i in seen:
            continue
        nm = clean_display_mod_name(it.get("name"), sid_i, fallback=False)
        out.append((sid_i, nm))
        seen.add(sid_i)

    return out


def workshop_mod_path(workshop_dir: str, mod_id: int) -> str:
    # workshop_dir is expected to be ".../steamapps/workshop"
    return os.path.join(workshop_dir, "content", str(DAYZ_APPID), str(int(mod_id)))


def _has_real_mod_content(mod_path: str) -> bool:
    """
    Heuristic to avoid false 'installed' after interrupted downloads.
    DayZ workshop mods normally contain .pbo files (usually under addons/).
    Fallback: require at least one non-zero file anywhere in the folder.
    """
    try:
        if not os.path.isdir(mod_path):
            return False

        # Fast path: look for .pbo in addons/
        addons = os.path.join(mod_path, "addons")
        if os.path.isdir(addons):
            try:
                for name in os.listdir(addons):
                    if name.lower().endswith(".pbo"):
                        p = os.path.join(addons, name)
                        try:
                            if os.path.getsize(p) > 0:
                                return True
                        except Exception:
                            return True
            except Exception:
                pass

        # Slower path: scan for any .pbo anywhere
        for root, _dirs, files in os.walk(mod_path):
            for fn in files:
                if fn.lower().endswith(".pbo"):
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fp) > 0:
                            return True
                    except Exception:
                        return True

        # Fallback: at least one non-zero file anywhere (catches non-pbo content)
        for root, _dirs, files in os.walk(mod_path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    if os.path.getsize(fp) > 0:
                        return True
                except Exception:
                    pass

    except Exception:
        return False

    return False


def is_mod_installed(workshop_dir: str, mod_id: int) -> bool:
    p = workshop_mod_path(workshop_dir, mod_id)
    return _has_real_mod_content(p)


def compute_missing_mods(workshop_dir: str, mods: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
    missing: List[Tuple[int, str]] = []
    for mid, name in mods:
        if not is_mod_installed(workshop_dir, mid):
            missing.append((mid, name))
    return missing


def _resolve_path(p: str) -> str:
    p = (p or "").strip()
    if not p:
        return ""
    return os.path.abspath(os.path.expanduser(p))


def _real_path(p: str) -> str:
    p = _resolve_path(p)
    if not p:
        return ""
    return os.path.realpath(p)


def parse_additional_mod_ids(text: str) -> List[int]:
    """
    Parses additional mod IDs from settings string.
    Accepts commas/spaces/newlines/semicolons.
    De-dupes, preserves order.
    """
    if not text:
        return []
    raw = str(text)
    for ch in [",", ";", "\n", "\t"]:
        raw = raw.replace(ch, " ")
    out: List[int] = []
    seen = set()
    for tok in raw.split():
        try:
            mid = int(tok.strip())
        except Exception:
            continue
        if mid <= 0 or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def merge_mod_lists_with_additional(
    mods: List[Tuple[int, str]],
    additional_ids: List[int],
) -> List[Tuple[int, str]]:
    """
    Keeps server mods first, appends extra IDs not already present.
    Extra names are blank.
    """
    out: List[Tuple[int, str]] = []
    seen = set()
    for mid, name in (mods or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i <= 0 or mid_i in seen:
            continue
        out.append((mid_i, name or ""))
        seen.add(mid_i)

    for mid in (additional_ids or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i <= 0 or mid_i in seen:
            continue
        out.append((mid_i, ""))
        seen.add(mid_i)

    return out


def ensure_dir(path: str) -> str:
    p = _resolve_path(path)
    os.makedirs(p, exist_ok=True)
    return p


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or "")
    return raw[:max(0, int(max_bytes))].decode("utf-8", errors="ignore")


def _watch_name_max(watch_folder: str) -> int:
    try:
        value = int(os.pathconf(watch_folder, "PC_NAME_MAX"))
        if value > 0:
            return value
    except (OSError, TypeError, ValueError):
        pass
    return 255


def symlink_name_for_mod(mod_name: str, mod_id: int, *, name_max: int = 255) -> str:
    """
    Prefer launcher-friendly @Name__<id>, with a deterministic @Mod fallback.
    We keep spaces (launcher sample shows '@Code Lock').
    """
    mid = int(mod_id)
    nm = clean_display_mod_name(mod_name, mid, fallback=False)
    if nm:
        nm = nm.replace("++", "pp").replace("+", "plus")
        nm = re.sub(r"\]\s*\[|\)\s*\(|}\s*{", "_", nm)
        nm = re.sub(r"[\[\](){}]", "", nm)
        nm = re.sub(r'[/\\:;|"<>*?%&#=,.]', "_", nm)
        nm = re.sub(r"\s+", " ", nm)
        nm = re.sub(r"_+", "_", nm)
        nm = re.sub(r"\s*_\s*", "_", nm)
        nm = nm.strip(" _.")
    nm = nm or "Mod"
    if not nm.startswith("@"):
        nm = "@" + nm

    suffix = f"__{mid}"
    limit = max(1, int(name_max or 255))
    minimum_component = f"@Mod{suffix}"
    minimum_bytes = len(minimum_component.encode("utf-8"))
    if minimum_bytes > limit:
        raise ValueError(
            f"NAME_MAX {limit} cannot fit required watch-link name "
            f"{minimum_component!r} ({minimum_bytes} bytes)"
        )
    prefix_budget = limit - len(suffix.encode("utf-8"))
    nm = _truncate_utf8(nm, prefix_budget).strip(" _.") or "@Mod"
    component = f"{nm}{suffix}"
    return component


def _is_dzll_owned_symlink_name(name: str) -> bool:
    """
    Names DZLL has created for watch-folder symlinks.
    Keep this narrow so cleanup never removes arbitrary user symlinks.
    """
    s = str(name or "")
    return bool(
        re.match(r"^@.+__\d+$", s)
        or re.match(r"^@\d+$", s)
    )


def _mod_id_from_dzll_symlink_name(name: str) -> int | None:
    s = str(name or "")
    m = re.search(r"__(\d+)$", s) or re.match(r"^@(\d+)$", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _resolve_symlink_target(link_path: str) -> str:
    raw = os.readlink(link_path)
    if os.path.isabs(raw):
        return _real_path(raw)
    return _real_path(os.path.join(os.path.dirname(link_path), raw))


def remove_dzll_symlinks_for_mod(mod_id, *, proton_prefix: str = "", log_fn=None) -> List[str]:
    """
    Remove only DZLL-owned watch-folder symlinks whose name encodes mod_id.
    This intentionally does not remove arbitrary symlinks or non-symlink paths.
    """
    def log(msg: str):
        try:
            if callable(log_fn):
                log_fn(msg)
            else:
                print(msg)
        except Exception:
            pass

    try:
        mid = int(mod_id)
    except Exception:
        return []
    if mid <= 0:
        return []

    pfx = _resolve_path(proton_prefix)
    if not pfx:
        try:
            from .steam_native import dayz_compatdata_dir

            compatdata = dayz_compatdata_dir()
            if compatdata is not None:
                pfx = str(Path(compatdata) / "pfx")
        except Exception:
            pfx = ""
    if not pfx:
        home = str(Path.home())
        pfx = os.path.join(home, ".local/share/Steam/steamapps/compatdata/221100/pfx")

    pfx_user = os.path.join(pfx, "drive_c/users/steamuser")
    watch_folders = [
        os.path.join(pfx_user, "DZLLMods"),
        os.path.join(pfx_user, "Documents", "Templates"),
    ]
    removed: List[str] = []

    for watch_folder in watch_folders:
        if not os.path.isdir(watch_folder):
            continue
        try:
            for name in os.listdir(watch_folder):
                if not _is_dzll_owned_symlink_name(name):
                    continue
                if _mod_id_from_dzll_symlink_name(name) != mid:
                    continue
                path = os.path.join(watch_folder, name)
                if not os.path.islink(path):
                    continue
                try:
                    os.unlink(path)
                    removed.append(path)
                    log(f"[MOD DELETE] removed DZLL symlink: {path}")
                except Exception as exc:
                    log(f"[MOD DELETE] failed to remove DZLL symlink {path}: {exc}")
        except Exception as exc:
            log(f"[MOD DELETE] failed to scan DZLL symlinks in {watch_folder}: {exc}")

    return removed


def _debug_join_paths_enabled() -> bool:
    return str(os.environ.get("DZLL_DEBUG_JOIN_PATHS") or "").strip().lower() in ("1", "true", "yes", "on")


def validate_selected_watch_symlinks(
    *,
    selected_paths: List[str],
    mods: List[Tuple[int, str]],
) -> List[str]:
    """
    Validate selected -mod symlink paths immediately before launch.
    Returns human-readable error strings. Does not modify the filesystem.
    """
    errors: List[str] = []
    by_path: Dict[str, Tuple[int, str]] = {}
    for idx, link_path in enumerate(selected_paths or []):
        mid = 0
        name = ""
        try:
            if idx < len(mods or []):
                mid, name = mods[idx]
        except Exception:
            pass
        by_path[str(link_path)] = (int(mid or 0), str(name or ""))

    for link_path in selected_paths or []:
        mid, name = by_path.get(str(link_path), (0, ""))
        label = f"{mid}"
        if name:
            label = f"{mid} ({name})"
        try:
            if not os.path.islink(link_path):
                errors.append(f"mod {label}: selected path is not a symlink: {link_path}")
                continue
            target = _resolve_symlink_target(link_path)
            if not os.path.isdir(target):
                errors.append(f"mod {label}: symlink target is missing/not a directory: {link_path} -> {target}")
                continue
            if not _has_real_mod_content(target):
                errors.append(f"mod {label}: symlink target has no real mod content: {link_path} -> {target}")
        except Exception as e:
            errors.append(f"mod {label}: failed to validate selected path {link_path}: {e}")
    return errors


def ensure_watch_symlinks(
    *,
    workshop_dir: str,
    mods: List[Tuple[int, str]],
    watch_folder: str,
    cleanup_stale: bool = True,
) -> Dict[str, List[str]]:
    """
    Creates symlinks in watch_folder -> workshop content/221100/<id>.
    Returns dict with created/updated/kept/removed/selected_paths/errors.
    selected_paths are Linux paths to the link entries (for current join set).
    """
    workshop_dir = _resolve_path(workshop_dir)
    watch_folder = ensure_dir(watch_folder)
    watch_name_max = _watch_name_max(watch_folder)
    debug_join_paths = _debug_join_paths_enabled()

    result = {
        "created": [],
        "updated": [],
        "kept": [],
        "removed": [],
        "selected_paths": [],
        "errors": [],
    }

    desired_links: Dict[str, str] = {}  # link_path -> target_path
    name_generation_failed = False

    for mid, name in (mods or []):
        try:
            mid_i = int(mid)
        except Exception:
            continue
        target = workshop_mod_path(workshop_dir, mid_i)
        if not os.path.isdir(target):
            msg = f"missing target for mod {mid_i}: {target}"
            logger.error("Join symlink missing target: %s", msg)
            result["errors"].append(msg)
            continue
        if not _has_real_mod_content(target):
            msg = f"invalid/missing mod content for mod {mid_i}: {target}"
            logger.error("Join symlink missing target content: %s", msg)
            result["errors"].append(msg)
            continue
        try:
            link_name = symlink_name_for_mod(name, mid_i, name_max=watch_name_max)
        except ValueError as error:
            result["errors"].append(f"mod {mid_i}: {error}")
            name_generation_failed = True
            continue
        link_path = os.path.join(watch_folder, link_name)
        desired_links[link_path] = target

    # A component-sizing failure is terminal for this requested set.  Return
    # before creating, replacing, or cleaning any links so the caller receives
    # a bounded Join-preparation failure without partial filesystem changes.
    if name_generation_failed:
        return result

    # Create/update desired links
    for link_path, target in desired_links.items():
        try:
            if os.path.islink(link_path):
                cur_real = _resolve_symlink_target(link_path)
                tgt_real = _real_path(target)
                if cur_real == tgt_real:
                    if debug_join_paths:
                        logger.debug(
                            "Join kept valid symlink: %s -> %s", link_path, cur_real,
                        )
                    result["kept"].append(link_path)
                else:
                    logger.debug(
                        "Join replaced wrong-target symlink: %s old=%s expected=%s",
                        link_path, cur_real, tgt_real,
                    )
                    os.unlink(link_path)
                    os.symlink(target, link_path)
                    result["updated"].append(link_path)
            elif os.path.exists(link_path):
                msg = f"path exists and is not symlink: {link_path}"
                logger.error("Join symlink non-symlink collision: %s", msg)
                result["errors"].append(msg)
                continue
            else:
                if debug_join_paths:
                    logger.debug(
                        "Join created symlink: %s -> %s",
                        link_path, _real_path(target),
                    )
                os.symlink(target, link_path)
                result["created"].append(link_path)

            result["selected_paths"].append(link_path)
        except Exception as e:
            result["errors"].append(f"{link_path}: {e}")

    if cleanup_stale:
        try:
            desired_set = set(desired_links.keys())
            for name in os.listdir(watch_folder):
                p = os.path.join(watch_folder, name)
                if p in desired_set:
                    continue
                if os.path.islink(p):
                    if not _is_dzll_owned_symlink_name(name):
                        continue
                    try:
                        mid = _mod_id_from_dzll_symlink_name(name)
                        target = _resolve_symlink_target(p)
                        os.unlink(p)
                        result["removed"].append(p)
                        logger.debug(
                            "Join removed stale DZLL symlink: %s mid=%s target=%s",
                            p, mid, target,
                        )
                    except Exception as e:
                        result["errors"].append(f"remove stale {p}: {e}")
        except Exception as e:
            result["errors"].append(f"cleanup stale failed: {e}")

    return result


def scan_installed_mods_in_watch_folder(watch_folder: str) -> List[str]:
    """
    Returns Linux paths of symlink entries in watch_folder (sorted).
    These become Local.json knownLocalMods after Proton path conversion.
    """
    watch_folder = _resolve_path(watch_folder)
    out: List[str] = []
    if not os.path.isdir(watch_folder):
        return out
    try:
        for name in sorted(os.listdir(watch_folder), key=lambda s: s.lower()):
            p = os.path.join(watch_folder, name)
            if os.path.islink(p):
                out.append(p)
    except Exception:
        return out
    return out
