# PR16-01 — Independent Verification of Q2, Q3 and V1 Remediation

Date: 2026-10-11
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`
Verified against: working tree at `de504a4` plus all dirty/untracked PR16-01 material
Remediation under review: `devReports/PR16_01_Q2_Q3_V1_REMEDIATION.md`
Prior findings source: `devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md` (Q2, Q3),
`devReports/PR16_01_Q1_Q4_INDEPENDENT_VERIFICATION.md` (V1)

Verification mode: **strictly read-only.** No repository source, test, tracker or
`MEMORY.md` file was modified. Nothing was staged or committed. No mutating Git
command was run. DZLL and Steam were not launched. No network operation,
dependency installation, Forge, Discord IPC or packaging action occurred. All
dirty and untracked files were preserved. Temporary probes were written only
under `/tmp/pr16_audit_probe/`. Both experiment switches remain exact-value,
default-off (`window.py:306-307` — verified unchanged).

PR16-01 status: **IN PROGRESS** (unchanged).

---

## 1. Verdict

| Item | Status | Confidence |
|---|---|---|
| **Q2** — map choices survived a failed/cancelled batched transaction (= Sol L1) | **FIXED AND VERIFIED** | High — reproduced corrected behaviour in 11 probes across all five failure/cancel paths |
| **Q3** — sweep exhaustion claimed `threshold` below the cohort target (= Sol L2) | **FIXED AND VERIFIED** | High — reproduced corrected behaviour in 13 probes across both warmup modes and all three non-counting drain paths |
| **V1** — the repository's `titlebar` parametrization never reached `published_titlebar_failed` | **CLOSED** | High — reproduced both branches independently |

**No regression was introduced by this pass.** The full offline suite passes, the
previously verified Q1 and Q4 behaviour still holds (I re-ran those 22 probes
unchanged), scope is contained to `window.py` plus the two PR own test files, and
the happy path, generation guards, shutdown rejection and GTK-thread ownership
are all behaviourally unchanged.

I found **one new Low coverage gap (G1)** that this pass leaves open, plus three
Low/informational observations. None is a source defect and none blocks the
scoped default-off commit.

**Disposition:** Q2, Q3 and V1 are closed. Combined with the prior verification,
**all six confirmed defects from the independent audit (Q1–Q6) and V1 are now
resolved except Q5 and Q6**, which remain open and were explicitly out of scope
here. The remaining pre-commit blocker is **M1** (stale tracker). Production-default
acceptance remains **blocked** on the runtime, memory and display-backed evidence
carried forward from the first audit.

---

## 2. Method

I re-derived both fixes from source rather than accepting the remediation report's
description, then wrote probes asserting the **correct post-remediation behaviour**
so each one fails if the fix regresses. Where the repository's own tests leave a
branch unexercised, I probed that branch specifically rather than trusting the
green suite.

Probe files under `/tmp/pr16_audit_probe/` (outside the repository, reusing the
repo's `tests/conftest.py` isolated-profile boundary):

| File | Probes | Focus |
|---|---|---|
| `test_verify_q2.py` | 11 | Q2: five failure/cancel paths, success commit, load-bearing `_filter_state` restore, supersession chaining, suppression-depth leak, snapshot degradation |
| `test_verify_q3_v1.py` | 15 | Q3: six-case mode × drain-path matrix, threshold truthfulness, empty cohort, timeout, stale generation, shutdown, sweep continuation; V1: both titlebar branches |
| `test_verify_q1_q4.py`, `test_verify_edges.py` | 22 | Re-run unchanged from the prior verification to detect regressions |

Result: **48 passed**. Two of my own probes failed on first run; both were
harness expectation bugs, not source defects, and are recorded in §6 so the
distinction is auditable.

Repository gates re-run:

```text
python -m pytest -q tests
2806 passed, 4 skipped, 66 warnings in 12.99s          (display available)

python -m pytest -q tests/test_startup_batching.py tests/test_a2s_warmup_500.py
45 passed, 1 warning in 0.16s

python -m pytest -q tests/test_startup_batching.py tests/test_a2s_warmup_500.py \
  tests/test_browser_server_ingestion_validation.py tests/test_core_startup_robustness.py \
  tests/test_deferred_mod_suggestions.py
179 passed, 32 warnings in 1.21s                       (0 skipped, display available)

PYTHONPYCACHEPREFIX=/tmp/dzll-q2q3v1-pyc python -m compileall -q \
  src/dzll_launcher tests/test_startup_batching.py tests/test_a2s_warmup_500.py
passed

git --no-pager diff --check
passed, no output
```

Trailing-whitespace scan of the three touched files returned 0 matches in each.

**Baseline reconciliation.** The remediation report quotes `2784 passed, 26 skipped`
(root) and `154 passed, 15 skipped` (adjacent). Those are headless figures. On
this display-capable machine the same commands give **2806 passed, 4 skipped**
and **179 passed, 0 skipped**. `2806 − 2784 = 22` and `26 − 4 = 22` — exactly
the display-dependent set, which executes and passes here. Both runs are green;
this is the same environment artefact recorded in the prior two reports, not a
regression. Relative to the previous pass the root suite grew by 9 tests
(2797 → 2806) with no change in skips, matching the seven new Q2 tests plus the
six-case Q3 matrix replacing/adding cases.

**Scope containment verified.** `app.py` (14), `mod_search.py` (23),
`mod_suggestions.py` (19) and the three modified test files (578 / 59 / 5) have
insertion counts identical to the previous pass, so this change touched only
`window.py` (1697 → 1742, +45) and the two untracked PR16-01 test files. No
unrelated dirty or untracked file was altered. `.qwen/` remains a CLI session
artefact and must be excluded from any commit.

---

## 3. Q2 verification — batched map-choice rollback

### 3.1 Implementation reviewed

- **Snapshot captured before mutation** — `window.py:5900`:
  `previous_map_state = DZLLWindow._snapshot_startup_map_state(self)`, which
  precedes both `_set_map_choices(choices)` at `window.py:5911` and the
  exception fallback `_set_map_choices(["All Maps"])` at `window.py:5920`. This
  is the ordering the fix depends on, and it is correct.
- **Snapshot contents** — `window.py:6533-6547`: the map model strings as a
  tuple, the selected dropdown index, and a `dict()` copy of `_filter_state`.
  The whole read is wrapped in `try/except → return None`, so unusable widgets
  degrade to "no snapshot" rather than raising during startup.
- **Threading** — passed as a keyword into `_start_startup_row_batches`
  (`window.py:5953`, parameter default at `6056`) and stored in the transaction
  state at `window.py:6124`. Default `None` keeps direct callers safe.
- **Restore helper** — `window.py:6549-6565`. Restores the model via
  `_set_map_choices`, then explicitly re-applies the snapshotted selected index
  under filter-refresh suppression, then restores `_filter_state`. The
  `_filter_state` assignment sits **outside** the widget `try`, so a widget
  failure still leaves the cache consistent.
- **Restore call sites** — `window.py:6572` inside `_fail_startup_row_batches`
  (def `6567`) and `window.py:6603` inside `_cancel_startup_row_batches`
  (def `6600`). Those two cover every terminal non-publication outcome.
- **Not called on success** — verified by grep: `_restore_startup_map_state` has
  exactly two call sites, neither in `_publish_startup_row_batches`.

### 3.2 Path-by-path challenge

| Path | Restore invoked? | Verified |
|---|---|---|
| `row_construction_failed` | via `_fail_startup_row_batches` (`6572`) | Probe: full `mstate()` equality, depth 0, no A2S, no 25 s timer |
| `publication_failed` | via `_fail_startup_row_batches` (`6572`) | Probe: same, plus `_obj_by_key`/`live` rollback intact |
| `idle_submission_failed` | via `_fail_startup_row_batches` (`6572`) | Probe: `_apply_db_rows` returns `False`, not the pending sentinel; state restored |
| `superseded` cancellation | via `_cancel_startup_row_batches` (`6603`) | Probe: late idle callback still rejected; `removed == [1]` |
| `shutdown` cancellation | via `_cancel_startup_row_batches` (`6603`) | Probe: restore runs during teardown without raising; no A2S, no publication |
| Successful publication | **not** invoked | Probe: `map_model == ["All Maps", "Chernarus"]`, all 7 rows committed |

### 3.3 Consistency of choices, selected index and cached filter state

All three are asserted together in every failure probe via a single tuple
comparison, so a partial restore cannot pass:

```python
def mstate(host):
    return (list(host.map_model.rows), host.map_dropdown.get_selected(),
            dict(host._filter_state), host._filter_refresh_suppress_depth)
```

Two mechanisms make the index restore correct rather than merely cosmetic:

1. `_set_map_choices` (`window.py:2383-2409`) preserves the selection **by value**
   (`old_val` lookup, falling back to index 0 when the value is gone). After a
   failed transaction the model holds the *new* choices, so value-preservation
   alone would land on the wrong entry; the explicit
   `map_dropdown.set_selected(int(snapshot["selected"]))` at `window.py:6559`
   corrects it. Probes confirm the original index 1 is recovered, not 0.
2. Both the model splice and the explicit `set_selected` run under
   `_push_filter_refresh_suppression()` / `_pop_filter_refresh_suppression()`
   (`window.py:2404-2409` and `6557-6561`), so no `_on_filter_changed` fires
   mid-restore and the visible store is not rebuilt from a half-restored model.

**GTK signal suppression — balanced.** Both push/pop pairs are in `try/finally`.
I probed the worst case (`map_model.splice` raising inside restore): the helper
catches and logs, `_filter_state` is still restored, and
`_filter_refresh_suppress_depth` returns to **0**. No depth leak, which matters
because a leaked depth would silently suppress every later filter refresh for
the session.

**Generation ownership — correct.** `_fail_startup_row_batches` checks
`state.get("generation") != generation` and returns *before* restoring
(`window.py:6568-6571`), so a stale failure cannot restore over a newer
transaction. `_cancel_startup_row_batches` restores *before* bumping the
generation (`6603` then `6604-6606`), so the restore still sees the state it is
cancelling. Both orderings are right, and they differ deliberately.

**Re-entrancy and supersession chaining — correct.** `_apply_db_rows` cancels a
live transaction at `window.py:5857-5858`, i.e. **before** capturing its own
snapshot at `5900`. A superseding transaction therefore snapshots the *restored*
state, not the dirty one. I probed the full chain (A starts → A dirties the model
→ B supersedes → A restores → B snapshots → B cancelled) and the final state
equals the original exactly, with depth 0. No drift accumulates across
generations.

**Could rollback overwrite a newer legitimate user selection?** I looked for this
specifically and it is **not reachable**:

- `startup_dimmer` is a `Gtk.Box` with `set_can_target(True)` added as an overlay
  child (`startup_ui.py:19-24`, `startup_ui.py:66`), and `_set_updating(True, …)`
  (`window.py:1586`) keeps it visible for the whole startup, including the entire
  batched build window. A `can_target` overlay child absorbs pointer input over
  the browser, so the map dropdown cannot be operated during the transaction.
- The batched path is reachable only from `apply_db_rows_once` with
  `allow_startup_batching=True`, i.e. startup only. `_manual_update_server_database`
  passes `allow_startup_batching=False` and is additionally gated by
  `_server_db_update_inflight`, which the batch holds set until `complete()`.
- Keyboard/programmatic routes to the map selection during startup would have to
  bypass the dimmer; I found none.

**Residual (theoretical, not a defect):** the restore is unconditional with
respect to *when* the snapshot was taken. If input blocking were ever weakened —
e.g. the dimmer made non-targetable, or a keyboard shortcut routed to the map
dropdown during startup — a rollback would silently discard a selection made
inside the snapshot→restore window. The window is bounded by one startup
transaction, and the failure it protects against is rarer than the clobber would
be. I would not change the design; I would add a one-line comment recording that
correctness depends on the dimmer blocking input.

### 3.4 Asymmetry noted — defensive empty terminal does not restore

The defensive empty-batch terminal at `window.py:6288-6310` (which reports
`complete("empty")` at `6307`) does **not** call `_restore_startup_map_state`.
I checked whether that is a hole and it is not:

- Via `_apply_db_rows` it is unreachable — the `no_valid_rows` gate at
  `window.py:5942-5944` routes empty loads to the synchronous loader, so a
  batched transaction always has at least one row.
- Via a direct `_start_startup_row_batches(host, [], …)` call, `previous_map_state`
  defaults to `None` (`window.py:6056`), so `_restore_startup_map_state` returns
  immediately at `window.py:6550-6553`. There is nothing to restore.
- `state["objects"]` cannot be empty while `state["rows"]` is non-empty:
  `_build_startup_batched_row` either appends or raises.

So the asymmetry is unreachable in production and harmless in tests. Recorded for
completeness; if a future change made empty batches reachable through
`_apply_db_rows`, the map model would end at `["All Maps"]` — which is the correct
end state for zero servers anyway.

### 3.5 Repository coverage vs probe coverage

The repository's four failure tests (`test_cancellation_rejects_late_idle_callback_without_publication`,
`test_row_construction_failure_recovers_overlay_without_a2s_or_cache_timer`,
`test_publication_failure_rolls_back_and_recovers_overlay`,
`test_idle_submission_failure_restores_map_choices_and_filter_state`) all bind the
**real** `_set_map_choices` and the real suppression helpers via
`install_map_state()`, and assert full `map_state()` equality. That is genuinely
strong coverage of the widget-level restore — considerably better than a stub.

**However, `_filter_state` restoration is not exercised as a change.** In all four
tests `_filter_state` at restore time already equals the snapshot, because
`_set_map_choices` suppresses `_on_filter_changed` and the publication-failure
test replaces `_on_filter_changed` with a lambda that raises *before* touching
the cache. Deleting `window.py:6565` would leave the entire repository suite
green.

I proved that line is load-bearing with
`test_q2_filter_state_restore_is_load_bearing`: an `_on_filter_changed` that
assigns `host._filter_state = {..., "marker": "PUBLISHED-DIRTY"}` and then raises
leaves a dirty cache, and the restore correctly reverts it to `marker == "original"`.
This mirrors the real `_on_filter_changed`, which assigns `self._filter_state = new_state`
at `window.py:10576` (and at `10593` via `_apply_filter_changed(state=new_state)`)
*before* the visible-store rebuild that can raise.

This is finding **G1** (§5).

---

## 4. Q3 verification — truthful A2S exhaustion reporting

### 4.1 Implementation reviewed

`_maybe_finish_startup_rest_sweep` (`window.py:7880`) now derives the reason
(`window.py:7897-7903`):

```python
if not self._startup_warmup_revealed:
    reveal_reason = (
        "threshold"
        if self._startup_warmup_completed >= self._startup_warmup_target
        else "sweep_exhausted"
    )
    self._complete_startup_warmup(generation, reveal_reason=reveal_reason)
```

and `_complete_startup_warmup` accepts it (`window.py:7708`):

```python
if reveal_reason not in {"threshold", "timeout", "sweep_exhausted"}:
```

Adding `sweep_exhausted` to the accepted set is essential — without it the
derivation at `window.py:7709-7714` would have silently rewritten the new label
back to `timeout`. Both halves of the fix are present.

### 4.2 Every reveal site audited

There are five `_complete_startup_warmup` call sites. I checked each against the
criterion "`threshold` must never be reported unless `completed >= target`":

| Site | Reason | Truthful? |
|---|---|---|
| `window.py:7677` — `not keys` | `threshold` | Yes, vacuously: `warmup_keys` empty ⇒ `target == 0` (`window.py:7639-7641`) and `completed == 0`, so `0 >= 0` holds |
| `window.py:7693` — empty cohort, non-empty keys | `threshold` | Yes, vacuously: same `target == 0`. The full sweep still runs afterwards via `_pump_startup_rest_sweep` |
| `window.py:7701` — timeout | `timeout` | Yes |
| `window.py:7848` — future completion | `threshold` | Yes: guarded by `completed >= target` at `window.py:7845-7846` in the same expression |
| `window.py:7903` — exhaustion | derived | Yes: `threshold` only when the target is met, else `sweep_exhausted` |

**No path can now report `threshold` with `completed < target`.** Verified
empirically across cohort sizes 1–8 in both warmup modes
(`test_q3_exhaustion_arm_never_reports_threshold_below_target`).

### 4.3 The three non-counting drain paths

The asymmetry Q3 described is still present in the pump — correctly so, since it
is the accounting reality — and is now truthfully labelled:

| Path | Sweep counter | Warmup counter |
|---|---|---|
| `window.py:7788-7791` — object missing from `_obj_by_key` | incremented | not incremented |
| `window.py:7792-7797` — `ip`/`qport` coercion failure | incremented | not incremented |
| `window.py:7810-7812` — executor `submit` failure (inflight decremented) | incremented | not incremented |
| `window.py:7819-7825` — future completion | incremented | incremented iff `key in _startup_warmup_keys` |

All three drain paths are covered by the repository's new six-case matrix
(`tests/test_a2s_warmup_500.py:211-261`, `default`/`experimental` ×
`missing`/`invalid`/`submission`) and independently by my probes. In every case:

```
_startup_live_rest_completed == len(keys)      # complete full-sweep accounting
_startup_warmup_completed    == 0              # zero readiness completions
_startup_warmup_target       == 2              # expected ceiling target
events.count("reveal")       == 1              # exactly-once reveal
len(threshold) == len(reveal) == len(sweep) == 1
threshold[0]["reason"]       == "sweep_exhausted"
reveal[0]["reveal_reason"]   == "sweep_exhausted"
removed == [41] and _startup_warmup_timeout_id == 0   # timeout removed
_suggestion_sweep_complete is True
```

### 4.4 Exactly-once reveal, accounting, timeout removal, continued sweep

- **Exactly once:** the `_startup_warmup_revealed` guard at `window.py:7706-7707`
  is intact, and the reveal flag is set *before* the flush (`window.py:7727`),
  so the re-entrant `_maybe_finish_startup_rest_sweep` call from
  `_flush_startup_rest_results` cannot reveal twice. Probed: one `reveal` event
  and one of each instrumentation marker in all six matrix cases, and in the
  genuine-threshold, empty-cohort and timeout cases.
- **Complete accounting:** `_startup_live_rest_completed == _startup_live_rest_total`
  in every exhaustion case, and in the success case results are applied across
  multiple flushes totalling all rows (observed `[3, 1]` for 4 keys — the flush
  threshold fires mid-sweep, then the reveal flushes the remainder). Total
  applied always equals the row count.
- **Timeout removal:** `removed == [41]` and `_startup_warmup_timeout_id == 0`
  after every reveal, including the timeout path itself.
- **Continued sweep:** revealing at the target does not stop the sweep. With 10
  keys and a target of 6, `completed` reaches 10 and `_suggestion_sweep_complete`
  becomes True while exactly one reveal fired. The vacuous empty-cohort reveal
  also leaves the full sweep running.

### 4.5 Both warmup modes

Verified for `default` (65 %, full eligible band-0/1 cohort) and `experimental`
(60 %, first 500 of the band prefix): target arithmetic, cohort size, reason
derivation and exactly-once semantics all match. The exhaustion branch is
mode-independent, so — as I noted in the original audit — the fix also corrects
the default mode's labelling, not just the experiment's. No worker count, cohort
selection, queue order or dispatch strategy changed; `_submit_startup_rest_batches`
(`window.py:7605`) is otherwise untouched.

### 4.6 Stale generations, shutdown, timeout

- **Stale generation:** `_maybe_finish_startup_rest_sweep` (`window.py:7881-7882`)
  and `_complete_startup_warmup` (`window.py:7704-7705`) both reject a mismatched
  generation before any work. Probed: no reveal, no marker.
- **Shutdown:** `_pump_startup_rest_sweep` returns early on
  `_shutdown_cleanup_done` (`window.py:7781-7782`). Probed: queue left
  un-drained, no reveal, no marker.
- **Timeout:** with genuinely pending futures (a future whose `add_done_callback`
  never fires), the sweep stays unrevealed, the `STARTUP_WARMUP_MAX_WAIT_SECS`
  source is scheduled, and `_on_startup_warmup_timeout` reveals with
  `reveal_reason == "timeout"` exactly once while `_startup_live_rest_completed`
  stays 0 — correctly distinguishing "the gate fired" from "the sweep finished".

### 4.7 Observation — the `threshold` arm of the new ternary is unreachable

`_startup_warmup_completed` has exactly **one** increment site
(`window.py:7825`), inside `_on_startup_rest_future_done`, and the threshold
reveal check at `window.py:7845-7848` runs in the same callback immediately
afterwards. Therefore whenever `completed >= target`, `_startup_warmup_revealed`
is already True and the guard at `window.py:7897` skips the branch entirely. The
`"threshold"` arm at `window.py:7899-7900` can never be taken.

This is harmless defensive code and I verified the unreachability empirically
across cohort sizes 1–8. It is recorded only because it means the ternary always
evaluates to `sweep_exhausted` in practice, so a future reader may believe the
branch is tested when no test can reach it.

### 4.8 Residual semantic note — `threshold` with a zero target

When the cohort is empty, `target == 0` and the reveal is labelled `threshold`
with `completed == 0`. Under the `completed >= target` definition this is
truthful, and it matches the documented "empty cohort reveals immediately"
design, so I do **not** class it as a defect. It is trending-relevant only:
anyone filtering A2S traces on `reason == "threshold"` to mean "the readiness
cohort was actually satisfied" will also pick up zero-target startups. If that
distinction matters when accepting the 500/60 experiment, a separate
`no_cohort` reason at `window.py:7677` and `7693` would disambiguate. Optional.

---

## 5. V1 verification and remaining coverage gaps

### 5.1 V1 — closed

- **New permanent test** `tests/test_startup_batching.py:665-696`
  (`test_transient_titlebar_failure_reports_dedicated_contained_outcome`) fails
  only the *first* titlebar call and asserts `outcomes == ["published_titlebar_failed"]`,
  `titlebar_calls == 2`, companion and A2S ran, `_suggestion_sweep_fallback_ready is False`,
  and `overlay_complete` not in events. That reaches the dedicated containment
  branch at `window.py:6364-6369`, which no test previously asserted.
- **Renamed parametrization** at `tests/test_startup_batching.py:581` —
  `("persistent_titlebar", "companion", "a2s")` — makes the escalation explicit
  instead of implying the titlebar case tests containment.

I verified both branches independently:

| Case | Outcome | Failure recovery | Rows | Cache timer |
|---|---|---|---|---|
| Transient titlebar (fails once) | `published_titlebar_failed` | not entered (`_suggestion_sweep_fallback_ready is False`) | retained | scheduled |
| Persistent titlebar (always fails) | `published_post_load_failed` | entered (`… is True`) | retained | scheduled before the raise |

The two outcomes are distinct and each is reachable, and the persistent case
escalates precisely because `_finish_db_rows_application` calls
`_apply_titlebar_counts()` a second time unguarded (`window.py:6015`). That
mechanism is now documented in the test id rather than hidden. **V1 is closed.**

### 5.2 Remaining coverage gaps introduced or left by this pass

**G1 — Low (the only one I recommend acting on now).** `window.py:6565`
(`self._filter_state = …` in `_restore_startup_map_state`) is never exercised as
a *change* by any repository test. All four failure tests leave `_filter_state`
equal to its snapshot at restore time, so deleting the line would keep the suite
green. It is load-bearing in production: the real `_on_filter_changed` assigns
`_filter_state` at `window.py:10576`/`10593` before the visible-store rebuild
that can raise, and `publication_failed` is the outcome where that happens.
Smallest correction: in
`test_publication_failure_rolls_back_and_recovers_overlay`, make the injected
`_on_filter_changed` mutate `_filter_state` before raising, and assert the
snapshot value is restored. My `test_q2_filter_state_restore_is_load_bearing`
is a drop-in template. Confidence: High.

**G2 — Low.** No repository test asserts `_filter_refresh_suppress_depth == 0`
after a restore in which a widget mutation raises. A leaked suppression depth
would silently disable every later filter refresh for the session, so this is
worth one assertion. Covered by my `test_q2_restore_survives_a_raising_set_map_choices`.

**G3 — Low.** No repository test covers superseded-cancel *chaining* — that
transaction B snapshots A's restored state rather than A's dirty state. The
ordering at `window.py:5857-5858` (cancel) preceding `5900` (snapshot) is what
makes this correct, and it is exactly the kind of ordering a future refactor
could invert without any test failing. Covered by my
`test_q2_superseded_chaining_does_not_drift`.

**G4 — informational.** The `threshold` arm at `window.py:7899-7900` is
unreachable (§4.7), so it cannot be covered. Either leave it as defence or
simplify the ternary; no test action is possible.

**Carried forward, still open:** Q5 (`format_version=0` folded into
`_suggestion_cache_migration_pending` at `window.py:9605`), Q6 (bare `os.replace`
without directory fsync at `window.py:9688` and `9764`), V2 (duplicate
`_apply_titlebar_counts` per startup), V3 (stale `_startup_live_rest_total` /
`_completed` after post-load failure recovery), V4 (empty-endpoint guard does not
clear `_deferred_suggestion_restore_pending`), M1 (stale tracker), and risks
R1–R6. None was in scope for this pass and none blocks a default-off commit.

---

## 6. Probe harness corrections (not source defects)

Two of my probes failed on first run. Both were my own expectation errors, and I
record them so the distinction between defect and harness bug is auditable:

1. `test_q3_threshold_is_only_reported_when_the_target_is_reached` asserted a
   single `("applied", 4, …)` event. The real sequence was
   `[('applied', 3, …), 'sort', 'reveal', ('applied', 1, …)]` — the flush
   threshold fires mid-sweep and the reveal flushes the remainder. The **source
   behaviour is correct** (all 4 rows applied); my assertion wrongly assumed one
   batch. Fixed to assert the applied total equals the row count.
2. `test_q3_timeout_reports_timeout_not_threshold` used a "hanging" executor
   whose future still invoked `add_done_callback` immediately, so completions
   advanced and the threshold reveal fired before the timeout. Fixed by adding a
   `PendingFuture` whose `add_done_callback` is a no-op — which then also let me
   assert `_startup_live_rest_inflight == len(keys)` and `completed == 0`, a
   stronger check of the timeout path than the original.

Neither failure indicated a source problem, and both corrected probes now pass.

---

## 7. Regression assessment

No regression found. Specifically re-verified:

- **Q1 still fixed** — re-ran all 5 Q1 probes unchanged: zero-row and all-invalid
  loads route to the synchronous empty state, no A2S, no Companion, no 25 s cache
  timer, warm records preserved, no deferred persistence, and the defensive empty
  terminal reports `"empty"`.
- **Q4 still fixed** — re-ran all 11 Q4 probes unchanged: exactly-once `complete()`
  across all six terminal paths, ownership released on every injected failure,
  rows retained, partial A2S generations invalidated, manual refresh and deferred
  autocomplete recoverable, no exception escapes the idle callback.
- **Happy path** — pending sentinel returned, stores empty before drain,
  `("visible","load")` before `"a2s"`, outcome `published`, 25 s cache timer
  scheduled, and (new) the derived map choices committed with suppression depth 0.
- **GTK-thread ownership** — `tests/test_startup_batching.py:383` still asserts
  `ServerObject` construction occurs only from the idle callback. The new snapshot
  and restore helpers run on the GTK thread only: snapshot from `_apply_db_rows`,
  restore from `_fail_`/`_cancel_startup_row_batches`, all of which are invoked
  from idle callbacks, from `_apply_db_rows`, or from `_shutdown_cleanup`.
- **Synchronous default path** — untouched; `_load_rows_into_store` and its
  ordering are unchanged, and `_set_map_choices` at `window.py:5911` still runs
  before loader selection for both paths.

One behavioural note worth recording for trace readers: the restore means a
failed batched startup now leaves the map dropdown showing the **pre-startup**
model (typically `["All Maps"]`) rather than choices derived from rows that were
never committed. That is the intended fix, but it makes the failure presentation
self-consistent in a way it previously was not — the empty label and the map
dropdown now agree.

---

## 8. Conclusion and recommendation

Q2, Q3 and V1 are **independently verified as fixed**, with no regression to the
previously verified Q1 and Q4 behaviour or to the synchronous default path.

Both fixes are correctly minimal. Q2 is a snapshot/restore pair attached to the
existing transaction state and invoked from the two existing terminal paths — it
does not reorder `_set_map_choices` relative to the loaders, so the synchronous
path is untouched. Q3 is a derived label plus one entry in an accepted-reason set,
with no change to dispatch, cohort selection or reveal ordering. Neither fix
expands the experimental surface.

The fixes also hold up under the specific challenges requested: suppression
depth is balanced even when a widget raises, generation ownership prevents a
stale failure from restoring over a newer transaction, supersession chains
without drift, rollback cannot clobber a legitimate user selection because the
`can_target` startup dimmer blocks input for the whole transaction, `threshold`
is unreachable with `completed < target` at all five reveal sites, and the reveal
remains exactly-once with complete accounting and timeout removal in both warmup
modes.

Recommended next actions, in order:

1. **G1** — extend `test_publication_failure_rolls_back_and_recovers_overlay` so
   the injected `_on_filter_changed` mutates `_filter_state` before raising, and
   assert restoration. This is the one assertion that would currently allow
   `window.py:6565` to be deleted silently.
2. **G2, G3** — add the suppression-depth and supersession-chaining assertions.
   Both are one-test additions; templates exist in my probe file.
3. **Q5, Q6** — the two remaining confirmed Low source findings from the first
   audit (migration/save label conflation; missing directory fsync at
   `window.py:9688` and `9764`). Both are a few lines each.
4. **M1** — update the tracker before staging. Still outstanding, and now the
   only pre-commit blocker.
5. Then take the single narrowly staged **default-off** commit using the first
   audit's exact path list, excluding `.qwen/`.
6. **Production default remains blocked.** Nothing in this pass supplies the
   runtime, frame/input, per-slice timing, memory or repeated full-sweep evidence
   required, and the post-publication failure paths still need a live-GTK
   fault-injection pass. Keep PR16-01 **IN PROGRESS**.

Optional, if trace semantics matter for accepting the 500/60 experiment: a
distinct `no_cohort` reason for the two zero-target reveals (§4.8).
