import json
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import window as window_module
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


IP = "1.2.3.4"
GPORT = 2302
QPORT = 2303


def success_result(mods=None):
    result = {
        "ok": True,
        "ping_ms": 42,
        "players": 5,
        "max_players": 60,
        "queue": 0,
        "time": "12:00",
        "password": False,
    }
    if mods is not None:
        result["mods"] = mods
    return result


def make_host(obj):
    key = fav_key(IP, GPORT)
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        _browser_live_offline_streaks={},
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
    return host, key


def test_browser_live_tick_applies_mods_from_successful_rules_query():
    obj = ServerObject(ip=IP, gport=GPORT, qport=QPORT, ping=42, players=5, max_players=60, time="12:00", queue=0)
    host, key = make_host(obj)
    mods = {"ok": True, "mods": [{"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}]}

    DZLLWindow._apply_live_results(host, [(key, success_result(mods))], reason="browser-live")

    assert obj.mod_count == 1
    assert json.loads(obj.mods_json) == [{"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}]


def test_browser_live_tick_leaves_mods_untouched_when_rules_query_failed():
    obj = ServerObject(
        ip=IP, gport=GPORT, qport=QPORT, ping=42, players=5, max_players=60, time="12:00", queue=0,
        mods_json="[]", mod_count=0,
    )
    host, key = make_host(obj)
    mods = {"ok": False, "err": "timeout"}

    DZLLWindow._apply_live_results(host, [(key, success_result(mods))], reason="browser-live")

    assert obj.mod_count == 0
    assert obj.mods_json == "[]"


def test_browser_live_tick_ignores_mods_key_when_absent():
    obj = ServerObject(
        ip=IP, gport=GPORT, qport=QPORT, ping=42, players=5, max_players=60, time="12:00", queue=0,
        mods_json="[]", mod_count=0,
    )
    host, key = make_host(obj)

    DZLLWindow._apply_live_results(host, [(key, success_result(None))], reason="browser-live")

    assert obj.mod_count == 0
    assert obj.mods_json == "[]"


def test_full_sweep_batch_does_not_apply_mods_even_if_present():
    obj = ServerObject(
        ip=IP, gport=GPORT, qport=QPORT, ping=42, players=5, max_players=60, time="12:00", queue=0,
        mods_json="[]", mod_count=0,
    )
    host, key = make_host(obj)
    mods = {"ok": True, "mods": [{"steamWorkshopId": 1559212036, "name": "DayZ-Expansion-Core"}]}

    DZLLWindow._apply_live_results(host, [(key, success_result(mods))], reason="batch")

    assert obj.mod_count == 0
    assert obj.mods_json == "[]"


from concurrent.futures import ThreadPoolExecutor


def tick_host(*, manual, monkeypatch):
    key = fav_key(IP, GPORT)
    obj = ServerObject(ip=IP, gport=GPORT, qport=QPORT, ping=-1)
    store = SimpleNamespace(rows=[obj])
    store.get_n_items = lambda: len(store.rows)
    store.get_item = lambda idx: store.rows[idx]
    scroller = SimpleNamespace(get_vadjustment=lambda: None)
    applied = []
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        column_view_store=store,
        scroller=scroller,
        retained_servers={key: {"manual": manual}} if manual is not None else {},
        _browser_live_token=0,
        _browser_live_last_refresh={},
        _browser_live_target_keys=set(),
        _browser_live_inflight=False,
        _browser_live_executor=ThreadPoolExecutor(max_workers=2),
        _shutdown_cleanup_done=False,
        _scrollbar_interaction=None,
        _browser_live_filter_cooldown_until=0.0,
        _browser_live_scroll_active_until=0.0,
        _steam_ugc_worker_in_progress=False,
        _mod_download_backend_active="",
        get_visible=lambda: True,
        get_mapped=lambda: True,
        get_surface=lambda: None,
        _refresh_last_played_calendar=lambda: None,
        _apply_browser_live_results=lambda *args, **kwargs: applied.append(args),
    )
    host._browser_live_should_pause = MethodType(DZLLWindow._browser_live_should_pause, host)
    host._browser_live_candidate_groups = MethodType(DZLLWindow._browser_live_candidate_groups, host)
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args, **kwargs: fn(*args, **kwargs))

    real_thread_cls = window_module.threading.Thread

    class _ImmediateRunner:
        def __init__(self, target):
            self._target = target

        def start(self):
            self._target()

    def fake_thread(target=None, daemon=None, **kwargs):
        if daemon and target is not None and not kwargs:
            return _ImmediateRunner(target)
        return real_thread_cls(target=target, daemon=daemon, **kwargs)

    monkeypatch.setattr(window_module.threading, "Thread", fake_thread)
    return host, key, applied


def test_browser_live_tick_queries_mods_for_manual_direct_connect_servers(monkeypatch):
    monkeypatch.setattr(
        window_module,
        "query_server_live",
        lambda *_a, **_kw: {"ok": True, "ping_ms": 10, "players": 1, "max_players": 60, "queue": 0, "time": "12:00", "password": False},
    )
    mods_calls = []
    monkeypatch.setattr(
        window_module,
        "query_server_mods",
        lambda ip, qport, **kwargs: mods_calls.append((ip, qport)) or {"ok": True, "mods": []},
    )
    host, key, applied = tick_host(manual=True, monkeypatch=monkeypatch)

    DZLLWindow._browser_live_tick(host)

    assert mods_calls == [(IP, QPORT)]
    assert len(applied) == 1
    _token, _targets, results = applied[0]
    result_key, info = results[0]
    assert result_key == key
    assert info.get("mods") == {"ok": True, "mods": []}


def test_browser_live_tick_skips_mods_query_for_non_manual_servers(monkeypatch):
    monkeypatch.setattr(
        window_module,
        "query_server_live",
        lambda *_a, **_kw: {"ok": True, "ping_ms": 10, "players": 1, "max_players": 60, "queue": 0, "time": "12:00", "password": False},
    )
    mods_calls = []
    monkeypatch.setattr(
        window_module,
        "query_server_mods",
        lambda ip, qport, **kwargs: mods_calls.append((ip, qport)) or {"ok": True, "mods": []},
    )
    host, key, applied = tick_host(manual=False, monkeypatch=monkeypatch)

    DZLLWindow._browser_live_tick(host)

    assert mods_calls == []
    _token, _targets, results = applied[0]
    _result_key, info = results[0]
    assert "mods" not in info
