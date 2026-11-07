from __future__ import annotations

import collections
import copy
import fcntl
import hashlib
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .companion_restart_phase2_authority_consumers import (
    AuthorityConsumerDecision,
    AuthoritySuppressionKey,
    AuthoritySuppressionKind,
)
from .companion_restart_phase2_consumer_state import (
    AlertActionOutcome,
    AlertLifecycle,
    AutoCompactionPolicy,
    ConsumerAlertRecord,
    alert_record,
    auto_compaction_reasons,
    append_alert,
    bound_consumer_ledger,
    consumer_suppression_set,
    empty_consumer_state,
    key_suppressed,
    latest_alert_records,
    ledger_from_mapping as consumer_ledger_from_mapping,
    ledger_to_mapping as consumer_ledger_to_mapping,
    invalidate_superseded_regime_warnings,
    suppression_key_id,
    with_decision,
)

from .companion_restart_phase2_schema4 import (
    SCHEMA4_ROOT_VERSION,
    Schema4Error,
    Schema4LoadResult,
    Schema4ValidationError,
    Schema4ValidationIssue,
    Schema4ValidationReport,
    Schema4VersionError,
    assemble_schema4_payload,
    canonical_json_bytes,
    compact_schema4_state,
    deduplicate_schema4_snapshot,
    deserialize_schema4_bytes,
    encode_schema4_server_fragment,
    rebuild_schema4_server_record,
    schema_version_from_bytes,
    serialize_schema4_state,
    validate_schema4_envelope,
    validate_schema4_server_fragment,
    validate_schema4_server_record,
    verify_schema4_payload_layout,
)


logger = logging.getLogger(__name__)


AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED_DEFAULT = False
SCHEMA4_RUNTIME_WRITE_SEMANTICS_VERSION = 1
SCHEMA4_RUNTIME_WRITABLE_AUTHORITY_STATUSES = frozenset(
    {"valid_shadow_only", "valid_authoritative_runtime"}
)


class Schema4RuntimeError(Schema4Error):
    """Authoritative schema-4 runtime operation failed safely."""


class Schema4WriterLockError(Schema4RuntimeError):
    """Another runtime, migration apply, or compaction owns the writer lock."""


class Schema4GenerationConflict(Schema4RuntimeError):
    """The active file changed since this runtime loaded it."""


class Schema4ServerWriteRejected(Schema4ValidationError):
    """One server's record cannot be written; the store and other servers are intact.

    Raised before any in-memory or on-disk change for that server: the target
    record is quarantined or unavailable, or its rebuilt record failed
    per-server validation.  It is never raised for root, I/O or whole-store
    failures, which remain global.
    """

    def __init__(
        self,
        server_key: str,
        reason_code: str,
        detail: str,
        *,
        validation_codes: tuple[str, ...] = (),
    ) -> None:
        self.server_key = str(server_key)
        self.reason_code = str(reason_code)
        self.detail = str(detail)
        self.validation_codes = tuple(str(item) for item in validation_codes)
        super().__init__(
            f"schema-4 server {self.server_key!r} {self.reason_code}: {self.detail}"
        )


class Schema4WriteFailurePhase(str, Enum):
    """Commit certainty for a failed schema-4 atomic installation."""

    CONFIRMED_PRE_COMMIT = "confirmed_pre_commit"
    POST_COMMIT_OR_AMBIGUOUS = "post_commit_or_ambiguous"


class Schema4WriteFailure(RuntimeError):
    """An atomic install failed with an explicit commit-phase classification."""

    def __init__(
        self,
        phase: Schema4WriteFailurePhase,
        cause: BaseException,
    ) -> None:
        self.phase = phase
        self.cause = cause
        super().__init__(f"{phase.value}: {type(cause).__name__}: {cause}")


class Schema4CrashPoint(str, Enum):
    BEFORE_TEMP_COMPLETE = "before_temp_complete"
    AFTER_TEMP_FSYNC_BEFORE_REPLACE = "after_temp_fsync_before_replace"
    AFTER_REPLACE = "after_replace"


@dataclass(frozen=True)
class Schema4RecoveryResult:
    stale_temporary_files_removed: tuple[str, ...]
    restored_last_known_good: bool
    preserved_invalid_path: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class Schema4RuntimeSnapshot:
    enabled: bool
    active_path: str
    generation: int
    active_sha256: str | None
    valid_server_keys: tuple[str, ...]
    quarantined_server_keys: tuple[str, ...]
    dirty_server_keys: tuple[str, ...]
    write_count: int
    decision_ids: tuple[tuple[str, str], ...]
    recovery: Schema4RecoveryResult | None


@dataclass(frozen=True)
class Schema4WriteResult:
    wrote: bool
    generation: int
    sha256: str | None
    dirty_server_keys: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class Schema4ExportSnapshot:
    canonical_bytes: bytes
    schema_version: int
    server_count: int
    sha256: str


@dataclass(frozen=True)
class Schema4SoakResult:
    start_count: int
    healthy_poll_count: int
    durable_update_count: int
    write_count: int
    final_sha256: str
    final_generation: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ConsumerAlertReservation:
    reserved: bool
    key: AuthoritySuppressionKey
    record: ConsumerAlertRecord | None
    write_result: Schema4WriteResult | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class Schema4RebuildResult:
    """Outcome of one per-server learning rebuild (R8).

    ``changed`` is True when a semantically different record was installed
    (and the server marked dirty).  ``staged_schema3_bytes`` are the
    canonical bytes of the schema-3 runtime record the rebuild consumed.
    """

    server_key: str
    changed: bool
    staged_schema3_bytes: bytes
    rebuild_seconds: float = 0.0
    rebased: int = 0


# Sentinel for "no installed record" so the install-time identity check can
# distinguish a missing record from any installed object.
_NO_RECORD = object()
_MAX_REBUILD_REBASES = 8


class Schema4WriterLock:
    """Single-process/OS advisory ownership for one schema-4 active file."""

    def __init__(self, active_path: str | os.PathLike[str]) -> None:
        self.active_path = Path(active_path)
        self.path = self.active_path.with_name(self.active_path.name + ".schema4.lock")
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise Schema4WriterLockError(
                f"schema-4 writer lock is already held: {self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "Schema4WriterLock":
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


class Schema4WriteTicket:
    """Durability handle for one flush request.

    Callbacks run on the owner (GTK) thread: immediately in inline mode, or
    from ``process_completions`` once the persistence worker reports.  A
    ticket resolves only after the snapshot that contains the requested state
    was atomically installed (``error is None``) or definitively failed.
    """

    __slots__ = ("_callbacks", "_done", "_worker_done", "result", "error")

    def __init__(self) -> None:
        self._callbacks: list[Callable[["Schema4WriteTicket"], None]] = []
        self._done = False
        self._worker_done = threading.Event()
        self.result: Schema4WriteResult | None = None
        self.error: BaseException | None = None

    @property
    def done(self) -> bool:
        return self._done

    def add_callback(self, callback: Callable[["Schema4WriteTicket"], None]) -> None:
        if self._done:
            _run_ticket_callback(callback, self)
        else:
            self._callbacks.append(callback)

    def _resolve(
        self, result: Schema4WriteResult | None, error: BaseException | None
    ) -> None:
        if self._done:
            return
        self.result = result
        self.error = error
        self._done = True
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            _run_ticket_callback(callback, self)


def _run_ticket_callback(callback, ticket: Schema4WriteTicket) -> None:
    try:
        callback(ticket)
    except Exception:
        logger.exception("Server Companion persistence completion callback failed")


@dataclass
class _WriteJob:
    sequences: list[int]
    root: dict
    tickets: list[Schema4WriteTicket]
    dirty_keys: set[str]


@dataclass
class _RebuildJob:
    """One ordered per-server rebuild (R8).

    ``base`` is the installed (immutable, copy-on-write) record the rebuild
    starts from, or ``_NO_RECORD``.  ``schema3_record`` is a detached copy
    owned by the job.  The worker reads only these two objects.
    """

    server_key: str
    base: object
    schema3_record: dict
    updated_at: float
    durable_reason: str
    ticket: Schema4WriteTicket
    rebased: int = 0


@dataclass(frozen=True)
class _Completion:
    kind: str
    sequences: tuple[int, ...] = ()
    tickets: tuple[Schema4WriteTicket, ...] = ()
    result: object = None
    error: BaseException | None = None
    written_fields: Mapping[str, object] | None = None
    report: Schema4ValidationReport | None = None
    owner_callback: Callable[[object, BaseException | None], None] | None = None
    duration: float = 0.0
    rebuild_job: "_RebuildJob | None" = None


class _Schema4DiskWriter:
    """Disk-side identity and the encoded-fragment cache.

    Used by exactly one thread at a time: the owner thread in inline mode,
    or the persistence worker in background mode.  It reads only immutable
    copy-on-write snapshots and never touches the owner's live state.
    """

    def __init__(
        self,
        backend: "AuthoritativeSchema4Runtime",
        *,
        sha256: str | None,
        generation: int,
    ) -> None:
        self._backend = backend
        self.sha256 = sha256
        self.generation = int(generation)
        # key -> (record object, exact fragment bytes, quarantined)
        self._fragments: dict[str, tuple[object, bytes, bool]] = {}
        self.encoded_fragment_count = 0
        self.bytes_written = 0

    def _fragments_for(
        self, servers: Mapping[str, object]
    ) -> tuple[dict[str, bytes], tuple[str, ...]]:
        fragments: dict[str, bytes] = {}
        quarantined: list[str] = []
        for raw_key, record in servers.items():
            key = str(raw_key)
            cached = self._fragments.get(key)
            if cached is None or cached[0] is not record:
                # Copy-on-write: a record object is never mutated after it is
                # installed, so identity proves the cached bytes are current.
                fragment = encode_schema4_server_fragment(record)
                is_quarantined = validate_schema4_server_fragment(key, fragment)
                cached = (record, fragment, is_quarantined)
                self._fragments[key] = cached
                self.encoded_fragment_count += 1
            fragments[key] = cached[1]
            if cached[2]:
                quarantined.append(key)
        for stale in set(self._fragments) - set(fragments):
            del self._fragments[stale]
        return fragments, tuple(sorted(quarantined))

    def payload_for(self, root: Mapping[str, object]) -> tuple[bytes, Schema4ValidationReport]:
        """Validated canonical bytes for ``root`` (== serialize_schema4_state)."""

        if not isinstance(root.get("servers"), Mapping):
            raise Schema4ValidationError("schema-4 servers must be a mapping")
        disk_root = deduplicate_schema4_snapshot(root)
        validate_schema4_envelope(disk_root)
        fragments, quarantined = self._fragments_for(disk_root["servers"])
        payload = assemble_schema4_payload(disk_root, fragments)
        verify_schema4_payload_layout(payload, disk_root, fragments)
        valid = tuple(sorted(key for key in fragments if key not in quarantined))
        report = Schema4ValidationReport(
            root_valid=True,
            valid_server_keys=valid,
            quarantined_server_keys=quarantined,
            issues=tuple(
                Schema4ValidationIssue(
                    key, "server_quarantined", "server authority record is quarantined"
                )
                for key in quarantined
            ),
        )
        return payload, report

    def write(self, job: _WriteJob) -> _Completion:
        started = time.perf_counter()
        fields = {
            "runtime_write_generation": self.generation + 1,
            "runtime_write_semantics_version": SCHEMA4_RUNTIME_WRITE_SEMANTICS_VERSION,
            "previous_runtime_payload_sha256": self.sha256,
        }
        try:
            write_root = dict(job.root)
            write_root.update(fields)
            payload, report = self.payload_for(write_root)
            dirty = tuple(sorted(job.dirty_keys))
            _install_atomic_schema4(
                self._backend.active_path,
                payload,
                expected_sha256=self.sha256,
                last_known_good_path=self._backend.last_known_good_path,
                crash_injector=self._backend._crash_injector,
            )
        except Exception as exc:
            return _Completion(
                kind="write",
                sequences=tuple(job.sequences),
                tickets=tuple(job.tickets),
                error=exc,
                duration=time.perf_counter() - started,
            )
        self.generation += 1
        self.sha256 = _sha256(payload)
        self.bytes_written += len(payload)
        result = Schema4WriteResult(
            wrote=True,
            generation=self.generation,
            sha256=self.sha256,
            dirty_server_keys=dirty,
            reason_codes=("validated_atomic_replace", "last_known_good_rotated"),
        )
        return _Completion(
            kind="write",
            sequences=tuple(job.sequences),
            tickets=tuple(job.tickets),
            result=result,
            written_fields=fields,
            report=report,
            duration=time.perf_counter() - started,
        )


class _Schema4PersistenceWorker:
    """One ordered background writer with a bounded, coalescing queue.

    At most one write runs and one waits.  A newer snapshot replaces the
    waiting one (it contains every earlier staged change), so queued work is
    bounded, writes are never reordered and stale state can never overwrite
    newer state.  Every coalesced request keeps its ticket.

    R8: the same thread also runs per-server learning rebuilds, strictly in
    submission order and never coalesced (each rebuild advances the learned
    state and decision history, so none may be skipped).  The runtime keeps
    at most one rebuild per server in flight, so this queue is bounded by
    the number of servers with pending work.
    """

    def __init__(self, backend: "AuthoritativeSchema4Runtime") -> None:
        self._backend = backend
        self._cond = threading.Condition()
        self._pending: _WriteJob | None = None
        self._rebuilds: collections.deque = collections.deque()
        self._aux: collections.deque = collections.deque()
        self._busy = False
        self._stopping = False
        self._discard = False
        self.coalesced_requests = 0
        self.completed_writes = 0
        self.max_write_seconds = 0.0
        self.total_write_seconds = 0.0
        self.completed_rebuilds = 0
        self.max_rebuild_seconds = 0.0
        self.total_rebuild_seconds = 0.0
        self.max_rebuild_queue_depth = 0
        self._thread = threading.Thread(
            target=self._run,
            name="dzll-companion-persistence",
            daemon=True,
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def busy(self) -> bool:
        with self._cond:
            return bool(
                self._busy or self._pending is not None or self._aux or self._rebuilds
            )

    def submit_rebuild(self, job: "_RebuildJob") -> None:
        with self._cond:
            if self._stopping:
                raise Schema4RuntimeError("persistence worker is stopping")
            if not self._thread.is_alive():
                raise Schema4RuntimeError("persistence worker stopped unexpectedly")
            self._rebuilds.append(job)
            self.max_rebuild_queue_depth = max(
                self.max_rebuild_queue_depth, len(self._rebuilds)
            )
            self._cond.notify_all()

    def submit(self, job: _WriteJob) -> None:
        with self._cond:
            if self._stopping:
                raise Schema4RuntimeError("persistence worker is stopping")
            if not self._thread.is_alive():
                raise Schema4RuntimeError("persistence worker stopped unexpectedly")
            if self._pending is None:
                self._pending = job
            else:
                self._pending.sequences.extend(job.sequences)
                self._pending.root = job.root
                self._pending.tickets.extend(job.tickets)
                self._pending.dirty_keys.update(job.dirty_keys)
                self.coalesced_requests += 1
            self._cond.notify_all()

    def submit_aux(self, task: Callable[[], _Completion]) -> None:
        with self._cond:
            if self._stopping or not self._thread.is_alive():
                raise Schema4RuntimeError("persistence worker is not running")
            self._aux.append(task)
            self._cond.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._busy or self._pending is not None or self._aux or self._rebuilds:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def stop(self, *, discard_pending: bool) -> None:
        with self._cond:
            self._stopping = True
            self._discard = bool(discard_pending)
            self._cond.notify_all()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._cond:
                while (
                    self._pending is None
                    and not self._aux
                    and not self._rebuilds
                    and not self._stopping
                ):
                    self._cond.wait()
                job = None
                task = None
                rebuild = None
                if self._rebuilds:
                    # Rebuilds first: GTK-thread ordered learning work waits
                    # on them, and a later write then includes their result.
                    rebuild = self._rebuilds.popleft()
                elif self._pending is not None:
                    job, self._pending = self._pending, None
                elif self._aux:
                    task = self._aux.popleft()
                else:
                    return
                self._busy = True
            try:
                if rebuild is not None:
                    if self._discard:
                        completion = _Completion(
                            kind="rebuild",
                            tickets=(rebuild.ticket,),
                            error=Schema4RuntimeError(
                                "persistence stopped before this rebuild was attempted"
                            ),
                            rebuild_job=rebuild,
                        )
                    else:
                        completion = self._backend._compute_server_rebuild(rebuild)
                        self.completed_rebuilds += 1
                        self.max_rebuild_seconds = max(
                            self.max_rebuild_seconds, completion.duration
                        )
                        self.total_rebuild_seconds += completion.duration
                elif job is not None:
                    if self._discard:
                        completion = _Completion(
                            kind="write",
                            sequences=tuple(job.sequences),
                            tickets=tuple(job.tickets),
                            error=Schema4RuntimeError(
                                "persistence stopped before this write was attempted"
                            ),
                        )
                    else:
                        completion = self._backend._writer.write(job)
                        self.completed_writes += 1
                        self.max_write_seconds = max(
                            self.max_write_seconds, completion.duration
                        )
                        self.total_write_seconds += completion.duration
                else:
                    completion = task()
            except BaseException as exc:  # defensive: tasks capture errors
                if rebuild is not None:
                    completion = _Completion(
                        kind="rebuild",
                        tickets=(rebuild.ticket,),
                        error=exc,
                        rebuild_job=rebuild,
                    )
                else:
                    completion = _Completion(
                        kind="write" if job is not None else "aux",
                        sequences=tuple(job.sequences) if job is not None else (),
                        tickets=tuple(job.tickets) if job is not None else (),
                        error=exc,
                    )
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()
            self._backend._deliver_completion(completion)


class AuthoritativeSchema4Runtime:
    """Validated single-writer persistence backend for schema 4.

    It owns persistence only.  During Stage 3A the caller's schema-3 scorer and
    consumers remain the displayed production authority.

    R7: authoritative memory is copy-on-write.  Every mutation installs new
    root/server mappings and never mutates an installed record, so a write
    can serialize a snapshot without copying it.  Mutations validate only the
    changed record (the rest of the state is unchanged and already valid).
    Writes run inline (default) or on one ordered background worker
    (``enable_background_writes``); both use the same validated code path.
    """

    def __init__(
        self,
        *,
        active_path: Path,
        enabled: bool,
        state: dict | None = None,
        load_result: Schema4LoadResult | None = None,
        active_bytes: bytes | None = None,
        writer_lock: Schema4WriterLock | None = None,
        recovery: Schema4RecoveryResult | None = None,
        crash_injector: Callable[[Schema4CrashPoint], None] | None = None,
        _owned_state: bool = False,
    ) -> None:
        self.active_path = Path(active_path)
        self.enabled = bool(enabled)
        if state is not None and not _owned_state:
            state = copy.deepcopy(state)
        if state is not None:
            _share_identical_schema3_entries(state)
        self._state = state
        self._load_result = load_result
        self._expected_sha256 = _sha256(active_bytes) if active_bytes is not None else None
        self._generation = int((state or {}).get("runtime_write_generation", 0) or 0)
        self._writer_lock = writer_lock
        self._mutex = threading.RLock()
        self._dirty_servers: set[str] = set()
        self._root_dirty = False
        self._closed = False
        self._write_count = 0
        self._recovery = recovery
        self._crash_injector = crash_injector
        self._sequence = 0
        self._submitted: dict[int, tuple[frozenset[str], bool]] = {}
        self._last_ticket: Schema4WriteTicket | None = None
        self._writer = _Schema4DiskWriter(
            self, sha256=self._expected_sha256, generation=self._generation
        )
        self._worker: _Schema4PersistenceWorker | None = None
        self._post_to_owner: Callable[[Callable[[], object]], object] | None = None
        self._completions: collections.deque = collections.deque()
        self._completion_ready = threading.Event()
        # R8: rebuild jobs submitted to the worker and not yet applied on
        # the owner thread.
        self._rebuilds_in_flight = 0
        self._rebuild_rebase_count = 0

    @classmethod
    def open(
        cls,
        active_path: str | os.PathLike[str],
        *,
        enabled: bool = AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED_DEFAULT,
        crash_injector: Callable[[Schema4CrashPoint], None] | None = None,
    ) -> "AuthoritativeSchema4Runtime":
        path = Path(active_path)
        if not enabled:
            return cls(active_path=path, enabled=False)
        lock = Schema4WriterLock(path)
        lock.acquire()
        try:
            recovery, raw, loaded = _recover_schema4_path_loaded(path)
            return cls(
                active_path=path,
                enabled=True,
                state=loaded.state,
                load_result=loaded,
                active_bytes=raw,
                writer_lock=lock,
                recovery=recovery,
                crash_injector=crash_injector,
                _owned_state=True,
            )
        except Exception:
            lock.release()
            raise

    @property
    def state(self) -> dict:
        if not self.enabled or self._state is None:
            raise Schema4RuntimeError("authoritative schema-4 runtime is disabled")
        return copy.deepcopy(self._state)

    @property
    def last_known_good_path(self) -> Path:
        return self.active_path.with_name(self.active_path.name + ".last-known-good")

    @property
    def dirty(self) -> bool:
        """True while any staged change is not yet durably installed."""

        return bool(self._dirty_servers or self._root_dirty or self._submitted)

    @property
    def background_writes_enabled(self) -> bool:
        return self._worker is not None

    @property
    def write_in_progress(self) -> bool:
        return bool(self._submitted or self._rebuilds_in_flight) or (
            self._worker is not None and self._worker.busy
        )

    @property
    def rebuild_in_progress(self) -> bool:
        return bool(self._rebuilds_in_flight)

    def persistence_stats(self) -> dict:
        worker = self._worker
        return {
            "background": worker is not None,
            "writes": self._write_count,
            "bytes_written": self._writer.bytes_written,
            "encoded_fragments": self._writer.encoded_fragment_count,
            "coalesced_requests": 0 if worker is None else worker.coalesced_requests,
            "max_write_seconds": 0.0 if worker is None else worker.max_write_seconds,
            "total_write_seconds": 0.0 if worker is None else worker.total_write_seconds,
            "rebuilds": 0 if worker is None else worker.completed_rebuilds,
            "max_rebuild_seconds": 0.0 if worker is None else worker.max_rebuild_seconds,
            "total_rebuild_seconds": (
                0.0 if worker is None else worker.total_rebuild_seconds
            ),
            "max_rebuild_queue_depth": (
                0 if worker is None else worker.max_rebuild_queue_depth
            ),
            "rebuild_rebases": self._rebuild_rebase_count,
        }

    def schema3_projection(self) -> dict:
        """Return the in-memory schema-3 detector/scorer projection only."""

        if not self.enabled or self._state is None:
            raise Schema4RuntimeError("authoritative schema-4 runtime is disabled")
        state = self._state
        snapshot = state.get("schema3_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 3:
            raise Schema4ValidationError("schema-4 state has no valid schema-3 runtime projection")
        projected = {
            key: copy.deepcopy(value)
            for key, value in snapshot.items()
            if key != "servers"
        }
        projected_servers = {}
        records = state.get("servers", {})
        for key, value in (snapshot.get("servers") or {}).items():
            record = records.get(key)
            if record is None:
                projected_servers[key] = copy.deepcopy(value)
        for key, record in records.items():
            if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
                projected_servers.pop(key, None)
                continue
            legacy = record.get("legacy_schema3_record")
            if isinstance(legacy, Mapping):
                projected_servers[key] = copy.deepcopy(dict(legacy))
            elif key in (snapshot.get("servers") or {}):
                projected_servers[key] = copy.deepcopy(snapshot["servers"][key])
        projected["servers"] = projected_servers
        return projected

    def _export_from(self, root: Mapping[str, object]) -> Schema4ExportSnapshot:
        payload, report = self._writer.payload_for(root)
        if not report.valid:
            raise Schema4ValidationError(
                "authoritative export snapshot failed strict validation"
            )
        return Schema4ExportSnapshot(
            canonical_bytes=payload,
            schema_version=SCHEMA4_ROOT_VERSION,
            server_count=len(root.get("servers", {})),
            sha256=_sha256(payload),
        )

    def export_snapshot(self) -> Schema4ExportSnapshot:
        """Return one validated canonical view of current authoritative memory."""

        self._ensure_open()
        if self._worker is None:
            with self._mutex:
                assert self._state is not None
                return self._export_from(self._state)
        box: dict[str, object] = {}
        done = threading.Event()

        def finished(snapshot, error):
            box["snapshot"], box["error"] = snapshot, error
            done.set()

        self.export_snapshot_async(finished)
        while not done.is_set():
            self._completion_ready.wait(0.05)
            self.process_completions()
        if box.get("error") is not None:
            raise box["error"]  # type: ignore[misc]
        return box["snapshot"]  # type: ignore[return-value]

    def export_snapshot_async(
        self,
        callback: Callable[[Schema4ExportSnapshot | None, BaseException | None], None],
    ) -> None:
        """Serialize and validate the current snapshot off the owner thread."""

        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            root = self._state
        if self._worker is None:
            try:
                snapshot = self._export_from(root)
            except Exception as exc:
                callback(None, exc)
            else:
                callback(snapshot, None)
            return

        def task() -> _Completion:
            try:
                result, error = self._export_from(root), None
            except BaseException as exc:
                result, error = None, exc
            return _Completion(
                kind="aux", result=result, error=error, owner_callback=callback
            )

        self._worker.submit_aux(task)

    def authority_decision(self, server_key: str):
        """Return a copied immutable decision for a validated server only."""

        from .companion_restart_phase2_schema4 import persisted_authority_decision

        self._ensure_open()
        assert self._state is not None
        record = self._state.get("servers", {}).get(str(server_key))
        if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
            return None
        return persisted_authority_decision(record)

    def server_authority_valid(self, server_key: str) -> bool:
        self._ensure_open()
        assert self._state is not None
        record = self._state.get("servers", {}).get(str(server_key))
        return bool(
            isinstance(record, Mapping)
            and record.get("authority_status") != "quarantined"
            and isinstance(record.get("authority_decision"), Mapping)
        )

    def consumer_fired_keys(
        self, server_key: str
    ) -> frozenset[AuthoritySuppressionKey]:
        self._ensure_open()
        assert self._state is not None
        record = self._state.get("servers", {}).get(str(server_key))
        if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
            return frozenset()
        ledger = consumer_ledger_from_mapping(
            record.get("consumer_state"), server_key=str(server_key)
        )
        return consumer_suppression_set(
            ledger, event_finalized_at=_event_finalized_at(record)
        )

    def _consumer_record(self, server_key: str) -> Mapping[str, object]:
        assert self._state is not None
        record = self._state.get("servers", {}).get(server_key)
        if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
            raise Schema4ServerWriteRejected(
                server_key,
                "server_quarantined" if isinstance(record, Mapping) else "server_unavailable",
                "consumer server is unavailable or quarantined",
            )
        return record

    def _install_consumer_state(self, server_key: str, ledger) -> None:
        """Copy-on-write install of one server's consumer ledger (validated)."""

        assert self._state is not None
        mapping = consumer_ledger_to_mapping(ledger)
        canonical_json_bytes(mapping)  # JSON-safe before it is trusted
        record = dict(self._state["servers"][server_key])
        record["consumer_state"] = mapping
        self._validate_record_for_server(server_key, record)
        servers = dict(self._state["servers"])
        servers[server_key] = record
        candidate = dict(self._state)
        candidate["servers"] = servers
        self._state = candidate
        self._dirty_servers.add(server_key)

    def record_consumer_decision(
        self, decision: AuthorityConsumerDecision, *, created_at: float
    ) -> bool:
        """Stage a consumer snapshot without forcing poll-by-poll persistence."""

        self._ensure_open()
        with self._mutex:
            record = self._consumer_record(decision.server_key)
            ledger = consumer_ledger_from_mapping(
                record.get("consumer_state"), server_key=decision.server_key
            )
            updated = with_decision(ledger, decision)
            updated = invalidate_superseded_regime_warnings(
                updated,
                active_regime_id=decision.selected_regime_id,
                authority_consumer_decision_id=decision.decision_id,
                created_at=created_at,
            )
            updated = bound_consumer_ledger(
                updated,
                now=created_at,
                event_finalized_at=_event_finalized_at(record),
                protect_decision_ids=(ledger.current_authority_consumer_decision_id,),
            )
            if consumer_ledger_to_mapping(updated) == consumer_ledger_to_mapping(ledger):
                return False
            self._install_consumer_state(decision.server_key, updated)
            return True

    def stage_consumer_alert_reservation(
        self,
        *,
        decision: AuthorityConsumerDecision,
        key: AuthoritySuppressionKey,
        created_at: float,
    ) -> ConsumerAlertReservation:
        """Stage (not yet durable) one alert reservation in authoritative memory.

        The staged reservation immediately suppresses the key in memory, so a
        concurrent request can never dispatch it twice.  The caller must not
        dispatch until a flush covering it has completed successfully.
        """

        self._ensure_open()
        if key.server_key != decision.server_key:
            raise Schema4ValidationError("consumer alert key server mismatch")
        with self._mutex:
            record = self._consumer_record(decision.server_key)
            if key.kind is AuthoritySuppressionKind.GENERIC_RECOVERY and key.event_id not in {
                item.get("event_id")
                for item in record.get("physical_events", ())
                if isinstance(item, Mapping)
            }:
                # Retention R4: an event outside the retained ring was folded
                # and its recovery records pruned; it can never be a live
                # recovery event again, so it is never alertable again.
                return ConsumerAlertReservation(
                    False,
                    key,
                    None,
                    None,
                    ("generic_recovery_event_not_retained", "at_most_once_delivery"),
                )
            ledger = consumer_ledger_from_mapping(
                record.get("consumer_state"), server_key=decision.server_key
            )
            preceding_decision_id = ledger.current_authority_consumer_decision_id
            ledger = with_decision(ledger, decision)
            finalized = _event_finalized_at(record)
            if key_suppressed(ledger, key, event_finalized_at=finalized):
                return ConsumerAlertReservation(
                    False,
                    key,
                    None,
                    None,
                    ("suppressed_already_fired", "at_most_once_delivery"),
                )
            prior = next(
                (
                    item
                    for item in latest_alert_records(ledger)
                    if item.key_id == suppression_key_id(key)
                ),
                None,
            )
            reserved = alert_record(
                key=key,
                authority_consumer_decision_id=decision.decision_id,
                created_at=created_at,
                lifecycle=AlertLifecycle.RESERVED,
                action_outcome=AlertActionOutcome.RESERVED_AT_MOST_ONCE,
                supersedes_record_id=None if prior is None else prior.record_id,
                reason_codes=(
                    "reserved_before_external_action",
                    "at_most_once_crash_policy",
                ),
            )
            ledger = append_alert(ledger, reserved)
            ledger = bound_consumer_ledger(
                ledger,
                now=created_at,
                event_finalized_at=finalized,
                protect_key_ids=(reserved.key_id,),
                protect_decision_ids=(preceding_decision_id,),
            )
            self._install_consumer_state(decision.server_key, ledger)
            return ConsumerAlertReservation(
                True,
                key,
                reserved,
                None,
                ("reserved_pending_durable_commit", "at_most_once_delivery"),
            )

    def reserve_consumer_alert(
        self,
        *,
        decision: AuthorityConsumerDecision,
        key: AuthoritySuppressionKey,
        created_at: float,
    ) -> ConsumerAlertReservation:
        """Atomically consume one alert slot before external action dispatch."""

        reservation = self.stage_consumer_alert_reservation(
            decision=decision, key=key, created_at=created_at
        )
        if not reservation.reserved:
            return reservation
        written = self.flush()
        return ConsumerAlertReservation(
            True,
            key,
            reservation.record,
            written,
            ("reserved_and_durable_before_dispatch", "at_most_once_delivery"),
        )

    def stage_consumer_alert_completion(
        self,
        *,
        decision: AuthorityConsumerDecision,
        reservation: ConsumerAlertReservation,
        completed_at: float,
        action_succeeded: bool,
    ) -> bool:
        """Stage the dispatch outcome; return False if nothing needed staging."""

        self._ensure_open()
        if not reservation.reserved or reservation.record is None:
            raise Schema4RuntimeError("consumer alert was not reserved")
        with self._mutex:
            assert self._state is not None
            record = self._state["servers"][decision.server_key]
            ledger = consumer_ledger_from_mapping(
                record.get("consumer_state"), server_key=decision.server_key
            )
            if reservation.record.record_id not in {
                item.record_id for item in ledger.alert_records
            }:
                # Retention R5: the reservation was folded into the suppression
                # high-water mark, so the key stays suppressed without it and
                # its outcome cannot change that.  Never re-append it.
                if not key_suppressed(
                    ledger, reservation.key, event_finalized_at=_event_finalized_at(record)
                ):
                    raise Schema4RuntimeError(
                        "consumer alert reservation is missing and not suppressed"
                    )
                return False
            completed = alert_record(
                key=reservation.key,
                authority_consumer_decision_id=decision.decision_id,
                created_at=completed_at,
                lifecycle=(
                    AlertLifecycle.EMITTED
                    if action_succeeded
                    else AlertLifecycle.ACTION_FAILED_AFTER_RESERVATION
                ),
                action_outcome=(
                    AlertActionOutcome.EMITTED
                    if action_succeeded
                    else AlertActionOutcome.ACTION_FAILED
                ),
                supersedes_record_id=reservation.record.record_id,
                reason_codes=(
                    "external_action_returned_success"
                    if action_succeeded
                    else "external_action_failed_after_durable_reservation",
                    "reservation_remains_replay_suppressing",
                ),
            )
            ledger = append_alert(ledger, completed)
            ledger = bound_consumer_ledger(
                ledger,
                now=completed_at,
                event_finalized_at=_event_finalized_at(record),
                protect_key_ids=(completed.key_id,),
            )
            self._install_consumer_state(decision.server_key, ledger)
            return True

    def complete_consumer_alert(
        self,
        *,
        decision: AuthorityConsumerDecision,
        reservation: ConsumerAlertReservation,
        completed_at: float,
        action_succeeded: bool,
    ) -> Schema4WriteResult:
        """Append dispatch outcome; reservation remains replay-suppressing on failure."""

        self.stage_consumer_alert_completion(
            decision=decision,
            reservation=reservation,
            completed_at=completed_at,
            action_succeeded=action_succeeded,
        )
        return self.flush()

    def snapshot(self) -> Schema4RuntimeSnapshot:
        if not self.enabled or self._state is None:
            return Schema4RuntimeSnapshot(
                enabled=False,
                active_path=str(self.active_path),
                generation=0,
                active_sha256=None,
                valid_server_keys=(),
                quarantined_server_keys=(),
                dirty_server_keys=(),
                write_count=0,
                decision_ids=(),
                recovery=None,
            )
        decisions = []
        for key, record in self._state.get("servers", {}).items():
            if not isinstance(record, Mapping):
                continue
            decision = record.get("authority_decision")
            if isinstance(decision, Mapping) and isinstance(decision.get("decision_id"), str):
                decisions.append((str(key), str(decision["decision_id"])))
        report = self._load_result.report if self._load_result is not None else None
        dirty = set(self._dirty_servers)
        for keys, _root in self._submitted.values():
            dirty.update(keys)
        return Schema4RuntimeSnapshot(
            enabled=True,
            active_path=str(self.active_path),
            generation=self._generation,
            active_sha256=self._expected_sha256,
            valid_server_keys=() if report is None else report.valid_server_keys,
            quarantined_server_keys=() if report is None else report.quarantined_server_keys,
            dirty_server_keys=tuple(sorted(dirty)),
            write_count=self._write_count,
            decision_ids=tuple(sorted(decisions)),
            recovery=self._recovery,
        )

    def server_write_block(self, server_key: str) -> tuple[str, tuple[str, ...]] | None:
        """Return why this server's record must not be written, or ``None``.

        A quarantined or otherwise unwritable record is preserved verbatim
        and never rebuilt, so its invalid evidence is neither trusted nor
        silently replaced by fresh learning.
        """

        self._ensure_open()
        with self._mutex:
            return self._server_write_block_locked(str(server_key))

    def _server_write_block_locked(
        self, server_key: str
    ) -> tuple[str, tuple[str, ...]] | None:
        assert self._state is not None
        existing = self._state.get("servers", {}).get(server_key)
        if existing is None:
            snapshot = self._state.get("schema3_snapshot")
            snapshot_servers = (
                snapshot.get("servers") if isinstance(snapshot, Mapping) else None
            )
            if isinstance(snapshot_servers, Mapping) and server_key in snapshot_servers:
                return ("server_unavailable", ())
            return None
        if not isinstance(existing, Mapping):
            return ("server_unavailable", ())
        status = existing.get("authority_status")
        if status in SCHEMA4_RUNTIME_WRITABLE_AUTHORITY_STATUSES:
            return None
        codes: tuple[str, ...] = ()
        errors = existing.get("validation_errors")
        if isinstance(errors, list):
            codes = tuple(
                sorted(
                    {
                        str(item.get("code"))
                        for item in errors
                        if isinstance(item, Mapping) and item.get("code")
                    }
                )
            )
        return (
            "server_quarantined" if status == "quarantined" else "server_not_writable",
            codes,
        )

    @staticmethod
    def _validate_record_for_server(
        server_key: str, record: Mapping[str, object]
    ) -> None:
        """Per-server strict validation of a record about to be trusted.

        Every other record and the root are unchanged and were validated when
        installed, so validating the changed record is equivalent to
        validating the whole candidate state.
        """

        issues = validate_schema4_server_record(server_key, record)
        if issues:
            raise Schema4ServerWriteRejected(
                server_key,
                "server_record_validation_failed",
                "; ".join(item.detail for item in issues)[:512],
                validation_codes=tuple(sorted({item.code for item in issues})),
            )

    def note_healthy_poll(self, _server_key: str) -> None:
        """Healthy poll-only activity is intentionally non-durable."""

        self._ensure_open()

    def update_server_from_schema3(
        self,
        server_key: str,
        schema3_record: Mapping[str, object],
        *,
        updated_at: float,
        durable_reason: str,
    ) -> bool:
        """Rebuild the affected server and mark it dirty iff semantics changed."""

        self._ensure_open()
        with self._mutex:
            existing = self._prepare_server_rebuild_locked(server_key)
            rebuilt, entry = self._rebuild_server_record(
                server_key,
                {} if existing is _NO_RECORD else existing,
                schema3_record,
                updated_at=updated_at,
                durable_reason=durable_reason,
            )
            if rebuilt is None:
                return False
            self._install_server_rebuild_locked(
                server_key, rebuilt, entry, updated_at=updated_at
            )
            return True

    # ------------------------------------------------------------------
    # R8: per-server rebuilds on the ordered worker
    # ------------------------------------------------------------------

    def _prepare_server_rebuild_locked(self, server_key: str) -> object:
        """Owner thread: refuse an unwritable server; return the base record."""

        assert self._state is not None
        snapshot = self._state.get("schema3_snapshot")
        snapshot_servers = (
            snapshot.get("servers") if isinstance(snapshot, Mapping) else None
        )
        if not isinstance(snapshot_servers, Mapping):
            raise Schema4ValidationError(
                "schema-4 state has no writable schema-3 server projection"
            )
        block = self._server_write_block_locked(server_key)
        if block is not None:
            raise Schema4ServerWriteRejected(
                server_key,
                block[0],
                f"schema-4 server {server_key!r} is unavailable or quarantined",
                validation_codes=block[1],
            )
        existing = self._state.get("servers", {}).get(server_key)
        return _NO_RECORD if existing is None else existing

    @classmethod
    def _rebuild_server_record(
        cls,
        server_key: str,
        existing_for_rebuild: Mapping[str, object],
        schema3_record: Mapping[str, object],
        *,
        updated_at: float,
        durable_reason: str,
    ) -> tuple[dict | None, dict | None]:
        """Pure: rebuild, compare and validate one record.

        Reads only its arguments (an immutable installed record and a
        detached schema-3 copy), so it may run on the persistence worker.
        Returns ``(None, None)`` when the rebuilt record is semantically
        unchanged.
        """

        try:
            rebuilt = rebuild_schema4_server_record(
                existing_for_rebuild,
                schema3_record,
                server_key=server_key,
                updated_at=updated_at,
            )
        except Schema4Error as exc:
            # Deterministic for this record: one server's evidence cannot
            # be rebuilt.  The stored record and all other servers stay
            # intact and writable.
            raise Schema4ServerWriteRejected(
                server_key,
                "server_rebuild_rejected",
                f"{type(exc).__name__}: {exc}",
            ) from exc
        before = canonical_json_bytes(existing_for_rebuild)
        after = canonical_json_bytes(rebuilt)
        if before == after:
            return None, None
        reasons = set(rebuilt.get("reason_codes", ()))
        reasons.add(str(durable_reason))
        rebuilt["reason_codes"] = sorted(reasons)
        cls._validate_record_for_server(server_key, rebuilt)
        entry = copy.deepcopy(dict(schema3_record))
        legacy = rebuilt.get("legacy_schema3_record")
        if isinstance(legacy, dict) and legacy == entry:
            # Share the identical mapping (R6 stores it once on disk);
            # neither copy is ever mutated after installation.
            entry = legacy
        return rebuilt, entry

    def _install_server_rebuild_locked(
        self,
        server_key: str,
        rebuilt: dict,
        entry: dict,
        *,
        updated_at: float,
    ) -> None:
        """Owner thread: copy-on-write install of one validated record."""

        assert self._state is not None
        snapshot = self._state["schema3_snapshot"]
        candidate_snapshot = dict(snapshot)
        candidate_snapshot_servers = dict(snapshot["servers"])
        candidate_snapshot_servers[server_key] = entry
        candidate_snapshot["servers"] = candidate_snapshot_servers
        candidate_snapshot["updated_at"] = max(
            int(updated_at),
            int(candidate_snapshot.get("created_at", 0) or 0),
        )
        servers = dict(self._state.get("servers", {}))
        servers[server_key] = rebuilt
        candidate = dict(self._state)
        candidate["servers"] = servers
        candidate["schema3_snapshot"] = candidate_snapshot
        candidate["updated_at"] = max(
            int(updated_at), int(candidate.get("created_at", 0) or 0)
        )
        self._state = candidate
        self._dirty_servers.add(server_key)

    def submit_server_rebuild(
        self,
        server_key: str,
        schema3_record: Mapping[str, object],
        *,
        updated_at: float,
        durable_reason: str,
        callback: Callable[[Schema4WriteTicket], None] | None = None,
    ) -> Schema4WriteTicket:
        """Rebuild one server without blocking the owner thread (R8).

        An unwritable (quarantined/unavailable) server or a missing schema-3
        projection is refused synchronously, exactly as
        ``update_server_from_schema3``.  Otherwise the rebuild, canonical
        comparison and per-record validation run on the persistence worker
        (inline when background writes are off); the validated record is
        installed copy-on-write on the owner thread, and the ticket then
        resolves with a ``Schema4RebuildResult`` (or the failure).  The
        caller must keep at most one rebuild per server in flight.

        ``schema3_record`` must be detached from live state (never mutated
        afterwards); the job owns it.
        """

        self._ensure_open()
        ticket = Schema4WriteTicket()
        if callback is not None:
            ticket.add_callback(callback)
        key = str(server_key)
        with self._mutex:
            base = self._prepare_server_rebuild_locked(key)
            job = _RebuildJob(
                server_key=key,
                base=base,
                schema3_record=schema3_record,  # type: ignore[arg-type]
                updated_at=float(updated_at),
                durable_reason=str(durable_reason),
                ticket=ticket,
            )
            worker = self._worker
            self._rebuilds_in_flight += 1
        if worker is None:
            self._apply_completion(self._compute_server_rebuild(job))
            return ticket
        try:
            worker.submit_rebuild(job)
        except BaseException as exc:
            self._apply_completion(
                _Completion(
                    kind="rebuild", tickets=(ticket,), error=exc, rebuild_job=job
                )
            )
        return ticket

    def _compute_server_rebuild(self, job: _RebuildJob) -> _Completion:
        """Worker (or inline): the expensive, read-only part of a rebuild."""

        started = time.perf_counter()
        try:
            base = {} if job.base is _NO_RECORD else job.base
            rebuilt, entry = self._rebuild_server_record(
                job.server_key,
                base,  # type: ignore[arg-type]
                job.schema3_record,
                updated_at=job.updated_at,
                durable_reason=job.durable_reason,
            )
            staged = canonical_json_bytes(job.schema3_record)
        except BaseException as exc:
            return _Completion(
                kind="rebuild",
                tickets=(job.ticket,),
                error=exc,
                duration=time.perf_counter() - started,
                rebuild_job=job,
            )
        return _Completion(
            kind="rebuild",
            tickets=(job.ticket,),
            result=(rebuilt, entry, staged),
            duration=time.perf_counter() - started,
            rebuild_job=job,
        )

    def _apply_rebuild_completion(self, completion: _Completion) -> bool:
        """Owner thread: install a computed rebuild; False when re-submitted."""

        job = completion.rebuild_job
        assert job is not None
        ticket = job.ticket
        error = completion.error
        result: Schema4RebuildResult | None = None
        resubmit = None
        with self._mutex:
            if error is None and (self._closed or self._state is None):
                error = Schema4RuntimeError(
                    "authoritative schema-4 runtime is closed"
                )
            if error is None:
                current = self._state["servers"].get(job.server_key)
                current = _NO_RECORD if current is None else current
                if current is not job.base:
                    # The record changed outside the ordered per-server
                    # queue (maintenance compaction or a test hook).  Never
                    # install a rebuild of a stale base: recompute on the
                    # current record instead.
                    if job.rebased >= _MAX_REBUILD_REBASES:
                        error = Schema4RuntimeError(
                            "per-server rebuild base kept changing"
                        )
                    else:
                        try:
                            base = self._prepare_server_rebuild_locked(
                                job.server_key
                            )
                        except BaseException as exc:
                            error = exc
                        else:
                            self._rebuild_rebase_count += 1
                            resubmit = _RebuildJob(
                                server_key=job.server_key,
                                base=base,
                                schema3_record=job.schema3_record,
                                updated_at=job.updated_at,
                                durable_reason=job.durable_reason,
                                ticket=ticket,
                                rebased=job.rebased + 1,
                            )
                else:
                    rebuilt, entry, staged = completion.result  # type: ignore[misc]
                    if rebuilt is not None:
                        self._install_server_rebuild_locked(
                            job.server_key,
                            rebuilt,
                            entry,
                            updated_at=job.updated_at,
                        )
                    result = Schema4RebuildResult(
                        server_key=job.server_key,
                        changed=rebuilt is not None,
                        staged_schema3_bytes=staged,
                        rebuild_seconds=completion.duration,
                        rebased=job.rebased,
                    )
            if resubmit is None:
                self._rebuilds_in_flight = max(0, self._rebuilds_in_flight - 1)
            worker = self._worker
        if resubmit is not None:
            if worker is None:
                self._apply_completion(self._compute_server_rebuild(resubmit))
                return False
            try:
                worker.submit_rebuild(resubmit)
            except BaseException as exc:
                self._apply_completion(
                    _Completion(
                        kind="rebuild",
                        tickets=(ticket,),
                        error=exc,
                        rebuild_job=resubmit,
                    )
                )
            return False
        ticket._resolve(result, error)
        return True

    def replace_server_record(
        self, server_key: str, record: Mapping[str, object], *, reason: str
    ) -> bool:
        """Test/maintenance hook for a fully derived immutable server record."""

        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            replacement = copy.deepcopy(dict(record))
            servers = dict(self._state.get("servers", {}))
            servers[server_key] = replacement
            candidate = dict(self._state)
            candidate["servers"] = servers
            serialize_schema4_state(candidate)
            current = self._state.get("servers", {}).get(server_key)
            if canonical_json_bytes(current) == canonical_json_bytes(record):
                return False
            replacement["reason_codes"] = sorted(
                set(replacement.get("reason_codes", ())) | {str(reason)}
            )
            self._state = candidate
            self._dirty_servers.add(server_key)
            return True

    def compact(self) -> bool:
        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            compacted = compact_schema4_state(self._state)
            if canonical_json_bytes(compacted) == canonical_json_bytes(self._state):
                return False
            self._state = compacted
            self._root_dirty = True
            return True

    def maybe_auto_compact(
        self, policy: AutoCompactionPolicy
    ) -> tuple[str, ...]:
        """Apply configured thresholds only when an explicit enabled policy is supplied."""

        self._ensure_open()
        assert self._state is not None
        raw_samples = 0
        legacy_coverage = 0
        decisions = 0
        alerts = 0
        for record in self._state.get("servers", {}).values():
            if not isinstance(record, Mapping):
                continue
            for event in record.get("physical_events", ()):
                if isinstance(event, Mapping):
                    raw_samples += len(event.get("samples", ()))
            legacy = record.get("legacy_schema3_record")
            if isinstance(legacy, Mapping):
                legacy_coverage += len(legacy.get("coverage_segments", ()))
            decisions += len(record.get("authority_decisions", ()))
            consumer = record.get("consumer_state")
            if isinstance(consumer, Mapping):
                alerts += len(consumer.get("alert_records", ()))
        reasons = auto_compaction_reasons(
            policy=policy,
            file_size=self.active_path.stat().st_size,
            raw_sample_count=raw_samples,
            legacy_coverage_count=legacy_coverage,
            decision_history_count=decisions,
            consumer_alert_record_count=alerts,
        )
        if reasons:
            self.compact()
        return reasons

    # ------------------------------------------------------------------
    # Writes: one validated path, inline or on the ordered worker
    # ------------------------------------------------------------------

    def enable_background_writes(
        self, post_to_owner: Callable[[Callable[[], object]], object]
    ) -> None:
        """Move writes to one ordered worker thread.

        ``post_to_owner`` schedules a zero-argument callable on the owner
        thread (production: the main-loop idle scheduler).  Completions and every ticket
        callback then run on the owner thread via ``process_completions``.
        """

        self._ensure_open()
        with self._mutex:
            if self._worker is not None:
                self._post_to_owner = post_to_owner
                return
            if self._submitted:
                raise Schema4RuntimeError("cannot switch write mode during a write")
            self._post_to_owner = post_to_owner
            self._worker = _Schema4PersistenceWorker(self)
            root = self._state

        def warm() -> _Completion:
            # Encode and validate every fragment once in the background, so
            # the first event-time write only encodes what changed.
            try:
                if root is not None:
                    self._writer.payload_for(root)
            except BaseException:
                pass
            return _Completion(kind="noop")

        self._worker.submit_aux(warm)

    def flush_async(
        self, callback: Callable[[Schema4WriteTicket], None] | None = None
    ) -> Schema4WriteTicket:
        """Request durability of the current state without waiting.

        The ticket resolves after the newest snapshot containing every change
        staged so far has been atomically installed, or with the failure.
        """

        self._ensure_open()
        ticket = Schema4WriteTicket()
        if callback is not None:
            ticket.add_callback(callback)
        with self._mutex:
            staged = bool(self._dirty_servers or self._root_dirty)
            if not staged:
                last = self._last_ticket
                if self._submitted and last is not None and not last.done:
                    # The newest submitted snapshot already contains the
                    # current state: its durability is this request's.
                    last.add_callback(lambda done: ticket._resolve(done.result, done.error))
                    return ticket
                unchanged = Schema4WriteResult(
                    wrote=False,
                    generation=self._generation,
                    sha256=self._expected_sha256,
                    dirty_server_keys=(),
                    reason_codes=("unchanged_no_write",),
                )
            else:
                assert self._state is not None
                self._sequence += 1
                sequence = self._sequence
                keys = frozenset(self._dirty_servers)
                self._submitted[sequence] = (keys, self._root_dirty)
                self._dirty_servers = set()
                self._root_dirty = False
                self._last_ticket = ticket
                job = _WriteJob([sequence], self._state, [ticket], set(keys))
                worker = self._worker
        if not staged:
            ticket._resolve(unchanged, None)
            return ticket
        if worker is None:
            completion = self._writer.write(job)
            self._apply_completion(completion)
            return ticket
        try:
            worker.submit(job)
        except BaseException as exc:
            self._apply_completion(
                _Completion(
                    kind="write",
                    sequences=(sequence,),
                    tickets=(ticket,),
                    error=exc,
                )
            )
        return ticket

    def flush(self) -> Schema4WriteResult:
        ticket = self.flush_async()
        self.wait_for(ticket)
        if ticket.error is not None:
            raise ticket.error
        assert ticket.result is not None
        return ticket.result

    def wait_for(self, ticket: Schema4WriteTicket, timeout: float | None = None) -> bool:
        """Block the owner thread until ``ticket`` resolves (shutdown/tests)."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while not ticket.done:
            self.process_completions()
            if ticket.done:
                break
            if self._worker is None or not self._worker.alive:
                self.process_completions()
                if not ticket.done:
                    if self._worker is not None and self._rebuilds_in_flight:
                        # A dead worker never reports queued rebuilds.
                        self._rebuilds_in_flight = 0
                    ticket._resolve(
                        None,
                        Schema4RuntimeError("persistence worker is not running"),
                    )
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._completion_ready.wait(0.05 if remaining is None else min(0.05, remaining))
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every submitted write completed (shutdown/tests)."""

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self.process_completions()
            if (
                self._rebuilds_in_flight
                and self._worker is not None
                and not self._worker.alive
                and not self._completions
            ):
                # A dead worker never reports queued rebuilds.
                self._rebuilds_in_flight = 0
            if (
                not self._submitted
                and not self._rebuilds_in_flight
                and (self._worker is None or not self._worker.busy)
            ):
                self.process_completions()
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._completion_ready.wait(0.05 if remaining is None else min(0.05, remaining))

    def _deliver_completion(self, completion: _Completion) -> None:
        """Worker thread: queue a completion for the owner thread."""

        self._completions.append(completion)
        for ticket in completion.tickets:
            ticket._worker_done.set()
        self._completion_ready.set()
        post = self._post_to_owner
        if post is not None:
            try:
                post(self._process_completions_idle)
            except Exception:
                # Drained by the next owner-thread wait, flush or close.
                pass

    def _process_completions_idle(self) -> bool:
        self.process_completions()
        return False

    def process_completions(self) -> int:
        """Owner thread: apply every queued completion in order."""

        count = 0
        self._completion_ready.clear()
        while True:
            try:
                completion = self._completions.popleft()
            except IndexError:
                return count
            self._apply_completion(completion)
            count += 1

    def _apply_completion(self, completion: _Completion) -> None:
        if completion.kind == "noop":
            return
        if completion.kind == "rebuild":
            self._apply_rebuild_completion(completion)
            return
        if completion.kind == "aux":
            if completion.owner_callback is not None:
                try:
                    completion.owner_callback(completion.result, completion.error)
                except Exception:
                    logger.exception("Server Companion persistence task callback failed")
            return
        with self._mutex:
            if completion.error is None:
                for sequence in completion.sequences:
                    self._submitted.pop(sequence, None)
                result = completion.result
                assert isinstance(result, Schema4WriteResult)
                self._generation = result.generation
                self._expected_sha256 = result.sha256
                self._write_count += 1
                if completion.report is not None:
                    self._load_result = Schema4LoadResult(state={}, report=completion.report)
                if completion.written_fields and self._state is not None:
                    updated = dict(self._state)
                    updated.update(completion.written_fields)
                    self._state = updated
            else:
                for sequence in completion.sequences:
                    keys, root = self._submitted.pop(sequence, (frozenset(), False))
                    # Still not durable: the next flush writes them again.
                    self._dirty_servers.update(keys)
                    self._root_dirty = self._root_dirty or root
        for ticket in completion.tickets:
            ticket._resolve(
                completion.result if completion.error is None else None,
                completion.error,
            )

    def close(self, *, flush: bool = True) -> Schema4WriteResult | None:
        if self._closed:
            return None
        try:
            if (
                flush
                and self.enabled
                and self._rebuilds_in_flight
                and self._worker is not None
            ):
                # R8: install in-flight rebuilds so the final write has them.
                self.wait_idle()
            return self.flush() if flush and self.enabled and self.dirty else None
        finally:
            worker = self._worker
            if worker is not None:
                # Never release the writer lock while a write is running.
                worker.stop(discard_pending=True)
                self.process_completions()
                self._worker = None
            self._closed = True
            if self._writer_lock is not None:
                self._writer_lock.release()

    def _ensure_open(self) -> None:
        if not self.enabled:
            raise Schema4RuntimeError("authoritative schema-4 runtime is disabled")
        if self._closed:
            raise Schema4RuntimeError("authoritative schema-4 runtime is closed")

    def __enter__(self) -> "AuthoritativeSchema4Runtime":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close(flush=_type is None)


class Schema4RuntimeSoakHarness:
    """Deterministic temporary-file harness; never resolves a user path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise Schema4RuntimeError("soak harness requires an existing copied fixture")
        self.original_bytes = self.path.read_bytes()
        deserialize_schema4_bytes(self.original_bytes, quarantine_invalid_servers=False)
        self.runtime: AuthoritativeSchema4Runtime | None = None
        self.start_count = 0
        self.healthy_poll_count = 0
        self.durable_update_count = 0

    def start(self) -> AuthoritativeSchema4Runtime:
        if self.runtime is not None:
            raise Schema4RuntimeError("soak runtime is already started")
        self.runtime = AuthoritativeSchema4Runtime.open(self.path, enabled=True)
        self.start_count += 1
        return self.runtime

    def stop(self, *, flush: bool = True) -> None:
        if self.runtime is None:
            return
        self.runtime.close(flush=flush)
        self.runtime = None

    def restart(self) -> AuthoritativeSchema4Runtime:
        self.stop(flush=True)
        return self.start()

    def healthy_polls(self, server_key: str, count: int) -> None:
        runtime = self._running()
        for _ in range(max(0, int(count))):
            runtime.note_healthy_poll(server_key)
            self.healthy_poll_count += 1

    def install_server_record(
        self, server_key: str, record: Mapping[str, object], *, reason: str
    ) -> bool:
        changed = self._running().replace_server_record(
            server_key, record, reason=reason
        )
        if changed:
            self.durable_update_count += 1
        return changed

    def compact(self) -> bool:
        changed = self._running().compact()
        if changed:
            self.durable_update_count += 1
        return changed

    def flush(self) -> Schema4WriteResult:
        return self._running().flush()

    def rollback_fixture(self) -> str:
        self.stop(flush=False)
        _replace_bytes_fsynced(self.path, self.original_bytes)
        return _sha256(self.path.read_bytes())

    def result(self) -> Schema4SoakResult:
        runtime = self._running()
        snapshot = runtime.snapshot()
        payload = self.path.read_bytes()
        return Schema4SoakResult(
            start_count=self.start_count,
            healthy_poll_count=self.healthy_poll_count,
            durable_update_count=self.durable_update_count,
            write_count=snapshot.write_count,
            final_sha256=_sha256(payload),
            final_generation=snapshot.generation,
            reason_codes=(
                "temporary_schema4_fixture_only",
                "production_schema3_and_consumers_untouched",
            ),
        )

    def _running(self) -> AuthoritativeSchema4Runtime:
        if self.runtime is None:
            raise Schema4RuntimeError("soak runtime is not started")
        return self.runtime


def _recover_schema4_path(path: Path) -> Schema4RecoveryResult:
    return _recover_schema4_path_loaded(path)[0]


def _recover_schema4_path_loaded(
    path: Path,
) -> tuple[Schema4RecoveryResult, bytes, Schema4LoadResult]:
    """Recover the active file and return it parsed and validated once.

    Before R7 the active bytes were parsed and validated twice at open (here
    and again by the caller).  The loaded result of the bytes that end up
    active is now returned instead.
    """

    if not path.exists():
        raise Schema4RuntimeError(
            "authoritative schema-4 runtime will not create or migrate state implicitly"
        )
    removed: list[str] = []
    stale_patterns = (
        f".{path.name}.schema4-runtime-*.tmp",
        f".{path.name}.last-known-good.link-*.tmp",
    )
    for pattern in stale_patterns:
        for stale in sorted(path.parent.glob(pattern)):
            try:
                stale.unlink()
                removed.append(str(stale))
            except OSError:
                pass
    reasons = set()
    if removed:
        reasons.add("stale_temporary_files_removed")
    try:
        active = path.read_bytes()
        # One parse: deserialization itself rejects a non-schema-4 root.
        loaded = deserialize_schema4_bytes(active, quarantine_invalid_servers=True)
        if not loaded.report.root_valid:
            raise Schema4ValidationError("active schema-4 root invalid")
        reasons.add("valid_active_schema4_retained")
        return (
            Schema4RecoveryResult(tuple(removed), False, None, tuple(sorted(reasons))),
            active,
            loaded,
        )
    except (OSError, Schema4Error, ValueError) as active_error:
        backup = path.with_name(path.name + ".last-known-good")
        if not backup.exists():
            raise Schema4RuntimeError(
                f"invalid active schema 4 and no last-known-good backup: {active_error}"
            ) from active_error
        backup_bytes = backup.read_bytes()
        try:
            if schema_version_from_bytes(backup_bytes) != SCHEMA4_ROOT_VERSION:
                raise Schema4VersionError("last-known-good is not schema 4")
            strict = deserialize_schema4_bytes(
                backup_bytes, quarantine_invalid_servers=False
            )
            if not strict.report.valid:
                raise Schema4ValidationError("last-known-good validation failed")
        except (Schema4Error, ValueError) as backup_error:
            raise Schema4RuntimeError(
                f"active and last-known-good schema-4 files are invalid: {backup_error}"
            ) from backup_error
        invalid_bytes = path.read_bytes()
        invalid_path = path.with_name(path.name + f".invalid-{_sha256(invalid_bytes)[:16]}")
        if not invalid_path.exists():
            _write_new_file_fsynced(invalid_path, invalid_bytes)
        _replace_bytes_fsynced(path, backup_bytes)
        reasons.update({"invalid_active_preserved", "last_known_good_restored"})
        # A strictly valid file loads identically with quarantining enabled.
        return (
            Schema4RecoveryResult(
                tuple(removed), True, str(invalid_path), tuple(sorted(reasons))
            ),
            backup_bytes,
            strict,
        )


_LKG_LINK_UNSUPPORTED_ERRNOS = frozenset(
    code
    for code in (
        getattr(__import__("errno"), name, None)
        for name in ("EXDEV", "EPERM", "ENOTSUP", "EOPNOTSUPP", "EMLINK", "ENOSYS", "EACCES")
    )
    if code is not None
)


def _prepare_last_known_good(
    active_path: Path, last_known_good_path: Path, expected_sha256: str | None
) -> tuple[Path | None, bytes | None]:
    """Capture the current active file as the next last-known-good, verified.

    R7: the active file is only ever replaced by rename, never modified in
    place, so a hard link to it preserves its exact bytes without copying
    them.  The linked bytes are hashed against the runtime's expected
    identity (generation-conflict detection).  When hard links are not
    supported the bytes are read and later copied, as before R7.
    """

    link_path = last_known_good_path.with_name(
        f".{last_known_good_path.name}.link-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    )
    try:
        os.link(active_path, link_path)
    except OSError as exc:
        if exc.errno not in _LKG_LINK_UNSUPPORTED_ERRNOS:
            raise
        current = active_path.read_bytes()
        if _sha256(current) != expected_sha256:
            raise Schema4GenerationConflict(
                "schema-4 active file changed after load; reload before retry"
            )
        return None, current
    try:
        if _sha256_file(link_path) != expected_sha256:
            raise Schema4GenerationConflict(
                "schema-4 active file changed after load; reload before retry"
            )
    except BaseException:
        _unlink_quiet(link_path)
        raise
    return link_path, None


def _install_atomic_schema4(
    active_path: Path,
    payload: bytes,
    *,
    expected_sha256: str | None,
    last_known_good_path: Path,
    crash_injector: Callable[[Schema4CrashPoint], None] | None,
) -> None:
    """Atomically install ``payload`` (already validated, byte for byte).

    Order: verify the active identity and capture it as last-known-good,
    write and fsync the temporary file and verify its bytes, rotate the
    last-known-good, rename over the active file, fsync the directory, and
    verify the installed bytes.  Failure phases are classified as before.
    """

    link_path, previous_valid_bytes = _prepare_last_known_good(
        active_path, last_known_good_path, expected_sha256
    )
    try:
        try:
            active_path.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_name = tempfile.mkstemp(
                prefix=f".{active_path.name}.schema4-runtime-",
                suffix=".tmp",
                dir=active_path.parent,
            )
        except OSError as exc:
            phase = (
                Schema4WriteFailurePhase.CONFIRMED_PRE_COMMIT
                if _active_unchanged(active_path, expected_sha256)
                else Schema4WriteFailurePhase.POST_COMMIT_OR_AMBIGUOUS
            )
            raise Schema4WriteFailure(phase, exc) from exc
        temp_path = Path(raw_name)
        active_replaced = False
        try:
            if crash_injector is not None:
                crash_injector(Schema4CrashPoint.BEFORE_TEMP_COMPLETE)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # The payload's every byte was validated before this call
            # (per-fragment strict parse plus exact layout); prove that the
            # durable temporary file holds exactly those bytes.
            payload_sha256 = _sha256(payload)
            if _sha256_file(temp_path) != payload_sha256:
                raise Schema4ValidationError("prepared runtime payload failed validation")
            if crash_injector is not None:
                crash_injector(Schema4CrashPoint.AFTER_TEMP_FSYNC_BEFORE_REPLACE)
            if link_path is not None:
                os.replace(link_path, last_known_good_path)
                link_path = None
                _fsync_directory(last_known_good_path.parent)
            else:
                _replace_bytes_fsynced(last_known_good_path, previous_valid_bytes or b"")
            os.replace(temp_path, active_path)
            active_replaced = True
            _fsync_directory(active_path.parent)
            if crash_injector is not None:
                crash_injector(Schema4CrashPoint.AFTER_REPLACE)
            if _sha256_file(active_path) != payload_sha256:
                raise Schema4RuntimeError("atomic schema-4 replacement verification failed")
        except Schema4Error as exc:
            if active_replaced:
                raise Schema4WriteFailure(
                    Schema4WriteFailurePhase.POST_COMMIT_OR_AMBIGUOUS,
                    exc,
                ) from exc
            raise
        except Exception as exc:
            if not active_replaced and not isinstance(exc, OSError):
                raise
            phase = Schema4WriteFailurePhase.POST_COMMIT_OR_AMBIGUOUS
            if not active_replaced and _active_unchanged(active_path, expected_sha256):
                phase = Schema4WriteFailurePhase.CONFIRMED_PRE_COMMIT
            raise Schema4WriteFailure(phase, exc) from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    finally:
        if link_path is not None:
            _unlink_quiet(link_path)


def _share_identical_schema3_entries(state: dict) -> None:
    """Point each equal schema-3 snapshot entry at its record's copy.

    In memory both mappings are identical (R6 stores one on disk).  Sharing
    the object lets every later write prove the equality by identity instead
    of a deep comparison.  Neither is ever mutated after installation.
    """

    snapshot = state.get("schema3_snapshot")
    records = state.get("servers")
    if not isinstance(snapshot, dict) or not isinstance(records, dict):
        return
    projected = snapshot.get("servers")
    if not isinstance(projected, dict):
        return
    for key, value in list(projected.items()):
        record = records.get(key)
        if not isinstance(record, dict) or record.get("authority_status") == "quarantined":
            continue
        legacy = record.get("legacy_schema3_record")
        if isinstance(legacy, dict) and legacy is not value and legacy == value:
            projected[key] = legacy


def _active_unchanged(active_path: Path, expected_sha256: str | None) -> bool:
    try:
        return _sha256_file(active_path) == expected_sha256
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _replace_bytes_fsynced(path: Path, payload: bytes) -> None:
    fd, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.replace-", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_new_file_fsynced(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _event_finalized_at(record: Mapping[str, object]) -> dict[str, float]:
    """Return ``finalized_at`` of every retained physical event of a record."""

    result: dict[str, float] = {}
    for item in record.get("physical_events", ()) or ():
        if not isinstance(item, Mapping):
            continue
        event_id = item.get("event_id")
        at = item.get("finalized_at")
        if (
            isinstance(event_id, str)
            and not isinstance(at, bool)
            and isinstance(at, (int, float))
        ):
            result[event_id] = float(at)
    return result


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
