"""Shared non-GTK reducer for stable preparation presentation state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .join_attempt import JoinAttemptTracker
from .join_popup_presentation import (
    JoinPopupActivityOutcome,
    JoinPopupActivityTracker,
    JoinPopupPhase,
)
from .preparation_contracts import PreparationProgressEvent


class PreparationProgressMode(Enum):
    HIDDEN = "hidden"
    DETERMINATE = "determinate"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class PreparationPresentationSnapshot:
    operation_id: int
    backend: str
    phase: JoinPopupPhase
    stage_text: str
    current_item_id: int = 0
    current_mod_name: str = ""
    item_total_bytes: int = 0
    current: int = 0
    total: int = 0
    progress_mode: PreparationProgressMode = PreparationProgressMode.HIDDEN
    fraction: float | None = None


@dataclass(frozen=True)
class PreparationReduction:
    snapshot: PreparationPresentationSnapshot | None
    activity: JoinPopupActivityOutcome | None = None


class PreparationPresentationReducer:
    """Apply the established Join activity/counter rules to raw events once."""

    def __init__(self, *, operation_id: int, is_current,
                 initialise_counter=None, note_transfer=None,
                 activity_tracker=None):
        self.operation_id = int(operation_id)
        self._is_current = is_current
        self._activity = activity_tracker or JoinPopupActivityTracker()
        self._private_attempts = None
        if initialise_counter is None or note_transfer is None:
            tracker = JoinAttemptTracker(log_sink=lambda _line: None)
            attempt = tracker.begin(ip="", game_port=0, query_port=0, name="presentation")
            self._private_attempts = (tracker, attempt.attempt_id)
            initialise_counter = lambda ids: tracker.initialize_download_counter(
                attempt.attempt_id, ids,
            )
            note_transfer = lambda mid: tracker.note_genuine_mod_work(
                attempt.attempt_id, mid,
            )
        self._initialise_counter = initialise_counter
        self._note_transfer = note_transfer
        self._snapshot: PreparationPresentationSnapshot | None = None

    @property
    def snapshot(self):
        return self._snapshot

    def apply(self, event: PreparationProgressEvent) -> PreparationReduction:
        if not self._is_current(self.operation_id):
            return PreparationReduction(None)
        payload = event.authoritative_payload()
        event_operation = int(event.operation_id or payload.get("join_attempt_id") or 0)
        if event_operation != self.operation_id:
            return PreparationReduction(None)

        event_type = str(payload.get("type") or "")
        if event_type == "presentation_work_set":
            self._initialise_counter(tuple(payload.get("work_ids") or ()))
            return PreparationReduction(None)

        message = str(payload.get("message") or "").strip()
        if event_type == "status" and message == "Checking/Updating Required Mods":
            return PreparationReduction(None)
        if event_type in ("preflight", "status") and message:
            waiting = "waiting" in message.lower() or "starting steam" in message.lower()
            phase = JoinPopupPhase.WAITING_STEAM if waiting else JoinPopupPhase.CHECKING
            text = "Waiting for Steam…" if waiting else "Checking & Preparing Mods for Join..."
            return self._accept(PreparationPresentationSnapshot(
                self.operation_id, "steam_client", phase, text,
            ))
        if event_type == "error" or bool(payload.get("error", False)):
            return self._accept(PreparationPresentationSnapshot(
                self.operation_id, str(event.backend or ""), JoinPopupPhase.ERROR,
                message or "Mod preparation failed.",
            ), immediate=True)

        if str(event.backend_owner or payload.get("backend_owner") or "") != "steam_client":
            return PreparationReduction(None)
        mid = int(event.item_id or 0)
        if mid <= 0:
            return PreparationReduction(None)

        payload["name"] = str(event.item_name or payload.get("name") or "").strip() or str(mid)
        outcome = self._activity.observe(payload)
        if outcome.presentation is None:
            return PreparationReduction(None, outcome)
        counter = self._note_transfer(mid)
        if counter.status not in ("assigned", "reused", "duplicate"):
            return PreparationReduction(None, outcome)

        total_bytes = int(event.total_bytes or 0)
        download_bytes = int(event.downloaded_bytes or 0)
        previous = self._snapshot
        same_item = previous is not None and previous.current_item_id == mid
        if total_bytes > 0:
            fraction = max(0.0, min(1.0, float(download_bytes) / float(total_bytes)))
            mode = PreparationProgressMode.DETERMINATE
        elif same_item and previous.progress_mode is PreparationProgressMode.DETERMINATE:
            fraction = previous.fraction
            mode = previous.progress_mode
            total_bytes = previous.item_total_bytes
        else:
            fraction = None
            mode = PreparationProgressMode.INDETERMINATE

        action = "Updating" if outcome.presentation.phase is JoinPopupPhase.UPDATING else "Downloading"
        snapshot = PreparationPresentationSnapshot(
            operation_id=self.operation_id,
            backend="steam_client",
            phase=JoinPopupPhase.DOWNLOADING,
            stage_text=f"{action} {payload['name']}…",
            current_item_id=mid,
            current_mod_name=payload["name"],
            item_total_bytes=total_bytes,
            current=counter.display_ordinal,
            total=counter.total,
            progress_mode=mode,
            fraction=fraction,
        )
        reduction = self._accept(snapshot)
        return PreparationReduction(reduction.snapshot, outcome)

    def apply_steamcmd_state(self, *, heading: str, line1: str, line2: str,
                             spinning: bool) -> PreparationReduction:
        """Reduce the established SteamCMD parser's semantic overlay state.

        Parsing stays exclusively in ``SteamCMDOverlayUI``.  This adapter only
        deduplicates the already reduced strings and mode that normal Join
        renders, so another presenter never interprets SteamCMD output.
        """
        if not self._is_current(self.operation_id):
            return PreparationReduction(None)
        detail = str(line2 or line1 or heading or "").strip()
        snapshot = PreparationPresentationSnapshot(
            operation_id=self.operation_id,
            backend="steamcmd",
            phase=JoinPopupPhase.DOWNLOADING if spinning else JoinPopupPhase.CHECKING,
            stage_text=detail,
            progress_mode=(
                PreparationProgressMode.INDETERMINATE
                if spinning else PreparationProgressMode.HIDDEN
            ),
        )
        return self._accept(snapshot)

    def _accept(self, snapshot: PreparationPresentationSnapshot, immediate=False):
        previous = self._snapshot
        if previous is not None and not immediate and snapshot.phase.rank < previous.phase.rank:
            return PreparationReduction(None)
        if snapshot == previous:
            return PreparationReduction(None)
        self._snapshot = snapshot
        return PreparationReduction(snapshot)
