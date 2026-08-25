from __future__ import annotations

import builtins
import json
import os
import threading

import pytest

from dzll_launcher import mod_metadata


@pytest.fixture
def isolated_metadata_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    metadata_path = cache_dir / "mod_metadata.json"
    monkeypatch.setattr(mod_metadata, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(mod_metadata, "MOD_METADATA_PATH", str(metadata_path))
    monkeypatch.setattr(mod_metadata, "_MOD_METADATA_LOCK", threading.RLock())
    return metadata_path


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _valid_mods() -> dict:
    return {
        "101": {"id": 101, "name": "Alpha", "size_bytes": 100},
        "102": {"id": 102, "name": "Beta", "last_used_at": "2026-01-02T03:04:05Z"},
    }


def test_missing_cache_is_quiet_and_first_mutation_creates_it(
    isolated_metadata_cache, caplog,
):
    assert mod_metadata.load_mod_metadata() == {"version": 1, "mods": {}}
    assert caplog.records == []

    mod_metadata.upsert_mod_metadata(101, name="Alpha")

    assert mod_metadata.load_mod_metadata()["mods"]["101"]["name"] == "Alpha"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'{"version":1,"mods":{"101":',
        b'{"version":1,"mods":{}} trailing',
        b'{"version":1,"mods":{"101":{"name":"\xff"}}}',
        json.dumps([1, 2]).encode(),
        json.dumps({"version": 1}).encode(),
        json.dumps({"version": 1, "mods": []}).encode(),
    ],
)
def test_malformed_outer_cache_is_rejected_with_diagnostic(
    isolated_metadata_cache, caplog, raw,
):
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_bytes(raw)

    with caplog.at_level("WARNING"):
        loaded = mod_metadata.load_mod_metadata()

    assert loaded == {"version": 1, "mods": {}}
    assert any("malformed mod metadata cache" in record.message for record in caplog.records)


def test_malformed_json_is_replaced_by_later_successful_mutation(
    isolated_metadata_cache,
):
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_text('{"version":1,"mods":{"101":', encoding="utf-8")

    mod_metadata.upsert_mod_metadata(103, name="Gamma")

    persisted = json.loads(isolated_metadata_cache.read_text(encoding="utf-8"))
    assert set(persisted["mods"]) == {"103"}
    assert persisted["version"] == 1


@pytest.mark.parametrize(
    ("include_version", "version"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(True, None, id="null"),
        pytest.param(True, 1, id="integer-one"),
        pytest.param(True, "1", id="string-one"),
    ],
)
def test_supported_or_unversioned_cache_loads_current_schema(
    isolated_metadata_cache, include_version, version,
):
    data = {"mods": _valid_mods()}
    if include_version:
        data["version"] = version
    _write_json(isolated_metadata_cache, data)

    loaded = mod_metadata.load_mod_metadata()

    assert loaded["version"] == 1
    assert set(loaded["mods"]) == {"101", "102"}


@pytest.mark.parametrize("version", ["v2", {}, []])
def test_malformed_version_preserves_current_shaped_mods_with_diagnostic(
    isolated_metadata_cache, caplog, version,
):
    _write_json(isolated_metadata_cache, {"version": version, "mods": _valid_mods()})

    with caplog.at_level("WARNING"):
        loaded = mod_metadata.load_mod_metadata()

    assert loaded["version"] == 1
    assert set(loaded["mods"]) == {"101", "102"}
    assert any("malformed version" in record.message for record in caplog.records)


def test_noncoercible_version_entries_survive_subsequent_mutation(
    isolated_metadata_cache,
):
    mods = _valid_mods()
    mods["101"]["future_field"] = {"preserved": True}
    _write_json(isolated_metadata_cache, {"version": "v2", "mods": mods})

    mod_metadata.upsert_mod_metadata(103, name="Gamma")

    persisted = json.loads(isolated_metadata_cache.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert set(persisted["mods"]) == {"101", "102", "103"}
    assert persisted["mods"]["101"]["future_field"] == {"preserved": True}


@pytest.mark.parametrize("version", [0, -1, 2, 10**999])
def test_unsupported_numeric_version_is_explicitly_rejected(
    isolated_metadata_cache, caplog, version,
):
    _write_json(isolated_metadata_cache, {"version": version, "mods": _valid_mods()})

    with caplog.at_level("WARNING"):
        loaded = mod_metadata.load_mod_metadata()

    assert loaded == {"version": 1, "mods": {}}
    assert loaded.load_status is mod_metadata._LoadStatus.UNSUPPORTED_VERSION
    assert any("unsupported schema version" in record.message for record in caplog.records)


@pytest.mark.parametrize("version", [0, -1, 2, 10**999])
def test_unsupported_numeric_version_aborts_upsert_without_touching_bytes(
    isolated_metadata_cache, version,
):
    original = json.dumps({
        "version": version,
        "mods": {
            "1001": {"id": 1001, "name": "Alpha"},
            "1002": {"id": 1002, "name": "Beta"},
        },
        "future_field": {"preserved": True},
    }, indent=2).encode("utf-8")
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_bytes(original)

    with pytest.raises(mod_metadata.MetadataCacheUnsupportedVersionError):
        mod_metadata.upsert_mod_metadata(1003, name="Gamma")

    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))

    # The failed transaction released both locks; explicit replacement with a
    # supported fixture allows the next normal transaction to proceed.
    _write_json(isolated_metadata_cache, {"version": 1, "mods": _valid_mods()})
    mod_metadata.upsert_mod_metadata(103, name="Gamma")
    assert set(mod_metadata.load_mod_metadata()["mods"]) == {"101", "102", "103"}


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda: mod_metadata.upsert_mod_metadata(103, name="Gamma"),
            id="single-upsert",
        ),
        pytest.param(
            lambda: mod_metadata.mark_mods_used([103], names_by_id={103: "Gamma"}),
            id="mark-used",
        ),
        pytest.param(
            lambda: mod_metadata.upsert_many_from_ugc_state({
                103: {"id": 103, "installed": True, "subscribed": True},
            }),
            id="ugc-bulk-upsert",
        ),
    ],
)
def test_all_metadata_mutation_entry_points_reject_unsupported_cache(
    isolated_metadata_cache, operation,
):
    original = json.dumps({"version": 2, "mods": _valid_mods()}).encode("utf-8")
    isolated_metadata_cache.parent.mkdir(parents=True)
    isolated_metadata_cache.write_bytes(original)

    with pytest.raises(mod_metadata.MetadataCacheUnsupportedVersionError):
        operation()

    assert isolated_metadata_cache.read_bytes() == original
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))


def test_huge_unsupported_integer_warning_is_bounded(
    isolated_metadata_cache, caplog,
):
    version = 10**999
    _write_json(isolated_metadata_cache, {"version": version, "mods": _valid_mods()})

    with caplog.at_level("WARNING"):
        mod_metadata.load_mod_metadata()

    messages = [record.message for record in caplog.records]
    assert len(messages) == 1
    assert "unsupported schema version" in messages[0]
    assert "approximately 1000 digits" in messages[0]
    assert len(messages[0]) < 300
    assert "0" * 100 not in messages[0]


@pytest.mark.parametrize(
    ("version", "summary"),
    [
        ("v" * 5000, "<str, 5000 characters>"),
        (list(range(1000)), "<list, 1000 items>"),
        ({str(index): index for index in range(1000)}, "<dict, 1000 items>"),
    ],
)
def test_huge_malformed_version_warning_is_bounded(
    isolated_metadata_cache, caplog, version, summary,
):
    _write_json(isolated_metadata_cache, {"version": version, "mods": _valid_mods()})

    with caplog.at_level("WARNING"):
        loaded = mod_metadata.load_mod_metadata()

    messages = [record.message for record in caplog.records]
    assert set(loaded["mods"]) == {"101", "102"}
    assert len(messages) == 1
    assert "malformed version" in messages[0]
    assert summary in messages[0]
    assert len(messages[0]) < 300


def test_bad_entry_and_modu004_name_revalidation_remain_entry_local(
    isolated_metadata_cache,
):
    mods = _valid_mods()
    mods.update({
        "103": "malformed entry",
        "104": {"id": 104, "name": "\x00\u202e\u200b", "unknown": {"kept": True}},
        "not-an-id": {"id": 105, "name": "Café فارسی\u200c 👩\u200d💻"},
    })
    _write_json(isolated_metadata_cache, {"version": 1, "mods": mods})
    original = isolated_metadata_cache.read_bytes()

    loaded = mod_metadata.load_mod_metadata()

    assert loaded["mods"]["101"]["name"] == "Alpha"
    assert loaded["mods"]["103"] == "malformed entry"
    assert "name" not in loaded["mods"]["104"]
    assert loaded["mods"]["104"]["unknown"] == {"kept": True}
    assert loaded["mods"]["not-an-id"]["name"] == "Café فارسی\u200c 👩\u200d💻"
    assert isolated_metadata_cache.read_bytes() == original


def _raise_for_metadata_read(monkeypatch, metadata_path, error, *, once=False):
    real_open = builtins.open
    calls = 0
    calls_lock = threading.Lock()

    def injected_open(file, *args, **kwargs):
        nonlocal calls
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if os.fspath(file) == os.fspath(metadata_path) and "r" in mode:
            with calls_lock:
                should_raise = not once or calls == 0
                calls += 1
            if should_raise:
                raise error
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", injected_open)


@pytest.mark.parametrize("error", [OSError("transient"), PermissionError("denied")])
def test_read_io_failure_aborts_mutation_without_replacing_cache(
    isolated_metadata_cache, monkeypatch, caplog, error,
):
    original_data = {"version": 1, "mods": _valid_mods()}
    _write_json(isolated_metadata_cache, original_data)
    original_bytes = isolated_metadata_cache.read_bytes()
    _raise_for_metadata_read(monkeypatch, isolated_metadata_cache, error)

    with caplog.at_level("WARNING"), pytest.raises(mod_metadata.MetadataCacheReadError):
        mod_metadata.upsert_mod_metadata(103, name="Gamma")

    assert isolated_metadata_cache.read_bytes() == original_bytes
    assert not list(isolated_metadata_cache.parent.glob("mod_metadata.*.tmp"))
    assert any("Could not read mod metadata cache" in record.message for record in caplog.records)


def test_standalone_transient_failure_and_successful_retry_preserve_entries(
    isolated_metadata_cache, monkeypatch,
):
    _write_json(isolated_metadata_cache, {"version": 1, "mods": _valid_mods()})
    original_bytes = isolated_metadata_cache.read_bytes()
    _raise_for_metadata_read(
        monkeypatch, isolated_metadata_cache, OSError("transient"), once=True,
    )

    with pytest.raises(mod_metadata.MetadataCacheReadError):
        mod_metadata.load_mod_metadata()
    assert isolated_metadata_cache.read_bytes() == original_bytes

    mod_metadata.upsert_mod_metadata(103, name="Gamma")
    assert set(mod_metadata.load_mod_metadata()["mods"]) == {"101", "102", "103"}


def test_failed_read_then_later_concurrent_writer_has_no_stale_overwrite(
    isolated_metadata_cache, monkeypatch,
):
    _write_json(isolated_metadata_cache, {"version": 1, "mods": _valid_mods()})
    _raise_for_metadata_read(
        monkeypatch, isolated_metadata_cache, OSError("transient"), once=True,
    )
    barrier = threading.Barrier(2)
    failures = []

    def writer(mod_id):
        try:
            barrier.wait(timeout=5)
            mod_metadata.upsert_mod_metadata(mod_id, name=f"Mod {mod_id}")
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=writer, args=(mod_id,)) for mod_id in (103, 104)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], mod_metadata.MetadataCacheReadError)
    loaded = mod_metadata.load_mod_metadata()
    assert set(loaded["mods"]) in ({"101", "102", "103"}, {"101", "102", "104"})
    assert all(not thread.is_alive() for thread in threads)
