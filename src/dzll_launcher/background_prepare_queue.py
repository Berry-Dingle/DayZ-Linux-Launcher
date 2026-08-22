"""In-memory FIFO ownership for serialized background server preparation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
import threading

from .background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    SingleServerBackgroundPreparation,
)
from .preparation_contracts import PreparationOutcome, PreparationStatus


class BackgroundServerState(Enum):
    IDLE = "idle"
    QUEUED = "queued"
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BackgroundPreparationRequest:
    request_id: int
    batch_id: int
    epoch: int
    identity: str
    snapshot: BackgroundServerPreparationSnapshot
    runtime: BackgroundPreparationRuntime
    batch_order: int
    display_name: str


@dataclass(frozen=True)
class BackgroundServerPreparationRecord:
    identity: str
    request_id: int = 0
    batch_id: int = 0
    state: BackgroundServerState = BackgroundServerState.IDLE
    display_name: str = ""
    batch_order: int = 0
    progress: object | None = None
    outcome: PreparationOutcome | None = None
    error: str = ""


@dataclass(frozen=True)
class BackgroundBatchEntry:
    identity: str
    request_id: int
    batch_id: int
    batch_order: int
    display_name: str
    state: BackgroundServerState
    outcome: PreparationOutcome
    error: str = ""


@dataclass(frozen=True)
class BackgroundBatchResult:
    batch_id: int
    entries: tuple[BackgroundBatchEntry, ...]
    cancelled_by_user: bool = False

    @property
    def ready_count(self) -> int:
        return sum(entry.state is BackgroundServerState.READY for entry in self.entries)

    @property
    def failed_count(self) -> int:
        return sum(entry.state is BackgroundServerState.FAILED for entry in self.entries)

    @property
    def cancelled_count(self) -> int:
        return sum(entry.state is BackgroundServerState.CANCELLED for entry in self.entries)

    @property
    def failed_entries(self) -> tuple[BackgroundBatchEntry, ...]:
        return tuple(
            entry for entry in self.entries
            if entry.state is BackgroundServerState.FAILED
        )


@dataclass(frozen=True)
class BackgroundQueueSnapshot:
    epoch: int
    active_batch_id: int
    accepting: bool
    cancelling: bool
    shutdown: bool
    active: BackgroundPreparationRequest | None
    pending: tuple[BackgroundPreparationRequest, ...]
    records: tuple[BackgroundServerPreparationRecord, ...]
    completed_batch: BackgroundBatchResult | None
    dispatch_reserved: bool
    blocked_reap_failure: bool = False
    blocked_error: str = ""

    @property
    def busy(self) -> bool:
        return bool(
            self.active is not None
            or self.pending
            or self.dispatch_reserved
            or self.cancelling
        )


@dataclass(frozen=True)
class BackgroundQueueTransition:
    snapshot: BackgroundQueueSnapshot
    accepted: bool = False
    request: BackgroundPreparationRequest | None = None
    dispatch: BackgroundPreparationRequest | None = None
    completed: BackgroundBatchEntry | None = None
    controller_to_cancel: SingleServerBackgroundPreparation | None = None


@dataclass(frozen=True)
class BackgroundRetryItem:
    batch_order: int
    snapshot: BackgroundServerPreparationSnapshot
    runtime: BackgroundPreparationRuntime


@dataclass(frozen=True)
class BackgroundRetrySetupFailure:
    batch_order: int
    identity: str
    display_name: str
    error: str


class BackgroundPreparationQueue:
    """Authoritative non-GTK state for one serialized background batch."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: deque[BackgroundPreparationRequest] = deque()
        self._active: BackgroundPreparationRequest | None = None
        self._active_controller: SingleServerBackgroundPreparation | None = None
        self._records: dict[str, BackgroundServerPreparationRecord] = {}
        self._batch_entries: dict[int, BackgroundBatchEntry] = {}
        self._next_request_id = 0
        self._next_batch_id = 0
        self._active_batch_id = 0
        self._next_batch_order = 0
        self._epoch = 0
        self._accepting = True
        self._cancelling = False
        self._shutdown = False
        self._completed_batch: BackgroundBatchResult | None = None
        self._blocked_reap_failure = False
        self._blocked_error = ""

    def _snapshot_locked(self) -> BackgroundQueueSnapshot:
        return BackgroundQueueSnapshot(
            epoch=self._epoch,
            active_batch_id=self._active_batch_id,
            accepting=self._accepting,
            cancelling=self._cancelling,
            shutdown=self._shutdown,
            active=self._active,
            pending=tuple(self._pending),
            records=tuple(self._records.values()),
            completed_batch=self._completed_batch,
            dispatch_reserved=bool(
                self._active is not None and self._active_controller is None
            ),
            blocked_reap_failure=self._blocked_reap_failure,
            blocked_error=self._blocked_error,
        )

    def snapshot(self) -> BackgroundQueueSnapshot:
        with self._lock:
            return self._snapshot_locked()

    @property
    def busy(self) -> bool:
        return self.snapshot().busy

    @property
    def accepting(self) -> bool:
        return self.snapshot().accepting

    def record_for(self, identity: str) -> BackgroundServerPreparationRecord:
        key = str(identity or "")
        with self._lock:
            return self._records.get(key, BackgroundServerPreparationRecord(key))

    def _clear_completed_locked(self) -> None:
        completed = self._completed_batch
        if completed is not None:
            for entry in completed.entries:
                record = self._records.get(entry.identity)
                if (
                    record is not None
                    and record.batch_id == entry.batch_id
                    and record.request_id == entry.request_id
                    and record.state in {
                        BackgroundServerState.READY,
                        BackgroundServerState.FAILED,
                        BackgroundServerState.CANCELLED,
                    }
                ):
                    self._records.pop(entry.identity, None)
        self._completed_batch = None

    def clear_completed_batch(self) -> BackgroundQueueTransition:
        with self._lock:
            if self._active is not None or self._pending or self._cancelling:
                return BackgroundQueueTransition(self._snapshot_locked())
            self._clear_completed_locked()
            return BackgroundQueueTransition(self._snapshot_locked(), accepted=True)

    def _begin_batch_locked(self) -> None:
        self._clear_completed_locked()
        self._next_batch_id += 1
        self._active_batch_id = self._next_batch_id
        self._next_batch_order = 0
        self._batch_entries = {}
        self._epoch += 1

    def _ensure_batch_locked(self) -> None:
        if self._active_batch_id == 0:
            self._begin_batch_locked()

    def _allocate_request_locked(
            self, snapshot: BackgroundServerPreparationSnapshot,
            runtime: BackgroundPreparationRuntime, batch_order: int | None = None,
    ) -> BackgroundPreparationRequest:
        self._next_request_id += 1
        if batch_order is None:
            batch_order = self._next_batch_order
        order = int(batch_order)
        self._next_batch_order = max(self._next_batch_order, order + 1)
        return BackgroundPreparationRequest(
            request_id=self._next_request_id,
            batch_id=self._active_batch_id,
            epoch=self._epoch,
            identity=snapshot.identity,
            snapshot=snapshot,
            runtime=runtime,
            batch_order=order,
            display_name=snapshot.name,
        )

    def _promote_locked(self) -> BackgroundPreparationRequest | None:
        if self._active is not None or self._cancelling or self._shutdown:
            return None
        if not self._pending:
            return None
        request = self._pending.popleft()
        self._active = request
        self._active_controller = None
        record = self._records[request.identity]
        self._records[request.identity] = replace(
            record,
            state=BackgroundServerState.PREPARING,
            progress=None,
            outcome=None,
            error="",
        )
        return request

    def enqueue(
            self, snapshot: BackgroundServerPreparationSnapshot,
            runtime: BackgroundPreparationRuntime,
    ) -> BackgroundQueueTransition:
        with self._lock:
            if not self._accepting or self._shutdown or self._cancelling:
                return BackgroundQueueTransition(self._snapshot_locked())
            identity = snapshot.identity
            record = self._records.get(identity)
            if record is not None and record.state in {
                BackgroundServerState.QUEUED,
                BackgroundServerState.PREPARING,
            }:
                return BackgroundQueueTransition(self._snapshot_locked())
            self._ensure_batch_locked()
            request = self._allocate_request_locked(snapshot, runtime)
            self._records[identity] = BackgroundServerPreparationRecord(
                identity=identity,
                request_id=request.request_id,
                batch_id=request.batch_id,
                state=BackgroundServerState.QUEUED,
                display_name=request.display_name,
                batch_order=request.batch_order,
            )
            self._pending.append(request)
            dispatch = self._promote_locked()
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                request=request,
                dispatch=dispatch,
            )

    def enqueue_retry_batch(
            self, items: tuple[BackgroundRetryItem, ...],
            setup_failures: tuple[BackgroundRetrySetupFailure, ...] = (),
    ) -> BackgroundQueueTransition:
        with self._lock:
            if (
                not self._accepting
                or self._shutdown
                or self._cancelling
                or self._active is not None
                or self._pending
            ):
                return BackgroundQueueTransition(self._snapshot_locked())
            self._begin_batch_locked()
            ordered = [
                (item.batch_order, "item", item) for item in items
            ] + [
                (failure.batch_order, "failure", failure)
                for failure in setup_failures
            ]
            last_request = None
            for order, kind, value in sorted(ordered, key=lambda entry: entry[0]):
                if kind == "item":
                    request = self._allocate_request_locked(
                        value.snapshot, value.runtime, order,
                    )
                    last_request = request
                    self._records[request.identity] = BackgroundServerPreparationRecord(
                        identity=request.identity,
                        request_id=request.request_id,
                        batch_id=request.batch_id,
                        state=BackgroundServerState.QUEUED,
                        display_name=request.display_name,
                        batch_order=request.batch_order,
                    )
                    self._pending.append(request)
                else:
                    self._next_request_id += 1
                    request_id = self._next_request_id
                    error = str(value.error or "Could not resolve server for retry.")
                    outcome = PreparationOutcome(
                        PreparationStatus.FAILED,
                        reason="retry_snapshot_unavailable",
                        error=error,
                    )
                    record = BackgroundServerPreparationRecord(
                        identity=value.identity,
                        request_id=request_id,
                        batch_id=self._active_batch_id,
                        state=BackgroundServerState.FAILED,
                        display_name=value.display_name,
                        batch_order=int(order),
                        outcome=outcome,
                        error=error,
                    )
                    self._records[value.identity] = record
                    self._batch_entries[request_id] = BackgroundBatchEntry(
                        identity=record.identity,
                        request_id=request_id,
                        batch_id=record.batch_id,
                        batch_order=record.batch_order,
                        display_name=record.display_name,
                        state=record.state,
                        outcome=outcome,
                        error=error,
                    )
            dispatch = self._promote_locked()
            if dispatch is None:
                self._finalize_batch_locked(cancelled_by_user=False)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                request=last_request,
                dispatch=dispatch,
            )

    def is_current(self, request: BackgroundPreparationRequest) -> bool:
        with self._lock:
            return self._is_current_locked(request)

    def _is_current_locked(self, request: BackgroundPreparationRequest) -> bool:
        return bool(
            not self._shutdown
            and not self._cancelling
            and request.epoch == self._epoch
            and self._active == request
        )

    def attach_controller(
            self, request: BackgroundPreparationRequest,
            controller: SingleServerBackgroundPreparation,
    ) -> bool:
        with self._lock:
            if not self._is_current_locked(request):
                return False
            self._active_controller = controller
            return True

    def update_progress(self, request: BackgroundPreparationRequest, progress) -> bool:
        with self._lock:
            if not self._is_current_locked(request):
                return False
            record = self._records.get(request.identity)
            if record is None or record.request_id != request.request_id:
                return False
            self._records[request.identity] = replace(record, progress=progress)
            return True

    def observe_terminal(
            self, request: BackgroundPreparationRequest,
            outcome: PreparationOutcome,
    ) -> bool:
        with self._lock:
            if not self._is_current_locked(request):
                return False
            record = self._records.get(request.identity)
            if record is None or record.request_id != request.request_id:
                return False
            self._records[request.identity] = replace(
                record,
                outcome=outcome,
                error=str(outcome.error or outcome.reason or ""),
            )
            return True

    @staticmethod
    def _terminal_state(outcome: PreparationOutcome) -> BackgroundServerState:
        if outcome.status in {PreparationStatus.READY, PreparationStatus.NO_REQUIRED_MODS}:
            return BackgroundServerState.READY
        if outcome.status is PreparationStatus.CANCELLED:
            return BackgroundServerState.CANCELLED
        return BackgroundServerState.FAILED

    def _finalize_batch_locked(self, *, cancelled_by_user: bool) -> None:
        entries = tuple(sorted(
            self._batch_entries.values(),
            key=lambda entry: (entry.batch_order, entry.request_id),
        ))
        batch_id = self._active_batch_id
        self._completed_batch = BackgroundBatchResult(
            batch_id=batch_id,
            entries=entries,
            cancelled_by_user=bool(cancelled_by_user),
        )
        self._active_batch_id = 0
        self._next_batch_order = 0
        self._batch_entries = {}

    def finish(
            self, request: BackgroundPreparationRequest,
            outcome: PreparationOutcome,
    ) -> BackgroundQueueTransition:
        with self._lock:
            if self._active != request:
                return BackgroundQueueTransition(self._snapshot_locked())
            cancelling = self._cancelling
            if request.epoch != self._epoch and not (cancelling or self._shutdown):
                return BackgroundQueueTransition(self._snapshot_locked())
            record = self._records.get(request.identity)
            if record is None or record.request_id != request.request_id:
                return BackgroundQueueTransition(self._snapshot_locked())
            if cancelling or self._shutdown:
                outcome = PreparationOutcome(
                    PreparationStatus.CANCELLED,
                    reason="shutdown" if self._shutdown else "batch_cancelled",
                    error=str(outcome.error or "Background preparation cancelled."),
                    backend=outcome.backend,
                    effective_workshop_path=outcome.effective_workshop_path,
                    did_work=outcome.did_work,
                )
            state = self._terminal_state(outcome)
            completed_record = replace(
                record,
                state=state,
                progress=None,
                outcome=outcome,
                error=str(outcome.error or outcome.reason or ""),
            )
            self._records[request.identity] = completed_record
            completed = BackgroundBatchEntry(
                identity=completed_record.identity,
                request_id=completed_record.request_id,
                batch_id=completed_record.batch_id,
                batch_order=completed_record.batch_order,
                display_name=completed_record.display_name,
                state=completed_record.state,
                outcome=outcome,
                error=completed_record.error,
            )
            self._batch_entries[request.request_id] = completed
            self._active = None
            self._active_controller = None
            dispatch = None
            if cancelling:
                self._cancelling = False
                self._accepting = True
                self._finalize_batch_locked(cancelled_by_user=True)
            elif self._shutdown:
                self._active_batch_id = 0
                self._batch_entries = {}
            else:
                dispatch = self._promote_locked()
                if dispatch is None:
                    self._finalize_batch_locked(cancelled_by_user=False)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                dispatch=dispatch,
                completed=completed,
            )

    def finish_blocked_reap_failure(
            self, request: BackgroundPreparationRequest,
            outcome: PreparationOutcome,
    ) -> BackgroundQueueTransition:
        """Terminalize the active item without dispatching while Steam is unsafe."""

        with self._lock:
            if self._active != request:
                return BackgroundQueueTransition(self._snapshot_locked())
            record = self._records.get(request.identity)
            if record is None or record.request_id != request.request_id:
                return BackgroundQueueTransition(self._snapshot_locked())
            completed_record = replace(
                record,
                state=BackgroundServerState.FAILED,
                progress=None,
                outcome=outcome,
                error=str(outcome.error or outcome.reason or ""),
            )
            self._records[request.identity] = completed_record
            completed = BackgroundBatchEntry(
                identity=completed_record.identity,
                request_id=completed_record.request_id,
                batch_id=completed_record.batch_id,
                batch_order=completed_record.batch_order,
                display_name=completed_record.display_name,
                state=completed_record.state,
                outcome=outcome,
                error=completed_record.error,
            )
            self._batch_entries[request.request_id] = completed
            self._active = None
            self._active_controller = None
            self._accepting = False
            self._cancelling = False
            self._blocked_reap_failure = True
            self._blocked_error = completed_record.error
            if not self._pending:
                self._finalize_batch_locked(cancelled_by_user=False)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                completed=completed,
            )

    def clear_reap_block(self) -> BackgroundQueueTransition:
        """Resume dispatch only after the shared preparation gate recovered."""

        with self._lock:
            if not self._blocked_reap_failure:
                return BackgroundQueueTransition(self._snapshot_locked())
            self._blocked_reap_failure = False
            self._blocked_error = ""
            self._accepting = not self._shutdown
            dispatch = self._promote_locked() if not self._shutdown else None
            if (
                dispatch is None
                and not self._shutdown
                and self._active_batch_id != 0
                and not self._pending
            ):
                self._finalize_batch_locked(cancelled_by_user=False)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                dispatch=dispatch,
            )

    def cancel_blocked_pending(self) -> BackgroundQueueTransition:
        """Clear queued work without pretending the terminal helper is cancelling."""

        with self._lock:
            if not self._blocked_reap_failure:
                return BackgroundQueueTransition(self._snapshot_locked())
            while self._pending:
                pending = self._pending.popleft()
                record = self._records.get(pending.identity)
                if record is not None and record.request_id == pending.request_id:
                    self._records.pop(pending.identity, None)
            if self._active_batch_id != 0:
                self._finalize_batch_locked(cancelled_by_user=True)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
            )

    def submission_failed(
            self, request: BackgroundPreparationRequest, error: BaseException,
    ) -> BackgroundQueueTransition:
        return self.finish(request, PreparationOutcome(
            PreparationStatus.FAILED,
            reason="worker_submission_failed",
            error=f"Could not start mod preparation: {error}",
            backend=request.runtime.mod_download_backend,
        ))

    def cancel_all(self) -> BackgroundQueueTransition:
        with self._lock:
            snapshot = self._snapshot_locked()
            if self._shutdown or self._cancelling or not snapshot.busy:
                return BackgroundQueueTransition(snapshot)
            self._cancelling = True
            self._accepting = False
            self._epoch += 1
            while self._pending:
                pending = self._pending.popleft()
                record = self._records.get(pending.identity)
                if record is not None and record.request_id == pending.request_id:
                    self._records.pop(pending.identity, None)
            controller = self._active_controller
            completed = None
            if self._active is not None and controller is None:
                active = self._active
                outcome = PreparationOutcome(
                    PreparationStatus.CANCELLED,
                    reason="batch_cancelled",
                    error="Background preparation cancelled before it started.",
                    backend=active.runtime.mod_download_backend,
                )
                record = self._records[active.identity]
                record = replace(
                    record,
                    state=BackgroundServerState.CANCELLED,
                    outcome=outcome,
                    error=outcome.error,
                )
                self._records[active.identity] = record
                completed = BackgroundBatchEntry(
                    identity=record.identity,
                    request_id=record.request_id,
                    batch_id=record.batch_id,
                    batch_order=record.batch_order,
                    display_name=record.display_name,
                    state=record.state,
                    outcome=outcome,
                    error=record.error,
                )
                self._batch_entries[active.request_id] = completed
                self._active = None
                self._cancelling = False
                self._accepting = True
                self._finalize_batch_locked(cancelled_by_user=True)
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                completed=completed,
                controller_to_cancel=controller,
            )

    def shutdown(self) -> BackgroundQueueTransition:
        with self._lock:
            if self._shutdown:
                return BackgroundQueueTransition(self._snapshot_locked())
            self._shutdown = True
            self._accepting = False
            self._cancelling = False
            self._epoch += 1
            while self._pending:
                pending = self._pending.popleft()
                record = self._records.get(pending.identity)
                if record is not None and record.request_id == pending.request_id:
                    self._records.pop(pending.identity, None)
            controller = self._active_controller
            if self._active is not None and controller is None:
                self._records.pop(self._active.identity, None)
                self._active = None
                self._active_batch_id = 0
                self._batch_entries = {}
            self._completed_batch = None
            return BackgroundQueueTransition(
                self._snapshot_locked(),
                accepted=True,
                controller_to_cancel=controller,
            )
