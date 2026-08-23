from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .companion_restart_phase2_authority_consumers import (
    AuthorityConsumerDecision,
    AuthoritySuppressionKey,
)
from .companion_restart_phase2_consumer_state import (
    AlertActionOutcome,
    AlertLifecycle,
    AutoCompactionPolicy,
    ConsumerAlertRecord,
    alert_record,
    auto_compaction_reasons,
    append_alert,
    empty_consumer_state,
    fired_suppression_keys,
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
    Schema4VersionError,
    canonical_json_bytes,
    compact_schema4_state,
    deserialize_schema4_bytes,
    rebuild_schema4_server_record,
    schema_version_from_bytes,
    serialize_schema4_state,
)


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


class AuthoritativeSchema4Runtime:
    """Validated single-writer persistence backend for schema 4.

    It owns persistence only.  During Stage 3A the caller's schema-3 scorer and
    consumers remain the displayed production authority.
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
    ) -> None:
        self.active_path = Path(active_path)
        self.enabled = bool(enabled)
        self._state = copy.deepcopy(state) if state is not None else None
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
            recovery = _recover_schema4_path(path)
            raw = path.read_bytes()
            if schema_version_from_bytes(raw) != SCHEMA4_ROOT_VERSION:
                raise Schema4VersionError(
                    "authoritative schema-4 runtime requires an explicitly migrated schema-4 file"
                )
            loaded = deserialize_schema4_bytes(raw, quarantine_invalid_servers=True)
            if not loaded.report.root_valid:
                raise Schema4ValidationError("schema-4 root validation failed")
            return cls(
                active_path=path,
                enabled=True,
                state=loaded.state,
                load_result=loaded,
                active_bytes=raw,
                writer_lock=lock,
                recovery=recovery,
                crash_injector=crash_injector,
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
        return bool(self._dirty_servers or self._root_dirty)

    def schema3_projection(self) -> dict:
        """Return the in-memory schema-3 detector/scorer projection only."""

        state = self.state
        snapshot = state.get("schema3_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 3:
            raise Schema4ValidationError("schema-4 state has no valid schema-3 runtime projection")
        projected = copy.deepcopy(snapshot)
        projected_servers = projected.setdefault("servers", {})
        for key, record in state.get("servers", {}).items():
            if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
                projected_servers.pop(key, None)
                continue
            legacy = record.get("legacy_schema3_record")
            if isinstance(legacy, Mapping):
                projected_servers[key] = copy.deepcopy(dict(legacy))
        return projected

    def export_snapshot(self) -> Schema4ExportSnapshot:
        """Return one validated canonical view of current authoritative memory."""

        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            payload = serialize_schema4_state(self._state)
            loaded = deserialize_schema4_bytes(
                payload, quarantine_invalid_servers=False
            )
            if not loaded.report.valid:
                raise Schema4ValidationError(
                    "authoritative export snapshot failed strict validation"
                )
            return Schema4ExportSnapshot(
                canonical_bytes=payload,
                schema_version=SCHEMA4_ROOT_VERSION,
                server_count=len(loaded.state.get("servers", {})),
                sha256=_sha256(payload),
            )

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
        return fired_suppression_keys(ledger)

    def record_consumer_decision(
        self, decision: AuthorityConsumerDecision, *, created_at: float
    ) -> bool:
        """Stage a consumer snapshot without forcing poll-by-poll persistence."""

        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            record = self._state.get("servers", {}).get(decision.server_key)
            if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
                raise Schema4ValidationError("consumer server is unavailable or quarantined")
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
            if consumer_ledger_to_mapping(updated) == consumer_ledger_to_mapping(ledger):
                return False
            candidate = copy.deepcopy(self._state)
            candidate["servers"][decision.server_key]["consumer_state"] = (
                consumer_ledger_to_mapping(updated)
            )
            serialize_schema4_state(candidate)
            self._state = candidate
            self._dirty_servers.add(decision.server_key)
            return True

    def reserve_consumer_alert(
        self,
        *,
        decision: AuthorityConsumerDecision,
        key: AuthoritySuppressionKey,
        created_at: float,
    ) -> ConsumerAlertReservation:
        """Atomically consume one alert slot before external action dispatch."""

        self._ensure_open()
        if key.server_key != decision.server_key:
            raise Schema4ValidationError("consumer alert key server mismatch")
        with self._mutex:
            assert self._state is not None
            record = self._state.get("servers", {}).get(decision.server_key)
            if not isinstance(record, Mapping) or record.get("authority_status") == "quarantined":
                raise Schema4ValidationError("consumer server is unavailable or quarantined")
            ledger = consumer_ledger_from_mapping(
                record.get("consumer_state"), server_key=decision.server_key
            )
            ledger = with_decision(ledger, decision)
            if key in fired_suppression_keys(ledger):
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
            candidate = copy.deepcopy(self._state)
            candidate["servers"][decision.server_key]["consumer_state"] = (
                consumer_ledger_to_mapping(ledger)
            )
            serialize_schema4_state(candidate)
            self._state = candidate
            self._dirty_servers.add(decision.server_key)
            written = self.flush()
            return ConsumerAlertReservation(
                True,
                key,
                reserved,
                written,
                ("reserved_and_durable_before_dispatch", "at_most_once_delivery"),
            )

    def complete_consumer_alert(
        self,
        *,
        decision: AuthorityConsumerDecision,
        reservation: ConsumerAlertReservation,
        completed_at: float,
        action_succeeded: bool,
    ) -> Schema4WriteResult:
        """Append dispatch outcome; reservation remains replay-suppressing on failure."""

        self._ensure_open()
        if not reservation.reserved or reservation.record is None:
            raise Schema4RuntimeError("consumer alert was not reserved")
        with self._mutex:
            assert self._state is not None
            record = self._state["servers"][decision.server_key]
            ledger = consumer_ledger_from_mapping(
                record.get("consumer_state"), server_key=decision.server_key
            )
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
            candidate = copy.deepcopy(self._state)
            candidate["servers"][decision.server_key]["consumer_state"] = (
                consumer_ledger_to_mapping(ledger)
            )
            serialize_schema4_state(candidate)
            self._state = candidate
            self._dirty_servers.add(decision.server_key)
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
        return Schema4RuntimeSnapshot(
            enabled=True,
            active_path=str(self.active_path),
            generation=self._generation,
            active_sha256=self._expected_sha256,
            valid_server_keys=() if report is None else report.valid_server_keys,
            quarantined_server_keys=() if report is None else report.quarantined_server_keys,
            dirty_server_keys=tuple(sorted(self._dirty_servers)),
            write_count=self._write_count,
            decision_ids=tuple(sorted(decisions)),
            recovery=self._recovery,
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
            assert self._state is not None
            servers = self._state.setdefault("servers", {})
            existing = servers.get(server_key)
            snapshot = self._state.get("schema3_snapshot")
            snapshot_servers = (
                snapshot.get("servers") if isinstance(snapshot, Mapping) else None
            )
            if not isinstance(snapshot_servers, Mapping):
                raise Schema4ValidationError(
                    "schema-4 state has no writable schema-3 server projection"
                )
            if existing is None:
                if server_key in snapshot_servers:
                    raise Schema4ValidationError(
                        f"schema-4 server {server_key!r} is unavailable"
                    )
                existing_for_rebuild: Mapping[str, object] = {}
            elif not isinstance(existing, Mapping):
                raise Schema4ValidationError(
                    f"schema-4 server {server_key!r} is unavailable or quarantined"
                )
            elif (
                existing.get("authority_status")
                not in SCHEMA4_RUNTIME_WRITABLE_AUTHORITY_STATUSES
            ):
                raise Schema4ValidationError(
                    f"schema-4 server {server_key!r} is unavailable or quarantined"
                )
            else:
                existing_for_rebuild = existing
            rebuilt = rebuild_schema4_server_record(
                existing_for_rebuild,
                schema3_record,
                server_key=server_key,
                updated_at=updated_at,
            )
            before = canonical_json_bytes(existing_for_rebuild)
            after = canonical_json_bytes(rebuilt)
            if before == after:
                return False
            reasons = set(rebuilt.get("reason_codes", ()))
            reasons.add(str(durable_reason))
            rebuilt["reason_codes"] = sorted(reasons)
            candidate = copy.deepcopy(self._state)
            candidate["servers"][server_key] = rebuilt
            candidate_snapshot = candidate["schema3_snapshot"]
            candidate_snapshot["servers"][server_key] = copy.deepcopy(
                dict(schema3_record)
            )
            candidate_snapshot["updated_at"] = max(
                int(updated_at),
                int(candidate_snapshot.get("created_at", 0) or 0),
            )
            candidate["updated_at"] = max(
                int(updated_at), int(candidate.get("created_at", 0) or 0)
            )
            serialize_schema4_state(candidate)
            self._state = candidate
            self._dirty_servers.add(server_key)
            return True

    def replace_server_record(
        self, server_key: str, record: Mapping[str, object], *, reason: str
    ) -> bool:
        """Test/maintenance hook for a fully derived immutable server record."""

        self._ensure_open()
        with self._mutex:
            assert self._state is not None
            candidate = copy.deepcopy(self._state)
            candidate.setdefault("servers", {})[server_key] = copy.deepcopy(dict(record))
            serialize_schema4_state(candidate)
            current = self._state["servers"].get(server_key)
            if canonical_json_bytes(current) == canonical_json_bytes(record):
                return False
            candidate["servers"][server_key]["reason_codes"] = sorted(
                set(candidate["servers"][server_key].get("reason_codes", ()))
                | {str(reason)}
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

    def flush(self) -> Schema4WriteResult:
        self._ensure_open()
        with self._mutex:
            if not self.dirty:
                return Schema4WriteResult(
                    wrote=False,
                    generation=self._generation,
                    sha256=self._expected_sha256,
                    dirty_server_keys=(),
                    reason_codes=("unchanged_no_write",),
                )
            assert self._state is not None
            current = self.active_path.read_bytes()
            current_hash = _sha256(current)
            if current_hash != self._expected_sha256:
                raise Schema4GenerationConflict(
                    "schema-4 active file changed after load; reload before retry"
                )
            next_state = copy.deepcopy(self._state)
            next_generation = self._generation + 1
            next_state["runtime_write_generation"] = next_generation
            next_state["runtime_write_semantics_version"] = (
                SCHEMA4_RUNTIME_WRITE_SEMANTICS_VERSION
            )
            next_state["previous_runtime_payload_sha256"] = current_hash
            payload = serialize_schema4_state(next_state)
            dirty = tuple(sorted(self._dirty_servers))
            _install_atomic_schema4(
                self.active_path,
                payload,
                previous_valid_bytes=current,
                last_known_good_path=self.last_known_good_path,
                crash_injector=self._crash_injector,
            )
            self._state = next_state
            self._generation = next_generation
            self._expected_sha256 = _sha256(payload)
            self._dirty_servers.clear()
            self._root_dirty = False
            self._write_count += 1
            loaded = deserialize_schema4_bytes(payload, quarantine_invalid_servers=True)
            self._load_result = loaded
            return Schema4WriteResult(
                wrote=True,
                generation=self._generation,
                sha256=self._expected_sha256,
                dirty_server_keys=dirty,
                reason_codes=("validated_atomic_replace", "last_known_good_rotated"),
            )

    def close(self, *, flush: bool = True) -> Schema4WriteResult | None:
        if self._closed:
            return None
        try:
            return self.flush() if flush and self.enabled and self.dirty else None
        finally:
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
    if not path.exists():
        raise Schema4RuntimeError(
            "authoritative schema-4 runtime will not create or migrate state implicitly"
        )
    removed: list[str] = []
    for stale in sorted(path.parent.glob(f".{path.name}.schema4-runtime-*.tmp")):
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
        if schema_version_from_bytes(active) != SCHEMA4_ROOT_VERSION:
            raise Schema4VersionError("active state is not schema 4")
        loaded = deserialize_schema4_bytes(active, quarantine_invalid_servers=True)
        if not loaded.report.root_valid:
            raise Schema4ValidationError("active schema-4 root invalid")
        reasons.add("valid_active_schema4_retained")
        return Schema4RecoveryResult(tuple(removed), False, None, tuple(sorted(reasons)))
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
            loaded = deserialize_schema4_bytes(
                backup_bytes, quarantine_invalid_servers=False
            )
            if not loaded.report.valid:
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
        return Schema4RecoveryResult(
            tuple(removed), True, str(invalid_path), tuple(sorted(reasons))
        )


def _install_atomic_schema4(
    active_path: Path,
    payload: bytes,
    *,
    previous_valid_bytes: bytes,
    last_known_good_path: Path,
    crash_injector: Callable[[Schema4CrashPoint], None] | None,
) -> None:
    try:
        active_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_name = tempfile.mkstemp(
            prefix=f".{active_path.name}.schema4-runtime-",
            suffix=".tmp",
            dir=active_path.parent,
        )
    except OSError as exc:
        try:
            unchanged = active_path.read_bytes() == previous_valid_bytes
        except OSError:
            unchanged = False
        phase = (
            Schema4WriteFailurePhase.CONFIRMED_PRE_COMMIT
            if unchanged
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
        # Validate the exact bytes that will be installed, not only the mapping.
        loaded = deserialize_schema4_bytes(
            temp_path.read_bytes(), quarantine_invalid_servers=False
        )
        if not (
            loaded.report.root_valid
            and all(item.code == "server_quarantined" for item in loaded.report.issues)
        ):
            raise Schema4ValidationError("prepared runtime payload failed validation")
        if crash_injector is not None:
            crash_injector(Schema4CrashPoint.AFTER_TEMP_FSYNC_BEFORE_REPLACE)
        _replace_bytes_fsynced(last_known_good_path, previous_valid_bytes)
        os.replace(temp_path, active_path)
        active_replaced = True
        _fsync_directory(active_path.parent)
        if crash_injector is not None:
            crash_injector(Schema4CrashPoint.AFTER_REPLACE)
        if active_path.read_bytes() != payload:
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
        if not active_replaced:
            try:
                unchanged = active_path.read_bytes() == previous_valid_bytes
            except OSError:
                unchanged = False
            if unchanged:
                phase = Schema4WriteFailurePhase.CONFIRMED_PRE_COMMIT
        raise Schema4WriteFailure(phase, exc) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
