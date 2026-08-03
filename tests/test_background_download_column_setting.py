from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from dzll_launcher import settings as settings_module
from dzll_launcher.column_view import build_server_column_view
from dzll_launcher.settings import DEFAULTS
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


def _settle_layout():
    context = GLib.MainContext.default()
    for _ in range(100):
        if not context.pending():
            break
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


def test_runtime_toggle_controls_the_one_existing_column_without_touching_operation():
    class Column:
        def __init__(self):
            self.visible = True
            self.calls = []

        def set_visible(self, value):
            self.visible = bool(value)
            self.calls.append(bool(value))

    operation = object()
    column = Column()
    host = SimpleNamespace(
        list_view=SimpleNamespace(background_download_column=column),
        _background_prepare_controller=operation,
        _background_prepare_active=True,
    )
    for visible in (False, True, False, True):
        DZLLWindow._set_background_download_column_visible(host, visible)
    assert column.calls == [False, True, False, True]
    assert host.list_view.background_download_column is column
    assert host._background_prepare_controller is operation
    assert host._background_prepare_active is True


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
        _settle_layout()
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
        shown_join_x = join_buttons[0].get_allocation().x

        column.set_visible(False)
        _settle_layout()
        assert not column.get_visible()
        assert columns.get_n_items() == count
        assert not download_buttons[0].should_layout()
        assert download_buttons[0].get_allocated_width() == 0
        hidden_join_x = join_buttons[0].get_allocation().x
        assert hidden_join_x < shown_join_x

        column.set_visible(True)
        _settle_layout()
        assert column.get_visible()
        assert columns.get_n_items() == count
        assert download_buttons[0].should_layout()
        assert download_buttons[0].get_allocated_width() > 0
        assert join_buttons[0].get_allocation().x == shown_join_x
    finally:
        window.destroy()
