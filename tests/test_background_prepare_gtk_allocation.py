from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from dzll_launcher.window import DZLLWindow


class StatusHost:
    def _background_prepare_retry_failed_clicked(self, _button):
        return None

    def _background_prepare_action_clicked(self, _button):
        return None


def _settle_layout():
    context = GLib.MainContext.default()
    for _ in range(50):
        if not context.pending():
            break
        context.iteration(False)


def test_permanent_status_grid_has_stable_real_gtk_allocation():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")

    host = StatusHost()
    status = DZLLWindow._build_background_prepare_status_block(host)
    host.background_prepare_status = status
    host._background_prepare_active = False
    host._background_prepare_controller = None

    browser = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    search = Gtk.Label(label="Search")
    header = Gtk.Label(label="Server table header")
    table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    table.set_vexpand(True)
    browser.append(search)
    browser.append(status)
    browser.append(header)
    browser.append(table)

    window = Gtk.Window()
    window.set_default_size(900, 500)
    window.set_child(browser)
    window.present()

    try:
        DZLLWindow._background_prepare_set_active_presentation(
            host, True, server_name="Server", queued_count=0,
        )
        host.background_prepare_detail_label.set_text("Preparing…")
        DZLLWindow._background_prepare_set_progress_presentation(host, False)
        status.set_visible(True)
        _settle_layout()

        outer_identity = id(status)
        info = host.background_prepare_info
        progress_row = host.background_prepare_progress_row
        assert host.background_prepare_grid.get_child_at(0, 0) is info
        assert host.background_prepare_grid.get_child_at(0, 1) is progress_row

        allocations = {}

        def record(name):
            _settle_layout()
            allocations[name] = (
                status.get_allocated_height(),
                header.get_allocation().y,
                progress_row.get_allocated_height(),
            )
            assert id(host.background_prepare_status) == outer_identity
            assert host.background_prepare_grid.get_child_at(0, 0) is info
            assert host.background_prepare_grid.get_child_at(0, 1) is progress_row

        record("checking")

        host.background_prepare_progress.set_fraction(0.5)
        host.background_prepare_percent_label.set_text("50%")
        DZLLWindow._background_prepare_set_progress_presentation(host, True)
        record("progress")

        DZLLWindow._background_prepare_present_cancelling(host)
        record("cancelling")

        DZLLWindow._background_prepare_render_batch_summary(
            host,
            SimpleNamespace(
                ready_count=0,
                failed_count=0,
                cancelled_count=1,
                failed_entries=(),
            ),
        )
        record("cancelled")

        heights = {value[0] for value in allocations.values()}
        header_positions = {value[1] for value in allocations.values()}
        progress_heights = {value[2] for value in allocations.values()}
        assert len(heights) == 1
        assert len(header_positions) == 1
        assert len(progress_heights) == 1
        assert next(iter(progress_heights)) > 0
        assert progress_row.get_visible()
        assert progress_row.get_opacity() == 0.0

        for label in (
            host.background_prepare_server_label,
            host.background_prepare_detail_label,
            host.background_prepare_queue_label,
            host.background_prepare_failed_label,
            host.background_prepare_count_label,
            host.background_prepare_right_status_label,
        ):
            assert not label.get_wrap()

        visible_header_y = header.get_allocation().y
        status.set_visible(False)
        DZLLWindow._background_prepare_set_status_container(host, False)
        _settle_layout()
        assert not status.should_layout()
        assert header.get_allocation().y < visible_header_y
    finally:
        window.destroy()
