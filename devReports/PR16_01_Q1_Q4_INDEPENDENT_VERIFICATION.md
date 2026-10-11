# PR16-01 — Independent Verification of Q1/Q4 Remediation

Date: 2026-10-11
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`
Verified against: working tree at `de504a4` plus all dirty/untracked PR16-01 material
Remediation under review: `devReports/PR16_01_Q1_Q4_REMEDIATION.md`
Original findings source: `devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md` (Q1 High, Q4 Low)

Verification mode: **strictly read-only.** No repository source, test, tracker or
`MEMORY.md` file was modified. Nothing was staged or committed. No mutating Git
command was run. DZLL and Steam were not launched. No network operation,
dependency installation, Forge operation or PR16 review-clone access occurred.
All dirty and untracked files were preserved. Temporary probes were written only
under `/tmp/pr16_audit_probe/`. Both experiment switches remain default-off
(`window.py:306-307`, exact `"1"` comparison — verified unchanged).

PR16-01 status: **IN PROGRESS** (unchanged).

---

## 1. Verdict

| Finding | Status | Confidence |
|---|---|---|
| **Q1** — zero-valid-row batched load reported `loaded=True`, lost the empty state, and scheduled a destructive empty rewrite of the warm per-server cache | **FIXED AND VERIFIED** | High — reproduced corrected behaviour offline in 5 probes |
| **Q4** — batched post-publication coordination was unguarded, permanently leaking `_server_db_update_inflight` | **FIXED AND VERIFIED** | High — reproduced corrected behaviour offline in 11 probes |

**No confirmed regression was introduced by this remediation pass.** The happy
path, the pre-existing construction/publication failure paths, generation
cancellation, shutdown rejection and GTK-thread ownership are all unchanged in
behaviour, and the full offline suite passes.

I found **one Low test-quality defect (V1)** and **three Low informational
observations (V2–V4)**. None is a source defect, none blocks the scoped
default-off commit, and V1 is the only one I recommend acting on now.

**Disposition:** Q1 and Q4 are closed for the purposes of the scoped
**default-off** experimental commit. Production-default acceptance remains
**blocked** on the outstanding runtime, memory and display-backed evidence
carried forward from the prior audit, and on the still-open Q2, Q3, Q5, Q6 and
M1 findings, which this pass explicitly did not address.

---

## 2. Method

I re-derived the corrected behaviour from source rather than accepting the
remediation report's description, then rewrote my original adversarial probes so
that **every assertion states the correct post-remediation behaviour** — i.e.
each probe now fails if the fix regresses, rather than failing when the defect
is absent. The original defect-asserting expectations were discarded as success
criteria, as instructed.

Probe files (outside the repository, using the repo's own `tests/conftest.py`
profile boundary):

| File | Probes | Focus |
|---|---|---|
| `/tmp/pr16_audit_probe/test_verify_q1_q4.py` | 16 | Q1 empty/all-invalid/defensive-empty/cache/suggestion; Q4 containment, stale generation, recovery, no-escape, happy path |
| `/tmp/pr16_audit_probe/test_verify_edges.py` | 6 | Coordinator absence, exactly-once `complete()` across all six terminal paths, idle-submission failure, transient vs persistent titlebar, warm-cache survival |

Result: **22 passed**. One initial failure was my own probe's expected-string
typo (`construction_failed` vs the real `row_construction_failed`) and was
corrected before being treated as evidence.

Repository gates re-run:

```text
python -m pytest -q tests
2797 passed, 4 skipped, 53 warnings in 12.72s

python -m pytest -q tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py
29 passed, 1 warning in 0.16s

PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-verify-pyc python -m py_compile \
  src/dzll_launcher/window.py tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py
passed

git --no-pager diff --check
passed, no output
```

**Scope containment verified.** `app.py` (14), `mod_search.py` (23),
`mod_suggestions.py` (19) and the three modified test files
(578 / 59 / 5) have insertion counts identical to the pre-remediation baseline,
so this pass touched only `window.py` (1611 → 1697 insertions, +86) and the two
untracked PR16-01 test files. No unrelated dirty or untracked file was altered.

**Baseline correction (carried forward from the prior audit).** The remediation
report quotes `2775 passed, 26 skipped` (root) and `227 passed, 15 skipped`
(focused). Those are headless figures. On this display-capable machine the same
commands yield **2797 passed, 4 skipped** and **29 passed, 0 skipped**.
`2797 − 2775 = 22` and `26 − 4 = 22` — exactly the display-dependent set, which
executes and passes here. Both runs are green; the earlier numbers should not be
quoted as the current baseline.

---

## 3. Q1 verification

### 3.1 Implementation reviewed

Four independent layers, all confirmed present in source:

1. **Eligibility gate** — `window.py:5942-5944`:
   ```python
   if not fallback_reason and not endpoint_valid_rows:
       fallback_reason = "no_valid_rows"
   ```
   Placed after `_startup_batching_fallback_reason()` and before the batched
   dispatch, so a zero-valid-row load now takes the established synchronous
   loader. This was my recommended Step 1, and it is the correct choice: it
   reuses the proven empty-state implementation instead of adding a second one.

2. **Defensive empty-batch terminal** — `window.py:6284-6306`. Reached only if an
   empty transaction arrives at the batching layer directly. It bumps the
   generation, clears batch state and idle-source ownership, disarms deferred
   suggestions, calls `_finish_db_rows_application(False, True)` inside a
   `try/except/finally`, and reports the truthful `"empty"` outcome
   (`window.py:6303`).

3. **Deferred arming guarded on non-empty publication** — `window.py:6346`:
   `if state["objects"] and not bool(getattr(self, "_suggestion_cache_ready", False))`.

4. **Cache-writer and suggestion-scheduler guards** — `window.py:6610-6611`
   (`if not candidate: return False`) and `window.py:9583-9584`
   (`if not getattr(self, "_obj_by_key", None): return False`).

**Critical ordering detail verified:** the new falsey-candidate guard at
`window.py:6610` sits *before* the unconditional prune
`self._server_mod_cache_records = dict(candidate)` at `window.py:6613`. That
ordering is what actually prevents the wipe. Had the guard been added after the
prune, the in-memory record set would still have been destroyed even though the
disk write was suppressed. This is correct as implemented.

### 3.2 Requirement-by-requirement

| Required behaviour | Verdict | Evidence |
|---|---|---|
| Empty startup produces the proper empty state | **PASS** | `("empty_text", "No Servers Found.")` and `("empty_visible", True)` emitted; `queued == []` proves no batched transaction was scheduled |
| All-invalid startup produces the proper empty state | **PASS** | Same, with `[{"ip": "invalid address", ...}]` — endpoint pre-filter at `window.py:5879-5890` drops it, gate routes to synchronous |
| No A2S startup is triggered | **PASS** | `"a2s" not in events` in both cases |
| No Server Companion startup is triggered | **PASS** | `"companion" not in events` and `_server_companion_rows_loaded is False` |
| No empty per-server cache rewrite is possible | **PASS** | No 25 s timer registered; and a *direct* `_schedule_server_mod_cache_write()` call with `candidate == {}` returns `False` against a `ForbiddenExecutor` that fails the test on any submit |
| Existing warm cache records are preserved | **PASS** | `_server_mod_cache_records == warm` (5 entries) and `_server_mod_cache_persisted` unchanged after both the empty load and the direct writer call |
| No empty global suggestion cache is persisted | **PASS** | `_maybe_schedule_deferred_suggestion_restore()` with `_obj_by_key == {}`, `_pending=True`, `_allowed=True` and a `ForbiddenExecutor` returns `False`, submits nothing, and leaves `_suggestion_cache_ready False`; the defensive empty path also leaves `_deferred_suggestion_restore_pending False` |
| The direct defensive empty-batch path works | **PASS** | `_start_startup_row_batches(host, [], ...)` returns `_STARTUP_DB_APPLY_PENDING`, drains to outcome `["empty"]`, clears `_server_db_update_inflight` and `_startup_row_batch_state`, shows the empty state, starts neither A2S nor Companion, registers no 25 s timer, preserves warm records, and does not arm deferred suggestions |
| Instrumentation is truthful | **PASS** | `startup_row_load_path` reports `path=synchronous`, `reason=no_valid_rows` |

### 3.3 Original Q1 defect chain — closed

The destructive chain I reproduced in the prior audit was:

```
zero valid rows -> batched path -> _finish_db_rows_application(True, True)
  -> 25s _schedule_server_mod_cache_write timer
  -> _server_mod_cache_records = dict({})      # warm set wiped
  -> stage(path, {}) -> publish                # warm cache file overwritten empty
```

Every link is now broken, and broken redundantly: the batched path is not
selected (gate), the terminal reports `loaded=False` if it somehow is
(defensive branch), the timer is not scheduled (`_finish_db_rows_application`
returns at `window.py:5970`), and the writer refuses an empty candidate even if
called directly. I could not construct a remaining route to an empty cache
rewrite.

The secondary R7 chain from the prior audit (empty deferred build persisting an
empty global catalogue) is also closed, by both the non-empty arming guard at
`window.py:6346` and the empty-endpoint guard at `window.py:9583-9584`.

---

## 4. Q4 verification

### 4.1 Implementation reviewed

The new structure of `_publish_startup_row_batches()` (`window.py:6279-6500`):

- **Ownership released before coordination** — `window.py:6354-6357` clears
  `_startup_row_batch_state` and `_startup_row_batch_idle_id` *before* the
  post-load `try` opens. This was the specific ordering the task asked me to
  scrutinise, and it is correct: post-load coordination cannot observe stale
  transaction state, and a later `_apply_db_rows()` will not issue a spurious
  `superseded` cancel.
- **Titlebar individually contained** — `window.py:6364-6369` sets
  `terminal_outcome = "published_titlebar_failed"` and continues rather than
  aborting coordination.
- **Whole post-publication sequence guarded** — `window.py:6361-6487` encloses
  perf reporting, `_finish_db_rows_application(True, True)` (Companion restore,
  A2S submission, offline timer, titlebar) and the summary marker.
- **Recovery handler** — `window.py:6455-6487`: sets
  `published_post_load_failed`, bumps `_startup_live_generation` (6459), removes
  `_startup_warmup_timeout_id` and `_startup_live_rest_flush_id` sources
  (6461-6468), clears queue/buffer/inflight (6470-6472), sets
  `_suggestion_sweep_fallback_ready = True` (6473), completes or releases the
  presentation (6474-6483), and flags deferred recovery (6484-6487).
- **Terminal `finally`** — `window.py:6488-6494`: clears
  `_server_db_update_inflight` (6489) and invokes `complete(terminal_outcome)`
  (6492) with the callback's own exception contained (6493-6494).
- **Deferred recovery after ownership release** — `window.py:6495-6499`, outside
  the `finally`, so `_maybe_schedule_deferred_suggestion_restore()` is no longer
  gated by the flag it needs cleared. This fixes the one-tick delay I recorded
  in §5.5 of the prior audit for the failure path.
- **`_fail_startup_row_batches` gained a terminal `finally`** —
  `window.py:6548-6554` clears ownership and calls `complete(outcome)` even if
  the failure presentation itself raises.

The report's claim that the implementation "does not use an unconditional
success-reporting `finally`" is accurate: `terminal_outcome` is a mutable local
initialised to `"published"` (`window.py:6358`) and only reassigned by the two
failure branches, so the `finally` reports the real outcome rather than a
constant.

### 4.2 Requirement-by-requirement

| Required behaviour | Verdict | Evidence |
|---|---|---|
| Titlebar failure cannot leak DB in-flight ownership | **PASS** | Both transient (`published_titlebar_failed`) and persistent (`published_post_load_failed`) variants end with `_server_db_update_inflight is False` |
| Server Companion failure cannot leak ownership | **PASS** | Outcome `published_post_load_failed`, flag cleared |
| A2S submission failure cannot leak ownership | **PASS** | Outcome `published_post_load_failed`, flag cleared |
| Valid published rows are retained | **PASS** | `store.rows == ["One"]` and `column_view_store.rows == ["One"]` after every injected post-publication failure — no rollback of a good publication |
| Partial A2S generations cannot publish stale results | **PASS** | See §4.3 |
| Startup presentation cannot remain stuck | **PASS** | `coordinator.complete_startup()` invoked on recovery; with `_startup_presentation = None` the fallback `_set_updating(False)` is invoked (observed `updating calls = [(False,)]`) |
| Manual database refresh recovers | **PASS** | After a post-load failure, `_manual_update_server_database()` sets the flag and submits to `_db_executor` (`len(db_executor.calls) == 1`) — previously impossible |
| Deferred suggestions recover | **PASS** | `_pending`, `_allowed` and `_inflight` all True and exactly one `prepare_deferred_suggestions` submitted; completing the future and running the GLib callback yields `_suggestion_cache_ready True`, `_mod_suggestion_index["count"] == 1` and the queued refresh |
| All failure terminal outcomes are truthful | **PASS with nuance V1** | Observed outcomes across six terminal paths: `published`, `row_construction_failed`, `publication_failed`, `idle_submission_failed`, `published_titlebar_failed`, `published_post_load_failed`. Each matches its actual failure point. See V1 for the one branch the repository's own tests do not assert |
| No new exception escape | **PASS** | Five injection points (titlebar, companion, a2s, `_set_updating`, offline-recheck timer) plus an exploding `complete()` callback plus an exploding coordinator — in every case `drain()` returned normally |
| No lifecycle regression | **PASS** | Happy path unchanged: `_STARTUP_DB_APPLY_PENDING` returned, stores empty before drain, `("visible","load")` before `"a2s"`, outcome `published`, 25 s cache timer scheduled. Construction and publication failure paths still roll back to empty stores, start no A2S, schedule no cache timer, and now also release ownership |

### 4.3 Partial A2S generation — detailed result

I injected a `_submit_startup_rest_batches` that performs real partial work
(increments the generation, registers warmup timeout source `41` and flush
source `42`, fills the queue, buffers a result, sets `inflight = 2`, arms a
cohort target) and *then* raises. After recovery:

```
41 in removed and 42 in removed          # both GLib sources removed
_startup_warmup_timeout_id == 0
_startup_live_rest_flush_id == 0
list(_startup_live_rest_queue) == []
_startup_live_rest_buffer == []
_startup_live_rest_inflight == 0
_startup_live_generation == seen_gen[0] + 1
```

A late `_on_startup_rest_future_done(stale_gen, fut, key)` carrying a valid
payload was then rejected: `_startup_warmup_completed` stayed `0` and the buffer
stayed empty. The generation guard at `window.py:7696-7698` does the work; the
recovery handler's generation bump is what makes it effective. **Confirmed: a
partially started A2S generation cannot publish stale results after a
post-publication failure.**

### 4.4 Exception-handling structure, callback ownership, cleanup ordering, re-entrancy

These were called out for particular attention. Assessment:

**Callback ownership — correct.** `complete` is captured once
(`window.py:6354`) and invoked exactly once on every terminal path. I verified
this empirically across six paths (`published`, `row_construction_failed`,
`publication_failed`, `idle_submission_failed`, `published_titlebar_failed`,
`published_post_load_failed`) with `len(outcomes) == 1` in each case. The
publication-failure branch returns at `window.py:6352` *before* the post-load
`try/finally` opens, so it cannot double-fire alongside `_fail_startup_row_batches`.
A raising `complete()` is caught and logged (`window.py:6493-6494`, `6553-6554`,
`6304-6305`) after ownership has already been released, so a bad callback cannot
re-leak the flag.

**Cleanup ordering — correct, and deliberately improved.** Transaction state and
idle-source ownership are released *before* post-load coordination
(`window.py:6356-6357`), and `_server_db_update_inflight` is released *before*
`complete()` and *before* deferred-suggestion recovery (`window.py:6489` then
`6492` then `6495`). Both orderings matter: the first prevents a spurious
`superseded` cancel, the second is what makes the deferred restore schedulable
during failure recovery instead of waiting a browser-live tick.

**Re-entrancy — safe.** The one genuine re-entrancy exposure is that the
recovery handler calls `coordinator.complete_startup()` (`window.py:6477`) when
a reveal may already have completed it. `StartupPresentationCoordinator.complete_startup()`
is idempotent — `startup_presentation.py:42-45` short-circuits on
`self.startup_completed` and only retries the notice — so a second call cannot
re-hide, re-show the update dialog, or double-count (`_update_attempted` guard at
`startup_presentation.py:50-56`). Verified by source and exercised by probe.

A related re-entrancy path exists and is handled: `_apply_titlebar_counts()` is
called unguarded inside `_complete_startup_presentation()` (`window.py:5489`),
which can itself run inside `_finish_db_rows_application()` via an immediate
reveal. A titlebar failure there propagates to the outer handler, which then
calls `complete_startup()` a second time (idempotent) and skips the deferred
arming inside `_complete_startup_presentation()` — but compensates for exactly
that at `window.py:6484-6487`. The net effect is correct; see V1/V2 for the
labelling consequence.

**No unconditional-success `finally`** — confirmed. `_server_db_update_inflight`
is cleared twice on some paths (directly at `window.py:6489` and again inside
`complete()`), which is idempotent and harmless.

**Unreachable-early-return check.** `_fail_startup_row_batches()` still returns
before its `finally` when the generation is stale (`window.py:6530-6532`), which
would skip the ownership clear. I checked reachability: every caller
(`window.py:6127`, `6130`, `6271`, `6350`) has already verified the generation
on the same GTK thread with no intervening yield, so no interleaving can clear
the state first. Not reachable; recorded for completeness.

---

## 5. New findings from this verification pass

### 5.1 Confirmed defects

**None in source.** One test-quality defect:

#### V1 — Low: the repository's `titlebar` parametrization never exercises the `published_titlebar_failed` branch

`tests/test_startup_batching.py:491-492` parametrizes
`("titlebar", "companion", "a2s")` and asserts
`outcomes == ["published_post_load_failed"]` for **all three**. For the titlebar
case this passes only incidentally: `_finish_db_rows_application()` calls
`_apply_titlebar_counts()` a **second time**, unguarded, at `window.py:6015`, so
a persistent titlebar failure escapes there and is reclassified by the outer
handler at `window.py:6457`. The dedicated containment branch at
`window.py:6364-6369` is therefore asserted by **no test in the repository**.

Evidence (both probes pass):

- `test_q4_transient_titlebar_failure_reports_titlebar_outcome_and_starts_a2s`
  — a titlebar that fails once then succeeds yields exactly
  `["published_titlebar_failed"]`, with `"a2s"` and `"companion"` in events, the
  25 s cache timer scheduled, rows retained, and
  `_suggestion_sweep_fallback_ready` left **False** (no failure recovery run).
  This proves the containment branch works and is distinguishable.
- `test_q4_persistent_titlebar_failure_degrades_to_post_load_outcome` — a
  persistent titlebar failure yields `["published_post_load_failed"]` with
  `"a2s"` already in events, reproducing the path the repository test actually
  takes.

This is **not a source defect**. The behaviour matches the remediation report's
own table ("`published_titlebar_failed`, unless a later coordination step also
fails"). It is a coverage gap plus a mildly misleading parametrization: the test
name and its `"titlebar"` id suggest titlebar containment is verified, but its
assertions are identical to the other two cases and would still pass if the
`window.py:6364-6369` guard were deleted.

Smallest correction: add one transient-titlebar case asserting
`published_titlebar_failed` (and that no failure recovery ran), and rename or
annotate the persistent case so it is clear it exercises the post-load branch.
Confidence: High.

### 5.2 Informational observations (not defects)

**V2 — Low: `_apply_titlebar_counts()` runs twice on every successful startup.**
Once at `window.py:6366` (batched) / inside `_load_rows_into_store()`
(synchronous) and again at `window.py:6015` inside `_finish_db_rows_application()`.
The double call is pre-existing in the synchronous path and inherited by the
batched path; it was not introduced by this remediation. Cost is one extra
titlebar recompute per startup. It is recorded here only because the new
titlebar guard makes the redundancy semantically visible — it is the mechanism
behind V1.

**V3 — Low: recovery leaves `_startup_live_rest_total` / `_startup_live_rest_completed` stale.**
`window.py:6470-6472` clears the queue, buffer and in-flight count but not the
two counters. Both are consumed only by `DEBUG_STARTUP_LIVE` progress output and
the `a2s_sweep_completion` perf marker, and `_maybe_finish_startup_rest_sweep()`
is generation-gated so it cannot run for the invalidated generation. No
functional impact; the only consequence is that no `a2s_sweep_completion` line
is emitted for a failed generation, which is arguably correct.

**V4 — Low: the empty-endpoint guard does not clear `_deferred_suggestion_restore_pending`.**
`window.py:9583-9584` returns `False` without clearing `_pending`, so with an
empty `_obj_by_key` and a not-ready catalogue the flag stays set,
`_refresh_mod_suggestions()` keeps hiding the panel with `catalogue_not_ready`,
and `_resolve_exact_mod_chip()` keeps returning `None`. With zero servers there
is nothing to autocomplete, and the Q1 gate means the batched path never arms
`_pending` for an empty publication (`window.py:6346`), so this is unreachable
defence-in-depth. Recorded, not a defect.

### 5.3 Hypothetical risks (unverified, unchanged from the prior audit)

R1–R6 from `PR16_01_QWEN_INDEPENDENT_AUDIT.md` are untouched by this pass and
remain open: slice-budget overrun at row boundaries, the ~273–284 ms
uninterrupted final publication, GIL contention from the deferred worker,
unmeasured peak RSS/USS during suggestion-cache decode, orphaned `.pending`
files on early exit, and synchronous/batched conversion duplication. None is a
confirmed defect.

Two new hypothetical risks specific to this remediation, both accepted by design
and documented in the remediation report:

- A post-publication exception deliberately **retains** already-valid rows rather
  than rolling back. Already-executing A2S worker calls cannot be cancelled; only
  their late results are rejected by generation. Verified rejected (§4.3).
- The three post-publication failure points have been verified only against
  focused host doubles and deterministic executor/GLib queues, not a live GTK
  session. I agree with the report that a maintainer runtime fault-injection pass
  is still required for production-default acceptance.

### 5.4 Previously reported findings not addressed by this pass

Q2 (map-choice rollback, = Sol L1), Q3 (A2S exhaustion mislabelled `threshold`,
= Sol L2), Q5 (`format_version=0` migration/save conflation), Q6 (missing
directory fsync at `window.py:9688` and `9764`) and M1 (stale tracker) remain
open. I confirmed by inspection that none of them was touched, which matches the
remediation report's declared scope. Q2 and Q3 are the two I recommend taking
next, per Steps 4-5 of the prior audit's remediation plan.

---

## 6. Regression assessment

No regression found. Specifically verified unchanged:

- **Happy path** — pending sentinel, empty stores before drain, visible-before-A2S
  ordering, `published` outcome, 25 s cache timer.
- **Construction failure** — `row_construction_failed`, empty stores, no A2S, no
  cache timer, ownership now also released (an improvement).
- **Publication failure** — `publication_failed`, full rollback of source,
  visible, `_obj_by_key` and `live`, no A2S, no cache timer, ownership released.
- **Idle-submission failure** — returns `False` rather than the pending sentinel,
  outcome `idle_submission_failed`, ownership released, nothing published.
- **Cancellation and shutdown** — late idle callbacks still rejected; the
  `superseded` and `shutdown` paths still deliberately do not invoke `complete()`.
- **GTK-thread ownership** — `tests/test_startup_batching.py:293-312` still
  asserts construction occurs only from the idle callback on the caller thread.
- **Synchronous default path** — untouched; all ten original fallback reasons
  intact, plus the new `no_valid_rows`.
- **Full offline suite** — 2797 passed, 4 skipped (display-capable), including
  the 22 display-backed tests the headless baseline skips.

The new exception-handling structure adds one behavioural change worth naming
explicitly: on a post-publication failure the browser now keeps its valid rows
and dismisses the overlay instead of leaving the window permanently unable to
refresh. That is the intended fix, not a regression, but it does mean a
partially coordinated startup (rows visible, A2S generation invalidated, no
sweep-completion marker) is now a reachable state. `_suggestion_sweep_fallback_ready = True`
(`window.py:6473`) is what keeps suggestion refresh and migration schedulable in
that state, which is the correct compensating choice.

---

## 7. Conclusion and recommendation

Q1 and Q4 are **independently verified as fixed**. Both fixes are layered rather
than single-point: Q1 is closed by an eligibility gate, a defensive terminal, a
non-empty arming guard, a cache-writer guard and a scheduler guard; Q4 is closed
by ownership release ordering, per-step containment, a generation-invalidating
recovery handler and a terminal `finally`. I could not construct a remaining
route to either original failure, and I found no regression introduced by the
pass.

Recommended next actions, in order:

1. **V1** — add a transient-titlebar case asserting `published_titlebar_failed`,
   and clarify the persistent-titlebar case's id. Small, test-only, and it closes
   the last unasserted branch in the new exception structure.
2. **M1** — update the tracker before staging (still outstanding).
3. **Q2, Q3** — the two remaining confirmed source defects, both Low, both with
   small fixes already specified in the prior audit (Steps 4-5).
4. Then take the single narrowly staged **default-off** commit using the prior
   audit's exact path list, excluding `.qwen/` (a Qwen Code CLI session
   artefact, not PR16-01 material).
5. **Production default remains blocked.** Nothing in this pass supplies the
   runtime, frame/input, memory or repeated full-sweep evidence required, and the
   three post-publication failure points still need a live-GTK fault-injection
   pass. Keep PR16-01 **IN PROGRESS**.

Q5, Q6 and V2–V4 may be deferred or recorded as accepted; none blocks the
default-off commit.
