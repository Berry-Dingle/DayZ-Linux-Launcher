from pathlib import Path

from dzll_launcher.styles import get_app_css


ROOT = Path(__file__).resolve().parents[1]


def app_css() -> str:
    return get_app_css("#56575a", 220, "#717171").decode("utf-8")


def test_required_semantic_palette_tokens_are_defined():
    css = app_css()
    required = {
        "dzll_surface_panel": "#202326",
        "dzll_surface_control": "#2f3438",
        "dzll_surface_content": "#141618",
        "dzll_surface_settings": "#181818",
        "dzll_accent": "#0d686c",
        "dzll_text_primary": "#f1f3f4",
        "dzll_text_secondary": "#c4c9cd",
        "dzll_text_muted": "#8e979e",
        "dzll_mod_metadata": "#e2e5e7",
        "dzll_text_disabled": "#858d94",
        "dzll_focus": "#79aeb0",
        "dzll_border": "#565f66",
        "dzll_hover_border": "#68727a",
        "dzll_divider": "#3b4248",
        "dzll_entry_focus": "#b6bdc2",
        "dzll_mod_focus": "#4fb8b2",
        "dzll_scrollbar_border": "#3fa9a5",
        "dzll_switch_knob_on": "#d5dadd",
    }
    for name, value in required.items():
        assert f"@define-color {name} {value};" in css


def test_dzll_owned_css_has_no_gtk_theme_colour_dependency():
    css = app_css()
    assert "@theme_base_color" not in css
    assert "@theme_text_color" not in css


def test_root_scope_and_major_surface_rules_are_present():
    css = app_css()
    for selector_or_rule in (
        ".dzll-app-root",
        ".dzll-app-root .sidebar-frame",
        ".dzll-app-root .dzll-browser-surface",
        "columnview.dzll-column-view",
        ".server-companion-panel",
        ".settings-panel .settings-content",
        ".mods-card .mods-list",
        ".required-mods-popover",
        ".update-card",
    ):
        assert selector_or_rule in css


def test_selected_settings_navigation_uses_semantic_accent():
    css = app_css()
    selected_rule = css.split(".settings-nav row:selected {", 1)[1].split("}", 1)[0]
    hover_rule = css.split(".settings-nav row:hover {", 1)[1].split("}", 1)[0]
    hover_child = css.split(".settings-nav row:hover > * {", 1)[1].split("}", 1)[0]
    assert "background: @dzll_accent;" in selected_rule
    assert "color: @dzll_text_on_accent;" in selected_rule
    assert "border-radius: 7px;" in selected_rule
    assert "background: @dzll_control_hover;" in hover_rule
    assert "border-radius: 7px;" in hover_rule
    assert "background: transparent;" in hover_child
    assert "border-radius: 7px;" in hover_child


def test_root_and_feature_styling_hooks_are_applied():
    sources = {
        "window": (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8"),
        "settings": (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8"),
        "mods": (ROOT / "src/dzll_launcher/mods_ui.py").read_text(encoding="utf-8"),
        "companion": (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8"),
    }
    assert 'overlay.add_css_class("dzll-app-root")' in sources["window"]
    assert 'add_css_class("settings-content")' in sources["settings"]
    assert 'add_css_class("mods-search")' in sources["mods"]
    assert 'add_css_class("mods-row")' in sources["mods"]
    assert 'add_css_class("server-companion-content")' in sources["companion"]
    assert 'add_css_class("companion-sound-popover")' in sources["companion"]


def test_companion_voice_popover_only_makes_its_outer_surface_transparent():
    css = app_css()
    outer = css.split(".companion-sound-popover {", 1)[1].split("}", 1)[0]
    contents = css.split(".companion-sound-popover > contents {", 1)[1].split("}", 1)[0]
    arrow = css.split(".companion-sound-popover > arrow {", 1)[1].split("}", 1)[0]

    assert "background: transparent;" in outer
    assert "border-color: transparent;" in outer
    assert "box-shadow: none;" in outer
    assert "background: @dzll_surface_content;" in contents
    assert "border-color: @dzll_border;" in contents
    assert "background: @dzll_surface_content;" in arrow
    assert "border-color: @dzll_border;" in arrow
    assert "popover {" not in outer


def test_server_companion_adjacent_join_header_has_no_right_border():
    css = app_css()
    source = (ROOT / "src" / "dzll_launcher" / "column_view.py").read_text(
        encoding="utf-8",
    )
    rule = css.split(
        "columnview.dzll-column-view > header > button.dzll-column-title-join,",
        1,
    )[1].split("}", 1)[0]

    assert 'if column_index == len(title_buttons) - 1:' in source
    assert '_add_css_classes(button, "dzll-column-title-join")' in source
    assert "border-right: 0;" in rule
    assert "border-top" not in rule
    assert "border-bottom" not in rule
    assert "border-left" not in rule


def test_sidebar_no_longer_installs_a_second_css_provider():
    source = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    assert "Gtk.CssProvider" not in source
    assert "add_provider_for_display" not in source


def test_hover_border_is_softer_than_focus_and_dropdown_hover_is_semantic():
    css = app_css()
    assert "@define-color dzll_hover_border #68727a;" in css
    assert "border-color: @dzll_hover_border;" in css
    assert "border-color: @dzll_focus;" in css
    dropdown_hover = css.split(".dzll-app-root dropdown > button:hover,", 1)[1].split("}", 1)[0]
    assert "background: @dzll_control_hover;" in dropdown_hover
    assert "background-image: none;" in dropdown_hover
    assert "border-color: @dzll_hover_border;" in dropdown_hover
    assert "@theme" not in dropdown_hover


def test_dropdown_popup_rows_have_distinct_hover_and_selection_states():
    css = app_css()
    hover = css.split(".dzll-app-root popover row:hover,", 1)[1].split("}", 1)[0]
    selected = css.split(".dzll-app-root popover row:selected,", 1)[1].split("}", 1)[0]
    selected_hover = css.split(".dzll-app-root popover row:selected:hover,", 1)[1].split("}", 1)[0]
    assert "background: @dzll_control_hover;" in hover
    assert "background: @dzll_selection;" in selected
    assert "background: @dzll_accent_hover;" in selected_hover
    sidebar = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    settings = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    assert 'map_dropdown.add_css_class("dzll-dropdown")' in sidebar
    assert 'dd.add_css_class("dzll-dropdown")' in settings


def test_scrollbar_thumb_is_clean_and_browser_track_is_transparent():
    css = app_css()
    thumb = css.split(".dzll-app-root scrollbar slider,", 1)[1].split("}", 1)[0]
    browser_track = css.split(".dzll-app-root .dzll-browser-surface scrollbar,", 1)[1].split("}", 1)[0]
    assert "background: @dzll_surface_control;" in thumb
    assert "border: 1px solid @dzll_scrollbar_border;" in thumb
    assert thumb.count("border:") == 1
    assert "outline: none;" in thumb
    assert "box-shadow: none;" in thumb
    assert "min-width: 6px;" in css
    assert "min-height: 6px;" in css
    assert "background: transparent;" in browser_track
    assert "box-shadow: none;" in browser_track


def test_dropdown_popup_track_is_transparent():
    css = app_css()
    track = css.split(".dzll-dropdown popover scrollbar,", 1)[1].split("}", 1)[0]
    assert "background: transparent;" in track
    assert "border-color: transparent;" in track
    assert "outline: none;" in track
    assert "box-shadow: none;" in track


def test_normal_search_focus_is_neutral_and_mod_focus_is_teal():
    css = app_css()
    normal = css.split(".dzll-app-root entry:focus,", 1)[1].split("}", 1)[0]
    mod = css.split("entry.top-search-entry.mod-search-entry-active:focus,", 1)[1].split("}", 1)[0]
    assert "border-color: @dzll_entry_focus;" in normal
    assert "@dzll_mod_focus" not in normal
    assert "border-color: @dzll_mod_focus;" in mod
    assert "outline-color: @dzll_mod_focus;" in mod


def test_dropdown_closed_states_explicitly_suppress_theme_blue_fill():
    css = app_css()
    assert ".dzll-app-root dropdown.dzll-dropdown > button" in css
    assert ".dzll-app-root dropdown.dzll-dropdown:focus > button" in css
    assert ".dzll-app-root dropdown.dzll-dropdown > button:checked" in css
    assert ".dzll-app-root dropdown.dzll-dropdown > button:selected" in css
    assert css.count("background-image: none;") >= 5
    assert "background-color: @dzll_control_hover;" in css
    assert "background-color: @dzll_control_pressed;" in css


def test_queue_uses_primary_text_in_browser_and_companion():
    css = app_css()
    assert "@define-color dzll_queue #f1f3f4;" in css
    assert ".dzll-column-view .dzll-queue" in css
    assert ".server-companion-panel .companion-queue" in css
    companion_queue = css.split(".server-companion-panel .companion-queue {", 1)[1].split("}", 1)[0]
    backdrop_queue = css.split(".server-companion-panel:backdrop .companion-queue {", 1)[1].split("}", 1)[0]
    assert "color: @dzll_text_primary;" in companion_queue
    assert "color: @dzll_text_primary;" in backdrop_queue


def test_required_mods_popover_has_transparent_outer_and_dark_card():
    css = app_css()
    outer = css.split(".required-mods-popover {", 1)[1].split("}", 1)[0]
    card = css.split(".required-mods-popover > contents {", 1)[1].split("}", 1)[0]
    assert "background: transparent;" in outer
    assert "border: none;" in outer
    assert "box-shadow: none;" in outer
    assert "background: @dzll_surface_content;" in card
    assert "border: 1px solid @dzll_border;" in card


def test_server_companion_backdrop_preserves_feature_colours():
    css = app_css()
    for selector_or_rule in (
        ".server-companion-panel:backdrop label",
        ".server-companion-panel:backdrop .companion-queue",
        ".server-companion-panel:backdrop .companion-restart-learning-value",
        ".server-companion-panel:backdrop button:not(.flat)",
        ".server-companion-panel:backdrop button.suggested-action",
        ".server-companion-panel:backdrop switch:checked",
        ".server-companion-panel:backdrop switch slider",
        ".server-companion-panel:backdrop separator",
        ".server-companion-panel:backdrop .ping-good",
        ".server-companion-panel:backdrop .ping-offline",
        ".server-companion-panel:backdrop button.server-companion-power-on-button",
        ".server-companion-panel:backdrop button.server-companion-power-off-button",
        ".server-companion-panel:backdrop scale.horizontal slider",
    ):
        assert selector_or_rule in css
    assert "filter: none;" in css


def test_monitor_eye_backdrop_colours_are_explicit():
    css = app_css()
    column_view = (ROOT / "src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
    idle = css.split("button.monitor-btn,", 1)[1].split("}", 1)[0]
    active = css.split("button.flat.monitor-btn.monitor-btn-active,", 1)[1].split("}", 1)[0]
    idle_image = css.split(
        ".dzll-app-root .dzll-column-view button.monitor-btn:backdrop image.monitor-eye-idle {", 1
    )[1].split("}", 1)[0]
    assert "button.monitor-btn:backdrop" in idle
    assert "color: @dzll_monitor_idle;" in idle
    assert "opacity: 1;" in idle
    assert "button.flat.monitor-btn.monitor-btn-active:backdrop" in active
    assert "color: @dzll_monitor_active;" in active
    assert "filter: none;" in active
    assert 'image.add_css_class("monitor-eye-idle")' in column_view
    assert 'image.remove_css_class("monitor-eye-idle")' in column_view
    assert "color: @dzll_monitor_idle;" in idle_image
    assert "-gtk-icon-filter: none;" in idle_image
    assert "opacity: 1;" in idle_image
    assert "filter: none;" in idle_image
    assert "dzll-join-button" not in idle_image


def test_restart_learning_text_is_primary_and_confidence_has_own_hook():
    css = app_css()
    companion = (ROOT / "src/dzll_launcher/server_companion_ui.py").read_text(encoding="utf-8")
    value = css.split(".server-companion-panel .companion-restart-learning-value {", 1)[1].split("}", 1)[0]
    backdrop = css.split(".server-companion-panel:backdrop .companion-restart-learning-value {", 1)[1].split("}", 1)[0]
    assert "color: @dzll_text_primary;" in value
    assert "color: @dzll_text_primary;" in backdrop
    assert 'add_css_class("companion-restart-confidence-value")' in companion
    assert ".companion-restart-confidence-value.ping-good" in css
    assert ".companion-restart-confidence-value.ping-greeny" in css
    assert ".companion-restart-confidence-value.ping-yellow" in css
    assert ".companion-restart-confidence-value.ping-orange" in css


def _relative_luminance(hex_value: str) -> float:
    channels = [int(hex_value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted((_relative_luminance(foreground), _relative_luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_muted_text_contrast_stays_above_small_text_requirement():
    muted = "#8e979e"
    for surface in ("#202326", "#181818", "#141618"):
        assert _contrast(muted, surface) >= 4.5


def test_switch_knob_has_one_solid_layer_without_fuzzy_edges():
    css = app_css()
    slider = css.split(".dzll-app-root switch slider,", 1)[1].split("}", 1)[0]
    assert "background: @dzll_text_secondary;" in slider
    assert "border-color: transparent;" in slider
    assert "border: 1px" not in slider
    assert "outline: none;" in slider
    assert "box-shadow: none;" in slider
    assert "opacity: 1;" in slider
    checked = css.split(".dzll-app-root switch:checked slider,", 1)[1].split("}", 1)[0]
    assert "background: @dzll_switch_knob_on;" in checked
    assert "@dzll_text_on_accent" not in checked


def test_companion_volume_slider_has_one_crisp_layer():
    css = app_css()
    slider = css.split(".server-companion-panel scale.horizontal slider {", 1)[1].split("}", 1)[0]
    backdrop = css.split(".server-companion-panel:backdrop scale.horizontal slider {", 1)[1].split("}", 1)[0]
    for rule in (slider, backdrop):
        assert "background: @dzll_scale_knob;" in rule
        assert "background-color: @dzll_scale_knob;" in rule
        assert "background-image: none;" in rule
        assert "border-color: transparent;" in rule
        assert "border-image: none;" in rule
        assert "outline: none;" in rule
        assert "box-shadow: none;" in rule
        assert "text-shadow: none;" in rule
        assert "opacity: 1;" in rule
        assert "filter: none;" in rule


def test_mod_manager_scrollbar_track_is_transparent_with_browser_matching_gap():
    css = app_css()
    track = css.split(
        ".dzll-app-root .mods-card .mods-list scrollbar,", 1
    )[1].split("}", 1)[0]
    gap = css.split(
        ".dzll-app-root .mods-card .mods-list scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    browser_gap = css.split(
        ".dzll-app-root .dzll-browser-surface scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    assert "background: transparent;" in track
    assert "border-color: transparent;" in track
    assert "outline: none;" in track
    assert "box-shadow: none;" in track
    assert "margin-top: 3px;" in gap
    assert gap.strip() == browser_gap.strip()
    assert "min-width: 6px;" in css
    assert "required-mods-popover" not in gap
    assert "dzll-dropdown" not in gap
    assert "server-companion-panel" not in gap


def test_startup_spinner_has_dedicated_thirty_pixel_hook():
    css = app_css()
    startup = (ROOT / "src/dzll_launcher/startup_ui.py").read_text(encoding="utf-8")
    rule = css.split(".startup-spinner {", 1)[1].split("}", 1)[0]
    generic = css.split(".dzll-app-root spinner,", 1)[1].split("}", 1)[0]
    assert 'startup_spinner.add_css_class("startup-spinner")' in startup
    assert "min-width: 30px;" in rule
    assert "min-height: 30px;" in rule
    assert "min-width" not in generic
    assert "min-height" not in generic


def test_only_browser_vertical_scrollbar_has_three_pixel_top_gap():
    css = app_css()
    rule = css.split(
        ".dzll-app-root .dzll-browser-surface scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    assert "margin-top: 3px;" in rule
    assert "required-mods-popover" not in rule
    assert "dzll-dropdown" not in rule
    assert "server-companion-panel" not in rule
    assert "mods-card" not in rule
    assert "min-width: 6px;" in css


def test_mod_manager_disabled_actions_are_consistently_subdued():
    css = app_css()
    disabled = css.split(".mods-card button:disabled,", 1)[1].split("}", 1)[0]
    children = css.split(".mods-card button:disabled label,", 1)[1].split("}", 1)[0]
    assert ".mods-card button.mods-danger-action:disabled:hover" in css
    assert ".mods-card button.mods-stop-action:disabled:hover" in css
    assert "background: @dzll_control_disabled;" in disabled
    assert "color: @dzll_text_disabled;" in disabled
    assert "border-color: @dzll_divider;" in disabled
    assert "background-image: none;" in disabled
    assert "color: @dzll_text_disabled;" in children


def test_mod_manager_row_hover_has_no_fill_change():
    css = app_css()
    normal = css.split(".mods-card row,", 1)[1].split("}", 1)[0]
    hover = css.split(
        ".dzll-app-root .mods-card row.mods-row:hover,", 1
    )[1].split("}", 1)[0]
    assert "background: @dzll_surface_content;" in normal
    assert "background: @dzll_surface_content;" in hover
    assert "@dzll_control_hover" not in hover


def test_mod_manager_row_divider_is_owned_by_row_bottom_border():
    css = app_css()
    row_border = css.split(".mods-card .mods-list row.mods-row {", 1)[1].split("}", 1)[0]
    assert "border-top: 0;" in row_border
    assert "border-right: 0;" in row_border
    assert "border-bottom: 1px solid @dzll_divider;" in row_border
    assert "border-left: 0;" in row_border
    assert ".mods-card row separator" not in css
    assert ".mods-card .mods-column-separator" in css

    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    row_source = body.split("    def _make_row(", 1)[1].split("    def _loaded_item_id_set", 1)[0]
    assert "Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)" not in row_source
    assert 'add_css_class("mods-row-divider-spacer")' in row_source


def test_mod_manager_row_spacing_and_border_inset_preserve_content_geometry():
    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    row_source = body.split("    def _make_row(", 1)[1].split("    def _loaded_item_id_set", 1)[0]
    assert "row.set_margin_start(MOD_ROW_BORDER_INSET)" in row_source
    assert "row.set_margin_end(MOD_ROW_BORDER_INSET)" in row_source
    assert "Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)" in row_source
    assert "outer.set_margin_top(9)" in row_source
    assert "outer.set_margin_bottom(9)" in row_source
    assert "outer.set_margin_start(MOD_ROW_CONTENT_INSET)" in row_source
    assert "outer.set_margin_end(MOD_ROW_CONTENT_INSET)" in row_source


def test_mod_manager_header_uses_finalized_row_column_geometry():
    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    header_source = body.split("    def _make_column_header", 1)[1].split("    def _make_sort_header_label", 1)[0]
    assert "header.set_margin_start(MOD_ROW_BORDER_INSET + 5)" in header_source
    assert "header.set_margin_end(MOD_ROW_BORDER_INSET + 5)" in header_source
    assert "header_tail.set_margin_start(0)" in header_source
    assert "spacing=8" in header_source
    assert "MOD_WORKSHOP_COLUMN_CHARS" in header_source
    assert "MOD_ID_COLUMN_WIDTH" in header_source
    assert "MOD_SIZE_COLUMN_CHARS" in header_source
    assert "MOD_LAST_USED_COLUMN_CHARS" in header_source
    assert "MOD_REPAIR_COLUMN_WIDTH" in header_source
    assert 'Gtk.Label(label="")' in header_source


def test_mod_manager_repair_column_is_inactive_and_narrow():
    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    assert "MOD_REPAIR_COLUMN_WIDTH = 48" in body
    repair_source = body.split("    def _make_repair_control", 1)[1].split("    def _current_row_for_mod", 1)[0]
    assert '"tools-symbolic"' in repair_source
    assert 'set_tooltip_text("Repair Mod")' in repair_source
    row_source = body.split("    def _make_row(", 1)[1].split("    def _loaded_item_id_set", 1)[0]
    assert "self._make_repair_control(int(mod_id))" in row_source


def test_mod_manager_repair_icon_is_unboxed_and_orange():
    css = app_css()
    assert "button.flat.mods-repair-icon-btn" in css
    assert "color: @dzll_repair_orange;" in css
    assert "color: @dzll_repair_orange_hover;" in css
    repair = css.split("button.flat.mods-repair-icon-btn", 1)[1]
    assert "background: transparent;" in repair
    assert "border: none;" in repair
    assert "box-shadow: none;" in repair
    assert "outline: none;" in repair
    assert "button.flat.mods-repair-icon-btn:hover" in css
    assert "button.flat.mods-repair-icon-btn:active" in css

    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    repair_source = body.split("    def _make_repair_control", 1)[1].split("    def _current_row_for_mod", 1)[0]
    assert '"tools-symbolic"' in repair_source
    assert 'add_css_class("mods-repair-icon-btn")' in repair_source
    assert 'set_tooltip_text("Repair Mod")' in repair_source
    assert "attach_pointer_cursor(btn)" in repair_source


def test_server_background_download_icon_is_folder_download_and_magenta():
    css = app_css()
    assert "button.flat.dzll-download-mods-button" in css
    assert "color: #d946ef;" in css
    assert "button.flat.dzll-download-mods-button:hover" in css
    assert "color: #e879f9;" in css

    source = ROOT / "src" / "dzll_launcher" / "column_view.py"
    body = source.read_text(encoding="utf-8")
    icon_helper = body.split("def _download_icon_name", 1)[1].split(
        "def ", 1,
    )[0]
    assert 'return "folder-download-symbolic"' in icon_helper
    assert 'return "document-save-symbolic"' not in icon_helper
    assert 'return "go-down-symbolic"' not in icon_helper
    factory = body.split("download_factory = _make_action_factory", 1)[1].split(
        "_append_column", 1,
    )[0]
    assert '"dzll-download-mods-button"' in factory
    assert '"Subscribe and download required\\n"' in factory
    assert '"mods without joining\\n"' in factory
    assert '"Servers can be queued"' in factory


def test_mod_manager_workshop_link_states_target_symbolic_image():
    css = app_css()
    normal = css.split(
        ".dzll-app-root .mods-card button.flat.mod-workshop-link-btn,", 1
    )[1].split("}", 1)[0]
    hover = css.split(
        ".dzll-app-root .mods-card button.flat.mod-workshop-link-btn:hover,", 1
    )[1].split("}", 1)[0]
    backdrop = css.split(
        ".dzll-app-root .mods-card button.flat.mod-workshop-link-btn:backdrop,", 1
    )[1].split("}", 1)[0]
    disabled = css.split(
        ".dzll-app-root .mods-card button.flat.mod-workshop-link-btn:disabled,", 1
    )[1].split("}", 1)[0]
    assert "button.flat.mod-workshop-link-btn image" in css
    assert "button.flat.mod-workshop-link-btn:hover image" in css
    assert "button.flat.mod-workshop-link-btn:backdrop image" in css
    assert "button.flat.mod-workshop-link-btn:disabled image" in css
    assert "color: @dzll_link;" in normal
    assert "background: transparent;" in normal
    assert "color: @dzll_link_hover;" in hover
    assert "color: @dzll_link;" in backdrop
    assert "-gtk-icon-filter: none;" in backdrop
    assert "color: @dzll_text_disabled;" in disabled
    assert ".dzll-app-root button.flat.mod-workshop-link-btn" not in css


def test_mod_manager_metadata_is_brighter_without_changing_primary_or_link_roles():
    css = app_css()
    source = (ROOT / "src/dzll_launcher/mods_ui.py").read_text(encoding="utf-8")
    metadata = css.split(".mods-card .mods-row .mods-metadata {", 1)[1].split("}", 1)[0]
    disabled = css.split(
        ".mods-card .mods-row:disabled .mods-metadata,", 1
    )[1].split("}", 1)[0]
    names = css.split(".mods-card .mods-name {", 1)[1].split("}", 1)[0]
    workshop_link = css.split(
        ".dzll-app-root .mods-card button.flat.mod-workshop-link-btn,", 1
    )[1].split("}", 1)[0]

    assert "@define-color dzll_text_muted #8e979e;" in css
    assert "@define-color dzll_mod_metadata #e2e5e7;" in css
    assert "color: @dzll_mod_metadata;" in metadata
    assert "color: @dzll_text_disabled;" in disabled
    assert source.count('add_css_class("mods-metadata")') == 4
    assert "color: @dzll_text_primary;" in names
    assert "color: @dzll_link;" in workshop_link


def test_server_browser_column_header_hover_and_press_match_normal_paint():
    css = app_css()
    paint = css.split("columnview.dzll-column-view > header,", 1)[1].split("}", 1)[0]
    borders = css.rsplit(
        "columnview.dzll-column-view > header > button,\n"
        "        columnview.dzll-column-view > header > button:hover,", 1
    )[1].split("}", 1)[0]
    assert "columnview.dzll-column-view > header > button:active" in borders
    assert "button.dzll-column-title-flat:hover" in borders
    assert "button.dzll-column-title-flat:active" in borders
    assert "border-color: @dzll_border;" in borders
    assert "background: @dzll_surface_panel;" in paint
    assert "background-color: @dzll_surface_panel;" in paint
    assert "background-image: none;" in paint
    assert "box-shadow: none;" in paint
    assert "outline: none;" in paint
    assert "@dzll_control_hover" not in paint
    assert "@dzll_control_pressed" not in paint
    assert ".mods-column-header" not in borders


def test_server_browser_column_geometry_is_explicit_and_state_stable():
    css = app_css()
    header = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > header > button {", 1
    )[1].split("}", 1)[0]
    dividers = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > header > button:nth-child(-n+7),", 1
    )[1].split("}", 1)[0]
    cells = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > listview > row > cell {", 1
    )[1].split("}", 1)[0]

    assert "min-height: 38px;" in header
    assert "button:nth-child(-n+7):hover" in dividers
    assert "button:nth-child(-n+7):active" in dividers
    assert "button:nth-child(-n+7):checked" in dividers
    assert "border-right: 1px solid @dzll_divider;" in dividers
    assert "padding-left: 0;" in cells
    assert "padding-right: 0;" in cells

    source = (ROOT / "src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
    players_setup = source.split("def _make_players_factory(", 1)[1].split(
        "def bind(_factory, list_item):", 1
    )[0]
    assert "content.set_halign(Gtk.Align.CENTER)" in players_setup
    assert "content.set_hexpand(not ubuntu_geometry)" in players_setup
    assert "if ubuntu_geometry:" in players_setup
    assert "else:\n            outer.append(content)" in players_setup
    assert "start_spacer.set_hexpand(True)" in players_setup
    assert "end_spacer.set_hexpand(True)" in players_setup
    assert "outer.append(start_spacer)" in players_setup
    assert "outer.append(end_spacer)" in players_setup

def test_server_browser_row_hover_matches_normal_without_targeting_controls():
    css = app_css()
    hover = css.split(
        ".dzll-app-root columnview.dzll-column-view > listview > row:hover,", 1
    )[1].split("}", 1)[0]
    wrappers = css.split(
        ".dzll-app-root columnview.dzll-column-view > listview > row:hover > listitem > *,", 1
    )[1].split("}", 1)[0]
    assert "background: @dzll_surface_content;" in hover
    assert "background-color: @dzll_surface_content;" in hover
    assert "border-bottom-color: @dzll_divider;" in hover
    assert "color: @dzll_text_primary;" in hover
    assert "opacity: 1;" in hover
    assert "background: transparent;" in wrappers
    assert "button" not in hover
    assert "button" not in wrappers
    assert ".mods-card" not in hover
    assert ".dzll-dropdown" not in hover


def test_approved_geometry_is_limited_to_specific_styling_hooks():
    css = app_css()

    vertical_scrollbar = css.split(
        ".server-companion-panel scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    assert vertical_scrollbar.strip() == "min-width: 6px;"

    browser_scrollbar = css.split(
        ".dzll-app-root .dzll-browser-surface scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    mods_scrollbar = css.split(
        ".dzll-app-root .mods-card .mods-list scrollbar.vertical {", 1
    )[1].split("}", 1)[0]
    assert browser_scrollbar.strip() == "margin-top: 3px;"
    assert mods_scrollbar.strip() == "margin-top: 3px;"

    startup_spinner = css.split(".startup-spinner {", 1)[1].split("}", 1)[0]
    assert {line.strip() for line in startup_spinner.splitlines() if line.strip()} == {
        "min-width: 30px;",
        "min-height: 30px;",
    }

    settings_hover = css.split(".settings-nav row:hover {", 1)[1].split("}", 1)[0]
    settings_selected = css.split(".settings-nav row:selected {", 1)[1].split("}", 1)[0]
    assert "border-radius: 7px;" in settings_hover
    assert "border-radius: 7px;" in settings_selected

    settings_source = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    assert "Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)" in settings_source
    assert "_general_action_button_size_group.add_widget(btn)" in settings_source
    assert "_general_action_button_size_group.add_widget(update_db_btn)" in settings_source

    sidebar_source = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    assert "sidebar_frame.set_size_request(SIDEBAR_WIDTH, -1)" in sidebar_source
    assert "sidebar_frame.set_hexpand(False)" in sidebar_source

    # The approved geometry is feature-scoped; no unqualified native-node rule
    # should enlarge every scrollbar, spinner, or hovered row in GTK.
    assert "\n        scrollbar.vertical {" not in css
    assert "\n        spinner {" not in css
    assert "\n        row:hover {" not in css
