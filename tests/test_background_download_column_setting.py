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

        column.set_visible(False)
        _settle_layout(window)
        assert not column.get_visible()
        assert columns.get_n_items() == count
        assert not download_buttons[0].get_mapped()
        hidden_download_ok, _hidden_download_bounds = (
            download_buttons[0].compute_bounds(view)
        )
        assert not hidden_download_ok
        hidden_name_ok, hidden_name_bounds = name_cell.compute_bounds(view)
        assert hidden_name_ok
        assert hidden_name_bounds.get_width() > shown_name_width

        column.set_visible(True)
        _settle_layout(window)
        assert column.get_visible()
        assert columns.get_n_items() == count
        restored_download_buttons = [
            widget for widget in _walk(view)
            if isinstance(widget, Gtk.Button)
            and widget.has_css_class("dzll-download-mods-button")
        ]
        assert len(restored_download_buttons) == 1
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
