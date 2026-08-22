from __future__ import annotations

import argparse
import copy
import dataclasses
import enum
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .companion_restart_phase2_authority import (
    AUTHORITY_SCHEMA_VERSION,
    AUTHORITY_SEMANTIC_VERSION,
    AuthorityCandidate,
    AuthorityDecision,
    AuthorityGate,
    AuthorityOrigin,
    ChallengerAuthorization,
    ChallengerContext,
    ChallengerStatus,
    HighEvidenceLedger,
    NormalCandidateEvidence,
    NormalEvidenceLedger,
    RegimeRecord,
    RegimeRecordStatus,
    RegimeState,
    evaluate_authority,
)
from .companion_restart_phase2_continuity import (
    CONTINUITY_PROVENANCE_VERSION,
    CONTINUITY_SCHEMA_VERSION,
    CadenceStreak,
    ChainLifecycle,
    ContinuityChain,
    ContinuitySpan,
    ContinuitySpanKind,
    CoverageQuality,
    IntervalRelationship,
    RelationshipKind,
    StreakLifecycle,
    build_cadence_streaks,
    build_continuity_chains,
    extract_interval_relationships,
    observed_transition_spans_from_events,
    reconstruct_historical_event_provenance,
)
from .companion_restart_phase2_detection import (
    DetectionConfig,
    PhysicalRestartEvent,
    normalize_event_restart_start_phase,
)
from .companion_restart_phase2_consumer_state import (
    alert_record,
    append_alert,
    compact_consumer_ledger,
    empty_consumer_state,
    ledger_from_mapping as consumer_ledger_from_mapping,
    ledger_to_mapping as consumer_ledger_to_mapping,
    latest_alert_records,
    validate_consumer_state,
    with_decision,
)
from .companion_restart_phase2_authority_consumers import (
    AuthorityConsumerPolicyInput,
    AuthoritySuppressionKind,
    evaluate_authority_consumers,
)
from .companion_restart_phase2_expected_windows import (
    EXPECTED_WINDOW_SCHEMA_VERSION,
    EXPECTED_WINDOW_SEMANTIC_VERSION,
    ExpectedWindowEpisodeEvidence,
    ExpectedWindowLedgerRecord,
    ExpectedWindowOutcome,
    ExpectedWindowResult,
    ExpectedWindowRevision,
    ReconciliationReason,
    WindowCoverageClassification,
    classify_expected_window,
    continuity_spans_from_coverage,
    derive_active_miss_ledger,
    deterministic_model_revision_id,
    import_legacy_expected_miss,
    reconcile_expected_window_ledger,
)
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    CoverageTimeline,
    PhaseRecencyPolicy,
    RestartScheduleScorer,
    candidate_phase_tolerance,
)
from .companion_restart_phase2_storage import (
    atomic_write_bytes,
    initialize_phase2_state,
    new_phase2_state,
    normalize_phase2_state,
)


SCHEMA4_ROOT_VERSION = 4
SCHEMA4_RUNTIME_STATE_VERSION = 2
SCHEMA4_PHYSICAL_EVENT_PROVENANCE_VERSION = CONTINUITY_PROVENANCE_VERSION
SCHEMA4_CONTINUITY_SEMANTICS_VERSION = 1
SCHEMA4_RELATIONSHIP_SEMANTICS_VERSION = 1
SCHEMA4_STREAK_SEMANTICS_VERSION = 1
SCHEMA4_EXPECTED_WINDOW_SEMANTICS_VERSION = EXPECTED_WINDOW_SEMANTIC_VERSION
SCHEMA4_AUTHORITY_SEMANTICS_VERSION = AUTHORITY_SEMANTIC_VERSION
SCHEMA4_REGIME_SEMANTICS_VERSION = 1
SCHEMA4_CONSUMER_ADAPTER_VERSION = 1
SCHEMA4_MIGRATION_TOOL_VERSION = 2
SCHEMA4_SCORING_ALGORITHM_VERSION = 3
SCHEMA4_SUPPORTED_MIGRATION_TOOL_VERSIONS = frozenset({1, 2})
SCHEMA4_SUPPORTED_SCORING_ALGORITHM_VERSIONS = frozenset({2, 3})
SCHEMA4_RECONCILIATION_EVIDENCE_ID_VERSION = 1
SCHEMA4_APPLY_APPROVAL_TOKEN = "APPLY-SCHEMA4-PREPARED-STATE"


class Schema4Error(ValueError):
    """Base error for explicit schema-4 maintenance operations."""


class Schema4VersionError(Schema4Error):
    """Input has an unsupported root schema version."""


class Schema4ValidationError(Schema4Error):
    """Schema-4 content or immutable references are invalid."""


class Schema4StartupPreparationStatus(str, enum.Enum):
    FRESH_CREATED = "fresh_created"
    SCHEMA3_MIGRATED = "schema3_migrated"
    SCHEMA4_RETAINED = "schema4_retained"
    DEFERRED_TO_RUNTIME_RECOVERY = "deferred_to_runtime_recovery"


@dataclass(frozen=True)
class Schema4StartupPreparationResult:
    status: Schema4StartupPreparationStatus
    active_path: Path
    backup_path: Path | None = None
    audit_path: Path | None = None
    source_sha256: str | None = None
    legacy_reset_performed: bool = False
    legacy_backup_path: Path | None = None


@dataclass(frozen=True)
class Schema4ValidationIssue:
    server_key: str | None
    code: str
    detail: str


@dataclass(frozen=True)
class Schema4ValidationReport:
    root_valid: bool
    valid_server_keys: tuple[str, ...]
    quarantined_server_keys: tuple[str, ...]
    issues: tuple[Schema4ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return self.root_valid and not self.quarantined_server_keys


@dataclass(frozen=True)
class Schema4LoadResult:
    state: dict
    report: Schema4ValidationReport


@dataclass(frozen=True)
class Schema4MigrationResult:
    source_schema_version: int
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    backup_path: Path | None
    backup_size: int | None
    backup_sha256: str | None
    prepared_path: Path
    prepared_size: int
    prepared_sha256: str
    audit_path: Path
    state: dict
    validation: Schema4ValidationReport
    idempotent_noop: bool


@dataclass(frozen=True)
class Schema4ShadowComparison:
    server_key: str
    in_memory_decision_id: str
    persisted_decision_id: str | None
    decision_id_equal: bool
    in_memory_relationship_count: int
    persisted_relationship_count: int
    in_memory_streak_count: int
    persisted_streak_count: int
    in_memory_expected_window_count: int
    persisted_expected_window_count: int
    object_counts_equal: bool
    migration_warnings: tuple[str, ...]
    reason_codes: tuple[str, ...]


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def schema_version_from_bytes(raw: bytes) -> int:
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Schema4ValidationError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise Schema4VersionError("state has no integer root schema_version")
    return int(value["schema_version"])


def serialize_schema4_state(state: object) -> bytes:
    loaded = deserialize_schema4_state(state, quarantine_invalid_servers=False)
    if not _report_acceptable_for_persistence(loaded.report):
        detail = "; ".join(item.detail for item in loaded.report.issues)
        raise Schema4ValidationError(detail or "schema-4 validation failed")
    return canonical_json_bytes(loaded.state)


def deserialize_schema4_bytes(
    raw: bytes, *, quarantine_invalid_servers: bool = True
) -> Schema4LoadResult:
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise Schema4ValidationError(f"invalid schema-4 JSON: {exc}") from exc
    return deserialize_schema4_state(
        value, quarantine_invalid_servers=quarantine_invalid_servers
    )


def deserialize_schema4_state(
    state: object, *, quarantine_invalid_servers: bool = True
) -> Schema4LoadResult:
    if not isinstance(state, dict):
        raise Schema4ValidationError("schema-4 root must be a mapping")
    version = state.get("schema_version")
    if type(version) is not int or version != SCHEMA4_ROOT_VERSION:
        raise Schema4VersionError(
            f"unsupported restart-learning schema {version!r}; expected 4"
        )
    normalized = copy.deepcopy(state)
    issues: list[Schema4ValidationIssue] = []
    root_valid = _validate_root(normalized, issues)
    servers = normalized.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        normalized["servers"] = servers
        root_valid = False
    valid_keys: list[str] = []
    quarantined: list[str] = []
    for raw_key in sorted(tuple(servers)):
        key = str(raw_key)
        record = servers[raw_key]
        server_issues = _validate_server_record(key, record)
        if server_issues:
            issues.extend(server_issues)
            quarantined.append(key)
            if quarantine_invalid_servers:
                servers[key] = {
                    "server_key": key,
                    "authority_status": "quarantined",
                    "schema_version": SCHEMA4_ROOT_VERSION,
                    "semantic_version": SCHEMA4_AUTHORITY_SEMANTICS_VERSION,
                    "validation_errors": [
                        {"code": item.code, "detail": item.detail}
                        for item in server_issues
                    ],
                    "quarantined_payload": copy.deepcopy(record),
                    "reason_codes": ["schema4_server_authority_quarantined"],
                }
        else:
            valid_keys.append(key)
    normalized["servers"] = {
        key: servers[key] for key in sorted(servers, key=str)
    }
    return Schema4LoadResult(
        state=normalized,
        report=Schema4ValidationReport(
            root_valid=root_valid,
            valid_server_keys=tuple(valid_keys),
            quarantined_server_keys=tuple(quarantined),
            issues=tuple(issues),
        ),
    )


def migrate_state_file(
    *,
    source_path: str | os.PathLike[str],
    prepared_path: str | os.PathLike[str],
    audit_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str] | None,
    server_key: str | None = None,
    source_snapshot: bytes | None = None,
    source_snapshot_mtime_ns: int | None = None,
) -> Schema4MigrationResult:
    """Prepare schema 4 beside the source; never replace the source."""

    source = Path(source_path)
    prepared = Path(prepared_path)
    audit = Path(audit_path)
    if source_snapshot is None:
        source_stat = source.stat()
        source_bytes = source.read_bytes()
        source_mtime_ns = source_stat.st_mtime_ns
    else:
        source_bytes = bytes(source_snapshot)
        if source_snapshot_mtime_ns is None:
            raise Schema4Error(
                "immutable source snapshot requires its captured mtime_ns"
            )
        source_mtime_ns = int(source_snapshot_mtime_ns)
    source_hash = _sha256(source_bytes)
    version = schema_version_from_bytes(source_bytes)

    parsed_schema3 = None
    schema4_load = None
    if version == SCHEMA4_ROOT_VERSION:
        schema4_load = deserialize_schema4_bytes(
            source_bytes, quarantine_invalid_servers=False
        )
        if not schema4_load.report.valid:
            raise Schema4ValidationError("schema-4 source failed validation")
    elif version == 3:
        try:
            parsed_schema3 = json.loads(
                source_bytes.decode("utf-8"), parse_constant=_reject_constant
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise Schema4ValidationError(f"invalid schema-3 JSON: {exc}") from exc
        # Validate the exact immutable representation that conversion will use.
        # Keep the original parsed mapping for migration so schema3_snapshot
        # remains an exact semantic projection of the source bytes.
        normalize_phase2_state(parsed_schema3)
    else:
        raise Schema4VersionError(
            f"unsupported source schema {version}; no file was changed"
        )

    backup: Path | None = None
    if backup_path is not None:
        backup = Path(backup_path)
        if backup.resolve() == source.resolve():
            raise Schema4Error("backup path must differ from source")
        if backup.exists() and backup.read_bytes() != source_bytes:
            raise Schema4Error("existing backup does not match source bytes")
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(backup, source_bytes)
        if backup.read_bytes() != source_bytes:
            raise Schema4Error("backup verification failed")

    if version == SCHEMA4_ROOT_VERSION:
        assert schema4_load is not None
        prepared_bytes = source_bytes
        state = schema4_load.state
        idempotent = True
    elif version == 3:
        assert parsed_schema3 is not None
        state = migrate_schema3_state(
            parsed_schema3,
            source_bytes=source_bytes,
            source_mtime_ns=source_mtime_ns,
            only_server_key=server_key,
        )
        prepared_bytes = serialize_schema4_state(state)
        idempotent = False

    if prepared.resolve() == source.resolve():
        raise Schema4Error("prepared output must not replace source")
    prepared.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(prepared, prepared_bytes)
    reread = prepared.read_bytes()
    if reread != prepared_bytes:
        raise Schema4Error("prepared output verification failed")
    validation = deserialize_schema4_bytes(
        reread, quarantine_invalid_servers=False
    ).report
    if not _report_acceptable_for_persistence(validation):
        raise Schema4ValidationError("prepared output reference validation failed")
    audit_payload = copy.deepcopy(state["migration_audit"])
    audit.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(audit, canonical_json_bytes(audit_payload))
    return Schema4MigrationResult(
        source_schema_version=version,
        source_size=len(source_bytes),
        source_mtime_ns=source_mtime_ns,
        source_sha256=source_hash,
        backup_path=backup,
        backup_size=None if backup is None else backup.stat().st_size,
        backup_sha256=None if backup is None else _sha256(backup.read_bytes()),
        prepared_path=prepared,
        prepared_size=len(reread),
        prepared_sha256=_sha256(reread),
        audit_path=audit,
        state=state,
        validation=validation,
        idempotent_noop=idempotent,
    )


def prepare_schema4_state_for_startup(
    *,
    active_path: str | os.PathLike[str],
    legacy_path: str | os.PathLike[str] | None = None,
    now: int | float | None = None,
    generation_id: str | None = None,
) -> Schema4StartupPreparationResult:
    """Prepare only expected fresh/schema-3 startup state for strict schema 4.

    Existing corrupt, malformed, and unsupported state is deliberately left
    untouched for ``AuthoritativeSchema4Runtime.open`` to recover from a valid
    schema-4 LKG or reject.  Callers must provide their own failure boundary.
    """

    active = Path(active_path)
    legacy = None if legacy_path is None else Path(legacy_path)
    legacy_result = None
    if not active.exists() and legacy is not None and legacy.exists():
        legacy_result = initialize_phase2_state(
            active_path=active,
            legacy_path=legacy,
            now=now,
            generation_id=generation_id,
        )
        if not legacy_result.persistence_enabled:
            raise Schema4Error(
                "schema-4 startup could not safely prepare legacy phase-1 state: "
                f"{legacy_result.error or legacy_result.failure or 'unknown failure'}"
            )

    if not active.exists():
        result = _create_fresh_schema4_state(
            active=active,
            now=now,
            generation_id=generation_id,
        )
        return dataclasses.replace(
            result,
            legacy_reset_performed=bool(
                legacy_result is not None and legacy_result.reset_performed
            ),
            legacy_backup_path=(
                None if legacy_result is None else legacy_result.backup_path
            ),
        )

    with active.open("rb") as source_handle:
        raw = source_handle.read()
        source_mtime_ns = os.fstat(source_handle.fileno()).st_mtime_ns
    try:
        version = schema_version_from_bytes(raw)
    except Schema4Error:
        return Schema4StartupPreparationResult(
            Schema4StartupPreparationStatus.DEFERRED_TO_RUNTIME_RECOVERY,
            active,
        )
    if version == SCHEMA4_ROOT_VERSION:
        return Schema4StartupPreparationResult(
            Schema4StartupPreparationStatus.SCHEMA4_RETAINED,
            active,
            legacy_reset_performed=bool(
                legacy_result is not None and legacy_result.reset_performed
            ),
            legacy_backup_path=(
                None if legacy_result is None else legacy_result.backup_path
            ),
        )
    if version != 3:
        return Schema4StartupPreparationResult(
            Schema4StartupPreparationStatus.DEFERRED_TO_RUNTIME_RECOVERY,
            active,
        )

    try:
        schema3_snapshot = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_constant
        )
        normalize_phase2_state(schema3_snapshot)
    except Exception:
        return Schema4StartupPreparationResult(
            Schema4StartupPreparationStatus.DEFERRED_TO_RUNTIME_RECOVERY,
            active,
        )

    result = _migrate_schema3_for_startup(
        active,
        source_snapshot=raw,
        source_snapshot_mtime_ns=source_mtime_ns,
    )
    return dataclasses.replace(
        result,
        legacy_reset_performed=bool(
            legacy_result is not None and legacy_result.reset_performed
        ),
        legacy_backup_path=(
            None if legacy_result is None else legacy_result.backup_path
        ),
    )


def _create_fresh_schema4_state(
    *,
    active: Path,
    now: int | float | None,
    generation_id: str | None,
) -> Schema4StartupPreparationResult:
    from .companion_restart_phase2_schema4_runtime import Schema4WriterLock

    raced = False
    with Schema4WriterLock(active):
        if active.exists():
            raced = True
        else:
            schema3 = new_phase2_state(now=now, generation_id=generation_id)
            source = canonical_json_bytes(schema3)
            state = migrate_schema3_state(
                schema3,
                source_bytes=source,
                source_mtime_ns=0,
            )
            payload = serialize_schema4_state(state)
            loaded = deserialize_schema4_bytes(
                payload, quarantine_invalid_servers=False
            )
            if not loaded.report.valid:
                raise Schema4ValidationError(
                    "fresh schema-4 startup state failed strict validation"
                )
            atomic_write_bytes(active, payload)
            installed = active.read_bytes()
            if installed != payload:
                raise Schema4Error(
                    "fresh schema-4 startup state failed byte verification"
                )
            verified = deserialize_schema4_bytes(
                installed, quarantine_invalid_servers=False
            )
            if not verified.report.valid:
                raise Schema4ValidationError(
                    "installed fresh schema-4 startup state failed validation"
                )
            return Schema4StartupPreparationResult(
                Schema4StartupPreparationStatus.FRESH_CREATED,
                active,
                source_sha256=_sha256(source),
            )
    if raced:
        return prepare_schema4_state_for_startup(
            active_path=active,
            now=now,
            generation_id=generation_id,
        )
    raise Schema4Error("fresh schema-4 startup preparation did not complete")


def _migrate_schema3_for_startup(
    active: Path,
    *,
    source_snapshot: bytes,
    source_snapshot_mtime_ns: int,
) -> Schema4StartupPreparationResult:
    source = bytes(source_snapshot)
    source_sha256 = _sha256(source)
    identity = source_sha256
    backup = active.with_name(f"{active.name}.pre-schema4-{identity}.json")
    audit = active.with_name(
        f"{active.name}.schema4-migration-audit-{identity}.json"
    )
    prepared_fd, prepared_name = tempfile.mkstemp(
        prefix=f".{active.name}.schema4-startup-prepared-",
        suffix=".tmp",
        dir=active.parent,
    )
    os.close(prepared_fd)
    prepared = Path(prepared_name)
    try:
        result = migrate_state_file(
            source_path=active,
            prepared_path=prepared,
            audit_path=audit,
            backup_path=backup,
            source_snapshot=source,
            source_snapshot_mtime_ns=source_snapshot_mtime_ns,
        )
        apply_prepared_schema4(
            active_path=active,
            prepared_path=result.prepared_path,
            verified_backup_path=result.backup_path,
            expected_source_sha256=result.source_sha256,
            approval_token=SCHEMA4_APPLY_APPROVAL_TOKEN,
        )
        installed = deserialize_schema4_bytes(
            active.read_bytes(), quarantine_invalid_servers=False
        )
        if not installed.report.valid:
            raise Schema4ValidationError(
                "automatic schema-3 migration failed installed-state validation"
            )
        return Schema4StartupPreparationResult(
            Schema4StartupPreparationStatus.SCHEMA3_MIGRATED,
            active,
            backup_path=result.backup_path,
            audit_path=result.audit_path,
            source_sha256=result.source_sha256,
        )
    except Exception as exc:
        rollback_error = None
        try:
            # Roll back only bytes installed by this transaction.  A live
            # schema-3 source changed concurrently is an apply conflict and
            # must never be overwritten with the older snapshot.
            if (
                backup.exists()
                and active.exists()
                and prepared.exists()
                and active.read_bytes() == prepared.read_bytes()
            ):
                rollback_whole_file(
                    active_path=active,
                    verified_backup_path=backup,
                    expected_backup_sha256=source_sha256,
                )
        except Exception as restore_exc:
            rollback_error = restore_exc
        if rollback_error is not None:
            raise Schema4Error(
                "automatic schema-3 migration failed and byte-identical rollback "
                f"also failed: migration={exc}; rollback={rollback_error}"
            ) from rollback_error
        raise
    finally:
        try:
            prepared.unlink()
        except OSError:
            pass


def migrate_schema3_state(
    state: object,
    *,
    source_bytes: bytes,
    source_mtime_ns: int,
    only_server_key: str | None = None,
) -> dict:
    if not isinstance(state, dict) or state.get("schema_version") != 3:
        raise Schema4VersionError("explicit migration requires schema 3")
    source_hash = _sha256(source_bytes)
    raw_servers = state.get("servers")
    if not isinstance(raw_servers, dict):
        raw_servers = {}
    selected_keys = sorted(str(key) for key in raw_servers)
    if only_server_key is not None:
        selected_keys = [key for key in selected_keys if key == only_server_key]
        if not selected_keys:
            raise Schema4Error(f"server {only_server_key!r} is not present")
    migrated_servers: dict[str, dict] = {}
    server_audits: list[dict] = []
    for key in selected_keys:
        raw = raw_servers[key]
        try:
            record, audit = _migrate_server(
                key,
                raw,
                state=state,
                source_bytes=source_bytes,
            )
        except Exception as exc:
            record = _quarantined_migration_record(key, raw, exc)
            audit = _quarantined_migration_audit(key, raw, exc)
        migrated_servers[key] = record
        server_audits.append(audit)
    totals = _migration_totals(server_audits)
    migration_id = _stable_id(
        "schema3-to-schema4-migration",
        SCHEMA4_MIGRATION_TOOL_VERSION,
        source_hash,
        tuple(selected_keys),
    )
    migrated_at = _safe_nonnegative_number(state.get("updated_at"), 0)
    audit = {
        "audit_id": migration_id,
        "schema_version": SCHEMA4_ROOT_VERSION,
        "semantic_version": SCHEMA4_MIGRATION_TOOL_VERSION,
        "migration_tool_version": SCHEMA4_MIGRATION_TOOL_VERSION,
        "source_schema_version": 3,
        "source_size": len(source_bytes),
        "source_mtime_ns": int(source_mtime_ns),
        "source_sha256": source_hash,
        "migrated_at": migrated_at,
        "selected_server_key": only_server_key,
        "servers": server_audits,
        "global_totals": totals,
        "validation_result": "valid_after_prepared_reload",
        "reason_codes": [
            "explicit_detached_schema4_migration",
            "ordinary_startup_migration_forbidden",
            "source_bytes_preserved",
        ],
    }
    return {
        "schema_version": SCHEMA4_ROOT_VERSION,
        "runtime_state_version": SCHEMA4_RUNTIME_STATE_VERSION,
        "scoring_algorithm_version": SCHEMA4_SCORING_ALGORITHM_VERSION,
        "state_generation_id": _stable_id("schema4-generation", source_hash),
        "created_at": _safe_nonnegative_number(state.get("created_at"), 0),
        "updated_at": migrated_at,
        "version_manifest": _version_manifest(),
        "source_schema3": {
            "size": len(source_bytes),
            "mtime_ns": int(source_mtime_ns),
            "sha256": source_hash,
            "state_generation_id": state.get("state_generation_id"),
        },
        "schema3_snapshot": copy.deepcopy(state),
        "servers": migrated_servers,
        "migration_audit": audit,
        "reason_codes": [
            "schema4_shadow_persistence_only",
            "production_schema3_authority_unchanged",
        ],
    }


def _migrate_server(
    server_key: str,
    raw: object,
    *,
    state: Mapping[str, object],
    source_bytes: bytes,
) -> tuple[dict, dict]:
    if not isinstance(raw, dict):
        raise Schema4ValidationError("schema-3 server record is not a mapping")
    # Runtime helpers are imported lazily to avoid making ordinary runtime load
    # depend on schema-4 maintenance code.
    from .companion_restart_phase2_runtime import (
        _deserialize_coverage,
        _deserialize_event,
        _deserialize_server,
        _serialize_event,
    )

    sessions = tuple(
        item for item in raw.get("monitoring_sessions", ()) if isinstance(item, dict)
    )
    coverage = tuple(
        _deserialize_coverage(item)
        for item in raw.get("coverage_segments", ())
        if isinstance(item, dict)
    )
    events = tuple(
        _deserialize_event(item)
        for item in raw.get("events", ())
        if isinstance(item, dict)
    )
    provenance = tuple(
        reconstruct_historical_event_provenance(item, sessions) for item in events
    )
    reconstructed_events = tuple(item.event for item in provenance)
    proven_events = tuple(item.event for item in provenance if item.proven)
    spans = continuity_spans_from_coverage(
        coverage,
        server_key=server_key,
        monitoring_sessions=sessions,
    )
    transitions = observed_transition_spans_from_events(proven_events)
    spans = tuple(
        sorted(
            {item.reference_id: item for item in (*spans, *transitions)}.values(),
            key=lambda item: (item.start_at, item.end_at, item.reference_id),
        )
    )
    chains = _deduplicate_chains(build_continuity_chains(spans))
    relationships = extract_interval_relationships(reconstructed_events, spans)
    streaks = build_cadence_streaks(relationships, chains=chains)

    server_runtime = _deserialize_server(server_key, raw, DetectionConfig())
    now = float(_safe_nonnegative_number(state.get("updated_at"), 0))
    score = RestartScheduleScorer().score(
        server_runtime.events,
        CoverageTimeline(tuple(server_runtime.coverage)),
        now=now,
        incumbent_period_seconds=server_runtime.incumbent_period_seconds,
        aggregate=server_runtime.aggregate,
        expected_misses=server_runtime.expected_misses,
        regime_boundaries=(item.ended_at for item in server_runtime.prior_regimes),
        phase_recency_policy=PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )

    episode_evidence = tuple(
        ExpectedWindowEpisodeEvidence(
            evidence_id=str(item.get("event_id") or _stable_id("episode", item)),
            start_at=float(_safe_nonnegative_number(item.get("started_at"), 0)),
            end_at=_optional_nonnegative_number(item.get("finalized_at")),
            outage_observed=False,
            recovery_observed=False,
            unresolved=True,
            restart_like=True,
            reason_codes=("persisted_incomplete_episode_diagnostic",),
        )
        for item in raw.get("incomplete_episodes", ())
        if isinstance(item, dict)
    )
    original_windows: list[ExpectedWindowResult] = []
    classifications: list[ExpectedWindowResult] = []
    for item in raw.get("expected_window_misses", ()):
        if not isinstance(item, dict):
            continue
        period = int(item.get("period_seconds"))
        if period not in CANDIDATE_PERIODS:
            continue
        expected = float(item.get("expected_at"))
        tolerance = candidate_phase_tolerance(period)
        phase = expected % period
        legacy_key = str(item.get("key") or f"{period}:{round(expected)}")
        model_revision = deterministic_model_revision_id(
            server_key, period, phase, f"schema3:{legacy_key}"
        )
        original_windows.append(
            import_legacy_expected_miss(
                server_key=server_key,
                candidate_period_seconds=period,
                expected_phase_offset=phase,
                window_start_at=max(0.0, expected - tolerance),
                expected_at=expected,
                window_end_at=expected + tolerance,
                model_revision_id=model_revision,
                legacy_key=legacy_key,
            )
        )
        classifications.append(
            classify_expected_window(
                server_key=server_key,
                candidate_period_seconds=period,
                expected_phase_offset=phase,
                window_start_at=max(0.0, expected - tolerance),
                expected_at=expected,
                window_end_at=expected + tolerance,
                model_revision_id=model_revision,
                events=events,
                spans=spans,
                episodes=episode_evidence,
            )
        )
    reconciled = _reconcile_migrated_expected_windows(
        original_windows=original_windows,
        classifications=classifications,
        events=events,
        spans=spans,
        episodes=episode_evidence,
    )
    before = evaluate_authority(
        server_key=server_key,
        normal_score=score,
        relationships=relationships,
        streaks=streaks,
        expected_window_ledger=original_windows,
        events=proven_events,
    )
    after = evaluate_authority(
        server_key=server_key,
        normal_score=score,
        relationships=relationships,
        streaks=streaks,
        expected_window_ledger=reconciled,
        events=proven_events,
        prior_decision=before,
    )
    expected_records = tuple(reconciled)
    normal_wrapper = _ledger_wrapper("normal", after.normal_ledger, after.decision_id)
    high_wrapper = _ledger_wrapper("high", after.high_ledger, after.decision_id)
    authority_decisions = tuple(
        {_decision_payload(item)["decision_id"]: _decision_payload(item) for item in (before, after)}.values()
    )
    record = {
        "schema_version": SCHEMA4_ROOT_VERSION,
        "semantic_version": SCHEMA4_AUTHORITY_SEMANTICS_VERSION,
        "server_key": server_key,
        "authority_status": "valid_shadow_only",
        "legacy_schema3_record": copy.deepcopy(raw),
        "physical_events": [
            {
                **_serialize_event(item, include_samples=True),
                "schema_version": SCHEMA4_ROOT_VERSION,
                "semantic_version": SCHEMA4_PHYSICAL_EVENT_PROVENANCE_VERSION,
                "physical_event_provenance_version": item.provenance_version,
            }
            for item in sorted(reconstructed_events, key=lambda value: value.sequence)
        ],
        "continuity_spans": [
            {
                "schema_version": CONTINUITY_SCHEMA_VERSION,
                "semantic_version": SCHEMA4_CONTINUITY_SEMANTICS_VERSION,
                **_primitive(item),
            }
            for item in spans
        ],
        "continuity_chains": [
            {
                "semantic_version": SCHEMA4_CONTINUITY_SEMANTICS_VERSION,
                **_primitive(item),
            }
            for item in _sorted_by_id(chains, "chain_id")
        ],
        "interval_relationships": [
            {
                "semantic_version": SCHEMA4_RELATIONSHIP_SEMANTICS_VERSION,
                **_primitive(item),
            }
            for item in _sorted_by_id(relationships, "relationship_id")
        ],
        "cadence_streaks": [
            {
                "semantic_version": SCHEMA4_STREAK_SEMANTICS_VERSION,
                **_primitive(item),
            }
            for item in _sorted_by_id(streaks, "streak_id")
        ],
        "expected_window_episode_evidence": [
            {
                "schema_version": EXPECTED_WINDOW_SCHEMA_VERSION,
                "semantic_version": EXPECTED_WINDOW_SEMANTIC_VERSION,
                **_primitive(item),
            }
            for item in sorted(episode_evidence, key=lambda value: value.evidence_id)
        ],
        "expected_window_results": [
            {"record_type": type(item).__name__, **_primitive(item)}
            for item in sorted(expected_records, key=lambda value: (value.expected_at, value.result_id))
        ],
        "normal_evidence_ledger": normal_wrapper,
        "high_evidence_ledger": high_wrapper,
        "authority_candidates": [
            _primitive(item) for item in sorted(after.candidates, key=lambda value: value.candidate_id)
        ],
        "challenger_contexts": [
            _primitive(item) for item in sorted(after.challenger_contexts, key=lambda value: value.context_id)
        ],
        "regimes": [
            _primitive(item) for item in sorted(after.regimes, key=lambda value: value.regime_id)
        ],
        "authority_decisions": sorted(authority_decisions, key=lambda value: value["decision_id"]),
        "prior_authority_decision_id": before.decision_id,
        "authority_decision": _decision_payload(after),
        "consumer_state": empty_consumer_state(server_key),
        "migration_audit": {},
        "reason_codes": [
            "derived_authority_shadow_only",
            "all_source_physical_events_preserved",
            "unproven_continuity_never_high_authority",
        ],
    }
    active_before = derive_active_miss_ledger(original_windows)
    active_after = derive_active_miss_ledger(reconciled)
    unproven_count = sum(not item.proven for item in provenance)
    high_count = sum(item.high_authority_eligible for item in relationships)
    audit = {
        "audit_id": _stable_id("schema4-server-audit", server_key, _sha256(source_bytes)),
        "schema_version": SCHEMA4_ROOT_VERSION,
        "semantic_version": SCHEMA4_MIGRATION_TOOL_VERSION,
        "server_key": server_key,
        "source_event_count": len(events),
        "source_coverage_count": len(coverage),
        "source_session_count": len(sessions),
        "source_expected_miss_count": len(original_windows),
        "continuity_chains_reconstructed": len(chains),
        "relationships_reconstructed": len(relationships),
        "high_eligible_relationships": high_count,
        "relationships_left_normal_only": len(relationships) - high_count,
        "streaks_reconstructed": len(streaks),
        "expected_window_rows_imported": len(original_windows),
        "revisions_created": len(reconciled) - len(original_windows),
        "active_misses_before": len(active_before.active_results),
        "active_misses_after": len(active_after.active_results),
        "authority_candidates": len(after.candidates),
        "selected_shadow_regime": None if after.selected_shadow_regime is None else after.selected_shadow_regime.regime_id,
        "selected_shadow_period_seconds": None if after.selected_shadow_regime is None else after.selected_shadow_regime.candidate_period_seconds,
        "h_gate": _decision_gate(after).value,
        "decision_id": after.decision_id,
        "migration_warnings": [],
        "unprovable_history": unproven_count,
        "dropped_duplicate_ids": 0,
        "reference_validation_result": "valid",
        "reason_codes": ["server_migration_completed"],
    }
    record["migration_audit"] = copy.deepcopy(audit)
    return record, audit


def _reconcile_migrated_expected_windows(
    *,
    original_windows: Sequence[ExpectedWindowResult],
    classifications: Sequence[ExpectedWindowResult],
    events: Sequence[PhysicalRestartEvent],
    spans: Sequence[ContinuitySpan],
    episodes: Sequence[ExpectedWindowEpisodeEvidence],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    """Reconcile each lineage with only its own immutable proving evidence."""

    if len(original_windows) != len(classifications):
        raise Schema4ValidationError(
            "expected-window migration classifications do not match lineages"
        )
    records: dict[str, ExpectedWindowLedgerRecord] = {}
    for original, desired in zip(original_windows, classifications):
        reconciled_at = _migration_reconciliation_timestamp(
            desired,
            events=events,
            spans=spans,
            episodes=episodes,
        )
        evidence_revision_id = _migration_reconciliation_evidence_revision_id(
            original,
            desired,
            reconciled_at=reconciled_at,
        )
        for record in reconcile_expected_window_ledger(
            (original,),
            (desired,),
            reconciled_at=reconciled_at,
            evidence_revision_id=evidence_revision_id,
        ):
            records[record.result_id] = record
    return tuple(
        sorted(records.values(), key=lambda item: (item.expected_at, item.result_id))
    )


def _migration_reconciliation_evidence_payload(
    original: ExpectedWindowResult,
    desired: ExpectedWindowResult,
    *,
    reconciled_at: float,
) -> dict:
    """Canonical server-local semantic proof identity for one reconciliation."""

    reason = None
    if desired.outcome is ExpectedWindowOutcome.HIT:
        reason = ReconciliationReason.FINALIZED_EVENT.value
    elif (
        desired.outcome is ExpectedWindowOutcome.AMBIGUOUS
        and desired.outage_observed
    ):
        reason = ReconciliationReason.OBSERVED_OUTAGE.value
    return {
        "semantic_tag": "schema3-expected-window-reconciliation-evidence",
        "semantic_version": SCHEMA4_RECONCILIATION_EVIDENCE_ID_VERSION,
        "server_key": original.server_key,
        "lineage_result_id": original.result_id,
        "candidate_period_seconds": original.candidate_period_seconds,
        "expected_phase_offset": round(original.expected_phase_offset, 6),
        "reconciliation_outcome": ExpectedWindowOutcome.RETRACTED_MISS.value,
        "classification_outcome": desired.outcome.value,
        "reconciliation_reason": reason,
        "reconciled_at": round(reconciled_at, 6),
        "qualifying_finalized_event_id": desired.qualifying_finalized_event_id,
        "overlapping_outage_episode_ids": sorted(
            set(desired.overlapping_outage_episode_ids)
        ),
    }


def _migration_reconciliation_evidence_revision_id(
    original: ExpectedWindowResult,
    desired: ExpectedWindowResult,
    *,
    reconciled_at: float,
) -> str:
    return _stable_id(
        "schema3-expected-window-reconciliation-evidence",
        SCHEMA4_RECONCILIATION_EVIDENCE_ID_VERSION,
        _migration_reconciliation_evidence_payload(
            original,
            desired,
            reconciled_at=reconciled_at,
        ),
    )


def _migration_reconciliation_timestamp(
    desired: ExpectedWindowResult,
    *,
    events: Sequence[PhysicalRestartEvent],
    spans: Sequence[ContinuitySpan],
    episodes: Sequence[ExpectedWindowEpisodeEvidence],
) -> float:
    """Return the latest immutable timestamp needed to prove the revision.

    Finalized events become proof at finalization.  Persisted outage spans and
    episode evidence become proof at their first overlap with the expected
    window.  The latest such proof point is used; the deterministic window end
    is the conservative fallback when no evidence object can be resolved.
    """

    event_by_id = {item.event_id: item for item in events}
    span_by_id = {item.reference_id: item for item in spans}
    episode_by_id = {item.evidence_id: item for item in episodes}
    evidence_ids = set(desired.overlapping_outage_episode_ids)
    if desired.qualifying_finalized_event_id is not None:
        evidence_ids.add(desired.qualifying_finalized_event_id)
    proof_times: list[float] = []
    for evidence_id in sorted(evidence_ids):
        event = event_by_id.get(evidence_id)
        if event is not None:
            proof_times.append(float(event.finalized_at))
            continue
        span = span_by_id.get(evidence_id)
        if span is not None:
            proof_times.append(
                max(float(span.start_at), float(desired.window_start_at))
            )
            continue
        episode = episode_by_id.get(evidence_id)
        if episode is not None:
            proof_times.append(
                max(float(episode.start_at), float(desired.window_start_at))
            )
    return max(proof_times, default=float(desired.window_end_at))


def validate_schema4_state(state: object) -> Schema4ValidationReport:
    return deserialize_schema4_state(
        state, quarantine_invalid_servers=False
    ).report


def compact_schema4_state(state: object) -> dict:
    """Drop reconstructable raw detail while preserving immutable authority."""

    loaded = deserialize_schema4_state(state, quarantine_invalid_servers=False)
    if not _report_acceptable_for_persistence(loaded.report):
        raise Schema4ValidationError("cannot compact invalid schema-4 state")
    compacted = copy.deepcopy(loaded.state)
    snapshot = compacted.get("schema3_snapshot")
    if isinstance(snapshot, dict):
        for raw in (snapshot.get("servers") or {}).values():
            if isinstance(raw, dict):
                raw["coverage_segments"] = []
                raw["monitoring_sessions"] = []
                for event in raw.get("events", ()):
                    if isinstance(event, dict):
                        event["samples"] = []
    for record in compacted["servers"].values():
        legacy = record.get("legacy_schema3_record")
        if isinstance(legacy, dict):
            legacy["coverage_segments"] = []
            legacy["monitoring_sessions"] = []
            for event in legacy.get("events", ()):
                if isinstance(event, dict):
                    event["samples"] = []
        for event in record.get("physical_events", ()):
            if isinstance(event, dict):
                event["samples"] = []
        reasons = set(record.get("reason_codes") or ())
        reasons.add("raw_samples_and_legacy_coverage_compacted")
        record["reason_codes"] = sorted(reasons)
        if isinstance(record.get("consumer_state"), Mapping):
            regimes = {
                str(item.get("regime_id"))
                for item in record.get("regimes", ())
                if isinstance(item, Mapping)
                and item.get("status") == RegimeRecordStatus.SUPERSEDED.value
            }
            consumer = consumer_ledger_from_mapping(
                record["consumer_state"], server_key=str(record.get("server_key"))
            )
            record["consumer_state"] = consumer_ledger_to_mapping(
                compact_consumer_ledger(
                    consumer,
                    now=float(compacted.get("updated_at", 0) or 0),
                    superseded_regime_ids=regimes,
                )
            )
    report = validate_schema4_state(compacted)
    if not _report_acceptable_for_persistence(report):
        raise Schema4ValidationError("compaction broke immutable references")
    return compacted


def rebuild_schema4_server_record(
    existing_record: Mapping[str, object],
    schema3_record: Mapping[str, object],
    *,
    server_key: str,
    updated_at: float,
) -> dict:
    """Rebuild one authoritative server from live schema-3 runtime evidence.

    The schema-3 mapping is an in-memory detector/scorer projection, not a
    persistence authority.  Existing immutable schema-4 evidence is unioned
    back into the rebuilt record so compaction, process restart, and a bounded
    schema-3 recent horizon cannot erase durable relationships or lineages.
    """

    if existing_record.get("authority_status") == "quarantined":
        raise Schema4ValidationError(
            f"cannot update quarantined schema-4 server {server_key}"
        )
    synthetic = {
        "schema_version": 3,
        "scoring_algorithm_version": 1,
        "created_at": 0,
        "updated_at": max(0.0, float(updated_at)),
        "servers": {server_key: copy.deepcopy(dict(schema3_record))},
    }
    source_bytes = canonical_json_bytes(synthetic)
    fresh, _audit = _migrate_server(
        server_key,
        synthetic["servers"][server_key],
        state=synthetic,
        source_bytes=source_bytes,
    )

    def union(
        name: str,
        identity: str,
        *,
        prefer_new: bool = True,
        immutable: bool = False,
    ) -> list[dict]:
        values: dict[str, dict] = {}
        for item in existing_record.get(name, ()):
            if isinstance(item, Mapping) and isinstance(item.get(identity), str):
                values[str(item[identity])] = copy.deepcopy(dict(item))
        for item in fresh.get(name, ()):
            if not isinstance(item, Mapping) or not isinstance(item.get(identity), str):
                continue
            key = str(item[identity])
            incoming = copy.deepcopy(dict(item))
            if immutable and key in values:
                # The identity is the immutable object boundary.  A replay may
                # see additional compactable coverage around an already
                # finalized interval, but it must not mutate the durable object
                # retroactively.  Only a new deterministic ID may add evidence.
                continue
            if prefer_new or key not in values:
                values[key] = incoming
        return [values[key] for key in sorted(values)]

    # A compacted old event may have no samples while a replayed event does.
    physical = union("physical_events", "event_id")
    old_events = {
        str(item.get("event_id")): item
        for item in existing_record.get("physical_events", ())
        if isinstance(item, Mapping)
    }
    for index, item in enumerate(physical):
        old = old_events.get(str(item.get("event_id")))
        if old and len(old.get("samples", ())) > len(item.get("samples", ())):
            physical[index] = copy.deepcopy(dict(old))
    fresh["physical_events"] = physical
    fresh["continuity_spans"] = union("continuity_spans", "reference_id")
    fresh["continuity_chains"] = union("continuity_chains", "chain_id")
    fresh["interval_relationships"] = union(
        "interval_relationships",
        "relationship_id",
        prefer_new=False,
        immutable=True,
    )
    fresh["cadence_streaks"] = union(
        "cadence_streaks", "streak_id", prefer_new=False, immutable=True
    )
    fresh["expected_window_episode_evidence"] = union(
        "expected_window_episode_evidence", "evidence_id"
    )

    # Preserve append-only window lineages.  If detached rebuilding proposes a
    # second child for a lineage that already has a durable revision, the
    # durable child wins and the replay proposal is discarded.
    windows: dict[str, dict] = {}
    lineage_children: dict[str, str] = {}
    for item in existing_record.get("expected_window_results", ()):
        if not isinstance(item, Mapping) or not isinstance(item.get("result_id"), str):
            continue
        value = copy.deepcopy(dict(item))
        windows[value["result_id"]] = value
        parent = value.get("supersedes_result_id")
        if isinstance(parent, str):
            lineage_children[parent] = value["result_id"]
    for item in fresh.get("expected_window_results", ()):
        if not isinstance(item, Mapping) or not isinstance(item.get("result_id"), str):
            continue
        value = copy.deepcopy(dict(item))
        parent = value.get("supersedes_result_id")
        if (
            isinstance(parent, str)
            and parent in lineage_children
            and lineage_children[parent] != value["result_id"]
        ):
            continue
        windows.setdefault(value["result_id"], value)
        if isinstance(parent, str):
            lineage_children[parent] = value["result_id"]
    fresh["expected_window_results"] = sorted(
        windows.values(), key=lambda item: (item.get("expected_at", 0), item["result_id"])
    )

    from .companion_restart_phase2_runtime import _deserialize_server

    runtime = _deserialize_server(server_key, schema3_record, DetectionConfig())
    score = RestartScheduleScorer().score(
        runtime.events,
        CoverageTimeline(tuple(runtime.coverage)),
        now=max(0.0, float(updated_at)),
        incumbent_period_seconds=runtime.incumbent_period_seconds,
        aggregate=runtime.aggregate,
        expected_misses=runtime.expected_misses,
        regime_boundaries=(item.ended_at for item in runtime.prior_regimes),
        phase_recency_policy=PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    relationships = persisted_interval_relationships(fresh)
    streaks = persisted_cadence_streaks(fresh)
    windows_ledger = persisted_expected_window_ledger(fresh)
    events = persisted_physical_events(fresh)
    try:
        prior = persisted_authority_decision(existing_record)
    except (KeyError, TypeError, ValueError, Schema4Error):
        prior = None
    decision = evaluate_authority(
        server_key=server_key,
        normal_score=score,
        relationships=relationships,
        streaks=streaks,
        expected_window_ledger=windows_ledger,
        events=events,
        prior_decision=prior,
    )

    fresh["normal_evidence_ledger"] = _ledger_wrapper(
        "normal", decision.normal_ledger, decision.decision_id
    )
    fresh["high_evidence_ledger"] = _ledger_wrapper(
        "high", decision.high_ledger, decision.decision_id
    )
    fresh["authority_candidates"] = [
        _primitive(item)
        for item in sorted(decision.candidates, key=lambda item: item.candidate_id)
    ]
    fresh["challenger_contexts"] = [
        _primitive(item)
        for item in sorted(decision.challenger_contexts, key=lambda item: item.context_id)
    ]
    regimes = {
        str(item.get("regime_id")): copy.deepcopy(dict(item))
        for item in existing_record.get("regimes", ())
        if isinstance(item, Mapping) and isinstance(item.get("regime_id"), str)
    }
    regimes.update({item.regime_id: _primitive(item) for item in decision.regimes})
    fresh["regimes"] = [regimes[key] for key in sorted(regimes)]
    history = {
        str(item.get("decision_id")): copy.deepcopy(dict(item))
        for item in existing_record.get("authority_decisions", ())
        if isinstance(item, Mapping) and isinstance(item.get("decision_id"), str)
    }
    history[decision.decision_id] = _decision_payload(decision)
    fresh["authority_decisions"] = _retain_authority_decisions(
        history.values(),
        current_id=decision.decision_id,
        prior_id=None if prior is None else prior.decision_id,
    )
    fresh["prior_authority_decision_id"] = (
        None if prior is None else prior.decision_id
    )
    fresh["authority_decision"] = _decision_payload(decision)
    fresh["consumer_state"] = copy.deepcopy(
        existing_record.get("consumer_state", empty_consumer_state(server_key))
    )
    fresh["authority_status"] = "valid_authoritative_runtime"
    fresh["legacy_schema3_record"] = copy.deepcopy(dict(schema3_record))
    fresh["reason_codes"] = sorted(
        set(existing_record.get("reason_codes", ()))
        | set(fresh.get("reason_codes", ()))
        | {"authoritative_schema4_runtime_rebuilt"}
    )
    audit = copy.deepcopy(fresh.get("migration_audit", {}))
    audit.update({
        "reference_validation_result": "valid",
        "runtime_updated_at": max(0.0, float(updated_at)),
        "runtime_relationship_count": len(relationships),
        "runtime_streak_count": len(streaks),
        "runtime_expected_window_count": len(windows_ledger),
        "runtime_decision_id": decision.decision_id,
    })
    fresh["migration_audit"] = audit
    issues = _validate_server_record(server_key, fresh)
    if issues:
        raise Schema4ValidationError(
            "; ".join(f"{item.code}: {item.detail}" for item in issues)
        )
    return fresh


def _retain_authority_decisions(
    decisions: Iterable[Mapping[str, object]],
    *,
    current_id: str,
    prior_id: str | None = None,
    limit: int = 64,
) -> list[dict]:
    """Deterministically retain current/prior and transition-significant history."""

    by_id: dict[str, dict] = {}
    for item in decisions:
        if not isinstance(item.get("decision_id"), str):
            continue
        identity = str(item["decision_id"])
        if identity not in by_id:
            by_id[identity] = copy.deepcopy(dict(item))
    ordered = list(by_id.values())
    significant_states = {
        "transition_confirmed",
        "new_regime_provisional",
        "new_regime_established",
        "change_suspected",
        "challenger_accumulating",
    }
    mandatory_ids = {
        str(item["decision_id"])
        for item in ordered
        if item.get("state") in significant_states
    }
    mandatory_ids.add(current_id)
    if prior_id is not None:
        mandatory_ids.add(prior_id)
    # Retain the newest transition-significant decisions when even transition
    # history exceeds the bounded policy, while never dropping current/prior.
    protected = {current_id}
    if prior_id is not None:
        protected.add(prior_id)
    significant_order = [
        str(item["decision_id"])
        for item in ordered
        if str(item["decision_id"]) in mandatory_ids
        and str(item["decision_id"]) not in protected
    ]
    room = max(0, int(limit) - len(protected))
    keep_ids = protected | set(significant_order[-room:])
    for item in reversed(ordered):
        if len(keep_ids) >= int(limit):
            break
        keep_ids.add(str(item["decision_id"]))
    return [item for item in ordered if str(item["decision_id"]) in keep_ids]


def apply_prepared_schema4(
    *,
    active_path: str | os.PathLike[str],
    prepared_path: str | os.PathLike[str],
    verified_backup_path: str | os.PathLike[str],
    expected_source_sha256: str,
    approval_token: str,
) -> None:
    """Install only with an explicit token and an exact whole-file backup."""

    if approval_token != SCHEMA4_APPLY_APPROVAL_TOKEN:
        raise Schema4Error("explicit schema-4 apply approval token is required")
    from .companion_restart_phase2_schema4_runtime import Schema4WriterLock

    active = Path(active_path)
    with Schema4WriterLock(active):
        prepared = Path(prepared_path)
        backup = Path(verified_backup_path)
        source_bytes = active.read_bytes()
        if _sha256(source_bytes) != expected_source_sha256:
            raise Schema4Error("active source hash changed since preparation")
        if backup.read_bytes() != source_bytes:
            raise Schema4Error("verified backup is not byte-identical to active source")
        prepared_bytes = prepared.read_bytes()
        load = deserialize_schema4_bytes(prepared_bytes, quarantine_invalid_servers=False)
        if not load.report.valid:
            raise Schema4ValidationError("prepared schema-4 file is invalid")
        _require_current_apply_versions(load.state)
        atomic_write_bytes(active, prepared_bytes)
        if active.read_bytes() != prepared_bytes:
            raise Schema4Error("schema-4 apply verification failed")


def rollback_whole_file(
    *,
    active_path: str | os.PathLike[str],
    verified_backup_path: str | os.PathLike[str],
    expected_backup_sha256: str,
) -> None:
    from .companion_restart_phase2_schema4_runtime import Schema4WriterLock

    with Schema4WriterLock(active_path):
        backup_bytes = Path(verified_backup_path).read_bytes()
        if _sha256(backup_bytes) != expected_backup_sha256:
            raise Schema4Error("rollback backup hash does not match approval")
        if schema_version_from_bytes(backup_bytes) != 3:
            raise Schema4VersionError("rollback backup is not schema 3")
        atomic_write_bytes(active_path, backup_bytes)
        if Path(active_path).read_bytes() != backup_bytes:
            raise Schema4Error("whole-file rollback verification failed")


def persisted_authority_decision(record: Mapping[str, object]) -> AuthorityDecision:
    value = record.get("authority_decision")
    if not isinstance(value, Mapping):
        raise Schema4ValidationError("authority_decision is missing")
    return _decision_from_payload(value, record)


def compare_persisted_authority(
    *,
    record: Mapping[str, object] | None,
    in_memory_decision: AuthorityDecision,
    in_memory_relationship_count: int,
    in_memory_streak_count: int,
    in_memory_expected_window_count: int,
) -> Schema4ShadowComparison:
    if record is None or record.get("authority_status") == "quarantined":
        warnings = ("persisted_schema4_server_unavailable",)
        return Schema4ShadowComparison(
            server_key=in_memory_decision.server_key,
            in_memory_decision_id=in_memory_decision.decision_id,
            persisted_decision_id=None,
            decision_id_equal=False,
            in_memory_relationship_count=in_memory_relationship_count,
            persisted_relationship_count=0,
            in_memory_streak_count=in_memory_streak_count,
            persisted_streak_count=0,
            in_memory_expected_window_count=in_memory_expected_window_count,
            persisted_expected_window_count=0,
            object_counts_equal=False,
            migration_warnings=warnings,
            reason_codes=("schema4_shadow_comparison_only", *warnings),
        )
    persisted = persisted_authority_decision(record)
    persisted_relationships = len(record.get("interval_relationships", ()))
    persisted_streaks = len(record.get("cadence_streaks", ()))
    persisted_windows = len(record.get("expected_window_results", ()))
    counts_equal = (
        in_memory_relationship_count == persisted_relationships
        and in_memory_streak_count == persisted_streaks
        and in_memory_expected_window_count == persisted_windows
    )
    warnings = tuple(
        record.get("migration_audit", {}).get("migration_warnings", ())
        if isinstance(record.get("migration_audit"), dict)
        else ()
    )
    equal = in_memory_decision.decision_id == persisted.decision_id
    reasons = {"schema4_shadow_comparison_only"}
    reasons.add("decision_ids_equal" if equal else "decision_ids_differ")
    reasons.add("object_counts_equal" if counts_equal else "object_counts_differ")
    return Schema4ShadowComparison(
        server_key=in_memory_decision.server_key,
        in_memory_decision_id=in_memory_decision.decision_id,
        persisted_decision_id=persisted.decision_id,
        decision_id_equal=equal,
        in_memory_relationship_count=in_memory_relationship_count,
        persisted_relationship_count=persisted_relationships,
        in_memory_streak_count=in_memory_streak_count,
        persisted_streak_count=persisted_streaks,
        in_memory_expected_window_count=in_memory_expected_window_count,
        persisted_expected_window_count=persisted_windows,
        object_counts_equal=counts_equal,
        migration_warnings=warnings,
        reason_codes=tuple(sorted(reasons)),
    )


def persisted_physical_events(
    record: Mapping[str, object],
) -> tuple[PhysicalRestartEvent, ...]:
    from .companion_restart_phase2_runtime import _deserialize_event

    return tuple(
        _deserialize_event(item) for item in record.get("physical_events", ())
    )


def normalize_restart_start_phases(
    state: Mapping[str, object],
    *,
    updated_at: float,
) -> tuple[dict, dict[str, dict[str, object]]]:
    """Explicitly rebuild schema-4 state with restart-start canonical phases.

    Retained physical events are the source of truth.  Existing relationship,
    scoring, window, regime, and authority projections are intentionally not
    unioned back: `_migrate_server` regenerates them through the normal detached
    learner paths.  Alert suppression state is operational history rather than
    phase-learning evidence and is preserved unchanged.
    """

    loaded = deserialize_schema4_state(
        state, quarantine_invalid_servers=False
    )
    if not loaded.report.valid:
        raise Schema4ValidationError(
            "restart-start normalization requires valid schema-4 state"
        )
    from .companion_restart_phase2_runtime import _serialize_event

    normalized = copy.deepcopy(loaded.state)
    summaries: dict[str, dict[str, object]] = {}
    schema3_snapshot = normalized.get("schema3_snapshot")
    snapshot_servers = (
        schema3_snapshot.get("servers")
        if isinstance(schema3_snapshot, dict)
        and isinstance(schema3_snapshot.get("servers"), dict)
        else None
    )
    for server_key, existing in tuple(normalized.get("servers", {}).items()):
        if not isinstance(existing, Mapping):
            continue
        legacy = existing.get("legacy_schema3_record")
        if not isinstance(legacy, Mapping):
            raise Schema4ValidationError(
                f"schema-4 server {server_key!r} has no rebuildable schema-3 projection"
            )
        source_events = persisted_physical_events(existing)
        migrated_events = tuple(
            normalize_event_restart_start_phase(item) for item in source_events
        )
        changed = tuple(
            item.event_id
            for item, migrated in zip(source_events, migrated_events)
            if (
                item.canonical_phase_at != migrated.canonical_phase_at
                or item.phase_uncertainty != migrated.phase_uncertainty
            )
        )
        neutralized = tuple(
            item.event_id
            for item in migrated_events
            if item.canonical_phase_at is None
            and "restart_start_phase_unavailable" in item.reason_codes
        )
        raw = copy.deepcopy(dict(legacy))
        raw["events"] = [
            _serialize_event(item, include_samples=True)
            for item in migrated_events
        ]
        raw["event_seq"] = max(
            (item.sequence for item in migrated_events), default=0
        )
        raw["folded_through_event_seq"] = 0
        raw["aggregate"] = {}
        raw["prior_regimes"] = []
        raw["expected_window_misses"] = []
        raw.pop("candidate_diagnostics", None)
        source = {
            "schema_version": 3,
            "scoring_algorithm_version": 1,
            "created_at": normalized.get("created_at", 0),
            "updated_at": float(updated_at),
            "servers": {server_key: raw},
        }
        fresh, audit = _migrate_server(
            server_key,
            raw,
            state=source,
            source_bytes=canonical_json_bytes(source),
        )
        fresh["authority_status"] = str(
            existing.get("authority_status") or "valid_authoritative_runtime"
        )
        authority_decision = persisted_authority_decision(fresh)
        consumer_decision = evaluate_authority_consumers(
            AuthorityConsumerPolicyInput(
                authority_decision=authority_decision,
                now=float(updated_at),
                server_online_healthy=False,
                restart_alert_enabled=False,
            )
        )
        rebuilt_consumer = consumer_ledger_from_mapping(
            empty_consumer_state(server_key), server_key=server_key
        )
        rebuilt_consumer = with_decision(rebuilt_consumer, consumer_decision)
        existing_consumer = existing.get("consumer_state")
        if isinstance(existing_consumer, Mapping):
            old_consumer = consumer_ledger_from_mapping(
                existing_consumer, server_key=server_key
            )
            physical_ids = {item.event_id for item in migrated_events}
            for item in latest_alert_records(old_consumer):
                # Recovery suppression is tied to an immutable physical event
                # and remains valid.  Scheduled-warning keys are tied to the
                # replaced recovery phase/regime and must be regenerated from
                # the next restart-start consumer decision.
                if (
                    item.namespace is not AuthoritySuppressionKind.GENERIC_RECOVERY
                    or item.physical_event_id not in physical_ids
                ):
                    continue
                rebuilt_consumer = append_alert(
                    rebuilt_consumer,
                    alert_record(
                        key=item.suppression_key(),
                        authority_consumer_decision_id=consumer_decision.decision_id,
                        created_at=item.created_at,
                        lifecycle=item.lifecycle,
                        action_outcome=item.action_outcome,
                        compacted=item.compacted,
                        reason_codes=tuple(
                            sorted(
                                set(item.reason_codes)
                                | {"preserved_across_restart_start_phase_normalization"}
                            )
                        ),
                    ),
                )
        fresh["consumer_state"] = consumer_ledger_to_mapping(rebuilt_consumer)
        fresh["reason_codes"] = sorted(
            set(fresh.get("reason_codes", ()))
            | {"restart_start_phase_normalized"}
        )
        fresh_audit = copy.deepcopy(fresh.get("migration_audit", {}))
        fresh_audit.update(
            {
                "restart_start_phase_normalized_at": float(updated_at),
                "restart_start_events_changed": len(changed),
                "restart_start_events_neutralized": len(neutralized),
            }
        )
        fresh["migration_audit"] = fresh_audit
        normalized["servers"][server_key] = fresh
        if snapshot_servers is not None:
            snapshot_servers[server_key] = copy.deepcopy(raw)
        summaries[server_key] = {
            "physical_event_count": len(migrated_events),
            "changed_event_ids": changed,
            "neutralized_event_ids": neutralized,
            "relationship_count": len(fresh.get("interval_relationships", ())),
            "selected_shadow_period_seconds": audit.get(
                "selected_shadow_period_seconds"
            ),
        }
    normalized["updated_at"] = max(
        float(updated_at), float(normalized.get("created_at", 0) or 0)
    )
    if isinstance(schema3_snapshot, dict):
        schema3_snapshot["updated_at"] = normalized["updated_at"]
    normalized["reason_codes"] = sorted(
        set(normalized.get("reason_codes", ()))
        | {"restart_start_phase_normalization_completed"}
    )
    final = deserialize_schema4_state(
        normalized, quarantine_invalid_servers=False
    )
    if not final.report.valid:
        detail = "; ".join(
            f"{item.server_key or 'root'}:{item.code}:{item.detail}"
            for item in final.report.issues
        )
        raise Schema4ValidationError(
            "restart-start normalized state failed schema-4 validation"
            + (f": {detail}" if detail else "")
        )
    return final.state, summaries


def persisted_continuity_chains(
    record: Mapping[str, object],
) -> tuple[ContinuityChain, ...]:
    return tuple(
        ContinuityChain(
            schema_version=int(item["schema_version"]),
            provenance_version=int(item["provenance_version"]),
            chain_id=str(item["chain_id"]),
            server_key=str(item["server_key"]),
            app_session_id=str(item["app_session_id"]),
            monitoring_session_id=str(item["monitoring_session_id"]),
            poll_generation=int(item["poll_generation"]),
            start_at=float(item["start_at"]),
            end_at=float(item["end_at"]),
            termination_reason=str(item["termination_reason"]),
            reference_ids=tuple(item.get("reference_ids", ())),
            observed_span_count=int(item["observed_span_count"]),
            provenance_proven=bool(item["provenance_proven"]),
            query_health_adequate=bool(item["query_health_adequate"]),
            blocker_kinds=tuple(item.get("blocker_kinds", ())),
            lifecycle=ChainLifecycle(item["lifecycle"]),
            reason_codes=tuple(item.get("reason_codes", ())),
        )
        for item in record.get("continuity_chains", ())
    )


def persisted_interval_relationships(
    record: Mapping[str, object],
) -> tuple[IntervalRelationship, ...]:
    return tuple(
        IntervalRelationship(
            schema_version=int(item["schema_version"]),
            provenance_version=int(item["provenance_version"]),
            relationship_id=str(item["relationship_id"]),
            server_key=str(item["server_key"]),
            candidate_period_seconds=int(item["candidate_period_seconds"]),
            left_event_id=str(item["left_event_id"]),
            left_event_sequence=int(item["left_event_sequence"]),
            right_event_id=str(item["right_event_id"]),
            right_event_sequence=int(item["right_event_sequence"]),
            left_at=float(item["left_at"]),
            right_at=float(item["right_at"]),
            observed_interval_seconds=float(item["observed_interval_seconds"]),
            signed_residual_seconds=float(item["signed_residual_seconds"]),
            tolerance_seconds=float(item["tolerance_seconds"]),
            relationship_kind=RelationshipKind(item["relationship_kind"]),
            multiplier=int(item["multiplier"]),
            intervening_event_ids=tuple(item.get("intervening_event_ids", ())),
            coverage_quality=CoverageQuality(item["coverage_quality"]),
            observed_ratio=float(item["observed_ratio"]),
            largest_unexplained_gap=float(item["largest_unexplained_gap"]),
            monitoring_session_id=item.get("monitoring_session_id"),
            continuity_chain_id=item.get("continuity_chain_id"),
            continuity_qualified=bool(item["continuity_qualified"]),
            normal_support_diagnostic=float(item["normal_support_diagnostic"]),
            high_authority_eligible=bool(item["high_authority_eligible"]),
            high_quality_diagnostic=float(item["high_quality_diagnostic"]),
            reason_codes=tuple(item.get("reason_codes", ())),
        )
        for item in record.get("interval_relationships", ())
    )


def persisted_cadence_streaks(
    record: Mapping[str, object],
) -> tuple[CadenceStreak, ...]:
    return tuple(
        CadenceStreak(
            schema_version=int(item["schema_version"]),
            provenance_version=int(item["provenance_version"]),
            streak_id=str(item["streak_id"]),
            candidate_period_seconds=int(item["candidate_period_seconds"]),
            event_ids=tuple(item.get("event_ids", ())),
            relationship_ids=tuple(item.get("relationship_ids", ())),
            interval_count=int(item["interval_count"]),
            event_count=int(item["event_count"]),
            start_at=float(item["start_at"]),
            end_at=float(item["end_at"]),
            server_key=str(item["server_key"]),
            monitoring_session_id=str(item["monitoring_session_id"]),
            continuity_chain_id=str(item["continuity_chain_id"]),
            complete_coverage_start=float(item["complete_coverage_start"]),
            complete_coverage_end=float(item["complete_coverage_end"]),
            minimum_observed_ratio=float(item["minimum_observed_ratio"]),
            maximum_unexplained_gap=float(item["maximum_unexplained_gap"]),
            query_health_quality=float(item["query_health_quality"]),
            phase_estimate=float(item["phase_estimate"]),
            phase_spread=float(item["phase_spread"]),
            lifecycle=StreakLifecycle(item["lifecycle"]),
            reason_codes=tuple(item.get("reason_codes", ())),
        )
        for item in record.get("cadence_streaks", ())
    )


def persisted_expected_window_ledger(
    record: Mapping[str, object],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values: list[ExpectedWindowLedgerRecord] = []
    for item in record.get("expected_window_results", ()):
        cls = (
            ExpectedWindowRevision
            if item.get("record_type") == "ExpectedWindowRevision"
            else ExpectedWindowResult
        )
        values.append(
            cls(
                schema_version=int(item["schema_version"]),
                semantic_version=int(item["semantic_version"]),
                result_id=str(item["result_id"]),
                server_key=str(item["server_key"]),
                candidate_period_seconds=int(item["candidate_period_seconds"]),
                expected_phase_offset=float(item["expected_phase_offset"]),
                window_start_at=float(item["window_start_at"]),
                expected_at=float(item["expected_at"]),
                window_end_at=float(item["window_end_at"]),
                model_revision_id=str(item["model_revision_id"]),
                regime_interpretation_id=item.get("regime_interpretation_id"),
                continuity_chain_id=item.get("continuity_chain_id"),
                coverage_classification=WindowCoverageClassification(item["coverage_classification"]),
                observed_ratio=float(item["observed_ratio"]),
                largest_unexplained_gap=float(item["largest_unexplained_gap"]),
                blocker_kind=item.get("blocker_kind"),
                healthy_throughout=bool(item["healthy_throughout"]),
                outage_observed=bool(item["outage_observed"]),
                overlapping_outage_episode_ids=tuple(item.get("overlapping_outage_episode_ids", ())),
                unresolved_episode=bool(item["unresolved_episode"]),
                qualifying_finalized_event_id=item.get("qualifying_finalized_event_id"),
                outcome=ExpectedWindowOutcome(item["outcome"]),
                negative_penalty_active=bool(item["negative_penalty_active"]),
                reconciliation_reason=(
                    None
                    if item.get("reconciliation_reason") is None
                    else ReconciliationReason(item["reconciliation_reason"])
                ),
                reconciled_at=_optional_nonnegative_number(item.get("reconciled_at")),
                reconciliation_revision_id=item.get("reconciliation_revision_id"),
                reason_codes=tuple(item.get("reason_codes", ())),
                supersedes_result_id=item.get("supersedes_result_id"),
            )
        )
    return tuple(values)


def _validate_root(state: Mapping[str, object], issues: list[Schema4ValidationIssue]) -> bool:
    valid = True
    manifest = state.get("version_manifest")
    if not isinstance(manifest, dict):
        issues.append(Schema4ValidationIssue(None, "missing_version_manifest", "version_manifest must be a mapping"))
        valid = False
    else:
        expected = _version_manifest()
        for key, value in expected.items():
            actual = manifest.get(key)
            supported = (
                SCHEMA4_SUPPORTED_MIGRATION_TOOL_VERSIONS
                if key == "migration_tool_version"
                else {value}
            )
            if actual not in supported:
                issues.append(Schema4ValidationIssue(None, "unsupported_semantic_version", f"{key}={actual!r}, expected one of {sorted(supported)!r}"))
                valid = False
    scoring_version = state.get("scoring_algorithm_version")
    if scoring_version not in SCHEMA4_SUPPORTED_SCORING_ALGORITHM_VERSIONS:
        issues.append(
            Schema4ValidationIssue(
                None,
                "unsupported_scoring_algorithm_version",
                (
                    f"scoring_algorithm_version={scoring_version!r}, expected one of "
                    f"{sorted(SCHEMA4_SUPPORTED_SCORING_ALGORITHM_VERSIONS)!r}"
                ),
            )
        )
        valid = False
    if not isinstance(state.get("migration_audit"), dict):
        issues.append(Schema4ValidationIssue(None, "missing_migration_audit", "root migration_audit is required"))
        valid = False
    return valid


def _report_acceptable_for_persistence(report: Schema4ValidationReport) -> bool:
    return report.root_valid and all(
        item.code == "server_quarantined" for item in report.issues
    )


def _validate_server_record(server_key: str, record: object) -> list[Schema4ValidationIssue]:
    issues: list[Schema4ValidationIssue] = []
    add = lambda code, detail: issues.append(Schema4ValidationIssue(server_key, code, detail))
    if not isinstance(record, dict):
        add("server_not_mapping", "server authority record must be a mapping")
        return issues
    if record.get("authority_status") == "quarantined":
        add("server_quarantined", "server authority record is quarantined")
        return issues
    if record.get("server_key") != server_key:
        add("server_key_mismatch", "record server_key does not match collection key")
    collection_ids: dict[str, set[str]] = {}
    specs = {
        "physical_events": "event_id",
        "continuity_spans": "reference_id",
        "continuity_chains": "chain_id",
        "interval_relationships": "relationship_id",
        "cadence_streaks": "streak_id",
        "expected_window_episode_evidence": "evidence_id",
        "expected_window_results": "result_id",
        "authority_candidates": "candidate_id",
        "challenger_contexts": "context_id",
        "regimes": "regime_id",
        "authority_decisions": "decision_id",
    }
    for name, field in specs.items():
        values = record.get(name)
        if not isinstance(values, list):
            add("missing_collection", f"{name} must be a list")
            collection_ids[name] = set()
            continue
        ids: list[str] = []
        for index, item in enumerate(values):
            if not isinstance(item, dict) or not isinstance(item.get(field), str) or not item.get(field):
                add("invalid_object_id", f"{name}[{index}] has no {field}")
                continue
            ids.append(item[field])
        if len(ids) != len(set(ids)):
            add("duplicate_object_id", f"{name} contains duplicate {field} values")
        collection_ids[name] = set(ids)
    events = collection_ids.get("physical_events", set())
    spans = collection_ids.get("continuity_spans", set())
    chains = collection_ids.get("continuity_chains", set())
    relationships = collection_ids.get("interval_relationships", set())
    streaks = collection_ids.get("cadence_streaks", set())
    windows = collection_ids.get("expected_window_results", set())
    episodes = collection_ids.get("expected_window_episode_evidence", set())
    candidates = collection_ids.get("authority_candidates", set())
    contexts = collection_ids.get("challenger_contexts", set())
    regimes = collection_ids.get("regimes", set())
    decisions = collection_ids.get("authority_decisions", set())

    for item in record.get("continuity_chains", ()):
        for ref in item.get("reference_ids", ()):
            if ref not in spans:
                add("broken_chain_span_reference", f"chain {item.get('chain_id')} references {ref}")
    for item in record.get("interval_relationships", ()):
        for ref in (item.get("left_event_id"), item.get("right_event_id"), *item.get("intervening_event_ids", ())):
            if ref not in events:
                add("broken_relationship_endpoint", f"relationship {item.get('relationship_id')} references event {ref}")
        chain = item.get("continuity_chain_id")
        if item.get("high_authority_eligible") and chain not in chains:
            add("broken_relationship_chain_reference", f"high relationship {item.get('relationship_id')} references chain {chain}")
    for item in record.get("cadence_streaks", ()):
        for ref in item.get("relationship_ids", ()):
            if ref not in relationships:
                add("broken_streak_relationship_reference", f"streak {item.get('streak_id')} references {ref}")
        for ref in item.get("event_ids", ()):
            if ref not in events:
                add("broken_streak_event_reference", f"streak {item.get('streak_id')} references {ref}")
        if item.get("continuity_chain_id") not in chains:
            add("broken_streak_chain_reference", f"streak {item.get('streak_id')} has unknown chain")
    by_window = {item.get("result_id"): item for item in record.get("expected_window_results", ())}
    superseded: set[str] = set()
    for item in by_window.values():
        parent = item.get("supersedes_result_id")
        if parent is not None:
            if parent not in windows:
                add("broken_revision_lineage", f"revision {item.get('result_id')} supersedes {parent}")
            if parent in superseded:
                add("branched_revision_lineage", f"multiple revisions supersede {parent}")
            superseded.add(parent)
        finalized = item.get("qualifying_finalized_event_id")
        if finalized is not None and finalized not in events:
            add("broken_window_event_reference", f"window {item.get('result_id')} references {finalized}")
        evidence_universe = events | spans | episodes
        for ref in item.get("overlapping_outage_episode_ids", ()):
            if ref not in evidence_universe:
                add("broken_window_evidence_reference", f"window {item.get('result_id')} references {ref}")
    _validate_no_lineage_cycle(by_window, add)

    normal = record.get("normal_evidence_ledger")
    high = record.get("high_evidence_ledger")
    if not isinstance(normal, dict) or not isinstance(normal.get("ledger_id"), str):
        add("invalid_normal_ledger", "normal evidence ledger is missing identity")
    if not isinstance(high, dict) or not isinstance(high.get("ledger_id"), str):
        add("invalid_high_ledger", "high evidence ledger is missing identity")
    if isinstance(high, dict):
        value = high.get("value") if isinstance(high.get("value"), dict) else {}
        for ref in value.get("high_relationship_ids", ()):
            if ref not in relationships:
                add("broken_high_ledger_relationship", f"high ledger references {ref}")
        for ref in value.get("maximal_streak_ids", ()):
            if ref not in streaks:
                add("broken_high_ledger_streak", f"high ledger references {ref}")
    for item in record.get("authority_candidates", ()):
        for ref in item.get("high_relationship_ids", ()):
            if ref not in relationships:
                add("broken_candidate_relationship_reference", f"candidate {item.get('candidate_id')} references {ref}")
        for ref in item.get("maximal_streak_ids", ()):
            if ref not in streaks:
                add("broken_candidate_streak_reference", f"candidate {item.get('candidate_id')} references {ref}")
    for item in record.get("challenger_contexts", ()):
        for ref in item.get("normal_relationship_ids", ()):
            if ref not in relationships:
                add("broken_context_relationship_reference", f"context {item.get('context_id')} references {ref}")
        for ref in item.get("high_relationship_ids", ()):
            if ref not in relationships:
                add("broken_context_high_reference", f"context {item.get('context_id')} references {ref}")
        for ref in item.get("streak_ids", ()):
            if ref not in streaks:
                add("broken_context_streak_reference", f"context {item.get('context_id')} references {ref}")
        for ref in item.get("off_phase_event_ids", ()):
            if ref not in events:
                add("broken_context_event_reference", f"context {item.get('context_id')} references {ref}")
    evidence = events | relationships | streaks | windows
    for item in record.get("regimes", ()):
        for ref in item.get("defining_evidence_ids", ()):
            if ref not in evidence:
                add("broken_regime_evidence_reference", f"regime {item.get('regime_id')} references {ref}")
        superseded_by = item.get("superseded_by_id")
        if superseded_by is not None and superseded_by not in regimes:
            add("broken_regime_supersession", f"regime {item.get('regime_id')} references {superseded_by}")
    current = record.get("authority_decision")
    if not isinstance(current, dict) or current.get("decision_id") not in decisions:
        add("broken_current_decision_reference", "current authority decision is absent from history")
    else:
        for ref in current.get("candidate_ids", ()):
            if ref not in candidates:
                add("broken_decision_candidate_reference", f"decision references {ref}")
        for ref in current.get("challenger_context_ids", ()):
            if ref not in contexts:
                add("broken_decision_context_reference", f"decision references {ref}")
        for ref in current.get("regime_ids", ()):
            if ref not in regimes:
                add("broken_decision_regime_reference", f"decision references {ref}")
    prior = record.get("prior_authority_decision_id")
    if prior is not None and prior not in decisions:
        add("broken_prior_decision_reference", f"prior decision {prior} is absent")
    try:
        persisted_authority_decision(record)
    except Exception as exc:
        add("authority_deserialization_failed", str(exc))
    consumer_errors = validate_consumer_state(
        record.get("consumer_state"),
        server_key=server_key,
        regime_ids=regimes,
        physical_event_ids=events,
        authority_decision_ids=decisions,
    )
    for error in consumer_errors:
        add("invalid_consumer_state", error)
    return issues


def _decision_payload(decision: AuthorityDecision) -> dict:
    return {
        "schema_version": decision.schema_version,
        "semantic_version": decision.semantic_version,
        "decision_id": decision.decision_id,
        "server_key": decision.server_key,
        "selected_shadow_regime_id": None if decision.selected_shadow_regime is None else decision.selected_shadow_regime.regime_id,
        "incumbent_shadow_regime_id": None if decision.incumbent_shadow_regime is None else decision.incumbent_shadow_regime.regime_id,
        "strongest_challenger_id": None if decision.strongest_challenger is None else decision.strongest_challenger.context_id,
        "regime_ids": [item.regime_id for item in decision.regimes],
        "candidate_ids": [item.candidate_id for item in decision.candidates],
        "challenger_context_ids": [item.context_id for item in decision.challenger_contexts],
        "normal_ledger_id": _ledger_id("normal", decision.normal_ledger, decision.decision_id),
        "high_ledger_id": _ledger_id("high", decision.high_ledger, decision.decision_id),
        "state": decision.state.value,
        "cycle_visible": decision.cycle_visible,
        "prediction_usable": decision.prediction_usable,
        "countdown_safe": decision.countdown_safe,
        "suspicion_level": decision.suspicion_level,
        "active_suspicion_evidence_ids": list(decision.active_suspicion_evidence_ids),
        "normal_confidence": decision.normal_confidence,
        "high_confidence": decision.high_confidence,
        "combined_confidence": decision.combined_confidence,
        "reason_codes": list(decision.reason_codes),
    }


def _decision_from_payload(
    payload: Mapping[str, object], record: Mapping[str, object]
) -> AuthorityDecision:
    candidate_map = {
        item.get("candidate_id"): _authority_candidate_from(item)
        for item in record.get("authority_candidates", ())
    }
    context_map = {
        item.get("context_id"): _challenger_from(item)
        for item in record.get("challenger_contexts", ())
    }
    regime_map = {
        item.get("regime_id"): _regime_from(item)
        for item in record.get("regimes", ())
    }
    candidates = tuple(
        candidate_map[item]
        for item in payload.get("candidate_ids", ())
        if item in candidate_map
    )
    contexts = tuple(
        context_map[item]
        for item in payload.get("challenger_context_ids", ())
        if item in context_map
    )
    regimes = tuple(
        regime_map[item]
        for item in payload.get("regime_ids", ())
        if item in regime_map
    )
    by_regime = {item.regime_id: item for item in regimes}
    by_context = {item.context_id: item for item in contexts}
    normal = _normal_ledger_from(record["normal_evidence_ledger"]["value"])
    high = _high_ledger_from(record["high_evidence_ledger"]["value"], candidates)
    return AuthorityDecision(
        schema_version=int(payload["schema_version"]),
        semantic_version=int(payload["semantic_version"]),
        decision_id=str(payload["decision_id"]),
        server_key=str(payload["server_key"]),
        selected_shadow_regime=by_regime.get(payload.get("selected_shadow_regime_id")),
        incumbent_shadow_regime=by_regime.get(payload.get("incumbent_shadow_regime_id")),
        strongest_challenger=by_context.get(payload.get("strongest_challenger_id")),
        regimes=regimes,
        candidates=candidates,
        challenger_contexts=contexts,
        normal_ledger=normal,
        high_ledger=high,
        state=RegimeState(payload["state"]),
        cycle_visible=bool(payload["cycle_visible"]),
        prediction_usable=bool(payload["prediction_usable"]),
        countdown_safe=bool(payload["countdown_safe"]),
        suspicion_level=int(payload["suspicion_level"]),
        active_suspicion_evidence_ids=tuple(payload.get("active_suspicion_evidence_ids", ())),
        normal_confidence=float(payload["normal_confidence"]),
        high_confidence=float(payload["high_confidence"]),
        combined_confidence=float(payload["combined_confidence"]),
        reason_codes=tuple(payload.get("reason_codes", ())),
    )


def _normal_ledger_from(value: Mapping[str, object]) -> NormalEvidenceLedger:
    return NormalEvidenceLedger(
        schema_version=int(value["schema_version"]),
        semantic_version=int(value["semantic_version"]),
        server_key=str(value["server_key"]),
        candidates=tuple(
            NormalCandidateEvidence(
                candidate_period_seconds=int(item["candidate_period_seconds"]),
                phase_offset=float(item["phase_offset"]),
                fundamental_confidence=float(item["fundamental_confidence"]),
                phase_confidence=float(item["phase_confidence"]),
                aligned_normal_confidence=float(item["aligned_normal_confidence"]),
                aligned_relationship_ids=tuple(item.get("aligned_relationship_ids", ())),
                compatible_multiple_relationship_ids=tuple(item.get("compatible_multiple_relationship_ids", ())),
                off_phase_event_ids=tuple(item.get("off_phase_event_ids", ())),
                active_genuine_miss_ids=tuple(item.get("active_genuine_miss_ids", ())),
                compatible_hit_ids=tuple(item.get("compatible_hit_ids", ())),
                neutral_window_ids=tuple(item.get("neutral_window_ids", ())),
                reason_codes=tuple(item.get("reason_codes", ())),
            )
            for item in value.get("candidates", ())
        ),
        active_genuine_miss_ids=tuple(value.get("active_genuine_miss_ids", ())),
        neutral_window_ids=tuple(value.get("neutral_window_ids", ())),
        reason_codes=tuple(value.get("reason_codes", ())),
    )


def _high_ledger_from(
    value: Mapping[str, object], candidates: tuple[AuthorityCandidate, ...]
) -> HighEvidenceLedger:
    ids = {item.get("candidate_id") for item in value.get("candidates", ())}
    return HighEvidenceLedger(
        schema_version=int(value["schema_version"]),
        semantic_version=int(value["semantic_version"]),
        server_key=str(value["server_key"]),
        high_relationship_ids=tuple(value.get("high_relationship_ids", ())),
        maximal_streak_ids=tuple(value.get("maximal_streak_ids", ())),
        candidates=tuple(item for item in candidates if item.candidate_id in ids),
        reason_codes=tuple(value.get("reason_codes", ())),
    )


def _authority_candidate_from(value: Mapping[str, object]) -> AuthorityCandidate:
    return AuthorityCandidate(
        schema_version=int(value["schema_version"]), semantic_version=int(value["semantic_version"]),
        candidate_id=str(value["candidate_id"]), regime_id=str(value["regime_id"]), server_key=str(value["server_key"]),
        candidate_period_seconds=int(value["candidate_period_seconds"]), phase_offset=float(value["phase_offset"]),
        phase_spread=float(value["phase_spread"]), phase_tolerance=float(value["phase_tolerance"]),
        high_relationship_ids=tuple(value.get("high_relationship_ids", ())), maximal_streak_ids=tuple(value.get("maximal_streak_ids", ())),
        relationship_quality_values=tuple((str(a), float(b)) for a, b in value.get("relationship_quality_values", ())),
        base_authority=float(value["base_authority"]), streak_bonus=float(value["streak_bonus"]),
        streak_bonus_values=tuple((str(a), float(b)) for a, b in value.get("streak_bonus_values", ())),
        high_authority=float(value["high_authority"]), phase_quality=float(value["phase_quality"]),
        high_phase_authority=float(value["high_phase_authority"]), aligned_normal_confidence=float(value["aligned_normal_confidence"]),
        combined_confidence=float(value["combined_confidence"]), h1_gate=bool(value["h1_gate"]), h2_gate=bool(value["h2_gate"]),
        h3_gate=bool(value["h3_gate"]), h4_gate=bool(value["h4_gate"]), harmonic_blockers=tuple(value.get("harmonic_blockers", ())),
        defining_evidence_ids=tuple(value.get("defining_evidence_ids", ())), first_evidence_at=float(value["first_evidence_at"]),
        last_evidence_at=float(value["last_evidence_at"]), reason_codes=tuple(value.get("reason_codes", ())),
    )


def _challenger_from(value: Mapping[str, object]) -> ChallengerContext:
    return ChallengerContext(
        schema_version=int(value["schema_version"]), semantic_version=int(value["semantic_version"]), context_id=str(value["context_id"]),
        server_key=str(value["server_key"]), incumbent_regime_id=str(value["incumbent_regime_id"]), proposed_regime_id=str(value["proposed_regime_id"]),
        candidate_period_seconds=int(value["candidate_period_seconds"]), phase_offset=float(value["phase_offset"]), phase_tolerance=float(value["phase_tolerance"]),
        normal_relationship_ids=tuple(value.get("normal_relationship_ids", ())), off_phase_event_ids=tuple(value.get("off_phase_event_ids", ())),
        active_genuine_miss_ids=tuple(value.get("active_genuine_miss_ids", ())), high_relationship_ids=tuple(value.get("high_relationship_ids", ())),
        streak_ids=tuple(value.get("streak_ids", ())), normal_context_strength=float(value["normal_context_strength"]), high_authority=float(value["high_authority"]),
        high_confirmation_present=bool(value["high_confirmation_present"]), authorization_state=ChallengerAuthorization(value["authorization_state"]),
        regime_change_status=ChallengerStatus(value["regime_change_status"]), first_evidence_at=float(value["first_evidence_at"]),
        last_evidence_at=float(value["last_evidence_at"]), reason_codes=tuple(value.get("reason_codes", ())),
    )


def _regime_from(value: Mapping[str, object]) -> RegimeRecord:
    return RegimeRecord(
        schema_version=int(value["schema_version"]), semantic_version=int(value["semantic_version"]), regime_id=str(value["regime_id"]),
        server_key=str(value["server_key"]), candidate_period_seconds=int(value["candidate_period_seconds"]), phase_offset=float(value["phase_offset"]),
        phase_tolerance=float(value["phase_tolerance"]), origin=AuthorityOrigin(value["origin"]), authority_gate=AuthorityGate(value["authority_gate"]),
        high_authority=float(value["high_authority"]), phase_authority=float(value["phase_authority"]), combined_confidence=float(value["combined_confidence"]),
        status=RegimeRecordStatus(value["status"]), defining_evidence_ids=tuple(value.get("defining_evidence_ids", ())),
        established_at=_optional_nonnegative_number(value.get("established_at")), superseded_at=_optional_nonnegative_number(value.get("superseded_at")),
        superseded_by_id=value.get("superseded_by_id"), reason_codes=tuple(value.get("reason_codes", ())),
    )


def _ledger_wrapper(kind: str, ledger: object, decision_id: str) -> dict:
    return {
        "ledger_id": _ledger_id(kind, ledger, decision_id),
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "semantic_version": AUTHORITY_SEMANTIC_VERSION,
        "kind": kind,
        "value": _primitive(ledger),
        "reason_codes": ["immutable_reference_ledger", "scalar_totals_not_authoritative"],
    }


def _ledger_id(kind: str, ledger: object, decision_id: str) -> str:
    return _stable_id("schema4-ledger", kind, decision_id, _primitive(ledger))


def _primitive(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _primitive(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_primitive(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    return value


def _sorted_by_id(values: Iterable[object], field: str) -> tuple[object, ...]:
    return tuple(sorted(values, key=lambda item: str(getattr(item, field))))


def _deduplicate_chains(
    chains: Iterable[ContinuityChain],
) -> tuple[ContinuityChain, ...]:
    """Persist one durable identity while retaining all proven fragments."""

    grouped: dict[str, list[ContinuityChain]] = {}
    for item in chains:
        grouped.setdefault(item.chain_id, []).append(item)
    values: list[ContinuityChain] = []
    for chain_id, fragments in grouped.items():
        first = min(fragments, key=lambda item: (item.start_at, item.end_at))
        if len(fragments) == 1:
            values.append(first)
            continue
        references = tuple(
            sorted({ref for fragment in fragments for ref in fragment.reference_ids})
        )
        values.append(
            dataclasses.replace(
                first,
                start_at=min(item.start_at for item in fragments),
                end_at=max(item.end_at for item in fragments),
                termination_reason="persisted_chain_fragments",
                reference_ids=references,
                observed_span_count=len(references),
                provenance_proven=all(item.provenance_proven for item in fragments),
                query_health_adequate=all(
                    item.query_health_adequate for item in fragments
                ),
                blocker_kinds=tuple(
                    sorted(
                        {
                            blocker
                            for fragment in fragments
                            for blocker in fragment.blocker_kinds
                        }
                    )
                ),
                lifecycle=ChainLifecycle.HISTORICAL,
                reason_codes=tuple(
                    sorted(
                        {
                            reason
                            for fragment in fragments
                            for reason in fragment.reason_codes
                        }
                        | {"durable_chain_fragments_merged_by_identity"}
                    )
                ),
            )
        )
    return tuple(sorted(values, key=lambda item: (item.start_at, item.chain_id)))


def _version_manifest() -> dict:
    return {
        "root_schema_version": SCHEMA4_ROOT_VERSION,
        "runtime_state_version": SCHEMA4_RUNTIME_STATE_VERSION,
        "physical_event_provenance_version": SCHEMA4_PHYSICAL_EVENT_PROVENANCE_VERSION,
        "continuity_semantics_version": SCHEMA4_CONTINUITY_SEMANTICS_VERSION,
        "relationship_semantics_version": SCHEMA4_RELATIONSHIP_SEMANTICS_VERSION,
        "streak_semantics_version": SCHEMA4_STREAK_SEMANTICS_VERSION,
        "expected_window_semantics_version": SCHEMA4_EXPECTED_WINDOW_SEMANTICS_VERSION,
        "authority_semantics_version": SCHEMA4_AUTHORITY_SEMANTICS_VERSION,
        "regime_semantics_version": SCHEMA4_REGIME_SEMANTICS_VERSION,
        "consumer_adapter_version": SCHEMA4_CONSUMER_ADAPTER_VERSION,
        "migration_tool_version": SCHEMA4_MIGRATION_TOOL_VERSION,
    }


def _require_current_apply_versions(state: Mapping[str, object]) -> None:
    manifest = state.get("version_manifest")
    audit = state.get("migration_audit")
    if not isinstance(manifest, Mapping) or not isinstance(audit, Mapping):
        raise Schema4ValidationError(
            "prepared schema-4 state lacks versioned migration metadata"
        )
    if (
        manifest.get("migration_tool_version") != SCHEMA4_MIGRATION_TOOL_VERSION
        or audit.get("migration_tool_version") != SCHEMA4_MIGRATION_TOOL_VERSION
        or state.get("scoring_algorithm_version")
        != SCHEMA4_SCORING_ALGORITHM_VERSION
    ):
        raise Schema4VersionError(
            "prepared schema-4 state is readable but is not eligible for apply "
            "under the current migration/scoring semantics"
        )


def _decision_gate(decision: AuthorityDecision) -> AuthorityGate:
    if decision.selected_shadow_regime is None:
        return AuthorityGate.NONE
    return decision.selected_shadow_regime.authority_gate


def _quarantined_migration_record(server_key: str, raw: object, exc: Exception) -> dict:
    return {
        "schema_version": SCHEMA4_ROOT_VERSION,
        "semantic_version": SCHEMA4_AUTHORITY_SEMANTICS_VERSION,
        "server_key": server_key,
        "authority_status": "quarantined",
        "validation_errors": [{"code": "migration_failed", "detail": f"{type(exc).__name__}: {exc}"}],
        "quarantined_payload": copy.deepcopy(raw),
        "reason_codes": ["server_migration_failed_closed"],
    }


def _quarantined_migration_audit(server_key: str, raw: object, exc: Exception) -> dict:
    return {
        "audit_id": _stable_id("schema4-quarantine-audit", server_key, _primitive(raw)),
        "schema_version": SCHEMA4_ROOT_VERSION,
        "semantic_version": SCHEMA4_MIGRATION_TOOL_VERSION,
        "server_key": server_key,
        "source_event_count": len(raw.get("events", ())) if isinstance(raw, dict) else 0,
        "source_coverage_count": len(raw.get("coverage_segments", ())) if isinstance(raw, dict) else 0,
        "source_session_count": len(raw.get("monitoring_sessions", ())) if isinstance(raw, dict) else 0,
        "source_expected_miss_count": len(raw.get("expected_window_misses", ())) if isinstance(raw, dict) else 0,
        "continuity_chains_reconstructed": 0, "relationships_reconstructed": 0, "high_eligible_relationships": 0,
        "relationships_left_normal_only": 0, "streaks_reconstructed": 0, "expected_window_rows_imported": 0,
        "revisions_created": 0, "active_misses_before": 0, "active_misses_after": 0, "authority_candidates": 0,
        "selected_shadow_regime": None, "selected_shadow_period_seconds": None, "h_gate": AuthorityGate.NONE.value,
        "migration_warnings": [f"{type(exc).__name__}: {exc}"], "unprovable_history": 0, "dropped_duplicate_ids": 0,
        "reference_validation_result": "quarantined", "reason_codes": ["server_migration_failed_closed"],
    }


def _migration_totals(audits: Sequence[Mapping[str, object]]) -> dict:
    fields = (
        "source_event_count", "source_coverage_count", "source_session_count", "source_expected_miss_count",
        "continuity_chains_reconstructed", "relationships_reconstructed", "high_eligible_relationships",
        "relationships_left_normal_only", "streaks_reconstructed", "expected_window_rows_imported", "revisions_created",
        "active_misses_before", "active_misses_after", "authority_candidates", "unprovable_history", "dropped_duplicate_ids",
    )
    return {
        "server_count": len(audits),
        "quarantined_server_count": sum("server_migration_failed_closed" in item.get("reason_codes", ()) for item in audits),
        **{field: sum(int(item.get(field, 0) or 0) for item in audits) for field in fields},
    }


def _validate_no_lineage_cycle(by_id: Mapping[object, Mapping[str, object]], add) -> None:
    for identity, item in by_id.items():
        seen: set[object] = set()
        current = item
        while current.get("supersedes_result_id") is not None:
            parent = current.get("supersedes_result_id")
            if parent in seen or parent == identity:
                add("revision_lineage_cycle", f"expected-window lineage cycles at {identity}")
                break
            seen.add(parent)
            current = by_id.get(parent, {})


def _safe_nonnegative_number(value: object, fallback: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        return fallback
    return value


def _optional_nonnegative_number(value: object) -> float | None:
    parsed = _safe_nonnegative_number(value, -1)
    return None if parsed == -1 else float(parsed)


def _stable_id(*parts: object) -> str:
    return _sha256(json.dumps(_primitive(parts), ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON value {value}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare or explicitly apply Server Companion schema 4")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--prepared", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--server")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--approval-token")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dry_run:
        if args.prepared is None or args.audit is None or args.backup is None:
            raise SystemExit("--dry-run requires --prepared, --audit, and --backup")
        result = migrate_state_file(
            source_path=args.source,
            prepared_path=args.prepared,
            audit_path=args.audit,
            backup_path=args.backup,
            server_key=args.server,
        )
        print(json.dumps({
            "source_sha256": result.source_sha256,
            "backup_sha256": result.backup_sha256,
            "prepared_path": str(result.prepared_path),
            "prepared_size": result.prepared_size,
            "prepared_sha256": result.prepared_sha256,
            "audit_path": str(result.audit_path),
            "validation": result.validation.valid,
            "idempotent_noop": result.idempotent_noop,
        }, sort_keys=True))
        return 0
    if args.prepared is None or args.backup is None or not args.expected_source_sha256:
        raise SystemExit("--apply requires --prepared, --backup, and --expected-source-sha256")
    apply_prepared_schema4(
        active_path=args.source,
        prepared_path=args.prepared,
        verified_backup_path=args.backup,
        expected_source_sha256=args.expected_source_sha256,
        approval_token=str(args.approval_token or ""),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
