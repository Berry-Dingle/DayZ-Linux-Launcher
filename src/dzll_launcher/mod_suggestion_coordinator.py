"""Lifecycle of the mod-autocomplete catalogue (``ModSuggestionCacheCoordinator``).

The coordinator owns the in-memory suggestion index and the state of its
on-disk cache. That covers:

- the synchronous rebuild during row loading;
- the deferred background restore after batched startup;
- the monthly background refresh;
- the background migration of old cache formats.

Workers run on the update executor. Their results return to the GTK thread
through ``GLib.idle_add`` and are discarded when the catalogue generation
has moved on (``invalidate()``) or the window has shut down.

Startup-performance instrumentation belongs to the window and is injected
as callables.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from gi.repository import GLib

from .atomic_json import _fsync_directory_best_effort
from .mod_suggestion_cache import (
    REFRESH_SECONDS as MOD_SUGGESTION_REFRESH_SECONDS,
    build_staged_suggestion_cache,
    build_staged_suggestion_migration,
    load_suggestion_cache,
    prepare_deferred_suggestions,
    save_suggestion_cache,
    suggestion_cache_path,
)
from .mod_suggestions import build_mod_suggestion_index

RETRY_DELAY_SECONDS = 60.0


class ModSuggestionCacheCoordinator:
    """Owns the suggestion index and its cache lifecycle. All methods run on the GTK thread."""

    def __init__(
        self,
        *,
        cache_path,
        servers: Callable[[], Any],
        parsed_mods_by_key: Callable[[], Any],
        db_fetched_ok: Callable[[], bool],
        provisional_load: Callable[[], bool],
        db_update_inflight: Callable[[], bool],
        sweep_ready: Callable[[], bool],
        is_shut_down: Callable[[], bool],
        submit: Callable[..., Any],
        on_index_replaced: Callable[[], None],
        on_restored: Callable[[], None],
        perf_enabled: Callable[[], bool],
        perf_context: Callable[[], Any],
        log_apply_phase: Callable[..., None],
        log_phase: Callable[..., None],
    ) -> None:
        self._servers = servers
        self._parsed_mods_by_key = parsed_mods_by_key
        self._db_fetched_ok = db_fetched_ok
        self._provisional_load = provisional_load
        self._db_update_inflight = db_update_inflight
        self._sweep_ready = sweep_ready
        self._is_shut_down = is_shut_down
        self._submit = submit
        self._on_index_replaced = on_index_replaced
        self._on_restored = on_restored
        self._perf_enabled = perf_enabled
        self._perf_context = perf_context
        self._log_apply_phase = log_apply_phase
        self._log_phase = log_phase

        self.index = build_mod_suggestion_index(())
        self.cache_path = cache_path
        self.ready = False
        self.expired = False
        self.created_at = 0.0
        self.generation = 0
        self.refresh_inflight = False
        self.refresh_retry_after = 0.0
        self.migration_pending = False
        self.migration_inflight = False
        self.migration_retry_after = 0.0
        self.migration_created_at = 0.0
        self.migration_kind = None
        self.restore_pending = False
        self.restore_allowed = False
        self.restore_inflight = False
        self.restore_generation = 0
        self.restore_retry_after = 0.0

    def _path(self):
        return self.cache_path or suggestion_cache_path()

    # ------------------------------------------------------------- lifecycle operations

    def invalidate(self) -> None:
        """Start a new catalogue generation: in-flight results become stale and no restore is pending."""
        self.generation = int(self.generation or 0) + 1
        self.restore_pending = False
        self.restore_allowed = False
        self.restore_inflight = False

    def mark_restore_pending(self) -> None:
        """Rows were published without a catalogue; restore it once startup allows."""
        self.restore_pending = True
        self.restore_allowed = False

    def drop_pending_restore(self) -> None:
        self.restore_pending = False
        self.restore_allowed = False

    def allow_pending_restore(self) -> bool:
        """Permit a pending deferred restore to be scheduled. Returns whether one was pending."""
        if bool(self.restore_pending):
            self.restore_allowed = True
            return True
        return False

    # ------------------------------------------------------------- synchronous rebuild

    def rebuild_index(self):
        if bool(self.ready):
            if self._perf_enabled():
                context = self._perf_context()
                if isinstance(context, dict):
                    context["suggestion_cache_state"] = "memory_hit"
                self._log_apply_phase(
                    "mod_suggestion_cache_hit", time.monotonic_ns(),
                    timer_scope="nested", parent="global_mod_suggestion_index",
                    source="memory", expired=bool(self.expired),
                )
            self._on_index_replaced()
            return

        cache_path = self._path()
        load_start_ns = time.monotonic_ns() if self._perf_enabled() else 0
        load_stages = {} if self._perf_enabled() else None
        cached = load_suggestion_cache(
            cache_path, perf_report=(load_stages.update if load_stages is not None else None),
        )
        if self._perf_enabled():
            stage_fields = {
                key.removesuffix("_ns") + "_ms": f"{value / 1_000_000:.3f}"
                for key, value in load_stages.items() if key.endswith("_ns")
            }
            stage_fields["bytes_read"] = load_stages.get("bytes_read", 0)
            context = self._perf_context()
            if isinstance(context, dict):
                context["suggestion_cache_state"] = "disk_hit" if cached is not None else "disk_miss"
            self._log_apply_phase(
                "mod_suggestion_cache_load", load_start_ns,
                timer_scope="nested", parent="global_mod_suggestion_index",
                result=("hit" if cached is not None else "miss"),
                format=load_stages.get("format", "unknown"),
                expired=(cached.expired if cached is not None else False),
                entries=(len(cached.index) if cached is not None else 0),
                **stage_fields,
            )
        if cached is not None:
            self.index = cached.index
            self._on_index_replaced()
            self.ready = True
            self.expired = cached.expired
            self.created_at = time.time() - cached.age_seconds
            self.migration_pending = cached.format_version == 1
            self.migration_created_at = cached.created_at
            self.migration_kind = (
                "v1_migration" if cached.format_version == 1 else None
            )
            return

        servers = self._servers()
        parsed_mods_by_key = self._parsed_mods_by_key()
        if parsed_mods_by_key is not None:
            self.index = build_mod_suggestion_index(
                (), parsed_mods_values=(parsed_mods_by_key[key] for key in servers),
            )
        else:
            mods_json_values = []
            try:
                for obj in servers.values():
                    mods_json_values.append(getattr(obj, "mods_json", "") or "")
            except Exception:
                mods_json_values = []
            self.index = build_mod_suggestion_index(mods_json_values)
        self._on_index_replaced()
        if (servers or self._db_fetched_ok()) and not self._provisional_load():
            try:
                save_suggestion_cache(cache_path, self.index)
                self.ready = True
                self.expired = False
                self.created_at = time.time()
                self.migration_pending = False
                self.migration_kind = None
                if self._perf_enabled():
                    self._log_apply_phase(
                        "mod_suggestion_cache_save", load_start_ns,
                        timer_scope="nested", parent="global_mod_suggestion_index",
                        entries=len(self.index),
                    )
            except (OSError, ValueError, TypeError):
                # The in-memory catalogue remains usable; a later load may retry persistence.
                pass

    # ------------------------------------------------------------- deferred restore

    def schedule_deferred_restore(self):
        if self._is_shut_down():
            return False
        if bool(self.ready):
            self.restore_pending = False
            self.restore_allowed = False
            return False
        if (not bool(self.restore_pending) or
                not bool(self.restore_allowed) or
                bool(self.restore_inflight)):
            return False
        servers = self._servers()
        if not servers:
            return False
        if (bool(self.refresh_inflight) or
                bool(self.migration_inflight) or
                self._db_update_inflight() or
                time.monotonic() < float(self.restore_retry_after or 0.0)):
            return False

        schedule_start_ns = time.monotonic_ns()
        generation = int(self.generation or 0)
        perf_context = dict(self._perf_context() or {})
        cache_path = self._path()
        payloads = tuple(
            getattr(obj, "mods_json", "") or "" for obj in servers.values()
        )
        self.restore_inflight = True
        self.restore_generation = generation
        try:
            future = self._submit(
                prepare_deferred_suggestions, cache_path, payloads,
                measure=self._perf_enabled(),
            )
            future.add_done_callback(
                lambda done, gen=generation, started=schedule_start_ns: GLib.idle_add(
                    self._on_deferred_restore_done,
                    gen, started, perf_context, done,
                )
            )
        except Exception:
            if self.restore_generation == generation:
                self.restore_inflight = False
            self.restore_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if self._perf_enabled():
                self._log_apply_phase(
                    "deferred_suggestion_schedule", schedule_start_ns,
                    timer_scope="exclusive", result="submit_failed",
                    suggestion_generation=generation, rows=len(payloads),
                )
            return False
        if self._perf_enabled():
            self._log_apply_phase(
                "deferred_suggestion_schedule", schedule_start_ns,
                timer_scope="exclusive", result="submitted",
                suggestion_generation=generation, rows=len(payloads),
            )
        return False

    def _on_deferred_restore_done(
            self, generation: int, schedule_start_ns: int,
            perf_context: dict, future):
        owns_inflight = (
            bool(self.restore_inflight) and
            generation == int(self.restore_generation or 0)
        )
        if owns_inflight:
            self.restore_inflight = False
        try:
            result = future.result()
        except Exception:
            if (not self._is_shut_down() and
                    generation == int(self.generation or 0)):
                self.restore_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if self._perf_enabled():
                self._log_apply_phase(
                    "deferred_suggestion_worker", schedule_start_ns,
                    timer_scope="worker_duration_unavailable", result="worker_failed",
                    suggestion_generation=generation, **perf_context,
                )
            return False

        stages = dict(getattr(result, "stages", {}) or {})
        if self._perf_enabled():
            stage_fields = {
                key.removesuffix("_ns") + "_ms": f"{value / 1_000_000:.3f}"
                for key, value in stages.items()
                if key.endswith("_ns") and isinstance(value, int)
            }
            self._log_apply_phase(
                "deferred_suggestion_worker", schedule_start_ns,
                duration_ns=int(stages.get("worker_total_ns", 0) or 0),
                timer_scope="worker_duration_reported_on_main",
                execution_thread="worker", result=result.outcome,
                suggestion_generation=generation,
                entries=int(result.index.get("count", 0) or 0),
                bytes_read=int(stages.get("bytes_read", 0) or 0),
                format=stages.get("format", "unknown"),
                **stage_fields, **perf_context,
            )
        if (self._is_shut_down() or
                generation != int(self.generation or 0)):
            if self._perf_enabled():
                self._log_apply_phase(
                    "deferred_suggestion_apply", time.monotonic_ns(),
                    duration_ns=0, timer_scope="main_thread", result="stale",
                    suggestion_generation=generation, **perf_context,
                )
            return False

        apply_start_ns = time.monotonic_ns()
        self.index = result.index
        self._on_index_replaced()
        self.ready = True
        self.expired = bool(result.expired)
        self.created_at = float(result.created_at or 0.0)
        self.migration_pending = result.format_version in (0, 1)
        self.migration_created_at = float(result.created_at or 0.0)
        self.migration_kind = {
            0: "first_save", 1: "v1_migration",
        }.get(result.format_version)
        self.restore_pending = False
        self.restore_allowed = False
        self.restore_retry_after = 0.0
        try:
            self._on_restored()
        except Exception:
            pass
        apply_ns = time.monotonic_ns() - apply_start_ns
        if self._perf_enabled():
            self._log_apply_phase(
                "deferred_suggestion_apply", apply_start_ns,
                duration_ns=apply_ns, timer_scope="main_thread",
                result="committed", source=result.outcome,
                suggestion_generation=generation,
                entries=int(result.index.get("count", 0) or 0),
                **perf_context,
            )
            self._log_apply_phase(
                "autocomplete_ready", time.monotonic_ns(), duration_ns=0,
                timer_scope="marker", suggestion_generation=generation,
                entries=int(result.index.get("count", 0) or 0),
                source=result.outcome,
                **perf_context,
            )
        return False

    # ------------------------------------------------------------- monthly refresh

    def schedule_refresh(self):
        if self._is_shut_down():
            return
        created_at = float(self.created_at or 0.0)
        if created_at and time.time() - created_at >= MOD_SUGGESTION_REFRESH_SECONDS:
            self.expired = True
        if not bool(self.ready) or not bool(self.expired):
            return
        if (bool(self.migration_pending) or
                bool(self.refresh_inflight) or
                bool(self.migration_inflight) or
                self._db_update_inflight()):
            return
        if not self._sweep_ready():
            return
        servers = self._servers()
        if not servers or time.monotonic() < float(self.refresh_retry_after or 0.0):
            return

        generation = int(self.generation or 0)
        payloads = tuple(getattr(obj, "mods_json", "") or "" for obj in servers.values())
        cache_path = self._path()
        start_ns = time.monotonic_ns()
        self.refresh_inflight = True
        try:
            future = self._submit(build_staged_suggestion_cache, cache_path, payloads)
            future.add_done_callback(lambda done, gen=generation, started=start_ns: GLib.idle_add(
                self._on_refresh_done, gen, started, done,
            ))
        except Exception:
            self.refresh_inflight = False
            self.refresh_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            return
        if self._perf_enabled():
            self._log_phase("mod_suggestion_cache_refresh_start", start_ns,
                            generation=generation, rows=len(payloads))

    def _on_refresh_done(self, generation: int, start_ns: int, future):
        self.refresh_inflight = False
        try:
            replacement, staged_path = future.result()
        except Exception:
            self.refresh_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if self._perf_enabled():
                self._log_phase("mod_suggestion_cache_refresh_complete", start_ns,
                                result="worker_failed")
            return False
        if (self._is_shut_down() or
                generation != int(self.generation or 0)):
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            destination = Path(self._path())
            os.replace(staged_path, destination)
            _fsync_directory_best_effort(destination.parent)
        except OSError:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.refresh_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if self._perf_enabled():
                self._log_phase("mod_suggestion_cache_refresh_complete", start_ns,
                                result="publish_failed")
            return False
        self.index = replacement
        self._on_index_replaced()
        self.created_at = time.time()
        self.expired = False
        self.migration_pending = False
        self.migration_kind = None
        if self._perf_enabled():
            self._log_phase("mod_suggestion_cache_refresh_complete", start_ns,
                            result="committed", entries=len(replacement))
        return False

    # ------------------------------------------------------------- format migration

    def schedule_migration(self):
        if (self._is_shut_down() or
                not bool(self.migration_pending) or
                bool(self.migration_inflight) or
                bool(self.refresh_inflight) or
                self._db_update_inflight()):
            return
        if not self._sweep_ready():
            return
        if time.monotonic() < float(self.migration_retry_after or 0.0):
            return
        generation = int(self.generation or 0)
        path = self._path()
        index = self.index
        created_at = float(self.migration_created_at or self.created_at or 0.0)
        kind = self.migration_kind or "v1_migration"
        start_ns = time.monotonic_ns() if self._perf_enabled() else 0
        self.migration_inflight = True
        try:
            future = self._submit(build_staged_suggestion_migration, path, index, created_at)
            future.add_done_callback(lambda done, gen=generation, started=start_ns: GLib.idle_add(
                self._on_migration_done, gen, started, done,
            ))
        except Exception:
            self.migration_inflight = False
            self.migration_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if bool(self.expired):
                self.migration_pending = False
            return
        if self._perf_enabled():
            self._log_phase("mod_suggestion_cache_migration_start", start_ns,
                            generation=generation, entries=len(index), kind=kind)

    def _on_migration_done(self, generation: int, start_ns: int, future):
        self.migration_inflight = False
        kind = self.migration_kind or "v1_migration"
        try:
            staged_path = future.result()
        except Exception:
            self.migration_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if bool(self.expired):
                self.migration_pending = False
            if self._perf_enabled():
                self._log_phase("mod_suggestion_cache_migration_complete", start_ns,
                                result="worker_failed", kind=kind)
            return False
        if (self._is_shut_down() or
                generation != int(self.generation or 0) or
                not bool(self.migration_pending)):
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        try:
            destination = Path(self._path())
            os.replace(staged_path, destination)
            _fsync_directory_best_effort(destination.parent)
        except OSError:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.migration_retry_after = time.monotonic() + RETRY_DELAY_SECONDS
            if bool(self.expired):
                self.migration_pending = False
            if self._perf_enabled():
                self._log_phase("mod_suggestion_cache_migration_complete", start_ns,
                                result="publish_failed", kind=kind)
            return False
        self.migration_pending = False
        self.migration_kind = None
        if self._perf_enabled():
            self._log_phase("mod_suggestion_cache_migration_complete", start_ns,
                            result="committed", entries=len(self.index), kind=kind)
        return False
