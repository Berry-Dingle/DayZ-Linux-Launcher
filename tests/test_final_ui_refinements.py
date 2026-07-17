from pathlib import Path
from types import SimpleNamespace

from dzll_launcher.config import DISCLAIMER_TEXT
from dzll_launcher.styles import get_app_css
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]


def app_css() -> str:
    return get_app_css("#56575a", 220, "#717171").decode("utf-8")


class _FakeButton:
    def __init__(self):
        self.visible = None
        self.classes = {"server-companion-power-off-button"}
        self.tooltip = ""
        self.child = None

    def set_visible(self, visible):
        self.visible = bool(visible)

    def add_css_class(self, name):
        self.classes.add(name)

    def remove_css_class(self, name):
        self.classes.discard(name)

    def set_tooltip_text(self, text):
        self.tooltip = text

    def set_child(self, child):
        self.child = child


class _FakeIcon:
    def __init__(self):
        self.icon_name = "system-shutdown-symbolic"
        self.classes = {"server-companion-power-off-icon"}

    def set_from_icon_name(self, name):
        self.icon_name = name

    def add_css_class(self, name):
        self.classes.add(name)

    def remove_css_class(self, name):
        self.classes.discard(name)


class _FakeLabel:
    def __init__(self, text):
        self.text = text


def _companion_control_host(*, enabled: bool, docked: bool):
    return SimpleNamespace(
        settings={"show_server_companion": enabled},
        _server_companion_docked=docked,
        server_companion_show_btn=_FakeButton(),
        server_companion_show_icon=_FakeIcon(),
        server_companion_redock_icon=_FakeLabel("↙"),
    )


def test_companion_title_button_hidden_docked_and_shown_undocked():
    docked = _companion_control_host(enabled=True, docked=True)
    DZLLWindow._refresh_server_companion_power_controls(docked)
    assert docked.server_companion_show_btn.visible is False

    undocked = _companion_control_host(enabled=True, docked=False)
    DZLLWindow._refresh_server_companion_power_controls(undocked)
    assert undocked.server_companion_show_btn.visible is True
    assert undocked.server_companion_show_btn.tooltip == "Redock Server Companion"
    assert undocked.server_companion_show_btn.child is undocked.server_companion_redock_icon
    assert undocked.server_companion_show_btn.child.text == "↙"
    assert "server-companion-power-off-button" not in undocked.server_companion_show_btn.classes


def test_companion_redock_click_uses_existing_path_once_and_preserves_state():
    monitored = object()
    snapshot = {"name": "Preserved server", "queue": 3}
    host = SimpleNamespace(
        settings={"show_server_companion": True},
        _server_companion_docked=False,
        _server_companion_reparenting=False,
        _server_companion_obj=monitored,
        _server_companion_snapshot=snapshot,
    )
    calls = []

    def dock():
        calls.append("dock")
        host._server_companion_docked = True

    host._dock_server_companion = dock
    DZLLWindow._on_server_companion_title_button_clicked(host)
    DZLLWindow._on_server_companion_title_button_clicked(host)

    assert calls == ["dock"]
    assert host._server_companion_obj is monitored
    assert host._server_companion_snapshot is snapshot

    host._server_companion_docked = False
    host._server_companion_reparenting = True
    DZLLWindow._on_server_companion_title_button_clicked(host)
    assert calls == ["dock"]

    window_source = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    dock_block = window_source.split("    def _dock_server_companion(self):", 1)[1].split(
        "    def _collapse_server_companion_dock_space", 1
    )[0]
    assert "_start_server_companion_polling()" not in dock_block


def test_disclaimer_has_one_forced_break_and_word_wrapping():
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    assert DISCLAIMER_TEXT == (
        "DayZ® is a registered trademark of Bohemia Interactive.\n"
        "DZLL is an unofficial community-made launcher and is not affiliated with or endorsed by Bohemia Interactive."
    )
    assert DISCLAIMER_TEXT.count("\n") == 1
    assert "disclaimer.set_wrap(True)" in sidebar
    assert "disclaimer.set_wrap_mode(Pango.WrapMode.WORD)" in sidebar
    assert "disclaimer.set_wrap_mode(Pango.WrapMode.WORD_CHAR)" not in sidebar
    assert "disclaimer.set_hexpand(False)" in sidebar
    assert "disclaimer.set_width_chars(1)" in sidebar
    assert "disclaimer.set_max_width_chars(1)" in sidebar


def test_sidebar_and_redock_control_retain_fixed_compact_structure():
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    window = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    companion = (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8")
    assert "sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)" in sidebar
    assert "sidebar_frame.set_hexpand(False)" in sidebar
    assert "sidebar_frame.set_halign(Gtk.Align.START)" in sidebar
    assert "effective = SIDEBAR_WIDTH - (SIDEBAR_INNER_PADDING * 2)" in sidebar
    assert 'self.server_companion_redock_icon = Gtk.Label(label="↙")' in window
    assert 'self.dock_toggle_btn.set_label("↙")' in companion
    control_block = window.split("server_companion_show_icon =", 1)[1].split(
        "main.append(self.server_list_overlay)", 1
    )[0]
    assert "Gtk.Button()" in control_block
    assert "Gtk.MenuButton" not in control_block
    assert "Gtk.DropDown" not in control_block
    assert "DZLL Server Companion" not in control_block
    assert "set_size_request(28, 28)" in control_block
    assert 'add_css_class("flat")' in control_block


def test_general_settings_actions_share_horizontal_size_group():
    settings = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    assert 'Gtk.Button(label="Clear Played History")' in settings
    assert 'Gtk.Button(label="Update Server Database")' in settings
    assert "Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)" in settings
    assert "_general_action_button_size_group.add_widget(btn)" in settings
    assert "_general_action_button_size_group.add_widget(update_db_btn)" in settings


def test_selected_and_hovered_settings_rows_share_seven_pixel_radius():
    css = app_css()
    selected = css.split(".settings-nav row:selected {", 1)[1].split("}", 1)[0]
    selected_hover = css.split(".settings-nav row:selected:hover,", 1)[1].split("}", 1)[0]
    child = css.split(".settings-nav row:selected > * {", 1)[1].split("}", 1)[0]
    assert "background: @dzll_accent;" in selected
    assert "color: @dzll_text_on_accent;" in selected
    hover = css.split(".settings-nav row:hover {", 1)[1].split("}", 1)[0]
    hover_child = css.split(".settings-nav row:hover > * {", 1)[1].split("}", 1)[0]
    assert "border-radius: 7px;" in selected
    assert "border-radius: 7px;" in selected_hover
    assert "background: transparent;" in child
    assert "border-radius: 7px;" in child
    assert "background: @dzll_control_hover;" in hover
    assert "border-radius: 7px;" in hover
    assert "background: transparent;" in hover_child
    assert "border-radius: 7px;" in hover_child


def test_closed_dropdown_selected_item_neutralizes_only_button_stack_row():
    css = app_css()
    rule = css.split(
        ".dzll-app-root dropdown.dzll-dropdown > button stack,", 1
    )[1].split("}", 1)[0]
    assert "button stack > row:hover" in rule
    assert "button stack > row:selected" in rule
    assert "button stack > row > box" in rule
    assert "button stack > row:hover > box > label" in rule
    assert "background: transparent;" in rule
    assert "background-image: none;" in rule
    assert "box-shadow: none;" in rule
    assert "outline: none;" in rule
    assert "popover" not in rule
    assert ".dzll-dropdown popover row:hover" in css
