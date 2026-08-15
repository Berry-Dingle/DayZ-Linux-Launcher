#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Callable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JoinServerIdentity:
    ip: str
    game_port: int
    query_port: int
    name: str

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.game_port}"


@dataclass
class JoinAttempt:
    attempt_id: int
    server: JoinServerIdentity
    clicked_wall: float
    clicked_monotonic: float
    skip_dayz_launcher: bool = True
    phase: str = "preparing"
    popup_state: str = "checking"
    genuine_mod_work: bool = False
    active_mod_id: int | None = None
    download_counter_initialized: bool = False
    download_work_ids: tuple[int, ...] = ()
    download_work_total: int = 0
    download_ordinals: dict[int, int] = field(default_factory=dict)
    download_next_ordinal: int = 1
    download_display_ordinal: int = 0
    download_duplicate_logged: set[int] = field(default_factory=set)
    popup_closed: bool = False
    success_process: str = ""
    cleanup_reason: str = ""
    transitions: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class JoinDownloadOrdinal:
    status: str
    assigned_ordinal: int = 0
    display_ordinal: int = 0
    total: int = 0
    transition: str = ""
    should_log_duplicate: bool = False


class JoinAttemptTracker:
    """Process-local Join diagnostics and stale-callback guard."""

    def __init__(self, *, log_sink: Callable[[str], None] | None = None,
                 wall_clock: Callable[[], float] = time.time,
                 monotonic_clock: Callable[[], float] = time.monotonic):
        self._log_sink = log_sink or logger.debug
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._lock = threading.Lock()
        self._next_id = 1
        self._active: JoinAttempt | None = None
        self._closed = False

    @property
    def active(self) -> JoinAttempt | None:
        with self._lock:
            return self._active

    def begin(self, *, ip: str, game_port: int, query_port: int, name: str,
              skip_dayz_launcher: bool = True) -> JoinAttempt | None:
        with self._lock:
            if self._closed or self._active is not None:
                return None
            attempt = JoinAttempt(
                attempt_id=self._next_id,
                server=JoinServerIdentity(str(ip or ""), int(game_port), int(query_port), str(name or "")),
                clicked_wall=float(self._wall_clock()),
                clicked_monotonic=float(self._monotonic_clock()),
                skip_dayz_launcher=bool(skip_dayz_launcher),
            )
            self._next_id += 1
            self._active = attempt
        self.log(attempt.attempt_id, "join click received", server=attempt.server.key,
                 query_port=attempt.server.query_port, name=attempt.server.name)
        return attempt

    def set_popup_state(self, attempt_id: int, state: str) -> bool:
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return False
            self._active.popup_state = str(state)
            return True

    def initialize_download_counter(self, attempt_id: int, mod_ids) -> tuple[bool, int]:
        ids = []
        seen = set()
        for raw in mod_ids or []:
            try:
                mid = int(raw)
            except Exception:
                continue
            if mid > 0 and mid not in seen:
                ids.append(mid)
                seen.add(mid)
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return False, 0
            active = self._active
            if active.download_counter_initialized:
                return False, active.download_work_total
            active.download_counter_initialized = True
            active.download_work_ids = tuple(ids)
            active.download_work_total = len(ids)
            active.download_ordinals.clear()
            active.download_next_ordinal = 1
            active.download_display_ordinal = 0
            active.download_duplicate_logged.clear()
            active.active_mod_id = None
            return True, active.download_work_total

    def note_genuine_mod_work(self, attempt_id: int, mod_id: int) -> JoinDownloadOrdinal:
        """Assign a monotonic presentation ordinal for genuine item activity."""
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return JoinDownloadOrdinal("stale")
            active = self._active
            mid = int(mod_id)
            if not active.download_counter_initialized or active.download_work_total <= 0:
                return JoinDownloadOrdinal("uninitialized")
            if mid not in active.download_work_ids:
                return JoinDownloadOrdinal("not-in-work-set", total=active.download_work_total)
            previous = active.active_mod_id
            active.genuine_mod_work = True
            active.active_mod_id = mid
            active.popup_state = "active-download"
            transition = "first" if previous is None else ("changed" if previous != mid else "same")

            ordinal = active.download_ordinals.get(mid)
            if ordinal is None:
                if active.download_next_ordinal > active.download_work_total:
                    return JoinDownloadOrdinal(
                        "overflow", total=active.download_work_total, transition=transition
                    )
                ordinal = active.download_next_ordinal
                active.download_ordinals[mid] = ordinal
                active.download_next_ordinal += 1
                active.download_display_ordinal = max(active.download_display_ordinal, ordinal)
                return JoinDownloadOrdinal(
                    "assigned", ordinal, active.download_display_ordinal,
                    active.download_work_total, transition,
                )

            # Re-visiting an earlier item retains its assignment internally, while
            # the user-facing numerator remains at the monotonic high-water mark.
            active.download_display_ordinal = max(active.download_display_ordinal, ordinal)
            if previous != mid:
                return JoinDownloadOrdinal(
                    "reused", ordinal, active.download_display_ordinal,
                    active.download_work_total, transition,
                )

            should_log = mid not in active.download_duplicate_logged
            if should_log:
                active.download_duplicate_logged.add(mid)
            return JoinDownloadOrdinal(
                "duplicate", ordinal, active.download_display_ordinal,
                active.download_work_total, transition, should_log,
            )

    def close_popup(self, attempt_id: int, process: str) -> bool:
        """Mark the matching attempt's popup closed exactly once."""
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return False
            if self._active.popup_closed:
                return False
            self._active.popup_closed = True
            self._active.popup_state = "closed"
            self._active.success_process = str(process)
            return True

    def matches(self, attempt_id: int) -> bool:
        with self._lock:
            return bool(not self._closed and self._active is not None
                        and self._active.attempt_id == int(attempt_id))

    def log(self, attempt_id: int, event: str, **fields) -> bool:
        with self._lock:
            attempt = self._active
            if attempt is None or attempt.attempt_id != int(attempt_id):
                return False
            elapsed = max(0.0, float(self._monotonic_clock()) - attempt.clicked_monotonic)
            attempt.transitions.append((str(event), elapsed))
        wall = datetime.fromtimestamp(float(self._wall_clock()), tz=timezone.utc).isoformat(timespec="milliseconds")
        details = " ".join(f"{key}={value!r}" for key, value in fields.items())
        self._log_sink(f"[join:{attempt_id}] wall={wall} elapsed={elapsed:.3f}s {event}"
                       + (f" {details}" if details else ""))
        return True

    def set_phase(self, attempt_id: int, phase: str) -> bool:
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return False
            self._active.phase = str(phase)
            return True

    def cleanup(self, attempt_id: int, reason: str) -> bool:
        with self._lock:
            active = self._active
            counter_total = (
                active.download_work_total
                if active is not None and active.attempt_id == int(attempt_id)
                else 0
            )
        if counter_total:
            self.log(
                attempt_id,
                "download presentation counter cleared",
                total=counter_total,
                reason=str(reason),
            )
        self.log(attempt_id, "active attempt cleanup", reason=str(reason))
        with self._lock:
            if self._active is None or self._active.attempt_id != int(attempt_id):
                return False
            self._active.cleanup_reason = str(reason)
            self._active.phase = "complete"
            self._active.download_counter_initialized = False
            self._active.download_work_ids = ()
            self._active.download_work_total = 0
            self._active.download_ordinals.clear()
            self._active.download_next_ordinal = 1
            self._active.download_display_ordinal = 0
            self._active.download_duplicate_logged.clear()
            self._active.active_mod_id = None
            self._active = None
            return True

    def close(self, reason: str = "application shutdown") -> JoinAttempt | None:
        with self._lock:
            attempt = self._active
        if attempt is not None:
            self.cleanup(attempt.attempt_id, reason)
        with self._lock:
            self._closed = True
        return attempt
