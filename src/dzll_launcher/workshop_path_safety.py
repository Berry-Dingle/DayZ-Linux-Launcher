"""Narrow path guards for destructive Steam Workshop filesystem changes."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable, Literal


WorkshopLeafKind = Literal["directory", "file"]


def canonical_trusted_root(path) -> Path | None:
    """Resolve a caller-established library/Workshop root exactly once."""

    try:
        return Path(path).expanduser().resolve()
    except Exception:
        return None


def _clean_relative_parts(parts: Iterable[object]) -> tuple[str, ...] | None:
    cleaned: list[str] = []
    for raw_part in parts:
        part = str(raw_part)
        if (
            not part
            or part in (".", "..")
            or Path(part).is_absolute()
            or Path(part).name != part
        ):
            return None
        cleaned.append(part)
    return tuple(cleaned) if cleaned else None


def _safe_workshop_descendant(
    *,
    trusted_root,
    workshop_prefix: Iterable[object],
    relative_parts: Iterable[object],
    candidate,
    leaf_kind: WorkshopLeafKind,
) -> Path | None:
    """Validate one exact Workshop mutation path below an established root.

    The trusted root may itself have been reached through a symlink.  It is
    canonicalized before descendant checks begin.  Every existing component
    below it must be a real, non-symlink filesystem entry.
    """

    root = canonical_trusted_root(trusted_root)
    raw_prefix = tuple(workshop_prefix)
    prefix = _clean_relative_parts(raw_prefix) if raw_prefix else ()
    relative = _clean_relative_parts(relative_parts)
    if root is None or prefix is None or relative is None:
        return None
    if leaf_kind not in ("directory", "file"):
        return None

    workshop_root = root.joinpath(*prefix)
    expected = workshop_root.joinpath(*relative)

    try:
        candidate_lexical = Path(
            os.path.abspath(os.fspath(Path(candidate).expanduser()))
        )
    except Exception:
        return None
    if candidate_lexical != expected:
        return None

    current = root
    all_parts = (*prefix, *relative)
    for index, part in enumerate(all_parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            # No deeper component can currently exist once a real component is
            # absent.  A broken symlink is still observed by lstat and rejected.
            break
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return None
        is_leaf = index == len(all_parts) - 1
        if is_leaf:
            if leaf_kind == "directory" and not stat.S_ISDIR(mode):
                return None
            if leaf_kind == "file" and not stat.S_ISREG(mode):
                return None
        elif not stat.S_ISDIR(mode):
            return None

    try:
        candidate_resolved = candidate_lexical.resolve()
        expected_resolved = expected.resolve()
        workshop_resolved = workshop_root.resolve()
    except Exception:
        return None

    if candidate_resolved != expected_resolved:
        return None
    try:
        if not expected_resolved.is_relative_to(workshop_resolved):
            return None
    except (AttributeError, TypeError, ValueError):
        try:
            expected_resolved.relative_to(workshop_resolved)
        except ValueError:
            return None

    # Return the independently derived path, never an untrusted alias supplied
    # by a helper event or caller.
    return expected


def safe_native_workshop_mutation_path(
    library_root,
    candidate,
    *,
    relative_parts: Iterable[object],
    leaf_kind: WorkshopLeafKind,
) -> Path | None:
    """Validate a path below a canonical native Steam library root."""

    return _safe_workshop_descendant(
        trusted_root=library_root,
        workshop_prefix=("steamapps", "workshop"),
        relative_parts=relative_parts,
        candidate=candidate,
        leaf_kind=leaf_kind,
    )


def safe_configured_workshop_mutation_path(
    workshop_root,
    candidate,
    *,
    relative_parts: Iterable[object],
    leaf_kind: WorkshopLeafKind,
) -> Path | None:
    """Validate a path below SteamCMD's configured/effective Workshop root."""

    return _safe_workshop_descendant(
        trusted_root=workshop_root,
        workshop_prefix=(),
        relative_parts=relative_parts,
        candidate=candidate,
        leaf_kind=leaf_kind,
    )
