"""Offline lifecycle checks for deferred batched-startup autocomplete data."""

import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from dzll_launcher import mod_suggestion_cache, mod_suggestions, window
from dzll_launcher.window import DZLLWindow


PAYLOADS = (
    '[{"steamWorkshopId":"42","name":"Code Lock"}]',
    '[{"steamWorkshopId":"77","name":"Community Framework"}]',
)


class CapturingExecutor:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []
        self.future = Future()

    def submit(self, fn, *args, **kwargs):
        if self.fail:
            raise RuntimeError("submission failed")
        self.calls.append((fn, args, kwargs))
        return self.future


def make_host(tmp_path, executor=None):
    executor = executor or CapturingExecutor()
    return SimpleNamespace(
        _shutdown_cleanup_done=False,
        _suggestion_cache_ready=False,
        _suggestion_cache_expired=False,
        _suggestion_cache_created_at=0.0,
        _suggestion_cache_generation=4,
        _suggestion_cache_migration_pending=False,
        _suggestion_cache_migration_created_at=0.0,
        _suggestion_cache_migration_inflight=False,
        _suggestion_refresh_inflight=False,
        _server_db_update_inflight=False,
        _deferred_suggestion_restore_pending=True,
        _deferred_suggestion_restore_allowed=True,
        _deferred_suggestion_restore_inflight=False,
        _deferred_suggestion_restore_generation=0,
        _deferred_suggestion_restore_retry_after=0.0,
        _suggestion_cache_path=tmp_path / "mod_suggestions_v1.json",
        _obj_by_key={
            "1.1.1.1:2302": SimpleNamespace(mods_json=PAYLOADS[0]),
            "2.2.2.2:2302": SimpleNamespace(mods_json=PAYLOADS[1]),
        },
        _mod_suggestion_index=mod_suggestions.build_mod_suggestion_index(()),
        _mod_suggestion_last_rows=(),
        _update_executor=executor,
        _queue_mod_suggestions_refresh=lambda: None,
        _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_context={"load_id": 8, "input_rows": 2, "valid_rows": 2},
    )


def test_deferred_worker_loads_valid_cache_as_plain_data(tmp_path):
    path = tmp_path / "cache"
    original = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    mod_suggestion_cache.save_suggestion_cache(path, original)
    result = mod_suggestion_cache.prepare_deferred_suggestions(
        path, PAYLOADS, measure=True,
    )
    assert result.outcome == "cache_hit"
    assert result.index == original
    assert result.stages["bytes_read"] == path.stat().st_size
    assert result.stages["worker_total_ns"] >= result.stages["total_ns"]


def test_missing_or_corrupt_cache_rebuilds_without_publishing_from_worker(tmp_path):
    for name, initial in (("missing", None), ("corrupt", b"not-json")):
        path = tmp_path / name
        if initial is not None:
            path.write_bytes(initial)
        before = path.read_bytes() if path.exists() else None
        result = mod_suggestion_cache.prepare_deferred_suggestions(
            path, PAYLOADS, measure=True,
        )
        assert result.outcome == "cache_miss_rebuilt"
        assert result.index["count"] == 2
        assert result.format_version == 0
        assert (path.read_bytes() if path.exists() else None) == before
        assert result.stages["fallback_build_ns"] >= 0


def test_overlay_completion_is_the_deferred_submission_boundary(tmp_path, monkeypatch):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    host._deferred_suggestion_restore_allowed = False
    host._set_updating = lambda *_args: None
    host._apply_titlebar_counts = lambda: None
    host._startup_presentation = None
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    assert DZLLWindow._maybe_schedule_deferred_suggestion_restore(host) is False
    assert executor.calls == []
    DZLLWindow._complete_startup_presentation(host)
    assert host._deferred_suggestion_restore_allowed is True
    assert len(executor.calls) == 1


def test_successful_deferred_result_publishes_on_main_callback_and_refreshes_open_query(
        tmp_path, monkeypatch):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    queued = []
    refreshes = []
    host._queue_mod_suggestions_refresh = lambda: refreshes.append("refresh")
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    monkeypatch.setattr(
        window.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
    DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
    assert len(executor.calls) == 1
    fn, args, kwargs = executor.calls[0]
    assert fn is mod_suggestion_cache.prepare_deferred_suggestions
    assert args[0] == host._suggestion_cache_path
    assert args[1] == PAYLOADS
    assert all(isinstance(value, str) for value in args[1])
    assert args[1][0] is host._obj_by_key["1.1.1.1:2302"].mods_json
    executor.future.set_result(fn(*args, **kwargs))
    assert host._suggestion_cache_ready is False
    callback, callback_args = queued.pop()
    assert callback(*callback_args) is False
    assert host._suggestion_cache_ready is True
    assert host._deferred_suggestion_restore_pending is False
    assert host._mod_suggestion_index["count"] == 2
    assert host._suggestion_cache_migration_pending is True
    assert refreshes == ["refresh"]


def test_early_autocomplete_is_unavailable_without_sync_rebuild_and_selected_chips_work():
    events = []
    host = SimpleNamespace(
        _suggestion_cache_ready=False,
        _deferred_suggestion_restore_pending=True,
        _deferred_suggestion_restore_inflight=False,
        _mod_suggestion_selecting=False,
        _mod_suggestion_dismissed=None,
        _mod_suggestion_index=mod_suggestions.build_mod_suggestion_index(PAYLOADS),
        _selected_mod_chips=[{"display_name": "Code Lock", "query_term": "42"}],
        mod_suggestion_panel=SimpleNamespace(),
        mod_suggestion_list=SimpleNamespace(),
        search_entry=SimpleNamespace(get_text=lambda: "Code"),
        _current_mod_suggestion_token=lambda: ("Code", 0, 4, "code"),
        _mod_suggestion_dismiss_key=lambda *_args: ("Code", 0, "code"),
        _hide_mod_suggestions=lambda reason: events.append(reason),
        _log_mod_suggest=lambda _message: None,
        _rebuild_mod_suggestion_index=lambda: events.append("sync_rebuild"),
    )
    DZLLWindow._refresh_mod_suggestions(host)
    assert events == ["catalogue_not_ready"]
    assert DZLLWindow._resolve_exact_mod_chip(host, "Code Lock") is None
    assert DZLLWindow._selected_mod_chip_query_terms(host) == ("42",)


def test_submission_failure_stays_unavailable_and_retryable(tmp_path, monkeypatch):
    host = make_host(tmp_path, CapturingExecutor(fail=True))
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
    assert host._suggestion_cache_ready is False
    assert host._deferred_suggestion_restore_pending is True
    assert host._deferred_suggestion_restore_inflight is False
    assert host._deferred_suggestion_restore_retry_after > time.monotonic()


def test_empty_published_endpoint_map_cannot_schedule_deferred_persistence(
        tmp_path, monkeypatch):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    host._obj_by_key = {}
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)

    assert DZLLWindow._maybe_schedule_deferred_suggestion_restore(host) is False

    assert executor.calls == []
    assert host._deferred_suggestion_restore_inflight is False
    assert host._suggestion_cache_ready is False


def test_worker_failure_stays_unavailable_and_retryable(tmp_path, monkeypatch):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    queued = []
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    monkeypatch.setattr(
        window.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
    executor.future.set_exception(RuntimeError("decode worker failed"))
    callback, callback_args = queued.pop()
    callback(*callback_args)
    assert host._suggestion_cache_ready is False
    assert host._deferred_suggestion_restore_pending is True
    assert host._deferred_suggestion_restore_inflight is False
    assert host._deferred_suggestion_restore_retry_after > time.monotonic()


def test_shutdown_and_generation_replacement_reject_worker_results(tmp_path, monkeypatch):
    for mode in ("shutdown", "replacement"):
        executor = CapturingExecutor()
        host = make_host(tmp_path / mode, executor)
        queued = []
        monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
        monkeypatch.setattr(
            window.GLib, "idle_add",
            lambda callback, *args: queued.append((callback, args)) or 1,
        )
        DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
        fn, args, kwargs = executor.calls[0]
        executor.future.set_result(fn(*args, **kwargs))
        if mode == "shutdown":
            host._shutdown_cleanup_done = True
        else:
            host._suggestion_cache_generation += 1
            host._mod_suggestion_index = mod_suggestions.build_mod_suggestion_index(
                ('[{"steamWorkshopId":"99","name":"Newer"}]',)
            )
        callback, callback_args = queued.pop()
        callback(*callback_args)
        assert host._suggestion_cache_ready is False
        assert host._mod_suggestion_index["count"] == (0 if mode == "shutdown" else 1)


def test_deferred_perf_phases_keep_load_identity(tmp_path, monkeypatch):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    queued = []
    phases = []
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(
        window, "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    monkeypatch.setattr(
        window.GLib, "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 1,
    )
    DZLLWindow._maybe_schedule_deferred_suggestion_restore(host)
    fn, args, kwargs = executor.calls[0]
    executor.future.set_result(fn(*args, **kwargs))
    callback, callback_args = queued.pop()
    callback(*callback_args)
    by_name = {phase: fields for phase, fields in phases}
    assert by_name["deferred_suggestion_schedule"]["result"] == "submitted"
    assert by_name["deferred_suggestion_worker"]["result"] == "cache_miss_rebuilt"
    assert by_name["deferred_suggestion_apply"]["result"] == "committed"
    assert by_name["autocomplete_ready"]["load_id"] == 8
    assert {fields["load_id"] for fields in by_name.values()} == {8}


@pytest.mark.parametrize(
    "format_version,outcome,expected_kind",
    [
        (0, "cache_miss_rebuilt", "first_save"),
        (1, "cache_hit", "v1_migration"),
        (2, "cache_hit", None),
    ],
)
def test_deferred_cache_publication_instrumentation_distinguishes_kind(
        tmp_path, monkeypatch, format_version, outcome, expected_kind):
    executor = CapturingExecutor()
    host = make_host(tmp_path, executor)
    host._suggestion_sweep_complete = True
    phases = []
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(
        window, "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    result = SimpleNamespace(
        index=mod_suggestions.build_mod_suggestion_index(PAYLOADS),
        expired=False,
        created_at=1_000_000_000,
        format_version=format_version,
        outcome=outcome,
        stages={},
    )
    completed = Future()
    completed.set_result(result)

    assert DZLLWindow._on_deferred_suggestion_restore_done(
        host, host._suggestion_cache_generation, time.monotonic_ns(), {}, completed,
    ) is False
    assert host._suggestion_cache_migration_kind == expected_kind
    assert host._suggestion_cache_migration_pending is (format_version in (0, 1))

    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    DZLLWindow._maybe_schedule_mod_suggestion_migration(host)
    if format_version == 2:
        assert executor.calls == []
        return
    assert len(executor.calls) == 1
    staged = mod_suggestion_cache.build_staged_suggestion_migration(
        host._suggestion_cache_path, host._mod_suggestion_index,
        host._suggestion_cache_migration_created_at,
    )
    executor.future.set_result(staged)
    assert DZLLWindow._on_mod_suggestion_migration_done(
        host, host._suggestion_cache_generation, time.monotonic_ns(), executor.future,
    ) is False
    assert host._suggestion_cache_migration_pending is False

    by_name = {phase: fields for phase, fields in phases}
    assert by_name["mod_suggestion_cache_migration_start"]["kind"] == expected_kind
    assert by_name["mod_suggestion_cache_migration_complete"]["kind"] == expected_kind

