from __future__ import annotations

import builtins
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from dzll_launcher import atomic_json, mod_metadata, mod_name_resolver, mods_ui
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.steam_native import SteamClientState


@pytest.fixture
def isolated_metadata_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    metadata_path = cache_dir / "mod_metadata.json"
    monkeypatch.setattr(mod_metadata, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(mod_metadata, "MOD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_LOCK", threading.RLock())
    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_TRANSACTION", threading.local())
    monkeypatch.setattr(mod_name_resolver, "names_from_server_db", lambda _ids: {})
    return metadata_path


def _write_cache(path: Path, mods: dict, *, version=1) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": version, "mods": mods}, indent=2).encode("utf-8")
    path.write_bytes(payload)
    return payload


def _read_mods(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["mods"]


def _observe_persistence(monkeypatch) -> dict[str, int]:
    counts = {
        "loads": 0,
        "transactions": 0,
        "serializations": 0,
        "tempfiles": 0,
        "fsyncs": 0,
        "replaces": 0,
    }
    original_load = mod_metadata.load_mod_metadata
    original_mutate = mod_metadata._mutate_mod_metadata
    original_dump = mod_metadata.json.dump
    original_mkstemp = mod_metadata.tempfile.mkstemp
    original_fsync = mod_metadata.os.fsync
    original_replace = mod_metadata.os.replace

    def load(*args, **kwargs):
        counts["loads"] += 1
        return original_load(*args, **kwargs)

    def mutate(*args, **kwargs):
        counts["transactions"] += 1
        return original_mutate(*args, **kwargs)

    def dump(*args, **kwargs):
        counts["serializations"] += 1
        return original_dump(*args, **kwargs)

    def mkstemp(*args, **kwargs):
        counts["tempfiles"] += 1
        return original_mkstemp(*args, **kwargs)

    def fsync(*args, **kwargs):
        counts["fsyncs"] += 1
        return original_fsync(*args, **kwargs)

    def replace(*args, **kwargs):
        counts["replaces"] += 1
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(mod_metadata, "load_mod_metadata", load)
    monkeypatch.setattr(mod_metadata, "_mutate_mod_metadata", mutate)
    monkeypatch.setattr(mod_metadata.json, "dump", dump)
    monkeypatch.setattr(mod_metadata.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(mod_metadata.os, "fsync", fsync)
    monkeypatch.setattr(mod_metadata.os, "replace", replace)
    return counts


def test_bulk_names_empty_or_all_weak_do_not_open_transaction(monkeypatch):
    monkeypatch.setattr(
        mod_metadata,
        "_mutate_mod_metadata",
        lambda _mutate: pytest.fail("empty or weak-only input opened a transaction"),
    )

    mod_metadata.upsert_many_names({})
    mod_metadata.upsert_many_names({101: "", 102: "@Mod-ID - 102", 103: "@103"})


def test_save_fsyncs_directory_after_replacement(isolated_metadata_cache, monkeypatch):
    events = []
    original_replace = mod_metadata.os.replace

    def replace(*args):
        events.append("replace")
        return original_replace(*args)

    def fsync_directory(path):
        events.append(("directory-fsync", Path(path)))

    monkeypatch.setattr(mod_metadata.os, "replace", replace)
    monkeypatch.setattr(mod_metadata, "_fsync_directory_best_effort", fsync_directory)

    mod_metadata.save_mod_metadata({"version": 1, "mods": {"101": {"name": "Name"}}})

    assert events == ["replace", ("directory-fsync", isolated_metadata_cache.parent)]
    assert mod_metadata.load_mod_metadata()["mods"]["101"]["name"] == "Name"


def test_directory_fsync_failure_preserves_successful_replacement(
    isolated_metadata_cache, monkeypatch,
):
    real_fsync = mod_metadata.os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync unavailable")
        return real_fsync(fd)

    monkeypatch.setattr(
        mod_metadata.os, "fsync", fsync,
    )

    mod_metadata.save_mod_metadata({"version": 1, "mods": {"102": {"name": "Name"}}})

    assert mod_metadata.load_mod_metadata()["mods"]["102"]["name"] == "Name"


def test_directory_fsync_helper_closes_directory_descriptor(tmp_path, monkeypatch):
    real_open = atomic_json.os.open
    real_close = atomic_json.os.close
    opened = []
    closed = []

    def open_directory(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_directory(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(atomic_json.os, "open", open_directory)
    monkeypatch.setattr(atomic_json.os, "close", close_directory)

    atomic_json._fsync_directory_best_effort(tmp_path)

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        os.fsync(opened[0])


def test_pre_replace_failure_skips_directory_fsync_and_cleans_temp(
    isolated_metadata_cache, monkeypatch,
):
    events = []
    real_fsync = mod_metadata.os.fsync
    real_replace = mod_metadata.os.replace

    def fsync(fd):
        events.append("file-fsync")
        return real_fsync(fd)

    def replace(*args):
        events.append("replace")
        return real_replace(*args)

    monkeypatch.setattr(mod_metadata.os, "fsync", fsync)
    monkeypatch.setattr(mod_metadata.os, "replace", replace)
    monkeypatch.setattr(
        mod_metadata,
        "_fsync_directory_best_effort",
        lambda _path: events.append("directory-fsync"),
    )
    monkeypatch.setattr(
        mod_metadata.json,
        "dump",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failure")),
    )

    with pytest.raises(OSError, match="write failure"):
        mod_metadata.save_mod_metadata({"version": 1, "mods": {"103": {"name": "Name"}}})

    assert events == []
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))


def test_replace_failure_skips_directory_fsync_and_cleans_temp(
    isolated_metadata_cache, monkeypatch,
):
    events = []
    monkeypatch.setattr(
        mod_metadata.os,
        "replace",
        lambda *_args: (events.append("replace"), (_ for _ in ()).throw(OSError("replace failure")))[1],
    )
    monkeypatch.setattr(
        mod_metadata,
        "_fsync_directory_best_effort",
        lambda _path: events.append("directory-fsync"),
    )

    with pytest.raises(OSError, match="replace failure"):
        mod_metadata.save_mod_metadata({"version": 1, "mods": {"104": {"name": "Name"}}})

    assert events == ["replace"]
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))


def test_preexisting_stale_temp_is_not_swept(isolated_metadata_cache):
    stale = isolated_metadata_cache.parent / "mod_metadata.old.tmp"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale", encoding="utf-8")

    mod_metadata.save_mod_metadata({"version": 1, "mods": {"105": {"name": "Name"}}})

    assert stale.read_text(encoding="utf-8") == "stale"


@pytest.mark.parametrize("count", [0, 5, 10, 11, 25])
def test_stale_metadata_temps_retain_newest_ten(isolated_metadata_cache, count):
    isolated_metadata_cache.parent.mkdir(parents=True, exist_ok=True)
    stale = []
    for index in range(count):
        path = isolated_metadata_cache.parent / f"mod_metadata.stale-{index:02d}.tmp"
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
        stale.append(path)

    mod_metadata.upsert_many_names({106: "Name"})

    retained = [path for path in stale if path.exists()]
    assert retained == stale[max(0, count - 10):]


def test_stale_metadata_equal_mtimes_use_deterministic_filename_order(
    isolated_metadata_cache,
):
    isolated_metadata_cache.parent.mkdir(parents=True, exist_ok=True)
    stale = [
        isolated_metadata_cache.parent / f"mod_metadata.equal-{index:02d}.tmp"
        for index in range(11)
    ]
    for path in stale:
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (100, 100))

    mod_metadata.upsert_many_names({107: "Name"})

    assert stale[-1].exists() is False
    assert all(path.exists() for path in stale[:-1])


def test_stale_cleanup_ignores_unrelated_files_symlinks_and_directories(
    isolated_metadata_cache,
):
    isolated_metadata_cache.parent.mkdir(parents=True, exist_ok=True)
    regular = [
        isolated_metadata_cache.parent / f"mod_metadata.regular-{index:02d}.tmp"
        for index in range(11)
    ]
    for index, path in enumerate(regular):
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    unrelated = isolated_metadata_cache.parent / "other-owner.tmp"
    unrelated.write_text("keep", encoding="utf-8")
    target = isolated_metadata_cache.parent / "symlink-target"
    target.write_text("keep", encoding="utf-8")
    symlink = isolated_metadata_cache.parent / "mod_metadata.link.tmp"
    symlink.symlink_to(target)
    directory = isolated_metadata_cache.parent / "mod_metadata.directory.tmp"
    directory.mkdir()

    mod_metadata.upsert_many_names({108: "Name"})

    assert regular[0].exists() is False
    assert all(path.exists() for path in regular[1:])
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert symlink.is_symlink()
    assert directory.is_dir()
    assert target.read_text(encoding="utf-8") == "keep"


def test_stale_cleanup_failure_is_non_fatal(isolated_metadata_cache, monkeypatch):
    isolated_metadata_cache.parent.mkdir(parents=True, exist_ok=True)
    stale = isolated_metadata_cache.parent / "mod_metadata.old.tmp"
    stale.write_text("stale", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_stale_unlink(path, *args, **kwargs):
        if path == stale:
            raise PermissionError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)
    mod_metadata.upsert_many_names({109: "Name"})

    assert isolated_metadata_cache.is_file()
    assert stale.is_file()


def test_failed_save_does_not_trigger_stale_cleanup(
    isolated_metadata_cache, monkeypatch,
):
    isolated_metadata_cache.parent.mkdir(parents=True, exist_ok=True)
    stale = []
    for index in range(11):
        path = isolated_metadata_cache.parent / f"mod_metadata.failed-{index:02d}.tmp"
        path.write_text("stale", encoding="utf-8")
        stale.append(path)
    monkeypatch.setattr(
        mod_metadata.json,
        "dump",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failure")),
    )

    with pytest.raises(OSError, match="write failure"):
        mod_metadata.upsert_many_names({110: "Name"})

    assert all(path.exists() for path in stale)


def test_bulk_names_persist_one_valid_name(isolated_metadata_cache):
    mod_metadata.upsert_many_names({101: "One Name"})

    assert _read_mods(isolated_metadata_cache)["101"]["name"] == "One Name"


def test_bulk_names_preserve_fields_and_use_one_timestamp(
    isolated_metadata_cache, monkeypatch,
):
    _write_cache(isolated_metadata_cache, {
        "101": {
            "id": 101,
            "name": "Old",
            "size_bytes": 4096,
            "install_folder": "/workshop/101",
            "subscribed": True,
            "installed": True,
            "last_used_at": "2026-01-02T03:04:05Z",
            "future_field": {"preserved": True},
        },
    })
    monkeypatch.setattr(mod_metadata, "_utc_now", lambda: "2026-08-31T12:00:00Z")

    mod_metadata.upsert_many_names({101: "New Name", 102: "Second Name"})

    mods = _read_mods(isolated_metadata_cache)
    assert mods["101"] == {
        "id": 101,
        "name": "New Name",
        "size_bytes": 4096,
        "install_folder": "/workshop/101",
        "subscribed": True,
        "installed": True,
        "last_used_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-08-31T12:00:00Z",
        "future_field": {"preserved": True},
    }
    assert mods["102"] == {
        "id": 102,
        "name": "Second Name",
        "updated_at": "2026-08-31T12:00:00Z",
    }


def test_bulk_names_normalize_unicode_suffix_length_and_duplicate_ids(
    isolated_metadata_cache,
):
    raw_long = "Café فارسی\u200c 👩\u200d💻\n\u202e\u200b" + ("界" * 1000) + "__201"

    mod_metadata.upsert_many_names({
        201: raw_long,
        202: "First value",
        "202": "Final\nvalue__202",
        203: "@Mod-ID - 203",
    })

    mods = _read_mods(isolated_metadata_cache)
    assert mods["201"]["name"] == mod_metadata.clean_display_mod_name(raw_long, 201)
    assert len(mods["201"]["name"]) == mod_metadata.MOD_DISPLAY_NAME_MAX_CHARS
    assert mods["202"]["name"] == "Final value"
    assert "203" not in mods


def test_bulk_names_invalid_id_aborts_before_transaction(monkeypatch):
    monkeypatch.setattr(
        mod_metadata,
        "_mutate_mod_metadata",
        lambda _mutate: pytest.fail("invalid input opened a transaction"),
    )

    with pytest.raises(ValueError):
        mod_metadata.upsert_many_names({"not-an-id": "Name"})


def test_bulk_names_nested_transaction_reuses_one_load_and_save(
    isolated_metadata_cache, monkeypatch,
):
    _write_cache(isolated_metadata_cache, {"1": {"id": 1, "name": "Baseline"}})
    counts = _observe_persistence(monkeypatch)

    mod_metadata._mutate_mod_metadata(
        lambda data: mod_metadata.upsert_many_names({2: "Two", 3: "Three"})
    )

    assert set(_read_mods(isolated_metadata_cache)) == {"1", "2", "3"}
    assert counts == {
        "loads": 1,
        "transactions": 2,
        "serializations": 1,
        "tempfiles": 1,
        "fsyncs": 2,
        "replaces": 1,
    }


@pytest.mark.parametrize("version", [0, -1, 2, 10**100])
def test_bulk_names_reject_unsupported_version_without_touching_bytes(
    isolated_metadata_cache, version,
):
    original = _write_cache(
        isolated_metadata_cache,
        {"1": {"id": 1, "name": "Baseline"}},
        version=version,
    )

    with pytest.raises(mod_metadata.MetadataCacheUnsupportedVersionError):
        mod_metadata.upsert_many_names({2: "Two", 3: "Three"})

    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))


@pytest.mark.parametrize("error", [OSError("transient"), PermissionError("denied")])
def test_bulk_names_read_failure_preserves_cache(
    isolated_metadata_cache, monkeypatch, error,
):
    original = _write_cache(
        isolated_metadata_cache, {"1": {"id": 1, "name": "Baseline"}},
    )
    real_open = builtins.open

    def injected_open(file, *args, **kwargs):
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if os.fspath(file) == os.fspath(isolated_metadata_cache) and "r" in mode:
            raise error
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", injected_open)
    with pytest.raises(mod_metadata.MetadataCacheReadError):
        mod_metadata.upsert_many_names({2: "Two", 3: "Three"})

    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))


def test_bulk_names_replace_failure_is_atomic_and_retryable(
    isolated_metadata_cache, monkeypatch,
):
    original = _write_cache(
        isolated_metadata_cache, {"1": {"id": 1, "name": "Baseline"}},
    )
    real_replace = mod_metadata.os.replace
    monkeypatch.setattr(
        mod_metadata.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
    )

    with pytest.raises(OSError, match="replace failure"):
        mod_metadata.upsert_many_names({2: "Two", 3: "Three"})

    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))

    monkeypatch.setattr(mod_metadata.os, "replace", real_replace)
    mod_metadata.upsert_many_names({2: "Two", 3: "Three"})
    assert set(_read_mods(isolated_metadata_cache)) == {"1", "2", "3"}


def test_bulk_names_recovers_malformed_outer_cache(isolated_metadata_cache):
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_text('{"version":1,"mods":{"1":', encoding="utf-8")

    mod_metadata.upsert_many_names({2: "Two", 3: "Three"})

    assert set(_read_mods(isolated_metadata_cache)) == {"2", "3"}


@pytest.mark.parametrize("count", [1, 10, 100, 500])
def test_resolver_strong_names_use_one_physical_save(
    isolated_metadata_cache, monkeypatch, count,
):
    counts = _observe_persistence(monkeypatch)
    ids = list(range(100_000, 100_000 + count))

    resolved = mod_name_resolver.resolve_best_mod_names(
        ids,
        metadata={},
        workshop_roots=[],
        symlink_names={mid: f"Strong Name {mid}" for mid in ids},
    )

    assert len(resolved) == count
    assert len(_read_mods(isolated_metadata_cache)) == count
    assert counts == {
        "loads": 1,
        "transactions": 1,
        "serializations": 1,
        "tempfiles": 1,
        "fsyncs": 2,
        "replaces": 1,
    }


@pytest.mark.parametrize("count", [1, 10, 100, 500])
def test_resolver_strong_cache_hits_use_one_confirmation_save(
    isolated_metadata_cache, monkeypatch, count,
):
    ids = list(range(200_000, 200_000 + count))
    cached = {
        str(mid): {"id": mid, "name": f"Cached Name {mid}"}
        for mid in ids
    }
    _write_cache(isolated_metadata_cache, cached)
    counts = _observe_persistence(monkeypatch)

    resolved = mod_name_resolver.resolve_best_mod_names(
        ids, metadata=cached, workshop_roots=[], symlink_names={},
    )

    assert len(resolved) == count
    assert counts == {
        "loads": 1,
        "transactions": 1,
        "serializations": 1,
        "tempfiles": 1,
        "fsyncs": 2,
        "replaces": 1,
    }


def test_resolver_mixed_hits_and_misses_use_one_save(
    isolated_metadata_cache, monkeypatch,
):
    ids = list(range(300_000, 300_100))
    cached = {
        str(mid): {"id": mid, "name": f"Cached Name {mid}"}
        for mid in ids[:90]
    }
    _write_cache(isolated_metadata_cache, cached)
    counts = _observe_persistence(monkeypatch)

    resolved = mod_name_resolver.resolve_best_mod_names(
        ids,
        metadata=cached,
        workshop_roots=[],
        symlink_names={mid: f"New Name {mid}" for mid in ids[90:]},
    )

    assert len(resolved) == 100
    assert len(_read_mods(isolated_metadata_cache)) == 100
    assert counts == {
        "loads": 1,
        "transactions": 1,
        "serializations": 1,
        "tempfiles": 1,
        "fsyncs": 2,
        "replaces": 1,
    }


def test_resolver_batches_all_strong_sources_and_excludes_weak(
    tmp_path, monkeypatch,
):
    local_root = tmp_path / "workshop"
    local_item = local_root / "content/221100/403"
    local_item.mkdir(parents=True)
    (local_item / "meta.cpp").write_text('name = "Local Name";\n', encoding="utf-8")
    monkeypatch.setattr(
        mod_name_resolver,
        "names_from_server_db",
        lambda _ids: {402: "Server Name"},
    )
    persisted = []
    monkeypatch.setattr(
        mod_name_resolver,
        "upsert_many_names",
        lambda names: persisted.append(dict(names)),
    )

    resolved = mod_name_resolver.resolve_best_mod_names(
        [401, 402, 403, 404, 405],
        metadata={"401": {"name": "Cached Name"}},
        workshop_roots=[local_root],
        symlink_names={404: "Symlink Name"},
    )

    assert resolved == {
        401: "Cached Name",
        402: "Server Name",
        403: "Local Name",
        404: "Symlink Name",
        405: "@Mod-ID - 405",
    }
    assert persisted == [{
        401: "Cached Name",
        402: "Server Name",
        403: "Local Name",
        404: "Symlink Name",
    }]


@pytest.mark.parametrize("failure", ["read", "unsupported", "replace"])
def test_resolver_batch_failure_returns_names_and_preserves_prior_cache(
    isolated_metadata_cache, monkeypatch, failure,
):
    version = 2 if failure == "unsupported" else 1
    original = _write_cache(
        isolated_metadata_cache,
        {"1": {"id": 1, "name": "Baseline"}},
        version=version,
    )
    ids = [501, 502, 503]

    real_replace = mod_metadata.os.replace
    if failure == "read":
        real_open = builtins.open

        def injected_open(file, *args, **kwargs):
            mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
            if os.fspath(file) == os.fspath(isolated_metadata_cache) and "r" in mode:
                raise PermissionError("injected denial")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", injected_open)
    elif failure == "replace":
        monkeypatch.setattr(
            mod_metadata.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
        )

    resolved = mod_name_resolver.resolve_best_mod_names(
        ids,
        metadata={},
        workshop_roots=[],
        symlink_names={mid: f"Resolved {mid}" for mid in ids},
    )

    assert resolved == {mid: f"Resolved {mid}" for mid in ids}
    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))

    if failure == "replace":
        monkeypatch.setattr(mod_metadata.os, "replace", real_replace)
        retried = mod_name_resolver.resolve_best_mod_names(
            ids,
            metadata={},
            workshop_roots=[],
            symlink_names={mid: f"Resolved {mid}" for mid in ids},
        )
        assert retried == resolved
        assert set(_read_mods(isolated_metadata_cache)) == {"1", "501", "502", "503"}


@pytest.mark.parametrize("updater_kind", ["ugc-bulk", "individual"])
def test_resolver_batch_serializes_with_concurrent_metadata_updates(
    isolated_metadata_cache, monkeypatch, updater_kind,
):
    _write_cache(isolated_metadata_cache, {"1": {"id": 1, "name": "Baseline"}})
    original_save = mod_metadata.save_mod_metadata
    resolver_at_save = threading.Event()
    release_resolver = threading.Event()
    updater_started = threading.Event()
    errors = []

    def blocking_save(data):
        if threading.current_thread().name == "resolver":
            resolver_at_save.set()
            assert release_resolver.wait(5)
        return original_save(data)

    monkeypatch.setattr(mod_metadata, "save_mod_metadata", blocking_save)
    ids = list(range(600, 700))

    def resolve():
        try:
            mod_name_resolver.resolve_best_mod_names(
                ids,
                metadata={},
                workshop_roots=[],
                symlink_names={mid: f"Resolved {mid}" for mid in ids},
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    def update():
        updater_started.set()
        try:
            if updater_kind == "ugc-bulk":
                mod_metadata.upsert_many_from_ugc_state(
                    {800: {"id": 800, "installed": True}},
                    names_by_id={800: "UGC Update"},
                )
            else:
                mod_metadata.upsert_mod_metadata(801, name="Individual Update")
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    resolver_thread = threading.Thread(target=resolve, name="resolver")
    resolver_thread.start()
    assert resolver_at_save.wait(5)
    updater_thread = threading.Thread(target=update, name="updater")
    updater_thread.start()
    assert updater_started.wait(5)
    time.sleep(0.02)
    assert updater_thread.is_alive()
    release_resolver.set()
    resolver_thread.join(5)
    updater_thread.join(5)

    assert not resolver_thread.is_alive()
    assert not updater_thread.is_alive()
    assert errors == []
    mods = _read_mods(isolated_metadata_cache)
    assert set(map(str, ids)).issubset(mods)
    assert "1" in mods
    assert ("800" if updater_kind == "ugc-bulk" else "801") in mods


@pytest.mark.parametrize("existing_count", [10, 100, 1000])
def test_resolver_save_count_is_bounded_by_batch_not_cache_size(
    isolated_metadata_cache, monkeypatch, existing_count,
):
    cached = {
        str(mid): {"id": mid, "name": f"Existing {mid}"}
        for mid in range(1, existing_count + 1)
    }
    _write_cache(isolated_metadata_cache, cached)
    counts = _observe_persistence(monkeypatch)
    ids = list(range(700_000, 700_100))

    mod_name_resolver.resolve_best_mod_names(
        ids,
        metadata=cached,
        workshop_roots=[],
        symlink_names={mid: f"New Name {mid}" for mid in ids},
    )

    assert counts["loads"] == 1
    assert counts["transactions"] == 1
    assert counts["serializations"] == 1
    assert counts["tempfiles"] == 1
    assert counts["fsyncs"] == 2
    assert counts["replaces"] == 1


def test_real_mod_manager_split_library_inventory_uses_one_name_save(
    isolated_metadata_cache, tmp_path, monkeypatch,
):
    dayz_library = tmp_path / "DayZLibrary"
    secondary_library = tmp_path / "SecondaryLibrary"
    dayz_root = dayz_library / "steamapps/workshop"
    secondary_root = secondary_library / "steamapps/workshop"
    expected = {}
    for index, mid in enumerate(range(800_000, 800_120)):
        root = dayz_root if index % 2 == 0 else secondary_root
        item = root / "content/221100" / str(mid)
        item.mkdir(parents=True)
        name = f"Inventory Name {mid}"
        (item / "meta.cpp").write_text(f'name = "{name}";\n', encoding="utf-8")
        expected[mid] = name

    monkeypatch.setattr(mods_ui, "dayz_steam_library", lambda: dayz_library)
    monkeypatch.setattr(
        mods_ui,
        "dayz_workshop_content_dir",
        lambda: dayz_root / "content/221100",
    )
    monkeypatch.setattr(
        mods_ui,
        "native_steam_libraries",
        lambda: [dayz_library, secondary_library],
    )
    monkeypatch.setattr(mods_ui, "autodetect_workshop_dir", lambda: str(dayz_root))
    save_calls = 0
    original_save = mod_metadata.save_mod_metadata

    def counted_save(data):
        nonlocal save_calls
        save_calls += 1
        return original_save(data)

    monkeypatch.setattr(mod_metadata, "save_mod_metadata", counted_save)
    items = ModsManagerOverlay.__new__(ModsManagerOverlay)._load_installed_items(
        str(dayz_root),
        str(tmp_path / "isolated-prefix"),
        steam_state=SteamClientState.OFFLINE,
    )

    assert len(items) == 120
    assert {item[1]: item[0] for item in items} == expected
    assert save_calls == 1
    assert len(_read_mods(isolated_metadata_cache)) == 120
    assert mods_ui._candidate_workshop_roots(str(dayz_root)) == [
        dayz_root.resolve(), secondary_root.resolve(),
    ]
