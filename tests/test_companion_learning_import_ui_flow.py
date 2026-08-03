from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

from dzll_launcher import companion_learning_transfer_ui as ui
from dzll_launcher.companion_learning_transfer import ValidatedLearningImport


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.text = str(kwargs.get("label", ""))
        self.visible = False
        self.sensitive = True
        self.children = []
        self.revealed = False
        self.classes = set()
        self.callback = None
        self.margin_top = 0
        self.size_request = None
        self.homogeneous = False

    def set_text(self, value): self.text = str(value)
    def set_visible(self, value): self.visible = bool(value)
    def set_sensitive(self, value): self.sensitive = bool(value)
    def set_hexpand(self, _value): pass
    def set_vexpand(self, _value): pass
    def set_can_target(self, _value): pass
    def set_size_request(self, *args): self.size_request = args
    def set_margin_start(self, _value): pass
    def set_margin_end(self, _value): pass
    def set_margin_top(self, value): self.margin_top = value
    def set_margin_bottom(self, _value): pass
    def set_wrap(self, _value): pass
    def set_justify(self, _value): pass
    def set_selectable(self, _value): pass
    def set_halign(self, _value): pass
    def set_homogeneous(self, value): self.homogeneous = bool(value)
    def set_valign(self, _value): pass
    def set_transition_type(self, _value): pass
    def set_transition_duration(self, _value): pass
    def add_css_class(self, value): self.classes.add(value)
    def remove_css_class(self, value): self.classes.discard(value)
    def append(self, child): self.children.append(child)
    def remove(self, child): self.children.remove(child)
    def get_first_child(self): return self.children[0] if self.children else None
    def set_child(self, child): self.children = [child]
    def set_reveal_child(self, value): self.revealed = bool(value)
    def connect(self, _signal, callback): self.callback = callback


class FakeOverlay:
    def __init__(self): self.overlays = []
    def add_overlay(self, child): self.overlays.append(child)


class ImmediateExecutor:
    def submit(self, callback):
        future = Future()
        try:
            future.set_result(callback())
        except BaseException as exc:
            future.set_exception(exc)
        return future


class FakeChooser:
    def __init__(self, path=None, error=None):
        self.path = path
        self.error = error
        self.hidden = False

    def hide(self): self.hidden = True
    def get_file(self):
        if self.error:
            raise self.error
        if self.path is None:
            return None
        return SimpleNamespace(get_path=lambda: str(self.path))


def controller(tmp_path):
    owner = FakeOverlay()
    win = SimpleNamespace(
        _shutdown_cleanup_done=False,
        _hi_executor=ImmediateExecutor(),
        _main_overlay=owner,
    )
    value = ui.CompanionLearningTransferController(win)
    value._export_button = FakeWidget()
    value._import_button = FakeWidget()
    value._cancel_button = FakeWidget()
    value._pending_container = FakeWidget()
    value._status_label = FakeWidget()
    return value, owner


def validated(filename="valid-export.json", count=4):
    return ValidatedLearningImport(b"{}", "0" * 64, 4, count, filename)


def test_valid_chooser_selection_reaches_validation(tmp_path):
    value, _owner = controller(tmp_path)
    seen = []
    value._begin_validation = seen.append
    chooser = FakeChooser(tmp_path / "export.json")
    value._busy = True
    value._on_import_chooser_response(chooser, ui.Gtk.ResponseType.ACCEPT)
    assert chooser.hidden
    assert seen == [tmp_path / "export.json"]


def test_validation_completion_is_handed_to_gtk_thread(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    queued = []
    monkeypatch.setattr(ui, "validate_external_learning_file", lambda _path: validated())
    monkeypatch.setattr(
        ui.GLib,
        "idle_add",
        lambda callback, *args: queued.append((callback, args)) or 77,
    )
    value._begin_validation(tmp_path / "valid.json")
    assert len(queued) == 1
    callback, args = queued[0]
    assert callback == value._validation_complete
    assert args[0] == value._generation
    assert args[1].source_filename == "valid-export.json"


def test_valid_export_displays_complete_confirmation_without_staging(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    monkeypatch.setattr(ui, "CFG_DIR", str(tmp_path))
    shown = []
    value._show_feedback = lambda title, body, **kwargs: shown.append((title, body, kwargs))
    value._generation = 3
    assert value._validation_complete(3, validated("selected.json", 9), None) is False
    title, body, options = shown[-1]
    assert title == "Replace Server Companion learning database?"
    assert "File: selected.json" in body
    assert "current Server Companion learning database will be completely replaced" in body
    assert "Schema: 4" in body
    assert "selected database contains 9 learned server records" in body
    assert "permanent backup" in body
    assert "must restart to complete the replacement" in body
    assert [item[0] for item in options["actions"]] == ["Cancel", "Restart Later", "Restart Now"]
    assert options["actions"][0][2] == ()
    assert list(tmp_path.iterdir()) == []


def test_invalid_json_displays_actual_validation_error(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    source = tmp_path / "bad.json"
    source.write_text("{")
    shown = []
    value._show_feedback = lambda title, body, **kwargs: shown.append((title, body))
    monkeypatch.setattr(ui.GLib, "idle_add", lambda callback, *args: callback(*args) or 1)
    value._begin_validation(source)
    assert shown
    assert shown[-1][0] == "Selected database validation failed"
    assert "Invalid JSON" in shown[-1][1]
    assert not value._busy


def test_chooser_cancellation_clears_busy_without_error(tmp_path):
    value, _owner = controller(tmp_path)
    errors = []
    value._show_message = lambda *args, **kwargs: errors.append((args, kwargs))
    value._set_busy(True, "waiting")
    chooser = FakeChooser()
    value._on_import_chooser_response(chooser, ui.Gtk.ResponseType.CANCEL)
    assert chooser.hidden
    assert not value._busy
    assert value._status_label.text == ""
    assert errors == []


def test_chooser_callback_exception_becomes_visible_error(tmp_path):
    value, _owner = controller(tmp_path)
    errors = []
    value._show_message = lambda title, body, **kwargs: errors.append((title, body, kwargs))
    value._set_busy(True, "waiting")
    value._on_import_chooser_response(
        FakeChooser(error=RuntimeError("Gio path extraction exploded")),
        ui.Gtk.ResponseType.ACCEPT,
    )
    assert not value._busy
    assert errors[-1][0] == "Database replacement failed"
    assert "Gio path extraction exploded" in errors[-1][1]


def test_confirmation_exception_becomes_visible_error(tmp_path):
    value, _owner = controller(tmp_path)
    errors = []
    value._show_import_warning = lambda _validated: (_ for _ in ()).throw(RuntimeError("overlay failed"))
    value._show_message = lambda title, body, **kwargs: errors.append((title, body, kwargs))
    value._generation = 2
    value._validation_complete(2, validated(), None)
    assert errors[-1][0] == "Database replacement confirmation failed"
    assert "overlay failed" in errors[-1][1]


def test_feedback_exception_falls_back_to_visible_settings_status(tmp_path):
    value, _owner = controller(tmp_path)
    value._show_feedback = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("presentation failed")
    )
    value._show_message("Database replacement failed", "Readable reason", error=True)
    assert value._status_label.visible
    assert value._status_label.text == "Database replacement failed: Readable reason"


def test_pending_state_remains_visible_while_staged(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    pending = SimpleNamespace(source_filename="selected.json", server_count=6)
    monkeypatch.setattr(ui, "read_pending_import", lambda *_args, **_kwargs: pending)
    value.refresh_pending_state()
    assert value._pending_container.visible


def test_restart_later_stages_and_returns_without_second_modal(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    staged = []
    refreshed = []
    messages = []
    monkeypatch.setattr(
        ui,
        "stage_pending_import",
        lambda item, **kwargs: staged.append((item, kwargs)),
    )
    value.refresh_pending_state = lambda: refreshed.append(True)
    value._show_message = lambda *args, **kwargs: messages.append((args, kwargs))
    item = validated("selected.json", 6)
    value._stage_after_approval(item, restart_now=False)
    assert staged and staged[0][0] is item
    assert refreshed == [True]
    assert messages == []
    assert not value._busy


def test_restart_later_staging_failure_remains_visible(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    messages = []
    monkeypatch.setattr(
        ui,
        "stage_pending_import",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    value._show_message = lambda *args, **kwargs: messages.append((args, kwargs))
    value._stage_after_approval(validated(), restart_now=False)
    assert messages[0][0] == ("Database replacement staging failed", "disk full")
    assert messages[0][1]["error"] is True


def test_warning_overlay_is_attached_and_revealed_on_main_owner(tmp_path, monkeypatch):
    value, owner = controller(tmp_path)
    fake_gtk = SimpleNamespace(
        Box=FakeWidget,
        Label=FakeWidget,
        Button=FakeWidget,
        Revealer=FakeWidget,
        Orientation=SimpleNamespace(VERTICAL=1, HORIZONTAL=2),
        Justification=SimpleNamespace(CENTER=1),
        Align=SimpleNamespace(CENTER=1, START=2),
        RevealerTransitionType=SimpleNamespace(SLIDE_DOWN=1),
    )
    monkeypatch.setattr(ui, "Gtk", fake_gtk)
    value._show_feedback(
        "Warning",
        "Body",
        actions=(
            ("Cancel", lambda: None, ()),
            ("Restart Later", lambda: None, ()),
            ("Restart Now", lambda: None, ()),
        ),
        error=True,
    )
    assert len(owner.overlays) == 2
    assert owner.overlays[0] is value._feedback_scrim
    assert owner.overlays[1] is value._feedback_revealer
    assert value._feedback_scrim.visible
    assert value._feedback_revealer.visible and value._feedback_revealer.revealed
    assert value._feedback_revealer.margin_top == 50
    button_row = value._feedback_buttons
    assert button_row.margin_top == 20
    assert [child.size_request for child in button_row.children] == [
        (140, -1), (140, -1), (140, -1)
    ]
    value._show_message("Complete", "Done")
    assert not button_row.homogeneous
    assert len(button_row.children) == 1
    assert button_row.children[0].text == "OK"
    assert button_row.children[0].size_request == (140, -1)
    value._show_feedback(
        "Cancel pending learning import?",
        "Body",
        actions=(
            ("Keep Pending Import", lambda: None, ()),
            ("Cancel Pending Import", lambda: None, ()),
        ),
        error=True,
    )
    assert button_row.homogeneous
    assert [child.size_request for child in button_row.children] == [
        (160, -1), (160, -1)
    ]


def test_real_warning_overlay_is_visible_above_window_when_display_available():
    if Gdk.Display.get_default() is None:
        pytest.skip("usable GTK display is unavailable")
    try:
        window = ui.Gtk.Window()
        overlay = ui.Gtk.Overlay()
        window.set_child(overlay)
        controller = ui.CompanionLearningTransferController(
            SimpleNamespace(_main_overlay=overlay)
        )
        window.present()
        controller._show_feedback(
            "Replace Server Companion learning data?",
            "File: selected.json\nSchema: 4\nServer records: 2",
            actions=(("Cancel", controller._hide_feedback, ()),),
            error=True,
        )
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        assert controller._feedback_revealer.get_root() is window
        assert controller._feedback_revealer.get_visible()
        assert controller._feedback_revealer.get_reveal_child()
    except RuntimeError as exc:
        pytest.skip(f"usable GTK display is unavailable: {exc}")
    finally:
        if "window" in locals():
            window.destroy()


def test_repeated_import_click_is_blocked_while_active(tmp_path, monkeypatch):
    value, _owner = controller(tmp_path)
    calls = []
    monkeypatch.setattr(
        ui.Gtk.FileChooserNative,
        "new",
        lambda **_kwargs: calls.append(True),
    )
    value._set_busy(True, "Validating learning data…")
    value.choose_import()
    assert calls == []
    assert not value._import_button.sensitive
