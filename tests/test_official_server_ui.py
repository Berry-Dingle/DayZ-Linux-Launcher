from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

from dzll_launcher import settings, ui_row
from dzll_launcher.settings_ui import SettingsUI
from dzll_launcher.ui_row import ServerObject, ServerRowWidget


ROOT = Path(__file__).resolve().parents[1]
BADGE = ROOT / "src/dzll_launcher/images/official-badge.svg"


def test_official_setting_defaults_persists_and_rejects_invalid_type(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert settings.DEFAULTS["hide_official_servers"] is False
    assert settings.load_settings()["hide_official_servers"] is False
    saved = settings.load_settings()
    saved["hide_official_servers"] = True
    settings.save_settings(saved)
    assert settings.load_settings()["hide_official_servers"] is True
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "invalid.json"))
    (tmp_path / "invalid.json").write_text('{"hide_official_servers": "true"}', encoding="utf-8")
    assert settings.load_settings()["hide_official_servers"] is False


def test_sidebar_toggle_order_and_runtime_filter_refresh():
    source = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")
    assert source.index('"prioritise_trusted_servers",\n        "Trusted First"') < source.index(
        'sidebar_setting_checkbutton("hide_official_servers", "Hide Official Servers")'
    ) < source.index('compact_int_setting_entry("high_ping_cutoff_ms"')

    calls = []
    host = SimpleNamespace(
        settings={"hide_official_servers": True},
        _sidebar_settings_widgets={},
        _on_filter_changed=lambda **kwargs: calls.append(kwargs),
    )
    panel = SettingsUI.__new__(SettingsUI)
    panel._win = host
    panel._apply_setting_runtime_effects("hide_official_servers")
    assert calls == [{"reason": "settings"}]


def test_svg_is_flat_vector_shield_with_check_and_packaged():
    assert BADGE.is_file()
    source = BADGE.read_text(encoding="utf-8")
    root = ElementTree.parse(BADGE).getroot()
    ns = "{http://www.w3.org/2000/svg}"
    assert (root.get("width"), root.get("height")) == ("18", "18")
    assert root.get("viewBox") == "0 0 20 20"
    assert set(root.attrib) == {"width", "height", "viewBox"}
    assert "data:" not in source.lower()
    assert not any(key.lower().endswith("href") for element in root.iter() for key in element.attrib)
    assert [element.tag for element in root.iter()] == [ns + "svg", ns + "path", ns + "path"]
    shield, check = list(root)
    assert shield.get("d", "").startswith("M10 1.5 C")
    assert " V" in shield.get("d", "") and shield.get("d", "").endswith(" Z")
    assert shield.get("fill") == "#0d686c"
    assert shield.get("stroke") == "#ffffff"
    assert shield.get("stroke-width") == "0.75"
    assert check.get("d", "").startswith("M6.1 10.15 L")
    assert check.get("stroke") == "#ffffff"
    assert check.get("fill") == "none"
    assert check.get("stroke-width") == "2.15"
    assert check.get("stroke-linecap") == "round"
    assert '"images/*.svg"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_legacy_light_row_badge_and_lock_precedence():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")
    if not ui_row.LIGHT_ROWS_ENABLED:
        pytest.skip("legacy light row mode is disabled")

    col_groups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in range(7)]
    row = ServerRowWidget(col_groups, lambda *_: None, lambda *_: None, lambda *_: None)
    row.bind(ServerObject(official=True, password=True))
    assert row.light_official_badge.get_visible()
    assert row.light_official_badge.get_tooltip_text() == "Official Server"
    assert row.light_lock_label.get_text() == ""

    row.bind(ServerObject(password=True))
    assert not row.light_official_badge.get_visible()
    assert row.light_official_badge.get_tooltip_text() is None
    assert row.light_lock_label.get_text() == "🔒"
    assert row.light_lock_label.get_tooltip_text() == "Password Protected"

    row.bind(ServerObject())
    assert not row.light_official_badge.get_visible()
    assert row.light_lock_label.get_text() == ""
    assert row.light_lock_label.get_tooltip_text() is None


def test_settings_runtime_sync_restores_sidebar_toggle():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")
    toggle = Gtk.ToggleButton()
    toggle.set_active(True)
    host = SimpleNamespace(
        settings={"hide_official_servers": False},
        _sidebar_settings_widgets={"hide_official_servers": toggle},
        _settings_update_guard=False,
        _on_filter_changed=lambda **_kwargs: None,
    )
    panel = SettingsUI.__new__(SettingsUI)
    panel._win = host
    panel._apply_setting_runtime_effects("hide_official_servers")
    assert toggle.get_active() is False
