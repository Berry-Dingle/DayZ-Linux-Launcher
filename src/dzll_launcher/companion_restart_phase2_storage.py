from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import tempfile
import time
import uuid


PHASE2_SCHEMA_VERSION = 3
PHASE2_SCORING_ALGORITHM_VERSION = 1
PHASE2_MIGRATION_RESET_VERSION = "phase2-v032-1"

PHASE2_ACTIVE_FILENAME = "companion_restart_learning_phase2.json"
LEGACY_PHASE1_FILENAME = "companion_restart_learning.json"
LEGACY_BACKUP_PREFIX = "companion_restart_learning.pre-phase2"


class InvalidPhase2StateError(ValueError):
    """Raised when JSON does not identify a Phase 2 restart-learning state."""


class AtomicWriteError(OSError):
    """A destination was not safely installed by an atomic write."""

    def __init__(self, path: Path, stage: str, cause: BaseException):
        self.path = Path(path)
        self.stage = str(stage)
        self.cause = cause
        super().__init__(f"atomic write failed during {self.stage} for {self.path}: {cause}")


class Phase2InitializationStatus(str, Enum):
    ACTIVE_LOADED = "active_loaded"
    FRESH_CREATED = "fresh_created"
    LEGACY_MIGRATED = "legacy_migrated"
    ACTIVE_RECOVERED = "active_recovered"
    FAILED = "failed"


class Phase2MigrationFailure(str, Enum):
    ACTIVE_READ_FAILED = "active_read_failed"
    FRESH_WRITE_FAILED = "fresh_write_failed"
    LEGACY_READ_FAILED = "legacy_read_failed"
    ACTIVE_PREPARE_FAILED = "active_prepare_failed"
    BACKUP_WRITE_FAILED = "backup_write_failed"
    BACKUP_VERIFICATION_FAILED = "backup_verification_failed"
    ACTIVE_INSTALL_FAILED = "active_install_failed"
    ACTIVE_VERIFICATION_FAILED = "active_verification_failed"
    CORRUPT_BACKUP_FAILED = "corrupt_backup_failed"
    RECOVERY_WRITE_FAILED = "recovery_write_failed"


@dataclass(frozen=True)
class Phase2MigrationResult:
    state: dict
    status: Phase2InitializationStatus
    reset_performed: bool
    recovery_performed: bool
    active_path: Path
    backup_path: Path | None
    legacy_checksum: str | None
    persistence_enabled: bool
    failure: Phase2MigrationFailure | None = None
    error_kind: str | None = None
    error: str | None = None


def phase2_paths(config_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    root = Path(config_dir)
    return root / PHASE2_ACTIVE_FILENAME, root / LEGACY_PHASE1_FILENAME


def new_phase2_state(
    *,
    now: int | float | None = None,
    generation_id: str | None = None,
    reset_from_checksum: str | None = None,
    reset_backup_path: str | os.PathLike[str] | None = None,
    reset_completed_at: int | float | None = None,
) -> dict:
    timestamp = _safe_timestamp(time.time() if now is None else now, 0)
    generation = _normalize_generation_id(
        str(uuid.uuid4()) if generation_id is None else generation_id
    )
    checksum = _normalize_checksum(reset_from_checksum)
    backup_path = _normalize_optional_string(reset_backup_path)
    completed_at = _normalize_optional_timestamp(reset_completed_at)
    return {
        "schema_version": PHASE2_SCHEMA_VERSION,
        "scoring_algorithm_version": PHASE2_SCORING_ALGORITHM_VERSION,
        "migration_reset_version": PHASE2_MIGRATION_RESET_VERSION,
        "state_generation_id": generation,
        "created_at": timestamp,
        "updated_at": timestamp,
        "reset_from_checksum": checksum,
        "reset_backup_path": backup_path,
        "reset_completed_at": completed_at,
        "servers": {},
    }


def is_phase2_state(state: object) -> bool:
    if not isinstance(state, dict):
        return False
    schema_version = state.get("schema_version")
    if type(schema_version) is not int or schema_version != PHASE2_SCHEMA_VERSION:
        return False
    if state.get("migration_reset_version") != PHASE2_MIGRATION_RESET_VERSION:
        return False
    try:
        _normalize_generation_id(state.get("state_generation_id"))
    except InvalidPhase2StateError:
        return False
    return True


def normalize_phase2_state(state: object, *, now: int | float | None = None) -> dict:
    if not isinstance(state, dict):
        raise InvalidPhase2StateError("Phase 2 state must be a dictionary")
    if not is_phase2_state(state):
        raise InvalidPhase2StateError("JSON is not a valid Phase 2 restart-learning state")

    normalized = copy.deepcopy(state)
    # Normalization must be stable across repeated loads. Callers may provide a
    # deterministic fallback for malformed timestamps; otherwise use epoch zero
    # rather than changing derived state every time it is read.
    fallback_now = _safe_timestamp(0 if now is None else now, 0)
    created_at = _safe_timestamp(normalized.get("created_at"), fallback_now)
    updated_at = _safe_timestamp(normalized.get("updated_at"), created_at)
    scoring_version = _safe_int(
        normalized.get("scoring_algorithm_version"),
        PHASE2_SCORING_ALGORITHM_VERSION,
    )
    if scoring_version <= 0:
        scoring_version = PHASE2_SCORING_ALGORITHM_VERSION

    servers = normalized.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = {str(key): copy.deepcopy(value) for key, value in servers.items()}

    normalized.update({
        "schema_version": PHASE2_SCHEMA_VERSION,
        "scoring_algorithm_version": scoring_version,
        "migration_reset_version": PHASE2_MIGRATION_RESET_VERSION,
        "state_generation_id": _normalize_generation_id(
            normalized.get("state_generation_id")
        ),
        "created_at": created_at,
        "updated_at": max(created_at, updated_at),
        "reset_from_checksum": _normalize_checksum(
            normalized.get("reset_from_checksum")
        ),
        "reset_backup_path": _normalize_optional_string(
            normalized.get("reset_backup_path")
        ),
        "reset_completed_at": _normalize_optional_timestamp(
            normalized.get("reset_completed_at")
        ),
        "servers": servers,
    })
    try:
        _serialize_json(normalized)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidPhase2StateError(
            f"Phase 2 state contains non-JSON or non-finite values: {exc}"
        ) from exc
    return normalized


def load_phase2_state(path: str | os.PathLike[str]) -> dict:
    path = Path(path)
    raw = path.read_bytes()
    try:
        state = _decode_json_bytes(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidPhase2StateError(f"invalid Phase 2 JSON: {exc}") from exc
    return normalize_phase2_state(state)


def atomic_write_json(path: str | os.PathLike[str], data: object) -> None:
    destination = Path(path)
    try:
        payload = _serialize_json(data)
    except Exception as exc:
        raise AtomicWriteError(destination, "serialization", exc) from exc
    atomic_write_bytes(destination, payload)


def atomic_write_bytes(path: str | os.PathLike[str], payload: bytes) -> None:
    destination = Path(path)
    if not isinstance(payload, bytes):
        raise TypeError("atomic byte payload must be bytes")
    prepared = _prepare_atomic_bytes(destination, payload)
    _install_prepared(prepared, destination)


def initialize_phase2_state(
    *,
    active_path: str | os.PathLike[str],
    legacy_path: str | os.PathLike[str],
    now: int | float | None = None,
    generation_id: str | None = None,
) -> Phase2MigrationResult:
    """Load or safely initialize Phase 2 restart-learning storage.

    Explicit paths keep migration tests isolated from the user's configuration
    while the application supplies its production paths at startup.
    """

    active = Path(active_path)
    legacy = Path(legacy_path)
    timestamp = _safe_timestamp(time.time() if now is None else now, 0)

    if active.exists():
        return _load_or_recover_active(
            active=active,
            now=timestamp,
            generation_id=generation_id,
        )

    if not legacy.exists():
        state = new_phase2_state(now=timestamp, generation_id=generation_id)
        try:
            atomic_write_json(active, state)
        except Exception as exc:
            return _failure_result(
                state=state,
                active=active,
                failure=Phase2MigrationFailure.FRESH_WRITE_FAILED,
                exc=exc,
            )
        return Phase2MigrationResult(
            state=state,
            status=Phase2InitializationStatus.FRESH_CREATED,
            reset_performed=False,
            recovery_performed=False,
            active_path=active,
            backup_path=None,
            legacy_checksum=None,
            persistence_enabled=True,
        )

    try:
        legacy_bytes = legacy.read_bytes()
    except Exception as exc:
        state = new_phase2_state(now=timestamp, generation_id=generation_id)
        return _failure_result(
            state=state,
            active=active,
            failure=Phase2MigrationFailure.LEGACY_READ_FAILED,
            exc=exc,
        )

    legacy_checksum = hashlib.sha256(legacy_bytes).hexdigest()
    backup = _find_matching_backup(
        legacy.parent,
        f"{LEGACY_BACKUP_PREFIX}-*.json",
        legacy_bytes,
    )
    if backup is None:
        backup = _next_timestamped_path(
            legacy.parent,
            LEGACY_BACKUP_PREFIX,
            timestamp,
        )

    state = new_phase2_state(
        now=timestamp,
        generation_id=generation_id,
        reset_from_checksum=legacy_checksum,
        reset_backup_path=str(backup),
        reset_completed_at=timestamp,
    )
    try:
        active_payload = _serialize_json(state)
        prepared_active = _prepare_atomic_bytes(active, active_payload)
    except Exception as exc:
        return _failure_result(
            state=state,
            active=active,
            backup=backup if backup.exists() else None,
            checksum=legacy_checksum,
            failure=Phase2MigrationFailure.ACTIVE_PREPARE_FAILED,
            exc=exc,
        )

    try:
        if not backup.exists() or not _exact_bytes_match(backup, legacy_bytes):
            backup = _preserve_exact_bytes(
                directory=legacy.parent,
                prefix=LEGACY_BACKUP_PREFIX,
                timestamp=timestamp,
                payload=legacy_bytes,
                preferred=backup,
                verify=False,
            )
    except Exception as exc:
        _remove_file_best_effort(prepared_active)
        return _failure_result(
            state=state,
            active=active,
            backup=backup if backup.exists() else None,
            checksum=legacy_checksum,
            failure=Phase2MigrationFailure.BACKUP_WRITE_FAILED,
            exc=exc,
        )

    if str(backup) != state["reset_backup_path"]:
        _remove_file_best_effort(prepared_active)
        state = new_phase2_state(
            now=timestamp,
            generation_id=state["state_generation_id"],
            reset_from_checksum=legacy_checksum,
            reset_backup_path=str(backup),
            reset_completed_at=timestamp,
        )
        try:
            active_payload = _serialize_json(state)
            prepared_active = _prepare_atomic_bytes(active, active_payload)
        except Exception as exc:
            return _failure_result(
                state=state,
                active=active,
                backup=backup,
                checksum=legacy_checksum,
                failure=Phase2MigrationFailure.ACTIVE_PREPARE_FAILED,
                exc=exc,
            )

    try:
        _verify_exact_bytes(backup, legacy_bytes)
    except Exception as exc:
        _remove_file_best_effort(prepared_active)
        return _failure_result(
            state=state,
            active=active,
            backup=backup,
            checksum=legacy_checksum,
            failure=Phase2MigrationFailure.BACKUP_VERIFICATION_FAILED,
            exc=exc,
        )

    try:
        _install_prepared(prepared_active, active)
    except Exception as exc:
        return _failure_result(
            state=state,
            active=active,
            backup=backup,
            checksum=legacy_checksum,
            failure=Phase2MigrationFailure.ACTIVE_INSTALL_FAILED,
            exc=exc,
        )

    try:
        _verify_exact_bytes(active, active_payload)
        normalize_phase2_state(_decode_json_bytes(active_payload))
    except Exception as exc:
        # Legacy and its verified backup still exist. Remove the unverified
        # active file when possible so the next launch retries the migration
        # instead of treating a partial install as authoritative.
        _remove_file_best_effort(active)
        _fsync_directory_best_effort(active.parent)
        return _failure_result(
            state=state,
            active=active,
            backup=backup,
            checksum=legacy_checksum,
            failure=Phase2MigrationFailure.ACTIVE_VERIFICATION_FAILED,
            exc=exc,
        )

    try:
        legacy.unlink()
        _fsync_directory_best_effort(legacy.parent)
    except OSError:
        # The verified backup and valid Phase 2 active file make cleanup optional.
        pass

    return Phase2MigrationResult(
        state=state,
        status=Phase2InitializationStatus.LEGACY_MIGRATED,
        reset_performed=True,
        recovery_performed=False,
        active_path=active,
        backup_path=backup,
        legacy_checksum=legacy_checksum,
        persistence_enabled=True,
    )


def _load_or_recover_active(
    *,
    active: Path,
    now: int,
    generation_id: str | None,
) -> Phase2MigrationResult:
    try:
        active_bytes = active.read_bytes()
    except Exception as exc:
        state = new_phase2_state(now=now, generation_id=generation_id)
        return _failure_result(
            state=state,
            active=active,
            failure=Phase2MigrationFailure.ACTIVE_READ_FAILED,
            exc=exc,
        )

    try:
        state = normalize_phase2_state(_decode_json_bytes(active_bytes))
    except Exception:
        state = new_phase2_state(now=now, generation_id=generation_id)
        corrupt_prefix = f"{active.stem}.corrupt"
        try:
            corrupt = _preserve_exact_bytes(
                directory=active.parent,
                prefix=corrupt_prefix,
                timestamp=now,
                payload=active_bytes,
            )
        except Exception as exc:
            return _failure_result(
                state=state,
                active=active,
                failure=Phase2MigrationFailure.CORRUPT_BACKUP_FAILED,
                exc=exc,
            )
        try:
            atomic_write_json(active, state)
        except Exception as exc:
            return _failure_result(
                state=state,
                active=active,
                backup=corrupt,
                failure=Phase2MigrationFailure.RECOVERY_WRITE_FAILED,
                exc=exc,
            )
        return Phase2MigrationResult(
            state=state,
            status=Phase2InitializationStatus.ACTIVE_RECOVERED,
            reset_performed=False,
            recovery_performed=True,
            active_path=active,
            backup_path=corrupt,
            legacy_checksum=None,
            persistence_enabled=True,
        )

    return Phase2MigrationResult(
        state=state,
        status=Phase2InitializationStatus.ACTIVE_LOADED,
        reset_performed=False,
        recovery_performed=False,
        active_path=active,
        backup_path=None,
        legacy_checksum=None,
        persistence_enabled=True,
    )


def _serialize_json(data: object) -> bytes:
    text = json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _decode_json_bytes(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=_reject_json_constant,
    )


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON value {value}")


def _prepare_atomic_bytes(destination: Path, payload: bytes) -> Path:
    destination = Path(destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise AtomicWriteError(destination, "parent_creation", exc) from exc

    fd = -1
    temporary: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary = Path(temp_name)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            _remove_file_best_effort(temporary)
        if isinstance(exc, AtomicWriteError):
            raise
        raise AtomicWriteError(destination, "temporary_write", exc) from exc


def _install_prepared(temporary: Path, destination: Path) -> None:
    temporary = Path(temporary)
    destination = Path(destination)
    try:
        os.replace(temporary, destination)
    except Exception as exc:
        _remove_file_best_effort(temporary)
        raise AtomicWriteError(destination, "replacement", exc) from exc
    _fsync_directory_best_effort(destination.parent)


def _preserve_exact_bytes(
    *,
    directory: Path,
    prefix: str,
    timestamp: int,
    payload: bytes,
    preferred: Path | None = None,
    verify: bool = True,
) -> Path:
    matching = _find_matching_backup(directory, f"{prefix}-*.json", payload)
    if matching is not None:
        return matching

    candidate = preferred or _next_timestamped_path(directory, prefix, timestamp)
    temporary = _prepare_atomic_bytes(candidate, payload)
    attempt = candidate
    suffix_index = _collision_index(candidate)
    while True:
        try:
            os.link(temporary, attempt)
            break
        except FileExistsError:
            suffix_index += 1
            attempt = _path_with_collision_suffix(candidate, suffix_index)
        except Exception as exc:
            _remove_file_best_effort(temporary)
            raise AtomicWriteError(attempt, "backup_replacement", exc) from exc
    _remove_file_best_effort(temporary)
    _fsync_directory_best_effort(directory)
    if verify:
        _verify_exact_bytes(attempt, payload)
    return attempt


def _verify_exact_bytes(path: Path, expected: bytes) -> None:
    actual = Path(path).read_bytes()
    if len(actual) != len(expected):
        raise OSError(f"backup size mismatch for {path}")
    if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
        raise OSError(f"backup checksum mismatch for {path}")


def _exact_bytes_match(path: Path, expected: bytes) -> bool:
    try:
        _verify_exact_bytes(path, expected)
        return True
    except OSError:
        return False


def _find_matching_backup(directory: Path, pattern: str, payload: bytes) -> Path | None:
    try:
        paths = sorted(Path(directory).glob(pattern))
    except OSError:
        return None
    for path in paths:
        if path.is_file() and _exact_bytes_match(path, payload):
            return path
    return None


def _next_timestamped_path(directory: Path, prefix: str, timestamp: int) -> Path:
    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = Path(directory) / f"{prefix}-{stamp}.json"
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = _path_with_collision_suffix(base, index)
        if not candidate.exists():
            return candidate
        index += 1


def _path_with_collision_suffix(path: Path, index: int) -> Path:
    name = path.name
    if name.endswith(".json"):
        name = name[:-5]
    if name.rsplit("-", 1)[-1].isdigit() and len(name.rsplit("-", 1)[-1]) == 2:
        name = name.rsplit("-", 1)[0]
    return path.with_name(f"{name}-{index:02d}.json")


def _collision_index(path: Path) -> int:
    stem = path.stem
    tail = stem.rsplit("-", 1)[-1]
    if len(tail) == 2 and tail.isdigit():
        return int(tail)
    return 0


def _fsync_directory_best_effort(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = -1
    try:
        fd = os.open(str(directory), flags)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _remove_file_best_effort(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _failure_result(
    *,
    state: dict,
    active: Path,
    failure: Phase2MigrationFailure,
    exc: BaseException,
    backup: Path | None = None,
    checksum: str | None = None,
) -> Phase2MigrationResult:
    return Phase2MigrationResult(
        state=copy.deepcopy(state),
        status=Phase2InitializationStatus.FAILED,
        reset_performed=False,
        recovery_performed=False,
        active_path=active,
        backup_path=backup,
        legacy_checksum=checksum,
        persistence_enabled=False,
        failure=failure,
        error_kind="permission" if isinstance(_root_cause(exc), PermissionError) else "io",
        error=str(exc),
    )


def _root_cause(exc: BaseException) -> BaseException:
    current = exc
    seen: set[int] = set()
    while getattr(current, "__cause__", None) is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__  # type: ignore[assignment]
    return current


def _normalize_generation_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPhase2StateError("state_generation_id must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as exc:
        raise InvalidPhase2StateError("state_generation_id must be a UUID string") from exc


def _normalize_checksum(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    checksum = value.strip().lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        return None
    return checksum


def _normalize_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        return None
    text = os.fspath(value).strip()
    return text or None


def _normalize_optional_timestamp(value: object) -> int | None:
    if value is None:
        return None
    timestamp = _safe_int(value, -1)
    return timestamp if timestamp >= 0 else None


def _safe_timestamp(value: object, default: int) -> int:
    timestamp = _safe_int(value, default)
    return max(0, timestamp)


def _safe_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return int(default)
