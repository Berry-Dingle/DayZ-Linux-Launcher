"""Startup-notice gating must treat the shared Join preparation overlay as a blocker."""

from types import SimpleNamespace

import pytest

import gi

gi.require_version("Gtk", "4.0")

from dzll_launcher.companion_restart_phase2_runtime import RuntimeNotice  # noqa: E402
from dzll_launcher.join_preparation_overlay_ui import JoinPreparationOverlayUI  # noqa: E402
from dzll_launcher.startup_presentation import StartupPresentationCoordinator  # noqa: E402
from dzll_launcher.window import DZLLWindow  # noqa: E402


class FakeWidget:
    """Minimal widget with GTK-style visibility and notify::visible signals."""

    def __init__(self, visible=False):
        self._visible = bool(visible)
        self.handlers = {}

    def get_visible(self):
        return self._visible

    def set_visible(self, visible):
        changed = bool(visible) != self._visible
        self._visible = bool(visible)
        if changed:
            for handler in list(self.handlers.get("notify::visible", [])):
                handler(self, None)

    def connect(self, signal, handler):
        if signal != "notify::visible":
            raise TypeError(f"unknown signal {signal}")
        self.handlers.setdefault(signal, []).append(handler)
        return len(self.handlers[signal])


class FakeRevealer(FakeWidget):
    def __init__(self, revealed=False):
        super().__init__(visible=True)
        self._revealed = bool(revealed)

    def get_reveal_child(self):
        return self._revealed

    def set_reveal_child(self, revealed):
        changed = bool(revealed) != self._revealed
        self._revealed = bool(revealed)
        if changed:
            for handler in list(self.handlers.get("notify::reveal-child", [])):
                handler(self, None)

    def connect(self, signal, handler):
        if signal not in {"notify::visible", "notify::reveal-child"}:
            raise TypeError(f"unknown signal {signal}")
        self.handlers.setdefault(signal, []).append(handler)
        return len(self.handlers[signal])


def _host():
    host = SimpleNamespace(
        settings_scrim=FakeWidget(),
        settings_revealer=FakeRevealer(),
        start_steam_join_scrim=FakeWidget(),
        start_steam_join_box=FakeWidget(),
        _join_preparation_overlay_ui=SimpleNamespace(
            join_preparation_scrim=FakeWidget(),
            join_preparation_card=FakeWidget(),
        ),
        shown=[],
    )
    host._retry_restart_learning_notice = (
        lambda: DZLLWindow._retry_restart_learning_notice(host)
    )

    def show_notice(notice):
        host.shown.append(notice)
        return True

    host._startup_presentation = StartupPresentationCoordinator(
        pending_notice=RuntimeNotice(kind="legacy_reset", title="Reset", body="Body"),
        hide_startup=lambda: None,
        maybe_show_update=lambda: False,
        blockers_visible=lambda: DZLLWindow._startup_notice_blockers_visible(host),
        show_notice=show_notice,
    )
    DZLLWindow._connect_startup_notice_blockers(host)
    return host


def test_real_overlay_ui_exposes_the_widget_names_used_for_blocking():
    ui = JoinPreparationOverlayUI(
        SimpleNamespace(), pulse_active=lambda: False, active_download=lambda: None
    )
    assert hasattr(ui, "join_preparation_scrim")
    assert hasattr(ui, "join_preparation_card")


@pytest.mark.parametrize("widget_name", ["join_preparation_scrim", "join_preparation_card"])
def test_notice_deferred_while_join_overlay_visible_then_shown_on_hide(widget_name):
    host = _host()
    overlay = host._join_preparation_overlay_ui
    widget = getattr(overlay, widget_name)
    widget.set_visible(True)

    result = host._startup_presentation.complete_startup()

    assert result.completed_now is True
    assert result.notice_shown is False
    assert host.shown == []
    assert host._startup_presentation.pending_notice is not None

    widget.set_visible(False)

    assert len(host.shown) == 1
    assert host.shown[0].title == "Reset"
    assert host._startup_presentation.notice_showing is True
    assert host._startup_presentation.pending_notice is None


def test_notice_waits_for_both_join_overlay_widgets_to_hide():
    host = _host()
    overlay = host._join_preparation_overlay_ui
    overlay.join_preparation_scrim.set_visible(True)
    overlay.join_preparation_card.set_visible(True)
    host._startup_presentation.complete_startup()

    overlay.join_preparation_card.set_visible(False)
    assert host.shown == []

    overlay.join_preparation_scrim.set_visible(False)
    assert len(host.shown) == 1


def test_notice_shown_immediately_when_no_blocker_visible():
    host = _host()
    result = host._startup_presentation.complete_startup()
    assert result.notice_shown is True
    assert len(host.shown) == 1


@pytest.mark.parametrize(
    "widget_name", ["settings_scrim", "start_steam_join_scrim", "start_steam_join_box"]
)
def test_existing_visibility_blockers_unchanged(widget_name):
    host = _host()
    widget = getattr(host, widget_name)
    widget.set_visible(True)
    host._startup_presentation.complete_startup()
    assert host.shown == []

    widget.set_visible(False)
    assert len(host.shown) == 1


def test_existing_settings_revealer_blocker_unchanged():
    host = _host()
    host.settings_revealer.set_reveal_child(True)
    host._startup_presentation.complete_startup()
    assert host.shown == []

    host.settings_revealer.set_reveal_child(False)
    assert len(host.shown) == 1


def test_missing_join_overlay_ui_is_tolerated():
    host = _host()
    host._join_preparation_overlay_ui = None
    assert DZLLWindow._startup_notice_blockers_visible(host) is False
    DZLLWindow._connect_startup_notice_blockers(host)
