#!/usr/bin/env python3
"""Strict same-user process identity for DayZ and DayZ Launcher.

A supported identity is either the exact Windows process ``comm`` name, or an
exact target executable argv token corroborated by a Wine/Proton executable or
DayZ's Steam AppID in that process environment.  Target text in an unrelated
process command line is deliberately insufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .steam_native import DAYZ_APPID


_DAYZ_EXECUTABLES = frozenset({"dayz_x64.exe", "dayz.exe"})
_DAYZ_LAUNCHER_EXECUTABLES = frozenset({"dayzlauncher.exe"})
_WINE_PROCESS_EXECUTABLES = frozenset({
    "wine",
    "wine64",
    "wine-preloader",
    "wine64-preloader",
    "proton",
    "proton-waitforexitandrun",
})
_APP_ID_ENVIRONMENT_KEYS = frozenset({
    "SteamAppId",
    "SteamGameId",
    "STEAM_COMPAT_APP_ID",
})
_LINUX_COMM_LIMIT = 15


@dataclass(frozen=True)
class DayZProcessSnapshot:
    dayz_running: bool = False
    launcher_running: bool = False
    dayz_pids: tuple[int, ...] = ()
    launcher_pids: tuple[int, ...] = ()


def _token_basename(value: str) -> str:
    text = str(value or "").replace("\\", "/")
    return text.rsplit("/", 1)[-1].casefold() if text else ""


def _decode_argv(raw: bytes) -> tuple[str, ...]:
    if not raw or b"\x00" not in raw:
        return ()
    try:
        values = raw.split(b"\x00")
        return tuple(os.fsdecode(value) for value in values if value)
    except Exception:
        return ()


def _target_kind(argv: tuple[str, ...], comm: str) -> tuple[str, bool] | None:
    token_names = {_token_basename(token) for token in argv}
    token_kinds = set()
    if token_names & _DAYZ_EXECUTABLES:
        token_kinds.add("dayz")
    if token_names & _DAYZ_LAUNCHER_EXECUTABLES:
        token_kinds.add("launcher")

    comm_name = str(comm or "").casefold()
    comm_kinds = set()
    if comm_name in _DAYZ_EXECUTABLES:
        comm_kinds.add("dayz")
    if comm_name in {
        name[:_LINUX_COMM_LIMIT] for name in _DAYZ_LAUNCHER_EXECUTABLES
    }:
        comm_kinds.add("launcher")

    kinds = token_kinds | comm_kinds
    if len(kinds) != 1:
        return None
    kind = next(iter(kinds))
    return kind, kind in comm_kinds


def _has_dayz_app_environment(raw: bytes) -> bool:
    if not raw:
        return False
    expected = str(DAYZ_APPID)
    for entry in raw.split(b"\x00"):
        if b"=" not in entry:
            continue
        try:
            key_raw, value_raw = entry.split(b"=", 1)
            key = key_raw.decode("ascii", "strict")
        except Exception:
            continue
        if key not in _APP_ID_ENVIRONMENT_KEYS:
            continue
        try:
            value = value_raw.decode("ascii", "strict")
        except Exception:
            continue
        if value == expected:
            return True
    return False


def _process_start_time(raw: bytes) -> bytes | None:
    """Extract Linux /proc/<pid>/stat field 22 without trusting comm spacing."""

    try:
        comm_end = raw.rfind(b")")
        fields_after_comm = raw[comm_end + 1:].split()
        # fields_after_comm[0] is field 3 (state); field 22 is index 19.
        return fields_after_comm[19] if comm_end >= 0 and len(fields_after_comm) > 19 else None
    except Exception:
        return None


def _same_process(entry: Path, directory_before, start_time_before: bytes) -> bool:
    try:
        directory_after = entry.stat(follow_symlinks=False)
        start_time_after = _process_start_time((entry / "stat").read_bytes())
    except Exception:
        return False
    return bool(
        stat.S_ISDIR(directory_after.st_mode)
        and directory_after.st_dev == directory_before.st_dev
        and directory_after.st_ino == directory_before.st_ino
        and directory_after.st_uid == directory_before.st_uid
        and start_time_after == start_time_before
    )


def _classify_process(entry: Path, *, expected_uid: int) -> str | None:
    try:
        directory_before = entry.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_before.st_mode)
            or directory_before.st_uid != expected_uid
        ):
            return None
        start_time_before = _process_start_time((entry / "stat").read_bytes())
        if start_time_before is None:
            return None
        argv = _decode_argv((entry / "cmdline").read_bytes())
        if not argv:
            return None
        comm = (entry / "comm").read_text(
            encoding="utf-8", errors="strict",
        ).removesuffix("\n")
    except Exception:
        return None

    target = _target_kind(argv, comm)
    if target is None:
        return None
    kind, direct_comm_identity = target

    corroborated = direct_comm_identity
    if not corroborated:
        try:
            executable = os.readlink(entry / "exe")
        except Exception:
            executable = ""
        corroborated = _token_basename(executable) in _WINE_PROCESS_EXECUTABLES
    if not corroborated:
        try:
            corroborated = _has_dayz_app_environment(
                (entry / "environ").read_bytes()
            )
        except Exception:
            corroborated = False
    if not corroborated or not _same_process(
        entry, directory_before, start_time_before,
    ):
        return None
    return kind


def scan_dayz_processes(
        proc_root: Path | str = Path("/proc"), *,
        expected_uid: int | None = None) -> DayZProcessSnapshot:
    """Return one strict snapshot of supported DayZ-family processes."""

    root = Path(proc_root)
    uid = os.geteuid() if expected_uid is None else int(expected_uid)
    try:
        entries = list(root.iterdir())
    except Exception:
        return DayZProcessSnapshot()

    dayz_pids = []
    launcher_pids = []
    for entry in entries:
        name = str(getattr(entry, "name", ""))
        if not name.isdigit():
            continue
        kind = _classify_process(entry, expected_uid=uid)
        if kind == "dayz":
            dayz_pids.append(int(name))
        elif kind == "launcher":
            launcher_pids.append(int(name))

    dayz_pids.sort()
    launcher_pids.sort()
    return DayZProcessSnapshot(
        dayz_running=bool(dayz_pids),
        launcher_running=bool(launcher_pids),
        dayz_pids=tuple(dayz_pids),
        launcher_pids=tuple(launcher_pids),
    )
