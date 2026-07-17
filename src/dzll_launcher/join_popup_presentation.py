#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class JoinPopupPhase(Enum):
    CHECKING = 10
    WAITING_STEAM = 15
    PREPARING_MODS = 20
    DOWNLOADING = 30
    UPDATING = 31
    FINALISING = 40
    PREPARING_LAUNCH = 50
    LAUNCHING = 60
    CANCELLING = 90
    ERROR = 100

    @property
    def rank(self) -> int:
        if self in (JoinPopupPhase.DOWNLOADING, JoinPopupPhase.UPDATING):
            return 30
        if self == JoinPopupPhase.WAITING_STEAM:
            return JoinPopupPhase.CHECKING.value
        return int(self.value)


@dataclass(frozen=True)
class JoinPopupPresentation:
    attempt_id: int
    backend: str
    phase: JoinPopupPhase
    text: str
    item_key: int | str | None = None


@dataclass
class JoinPopupItemActivity:
    initial_classification: str = "ambiguous"
    request_attempted: bool = False
    request_accepted: bool = False
    meaningful_byte_progress: bool = False
    last_download_bytes: int | None = None
    work_confirmed: bool = False
    display_action: str = ""
    logged_suppressions: set[str] = field(default_factory=set)
    authorisation_logged: bool = False
    byte_progress_logged: bool = False


@dataclass(frozen=True)
class JoinPopupActivityOutcome:
    presentation: JoinPopupPresentation | None
    suppressed_reason: str = ""
    authorised_reason: str = ""
    bytes_advanced: bool = False
    work_confirmed: bool = False
    should_log: bool = False


class JoinPopupActivityTracker:
    """Presentation-only evidence for visible UGC item activity."""

    def __init__(self):
        self._items: dict[tuple[int, int], JoinPopupItemActivity] = {}

    def clear_attempt(self, attempt_id: int) -> None:
        attempt_id = int(attempt_id or 0)
        self._items = {
            key: value for key, value in self._items.items()
            if key[0] != attempt_id
        }

    def get(self, attempt_id: int, item_id: int) -> JoinPopupItemActivity | None:
        return self._items.get((int(attempt_id), int(item_id)))

    def observe(self, event: dict) -> JoinPopupActivityOutcome:
        event = dict(event or {})
        try:
            attempt_id = int(event.get("join_attempt_id") or 0)
            mid = int(event.get("id") or 0)
        except Exception:
            attempt_id = 0
            mid = 0
        backend_owner = str(event.get("backend_owner") or "")
        if attempt_id <= 0 or mid <= 0 or backend_owner != "steam_client":
            return JoinPopupActivityOutcome(None, "invalid ownership", should_log=False)

        key = (attempt_id, mid)
        activity = self._items.setdefault(key, JoinPopupItemActivity())
        source = str(event.get("event_source") or "ambiguous").strip().lower()
        installed = bool(event.get("installed", False))
        needs_update = bool(event.get("needs_update", False))
        ready = bool(event.get("ready", False))
        normalised_missing = bool(event.get("filesystem_normalized_missing", False))

        if activity.last_download_bytes is None:
            activity.last_download_bytes = _event_bytes(event)

        if source in ("initial", "query") and activity.initial_classification == "ambiguous":
            if normalised_missing:
                activity.initial_classification = "ambiguous"
            elif installed and needs_update:
                activity.initial_classification = "outdated"
            elif installed:
                activity.initial_classification = "installed/current"
            elif not installed:
                activity.initial_classification = "missing"

        # A request snapshot carries the pre-request state. Preserve that as a
        # fallback classification if no initial item event was observed.
        if source == "request" and activity.initial_classification == "ambiguous":
            was_installed = bool(event.get("was_installed_before", installed))
            if normalised_missing:
                activity.initial_classification = "ambiguous"
            elif was_installed and needs_update:
                activity.initial_classification = "outdated"
            elif was_installed:
                activity.initial_classification = "installed/current"
            else:
                activity.initial_classification = "missing"

        if bool(event.get("request_attempted", False)) or source == "request":
            activity.request_attempted = True
        if bool(event.get("request_accepted", False)):
            activity.request_accepted = True

        current_bytes = _event_bytes(event)
        previous_bytes = activity.last_download_bytes
        bytes_advanced = previous_bytes is not None and current_bytes > previous_bytes
        activity.last_download_bytes = max(int(previous_bytes or 0), current_bytes)
        if bytes_advanced and not ready and source not in ("initial", "query", "refresh", "final"):
            activity.meaningful_byte_progress = True

        # Request acceptance is queue provenance, not proof that Steam has begun
        # transferring this item.  Keep the intended action, but require later
        # correlated byte advancement before exposing an individual mod name.
        authorised_reason = ""
        if source == "request" and activity.request_accepted:
            if activity.initial_classification == "missing":
                activity.display_action = "download"
            elif activity.initial_classification == "outdated":
                activity.display_action = "update"

        if activity.meaningful_byte_progress and not activity.work_confirmed:
            if activity.initial_classification == "missing":
                activity.display_action = "download"
            elif activity.initial_classification in ("outdated", "installed/current"):
                activity.display_action = "update"
            else:
                activity.display_action = "update" if bool(event.get("was_installed_before", False)) else "download"
            activity.work_confirmed = True
            authorised_reason = "meaningful byte advancement"
        elif bytes_advanced and activity.meaningful_byte_progress:
            authorised_reason = "meaningful byte advancement"

        if ready or source in ("final",):
            return self._suppress(activity, "ready/final snapshot", bytes_advanced)
        if source in ("initial", "query"):
            return self._suppress(activity, "initial/query snapshot", bytes_advanced)
        if source == "refresh":
            return self._suppress(activity, "refresh snapshot", bytes_advanced)
        if source == "request" and activity.request_attempted and not activity.request_accepted:
            return self._suppress(activity, "download request rejected", bytes_advanced)

        if not activity.work_confirmed:
            return self._suppress(activity, "no positive work evidence", bytes_advanced)

        if activity.display_action not in ("download", "update"):
            return self._suppress(activity, "ambiguous display action", bytes_advanced)

        name = str(event.get("name") or mid).strip() or str(mid)
        phase = JoinPopupPhase.UPDATING if activity.display_action == "update" else JoinPopupPhase.DOWNLOADING
        verb = "Updating" if activity.display_action == "update" else "Downloading"
        presentation = JoinPopupPresentation(
            attempt_id, "steam_client", phase, f"{verb} {name}…", mid,
        )
        log_authorisation = bool(authorised_reason) and not activity.authorisation_logged
        log_byte_progress = bytes_advanced and not activity.byte_progress_logged
        should_log = log_authorisation or log_byte_progress
        if log_authorisation:
            activity.authorisation_logged = True
        if log_byte_progress:
            activity.byte_progress_logged = True
        return JoinPopupActivityOutcome(
            presentation,
            authorised_reason=authorised_reason or "confirmed item work",
            bytes_advanced=bytes_advanced,
            work_confirmed=True,
            should_log=should_log,
        )

    @staticmethod
    def _suppress(activity: JoinPopupItemActivity, reason: str,
                  bytes_advanced: bool) -> JoinPopupActivityOutcome:
        should_log = reason not in activity.logged_suppressions
        if should_log:
            activity.logged_suppressions.add(reason)
        return JoinPopupActivityOutcome(
            None,
            suppressed_reason=reason,
            bytes_advanced=bytes_advanced,
            work_confirmed=activity.work_confirmed,
            should_log=should_log,
        )


def _event_bytes(event: dict) -> int:
    try:
        return max(0, int(event.get("download_bytes") or 0))
    except Exception:
        return 0


def ugc_activity_presentation(event: dict, *, attempt_id: int,
                              tracker: JoinPopupActivityTracker | None = None) -> JoinPopupPresentation | None:
    """Return an item phase only after attempt-correlated positive work evidence."""
    event = dict(event or {})
    event.setdefault("join_attempt_id", int(attempt_id or 0))
    event.setdefault("backend_owner", "steam_client")
    return (tracker or JoinPopupActivityTracker()).observe(event).presentation


class JoinPopupPresentationController:
    """Coalesce normal popup states while rendering urgent states immediately."""

    def __init__(self, *, schedule: Callable[[Callable[[], bool]], int],
                 commit: Callable[[JoinPopupPresentation], None],
                 is_current_attempt: Callable[[int], bool]):
        self._schedule = schedule
        self._commit = commit
        self._is_current_attempt = is_current_attempt
        self._pending: JoinPopupPresentation | None = None
        self._rendered: JoinPopupPresentation | None = None
        self._source_id = 0
        self._highest_phase_by_attempt: dict[int, int] = {}

    @property
    def pending(self) -> JoinPopupPresentation | None:
        return self._pending

    @property
    def rendered(self) -> JoinPopupPresentation | None:
        return self._rendered

    def request(self, state: JoinPopupPresentation) -> bool:
        if not self._is_current_attempt(int(state.attempt_id)):
            return False
        highest = self._highest_phase_by_attempt.get(int(state.attempt_id), -1)
        if state.phase.rank < highest:
            return False
        self._highest_phase_by_attempt[int(state.attempt_id)] = max(highest, state.phase.rank)
        if state == self._pending:
            return False
        if state == self._rendered:
            # The newest state has returned to what is already on screen.  Drop
            # any intermediate state queued earlier in this same main-loop turn.
            self._pending = None
            return False
        self._pending = state
        if not self._source_id:
            self._source_id = int(self._schedule(self._flush) or 0)
        return True

    def render_immediate(self, state: JoinPopupPresentation) -> bool:
        if not self._is_current_attempt(int(state.attempt_id)):
            return False
        self._pending = None
        self._highest_phase_by_attempt[int(state.attempt_id)] = max(
            self._highest_phase_by_attempt.get(int(state.attempt_id), -1), state.phase.rank
        )
        if state == self._rendered:
            return False
        self._rendered = state
        self._commit(state)
        return True

    def invalidate_pending(self) -> None:
        self._pending = None

    def reset(self) -> None:
        self._pending = None
        self._rendered = None
        self._highest_phase_by_attempt.clear()

    def _flush(self) -> bool:
        self._source_id = 0
        state = self._pending
        self._pending = None
        if state is None or not self._is_current_attempt(int(state.attempt_id)):
            return False
        if state == self._rendered:
            return False
        self._rendered = state
        self._commit(state)
        return False
