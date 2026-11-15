#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from .ui_row import attach_pointer_cursor


BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET = 10
BACKGROUND_PREPARE_ACTIVE_BOTTOM_SPACING = 8


class BackgroundPrepareStatusUI:
    """Build and present the background-preparation status strip.

    Presentation only. The request lifecycle, queue snapshots, the operation
    gate, reap recovery, Steam consent and the retry/close actions stay in the
    window, which calls these methods on the GTK main thread. The strip widgets
    are mirrored onto the window under their existing names.
    """

    def __init__(self, win):
        self.win = win
        self.background_prepare_status = None
        self.background_prepare_grid = None
        self.background_prepare_info = None
        self.background_prepare_server_label = None
        self.background_prepare_detail_label = None
        self.background_prepare_queue_divider = None
        self.background_prepare_queue_label = None
        self.background_prepare_failed_label = None
        self.background_prepare_retry_btn = None
        self.background_prepare_count_label = None
        self.background_prepare_percent_label = None
        self.background_prepare_right_status_label = None
        self.background_prepare_action_btn = None

    def build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_hexpand(True)
        root.set_vexpand(False)
        root.set_valign(Gtk.Align.START)
        root.set_visible(False)
        root.add_css_class("dzll-background-prepare-status")

        grid = Gtk.Grid()
        grid.set_hexpand(True)
        grid.set_vexpand(False)
        grid.set_margin_top(3)
        grid.set_margin_bottom(3)
        grid.set_margin_start(8)
        grid.set_margin_end(8)
        self.background_prepare_grid = grid
        root.append(grid)

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.background_prepare_info = info
        info.add_css_class("dzll-background-prepare-info")
        info.set_vexpand(False)
        info.set_valign(Gtk.Align.CENTER)
        grid.attach(info, 0, 0, 2, 1)

        server_block = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        server_block.set_size_request(240, -1)
        server_block.add_css_class("dzll-background-prepare-server-block")
        info.append(server_block)

        self.background_prepare_server_label = Gtk.Label(xalign=0.0)
        self.background_prepare_server_label.set_hexpand(True)
        self.background_prepare_server_label.set_wrap(False)
        self.background_prepare_server_label.set_single_line_mode(True)
        self.background_prepare_server_label.set_width_chars(30)
        self.background_prepare_server_label.set_max_width_chars(30)
        self.background_prepare_server_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.background_prepare_server_label.add_css_class(
            "dzll-background-prepare-server"
        )
        server_block.append(self.background_prepare_server_label)

        server_divider = Gtk.Label(label="•")
        server_divider.set_valign(Gtk.Align.CENTER)
        server_divider.add_css_class("dzll-background-prepare-bullet")
        info.append(server_divider)

        self.background_prepare_detail_label = Gtk.Label(xalign=0.0)
        self.background_prepare_detail_label.set_hexpand(True)
        self.background_prepare_detail_label.set_wrap(False)
        self.background_prepare_detail_label.set_single_line_mode(True)
        self.background_prepare_detail_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.background_prepare_detail_label.add_css_class(
            "dzll-background-prepare-mod"
        )
        info.append(self.background_prepare_detail_label)

        self.background_prepare_queue_divider = Gtk.Label(label="•")
        self.background_prepare_queue_divider.set_valign(Gtk.Align.CENTER)
        self.background_prepare_queue_divider.set_visible(False)
        self.background_prepare_queue_divider.add_css_class(
            "dzll-background-prepare-bullet"
        )
        info.append(self.background_prepare_queue_divider)

        self.background_prepare_queue_label = Gtk.Label(xalign=0.0)
        self.background_prepare_queue_label.set_wrap(False)
        self.background_prepare_queue_label.set_single_line_mode(True)
        self.background_prepare_queue_label.set_visible(False)
        self.background_prepare_queue_label.add_css_class(
            "dzll-background-prepare-queue"
        )
        info.append(self.background_prepare_queue_label)

        self.background_prepare_failed_label = Gtk.Label(xalign=0.0)
        self.background_prepare_failed_label.set_hexpand(True)
        self.background_prepare_failed_label.set_wrap(False)
        self.background_prepare_failed_label.set_single_line_mode(True)
        self.background_prepare_failed_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.background_prepare_failed_label.set_visible(False)
        info.append(self.background_prepare_failed_label)

        self.background_prepare_retry_btn = Gtk.Button(label="Retry Failed")
        self.background_prepare_retry_btn.add_css_class("flat")
        self.background_prepare_retry_btn.set_visible(False)
        self.background_prepare_retry_btn.connect(
            "clicked", self.win._background_prepare_retry_failed_clicked,
        )
        attach_pointer_cursor(self.background_prepare_retry_btn)
        info.append(self.background_prepare_retry_btn)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        right.set_size_request(108, -1)
        right.set_halign(Gtk.Align.END)
        right.add_css_class("dzll-background-prepare-actions")
        info.append(right)

        self.background_prepare_count_label = Gtk.Label(xalign=1.0)
        self.background_prepare_count_label.set_wrap(False)
        self.background_prepare_count_label.set_single_line_mode(True)
        self.background_prepare_count_label.set_width_chars(5)
        self.background_prepare_count_label.set_visible(False)
        self.background_prepare_count_label.add_css_class(
            "dzll-background-prepare-count"
        )
        right.append(self.background_prepare_count_label)

        self.background_prepare_percent_label = Gtk.Label(label="", xalign=1.0)
        self.background_prepare_percent_label.set_size_request(44, -1)
        self.background_prepare_percent_label.set_width_chars(4)
        self.background_prepare_percent_label.set_max_width_chars(4)
        self.background_prepare_percent_label.set_halign(Gtk.Align.FILL)
        self.background_prepare_percent_label.set_valign(Gtk.Align.CENTER)
        self.background_prepare_percent_label.set_single_line_mode(True)
        self.background_prepare_percent_label.add_css_class(
            "dzll-background-prepare-percent"
        )
        right.append(self.background_prepare_percent_label)

        self.background_prepare_right_status_label = Gtk.Label(xalign=1.0)
        self.background_prepare_right_status_label.set_wrap(False)
        self.background_prepare_right_status_label.set_single_line_mode(True)
        self.background_prepare_right_status_label.set_ellipsize(
            Pango.EllipsizeMode.END
        )
        self.background_prepare_right_status_label.set_max_width_chars(16)
        self.background_prepare_right_status_label.set_visible(False)
        self.background_prepare_right_status_label.add_css_class(
            "dzll-background-prepare-right-status"
        )
        right.append(self.background_prepare_right_status_label)

        self.background_prepare_action_btn = Gtk.Button(label="Cancel")
        self.background_prepare_action_btn.add_css_class("flat")
        self.background_prepare_action_btn.set_hexpand(True)
        self.background_prepare_action_btn.set_halign(Gtk.Align.END)
        self.background_prepare_action_btn.connect(
            "clicked", self.win._background_prepare_action_clicked,
        )
        attach_pointer_cursor(self.background_prepare_action_btn)
        right.append(self.background_prepare_action_btn)

        self.background_prepare_status = root
        win = self.win
        win.background_prepare_grid = self.background_prepare_grid
        win.background_prepare_info = self.background_prepare_info
        win.background_prepare_server_label = self.background_prepare_server_label
        win.background_prepare_detail_label = self.background_prepare_detail_label
        win.background_prepare_queue_divider = self.background_prepare_queue_divider
        win.background_prepare_queue_label = self.background_prepare_queue_label
        win.background_prepare_failed_label = self.background_prepare_failed_label
        win.background_prepare_retry_btn = self.background_prepare_retry_btn
        win.background_prepare_count_label = self.background_prepare_count_label
        win.background_prepare_percent_label = self.background_prepare_percent_label
        win.background_prepare_right_status_label = (
            self.background_prepare_right_status_label
        )
        win.background_prepare_action_btn = self.background_prepare_action_btn
        return root

    def set_status_container(
            self, present: bool, *, active: bool = False) -> None:
        root = self.background_prepare_status
        for class_name, enabled in (
            ("dzll-background-prepare-present", present),
            ("dzll-background-prepare-active", present and active),
        ):
            method = getattr(root, "add_css_class" if enabled else "remove_css_class", None)
            if callable(method):
                method(class_name)
        margin = BACKGROUND_PREPARE_ACTIVE_HORIZONTAL_INSET if present else 0
        bottom = BACKGROUND_PREPARE_ACTIVE_BOTTOM_SPACING if present else 0
        for method_name, value in (
            ("set_margin_start", margin),
            ("set_margin_end", margin),
            ("set_margin_bottom", bottom),
        ):
            setter = getattr(root, method_name, None)
            if callable(setter):
                setter(value)

    def set_active_presentation(
            self, active: bool, *, server_name: str = "", queued_count: int = 0) -> None:
        self.set_status_container(active, active=active)

        queue_label = getattr(self, "background_prepare_queue_label", None)
        queue_divider = getattr(self, "background_prepare_queue_divider", None)
        right_status = getattr(self, "background_prepare_right_status_label", None)
        if active:
            self.background_prepare_server_label.set_text(str(server_name or "Server"))
            tooltip = getattr(
                self.background_prepare_server_label, "set_tooltip_text", None,
            )
            if callable(tooltip):
                tooltip(str(server_name or "Server"))
            if queue_label is not None:
                queue_label.set_text(
                    f"Queued Servers: {queued_count}" if queued_count > 0 else ""
                )
                queue_label.set_visible(queued_count > 0)
            if queue_divider is not None:
                queue_divider.set_visible(queued_count > 0)
            if right_status is not None:
                right_status.set_visible(False)
            self.set_action_presentation(True)
        else:
            if queue_label is not None:
                queue_label.set_text("")
                queue_label.set_visible(False)
            if queue_divider is not None:
                queue_divider.set_visible(False)
            if right_status is not None:
                right_status.set_visible(False)

    def set_action_presentation(self, actionable: bool) -> None:
        button = self.background_prepare_action_btn
        button.set_visible(True)
        button.set_opacity(1.0 if actionable else 0.0)
        button.set_sensitive(bool(actionable))

    def present_cancelling(self) -> None:
        self.set_status_container(True)
        self.background_prepare_server_label.set_text(
            "Cancelling background mod preparation…"
        )
        self.background_prepare_detail_label.set_text(
            "Waiting for active Steam cleanup to finish."
        )
        self.background_prepare_queue_label.set_text("")
        self.background_prepare_queue_label.set_visible(False)
        self.background_prepare_queue_divider.set_visible(False)
        self.background_prepare_count_label.set_text("")
        self.background_prepare_count_label.set_visible(False)
        self.background_prepare_retry_btn.set_visible(False)
        self.background_prepare_failed_label.set_visible(False)
        self.set_action_presentation(False)
        self.background_prepare_right_status_label.set_text("Cancelling…")
        self.background_prepare_right_status_label.set_visible(True)
        self.set_progress_presentation(False)
        self.background_prepare_status.set_visible(True)

    def set_progress_presentation(
            self, active: bool, fraction: float = 0.0) -> None:
        root = self.background_prepare_status
        old_class = str(
            getattr(root, "_dzll_background_prepare_progress_class", "") or ""
        )
        if old_class:
            root.remove_css_class(old_class)
            root._dzll_background_prepare_progress_class = ""
        if not active:
            self.background_prepare_percent_label.set_text("")
            return
        percent = max(0, min(100, int(float(fraction) * 100)))
        progress_class = f"dzll-background-prepare-progress-{percent}"
        root.add_css_class(progress_class)
        root._dzll_background_prepare_progress_class = progress_class
