from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from dzll_launcher import app as app_module
from dzll_launcher import window as window_module


class Widget:
    def __init__(self, *, active=False):
        self.active = bool(active)
        self.visible = False
        self.text = ""
        self.focused = False

    def get_active(self):
        return self.active

    def set_active(self, value):
        self.active = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_text(self, value):
        self.text = str(value)

    def grab_focus(self):
        self.focused = True


class FakeLoop:
    def __init__(self, on_run=None, *, run_error=None, events=None):
        self.on_run = on_run
        self.run_error = run_error
        self.events = events
        self.run_calls = 0
        self.quit_calls = 0
        self.running = False

    def run(self):
        self.run_calls += 1
        self.running = True
        if self.run_error is not None:
            raise self.run_error
        if self.on_run is not None:
            self.on_run(self)

    def quit(self):
        self.quit_calls += 1
        self.running = False
        if self.events is not None:
            self.events.append("consent-quit")

    def is_running(self):
        return self.running


class ConsentHost:
    _finish_start_steam_join_consent = (
        window_module.DZLLWindow._finish_start_steam_join_consent
    )
    _show_start_steam_join_consent_blocking = (
        window_module.DZLLWindow._show_start_steam_join_consent_blocking
    )
    _background_prepare_cancel_consent_ui = (
        window_module.DZLLWindow._background_prepare_cancel_consent_ui
    )

    def __init__(self, *, always=False):
        self._start_steam_join_decision = None
        self._start_steam_join_loop = None
        self.start_steam_join_title = Widget()
        self.start_steam_join_text = Widget()
        self.start_steam_join_check = Widget(active=always)
        self.start_steam_join_scrim = Widget()
        self.start_steam_join_box = Widget()
        self.start_steam_join_start_btn = Widget()


@pytest.mark.parametrize(
    ("accepted", "always", "expected"),
    [
        (True, True, (True, True)),
        (False, False, (False, False)),
    ],
)
def test_normal_consent_completion_quits_and_clears_loop(
        monkeypatch, accepted, always, expected):
    host = ConsentHost(always=always)

    def complete(active):
        host.start_steam_join_check.set_active(always)
        host._finish_start_steam_join_consent(
            accepted, expected_loop=active,
        )

    loop = FakeLoop(on_run=complete)
    monkeypatch.setattr(
        window_module, "GLib", SimpleNamespace(MainLoop=lambda: loop)
    )

    assert host._show_start_steam_join_consent_blocking() == expected
    assert loop.run_calls == 1
    assert loop.quit_calls == 1
    assert host._start_steam_join_loop is None
    assert not host.start_steam_join_box.visible
    assert not host.start_steam_join_scrim.visible


def test_repeated_completion_is_idempotent():
    host = ConsentHost(always=True)
    loop = FakeLoop()
    host._start_steam_join_loop = loop

    assert host._finish_start_steam_join_consent(True)
    assert not host._finish_start_steam_join_consent(False, always=False)
    assert host._start_steam_join_decision == (True, True)
    assert loop.quit_calls == 1


def test_expected_loop_identity_cannot_finish_newer_consent():
    host = ConsentHost()
    old_loop = FakeLoop()
    new_loop = FakeLoop()
    host._start_steam_join_loop = new_loop

    assert not host._finish_start_steam_join_consent(
        False, expected_loop=old_loop, always=False,
    )
    assert host._start_steam_join_loop is new_loop
    assert host._start_steam_join_decision is None
    assert old_loop.quit_calls == 0
    assert new_loop.quit_calls == 0


def test_old_loop_finally_cannot_clear_newer_loop(monkeypatch):
    host = ConsentHost()
    newer_loop = FakeLoop()

    def replace_with_newer(_old_loop):
        host._start_steam_join_decision = (False, False)
        host._start_steam_join_loop = newer_loop

    old_loop = FakeLoop(on_run=replace_with_newer)
    monkeypatch.setattr(
        window_module, "GLib", SimpleNamespace(MainLoop=lambda: old_loop)
    )

    assert host._show_start_steam_join_consent_blocking() == (False, False)
    assert host._start_steam_join_loop is newer_loop
    assert newer_loop.quit_calls == 0


def test_main_loop_exception_clears_reference_and_fails_closed(monkeypatch):
    host = ConsentHost()
    loop = FakeLoop(run_error=RuntimeError("loop failed"))
    monkeypatch.setattr(
        window_module, "GLib", SimpleNamespace(MainLoop=lambda: loop)
    )

    assert host._show_start_steam_join_consent_blocking() == (False, False)
    assert loop.run_calls == 1
    assert host._start_steam_join_loop is None


def test_presentation_failure_does_not_enter_nested_loop(monkeypatch):
    host = ConsentHost()
    loop = FakeLoop()

    def fail_set_text(_value):
        raise RuntimeError("presentation failed")

    host.start_steam_join_title.set_text = fail_set_text
    monkeypatch.setattr(
        window_module, "GLib", SimpleNamespace(MainLoop=lambda: loop)
    )

    assert host._show_start_steam_join_consent_blocking() == (False, False)
    assert loop.run_calls == 0
    assert loop.quit_calls == 1
    assert host._start_steam_join_decision == (False, False)
    assert host._start_steam_join_loop is None


def test_shutdown_quits_consent_before_dependent_teardown():
    events = []

    class ShutdownHost(ConsentHost):
        _shutdown_cleanup = window_module.DZLLWindow._shutdown_cleanup

        def __init__(self):
            super().__init__()
            self._shutdown_cleanup_done = False
            self._background_prepare_ui_generation = 0
            self._preparation_reap_recovery_source_id = 0
            self._background_prepare_queue = None
            self._background_prepare_controller = None
            self._scroll_drag_light = None
            self._discord = None

        def _settle_browser_scrollbar_interaction(self, reason):
            events.append(("dependent-teardown", reason))

    host = ShutdownHost()
    loop = FakeLoop(events=events)
    host._start_steam_join_loop = loop

    host._shutdown_cleanup()

    assert host._shutdown_cleanup_done
    assert host._start_steam_join_decision == (False, False)
    assert loop.quit_calls == 1
    assert events[0] == "consent-quit"
    assert events[1] == ("dependent-teardown", "shutdown")


def test_direct_application_quit_and_restart_end_active_consent():
    app = app_module.DZLLApp()
    host = ConsentHost()
    app.window = host

    direct_loop = FakeLoop()
    host._start_steam_join_loop = direct_loop
    app.quit()
    assert host._start_steam_join_decision == (False, False)
    assert direct_loop.quit_calls == 1

    restart_loop = FakeLoop()
    host._start_steam_join_loop = restart_loop
    host._start_steam_join_decision = None
    app.request_restart()
    assert app.restart_requested
    assert host._start_steam_join_decision == (False, False)
    assert restart_loop.quit_calls == 1


def test_foreground_join_cancel_ends_consent_and_preserves_cancel_state():
    host = ConsentHost()
    loop = FakeLoop()
    host._start_steam_join_loop = loop
    host._join_attempts = SimpleNamespace(active=SimpleNamespace(attempt_id=7))
    host._steam_client_safe_cancel_requested = False
    host._steamcmd_cancel_event = threading.Event()
    events = []
    host._steam_client_set_cancel_buttons = (
        lambda safe_cancel: events.append(("buttons", safe_cancel))
    )
    host._steam_ugc_render_cancelling = lambda: events.append(("cancelling",))

    window_module.DZLLWindow._steam_client_download_cancel_clicked(host, 7)

    assert host._steamcmd_cancel_event.is_set()
    assert host._steam_client_safe_cancel_requested
    assert host._start_steam_join_decision == (False, False)
    assert loop.quit_calls == 1
    assert events == [("buttons", True), ("cancelling",)]


def test_background_cancel_still_ends_consent():
    host = ConsentHost()
    loop = FakeLoop()
    host._start_steam_join_loop = loop

    host._background_prepare_cancel_consent_ui()

    assert host._start_steam_join_decision == (False, False)
    assert loop.quit_calls == 1
    assert not host.start_steam_join_box.visible
    assert not host.start_steam_join_scrim.visible
