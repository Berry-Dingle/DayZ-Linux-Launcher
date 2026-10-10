from types import SimpleNamespace

from dzll_launcher import column_view
from dzll_launcher.ui_row import ServerObject


class FakeWidget:
    def __init__(self, text="", visible=True):
        self.text = text
        self.visible = visible
        self.tooltip = None

    def get_text(self):
        return self.text

    def set_text(self, value):
        self.text = value

    def get_visible(self):
        return self.visible

    def set_visible(self, value):
        self.visible = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = value

    def add_css_class(self, _cls):
        pass

    def remove_css_class(self, _cls):
        pass


class FakeButton(FakeWidget):
    def set_child(self, _child):
        pass


def make_name_cell():
    return SimpleNamespace(
        _dzll_lock_label=FakeWidget(),
        _dzll_name_label=FakeWidget(),
        _dzll_perspective_label=FakeWidget(),
        _dzll_flag_label=FakeWidget(),
        _dzll_ipport_label=FakeWidget(),
        _dzll_mods_label=FakeWidget(),
        _dzll_mods_button=FakeButton(),
        _dzll_mods_button_label=FakeWidget(),
    )


def test_name_change_after_bind_refreshes_cell_live():
    cell = make_name_cell()
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="", map_name="")
    column_view._bind_name_cell(cell, obj, None)
    column_view._connect_name_cell_notify_handlers(cell, obj, None)
    assert cell._dzll_name_label.text == ""

    obj.name = "Direct Connect Server"

    assert cell._dzll_name_label.text == "Direct Connect Server"


def test_country_change_after_bind_refreshes_flag_live():
    cell = make_name_cell()
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, country="")
    column_view._bind_name_cell(cell, obj, None)
    column_view._connect_name_cell_notify_handlers(cell, obj, None)
    assert cell._dzll_flag_label.text == ""

    obj.country = "DE"

    assert cell._dzll_flag_label.text != ""


def test_unrelated_property_change_does_not_require_notify_handler():
    cell = make_name_cell()
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="Steady")
    column_view._bind_name_cell(cell, obj, None)
    column_view._connect_name_cell_notify_handlers(cell, obj, None)

    obj.ping = 42  # not one of _NAME_CELL_NOTIFY_PROPS

    assert cell._dzll_name_label.text == "Steady"


def test_rebind_to_new_object_disconnects_old_object_notify():
    cell = make_name_cell()
    old = ServerObject(ip="1.1.1.1", gport=2302, qport=2303, name="Old")
    new = ServerObject(ip="2.2.2.2", gport=2302, qport=2303, name="New")
    column_view._bind_name_cell(cell, old, None)
    column_view._connect_name_cell_notify_handlers(cell, old, None)

    column_view._disconnect_notify_handlers(cell, None, "name")
    column_view._bind_name_cell(cell, new, None)
    column_view._connect_name_cell_notify_handlers(cell, new, None)
    assert cell._dzll_name_label.text == "New"

    old.name = "Old Changed"

    assert cell._dzll_name_label.text == "New"


def test_empty_cell_cannot_be_updated_by_stale_object_notify():
    cell = make_name_cell()
    obj = ServerObject(ip="1.2.3.4", gport=2302, qport=2303, name="Bound")
    column_view._bind_name_cell(cell, obj, None)
    column_view._connect_name_cell_notify_handlers(cell, obj, None)

    column_view._disconnect_notify_handlers(cell, None, "name")
    column_view._bind_name_cell(cell, None, None)

    obj.name = "Changed After Unbind"

    assert cell._dzll_name_label.text == ""
