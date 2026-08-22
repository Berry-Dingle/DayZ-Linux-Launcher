from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_general_companion_block_order_and_labels():
    source = (ROOT / "src/dzll_launcher/settings_ui.py").read_text(encoding="utf-8")
    general = source.split("    def _settings_page_general", 1)[1].split(
        "    def _settings_page_launch", 1
    )[0]
    show = general.index('"Show Server Companion"')
    heading = general.index('"Server Companion Learning Data"')
    background = general.index('"Show Background Download Buttons"')
    ingame_name = general.index('"Ingame Name"')
    hide_test = general.index('"Hide Test Servers By Default"')
    counts = general.index('"Show Counts In Title Bar"')
    assert ingame_name < background < hide_test < show < heading < counts
    assert general.count("box.append(hr())", 0, show) >= 1
    assert general.count("box.append(hr())", heading, counts) >= 1
    assert '"Export Data"' in general
    assert '"Import Data"' in general
    assert '"Export Learning Data…"' not in general
    assert '"Import Learning Data…"' not in general
    assert 'Gtk.Button(label="Cancel Pending Import")' not in general
    assert '"dialog-warning-symbolic"' in general
    assert '" Learning database replacement pending - "' in general
    assert 'Gtk.Button(label="Cancel")' in general
    assert '"Will be applied on next restart."' in general
    assert '"Export or replace restart-learning data."' not in general
    assert 'export_learning_btn.set_tooltip_text("Export restart-learning data.")' in general
    assert 'import_learning_btn.set_tooltip_text("Replace restart-learning data.")' in general
    assert "server addresses and historical observations" not in general
    assert 'learning_heading.add_css_class("companion-learning-data-title")' in general


def test_pending_import_notice_uses_orange_warning_and_blue_inline_cancel():
    styles = (ROOT / "src/dzll_launcher/styles.py").read_text(encoding="utf-8")
    assert ".settings-panel label.companion-pending-import-warning" in styles
    assert "color: #ff9f1c;" in styles
    assert ".settings-panel button.companion-pending-import-cancel label" in styles
    assert "color: #35a7ff;" in styles
    title_rule = styles.split(
        ".settings-section-title.companion-learning-data-title", 1
    )[1].split("}}", 1)[0]
    assert "font-weight: 400;" in title_rule
    assert "font-size: 1em;" in title_rule
    pending_rule = styles.split(
        ".settings-panel label.companion-pending-import-warning", 1
    )[1].split("}}", 1)[0]
    assert "font-weight" not in pending_rule


def test_ui_routes_three_decisions_and_prevents_duplicate_actions():
    source = (ROOT / "src/dzll_launcher/companion_learning_transfer_ui.py").read_text(
        encoding="utf-8"
    )
    assert '"Replace Server Companion learning database?"' in source
    assert '"Restart Later"' in source and '"Restart Now"' in source
    assert "stage_pending_import(validated" in source
    assert "if self._busy:" in source
    assert "_shutdown_cleanup_done" in source
    assert "request_restart" in source
    assert '("Keep Pending Import", self._hide_feedback, ())' in source
    assert '("Cancel Pending Import", confirmed, ())' in source
    assert "self._feedback_buttons.set_homogeneous(pending_cancel_actions)" in source
    assert "width = 160 if pending_cancel_actions else 140" in source
    restart_later_tail = source.split(
        "if restart_now:", 1
    )[1].split("    def _refresh_sensitivity", 1)[0]
    assert '"OK"' not in restart_later_tail
    assert '"Import staged for next start"' not in source


def test_user_visible_replacement_copy_is_not_additive():
    ui_source = (ROOT / "src/dzll_launcher/companion_learning_transfer_ui.py").read_text(
        encoding="utf-8"
    )
    transfer_source = (ROOT / "src/dzll_launcher/companion_learning_transfer.py").read_text(
        encoding="utf-8"
    )
    combined = ui_source + transfer_source
    assert "record(s)" not in combined
    assert "will be imported" not in combined
    assert "Imported {self.server_count}" not in combined
    assert 'return "Server Companion learning database replaced"' in transfer_source
    assert 'f"The new database contains {records}. "' in transfer_source


def test_startup_apply_precedes_runtime_initialization_and_power_path_is_unchanged():
    source = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
    init = source.split("class DZLLWindow", 1)[1].split("self._pending_last_played_obj", 1)[0]
    assert init.index("apply_pending_import_at_startup(") < init.index(
        "_initialize_companion_restart_runtime_for_window("
    )
    assert "Phase2RestartRuntime.initialize_with_startup_fallback(" in source
    assert 'set_on_power_off(lambda *_: self.set_server_companion_enabled(False))' in source
    visible = source.split("    def set_server_companion_visible", 1)[1].split(
        "    def _refresh_server_companion_power_controls", 1
    )[0]
    assert "apply_pending_import" not in visible
    assert "shutdown(" not in visible


def test_post_restart_import_notice_buttons_are_equal_width():
    source = (ROOT / "src/dzll_launcher/restart_learning_notice_ui.py").read_text(
        encoding="utf-8"
    )
    assert "buttons.set_homogeneous(True)" in source
    assert "self.copy_button.set_size_request(160, -1)" in source
    assert "ok_button.set_size_request(160, -1)" in source
    assert '"persistence_write_failed"' in source
