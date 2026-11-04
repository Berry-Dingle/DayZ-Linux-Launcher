"""Opt-in diagnostics and instrumentation for the server browser.

Every component here is inert unless its environment flag is enabled.
Dependencies on the main window are injected as callables, so the
components do not hold a reference to the window itself.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .ui_row import ServerObject

PERF_STALL_INTERVAL_MS = 250
PERF_STALL_LATE_MS = 250


def model_row_count(*models) -> int:
    """Return the item count of the first model that is present and readable."""
    for model in models:
        try:
            if model is not None:
                return int(model.get_n_items())
        except Exception:
            pass
    return 0


class BrowserPerfDiagnostics:
    """Per-second ``[PERF]`` counters (``DZLL_PERF_LOG=1``).

    The window owns GLib source scheduling and removal. The tick methods
    here are the GLib callbacks: they return ``False`` once the window has
    shut down, and ``True`` otherwise.
    """

    def __init__(
        self,
        *,
        origin_ns: int,
        is_shut_down: Callable[[], bool],
        sort_model: Callable[[], Any],
        filter_model: Callable[[], Any],
    ) -> None:
        self._origin_ns = origin_ns
        self._is_shut_down = is_shut_down
        self._sort_model = sort_model
        self._filter_model = filter_model
        self.stall_last = time.perf_counter()
        self.row_binds = 0
        self.sort_calls = 0
        self.sort_total = 0.0
        self.sort_max = 0.0
        self.scroll_events = 0
        self.scroll_total = 0.0
        self.scroll_max = 0.0

    def note_sort(self, duration: float) -> None:
        self.sort_calls += 1
        self.sort_total += duration
        if duration > self.sort_max:
            self.sort_max = duration

    def note_scroll(self, duration: float) -> None:
        self.scroll_events += 1
        self.scroll_total += duration
        if duration > self.scroll_max:
            self.scroll_max = duration

    def main_loop_stall_tick(self):
        if self._is_shut_down():
            return False
        now = time.perf_counter()
        elapsed_ms = (now - self.stall_last) * 1000.0
        self.stall_last = now
        if elapsed_ms > float(PERF_STALL_INTERVAL_MS + PERF_STALL_LATE_MS):
            startup_ms = (
                (time.monotonic_ns() - self._origin_ns) / 1_000_000
                if self._origin_ns else 0.0
            )
            print(f"[PERF] main loop stall: {elapsed_ms:.0f}ms startup_ms={startup_ms:.1f}", flush=True)
        return True

    def row_bind_tick(self):
        if self._is_shut_down():
            return False
        count = self.row_binds
        self.row_binds = 0
        if count > 0:
            print(f"[PERF] row binds/sec={count}", flush=True)
        return True

    def sort_tick(self):
        if self._is_shut_down():
            return False
        count, total, max_one = self.sort_calls, self.sort_total, self.sort_max
        self.sort_calls = 0
        self.sort_total = 0.0
        self.sort_max = 0.0
        if count > 0:
            print(f"[PERF] sort calls/sec={count} total={total * 1000.0:.1f}ms max={max_one * 1000.0:.1f}ms", flush=True)
        return True

    def scroll_tick(self):
        if self._is_shut_down():
            return False
        count, total, max_one = self.scroll_events, self.scroll_total, self.scroll_max
        self.scroll_events = 0
        self.scroll_total = 0.0
        self.scroll_max = 0.0
        if count > 0:
            print(f"[PERF] scroll events/sec={count} total={total * 1000.0:.1f}ms max={max_one * 1000.0:.1f}ms", flush=True)
        return True

    @staticmethod
    def model_value(model, name: str):
        if model is None:
            return None
        getter = f"get_{name}"
        try:
            fn = getattr(model, getter, None)
            if callable(fn):
                return fn()
        except Exception:
            pass
        try:
            return model.get_property(name)
        except Exception:
            return None

    def model_pending_tick(self):
        if self._is_shut_down():
            return False
        sort_model = self._sort_model()
        filter_model = self._filter_model()
        values = {
            "sort_pending": self.model_value(sort_model, "pending"),
            "filter_pending": self.model_value(filter_model, "pending"),
            "sort_incremental": self.model_value(sort_model, "incremental"),
            "filter_incremental": self.model_value(filter_model, "incremental"),
        }
        if any(v not in (None, 0, False) for v in values.values()):
            parts = [f"{k}={v}" for k, v in values.items() if v is not None]
            print(f"[PERF] model state: {' '.join(parts)}", flush=True)
        return True


class SortDebugRecorder:
    """``[SORT]`` instrumentation for one sort application (``DZLL_DEBUG_SORT=1``)."""

    def __init__(self, *, enabled: bool, row_count: Callable[[], int]) -> None:
        self.enabled = bool(enabled)
        self._row_count = row_count
        self.stats: dict | None = None

    def _active_stats(self) -> dict | None:
        stats = self.stats
        if not isinstance(stats, dict) or not stats.get("active"):
            return None
        return stats

    def begin(self, key: str, direction: str) -> None:
        if not self.enabled:
            return
        rows = self._row_count()
        self.stats = {
            "active": True,
            "key": str(key or ""),
            "direction": str(direction or ""),
            "rows": int(rows or 0),
            "started_at": time.perf_counter(),
            "calls": 0,
            "total_time": 0.0,
            "max_call": 0.0,
            "cached_numeric": 0,
            "parsed_text": 0,
            "offline": 0,
            "live_ping_updates": 0,
            "row_notifications": 0,
            "notify_props": {},
            "row_binds": 0,
            "bind_kinds": {},
            "model_events": {},
            "scroll_to_skipped": 0,
            "visible_sort": "",
            "key_builds": 0,
            "key_time": 0.0,
        }
        print(f"[SORT] requested column={key} direction={direction} rows={int(rows or 0)}", flush=True)

    def note_notify(self, prop_name: str) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        name = str(prop_name or "unknown")
        props = stats.setdefault("notify_props", {})
        props[name] = int(props.get(name, 0) or 0) + 1
        stats["row_notifications"] = int(stats.get("row_notifications", 0) or 0) + 1

    def attach_notify_probe(self, obj) -> None:
        if not self.enabled or not isinstance(obj, ServerObject):
            return
        if bool(getattr(obj, "_dzll_sort_debug_notify_attached", False)):
            return
        try:
            def on_notify(_obj, pspec):
                try:
                    name = pspec.name
                except Exception:
                    name = "unknown"
                self.note_notify(name)

            obj.connect("notify", on_notify)
            obj._dzll_sort_debug_notify_attached = True
        except Exception:
            pass

    def note_bind(self, kind: str) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        stats["row_binds"] = int(stats.get("row_binds", 0) or 0) + 1
        kinds = stats.setdefault("bind_kinds", {})
        name = str(kind or "unknown")
        kinds[name] = int(kinds.get(name, 0) or 0) + 1

    def note_model_event(self, event: str) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        events = stats.setdefault("model_events", {})
        name = str(event or "unknown")
        events[name] = int(events.get(name, 0) or 0) + 1

    def note_scroll_to_skipped(self, pos: int, count: int, reason: str) -> None:
        if not self.enabled:
            return
        stats = self.stats
        if isinstance(stats, dict):
            stats["scroll_to_skipped"] = int(stats.get("scroll_to_skipped", 0) or 0) + 1
        print(f"[SORT] skip_scroll_to pos={int(pos)} count={int(count)} reason={reason}", flush=True)

    def note_key_build(self, duration: float) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        stats["visible_sort"] = "key_tuple"
        stats["key_builds"] = int(stats.get("key_builds", 0) or 0) + 1
        stats["key_time"] = float(stats.get("key_time", 0.0) or 0.0) + float(duration or 0.0)

    def note_compare(self, key: str, duration: float, a, b) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        if str(stats.get("key") or "") != str(key or ""):
            return
        stats["calls"] = int(stats.get("calls", 0) or 0) + 1
        stats["total_time"] = float(stats.get("total_time", 0.0) or 0.0) + float(duration or 0.0)
        if duration > float(stats.get("max_call", 0.0) or 0.0):
            stats["max_call"] = float(duration or 0.0)
        if key == "ping":
            stats["cached_numeric"] = int(stats.get("cached_numeric", 0) or 0) + 1
            try:
                if int(getattr(a, "ping", -1)) < 0 or int(getattr(b, "ping", -1)) < 0:
                    stats["offline"] = int(stats.get("offline", 0) or 0) + 1
            except Exception:
                pass
        elif key == "players":
            stats["cached_numeric"] = int(stats.get("cached_numeric", 0) or 0) + 1

    def note_live_update(self, reason: str, row_notifications: int = 0, ping_updates: int = 0) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        if reason == "browser-live":
            stats["live_ping_updates"] = int(stats.get("live_ping_updates", 0) or 0) + int(ping_updates or 0)

    def finish(self) -> None:
        if not self.enabled:
            return
        stats = self._active_stats()
        if stats is None:
            return
        stats["active"] = False
        key = str(stats.get("key") or "")
        calls = int(stats.get("calls", 0) or 0)
        total_time = float(stats.get("total_time", 0.0) or 0.0)
        max_call = float(stats.get("max_call", 0.0) or 0.0)
        elapsed = time.perf_counter() - float(stats.get("started_at", time.perf_counter()) or time.perf_counter())
        print(
            f"[SORT] {key} calls={calls} total_key_time={total_time:.3f}s "
            f"max_call={max_call:.4f}s cached_numeric={int(stats.get('cached_numeric', 0) or 0)} "
            f"parsed_text={int(stats.get('parsed_text', 0) or 0)} offline={int(stats.get('offline', 0) or 0)}",
            flush=True,
        )
        props = dict(stats.get("notify_props", {}) or {})
        prop_text = " ".join(f"{name}={props[name]}" for name in sorted(props)) or "none=0"
        events = dict(stats.get("model_events", {}) or {})
        event_text = " ".join(f"{name}={events[name]}" for name in sorted(events)) or "none=0"
        bind_kinds = dict(stats.get("bind_kinds", {}) or {})
        bind_text = " ".join(f"{name}={bind_kinds[name]}" for name in sorted(bind_kinds)) or "none=0"
        visible_sort = str(stats.get("visible_sort") or "unknown")
        print(f"[SORT] notify_props {prop_text}", flush=True)
        print(
            f"[SORT] visible_sort={visible_sort} key_builds={int(stats.get('key_builds', 0) or 0)} "
            f"key_time={float(stats.get('key_time', 0.0) or 0.0):.3f}s "
            f"visible_comparator_calls=0 total_comparator_calls={calls}",
            flush=True,
        )
        print(
            f"[SORT] row_binds_during_sort={int(stats.get('row_binds', 0) or 0)} "
            f"bind_kinds {bind_text} scroll_to_skipped={int(stats.get('scroll_to_skipped', 0) or 0)}",
            flush=True,
        )
        print(f"[SORT] model_events {event_text}", flush=True)
        print(
            f"[SORT] apply_elapsed={elapsed:.3f}s "
            f"live_ping_updates_during_sort={int(stats.get('live_ping_updates', 0) or 0)} "
            f"row_notifications={int(stats.get('row_notifications', 0) or 0)}",
            flush=True,
        )


class FilterTimingLogger:
    """``[filter-timing]`` lines for filter and sort actions (``DZLL_DEBUG_FILTER_TIMING=1``).

    ``current_ctx`` carries an outer timing context across nested calls,
    for example from a sidebar setting change into the filter refresh it triggers.
    """

    _CTX_FIELDS = (
        ("callback_entry_ms", "callback_entry"),
        ("on_filter_ms", "on_filter"),
        ("build_state_ms", "build_state"),
        ("apply_ms", "apply"),
        ("sort_set_ms", "set_sort"),
        ("sorter_invalidation_ms", "sorter_invalidation"),
        ("settings_save_ms", "settings_save"),
        ("runtime_effects_ms", "runtime_effects"),
    )

    def __init__(self, *, enabled: bool, sort_description: Callable[[], str]) -> None:
        self.enabled = bool(enabled)
        self._sort_description = sort_description
        self.current_ctx: dict | None = None

    def context(self, action: str, total_start: float | None = None) -> dict | None:
        if not self.enabled:
            return None
        return {
            "action": str(action or "unknown"),
            "total_start": float(total_start if total_start is not None else time.perf_counter()),
        }

    def log(self, ctx: dict | None) -> None:
        if not self.enabled or not isinstance(ctx, dict):
            return
        now = time.perf_counter()
        total_start = float(ctx.get("total_start", now) or now)
        rebuild = ctx.get("rebuild") if isinstance(ctx.get("rebuild"), dict) else {}
        parts = [
            "[filter-timing]",
            f"action={ctx.get('action', 'unknown')}",
            f"total={(now - total_start) * 1000.0:.1f}ms",
        ]
        for key, label in self._CTX_FIELDS:
            if key in ctx:
                parts.append(f"{label}={float(ctx.get(key, 0.0) or 0.0):.1f}ms")
        if "gtk_filter" in ctx:
            value = str(ctx.get("gtk_filter") or "")
            if "gtk_filter_ms" in ctx:
                value = f"{value}:{float(ctx.get('gtk_filter_ms', 0.0) or 0.0):.1f}ms"
            parts.append(f"gtk_filter={value}")
        if rebuild:
            scan_ms = float(rebuild.get("scan_ms", 0.0) or 0.0)
            filter_func_ms = float(rebuild.get("filter_func_ms", 0.0) or 0.0)
            iter_overhead_ms = max(0.0, scan_ms - filter_func_ms)
            parts.extend(
                (
                    f"rebuild={float(rebuild.get('total_ms', 0.0) or 0.0):.1f}ms",
                    f"scan={scan_ms:.1f}ms",
                    f"filter_func={filter_func_ms:.1f}ms",
                    f"iter_overhead={iter_overhead_ms:.1f}ms",
                    f"sort_ms={float(rebuild.get('sort_ms', 0.0) or 0.0):.1f}ms",
                    f"sort_skipped={str(bool(rebuild.get('sort_skipped', False))).lower()}",
                    f"splice={float(rebuild.get('splice_ms', 0.0) or 0.0):.1f}ms",
                    f"old_count_ms={float(rebuild.get('store_old_count_ms', 0.0) or 0.0):.1f}ms",
                    f"prepare={float(rebuild.get('store_prepare_ms', 0.0) or 0.0):.1f}ms",
                    f"splice_call={float(rebuild.get('store_splice_call_ms', 0.0) or 0.0):.1f}ms",
                    f"post_splice={float(rebuild.get('store_post_splice_ms', 0.0) or 0.0):.1f}ms",
                    f"update={rebuild.get('store_update', 'unknown')}",
                    f"replace_all={str(bool(rebuild.get('replace_all', False))).lower()}",
                    f"rows={int(rebuild.get('source_rows', 0) or 0)}",
                    f"matched={int(rebuild.get('matched_rows', 0) or 0)}",
                    f"rejected={int(rebuild.get('rejected_rows', 0) or 0)}",
                    f"spliced={int(rebuild.get('spliced_rows', 0) or 0)}",
                    f"old={int(rebuild.get('old_count', 0) or 0)}",
                    f"new={int(rebuild.get('new_count', 0) or 0)}",
                    f"delta={int(rebuild.get('delta_count', 0) or 0)}",
                    f"removed={int(rebuild.get('removed_count', 0) or 0)}",
                    f"inserted={int(rebuild.get('inserted_count', 0) or 0)}",
                )
            )
        if ctx.get("scroll_queued"):
            parts.append("scroll=queued")
        parts.append(f"sort={self._sort_description()}")
        print(" ".join(parts), flush=True)


def _format_reorder_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_format_reorder_value(v) for v in value) + "]"
    text = str(value)
    if not text:
        return "-"
    return text.replace("\n", "\\n").replace(" ", "_")


class BrowserReorderDebug:
    """``[browser-reorder]`` tracing of visible-store changes (``DZLL_DEBUG_BROWSER_REORDER=1``)."""

    def __init__(
        self,
        *,
        enabled: bool,
        store: Callable[[], Any],
        scroller: Callable[[], Any],
        filter_state: Callable[[], Any],
        ping_cutoff: Callable[[], Any],
        key_for_obj: Callable[[Any], str],
        row_height_px: int,
    ) -> None:
        self.enabled = bool(enabled)
        self._store = store
        self._scroller = scroller
        self._filter_state = filter_state
        self._ping_cutoff = ping_cutoff
        self._key_for_obj = key_for_obj
        self._row_height_px = row_height_px

    def log(self, message: str, **fields) -> None:
        if not self.enabled:
            return
        parts = [f"[browser-reorder] {message}"]
        parts.extend(f"{key}={_format_reorder_value(value)}" for key, value in fields.items())
        print(" ".join(parts), flush=True)

    def visible_keys(self, limit: int = 5) -> list[str]:
        if not self.enabled:
            return []
        store = self._store()
        if store is None:
            return []
        try:
            count = min(max(0, int(limit)), int(store.get_n_items()))
        except Exception:
            count = 0
        keys = []
        for i in range(count):
            try:
                keys.append(self._key_for_obj(store.get_item(i)))
            except Exception:
                keys.append("-")
        return keys

    def scroll_key(self) -> str:
        if not self.enabled:
            return "-"
        store = self._store()
        if store is None:
            return "-"
        try:
            vadj = self._scroller().get_vadjustment()
            if not vadj:
                return "-"
            row_h = max(1, int(self._row_height_px))
            index = int(max(0.0, float(vadj.get_value())) // row_h)
            count = int(store.get_n_items())
            if index >= count:
                index = max(0, count - 1)
            if index < 0 or count <= 0:
                return "-"
            return self._key_for_obj(store.get_item(index))
        except Exception:
            return "-"

    def live_filter_state(self) -> dict:
        state = self._filter_state()
        if not isinstance(state, dict):
            state = {}
        try:
            ping_cutoff = int(self._ping_cutoff() or 0)
        except Exception:
            ping_cutoff = 0
        try:
            max_players_cutoff = int(state.get("max_players_cutoff", 0) or 0)
        except Exception:
            max_players_cutoff = 0
        return {
            "online_only": bool(state.get("online_only", False)),
            "no_password": bool(state.get("no_password", False)),
            "ping_cutoff": ping_cutoff,
            "ping_cutoff_active": ping_cutoff > 0,
            "max_players_cutoff": max_players_cutoff,
            "max_players_active": max_players_cutoff > 0,
        }
