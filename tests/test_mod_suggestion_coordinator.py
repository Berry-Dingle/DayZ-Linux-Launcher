"""Lifecycle tests for ModSuggestionCacheCoordinator with stub dependencies (no window)."""

import hashlib
import json
from collections import deque
from types import SimpleNamespace

import pytest

from dzll_launcher import mod_suggestion_cache, mod_suggestion_coordinator as module, mod_suggestions
from dzll_launcher.mod_suggestion_coordinator import ModSuggestionCacheCoordinator

PAYLOADS = (
    '[{"steamWorkshopId":"42","name":"Code Lock"}]',
    '[{"steamWorkshopId":"77","name":"Community Framework"}]',
)


class Future:
    def __init__(self, fn, args, kwargs):
        self.fn, self.args, self.kwargs, self.callbacks = fn, args, kwargs, []
        self.value = self.error = None

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value

    def run(self, error=None):
        if error is None:
            self.value = self.fn(*self.args, **self.kwargs)
        else:
            self.error = error
        for callback in self.callbacks:
            callback(self)


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = SimpleNamespace(
        servers={f"k{i}": SimpleNamespace(mods_json=payload) for i, payload in enumerate(PAYLOADS)},
        parsed=None, fetched_ok=True, provisional=False, db_inflight=False, sweep_ready=True,
        shutdown=False, pending=deque(), idles=deque(), events=[], phases=[],
    )
    monkeypatch.setattr(module.GLib, "idle_add", lambda callback, *args: state.idles.append((callback, args)) or 1)

    def submit(fn, *args, **kwargs):
        future = Future(fn, args, kwargs)
        state.pending.append(future)
        return future

    state.coordinator = ModSuggestionCacheCoordinator(
        cache_path=tmp_path / "mod_suggestions_v1.json",
        servers=lambda: state.servers,
        parsed_mods_by_key=lambda: state.parsed,
        db_fetched_ok=lambda: state.fetched_ok,
        provisional_load=lambda: state.provisional,
        db_update_inflight=lambda: state.db_inflight,
        sweep_ready=lambda: state.sweep_ready,
        is_shut_down=lambda: state.shutdown,
        submit=submit,
        on_index_replaced=lambda: state.events.append("rows_reset"),
        on_restored=lambda: state.events.append("restored"),
        perf_enabled=lambda: False,
        perf_context=lambda: None,
        log_apply_phase=lambda *args, **kwargs: state.phases.append(args),
        log_phase=lambda *args, **kwargs: state.phases.append(args),
    )

    def complete(error=None):
        state.pending.popleft().run(error)
        while state.idles:
            callback, args = state.idles.popleft()
            assert callback(*args) is False

    state.complete = complete
    state.path = tmp_path / "mod_suggestions_v1.json"
    return state


def test_restore_state_operations():
    coordinator = ModSuggestionCacheCoordinator(
        cache_path=None, servers=dict, parsed_mods_by_key=lambda: None, db_fetched_ok=lambda: False,
        provisional_load=lambda: False, db_update_inflight=lambda: False, sweep_ready=lambda: False,
        is_shut_down=lambda: False, submit=None, on_index_replaced=lambda: None, on_restored=lambda: None,
        perf_enabled=lambda: False, perf_context=lambda: None, log_apply_phase=None, log_phase=None,
    )
    assert coordinator.index == mod_suggestions.build_mod_suggestion_index(())
    assert coordinator.allow_pending_restore() is False and coordinator.restore_allowed is False

    coordinator.mark_restore_pending()
    assert (coordinator.restore_pending, coordinator.restore_allowed) == (True, False)
    assert coordinator.allow_pending_restore() is True and coordinator.restore_allowed is True

    coordinator.restore_inflight = True
    coordinator.invalidate()
    assert coordinator.generation == 1
    assert (coordinator.restore_pending, coordinator.restore_allowed, coordinator.restore_inflight) == (False, False, False)

    coordinator.mark_restore_pending()
    coordinator.drop_pending_restore()
    assert (coordinator.restore_pending, coordinator.restore_allowed) == (False, False)


def test_rebuild_builds_persists_and_reuses_the_catalogue(env):
    env.coordinator.rebuild_index()

    expected = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    assert env.coordinator.index == expected
    assert env.coordinator.ready is True and env.coordinator.expired is False
    assert mod_suggestion_cache.load_suggestion_cache(env.path).index == expected
    assert env.events == ["rows_reset"]

    env.servers = {}
    env.coordinator.rebuild_index()  # memory hit: keeps the catalogue, only resets popover rows
    assert env.coordinator.index == expected
    assert env.events == ["rows_reset", "rows_reset"]


def test_provisional_load_builds_in_memory_without_persisting(env):
    env.provisional = True

    env.coordinator.rebuild_index()

    assert env.coordinator.index["count"] == 2
    assert env.coordinator.ready is False
    assert not env.path.exists()


def test_disk_cache_is_loaded_instead_of_rebuilding(env, monkeypatch):
    cached = mod_suggestions.build_mod_suggestion_index(('[{"name":"Cached"}]',))
    mod_suggestion_cache.save_suggestion_cache(env.path, cached)
    monkeypatch.setattr(module, "build_mod_suggestion_index", lambda *_a, **_k: pytest.fail("rebuilt"))

    env.coordinator.rebuild_index()

    assert env.coordinator.index == cached
    assert env.coordinator.ready is True
    assert env.coordinator.migration_pending is False


def test_deferred_restore_commits_on_main_callback(env):
    env.coordinator.mark_restore_pending()
    env.coordinator.schedule_deferred_restore()
    assert env.pending == deque()  # not allowed yet

    env.coordinator.allow_pending_restore()
    env.coordinator.schedule_deferred_restore()
    env.coordinator.schedule_deferred_restore()
    assert len(env.pending) == 1 and env.coordinator.restore_inflight is True
    assert env.pending[0].fn is mod_suggestion_cache.prepare_deferred_suggestions
    assert env.pending[0].kwargs == {"measure": False}

    env.complete()

    assert env.coordinator.ready is True
    assert env.coordinator.index["count"] == 2
    assert env.coordinator.migration_kind == "first_save" and env.coordinator.migration_pending is True
    assert (env.coordinator.restore_pending, env.coordinator.restore_inflight) == (False, False)
    assert env.events == ["rows_reset", "restored"]


def test_invalidate_rejects_inflight_restore_result(env):
    env.coordinator.mark_restore_pending()
    env.coordinator.allow_pending_restore()
    env.coordinator.schedule_deferred_restore()

    env.coordinator.invalidate()
    env.complete()

    assert env.coordinator.ready is False
    assert env.coordinator.index["count"] == 0
    assert env.events == []


def test_restore_worker_failure_backs_off(env, monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    env.coordinator.mark_restore_pending()
    env.coordinator.allow_pending_restore()
    env.coordinator.schedule_deferred_restore()

    env.complete(error=RuntimeError("worker failed"))

    assert env.coordinator.restore_retry_after == 100.0 + module.RETRY_DELAY_SECONDS
    assert env.coordinator.restore_pending is True and env.coordinator.restore_inflight is False
    env.coordinator.schedule_deferred_restore()
    assert not env.pending


def test_expired_catalogue_refreshes_after_sweep_and_publishes(env):
    env.coordinator.rebuild_index()
    env.coordinator.created_at = 1.0  # far in the past: expires on the next schedule
    env.servers = {"k": SimpleNamespace(mods_json='[{"name":"Fresh"}]')}
    env.sweep_ready = False
    env.coordinator.schedule_refresh()
    assert env.coordinator.expired is True and not env.pending

    env.sweep_ready = True
    env.coordinator.schedule_refresh()
    env.coordinator.schedule_refresh()
    assert len(env.pending) == 1 and env.coordinator.refresh_inflight is True

    env.complete()

    expected = mod_suggestions.build_mod_suggestion_index(('[{"name":"Fresh"}]',))
    assert env.coordinator.index == expected
    assert env.coordinator.expired is False and env.coordinator.refresh_inflight is False
    assert mod_suggestion_cache.load_suggestion_cache(env.path).index == expected


def test_stale_refresh_discards_staged_file_and_keeps_cache(env):
    env.coordinator.rebuild_index()
    before = env.path.read_bytes()
    old_index = env.coordinator.index
    env.coordinator.expired = True
    env.servers = {"k": SimpleNamespace(mods_json='[{"name":"Fresh"}]')}
    env.coordinator.schedule_refresh()
    future = env.pending[0]

    env.coordinator.invalidate()
    env.complete()

    _replacement, staged = future.value
    assert not staged.exists()
    assert env.path.read_bytes() == before
    assert env.coordinator.index is old_index


def test_v1_cache_migrates_to_current_format_without_changing_index(env):
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    body = {key: index[key] for key in ("mods", "count", "prefix_map", "exact_compact_map")}
    env.path.write_text(json.dumps({
        "version": 1, "created_at": 1_000_000_000, "index": body,
        "sha256": hashlib.sha256(mod_suggestion_cache._index_bytes(body)).hexdigest(),
    }))
    env.coordinator.rebuild_index()
    assert env.coordinator.migration_pending is True and env.coordinator.migration_kind == "v1_migration"
    loaded_index = env.coordinator.index

    env.coordinator.schedule_migration()
    env.coordinator.schedule_refresh()  # migration in flight blocks a refresh
    assert len(env.pending) == 1
    env.complete()

    assert env.coordinator.migration_pending is False and env.coordinator.migration_kind is None
    assert env.coordinator.index is loaded_index
    assert mod_suggestion_cache.load_suggestion_cache(env.path).format_version == 2


def test_shutdown_discards_completed_migration(env):
    index = mod_suggestions.build_mod_suggestion_index(PAYLOADS)
    env.coordinator.index = index
    env.coordinator.migration_pending = True
    env.coordinator.created_at = 1_000_000_000
    env.coordinator.schedule_migration()
    future = env.pending[0]

    env.shutdown = True
    env.complete()

    assert not future.value.exists()
    assert not env.path.exists()
    assert env.coordinator.migration_pending is True
