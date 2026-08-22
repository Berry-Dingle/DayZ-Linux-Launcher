from __future__ import annotations

import sqlite3
import time

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
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE servers (
            ip, gport, qport, name, map, players, maxPlayers, password,
            mods, modCount, third_person, timeWarp, time, country, ping, bm_rank
        )"""
    )
    rows = [
        _row("1.2.3.4", name="Valid"),
        _row("256.1.1.1", name="Invalid IP"),
        _row("2.3.4.5", 70000, 2303, name="Invalid Port"),
        _row(1234, name="Invalid Type"),
    ]
    for row in rows:
        connection.execute(
            "INSERT INTO servers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "DB_LOCAL_PATH", str(path))

    host = _BrowserHost()
    assert _load(host, db.read_servers_from_db()) is True

    assert [(obj.name, obj.ip) for obj in host.store.items] == [
        ("Valid", "1.2.3.4")
    ]


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
