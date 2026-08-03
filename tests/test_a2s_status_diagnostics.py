from types import SimpleNamespace

from dzll_launcher.a2s_status_diagnostics import (
    A2SStatusDiagnostics,
    diagnostics_enabled,
    parse_server_filter,
)
from dzll_launcher.window import DZLLWindow, fav_key


IP = "172.111.51.149"
GPORT = 2502
QPORT = 2503


def logger(env):
    lines = []
    value = A2SStatusDiagnostics(
        environ=env,
        writer=lambda line, flush=True: lines.append(line),
        clock=lambda: 123.5,
    )
    return value, lines


def test_diagnostics_disabled_by_default():
    value, lines = logger({})
    assert not diagnostics_enabled({})
    assert not value.emit("query-start", ip=IP, gport=GPORT, qport=QPORT)
    assert lines == []


def test_non_debug_query_result_contract_is_unchanged(monkeypatch):
    from dzll_launcher import a2s
    from dzll_launcher import a2s_status_diagnostics as diagnostics_module
    from dzll_launcher.live import query_server_live

    info = SimpleNamespace(
        ping=0.05,
        player_count=12,
        max_players=60,
        keywords="12:34,lqs2",
        password_protected=False,
    )
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(a2s, "info", lambda address, timeout: info)
    assert query_server_live(IP, QPORT) == {
        "ok": True,
        "ping_ms": 50,
        "players": 12,
        "max_players": 60,
        "queue": 2,
        "time": "12:34",
        "password": False,
    }


def test_server_filter_accepts_dzll_game_port_identity_and_logs_resolved_query_port():
    value, lines = logger(
        {
            "DZLL_A2S_STATUS_DEBUG": "1",
            "DZLL_A2S_STATUS_DEBUG_SERVER": f"{IP}:{GPORT}",
        }
    )
    assert parse_server_filter(f"{IP}:{GPORT}") == (IP, GPORT)
    assert value.emit("query-start", ip=IP, gport=GPORT, qport=QPORT)
    assert "gport=2502" in lines[0]
    assert "qport=2503" in lines[0]
    assert not value.emit("query-start", ip="192.0.2.1", gport=GPORT, qport=QPORT)


def test_info_and_players_failures_in_one_cycle_are_identified_without_redefining_streaks():
    value, lines = logger({"DZLL_A2S_STATUS_DEBUG": "1"})
    identity = dict(ip=IP, gport=GPORT, qport=QPORT, generation=7)
    assert value.observe_result(cycle_id="browser:7:key", classification="timeout", query="INFO", **identity) == 1
    assert value.observe_result(cycle_id="browser:7:key", classification="socket-error", query="PLAYERS", **identity) == 2
    assert "query=INFO" in lines[0] and "cycle_failure=first" in lines[0]
    assert "query=PLAYERS" in lines[1] and "cycle_failure=second" in lines[1]


def _host(ping=42, streak=0):
    obj = SimpleNamespace(
        ip=IP,
        gport=GPORT,
        qport=QPORT,
        ping=ping,
        players=10,
        max_players=60,
        password=False,
        time="--:--",
        queue=-1,
    )
    key = fav_key(IP, GPORT)
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        _browser_live_offline_streaks=({key: streak} if streak else {}),
        _browser_live_token=7,
        column_view_store=object(),
        live={key: {"offline": ping < 0}},
        dead={},
        _dead_session=set(),
        sort_key="ping",
        sort_asc=True,
        _active_filter_depends_on_live_values=lambda: False,
        _debug_browser_reorder=lambda *args, **kwargs: None,
        _debug_sort_note_live_update=lambda *args, **kwargs: None,
        _apply_titlebar_counts=lambda: None,
        _update_row_sort_ping=lambda value: None,
        _update_row_sort_players=lambda value: None,
        _on_filter_changed=lambda **kwargs: None,
    )
    return host, key, obj


def test_single_timeout_does_not_apply_visible_offline_under_current_rule(monkeypatch):
    lines = []
    from dzll_launcher import a2s_status_diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {"DZLL_A2S_STATUS_DEBUG": "1"})
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "writer", lambda line, flush=True: lines.append(line))
    host, key, obj = _host()
    result = {"ok": False, "err": "timed out", "_a2s_diag": {"classification": "timeout", "cycle_id": "browser:7:key"}}
    DZLLWindow._apply_live_results(host, [(key, result)], reason="browser-live")
    assert obj.ping == 42
    assert host._browser_live_offline_streaks[key] == 1
    assert any("requested_status=unchanged" in line and "applied=False" in line for line in lines)


def test_late_stale_failure_is_logged_as_rejected_not_applied(monkeypatch):
    lines = []
    from dzll_launcher import a2s_status_diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {"DZLL_A2S_STATUS_DEBUG": "1"})
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "writer", lambda line, flush=True: lines.append(line))
    host, key, _obj = _host()
    DZLLWindow._diagnose_rejected_browser_results(
        host, 6, {key}, [(key, {"ok": False, "err": "timed out"})], "stale", "newer-generation"
    )
    assert any("classification=stale" in line and "applied=False" in line for line in lines)
    assert all("disposition=applied" not in line for line in lines)


def test_success_logs_streak_reset_and_online_application(monkeypatch):
    lines = []
    from dzll_launcher import a2s_status_diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {"DZLL_A2S_STATUS_DEBUG": "1"})
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "writer", lambda line, flush=True: lines.append(line))
    host, key, obj = _host(ping=-1, streak=2)
    result = {
        "ok": True,
        "ping_ms": 51,
        "players": 11,
        "max_players": 60,
        "time": "12:34",
        "password": False,
        "_a2s_diag": {"classification": "success", "cycle_id": "browser:7:key"},
    }
    DZLLWindow._apply_live_results(host, [(key, result)], reason="browser-live")
    assert obj.ping == 51
    assert key not in host._browser_live_offline_streaks
    assert any(
        "previous_streak=2" in line
        and "streak_action=reset" in line
        and "requested_status=ONLINE" in line
        and "reason=success-recovery" in line
        for line in lines
    )
