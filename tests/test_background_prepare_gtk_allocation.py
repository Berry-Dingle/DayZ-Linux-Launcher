from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from dzll_launcher.window import DZLLWindow


class StatusHost:
    def _background_prepare_retry_failed_clicked(self, _button):
        return None

    def _background_prepare_action_clicked(self, _button):
        return None


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
        _settle_layout(window)

        outer_identity = id(status)
        info = host.background_prepare_info
        assert host.background_prepare_grid.get_child_at(0, 0) is info
        assert host.background_prepare_grid.get_child_at(0, 1) is None

        allocations = {}

        def record(name):
            _settle_layout(window)
            status_ok, status_bounds = status.compute_bounds(browser)
            header_ok, header_bounds = header.compute_bounds(browser)
            assert status_ok
            assert header_ok
            allocations[name] = (
                status_bounds.get_height(),
                header_bounds.get_y(),
            )
            assert id(host.background_prepare_status) == outer_identity
            assert host.background_prepare_grid.get_child_at(0, 0) is info
            assert host.background_prepare_grid.get_child_at(0, 1) is None

        record("checking")

        host.background_prepare_percent_label.set_text("50%")
        DZLLWindow._background_prepare_set_progress_presentation(host, True, 0.5)
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
        assert len(heights) == 1
        assert len(header_positions) == 1
        assert next(iter(heights)) > 0

        for label in (
            host.background_prepare_server_label,
            host.background_prepare_detail_label,
            host.background_prepare_queue_label,
            host.background_prepare_failed_label,
            host.background_prepare_count_label,
            host.background_prepare_right_status_label,
        ):
            assert not label.get_wrap()

        visible_status_ok, visible_status_bounds = status.compute_bounds(browser)
        visible_header_ok, visible_header_bounds = header.compute_bounds(browser)
        assert visible_status_ok
        assert visible_header_ok
        status.set_visible(False)
        DZLLWindow._background_prepare_set_status_container(host, False)
        _settle_layout(window)
        assert not status.get_mapped()
        hidden_header_ok, hidden_header_bounds = header.compute_bounds(browser)
        assert hidden_header_ok
        assert hidden_header_bounds.get_y() < visible_header_bounds.get_y()
        assert hidden_header_bounds.get_y() <= visible_status_bounds.get_y()
    finally:
        window.destroy()
