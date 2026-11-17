from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango

from .companion_learning_deletion import (
    SCOPE_ALL,
    SCOPE_SERVERS,
    pending_deletion_exists,
    pending_import_exists,
    stage_pending_deletion,
)
from .companion_learning_transfer import (
    PendingImportError,
    cancel_pending_import,
    default_export_filename,
    format_learned_server_records,
    format_quarantined_note,
    pending_import_paths,
    read_pending_import,
    stage_pending_import,
    validate_external_learning_file,
    write_export_file,
)
from .config import CFG_DIR


DELETE_ALL_TOOLTIP = "Permanently delete all restart-learning data."
DELETE_BLOCKED_BY_IMPORT_TOOLTIP = (
    "Unavailable while a learning database replacement is pending.\n"
    "Cancel it or restart DZLL first."
)
# Overlay card geometry: the standard confirmation card width (as the Flatpak
# recovery and join cards) with the body wrapped at a readable width.
FEEDBACK_CARD_WIDTH = 520
FEEDBACK_TEXT_MAX_WIDTH_CHARS = 60
# Actions that safely cancel a confirmation; a click outside the card runs it.
FEEDBACK_SCRIM_DISMISS_LABELS = frozenset({"Cancel", "Keep Pending Import"})
DELETE_LOADING_TOOLTIP = "Unavailable while Server Companion learning is loading."
DELETE_NO_LEARNING_TOOLTIP = "No learning data has been saved for this server."
DANGER_ACTION_CSS = "companion-learning-danger-action"


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
        self._delete_all_button = None
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
        self._feedback_dismiss = None

    def bind_settings_widgets(
        self, *, export_button, import_button, cancel_button, pending_container,
        status_label, delete_all_button=None,
    ) -> None:
        self._export_button = export_button
        self._import_button = import_button
        self._cancel_button = cancel_button
        self._delete_all_button = delete_all_button
        self._pending_container = pending_container
        self._status_label = status_label
        self.refresh_pending_state()

    def refresh_pending_state(self) -> None:
        pending = read_pending_import(CFG_DIR, strict=False)
        if self._pending_container is not None:
            self._pending_container.set_visible(pending is not None)
        self._refresh_sensitivity()
        refresh = getattr(self._win, "_refresh_server_companion_delete_learning_state", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.exception("SC learning deletion: bin state refresh failed")

    def server_deletion_block_reason(self, server_key: str | None) -> str | None:
        """Why the panel bin is unavailable for ``server_key``, or ``None``."""

        runtime = getattr(self._win, "_companion_restart_phase2", None)
        if runtime is None or bool(getattr(runtime, "loading", False)):
            return DELETE_LOADING_TOOLTIP
        if pending_import_exists(CFG_DIR):
            return DELETE_BLOCKED_BY_IMPORT_TOOLTIP
        has_learning = getattr(runtime, "has_learning_for", None)
        if server_key and callable(has_learning):
            try:
                if not has_learning(server_key):
                    return DELETE_NO_LEARNING_TOOLTIP
            except Exception:
                logger.exception("SC learning deletion: learning lookup failed")
        return None

    def choose_export(self, *_args) -> None:
        if self._busy:
            return
        runtime = self._win._companion_restart_phase2
        request = getattr(runtime, "export_authoritative_schema4_snapshot_async", None)
        if not callable(request):
            try:
                snapshot = runtime.export_authoritative_schema4_snapshot()
            except Exception as exc:
                self._show_message("Export failed", str(exc), error=True)
                return
            self._present_export_dialog(snapshot)
            return
        # R7: serialization and validation run on the persistence worker; the
        # GTK thread only receives the finished snapshot.
        self._busy = True
        self._set_busy(True, "Preparing learning data export…")

        def ready(snapshot, error) -> None:
            self._set_busy(False)
            if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
                return
            if error is not None or snapshot is None:
                self._show_message(
                    "Export failed", str(error or "Unknown export error."), error=True
                )
                return
            self._present_export_dialog(snapshot)

        try:
            request(ready)
        except Exception as exc:
            self._set_busy(False)
            self._show_message("Export failed", str(exc), error=True)

    def _present_export_dialog(self, snapshot) -> None:
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
            if self._busy:
                return
            self._busy = True
            self._set_busy(True, "Writing learning data export…")

            def written(_result, error) -> None:
                self._set_busy(False)
                if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
                    return
                if error is not None:
                    self._show_message("Export failed", str(error), error=True)
                    return
                body = (
                    "Exported a database containing "
                    f"{format_learned_server_records(snapshot.server_count)} to:\n{path}"
                )
                note = format_quarantined_note(
                    len(getattr(snapshot, "quarantined_server_keys", ()) or ())
                )
                if note:
                    body += f"\n\n{note}"
                self._show_message("Learning data exported", body)

            self._run_off_gtk_thread(
                lambda: write_export_file(path, snapshot.canonical_bytes), written
            )

        dialog.connect("response", done)
        dialog.show()

    def _run_off_gtk_thread(self, work, done) -> None:
        """Run blocking file work on the executor; ``done(result, error)`` on GTK."""

        def guarded():
            try:
                return work(), None
            except Exception as exc:
                logger.exception("SC learning transfer background work failed")
                return None, exc

        try:
            future = self._win._hi_executor.submit(guarded)
        except Exception as exc:
            logger.exception("SC learning transfer background submission failed")
            done(None, exc)
            return
        if future.done():
            result, error = future.result()
            done(result, error)
            return

        def finished(completed) -> None:
            try:
                result, error = completed.result()
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(lambda: (done(result, error), False)[1])

        future.add_done_callback(finished)

    def choose_import(self, *_args) -> None:
        if self._busy:
            logger.debug("SC learning import: duplicate action blocked while busy")
            return
        if pending_deletion_exists(CFG_DIR):
            self._show_message(
                "Import unavailable",
                "A Server Companion learning data deletion is pending. "
                "Restart DZLL to complete it before importing a database.",
                error=True,
            )
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
            "This removes only the staged private copy.\n"
            "The current Server Companion learning database is unchanged.",
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
            f"The selected database contains {records}.\n\n"
            "DZLL must restart to complete the replacement."
        )
        note = format_quarantined_note(getattr(validated, "quarantined_server_count", 0))
        if note:
            body += f"\n\n{note}"
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
        # R7: the staged copy (database-sized write, fsync and verification)
        # is written off the GTK thread.
        self._run_off_gtk_thread(
            lambda: stage_pending_import(validated, config_dir=CFG_DIR, replace_existing=True),
            lambda _result, error: self._staging_finished(error, restart_now=restart_now),
        )

    def _staging_finished(self, error, *, restart_now: bool) -> None:
        self._set_busy(False)
        if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
            return
        if error is not None:
            logger.error("SC learning import staging failed: %s", error)
            self._show_message("Database replacement staging failed", str(error), error=True)
            return
        self.refresh_pending_state()
        if restart_now:
            app = self._win.get_application()
            request = getattr(app, "request_restart", None)
            if callable(request):
                request()
                return
            self._show_message(
                "Database replacement staged",
                "DZLL will close.\nReopen it to apply the pending database replacement.",
            )
            app.quit()
            return

    # ------------------------------------------------------------------
    # Learning data deletion (staged; applied at the next startup)
    # ------------------------------------------------------------------

    def choose_delete_all(self, *_args) -> None:
        if self._busy:
            return
        if pending_import_exists(CFG_DIR):
            self._show_message(
                "Deletion unavailable", DELETE_BLOCKED_BY_IMPORT_TOOLTIP, error=True
            )
            return
        body = (
            "All restart-learning data for every server will be permanently deleted.\n"
            "This cannot be undone.\n"
            "DZLL does not keep a backup of the deleted data.\n\n"
            "Exporting your learning data first is strongly recommended, so it can be "
            "imported again later.\n\n"
            "DZLL will restart to complete the deletion.\n"
            "Server Companion will open without a monitored server.\n"
            "Settings, favourites and alert preferences are not changed."
        )
        self._show_deletion_confirmation(
            "Delete all Server Companion learning data?",
            body,
            scope=SCOPE_ALL,
            server_keys=(),
        )

    def choose_delete_server(self, server_key: str | None, server_name: str = "") -> None:
        if self._busy:
            return
        key = str(server_key or "").strip()
        if not key:
            return
        reason = self.server_deletion_block_reason(key)
        if reason is not None:
            self._show_message("Deletion unavailable", reason, error=True)
            return
        name = str(server_name or "").strip() or key
        body = (
            f"Server: {name}\n\n"
            "Only the restart-learning data for this monitored server will be "
            "permanently deleted.\n"
            "Learning data for all other servers is kept.\n"
            "This cannot be undone.\n"
            "DZLL does not keep a backup of the deleted data.\n\n"
            "Exporting your learning data first is recommended.\n\n"
            "DZLL will restart to complete the deletion.\n"
            "Server Companion will open without a monitored server."
        )
        self._show_deletion_confirmation(
            "Delete learning data for this server?",
            body,
            scope=SCOPE_SERVERS,
            server_keys=(key,),
        )

    def _show_deletion_confirmation(self, title, body, *, scope, server_keys) -> None:
        keys = tuple(server_keys)

        def export_first():
            self._hide_feedback()
            self.choose_export()

        def delete_now():
            self._hide_feedback()
            self._stage_deletion(scope=scope, server_keys=keys)

        self._show_feedback(
            title,
            body,
            actions=(
                ("Cancel", self._hide_feedback, ()),
                ("Export Data", export_first, ()),
                ("Delete and Restart", delete_now, (DANGER_ACTION_CSS,)),
            ),
            error=True,
        )

    def _stage_deletion(self, *, scope: str, server_keys: tuple[str, ...]) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_busy(True, "Preparing learning data deletion…")
        logger.debug(
            "SC learning deletion: staging scope=%s servers=%d", scope, len(server_keys)
        )
        self._run_off_gtk_thread(
            lambda: stage_pending_deletion(
                config_dir=CFG_DIR, scope=scope, server_keys=server_keys
            ),
            lambda _result, error: self._deletion_staged(error),
        )

    def _deletion_staged(self, error) -> None:
        self._set_busy(False)
        if bool(getattr(self._win, "_shutdown_cleanup_done", False)):
            return
        if error is not None:
            logger.error("SC learning deletion staging failed: %s", error)
            self._show_message("Learning data deletion failed", str(error), error=True)
            return
        # D5: after the restart Server Companion shows no monitored server.
        # The panel's Clear path ends monitoring and forgets the last server.
        if getattr(self._win, "_server_companion_obj", None) is not None:
            try:
                self._win.clear_server_companion()
            except Exception:
                logger.exception("SC learning deletion: clearing the monitored server failed")
        app = self._win.get_application()
        request = getattr(app, "request_restart", None)
        if callable(request):
            request()
            return
        self._show_message(
            "Learning data deletion staged",
            "DZLL will close.\nReopen it to complete the deletion.",
        )
        app.quit()

    def _refresh_sensitivity(self) -> None:
        for button in (self._export_button, self._import_button, self._cancel_button):
            if button is not None:
                button.set_sensitive(not self._busy)
        if self._delete_all_button is not None:
            blocked = pending_import_exists(CFG_DIR)
            self._delete_all_button.set_sensitive(not self._busy and not blocked)
            self._delete_all_button.set_tooltip_text(
                DELETE_BLOCKED_BY_IMPORT_TOOLTIP if blocked else DELETE_ALL_TOOLTIP
            )

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
        """Build the card in the standard DZLL confirmation style.

        Same pattern as the Mod Manager and Flatpak confirmations: a centred
        ``warning-card`` with a left-aligned ``dzll-overlay-heading`` title,
        ``confirmation-body`` text and ``warning-btn`` buttons.
        """

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
        # A click outside a confirmation cancels it (never a result message).
        scrim_click = Gtk.GestureClick.new()
        scrim_click.set_button(0)
        scrim_click.connect("pressed", lambda *_args: self._on_feedback_scrim_pressed())
        scrim.add_controller(scrim_click)
        owner.add_overlay(scrim)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.add_css_class("warning-card")
        card.add_css_class("companion-learning-card")
        card.set_size_request(FEEDBACK_CARD_WIDTH, -1)

        title = Gtk.Label()
        title.set_xalign(0.0)
        title.set_wrap(True)
        title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_max_width_chars(FEEDBACK_TEXT_MAX_WIDTH_CHARS)
        title.add_css_class("dzll-overlay-heading")
        title.add_css_class("confirmation-title")
        card.append(title)

        body = Gtk.Label()
        body.set_xalign(0.0)
        body.set_wrap(True)
        # Paths and long server names may have no spaces to break at.
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_max_width_chars(FEEDBACK_TEXT_MAX_WIDTH_CHARS)
        body.set_selectable(True)
        body.add_css_class("confirmation-body")
        card.append(body)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(12)
        card.append(buttons)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        revealer.set_transition_duration(150)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.CENTER)
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
        # Only a confirmation with a safe cancelling action is dismissed by a
        # click outside the card; result messages need their own button.
        self._feedback_dismiss = None
        for label, callback, css_classes in actions:
            if str(label) in FEEDBACK_SCRIM_DISMISS_LABELS:
                self._feedback_dismiss = (str(label), callback)
            button = Gtk.Button(label=label)
            button.add_css_class("warning-btn")
            for css_class in css_classes:
                button.add_css_class(css_class)
            button.connect(
                "clicked",
                lambda _button, cb=callback, action=label: self._invoke_feedback_action(
                    action, cb
                ),
            )
            self._feedback_buttons.append(button)
        # Warning and error titles are red (the card keeps the shared
        # ``restart-notice-error`` modifier).
        if error:
            self._feedback_card.add_css_class("restart-notice-error")
        else:
            self._feedback_card.remove_css_class("restart-notice-error")
        self._feedback_scrim.set_visible(True)
        self._feedback_revealer.set_visible(True)
        self._feedback_revealer.set_reveal_child(True)

    def _on_feedback_scrim_pressed(self) -> None:
        dismiss = getattr(self, "_feedback_dismiss", None)
        if dismiss is None:
            return
        label, callback = dismiss
        self._invoke_feedback_action(label, callback)

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
