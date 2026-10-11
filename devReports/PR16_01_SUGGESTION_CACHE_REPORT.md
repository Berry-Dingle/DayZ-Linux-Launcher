# PR16-01 — persistent mod-suggestion catalogue cache

Date: 2026-10-10  
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`  
Status: **IN PROGRESS**, pending a controlled runtime cache benchmark.

## Measured context

The prior shared-decoding experiment was measured on 11,980 servers in three runs before and after: GTK DB callback average **4,140 → 3,576 ms**, global suggestion build **1,496 → 1,180 ms**, and main-loop stall **4,222 → 3,728 ms**. These are supplied measurements of the earlier experiment. The persistent cache introduced here has **not** been runtime benchmarked.

## Exact files changed for this task

| File | Purpose |
|---|---|
| `src/dzll_launcher/mod_suggestion_cache.py` | New XDG cache path, versioned bounded JSON format, integrity check, validation, atomic save, and worker-side staged build. |
| `src/dzll_launcher/window.py` | Reuse a valid catalogue on every DB load, preserve cold-cache fallback, gate monthly refresh on completed initial sweep and the regular browser A2S tick, reject stale workers, and retain opt-in performance diagnostics. |
| `tests/test_mod_suggestion_cache.py` | Cache format, expiry, corruption, bounds, XDG path, empty index, and exact suggestion equivalence tests. |
| `tests/test_browser_server_ingestion_validation.py` | DB-load cache reuse, expired/corrupt recovery, duplicate/empty datasets, worker failure, generation, and shutdown tests. |
| `devReports/PR16_IMPROVEMENTS_TRACKER.md` | Record supplied shared-decoding benchmark and new cache work; keep PR16-01 IN PROGRESS. |
| `devReports/PR16_01_SUGGESTION_CACHE_REPORT.md` | This report. |

The working tree already contained instrumentation and shared-decoding changes in `app.py`, `window.py`, `mod_search.py`, `mod_suggestions.py`, the untracked `startup_perf.py` and `mod_parsed.py`, and other reports, packages and release artifacts. Those earlier changes were preserved. No Git-history-changing command was used.

## Cache format and use

`suggestion_cache_path()` resolves `$XDG_CACHE_HOME/dzll/mod_suggestions_v1.json`, falling back to `~/.cache/dzll` (`mod_suggestion_cache.py:19-31`). Format version 1 stores creation time, all built mod entries, count, prefix map, exact-compact map, and a SHA-256 digest of the index body (`mod_suggestion_cache.py:101-146`). The mutable query-result cache is deliberately **not** persisted and is restored empty. Entry tuple fields and lookup-map index tuples are reconstructed, preserving `suggest_mods()` ranking and matching semantics (`mod_suggestion_cache.py:60-99`; `mod_suggestions.py:438-536`). The loader enforces a 64 MiB input limit, entry/map bounds, field types and lengths, map index ranges, version, age sanity and digest. Missing, corrupt, oversized or incompatible files are safe misses; atomic writes use the existing `atomic_write_json()` facility (`mod_suggestion_cache.py:101-146`; `atomic_json.py:9-43`). A semantic change to catalogue construction or lookup representation must increment `FORMAT_VERSION`.

On the first DB load, a valid file becomes the active index. Later provisional/final and manual DB loads reuse the in-memory index without scanning server mods for global suggestions (`window.py:8447-8470`). A valid **empty** catalogue is still marked ready, so the suggestion UI's empty-index fallback cannot start a costly rebuild (`window.py:8802-8805`). With no valid cache, the existing canonical `_obj_by_key` set is used to build an index; a nonprovisional load saves it atomically (`window.py:8471-8497`). A provisional watchdog load can use an in-memory cold result but does not persist it, allowing the final DB load to build from its final canonical set (`window.py:5441-5455`, `8486-8497`). A confirmed successful empty DB may persist an empty index; a failed empty fetch does not. If disk saving fails, autocomplete still uses the in-memory result.

Per-server previews/search indexes continue to use the current DB payload and the load-local parsed data (`window.py:5860-5874`, `5933-5938`); the persistent catalogue does not replace either. Raw `mods_json` remains on `ServerObject` for the required-mod row display and authoritative join-time parser (`window.py:5916`; `workshop_mods.py:19-53`). An intentionally stale popularity catalogue may omit newly added mods or suggest a mod absent from today's servers until refresh; server filtering and join preparation still use current data.

## Monthly refresh lifecycle

The cache expires at 30 days, but an expired file loads and remains fully usable (`mod_suggestion_cache.py:23`, `101-124`; `window.py:8458-8469`). Refresh is checked only from the normal five-second visible-server A2S tick after its existing pause gate (`window.py:7402-7407`). Submission also requires no DB update, no other refresh, a nonempty current canonical set, and either completion of the **entire** initial A2S sweep or the safe no-sweep DB failure/empty path (`window.py:8499-8529`). The overlay's earlier warmup reveal is not the refresh gate. Normal browser refresh and Server Companion polling are not stopped or awaited. Sweep completion is recorded only after its queue, inflight work and result buffer are empty, including the zero-key path (`window.py:6625-6631`, `6808-6824`). Failed individual A2S futures and submit failures are accounted for by existing sweep completion; a DB load that cannot start a sweep enables deferred retry at a later normal browser tick. A cancelled old generation is replaced by the new DB generation or shutdown gate (`window.py:5745-5748`, `5813-5815`, `3020`).

At refresh submission, GTK snapshots only raw string payloads from the final `_obj_by_key` values. The existing single-worker update executor receives only those strings and a `Path`; its worker builds the full plain-Python index and writes a unique staged file (`window.py:8514-8531`; `mod_suggestion_cache.py:149-160`). The old in-memory index remains active and queryable. The GTK completion callback rejects results after DB generation change or shutdown, and publishes a successful staged file with `os.replace()` before switching the in-memory index (`window.py:8533-8574`). Each replacement has a fresh empty query-result cache. Worker/submit/publish failures leave the old index and cache active and wait at least 60 seconds before another normal-tick attempt. Only one refresh can be in flight per window. No GTK/GObject is read or changed by the cache worker.

## Diagnostics

Existing `DZLL_PERF_LOG=1` startup phase instrumentation is retained. New low-volume phases are `mod_suggestion_cache_load` (`result=hit|miss`, expiry and entry count on hits), `mod_suggestion_cache_hit` (`source=memory`), `mod_suggestion_cache_save`, `mod_suggestion_cache_refresh_start`, and `mod_suggestion_cache_refresh_complete` (`result=committed|worker_failed|publish_failed`) (`window.py:8447-8574`). The original `global_mod_suggestion_index` phase still brackets the loader call (`window.py:5963-5974`). No per-server cache timings or A2S callback logs were added.

## Offline verification

Command: `python -m pytest -q tests/test_mod_suggestion_cache.py tests/test_browser_server_ingestion_validation.py tests/test_mod_name_normalization.py tests/test_mod_name_resolver.py tests/test_mod_metadata_bulk_names.py tests/test_server_mod_popup_sort_cleanup.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py`.

Result: **273 passed, 13 skipped**, with one PyGObject deprecation warning. The skipped display-dependent cases are not executed coverage. Repository `conftest.py` isolated HOME/XDG before application imports. `python -m py_compile` succeeded for the changed Python files. `git --no-pager diff --check` succeeded. Final diff/status were inspected. No DZLL launch, Steam launch, live A2S query, network request, dependency install, commit or other mutating Git command was performed.

Specific regression evidence: round-trip cache entries/maps and `suggest_mods()` results match a fresh index for alias, exact ID, prefix and multiword queries (`tests/test_mod_suggestion_cache.py:25-45`); corruption, incompatible version, tampering and size limits produce safe misses (`tests/test_mod_suggestion_cache.py:46-77`); canonical duplicate endpoint and current per-server index behavior are covered in ingestion tests (`tests/test_browser_server_ingestion_validation.py:144-172`, `202-216`); warm/empty cache reuse and corrupt/expired disk recovery are covered (`tests/test_browser_server_ingestion_validation.py:218-264`); deferred sweep gate, duplicate suppression, old-index continuity, worker failure, changed DB generation and shutdown are covered (`tests/test_browser_server_ingestion_validation.py:284-359`).

## Residual risks and runtime benchmark plan

Cold/corrupt-cache construction and initial atomic save still happen synchronously during DB loading, as a one-time recovery path. Valid-cache loading parses and validates the bounded JSON file on GTK. The monthly worker build is off GTK, but making its immutable string snapshot is an O(server-count) GTK operation; these costs require measurement. The cache may be large, and staged refresh temporarily holds both old and new indexes plus the payload snapshot. A crash can leave a harmless `.pending` file; it is never loaded as a catalogue. Display-backed startup and a real 11,980-server cache profile were not run.

Benchmark on the same DB/settings/hardware as the supplied runs with `DZLL_PERF_LOG=1`: at least three **warm-cache** starts and three **cold-cache** starts, plus an expired-cache start through complete A2S sweep and worker commit. Capture cache hit/miss/load/refresh phases, `global_mod_suggestion_index`, GTK DB callback, main-loop stall, overlay dismissal and peak RSS. Confirm expired autocomplete works before refresh and that refresh starts only after the full sweep while normal background A2S is active. Compare medians/ranges against the supplied shared-decoding baseline; do not infer benefit from the current offline tests. Keep PR16-01 **IN PROGRESS** until that runtime comparison and display-backed regression verification complete.
