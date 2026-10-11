from __future__ import annotations

import time
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import config, window
from dzll_launcher.window import DZLLWindow


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


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


def warmup_host(monkeypatch, *, experimental, startup_completed=False):
    scheduled = []
    removed = []
    events = []
    monkeypatch.setattr(window, "A2S_WARMUP_500_ENABLED", experimental)
    monkeypatch.setattr(
        window.GLib,
        "timeout_add_seconds",
        lambda seconds, callback, generation: scheduled.append(
            (seconds, callback, generation)
        ) or 41,
    )
    monkeypatch.setattr(window.GLib, "timeout_add", lambda *_: 0)
    monkeypatch.setattr(window.GLib, "source_remove", removed.append)
    host = SimpleNamespace(
        _startup_live_generation=0,
        _startup_live_rest_flush_id=0,
        _startup_warmup_timeout_id=0,
        _startup_live_rest_buffer=[],
        _shutdown_cleanup_done=False,
        _startup_presentation=SimpleNamespace(startup_completed=startup_completed),
        _pump_startup_rest_sweep=lambda _generation: False,
        _flush_startup_rest_results=lambda _generation: events.append("flush") or False,
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort") or False,
        _complete_startup_presentation=lambda: events.append("reveal") or False,
        _set_updating=lambda *_: events.append("updating"),
    )
    bind(
        host,
        "_submit_startup_rest_batches",
        "_on_startup_rest_future_done",
        "_on_startup_warmup_timeout",
        "_complete_startup_warmup",
    )
    return host, scheduled, removed, events


def test_default_path_keeps_full_eligible_cohort_and_65_percent(monkeypatch):
    host, scheduled, _removed, _events = warmup_host(monkeypatch, experimental=False)
    keys = [f"server-{index}" for index in range(600)]

    host._submit_startup_rest_batches(keys, warmup_count=520)

    assert host._startup_warmup_mode == "default"
    assert host._startup_warmup_cohort_count == 520
    assert host._startup_warmup_keys == set(keys[:520])
    assert host._startup_warmup_target == 338
    assert list(host._startup_live_rest_queue) == keys
    assert scheduled[0][0] == config.STARTUP_WARMUP_MAX_WAIT_SECS


def test_experiment_uses_first_500_dispatch_entries_and_60_percent(monkeypatch):
    host, scheduled, _removed, _events = warmup_host(monkeypatch, experimental=True)
    keys = [f"server-{index}" for index in range(800)]

    host._submit_startup_rest_batches(keys, warmup_count=700)

    assert host._startup_warmup_mode == "experimental_500_60"
    assert host._startup_warmup_cohort_count == 500
    assert host._startup_warmup_keys == set(keys[:500])
    assert host._startup_warmup_target == 300
    assert list(host._startup_live_rest_queue) == keys
    assert scheduled[0][0] == config.STARTUP_WARMUP_MAX_WAIT_SECS


def test_experiment_deduplicates_dispatch_without_reordering(monkeypatch):
    host, _scheduled, _removed, _events = warmup_host(monkeypatch, experimental=True)
    keys = ["a", "b", "a", "c", "b", "d"]

    host._submit_startup_rest_batches(keys, warmup_count=5)

    assert list(host._startup_live_rest_queue) == ["a", "b", "c", "d"]
    assert host._startup_warmup_keys == {"a", "b", "c"}
    assert host._startup_warmup_cohort_count == 3
    assert host._startup_warmup_target == 2


def test_experiment_dispatches_each_distinct_endpoint_once(monkeypatch):
    class PendingFuture:
        def add_done_callback(self, callback):
            self.callback = callback

    class Executor:
        def __init__(self):
            self.keys = []

        def submit(self, _callable, _generation, key, _ip, _qport):
            self.keys.append(key)
            return PendingFuture()

    host, _scheduled, _removed, _events = warmup_host(monkeypatch, experimental=True)
    host._startup_live_executor = Executor()
    host._query_startup_rest_one = lambda *_args: None
    host._obj_by_key = {
        key: SimpleNamespace(ip=f"10.0.0.{index}", qport=2303)
        for index, key in enumerate(("a", "b", "c", "d"), 1)
    }
    bind(host, "_pump_startup_rest_sweep", "_maybe_finish_startup_rest_sweep")
    monkeypatch.setattr(window, "STARTUP_LIVE_REST_WORKERS", 8)

    host._submit_startup_rest_batches(
        ["a", "b", "a", "c", "b", "d"], warmup_count=5,
    )

    assert host._startup_live_executor.keys == ["a", "b", "c", "d"]
    assert host._startup_live_rest_total == 4


@pytest.mark.parametrize(
    "cohort_size, expected_target",
    [(1, 1), (2, 2), (3, 2), (7, 5), (499, 300)],
)
def test_experiment_uses_ceiling_for_smaller_cohorts(
    monkeypatch, cohort_size, expected_target,
):
    host, _scheduled, _removed, _events = warmup_host(monkeypatch, experimental=True)
    keys = [f"server-{index}" for index in range(cohort_size)]

    host._submit_startup_rest_batches(keys, warmup_count=cohort_size)

    assert host._startup_warmup_target == expected_target


def test_empty_experimental_cohort_reveals_without_timer_and_keeps_full_sweep(monkeypatch):
    host, scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    keys = ["lower-rank-a", "lower-rank-b"]

    host._submit_startup_rest_batches(keys, warmup_count=0)

    assert host._startup_warmup_target == 0
    assert scheduled == []
    assert events == ["flush", "sort", "reveal"]
    assert list(host._startup_live_rest_queue) == keys


def test_failed_offline_and_timed_out_attempts_count_toward_threshold(monkeypatch):
    host, _scheduled, removed, events = warmup_host(monkeypatch, experimental=True)
    keys = [f"server-{index}" for index in range(5)]
    host._submit_startup_rest_batches(keys, warmup_count=5)
    assert host._startup_warmup_target == 3

    host._on_startup_rest_future_done(1, Future(1, keys[0], {"ok": False}), keys[0])
    host._on_startup_rest_future_done(1, Future(1, keys[1], raises=True), keys[1])
    assert events == []
    host._on_startup_rest_future_done(1, Future(1, keys[2], {"ok": False}), keys[2])

    assert host._startup_warmup_completed == 3
    assert events == ["flush", "sort", "reveal"]
    assert removed == [41]
    assert list(host._startup_live_rest_queue) == keys


def test_timeout_reveals_once_and_reports_timeout_reason(monkeypatch):
    host, scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    phases = []
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(
        window,
        "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    host._startup_perf_origin_ns = time.monotonic_ns()
    host._startup_perf_db_apply_context = {}
    host._startup_perf_list_publication_ns = time.monotonic_ns()
    keys = [f"server-{index}" for index in range(10)]
    host._submit_startup_rest_batches(keys, warmup_count=10)

    scheduled[0][1](scheduled[0][2])
    scheduled[0][1](scheduled[0][2])

    assert events == ["flush", "sort", "reveal"]
    threshold = next(fields for phase, fields in phases if phase == "a2s_warmup_threshold")
    reveal = next(fields for phase, fields in phases if phase == "a2s_warmup_reveal")
    assert threshold["reason"] == "timeout"
    assert reveal["reveal_reason"] == "timeout"
    assert reveal["completed"] == 0
    assert reveal["target"] == 6
    assert reveal["publication_to_overlay_dismissal_ms"] != "unknown"


@pytest.mark.parametrize("experimental", (False, True), ids=("default", "experimental"))
@pytest.mark.parametrize("failure_mode", ("missing", "invalid", "submission"))
def test_sweep_exhaustion_is_truthful_and_reveals_exactly_once(
        monkeypatch, experimental, failure_mode):
    host, _scheduled, removed, events = warmup_host(
        monkeypatch, experimental=experimental,
    )
    phases = []
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(
        window, "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    monkeypatch.setattr(window, "STARTUP_LIVE_REST_WORKERS", 8)
    host._startup_perf_origin_ns = time.monotonic_ns()
    host._startup_perf_db_apply_context = {}
    host._startup_perf_list_publication_ns = time.monotonic_ns()
    keys = ["a", "b", "c"]
    if failure_mode == "missing":
        host._obj_by_key = {}
    else:
        qport = object() if failure_mode == "invalid" else 2303
        host._obj_by_key = {
            key: SimpleNamespace(ip=f"10.0.0.{index}", qport=qport)
            for index, key in enumerate(keys, 1)
        }
    if failure_mode == "submission":
        host._startup_live_executor = SimpleNamespace(
            submit=lambda *_args: (_ for _ in ()).throw(RuntimeError("submit failed")),
        )
        host._query_startup_rest_one = lambda *_args: None
    bind(host, "_pump_startup_rest_sweep", "_maybe_finish_startup_rest_sweep")

    host._submit_startup_rest_batches(keys, warmup_count=len(keys))
    host._maybe_finish_startup_rest_sweep(host._startup_live_generation)

    assert host._startup_live_rest_completed == len(keys)
    assert host._startup_warmup_completed == 0
    assert host._startup_warmup_target == 2
    assert events == ["flush", "sort", "reveal"]
    assert removed == [41]
    threshold = [fields for phase, fields in phases if phase == "a2s_warmup_threshold"]
    reveal = [fields for phase, fields in phases if phase == "a2s_warmup_reveal"]
    sweep = [fields for phase, fields in phases if phase == "a2s_sweep_completion"]
    assert len(threshold) == len(reveal) == len(sweep) == 1
    assert threshold[0]["reason"] == "sweep_exhausted"
    assert reveal[0]["reveal_reason"] == "sweep_exhausted"
    assert reveal[0]["completed"] == 0
    assert reveal[0]["target"] == 2
    assert sweep[0]["completed"] == len(keys)
    assert sweep[0]["total"] == len(keys)


def test_stale_generation_completion_cannot_advance_or_reveal(monkeypatch):
    host, _scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    keys = ["a", "b"]
    host._submit_startup_rest_batches(keys, warmup_count=2)
    host._startup_live_generation = 2

    host._on_startup_rest_future_done(1, Future(1, "a"), "a")

    assert host._startup_warmup_completed == 0
    assert events == []


def test_manual_replacement_keeps_default_warmup_when_experiment_enabled(monkeypatch):
    host, _scheduled, _removed, _events = warmup_host(
        monkeypatch, experimental=True, startup_completed=True,
    )
    keys = [f"server-{index}" for index in range(600)]

    host._submit_startup_rest_batches(keys, warmup_count=520)

    assert host._startup_warmup_mode == "default"
    assert host._startup_warmup_cohort_count == 520
    assert host._startup_warmup_target == 338


def test_full_sweep_completion_is_independent_of_early_reveal(monkeypatch):
    host, _scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    keys = [f"server-{index}" for index in range(10)]
    host._submit_startup_rest_batches(keys, warmup_count=5)
    for key in keys[:3]:
        host._on_startup_rest_future_done(1, Future(1, key), key)

    assert events == ["flush", "sort", "reveal"]
    assert list(host._startup_live_rest_queue) == keys
    assert host._startup_live_rest_total == len(keys)


def test_threshold_reveal_respects_user_changed_sort(monkeypatch):
    host, _scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    host._sort_changed_by_user = True
    host.sort_key = "players"
    host.sort_asc = False
    host._set_sort = lambda *_args, **_kwargs: pytest.fail("user sort was replaced")
    bind(host, "_auto_sort_lowest_ping_after_startup")
    host._submit_startup_rest_batches(["a"], warmup_count=1)

    host._on_startup_rest_future_done(1, Future(1, "a"), "a")

    assert events == ["flush", "reveal"]
    assert (host.sort_key, host.sort_asc) == ("players", False)


def test_shutdown_generation_guard_ignores_late_completion(monkeypatch):
    host, _scheduled, _removed, events = warmup_host(monkeypatch, experimental=True)
    host._submit_startup_rest_batches(["a"], warmup_count=1)
    host._shutdown_cleanup_done = True
    host._startup_live_generation += 1

    host._on_startup_rest_future_done(1, Future(1, "a"), "a")

    assert events == []
    assert host._startup_warmup_completed == 0
