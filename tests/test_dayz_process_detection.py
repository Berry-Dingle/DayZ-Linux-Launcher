import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import window as window_module
from dzll_launcher.dayz_process import DayZProcessSnapshot, scan_dayz_processes
from dzll_launcher.join_attempt import JoinAttemptTracker
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.window import DZLLWindow


def _proc_stat(pid: int, comm: str, start_time: int) -> bytes:
    fields_3_through_21 = ["R", *("0" for _ in range(18))]
    return (
        f"{pid} ({comm}) {' '.join(fields_3_through_21)} {start_time}\n"
    ).encode("ascii")


def _write_process(
        root: Path, pid: int, *, argv, comm: str, executable: str,
        environment=None, start_time: int | None = None) -> Path:
    entry = root / str(pid)
    entry.mkdir()
    raw_argv = b"\x00".join(os.fsencode(value) for value in argv)
    (entry / "cmdline").write_bytes(raw_argv + (b"\x00" if raw_argv else b""))
    (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (entry / "stat").write_bytes(_proc_stat(pid, comm, start_time or (1000 + pid)))
    raw_environment = b"\x00".join(
        f"{key}={value}".encode("ascii")
        for key, value in (environment or {}).items()
    )
    (entry / "environ").write_bytes(
        raw_environment + (b"\x00" if raw_environment else b"")
    )
    (entry / "exe").symlink_to(executable)
    return entry


@pytest.mark.parametrize("process", [
    {
        "argv": ["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZ_x64.exe"],
        "comm": "wine64-preloader",
        "executable": "/proton/files/bin/wine64-preloader",
        "expected": "dayz",
    },
    {
        "argv": ["/proton/files/bin/wine", "Z:\\DayZ\\DayZ.exe"],
        "comm": "wine",
        "executable": "/proton/files/bin/wine",
        "expected": "dayz",
    },
    {
        "argv": ["/proton/files/bin/wine64-preloader", "C:\\DayZ\\DayZLauncher.exe"],
        "comm": "wine64-preloader",
        "executable": "/proton/files/bin/wine64-preloader",
        "expected": "launcher",
    },
    {
        "argv": ["/usr/bin/python3", "/steam/steamapps/common/Proton/proton",
                 "waitforexitandrun", "Z:\\DayZ\\DayZ_x64.exe"],
        "comm": "python3",
        "executable": "/usr/bin/python3",
        "environment": {"SteamAppId": "221100"},
        "expected": "dayz",
    },
    {
        "argv": ["/proton/files/bin/wine64-preloader"],
        "comm": "DayZ_x64.exe",
        "executable": "/proton/files/bin/wine64-preloader",
        "expected": "dayz",
    },
    {
        "argv": ["/proton/files/bin/wine64-preloader"],
        "comm": "DayZLauncher.ex",
        "executable": "/proton/files/bin/wine64-preloader",
        "expected": "launcher",
    },
])
def test_legitimate_wine_proton_and_direct_comm_forms(tmp_path, process):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(proc_root, 101, **{
        key: value for key, value in process.items() if key != "expected"
    })

    snapshot = scan_dayz_processes(proc_root)

    assert snapshot.dayz_running is (process["expected"] == "dayz")
    assert snapshot.launcher_running is (process["expected"] == "launcher")


@pytest.mark.parametrize("argv,comm,executable", [
    (["/usr/bin/python3", "script.py", "DayZ_x64.exe"], "python3", "/usr/bin/python3"),
    (["/usr/bin/bash", "script.sh", "DayZ.exe"], "bash", "/usr/bin/bash"),
    (["/usr/bin/editor", "DayZLauncher.exe"], "editor", "/usr/bin/editor"),
    (["/usr/bin/tail", "/tmp/notes-DayZ_x64.exe.log"], "tail", "/usr/bin/tail"),
    (["/usr/bin/editor", "/tmp/notes-DayZLauncher.exe.log"], "editor", "/usr/bin/editor"),
    (["/usr/bin/python3", "DayZ_x64Xexe"], "python3", "/usr/bin/python3"),
    (["/usr/bin/python3", "DayZLauncherXexe"], "python3", "/usr/bin/python3"),
    (["/usr/bin/python3", "DayZ Launcher"], "python3", "/usr/bin/python3"),
    (["/usr/bin/python3", "some-DayZLauncher-value"], "python3", "/usr/bin/python3"),
    (["/proton/files/bin/wine", "'DayZ.exe'"], "wine", "/proton/files/bin/wine"),
    (["/proton/files/bin/wine", " DayZ.exe "], "wine", "/proton/files/bin/wine"),
    (["/proton/files/bin/wine"], " DayZ_x64.exe ", "/proton/files/bin/wine"),
])
def test_unrelated_marker_bearing_processes_are_rejected(
        tmp_path, argv, comm, executable):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101, argv=argv, comm=comm, executable=executable,
    )

    assert scan_dayz_processes(proc_root) == DayZProcessSnapshot()


def test_exact_target_token_needs_valid_corroboration(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "script.py", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
        environment={"SteamAppId": "999999"},
    )
    _write_process(
        proc_root, 102,
        argv=["/usr/bin/python3", "script.py", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
        environment={"SteamGameId": "221100"},
    )

    snapshot = scan_dayz_processes(proc_root)

    assert snapshot.dayz_pids == (102,)


@pytest.mark.parametrize("appid_value,expected", [
    ("221100", True),
    (" 221100", False),
    ("221100 ", False),
    ("\t221100", False),
    ("221100\n", False),
    ('"221100"', False),
    ("x221100", False),
    ("221100x", False),
    ("221100  ", False),
    ("  221100  ", False),
    ("221 100", False),
])
def test_appid_corroboration_requires_exact_raw_value(
        tmp_path, appid_value, expected):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "script.py", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
        environment={"SteamAppId": appid_value},
    )

    assert scan_dayz_processes(proc_root).dayz_running is expected


def test_unrelated_non_ascii_environment_does_not_hide_app_id(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    entry = _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "proton", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
    )
    (entry / "environ").write_bytes(
        b"DISPLAY_NAME=\xff\x00SteamAppId=221100\x00"
    )

    assert scan_dayz_processes(proc_root).dayz_pids == (101,)


def test_empty_malformed_missing_and_unreadable_processes_fail_closed(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    empty = _write_process(
        proc_root, 101, argv=[], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )
    malformed = _write_process(
        proc_root, 102, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )
    (malformed / "cmdline").write_bytes(b"DayZ_x64.exe")
    missing = _write_process(
        proc_root, 103, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )
    (missing / "comm").unlink()
    unreadable = _write_process(
        proc_root, 104, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )
    valid = _write_process(
        proc_root, 105, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )
    original_read_bytes = Path.read_bytes

    def controlled_read(path):
        if path == unreadable / "cmdline":
            raise PermissionError("synthetic unreadable process")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", controlled_read)

    assert scan_dayz_processes(proc_root).dayz_pids == (105,)
    assert empty.exists()


def test_pid_start_time_change_is_rejected(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    entry = _write_process(
        proc_root, 101, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader", start_time=1001,
    )
    original_read_bytes = Path.read_bytes
    stat_reads = 0

    def reused_pid(path):
        nonlocal stat_reads
        if path == entry / "stat":
            stat_reads += 1
            if stat_reads == 2:
                return _proc_stat(101, "DayZ_x64.exe", 2002)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reused_pid)

    assert scan_dayz_processes(proc_root) == DayZProcessSnapshot()


def test_other_user_process_is_rejected(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101, argv=["DayZ_x64.exe"], comm="DayZ_x64.exe",
        executable="/proton/files/bin/wine64-preloader",
    )

    snapshot = scan_dayz_processes(
        proc_root, expected_uid=os.geteuid() + 1,
    )

    assert snapshot == DayZProcessSnapshot()


def test_multiple_processes_keep_only_genuine_candidates(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101, argv=["/usr/bin/python3", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
    )
    _write_process(
        proc_root, 102, argv=["/usr/bin/editor", "DayZLauncher.exe"],
        comm="editor", executable="/usr/bin/editor",
    )
    _write_process(
        proc_root, 201,
        argv=["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZ_x64.exe"],
        comm="wine64-preloader", executable="/proton/files/bin/wine64-preloader",
    )
    _write_process(
        proc_root, 202,
        argv=["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZLauncher.exe"],
        comm="wine64-preloader", executable="/proton/files/bin/wine64-preloader",
    )

    snapshot = scan_dayz_processes(proc_root)

    assert snapshot.dayz_pids == (201,)
    assert snapshot.launcher_pids == (202,)


class _Button:
    def set_label(self, _value):
        pass

    def set_visible(self, _value):
        pass


class _Clock:
    def __init__(self, advances):
        self.now = 0.0
        self.advances = list(advances)

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, _seconds):
        if not self.advances:
            raise AssertionError("unexpected extra watcher poll")
        self.now += float(self.advances.pop(0))


class _WatcherHost:
    def __init__(self, snapshot_fn, *, skip_launcher=True):
        self.snapshot_fn = snapshot_fn
        self._join_attempts = JoinAttemptTracker(log_sink=lambda _line: None)
        self.attempt = self._join_attempts.begin(
            ip="192.0.2.1", game_port=2302, query_port=2303,
            name="Synthetic", skip_dayz_launcher=skip_launcher,
        )
        self._discord_watch_lock = __import__("threading").Lock()
        self._discord_watch_active = False
        self._discord = SimpleNamespace(events=[], set_menu=lambda: None)
        self.settings = {"discord_detail_level": "ingame"}
        self._discord_last_join = None
        self.steamcmd_cancel_btn = _Button()
        self.process_presentations = []
        self.errors = []
        self.cleanups = []
        self.companion_activations = []
        self._pending_join_attempt_id = self.attempt.attempt_id
        self._pending_server_companion_obj = "pending-server"
        self._pending_last_played_obj = None
        self._pending_join_mod_ids = []
        self._pending_join_mod_names_by_id = {}

    def _dayz_process_snapshot(self):
        return self.snapshot_fn()

    def _join_attempt_is_active(self, attempt_id):
        return self._join_attempts.matches(attempt_id)

    def _join_log(self, attempt_id, event, **fields):
        return self._join_attempts.log(attempt_id, event, **fields)

    def _join_watcher_ui_call(self, callback, *args):
        return callback(*args)

    def _join_popup_process_detected(self, attempt_id, process):
        self.process_presentations.append(str(process))
        return True

    def _join_popup_watcher_failure(self, attempt_id, reason):
        self.errors.append(str(reason))
        self._cleanup_join_attempt(attempt_id, "watcher terminal failure")
        return True

    def _clear_join_pending_state(self, attempt_id):
        return DZLLWindow._clear_join_pending_state(self, attempt_id)

    def _cleanup_join_attempt(self, attempt_id, reason, *, clear_pending=True):
        self.cleanups.append(str(reason))
        cleaned = self._join_attempts.cleanup(attempt_id, reason)
        if cleaned and clear_pending:
            self._clear_join_pending_state(attempt_id)
        return cleaned

    def set_server_companion_server(self, server):
        self.companion_activations.append(server)
        return False


def _run_watcher(monkeypatch, host, clock):
    monkeypatch.setattr(window_module.time, "time", clock.time)
    monkeypatch.setattr(window_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(window_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        window_module.GLib, "idle_add", lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(window_module, "is_native_steam_running", lambda: False)
    DZLLWindow._watch_dayz_session_until_exit(host, host.attempt.attempt_id)


def test_unrelated_exact_dayz_argument_cannot_complete_join(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "script.py", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
    )
    host = _WatcherHost(lambda: scan_dayz_processes(proc_root))

    _run_watcher(monkeypatch, host, _Clock([120.0]))

    assert host.process_presentations == []
    assert host.cleanups == ["watcher terminal failure"]
    assert host.companion_activations == []
    assert host._pending_join_attempt_id == 0
    assert not hasattr(host._discord, "_mode")


def test_legitimate_dayz_identity_completes_join_and_exit_uses_same_classifier(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZ_x64.exe"],
        comm="wine64-preloader", executable="/proton/files/bin/wine64-preloader",
    )
    snapshots = [scan_dayz_processes(proc_root), DayZProcessSnapshot()]
    host = _WatcherHost(lambda: snapshots.pop(0))

    _run_watcher(monkeypatch, host, _Clock([]))

    assert host.process_presentations == ["DayZ"]
    assert host.cleanups == ["DayZ detected"]
    assert host.companion_activations == ["pending-server"]
    assert snapshots == []


@pytest.mark.parametrize("argument", ["DayZLauncher.exe", "DayZ Launcher"])
def test_unrelated_launcher_text_keeps_initial_120_second_timeout(
        tmp_path, monkeypatch, argument):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "script.py", argument],
        comm="python3", executable="/usr/bin/python3",
    )
    host = _WatcherHost(
        lambda: scan_dayz_processes(proc_root), skip_launcher=False,
    )
    clock = _Clock([120.0])

    _run_watcher(monkeypatch, host, clock)

    assert clock.now == 120.0
    assert host.process_presentations == []
    assert host.cleanups == ["watcher terminal failure"]


def test_legitimate_launcher_identity_starts_outer_deadline(
        tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _write_process(
        proc_root, 101,
        argv=["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZLauncher.exe"],
        comm="wine64-preloader", executable="/proton/files/bin/wine64-preloader",
    )
    host = _WatcherHost(
        lambda: scan_dayz_processes(proc_root), skip_launcher=False,
    )
    clock = _Clock([1800.0])

    _run_watcher(monkeypatch, host, clock)

    assert clock.now == 1800.0
    assert host.process_presentations == ["DayZ Launcher"]
    assert "30 minutes" in host.errors[0]
    assert host.cleanups == ["watcher terminal failure"]


def test_mod_manager_repair_uses_strict_owner_predicate(tmp_path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    unrelated = _write_process(
        proc_root, 101,
        argv=["/usr/bin/python3", "script.py", "DayZ_x64.exe"],
        comm="python3", executable="/usr/bin/python3",
    )
    owner = SimpleNamespace(
        _dayz_process_snapshot=lambda: scan_dayz_processes(proc_root),
    )
    owner._dayz_game_running = lambda: DZLLWindow._dayz_game_running(owner)
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay.host = SimpleNamespace(_win=owner)

    assert not overlay._dayz_running_for_repair()

    for child in unrelated.iterdir():
        child.unlink()
    unrelated.rmdir()
    _write_process(
        proc_root, 201,
        argv=["/proton/files/bin/wine64-preloader", "Z:\\DayZ\\DayZ_x64.exe"],
        comm="wine64-preloader", executable="/proton/files/bin/wine64-preloader",
    )

    assert overlay._dayz_running_for_repair()
