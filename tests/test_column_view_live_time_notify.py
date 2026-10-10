from pathlib import Path
import pytest

from dzll_launcher import column_view
from dzll_launcher.scrollbar_interaction import ScrollbarDragLightController
from dzll_launcher.ui_row import ServerObject, time_speed_texture


ROOT = Path(__file__).resolve().parents[1]


def test_time_icons_are_cached_fourteen_pixel_textures():
    for filename in ("sunIcon.png", "moonIcon.png"):
        texture = time_speed_texture(filename)
        assert (texture.get_width(), texture.get_height()) == (14, 14)
        assert time_speed_texture(filename) is texture


def test_time_remains_one_fixed_nonsortable_column():
    source = (ROOT / "src/dzll_launcher/column_view.py").read_text()
    assert source.count('        "TIME",\n        _make_time_factory(') == 1
    assert column_view._META_WIDTHS["time"] == 116
    assert "TIME" not in column_view._SORTABLE_HEADER_KEYS


class FakeFactory:
    def __init__(self):
        self.callbacks = {}

    def connect(self, signal, callback):
        self.callbacks[signal] = callback

    def emit(self, signal, item):
        self.callbacks[signal](self, item)


class FakeLabel:
    def __init__(self, **_kwargs):
        self.text = ""
        self.writes = []
        self.xalign = _kwargs.get("xalign")
        self.width_chars = None

    def set_width_chars(self, value):
        self.width_chars = value

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.text = value
        self.writes.append(value)

    def __getattr__(self, name):
        if name.startswith("set_") or name == "add_css_class":
            return lambda *_args: None
        raise AttributeError(name)


class FakeBox:
    def __init__(self, **kwargs):
        self.children = []
        self.spacing = kwargs.get("spacing")
        self.margin_start = 0
        self.margin_end = 0
        self.hexpand = False
        self.size_request = None

    def set_size_request(self, width, height):
        self.size_request = (width, height)

    def set_hexpand(self, value):
        self.hexpand = value

    def set_margin_start(self, value):
        self.margin_start = value

    def set_margin_end(self, value):
        self.margin_end = value

    def append(self, child):
        self.children.append(child)

    def __getattr__(self, name):
        if name.startswith("set_") or name == "add_css_class":
            return lambda *_args: None
        raise AttributeError(name)


class FakePicture(FakeBox):
    @classmethod
    def new_for_paintable(cls, paintable):
        picture = cls()
        picture.paintable = paintable
        return picture

    def set_size_request(self, width, height):
        self.size_request = (width, height)


class FakeListItem:
    def __init__(self, obj):
        self.obj = obj
        self.child = None

    def get_item(self):
        return self.obj

    def get_child(self):
        return self.child

    def set_child(self, child):
        self.child = child


def bound_time_factory(monkeypatch, obj, drag_light=None):
    fake_factory = FakeFactory()
    monkeypatch.setattr(column_view.Gtk.SignalListItemFactory, "__new__", lambda cls: fake_factory)
    monkeypatch.setattr(column_view.Gtk, "Box", FakeBox)
    monkeypatch.setattr(column_view.Gtk, "Label", FakeLabel)
    monkeypatch.setattr(column_view.Gtk, "Picture", FakePicture)
    monkeypatch.setattr(column_view, "time_speed_texture", lambda filename: filename)
    factory = column_view._make_time_factory(drag_light=drag_light)
    item = FakeListItem(obj)
    factory.emit("setup", item)
    factory.emit("bind", item)
    return factory, item, item.child


def bound_played_factory(monkeypatch, obj):
    fake_factory = FakeFactory()
    monkeypatch.setattr(column_view.Gtk.SignalListItemFactory, "__new__", lambda cls: fake_factory)
    monkeypatch.setattr(column_view, "_center_label", lambda **kwargs: FakeLabel())
    monkeypatch.setattr(column_view, "_cell_label", lambda widget: widget)
    monkeypatch.setattr(column_view, "_register_bound_cell", lambda *args, **kwargs: None)
    monkeypatch.setattr(column_view, "_unregister_bound_cell", lambda *args, **kwargs: None)
    factory = column_view._make_label_factory(
        column_view._bind_played,
        factory_name="played",
        notify_props=("played",),
    )
    item = FakeListItem(obj)
    factory.emit("setup", item)
    factory.emit("bind", item)
    return factory, item, item.child


def test_bound_played_cell_updates_on_notify(monkeypatch):
    obj = ServerObject(played="20 Days Ago")
    factory, item, label = bound_played_factory(monkeypatch, obj)
    assert label.text == "20 Days Ago"
    obj.played = "Today"
    assert label.text == "Today"
    factory.emit("unbind", item)
    obj.played = "Yesterday"
    assert label.text == "Today"


def test_recycled_played_cell_disconnects_old_object(monkeypatch):
    old = ServerObject(played="20 Days Ago")
    current = ServerObject(played="2 Days Ago")
    factory, item, label = bound_played_factory(monkeypatch, old)
    item.obj = current
    factory.emit("bind", item)
    assert label._dzll_notify_obj is current
    assert len(label._dzll_notify_ids) == 1
    assert label.text == "2 Days Ago"
    writes = len(label.writes)
    old.played = "Today"
    assert label.text == "2 Days Ago"
    assert len(label.writes) == writes
    current.played = "Yesterday"
    assert label.text == "Yesterday"


def test_changing_live_time_refreshes_only_bound_time_cell(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0, night_timewarp=4.5)
    _factory, _item, cell = bound_time_factory(monkeypatch, obj)
    assert (cell._dzll_time_main.text, cell._dzll_time_day.text, cell._dzll_time_night.text) == ("12:00", "x12", "x5")
    initial_writes = len(cell._dzll_time_main.writes)
    obj.time = "12:01"
    assert cell._dzll_time_main.text == "12:01"
    assert len(cell._dzll_time_main.writes) == initial_writes + 1
    obj.night_timewarp = 8.5
    assert cell._dzll_time_night.text == "x9"
    obj.timewarp = None
    assert cell._dzll_time_day.text == "x?"


def test_unchanged_time_notify_avoids_redundant_label_write(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0)
    _factory, _item, cell = bound_time_factory(monkeypatch, obj)
    initial_writes = len(cell._dzll_time_main.writes)
    obj.time = "12:00"
    assert cell._dzll_time_main.text == "12:00"
    assert len(cell._dzll_time_main.writes) == initial_writes


def test_rebinding_recycled_cell_disconnects_old_time_handler(monkeypatch):
    old = ServerObject(time="01:00", timewarp=1.0, night_timewarp=8.0)
    current = ServerObject(time="02:00", timewarp=2.0)
    factory, item, cell = bound_time_factory(monkeypatch, old)
    old_handler_ids = list(cell._dzll_notify_ids)
    assert old_handler_ids
    item.obj = current
    factory.emit("bind", item)
    assert cell._dzll_notify_obj is current
    assert cell._dzll_notify_ids
    assert cell._dzll_notify_ids != old_handler_ids or old is not current
    assert cell._dzll_time_main.text == "02:00"
    assert cell._dzll_time_day.text == "x2"
    assert cell._dzll_time_night.text == "x?"


def test_old_model_change_after_rebind_cannot_alter_new_cell(monkeypatch):
    old = ServerObject(time="01:00", timewarp=1.0)
    current = ServerObject(time="02:00", timewarp=2.0)
    factory, item, cell = bound_time_factory(monkeypatch, old)
    item.obj = current
    factory.emit("bind", item)
    writes_after_rebind = len(cell._dzll_time_main.writes)
    old.time = "09:59"
    assert cell._dzll_time_main.text == "02:00"
    assert len(cell._dzll_time_main.writes) == writes_after_rebind
    current.time = "02:01"
    assert cell._dzll_time_main.text == "02:01"


def test_time_notify_path_does_not_rebuild_or_reorder_model(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0)
    _factory, _item, cell = bound_time_factory(monkeypatch, obj)
    obj.time = "12:01"
    assert cell._dzll_time_main.text == "12:01"
    # The isolated label notify callback has no window/store/rebuild dependency.
    assert not hasattr(cell, "splice")


def test_players_and_ping_factory_bindings_are_unchanged():
    source = (ROOT / "src/dzll_launcher/column_view.py").read_text()
    assert 'notify_props = ("players", "max_players", "queue")' in source
    assert 'factory_name="ping",' in source
    assert 'notify_props=("ping",),' in source
    assert '_make_time_factory(perf_metrics, drag_light)' in source
    assert 'notify_props = ("time", "timewarp", "night-timewarp")' in source


@pytest.mark.parametrize("day,night,expected", [
    (4.2, 4.4, ("x4", "x4")),
    (4.5, 4.9, ("x5", "x5")),
    (2.17, 11.6, ("x2", "x12")),
    (2.5, 8.5, ("x3", "x9")),
    (1.0, None, ("x1", "x?")),
    (None, 1.0, ("x?", "x1")),
    (None, None, ("x?", "x?")),
    ("bad", float("inf"), ("x?", "x?")),
    (0, -1, ("x?", "x?")),
])
def test_time_speed_formatting_and_binding(monkeypatch, day, night, expected):
    obj = ServerObject(time="18:30", timewarp=day, night_timewarp=night)
    factory, item, cell = bound_time_factory(monkeypatch, obj)
    assert cell._dzll_time_main.text == "18:30"
    assert (cell._dzll_time_day.text, cell._dzll_time_night.text) == expected
    assert cell._dzll_time_stack.spacing == 0
    assert len(cell._dzll_time_stack.children) == 2
    assert cell.spacing == 0
    assert len(cell.children) == 3
    assert cell.children[1] is cell._dzll_time_group
    assert cell.children[0].hexpand and cell.children[2].hexpand
    assert cell.children[0].size_request == (4, -1)
    assert cell._dzll_time_group.children == [cell._dzll_time_main, cell._dzll_time_stack]
    assert cell._dzll_time_group.spacing == 4
    assert cell._dzll_time_main.xalign == 0.5
    assert cell._dzll_time_main.width_chars == 5
    assert (cell._dzll_time_stack.margin_start, cell._dzll_time_stack.margin_end) == (2, 2)
    assert all(row.spacing == 2 for row in cell._dzll_time_stack.children)
    assert all(row.children[1].xalign == 0.0 and row.children[1].width_chars == 4
               for row in cell._dzll_time_stack.children)
    assert cell._dzll_time_stack.children[0].children[0].paintable == "sunIcon.png"
    assert cell._dzll_time_stack.children[1].children[0].paintable == "moonIcon.png"
    assert all(row.children[0].size_request == (14, 14)
               for row in cell._dzll_time_stack.children)
    factory.emit("unbind", item)
    assert (cell._dzll_time_main.text, cell._dzll_time_day.text, cell._dzll_time_night.text) == ("", "", "")
    assert cell._dzll_bound_obj is None
    assert cell._dzll_notify_obj is None


def test_light_bind_recycling_and_settle_restores_full_notifications(monkeypatch):
    drag = ScrollbarDragLightController(True)
    assert drag.begin(7)
    old = ServerObject(time="18:30", timewarp=8.0, night_timewarp=12.0)
    current = ServerObject(time="--:--", timewarp=None, night_timewarp=2.5)
    factory, item, cell = bound_time_factory(monkeypatch, old, drag)
    assert (cell._dzll_time_main.text, cell._dzll_time_day.text, cell._dzll_time_night.text) == ("18:30", "x8", "x12")
    assert cell._dzll_notify_obj is None
    item.obj = current
    factory.emit("bind", item)
    assert (cell._dzll_time_main.text, cell._dzll_time_day.text, cell._dzll_time_night.text) == ("--:--", "x?", "x3")
    old.time = "19:00"
    current.time = "07:00"
    assert cell._dzll_time_main.text == "--:--"
    assert drag.deactivate(7)
    drag.refresh_bound_cells()
    assert cell._dzll_notify_obj is current
    assert cell._dzll_time_main.text == "07:00"
    current.time = "07:01"
    assert cell._dzll_time_main.text == "07:01"
    factory.emit("unbind", item)
    assert len(drag.registry) == 0
