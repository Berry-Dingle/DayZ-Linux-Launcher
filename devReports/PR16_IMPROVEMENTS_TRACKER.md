# DZLL — PR #16 Improvement Investigation Tracker

Created: 2026-10-10
Last updated: 2026-10-12
Overall status: OPEN
Source: GitHub PR #16 — mrmaffen
Baseline: DZLL v0.4.1-beta

PR: https://github.com/Berry-Dingle/DayZ-Linux-Launcher/pull/16

## Purpose

Track potentially useful improvements identified during review of
PR #16 without merging the contributor's PR wholesale.

Each improvement must be independently investigated, evaluated,
implemented where appropriate, and tested against official DZLL.

This tracker is the authoritative record of investigation progress,
maintainer decisions, outstanding work and closure evidence.

Current source code and tests remain authoritative over historical
audit findings.

## Status definitions

OPEN
- Investigation, implementation or verification remains outstanding.

IN PROGRESS
- Actively being investigated, implemented or tested.

BLOCKED
- Cannot proceed until a dependency or decision is resolved.

CLOSED — IMPLEMENTED
- Implemented in official DZLL and successfully verified.

CLOSED — REJECTED
- Evaluated and deliberately rejected.

CLOSED — DEFERRED
- Investigation deliberately shelved.
- Reopen when development priorities permit.

Rules:
- Record a dated progress-log entry whenever status changes.
- Every closed item must include a closure reason.
- CLOSED — IMPLEMENTED requires test or verification evidence.
- A failed test or incomplete implementation does not close an item.
- Reopened items must record why they were reopened.
- Do not silently reopen rejected or deferred items.
- Do not introduce PR code into official DZLL merely because
  an improvement is listed here.

## Investigation overview

| ID | Investigation | Priority | Status | Decision |
|---|---|---|---|---|
| PR16-01 | Startup performance | HIGH | IN PROGRESS | ADAPT — READY FOR DEFAULT ENABLEMENT |
| PR16-02 | Direct Connect | MEDIUM | OPEN | INVESTIGATE |
| PR16-03 | Retained favourites | MEDIUM | OPEN | INVESTIGATE |
| PR16-04 | Steam Workshop stall detection | MEDIUM | OPEN | INVESTIGATE |
| PR16-05 | Startup refresh progress | LOW | OPEN | OPTIONAL |
| PR16-06 | Visible-row refresh indicator | LOW | OPEN | REDESIGN |
| PR16-07 | Minor UI improvements | LOW | OPEN | SELECTIVE |
| PR16-08 | Rejected UI and behaviour changes | N/A | CLOSED — REJECTED | REJECT |

---

## PR16-01 — Startup Performance

Priority: HIGH
Status: IN PROGRESS
Decision: ADAPT — READY FOR DEFAULT ENABLEMENT
Implementation: DEFAULT-OFF STARTUP BATCHING, DEFERRED SUGGESTIONS, VERSION-2 SUGGESTION CACHE, CANONICAL IN-MEMORY PER-SERVER DERIVED-MOD CACHE AND A2S 500/60 EXPERIMENTS IMPLEMENTED AND VERIFIED; COMPACT AUTOCOMPLETE POSTINGS PROTOTYPE REJECTED; FINAL INTEGRATION ACCEPTED FOR A SEPARATE DEFAULT-ENABLEMENT CHANGE

### Objective

Investigate reducing GTK main-thread blocking during server loading,
row construction, model updates and startup refresh.

### PR implementation

- Moves row preparation into the DB executor.
- Moves ServerObject construction off the GTK main thread.
- Reuses Refresh All machinery for startup status refreshing.
- Adds startup refresh progress feedback.

### Sol Medium audit findings

Confirmed:
- DB fetching and SQLite reading were already asynchronous.
- Live A2S queries were already asynchronous.
- PR moves endpoint conversion, display-value preparation,
  mod indexing and ServerObject construction to a worker.
- Main-thread operations still include bulk model insertion,
  filtering, sorting, visible-store rebuilding and GTK updates.
- Row-build worker errors have no proper completion recovery.
- Reload and active refresh generations are not fully coordinated.
- Shutdown does not invalidate every pending row-build callback.

Risks:
- ServerObject is a GObject constructed on the worker.
- GObject thread-safety assumptions are not established.
- Worker exceptions can leave the server browser empty.
- Overlapping refresh/reload operations can apply stale results.
- The shared executor can become congested.
- Large unchunked main-thread operations remain.

Observed during PR testing:
- Startup felt approximately the same as official DZLL.
- Spinner still paused for approximately 1–2 seconds.
- No verified performance improvement was established.

### Current official implementation and hardening status

The adapted official implementation keeps `ServerObject`, GObject and GTK model
construction on the GTK thread. Worker work is restricted to plain-data
preparation. Startup row construction is sliced through GTK idle callbacks and
published transactionally, with generation, cancellation, rollback and shutdown
guards. Suggestion restoration and cache persistence are deferred, and A2S
500/60 changes only the initial readiness cohort; the complete queue continues
after reveal.

Both experiments remain exact-value, default-off switches:

- `DZLL_STARTUP_BATCHING=1`
- `DZLL_A2S_WARMUP_500=1`

| Finding | Resolution evidence | Status |
|---|---|---|
| Q1 | Empty/all-invalid batched input retains authoritative empty-state semantics and cannot publish an empty warm-cache rewrite. | RESOLVED |
| Q2 | Failed, cancelled and superseded batches restore map choices, selection and cached filter state. | RESOLVED |
| Q3 | Exhaustion below the A2S target is reported as `sweep_exhausted`, not `threshold`. | RESOLVED |
| Q4 | Post-publication failures terminate with truthful outcomes and release startup ownership. | RESOLVED |
| Q5 | Deferred first-save and v1 migration have distinct instrumentation; v2 restoration schedules no publication. | RESOLVED |
| Q6 | Both staged suggestion-cache publications fsync the destination directory after successful atomic replacement. | RESOLVED |
| V1 | Transient and persistent titlebar failures exercise and assert their distinct terminal outcomes. | RESOLVED |
| G1 | Filter-state rollback is directly asserted and independently mutation-verified. | RESOLVED |
| G2 | Filter-suppression depth is balanced when map-widget restoration raises. | RESOLVED |
| G3 | A superseding batch snapshots the prior batch's restored map state. | RESOLVED |
| H1 | Repository tests now assert exact `os.replace` then directory-fsync ordering for refresh and first-save/v1-migration publication. | RESOLVED |
| H2 | The deferred restoration matrix now covers format v2, asserting no pending migration, `kind is None`, and no publication submission. | RESOLVED |

These resolutions established scoped default-off commit readiness only. The
later runtime, live-sweep and maintainer desktop evidence recorded below
establishes production-default acceptance.

### Next steps

- [x] Add opt-in startup baseline phase instrumentation to official DZLL.
- [x] Profile official DZLL startup with representative, controlled data.
- [x] Measure main-thread stalls during loading.
- [x] Measure database processing and row preparation.
- [x] Measure model insertion, filtering and sorting.
- [x] Measure time to first usable server list.
- [x] Measure total startup time and peak memory.
- [x] Identify actual bottlenecks before implementing changes.
- [x] Evaluate worker-side pure Python data preparation.
- [x] Keep GTK model operations on the main thread.
- [x] Establish safe GObject ownership and lifecycle.
- [x] Implement worker failure and executor-submit recovery.
- [x] Protect against overlapping reloads and stale results.
- [x] Test shutdown while asynchronous work is active.
- [x] Benchmark against the original implementation.

### Remaining acceptance work

- [x] Obtain controlled repeated combined GTK batching/deferred-suggestion runs
  against matched synchronous controls from byte-identical isolated profiles.
- [x] Obtain maintainer desktop acceptance of rendered responsiveness and
  interaction through startup and post-reveal work.
- [x] Measure maximum and p95 row-slice duration on the final combined code.
- [x] Measure current and settled RSS/USS at pre-construction, staging peak,
  suggestion restoration and readiness; peak/high-water RSS alone is not enough.
- [x] Complete automated display-backed filter, favourites, Previously Joined, chip, map,
  sort and autocomplete checks, including live shutdown/reload fault injection.
- [x] Repeat A2S full-sweep accounting in both A/B arms and exercise threshold,
  timeout, offline/failure, submission-failure and user-interaction cases.
- [x] Decide whether each default-off experiment meets production-default
  reliability, responsiveness and memory acceptance: **READY**. Both may become
  production defaults in a separate change; this review did not change them.

PR16-01 remains IN PROGRESS only until the separately scoped default-enablement
change is made and verified. The implementation and acceptance review are
otherwise complete.

### Closure criteria

Close as IMPLEMENTED only when:
- Changes are incorporated into official DZLL.
- Concurrency and shutdown tests pass.
- No startup reliability regression is introduced.
- Measurable improvement is demonstrated.

Otherwise document the outcome and close as REJECTED
or DEFERRED as appropriate.

---

## PR16-02 — Direct Connect

Priority: MEDIUM
Status: OPEN
Decision: INVESTIGATE
Implementation: NOT STARTED

### Objective

Allow users to add and connect to servers by IP and port even
when those servers are absent from Forge's published database.

This feature was already planned independently of PR #16.

### PR implementation

- Adds manual IPv4:port server entry.
- Retrieves live A2S_INFO metadata.
- Adds A2S_RULES DayZ mod decoding.
- Stores manually added servers.
- Integrates manual entries into existing server rows.

### Key requirements

- Reuse established DZLL architecture and UX.
- Compare DayZ mod decoding against Forge.
- Confirm handling of chunked and encoded A2S_RULES data.
- Verify little-endian fields, escaping and chunk ordering.
- Validate parsing against actual DayZ servers.
- Handle malformed and incomplete protocol responses.
- Prevent duplicate canonical endpoint entries.
- Refresh server metadata when appropriate.
- Refresh required mod lists BEFORE EVERY JOIN.
- Detect added and removed server mods.
- Subscribe to newly required mods through existing workflow.
- Avoid relying on stale mod lists from initial server addition.
- Handle offline and unreachable servers.
- Verify correct game/query port handling.
- Ensure failed lookups do not corrupt retained entries.

### Known PR risks

- Binary parser requires real-protocol validation.
- A2S_INFO and A2S_RULES can consume separate timeout budgets.
- Combined requests may exceed the enclosing browser deadline.
- Initial mod retrieval does not prove join-time freshness.

### Next steps

- [ ] Inspect Forge's working mod decoder.
- [ ] Compare decoding approaches.
- [ ] Establish canonical manual-server identity.
- [ ] Design shared live metadata/mod refresh mechanism.
- [ ] Design mandatory pre-join mod refresh.
- [ ] Test changing server mod requirements.
- [ ] Test Steam Workshop subscription integration.
- [ ] Test offline and malformed responses.
- [ ] Implement independent DZLL solution.
- [ ] Perform end-to-end join verification.

### Closure criteria

Close as IMPLEMENTED only when:
- Direct Connect operates correctly.
- Required mods are refreshed before joining.
- New mods are handled by the existing Workshop workflow.
- Persistent entries and error paths are verified.

---

## PR16-03 — Retained Favourites

Priority: MEDIUM
Status: OPEN
Decision: INVESTIGATE
Implementation: NOT STARTED

### Objective

Preserve favourite servers when they disappear from the published
Forge server database.

### PR implementation

- Stores retained server snapshots.
- Merges retained entries into the downloaded server list.
- Allows favourites to survive database disappearance.

### Known issue

The retained snapshot is primarily updated when the favourite
is created or toggled.

Ordinary live updates do not consistently refresh the retained
metadata snapshot.

A server restored after DB disappearance may therefore display
old metadata or required mods.

### Requirements

- Preserve favourite identity when Forge drops a server.
- Avoid duplicate server rows.
- Retain sufficient information to query the server directly.
- Refresh online/offline status.
- Refresh live metadata.
- Refresh required mods BEFORE EVERY JOIN.
- Handle server name, map and mod changes.
- Share appropriate refresh infrastructure with Direct Connect.
- Avoid excessive A2S traffic.
- Handle server reappearance in Forge.
- Establish clear retention and deletion semantics.
- Preserve existing favourites and user settings.

### Next steps

- [ ] Review current favourite persistence.
- [ ] Review retained-server storage architecture.
- [ ] Define canonical server identity.
- [ ] Design shared Direct Connect/retained refresh.
- [ ] Define metadata freshness policy.
- [ ] Define mandatory pre-join mod refresh.
- [ ] Handle Forge disappearance/reappearance.
- [ ] Test persistence and migration.
- [ ] Test stale/offline retained servers.
- [ ] Implement and verify independently.

### Closure criteria

Close as IMPLEMENTED only when retained favourites remain
usable independently of the Forge database, with current
metadata and verified pre-join mod requirements.

---

## PR16-04 — Steam Workshop Stall Detection

Priority: MEDIUM
Status: OPEN
Decision: INVESTIGATE
Implementation: NOT STARTED

### Clarification

The PR's 90-second timeout does NOT replace normal DayZ
process-launch detection.

It introduces a 90-second no-progress threshold in the Steam
UGC subscription/download process.

The existing overall download timeout remains an upper limit.

### Potential problem

Steam can legitimately spend extended periods:
- Queuing downloads.
- Preparing Workshop content.
- Processing or verifying installed content.
- Waiting without reporting measurable byte progress.

A fixed 90-second no-progress threshold could report a false stall.

### Next steps

- [ ] Review existing official Steam UGC state handling.
- [ ] Inspect the PR's progress_key tracking.
- [ ] Identify legitimate non-progress states.
- [ ] Test slow and queued Workshop downloads.
- [ ] Evaluate false-positive stall detection.
- [ ] Define appropriate recovery/cancellation behaviour.
- [ ] Decide whether adaptive stall detection is worthwhile.

### Closure criteria

Close as IMPLEMENTED only after representative Steam
conditions are tested and false failure risk is acceptable.

---

## PR16-05 — Startup Refresh Progress

Priority: LOW
Status: OPEN
Decision: OPTIONAL
Implementation: NOT STARTED

### Objective

Provide useful feedback during startup server refresh without
introducing misleading progress information or visual clutter.

### PR approach

- Reuses Refresh All progress machinery.
- Counts completed refresh attempts.
- Includes failed and skipped attempts in completion.
- Does not necessarily represent successful server updates.

### Next steps

- [ ] Evaluate usefulness against current startup UI.
- [ ] Decide appropriate status wording.
- [ ] Distinguish attempts from successful updates.
- [ ] Preserve existing startup warmup behaviour.
- [ ] Ensure progress UI does not block startup.
- [ ] Implement only if worthwhile.

### Closure criteria

Close as IMPLEMENTED after correct progress semantics and
startup integration are verified.

---

## PR16-06 — Visible-Row Refresh Indicator

Priority: LOW
Status: OPEN
Decision: REDESIGN
Implementation: NOT STARTED

### Existing behaviour

- Browser refreshes visible server rows.
- Approximately 20 additional rows are buffered.
- The background refresh cycle runs every five seconds.

### PR implementation

- Refreshed rows receive recurring yellow highlighting.
- Repeated flashing was judged excessive and distracting.

Decision:
Do not adopt the PR's row-flash presentation.

### Preferred alternative

- Small rectangular status block in the browser's bottom-right.
- Text such as "Refreshing visible rows".
- Small LED-style indicator.
- LED briefly flashes when a genuine refresh cycle starts.
- Minimal animation and restrained visual presentation.
- Sharp 90-degree corners.
- No server-row-wide highlighting.
- No complex progress tracking for a quick refresh operation.

### Next steps

- [ ] Identify existing browser refresh-cycle trigger.
- [ ] Design unobtrusive status indicator.
- [ ] Connect indicator to actual refresh events.
- [ ] Ensure no additional expensive UI work.
- [ ] Test visual behaviour during scrolling and filtering.

### Closure criteria

Close as IMPLEMENTED only after visual approval and
verification that normal browser performance is unaffected.

---

## PR16-07 — Minor UI Improvements

Priority: LOW
Status: OPEN
Decision: SELECTIVE
Implementation: NOT STARTED

### Candidates

- Settings gear icon instead of sliders icon.
- Manage Mods text button instead of existing icon.
- Revised refresh-button appearance.
- Appropriate startup refresh progress placement.

### Design constraints

- Preserve established DZLL layout where practical.
- Maintain DZSA familiarity for migrating players.
- Avoid rearranging controls without functional justification.
- Avoid increasing window size without clear benefit.
- Preserve discoverability of important actions.
- Prefer small focused improvements over broad redesign.

### Next steps

- [ ] Review candidate changes individually.
- [ ] Decide which offer genuine usability improvements.
- [ ] Implement selected changes in official DZLL style.
- [ ] Verify appearance and layout at supported resolutions.

### Closure criteria

Close when all candidates have been individually accepted,
rejected or deferred and any accepted changes are verified.

---

## PR16-08 — Rejected UI and Behaviour Changes

Priority: N/A
Status: CLOSED — REJECTED
Decision: REJECT
Closed: 2026-10-10

### Rejected changes

- Removal of Show Favourites filter.
- Removal of Previously Played / Previously Joined filter.
- Replacement of filtering with column sorting.
- PR FAV sorting implementation.
- Default Pin Favourites enabled.
- Default window widening from 1200 to 1420 pixels.
- Continuous yellow row flashing during live refresh.
- Removal of the accessible Server Companion on/off control.
- Companion hide action disabling monitoring.
- Closing undocked Companion disabling monitoring.
- Unnecessary broad layout rearrangements.

### Reasons

Sorting and filtering are different operations.

Server Companion monitoring must remain independent of whether
its UI is docked, undocked or temporarily hidden.

Closing an undocked Companion window should redock it rather
than implicitly disable monitoring.

Established UI preferences and existing workflows should not
be removed without compelling functional justification.

The PR FAV sort comparator also fails to sort by favourite
state according to the initial source audit.

### Preserved behaviour

- Show Favourites remains available.
- Previously Played filtering remains available.
- Pin Favourites remains optional and off by default.
- Server Companion has explicit on/off controls.
- Undocked Companion closes by redocking.
- Current default window sizing remains preferred.
- Normal background refresh does not flash every row.

### Closure evidence

Maintainer review and hands-on testing: 2026-10-10.
Initial Luna audit confirmed filter removals and FAV sort defect.

---

## Additional decision — Hide Test Servers

Status: CLOSED — ACCEPTABLE REMOVAL
Decision date: 2026-10-10

The PR removes Hide Test Servers completely.

The maintainer considers its removal acceptable because the
minimum server-capacity filter can exclude many small test
servers when configured appropriately.

This is not functionally identical:
- Large-capacity test servers may remain visible.
- Small legitimate servers may be excluded.

No independent reimplementation is currently planned.

This records design acceptance, not implementation in main.

---

## Evidence

### Initial Luna audit

Report:
pr16_initial_audit.md

Findings:
- 13 source files and 17 test files changed.
- 2,323 insertions and 676 deletions.
- FAV sorting comparator defect.
- Removed favourite/played/test filters.
- Direct Connect and retention concerns.
- 90-second Steam UGC stall threshold.
- Async startup improvement requiring deeper analysis.

### Sol Medium startup audit

Report:
pr16_startup_deep_audit.md

Findings:
- Background data preparation is a valid optimisation idea.
- GObject construction occurs in a worker.
- Worker exception recovery is incomplete.
- Reload/sweep and shutdown lifecycle gaps exist.
- Main-thread model operations remain substantial.
- No demonstrated startup benchmark improvement.
- Recommendation: ADAPT, do not copy unchanged.

Audit files originated in the disposable PR review workflow.
They are not assumed to exist inside the official repository.

### Repository references

Official:
 /home/gbrown/GithubRepos/DayZ-Linux-Launcher

Disposable PR review:
 /home/gbrown/GithubRepos/PR-Reviews/DZLL-PR16

PR:
 https://github.com/Berry-Dingle/DayZ-Linux-Launcher/pull/16

PR comparison:
 Local main de504a4
 Local pr16-review c7b6539

Historical findings must be revalidated against current code
before implementing changes.

---

## Progress log

### 2026-10-12

- PR16-01 final integration review: maintainer-verified Fedora KDE live runs
  independently exhausted the same 12,166 eligible endpoints in both modes.
  The combined experimental run reached its 300/500 readiness threshold,
  dispatched and completed 12,166 attempts, found 10,295 responsive endpoints,
  and exhausted in 124.042 seconds with zero queued and zero in flight. The
  normal run reached 922/1,414, dispatched and completed 12,166 attempts, found
  10,443 responsive endpoints, and exhausted in 122.278 seconds with zero
  queued and zero in flight. These separate live-network observations establish
  full-sweep completion but are not a deterministic timing or response-rate
  comparison. The maintainer reports substantially faster, smoother startup,
  accepts the remaining two or three minor micro-pauses for this release, and
  verified filters, search and mod suggestions. Together with the stable
  45-minute memory plateau, retained cache canonicalisation, GTK publication
  improvement, lifecycle/failure coverage and the current full display-backed
  result (2,821 passed, 4 unrelated detached-fixture skips), both startup
  experiments are **READY** to become production defaults in a separate change.
  They remain default-off in this review. See
  `PR16_01_FINAL_INTEGRATION_REVIEW.md`.
- PR16-01 GTK responsiveness follow-up: temporary per-row, cyclic-GC,
  idle-gap, model-notification and full-application probes on the byte-identical
  12,101-row inputs demonstrated that slice overruns are automatic cyclic-GC
  pauses inside otherwise sub-millisecond row construction, not pathological
  server records or time between idle callbacks. The final-publication hitch is
  dominated by the first visible `Gio.ListStore.splice` and its synchronous GTK
  consumers (193.6–214.2 ms in the matched baseline), plus a 64.1–65.5 ms
  all-row sort-key snapshot; source-store publication was only 5.4–6.9 ms.
  The sort-key snapshot now runs incrementally inside the GTK-owned row batches
  using one captured timestamp. In three matched before/after runs, median final
  publication improved from 310.4 to 252.1 ms and the median sampled maximum
  main-loop gap improved from 426.7 to 356.7 ms, with no material HWM increase.
  Slice maxima remain above the 6 ms budget (matched medians 14.5 before and
  15.9 ms after), so GC-control experiments were not retained: they moved or
  deferred pauses rather than proving a lifecycle-safe responsiveness gain.
  Focused tests passed (86), and the full isolated display-backed suite passed
  (2,813 passed, 4 unrelated detached-fixture skips). Both switches remain
  default-off and PR16-01 remains IN PROGRESS; rendered input/frame evidence,
  the residual first-visible-model hitch and completed matched live A2S sweeps
  remain acceptance blockers. See `PR16_01_GTK_RESPONSIVENESS_FOLLOWUP.md`.
- PR16-01 final acceptance: three byte-identical isolated Xvfb controls and
  three combined experimental runs loaded 12,101 rows with 100% per-server
  cache hits. Median DB-to-GTK completion improved 1,897 to 1,494 ms, overlay
  dismissal improved 2.718 to 2.228 seconds, and the sampled pre-overlay
  main-loop maximum improved 1,955 to 433 ms. Experimental settled USS was
  15.18 MiB higher and observed peak USS was 20.92 MiB higher. Both synthetic
  A2S arms completed all 12,101 attempts; early reveal left late results active,
  autocomplete/filtering worked, and explicit ping sorting restored canonical
  order. The display-backed full suite passed (2,813 passed, 4 unrelated
  detached-fixture skips). A bounded live experimental run revealed at 300/500
  but did not finish the 12,101-endpoint sweep within 90 seconds. More
  importantly, all three batched runs exceeded the 6 ms slice budget (59.6–77.6
  ms), final publication remained 325–351 ms, and no external rendered-frame or
  human input-latency observation was available. Recommendation: **NOT READY**
  to enable either experiment by default; keep both opt-in and PR16-01 IN
  PROGRESS pending the blockers in `PR16_01_FINAL_ACCEPTANCE.md`.
- PR16-01: retained canonical in-memory per-server cache records after successful
  restoration. On the current 12,101-record cache this removed 14.70 MiB from
  the reachable Python graph and 8.91 MiB from matched process USS deltas while
  reusing the restored `frozenset` for row construction and subsequent hits.
  The on-disk format, atomic writer and join authority are unchanged. A packed
  32-bit autocomplete-posting prototype was rejected and removed: despite a
  35.65 MiB traced steady-Python reduction on the same 20,335-entry catalogue,
  it increased matched warm-load USS by about 16.7 MiB, increased cold-build
  USS and peak, slowed restoration, and made all measured representative
  uncached lookups slightly slower. Focused and full regression results are
  recorded in `PR16_01_MEMORY_OPTIMISATION_RESULTS.md`. Both startup experiments
  remain default-off and PR16-01 remains IN PROGRESS.

### 2026-10-11

- PR16-01: closed hardening findings Q1-Q6, V1, G1-G3, H1 and H2 with
  permanent regression coverage. Suggestion-cache tests now require successful
  atomic replacement before directory fsync for refresh and first-save/migration
  publication, and deferred restoration format v2 proves no pending migration,
  no migration kind and no publication submission. The focused startup/cache
  suite passed (73 passed); the maintained full suite passed (2,790 passed,
  26 display-dependent skips). Production-default runtime, frame/input,
  display-backed and RSS/USS acceptance remain open. Both experiments remain
  default-off and PR16-01 remains IN PROGRESS.
- PR16-01: verified the persistent per-server cache and global suggestion v2
  interaction against the offline suite. Corrected the stale Last Joined tick
  test double to include the already existing suggestion-refresh scheduler and
  added a warm/cold selected-chip filter differential case. The full suite now
  passes with the formerly failing case included. Maintainer warm runtime data
  shows about 1,678 ms GTK DB callback and 1,837 ms main-loop stall on 11,980
  servers, with 99.77–100% per-server hits. An isolated serialization proxy
  supports a transient RSS increase but does not explain the full reported
  high-water delta. Profile remaining main-thread phases and memory before the
  next architecture change. Status remains IN PROGRESS.
- PR16-01: added a separate bounded, versioned per-server preview/search cache
  keyed by canonical endpoint and exact raw-mod SHA-256. Cold misses retain the
  shared parsed path; warm hits restore derived values. Final successful loads
  schedule deferred plain-data atomic publication with generation/shutdown guards;
  provisional and failed loads cannot prune disk entries. Focused offline tests
  passed (315 passed, 13 skipped); the broad suite passed with the one previously
  documented unrelated Last Joined fixture case deselected (2,725 passed,
  24 skipped, 1 deselected). The actual 11,980-server cold/warm GTK benchmarks,
  display-backed checks and peak-memory acceptance remain pending. Status stays
  IN PROGRESS.
- PR16-01: three 11,980-server runs after in-place restoration measured warm
  cache loading at 575–583 ms, GTK DB callback at 3,131–3,161 ms and main-loop
  stall at 3,226–3,318 ms. The stage timing attributed about 218 ms to
  canonical JSON re-serialization and about 7 ms to SHA-256. Added a version-2
  exact-payload-byte digest format, version-1 compatibility and deferred
  worker-based migration after the initial sweep. Focused offline regression
  checks passed; runtime comparison remains pending and status stays IN PROGRESS.
- PR16-01: maintainer measured the persistent warm-cache GTK DB callback at
  3,198 ms average, with 710–808 ms spent loading a roughly 19 MB catalogue
  containing 20,197 entries. Added opt-in aggregate cache-load stage timing
  and in-place version-1 restoration, with differential offline regressions.
  Runtime comparison of this restoration experiment remains outstanding;
  status stays IN PROGRESS.

### 2026-10-10

- PR16-01 changed from OPEN to IN PROGRESS. Added opt-in `DZLL_PERF_LOG=1`
  startup phase instrumentation in official DZLL and focused offline tests.
  Baseline runs and optimisation remain outstanding; PR16-01 is not closed.
- PR16-01: three 11,980-server baseline runs confirmed the mod-indexing pause.
  Added a load-local shared JSON decode/name-cleaning experiment with differential
  regression tests. Subsequent three-run comparison measured GTK DB callback
  average 4,140 -> 3,576 ms, global suggestions 1,496 -> 1,180 ms, and
  main-loop stall 4,222 -> 3,728 ms.
- PR16-01: added a versioned persistent global suggestion catalogue cache with
  30-day expiry and deferred background refresh after the full startup A2S sweep.
  Focused offline cache/lifecycle regressions passed. Runtime cache benchmark
  remains outstanding; status stays IN PROGRESS.
- Luna initial cumulative-diff audit completed.
- Sol Medium startup performance deep audit completed.
- Experimental PR interface inspected.
- Maintainer decided PR #16 will not be merged wholesale.
- Startup optimisation classified ADAPT.
- Direct Connect and retained favourites classified OPEN.
- Workshop stall detection classified OPEN.
- Startup progress and indicator ideas classified OPEN.
- Unwanted UI and Companion changes classified CLOSED — REJECTED.
- Hide Test Servers removal accepted in principle.
- Tracker established for independent investigations.

---

## Tracker maintenance rules

1. Update the relevant item's status whenever work begins,
   becomes blocked, completes or is deliberately deferred.

2. Keep the investigation overview table synchronized with
   each item's detailed status.

3. Record every status change in the dated progress log.

4. Include test results or verification evidence when closing
   an item as IMPLEMENTED.

5. Do not mark an item implemented merely because code was
   written or an automated agent reported success.

6. Do not overwrite established maintainer decisions without
   explicit approval.

7. Do not silently import rejected PR behaviour during
   implementation of accepted ideas.

8. Keep historical audit findings distinguishable from
   verified current-source behaviour.

9. Review and update the tracker before starting a new
   investigation session.

10. When all investigation items are closed, update the
    overall tracker status to CLOSED and record the date.
