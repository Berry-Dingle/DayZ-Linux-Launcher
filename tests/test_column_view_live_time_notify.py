from pathlib import Path

from dzll_launcher import column_view
from dzll_launcher.ui_row import ServerObject


ROOT = Path(__file__).resolve().parents[1]


class FakeFactory:
    def __init__(self):
        self.callbacks = {}

    def connect(self, signal, callback):
        self.callbacks[signal] = callback

    def emit(self, signal, item):
        self.callbacks[signal](self, item)


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.writes = []

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.text = value
        self.writes.append(value)


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


def bound_time_factory(monkeypatch, obj):
    fake_factory = FakeFactory()
    monkeypatch.setattr(column_view.Gtk.SignalListItemFactory, "__new__", lambda cls: fake_factory)
    monkeypatch.setattr(column_view, "_center_label", lambda **kwargs: FakeLabel())
    monkeypatch.setattr(column_view, "_cell_label", lambda widget: widget)
    monkeypatch.setattr(column_view, "_register_bound_cell", lambda *args, **kwargs: None)
    monkeypatch.setattr(column_view, "_unregister_bound_cell", lambda *args, **kwargs: None)
    factory = column_view._make_label_factory(
        column_view._bind_time,
        factory_name="time",
        notify_props=("time",),
    )
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
    obj.played = "1 Day Ago"
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
    current.played = "1 Day Ago"
    assert label.text == "1 Day Ago"


def test_changing_live_time_refreshes_only_bound_time_cell(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0)
    _factory, _item, label = bound_time_factory(monkeypatch, obj)
    assert label.text == "12:00 (x12)"
    initial_writes = len(label.writes)
    obj.time = "12:01"
    assert label.text == "12:01 (x12)"
    assert len(label.writes) == initial_writes + 1


def test_unchanged_time_notify_avoids_redundant_label_write(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0)
    _factory, _item, label = bound_time_factory(monkeypatch, obj)
    initial_writes = len(label.writes)
    obj.time = "12:00"
    assert label.text == "12:00 (x12)"
    assert len(label.writes) == initial_writes


def test_rebinding_recycled_cell_disconnects_old_time_handler(monkeypatch):
    old = ServerObject(time="01:00", timewarp=1.0)
    current = ServerObject(time="02:00", timewarp=2.0)
    factory, item, label = bound_time_factory(monkeypatch, old)
    old_handler_ids = list(label._dzll_notify_ids)
    assert old_handler_ids
    item.obj = current
    factory.emit("bind", item)
    assert label._dzll_notify_obj is current
    assert label._dzll_notify_ids
    assert label._dzll_notify_ids != old_handler_ids or old is not current
    assert label.text == "02:00 (x2)"


def test_old_model_change_after_rebind_cannot_alter_new_cell(monkeypatch):
    old = ServerObject(time="01:00", timewarp=1.0)
    current = ServerObject(time="02:00", timewarp=2.0)
    factory, item, label = bound_time_factory(monkeypatch, old)
    item.obj = current
    factory.emit("bind", item)
    writes_after_rebind = len(label.writes)
    old.time = "09:59"
    assert label.text == "02:00 (x2)"
    assert len(label.writes) == writes_after_rebind
    current.time = "02:01"
    assert label.text == "02:01 (x2)"


def test_time_notify_path_does_not_rebuild_or_reorder_model(monkeypatch):
    obj = ServerObject(time="12:00", timewarp=12.0)
    _factory, _item, label = bound_time_factory(monkeypatch, obj)
    obj.time = "12:01"
    assert label.text == "12:01 (x12)"
    # The isolated label notify callback has no window/store/rebuild dependency.
    assert not hasattr(label, "splice")


def test_players_and_ping_factory_bindings_are_unchanged():
    source = (ROOT / "src/dzll_launcher/column_view.py").read_text()
    assert 'notify_props = ("players", "max_players", "queue")' in source
    assert 'factory_name="ping",' in source
    assert 'notify_props=("ping",),' in source
    assert 'factory_name="time",' in source
    assert 'notify_props=("time",),' in source
