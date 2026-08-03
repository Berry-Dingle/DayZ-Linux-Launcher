from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window as window_module
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_n_items(self):
        return len(self.rows)

    def get_item(self, index):
        return self.rows[index]

    def splice(self, position, removed, additions):
        self.rows[position:position + removed] = list(additions)


def server(name, *, fav=False, ping=40, third_person=False, map_name="Chernarus", **values):
    defaults = {
        "ip": f"10.0.0.{1 if fav else 2}",
        "gport": 2302,
        "qport": 2303,
        "fav": fav,
        "ping": ping,
        "third_person": third_person,
        "map_name": map_name,
        "max_players": 60,
        "password": False,
        "played": "1 Day Ago",
        "name": name,
        "sort_ping": ping if ping >= 0 else 999999,
    }
    defaults.update(values)
    obj = ServerObject(**defaults)
    obj.search_blob = f"{obj.name.lower()}\n{obj.ip}:{obj.gport}"
    return obj


def filter_host(*, pin, state, favorites):
    return SimpleNamespace(
        settings={"pin_favorite_servers": pin},
        favorites=favorites,
        _filter_state=state,
        _build_filter_state=lambda: state,
    )


def base_state(obj):
    return {
        "live": {fav_key(obj.ip, obj.gport): {"hide_high_ping": False}},
        "query": "",
        "mod_query_mode": False,
        "mod_query": (),
        "hide_test_servers": False,
        "max_players_cutoff": 0,
        "show_fav": False,
        "one_pp_only": False,
        "three_pp_only": False,
        "no_password": False,
        "online_only": False,
        "played_only": False,
        "selected_map": "All Maps",
    }


def matches(obj, *, pin, **updates):
    state = base_state(obj)
    state.update(updates)
    favorites = {fav_key(obj.ip, obj.gport): True} if bool(obj.fav) else {}
    host = filter_host(pin=pin, state=state, favorites=favorites)
    return DZLLWindow._combined_filter_func(host, obj)


def test_offline_favourite_remains_visible_with_online_only():
    favourite = server("Favourite", fav=True, ping=-1)
    assert matches(favourite, pin=False, online_only=True)


@pytest.mark.parametrize(
    ("obj", "state"),
    [
        (
            server("High ping", fav=True, ping=300),
            {"live": {fav_key("10.0.0.1", 2302): {"hide_high_ping": True}}},
        ),
        (server("Small", fav=True, max_players=20), {"max_players_cutoff": 60}),
    ],
)
def test_favourite_bypasses_ping_and_minimum_slot_filters(obj, state):
    assert matches(obj, pin=False, **state)


def test_pin_off_favourite_obeys_test_server_filter():
    favourite = server("Test", fav=True)
    favourite.is_likely_test_server = True
    assert not matches(favourite, pin=False, hide_test_servers=True)


@pytest.mark.parametrize(
    ("obj", "state", "visible"),
    [
        (server("Offline", fav=True, ping=-1), {"online_only": True}, True),
        (server("3PP", fav=True, third_person=True), {"one_pp_only": True}, False),
        (server("Map", fav=True, map_name="Livonia"), {"selected_map": "Chernarus"}, False),
        (server("Country GB", fav=True, country="GB"), {"query": "country:de"}, False),
        (
            server("High ping", fav=True, ping=300),
            {"live": {fav_key("10.0.0.1", 2302): {"hide_high_ping": True}}}, True,
        ),
        (server("Small", fav=True, max_players=20), {"max_players_cutoff": 60}, True),
        (server("Password", fav=True, password=True), {"no_password": True}, False),
        (server("Vanilla", fav=True), {"mod_query_mode": True, "mod_query": (("required", "modded"),)}, False),
        (server("Search", fav=True), {"query": "does-not-match"}, False),
        (server("Unplayed", fav=True, played=""), {"played_only": True}, False),
    ],
)
def test_pin_setting_does_not_expand_favourite_exemption_scope(obj, state, visible):
    assert matches(obj, pin=True, **state) is visible


@pytest.mark.parametrize(
    ("obj", "state"),
    [
        (server("Offline", ping=-1), {"online_only": True}),
        (server("3PP", third_person=True), {"one_pp_only": True}),
        (server("Map", map_name="Livonia"), {"selected_map": "Chernarus"}),
        (
            server("High ping", ping=300),
            {"live": {fav_key("10.0.0.2", 2302): {"hide_high_ping": True}}},
        ),
        (server("Small", max_players=20), {"max_players_cutoff": 60}),
        (server("Password", password=True), {"no_password": True}),
        (server("Search"), {"query": "does-not-match"}),
        (server("Unplayed", played=""), {"played_only": True}),
    ],
)
def test_non_favourites_still_obey_active_filters(obj, state):
    assert not matches(obj, pin=True, **state)


def rebuild_host(rows, *, pin, state):
    source = Store(rows)
    visible = Store()
    host = SimpleNamespace(
        settings={"pin_favorite_servers": pin, "prioritise_trusted_servers": False},
        favorites={fav_key(row.ip, row.gport): True for row in rows if bool(row.fav)},
        _filter_state=state,
        store=source,
        column_view_store=visible,
        sort_key="ping",
        sort_asc=True,
        _debug_sort_note_model_event=lambda *_args: None,
        _browser_reorder_is_background_reason=lambda *_args: False,
        _debug_browser_reorder=lambda *_args, **_kwargs: None,
        _debug_sort_note_key_build=lambda *_args: None,
        _active_filter_depends_on_live_values=lambda: True,
    )
    host._combined_filter_func = MethodType(DZLLWindow._combined_filter_func, host)
    return host, visible


def test_pinned_favourites_sort_above_matching_non_favourites_without_duplicates():
    favourite = server("Favourite", fav=True, ping=-1)
    normal = server("Normal", ping=20)
    state = base_state(favourite)
    state["online_only"] = True
    host, visible = rebuild_host([normal, favourite, favourite], pin=True, state=state)
    # Source models normally contain one row per identity; reconciliation is the
    # path responsible for protecting against duplicate injection.
    host.store = Store([normal, favourite])
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="test")
    assert visible.rows == [favourite, normal]
    assert visible.rows.count(favourite) == 1


def test_turning_pinning_on_and_off_does_not_change_exempt_membership():
    favourite = server("Favourite", fav=True, ping=-1)
    state = base_state(favourite)
    state["online_only"] = True
    host, visible = rebuild_host([favourite], pin=False, state=state)
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="pin-off")
    assert visible.rows == [favourite]
    host.settings["pin_favorite_servers"] = True
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="pin-on")
    assert visible.rows == [favourite]
    host.settings["pin_favorite_servers"] = False
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="pin-off-again")
    assert visible.rows == [favourite]


def test_favourite_toggle_updates_filtered_membership_immediately(monkeypatch):
    monkeypatch.setattr(window_module, "save_favorites", lambda _value: None)
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda callback: callback() or 1)
    obj = server("Candidate", ping=-1)
    state = base_state(obj)
    state["online_only"] = True
    host, visible = rebuild_host([obj], pin=True, state=state)
    host.favorites = {}
    host.cb_show_fav = SimpleNamespace(get_active=lambda: False)
    host.scroller = SimpleNamespace(get_vadjustment=lambda: None)
    host._on_filter_changed = lambda **_kwargs: DZLLWindow._rebuild_column_view_store(
        host, reorder_reason="favourites"
    )
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="initial")
    assert visible.rows == []
    DZLLWindow._toggle_favorite_for_obj(host, obj)
    assert visible.rows == [obj]
    DZLLWindow._toggle_favorite_for_obj(host, obj)
    assert visible.rows == []


def test_offline_online_reconciliation_keeps_one_pinned_favourite():
    favourite = server("Favourite", fav=True, ping=20)
    state = base_state(favourite)
    state["online_only"] = True
    host, visible = rebuild_host([favourite], pin=True, state=state)
    host._browser_reorder_visible_keys = lambda *_args: []
    host._browser_reorder_scroll_key = lambda: "-"
    host._browser_reorder_live_filter_state = lambda: {}
    host._browser_reorder_key_for_obj = lambda obj: fav_key(obj.ip, obj.gport)
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="initial")
    for ping in (-1, 25, -1, 30):
        favourite.ping = ping
        DZLLWindow._reconcile_visible_store_membership_preserve_order(host, reason="live")
        assert visible.rows == [favourite]


def test_existing_sort_order_within_groups_is_preserved():
    fav_fast = server("Fav Fast", fav=True, ping=20, ip="10.0.1.1")
    fav_slow = server("Fav Slow", fav=True, ping=40, ip="10.0.1.2")
    normal_fast = server("Normal Fast", ping=10, ip="10.0.2.1")
    normal_slow = server("Normal Slow", ping=50, ip="10.0.2.2")
    state = base_state(fav_fast)
    host, visible = rebuild_host(
        [normal_slow, fav_slow, normal_fast, fav_fast], pin=True, state=state
    )
    DZLLWindow._rebuild_column_view_store(host, reorder_reason="sort")
    assert visible.rows == [fav_fast, fav_slow, normal_fast, normal_slow]
