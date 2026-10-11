# PR16-01 — Experimental GTK startup batching

Date: 2026-10-11  
Status: opt-in experiment implemented and verified offline; PR16-01 remains **IN PROGRESS** pending controlled runtime comparison.

## Scope and evidence

This task implemented a narrow GTK idle-batching experiment for the measured startup row loop. It did not launch DZLL or Steam, access the network, install dependencies, change A2S thresholds, inspect the PR16 review clone, modify Forge or `MEMORY.md`, or mutate Git state/history. Existing dirty and untracked files were preserved.

The supplied 12,047-row warm baseline is:

- GTK DB callback: 1681.82 / 1684.10 / 1681.23 ms.
- Main-loop stall: 1806 / 1693 / 1738 ms.
- Row loop: 860.99 / 874.46 / 865.54 ms.
- `ServerObject` construction: approximately 571 ms.
- Global suggestion index: approximately 358 ms.
- Initial visible-list rebuild: approximately 225 ms.
- Source-store insertion: approximately 9 ms.
- Per-server cache hit rate: 99.9–100%.

Those values were supplied by the maintainer and were not reproduced here. No runtime performance result is claimed by this report.

## Exact files changed

| File | Change in this task |
|---|---|
| `src/dzll_launcher/window.py` | Add the default-off switch, startup-only eligibility gate, GTK idle row transaction, atomic model publication, generation/cancellation/failure handling, deferred suggestion-cache persistence at commit, and aggregate performance markers. |
| `tests/test_startup_batching.py` | Add focused tests for path selection, model parity, lifecycle cancellation, failure recovery, ordering and GTK-thread ownership. |
| `tests/test_startup_perf_instrumentation.py` | Extend the existing expected phase sequence with the new path-selection marker. |
| `devReports/PR16_01_GTK_BATCHING_EXPERIMENT_REPORT.md` | Record implementation, verification, benchmark and rollback details. |

The tracker was not changed and PR16-01 remains IN PROGRESS.

## Architecture

### Opt-in boundary

`DZLL_STARTUP_BATCHING=1` enables the experiment. The constant is read at process import, matching the existing diagnostic switches. With the variable unset or set to any other value, `_apply_db_rows()` selects the existing synchronous `_load_rows_into_store()` path. The disabled check is first in the eligibility gate, so the default path does not inspect or mutate model state beyond its existing work.

Only the ordinary successful final callback from `_begin_startup_update()` passes `allow_startup_batching=True`. The experiment then requires all of the following:

- fetched database success;
- no provisional watchdog load or provisional-to-final replacement;
- active Column View model and no legacy GTK sort model;
- no shutdown;
- empty source store, visible store and endpoint-object index;
- a lifecycle state that can be identified unambiguously.

The existing synchronous loader remains authoritative for manual database updates, provisional watchdog loads, final replacement after a provisional load, fetch failure and cached fallback, legacy model mode, already committed rows, missing Column View state, shutdown and other ambiguous callers.

### Idle row transaction

The successful final database still performs endpoint validation, map preparation and per-server cache loading on GTK before scheduling row work. The row loop then runs from one tracked `GLib.idle_add` source. Each invocation:

1. verifies the transaction generation and shutdown state;
2. constructs at least one row on GTK;
3. stops after crossing a 6 ms monotonic budget;
4. returns control to GLib when rows remain.

The time budget is intentionally conservative for a 60 Hz display and is a starting value for runtime measurement. A single unusually expensive row can exceed it because the deadline is checked at row boundaries. No worker or executor was added. `ServerObject` construction, property writes, debug attachment and all `Gio.ListStore` mutations remain on the GTK main thread.

During construction, the transaction owns private Python containers for:

- ordered `ServerObject` references;
- last-object-by-endpoint mapping;
- parsed-mod sidecars;
- live-state defaults;
- per-server cache candidate records and repair state;
- phase counters and idle-slice durations.

The public source and visible stores remain empty. Duplicate endpoints remain as separate ordered source rows, while the last duplicate remains authoritative in `_obj_by_key`, matching the synchronous path. Current `modCount` overrides, raw `mods_json`, favourites, Last Joined display, server mod search indexes and test-server markers are applied with the same conversions as the current synchronous loader.

### Atomic publication

After the final row, one uninterrupted GTK callback:

1. splices the full ordered object list into the source store;
2. commits endpoint, live and per-server cache sidecars;
3. restores or builds the global suggestion index;
4. snapshots all sort keys;
5. runs the existing complete filter/sort/visible-store rebuild once;
6. performs the existing title and Discord updates;
7. schedules the existing deferred per-server cache write and starts the existing A2S startup coordinator.

There is no main-loop yield between source publication and the complete visible rebuild, so other GTK callbacks cannot observe an intermediate source state. The visible store receives only the existing full filtered and sorted splice. The browser stays behind the startup overlay, and A2S receives only the committed endpoint map.

When a global suggestion cache miss requires persistence, the experiment builds the in-memory index before visible publication but defers the disk save until the source and visible models have committed successfully. The existing 25-second per-server cache write is still scheduled only by post-load coordination after successful publication.

## Generation and lifecycle safeguards

- Each experiment receives a monotonically increasing startup-row generation. Every idle slice and publication verifies it.
- Any superseding `_apply_db_rows()` cancels the pending source before starting its own synchronous or eligible path.
- Shutdown invalidates the generation, removes the tracked GLib source and drops staged references.
- A late callback returns immediately if its state, generation or shutdown guard is stale.
- Row-construction failure publishes nothing, releases staging, completes startup presentation with an error state, and invokes no A2S or cache-write timer.
- Publication failure rolls the source and visible stores back to empty, clears the endpoint index, removes only live defaults added by this transaction, restores suggestion/cache state, completes the overlay, and invokes no A2S or cache-write timer.
- A successful transaction clears its staging state before post-load coordination. It then uses the existing A2S generation, Companion restore, offline timer and deferred cache-write guards.
- Idle submission failure follows the same recovery path as construction failure, so the overlay does not remain indefinitely.
- The startup DB in-flight flag stays set while idle slices are pending and is cleared by the transaction completion callback. Synchronous and exceptional final paths retain the previous `finally` cleanup.

No GObject is constructed or mutated in a background thread. SQLite fetch/read behavior and the existing worker lifecycle are unchanged.

## Unsupported-path fallback behavior

The aggregate `startup_row_load_path` marker records `path=batched` or `path=synchronous`. Synchronous reasons are:

- `disabled`;
- `unsupported_caller`;
- `nonfinal_database`;
- `provisional_or_replacement`;
- `legacy_model`;
- `shutdown`;
- `column_view_unavailable`;
- `rows_already_committed`;
- `ambiguous_model_state`;
- `endpoint_index_not_empty`.

This gate keeps manual refresh, provisional/final replacement, cached fallback and legacy models out of the experiment without restructuring those lifecycles.

## Instrumentation

All new lines are gated by `DZLL_PERF_LOG=1`; there is no per-row logging.

| Phase | Meaning |
|---|---|
| `startup_row_load_path` | Selected path and synchronous fallback reason. |
| `startup_row_batch_build` | Wall elapsed row-build interval, batch count, maximum/average idle-slice duration, summed slice CPU time, rows and cache hits/misses. |
| Existing `row_scalar_conversion`, `row_sidecar_attachment`, `row_conversion`, `mod_preview_index`, `server_object_construction`, `row_loop_total` | Batched aggregate attribution; `row_loop_total` is explicitly labeled as idle-slice execution only and includes a separate wall elapsed field. |
| Existing `server_mod_cache_apply` | Cache read/fingerprint/restore/rebuild overlap with hit/miss counts and hit rate. |
| `batched_source_store_publication` | One source splice plus endpoint/live/cache sidecar commit. |
| `batched_global_mod_suggestion_index` | Global suggestion restore/build before visible publication. |
| `batched_sort_key_snapshot` | Full sort-key snapshot. |
| `batched_initial_visible_store_rebuild` | Existing filter, sort and visible splice call as seen by the transaction. Existing nested initial-rebuild timers remain available. |
| `batched_suggestion_cache_persistence` | Cold/missing global cache save after successful model commit, when present. |
| `startup_row_batch_publication` | Full final publication duration, visible count, largest publication component and DB-ready-to-publication elapsed time. |
| `startup_row_batch_summary` | Full batched apply wall time and the largest uninterrupted GTK section among initial setup, maximum idle slice, final publication and post-load coordination. |
| `startup_row_batch_cancelled` / `startup_row_batch_failure` | Aggregate lifecycle outcome without per-row output. |

Every line inherits the current `load_id`, input/valid row counts, fetched/provisional state and cache states from the existing DB-application context. Nested and overlapping timers retain explicit scope labels; `server_mod_cache_apply` must not be added to `row_loop_total`.

## Focused tests

`tests/test_startup_batching.py` covers:

- default synchronous path versus opt-in idle path;
- full source order and visible filtering/sorting parity;
- favourites, Last Joined values, raw mods, mod-search indexes and current `modCount` override parity;
- duplicate endpoint list/order and last-object index behavior;
- manual/nonfinal, provisional/replacement and legacy fallback decisions;
- superseding cancellation and stale callback rejection;
- shutdown during pending construction;
- row-construction failure;
- failure after source mutation during final publication, including rollback of source, visible, endpoint and live state;
- no A2S or cache timer before successful commit;
- suggestion-cache persistence after complete visible commit and before A2S;
- `ServerObject` construction occurring only when the GTK idle callback runs on the caller thread;
- same-load instrumentation identity, slice counts and cache hit/miss counts.

Verification executed with the repository's isolated pytest HOME/XDG boundary:

```text
pytest -q tests/test_startup_batching.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_browser_server_ingestion_validation.py tests/test_mod_suggestion_cache.py tests/test_pin_favourites_filter_priority.py tests/test_last_joined_calendar.py tests/test_core_startup_robustness.py
236 passed, 13 skipped, 1 warning in 0.38s
```

The skips are the existing GTK/display-dependent skips in the selected suite. The warning is the existing PyGObject `GLib.unix_signal_add_full` deprecation warning.

Additional gates:

```text
python -m py_compile src/dzll_launcher/window.py tests/test_startup_batching.py tests/test_startup_perf_instrumentation.py
passed

git --no-pager diff --check
passed
```

No runtime benchmark was run.

## Known limitations and remaining GTK phases

- The experiment targets only the approximately 861–874 ms row loop. It can improve animation opportunities while increasing total DB-ready-to-publication time through repeated main-loop scheduling.
- Endpoint validation, map model update and per-server cache disk loading remain in the initial uninterrupted GTK setup before the first row slice.
- Final publication remains one uninterrupted section. The supplied baseline suggests the global suggestion index at approximately 358 ms and initial visible rebuild at approximately 225 ms may remain visible stalls even if row slices stay near budget.
- Cold global suggestion-cache persistence occurs after model commit but remains in the final GTK callback. `batched_suggestion_cache_persistence` exposes that case.
- The private transaction temporarily retains the input row tuple, constructed objects, endpoint/parsed/live dictionaries, a copied cache-record dictionary and cache candidates. Peak memory can therefore rise until publication or cancellation. References are released when state is cleared.
- Row conversion is intentionally duplicated in the experimental path so the default synchronous hot path remains unchanged. Parity tests reduce drift risk, but future ingestion changes must update both paths while the experiment exists.
- The 6 ms deadline is checked between rows. It is not a hard preemption boundary.
- `initial_next_frame_tick` remains a frame-clock opportunity marker, not proof of rendered pixels. Screen recording or an equivalent external frame observation is still needed for spinner continuity and first visible row.

## Maintainer runtime benchmark

Use the same 12,047-row database, settings, display/session, Python/GTK build and warm cache files for every comparison. Close each DZLL process normally after the startup trace, next-frame marker, overlay dismissal and A2S completion. Keep other diagnostic switches and profilers disabled. If the DB or cache state changes, discard the set and repeat all runs.

### Default synchronous control

```bash
env -u DZLL_STARTUP_BATCHING PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-default-run1.log
env -u DZLL_STARTUP_BATCHING PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-default-run2.log
env -u DZLL_STARTUP_BATCHING PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-default-run3.log
```

### Opt-in experiment

```bash
PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-optin-run1.log
PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-optin-run2.log
PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-batch-optin-run3.log
```

Confirm `startup_row_load_path path=batched`, identical `input_rows`/`valid_rows`, comparable cache states, one final nonprovisional `load_id`, and no migration/write or unrelated process overlapping the measured interval. A fallback run is not an opt-in sample.

For each run record:

- `db_gtk_completion` wall time;
- largest `[PERF] main loop stall`;
- `startup_row_batch_build` batch count, maximum/average slice and row CPU/wall time;
- `startup_row_batch_publication` duration and largest component;
- `startup_row_batch_summary largest_sync_phase` and duration;
- DB-ready-to-publication time;
- cache hits/misses and suggestion cache state;
- `initial_next_frame_tick` and `startup_overlay_dismissal` timestamps.

Use an external 60 fps recording aligned with `startup_ms` to record spinner freeze intervals, first frame containing bound server rows and first responsive input. Do not treat the next-frame marker as rendered pixels or overlay dismissal as first render. Compare the median and full range across the three default and three opt-in runs. Total startup time may regress while animation improves, so judge both separately.

Runtime acceptance requires a repeatable material reduction in longest main-loop stall and visibly smoother spinner/input behavior, with identical source/visible counts, filters, sorting and A2S behavior. Roll back the experiment if any of these occur:

- no material stall or animation improvement beyond run variation;
- final publication becomes the same-size stall as the old row loop;
- DB-ready-to-publication or memory growth is disproportionate;
- partial/flickering rows, wrong sort/filter output or duplicate mismatch;
- overlay, A2S, cache or shutdown lifecycle regression;
- slice duration repeatedly exceeds the budget by a material amount.

## Rollback

The immediate runtime rollback is to leave `DZLL_STARTUP_BATCHING` unset. That selects the unchanged synchronous loader without changing caches or persisted data.

If the experiment is removed from source, remove only the batching constants/state, startup eligibility and idle transaction methods, the deferred suggestion-cache-save guard, the asynchronous completion branch, and `tests/test_startup_batching.py`; retain the previously measured startup/cache instrumentation. Re-run the same focused suite, `py_compile` and `git --no-pager diff --check`. No cache migration or data repair is required because the experiment uses the existing formats and publication functions.

## Conclusion

The implementation is intentionally default-off and limited to the one lifecycle where an empty browser can be populated transactionally. It yields during the measured row-construction work without moving GObjects off GTK, keeps partial rows private, and leaves every unsupported path on the proven synchronous loader. Runtime evidence is still required to decide whether the reduction in uninterrupted row work justifies the added staging memory and lifecycle code. PR16-01 therefore remains IN PROGRESS.
