# PR16-01 — Final Integration Review and Release-Readiness Decision

Date: 2026-10-12

Checkpoint: `005e4a803fd9d4335486f31232efb33bd9f953cd`

Decision: **READY** for both `DZLL_STARTUP_BATCHING` and
`DZLL_A2S_WARMUP_500` to become production defaults in a separate, explicitly
scoped change.

This review did not change either default. Both remain exact-value opt-ins in
the reviewed tree. No speculative optimisation was implemented.

## Executive finding

The cumulative PR16-01 work after checkpoint `005e4a8` integrates correctly.
The retained server-mod cache canonicalisation, GTK publication improvement and
A2S sweep diagnostics preserve the existing data, threading, lifecycle and
persistence contracts. No release-blocking correctness defect was found.

The prior NOT READY decision was based on two missing acceptance inputs rather
than a confirmed defect: completed live sweeps in both modes and maintainer
desktop acceptance through the remaining GTK hitches. The newly supplied Fedora
KDE evidence closes both gaps:

- both independent live-network runs dispatched and completed every one of the
  same 12,166 eligible endpoints and ended with zero queued and zero in flight;
- the maintainer observed substantially faster, smoother startup, accepted the
  remaining two or three minor micro-pauses for this release, and verified
  filters, search and mod suggestions; and
- the previous 45-minute idle memory observation reached a stable plateau.

The two live runs are not deterministic timing comparisons. Their elapsed times
and responsive totals must not be used to claim that either mode is faster or
finds more servers. They do establish successful end-to-end exhaustion in each
mode.

No further production-code fix is required before a separately reviewed default
flip. PR16-01 remains IN PROGRESS in the tracker only because this task expressly
left the defaults unchanged.

## Scope and baseline

`HEAD` is the requested checkpoint, `005e4a8` (`perf: add opt-in startup batching
and A2S warmup`). The tracked checkpoint-to-worktree diff initially contained
five files:

- `devReports/PR16_IMPROVEMENTS_TRACKER.md`
- `src/dzll_launcher/server_mod_cache.py`
- `src/dzll_launcher/window.py`
- `tests/test_browser_server_ingestion_validation.py`
- `tests/test_startup_batching.py`

The PR16-01 diagnostic regression file
`tests/test_a2s_sweep_diagnostics.py` is untracked and is part of the reviewed
work. The relevant post-checkpoint reports are also untracked.

The checkout already contained many unrelated untracked reports, packaging
artifacts, release trees, `.qwen/`, `PKGBUILD`, `pkg/` and other files. They were
treated as pre-existing user state and were not modified, deleted, staged or
included in the proposed commit scope. `DZLL_MEMORY.md` was read as required and
left unchanged.

No mutating Git command was used. Nothing was staged, committed, pushed, reset,
checked out, stashed or cleaned.

## Cumulative diff audit

### Retained server-mod cache canonicalisation

`server_mod_cache.lookup()` now replaces a successfully restored seven-element
JSON list with the canonical in-memory tuple
`(digest, count, preview, index)`. Subsequent hits reuse the same restored
`frozenset`-backed index rather than reconstructing it.

Confirmed properties:

- canonical endpoint identity remains `ip:gport`;
- the fingerprint remains SHA-256 of the exact accepted raw `mods` text;
- invalid, stale or oversized records still return a miss and are not promoted;
- canonicalisation occurs only after complete validation succeeds;
- the on-disk schema, magic, digest and bounds are unchanged;
- the serializer accepts mixed raw-list and canonical-tuple records and writes
  the established list representation;
- staging still writes and fsyncs a same-directory temporary file before the
  existing atomic `os.replace` and destination-directory fsync; and
- authoritative join-time mod parsing remains outside this preview/search
  cache.

The focused regression proves one-time promotion, object-identity reuse on a
second lookup, preservation of untouched raw records and mixed-record disk
round-trip. Existing cache tests continue to cover corruption, bounds, failed
atomic replacement, generation replacement, shutdown, provisional loads,
failed loads, manual refresh and warm-cache preservation.

The retained measurement remains appropriately scoped: 14.70 MiB was removed
from the reachable Python graph and matched component-process USS improved by
8.91 MiB on the 12,101-record input. This is not represented as an exact
reduction from the separate live 815.8 MiB USS plateau.

### GTK batched publication improvement

The batched transaction captures one timestamp at setup and computes the
existing `played`, `sort_ping`, `sort_players` and `sort_played_days` values as
each `ServerObject` is built on the GTK thread. The former all-row sort-key pass
was removed from final publication. Instrumentation now attributes this work to
`row_sort_key_snapshot` rather than hiding it.

Confirmed properties:

- `ServerObject` and all GTK/GObject model construction remain on the GTK main
  thread;
- every row in one transaction uses the same timestamp, preserving the prior
  snapshot semantics;
- source and visible publication remain atomic;
- synchronous and batched rows have parity for the three sort properties,
  favourites, duplicate endpoint replacement and Last Joined state;
- map choices, filters, search indexes, mod suggestions and explicit sort paths
  are unchanged; and
- the change does not add a second visible model, a new per-row field, a GC
  policy or worker-side GObject work.

The retained matched measurements reduced median final publication from 310.4
ms to 252.1 ms and the median externally sampled maximum main-loop gap from
426.7 ms to 356.7 ms. Automatic cyclic-GC pauses can still overrun the nominal
6 ms row-slice budget, and first visible-model binding still creates a
several-hundred-millisecond hitch. Those facts remain documented; the
maintainer's Fedora KDE observation now accepts the resulting two or three
minor micro-pauses for this release.

### A2S sweep diagnostics

`DZLL_A2S_SWEEP_DIAGNOSTICS=1` enables aggregate diagnostics; all other values
leave them disabled. The diagnostic writes directly to `stderr` because normal
startup logging filters informational module messages. Formatting and I/O
failures are contained.

Confirmed accounting semantics:

- the start marker records distinct eligible endpoints;
- the dispatched counter advances only after the executor accepts work;
- every current-generation callback advances completed attempts, including
  offline, timeout and exceptional results;
- responsive count requires a current-generation tuple/list payload whose
  information dictionary has `ok=True`;
- stale-generation callbacks return before mutating current counters;
- only exhausted state with an empty queue, zero in flight and
  `completed == dispatched` can emit `FULL A2S SWEEP COMPLETE`;
- cancelled and superseded generations emit a truthful terminal record but
  never the full-completion marker; and
- shutdown, DB replacement and post-publication failure record termination
  before incrementing the generation that rejects late callbacks.

The diagnostics do not change worker count, timeout, queue order, warmup policy,
result application or completion ownership. When disabled, the sink exits
before formatting or I/O.

## Live Fedora KDE acceptance evidence

### Combined experimental mode

Both startup feature flags were enabled.

- Eligible endpoints: 12,166
- Warmup threshold observed: 300/500
- Dispatched attempts: 12,166
- Completed attempts: 12,166
- Successfully responsive: 10,295
- Full sweep exhausted: 124.042 seconds
- Terminal queue: 0
- Terminal in flight: 0

### Normal mode

Both startup feature flags were disabled.

- Eligible endpoints: 12,166
- Warmup threshold observed: 922/1,414
- Dispatched attempts: 12,166
- Completed attempts: 12,166
- Successfully responsive: 10,443
- Full sweep exhausted: 122.278 seconds
- Terminal queue: 0
- Terminal in flight: 0

These maintainer-verified runs satisfy the live full-sweep completion criterion.
Because they were separate live-network runs, the 1.764-second elapsed
difference and 148-response difference are environmental observations, not an
A/B performance or correctness result.

The maintainer additionally verified substantially faster and smoother startup,
working filters, working search and working mod suggestions. Remaining minor
micro-pauses are explicitly accepted for this release.

## Lifecycle, failure and persistence review

### Generation and supersession

- Startup row batches carry a transaction generation and reject a mismatched or
  shutdown callback.
- A2S callbacks compare their generation before changing queue, completion,
  buffer or diagnostic state.
- Starting a replacement sweep terminates the prior diagnostic generation,
  increments the live generation and creates fresh queues and counters.
- Deferred suggestion restoration and server-mod cache writes retain their own
  generation and shutdown guards.

### Shutdown

- Shutdown terminates the active A2S diagnostic as cancelled before generation
  invalidation.
- Pending warmup and flush sources are removed, and late callbacks are rejected.
- Batched row staging is released on cancellation; shutdown during construction
  and late idle callbacks are covered by regression tests.
- Cache workers remain plain-data workers; no GTK object is transferred to or
  mutated from a worker.

### Rollback and terminal ownership

- Row-construction and idle-submission failures recover the startup overlay
  without starting A2S or publishing cache data.
- Pre-publication failure restores map choices, selection and filter state.
- Publication failure rolls back source/model, endpoint/live and cache state.
- Post-publication failure retains valid published rows, invalidates active A2S
  work, clears queue/buffer ownership, releases `_server_db_update_inflight` and
  attempts deferred-suggestion recovery.
- Empty and all-invalid input cannot publish an empty replacement over a valid
  warm cache.
- Completion callback failure is contained after ownership is released.

### Cache persistence

- Canonical tuples serialize back into the established disk representation.
- Failed staging or replacement preserves the previous cache.
- Provisional and failed DB loads cannot prune or publish a replacement cache.
- Generation change and shutdown reject late cache publication.
- V2 suggestion restoration schedules no migration, while valid V1 migration
  retains the established deferred, atomic publication path.

No lifecycle, rollback, shutdown or persistence gap was found in the cumulative
diff.

## Default-off and experimental-on verification

Fresh imports of the current source produced:

| Environment | Startup batching | A2S 500/60 | Sweep diagnostics |
|---|---:|---:|---:|
| Variables absent | `False` | `False` | `False` |
| Exact value `1` | `True` | `True` | `True` |
| Values `true` / `yes` / `on` | `False` | `False` | `False` |

Automated tests cover both synchronous/default and batched/experimental row
paths, default and 500/60 cohort calculations, distinct endpoint dispatch,
threshold and timeout reveal, sweep exhaustion, offline/exception/submission
failure, late updates, explicit sorting, supersession and shutdown.

The defaults were not changed by this review.

## Test results

### Focused regressions

Command scope:

`tests/test_browser_server_ingestion_validation.py`,
`tests/test_startup_batching.py`, `tests/test_a2s_warmup_500.py`,
`tests/test_a2s_sweep_diagnostics.py`,
`tests/test_deferred_mod_suggestions.py`,
`tests/test_startup_perf_instrumentation.py`,
`tests/test_startup_warmup_stable_live_list.py`,
`tests/test_core_startup_robustness.py` and
`tests/test_mod_suggestion_cache.py`.

Result: **242 passed, 15 skipped, 1 warning** in 0.40 seconds. The skips were GTK
display cases because the initial sandbox could not open its Xvfb socket; those
cases were subsequently executed by the unrestricted maintained suite.

### Maintained full display-backed suite

Command:

`xvfb-run -a -s '-screen 0 1440x900x24' env GDK_BACKEND=x11 GSK_RENDERER=cairo LIBGL_ALWAYS_SOFTWARE=1 python -m pytest -q -rs -p no:cacheprovider tests`

Result: **2,821 passed, 4 skipped, 90 warnings** in 12.10 seconds.

The four skips are unrelated detached Bob/Server Companion fixtures:

- `test_companion_restart_phase2_authority_consumers.py`
- `test_companion_restart_phase2_consumer_state.py`
- `test_companion_restart_phase2_schema4.py`
- `test_companion_restart_phase2_schema4_runtime.py`

All display-dependent GTK tests executed. Warnings are existing PyGObject/GTK
and Python asyncio deprecations.

### Static and diff validation

- Fresh-import gate matrix: passed.
- `python -m compileall -q src/dzll_launcher` and compilation of the changed
  tests: passed.
- `git --no-pager diff --check 005e4a8 --`: passed.
- Final checkpoint-to-worktree diff inspection: passed.

No test was suppressed, deselected or xfailed.

## Accidental-change and hygiene audit

No accidental implementation change was found.

- No Discord IPC file or behavior was modified.
- No CSS, theme, Steam, join-flow or Server Companion implementation was
  modified.
- No cache format, endpoint identity or join authority changed.
- No temporary benchmark wrapper, GC experiment, packed autocomplete prototype,
  copied profile or measurement script remains in the proposed scope.
- Existing general debug switches predate this diff. The only new diagnostic is
  the intentional exact-value, default-off A2S aggregate diagnostic.
- No `TODO`, `FIXME`, breakpoint, debugger import or unconditional diagnostic
  output was added by the cumulative source/test diff.
- Unrelated dirty and untracked files remain untouched and must not be staged by
  a future scoped commit.

## Outstanding risks

1. Automatic cyclic GC can still make individual experimental batches exceed
   the nominal 6 ms budget. This is a known measured limitation, not an unknown
   failure mode. The attempted GC policies were correctly rejected because they
   moved rather than eliminated pauses.
2. Initial visible-model binding retains a several-hundred-millisecond atomic
   hitch. The maintainer observes only two or three minor micro-pauses and has
   accepted them for this release.
3. Experimental startup retains a measured roughly 15.18 MiB settled USS and
   20.92 MiB full-run peak USS increase in matched isolated measurements. The
   separate 45-minute real-profile experiment plateaued without sustained idle
   growth, and the maintainer accepts the footprint.
4. The two live A2S runs were not simultaneous or deterministic. They prove
   completion, not comparative throughput or response-rate equivalence.
5. The opt-in A2S diagnostic writes synchronously to `stderr` at a few aggregate
   lifecycle points. It is disabled by default, I/O failures are contained and
   no per-endpoint output is produced.

None of these remaining risks requires another fix before the default change
under the maintainer's stated release acceptance.

## Exact files suitable for the next commit

The following 13 paths are the complete PR16-01 post-checkpoint scope suitable
for a future literal-allowlist commit. This review did not stage them.

1. `src/dzll_launcher/server_mod_cache.py`
2. `src/dzll_launcher/window.py`
3. `tests/test_browser_server_ingestion_validation.py`
4. `tests/test_startup_batching.py`
5. `tests/test_a2s_sweep_diagnostics.py`
6. `devReports/PR16_IMPROVEMENTS_TRACKER.md`
7. `devReports/PR16_01_MEMORY_ACCOUNTING_AUDIT.md`
8. `devReports/PR16_01_MEMORY_OPTIMISATION_RESULTS.md`
9. `devReports/PR16_01_GTK_RESPONSIVENESS_FOLLOWUP.md`
10. `devReports/PR16_01_A2S_SWEEP_DIAGNOSTICS.md`
11. `devReports/PR16_01_A2S_SWEEP_DIAGNOSTIC_VISIBILITY.md`
12. `devReports/PR16_01_FINAL_ACCEPTANCE.md`
13. `devReports/PR16_01_FINAL_INTEGRATION_REVIEW.md`

The historical `PR16_01_FINAL_ACCEPTANCE.md` accurately records why the earlier
decision was NOT READY; this report and the tracker record the subsequent live
evidence and superseding READY decision.

## Final recommendation

**READY.** Both startup experiments can become production defaults.

The recommendation is supported by measured startup improvement, accepted
desktop behavior, stable idle memory, completed full live sweeps in both modes,
correct default/experimental separation, preserved GTK-thread ownership,
successful lifecycle and failure coverage, and a clean maintained
display-backed suite.

No further fix is required before default enablement. Implement the default flip
as a separate narrow change with explicit tests for the new default and an
available rollback/disable path. Do not fold unrelated cleanup, Discord IPC,
CSS/theme or speculative responsiveness work into that change.
