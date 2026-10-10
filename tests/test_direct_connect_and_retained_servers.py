from __future__ import annotations

import json
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window as window_module
from dzll_launcher.window import DZLLWindow, _parse_direct_connect_address, fav_key
from dzll_launcher.ui_row import ServerObject


class _SyncExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Store:
    def __init__(self, items=()):
        self.items = list(items)

    def get_n_items(self):
        return len(self.items)

    def get_item(self, index):
        return self.items[index]

    def splice(self, position, count, replacements):
        self.items[position:position + count] = list(replacements)


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def direct_connect_host(**overrides):
    host = SimpleNamespace(
        store=_Store(),
        _obj_by_key={},
        live={},
        favorites={},
        last_played={},
        retained_servers={},
        sorter=None,
        _executor=_SyncExecutor(),
        column_view_store=None,
        _rebuild_mod_suggestion_index=lambda: None,
        _on_filter_changed=lambda **_kwargs: None,
        _apply_titlebar_counts=lambda: None,
    )
    for key, value in overrides.items():
        setattr(host, key, value)
    return bind(
        host,
        "_retained_snapshot_from_obj",
        "_update_retained_cache_for_obj",
        "_toggle_favorite_for_obj",
        "_snapshot_row_sort_keys",
        "_update_row_sort_ping",
        "_update_row_sort_players",
        "_update_row_sort_played_days",
        "_set_int_property_if_changed",
        "_add_direct_connect_server",
        "_query_direct_connect_info",
        "_apply_direct_connect_info",
        "_submit_live_batch",
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3.4:2302", ("1.2.3.4", 2302, 2303)),
        (" 1.2.3.4:2302 ", ("1.2.3.4", 2302, 2303)),
    ],
)
def test_parse_direct_connect_address_accepts_valid_input(text, expected):
    assert _parse_direct_connect_address(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1.2.3.4",
        "example.com:2302",
        "1.2.3.4:not-a-port",
        "999.999.999.999:2302",
        "1.2.3.4:70000",
    ],
)
def test_parse_direct_connect_address_rejects_invalid_input(text):
    assert _parse_direct_connect_address(text) is None


def test_adding_new_server_creates_row_and_manual_retained_entry(monkeypatch):
    host = direct_connect_host()
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(
        window_module,
        "query_server_live",
        lambda *_args, **_kwargs: {
            "ok": True,
            "ping_ms": 42,
            "players": 5,
            "max_players": 60,
            "queue": None,
            "time": "12:00",
            "password": False,
            "name": "My Server",
            "map": "chernarusplus",
        },
    )
    monkeypatch.setattr(
        window_module,
        "query_server_mods",
        lambda *_args, **_kwargs: {
            "ok": True,
            "mods": [{"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}],
        },
    )

    host._add_direct_connect_server("1.2.3.4", 2302, 2303, add_favorite=False)

    key = fav_key("1.2.3.4", 2302)
    assert key in host._obj_by_key
    obj = host._obj_by_key[key]
    assert obj in host.store.items
    assert obj.fav is False
    assert obj.name == "My Server"
    assert obj.map_name == "Chernarus"
    assert obj.players == 5
    assert obj.ping == 42
    assert obj.mod_count == 1
    assert json.loads(obj.mods_json) == [
        {"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}
    ]

    retained = host.retained_servers[key]
    assert retained["manual"] is True
    assert retained["name"] == "My Server"


def test_adding_new_server_with_favourite_checked_persists_favourite(monkeypatch):
    host = direct_connect_host()
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(window_module, "save_favorites", lambda _favs: None)
    monkeypatch.setattr(
        window_module,
        "query_server_live",
        lambda *_args, **_kwargs: {"ok": False},
    )
    monkeypatch.setattr(
        window_module,
        "query_server_mods",
        lambda *_args, **_kwargs: {"ok": False, "mods": []},
    )

    host._add_direct_connect_server("1.2.3.4", 2302, 2303, add_favorite=True)

    key = fav_key("1.2.3.4", 2302)
    assert host.favorites.get(key) is True
    assert host._obj_by_key[key].fav is True


def test_adding_already_listed_server_does_not_duplicate_row(monkeypatch):
    existing = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="Already Here")
    key = fav_key("1.2.3.4", 2302)
    host = direct_connect_host(
        _obj_by_key={key: existing},
        store=_Store([existing]),
    )
    monkeypatch.setattr(window_module, "save_favorites", lambda _favs: None)
    calls = []
    host._submit_live_batch = lambda keys, reason="batch": calls.append((tuple(keys), reason))

    host._add_direct_connect_server("1.2.3.4", 2302, 2303, add_favorite=True)

    assert len(host.store.items) == 1
    assert existing.fav is True
    assert host.retained_servers[key]["manual"] is True
    assert calls == [((key,), "direct-connect")]


def test_failed_a2s_query_marks_new_row_offline(monkeypatch):
    host = direct_connect_host()
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args, **kwargs: fn(*args, **kwargs))
    monkeypatch.setattr(window_module, "query_server_live", lambda *_args, **_kwargs: {"ok": False})
    monkeypatch.setattr(
        window_module,
        "query_server_mods",
        lambda *_args, **_kwargs: {"ok": False, "mods": []},
    )

    host._add_direct_connect_server("1.2.3.4", 2302, 2303, add_favorite=False)

    key = fav_key("1.2.3.4", 2302)
    obj = host._obj_by_key[key]
    assert obj.ping == -1
    assert host.live[key]["offline"] is True


def test_favouriting_snapshots_row_and_unfavouriting_prunes_non_manual_entry(monkeypatch):
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="Some Server", map_name="Chernarus")
    host = direct_connect_host()
    monkeypatch.setattr(window_module, "save_favorites", lambda _favs: None)
    host.scroller = SimpleNamespace(get_vadjustment=lambda: None)

    host._toggle_favorite_for_obj(obj)
    key = fav_key("1.2.3.4", 2302)
    assert obj.fav is True
    assert host.retained_servers[key]["name"] == "Some Server"
    assert host.retained_servers[key]["manual"] is False

    host._toggle_favorite_for_obj(obj)
    assert obj.fav is False
    assert key not in host.retained_servers


def test_unfavouriting_manual_direct_connect_entry_keeps_retained_row(monkeypatch):
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="Kept Server")
    host = direct_connect_host()
    monkeypatch.setattr(window_module, "save_favorites", lambda _favs: None)
    host.scroller = SimpleNamespace(get_vadjustment=lambda: None)
    key = fav_key("1.2.3.4", 2302)
    host._update_retained_cache_for_obj(obj, manual=True)

    host._toggle_favorite_for_obj(obj)
    host._toggle_favorite_for_obj(obj)

    assert obj.fav is False
    assert key in host.retained_servers
    assert host.retained_servers[key]["manual"] is True


def test_apply_db_rows_merges_retained_rows_missing_from_latest_snapshot():
    host = direct_connect_host(
        retained_servers={
            fav_key("9.9.9.9", 2302): {
                "ip": "9.9.9.9", "gport": 2302, "qport": 2303,
                "name": "Retained Only", "map": "Chernarus",
                "players": 0, "maxPlayers": 60, "password": 0,
                "mods": "[]", "modCount": 0, "third_person": 0,
                "timeWarp": 1.0, "time": "--:--", "country": "",
                "ping": -1, "bm_rank": 999999999, "manual": True,
            },
            fav_key("1.1.1.1", 2302): {
                "ip": "1.1.1.1", "gport": 2302, "qport": 2303,
                "name": "Already In DB Too", "map": "Chernarus",
                "players": 0, "maxPlayers": 60, "password": 0,
                "mods": "[]", "modCount": 0, "third_person": 0,
                "timeWarp": 1.0, "time": "--:--", "country": "",
                "ping": -1, "bm_rank": 999999999, "manual": False,
            },
        },
    )
    host._set_map_choices = lambda _choices: None
    db_rows = [
        {
            "ip": "1.1.1.1", "gport": 2302, "qport": 2303,
            "name": "From DB", "map": "chernarusplus",
            "players": 5, "maxPlayers": 60, "password": 0,
            "mods": "[]", "modCount": 0, "third_person": 0,
            "timeWarp": 1.0, "time": "12:00", "country": "",
            "ping": 30, "bm_rank": 1,
        },
    ]

    seen_rows = {}

    def capture(rows, on_loaded=None):
        seen_rows["rows"] = rows

    host._load_rows_into_store = capture
    DZLLWindow._apply_db_rows(host, db_rows, True)

    ips = {row["ip"] for row in seen_rows["rows"]}
    assert ips == {"1.1.1.1", "9.9.9.9"}
    names_by_ip = {row["ip"]: row["name"] for row in seen_rows["rows"]}
    assert names_by_ip["1.1.1.1"] == "From DB"
    assert names_by_ip["9.9.9.9"] == "Retained Only"
