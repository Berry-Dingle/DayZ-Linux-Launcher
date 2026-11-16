"""Sidebar version label: text, single version source, packaging consistency."""

import re
from pathlib import Path

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

from dzll_launcher import config, settings, sidebar_ui

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "dzll_launcher"


class _SidebarHost:
    """The window surface build_sidebar touches, without a real DZLLWindow."""

    def __init__(self, saved=None):
        self.settings = dict(settings.DEFAULTS)
        self.settings.update(saved or {})
        self._filter_timing = None
        self.filter_changes = []

    def _filter_refresh_is_suppressed(self):
        return False

    def _on_filter_changed(self, **kwargs):
        self.filter_changes.append(kwargs.get("reason"))

    def _on_reset_clicked(self, *_args):
        pass

    def _apply_setting_runtime_effects(self, _key):
        pass


@pytest.fixture
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


def _version_parts(app_version):
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)(?:-([a-z]+))?", app_version)
    assert match, f"unexpected APP_VERSION shape: {app_version!r}"
    return match.group(1), match.group(2)


@pytest.mark.parametrize(
    ("app_version", "expected"),
    [
        ("v0.4.2-beta", "DZLL - v0.4.2 Beta"),
        ("v0.4.3-beta", "DZLL - v0.4.3 Beta"),
        ("v0.5.0-rc-2", "DZLL - v0.5.0 Rc 2"),
        ("v1.0.0", "DZLL - v1.0.0"),
        ("1.2.3-beta", "DZLL - v1.2.3 Beta"),
        ("  v0.4.2-beta  ", "DZLL - v0.4.2 Beta"),
        ("", "DZLL"),
    ],
)
def test_format_sidebar_version_text(app_version, expected):
    assert sidebar_ui.format_sidebar_version_text(app_version) == expected


def test_default_label_reads_config_app_version():
    assert sidebar_ui.APP_VERSION is config.APP_VERSION
    assert sidebar_ui.format_sidebar_version_text() == (
        sidebar_ui.format_sidebar_version_text(config.APP_VERSION)
    )


def test_current_release_label():
    assert config.APP_VERSION == "v0.4.2-beta"
    assert sidebar_ui.format_sidebar_version_text() == "DZLL - v0.4.2 Beta"


def test_label_follows_a_version_bump(monkeypatch):
    monkeypatch.setattr(sidebar_ui, "APP_VERSION", "v0.4.3-beta")
    assert sidebar_ui.format_sidebar_version_text() == "DZLL - v0.4.3 Beta"


def test_no_independent_version_literal_in_sidebar():
    source = (SRC / "sidebar_ui.py").read_text(encoding="utf-8")
    assert not re.search(r"\d+\.\d+\.\d+", source)


def test_settings_about_uses_the_same_version_source():
    source = (SRC / "settings_ui.py").read_text(encoding="utf-8")
    assert "from .config import IMAGES_DIR, APP_VERSION, RELEASES_URL" in source
    assert "Version: {APP_VERSION}" in source


def test_package_versions_match_app_version():
    release, stage = _version_parts(config.APP_VERSION)
    py_stage = {"beta": "b0", "alpha": "a0", "rc": "rc0", None: ""}[stage]

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf'^version = "{re.escape(release + py_stage)}"$', pyproject, re.M)

    changelog_head = (ROOT / "debian" / "changelog").read_text(encoding="utf-8").splitlines()[0]
    deb_upstream = release + (f"~{py_stage}" if py_stage else "")
    assert changelog_head.startswith(f"dzll-launcher ({deb_upstream}-")

    spec = (ROOT / "dzll_launcher.spec").read_text(encoding="utf-8")
    assert re.search(rf"^Version:\s+{re.escape(release)}$", spec, re.M)
    if stage:
        assert re.search(rf"^Release:\s+0\.{stage}%\{{\?dist\}}$", spec, re.M)
    assert f"-{release}{py_stage}.tar.gz" in spec

    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD.template").read_text(encoding="utf-8")
    assert re.search(rf"^pkgver={re.escape(release)}$", pkgbuild, re.M)


def test_version_label_is_centred_beneath_disclaimer(gtk_display):
    host = _SidebarHost()
    frame = sidebar_ui.build_sidebar(host, include_toolbar=False)

    label = host.sidebar_version_label
    assert label.get_text() == "DZLL - v0.4.2 Beta"
    assert label.get_halign() == Gtk.Align.CENTER
    assert label.get_xalign() == pytest.approx(0.5)
    assert label.has_css_class("sidebar-version")

    gap = label.get_prev_sibling()
    assert gap.get_size_request()[1] == sidebar_ui.SIDEBAR_DISCLAIMER_VERSION_SPACING
    disclaimer = gap.get_prev_sibling()
    assert disclaimer.has_css_class("disclaimer")
    assert disclaimer.get_text() == config.DISCLAIMER_TEXT
    assert label.get_next_sibling() is None
    assert label.get_root() is None and frame is not None


def test_version_label_style_uses_dzll_tokens():
    from dzll_launcher.styles import get_app_css

    css = get_app_css("#000", 220, "#999").decode("utf-8")
    block = css[css.index(".dzll-app-root .sidebar-version,"):]
    block = block[: block.index("}")]
    assert "color: @dzll_text_disclaimer;" in block
    assert "font-size: 0.82em;" in block
