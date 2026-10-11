# PR16-01 — GTK startup phase measurement

Date: 2026-10-11  
Status: instrumentation prepared and verified offline; PR16-01 remains **IN PROGRESS**. No runtime measurements were made in this task.

## Scope

This task inspected and extended the current opt-in startup instrumentation around the remaining approximately 1.68-second GTK database application callback. The implementation remains synchronous and GTK-owned. No worker, batching, cache, A2S, filter, sort, favourite, Previously Joined, mod-search, overlay or spinner behavior changed.

No DZLL or Steam process was launched. No network operation, dependency installation, Forge edit, PR16 review-clone inspection, Git mutation or `MEMORY.md` edit occurred. Existing dirty and untracked source, tests, reports, package outputs and release artifacts were preserved.

Files changed for this task:

| File | Change |
|---|---|
| `src/dzll_launcher/window.py` | Add one DB-application context, missing aggregate phase boundaries, explicit timer relationships and a one-shot next-frame-clock marker, all gated by `DZLL_PERF_LOG=1`. |
| `tests/test_startup_perf_instrumentation.py` | Extend observational-flow tests for phase identity, cache context, parent/overlap labels, replacement load IDs and next-frame semantics. |
| `devReports/PR16_01_GTK_PHASE_MEASUREMENT_REPORT.md` | This report and maintainer benchmark procedure. |

The existing logger in `startup_perf.py` is unchanged. It still emits one fail-safe line per phase with monotonic `duration_ms`, cumulative `startup_ms`, thread name and aggregate fields (`startup_perf.py:7-24`).

## Current GTK application path

The database worker still performs update checking, fetch and SQLite read, then posts the result through `GLib.idle_add`. The GTK callback calls `_apply_db_rows()`, which validates endpoints, prepares the map model, loads rows into the source `Gio.ListStore`, builds or restores the global suggestion catalogue, snapshots sort keys, filters/sorts and replaces the visible store, then performs startup coordination and starts the A2S sweep (`window.py:5496-5638,5809-5950`). `ServerObject` construction, GObject property assignment and both `Gio.ListStore` mutations remain on GTK.

The application can perform more than one database application in one startup: the 30-second watchdog may provisionally apply the cached DB and a later successful worker result may replace it. Each `_apply_db_rows()` call now increments `load_id`, so those applications cannot be confused (`window.py:5809-5826`). Every DB-application phase receives the same context:

- `load_id`
- `input_rows` and, after validation, `valid_rows`
- `fetched_ok` and `provisional`
- `server_cache_state`: `disk_load` or `memory_reuse`
- `suggestion_cache_state`: initially `disk_lookup_pending` or `memory_hit`, then `disk_hit`/`disk_miss` after the load result

The helper copies this context into each phase line (`window.py:318-328`). A later callback captures its context when necessary, so a replacement load cannot relabel the next-frame marker from the preceding application (`window.py:331-353`). Existing worker timers remain outside a DB application and therefore do not receive `load_id`; they measure fetch/read work before GTK application. `db_gtk_callback_entry` is also a callback arrival marker rather than an application phase. The enclosing `db_gtk_completion` line is emitted after `_apply_db_rows()` and carries the completed application's `load_id` (`window.py:5496-5527`).

## Phase coverage after this change

| Required category | Timer coverage | Status and interpretation |
|---|---|---|
| Endpoint validation | `endpoint_validation` | Existing exclusive timer, now identified by load context. |
| Map preparation | `map_choice_build`, `map_model_update`; enclosing `map_preparation` | Existing parent was insufficient to separate pure aggregation from GTK string-model replacement. Two aggregate child timers were added without changing the try/fallback behavior (`window.py:5849-5888`). Do not add the parent to its children. |
| Per-server cache loading/restoration | `server_mod_cache_apply` and its existing `cache_*`, fingerprint, record-restore and miss-rebuild fields | Existing detail retained. Its start now precedes disk load. It is labeled `timer_scope=overlap overlaps=row_loop_total`: disk load occurs before the row loop, while fingerprint/lookup/miss work occurs inside it (`window.py:5955-5974,6042-6066,6136-6163`). Do not add the complete cache timer to `row_loop_total`. |
| Scalar conversion | `row_scalar_conversion` | New exclusive aggregate for endpoint/scalar/map/time/favourite/played conversion across accepted rows (`window.py:6099-6204`). It is the sum of interleaved per-row segments, not one contiguous pause. |
| Row sidecar attachment | `row_sidecar_attachment`; `source_sidecar_clear` | New exclusive aggregates for Python search/filter/mod-index fields and endpoint/live maps, plus clearing old endpoint sidecars (`window.py:6074-6082,6234-6252`). The attachment aggregate is interleaved with object append. |
| `ServerObject` construction/property assignment | `server_object_construction` | Existing exclusive aggregate retained. It times the constructor and property writes only. |
| Source `Gio.ListStore` append/clear | `source_store_insertion`, `source_store_clear` | Existing append aggregate retained; a missing exclusive clear timer was added around the source splice (`window.py:6067-6074,6242-6247`). Deferred GTK notifications/rendering are not charged to these calls. |
| Whole row loop | `row_loop_total` | Existing enclosing phase retained with an explicit duration captured before diagnostic lines are emitted. Its children are `row_scalar_conversion`, `row_sidecar_attachment`, `row_conversion` residual, `mod_preview_index`, `server_object_construction` and `source_store_insertion` (`window.py:6254-6291`). `row_conversion` is now explicitly `exclusive_residual`; it contains remaining loop work including cache fingerprint/lookup, control flow and timing overhead. |
| Global suggestion cache | `global_mod_suggestion_index`; nested `mod_suggestion_cache_hit`, `mod_suggestion_cache_load`, and cold `mod_suggestion_cache_save` | Existing detailed load/restoration fields retained and labeled nested. The global parent includes its nested cache operation or cold build/save (`window.py:6293-6306,8808-8884`). Do not add the nested load to the parent. |
| Sort-key snapshot | `sort_key_snapshot` | Existing exclusive timer retained (`window.py:6307-6313`). |
| Initial filter, sort and visible splice | `initial_filter_scan`, `initial_sort`, `initial_visible_store_splice`; enclosing `initial_visible_store_rebuild` | Existing timers retained and labeled exclusive children plus parent (`window.py:6460-6779`). Do not add the parent to the children. The splice timer ends when the model call returns; later binding/layout/drawing is outside it. |
| Post-load coordination and A2S submission | `a2s_initial_submission`; enclosing `post_load_startup_coordination` | New parent spans cover cache-write timer scheduling, Companion restore, updating state, key grouping, A2S state/setup and initial executor submissions. Cached-failure reveal is separately labeled and includes the final sort and overlay call (`window.py:5900-5949`). A2S network work remains asynchronous and outside the DB callback. |
| DB application total | `db_gtk_completion` | Existing enclosing callback timer, now labeled `timer_scope=parent includes=all_db_gtk_apply_phases` and correlated with `load_id`. This is the measured approximately 1.68-second target, including instrumentation overhead when enabled. |
| First post-publication frame opportunity | `initial_next_frame_tick` | New one-shot GTK tick callback scheduled after the initial visible-store rebuild. It is labeled `timer_scope=latency_marker meaning=next_frame_clock_tick_not_draw_complete` (`window.py:331-353,6775-6781`). It proves that the frame clock reached its next tick after GTK regained control; it does not prove pixels were presented. |
| Overlay dismissal | `startup_overlay_dismissal` | Existing timer retained, now correlated to the load. It measures only the coordinator/UI call at reveal; its `startup_ms` locates overlay dismissal after A2S warmup (`window.py:5454-5477`). It is not first-frame timing. |

No per-row line is emitted. The additional per-row work when profiling is enabled is limited to monotonic clock reads accumulated into two counters. With `DZLL_PERF_LOG` unset, the new context, counters, tick callback and lines are inactive.

## Timer hierarchy and double-counting rules

`timer_scope` makes the relationship machine-visible:

- `exclusive` and `exclusive_aggregate` values may be compared as component costs. An aggregate can contain disjoint pieces gathered across the row loop.
- `exclusive_residual` is the parent duration after the named exclusive row-loop children are subtracted.
- `parent` contains the phases named by `includes`; never add a parent to its children.
- `nested` is already included in the named `parent`.
- `overlap` crosses another phase boundary. `server_mod_cache_apply` includes disk-load work before `row_loop_total` and fingerprint/restore/miss work inside it, so it must never be used in an additive total.
- `marker` and `latency_marker` locate lifecycle events; they are not CPU components.

For a top-level view of the uninterrupted GTK callback, compare these sequential spans for one `load_id`: `endpoint_validation`, `map_preparation`, `source_store_clear`, `source_sidecar_clear`, `row_loop_total`, `global_mod_suggestion_index`, `sort_key_snapshot`, `initial_visible_store_rebuild`, and `post_load_startup_coordination`. Compare their durations with `db_gtk_completion`, but do not expect an exact sum: small uninstrumented statements, phase-line emission, GObject/model notification work occurring inside calls and timer boundaries account for differences.

The best indicator of the longest uninterrupted architectural block is the largest top-level synchronous span, usually one of `row_loop_total`, `global_mod_suggestion_index`, `initial_visible_store_rebuild` or `post_load_startup_coordination`. The row scalar/object/append values explain `row_loop_total`, but each is accumulated from interleaved per-row operations and is not itself one continuous block. The initial scan/sort/splice children similarly explain their enclosing rebuild.

## Focused regression evidence

The tests use the repository's pre-collection isolated HOME/XDG setup and never touch the real user profile.

- `python -m pytest -q tests/test_startup_perf_instrumentation.py` — **15 passed**, one existing PyGObject deprecation warning.
- `python -m pytest -q tests/test_startup_perf_instrumentation.py tests/test_browser_server_ingestion_validation.py tests/test_mod_suggestion_cache.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py tests/test_pin_favourites_filter_priority.py` — **206 passed, 13 skipped**, one existing PyGObject deprecation warning. The skips require a display and are not executed coverage.
- `python -m py_compile src/dzll_launcher/window.py src/dzll_launcher/startup_perf.py tests/test_startup_perf_instrumentation.py` — passed.
- `git --no-pager diff --check` — passed.

Tests cover enabled/disabled observational flow, cache-stage reuse, phase context, map/A2S ordering, incrementing replacement `load_id`, exact source rows, row timer scope, filter/sort output and the fact that the next-frame marker snapshots its originating load and says it is not draw completion (`tests/test_startup_perf_instrumentation.py:41-275`). No runtime benchmark is claimed.

## Maintainer runtime benchmark procedure

### 1. Freeze the comparison conditions

Use the same current source checkout, 11,980-row DB, settings, display/session, GTK/Python build and warm per-server/global cache files for all three runs. Record database and cache file sizes/hashes before run 1 and after run 3. Close every existing DZLL process first and leave the machine idle. Do not enable `DZLL_DEBUG_FILTER_TIMING`, sort diagnostics, A2S diagnostics or profilers alongside `DZLL_PERF_LOG`; their extra work would change the callback.

For a valid warm run, require:

- the same `input_rows` and `valid_rows` on all compared `load_id`s;
- `server_mod_cache_apply` showing the expected approximately 99.77–100% hit rate and comparable file/entry counts;
- `suggestion_cache_state=disk_hit` on a fresh-process load, or the same state in every compared run;
- no cache migration/write, database replacement or unrelated heavy process overlapping the callback;
- one ordinary final application. If the watchdog produces `provisional=True` followed by a replacement `load_id`, analyze those paths separately and do not mix them with normal single-load runs.

### 2. Capture three separate warm launches

From the repository root, run one launch at a time and close DZLL normally only after the desired startup lines, next-frame marker, overlay dismissal and A2S sweep completion have appeared:

```bash
PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-gtk-phase-run1.log
PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-gtk-phase-run2.log
PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-gtk-phase-run3.log
```

These commands are for the maintainer's later authorized runtime session. They were not run during this task. If a launch fetches a changed DB, discard it or restart the three-run set after caches are warm for the new identical snapshot.

Extract the chosen application and stall lines, normally `load_id=1` for a single final load:

```bash
rg '(^\[STARTUP-PERF\].*load_id=1)|(^\[PERF\] main loop stall)' /tmp/dzll-pr16-gtk-phase-run1.log
```

Repeat for run 2 and run 3. Preserve complete logs; a line subset is insufficient to diagnose provisional replacement or cache-state drift.

### 3. Identify the longest uninterrupted GTK phase

For each run, first record `db_gtk_completion duration_ms` and the largest `[PERF] main loop stall`. Then make one row per top-level phase listed in the timer-hierarchy section. Confirm `thread=MainThread` on GTK phases and the same `load_id`, row counts and cache states.

Find the largest top-level child duration. Use its children only for attribution:

- If `row_loop_total` dominates, inspect the scalar, sidecar, residual, mod, constructor and source-insertion aggregates. Do not add `server_mod_cache_apply`; use its subfields to identify the cache share already overlapping the loop, plus its pre-loop disk-load share.
- If `global_mod_suggestion_index` dominates, inspect the nested load stages (`file_stat_read`, JSON decode, SHA-256/integrity, index/entry/prefix/exact restore). Do not add them to the parent.
- If `initial_visible_store_rebuild` dominates, inspect filter scan, Python sort and visible splice. Deferred GTK binding/layout is not proven by the splice value.
- If `post_load_startup_coordination` dominates, inspect `a2s_initial_submission`; network query duration is not inside the GTK callback.

Compare the median and full range across the three runs for the callback, stall and dominant phase. A result is credible only if the phase remains dominant and its spread is materially smaller than the proposed improvement. Do not subtract independently measured historical values or combine phases from different runs.

### 4. Separate model publication, next frame and overlay dismissal

Use these timestamps in order:

1. `initial_visible_store_splice startup_ms`: the visible model replacement call returned.
2. `initial_visible_store_rebuild startup_ms`: synchronous scan/sort/publication completed.
3. `initial_next_frame_tick startup_ms`: GTK reached the next frame-clock tick after publication and regained an opportunity to render.
4. `startup_overlay_dismissal startup_ms`: the A2S reveal gate completed and the overlay coordinator ran.

The next-frame tick is not proof of presentation. To establish the actual first rendered nonempty list and spinner continuity, make an external 60 fps screen recording or equivalent frame observation and align it with log `startup_ms`. Record the first video frame containing bound server rows, any spinner freeze interval and the first responsive input. Do not label `initial_next_frame_tick` as first pixels or `startup_overlay_dismissal` as first render. The overlay can remain visible over an already published/renderable list while A2S warmup continues.

### 5. Decision gate

Retain PR16-01 as IN PROGRESS until the three-run data identifies a repeatable dominant synchronous block. Proceed to a later architecture experiment only if reducing or yielding that block can plausibly improve the longest GTK stall and visible animation by more than run-to-run variation. If no child is large or stable enough, keep the current approximately 1.8-second baseline rather than add lifecycle complexity. Do not infer improvement from total startup wall-clock alone.

## Verification boundary

This task prepared measurement only. It did not change startup execution order, cache ownership, model contents, filters, sorting, A2S thresholds or scheduling. It did not generate new runtime numbers. Only allowed read-only Git inspection commands were used; no Git state/history mutation occurred.
