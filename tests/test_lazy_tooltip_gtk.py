"""Real GTK coverage for the lazy server-row tooltips.

These tests drive real Gtk widgets (and a real Gtk.ColumnView for the
end-to-end case) and emit the actual ``query-tooltip`` signal, so the handler
installed by ``_set_lazy_tooltip`` is exercised exactly as GTK calls it.
"""

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from dzll_launcher import column_view
from dzll_launcher.ui_row import ServerObject


@pytest.fixture(autouse=True)
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


@pytest.fixture
def shown_tooltips(monkeypatch):
    """Record the text GTK would display, keyed by the Gtk.Tooltip instance."""
    shown = {}
    original = Gtk.Tooltip.set_text

    def record(tooltip, text):
        shown[tooltip] = text
        return original(tooltip, text)

    monkeypatch.setattr(Gtk.Tooltip, "set_text", record)
    return shown


@pytest.fixture
def count_handlers(monkeypatch):
    """Return a function counting the lazy handlers connected to a widget.

    query-tooltip stops at the first handler returning True, so duplicates
    would be invisible on a normal hover. While counting, the wrapper returns
    False so every connected handler runs once. Patch before widgets connect.
    """
    state = {"counting": False, "calls": 0}
    original = column_view._on_lazy_tooltip_query

    def wrapper(widget, *args):
        if state["counting"]:
            state["calls"] += 1
            return False
        return original(widget, *args)

    monkeypatch.setattr(column_view, "_on_lazy_tooltip_query", wrapper)

    def count(widget):
        state.update(counting=True, calls=0)
        try:
            widget.emit("query-tooltip", 1, 1, False, GObject.new(Gtk.Tooltip))
        finally:
            state["counting"] = False
        return state["calls"]

    return count


def query_tooltip(widget, shown):
    """Emit query-tooltip like GTK does on hover; return the displayed text or None."""
    tooltip = GObject.new(Gtk.Tooltip)
    handled = widget.emit("query-tooltip", 1, 1, False, tooltip)
    return shown.get(tooltip) if handled else None


def pump_main_loop(rounds=30):
    context = GLib.MainContext.default()
    for _ in range(rounds):
        while context.pending():
            context.iteration(False)
        GLib.usleep(5000)


def find_widgets(root, attr):
    found = []
    stack = [root]
    while stack:
        widget = stack.pop()
        if hasattr(widget, attr):
            found.append(widget)
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def server(name, ip, *, country="", password=False, official=False, mod_count=0):
    return ServerObject(
        name=name, ip=ip, gport=2302, qport=27016, country=country,
        password=password, official=official, mod_count=mod_count,
    )


def build_column_view(items, *, join_tooltip="Join server"):
    store = Gio.ListStore(item_type=ServerObject)
    for item in items:
        store.append(item)
    view = Gtk.ColumnView(model=Gtk.NoSelection(model=store))
    view.append_column(
        Gtk.ColumnViewColumn(title="Name", factory=column_view._make_name_factory()),
    )
    view.append_column(
        Gtk.ColumnViewColumn(
            title="Join",
            factory=column_view._make_action_factory(
                "media-playback-start-symbolic",
                None,
                tooltip_text=join_tooltip,
                factory_name="join",
            ),
        ),
    )
    window = Gtk.Window()
    window.set_default_size(800, 400)
    window.set_child(view)
    window.present()
    pump_main_loop()
    return window, view, store


@pytest.fixture
def column_view_window():
    windows = []

    def build(*args, **kwargs):
        window, view, store = build_column_view(*args, **kwargs)
        windows.append(window)
        return view, store

    yield build
    for window in windows:
        window.destroy()
    pump_main_loop(3)


def name_cell_for(view, server_name):
    for cell in find_widgets(view, "_dzll_name_label"):
        if cell._dzll_name_label.get_text() == server_name:
            return cell
    raise AssertionError(f"no bound name cell for {server_name!r}")


# --- _set_lazy_tooltip on plain real widgets --------------------------------


def test_query_shows_the_current_text(shown_tooltips):
    label = Gtk.Label()
    column_view._set_lazy_tooltip(label, "Password Protected")

    assert label.get_has_tooltip() is True
    # The text is resolved on hover, not pushed into GTK's tooltip property.
    assert label.get_tooltip_text() is None
    assert query_tooltip(label, shown_tooltips) == "Password Protected"


def test_query_follows_text_updates(shown_tooltips):
    label = Gtk.Label()
    column_view._set_lazy_tooltip(label, "first")
    column_view._set_lazy_tooltip(label, "second")

    assert query_tooltip(label, shown_tooltips) == "second"


@pytest.mark.parametrize("cleared", [None, ""])
def test_cleared_text_shows_no_tooltip(shown_tooltips, cleared):
    label = Gtk.Label()
    column_view._set_lazy_tooltip(label, "something")
    column_view._set_lazy_tooltip(label, cleared)

    assert query_tooltip(label, shown_tooltips) is None


def test_existing_static_tooltip_is_replaced(shown_tooltips):
    button = Gtk.Button()
    button.set_tooltip_text("static")
    column_view._set_lazy_tooltip(button, "lazy")

    assert button.get_tooltip_text() is None
    assert query_tooltip(button, shown_tooltips) == "lazy"


def test_repeated_updates_connect_a_single_handler(count_handlers, shown_tooltips):
    label = Gtk.Label()
    for index in range(25):
        column_view._set_lazy_tooltip(label, f"text {index}")
    column_view._set_lazy_tooltip(label, None)
    column_view._set_lazy_tooltip(label, "final")

    assert query_tooltip(label, shown_tooltips) == "final"
    assert count_handlers(label) == 1


# --- Server rows: real cells bound, rebound and unbound ---------------------


def test_column_view_rows_show_their_own_server_tooltips(column_view_window, shown_tooltips):
    view, _store = column_view_window([
        server("Alpha", "1.1.1.1", country="FR", password=True),
        server("Bravo", "2.2.2.2", country="DE", official=True),
    ])
    alpha = name_cell_for(view, "Alpha")
    bravo = name_cell_for(view, "Bravo")

    assert query_tooltip(alpha._dzll_name_label, shown_tooltips) == "Alpha"
    assert query_tooltip(bravo._dzll_name_label, shown_tooltips) == "Bravo"
    assert query_tooltip(alpha._dzll_ipport_label, shown_tooltips) == (
        "Right-click to copy address: 1.1.1.1:2302"
    )
    assert query_tooltip(bravo._dzll_ipport_label, shown_tooltips) == (
        "Right-click to copy address: 2.2.2.2:2302"
    )
    assert query_tooltip(alpha._dzll_flag_label, shown_tooltips) == (
        column_view.country_tooltip_for("FR")
    )
    assert query_tooltip(alpha._dzll_lock_label, shown_tooltips) == "Password Protected"
    assert query_tooltip(bravo._dzll_lock_label, shown_tooltips) is None
    assert query_tooltip(bravo._dzll_official_badge, shown_tooltips) == "Official Server"
    assert query_tooltip(alpha._dzll_official_badge, shown_tooltips) is None

    join_buttons = find_widgets(view, "_dzll_drag_action_light")
    assert join_buttons
    for button in join_buttons:
        assert query_tooltip(button, shown_tooltips) == "Join server"


def test_rebinding_a_cell_to_another_server_updates_its_tooltips(
    column_view_window, shown_tooltips,
):
    view, _store = column_view_window([server("Alpha", "1.1.1.1", password=True)])
    cell = name_cell_for(view, "Alpha")

    # Same real widget recycled for another server, as GTK does while scrolling.
    column_view._bind_name_cell(cell, server("Bravo", "2.2.2.2", country="DE"))

    assert query_tooltip(cell._dzll_name_label, shown_tooltips) == "Bravo"
    assert query_tooltip(cell._dzll_ipport_label, shown_tooltips) == (
        "Right-click to copy address: 2.2.2.2:2302"
    )
    assert query_tooltip(cell._dzll_flag_label, shown_tooltips) == (
        column_view.country_tooltip_for("DE")
    )
    assert query_tooltip(cell._dzll_lock_label, shown_tooltips) is None


def test_unbinding_a_cell_clears_its_tooltips(column_view_window, shown_tooltips):
    view, store = column_view_window([server("Alpha", "1.1.1.1", country="FR", password=True)])
    cell = name_cell_for(view, "Alpha")

    store.remove_all()  # GTK runs the factory's unbind on the row
    pump_main_loop()

    for widget in (
        cell._dzll_name_label,
        cell._dzll_flag_label,
        cell._dzll_ipport_label,
        cell._dzll_lock_label,
        cell._dzll_official_badge,
    ):
        assert query_tooltip(widget, shown_tooltips) is None


def test_rebinding_many_times_keeps_one_handler_per_widget(
    column_view_window, shown_tooltips, count_handlers,
):
    view, _store = column_view_window([server("Alpha", "1.1.1.1", password=True)])
    cell = name_cell_for(view, "Alpha")

    for index in range(20):
        column_view._bind_name_cell(cell, server(f"Server {index}", f"10.0.0.{index}"))
        column_view._bind_name_cell(cell, None)
    column_view._bind_name_cell(cell, server("Last", "9.9.9.9"))

    assert query_tooltip(cell._dzll_name_label, shown_tooltips) == "Last"
    for widget in (
        cell._dzll_name_label,
        cell._dzll_flag_label,
        cell._dzll_ipport_label,
        cell._dzll_lock_label,
        cell._dzll_official_badge,
        cell._dzll_mods_button,
    ):
        assert count_handlers(widget) == 1
