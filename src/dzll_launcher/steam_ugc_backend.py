#!/usr/bin/env python3
"""
Steam Client UGC backend for DayZ Workshop items.

This is the required-mod preparation backend.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from .workshop_path_safety import safe_native_workshop_mutation_path


logger = logging.getLogger(__name__)


DAYZ_APPID = 221100
UGC_PREFLIGHT_TIMEOUT_S = 60.0
UGC_PREFLIGHT_RETRY_S = 3.0
UGC_PREFLIGHT_PROBE_TIMEOUT_S = 8.0
UGC_SUBSCRIBED_REFRESH_BATCH_TIMEOUT_S = 2.0
UGC_CANCEL_CLEANUP_BATCH_TIMEOUT_S = 2.0
UGC_DESTRUCTIVE_CLEANUP_SNAPSHOT_MAX_AGE_S = 300.0


class SteamLaunchPolicy(Enum):
    BACKEND_ALLOWED = "backend_allowed"
    WAIT_ONLY = "wait_only"
    REQUIRE_RUNNING = "require_running"


@dataclass(frozen=True)
class UGCSubscriptionSnapshot:
    """Authoritative subscription inventory captured by one native UGC query."""

    valid: bool
    requested_ids: frozenset[int]
    subscribed_ids: frozenset[int]
    states: dict[int, dict]
    logged_on: bool = False
    steam_id: int = 0
    native_attachment_verified: bool = False
    created_monotonic: float = 0.0

    def is_fresh_for_destructive_cleanup(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        """Return whether this in-process authority is current enough to delete."""

        try:
            captured = float(self.created_monotonic)
            now = (
                time.monotonic()
                if now_monotonic is None
                else float(now_monotonic)
            )
        except (TypeError, ValueError):
            return False
        age = now - captured
        return bool(
            captured > 0.0
            and age >= 0.0
            and age <= UGC_DESTRUCTIVE_CLEANUP_SNAPSHOT_MAX_AGE_S
        )

    def proves_unsubscribed(self, mod_id: int) -> bool:
        try:
            mid = int(mod_id)
        except Exception:
            return False
        state = self.states.get(mid) if isinstance(self.states, dict) else None
        return bool(
            self.valid
            and self.logged_on
            and self.steam_id > 0
            and self.native_attachment_verified
            # An empty subscription enumeration is indistinguishable from the
            # transient all-zero cache observed during native Steam handoff.
            and bool(self.subscribed_ids)
            and mid in self.requested_ids
            and isinstance(self.states, dict)
            and mid in self.states
            and mid not in self.subscribed_ids
            and isinstance(state, dict)
            and state.get("subscribed") is False
        )


@dataclass(frozen=True)
class UGCNativeReadiness:
    """One authenticated native SteamAPI/UGC readiness observation."""

    valid: bool
    steam_id: int = 0
    logged_on: bool = False
    native_attachment_verified: bool = False
    created_monotonic: float = 0.0


def _steam_launch_policy(value, allow_start_steam: bool) -> SteamLaunchPolicy:
    if isinstance(value, SteamLaunchPolicy):
        return value
    try:
        return SteamLaunchPolicy(str(value))
    except (TypeError, ValueError):
        return (
            SteamLaunchPolicy.BACKEND_ALLOWED
            if bool(allow_start_steam)
            else SteamLaunchPolicy.REQUIRE_RUNNING
        )


@dataclass
class UGCModSession:
    id: int
    was_subscribed_before: bool | None = None
    was_installed_before: bool = False
    subscribed_by_dzll_this_join: bool = False
    installed_now: bool = False
    needs_update: bool = False
    last_state_names: list[str] | None = None
    download_bytes: int = 0
    total_bytes: int = 0
    size_on_disk: int = 0
    downloading: bool = False
    download_pending: bool = False
    install_folder: str | None = None
    event_source: str = "initial"
    request_attempted: bool = False
    request_accepted: bool = False
    helper_subscribe_attempted: bool = False
    filesystem_normalized_missing: bool = False

    def update_from_item(self, event: dict, *, source: str | None = None) -> None:
        raw_source = str(source or event.get("event_source") or event.get("type") or "poll")
        self.event_source = raw_source if raw_source in ("initial", "request", "poll", "refresh", "final") else "poll"
        if self.event_source == "request":
            self.request_attempted = True
            self.request_accepted = bool(event.get("download_requested", False))
            subscribe_call_result = event.get("subscribe_call_result")
            if type(subscribe_call_result) is int:
                self.helper_subscribe_attempted = True
        self.installed_now = bool(event.get("installed", False))
        self.needs_update = bool(event.get("needs_update", False))
        self.last_state_names = list(event.get("state_names") or [])
        self.downloading = bool(event.get("downloading", False))
        self.download_pending = bool(event.get("download_pending", False))
        try:
            self.download_bytes = int(event.get("download_bytes") or 0)
        except Exception:
            self.download_bytes = 0
        try:
            self.total_bytes = int(event.get("total_bytes") or 0)
        except Exception:
            self.total_bytes = 0
        try:
            self.size_on_disk = int(event.get("size_on_disk") or 0)
        except Exception:
            self.size_on_disk = 0
        folder = event.get("install_folder")
        self.install_folder = str(folder) if folder else None
        self.filesystem_normalized_missing = bool(event.get("filesystem_normalized_missing", False))

    def event(self) -> dict:
        data = asdict(self)
        data["type"] = "session"
        data["installed"] = bool(self.installed_now)
        data["ready"] = ugc_item_ready(data)
        data["state_names"] = list(self.last_state_names or [])
        return data


def ugc_item_ready(item) -> bool:
    if isinstance(item, UGCModSession):
        return (
            bool(item.installed_now)
            and not bool(item.needs_update)
            and not bool(item.downloading)
            and not bool(item.download_pending)
        )
    event = dict(item or {})
    return (
        bool(event.get("installed", event.get("installed_now", False)))
        and not bool(event.get("needs_update", False))
        and not bool(event.get("downloading", False))
        and not bool(event.get("download_pending", False))
    )


def emit(event: dict) -> None:
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _dedupe_sorted_ids(mod_ids: Iterable[int]) -> list[int]:
    out = []
    seen = set()
    for raw in mod_ids or []:
        try:
            mid = int(raw)
        except Exception:
            continue
        if mid > 0 and mid not in seen:
            out.append(mid)
            seen.add(mid)
    out.sort()
    return out


def _strict_cleanup_workshop_item_id(value) -> int | None:
    """Return an exact Workshop identity suitable for cleanup authority."""

    if type(value) is not int:
        return None
    if value <= 0 or value > 0xFFFFFFFFFFFFFFFF:
        return None
    return value


def _strict_cleanup_workshop_item_ids(values) -> list[int]:
    """Deduplicate exact Workshop identities without coercing their values."""

    return sorted({
        item_id
        for value in values or []
        if (item_id := _strict_cleanup_workshop_item_id(value)) is not None
    })


def _helper_env(*, strict_native_attachment: bool = False) -> dict:
    env = dict(os.environ)
    for name in ("SteamAppId", "SteamGameId", "SteamOverlayGameId"):
        env.pop(name, None)
    if strict_native_attachment:
        for name in tuple(env):
            upper = str(name).upper()
            if (
                upper.startswith("FLATPAK_")
                or upper.startswith("PRESSURE_VESSEL_")
                or upper.startswith("STEAM_COMPAT_")
                or upper.startswith("STEAM_RUNTIME")
                or upper in {"LD_LIBRARY_PATH", "LD_PRELOAD"}
            ):
                env.pop(name, None)
        for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            if "com.valvesoftware.steam" in str(env.get(name, "")).casefold():
                env.pop(name, None)
    package_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = package_root if not current else f"{package_root}:{current}"
    return env


def _helper_cmd(command: str, *, appid: int, timeout: float | None, mod_ids: list[int]) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "dzll_launcher.steam_ugc_helper",
        command,
        "--appid",
        str(int(appid)),
    ]
    if timeout is not None and command in (
        "refresh-subscribed-state", "subscribe-download", "unsubscribe",
        "cancel-cleanup-unsubscribe",
    ):
        cmd.extend(["--timeout", str(int(max(0, timeout)))])
    cmd.extend(str(int(mid)) for mid in mod_ids)
    return cmd


def _log_event(progress_cb, message: str, **extra) -> None:
    diagnostic = (
        f"{message} {json.dumps(extra, sort_keys=True, default=str)}"
        if extra else message
    )
    if "failure-to-reap" in message.casefold():
        eprint(diagnostic)
    else:
        logger.debug("%s", diagnostic)
    event = {"type": "log", "message": message}
    event.update(extra)
    _progress(progress_cb, event)


class UGCHelperReapError(RuntimeError):
    """Raised when DZLL's own UGC helper cannot be confirmed exited."""

    def __init__(
        self,
        message: str,
        *,
        helper_process_confirmed_dead: bool = False,
        helper_process_may_be_alive: bool = True,
        reader_cleanup_only: bool = False,
        steamapi_shutdown_confirmed: bool = False,
        process=None,
        session=None,
    ) -> None:
        super().__init__(message)
        self.helper_process_confirmed_dead = bool(helper_process_confirmed_dead)
        self.helper_process_may_be_alive = bool(
            helper_process_may_be_alive and not helper_process_confirmed_dead
        )
        self.reader_cleanup_only = bool(reader_cleanup_only)
        self.steamapi_shutdown_confirmed = bool(steamapi_shutdown_confirmed)
        self._recovery_process = process
        self._recovery_session = session

    def bind_session(self, session) -> "UGCHelperReapError":
        """Retain the concrete owner needed for non-PID-based recovery."""

        self._recovery_session = session
        if self._recovery_process is None:
            self._recovery_process = getattr(session, "_proc", None)
        self.steamapi_shutdown_confirmed = bool(
            self.steamapi_shutdown_confirmed
            or getattr(session, "_shutdown_complete", False)
        )
        return self

    def refresh_process_confirmation(self) -> bool:
        """Poll the retained Process handle and record confirmed process death."""

        if self.helper_process_confirmed_dead:
            return True
        proc = self._recovery_process
        if proc is None:
            return False
        try:
            confirmed = proc.poll() is not None
        except Exception:
            confirmed = False
        if confirmed:
            self.helper_process_confirmed_dead = True
            self.helper_process_may_be_alive = False
        return confirmed

    def finalize_confirmed_recovery(self) -> bool:
        """Release retained local resources after Process-based death confirmation."""

        if not self.refresh_process_confirmation():
            return False
        session = self._recovery_session
        if session is not None:
            try:
                finalized = bool(session._finalize_confirmed_reap_recovery())
            except Exception as exc:
                logger.warning(
                    "Steam UGC helper exited but local recovery cleanup was incomplete: %s",
                    exc,
                )
            else:
                if finalized:
                    self._recovery_session = None
                    self._recovery_process = None
        return True


class UGCSessionError(RuntimeError):
    """Raised when the cooperative helper protocol fails closed."""


def _unconfirmed_helper_reap_error(message: str, proc) -> UGCHelperReapError:
    error = UGCHelperReapError(
        message,
        helper_process_may_be_alive=True,
        process=proc,
    )
    error.refresh_process_confirmation()
    return error


_ACTIVE_UGC_SESSION = threading.local()
_ACTIVE_UGC_SESSIONS_LOCK = threading.Lock()
_ACTIVE_UGC_SESSIONS: set = set()


class _NeverCancelled:
    @staticmethod
    def is_set() -> bool:
        return False


_CLEANUP_CANCEL_EVENT = _NeverCancelled()


def activate_ugc_session(session) -> None:
    if getattr(_ACTIVE_UGC_SESSION, "value", None) is not None:
        raise UGCSessionError("a Steam UGC session is already active on this worker")
    _ACTIVE_UGC_SESSION.value = session


def deactivate_ugc_session(session) -> bool:
    if getattr(_ACTIVE_UGC_SESSION, "value", None) is not session:
        return False
    _ACTIVE_UGC_SESSION.value = None
    return True


def register_owned_ugc_session(session) -> None:
    """Record a cooperative session whose resources DZLL still owns."""

    with _ACTIVE_UGC_SESSIONS_LOCK:
        _ACTIVE_UGC_SESSIONS.add(session)


def unregister_owned_ugc_session(session) -> None:
    """Forget a cooperative session after confirmed resource teardown."""

    with _ACTIVE_UGC_SESSIONS_LOCK:
        _ACTIVE_UGC_SESSIONS.discard(session)


def active_ugc_session():
    return getattr(_ACTIVE_UGC_SESSION, "value", None)


def close_active_ugc_sessions() -> list[str]:
    """Close and reap every cooperative helper currently owned by DZLL."""
    with _ACTIVE_UGC_SESSIONS_LOCK:
        sessions = list(_ACTIVE_UGC_SESSIONS)
    errors: list[str] = []
    for session in sessions:
        close_error = None
        try:
            session.close()
        except Exception as exc:
            close_error = exc
        try:
            resources_released = bool(
                session._owned_resources_release_confirmed()
            )
        except Exception:
            resources_released = False
        if resources_released:
            unregister_owned_ugc_session(session)
        if close_error is not None and resources_released:
            eprint(
                "[Steam UGC] Recovery retained helper shutdown diagnostic: "
                f"{close_error}"
            )
        elif close_error is not None:
            errors.append(str(close_error))
        elif not resources_released:
            errors.append("Steam UGC helper could not be confirmed reaped")
    with _ACTIVE_UGC_SESSIONS_LOCK:
        remaining_sessions = list(_ACTIVE_UGC_SESSIONS)
    if remaining_sessions and not errors:
        errors.append("Steam UGC helper cleanup remains incomplete")
    return errors


def _stop_helper_process(proc: subprocess.Popen, *, command: str, progress_cb=None) -> None:
    pid = int(getattr(proc, "pid", 0) or 0)
    if proc.poll() is not None:
        _log_event(
            progress_cb, "[Steam UGC] Helper already exited",
            command=command, helper_pid=pid, returncode=proc.poll(),
        )
        return
    stop_started = time.monotonic()
    try:
        proc.terminate()
    except Exception as exc:
        _log_event(
            progress_cb, "[Steam UGC] Helper terminate failed",
            command=command, helper_pid=pid, error=str(exc),
        )
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _log_event(
            progress_cb, "[Steam UGC] Helper terminate timed out; killing",
            command=command, helper_pid=pid,
        )
        try:
            proc.kill()
        except Exception as exc:
            _log_event(
                progress_cb, "[Steam UGC] Helper kill failed",
                command=command, helper_pid=pid, error=str(exc),
            )
        try:
            proc.wait(timeout=2.0)
        except Exception as exc:
            elapsed = time.monotonic() - stop_started
            _log_event(
                progress_cb, "[Steam UGC] Helper failure-to-reap",
                command=command, helper_pid=pid, elapsed=f"{elapsed:.3f}s",
                error=str(exc),
            )
            raise _unconfirmed_helper_reap_error(
                f"UGC helper pid {pid} for {command} could not be reaped", proc,
            ) from exc
    except Exception as exc:
        elapsed = time.monotonic() - stop_started
        _log_event(
            progress_cb, "[Steam UGC] Helper failure-to-reap",
            command=command, helper_pid=pid, elapsed=f"{elapsed:.3f}s",
            error=str(exc),
        )
        raise _unconfirmed_helper_reap_error(
            f"UGC helper pid {pid} for {command} could not be reaped", proc,
        ) from exc
    returncode = proc.poll()
    elapsed = time.monotonic() - stop_started
    if returncode is None:
        _log_event(
            progress_cb, "[Steam UGC] Helper failure-to-reap",
            command=command, helper_pid=pid, elapsed=f"{elapsed:.3f}s",
        )
        raise _unconfirmed_helper_reap_error(
            f"UGC helper pid {pid} for {command} remained alive after stop", proc,
        )
    _log_event(
        progress_cb, "[Steam UGC] Stopped helper subprocess",
        command=command, helper_pid=pid, returncode=returncode,
        cleanup_elapsed=f"{elapsed:.3f}s",
    )


class CooperativeUGCSession:
    """One sequential SteamAPI helper process for one preparation operation."""

    def __init__(self, *, appid: int = DAYZ_APPID, cancel_event=None, progress_cb=None):
        self.appid = int(appid)
        self.cancel_event = cancel_event
        self.progress_cb = progress_cb
        self._proc = None
        self._stdout_q: queue.Queue = queue.Queue()
        self._stderr_tail: list[str] = []
        self._stdout_thread = None
        self._stderr_thread = None
        self._request_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._close_requested = threading.Event()
        self._next_request_id = 0
        self._temp_dir = ""
        self._ready = False
        self._closed = False
        self._shutdown_complete = False
        self._stdin_closed = False
        self._read_pipes_closed = False
        self._temp_cleanup_complete = True
        self._owned_resources_released = False
        self._teardown_error = None
        self._fatal = False
        self._active_request_id = ""
        self._abandoned_request_ids: set[str] = set()

    @property
    def helper_pid(self) -> int:
        return int(getattr(self._proc, "pid", 0) or 0)

    @property
    def usable(self) -> bool:
        return bool(
            not self._closed
            and not self._close_requested.is_set()
            and not self._fatal
            and (self._proc is None or self._proc.poll() is None)
        )

    def _begin_active_command(self, request_id: str, command: str) -> None:
        with self._state_lock:
            if self._active_request_id:
                raise UGCSessionError(
                    "Steam UGC helper already has an active command"
                )
            self._active_request_id = str(request_id)

    def _finish_active_command(
        self,
        request_id: str,
        *,
        abandoned: bool = False,
    ) -> bool:
        with self._state_lock:
            if self._active_request_id != str(request_id):
                return False
            if abandoned:
                self._abandoned_request_ids.add(str(request_id))
            self._active_request_id = ""
            return True

    def _protocol_failure(self, message: str, *, raw=None) -> UGCSessionError:
        self._fatal = True
        if raw is not None:
            diagnostic = repr(str(raw))
            if len(diagnostic) > 240:
                diagnostic = diagnostic[:237] + "..."
            eprint(
                "[steam-ugc-backend][protocol] "
                f"helper_pid={self.helper_pid} rejected_frame={diagnostic}"
            )
        return UGCSessionError(message)

    def _start(self) -> None:
        if self._proc is not None:
            return
        cmd = _helper_cmd("session", appid=self.appid, timeout=None, mod_ids=[])
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_helper_env(),
        )
        _log_event(
            self.progress_cb, "[Steam UGC] Cooperative helper session started",
            command="session", helper_pid=self.helper_pid,
        )

        def read_stdout():
            try:
                for line in self._proc.stdout or []:
                    self._stdout_q.put(str(line or ""))
            finally:
                self._stdout_q.put(None)
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Cooperative protocol reader exited",
                    command="session", helper_pid=self.helper_pid,
                )

        def read_stderr():
            try:
                for line in self._proc.stderr or []:
                    text = str(line or "").rstrip()
                    if text:
                        self._stderr_tail.append(text)
                        del self._stderr_tail[:-16]
            except Exception:
                pass
            finally:
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Cooperative stderr reader exited",
                    command="session", helper_pid=self.helper_pid,
                )

        self._stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise UGCSessionError("Steam UGC helper session is not running")
        payload = json.dumps(message, sort_keys=True, separators=(",", ":"))
        with self._write_lock:
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()

    def _next_id(self, prefix: str) -> str:
        self._next_request_id += 1
        return f"{prefix}-{self._next_request_id}"

    def _decode_event(self, raw) -> dict:
        if raw is None:
            raise self._protocol_failure(
                "Steam UGC helper closed its protocol stream"
            )
        if not isinstance(raw, str) or not raw.endswith("\n"):
            raise self._protocol_failure(
                "Steam UGC helper ended with a partial protocol message",
                raw=raw,
            )
        try:
            event = json.loads(raw[:-1])
        except Exception as exc:
            raise self._protocol_failure(
                "Steam UGC helper emitted malformed protocol data",
                raw=raw,
            ) from exc
        if not isinstance(event, dict):
            raise self._protocol_failure(
                "Steam UGC helper emitted a non-object protocol event",
                raw=raw,
            )
        if event.get("type") == "session_starting":
            self._temp_dir = str(event.get("temp_dir") or "")
            self._temp_cleanup_complete = not bool(self._temp_dir)
        if event.get("type") == "session_ready":
            self._ready = True
        if event.get("type") == "fatal_session_error":
            self._fatal = True
            raise UGCSessionError(str(event.get("error") or "fatal Steam UGC session error"))
        return event

    def _event(self, timeout: float) -> dict:
        try:
            raw = self._stdout_q.get(timeout=max(0.01, float(timeout)))
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for Steam UGC helper protocol") from exc
        return self._decode_event(raw)

    def _close_stdin(self) -> None:
        if self._stdin_closed:
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            self._stdin_closed = True
            return
        with self._write_lock:
            proc.stdin.close()
            self._stdin_closed = True
        _log_event(
            self.progress_cb,
            "[Steam UGC] Cooperative helper stdin closed",
            command="session", helper_pid=self.helper_pid,
        )

    def _close_read_pipes(self) -> None:
        if self._read_pipes_closed:
            return
        proc = self._proc
        errors = []
        for stream in (
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
        ):
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise UGCSessionError(
                "; ".join(str(error) for error in errors)
            )
        self._read_pipes_closed = True

    def _wait_until_ready(self, *, timeout: float | None, cancel_event=None) -> bool:
        if self._ready:
            return True
        deadline = (
            time.monotonic() + max(0.0, float(timeout))
            if timeout is not None else None
        )
        while not self._ready:
            selected_cancel = cancel_event if cancel_event is not None else self.cancel_event
            if self._close_requested.is_set():
                return False
            if selected_cancel is not None and selected_cancel.is_set():
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            try:
                event = self._event(0.1)
            except TimeoutError:
                if self._proc.poll() is not None:
                    raise UGCSessionError(
                        "Steam UGC helper exited before the session became ready"
                    )
                continue
            event_type = str(event.get("type") or "")
            if event_type in ("session_starting", "session_waiting", "session_ready"):
                continue
            raise UGCSessionError(
                f"unexpected Steam UGC startup event: {event_type or '<missing>'}"
            )
        return True

    def run_command(
        self,
        command: str,
        *,
        timeout: float | None,
        mod_ids: list[int],
        cancel_event=None,
        cancel_cleanup_ids=None,
        on_event=None,
        progress_cb=None,
    ) -> tuple[bool, int | None]:
        command_name = {
            "state": "query_state",
            "refresh-subscribed-state": "refresh_subscribed_state",
            "subscribe-download": "subscribe_download",
            "unsubscribe": "unsubscribe",
        }.get(str(command))
        if command_name is None:
            raise UGCSessionError(f"unsupported cooperative command: {command}")
        command_item_ids = _dedupe_sorted_ids(mod_ids)
        command_item_id_set = set(command_item_ids)
        approved_cancel_cleanup_ids = (
            [
                item_id for item_id in _strict_cleanup_workshop_item_ids(
                    cancel_cleanup_ids or [],
                )
                if item_id in command_item_id_set
            ]
            if command_name == "subscribe_download" else []
        )
        with self._request_lock:
            if self._closed or self._close_requested.is_set():
                raise UGCSessionError("Steam UGC helper session is closed")
            if self._fatal:
                raise UGCSessionError("Steam UGC helper session has failed")
            request_id = ""
            try:
                self._start()
                if not self._wait_until_ready(
                    timeout=timeout,
                    cancel_event=cancel_event,
                ):
                    return False, self._proc.poll()
                if self._close_requested.is_set():
                    return False, self._proc.poll()
                request_id = self._next_id(command_name)
                self._begin_active_command(request_id, command_name)
                self._send(
                    {
                        "command": command_name,
                        "request_id": request_id,
                        "item_ids": command_item_ids,
                        "timeout": float(timeout if timeout is not None else 120.0),
                    }
                )
                deadline = (
                    time.monotonic() + float(timeout) + (
                        1.0 if command_name == "refresh_subscribed_state" else 0.0
                    )
                    if timeout is not None else None
                )
                accepted = False
                cancel_sent = False
                cancel_id = ""
                cancel_reason = ""
                cancel_acknowledged = False
                cancelled_command_result = None
                while True:
                    selected_cancel = (
                        cancel_event if cancel_event is not None else self.cancel_event
                    )
                    cancellation_requested = bool(
                        self._close_requested.is_set()
                        or (
                            selected_cancel is not None
                            and selected_cancel.is_set()
                        )
                    )
                    if cancellation_requested and not cancel_sent:
                        cancel_id = self._next_id("cancel")
                        cancel_message = {
                            "command": "cancel",
                            "request_id": cancel_id,
                            "target_request_id": request_id,
                        }
                        if approved_cancel_cleanup_ids:
                            cancel_message["cleanup_item_ids"] = (
                                approved_cancel_cleanup_ids
                            )
                        self._send(cancel_message)
                        cancel_sent = True
                        cancel_reason = (
                            "shutdown" if self._close_requested.is_set() else "user"
                        )
                        deadline = time.monotonic() + 5.0
                    if deadline is not None and time.monotonic() >= deadline and not cancel_sent:
                        cancel_id = self._next_id("timeout-cancel")
                        cancel_message = {
                            "command": "cancel",
                            "request_id": cancel_id,
                            "target_request_id": request_id,
                        }
                        if approved_cancel_cleanup_ids:
                            cancel_message["cleanup_item_ids"] = (
                                approved_cancel_cleanup_ids
                            )
                        self._send(cancel_message)
                        cancel_sent = True
                        cancel_reason = "timeout"
                        deadline = time.monotonic() + 5.0
                    if deadline is not None and time.monotonic() >= deadline and cancel_sent:
                        missing = []
                        if not cancel_acknowledged:
                            missing.append("cancellation acknowledgement")
                        if cancelled_command_result is None:
                            missing.append("cancelled command result")
                        raise UGCSessionError(
                            "cooperative helper did not complete "
                            f"{cancel_reason} for {command_name}: "
                            f"missing {', '.join(missing)}"
                        )
                    try:
                        event = self._event(0.1)
                    except TimeoutError:
                        if self._proc.poll() is not None:
                            raise UGCSessionError(
                                f"Steam UGC helper exited during {command_name}"
                            )
                        continue
                    event_request_id = event.get("request_id")
                    event_type = str(event.get("type") or "")
                    if event_request_id not in (request_id, cancel_id):
                        raise UGCSessionError(
                            "Steam UGC helper response request_id did not match "
                            "the active command"
                        )
                    if event_request_id == cancel_id:
                        if event_type != "cancellation_ack":
                            raise UGCSessionError("unexpected cancellation response")
                        if event.get("target_request_id") not in (None, request_id):
                            raise UGCSessionError(
                                "cancellation acknowledgement target_request_id "
                                "did not match the active command"
                            )
                        if cancel_acknowledged:
                            raise UGCSessionError(
                                "duplicate cancellation acknowledgement"
                            )
                        cancel_acknowledged = True
                        if cancelled_command_result is not None:
                            self._finish_active_command(request_id)
                            return cancelled_command_result, self._proc.poll()
                        continue
                    if event_type == "command_accepted":
                        if accepted:
                            raise UGCSessionError(
                                "duplicate command acceptance from Steam UGC helper"
                            )
                        accepted = True
                    elif not accepted:
                        raise UGCSessionError(
                            "Steam UGC helper emitted command data before acceptance"
                        )
                    if callable(on_event):
                        on_event(event)
                    if event_type == "recoverable_error":
                        self._finish_active_command(request_id)
                        return False, self._proc.poll()
                    if event_type == "command_result":
                        result = bool(event.get("ok", False))
                        if cancel_sent:
                            if cancelled_command_result is not None:
                                raise UGCSessionError(
                                    "duplicate command result from Steam UGC helper"
                                )
                            cancelled_command_result = result
                            if cancel_acknowledged:
                                self._finish_active_command(request_id)
                                return result, self._proc.poll()
                            continue
                        self._finish_active_command(request_id)
                        return result, self._proc.poll()
                    if event_type not in (
                        "command_accepted", "item", "request",
                    ):
                        raise UGCSessionError(
                            "unexpected Steam UGC helper event: "
                            f"{event_type or '<missing>'}"
                        )
            except Exception:
                self._fatal = True
                if request_id:
                    self._finish_active_command(request_id, abandoned=True)
                raise

    def _cleanup_temp_dir(self) -> None:
        path_text = str(self._temp_dir or "")
        if not path_text:
            self._temp_cleanup_complete = True
            return
        path = Path(path_text)
        temp_root = Path(tempfile.gettempdir()).resolve()
        valid = (
            path.is_absolute()
            and path.parent.resolve() == temp_root
            and path.name.startswith("dzll_steam_ugc_")
        )
        if not valid:
            raise UGCSessionError("helper reported an unsafe temporary AppID path")
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise UGCSessionError("temporary Steam AppID directory survived session cleanup")
        self._temp_cleanup_complete = True

    def _mark_owned_resources_released_locked(self) -> bool:
        if self._owned_resources_released:
            return True
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    return False
            except Exception:
                return False
            if not self._stdin_closed or not self._read_pipes_closed:
                return False
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                return False
        if not self._temp_cleanup_complete:
            return False
        self._owned_resources_released = True
        return True

    def _owned_resources_release_confirmed(self) -> bool:
        """Return authoritative process/reader/local cleanup completion."""

        with self._close_lock:
            return bool(self._owned_resources_released)

    def _finalize_owned_recovery_if_confirmed_locked(self) -> bool:
        if self._owned_resources_released:
            return True
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    return False
            except Exception:
                return False
        errors = []
        for cleanup in (self._close_stdin, self._close_read_pipes):
            try:
                cleanup()
            except Exception as exc:
                errors.append(exc)
        surviving_readers = []
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.0)
                if thread.is_alive():
                    surviving_readers.append(thread)
        try:
            self._cleanup_temp_dir()
        except Exception as exc:
            errors.append(exc)
        if surviving_readers:
            errors.append(UGCSessionError(
                "Steam UGC helper reader cleanup remains incomplete"
            ))
        if errors:
            raise UGCSessionError("; ".join(str(error) for error in errors))
        return self._mark_owned_resources_released_locked()

    def close(self) -> None:
        # Signal first so an in-flight command observes cancellation, then wait
        # for its exactly-once finalization before beginning shutdown.
        self._close_requested.set()
        try:
            with self._close_lock:
                if self._owned_resources_released:
                    return
                if self._closed:
                    try:
                        finalized = self._finalize_owned_recovery_if_confirmed_locked()
                    except Exception as exc:
                        self._teardown_error = exc
                        raise
                    if finalized:
                        self._teardown_error = None
                        return
                    if self._teardown_error is not None:
                        raise self._teardown_error
                    raise UGCSessionError(
                        "Steam UGC session resource teardown is not confirmed"
                    )
                with self._request_lock:
                    if self._closed:
                        return
                    self._closed = True
                    try:
                        self._close_session()
                    except UGCHelperReapError as exc:
                        self._teardown_error = exc.bind_session(self)
                        self._mark_owned_resources_released_locked()
                        raise
                    except Exception as exc:
                        self._teardown_error = exc
                        self._mark_owned_resources_released_locked()
                        raise
                    if not self._mark_owned_resources_released_locked():
                        self._teardown_error = UGCSessionError(
                            "Steam UGC session resource teardown is not confirmed"
                        )
                        raise self._teardown_error
                    self._teardown_error = None
        finally:
            if self._owned_resources_release_confirmed():
                unregister_owned_ugc_session(self)

    def _finalize_confirmed_reap_recovery(self) -> bool:
        """Best-effort local cleanup after the retained Process is confirmed dead."""

        try:
            with self._close_lock:
                try:
                    finalized = self._finalize_owned_recovery_if_confirmed_locked()
                except Exception as exc:
                    self._teardown_error = exc
                    raise
                if finalized:
                    self._teardown_error = None
                return bool(finalized)
        finally:
            if self._owned_resources_release_confirmed():
                unregister_owned_ugc_session(self)

    def _close_session(self) -> None:
        proc = self._proc
        if proc is None:
            return
        shutdown_error = None
        shutdown_acknowledged = False
        escalated = False
        escalation_reason = ""
        request_id = ""

        def process_shutdown_event(event: dict) -> None:
            nonlocal shutdown_acknowledged
            event_type = str(event.get("type") or "")
            event_request_id = event.get("request_id")
            if str(event_request_id) in self._abandoned_request_ids:
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Quarantined late command event",
                    command="session", helper_pid=self.helper_pid,
                    request_id=event_request_id,
                    event_type=event_type or "<missing>",
                )
                return
            if (
                event_request_id is None
                and event_type in {
                    "session_starting", "session_waiting", "session_ready",
                }
            ):
                return
            if event_type == "command_accepted":
                if event_request_id != request_id:
                    raise UGCSessionError(
                        "shutdown acknowledgement request_id did not match"
                    )
                shutdown_acknowledged = True
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Cooperative shutdown acknowledged",
                    command="session", helper_pid=self.helper_pid,
                    request_id=request_id,
                )
                return
            if event_type == "shutdown_complete":
                if event_request_id != request_id:
                    raise UGCSessionError(
                        "shutdown_complete request_id did not match"
                    )
                if not bool(event.get("ok", False)):
                    raise UGCSessionError(
                        str(event.get("error") or
                            "helper reported failed shutdown cleanup")
                    )
                if not bool(event.get("steamapi_shutdown", False)):
                    raise UGCSessionError(
                        "helper did not confirm SteamAPI shutdown"
                    )
                if not bool(event.get("temp_cleanup", False)):
                    raise UGCSessionError(
                        "helper did not confirm temporary AppID cleanup"
                    )
                self._shutdown_complete = True
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Cooperative shutdown complete",
                    command="session", helper_pid=self.helper_pid,
                    request_id=request_id,
                    steamapi_shutdown=True, temp_cleanup=True,
                )
                return
            raise UGCSessionError(
                f"unexpected event during helper shutdown: "
                f"{event_type or '<missing>'}"
            )

        if proc.poll() is None:
            request_id = self._next_id("shutdown")
            try:
                self._send({"command": "shutdown", "request_id": request_id})
                _log_event(
                    self.progress_cb,
                    "[Steam UGC] Cooperative shutdown command sent",
                    command="session", helper_pid=self.helper_pid,
                    request_id=request_id,
                )
                # No more commands are valid after shutdown. Closing the parent's
                # write end also releases the helper's blocking stdin reader so
                # Python can finish interpreter shutdown naturally.
                self._close_stdin()
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not self._shutdown_complete:
                    try:
                        event = self._event(0.1)
                    except TimeoutError:
                        if proc.poll() is not None and self._stdout_q.empty():
                            break
                        continue
                    process_shutdown_event(event)
            except Exception as exc:
                shutdown_error = exc
        try:
            self._close_stdin()
        except Exception as exc:
            if shutdown_error is None:
                shutdown_error = exc
        if self._shutdown_complete and proc.poll() is None:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                escalation_reason = (
                    "helper acknowledged clean shutdown but did not exit "
                    "within the natural-exit grace period"
                )
        elif proc.poll() is None:
            escalation_reason = (
                f"shutdown handshake failed: {shutdown_error}"
                if shutdown_error is not None
                else "helper did not emit shutdown_complete"
            )
        if proc.poll() is None:
            escalated = True
            _log_event(
                self.progress_cb,
                "[Steam UGC] Escalating helper shutdown",
                command="session", helper_pid=self.helper_pid,
                reason=escalation_reason,
            )
            _stop_helper_process(proc, command="session", progress_cb=self.progress_cb)
        try:
            proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            escalated = True
            escalation_reason = "helper remained alive after initial reap wait"
            _log_event(
                self.progress_cb,
                "[Steam UGC] Escalating helper shutdown",
                command="session", helper_pid=self.helper_pid,
                reason=escalation_reason,
            )
            _stop_helper_process(proc, command="session", progress_cb=self.progress_cb)
        _log_event(
            self.progress_cb,
            "[Steam UGC] Cooperative helper process exited naturally"
            if not escalated else
            "[Steam UGC] Cooperative helper process exited after escalation",
            command="session", helper_pid=self.helper_pid,
            returncode=proc.poll(),
        )
        surviving_readers = []
        for name, thread in (
            ("protocol", self._stdout_thread),
            ("stderr", self._stderr_thread),
        ):
            if thread is not None:
                thread.join(timeout=1.0)
                if thread.is_alive():
                    surviving_readers.append(name)
                    continue
                _log_event(
                    self.progress_cb,
                    f"[Steam UGC] Cooperative {name} reader exit confirmed",
                    command="session", helper_pid=self.helper_pid,
                )
        if (
            shutdown_error is None
            and not self._shutdown_complete
            and not escalated
            and bool(request_id)
            and self._stdout_thread is not None
            and not self._stdout_thread.is_alive()
        ):
            try:
                while not self._shutdown_complete:
                    raw = self._stdout_q.get_nowait()
                    process_shutdown_event(self._decode_event(raw))
            except queue.Empty:
                pass
            except Exception as exc:
                shutdown_error = exc
        self._close_read_pipes()
        cleanup_error = None
        try:
            self._cleanup_temp_dir()
        except Exception as exc:
            cleanup_error = exc
        if cleanup_error is None:
            _log_event(
                self.progress_cb,
                "[Steam UGC] Temporary AppID cleanup confirmed",
                command="session", helper_pid=self.helper_pid,
                temp_dir=self._temp_dir,
            )
        _log_event(
            self.progress_cb, "[Steam UGC] Cooperative helper session exited",
            command="session", helper_pid=self.helper_pid, returncode=proc.poll(),
            shutdown_complete=bool(self._shutdown_complete),
            shutdown_acknowledged=bool(shutdown_acknowledged),
        )
        if surviving_readers:
            reader_names = ", ".join(surviving_readers)
            raise UGCHelperReapError(
                f"Steam UGC helper {reader_names} reader thread survived shutdown",
                helper_process_confirmed_dead=True,
                helper_process_may_be_alive=False,
                reader_cleanup_only=True,
                steamapi_shutdown_confirmed=bool(self._shutdown_complete),
                process=proc,
                session=self,
            )
        if cleanup_error is not None:
            raise cleanup_error
        if shutdown_error is not None:
            raise UGCSessionError(f"cooperative helper shutdown failed: {shutdown_error}")
        if escalated or proc.returncode != 0 or not self._shutdown_complete:
            for line in self._stderr_tail:
                eprint(f"[steam-ugc-backend][session-stderr] {line}")
            raise UGCSessionError(
                "Steam UGC helper session did not confirm clean SteamAPI shutdown"
                + (f": {escalation_reason}" if escalation_reason else "")
            )


def _run_helper_json_lines(
    command: str,
    *,
    appid: int,
    timeout: float | None,
    mod_ids: list[int],
    cancel_event=None,
    cancel_cleanup_ids=None,
    on_event: Callable[[dict], None] | None = None,
    progress_cb=None,
    strict_native_environment: bool = False,
) -> tuple[bool, int | None]:
    session = None if strict_native_environment else active_ugc_session()
    if session is not None:
        return session.run_command(
            command,
            timeout=timeout,
            mod_ids=mod_ids,
            cancel_event=cancel_event,
            cancel_cleanup_ids=cancel_cleanup_ids,
            on_event=on_event,
            progress_cb=progress_cb,
        )
    cmd = _helper_cmd(command, appid=appid, timeout=timeout, mod_ids=mod_ids)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_helper_env(
                strict_native_attachment=strict_native_environment,
            ),
        )
    except OSError as exc:
        eprint(f"[steam-ugc-backend] failed to start helper: {exc}")
        return False, None
    helper_pid = int(getattr(proc, "pid", 0) or 0)
    _log_event(
        progress_cb, "[Steam UGC] Helper subprocess started",
        command=command, helper_pid=helper_pid,
    )

    stdout_q: queue.Queue[str] = queue.Queue()
    stderr_tail: list[str] = []

    def read_stdout():
        try:
            for line in proc.stdout or []:
                stdout_q.put(str(line or ""))
        except Exception:
            pass

    def read_stderr():
        try:
            for line in proc.stderr or []:
                text = str(line or "").rstrip()
                if text:
                    stderr_tail.append(text)
                    del stderr_tail[:-8]
        except Exception:
            pass

    stdout_t = threading.Thread(target=read_stdout, daemon=True)
    stderr_t = threading.Thread(target=read_stderr, daemon=True)
    stdout_t.start()
    stderr_t.start()

    deadline = (
        time.monotonic() + float(timeout) + (
            1.0 if command in (
                "refresh-subscribed-state", "cancel-cleanup-unsubscribe",
            ) else 0.0
        )
        if timeout is not None else None
    )
    ok = False
    done_seen = False

    def process_protocol_line(line) -> None:
        nonlocal done_seen, ok
        raw = str(line or "").strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except Exception:
            eprint(f"[steam-ugc-backend] ignoring malformed helper output: {raw[:160]}")
            return
        if callable(on_event):
            on_event(event)
        if event.get("type") == "done":
            done_seen = True
            ok = bool(event.get("ok", False))

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _log_event(progress_cb, "[Steam UGC] Cancel requested", command=command)
                _stop_helper_process(proc, command=command, progress_cb=progress_cb)
                return False, proc.poll()

            if deadline is not None and time.monotonic() >= deadline:
                _stop_helper_process(proc, command=command, progress_cb=progress_cb)
                eprint(f"[steam-ugc-backend] helper {command} timed out")
                return False, proc.poll()

            try:
                line = stdout_q.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and stdout_q.empty():
                    break
                continue

            process_protocol_line(line)

        rc = proc.poll()
        _log_event(
            progress_cb, "[Steam UGC] Helper subprocess exited",
            command=command, helper_pid=helper_pid, returncode=rc,
        )
        stdout_reader_finished = False
        try:
            stdout_t.join(timeout=0.5)
            stdout_reader_finished = not stdout_t.is_alive()
        except Exception:
            stdout_reader_finished = False
        try:
            stderr_t.join(timeout=0.5)
        except Exception:
            pass
        if stdout_reader_finished:
            while True:
                try:
                    process_protocol_line(stdout_q.get_nowait())
                except queue.Empty:
                    break
        if rc != 0 or not done_seen or not stdout_reader_finished:
            for line in stderr_tail:
                eprint(f"[steam-ugc-backend][helper-stderr] {line}")
            return False, rc
        return bool(ok), rc
    finally:
        if proc.poll() is None:
            _stop_helper_process(proc, command=command, progress_cb=progress_cb)


def _cancel_aware_wait(cancel_event, timeout: float) -> bool:
    """Wait up to timeout; return False immediately when cancellation is set."""
    duration = max(0.0, float(timeout))
    if duration <= 0:
        return not bool(cancel_event is not None and cancel_event.is_set())
    if cancel_event is None:
        time.sleep(duration)
        return True
    return not bool(cancel_event.wait(timeout=duration))


def _progress(progress_cb, event: dict) -> None:
    if callable(progress_cb):
        try:
            progress_cb(event)
        except Exception:
            pass


def _ugc_preflight_event(progress_cb, message: str, *, ok=None, reason: str = "", error: bool = False) -> None:
    event = {
        "backend": "steam_ugc",
        "type": "preflight",
        "message": str(message or ""),
    }
    if ok is not None:
        event["ok"] = bool(ok)
    if reason:
        event["reason"] = str(reason)
    if error:
        event["error"] = True
    _progress(progress_cb, event)


def _supported_native_steam_mutation_state():
    try:
        from .steam_native import SteamClientState, detect_steam_client_state

        state = detect_steam_client_state()
        return state is SteamClientState.NATIVE, state
    except Exception:
        return False, None


def _run_ugc_native_steam_preflight(
    ids: list[int],
    *,
    appid: int,
    cancel_event=None,
    progress_cb=None,
    allow_start_steam: bool = True,
    launch_policy=None,
    timeout_s: float = UGC_PREFLIGHT_TIMEOUT_S,
) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        _ugc_preflight_event(
            progress_cb, "Steam startup cancelled.",
            ok=False, reason="cancelled", error=True,
        )
        return False

    try:
        from .steam_native import is_flatpak_steam_running, is_native_steam_running, resolve_native_steam_cmd
    except Exception as exc:
        message = f"Steam preflight failed: {exc}"
        _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_native_unavailable", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    try:
        if is_flatpak_steam_running():
            message = "Flatpak Steam is running. DZLL UGC install requires native Steam."
            _ugc_preflight_event(progress_cb, message, ok=False, reason="flatpak_steam_running", error=True)
            eprint(f"[Steam UGC] {message}")
            return False
    except Exception as exc:
        message = f"Could not check Steam process type: {exc}"
        _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_process_check_failed", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    steam_cmd = resolve_native_steam_cmd()
    if not steam_cmd:
        message = "Native Steam executable was not found. DZLL UGC install cannot continue."
        _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_not_found", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    try:
        native_running = is_native_steam_running()
    except Exception:
        native_running = False

    policy = _steam_launch_policy(launch_policy, allow_start_steam)
    if not native_running:
        if policy is SteamLaunchPolicy.REQUIRE_RUNNING:
            message = "Native Steam is not running. Start Steam before joining, or enable Start Steam on Join."
            _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_not_running", error=True)
            eprint(f"[Steam UGC] {message}")
            return False
        if policy is SteamLaunchPolicy.BACKEND_ALLOWED:
            _ugc_preflight_event(progress_cb, "Starting Steam...")
            if cancel_event is not None and cancel_event.is_set():
                _ugc_preflight_event(
                    progress_cb, "Steam startup cancelled.",
                    ok=False, reason="cancelled", error=True,
                )
                return False
            try:
                subprocess.Popen(
                    [steam_cmd, "-silent"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as exc:
                message = f"Failed to start native Steam: {exc}"
                _ugc_preflight_event(progress_cb, message, ok=False, reason="native_steam_start_failed", error=True)
                eprint(f"[Steam UGC] {message}")
                return False
        else:
            _ugc_preflight_event(progress_cb, "Waiting for Steam...")
    else:
        _ugc_preflight_event(progress_cb, "Checking Steam...")

    probe_ids = ids[:1] if ids else []
    if not probe_ids:
        message = "No Workshop item was available for Steam readiness probing."
        _ugc_preflight_event(progress_cb, message, ok=False, reason="no_probe_item", error=True)
        eprint(f"[Steam UGC] {message}")
        return False

    deadline = time.monotonic() + max(1.0, float(timeout_s))
    last_message_at = 0.0
    attempt = 0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            _ugc_preflight_event(progress_cb, "Steam startup cancelled.", ok=False, reason="cancelled", error=True)
            return False

        now = time.monotonic()
        if now - last_message_at >= UGC_PREFLIGHT_RETRY_S:
            try:
                native_running = is_native_steam_running()
            except Exception:
                native_running = False
            _ugc_preflight_event(progress_cb, "Waiting for Steam login..." if native_running else "Waiting for Steam...")
            last_message_at = now

        remaining = max(0.1, deadline - time.monotonic())
        probe_timeout = min(UGC_PREFLIGHT_PROBE_TIMEOUT_S, remaining)
        attempt += 1
        ok, _rc = _run_helper_json_lines(
            "state",
            appid=int(appid),
            timeout=probe_timeout,
            mod_ids=probe_ids,
            cancel_event=cancel_event,
            on_event=None,
            progress_cb=None,
        )
        if ok:
            _ugc_preflight_event(progress_cb, "Steam is ready.", ok=True, reason="ready")
            return True

        sleep_for = min(UGC_PREFLIGHT_RETRY_S, max(0.0, deadline - time.monotonic()))
        if sleep_for > 0 and not _cancel_aware_wait(cancel_event, sleep_for):
            _ugc_preflight_event(
                progress_cb, "Steam startup cancelled.",
                ok=False, reason="cancelled", error=True,
            )
            return False

    message = "Steam did not become ready. Please make sure native Steam is running and logged in."
    _ugc_preflight_event(progress_cb, message, ok=False, reason="steam_not_ready", error=True)
    eprint(f"[Steam UGC] {message}")
    return False


def _normalize_ugc_snapshot(event: dict) -> dict:
    normalized = dict(event or {})
    normalized.setdefault("filesystem_normalized_missing", False)
    if not bool(normalized.get("installed", False)):
        return normalized

    install_folder = str(normalized.get("install_folder") or "").strip()
    if not install_folder:
        return normalized

    installed_dir_exists = False
    try:
        path = Path(install_folder).expanduser()
        installed_dir_exists = path.is_dir() and not path.is_symlink()
    except Exception:
        installed_dir_exists = False

    if installed_dir_exists:
        return normalized

    normalized["installed"] = False
    normalized["filesystem_normalized_missing"] = True
    normalized["install_folder"] = None
    normalized["size_on_disk"] = 0
    try:
        state_names = [str(name) for name in (normalized.get("state_names") or []) if str(name) != "Installed"]
        normalized["state_names"] = state_names
    except Exception:
        normalized["state_names"] = []
    return normalized


def _cache_ugc_state(state_by_id: dict[int, dict], *, names_by_id=None) -> None:
    if not state_by_id:
        return
    try:
        from .mod_metadata import upsert_many_from_ugc_state

        normalized = {int(mid): _normalize_ugc_snapshot(state) for mid, state in (state_by_id or {}).items()}
        upsert_many_from_ugc_state(normalized, names_by_id=names_by_id)
    except Exception as exc:
        eprint(f"[Steam UGC] Metadata cache update failed: {exc}")


def _cleanup_subscriptions(
    sessions: dict[int, UGCModSession],
    *,
    appid: int,
    progress_cb=None,
    only_ids=None,
) -> None:
    cleanup_started = time.monotonic()
    allowed_ids = (
        set(_strict_cleanup_workshop_item_ids(only_ids))
        if only_ids is not None else None
    )
    cleanup_ids = sorted(
        mid
        for mid, session in sessions.items()
        if session.subscribed_by_dzll_this_join
        and session.was_subscribed_before is False
        and session.helper_subscribe_attempted
        and not session.installed_now
        and (allowed_ids is None or mid in allowed_ids)
    )
    _log_event(progress_cb, f"[Steam UGC] Cleanup unsubscribe ids: {cleanup_ids}", cleanup_ids=cleanup_ids)
    if not cleanup_ids:
        _progress(progress_cb, {"type": "cleanup", "ok": True, "unsubscribed": [], "skipped": True})
        _log_event(
            progress_cb, "[Steam UGC] Cancel cleanup complete",
            ok=True, cleanup_ids=cleanup_ids,
            cleanup_elapsed=f"{time.monotonic() - cleanup_started:.3f}s",
        )
        return

    _progress(progress_cb, {"type": "cleanup", "ok": None, "unsubscribing": cleanup_ids})
    ok, _rc = _run_helper_json_lines(
        "unsubscribe",
        appid=appid,
        timeout=180,
        mod_ids=cleanup_ids,
        cancel_event=_CLEANUP_CANCEL_EVENT,
        on_event=lambda event: _progress(progress_cb, {"type": "cleanup_event", "event": event}),
        progress_cb=progress_cb,
    )
    _progress(progress_cb, {"type": "cleanup", "ok": bool(ok), "unsubscribed": cleanup_ids})
    _log_event(
        progress_cb, "[Steam UGC] Cancel cleanup complete",
        ok=bool(ok), cleanup_ids=cleanup_ids,
        cleanup_elapsed=f"{time.monotonic() - cleanup_started:.3f}s",
    )


def _subscription_cleanup_candidates(parent_authorized_ids, helper_attempted_ids) -> list[int]:
    """Intersect parent state authority with exact helper subscription attempts."""

    parent_authorized = set(_strict_cleanup_workshop_item_ids(
        parent_authorized_ids or [],
    ))
    helper_attempted = set(_strict_cleanup_workshop_item_ids(
        helper_attempted_ids or [],
    ))
    return sorted(parent_authorized & helper_attempted)


def _refresh_current_state(sessions: dict[int, UGCModSession], *, appid: int, progress_cb=None, names_by_id=None) -> None:
    ids = sorted(sessions)
    if not ids:
        return

    def on_refresh_event(event: dict) -> None:
        if event.get("type") != "item":
            _progress(progress_cb, {"type": "refresh_event", "event": event})
            return
        event = _normalize_ugc_snapshot(event)
        mid = _strict_cleanup_workshop_item_id(event.get("id"))
        session = sessions.get(mid)
        if session is None:
            return
        session.update_from_item(event, source="refresh")
        _cache_ugc_state({mid: event}, names_by_id=names_by_id)
        _progress(progress_cb, session.event())

    _run_helper_json_lines(
        "state",
        appid=appid,
        timeout=120,
        mod_ids=ids,
        cancel_event=_CLEANUP_CANCEL_EVENT,
        on_event=on_refresh_event,
        progress_cb=progress_cb,
    )


def query_ugc_state_checked(mod_ids, *, appid=DAYZ_APPID, timeout=60) -> tuple[bool, dict[int, dict]]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return True, snapshots

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        mid = _strict_cleanup_workshop_item_id(event.get("id"))
        if mid is not None:
            snapshots[mid] = dict(event)

    ok, _rc = _run_helper_json_lines(
        "state",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return bool(ok), snapshots


def _explicit_subscription_state(states, mod_id: int) -> bool | None:
    """Return a subscription value only when the target carries a real bool."""

    if not isinstance(states, dict):
        return None
    state = states.get(int(mod_id))
    if not isinstance(state, dict) or "subscribed" not in state:
        return None
    subscribed = state["subscribed"]
    if type(subscribed) is not bool:
        return None
    return subscribed


def refresh_subscribed_ugc_state_checked(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    timeout=UGC_SUBSCRIBED_REFRESH_BATCH_TIMEOUT_S,
    cancel_event=None,
) -> tuple[bool, dict[int, dict], dict]:
    """Best-effort metadata refresh with authoritative final item snapshots."""
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    result = {
        "subscribed": [],
        "refreshed": [],
        "failed": [],
        "timed_out": [],
        "failures": [],
    }
    if not ids:
        return True, snapshots, result

    def on_event(event: dict) -> None:
        if event.get("type") == "item":
            normalized = _normalize_ugc_snapshot(event)
            mid = _strict_cleanup_workshop_item_id(normalized.get("id"))
            if mid is not None:
                snapshots[mid] = dict(normalized)
            return
        if event.get("type") not in ("command_result", "done"):
            return
        for key in ("subscribed", "refreshed", "failed", "timed_out"):
            values = []
            for raw in event.get(key) or []:
                try:
                    value = int(raw)
                except Exception:
                    continue
                if value > 0:
                    values.append(value)
            result[key] = sorted(set(values))
        result["failures"] = [
            dict(value) for value in event.get("failures") or []
            if isinstance(value, dict)
        ]

    ok, _rc = _run_helper_json_lines(
        "refresh-subscribed-state",
        appid=int(appid),
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=cancel_event,
        on_event=on_event,
        progress_cb=None,
    )
    complete_state = bool(ok and all(mid in snapshots for mid in ids))
    _cache_ugc_state(snapshots)
    return complete_state, snapshots, result


def query_ugc_inventory_checked(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    timeout=60,
    cancel_event=None,
) -> UGCSubscriptionSnapshot:
    """Return per-item state only with a complete authenticated subscription list.

    A successful helper command alone is insufficient: the helper must also
    confirm that Steam is logged on, return its complete subscribed-item list,
    and emit exactly one state snapshot for every requested item.  Agreement
    between the list and each item's Subscribed flag guards against the
    transient all-zero state seen while Steam UGC is not ready.
    """
    ids = _dedupe_sorted_ids(mod_ids)
    requested = frozenset(ids)
    snapshots: dict[int, dict] = {}
    terminal: dict = {}
    init_event: dict = {}

    if not ids:
        return UGCSubscriptionSnapshot(
            valid=False,
            requested_ids=requested,
            subscribed_ids=frozenset(),
            states={},
            logged_on=False,
            steam_id=0,
            native_attachment_verified=False,
            created_monotonic=time.monotonic(),
        )

    def on_event(event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "init":
            init_event.clear()
            init_event.update(event)
        elif event_type == "item":
            normalized = _normalize_ugc_snapshot(event)
            mid = _strict_cleanup_workshop_item_id(normalized.get("id"))
            if mid is not None:
                snapshots[mid] = dict(normalized)
        elif event_type in ("command_result", "done"):
            terminal.clear()
            terminal.update(event)

    try:
        ok, _rc = _run_helper_json_lines(
            "state",
            appid=int(appid),
            timeout=float(timeout),
            mod_ids=ids,
            cancel_event=cancel_event,
            on_event=on_event,
            progress_cb=None,
            strict_native_environment=True,
        )
    except Exception:
        ok = False

    subscribed: set[int] = set()
    inventory_well_formed = True
    for raw_mid in terminal.get("subscribed_item_ids") or []:
        mid = _strict_cleanup_workshop_item_id(raw_mid)
        if mid is None:
            inventory_well_formed = False
            continue
        subscribed.add(mid)
    try:
        subscription_count = int(terminal.get("subscription_count"))
    except Exception:
        subscription_count = -1

    complete_states = set(snapshots) == set(ids)
    subscription_fields_valid = complete_states and all(
        isinstance(snapshots.get(mid), dict)
        and "subscribed" in snapshots[mid]
        and type(snapshots[mid]["subscribed"]) is bool
        for mid in ids
    )
    consistent = subscription_fields_valid and all(
        snapshots[mid]["subscribed"] == (mid in subscribed)
        for mid in ids
    )
    identity_verified, steam_id, logged_on, native_attachment_verified = (
        _authenticated_native_helper_result(
            init_event, terminal, appid=int(appid),
        )
    )
    valid = bool(
        ok
        and terminal
        and terminal.get("subscription_inventory_complete", False)
        and identity_verified
        and inventory_well_formed
        and subscription_count == len(subscribed)
        and complete_states
        and subscription_fields_valid
        and consistent
    )
    if valid:
        _cache_ugc_state(snapshots)
    return UGCSubscriptionSnapshot(
        valid=valid,
        requested_ids=requested,
        subscribed_ids=frozenset(subscribed) if valid else frozenset(),
        states=dict(snapshots) if valid else {},
        logged_on=logged_on,
        steam_id=steam_id if valid else 0,
        native_attachment_verified=native_attachment_verified,
        created_monotonic=time.monotonic(),
    )


def _native_helper_attachment_verified(init_event: dict, *, appid: int) -> bool:
    """Validate that a helper loaded SteamAPI from a native Steam tree."""
    try:
        steam_root = Path(str(init_event.get("steam_root") or "")).expanduser().resolve()
        library_path = Path(str(init_event.get("lib") or "")).expanduser().resolve()
        root_low = str(steam_root).casefold()
        lib_low = str(library_path).casefold()
        return bool(
            init_event.get("ok", False)
            and int(init_event.get("appid") or 0) == int(appid)
            and not bool(init_event.get("flatpak_environment", True))
            and "com.valvesoftware.steam" not in root_low
            and "com.valvesoftware.steam" not in lib_low
            and "flatpak" not in root_low
            and "flatpak" not in lib_low
            and library_path.is_relative_to(steam_root)
            and library_path.name == "libsteam_api.so"
        )
    except Exception:
        return False


def _authenticated_native_helper_result(
    init_event: dict,
    terminal: dict,
    *,
    appid: int,
) -> tuple[bool, int, bool, bool]:
    """Shared native provenance, login, and stable-identity predicate."""
    try:
        steam_id = int(terminal.get("steam_id") or 0)
    except Exception:
        steam_id = 0
    logged_on = bool(terminal.get("logged_on", False))
    attachment_ok = _native_helper_attachment_verified(
        init_event, appid=int(appid),
    )
    identity_stable = bool(
        terminal.get("identity_stable", False)
        or terminal.get("subscription_inventory_complete", False)
    )
    valid = bool(
        logged_on and steam_id > 0 and attachment_ok and identity_stable
    )
    return valid, steam_id, logged_on, attachment_ok


def probe_native_mod_manager_readiness(
    *,
    appid=DAYZ_APPID,
    timeout=8.0,
    cancel_event=None,
) -> UGCNativeReadiness:
    """Prove native Steam login, stable identity, and UGC initialization."""
    init_event: dict = {}
    terminal: dict = {}

    def on_event(event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "init":
            init_event.clear()
            init_event.update(event)
        elif event_type == "done":
            terminal.clear()
            terminal.update(event)

    try:
        ok, _rc = _run_helper_json_lines(
            "readiness",
            appid=int(appid),
            timeout=float(timeout),
            mod_ids=[],
            cancel_event=cancel_event,
            on_event=on_event,
            strict_native_environment=True,
        )
    except Exception:
        ok = False
    identity_verified, steam_id, logged_on, attachment_ok = (
        _authenticated_native_helper_result(
            init_event, terminal, appid=int(appid),
        )
    )
    valid = bool(
        ok
        and terminal.get("ok", False)
        and terminal.get("ugc_ready", False)
        and identity_verified
    )
    return UGCNativeReadiness(
        valid=valid,
        steam_id=steam_id if valid else 0,
        logged_on=logged_on,
        native_attachment_verified=attachment_ok,
        created_monotonic=time.monotonic(),
    )


def query_ugc_state(mod_ids, *, appid=DAYZ_APPID, timeout=60) -> dict[int, dict]:
    _ok, snapshots = query_ugc_state_checked(mod_ids, appid=appid, timeout=timeout)
    return snapshots


def wait_for_ugc_ready(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    cancel_event=None,
    progress_cb=None,
    allow_start_steam: bool = True,
    launch_policy=None,
    timeout_s: float = UGC_PREFLIGHT_TIMEOUT_S,
) -> bool:
    ids = _dedupe_sorted_ids(mod_ids)
    if not ids:
        return True
    return _run_ugc_native_steam_preflight(
        ids,
        appid=int(appid),
        cancel_event=cancel_event,
        progress_cb=progress_cb,
        allow_start_steam=bool(allow_start_steam),
        launch_policy=launch_policy,
        timeout_s=float(timeout_s),
    )


def unsubscribe_ugc_items_checked(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    timeout=120,
) -> tuple[bool, dict[int, dict]]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return True, snapshots
    native_ok, _state = _supported_native_steam_mutation_state()
    if not native_ok:
        raise UGCSessionError(
            "supported native Steam is unavailable; refusing Workshop mutation"
        )

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        mid = _strict_cleanup_workshop_item_id(event.get("id"))
        if mid is not None:
            snapshots[mid] = dict(event)

    ok, _rc = _run_helper_json_lines(
        "unsubscribe",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return bool(ok), snapshots


def unsubscribe_ugc_items(mod_ids, *, appid=DAYZ_APPID, timeout=120) -> dict[int, dict]:
    """Compatibility wrapper for callers which only consume item snapshots."""

    _ok, snapshots = unsubscribe_ugc_items_checked(
        mod_ids,
        appid=appid,
        timeout=timeout,
    )
    return snapshots


def cleanup_cancelled_ugc_subscriptions(
    mod_ids,
    *,
    appid=DAYZ_APPID,
    timeout=UGC_CANCEL_CLEANUP_BATCH_TIMEOUT_S,
    progress_cb=None,
) -> dict:
    """Best-effort cleanup in a fresh, short-lived SteamAPI context."""
    ids = _strict_cleanup_workshop_item_ids(mod_ids)
    result = {
        "candidates": ids,
        "attempted": [],
        "confirmed_unsubscribed": [],
        "retained_installed": [],
        "already_unsubscribed": [],
        "failed": [],
        "timed_out": [],
        "failures": [],
    }
    if not ids:
        return result
    native_ok, _state = _supported_native_steam_mutation_state()
    if not native_ok:
        result["failed"] = list(ids)
        result["failures"] = [
            {"id": mid, "reason": "native_steam_unavailable"} for mid in ids
        ]
        return result

    def on_event(event: dict) -> None:
        if event.get("type") != "done":
            return
        for key in (
            "candidates", "attempted", "confirmed_unsubscribed",
            "retained_installed", "already_unsubscribed", "failed",
            "timed_out",
        ):
            result[key] = _strict_cleanup_workshop_item_ids(
                event.get(key) or [],
            )
        result["failures"] = list(event.get("failures") or [])

    ok, _rc = _run_helper_json_lines(
        "cancel-cleanup-unsubscribe",
        appid=int(appid),
        timeout=min(
            UGC_CANCEL_CLEANUP_BATCH_TIMEOUT_S,
            max(0.0, float(timeout)),
        ),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=progress_cb,
        strict_native_environment=True,
    )
    accounted = set(
        result["confirmed_unsubscribed"]
        + result["retained_installed"]
        + result["already_unsubscribed"]
        + result["failed"]
        + result["timed_out"]
    )
    if not ok and not accounted:
        result["failed"] = list(ids)
        result["failures"] = [
            {"id": mid, "reason": "fresh_cleanup_helper_failed"} for mid in ids
        ]
    _log_event(
        progress_cb,
        "[Steam UGC] Fresh-context cancel cleanup completed",
        **result,
    )
    return result


def request_unsubscribe_ugc_items(mod_ids, *, appid=DAYZ_APPID, timeout=12) -> tuple[bool, dict[int, dict]]:
    ids = _dedupe_sorted_ids(mod_ids)
    snapshots: dict[int, dict] = {}
    if not ids:
        return True, snapshots
    native_ok, _state = _supported_native_steam_mutation_state()
    if not native_ok:
        return False, snapshots

    def on_event(event: dict) -> None:
        if event.get("type") != "item":
            return
        event = _normalize_ugc_snapshot(event)
        mid = _strict_cleanup_workshop_item_id(event.get("id"))
        if mid is not None:
            snapshots[mid] = dict(event)

    ok, _rc = _run_helper_json_lines(
        "unsubscribe-request",
        appid=appid,
        timeout=float(timeout),
        mod_ids=ids,
        cancel_event=None,
        on_event=on_event,
        progress_cb=None,
    )
    _cache_ugc_state(snapshots)
    return bool(ok), snapshots


def repair_ugc_item(
    mod_id,
    *,
    appid=DAYZ_APPID,
    progress_cb=None,
    removal_timeout: float = 120.0,
    download_timeout: float = 3600.0,
    poll_interval: float = 1.0,
    request_unsubscribe_fn=None,
    query_state_fn=None,
    install_fn=None,
    content_exists_fn=None,
    monotonic_fn=None,
    sleep_fn=None,
) -> dict:
    """Reacquire one Workshop item through Steam UGC without deleting files."""
    try:
        mid = int(mod_id)
    except Exception:
        mid = 0
    if mid <= 0:
        return {"ok": False, "id": mid, "reason": "invalid_id", "error": "Invalid Workshop ID."}

    uses_default_mutation_backend = request_unsubscribe_fn is None
    if uses_default_mutation_backend:
        native_ok, _state = _supported_native_steam_mutation_state()
        if not native_ok:
            return {
                "ok": False,
                "id": mid,
                "reason": "unsupported_steam",
                "error": "Start supported native Steam before repairing this mod.",
            }

    request_unsubscribe_fn = request_unsubscribe_fn or request_unsubscribe_ugc_items
    query_state_fn = query_state_fn or query_ugc_state_checked
    install_fn = install_fn or run_ugc_install
    monotonic_fn = monotonic_fn or time.monotonic
    sleep_fn = sleep_fn or time.sleep

    def content_exists(state: dict) -> bool:
        if callable(content_exists_fn):
            return bool(content_exists_fn(mid, dict(state or {})))
        return _has_real_native_workshop_folder(mid, appid=int(appid), states=[dict(state or {})])

    def emit_stage(stage: str, **extra) -> None:
        event = {"type": "repair", "id": mid, "stage": str(stage)}
        event.update(extra)
        _progress(progress_cb, event)

    def query() -> tuple[bool, dict]:
        ok, states = query_state_fn([mid], appid=int(appid), timeout=10)
        return bool(ok), dict((states or {}).get(mid) or (states or {}).get(str(mid)) or {})

    emit_stage("unsubscribing")
    try:
        request_ok, _snapshots = request_unsubscribe_fn([mid], appid=int(appid), timeout=12)
    except UGCHelperReapError:
        raise
    except Exception as exc:
        return {"ok": False, "id": mid, "reason": "unsubscribe_failed", "error": f"Steam could not unsubscribe the mod: {exc}"}
    if not request_ok:
        return {"ok": False, "id": mid, "reason": "unsubscribe_failed", "error": "Steam did not accept the unsubscribe request."}

    deadline = monotonic_fn() + max(0.0, float(removal_timeout))
    unsubscribed = False
    while monotonic_fn() <= deadline:
        emit_stage("waiting_removal")
        try:
            state_ok, state = query()
        except UGCHelperReapError:
            raise
        except Exception:
            state_ok, state = False, {}
        subscription_state = _explicit_subscription_state({mid: state}, mid)
        if state_ok and subscription_state is not None:
            unsubscribed = subscription_state is False
            if unsubscribed and not content_exists(state):
                break
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            reason = "removal_timeout" if unsubscribed else "unsubscribe_failed"
            error = (
                "Steam unsubscribed the mod but did not finish removing its local content in time."
                if unsubscribed
                else "Steam still reports the mod subscribed."
            )
            return {"ok": False, "id": mid, "reason": reason, "error": error, "not_installed": unsubscribed}
        sleep_fn(min(max(0.01, float(poll_interval)), remaining))

    emit_stage("subscribing")

    if uses_default_mutation_backend:
        native_ok, _state = _supported_native_steam_mutation_state()
        if not native_ok:
            return {
                "ok": False,
                "id": mid,
                "reason": "unsupported_steam",
                "error": "Supported native Steam became unavailable during repair.",
                "not_installed": True,
            }

    def install_progress(event: dict) -> None:
        forwarded = dict(event or {})
        forwarded.setdefault("id", mid)
        forwarded["type"] = "repair_progress"
        forwarded["stage"] = "downloading"
        _progress(progress_cb, forwarded)

    try:
        install_ok = bool(
            install_fn(
                [mid],
                appid=int(appid),
                cancel_event=None,
                progress_cb=install_progress,
                allow_start_steam=False,
                timeout=float(download_timeout),
            )
        )
    except UGCHelperReapError:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "id": mid,
            "reason": "download_failed",
            "error": f"Steam could not reinstall the mod: {exc}",
            "not_installed": True,
        }

    emit_stage("verifying")
    try:
        state_ok, terminal = query()
    except UGCHelperReapError:
        raise
    except Exception:
        state_ok, terminal = False, {}
    if not state_ok or not terminal:
        return {
            "ok": False,
            "id": mid,
            "reason": "terminal_unresolved",
            "error": "Steam finished without returning a verifiable final mod state.",
            "not_installed": True,
        }
    terminal_subscription = _explicit_subscription_state({mid: terminal}, mid)
    if terminal_subscription is not True:
        return {
            "ok": False,
            "id": mid,
            "reason": "subscribe_failed",
            "error": "Steam did not resubscribe the mod.",
            "not_installed": True,
            "state": terminal,
        }
    if not install_ok or not ugc_item_ready(terminal):
        return {
            "ok": False,
            "id": mid,
            "reason": "download_failed",
            "error": "Steam did not finish installing the repaired mod.",
            "not_installed": not bool(terminal.get("installed", False)),
            "state": terminal,
        }
    if not content_exists(terminal):
        return {
            "ok": False,
            "id": mid,
            "reason": "missing_directory",
            "error": "Steam reports the mod ready, but its Workshop content directory is missing.",
            "not_installed": True,
            "state": terminal,
        }

    emit_stage("success", fraction=1.0, percent=100)
    return {"ok": True, "id": mid, "reason": "success", "state": terminal}


def _native_steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        from .steam_native import native_steam_libraries, resolve_native_steam_root

        roots.extend(Path(path) for path in native_steam_libraries())

        root = resolve_native_steam_root()
        if root is not None:
            roots.append(Path(root))
    except Exception:
        pass
    roots.extend((Path.home() / ".local/share/Steam", Path.home() / ".steam/steam"))

    out: list[Path] = []
    seen = set()
    for root in roots:
        try:
            resolved = Path(root).expanduser().resolve()
        except Exception:
            continue
        key = str(resolved)
        if key not in seen:
            out.append(resolved)
            seen.add(key)
    return out


def _safe_native_workshop_path_for_root(
    root,
    candidate,
    *,
    relative_parts,
    leaf_kind: str,
) -> Path | None:
    return safe_native_workshop_mutation_path(
        root,
        candidate,
        relative_parts=relative_parts,
        leaf_kind=leaf_kind,
    )


def _safe_workshop_content_path(path: str, *, mod_id: int, appid: int) -> Path | None:
    if not path:
        return None
    mid = _strict_cleanup_workshop_item_id(mod_id)
    if mid is None:
        return None
    try:
        appid_i = int(appid)
        raw = Path(path).expanduser()
    except Exception:
        return None
    if raw.name != str(mid):
        return None
    for root in _native_steam_roots():
        safe = _safe_native_workshop_path_for_root(
            root,
            raw,
            relative_parts=("content", str(appid_i), str(mid)),
            leaf_kind="directory",
        )
        if safe is not None:
            return safe
    return None


def _safe_workshop_download_dir(path: Path, *, mod_id: int, appid: int) -> Path | None:
    mid = _strict_cleanup_workshop_item_id(mod_id)
    if mid is None:
        return None
    try:
        appid_i = int(appid)
        raw = Path(path).expanduser()
    except Exception:
        return None
    if raw.name != str(mid):
        return None
    for root in _native_steam_roots():
        safe = _safe_native_workshop_path_for_root(
            root,
            raw,
            relative_parts=("downloads", str(appid_i), str(mid)),
            leaf_kind="directory",
        )
        if safe is not None:
            return safe
    return None


def _safe_workshop_patch_file(path: Path, *, mod_id: int, appid: int) -> Path | None:
    mid = _strict_cleanup_workshop_item_id(mod_id)
    if mid is None:
        return None
    try:
        appid_i = int(appid)
        expected_name = f"state_{appid_i}_{appid_i}_{mid}.patch"
        raw = Path(path).expanduser()
    except Exception:
        return None
    if raw.name != expected_name:
        return None
    for root in _native_steam_roots():
        for relative_parts in (
            ("downloads", expected_name),
            (expected_name,),
        ):
            safe = _safe_native_workshop_path_for_root(
                root,
                raw,
                relative_parts=relative_parts,
                leaf_kind="file",
            )
            if safe is not None:
                return safe
    return None


def _safe_workshop_acf_path(path: Path, *, appid: int) -> Path | None:
    try:
        appid_i = int(appid)
        expected_name = f"appworkshop_{appid_i}.acf"
        raw = Path(path).expanduser()
    except Exception:
        return None
    if raw.name != expected_name:
        return None
    for root in _native_steam_roots():
        safe = _safe_native_workshop_path_for_root(
            root,
            raw,
            relative_parts=(expected_name,),
            leaf_kind="file",
        )
        if safe is not None:
            return safe
    return None


def _native_workshop_cleanup_plan(mod_id: int, appid: int) -> list[dict]:
    """Resolve every per-library mutation path before any deletion begins."""

    mid = _strict_cleanup_workshop_item_id(mod_id)
    if mid is None:
        raise RuntimeError(f"invalid Workshop item id: {mod_id!r}")
    appid_i = int(appid)
    patch_name = f"state_{appid_i}_{appid_i}_{mid}.patch"
    plans: list[dict] = []
    for root in _native_steam_roots():
        candidates = {
            "content": (
                root / "steamapps/workshop/content" / str(appid_i) / str(mid),
                ("content", str(appid_i), str(mid)),
                "directory",
            ),
            "downloads": (
                root / "steamapps/workshop/downloads" / str(appid_i) / str(mid),
                ("downloads", str(appid_i), str(mid)),
                "directory",
            ),
            "downloads_patch": (
                root / "steamapps/workshop/downloads" / patch_name,
                ("downloads", patch_name),
                "file",
            ),
            "root_patch": (
                root / "steamapps/workshop" / patch_name,
                (patch_name,),
                "file",
            ),
            "acf": (
                root / "steamapps/workshop" / f"appworkshop_{appid_i}.acf",
                (f"appworkshop_{appid_i}.acf",),
                "file",
            ),
        }
        plan = {"root": root}
        for key, (candidate, relative_parts, leaf_kind) in candidates.items():
            safe = _safe_native_workshop_path_for_root(
                root,
                candidate,
                relative_parts=relative_parts,
                leaf_kind=leaf_kind,
            )
            if safe is None:
                if key == "content":
                    raise RuntimeError(
                        f"refusing unsafe install folder: {candidate}"
                    )
                raise RuntimeError(f"refusing unsafe Workshop path: {candidate}")
            plan[key] = safe
        plans.append(plan)
    return plans


def _candidate_content_dirs(mod_id: int, appid: int, before: dict, after: dict) -> list[Path]:
    candidates: list[Path] = []
    for state in (before or {}, after or {}):
        folder = str(state.get("install_folder") or "").strip()
        safe = _safe_workshop_content_path(folder, mod_id=mod_id, appid=appid)
        if safe is not None:
            candidates.append(safe)
    for root in _native_steam_roots():
        candidates.append(root / "steamapps/workshop/content" / str(int(appid)) / str(int(mod_id)))

    out: list[Path] = []
    seen = set()
    for path in candidates:
        safe = _safe_workshop_content_path(str(path), mod_id=mod_id, appid=appid)
        if safe is None:
            continue
        key = str(safe)
        if key not in seen:
            out.append(safe)
            seen.add(key)
    return out


def _delete_dir_if_present(path: Path, *, log_fn=None, path_validator=None) -> bool:
    if callable(path_validator):
        path = path_validator(path)
        if path is None:
            raise RuntimeError("refusing to delete unsafe Workshop directory")
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(path_validator):
        revalidated = path_validator(path)
        if revalidated is None or Path(revalidated) != path:
            raise RuntimeError(f"refusing changed Workshop directory: {path}")
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(log_fn):
        log_fn(f"[Steam UGC] deleting local mod folder: {path}")
    shutil.rmtree(path, ignore_errors=False)
    return True


def _delete_file_if_present(path: Path, *, log_fn=None, path_validator=None) -> bool:
    if callable(path_validator):
        path = path_validator(path)
        if path is None:
            raise RuntimeError("refusing to delete unsafe Workshop file")
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(path_validator):
        revalidated = path_validator(path)
        if revalidated is None or Path(revalidated) != path:
            raise RuntimeError(f"refusing changed Workshop file: {path}")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"refusing to delete unsafe path: {path}")
    if callable(log_fn):
        log_fn(f"[Steam UGC] deleting local staging file: {path}")
    path.unlink()
    return True


def _mark_metadata_deleted(mod_id: int) -> None:
    try:
        from .mod_metadata import upsert_mod_metadata

        upsert_mod_metadata(
            int(mod_id),
            subscribed=False,
            installed=False,
            clear_fields=["install_folder"],
        )
    except Exception as exc:
        eprint(f"[Steam UGC] Metadata cache delete update failed: {exc}")


def _native_appworkshop_acf_paths(appid: int) -> list[Path]:
    out: list[Path] = []
    seen = set()
    for root in _native_steam_roots():
        path = root / "steamapps/workshop" / f"appworkshop_{int(appid)}.acf"
        safe = _safe_native_workshop_path_for_root(
            root,
            path,
            relative_parts=(f"appworkshop_{int(appid)}.acf",),
            leaf_kind="file",
        )
        if safe is None:
            raise RuntimeError(f"refusing unsafe Workshop ACF path: {path}")
        key = str(safe)
        if key not in seen:
            out.append(safe)
            seen.add(key)
    return out


def _find_acf_section_bounds(text: str, section_name: str) -> tuple[int, int, int] | None:
    match = re.search(rf'"{re.escape(section_name)}"\s*\{{', text)
    if not match:
        return None

    start_brace = text.find("{", match.start())
    if start_brace < 0:
        return None

    depth = 0
    for i in range(start_brace, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return match.start(), start_brace, i
    return None


def _extract_workshop_acf_ids_from_text(text: str) -> list[int]:
    ids: list[int] = []
    seen = set()
    for section_name in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        bounds = _find_acf_section_bounds(text, section_name)
        if bounds is None:
            continue
        _section_start, start_brace, end_brace = bounds
        block = text[start_brace : end_brace + 1]
        for _start, _end, key in _direct_acf_child_entries(block):
            try:
                mid = int(key)
            except Exception:
                continue
            if mid > 0 and mid not in seen:
                ids.append(mid)
                seen.add(mid)
    ids.sort()
    return ids


def _direct_acf_child_entries(block: str) -> list[tuple[int, int, str]]:
    entries: list[tuple[int, int, str]] = []
    depth = 0
    i = 0
    while i < len(block):
        c = block[i]
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        if depth != 1 or c != '"':
            i += 1
            continue

        key_start = i
        key_end = block.find('"', key_start + 1)
        if key_end < 0:
            break
        key = block[key_start + 1 : key_end]
        if not key.isdigit():
            i = key_end + 1
            continue

        value_start = key_end + 1
        while value_start < len(block) and block[value_start] in " \t\r\n":
            value_start += 1
        if value_start >= len(block):
            break

        line_start = block.rfind("\n", 0, key_start)
        cut_start = 0 if line_start < 0 else line_start + 1

        if block[value_start] == "{":
            d = 0
            value_end = -1
            for j in range(value_start, len(block)):
                cj = block[j]
                if cj == "{":
                    d += 1
                elif cj == "}":
                    d -= 1
                    if d == 0:
                        value_end = j + 1
                        break
            if value_end < 0:
                i = value_start + 1
                continue
            cut_end = value_end
        elif block[value_start] == '"':
            newline = block.find("\n", value_start)
            cut_end = len(block) if newline < 0 else newline + 1
        else:
            i = value_start + 1
            continue

        if cut_end < len(block) and block[cut_end] == "\r":
            cut_end += 1
        if cut_end < len(block) and block[cut_end] == "\n":
            cut_end += 1
        entries.append((cut_start, cut_end, key))
        i = cut_end
    return entries


def _remove_workshop_acf_ids_from_text(text: str, mod_ids: Iterable[int]) -> tuple[str, set[int]]:
    target_ids = {str(mid) for mid in _dedupe_sorted_ids(mod_ids)}
    if not target_ids:
        return text, set()

    removed: set[int] = set()
    new_text = text

    def _remove_from_section(full_text: str, section_name: str) -> tuple[str, set[int]]:
        bounds = _find_acf_section_bounds(full_text, section_name)
        if bounds is None:
            return full_text, set()

        _section_start, start_brace, end_brace = bounds
        block = full_text[start_brace : end_brace + 1]
        spans: list[tuple[int, int, int]] = []

        for start, end, mid_str in _direct_acf_child_entries(block):
            if mid_str not in target_ids:
                continue
            spans.append((start, end, int(mid_str)))

        if not spans:
            return full_text, set()

        spans.sort(key=lambda item: item[0])
        merged: list[tuple[int, int, int]] = []
        for start, end, mid in spans:
            if merged and start < merged[-1][1]:
                continue
            merged.append((start, end, mid))

        new_block = block
        section_removed: set[int] = set()
        for start, end, mid in reversed(merged):
            new_block = new_block[:start] + new_block[end:]
            section_removed.add(mid)

        return full_text[:start_brace] + new_block + full_text[end_brace + 1 :], section_removed

    for section in ("WorkshopItemsInstalled", "WorkshopItemDetails"):
        new_text, section_removed = _remove_from_section(new_text, section)
        removed.update(section_removed)

    return new_text, removed


def _write_all_and_fsync(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError(f"short Workshop ACF write: {written!r}")
        offset += written
    os.fsync(fd)


def _unlink_owned_regular_file(path: Path | None, identity) -> None:
    """Remove only the unchanged regular inode created by this invocation."""

    if path is None or identity is None:
        return
    try:
        current = os.lstat(path)
        if (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


ACF_BACKUP_RETENTION_COUNT = 5


@dataclass(frozen=True)
class _ACFBackupCandidate:
    path: Path
    timestamp: str
    mtime_ns: int
    collision_counter: int
    identity: tuple[int, int]


def _acf_backup_name_match(source_name: str, candidate_name: str):
    match = re.fullmatch(
        rf"{re.escape(source_name)}\.bak\."
        r"(\d{8}-\d{6})\.(\d+)(?:\.(\d+))?",
        candidate_name,
    )
    if match is None:
        return None
    timestamp = match.group(1)
    try:
        time.strptime(timestamp, "%Y%m%d-%H%M%S")
    except (OverflowError, ValueError):
        return None
    raw_counter = match.group(3)
    if raw_counter is None:
        collision_counter = 0
    else:
        # _open_unique_acf_backup emits only .1 through .999.  Values outside
        # that range are not attributable to this writer.
        if len(raw_counter) > 3:
            return None
        collision_counter = int(raw_counter)
        if not 1 <= collision_counter <= 999:
            return None
    return timestamp, collision_counter


def _log_acf_backup_prune_warning(log_fn, source: Path, candidate, reason) -> None:
    message = (
        f"[Steam UGC] could not prune old ACF backup for {source}: "
        f"{candidate}: {reason}"
    )
    if callable(log_fn):
        try:
            log_fn(message)
            return
        except Exception:
            pass
    try:
        logger.warning(message)
    except Exception:
        pass


def _prune_old_acf_backups(
    path: Path,
    *,
    validate_authoritative_path,
    log_fn=None,
    protected_backup_path: Path | None = None,
    protected_backup_identity: tuple[int, int] | None = None,
) -> None:
    """Keep only the newest completed DZLL backups for one exact ACF."""

    path = Path(path)
    try:
        validate_authoritative_path()
        with os.scandir(path.parent) as directory:
            entries = list(directory)
    except OSError as exc:
        _log_acf_backup_prune_warning(log_fn, path, path.parent, exc)
        return
    except Exception as exc:
        _log_acf_backup_prune_warning(log_fn, path, path.parent, exc)
        return

    protected_path = (
        None if protected_backup_path is None else Path(protected_backup_path)
    )
    protected_candidate: _ACFBackupCandidate | None = None
    candidates: list[_ACFBackupCandidate] = []
    for entry in entries:
        parsed = _acf_backup_name_match(path.name, entry.name)
        if parsed is None:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            _log_acf_backup_prune_warning(
                log_fn, path, path.parent / entry.name, exc,
            )
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        timestamp, collision_counter = parsed
        candidate = _ACFBackupCandidate(
            path=path.parent / entry.name,
            timestamp=timestamp,
            mtime_ns=info.st_mtime_ns,
            collision_counter=collision_counter,
            identity=(info.st_dev, info.st_ino),
        )
        if protected_path is not None and candidate.path == protected_path:
            if candidate.identity == protected_backup_identity:
                protected_candidate = candidate
            else:
                _log_acf_backup_prune_warning(
                    log_fn,
                    path,
                    candidate.path,
                    "new backup identity changed before retention",
                )
            continue
        candidates.append(candidate)

    candidates.sort(
        key=lambda candidate: (
            candidate.timestamp,
            candidate.mtime_ns,
            candidate.collision_counter,
            candidate.path.name,
        ),
        reverse=True,
    )
    historical_keep_count = ACF_BACKUP_RETENTION_COUNT
    if protected_candidate is not None:
        historical_keep_count -= 1
    for candidate in candidates[historical_keep_count:]:
        try:
            validate_authoritative_path()
        except Exception as exc:
            _log_acf_backup_prune_warning(
                log_fn, path, candidate.path, exc,
            )
            return
        try:
            current = os.lstat(candidate.path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _log_acf_backup_prune_warning(
                log_fn, path, candidate.path, exc,
            )
            continue
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != candidate.identity
        ):
            _log_acf_backup_prune_warning(
                log_fn,
                path,
                candidate.path,
                "candidate identity or file type changed",
            )
            continue
        try:
            candidate.path.unlink()
        except OSError as exc:
            _log_acf_backup_prune_warning(
                log_fn, path, candidate.path, exc,
            )


def _open_unique_acf_backup(path: Path, suffix: str) -> tuple[int, Path, tuple[int, int]]:
    base_name = f"{path.name}.bak.{suffix}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for counter in range(1000):
        name = base_name if counter == 0 else f"{base_name}.{counter}"
        candidate = path.with_name(name)
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            try:
                mode = os.lstat(candidate).st_mode
            except OSError as exc:
                raise RuntimeError(
                    f"failed to inspect Workshop ACF backup path: {candidate}"
                ) from exc
            if stat.S_ISLNK(mode):
                raise RuntimeError(
                    f"refusing symlinked Workshop ACF backup path: {candidate}"
                )
            continue
        created = os.fstat(fd)
        return fd, candidate, (created.st_dev, created.st_ino)
    raise RuntimeError("could not allocate a unique Workshop ACF backup path")


def _write_acf_atomically_with_backup(
    path: Path,
    original_bytes: bytes,
    new_text: str,
    *,
    path_validator=None,
    log_fn=None,
) -> str:
    path = Path(path)

    def validate_authoritative_path() -> None:
        if callable(path_validator):
            validated = path_validator(path)
            if validated is None or Path(validated) != path:
                raise RuntimeError(f"refusing changed Workshop ACF path: {path}")
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(f"failed to validate Workshop ACF path: {path}") from exc
        if not stat.S_ISREG(current.st_mode):
            raise RuntimeError(f"refusing unsafe Workshop ACF path: {path}")

    validate_authoritative_path()
    original_mode = os.lstat(path).st_mode & 0o777
    payload = new_text.encode("utf-8")
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{ts}.{os.getpid()}"
    temp_fd = -1
    temp_path: Path | None = None
    temp_identity = None
    backup_fd = -1
    backup_path: Path | None = None
    backup_identity = None
    backup_complete = False
    try:
        # Preserve the exact authoritative bytes before preparing a replacement.
        validate_authoritative_path()
        backup_fd, backup_path, backup_identity = _open_unique_acf_backup(
            path, suffix,
        )
        os.fchmod(backup_fd, original_mode)
        _write_all_and_fsync(backup_fd, original_bytes)
        os.close(backup_fd)
        backup_fd = -1
        backup_complete = True

        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        temp_stat = os.fstat(temp_fd)
        temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
        os.fchmod(temp_fd, original_mode)
        _write_all_and_fsync(temp_fd, payload)
        os.close(temp_fd)
        temp_fd = -1

        # This is the final current-state check before the atomic commit point.
        validate_authoritative_path()
        os.replace(temp_path, path)
        temp_path = None
        temp_identity = None
        validate_authoritative_path()
        try:
            _prune_old_acf_backups(
                path,
                validate_authoritative_path=validate_authoritative_path,
                log_fn=log_fn,
                protected_backup_path=backup_path,
                protected_backup_identity=backup_identity,
            )
        except Exception as exc:
            # Retention happens after the authoritative commit and must never
            # turn that successful rewrite into a reported failure.
            _log_acf_backup_prune_warning(log_fn, path, path.parent, exc)
        return str(backup_path)
    finally:
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if backup_fd >= 0:
            try:
                os.close(backup_fd)
            except OSError:
                pass
        _unlink_owned_regular_file(temp_path, temp_identity)
        if not backup_complete:
            _unlink_owned_regular_file(backup_path, backup_identity)


def _has_real_native_workshop_folder(mod_id: int, *, appid: int, states: Iterable[dict] = ()) -> bool:
    candidates: list[Path] = []
    for state in states or ():
        folder = str((state or {}).get("install_folder") or "").strip()
        safe = _safe_workshop_content_path(folder, mod_id=int(mod_id), appid=int(appid))
        if safe is not None:
            candidates.append(safe)
    for root in _native_steam_roots():
        candidates.append(root / "steamapps/workshop/content" / str(int(appid)) / str(int(mod_id)))

    for path in candidates:
        safe = _safe_workshop_content_path(str(path), mod_id=int(mod_id), appid=int(appid))
        if safe is None:
            continue
        try:
            if safe.exists() and safe.is_dir() and not safe.is_symlink():
                return True
        except Exception:
            continue
    return False


def _remove_workshop_acf_entries_from_paths(
    paths: Iterable[Path],
    mod_ids: Iterable[int],
    *,
    log_fn=None,
    path_validator=None,
) -> dict:
    ids = _dedupe_sorted_ids(mod_ids)
    result = {
        "ok": False,
        "paths": [],
        "removed_ids": [],
        "missing_ids": list(ids),
        "backups": [],
        "error": "",
    }
    if not ids:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    removed_any: set[int] = set()
    for raw_path in paths or ():
        path = Path(raw_path).expanduser()
        result["paths"].append(str(path))
        try:
            if callable(path_validator):
                path = path_validator(path)
                if path is None:
                    raise RuntimeError("refusing unsafe Workshop ACF path")
                path = Path(path)
            if not path.is_file():
                continue
            original_bytes = path.read_bytes()
            text = original_bytes.decode("utf-8", "replace")
            new_text, removed = _remove_workshop_acf_ids_from_text(text, ids)
            if not removed:
                continue
            if callable(path_validator):
                revalidated = path_validator(path)
                if revalidated is None or Path(revalidated) != path:
                    raise RuntimeError(f"refusing changed Workshop ACF path: {path}")
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(f"refusing unsafe Workshop ACF path: {path}")
            backup = _write_acf_atomically_with_backup(
                path,
                original_bytes,
                new_text,
                path_validator=path_validator,
                log_fn=log_fn,
            )
            result["backups"].append(backup)
            removed_any.update(removed)
            try:
                if callable(log_fn):
                    log_fn(f"[Steam UGC] removed ACF entries from {path}: {sorted(removed)}")
            except Exception:
                pass
        except Exception as exc:
            result["error"] = str(exc)
            return result

    result["removed_ids"] = sorted(removed_any)
    result["missing_ids"] = [mid for mid in ids if mid not in removed_any]
    result["ok"] = True
    return result


def remove_workshop_acf_entries(mod_ids, *, appid=DAYZ_APPID, log_fn=None) -> dict:
    """
    Remove exact DayZ Workshop item records from native appworkshop_<appid>.acf.

    The helper only removes keyed entries matching the supplied IDs inside known
    Workshop item sections. It skips IDs Steam still reports as subscribed and
    IDs that still have a real native workshop content folder.
    """
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
        except Exception:
            pass

    ids = _dedupe_sorted_ids(mod_ids)
    result = {
        "ok": False,
        "appid": int(appid),
        "paths": [],
        "removed_ids": [],
        "missing_ids": list(ids),
        "skipped_subscribed_ids": [],
        "skipped_installed_ids": [],
        "backups": [],
        "error": "",
    }
    if not ids:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    try:
        query_ok, states = query_ugc_state_checked(
            ids, appid=int(appid), timeout=20,
        )
    except Exception as exc:
        result["error"] = f"failed to verify UGC subscription state: {exc}"
        return result
    if not query_ok:
        result["error"] = "failed to verify UGC subscription state"
        return result

    subscription_states = {
        mid: _explicit_subscription_state(states, mid) for mid in ids
    }
    if any(value is None for value in subscription_states.values()):
        result["error"] = (
            "failed to verify explicit UGC subscription state for every item"
        )
        return result

    removable: list[int] = []
    for mid in ids:
        state = dict(states.get(mid) or {})
        if subscription_states[mid] is True:
            result["skipped_subscribed_ids"].append(mid)
            continue
        if _has_real_native_workshop_folder(mid, appid=int(appid), states=[state]):
            result["skipped_installed_ids"].append(mid)
            continue
        removable.append(mid)

    if not removable:
        result["ok"] = True
        result["missing_ids"] = []
        return result

    try:
        acf_paths = _native_appworkshop_acf_paths(int(appid))
    except Exception as exc:
        result["error"] = str(exc)
        return result
    path_result = _remove_workshop_acf_entries_from_paths(
        acf_paths,
        removable,
        log_fn=log,
        path_validator=lambda path: _safe_workshop_acf_path(
            path, appid=int(appid),
        ),
    )
    if not bool(path_result.get("ok", False)):
        result["paths"] = list(path_result.get("paths") or [])
        result["backups"] = list(path_result.get("backups") or [])
        result["error"] = str(path_result.get("error") or "")
        return result

    removed_sorted = list(path_result.get("removed_ids") or [])
    protected = set(result["skipped_subscribed_ids"]) | set(result["skipped_installed_ids"])
    result["paths"] = list(path_result.get("paths") or [])
    result["backups"] = list(path_result.get("backups") or [])
    result["removed_ids"] = removed_sorted
    result["missing_ids"] = [mid for mid in ids if mid not in set(removed_sorted) and mid not in protected]
    result["ok"] = True
    return result


def scrub_stale_dayz_workshop_acf(
    *,
    appid=DAYZ_APPID,
    remove_non_subscribed_installed=False,
    log_fn=None,
) -> dict:
    """
    Repair stale native DayZ appworkshop records without touching current mods.

    By default, removes only ACF item IDs that Steam reports unsubscribed and
    that do not have a real native workshop content folder.

    When remove_non_subscribed_installed=True, treats every non-subscribed
    direct ACF item as repair debris and removes it through delete_ugc_mod(),
    preserving subscribed items.
    """
    result = {
        "ok": False,
        "appid": int(appid),
        "remove_non_subscribed_installed": bool(remove_non_subscribed_installed),
        "acf_ids": [],
        "removed_ids": [],
        "removed_count": 0,
        "deleted_ids": [],
        "deleted_count": 0,
        "kept_ids": [],
        "kept_count": 0,
        "kept_subscribed_ids": [],
        "failures": [],
        "cleanup": {},
        "error": "",
    }

    acf_ids: set[int] = set()
    try:
        for path in _native_appworkshop_acf_paths(int(appid)):
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            acf_ids.update(_extract_workshop_acf_ids_from_text(text))
    except Exception as exc:
        result["error"] = str(exc)
        return result

    ids = sorted(acf_ids)
    result["acf_ids"] = ids
    if not ids:
        result["ok"] = True
        return result

    try:
        query_ok, states = query_ugc_state_checked(
            ids, appid=int(appid), timeout=60,
        )
    except Exception as exc:
        result["error"] = f"failed to query UGC state: {exc}"
        return result
    if not query_ok:
        result["error"] = "failed to query authoritative UGC state"
        return result

    subscription_states = {
        mid: _explicit_subscription_state(states, mid) for mid in ids
    }
    if any(value is None for value in subscription_states.values()):
        result["error"] = (
            "failed to verify explicit UGC subscription state for every item"
        )
        return result

    if bool(remove_non_subscribed_installed):
        kept_subscribed: list[int] = []
        delete_candidates: list[int] = []
        for mid in ids:
            state = dict(states.get(mid) or {})
            if subscription_states[mid] is True:
                kept_subscribed.append(mid)
            else:
                delete_candidates.append(mid)

        deleted: list[int] = []
        failures: list[dict] = []
        for mid in delete_candidates:
            try:
                delete_result = delete_ugc_mod(mid, appid=int(appid), log_fn=log_fn)
            except Exception as exc:
                failures.append({"id": mid, "error": str(exc)})
                continue
            if bool(delete_result.get("ok", False)):
                deleted.append(mid)
            else:
                failures.append(
                    {
                        "id": mid,
                        "error": str(delete_result.get("error") or "delete failed"),
                        "result": delete_result,
                    }
                )

        failed_ids = {int(item.get("id")) for item in failures if item.get("id") is not None}
        result["deleted_ids"] = deleted
        result["deleted_count"] = len(deleted)
        result["removed_ids"] = deleted
        result["removed_count"] = len(deleted)
        result["kept_subscribed_ids"] = kept_subscribed
        result["kept_ids"] = sorted(set(kept_subscribed) | failed_ids)
        result["kept_count"] = len(result["kept_ids"])
        result["failures"] = failures
        result["ok"] = not failures
        if failures:
            result["error"] = f"failed to delete {len(failures)} non-subscribed workshop item(s)"
        return result

    stale: list[int] = []
    kept: list[int] = []
    kept_subscribed: list[int] = []
    for mid in ids:
        state = dict(states.get(mid) or {})
        subscribed = subscription_states[mid] is True
        has_real_folder = _has_real_native_workshop_folder(mid, appid=int(appid), states=[state])
        if subscribed:
            kept.append(mid)
            kept_subscribed.append(mid)
            continue
        if has_real_folder:
            kept.append(mid)
            continue
        stale.append(mid)

    cleanup = remove_workshop_acf_entries(stale, appid=int(appid), log_fn=log_fn)
    result["cleanup"] = cleanup
    result["removed_ids"] = list(cleanup.get("removed_ids") or [])
    result["removed_count"] = len(result["removed_ids"])
    kept_set = set(kept) | (set(stale) - set(result["removed_ids"]))
    result["kept_ids"] = sorted(kept_set)
    result["kept_count"] = len(result["kept_ids"])
    result["kept_subscribed_ids"] = kept_subscribed
    result["ok"] = bool(cleanup.get("ok", False))
    result["error"] = str(cleanup.get("error") or "")
    return result


def delete_ugc_mod(mod_id, *, appid=DAYZ_APPID, timeout=120, log_fn=None) -> dict:
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
            else:
                print(message)
        except Exception:
            pass

    result = {
        "id": None,
        "unsubscribed": False,
        "deleted_folder": False,
        "acf_cleanup": {},
        "removed_symlinks": [],
        "before": {},
        "after": {},
        "ok": False,
        "error": "",
    }

    try:
        mid = int(mod_id)
    except Exception:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result
    result["id"] = mid
    if mid <= 0:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result

    try:
        before_ok, before_by_id = query_ugc_state_checked(
            [mid], appid=appid, timeout=min(float(timeout), 60.0),
        )
        before_subscribed = _explicit_subscription_state(before_by_id, mid)
        if not before_ok or before_subscribed is None:
            result["error"] = "failed to verify authoritative UGC subscription state"
            return result
        before = dict(before_by_id.get(mid) or {})
        result["before"] = before

        if before_subscribed is True:
            unsubscribe_ok, _unsubscribe_states = unsubscribe_ugc_items_checked(
                [mid], appid=appid, timeout=timeout,
            )
            if not unsubscribe_ok:
                result["error"] = "Steam UGC unsubscribe command failed"
                return result
            after_unsub_ok, after_unsub_by_id = query_ugc_state_checked(
                [mid], appid=appid, timeout=min(float(timeout), 60.0),
            )
            after_unsub_state = _explicit_subscription_state(
                after_unsub_by_id, mid,
            )
            after_unsub = dict(after_unsub_by_id.get(mid) or {})
            if not after_unsub_ok or after_unsub_state is not False:
                result["after"] = dict(after_unsub)
                result["error"] = (
                    "Steam did not authoritatively confirm the item is unsubscribed"
                )
                return result
            result["unsubscribed"] = True

        after_ok, after_by_id = query_ugc_state_checked(
            [mid], appid=appid, timeout=min(float(timeout), 60.0),
        )
        after_subscribed = _explicit_subscription_state(after_by_id, mid)
        if not after_ok or after_subscribed is not False:
            result["error"] = (
                "failed to confirm authoritative unsubscribed UGC state before deletion"
            )
            return result
        after = dict(after_by_id.get(mid) or {})
        result["after"] = after

        try:
            cleanup_plan = _native_workshop_cleanup_plan(mid, int(appid))
        except Exception as exc:
            result["error"] = str(exc)
            return result

        for state in (before, after):
            reported_folder = str(state.get("install_folder") or "").strip()
            if reported_folder and _safe_workshop_content_path(reported_folder, mod_id=mid, appid=int(appid)) is None:
                result["error"] = f"refusing unsafe install folder: {reported_folder}"
                return result

        deleted_any_folder = False
        for plan in cleanup_plan:
            root = plan["root"]
            content_relative = ("content", str(int(appid)), str(mid))
            if _delete_dir_if_present(
                plan["content"],
                log_fn=log,
                path_validator=lambda path, root=root, relative=content_relative: (
                    _safe_native_workshop_path_for_root(
                        root,
                        path,
                        relative_parts=relative,
                        leaf_kind="directory",
                    )
                ),
            ):
                deleted_any_folder = True
            downloads_relative = ("downloads", str(int(appid)), str(mid))
            _delete_dir_if_present(
                plan["downloads"],
                log_fn=log,
                path_validator=lambda path, root=root, relative=downloads_relative: (
                    _safe_native_workshop_path_for_root(
                        root,
                        path,
                        relative_parts=relative,
                        leaf_kind="directory",
                    )
                ),
            )
            patch_name = f"state_{int(appid)}_{int(appid)}_{mid}.patch"
            for key, relative in (
                ("downloads_patch", ("downloads", patch_name)),
                ("root_patch", (patch_name,)),
            ):
                _delete_file_if_present(
                    plan[key],
                    log_fn=log,
                    path_validator=lambda path, root=root, relative=relative: (
                        _safe_native_workshop_path_for_root(
                            root,
                            path,
                            relative_parts=relative,
                            leaf_kind="file",
                        )
                    ),
                )

        result["deleted_folder"] = bool(deleted_any_folder)

        if deleted_any_folder and bool(after.get("installed", False)):
            after = _normalize_ugc_snapshot(after)
            result["after"] = after
            _cache_ugc_state({mid: after})

        try:
            from .steamcmd_mods import remove_dzll_symlinks_for_mod

            result["removed_symlinks"] = remove_dzll_symlinks_for_mod(mid, log_fn=log)
        except Exception as exc:
            result["error"] = f"failed to remove DZLL symlinks: {exc}"
            return result

        acf_cleanup = remove_workshop_acf_entries([mid], appid=int(appid), log_fn=log)
        result["acf_cleanup"] = acf_cleanup
        if not bool(acf_cleanup.get("ok", False)):
            result["error"] = str(acf_cleanup.get("error") or "failed to clean appworkshop ACF")
            return result

        _mark_metadata_deleted(mid)
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def delete_ugc_mod_local_files_after_unsubscribe(
    mod_id,
    *,
    appid=DAYZ_APPID,
    log_fn=None,
    steam_absence_verified: bool = False,
    subscription_snapshot: UGCSubscriptionSnapshot | None = None,
) -> dict:
    """
    Delete local DayZ Workshop state for an already-unsubscribed item.

    This does not call Steam UGC APIs; it only removes guarded native DayZ
    workshop paths, exact ACF entries, DZLL-owned symlinks, and cached metadata.
    The caller must supply fresh authoritative proof that Steam is absent, and
    this boundary independently rechecks the current Steam state.
    """
    def log(message: str) -> None:
        try:
            if callable(log_fn):
                log_fn(message)
            else:
                print(message)
        except Exception:
            pass

    result = {
        "id": None,
        "deleted_folder": False,
        "deleted_staging": False,
        "acf_cleanup": {},
        "removed_symlinks": [],
        "ok": False,
        "error": "",
    }

    try:
        mid = int(mod_id)
    except Exception:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result
    result["id"] = mid
    if mid <= 0:
        result["error"] = f"invalid mod id: {mod_id!r}"
        return result

    try:
        cleanup_appid = int(appid)
    except (TypeError, ValueError):
        cleanup_appid = 0
    if cleanup_appid != DAYZ_APPID:
        result["error"] = (
            f"refusing local Workshop cleanup for unsupported app id: {appid!r}"
        )
        return result

    _native_ok, steam_state = _supported_native_steam_mutation_state()
    if not bool(steam_absence_verified):
        result["error"] = (
            "refusing local Workshop cleanup without fresh authoritative proof "
            "that Steam is offline"
        )
        return result
    if not isinstance(subscription_snapshot, UGCSubscriptionSnapshot) or not (
        subscription_snapshot.is_fresh_for_destructive_cleanup()
        and subscription_snapshot.proves_unsubscribed(mid)
    ):
        result["error"] = (
            "refusing local Workshop cleanup without a current authoritative "
            "subscription snapshot proving the item is unsubscribed"
        )
        return result
    if str(getattr(steam_state, "value", "")) != "offline":
        result["error"] = (
            "refusing local Workshop cleanup unless supported native Steam is stopped"
        )
        return result

    try:
        cleanup_plan = _native_workshop_cleanup_plan(mid, cleanup_appid)
        deleted_any_folder = False
        deleted_any_staging = False

        for plan in cleanup_plan:
            root = plan["root"]
            content_relative = ("content", str(cleanup_appid), str(mid))
            if _delete_dir_if_present(
                plan["content"],
                log_fn=log,
                path_validator=lambda path, root=root, relative=content_relative: (
                    _safe_native_workshop_path_for_root(
                        root,
                        path,
                        relative_parts=relative,
                        leaf_kind="directory",
                    )
                ),
            ):
                deleted_any_folder = True

            downloads_relative = ("downloads", str(cleanup_appid), str(mid))
            if _delete_dir_if_present(
                plan["downloads"],
                log_fn=log,
                path_validator=lambda path, root=root, relative=downloads_relative: (
                    _safe_native_workshop_path_for_root(
                        root,
                        path,
                        relative_parts=relative,
                        leaf_kind="directory",
                    )
                ),
            ):
                deleted_any_staging = True

            patch_name = f"state_{cleanup_appid}_{cleanup_appid}_{mid}.patch"
            for key, relative in (
                ("downloads_patch", ("downloads", patch_name)),
                ("root_patch", (patch_name,)),
            ):
                if _delete_file_if_present(
                    plan[key],
                    log_fn=log,
                    path_validator=lambda path, root=root, relative=relative: (
                        _safe_native_workshop_path_for_root(
                            root,
                            path,
                            relative_parts=relative,
                            leaf_kind="file",
                        )
                    ),
                ):
                    deleted_any_staging = True

        result["deleted_folder"] = bool(deleted_any_folder)
        result["deleted_staging"] = bool(deleted_any_staging)

        try:
            from .steamcmd_mods import remove_dzll_symlinks_for_mod

            result["removed_symlinks"] = remove_dzll_symlinks_for_mod(mid, log_fn=log)
        except Exception as exc:
            result["error"] = f"failed to remove DZLL symlinks: {exc}"
            return result

        acf_cleanup = _remove_workshop_acf_entries_from_paths(
            _native_appworkshop_acf_paths(cleanup_appid),
            [mid],
            log_fn=log,
            path_validator=lambda path: _safe_workshop_acf_path(
                path, appid=cleanup_appid,
            ),
        )
        result["acf_cleanup"] = acf_cleanup
        if not bool(acf_cleanup.get("ok", False)):
            result["error"] = str(acf_cleanup.get("error") or "failed to clean appworkshop ACF")
            return result

        _mark_metadata_deleted(mid)
        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def run_ugc_install(
    mod_ids,
    *,
    appid: int = DAYZ_APPID,
    cancel_event=None,
    progress_cb=None,
    handoff_cb=None,
    names_by_id=None,
    allow_start_steam: bool = True,
    launch_policy=None,
    timeout: float = 3600,
) -> bool:
    ids = _dedupe_sorted_ids(mod_ids)
    if not ids:
        return True

    sessions = {mid: UGCModSession(id=mid) for mid in ids}
    parent_cleanup_allowlist: list[int] = []
    _progress(progress_cb, {"type": "start", "appid": int(appid), "items": ids})
    _log_event(progress_cb, f"[JOIN] Steam UGC checking required mod readiness: {len(ids)} ids", ids=ids)

    def on_state_event(event: dict) -> None:
        if event.get("type") != "item":
            _progress(progress_cb, {"type": "helper_event", "event": event})
            return
        event = _normalize_ugc_snapshot(event)
        mid = _strict_cleanup_workshop_item_id(event.get("id"))
        session = sessions.get(mid)
        if session is None:
            return
        session.was_subscribed_before = _explicit_subscription_state(
            {mid: event}, mid,
        )
        session.was_installed_before = bool(event.get("installed", False))
        session.update_from_item(event, source="initial")
        _cache_ugc_state({mid: event}, names_by_id=names_by_id)
        _progress(progress_cb, session.event())

    try:
        if not _run_ugc_native_steam_preflight(
            ids,
            appid=int(appid),
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            allow_start_steam=bool(allow_start_steam),
            launch_policy=launch_policy,
            timeout_s=UGC_PREFLIGHT_TIMEOUT_S,
        ):
            _progress(progress_cb, {"type": "done", "ok": False, "reason": "preflight_failed", "sessions": [s.event() for s in sessions.values()]})
            return False

        ok, _rc = _run_helper_json_lines(
            "state",
            appid=appid,
            timeout=min(float(timeout), 120.0),
            mod_ids=ids,
            cancel_event=cancel_event,
            on_event=on_state_event,
            progress_cb=progress_cb,
        )
        if not ok:
            _progress(progress_cb, {"type": "done", "ok": False, "reason": "initial_state_failed", "sessions": [s.event() for s in sessions.values()]})
            return False

        for mid, session in sorted(sessions.items()):
            _log_event(
                progress_cb,
                "[Steam UGC] item "
                f"{mid} ready={ugc_item_ready(session)} "
                f"installed={bool(session.installed_now)} "
                f"needs_update={bool(session.needs_update)} "
                f"downloading={bool(session.downloading)} "
                f"pending={bool(session.download_pending)}",
                id=mid,
                ready=ugc_item_ready(session),
                installed=bool(session.installed_now),
                needs_update=bool(session.needs_update),
                downloading=bool(session.downloading),
                download_pending=bool(session.download_pending),
            )

        not_ready = sorted(mid for mid, session in sessions.items() if not ugc_item_ready(session))
        if not not_ready:
            _log_event(progress_cb, f"[Steam UGC] All required Workshop items are ready: {len(ids)} ids", ids=ids)
            _progress(progress_cb, {"type": "done", "ok": True, "installed": ids, "ready": ids, "failed": [], "sessions": [s.event() for s in sessions.values()]})
            return True

        _log_event(progress_cb, f"[Steam UGC] required update/download ids: {not_ready}", ids=not_ready)

        for mid in not_ready:
            session = sessions[mid]
            if session.was_subscribed_before is False:
                session.subscribed_by_dzll_this_join = True
        parent_cleanup_allowlist = sorted(
            mid
            for mid in not_ready
            if sessions[mid].subscribed_by_dzll_this_join
            and sessions[mid].was_subscribed_before is False
        )

        _progress(progress_cb, {"backend": "steam_ugc", "type": "status", "message": "Checking/Updating Required Mods"})

        cancel_handoff: dict = {}

        def publish_cancel_handoff(helper_attempted_ids) -> bool:
            """Publish this command's two-factor cleanup provenance once."""

            nonlocal cancel_handoff
            if cancel_handoff:
                return False
            helper_attempted = set(_strict_cleanup_workshop_item_ids(
                helper_attempted_ids or [],
            ))
            cleanup_candidates = _subscription_cleanup_candidates(
                parent_cleanup_allowlist, helper_attempted,
            )
            cancel_handoff = {
                "parent_allowlisted": list(parent_cleanup_allowlist),
                "helper_subscribe_attempted": sorted(helper_attempted),
                "cleanup_candidates": cleanup_candidates,
            }
            if callable(handoff_cb):
                try:
                    handoff_cb(dict(cancel_handoff))
                except Exception as exc:
                    _log_event(
                        progress_cb,
                        "[Steam UGC] Cancellation handoff callback failed",
                        error=str(exc), cleanup_candidates=cleanup_candidates,
                    )
            return True

        def on_install_event(event: dict) -> None:
            event_type = event.get("type")
            if event_type in ("item", "request"):
                event = _normalize_ugc_snapshot(event)
                mid = _strict_cleanup_workshop_item_id(event.get("id"))
                session = sessions.get(mid)
                if session is not None:
                    source = "request" if event_type == "request" else (
                        "poll" if session.request_attempted else "initial"
                    )
                    session.update_from_item(event, source=source)
                    _cache_ugc_state({mid: event}, names_by_id=names_by_id)
                    _progress(progress_cb, session.event())
                _progress(progress_cb, {"type": "helper_event", "event": event})
                return
            if event_type == "done":
                for mid_raw in event.get("ready") or event.get("installed") or []:
                    try:
                        mid = int(mid_raw)
                    except Exception:
                        continue
                    session = sessions.get(mid)
                    if session is not None:
                        session.installed_now = True
                        session.needs_update = False
                        session.downloading = False
                        session.download_pending = False
                _progress(progress_cb, {"type": "helper_event", "event": event})
                return
            if event_type == "command_result" and isinstance(
                event.get("cancel_handoff"), dict,
            ):
                helper_handoff = dict(event["cancel_handoff"])
                helper_attempted = _strict_cleanup_workshop_item_ids(
                    helper_handoff.get("helper_subscribe_attempted") or [],
                )
                publish_cancel_handoff(helper_attempted)
            _progress(progress_cb, {"type": "helper_event", "event": event})

        ok, _rc = _run_helper_json_lines(
            "subscribe-download",
            appid=appid,
            timeout=float(timeout),
            mod_ids=not_ready,
            cancel_event=cancel_event,
            cancel_cleanup_ids=parent_cleanup_allowlist,
            on_event=on_install_event,
            progress_cb=progress_cb,
        )

        if cancel_event is not None and cancel_event.is_set():
            if not ok and not cancel_handoff:
                fallback_attempted = [
                    mid for mid, session in sessions.items()
                    if session.helper_subscribe_attempted
                    and not session.installed_now
                ]
                fallback_candidates = _subscription_cleanup_candidates(
                    parent_cleanup_allowlist, fallback_attempted,
                )
                if fallback_candidates:
                    publish_cancel_handoff(fallback_attempted)
                    _log_event(
                        progress_cb,
                        "[Steam UGC] Exported terminal-race cancel cleanup handoff",
                        **cancel_handoff,
                    )
            if cancel_handoff:
                _log_event(
                    progress_cb,
                    "[Steam UGC] Deferred cancel cleanup handoff captured",
                    **cancel_handoff,
                )
            installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
            ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
            failed = sorted(mid for mid in ids if mid not in ready)
            _progress(progress_cb, {"type": "done", "ok": False, "cancelled": True, "installed": installed, "ready": ready, "failed": failed, "sessions": [s.event() for s in sessions.values()]})
            return False

        installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
        ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
        failed = sorted(mid for mid in ids if mid not in ready)
        if ok and not failed:
            _progress(progress_cb, {"type": "done", "ok": True, "installed": installed, "ready": ready, "failed": [], "sessions": [s.event() for s in sessions.values()]})
            return True

        _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
        installed = sorted(mid for mid, session in sessions.items() if session.installed_now)
        ready = sorted(mid for mid, session in sessions.items() if ugc_item_ready(session))
        failed = sorted(mid for mid in ids if mid not in ready)
        cleanup_candidates = _subscription_cleanup_candidates(
            parent_cleanup_allowlist,
            [
                mid for mid, session in sessions.items()
                if session.helper_subscribe_attempted
            ],
        )
        _cleanup_subscriptions(
            sessions, appid=appid, progress_cb=progress_cb,
            only_ids=cleanup_candidates,
        )
        _progress(progress_cb, {"type": "done", "ok": False, "installed": installed, "ready": ready, "failed": failed, "sessions": [s.event() for s in sessions.values()]})
        return False
    except KeyboardInterrupt:
        _progress(progress_cb, {"type": "cancelled", "reason": "keyboard_interrupt"})
        _log_event(progress_cb, "[Steam UGC] Cancel requested", reason="keyboard_interrupt")
        _refresh_current_state(sessions, appid=appid, progress_cb=progress_cb, names_by_id=names_by_id)
        cleanup_candidates = _subscription_cleanup_candidates(
            parent_cleanup_allowlist,
            [
                mid for mid, session in sessions.items()
                if session.helper_subscribe_attempted
            ],
        )
        _cleanup_subscriptions(
            sessions, appid=appid, progress_cb=progress_cb,
            only_ids=cleanup_candidates,
        )
        return False
    except Exception as exc:
        _progress(progress_cb, {"type": "error", "ok": False, "message": str(exc)})
        session = active_ugc_session()
        if isinstance(session, CooperativeUGCSession) and not session.usable:
            _log_event(
                progress_cb,
                "[Steam UGC] Skipping refresh and unsubscribe after fatal "
                "cooperative session failure",
                helper_pid=session.helper_pid,
                error=str(exc),
            )
        else:
            _refresh_current_state(
                sessions, appid=appid, progress_cb=progress_cb,
                names_by_id=names_by_id,
            )
            cleanup_candidates = _subscription_cleanup_candidates(
                parent_cleanup_allowlist,
                [
                    mid for mid, install_session in sessions.items()
                    if install_session.helper_subscribe_attempted
                ],
            )
            _cleanup_subscriptions(
                sessions, appid=appid, progress_cb=progress_cb,
                only_ids=cleanup_candidates,
            )
        return False


def _parse_item_id(raw: str) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"invalid item id: {raw!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dzll_launcher.steam_ugc_backend",
        description="Steam Client UGC backend for DayZ Workshop items.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="subscribe/download missing items and wait until installed")
    install.add_argument("--appid", type=int, default=DAYZ_APPID)
    install.add_argument("--timeout", type=float, default=3600.0)
    install.add_argument("item_ids", nargs="+", type=_parse_item_id)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "install":
        try:
            ok = run_ugc_install(
                args.item_ids,
                appid=int(args.appid),
                timeout=float(args.timeout),
                progress_cb=emit,
            )
        except KeyboardInterrupt:
            emit({"type": "cancelled", "reason": "keyboard_interrupt"})
            return 4
        return 0 if ok else 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
