from types import SimpleNamespace

from dzll_launcher import browser_diagnostics as diag
from dzll_launcher.browser_diagnostics import (
    BrowserPerfDiagnostics,
    BrowserReorderDebug,
    FilterTimingLogger,
    SortDebugRecorder,
    model_row_count,
)
from dzll_launcher.scrollbar_interaction import (
    ScrollbarInteractionState,
    format_scroll_drag_summary,
    scroll_drag_value_text,
)
from dzll_launcher.ui_row import ServerObject


class Store:
    def __init__(self, items):
        self.items = list(items)

    def get_n_items(self):
        return len(self.items)

    def get_item(self, index):
        return self.items[index]


class BrokenStore:
    def get_n_items(self):
        raise RuntimeError("unreadable")


def perf(**overrides):
    kwargs = dict(
        origin_ns=0,
        is_shut_down=lambda: False,
        sort_model=lambda: None,
        filter_model=lambda: None,
    )
    kwargs.update(overrides)
    return BrowserPerfDiagnostics(**kwargs)


def reorder(enabled=True, **overrides):
    kwargs = dict(
        enabled=enabled,
        store=lambda: Store(["a", "b", "c"]),
        scroller=lambda: SimpleNamespace(
            get_vadjustment=lambda: SimpleNamespace(get_value=lambda: 70.0)
        ),
        filter_state=lambda: {"online_only": True, "max_players_cutoff": 0},
        ping_cutoff=lambda: 150,
        key_for_obj=lambda obj: f"key:{obj}",
        row_height_px=64,
    )
    kwargs.update(overrides)
    return BrowserReorderDebug(**kwargs)


def test_model_row_count_falls_through_missing_and_unreadable_models():
    assert model_row_count(None, BrokenStore(), Store([1, 2])) == 2
    assert model_row_count(None, None) == 0


def test_disabled_components_are_silent_and_do_not_query_dependencies(capsys):
    def explode():
        raise AssertionError("dependency queried while disabled")

    sort_debug = SortDebugRecorder(enabled=False, row_count=explode)
    sort_debug.begin("ping", "asc")
    sort_debug.note_bind("full")
    sort_debug.note_model_event("sorter_changed")
    sort_debug.note_compare("ping", 0.1, None, None)
    sort_debug.note_scroll_to_skipped(1, 2, "out_of_range")
    sort_debug.finish()
    assert sort_debug.stats is None

    timing = FilterTimingLogger(enabled=False, sort_description=explode)
    assert timing.context("filter") is None
    timing.log({"action": "filter"})

    debug = reorder(enabled=False, store=explode, scroller=explode)
    debug.log("rebuild-start", reason="load")
    assert debug.visible_keys(5) == []
    assert debug.scroll_key() == "-"

    assert capsys.readouterr().out == ""


def test_sort_recorder_counts_one_sort_and_prints_summary(capsys):
    recorder = SortDebugRecorder(enabled=True, row_count=lambda: 4)
    recorder.begin("ping", "asc")
    recorder.note_bind("light")
    recorder.note_model_event("sorter_changed")
    recorder.note_compare("ping", 0.002, SimpleNamespace(ping=-1), SimpleNamespace(ping=10))
    recorder.note_compare("players", 0.5, None, None)
    recorder.note_live_update("browser-live", ping_updates=3)
    recorder.finish()
    recorder.note_bind("after-finish")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "[SORT] requested column=ping direction=asc rows=4"
    assert lines[1].startswith("[SORT] ping calls=1 ")
    assert "cached_numeric=1" in lines[1] and "offline=1" in lines[1]
    assert "[SORT] row_binds_during_sort=1 bind_kinds light=1 scroll_to_skipped=0" in lines
    assert "[SORT] model_events sorter_changed=1" in lines
    assert "live_ping_updates_during_sort=3" in lines[-1]
    assert recorder.stats["active"] is False
    assert recorder.stats["row_binds"] == 1


def test_sort_notify_probe_attaches_once_and_counts_notifications():
    recorder = SortDebugRecorder(enabled=True, row_count=lambda: 1)
    recorder.begin("ping", "asc")
    obj = ServerObject()
    recorder.attach_notify_probe(obj)
    recorder.attach_notify_probe(obj)
    obj.ping = 33

    assert recorder.stats["notify_props"] == {"ping": 1}
    assert recorder.stats["row_notifications"] == 1


def test_filter_timing_log_uses_injected_sort_description(capsys):
    timing = FilterTimingLogger(enabled=True, sort_description=lambda: "players:desc")
    ctx = timing.context("sidebar", 0.0)
    ctx.update(build_state_ms=1.25, gtk_filter="DIFFERENT", scroll_queued=True)
    timing.log(ctx)

    line = capsys.readouterr().out.strip()
    assert line.startswith("[filter-timing] action=sidebar total=")
    assert "build_state=1.2ms" in line or "build_state=1.3ms" in line
    assert "gtk_filter=DIFFERENT" in line
    assert line.endswith("scroll=queued sort=players:desc")
    assert timing.current_ctx is None


def test_reorder_debug_formats_fields_and_reads_visible_and_scroll_keys(capsys):
    debug = reorder()
    assert debug.visible_keys(2) == ["key:a", "key:b"]
    assert debug.scroll_key() == "key:b"
    assert debug.live_filter_state() == {
        "online_only": True,
        "no_password": False,
        "ping_cutoff": 150,
        "ping_cutoff_active": True,
        "max_players_cutoff": 0,
        "max_players_active": False,
    }
    debug.log("splice", flag=True, keys=["a", ""], text="two words\nline", empty="")
    assert capsys.readouterr().out == (
        "[browser-reorder] splice flag=true keys=[a,-] text=two_words\\nline empty=-\n"
    )


def test_reorder_scroll_key_survives_missing_scroller():
    def missing():
        raise AttributeError("scroller")

    assert reorder(scroller=missing).scroll_key() == "-"


def test_perf_ticks_report_and_reset_counters(capsys):
    diagnostics = perf()
    diagnostics.note_sort(0.002)
    diagnostics.note_sort(0.001)
    diagnostics.note_scroll(0.004)

    assert diagnostics.sort_tick() is True
    assert diagnostics.scroll_tick() is True
    assert diagnostics.row_bind_tick() is True
    assert capsys.readouterr().out.splitlines() == [
        "[PERF] sort calls/sec=2 total=3.0ms max=2.0ms",
        "[PERF] scroll events/sec=1 total=4.0ms max=4.0ms",
    ]
    assert (diagnostics.sort_calls, diagnostics.sort_total, diagnostics.sort_max) == (0, 0.0, 0.0)
    assert diagnostics.sort_tick() is True
    assert capsys.readouterr().out == ""


def test_perf_ticks_stop_after_shutdown(capsys):
    diagnostics = perf(is_shut_down=lambda: True)
    for tick in (
        diagnostics.main_loop_stall_tick,
        diagnostics.row_bind_tick,
        diagnostics.sort_tick,
        diagnostics.scroll_tick,
        diagnostics.model_pending_tick,
    ):
        assert tick() is False
    assert capsys.readouterr().out == ""


def test_perf_stall_tick_reports_only_late_ticks(monkeypatch, capsys):
    clock = [10.0]
    monkeypatch.setattr(diag.time, "perf_counter", lambda: clock[0])
    diagnostics = perf()
    clock[0] += (diag.PERF_STALL_INTERVAL_MS + diag.PERF_STALL_LATE_MS) / 1000.0
    assert diagnostics.main_loop_stall_tick() is True
    clock[0] += 0.9
    assert diagnostics.main_loop_stall_tick() is True
    assert capsys.readouterr().out == "[PERF] main loop stall: 900ms startup_ms=0.0\n"


def test_perf_model_tick_reports_pending_models(capsys):
    model = SimpleNamespace(get_pending=lambda: 7, get_incremental=lambda: True)
    diagnostics = perf(sort_model=lambda: model)
    assert diagnostics.model_pending_tick() is True
    assert capsys.readouterr().out == (
        "[PERF] model state: sort_pending=7 sort_incremental=True\n"
    )


def test_scroll_drag_summary_is_one_line_with_settlement_fields():
    state = ScrollbarInteractionState()
    generation, _ = state.begin(now=1.0, adjustment_value=10.0)
    state.record_adjustment(20.0, generation=generation, now=2.0)
    settlement = state.settle(reason="release", now=2.5, generation=generation)

    line = format_scroll_drag_summary(
        settlement,
        None,
        0.001,
        applied=2,
        popup_closed=1,
        popup_suppressed=0,
        adjustment_total=0.0,
        adjustment_max=0.0,
    )

    assert "\n" not in line
    assert line.startswith(f"[SCROLL-DRAG] id={generation} ")
    assert "first=10.0 latest=20.0 final=- " in line
    assert "applied=2 " in line
    assert "watchdog=0 last_adjustment_to_settle_ms=500.0 release_observed=1 " in line
    assert scroll_drag_value_text(None) == "-"
    assert scroll_drag_value_text(3) == "3.0"
