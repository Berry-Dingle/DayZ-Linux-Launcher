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


class FakeButton(FakeBox):
    def __init__(self):
        super().__init__()
        self.visible = True
        self.tooltip = "Refresh all server status"

    def get_tooltip_text(self):
        return self.tooltip

    def set_tooltip_text(self, text):
        self.tooltip = text


class FakeIcon:
    def __init__(self):
        self.classes = set()

    def has_css_class(self, name):
        return name in self.classes

    def add_css_class(self, name):
        self.classes.add(name)

    def remove_css_class(self, name):
        self.classes.discard(name)


class FakeFuture:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


def progress_host(*, total=10, completed=0, generation=1, running=True):
    """A controller with fake sidebar widgets. ``host.c`` is the controller."""
    prefix_label = FakeLabel()
    prefix_label.text = "Refreshing:"
    host = SimpleNamespace(
        refresh_status_btn=FakeButton(),
        refresh_status_icon=FakeIcon(),
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
            icon=host.refresh_status_icon,
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

    column = body.index("search_header.append(toolbar_icons_column)")
    settings = body.index("toolbar_icons_row.append(settings_btn)")
    mods = body.index("toolbar_icons_row.append(window.mod_manager_header_btn)")
    companion = body.index("toolbar_icons_row.append(window.server_companion_toggle_btn)")
    refresh = body.index("toolbar_icons_row.append(window.refresh_status_btn)")
    progress = body.index("toolbar_icons_column.append(window.status_refresh_progress_box)")
    assert column < settings < mods < companion < refresh < progress
    assert "Gtk.Stack" not in body
    assert "Gtk.Separator" not in body
    assert '"|"' not in body and "'|'" not in body
    assert config.SIDEBAR_WIDTH == 220
    assert "toolbar_cell.set_size_request(SIDEBAR_WIDTH, -1)" in body


def test_progress_widget_sits_under_icons_at_their_width_and_is_hidden_initially():
    source = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    assert "toolbar_cell.set_halign(Gtk.Align.FILL)" in source
    assert "toolbar_cell.set_overflow(Gtk.Overflow.HIDDEN)" in source
    assert "search_header.set_halign(Gtk.Align.FILL)" in source
    assert "search_header.set_overflow(Gtk.Overflow.HIDDEN)" in source
    assert "toolbar_icons_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)" in source
    assert "toolbar_icons_column.set_hexpand(False)" in source
    assert "toolbar_icons_column.set_halign(Gtk.Align.START)" in source
    assert "status_refresh_progress_box.set_hexpand(False)" in source
    assert "status_refresh_progress_box.set_size_request(1, -1)" in source
    assert "status_refresh_progress_box.set_halign(Gtk.Align.FILL)" in source
    assert "status_refresh_progress_box.set_visible(False)" in source
    assert 'Gtk.Label(label="Refreshing:", xalign=0.0)' in source
    assert "status_refresh_progress_prefix_label.set_halign(Gtk.Align.START)" in source
    assert "status_refresh_progress_count_label = Gtk.Label(xalign=0.0)" in source
    assert "status_refresh_progress_count_label.set_hexpand(True)" in source
    assert "status_refresh_progress_count_label.set_halign(Gtk.Align.FILL)" in source
    # The count label must never widen the icon column.
    assert "status_refresh_progress_count_label.set_width_chars(1)" in source
    assert "status_refresh_progress_count_label.set_max_width_chars(1)" in source
    assert "status_refresh_progress_count_label.set_ellipsize(Pango.EllipsizeMode.START)" in source
    assert "status_refresh_progress_bar.set_show_text(False)" in source
    assert "status_refresh_progress_box.append(window.status_refresh_progress_text_row)" in source
    assert source.index("status_refresh_progress_box.append(window.status_refresh_progress_text_row)") < source.index("status_refresh_progress_box.append(window.status_refresh_progress_bar)")


def test_toolbar_spacing_is_tightened_locally_without_global_spacing_changes():
    source = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    body = source[source.index("def build_sidebar_toolbar"):source.index("def build_sidebar(")]
    assert "Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)" in body
    assert "spacing=8" not in body
    assert 'add_css_class("dzll-sidebar-toolbar-row")' in body
    assert "status_refresh_progress_box.set_margin_start" not in body
    assert "settings_btn.set_margin" not in body
    assert "mod_manager_header_btn.set_margin" not in body


def test_refresh_button_spins_its_own_icon_and_toggles_the_sweep():
    sidebar = SIDEBAR_SOURCE.read_text(encoding="utf-8")
    source = CONTROLLER_SOURCE.read_text(encoding="utf-8")
    running_state = source[source.index("def _set_slot_running"):source.index("def _attempt_finished")]
    refresh = sidebar[sidebar.index("window.refresh_status_btn = Gtk.Button()"):
                      sidebar.index("window.status_refresh_progress_box = Gtk.Box")]
    assert "Gtk.Spinner" not in running_state
    assert "Gtk.Spinner" not in refresh
    assert "set_child" not in running_state
    assert "add_css_class(STATUS_REFRESH_SPINNING_CLASS)" in running_state
    assert "remove_css_class(STATUS_REFRESH_SPINNING_CLASS)" in running_state
    assert "refresh_status_btn.set_size_request" not in refresh
    assert 'window.refresh_status_icon = _sidebar_toolbar_icon("refreshIcon.svg")' in refresh
    assert "refresh_status_btn.set_child(window.refresh_status_icon)" in refresh
    assert 'refresh_status_btn.connect("clicked", window._status_refresh.toggle)' in refresh


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


def test_running_spins_the_original_icon_and_restores_it_with_tooltip():
    host = progress_host()
    host.c._ensure_cursor_controller = lambda _btn: False
    host.c._apply_cursor_for_state = lambda _btn: False
    spinning = status_refresh_controller.STATUS_REFRESH_SPINNING_CLASS

    original_refresh = host.refresh_status_btn
    original_icon = host.refresh_status_icon
    host.c._set_slot_running(True, generation=1)
    assert spinning in host.refresh_status_icon.classes
    assert host.refresh_status_btn.tooltip == "Stop refreshing"
    assert host.status_refresh_progress_box.visible is True

    # A second running call must not capture "Stop refreshing" as the idle tooltip.
    host.c._set_slot_running(True, generation=1)

    host.c._set_slot_running(False, generation=1)
    assert spinning not in host.refresh_status_icon.classes
    assert host.refresh_status_btn.tooltip == "Refresh all server status"
    assert host.refresh_status_btn is original_refresh
    assert host.refresh_status_icon is original_icon


def test_manual_sweep_freezes_total_and_renders_initial_formatted_label():
    host = progress_host(total=0, running=False)
    host.servers = {index: object() for index in range(8_900)}
    host.c._set_slot_running = lambda *_args, **_kwargs: False
    host.c._pump = lambda _generation: False

    host.c.start()
    host.servers["later"] = object()

    assert host.c.total == 8_900
    assert len(host.c.queue) == 8_900
    assert host.status_refresh_progress_count_label.text == "0/8900"
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
    assert host.status_refresh_progress_count_label.text == "3/8"
    assert host.status_refresh_progress_bar.fraction == pytest.approx(3 / 8)

    host.c.completed = 1
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "3/8"
    host.c.completed = 99
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "8/8"
    assert host.status_refresh_progress_bar.fraction == 1.0


def test_full_progress_text_format_is_not_shortened_or_abbreviated():
    host = progress_host(total=11_090, completed=1_256)

    host.c._render_progress(1)

    assert host.status_refresh_progress_prefix_label.text == "Refreshing:"
    assert host.status_refresh_progress_count_label.text == "1256/11090"
    assert host.status_refresh_progress_count_label.tooltip == "Refreshing: 1256/11090"


def test_initial_and_final_text_keep_exact_unseparated_values():
    host = progress_host(total=8_484, completed=0)

    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "0/8484"

    host.c.completed = 8_484
    host.c._render_progress(1)
    assert host.status_refresh_progress_count_label.text == "8484/8484"
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
    assert "2/2" in host.status_refresh_progress_count_label.history
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


def test_repeated_start_while_running_is_ignored():
    host = progress_host(running=True)
    host.servers = {"new": object()}
    host.c.queue = deque(["existing"])

    host.c.start()

    assert list(host.c.queue) == ["existing"]
    assert host.c.generation == 1


def test_toggle_starts_when_idle_and_stops_when_running():
    host = progress_host(running=False)
    calls = []
    host.c.start = lambda: calls.append("start")
    host.c.stop = lambda: calls.append("stop")

    host.c.toggle()
    host.c.running = True
    host.c.toggle()

    assert calls == ["start", "stop"]


def test_stop_applies_buffered_results_finishes_and_ignores_late_futures(monkeypatch):
    host = progress_host(total=10, completed=3)
    host.c.queue = deque(["a", "b", "c"])
    host.c.inflight = 2
    host.c.buffer = [("s1", {"ok": True})]
    host.c.flush_id = 77
    host.c.progress_update_id = 78
    host.c._ensure_cursor_controller = lambda _btn: False
    host.c._apply_cursor_for_state = lambda _btn: False
    host.c._set_slot_running(True, generation=1)
    removed = []
    monkeypatch.setattr(status_refresh_controller.GLib, "source_remove", removed.append)
    monkeypatch.setattr(status_refresh_controller.GLib, "idle_add", lambda *_args: 0)

    host.c.stop()

    assert 77 in removed and 78 in removed
    assert host.applied == [([("s1", {"ok": True})], {"reason": "manual-status-refresh"})]
    assert host.browser_refreshes == [True]
    assert host.c.running is False
    assert not host.c.queue
    assert host.c.buffer == []
    assert host.c.inflight == 0
    assert host.c.generation == 2
    assert host.status_refresh_progress_box.visible is False
    assert status_refresh_controller.STATUS_REFRESH_SPINNING_CLASS not in host.refresh_status_icon.classes

    # A query that was in flight when the sweep stopped returns later.
    host.c._pump = lambda _generation: False
    host.c._on_future_done(1, FakeFuture((1, ("late", {"ok": True}))))
    assert host.c.completed == 3
    assert host.c.buffer == []
    assert len(host.applied) == 1


def test_stop_when_idle_does_nothing():
    host = progress_host(running=False)

    host.c.stop()

    assert host.c.generation == 1
    assert host.browser_refreshes == []


def test_progress_shows_manual_and_startup_sweeps_and_no_manual_spinner_path_remains():
    controller = CONTROLLER_SOURCE.read_text(encoding="utf-8")
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    manual = controller[controller.index("    def start(self"):controller.index("    def _query_one")]
    row_start = source.index("def _refresh_server_for_obj")
    row = source[row_start:source.index("\n    def ", row_start + 1)]
    startup = source[source.index("def _submit_startup_rest_batches"):source.index("def _refresh_browser_after_status_refresh")]
    assert "_render_progress" in manual
    assert "_status_refresh" not in row
    # The startup sweep only *displays* its progress through the controller's
    # external-sweep API; it never runs a manual sweep.
    assert "controller.show_external_progress(" in startup
    assert "controller.end_external_progress()" in startup
    assert "_status_refresh.start" not in startup
    assert "_status_refresh.toggle" not in startup

    running_state = controller[controller.index("def _set_slot_running"):controller.index("def _attempt_finished")]
    assert "Gtk.Spinner" not in running_state
    assert "spinner" not in running_state


def test_progress_css_is_narrowly_scoped_and_thin():
    source = STYLES_SOURCE.read_text(encoding="utf-8")
    assert ".dzll-app-root .status-refresh-progress-label" in source
    assert "font-size: 10px" in source
    assert ".dzll-app-root .status-refresh-progress-bar trough" in source
    assert ".dzll-app-root .status-refresh-progress-bar progress" in source
    assert "min-height: 3px" in source
    assert "progressbar.status-refresh-progress-bar" not in source


def test_refresh_spin_css_is_scoped_and_turns_counter_clockwise():
    source = STYLES_SOURCE.read_text(encoding="utf-8")
    assert "@keyframes dzll-status-refresh-spin" in source
    assert "to {{ -gtk-icon-transform: rotate(-1turn); }}" in source
    assert ".dzll-app-root image.status-refresh-spinning {{" in source
    assert "animation: dzll-status-refresh-spin 1s linear infinite;" in source
