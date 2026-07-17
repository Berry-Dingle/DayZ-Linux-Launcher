from pathlib import Path

from dzll_launcher.styles import add_platform_css_classes, get_app_css, is_ubuntu_platform


class CssClassRecorder:
    def __init__(self):
        self.classes = []

    def add_css_class(self, css_class):
        self.classes.append(css_class)


def write_os_release(tmp_path: Path, text: str) -> str:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return str(path)


def app_css() -> str:
    return get_app_css("#56575a", 220, "#717171").decode("utf-8")


def test_ubuntu_os_release_adds_platform_root_class(tmp_path):
    path = write_os_release(tmp_path, "ID=ubuntu\nID_LIKE=debian\n")
    widget = CssClassRecorder()

    assert is_ubuntu_platform(path) is True
    assert add_platform_css_classes(widget, path) is True
    assert widget.classes == ["dzll-platform-ubuntu"]


def test_fedora_os_release_does_not_add_ubuntu_class(tmp_path):
    path = write_os_release(tmp_path, "ID=fedora\nID_LIKE=\"rhel centos\"\n")
    widget = CssClassRecorder()

    assert is_ubuntu_platform(path) is False
    assert add_platform_css_classes(widget, path) is False
    assert widget.classes == []


def test_ubuntu_geometry_rules_are_all_root_scoped():
    css = app_css()
    geometry_selectors = [
        line.strip()
        for line in css.splitlines()
        if "nth-child(-n+7)" in line
        or "listview > row > cell" in line
        or ("header > button" in line and "dzll-platform-ubuntu" in line)
    ]

    assert geometry_selectors
    assert all(line.startswith(".dzll-platform-ubuntu ") for line in geometry_selectors)
    assert "dzll-yaru-geometry" not in css


def test_shared_fedora_geometry_matches_pre_ubuntu_defaults():
    css = app_css()
    rows = css.split(".dzll-column-view row,", 1)[1].split("}", 1)[0]
    assert {line.strip() for line in rows.splitlines() if line.strip()} == {
        ".dzll-column-view listitem {",
        "padding-top: 4px;",
        "padding-bottom: 5px;",
        "border-bottom: 1px solid @dzll_divider;",
    }

    source = Path("src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
    players = source.split("def _make_players_factory(", 1)[1].split(
        "def bind(_factory, list_item):", 1
    )[0]
    assert "ubuntu_geometry: bool = False" in source
    assert "content.set_hexpand(not ubuntu_geometry)" in players
    assert "else:\n            outer.append(content)" in players


def test_startup_detection_is_single_root_decision_passed_to_column_view():
    window = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    assert window.count("add_platform_css_classes(overlay)") == 1
    assert "ubuntu_geometry=self._ubuntu_geometry" in window


def test_followup_row_height_reduction_is_ubuntu_only():
    css = app_css()
    ubuntu_rows = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > listview > row {", 1
    )[1].split("}", 1)[0]
    shared_rows = css.split(".dzll-column-view row,", 1)[1].split("}", 1)[0]

    assert "padding-top: 2px;" in ubuntu_rows
    assert "padding-bottom: 3px;" in ubuntu_rows
    assert "padding-top: 4px;" in shared_rows
    assert "padding-bottom: 5px;" in shared_rows
    assert "padding-top: 2px;" not in shared_rows
    assert "padding-bottom: 3px;" not in shared_rows


def test_followup_scrollbar_state_gap_is_ubuntu_browser_and_mod_manager_only():
    css = app_css()
    normal = css.split(
        ".dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical > range > trough > slider,", 1
    )[1].split("}", 1)[0]
    states = css.split(
        ".dzll-platform-ubuntu .dzll-browser-surface scrollbar.vertical.overlay-indicator.hovering", 1
    )[1].split("}", 1)[0]

    assert "margin-top: 2px;" in normal
    assert "margin-bottom: 2px;" in normal
    assert ".mods-card .mods-list" in normal
    assert ".overlay-indicator.dragging" in states
    assert "slider:hover" in states
    assert "slider:active" in states
    assert "margin-top: 1px;" in states
    assert "margin-bottom: 3px;" in states
    assert states.count(".mods-card .mods-list") == 4
    assert states.count(".dzll-platform-ubuntu") == 7
    assert "margin-left" not in states
    assert "margin-right" not in states


def test_played_uses_existing_centered_factory_and_ubuntu_cell_scope():
    css = app_css()
    source = Path("src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
    cell_rule = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > listview > row > cell {", 1
    )[1].split("}", 1)[0]

    assert '"PLAYED",\n        _make_label_factory(_bind_played' in source
    assert "padding-left: 0;" in cell_rule
    assert "padding-right: 0;" in cell_rule


def test_final_header_increase_and_sc_alignment_are_ubuntu_only():
    css = app_css()
    window = Path("src/dzll_launcher/window.py").read_text(encoding="utf-8")
    ubuntu_header = css.split(
        ".dzll-platform-ubuntu columnview.dzll-column-view > header > button {", 1
    )[1].split("}", 1)[0]
    shared_header = css.rsplit(
        "columnview.dzll-column-view > header > button,\n"
        "        columnview.dzll-column-view > header > button:hover,", 1
    )[1].split("}", 1)[0]

    assert "min-height: 38px;" in ubuntu_header
    assert "min-height" not in shared_header
    assert "set_margin_top(5 if self._ubuntu_geometry else 4)" in window
