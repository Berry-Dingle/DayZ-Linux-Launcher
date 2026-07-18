import gc
from types import SimpleNamespace
import weakref
from pathlib import Path

import pytest

from dzll_launcher import column_view
from dzll_launcher import scrollbar_interaction as interaction_module
from dzll_launcher.scrollbar_interaction import (
    BoundCellRegistry,
    ScrollbarDragLightController,
    ScrollbarFactoryMetrics,
    drag_light_enabled_from_env,
)


class WeakCell:
    pass


class FakeListItem:
    def __init__(self, child, item=None):
        self.child = child
        self.item = item

    def get_child(self):
        return self.child

    def get_item(self):
        return self.item


class FakeWidget:
    def __init__(self, text="", visible=True):
        self.text = text
        self.visible = visible
        self.tooltip = "old tooltip"
        self.cursor = object()
        self.markup = None
        self.markup_writes = 0

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.text = value
        self.markup = None

    def set_markup(self, value):
        self.markup = value
        self.markup_writes += 1
        self.text = value.split(">", 1)[1].rsplit("<", 1)[0]

    def get_visible(self):
        return self.visible

    def set_visible(self, value):
        self.visible = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = value

    def set_cursor(self, value):
        self.cursor = value


def make_name_cell():
    return SimpleNamespace(
        _dzll_lock_label=FakeWidget("stale"),
        _dzll_name_label=FakeWidget("stale"),
        _dzll_perspective_label=FakeWidget("stale"),
        _dzll_flag_label=FakeWidget("stale"),
        _dzll_ipport_label=FakeWidget("stale"),
        _dzll_mods_label=FakeWidget("stale", False),
        _dzll_mods_button=FakeWidget("", True),
        _dzll_mods_button_label=FakeWidget("stale"),
    )


def test_controller_feature_gate_is_active_only_between_begin_and_settle():
    disabled = ScrollbarDragLightController(False)
    assert not disabled.begin(1)
    assert not disabled.is_active()

    enabled = ScrollbarDragLightController(True)
    assert not enabled.is_active()
    assert enabled.begin(4)
    assert enabled.is_active()
    assert enabled.deactivate(4)
    assert not enabled.is_active()
    assert not enabled.deactivate(4)


def test_unset_feature_flag_enables_drag_light_binding():
    assert drag_light_enabled_from_env(None)
    controller = ScrollbarDragLightController(drag_light_enabled_from_env(None))
    assert controller.begin(1)
    assert controller.is_active()


@pytest.mark.parametrize("value", ("1", "true", "yes", "on", " TRUE ", "On"))
def test_explicit_true_feature_flag_values_enable_drag_light_binding(value):
    assert drag_light_enabled_from_env(value)


@pytest.mark.parametrize("value", ("0", "false", "no", "off", " FALSE ", "Off"))
def test_explicit_false_feature_flag_values_disable_drag_light_binding(value):
    assert not drag_light_enabled_from_env(value)


@pytest.mark.parametrize("value", ("", "invalid", "2", "enabled"))
def test_invalid_feature_flag_values_use_enabled_default(value):
    assert drag_light_enabled_from_env(value)


def test_scroll_drag_perf_instrumentation_remains_disabled_by_default():
    source = (Path(__file__).resolve().parents[1] / "src/dzll_launcher/window.py").read_text(
        encoding="utf-8"
    )
    assert 'SCROLL_DRAG_PERF_ENABLED = os.environ.get("DZLL_SCROLL_DRAG_PERF") == "1"' in source


def test_explicit_disable_returns_factory_selection_to_full_bind_mode():
    controller = ScrollbarDragLightController(drag_light_enabled_from_env("off"))
    assert not controller.begin(1)
    assert not column_view._drag_light_active(controller)


def test_wheel_or_keyboard_style_activity_without_press_keeps_full_mode():
    controller = ScrollbarDragLightController(True)
    assert not column_view._drag_light_active(controller)


def test_name_light_bind_replaces_all_identity_fields_and_popup_ownership(monkeypatch):
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda widget, value: setattr(widget, "cursor", value))
    cell = make_name_cell()
    obj = column_view.ServerObject(
        name="Current Server",
        ip="10.20.30.40",
        gport=2302,
        password=True,
        third_person=True,
        mod_count=12,
        mods_json='[{"name":"Must not prepare"}]',
    )

    column_view._bind_name_cell_light(cell, obj)

    assert cell._dzll_bound_obj is obj
    assert cell._dzll_mods_button._dzll_bound_obj is obj
    assert cell._dzll_name_label.text == "Current Server"
    assert cell._dzll_ipport_label.text == "10.20.30.40:2302"
    assert cell._dzll_ipport_label._dzll_ipport_plain == "10.20.30.40:2302"
    assert cell._dzll_mods_button_label.text == "Mods: 12"
    assert cell._dzll_lock_label.text == "🔒"
    assert cell._dzll_perspective_label.text == "3P"
    assert cell._dzll_mods_button._dzll_required_mod_names == []
    assert cell._dzll_name_label.tooltip is None
    assert cell._dzll_ipport_label.tooltip is None


def test_favourite_join_and_monitor_light_binds_replace_action_targets():
    first = column_view.ServerObject(name="Old", fav=False)
    current = column_view.ServerObject(name="Current", fav=True)
    label = SimpleNamespace(markup="", set_markup=lambda value: setattr(label, "markup", value))
    fav = SimpleNamespace(_dzll_bound_obj=first, _dzll_label=label, _dzll_fav_state=False)
    column_view._bind_fav_button_light(fav, current)
    assert fav._dzll_bound_obj is current
    assert "★" in label.markup

    join = SimpleNamespace(_dzll_bound_obj=first)
    column_view._bind_action_button_light(
        join,
        current,
        factory_name="join",
        active_css_class=None,
        tooltip_text="Join this server",
    )
    assert join._dzll_bound_obj is current

    monitor = SimpleNamespace(
        _dzll_bound_obj=first,
        _dzll_drag_action_light=False,
        set_tooltip_text=lambda value: setattr(monitor, "tooltip", value),
    )
    column_view._bind_action_button_light(
        monitor,
        current,
        factory_name="monitor",
        active_css_class="monitor-btn-active",
        tooltip_text="Monitor this server",
    )
    assert monitor._dzll_bound_obj is current
    assert monitor.tooltip == "Monitor this server"


def test_players_and_ping_light_bind_show_current_values(monkeypatch):
    monkeypatch.setattr(column_view.Gtk, "Label", FakeWidget)
    players = FakeWidget("old")
    queue = FakeWidget("old")
    cell = SimpleNamespace(_dzll_players_label=players, _dzll_queue_label=queue)
    obj = column_view.ServerObject(players=31, max_players=60, queue=4, ping=87)

    column_view._bind_players_cell_light(cell, obj)
    ping = FakeWidget("old")
    column_view._bind_ping_light(ping, obj)

    assert players.text == "31/60"
    assert queue.text == "(Q4)"
    assert ping.text == "87ms"


def test_name_light_bind_uses_current_cached_flag_and_replaces_recycled_flag(monkeypatch):
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda *_args: None)
    cell = make_name_cell()
    first = column_view.ServerObject(country="US", ip="1.1.1.1", gport=2302)
    current = column_view.ServerObject(country="DE", ip="2.2.2.2", gport=2302)
    column_view.row_stable_display(first)
    column_view.row_stable_display(current)
    expected_first = first._row_flag_text
    expected_current = current._row_flag_text

    monkeypatch.setattr(
        column_view,
        "flag_for",
        lambda _country: (_ for _ in ()).throw(AssertionError("cache bypassed")),
    )
    column_view._bind_name_cell_light(cell, first)
    assert cell._dzll_flag_label.text == expected_first
    column_view._bind_name_cell_light(cell, current)
    assert cell._dzll_flag_label.text == expected_current
    assert cell._dzll_flag_label.text != expected_first


def test_name_light_bind_missing_and_invalid_country_match_full_fallback(monkeypatch):
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda *_args: None)
    monkeypatch.setattr(column_view, "_set_perspective_class", lambda *_args: None)
    monkeypatch.setattr(column_view, "_bind_required_mods_popover_target", lambda *_args: None)
    for country in ("", "USA", "1!"):
        light_cell = make_name_cell()
        full_cell = make_name_cell()
        obj = column_view.ServerObject(country=country)
        column_view._bind_name_cell_light(light_cell, obj)
        column_view._bind_name_cell(full_cell, obj)
        assert light_cell._dzll_flag_label.text == full_cell._dzll_flag_label.text


def test_ping_light_bind_preserves_full_palette_and_recycled_colour():
    label = FakeWidget("old")
    cases = (
        (-1, "ping-offline"),
        (60, "ping-good"),
        (61, "ping-greeny"),
        (101, "ping-yellow"),
        (141, "ping-orange"),
        (191, "ping-bad"),
    )
    previous_markup = None
    for ping, css_class in cases:
        obj = column_view.ServerObject(ping=ping)
        expected_text, expected_class = column_view.row_ping_display(obj)
        assert expected_class == css_class
        column_view._bind_ping_light(label, obj)
        assert label.text == expected_text
        assert column_view.PING_MARKUP_COLORS[css_class] in label.markup
        if previous_markup is not None:
            assert label.markup != previous_markup
        previous_markup = label.markup


def test_ping_light_unknown_state_matches_full_bind_without_notify_work():
    light = FakeWidget("stale")
    full = FakeWidget("stale")
    obj = column_view.ServerObject(ping=-1)
    column_view._bind_ping_light(light, obj)
    column_view._bind_ping(full, obj)
    assert light.text == full.text == "OFFLINE"
    assert light.markup == full.markup
    assert not hasattr(light, "_dzll_notify_ids")


def test_flag_and_ping_light_metrics_count_only_restored_visual_writes(monkeypatch):
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda *_args: None)
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(2)
    cell = make_name_cell()
    column_view._bind_name_cell_light(
        cell,
        column_view.ServerObject(country="GB", ip="1.2.3.4", gport=2302),
        metrics,
    )
    ping = FakeWidget("old")
    column_view._bind_ping_light(ping, column_view.ServerObject(ping=87), metrics)
    snapshot = metrics.settle(2)
    assert snapshot.operations["light_flag_updates"] == 1
    assert snapshot.operations["light_ping_colour_updates"] == 1
    assert snapshot.operations["drag_skipped_tooltip_writes"] == 3
    assert snapshot.operations["drag_skipped_popup_preparation"] == 1
    assert snapshot.operations.get("notify_connects", 0) == 0


def test_light_name_metrics_record_skipped_presentation_work(monkeypatch):
    monkeypatch.setattr(column_view, "_set_required_mods_pointer_cursor", lambda *_args: None)
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(1)
    column_view._bind_name_cell_light(
        make_name_cell(),
        column_view.ServerObject(name="Measured", ip="1.2.3.4", gport=2302),
        metrics,
    )
    snapshot = metrics.settle(1)
    assert snapshot.operations["drag_skipped_tooltip_writes"] == 3
    assert snapshot.operations["drag_skipped_css_writes"] == 3
    assert snapshot.operations["drag_skipped_popup_preparation"] == 1


def test_old_notify_identity_is_rejected_and_disconnect_clears_handler_state():
    old = SimpleNamespace(disconnect=lambda _hid: None)
    current = object()
    widget = SimpleNamespace(_dzll_notify_obj=current, _dzll_notify_ids=[7])
    assert not column_view._notify_is_current(widget, old)
    assert column_view._notify_is_current(widget, current)
    widget._dzll_notify_obj = old
    column_view._disconnect_notify_handlers(widget)
    assert widget._dzll_notify_obj is None
    assert widget._dzll_notify_ids == []


def test_bound_registry_retains_callback_local_wrappers_until_exact_unbind():
    registry = BoundCellRegistry()
    cell = WeakCell()
    obj = object()
    item = FakeListItem(cell, obj)
    refresh = lambda _item, _cell: None
    assert registry.register(
        "name", cell, item, obj, refresh, generation=1
    )
    cell_ref = weakref.ref(cell)
    item_ref = weakref.ref(item)
    del cell
    del item
    gc.collect()
    assert cell_ref() is not None
    assert item_ref() is not None
    assert len(registry) == 1
    assert registry.unregister(cell_ref(), item_ref())
    assert len(registry) == 0
    del obj
    gc.collect()
    assert cell_ref() is None
    assert item_ref() is None


def test_duplicate_registration_updates_current_object_and_stays_bounded():
    registry = BoundCellRegistry()
    cell = WeakCell()
    first = object()
    current = object()
    item = FakeListItem(cell, first)
    refresh = lambda _item, _cell: None
    assert registry.register(
        "name", cell, item, first, refresh, generation=2
    )
    item.item = current
    assert registry.register(
        "name", cell, item, current, refresh, generation=2
    )
    assert len(registry) == 1
    record = registry.resolve(id(cell))
    assert record.current_obj is current


def test_old_unbind_cannot_remove_newer_registration_for_same_cell():
    registry = BoundCellRegistry()
    cell = WeakCell()
    old_item = FakeListItem(cell, object())
    current_obj = object()
    current_item = FakeListItem(cell, current_obj)
    refresh = lambda _item, _cell: None
    registry.register(
        "name", cell, old_item, old_item.item, refresh, generation=1
    )
    registry.register(
        "name", cell, current_item, current_obj, refresh, generation=1
    )
    assert not registry.unregister(cell, old_item)
    assert len(registry) == 1
    assert registry.resolve(id(cell)).list_item is current_item


def test_registry_size_tracks_current_cells_not_rows_visited():
    registry = BoundCellRegistry()
    cells = [WeakCell() for _ in range(4)]
    items = [FakeListItem(cell) for cell in cells]
    refresh = lambda _item, _cell: None
    for row in range(100):
        index = row % len(cells)
        obj = object()
        items[index].item = obj
        registry.register(
            "name",
            cells[index],
            items[index],
            obj,
            refresh,
            generation=3,
        )
    assert len(registry) == len(cells)


def test_dead_or_disposed_cell_record_is_pruned_safely():
    class DeadItem(FakeListItem):
        dead = False

        def get_child(self):
            if self.dead:
                raise ReferenceError("disposed")
            return super().get_child()

    controller = ScrollbarDragLightController(True)
    controller.begin(5)
    cell = WeakCell()
    obj = object()
    item = DeadItem(cell, obj)
    refresh = lambda _item, _cell: None
    controller.register("name", cell, item, obj, refresh)
    item.dead = True
    controller.deactivate(5)
    assert controller.refresh_bound_cells() == {}
    assert len(controller.registry) == 0


def test_dead_weak_dispatcher_is_pruned_without_refresh():
    controller = ScrollbarDragLightController(True)
    controller.begin(6)
    cell = WeakCell()
    obj = object()
    item = FakeListItem(cell, obj)
    refresh = lambda _item, _cell: None
    controller.register("name", cell, item, obj, refresh)
    del refresh
    gc.collect()
    controller.deactivate(6)
    assert controller.refresh_bound_cells() == {}
    assert len(controller.registry) == 0


def test_settle_refreshes_each_live_cell_once_and_retains_bound_records():
    controller = ScrollbarDragLightController(True)
    controller.begin(9)
    calls = []
    cells = [WeakCell(), WeakCell()]
    objects = [object(), object()]
    items = [FakeListItem(cell, obj) for cell, obj in zip(cells, objects)]
    callbacks = []
    for cell, item, obj in zip(cells, items, objects):
        def refresh(_item, current, calls=calls):
            calls.append(current)

        callbacks.append(refresh)
        assert controller.register(
            "ping",
            cell,
            item,
            obj,
            refresh,
        )
    controller.deactivate(9)
    assert controller.refresh_bound_cells() == {"ping": 2}
    assert calls == cells
    assert len(controller.registry) == 2
    assert controller.refresh_bound_cells() == {}
    assert calls == cells


def test_settle_uses_current_object_after_recycling_same_cell():
    controller = ScrollbarDragLightController(True)
    controller.begin(10)
    cell = WeakCell()
    old = object()
    current = object()
    item = FakeListItem(cell, old)
    seen = []

    def refresh(current_item, _cell):
        seen.append(current_item.get_item())

    controller.register("name", cell, item, old, refresh)
    item.item = current
    controller.register("name", cell, item, current, refresh)
    controller.deactivate(10)
    assert controller.refresh_bound_cells() == {"name": 1}
    assert seen == [current]


def test_all_nine_factory_kinds_register_and_refresh_once():
    from dzll_launcher.scrollbar_interaction import SCROLLBAR_FACTORY_NAMES

    controller = ScrollbarDragLightController(True)
    controller.begin(11)
    callbacks = []
    cells = []
    items = []
    for factory_name in SCROLLBAR_FACTORY_NAMES:
        cell = WeakCell()
        obj = object()
        item = FakeListItem(cell, obj)

        def refresh(_item, _cell):
            return None

        callbacks.append(refresh)
        cells.append(cell)
        items.append(item)
        assert controller.register(factory_name, cell, item, obj, refresh)
    controller.deactivate(11)
    assert controller.refresh_bound_cells() == {
        name: 1 for name in SCROLLBAR_FACTORY_NAMES
    }


def test_stale_generation_entry_is_skipped_but_current_entry_refreshes():
    controller = ScrollbarDragLightController(True)
    callbacks = []
    controller.begin(1)
    stale_cell = WeakCell()
    stale_obj = object()
    stale_item = FakeListItem(stale_cell, stale_obj)
    stale_refresh = lambda _item, _cell: None
    callbacks.append(stale_refresh)
    controller.register("name", stale_cell, stale_item, stale_obj, stale_refresh)
    controller.deactivate(1)
    controller.begin(2)
    current_cell = WeakCell()
    current_obj = object()
    current_item = FakeListItem(current_cell, current_obj)
    seen = []

    def current_refresh(_item, _cell):
        seen.append("current")

    callbacks.append(current_refresh)
    controller.register(
        "ping", current_cell, current_item, current_obj, current_refresh
    )
    controller.deactivate(2)
    assert controller.refresh_bound_cells() == {"ping": 1}
    assert seen == ["current"]
    assert len(controller.registry) == 2


def test_registry_and_successful_settle_counts_are_instrumented(monkeypatch):
    values = iter((100, 160))
    monkeypatch.setattr(
        interaction_module.time,
        "perf_counter_ns",
        lambda: next(values),
    )
    controller = ScrollbarDragLightController(True)
    metrics = ScrollbarFactoryMetrics(True)
    controller.begin(12)
    metrics.begin(12)
    cell = WeakCell()
    obj = object()
    item = FakeListItem(cell, obj)
    refresh = lambda _item, _cell: None
    controller.register("name", cell, item, obj, refresh)
    controller.deactivate(12)
    assert controller.refresh_bound_cells(metrics) == {"name": 1}
    snapshot = metrics.settle(12)
    assert snapshot.settle_refresh_count == 1
    assert snapshot.operations["name_settle_cells_refreshed"] == 1
    assert snapshot.operations["settle_full_refresh_passes"] == 1
    assert snapshot.operations["registry_before_settle"] == 1
    assert snapshot.operations["registry_live"] == 1
    assert snapshot.operations["registry_stale"] == 0
    assert snapshot.operations["registry_after_settle"] == 1


def test_metrics_distinguish_light_full_and_settle_refresh_timing(monkeypatch):
    values = iter((10, 30, 40, 70, 80, 130))
    monkeypatch.setattr(interaction_module.time, "perf_counter_ns", lambda: next(values))
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(3)
    metrics.finish(metrics.start("name", "bind", bind_mode="light"))
    metrics.finish(metrics.start("ping", "bind", bind_mode="full"))
    token = metrics.start_settle_refresh("name")
    metrics.finish_settle_refresh(token)
    snapshot = metrics.settle(3)

    assert snapshot.factories["name"].light_bind_count == 1
    assert snapshot.factories["name"].light_bind_total_ns == 20
    assert snapshot.factories["ping"].full_bind_count == 1
    assert snapshot.settle_refresh_count == 1
    assert snapshot.settle_refresh_total_ns == 50
    assert snapshot.settle_refresh_max_factory == "name"


def test_disabled_metrics_use_no_clock_for_bind_or_settle_refresh(monkeypatch):
    monkeypatch.setattr(
        interaction_module.time,
        "perf_counter_ns",
        lambda: (_ for _ in ()).throw(AssertionError("disabled path used clock")),
    )
    metrics = ScrollbarFactoryMetrics(False)
    assert not metrics.begin(1)
    assert metrics.start("name", "bind", bind_mode="light") is None
    assert metrics.start_settle_refresh("name") is None
