"""Row eye button: a second click stops monitoring but keeps the panel open."""
from types import MethodType, SimpleNamespace

from dzll_launcher import column_view
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


def _server(ip="198.51.100.20", gport=2302, qport=27016):
    obj = ServerObject()
    obj.ip = ip
    obj.gport = gport
    obj.qport = qport
    return obj


def _host(enabled=True, monitored=None):
    calls = []
    host = SimpleNamespace(
        settings={"show_server_companion": enabled},
        _server_companion_obj=monitored,
        calls=calls,
    )

    def set_server(obj, persist=True):
        calls.append(("set_server", obj))
        host._server_companion_obj = obj

    def clear():
        calls.append(("clear",))
        host._server_companion_obj = None

    def set_enabled(value):
        calls.append(("set_enabled", value))
        host.settings["show_server_companion"] = bool(value)

    host.set_server_companion_server = set_server
    host.clear_server_companion = clear
    host.set_server_companion_enabled = set_enabled
    for name in (
        "_server_companion_identity_for_obj",
        "_is_server_companion_monitoring_obj",
        "_monitor_server_companion_for_obj",
    ):
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def test_first_click_monitors_and_opens_companion():
    host = _host(enabled=False)
    obj = _server()
    host._monitor_server_companion_for_obj(obj)
    assert host.calls == [("set_server", obj), ("set_enabled", True)]
    assert host._is_server_companion_monitoring_obj(obj)


def test_second_click_stops_monitoring_but_keeps_companion_open():
    host = _host(enabled=False)
    obj = _server()
    host._monitor_server_companion_for_obj(obj)
    host.calls.clear()
    host._monitor_server_companion_for_obj(obj)
    assert host.calls == [("clear",)]
    assert host.settings["show_server_companion"] is True
    assert not host._is_server_companion_monitoring_obj(obj)


def test_second_click_matches_by_identity_not_object():
    host = _host(enabled=True, monitored=_server())
    host._monitor_server_companion_for_obj(_server())  # same ip:gport, new row object
    assert host.calls == [("clear",)]


def test_click_on_another_server_switches_instead_of_stopping():
    host = _host(enabled=True, monitored=_server())
    other = _server(ip="203.0.113.5")
    host._monitor_server_companion_for_obj(other)
    assert host.calls == [("set_server", other)]
    assert host._is_server_companion_monitoring_obj(other)


def test_third_click_monitors_again():
    host = _host(enabled=True)
    obj = _server()
    for _ in range(3):
        host._monitor_server_companion_for_obj(obj)
    assert [c[0] for c in host.calls] == ["set_server", "clear", "set_server"]
    assert host._is_server_companion_monitoring_obj(obj)


def test_companion_off_with_remembered_server_monitors_instead_of_clearing():
    # With SC turned off the row is not shown as monitored, so a click must
    # turn SC on for that server rather than clearing it.
    host = _host(enabled=False, monitored=_server())
    obj = _server()
    host._monitor_server_companion_for_obj(obj)
    assert host.calls == [("set_server", obj), ("set_enabled", True)]


def test_active_tooltip_mentions_stopping():
    import inspect

    source = inspect.getsource(column_view.build_server_column_view)
    assert 'active_tooltip_text="Currently monitoring this server\\nClick to stop monitoring"' in source
