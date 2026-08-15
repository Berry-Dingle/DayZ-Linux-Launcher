from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from .companion_learning_transfer import (
    PendingImportError,
    cancel_pending_import,
    default_export_filename,
    format_learned_server_records,
    pending_import_paths,
    read_pending_import,
    stage_pending_import,
    validate_external_learning_file,
    write_export_file,
)
from .config import CFG_DIR


logger = logging.getLogger(__name__)


class CompanionLearningTransferController:
    """GTK orchestration only; validation and file transactions stay pure."""

    def __init__(self, window) -> None:
        self._win = window
        self._busy = False
        self._generation = 0
        self._export_button = None
        self._import_button = None
        self._cancel_button = None
        self._pending_container = None
        self._status_label = None
        self._import_chooser = None
        self._validation_future = None
        self._feedback_scrim = None
        self._feedback_revealer = None
        self._feedback_card = None
        self._feedback_title = None
        self._feedback_body = None
        self._feedback_buttons = None

    def bind_settings_widgets(
        self, *, export_button, import_button, cancel_button, pending_container,
        status_label,
    ) -> None:
        self._export_button = export_button
        self._import_button = import_button
        self._cancel_button = cancel_button
        self._pending_container = pending_container
        self._status_label = status_label
        self.refresh_pending_state()

    def refresh_pending_state(self) -> None:
        pending = read_pending_import(CFG_DIR, strict=False)
        if self._pending_container is not None:
            self._pending_container.set_visible(pending is not None)
        self._refresh_sensitivity()

    def choose_export(self, *_args) -> None:
        if self._busy:
            return
        try:
            snapshot = self._win._companion_restart_phase2.export_authoritative_schema4_snapshot()
        except Exception as exc:
            self._show_message("Export failed", str(exc), error=True)
            return
        dialog = Gtk.FileChooserNative.new(
            title="Export Server Companion Learning Data",
            parent=self._win,
            action=Gtk.FileChooserAction.SAVE,
            accept_label="Export",
            cancel_label="Cancel",
        )
        dialog.set_current_name(default_export_filename())
        if hasattr(dialog, "set_do_overwrite_confirmation"):
            dialog.set_do_overwrite_confirmation(True)
        dialog.add_filter(self._json_filter())

        def done(chooser, response):
            if response != Gtk.ResponseType.ACCEPT:
                return
            selected = chooser.get_file()
            path = selected.get_path() if selected else None
            if not path:
                self._show_message("Export failed", "The selected destination is not a local file.", error=True)
                return
            try:
                write_export_file(path, snapshot.canonical_bytes)
            except Exception as exc:
                self._show_message("Export failed", str(exc), error=True)
                return
            self._show_message(
                "Learning data exported",
                "Exported a database containing "
                f"{format_learned_server_records(snapshot.server_count)} to:\n{path}",
            )

        dialog.connect("response", done)
        dialog.show()

    def choose_import(self, *_args) -> None:
        if self._busy:
            logger.debug("SC learning import: duplicate action blocked while busy")
            return
        logger.debug("SC learning import: opening native file chooser")
        self._set_busy(True, "Choose a schema-4 JSON file…")
        try:
            dialog = Gtk.FileChooserNative.new(
                title="Import Server Companion Learning Data",
                parent=self._win,
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Open",
                cancel_label="Cancel",
            )
            dialog.add_filter(self._json_filter())
            dialog.connect("response", self._on_import_chooser_response)
            self._import_chooser = dialog
            dialog.show()
        except Exception as exc:
            logger.exception("SC learning import chooser creation/display failed")
            self._import_chooser = None
            self._set_busy(False)
            self._show_message("Database replacement failed", f"Could not open the file chooser: {exc}", error=True)

    def _on_import_chooser_response(self, chooser, response) -> None:
        try:
            logger.debug("SC learning import: chooser response=%s", int(response))
            try:
                chooser.hide()
            except Exception:
                logger.exception("SC learning import: chooser hide failed")
            self._import_chooser = None
            if response != Gtk.ResponseType.ACCEPT:
                logger.debug("SC learning import: chooser cancelled")
                self._set_busy(False)
                return
            selected = chooser.get_file()
            path = selected.get_path() if selected is not None else None
            if not path:
                raise ValueError("The selected source is not a local file.")
            logger.debug("SC learning import: selected file=%s", Path(path).name)
            self._begin_validation(Path(path))
        except Exception as exc:
            logger.exception("SC learning import chooser response failed")
            self._set_busy(False)
            self._show_message("Database replacement failed", str(exc), error=True)

    def cancel_pending(self, *_args) -> None:
        if self._busy:
            return

        def confirmed():
            self._hide_feedback()
            try:
                cancel_pending_import(CFG_DIR)
                self.refresh_pending_state()
                self._show_message(
                    "Pending database replacement cancelled",
                    "The current Server Companion learning database was not changed.",
                )
            except Exception as exc:
                logger.exception("SC learning pending-import cancellation failed")
                self._show_message("Cancellation failed", str(exc), error=True)

        self._show_feedback(
            "Cancel pending learning database replacement?",
            "This removes only the staged private copy. The current Server Companion learning database is unchanged.",
            actions=(
                ("Keep Pending Import", self._hide_feedback, ()),
                ("Cancel Pending Import", confirmed, ()),
            ),
            error=True,
        )

    def _begin_validation(self, path: Path) -> None:
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._set_busy(True, "Validating selected learning database…")
        logger.debug(
            "SC learning import: submitting validation generation=%d file=%s",
            generation,
            path.name,
        )

        def worker():
            try:
                logger.debug("SC learning import: validation worker started generation=%d", generation)
                return validate_external_learning_file(path), None
            except Exception as exc:
                logger.exception("SC learning import validation failed generation=%d", generation)
                return None, str(exc)

        try:
            future = self._win._hi_executor.submit(worker)
            self._validation_future = future
            future.add_done_callback(
                lambda completed: self._validation_worker_done(generation, completed)
            )
        except Exception as exc:
            logger.exception("SC learning import validation submission failed")
            self._validation_future = None
            self._set_busy(False)
            self._show_message("Database replacement failed", str(exc), error=True)

    def _validation_worker_done(self, generation: int, future) -> None:
        try:
            validated, error = future.result()
        except Exception as exc:
            logger.exception("SC learning import validation future failed generation=%d", generation)
            validated, error = None, str(exc)
        try:
            source_id = GLib.idle_add(
                self._validation_complete, generation, validated, error
            )
            logger.debug(
                "SC learning import: GTK completion queued generation=%d source=%s",
                generation,
                source_id,
            )
        except Exception:
            logger.exception(
                "SC learning import: could not queue GTK completion generation=%d",
                generation,
            )

    def _validation_complete(self, generation: int, validated, error: str | None) -> bool:
        logger.debug(
            "SC learning import: GTK completion generation=%d result=%s",
            generation,
            "error" if error or validated is None else "valid",
        )
        if generation != self._generation:
            logger.debug(
                "SC learning import: stale completion ignored generation=%d current=%d",
                generation,
                self._generation,
            )
            return False
        if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
            logger.debug("SC learning import: completion ignored during shutdown")
            return False
        self._validation_future = None
        self._set_busy(False)
        if error or validated is None:
            self._show_message("Selected database validation failed", error or "Unknown validation error.", error=True)
            return False
        try:
            self._show_import_warning(validated)
        except Exception as exc:
            logger.exception("SC learning import confirmation presentation failed")
            self._show_message(
                "Database replacement confirmation failed",
                f"The file was valid, but the confirmation could not be displayed: {exc}",
                error=True,
            )
        return False

    def _show_import_warning(self, validated) -> None:
        existing_payload, existing_meta = pending_import_paths(CFG_DIR)
        replacing = existing_payload.exists() or existing_meta.exists()
        records = format_learned_server_records(validated.server_count)
        body = (
            "The current Server Companion learning database will be completely replaced with the selected database.\n"
            "A permanent backup of the current database will be created before replacement.\n\n"
            f"File: {validated.source_filename}\nSchema: {validated.schema_version}\n"
            f"The selected database contains {records}.\n"
            "DZLL must restart to complete the replacement."
        )
        if replacing:
            body += "\n\nThe currently staged replacement will be replaced by this selected database."
        body += "\n\nLearning data may contain server addresses and historical observations."
        logger.debug(
            "SC learning import: presenting confirmation file=%s schema=%d servers=%d",
            validated.source_filename,
            validated.schema_version,
            validated.server_count,
        )

        def restart_later():
            self._hide_feedback()
            self._stage_after_approval(validated, restart_now=False)

        def restart_now():
            self._hide_feedback()
            self._stage_after_approval(validated, restart_now=True)

        self._show_feedback(
            "Replace Server Companion learning database?",
            body,
            actions=(
                ("Cancel", self._hide_feedback, ()),
                ("Restart Later", restart_later, ()),
                ("Restart Now", restart_now, ("suggested-action",)),
            ),
            error=True,
        )

    def _stage_after_approval(self, validated, *, restart_now: bool) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_busy(True, "Staging replacement learning database…")
        logger.debug(
            "SC learning import: staging approved action=%s",
            "restart-now" if restart_now else "restart-later",
        )
        try:
            stage_pending_import(validated, config_dir=CFG_DIR, replace_existing=True)
        except Exception as exc:
            logger.exception("SC learning import staging failed")
            self._show_message("Database replacement staging failed", str(exc), error=True)
            return
        finally:
            self._set_busy(False)
        self.refresh_pending_state()
        if restart_now:
            app = self._win.get_application()
            request = getattr(app, "request_restart", None)
            if callable(request):
                request()
                return
            self._show_message(
                "Database replacement staged",
                "DZLL will close. Reopen it to apply the pending database replacement.",
            )
            app.quit()
            return

    def _refresh_sensitivity(self) -> None:
        for button in (self._export_button, self._import_button, self._cancel_button):
            if button is not None:
                button.set_sensitive(not self._busy)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = bool(busy)
        self._refresh_sensitivity()
        if self._status_label is not None:
            self._status_label.set_text(str(status or ""))
            self._status_label.set_visible(bool(status))

    @staticmethod
    def _json_filter():
        value = Gtk.FileFilter()
        value.set_name("JSON files")
        value.add_mime_type("application/json")
        value.add_pattern("*.json")
        return value

    def _show_message(self, title: str, body: str, *, error: bool = False) -> None:
        try:
            self._show_feedback(
                title,
                str(body),
                actions=(("OK", self._hide_feedback, ("suggested-action",)),),
                error=error,
            )
        except Exception:
            logger.exception("SC learning import/export feedback presentation failed")
            if self._status_label is not None:
                self._status_label.set_text(f"{title}: {body}")
                self._status_label.set_visible(True)

    def _ensure_feedback_overlay(self) -> None:
        if self._feedback_revealer is not None:
            return
        owner = getattr(self._win, "_main_overlay", None)
        if owner is None:
            raise RuntimeError("main-window overlay is unavailable")

        scrim = Gtk.Box()
        scrim.set_hexpand(True)
        scrim.set_vexpand(True)
        scrim.set_visible(False)
        scrim.set_can_target(True)
        scrim.add_css_class("settings-scrim")
        owner.add_overlay(scrim)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("update-card")
        card.add_css_class("restart-learning-notice-card")
        card.set_size_request(640, -1)
        card.set_margin_start(12)
        card.set_margin_end(12)
        card.set_margin_top(12)
        card.set_margin_bottom(12)

        title = Gtk.Label()
        title.add_css_class("update-title")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        card.append(title)

        body = Gtk.Label()
        body.add_css_class("update-subtitle")
        body.set_wrap(True)
        body.set_justify(Gtk.Justification.CENTER)
        body.set_selectable(True)
        card.append(body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(20)
        card.append(buttons)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(150)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.START)
        revealer.set_margin_top(50)
        revealer.set_reveal_child(False)
        revealer.set_visible(False)
        revealer.set_child(card)
        owner.add_overlay(revealer)

        self._feedback_scrim = scrim
        self._feedback_revealer = revealer
        self._feedback_card = card
        self._feedback_title = title
        self._feedback_body = body
        self._feedback_buttons = buttons

    def _show_feedback(self, title: str, body: str, *, actions, error: bool) -> None:
        self._ensure_feedback_overlay()
        while True:
            child = self._feedback_buttons.get_first_child()
            if child is None:
                break
            self._feedback_buttons.remove(child)
        self._feedback_title.set_text(str(title))
        self._feedback_body.set_text(str(body))
        pending_cancel_actions = {
            str(action[0]) for action in actions
        } == {"Keep Pending Import", "Cancel Pending Import"}
        self._feedback_buttons.set_homogeneous(pending_cancel_actions)
        for label, callback, css_classes in actions:
            button = Gtk.Button(label=label)
            width = 160 if pending_cancel_actions else 140
            button.set_size_request(width, -1)
            for css_class in css_classes:
                button.add_css_class(css_class)
            button.connect(
                "clicked",
                lambda _button, cb=callback, action=label: self._invoke_feedback_action(
                    action, cb
                ),
            )
            self._feedback_buttons.append(button)
        if error:
            self._feedback_card.add_css_class("restart-notice-error")
        else:
            self._feedback_card.remove_css_class("restart-notice-error")
        self._feedback_scrim.set_visible(True)
        self._feedback_revealer.set_visible(True)
        self._feedback_revealer.set_reveal_child(True)

    def _invoke_feedback_action(self, label: str, callback) -> None:
        try:
            callback()
        except Exception as exc:
            logger.exception("SC learning import feedback action failed: %s", label)
            self._show_message(
                "Database replacement action failed",
                f"{label} could not be completed: {exc}",
                error=True,
            )

    def _hide_feedback(self, *_args) -> None:
        if self._feedback_revealer is not None:
            self._feedback_revealer.set_reveal_child(False)
            self._feedback_revealer.set_visible(False)
        if self._feedback_scrim is not None:
            self._feedback_scrim.set_visible(False)
