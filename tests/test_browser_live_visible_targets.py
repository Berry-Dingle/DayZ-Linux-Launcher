"""Browser-live visible-row targeting: realised rows, pitch fallback, ticks.

Display-free: GTK rows are represented by fake cells and list items that
expose the same calls the tracker uses (get_mapped, compute_bounds,
get_position, get_item). Real GTK coverage is in
test_browser_live_visible_targets_gtk.py.
"""

import threading
from types import MethodType, SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gio

from dzll_launcher import window as window_module
from dzll_launcher.bound_row_tracker import INVALID_LIST_POSITION, BoundRowTracker
from dzll_launcher.config import BROWSER_LIVE_AHEAD_PER_TICK, BROWSER_LIVE_AHEAD_ROWS
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow, fav_key

VIEWPORT_H = 800.0
ROW_H = 53.0


# --------------------------------------------------------------- fakes
class Rect:
    def __init__(self, y, h):
        self._y, self._h = float(y), float(h)

    def get_y(self):
        return self._y

    def get_height(self):
        return self._h


class Cell:
    def __init__(self, y, h, mapped=True, bounds_ok=True):
        self.y, self.h, self.mapped, self.bounds_ok = y, h, mapped, bounds_ok

    def get_mapped(self):
        return self.mapped

    def compute_bounds(self, _viewport):
        return self.bounds_ok, Rect(self.y, self.h)


class Item:
    def __init__(self, position, obj):
        self.position, self.obj = position, obj

    def get_position(self):
        return self.position

    def get_item(self):
        return self.obj


class Adjustment:
    def __init__(self, value=0.0, page=VIEWPORT_H, upper=0.0, lower=0.0):
        self.value, self.page, self.upper, self.lower = value, page, upper, lower

    def get_value(self):
        return self.value

    def get_page_size(self):
        return self.page

    def get_upper(self):
        return self.upper

    def get_lower(self):
        return self.lower


class Scroller:
    def __init__(self, height=VIEWPORT_H, adjustment=None):
        self.height = height
        self.adjustment = adjustment or Adjustment(page=height)

    def get_height(self):
        return self.height

    def get_vadjustment(self):
        return self.adjustment


def make_store(n=600, prefix="10.1"):
    store = Gio.ListStore.new(ServerObject)
    objs = [
        ServerObject(name=f"S{i:04d}", ip=f"{prefix}.{i // 250}.{i % 250 + 1}", gport=2302, qport=27016)
        for i in range(n)
    ]
    store.splice(0, 0, objs)
    return store, objs


def bind_window(tracker, store, first_visible, heights=None, viewport_h=VIEWPORT_H, buffer=60):
    """Bind rows like GTK: a realised buffer around the viewport.

    Row ``first_visible`` starts at y=0; rows before it have negative y.
    ``heights(i)`` gives each row's height. Returns the cells by position.
    """
    heights = heights or (lambda _i: ROW_H)
    tracker.clear()
    n = store.get_n_items()
    lo = max(0, first_visible - buffer)
    y = -sum(heights(i) for i in range(lo, first_visible))
    cells = {}
    for pos in range(lo, min(n, first_visible + buffer + 40)):
        cell = Cell(y, heights(pos))
        tracker.add(cell, Item(pos, store.get_item(pos)))
        cells[pos] = cell
        y += heights(pos)
    return cells


def truly_visible(cells, viewport_h=VIEWPORT_H):
    return sorted(p for p, c in cells.items() if c.y + c.h > 0 and c.y < viewport_h)


def host_for(store, scroller, tracker=None):
    host = SimpleNamespace(
        column_view_store=store,
        scroller=scroller,
        _browser_bound_rows=tracker if tracker is not None else BoundRowTracker(),
    )
    return host


def groups(host):
    vis, ahead = DZLLWindow._browser_live_candidate_groups(host)
    index = {fav_key(o.ip, o.gport): i for i, o in enumerate(host.column_view_store)}
    return [index[k] for k, _ip, _q in vis], [index[k] for k, _ip, _q in ahead]


# ------------------------------------------------------------- tracker
def test_tracker_add_rebind_discard_and_clear():
    store, objs = make_store(5)
    tracker = BoundRowTracker()
    cell = Cell(0, ROW_H)
    tracker.add(cell, Item(0, objs[0]))
    tracker.add(cell, Item(3, objs[3]))  # recycled cell rebound to another row
    assert len(tracker) == 1
    assert tracker.visible_positions(Scroller(), store) == [3]
    tracker.add(None, Item(1, objs[1]))
    tracker.add(Cell(0, 1), None)
    tracker.discard(None)
    assert len(tracker) == 1
    tracker.discard(cell)
    assert len(tracker) == 0
    assert tracker.visible_positions(Scroller(), store) == []
    tracker.add(Cell(0, ROW_H), Item(1, objs[1]))
    tracker.clear()
    assert len(tracker) == 0


def test_tracker_overlap_boundaries():
    store, objs = make_store(10)
    tracker = BoundRowTracker()
    placements = {
        0: (-ROW_H, ROW_H),  # ends exactly at the top edge: hidden
        1: (-10, ROW_H),  # partly visible at the top
        2: (100, ROW_H),
        3: (VIEWPORT_H - 1, ROW_H),  # one pixel visible at the bottom
        4: (VIEWPORT_H, ROW_H),  # starts exactly at the bottom edge: hidden
    }
    for pos, (y, h) in placements.items():
        tracker.add(Cell(y, h), Item(pos, objs[pos]))
    assert tracker.visible_positions(Scroller(), store) == [1, 2, 3]


def test_tracker_rejects_unusable_rows():
    store, objs = make_store(10)
    other = ServerObject(ip="10.9.9.9", gport=2302)
    tracker = BoundRowTracker()
    tracker.add(Cell(0, ROW_H, mapped=False), Item(0, objs[0]))
    tracker.add(Cell(0, ROW_H, bounds_ok=False), Item(1, objs[1]))
    tracker.add(Cell(0, ROW_H), Item(INVALID_LIST_POSITION, objs[2]))
    tracker.add(Cell(0, ROW_H), Item(99, objs[3]))  # beyond the model
    tracker.add(Cell(0, ROW_H), Item(-1, objs[4]))
    tracker.add(Cell(0, ROW_H), Item(5, other))  # model moved on (rebuild in progress)
    tracker.add(Cell(0, ROW_H), Item(6, None))

    class Broken(Cell):
        def compute_bounds(self, _viewport):
            raise RuntimeError("widget gone")

    tracker.add(Broken(0, ROW_H), Item(7, objs[7]))
    tracker.add(Cell(0, ROW_H), Item(8, objs[8]))
    assert tracker.visible_positions(Scroller(), store) == [8]
    assert tracker.visible_positions(Scroller(height=0), store) == []
    assert tracker.visible_positions(None, store) == []
    assert tracker.visible_positions(Scroller(), None) == []
    assert tracker.visible_positions(Scroller(), Gio.ListStore.new(ServerObject)) == []


# ----------------------------------------------- candidate group selection
@pytest.mark.parametrize("first_visible", [0, 40, 175, 350, 560])
def test_deep_scroll_targets_the_realised_visible_rows(first_visible):
    store, _objs = make_store()
    tracker = BoundRowTracker()
    cells = bind_window(tracker, store, first_visible)
    # A deliberately wrong adjustment proves the realised rows take priority.
    host = host_for(store, Scroller(adjustment=Adjustment(value=0.0, upper=600 * 64.0)), tracker)
    visible, ahead = groups(host)
    assert visible == truly_visible(cells)
    assert ahead == list(range(visible[-1] + 1, min(600, visible[-1] + 1 + BROWSER_LIVE_AHEAD_ROWS)))
    assert host._browser_live_range_source == "realised"


def test_variable_row_heights_use_realised_rows_where_pitch_is_wrong():
    store, _objs = make_store()
    tracker = BoundRowTracker()

    def heights(i):
        return 150.0 if i % 4 == 0 else 44.0

    first_visible = 320
    cells = bind_window(tracker, store, first_visible, heights)
    # GTK's adjustment estimate with mixed heights: value and upper do not
    # map linearly to rows (prototype: pitch put row 285 first for row 320).
    adjustment = Adjustment(value=17776.0, upper=600 * 62.3)
    host = host_for(store, Scroller(adjustment=adjustment), tracker)
    visible, _ahead = groups(host)
    assert visible == truly_visible(cells)
    assert visible[0] == first_visible

    pitch_host = host_for(store, Scroller(adjustment=adjustment), BoundRowTracker())
    pitch_visible, _ = groups(pitch_host)
    assert pitch_host._browser_live_range_source == "pitch"
    assert not set(pitch_visible) & set(visible)  # pitch alone misses every visible row


@pytest.mark.parametrize("pitch", [40.0, 53.0, 64.0, 80.0])
@pytest.mark.parametrize("first_row", [0, 105, 350, 540])
def test_pitch_fallback_without_realised_rows(pitch, first_row):
    store, _objs = make_store()
    adjustment = Adjustment(value=first_row * pitch, page=VIEWPORT_H, upper=600 * pitch)
    host = host_for(store, Scroller(adjustment=adjustment))
    visible, ahead = groups(host)
    assert host._browser_live_range_source == "pitch"
    assert visible[0] == first_row
    expected_count = -(-VIEWPORT_H // pitch) + 1
    assert len(visible) == min(600 - first_row, expected_count)
    if visible[-1] < 599:
        assert ahead[0] == visible[-1] + 1


def test_pitch_fallback_respects_adjustment_lower():
    store, _objs = make_store()
    adjustment = Adjustment(value=100.0 + 200 * ROW_H, upper=100.0 + 600 * ROW_H, lower=100.0)
    host = host_for(store, Scroller(adjustment=adjustment))
    visible, _ = groups(host)
    assert visible[0] == 200


def test_fixed_height_is_the_last_resort():
    store, _objs = make_store()
    # No usable span: the 64 px constant applies.
    host = host_for(store, Scroller(adjustment=Adjustment(value=64.0 * 100, upper=0.0)))
    visible, _ = groups(host)
    assert host._browser_live_range_source == "fixed"
    assert visible[0] == 100

    class NoAdjustment(Scroller):
        def get_vadjustment(self):
            return None

    host = host_for(store, NoAdjustment())
    visible, _ = groups(host)
    assert host._browser_live_range_source == "fixed"
    assert visible[0] == 0


def test_window_without_tracker_still_uses_the_pitch():
    store, _objs = make_store()
    host = SimpleNamespace(
        column_view_store=store,
        scroller=Scroller(adjustment=Adjustment(value=300 * ROW_H, upper=600 * ROW_H)),
    )
    visible, _ = groups(host)
    assert visible[0] == 300


def test_viewport_resize_changes_the_visible_rows():
    store, _objs = make_store()
    tracker = BoundRowTracker()
    cells = bind_window(tracker, store, 200)
    small = host_for(store, Scroller(height=400.0), tracker)
    large = host_for(store, Scroller(height=1200.0), tracker)
    small_visible, _ = groups(small)
    large_visible, _ = groups(large)
    assert small_visible == truly_visible(cells, 400.0)
    assert large_visible == truly_visible(cells, 1200.0)
    assert small_visible[0] == large_visible[0] == 200
    assert len(large_visible) > len(small_visible)

    # The pitch fallback follows the page size too.
    short = host_for(store, Scroller(adjustment=Adjustment(value=200 * ROW_H, page=400.0, upper=600 * ROW_H)))
    tall = host_for(store, Scroller(adjustment=Adjustment(value=200 * ROW_H, page=1200.0, upper=600 * ROW_H)))
    assert len(groups(tall)[0]) > len(groups(short)[0])


def test_filter_or_sort_rebuild_retargets_the_new_rows():
    store, objs = make_store()
    tracker = BoundRowTracker()
    cells = bind_window(tracker, store, 100)
    host = host_for(store, Scroller(adjustment=Adjustment(value=100 * ROW_H, upper=600 * ROW_H)), tracker)
    before_keys = {k for k, _i, _q in DZLLWindow._browser_live_candidate_groups(host)[0]}

    # Rebuild: reverse order and drop every third row (a sort plus a filter).
    kept = [o for i, o in enumerate(reversed(objs)) if i % 3]
    store.splice(0, store.get_n_items(), kept)

    # Until GTK rebinds, the old bindings no longer match the model and are
    # ignored rather than targeting whatever row now sits at that position.
    stale_positions = tracker.visible_positions(host.scroller, store)
    assert all(store.get_item(p) is objs[p] for p in stale_positions)

    # GTK rebinds the same cells to the rows now at those positions.
    for pos, cell in cells.items():
        if pos < store.get_n_items():
            tracker.add(cell, Item(pos, store.get_item(pos)))
    vis, _ahead = DZLLWindow._browser_live_candidate_groups(host)
    expected = [fav_key(store.get_item(p).ip, store.get_item(p).gport) for p in truly_visible(cells)]
    assert [k for k, _i, _q in vis] == expected
    assert host._browser_live_range_source == "realised"
    assert {k for k, _i, _q in vis} != before_keys


# --------------------------------------------- tick pipeline (unchanged)
class Indicator:
    def __init__(self):
        self.pulses = 0

    def pulse(self):
        self.pulses += 1


@pytest.fixture
def tick_host(monkeypatch):
    store, objs = make_store(prefix="10.2")
    tracker = BoundRowTracker()
    queried = []
    lock = threading.Lock()

    def fake_query(ip, qport, **_kw):
        with lock:
            queried.append(ip)
        return {"ok": True, "ping_ms": 30, "players": 1, "max_players": 60}

    pending = []
    delivered = threading.Event()

    def capture_idle(callback, *args):
        pending.append((callback, args))
        delivered.set()
        return 1

    monkeypatch.setattr(window_module, "query_server_live", fake_query)
    monkeypatch.setattr(window_module, "GLib", SimpleNamespace(idle_add=capture_idle))

    applied = []
    rejected = []
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=4)
    host = SimpleNamespace(
        column_view_store=store,
        scroller=Scroller(adjustment=Adjustment(value=0.0, upper=600 * ROW_H)),
        _browser_bound_rows=tracker,
        _obj_by_key={fav_key(o.ip, o.gport): o for o in objs},
        _mod_suggestions=SimpleNamespace(
            schedule_deferred_restore=lambda: None,
            schedule_refresh=lambda: None,
            schedule_migration=lambda: None,
        ),
        _refresh_last_played_calendar=lambda: None,
        _browser_live_should_pause=lambda: False,
        _browser_live_inflight=False,
        _browser_live_last_refresh={},
        _browser_live_token=0,
        _browser_live_target_keys=set(),
        _browser_live_executor=executor,
        _browser_live_scroll_active_until=0.0,
        _defer_browser_scrollbar_work=lambda *_a: False,
        _diagnose_rejected_browser_results=lambda token, keys, results, cls, reason: rejected.append(reason),
        _apply_live_results=lambda results, reason: applied.append([k for k, _i in results]),
        _visible_refresh_indicator=Indicator(),
    )
    host._browser_live_candidate_groups = MethodType(DZLLWindow._browser_live_candidate_groups, host)
    host._apply_browser_live_results = MethodType(DZLLWindow._apply_browser_live_results, host)
    index = {fav_key(o.ip, o.gport): i for i, o in enumerate(objs)}
    ip_index = {o.ip: i for i, o in enumerate(objs)}

    def tick():
        queried.clear()
        pending.clear()
        delivered.clear()
        assert DZLLWindow._browser_live_tick(host) is True
        assert delivered.wait(5.0)
        callback, args = pending[0]
        return sorted(ip_index[ip] for ip in queried), args

    def apply(args):
        token, target_keys, results = args
        return DZLLWindow._apply_browser_live_results(host, token, target_keys, results)

    ns = SimpleNamespace(
        host=host, store=store, tracker=tracker, tick=tick, apply=apply,
        applied=applied, rejected=rejected, index=index,
    )
    yield ns
    executor.shutdown(wait=True)


def test_refresh_follows_the_viewport_after_scrolling(tick_host):
    t = tick_host
    top_cells = bind_window(t.tracker, t.store, 0)
    top_visible = truly_visible(top_cells)
    dispatched, first_args = t.tick()
    assert set(top_visible) <= set(dispatched)
    assert len(dispatched) == len(top_visible) + BROWSER_LIVE_AHEAD_PER_TICK

    # The user scrolls far down while that cycle is in flight. The scroll
    # handler invalidates the targets; GTK binds the new rows.
    DZLLWindow._invalidate_browser_live_targets(t.host)
    deep_cells = bind_window(t.tracker, t.store, 350)
    deep_visible = truly_visible(deep_cells)

    # The in-flight results for the old rows are rejected as stale.
    t.host._browser_live_inflight = True
    t.apply(first_args)
    assert t.applied == []
    assert t.rejected == ["newer-generation"]
    assert t.host._visible_refresh_indicator.pulses == 0
    assert t.host._browser_live_inflight is False

    # Two further cycles target the new viewport, not the original rows.
    for cycle in range(2):
        dispatched, args = t.tick()
        assert set(deep_visible) <= set(dispatched)
        assert not set(dispatched) & set(top_visible)
        assert min(dispatched) == deep_visible[0]
        t.apply(args)
        assert set(t.index[k] for k in t.applied[-1]) == set(dispatched)
        assert t.host._visible_refresh_indicator.pulses == cycle + 1

    # Returning to the top retargets the original rows.
    DZLLWindow._invalidate_browser_live_targets(t.host)
    bind_window(t.tracker, t.store, 0)
    dispatched, _args = t.tick()
    assert set(top_visible) <= set(dispatched)


def test_superseded_target_set_is_rejected(tick_host):
    t = tick_host
    bind_window(t.tracker, t.store, 200)
    _dispatched, args = t.tick()
    # Same token, but the current target set has changed.
    t.host._browser_live_target_keys = {"10.0.0.1:2302"}
    t.apply(args)
    assert t.applied == []
    assert t.rejected == ["targets-superseded"]


def test_filter_rebuild_retargets_the_next_tick(tick_host):
    t = tick_host
    cells = bind_window(t.tracker, t.store, 120)
    dispatched_before, args = t.tick()
    t.apply(args)

    objs = list(t.store)
    kept = list(reversed(objs[::2]))
    t.store.splice(0, t.store.get_n_items(), kept)
    DZLLWindow._invalidate_browser_live_targets(t.host)  # as filter and sort rebuilds do
    for pos, cell in cells.items():
        if pos < t.store.get_n_items():
            t.tracker.add(cell, Item(pos, t.store.get_item(pos)))
    dispatched_after, _args = t.tick()
    expected_ips = {t.store.get_item(p).ip for p in truly_visible(cells)}
    queried_ips = {objs[i].ip for i in dispatched_after}
    assert expected_ips <= queried_ips
    assert queried_ips != {objs[i].ip for i in dispatched_before}
