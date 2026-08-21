from pathlib import Path

from dzll_launcher import server_companion_ui


ROOT = Path(__file__).resolve().parents[1]


class Label:
    def __init__(self):
        self.text = ""
        self.visible = False
        self.opacity = 1.0

    def set_text(self, text):
        self.text = str(text)

    def set_visible(self, visible):
        self.visible = bool(visible)

    def set_opacity(self, opacity):
        self.opacity = float(opacity)


class PanelHarness:
    _clear_join_status_if_current = (
        server_companion_ui.ServerCompanionPanel._clear_join_status_if_current
    )
    _join_status_flash_tick = (
        server_companion_ui.ServerCompanionPanel._join_status_flash_tick
    )

    def __init__(self):
        self.join_status_label = Label()
        self._join_status_flash_timer_id = 0
        self._join_status_flash_step = 0
        self._join_status_clear_timer_id = 0
        self._join_status_generation = 0
        self._snapshot = {"server": "unchanged"}
        self.unrelated_state = {"alerts": True, "docked": True}

    def set_join_status(self, *args, **kwargs):
        return server_companion_ui.ServerCompanionPanel.set_join_status(
            self, *args, **kwargs,
        )


class TimerHarness:
    def __init__(self, monkeypatch):
        self.next_id = 1
        self.scheduled = {}
        self.removed = []

        def timeout_add_seconds(interval, callback, *args):
            source_id = self.next_id
            self.next_id += 1
            self.scheduled[source_id] = (int(interval), callback, args)
            return source_id

        monkeypatch.setattr(
            server_companion_ui.GLib,
            "timeout_add_seconds",
            timeout_add_seconds,
        )
        monkeypatch.setattr(
            server_companion_ui.GLib,
            "source_remove",
            self.removed.append,
        )

    def fire(self, source_id):
        _interval, callback, args = self.scheduled[source_id]
        return callback(*args)


def test_transient_join_status_clears_after_twenty_seconds(monkeypatch):
    timers = TimerHarness(monkeypatch)
    panel = PanelHarness()

    panel.set_join_status("Mod download cancelled")

    source_id = panel._join_status_clear_timer_id
    assert timers.scheduled[source_id][0] == 20
    assert panel.join_status_label.text == "Mod download cancelled"
    assert timers.fire(source_id) is False
    assert panel.join_status_label.text == ""
    assert panel.join_status_label.visible is False


def test_older_timer_cannot_clear_newer_join_status(monkeypatch):
    timers = TimerHarness(monkeypatch)
    panel = PanelHarness()
    panel.set_join_status("Preparing mods...")
    old_source_id = panel._join_status_clear_timer_id

    panel.set_join_status("Connecting to server...")
    new_source_id = panel._join_status_clear_timer_id

    assert old_source_id in timers.removed
    assert timers.fire(old_source_id) is False
    assert panel.join_status_label.text == "Connecting to server..."
    assert panel.join_status_label.visible is True
    assert new_source_id != old_source_id


def test_repeated_updates_replace_timeout_and_latest_timer_clears(monkeypatch):
    timers = TimerHarness(monkeypatch)
    panel = PanelHarness()
    panel.set_join_status("Preparing mods...")
    first_source_id = panel._join_status_clear_timer_id
    panel.set_join_status("Preparing mods...")
    second_source_id = panel._join_status_clear_timer_id

    assert first_source_id in timers.removed
    assert second_source_id != first_source_id
    assert timers.fire(second_source_id) is False
    assert panel.join_status_label.text == ""
    assert panel._join_status_clear_timer_id == 0


def test_timeout_clear_does_not_change_companion_state(monkeypatch):
    timers = TimerHarness(monkeypatch)
    panel = PanelHarness()
    snapshot = panel._snapshot
    unrelated = dict(panel.unrelated_state)
    panel.set_join_status("Mod download cancelled")

    timers.fire(panel._join_status_clear_timer_id)

    assert panel._snapshot is snapshot
    assert panel.unrelated_state == unrelated


def test_explicitly_persistent_join_status_has_no_clear_timer(monkeypatch):
    timers = TimerHarness(monkeypatch)
    panel = PanelHarness()

    panel.set_join_status("Waiting for user action", transient=False)

    assert panel._join_status_clear_timer_id == 0
    assert timers.scheduled == {}
    assert panel.join_status_label.text == "Waiting for user action"
    assert panel.join_status_label.visible is True


def test_steamcmd_login_required_is_explicitly_persistent():
    source = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    login_status = source.split('"SteamCMD Login Required"', 1)[1].split(
        "return self._steamcmd_overlay_ui", 1,
    )[0]
    assert "transient=False" in login_status
