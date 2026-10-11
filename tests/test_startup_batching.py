"""Focused offline checks for the opt-in GTK startup batching transaction."""

import threading
import time
from concurrent.futures import Future
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window
from dzll_launcher.window import DZLLWindow


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_n_items(self):
        return len(self.rows)

    def get_item(self, index):
        return self.rows[index]

    def append(self, row):
        self.rows.append(row)

    def splice(self, index, count, replacement):
        self.rows[index:index + count] = list(replacement)


class StringModel(Store):
    def get_string(self, index):
        return self.rows[index]


class Dropdown:
    def __init__(self, selected=0):
        self.selected = selected

    def get_selected(self):
        return self.selected

    def set_selected(self, selected):
        self.selected = selected


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))


def row(name, ip, ping, *, favorite=False, played=None, mods="[]"):
    return {
        "name": name, "ip": ip, "gport": 2302, "qport": 27016,
        "map": "ChernarusPlus", "players": 10, "maxPlayers": 60,
        "password": 0, "third_person": 1, "official": 0,
        "mods": mods, "modCount": 0, "time": "12:00",
        "timeWarp": 1.0, "nightTimeWarp": 1.0, "ping": ping,
        "bm_rank": 50, "_favorite": favorite, "_played": played,
    }


def make_host(rows, events, *, source_store=None, filter_func=None):
    source = source_store or Store()
    visible = Store()
    favorites = {
        f"{item['ip']}:{item['gport']}": True for item in rows if item.get("_favorite")
    }
    last_played = {
        f"{item['ip']}:{item['gport']}": item["_played"]
        for item in rows if item.get("_played") is not None
    }
    host = SimpleNamespace(
        store=source, column_view_store=visible, sort_model=None, sorter=None,
        _obj_by_key={}, _browser_live_offline_streaks={}, live={},
        favorites=favorites, last_played=last_played, settings={},
        sort_key="ping", sort_asc=True,
        _shutdown_cleanup_done=False, _startup_db_provisional=False,
        _suggestion_provisional_load=False, _suggestion_cache_generation=0,
        _server_mod_cache_generation=0, _server_mod_cache_records={},
        _server_mod_cache_persisted={}, _server_mod_cache_candidate={},
        _server_mod_cache_needs_repair=False, _server_mod_cache_file_bytes=0,
        _startup_row_batch_generation=0, _startup_row_batch_idle_id=0,
        _startup_row_batch_state=None, _startup_live_generation=0,
        _server_companion_rows_loaded=False, _startup_perf_origin_ns=time.monotonic_ns(),
        _startup_perf_db_apply_id=0, _startup_perf_db_apply_context={},
        _suggestion_cache_ready=True, _suggestion_cache_expired=False,
        _suggestion_sweep_complete=False, _suggestion_sweep_fallback_ready=False,
        _server_db_update_inflight=True,
        _set_map_choices=lambda _choices: events.append("maps"),
        _is_likely_test_server_name=lambda _name: False,
        _debug_sort_attach_notify_probe=lambda _obj: None,
        _rebuild_mod_suggestion_index=lambda: events.append("suggestions"),
        _snapshot_all_sort_keys=lambda: events.append("snapshot"),
        _restore_server_companion_if_enabled=lambda: events.append("companion"),
        _set_updating=lambda *_args: events.append("updating"),
        _submit_startup_rest_batches=lambda *_args, **_kwargs: events.append("a2s"),
        _bm_live_group=lambda key: int(getattr(host._obj_by_key[key], "bm_rank", 999999999)),
        _offline_recheck_tick=lambda: False,
        _apply_titlebar_counts=lambda: events.append("title"),
        _complete_startup_presentation=lambda: events.append("overlay_complete"),
        _auto_sort_lowest_ping_after_startup=lambda: events.append("cached_sort"),
        empty_label=SimpleNamespace(
            set_text=lambda text: events.append(("empty_text", text)),
            set_visible=lambda visible: events.append(("empty_visible", visible)),
        ),
    )

    def rebuild(*, reason):
        selected = [obj for obj in source.rows if filter_func is None or filter_func(obj)]
        selected.sort(key=lambda obj: (obj.ping, obj.name.lower()))
        visible.splice(0, visible.get_n_items(), selected)
        events.append(("visible", reason))

    host._on_filter_changed = rebuild
    bind(
        host, "_start_startup_row_batches", "_build_startup_batched_row",
        "_run_startup_row_batch", "_publish_startup_row_batches",
        "_rollback_startup_row_publication", "_fail_startup_row_batches",
        "_cancel_startup_row_batches", "_finish_db_rows_application",
    )
    host._load_rows_into_store = MethodType(DZLLWindow._load_rows_into_store, host)
    return host


def install_idle_queue(monkeypatch):
    queued = []
    removed = []

    def idle_add(callback, *args):
        queued.append((callback, args))
        return len(queued)

    monkeypatch.setattr(window.GLib, "idle_add", idle_add)
    monkeypatch.setattr(window.GLib, "source_remove", lambda source_id: removed.append(source_id))
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_args: 71)
    return queued, removed


def drain(item):
    callback, args = item
    while callback(*args):
        pass


def install_map_state(host, choices=("All Maps", "Livonia"), selected=1):
    host.map_model = StringModel(choices)
    host.map_dropdown = Dropdown(selected)
    host._filter_state = {"selected_map": choices[selected], "marker": "original"}
    host._filter_refresh_suppress_depth = 0
    bind(host, "_set_map_choices", "_push_filter_refresh_suppression", "_pop_filter_refresh_suppression")
    on_filter_changed = host._on_filter_changed

    def rebuild_with_map_state(*, reason):
        selected_index = host.map_dropdown.get_selected()
        host._filter_state = {
            "selected_map": host.map_model.get_string(selected_index),
            "marker": "published",
        }
        on_filter_changed(reason=reason)

    host._on_filter_changed = rebuild_with_map_state


def map_state(host):
    return (
        list(host.map_model.rows),
        host.map_dropdown.get_selected(),
        dict(host._filter_state),
    )


def test_default_path_is_synchronous_and_opt_in_path_is_idle_batched(monkeypatch):
    rows = [row("One", "10.0.0.1", 40)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", False)
    events = []
    host = make_host(rows, events)
    assert DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True) is False
    assert queued == []
    assert [obj.name for obj in host.store.rows] == ["One"]
    assert "suggestions" in events
    assert host._deferred_suggestion_restore_pending is False

    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    result = DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    assert result is window._STARTUP_DB_APPLY_PENDING
    assert host.store.rows == [] and "a2s" not in events
    drain(queued[-1])
    assert [obj.name for obj in host.store.rows] == ["One"]
    assert events.index(("visible", "load")) < events.index("a2s")


def test_batched_source_filter_sort_favorite_played_and_duplicate_parity(monkeypatch):
    rows = [
        row(
            "Zulu", "10.0.0.1", 80, favorite=True, played=int(time.time()),
            mods='[{"steamWorkshopId":"42","name":"Example Mod"}]',
        ),
        row("Alpha", "10.0.0.2", 20),
        row("Zulu replacement", "10.0.0.1", 10, favorite=True),
    ]
    rows[0]["modCount"] = 7
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", False)
    sync_events = []
    sync = make_host(rows, sync_events, filter_func=lambda obj: obj.fav)
    DZLLWindow._apply_db_rows(sync, rows, True, allow_startup_batching=True)

    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "STARTUP_ROW_BATCH_BUDGET_NS", 0)
    batch_events = []
    batch = make_host(rows, batch_events, filter_func=lambda obj: obj.fav)
    assert DZLLWindow._apply_db_rows(
        batch, rows, True, allow_startup_batching=True,
    ) is window._STARTUP_DB_APPLY_PENDING
    drain(queued[-1])

    fields = lambda obj: (
        obj.name, obj.ip, obj.gport, obj.fav, obj.played, obj.mods_json,
        obj.mod_count, obj.mod_search_index,
    )
    assert [fields(obj) for obj in batch.store.rows] == [fields(obj) for obj in sync.store.rows]
    assert [fields(obj) for obj in batch.column_view_store.rows] == [
        fields(obj) for obj in sync.column_view_store.rows
    ]
    assert batch._obj_by_key["10.0.0.1:2302"].name == "Zulu replacement"
    assert len(batch._startup_row_batch_state or {}) == 0


@pytest.mark.parametrize(
    "allow,fetched,provisional,legacy,expected",
    [
        (False, True, False, False, "unsupported_caller"),
        (True, False, False, False, "nonfinal_database"),
        (True, True, True, False, "provisional_or_replacement"),
        (True, True, False, True, "legacy_model"),
    ],
)
def test_unsupported_startup_paths_retain_synchronous_fallback(
        monkeypatch, allow, fetched, provisional, legacy, expected):
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "ENABLE_LEGACY_GTK_SORT_MODEL", legacy)
    host = SimpleNamespace(
        _suggestion_provisional_load=provisional, _startup_db_provisional=False,
        _shutdown_cleanup_done=False, sort_model=None,
        store=Store(), column_view_store=Store(), _obj_by_key={},
    )
    assert DZLLWindow._startup_batching_fallback_reason(host, fetched, allow) == expected


def test_cancellation_rejects_late_idle_callback_without_publication(monkeypatch):
    rows = [row(f"Server {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    queued, removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    install_map_state(host)
    before = map_state(host)
    DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    callback, args = queued[-1]
    host._cancel_startup_row_batches("superseded")
    assert removed == [1]
    assert callback(*args) is False
    assert host.store.rows == [] and host.column_view_store.rows == []
    assert "a2s" not in events
    assert map_state(host) == before


def test_superseding_batch_snapshots_the_restored_map_state(monkeypatch):
    first_rows = [row(f"First {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    second_rows = [row(f"Second {index}", f"10.0.1.{index}", 40) for index in range(1, 7)]
    for item in second_rows:
        item["map"] = "Sakhal"
    queued, removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    host = make_host(first_rows, [])
    install_map_state(host)
    before = map_state(host)

    assert DZLLWindow._apply_db_rows(
        host, first_rows, True, allow_startup_batching=True,
    ) is window._STARTUP_DB_APPLY_PENDING
    assert host.map_model.rows == ["All Maps", "Chernarus"]

    assert DZLLWindow._apply_db_rows(
        host, second_rows, True, allow_startup_batching=True,
    ) is window._STARTUP_DB_APPLY_PENDING
    assert removed == [1]
    assert host._startup_row_batch_state["previous_map_state"] == {
        "choices": tuple(before[0]), "selected": before[1], "filter_state": before[2],
    }
    assert host.map_model.rows == ["All Maps", "Sakhal"]

    host._cancel_startup_row_batches("shutdown")
    assert removed == [1, 2]
    assert map_state(host) == before
    assert host._filter_refresh_suppress_depth == 0
    assert queued[0][0](*queued[0][1]) is False
    assert queued[1][0](*queued[1][1]) is False


def test_shutdown_during_construction_releases_staging_and_rejects_late_callback(monkeypatch):
    rows = [row("One", "10.0.0.1", 40)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    callback, args = queued[-1]
    host._shutdown_cleanup_done = True
    host._cancel_startup_row_batches("shutdown")
    assert callback(*args) is False
    assert host._startup_row_batch_state is None
    assert host.store.rows == [] and "a2s" not in events


def test_row_construction_failure_recovers_overlay_without_a2s_or_cache_timer(monkeypatch):
    rows = [row(f"Server {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    queued, _removed = install_idle_queue(monkeypatch)
    timers = []
    monkeypatch.setattr(
        window.GLib, "timeout_add_seconds",
        lambda _seconds, callback, *_args: timers.append(callback) or 71,
    )
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    install_map_state(host)
    before = map_state(host)
    host._build_startup_batched_row = lambda *_args: (_ for _ in ()).throw(RuntimeError("bad row"))
    outcomes = []
    DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
        batched_complete=outcomes.append,
    )
    drain(queued[-1])
    assert outcomes == ["row_construction_failed"]
    assert "overlay_complete" in events and "a2s" not in events
    assert timers == []
    assert host.store.rows == [] and host.column_view_store.rows == []
    assert map_state(host) == before


def test_publication_failure_rolls_back_and_recovers_overlay(monkeypatch):
    rows = [row(f"Server {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    queued, _removed = install_idle_queue(monkeypatch)
    timers = []
    monkeypatch.setattr(
        window.GLib, "timeout_add_seconds",
        lambda _seconds, callback, *_args: timers.append(callback) or 71,
    )
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    install_map_state(host)
    before = map_state(host)
    def fail_after_filter_state_change(**_kwargs):
        host._filter_state = {"selected_map": "Chernarus", "marker": "publication-dirty"}
        raise RuntimeError("publication failed")

    host._on_filter_changed = fail_after_filter_state_change
    outcomes = []
    DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
        batched_complete=outcomes.append,
    )
    drain(queued[-1])
    assert outcomes == ["publication_failed"]
    assert "overlay_complete" in events and "a2s" not in events
    assert timers == []
    assert host.store.rows == [] and host.column_view_store.rows == []
    assert host._obj_by_key == {} and host.live == {}
    assert host._server_db_update_inflight is False
    assert map_state(host) == before


def test_map_restore_balances_filter_suppression_when_widget_raises():
    host = SimpleNamespace(
        map_model=StringModel(("All Maps", "Chernarus")),
        map_dropdown=Dropdown(0),
        _filter_state={"selected_map": "Chernarus", "marker": "dirty"},
        _filter_refresh_suppress_depth=0,
    )
    bind(host, "_set_map_choices", "_push_filter_refresh_suppression", "_pop_filter_refresh_suppression")

    def raise_on_selection(_selected):
        raise RuntimeError("widget failed")

    host.map_dropdown.set_selected = raise_on_selection
    state = {
        "previous_map_state": {
            "choices": ("All Maps", "Livonia"),
            "selected": 1,
            "filter_state": {"selected_map": "Livonia", "marker": "original"},
        },
    }

    DZLLWindow._restore_startup_map_state(host, state)

    assert host._filter_refresh_suppress_depth == 0
    assert host._filter_state == {"selected_map": "Livonia", "marker": "original"}


def test_idle_submission_failure_restores_map_choices_and_filter_state(monkeypatch):
    rows = [row(f"Server {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window.GLib, "idle_add", lambda *_args: 0)
    events = []
    host = make_host(rows, events)
    install_map_state(host)
    before = map_state(host)
    outcomes = []

    assert DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
        batched_complete=outcomes.append,
    ) is False

    assert outcomes == ["idle_submission_failed"]
    assert map_state(host) == before
    assert host.store.rows == [] and "a2s" not in events


def test_successful_batch_commits_new_map_choices(monkeypatch):
    rows = [row(f"Server {index}", f"10.0.0.{index}", 40) for index in range(1, 7)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    install_map_state(host)

    assert DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
    ) is window._STARTUP_DB_APPLY_PENDING
    drain(queued[-1])

    assert host.map_model.rows == ["All Maps", "Chernarus"]
    assert host.map_dropdown.get_selected() == 0
    assert host._filter_state["selected_map"] == "All Maps"


def test_server_objects_are_constructed_only_from_the_gtk_idle_callback(monkeypatch):
    rows = [row("One", "10.0.0.1", 40)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    constructed_on = []
    original = window.ServerObject

    def recording_server_object(*args, **kwargs):
        constructed_on.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(window, "ServerObject", recording_server_object)
    caller_thread = threading.get_ident()
    DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    assert constructed_on == []
    drain(queued[-1])
    assert constructed_on == [caller_thread]


def test_batch_instrumentation_reports_slices_publication_identity_and_cache_counts(monkeypatch):
    rows = [row("One", "10.0.0.1", 40), row("Two", "10.0.0.2", 20)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "STARTUP_ROW_BATCH_BUDGET_NS", 0)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    phases = []
    monkeypatch.setattr(
        window, "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    events = []
    host = make_host(rows, events)
    mods_json = rows[0]["mods"]
    digest = window.server_mod_cache.fingerprint(mods_json)
    host._server_mod_cache_records = {
        "10.0.0.1:2302": (
            digest, 0, "", window.build_server_mod_index(mods_json),
        )
    }
    DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    drain(queued[-1])
    by_name = {name: fields for name, fields in phases}
    assert by_name["startup_row_load_path"]["path"] == "batched"
    build = by_name["startup_row_batch_build"]
    assert build["load_id"] == 1 and build["rows"] == 2
    assert build["batch_count"] == 2
    assert build["cache_hits"] == 1 and build["cache_misses"] == 1
    publication = by_name["startup_row_batch_publication"]
    assert publication["load_id"] == 1 and publication["visible_rows"] == 2
    assert publication["largest_publication_component"] in {
        "source_store_publication", "sort_key_snapshot",
        "initial_visible_store_rebuild",
    }
    summary = by_name["startup_row_batch_summary"]
    assert summary["load_id"] == 1
    assert summary["largest_sync_phase"] in {
        "initial_batch_setup", "max_idle_slice", "final_publication",
        "post_load_startup_coordination",
    }


def test_suggestion_catalogue_is_deferred_while_visible_commit_and_a2s_proceed(monkeypatch):
    rows = [row("One", "10.0.0.1", 40)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    events = []
    host = make_host(rows, events)
    host._mod_suggestion_index = {"mods": ()}
    host._suggestion_cache_ready = False

    DZLLWindow._apply_db_rows(host, rows, True, allow_startup_batching=True)
    assert "suggestions" not in events and "a2s" not in events
    drain(queued[-1])
    assert events.index(("visible", "load")) < events.index("a2s")
    assert "suggestions" not in events
    assert host._deferred_suggestion_restore_pending is True
    assert host._deferred_suggestion_restore_allowed is False


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"ip": "invalid address", "gport": 2302, "qport": 2303}],
    ],
    ids=("empty", "all-invalid"),
)
def test_zero_valid_rows_use_synchronous_empty_state_and_preserve_warm_cache(
        monkeypatch, rows):
    queued, _removed = install_idle_queue(monkeypatch)
    timers = []
    phases = []
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(
        window.GLib, "timeout_add_seconds",
        lambda seconds, callback, *args: timers.append((seconds, callback, args)) or 71,
    )
    monkeypatch.setattr(
        window, "log_startup_phase",
        lambda _origin, phase, _start, **fields: phases.append((phase, fields)),
    )
    warm_record = (
        window.server_mod_cache.fingerprint("[]"), 0, "",
        window.build_server_mod_index("[]"),
    )
    warm_records = {"10.0.0.9:2302": warm_record}
    events = []
    host = make_host(rows, events)
    host._server_mod_cache_records = dict(warm_records)
    host._server_mod_cache_persisted = {
        key: record[0] for key, record in warm_records.items()
    }

    assert DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
    ) is False

    assert queued == []
    assert ("empty_text", "No Servers Found.") in events
    assert ("empty_visible", True) in events
    assert host._server_companion_rows_loaded is False
    assert "companion" not in events and "a2s" not in events
    assert not any(seconds == 25 for seconds, _callback, _args in timers)
    assert host._server_mod_cache_records == warm_records
    assert host._server_mod_cache_persisted == {
        key: record[0] for key, record in warm_records.items()
    }
    load_path = next(fields for phase, fields in phases if phase == "startup_row_load_path")
    assert load_path["path"] == "synchronous"
    assert load_path["reason"] == "no_valid_rows"


def test_accidental_empty_batch_publication_recovers_without_cache_or_suggestion_write(
        monkeypatch):
    queued, _removed = install_idle_queue(monkeypatch)
    timers = []
    monkeypatch.setattr(
        window.GLib, "timeout_add_seconds",
        lambda seconds, callback, *args: timers.append((seconds, callback, args)) or 71,
    )
    events = []
    host = make_host([], events)
    warm_record = (
        window.server_mod_cache.fingerprint("[]"), 0, "",
        window.build_server_mod_index("[]"),
    )
    warm_records = {"10.0.0.9:2302": warm_record}
    host._server_mod_cache_records = dict(warm_records)
    host._server_mod_cache_persisted = {
        key: record[0] for key, record in warm_records.items()
    }
    host._suggestion_cache_ready = False
    outcomes = []

    assert DZLLWindow._start_startup_row_batches(
        host, [], outcomes.append,
    ) is window._STARTUP_DB_APPLY_PENDING
    drain(queued[-1])

    assert outcomes == ["empty"]
    assert host._startup_row_batch_state is None
    assert host._server_db_update_inflight is False
    assert host._server_companion_rows_loaded is False
    assert host._server_mod_cache_records == warm_records
    assert host._server_mod_cache_candidate == {}
    assert host._deferred_suggestion_restore_pending is False
    assert "a2s" not in events and "companion" not in events
    assert not any(seconds == 25 for seconds, _callback, _args in timers)
    assert ("empty_text", "No Servers Found.") in events
    assert ("empty_visible", True) in events

    host._update_executor = SimpleNamespace(
        submit=lambda *_args, **_kwargs: pytest.fail("empty cache must not be staged")
    )
    assert DZLLWindow._schedule_server_mod_cache_write(
        host, host._server_mod_cache_generation,
    ) is False
    assert host._server_mod_cache_records == warm_records
    assert host._server_mod_cache_persisted == {
        key: record[0] for key, record in warm_records.items()
    }


class _RecordingExecutor:
    def __init__(self):
        self.calls = []
        self.futures = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.calls.append((fn, args, kwargs))
        self.futures.append(future)
        return future


@pytest.mark.parametrize("failure_point", ("persistent_titlebar", "companion", "a2s"))
def test_post_publication_exceptions_release_ownership_and_restore_autocomplete(
        tmp_path, monkeypatch, failure_point):
    rows = [row("One", "10.0.0.1", 40, mods='[{"steamWorkshopId":"42","name":"Code Lock"}]')]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    events = []
    host = make_host(rows, events)
    host._suggestion_cache_ready = False
    host._suggestion_cache_expired = False
    host._suggestion_cache_created_at = 0.0
    host._suggestion_cache_migration_pending = False
    host._suggestion_cache_migration_created_at = 0.0
    host._suggestion_cache_migration_inflight = False
    host._suggestion_refresh_inflight = False
    host._deferred_suggestion_restore_pending = False
    host._deferred_suggestion_restore_allowed = False
    host._deferred_suggestion_restore_inflight = False
    host._deferred_suggestion_restore_generation = 0
    host._deferred_suggestion_restore_retry_after = 0.0
    host._suggestion_cache_path = tmp_path / "mod_suggestions_v1.json"
    host._mod_suggestion_index = {"mods": (), "count": 0}
    host._mod_suggestion_last_rows = ()
    host._queue_mod_suggestions_refresh = lambda: events.append("suggestions_ready")
    host._startup_presentation = SimpleNamespace(
        startup_completed=False,
        complete_startup=lambda: events.append("recovery_overlay_complete"),
    )
    update_executor = _RecordingExecutor()
    host._update_executor = update_executor
    bind(host, "_maybe_schedule_deferred_suggestion_restore")

    def fail(message):
        raise RuntimeError(message)

    if failure_point == "persistent_titlebar":
        host._apply_titlebar_counts = lambda: fail("titlebar")
    elif failure_point == "companion":
        host._restore_server_companion_if_enabled = lambda: fail("companion")
    else:
        host._submit_startup_rest_batches = lambda *_args, **_kwargs: fail("a2s")

    outcomes = []

    def complete(outcome):
        outcomes.append(outcome)
        host._server_db_update_inflight = False

    assert DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True, batched_complete=complete,
    ) is window._STARTUP_DB_APPLY_PENDING
    drain(queued[0])

    assert outcomes == ["published_post_load_failed"]
    assert host._server_db_update_inflight is False
    assert host._startup_row_batch_state is None
    assert [obj.name for obj in host.store.rows] == ["One"]
    assert [obj.name for obj in host.column_view_store.rows] == ["One"]
    assert host._suggestion_sweep_fallback_ready is True
    assert host._deferred_suggestion_restore_pending is True
    assert host._deferred_suggestion_restore_allowed is True
    assert host._deferred_suggestion_restore_inflight is True
    assert len(update_executor.calls) == 1

    fn, args, kwargs = update_executor.calls[0]
    update_executor.futures[0].set_result(fn(*args, **kwargs))
    callback, callback_args = queued[-1]
    assert callback(*callback_args) is False
    assert host._suggestion_cache_ready is True
    assert host._mod_suggestion_index["count"] == 1
    assert "suggestions_ready" in events

    db_executor = _RecordingExecutor()
    host._db_executor = db_executor
    host._rebuild_column_view_store = lambda **_kwargs: None
    host._update_sort_indicators = lambda: None
    host._queue_scroll_to_top = lambda: None
    host._set_updating = lambda *_args: None
    DZLLWindow._manual_update_server_database(host)
    assert host._server_db_update_inflight is True
    assert len(db_executor.calls) == 1


def test_transient_titlebar_failure_reports_dedicated_contained_outcome(monkeypatch):
    rows = [row("One", "10.0.0.1", 40)]
    queued, _removed = install_idle_queue(monkeypatch)
    monkeypatch.setattr(window, "STARTUP_BATCHING_ENABLED", True)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", False)
    events = []
    host = make_host(rows, events)
    titlebar_calls = 0

    def transient_titlebar_failure():
        nonlocal titlebar_calls
        titlebar_calls += 1
        if titlebar_calls == 1:
            raise RuntimeError("transient titlebar failure")
        events.append("title")

    host._apply_titlebar_counts = transient_titlebar_failure
    outcomes = []

    assert DZLLWindow._apply_db_rows(
        host, rows, True, allow_startup_batching=True,
        batched_complete=outcomes.append,
    ) is window._STARTUP_DB_APPLY_PENDING
    drain(queued[-1])

    assert outcomes == ["published_titlebar_failed"]
    assert titlebar_calls == 2
    assert "companion" in events and "a2s" in events
    assert host._suggestion_sweep_fallback_ready is False
    assert "overlay_complete" not in events

