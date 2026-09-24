import os
import time
from datetime import datetime
from types import MethodType, SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from dzll_launcher import storage, window as window_module
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key


@pytest.fixture
def local_zone(monkeypatch):
    original = os.environ.get("TZ")

    def set_zone(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_zone
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def epoch(year, month, day, hour=0, minute=0, zone="UTC"):
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        pytest.skip(f"timezone unavailable: {zone}")
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


@pytest.mark.parametrize(
    "age, expected",
    [(0, "Today"), (1, "1 Day Ago"), (2, "2 Days Ago"),
     (5, "5 Days Ago"), (20, "20 Days Ago")],
)
def test_local_calendar_day_labels(local_zone, age, expected):
    local_zone("Europe/London")
    now = epoch(2026, 9, 22, 8, zone="Europe/London")
    joined = epoch(2026, 9, 22 - age, 22, zone="Europe/London")
    assert storage.local_days_ago(joined, now_ts=now) == age
    assert storage.human_last_played(joined, now_ts=now) == expected


def test_two_minutes_across_midnight_changes_today_to_one_day(local_zone):
    local_zone("UTC")
    joined = epoch(2026, 9, 21, 23, 59)
    assert storage.human_last_played(joined, now_ts=epoch(2026, 9, 21, 23, 59)) == "Today"
    assert storage.human_last_played(joined, now_ts=epoch(2026, 9, 22, 0, 1)) == "1 Day Ago"


@pytest.mark.parametrize(
    "joined, viewed",
    [((2026, 3, 28, 22), (2026, 3, 29, 8)),
     ((2026, 10, 24, 22), (2026, 10, 25, 8))],
)
def test_dst_overnight_uses_local_dates(local_zone, joined, viewed):
    local_zone("Europe/London")
    assert storage.local_days_ago(
        epoch(*joined, zone="Europe/London"),
        now_ts=epoch(*viewed, zone="Europe/London"),
    ) == 1


def test_future_and_invalid_timestamps(local_zone):
    local_zone("UTC")
    now = epoch(2026, 9, 22, 8)
    future = now + 3600
    assert storage.local_days_ago(future, now_ts=now) == 0
    assert storage.human_last_played(future, now_ts=now) == "Today"
    assert storage.local_days_ago("invalid", now_ts=now) is None
    assert storage.local_days_ago(10**30, now_ts=now) is None
    assert storage.human_last_played("invalid", now_ts=now) == ""


def test_future_timestamp_uses_zero_sort_key(local_zone):
    local_zone("UTC")
    now = epoch(2026, 9, 22, 8)
    row = ServerObject(ip="10.0.0.1", gport=2302)
    host, _visible = browser_host([row], {fav_key(row.ip, row.gport): now + 3600})
    assert DZLLWindow._update_row_sort_played_days(host, row, now)
    assert row.sort_played_days == 0


def test_timezone_change_and_persisted_epoch_recalculation(local_zone, monkeypatch, tmp_path):
    now = epoch(2026, 9, 22, 0, 30)
    joined = epoch(2026, 9, 21, 23, 30)
    monkeypatch.setattr(storage, "LAST_PLAYED_PATH", str(tmp_path / "last_played.json"))
    monkeypatch.setattr(storage.time, "time", lambda: now)
    storage.save_last_played({"server": joined})
    loaded = storage.load_last_played()
    assert loaded == {"server": joined}
    local_zone("UTC")
    assert storage.human_last_played(loaded["server"], now_ts=now) == "1 Day Ago"
    local_zone("Pacific/Honolulu")
    assert storage.human_last_played(loaded["server"], now_ts=now) == "Today"


class Store:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_n_items(self):
        return len(self.rows)

    def get_item(self, index):
        return self.rows[index]

    def splice(self, position, removed, additions):
        self.rows[position:position + removed] = list(additions)


def browser_host(rows, history, *, sort_key="ping", played_only=False):
    source = Store(rows)
    visible = Store()
    state = {
        "played_only": played_only, "hide_test_servers": False,
        "selected_map": "All Maps", "live": {},
    }
    host = SimpleNamespace(
        last_played=history, _obj_by_key={fav_key(r.ip, r.gport): r for r in rows},
        store=source, column_view_store=visible, sort_key=sort_key, sort_asc=True,
        settings={"pin_favorite_servers": False, "prioritise_trusted_servers": False},
        favorites={}, _filter_state=state, _last_played_presentation_token=None,
        _debug_sort_note_model_event=lambda *_args: None,
        _browser_reorder_is_background_reason=lambda *_args: False,
        _debug_browser_reorder=lambda *_args, **_kwargs: None,
        _debug_sort_note_key_build=lambda *_args: None,
        _active_filter_depends_on_live_values=lambda: False,
    )
    host._combined_filter_func = MethodType(DZLLWindow._combined_filter_func, host)
    host._set_int_property_if_changed = MethodType(DZLLWindow._set_int_property_if_changed, host)
    host._update_row_sort_played_days = MethodType(DZLLWindow._update_row_sort_played_days, host)
    host._rebuild_column_view_store = MethodType(DZLLWindow._rebuild_column_view_store, host)
    return host, visible


def test_rollover_refreshes_all_history_and_rebuilds_active_sort_once(local_zone, monkeypatch):
    local_zone("UTC")
    late = epoch(2026, 9, 21, 23, 59)
    older = epoch(2026, 9, 20, 12)
    clock = [epoch(2026, 9, 21, 23, 59)]
    monkeypatch.setattr(window_module.time, "time", lambda: clock[0])
    a = ServerObject(name="Z", ip="10.0.0.1", gport=2302)
    b = ServerObject(name="A", ip="10.0.0.2", gport=2302)
    host, visible = browser_host(
        [a, b], {fav_key(a.ip, a.gport): late, fav_key(b.ip, b.gport): older},
        sort_key="played",
    )
    rebuilds = []
    original = host._rebuild_column_view_store
    host._rebuild_column_view_store = lambda **kw: (rebuilds.append(kw), original(**kw))
    assert DZLLWindow._refresh_last_played_calendar(host)
    assert [a.played, b.played] == ["Today", "1 Day Ago"]
    assert [a.sort_played_days, b.sort_played_days] == [0, 1]
    clock[0] = epoch(2026, 9, 22, 0, 1)
    assert DZLLWindow._refresh_last_played_calendar(host)
    assert [a.played, b.played] == ["1 Day Ago", "2 Days Ago"]
    assert [a.sort_played_days, b.sort_played_days] == [1, 2]
    assert len(rebuilds) == 2
    assert visible.rows == [a, b]
    assert len({id(row) for row in visible.rows}) == 2
    assert not DZLLWindow._refresh_last_played_calendar(host)
    assert len(rebuilds) == 2


def test_rollover_without_played_sort_updates_models_without_rebuild(local_zone, monkeypatch):
    local_zone("UTC")
    now = epoch(2026, 9, 22, 0, 1)
    monkeypatch.setattr(window_module.time, "time", lambda: now)
    row = ServerObject(ip="10.0.0.1", gport=2302, played="Today")
    host, _visible = browser_host([row], {fav_key(row.ip, row.gport): now - 120})
    host._rebuild_column_view_store = lambda **_kw: pytest.fail("unneeded rebuild")
    assert DZLLWindow._refresh_last_played_calendar(host)
    assert row.played == "1 Day Ago"
    assert row.sort_played_days == 1


def test_timezone_change_same_local_date_refreshes_history(local_zone, monkeypatch):
    now = epoch(2026, 9, 22, 12)
    joined = epoch(2026, 9, 22, 1)
    monkeypatch.setattr(window_module.time, "time", lambda: now)
    row = ServerObject(ip="10.0.0.1", gport=2302)
    host, _visible = browser_host([row], {fav_key(row.ip, row.gport): joined})
    host._rebuild_column_view_store = lambda **_kw: None
    local_zone("UTC")
    DZLLWindow._refresh_last_played_calendar(host)
    assert row.played == "Today"
    local_zone("Pacific/Honolulu")
    DZLLWindow._refresh_last_played_calendar(host)
    assert row.played == "1 Day Ago"


def test_timezone_source_change_with_same_current_offset_recalculates_history(local_zone, monkeypatch):
    now = epoch(2026, 11, 1, 12)
    joined = epoch(2026, 10, 24, 23, 30)
    monkeypatch.setattr(window_module.time, "time", lambda: now)
    row = ServerObject(ip="10.0.0.1", gport=2302)
    host, _visible = browser_host([row], {fav_key(row.ip, row.gport): joined})
    local_zone("UTC")
    DZLLWindow._refresh_last_played_calendar(host)
    assert row.played == "8 Days Ago"
    local_zone("Europe/London")
    DZLLWindow._refresh_last_played_calendar(host)
    assert row.played == "7 Days Ago"
    assert row.sort_played_days == 7


def test_map_after_sleep_catches_up_across_midnight(local_zone, monkeypatch):
    local_zone("UTC")
    joined = epoch(2026, 9, 21, 23, 59)
    clock = [joined]
    monkeypatch.setattr(window_module.time, "time", lambda: clock[0])
    row = ServerObject(ip="10.0.0.1", gport=2302)
    host, _visible = browser_host([row], {fav_key(row.ip, row.gport): joined})
    host._refresh_last_played_calendar = MethodType(DZLLWindow._refresh_last_played_calendar, host)
    DZLLWindow._on_last_played_map(host)
    assert row.played == "Today"
    clock[0] = epoch(2026, 9, 22, 8)
    DZLLWindow._on_last_played_map(host)
    assert row.played == "1 Day Ago"
    assert row.sort_played_days == 1


def test_map_and_eligible_live_tick_check_rollover(monkeypatch):
    calls = []
    host = SimpleNamespace(
        _refresh_last_played_calendar=lambda: calls.append("refresh"),
        _browser_live_should_pause=lambda: False,
        _browser_live_inflight=True,
    )
    DZLLWindow._on_last_played_map(host)
    assert DZLLWindow._browser_live_tick(host) is True
    assert calls == ["refresh", "refresh"]
    host._browser_live_should_pause = lambda: True
    assert DZLLWindow._browser_live_tick(host) is True
    assert calls == ["refresh", "refresh"]
