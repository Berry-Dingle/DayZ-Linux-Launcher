# PR16-01 startup baseline instrumentation

Date: 2026-10-10  
Status: instrumentation added; baseline not yet measured; PR16-01 remains **IN PROGRESS / ADAPT**.

## Files changed

- `src/dzll_launcher/app.py` — activation, construction, presentation and construction-failure markers.
- `src/dzll_launcher/window.py` — startup phase markers and cumulative timestamp on existing main-loop stall log.
- `src/dzll_launcher/startup_perf.py` — small monotonic, fail-safe, single-line phase logger.
- `tests/test_startup_perf_instrumentation.py` — offline opt-in and control-flow tests.
- `devReports/PR16_IMPROVEMENTS_TRACKER.md` — PR16-01 status set to IN PROGRESS, instrumentation progress and dated log entry; not closed.
- `devReports/PR16_01_STARTUP_INSTRUMENTATION_REPORT.md` — this report.

The pre-existing untracked `MEMORY.md`, reports, packages and release directories were preserved. No Git state/history was changed.

## Measurements and how to read them

Start a **later authorized** baseline run with `DZLL_PERF_LOG=1` in the process environment. The new output consists of one `[STARTUP-PERF]` line per phase, with `phase=`, `duration_ms=`, `startup_ms=` (monotonic elapsed since first application activation), `thread=`, and relevant row counts/outcome fields. Only phase and aggregate row measurements are emitted; no server-specific timing is printed. The helper uses `time.monotonic_ns()` and catches diagnostic errors (`src/dzll_launcher/startup_perf.py:7-24`). With the flag unset, no new phase lines or per-row clock reads occur. The pre-existing `[PERF] main loop stall` diagnostic remains enabled by the same flag and now includes `startup_ms` (`src/dzll_launcher/window.py:1316-1336`).

| Phase names | Meaning | Source |
|---|---|---|
| `activation`, `window_construction`, `window_construction_failed`, `window_present` | First activation marker, constructor duration/failure and `present()` call duration. | `src/dzll_launcher/app.py:67-111` |
| `db_startup_submit`, `db_worker_start`, `db_worker_fetch`, `db_worker_read`, `db_worker_read_cached`, `db_worker_total` | DB scheduling marker, worker start, fetch, normal/cached read and entire worker lifetime (including existing update check). | `src/dzll_launcher/window.py:5406-5545` |
| `db_gtk_callback_entry`, `db_gtk_completion`, `db_watchdog_gtk_read` | Completion callback entry; total GTK apply duration including downstream row/model work, with provisional/fetch outcome; exceptional watchdog SQLite read on GTK. | `src/dzll_launcher/window.py:5420-5445`, `5496-5506` |
| `endpoint_validation`, `map_preparation` | Whole-input endpoint pass, then map aggregation/dropdown update. | `src/dzll_launcher/window.py:5723-5750` |
| `row_conversion`, `mod_preview_index`, `server_object_construction`, `source_store_insertion`, `row_loop_total` | Aggregate row-loop cost, with separate mod JSON preview/search indexing, GObject constructor/properties and source `Gio.ListStore.append`; `row_conversion` is the residual after subtracting these three components, so also contains loop/dictionary/timing overhead. | `src/dzll_launcher/window.py:5788-5933` |
| `global_mod_suggestion_index`, `sort_key_snapshot` | Global mod index and whole-source sort-key snapshot. Empty input still logs index duration. | `src/dzll_launcher/window.py:5794-5801`, `5934-5943` |
| `initial_filter_scan`, `initial_sort`, `initial_visible_store_splice`, `initial_visible_store_rebuild` | First `load` rebuild: full filter scan, Python sort, complete visible-store replacement and enclosing rebuild. Each includes relevant source/matched/inserted counts. | `src/dzll_launcher/window.py:6078-6399` |
| `warmup_filter_scan`, `warmup_sort`, `warmup_visible_store_splice`, `warmup_visible_store_rebuild` | Final ping-sort rebuild following successful A2S warmup. Cached failure's equivalent final sort uses the `cached_final_` prefix. | `src/dzll_launcher/window.py:6078-6399`, `6616-6646` |
| `a2s_warmup_start`, `a2s_warmup_threshold`, `a2s_warmup_reveal`, `a2s_sweep_completion` | Queue start, threshold/timeout decision with completed/target counts, reveal path after flush/sort, and completion of the whole A2S sweep (possibly after reveal). | `src/dzll_launcher/window.py:6552-6599`, `6616-6646`, `6769-6794` |
| `startup_overlay_dismissal` | Duration of the existing coordinator/UI dismissal path; `startup_ms` on this line is activation-to-overlay-hidden, the current proxy for first usable list. | `src/dzll_launcher/window.py:5377-5398` |

Durations of enclosing phases overlap their child phases; do not sum every line. The row aggregate measurements invoke the monotonic clock around operations only when profiling is enabled and produce one line per category, not per row. This can add profiling overhead to enabled runs, so compare similarly instrumented runs. Main-loop stall output indicates callback lateness over the existing threshold, not which phase caused it.

## Behaviour and failure paths

No executor, query, model, filtering, sorting, warmup threshold, reveal condition or startup scheduling rule was changed. Logging is conditional on `DZLL_PERF_LOG=1`. The existing cached-DB worker route logs `db_worker_read_cached`; the 30-second provisional watchdog logs `db_watchdog_gtk_read`; GTK apply logs `fetched_ok` and `provisional`. Empty-row startup logs global index and overlay dismissal if the dimmer was visible. Worker errors still reach the existing completion callback, and the diagnostic helper cannot raise into startup (`src/dzll_launcher/window.py:5406-5553`, `src/dzll_launcher/startup_perf.py:7-24`).

## Verification

- `python -m py_compile src/dzll_launcher/app.py src/dzll_launcher/window.py src/dzll_launcher/startup_perf.py tests/test_startup_perf_instrumentation.py` — passed.
- `python -m pytest -q tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py tests/test_pin_favourites_filter_priority.py tests/test_column_view_live_time_notify.py tests/test_companion_schema4_startup_boundary.py` — **101 passed, 13 skipped**, one PyGObject deprecation warning, in 0.24 seconds. Skipped display-dependent tests are not claimed as executed coverage. The focused tests compare enabled/disabled DB apply, row loading, visible rebuild and warmup order, and check line format (`tests/test_startup_perf_instrumentation.py:1-159`). Tests ran with the repository's disposable HOME/XDG and protected-root hook (`tests/conftest.py:1-82`).
- `git --no-pager diff --check` — passed. Final diff reviewed. No mutating Git command was used.

No DZLL process, Steam, live A2S query, dependency install or performance benchmark was run.

## Limits and next baseline procedure

Instrumentation stops at overlay dismissal for the first-list proxy; it does **not** measure first rendered frame, input responsiveness, peak RSS or total process exit time. `startup_ms` at `a2s_sweep_completion` measures full sweep completion and can be later than usable list. Source-store clearing is outside `source_store_insertion`; `visible_store_splice` times the Python/GTK call and immediate surrounding work, not deferred layout/render. Pre-window import cost and GTK stalls before the window's diagnostic timer starts are unmeasured. The 30-second watchdog and network/update-check behaviour make uncontrolled runs unsuitable for attribution. No actual baseline numbers are asserted here.

For the next, separately authorized measurement session: use the official `src/dzll_launcher` tree, a detached representative ~12,000-row DB fixture and disposable HOME/XDG profile; keep row/mod counts, settings, GTK/Python versions, hardware and cache state fixed. Run several repetitions with `DZLL_PERF_LOG=1`, capture complete output, and compare phase durations, `startup_ms` at overlay dismissal, stall lines and full-sweep completion. Keep DB/update/A2S input controlled for local-phase comparisons, then measure network/warmup variability separately. Record peak RSS externally and observe first rendered, responsive list independently. Use the measured dominant phase to decide whether a narrow optimisation experiment is warranted; retain PR16-01 IN PROGRESS until that evidence exists.
