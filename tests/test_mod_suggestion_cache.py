from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import mod_suggestion_cache, mod_suggestions, window
from dzll_launcher.window import DZLLWindow


PAYLOADS = (
    '[{"steamWorkshopId":"1646187754","name":"@CodeLock"},'
    '{"steamWorkshopId":"1559212036","name":"@CF"},'
    '{"name":"Some Other Mod"}]',
    '[{"steamWorkshopId":"1646187754","name":"Code Lock"},'
    '{"steamWorkshopId":"1646187754","name":"Code Lock"},'
    '{"name":"Some Other Mod"}]',
)


def _restore_v1_reference(value):
    """Frozen copy of the pre-optimisation version-1 reconstruction contract."""
    if not isinstance(value, dict) or set(value) != {"mods", "count", "prefix_map", "exact_compact_map"}:
        raise ValueError("invalid suggestion index")
    mods = value["mods"]
    if not isinstance(mods, list) or len(mods) > mod_suggestion_cache.MAX_ENTRIES or mod_suggestion_cache._positive_count(value["count"]) != len(mods):
        raise ValueError("invalid suggestion entries")
    restored = []
    expected = set(mod_suggestion_cache._TUPLE_FIELDS + mod_suggestion_cache._TEXT_FIELDS + ("server_count",))
    for entry in mods:
        if not isinstance(entry, dict) or set(entry) != expected:
            raise ValueError("invalid suggestion entry")
        restored.append({
            **{field: mod_suggestion_cache._text(entry[field]) for field in mod_suggestion_cache._TEXT_FIELDS},
            **{field: mod_suggestion_cache._strings(entry[field]) for field in mod_suggestion_cache._TUPLE_FIELDS},
            "server_count": mod_suggestion_cache._positive_count(entry["server_count"]),
        })

    def old_lookup(source):
        if not isinstance(source, dict) or len(source) > mod_suggestion_cache.MAX_LOOKUP_KEYS:
            raise ValueError("invalid suggestion lookup")
        result = {}
        for key, indices in source.items():
            mod_suggestion_cache._text(key)
            if not isinstance(indices, list) or len(indices) > len(restored):
                raise ValueError("invalid suggestion lookup indices")
            if any(type(idx) is not int or idx < 0 or idx >= len(restored) for idx in indices):
                raise ValueError("invalid suggestion lookup index")
            result[key] = tuple(indices)
        return result

    return mod_suggestions._ModSuggestionIndex({
        "mods": tuple(restored), "count": len(restored), "cache": {},
        "prefix_map": old_lookup(value["prefix_map"]),
        "exact_compact_map": old_lookup(value["exact_compact_map"]),
    })


@pytest.mark.parametrize("payloads", [
    (), PAYLOADS,
    ('[{"steamWorkshopId":"1646187754","name":"CodeLock"},'
     '{"steamWorkshopId":"1234567890123456789012345678901234567890","name":"LongPrefixName"},'
     '{"name":"Alpha Beta CamelCase"}]',
     '[{"steamWorkshopId":"1646187754","name":"Code Lock"},'
     '{"name":"Alpha Beta CamelCase"}]'),
])
def test_in_place_restore_matches_original_version_one_index_and_queries(payloads):
    built = mod_suggestions.build_mod_suggestion_index(payloads)
    body = json.loads(json.dumps({
        key: built[key] for key in ("mods", "count", "prefix_map", "exact_compact_map")
    }))
    original = _restore_v1_reference(copy.deepcopy(body))
    decoded = copy.deepcopy(body)
    first_entry = decoded["mods"][0] if decoded["mods"] else None
    prefix_map = decoded["prefix_map"]
    exact_map = decoded["exact_compact_map"]
    restored = mod_suggestion_cache._restore_index(decoded)
    assert restored == original
    assert type(restored["mods"]) is tuple
    assert restored["cache"] == {} and restored["cache"] is not original["cache"]
    assert restored["prefix_map"] is prefix_map
    assert restored["exact_compact_map"] is exact_map
    if first_entry is not None:
        assert restored["mods"][0] is first_entry
        for field in mod_suggestion_cache._TUPLE_FIELDS:
            assert type(first_entry[field]) is tuple
    for query in ("cl", "codelock", "1646187754", "cf", "community", "alpha", "camel", "longprefix", "1234567890123456789012345678901234567890", "alpha beta", "missing words"):
        for _ in range(2):
            assert mod_suggestions.suggest_mods(restored, query) == mod_suggestions.suggest_mods(original, query)
    assert restored["cache"] == original["cache"]


@pytest.mark.parametrize("corrupt", [
    lambda body: body.update(count=body["count"] + 1),
    lambda body: body["mods"][0].update(name=4),
    lambda body: body["mods"][0].update(tokens={"bad": "type"}),
    lambda body: body["mods"][0].pop("workshop_id"),
    lambda body: body["mods"][0].update(server_count=True),
    lambda body: body["prefix_map"].update(bad=[body["count"]]),
    lambda body: body["exact_compact_map"].update(bad=[-1]),
    lambda body: body["prefix_map"].update(bad=[True]),
])
def test_malformed_bodies_match_original_rejection_and_load_as_cache_miss(tmp_path, corrupt):
    built = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    body = json.loads(json.dumps({
        key: built[key] for key in ("mods", "count", "prefix_map", "exact_compact_map")
    }))
    corrupt(body)
    with pytest.raises((ValueError, TypeError)):
        _restore_v1_reference(copy.deepcopy(body))
    with pytest.raises((ValueError, TypeError)):
        mod_suggestion_cache._restore_index(copy.deepcopy(body))
    path = tmp_path / "mod_suggestions_v1.json"
    document = {
        "version": 1, "created_at": 1_000_000_000, "index": body,
        "sha256": hashlib.sha256(mod_suggestion_cache._index_bytes(body)).hexdigest(),
    }
    path.write_text(json.dumps(document))
    assert mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001) is None


def test_cache_load_stage_report_is_aggregate_and_opt_in(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    mod_suggestion_cache.save_suggestion_cache(path, mod_suggestions.build_mod_suggestion_index(PAYLOADS))
    reports = []
    assert mod_suggestion_cache.load_suggestion_cache(path, perf_report=reports.append) is not None
    assert len(reports) == 1
    assert reports[0]["bytes_read"] == path.stat().st_size
    assert reports[0]["format"] == "v2"
    assert reports[0]["canonical_json_ns"] == 0
    assert reports[0]["integrity_ns"] == reports[0]["sha256_ns"]
    assert reports[0]["index_restore_ns"] >= sum(reports[0][stage] for stage in (
        "entry_restore_ns", "prefix_restore_ns", "exact_restore_ns",
    ))
    assert reports[0]["total_ns"] >= sum(reports[0][stage] for stage in (
        "file_stat_read_ns", "json_decode_ns", "canonical_json_ns", "sha256_ns",
        "entry_restore_ns", "prefix_restore_ns", "exact_restore_ns",
    ))
    assert all(reports[0][stage] >= 0 for stage in reports[0] if stage.endswith("_ns"))
    assert mod_suggestion_cache.load_suggestion_cache(tmp_path / "missing.json", perf_report=reports.append) is None
    assert len(reports) == 2 and reports[1]["total_ns"] > 0

    monkeypatch.setattr(mod_suggestion_cache.time, "monotonic_ns", lambda: pytest.fail("disabled timer called"))
    assert mod_suggestion_cache.load_suggestion_cache(path) is not None


def test_cache_uses_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert mod_suggestion_cache.suggestion_cache_path() == tmp_path / "dzll" / "mod_suggestions_v1.json"


def test_cold_warm_and_expired_cache_preserve_complete_suggestions(tmp_path):
    path = tmp_path / "cache" / "mod_suggestions_v1.json"
    assert mod_suggestion_cache.load_suggestion_cache(path) is None
    original = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    mod_suggestion_cache.save_suggestion_cache(path, original, now=1_000_000_000)

    warm = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001)
    assert warm is not None and warm.expired is False
    assert warm.index == original
    assert warm.index["cache"] is not original["cache"]
    for query in ("cl", "codelock", "1646187754", "cf", "community", "some other", "lock"):
        assert mod_suggestions.suggest_mods(warm.index, query) == mod_suggestions.suggest_mods(original, query)
    assert warm.index["prefix_map"] == original["prefix_map"]
    assert warm.index["exact_compact_map"] == original["exact_compact_map"]

    expired = mod_suggestion_cache.load_suggestion_cache(
        path, now=1_000_000_000 + mod_suggestion_cache.REFRESH_SECONDS,
    )
    assert expired is not None and expired.expired is True
    assert mod_suggestions.suggest_mods(expired.index, "cl") == mod_suggestions.suggest_mods(original, "cl")


@pytest.mark.parametrize("document", [
    b"not json",
    json.dumps({"version": 99, "created_at": 1_000_000_000, "index": {}}).encode(),
    json.dumps({"version": 1, "created_at": 1_000_000_000,
                "index": {"mods": [], "count": 1, "prefix_map": {}, "exact_compact_map": {}}}).encode(),
    json.dumps({"version": 1, "created_at": 1_000_000_000,
                "index": {"mods": [], "count": 0, "prefix_map": {"bad": [0]}, "exact_compact_map": {}}}).encode(),
])
def test_corrupt_or_incompatible_cache_is_a_safe_miss(tmp_path, document):
    path = tmp_path / "mod_suggestions_v1.json"
    path.write_bytes(document)
    assert mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001) is None


def test_oversized_cache_is_rejected_before_json_decode(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    path.write_bytes(b"x" * 20)
    monkeypatch.setattr(mod_suggestion_cache, "MAX_CACHE_BYTES", 10)
    monkeypatch.setattr(mod_suggestion_cache.json, "loads", lambda *_: pytest.fail("oversized JSON decoded"))
    assert mod_suggestion_cache.load_suggestion_cache(path) is None


def test_in_bounds_tampering_is_rejected_by_checksum(tmp_path):
    path = tmp_path / "mod_suggestions_v1.json"
    mod_suggestion_cache.save_suggestion_cache(path, mod_suggestions.build_mod_suggestion_index(PAYLOADS))
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"CodeLock", b"Tampered"))
    assert mod_suggestion_cache.load_suggestion_cache(path) is None


def test_empty_index_round_trips_with_empty_query_cache(tmp_path):
    path = tmp_path / "mod_suggestions_v1.json"
    original = mod_suggestions.build_mod_suggestion_index(())
    mod_suggestion_cache.save_suggestion_cache(path, original, now=1_000_000_000)
    loaded = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001)
    assert loaded is not None and loaded.index == original
    assert mod_suggestions.suggest_mods(loaded.index, "codelock") == ()


def _write_v1_cache(path, index, created_at=1_000_000_000):
    body = {key: index[key] for key in ("mods", "count", "prefix_map", "exact_compact_map")}
    path.write_text(json.dumps({
        "version": 1, "created_at": created_at, "index": body,
        "sha256": hashlib.sha256(mod_suggestion_cache._index_bytes(body)).hexdigest(),
    }))


def test_v2_digest_covers_exact_payload_before_decoding(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    original = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    mod_suggestion_cache.save_suggestion_cache(path, original, now=1_000_000_000)
    raw = path.read_bytes()
    assert raw.startswith(mod_suggestion_cache.V2_MAGIC)
    loaded = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001)
    assert loaded is not None and loaded.format_version == 2 and loaded.index == original
    assert loaded.created_at == 1_000_000_000
    for query in ("cl", "cf", "1646187754", "codelock", "some", "other mod", "missing"):
        for _ in range(2):
            assert mod_suggestions.suggest_mods(loaded.index, query) == mod_suggestions.suggest_mods(original, query)
    tampered = raw[:-1] + (b" " if raw[-1:] != b" " else b"x")
    path.write_bytes(tampered)
    monkeypatch.setattr(mod_suggestion_cache.json, "loads", lambda *_: pytest.fail("tampered payload decoded"))
    assert mod_suggestion_cache.load_suggestion_cache(path) is None


@pytest.mark.parametrize("bad", [
    lambda raw: raw[:-1],
    lambda raw: raw[:len(mod_suggestion_cache.V2_MAGIC)] + b"x" + raw[len(mod_suggestion_cache.V2_MAGIC) + 1:],
    lambda raw: raw.replace(b"CodeLock", b"CodeLost"),
    lambda raw: b"DZLL-MOD-SUGGESTIONS-V3\n" + raw[len(mod_suggestion_cache.V2_MAGIC):],
])
def test_v2_corrupt_truncated_tampered_and_future_files_are_safe_misses(tmp_path, bad):
    path = tmp_path / "cache"
    mod_suggestion_cache.save_suggestion_cache(path, mod_suggestions.build_mod_suggestion_index(PAYLOADS))
    path.write_bytes(bad(path.read_bytes()))
    assert mod_suggestion_cache.load_suggestion_cache(path) is None


def test_valid_v1_cache_loads_and_staged_migration_preserves_age_and_suggestions(tmp_path):
    path = tmp_path / "cache"
    original = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, original)
    before = path.read_bytes()
    reports = []
    loaded = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001, perf_report=reports.append)
    assert loaded is not None and loaded.format_version == 1 and loaded.index == original
    assert reports[0]["format"] == "v1" and reports[0]["canonical_json_ns"] > 0
    staged = mod_suggestion_cache.build_staged_suggestion_migration(path, loaded.index, loaded.created_at)
    assert path.read_bytes() == before
    assert mod_suggestion_cache.load_suggestion_cache(staged, now=1_000_000_001).index == original
    staged.replace(path)
    after = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001)
    assert after is not None and after.format_version == 2 and after.index == original
    assert after.age_seconds == loaded.age_seconds


def test_v1_expired_and_empty_indexes_migrate_without_rebuild(tmp_path):
    for name, index in (("full", mod_suggestions.build_mod_suggestion_index(PAYLOADS)),
                        ("empty", mod_suggestions.build_mod_suggestion_index(()))):
        path = tmp_path / name
        _write_v1_cache(path, index)
        now = 1_000_000_000 + mod_suggestion_cache.REFRESH_SECONDS
        loaded = mod_suggestion_cache.load_suggestion_cache(path, now=now)
        assert loaded is not None and loaded.expired
        stage = mod_suggestion_cache.build_staged_suggestion_migration(path, loaded.index, loaded.created_at)
        stage.replace(path)
        migrated = mod_suggestion_cache.load_suggestion_cache(path, now=now)
        assert migrated is not None and migrated.expired and migrated.index == index


def test_v2_atomic_write_failure_keeps_previous_cache(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    original = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, original)
    before = path.read_bytes()
    monkeypatch.setattr(mod_suggestion_cache.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        mod_suggestion_cache.save_suggestion_cache(path, original)
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_v2_valid_checksum_still_validates_lookup_indices(tmp_path):
    path = tmp_path / "cache"
    mod_suggestion_cache.save_suggestion_cache(path, mod_suggestions.build_mod_suggestion_index(PAYLOADS))
    raw = path.read_bytes()
    payload_start = len(mod_suggestion_cache.V2_MAGIC) + 65
    body = json.loads(raw[payload_start:])
    body["index"]["prefix_map"]["bad"] = [body["index"]["count"]]
    payload = json.dumps(body, separators=(",", ":")).encode()
    path.write_bytes(mod_suggestion_cache.V2_MAGIC + hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload)
    assert mod_suggestion_cache.load_suggestion_cache(path) is None


def test_migration_completion_replaces_v1_once_without_changing_active_index(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, index)
    loaded = mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001)
    stage = mod_suggestion_cache.build_staged_suggestion_migration(
        path, loaded.index, loaded.created_at,
    )
    pending = Future()
    submissions = []
    class Executor:
        def submit(self, fn, *args):
            submissions.append((fn, args))
            return pending
    monkeypatch.setattr(window.GLib, "idle_add", lambda *_: 1)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    operations = []
    original_replace = window.os.replace
    def replace(source, destination):
        original_replace(source, destination)
        operations.append(("replace", Path(source), Path(destination)))
    monkeypatch.setattr(window.os, "replace", replace)
    monkeypatch.setattr(
        window, "_fsync_directory_best_effort",
        lambda directory: operations.append(("fsync", Path(directory))),
    )
    host = SimpleNamespace(
        _suggestion_cache_path=path, _mod_suggestion_index=loaded.index,
        _suggestion_cache_created_at=loaded.created_at,
        _suggestion_cache_migration_pending=True, _suggestion_cache_generation=2,
        _suggestion_sweep_complete=True, _suggestion_cache_expired=False,
        _update_executor=Executor(), _shutdown_cleanup_done=False,
    )
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert len(submissions) == 1
    pending.set_result(stage)
    assert DZLLWindow._on_mod_suggestion_migration_done(host, 2, 0, pending) is False
    assert host._mod_suggestion_index is loaded.index
    assert not host._suggestion_cache_migration_pending
    assert operations == [
        ("replace", stage, path),
        ("fsync", tmp_path),
    ]
    assert mod_suggestion_cache.load_suggestion_cache(path, now=1_000_000_001).format_version == 2
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert len(submissions) == 1


def test_refresh_completion_fsyncs_directory_after_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    staged = tmp_path / ".cache.pending"
    staged.write_bytes(b"replacement")
    replacement = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    completed = Future()
    completed.set_result((replacement, staged))
    operations = []
    original_replace = window.os.replace
    def replace(source, destination):
        original_replace(source, destination)
        operations.append(("replace", Path(source), Path(destination)))
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    monkeypatch.setattr(window.os, "replace", replace)
    monkeypatch.setattr(
        window, "_fsync_directory_best_effort",
        lambda directory: operations.append(("fsync", Path(directory))),
    )
    host = SimpleNamespace(
        _suggestion_cache_path=path,
        _suggestion_cache_generation=3,
        _suggestion_refresh_inflight=True,
        _shutdown_cleanup_done=False,
    )

    assert DZLLWindow._on_mod_suggestion_refresh_done(host, 3, 0, completed) is False

    assert path.read_bytes() == b"replacement"
    assert operations == [
        ("replace", staged, path),
        ("fsync", tmp_path),
    ]


def test_migration_waits_for_sweep_and_rejects_stale_or_shutdown_worker(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, index)
    old = path.read_bytes()
    pending = Future()
    submitted = []
    class Executor:
        def submit(self, fn, *args):
            submitted.append((fn, args))
            return pending
    monkeypatch.setattr(window.GLib, "idle_add", lambda *_: 1)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    host = SimpleNamespace(
        _suggestion_cache_path=path, _mod_suggestion_index=index,
        _suggestion_cache_created_at=1_000_000_000, _suggestion_cache_migration_pending=True,
        _suggestion_cache_generation=4, _suggestion_sweep_complete=False,
        _suggestion_sweep_fallback_ready=False, _suggestion_cache_expired=False,
        _update_executor=Executor(), _shutdown_cleanup_done=False,
    )
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert submitted == []
    host._suggestion_sweep_complete = True
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert len(submitted) == 1
    stage = mod_suggestion_cache.build_staged_suggestion_migration(path, index, 1_000_000_000)
    pending.set_result(stage)
    host._suggestion_cache_generation += 1
    assert DZLLWindow._on_mod_suggestion_migration_done(host, 4, 0, pending) is False
    assert not stage.exists() and path.read_bytes() == old
    host._shutdown_cleanup_done = True
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert len(submitted) == 1


def test_migration_failure_preserves_v1_and_allows_retry(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, index)
    old = path.read_bytes()
    failed = Future()
    failed.set_exception(OSError("worker failed"))
    host = SimpleNamespace(_suggestion_cache_path=path, _mod_suggestion_index=index,
                           _suggestion_cache_migration_pending=True,
                           _suggestion_cache_migration_inflight=True,
                           _suggestion_cache_expired=False, _shutdown_cleanup_done=False)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    assert DZLLWindow._on_mod_suggestion_migration_done(host, 0, 0, failed) is False
    assert host._suggestion_cache_migration_pending and not host._suggestion_cache_migration_inflight
    assert host._suggestion_cache_migration_retry_after > 0 and path.read_bytes() == old


def test_shutdown_discards_completed_migration_without_replacing_v1(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, index)
    before = path.read_bytes()
    stage = mod_suggestion_cache.build_staged_suggestion_migration(path, index, 1_000_000_000)
    completed = Future()
    completed.set_result(stage)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    host = SimpleNamespace(
        _suggestion_cache_path=path, _suggestion_cache_generation=1,
        _suggestion_cache_migration_inflight=True,
        _suggestion_cache_migration_pending=True, _shutdown_cleanup_done=True,
    )
    assert DZLLWindow._on_mod_suggestion_migration_done(host, 1, 0, completed) is False
    assert not stage.exists() and path.read_bytes() == before


def test_expired_v1_migration_precedes_monthly_refresh_without_db_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    _write_v1_cache(path, index)
    jobs = []
    class Executor:
        def submit(self, fn, *args):
            jobs.append((fn, args))
            return Future()
    monkeypatch.setattr(window.GLib, "idle_add", lambda *_: 1)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    host = SimpleNamespace(
        _suggestion_cache_path=path, _mod_suggestion_index=index,
        _suggestion_cache_created_at=1_000_000_000,
        _suggestion_cache_migration_pending=True,
        _suggestion_cache_expired=True, _suggestion_cache_ready=True,
        _suggestion_cache_generation=1, _suggestion_sweep_complete=True,
        _obj_by_key={"server": SimpleNamespace(mods_json="[]")},
        _update_executor=Executor(), _shutdown_cleanup_done=False,
    )
    DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert jobs == []
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    assert len(jobs) == 1 and jobs[0][0] is mod_suggestion_cache.build_staged_suggestion_migration
    assert host._mod_suggestion_index is index and path.read_bytes().startswith(b"{")
