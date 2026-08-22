from __future__ import annotations

import json
import multiprocessing
import threading

import pytest

from dzll_launcher import mod_metadata


class _ObservedRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts = 0
        self._attempts_lock = threading.Lock()
        self.second_attempted = threading.Event()

    def __enter__(self):
        with self._attempts_lock:
            self._attempts += 1
            if self._attempts == 2:
                self.second_attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._lock.release()


@pytest.fixture
def isolated_metadata(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    metadata_path = cache_dir / "mod_metadata.json"
    monkeypatch.setattr(mod_metadata, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(mod_metadata, "MOD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_LOCK", threading.RLock())
    mod_metadata.save_mod_metadata({"version": 1, "mods": {"1": {"id": 1, "name": "Baseline"}}})
    return metadata_path


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_controlled_threads(monkeypatch, first, second) -> None:
    first_loaded = threading.Event()
    release_first = threading.Event()
    observed_lock = _ObservedRLock()
    original_load = mod_metadata.load_mod_metadata
    load_calls = 0
    load_calls_lock = threading.Lock()

    def controlled_load():
        nonlocal load_calls
        data = original_load()
        with load_calls_lock:
            load_calls += 1
            call_number = load_calls
        if call_number == 1:
            first_loaded.set()
            assert release_first.wait(5)
        return data

    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_LOCK", observed_lock)
    monkeypatch.setattr(mod_metadata, "load_mod_metadata", controlled_load)
    errors: list[BaseException] = []

    def run(operation):
        try:
            operation()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(first,))
    second_thread = threading.Thread(target=run, args=(second,))
    first_thread.start()
    assert first_loaded.wait(5)
    second_thread.start()
    assert observed_lock.second_attempted.wait(5)
    release_first.set()
    first_thread.join(5)
    second_thread.join(5)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("first_id,second_id", [(2, 3), (3, 2)])
def test_independent_upserts_survive_both_serialized_orderings(
    isolated_metadata, monkeypatch, first_id, second_id
):
    _run_controlled_threads(
        monkeypatch,
        lambda: mod_metadata.upsert_mod_metadata(first_id, name=f"Mod {first_id}"),
        lambda: mod_metadata.upsert_mod_metadata(second_id, name=f"Mod {second_id}"),
    )

    assert set(_read(isolated_metadata)["mods"]) == {"1", "2", "3"}


def test_bulk_update_and_mark_used_both_survive(isolated_metadata, monkeypatch):
    _run_controlled_threads(
        monkeypatch,
        lambda: mod_metadata.upsert_many_from_ugc_state(
            {2: {"id": 2, "subscribed": True, "installed": True, "size_on_disk": 2048}}
        ),
        lambda: mod_metadata.mark_mods_used([1], used_at="2026-01-02T03:04:05Z"),
    )

    mods = _read(isolated_metadata)["mods"]
    assert mods["1"]["last_used_at"] == "2026-01-02T03:04:05Z"
    assert mods["2"]["size_bytes"] == 2048
    assert mods["2"]["installed"] is True


def test_individual_upsert_and_mark_used_preserve_same_mod_fields(isolated_metadata, monkeypatch):
    _run_controlled_threads(
        monkeypatch,
        lambda: mod_metadata.upsert_mod_metadata(1, size_bytes=4096, installed=True),
        lambda: mod_metadata.mark_mods_used([1], used_at="2026-02-03T04:05:06Z"),
    )

    entry = _read(isolated_metadata)["mods"]["1"]
    assert entry["name"] == "Baseline"
    assert entry["size_bytes"] == 4096
    assert entry["installed"] is True
    assert entry["last_used_at"] == "2026-02-03T04:05:06Z"


def test_multiple_simultaneous_writers_preserve_every_mod(isolated_metadata):
    writer_count = 12
    start = threading.Barrier(writer_count)
    errors: list[BaseException] = []

    def writer(mod_id: int) -> None:
        try:
            start.wait(timeout=5)
            mod_metadata.upsert_mod_metadata(mod_id, name=f"Mod {mod_id}")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(mod_id,)) for mod_id in range(2, 14)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert set(_read(isolated_metadata)["mods"]) == {str(mod_id) for mod_id in range(1, 14)}


def _cross_process_upsert(
    cache_dir: str,
    metadata_path: str,
    mod_id: int,
    loaded,
    release,
    lock_attempted,
) -> None:
    from dzll_launcher import mod_metadata as child_metadata

    child_metadata.CACHE_DIR = cache_dir
    child_metadata.MOD_METADATA_PATH = metadata_path
    child_metadata._MOD_METADATA_LOCK = threading.RLock()

    if loaded is not None:
        original_load = child_metadata.load_mod_metadata

        def controlled_load():
            data = original_load()
            loaded.set()
            if not release.wait(10):
                raise RuntimeError("timed out waiting to release first writer")
            return data

        child_metadata.load_mod_metadata = controlled_load

    if lock_attempted is not None:
        original_flock = child_metadata.fcntl.flock

        def observed_flock(fd, operation):
            if operation == child_metadata.fcntl.LOCK_EX:
                lock_attempted.set()
            return original_flock(fd, operation)

        child_metadata.fcntl.flock = observed_flock

    child_metadata.upsert_mod_metadata(mod_id, name=f"Mod {mod_id}")


def test_cross_process_writers_share_stable_lock_file(isolated_metadata):
    context = multiprocessing.get_context("spawn")
    first_loaded = context.Event()
    release_first = context.Event()
    second_lock_attempted = context.Event()
    cache_dir = str(isolated_metadata.parent)
    metadata_path = str(isolated_metadata)

    first = context.Process(
        target=_cross_process_upsert,
        args=(cache_dir, metadata_path, 2, first_loaded, release_first, None),
    )
    second = context.Process(
        target=_cross_process_upsert,
        args=(cache_dir, metadata_path, 3, None, None, second_lock_attempted),
    )
    first.start()
    assert first_loaded.wait(10)
    lock_path = isolated_metadata.with_name(f"{isolated_metadata.name}.lock")
    lock_inode = lock_path.stat().st_ino
    first_metadata_inode = isolated_metadata.stat().st_ino

    second.start()
    assert second_lock_attempted.wait(10)
    release_first.set()
    first.join(10)
    second.join(10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert set(_read(isolated_metadata)["mods"]) == {"1", "2", "3"}
    assert lock_path.stat().st_ino == lock_inode
    assert isolated_metadata.stat().st_ino != first_metadata_inode


def test_failed_atomic_save_preserves_file_and_releases_locks(isolated_metadata, monkeypatch):
    original_bytes = isolated_metadata.read_bytes()
    original_replace = mod_metadata.os.replace

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(mod_metadata.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        mod_metadata.upsert_mod_metadata(2, name="Failed")

    assert isolated_metadata.read_bytes() == original_bytes
    assert list(isolated_metadata.parent.glob("mod_metadata.*.tmp")) == []

    monkeypatch.setattr(mod_metadata.os, "replace", original_replace)
    mod_metadata.upsert_mod_metadata(3, name="Recovered")
    assert set(_read(isolated_metadata)["mods"]) == {"1", "3"}


def test_nested_internal_mutation_reuses_transaction_without_deadlock(isolated_metadata):
    def outer(data):
        data["mods"]["2"] = {"id": 2, "name": "Outer"}
        mod_metadata._mutate_mod_metadata(
            lambda current: current["mods"].__setitem__("3", {"id": 3, "name": "Nested"})
        )

    mod_metadata._mutate_mod_metadata(outer)

    assert set(_read(isolated_metadata)["mods"]) == {"1", "2", "3"}


def test_serial_field_semantics_are_unchanged(isolated_metadata):
    mod_metadata.upsert_mod_metadata(
        1,
        name="Resolved Name",
        size_bytes=100,
        install_folder="/tmp/workshop/1",
        subscribed=True,
        installed=True,
        updated_at="2026-03-04T05:06:07Z",
    )
    mod_metadata.upsert_mod_metadata(1, name="@Mod-ID - 1", clear_fields=["install_folder"])

    entry = _read(isolated_metadata)["mods"]["1"]
    assert entry["name"] == "Resolved Name"
    assert entry["size_bytes"] == 100
    assert entry["install_folder"] is None
    assert entry["subscribed"] is True
    assert entry["installed"] is True
