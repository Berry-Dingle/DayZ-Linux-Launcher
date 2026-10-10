import pytest

from dzll_launcher import app as app_module


def test_notification_application_identity_is_set_before_app_runs(monkeypatch):
    calls = []

    class FakeApp:
        restart_requested = False

        def __init__(self):
            calls.append("create")

        def run(self, _argv):
            calls.append("run")
            return 0

    monkeypatch.setattr(app_module.GLib, "set_application_name", lambda name: calls.append(name))
    monkeypatch.setattr(app_module, "DZLLApp", FakeApp)

    with pytest.raises(SystemExit) as result:
        app_module.main()

    assert result.value.code == 0
    assert calls == ["DayZ Linux Launcher", "create", "run"]
