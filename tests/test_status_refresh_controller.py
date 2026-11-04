"""Sweep lifecycle tests for StatusRefreshController with a deterministic executor and GLib."""

from collections import deque
from types import SimpleNamespace

import pytest

from dzll_launcher import status_refresh_controller as module
from dzll_launcher.config import STARTUP_LIVE_FLUSH_MAX, STARTUP_LIVE_FLUSH_MS, STARTUP_LIVE_REST_WORKERS
from dzll_launcher.status_refresh_controller import StatusRefreshController, StatusRefreshWidgets


class Future:
    def __init__(self, fn, args):
        self.fn, self.args, self.callbacks, self.value = fn, args, [], None

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def result(self):
        return self.value

    def finish(self):
        self.value = self.fn(*self.args)
        for callback in self.callbacks:
            callback(self)


@pytest.fixture
def sweep(monkeypatch):
    state = SimpleNamespace(pending=deque(), idles=deque(), timers={}, removed=[], applied=[], refreshes=[],
                            queries=[], shutdown=False, scroll_sets=[])

    def timeout_add(ms, callback, *args):
        source_id = 700 + len(state.timers) + len(state.removed)
        state.timers[source_id] = (ms, callback, args)
        return source_id

    def source_remove(source_id):
        state.removed.append(source_id)
        state.timers.pop(source_id, None)

    monkeypatch.setattr(module.GLib, "idle_add", lambda callback, *args: state.idles.append((callback, args)) or 1)
    monkeypatch.setattr(module.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(module.GLib, "source_remove", source_remove)

    def query(ip, qport, **kwargs):
        state.queries.append((ip, qport, kwargs))
        return {"ok": True}

    monkeypatch.setattr(module, "query_server_live", query)

    servers = {f"k{i}": SimpleNamespace(ip=f"10.0.0.{i}", qport=27000 + i, gport=2300 + i) for i in range(100)}
    vadj = SimpleNamespace(get_value=lambda: 42.0, set_value=state.scroll_sets.append)

    def submit(fn, *args):
        future = Future(fn, args)
        state.pending.append(future)
        return future

    state.servers = servers
    state.controller = StatusRefreshController(
        widgets=StatusRefreshWidgets,
        server_keys=lambda: list(servers),
        lookup_server=servers.get,
        submit=submit,
        apply_live_results=lambda results, **kwargs: state.applied.append((len(results), kwargs)),
        refresh_browser=lambda: state.refreshes.append(True),
        scroller=lambda: SimpleNamespace(get_vadjustment=lambda: vadj),
        model_id=lambda: 1234,
        is_shut_down=lambda: state.shutdown,
    )

    def finish_one():
        state.pending.popleft().finish()
        while state.idles:
            callback, args = state.idles.popleft()
            callback(*args)

    state.finish_one = finish_one
    return state


def test_start_caps_inflight_queries_at_worker_limit(sweep):
    sweep.controller.start()

    assert sweep.controller.running is True
    assert sweep.controller.total == 100
    assert len(sweep.pending) == STARTUP_LIVE_REST_WORKERS
    assert sweep.controller.inflight == STARTUP_LIVE_REST_WORKERS
    assert len(sweep.controller.queue) == 100 - STARTUP_LIVE_REST_WORKERS


def test_worker_query_arguments_and_result_shape(sweep):
    sweep.controller.start()
    future = sweep.pending[0]
    future.value = future.fn(*future.args)

    ip, qport, kwargs = sweep.queries[0]
    assert (ip, qport) == ("10.0.0.0", 27000)
    assert kwargs == {
        "timeout": module.STARTUP_LIVE_REST_TIMEOUT_SECS,
        "gport": 2300,
        "cycle_id": "manual-status:1:k0",
        "generation": 1,
        "row_id": id(sweep.servers["k0"]),
        "model_id": 1234,
    }
    assert future.value == (1, ("k0", {"ok": True}))


def test_results_flush_at_threshold_and_complete_sweep_with_scroll_restore(sweep):
    sweep.controller.start()
    while sweep.pending:
        sweep.finish_one()

    assert sweep.applied[0] == (STARTUP_LIVE_FLUSH_MAX, {"reason": "manual-status-refresh"})
    assert sum(count for count, _ in sweep.applied) == 100
    assert all(kwargs == {"reason": "manual-status-refresh"} for _, kwargs in sweep.applied)
    assert sweep.controller.running is False
    assert sweep.controller.completed == 100
    assert sweep.refreshes == [True]
    assert sweep.scroll_sets == [42.0]  # restored via GLib.idle_add after the final rebuild
    assert sweep.controller.flush_id == 0
    assert sweep.removed  # the final pending flush timer was removed before the final flush
    # An earlier one-shot flush timer, superseded by the immediate threshold flush, may
    # still fire later. It must do nothing once the sweep has finished.
    applied_before = list(sweep.applied)
    for _ms, callback, args in list(sweep.timers.values()):
        if callback == sweep.controller._flush_results:
            assert callback(*args) is False
    assert sweep.applied == applied_before
    assert sweep.refreshes == [True]


def test_partial_batch_waits_for_flush_timer(sweep):
    sweep.controller.start()
    sweep.finish_one()

    flush = [entry for entry in sweep.timers.values() if entry[1] == sweep.controller._flush_results]
    assert flush == [(int(STARTUP_LIVE_FLUSH_MS), sweep.controller._flush_results, (1,))]
    assert sweep.applied == []


def test_cancel_invalidates_inflight_results_and_removes_flush_timer(sweep):
    sweep.controller.start()
    sweep.finish_one()
    flush_id = sweep.controller.flush_id
    assert flush_id

    sweep.controller.cancel()
    while sweep.pending:
        sweep.finish_one()

    assert flush_id in sweep.removed
    assert sweep.controller.generation == 2
    assert sweep.controller.running is False
    assert sweep.controller.flush_id == 0
    assert sweep.controller.inflight == 0
    assert not sweep.controller.queue and not sweep.controller.buffer
    assert sweep.controller.completed == 1
    assert sweep.applied == [] and sweep.refreshes == []


def test_shutdown_stops_pumping_and_counting(sweep):
    sweep.controller.start()
    sweep.shutdown = True
    queued = len(sweep.controller.queue)

    sweep.finish_one()

    assert len(sweep.controller.queue) == queued
    assert sweep.controller.completed == 0


def test_new_sweep_ignores_results_from_previous_generation(sweep):
    sweep.controller.start()
    old = sweep.pending.popleft()
    sweep.controller.cancel()
    sweep.pending.clear()
    sweep.controller.start()
    completed = sweep.controller.completed

    old.finish()
    while sweep.idles:
        callback, args = sweep.idles.popleft()
        callback(*args)

    assert sweep.controller.generation == 3
    assert sweep.controller.completed == completed
    assert sweep.controller.inflight == STARTUP_LIVE_REST_WORKERS
