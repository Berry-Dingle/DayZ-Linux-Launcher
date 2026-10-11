# PR16-01 — Final Integration and Commit-Readiness Audit

Date: 2026-10-11  
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`  
Baseline: `de504a4` (`main`, `origin/main`, tag `v0.4.1-beta`)  
Audit mode: read-only except for this requested report  
PR16-01 status: **IN PROGRESS**

## Executive disposition

The accumulated PR16-01 implementation is internally coherent, keeps GObject and GTK model work on the GTK thread, preserves the established synchronous paths, and passes the maintained offline suite. No critical or high-severity defect was found in the PR16-01 source or tests.

The work is **conditionally ready for one scoped commit as default-off experimental infrastructure**, but it is **not ready to become production-default behavior** and PR16-01 must remain **IN PROGRESS**. Before the scoped commit is made, the tracker must be updated to reflect the accumulated implementation and supplied runtime evidence. Two low-severity source-confirmed edge cases should also be recorded and preferably corrected with focused tests before enabling either experiment by default:

1. batched row failure/cancellation does not roll back the map-choice model that was updated before the row transaction; and
2. A2S full-sweep exhaustion is always instrumented as a `threshold` reveal even when executor submission/missing-object paths prevented the cohort target from being reached.

Neither issue was observed in the supplied successful runtime evidence. Neither corrupts the canonical server database or cache. They are still real discrepancies between the source and the documented atomicity/readiness contracts.

The synchronous Discord Rich Presence IPC startup hang is a verified, high-user-impact issue, but it is outside the PR16-01 diff and outside this commit. It should be fixed in a separate change after the startup-performance commit, as directed.

No source, test, tracker or `MEMORY.md` file was edited. Nothing was staged. No mutating Git command, DZLL/Steam launch, network operation, dependency installation, Forge operation or review-clone operation was performed.

## Evidence reviewed

The audit reviewed the current authoritative working tree, the complete tracked diff, the new PR16-01 source and tests, the project `MEMORY.md`, and these requested reports:

- `devReports/PR16_IMPROVEMENTS_TRACKER.md`
- `devReports/PR16_01_GTK_RESPONSIVENESS_DESIGN.md`
- `devReports/PR16_01_GTK_BATCHING_EXPERIMENT_REPORT.md`
- `devReports/PR16_01_DEFERRED_SUGGESTIONS_REPORT.md`
- `devReports/PR16_01_A2S_WARMUP_500_EXPERIMENT.md`
- `devReports/PR16_01_A2S_STARTUP_REGRESSION.md`

The audit also cross-checked the other untracked PR16-01 design, instrumentation, cache and verification reports when attributing files to the proposed commit.

Runtime claims in this audit are limited to evidence supplied by the maintainer:

- synchronous GTK callback baseline: approximately 1,682 ms;
- batched construction: 140–144 idle slices;
- batched final publication after deferred suggestions: approximately 273–284 ms;
- default A2S warmup: 1,414 candidates and 920 completions;
- experimental warmup: 500 candidates and 300 completions;
- three-run median post-publication wait: approximately 7.24 seconds default versus 2.31 seconds experimental;
- six-run A/B evidence: repeatable earlier reveal;
- separate full-sweep evidence: 300/500 readiness, reveal at `startup_ms=6036.162`, and full accounting of 12,101/12,101 at `startup_ms=134619.756`;
- no errors in the supplied full-sweep extract;
- useful responsive servers immediately after reveal; and
- successful autocomplete after deferred catalogue restoration.

This audit did not rerun or extend that live evidence.

## Findings by severity

### Critical

None.

### High

No high-severity defect was found in the PR16-01 changes.

The separately verified synchronous Discord Rich Presence connection hang is high user impact but is not caused by PR16-01 and is not in the proposed staging set. It must remain a separate follow-up.

### Medium

#### M1 — Tracker evidence is materially stale

**Classification:** verified documentation/integration defect; pre-commit action required.

The tracker still describes runtime verification as pending in the implementation summary, retains most PR16-01 next-step boxes as unchecked, and stops its progress log before GTK batching, deferred autocomplete restoration, A2S 500/60 testing and the supplied runtime results. The status itself is correct: **IN PROGRESS**.

The commit should not include the tracker in its current stale form. Update it immediately before staging, keeping the status IN PROGRESS and recording the evidence and remaining acceptance work described below.

### Low

#### L1 — Map choices are outside the batched atomic transaction

**Classification:** verified source defect; not observed in supplied runtime evidence.

`_apply_db_rows()` validates endpoints, builds map choices and calls `_set_map_choices()` before it selects the batched path. The private row transaction later commits or rolls back source rows, the visible store, endpoint/live sidecars and server-cache state. Its failure and cancellation paths do not restore the preceding map-choice model.

Consequences are limited but real: an idle-submission failure, row-construction failure, publication failure or direct supersession can leave map choices derived from rows that were never published. The ordinary eligible startup begins empty and failure presentation reports no servers, so this does not expose partial server rows or corrupt persistent state. It nevertheless means publication is not atomic across all browser-visible derived state, contrary to the stronger design wording.

Required before default-on acceptance: include map choices in the staged transaction or snapshot/restore them on failure/cancellation, and add focused construction/publication-failure tests. This is recommended before the scoped commit if the commit is intended to advertise fully atomic browser state; otherwise record it explicitly as a known default-off experimental limitation.

#### L2 — Rare A2S exhaustion path is mislabeled as threshold readiness

**Classification:** verified instrumentation/contract defect; normal successful dispatch is unaffected.

Missing/invalid objects and executor submission failures increment the full-sweep completed counter but do not increment `_startup_warmup_completed`. If those paths drain the queue with no futures or buffer remaining, `_maybe_finish_startup_rest_sweep()` calls `_complete_startup_warmup(..., reveal_reason="threshold")` even when the cohort target was not reached. This produces an immediate reveal labeled `threshold`, rather than a timeout or an explicit `sweep_exhausted`/`submission_failure` reason.

The reveal-on-exhaustion behavior predates the 500/60 experiment, but the new explicit reason and acceptance instrumentation make the mismatch part of the accumulated PR16-01 diff. The full-sweep `completed` field also means endpoints accounted for, not necessarily successfully submitted A2S futures. Therefore `completed=12101 total=12101` proves the current generation drained and accounted for the queue; it does not by itself prove 12,101 successful network queries.

Required before default-on acceptance: add a focused submission-failure/missing-object case and report a truthful exhaustion reason or explicitly define those paths as completed attempts. The supplied successful full-sweep log did not show this failure mode.

### Source-confirmed risks, not verified defects

- The synchronous and batched row-construction paths duplicate scalar conversion, cache lookup, `ServerObject` construction and sidecar attachment. Differential tests currently protect parity, but future ingestion changes must modify both paths until the experiment is consolidated or removed.
- The batched transaction temporarily retains the input rows, staged GObjects, endpoint/live dictionaries, cache record copy and candidate records. The deferred suggestion worker also temporarily holds raw bytes and decoded JSON. Current retained and peak RSS/USS behavior has not been established from the supplied evidence.
- A single row can exceed the 6 ms slice budget because yielding occurs only between rows. Supplied slice counts show batching occurred, but this audit was not given the final per-run maximum slice durations for the latest combined implementation.
- Final source/visible splices, sort-key snapshots and post-load coordination remain uninterrupted GTK work. The supplied 273–284 ms final publication is materially smaller than the 1,682 ms synchronous callback but is still long enough to be visible on a 60 Hz display.
- Running deferred JSON restoration on `_update_executor` removes it from the GTK callback but can still contend for the GIL. Successful autocomplete proves recovery, not absence of a post-reveal frame/input hitch.
- A running staged cache worker can finish after shutdown has stopped the main loop. Generation checks prevent stale canonical publication if the GLib callback runs, but a callback that never runs may leave a hidden `.pending` file. Such a file is not read as the canonical cache and is not data corruption; shutdown artifact cleanup is unmeasured.
- The cache refresh and migration final `os.replace()` operations are atomic name replacements, but unlike the dedicated cache save/publish helpers they do not perform a post-replacement directory fsync. This is a crash-durability hardening opportunity, not an observed correctness failure.
- Earlier A2S reveal intentionally uses fewer fresh observations for the one-time ping sort. Late live values update row properties without continuously rebuilding order or filter membership. This is established stable-list behavior, not a defect, but it increases the importance of explicit sort/filter actions applying the later values correctly.

## GTK batching and publication audit

### Ownership and execution

- Endpoint validation and map-model preparation remain on GTK before batching.
- Per-server cache file loading also occurs on GTK before the first row slice.
- Every `ServerObject`, property write, Python sidecar attachment and `Gio.ListStore` mutation remains on the GTK thread.
- No GObject is moved to a worker.
- The transaction processes at least one row per idle callback and yields after crossing the 6 ms monotonic deadline.
- Partial rows remain in private Python containers and are not exposed through `store`, `column_view_store`, `_obj_by_key` or A2S.

### Successful publication

The source store is populated in one splice, endpoint/live/cache sidecars are committed, sort keys are snapshotted, and the complete filtered/sorted visible store is rebuilt before A2S is started. There is no GLib yield inside that row/model commit, so ordinary GTK callbacks cannot observe a partially published row set. Duplicate endpoint behavior matches the synchronous path: duplicate source rows are retained and the final duplicate owns `_obj_by_key`.

The global suggestion catalogue is no longer part of batched final publication. This matches the supplied reduction to approximately 273–284 ms and avoids retaining another staged catalogue payload in the row transaction.

### Cancellation, generations and shutdown

- Each batched transaction has a monotonically increasing generation.
- Every slice and final publication checks generation and shutdown state.
- A superseding apply or shutdown invalidates the generation, removes the tracked idle source and releases the transaction state.
- Row, idle-submission and publication failures dismiss the startup overlay with a failure state and do not start A2S or schedule the per-server cache timer.
- Publication failure clears source/visible stores and endpoint/live state and restores the prior server-cache references.
- The startup DB in-flight flag is cleared by the completion closure on success and handled failures. Shutdown cancellation intentionally does not invoke the closure because the window is being destroyed.

No stale row callback capable of publishing after generation replacement was found. The map-choice exception in L1 is the remaining atomic-state gap.

### Cache ownership and memory

The per-server derived cache is separately bounded and keyed by canonical `IP:gamePort` plus SHA-256 of the exact accepted raw `mods` string. It does not use query port, server name, DB mtime or Python hash as identity. It restores only preview/search derivations; join-time mod parsing remains authoritative elsewhere.

Warm cache entries retain the current database `modCount` override. Provisional and failed loads do not schedule canonical cache publication. Final successful loads schedule worker-side serialization/staging after 25 seconds, then use a generation/shutdown guard before atomic replacement. Oversized record sets are pruned to the successful candidate set rather than being republished wholesale.

The implementation deliberately retains in-memory cache records for reuse. That is a performance tradeoff, not a leak demonstrated by the available evidence. Peak and settled RSS/USS remain acceptance gaps.

## Deferred mod suggestion audit

### Worker lifecycle and readiness

The batched path publishes browser-authoritative rows first, starts A2S, dismisses the overlay at the readiness gate, and only then authorizes deferred suggestion restoration. A single existing update executor performs bounded read, integrity verification, JSON decode, validation and either index restoration or rebuild using plain Python data. The worker receives a tuple of references to committed `mods_json` strings, not GObjects.

Only one deferred restore may be in flight. Result publication requires the same suggestion-cache generation and a live window. Worker and submit failures retain the pending request and establish a 60-second retry boundary. The regular browser-live tick retries after authorization and does so even when browser-live querying itself is paused.

### Autocomplete, chips and filters

Before readiness, the suggestion panel is hidden as `catalogue_not_ready`; typing does not synchronously rebuild the catalogue, and a new exact chip is not committed from incomplete data. The typed text remains available for retry.

Existing selected chips continue to filter through each row's already committed `mod_search_index`. They do not depend on the global autocomplete catalogue. Once the deferred result is published, an active eligible query is refreshed without another keystroke. The maintainer's successful post-restoration autocomplete test is consistent with this design.

### Monthly refresh and migration

- Valid v2 data is restored and may be marked expired.
- Valid v1 data remains usable and is marked for deferred v2 migration.
- A missing/corrupt cache is rebuilt on the worker and marked for staged persistence.
- Migration and monthly refresh wait for the initial sweep/fallback gate, avoid overlapping one another, stage plain data off-thread, and reject stale generations before replacement.
- An expired v1 catalogue migrates before the later monthly refresh; this can cause two deferred writes but preserves availability and freshness semantics.

No stale catalogue overwrite, selected-chip dependency or synchronous fallback rebuild was found in the supported batched path.

## A2S 500/60 audit

### Readiness selection

The experiment is gated by `DZLL_A2S_WARMUP_500=1` and only applies while the initial startup presentation is incomplete. The complete endpoint queue is still dispatched. The experimental cohort is the first 500 distinct endpoints from the existing eligible coarse-band prefix; it does not add a new ranking algorithm. The target is ceiling 60%, so 500 candidates require 300 completed futures.

With the switch unset or not exactly `1`, the existing full eligible cohort and 65% ceiling remain in effect. Manual/replacement paths after presentation completion retain the default behavior even when the environment switch is set.

### Reveal, sort and late results

Normal future completions count once toward readiness regardless of online/offline payload or future exception, which makes the gate a completed-work gate rather than an online-server quota. At readiness or timeout, buffered results are flushed, the existing final ping-sort helper runs, and the overlay is dismissed once.

The final ping sort changes the initial descending-ping state to ascending only if the user has not selected another sort. A user-initiated sort sets `_sort_changed_by_user` and is preserved. Late startup results continue updating current-generation objects and do not cancel the remaining queue. They intentionally do not continuously reorder or splice the visible list. Explicit user sort/filter/settings rebuilds use the updated values.

Generation replacement and shutdown invalidate late result callbacks and remove the warmup timer. The full-sweep completion marker is independent of early reveal.

### Runtime assessment

The supplied evidence is strong enough to establish a repeatable readiness improvement for the tested profile: median post-publication waiting fell from approximately 7.24 seconds to 2.31 seconds across the supplied comparison, useful responsive servers were present immediately, and the separate verification continued from 300/500 readiness to full 12,101/12,101 queue accounting without reported errors.

That evidence supports retaining the experiment and makes 500/60 the strongest candidate of the two switches for eventual production default. It does not yet justify changing the default because only one separate full-sweep run was supplied, `completed` is accounting rather than success semantics, and live failure/timeout/user-interaction matrices remain incomplete.

## Production-default decision

### GTK batching plus deferred suggestions

**Decision now:** retain default-off; do not enable by default yet.

The evidence demonstrates a material decomposition of the approximately 1,682 ms synchronous callback into 140–144 idle slices and a much smaller approximately 273–284 ms final publication. Successful autocomplete restoration removes the principal functional concern introduced by deferral. This is sufficient to keep and commit the experiment, but not sufficient for production-default acceptance.

Additional acceptance evidence needed:

1. At least three controlled combined runs from byte-identical isolated profiles with batching and deferred suggestions enabled, plus matched synchronous controls.
2. External frame/input evidence for spinner continuity, first nonempty frame, first responsive interaction and any post-reveal worker hitch.
3. Maximum and p95 idle-slice duration for the latest combined code, not only slice count.
4. Current and settled RSS/USS before construction, at staging peak, during suggestion restore and after readiness; high-water RSS alone is insufficient.
5. Warm v2, valid v1 migration, missing/corrupt cache and materially cold per-server cache runs.
6. Display-backed checks for source/visible counts, active filters, favourites, Previously Joined, selected chips, map choice, sort and autocomplete.
7. Real shutdown/reload during row slicing and deferred restoration, including absence or bounded cleanup of staged artifacts.
8. Focused resolution of L1 or explicit acceptance of its failure-state limitation.

### A2S 500/60

**Decision now:** retain default-off; do not enable by default yet.

The supplied A/B evidence demonstrates a repeatable and material earlier reveal, and the separate full-sweep run demonstrates continuation after reveal. Before default-on acceptance, obtain:

1. Repeated full-sweep completion/accounting in both A/B arms, not only one separate experimental run.
2. Runtime cases dominated by threshold, timeout, offline/failing responses and executor submission failure.
3. Explicit interaction checks where the user changes sort and filters before reveal and again after late live updates.
4. Confirmation that favourite/Previously Joined servers outside the first 500 remain usable and are refreshed as expected later.
5. Source/visible row counts and no duplicate dispatch for each run.
6. Input/frame observations through reveal and while the remaining approximately 11,600 queries complete.
7. Focused correction or explicit semantics for L2.

## Duplicated code, stale state and callback review

### Confirmed present

- The synchronous and batched row loaders contain intentional duplicated conversion/construction logic. It is live code, not dead code.
- The tracker is stale relative to current implementation and runtime evidence.
- Map choices can outlive a failed/cancelled batched transaction (L1).
- The A2S exhaustion marker can claim threshold readiness without meeting the cohort target (L2).

### Not found

- No duplicate A2S startup queue is started by a successful batched publication.
- No A2S work starts against partially constructed rows.
- No second deferred suggestion worker is submitted while one owns the generation.
- No stale row/suggestion result can overwrite a newer generation through the reviewed supported callbacks.
- No dead callback was found that can publish after shutdown or generation replacement.
- No default-on switch was introduced: both `DZLL_STARTUP_BATCHING` and `DZLL_A2S_WARMUP_500` require the exact value `1`.
- No SteamCMD, Flatpak Steam or `steam +workshop_download_item` path was introduced.

## Verification performed in this audit

All commands ran against the root checkout and used the maintained pytest profile boundary, which replaces HOME/XDG before application imports and fails closed on real DZLL profile access.

### Focused PR16-01 and adjacent regression set

Command:

```text
python -m pytest -q tests/test_a2s_warmup_500.py tests/test_deferred_mod_suggestions.py tests/test_startup_batching.py tests/test_mod_suggestion_cache.py tests/test_startup_perf_instrumentation.py tests/test_browser_server_ingestion_validation.py tests/test_core_startup_robustness.py tests/test_last_joined_calendar.py tests/test_startup_warmup_stable_live_list.py tests/test_pin_favourites_filter_priority.py
```

Result:

```text
262 passed, 15 skipped, 1 warning in 0.40s
```

### Maintained root suite

Command:

```text
python -m pytest -q tests
```

Result:

```text
2768 passed, 26 skipped, 1 warning in 11.02s
```

The 26 skips are display-dependent and are not claimed as executed coverage. The warning is the existing PyGObject `GLib.unix_signal_add_full` deprecation warning.

### Compile and whitespace checks

The changed/new PR16-01 Python source and tests passed `python -m py_compile` with `PYTHONPYCACHEPREFIX` under `/tmp`. `git --no-pager diff --check` passed with no output.

No DZLL process, Steam process, live A2S query or network operation was used.

## Working-tree classification

### Intended PR16-01 tracked modifications

- `src/dzll_launcher/app.py`
- `src/dzll_launcher/mod_search.py`
- `src/dzll_launcher/mod_suggestions.py`
- `src/dzll_launcher/window.py`
- `tests/test_browser_server_ingestion_validation.py`
- `tests/test_core_startup_robustness.py`
- `tests/test_last_joined_calendar.py`

The reviewed hunks in these seven tracked files are attributable to PR16-01 instrumentation, shared mod parsing/indexing, persistent caches, GTK batching, deferred suggestions, A2S 500/60, activation regression coverage and the stale test-double repair. No unrelated tracked hunk was identified.

### Intended PR16-01 untracked source and tests

- `src/dzll_launcher/mod_parsed.py`
- `src/dzll_launcher/mod_suggestion_cache.py`
- `src/dzll_launcher/server_mod_cache.py`
- `src/dzll_launcher/startup_perf.py`
- `tests/test_a2s_warmup_500.py`
- `tests/test_deferred_mod_suggestions.py`
- `tests/test_mod_suggestion_cache.py`
- `tests/test_startup_batching.py`
- `tests/test_startup_perf_instrumentation.py`

### PR16-01 untracked reports and tracker

The tracker and all `devReports/PR16_01_*.md` files listed in the proposed staging set below belong to the accumulated PR16-01 work. The tracker must be updated before it is staged. This final audit is also part of that evidence set.

### Existing unrelated dirty/untracked material

- Root `MEMORY.md` is untracked project memory and was read but not modified. It should not be staged in the PR16-01 commit unless the maintainer makes a separate explicit decision.
- `devReports/PythonCrash.txt` is an untracked diagnostic artifact associated with the separate Discord startup hang; exclude it from PR16-01.
- The many other untracked `devReports/` files predate or are unrelated to this scoped startup change; exclude them.
- `PKGBUILD`, `dzll-launcher-0.4.0-1-any.pkg.tar.zst`, `dzll-launcher-0.4.1-1-any.pkg.tar.zst`, `pkg/`, `release-v0.4.1-beta/`, `src/DayZ-Linux-Launcher-0.4.0-beta/` and `src/DayZ-Linux-Launcher-0.4.1-beta/` are packaging/release inputs, payloads or generated trees. Exclude all of them.
- The untracked packaged release trees contain duplicate test module names. This is why a bare repository-wide pytest collection is not the maintained invocation; `python -m pytest -q tests` is the authoritative root-suite command for this checkout.

No tracked packaging or release file is modified. No staged changes exist.

## Exact proposed staging list for one scoped PR16-01 commit

Do not use broad pathspecs such as `git add .`, `git add src`, `git add tests` or `git add devReports`. Stage only the following exact paths, after the tracker update and any accepted correction for L1/L2:

### Source — 8 files

1. `src/dzll_launcher/app.py`
2. `src/dzll_launcher/mod_parsed.py`
3. `src/dzll_launcher/mod_search.py`
4. `src/dzll_launcher/mod_suggestion_cache.py`
5. `src/dzll_launcher/mod_suggestions.py`
6. `src/dzll_launcher/server_mod_cache.py`
7. `src/dzll_launcher/startup_perf.py`
8. `src/dzll_launcher/window.py`

### Tests — 8 files

9. `tests/test_a2s_warmup_500.py`
10. `tests/test_browser_server_ingestion_validation.py`
11. `tests/test_core_startup_robustness.py`
12. `tests/test_deferred_mod_suggestions.py`
13. `tests/test_last_joined_calendar.py`
14. `tests/test_mod_suggestion_cache.py`
15. `tests/test_startup_batching.py`
16. `tests/test_startup_perf_instrumentation.py`

### Tracker and PR16-01 evidence — 19 files

17. `devReports/PR16_IMPROVEMENTS_TRACKER.md`
18. `devReports/PR16_01_A2S_STARTUP_REGRESSION.md`
19. `devReports/PR16_01_A2S_WARMUP_500_EXPERIMENT.md`
20. `devReports/PR16_01_CACHE_RESTORATION_OPTIMISATION_REPORT.md`
21. `devReports/PR16_01_CACHE_VERIFICATION_REPORT.md`
22. `devReports/PR16_01_DEFERRED_SUGGESTIONS_REPORT.md`
23. `devReports/PR16_01_FINAL_INTEGRATION_AUDIT.md`
24. `devReports/PR16_01_GTK_BATCHING_EXPERIMENT_REPORT.md`
25. `devReports/PR16_01_GTK_PHASE_MEASUREMENT_REPORT.md`
26. `devReports/PR16_01_GTK_RESPONSIVENESS_DESIGN.md`
27. `devReports/PR16_01_MOD_INDEX_AUDIT.md`
28. `devReports/PR16_01_MOD_INDEX_OPTIMISATION_REPORT.md`
29. `devReports/PR16_01_PER_SERVER_MOD_CACHE_AUDIT.md`
30. `devReports/PR16_01_PER_SERVER_MOD_CACHE_IMPLEMENTATION_REPORT.md`
31. `devReports/PR16_01_STARTUP_BASELINE_AUDIT.md`
32. `devReports/PR16_01_STARTUP_INSTRUMENTATION_REPORT.md`
33. `devReports/PR16_01_SUGGESTION_CACHE_LOAD_AUDIT.md`
34. `devReports/PR16_01_SUGGESTION_CACHE_REPORT.md`
35. `devReports/PR16_01_SUGGESTION_CACHE_V2_REPORT.md`

This list intentionally excludes all other dirty/untracked files.

## Required tracker updates

Keep the overview and detailed status as **IN PROGRESS**. Do not close PR16-01 from the present evidence.

Update the PR16-01 implementation summary to include:

- shared parsed-mod indexing;
- persistent v2 global suggestion cache and deferred v1 migration;
- persistent per-server derived-mod cache;
- opt-in GTK row batching with atomic row/model publication and failure recovery;
- deferred post-reveal autocomplete restoration;
- opt-in A2S 500/60 readiness gate; and
- supplied runtime evidence, while explicitly retaining default-off status.

Update the checklist as follows:

- Mark representative startup profiling, main-thread stall measurement, row preparation, model insertion/filter/sort measurement, time-to-usable-list measurement, bottleneck identification, GTK-thread GObject ownership, worker failure recovery, generation protection and shutdown tests complete to the extent supported by source/tests and supplied logs.
- Keep peak/current memory acceptance open.
- Keep display-backed frame/input acceptance open except for the narrow existing constructor tests; do not infer smoothness from idle-slice count or next-frame markers.
- Keep cold/corrupt/v1 runtime paths open.
- Keep repeated full-sweep A/B and timeout/failure/user-interaction acceptance open.
- Record L1 and L2 as remaining integration items unless corrected before the tracker update.

Add dated progress entries recording:

1. batching reduced the uninterrupted final publication to approximately 273–284 ms while row construction ran in 140–144 idle slices;
2. deferred autocomplete restoration was functionally successful;
3. 500/60 reduced the three-run median post-publication wait from approximately 7.24 seconds to 2.31 seconds and repeated across the six-run A/B test;
4. the separate full-sweep run reached 12,101/12,101 accounting after reveal with no errors in the supplied extract;
5. useful responsive servers were available immediately after reveal;
6. maintained offline verification now passes at 2,768 passed, 26 skipped, 1 warning in this audit; and
7. the Discord IPC hang is a separate issue and is excluded from PR16-01.

Revise closure/acceptance wording so **CLOSED — IMPLEMENTED** additionally requires:

- explicit production-default decision for each switch;
- resolved or accepted L1/L2 semantics;
- controlled display/frame/input evidence;
- acceptable current/peak RSS/USS;
- repeated full-sweep and failure/timeout evidence;
- correct user sort/filter behavior with late results; and
- no startup reliability regression after the separate Discord fix is integrated and tested independently.

## Final conclusion

The accumulated PR16-01 work is technically suitable for a single narrowly staged **default-off** commit after the tracker is brought current. The implementation materially improves the tested startup path without moving GObjects off GTK, without exposing partial rows, and without stopping the full A2S sweep. The maintained offline suite and focused checks pass.

Do not make GTK batching/deferred suggestions or A2S 500/60 production defaults yet. The supplied runtime evidence is promising—especially for 500/60—but memory, display/input, failure-path and repeated full-sweep acceptance remain incomplete. Keep PR16-01 **IN PROGRESS**.
