# PR16-01 — cache regression verification

Date: 2026-10-11  
Status: **IN PROGRESS**. This was an offline source and regression verification pass. No DZLL or Steam process was launched; no network request, dependency installation, Forge edit, or Git mutation was made.

## Scope and changes

The implementation remains the separate per-server derived-mod cache in `src/dzll_launcher/server_mod_cache.py`, the global v2 suggestion cache in `src/dzll_launcher/mod_suggestion_cache.py`, and their existing integration in `src/dzll_launcher/window.py`. No production cache format, source, startup architecture, A2S, join, layout, Steam, or Companion code was changed in this pass.

Files changed for this verification:

| File | Change |
|---|---|
| `tests/test_last_joined_calendar.py` | Add the existing suggestion-refresh scheduler to the live-tick test double and assert its call order. |
| `tests/test_browser_server_ingestion_validation.py` | Add one cold/warm differential test covering selected mod chips through the actual browser filter function. |
| `devReports/PR16_IMPROVEMENTS_TRACKER.md` | Record current measured runtime and verification progress; retain **IN PROGRESS**. |
| `devReports/PR16_01_CACHE_VERIFICATION_REPORT.md` | This report. |

The checkout was already dirty and contained untracked reports, packages, and release directories. Those were preserved. The large pre-existing diff in `tests/test_browser_server_ingestion_validation.py` belongs to the earlier cache work; this pass added only the selected-chip test.

## Source-confirmed behavior and regression evidence

| Area | Current behavior and evidence |
|---|---|
| Per-server cold/warm path | `window.py:5921-5953,5989-6025,6097-6124` loads one bounded cache, fingerprints the exact accepted raw `mods` string for each valid endpoint, restores preview/count/search sets on a matching digest, and otherwise calls `ParsedServerMods.from_json()`, `parse_mods_preview()`, and `build_server_mod_index()`. The current DB `modCount` override is applied after either path; raw `mods_json` stays on `ServerObject`. Differential tests cover valid, empty, malformed, non-list, Unicode, duplicate names/IDs, zero ID, eight-name truncation, index types/alias identity, and no per-row JSON decode on a warm hit (`test_server_mod_cache_warm_output_matches_uncached_builders_without_row_decode`). |
| Filter and selected chips | `_build_filter_state()` turns selected-chip `query_term` values into the parsed required-mod query; `_combined_filter_func()` checks that query against `obj.mod_search_index` (`window.py:8238-8344,8389-8397`). The new test `test_server_mod_cache_warm_selected_chips_filter_like_cold` verifies a two-chip ID/name query gives the same accepted/rejected rows after cold and warm loads. Existing differential tests compare raw mod queries on both indexes. |
| Autocomplete | The catalogue uses its own global v2 cache and is not built from restored per-server sets (`window.py:8602-8655`). `suggest_mods()` consumes the restored global index. Existing tests in `test_mod_suggestion_cache.py` compare cold and restored ranking, ordering, aliases, IDs, and queries; `test_server_mod_cache_hit_preserves_cold_global_suggestion_fallback` verifies that a missing global cache rebuilds from the final endpoint raw JSON even when per-server records hit. |
| Partial invalidation and lifecycle | `test_server_mod_cache_partial_change_query_port_and_duplicate_endpoint` verifies only changed payloads rebuild, query-port-only changes retain a hit, and the last duplicate canonical endpoint owns the persisted record. `test_server_mod_cache_provisional_failed_final_and_manual_refresh` checks provisional/failed loads do not publish/prune, successful final/manual loads do, and disappeared/reappeared endpoints behave correctly. Stage and completion guards reject stale DB generations and shutdown (`window.py:5759-5812,5855-5920`). |
| Corrupt/missing cache | `server_mod_cache.py:66-126` checks file size, frame digest, schema and normalization versions, entry/record bounds. Missing or invalid data becomes a miss; failed atomic publication retains the preceding file. Existing tests cover corrupt/version/bounds/write failure and unchanged-write avoidance. Global v2 tests cover byte-digest damage, incompatible version, bounds, v1 migration, and failure recovery (`test_mod_suggestion_cache.py`). |
| Join freshness | Browser and join paths still read/parse raw `mods_json`; cached preview/search fields are never authority for Workshop preparation (`window.py:12192-12200,12323-12330`; `src/dzll_launcher/workshop_mods.py:19-53`). No join or Steam code changed. |
| Global monthly lifecycle | The 30-day expiry check only schedules a refresh when cache readiness/expiry, no conflicting migration/refresh/DB update, and `_suggestion_sweep_complete` or the pre-existing `_suggestion_sweep_fallback_ready` gate are satisfied (`window.py:8666-8700`). Normal initial sweep success sets the completion gate; a documented failed/cancelled-sweep fallback remains possible. Existing tests prove an expired catalogue stays in use and no refresh job begins before sweep completion, and the worker result replaces it only after the guarded callback (`test_expired_catalogue_waits_for_full_sweep_then_swaps_after_worker`). V1 migration has analogous gating (`window.py:8742-8814`). |

The global catalogue is intentionally month-aged rather than invalidated for every server DB update. A changed server's preview and filter index become current immediately; autocomplete may show the previous catalogue until its scheduled refresh. This is pre-existing cache policy and was not altered. Source review and detached tests establish the above logic; they are not display-backed UI or real startup verification.

## Outstanding Last Joined test failure

`tests/test_last_joined_calendar.py::test_map_and_eligible_live_tick_check_rollover` constructed a `SimpleNamespace` for `_browser_live_tick()` without `_maybe_schedule_mod_suggestion_refresh`. The tick calls that method on the active path before refreshing the calendar (`window.py:7556-7563`); the real `DZLLWindow` implements it (`window.py:8666`). The fixture therefore raised `AttributeError` before reaching the behavior it intended to test. This is a stale test double, not a newly introduced production missing-method path. The test double now supplies a recording no-op scheduler and asserts it is called on the active tick and not on the paused tick. The formerly failing test passes without deselection, skip, suppression, xfail, or production change.

## Runtime and memory findings

Maintainer-supplied 11,980-server measurements: original GTK DB callback **4,140 ms**, current warm callback **about 1,678 ms**; original main-loop stall **about 4,222 ms**, current stall **about 1,837 ms**. Per-server hits were **99.77–100%**, warm per-server processing **about 310–320 ms**, and global suggestion processing **about 360 ms**. These are runtime observations supplied for this pass; no runtime benchmark was repeated here. They establish a substantial warm-path improvement while leaving roughly 1.8 seconds of main-loop blocking.

The reported process peak RSS rose from about **441 MiB** near initial cache application to about **743 MiB** around deferred publication in one run, a difference of about **302 MiB** between high-water observations. Peak RSS (`VmHWM`/`ru_maxrss`) is the historical process maximum, not current resident memory and not memory attributable solely to the cache. The publication worker's `server_mod_cache.stage()` converts retained records to a second JSON-friendly dict/list structure, creates a JSON string, UTF-8 payload bytes and framed bytes, then writes a temporary file (`server_mod_cache.py:129-190`). These simultaneous objects make a transient increase plausible. The source retains the candidate and loaded-record maps while a publication is pending (`window.py:5855-5882`), and other browser/GObject/global-catalogue/A2S allocations coexist.

One isolated offline proxy used the existing 11,874-row SQLite snapshot via read-only immutable SQLite and staged 11,874 cache entries to a temporary directory. Before staging, sampled `VmRSS` was **249,616 KiB**; the observed staging peak was **302,548 KiB** (about **52 MiB** higher), and process `VmHWM` reached **323,328 KiB** (about **72 MiB** above the pre-stage sample). After staging and GC, sampled `VmRSS` was **259,928 KiB**. Sampling can miss short peaks. This simplified process supports temporary allocation as a contributor but does **not** explain the full roughly 302 MiB runtime high-water difference, identify the specific owning objects, or establish steady-state growth. It is not a GTK startup memory benchmark. The earlier isolated full-cache artifact was 21,890,210 bytes for 11,874 entries, within the 64 MiB/50,000-entry bounds.

For attribution, run controlled cold and warm starts with the same DB/global catalogue and sample time-aligned `VmRSS`, `VmHWM`, and preferably private RSS/USS around DB apply, 25-second stage, and publication. Record active browser count, A2S progress, generation, cache entry/file sizes, and stage duration; collect Python allocation tracing or heap snapshots only if the rise repeats. Compare current RSS after publication as well as peak. A single high-water jump cannot justify a cache-memory fix.

## Verification commands and results

- `python -m pytest -q tests/test_last_joined_calendar.py::test_map_and_eligible_live_tick_check_rollover` — **1 passed**.
- `python -m pytest -q tests/test_browser_server_ingestion_validation.py::test_server_mod_cache_warm_selected_chips_filter_like_cold` — **1 passed**.
- `python -m pytest -q tests` — **2,727 passed, 24 skipped, 1 PyGObject deprecation warning**; no failures or deselections. The skipped cases are environment/display-dependent and do not count as executed coverage.
- `python -m py_compile tests/test_last_joined_calendar.py tests/test_browser_server_ingestion_validation.py src/dzll_launcher/window.py src/dzll_launcher/server_mod_cache.py src/dzll_launcher/mod_suggestion_cache.py` — passed.
- `git --no-pager diff --check` — passed. Only permitted read-only Git inspection commands were used.

The test suite isolates HOME/XDG before application imports through `tests/conftest.py`; cache tests use detached rows and `tmp_path`. No real user cache/profile was written by tests.

## Next optimisation stage

Keep PR16-01 **IN PROGRESS**. The remaining main-thread budget needs phase attribution in a controlled runtime profile. In particular, compare `server_object_construction`, `source_store_insertion`, row scalar conversion, sort-key snapshot, model/filter rebuild, sorter notification, and global suggestion loading within the same run (`window.py:6054-6138`, `startup_perf.py`). Global v2 restoration still costs about 360 ms in the supplied runtime; verify read/decode/integrity/restoration subphases before changing it. The roughly 310–320 ms per-server warm path is already far below the former 1,322 ms rebuild. Measure the 25-second publication window for a second stall and memory behavior, then choose one narrow next change based on the dominant measured phase. No threading or model ownership change was made here.
