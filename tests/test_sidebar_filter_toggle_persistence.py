"""Top sidebar filter toggles persist through settings.json and restore quietly."""

import json

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk

from dzll_launcher import settings, settings_ui, sidebar_ui

FILTER_KEYS = tuple(key for _attr, key in sidebar_ui.SIDEBAR_FILTER_TOGGLE_SETTINGS)


class _SidebarHost:
    def __init__(self, saved):
        self.settings = saved
        self._filter_timing = None
        self.filter_changes = []

    def _filter_refresh_is_suppressed(self):
        return False

    def _on_filter_changed(self, **kwargs):
        self.filter_changes.append(kwargs.get("reason"))

    def _on_reset_clicked(self, *_args):
        for attr, _key in sidebar_ui.SIDEBAR_FILTER_TOGGLE_SETTINGS:
            getattr(self, attr).set_active(False)

    def _apply_setting_runtime_effects(self, _key):
        pass


@pytest.fixture
def gtk_display():
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))
    return path


@pytest.fixture
def save_calls(monkeypatch):
    calls = []
    original = sidebar_ui.save_settings

    def recording_save(values):
        calls.append(dict(values))
        return original(values)

    monkeypatch.setattr(sidebar_ui, "save_settings", recording_save)
    return calls


def _build(saved_values=None):
    values = settings.load_settings()
    values.update(saved_values or {})
    host = _SidebarHost(values)
    host.frame = sidebar_ui.build_sidebar(host, include_toolbar=False)
    return host


def _states(host):
    return {key: getattr(host, attr).get_active() for attr, key in sidebar_ui.SIDEBAR_FILTER_TOGGLE_SETTINGS}


def test_toggle_mapping_covers_the_top_sidebar_toggles():
    assert sidebar_ui.SIDEBAR_FILTER_TOGGLE_SETTINGS == (
        ("cb_show_fav", "filter_show_favourites"),
        ("cb_1pp_only", "filter_1pp_only"),
        ("cb_3pp_only", "filter_3pp_only"),
        ("cb_no_password", "filter_no_password"),
        ("cb_online_only", "filter_online_only"),
        ("cb_played_only", "filter_played_only"),
    )


def test_defaults_keep_every_filter_off():
    for key in FILTER_KEYS:
        assert settings.DEFAULTS[key] is False


def test_missing_and_invalid_saved_values_use_defaults(settings_path):
    settings_path.write_text(json.dumps({"filter_online_only": "yes", "filter_no_password": 1}))
    loaded = settings.load_settings()
    assert all(loaded[key] is False for key in FILTER_KEYS)


def test_conflicting_perspective_values_keep_first_person(settings_path):
    settings_path.write_text(json.dumps({"filter_1pp_only": True, "filter_3pp_only": True}))
    loaded = settings.load_settings()
    assert loaded["filter_1pp_only"] is True
    assert loaded["filter_3pp_only"] is False


def test_new_user_starts_with_filters_off(gtk_display, settings_path, save_calls):
    host = _build()
    assert not any(_states(host).values())
    assert save_calls == []
    assert not settings_path.exists()


def test_saved_states_restore_without_refresh_or_save(gtk_display, settings_path, save_calls):
    saved = dict(settings.DEFAULTS)
    saved.update(filter_show_favourites=True, filter_3pp_only=True, filter_online_only=True, filter_played_only=True)
    settings.save_settings(saved)
    before = settings_path.read_bytes()

    host = _build()

    assert _states(host) == {
        "filter_show_favourites": True,
        "filter_1pp_only": False,
        "filter_3pp_only": True,
        "filter_no_password": False,
        "filter_online_only": True,
        "filter_played_only": True,
    }
    assert host.filter_changes == []
    assert save_calls == []
    assert settings_path.read_bytes() == before
    # Visual switch state follows the restored value.
    track = host.cb_online_only.get_child()
    assert track.has_css_class("sidebar-mini-switch-on")


def test_restore_never_shows_both_perspectives(gtk_display, settings_path):
    host = _build({"filter_1pp_only": True, "filter_3pp_only": True})
    assert host.cb_1pp_only.get_active() is True
    assert host.cb_3pp_only.get_active() is False


@pytest.mark.parametrize(("attr", "key"), sidebar_ui.SIDEBAR_FILTER_TOGGLE_SETTINGS)
def test_each_toggle_saves_on_and_off(gtk_display, settings_path, attr, key):
    host = _build()
    toggle = getattr(host, attr)

    toggle.set_active(True)
    assert host.settings[key] is True
    assert settings.load_settings()[key] is True

    toggle.set_active(False)
    assert host.settings[key] is False
    assert settings.load_settings()[key] is False
    # The existing filter behaviour still runs.
    assert len(host.filter_changes) == 2


def test_restart_round_trip(gtk_display, settings_path):
    first = _build()
    first.cb_no_password.set_active(True)
    first.cb_played_only.set_active(True)

    second = _build()
    assert second.cb_no_password.get_active() is True
    assert second.cb_played_only.get_active() is True
    assert second.cb_show_fav.get_active() is False
    assert second.filter_changes == []


def test_perspective_exclusion_is_persisted(gtk_display, settings_path):
    host = _build({"filter_3pp_only": True})
    host.cb_1pp_only.set_active(True)

    assert host.cb_3pp_only.get_active() is False
    loaded = settings.load_settings()
    assert loaded["filter_1pp_only"] is True
    assert loaded["filter_3pp_only"] is False


def test_reset_button_clears_persisted_filters(gtk_display, settings_path):
    host = _build({"filter_show_favourites": True, "filter_online_only": True})
    host._on_reset_clicked()
    loaded = settings.load_settings()
    assert all(loaded[key] is False for key in FILTER_KEYS)


def test_unchanged_state_does_not_rewrite_settings(gtk_display, settings_path, save_calls):
    host = _build({"filter_online_only": True})
    host.cb_online_only.toggled()  # emits without a state change
    assert save_calls == []


def test_other_settings_are_preserved(gtk_display, settings_path):
    saved = dict(settings.DEFAULTS)
    saved.update(hide_official_servers=True, high_ping_cutoff_ms=180, show_server_companion=True)
    settings.save_settings(saved)

    host = _build()
    host.cb_online_only.set_active(True)

    loaded = settings.load_settings()
    assert loaded["hide_official_servers"] is True
    assert loaded["high_ping_cutoff_ms"] == 180
    assert loaded["show_server_companion"] is True
    assert loaded["filter_online_only"] is True


def test_settings_reset_keeps_visible_filter_states_persisted(gtk_display, settings_path, monkeypatch):
    host = _build()
    host.cb_online_only.set_active(True)
    host._settings_widgets = {}
    host._obj_by_key = {}
    host._ping_cutoff_ms = 250
    host.live = {}
    host._apply_titlebar_counts = lambda: None
    owner = settings_ui.SettingsUI.__new__(settings_ui.SettingsUI)
    owner._win = host
    monkeypatch.setattr(settings_ui.SettingsUI, "_reset_entry_value", lambda *_a, **_k: None)

    owner._on_reset_settings_clicked()

    assert host.cb_online_only.get_active() is True
    assert host.settings["filter_online_only"] is True
    assert settings.load_settings()["filter_online_only"] is True


def test_store_helper_reports_changes(gtk_display, settings_path):
    host = _build()
    host.cb_show_fav.set_active(True)
    host.settings["filter_show_favourites"] = False
    assert sidebar_ui.store_sidebar_filter_toggle_states(host) is True
    assert host.settings["filter_show_favourites"] is True
    assert sidebar_ui.store_sidebar_filter_toggle_states(host) is False
