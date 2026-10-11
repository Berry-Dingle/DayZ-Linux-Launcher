"""Bounded, versioned disk cache for the fully built mod autocomplete catalogue."""

from __future__ import annotations

import json
import hashlib
import os
import hmac
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .atomic_json import _fsync_directory_best_effort
from .mod_suggestions import _ModSuggestionIndex
from .mod_suggestions import build_mod_suggestion_index


FORMAT_VERSION = 2
V2_MAGIC = b"DZLL-MOD-SUGGESTIONS-V2\n"
_VERSION_PREFIX = b"DZLL-MOD-SUGGESTIONS-V"
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_LOOKUP_KEYS = 500_000
REFRESH_SECONDS = 30 * 24 * 60 * 60
_TUPLE_FIELDS = ("raw_names", "normalized_names", "compact_names", "tokens")
_TEXT_FIELDS = ("name", "display_name", "normalized_name", "compact_name", "workshop_id")


def suggestion_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(cache_home) / "dzll" / "mod_suggestions_v1.json"


@dataclass(frozen=True)
class CachedSuggestions:
    index: _ModSuggestionIndex
    expired: bool
    age_seconds: float
    format_version: int = FORMAT_VERSION
    created_at: float = 0.0


@dataclass(frozen=True)
class DeferredSuggestions:
    index: _ModSuggestionIndex
    expired: bool
    age_seconds: float
    format_version: int
    created_at: float
    outcome: str
    stages: dict[str, int | str]


def _text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("invalid suggestion text")
    return value


def _positive_count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 10_000_000:
        raise ValueError("invalid suggestion count")
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 4096:
        raise ValueError("invalid suggestion string list")
    return tuple(_text(item) for item in value)


def _lookup(value: Any, entry_count: int) -> dict[str, tuple[int, ...]]:
    if not isinstance(value, dict) or len(value) > MAX_LOOKUP_KEYS:
        raise ValueError("invalid suggestion lookup")
    for key, indices in value.items():
        _text(key)
        if not isinstance(indices, list) or len(indices) > entry_count:
            raise ValueError("invalid suggestion lookup indices")
        if any(type(idx) is not int or idx < 0 or idx >= entry_count for idx in indices):
            raise ValueError("invalid suggestion lookup index")
        value[key] = tuple(indices)
    return value


def _restore_index(value: Any, *, perf_stages: dict[str, int] | None = None) -> _ModSuggestionIndex:
    if not isinstance(value, dict) or set(value) != {"mods", "count", "prefix_map", "exact_compact_map"}:
        raise ValueError("invalid suggestion index")
    mods = value["mods"]
    if not isinstance(mods, list) or len(mods) > MAX_ENTRIES or _positive_count(value["count"]) != len(mods):
        raise ValueError("invalid suggestion entries")
    expected = set(_TUPLE_FIELDS + _TEXT_FIELDS + ("server_count",))
    entry_start_ns = time.monotonic_ns() if perf_stages is not None else 0
    try:
        for entry in mods:
            if not isinstance(entry, dict) or set(entry) != expected:
                raise ValueError("invalid suggestion entry")
            for field in _TEXT_FIELDS:
                _text(entry[field])
            for field in _TUPLE_FIELDS:
                entry[field] = _strings(entry[field])
            _positive_count(entry["server_count"])
    finally:
        if perf_stages is not None:
            perf_stages["entry_restore_ns"] = time.monotonic_ns() - entry_start_ns

    prefix_start_ns = time.monotonic_ns() if perf_stages is not None else 0
    try:
        prefix_map = _lookup(value["prefix_map"], len(mods))
    finally:
        if perf_stages is not None:
            perf_stages["prefix_restore_ns"] = time.monotonic_ns() - prefix_start_ns

    exact_start_ns = time.monotonic_ns() if perf_stages is not None else 0
    try:
        exact_map = _lookup(value["exact_compact_map"], len(mods))
    finally:
        if perf_stages is not None:
            perf_stages["exact_restore_ns"] = time.monotonic_ns() - exact_start_ns

    return _ModSuggestionIndex({
        "mods": tuple(mods),
        "count": len(mods),
        "cache": {},
        "prefix_map": prefix_map,
        "exact_compact_map": exact_map,
    })


def _index_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_suggestion_cache(
    path: Path, *, now: float | None = None,
    perf_report: Callable[[dict[str, int | str]], None] | None = None,
) -> CachedSuggestions | None:
    stages = ({
        "bytes_read": 0,
        "format": "unknown",
        "file_stat_read_ns": 0,
        "json_decode_ns": 0,
        "canonical_json_ns": 0,
        "sha256_ns": 0,
        "integrity_ns": 0,
        "index_restore_ns": 0,
        "entry_restore_ns": 0,
        "prefix_restore_ns": 0,
        "exact_restore_ns": 0,
        "total_ns": 0,
    } if perf_report is not None else None)
    total_start_ns = time.monotonic_ns() if stages is not None else 0
    try:
        read_start_ns = time.monotonic_ns() if stages is not None else 0
        try:
            if path.stat().st_size > MAX_CACHE_BYTES:
                return None
            with path.open("rb") as handle:
                raw = handle.read(MAX_CACHE_BYTES + 1)
        finally:
            if stages is not None:
                stages["file_stat_read_ns"] = time.monotonic_ns() - read_start_ns
        if len(raw) > MAX_CACHE_BYTES:
            return None
        if stages is not None:
            stages["bytes_read"] = len(raw)
        if raw.startswith(V2_MAGIC):
            version = 2
            if stages is not None:
                stages["format"] = "v2"
            digest_start = len(V2_MAGIC)
            payload_start = digest_start + 65
            if len(raw) <= payload_start or raw[payload_start - 1:payload_start] != b"\n":
                return None
            digest = raw[digest_start:payload_start - 1]
            if len(digest) != 64 or any(byte not in b"0123456789abcdef" for byte in digest):
                return None
            hash_start_ns = time.monotonic_ns() if stages is not None else 0
            try:
                valid_digest = hmac.compare_digest(hashlib.sha256(memoryview(raw)[payload_start:]).hexdigest().encode("ascii"), digest)
            finally:
                if stages is not None:
                    stages["sha256_ns"] = time.monotonic_ns() - hash_start_ns
                    stages["integrity_ns"] = stages["sha256_ns"]
            if not valid_digest:
                return None
            decode_start_ns = time.monotonic_ns() if stages is not None else 0
            try:
                document = json.loads(raw[payload_start:])
            finally:
                if stages is not None:
                    stages["json_decode_ns"] = time.monotonic_ns() - decode_start_ns
            if not isinstance(document, dict) or set(document) != {"created_at", "index"}:
                return None
        else:
            if raw.startswith(_VERSION_PREFIX):
                return None
            version = 1
            if stages is not None:
                stages["format"] = "v1"
            decode_start_ns = time.monotonic_ns() if stages is not None else 0
            try:
                document = json.loads(raw)
            finally:
                if stages is not None:
                    stages["json_decode_ns"] = time.monotonic_ns() - decode_start_ns
            if not isinstance(document, dict) or set(document) != {"version", "created_at", "index", "sha256"}:
                return None
            if type(document["version"]) is not int or document["version"] != 1:
                return None
        created_at = document["created_at"]
        if type(created_at) not in (int, float) or not 0 < created_at < 10_000_000_000:
            return None
        age = (time.time() if now is None else now) - created_at
        if age < -86400:
            return None
        if version == 1:
            digest = document["sha256"]
            if not isinstance(digest, str) or len(digest) != 64:
                return None
            integrity_start_ns = time.monotonic_ns() if stages is not None else 0
            canonical_start_ns = time.monotonic_ns() if stages is not None else 0
            try:
                canonical = _index_bytes(document["index"])
            finally:
                if stages is not None:
                    stages["canonical_json_ns"] = time.monotonic_ns() - canonical_start_ns
            hash_start_ns = time.monotonic_ns() if stages is not None else 0
            try:
                valid_digest = hmac.compare_digest(hashlib.sha256(canonical).hexdigest(), digest)
            finally:
                if stages is not None:
                    stages["sha256_ns"] = time.monotonic_ns() - hash_start_ns
                    stages["integrity_ns"] = time.monotonic_ns() - integrity_start_ns
            if not valid_digest:
                return None
        restore_start_ns = time.monotonic_ns() if stages is not None else 0
        try:
            restored = _restore_index(document["index"], perf_stages=stages)
        finally:
            if stages is not None:
                stages["index_restore_ns"] = time.monotonic_ns() - restore_start_ns
        return CachedSuggestions(restored, age >= REFRESH_SECONDS, max(0.0, age), version, created_at)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, MemoryError):
        return None
    finally:
        if stages is not None:
            stages["total_ns"] = time.monotonic_ns() - total_start_ns
            try:
                perf_report(stages)
            except Exception:
                pass


def prepare_deferred_suggestions(
    path: Path, mods_json_values: tuple[str, ...], *, measure: bool = False,
) -> DeferredSuggestions:
    """Load or rebuild plain autocomplete data on a worker without publishing files."""
    worker_start_ns = time.monotonic_ns() if measure else 0
    stages: dict[str, int | str] = {}
    cached = load_suggestion_cache(
        path, perf_report=(stages.update if measure else None),
    )
    if cached is not None:
        if measure:
            stages["fallback_build_ns"] = 0
            stages["worker_total_ns"] = time.monotonic_ns() - worker_start_ns
        return DeferredSuggestions(
            cached.index, cached.expired, cached.age_seconds,
            cached.format_version, cached.created_at, "cache_hit", stages,
        )

    build_start_ns = time.monotonic_ns() if measure else 0
    index = build_mod_suggestion_index(mods_json_values)
    if measure:
        stages["fallback_build_ns"] = time.monotonic_ns() - build_start_ns
        stages["worker_total_ns"] = time.monotonic_ns() - worker_start_ns
    return DeferredSuggestions(
        index, False, 0.0, 0, time.time(), "cache_miss_rebuilt", stages,
    )


def save_suggestion_cache(path: Path, index: dict[str, Any], *, now: float | None = None) -> None:
    index_data = {
        "mods": index["mods"],
        "count": index["count"],
        "prefix_map": index["prefix_map"],
        "exact_compact_map": index["exact_compact_map"],
    }
    payload = json.dumps({"created_at": time.time() if now is None else now, "index": index_data},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = V2_MAGIC + hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n" + payload
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError("suggestion cache exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            if handle.write(raw) != len(raw):
                raise OSError("short suggestion cache write")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory_best_effort(path.parent)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_staged_suggestion_migration(path: Path, index: dict[str, Any], created_at: float) -> Path:
    """Serialize an already loaded v1 index on a plain-data worker."""
    stage = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        save_suggestion_cache(stage, index, now=created_at)
        return stage
    except Exception:
        stage.unlink(missing_ok=True)
        raise


def build_staged_suggestion_cache(path: Path, mods_json_values: tuple[str, ...]) -> tuple[_ModSuggestionIndex, Path]:
    """Worker-only plain-data build; caller validates generation before publication."""
    stage = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        index = build_mod_suggestion_index(mods_json_values)
        save_suggestion_cache(stage, index)
        return index, stage
    except Exception:
        stage.unlink(missing_ok=True)
        raise
