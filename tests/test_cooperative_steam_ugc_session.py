import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import steam_client_mods, steam_ugc_backend, steam_ugc_helper


class QueueStdout:
    def __init__(self):
        self.lines = queue.Queue()

    def push(self, event):
        self.lines.put(json.dumps(event) + "\n")

    def close(self):
        self.lines.put(None)

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class ProtocolProcess:
    next_pid = 7100

    def __init__(self, temp_dir):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = None
        self.stdout = QueueStdout()
        self.stderr = io.StringIO("")
        self.commands = []
        self.signals = []
        self.stdin_closed = False
        self.stdin = SimpleNamespace(
            write=self.write,
            flush=lambda: None,
            close=self.close_stdin,
        )
        self.stdout.push(
            {
                "type": "session_starting",
                "request_id": None,
                "helper_pid": self.pid,
                "temp_dir": str(temp_dir),
            }
        )
        self.stdout.push(
            {
                "type": "session_ready",
                "request_id": None,
                "helper_pid": self.pid,
            }
        )

    def write(self, raw):
        message = json.loads(raw)
        self.commands.append(message)
        request_id = message["request_id"]
        command = message["command"]
        if command == "shutdown":
            self.stdout.push(
                {
                    "type": "command_accepted",
                    "request_id": request_id,
                    "command": command,
                }
            )
            self.stdout.push(
                {
                    "type": "shutdown_complete",
                    "request_id": request_id,
                    "ok": True,
                    "reason": "shutdown",
                    "steamapi_shutdown": True,
                    "temp_cleanup": True,
                }
            )
            self.returncode = 0
            self.stdout.close()
        elif command == "cancel":
            self.stdout.push(
                {
                    "type": "cancellation_ack",
                    "request_id": request_id,
                    "target_request_id": message["target_request_id"],
                }
            )
            self.stdout.push(
                {
                    "type": "command_result",
                    "request_id": message["target_request_id"],
                    "ok": False,
                    "cancelled": True,
                }
            )
        else:
            self.stdout.push(
                {
                    "type": "command_accepted",
                    "request_id": request_id,
                    "command": command,
                }
            )
            for item_id in message["item_ids"]:
                self.stdout.push(
                    {
                        "type": "item",
                        "request_id": request_id,
                        "id": item_id,
                        "installed": True,
                        "needs_update": False,
                        "downloading": False,
                        "download_pending": False,
                    }
                )
            self.stdout.push(
                {
                    "type": "command_result",
                    "request_id": request_id,
                    "command": command,
                    "ok": True,
                }
            )
        return len(raw)

    def close_stdin(self):
        self.stdin_closed = True

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None:
            raise steam_ugc_backend.subprocess.TimeoutExpired("session", timeout)
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")
        self.returncode = -15
        self.stdout.close()

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9
        self.stdout.close()


class DelayedOneShotStdout:
    def __init__(self, lines, *, delay=0.0, release=None):
        self._lines = list(lines)
        self._delay = float(delay)
        self._release = release

    def __iter__(self):
        if self._release is not None:
            self._release.wait()
        elif self._delay:
            time.sleep(self._delay)
        yield from self._lines


class OneShotProcess:
    next_pid = 7200

    def __init__(self, lines, *, delay=0.0, release=None):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = 0
        self.stdout = DelayedOneShotStdout(
            lines, delay=delay, release=release,
        )
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode


class DelayedShutdownStdout:
    def __init__(self):
        self._lines = queue.Queue()
        self._closed = False

    def push(self, raw, *, delay=0.0):
        self._lines.put((float(delay), str(raw)))

    def close(self):
        if not self._closed:
            self._closed = True
            self._lines.put(None)

    def __iter__(self):
        while True:
            item = self._lines.get()
            if item is None:
                return
            delay, raw = item
            if delay:
                time.sleep(delay)
            yield raw


class DelayedShutdownProcess:
    next_pid = 7300

    def __init__(self, terminal_event, *, delay=0.2):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = None
        self.stdout = DelayedShutdownStdout()
        self.stderr = io.StringIO("")
        self._terminal_event = terminal_event
        self._delay = float(delay)
        self.stdin = SimpleNamespace(
            write=self.write,
            flush=lambda: None,
            close=lambda: None,
        )

    def write(self, raw):
        message = json.loads(raw)
        request_id = message["request_id"]
        assert message["command"] == "shutdown"
        self.stdout.push(json.dumps({
            "type": "command_accepted",
            "request_id": request_id,
            "command": "shutdown",
        }) + "\n")
        terminal = (
            self._terminal_event(request_id)
            if callable(self._terminal_event) else self._terminal_event
        )
        self.stdout.push(
            terminal if isinstance(terminal, str) else json.dumps(terminal) + "\n",
            delay=self._delay,
        )
        self.returncode = 0
        self.stdout.close()
        return len(raw)

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        del timeout
        return self.returncode


def run_one_shot_process(monkeypatch, process, *, on_event=None):
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    return steam_ugc_backend._run_helper_json_lines(
        "readiness", appid=221100, timeout=2, mod_ids=[],
        on_event=on_event, strict_native_environment=True,
    )


def test_parent_reuses_one_helper_for_multiple_commands_and_cleans_temp(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_test_"))
    (appid_dir / "steam_appid.txt").write_text("221100\n")
    processes = []

    def popen(*_args, **_kwargs):
        process = ProtocolProcess(appid_dir)
        processes.append(process)
        return process

    monkeypatch.setattr(steam_ugc_backend.subprocess, "Popen", popen)
    session = steam_ugc_backend.CooperativeUGCSession()
    steam_ugc_backend.activate_ugc_session(session)
    events = []
    try:
        assert steam_ugc_backend._run_helper_json_lines(
            "state", appid=221100, timeout=2, mod_ids=[1],
            on_event=events.append,
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "state", appid=221100, timeout=2, mod_ids=[2],
            on_event=events.append,
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "refresh-subscribed-state", appid=221100, timeout=2, mod_ids=[2],
            on_event=events.append,
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "subscribe-download", appid=221100, timeout=2, mod_ids=[2],
            on_event=events.append,
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "unsubscribe", appid=221100, timeout=2, mod_ids=[2],
            on_event=events.append,
        )[0]
    finally:
        steam_ugc_backend.deactivate_ugc_session(session)
        session.close()

    assert len(processes) == 1
    assert [message["command"] for message in processes[0].commands] == [
        "query_state", "query_state", "refresh_subscribed_state",
        "subscribe_download", "unsubscribe", "shutdown",
    ]
    request_ids = [message["request_id"] for message in processes[0].commands]
    assert len(request_ids) == len(set(request_ids))
    assert not appid_dir.exists()
    assert processes[0].signals == []
    assert processes[0].stdin_closed
    assert [event["id"] for event in events if event.get("type") == "item"] == [1, 2, 2, 2, 2]


def test_helper_session_initializes_and_shuts_down_steamapi_once(monkeypatch):
    commands = (
        '{"command":"query_state","request_id":"q-1","item_ids":[7],"timeout":2}\n'
        '{"command":"shutdown","request_id":"s-2"}\n'
    )
    events = []
    counts = {"init": 0, "shutdown": 0}
    temp_path = []

    class FakeSteam:
        ugc_accessor_name = "SteamAPI_SteamUGC_v021"

        def __init__(self, _paths):
            pass

        def init(self):
            counts["init"] += 1

        def shutdown(self):
            counts["shutdown"] += 1

        def snapshot(self, item_id):
            return steam_ugc_helper.ItemSnapshot(
                item_id=item_id,
                state=5,
                state_names=["Subscribed", "Installed"],
                subscribed=True,
                installed=True,
                needs_update=False,
                downloading=False,
                download_pending=False,
                download_bytes=1,
                total_bytes=1,
                size_on_disk=1,
                install_folder="/workshop/7",
            )

    original_setup = steam_ugc_helper.setup_temp_appid

    def setup(appid):
        tmp = original_setup(appid)
        temp_path.append(Path(tmp.name))
        return tmp

    monkeypatch.setattr(steam_ugc_helper.sys, "stdin", io.StringIO(commands))
    monkeypatch.setattr(steam_ugc_helper, "detect_steam_paths", lambda: SimpleNamespace())
    monkeypatch.setattr(steam_ugc_helper, "ensure_ld_library_path", lambda _paths: None)
    monkeypatch.setattr(steam_ugc_helper, "setup_temp_appid", setup)
    monkeypatch.setattr(steam_ugc_helper, "SteamUGC", FakeSteam)
    monkeypatch.setattr(steam_ugc_helper, "emit", events.append)

    args = SimpleNamespace(appid=221100)
    assert steam_ugc_helper.command_session(args) == 0
    assert counts == {"init": 1, "shutdown": 1}
    assert len(temp_path) == 1
    assert not temp_path[0].exists()
    assert [event["type"] for event in events].count("session_ready") == 1
    assert [event["type"] for event in events].count("shutdown_complete") == 1
    shutdown_complete = next(
        event for event in events if event["type"] == "shutdown_complete"
    )
    assert shutdown_complete["request_id"] == "s-2"
    assert shutdown_complete["steamapi_shutdown"]
    assert shutdown_complete["temp_cleanup"]
    assert any(
        event.get("type") == "command_result"
        and event.get("request_id") == "q-1"
        and event.get("ok")
        for event in events
    )


def test_cooperative_unsubscribe_uses_canonical_snapshot_item_id(monkeypatch):
    events = []
    subscribed = {7: True}

    class FakeSteam:
        def unsubscribe(self, item_id):
            subscribed[int(item_id)] = False
            return 1

        def run_callbacks(self):
            return None

        def snapshot(self, item_id):
            return steam_ugc_helper.ItemSnapshot(
                item_id=int(item_id),
                state=0,
                state_names=[],
                subscribed=subscribed[int(item_id)],
                installed=True,
                needs_update=False,
                downloading=False,
                download_pending=False,
                download_bytes=1,
                total_bytes=1,
                size_on_disk=1,
                install_folder="/workshop/7",
            )

    monkeypatch.setattr(
        steam_ugc_helper, "_session_emit",
        lambda request_id, event_type, **fields: events.append(
            {"request_id": request_id, "type": event_type, **fields}
        ),
    )
    monkeypatch.setattr(steam_ugc_helper, "emit", events.append)
    steam_ugc_helper._session_unsubscribe(
        FakeSteam(), queue.Queue(), "u-1", [7], 2,
    )
    item = next(event for event in events if event.get("type") == "item")
    result = next(
        event for event in events
        if event.get("type") == "command_result"
        and event.get("request_id") == "u-1"
    )
    assert item["id"] == 7
    assert result["ok"] is True
    assert result["unsubscribed"] == [7]
    assert result["failed"] == []
    assert not any(event.get("type") == "fatal_session_error" for event in events)


def _cancel_cleanup_snapshot(
    item_id, *, subscribed=True, installed=False, downloading=True,
    download_pending=False,
):
    state_names = []
    if subscribed:
        state_names.append("Subscribed")
    if installed:
        state_names.append("Installed")
    if downloading:
        state_names.append("Downloading")
    if download_pending:
        state_names.append("DownloadPending")
    return steam_ugc_helper.ItemSnapshot(
        item_id=int(item_id),
        state=0,
        state_names=state_names,
        subscribed=bool(subscribed),
        installed=bool(installed),
        needs_update=False,
        downloading=bool(downloading),
        download_pending=bool(download_pending),
        download_bytes=1,
        total_bytes=10,
        size_on_disk=1 if installed else 0,
        install_folder=f"/workshop/{item_id}" if installed else None,
    )


def test_fresh_cancel_cleanup_batches_downloading_and_pending_before_wait():
    state = {
        1: _cancel_cleanup_snapshot(1, downloading=True),
        2: _cancel_cleanup_snapshot(
            2, downloading=False, download_pending=True,
        ),
    }
    operations = []

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def unsubscribe(self, item_id):
            item_id = int(item_id)
            operations.append(("unsubscribe", item_id))
            old = state[item_id]
            state[item_id] = _cancel_cleanup_snapshot(
                item_id, subscribed=False, installed=old.installed,
                downloading=False, download_pending=False,
            )
            return 100 + item_id

        def run_callbacks(self):
            operations.append(("callbacks", None))

    result = steam_ugc_helper._cancel_cleanup_unsubscribe_batch(
        FakeSteam(), [1, 2], timeout=2,
    )
    assert result["attempted"] == [1, 2]
    assert result["confirmed_unsubscribed"] == [1, 2]
    assert result["failed"] == []
    assert result["timed_out"] == []
    first_callbacks = operations.index(("callbacks", None))
    assert operations[:first_callbacks] == [
        ("unsubscribe", 1), ("unsubscribe", 2),
    ]


def test_fresh_cancel_cleanup_retains_installed_and_accepts_already_clean():
    state = {
        1: _cancel_cleanup_snapshot(
            1, subscribed=True, installed=True, downloading=False,
        ),
        2: _cancel_cleanup_snapshot(2, subscribed=True),
        3: _cancel_cleanup_snapshot(3, subscribed=False),
    }
    unsubscribed = []

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def unsubscribe(self, item_id):
            unsubscribed.append(int(item_id))
            return 1

        def run_callbacks(self):
            return None

    result = steam_ugc_helper._cancel_cleanup_unsubscribe_batch(
        FakeSteam(), [1, 2, 3], timeout=0,
    )
    assert result["retained_installed"] == [1]
    assert result["already_unsubscribed"] == [3]
    assert result["attempted"] == [2]
    assert result["timed_out"] == [2]
    assert unsubscribed == [2]


def test_fresh_cancel_cleanup_uses_one_deadline_for_large_batch():
    item_ids = list(range(1, 81))
    state = {
        item_id: _cancel_cleanup_snapshot(item_id, subscribed=True)
        for item_id in item_ids
    }
    monotonic_values = iter((10.0, 12.0))
    operations = []

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def run_callbacks(self):
            operations.append(("callbacks", None))

        def unsubscribe(self, item_id):
            operations.append(("unsubscribe", int(item_id)))
            return 1000 + int(item_id)

    result = steam_ugc_helper._cancel_cleanup_unsubscribe_batch(
        FakeSteam(), item_ids, timeout=2,
        monotonic_fn=lambda: next(monotonic_values),
        sleep_fn=lambda _duration: pytest.fail("expired batch must not sleep"),
    )
    first_callbacks = operations.index(("callbacks", None))
    assert operations[:first_callbacks] == [
        ("unsubscribe", item_id) for item_id in item_ids
    ]
    assert result["timed_out"] == item_ids
    assert result["attempted"] == item_ids


def test_fresh_cancel_cleanup_records_unsubscribe_failure_without_guessing():
    state = {7: _cancel_cleanup_snapshot(7, subscribed=True)}

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def run_callbacks(self):
            return None

        def unsubscribe(self, _item_id):
            raise RuntimeError("request failed")

    result = steam_ugc_helper._cancel_cleanup_unsubscribe_batch(
        FakeSteam(), [7], timeout=2,
    )
    assert result["failed"] == [7]
    assert result["attempted"] == [7]
    assert result["confirmed_unsubscribed"] == []


def test_fresh_cancel_cleanup_backend_forces_one_shot_context(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        steam_ugc_backend, "_supported_native_steam_mutation_state",
        lambda: (True, "native"),
    )

    def fake_helper(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        kwargs["on_event"]({
            "type": "done", "ok": True,
            "candidates": [7], "attempted": [7],
            "confirmed_unsubscribed": [7], "retained_installed": [],
            "already_unsubscribed": [], "failed": [], "timed_out": [],
            "failures": [],
        })
        return True, 0

    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", fake_helper,
    )
    result = steam_ugc_backend.cleanup_cancelled_ugc_subscriptions([7])
    assert observed["command"] == "cancel-cleanup-unsubscribe"
    assert observed["strict_native_environment"] is True
    assert observed["timeout"] == 2.0
    assert result["confirmed_unsubscribed"] == [7]


def test_fresh_cancel_cleanup_command_shuts_down_once(monkeypatch):
    shutdowns = []
    emitted = []
    state = {
        1: _cancel_cleanup_snapshot(1, subscribed=True, installed=True),
        2: _cancel_cleanup_snapshot(2, subscribed=False),
    }

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def unsubscribe(self, _item_id):
            pytest.fail("installed and already-clean items must be retained")

        def run_callbacks(self):
            return None

        def shutdown(self):
            shutdowns.append(True)

    class FakeTemp:
        def cleanup(self):
            return None

    monkeypatch.setattr(
        steam_ugc_helper, "init_steam",
        lambda _appid: (FakeSteam(), SimpleNamespace(), FakeTemp()),
    )
    monkeypatch.setattr(steam_ugc_helper, "emit", emitted.append)
    args = SimpleNamespace(appid=221100, timeout=2.0, item_ids=[1, 2])
    assert steam_ugc_helper.command_cancel_cleanup_unsubscribe(args) == 0
    done = next(event for event in emitted if event.get("type") == "done")
    assert done["retained_installed"] == [1]
    assert done["already_unsubscribed"] == [2]
    assert shutdowns == [True]


def test_cancel_during_submission_stops_later_requests_and_hands_off_attempted(monkeypatch):
    commands = queue.Queue()
    state = {
        item_id: _cancel_cleanup_snapshot(item_id, subscribed=False)
        for item_id in (1, 2, 3)
    }
    subscribe_calls = []
    download_calls = []
    unsubscribe_calls = []

    class FakeSteam:
        def snapshot(self, item_id):
            return state[int(item_id)]

        def subscribe(self, item_id):
            item_id = int(item_id)
            subscribe_calls.append(item_id)
            state[item_id] = _cancel_cleanup_snapshot(item_id, subscribed=True)
            return 100 + item_id

        def download(self, item_id, _high_priority):
            item_id = int(item_id)
            download_calls.append(item_id)
            commands.put({
                "command": "cancel",
                "request_id": "c-1",
                "target_request_id": "d-1",
                "cleanup_item_ids": [1, 2, 3],
            })
            return True

        def unsubscribe(self, item_id):
            item_id = int(item_id)
            unsubscribe_calls.append(item_id)
            state[item_id] = _cancel_cleanup_snapshot(
                item_id, subscribed=False, downloading=False,
            )
            return 200 + item_id

        def run_callbacks(self):
            return None

    monkeypatch.setattr(steam_ugc_helper, "emit", lambda _event: None)
    monkeypatch.setattr(steam_ugc_helper, "_session_emit", lambda *_a, **_k: None)
    with pytest.raises(steam_ugc_helper._SessionCancelled) as caught:
        steam_ugc_helper._session_subscribe_download(
            FakeSteam(), commands, "d-1", [1, 2, 3], 30,
        )
    assert subscribe_calls == [1]
    assert download_calls == [1]
    assert unsubscribe_calls == []
    assert caught.value.cancel_handoff == {
        "parent_allowlisted": [1, 2, 3],
        "helper_subscribe_attempted": [1],
        "cleanup_candidates": [1],
    }


def test_cancel_between_subscribe_and_download_hands_off_subscription(monkeypatch):
    commands = queue.Queue()
    subscribed = []
    downloads = []

    class FakeSteam:
        def snapshot(self, item_id):
            return _cancel_cleanup_snapshot(item_id, subscribed=False)

        def subscribe(self, item_id):
            subscribed.append(int(item_id))
            commands.put({
                "command": "cancel", "request_id": "c-1",
                "target_request_id": "d-1", "cleanup_item_ids": [7, 8],
            })
            return 70

        def download(self, item_id, _high_priority):
            downloads.append(int(item_id))
            return True

    monkeypatch.setattr(steam_ugc_helper, "emit", lambda _event: None)
    monkeypatch.setattr(steam_ugc_helper, "_session_emit", lambda *_a, **_k: None)
    with pytest.raises(steam_ugc_helper._SessionCancelled) as caught:
        steam_ugc_helper._session_subscribe_download(
            FakeSteam(), commands, "d-1", [7, 8], 30,
        )
    assert subscribed == [7]
    assert downloads == []
    assert caught.value.cancel_handoff["cleanup_candidates"] == [7]


def test_protocol_request_id_mismatch_fails_closed(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_bad_protocol_"))

    class BadProtocolProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "shutdown":
                return super().write(raw)
            self.stdout.push(
                {
                    "type": "command_result",
                    "request_id": "wrong-request",
                    "ok": True,
                }
            )
            return len(raw)

    process = BadProtocolProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    with pytest.raises(steam_ugc_backend.UGCSessionError, match="request_id"):
        session.run_command("state", timeout=2, mod_ids=[1])
    session.close()
    assert not appid_dir.exists()


def test_active_session_routes_commands_without_one_shot_process(monkeypatch):
    calls = []

    class FakeSession:
        def run_command(self, command, **kwargs):
            calls.append((command, tuple(kwargs["mod_ids"])))
            return True, None

    monkeypatch.setattr(
        steam_ugc_backend.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("one-shot helper must not start"),
    )
    session = FakeSession()
    steam_ugc_backend.activate_ugc_session(session)
    try:
        assert steam_ugc_backend._run_helper_json_lines(
            "state", appid=221100, timeout=2, mod_ids=[3],
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "subscribe-download", appid=221100, timeout=2, mod_ids=[3],
        )[0]
        assert steam_ugc_backend._run_helper_json_lines(
            "refresh-subscribed-state", appid=221100, timeout=2, mod_ids=[3],
        )[0]
    finally:
        steam_ugc_backend.deactivate_ugc_session(session)
    assert calls == [
        ("state", (3,)),
        ("subscribe-download", (3,)),
        ("refresh-subscribed-state", (3,)),
    ]


def test_one_shot_done_consumed_before_exit_check_remains_successful(monkeypatch):
    process = OneShotProcess([
        json.dumps({"type": "done", "ok": True}) + "\n",
    ])
    assert run_one_shot_process(monkeypatch, process) == (True, 0)


def test_one_shot_done_enqueued_during_reader_join_is_finally_drained(monkeypatch):
    process = OneShotProcess([
        json.dumps({"type": "done", "ok": True}) + "\n",
    ], delay=0.3)
    assert run_one_shot_process(monkeypatch, process) == (True, 0)


def test_one_shot_delayed_malformed_terminal_remains_fail_closed(
        monkeypatch, capsys):
    process = OneShotProcess(["not-json\n"], delay=0.3)
    assert run_one_shot_process(monkeypatch, process) == (False, 0)
    assert "ignoring malformed helper output" in capsys.readouterr().err


def test_one_shot_final_drain_delivers_late_events_before_done(monkeypatch):
    events = []
    process = OneShotProcess([
        json.dumps({"type": "item", "id": 7, "installed": True}) + "\n",
        json.dumps({"type": "done", "ok": True}) + "\n",
    ], delay=0.3)
    assert run_one_shot_process(
        monkeypatch, process, on_event=events.append,
    ) == (True, 0)
    assert [event["type"] for event in events] == ["item", "done"]


def test_one_shot_surviving_protocol_reader_fails_bounded(monkeypatch):
    release = threading.Event()
    process = OneShotProcess([
        json.dumps({"type": "done", "ok": True}) + "\n",
    ], release=release)
    started = time.monotonic()
    try:
        assert run_one_shot_process(monkeypatch, process) == (False, 0)
    finally:
        release.set()
    assert time.monotonic() - started < 1.5


def test_steam_client_boundary_activates_shared_session_on_worker(monkeypatch):
    session = object()
    observed = []

    def run_install(_ids, **_kwargs):
        observed.append(steam_ugc_backend.active_ugc_session())
        return True

    monkeypatch.setattr(steam_client_mods, "run_ugc_install", run_install)
    assert steam_client_mods.run_steam_client_install(
        workshop_dir="/unused",
        mod_ids=[7],
        ugc_session=session,
    )
    assert observed == [session]
    assert steam_ugc_backend.active_ugc_session() is None


def test_steam_client_boundary_borrows_same_session_without_clearing_owner(
        monkeypatch):
    class Session:
        def __init__(self):
            self.commands = []

        def run_command(self, command, **_kwargs):
            self.commands.append(command)
            return True, 0

    session = Session()
    observed = []

    def run_install(_ids, **_kwargs):
        observed.append(steam_ugc_backend.active_ugc_session())
        return steam_ugc_backend._run_helper_json_lines(
            "state", appid=221100, timeout=2, mod_ids=[7],
        )[0]

    monkeypatch.setattr(
        steam_client_mods,
        "run_ugc_install",
        run_install,
    )

    steam_ugc_backend.activate_ugc_session(session)
    try:
        assert steam_client_mods.run_steam_client_install(
            workshop_dir="/unused",
            mod_ids=[7],
            ugc_session=session,
        )
        assert observed == [session]
        assert session.commands == ["state"]
        assert steam_ugc_backend.active_ugc_session() is session
    finally:
        steam_ugc_backend.deactivate_ugc_session(session)


def test_steam_client_boundary_borrow_exception_preserves_owner(monkeypatch):
    session = object()

    def fail_install(*_args, **_kwargs):
        assert steam_ugc_backend.active_ugc_session() is session
        raise RuntimeError("synthetic install failure")

    monkeypatch.setattr(steam_client_mods, "run_ugc_install", fail_install)
    steam_ugc_backend.activate_ugc_session(session)
    try:
        with pytest.raises(RuntimeError, match="synthetic install failure"):
            steam_client_mods.run_steam_client_install(
                workshop_dir="/unused",
                mod_ids=[7],
                ugc_session=session,
            )
        assert steam_ugc_backend.active_ugc_session() is session
    finally:
        steam_ugc_backend.deactivate_ugc_session(session)


def test_steam_client_boundary_rejects_different_active_session(monkeypatch):
    active_session = object()
    supplied_session = object()
    monkeypatch.setattr(
        steam_client_mods,
        "run_ugc_install",
        lambda *_args, **_kwargs: pytest.fail("UGC install must not start"),
    )

    steam_ugc_backend.activate_ugc_session(active_session)
    try:
        with pytest.raises(
            steam_ugc_backend.UGCSessionError,
            match="already active on this worker",
        ):
            steam_client_mods.run_steam_client_install(
                workshop_dir="/unused",
                mod_ids=[7],
                ugc_session=supplied_session,
            )
        assert steam_ugc_backend.active_ugc_session() is active_session
    finally:
        steam_ugc_backend.deactivate_ugc_session(active_session)


def test_steam_client_boundary_owned_activation_cleans_after_exception(monkeypatch):
    session = object()

    def fail_install(*_args, **_kwargs):
        assert steam_ugc_backend.active_ugc_session() is session
        raise RuntimeError("synthetic install failure")

    monkeypatch.setattr(steam_client_mods, "run_ugc_install", fail_install)
    with pytest.raises(RuntimeError, match="synthetic install failure"):
        steam_client_mods.run_steam_client_install(
            workshop_dir="/unused",
            mod_ids=[7],
            ugc_session=session,
        )
    assert steam_ugc_backend.active_ugc_session() is None


def test_steam_client_boundary_without_session_remains_unbound(monkeypatch):
    observed = []
    monkeypatch.setattr(
        steam_client_mods,
        "run_ugc_install",
        lambda *_args, **_kwargs: (
            observed.append(steam_ugc_backend.active_ugc_session()) or True
        ),
    )

    assert steam_client_mods.run_steam_client_install(
        workshop_dir="/unused",
        mod_ids=[7],
        ugc_session=None,
    )
    assert observed == [None]
    assert steam_ugc_backend.active_ugc_session() is None


def test_stop_waiting_wrapper_preserves_outer_session_across_fresh_worker(
        monkeypatch):
    from dzll_launcher.window import DZLLWindow

    session = object()
    outer_thread = threading.get_ident()
    observed = []

    def run_install(_ids, **_kwargs):
        observed.append(
            (
                threading.get_ident(),
                steam_ugc_backend.active_ugc_session(),
            )
        )
        return True

    monkeypatch.setattr(steam_client_mods, "run_ugc_install", run_install)
    host = SimpleNamespace(
        _steam_client_stop_waiting_event=threading.Event(),
        _run_steam_client_install_impl=steam_client_mods.run_steam_client_install,
    )

    steam_ugc_backend.activate_ugc_session(session)
    try:
        assert DZLLWindow._run_steam_client_install_with_stop_waiting(
            host,
            workshop_dir="/unused",
            mod_ids=[7],
            ugc_session=session,
        )
        assert len(observed) == 1
        assert observed[0][1] is session
        assert observed[0][0] != outer_thread
        assert steam_ugc_backend.active_ugc_session() is session
    finally:
        steam_ugc_backend.deactivate_ugc_session(session)


class ObservedStopWaitingEvent:
    def __init__(self, *, pause_first_unset_read=False):
        self._event = threading.Event()
        self.pause_first_unset_read = bool(pause_first_unset_read)
        self.first_unset_read = threading.Event()
        self.release_first_unset_read = threading.Event()
        self.any_read = threading.Event()
        self.set_read = threading.Event()
        self.read_count = 0

    def set(self):
        self._event.set()

    def is_set(self):
        self.read_count += 1
        self.any_read.set()
        snapshot = self._event.is_set()
        if (
            self.pause_first_unset_read
            and self.read_count == 1
            and not snapshot
        ):
            self.first_unset_read.set()
            self.release_first_unset_read.wait()
        if snapshot:
            self.set_read.set()
        return snapshot


def test_stop_waiting_wrapper_keeps_e1_after_window_field_replacement():
    from dzll_launcher.window import DZLLWindow

    e1 = ObservedStopWaitingEvent(pause_first_unset_read=True)
    e2 = ObservedStopWaitingEvent()
    operation_cancel = threading.Event()
    inner_started = threading.Event()
    inner_saw_cancel = threading.Event()
    release_inner_teardown = threading.Event()
    inner_finished = threading.Event()
    wrapper_returned = threading.Event()
    result = []

    def install(*_args, cancel_event, **_kwargs):
        assert cancel_event is operation_cancel
        inner_started.set()
        cancel_event.wait()
        inner_saw_cancel.set()
        release_inner_teardown.wait()
        inner_finished.set()
        return False

    host = SimpleNamespace(
        _steam_client_stop_waiting_event=e1,
        _run_steam_client_install_impl=install,
    )

    def invoke_wrapper():
        result.append(DZLLWindow._run_steam_client_install_with_stop_waiting(
            host,
            cancel_event=operation_cancel,
            stop_waiting_event=e1,
        ))
        wrapper_returned.set()

    wrapper = threading.Thread(target=invoke_wrapper)
    wrapper.start()
    assert inner_started.wait(2)
    assert e1.first_unset_read.wait(2)

    operation_cancel.set()
    e1.set()
    host._steam_client_stop_waiting_event = e2
    assert host._steam_client_stop_waiting_event is e2
    assert inner_saw_cancel.wait(2)
    e1.release_first_unset_read.set()

    assert e1.set_read.wait(2)
    assert wrapper_returned.wait(2)
    wrapper.join()
    assert result == [False]
    assert not release_inner_teardown.is_set()
    assert e1.read_count == 2
    assert e2.read_count == 0
    assert operation_cancel.is_set()

    release_inner_teardown.set()
    assert inner_finished.wait(2)


def test_stop_waiting_wrapper_good_ordering_returns_before_replacement():
    from dzll_launcher.window import DZLLWindow

    e1 = ObservedStopWaitingEvent()
    e2 = ObservedStopWaitingEvent()
    inner_started = threading.Event()
    release_inner_teardown = threading.Event()
    inner_finished = threading.Event()
    wrapper_returned = threading.Event()
    result = []

    def install(*_args, **_kwargs):
        inner_started.set()
        release_inner_teardown.wait()
        inner_finished.set()
        return False

    host = SimpleNamespace(
        _steam_client_stop_waiting_event=e1,
        _run_steam_client_install_impl=install,
    )
    e1.set()

    def invoke_wrapper():
        result.append(DZLLWindow._run_steam_client_install_with_stop_waiting(
            host, stop_waiting_event=e1,
        ))
        wrapper_returned.set()

    wrapper = threading.Thread(target=invoke_wrapper)
    wrapper.start()
    assert inner_started.wait(2)
    assert e1.set_read.wait(2)
    assert wrapper_returned.wait(2)
    wrapper.join()

    host._steam_client_stop_waiting_event = e2
    assert result == [False]
    assert e1.read_count == 1
    assert e2.read_count == 0
    assert not release_inner_teardown.is_set()
    release_inner_teardown.set()
    assert inner_finished.wait(2)


def test_stop_waiting_wrapper_isolates_attempt_e1_from_attempt_e2():
    from dzll_launcher.window import DZLLWindow

    e1 = ObservedStopWaitingEvent()
    e2 = ObservedStopWaitingEvent()
    started = {"A": threading.Event(), "B": threading.Event()}
    release_inner = {"A": threading.Event(), "B": threading.Event()}
    inner_finished = {"A": threading.Event(), "B": threading.Event()}
    returned = {"A": threading.Event(), "B": threading.Event()}
    results = {}

    def install(*_args, attempt, **_kwargs):
        started[attempt].set()
        release_inner[attempt].wait()
        inner_finished[attempt].set()
        return False

    host = SimpleNamespace(
        _steam_client_stop_waiting_event=e1,
        _run_steam_client_install_impl=install,
    )

    def invoke(attempt, stop_event):
        results[attempt] = DZLLWindow._run_steam_client_install_with_stop_waiting(
            host, attempt=attempt, stop_waiting_event=stop_event,
        )
        returned[attempt].set()

    wrapper_a = threading.Thread(target=invoke, args=("A", e1))
    wrapper_a.start()
    assert started["A"].wait(2)

    host._steam_client_stop_waiting_event = e2
    wrapper_b = threading.Thread(target=invoke, args=("B", e2))
    wrapper_b.start()
    assert started["B"].wait(2)
    assert e2.any_read.wait(2)

    e1.set()
    assert returned["A"].wait(2)
    wrapper_a.join()
    assert not returned["B"].is_set()
    assert e2.read_count > 0

    e2.set()
    assert returned["B"].wait(2)
    wrapper_b.join()
    assert results == {"A": False, "B": False}
    assert e1.set_read.is_set()
    assert e2.set_read.is_set()

    release_inner["A"].set()
    release_inner["B"].set()
    assert inner_finished["A"].wait(2)
    assert inner_finished["B"].wait(2)


def test_stop_waiting_wrapper_preserves_foreground_gate_until_outer_finishes():
    from dzll_launcher.background_prepare import preparation_operation_gate
    from dzll_launcher.join_attempt import JoinAttemptTracker
    from dzll_launcher.join_preparation_busy import shared_join_preparation_busy
    from dzll_launcher.window import DZLLWindow

    e1 = ObservedStopWaitingEvent()
    e2 = ObservedStopWaitingEvent()
    operation_cancel = threading.Event()
    inner_started = threading.Event()
    release_inner_teardown = threading.Event()
    inner_finished = threading.Event()
    wrapper_returned = threading.Event()
    outer_finished = threading.Event()

    attempts = JoinAttemptTracker(log_sink=lambda _line: None)
    attempt_a = attempts.begin(
        ip="127.0.0.1", game_port=2302, query_port=27016, name="A",
    )

    def install(*_args, cancel_event, **_kwargs):
        inner_started.set()
        cancel_event.wait()
        release_inner_teardown.wait()
        inner_finished.set()
        return False

    host = SimpleNamespace(
        _join_attempts=attempts,
        _steam_client_stop_waiting_event=e1,
        _run_steam_client_install_impl=install,
    )
    gate = preparation_operation_gate(host)
    lease = gate.try_acquire("foreground_join")
    assert lease is not None

    def outer_worker():
        assert not DZLLWindow._run_steam_client_install_with_stop_waiting(
            host,
            cancel_event=operation_cancel,
            stop_waiting_event=e1,
        )
        wrapper_returned.set()
        inner_finished.wait()
        assert gate.release(lease)
        outer_finished.set()

    outer = threading.Thread(target=outer_worker)
    outer.start()
    assert inner_started.wait(2)
    assert e1.any_read.wait(2)

    operation_cancel.set()
    e1.set()
    assert attempts.cleanup(attempt_a.attempt_id, "stop waiting/cancel")
    host._steam_client_stop_waiting_event = e2

    assert wrapper_returned.wait(2)
    assert gate.active_owner == "foreground_join"
    assert shared_join_preparation_busy(host)
    assert e2.read_count == 0

    release_inner_teardown.set()
    assert outer_finished.wait(2)
    outer.join()
    assert not shared_join_preparation_busy(host)
    attempt_b = attempts.begin(
        ip="127.0.0.2", game_port=2302, query_port=27017, name="B",
    )
    assert attempt_b is not None
    assert attempt_b.attempt_id != attempt_a.attempt_id


def test_shutdown_cancels_active_wrapper_without_stop_event_swap_hang():
    from dzll_launcher.join_attempt import JoinAttemptTracker
    from dzll_launcher.window import DZLLWindow

    e1 = ObservedStopWaitingEvent()
    e2 = ObservedStopWaitingEvent()
    operation_cancel = threading.Event()
    inner_started = threading.Event()
    inner_saw_cancel = threading.Event()
    release_inner_teardown = threading.Event()
    wrapper_returned = threading.Event()
    result = []
    attempts = JoinAttemptTracker(log_sink=lambda _line: None)
    attempts.begin(
        ip="127.0.0.1", game_port=2302, query_port=27016, name="A",
    )

    def install(*_args, cancel_event, **_kwargs):
        assert cancel_event is operation_cancel
        inner_started.set()
        cancel_event.wait()
        inner_saw_cancel.set()
        release_inner_teardown.wait()
        return False

    host = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _background_prepare_ui_generation=0,
        _preparation_reap_recovery_source_id=0,
        _background_prepare_queue=None,
        _background_prepare_controller=None,
        _scroll_drag_light=None,
        _discord=None,
        _join_attempts=attempts,
        _join_popup_item_activity=SimpleNamespace(
            clear_attempt=lambda _attempt_id: None,
        ),
        _steamcmd_cancel_event=operation_cancel,
        _steam_client_stop_waiting_event=e1,
        _run_steam_client_install_impl=install,
        _finish_start_steam_join_consent=lambda *_args, **_kwargs: False,
        _clear_join_pending_state=lambda _attempt_id: True,
    )

    def invoke_wrapper():
        result.append(DZLLWindow._run_steam_client_install_with_stop_waiting(
            host,
            cancel_event=operation_cancel,
            stop_waiting_event=e1,
        ))
        wrapper_returned.set()

    wrapper = threading.Thread(target=invoke_wrapper)
    wrapper.start()
    assert inner_started.wait(2)
    assert e1.any_read.wait(2)
    host._steam_client_stop_waiting_event = e2

    DZLLWindow._shutdown_cleanup(host)

    assert host._shutdown_cleanup_done
    assert operation_cancel.is_set()
    assert inner_saw_cancel.wait(2)
    assert attempts.active is None
    assert e1.set_read.is_set() is False
    assert e2.read_count == 0
    assert wrapper_returned.is_set() is False

    release_inner_teardown.set()
    assert wrapper_returned.wait(2)
    wrapper.join()
    assert result == [False]


def test_helper_environment_scrubs_inherited_app_identity(monkeypatch):
    monkeypatch.setenv("SteamAppId", "999")
    monkeypatch.setenv("SteamGameId", "999")
    monkeypatch.setenv("SteamOverlayGameId", "999")
    env = steam_ugc_backend._helper_env()
    assert "SteamAppId" not in env
    assert "SteamGameId" not in env
    assert "SteamOverlayGameId" not in env


def test_unset_cancel_never_sends_cancel_while_startup_waits_then_succeeds(
        monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_delayed_"))
    process = ProtocolProcess(appid_dir)
    # Replace the eager ready event with repeated startup waits and delayed ready.
    process.stdout = QueueStdout()
    process.stdout.push(
        {
            "type": "session_starting",
            "request_id": None,
            "helper_pid": process.pid,
            "temp_dir": str(appid_dir),
        }
    )
    process.stdout.push({"type": "session_waiting", "request_id": None})
    process.stdout.push({"type": "session_waiting", "request_id": None})
    process.stdout.push(
        {"type": "session_ready", "request_id": None, "helper_pid": process.pid}
    )
    cancel = threading.Event()
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    session.close()
    assert not cancel.is_set()
    assert [message["command"] for message in process.commands] == [
        "query_state", "shutdown",
    ]
    assert not appid_dir.exists()


def test_readiness_timeout_is_failure_without_protocol_cancel(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_wait_timeout_"))
    process = ProtocolProcess(appid_dir)
    process.stdout = QueueStdout()
    process.stdout.push(
        {
            "type": "session_starting",
            "request_id": None,
            "helper_pid": process.pid,
            "temp_dir": str(appid_dir),
        }
    )
    process.stdout.push({"type": "session_waiting", "request_id": None})
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=0.01, mod_ids=[1]) == (False, None)
    assert process.commands == []
    session.close()
    assert [message["command"] for message in process.commands] == ["shutdown"]
    assert not appid_dir.exists()


def test_cleared_previously_used_event_is_not_reused_as_cancellation(
        monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_cleared_event_"))
    process = ProtocolProcess(appid_dir)
    cancel = threading.Event()
    cancel.set()
    cancel.clear()
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    session.close()
    assert all(message["command"] != "cancel" for message in process.commands)
    assert not appid_dir.exists()


def test_real_cancel_targets_active_request_and_acknowledges(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_real_cancel_"))
    cancel = threading.Event()

    class CancellableProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            command = message["command"]
            if command == "query_state":
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": message["request_id"],
                        "command": command,
                    }
                )
                cancel.set()
                return len(raw)
            if command == "cancel":
                self.stdout.push(
                    {
                        "type": "cancellation_ack",
                        "request_id": message["request_id"],
                        "target_request_id": message["target_request_id"],
                    }
                )
                self.stdout.push(
                    {
                        "type": "command_result",
                        "request_id": message["target_request_id"],
                        "ok": False,
                        "cancelled": True,
                    }
                )
                return len(raw)
            return super().write(raw)

    process = CancellableProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    assert not session.run_command("state", timeout=2, mod_ids=[1])[0]
    query = next(message for message in process.commands if message["command"] == "query_state")
    cancel_message = next(message for message in process.commands if message["command"] == "cancel")
    assert cancel_message["target_request_id"] == query["request_id"]
    assert len([message for message in process.commands if message["command"] == "cancel"]) == 1
    cancel.clear()
    session.close()
    assert not appid_dir.exists()


@pytest.mark.parametrize("result_first", [False, True])
def test_cancel_drain_accepts_both_terminal_orderings_before_cleanup(
        monkeypatch, result_first):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_cancel_drain_"))
    cancel = threading.Event()

    class OrderedCancelProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            command = message["command"]
            if command == "query_state":
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": message["request_id"],
                        "command": command,
                    }
                )
                cancel.set()
                return len(raw)
            if command == "cancel":
                acknowledgement = {
                    "type": "cancellation_ack",
                    "request_id": message["request_id"],
                    "target_request_id": message["target_request_id"],
                }
                result = {
                    "type": "command_result",
                    "request_id": message["target_request_id"],
                    "ok": False,
                    "cancelled": True,
                }
                for event in (
                    (result, acknowledgement)
                    if result_first else (acknowledgement, result)
                ):
                    self.stdout.push(event)
                return len(raw)
            return super().write(raw)

    process = OrderedCancelProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    assert session.run_command("state", timeout=2, mod_ids=[1])[0] is False
    cancel.clear()
    assert session.run_command(
        "unsubscribe", timeout=2, mod_ids=[1],
    )[0] is True
    session.close()
    commands = [message["command"] for message in process.commands]
    assert commands[:2] == ["query_state", "cancel"]
    assert commands.count("unsubscribe") >= 1
    assert commands.count("shutdown") >= 1
    assert commands.index("unsubscribe") > commands.index("cancel")
    assert commands.index("shutdown") > commands.index("unsubscribe")
    assert process.signals == []
    assert not appid_dir.exists()


def test_missing_cancel_ack_fails_closed_then_cleans_session(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_missing_ack_"))
    cancel = threading.Event()

    class MissingAckProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "query_state":
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": message["request_id"],
                        "command": "query_state",
                    }
                )
                cancel.set()
                return len(raw)
            if message["command"] == "cancel":
                return len(raw)
            return super().write(raw)

    process = MissingAckProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    monkeypatch.setattr(steam_ugc_backend.time, "monotonic", _fast_monotonic())
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="missing cancellation acknowledgement",
    ):
        session.run_command("state", timeout=20, mod_ids=[1])
    cancel.clear()
    session.close()
    assert len([message for message in process.commands if message["command"] == "cancel"]) == 1
    assert not appid_dir.exists()


def test_helper_cooperative_cancel_still_shuts_down_once_and_removes_appid(monkeypatch):
    commands = (
        '{"command":"subscribe_download","request_id":"d-1","item_ids":[7],"timeout":30}\n'
        '{"command":"cancel","request_id":"c-2","target_request_id":"d-1"}\n'
        '{"command":"shutdown","request_id":"s-3"}\n'
    )
    events = []
    counts = {"init": 0, "shutdown": 0}
    temp_path = []

    class FakeSteam:
        ugc_accessor_name = "fake"

        def __init__(self, _paths):
            pass

        def init(self):
            counts["init"] += 1

        def shutdown(self):
            counts["shutdown"] += 1

        def snapshot(self, item_id):
            return steam_ugc_helper.ItemSnapshot(
                item_id, 1, ["Subscribed"], True, False, False, True, False,
                1, 10, 0, None,
            )

        def subscribe(self, _item_id):
            return 1

        def download(self, _item_id, _high_priority):
            return True

        def run_callbacks(self):
            return None

    original_setup = steam_ugc_helper.setup_temp_appid

    def setup(appid):
        tmp = original_setup(appid)
        temp_path.append(Path(tmp.name))
        return tmp

    monkeypatch.setattr(steam_ugc_helper.sys, "stdin", io.StringIO(commands))
    monkeypatch.setattr(steam_ugc_helper, "detect_steam_paths", lambda: SimpleNamespace())
    monkeypatch.setattr(steam_ugc_helper, "ensure_ld_library_path", lambda _paths: None)
    monkeypatch.setattr(steam_ugc_helper, "setup_temp_appid", setup)
    monkeypatch.setattr(steam_ugc_helper, "SteamUGC", FakeSteam)
    monkeypatch.setattr(steam_ugc_helper, "emit", events.append)

    assert steam_ugc_helper.command_session(SimpleNamespace(appid=221100)) == 0
    assert counts == {"init": 1, "shutdown": 1}
    assert not temp_path[0].exists()
    assert any(event.get("type") == "cancellation_ack" for event in events)
    assert any(
        event.get("type") == "command_result"
        and event.get("request_id") == "d-1"
        and event.get("cancelled")
        for event in events
    )


def test_cancel_ack_and_result_do_not_unsubscribe_before_session_shutdown(monkeypatch):
    download_started = threading.Event()
    events = []
    operations = []
    subscribed = {7: False}

    class ControlledInput:
        def __iter__(self):
            yield (
                '{"command":"subscribe_download","request_id":"d-1",'
                '"item_ids":[7],"timeout":30}\n'
            )
            assert download_started.wait(timeout=2.0)
            yield (
                '{"command":"cancel","request_id":"c-2",'
                '"target_request_id":"d-1","cleanup_item_ids":[7]}\n'
            )
            yield '{"command":"shutdown","request_id":"s-3"}\n'

    class FakeSteam:
        ugc_accessor_name = "fake"

        def __init__(self, _paths):
            pass

        def init(self):
            return None

        def shutdown(self):
            operations.append("steam_shutdown")

        def snapshot(self, item_id):
            return _cancel_cleanup_snapshot(
                item_id, subscribed=subscribed[int(item_id)],
                downloading=subscribed[int(item_id)],
            )

        def subscribe(self, item_id):
            subscribed[int(item_id)] = True
            return 70

        def download(self, _item_id, _high_priority):
            download_started.set()
            return True

        def unsubscribe(self, item_id):
            operations.append("unsubscribe")
            subscribed[int(item_id)] = False
            return 71

        def run_callbacks(self):
            operations.append("callbacks")

    def collect(event):
        events.append(event)
        if event.get("type") in {"cancellation_ack", "command_result"}:
            operations.append(event["type"])

    monkeypatch.setattr(steam_ugc_helper.sys, "stdin", ControlledInput())
    monkeypatch.setattr(
        steam_ugc_helper, "detect_steam_paths", lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        steam_ugc_helper, "ensure_ld_library_path", lambda _paths: None,
    )
    monkeypatch.setattr(steam_ugc_helper, "SteamUGC", FakeSteam)
    monkeypatch.setattr(steam_ugc_helper, "emit", collect)

    assert steam_ugc_helper.command_session(SimpleNamespace(appid=221100)) == 0
    assert "unsubscribe" not in operations
    assert operations.index("cancellation_ack") < operations.index("command_result")
    assert operations.index("command_result") < operations.index("steam_shutdown")
    cancelled = next(
        event for event in events
        if event.get("type") == "command_result"
        and event.get("request_id") == "d-1"
    )
    assert cancelled["cancel_handoff"] == {
        "parent_allowlisted": [7],
        "helper_subscribe_attempted": [7],
        "cleanup_candidates": [7],
    }
    assert operations.count("steam_shutdown") == 1


def test_helper_fatal_command_error_fails_closed_and_removes_appid(monkeypatch):
    events = []
    counts = {"init": 0, "shutdown": 0}
    temp_path = []

    class FailingSteam:
        ugc_accessor_name = "fake"

        def __init__(self, _paths):
            pass

        def init(self):
            counts["init"] += 1

        def shutdown(self):
            counts["shutdown"] += 1

        def snapshot(self, _item_id):
            raise RuntimeError("state exploded")

    original_setup = steam_ugc_helper.setup_temp_appid

    def setup(appid):
        tmp = original_setup(appid)
        temp_path.append(Path(tmp.name))
        return tmp

    monkeypatch.setattr(
        steam_ugc_helper.sys,
        "stdin",
        io.StringIO(
            '{"command":"query_state","request_id":"q-1","item_ids":[7],"timeout":2}\n'
        ),
    )
    monkeypatch.setattr(steam_ugc_helper, "detect_steam_paths", lambda: SimpleNamespace())
    monkeypatch.setattr(steam_ugc_helper, "ensure_ld_library_path", lambda _paths: None)
    monkeypatch.setattr(steam_ugc_helper, "setup_temp_appid", setup)
    monkeypatch.setattr(steam_ugc_helper, "SteamUGC", FailingSteam)
    monkeypatch.setattr(steam_ugc_helper, "emit", events.append)

    assert steam_ugc_helper.command_session(SimpleNamespace(appid=221100)) == 5
    assert counts == {"init": 1, "shutdown": 1}
    assert not temp_path[0].exists()
    assert any(event.get("type") == "fatal_session_error" for event in events)
    assert any(event.get("type") == "shutdown_complete" for event in events)


def test_forced_helper_termination_removes_reported_appid_dir(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_forced_"))

    class UnresponsiveShutdownProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "shutdown":
                return len(raw)
            return super().write(raw)

    process = UnresponsiveShutdownProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    monkeypatch.setattr(steam_ugc_backend.time, "monotonic", _fast_monotonic())
    with pytest.raises(steam_ugc_backend.UGCSessionError, match="clean SteamAPI shutdown"):
        session.close()
    assert process.signals == ["terminate"]
    assert not appid_dir.exists()


def test_acknowledged_shutdown_that_does_not_exit_fails_closed(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_nonexit_"))

    class NonExitingProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "shutdown":
                request_id = message["request_id"]
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": request_id,
                        "command": "shutdown",
                    }
                )
                self.stdout.push(
                    {
                        "type": "shutdown_complete",
                        "request_id": request_id,
                        "ok": True,
                        "steamapi_shutdown": True,
                        "temp_cleanup": True,
                    }
                )
                return len(raw)
            return super().write(raw)

    process = NonExitingProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="natural-exit grace period",
    ):
        session.close()
    assert process.stdin_closed
    assert process.signals == ["terminate"]
    assert not appid_dir.exists()


def test_delayed_natural_exit_after_shutdown_is_clean(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_delayed_exit_"))

    class DelayedExitProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "shutdown":
                request_id = message["request_id"]
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": request_id,
                        "command": "shutdown",
                    }
                )
                self.stdout.push(
                    {
                        "type": "shutdown_complete",
                        "request_id": request_id,
                        "ok": True,
                        "steamapi_shutdown": True,
                        "temp_cleanup": True,
                    }
                )
                return len(raw)
            return super().write(raw)

        def wait(self, timeout):
            if self.returncode is None and timeout >= 5.0:
                self.returncode = 0
                self.stdout.close()
            return super().wait(timeout)

    process = DelayedExitProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    session.close()
    assert process.stdin_closed
    assert process.signals == []
    assert not appid_dir.exists()


def test_shutdown_complete_enqueued_during_reader_join_is_finally_drained(
        monkeypatch):
    process = DelayedShutdownProcess(
        lambda request_id: {
            "type": "shutdown_complete",
            "request_id": request_id,
            "ok": True,
            "steamapi_shutdown": True,
            "temp_cleanup": True,
        },
    )
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    session._start()
    session.close()
    assert session._shutdown_complete
    assert process.returncode == 0


def test_delayed_mismatched_shutdown_complete_remains_fail_closed(monkeypatch):
    process = DelayedShutdownProcess({
        "type": "shutdown_complete",
        "request_id": "wrong-shutdown",
        "ok": True,
        "steamapi_shutdown": True,
        "temp_cleanup": True,
    })
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    session._start()
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="shutdown_complete request_id did not match",
    ):
        session.close()
    assert not session._shutdown_complete


def test_delayed_malformed_shutdown_terminal_remains_fail_closed(monkeypatch):
    process = DelayedShutdownProcess("not-json\n")
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    session._start()
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="malformed protocol data",
    ):
        session.close()
    assert session._fatal
    assert not session._shutdown_complete


def test_mismatched_shutdown_complete_fails_closed(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_bad_shutdown_id_"))

    class MismatchedShutdownProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            if message["command"] == "shutdown":
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": message["request_id"],
                        "command": "shutdown",
                    }
                )
                self.stdout.push(
                    {
                        "type": "shutdown_complete",
                        "request_id": "wrong-shutdown",
                        "ok": True,
                        "steamapi_shutdown": True,
                        "temp_cleanup": True,
                    }
                )
                return len(raw)
            return super().write(raw)

    process = MismatchedShutdownProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="shutdown_complete request_id",
    ):
        session.close()
    assert process.signals == ["terminate"]
    assert not appid_dir.exists()


def test_appid_cleanup_failure_after_clean_shutdown_fails_closed(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_cleanup_failure_"))
    process = ProtocolProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(steam_ugc_backend.shutil, "rmtree", lambda _path: None)
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="temporary Steam AppID directory survived",
    ):
        session.close()
    assert process.signals == []
    assert appid_dir.exists()
    appid_dir.rmdir()


def test_reader_thread_survival_after_clean_shutdown_fails_closed(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_reader_failure_"))
    process = ProtocolProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    assert session.run_command("state", timeout=2, mod_ids=[1])[0]

    class SurvivingThread:
        def join(self, timeout):
            assert timeout == 1.0

        def is_alive(self):
            return True

    session._stderr_thread = SurvivingThread()
    with pytest.raises(
        steam_ugc_backend.UGCHelperReapError,
        match="stderr reader thread survived",
    ) as raised:
        session.close()
    assert process.signals == []
    assert raised.value.helper_process_confirmed_dead
    assert not raised.value.helper_process_may_be_alive
    assert raised.value.reader_cleanup_only
    assert raised.value.steamapi_shutdown_confirmed


def test_unconfirmed_session_ownership_is_retained_until_process_recovery():
    class Process:
        alive = True

        def poll(self):
            return None if self.alive else 0

    class Session:
        def __init__(self):
            self._proc = Process()
            self._shutdown_complete = False
            self.finalized = 0

        def _finalize_confirmed_reap_recovery(self):
            self.finalized += 1

    session = Session()
    steam_ugc_backend.activate_ugc_session(session)
    error = steam_ugc_backend.UGCHelperReapError(
        "unconfirmed", process=session._proc,
    ).bind_session(session)
    steam_ugc_backend.deactivate_ugc_session(
        session, retain_for_recovery=True,
    )
    assert steam_ugc_backend.active_ugc_session() is None
    assert session in steam_ugc_backend._ACTIVE_UGC_SESSIONS
    assert not error.finalize_confirmed_recovery()
    session._proc.alive = False
    assert error.finalize_confirmed_recovery()
    assert session.finalized == 1
    assert session not in steam_ugc_backend._ACTIVE_UGC_SESSIONS


def test_native_and_python_diagnostics_cannot_enter_protocol_stdout():
    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    prior_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(project_root / "src") + (
        os.pathsep + prior_pythonpath if prior_pythonpath else ""
    )
    script = (
        "import os\n"
        "from dzll_launcher import steam_ugc_helper as helper\n"
        "helper._isolate_protocol_stdout()\n"
        "os.write(1, b'native Steamworks diagnostic\\n')\n"
        "print('ordinary Python diagnostic', flush=True)\n"
        "helper.emit({'type': 'probe', 'request_id': 'p-1'})\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "type": "probe", "request_id": "p-1",
    }
    assert result.stdout.count("\n") == 1
    assert "native Steamworks diagnostic" in result.stderr
    assert "ordinary Python diagnostic" in result.stderr


def test_malformed_active_command_is_finalized_once_then_shutdown_quarantines_stale_events(
        monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_malformed_"))

    class MalformedActiveProcess(ProtocolProcess):
        def __init__(self, temp_dir):
            super().__init__(temp_dir)
            self.active_request_id = ""

        def write(self, raw):
            message = json.loads(raw)
            self.commands.append(message)
            command = message["command"]
            request_id = message["request_id"]
            if command == "shutdown":
                self.stdout.push(
                    {
                        "type": "item", "request_id": self.active_request_id,
                        "id": 1, "installed": True,
                    }
                )
                self.stdout.push(
                    {
                        "type": "command_result",
                        "request_id": self.active_request_id,
                        "ok": True,
                    }
                )
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": request_id,
                        "command": "shutdown",
                    }
                )
                self.stdout.push(
                    {
                        "type": "shutdown_complete", "request_id": request_id,
                        "ok": True, "steamapi_shutdown": True,
                        "temp_cleanup": True,
                    }
                )
                self.returncode = 0
                self.stdout.close()
            else:
                self.active_request_id = request_id
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": request_id,
                        "command": command,
                    }
                )
                self.stdout.lines.put("native output on protocol fd\n")
            return len(raw)

    process = MalformedActiveProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    delivered = []
    session = steam_ugc_backend.CooperativeUGCSession()
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="malformed protocol data",
    ):
        session.run_command(
            "state", timeout=2, mod_ids=[1], on_event=delivered.append,
        )
    assert session._active_request_id == ""
    assert session._abandoned_request_ids == {process.active_request_id}
    assert not session.usable

    session.close()
    commands_after_close = list(process.commands)
    session.close()
    assert process.commands == commands_after_close
    assert [event["type"] for event in delivered] == ["command_accepted"]
    assert process.commands[-1]["command"] == "shutdown"
    assert process.commands[-1]["request_id"].startswith("shutdown-")
    assert process.signals == []
    assert not appid_dir.exists()


def test_partial_protocol_message_followed_by_eof_fails_the_session():
    session = steam_ugc_backend.CooperativeUGCSession()
    session._stdout_q.put('{"type":"item"')
    session._stdout_q.put(None)
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="partial protocol message",
    ):
        session._event(0.1)
    assert session._fatal
    with pytest.raises(
        steam_ugc_backend.UGCSessionError,
        match="closed its protocol stream",
    ):
        session._event(0.1)


@pytest.mark.parametrize(
    ("bad_response", "message"),
    [
        ("duplicate", "duplicate command acceptance"),
        ("unexpected", "unexpected Steam UGC helper event"),
        ("concatenated", "malformed protocol data"),
    ],
)
def test_duplicated_concatenated_and_unexpected_command_data_fail_the_session(
        monkeypatch, bad_response, message):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_bad_frame_"))

    class BadFrameProcess(ProtocolProcess):
        def write(self, raw):
            command = json.loads(raw)
            if command["command"] == "shutdown":
                return super().write(raw)
            self.commands.append(command)
            accepted = {
                "type": "command_accepted",
                "request_id": command["request_id"],
                "command": command["command"],
            }
            self.stdout.push(accepted)
            if bad_response == "duplicate":
                self.stdout.push(accepted)
            elif bad_response == "unexpected":
                self.stdout.push(
                    {
                        "type": "not_a_protocol_event",
                        "request_id": command["request_id"],
                    }
                )
            else:
                first = json.dumps(
                    {"type": "item", "request_id": command["request_id"]}
                )
                second = json.dumps(
                    {"type": "command_result", "request_id": command["request_id"]}
                )
                self.stdout.lines.put(first + second + "\n")
            return len(raw)

    process = BadFrameProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    with pytest.raises(steam_ugc_backend.UGCSessionError, match=message):
        session.run_command("state", timeout=2, mod_ids=[1])
    assert not session.usable
    session.close()
    assert not appid_dir.exists()


def test_multiple_json_lines_from_one_buffer_are_independent_messages():
    session = steam_ugc_backend.CooperativeUGCSession()
    combined = (
        '{"type":"session_waiting","request_id":null}\n'
        '{"type":"session_ready","request_id":null}\n'
    )
    for line in io.StringIO(combined):
        session._stdout_q.put(line)
    assert session._event(0.1)["type"] == "session_waiting"
    assert session._event(0.1)["type"] == "session_ready"
    assert session._ready


def test_shutdown_waits_for_active_command_cancellation_and_reaps_once(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_active_close_"))
    command_started = threading.Event()

    class ActiveUntilCancelledProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            if message["command"] not in {"cancel", "shutdown"}:
                self.commands.append(message)
                self.stdout.push(
                    {
                        "type": "command_accepted",
                        "request_id": message["request_id"],
                        "command": message["command"],
                    }
                )
                command_started.set()
                return len(raw)
            return super().write(raw)

    process = ActiveUntilCancelledProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession()
    results = []
    failures = []

    def run_command():
        try:
            results.append(session.run_command("state", timeout=30, mod_ids=[1]))
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_command)
    worker.start()
    assert command_started.wait(timeout=2.0)
    session.close()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert failures == []
    assert results and results[0][0] is False
    assert [message["command"] for message in process.commands] == [
        "query_state", "cancel", "shutdown",
    ]
    assert session._active_request_id == ""
    assert process.signals == []
    assert not appid_dir.exists()


def test_close_waits_for_active_cancel_handoff_result_before_shutdown(monkeypatch):
    appid_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_cancel_cleanup_close_"))
    cancel = threading.Event()
    cancel_received = threading.Event()

    class CleanupUntilReleasedProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            if message["command"] == "subscribe_download":
                self.commands.append(message)
                self.stdout.push({
                    "type": "command_accepted",
                    "request_id": message["request_id"],
                    "command": message["command"],
                })
                cancel.set()
                return len(raw)
            if message["command"] == "cancel":
                self.commands.append(message)
                self.stdout.push({
                    "type": "cancellation_ack",
                    "request_id": message["request_id"],
                    "target_request_id": message["target_request_id"],
                })
                cancel_received.set()
                return len(raw)
            return super().write(raw)

    process = CleanupUntilReleasedProcess(appid_dir)
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )
    session = steam_ugc_backend.CooperativeUGCSession(cancel_event=cancel)
    command_result = []
    worker = threading.Thread(target=lambda: command_result.append(
        session.run_command(
            "subscribe-download", timeout=30, mod_ids=[7],
            cancel_cleanup_ids=[7],
        )
    ))
    worker.start()
    assert cancel_received.wait(timeout=2.0)

    close_done = threading.Event()
    closer = threading.Thread(target=lambda: (session.close(), close_done.set()))
    closer.start()
    assert not close_done.wait(timeout=0.05)
    process.stdout.push({
        "type": "command_result",
        "request_id": process.commands[0]["request_id"],
        "ok": False,
        "cancelled": True,
        "cancel_handoff": {
            "parent_allowlisted": [7],
            "helper_subscribe_attempted": [7],
            "cleanup_candidates": [7],
        },
    })
    worker.join(timeout=2.0)
    closer.join(timeout=2.0)
    assert not worker.is_alive()
    assert not closer.is_alive()
    assert command_result == [(False, None)]
    assert [message["command"] for message in process.commands] == [
        "subscribe_download", "cancel", "shutdown",
    ]
    cancel_message = process.commands[1]
    assert cancel_message["cleanup_item_ids"] == [7]
    assert not appid_dir.exists()


def test_helper_active_shutdown_preserves_id_without_old_command_terminal_events(
        monkeypatch):
    commands = (
        '{"command":"subscribe_download","request_id":"d-1",'
        '"item_ids":[7],"timeout":30}\n'
        '{"command":"shutdown","request_id":"s-2"}\n'
    )
    events = []

    class BusySteam:
        ugc_accessor_name = "fake"

        def __init__(self, _paths):
            pass

        def init(self):
            return None

        def shutdown(self):
            return None

        def snapshot(self, item_id):
            return steam_ugc_helper.ItemSnapshot(
                item_id, 1, ["Subscribed"], True, False, False, True, False,
                1, 10, 0, None,
            )

        def subscribe(self, _item_id):
            return 1

        def download(self, _item_id, _high_priority):
            return True

        def run_callbacks(self):
            return None

    monkeypatch.setattr(steam_ugc_helper.sys, "stdin", io.StringIO(commands))
    monkeypatch.setattr(
        steam_ugc_helper, "detect_steam_paths", lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        steam_ugc_helper, "ensure_ld_library_path", lambda _paths: None,
    )
    monkeypatch.setattr(steam_ugc_helper, "SteamUGC", BusySteam)
    monkeypatch.setattr(steam_ugc_helper, "emit", events.append)

    assert steam_ugc_helper.command_session(SimpleNamespace(appid=221100)) == 0
    shutdown_complete = next(
        event for event in events if event["type"] == "shutdown_complete"
    )
    assert shutdown_complete["request_id"] == "s-2"
    assert any(
        event.get("type") == "command_accepted"
        and event.get("request_id") == "s-2"
        for event in events
    )
    assert not any(
        event.get("request_id") == "d-1"
        and event.get("type") in {"command_result", "cancellation_ack"}
        for event in events
    )


def test_retry_after_failed_helper_session_starts_clean_process(monkeypatch):
    first_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_retry_first_"))
    second_dir = Path(tempfile.mkdtemp(prefix="dzll_steam_ugc_retry_second_"))

    class FirstFailedProcess(ProtocolProcess):
        def write(self, raw):
            message = json.loads(raw)
            if message["command"] == "shutdown":
                return super().write(raw)
            self.commands.append(message)
            self.stdout.push(
                {
                    "type": "command_accepted",
                    "request_id": message["request_id"],
                    "command": message["command"],
                }
            )
            self.stdout.lines.put("broken frame\n")
            return len(raw)

    processes = [FirstFailedProcess(first_dir), ProtocolProcess(second_dir)]
    monkeypatch.setattr(
        steam_ugc_backend.subprocess,
        "Popen",
        lambda *_args, **_kwargs: processes.pop(0),
    )

    failed = steam_ugc_backend.CooperativeUGCSession()
    with pytest.raises(steam_ugc_backend.UGCSessionError):
        failed.run_command("state", timeout=2, mod_ids=[1])
    failed.close()

    retry = steam_ugc_backend.CooperativeUGCSession()
    assert retry.run_command("state", timeout=2, mod_ids=[1])[0]
    retry.close()
    assert retry._next_request_id == 2
    assert retry._fatal is False
    assert not first_dir.exists()
    assert not second_dir.exists()


def _fast_monotonic():
    values = iter(range(100))
    return lambda: float(next(values))
