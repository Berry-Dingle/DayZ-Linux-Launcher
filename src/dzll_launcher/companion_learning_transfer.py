from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .companion_restart_phase2_runtime import (
    MAX_COVERAGE_SEGMENTS,
    MAX_FINALIZED_EVENTS,
    MAX_FIRED_KEYS,
    MAX_INCOMPLETE_EPISODES,
    MAX_MONITORING_SESSIONS,
    _deserialize_server,
)
from .companion_restart_phase2_detection import DetectionConfig
from .companion_restart_phase2_schema4 import (
    SCHEMA4_ROOT_VERSION,
    Schema4ValidationError,
    Schema4VersionError,
    deserialize_schema4_bytes,
    schema_version_from_bytes,
    serialize_schema4_state,
)
from .companion_restart_phase2_schema4_runtime import Schema4WriterLock
from .companion_restart_phase2_storage import normalize_phase2_state


MAX_IMPORT_BYTES = 8 * 1024 * 1024
PENDING_FORMAT_VERSION = 1
PENDING_PAYLOAD_NAME = "companion_restart_learning_phase2.pending-import.json"
PENDING_METADATA_NAME = PENDING_PAYLOAD_NAME + ".meta"
IMPORT_BACKUP_PREFIX = "companion_restart_learning_phase2.import-backup"
PENDING_METADATA_FIELDS = frozenset({
    "pending_format_version", "payload_sha256", "schema_version", "server_count",
    "source_filename", "staged_at",
})

ROOT_REQUIRED_FIELDS = frozenset({
    "schema_version", "runtime_state_version", "scoring_algorithm_version",
    "state_generation_id", "created_at", "updated_at", "version_manifest",
    "source_schema3", "schema3_snapshot", "servers", "migration_audit",
    "reason_codes",
})
ROOT_OPTIONAL_FIELDS = frozenset({
    "runtime_write_generation", "runtime_write_semantics_version",
    "previous_runtime_payload_sha256",
})
SERVER_FIELDS = frozenset({
    "schema_version", "semantic_version", "server_key", "authority_status",
    "legacy_schema3_record", "physical_events", "continuity_spans",
    "continuity_chains", "interval_relationships", "cadence_streaks",
    "expected_window_episode_evidence", "expected_window_results",
    "normal_evidence_ledger", "high_evidence_ledger", "authority_candidates",
    "challenger_contexts", "regimes", "authority_decisions",
    "prior_authority_decision_id", "authority_decision", "consumer_state",
    "migration_audit", "reason_codes",
})


class ExternalLearningValidationError(ValueError):
    pass


class PendingImportError(RuntimeError):
    pass


def format_learned_server_records(count: int) -> str:
    value = int(count)
    noun = "record" if value == 1 else "records"
    return f"{value} learned server {noun}"


@dataclass(frozen=True)
class ValidatedLearningImport:
    canonical_bytes: bytes
    sha256: str
    schema_version: int
    server_count: int
    source_filename: str


@dataclass(frozen=True)
class PendingLearningImport:
    payload_path: Path
    metadata_path: Path
    sha256: str
    schema_version: int
    server_count: int
    source_filename: str
    staged_at: str
    canonical_bytes: bytes


@dataclass(frozen=True)
class StartupImportResult:
    status: str
    safe_to_initialize: bool = True
    server_count: int = 0
    backup_path: Path | None = None
    error: str | None = None
    pending_path: Path | None = None
    live_path: Path | None = None

    @property
    def notice_title(self) -> str | None:
        if self.status == "applied":
            return "Server Companion learning database replaced"
        if self.status in {"pending_invalid", "apply_failed", "rolled_back"}:
            return "Server Companion learning database replacement failed"
        if self.status == "rollback_failed":
            return "Server Companion learning disabled"
        return None

    @property
    def notice_body(self) -> str | None:
        if self.status == "applied":
            records = format_learned_server_records(self.server_count)
            return (
                "The previous Server Companion learning database was replaced successfully. "
                f"The new database contains {records}. "
                "A permanent backup of the previous database was retained."
            )
        if self.status == "rolled_back":
            return f"The replacement database could not be installed and the previous learning database was restored. {self.error or ''}".strip()
        if self.status == "rollback_failed":
            return f"Database replacement and rollback verification failed. Learning is disabled for this launch. {self.error or ''}".strip()
        if self.status in {"pending_invalid", "apply_failed"}:
            return f"The pending database replacement was not applied. The current learning database was left unchanged. {self.error or ''}".strip()
        return None


def pending_import_paths(config_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    root = Path(config_dir)
    return root / PENDING_PAYLOAD_NAME, root / PENDING_METADATA_NAME


def validate_external_learning_bytes(
    raw: bytes, *, source_filename: str = ""
) -> ValidatedLearningImport:
    if not isinstance(raw, bytes):
        raise TypeError("external learning input must be bytes")
    if not raw:
        raise ExternalLearningValidationError("The selected file is empty.")
    if len(raw) > MAX_IMPORT_BYTES:
        raise ExternalLearningValidationError("The selected file exceeds the 8 MiB limit.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExternalLearningValidationError(f"The selected file is not valid UTF-8: {exc}") from exc
    try:
        state = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExternalLearningValidationError(f"Invalid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ExternalLearningValidationError("The JSON top level must be an object.")
    version = state.get("schema_version")
    if type(version) is not int or version != SCHEMA4_ROOT_VERSION:
        raise ExternalLearningValidationError(
            f"Unsupported learning schema {version!r}; schema 4 is required."
        )
    missing = ROOT_REQUIRED_FIELDS - state.keys()
    unknown = state.keys() - ROOT_REQUIRED_FIELDS - ROOT_OPTIONAL_FIELDS
    if missing:
        raise ExternalLearningValidationError(f"Missing required root fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ExternalLearningValidationError(f"Unknown root fields: {', '.join(sorted(unknown))}")
    servers = state.get("servers")
    if not isinstance(servers, dict):
        raise ExternalLearningValidationError("servers must be an object.")
    _validate_server_keys_and_fields(servers)
    _validate_root_scalars(state)
    _reject_boolean_numeric_fields(state)
    _reject_nonfinite_tree(state)
    try:
        loaded = deserialize_schema4_bytes(raw, quarantine_invalid_servers=False)
    except (Schema4ValidationError, Schema4VersionError, TypeError, ValueError) as exc:
        raise ExternalLearningValidationError(str(exc)) from exc
    if not loaded.report.valid:
        detail = "; ".join(f"{item.code}: {item.detail}" for item in loaded.report.issues)
        raise ExternalLearningValidationError(detail or "Schema-4 validation failed.")
    if loaded.report.quarantined_server_keys:
        raise ExternalLearningValidationError(
            "The selected database may not contain quarantined records."
        )
    _validate_schema3_projection(loaded.state)
    try:
        canonical = serialize_schema4_state(loaded.state)
        roundtrip = deserialize_schema4_bytes(canonical, quarantine_invalid_servers=False)
        canonical_again = serialize_schema4_state(roundtrip.state)
    except Exception as exc:
        raise ExternalLearningValidationError(f"Canonical validation failed: {exc}") from exc
    if canonical != canonical_again:
        raise ExternalLearningValidationError("The document is not stable under canonical round trip.")
    if not roundtrip.report.valid:
        raise ExternalLearningValidationError("Canonical document failed strict validation.")
    return ValidatedLearningImport(
        canonical_bytes=canonical,
        sha256=_sha256(canonical),
        schema_version=SCHEMA4_ROOT_VERSION,
        server_count=len(roundtrip.state["servers"]),
        source_filename=Path(source_filename).name if source_filename else "selected file",
    )


def validate_external_learning_file(path: str | os.PathLike[str]) -> ValidatedLearningImport:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ExternalLearningValidationError(f"Cannot read selected file: {exc}") from exc
    if size > MAX_IMPORT_BYTES:
        raise ExternalLearningValidationError("The selected file exceeds the 8 MiB limit.")
    try:
        raw = _read_capped(source)
    except OSError as exc:
        raise ExternalLearningValidationError(f"Cannot read selected file: {exc}") from exc
    return validate_external_learning_bytes(raw, source_filename=source.name)


def stage_pending_import(
    validated: ValidatedLearningImport,
    *,
    config_dir: str | os.PathLike[str],
    replace_existing: bool = False,
    now: datetime | None = None,
) -> PendingLearningImport:
    payload_path, metadata_path = pending_import_paths(config_dir)
    existing = read_pending_import(config_dir, strict=False)
    if (payload_path.exists() or metadata_path.exists()) and not replace_existing:
        raise PendingImportError("A pending Server Companion learning database replacement already exists.")
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    staged_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "pending_format_version": PENDING_FORMAT_VERSION,
        "payload_sha256": validated.sha256,
        "schema_version": validated.schema_version,
        "server_count": validated.server_count,
        "source_filename": validated.source_filename,
        "staged_at": staged_at,
    }
    metadata_bytes = _canonical_metadata_bytes(metadata)
    prior_payload = payload_path.read_bytes() if existing is not None else None
    prior_metadata = metadata_path.read_bytes() if existing is not None else None
    # Preserve a valid prior pair until both replacement temp files are complete.
    payload_temp = None
    metadata_temp = None
    try:
        payload_temp = _prepare_temp(payload_path, validated.canonical_bytes)
        metadata_temp = _prepare_temp(metadata_path, metadata_bytes)
        if _sha256(payload_temp.read_bytes()) != validated.sha256:
            raise PendingImportError("Pending payload verification failed.")
        if json.loads(metadata_temp.read_text(encoding="utf-8")) != metadata:
            raise PendingImportError("Pending metadata verification failed.")
        os.replace(payload_temp, payload_path)
        _fsync_directory(payload_path.parent)
        os.replace(metadata_temp, metadata_path)
        _fsync_directory(metadata_path.parent)
        result = read_pending_import(config_dir, strict=True)
        if result is None:
            raise PendingImportError("Pending database replacement verification failed.")
        return result
    except Exception:
        if payload_temp is not None:
            _unlink_quiet(payload_temp)
        if metadata_temp is not None:
            _unlink_quiet(metadata_temp)
        # If there was no valid prior pair, never leave a newly incomplete pair.
        if existing is None:
            _unlink_quiet(payload_path)
            _unlink_quiet(metadata_path)
        else:
            try:
                _atomic_write_verified(payload_path, prior_payload or b"")
                _atomic_write_verified(metadata_path, prior_metadata or b"")
            except Exception as restore_exc:
                raise PendingImportError(
                    f"Staging failed and the previous pending database replacement could not be restored: {restore_exc}"
                )
        raise


def read_pending_import(
    config_dir: str | os.PathLike[str], *, strict: bool = True
) -> PendingLearningImport | None:
    payload_path, metadata_path = pending_import_paths(config_dir)
    if not payload_path.exists() and not metadata_path.exists():
        return None
    if not payload_path.exists() or not metadata_path.exists():
        if strict:
            raise PendingImportError("Pending database replacement payload and metadata are incomplete.")
        return None
    try:
        try:
            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonfinite,
            )
        except Exception as exc:
            raise PendingImportError(
                f"Pending database replacement metadata is invalid: {exc}"
            ) from exc
        if not isinstance(metadata, dict) or set(metadata) != PENDING_METADATA_FIELDS:
            raise PendingImportError("Pending database replacement metadata fields are incomplete or unknown.")
        if metadata.get("pending_format_version") != PENDING_FORMAT_VERSION:
            raise PendingImportError("Pending database replacement metadata version is unsupported.")
        if type(metadata.get("schema_version")) is not int or type(metadata.get("server_count")) is not int or metadata["server_count"] < 0:
            raise PendingImportError("Pending database replacement metadata numeric fields are invalid.")
        if metadata["schema_version"] != SCHEMA4_ROOT_VERSION:
            raise PendingImportError("Pending database replacement metadata schema is unsupported.")
        if not isinstance(metadata.get("payload_sha256"), str) or len(metadata["payload_sha256"]) != 64:
            raise PendingImportError("Pending database replacement metadata SHA-256 is invalid.")
        try:
            int(metadata["payload_sha256"], 16)
        except ValueError as exc:
            raise PendingImportError("Pending database replacement metadata SHA-256 is invalid.") from exc
        if not isinstance(metadata.get("source_filename"), str) or not isinstance(metadata.get("staged_at"), str):
            raise PendingImportError("Pending database replacement display metadata is invalid.")
        if not metadata["source_filename"] or Path(metadata["source_filename"]).name != metadata["source_filename"]:
            raise PendingImportError("Pending database replacement source filename is invalid.")
        try:
            staged = datetime.fromisoformat(metadata["staged_at"].replace("Z", "+00:00"))
            if staged.tzinfo is None:
                raise ValueError("timezone is required")
        except ValueError as exc:
            raise PendingImportError("Pending database replacement staged timestamp is invalid.") from exc
        try:
            payload = _read_capped(payload_path)
        except ExternalLearningValidationError as exc:
            raise PendingImportError(str(exc)) from exc
        digest = _sha256(payload)
        if digest != metadata.get("payload_sha256"):
            raise PendingImportError("Pending database replacement SHA-256 does not match metadata.")
        validated = validate_external_learning_bytes(
            payload, source_filename=str(metadata.get("source_filename") or "pending import")
        )
        if validated.sha256 != digest:
            raise PendingImportError("Pending canonical SHA-256 verification failed.")
        if validated.schema_version != metadata.get("schema_version"):
            raise PendingImportError("Pending schema version does not match metadata.")
        if validated.server_count != metadata.get("server_count"):
            raise PendingImportError("Pending server count does not match metadata.")
        return PendingLearningImport(
            payload_path, metadata_path, digest, validated.schema_version,
            validated.server_count, str(metadata.get("source_filename") or "pending import"),
            str(metadata.get("staged_at") or ""), validated.canonical_bytes,
        )
    except Exception:
        if strict:
            raise
        return None


def cancel_pending_import(config_dir: str | os.PathLike[str]) -> bool:
    payload, metadata = pending_import_paths(config_dir)
    existed = payload.exists() or metadata.exists()
    _unlink_quiet(payload)
    _unlink_quiet(metadata)
    _fsync_directory(Path(config_dir))
    if payload.exists() or metadata.exists():
        raise PendingImportError("Pending database replacement files could not be removed.")
    return existed


def apply_pending_import_at_startup(
    *, config_dir: str | os.PathLike[str], live_path: str | os.PathLike[str],
    now: datetime | None = None,
) -> StartupImportResult:
    payload_path, metadata_path = pending_import_paths(config_dir)
    live = Path(live_path)
    if not payload_path.exists() and not metadata_path.exists():
        return StartupImportResult("none", live_path=live)
    try:
        pending = read_pending_import(config_dir, strict=True)
        assert pending is not None
    except Exception as exc:
        return StartupImportResult(
            "pending_invalid", safe_to_initialize=live.exists(), error=str(exc),
            pending_path=payload_path, live_path=live,
        )
    imported = pending.canonical_bytes
    prior_exists = False
    prior = b""
    prior_schema_version: int | None = None
    backup: Path | None = None
    installation_attempted = False
    try:
        with Schema4WriterLock(live):
            prior_exists = live.exists()
            if prior_exists:
                prior = live.read_bytes()
                prior_schema_version = _validate_live_authority_bytes(prior)
                backup = _next_backup_path(live.parent, now=now)
                _write_new_verified(backup, prior)
                if prior_schema_version == SCHEMA4_ROOT_VERSION:
                    _atomic_write_verified(
                        live.with_name(live.name + ".last-known-good"), prior
                    )
            installation_attempted = True
            _install_verified(live, imported)
            installed = validate_external_learning_bytes(live.read_bytes(), source_filename=live.name)
            if installed.sha256 != pending.sha256:
                raise PendingImportError("Installed learning payload failed SHA-256 verification.")
        _unlink_quiet(pending.payload_path)
        _unlink_quiet(pending.metadata_path)
        _fsync_directory(live.parent)
        return StartupImportResult("applied", True, pending.server_count, backup, pending_path=payload_path, live_path=live)
    except Exception as exc:
        if installation_attempted:
            try:
                with Schema4WriterLock(live):
                    if prior_exists and backup is not None:
                        _restore_validated_live(
                            live,
                            backup.read_bytes(),
                            schema_version=prior_schema_version,
                        )
                        if live.read_bytes() != prior:
                            raise PendingImportError("Rollback bytes do not match the permanent backup.")
                    elif not prior_exists:
                        _unlink_quiet(live)
                        _fsync_directory(live.parent)
                return StartupImportResult(
                    "rolled_back" if prior_exists else "apply_failed",
                    prior_exists,
                    error=str(exc), backup_path=backup,
                    pending_path=payload_path, live_path=live,
                )
            except Exception as rollback_exc:
                return StartupImportResult(
                    "rollback_failed", False, error=f"install: {exc}; rollback: {rollback_exc}",
                    backup_path=backup, pending_path=payload_path, live_path=live,
                )
        return StartupImportResult(
            "apply_failed", live.exists(), error=str(exc), backup_path=backup,
            pending_path=payload_path, live_path=live,
        )


def write_export_file(path: str | os.PathLike[str], payload: bytes) -> None:
    destination = Path(path)
    validated = validate_external_learning_bytes(payload, source_filename=destination.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_verified(destination, validated.canonical_bytes)


def default_export_filename(now: datetime | None = None) -> str:
    value = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"dzll-server-companion-learning-schema4-{value.strftime('%Y%m%d-%H%M%S')}.json"


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _reject_nonfinite(value: str):
    raise ValueError(f"non-finite JSON value {value}")


def _reject_nonfinite_tree(value: object, path: str = "root") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ExternalLearningValidationError(f"{path} contains a non-finite number.")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_tree(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_tree(item, f"{path}[{index}]")


def _validate_server_keys_and_fields(servers: Mapping[str, object]) -> None:
    for key, record in servers.items():
        if not isinstance(key, str) or not key or ":" not in key:
            raise ExternalLearningValidationError(f"Invalid server identity {key!r}.")
        host, port_text = key.rsplit(":", 1)
        if not host.strip() or not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
            raise ExternalLearningValidationError(f"Invalid server identity {key!r}.")
        if not isinstance(record, dict):
            raise ExternalLearningValidationError(f"Server {key!r} must be an object.")
        unknown = record.keys() - SERVER_FIELDS
        if unknown:
            raise ExternalLearningValidationError(f"Server {key!r} has unknown fields: {', '.join(sorted(unknown))}")
        if record.get("server_key") != key:
            raise ExternalLearningValidationError(f"Server key mismatch for {key!r}.")
        if record.get("authority_status") == "quarantined":
            raise ExternalLearningValidationError(f"Server {key!r} is quarantined.")


def _validate_schema3_projection(state: Mapping[str, object]) -> None:
    snapshot = state.get("schema3_snapshot")
    try:
        normalized = normalize_phase2_state(snapshot)
    except Exception as exc:
        raise ExternalLearningValidationError(f"Invalid embedded schema-3 projection: {exc}") from exc
    projected = normalized.get("servers", {})
    authoritative = state.get("servers", {})
    if set(projected) != set(authoritative):
        raise ExternalLearningValidationError("Embedded schema-3 server keys do not match authoritative servers.")
    for key, record in authoritative.items():
        legacy = record.get("legacy_schema3_record")
        if not isinstance(legacy, dict) or projected.get(key) != legacy:
            raise ExternalLearningValidationError(f"Embedded schema-3 record mismatch for {key!r}.")
        limits = {
            "events": MAX_FINALIZED_EVENTS,
            "coverage_segments": MAX_COVERAGE_SEGMENTS,
            "fired_keys": MAX_FIRED_KEYS,
            "incomplete_episodes": MAX_INCOMPLETE_EPISODES,
            "monitoring_sessions": MAX_MONITORING_SESSIONS,
        }
        for field, limit in limits.items():
            values = legacy.get(field, ())
            if not isinstance(values, list) or len(values) > limit:
                raise ExternalLearningValidationError(
                    f"Schema-3 {field} for {key!r} exceeds its retention contract."
                )
        try:
            _deserialize_server(key, legacy, DetectionConfig())
        except Exception as exc:
            raise ExternalLearningValidationError(f"Invalid schema-3 server {key!r}: {exc}") from exc


def _validate_root_scalars(state: Mapping[str, object]) -> None:
    manifest = state.get("version_manifest")
    if not isinstance(manifest, dict):
        raise ExternalLearningValidationError("version_manifest must be an object.")
    for name in ("runtime_state_version", "scoring_algorithm_version"):
        if type(state.get(name)) is not int or state[name] <= 0:
            raise ExternalLearningValidationError(f"{name} must be a positive integer.")
    for name in ("created_at", "updated_at"):
        value = state.get(name)
        if type(value) not in {int, float} or not math.isfinite(float(value)) or value < 0:
            raise ExternalLearningValidationError(f"{name} must be a finite non-negative number.")
    if float(state["updated_at"]) < float(state["created_at"]):
        raise ExternalLearningValidationError("updated_at must not precede created_at.")
    if not isinstance(state.get("state_generation_id"), str) or not state["state_generation_id"]:
        raise ExternalLearningValidationError("state_generation_id must be a non-empty string.")
    if "runtime_write_generation" in state and (
        type(state["runtime_write_generation"]) is not int
        or state["runtime_write_generation"] < 0
    ):
        raise ExternalLearningValidationError("runtime_write_generation must be a non-negative integer.")


def _reject_boolean_numeric_fields(value: object, path: str = "root") -> None:
    numeric_exact = {
        "schema_version", "semantic_version", "sequence", "event_seq",
        "folded_through_event_seq", "poll_generation", "suspicion_level",
        "runtime_write_generation", "runtime_state_version",
        "scoring_algorithm_version", "source_size", "source_mtime_ns",
    }
    numeric_suffixes = (
        "_at", "_seconds", "_count", "_version", "_confidence", "_weight",
        "_ratio", "_offset", "_generation", "_uncertainty", "_players",
    )
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, bool) and (
                key in numeric_exact or any(key.endswith(suffix) for suffix in numeric_suffixes)
            ):
                raise ExternalLearningValidationError(
                    f"{path}.{key} must be numeric, not boolean."
                )
            _reject_boolean_numeric_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_boolean_numeric_fields(item, f"{path}[{index}]")


def _validate_live_authority_bytes(raw: bytes) -> int:
    version = schema_version_from_bytes(raw)
    if version == SCHEMA4_ROOT_VERSION:
        loaded = deserialize_schema4_bytes(raw, quarantine_invalid_servers=True)
        if not loaded.report.root_valid:
            raise PendingImportError(
                "Current live learning file has an invalid schema-4 root."
            )
        return version
    if version == 3:
        try:
            decoded = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_nonfinite,
            )
            normalize_phase2_state(decoded)
        except Exception as exc:
            raise PendingImportError(
                f"Current live schema-3 learning file is invalid: {exc}"
            ) from exc
        return version
    raise PendingImportError(
        f"Current live learning schema {version} cannot be replaced safely."
    )


def _restore_validated_live(
    destination: Path,
    payload: bytes,
    *,
    schema_version: int | None,
) -> None:
    if schema_version == SCHEMA4_ROOT_VERSION:
        _install_verified(destination, payload)
        return
    if schema_version == 3:
        if _validate_live_authority_bytes(payload) != 3:
            raise PendingImportError("Schema-3 rollback payload validation failed.")
        _atomic_write_verified(destination, payload)
        if destination.read_bytes() != payload:
            raise PendingImportError("Schema-3 rollback byte verification failed.")
        return
    raise PendingImportError("The previous live learning schema is unknown.")


def _canonical_metadata_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prepare_temp(destination: Path, payload: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except Exception:
        _unlink_quiet(path)
        raise


def _atomic_write_verified(destination: Path, payload: bytes) -> None:
    temp = _prepare_temp(destination, payload)
    try:
        os.replace(temp, destination)
        _fsync_directory(destination.parent)
        if destination.read_bytes() != payload:
            raise PendingImportError(f"Verification failed for {destination}.")
    finally:
        _unlink_quiet(temp)


def _install_verified(destination: Path, payload: bytes) -> None:
    validate_external_learning_bytes(payload, source_filename=destination.name)
    _atomic_write_verified(destination, payload)
    validate_external_learning_bytes(destination.read_bytes(), source_filename=destination.name)


def _write_new_verified(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    _fsync_directory(destination.parent)
    if destination.read_bytes() != payload:
        raise PendingImportError(f"Backup verification failed for {destination}.")


def _next_backup_path(directory: Path, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = directory / f"{IMPORT_BACKUP_PREFIX}-{stamp}.json"
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = directory / f"{IMPORT_BACKUP_PREFIX}-{stamp}-{index:02d}.json"
        if not candidate.exists():
            return candidate
        index += 1


def _fsync_directory(directory: Path) -> None:
    if not directory.exists():
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_capped(path: Path) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(MAX_IMPORT_BYTES + 1)
    if len(payload) > MAX_IMPORT_BYTES:
        raise ExternalLearningValidationError("The selected file exceeds the 8 MiB limit.")
    return payload
