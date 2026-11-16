"""Manual "refresh all server status" sweep with sidebar progress.

The controller queries every server once through the shared live executor,
with ``STARTUP_LIVE_REST_WORKERS`` queries in flight at most. Worker results
return to the GTK thread through ``GLib.idle_add``, are buffered, and are
applied in batches. While the sweep runs, the refresh icon spins and a
progress bar appears beneath the sidebar icons. Clicking the button
again stops the sweep.

Every callback carries the sweep generation. A callback from a superseded or
cancelled sweep is ignored.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

from .config import (
    STARTUP_LIVE_FLUSH_MAX,
    STARTUP_LIVE_FLUSH_MS,
    STARTUP_LIVE_REST_TIMEOUT_SECS,
    STARTUP_LIVE_REST_WORKERS,
)
from .live import query_server_live

STATUS_REFRESH_PROGRESS_UPDATE_MS = 125
STATUS_REFRESH_SPINNING_CLASS = "status-refresh-spinning"


@dataclass(frozen=True)
class StatusRefreshWidgets:
    """Sidebar widgets used by the sweep. Any of them may be ``None``."""

    button: Any = None
    progress_box: Any = None
    count_label: Any = None
    progress_bar: Any = None
    icon: Any = None


class StatusRefreshController:
    """Runs one manual status sweep at a time. All methods run on the GTK thread except ``_query_one``."""

    def __init__(
        self,
        *,
        widgets: Callable[[], StatusRefreshWidgets],
        server_keys: Callable[[], list],
        lookup_server: Callable[[Any], Any],
        submit: Callable[..., Any],
        apply_live_results: Callable[..., Any],
        refresh_browser: Callable[[], None],
        scroller: Callable[[], Any],
        model_id: Callable[[], int],
        is_shut_down: Callable[[], bool],
        debug: bool = False,
    ) -> None:
        self._widgets = widgets
        self._server_keys = server_keys
        self._lookup_server = lookup_server
        self._submit = submit
        self._apply_live_results = apply_live_results
        self._refresh_browser = refresh_browser
        self._scroller = scroller
        self._model_id = model_id
        self._is_shut_down = is_shut_down
        self._debug = bool(debug)
        self.generation = 0
        self.running = False
        self.queue = deque()
        self.inflight = 0
        self.buffer = []
        self.flush_id = 0
        self.total = 0
        self.completed = 0
        self.started_at = 0.0
        self.last_log_completed = 0
        self.progress_update_id = 0
        self.idle_tooltip = None
        self.progress_last_rendered = 0
        self.scroll_value = None
        self.cursor_controller_attached = False

    # ------------------------------------------------------------- sidebar UI

    def _apply_cursor_for_state(self, btn=None):
        btn = btn or self._widgets().button
        if btn is None:
            return False
        try:
            btn.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        except Exception:
            pass
        return False

    def _ensure_cursor_controller(self, btn=None):
        btn = btn or self._widgets().button
        if btn is None or bool(self.cursor_controller_attached):
            return False

        motion = Gtk.EventControllerMotion.new()

        def _enter_or_motion(_ctrl, *_args):
            self._apply_cursor_for_state(btn)

        def _leave(_ctrl):
            try:
                btn.set_cursor(None)
            except Exception:
                pass

        motion.connect("enter", _enter_or_motion)
        motion.connect("motion", _enter_or_motion)
        motion.connect("leave", _leave)
        try:
            btn.add_controller(motion)
            self.cursor_controller_attached = True
        except Exception:
            pass
        return False

    def _cancel_progress_update(self, generation: int | None = None):
        if generation is not None and generation != self.generation:
            return False
        source_id = int(self.progress_update_id or 0)
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self.progress_update_id = 0
        return False

    def _set_progress_visible(self, visible: bool, generation: int | None = None):
        if generation is not None and generation != self.generation:
            return False
        widgets = self._widgets()
        box = widgets.progress_box
        count_label = widgets.count_label
        bar = widgets.progress_bar
        if not visible:
            self.progress_last_rendered = 0
            try:
                if count_label is not None:
                    count_label.set_text("")
                    count_label.set_tooltip_text(None)
                if bar is not None:
                    bar.set_fraction(0.0)
                if box is not None:
                    box.set_visible(False)
            except Exception:
                pass
            return False
        try:
            if box is not None:
                box.set_visible(True)
        except Exception:
            pass
        return False

    def _render_progress(self, generation: int, force: bool = False):
        if generation != self.generation:
            return False
        self.progress_update_id = 0
        if self._is_shut_down():
            return False
        if not force and not bool(self.running):
            return False
        total = max(0, int(self.total or 0))
        if total <= 0:
            self._set_progress_visible(False, generation=generation)
            return False
        current = max(0, int(self.completed or 0))
        previous = max(0, int(self.progress_last_rendered or 0))
        completed = min(total, max(previous, current))
        self.progress_last_rendered = completed
        count_text = f"{completed}/{total}"
        tooltip_text = f"Refreshing: {count_text}"
        widgets = self._widgets()
        count_label = widgets.count_label
        bar = widgets.progress_bar
        try:
            if count_label is not None:
                count_label.set_text(count_text)
                count_label.set_tooltip_text(tooltip_text)
            if bar is not None:
                bar.set_fraction(completed / total)
            self._set_progress_visible(True, generation=generation)
        except Exception:
            pass
        return False

    def _schedule_progress_update(self, generation: int):
        if generation != self.generation:
            return False
        if self._is_shut_down():
            return False
        if not bool(self.running):
            return False
        if int(self.progress_update_id or 0):
            return False
        try:
            self.progress_update_id = GLib.timeout_add(
                STATUS_REFRESH_PROGRESS_UPDATE_MS,
                self._render_progress,
                generation,
            )
        except Exception:
            self.progress_update_id = 0
        return False

    def _clear_progress(self, generation: int | None = None):
        if generation is not None and generation != self.generation:
            return False
        self._cancel_progress_update(generation)
        self._set_progress_visible(False, generation=generation)
        return False

    def _set_slot_running(self, running: bool, generation: int | None = None):
        if generation is not None and generation != self.generation:
            return False
        widgets = self._widgets()
        btn = widgets.button
        progress = widgets.progress_box
        icon = widgets.icon
        if btn is None or progress is None:
            return False
        self._ensure_cursor_controller(btn)
        try:
            if running:
                progress.set_visible(True)
                if icon is not None and not icon.has_css_class(STATUS_REFRESH_SPINNING_CLASS):
                    self.idle_tooltip = btn.get_tooltip_text()
                    icon.add_css_class(STATUS_REFRESH_SPINNING_CLASS)
                    btn.set_tooltip_text("Stop refreshing")
                self._apply_cursor_for_state(btn)
            else:
                if icon is not None and icon.has_css_class(STATUS_REFRESH_SPINNING_CLASS):
                    icon.remove_css_class(STATUS_REFRESH_SPINNING_CLASS)
                    btn.set_tooltip_text(self.idle_tooltip)
                self._apply_cursor_for_state(btn)
        except Exception:
            return False
        return False

    # ------------------------------------------------------------- sweep

    def _attempt_finished(self, generation: int, result=None):
        """Count one finished query. This is the only place ``completed`` is incremented."""
        if generation != self.generation:
            return False
        if self._is_shut_down():
            return False
        if not bool(self.running):
            return False
        total = max(0, int(self.total or 0))
        completed = max(0, int(self.completed or 0))
        self.completed = min(total, completed + 1)
        if result:
            self.buffer.append(result)
            if len(self.buffer) >= int(STARTUP_LIVE_FLUSH_MAX):
                self._flush_results(generation)
            elif not int(self.flush_id or 0):
                try:
                    self.flush_id = GLib.timeout_add(
                        int(STARTUP_LIVE_FLUSH_MS),
                        self._flush_results,
                        generation,
                    )
                except Exception:
                    self.flush_id = 0
        self._schedule_progress_update(generation)
        return True

    def toggle(self, *_args):
        """Refresh-button handler: start a sweep, or stop the running one."""
        if bool(self.running):
            self.stop()
        else:
            self.start()

    def start(self, *_args):
        """Begin a sweep of every current server. Ignored while one is running."""
        if bool(self.running):
            return
        keys = self._server_keys()
        if not keys:
            self._clear_progress()
            return

        self.generation = int(self.generation or 0) + 1
        generation = self.generation
        self.running = True
        self.queue = deque(keys)
        self.inflight = 0
        self.buffer = []
        self.total = len(keys)
        self.completed = 0
        self.started_at = time.monotonic()
        self.last_log_completed = 0
        self.progress_last_rendered = 0
        self.scroll_value = None
        try:
            vadj = self._scroller().get_vadjustment()
            if vadj:
                self.scroll_value = float(vadj.get_value())
        except Exception:
            self.scroll_value = None

        tid = int(self.flush_id or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.flush_id = 0

        self._set_slot_running(True, generation=generation)
        self._render_progress(generation, force=True)

        if self._debug:
            print(
                f"[STATUS-REFRESH] begin total={len(keys)} workers={STARTUP_LIVE_REST_WORKERS} "
                f"timeout={STARTUP_LIVE_REST_TIMEOUT_SECS:.2f}s",
                flush=True,
            )
        self._pump(generation)

    def _query_one(self, generation: int, k, ip: str, qport: int):
        """Worker-thread query for one server."""
        obj = self._lookup_server(k)
        try:
            info = query_server_live(
                ip,
                qport,
                timeout=STARTUP_LIVE_REST_TIMEOUT_SECS,
                gport=(obj.gport if obj is not None else qport),
                cycle_id=f"manual-status:{generation}:{k}",
                generation=generation,
                row_id=(id(obj) if obj is not None else "missing"),
                model_id=self._model_id(),
            )
        except Exception as e:
            info = {"ok": False, "err": str(e)}
        return generation, (k, info)

    def _pump(self, generation: int):
        if generation != self.generation:
            return False
        if self._is_shut_down():
            return False

        cap = int(STARTUP_LIVE_REST_WORKERS)
        while int(self.inflight or 0) < cap:
            try:
                k = self.queue.popleft()
            except Exception:
                break
            obj = self._lookup_server(k)
            if not obj:
                self._attempt_finished(generation)
                continue
            try:
                ip = str(obj.ip)
                qport = int(obj.qport)
            except Exception:
                self._attempt_finished(generation)
                continue

            self.inflight = int(self.inflight or 0) + 1
            try:
                fut = self._submit(self._query_one, generation, k, ip, qport)
                fut.add_done_callback(
                    lambda done_fut, gen=generation: GLib.idle_add(
                        self._on_future_done,
                        gen,
                        done_fut,
                    )
                )
            except Exception:
                self.inflight = max(0, int(self.inflight or 0) - 1)
                self._attempt_finished(generation)

        self._maybe_finish(generation)
        return False

    def _on_future_done(self, generation: int, fut):
        if generation != self.generation:
            return False
        self.inflight = max(0, int(self.inflight or 0) - 1)
        try:
            result_generation, result = fut.result()
        except Exception:
            result_generation, result = generation, None
        self._attempt_finished(
            generation,
            result=(result if result_generation == generation else None),
        )

        if self._debug:
            completed = int(self.completed or 0)
            total = int(self.total or 0)
            last_logged = int(self.last_log_completed or 0)
            if completed == total or completed - last_logged >= 250:
                self.last_log_completed = completed
                elapsed = time.monotonic() - float(self.started_at or time.monotonic())
                rate = (completed / elapsed) if elapsed > 0 else 0.0
                print(
                    f"[STATUS-REFRESH] progress completed={completed}/{total} "
                    f"elapsed={elapsed:.1f}s rate={rate:.1f}/s",
                    flush=True,
                )

        self._pump(generation)
        return False

    def _flush_results(self, generation: int):
        if generation != self.generation:
            return False
        self.flush_id = 0
        results = list(self.buffer or ())
        self.buffer = []
        if results:
            self._apply_live_results(results, reason="manual-status-refresh")
        self._maybe_finish(generation)
        return False

    def _maybe_finish(self, generation: int):
        if generation != self.generation:
            return False
        if not bool(self.running):
            return False
        if int(self.inflight or 0) > 0:
            return False
        if len(self.queue or ()) > 0:
            return False
        if self.buffer:
            tid = int(self.flush_id or 0)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                self.flush_id = 0
            self._flush_results(generation)
            return False

        self._complete_sweep(generation, outcome="complete")
        return False

    def _complete_sweep(self, generation: int, outcome: str):
        self._cancel_progress_update(generation)
        self._render_progress(generation, force=True)
        self.running = False
        self._refresh_browser()
        scroll_value = self.scroll_value
        if scroll_value is not None:
            def restore_status_refresh_scroll(value=scroll_value):
                try:
                    vadj = self._scroller().get_vadjustment()
                    if vadj:
                        vadj.set_value(value)
                except Exception:
                    pass
                return False

            GLib.idle_add(restore_status_refresh_scroll)
        self._set_slot_running(False, generation=generation)
        self._clear_progress(generation)
        if self._debug:
            total = int(self.total or 0)
            completed = int(self.completed or 0)
            elapsed = time.monotonic() - float(self.started_at or time.monotonic())
            rate = (completed / elapsed) if elapsed > 0 else 0.0
            print(
                f"[STATUS-REFRESH] {outcome} completed={completed}/{total} "
                f"elapsed={elapsed:.2f}s rate={rate:.1f}/s",
                flush=True,
            )
        return False

    def stop(self, *_args):
        """Stop the running sweep at the user's request.

        Results already received are applied and the browser is refreshed as
        on completion. Unstarted queries are dropped, and queries still in
        flight are ignored when they return.
        """
        if not bool(self.running):
            return
        generation = self.generation
        self.queue = deque()
        tid = int(self.flush_id or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.flush_id = 0
        results = list(self.buffer or ())
        self.buffer = []
        if results:
            self._apply_live_results(results, reason="manual-status-refresh")
        self._complete_sweep(generation, outcome="stopped")
        self.generation = int(self.generation or 0) + 1
        self.inflight = 0

    def cancel(self):
        """Stop the current sweep and invalidate its pending callbacks (used at shutdown)."""
        generation = int(self.generation or 0)
        tid = int(self.flush_id or 0)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.flush_id = 0
        self._clear_progress(generation)
        self.running = False
        self.queue = deque()
        self.buffer = []
        self.inflight = 0
        self._set_slot_running(False, generation=generation)
        self.generation = int(self.generation or 0) + 1
