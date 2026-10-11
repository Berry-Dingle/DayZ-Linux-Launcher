# PR16-01 — Independent Adversarial Integration Audit (Qwen)

Date: 2026-10-11
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`
Baseline inspected: working tree at `de504a4` (`main`, tag `v0.4.1-beta`) plus all
dirty and untracked PR16-01 material.
Audit mode: **strictly read-only**. No source, test, tracker or `MEMORY.md` file
was modified. Nothing was staged or committed. No mutating Git command was run.
DZLL and Steam were not launched. No network operation, dependency installation,
Forge operation or PR16 review-clone access was performed. All dirty and
untracked files were preserved.

PR16-01 status: **IN PROGRESS** (unchanged by this audit).

---

## 1. Executive risk assessment

The PR16-01 accumulation is broadly sound on the axes that matter most: GObject
and GTK model work stay on the GTK main thread, the batched transaction keeps
partial rows private, generation and shutdown guards are consistently applied,
and the two experiments are genuinely default-off behind exact-value environment
switches. I independently reproduced Sol's two known findings (L1, L2) and
confirmed both.

**However, this audit does not agree with the prior conclusion that no defect
above Low severity exists in the PR16-01 source.** I found one additional
defect that I rate **High**, which the final integration audit did not
identify:

> A successful database load that yields **zero endpoint-valid rows** takes the
> batched path, is reported to the post-load coordinator as
> `loaded=True`, silently skips the "No Servers Found." empty state, marks
> Server Companion rows as loaded, starts A2S, and schedules the deferred
> per-server cache writer — which then **overwrites the warm on-disk per-server
> mod cache with an empty record set.**

That last consequence is persistent-state destruction that survives the
session and invalidates subsequent benchmark comparisons. It is reproduced
offline in this audit (Probe F, §4). The synchronous path does not have this
behaviour (Probe F2).

I also confirmed a **structural error-containment asymmetry** (Q4): the batched
path's post-publication coordination runs outside any exception guard, so an
exception there permanently leaks `_server_db_update_inflight = True`, which
blocks all later manual database refreshes *and* permanently blocks deferred
autocomplete restoration for the session. The synchronous path clears that flag
in a `finally`.

Both new findings are gated behind `DZLL_STARTUP_BATCHING=1`, so shipping
default-off does not expose end users. They do expose the maintainer's own
experiment runs and any early adopter of the switch. Both fixes are small
(§9), and I recommend landing Q1 before the scoped commit rather than after,
because a single anomalous run can destroy the warm cache the benchmark depends
on.

**Disposition:**

| Gate | Verdict |
|---|---|
| Scoped **default-off** experimental commit | **Not yet.** Fix Q1 first (small, low-risk). Q2/Q3/Q4 may be committed as recorded known limitations or fixed in the same pass. Tracker must be updated first (Sol M1, re-confirmed). |
| **Production-default** acceptance of either switch | **No.** Unchanged from Sol's conclusion, and now additionally blocked on Q1/Q4 resolution plus the outstanding runtime, memory and display-backed evidence. |

One correction to the prior audit's verification baseline: with a display
available, the maintained suite here reports **2790 passed, 4 skipped** — the 22
display-backed tests that were skipped in the earlier headless run executed and
passed. The previously quoted `2768 passed, 26 skipped` should not be carried
forward as the current baseline (§8).

---

## 2. Evidence classification

Per the requested four-way split.

### 2.1 Confirmed source defects (reproduced offline in this audit)

| ID | Severity | Summary | Probe |
|---|---|---|---|
| **Q1** | **High** | Zero-usable-row batched load reports `loaded=True`; loses empty state; schedules destructive empty rewrite of the warm per-server mod cache | A, A2, F, F2 |
| **Q2** | Low | Map-choice model is committed before the batched transaction and never rolled back (**= Sol L1, confirmed**) | B, B2 |
| **Q3** | Low | A2S sweep exhaustion reveals with `reason="threshold"` while `completed < target` (**= Sol L2, confirmed**) | C, C2 |
| **Q4** | Low | Batched post-publication coordination is unguarded; an exception permanently leaks `_server_db_update_inflight` | D, E |
| **Q5** | Low | `format_version=0` (fresh worker rebuild) is folded into `_suggestion_cache_migration_pending`, conflating first-save with v1→v2 migration | source-read |
| **Q6** | Low | Two suggestion-cache publications use bare `os.replace()` with no directory fsync, unlike every other cache publisher | source-read |

Q5 and Q6 are confirmed by source reading only; neither needed a probe and
neither is a behavioural defect today.

### 2.2 Reproducible test failures

**None in the maintained suite.** The full offline suite and the focused PR16-01
subset both pass (§8). Every defect in §2.1 is a *coverage gap*, not a failing
test: the suite is green because no test exercises these paths. That distinction
matters for the acceptance judgement in §8.

The only failing tests produced during this audit were my own adversarial probes
in `/tmp/pr16_audit_probe/`, which are deliberately written to fail when the
defect is absent (Probe B/B2 initially failed on a bad fixture — a single
synthetic row did not reach `MIN_MAP_COUNT = 5` in `maps.py:3` — and were
corrected before being treated as evidence).

### 2.3 Plausible but unverified risks

| ID | Risk | Why unverified |
|---|---|---|
| R1 | A single row can exceed the 6 ms slice budget; the deadline is checked only at row boundaries (`window.py:6258-6266`). Prior measurements report 20.5–29.3 ms max slices. | No per-slice max/p95 for the *current* combined code was supplied. |
| R2 | Final publication remains one uninterrupted GTK section (~273–284 ms supplied). | Visible on a 60 Hz display; no external frame evidence. |
| R3 | Deferred JSON restoration on `_update_executor` contends for the GIL and may cause a post-reveal hitch. | Successful autocomplete proves recovery, not absence of a hitch. |
| R4 | Peak transient memory in `load_suggestion_cache`: bounded raw buffer (≤64 MiB, `mod_suggestion_cache.py:26`) plus the parsed document plus the restored index coexist. | No time-aligned RSS/USS measurement supplied. |
| R5 | A staged `.pending` file can be orphaned if the process exits before the GLib result callback runs (`window.py:9743-9780`, `6549-6586`). | Not data corruption (never read as canonical); cleanup unmeasured. |
| R6 | Synchronous and batched row conversion are duplicated (`window.py:6135-6249` vs `6651-6800`). Differential parity tests exist, but future ingestion changes must edit both. | Drift risk, not a present defect. |
| R7 | Q1's secondary chain: zero rows → deferred restore with empty payloads → `cache_miss_rebuilt`, `format_version=0` → `migration_pending=True` (`window.py:9605`) → post-sweep migration persists an **empty** global autocomplete catalogue. | Source-reasoned; requires the suggestion cache to be missing/corrupt *and* zero rows. Not independently probed. Mitigated by the same Q1 fix. |
| R8 | Q4's trigger requires `_apply_titlebar_counts()`, `_restore_server_companion_if_enabled()`, `_set_updating()` or `_submit_startup_rest_batches()` to raise. | Structural asymmetry is source-confirmed and probed; the production trigger is not demonstrated. |

### 2.4 Accepted design trade-offs (not defects)

- **Duplicated row conversion** between the synchronous and batched loaders, so
  the proven default hot path is untouched. Correct choice for an experiment;
  must be consolidated or deleted before the experiment becomes permanent.
- **Retained in-memory cache records** (`_server_mod_cache_records`) for reuse
  across loads. Deliberate; bounded by `MAX_ENTRIES = 50_000`
  (`server_mod_cache.py:21`).
- **Pruning the record set to the current load's candidates** at +25 s
  (`window.py:6528`). Correct for a stable DB; harmful only in the Q1
  degenerate case.
- **Readiness is a completed-work gate, not an online-server quota.** Failed and
  timed-out attempts count (`window.py:7695-7701`). Documented and intentional.
- **Stable live list.** Late A2S results update row properties without
  continuous reorder/splice; explicit user sort/filter applies later values.
  Established behaviour, preserved.
- **Coarse-band cohort.** The experimental 500 are the first 500 entries of the
  existing `bm_rank` band prefix, not an exact top-500 ranking
  (`window.py:7481-7495`). Correctly disclaimed in the experiment report.
- **20 s warmup timeout as the recovery path** for non-attempt drains. Correct
  in principle; Q3 is only about the *label*, not the reveal itself.

---

## 3. Findings ranked by severity

### Critical

None.

### High

#### Q1 — Zero-usable-row batched load reports success, loses the empty state, and schedules a destructive empty cache rewrite

**Classification:** confirmed source defect, reproduced offline. **New** — not
present in `PR16_01_FINAL_INTEGRATION_AUDIT.md`.

**Source.** `_publish_startup_row_batches()` ends with a hardcoded success
argument:

```
window.py:6394    complete = state.get("complete")
window.py:6395    self._startup_row_batch_state = None
window.py:6396    self._startup_row_batch_idle_id = 0
window.py:6397    post_load_start_ns = time.monotonic_ns()
window.py:6398    self._finish_db_rows_application(True, True)     # <-- loaded=True, always
```

The synchronous loader computes the same argument honestly:

```
window.py:6880    return self.store.get_n_items() > 0
window.py:5957    loaded = self._load_rows_into_store(endpoint_valid_rows)
window.py:5958    return DZLLWindow._finish_db_rows_application(self, loaded, fetched_ok)
```

and `_finish_db_rows_application()` branches on it:

```
window.py:5960    def _finish_db_rows_application(self, loaded: bool, fetched_ok: bool):
window.py:5961        if not loaded:
window.py:5962            self._suggestion_sweep_fallback_ready = True
window.py:5963            self._server_companion_rows_loaded = False
window.py:5964            msg = "No Servers Found."
...
window.py:5967            self.empty_label.set_text(msg)
window.py:5968            self.empty_label.set_visible(True)
window.py:5969            self._complete_startup_presentation()
window.py:5970            return False
```

The empty-row case is never routed away from batching —
`_startup_batching_fallback_reason()` (`window.py:6024-6047`) checks the switch,
caller, `fetched_ok`, provisional/replacement, legacy model, shutdown, column
view, `store`/`column_view_store` item counts and `_obj_by_key`, but **has no
emptiness condition**. With zero rows the batched transaction still runs: the
`while` loop in `_run_startup_row_batch()` (`window.py:6259-6266`) does not
execute, `state["index"] == len(state["rows"]) == 0`, and publication proceeds
normally.

**Trigger conditions (reachable).** `DZLL_STARTUP_BATCHING=1` plus a load where
`fetched_ok=True` and `endpoint_valid_rows` is empty. Two concrete routes:

1. `read_servers_from_db()` swallows *any* exception and returns `[]`
   (`db.py:153-156`), and returns `[]` when the file is absent (`db.py:124-125`).
   `fetch_db_overwrite_local()` validates non-emptiness *before* `os.replace()`
   (`db.py:111-116`), but `read_servers_from_db()` re-opens the database in a
   separate connection afterwards, so a transient read error, lock or schema
   mismatch between the two yields `rows=[]` with `ok=True`.
2. Every row fails `normalize_server_endpoint()` in the pre-filter at
   `window.py:5879-5890`.

**Expected vs actual.**

| Behaviour | Synchronous (expected) | Batched (actual) |
|---|---|---|
| `empty_label` | `"No Servers Found."`, visible | untouched, stays hidden |
| `_server_companion_rows_loaded` | `False` | **`True`** |
| `_restore_server_companion_if_enabled()` | not called | **called** |
| `_set_updating(True, "Updating The Server Database…")` | not called | **called** |
| A2S submission | not started | **started** (empty cohort) |
| 25 s `_schedule_server_mod_cache_write` timer | **not scheduled** | **scheduled** |
| `_server_mod_cache_records` after the tick | warm set retained | **wiped to `{}`** |
| On-disk per-server cache | untouched | **rewritten with 0 entries** |

**Evidence (Probe F / F2, §4).** With a 5-entry warm record set:

```
[PROBE F]  events               = [('maps', ['All Maps']), 'snapshot', ('visible', 'load'),
                                    'title', 'companion', 'updating', 'a2s', 'title']
[PROBE F]  25s timers scheduled  = [(25, '_schedule_server_mod_cache_write'), (300, '<lambda>')]
[PROBE F]  records after publish = 5
[PROBE F]  candidate after pub   = 0
[PROBE F]  persisted (disk map)  = 5
[PROBE F]  records after 25s tick = 0
[PROBE F]  worker calls           = [('stage', 0)]
[PROBE F]  staged payload entries = [0]

[PROBE F2] events              = [('maps', ['All Maps']), 'suggestions',
                                   ('empty_text', 'No Servers Found.'),
                                   ('empty_visible', True), 'overlay_complete']
[PROBE F2] 25s timers          = []
[PROBE F2] records retained    = 5
```

The wipe mechanism is `_schedule_server_mod_cache_write()`:

```
window.py:6525    candidate = getattr(self, "_server_mod_cache_candidate", None)
window.py:6527    fingerprints = {key: record[0] for key, record in candidate.items()}
window.py:6528    self._server_mod_cache_records = dict(candidate)     # <- unconditional prune
window.py:6529    if (fingerprints == getattr(self, "_server_mod_cache_persisted", {}) and
window.py:6530            not bool(getattr(self, "_server_mod_cache_needs_repair", False))):
window.py:6531        return False
...
window.py:6538    future = self._update_executor.submit(server_mod_cache.stage, path, plain_records, ...)
```

With `candidate == {}` and `persisted` holding 5 fingerprints, the guard at
6529-6531 does **not** short-circuit, so `server_mod_cache.stage()` is submitted
with an empty record set and `server_mod_cache.publish()` atomically replaces the
warm cache with an empty one.

**Impact.** The per-server derived cache is the artefact whose warm/cold delta
the PR16-01 benchmark is built on (the tracker records ~575–583 ms warm cache
loading versus ~3,131–3,198 ms GTK DB callback cold). Destroying it costs
seconds of GTK time on the next start and silently invalidates any A/B run
performed after the anomaly. Combined with the missing empty-state label, the
user sees a blank browser with no explanation, and Server Companion is told rows
are loaded when none are.

**Test coverage:** **none.** `tests/test_startup_batching.py` has no zero-row or
all-rows-invalid case; its ten tests all use one or more valid rows. The empty
state is asserted only for the synchronous path elsewhere in the suite.

**Smallest safe correction.** Add an emptiness condition to the eligibility gate
so the degenerate case takes the proven synchronous loader — one added branch,
no change to publication semantics:

```python
# window.py, in _startup_batching_fallback_reason(), before the final "return ''"
if not endpoint_valid_rows:
    return "no_valid_rows"
```

This requires passing the row count into `_startup_batching_fallback_reason()`
(currently `(self, fetched_ok, allow_startup_batching)`) or performing the check
at the call site (`window.py:5939-5950`). The call-site form is the smaller
diff:

```python
fallback_reason = DZLLWindow._startup_batching_fallback_reason(
    self, fetched_ok, allow_startup_batching,
)
if not fallback_reason and not endpoint_valid_rows:
    fallback_reason = "no_valid_rows"
```

Defence-in-depth alternative (also recommended, independently): change
`window.py:6398` to `self._finish_db_rows_application(bool(state["objects"]), True)`
and guard the deferred-restore arming at `window.py:6321-6323` on
`state["objects"]` being non-empty. Either fix alone closes the cache wipe; both
together also close R7.

**Regression tests required.**
1. Batched load with `rows=[]` and `fetched_ok=True`: assert
   `("empty_text", "No Servers Found.")` and `("empty_visible", True)` are
   emitted, `_server_companion_rows_loaded is False`, `"a2s" not in events`,
   `"companion" not in events`, and **no 25 s timer is registered**.
2. Batched load where every row fails endpoint validation: same assertions.
3. Warm-cache variant: pre-populate `_server_mod_cache_records`/`_persisted`,
   run the zero-row batched load, invoke the captured 25 s callback if any, and
   assert `_server_mod_cache_records` is unchanged and `server_mod_cache.stage`
   was never submitted.
4. Differential: for both zero-row and all-invalid inputs, assert the batched
   and synchronous paths produce identical `empty_label`,
   `_server_companion_rows_loaded`, A2S-start and cache-timer outcomes.
5. Instrumentation: assert `startup_row_load_path` reports
   `path=synchronous reason=no_valid_rows` if the gate fix is taken.

**Confidence: High.** Mechanism read directly from source and reproduced offline
in both directions (batched fails, synchronous correct).

---

### Medium

#### M1 — Tracker evidence is materially stale (re-confirms Sol M1)

`devReports/PR16_IMPROVEMENTS_TRACKER.md` still carries
`Implementation: … RUNTIME VERIFICATION PENDING`, leaves most PR16-01 next-step
boxes unchecked, and its progress log stops at 2026-10-11 before GTK batching,
deferred autocomplete restoration, the A2S 500/60 experiment and the supplied
runtime results. The overview status **IN PROGRESS** is correct.

The tracker must be updated before staging. In addition to Sol's prescribed
updates, it should record Q1–Q6 from this audit and the corrected test baseline
(§8). I did not modify it.

---

### Low

#### Q2 — Map choices are committed before the batched transaction and never rolled back (**= Sol L1, independently confirmed**)

**Source.** `_apply_db_rows()` builds and installs the map model *before* it
decides which loader to use:

```
window.py:5905        choices = map_choices_from_db_rows(endpoint_valid_rows)
window.py:5910        self._set_map_choices(choices)
window.py:5919        self._set_map_choices(["All Maps"])      # exception fallback
...
window.py:5939    fallback_reason = DZLLWindow._startup_batching_fallback_reason(...)
window.py:5948        return DZLLWindow._start_startup_row_batches(...)
window.py:5957    loaded = self._load_rows_into_store(endpoint_valid_rows)
```

Neither `_rollback_startup_row_publication()` (`window.py:6426-6451`) nor
`_fail_startup_row_batches()` (`window.py:6453-6475`) nor
`_cancel_startup_row_batches()` (`window.py:6477-6496`) touches `map_model` or
`map_dropdown`. `_set_map_choices()` splices the dropdown model
(`window.py:2382-2409`), so the change is immediately user-visible state.

**Trigger.** Any batched failure or cancellation: `row_construction_failed`,
`publication_failed`, `idle_submission_failed`, or a `superseded` cancel.

**Evidence (Probe B/B2).** Six rows sharing `ChernarusPlus` (≥ `MIN_MAP_COUNT`):

```
[PROBE B]  map model after failed transaction = ['All Maps', 'Chernarus']
[PROBE B]  source rows = []
[PROBE B2] map model after cancellation = ['All Maps', 'Chernarus']
```

The browser has zero published rows while the map dropdown offers a map filter
derived from rows that were never committed. The failure path *does* show
`"No Servers Found (Startup List Build Failed)."` (`window.py:6464-6465`), so the
UI simultaneously says "no servers" and offers map choices.

**Scope note.** I verified the `superseded` variant is effectively unreachable
in production: all three `_apply_db_rows()` call sites (`window.py:5532`,
`5554`, `5719`) are gated — `apply_db_rows_once()` by `_startup_db_applied`, and
`_manual_update_server_database()` by `_server_db_update_inflight`
(`window.py:5685-5687`), which the batched path deliberately holds set until
`complete("published")`. The reachable variants are therefore the three failure
outcomes, not supersession. This narrows Sol's L1 slightly without changing its
severity.

**Expected vs actual.** Expected: map model restored (or deferred) so all
browser-visible derived state commits or rolls back together. Actual: map model
survives.

**Test coverage: none.** `tests/test_startup_batching.py` stubs `_set_map_choices`
as `lambda _choices: events.append("maps")` and never asserts on the choices or
on post-failure map state.

**Smallest safe correction.** Snapshot and restore in the two terminal paths.
Capture the current `map_model` strings and selected index in
`_start_startup_row_batches()` state; on `_fail_startup_row_batches()` and
`_cancel_startup_row_batches()`, re-splice the snapshot. Moving
`_set_map_choices()` after successful publication is architecturally cleaner but
changes ordering in the synchronous path too, so it is the larger diff and I do
not recommend it for this pass.

**Regression tests required.** Assert the map model equals its pre-transaction
snapshot after (a) row-construction failure, (b) publication failure, (c) idle
submission failure, (d) superseding cancellation; and assert it equals the new
choices after successful publication.

**Confidence: High.** Reproduced.

---

#### Q3 — A2S sweep exhaustion reveals as `threshold` below the cohort target (**= Sol L2, independently confirmed**)

**Source.** `_maybe_finish_startup_rest_sweep()` labels exhaustion as threshold
unconditionally:

```
window.py:7773        if not self._startup_warmup_revealed:
window.py:7774            self._complete_startup_warmup(generation, reveal_reason="threshold")
```

and `_complete_startup_warmup()` only derives a truthful reason when the caller
passes `None`:

```
window.py:7585        if reveal_reason not in {"threshold", "timeout"}:
window.py:7586            reveal_reason = (
window.py:7587                "threshold"
window.py:7588                if self._startup_warmup_completed >= self._startup_warmup_target
window.py:7589                else "timeout"
window.py:7590            )
```

Because `"threshold"` is in the accepted set, the derivation at 7586-7590 is
bypassed.

Three drain paths increment `_startup_live_rest_completed` **without**
incrementing `_startup_warmup_completed` (only `window.py:7700-7701` does that):

```
window.py:7666-7669   obj missing from _obj_by_key
window.py:7670-7675   ip/qport coercion failure
window.py:7688-7691   executor submit failure (inflight decremented)
```

If those paths drain the queue with no futures and no buffer, the exhaustion
branch fires with `completed = 0`.

**Evidence (Probe C/C2).** Ten-key cohort, target 6:

```
[PROBE C]  threshold log = [{'a2s_generation': 1, 'completed': 0, 'target': 6,
                              'warmup_mode': 'experimental_500_60',
                              'cohort_count': 10, 'reason': 'threshold'}]
[PROBE C]  reveal log    = [{...'completed': 0, 'target': 6,
                              'reveal_reason': 'threshold', ...}]

[PROBE C2] completed/target = 0 / 6      (executor.submit raising)
[PROBE C2] reveal = [{...'reveal_reason': 'threshold', 'completed': 0, ...}]
```

**Expected vs actual.** Expected: an explicit `sweep_exhausted` (or
`submission_failure`) reason, or the derived `timeout`. Actual: `threshold`,
which asserts a cohort target that was not met. Both the `a2s_warmup_threshold`
and `a2s_warmup_reveal` acceptance markers carry the false label, so a trace
reviewer validating the 500/60 experiment against
`reveal_reason == "threshold"` would accept a run in which **no A2S query was
ever submitted**.

**Blast radius.** Instrumentation and acceptance semantics only — the reveal
itself is the correct recovery (the browser must not stay behind the overlay
forever). The `completed` field in `a2s_sweep_completion` is likewise
*accounting*, not success, exactly as Sol noted; `completed=12101 total=12101`
proves the queue drained, not that 12,101 queries succeeded.

**Attribution.** The reveal-on-exhaustion behaviour predates the 500/60
experiment. The explicit `reveal_reason` label and the acceptance markers built
on it are new in the PR16-01 diff, which is what makes the mismatch part of this
change set.

**Test coverage: none.** `tests/test_a2s_warmup_500.py` covers threshold,
timeout, stale generation, shutdown, empty cohort, duplicate dispatch,
offline/exception counting and user-sort preservation, but has no
missing-object or executor-submission-failure exhaustion case.

**Smallest safe correction.** Pass `None` (or a new explicit reason) at
`window.py:7774`:

```python
self._complete_startup_warmup(generation, reveal_reason="sweep_exhausted")
```

and extend the accepted set at `window.py:7585` to
`{"threshold", "timeout", "sweep_exhausted"}` so the new label is not silently
rewritten. Passing `None` instead is a one-token change and already yields the
truthful `timeout` label via 7586-7590, at the cost of not distinguishing
exhaustion from a genuine 20 s timeout.

**Regression tests required.** (a) All cohort endpoints missing from
`_obj_by_key`; (b) executor `submit` raising for every key; (c) `ip`/`qport`
coercion failing. For each: assert a reveal occurs exactly once, the queue is
fully accounted, and `reveal_reason != "threshold"` unless
`completed >= target`. Additionally assert the same for the default (switch-off)
mode, since the exhaustion branch is mode-independent.

**Confidence: High.** Reproduced in two independent variants.

---

#### Q4 — Batched post-publication coordination is unguarded; an exception permanently leaks `_server_db_update_inflight`

**Classification:** confirmed structural defect; production trigger not
demonstrated.

**Source.** The publication `try/except` closes at `window.py:6324-6328`.
Everything after it runs unguarded while the transaction state is still live:

```
window.py:6330        self._apply_titlebar_counts()          # outside the try
window.py:6331-6335   discord set_menu()                     # individually guarded
...
window.py:6395        self._startup_row_batch_state = None
window.py:6398        self._finish_db_rows_application(True, True)   # outside any try
...
window.py:6422        if callable(complete):
window.py:6423            complete("published")              # clears _server_db_update_inflight
```

`complete` is the only thing that clears the flag on the batched path
(`window.py:5545-5551`). The synchronous path clears it in a `finally`
regardless of exceptions:

```
window.py:5619        result = None
window.py:5620        try:
window.py:5621            result = apply_db_rows_once(rows, ok, allow_batch=True)
window.py:5622        finally:
window.py:5623            if result is not _STARTUP_DB_APPLY_PENDING:
window.py:5624                self._server_db_update_inflight = False
```

**Evidence (Probe D/E).**

```
[PROBE D] raised = RuntimeError('titlebar')       # _apply_titlebar_counts raising
[PROBE D] outcomes = []                           # complete() never called
[PROBE D] inflight = True                         # leaked
[PROBE D] batch_state is None = False             # transaction state also leaked
[PROBE D] rows published = ['One']
[PROBE D] a2s started = False

[PROBE E] raised = RuntimeError('executor saturated')   # _submit_startup_rest_batches raising
[PROBE E] outcomes = []
[PROBE E] inflight = True
[PROBE E] batch_state is None = True
[PROBE E] rows published = ['One']
```

**Consequences if triggered.** The exception escapes into the GLib idle
dispatch, so nothing upstream recovers. For the rest of the session:
`_manual_update_server_database()` returns immediately at `window.py:5685-5687`
(no manual DB refresh possible), and
`_maybe_schedule_deferred_suggestion_restore()` is permanently gated by
`_server_db_update_inflight` at `window.py:9499-9506` (**autocomplete never
recovers**). Probe D additionally leaves `_startup_row_batch_state` set, which
forces a `superseded` cancel on any later `_apply_db_rows()`
(`window.py:5857-5858`).

**Expected vs actual.** Expected: parity with the synchronous path — the
completion closure runs on every terminal outcome. Actual: it runs only on the
no-exception path.

**Test coverage: none.** The suite tests `row_construction_failed` and
`publication_failed`, both of which are inside guarded regions, but no
post-publication exception case.

**Smallest safe correction.** Wrap the tail of `_publish_startup_row_batches()`
so `complete` always runs, and move `_apply_titlebar_counts()` inside the
guarded region or its own `try`:

```python
try:
    self._apply_titlebar_counts()
    ...
    self._finish_db_rows_application(bool(state["objects"]), True)
finally:
    if callable(complete):
        complete("published")
```

**Regression tests required.** Inject a raise into (a) `_apply_titlebar_counts`,
(b) `_submit_startup_rest_batches`, (c) `_restore_server_companion_if_enabled`.
Assert in each case: `complete` was invoked, `_server_db_update_inflight` is
`False`, `_startup_row_batch_state is None`, the idle callback does not
propagate, and a subsequent `_maybe_schedule_deferred_suggestion_restore()` is
not gated.

**Confidence: Medium-High** on the structural asymmetry (source-confirmed and
probed); **Low** on production trigger likelihood. Impact if triggered is high
and unrecoverable without a restart, which is why I recommend fixing it despite
the Low severity rating.

---

#### Q5 — `format_version=0` conflates a fresh rebuild with a v1→v2 migration

**Source.**

```
window.py:9605    self._suggestion_cache_migration_pending = result.format_version in (0, 1)
```

`prepare_deferred_suggestions()` returns `format_version=0` for
`cache_miss_rebuilt` (`mod_suggestion_cache.py:277-281`) and `1` or `2` for a
cache hit (`mod_suggestion_cache.py:265-269`). Setting `migration_pending` for
`0` is *behaviourally correct* — it routes the rebuilt index to
`_maybe_schedule_mod_suggestion_migration()` (`window.py:9709-9741`), which
persists it via `build_staged_suggestion_migration()`
(`mod_suggestion_cache.py:309-317`) and so avoids an immediate second rebuild.

The problem is naming and instrumentation: a first-time save is logged as
`mod_suggestion_cache_migration_start` / `_complete` (`window.py:9739-9741`,
`9777-9780`), and `_suggestion_cache_migration_created_at` is set from the
worker's `created_at` (`window.py:9606`). A trace reader auditing v1→v2
migration volume will over-count. `tests/test_deferred_mod_suggestions.py:139`
asserts `host._suggestion_cache_migration_pending is True` after a
`cache_miss_rebuilt` result, which locks in the conflation without naming it.

**Smallest safe correction.** Introduce a distinct `_suggestion_cache_save_pending`
flag, or relabel the perf phase with a `kind=first_save|v1_migration` field
derived from `format_version`. The relabel is the smaller diff and does not
change control flow.

**Regression tests required.** Assert the emitted phase/`kind` field
distinguishes `format_version=0` from `format_version=1`, and that both still
result in exactly one staged publication.

**Confidence: High** (source-read; behaviour verified correct, label verified
misleading).

---

#### Q6 — Two suggestion-cache publications skip the directory fsync

**Source.**

```
window.py:9688    os.replace(staged_path, ... or suggestion_cache_path())    # refresh
window.py:9764    os.replace(staged_path, ... or suggestion_cache_path())    # migration
```

Every other cache publisher in the tree follows `os.replace()` with a directory
fsync: `mod_suggestion_cache.save_suggestion_cache()`
(`mod_suggestion_cache.py:304-305`) and `server_mod_cache.publish()`
(`server_mod_cache.py:179-181`) both call `_fsync_directory_best_effort(path.parent)`.

`os.replace()` is atomic with respect to the namespace, so this is **not** a
correctness defect — a reader always sees either the old or the new file. It is
a crash-durability gap: after a power loss the rename itself may not be
persisted, reverting to the prior catalogue. Sol listed this as a risk; I
confirm the two exact sites and confirm they are the only ones missing the
pattern.

**Smallest safe correction.** Add `_fsync_directory_best_effort(path.parent)`
after each `os.replace()`, or route both through a small shared helper.

**Regression tests required.** Assert the directory fsync helper is invoked on
both the refresh-commit and migration-commit paths (monkeypatch and record).

**Confidence: High.**

---

## 4. Reproduction

All probes ran outside the repository, against the repo's own `tests/conftest.py`
profile boundary, so the working tree was never modified:

```bash
mkdir -p /tmp/pr16_audit_probe
cp /home/gbrown/GithubRepos/DayZ-Linux-Launcher/tests/conftest.py /tmp/pr16_audit_probe/
printf '[pytest]\npythonpath = /home/gbrown/GithubRepos/DayZ-Linux-Launcher/src\n' \
  > /tmp/pr16_audit_probe/pytest.ini
cd /tmp/pr16_audit_probe && python -m pytest -q -s .
```

Probe files written there (not part of the repository):

| File | Probes | Finding |
|---|---|---|
| `test_probe_findings.py` | A, A2, B, B2, C, C2 | Q1, Q2, Q3 |
| `test_probe_containment.py` | D, E | Q4 |
| `test_probe_cache_wipe.py` | F, F2 | Q1 (destructive chain) |

Result: `9 passed` across the three files once the Probe B fixture was
corrected. Probes A/A2 assert the *absence* of the empty state, F asserts the
cache wipe, F2 asserts the synchronous path is correct, C/C2 assert
`reveal_reason == "threshold"` with `completed < target`, D/E assert the
inflight leak. Each probe is written to fail if the corresponding defect is
fixed, so they double as the acceptance checks for §9.

When promoting these to `tests/`, they must move inside the repository's pytest
profile boundary and reuse the existing `make_host` / `warmup_host` doubles in
`tests/test_startup_batching.py` and `tests/test_a2s_warmup_500.py` rather than
duplicating them.

---

## 5. Architecture assessment

### 5.1 GTK/GObject main-thread ownership — sound

Independently verified, not taken on report:

- `ServerObject` construction, all property writes, Python sidecar attachment
  and every `Gio.ListStore` mutation occur inside
  `_build_startup_batched_row()` / `_publish_startup_row_batches()`, which run
  only from the tracked `GLib.idle_add` source (`window.py:6121-6123`,
  `6251-6275`). `tests/test_startup_batching.py:291-310` asserts construction
  happens on the caller thread only when the idle callback runs.
- The deferred worker receives `cache_path` plus a tuple of `mods_json`
  **strings** (`window.py:9511-9513`). The tuple is materialised on GTK by
  reading `obj.mods_json`, so no GObject is dereferenced off-thread.
  `prepare_deferred_suggestions()` (`mod_suggestion_cache.py:255-282`) contains
  no GTK or GObject reference.
- The worker returns only `_ModSuggestionIndex`, dicts, tuples, strings, numbers
  and booleans. Publication back onto GTK happens in
  `_on_deferred_suggestion_restore_done()` via `GLib.idle_add`
  (`window.py:9520-9527`).
- No new executor was added; the existing single-worker `_update_executor` is
  reused.

This is a materially safer design than PR #16's off-thread `ServerObject`
construction, and the tracker's ADAPT decision is vindicated.

### 5.2 Transaction atomicity and lifecycle — good, with the gaps above

Verified working: monotonic generation (`window.py:6087-6088`); generation +
shutdown checked at every slice entry (`window.py:6252-6257`) and at publication
(`6278-6281`); private staging containers with the public `store`,
`column_view_store` and `_obj_by_key` left empty until the single splice at
`window.py:6290-6292`; no GLib yield between the source splice and the complete
visible rebuild (`6290-6314`), so no ordinary GTK callback can observe a
partially published row set; `_server_mod_cache_records` / `_candidate` /
`_needs_repair` snapshotted before mutation (`6285-6289`) and restored on
rollback (`6443-6448`); `committed_live_keys` tracking so only
transaction-added live entries are removed (`6294-6297`, `6437-6439`);
transaction state cleared *before* post-load coordination (`6395`) so
`_finish_db_rows_application` cannot observe stale state.

Duplicate-endpoint semantics match the synchronous path: duplicates stay as
separate ordered source rows while the last wins in `_obj_by_key`
(`window.py:6238-6240` vs `6793-6795`), asserted by
`tests/test_startup_batching.py:152-196`.

Supersession and shutdown: `_cancel_startup_row_batches()` bumps the generation,
drops state and removes the tracked source (`window.py:6477-6496`); shutdown
calls it at `window.py:3094` before incrementing the suggestion and server-cache
generations (`3095-3099`). The `superseded` path is defensive-only in practice
(§Q2 scope note).

Gaps: Q1 (success reporting), Q2 (map model outside the transaction), Q4 (no
containment after publication).

### 5.3 Cache ownership, publication and invalidation — sound

Per-server cache: keyed by canonical `IP:gameport` plus SHA-256 of the exact
accepted raw `mods` string (`server_mod_cache.py:33-34`); bounded by
`MAX_BYTES = 64 MiB` and `MAX_ENTRIES = 50_000` (`server_mod_cache.py:20-21`);
magic + digest + `\n` framing with `hmac.compare_digest`
(`server_mod_cache.py:90-101`); strict schema/normalization version checks
(`server_mod_cache.py:105-108`); staged worker write with generation and
shutdown guards and `os.replace` + directory fsync on publish
(`window.py:6521-6586`, `server_mod_cache.py:143-181`). Provisional loads and
failed loads do not schedule canonical publication
(`window.py:5975`, `6453-6475`).

Global suggestion cache: v2 exact-payload-byte digest with v1 compatibility and
deferred migration (`mod_suggestion_cache.py:125-240`); bounded read;
per-entry/per-lookup validation with hard caps (`mod_suggestion_cache.py:58-83`,
`96-104`); `save_suggestion_cache` does temp-file + fsync + `os.replace` +
directory fsync (`mod_suggestion_cache.py:285-306`). The deferred worker
deliberately publishes no file, which correctly avoids a worker writing stale
data before GTK validates the generation.

Mutual exclusion is correct: the deferred restore refuses to overlap a refresh
or migration and waits while a DB update is in flight (`window.py:9499-9506`);
refresh refuses while a migration is pending (`window.py:9641-9644`); migration
refuses while a refresh is in flight (`window.py:9711-9714`). Only one deferred
restore can be in flight (`window.py:9495-9498`), and the result callback clears
the flag only when it owns the generation (`window.py:9548-9555`), so an old
callback cannot release a newer operation's ownership.

Gaps: Q1 (destructive empty prune), Q5 (labelling), Q6 (fsync).

### 5.4 A2S dispatch, accounting and sorting — sound except Q3

Queue construction is unchanged: `read_servers_from_db()` orders by
`players DESC, ping ASC` (`db.py:145-148`), `_finish_db_rows_application()`
stable-sorts keys by `_bm_live_group()` (`window.py:6005-6007`), and
`warmup_count` counts bands 0-1 (`window.py:6006`). The experimental cohort is
the first 500 of that existing prefix with first-occurrence dedup
(`window.py:7487-7496`); the full distinct queue is still dispatched. Target is
ceiling 60% (`window.py:7512-7514`, `_A2S_WARMUP_EXPERIMENT_READY_PERCENT = 60`
at `window.py:309`). Both switches require the exact value `"1"`
(`window.py:306-307`), and manual/post-startup replacement falls back to the
full cohort at 65% (`window.py:7484-7487`,
`tests/test_a2s_warmup_500.py:229-238`).

Reveal ordering is correct: generation check → single-reveal guard → remove
timeout and flush sources → flush → final ping sort → complete presentation
(`window.py:7580-7614`). `_startup_warmup_revealed` is set before the flush
(`window.py:7600`), so the re-entrant `_maybe_finish_startup_rest_sweep()` call
from `_flush_startup_rest_results()` cannot recurse into a second reveal.

`_auto_sort_lowest_ping_after_startup()` preserves a user-selected sort
(`window.py:10974-10979`), covered by
`tests/test_a2s_warmup_500.py:245-259`.

Generation replacement and shutdown invalidate late callbacks
(`window.py:7696-7698`, `7655-7658`). Gap: Q3.

### 5.5 Deferred suggestions — sound

Authorisation boundary is overlay dismissal (`window.py:5498-5501`), reached from
`_complete_startup_presentation()`. Before readiness the panel hides with
`catalogue_not_ready` (`window.py:10019-10024`) and exact-chip resolution
returns no match while leaving the typed text retryable
(`window.py:9219-9223`). Selected chips keep working because filtering uses each
row's committed `mod_search_index`, not the global catalogue — asserted by
`tests/test_deferred_mod_suggestions.py:143-165`. An active query is refreshed
without another keystroke after publication (`window.py:9612-9615`).

One benign ordering artefact worth recording: at publication time
`_server_db_update_inflight` is still `True` (it is cleared by
`complete("published")` at `window.py:6423`, after
`_finish_db_rows_application()` at `6398`), so the first
`_maybe_schedule_deferred_suggestion_restore()` call from
`_complete_startup_presentation()` is gated out at `window.py:9501`. Recovery is
automatic via `_browser_live_tick()`, which calls the scheduler before its pause
check (`window.py:8358-8360`) and repeats from construction
(`window.py:1403`). Net effect is at most one `BROWSER_LIVE_INTERVAL_SECS` delay
to autocomplete readiness, not a stall. Not a defect; relevant to interpreting
`deferred_suggestion_schedule` timestamps in benchmark traces.

### 5.6 Memory and resource assessment

**Temporarily retained by the batched transaction** (`window.py:6090-6119`):
the input row tuple (~12,000 dicts, shared with the DB callback's list), the
staged `ServerObject` list, `obj_by_key`, `live_defaults`, a `dict(records)`
shallow copy of the per-server cache, and `cache_candidate`. The `dict(records)`
copy shares the underlying record objects, so it is a pointer shell, not a
payload duplicate. `self.store` stays empty until publication, so the
`ServerObject`s are not doubled.

**Peak overlap** occurs after the splice and before `_publish_startup_row_batches`
returns: the local `state` still pins `rows`, `objects`, `records` and
`cache_candidate` across `_finish_db_rows_application()` and the perf-logging
block. Since publication assigns the *same* objects
(`self._server_mod_cache_records = state["records"]`,
`self._server_mod_cache_candidate = state["cache_candidate"]`,
`window.py:6299-6303`), the extra retention is dict shells plus one list of
pointers. Modest and bounded.

**Deferred worker transient peak** is the larger unknown: `load_suggestion_cache()`
holds the raw buffer (≤64 MiB, `mod_suggestion_cache.py:158-163`), the parsed
`document`, and the restored index simultaneously. `_restore_index()` mutates
entry dicts in place and returns tuples over the same dicts
(`mod_suggestion_cache.py:96-140`), so `document` and `restored` share structure
and `document` becomes unreachable on return. The 19 MB / 20,197-entry catalogue
cited in the tracker is the realistic case. **No time-aligned RSS/USS measurement
was supplied** — this remains the single largest acceptance gap and I concur with
Sol that high-water `ru_maxrss` (used at `window.py:6583`) cannot answer it.

**Unbounded allocations:** none found. Every cache path is bounded by an explicit
constant before allocation (`server_mod_cache.py:20-24`,
`mod_suggestion_cache.py:24-27`), and `_lookup()` validates index bounds against
the entry count (`mod_suggestion_cache.py:76-88`).

**Worker/executor starvation:** the deferred restore, monthly refresh, v1
migration and per-server cache write all share the single-worker
`_update_executor`, and the mutual-exclusion guards (`window.py:9499-9506`,
`9641-9644`, `9711-9714`) serialise them. Worst case is a queue, not a
deadlock — every waiter is re-evaluated from a repeating timer. A cold start
(missing global cache *and* cold per-server cache) can serialise a full catalogue
rebuild ahead of the per-server staging; that is a latency, not a correctness,
concern.

**Excessive GTK idle scheduling:** one tracked idle source for the whole
transaction, self-removing on terminal outcomes. ~140-144 slices at 12,047 rows
means ~140 main-loop round trips; total DB-ready-to-publication wall time rises
while the longest uninterrupted section falls. That is the documented and
intended trade.

**Duplicate synchronous/batched conversion:** confirmed present and live
(R6). Parity is protected by `tests/test_startup_batching.py:152-196` and
`tests/test_browser_server_ingestion_validation.py`. This must be consolidated
or deleted before the experiment becomes permanent; carrying two ingestion
implementations indefinitely is the largest long-term maintenance risk in
PR16-01.

---

## 6. Functional parity assessment

| Area | Verdict | Basis |
|---|---|---|
| Favourites | Parity | `fav = bool(self.favorites.get(k, False))` identical (`window.py:6216` vs `6779`); asserted `test_startup_batching.py:152-196` |
| Previously Joined | Parity | `last_played` / `human_last_played` identical (`window.py:6217-6219` vs `6780-6782`) |
| Mod preview + per-server index | Parity | Same `parse_mods_preview` / `build_server_mod_index` calls with the shared `ParsedServerMods` (`window.py:6176-6181` vs `6723-6727`); `mod_search.py:101-150` and `mod_parsed.py` keep the parsed and JSON routes equivalent — both filter non-dict items, both use `clean_display_mod_name(..., fallback=False)` |
| Selected mod chips | Parity, and correctly independent of the deferred catalogue | Chips filter via each row's committed `mod_search_index`; `test_deferred_mod_suggestions.py:143-165` |
| Global autocomplete | Deliberately deferred | `catalogue_not_ready` contract at `window.py:10019-10024`; retry text preserved; `test_deferred_mod_suggestions.py:143-165` |
| Duplicate endpoints | Parity | Separate source rows, last wins in `_obj_by_key` (`window.py:6238-6240` vs `6793-6795`) |
| Raw mod info / join-time authority | Parity | `mods_json` set from the same accepted raw string (`window.py:6163-6164` vs `6710-6711`); the per-server cache restores only preview/search derivations, never join-time parsing |
| Server Companion | **Divergence in the Q1 case only** | `_server_companion_rows_loaded = True` and `_restore_server_companion_if_enabled()` on a zero-row batched load (Probe A/F) |
| Synchronous startup path | Preserved | `_load_rows_into_store()` unchanged in behaviour; all ten fallback reasons route away from batching (`window.py:6024-6047`) |
| Cold / warm / missing / corrupt / expired caches | Correct in the supported paths | Bounded reads, digest + schema validation, v1 compatibility, deferred migration, monthly refresh; **except** the Q1 zero-row wipe and R7 empty-catalogue poison |

Column View remains the only server-browser presentation; no legacy sort model
is enabled by the change (`ENABLE_LEGACY_GTK_SORT_MODEL` still requires the exact
value `"1"`, `window.py:319`). No SteamCMD, Flatpak Steam or
`steam +workshop_download_item` path was introduced — I verified the diff
touches only `app.py`, `mod_search.py`, `mod_suggestions.py`, `window.py` and
four new modules, none of which reference Steam backends.

---

## 7. Comparison with Sol's known findings

| Sol | My verdict | Notes |
|---|---|---|
| **L1** — map-choice state survives a failed/cancelled batched transaction | **Confirmed** (Q2, Low) | Reproduced for both the row-construction-failure and cancellation variants. I narrow the scope: the `superseded` variant is effectively unreachable because all three `_apply_db_rows()` call sites are gated, so the reachable triggers are the three failure outcomes. Severity Low is correct — cosmetic UI contradiction, no persistent-state damage. |
| **L2** — A2S sweep exhaustion may claim threshold readiness without meeting the cohort target | **Confirmed** (Q3, Low) | Reproduced in two variants (missing objects; executor submit failure), both yielding `completed=0, target=6, reveal_reason="threshold"`. I add the specific mechanism Sol did not enumerate: `_complete_startup_warmup()`'s reason-derivation at `window.py:7585-7590` is bypassed precisely because `"threshold"` is in the accepted set, so passing `None` at `window.py:7774` is a one-token fix. I also note the blast radius is broader than the experimental mode — the exhaustion branch is mode-independent, so the default 65% path mislabels too. |
| **M1** — tracker materially stale | **Confirmed** (M1, Medium) | Unchanged; must be updated before staging. |
| **"No critical or high-severity defect was found in the PR16-01 source or tests"** | **Disagree** | Q1 is a High-severity confirmed defect with persistent-state destruction, reproduced offline in both directions. It is not reachable with the switches unset, so Sol's default-off commit-readiness reasoning is not wholly invalidated — but the claim of no High defect in the PR16-01 source does not hold, and the experiment must not be run against a warm cache profile until Q1 is fixed. |
| Risks list (duplication, staging memory, slice overrun, final publication size, GIL contention, orphaned `.pending`, missing dir fsync, earlier-reveal freshness) | **Concur**, mapped to R1-R6 and Q6 | I add the exact `os.replace()` sites (`window.py:9688`, `9764`) and confirm they are the only two missing the fsync pattern. |
| Verification baseline `2768 passed, 26 skipped` | **Superseded** | See §8. |

---

## 8. Verification performed in this audit

All commands ran against the root checkout. No mutating Git command was used;
`git status`, `git diff` and `git log` only.

### Maintained root suite

```text
python -m pytest -q tests
2790 passed, 4 skipped, 56 warnings in 12.55s
```

### Focused PR16-01 and adjacent regression set

```text
python -m pytest -q tests/test_a2s_warmup_500.py tests/test_deferred_mod_suggestions.py \
  tests/test_startup_batching.py tests/test_mod_suggestion_cache.py \
  tests/test_startup_perf_instrumentation.py tests/test_browser_server_ingestion_validation.py \
  tests/test_core_startup_robustness.py tests/test_last_joined_calendar.py \
  tests/test_startup_warmup_stable_live_list.py tests/test_pin_favourites_filter_priority.py
277 passed, 36 warnings in 1.34s
```

### Whitespace

```text
git --no-pager diff --check     # clean, no output
```

### Adversarial probes (outside the repository)

```text
cd /tmp/pr16_audit_probe && python -m pytest -q -s .
9 passed
```

### Baseline discrepancy — important

The prior audit recorded `2768 passed, 26 skipped` for the root suite and
`262 passed, 15 skipped` for the focused set. My runs report **4 and 0 skips**
respectively: a display was available in this environment, so the 22
display-backed tests executed and passed. `2790 − 2768 = 22` and
`26 − 4 = 22`, so the delta is exactly the display-dependent set.

This is *stronger* evidence than the earlier headless run, and the earlier
figures should not be quoted as the current baseline. It also means the prior
caveat "the 26 skips are display-dependent and are not claimed as executed
coverage" no longer applies to those 22 tests on a display-capable machine. The
remaining 4 skips are still not executed coverage.

Warnings are pre-existing PyGObject / Gtk deprecations
(`GLib.unix_signal_add_full`, `Gtk.CssProvider.load_from_data`,
`Gdk.cairo_set_source_pixbuf`, `asyncio.get_event_loop_policy`), unrelated to
PR16-01.

### Working-tree note

`.qwen/settings.json` appeared as untracked during this session; it is a Qwen
Code CLI session artefact, not part of PR16-01, and must be excluded from the
commit. All other dirty/untracked material matches the prior audit's
classification. No file was staged, and no probe artefact was written inside the
repository.

---

## 9. Test sufficiency and remediation

### 9.1 Are offline tests sufficient for an experimental default-off commit?

**Not yet — but close.** The suite is green and the two switches are genuinely
default-off, so end users are unaffected. The blocker is Q1: it destroys
persisted state, and the person most likely to hit it is the maintainer running
the very benchmark that justifies the experiment. A single anomalous zero-row
load wipes the warm per-server cache and silently invalidates every subsequent
A/B comparison. The fix is a two-line gate, so there is no reason to ship
without it.

Sufficient **after**: Q1 fixed with its regression tests; Q4 fixed or explicitly
recorded as a known default-off limitation; Q2/Q3 fixed or recorded likewise;
tracker updated (M1).

Not required before a default-off commit: Q5, Q6, R1-R6. Record them.

### 9.2 Are offline tests sufficient for production-default acceptance?

**No.** Offline tests cannot answer any of the questions that decide
production-default status, and my audit adds two more reasons to say no:

1. Everything in the prior audit's acceptance lists remains outstanding —
   repeated controlled combined runs, external frame/input evidence, per-slice
   max/p95 for the current combined code, time-aligned current/settled RSS/USS
   (R4), warm-v2 / valid-v1 / missing / corrupt / materially-cold cache runs,
   display-backed checks of counts, filters, favourites, Previously Joined,
   chips, map choice, sort and autocomplete, and real shutdown/reload during
   slicing and deferred restoration.
2. **The offline suite has no negative-path coverage for the batched
   transaction's degenerate or post-publication cases.** Q1, Q2 and Q4 were all
   found by inspection and probe, not by any existing test. A green suite
   therefore provides weaker assurance for the batched path than its size
   suggests. Before default-on, the failure matrix must be covered: zero rows,
   all-invalid rows, construction failure, publication failure, idle submission
   failure, post-publication exception, shutdown mid-slice, shutdown mid-deferred
   restore, and cache-wipe non-occurrence for each.
3. For A2S 500/60 specifically: repeated full-sweep completion in **both** arms
   (only one experimental full-sweep run was supplied), runtime cases dominated
   by threshold / timeout / offline / submission failure, sort-and-filter
   interaction before reveal and again after late live updates, and confirmation
   that favourites and Previously Joined servers outside the first 500 remain
   usable and refresh later. Q3 must be fixed first, otherwise
   `reveal_reason` cannot be used as an acceptance criterion at all.

### 9.3 Ordered remediation plan

Ordered to minimise churn and protect currently working behaviour. Each step is
independently committable and independently revertible.

**Step 1 — Q1 (High). Route the degenerate case to the proven synchronous loader.**
Add the emptiness check at the `_apply_db_rows()` call site
(`window.py:5939-5950`) so a zero-row load yields
`reason="no_valid_rows"`. Touches ~3 lines, cannot affect any non-empty load,
and closes both the empty-state loss and the cache wipe. Add regression tests
1-5 from §Q1. *Do not skip this step before the commit.*

**Step 2 — Q1 defence-in-depth.** Change `window.py:6398` to
`self._finish_db_rows_application(bool(state["objects"]), True)` and guard the
deferred-restore arming at `window.py:6321-6323` on non-empty publication. Two
lines. Also closes R7. This is belt-and-braces behind Step 1 and makes the
batched path's success reporting honest by construction rather than by gate.

**Step 3 — Q4 (Low, high impact).** Wrap the tail of
`_publish_startup_row_batches()` in `try/finally` so `complete` always runs, and
move `_apply_titlebar_counts()` (`window.py:6330`) inside a guard. ~6 lines.
Restores parity with the synchronous path's `finally` at `window.py:5622-5624`.
Add the three injection tests from §Q4.

**Step 4 — Q3 (Low).** Pass `reveal_reason="sweep_exhausted"` at
`window.py:7774` and extend the accepted set at `window.py:7585` to include it.
Two lines. Update `tests/test_a2s_warmup_500.py` expectations and add the three
exhaustion cases. Verify no existing test asserts the old label — none does
today, but check `test_startup_warmup_stable_live_list.py` when editing.

**Step 5 — Q2 (Low).** Snapshot the map model strings and selected index into the
transaction state in `_start_startup_row_batches()`; restore in
`_fail_startup_row_batches()` and `_cancel_startup_row_batches()`. ~15 lines,
confined to the experimental path — deliberately *not* the "move
`_set_map_choices()` after publication" refactor, which would also perturb the
synchronous path. Add the four map-state assertions from §Q2.

**Step 6 — Q6 (Low).** Add `_fsync_directory_best_effort(path.parent)` after
`window.py:9688` and `window.py:9764`. Two lines.

**Step 7 — Q5 (Low).** Add a `kind=first_save|v1_migration` field to the two
migration perf phases, derived from `format_version`. Label-only; no control-flow
change.

**Step 8 — M1 (Medium, pre-commit).** Update the tracker: implementation summary,
checklist, closure wording, dated progress entries, and the Q1-Q6 findings from
this audit. Keep status **IN PROGRESS**. Keep
`devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md` out of the staged set unless the
maintainer wants it committed alongside the other PR16-01 evidence.

**Step 9 — Deferred, not part of this commit.** Consolidate the duplicated
synchronous/batched row conversion (R6) once the experiment is accepted or
rejected. Attempting it now would enlarge the diff and put the working
synchronous path at risk for no experimental benefit.

Steps 1-3 are the pre-commit set I recommend. Steps 4-7 are cheap and can ride
along or follow. Step 8 is mandatory before staging.

---

## 10. Conclusion

PR16-01's core architecture is the right one: GObjects stay on GTK, partial rows
stay private, generations and shutdown are handled consistently, and both
experiments are honestly default-off. The synchronous control path is preserved
and, on a display-capable machine, the full offline suite passes at
**2790 passed, 4 skipped**.

The prior audit's central negative claim does not survive independent testing.
A zero-usable-row successful load under `DZLL_STARTUP_BATCHING=1` is reported as
`loaded=True`, loses the "No Servers Found." presentation, tells Server
Companion that rows are loaded, starts A2S, and schedules a deferred writer that
**overwrites the warm per-server mod cache with an empty record set** — a
persistent, session-surviving regression that the synchronous path does not
have. Alongside it, the batched path's post-publication coordination lacks the
exception containment the synchronous path provides, and can permanently disable
manual DB refresh and autocomplete recovery for the session.

Sol's L1 and L2 are both real and both reproduced here; L2's blast radius is
slightly wider than reported (it also affects the default 65% mode) and L1's
reachable trigger set is slightly narrower (supersession is gated).

Recommended disposition: fix Steps 1-3, update the tracker, then take the single
narrowly staged **default-off** commit using the prior audit's exact path list
(minus `.qwen/settings.json`). Do **not** enable either switch by default. Keep
PR16-01 **IN PROGRESS**.
