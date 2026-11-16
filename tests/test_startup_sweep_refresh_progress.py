"""The startup A2S sweep is shown in the sidebar refresh button and progress line.

On launch the window runs a full A2S sweep. The sidebar shows it exactly like
a manual refresh: the refresh icon spins and ``Refreshing: n/total`` with a bar
appears under the icons. Clicking the spinning icon stops the startup sweep;
it then drains its in-flight queries and completes through the normal path.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import status_refresh_controller, window
from dzll_launcher.status_refresh_controller import (
    STATUS_REFRESH_SPINNING_CLASS,
    StatusRefreshController,
    StatusRefreshWidgets,
)
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = ROOT / "src/dzll_launcher/window.py"


# --------------------------------------------------------------- fake widgets


class FakeBox:
    def __init__(self):
        self.visible = False

    def set_visible(self, visible):
        self.visible = bool(visible)


class FakeButton:
    def __init__(self):
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


class FakeBar:
    def __init__(self):
        self.fraction = 0.0

    def set_fraction(self, fraction):
        self.fraction = fraction


class Timers:
    """Stands in for GLib timeouts so coalesced repaints can be fired by hand."""

    def __init__(self):
        self.pending = {}
        self.removed = []
        self._next_id = 100

    def timeout_add(self, _interval, callback, *args):
        self._next_id += 1
        self.pending[self._next_id] = (callback, args)
        return self._next_id

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.pending.pop(source_id, None)

    def fire_all(self):
        for source_id, (callback, args) in list(self.pending.items()):
            self.pending.pop(source_id, None)
            callback(*args)


@pytest.fixture
def timers(monkeypatch):
    fake = Timers()
    monkeypatch.setattr(status_refresh_controller.GLib, "timeout_add", fake.timeout_add)
    monkeypatch.setattr(status_refresh_controller.GLib, "source_remove", fake.source_remove)
    return fake


def make_ui():
    ui = SimpleNamespace(
        button=FakeButton(),
        icon=FakeIcon(),
        box=FakeBox(),
        count=FakeLabel(),
        bar=FakeBar(),
        manual_starts=[],
    )
    ui.controller = StatusRefreshController(
        widgets=lambda: StatusRefreshWidgets(
            button=ui.button,
            progress_box=ui.box,
            count_label=ui.count,
            progress_bar=ui.bar,
            icon=ui.icon,
        ),
        server_keys=lambda: ui.manual_starts.append(True) or [],
        lookup_server=lambda _key: None,
        submit=lambda *_args: None,
        apply_live_results=lambda *_args, **_kwargs: None,
        refresh_browser=lambda: None,
        scroller=lambda: SimpleNamespace(get_vadjustment=lambda: None),
        model_id=lambda: 0,
        is_shut_down=lambda: False,
    )
    ui.controller._ensure_cursor_controller = lambda *_args: False
    ui.controller._apply_cursor_for_state = lambda *_args: False
    return ui


def spinning(ui):
    return STATUS_REFRESH_SPINNING_CLASS in ui.icon.classes


# ------------------------------------------------------ controller behaviour


def test_external_progress_looks_like_a_manual_refresh(timers):
    ui = make_ui()

    assert ui.controller.show_external_progress(0, 11_090) is True

    assert spinning(ui)
    assert ui.button.tooltip == "Stop refreshing"
    assert ui.box.visible is True
    assert ui.count.text == "0/11090"
    assert ui.count.tooltip == "Refreshing: 0/11090"
    assert ui.bar.fraction == 0.0
    assert ui.controller.running is False  # no manual sweep was started


def test_external_updates_are_coalesced_into_one_repaint(timers):
    ui = make_ui()
    ui.controller.show_external_progress(0, 100)

    for completed in range(1, 40):
        ui.controller.show_external_progress(completed, 100)

    assert len(timers.pending) == 1
    assert ui.count.text == "0/100"
    timers.fire_all()
    assert ui.count.text == "39/100"
    assert ui.bar.fraction == pytest.approx(0.39)


def test_external_count_is_clamped_to_total(timers):
    ui = make_ui()
    ui.controller.show_external_progress(250, 200)
    assert ui.count.text == "200/200"


def test_end_external_progress_restores_idle_button_and_is_idempotent(timers):
    ui = make_ui()
    ui.controller.show_external_progress(0, 10)
    ui.controller.show_external_progress(5, 10)
    pending = list(timers.pending)

    ui.controller.end_external_progress()
    ui.controller.end_external_progress()

    assert not spinning(ui)
    assert ui.button.tooltip == "Refresh all server status"
    assert ui.box.visible is False
    assert ui.count.text == ""
    assert ui.controller.external_active is False
    assert all(source_id in timers.removed for source_id in pending)


def test_clicking_during_external_sweep_stops_it_and_never_starts_manual(timers):
    ui = make_ui()
    stops = []
    ui.controller.show_external_progress(3, 10, on_stop=lambda: stops.append(True))

    ui.controller.toggle()

    assert stops == [True]
    assert ui.manual_starts == []
    assert ui.controller.running is False
    assert not spinning(ui)
    assert ui.box.visible is False

    # The next click is an ordinary manual refresh again.
    ui.controller.toggle()
    assert ui.manual_starts == [True]


def test_external_progress_does_not_take_over_a_running_manual_sweep(timers):
    ui = make_ui()
    ui.controller.running = True

    assert ui.controller.show_external_progress(1, 10) is False
    assert ui.controller.external_active is False
    assert not spinning(ui)
    assert ui.count.history == []


def test_external_progress_without_targets_is_not_shown(timers):
    ui = make_ui()
    assert ui.controller.show_external_progress(0, 0) is False
    assert not spinning(ui)
    assert ui.box.visible is False


def test_shutdown_cancel_ends_external_progress(timers):
    ui = make_ui()
    ui.controller.show_external_progress(0, 10)

    ui.controller.cancel()

    assert ui.controller.external_active is False
    assert not spinning(ui)
    assert ui.box.visible is False


# ------------------------------------------------- startup sweep integration


class PendingFuture:
    def __init__(self, generation, key):
        self.generation = generation
        self.key = key
        self.callbacks = []

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def result(self):
        return self.generation, (self.key, {"ok": True})

    def complete(self):
        for callback in self.callbacks:
            callback(self)


class Executor:
    def __init__(self):
        self.futures = []

    def submit(self, _callable, generation, key, _ip, _qport):
        future = PendingFuture(generation, key)
        self.futures.append(future)
        return future


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def startup_host(monkeypatch, timers, keys, *, workers=2):
    monkeypatch.setattr(window, "STARTUP_LIVE_REST_WORKERS", workers)
    monkeypatch.setattr(window, "A2S_WARMUP_500_ENABLED", False)
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_args: 0)
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback, *args: callback(*args))
    ui = make_ui()
    events = []
    host = SimpleNamespace(
        _startup_live_generation=0,
        _startup_live_rest_flush_id=0,
        _startup_warmup_timeout_id=0,
        _startup_live_rest_buffer=[],
        _shutdown_cleanup_done=False,
        _startup_presentation=SimpleNamespace(startup_completed=True),
        _startup_live_executor=Executor(),
        _query_startup_rest_one=lambda *_args: None,
        _obj_by_key={
            key: SimpleNamespace(ip=f"10.0.0.{index}", qport=2303)
            for index, key in enumerate(keys, 1)
        },
        _status_refresh=ui.controller,
        applied=[],
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort"),
        _complete_startup_presentation=lambda: events.append("reveal"),
        _set_updating=lambda *_args: events.append("updating"),
    )
    host._apply_live_results = lambda results, **_kwargs: host.applied.extend(results)
    bind(
        host,
        "_submit_startup_rest_batches",
        "_pump_startup_rest_sweep",
        "_on_startup_rest_future_done",
        "_flush_startup_rest_results",
        "_maybe_finish_startup_rest_sweep",
        "_complete_startup_warmup",
        "_on_startup_warmup_timeout",
    )
    return host, ui, events


def test_startup_sweep_shows_progress_from_launch_until_exhausted(monkeypatch, timers):
    keys = ["a", "b", "c"]
    host, ui, _events = startup_host(monkeypatch, timers, keys)

    host._submit_startup_rest_batches(keys, warmup_count=0)

    assert spinning(ui)
    assert ui.box.visible is True
    assert ui.count.text == "0/3"
    assert ui.button.tooltip == "Stop refreshing"

    host._startup_live_executor.futures[0].complete()
    timers.fire_all()
    assert ui.count.text == "1/3"

    for future in list(host._startup_live_executor.futures[1:]):
        future.complete()

    assert host._suggestion_sweep_complete is True
    assert sorted(key for key, _info in host.applied) == keys
    assert not spinning(ui)
    assert ui.box.visible is False
    assert ui.button.tooltip == "Refresh all server status"
    assert ui.controller.external_active is False


def test_stop_click_drains_startup_sweep_through_normal_completion(monkeypatch, timers):
    keys = ["a", "b", "c", "d", "e"]
    host, ui, _events = startup_host(monkeypatch, timers, keys, workers=2)
    host._submit_startup_rest_batches(keys, warmup_count=0)
    in_flight = list(host._startup_live_executor.futures)
    assert len(in_flight) == 2

    ui.controller.toggle()  # user clicks the spinning refresh icon

    assert ui.manual_starts == []
    assert not spinning(ui)
    assert ui.box.visible is False
    assert not host._startup_live_rest_queue
    assert getattr(host, "_suggestion_sweep_complete", False) is False

    # In-flight queries still return; their results are kept, nothing new is
    # dispatched, and the progress line does not come back.
    in_flight[0].complete()
    assert not spinning(ui)
    assert ui.box.visible is False
    in_flight[1].complete()

    assert len(host._startup_live_executor.futures) == 2
    assert sorted(key for key, _info in host.applied) == ["a", "b"]
    assert host._suggestion_sweep_complete is True
    assert host._startup_warmup_revealed is True
    assert ui.controller.external_active is False


def test_stop_with_nothing_in_flight_completes_immediately(monkeypatch, timers):
    host, ui, _events = startup_host(monkeypatch, timers, ["a"])
    host._startup_live_generation = 4
    host._startup_live_rest_queue = deque(["a"])
    host._startup_live_rest_inflight = 0
    host._startup_live_rest_buffer = []
    host._startup_warmup_revealed = True

    DZLLWindow._stop_startup_rest_sweep(host, 4)

    assert host._startup_sweep_progress_stopped_generation == 4
    assert not host._startup_live_rest_queue
    assert host._suggestion_sweep_complete is True


def test_stale_stop_request_is_ignored(monkeypatch, timers):
    host, _ui, _events = startup_host(monkeypatch, timers, ["a"])
    host._startup_live_generation = 5
    host._startup_live_rest_queue = deque(["a"])

    DZLLWindow._stop_startup_rest_sweep(host, 4)

    assert list(host._startup_live_rest_queue) == ["a"]
    assert getattr(host, "_startup_sweep_progress_stopped_generation", None) is None


def test_superseding_sweep_replaces_progress_with_the_new_total(monkeypatch, timers):
    host, ui, _events = startup_host(monkeypatch, timers, ["a", "b", "c", "d"])
    host._submit_startup_rest_batches(["a", "b", "c"], warmup_count=0)
    assert ui.count.text == "0/3"

    host._submit_startup_rest_batches(["a", "b", "c", "d"], warmup_count=0)

    assert spinning(ui)
    assert ui.count.text == "0/4"

    # Results from the superseded generation do not move the new progress.
    host._startup_live_executor.futures[0].complete()
    timers.fire_all()
    assert ui.count.text == "0/4"


def test_progress_ignores_stale_generations_and_missing_controller(monkeypatch, timers):
    host, ui, _events = startup_host(monkeypatch, timers, ["a"])
    host._startup_live_generation = 3
    host._startup_live_rest_total = 5
    host._startup_live_rest_completed = 1

    DZLLWindow._show_startup_sweep_progress(host, 2)
    assert ui.controller.external_active is False

    host._status_refresh = None
    DZLLWindow._show_startup_sweep_progress(host, 3)
    DZLLWindow._end_startup_sweep_progress(host)


def test_every_generation_bump_outside_the_sweep_ends_the_progress():
    source = WINDOW_SOURCE.read_text(encoding="utf-8")
    submit = source[source.index("    def _submit_startup_rest_batches"):]
    submit = submit[:submit.index("\n    def ", 10)]
    assert "DZLLWindow._end_startup_sweep_progress(self)" in submit

    finish = source[source.index("    def _maybe_finish_startup_rest_sweep"):]
    finish = finish[:finish.index("\n    def ", 10)]
    assert "DZLLWindow._end_startup_sweep_progress(self)" in finish

    # Database reload supersede and failed-publication cancel sites.
    superseded = 'DZLLWindow._log_a2s_sweep_terminal(self, "superseded")\n        DZLLWindow._end_startup_sweep_progress(self)'
    cancelled = 'DZLLWindow._log_a2s_sweep_terminal(self, "cancelled")\n            DZLLWindow._end_startup_sweep_progress(self)'
    assert source.count(superseded) == 2
    assert source.count(cancelled) == 1
    # Shutdown ends it through StatusRefreshController.cancel().
    assert "self._status_refresh.cancel()" in source
