"""Browser-live targeting against a real DZLLWindow ColumnView.

The window is presented on the live display (see the memory trap on desktop
input during GTK tests). A2S, the startup updater, the Steam player-count
poll and the five-second refresh timer are all disabled; target selection is
called directly.
"""

import time

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from dzll_launcher import column_view as column_view_module
from dzll_launcher import window as window_module
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key
from test_core_startup_robustness import _close_window, _new_app, isolated_window_state  # noqa: F401

N_ROWS = 600
TALL_ROW_PX = 150


@pytest.fixture(autouse=True)
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


def _pump(seconds):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.003)


@pytest.fixture
def browser(isolated_window_state, monkeypatch):  # noqa: F811
    monkeypatch.setattr(window_module, "query_server_live", lambda *a, **k: {"ok": False, "err": "test"})

    def no_startup_player_count(self):
        self._steam_global_players_startup_source_id = 0
        return False

    monkeypatch.setattr(DZLLWindow, "_steam_global_players_startup_tick", no_startup_player_count)
    monkeypatch.setattr(window_module.SteamGlobalPlayersPoller, "tick", lambda self: False)

    def make(tall_every=0):
        if tall_every:
            original = column_view_module._register_bound_cell

            def register(drag_light, factory_name, cell, list_item, full_refresh):
                original(drag_light, factory_name, cell, list_item, full_refresh)
                if factory_name == "name" and cell is not None:
                    index = int(list_item.get_item().name.split()[-1])
                    cell.set_size_request(-1, TALL_ROW_PX if index % tall_every == 0 else -1)

            monkeypatch.setattr(column_view_module, "_register_bound_cell", register)
        app = _new_app()
        window = DZLLWindow(app)
        window._stop_browser_live_refresh()
        objs = [
            ServerObject(
                name=f"Visible Target {i:04d}", ip=f"10.3.{i // 250}.{i % 250 + 1}",
                gport=2302, qport=27016, players=5, max_players=60, ping=40,
            )
            for i in range(N_ROWS)
        ]
        for obj in objs:
            window._obj_by_key[fav_key(obj.ip, obj.gport)] = obj
        window.column_view_store.splice(0, 0, objs)
        window.set_default_size(1400, 900)
        window.present()
        _pump(1.5)
        made.append((app, window))
        return window

    made = []
    yield make
    for app, window in made:
        _close_window(app, window)


def _scroll_to(window, fraction):
    adjustment = window.scroller.get_vadjustment()
    adjustment.set_value((adjustment.get_upper() - adjustment.get_page_size()) * fraction)
    _pump(0.8)


def _rows_on_screen(window):
    """Row numbers read from name labels of rows overlapping the viewport.

    Identifies rows by their rendered text, independently of the tracker's
    list-item positions.
    """
    height = window.scroller.get_height()
    found = set()
    stack = [window.list_view]
    while stack:
        widget = stack.pop()
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
        if not (isinstance(widget, Gtk.Label) and widget.get_mapped()):
            continue
        text = widget.get_text()
        if not text.startswith("Visible Target "):
            continue
        cell = widget.get_parent()
        while cell is not None and cell.get_css_name() != "cell":
            cell = cell.get_parent()
        ok, rect = (cell or widget).compute_bounds(window.scroller)
        if ok and rect.get_y() + rect.get_height() > 0 and rect.get_y() < height:
            found.add(int(text.split()[-1]))
    return sorted(found)


def _targets(window):
    visible, ahead = window._browser_live_candidate_groups()
    index = {fav_key(o.ip, o.gport): int(o.name.split()[-1]) for o in window.column_view_store}
    return [index[k] for k, _ip, _q in visible], [index[k] for k, _ip, _q in ahead]


@pytest.mark.parametrize("tall_every", [0, 4], ids=["uniform", "variable-heights"])
def test_targets_follow_deep_scrolling(browser, tall_every):
    window = browser(tall_every)
    assert len(window._browser_bound_rows) > 0
    for fraction in (0.0, 0.3, 0.6, 0.95, 0.0):
        _scroll_to(window, fraction)
        on_screen = _rows_on_screen(window)
        visible, ahead = _targets(window)
        assert window._browser_live_range_source == "realised"
        assert on_screen, fraction
        assert visible == on_screen, fraction
        if visible[-1] < N_ROWS - 1:
            assert ahead[0] == visible[-1] + 1


def test_targets_follow_a_resorted_list(browser):
    window = browser()
    _scroll_to(window, 0.5)
    before = _targets(window)[0]
    objs = list(window.column_view_store)
    window.column_view_store.splice(0, window.column_view_store.get_n_items(), list(reversed(objs)))
    window._invalidate_browser_live_targets()
    _pump(0.8)
    visible, _ahead = _targets(window)
    assert window._browser_live_range_source == "realised"
    # Rows now run in descending name order down the screen.
    assert visible == sorted(_rows_on_screen(window), reverse=True)
    assert set(visible) != set(before)


def test_tracker_follows_binding_and_is_cleared_at_shutdown(browser):
    window = browser()
    tracker = window._browser_bound_rows
    bound = len(tracker)
    assert bound > 0
    window.column_view_store.remove_all()
    _pump(0.5)
    assert len(tracker) == 0  # unbind discards every row
    assert window._browser_live_candidate_groups() == ([], [])
    window.column_view_store.splice(0, 0, [ServerObject(name="Visible Target 0000", ip="10.3.9.1", gport=2302)])
    _pump(0.5)
    assert len(tracker) == 1
    window._shutdown_cleanup()
    assert len(tracker) == 0


def test_reorder_diagnostics_share_the_refresh_visible_range(browser):
    window = browser(tall_every=4)
    debug = window._browser_reorder
    assert debug._visible_range.__func__ is DZLLWindow._browser_live_visible_range
    assert debug._visible_range.__self__ is window
    debug.enabled = True
    try:
        for fraction in (0.0, 0.6, 0.95):
            _scroll_to(window, fraction)
            first = _rows_on_screen(window)[0]
            obj = next(o for o in window.column_view_store if o.name == f"Visible Target {first:04d}")
            assert debug.scroll_key() == window._browser_reorder_key_for_obj(obj)
            visible, _ahead = window._browser_live_candidate_groups()
            assert visible[0][0] == fav_key(obj.ip, obj.gport)
    finally:
        debug.enabled = False
