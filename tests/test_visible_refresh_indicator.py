"""Visible-row refresh pill: real refresh trigger, repeat behaviour and cleanup."""

import time
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from dzll_launcher import visible_refresh_indicator as vri
from dzll_launcher.styles import get_app_css
from dzll_launcher.window import DZLLWindow

PULSE = vri.VISIBLE_REFRESH_PULSE_CLASS


@pytest.fixture(autouse=True)
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


@pytest.fixture
def indicator():
    ind = vri.VisibleRefreshIndicator()
    yield ind
    ind.cleanup()


def _iterate_until(predicate, timeout=2.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.005)
    return predicate()


def test_pill_layout_and_text(indicator):
    pill = indicator.widget
    assert indicator.label.get_text() == "Updating Visible Rows"
    assert pill.get_first_child() is indicator.label
    assert indicator.label.get_next_sibling() is indicator.dot
    assert pill.get_halign() == Gtk.Align.END
    assert pill.get_valign() == Gtk.Align.END
    assert pill.get_margin_end() == vri.VISIBLE_REFRESH_PILL_MARGIN_END >= 16
    assert pill.get_margin_bottom() == vri.VISIBLE_REFRESH_PILL_MARGIN_BOTTOM > 0
    assert pill.get_can_target() is False
    assert pill.get_can_focus() is False
    assert indicator.dot.get_size_request() == (7, 7)
    assert pill.get_visible() is True


def test_idle_indicator_has_no_timer(indicator):
    assert indicator.pulse_active is False
    assert not indicator.dot.has_css_class(PULSE)


def test_pulse_blinks_once_then_settles(indicator):
    assert indicator.pulse() is True
    assert indicator.dot.has_css_class(PULSE)
    assert indicator.pulse_active is True

    assert _iterate_until(lambda: not indicator.pulse_active)
    assert not indicator.dot.has_css_class(PULSE)


def test_repeat_pulse_replaces_the_pending_source(indicator, monkeypatch):
    removed = []
    original_remove = GLib.source_remove

    def record_remove(source_id):
        removed.append(source_id)
        return original_remove(source_id)

    monkeypatch.setattr(vri.GLib, "source_remove", record_remove)

    indicator.pulse()
    first = indicator._pulse_source_id
    indicator.pulse()
    second = indicator._pulse_source_id

    assert removed == [first]
    assert second and second != first
    assert GLib.MainContext.default().find_source_by_id(first) is None
    assert indicator.dot.has_css_class(PULSE)
    assert _iterate_until(lambda: not indicator.pulse_active)
    assert not indicator.dot.has_css_class(PULSE)


def test_cleanup_cancels_pending_pulse_and_disables(indicator):
    indicator.pulse()
    source_id = indicator._pulse_source_id

    indicator.cleanup()

    assert indicator.pulse_active is False
    assert not indicator.dot.has_css_class(PULSE)
    assert GLib.MainContext.default().find_source_by_id(source_id) is None
    assert indicator.pulse() is False
    assert indicator.pulse_active is False


def test_unrealize_cancels_a_pending_blink_without_disabling():
    ind = vri.VisibleRefreshIndicator()
    window = Gtk.Window()
    window.set_child(ind.widget)
    window.present()
    assert _iterate_until(ind.widget.get_realized)
    ind.pulse()
    source_id = ind._pulse_source_id

    window.destroy()

    assert ind.pulse_active is False
    assert not ind.dot.has_css_class(PULSE)
    assert GLib.MainContext.default().find_source_by_id(source_id) is None
    ind.cleanup()


def test_destroy_signal_runs_cleanup():
    ind = vri.VisibleRefreshIndicator()
    ind.pulse()
    source_id = ind._pulse_source_id

    ind.widget.emit("destroy")

    assert ind.pulse_active is False
    assert GLib.MainContext.default().find_source_by_id(source_id) is None
    assert ind.pulse() is False


def test_reset_keeps_the_indicator_usable(indicator):
    indicator.pulse()
    indicator.reset()
    assert indicator.pulse_active is False
    assert indicator.pulse() is True


def test_css_uses_dzll_tokens_and_a_plain_dot():
    css = get_app_css("#000", 220, "#999").decode("utf-8")

    def block(selector):
        start = css.index(selector + " {")
        return css[start: css.index("}", start)]

    dot = block(".visible-refresh-dot")
    assert "background-color: @dzll_mod_focus;" in dot
    assert "border-radius: 999px;" in dot
    assert "box-shadow: none;" in dot
    assert "animation" not in dot
    pulse = block(".visible-refresh-dot.visible-refresh-dot-pulse")
    assert "opacity:" in pulse
    assert "animation" not in pulse
    pill = block(".visible-refresh-pill")
    assert "@dzll_surface_control" in pill and "@dzll_border" in pill


# --- integration with the real browser live-refresh completion path ---


class _Indicator:
    def __init__(self):
        self.pulses = 0

    def pulse(self):
        self.pulses += 1
        return True


def _host(token=7, targets=("a", "b"), defer=False, scroll_active=False):
    applied = []
    host = SimpleNamespace(
        _browser_live_token=token,
        _browser_live_target_keys=set(targets),
        _browser_live_inflight=True,
        _browser_live_scroll_active_until=(time.monotonic() + 60.0) if scroll_active else 0.0,
        _obj_by_key={},
        _visible_refresh_indicator=_Indicator(),
        _defer_browser_scrollbar_work=lambda *_a: defer,
        _diagnose_rejected_browser_results=lambda *_a: None,
        _apply_live_results=lambda results, reason: applied.append((reason, list(results))),
    )
    host.applied = applied
    return host


def _apply(host, token=7, targets=("a", "b"), results=None, **kw):
    if results is None:
        results = [("a", {"ok": True, "ping_ms": 40}), ("b", {"ok": False})]
    return DZLLWindow._apply_browser_live_results(host, token, set(targets), results, **kw)


def test_accepted_visible_refresh_pulses_once():
    host = _host()
    _apply(host)
    assert host.applied and host.applied[0][0] == "browser-live"
    assert host._visible_refresh_indicator.pulses == 1
    assert host._browser_live_inflight is False


def test_each_completed_refresh_pulses_again():
    host = _host()
    _apply(host)
    _apply(host)
    assert host._visible_refresh_indicator.pulses == 2


@pytest.mark.parametrize(
    ("host_kw", "apply_kw"),
    [
        ({}, {"token": 6}),  # stale generation
        ({}, {"targets": ("a",)}),  # superseded targets
        ({}, {"results": []}),  # nothing came back
        ({"defer": True}, {}),  # deferred while dragging the scrollbar
        ({"scroll_active": True}, {}),  # skipped while scrolling
    ],
)
def test_rejected_or_deferred_refresh_does_not_pulse(host_kw, apply_kw):
    host = _host(**host_kw)
    _apply(host, **apply_kw)
    assert host._visible_refresh_indicator.pulses == 0


def test_deferred_refresh_pulses_when_it_settles():
    host = _host(defer=True)
    _apply(host)
    assert host._visible_refresh_indicator.pulses == 0
    _apply(host, _from_scroll_settle=True)
    assert host._visible_refresh_indicator.pulses == 1


def test_only_the_browser_live_completion_triggers_a_pulse():
    import inspect

    from dzll_launcher import window as window_module

    source = inspect.getsource(window_module)
    calls = [line for line in source.splitlines() if "DZLLWindow._pulse_visible_refresh_indicator(self)" in line]
    assert len(calls) == 1
    body = inspect.getsource(DZLLWindow._apply_browser_live_results)
    assert "DZLLWindow._pulse_visible_refresh_indicator(self)" in body
    # No independent periodic timer: the module's only timer ends a blink.
    module_source = inspect.getsource(vri)
    assert module_source.count("timeout_add") == 1
    assert "timeout_add_seconds" not in module_source


def test_window_without_indicator_is_tolerated():
    host = _host()
    del host._visible_refresh_indicator
    _apply(host)
    assert host.applied
