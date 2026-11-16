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
        self.tooltip = ""

    def set_tooltip_text(self, text):
        self.tooltip = text


class _FakeDot:
    def __init__(self):
        self.visible = None

    def set_visible(self, visible):
        self.visible = bool(visible)


def _companion_control_host(*, enabled: bool, docked: bool):
    return SimpleNamespace(
        settings={"show_server_companion": enabled},
        _server_companion_dock=SimpleNamespace(docked=docked),
        server_companion_toggle_btn=_FakeButton(),
        server_companion_toggle_dot=_FakeDot(),
    )


def test_companion_toggle_tooltip_and_dot_follow_enabled_state():
    off = _companion_control_host(enabled=False, docked=True)
    DZLLWindow._refresh_server_companion_power_controls(off)
    assert off.server_companion_toggle_btn.tooltip == "Turn on Server Companion"
    assert off.server_companion_toggle_dot.visible is False

    for docked in (True, False):
        on = _companion_control_host(enabled=True, docked=docked)
        DZLLWindow._refresh_server_companion_power_controls(on)
        assert on.server_companion_toggle_btn.tooltip == "Turn off Server Companion"
        assert on.server_companion_toggle_dot.visible is True


def test_companion_toggle_click_flips_enabled_state_and_ignores_reparenting():
    host = SimpleNamespace(
        settings={"show_server_companion": False},
        _server_companion_dock=SimpleNamespace(docked=True, reparenting=False),
    )
    calls = []

    def set_enabled(enabled):
        calls.append(enabled)
        host.settings["show_server_companion"] = enabled

    host.set_server_companion_enabled = set_enabled
    DZLLWindow._on_server_companion_toggle_clicked(host)
    DZLLWindow._on_server_companion_toggle_clicked(host)
    assert calls == [True, False]

    host._server_companion_dock.reparenting = True
    DZLLWindow._on_server_companion_toggle_clicked(host)
    assert calls == [True, False]

    dock_source = (ROOT / "src/dzll_launcher/server_companion_dock.py").read_text(encoding="utf-8")
    dock_block = dock_source.split("    def dock(self):", 1)[1].split(
        "    def collapse_dock_space", 1
    )[0]
    assert "_start_polling()" not in dock_block
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


def test_sidebar_companion_toggle_replaces_title_row_control():
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    window = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    companion = (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8")
    css = app_css()
    assert "sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)" in sidebar
    assert "sidebar_frame.set_hexpand(False)" in sidebar
    assert "sidebar_frame.set_halign(Gtk.Align.START)" in sidebar
    assert "effective = SIDEBAR_WIDTH - (SIDEBAR_INNER_PADDING * 2)" in sidebar
    # The browser title-row power/redock button is gone; the undocked panel
    # keeps its own redock control.
    assert "server_companion_show_btn" not in window
    assert "server_companion_redock_icon" not in window
    assert 'self.dock_toggle_btn.set_label("↙")' in companion
    control_block = sidebar.split("window.server_companion_toggle_btn = Gtk.Button()", 1)[1].split(
        "toolbar_icons_row.append(window.server_companion_toggle_btn)", 1
    )[0]
    assert 'add_css_class("flat")' in control_block
    assert "Gtk.MenuButton" not in control_block
    assert "window._on_server_companion_toggle_clicked()" in control_block
    assert '_sidebar_toolbar_icon("eyeIcon.svg")' in sidebar
    dot = css.split(".server-companion-toggle-dot {", 1)[1].split("}", 1)[0]
    assert "background-color: @dzll_mod_focus;" in dot
    assert "border-radius: 999px;" in dot


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
