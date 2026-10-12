from __future__ import annotations

from collections import deque
import logging
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window
from dzll_launcher.window import DZLLWindow


class Future:
    def __init__(self, generation, key, info=None, *, raises=False):
        self.generation = generation
        self.key = key
        self.info = info if info is not None else {"ok": True}
        self.raises = raises

    def result(self):
        if self.raises:
            raise TimeoutError("timed out")
        return self.generation, (self.key, self.info)


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def diagnostic_host(monkeypatch):
    monkeypatch.setattr(window, "A2S_WARMUP_500_ENABLED", False)
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_: 41)
    monkeypatch.setattr(window.GLib, "timeout_add", lambda *_: 0)
    monkeypatch.setattr(window.GLib, "source_remove", lambda *_: None)
    host = SimpleNamespace(
        _startup_live_generation=0,
        _startup_live_rest_flush_id=0,
        _startup_warmup_timeout_id=0,
        _startup_live_rest_buffer=[],
        _shutdown_cleanup_done=False,
        _startup_presentation=SimpleNamespace(startup_completed=False),
        _pump_startup_rest_sweep=lambda _generation: False,
        _flush_startup_rest_results=lambda _generation: False,
        _auto_sort_lowest_ping_after_startup=lambda: False,
        _complete_startup_presentation=lambda: False,
        _set_updating=lambda *_: False,
    )
    bind(
        host,
        "_submit_startup_rest_batches",
        "_on_startup_rest_future_done",
        "_on_startup_warmup_timeout",
        "_complete_startup_warmup",
        "_log_a2s_sweep_terminal",
    )
    return host


def test_diagnostic_is_silent_when_disabled(monkeypatch, capsys):
    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", False)
    host = diagnostic_host(monkeypatch)

    host._submit_startup_rest_batches([], warmup_count=0)
    assert capsys.readouterr().err == ""


def test_diagnostic_io_failure_cannot_break_sweep_lifecycle(monkeypatch):
    class BrokenStderr:
        def write(self, _text):
            raise OSError("stderr unavailable")

        def flush(self):
            raise OSError("stderr unavailable")

    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    monkeypatch.setattr(window.sys, "stderr", BrokenStderr())
    host = diagnostic_host(monkeypatch)

    assert host._submit_startup_rest_batches([], warmup_count=0) is False
    assert host._suggestion_sweep_complete is True
    assert host._a2s_sweep_diag_terminal_generation == 1


def test_diagnostic_is_visible_at_default_warning_level(monkeypatch, capsys):
    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    window._log_a2s_sweep("[A2S-SWEEP] start generation=%s", 7)

    assert capsys.readouterr().err == "[A2S-SWEEP] start generation=7\n"


def test_diagnostic_distinguishes_dispatched_completed_and_responsive(
    monkeypatch, capsys,
):
    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    host = diagnostic_host(monkeypatch)
    keys = ["responsive", "offline", "exception"]
    host._submit_startup_rest_batches(keys, warmup_count=len(keys))
    host._a2s_sweep_diag_dispatched = len(keys)

    host._on_startup_rest_future_done(1, Future(1, keys[0]), keys[0])
    host._on_startup_rest_future_done(
        1, Future(1, keys[1], {"ok": False, "err": "offline"}), keys[1],
    )
    host._on_startup_rest_future_done(1, Future(1, keys[2], raises=True), keys[2])
    host._startup_live_rest_queue.clear()
    host._log_a2s_sweep_terminal("exhausted", 1)
    captured = capsys.readouterr().err.splitlines()

    assert any("distinct_eligible_endpoints=3" in line for line in captured)
    assert any("warmup threshold reached" in line and "completed=2 target=2" in line
               for line in captured)
    terminal = next(line for line in captured if "reason=exhausted" in line)
    assert "attempted_dispatched=3" in terminal
    assert "completed_attempts=3" in terminal
    assert "successfully_responsive=1" in terminal
    assert "remaining_queued=0 remaining_inflight=0" in terminal
    assert sum("FULL A2S SWEEP COMPLETE" in line for line in captured) == 1


@pytest.mark.parametrize("reason", ("cancelled", "superseded"))
def test_interrupted_sweeps_never_log_full_completion(monkeypatch, capsys, reason):
    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    host = diagnostic_host(monkeypatch)
    host._submit_startup_rest_batches(["a", "b"], warmup_count=2)
    host._startup_live_rest_inflight = 1
    host._startup_live_rest_queue = deque(["b"])

    host._log_a2s_sweep_terminal(reason)
    captured = capsys.readouterr().err.splitlines()

    terminal = next(line for line in captured if f"reason={reason}" in line)
    assert "remaining_queued=1 remaining_inflight=1" in terminal
    assert not any("FULL A2S SWEEP COMPLETE" in line for line in captured)


def test_full_marker_requires_every_dispatched_attempt_to_complete(monkeypatch, capsys):
    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    host = diagnostic_host(monkeypatch)
    host._submit_startup_rest_batches(["a"], warmup_count=1)
    host._startup_live_rest_queue.clear()
    host._a2s_sweep_diag_dispatched = 1

    host._log_a2s_sweep_terminal("exhausted", 1)
    captured = capsys.readouterr().err.splitlines()

    assert any("reason=exhausted" in line for line in captured)
    assert not any("FULL A2S SWEEP COMPLETE" in line for line in captured)


def test_dispatched_counter_advances_only_after_executor_accepts_work(
    monkeypatch, capsys,
):
    class PendingFuture:
        def add_done_callback(self, callback):
            self.callback = callback

    class Executor:
        def __init__(self):
            self.keys = []

        def submit(self, _callable, _generation, key, _ip, _qport):
            self.keys.append(key)
            return PendingFuture()

    monkeypatch.setattr(window, "A2S_SWEEP_DIAGNOSTICS_ENABLED", True)
    monkeypatch.setattr(window, "STARTUP_LIVE_REST_WORKERS", 2)
    host = diagnostic_host(monkeypatch)
    host._obj_by_key = {
        "a": SimpleNamespace(ip="10.0.0.1", qport=2303),
        "b": SimpleNamespace(ip="10.0.0.2", qport=2303),
    }
    host._startup_live_executor = Executor()
    host._query_startup_rest_one = lambda *_: None
    bind(host, "_pump_startup_rest_sweep", "_maybe_finish_startup_rest_sweep")

    host._submit_startup_rest_batches(["a", "b"], warmup_count=0)

    assert host._startup_live_executor.keys == ["a", "b"]
    assert host._a2s_sweep_diag_dispatched == 2
    assert host._a2s_sweep_diag_completed == 0
    assert "distinct_eligible_endpoints=2" in capsys.readouterr().err
