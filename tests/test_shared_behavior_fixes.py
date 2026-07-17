from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from dzll_launcher import window as window_module
from dzll_launcher.styles import get_app_css
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS


ROOT = Path(__file__).resolve().parents[1]


class FakeAdjustment:
    def __init__(self, value=137.0):
        self.value = value
        self.set_calls = []

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value
        self.set_calls.append(value)


class FakeScroller:
    def __init__(self, adjustment):
        self.adjustment = adjustment

    def get_vadjustment(self):
        return self.adjustment


class FakeToggle:
    def __init__(self, active=False):
        self.active = active

    def get_active(self):
        return self.active


def favorite_host(*, pin=False, favorites_only=False):
    adjustment = FakeAdjustment()
    calls = []
    host = SimpleNamespace(
        scroller=FakeScroller(adjustment),
        favorites={},
        settings={"pin_favorite_servers": pin},
        cb_show_fav=FakeToggle(favorites_only),
        _on_filter_changed=lambda **kwargs: calls.append(kwargs),
    )
    return host, adjustment, calls


def test_undock_shrink_is_one_shot_coalesced_and_delayed(monkeypatch):
    scheduled = []
    monkeypatch.setattr(window_module.GLib, "timeout_add", lambda delay, callback: scheduled.append((delay, callback)) or 77)
    host = SimpleNamespace(_server_companion_undock_shrink_source_id=0)
    host._apply_server_companion_post_undock_shrink = lambda: False

    DZLLWindow._schedule_server_companion_post_undock_shrink(host)
    DZLLWindow._schedule_server_companion_post_undock_shrink(host)

    assert SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS == 200
    assert len(scheduled) == 1
    assert scheduled[0][0] == SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS
    assert host._server_companion_undock_shrink_source_id == 77


def test_undock_shrink_applies_only_while_still_undocked_and_preserves_state():
    monitored = object()
    snapshot = {"name": "Preserved"}
    calls = []
    host = SimpleNamespace(
        _server_companion_undock_shrink_source_id=77,
        _server_companion_docked=False,
        _server_companion_undocked_window=object(),
        _server_companion_obj=monitored,
        _server_companion_snapshot=snapshot,
        settings={"show_server_companion": True},
        _collapse_server_companion_dock_space=lambda: calls.append("collapse"),
    )

    assert DZLLWindow._apply_server_companion_post_undock_shrink(host) is False
    assert calls == ["collapse"]
    assert host._server_companion_undock_shrink_source_id == 0
    assert host._server_companion_obj is monitored
    assert host._server_companion_snapshot is snapshot

    host._server_companion_docked = True
    assert DZLLWindow._apply_server_companion_post_undock_shrink(host) is False
    assert calls == ["collapse"]


def test_redock_cancels_pending_shrink(monkeypatch):
    removed = []
    monkeypatch.setattr(window_module.GLib, "source_remove", removed.append)
    host = SimpleNamespace(_server_companion_undock_shrink_source_id=88)
    DZLLWindow._cancel_server_companion_post_undock_shrink(host)
    assert removed == [88]
    assert host._server_companion_undock_shrink_source_id == 0


def test_undock_schedules_after_present_and_power_off_keeps_immediate_collapse():
    source = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    undock = source.split("    def _undock_server_companion(self):", 1)[1].split("    def _dock_server_companion", 1)[0]
    off = source.split("    def set_server_companion_visible", 1)[1].split("    def _refresh_server_companion_power_controls", 1)[0]
    assert undock.index("win.present()") < undock.index("_schedule_server_companion_post_undock_shrink()")
    assert "_collapse_server_companion_dock_space()" not in undock
    assert "_cancel_server_companion_post_undock_shrink()" in off
    assert "_collapse_server_companion_dock_space()" in off


def test_css_has_no_max_width_and_supported_widget_constraints_remain():
    css = get_app_css("#56575a", 220, "#717171").decode("utf-8")
    companion = (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8")
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    assert "max-width" not in css
    assert "self.set_size_request(self.WIDTH, -1)" in companion
    assert "WIDTH = 280" in companion
    assert "sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)" in sidebar
    assert "sidebar_frame.set_hexpand(False)" in sidebar
    assert "disclaimer.set_size_request(effective, -1)" in sidebar
    assert "disclaimer.set_wrap(True)" in sidebar


@pytest.mark.parametrize("initial", [False, True])
def test_favorite_toggle_without_pin_or_favorites_filter_preserves_order_and_scroll(monkeypatch, initial):
    saved = []
    monkeypatch.setattr(window_module, "save_favorites", lambda value: saved.append(dict(value)))
    host, adjustment, filter_calls = favorite_host()
    obj = ServerObject(ip="127.0.0.1", gport=2302, fav=initial)
    visible_order = [object(), obj, object()]
    identity_before = id(obj)

    DZLLWindow._toggle_favorite_for_obj(host, obj)

    assert bool(obj.fav) is (not initial)
    assert id(obj) == identity_before
    assert visible_order[1] is obj
    assert filter_calls == []
    assert adjustment.value == 137.0
    assert adjustment.set_calls == []
    assert len(saved) == 1


def test_pin_favorites_and_favorites_only_filter_keep_intentional_refresh(monkeypatch):
    monkeypatch.setattr(window_module, "save_favorites", lambda _value: None)
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda callback: callback() or 1)
    for pin, filtered in ((True, False), (False, True)):
        host, adjustment, calls = favorite_host(pin=pin, favorites_only=filtered)
        obj = ServerObject(ip="127.0.0.1", gport=2302)
        DZLLWindow._toggle_favorite_for_obj(host, obj)
        assert calls == [{"reason": "favourites"}]
        assert adjustment.set_calls == [137.0]


@pytest.mark.parametrize(
    ("key", "a_kwargs", "b_kwargs"),
    [
        ("played", {"played": "1 Day Ago", "sort_played_days": 1}, {"played": "2 Days Ago", "sort_played_days": 2}),
        ("players", {"sort_players": 10}, {"sort_players": 20}),
        ("ping", {"sort_ping": 20}, {"sort_ping": 40}),
    ],
)
def test_explicit_sort_comparisons_remain_functional(key, a_kwargs, b_kwargs):
    host = SimpleNamespace(
        sort_key=key,
        sort_asc=True,
        settings={"pin_favorite_servers": False, "prioritise_trusted_servers": False},
    )
    a = ServerObject(name="A", ip="1.1.1.1", gport=2302, **a_kwargs)
    b = ServerObject(name="B", ip="2.2.2.2", gport=2302, **b_kwargs)
    assert DZLLWindow._sort_func_impl(host, a, b) == Gtk.Ordering.SMALLER
