from pathlib import Path
from types import SimpleNamespace

from dzll_launcher import column_view
from dzll_launcher import window as window_module
from dzll_launcher.scrollbar_interaction import (
    SCROLLBAR_FACTORY_NAMES,
    ScrollbarDragLightController,
    ScrollbarFactoryMetrics,
    ScrollbarInteractionState,
)
from dzll_launcher.window import DZLLWindow


class FakeAdjustment:
    def __init__(self, value=10.0):
        self.value = value
        self.set_calls = []

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value
        self.set_calls.append(value)


class FakeScroller:
    def __init__(self, adjustment, scrollbar=None):
        self.adjustment = adjustment
        self.scrollbar = scrollbar

    def get_vadjustment(self):
        return self.adjustment

    def get_vscrollbar(self):
        return self.scrollbar


def make_host(value=10.0):
    adjustment = FakeAdjustment(value)
    host = SimpleNamespace(
        scroller=FakeScroller(adjustment),
        _scrollbar_interaction=ScrollbarInteractionState(),
        _scrollbar_interaction_watchdog_id=0,
        _scroll_drag_adjustment_total=0.0,
        _scroll_drag_adjustment_max=0.0,
        _scroll_drag_popup_closed=0,
        _scroll_drag_popup_suppression_start=0,
        _scroll_drag_deferred_applied={},
        _browser_live_scroll_active_until=0.0,
        _browser_live_token=0,
        _browser_live_target_keys=set(),
        _scroll_to_top_idle_id=0,
        _programmatic_scroll_to_top_active_until=0.0,
        _perf_scroll_events=0,
        _perf_scroll_total=0.0,
        _perf_scroll_max=0.0,
        _shutdown_cleanup_done=False,
    )
    for name in (
        "_browser_vadjustment_value",
        "_cancel_browser_scrollbar_watchdog",
        "_defer_browser_scrollbar_work",
        "_drain_browser_scrollbar_work",
        "_on_browser_scrollbar_watchdog",
        "_print_scroll_drag_summary",
        "_settle_browser_scrollbar_interaction",
    ):
        setattr(host, name, getattr(DZLLWindow, name).__get__(host))
    host._invalidate_browser_live_targets = lambda: setattr(
        host, "_browser_live_token", host._browser_live_token + 1
    )
    return host, adjustment


def test_scrollbar_press_adjustments_and_release_never_write_adjustment(monkeypatch):
    scheduled = []
    monkeypatch.setattr(window_module.GLib, "timeout_add", lambda *args: scheduled.append(args) or 41)
    monkeypatch.setattr(window_module.GLib, "source_remove", lambda _source_id: None)
    monkeypatch.setattr(window_module, "set_required_mods_popup_suppressed", lambda _value: False)
    monkeypatch.setattr(window_module, "required_mods_popup_suppression_count", lambda: 0)
    host, adjustment = make_host()
    host._scroll_drag_light = ScrollbarDragLightController(True)

    DZLLWindow._on_browser_scrollbar_pressed(host)
    assert host._scrollbar_interaction.active
    assert host._scroll_drag_light.is_active()
    adjustment.value = 25.0
    DZLLWindow._on_browser_scroll_value_changed(host, adjustment)
    adjustment.value = 70.0
    DZLLWindow._on_browser_scroll_value_changed(host, adjustment)
    assert host._scrollbar_interaction.latest_adjustment_value == 70.0
    assert host._scrollbar_interaction.adjustment_events == 2

    DZLLWindow._on_browser_scrollbar_released(host)
    assert not host._scrollbar_interaction.active
    assert not host._scroll_drag_light.is_active()
    assert not DZLLWindow._settle_browser_scrollbar_interaction(host, "release")
    assert adjustment.set_calls == []
    assert len(scheduled) == 1


def test_adjustment_without_scrollbar_press_preserves_scroll_to_top_cancellation(monkeypatch):
    removed = []
    monkeypatch.setattr(window_module.GLib, "source_remove", removed.append)
    host, adjustment = make_host()
    host._scroll_drag_light = ScrollbarDragLightController(True)
    host._scroll_to_top_idle_id = 88
    adjustment.value = 33.0

    DZLLWindow._on_browser_scroll_value_changed(host, adjustment)

    assert not host._scrollbar_interaction.active
    assert not host._scroll_drag_light.is_active()
    assert host._scrollbar_interaction.adjustment_events == 0
    assert removed == [88]
    assert host._scroll_to_top_idle_id == 0
    assert adjustment.set_calls == []


def test_watchdog_unmap_and_shutdown_paths_settle_once(monkeypatch):
    monkeypatch.setattr(window_module, "set_required_mods_popup_suppressed", lambda _value: False)
    monkeypatch.setattr(window_module, "required_mods_popup_suppression_count", lambda: 0)
    for callback, reason in (
        (lambda host, generation: DZLLWindow._on_browser_scrollbar_watchdog(host, generation), "watchdog"),
        (lambda host, _generation: DZLLWindow._on_browser_scrollbar_unmap(host), "unmap"),
    ):
        host, _adjustment = make_host()
        generation, _ = host._scrollbar_interaction.begin(now=1.0)
        callback(host, generation)
        assert not host._scrollbar_interaction.active
        assert host._scrollbar_interaction.settle_reason == reason


def test_stopped_threshold_does_not_end_a_real_drag():
    host, _adjustment = make_host()
    host._scrollbar_interaction.begin(now=1.0)
    DZLLWindow._on_browser_scrollbar_stopped(host)
    assert host._scrollbar_interaction.active


def test_browser_live_is_paused_by_active_state_and_can_resume_after_settle(monkeypatch):
    host, _adjustment = make_host()
    host._scrollbar_interaction.begin(now=1.0)
    assert DZLLWindow._browser_live_should_pause(host)
    host._scrollbar_interaction.settle(reason="release", now=2.0)
    host._browser_live_scroll_active_until = 0.0
    host.get_visible = lambda: True
    host.get_mapped = lambda: True
    host.get_surface = lambda: None
    host._steam_ugc_worker_in_progress = False
    host._mod_download_backend_active = ""
    host._steamcmd_auth_request = None
    assert not DZLLWindow._browser_live_should_pause(host)


def test_deferred_work_drains_once_with_newest_request():
    host, _adjustment = make_host()
    calls = []
    host._apply_browser_live_results = lambda *args, **kwargs: calls.append((args, kwargs))
    generation, _ = host._scrollbar_interaction.begin(now=1.0)
    host._scrollbar_interaction.defer("browser-live-results", (1, {"old"}, ["old"]))
    host._scrollbar_interaction.defer("browser-live-results", (2, {"new"}, ["new"]))
    settlement = host._scrollbar_interaction.settle(
        reason="release", now=2.0, generation=generation
    )

    host._drain_browser_scrollbar_work(settlement)
    host._drain_browser_scrollbar_work(
        SimpleNamespace(deferred_work={})
    )
    assert calls == [((2, {"new"}, ["new"]), {"_from_scroll_settle": True})]


def test_controller_feature_detection_fallback_is_non_interfering(monkeypatch):
    class FakeClick:
        def __init__(self):
            self.button = None
            self.phase = None
            self.signals = {}

        def set_button(self, button):
            self.button = button

        def set_propagation_phase(self, phase):
            self.phase = phase

        def connect(self, name, callback):
            if name in {"stopped", "unpaired-release"}:
                raise TypeError("older build")
            self.signals[name] = callback

    class FakeGestureClick:
        @staticmethod
        def new():
            return FakeClick()

    class FakeScrollbar:
        def add_controller(self, controller):
            self.controller = controller

    scrollbar = FakeScrollbar()
    host = SimpleNamespace(scroller=FakeScroller(FakeAdjustment(), scrollbar))
    for name in (
        "_on_browser_scrollbar_pressed",
        "_on_browser_scrollbar_released",
        "_on_browser_scrollbar_cancelled",
        "_on_browser_scrollbar_stopped",
        "_on_browser_scrollbar_unpaired_release",
    ):
        setattr(host, name, lambda *_args: None)
    monkeypatch.setattr(window_module.Gtk, "GestureClick", FakeGestureClick)

    assert DZLLWindow._install_browser_scrollbar_interaction_controller(host)
    assert scrollbar.controller.button == 1
    assert "pressed" in scrollbar.controller.signals
    assert "released" in scrollbar.controller.signals
    assert not hasattr(host.scroller, "set_vadjustment")

    host.scroller = SimpleNamespace()
    assert not DZLLWindow._install_browser_scrollbar_interaction_controller(host)


def test_required_mod_popup_closes_once_suppresses_and_recovers(monkeypatch):
    target = SimpleNamespace(_dzll_required_mod_names=["Example Mod"])
    popover = SimpleNamespace(get_visible=lambda: True)
    target._dzll_required_mods_popover = popover
    closed = []
    shown = []
    monkeypatch.setattr(column_view, "_popdown_required_mods_popover", lambda widget: (
        closed.append(widget),
        column_view._clear_open_required_mods_popover(),
    ))
    column_view._OPEN_REQUIRED_MODS_WIDGET = target
    column_view._OPEN_REQUIRED_MODS_POPOVER = popover
    column_view.set_required_mods_popup_suppressed(False)

    assert column_view.set_required_mods_popup_suppressed(True)
    assert not column_view.set_required_mods_popup_suppressed(True)
    column_view._show_required_mods_popover(target)
    assert closed == [target]

    column_view.set_required_mods_popup_suppressed(False)
    monkeypatch.setattr(column_view, "_ensure_required_mods_root_click_controller", lambda widget: None)
    content = object()
    installed = []
    monkeypatch.setattr(
        column_view,
        "_make_required_mods_popover_content",
        lambda names: content,
    )
    fake = SimpleNamespace(
        get_visible=lambda: False,
        popup=lambda: shown.append(True),
        set_child=installed.append,
    )
    target._dzll_required_mods_popover = fake
    column_view._show_required_mods_popover(target)
    assert installed == [content]
    assert shown == [True]
    assert closed == [target]
    column_view._clear_open_required_mods_popover()


def test_explicit_sort_and_filter_paths_are_not_gated_by_interaction_state():
    source = Path(window_module.__file__).read_text(encoding="utf-8")
    set_sort = source.split("    def _set_sort(", 1)[1].split(
        "    def _on_column_view_sort_header_clicked", 1
    )[0]
    filter_changed = source.split("    def _on_filter_changed(", 1)[1].split(
        "    def _apply_search_filter_changed", 1
    )[0]
    assert "_scrollbar_interaction" not in set_sort
    assert "_scrollbar_interaction" not in filter_changed


def test_factory_summary_is_one_concise_line_with_all_factory_names():
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(12)
    for name in SCROLLBAR_FACTORY_NAMES:
        metrics.record_setup(name)
    snapshot = metrics.settle(12)

    line = DZLLWindow._format_scroll_drag_factory_summary(snapshot)

    assert line.startswith("[SCROLL-DRAG-FACTORIES] id=12 ")
    assert "\n" not in line
    assert len(line) < 1800
    for name in SCROLLBAR_FACTORY_NAMES:
        assert f"{name}=s1/" in line
    assert "name_ops=(" in line
    assert "notify=" in line
    assert "light_visuals=flag:" in line
    assert ",ping_color:" in line
    assert "action_ops=" in line
    assert "light_bind_ms=" in line
    assert "settle_passes=" in line
    assert "settle_refresh=" in line
    assert "registry_before_settle=" in line
    assert "registry_live=" in line
    assert "registry_stale=" in line
    assert "registry_after_settle=" in line
    assert "settle_cells=(" in line
    assert "skipped=(" in line


def test_all_settle_paths_restore_popup_then_refresh_once_without_adjustment_write(
    monkeypatch,
):
    for reason in ("release", "cancel", "watchdog", "unmap", "shutdown"):
        order = []
        monkeypatch.setattr(
            window_module,
            "set_required_mods_popup_suppressed",
            lambda value, order=order: order.append(f"popup:{value}") or False,
        )
        host, adjustment = make_host()
        controller = ScrollbarDragLightController(True)

        class Cell:
            pass

        class Item:
            def __init__(self, child, obj):
                self.child = child
                self.obj = obj

            def get_child(self):
                return self.child

            def get_item(self):
                return self.obj

        cell = Cell()
        obj = object()
        item = Item(cell, obj)
        generation, _ = host._scrollbar_interaction.begin(now=1.0)
        controller.begin(generation)

        def refresh(_item, _cell, order=order):
            order.append("refresh")

        controller.register(
            "name",
            cell,
            item,
            obj,
            refresh,
        )
        host._scroll_drag_light = controller
        host._drain_browser_scrollbar_work = (
            lambda _settlement, order=order: order.append("drain")
        )

        assert host._settle_browser_scrollbar_interaction(
            reason,
            generation=generation,
        )
        assert not host._settle_browser_scrollbar_interaction(
            reason,
            generation=generation,
        )
        assert order[:2] == ["popup:False", "refresh"]
        assert order.count("refresh") == 1
        assert order.count("drain") == (0 if reason == "shutdown" else 1)
        assert adjustment.set_calls == []


def test_drag_output_is_two_lines_and_distinguishes_release_from_watchdog(
    monkeypatch, capsys
):
    monkeypatch.setattr(window_module, "SCROLL_DRAG_PERF_ENABLED", True)
    monkeypatch.setattr(window_module, "required_mods_popup_suppression_count", lambda: 0)
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(5)
    snapshot = metrics.settle(5)
    host = SimpleNamespace(
        _scroll_drag_deferred_applied={},
        _scroll_drag_popup_suppression_start=0,
        _scroll_drag_popup_closed=0,
        _scroll_drag_adjustment_total=0.0,
        _scroll_drag_adjustment_max=0.0,
        _scroll_drag_factory_snapshot=snapshot,
    )
    host._scroll_drag_value_text = DZLLWindow._scroll_drag_value_text
    host._format_scroll_drag_factory_summary = (
        DZLLWindow._format_scroll_drag_factory_summary
    )

    state = ScrollbarInteractionState()
    generation, _ = state.begin(now=1.0, adjustment_value=10.0)
    state.record_adjustment(20.0, generation=generation, now=2.0)
    released = state.settle(reason="release", now=2.25, generation=generation)
    DZLLWindow._print_scroll_drag_summary(host, released, 20.0, 0.001)
    release_lines = capsys.readouterr().out.splitlines()
    assert len(release_lines) == 2
    assert "last_adjustment_to_settle_ms=250.0" in release_lines[0]
    assert "release_observed=1" in release_lines[0]
    assert "watchdog=0" in release_lines[0]

    state.begin(now=3.0, adjustment_value=20.0)
    generation = state.generation
    state.record_adjustment(30.0, generation=generation, now=4.0)
    watchdog = state.settle(reason="watchdog", now=18.0, generation=generation)
    DZLLWindow._print_scroll_drag_summary(host, watchdog, 30.0, 0.001)
    watchdog_lines = capsys.readouterr().out.splitlines()
    assert len(watchdog_lines) == 2
    assert "last_adjustment_to_settle_ms=14000.0" in watchdog_lines[0]
    assert "release_observed=0" in watchdog_lines[0]
    assert "watchdog=1" in watchdog_lines[0]


def test_name_cell_and_notify_operation_counters_follow_real_helpers(monkeypatch):
    class Widget:
        def __init__(self):
            self._dzll_perspective_class = None

        def set_text(self, _value):
            pass

        def set_tooltip_text(self, _value):
            pass

        def set_visible(self, _value):
            pass

        def add_css_class(self, _value):
            pass

        def remove_css_class(self, _value):
            pass

        def set_cursor(self, _value):
            pass

    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(1)
    cell = SimpleNamespace(
        _dzll_lock_label=Widget(),
        _dzll_name_label=Widget(),
        _dzll_perspective_label=Widget(),
        _dzll_flag_label=Widget(),
        _dzll_ipport_label=Widget(),
        _dzll_mods_label=Widget(),
        _dzll_mods_button=Widget(),
        _dzll_mods_button_label=Widget(),
    )
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda *_args: None)
    obj = column_view.ServerObject(
        name="Example",
        ip="127.0.0.1",
        gport=2302,
        country="GB",
        mods_json="[]",
    )

    column_view._bind_name_cell(cell, obj, metrics)
    disconnected = []
    notify_obj = SimpleNamespace(disconnect=disconnected.append)
    notify_widget = SimpleNamespace(
        _dzll_notify_obj=notify_obj,
        _dzll_notify_ids=[11, 12],
    )
    column_view._disconnect_notify_handlers(notify_widget, metrics, "ping")
    snapshot = metrics.settle(1)

    assert snapshot.operations["name_text_writes"] == 7
    assert snapshot.operations["name_tooltip_writes"] == 3
    assert snapshot.operations["name_visibility_changes"] == 2
    assert snapshot.operations["name_popover_owner_checks"] == 1
    assert snapshot.operations["name_mod_prepare_calls"] == 1
    assert snapshot.operations["name_flag_address_format_calls"] == 2
    assert snapshot.operations["notify_disconnects"] == 2
    assert snapshot.operations["ping_notify_disconnects"] == 2
    assert disconnected == [11, 12]
