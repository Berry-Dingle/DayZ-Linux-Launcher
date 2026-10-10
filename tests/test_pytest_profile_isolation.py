"""Regression coverage for the pre-collection profile boundary."""

import os
import pwd
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dzll_launcher import config, db, settings
from dzll_launcher import companion_learning_transfer_ui
from dzll_launcher import mod_metadata, mods_ui, storage, window


def test_import_time_paths_use_test_home():
    home = Path.home()
    assert home.name.startswith("dzll-pytest-home-")
    assert os.environ["HOME"] == str(home)
    for key, suffix in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        assert Path(os.environ[key]) == home / suffix
    for value in (
        config.CFG_DIR, settings.SETTINGS_PATH, storage.FAV_PATH,
        storage.LAST_PLAYED_PATH, storage.LAST_COMPANION_SERVER_PATH,
        storage.COMPANION_RESTART_LEARNING_PATH,
        window.COMPANION_RESTART_LEARNING_PHASE2_PATH,
        companion_learning_transfer_ui.CFG_DIR,
        config.DB_LOCAL_PATH, db.DB_LOCAL_PATH,
        mod_metadata.MOD_METADATA_PATH, mods_ui.PENDING_MOD_DELETES_PATH,
    ):
        assert Path(value).is_relative_to(home)


def test_settings_write_and_delayed_save_stay_in_test_home(tmp_path, monkeypatch):
    entered = threading.Event()
    proceed = threading.Event()

    def delayed_save():
        entered.set()
        assert proceed.wait(5)
        settings.save_settings({"ingame_name": "Delayed test"})

    with monkeypatch.context() as patch:
        patch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "temporary.json"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(delayed_save)
            assert entered.wait(5)
            # Restore the per-test alias while the worker remains alive.
            patch.undo()
            proceed.set()
            future.result(timeout=5)
    assert Path(settings.SETTINGS_PATH).is_relative_to(Path.home())
    assert settings.load_settings()["ingame_name"] == "Delayed test"


def test_database_write_stays_in_test_home(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    columns = (
        "ip", "gport", "qport", "name", "map", "players", "maxPlayers",
        "password", "mods", "modCount", "third_person", "timeWarp",
        "time", "country", "ping", "bm_rank",
    )
    connection.execute("CREATE TABLE servers (" + ", ".join(columns) + ")")
    connection.execute(
        "INSERT INTO servers VALUES (" + ",".join("?" for _ in columns) + ")",
        ("127.0.0.1", 2302, 2303, "Fixture", "chernarusplus", 1, 60,
         0, "[]", 0, 1, 1, "12:00", "GB", 10, 1),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "_fetch_db_bytes_with_retries", source.read_bytes)
    assert db.fetch_db_overwrite_local()
    assert Path(db.DB_LOCAL_PATH).is_relative_to(Path.home())
    assert len(db.read_servers_from_db()) == 1


def test_real_profile_write_is_rejected_by_audit_hook():
    protected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/dzll/blocked-by-pytest.json"
    script = (
        "import runpy\n"
        "runpy.run_path('tests/conftest.py')\n"
        f"open({str(protected)!r}, 'w').close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True,
        check=False, timeout=10,
    )
    assert result.returncode != 0
    assert "pytest blocked real DZLL profile access" in result.stderr


def test_guard_rejects_reads_replaces_and_sqlite_for_all_real_roots():
    script = """
import os
import pwd
import runpy
import sqlite3
from pathlib import Path

runpy.run_path('tests/conftest.py')
home = Path(pwd.getpwuid(os.getuid()).pw_dir)
roots = ('.config/dzll', '.local/share/dzll', '.local/state/dzll', '.cache/dzll')
source = Path(os.environ['HOME']) / 'replace-source'
source.write_text('safe source')
for suffix in roots:
    target = home / suffix / 'pytest-protected-target'
    operations = (
        lambda: open(target, 'rb'),
        lambda: os.replace(source, target),
        lambda: sqlite3.connect(target),
        lambda: os.mkdir(target),
    )
    for operation in operations:
        try:
            operation()
        except RuntimeError as exc:
            assert 'pytest blocked real DZLL profile access' in str(exc)
        else:
            raise AssertionError(f'guard allowed {target}')
assert source.read_text() == 'safe source'
"""
    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True,
        check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
