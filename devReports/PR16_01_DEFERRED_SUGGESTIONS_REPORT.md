# PR16-01 — Deferred global mod suggestion restoration

Date: 2026-10-11  
Status: experimental implementation complete and verified offline; PR16-01 remains **IN PROGRESS** pending controlled runtime evidence.

## Scope and measured motivation

This change removes global mod autocomplete catalogue restoration from the critical publication section of the default-off `DZLL_STARTUP_BATCHING=1` experiment. The existing synchronous startup path is unchanged.

Maintainer measurements for the 12,047-server batched profile showed:

- 143–144 GTK idle row batches;
- 20.5–29.3 ms longest row slice;
- 622–660 ms final GTK publication;
- 327–381 ms global suggestion catalogue restoration inside that publication;
- 803–858 ms largest main-loop stall, down from an original synchronous average near 1,746 ms.

These figures were supplied for this task. No live benchmark was run here.

The catalogue is used for autocomplete, popularity ordering and exact new-chip resolution. Per-server `mod_search_index` data remains on every `ServerObject` and continues to own server filtering, selected-chip matching and join-independent row behavior.

## Exact files changed

| File | Change in this task |
|---|---|
| `src/dzll_launcher/window.py` | Remove catalogue restoration from batched final publication; add post-overlay worker scheduling, generation and retry state, GTK result publication, explicit early-autocomplete behavior and performance markers. |
| `src/dzll_launcher/mod_suggestion_cache.py` | Add a pure-data worker entry point that loads and validates the existing cache or rebuilds a complete index from existing mod JSON references. |
| `tests/test_deferred_mod_suggestions.py` | Add focused cache, readiness, worker, lifecycle, generation, selected-chip and instrumentation tests. |
| `tests/test_startup_batching.py` | Assert synchronous-path restoration and batched publication/A2S progress without catalogue work; update publication-component expectations. |
| `devReports/PR16_01_DEFERRED_SUGGESTIONS_REPORT.md` | Record architecture, verification and benchmark procedure. |

No tracker, A2S threshold, Forge, UI layout, `MEMORY.md` or Git state/history change was made. PR16-01 remains IN PROGRESS.

## Startup control flow

### Default synchronous path

When `DZLL_STARTUP_BATCHING` is unset, `_load_rows_into_store()` continues to call `_rebuild_mod_suggestion_index()` synchronously before sort-key snapshot and visible-model rebuild. Cache-hit, miss rebuild/save, migration metadata and monthly refresh behavior remain as before.

Provisional watchdog loads, final replacement after a provisional load, manual database update, fetch failure/cached fallback, legacy model mode and ambiguous lifecycle states still select this synchronous loader.

### Experimental batched path

For an eligible `DZLL_STARTUP_BATCHING=1` startup:

1. GTK idle slices construct the full server-row transaction exactly as in the preceding batching experiment.
2. Final GTK publication commits the source store, endpoint/live/cache sidecars, sort-key snapshot and complete filtered/sorted visible model.
3. The publication marks global suggestions as pending but does not read, decode, validate, restore, build or save the catalogue.
4. Existing post-load coordination immediately starts the unchanged A2S sweep and deferred per-server cache timer.
5. The catalogue remains in its initialized empty state while the startup overlay and A2S warmup gate remain active.
6. `_complete_startup_presentation()` first dismisses the overlay through the existing coordinator, then authorizes and attempts deferred submission.
7. The existing single-worker `_update_executor` runs `prepare_deferred_suggestions()` using only a cache path and a tuple of references to existing `mods_json` strings.
8. The worker returns only a plain Python suggestion index and scalar cache metadata. A GLib idle callback verifies lifecycle ownership and publishes those fields on GTK.
9. If mod-search text is still active, the normal 35 ms suggestion refresh is queued so autocomplete appears without another keystroke.

The full initial A2S sweep, its 65% warmup gate, result application and final ping sort remain unchanged. Catalogue work begins after overlay dismissal and may overlap the remainder of the A2S sweep.

## Worker and cache behavior

`prepare_deferred_suggestions()` contains no GTK or GObject work.

- On a valid v2 or v1 cache hit, it performs the existing bounded file read, digest/integrity checks, JSON decoding, validation and in-place index restoration on the worker.
- On a missing, corrupt, incompatible or rejected cache, it builds the complete global index from the current server payload references on the worker.
- It does not publish a cache file. This avoids a worker writing stale data before GTK validates the generation.
- A rebuilt miss is marked for the existing post-sweep staged migration writer. That writer serializes the already built index and atomically replaces the cache after its existing generation checks, avoiding an immediate second catalogue rebuild.
- A valid v1 result retains the existing migration-pending behavior.
- An expired result retains the existing monthly refresh behavior after the initial sweep.

The worker result uses `_ModSuggestionIndex`, dictionaries, tuples, strings, numbers and booleans only. GTK publishes the index reference and cache metadata in `_on_deferred_suggestion_restore_done()`.

## Autocomplete readiness contract

During the deferred interval:

- global suggestions are temporarily unavailable;
- the suggestion panel is hidden with reason `catalogue_not_ready`;
- no synchronous row-derived catalogue rebuild is attempted from focus or typing;
- exact resolution of a new chip returns no match and leaves the typed text available for retry;
- no incorrect, partial or stale suggestion list is displayed.

Existing selected chips continue to provide their stored `query_term` values to `_combined_filter_func()`. Filtering uses each row's already committed `mod_search_index`; it does not depend on the global catalogue. Mod previews, current DB `modCount` overrides, raw mod data, favourites, Previously Joined, sorting, Server Companion and join-time mod verification are untouched.

After successful GTK publication, `_suggestion_cache_ready` becomes true, the pending state clears, and normal autocomplete and exact-chip resolution resume. An active eligible query is refreshed automatically.

## Lifecycle safeguards

- The deferred operation captures the current `_suggestion_cache_generation` and original startup performance context.
- Only one deferred load may be in flight. Repeated overlay completion and browser-live ticks cannot submit duplicates.
- Result application requires both a non-shutdown window and the same cache generation. A stale result cannot overwrite a catalogue installed by a later synchronous load or refresh.
- A synchronous database application increments the generation and clears deferred pending/allowed/in-flight ownership before using the existing synchronous catalogue path.
- Batched transaction start and shutdown likewise invalidate prior deferred ownership.
- Shutdown clears pending authorization and increments the cache generation; a late GLib callback discards its plain result.
- Executor submission failure and unexpected worker failure leave autocomplete unavailable, preserve the pending request and set a 60-second retry boundary. The browser-live tick retries only after overlay authorization and only when no load is active.
- Missing and corrupt cache files are handled as complete worker-side rebuilds, not GTK fallback work.
- The scheduler refuses to overlap an existing suggestion refresh or migration worker and waits while a database update is marked in flight.
- Cache-miss rebuilding publishes no file from the deferred worker. Existing staged migration and refresh code retains atomic file publication and stale-generation cleanup.

The result callback clears the in-flight flag only when it owns that generation, so an old callback cannot clear ownership for a newer operation.

## Memory ownership

The scheduler creates one tuple containing references to `mods_json` strings already owned by committed `ServerObject`s. It does not copy the strings or retain server GObjects in the worker request. The tuple and executor work item are released after the worker completes.

On a cache hit, the loader temporarily owns the bounded raw byte buffer and decoded document while restoring the final index in place. Those temporary objects become unreachable when the worker function returns; only the final index crosses to GTK. On a miss, the worker builds one final index from the existing string references. It does not create or retain another 20 MB cache payload snapshot or staged output file.

The active global index is necessarily retained after GTK publication because autocomplete uses it. Peak transient memory during JSON decoding still needs runtime RSS/USS measurement; this implementation reduces retained ownership rather than claiming zero transient allocation.

## Performance instrumentation

All new logging is gated by `DZLL_PERF_LOG=1` and emits no per-row lines.

| Phase | Fields and meaning |
|---|---|
| `deferred_suggestion_schedule` | Main-thread tuple/scheduling duration, submitted or submission-failed result, generation and row count. |
| `deferred_suggestion_worker` | Worker total duration plus existing file read, JSON decode, canonical JSON, SHA-256/integrity, entry/prefix/exact restore and fallback-build durations; cache-hit, rebuilt or worker-failed result. The line is reported on GTK and explicitly labels worker execution ownership. |
| `deferred_suggestion_apply` | GTK result-publication duration and committed or stale outcome. |
| `autocomplete_ready` | Zero-duration readiness marker with startup timestamp, generation, source and catalogue entry count. |
| Existing `startup_row_batch_publication` | Final publication duration and `largest_publication_component`; the component set now excludes global suggestion restoration. |
| Existing `startup_row_batch_summary` | Largest uninterrupted startup GTK section after the change. |

Deferred callback lines retain the original DB `load_id`, input/valid row counts and cache context even if the window's current performance context later changes. `cache_miss_rebuilt`, `submit_failed`, `worker_failed` and `stale` make fallback and failure outcomes explicit.

## Offline verification

Focused tests cover:

- valid-cache worker restoration and stage timing;
- missing and corrupt cache fallback without worker-side file publication;
- overlay dismissal as the authorization boundary;
- successful worker-to-GLib-to-GTK publication;
- no duplicate concurrent load;
- payload reference ownership without GObject transfer;
- early autocomplete hiding without synchronous rebuild;
- selected-chip query terms while the catalogue is unavailable;
- automatic refresh of an active query after readiness;
- submission failure and unexpected worker failure retry state;
- shutdown and generation replacement rejection;
- no stale overwrite of a newer catalogue;
- same-load performance identity;
- synchronous default restoration parity;
- batched list publication and A2S start without catalogue work;
- existing source/visible parity, filters, sorting, favourites, Previously Joined, cache, ingestion and startup robustness.

Executed command and result:

```text
pytest -q tests/test_deferred_mod_suggestions.py tests/test_startup_batching.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_browser_server_ingestion_validation.py tests/test_mod_suggestion_cache.py tests/test_pin_favourites_filter_priority.py tests/test_last_joined_calendar.py tests/test_core_startup_robustness.py
245 passed, 13 skipped, 1 warning in 0.39s
```

The skips are the existing GTK/display-dependent skips. The warning is the existing PyGObject `GLib.unix_signal_add_full` deprecation warning.

Additional gates:

```text
python -m py_compile src/dzll_launcher/window.py src/dzll_launcher/mod_suggestion_cache.py tests/test_deferred_mod_suggestions.py tests/test_startup_batching.py tests/test_startup_perf_instrumentation.py
passed

git --no-pager diff --check
passed
```

DZLL, Steam and live benchmarks were not launched. No network or dependency installation was used.

## Known limitations

- Autocomplete is deliberately unavailable from first list publication until the post-overlay worker result reaches GTK. Existing selected chips remain active, but a new chip cannot be resolved during that interval.
- Python JSON decoding, validation and restoration can contend for the GIL with GTK and A2S callbacks even though it runs on a worker. Runtime frame and input measurements must confirm that this contention is materially smaller than the former 327–381 ms GTK block.
- A missing or corrupt cache requires a full worker rebuild and a later post-sweep staged serialization. That cold path uses more CPU and takes longer to reach autocomplete readiness than a warm valid cache.
- Worker completion depends on a GLib result callback. Shutdown generation checks prevent stale publication, but a process exiting before queued callbacks run naturally does not expose autocomplete readiness.
- The final GTK publication still includes source commit, sort-key snapshot and the full visible filter/sort/splice. The next benchmark must identify the new `largest_publication_component`; this implementation does not claim that the remaining 225 ms-class visible rebuild has improved.
- The row-slice maximum remains separately bounded only at row boundaries and was measured at 20.5–29.3 ms before this change.

## Isolated-profile runtime benchmark

Prepare one isolated seed profile containing the same 12,047-row database, settings and warm per-server/global caches used for the prior measurements. Make six byte-identical copies before launching; do not reuse a mutated run directory because migration, refresh and cache timers can change it.

Example preparation, with the maintainer replacing `/path/to/prepared-dzll-profile` with the existing isolated seed:

```bash
DZLL_BENCH_SEED=/path/to/prepared-dzll-profile
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-sync-1
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-sync-2
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-sync-3
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-deferred-1
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-deferred-2
cp -a "$DZLL_BENCH_SEED" /tmp/dzll-pr16-deferred-3
```

Default synchronous controls:

```bash
env -u DZLL_STARTUP_BATCHING HOME=/tmp/dzll-pr16-sync-1 XDG_CONFIG_HOME=/tmp/dzll-pr16-sync-1/.config XDG_DATA_HOME=/tmp/dzll-pr16-sync-1/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-sync-1/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-sync-1/.cache PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-sync-1.log
env -u DZLL_STARTUP_BATCHING HOME=/tmp/dzll-pr16-sync-2 XDG_CONFIG_HOME=/tmp/dzll-pr16-sync-2/.config XDG_DATA_HOME=/tmp/dzll-pr16-sync-2/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-sync-2/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-sync-2/.cache PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-sync-2.log
env -u DZLL_STARTUP_BATCHING HOME=/tmp/dzll-pr16-sync-3 XDG_CONFIG_HOME=/tmp/dzll-pr16-sync-3/.config XDG_DATA_HOME=/tmp/dzll-pr16-sync-3/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-sync-3/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-sync-3/.cache PYTHONPATH=src DZLL_PERF_LOG=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-sync-3.log
```

Opt-in deferred runs:

```bash
HOME=/tmp/dzll-pr16-deferred-1 XDG_CONFIG_HOME=/tmp/dzll-pr16-deferred-1/.config XDG_DATA_HOME=/tmp/dzll-pr16-deferred-1/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-deferred-1/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-deferred-1/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-deferred-1.log
HOME=/tmp/dzll-pr16-deferred-2 XDG_CONFIG_HOME=/tmp/dzll-pr16-deferred-2/.config XDG_DATA_HOME=/tmp/dzll-pr16-deferred-2/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-deferred-2/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-deferred-2/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-deferred-2.log
HOME=/tmp/dzll-pr16-deferred-3 XDG_CONFIG_HOME=/tmp/dzll-pr16-deferred-3/.config XDG_DATA_HOME=/tmp/dzll-pr16-deferred-3/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-deferred-3/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-deferred-3/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-deferred-3.log
```

For every run confirm identical DB row counts, cache file hashes and cache state before comparison. Discard a set if a fetch changes the database. Record callback duration, largest main-loop stall, row-slice maximum, final publication, largest publication component, next-frame marker, overlay dismissal and first responsive input.

For deferred runs also verify this ordering by `startup_ms`:

1. `startup_row_batch_publication` completes;
2. A2S warmup begins and the overlay is eventually dismissed;
3. `deferred_suggestion_schedule` occurs after `startup_overlay_dismissal`;
4. `deferred_suggestion_worker` reports the expected warm `cache_hit` and stage durations;
5. `deferred_suggestion_apply` remains small;
6. `autocomplete_ready` appears and mod suggestions work normally.

Use a 60 fps recording or equivalent frame observation to compare spinner motion before reveal, first nonempty browser frame, first responsive interaction and any post-reveal hitch while the worker holds the GIL. Measure current RSS/USS before scheduling, during worker restoration and after readiness; do not use only process high-water RSS to infer retained ownership.

Rollback conditions are any source/visible/filter/sort mismatch, selected-chip failure, A2S lifecycle change, stale catalogue overwrite, repeated post-reveal hitch, material retained-memory increase, or an insignificant publication/stall improvement relative to run variation. Immediate runtime rollback is to leave `DZLL_STARTUP_BATCHING` unset; this restores the existing synchronous startup path without a cache migration or data repair.

## Conclusion

The experimental final publication now depends only on browser-authoritative row and model state. Global autocomplete restoration begins after overlay dismissal on an existing worker, and GTK publishes only the completed plain index after generation validation. The design preserves the synchronous control path and existing refresh/migration machinery while avoiding a second retained payload snapshot. Runtime UX and memory evidence are still required, so PR16-01 remains IN PROGRESS.
