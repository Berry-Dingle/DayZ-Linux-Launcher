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
        '{"discord_rich_presence":false}', encoding="utf-8"
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
