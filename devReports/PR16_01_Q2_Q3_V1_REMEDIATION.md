# PR16-01 — Q2, Q3 and V1 Remediation Pass 2

Date: 2026-10-11

Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`

Status: **IN PROGRESS**

## Scope and constraints

This pass implemented only the confirmed Q2 and Q3 source corrections and the
V1 permanent regression coverage requested in:

- `devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md`
- `devReports/PR16_01_Q1_Q4_INDEPENDENT_VERIFICATION.md`

No synchronous-loader behaviour, A2S dispatch strategy, Discord IPC, Forge,
packaging, tracker, or `MEMORY.md` content was changed. Both experimental
switches remain exact-value, default-off environment switches:

- `DZLL_STARTUP_BATCHING=1`
- `DZLL_A2S_WARMUP_500=1`

DZLL and Steam were not launched. No network access or dependency installation
was used. No Git staging, commit, reset, clean, checkout, or other mutating Git
command was used. The pre-existing dirty and untracked working-tree contents
were preserved.

## Implementation

### Q2 — Batched map-choice rollback

`src/dzll_launcher/window.py` now snapshots the map model strings, selected
dropdown index, and cached filter state immediately before the existing map
model update. That snapshot is attached to the batched transaction state.

The snapshot is restored by the common batch failure path and by explicit batch
cancellation. This covers row-construction failure, publication failure,
idle-submission failure, supersession, and shutdown cancellation. Restoration
uses the existing filter-refresh suppression ownership around selection changes
and restores the cached filter state after the widget state, so a failed or
cancelled unpublished batch cannot leave a map choice derived from rows that
were never committed.

Successful publication does not invoke rollback, so the newly derived map
choices and the resulting selected filter state commit normally. The
synchronous loader retains its existing ordering and behaviour.

Transaction generation checks, GTK-thread object/model ownership, and existing
publication rollback remain unchanged.

### Q3 — Truthful A2S exhaustion reporting

`_maybe_finish_startup_rest_sweep()` now determines the terminal reveal reason
from the actual warmup accounting:

- `threshold` only when `completed >= target`;
- `sweep_exhausted` when the queue, in-flight work, and result buffer are empty
  while the readiness target remains unmet.

`_complete_startup_warmup()` accepts `sweep_exhausted` as an explicit reason.
Reveal-on-exhaustion is preserved, including its exactly-once guard, final
flush/sort, overlay completion, and timeout removal. No worker count, cohort
selection, queue order, query strategy, or full-sweep dispatch behaviour was
changed.

### V1 — Titlebar containment coverage

A permanent transient-titlebar regression test now makes only the dedicated
publication titlebar call fail. It proves that:

- the terminal outcome is exactly `published_titlebar_failed`;
- later post-load coordination still runs;
- A2S and Server Companion startup proceed;
- failure recovery is not entered.

The existing post-publication parametrization now names its titlebar case
`persistent_titlebar`, making clear that repeated failure reaches the later
unguarded titlebar call and is reported as `published_post_load_failed`.

## Permanent regression coverage

### `tests/test_startup_batching.py`

- cancellation restores the exact prior map choices, selected index, and
  cached filter state;
- row-construction failure restores the prior map/filter state;
- publication failure restores the prior map/filter state;
- idle-submission failure restores the prior map/filter state;
- successful publication commits the new map choices and selected filter;
- transient titlebar failure reports `published_titlebar_failed` and remains
  contained;
- persistent titlebar failure remains distinguished as
  `published_post_load_failed`.

### `tests/test_a2s_warmup_500.py`

A six-case matrix covers default and experimental warmup modes for:

- missing row objects;
- invalid query endpoints;
- executor submission failures.

Every case asserts complete full-sweep accounting, zero readiness completions,
the expected target, exactly one reveal, exactly one threshold/reveal/sweep
instrumentation record, timeout removal, and `sweep_exhausted` in both warmup
markers.

## Files changed by this pass

- `src/dzll_launcher/window.py`
- `tests/test_startup_batching.py`
- `tests/test_a2s_warmup_500.py`
- `devReports/PR16_01_Q2_Q3_V1_REMEDIATION.md`

All other dirty or untracked paths shown by the baseline and final
`git status --short` pre-existed this pass and were left untouched.

## Verification

All Python commands used an isolated temporary HOME/XDG profile rooted at
`/tmp/dzll-q2q3v1.qvx46R`; the real user profile was not used.

1. Focused Q2/Q3/V1 tests:

   `python -m pytest -q tests/test_startup_batching.py tests/test_a2s_warmup_500.py`

   Result: **45 passed**, 1 warning.

2. Adjacent startup/ingestion tests:

   `python -m pytest -q tests/test_startup_batching.py tests/test_a2s_warmup_500.py tests/test_browser_server_ingestion_validation.py tests/test_core_startup_robustness.py`

   Result: **154 passed, 15 skipped**, 1 warning.

3. Required full suite:

   `python -m pytest -q tests`

   Result: **2784 passed, 26 skipped**, 1 warning in 13.06 seconds.

4. Python compilation:

   `python -m compileall -q src/dzll_launcher tests/test_startup_batching.py tests/test_a2s_warmup_500.py`

   Result: **passed**.

5. Whitespace/diff validation:

   `git --no-pager diff --check`

   Result: **passed**.

   A direct trailing-whitespace scan of the two untracked regression-test files
   and the modified production file also found no matches.

The only warning in the pytest runs was the existing PyGObject deprecation
warning for `GLib.unix_signal_add_full`.

## Result

Q2 and Q3 are remediated with focused transaction/accounting changes, and V1 is
closed by permanent branch-specific regression coverage. PR16-01 remains
**IN PROGRESS**; no production-default or runtime-performance acceptance claim
is made by this offline pass.
