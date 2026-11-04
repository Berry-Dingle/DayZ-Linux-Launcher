from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import config
from dzll_launcher import status_refresh_controller
from dzll_launcher.status_refresh_controller import StatusRefreshController, StatusRefreshWidgets


ROOT = Path(__file__).resolve().parents[1]
SIDEBAR_SOURCE = ROOT / "src/dzll_launcher/sidebar_ui.py"
WINDOW_SOURCE = ROOT / "src/dzll_launcher/window.py"
CONTROLLER_SOURCE = ROOT / "src/dzll_launcher/status_refresh_controller.py"
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


def progress_host(*, total=10, completed=0, generation=1, running=True):
    """A controller with fake sidebar widgets. ``host.c`` is the controller."""
    refresh_btn = FakeBox()
    refresh_btn.visible = True
    prefix_label = FakeLabel()
    prefix_label.text = "Refreshing:"
    host = SimpleNamespace(
        refresh_status_btn=refresh_btn,
        status_refresh_slot=FakeStack(),
        status_refresh_progress_box=FakeBox(),
        status_refresh_progress_prefix_label=prefix_label,
        status_refresh_progress_count_label=FakeLabel(),
        status_refresh_progress_bar=FakeProgressBar(),
        shutdown=False,
        servers={},
        scroller=SimpleNamespace(get_vadjustment=lambda: None),
        executor=None,
        applied=[],
        browser_refreshes=[],
    )
    host.c = StatusRefreshController(
        widgets=lambda: StatusRefreshWidgets(
            button=host.refresh_status_btn,
            progress_box=host.status_refresh_progress_box,
            count_label=host.status_refresh_progress_count_label,
            progress_bar=host.status_refresh_progress_bar,
            slot=host.status_refresh_slot,
        ),
        server_keys=lambda: list(host.servers),
        lookup_server=lambda key: host.servers.get(key),
        submit=lambda *args: host.executor.submit(*args),
        apply_live_results=lambda results, **kwargs: host.applied.append((results, kwargs)),
        refresh_browser=lambda: host.browser_refreshes.append(True),
        scroller=lambda: host.scroller,
        model_id=lambda: 0,
        is_shut_down=lambda: host.shutdown,
    )
    host.c.generation = generation
    host.c.running = running
    host.c.total = total
    host.c.completed = completed
    return host


def completion_host(*, total=1):
    host = progress_host(total=total)
    host.c._flush_results = lambda _generation: False
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
    source = CONTROLLER_SOURCE.read_text(encoding="utf-8")
    slot_state = source[source.index("def _set_slot_running"):source.index("def _attempt_finished")]
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
    assert "refresh_status_btn.connect(\"clicked\", window._status_refresh.start)" in refresh


def test_idle_progress_is_hidden_and_reset():
    host = progress_host(completed=7)
    host.status_refresh_progress_box.visible = True
    host.status_refresh_progress_prefix_label.text = "Refreshing:"
    host.status_refresh_progress_count_label.text = "old"
    host.status_refresh_progress_bar.fraction = 0.7

    host.c._clear_progress(1)

    assert host.status_refresh_progress_box.visible is False
    assert host.status_refresh_progress_prefix_label.text == "Refreshing:"
    assert host.status_refresh_progress_count_label.text == ""
    assert host.status_refresh_progress_count_label.tooltip is None
    assert host.status_refresh_progress_bar.fraction == 0.0
    assert host.status_refresh_slot.visible_child is host.refresh_status_btn


def test_slot_switches_between_original_refresh_and_progress_exclusively():
    host = progress_host()
    host.c._ensure_cursor_controller = lambda _btn: False
    host.c._apply_cursor_for_state = lambda _btn: False

    original_refresh = host.refresh_status_btn
    host.c._set_slot_running(True, generation=1)
    assert host.status_refresh_slot.visible_child is host.status_refresh_progress_box

    host.c._set_slot_running(False, generation=1)
    assert host.status_refresh_slot.visible_child is original_refresh
    assert host.refresh_status_btn is original_refresh


def test_manual_sweep_freezes_total_and_renders_initial_formatted_label():
    host = progress_host(total=0, running=False)
    host.servers = {index: object() for index in range(8_900)}
    host.c._set_slot_running = lambda *_args, **_kwargs: False
    host.c._pump = lambda _generation: False

    host.c.start()
    host.servers["later"] = object()

    assert host.c.total == 8_900
    assert len(host.c.queue) == 8_900
    assert host.status_refresh_progress_count_label.text == "0 / 8,900"
    assert host.status_refresh_progress_bar.fraction == 0.0
    assert host.status_refresh_progress_box.visible is True


def test_zero_target_sweep_never_enters_busy_or_shows_progress():
    host = progress_host(total=0, running=False)
    host.servers = {}
    slot_calls = []
    host.c._set_slot_running = lambda running, **_kwargs: slot_calls.append(running)

    host.c.start()

    assert slot_calls == []
    assert host.c.running is False
    assert host.status_refresh_progress_box.visible is False


@pytest.mark.parametrize("classification", ["success", "timeout", "failure", "neutral"])
def test_every_future_terminal_classification_increments_exactly_once(classification):
    host = completion_host()
    host.c.inflight = 1
    host.c._pump = lambda _generation: False
    info = {"classification": classification}

    host.c._on_future_done(1, FakeFuture((1, ("server", info))))

    assert host.c.completed == 1
    assert host.c.buffer == [("server", info)]
    assert host.c.inflight == 0


@pytest.mark.parametrize("terminal_path", ["missing", "invalid", "submit-failure"])
def test_synchronous_terminal_paths_increment_exactly_once(terminal_path):
    host = completion_host()
    host.c.queue = deque(["server"])
    host.c.inflight = 0
    host.c._maybe_finish = lambda _generation: False
    host.c._query_one = lambda *_args: None
    if terminal_path == "missing":
        host.servers = {}
        host.executor = None
    elif terminal_path == "invalid":
        host.servers = {"server": SimpleNamespace(ip="127.0.0.1", qport=object())}
        host.executor = None
    else:
        host.servers = {"server": SimpleNamespace(ip="127.0.0.1", qport=2302)}
        host.executor = SimpleNamespace(
            submit=lambda *_args: (_ for _ in ()).throw(RuntimeError("submit failed"))
        )

    host.c._pump(1)

    assert host.c.completed == 1
    assert not host.c.queue
    assert host.c.inflight == 0


def test_authoritative_helper_is_the_only_increment_owner_and_clamps():
    source = CONTROLLER_SOURCE.read_text(encoding="utf-8")
    increment = "self.completed = min(total, completed + 1)"
    assert source.count(increment) == 1
    assert "self.completed +=" not in source
    assert re.findall(r"self\._status_refresh_\w+", WINDOW_SOURCE.read_text(encoding="utf-8")) == []
    host = completion_host(total=2)

    assert host.c._attempt_finished(1) is True
    assert host.c._attempt_finished(1) is True
    assert host.c._attempt_finished(1) is True
    assert host.c.completed == 2


def test_render_is_monotonic_clamped_and_uses_completed_over_total():
    host = progress_host(total=8, completed=3)
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "3 / 8"
    assert host.status_refresh_progress_bar.fraction == pytest.approx(3 / 8)

    host.c.completed = 1
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "3 / 8"
    host.c.completed = 99
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "8 / 8"
    assert host.status_refresh_progress_bar.fraction == 1.0


def test_full_progress_text_format_is_not_shortened_or_abbreviated():
    host = progress_host(total=8_484, completed=1_464)

    host.c._render_progress(1)

    assert host.status_refresh_progress_prefix_label.text == "Refreshing:"
    assert host.status_refresh_progress_count_label.text == "1,464 / 8,484"
    assert host.status_refresh_progress_count_label.tooltip == "Refreshing: 1,464 / 8,484"
    assert " / " in host.status_refresh_progress_count_label.text


def test_initial_and_final_text_keep_exact_thousands_separated_values():
    host = progress_host(total=8_484, completed=0)

    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "0 / 8,484"

    host.c.completed = 8_484
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "8,484 / 8,484"
    assert host.status_refresh_progress_bar.fraction == 1.0


def test_many_completions_schedule_one_coalesced_render(monkeypatch):
    host = completion_host(total=100)
    scheduled = []

    def timeout_add(interval, callback, generation):
        scheduled.append((interval, callback, generation))
        return 41

    monkeypatch.setattr(status_refresh_controller.GLib, "timeout_add", timeout_add)
    for _ in range(20):
        host.c._attempt_finished(1)

    assert host.c.completed == 20
    assert len(scheduled) == 1
    assert scheduled[0][0] == status_refresh_controller.STATUS_REFRESH_PROGRESS_UPDATE_MS == 125
    assert scheduled[0][1:] == (host.c._render_progress, 1)


def test_stale_timer_and_stale_future_do_not_update_or_increment():
    host = completion_host(total=5)
    host.c.inflight = 1
    host.c._pump = lambda _generation: False

    host.c._render_progress(0)
    host.c._on_future_done(0, FakeFuture((0, ("old", {"ok": True}))))

    assert host.c.completed == 0
    assert host.status_refresh_progress_count_label.history == []
    assert host.c.inflight == 1


def test_final_completion_force_renders_total_then_hides_and_resets(monkeypatch):
    host = progress_host(total=2, completed=2)
    host.c.progress_update_id = 91
    host.c._set_slot_running = lambda *_args, **_kwargs: False
    removed = []
    monkeypatch.setattr(status_refresh_controller.GLib, "source_remove", removed.append)

    host.c._maybe_finish(1)

    assert 91 in removed
    assert "2 / 2" in host.status_refresh_progress_count_label.history
    assert host.status_refresh_progress_bar.history[-2:] == [1.0, 0.0]
    assert host.status_refresh_progress_box.visible is False
    assert host.c.running is False
    assert host.browser_refreshes == [True]


def test_stale_cleanup_cannot_hide_newer_sweep():
    host = progress_host(generation=2)
    host.status_refresh_progress_box.visible = True
    host.status_refresh_progress_count_label.text = "1 / 2"

    host.c._clear_progress(1)

    assert host.status_refresh_progress_box.visible is True
    assert host.status_refresh_progress_count_label.text == "1 / 2"


def test_shutdown_clears_timer_and_blocks_later_updates(monkeypatch):
    host = progress_host(total=4, completed=2)
    host.c.progress_update_id = 52
    removed = []
    monkeypatch.setattr(status_refresh_controller.GLib, "source_remove", removed.append)

    host.c._clear_progress(1)
    host.shutdown = True
    host.c.completed = 3
    host.c._render_progress(1)

    assert removed == [52]
    assert host.c.progress_update_id == 0
    assert host.status_refresh_progress_box.visible is False
    assert host.status_refresh_progress_count_label.text == ""


def test_repeated_click_while_running_is_ignored():
    host = progress_host(running=True)
    host.servers = {"new": object()}
    host.c.queue = deque(["existing"])

    host.c.start()

    assert list(host.c.queue) == ["existing"]
    assert host.c.generation == 1


def test_progress_is_manual_sweep_only_and_no_manual_spinner_path_remains():
    controller = CONTROLLER_SOURCE.read_text(encoding="utf-8")
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    manual = controller[controller.index("    def start(self"):controller.index("    def _query_one")]
    row_start = source.index("def _refresh_server_for_obj")
    row = source[row_start:source.index("\n    def ", row_start + 1)]
    startup = source[source.index("def _submit_startup_rest_batches"):source.index("def _refresh_browser_after_status_refresh")]
    assert "_render_progress" in manual
    assert "_status_refresh" not in row
    assert "_status_refresh" not in startup

    slot_state = controller[controller.index("def _set_slot_running"):controller.index("def _attempt_finished")]
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
