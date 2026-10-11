"""Bounded persistent preview/search data for canonical server endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .atomic_json import _fsync_directory_best_effort


SCHEMA_VERSION = 1
NORMALIZATION_VERSION = 1  # Bump for preview, name cleaning or search rules.
MAGIC = b"DZLL-SERVER-MODS-V1\n"
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_TEXT = 8192
MAX_SET_ITEMS = 4096
FIELDS = ("ids", "normalized_names", "compact_names", "tokens")


def cache_path() -> Path:
    home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(home) / "dzll" / "server_mods_v1.json"


def fingerprint(mods_json: str) -> str:
    return hashlib.sha256(mods_json.encode("utf-8", "surrogatepass")).hexdigest()


def _hex_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _restore(record: Any, digest: str) -> tuple[int, str, dict[str, frozenset[str]]] | None:
    if not isinstance(record, list) or len(record) != 7 or record[0] != digest:
        return None
    count, preview = record[1:3]
    if type(count) is not int or count < 0 or count > 1_000_000 or not isinstance(preview, str) or len(preview) > MAX_TEXT:
        return None
    fields = record[3:]
    for values in fields:
        if not isinstance(values, list) or len(values) > MAX_SET_ITEMS:
            return None
        if any(not isinstance(value, str) or len(value) > MAX_TEXT for value in values):
            return None
    ids, normalized, compact, tokens = (frozenset(values) for values in fields)
    return count, preview, {
        "ids": ids, "names": compact, "normalized_names": normalized,
        "compact_names": compact, "tokens": tokens,
    }


def lookup(records: dict[str, Any], endpoint: str, digest: str):
    record = records.get(endpoint)
    if isinstance(record, tuple) and len(record) == 4 and record[0] == digest:
        return record[1], record[2], record[3]
    return _restore(record, digest)


def load(path: Path, *, perf_report: Callable[[dict[str, int]], None] | None = None) -> dict[str, Any]:
    stages = {"read_ns": 0, "integrity_ns": 0, "decode_ns": 0, "restore_ns": 0,
              "total_ns": 0, "file_bytes": 0, "entries": 0} if perf_report else None
    start = time.monotonic_ns() if stages is not None else 0
    try:
        tick = time.monotonic_ns() if stages is not None else 0
        try:
            if path.stat().st_size > MAX_BYTES:
                return {}
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        finally:
            if stages is not None:
                stages["read_ns"] = time.monotonic_ns() - tick
        if len(raw) > MAX_BYTES:
            return {}
        if stages is not None:
            stages["file_bytes"] = len(raw)
        if not raw.startswith(MAGIC):
            return {}
        offset = len(MAGIC)
        expected = raw[offset:offset + 64]
        if len(expected) != 64 or raw[offset + 64:offset + 65] != b"\n":
            return {}
        payload = memoryview(raw)[offset + 65:]
        tick = time.monotonic_ns() if stages is not None else 0
        valid = hmac.compare_digest(hashlib.sha256(payload).hexdigest().encode("ascii"), expected)
        if stages is not None:
            stages["integrity_ns"] = time.monotonic_ns() - tick
        if not valid:
            return {}
        tick = time.monotonic_ns() if stages is not None else 0
        document = json.loads(payload.tobytes())
        if stages is not None:
            stages["decode_ns"] = time.monotonic_ns() - tick
        tick = time.monotonic_ns() if stages is not None else 0
        if not isinstance(document, dict) or set(document) != {"schema_version", "normalization_version", "entries"}:
            return {}
        if (type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION or
                type(document["normalization_version"]) is not int or document["normalization_version"] != NORMALIZATION_VERSION):
            return {}
        records = document["entries"]
        if not isinstance(records, dict) or len(records) > MAX_ENTRIES:
            return {}
        if any(not isinstance(key, str) or len(key) > 64 or not isinstance(value, list) or len(value) != 7 or not _hex_digest(value[0]) for key, value in records.items()):
            return {}
        if stages is not None:
            stages["restore_ns"] = time.monotonic_ns() - tick
            stages["entries"] = len(records)
        return records
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, MemoryError):
        return {}
    finally:
        if stages is not None:
            stages["total_ns"] = time.monotonic_ns() - start
            try:
                perf_report(stages)
            except Exception:
                pass


def _serializable(records: dict[str, Any]) -> dict[str, list[Any]]:
    if len(records) > MAX_ENTRIES:
        raise ValueError("too many server mod cache entries")
    result = {}
    for endpoint, record in records.items():
        if not isinstance(endpoint, str) or len(endpoint) > 64:
            raise ValueError("invalid endpoint key")
        if isinstance(record, tuple) and len(record) == 4:
            digest, count, preview, index = record
            record = [digest, count, preview, *(sorted(index[field]) for field in FIELDS)]
        if not isinstance(record, list) or len(record) != 7 or not _hex_digest(record[0]) or _restore(record, record[0]) is None:
            raise ValueError("invalid server mod cache record")
        result[endpoint] = record
    return result


def stage(path: Path, records: dict[str, Any], *, perf_report: Callable[[dict[str, int]], None] | None = None) -> tuple[Path, dict[str, int]]:
    stages = {"serialize_ns": 0, "write_ns": 0, "file_bytes": 0, "entries": len(records)}
    temporary = None
    fd = -1
    try:
        tick = time.monotonic_ns() if perf_report else 0
        payload = json.dumps({"schema_version": SCHEMA_VERSION, "normalization_version": NORMALIZATION_VERSION,
                              "entries": _serializable(records)},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw = MAGIC + hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n" + payload
        if len(raw) > MAX_BYTES:
            raise ValueError("server mod cache exceeds size limit")
        if perf_report:
            stages["serialize_ns"] = time.monotonic_ns() - tick
        stages["file_bytes"] = len(raw)
        tick = time.monotonic_ns() if perf_report else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            if handle.write(raw) != len(raw):
                raise OSError("short server mod cache write")
            handle.flush()
            os.fsync(handle.fileno())
        if perf_report:
            stages["write_ns"] = time.monotonic_ns() - tick
        if perf_report:
            perf_report(stages)
        return temporary, stages
    except Exception:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def publish(staged: Path, path: Path) -> None:
    os.replace(staged, path)
    _fsync_directory_best_effort(path.parent)
