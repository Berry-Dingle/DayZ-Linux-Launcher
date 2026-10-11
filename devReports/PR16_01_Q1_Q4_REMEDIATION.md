# PR16-01 Remediation Pass 1: Q1 and Q4

Date: 2026-10-11

Status: IMPLEMENTED AND OFFLINE-VERIFIED; PR16-01 REMAINS IN PROGRESS

## Scope

This pass addresses only findings Q1 and Q4 from
`devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md`, using the integration context in
`devReports/PR16_01_FINAL_INTEGRATION_AUDIT.md`.

No tracker or project-memory update was made. Neither experiment was enabled by
default. No Discord IPC, progress UI, Forge, packaging, release, or unrelated
source behavior was changed.

## Implementation

### Q1: zero-valid-row batched startup

Changed `src/dzll_launcher/window.py`:

- `_apply_db_rows()` now selects the established synchronous path when endpoint
  validation produces zero rows. With startup batching enabled, the recorded
  fallback reason is `no_valid_rows`.
- The synchronous empty path therefore remains authoritative for both a truly
  empty input and an all-invalid input: it reports `loaded=False`, displays the
  existing empty-state text, leaves Server Companion rows unloaded, and does not
  start A2S.
- `_publish_startup_row_batches()` now has a defensive zero-object terminal path
  in case an empty transaction reaches the batching layer directly. It clears
  batch state and database in-flight ownership, invokes the normal empty-state
  presentation, records the truthful `empty` completion outcome, and neither
  authorizes deferred suggestions nor performs a row publication.
- Deferred suggestion restoration is armed only after a non-empty row
  publication. `_maybe_schedule_deferred_suggestion_restore()` also refuses to
  submit a worker when the published endpoint map is empty.
- `_schedule_server_mod_cache_write()` now refuses all falsey candidates. This
  prevents an accidental empty cache candidate from replacing a valid warm
  per-server cache even if the scheduling method is called independently.

These checks preserve the pre-existing synchronous code path rather than
introducing a second empty-state implementation.

### Q4: post-publication exception containment

Changed `src/dzll_launcher/window.py`:

- Once source and visible stores have been published successfully, the batch
  transaction state and idle-source ownership are released before post-load
  coordination begins.
- Titlebar coordination is contained and logged. A titlebar failure does not by
  itself prevent subsequent startup coordination.
- The complete post-publication sequence, including performance reporting,
  Server Companion restoration, A2S startup submission, and subsequent startup
  coordination, is enclosed by an explicit failure path.
- A post-publication exception keeps the already valid source and visible rows,
  while invalidating the partial startup-live generation, removing known
  warmup/flush sources, clearing queued/buffered A2S work and in-flight counts,
  completing or releasing the startup presentation, and marking suggestion
  fallback readiness.
- Database-update ownership is cleared on both successful and failed terminal
  paths. The completion callback receives `published`,
  `published_titlebar_failed`, or `published_post_load_failed` as appropriate;
  callback exceptions are logged after ownership is released, and the
  implementation does not use an unconditional success-reporting `finally`.
- The pre-publication/transaction-failure handler now also clears database
  in-flight ownership in its own failure-valued terminal `finally`, even if
  failure presentation itself raises.
- If deferred suggestions were pending, failure recovery authorizes and submits
  restoration only after database in-flight ownership has been released. A
  scheduling failure is contained and logged, leaving the normal retry state
  intact.

### Failure-path semantics

| Point of failure | Published rows | A2S state | Startup ownership | Suggestions | Terminal outcome |
| --- | --- | --- | --- | --- | --- |
| Empty/all-invalid before batching | None | Never started | Synchronous completion | Not scheduled | Synchronous `loaded=False` behavior |
| Defensive empty batch terminal | None | Never started | Batch state and DB in-flight cleared | Not scheduled | `empty` |
| Publication transaction failure | Rolled back by existing controlled rollback | Not started | Failure handler clears DB in-flight ownership | Existing failure recovery | `publication_failed` |
| Titlebar failure before post-load work | Valid rows retained | Coordination continues where possible | Cleared at terminal completion | Normal or recovery scheduling | `published_titlebar_failed`, unless a later coordination step also fails |
| Server Companion/A2S/other post-publication failure | Valid rows retained | Partial generation invalidated; known queued state and sources cleared | Cleared before deferred recovery | Authorized and recoverably resubmitted | `published_post_load_failed` |

## Regression coverage

Changed `tests/test_startup_batching.py`:

- Parameterized empty-input and all-invalid-endpoint coverage verifies the
  synchronous fallback, exact empty-state UI, `loaded=False` consequences, no
  Server Companion restore, no A2S submission, no 25-second cache timer, warm
  cache preservation, and the `no_valid_rows` instrumentation reason.
- A direct empty batch transaction verifies defence-in-depth: truthful `empty`
  completion, cleared transaction/in-flight state, correct empty UI, no A2S or
  Server Companion work, no deferred suggestion arming, no cache timer, and
  preservation of warm cache state.
- The direct empty-cache scheduling guard is exercised with an executor that
  fails the test if staging is attempted.
- Parameterized post-publication injection covers titlebar coordination, Server
  Companion restoration, and A2S submission exceptions. Each case verifies
  retained valid rows, cleared batch/DB ownership, truthful failure outcome,
  fallback readiness, successful deferred autocomplete restoration, and the
  ability to submit a later manual database refresh.
- Existing publication-failure coverage now also asserts that rollback releases
  database in-flight ownership.

Changed `tests/test_deferred_mod_suggestions.py`:

- Added a test proving an empty published endpoint map cannot submit a deferred
  suggestion worker or mark the suggestion catalogue ready.

The existing startup batching tests continue to cover GTK-idle construction,
generation cancellation, shutdown rejection, publication rollback, synchronous
fallbacks, cache behavior, publication ordering, and deferred catalogue timing.

## Verification

All commands were run locally without launching DZLL or Steam and without
network access or dependency installation.

- `PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-q1q4-pyc python -m py_compile src/dzll_launcher/window.py tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py`
  - PASS; no output.
- `python -m pytest -q tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py`
  - PASS: 29 passed, 1 warning in 0.22 seconds.
- Broader focused startup/cache/ingestion set:
  - PASS: 227 passed, 15 skipped, 1 warning in 0.39 seconds.
- `python -m pytest -q tests`
  - PASS: 2775 passed, 26 skipped, 1 warning in 10.94 seconds.
- `git --no-pager diff --check`
  - PASS; no whitespace errors.

The warning in each pytest run is the existing PyGObject deprecation warning
for `GLib.unix_signal_add_full`; it is unrelated to Q1/Q4.

## Files changed by this remediation pass

- `src/dzll_launcher/window.py`
- `tests/test_startup_batching.py`
- `tests/test_deferred_mod_suggestions.py`
- `devReports/PR16_01_Q1_Q4_REMEDIATION.md`

The working tree already contained other tracked modifications and many
untracked PR16/release/report artifacts. They were preserved. No Git staging,
commit, reset, clean, checkout, or history-changing command was used.

## Residual risks and acceptance boundary

- The exception tests use focused host doubles and deterministic executor/GLib
  queues. They verify ownership, generation, retained-row, retry, and manual
  refresh semantics offline, but they do not constitute a live GTK runtime test.
- A post-publication exception deliberately favors retaining already valid rows
  over rolling back the browser. The affected A2S startup generation is
  invalidated and its known local queues are cleared; already executing worker
  calls cannot be forcibly stopped, but generation checks must reject their late
  results.
- If deferred suggestion submission itself fails during recovery, the existing
  retry-after policy remains responsible for a later retry. The failure no
  longer remains blocked by stale database in-flight ownership.
- The default-off experiment switches remain unchanged:
  `DZLL_STARTUP_BATCHING=1` and `DZLL_A2S_WARMUP_500=1` are still required to opt
  in.
- Final acceptance should include a maintainer runtime fault-injection pass in a
  real GTK session for the three post-publication failure points, confirming the
  overlay is released, published rows remain interactive, a manual refresh can
  be initiated, and autocomplete becomes available. Runtime verification should
  also repeat empty and all-invalid startup with an existing warm per-server
  cache and confirm the cache file remains byte-for-byte unchanged.

PR16-01 remains IN PROGRESS pending that final acceptance evidence and the
separate disposition of the remaining independent-audit findings. The known
Discord Rich Presence startup hang remains explicitly out of scope for this
change.
