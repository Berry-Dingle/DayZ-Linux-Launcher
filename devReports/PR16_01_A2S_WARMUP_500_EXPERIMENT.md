# PR16-01 — Experimental A2S Warmup Reduction

Date: 2026-10-11  
Status: **IN PROGRESS**  
Runtime acceptance: **pending maintainer benchmark**

## Scope and supplied measurements

This task adds a separate, default-off startup readiness experiment:

```text
DZLL_A2S_WARMUP_500=1
```

The change does not reduce the full A2S sweep, alter its concurrency or query timeout, add queries, add BattleMetrics ranking logic, change GTK batching, change deferred suggestion restoration, or modify visible-list live-update policy. DZLL, Steam and live benchmarks were not launched. No network operation, dependency installation, Forge access, PR16 review-clone access or mutating Git command was used.

The maintainer supplied this recent 12,047-server profile:

- database/list publication: about 7.70 seconds;
- overlay dismissal: about 15.81 seconds;
- post-publication wait: about 8.11 seconds;
- existing warmup cohort: 1,410 servers.

These values are inputs to the experiment. This task produced no new runtime timing result.

## Verified current queue mechanics

### Queue construction

`read_servers_from_db()` returns rows ordered by `players DESC, ping ASC`. Successful database application creates one `_obj_by_key` entry for each canonical endpoint key. `_finish_db_rows_application()` then performs a stable sort of those keys using `_bm_live_group()`:

1. `bm_rank <= 100`;
2. `bm_rank <= 1000`;
3. `bm_rank <= 2000`;
4. all remaining or invalid ranks.

Python's stable sort retains the existing database/insertion order inside each band. The current warmup count is the number of keys in the first two bands. Because those bands sort first, `keys[:warmup_count]` is the existing eligible warmup prefix. This is a coarse priority queue, not an exact rank-ordered recommendation list.

The experiment therefore takes the first 500 distinct keys from that existing eligible prefix. It does not score, sort or otherwise reinterpret those endpoints.

### Dispatch and completion

`_submit_startup_rest_batches()` places the complete ordered sweep in a `deque`. `_pump_startup_rest_sweep()` pops from the left and keeps the existing `STARTUP_LIVE_REST_WORKERS` concurrency. Each worker calls the existing `query_server_live()` with the existing startup timeout and posts its future completion back through `GLib.idle_add()`.

`_on_startup_rest_future_done()` increments the completed-attempt count before examining the result payload. A normal online response, an `ok=False` offline/failure response, a query timeout returned as failure, and an exceptional future all count once when their future completes. Submission failure and a missing/invalid object do not count toward warmup readiness because no A2S attempt completed; the unchanged 20-second fallback prevents an indefinite overlay in those cases.

The authoritative production source is `_obj_by_key`, whose keys are already distinct. The experimental path additionally removes repeated keys at the scheduler boundary while retaining first occurrence order, so an abnormal direct caller cannot dispatch the same endpoint twice.

### Reveal, sort and continued sweep

The existing 20-second warmup timer runs concurrently with the A2S work. Readiness occurs at the completion threshold or timeout, whichever arrives first. The one-time completion path:

1. rejects a stale A2S generation;
2. prevents a second reveal;
3. removes the timeout and pending flush source;
4. flushes accumulated live results;
5. calls the existing final ping-sort helper;
6. completes startup presentation and dismisses the overlay.

The final sort changes to ascending ping only when the user has not already changed the sort and the current sort is still the initial descending-ping state. It does not replace a user-selected sort. The full sweep queue and its in-flight futures remain active after reveal. Late startup results update row properties through the existing stable-list path and do not continuously reorder or splice the visible list. The independent full-sweep completion marker is emitted only when the queue, in-flight work and result buffer are all empty.

Generation checks discard callbacks from a superseded database application. Shutdown increments the startup A2S generation and removes the warmup timer, so late callbacks cannot advance or reveal the old generation.

## Exact files changed

| File | Change |
|---|---|
| `src/dzll_launcher/window.py` | Add the opt-in gate, experimental cohort selection, 60% ceiling threshold, duplicate protection, reveal-reason and elapsed-time instrumentation. A direct class-method call for existing startup-batch shutdown cancellation keeps partial shutdown test doubles compatible without changing runtime behavior. |
| `tests/test_a2s_warmup_500.py` | Add focused tests for default parity, queue-prefix selection, ceiling thresholds, empty/small cohorts, duplicate dispatch protection, failure/timeout accounting, timeout reveal, generations, shutdown, manual replacement, user sort and full-sweep continuation. |
| `devReports/PR16_01_A2S_WARMUP_500_EXPERIMENT.md` | Record verified mechanics, implementation, validation, benchmark procedure, risks and rollback. |

The tracker was not changed and PR16-01 remains **IN PROGRESS**. `MEMORY.md` and Forge were not modified. Existing GTK batching and deferred-suggestion behavior was preserved.

## Default and experimental behavior

### Default: switch unset or not equal to `1`

- The complete `bm_rank <= 1000` eligible prefix remains the warmup cohort.
- The threshold remains `ceil(cohort_count × 65 / 100)`.
- The existing 20-second timeout remains the fallback.
- The complete dispatch queue and its ordering are unchanged.
- Manual database update behavior remains unchanged.

### Experimental: `DZLL_A2S_WARMUP_500=1`

For an ordinary initial successful startup while startup presentation is incomplete:

- the cohort is the first `min(500, distinct eligible endpoints)` entries in existing dispatch-priority order;
- the threshold is `ceil(cohort_count × 60 / 100)`;
- a 500-entry cohort therefore reveals at 300 completed attempts;
- a smaller positive cohort uses ceiling rounding;
- an empty cohort is already at its zero threshold and reveals immediately;
- the full distinct endpoint queue is still dispatched in its existing order;
- A2S concurrency, timeout, query implementation, live result application and full-sweep completion are unchanged.

The switch is intentionally independent of `DZLL_STARTUP_BATCHING`. Either loader can reach the experimental readiness gate. The experiment does not require or enable GTK batching.

### Unsupported or replacement paths

Cached fetch failure does not start this A2S gate and retains its current cached reveal. A provisional watchdog load also retains its current path. If a final database replaces an already revealed provisional list, or a manual database update occurs after startup presentation completed, the scheduler uses the established full eligible cohort and 65% threshold even when the experiment variable is set. This keeps the experiment limited to the initial startup readiness decision and avoids changing replacement/manual overlay behavior.

## Completion, timeout and lifecycle semantics

- **Threshold:** every completed future whose endpoint belongs to the selected cohort increments the cohort count once. Result success is irrelevant to readiness.
- **Timeout:** the unchanged `STARTUP_WARMUP_MAX_WAIT_SECS = 20` timer reveals with the attempts completed so far.
- **Empty cohort:** reveal reason is `threshold`, because the zero threshold is already satisfied; no warmup timer is registered.
- **Duplicate keys:** the experimental queue keeps the first occurrence and removes later occurrences before dispatch. The cohort uses the same first-occurrence rule.
- **Replacement:** starting another sweep increments the generation. Old callbacks and old timeout callbacks return without applying, counting or revealing.
- **Shutdown:** cleanup increments the generation and removes the timer. The queue pump already rejects shutdown state.
- **Late results:** current-generation results continue to flush and update the committed objects after overlay dismissal; they do not trigger another warmup reveal.
- **Full sweep:** reveal does not clear the queue or cancel workers. Completion remains separately tracked by `_suggestion_sweep_complete` and `a2s_sweep_completion`.

## Performance instrumentation

All added timing fields are emitted only when `DZLL_PERF_LOG=1`. There is no per-server log line.

### `a2s_warmup_start`

Fields include:

- `warmup_mode=default|experimental_500_60`;
- `eligible_rows` before the experimental cap;
- `warmup_rows` and `cohort_count` after selection;
- `completion_percent`;
- `completion_target`;
- `total_rows` in the full sweep;
- `a2s_generation` and the existing database-application context.

### `a2s_warmup_threshold`

Records the actual cohort `completed` count, `target`, `cohort_count`, mode, generation and `reason=threshold|timeout`.

### `a2s_warmup_reveal`

Records the same actual count and target, `reveal_reason`, and `publication_to_overlay_dismissal_ms`. The publication timestamp is taken when the fully constructed list enters post-load coordination; the elapsed value ends after the existing presentation-completion call returns. This is an overlay lifecycle measurement, not proof that pixels were drawn.

### `a2s_sweep_completion`

Remains independent of reveal and now also records warmup mode and cohort count beside full `completed` and `total` values. This allows a trace to show that the approximately 12,000-server sweep continued after the 300-attempt experimental reveal.

The existing `startup_overlay_dismissal` and `initial_next_frame_tick` markers remain unchanged. The next-frame marker is a frame-clock callback, not a rendered-pixel measurement.

## Offline verification

Focused experiment and adjacent startup tests:

```text
python -m pytest -q tests/test_a2s_warmup_500.py tests/test_startup_warmup_stable_live_list.py tests/test_startup_perf_instrumentation.py
45 passed, 1 warning
```

Broader startup/cache/filter regression set:

```text
python -m pytest -q tests/test_a2s_warmup_500.py tests/test_startup_warmup_stable_live_list.py tests/test_startup_perf_instrumentation.py tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py tests/test_browser_server_ingestion_validation.py tests/test_mod_suggestion_cache.py tests/test_pin_favourites_filter_priority.py tests/test_last_joined_calendar.py tests/test_core_startup_robustness.py
262 passed, 13 skipped, 1 warning
```

Maintained root test suite:

```text
python -m pytest -q tests
2768 passed, 24 skipped, 1 warning
```

The 24 skips require unavailable display-backed execution and are not executed coverage. The warning is the existing PyGObject `GLib.unix_signal_add_full` deprecation warning.

A bare `python -m pytest -q` was also attempted and failed during collection because untracked packaged release trees under `src/DayZ-Linux-Launcher-0.4.0-beta` and `src/DayZ-Linux-Launcher-0.4.1-beta` duplicate the root test module names and profile boundary. The maintained `tests`-only invocation above excludes those protected package artifacts and passed.

Additional checks:

```text
PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-a2s-pycache python -m py_compile src/dzll_launcher/window.py tests/test_a2s_warmup_500.py
git --no-pager diff --check
```

Both passed with no output. No test used the real user HOME; the repository `tests/conftest.py` installed its isolated HOME/XDG profile before application imports.

## Focused coverage

The new tests verify:

- switch-off parity with the full eligible cohort and 65% ceiling threshold;
- first-500 selection from the supplied dispatch prefix without new ranking;
- exactly 300 completions for 500 candidates;
- ceiling results for 1, 2, 3, 7 and 499 candidates;
- immediate empty-cohort reveal without a timer;
- first-occurrence queue order and one dispatch per distinct endpoint;
- offline results and future exceptions counting as completed attempts;
- timeout fallback, one reveal and explicit timeout instrumentation;
- stale generation and shutdown callback rejection;
- established default behavior for post-startup/manual replacement;
- no queue cancellation at the early reveal threshold;
- preservation of a user-selected sort at reveal.

Existing adjacent tests cover coarse-band queue construction, flush/sort/reveal order, stable visible membership after late startup results, active filters, favourites/trusted bands, explicit user rebuilds, Refresh All isolation, cached fallback, GTK batching, deferred suggestions, selected mod chips and startup instrumentation.

## Known risks and limitations

- The first 500 are the first entries in the existing coarse-band priority queue. They are not an exact top-500 BattleMetrics ranking and should not be described as recommendations.
- Earlier reveal means fewer current A2S observations feed the one-time ping sort. More rows can initially retain database ping/status values.
- Stable-list policy prevents continual structural reshuffling. Late A2S results update properties, but filter membership and list order do not continuously rebuild; explicit filter/sort/refresh actions apply the later values.
- Favourites and Previously Joined outside the cohort remain present according to the published database model but may not have fresh A2S state at reveal.
- Failed and timed-out completed attempts intentionally satisfy readiness. The gate measures completed work, not successful online servers.
- If executor submission repeatedly fails, those non-attempts do not satisfy the cohort threshold; the 20-second timeout remains the recovery path.
- The full A2S workload and its CPU, callback and network cost are unchanged. This experiment can reduce post-publication waiting but cannot improve database publication time or the remaining GTK publication stall.
- Runtime impact depends on the actual completion distribution. If the current 20-second timeout is already the usual reveal cause, the smaller cohort may provide little or inconsistent benefit.
- No display-backed spinner, frame, input-latency or live A2S benchmark was run in this task.

## Isolated-profile benchmark commands

Use the same prepared 12,047-row database, settings, warm cache contents, GTK/Python build, display session and otherwise idle machine for every run. Make six byte-identical copies of an existing isolated seed profile before launching because normal cache maintenance can mutate a run profile. Keep `DZLL_STARTUP_BATCHING=1` in both arms to isolate the A2S gate against the supplied recent batched baseline.

Example preparation, replacing `/path/to/prepared-dzll-profile` with the maintainer's existing isolated seed:

```bash
for run in 1 2 3; do
  cp -a /path/to/prepared-dzll-profile "/tmp/dzll-pr16-a2s-default-${run}"
  cp -a /path/to/prepared-dzll-profile "/tmp/dzll-pr16-a2s-500-${run}"
done
```

Default runs:

```bash
env -u DZLL_A2S_WARMUP_500 HOME=/tmp/dzll-pr16-a2s-default-1 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-default-1/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-default-1/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-default-1/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-default-1/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-default-1.log
env -u DZLL_A2S_WARMUP_500 HOME=/tmp/dzll-pr16-a2s-default-2 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-default-2/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-default-2/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-default-2/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-default-2/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-default-2.log
env -u DZLL_A2S_WARMUP_500 HOME=/tmp/dzll-pr16-a2s-default-3 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-default-3/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-default-3/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-default-3/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-default-3/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-default-3.log
```

Experimental runs:

```bash
HOME=/tmp/dzll-pr16-a2s-500-1 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-500-1/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-500-1/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-500-1/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-500-1/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 DZLL_A2S_WARMUP_500=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-500-1.log
HOME=/tmp/dzll-pr16-a2s-500-2 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-500-2/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-500-2/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-500-2/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-500-2/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 DZLL_A2S_WARMUP_500=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-500-2.log
HOME=/tmp/dzll-pr16-a2s-500-3 XDG_CONFIG_HOME=/tmp/dzll-pr16-a2s-500-3/.config XDG_DATA_HOME=/tmp/dzll-pr16-a2s-500-3/.local/share XDG_STATE_HOME=/tmp/dzll-pr16-a2s-500-3/.local/state XDG_CACHE_HOME=/tmp/dzll-pr16-a2s-500-3/.cache PYTHONPATH=src DZLL_PERF_LOG=1 DZLL_STARTUP_BATCHING=1 DZLL_A2S_WARMUP_500=1 python -m dzll_launcher 2>&1 | tee /tmp/dzll-pr16-a2s-500-3.log
```

For each run, record:

- `a2s_warmup_start`: mode, eligible count, selected cohort and target;
- `a2s_warmup_threshold`: actual completed attempts and threshold/timeout reason;
- `a2s_warmup_reveal`: warmup elapsed and `publication_to_overlay_dismissal_ms`;
- `startup_overlay_dismissal` and `initial_next_frame_tick` timestamps;
- `a2s_sweep_completion`: full completed/total count and elapsed time;
- external video observations for spinner continuity, first visible server frame and first responsive interaction;
- final source/visible row counts, active filter/chip state, sort selection and whether the user changed sorting during warmup.

Compare all three values and the median for publication-to-overlay dismissal. Accept the experiment only if the reduction is repeatable beyond run-to-run variation, the browser becomes interactively usable sooner, the first visible list is correctly filtered/sorted, and the full sweep reaches the same total without duplicate dispatch. Treat threshold and timeout runs as different populations rather than averaging them together.

## Immediate rollback

Unset `DZLL_A2S_WARMUP_500` or set it to any value other than `1`. The next process uses the existing complete eligible cohort and 65% threshold. The experiment writes no new persistent format or setting, so rollback requires no cache deletion or data migration.

Rollback the experiment if runtime evidence shows insignificant or inconsistent readiness improvement, increased incorrect/stale visible state, duplicate or missing A2S dispatch, failure to complete the full sweep, incorrect final sort/filter behavior, a generation/shutdown reveal, or an overlay lifecycle regression. PR16-01 should remain **IN PROGRESS** until controlled runtime and display-backed acceptance evidence is recorded.
