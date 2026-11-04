"""Lifecycle tests for ServerCompanionDockController using recorded fake widgets."""

from types import SimpleNamespace

import pytest

from dzll_launcher import server_companion_dock as dock_module
from dzll_launcher.config import SIDEBAR_WIDTH, WINDOW_DEFAULT_SIZE
from dzll_launcher.server_companion_dock import (
    SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS,
    ServerCompanionDockController,
)
from dzll_launcher.server_companion_ui import ServerCompanionPanel


class Recorder:
    def __init__(self, name, log, fail=()):
        self.name = name
        self.log = log
        self.fail = set(fail)

    def record(self, method, *args):
        self.log.append((self.name, method, *args))
        if method in self.fail:
            raise RuntimeError(f"{self.name}.{method} failed")


class Revealer(Recorder):
    def __init__(self, log, fail=()):
        super().__init__("revealer", log, fail)
        self.child = None

    def set_reveal_child(self, value):
        self.record("set_reveal_child", value)

    def set_child(self, child):
        self.record("set_child", getattr(child, "name", child))
        self.child = child

    def set_visible(self, value):
        self.record("set_visible", value)


class Panel(Recorder):
    def __init__(self, log):
        super().__init__("panel", log)
        self.learning = False

    def set_docked(self, value):
        self.record("set_docked", value)

    def restart_learning_visible(self):
        return self.learning


class Box(Recorder):
    def set_size_request(self, *args):
        self.record("set_size_request", *args)


@pytest.fixture
def env(monkeypatch):
    log = []
    timers = {}
    window_failures = set()

    class FakeWindow(Recorder):
        def __init__(self, application=None, title=None):
            super().__init__("undocked", log, window_failures)
            log.append(("undocked", "init", application, title))
            if "init" in window_failures:
                raise RuntimeError("window creation failed")
            self.child = None

        def set_resizable(self, value):
            self.record("set_resizable", value)

        def set_child(self, child):
            self.record("set_child", getattr(child, "name", child))
            self.child = child

        def connect(self, signal, callback):
            self.record("connect", signal)
            self.close_request = callback
            return 4242

        def disconnect(self, handler_id):
            self.record("disconnect", handler_id)

        def present(self):
            self.record("present")

        def destroy(self):
            self.record("destroy")

        def set_default_size(self, *args):
            self.record("set_default_size", *args)

        def set_size_request(self, *args):
            self.record("set_size_request", *args)

        def queue_resize(self):
            self.record("queue_resize")

    def timeout_add(delay, callback):
        source_id = 900 + len(timers)
        timers[source_id] = callback
        log.append(("GLib", "timeout_add", delay, source_id))
        return source_id

    monkeypatch.setattr(dock_module.Gtk, "Window", FakeWindow)
    monkeypatch.setattr(dock_module.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(dock_module.GLib, "source_remove", lambda source_id: log.append(("GLib", "source_remove", source_id)))

    settings = {"show_server_companion": True}
    panel = Panel(log)
    revealer = Revealer(log)
    box = Box("browser_box", log)
    height = {"value": 700}

    def window_height():
        if isinstance(height["value"], Exception):
            raise height["value"]
        return height["value"]

    controller = ServerCompanionDockController(
        panel=lambda: panel,
        revealer=lambda: revealer,
        main_browser_box=lambda: box,
        get_application=lambda: "app",
        get_window_height=window_height,
        set_window_default_size=lambda *args: log.append(("main", "set_default_size", *args)),
        is_enabled=lambda: bool(settings.get("show_server_companion", False)),
        start_polling=lambda: log.append(("cb", "start_polling")),
        refresh_power_controls=lambda: log.append(("cb", "power_controls")),
        refresh_monitor_highlight=lambda: log.append(("cb", "highlight")),
    )
    return SimpleNamespace(controller=controller, log=log, timers=timers, settings=settings, panel=panel,
                           revealer=revealer, height=height, window_failures=window_failures)


def test_undock_reparents_into_window_then_schedules_shrink_and_polling(env):
    env.controller.undock()

    win = env.controller.undocked_window
    assert env.controller.docked is False
    assert env.controller.undocked_close_handler_id == 4242
    assert env.controller.reparenting is False
    assert win.child is env.panel and env.revealer.child is None
    assert env.log == [
        ("revealer", "set_reveal_child", False),
        ("revealer", "set_child", None),
        ("revealer", "set_visible", False),
        ("undocked", "init", "app", "DZLL Server Companion"),
        ("undocked", "set_resizable", False),
        ("undocked", "set_child", "panel"),
        ("undocked", "connect", "close-request"),
        ("panel", "set_docked", False),
        ("undocked", "set_default_size", ServerCompanionPanel.WIDTH, ServerCompanionPanel.UNDOCKED_COMPACT_HEIGHT),
        ("undocked", "set_size_request", ServerCompanionPanel.WIDTH, ServerCompanionPanel.UNDOCKED_COMPACT_HEIGHT),
        ("undocked", "queue_resize"),
        ("undocked", "present"),
        ("GLib", "timeout_add", SERVER_COMPANION_UNDOCK_SHRINK_DELAY_MS, 900),
        ("cb", "start_polling"),
        ("cb", "power_controls"),
    ]


def test_second_undock_presents_existing_window_and_disabled_undock_is_ignored(env):
    env.controller.undock()
    env.log.clear()
    env.controller.undock()
    assert env.log == [("undocked", "present")]

    env.controller.dock()
    env.settings["show_server_companion"] = False
    env.log.clear()
    env.controller.undock()
    assert env.log == [] and env.controller.docked is True


def test_close_request_redocks_unless_reparenting(env):
    env.controller.undock()
    win = env.controller.undocked_window

    env.controller.reparenting = True
    assert win.close_request(win) is False
    assert env.controller.docked is False

    env.controller.reparenting = False
    env.log.clear()
    assert win.close_request(win) is True
    assert env.controller.docked is True
    assert env.controller.undocked_window is None
    assert env.revealer.child is env.panel
    assert env.log == [
        ("GLib", "source_remove", 900),
        ("undocked", "disconnect", 4242),
        ("undocked", "set_child", None),
        ("undocked", "destroy"),
        ("panel", "set_docked", True),
        ("revealer", "set_child", "panel"),
        ("panel", "set_docked", True),
        ("revealer", "set_visible", True),
        ("revealer", "set_reveal_child", True),
        ("browser_box", "set_size_request", int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1),
        ("cb", "power_controls"),
    ]


def test_undock_failure_restores_docked_panel(env, capsys):
    env.window_failures.add("present")

    env.controller.undock()

    assert env.controller.docked is True
    assert env.controller.undocked_window is None
    assert env.controller.undocked_close_handler_id == 0
    assert env.controller.reparenting is False
    assert env.revealer.child is env.panel
    after_present = env.log[env.log.index(("undocked", "present")) + 1:]
    assert after_present == [
        ("undocked", "disconnect", 4242),
        ("undocked", "set_child", None),
        ("undocked", "destroy"),
        ("panel", "set_docked", True),
        ("revealer", "set_child", "panel"),
        ("revealer", "set_visible", True),
        ("revealer", "set_reveal_child", True),
        ("browser_box", "set_size_request", int(WINDOW_DEFAULT_SIZE[0]) - int(SIDEBAR_WIDTH), -1),
        ("cb", "highlight"),
        ("cb", "power_controls"),
        ("cb", "power_controls"),
    ]
    assert "[SC-DOCK] WARNING: undock failed; restoring docked Server Companion" in capsys.readouterr().out
    assert ("cb", "start_polling") not in env.log


def test_window_creation_failure_also_recovers(env, capsys):
    env.window_failures.add("init")

    env.controller.undock()

    assert env.controller.docked is True
    assert env.revealer.child is env.panel
    assert "undock failed" in capsys.readouterr().out


def test_post_undock_shrink_collapses_once_only_while_undocked(env):
    env.controller.undock()
    callback = env.timers[900]
    env.log.clear()

    assert callback() is False
    assert env.controller.shrink_source_id == 0
    assert env.log == [
        ("browser_box", "set_size_request", -1, -1),
        ("main", "set_default_size", int(WINDOW_DEFAULT_SIZE[0]), 700),
    ]

    env.controller.dock()
    env.log.clear()
    assert callback() is False
    assert env.log == []


@pytest.mark.parametrize("height", [0, RuntimeError("no height")])
def test_collapse_falls_back_to_default_height(env, height):
    env.height["value"] = height

    env.controller.collapse_dock_space()

    assert env.log[-1] == ("main", "set_default_size", int(WINDOW_DEFAULT_SIZE[0]), int(WINDOW_DEFAULT_SIZE[1]))


def test_undocked_size_tracks_learning_visibility_and_is_inert_when_docked(env):
    env.controller.update_undocked_size()
    assert env.log == []

    env.controller.undock()
    env.panel.learning = True
    env.log.clear()
    env.controller.update_undocked_size()
    assert env.log[0] == (
        "undocked", "set_default_size", ServerCompanionPanel.WIDTH, ServerCompanionPanel.UNDOCKED_EXPANDED_HEIGHT,
    )


def test_destroy_without_reattach_leaves_revealer_empty(env):
    env.controller.undock()
    env.log.clear()

    env.controller.destroy_undocked_window(reattach=False)

    assert env.controller.docked is True
    assert env.revealer.child is None
    assert env.log == [
        ("undocked", "disconnect", 4242),
        ("undocked", "set_child", None),
        ("undocked", "destroy"),
        ("panel", "set_docked", True),
    ]


def test_toggle_alternates_undock_and_dock(env):
    env.controller.toggle()
    assert env.controller.docked is False
    env.controller.toggle()
    assert env.controller.docked is True
