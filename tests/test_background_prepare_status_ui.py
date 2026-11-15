"""Stage 7c: BackgroundPrepareStatusUI presentation and window boundary."""
from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
)
from dzll_launcher.background_prepare_queue import BackgroundPreparationQueue
from dzll_launcher.background_prepare_status_ui import (
    BACKGROUND_PREPARE_ACTIVE_BOTTOM_SPACING,
    BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET,
    BackgroundPrepareStatusUI,
)
from dzll_launcher.preparation_contracts import PreparationOutcome, PreparationStatus
from dzll_launcher.preparation_presentation import PreparationProgressMode
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")

WIDGET_NAMES = (
    "background_prepare_status",
    "background_prepare_server_label",
    "background_prepare_detail_label",
    "background_prepare_queue_divider",
    "background_prepare_queue_label",
    "background_prepare_failed_label",
    "background_prepare_retry_btn",
    "background_prepare_count_label",
    "background_prepare_percent_label",
    "background_prepare_right_status_label",
    "background_prepare_action_btn",
)


class RecordingWidget:
    """Fake widget that appends every write to a shared, ordered event log."""

    def __init__(self, name, events):
        self._name = name
        self._events = events
        self.text = ""
        self.visible = False
        self.sensitive = True
        self.opacity = 1.0
        self.tooltip = None
        self.css_classes = set()
        self.margins = {}

    def _record(self, op, value):
        self._events.append((self._name, op, value))

    def set_text(self, value):
        self.text = str(value)
        self._record("text", self.text)

    def set_label(self, value):
        self.text = str(value)
        self._record("label", self.text)

    def set_visible(self, value):
        self.visible = bool(value)
        self._record("visible", self.visible)

    def set_sensitive(self, value):
        self.sensitive = bool(value)
        self._record("sensitive", self.sensitive)

    def set_opacity(self, value):
        self.opacity = float(value)
        self._record("opacity", self.opacity)

    def set_tooltip_text(self, value):
        self.tooltip = value
        self._record("tooltip", value)

    def add_css_class(self, value):
        self.css_classes.add(value)
        self._record("add_css", value)

    def remove_css_class(self, value):
        self.css_classes.discard(value)
        self._record("remove_css", value)

    def set_margin_start(self, value):
        self.margins["start"] = int(value)
        self._record("margin_start", int(value))

    def set_margin_end(self, value):
        self.margins["end"] = int(value)
        self._record("margin_end", int(value))

    def set_margin_bottom(self, value):
        self.margins["bottom"] = int(value)
        self._record("margin_bottom", int(value))


def _recording_ui(events=None, win=None):
    events = [] if events is None else events
    ui = BackgroundPrepareStatusUI(win)
    for name in WIDGET_NAMES:
        setattr(ui, name, RecordingWidget(name.removeprefix("background_prepare_"), events))
    return ui, events


def _window_host(queue=None):
    events = []
    host = SimpleNamespace(
        _background_prepare_queue=queue or BackgroundPreparationQueue(),
        _background_prepare_active=False,
        _background_prepare_controller=None,
        _background_prepare_ui_generation=3,
        _background_prepare_snapshot=object(),
        _background_prepare_cancel_requested=True,
        _background_prepare_terminal_handled=True,
        _refresh_background_prepare_action_states=lambda: events.append(
            ("window", "refresh_action_states", None)
        ),
        _background_prepare_is_current=lambda generation: generation == 7,
    )
    ui, _ = _recording_ui(events, host)
    for name in WIDGET_NAMES:
        setattr(host, name, getattr(ui, name))
    host._background_prepare_status_ui = ui
    host._steam_ugc_format_size = lambda value: DZLLWindow._steam_ugc_format_size(host, value)
    host._background_prepare_render_batch_summary = (
        lambda batch: DZLLWindow._background_prepare_render_batch_summary(host, batch)
    )
    host._background_prepare_close = lambda: DZLLWindow._background_prepare_close(host)
    host._background_prepare_apply_queue_snapshot = (
        lambda snapshot=None: DZLLWindow._background_prepare_apply_queue_snapshot(host, snapshot)
    )
    return host, ui, events


def _server(ip, name):
    return ServerObject(ip=ip, gport=2302, qport=27016, name=name, mods_json="[]")


def _snapshot(server):
    return BackgroundServerPreparationSnapshot(
        server.ip, server.gport, server.qport, server.name, ((101, "One"),),
    )


def _runtime():
    return BackgroundPreparationRuntime("/workshop", True, True)


def _progress(mode, fraction=None, *, name="", stage="Checking…", current=0, total=0):
    return SimpleNamespace(
        current_mod_name=name,
        stage_text=stage,
        item_total_bytes=0,
        current=current,
        total=total,
        progress_mode=mode,
        fraction=fraction,
    )


# --- Status changes -------------------------------------------------------


def test_active_presentation_writes_container_labels_and_action_in_original_order():
    ui, events = _recording_ui()

    ui.set_active_presentation(True, server_name="Alpha", queued_count=2)

    inset = BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET
    bottom = BACKGROUND_PREPARE_ACTIVE_BOTTOM_SPACING
    assert (inset, bottom) == (10, 8)
    assert events == [
        ("status", "add_css", "dzll-background-prepare-present"),
        ("status", "add_css", "dzll-background-prepare-active"),
        ("status", "margin_start", inset),
        ("status", "margin_end", inset),
        ("status", "margin_bottom", bottom),
        ("server_label", "text", "Alpha"),
        ("server_label", "tooltip", "Alpha"),
        ("queue_label", "text", "Queued Servers: 2"),
        ("queue_label", "visible", True),
        ("queue_divider", "visible", True),
        ("right_status_label", "visible", False),
        ("action_btn", "visible", True),
        ("action_btn", "opacity", 1.0),
        ("action_btn", "sensitive", True),
    ]


def test_active_presentation_defaults_server_name_and_hides_empty_queue():
    ui, _ = _recording_ui()
    ui.set_active_presentation(True)
    assert ui.background_prepare_server_label.text == "Server"
    assert ui.background_prepare_server_label.tooltip == "Server"
    assert ui.background_prepare_queue_label.text == ""
    assert not ui.background_prepare_queue_label.visible
    assert not ui.background_prepare_queue_divider.visible


def test_inactive_presentation_clears_container_and_optional_blocks_only():
    ui, events = _recording_ui()
    ui.set_active_presentation(True, server_name="Alpha", queued_count=1)
    events.clear()

    ui.set_active_presentation(False)

    assert events == [
        ("status", "remove_css", "dzll-background-prepare-present"),
        ("status", "remove_css", "dzll-background-prepare-active"),
        ("status", "margin_start", 0),
        ("status", "margin_end", 0),
        ("status", "margin_bottom", 0),
        ("queue_label", "text", ""),
        ("queue_label", "visible", False),
        ("queue_divider", "visible", False),
        ("right_status_label", "visible", False),
    ]
    # The action button and server label are deliberately left untouched.
    assert ui.background_prepare_server_label.text == "Alpha"


def test_present_without_active_keeps_active_class_off():
    ui, _ = _recording_ui()
    ui.set_status_container(True)
    classes = ui.background_prepare_status.css_classes
    assert "dzll-background-prepare-present" in classes
    assert "dzll-background-prepare-active" not in classes


def test_partial_ui_without_optional_widgets_matches_old_defensive_behaviour():
    ui, _ = _recording_ui()
    ui.background_prepare_queue_label = None
    ui.background_prepare_queue_divider = None
    ui.background_prepare_right_status_label = None
    ui.set_active_presentation(True, server_name="Alpha", queued_count=3)
    ui.set_active_presentation(False)
    assert ui.background_prepare_server_label.text == "Alpha"


# --- Preparation progress -------------------------------------------------


@pytest.mark.parametrize(
    ("fraction", "percent"),
    ((0.0, 0), (0.005, 0), (0.42, 42), (0.999, 99), (1.0, 100), (1.7, 100), (-0.3, 0)),
)
def test_progress_class_is_clamped_and_replaces_the_previous_class(fraction, percent):
    ui, events = _recording_ui()
    ui.set_progress_presentation(True, 0.5)
    events.clear()

    ui.set_progress_presentation(True, fraction)

    assert events == [
        ("status", "remove_css", "dzll-background-prepare-progress-50"),
        ("status", "add_css", f"dzll-background-prepare-progress-{percent}"),
    ]
    root = ui.background_prepare_status
    assert root._dzll_background_prepare_progress_class == (
        f"dzll-background-prepare-progress-{percent}"
    )


def test_inactive_progress_clears_class_and_percent_text():
    ui, events = _recording_ui()
    ui.set_progress_presentation(True, 0.25)
    events.clear()

    ui.set_progress_presentation(False)

    assert events == [
        ("status", "remove_css", "dzll-background-prepare-progress-25"),
        ("percent_label", "text", ""),
    ]
    assert ui.background_prepare_status._dzll_background_prepare_progress_class == ""
    events.clear()
    ui.set_progress_presentation(False)
    assert events == [("percent_label", "text", "")]


def test_window_snapshot_render_delegates_progress_to_the_strip():
    host, ui, events = _window_host()
    DZLLWindow._background_prepare_render_snapshot(
        host, 7, _progress(PreparationProgressMode.DETERMINATE, 0.37, name="Mod", current=2, total=5),
    )
    assert ui.background_prepare_detail_label.text == "Downloading Mod: Mod"
    assert ui.background_prepare_count_label.text == "2/5"
    assert "dzll-background-prepare-progress-37" in ui.background_prepare_status.css_classes
    assert ui.background_prepare_percent_label.text == "37%"

    DZLLWindow._background_prepare_render_snapshot(
        host, 7, _progress(PreparationProgressMode.INDETERMINATE, stage="Waiting for Steam…"),
    )
    assert ui.background_prepare_detail_label.text == "Waiting for Steam…"
    assert not any(
        name.startswith("dzll-background-prepare-progress-")
        for name in ui.background_prepare_status.css_classes
    )
    assert ui.background_prepare_percent_label.text == ""

    events.clear()
    DZLLWindow._background_prepare_render_snapshot(
        host, 6, _progress(PreparationProgressMode.DETERMINATE, 0.9),
    )
    assert events == []  # Stale generation: the window gate still rejects it.


# --- Cancellation ---------------------------------------------------------


def test_present_cancelling_write_sequence_is_unchanged():
    ui, events = _recording_ui()
    ui.set_progress_presentation(True, 0.6)
    events.clear()

    ui.present_cancelling()

    assert events == [
        ("status", "add_css", "dzll-background-prepare-present"),
        ("status", "remove_css", "dzll-background-prepare-active"),
        ("status", "margin_start", 10),
        ("status", "margin_end", 10),
        ("status", "margin_bottom", 8),
        ("server_label", "text", "Cancelling background mod preparation…"),
        ("detail_label", "text", "Waiting for active Steam cleanup to finish."),
        ("queue_label", "text", ""),
        ("queue_label", "visible", False),
        ("queue_divider", "visible", False),
        ("count_label", "text", ""),
        ("count_label", "visible", False),
        ("retry_btn", "visible", False),
        ("failed_label", "visible", False),
        ("action_btn", "visible", True),
        ("action_btn", "opacity", 0.0),
        ("action_btn", "sensitive", False),
        ("right_status_label", "text", "Cancelling…"),
        ("right_status_label", "visible", True),
        ("status", "remove_css", "dzll-background-prepare-progress-60"),
        ("percent_label", "text", ""),
        ("status", "visible", True),
    ]


def test_window_cancel_click_presents_cancelling_then_cancels_controller():
    queue = BackgroundPreparationQueue()
    transition = queue.enqueue(_snapshot(_server("10.0.0.1", "Alpha")), _runtime())
    cancelled = []
    assert queue.attach_controller(
        transition.dispatch, SimpleNamespace(cancel=lambda: cancelled.append(True)),
    )
    host, ui, events = _window_host(queue)
    host._background_prepare_cancel_consent_ui = lambda: events.append(
        ("window", "cancel_consent_ui", None)
    )

    DZLLWindow._background_prepare_action_clicked(host, ui.background_prepare_action_btn)

    assert host._background_prepare_cancel_requested is True
    assert cancelled == [True]
    assert ui.background_prepare_right_status_label.text == "Cancelling…"
    assert not ui.background_prepare_action_btn.sensitive
    assert ui.background_prepare_action_btn.opacity == 0.0
    tail = [entry[1] for entry in events if entry[0] == "window"]
    assert tail == ["refresh_action_states", "cancel_consent_ui"]
    cancelling_render = events.index(("right_status_label", "text", "Cancelling…"))
    assert cancelling_render < events.index(("window", "refresh_action_states", None))


def test_window_render_cancelling_is_generation_gated():
    host, ui, events = _window_host()
    DZLLWindow._background_prepare_render_cancelling(host, 6, 1)
    assert events == []
    DZLLWindow._background_prepare_render_cancelling(host, 7, 1)
    assert ui.background_prepare_status.visible
    assert ui.background_prepare_right_status_label.text == "Cancelling…"


# --- Errors ---------------------------------------------------------------


def test_reap_blocked_error_uses_strip_setters_and_keeps_window_state():
    queue = BackgroundPreparationQueue()
    transition = queue.enqueue(_snapshot(_server("10.0.0.1", "Blocked")), _runtime())
    queue.enqueue(_snapshot(_server("10.0.0.2", "Pending")), _runtime())
    queue.finish_blocked_reap_failure(
        transition.dispatch,
        PreparationOutcome(
            PreparationStatus.FAILED,
            reason="ugc_helper_failure_to_reap",
            error="helper pid 9 remained alive",
        ),
    )
    host, ui, events = _window_host(queue)
    host._background_prepare_active = True

    DZLLWindow._background_prepare_apply_queue_snapshot(host)

    assert host._background_prepare_active is False
    assert ui.background_prepare_failed_label.tooltip == "helper pid 9 remained alive"
    assert ui.background_prepare_queue_label.text == "Queued Servers: 1"
    assert ui.background_prepare_action_btn.text == "Cancel Queue"
    assert ui.background_prepare_action_btn.sensitive
    assert ui.background_prepare_action_btn.opacity == 1.0
    assert ui.background_prepare_right_status_label.text == "Blocked"
    assert "dzll-background-prepare-active" not in ui.background_prepare_status.css_classes
    assert events[-1] == ("window", "refresh_action_states", None)


def test_batch_summary_failure_shows_retry_and_clears_controller():
    host, ui, _ = _window_host()
    host._background_prepare_controller = object()
    failed = SimpleNamespace(
        display_name="Beta",
        identity="10.0.0.2:2302",
        error="",
        outcome=SimpleNamespace(reason="download_failed"),
    )
    batch = SimpleNamespace(
        ready_count=1, failed_count=1, cancelled_count=1, failed_entries=(failed,),
    )

    DZLLWindow._background_prepare_render_batch_summary(host, batch)

    assert ui.background_prepare_detail_label.text == "1 ready · 1 failed · 1 cancelled"
    assert ui.background_prepare_failed_label.text == "Failed server: Beta"
    assert ui.background_prepare_failed_label.tooltip == "Beta: download_failed"
    assert ui.background_prepare_retry_btn.visible
    assert ui.background_prepare_action_btn.text == "Close"
    assert ui.background_prepare_action_btn.sensitive
    assert host._background_prepare_controller is None
    assert host._background_prepare_active is False


# --- Cleanup --------------------------------------------------------------


def test_close_resets_window_state_then_hides_strip_in_original_order():
    host, ui, events = _window_host()
    ui.set_active_presentation(True, server_name="Alpha", queued_count=1)
    ui.set_progress_presentation(True, 0.5)
    events.clear()

    DZLLWindow._background_prepare_close(host)

    assert host._background_prepare_ui_generation == 4
    assert host._background_prepare_snapshot is None
    assert host._background_prepare_cancel_requested is False
    assert host._background_prepare_terminal_handled is False
    assert events == [
        ("status", "remove_css", "dzll-background-prepare-present"),
        ("status", "remove_css", "dzll-background-prepare-active"),
        ("status", "margin_start", 0),
        ("status", "margin_end", 0),
        ("status", "margin_bottom", 0),
        ("queue_label", "text", ""),
        ("queue_label", "visible", False),
        ("queue_divider", "visible", False),
        ("right_status_label", "visible", False),
        ("failed_label", "text", ""),
        ("failed_label", "tooltip", None),
        ("failed_label", "visible", False),
        ("retry_btn", "visible", False),
        ("status", "visible", False),
        ("window", "refresh_action_states", None),
    ]


def test_close_is_refused_while_the_queue_is_busy():
    queue = BackgroundPreparationQueue()
    queue.enqueue(_snapshot(_server("10.0.0.1", "Alpha")), _runtime())
    host, _ui, events = _window_host(queue)
    DZLLWindow._background_prepare_close(host)
    assert events == []
    assert host._background_prepare_ui_generation == 3


def test_idle_queue_snapshot_hides_strip_through_inactive_presentation():
    host, ui, events = _window_host()
    ui.set_active_presentation(True, server_name="Alpha")
    ui.background_prepare_status.set_visible(True)
    events.clear()

    DZLLWindow._background_prepare_apply_queue_snapshot(host)

    assert ("status", "remove_css", "dzll-background-prepare-present") in events
    assert events[-2:] == [
        ("status", "visible", False),
        ("window", "refresh_action_states", None),
    ]


# --- Window boundary ------------------------------------------------------


def test_window_keeps_lifecycle_and_delegates_only_presentation():
    for removed in (
        "def _build_background_prepare_status_block",
        "def _background_prepare_set_status_container",
        "def _background_prepare_set_active_presentation",
        "def _background_prepare_set_action_presentation",
        "def _background_prepare_present_cancelling",
        "def _background_prepare_set_progress_presentation",
        "BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET",
    ):
        assert removed not in WINDOW_SOURCE
    for kept in (
        "def _background_prepare_apply_queue_snapshot",
        "def _background_prepare_render_reap_blocked",
        "def _background_prepare_start_frozen_request",
        "def _background_prepare_steam_consent_blocking",
        "def _background_prepare_render_snapshot",
        "def _background_prepare_render_batch_summary",
        "def _background_prepare_action_clicked",
        "def _background_prepare_close",
        "def _background_prepare_retry_failed_clicked",
    ):
        assert kept in WINDOW_SOURCE
    status_ui_source = (
        ROOT / "src/dzll_launcher/background_prepare_status_ui.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "_background_prepare_queue", "GLib", "threading", "idle_add",
        "timeout_add", "PreparationOperationGate", "_join_attempts",
        "_ensure_join_steam_start_consent", "_preparation_reap_recovery",
    ):
        assert forbidden not in status_ui_source


# --- Real GTK -------------------------------------------------------------


class _HandlerHost:
    def __init__(self):
        self.clicks = []

    def _background_prepare_retry_failed_clicked(self, button):
        self.clicks.append(("retry", button))

    def _background_prepare_action_clicked(self, button):
        self.clicks.append(("action", button))


def test_real_gtk_build_mirrors_widgets_and_wires_click_handlers():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")

    host = _HandlerHost()
    ui = BackgroundPrepareStatusUI(host)
    root = ui.build()

    assert isinstance(root, Gtk.Box)
    assert ui.background_prepare_status is root
    assert not root.get_visible()
    assert root.has_css_class("dzll-background-prepare-status")
    for name in WIDGET_NAMES[1:] + ("background_prepare_grid", "background_prepare_info"):
        assert getattr(host, name) is getattr(ui, name)
    assert not hasattr(host, "background_prepare_status")
    assert ui.background_prepare_grid.get_child_at(0, 0) is ui.background_prepare_info

    ui.background_prepare_retry_btn.emit("clicked")
    ui.background_prepare_action_btn.emit("clicked")
    assert host.clicks == [
        ("retry", ui.background_prepare_retry_btn),
        ("action", ui.background_prepare_action_btn),
    ]

    ui.set_active_presentation(True, server_name="Alpha", queued_count=2)
    ui.set_progress_presentation(True, 0.5)
    assert root.has_css_class("dzll-background-prepare-active")
    assert root.has_css_class("dzll-background-prepare-progress-50")
    assert (root.get_margin_start(), root.get_margin_end(), root.get_margin_bottom()) == (10, 10, 8)
    assert ui.background_prepare_server_label.get_tooltip_text() == "Alpha"
    assert ui.background_prepare_queue_label.get_text() == "Queued Servers: 2"

    ui.present_cancelling()
    assert root.get_visible()
    assert not root.has_css_class("dzll-background-prepare-active")
    assert not root.has_css_class("dzll-background-prepare-progress-50")
    assert ui.background_prepare_action_btn.get_visible()
    assert ui.background_prepare_action_btn.get_opacity() == 0.0
    assert not ui.background_prepare_action_btn.get_sensitive()

    ui.set_active_presentation(False)
    root.set_visible(False)
    assert not root.has_css_class("dzll-background-prepare-present")
    assert (root.get_margin_start(), root.get_margin_end(), root.get_margin_bottom()) == (0, 0, 0)
