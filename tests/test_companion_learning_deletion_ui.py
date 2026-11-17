"""Delete Learned Data: Settings button, SC bin, confirmation and restart."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import companion_learning_deletion as deletion
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_learning_transfer_ui as ui
from dzll_launcher import server_companion_ui
from dzll_launcher.server_companion_ui import ServerCompanionPanel
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
KEY = "203.0.113.7:2302"


class ImmediateExecutor:
    def submit(self, callback):
        future = Future()
        try:
            future.set_result(callback())
        except BaseException as exc:
            future.set_exception(exc)
        return future


class Button:
    def __init__(self):
        self.sensitive = True
        self.tooltip = None

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = value


class App:
    def __init__(self):
        self.restarts = 0

    def request_restart(self):
        self.restarts += 1


def make_controller(tmp_path, monkeypatch, *, server=object(), runtime=None):
    monkeypatch.setattr(ui, "CFG_DIR", str(tmp_path))
    app = App()
    log = []
    win = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _hi_executor=ImmediateExecutor(),
        _server_companion_obj=server,
        _companion_restart_phase2=runtime
        or SimpleNamespace(loading=False, has_learning_for=lambda _key: True),
        get_application=lambda: app,
    )
    win.clear_server_companion = lambda: log.append("clear")
    value = ui.CompanionLearningTransferController(win)
    shown = []
    value._show_feedback = lambda title, body, **kwargs: shown.append((title, body, kwargs))
    value._hide_feedback = lambda *_args: log.append("hide")
    return value, win, app, shown, log


def action(shown, label):
    return {item[0]: item for item in shown[-1][2]["actions"]}[label]


def test_delete_all_confirmation_text_and_actions(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    value.choose_delete_all()
    title, body, options = shown[-1]
    assert title == "Delete all Server Companion learning data?"
    assert "every server will be permanently deleted" in body
    assert "This cannot be undone" in body
    assert "does not keep a backup" in body
    assert "Exporting your learning data first is strongly recommended" in body
    assert "DZLL will restart" in body
    assert "without a monitored server" in body
    assert [item[0] for item in options["actions"]] == ["Cancel", "Export Data", "Delete and Restart"]
    assert action(shown, "Delete and Restart")[2] == (ui.DANGER_ACTION_CSS,)
    assert options["error"] is True
    assert not deletion.pending_deletion_exists(tmp_path)


def test_accepting_delete_all_stages_clears_monitoring_and_restarts(tmp_path, monkeypatch):
    value, _win, app, shown, log = make_controller(tmp_path, monkeypatch)
    value.choose_delete_all()
    action(shown, "Delete and Restart")[1]()
    staged = deletion.read_pending_deletion(tmp_path)
    assert staged.scope == "all"
    # D5/C2: the monitored server is cleared before the forced restart.
    assert log == ["hide", "clear"]
    assert app.restarts == 1
    assert not value._busy


def test_export_action_routes_to_the_existing_export(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    exports = []
    value.choose_export = lambda *_args: exports.append(True)
    value.choose_delete_all()
    action(shown, "Export Data")[1]()
    assert exports == [True]
    assert not deletion.pending_deletion_exists(tmp_path)


def test_server_deletion_names_server_and_stages_the_captured_key(tmp_path, monkeypatch):
    value, win, app, shown, log = make_controller(tmp_path, monkeypatch)
    value.choose_delete_server(KEY, "Example DayZ Server")
    title, body, _options = shown[-1]
    assert title == "Delete learning data for this server?"
    assert "Server: Example DayZ Server" in body
    assert "Only the restart-learning data for this monitored server" in body
    assert "Learning data for all other servers is kept" in body
    assert "This cannot be undone" in body
    # The user switches server before accepting: the captured key is used.
    win._server_companion_obj = object()
    action(shown, "Delete and Restart")[1]()
    staged = deletion.read_pending_deletion(tmp_path)
    assert staged.scope == "servers" and staged.server_keys == (KEY,)
    assert log == ["hide", "clear"] and app.restarts == 1


def test_staging_failure_shows_error_without_clearing_or_restarting(tmp_path, monkeypatch):
    value, _win, app, shown, log = make_controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ui,
        "stage_pending_deletion",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("read-only")),
    )
    value.choose_delete_all()
    action(shown, "Delete and Restart")[1]()
    assert shown[-1][0] == "Learning data deletion failed"
    assert "read-only" in shown[-1][1]
    assert "clear" not in log and app.restarts == 0
    assert not value._busy


def test_no_monitored_server_skips_clear(tmp_path, monkeypatch):
    value, _win, app, shown, log = make_controller(tmp_path, monkeypatch, server=None)
    value.choose_delete_all()
    action(shown, "Delete and Restart")[1]()
    assert "clear" not in log and app.restarts == 1


def test_pending_import_blocks_deletion_everywhere(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    payload, _metadata = transfer.pending_import_paths(tmp_path)
    payload.write_bytes(b"{}")
    value.choose_delete_all()
    assert shown[-1][0] == "Deletion unavailable"
    value.choose_delete_server(KEY, "x")
    assert shown[-1][0] == "Deletion unavailable"
    assert value.server_deletion_block_reason(KEY) == ui.DELETE_BLOCKED_BY_IMPORT_TOOLTIP
    button = Button()
    value._delete_all_button = button
    value._refresh_sensitivity()
    assert not button.sensitive and button.tooltip == ui.DELETE_BLOCKED_BY_IMPORT_TOOLTIP
    payload.unlink()
    value._refresh_sensitivity()
    assert button.sensitive and button.tooltip == ui.DELETE_ALL_TOOLTIP


def test_pending_deletion_blocks_import(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    deletion.stage_pending_deletion(config_dir=tmp_path, scope="all")
    messages = []
    value._show_message = lambda title, body, **kwargs: messages.append((title, body))
    value.choose_import()
    assert messages[-1][0] == "Import unavailable"
    assert not value._busy


def test_block_reasons(tmp_path, monkeypatch):
    loading = SimpleNamespace(loading=True)
    value, _win, _app, _shown, _log = make_controller(tmp_path, monkeypatch, runtime=loading)
    assert value.server_deletion_block_reason(KEY) == ui.DELETE_LOADING_TOOLTIP
    empty = SimpleNamespace(loading=False, has_learning_for=lambda _key: False)
    value, *_ = make_controller(tmp_path, monkeypatch, runtime=empty)
    assert value.server_deletion_block_reason(KEY) == ui.DELETE_NO_LEARNING_TOOLTIP
    learned = SimpleNamespace(loading=False, has_learning_for=lambda _key: True)
    value, *_ = make_controller(tmp_path, monkeypatch, runtime=learned)
    assert value.server_deletion_block_reason(KEY) is None


def test_busy_controller_ignores_delete_requests(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    value._busy = True
    value.choose_delete_all()
    value.choose_delete_server(KEY, "x")
    assert shown == []


def test_export_and_import_messages_mention_quarantined_records(tmp_path, monkeypatch):
    value, _win, _app, shown, _log = make_controller(tmp_path, monkeypatch)
    validated = transfer.ValidatedLearningImport(b"{}", "0" * 64, 4, 13, "x.json", 1)
    value._show_import_warning(validated)
    assert "1 quarantined record is included" in shown[-1][1]


# --------------------------------------------------------------------------
# Window hooks (host tests: no real window)
# --------------------------------------------------------------------------


def test_window_bin_click_captures_key_and_name():
    calls = []
    host = SimpleNamespace(
        _server_companion_restart_learning_key=lambda: KEY,
        _server_companion_snapshot={"name": "Example DayZ Server"},
        companion_learning_transfer=SimpleNamespace(
            choose_delete_server=lambda key, name: calls.append((key, name))
        ),
    )
    DZLLWindow._on_server_companion_delete_learning_clicked(host)
    assert calls == [(KEY, "Example DayZ Server")]
    host._server_companion_restart_learning_key = lambda: None
    DZLLWindow._on_server_companion_delete_learning_clicked(host)
    assert len(calls) == 1


def test_window_refreshes_bin_state_from_controller_reason():
    states = []
    host = SimpleNamespace(
        server_companion_panel=SimpleNamespace(
            set_delete_learning_state=lambda sensitive, tooltip: states.append((sensitive, tooltip))
        ),
        companion_learning_transfer=SimpleNamespace(
            server_deletion_block_reason=lambda key: None if key == KEY else "no"
        ),
        _server_companion_restart_learning_key=lambda: KEY,
    )
    DZLLWindow._refresh_server_companion_delete_learning_state(host)
    DZLLWindow._refresh_server_companion_delete_learning_state(host, "other:1")
    assert states == [(True, None), (False, "no")]
    # Hosts without the controller or an old panel are tolerated.
    DZLLWindow._refresh_server_companion_delete_learning_state(SimpleNamespace())


def test_window_startup_notice_reports_deletion():
    host = SimpleNamespace(
        _companion_import_startup_result=None,
        _companion_deletion_startup_result=deletion.StartupDeletionResult(
            "applied", scope="servers", server_keys=(KEY,)
        ),
    )
    notice = DZLLWindow._server_companion_import_startup_notice(host)
    assert notice.title == "Server Companion learning data deleted"
    assert notice.kind == "learning_data_deleted"
    host._companion_deletion_startup_result = deletion.StartupDeletionResult("failed", error="x")
    assert DZLLWindow._server_companion_import_startup_notice(host).kind == "initialization_failed"
    host._companion_deletion_startup_result = deletion.StartupDeletionResult("none")
    assert DZLLWindow._server_companion_import_startup_notice(host) is None


def test_source_contracts():
    window = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    init = window.split("class DZLLWindow", 1)[1].split("self._pending_last_played_obj", 1)[0]
    runtime_init = init.index("_initialize_companion_restart_runtime_for_window(")
    assert init.index("pending_learning_changes_conflict(") < runtime_init
    assert init.index("apply_pending_import_at_startup(") < runtime_init
    assert init.index("apply_pending_deletion_at_startup(") < runtime_init
    assert "set_on_delete_learning(" in window
    settings = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    assert 'Gtk.Button(label="Delete All Learned Data")' in settings
    assert "delete_all_button=delete_learning_btn" in settings
    styles = (ROOT / "src/dzll_launcher/styles.py").read_text(encoding="utf-8")
    assert ".dzll-app-root button.server-companion-delete-learning-button image" in styles
    assert ".dzll-app-root button.companion-learning-danger-action" in styles


# --------------------------------------------------------------------------
# Real GTK panel
# --------------------------------------------------------------------------


def _gtk_available():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        return bool(Gtk.init_check())
    except Exception:
        return False


@pytest.mark.skipif(not _gtk_available(), reason="GTK 4 display unavailable")
def test_real_panel_bin_is_docked_only_and_follows_the_shown_server():
    panel = ServerCompanionPanel()
    clicks = []
    panel.set_on_delete_learning(lambda: clicks.append(True))
    button = panel.delete_learning_btn
    assert button.has_css_class("server-companion-delete-learning-button")
    assert not panel.delete_learning_visible()
    panel.set_server_snapshot({"name": "Example", "online": True})
    assert panel.delete_learning_visible()
    panel.set_docked(False)
    assert not panel.delete_learning_visible()
    button.emit("clicked")
    assert clicks == []
    panel.set_docked(True)
    assert panel.delete_learning_visible()
    button.emit("clicked")
    assert clicks == [True]
    panel.set_delete_learning_state(False, "No learning data has been saved for this server.")
    assert not button.get_sensitive()
    assert button.get_tooltip_text() == "No learning data has been saved for this server."
    panel.set_delete_learning_state(True)
    assert button.get_sensitive()
    assert button.get_tooltip_text() == server_companion_ui.DELETE_LEARNING_TOOLTIP
    panel.clear_server()
    assert not panel.delete_learning_visible()
    # The bin sits in the same row as the server name.
    assert button.get_parent() is panel.name_label.get_parent()


# --------------------------------------------------------------------------
# Stale pending indicator after a startup-applied import (R7 regression)
# --------------------------------------------------------------------------


class Widget(Button):
    def __init__(self):
        super().__init__()
        self.visible = False
        self.text = ""

    def set_visible(self, value):
        self.visible = bool(value)

    def set_text(self, value):
        self.text = str(value)


def test_runtime_install_refreshes_a_pending_line_made_stale_by_the_startup_apply(
    tmp_path, monkeypatch
):
    import threading

    from dzll_launcher import companion_restart_phase2_runtime as runtime

    monkeypatch.setattr(ui, "CFG_DIR", str(tmp_path))
    validated = transfer.validate_external_learning_bytes(
        deletion.fresh_schema4_bytes(now=1_900_000_000), source_filename="x.json"
    )
    pending = transfer.stage_pending_import(validated, config_dir=tmp_path)

    placeholder = runtime.Phase2RestartRuntime.loading_placeholder(
        active_path=str(tmp_path / "x.json")
    )
    host = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _hi_executor=ImmediateExecutor(),
        _companion_restart_phase2=placeholder,
        _server_companion_obj=None,
    )
    controller = ui.CompanionLearningTransferController(host)
    container, delete_all = Widget(), Widget()
    # Settings is built while the staged import still exists.
    controller.bind_settings_widgets(
        export_button=Widget(),
        import_button=Widget(),
        cancel_button=Widget(),
        pending_container=container,
        status_label=Widget(),
        delete_all_button=delete_all,
    )
    assert container.visible and not delete_all.sensitive

    # The background loader applies the import and removes the staged files.
    pending.payload_path.unlink()
    pending.metadata_path.unlink()
    loaded = SimpleNamespace(
        pending_notice=None,
        set_persistence_status_listener=lambda listener: None,
        enable_background_persistence=lambda post_to_owner: True,
    )
    host.__dict__.update(
        companion_learning_transfer=controller,
        _startup_presentation=None,
        _server_companion_poll_timer_id=0,
        _server_companion_should_poll=lambda: False,
        _server_companion_restart_learning_key=lambda: None,
        _record_server_companion_monitor_started=lambda key=None: None,
        _refresh_server_companion_restart_learning_summary=lambda: None,
        _server_companion_import_startup_notice=lambda: None,
        _debug_server_companion_alert=lambda message: None,
        _companion_learning_load_lock=threading.Lock(),
        _companion_learning_load_result=(None, loaded),
        _companion_learning_load_installed=False,
    )
    DZLLWindow._install_companion_restart_learning(host)

    assert not container.visible
    assert delete_all.sensitive and delete_all.tooltip == ui.DELETE_ALL_TOOLTIP


def test_delete_all_button_is_on_its_own_row_under_export_and_import():
    source = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    general = source.split("    def _settings_page_general", 1)[1].split(
        "    def _settings_page_launch", 1
    )[0]
    # Export and Import share row 0 with equal widths; Delete All spans both.
    assert "learning_actions = Gtk.Grid()" in general
    assert "learning_actions.set_column_homogeneous(True)" in general
    assert "learning_actions.attach(export_learning_btn, 0, 0, 1, 1)" in general
    assert "learning_actions.attach(import_learning_btn, 1, 0, 1, 1)" in general
    assert "learning_actions.attach(delete_learning_btn, 0, 1, 2, 1)" in general
    grid = general.index("learning_box.append(learning_actions)")
    pending = general.index("learning_box.append(pending_learning_box)")
    assert grid < pending
    assert "learning_box.append(delete_learning_btn)" not in general


@pytest.mark.skipif(not _gtk_available(), reason="GTK 4 display unavailable")
def test_real_delete_all_button_spans_export_and_import_exactly():
    from gi.repository import Gtk

    grid = Gtk.Grid()
    grid.set_column_spacing(8)
    grid.set_column_homogeneous(True)
    buttons = [Gtk.Button(label=text) for text in ("Export Data", "Import Data", "Delete All Learned Data")]
    grid.attach(buttons[0], 0, 0, 1, 1)
    grid.attach(buttons[1], 1, 0, 1, 1)
    grid.attach(buttons[2], 0, 1, 2, 1)
    width = grid.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
    grid.allocate(width, 80, -1, None)
    bounds = [button.compute_bounds(grid)[1] for button in buttons]
    export, imported, delete = (
        (round(item.get_x()), round(item.get_width())) for item in bounds
    )
    assert export[1] == imported[1]
    assert delete[0] == export[0]
    assert delete[1] == export[1] + 8 + imported[1]


def test_overlay_cards_use_the_standard_card_with_one_sentence_per_line():
    from dzll_launcher import restart_learning_notice_ui as notice_ui

    assert ui.FEEDBACK_CARD_WIDTH == 520 and ui.FEEDBACK_TEXT_MAX_WIDTH_CHARS == 60
    assert notice_ui.NOTICE_CARD_WIDTH == 520 and notice_ui.NOTICE_TEXT_MAX_WIDTH_CHARS == 60
    assert ui.FEEDBACK_SCRIM_DISMISS_LABELS == {"Cancel", "Keep Pending Import"}
    for name in ("companion_learning_transfer_ui.py", "restart_learning_notice_ui.py"):
        source = (ROOT / "src/dzll_launcher" / name).read_text(encoding="utf-8")
        assert 'add_css_class("warning-card")' in source
        assert 'add_css_class("update-card")' not in source
        assert 'add_css_class("dzll-overlay-heading")' in source
        assert "Gtk.Align.CENTER)" in source
    styles = (ROOT / "src/dzll_launcher/styles.py").read_text(encoding="utf-8")
    assert ".dzll-app-root .warning-card.restart-notice-error .confirmation-title" in styles
    body = transfer.StartupImportResult("applied", server_count=13).notice_body
    assert body.split("\n") == [
        "The previous Server Companion learning database was replaced successfully.",
        "The new database contains 13 learned server records.",
        "A permanent backup of the previous database was retained.",
    ]
    applied = deletion.StartupDeletionResult("applied", scope="servers", server_keys=(KEY,))
    assert len(applied.notice_body.split("\n")) == 2
