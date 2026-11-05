"""Shutdown must cancel the search debounce, suggestion-refresh debounce and Join progress pulse."""

import threading
from types import SimpleNamespace

from gi.repository import GLib as RealGLib

from dzll_launcher import join_preparation_overlay_ui as overlay_module
from dzll_launcher import mod_suggestion_popover as popover_module
from dzll_launcher import window as window_module
from dzll_launcher.join_attempt import JoinAttemptTracker
from dzll_launcher.join_preparation_overlay_ui import JoinPreparationOverlayUI
from dzll_launcher.mod_suggestion_popover import (
    SUGGESTION_REFRESH_DEBOUNCE_MS,
    ModSuggestionPopover,
)
from dzll_launcher.window import DZLLWindow


class FakeGLib:
    """Records timeouts; `run_due()` dispatches only sources that were never removed."""

    def __init__(self):
        self.next_id = 900
        self.timeouts = {}
        self.removed = []

    def timeout_add(self, delay, fn, *args):
        self.next_id += 1
        self.timeouts[self.next_id] = (int(delay), fn, args)
        return self.next_id

    def idle_add(self, fn, *args):
        return self.timeout_add(0, fn, *args)

    def source_remove(self, source_id):
        self.removed.append(int(source_id))
        self.timeouts.pop(int(source_id), None)
        return True

    def run_due(self):
        for source_id, (_delay, fn, args) in list(self.timeouts.items()):
            if source_id not in self.timeouts:
                continue
            if not fn(*args):
                self.timeouts.pop(source_id, None)

    def __getattr__(self, name):
        return getattr(RealGLib, name)


class FakeEntry:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text


class RecordingProgressBar:
    def __init__(self):
        self.calls = []

    def set_visible(self, value):
        self.calls.append(("set_visible", value))

    def set_show_text(self, value):
        self.calls.append(("set_show_text", value))

    def pulse(self):
        self.calls.append(("pulse",))


def _install_fake_glib(monkeypatch):
    fake = FakeGLib()
    for module in (window_module, popover_module, overlay_module):
        monkeypatch.setattr(module, "GLib", fake)
    return fake


def _shutdown_host(**extra):
    attempts = JoinAttemptTracker(log_sink=lambda _line: None)
    host = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _background_prepare_ui_generation=0,
        _preparation_reap_recovery_source_id=0,
        _background_prepare_queue=None,
        _background_prepare_controller=None,
        _scroll_drag_light=None,
        _discord=None,
        _join_attempts=attempts,
        _join_popup_item_activity=SimpleNamespace(clear_attempt=lambda _attempt_id: None),
        _join_preparation_cancel_event=threading.Event(),
        _finish_start_steam_join_consent=lambda *_args, **_kwargs: False,
        _clear_join_pending_state=lambda _attempt_id: True,
    )
    for name, value in extra.items():
        setattr(host, name, value)
    return host


def _arm_all_three(monkeypatch):
    fake = _install_fake_glib(monkeypatch)
    filter_calls = []
    refresh_calls = []
    host = _shutdown_host(_search_filter_debounce_id=0, _mod_download_backend_active="steam_client",
                          _steam_ugc_worker_in_progress=True)
    host._cancel_search_filter_debounce = lambda: DZLLWindow._cancel_search_filter_debounce(host)
    host._apply_search_filter_changed = lambda **kwargs: filter_calls.append(kwargs)

    popover = ModSuggestionPopover(
        catalogue=SimpleNamespace(ready=True, restore_pending=False, restore_inflight=False),
        has_rows=lambda: None,
        apply_search_filter=lambda **_kwargs: None,
        cancel_search_filter_debounce=lambda: None,
        debug_enabled=lambda: False,
    )
    popover.bind_widgets(entry=FakeEntry("chernarus"), panel=None, suggestion_list=None,
                         scroller=None, refresh_chips=None, sync_mode_ui=None)
    popover.refresh = lambda: refresh_calls.append("refresh")
    host._mod_suggestion_popover = popover

    bar = RecordingProgressBar()
    overlay = JoinPreparationOverlayUI(
        host,
        # Mirrors the window wiring, including the shutdown term.
        pulse_active=lambda: (
            not bool(getattr(host, "_shutdown_cleanup_done", False))
            and host._mod_download_backend_active == "steam_client"
            and bool(host._steam_ugc_worker_in_progress)
        ),
        active_download=lambda: None,
    )
    overlay.join_preparation_progress_bar = bar
    host._join_preparation_overlay_ui = overlay

    popover.queue_refresh()
    DZLLWindow._on_search_changed_debounced(host)
    overlay.start_progress_timer()
    return fake, host, popover, overlay, bar, filter_calls, refresh_calls


def test_all_three_timers_are_armed_with_expected_intervals(monkeypatch):
    fake, host, popover, overlay, *_ = _arm_all_three(monkeypatch)
    delays = {
        popover.refresh_source_id: SUGGESTION_REFRESH_DEBOUNCE_MS,
        host._search_filter_debounce_id: 200,
        overlay.progress_timer_id: 500,
    }
    assert all(source_id for source_id in delays)
    assert {sid: fake.timeouts[sid][0] for sid in delays} == delays


def test_shutdown_removes_all_three_pending_timers_and_clears_ids(monkeypatch):
    fake, host, popover, overlay, *_ = _arm_all_three(monkeypatch)
    armed = {popover.refresh_source_id, host._search_filter_debounce_id, overlay.progress_timer_id}

    DZLLWindow._shutdown_cleanup(host)

    assert host._shutdown_cleanup_done
    assert armed <= set(fake.removed)
    assert not (armed & set(fake.timeouts))
    assert popover.refresh_source_id == 0
    assert host._search_filter_debounce_id == 0
    assert overlay.progress_timer_id == 0


def test_late_callbacks_do_not_update_widgets_after_shutdown(monkeypatch):
    fake, host, popover, overlay, bar, filter_calls, refresh_calls = _arm_all_three(monkeypatch)

    DZLLWindow._shutdown_cleanup(host)
    fake.run_due()

    assert filter_calls == []
    assert refresh_calls == []
    assert bar.calls == []


def test_progress_pulse_stops_itself_once_shutdown_is_marked(monkeypatch):
    fake, host, popover, overlay, bar, *_ = _arm_all_three(monkeypatch)
    pulse_id = overlay.progress_timer_id

    host._shutdown_cleanup_done = True  # shutdown begun; the source has not been removed yet
    assert overlay._progress_pulse() is False
    assert overlay.progress_timer_id == 0
    assert bar.calls == []
    assert pulse_id in fake.timeouts  # only the guard stopped it


def test_timers_still_fire_normally_without_shutdown(monkeypatch):
    fake, host, popover, overlay, bar, filter_calls, refresh_calls = _arm_all_three(monkeypatch)

    fake.run_due()

    assert refresh_calls == ["refresh"]
    assert filter_calls == [{"scroll": True, "reason": "search"}]
    assert ("pulse",) in bar.calls
    assert overlay.progress_timer_id in fake.timeouts  # repeating pulse continues


def test_shutdown_preserves_active_join_cancel_event_and_closes_attempt(monkeypatch):
    fake, host, popover, overlay, *_ = _arm_all_three(monkeypatch)
    host._join_attempts.begin(ip="127.0.0.1", game_port=2302, query_port=27016, name="A")
    cancel_event = host._join_preparation_cancel_event

    DZLLWindow._shutdown_cleanup(host)

    assert host._join_preparation_cancel_event is cancel_event
    assert cancel_event.is_set()
    assert host._join_attempts.active is None
    # Shutdown must not run broader Join cleanup, which would reset these.
    assert host._mod_download_backend_active == "steam_client"
    assert host._steam_ugc_worker_in_progress is True


def test_partial_construction_without_timer_owners_is_safe(monkeypatch):
    fake = _install_fake_glib(monkeypatch)
    host = _shutdown_host()

    DZLLWindow._shutdown_cleanup(host)

    assert host._shutdown_cleanup_done
    assert host._search_filter_debounce_id == 0
    assert fake.removed == []


def test_timer_cancellation_failures_do_not_abort_shutdown(monkeypatch):
    _install_fake_glib(monkeypatch)

    def boom():
        raise RuntimeError("cancel failed")

    cancel_event = threading.Event()
    host = _shutdown_host(
        _mod_suggestion_popover=SimpleNamespace(cancel_refresh=boom),
        _join_preparation_overlay_ui=SimpleNamespace(stop_progress_timer=boom),
        _join_preparation_cancel_event=cancel_event,
    )

    DZLLWindow._shutdown_cleanup(host)

    assert host._shutdown_cleanup_done
    assert cancel_event.is_set()
