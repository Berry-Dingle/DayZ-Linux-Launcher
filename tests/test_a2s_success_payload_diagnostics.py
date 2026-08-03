from types import SimpleNamespace

from dzll_launcher import a2s
from dzll_launcher import a2s_status_diagnostics as diagnostics_module
from dzll_launcher.live import query_server_live
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


IP = "172.111.51.149"
GPORT = 2502
QPORT = 2503


def enable_target(monkeypatch):
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
    return lines


def host_with_obj(*, players=4, ping=21, game_time="12:00", queue=2):
    obj = ServerObject(
        ip=IP,
        gport=GPORT,
        qport=QPORT,
        players=players,
        max_players=60,
        ping=ping,
        time=game_time,
        timewarp=12.0,
        queue=queue,
        password=False,
    )
    key = fav_key(IP, GPORT)
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        _browser_live_offline_streaks={key: 1},
        _browser_live_token=9,
        column_view_store=object(),
        live={key: {"offline": False}},
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


def success_result(**updates):
    result = {
        "ok": True,
        "ping_ms": 30,
        "players": 7,
        "max_players": 60,
        "queue": 2,
        "time": "12:34",
        "password": False,
        "_a2s_diag": {
            "classification": "success",
            "cycle_id": "browser:9:target",
            "generation": 9,
            "parsed_info": {"players": 7, "max_players": 60, "ping_ms": 30, "game_time": "12:34"},
        },
    }
    result.update(updates)
    return result


def test_success_payload_logs_raw_parsed_info_values(monkeypatch):
    lines = enable_target(monkeypatch)
    info = SimpleNamespace(
        ping=0.031,
        player_count=7,
        max_players=60,
        keywords="dayz,12:34,lqs2",
        password_protected=True,
        server_name="Target Server",
        map_name="chernarusplus",
        folder="dayz",
        game="DayZ",
        version="1.28",
        edf=0xA0,
        port=GPORT,
    )
    monkeypatch.setattr(a2s, "info", lambda address, timeout, _trace=None: info)
    result = query_server_live(IP, QPORT, gport=GPORT, cycle_id="parsed")
    line = next(value for value in lines if "event=info-payload-parsed" in value)
    assert result["players"] == 7
    assert "server_name=Target_Server" in line
    assert "players=7" in line and "max_players=60" in line
    assert "ping_ms=31" in line and "game_time=12:34" in line
    assert "time_acceleration=not-in-a2s-info" in line
    assert "queue=2" in line and "password=True" in line


def test_before_after_changed_fields_notify_and_visible_readback(monkeypatch):
    lines = enable_target(monkeypatch)
    host, key, obj = host_with_obj()
    DZLLWindow._apply_live_results(host, [(key, success_result())], reason="browser-live")
    before = next(value for value in lines if "event=live-fields-before-apply" in value)
    after = next(value for value in lines if "event=live-fields-after-apply" in value)
    visible = next(value for value in lines if "event=visible-row-after-apply" in value)
    assert "changed_fields=players,ping,time" in before
    assert "unchanged_fields=max_players,queue,timewarp,password,status" in before
    assert "written_fields=time,players,max_players,queue,password,ping" in after
    assert "notify_signals=" in after and "players" in after and "ping" in after and "time" in after
    assert obj.players == 7 and obj.ping == 30 and obj.time == "12:34"
    assert "visible_readback_source=model" in visible
    assert "players':_7" in visible and "ping':_30" in visible and "time':_'12:34'" in visible


def test_unchanged_success_is_reported_as_unchanged(monkeypatch):
    lines = enable_target(monkeypatch)
    host, key, _obj = host_with_obj(players=4, ping=21, game_time="12:00", queue=2)
    same = success_result(ping_ms=21, players=4, time="12:00")
    same["_a2s_diag"]["parsed_info"] = {
        "players": 4,
        "max_players": 60,
        "ping_ms": 21,
        "game_time": "12:00",
    }
    DZLLWindow._apply_live_results(host, [(key, same)], reason="browser-live")
    before = next(value for value in lines if "event=live-fields-before-apply" in value)
    after = next(value for value in lines if "event=live-fields-after-apply" in value)
    assert "changed_fields=none" in before
    assert "unchanged_fields=players,max_players,queue,ping,time,timewarp,password,status" in before
    assert "changed_fields=none" in after


def test_success_diagnostics_disabled_by_default(monkeypatch):
    lines = []
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "writer",
        lambda line, flush=True: lines.append(line),
    )
    host, key, obj = host_with_obj()
    DZLLWindow._apply_live_results(host, [(key, success_result())], reason="browser-live")
    assert obj.players == 7 and obj.ping == 30
    assert lines == []
