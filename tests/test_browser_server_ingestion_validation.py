from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from dzll_launcher import db, window
from dzll_launcher import mod_parsed, mod_search, mod_suggestion_cache, mod_suggestions, server_mod_cache
from dzll_launcher.ui_row import ServerObject


class _Store:
    def __init__(self):
        self.items = []

    def get_n_items(self):
        return len(self.items)

    def splice(self, position, count, replacements):
        self.items[position : position + count] = replacements

    def append(self, item):
        self.items.append(item)


class _BrowserHost:
    def __init__(self):
        self.store = _Store()
        self._obj_by_key = {}
        self._browser_live_offline_streaks = {}
        self.favorites = {}
        self.last_played = {}
        self.live = {}
        self.sorter = None
        self._discord = None
        self.map_choices = []

    def _rebuild_mod_suggestion_index(self):
        pass

    def _snapshot_all_sort_keys(self):
        pass

    def _on_filter_changed(self, **_kwargs):
        pass

    def _apply_titlebar_counts(self):
        pass

    def _auto_sort_lowest_ping_after_startup(self):
        return False

    def _debug_sort_attach_notify_probe(self, _obj):
        pass

    def _is_likely_test_server_name(self, _name):
        return False

    def _set_map_choices(self, choices):
        self.map_choices = list(choices)

    def _load_rows_into_store(self, rows):
        return window.DZLLWindow._load_rows_into_store(self, rows)

    def _restore_server_companion_if_enabled(self):
        pass

    def _set_updating(self, *_args):
        pass

    def _complete_startup_presentation(self):
        pass


def _row(ip, gport=2302, qport=2303, *, name="Synthetic"):
    return {
        "ip": ip,
        "gport": gport,
        "qport": qport,
        "name": name,
        "map": "chernarusplus",
        "players": 0,
        "maxPlayers": 60,
        "password": 0,
        "mods": "[]",
        "modCount": 0,
        "third_person": 1,
        "timeWarp": 1.0,
        "time": "12:00",
        "country": "GB",
        "ping": 10,
        "bm_rank": 1,
    }


@pytest.fixture(autouse=True)
def _disable_glib_timers(monkeypatch, tmp_path):
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_args: 1)
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: tmp_path / "mod_suggestions_v1.json")


def _load(host, rows):
    return window.DZLLWindow._load_rows_into_store(host, rows)


class _IndexedBrowserHost(_BrowserHost):
    def _rebuild_mod_suggestion_index(self):
        return window.DZLLWindow._rebuild_mod_suggestion_index(self)

    def _on_mod_suggestion_refresh_done(self, generation, start_ns, future):
        return window.DZLLWindow._on_mod_suggestion_refresh_done(self, generation, start_ns, future)


@pytest.mark.parametrize("payload", [
    "", "not json", "{}", "null", "[]", "[null, 5, [], {}]",
    json.dumps([
        {"steamWorkshopId": 0, "name": "@Zero"},
        {"steamWorkshopId": " 42 ", "name": "@Repeated"},
        {"steamWorkshopId": "42", "name": "@Repeated"},
        {"steamWorkshopId": False, "name": "Name Only"},
        {"steamWorkshopId": 99, "name": ""},
        {"name": "Unicode café 🚀"},
        *({"steamWorkshopId": str(i), "name": f"Mod {i}"} for i in range(10)),
    ]),
    json.dumps([{"steamWorkshopId": [1, 2], "name": "nested"},
                {"name": "\u202eControl\u200b"}]),
])
def test_load_local_mod_index_paths_match_original_public_builders(payload):
    parsed = mod_parsed.ParsedServerMods.from_json(payload)
    assert window.parse_mods_preview(payload, parsed_mods=parsed) == window.parse_mods_preview(payload)
    assert window.parse_mods_preview(payload, max_names=0, parsed_mods=parsed) == window.parse_mods_preview(payload, max_names=0)
    assert mod_search.build_server_mod_index(payload, parsed_mods=parsed) == mod_search.build_server_mod_index(payload)
    old_index = mod_suggestions.build_mod_suggestion_index((payload, payload))
    new_index = mod_suggestions.build_mod_suggestion_index((), parsed_mods_values=(parsed, parsed))
    assert new_index == old_index
    for token in ("42", "repeated", "café", "mod", "zero", "nested"):
        assert mod_suggestions.suggest_mods(new_index, token) == mod_suggestions.suggest_mods(old_index, token)


def test_full_load_decodes_once_per_accepted_row_and_indexes_final_endpoint(monkeypatch):
    host = _IndexedBrowserHost()
    first = _row("1.2.3.4")
    first["mods"] = '[{"steamWorkshopId":"100","name":"Old"}]'
    last = _row("1.2.3.4")
    last["mods"] = '[{"steamWorkshopId":"200","name":"New"}]'
    other = _row("2.3.4.5")
    other["mods"] = '[{"steamWorkshopId":"200","name":"New"}]'
    other["modCount"] = 17
    invalid = _row("invalid address")
    invalid["mods"] = '[{"steamWorkshopId":"300","name":"Ignored"}]'

    loads = []
    original_loads = mod_parsed.json.loads
    def count_loads(value):
        loads.append(value)
        return original_loads(value)
    monkeypatch.setattr(mod_parsed.json, "loads", count_loads)

    assert _load(host, [first, last, other, invalid]) is True
    assert loads == [first["mods"], last["mods"], other["mods"]]
    assert len(host.store.items) == 3
    assert len(host._obj_by_key) == 2
    assert host.store.items[-1].mod_count == 17
    assert host.store.items[-1].mods_json == other["mods"]
    expected = mod_suggestions.build_mod_suggestion_index((last["mods"], other["mods"]))
    assert host._mod_suggestion_index == expected
    assert host._mod_suggestion_index["mods"][0]["server_count"] == 2
    assert host._load_parsed_mods_by_key is None


def test_load_local_name_cleaning_is_shared_without_merging_distinct_id_rules(monkeypatch):
    payload = json.dumps([
        {"steamWorkshopId": "42", "name": "@Shared"},
        {"steamWorkshopId": 0, "name": "@Zero"},
    ])
    calls = []
    original_clean = mod_parsed.clean_display_mod_name
    def count_clean(name, workshop_id, *, fallback):
        calls.append((name, workshop_id))
        return original_clean(name, workshop_id, fallback=fallback)
    monkeypatch.setattr(mod_parsed, "clean_display_mod_name", count_clean)

    parsed = mod_parsed.ParsedServerMods.from_json(payload)
    window.parse_mods_preview(payload, parsed_mods=parsed)
    mod_search.build_server_mod_index(payload, parsed_mods=parsed)
    mod_suggestions.build_mod_suggestion_index((), parsed_mods_values=(parsed,))

    assert calls == [("@Shared", "42"), ("@Zero", None), ("@Zero", "0")]


def test_empty_load_rebuilds_empty_suggestion_index_without_parsed_sidecar():
    host = _IndexedBrowserHost()
    assert _load(host, []) is False
    assert host._mod_suggestion_index == mod_suggestions.build_mod_suggestion_index(())
    assert host._mod_suggestion_last_rows == ()


def test_replacement_load_reuses_monthly_catalogue_and_later_fallback_does_not_rebuild():
    host = _IndexedBrowserHost()
    first = _row("1.2.3.4")
    first["mods"] = '[{"steamWorkshopId":"100","name":"Old"}]'
    replacement = _row("1.2.3.4")
    replacement["mods"] = '[{"steamWorkshopId":"200","name":"New"}]'

    assert _load(host, [first]) is True
    expected = mod_suggestions.build_mod_suggestion_index((first["mods"],))
    assert _load(host, [replacement]) is True
    assert host._mod_suggestion_index == expected
    assert host.store.items[-1].mod_search_index["ids"] == frozenset({"200"})
    host._rebuild_mod_suggestion_index()
    assert host._mod_suggestion_index == expected


def test_warm_cache_and_empty_cache_skip_global_rebuild_on_db_reload(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    cached = mod_suggestions.build_mod_suggestion_index(('[{"name":"Cached"}]',))
    mod_suggestion_cache.save_suggestion_cache(path, cached)
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: path)
    monkeypatch.setattr(window, "build_mod_suggestion_index", lambda *_args, **_kwargs: pytest.fail("global rebuild"))

    host = _IndexedBrowserHost()
    row = _row("1.2.3.4")
    row["mods"] = '[{"name":"Current"}]'
    assert _load(host, [row]) is True
    assert _load(host, [row]) is True
    assert host._mod_suggestion_index == cached
    assert host.store.items[0].mods_preview == "Current"

    empty_path = tmp_path / "empty.json"
    empty = mod_suggestions.build_mod_suggestion_index(())
    mod_suggestion_cache.save_suggestion_cache(empty_path, empty)
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: empty_path)
    second = _IndexedBrowserHost()
    assert _load(second, [row]) is True
    assert second._mod_suggestion_index == empty
    second._rebuild_mod_suggestion_index()
    assert second._mod_suggestion_index == empty


def test_expired_disk_cache_is_usable_and_corrupt_cache_rebuilds(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    cached = mod_suggestions.build_mod_suggestion_index(('[{"name":"Cached"}]',))
    mod_suggestion_cache.save_suggestion_cache(
        path, cached, now=time.time() - mod_suggestion_cache.REFRESH_SECONDS - 1,
    )
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: path)
    row = _row("1.2.3.4")
    row["mods"] = '[{"name":"Current"}]'
    host = _IndexedBrowserHost()
    assert _load(host, [row]) is True
    assert host._mod_suggestion_index == cached
    assert host._suggestion_cache_expired is True
    assert mod_suggestion_cache.load_suggestion_cache(path).expired is True

    path.write_text("corrupt")
    replacement = _IndexedBrowserHost()
    assert _load(replacement, [row]) is True
    expected = mod_suggestions.build_mod_suggestion_index((row["mods"],))
    assert replacement._mod_suggestion_index == expected
    assert mod_suggestion_cache.load_suggestion_cache(path).index == expected


class _DeferredSuggestionExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = Future()
        self.jobs.append((future, function, args))
        return future

    def finish(self, *, error=None):
        future, function, args = self.jobs.pop(0)
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(function(*args))


def test_expired_catalogue_waits_for_full_sweep_then_swaps_after_worker(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: path)
    idle = []
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback, *args: idle.append((callback, args)) or 1)
    host = _IndexedBrowserHost()
    host._update_executor = _DeferredSuggestionExecutor()
    host._startup_perf_origin_ns = time.monotonic_ns()
    old = _row("1.2.3.4")
    old["mods"] = '[{"name":"Old"}]'
    new = _row("1.2.3.4")
    new["mods"] = '[{"name":"New"}]'
    assert _load(host, [old]) is True
    old_index = host._mod_suggestion_index
    assert _load(host, [new]) is True
    host._suggestion_cache_created_at = time.time() - mod_suggestion_cache.REFRESH_SECONDS - 1
    host._suggestion_sweep_complete = False
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert not host._update_executor.jobs
    host._suggestion_sweep_complete = True
    host._server_db_update_inflight = True
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert not host._update_executor.jobs
    host._server_db_update_inflight = False
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert len(host._update_executor.jobs) == 1
    assert host._mod_suggestion_index is old_index
    host._update_executor.finish()
    assert host._mod_suggestion_index is old_index
    callback, args = idle.pop()
    assert callback(*args) is False
    expected = mod_suggestions.build_mod_suggestion_index((new["mods"],))
    assert host._mod_suggestion_index == expected
    assert mod_suggestion_cache.load_suggestion_cache(path).index == expected
    assert mod_suggestions.suggest_mods(host._mod_suggestion_index, "new") == mod_suggestions.suggest_mods(expected, "new")


def test_refresh_failure_generation_change_and_shutdown_keep_old_cache(tmp_path, monkeypatch):
    path = tmp_path / "mod_suggestions_v1.json"
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: path)
    idle = []
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback, *args: idle.append((callback, args)) or 1)
    host = _IndexedBrowserHost()
    host._update_executor = _DeferredSuggestionExecutor()
    host._startup_perf_origin_ns = time.monotonic_ns()
    row = _row("1.2.3.4")
    row["mods"] = '[{"name":"Old"}]'
    assert _load(host, [row]) is True
    old_index = host._mod_suggestion_index
    host._suggestion_cache_created_at = time.time() - mod_suggestion_cache.REFRESH_SECONDS - 1
    host._suggestion_sweep_complete = True
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    host._update_executor.finish(error=RuntimeError("worker failed"))
    callback, args = idle.pop()
    assert callback(*args) is False
    assert host._mod_suggestion_index is old_index
    assert host._suggestion_refresh_retry_after > time.monotonic()

    host._suggestion_refresh_retry_after = 0
    host._suggestion_sweep_complete = False
    host._suggestion_sweep_fallback_ready = True
    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert _load(host, [row]) is True  # A new DB generation invalidates the pending result.
    host._update_executor.finish()
    callback, args = idle.pop()
    assert callback(*args) is False
    assert host._mod_suggestion_index is old_index
    assert len(list(tmp_path.glob("*.pending"))) == 0

    window.DZLLWindow._maybe_schedule_mod_suggestion_refresh(host)
    assert len(host._update_executor.jobs) == 1
    host._shutdown_cleanup_done = True
    host._suggestion_cache_generation += 1
    host._update_executor.finish()
    callback, args = idle.pop()
    assert callback(*args) is False
    assert host._mod_suggestion_index is old_index


def _write_server_database(path, rows, *, include_official=False, include_night=False):
    connection = sqlite3.connect(path)
    columns = (
        "ip", "gport", "qport", "name", "map", "players", "maxPlayers",
        "password", "mods", "modCount", "third_person", "timeWarp", "time",
        "country", "ping", "bm_rank",
    )
    connection.execute(
        "CREATE TABLE servers (" + ", ".join(columns) +
        (", official" if include_official else "") +
        (", nightTimeWarp" if include_night else "") + ")"
    )
    placeholders = ",".join("?" for _ in range(len(columns) + int(include_official) + int(include_night)))
    connection.executemany(
        f"INSERT INTO servers VALUES ({placeholders})",
        [
            tuple(row.get(column) for column in columns) +
            ((row.get("official"),) if include_official else ()) +
            ((row.get("nightTimeWarp"),) if include_night else ())
            for row in rows
        ],
    )
    connection.commit()
    connection.close()


@pytest.mark.parametrize("include_official", [False, True])
def test_optional_official_column_reads_and_old_database_remains_usable(
    tmp_path, monkeypatch, include_official,
):
    local_path = tmp_path / "local.db"
    published_path = tmp_path / "published.db"
    rows = [_row("1.1.1.1"), _row("2.2.2.2"), _row("3.3.3.3")]
    for row, value in zip(rows, (1, 0, None)):
        row["official"] = value
    _write_server_database(published_path, rows, include_official=include_official)
    monkeypatch.setattr(db, "_fetch_db_bytes_with_retries", published_path.read_bytes)
    monkeypatch.setattr(db, "DB_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(local_path))

    assert db.fetch_db_overwrite_local() is True
    read = db.read_servers_from_db()
    by_ip = {row["ip"]: row["official"] for row in read}
    assert by_ip == (
        {"1.1.1.1": 1, "2.2.2.2": 0, "3.3.3.3": None}
        if include_official else
        {"1.1.1.1": None, "2.2.2.2": None, "3.3.3.3": None}
    )
    host = _BrowserHost()
    assert _load(host, read) is True
    assert {obj.ip: obj.official for obj in host.store.items} == (
        {"1.1.1.1": True, "2.2.2.2": False, "3.3.3.3": False}
        if include_official else
        {"1.1.1.1": False, "2.2.2.2": False, "3.3.3.3": False}
    )


@pytest.mark.parametrize("include_night", [False, True])
def test_optional_night_speed_projection_and_nullable_model(tmp_path, monkeypatch, include_night):
    local_path = tmp_path / "local.db"
    published_path = tmp_path / "published.db"
    rows = [_row("1.1.1.1"), _row("2.2.2.2")]
    rows[0].update(timeWarp=4.5, nightTimeWarp=11.6)
    rows[1].update(timeWarp=None, nightTimeWarp="bad")
    _write_server_database(published_path, rows, include_night=include_night)
    monkeypatch.setattr(db, "_fetch_db_bytes_with_retries", published_path.read_bytes)
    monkeypatch.setattr(db, "DB_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(local_path))
    assert db.fetch_db_overwrite_local() is True
    read = db.read_servers_from_db()
    assert {r["ip"]: r["nightTimeWarp"] for r in read} == {
        "1.1.1.1": 11.6 if include_night else None,
        "2.2.2.2": "bad" if include_night else None,
    }
    host = _BrowserHost()
    assert _load(host, read) is True
    by_ip = {obj.ip: obj for obj in host.store.items}
    assert by_ip["1.1.1.1"].timewarp == 4.5
    assert by_ip["1.1.1.1"].night_timewarp == (11.6 if include_night else None)
    assert by_ip["2.2.2.2"].timewarp is None
    assert by_ip["2.2.2.2"].night_timewarp is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, True), (0, False), (None, False), ("1", False), (2, False), (True, False), ("official", False)],
)
def test_official_mapping_accepts_only_integer_one(value, expected):
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row["official"] = value
    assert _load(host, [row]) is True
    assert host.store.items[0].official is expected


def test_read_servers_uses_safe_read_only_uri_for_unusual_path(tmp_path, monkeypatch):
    path = tmp_path / "servers space ? # café.sqlite"
    _write_server_database(path, [_row("127.0.0.1", name="First"),
                                  _row("127.0.0.2", name="Second")])
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))

    calls = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", connect)
    assert [row["name"] for row in db.read_servers_from_db()] == [
        "First", "Second",
    ]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs == {"uri": True}
    assert args == (f"{Path(path).resolve().as_uri()}?mode=ro",)

    read_only = real_connect(args[0], uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute("CREATE TABLE must_fail (value INTEGER)")
    finally:
        read_only.close()


def test_read_servers_closes_connection_after_post_connect_failure(
        tmp_path, monkeypatch):
    path = tmp_path / "servers.sqlite"
    _write_server_database(path, [_row("127.0.0.1")])
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))

    real_connect = sqlite3.connect
    tracked = []

    class TrackingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, *args, **kwargs):
            self._cursor.execute(*args, **kwargs)
            return self

        def fetchall(self):
            raise RuntimeError("injected fetch failure")

    class TrackingConnection:
        def __init__(self, connection):
            self._connection = connection
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def cursor(self):
            return TrackingCursor(self._connection.cursor())

        def close(self):
            self.close_calls += 1
            return self._connection.close()

    def connect(*args, **kwargs):
        connection = TrackingConnection(real_connect(*args, **kwargs))
        tracked.append(connection)
        return connection

    monkeypatch.setattr(db.sqlite3, "connect", connect)
    assert db.read_servers_from_db() == []
    assert len(tracked) == 1
    assert tracked[0].close_calls == 1


def test_read_servers_missing_database_does_not_connect(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))
    connect_called = []
    monkeypatch.setattr(
        db.sqlite3, "connect", lambda *args, **kwargs: connect_called.append(True),
    )

    assert db.read_servers_from_db() == []
    assert connect_called == []


def test_read_servers_corrupt_database_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))

    real_connect = sqlite3.connect
    tracked = []

    class TrackingConnection:
        def __init__(self, connection):
            self._connection = connection
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self):
            self.close_calls += 1
            return self._connection.close()

    def connect(*args, **kwargs):
        connection = TrackingConnection(real_connect(*args, **kwargs))
        tracked.append(connection)
        return connection

    monkeypatch.setattr(db.sqlite3, "connect", connect)
    assert db.read_servers_from_db() == []
    assert len(tracked) == 1
    assert tracked[0].close_calls == 1


def test_malformed_row_is_dropped_without_aborting_surrounding_valid_rows():
    host = _BrowserHost()

    assert _load(
        host,
        [
            _row("1.2.3.4", name="A"),
            _row("999.999.999.999", name="B"),
            _row("192.168.0.1", name="C"),
        ],
    ) is True

    assert [(obj.name, obj.ip) for obj in host.store.items] == [
        ("A", "1.2.3.4"),
        ("C", "192.168.0.1"),
    ]


def test_invalid_endpoint_row_cannot_contribute_browser_map_filter_state():
    host = _BrowserHost()
    valid = _row("1.2.3.4", name="Valid")
    valid["map"] = "chernarusplus"
    invalid = _row("example.com", name="Invalid")
    invalid["map"] = "enoch"

    assert window.DZLLWindow._apply_db_rows(host, [valid, invalid], False) is False

    assert [obj.name for obj in host.store.items] == ["Valid"]
    assert "Livonia" not in host.map_choices


def test_non_string_ip_does_not_abort_browser_population():
    host = _BrowserHost()

    assert _load(
        host,
        [_row("1.2.3.4", name="A"), _row(1234, name="Bad"), _row("2.3.4.5", name="C")],
    ) is True

    assert [obj.name for obj in host.store.items] == ["A", "C"]


@pytest.mark.parametrize(
    ("gport", "qport"),
    [
        (0, 2303),
        (-1, 2303),
        (65536, 2303),
        (2302, 0),
        (2302, -1),
        (2302, 65536),
        ("2302", 2303),
        (2302.0, 2303),
        (True, 2303),
        (2302, "2303"),
        (2302, 2303.0),
        (2302, False),
    ],
)
def test_invalid_port_rows_are_dropped(gport, qport):
    host = _BrowserHost()

    assert _load(host, [_row("1.2.3.4", gport, qport)]) is False
    assert host.store.items == []


def test_absent_query_port_fallback_is_validated():
    host = _BrowserHost()
    assert _load(host, [_row("1.2.3.4", 2302, None)]) is True
    assert host.store.items[0].qport == 2303

    host = _BrowserHost()
    assert _load(host, [_row("1.2.3.4", 65535, None)]) is False
    assert host.store.items == []


def test_isolated_sqlite_rows_are_validated_before_browser_objects(tmp_path, monkeypatch):
    path = tmp_path / "dzll-servers.db"
    rows = [
        _row("1.2.3.4", name="Valid"),
        _row("256.1.1.1", name="Invalid IP"),
        _row("2.3.4.5", 70000, 2303, name="Invalid Port"),
        _row(1234, name="Invalid Type"),
    ]
    _write_server_database(path, rows)
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))

    host = _BrowserHost()
    assert _load(host, db.read_servers_from_db()) is True

    assert [(obj.name, obj.ip) for obj in host.store.items] == [
        ("Valid", "1.2.3.4")
    ]


@pytest.mark.parametrize("name", [None, 123, 1.5, b"bytes"])
def test_wrong_type_or_missing_server_name_uses_empty_display_name(name):
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row["name"] = name

    assert _load(host, [row]) is True
    assert host.store.items[0].name == ""


def test_absent_server_name_uses_empty_display_name():
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row.pop("name")

    assert _load(host, [row]) is True
    assert host.store.items[0].name == ""


def test_valid_special_character_server_name_is_preserved():
    host = _BrowserHost()
    name = "Café Сервер 🚀 | [PVP] ★ \u202eBrand\u200b"

    assert _load(host, [_row("1.2.3.4", name=name)]) is True
    assert host.store.items[0].name == name


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("password", 0, False),
        ("password", 1, True),
        ("third_person", 0, False),
        ("third_person", 1, True),
        ("password", None, False),
        ("password", "false", False),
        ("password", "true", False),
        ("password", "maybe", False),
        ("password", "1", False),
        ("password", 2, False),
        ("third_person", "false", False),
        ("third_person", "true", False),
        ("third_person", -1, False),
    ],
)
def test_db_flags_accept_only_integer_zero_or_one(field, value, expected):
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row[field] = value

    assert _load(host, [row]) is True
    assert bool(getattr(host.store.items[0], field)) is expected


def test_malformed_advisory_fields_default_without_changing_endpoint_or_mods():
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row.update(
        {
            "map": 123,
            "players": "not-a-number",
            "maxPlayers": b"not-a-number",
            "password": "false",
            "mods": '[{"steamWorkshopId": 42, "name": "Required"}]',
            "modCount": "not-a-number",
            "third_person": "true",
            "timeWarp": "not-a-number",
            "time": 123,
            "country": 456,
            "ping": "not-a-number",
            "bm_rank": "not-a-number",
        }
    )

    assert _load(host, [row]) is True
    obj = host.store.items[0]
    assert (obj.ip, obj.gport, obj.qport) == ("1.2.3.4", 2302, 2303)
    assert obj.mods_json == row["mods"]
    assert obj.mod_search_index["ids"] == frozenset({"42"})
    assert (obj.map_name, obj.players, obj.max_players) == ("", 0, 0)
    assert (obj.password, obj.third_person) == (False, False)
    assert (obj.timewarp, obj.time, obj.country) == (None, "--:--", "")
    assert (obj.ping, obj.bm_rank) == (-1, 999999999)
    assert obj.mod_count == 1


@pytest.mark.parametrize("mods_value", [None, 123, 1.5, b"[]"])
def test_wrong_type_mod_payload_cannot_create_action_authority(mods_value):
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row["mods"] = mods_value
    row["modCount"] = None

    assert _load(host, [row]) is True
    obj = host.store.items[0]
    assert obj.mods_json == ""
    assert obj.mod_count == 0
    assert obj.mod_search_index["ids"] == frozenset()


def test_valid_unusual_numeric_values_keep_existing_semantics():
    host = _BrowserHost()
    row = _row("1.2.3.4")
    row.update(
        {
            "players": -10,
            "maxPlayers": -20,
            "modCount": -30,
            "timeWarp": -999999.0,
            "ping": -40,
            "bm_rank": -50,
        }
    )

    assert _load(host, [row]) is True
    obj = host.store.items[0]
    assert (obj.players, obj.max_players, obj.mod_count) == (-10, -20, -30)
    assert (obj.timewarp, obj.ping, obj.bm_rank) == (-999999.0, -40, -50)


def _mixed_type_database_rows():
    valid_before = _row("1.1.1.1", name="Before")
    malformed_name = _row("2.2.2.2", name=123)
    malformed_boolean = _row("3.3.3.3", name="Bad Boolean")
    malformed_boolean["password"] = "false"
    malformed_numeric = _row("4.4.4.4", name="Bad Numeric")
    malformed_numeric["players"] = "not-a-number"
    malformed_map = _row("5.5.5.5", name="Bad Map")
    malformed_map["map"] = 123
    valid_after = _row("6.6.6.6", name="After")
    invalid_endpoint = _row("example.com", name="Invalid Endpoint")
    return [
        valid_before,
        malformed_name,
        malformed_boolean,
        malformed_numeric,
        malformed_map,
        valid_after,
        invalid_endpoint,
    ]


@pytest.mark.parametrize("database_source", ["fetched", "cached"])
def test_malformed_sqlite_fields_are_contained_for_fetched_and_cached_databases(
    database_source, tmp_path, monkeypatch,
):
    local_path = tmp_path / "cache" / "dzll-servers.db"
    local_path.parent.mkdir()
    rows = _mixed_type_database_rows()

    if database_source == "fetched":
        published_path = tmp_path / "published.db"
        _write_server_database(published_path, rows)
        monkeypatch.setattr(
            db, "_fetch_db_bytes_with_retries", published_path.read_bytes,
        )
        monkeypatch.setattr(db, "DB_LOCAL_DIR", str(local_path.parent))
        monkeypatch.setattr(db, "DB_LOCAL_PATH", str(local_path))
        assert db.fetch_db_overwrite_local() is True
    else:
        _write_server_database(local_path, rows)
        monkeypatch.setattr(db, "DB_LOCAL_PATH", str(local_path))

    host = _BrowserHost()
    assert window.DZLLWindow._apply_db_rows(
        host, db.read_servers_from_db(), False,
    ) is False

    by_ip = {obj.ip: obj for obj in host.store.items}
    assert set(by_ip) == {
        "1.1.1.1", "2.2.2.2", "3.3.3.3",
        "4.4.4.4", "5.5.5.5", "6.6.6.6",
    }
    assert by_ip["2.2.2.2"].name == ""
    assert by_ip["3.3.3.3"].password is False
    assert by_ip["4.4.4.4"].players == 0
    assert by_ip["5.5.5.5"].map_name == ""
    assert "Chernarus" in host.map_choices


def test_valid_favorites_and_history_attach_but_malformed_keys_create_no_row():
    host = _BrowserHost()
    host.favorites = {
        "1.2.3.4:2302": True,
        "999.999.999.999:70000": True,
    }
    host.last_played = {
        "1.2.3.4:2302": int(time.time()),
        "999.999.999.999:70000": int(time.time()),
    }

    assert _load(host, [_row("1.2.3.4")]) is True

    assert len(host.store.items) == 1
    assert host.store.items[0].fav is True
    assert host.store.items[0].played
    assert "999.999.999.999:70000" not in host._obj_by_key


def test_companion_persisted_endpoint_uses_same_validation():
    host = _BrowserHost()

    invalid = window.DZLLWindow._server_companion_obj_from_persisted(
        host,
        {"ip": "example.com", "gport": 2302, "qport": 2303},
    )
    valid = window.DZLLWindow._server_companion_obj_from_persisted(
        host,
        {"ip": " 1.2.3.4 ", "gport": 2302, "qport": None, "name": "Valid"},
    )

    assert invalid is None
    assert isinstance(valid, ServerObject)
    assert (valid.ip, valid.gport, valid.qport) == ("1.2.3.4", 2302, 2303)


def test_invalid_companion_restore_does_not_activate_or_schedule_polling():
    class Host(_BrowserHost):
        def __init__(self):
            super().__init__()
            self._server_companion_rows_loaded = True
            self._server_companion_snapshot = None
            self.settings = {"show_server_companion": True}
            self.server_companion_panel = type(
                "Panel", (), {"get_root": lambda _self: object()}
            )()
            self._last_server_companion_saved = {
                "ip": "::1",
                "gport": 2302,
                "qport": 2303,
            }
            self.activations = []

        def _server_companion_obj_from_persisted(self, data):
            return window.DZLLWindow._server_companion_obj_from_persisted(self, data)

        def set_server_companion_server(self, obj, persist=False):
            self.activations.append((obj, persist))

    host = Host()

    window.DZLLWindow._restore_server_companion_if_enabled(host)

    assert host.activations == []


def test_invalid_companion_selection_is_rejected_before_monitoring_or_persistence():
    invalid = ServerObject(
        ip="example.com", gport=2302, qport=2303, name="Invalid", mods_json="[]"
    )

    # Validation is the first operation; an invalid selection cannot reach any
    # of the monitoring, learning, persistence, panel, or timer dependencies.
    window.DZLLWindow.set_server_companion_server(object(), invalid)


def test_foreground_join_rejects_invalid_endpoint_before_gate_or_worker():
    class Host:
        def __init__(self):
            self.statuses = []

        def _set_server_companion_join_status(self, message, flash=False):
            self.statuses.append((message, flash))

    host = Host()
    invalid = ServerObject(
        ip="example.com", gport=2302, qport=2303, name="Invalid", mods_json="[]"
    )

    window.DZLLWindow._join_server_for_obj(host, invalid)

    assert host.statuses == [("The selected server has an invalid address or port.", True)]


def _persist_server_mod_candidate(path, candidate):
    staged, _stages = server_mod_cache.stage(path, dict(candidate))
    server_mod_cache.publish(staged, path)


@pytest.mark.parametrize("payload", [
    "", "not json", "{}", "null", "[]", "[null, 5, [], {}]",
    json.dumps([
        {"steamWorkshopId": 0, "name": "@Zero"},
        {"steamWorkshopId": "42", "name": "@Repeated"},
        {"steamWorkshopId": "42", "name": "@Repeated"},
        {"name": "Unicode café 🚀"},
        *({"steamWorkshopId": str(i), "name": f"Mod {i}"} for i in range(10)),
    ]),
])
def test_server_mod_cache_warm_output_matches_uncached_builders_without_row_decode(tmp_path, monkeypatch, payload):
    path = tmp_path / "server_mods_v1.json"
    row = _row("1.2.3.4")
    row["mods"] = payload
    row["modCount"] = 77
    cold = _BrowserHost()
    cold._server_mod_cache_path = path
    assert _load(cold, [row]) is True
    _persist_server_mod_candidate(path, cold._server_mod_cache_candidate)
    warm = _BrowserHost()
    warm._server_mod_cache_path = path
    monkeypatch.setattr(mod_parsed.ParsedServerMods, "from_json",
                        lambda *_args: pytest.fail("per-server JSON decoded on hit"))
    assert _load(warm, [row]) is True
    old = cold.store.items[0]
    new = warm.store.items[0]
    assert (new.mod_count, new.mods_preview, new.mods_json) == (old.mod_count, old.mods_preview, payload)
    assert new.mod_search_index == old.mod_search_index
    assert all(type(values) is frozenset for values in new.mod_search_index.values())
    assert new.mod_search_index["names"] is new.mod_search_index["compact_names"]
    for query in ("42", "repeated", "zero", "café", "mod", "cf", "no-such-mod"):
        parsed = mod_search.parse_required_mod_query(query)
        assert mod_search.server_matches_mod_query(new.mod_search_index, parsed) == mod_search.server_matches_mod_query(old.mod_search_index, parsed)


def test_server_mod_cache_warm_selected_chips_filter_like_cold(tmp_path):
    class ChipHost(_BrowserHost):
        _selected_mod_chip_query_terms = window.DZLLWindow._selected_mod_chip_query_terms
        _build_filter_state = window.DZLLWindow._build_filter_state
        _normal_search_filter_text = lambda self: ""

        def __init__(self):
            super().__init__()
            self.settings = {"hide_test_servers": False}
            self._selected_mod_chips = [
                {"query_term": "42"},
                {"query_term": "code lock"},
            ]

    path = tmp_path / "server_mods_v1.json"
    matching = _row("1.2.3.4")
    matching["mods"] = '[{"steamWorkshopId":"42","name":"@Code Lock"}]'
    other = _row("2.3.4.5")
    other["mods"] = '[{"steamWorkshopId":"42","name":"Unrelated"}]'
    cold = ChipHost()
    cold._server_mod_cache_path = path
    assert _load(cold, [matching, other]) is True
    _persist_server_mod_candidate(path, cold._server_mod_cache_candidate)
    warm = ChipHost()
    warm._server_mod_cache_path = path
    assert _load(warm, [matching, other]) is True
    for host in (cold, warm):
        host._filter_state = host._build_filter_state()
        assert [window.DZLLWindow._combined_filter_func(host, obj) for obj in host.store.items] == [True, False]
    assert cold._filter_state["mod_query"] == warm._filter_state["mod_query"]


def test_server_mod_cache_partial_change_query_port_and_duplicate_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    first = _row("1.2.3.4")
    first["mods"] = '[{"steamWorkshopId": "10", "name": "First"}]'
    other = _row("2.3.4.5")
    other["mods"] = '[{"steamWorkshopId": "20", "name": "Other"}]'
    seed = _BrowserHost()
    seed._server_mod_cache_path = path
    _load(seed, [first, other])
    _persist_server_mod_candidate(path, seed._server_mod_cache_candidate)

    changed = dict(first, qport=2310, mods='[{"steamWorkshopId": "30", "name": "Changed"}]', modCount=9)
    earlier_duplicate = dict(first, mods='[{"steamWorkshopId": "40", "name": "Earlier"}]')
    calls = []
    original = mod_parsed.ParsedServerMods.from_json.__func__
    def traced(cls, value):
        calls.append(value)
        return original(cls, value)
    monkeypatch.setattr(mod_parsed.ParsedServerMods, "from_json", classmethod(traced))
    host = _BrowserHost()
    host._server_mod_cache_path = path
    _load(host, [earlier_duplicate, changed, dict(other, qport=2400)])
    assert calls == [earlier_duplicate["mods"], changed["mods"]]
    assert [obj.mod_search_index["ids"] for obj in host.store.items] == [
        frozenset({"40"}), frozenset({"30"}), frozenset({"20"}),
    ]
    assert host.store.items[1].mod_count == 9
    assert host._obj_by_key["1.2.3.4:2302"].mods_json == changed["mods"]
    assert host._server_mod_cache_candidate["1.2.3.4:2302"][0] == server_mod_cache.fingerprint(changed["mods"])
    _persist_server_mod_candidate(path, host._server_mod_cache_candidate)
    assert len(server_mod_cache.load(path)) == 2
    disappeared = _BrowserHost()
    disappeared._server_mod_cache_path = path
    _load(disappeared, [changed])
    _persist_server_mod_candidate(path, disappeared._server_mod_cache_candidate)
    assert set(server_mod_cache.load(path)) == {"1.2.3.4:2302"}
    reappeared = _BrowserHost()
    reappeared._server_mod_cache_path = path
    _load(reappeared, [changed, other])
    assert reappeared.store.items[1].mod_search_index["ids"] == frozenset({"20"})


def test_server_mod_cache_corrupt_version_bounds_and_atomic_failure(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    row = _row("1.2.3.4")
    host = _BrowserHost()
    host._server_mod_cache_path = path
    _load(host, [row])
    _persist_server_mod_candidate(path, host._server_mod_cache_candidate)
    original = path.read_bytes()
    path.write_bytes(original[:-1] + b"x")
    assert server_mod_cache.load(path) == {}
    payload = json.loads(original.split(b"\n", 2)[2])
    payload["normalization_version"] += 1
    changed = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(server_mod_cache.MAGIC + hashlib.sha256(changed).hexdigest().encode() + b"\n" + changed)
    assert server_mod_cache.load(path) == {}
    monkeypatch.setattr(server_mod_cache, "MAX_BYTES", len(original) - 1)
    path.write_bytes(original)
    assert server_mod_cache.load(path) == {}
    monkeypatch.setattr(server_mod_cache, "MAX_BYTES", 64 * 1024 * 1024)
    bad = dict(host._server_mod_cache_candidate)
    bad["1.2.3.4:2302"] = (server_mod_cache.fingerprint("[]"), 0, "", {"ids": frozenset({"x"})})
    with pytest.raises((KeyError, ValueError)):
        server_mod_cache.stage(path, bad)
    assert path.read_bytes() == original
    monkeypatch.setattr(server_mod_cache.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    staged, _ = server_mod_cache.stage(path, host._server_mod_cache_candidate)
    with pytest.raises(OSError):
        server_mod_cache.publish(staged, path)
    staged.unlink()
    assert path.read_bytes() == original


def test_server_mod_cache_publication_generation_shutdown_and_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    pending = []
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback, *args: pending.append((callback, args)) or 1)
    class Executor:
        def __init__(self):
            self.jobs = []
        def submit(self, fn, *args, **kwargs):
            future = Future()
            self.jobs.append((fn, args, kwargs, future))
            return future
        def complete(self):
            fn, args, kwargs, future = self.jobs.pop(0)
            future.set_result(fn(*args, **kwargs))
            callback, cbargs = pending.pop(0)
            callback(*cbargs)
    host = _BrowserHost()
    host._server_mod_cache_path = path
    host._update_executor = Executor()
    row = _row("1.2.3.4")
    _load(host, [row])
    assert window.DZLLWindow._schedule_server_mod_cache_write(host, host._server_mod_cache_generation - 1) is False
    assert host._update_executor.jobs == []
    window.DZLLWindow._schedule_server_mod_cache_write(host)
    assert len(host._update_executor.jobs) == 1
    host._server_mod_cache_generation += 1
    host._update_executor.complete()
    assert not path.exists()
    window.DZLLWindow._schedule_server_mod_cache_write(host)
    host._shutdown_cleanup_done = True
    host._update_executor.complete()
    assert not path.exists()
    host._shutdown_cleanup_done = False
    window.DZLLWindow._schedule_server_mod_cache_write(host)
    host._update_executor.complete()
    assert path.exists()
    _load(host, [row])
    window.DZLLWindow._schedule_server_mod_cache_write(host)
    assert host._update_executor.jobs == []


def test_server_mod_cache_provisional_failed_final_and_manual_refresh(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback, *args: callback(*args) or 1)
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda _delay, callback, *args: callback(*args) or 1)
    class ImmediateExecutor:
        def submit(self, fn, *args, **kwargs):
            future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future
    class ApplyHost(_BrowserHost):
        def __init__(self):
            super().__init__()
            self._server_mod_cache_path = path
            self._update_executor = ImmediateExecutor()
            self._db_executor = ImmediateExecutor()
            self._server_db_update_inflight = False
            self._startup_live_generation = 0
            self.empty_label = type("Label", (), {"set_text": lambda *_: None, "set_visible": lambda *_: None})()
        def _bm_live_group(self, _key):
            return 0
        def _submit_startup_rest_batches(self, *_args, **_kwargs):
            pass
        def _offline_recheck_tick(self):
            return False
        def _rebuild_column_view_store(self, **_kwargs):
            pass
        def _update_sort_indicators(self):
            pass
        def _queue_scroll_to_top(self):
            pass
        def _apply_db_rows(self, rows, ok):
            return window.DZLLWindow._apply_db_rows(self, rows, ok)
    host = ApplyHost()
    first = _row("1.2.3.4")
    first["mods"] = '[{"name":"First"}]'
    second = dict(first, mods='[{"name":"Second"}]')
    assert window.DZLLWindow._apply_db_rows(host, [first], True) is False
    original = path.read_bytes()
    host._suggestion_provisional_load = True
    window.DZLLWindow._apply_db_rows(host, [second], True)
    assert path.read_bytes() == original
    host._suggestion_provisional_load = False
    window.DZLLWindow._apply_db_rows(host, [second], False)
    assert path.read_bytes() == original
    window.DZLLWindow._apply_db_rows(host, [second], True)
    assert path.read_bytes() != original
    assert host.store.items[0].mods_preview == "Second"
    published = path.read_bytes()
    monkeypatch.setattr(window, "fetch_db_overwrite_local", lambda: False)
    window.DZLLWindow._manual_update_server_database(host)
    assert path.read_bytes() == published
    monkeypatch.setattr(window, "fetch_db_overwrite_local", lambda: True)
    monkeypatch.setattr(window, "read_servers_from_db", lambda: [first])
    window.DZLLWindow._manual_update_server_database(host)
    assert path.read_bytes() != published
    assert host.store.items[0].mods_preview == "First"
    other = _row("2.3.4.5")
    window.DZLLWindow._apply_db_rows(host, [first, other], True)
    assert len(server_mod_cache.load(path)) == 2
    window.DZLLWindow._apply_db_rows(host, [first], True)
    assert set(server_mod_cache.load(path)) == {"1.2.3.4:2302"}
    retained = path.read_bytes()
    window.DZLLWindow._apply_db_rows(host, [_row("invalid address")], True)
    assert path.read_bytes() == retained
    monkeypatch.setattr(window.GLib, "timeout_add_seconds",
                        lambda delay, *_args: (_ for _ in ()).throw(RuntimeError("timer unavailable")) if delay == 25 else 1)
    window.DZLLWindow._apply_db_rows(host, [second], True)
    assert host.store.items[0].mods_preview == "Second"
    assert path.read_bytes() == retained


def test_server_mod_cache_nonstring_input_and_bounded_record_restore(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    row = _row("1.2.3.4")
    row["mods"] = {"unexpected": "shape"}
    host = _BrowserHost()
    host._server_mod_cache_path = path
    _load(host, [row])
    assert host.store.items[0].mods_json == ""
    assert host.store.items[0].mods_preview == ""
    _persist_server_mod_candidate(path, host._server_mod_cache_candidate)
    records = server_mod_cache.load(path)
    monkeypatch.setattr(server_mod_cache, "MAX_SET_ITEMS", 0)
    records["1.2.3.4:2302"][3] = ["oversized"]
    assert server_mod_cache.lookup(records, "1.2.3.4:2302", server_mod_cache.fingerprint("")) is None


def test_server_mod_cache_restoration_canonicalizes_once_and_round_trips_mixed_records(tmp_path):
    path = tmp_path / "server_mods_v1.json"
    first = _row("1.2.3.4")
    first["mods"] = '[{"steamWorkshopId":"42","name":"Code Lock"}]'
    second = _row("2.3.4.5")
    second["mods"] = '[{"steamWorkshopId":"84","name":"Other Mod"}]'
    seed = _BrowserHost()
    seed._server_mod_cache_path = path
    assert _load(seed, [first, second]) is True
    _persist_server_mod_candidate(path, seed._server_mod_cache_candidate)

    records = server_mod_cache.load(path)
    first_key = "1.2.3.4:2302"
    second_key = "2.3.4.5:2302"
    assert type(records[first_key]) is list
    assert type(records[second_key]) is list

    digest = server_mod_cache.fingerprint(first["mods"])
    restored = server_mod_cache.lookup(records, first_key, digest)
    canonical = records[first_key]
    assert type(canonical) is tuple and len(canonical) == 4
    assert canonical[0] == digest
    assert canonical[1:] == restored
    assert canonical[3] is restored[2]

    second_lookup = server_mod_cache.lookup(records, first_key, digest)
    assert second_lookup[2] is canonical[3]
    assert type(records[second_key]) is list

    mixed_path = tmp_path / "mixed.json"
    _persist_server_mod_candidate(mixed_path, records)
    reloaded = server_mod_cache.load(mixed_path)
    assert type(reloaded[first_key]) is list
    assert type(reloaded[second_key]) is list
    assert server_mod_cache.lookup(reloaded, first_key, digest) == restored


def test_server_mod_cache_hit_preserves_cold_global_suggestion_fallback(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    row = _row("1.2.3.4")
    row["mods"] = '[{"steamWorkshopId":"42","name":"@Repeated"}]'
    seed = _BrowserHost()
    seed._server_mod_cache_path = path
    _load(seed, [row])
    _persist_server_mod_candidate(path, seed._server_mod_cache_candidate)
    monkeypatch.setattr(window, "suggestion_cache_path", lambda: tmp_path / "missing-global.json")
    host = _IndexedBrowserHost()
    host._server_mod_cache_path = path
    assert _load(host, [row]) is True
    assert host._mod_suggestion_index == mod_suggestions.build_mod_suggestion_index((row["mods"],))


def test_server_mod_cache_aggregate_profile_reports_warm_hit(tmp_path, monkeypatch):
    path = tmp_path / "server_mods_v1.json"
    row = _row("1.2.3.4")
    cold = _BrowserHost()
    cold._server_mod_cache_path = path
    _load(cold, [row])
    _persist_server_mod_candidate(path, cold._server_mod_cache_candidate)
    monkeypatch.setattr(window, "PERF_LOG_ENABLED", True)
    phases = []
    monkeypatch.setattr(window, "log_startup_phase",
                        lambda _origin, phase, _start, **fields: phases.append((phase, fields)))
    warm = _BrowserHost()
    warm._startup_perf_origin_ns = time.monotonic_ns()
    warm._server_mod_cache_path = path
    _load(warm, [row])
    fields = next(fields for phase, fields in phases if phase == "server_mod_cache_apply")
    assert fields["hits"] == 1 and fields["misses"] == 0 and fields["hit_rate"] == "1.0000"
    assert fields["file_bytes"] == path.stat().st_size and fields["entries"] == 1
    for name in ("cache_read_ms", "cache_integrity_ms", "cache_decode_ms",
                 "cache_envelope_restore_ms", "record_restore_ms", "fingerprint_ms", "total_cache_ms"):
        assert float(fields[name]) >= 0
    assert fields["peak_rss_kib"] > 0
