from pathlib import Path

from dzll_launcher.styles import get_app_css


ROOT = Path(__file__).resolve().parents[1]


def app_css() -> str:
    return get_app_css("#56575a", 220, "#717171").decode("utf-8")


def rule_after(css: str, selector: str) -> str:
    return css.split(selector, 1)[1].split("}", 1)[0]


def test_progressbar_uses_accent_fill_and_grey_trough():
    css = app_css()
    trough = rule_after(css, ".dzll-app-root progressbar trough,")
    fill = rule_after(css, ".dzll-app-root progressbar progress,")

    assert "background: @dzll_surface_control;" in trough
    assert "background-color: @dzll_surface_control;" in trough
    assert "background-image: none;" in trough
    assert "@dzll_accent" not in trough
    assert "background: @dzll_accent;" in fill
    assert "background-color: @dzll_accent;" in fill
    assert "background-image: none;" in fill
    assert "border-color: transparent;" in fill
    assert "border-image: none;" in fill
    assert "box-shadow: none;" in fill
    assert "outline: none;" in fill
    assert "text-shadow: none;" in fill
    assert "opacity: 1;" in fill
    assert "filter: none;" in fill
    assert "#0d686c" not in fill


def test_full_refresh_progress_inherits_fill_and_keeps_thin_geometry():
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    css = app_css()
    scoped = rule_after(css, ".dzll-app-root .status-refresh-progress-bar trough,")

    assert "Gtk.ProgressBar()" in sidebar
    assert 'add_css_class("status-refresh-progress-bar")' in sidebar
    assert "min-height: 3px;" in scoped
    assert ".status-refresh-progress-bar progress" in css
    assert "progressbar.status-refresh-progress-bar" not in css


def test_scale_uses_accent_highlight_grey_trough_and_neutral_thumb():
    css = app_css()
    trough = rule_after(css, ".dzll-app-root scale trough,")
    highlight = rule_after(css, ".dzll-app-root scale highlight,")
    slider = rule_after(css, ".dzll-app-root scale slider,")

    assert "background: @dzll_surface_control;" in trough
    assert "background-color: @dzll_surface_control;" in trough
    assert "background-image: none;" in trough
    assert "background: @dzll_accent;" in highlight
    assert "background-color: @dzll_accent;" in highlight
    assert "background-image: none;" in highlight
    assert "border-color: transparent;" in highlight
    assert "border-image: none;" in highlight
    assert "box-shadow: none;" in highlight
    assert "outline: none;" in highlight
    assert "text-shadow: none;" in highlight
    assert "opacity: 1;" in highlight
    assert "filter: none;" in highlight
    assert "background: @dzll_scale_knob;" in slider
    assert "@dzll_accent" not in slider


def test_alert_volume_and_all_current_ordinary_controls_use_common_rules():
    companion = (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8")
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    steamcmd = (ROOT / "src/dzll_launcher/join_preparation_overlay_ui.py").read_text(encoding="utf-8")

    assert companion.count("Gtk.Scale.new_with_range") == 1
    assert "self.alert_volume_scale = Gtk.Scale.new_with_range" in companion
    assert "self.alert_volume_scale.set_sensitive(False)" in companion
    assert sidebar.count("Gtk.ProgressBar()") == 1
    assert steamcmd.count("Gtk.ProgressBar()") == 1
    assert "warning" not in sidebar[sidebar.index("Gtk.ProgressBar()") - 120:sidebar.index("Gtk.ProgressBar()") + 180]
    assert "error" not in steamcmd[steamcmd.index("Gtk.ProgressBar()") - 120:steamcmd.index("Gtk.ProgressBar()") + 180]


def test_disabled_and_backdrop_scale_colours_remain_explicit_and_readable():
    css = app_css()
    trough = rule_after(css, ".server-companion-panel:backdrop scale trough {")
    highlight = rule_after(css, ".server-companion-panel:backdrop scale highlight {")
    slider = rule_after(css, ".server-companion-panel:backdrop scale.horizontal slider {")

    assert "background-color: @dzll_surface_control;" in trough
    assert "opacity: 1;" in trough
    assert "background-color: @dzll_accent;" in highlight
    assert "opacity: 1;" in highlight
    assert "background-color: @dzll_scale_knob;" in slider
    assert "opacity: 1;" in slider


def test_progress_and_scale_state_selectors_keep_the_active_nodes_teal():
    css = app_css()
    for state in ("hover", "active", "focus", "focus-within", "backdrop", "disabled"):
        assert f".dzll-app-root progressbar:{state} progress" in css
        assert f".server-companion-panel progressbar:{state} progress" in css
        assert f".dzll-app-root scale:{state} highlight" in css
        assert f".server-companion-panel scale:{state} highlight" in css
    for state in ("hover", "active", "focus", "backdrop", "disabled"):
        assert f".dzll-app-root progressbar progress:{state}" in css
        assert f".server-companion-panel progressbar progress:{state}" in css
        assert f".dzll-app-root scale highlight:{state}" in css
        assert f".server-companion-panel scale highlight:{state}" in css

    progress_rule = rule_after(css, ".dzll-app-root progressbar progress,")
    highlight_rule = rule_after(css, ".dzll-app-root scale highlight,")
    for rule in (progress_rule, highlight_rule):
        assert "background-color: @dzll_accent;" in rule
        assert "border-color: transparent;" in rule
        assert "box-shadow: none;" in rule
        assert "opacity: 1;" in rule


def test_rules_are_scoped_to_progress_and_scale_nodes_only():
    css = app_css()
    assert ".dzll-app-root progressbar progress" in css
    assert ".dzll-app-root scale highlight" in css
    assert ".dzll-app-root scale slider" in css
    assert "@define-color dzll_accent #0d686c;" in css
    assert "@define-color dzll_surface_control #2f3438;" in css
    assert "@define-color dzll_scale_knob #c4c9cd;" in css
