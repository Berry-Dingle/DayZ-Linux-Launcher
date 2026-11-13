from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from dzll_launcher import settings as settings_module
from dzll_launcher.column_view import build_server_column_view
from dzll_launcher.settings import DEFAULTS
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


def _settle_layout(widget):
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)

    frame_clock = widget.get_frame_clock()
    assert frame_clock is not None
    loop = GLib.MainLoop()
    completed_frames = 0

    def after_paint(*_args):
        nonlocal completed_frames
        completed_frames += 1
        if completed_frames >= 2:
            loop.quit()
        else:
            frame_clock.request_phase(Gdk.FrameClockPhase.PAINT)

    def timeout():
        loop.quit()
        return GLib.SOURCE_REMOVE

    handler = frame_clock.connect("after-paint", after_paint)
    timeout_id = GLib.timeout_add(1000, timeout)
    widget.queue_allocate()
    frame_clock.request_phase(Gdk.FrameClockPhase.PAINT)
    loop.run()
    frame_clock.disconnect(handler)
    if completed_frames >= 2:
        GLib.source_remove(timeout_id)
    assert completed_frames >= 2, "GTK did not complete two layout frames"
    while context.pending():
        context.iteration(False)


def _walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _walk(child)
        child = child.get_next_sibling()


def _build_view():
    store = Gio.ListStore.new(ServerObject)
    store.append(ServerObject(
        name="Allocation Server", ip="127.0.0.1", gport=2302, qport=2303,
        max_players=60, ping=40,
    ))
    selection = Gtk.NoSelection.new(store)
    noop = lambda *_args: None
    view, _features = build_server_column_view(
        selection, noop, noop, noop, noop,
        can_download_mods=lambda _obj: True,
        can_join=lambda _obj: True,
        download_presentation=lambda _obj: {},
        join_presentation=lambda _obj: {},
    )
    return view


def test_default_setting_is_disabled():
    assert DEFAULTS["show_background_download_buttons"] is False


def test_saved_disabled_setting_reloads(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "SETTINGS_PATH", str(path))
    saved = dict(DEFAULTS)
    saved["show_background_download_buttons"] = False
    settings_module.save_settings(saved)
    assert settings_module.load_settings()["show_background_download_buttons"] is False


class _FakeColumn:
    def __init__(self, name):
        self.name = name
        self.view = None
        self.visible = True
        self.visible_calls = []

    def get_column_view(self):
        return self.view

    def get_visible(self):
        return self.visible

    def set_visible(self, value):
        self.visible = bool(value)
        self.visible_calls.append(bool(value))


class _FakeColumns:
    def __init__(self, items):
        self._items = items

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]


class _FakeView:
    # No GLib idles: header refresh scheduling is a no-op for the fake.
    _dzll_construction_sources_active = False

    def __init__(self, names):
        self.columns = []
        for name in names:
            column = _FakeColumn(name)
            column.view = self
            self.columns.append(column)
        self.calls = []

    def get_columns(self):
        return _FakeColumns(self.columns)

    def remove_column(self, column):
        self.calls.append(("remove", column.name))
        self.columns.remove(column)
        column.view = None

    def insert_column(self, position, column):
        self.calls.append(("insert", position, column.name))
        self.columns.insert(position, column)
        column.view = self

    def names(self):
        return [column.name for column in self.columns]


def test_runtime_toggle_controls_the_one_existing_column_without_touching_operation():
    view = _FakeView(["fav", "name", "monitor", "download", "join"])
    column = view.columns[3]
    view.background_download_column = column
    view._dzll_background_download_anchor_column = view.columns[4]
    operation = object()
    host = SimpleNamespace(
        list_view=view,
        _background_prepare_controller=operation,
        _background_prepare_active=True,
    )
    for visible in (False, False, True, True, False, True):
        DZLLWindow._set_background_download_column_visible(host, visible)
    assert view.calls == [
        ("remove", "download"),
        ("insert", 3, "download"),
        ("remove", "download"),
        ("insert", 3, "download"),
    ]
    assert view.names() == ["fav", "name", "monitor", "download", "join"]
    # The column is never hidden in place: GTK 4.24 warns for the cell that
    # follows an invisible column.
    assert False not in column.visible_calls
    assert host.list_view.background_download_column is column
    assert host._background_prepare_controller is operation
    assert host._background_prepare_active is True


def test_reinsert_restores_hidden_in_place_column_and_tolerates_missing_column():
    from dzll_launcher.column_view import set_background_download_column_attached

    view = _FakeView(["monitor", "download", "join"])
    column = view.columns[1]
    view.background_download_column = column
    view._dzll_background_download_anchor_column = view.columns[2]
    column.visible = False  # e.g. hidden by an older code path
    assert set_background_download_column_attached(view, True) is True
    assert column.visible is True
    assert view.calls == []

    assert set_background_download_column_attached(SimpleNamespace(), True) is False
    assert set_background_download_column_attached(None, False) is False
    DZLLWindow._set_background_download_column_visible(SimpleNamespace(), True)


def _header_title_buttons(view):
    header = next(
        widget for widget in _walk(view) if widget.get_css_name() == "header"
    )
    child = header.get_first_child()
    buttons = []
    while child is not None:
        if child.get_css_name() == "button":
            buttons.append(child)
        child = child.get_next_sibling()
    return buttons


def _download_factory_buttons(view):
    refresh = view.refresh_download_mods_states
    index = refresh.__code__.co_freevars.index("buttons")
    return refresh.__closure__[index].cell_contents


def _download_buttons(view):
    return [
        widget for widget in _walk(view)
        if isinstance(widget, Gtk.Button)
        and widget.has_css_class("dzll-download-mods-button")
    ]


def test_real_column_visibility_collapses_header_rows_and_reclaims_width():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")

    view = _build_view()
    window = Gtk.Window()
    window.set_default_size(1100, 260)
    window.set_child(view)
    window.present()
    try:
        _settle_layout(window)
        column = view.background_download_column
        columns = view.get_columns()
        count = columns.get_n_items()
        download_buttons = [
            widget for widget in _walk(view)
            if isinstance(widget, Gtk.Button)
            and widget.has_css_class("dzll-download-mods-button")
        ]
        join_buttons = [
            widget for widget in _walk(view)
            if isinstance(widget, Gtk.Button)
            and widget.has_css_class("dzll-join-button")
        ]
        assert len(download_buttons) == 1
        assert len(join_buttons) == 1
        name_labels = [
            widget for widget in _walk(view)
            if isinstance(widget, Gtk.Label)
            and widget.has_css_class("server-name")
        ]
        assert len(name_labels) == 1
        name_cell = name_labels[0].get_parent().get_parent().get_parent()
        shown_name_ok, shown_name_bounds = name_cell.compute_bounds(view)
        shown_download_ok, _shown_download_bounds = (
            download_buttons[0].compute_bounds(view)
        )
        assert shown_name_ok
        assert shown_download_ok
        shown_name_width = shown_name_bounds.get_width()

        host = SimpleNamespace(list_view=view)
        DZLLWindow._set_background_download_column_visible(host, False)
        _settle_layout(window)
        assert column.get_visible()
        assert column.get_column_view() is None
        assert columns.get_n_items() == count - 1
        assert _download_buttons(view) == []
        assert _download_factory_buttons(view) == []
        hidden_name_ok, hidden_name_bounds = name_cell.compute_bounds(view)
        assert hidden_name_ok
        assert hidden_name_bounds.get_width() > shown_name_width

        DZLLWindow._set_background_download_column_visible(host, True)
        _settle_layout(window)
        assert column.get_visible()
        assert column.get_column_view() is view
        assert columns.get_n_items() == count
        assert columns.get_item(count - 2) is column
        restored_download_buttons = _download_buttons(view)
        assert len(restored_download_buttons) == 1
        assert _download_factory_buttons(view) == restored_download_buttons
        assert restored_download_buttons[0].get_mapped()
        restored_download_ok, _restored_download_bounds = (
            restored_download_buttons[0].compute_bounds(view)
        )
        restored_name_ok, restored_name_bounds = name_cell.compute_bounds(view)
        assert restored_download_ok
        assert restored_name_ok
        assert restored_name_bounds.get_width() == pytest.approx(shown_name_width)
    finally:
        window.destroy()


def test_column_removed_before_first_map_reinserts_with_normalized_header():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")

    view = _build_view()
    column = view.background_download_column
    count = view.get_columns().get_n_items()
    host = SimpleNamespace(list_view=view)
    # Startup with the setting disabled removes the column before the
    # one-time header normalisation runs.
    DZLLWindow._set_background_download_column_visible(host, False)
    window = Gtk.Window()
    window.set_default_size(1100, 260)
    window.set_child(view)
    window.present()
    try:
        _settle_layout(window)
        titles = _header_title_buttons(view)
        assert len(titles) == count - 1
        assert all(button.has_css_class("dzll-column-title-flat") for button in titles)
        join_titles = [b for b in titles if b.has_css_class("dzll-column-title-join")]
        assert len(join_titles) == 1

        for visible in (True, False, True):
            DZLLWindow._set_background_download_column_visible(host, visible)
            _settle_layout(window)

        titles = _header_title_buttons(view)
        assert len(titles) == count
        assert all(button.has_css_class("dzll-column-title-flat") for button in titles)
        assert [b for b in titles if b.has_css_class("dzll-column-title-join")] == join_titles
        assert view.get_columns().get_item(count - 2) is column
        download_buttons = _download_buttons(view)
        assert len(download_buttons) == 1
        assert download_buttons[0].get_mapped()
        # Repeated remove/insert must not accumulate stale factory buttons.
        assert _download_factory_buttons(view) == download_buttons
    finally:
        window.destroy()
