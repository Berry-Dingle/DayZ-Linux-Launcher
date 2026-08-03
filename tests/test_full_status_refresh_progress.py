from __future__ import annotations

from collections import deque
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import config
from dzll_launcher import window as window_module
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
SIDEBAR_SOURCE = ROOT / "src/dzll_launcher/sidebar_ui.py"
WINDOW_SOURCE = ROOT / "src/dzll_launcher/window.py"
STYLES_SOURCE = ROOT / "src/dzll_launcher/styles.py"


class FakeBox:
    def __init__(self):
        self.visible = False
        self.history = []

    def set_visible(self, visible):
        self.visible = bool(visible)
        self.history.append(self.visible)


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.tooltip = None
        self.history = []

    def set_text(self, text):
        self.text = text
        self.history.append(text)

    def set_tooltip_text(self, text):
        self.tooltip = text


class FakeProgressBar:
    def __init__(self):
        self.fraction = 0.0
        self.history = []

    def set_fraction(self, fraction):
        self.fraction = fraction
        self.history.append(fraction)


class FakeStack:
    def __init__(self):
        self.visible_child = None
        self.history = []

    def set_visible_child(self, child):
        self.visible_child = child
        self.history.append(child)


class FakeFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def progress_host(*, total=10, completed=0, generation=1, running=True):
    refresh_btn = FakeBox()
    refresh_btn.visible = True
    progress_box = FakeBox()
    prefix_label = FakeLabel()
    prefix_label.text = "Refreshing:"
    host = SimpleNamespace(
        _status_refresh_generation=generation,
        _status_refresh_running=running,
        _status_refresh_total=total,
        _status_refresh_completed=completed,
        _status_refresh_progress_last_rendered=0,
        _status_refresh_progress_update_id=0,
        _shutdown_cleanup_done=False,
        refresh_status_btn=refresh_btn,
        status_refresh_slot=FakeStack(),
        status_refresh_progress_box=progress_box,
        status_refresh_progress_prefix_label=prefix_label,
        status_refresh_progress_count_label=FakeLabel(),
        status_refresh_progress_bar=FakeProgressBar(),
    )
    return bind(
        host,
        "_cancel_status_refresh_progress_update",
        "_set_status_refresh_progress_visible",
        "_render_status_refresh_progress",
        "_schedule_status_refresh_progress_update",
        "_clear_status_refresh_progress",
    )


def completion_host(*, total=1):
    host = progress_host(total=total)
    host._status_refresh_buffer = []
    host._status_refresh_flush_id = 0
    host._flush_status_refresh_results = lambda _generation: False
    bind(host, "_status_refresh_attempt_finished")
    return host


def test_toolbar_order_has_no_separator_and_keeps_fixed_sidebar_width():
    source = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    body = source[source.index("def build_sidebar_toolbar"):source.index("def build_sidebar(")]

    settings = body.index("search_header.append(settings_btn)")
    mods = body.index("search_header.append(window.mod_manager_header_btn)")
    slot = body.index("search_header.append(window.status_refresh_slot)")
    refresh = body.index('status_refresh_slot.add_named(window.refresh_status_btn, "refresh")')
    progress = body.index('status_refresh_slot.add_named(window.status_refresh_progress_box, "progress")')
    assert settings < mods < refresh < progress < slot
    assert "Gtk.Separator" not in body
    assert '"|"' not in body and "'|'" not in body
    assert config.SIDEBAR_WIDTH == 220
    assert "toolbar_cell.set_size_request(SIDEBAR_WIDTH, -1)" in body


def test_progress_widget_is_compact_expanding_and_hidden_initially():
    source = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    assert "toolbar_cell.set_halign(Gtk.Align.FILL)" in source
    assert "toolbar_cell.set_overflow(Gtk.Overflow.HIDDEN)" in source
    assert "search_header.set_halign(Gtk.Align.FILL)" in source
    assert "search_header.set_overflow(Gtk.Overflow.HIDDEN)" in source
    assert "status_refresh_progress_box.set_hexpand(True)" in source
    assert "status_refresh_progress_box.set_size_request(1, -1)" in source
    assert "status_refresh_progress_box.set_halign(Gtk.Align.FILL)" in source
    assert "status_refresh_progress_box.set_visible(False)" in source
    assert 'Gtk.Label(label="Refreshing:", xalign=0.0)' in source
    assert "status_refresh_progress_prefix_label.set_halign(Gtk.Align.START)" in source
    assert "status_refresh_progress_count_label = Gtk.Label(xalign=1.0)" in source
    assert "status_refresh_progress_count_label.set_hexpand(True)" in source
    assert "status_refresh_progress_count_label.set_halign(Gtk.Align.FILL)" in source
    assert "status_refresh_progress_count_label.set_width_chars(1)" in source
    assert "status_refresh_progress_count_label.set_max_width_chars(1)" in source
    assert "status_refresh_progress_count_label.set_ellipsize(Pango.EllipsizeMode.START)" in source
    assert "status_refresh_progress_bar.set_show_text(False)" in source
    assert "status_refresh_slot.set_hexpand(True)" in source
    assert "status_refresh_slot.set_size_request(1, -1)" in source
    assert "status_refresh_slot.set_hhomogeneous(False)" in source
    assert "status_refresh_slot.set_vhomogeneous(True)" in source
    assert "status_refresh_slot.set_transition_type(Gtk.StackTransitionType.NONE)" in source
    assert "status_refresh_progress_box.append(window.status_refresh_progress_text_row)" in source
    assert source.index("status_refresh_progress_box.append(window.status_refresh_progress_text_row)") < source.index("status_refresh_progress_box.append(window.status_refresh_progress_bar)")


def test_toolbar_spacing_is_tightened_locally_without_global_spacing_changes():
    source = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    body = source[source.index("def build_sidebar_toolbar"):source.index("def build_sidebar(")]
    assert "Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)" in body
    assert "spacing=8" not in body
    assert 'add_css_class("dzll-sidebar-toolbar-row")' in body
    assert "status_refresh_progress_box.set_margin_start(9)" in body
    assert "status_refresh_slot.set_margin_start" not in body
    assert "settings_btn.set_margin" not in body
    assert "mod_manager_header_btn.set_margin" not in body


def test_refresh_and_progress_share_one_slot_without_spinner_or_button_recreation():
    sidebar = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    slot_state = source[source.index("def _set_status_refresh_slot_running"):source.index("def _on_refresh_status_clicked")]
    refresh = sidebar[sidebar.index("window.refresh_status_btn = Gtk.Button()"):
                      sidebar.index("window.status_refresh_progress_box = Gtk.Box")]
    assert "Gtk.Spinner" not in slot_state
    assert "spinner.start" not in slot_state
    assert "spinner.stop" not in slot_state
    assert "set_child" not in slot_state
    assert "slot.set_visible_child(progress)" in slot_state
    assert "slot.set_visible_child(btn)" in slot_state
    assert "refresh_status_btn.set_size_request" not in refresh
    assert "refresh_status_btn.set_child(Gtk.Image.new_from_icon_name" in refresh
    assert "refresh_status_btn.connect(\"clicked\", window._on_refresh_status_clicked)" in refresh


def test_idle_progress_is_hidden_and_reset():
    host = progress_host(completed=7)
    host.status_refresh_progress_box.visible = True
    host.status_refresh_progress_prefix_label.text = "Refreshing:"
    host.status_refresh_progress_count_label.text = "old"
    host.status_refresh_progress_bar.fraction = 0.7

    host._clear_status_refresh_progress(1)

    assert host.status_refresh_progress_box.visible is False
    assert host.status_refresh_progress_prefix_label.text == "Refreshing:"
    assert host.status_refresh_progress_count_label.text == ""
    assert host.status_refresh_progress_count_label.tooltip is None
    assert host.status_refresh_progress_bar.fraction == 0.0
    assert host.status_refresh_slot.visible_child is host.refresh_status_btn


def test_slot_switches_between_original_refresh_and_progress_exclusively():
    host = progress_host()
    host._ensure_status_refresh_cursor_controller = lambda _btn: False
    host._apply_status_refresh_cursor_for_state = lambda _btn: False
    bind(host, "_set_status_refresh_slot_running")

    original_refresh = host.refresh_status_btn
    host._set_status_refresh_slot_running(True, generation=1)
    assert host.status_refresh_slot.visible_child is host.status_refresh_progress_box

    host._set_status_refresh_slot_running(False, generation=1)
    assert host.status_refresh_slot.visible_child is original_refresh
    assert host.refresh_status_btn is original_refresh


def test_manual_sweep_freezes_total_and_renders_initial_formatted_label():
    host = progress_host(total=0, running=False)
    host._obj_by_key = {index: object() for index in range(8_900)}
    host._status_refresh_queue = deque()
    host._status_refresh_inflight = 0
    host._status_refresh_buffer = []
    host._status_refresh_flush_id = 0
    host._status_refresh_started_at = 0.0
    host._status_refresh_last_log_completed = 0
    host._status_refresh_scroll_value = None
    host.scroller = SimpleNamespace(get_vadjustment=lambda: None)
    host._set_status_refresh_slot_running = lambda *_args, **_kwargs: False
    host._pump_status_refresh = lambda _generation: False
    bind(host, "_on_refresh_status_clicked")

    host._on_refresh_status_clicked()
    host._obj_by_key["later"] = object()

    assert host._status_refresh_total == 8_900
    assert len(host._status_refresh_queue) == 8_900
    assert host.status_refresh_progress_count_label.text == "0 / 8,900"
    assert host.status_refresh_progress_bar.fraction == 0.0
    assert host.status_refresh_progress_box.visible is True


def test_zero_target_sweep_never_enters_busy_or_shows_progress():
    host = progress_host(total=0, running=False)
    host._obj_by_key = {}
    slot_calls = []
    host._set_status_refresh_slot_running = lambda running, **_kwargs: slot_calls.append(running)
    bind(host, "_on_refresh_status_clicked")

    host._on_refresh_status_clicked()

    assert slot_calls == []
    assert host._status_refresh_running is False
    assert host.status_refresh_progress_box.visible is False


@pytest.mark.parametrize("classification", ["success", "timeout", "failure", "neutral"])
def test_every_future_terminal_classification_increments_exactly_once(classification):
    host = completion_host()
    host._status_refresh_inflight = 1
    host._pump_status_refresh = lambda _generation: False
    bind(host, "_on_status_refresh_future_done")
    info = {"classification": classification}

    host._on_status_refresh_future_done(1, FakeFuture((1, ("server", info))))

    assert host._status_refresh_completed == 1
    assert host._status_refresh_buffer == [("server", info)]
    assert host._status_refresh_inflight == 0


@pytest.mark.parametrize("terminal_path", ["missing", "invalid", "submit-failure"])
def test_synchronous_terminal_paths_increment_exactly_once(terminal_path):
    host = completion_host()
    host._status_refresh_queue = deque(["server"])
    host._status_refresh_inflight = 0
    host._maybe_finish_status_refresh = lambda _generation: False
    host._query_status_refresh_one = lambda *_args: None
    if terminal_path == "missing":
        host._obj_by_key = {}
        host._startup_live_executor = None
    elif terminal_path == "invalid":
        host._obj_by_key = {"server": SimpleNamespace(ip="127.0.0.1", qport=object())}
        host._startup_live_executor = None
    else:
        host._obj_by_key = {"server": SimpleNamespace(ip="127.0.0.1", qport=2302)}
        host._startup_live_executor = SimpleNamespace(
            submit=lambda *_args: (_ for _ in ()).throw(RuntimeError("submit failed"))
        )
    bind(host, "_pump_status_refresh")

    host._pump_status_refresh(1)

    assert host._status_refresh_completed == 1
    assert not host._status_refresh_queue


def test_authoritative_helper_is_the_only_increment_owner_and_clamps():
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    increment = "self._status_refresh_completed = min(total, completed + 1)"
    assert source.count(increment) == 1
    assert "self._status_refresh_completed +=" not in source
    host = completion_host(total=2)

    assert host._status_refresh_attempt_finished(1) is True
    assert host._status_refresh_attempt_finished(1) is True
    assert host._status_refresh_attempt_finished(1) is True
    assert host._status_refresh_completed == 2


def test_render_is_monotonic_clamped_and_uses_completed_over_total():
    host = progress_host(total=8, completed=3)
    host._render_status_refresh_progress(1)
    assert host.status_refresh_progress_count_label.text == "3 / 8"
    assert host.status_refresh_progress_bar.fraction == pytest.approx(3 / 8)

    host._status_refresh_completed = 1
    host._render_status_refresh_progress(1)
    assert host.status_refresh_progress_count_label.text == "3 / 8"
    host._status_refresh_completed = 99
    host._render_status_refresh_progress(1)
    assert host.status_refresh_progress_count_label.text == "8 / 8"
    assert host.status_refresh_progress_bar.fraction == 1.0


def test_full_progress_text_format_is_not_shortened_or_abbreviated():
    host = progress_host(total=8_484, completed=1_464)

    host._render_status_refresh_progress(1)

    assert host.status_refresh_progress_prefix_label.text == "Refreshing:"
    assert host.status_refresh_progress_count_label.text == "1,464 / 8,484"
    assert host.status_refresh_progress_count_label.tooltip == "Refreshing: 1,464 / 8,484"
    assert " / " in host.status_refresh_progress_count_label.text


def test_initial_and_final_text_keep_exact_thousands_separated_values():
    host = progress_host(total=8_484, completed=0)

    host._render_status_refresh_progress(1)
    assert host.status_refresh_progress_count_label.text == "0 / 8,484"

    host._status_refresh_completed = 8_484
    host._render_status_refresh_progress(1)
    assert host.status_refresh_progress_count_label.text == "8,484 / 8,484"
    assert host.status_refresh_progress_bar.fraction == 1.0


def test_many_completions_schedule_one_coalesced_render(monkeypatch):
    host = completion_host(total=100)
    scheduled = []

    def timeout_add(interval, callback, generation):
        scheduled.append((interval, callback, generation))
        return 41

    monkeypatch.setattr(window_module.GLib, "timeout_add", timeout_add)
    for _ in range(20):
        host._status_refresh_attempt_finished(1)

    assert host._status_refresh_completed == 20
    assert len(scheduled) == 1
    assert scheduled[0][0] == window_module.STATUS_REFRESH_PROGRESS_UPDATE_MS == 125


def test_stale_timer_and_stale_future_do_not_update_or_increment():
    host = completion_host(total=5)
    host._status_refresh_inflight = 1
    host._pump_status_refresh = lambda _generation: False
    bind(host, "_on_status_refresh_future_done")

    host._render_status_refresh_progress(0)
    host._on_status_refresh_future_done(0, FakeFuture((0, ("old", {"ok": True}))))

    assert host._status_refresh_completed == 0
    assert host.status_refresh_progress_count_label.history == []
    assert host._status_refresh_inflight == 1


def test_final_completion_force_renders_total_then_hides_and_resets(monkeypatch):
    host = progress_host(total=2, completed=2)
    host._status_refresh_queue = deque()
    host._status_refresh_inflight = 0
    host._status_refresh_buffer = []
    host._status_refresh_flush_id = 0
    host._status_refresh_progress_update_id = 91
    host.combined_filter = None
    host.sorter = None
    host._status_refresh_scroll_value = None
    host._snapshot_all_sort_keys = lambda: None
    host._debug_browser_reorder = lambda *_args, **_kwargs: None
    host._rebuild_column_view_store = lambda **_kwargs: None
    host._set_status_refresh_slot_running = lambda *_args, **_kwargs: False
    removed = []
    monkeypatch.setattr(window_module.GLib, "source_remove", removed.append)
    bind(host, "_maybe_finish_status_refresh")

    host._maybe_finish_status_refresh(1)

    assert 91 in removed
    assert "2 / 2" in host.status_refresh_progress_count_label.history
    assert host.status_refresh_progress_bar.history[-2:] == [1.0, 0.0]
    assert host.status_refresh_progress_box.visible is False
    assert host._status_refresh_running is False


def test_stale_cleanup_cannot_hide_newer_sweep():
    host = progress_host(generation=2)
    host.status_refresh_progress_box.visible = True
    host.status_refresh_progress_count_label.text = "1 / 2"

    host._clear_status_refresh_progress(1)

    assert host.status_refresh_progress_box.visible is True
    assert host.status_refresh_progress_count_label.text == "1 / 2"


def test_shutdown_clears_timer_and_blocks_later_updates(monkeypatch):
    host = progress_host(total=4, completed=2)
    host._status_refresh_progress_update_id = 52
    removed = []
    monkeypatch.setattr(window_module.GLib, "source_remove", removed.append)

    host._clear_status_refresh_progress(1)
    host._shutdown_cleanup_done = True
    host._status_refresh_completed = 3
    host._render_status_refresh_progress(1)

    assert removed == [52]
    assert host._status_refresh_progress_update_id == 0
    assert host.status_refresh_progress_box.visible is False
    assert host.status_refresh_progress_count_label.text == ""


def test_repeated_click_while_running_is_ignored():
    host = progress_host(running=True)
    host._obj_by_key = {"new": object()}
    host._status_refresh_queue = deque(["existing"])
    bind(host, "_on_refresh_status_clicked")

    host._on_refresh_status_clicked()

    assert list(host._status_refresh_queue) == ["existing"]
    assert host._status_refresh_generation == 1


def test_progress_is_manual_sweep_only_and_no_manual_spinner_path_remains():
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    manual = source[source.index("def _on_refresh_status_clicked"):source.index("def _query_status_refresh_one")]
    row_start = source.index("def _refresh_server_for_obj")
    row = source[row_start:source.index("\n    def ", row_start + 1)]
    startup = source[source.index("def _submit_startup_rest_batches"):source.index("def _apply_status_refresh_cursor_for_state")]
    assert "_render_status_refresh_progress" in manual
    assert "_render_status_refresh_progress" not in row
    assert "_render_status_refresh_progress" not in startup

    slot_state = source[source.index("def _set_status_refresh_slot_running"):source.index("def _on_refresh_status_clicked")]
    assert "Gtk.Spinner" not in slot_state
    assert "spinner" not in slot_state


def test_progress_css_is_narrowly_scoped_and_thin():
    source = STYLES_SOURCE.read_text(encoding="utf-8")
    assert ".dzll-app-root .status-refresh-progress-label" in source
    assert "font-size: 10px" in source
    assert ".dzll-app-root .status-refresh-progress-bar trough" in source
    assert ".dzll-app-root .status-refresh-progress-bar progress" in source
    assert "min-height: 3px" in source
    assert "progressbar.status-refresh-progress-bar" not in source
