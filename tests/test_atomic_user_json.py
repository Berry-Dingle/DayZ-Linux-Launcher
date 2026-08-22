from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dzll_launcher import atomic_json, launcher_state, settings, storage


class _PartialFailureWriter:
    def __init__(self, handle) -> None:
        self._handle = handle

    def write(self, payload: bytes) -> int:
        self._handle.write(payload[: max(1, len(payload) // 2)])
        self._handle.flush()
        raise OSError("injected temporary write failure")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._handle.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name):
        return getattr(self._handle, name)


class _ShortWriter:
    def __init__(self, handle) -> None:
        self._handle = handle

    def write(self, payload: bytes) -> int:
        count = max(1, len(payload) // 2)
        self._handle.write(payload[:count])
        return count

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._handle.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name):
        return getattr(self._handle, name)


def _temp_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def _patch_temp_writer(monkeypatch, wrapper_type) -> None:
    original_fdopen = atomic_json.os.fdopen

    def wrapped_fdopen(fd, mode):
        return wrapper_type(original_fdopen(fd, mode))

    monkeypatch.setattr(atomic_json.os, "fdopen", wrapped_fdopen)


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    paths = {
        "settings": config / "settings.json",
        "favorites": config / "favorites.json",
        "last_played": config / "last_played.json",
        "last_companion": config / "last_companion_server.json",
        "dead": cache / "dead.json",
    }
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(paths["settings"]))
    monkeypatch.setattr(storage, "FAV_PATH", str(paths["favorites"]))
    monkeypatch.setattr(storage, "LAST_PLAYED_PATH", str(paths["last_played"]))
    monkeypatch.setattr(storage, "LAST_COMPANION_SERVER_PATH", str(paths["last_companion"]))
    monkeypatch.setattr(storage, "DEAD_PATH", str(paths["dead"]))
    monkeypatch.setattr(storage, "CACHE_DIR", str(cache))
    return paths


def test_settings_serialization_failure_preserves_exact_baseline(isolated_paths):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    baseline = b'{"ingame_name":"Baseline","unknown":"preserved"}'
    path.write_bytes(baseline)

    with pytest.raises(TypeError):
        settings.save_settings({"ingame_name": object()})

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_settings_temporary_write_failure_preserves_exact_baseline(
    isolated_paths, monkeypatch
):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    baseline = b'{"ingame_name":"Baseline"}'
    path.write_bytes(baseline)
    _patch_temp_writer(monkeypatch, _PartialFailureWriter)

    with pytest.raises(OSError, match="temporary write failure"):
        settings.save_settings({"ingame_name": "Replacement"})

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_settings_short_write_is_rejected_without_replacement(
    isolated_paths, monkeypatch
):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    baseline = b'{"ingame_name":"Baseline"}'
    path.write_bytes(baseline)
    _patch_temp_writer(monkeypatch, _ShortWriter)

    with pytest.raises(OSError, match="short JSON write"):
        settings.save_settings({"ingame_name": "Replacement"})

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_settings_replace_failure_preserves_exact_baseline(
    isolated_paths, monkeypatch
):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    baseline = b'{"ingame_name":"Baseline"}'
    path.write_bytes(baseline)
    monkeypatch.setattr(
        atomic_json.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
    )

    with pytest.raises(OSError, match="replace failure"):
        settings.save_settings({"ingame_name": "Replacement"})

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_settings_file_fsync_failure_preserves_exact_baseline(
    isolated_paths, monkeypatch
):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    baseline = b'{"ingame_name":"Baseline"}'
    path.write_bytes(baseline)
    monkeypatch.setattr(
        atomic_json.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )

    with pytest.raises(OSError, match="fsync failure"):
        settings.save_settings({"ingame_name": "Replacement"})

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_successful_settings_save_preserves_schema_and_format(isolated_paths):
    path = isolated_paths["settings"]
    supplied = dict(settings.DEFAULTS)
    supplied["ingame_name"] = "Survivor 日本"
    supplied["show_background_download_buttons"] = True

    settings.save_settings(supplied)

    expected = json.dumps(supplied, indent=2, sort_keys=True).encode("utf-8")
    assert path.read_bytes() == expected
    assert settings.load_settings() == supplied
    assert _temp_files(path) == []


@pytest.mark.parametrize(
    "label,baseline,replacement,saver",
    [
        ("favorites", {"one": True}, {"two": True}, storage.save_favorites),
        (
            "last_played",
            {"one": int(time.time())},
            {"two": int(time.time())},
            storage.save_last_played,
        ),
        (
            "last_companion",
            {"ip": "1.2.3.4", "gport": 2302},
            {"ip": "5.6.7.8", "gport": 2302},
            storage.save_last_companion_server,
        ),
        (
            "dead",
            {"one": {"fail_count": 1, "dead_until": 2, "last_fail": 3}},
            {"two": {"fail_count": 4, "dead_until": 5, "last_fail": 6}},
            storage.save_dead_cache,
        ),
    ],
)
def test_storage_wrappers_preserve_baseline_on_temporary_write_failure(
    isolated_paths, monkeypatch, label, baseline, replacement, saver
):
    path = isolated_paths[label]
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline_bytes = json.dumps(baseline, sort_keys=True).encode("utf-8")
    path.write_bytes(baseline_bytes)
    _patch_temp_writer(monkeypatch, _PartialFailureWriter)

    with pytest.raises(OSError, match="temporary write failure"):
        saver(replacement)

    assert path.read_bytes() == baseline_bytes
    assert _temp_files(path) == []


def test_storage_success_preserves_existing_format(tmp_path):
    path = tmp_path / "storage.json"
    value = {"unicode": "Café 日本", "nested": {"z": 2, "a": 1}}

    storage.save_json_dict(str(path), value)

    assert path.read_bytes() == json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    assert storage.load_json_dict(str(path)) == value


def test_local_json_write_failure_preserves_unknown_existing_state(
    tmp_path, monkeypatch
):
    path = tmp_path / "pfx" / "Local.json"
    path.parent.mkdir(parents=True)
    baseline_value = {
        "autodetectionDirectories": ["C:\\Existing"],
        "dateCreated": "/Date(1000)/",
        "knownLocalMods": ["C:\\Existing\\@Mod"],
        "userDirectories": ["C:\\UserSelected"],
        "unknownPreservedField": {"value": "Café 日本"},
    }
    baseline = json.dumps(baseline_value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    path.write_bytes(baseline)
    _patch_temp_writer(monkeypatch, _PartialFailureWriter)

    with pytest.raises(OSError, match="temporary write failure"):
        launcher_state.write_local_json(
            local_json_path=str(path),
            watch_folder_win="C:\\DZLLMods",
            known_local_mods_win=["C:\\DZLLMods\\@CF"],
        )

    assert path.read_bytes() == baseline
    assert _temp_files(path) == []


def test_successful_local_json_preserves_merge_format_and_unicode(tmp_path):
    path = tmp_path / "pfx" / "Local.json"
    path.parent.mkdir(parents=True)
    baseline_value = {
        "autodetectionDirectories": ["C:\\Existing"],
        "dateCreated": "/Date(1000)/",
        "knownLocalMods": ["C:\\Old"],
        "userDirectories": ["C:\\ユーザー"],
        "unknownPreservedField": "Café 日本",
    }
    path.write_text(
        json.dumps(baseline_value, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )

    launcher_state.write_local_json(
        local_json_path=str(path),
        watch_folder_win="C:\\DZLLMods-日本",
        known_local_mods_win=["C:\\DZLLMods-日本\\@Café"],
    )

    expected = launcher_state.build_local_json(
        watch_folder_win="C:\\DZLLMods-日本",
        known_local_mods_win=["C:\\DZLLMods-日本\\@Café"],
        existing=baseline_value,
    )
    assert path.read_bytes() == json.dumps(
        expected, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert launcher_state._json_read(str(path)) == expected
    assert b"Caf\xc3\xa9" in path.read_bytes()
    assert "日本" in path.read_text(encoding="utf-8")
    assert expected["unknownPreservedField"] == "Café 日本"
    assert expected["dateCreated"] == "/Date(1000)/"
    assert expected["userDirectories"] == ["C:\\ユーザー"]


def test_failed_saves_use_unique_temporary_names_and_clean_them(
    isolated_paths, monkeypatch
):
    path = isolated_paths["settings"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"ingame_name":"Baseline"}')
    original_mkstemp = atomic_json.tempfile.mkstemp
    names: list[str] = []

    def observed_mkstemp(*args, **kwargs):
        fd, name = original_mkstemp(*args, **kwargs)
        names.append(name)
        return fd, name

    monkeypatch.setattr(atomic_json.tempfile, "mkstemp", observed_mkstemp)
    monkeypatch.setattr(
        atomic_json.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failure")),
    )

    for value in ("First", "Second"):
        with pytest.raises(OSError, match="replace failure"):
            settings.save_settings({"ingame_name": value})

    assert len(names) == 2
    assert names[0] != names[1]
    assert _temp_files(path) == []
