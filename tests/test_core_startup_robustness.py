import json
import itertools
import time
from pathlib import Path

import pytest

from dzll_launcher import app as app_module
from dzll_launcher import column_view as column_view_module
from dzll_launcher import settings
from dzll_launcher import sidebar_ui
from dzll_launcher import storage
from dzll_launcher import window as window_module

_APP_SEQUENCE = itertools.count(1)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("high_ping_cutoff_ms", "bad"),
        ("high_ping_cutoff_ms", None),
        ("high_ping_cutoff_ms", True),
        ("hide_below_max_players", 12.5),
        ("hide_below_max_players", None),
        ("last_update_check_ts", "123"),
        ("update_remind_after_ts", False),
        ("server_companion_alert_volume", -1),
        ("server_companion_alert_volume", 101),
    ],
)
def test_invalid_typed_settings_fall_back_to_defaults(tmp_path, monkeypatch, key, value):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({key: value}), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))

    loaded = settings.load_settings()

    assert loaded[key] == settings.DEFAULTS[key]
    assert type(loaded[key]) is type(settings.DEFAULTS[key])


def test_valid_typed_settings_remain_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    supplied = {
        "high_ping_cutoff_ms": 0,
        "hide_below_max_players": -50,
        "server_companion_alert_volume": 100,
        "last_update_check_ts": 10**40,
        "ingame_name": "Survivor",
        "show_server_companion": True,
    }
    path.write_text(json.dumps(supplied), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))

    loaded = settings.load_settings()

    for key, value in supplied.items():
        assert loaded[key] == value
        assert type(loaded[key]) is type(value)


def test_invalid_string_and_boolean_settings_keep_exact_default_types(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"ingame_name": 42, "show_server_companion": "false"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))

    loaded = settings.load_settings()

    assert loaded["ingame_name"] == ""
    assert loaded["show_server_companion"] is False


def test_removed_steamcmd_settings_are_tolerated_without_startup_rewrite(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    persisted = {
        "enable_steamcmd_mod_handling": False,
        "mod_download_backend": "steamcmd",
        "steamcmd_username": "legacy-user",
        "steamcmd_path": "/legacy/steamcmd",
        "verify_mod_files": True,
        "auto_update_required_mods": True,
    }
    original = json.dumps(persisted, sort_keys=True).encode()
    path.write_bytes(original)
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))

    loaded = settings.load_settings()

    assert loaded["enable_steamcmd_mod_handling"] is False
    for removed in (
        "mod_download_backend", "steamcmd_username", "steamcmd_path",
        "verify_mod_files", "auto_update_required_mods",
    ):
        assert removed not in loaded
        assert removed not in settings.DEFAULTS
    assert path.read_bytes() == original


def test_last_played_prune_write_failure_is_best_effort(tmp_path, monkeypatch):
    path = tmp_path / "last_played.json"
    recent = int(time.time())
    baseline = json.dumps({"recent": recent, "expired": 1}, sort_keys=True).encode()
    path.write_bytes(baseline)
    monkeypatch.setattr(storage, "LAST_PLAYED_PATH", str(path))
    monkeypatch.setattr(
        storage,
        "save_last_played",
        lambda _value: (_ for _ in ()).throw(OSError("injected prune failure")),
    )

    loaded = storage.load_last_played()

    assert loaded == {"recent": recent}
    assert path.read_bytes() == baseline


def test_last_played_prune_and_logging_failures_remain_best_effort(
    tmp_path, monkeypatch
):
    path = tmp_path / "last_played.json"
    recent = int(time.time())
    baseline = json.dumps({"recent": recent, "expired": 1}, sort_keys=True).encode()
    path.write_bytes(baseline)
    monkeypatch.setattr(storage, "LAST_PLAYED_PATH", str(path))
    monkeypatch.setattr(
        storage,
        "save_last_played",
        lambda _value: (_ for _ in ()).throw(OSError("injected prune failure")),
    )
    monkeypatch.setattr(
        storage.logger,
        "exception",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected logger failure")
        ),
    )

    loaded = storage.load_last_played()

    assert loaded == {"recent": recent}
    assert path.read_bytes() == baseline


def _require_gtk_display():
    from gi.repository import Gdk, Gtk

    Gtk.init()
    if Gdk.Display.get_default() is None:
        pytest.skip("GTK display unavailable")


@pytest.fixture
def isolated_window_state(tmp_path, monkeypatch):
    _require_gtk_display()
    config_dir = tmp_path / "config"
    cache_dir = tmp_path / "cache"
    config_dir.mkdir()
    cache_dir.mkdir()

    monkeypatch.setattr(settings, "SETTINGS_PATH", str(config_dir / "settings.json"))
    (config_dir / "settings.json").write_text(
        '{"discord_rich_presence":false,"auto_check_updates":false}', encoding="utf-8"
    )
    for name, filename in (
        ("FAV_PATH", "favorites.json"),
        ("LAST_PLAYED_PATH", "last_played.json"),
        ("LAST_COMPANION_SERVER_PATH", "last_companion_server.json"),
        ("COMPANION_RESTART_LEARNING_PATH", "companion_restart_learning.json"),
        ("DEAD_PATH", "dead.json"),
    ):
        monkeypatch.setattr(storage, name, str(config_dir / filename))
    monkeypatch.setattr(storage, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(
        window_module,
        "COMPANION_RESTART_LEARNING_PATH",
        str(config_dir / "companion_restart_learning.json"),
    )
    monkeypatch.setattr(
        window_module,
        "COMPANION_RESTART_LEARNING_PHASE2_PATH",
        str(config_dir / "companion_restart_learning_phase2.json"),
    )
    monkeypatch.setattr(window_module, "ensure_user_desktop_integration", lambda **_kw: None)
    monkeypatch.setattr(window_module, "PERF_LOG_ENABLED", False)
    # These tests exercise window construction and cleanup, not the network
    # startup updater. Prevent its idle callback from starting an executor job.
    monkeypatch.setattr(window_module.DZLLWindow, "_begin_startup_update", lambda self: False)
    return {"config": config_dir, "cache": cache_dir}


def _close_window(app, window):
    try:
        window._shutdown_cleanup()
    finally:
        window.destroy()
        app.window = None
        app.quit()


def _new_app():
    original_app_id = app_module.APP_ID
    app_module.APP_ID = f"{original_app_id}.Core002Test{next(_APP_SEQUENCE)}"
    try:
        app = app_module.DZLLApp()
    finally:
        app_module.APP_ID = original_app_id
    app.register(None)
    return app


def _drain_main_context(limit=100):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(limit):
        if not context.pending():
            break
        context.iteration(False)


def test_real_window_constructs_from_fresh_isolated_state(isolated_window_state):
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        assert window.settings["high_ping_cutoff_ms"] == 250
        assert not window._shutdown_cleanup_done
    finally:
        _close_window(app, window)


def test_real_window_wires_preparation_overlay_renderer_to_its_widgets_and_download_state(
    isolated_window_state,
):
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        ui = window._join_preparation_overlay_ui
        for name in ("scrim", "card", "heading", "spinner", "detail_label",
                     "status_label", "progress_bar", "cancel_button"):
            assert getattr(ui, f"join_preparation_{name}") is getattr(window, f"join_preparation_{name}")
        assert (ui.progress_timer_id, ui.percent_label) == (0, None)
        for name in ("_steam_ugc_progress_timer_id", "_steam_ugc_percent_label"):
            assert not hasattr(window, name)

        window._mod_download_backend_active = "steam_client"
        window._steam_ugc_worker_in_progress = True
        assert ui._pulse_active() is True
        window._steam_ugc_worker_in_progress = False
        assert ui._pulse_active() is False
        window._steam_ugc_worker_in_progress = True
        window._mod_download_backend_active = "join"
        assert ui._pulse_active() is False
        window._steam_ugc_active_event = {"id": 7, "total_bytes": 9}
        assert ui._active_download() is window._steam_ugc_active_event
        window._mod_download_backend_active = ""
        window._steam_ugc_worker_in_progress = False
        window._steam_ugc_active_event = None
    finally:
        _close_window(app, window)


def test_real_window_wires_background_prepare_status_strip_to_its_widgets_and_actions(
    isolated_window_state,
):
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        ui = window._background_prepare_status_ui
        assert ui.win is window
        assert ui.background_prepare_status is window.background_prepare_status
        for name in ("grid", "info", "server_label", "detail_label", "queue_divider",
                     "queue_label", "failed_label", "retry_btn", "count_label",
                     "percent_label", "right_status_label", "action_btn"):
            assert getattr(ui, f"background_prepare_{name}") is getattr(
                window, f"background_prepare_{name}"
            )
        assert not window.background_prepare_status.get_visible()
        assert not hasattr(window_module, "BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET")
        assert not hasattr(window_module.DZLLWindow, "_build_background_prepare_status_block")

        # The idle strip's action button is wired to the window's real handler:
        # with nothing queued it closes the strip, advancing the UI generation.
        generation = window._background_prepare_ui_generation
        window.background_prepare_action_btn.emit("clicked")
        assert window._background_prepare_ui_generation == generation + 1
        assert not window.background_prepare_status.get_visible()

        # Retry with no completed batch is a no-op through the real handler.
        window.background_prepare_retry_btn.emit("clicked")
        assert window._background_prepare_ui_generation == generation + 1
    finally:
        _close_window(app, window)


def test_real_window_shutdown_cancels_search_suggestion_and_pulse_timers(
    isolated_window_state,
):
    from gi.repository import GLib

    app = _new_app()
    window = window_module.DZLLWindow(app)
    removed = []
    try:
        ui = window._join_preparation_overlay_ui
        popover = window._mod_suggestion_popover
        window._mod_download_backend_active = "steam_client"
        window._steam_ugc_worker_in_progress = True
        ui.start_progress_timer()
        popover.queue_refresh()
        window._on_search_changed_debounced()
        armed = (ui.progress_timer_id, popover.refresh_source_id, window._search_filter_debounce_id)
        assert all(armed)
        assert ui._pulse_active() is True

        real_remove = GLib.source_remove

        def recording_remove(source_id):
            removed.append(int(source_id))
            return real_remove(source_id)

        GLib.source_remove = recording_remove
        try:
            window._shutdown_cleanup()
        finally:
            GLib.source_remove = real_remove

        assert set(armed) <= set(removed)
        assert (ui.progress_timer_id, popover.refresh_source_id, window._search_filter_debounce_id) == (0, 0, 0)
        # The Join flags are untouched, but the pulse may no longer stay active.
        assert window._steam_ugc_worker_in_progress is True
        assert ui._pulse_active() is False
        assert ui._progress_pulse() is False
    finally:
        window._mod_download_backend_active = ""
        window._steam_ugc_worker_in_progress = False
        _close_window(app, window)


def test_a2s_warmup_opt_in_perf_activation_constructs_and_presents_real_window(
    isolated_window_state, monkeypatch,
):
    phases = []
    monkeypatch.setenv("DZLL_PERF_LOG", "1")
    monkeypatch.setattr(window_module, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(window_module, "A2S_WARMUP_500_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "log_startup_phase",
        lambda _origin, phase, _start, **_fields: phases.append(phase),
    )
    app = _new_app()

    app.do_activate()

    window = app.window
    try:
        assert window is not None
        assert window.get_visible()
        assert window._startup_warmup_mode == "default"
        assert phases[:3] == ["activation", "window_construction", "window_present"]
    finally:
        if window is not None:
            _close_window(app, window)


def test_a2s_warmup_opt_in_perf_activation_reports_constructor_failure(
    isolated_window_state, monkeypatch,
):
    phases = []
    monkeypatch.setenv("DZLL_PERF_LOG", "1")
    monkeypatch.setattr(window_module, "PERF_LOG_ENABLED", True)
    monkeypatch.setattr(window_module, "A2S_WARMUP_500_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "log_startup_phase",
        lambda _origin, phase, _start, **_fields: phases.append(phase),
    )
    monkeypatch.setattr(
        window_module,
        "build_sidebar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected constructor failure")
        ),
    )
    app = _new_app()

    app.do_activate()

    fatal = app._startup_failure_window
    assert app.window is None
    assert app._startup_failed
    assert fatal is not None
    assert fatal.get_visible()
    assert phases[:2] == ["activation", "window_construction_failed"]
    app._dismiss_startup_failure(fatal)


def test_official_sidebar_toggle_restores_saves_and_refreshes_real_window(
    isolated_window_state, monkeypatch,
):
    path = isolated_window_state["config"] / "settings.json"
    path.write_text(
        '{"discord_rich_presence":false,"hide_official_servers":true}',
        encoding="utf-8",
    )
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        toggle = window._sidebar_settings_widgets["hide_official_servers"]
        assert toggle.get_active()
        assert window._build_filter_state()["hide_official_servers"] is True
        refreshes = []
        monkeypatch.setattr(window, "_on_filter_changed", lambda **kw: refreshes.append(kw))
        toggle.set_active(False)
        assert window.settings["hide_official_servers"] is False
        assert settings.load_settings()["hide_official_servers"] is False
        assert window._build_filter_state()["hide_official_servers"] is False
        assert refreshes == [{"reason": "settings"}]
    finally:
        _close_window(app, window)


def test_real_window_companion_undock_close_redock_and_shutdown(isolated_window_state):
    from gi.repository import Gtk

    path = isolated_window_state["config"] / "settings.json"
    path.write_text(
        '{"discord_rich_presence":false,"auto_check_updates":false,"show_server_companion":true}',
        encoding="utf-8",
    )
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        dock = window._server_companion_dock
        panel = window.server_companion_panel
        revealer = window.server_companion_revealer
        assert dock.docked is True
        assert revealer.get_child() is panel

        dock.undock()
        undocked = dock.undocked_window
        assert dock.docked is False
        assert isinstance(undocked, Gtk.Window)
        assert undocked.get_child() is panel
        assert revealer.get_child() is None
        assert dock.shrink_source_id
        toggle = window.server_companion_toggle_btn
        dot = window.server_companion_toggle_dot
        assert not hasattr(window, "server_companion_show_btn")
        assert toggle.get_tooltip_text() == "Turn off Server Companion"
        assert dot.get_visible()

        undocked.close()  # real close-request: redocks and keeps the window alive
        assert dock.docked is True
        assert dock.undocked_window is None
        assert dock.shrink_source_id == 0
        assert revealer.get_child() is panel
        assert revealer.get_reveal_child() is True
        assert toggle.get_tooltip_text() == "Turn off Server Companion"

        dock.undock()
        toggle.emit("clicked")  # turning off while undocked reattaches the panel
        assert window.settings["show_server_companion"] is False
        assert dock.docked is True
        assert dock.undocked_window is None
        assert revealer.get_child() is panel
        assert toggle.get_tooltip_text() == "Turn on Server Companion"
        assert not dot.get_visible()

        toggle.emit("clicked")
        assert window.settings["show_server_companion"] is True
        assert dock.docked is True
        assert revealer.get_reveal_child() is True
        assert toggle.get_tooltip_text() == "Turn off Server Companion"
        assert dot.get_visible()

        dock.undock()
        assert dock.docked is False
        window._shutdown_cleanup()
        assert dock.docked is True
        assert dock.undocked_window is None
    finally:
        _close_window(app, window)


def test_real_window_accepts_invalid_persisted_numeric_setting(
    isolated_window_state,
):
    path = isolated_window_state["config"] / "settings.json"
    path.write_text(
        '{"discord_rich_presence":false,"high_ping_cutoff_ms":"bad"}',
        encoding="utf-8",
    )
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        assert window._ping_cutoff_ms == settings.DEFAULTS["high_ping_cutoff_ms"]
    finally:
        _close_window(app, window)


def test_real_window_survives_last_played_prune_write_failure(
    isolated_window_state, monkeypatch
):
    path = isolated_window_state["config"] / "last_played.json"
    path.write_text('{"expired":1}', encoding="utf-8")
    baseline = path.read_bytes()
    monkeypatch.setattr(
        storage,
        "save_last_played",
        lambda _value: (_ for _ in ()).throw(OSError("injected prune failure")),
    )
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        assert window.last_played == {}
        assert path.read_bytes() == baseline
    finally:
        _close_window(app, window)


def test_real_window_survives_corrupt_optional_sidebar_logo(
    isolated_window_state, monkeypatch
):
    images = isolated_window_state["config"] / "images"
    images.mkdir()
    (images / "dzll-new-logo.png").write_bytes(b"not-a-png")
    monkeypatch.setattr(sidebar_ui, "IMAGES_DIR", str(images))
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        assert window.get_child() is not None
    finally:
        _close_window(app, window)


def test_activation_cleans_real_partial_window_and_presents_fatal_error(
    isolated_window_state, monkeypatch
):
    captured = []

    def fail_late(host, *_args, **_kwargs):
        captured.append(host)
        raise RuntimeError("injected unrecoverable startup failure")

    monkeypatch.setattr(window_module, "build_sidebar", fail_late)
    app = _new_app()

    app.do_activate()

    assert app.window is None
    assert app._startup_failed
    assert len(captured) == 1
    partial = captured[0]
    assert partial._shutdown_cleanup_done
    assert partial._perf_diagnostic_source_ids == []
    assert partial._steam_global_players_startup_source_id == 0
    assert partial._steam_global_players_poll_source_id == 0
    assert partial._settings_init_idle_id == 0
    backend = partial._companion_restart_phase2._authoritative_schema4_backend
    if backend is not None:
        assert backend._closed
        assert backend._writer_lock._handle is None
    for name in (
        "_executor",
        "_db_executor",
        "_update_executor",
        "_hi_executor",
        "_browser_live_executor",
        "_startup_live_executor",
    ):
        assert getattr(partial, name)._shutdown
    fatal = app._startup_failure_window
    assert fatal is not None
    assert fatal in app.get_windows()
    assert partial not in app.get_windows()

    app.do_activate()
    assert app.window is None
    assert app._startup_failure_window is fatal

    from gi.repository import GLib

    GLib.idle_add(lambda: (app._dismiss_startup_failure(fatal), False)[1])
    assert app.run(["dzll-core002-test"]) == 0
    assert app._startup_failure_window is None
    assert partial not in app.get_windows()


def test_startup_logger_failure_cannot_prevent_partial_cleanup_or_fatal_handling(
    isolated_window_state, monkeypatch
):
    captured = []

    def fail_late(host, *_args, **_kwargs):
        captured.append(host)
        raise RuntimeError("original startup failure")

    monkeypatch.setattr(window_module, "build_sidebar", fail_late)
    monkeypatch.setattr(
        app_module.logger,
        "error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected logger failure")
        ),
    )
    app = _new_app()

    app.do_activate()

    assert app.window is None
    assert app._startup_failed
    assert len(captured) == 1
    partial = captured[0]
    assert partial._shutdown_cleanup_done
    assert partial not in app.get_windows()
    assert app._startup_failure_window in app.get_windows()
    app._dismiss_startup_failure(app._startup_failure_window)


def test_partial_cleanup_cancels_column_view_construction_idles(
    isolated_window_state, monkeypatch
):
    _drain_main_context()
    calls = []
    original_normalize = column_view_module._normalize_column_view_header

    def record_normalize(view):
        calls.append(view)
        return original_normalize(view)

    def fail_after_column_view(host, *_args, **_kwargs):
        assert getattr(host, "list_view", None) is not None
        raise RuntimeError("injected failure after ColumnView construction")

    monkeypatch.setattr(
        column_view_module, "_normalize_column_view_header", record_normalize
    )
    monkeypatch.setattr(window_module, "build_sidebar", fail_after_column_view)
    app = _new_app()

    app.do_activate()
    _drain_main_context()

    assert calls == []
    assert app.window is None
    fatal = app._startup_failure_window
    assert fatal is not None
    app._dismiss_startup_failure(fatal)


def test_successful_window_runs_column_view_construction_idles(
    isolated_window_state, monkeypatch
):
    _drain_main_context()
    calls = []
    original_normalize = column_view_module._normalize_column_view_header

    def record_normalize(view):
        calls.append(view)
        return original_normalize(view)

    monkeypatch.setattr(
        column_view_module, "_normalize_column_view_header", record_normalize
    )
    app = _new_app()
    window = window_module.DZLLWindow(app)
    try:
        window.present()
        _drain_main_context()
        assert calls
    finally:
        _close_window(app, window)


@pytest.mark.parametrize("dismiss_method", ["destroy", "close"])
def test_fatal_window_destruction_clears_reference_and_cannot_restart_window(
    isolated_window_state, monkeypatch, dismiss_method
):
    captured = []

    def fail_late(host, *_args, **_kwargs):
        captured.append(host)
        raise RuntimeError("injected unrecoverable startup failure")

    monkeypatch.setattr(window_module, "build_sidebar", fail_late)
    app = _new_app()
    app.do_activate()
    fatal = app._startup_failure_window
    assert fatal is not None

    getattr(fatal, dismiss_method)()
    _drain_main_context()

    assert app._startup_failure_window is None
    assert fatal not in app.get_windows()
    assert app.window is None
    app.do_activate()
    assert len(captured) == 1
    assert app.window is None
    assert app._startup_failure_window is None


def test_direct_fatal_window_destroy_allows_application_run_to_return(
    isolated_window_state, monkeypatch
):
    def fail_late(*_args, **_kwargs):
        raise RuntimeError("injected unrecoverable startup failure")

    monkeypatch.setattr(window_module, "build_sidebar", fail_late)
    app = _new_app()
    app.do_activate()
    fatal = app._startup_failure_window
    assert fatal is not None

    from gi.repository import GLib

    GLib.idle_add(lambda: (fatal.destroy(), False)[1])
    assert app.run(["dzll-core002-direct-fatal-destroy-test"]) == 0
    assert app._startup_failure_window is None
    assert fatal not in app.get_windows()
    assert app.window is None


def test_application_quit_destroys_registered_fatal_window(
    isolated_window_state, monkeypatch
):
    captured = []

    def fail_late(host, *_args, **_kwargs):
        captured.append(host)
        raise RuntimeError("injected unrecoverable startup failure")

    monkeypatch.setattr(window_module, "build_sidebar", fail_late)
    app = _new_app()
    app.do_activate()
    fatal = app._startup_failure_window
    assert fatal is not None
    assert fatal in app.get_windows()
    assert fatal.get_visible()

    from gi.repository import GLib

    GLib.idle_add(lambda: (app.quit(), False)[1])
    assert app.run(["dzll-core002-fatal-app-quit-test"]) == 0

    assert app._startup_failure_window is None
    assert app.get_windows() == []
    assert not fatal.get_visible()
    assert fatal not in app.get_windows()
    assert app.window is None
    assert len(captured) == 1
