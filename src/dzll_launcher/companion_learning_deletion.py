"""Staged deletion of Server Companion learning data.

A deletion is never applied to the live database while DZLL runs: the
authoritative schema-4 runtime owns it for the whole session (writer lock,
per-server rebuild queues).  The user's request is staged as a small private
file and applied at the next startup, before the learning runtime opens and
under the same writer lock, exactly like a pending database import.

Scopes:

* ``all``: the active database is replaced by a fresh, empty schema-4 state.
* ``servers``: each named server is removed from ``servers``, from the
  embedded ``schema3_snapshot`` and from the root ``migration_audit``, so the
  server key no longer occurs anywhere in the database.

In both cases ``.last-known-good`` (the runtime's automatic recovery copy) is
overwritten with the same new bytes, so startup recovery can never bring
deleted data back.  No other file (user or DZLL backups) is touched, and no
backup is created.  A deletion is complete only when both files are verified;
otherwise the request stays staged and is retried at the next startup
(re-applying is idempotent).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .companion_learning_transfer import (
    _atomic_write_verified,
    _canonical_metadata_bytes,
    _fsync_directory,
    _reject_nonfinite,
    _sha256,
    _strict_object,
    _unlink_quiet,
    pending_import_paths,
)
from .companion_restart_phase2_schema4 import (
    SCHEMA4_ROOT_VERSION,
    Schema4Error,
    _migration_totals,
    canonical_json_bytes,
    deserialize_schema4_bytes,
    migrate_schema3_state,
    schema_version_from_bytes,
    serialize_schema4_state,
)
from .companion_restart_phase2_schema4_runtime import Schema4WriterLock
from .companion_restart_phase2_storage import new_phase2_state


PENDING_DELETION_FORMAT_VERSION = 1
PENDING_DELETION_NAME = "companion_restart_learning_phase2.pending-delete.json"
PENDING_DELETION_FIELDS = frozenset({
    "pending_format_version", "scope", "server_keys", "staged_at", "request_sha256",
})
SCOPE_ALL = "all"
SCOPE_SERVERS = "servers"
LAST_KNOWN_GOOD_SUFFIX = ".last-known-good"


class PendingDeletionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingLearningDeletion:
    path: Path
    scope: str
    server_keys: tuple[str, ...]
    staged_at: str


@dataclass(frozen=True)
class StartupDeletionResult:
    status: str
    scope: str | None = None
    server_keys: tuple[str, ...] = ()
    error: str | None = None

    @property
    def notice_title(self) -> str | None:
        if self.status == "applied":
            return "Server Companion learning data deleted"
        if self.status in {"failed", "pending_invalid", "conflict"}:
            return "Server Companion learning data deletion not completed"
        return None

    @property
    def notice_body(self) -> str | None:
        if self.status == "applied":
            if self.scope == SCOPE_ALL:
                return (
                    "All Server Companion restart-learning data was permanently deleted.\n"
                    "Learning starts again from the beginning."
                )
            count = len(self.server_keys)
            servers = "1 server" if count == 1 else f"{count} servers"
            return (
                f"The restart-learning data for {servers} was permanently deleted.\n"
                "Learning data for all other servers was kept."
            )
        if self.status == "conflict":
            return (
                "A pending learning database replacement and a pending learning data "
                "deletion were both found, so neither was applied.\n"
                "Cancel the pending replacement in Settings and restart DZLL to "
                "complete the deletion."
            )
        if self.status == "pending_invalid":
            return (
                "The pending deletion request could not be read, so nothing was deleted.\n"
                f"{self.error or ''}"
            ).strip()
        if self.status == "failed":
            return (
                "The deletion could not be completed.\n"
                "DZLL will try again at the next start.\n"
                f"{self.error or ''}"
            ).strip()
        return None


def pending_deletion_path(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir) / PENDING_DELETION_NAME


def pending_deletion_exists(config_dir: str | os.PathLike[str]) -> bool:
    return pending_deletion_path(config_dir).exists()


def pending_import_exists(config_dir: str | os.PathLike[str]) -> bool:
    payload, metadata = pending_import_paths(config_dir)
    return payload.exists() or metadata.exists()


def stage_pending_deletion(
    *,
    config_dir: str | os.PathLike[str],
    scope: str,
    server_keys: Iterable[str] = (),
    now: datetime | None = None,
) -> PendingLearningDeletion:
    """Stage (or merge into) the deletion request; never touches the database."""

    try:
        keys = _normalized_keys(scope, server_keys)
    except ValueError as exc:
        raise PendingDeletionError(str(exc)) from exc
    if pending_import_exists(config_dir):
        raise PendingDeletionError(
            "A learning database replacement is pending. Cancel it in Settings "
            "before deleting learning data."
        )
    existing = read_pending_deletion(config_dir)
    if existing is not None:
        if existing.scope == SCOPE_ALL or scope == SCOPE_ALL:
            scope, keys = SCOPE_ALL, ()
        else:
            keys = tuple(sorted(set(existing.server_keys) | set(keys)))
    staged_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    body = {
        "pending_format_version": PENDING_DELETION_FORMAT_VERSION,
        "scope": scope,
        "server_keys": list(keys),
        "staged_at": staged_at,
    }
    request = dict(body, request_sha256=_sha256(canonical_json_bytes(body)))
    path = pending_deletion_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_verified(path, _canonical_metadata_bytes(request))
    staged = read_pending_deletion(config_dir)
    if staged is None or staged.scope != scope or staged.server_keys != keys:
        raise PendingDeletionError("The staged deletion request failed verification.")
    return staged


def read_pending_deletion(
    config_dir: str | os.PathLike[str],
) -> PendingLearningDeletion | None:
    """Return the staged request, ``None`` when absent; raise when invalid."""

    path = pending_deletion_path(config_dir)
    if not path.exists():
        return None
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except Exception as exc:
        raise PendingDeletionError(f"The pending deletion request is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != PENDING_DELETION_FIELDS:
        raise PendingDeletionError("The pending deletion request fields are incomplete or unknown.")
    if value.get("pending_format_version") != PENDING_DELETION_FORMAT_VERSION or type(
        value.get("pending_format_version")
    ) is not int:
        raise PendingDeletionError("The pending deletion request version is unsupported.")
    body = {name: value[name] for name in PENDING_DELETION_FIELDS - {"request_sha256"}}
    if value.get("request_sha256") != _sha256(canonical_json_bytes(body)):
        raise PendingDeletionError("The pending deletion request failed SHA-256 verification.")
    scope = value.get("scope")
    raw_keys = value.get("server_keys")
    if not isinstance(raw_keys, list) or not all(isinstance(item, str) for item in raw_keys):
        raise PendingDeletionError("The pending deletion request server keys are invalid.")
    try:
        keys = _normalized_keys(scope, raw_keys)
    except ValueError as exc:
        raise PendingDeletionError(str(exc)) from exc
    if list(keys) != raw_keys:
        raise PendingDeletionError("The pending deletion request server keys are not canonical.")
    staged_at = value.get("staged_at")
    try:
        if not isinstance(staged_at, str):
            raise ValueError("not a string")
        parsed = datetime.fromisoformat(staged_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
    except ValueError as exc:
        raise PendingDeletionError("The pending deletion request timestamp is invalid.") from exc
    return PendingLearningDeletion(path, str(scope), keys, staged_at)


def cancel_pending_deletion(config_dir: str | os.PathLike[str]) -> bool:
    path = pending_deletion_path(config_dir)
    existed = path.exists()
    _unlink_quiet(path)
    _fsync_directory(Path(config_dir))
    return existed


def pending_learning_changes_conflict(config_dir: str | os.PathLike[str]) -> bool:
    """A staged import and a staged deletion must never both be applied."""

    return pending_deletion_exists(config_dir) and pending_import_exists(config_dir)


def apply_pending_deletion_at_startup(
    *,
    config_dir: str | os.PathLike[str],
    live_path: str | os.PathLike[str],
    now: float | None = None,
) -> StartupDeletionResult:
    """Apply a staged deletion before the learning runtime opens."""

    if not pending_deletion_exists(config_dir):
        return StartupDeletionResult("none")
    if pending_import_exists(config_dir):
        return StartupDeletionResult("conflict")
    try:
        request = read_pending_deletion(config_dir)
    except PendingDeletionError as exc:
        # An unreadable request is never guessed at.  It is removed so it
        # cannot block every later start; the user can delete again.
        try:
            cancel_pending_deletion(config_dir)
        except OSError:
            pass
        return StartupDeletionResult("pending_invalid", error=str(exc))
    assert request is not None
    live = Path(live_path)
    lkg = live.with_name(live.name + LAST_KNOWN_GOOD_SUFFIX)
    timestamp = float(datetime.now(timezone.utc).timestamp() if now is None else now)
    try:
        with Schema4WriterLock(live):
            _apply_locked(request, live=live, lkg=lkg, now=timestamp)
    except Exception as exc:
        return StartupDeletionResult(
            "failed",
            scope=request.scope,
            server_keys=request.server_keys,
            error=f"{type(exc).__name__}: {exc}",
        )
    cancel_pending_deletion(config_dir)
    return StartupDeletionResult(
        "applied", scope=request.scope, server_keys=request.server_keys
    )


def fresh_schema4_bytes(*, now: float) -> bytes:
    """A strictly valid, empty schema-4 database (as fresh startup creates)."""

    schema3 = new_phase2_state(now=now)
    source = canonical_json_bytes(schema3)
    state = migrate_schema3_state(schema3, source_bytes=source, source_mtime_ns=0)
    payload = serialize_schema4_state(state)
    loaded = deserialize_schema4_bytes(payload, quarantine_invalid_servers=False)
    if not loaded.report.valid:
        raise PendingDeletionError("The fresh learning database failed strict validation.")
    return payload


def remove_servers_from_schema4_bytes(raw: bytes, server_keys: Iterable[str]) -> bytes:
    """Remove servers and scrub every reference to their keys (pure)."""

    keys = tuple(server_keys)
    if schema_version_from_bytes(raw) != SCHEMA4_ROOT_VERSION:
        raise PendingDeletionError("The learning database is not schema 4.")
    loaded = deserialize_schema4_bytes(raw, quarantine_invalid_servers=True)
    if not loaded.report.root_valid:
        raise PendingDeletionError("The learning database root is invalid.")
    state = loaded.state
    for key in keys:
        state.get("servers", {}).pop(key, None)
        snapshot = state.get("schema3_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("servers"), dict):
            snapshot["servers"].pop(key, None)
    audit = state.get("migration_audit")
    if isinstance(audit, dict):
        _scrub_migration_audit(audit, set(keys))
    payload = serialize_schema4_state(state)
    _verify_persistable(payload)
    text = payload.decode("utf-8")
    for key in keys:
        if server_key_referenced(text, key):
            raise PendingDeletionError(
                f"Server {key} is still referenced in the learning database after deletion."
            )
    return payload


def server_key_referenced(text: str, key: str) -> bool:
    """True when ``key`` occurs as a whole server identity in ``text``.

    A longer identity that merely contains it (``11.2.3.4:23020`` for
    ``1.2.3.4:2302``) is not a reference.
    """

    pattern = r"(?<![0-9A-Za-z.\-])" + re.escape(key) + r"(?![0-9])"
    return re.search(pattern, text) is not None


def _scrub_migration_audit(audit: dict, keys: set[str]) -> None:
    servers = audit.get("servers")
    if isinstance(servers, list):
        kept = [
            item
            for item in servers
            if not (isinstance(item, Mapping) and item.get("server_key") in keys)
        ]
        if len(kept) != len(servers):
            audit["servers"] = kept
            if isinstance(audit.get("global_totals"), dict):
                audit["global_totals"] = _migration_totals(
                    [item for item in kept if isinstance(item, Mapping)]
                )
    if audit.get("selected_server_key") in keys:
        audit["selected_server_key"] = None


def _apply_locked(request: PendingLearningDeletion, *, live: Path, lkg: Path, now: float) -> None:
    if request.scope == SCOPE_ALL:
        new = fresh_schema4_bytes(now=now)
        _install_pair(live, lkg, new, prior=live.read_bytes() if live.exists() else None)
        return
    if live.exists():
        prior = live.read_bytes()
        new = remove_servers_from_schema4_bytes(prior, request.server_keys)
        _install_pair(live, lkg, new, prior=prior)
        return
    # No active database: nothing to remove from it, but the recovery copy
    # must not keep the deleted servers either.
    if lkg.exists():
        prior_lkg = lkg.read_bytes()
        new = remove_servers_from_schema4_bytes(prior_lkg, request.server_keys)
        if new != prior_lkg:
            _atomic_write_verified(lkg, new)


def _install_pair(live: Path, lkg: Path, payload: bytes, *, prior: bytes | None) -> None:
    """Install ``payload`` as the active database, then as last-known-good."""

    if prior != payload:
        try:
            _atomic_write_verified(live, payload)
        except Exception:
            if prior is not None:
                try:
                    _atomic_write_verified(live, prior)
                except Exception as restore_exc:
                    raise PendingDeletionError(
                        f"Installing the new database failed and the previous one could not be restored: {restore_exc}"
                    ) from restore_exc
            raise
    # The active file now holds the post-deletion state.  If the recovery
    # copy cannot be written the request stays staged and is retried.
    if not lkg.exists() or lkg.read_bytes() != payload:
        _atomic_write_verified(lkg, payload)
    if live.read_bytes() != payload or lkg.read_bytes() != payload:
        raise PendingDeletionError("The deleted learning database failed byte verification.")


def _verify_persistable(payload: bytes) -> None:
    try:
        loaded = deserialize_schema4_bytes(payload, quarantine_invalid_servers=False)
    except Schema4Error as exc:
        raise PendingDeletionError(f"The new learning database is invalid: {exc}") from exc
    report = loaded.report
    if not report.root_valid or any(item.code != "server_quarantined" for item in report.issues):
        detail = "; ".join(
            f"{item.code}: {item.detail}" for item in report.issues if item.code != "server_quarantined"
        )
        raise PendingDeletionError(f"The new learning database failed validation. {detail}".strip())


def _normalized_keys(scope: object, server_keys: Iterable[str]) -> tuple[str, ...]:
    if scope == SCOPE_ALL:
        keys = tuple(server_keys)
        if keys:
            raise ValueError("A delete-all request must not name servers.")
        return ()
    if scope != SCOPE_SERVERS:
        raise ValueError(f"Unknown deletion scope {scope!r}.")
    keys = tuple(sorted({str(item).strip() for item in server_keys}))
    if not keys:
        raise ValueError("A server deletion request must name at least one server.")
    for key in keys:
        host, _, port = key.rpartition(":")
        if not host.strip() or not port.isdigit() or not (1 <= int(port) <= 65535):
            raise ValueError(f"Invalid server identity {key!r}.")
    return keys
