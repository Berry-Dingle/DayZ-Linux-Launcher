"""Rendering and lifecycle tests for the shared Join preparation overlay (JoinPreparationOverlayUI)."""

import threading
from types import SimpleNamespace

import pytest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from dzll_launcher import join_preparation_overlay_ui as module  # noqa: E402
from dzll_launcher.join_popup_presentation import JoinPopupPhase  # noqa: E402
from dzll_launcher.join_preparation_overlay_ui import JoinPreparationOverlayUI  # noqa: E402
from dzll_launcher.preparation_presentation import (  # noqa: E402
    PreparationPresentationSnapshot,
    PreparationProgressMode,
)
from dzll_launcher.window import DZLLWindow  # noqa: E402

TITLE = "Checking/updating the required mods for this server"


class Scheduler:
    def __init__(self):
        self.next_id = 40
        self.timeouts = {}
        self.removed = []
        self.fail_remove = False

    def timeout_add(self, delay, fn, *args):
        self.next_id += 1
        self.timeouts[self.next_id] = (delay, fn)
        return self.next_id

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.timeouts.pop(source_id, None)
        if self.fail_remove:
            raise RuntimeError("already removed")
        return True


@pytest.fixture
def sched(monkeypatch):
    scheduler = Scheduler()
    monkeypatch.setattr(module, "GLib", SimpleNamespace(
        timeout_add=scheduler.timeout_add, source_remove=scheduler.source_remove,
    ))
    return scheduler


class Widget:
    """Records every widget call as (name, method, args)."""

    def __init__(self, name, calls, parent=None):
        self.name = name
        self.calls = calls
        self.parent = parent
        self.text = ""

    def get_parent(self):
        return self.parent

    def get_text(self):
        return self.text

    def __getattr__(self, method):
        def record(*args):
            self.calls.append((self.name, method, args))
            if method in ("set_text", "set_label"):
                self.text = args[0]
        return record


def make_ui(*, pulse_active=lambda: True, active_download=lambda: None):
    calls = []
    ui = JoinPreparationOverlayUI(
        SimpleNamespace(_steam_ugc_worker_in_progress=True),
        pulse_active=pulse_active, active_download=active_download,
    )
    box = Widget("progress_box", calls)
    buttons = Widget("buttons", calls)
    for name in ("scrim", "card", "heading", "spinner", "detail_label", "status_label"):
        setattr(ui, f"join_preparation_{name}", Widget(name, calls))
    ui.join_preparation_progress_bar = Widget("bar", calls, parent=box)
    ui.join_preparation_cancel_button = Widget("cancel", calls, parent=buttons)
    ui.percent_label = Widget("percent", calls)
    return ui, calls


def of(calls, name):
    return [(method, args) for widget, method, args in calls if widget == name]


# ------------------------------------------------------------------ phase mapping


@pytest.mark.parametrize("status, expected", [
    ("Waiting for Steam to start", (JoinPopupPhase.WAITING_STEAM, "Waiting for Steam…", True)),
    ("All Mods Downloaded, go", (JoinPopupPhase.LAUNCHING, "All Mods Downloaded, Launching DayZ, Please Wait...", True)),
    ("  All Mods Ready", (JoinPopupPhase.LAUNCHING, "All Mods Ready, Launching DayZ, Please Wait...", True)),
    ("", (JoinPopupPhase.CHECKING, "Checking & Preparing Mods for Join...", True)),
    (None, (JoinPopupPhase.CHECKING, "Checking & Preparing Mods for Join...", True)),
])
def test_join_progress_phase_maps_status_to_canonical_text(status, expected):
    assert JoinPreparationOverlayUI.join_progress_phase(status) == expected


# ------------------------------------------------------------------ pulse timer


def test_progress_timer_starts_once_pulses_indeterminate_bars_and_stops_itself(sched):
    state = {"active": True, "event": None}
    ui, calls = make_ui(pulse_active=lambda: state["active"], active_download=lambda: state["event"])

    ui.start_progress_timer()
    ui.start_progress_timer()
    assert [delay for delay, _fn in sched.timeouts.values()] == [500]
    source_id = ui.progress_timer_id
    _delay, pulse = sched.timeouts[source_id]

    assert pulse() is True
    assert of(calls, "bar") == [("set_visible", (True,)), ("set_show_text", (False,)), ("pulse", ())]

    calls.clear()
    state["event"] = {"total_bytes": 10}
    assert pulse() is True
    assert calls == []
    state["event"] = "not-a-dict"
    assert pulse() is True
    assert of(calls, "bar")[-1] == ("pulse", ())

    state["active"] = False
    assert pulse() is False
    assert ui.progress_timer_id == 0


def test_stop_progress_timer_removes_the_source_and_tolerates_failure(sched):
    ui, _calls = make_ui()
    ui.stop_progress_timer()
    assert sched.removed == []
    ui.start_progress_timer()
    source_id = ui.progress_timer_id
    sched.fail_remove = True
    ui.stop_progress_timer()
    assert sched.removed == [source_id]
    assert ui.progress_timer_id == 0


# ------------------------------------------------------------------ rendering


def test_layout_active_hides_detail_and_shows_percent(sched):
    ui, calls = make_ui()
    ui.percent_label.text = "42%"
    ui.set_layout_active(True)
    assert of(calls, "detail_label") == [("set_visible", (False,))]
    assert of(calls, "bar") == [("set_margin_top", (10,))]
    assert of(calls, "buttons") == [("set_margin_top", (10,))]
    assert of(calls, "percent") == [("set_text", ("42%",)), ("set_visible", (True,))]
    calls.clear()
    ui.set_layout_active(False)
    assert of(calls, "detail_label") == [("set_visible", (True,))]
    assert of(calls, "percent") == [("set_text", ("0%",)), ("set_visible", (False,))]


def test_render_status_error_stops_pulse_and_spinner(sched):
    ui, calls = make_ui()
    ui.render_status("Waiting for Steam to finish...")
    assert ui.progress_timer_id
    assert ui.join_preparation_heading.text == TITLE
    assert ui.join_preparation_status_label.text == "Waiting for Steam to finish..."
    assert of(calls, "spinner") == [("set_visible", (True,)), ("set_spinning", (True,))]

    calls.clear()
    timer = ui.progress_timer_id
    ui.render_status("Steam download failed", error=True)
    assert of(calls, "spinner") == [("set_visible", (False,)), ("set_spinning", (False,))]
    assert ("set_fraction", (0.0,)) in of(calls, "bar")
    assert sched.removed == [timer] and ui.progress_timer_id == 0
    assert of(calls, "percent")[-2:] == [("set_text", ("0%",)), ("set_visible", (True,))]


def test_render_cancelling_and_active_text_leave_progress_untouched(sched):
    ui, calls = make_ui()
    assert ui.render_cancelling() is False
    assert ui.join_preparation_status_label.text == "Cancelling download and cleaning up..."
    assert ("set_fraction", (0.0,)) not in of(calls, "bar")
    calls.clear()
    assert ui.render_active_text("Downloading Mod: X - 5 MB (1/2)") is False
    assert ui.join_preparation_status_label.text == "Downloading Mod: X - 5 MB (1/2)"
    assert [m for m, _a in of(calls, "bar")] == ["set_margin_top"]
    assert ui.progress_timer_id == 0


def test_render_progress_determinate_and_indeterminate(sched):
    ui, calls = make_ui()
    ui.start_progress_timer()
    timer = ui.progress_timer_id
    snapshot = PreparationPresentationSnapshot(
        1, "steam_client", JoinPopupPhase.DOWNLOADING, "", 11, "Mod 11", 200,
        1, 3, PreparationProgressMode.DETERMINATE, 0.257,
    )
    ui.render_progress(snapshot)
    assert sched.removed == [timer] and ui.progress_timer_id == 0
    assert of(calls, "bar")[-1] == ("set_fraction", (0.257,))
    assert of(calls, "percent")[-2:] == [("set_text", ("25%",)), ("set_visible", (True,))]

    calls.clear()
    ui.render_progress(PreparationPresentationSnapshot(
        1, "steam_client", JoinPopupPhase.CHECKING, "Checking",
        progress_mode=PreparationProgressMode.INDETERMINATE,
    ))
    assert of(calls, "bar") == [("set_visible", (True,)), ("set_show_text", (False,)), ("set_fraction", (0.0,))]
    assert ui.progress_timer_id

    calls.clear()
    ui.render_progress(PreparationPresentationSnapshot(1, "steam_client", JoinPopupPhase.CHECKING, "x"))
    assert of(calls, "bar") == [] and of(calls, "spinner") == [("set_visible", (True,)), ("set_spinning", (True,))]


def test_show_preparing_show_join_progress_hide_and_reset(sched):
    ui, calls = make_ui()
    ui.show_preparing()
    for name in ("cancel", "scrim", "card"):
        assert ("set_visible", (True,)) in of(calls, name)
    assert ui.join_preparation_heading.text == TITLE
    assert ui.progress_timer_id

    calls.clear()
    ui.show_join_progress()
    assert of(calls, "cancel") == [
        ("set_label", ("Cancel",)), ("set_tooltip_text", ("Cancel this Join preparation.",)),
        ("set_sensitive", (True,)), ("set_visible", (True,)),
    ]

    calls.clear()
    timer = ui.progress_timer_id
    ui._hide_join_preparation_overlay()
    assert sched.removed == [timer] and ui.progress_timer_id == 0
    assert of(calls, "detail_label") == [("set_visible", (True,))]
    assert of(calls, "card") == [("set_visible", (False,))]
    assert of(calls, "scrim") == [("set_visible", (False,))]

    ui._reset_join_preparation_overlay()
    assert ui.win._steam_ugc_worker_in_progress is False
    assert ui.join_preparation_status_label.text == ""


# ------------------------------------------------------------------ window-side boundary


class RecordingOverlay:
    def __init__(self, events):
        self.events = events

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.events.append(("overlay", name, args, kwargs)) or False


def boundary_host(events):
    attempts = SimpleNamespace(set_popup_state=lambda aid, state: events.append(("popup_state", aid, state)))
    return SimpleNamespace(
        _join_preparation_overlay_ui=RecordingOverlay(events),
        _join_popup_attempt_id=lambda: 5,
        _join_attempts=attempts,
        _join_popup_presentation=SimpleNamespace(invalidate_pending=lambda: events.append(("invalidate",))),
        _set_server_companion_join_status=lambda text, flash=False: events.append(("companion", text, flash)),
        _steam_client_stop_waiting_event=threading.Event(),
        _steam_client_set_cancel_buttons=lambda safe_cancel: events.append(("cancel_buttons", safe_cancel)),
        _join_popup_request=lambda phase, text, **kw: events.append(("request", phase, text, kw)),
    )


def test_window_status_marks_attempt_error_before_rendering_and_companion_after():
    events = []
    host = boundary_host(events)
    assert DZLLWindow._steam_ugc_render_status(host, "   ") is False
    assert events == []
    assert DZLLWindow._steam_ugc_render_status(host, " Failed ", error=True) is False
    assert events == [
        ("popup_state", 5, "error"), ("invalidate",),
        ("overlay", "render_status", ("Failed",), {"error": True}),
        ("companion", "Failed", True),
    ]
    events.clear()
    DZLLWindow._steam_ugc_render_status(host, "Waiting")
    assert events == [("overlay", "render_status", ("Waiting",), {"error": False}), ("companion", "Waiting", False)]


def test_window_cancelling_marks_attempt_error_then_renders():
    events = []
    host = boundary_host(events)
    assert DZLLWindow._steam_ugc_render_cancelling(host) is False
    assert events == [("popup_state", 5, "error"), ("invalidate",), ("overlay", "render_cancelling", (), {})]


def test_steam_client_download_overlay_resets_cancel_state_then_shows_and_requests_checking():
    events = []
    host = boundary_host(events)
    host._steam_client_safe_cancel_requested = True
    host._steam_ugc_active_event = {"id": 1}
    host._steam_client_stop_waiting_event.set()
    assert DZLLWindow._show_steam_client_download_overlay(host, "ignored") is False
    assert host._mod_download_backend_active == "steam_client"
    assert host._steam_client_safe_cancel_requested is False
    assert host._steam_ugc_active_event is None
    assert not host._steam_client_stop_waiting_event.is_set()
    assert events == [
        ("cancel_buttons", False),
        ("companion", "Checking/Updating Required Mods", True),
        ("overlay", "show_preparing", (), {}),
        ("request", JoinPopupPhase.CHECKING, "Checking & Preparing Mods for Join...", {"backend": "steam_client"}),
    ]


# ------------------------------------------------------------------ real GTK


def test_real_overlay_widgets_render_progress_and_reset():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")
    win = SimpleNamespace(_steam_ugc_worker_in_progress=True,
                          _join_preparation_cancel_clicked=lambda: None)
    ui = JoinPreparationOverlayUI(win, pulse_active=lambda: False, active_download=lambda: None)
    ui.build(Gtk.Overlay())
    assert win.join_preparation_progress_bar is ui.join_preparation_progress_bar
    try:
        ui.show_preparing()
        label = ui.percent_label
        assert label is not None and label.get_parent() is ui.join_preparation_progress_bar.get_parent()
        assert (label.get_text(), label.get_visible()) == ("0%", True)
        assert ui.join_preparation_card.get_visible() and ui.join_preparation_scrim.get_visible()
        assert ui.join_preparation_detail_label.get_visible() is False
        assert ui.join_preparation_progress_bar.get_margin_top() == 10
        assert ui.join_preparation_cancel_button.get_parent().get_margin_top() == 10

        ui.render_progress(PreparationPresentationSnapshot(
            1, "steam_client", JoinPopupPhase.DOWNLOADING, "", 11, "Mod", 100, 1, 2,
            PreparationProgressMode.DETERMINATE, 0.5,
        ))
        assert ui.join_preparation_progress_bar.get_fraction() == pytest.approx(0.5)
        assert label.get_text() == "50%"
        assert ui.progress_timer_id == 0
        assert ui.get_percent_label() is label

        ui._hide_join_preparation_overlay()
        assert not ui.join_preparation_card.get_visible() and not ui.join_preparation_scrim.get_visible()
        assert (label.get_text(), label.get_visible()) == ("0%", False)
        assert ui.join_preparation_progress_bar.get_fraction() == 0.0
        assert ui.join_preparation_progress_bar.get_margin_top() == 0
        ui._reset_join_preparation_overlay()
        assert ui.join_preparation_heading.get_label() == ""
        assert win._steam_ugc_worker_in_progress is False
    finally:
        ui.stop_progress_timer()
