"""Real GTK rendering of the mod suggestion rows under a hostile light theme.

The rows are Gtk.ListBox rows. Themes such as Breeze Light paint ``list row``
white and ``row.activatable:hover`` / ``row:selected`` system blue. DZLL must
own the row background, text, hover and selection colours regardless.
"""

import re
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

from dzll_launcher import mod_suggestions, sidebar_ui  # noqa: E402
from dzll_launcher.styles import get_app_css  # noqa: E402
from dzll_launcher.window import DZLLWindow  # noqa: E402

PAYLOADS = (
    '[{"steamWorkshopId":"42","name":"Code Lock"},'
    '{"steamWorkshopId":"77","name":"Community Framework"},'
    '{"steamWorkshopId":"88","name":"Combat Tweaks"}]',
)

# Breeze Light list/row rules (``/usr/share/themes/Breeze/gtk-4.0/gtk.css``),
# loaded above any installed system theme but below DZLL's provider.
HOSTILE_LIGHT_THEME = b"""
list { color: #232629; background: white; }
list row { background: white; padding: 2px; }
list label { color: #232629; }
row.activatable:hover, row.activatable:active { background-color: #3daee9; }
row:selected, row:selected:backdrop { background-color: #3daee9; color: white; }
"""

APP_CSS = get_app_css("#56575a", 220, "#717171")
TOKENS = dict(re.findall(r"@define-color (\w+) (#[0-9a-fA-F]{6});", APP_CSS.decode("utf-8")))


def rgb(hex_value):
    return tuple(int(hex_value[i:i + 2], 16) for i in (1, 3, 5))


def assert_close(actual, expected_hex, tolerance=2):
    expected = rgb(expected_hex)
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected)), (actual, expected_hex)


def pump(rounds=30):
    context = GLib.MainContext.default()
    for _ in range(rounds):
        while context.pending():
            context.iteration(False)
        GLib.usleep(4000)


@pytest.fixture
def styled_suggestions(monkeypatch):
    Gtk.init_check()
    display = Gdk.Display.get_default()
    if display is None:
        pytest.skip("usable GTK display is unavailable")
    theme = Gtk.CssProvider()
    theme.load_from_data(HOSTILE_LIGHT_THEME, -1)
    app = Gtk.CssProvider()
    app.load_from_data(APP_CSS, -1)
    Gtk.StyleContext.add_provider_for_display(display, theme, Gtk.STYLE_PROVIDER_PRIORITY_SETTINGS)
    Gtk.StyleContext.add_provider_for_display(display, app, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    overlay = Gtk.Overlay()
    overlay.add_css_class("dzll-app-root")
    host = SimpleNamespace(
        _main_overlay=overlay,
        _apply_search_filter_changed=lambda **_kwargs: None,
        _cancel_search_filter_debounce=lambda: None,
        _on_search_changed_debounced=lambda: None,
    )
    host._mod_suggestions = SimpleNamespace(
        ready=True,
        restore_pending=False,
        restore_inflight=False,
        index=mod_suggestions.build_mod_suggestion_index(PAYLOADS),
        rebuild_index=lambda: None,
    )
    host._mod_suggestion_popover = DZLLWindow._build_mod_suggestion_popover(host)
    # Focus-loss hiding is deferred through GLib.idle_add; keep the panel up
    # while the test window may not hold keyboard focus.
    monkeypatch.setattr(sidebar_ui.GLib, "idle_add", lambda *_args: 0)
    overlay.set_child(sidebar_ui.build_search_area(host))
    window = Gtk.Window()
    window.set_default_size(420, 260)
    window.set_child(overlay)
    window.present()
    pump()
    host.search_entry.set_text("x -mods=co")
    host.search_entry.set_position(-1)
    host._mod_suggestion_popover.refresh()
    pump()
    try:
        yield window, host
    finally:
        window.destroy()
        Gtk.StyleContext.remove_provider_for_display(display, app)
        Gtk.StyleContext.remove_provider_for_display(display, theme)
        pump(3)


def render_row(window, panel, row):
    width, height = panel.get_width(), panel.get_height()
    paintable = Gtk.WidgetPaintable.new(panel)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    texture = window.get_renderer().render_texture(
        snapshot.to_node(), Graphene.Rect().init(0, 0, width, height))
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    buffer = data.get_data()
    ok, bounds = row.compute_bounds(panel)
    assert ok
    # Sample the row background at its right edge, clear of the label text.
    x = int(bounds.get_x() + bounds.get_width() - 4)
    y = int(bounds.get_y() + bounds.get_height() / 2)
    offset = y * stride + x * 4
    return tuple(buffer[offset:offset + 3])


def label_rgb(row):
    colour = row.get_child().get_color()
    return tuple(round(channel * 255) for channel in (colour.red, colour.green, colour.blue))


def test_suggestion_rows_use_dzll_colours_under_a_light_theme(styled_suggestions):
    window, host = styled_suggestions
    panel, suggestion_list = host.mod_suggestion_panel, host.mod_suggestion_list
    rows = [suggestion_list.get_row_at_index(index) for index in range(3)]
    assert all(rows) and panel.get_visible()
    assert suggestion_list.get_selected_row() is rows[0]

    # Unselected row: DZLL dark surface and primary text (never white on white).
    assert_close(render_row(window, panel, rows[1]), TOKENS["dzll_surface_content"])
    assert_close(label_rgb(rows[1]), TOKENS["dzll_text_primary"])

    # Keyboard selection: DZLL teal selection with on-accent text.
    assert_close(render_row(window, panel, rows[0]), TOKENS["dzll_selection"])
    assert_close(label_rgb(rows[0]), TOKENS["dzll_text_on_accent"])

    # Hover: DZLL teal, not the theme's system blue.
    rows[2].set_state_flags(Gtk.StateFlags.PRELIGHT, False)
    pump(5)
    assert_close(render_row(window, panel, rows[2]), TOKENS["dzll_accent_hover"])
    assert_close(label_rgb(rows[2]), TOKENS["dzll_text_on_accent"])
    rows[2].unset_state_flags(Gtk.StateFlags.PRELIGHT)

    # Selected and hovered.
    rows[0].set_state_flags(Gtk.StateFlags.PRELIGHT, False)
    pump(5)
    assert_close(render_row(window, panel, rows[0]), TOKENS["dzll_accent_hover"])
    rows[0].unset_state_flags(Gtk.StateFlags.PRELIGHT)

    # Window backdrop keeps the selection visible in DZLL teal.
    for row in rows:
        row.set_state_flags(Gtk.StateFlags.BACKDROP, False)
    pump(5)
    assert_close(render_row(window, panel, rows[0]), TOKENS["dzll_selection"])
    assert_close(render_row(window, panel, rows[1]), TOKENS["dzll_surface_content"])
    for row in rows:
        row.unset_state_flags(Gtk.StateFlags.BACKDROP)


def test_keyboard_navigation_moves_the_dzll_selection_highlight(styled_suggestions):
    window, host = styled_suggestions
    panel, suggestion_list = host.mod_suggestion_panel, host.mod_suggestion_list
    rows = [suggestion_list.get_row_at_index(index) for index in range(3)]

    assert host._mod_suggestion_popover.on_key_pressed(None, Gdk.KEY_Down, 0, 0) is True
    pump(5)
    assert suggestion_list.get_selected_row() is rows[1]
    assert_close(render_row(window, panel, rows[1]), TOKENS["dzll_selection"])
    assert_close(label_rgb(rows[1]), TOKENS["dzll_text_on_accent"])
    assert_close(render_row(window, panel, rows[0]), TOKENS["dzll_surface_content"])
    assert_close(label_rgb(rows[0]), TOKENS["dzll_text_primary"])
