import os
import subprocess

import pytest

from dzll_launcher import server_companion_audio as audio


@pytest.fixture
def env(monkeypatch):
    """Fake player lookup, audio files and process launch."""
    state = {"players": {"ffplay"}, "missing": set(), "launched": [], "popen_error": None}
    monkeypatch.setattr(
        audio.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in state["players"] else None,
    )
    monkeypatch.setattr(
        audio.os.path, "exists",
        lambda path: os.path.basename(path) not in state["missing"],
    )

    def popen(cmd, stdout=None, stderr=None):
        if state["popen_error"] is not None:
            raise state["popen_error"]
        state["launched"].append((cmd, stdout, stderr))

    monkeypatch.setattr(audio.subprocess, "Popen", popen)
    return state


def play(event="online", *, volume=80, sound="female"):
    debug = []
    result = audio.play_alert_sound(event, volume=volume, sound=sound, debug=debug.append)
    return result, debug


def test_sound_table_is_unchanged():
    assert audio.SERVER_COMPANION_ALERT_SOUNDS == {
        "online": {"female": "OnlineF.mp3", "male": "OnlineM.mp3", "beep": "serverOnlineBeep.mp3"},
        "restart_warning": {"female": "RestartF.mp3", "male": "RestartM.mp3", "beep": "serverOnlineBeep.mp3"},
    }
    assert audio.AUDIO_DIR == os.path.join(os.path.dirname(audio.__file__), "audio")


def test_bundled_alert_files_exist():
    for sounds in audio.SERVER_COMPANION_ALERT_SOUNDS.values():
        for name in sounds.values():
            assert os.path.isfile(os.path.join(audio.AUDIO_DIR, name))


def test_ffplay_command_uses_scaled_volume_and_detached_output(env):
    result, _debug = play("restart_warning", volume=55, sound="male")

    assert result == (True, None)
    assert env["launched"] == [(
        ["/usr/bin/ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-af", "volume=0.55",
         os.path.join(audio.AUDIO_DIR, "RestartM.mp3")],
        subprocess.DEVNULL, subprocess.DEVNULL,
    )]


def test_mpg123_fallback_uses_integer_gain(env):
    env["players"] = {"mpg123"}

    assert play(volume=150)[0] == (True, None)
    assert env["launched"][0][0] == ["/usr/bin/mpg123", "-g", "100", os.path.join(audio.AUDIO_DIR, "OnlineF.mp3")]


@pytest.mark.parametrize("volume", [0, -5])
def test_zero_volume_is_a_silent_success(env, volume):
    result, debug = play(volume=volume)

    assert result == (True, None)
    assert env["launched"] == []
    assert debug == ["audio skipped: event='online' volume=0"]


def test_invalid_volume_and_unknown_choices_fall_back(env):
    result, _debug = play("bogus", volume="loud", sound="nope")

    assert result == (True, None)
    cmd = env["launched"][0][0]
    assert "volume=0.80" in cmd
    assert cmd[-1] == os.path.join(audio.AUDIO_DIR, "OnlineF.mp3")


def test_missing_selected_file_falls_back_to_female_voice(env):
    env["missing"] = {"OnlineM.mp3"}

    result, debug = play(sound="male")

    assert result == (True, None)
    assert env["launched"][0][0][-1] == os.path.join(audio.AUDIO_DIR, "OnlineF.mp3")
    assert any(line.startswith("audio fallback selected: filename='OnlineF.mp3'") for line in debug)


def test_missing_files_report_failure_without_launching(env):
    env["missing"] = {"OnlineM.mp3", "OnlineF.mp3"}

    assert play(sound="male")[0] == (False, "Alert audio file missing")
    assert env["launched"] == []


def test_no_player_reports_install_hint(env):
    env["players"] = set()

    result, debug = play()

    assert result == (False, "Alert audio unavailable: install FFmpeg / ffplay")
    assert debug[-1] == "audio unavailable: no ffplay or mpg123 found"


def test_launch_failure_is_contained(env):
    env["popen_error"] = OSError("boom")

    result, debug = play()

    assert result == (False, "Alert audio unavailable: install FFmpeg / ffplay")
    assert debug[-1] == "audio process launch failed: OSError('boom')"


class FakeNotification:
    def __init__(self, title):
        self.title = title
        self.body = None

    @classmethod
    def new(cls, title):
        return cls(title)

    def set_body(self, body):
        self.body = body


class App:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send_notification(self, notification_id, notification):
        if self.error is not None:
            raise self.error
        self.sent.append((notification_id, notification.title, notification.body))


@pytest.fixture
def notifications(monkeypatch):
    monkeypatch.setattr(audio.Gio, "Notification", FakeNotification)


@pytest.mark.parametrize("snapshot, body", [
    ({"name": "  Server X  "}, "Server X"),
    ({"name": ""}, None),
    ({}, None),
    (None, None),
])
def test_back_online_notification_contents(notifications, snapshot, body):
    app = App()

    assert audio.send_back_online_notification(lambda: app, snapshot) is None
    assert app.sent == [("dzll-server-companion-back-online", "Server back online", body)]


def test_back_online_notification_never_raises(notifications):
    def no_app():
        raise RuntimeError("no application")

    audio.send_back_online_notification(lambda: None, {"name": "A"})
    audio.send_back_online_notification(no_app, {"name": "A"})
    audio.send_back_online_notification(lambda: App(RuntimeError("bus down")), {"name": "A"})
