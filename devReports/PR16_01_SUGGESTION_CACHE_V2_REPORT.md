# PR16-01: version-2 suggestion-cache format

Date: 2026-10-11  
Status: implemented for offline verification; **IN PROGRESS** pending controlled runtime benchmarks.

## Files changed

- `src/dzll_launcher/mod_suggestion_cache.py` — version-2 byte-framed format, v1 reader, exact-byte integrity check, atomic v2 writer, staged migration helper and aggregate load profiling.
- `src/dzll_launcher/window.py` — format diagnostic and deferred worker migration with generation/shutdown guards.
- `tests/test_mod_suggestion_cache.py` — v1/v2 compatibility, equivalence, corruption, atomicity and migration lifecycle tests.
- `tests/test_startup_perf_instrumentation.py` — v2 format and stage-field checks.
- `devReports/PR16_IMPROVEMENTS_TRACKER.md` — dated PR16-01 progress; status remains IN PROGRESS.
- This report.

No unrelated source, profile data, package output, release artifact, Git history or Git state was changed. DZLL, Steam and live A2S were not run.

## Format and integrity design

The established XDG cache path remains `dzll/mod_suggestions_v1.json` so existing installations find their v1 file without a path search or duplicate persistent copy (`mod_suggestion_cache.py:32-34`). The filename is now historical; the file content identifies its format. A v2 file is a byte-framed record:

```
DZLL-MOD-SUGGESTIONS-V2\n
<64 lowercase hexadecimal SHA-256 bytes>\n
<UTF-8 JSON payload bytes>
```

The payload is a JSON object with exactly `created_at` and `index`; the index still holds `mods`, `count`, `prefix_map` and `exact_compact_map`. The digest covers the **exact stored payload bytes**, including the timestamp. The loader validates the framing and digest before calling `json.loads` on the payload (`mod_suggestion_cache.py:158-185`). It never canonicalizes a decoded v2 index to check integrity. The v1 branch retains its original envelope, canonical serialization and checksum validation (`mod_suggestion_cache.py:186-227`). Unknown future byte-framed versions and incompatible v1 document versions are misses. Both branches then use the same existing in-place entry/map restoration and validation, including all entry, posting-index and collection limits, and create a fresh empty query-result cache (`mod_suggestion_cache.py:64-118,228-234`). The 64 MiB total-file bound applies before decoding and to the entire written v2 record (`mod_suggestion_cache.py:24-26,145-157,246-257`). Missing, malformed, oversized, corrupt and incompatible files remain safe misses.

The writer serializes one payload, hashes those bytes and writes the framed file via a same-directory temporary file, flush/fsync and `os.replace`; a failed replacement leaves the prior file in place and removes the temporary file (`mod_suggestion_cache.py:246-280`). Cold builds and monthly refreshes now write v2 through the unchanged `save_suggestion_cache()` call sites. No pickle or executable deserialization is involved. The v2 framing adds only a small fixed header to the current JSON payload; it does not omit entry fields or lookup maps.

## Migration and lifecycle

A valid v1 file is immediately restored and remains the active catalogue. The loader returns its format and original `created_at` metadata (`mod_suggestion_cache.py:37-43,234`); the window marks migration pending only for v1 (`window.py:8479-8487`). The normal browser-live scheduling point checks for a completed initial sweep, or the established safe fallback after a failed/cancelled sweep, before submitting migration (`window.py:7407-7411,8593-8616`). The worker receives only the plain-Python restored index and timestamp, serializes a staged v2 file, and never touches GTK/GObject (`mod_suggestion_cache.py:283-291`). It does not rebuild from DB rows. Existing A2S refresh and Companion work continue. A valid expired v1 index remains available; migration precedes the existing monthly refresh, which remains subject to its full-sweep gate (`window.py:8517-8533`).

The GTK completion checks shutdown, DB generation and pending state before atomically replacing the v1 path. It discards stale staged files; a successful replacement clears pending state, so later launches read v2 and do not migrate again (`window.py:8627-8665`). Submission, worker and publication failures retain the v1 file. Nonexpired migration retries after 60 seconds; an expired-cache migration failure releases the existing monthly refresh path so it cannot remain stuck. Refresh retains its previous generation/shutdown guards and keeps the old index active until its own successful publication (`window.py:8536-8591`). The current per-server preview/search index and authoritative join-time mod preparation paths were not changed.

## Diagnostics

With `DZLL_PERF_LOG=1`, `mod_suggestion_cache_load` now reports `format=v1|v2|unknown`, `result`, `bytes_read`, and milliseconds for `file_stat_read`, `sha256`, `integrity`, `json_decode`, `index_restore`, the entry/prefix/exact restoration subdivisions, `canonical_json`, and `total`, alongside the existing phase duration and cumulative startup elapsed time (`mod_suggestion_cache.py:125-143,237-243`; `window.py:8461-8478`). A v2 hit has `canonical_json_ms=0`; v1 retains that stage for compatibility. `mod_suggestion_cache_migration_start` and `mod_suggestion_cache_migration_complete` report one aggregate migration duration/result when enabled (`window.py:8623-8664`). There is no per-entry logging or load-stage timing when disabled.

## Offline regression evidence

The v2 round trip compares the complete index and repeated exact/ID/alias/prefix/token suggestion results to the original builder. The existing frozen v1 restoration reference and malformed-body cases still pass. New tests verify v1 reading, staged v1-to-v2 replacement without a DB rebuild, timestamp/expiry retention, empty index, corrupt/tampered/truncated/future records, checksum verification before JSON decoding, a correctly checksummed invalid posting, atomic write failure preserving v1, post-sweep scheduling, duplicate prevention, generation/shutdown rejection, worker failure and retry (`tests/test_mod_suggestion_cache.py:22-439`). The window diagnostic test checks v2 format and aggregate stage fields (`tests/test_startup_perf_instrumentation.py:41-71`). Existing ingestion tests cover warm-cache reuse, corrupt-cache rebuilding and monthly refresh publication.

Focused command:

`python -m pytest -q tests/test_mod_suggestion_cache.py tests/test_browser_server_ingestion_validation.py tests/test_mod_name_normalization.py tests/test_mod_name_resolver.py tests/test_mod_metadata_bulk_names.py tests/test_server_mod_popup_sort_cleanup.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py`

Result: **301 passed, 13 skipped**, one PyGObject deprecation warning, 0.58 s. `python -m py_compile` passed for the two changed source and two changed test files. `git --no-pager diff --check` passed. The tests use the repository's isolated HOME/XDG guard. An exploratory run that additionally included the unrelated `tests/test_last_joined_calendar.py` yielded one existing fixture failure: `test_map_and_eligible_live_tick_check_rollover` constructs a `SimpleNamespace` without the previously required `_maybe_schedule_mod_suggestion_refresh` method, so execution stops before the new migration call. That fixture was left outside this scoped change.

## Remaining risks and runtime acceptance

The user-supplied three-run baseline for 11,980 rows is warm load **575–583 ms**, GTK DB callback **3,131–3,161 ms**, main-loop stall **3,226–3,318 ms**, with about **193 ms** JSON decoding, **218 ms** canonical re-serialization and **7 ms** SHA-256. These are pre-v2 observations; no v2 startup timing or peak-RSS measurement was made here. A v1 file still pays the one-time canonical check on its first read. After a successful deferred migration, v2 should remove that operation, but the actual gain may differ because payload byte slicing, allocation, page-cache state and GTK scheduling have not been benchmarked. Worker migration also consumes CPU/I/O after the sweep; it should be observed for effect on normal A2S responsiveness. Concurrent external writers to the same cache path are outside the current single-process generation guard.

For runtime acceptance, preserve the real 11,980-server DB/settings and a copy of the valid v1 file. On the same machine and comparable idle conditions, perform at least three `DZLL_PERF_LOG=1` launches with v1, allow one post-sweep migration, then at least three warm v2 launches. Confirm the file changes format only after migration completion, its timestamp and suggestions remain intact, and subsequent launches report `format=v2` without another migration. Record per-run cache `file_stat_read_ms`, `integrity_ms`, `json_decode_ms`, `index_restore_ms`, `canonical_json_ms`, `total_ms`, GTK DB callback, main-loop stall, overlay dismissal and peak RSS; compare medians and ranges to the supplied pre-v2 baseline. Check exact ID, alias, prefix, token, ranking and repeated queries in the running UI. Separately verify a corrupt/missing file's cold rebuild and an expired v1 file's continued autocomplete through full sweep, migration and monthly refresh while regular five-second and Companion A2S continue. Keep PR16-01 IN PROGRESS until those runtime and behavioural checks pass.
