# PR16-01 — final acceptance and release-readiness audit

Date: 2026-10-12

Recommendation: **NOT READY** to enable either experimental flag by default.

PR16-01 remains **IN PROGRESS**. `DZLL_STARTUP_BATCHING=1` and
`DZLL_A2S_WARMUP_500=1` remain opt-in. No source, test, default, cache format,
Discord IPC, CSS or theme change was made by this acceptance audit.

## Decision summary

The combined experiment produced a large, repeatable improvement in sampled
startup responsiveness and an earlier overlay dismissal on controlled inputs:

- median DB GTK completion improved from **1,897 ms to 1,494 ms**;
- median app-start-to-overlay-dismissal improved from **2.718 s to 2.228 s**;
- median maximum sampled pre-overlay main-loop gap improved from **1,955 ms to
  433 ms**; and
- all controlled runs published the same 12,101 source rows, initially displayed
  the same 9,588 filtered rows, and completed the same 12,101 synthetic A2S
  attempts.

No correctness defect was found in cache reuse, autocomplete, filtering,
explicit sorting, late result application, full synthetic sweep completion or
clean shutdown. The automated display-backed suite passed.

Default enablement nevertheless fails the existing acceptance boundary for
three specific reasons:

1. Every batched run materially exceeded the explicit 6 ms idle-slice budget:
   **59.6–77.6 ms**. The experiment report names repeated material slice-budget
   overruns as a rollback condition.
2. Atomic final publication still blocked GTK for **325–351 ms**, and the sampled
   combined pre-overlay gap remained **416–441 ms**. This is much better than the
   control, but there is no external rendered-frame or human input-latency
   evidence proving acceptable animation and interaction.
3. A live experimental A2S run revealed correctly at 300/500 completions, but
   did not complete the 12,101-endpoint sweep within the audit's bounded
   90-second observation. There is therefore no matched, completed live-A2S A/B
   set proving end-to-end sweep equivalence under current infrastructure.

These are acceptance blockers, not evidence that the experiment is corrupt or
that the measured memory is intrinsically excessive.

## Scope and reviewed evidence

The audit reviewed the current PR16-01 tracker and the implementation,
hardening, final-integration, GTK batching, deferred-suggestion, A2S 500/60,
cache verification, memory-accounting and memory-optimisation reports. Current
source and tests were treated as authoritative where report line numbers had
drifted.

The established memory evidence remains valid:

- the maintainer's real-profile 45-minute plateau was about **918.8 MiB RSS /
  815.8 MiB USS**, with no meaningful sustained idle growth;
- per-server cache canonicalisation is retained and passed the maintained
  suite; and
- the packed autocomplete prototype remains reverted because its traced-Python
  reduction did not translate to improved USS or latency.

This audit did not repeat a 45-minute interactive real-profile observation. It
adds matched isolated Xvfb measurements and does not substitute their lower
absolute memory figures for the real-profile plateau.

## Matched startup benchmark

### Controls

Six isolated profile copies were made before launch. The database and both
caches were byte-identical in every arm:

| Input | SHA-256 |
|---|---|
| `dzll-servers.db` | `68dfe4afadcedc38f6871c2293803d67b6a8ff5a362f9aed7a51cfae67774c4a` |
| `server_mods_v1.json` | `eff0d87203c98b22ede69a83527a2ee3f088c8bc155150fadbc2825d34a0bec3` |
| `mod_suggestions_v1.json` | `fda59c54f385e41cb32a450e9dfdcd4e8a3d4ccb98a59d919f431085220c0d88` |

The copied settings were also identical across arms. Update checks and Discord
RPC were disabled only inside all six copies to exclude unrelated network and
Discord behavior; the real profile was not changed. A controlled successful DB
fetch boundary used the copied database. A deterministic 4 ms synthetic A2S
response was used in both matched arms so network availability could not bias
startup timing. The synthetic response exercised successful late row mutation,
not merely failed-completion counting.

All runs used Python 3.14, GTK4 under isolated 1440x900 Xvfb, the Cairo renderer,
the same source tree and 20 ms external RSS/USS sampling. A 10 ms GLib timer
sampled main-loop gaps. The harness and sampler were identical in both arms.

- Normal arm: both feature variables unset; synchronous rows and default A2S
  1,414/65% cohort (target 920).
- Experimental arm: `DZLL_STARTUP_BATCHING=1` and
  `DZLL_A2S_WARMUP_500=1`; batched rows and 500/60% cohort (target 300).

This is an application-level GTK benchmark, but the synthetic fetch/A2S
boundaries mean its absolute overlay and full-sweep times are controlled
comparison values, not claims about Internet conditions.

### Timing and responsiveness

| Metric | Normal, 3-run range (median) | Experimental, 3-run range (median) | Median change |
|---|---:|---:|---:|
| DB GTK completion | 1,892–1,914 ms (**1,897**) | 1,465–1,508 ms (**1,494**) | **-403 ms (-21.2%)** |
| Rows first published from app start | 2.336–2.358 s (**2.344**) | 1.925–1.973 s (**1.953**) | **-390 ms (-16.6%)** |
| Overlay dismissed from app start | 2.712–2.739 s (**2.718**) | 2.201–2.249 s (**2.228**) | **-489 ms (-18.0%)** |
| Publication-to-overlay dismissal | 447–456 ms (**453**) | 354–357 ms (**356**) | **-97 ms (-21.4%)** |
| Maximum sampled pre-overlay main-loop gap | 1,954–1,973 ms (**1,955**) | 416–441 ms (**433**) | **-1,523 ms (-77.9%)** |
| Sampled p95 main-loop gap | 17.6–18.9 ms (**17.9**) | 17.5–17.9 ms (**17.7**) | no material change |
| Full synthetic sweep from app start | 5.011–5.031 s (**5.027**) | 4.855–5.026 s (**4.919**) | **-108 ms (-2.2%)** |

The experiment therefore materially improves the single worst startup block.
It does not achieve its nominal slice budget. `STARTUP_ROW_BATCH_BUDGET_NS` is
6,000,000 ns (`src/dzll_launcher/window.py:310`), while measured maximum slices
were 77.6, 70.2 and 59.6 ms. Final publication was the largest synchronous phase
in all three runs at 339.7, 325.3 and 350.9 ms. Instrumented `[PERF] main loop
stall` maxima were 580–610 ms in the experimental arm versus 2,019–2,037 ms in
the control; the external GLib sampler saw the slightly smaller ranges above.
Both measures agree on the direction and the remaining hitch.

No 60 fps video, compositor frame trace, physical pointer/keyboard event or
human interaction observation was available. `initial_next_frame_tick` remains
a frame-clock callback, not proof that a row frame was rendered. Smoothness and
input acceptance therefore cannot be claimed from these timings alone.

### Memory

Memory values below are process measurements from the same Xvfb runs, not a
decomposition of Python versus GTK/native allocations.

| Metric | Normal median | Experimental median | Difference |
|---|---:|---:|---:|
| Settled RSS, two seconds after full sweep | 693.25 MiB | 704.48 MiB | **+11.23 MiB (+1.6%)** |
| Settled USS, two seconds after full sweep | 601.02 MiB | 616.20 MiB | **+15.18 MiB (+2.5%)** |
| Maximum observed RSS through shutdown probes | 706.57 MiB | 725.75 MiB | **+19.18 MiB (+2.7%)** |
| Maximum observed USS through shutdown probes | 611.32 MiB | 632.24 MiB | **+20.92 MiB (+3.4%)** |

The maximum includes the identical post-sweep autocomplete, filter, clear and
explicit-sort probes, so it is a full-run transient maximum rather than a
startup-only high-water. The experimental overhead is consistent with private
batched row staging, deferred suggestion restoration and allocator timing. It
is measurable but small relative to the supplied real-profile plateau and is
not classified as a release blocker by itself. Most importantly, the earlier
45-minute real-profile run with both switches enabled found a stable plateau and
no sustained idle growth.

## A2S sweep, late updates and refresh lifecycle

### Controlled complete sweeps

Every matched run dispatched and completed exactly **12,101 of 12,101**
synthetic attempts. There was no duplicate or missing dispatch.

- Normal reveal occurred at the exact default target, 920/1,414.
- Experimental reveal occurred at the exact configured target, 300/500.
- The experimental queue continued after reveal and applied the remaining
  11,801 attempts before independent full-sweep completion.
- All initially visible objects had received the synthetic successful live
  update by completion.
- The initial visible membership remained stable during background late
  updates, as designed. Clearing the later search triggered a current-state
  rebuild from 9,588 to 10,854 visible rows; this confirms updated live filter
  inputs were retained rather than lost.

The stable-list behavior is intentional: late results update current-generation
objects without continuously splicing/reordering the view
(`window.py:7821-7919`). Consequently the view is not guaranteed to remain in
current-ping order after thousands of late mutations. A user-initiated ping sort
rebuilt the view and restored the canonical favourite/trusted-band/ping order in
the corrected probes. This matches the documented architecture and is not a new
defect.

### Live infrastructure check

An unsandboxed read-only A2S probe succeeded in 131 ms. One bounded live
experimental application run then established:

- 12,101 rows published through the batched path;
- longest instrumented startup stall 568 ms;
- 300/500 threshold reached after about 2.082 s of live A2S work;
- overlay dismissed about 4.068 s after app activation; and
- the full sweep did **not** complete within the fixed 90-second observation.

The harness then requested normal application shutdown and exited with status
zero. Because the full sweep did not finish, this run cannot establish final
live completion counts, late-update convergence or live normal/experimental
equivalence. A matched live control was not started after the experimental arm
had already exceeded the bounded observation; doing so would have doubled a
lengthy, still incomplete interactive-infrastructure check without satisfying
the requested full-sweep criterion.

### Automated failure and lifecycle evidence

The focused startup/A2S matrix passed **88 tests**. It directly covers:

- default and experimental cohort/ceiling calculations;
- deduplication and exactly-once distinct endpoint dispatch;
- success, offline, timeout and exceptional future completions;
- submission failure, truthful `sweep_exhausted`, and 20-second fallback;
- full sweep independence from early reveal;
- late results preserving stable membership;
- explicit sorting and Refresh All's separate final rebuild;
- user-changed sorting preserved across reveal;
- stale generation, supersession and shutdown rejection;
- row-build, idle-submit, publication, titlebar and post-publication failure
  recovery;
- deferred-suggestion submit/worker failure, retry, generation and shutdown;
  and
- cache readiness and instrumentation identity.

Relevant production guards remain at `window.py:7607-7919` for startup A2S and
`window.py:3092-3140` for shutdown invalidation. The automated evidence is
strong, but it does not turn the incomplete live sweep into a live pass.

## Display-backed GTK acceptance

The maintained suite was run under isolated Xvfb with software rendering:

`python -m pytest -q -p no:cacheprovider tests`

Result: **2,813 passed, 4 skipped**, 90 warnings, in 14.72 seconds. A repeated
run used `-rs` to identify all skips. They require detached Bob/Server Companion
comparison fixtures and are unrelated to PR16-01 display access:

- `test_companion_restart_phase2_authority_consumers.py`: detached Bob inputs;
- `test_companion_restart_phase2_consumer_state.py`: detached Bob fixtures;
- `test_companion_restart_phase2_schema4.py`: detached comparison sources; and
- `test_companion_restart_phase2_schema4_runtime.py`: detached prepared state.

The display-backed cases that were skipped in the headless 2,791/26 run now
executed, including real window construction, construction idles, GTK allocation,
column visibility, official-row UI, scrollbar binding and the real warning
overlay. The warnings were PyGObject/GTK/asyncio deprecations, not test failures.

The application-level probes additionally verified in both benchmark arms:

- 12,101 source objects and identical initial visible count;
- successful autocomplete (`codelock` returned eight ranked suggestions);
- live search reducing the view to six matching rows;
- clearing search restoring the current-state filtered view;
- explicit ping sort restoring the expected composite order; and
- clean application exit after the sweep/probes.

This is meaningful automated GTK coverage. It is not a substitute for a human
visual inspection of spinner continuity, partial/flickering rows, pointer/typing
latency, map interaction or compositor behavior.

## Confirmed findings versus untested risks

### Confirmed

1. **No correctness regression found.** Controlled parity, cache hits,
   autocomplete, filter, explicit sort, late updates, synthetic full sweeps,
   fault tests and display-backed tests pass.
2. **Responsiveness improves materially.** The worst sampled pre-overlay gap is
   about 78% lower and overlay dismissal is about 18% earlier in the controlled
   comparison.
3. **The slice budget is not met.** All three maximum slices are about 10–13
   times the configured 6 ms budget.
4. **A substantial atomic publication hitch remains.** Final publication is
   325–351 ms and dominates the experimental synchronous phases.
5. **Memory overhead is real but bounded in this comparison.** Settled USS is
   about 15.18 MiB higher and full-run peak USS about 20.92 MiB higher. The
   separate real-profile evidence shows stability rather than a leak.
6. **The bounded live run did not complete a full sweep.** Early live reveal
   worked; full live equivalence is not established.

No confirmed release-blocking data corruption, cache-integrity, autocomplete,
join-authority, GTK-thread ownership, duplicate-dispatch or shutdown defect was
found.

### Untested or incomplete

1. Matched normal versus experimental **completed live** A2S sweeps under the
   same network/server conditions.
2. External rendered-frame continuity and physical input latency during the
   325–441 ms remaining hitches.
3. A human display inspection of spinner animation, row reveal, filtering,
   maps and shutdown/reload while real A2S callbacks continue.
4. A new 45-minute matched normal/experimental settled-memory run. The existing
   experimental 45-minute plateau is accepted as stability evidence, but has no
   same-session normal arm.

These are explicitly unverified; they are not reported as passes or defects.

## Release blockers and acceptance conditions

### Blockers for default enablement

1. **Batch slicing:** reduce or explain the repeatable 59.6–77.6 ms maximum
   slices relative to the 6 ms budget, then repeat the matched benchmark. Do not
   optimize merely for the benchmark; identify the phase responsible.
2. **Rendered responsiveness:** capture external frame/input evidence showing
   acceptable behavior through the remaining final-publication hitch.
3. **Live A2S completion:** obtain at least one completed matched live sweep per
   arm, preferably three, confirming exact dispatch/completion totals, late row
   convergence, refresh lifecycle and shutdown/reload behavior.

### Conditions already satisfied

- identical input hashes and controlled three-run A/B startup comparison;
- stable real-profile 45-minute memory plateau;
- retained/tested per-server cache canonicalisation;
- packed autocomplete prototype rejected and absent;
- GTK/GObject construction remains on the GTK thread;
- cache formats and atomic persistence unchanged;
- automated display-backed and fault/lifecycle suites pass; and
- both flags remain default-off.

## Final recommendation

**NOT READY.** Keep both flags opt-in and keep PR16-01 IN PROGRESS.

The implementation is promising and substantially more responsive in the
controlled benchmark. The recommendation is driven by the project's own
acceptance rules, not by an arbitrary memory target: repeated slice-budget
failure, a remaining several-hundred-millisecond publication hitch without
rendered/input evidence, and the absence of a completed matched live A2S sweep
are specific blockers. The measured 15–21 MiB experimental USS overhead is a
documented tradeoff, not independently a reason to reject the work.

No mutating Git command was used. Nothing was staged or committed.
