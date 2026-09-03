from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from dzll_launcher import db, window
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
def _disable_glib_timers(monkeypatch):
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_args: 1)


def _load(host, rows):
    return window.DZLLWindow._load_rows_into_store(host, rows)


def _write_server_database(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE servers (
            ip, gport, qport, name, map, players, maxPlayers, password,
            mods, modCount, third_person, timeWarp, time, country, ping, bm_rank
        )"""
    )
    connection.executemany(
        "INSERT INTO servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(row.values()) for row in rows],
    )
    connection.commit()
    connection.close()


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
    assert (obj.timewarp, obj.time, obj.country) == (1.0, "--:--", "")
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
