from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import a2s_status_diagnostics as diagnostics_module
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


IP = "172.111.51.149"
GPORT = 2502
QPORT = 2503


def make_host(*, ping=27, streak=0):
    key = fav_key(IP, GPORT)
    obj = ServerObject(
        ip=IP,
        gport=GPORT,
        qport=QPORT,
        players=4,
        max_players=60,
        ping=ping,
        time="12:00",
        queue=0,
    )
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        _browser_live_offline_streaks={key: streak} if streak else {},
        _browser_live_token=9,
        _browser_live_target_keys={key},
        _browser_live_scroll_active_until=0.0,
        _browser_live_inflight=True,
        column_view_store=object(),
        live={key: {"offline": ping < 0}},
        dead={},
        _dead_session=set(),
        sort_key="ping",
        sort_asc=True,
        _defer_browser_scrollbar_work=lambda *args, **kwargs: False,
        _active_filter_depends_on_live_values=lambda: False,
        _debug_browser_reorder=lambda *args, **kwargs: None,
        _debug_sort_note_live_update=lambda *args, **kwargs: None,
        _apply_titlebar_counts=lambda: None,
        _update_row_sort_ping=lambda value: None,
        _update_row_sort_players=lambda value: None,
        _on_filter_changed=lambda **kwargs: None,
    )
    host._apply_live_results = MethodType(DZLLWindow._apply_live_results, host)
    return host, key, obj


def success(ping):
    return {
        "ok": True,
        "ping_ms": ping,
        "players": 4,
        "max_players": 60,
        "queue": 0,
        "time": "12:00",
        "password": False,
        "_a2s_diag": {
            "classification": "success",
            "cycle_id": "browser:9:target",
            "generation": 9,
            "parsed_info": {"ping_ms": ping},
        },
    }


def apply(host, key, result):
    return DZLLWindow._apply_browser_live_results(host, 9, {key}, [(key, result)])


@pytest.mark.parametrize("sample", [26, 25, 28, 29])
def test_one_and_two_ms_changes_are_dampened_in_both_directions(sample):
    host, key, obj = make_host()
    notifications = []
    obj.connect("notify::ping", lambda *_args: notifications.append(obj.ping))
    apply(host, key, success(sample))
    assert obj.ping == 27
    assert notifications == []


@pytest.mark.parametrize("sample", [24, 30])
def test_three_ms_changes_are_applied_in_both_directions(sample):
    host, key, obj = make_host()
    notifications = []
    obj.connect("notify::ping", lambda *_args: notifications.append(obj.ping))
    apply(host, key, success(sample))
    assert obj.ping == sample
    assert notifications == [sample]


@pytest.mark.parametrize("sample", [20, 35])
def test_larger_ping_changes_are_applied(sample):
    host, key, obj = make_host()
    apply(host, key, success(sample))
    assert obj.ping == sample


def test_unchanged_ping_does_not_emit_an_update():
    host, key, obj = make_host()
    notifications = []
    obj.connect("notify::ping", lambda *_args: notifications.append(obj.ping))
    apply(host, key, success(27))
    assert obj.ping == 27
    assert notifications == []


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "neutral": True, "outcome": "alive-but-info-unavailable"},
        {"ok": False, "err": "timeout"},
    ],
)
def test_neutral_and_first_offline_failure_preserve_ping(result):
    host, key, obj = make_host()
    apply(host, key, result)
    assert obj.ping == 27


@pytest.mark.parametrize("sample, expected", [(25, "True"), (24, "False")])
def test_ping_dampened_diagnostic_matches_three_ms_threshold(monkeypatch, sample, expected):
    lines = []
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "environ",
        {
            "DZLL_A2S_STATUS_DEBUG": "1",
            "DZLL_A2S_STATUS_DEBUG_SERVER": f"{IP}:{GPORT}",
        },
    )
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "writer",
        lambda line, flush=True: lines.append(line),
    )
    host, key, _obj = make_host()
    apply(host, key, success(sample))
    line = next(value for value in lines if "event=live-fields-before-apply" in value)
    assert f"ping_dampened={expected}" in line
