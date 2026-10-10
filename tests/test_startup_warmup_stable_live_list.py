from __future__ import annotations

import ast
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import config, window
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.settings_ui import SettingsUI
from dzll_launcher.window import DZLLWindow, fav_key


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.splices = 0

    def get_n_items(self):
        return len(self.rows)

    def get_item(self, index):
        return self.rows[index]

    def splice(self, index, count, replacement):
        self.splices += 1
        self.rows[index:index + count] = list(replacement)


def bind(host, *names):
    for name in names:
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))
    return host


def endpoints(store):
    return [(obj.ip, obj.gport) for obj in store.rows]


def test_startup_queue_uses_both_top_rank_bands_before_lower_bands(monkeypatch):
    ranks = [1600, 950, 70, 2500, 110, 5, 999]
    rows = [
        {"ip": f"10.0.0.{index}", "gport": 2302, "qport": 2303, "bm_rank": rank}
        for index, rank in enumerate(ranks, 1)
    ]
    captured = []
    host = SimpleNamespace(
        _obj_by_key={},
        _startup_live_generation=0,
        _server_companion_rows_loaded=False,
        retained_servers={},
        empty_label=SimpleNamespace(set_text=lambda *_: None, set_visible=lambda *_: None),
        _set_map_choices=lambda *_: None,
        _restore_server_companion_if_enabled=lambda: None,
        _set_updating=lambda *_: None,
        _begin_status_refresh_sweep=lambda keys, warmup_count=None: captured.append((keys, warmup_count)),
        _apply_titlebar_counts=lambda: None,
        _offline_recheck_tick=lambda: False,
        _auto_sort_lowest_ping_after_startup=lambda: None,
        _complete_startup_presentation=lambda: None,
    )

    def fake_load_rows_into_store(valid, on_loaded=None):
        host._obj_by_key.update({
            fav_key(row["ip"], row["gport"]): SimpleNamespace(bm_rank=row["bm_rank"])
            for row in valid
        })
        if on_loaded is not None:
            on_loaded(True)

    host._load_rows_into_store = fake_load_rows_into_store
    bind(host, "_bm_live_group")
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda *_: 1)

    DZLLWindow._apply_db_rows(host, rows, True)

    keys, warmup_count = captured.pop()
    ordered_ranks = [host._obj_by_key[key].bm_rank for key in keys]
    assert ordered_ranks == [70, 5, 950, 110, 999, 1600, 2500]
    assert warmup_count == 5
    assert all(rank <= 1000 for rank in ordered_ranks[:warmup_count])


def warmup_host(monkeypatch, *, warmup_count=80, total=100):
    scheduled = []
    removed = []
    events = []
    monkeypatch.setattr(window.GLib, "timeout_add_seconds", lambda seconds, callback, generation: scheduled.append((seconds, callback, generation)) or 41)
    monkeypatch.setattr(window.GLib, "timeout_add", lambda *_: 0)
    monkeypatch.setattr(window.GLib, "source_remove", removed.append)
    host = SimpleNamespace(
        _status_refresh_generation=1,
        _status_refresh_running=True,
        _status_refresh_flush_id=0,
        _status_refresh_buffer=[],
        _status_refresh_total=total,
        _status_refresh_completed=0,
        _startup_warmup_timeout_id=0,
        _shutdown_cleanup_done=False,
        _startup_presentation=None,
        _flush_status_refresh_results=lambda _generation: events.append("flush") or False,
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort") or False,
        _complete_startup_presentation=lambda: events.append("reveal") or False,
        _set_updating=lambda *_: None,
        _schedule_status_refresh_progress_update=lambda *_: None,
    )
    bind(host, "_begin_startup_warmup_gate", "_status_refresh_attempt_finished", "_on_startup_warmup_timeout", "_complete_startup_warmup")
    keys = [f"server-{n}" for n in range(total)]
    host._begin_startup_warmup_gate(1, keys, warmup_count)
    return host, keys, scheduled, removed, events


def test_warmup_reveals_after_partial_top_1000_completion_and_sorts_first(monkeypatch):
    host, keys, scheduled, removed, events = warmup_host(monkeypatch)
    assert host._startup_warmup_target == 52  # ceil(65% of 80), beyond old first 50
    assert scheduled[0][0] == config.STARTUP_WARMUP_MAX_WAIT_SECS
    for key in keys[:51]:
        host._status_refresh_attempt_finished(1, key=key)
    assert events == []
    host._status_refresh_attempt_finished(1, key=keys[51])
    assert events == ["flush", "sort", "reveal"]
    assert removed == [41]
    host._status_refresh_attempt_finished(1, key=keys[52])
    assert events == ["flush", "sort", "reveal"]


def test_warmup_max_wait_reveals_once_without_all_results(monkeypatch):
    host, keys, scheduled, _removed, events = warmup_host(monkeypatch)
    host._status_refresh_attempt_finished(1, key=keys[0])
    scheduled[0][1](scheduled[0][2])
    assert host._startup_warmup_completed == 1
    assert events == ["flush", "sort", "reveal"]
    scheduled[0][1](scheduled[0][2])
    assert events == ["flush", "sort", "reveal"]


def test_row_loading_has_no_blind_one_second_sort_timer():
    source = Path(window.__file__).read_text(encoding="utf-8")
    loader = source.split("def _load_rows_into_store", 1)[1].split("def _set_int_property_if_changed", 1)[0]
    assert "_auto_sort_lowest_ping_after_startup" not in loader


def test_cached_db_fallback_still_sorts_before_reveal():
    events = []
    host = SimpleNamespace(
        _startup_live_generation=0,
        _server_companion_rows_loaded=False,
        retained_servers={},
        _set_map_choices=lambda *_: None,
        _load_rows_into_store=lambda _rows, on_loaded=None: (on_loaded(True) if on_loaded else None),
        _restore_server_companion_if_enabled=lambda: None,
        _set_updating=lambda *_: None,
        _auto_sort_lowest_ping_after_startup=lambda: events.append("sort"),
        _complete_startup_presentation=lambda: events.append("reveal"),
    )
    DZLLWindow._apply_db_rows(host, [{"ip": "10.0.0.1", "gport": 2302, "qport": 2303}], False)
    assert events == ["sort", "reveal"]


def make_live_host():
    ranks = [50, 500, 1500]
    pings = [200, 100, 20]
    rows = []
    for index, (rank, ping) in enumerate(zip(ranks, pings), 1):
        obj = ServerObject(
            ip=f"10.0.0.{index}", gport=2302, qport=2303, name=f"Server {index}",
            bm_rank=rank, ping=ping, players=index, max_players=60,
            time="12:00", queue=0,
        )
        rows.append(obj)
    live = {fav_key(obj.ip, obj.gport): {"hide_high_ping": False, "offline": False} for obj in rows}
    state = {
        "live": live, "query": "", "mod_query_mode": False, "mod_query": (),
        "max_players_cutoff": 50,
        "one_pp_only": False, "three_pp_only": False, "no_password": False,
        "online_only": False, "selected_map": "All Maps",
    }
    host = SimpleNamespace(
        store=Store(rows), column_view_store=Store(),
        _obj_by_key={fav_key(obj.ip, obj.gport): obj for obj in rows},
        _browser_live_offline_streaks={}, _ping_cutoff_ms=250,
        live=live, dead={}, _dead_session=set(), favorites={}, _filter_state=state,
        settings={"pin_favorite_servers": False, "prioritise_trusted_servers": True},
        sort_key="ping", sort_asc=True,
        _active_filter_depends_on_live_values=lambda: True,
        _debug_sort_note_model_event=lambda *_: None,
        _browser_reorder_is_background_reason=lambda *_: False,
        _debug_browser_reorder=lambda *_, **__: None,
        _debug_sort_note_key_build=lambda *_: None,
        _debug_sort_note_live_update=lambda *_, **__: None,
        _apply_titlebar_counts=lambda: None,
        _snapshot_all_sort_keys=lambda: [setattr(obj, "sort_players", obj.players) for obj in rows],
        _reconcile_visible_store_membership_preserve_order=lambda **_: pytest.fail("background membership splice"),
    )
    bind(host, "_combined_filter_func", "_rebuild_column_view_store", "_apply_live_results", "_set_int_property_if_changed", "_update_row_sort_ping", "_update_row_sort_players")
    host._on_filter_changed = lambda **_: host._rebuild_column_view_store(reorder_reason="explicit-filter")
    host._rebuild_column_view_store(reorder_reason="initial")
    return host, rows


def success(*, ping=40, max_players=60, players=5):
    return {"ok": True, "ping_ms": ping, "max_players": max_players, "players": players,
            "queue": 0, "time": "12:00", "password": False}


@pytest.mark.parametrize(
    "updated, expected",
    [
        ({"ping": 300}, (300, 60)),
        ({"max_players": 20}, (200, 20)),
    ],
)
def test_browser_live_cutoffs_update_values_without_moving_trusted_bands(updated, expected):
    host, rows = make_live_host()
    before = endpoints(host.column_view_store)
    splices = host.column_view_store.splices
    key = fav_key(rows[0].ip, rows[0].gport)

    host._apply_live_results([(
        key, success(ping=updated.get("ping", 200), max_players=updated.get("max_players", 60))
    )], reason="browser-live")

    assert (rows[0].ping, rows[0].max_players) == expected
    assert host.live[key]["hide_high_ping"] is ("ping" in updated)
    assert endpoints(host.column_view_store) == before
    assert host.column_view_store.splices == splices
    assert [obj.bm_rank for obj in host.column_view_store.rows] == [50, 500, 1500]
    host._on_filter_changed()
    assert endpoints(host.column_view_store) == before[1:]


def test_offline_recheck_and_late_startup_results_leave_endpoint_sequence_fixed():
    host, rows = make_live_host()
    before = endpoints(host.column_view_store)
    splices = host.column_view_store.splices
    key = fav_key(rows[1].ip, rows[1].gport)

    host._apply_live_results([(key, {"ok": False, "err": "timeout"})], reason="offline-recheck")
    host._apply_live_results([(key, {"ok": False, "err": "timeout"})], reason="offline-recheck")
    assert rows[1].ping == -1
    assert endpoints(host.column_view_store) == before
    host._startup_warmup_revealed = True
    host._apply_live_results([(key, success(ping=400, players=19))], reason="startup-batch")
    assert (rows[1].ping, rows[1].players) == (400, 19)
    assert endpoints(host.column_view_store) == before
    assert host.column_view_store.splices == splices


def test_explicit_sort_still_rebuilds():
    host, rows = make_live_host()
    original = endpoints(host.column_view_store)
    host.sort_key = "players"
    host.sort_asc = False
    host._rebuild_column_view_store(reorder_reason="explicit-sort")
    assert host.column_view_store.splices == 2
    assert endpoints(host.column_view_store) == original  # Trusted First still owns bands.
    host.settings["prioritise_trusted_servers"] = False
    host._rebuild_column_view_store(reorder_reason="explicit-trusted-toggle")
    assert endpoints(host.column_view_store) == list(reversed(original))


@pytest.mark.parametrize("sort_key, sort_asc", [("ping", True), ("players", False)])
def test_trusted_first_on_rebuilds_current_sort_and_background_stays_stable(
    monkeypatch, sort_key, sort_asc,
):
    host, rows = make_live_host()
    host.sort_key = sort_key
    host.sort_asc = sort_asc
    if sort_key == "players":
        for index, obj in enumerate(rows, 1):
            obj.players = index
    for index, obj in enumerate(rows):
        obj.ping = (200, 100, 20)[index] if sort_asc else (20, 100, 200)[index]
        obj.sort_ping = obj.ping
    host.settings["prioritise_trusted_servers"] = False
    host._rebuild_column_view_store(reorder_reason="trusted-off")
    assert endpoints(host.column_view_store) == endpoints(Store(reversed(rows)))

    host.sorter = None
    host._scroll_to_top = lambda: False
    panel = SettingsUI.__new__(SettingsUI)
    panel._win = host
    panel._sync_sidebar_setting_widget = lambda _key: None
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback: callback())
    splices = host.column_view_store.splices

    host.settings["prioritise_trusted_servers"] = True
    panel._apply_setting_runtime_effects("prioritise_trusted_servers")

    assert host.column_view_store.splices == splices + 1
    assert endpoints(host.column_view_store) == endpoints(Store(rows))
    assert (host.sort_key, host.sort_asc) == (sort_key, sort_asc)

    key = fav_key(rows[0].ip, rows[0].gport)
    host._apply_live_results([(key, success(ping=300, max_players=20, players=30))], reason="browser-live")
    assert (rows[0].ping, rows[0].max_players, rows[0].players) == (300, 20, 30)
    assert endpoints(host.column_view_store) == endpoints(Store(rows))
    assert host.column_view_store.splices == splices + 1

    host.settings["prioritise_trusted_servers"] = False
    panel._apply_setting_runtime_effects("prioritise_trusted_servers")
    assert host.column_view_store.splices == splices + 1


def test_cutoff_tooltips_have_explicit_compact_line_breaks():
    source = (Path(__file__).resolve().parents[1] / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tooltips = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "compact_int_setting_entry" or not node.args:
            continue
        key = ast.literal_eval(node.args[0])
        for keyword in node.keywords:
            if keyword.arg == "tooltip":
                tooltips[key] = ast.literal_eval(keyword.value)
    assert tooltips["high_ping_cutoff_ms"] == (
        "Applied when the server list is\n"
        "built or refreshed. Live ping\n"
        "updates do not move or remove\n"
        "servers from the current list."
    )
    assert tooltips["hide_below_max_players"] == (
        "Applied when the server list is\n"
        "built or refreshed. Live player-count\n"
        "updates do not move or remove\n"
        "servers from the current list."
    )


def test_refresh_all_final_rebuild_and_scroll_restoration_remain_separate(monkeypatch):
    events = []
    adjustment = SimpleNamespace(set_value=lambda value: events.append(("scroll", value)))
    host = SimpleNamespace(
        _status_refresh_generation=1, _status_refresh_running=True,
        _status_refresh_inflight=0, _status_refresh_queue=[], _status_refresh_buffer=[],
        _status_refresh_scroll_value=43.0, _status_refresh_total=2,
        _status_refresh_completed=2, _shutdown_cleanup_done=False,
        _startup_warmup_revealed=True,
        _update_status_refresh_last_label=lambda: None,
        _schedule_status_refresh_last_label_tick=lambda: None,
        _cancel_status_refresh_progress_update=lambda *_: None,
        _render_status_refresh_progress=lambda *_, **__: None,
        _snapshot_all_sort_keys=lambda: None,
        _debug_browser_reorder=lambda *_, **__: None,
        _debug_sort_note_model_event=lambda *_: None,
        _rebuild_column_view_store=lambda **_: events.append("rebuild"),
        _set_status_refresh_slot_running=lambda *_, **__: None,
        _clear_status_refresh_progress=lambda *_: None,
        scroller=SimpleNamespace(get_vadjustment=lambda: adjustment), sorter=None,
        combined_filter=None,
    )
    bind(host, "_maybe_finish_status_refresh")
    monkeypatch.setattr(window.GLib, "idle_add", lambda callback: callback())

    host._maybe_finish_status_refresh(1)

    assert events == ["rebuild", ("scroll", 43.0)]
    assert host._status_refresh_running is False
