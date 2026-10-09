from types import MethodType, SimpleNamespace

from dzll_launcher import column_view
from dzll_launcher.settings import DEFAULTS
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


def test_pin_favourites_defaults_to_enabled():
    assert DEFAULTS["pin_favorite_servers"] is True


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_n_items(self):
        return len(self.rows)

    def get_item(self, index):
        return self.rows[index]

    def splice(self, position, removed, additions):
        self.rows[position:position + removed] = list(additions)


def server(name, *, fav=False, ping=40):
    return ServerObject(
        ip="10.0.0.1" if fav else "10.0.0.2",
        gport=2302,
        qport=2303,
        fav=fav,
        ping=ping,
        sort_ping=ping,
        name=name,
    )


def sort_host(rows, *, sort_key="fav", sort_asc=True, pin=False):
    source = Store(rows)
    visible = Store()
    host = SimpleNamespace(
        settings={"pin_favorite_servers": pin, "prioritise_trusted_servers": False},
        favorites={fav_key(row.ip, row.gport): True for row in rows if bool(row.fav)},
        _filter_state={},
        store=source,
        column_view_store=visible,
        sort_key=sort_key,
        sort_asc=sort_asc,
        _debug_sort_note_model_event=lambda *_args: None,
        _browser_reorder_is_background_reason=lambda *_args: False,
        _debug_browser_reorder=lambda *_args, **_kwargs: None,
        _debug_sort_note_key_build=lambda *_args: None,
        _active_filter_depends_on_live_values=lambda: True,
    )
    host._combined_filter_func = lambda _obj: True
    return host, visible


def test_fav_header_is_registered_as_sortable():
    assert column_view._SORTABLE_HEADER_KEYS["FAV"] == "fav"


def test_fav_is_a_recognised_sort_key():
    assert "fav" in DZLLWindow.SORT_KEYS


def test_header_click_dispatches_fav_sort():
    calls = []
    host = SimpleNamespace(
        _set_sort=lambda key, **kwargs: calls.append(key),
        _filter_timing_log=lambda _ctx: None,
    )
    DZLLWindow._on_column_view_sort_header_clicked(host, "fav")
    assert calls == ["fav"]


def test_rebuild_sorts_favourites_first_when_sort_key_is_fav():
    favourite = server("Favourite", fav=True)
    normal = server("Normal", fav=False)
    host, visible = sort_host([normal, favourite], sort_key="fav", sort_asc=True)

    DZLLWindow._rebuild_column_view_store(host, reorder_reason="test")

    assert visible.rows == [favourite, normal]


def test_fav_sort_direction_toggle_reverses_order():
    favourite = server("Favourite", fav=True)
    normal = server("Normal", fav=False)
    host, visible = sort_host([normal, favourite], sort_key="fav", sort_asc=False)

    DZLLWindow._rebuild_column_view_store(host, reorder_reason="test")

    assert visible.rows == [normal, favourite]


def test_fav_sort_is_independent_of_pin_favorite_servers_setting():
    favourite = server("Favourite", fav=True)
    normal = server("Normal", fav=False)
    host, visible = sort_host([favourite, normal], sort_key="fav", sort_asc=True, pin=False)

    DZLLWindow._rebuild_column_view_store(host, reorder_reason="test")

    assert visible.rows == [favourite, normal]
