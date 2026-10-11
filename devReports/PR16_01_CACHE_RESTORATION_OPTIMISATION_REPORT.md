# PR16-01: suggestion-cache restoration optimisation

Date: 2026-10-11  
Status: implementation complete; runtime benchmark pending. PR16-01 remains **IN PROGRESS**.

## Scope and changed files

- `src/dzll_launcher/mod_suggestion_cache.py` — aggregate, opt-in load-stage timings and in-place version-1 index restoration.
- `src/dzll_launcher/window.py` — include the stage timings in the existing `mod_suggestion_cache_load` startup diagnostic when `DZLL_PERF_LOG=1`.
- `tests/test_mod_suggestion_cache.py` — frozen pre-change restoration reference, differential valid/malformed cases, and opt-in profiling checks.
- `tests/test_startup_perf_instrumentation.py` — enabled/disabled window-path diagnostic and control-flow check.
- `devReports/PR16_IMPROVEMENTS_TRACKER.md` — dated progress entry; status remains IN PROGRESS.
- This report.

The optimisation changes neither the version-1 disk format nor its writer. It changes no DB, A2S, UI, refresh, thread, join, Steam, or Companion path. No Git state or history was modified.

## Implementation

`load_suggestion_cache()` still checks the file-size limit, reads at most `MAX_CACHE_BYTES + 1`, validates the document/version/timestamp/digest, computes the SHA-256 of canonical index JSON, then restores the index; failures remain cache misses (`src/dzll_launcher/mod_suggestion_cache.py:119-190`). The format and limits remain `FORMAT_VERSION=1`, 64 MiB, 100,000 entries and 500,000 lookup keys (`mod_suggestion_cache.py:19-23`). Saving remains unchanged (`mod_suggestion_cache.py:201-220`).

Restoration now validates decoded entry dictionaries and converts their four list fields to tuples **in those dictionaries**, retaining the decoded dictionaries, text values, entry ordering and the final `mods` tuple (`mod_suggestion_cache.py:71-90,106-113`). Both decoded lookup dictionaries are retained; each posting list is validated against the entry count, including exact integer/index checks, then replaced with a tuple in the same dictionary (`mod_suggestion_cache.py:58-69,92-104`). The query-result cache is a new empty dictionary on each restoration (`mod_suggestion_cache.py:106-113`). The prior code allocated new entry dictionaries and new prefix/exact map dictionaries. Mutation on a malformed decoded index is confined to the disposable decoded document, which is discarded on a cache miss.

With `DZLL_PERF_LOG=1`, the single existing `mod_suggestion_cache_load` phase contains `result`, `expired`, `entries`, `bytes_read`, and aggregate `*_ms` fields for `file_stat_read`, `json_decode`, `canonical_json`, `sha256`, `entry_restore`, `prefix_restore`, `exact_restore`, and `total` (`window.py:8457-8472`). The existing phase logger also supplies phase duration and cumulative startup time. The loader measures with `time.monotonic_ns()` only when given a callback; the normal disabled path has no load-stage timing calls or per-entry logging (`mod_suggestion_cache.py:123-138,184-193`). Missing, incompatible and corrupt files also produce one aggregate miss line when enabled. The callback is insulated from cache recovery by catching its exceptions (`mod_suggestion_cache.py:184-193`). Stage boundaries exclude small intervening validation and object setup, so their sum need not equal `total_ms`.

## Behavioural equivalence evidence

The differential test retains a frozen copy of the pre-change version-1 restoration contract (`tests/test_mod_suggestion_cache.py:22-64`). It compares complete restored indexes and repeated suggestion queries for representative, empty and adversarial data, including aliases, workshop IDs, prefixes and multiple tokens (`tests/test_mod_suggestion_cache.py:68-101`). It checks tuple field types, entry order, reuse of decoded entry/map dictionaries, and a fresh empty query cache. Malformed bodies, including bad posting indices in both maps, compare rejection against the frozen implementation and are confirmed safe cache misses when wrapped in a correctly checksummed version-1 document (`tests/test_mod_suggestion_cache.py:104-120`). Existing cache tests continue to cover cold/warm/expired files and corrupt/incompatible documents (`tests/test_mod_suggestion_cache.py:145-180`).

The opt-in test checks the aggregate stage report, missing-file report, and that the disabled loader never calls its monotonic timer (`tests/test_mod_suggestion_cache.py:123-142`). The window test checks that the existing phase and its fields appear only when enabled, while the loaded index and ready state follow the same path (`tests/test_startup_perf_instrumentation.py:41-67`). These tests establish data and local control-flow equivalence; they do not substitute for an actual startup benchmark.

## Offline verification

Command: `python -m pytest -q tests/test_mod_suggestion_cache.py tests/test_browser_server_ingestion_validation.py tests/test_mod_name_normalization.py tests/test_mod_name_resolver.py tests/test_mod_metadata_bulk_names.py tests/test_server_mod_popup_sort_cleanup.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py`

Result: **287 passed, 13 skipped**, one PyGObject deprecation warning, in 0.55 s. `python -m py_compile` passed for both changed source files and both changed test files. `git --no-pager diff --check` passed. Tests used the suite's isolated HOME/XDG setup; DZLL, Steam, and live A2S were not launched, and no dependencies were installed.

## Remaining risk and benchmark procedure

The previous audit's isolated, proxy-data experiment suggested that in-place restoration could reduce restoration time, but the actual 11,980-server warm-start gain and memory effect remain unmeasured. JSON decoding, canonical serialization, and SHA-256 remain required for version-1 integrity; the new stage fields will reveal their actual shares. The aggregate timings themselves add a small opt-in measurement cost. No persistent cache format or monthly refresh lifecycle was changed: an expired index remains usable while deferred refresh waits for the complete initial A2S sweep; background refresh, generation and shutdown guards retain their prior behaviour.

For acceptance, use the same 11,980-server DB, version-1 cache file, settings, machine, and startup procedure used for the 710–808 ms / 3,198 ms warm-cache baseline. Preserve a copy of that cache and confirm it loads unchanged. Run at least three warm-cache launches with `DZLL_PERF_LOG=1` on the prior revision and at least three on this revision under comparable idle conditions. Record the eight stage fields, `mod_suggestion_cache_load` duration, global suggestion phase, GTK DB callback, GTK main-loop stall, overlay dismissal, and peak RSS; compare medians and ranges rather than a single run. Check autocomplete queries covering ID, alias, exact, prefix, token and repeated lookup. Separately exercise missing/corrupt and expired-cache startups and verify deferred refresh only after the complete initial sweep while the regular five-second and Companion A2S operations continue. Keep PR16-01 IN PROGRESS until that runtime comparison and regression check are complete.
