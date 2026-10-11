# PR16-01 — Independent Verification of Final Hardening (G1/G2/G3/Q5/Q6)

Date: 2026-10-11
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`
Verified against: working tree at `de504a4` plus all dirty/untracked PR16-01 material
Hardening under review: `devReports/PR16_01_FINAL_HARDENING_REPORT.md`
Prior findings source: `devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md` (Q5, Q6),
`devReports/PR16_01_Q2_Q3_V1_INDEPENDENT_VERIFICATION.md` (G1, G2, G3)

Verification mode: **strictly read-only.** No repository source, test, tracker or
`MEMORY.md` file was modified. Nothing was staged or committed. No mutating Git
command was run. DZLL and Steam were not launched. No network operation,
dependency installation, Forge, Discord IPC, packaging or UI action occurred. All
dirty and untracked files were preserved. Temporary probes were written only
under `/tmp/pr16_audit_probe/`. Both startup switches remain exact-value,
default-off (`window.py:306-308`, each `== "1"` — verified unchanged).

PR16-01 status: **IN PROGRESS** (unchanged).

---

## 1. Verdict

| Item | Status | Confidence |
|---|---|---|
| **G1** — `_filter_state` rollback coverage | **CLOSED, and proven load-bearing by mutation** | High |
| **G2** — suppression-depth recovery after widget failure | **CLOSED, and proven load-bearing by mutation** | High |
| **G3** — chained supersession snapshot ordering | **CLOSED, and proven load-bearing by mutation** | High |
| **Q5** — `first_save` vs `v1_migration` instrumentation | **FIXED AND VERIFIED** | High |
| **Q6** — directory fsync after staged publication | **FIXED AND VERIFIED** | High |

**No regression was introduced.** All 68 of my current-generation verification
probes pass, covering Q1–Q4, Q2/Q3/V1 and this pass. The full offline suite
passes, scope is contained to `window.py` plus three PR16-01 test files, and both
switches remain default-off.

I found **no confirmed source defect** in this pass. I found **two Low
test-quality gaps (H1, H2)**, **one characterized latent fragility (H3)**, and
**three informational observations (H4–H6)**. None blocks the scoped default-off
commit.

**Disposition.** With this pass, **every confirmed defect raised across all three
of my independent reviews is now closed**: Q1, Q2, Q3, Q4, Q5, Q6 and V1. The
only remaining pre-commit blocker is **M1** (stale tracker), which is a
documentation task, not a code task. Production-default acceptance remains
**blocked** on the runtime, memory and display-backed evidence carried forward
since the first audit.

---

## 2. Method

I re-derived each fix from source rather than accepting the report's description.
For G1/G2/G3 I went further than behavioural probing and ran **mutation tests**:
I bound deliberately-mutated copies of the production helpers onto host doubles
(at class level, because production calls them class-qualified) and confirmed the
repository's new assertions *fail* under each mutation. A test that survives
mutation of the line it claims to cover is not load-bearing, and that is exactly
the weakness G1 was raised for.

Probe files under `/tmp/pr16_audit_probe/` (outside the repository, reusing the
repo's `tests/conftest.py` isolated-profile boundary):

| File | Probes | Focus |
|---|---|---|
| `test_verify_hardening.py` | 14 | Q5 kind mapping for all three format versions, single publication, generation/shutdown invalidation, refresh supersession; Q6 ordering, destination correctness, no-fsync-on-failure, latent fragility |
| `test_verify_mutation.py` | 6 | G1/G2/G3 production behaviour **and** mutation detection |
| `test_verify_q1_q4.py`, `test_verify_edges.py`, `test_verify_q2.py`, `test_verify_q3_v1.py` | 48 | Re-run unchanged to detect regressions in previously verified fixes |

Repository gates re-run:

```text
python -m pytest -q tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py \
  tests/test_mod_suggestion_cache.py
72 passed, 1 warning in 0.21s                       (matches the report exactly)

python -m pytest -q tests
2811 passed, 4 skipped, 53 warnings in 12.62s       (display available)

env PYTHONPYCACHEPREFIX=/tmp/dzll-final-verify-pyc python -m compileall -q \
  src/dzll_launcher tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py \
  tests/test_mod_suggestion_cache.py
passed

git --no-pager diff --check
passed, no output
```

**Baseline reconciliation.** The report quotes `2789 passed, 26 skipped` (headless).
On this display-capable machine the same command gives **2811 passed, 4 skipped**.
`2811 − 2789 = 22` and `26 − 4 = 22` — exactly the display-dependent set, which
executes and passes here. Relative to my previous verification pass
(2806 passed / 4 skipped) the delta is **+5 passed, 0 skipped**, i.e. tests were
genuinely added and no test was skipped, deselected or xfailed to reach green.
Both runs agree once the environment is accounted for.

**Scope containment verified.** `app.py` (14), `mod_search.py` (23),
`mod_suggestions.py` (19) and the three modified test files (578 / 59 / 5) have
insertion counts identical to the previous pass. Only `window.py` grew
(1742 → 1763, **+21**) plus the three untracked PR16-01 test files. No unrelated
dirty or untracked file was altered. `.qwen/` remains a Qwen Code CLI session
artefact and must be excluded from any commit.

---

## 3. G1 — load-bearing filter-state rollback

### 3.1 Implementation and test reviewed

The production rollback is unchanged by this pass: `_restore_startup_map_state`
(`window.py:6551-6567`) restores the model via `_set_map_choices`, re-applies the
snapshotted selected index under suppression, and then restores the cached filter
state **outside** the widget `try` at `window.py:6567`.

The repository test was extended as claimed
(`tests/test_startup_batching.py:347-378`). The injected failure now mutates
before raising:

```python
def fail_after_filter_state_change(**_kwargs):
    host._filter_state = {"selected_map": "Chernarus", "marker": "publication-dirty"}
    raise RuntimeError("publication failed")
```

and the existing `assert map_state(host) == before` compares
`dict(host._filter_state)` alongside the model rows and selected index.

### 3.2 Mutation result — the test is genuinely load-bearing

| Variant | `_filter_state` after rollback | Repo assertion |
|---|---|---|
| Production | `{"selected_map": "Livonia", "marker": "original"}` | **passes** |
| `window.py:6567` removed | `{"selected_map": "Chernarus", "marker": "publication-dirty"}` | **fails** |

Under mutation the widget-level parts are still restored (`map_model.rows` and
`map_dropdown.get_selected()` both equal the originals), which isolates the
mutation precisely to the cached-filter-state line. This is the exact gap I
raised as G1 — previously the line could be deleted with a green suite. **G1 is
closed.**

I also confirmed no other mechanism could be doing this work:
`_rollback_startup_row_publication` handles `store`, `_obj_by_key`, `live` and
the server-mod cache only, and never touches `_filter_state`.

### 3.3 Production realism

The injected failure mirrors real `_on_filter_changed`, which assigns
`self._filter_state = new_state` at `window.py:10576` (and passes `state=new_state`
into `_apply_filter_changed` at `10591`) *before* the visible-store rebuild that
can raise. So the dirty-then-raise ordering is not artificial.

---

## 4. G2 — suppression balance after widget failure

### 4.1 Implementation and test reviewed

`window.py:6557-6563` wraps the explicit index restore in `push` / `try` /
`finally: pop`, and the whole widget block sits inside an outer
`try/except Exception → logger.exception`. `_set_map_choices` has its own
balanced `push`/`try`/`except`/`finally: pop` at `window.py:2404-2409`.

New test `tests/test_startup_batching.py:380-405`
(`test_map_restore_balances_filter_suppression_when_widget_raises`) binds the
**real** `_set_map_choices`, `_push_filter_refresh_suppression` and
`_pop_filter_refresh_suppression`, replaces `map_dropdown.set_selected` with a
raiser, and asserts `_filter_refresh_suppress_depth == 0` plus restoration of
`_filter_state`. The report's claim that no production change was needed is
correct — the existing nested `try/finally` already provided the behaviour.

### 4.2 Mutation result

| Variant | Depth after restore | Repo assertion |
|---|---|---|
| Production | `0` | **passes** |
| Inner `try/finally` removed | `1` (leaked) | **fails** |

**G2 is closed.** A leaked depth would silently suppress every later filter
refresh for the session, so this is a high-value assertion despite the Low
severity.

### 4.3 Honest scoping observed in the test

The test asserts the depth and `_filter_state`, but deliberately **not** the
selected index. That is correct: when `set_selected` genuinely raises, the index
cannot be restored. I verified what state results — `map_model` *is* restored
(the `splice` at `window.py:2398` precedes the failing `set_selected`), while the
index keeps its prior value. Because index 0 is always `"All Maps"`, the model
and selection remain mutually consistent and benign. The test does not
over-claim, which is the right call.

---

## 5. G3 — chained supersession

### 5.1 Implementation and test reviewed

The ordering that makes chaining correct is unchanged: `_apply_db_rows` cancels a
live transaction at `window.py:5857-5858` **before** capturing its own snapshot
at `window.py:5900`. `_cancel_startup_row_batches` restores at `window.py:6605`
before bumping the generation at `6606-6608`.

New test `tests/test_startup_batching.py:272-302`
(`test_superseding_batch_snapshots_the_restored_map_state`) performs the full
chain and asserts the decisive intermediate:

```python
assert host._startup_row_batch_state["previous_map_state"] == {
    "choices": tuple(before[0]), "selected": before[1], "filter_state": before[2],
}
```

i.e. transaction **B snapshotted the restored original state, not A's dirty
model** — followed by `map_state(host) == before` after cancelling B, depth 0,
`removed == [1, 2]` and rejection of both late idle callbacks.

### 5.2 Mutation result

| Variant | B's snapshot `choices` | Repo assertion |
|---|---|---|
| Production | `("All Maps", "Livonia")` — the original | **passes** |
| Restore disabled | `("All Maps", "Chernarus")` — A's dirty model | **fails** |

**G3 is closed.** This is the ordering a future refactor could most easily invert
without any test noticing, so pinning it is worthwhile.

### 5.3 Minor note

The test drives the second cancellation with reason `"shutdown"` rather than a
second `"superseded"`. The reason only feeds the `startup_row_batch_cancelled`
perf marker (`window.py:6612-6618`); the restore mechanics are identical, so
coverage is equivalent. No action needed.

---

## 6. Q5 — `first_save` vs `v1_migration` instrumentation

### 6.1 Implementation reviewed

A single descriptive field `_suggestion_cache_migration_kind` was added. Every
site that sets `_suggestion_cache_migration_pending` sets `kind` consistently:

| Site | `pending` | `kind` |
|---|---|---|
| `window.py:795` (init) | — | `None` |
| `window.py:9584-9588` (synchronous cache load) | `format_version == 1` | `"v1_migration"` iff fv 1, else `None` |
| `window.py:9610-9612` (synchronous save success) | `False` | `None` |
| `window.py:9741-9745` (deferred restore) | `format_version in (0, 1)` | `{0: "first_save", 1: "v1_migration"}.get(fv)` → `None` for fv 2 |
| `window.py:9847-9848` (refresh commit) | `False` | `None` |
| `window.py:9928-9929` (migration commit) | `False` | `None` |

Both markers carry it: `mod_suggestion_cache_migration_start` at
`window.py:9887-9888` and `mod_suggestion_cache_migration_complete` at
`window.py:9907` (`worker_failed`), `9919` (`publish_failed`) and `9925`
(`committed`).

As the report states, the field is descriptive only — I confirmed by reading
`_maybe_schedule_mod_suggestion_migration` (`window.py:9853-9888`) that no
scheduling decision reads `kind`. The gating set is unchanged: shutdown,
`migration_pending`, `migration_inflight`, `refresh_inflight`,
`_server_db_update_inflight`, sweep completion, and the retry deadline.

### 6.2 Verification

| Requirement | Verdict | Evidence |
|---|---|---|
| fv 0 → `first_save` | **PASS** | Probe + repo parametrized case |
| fv 1 → `v1_migration` | **PASS** | Probe + repo parametrized case |
| fv 2 → `kind is None`, `pending is False`, **no publication scheduled** | **PASS (probe only — see H2)** | Two scheduling attempts produce `executor.calls == []` |
| Start and completion markers carry the same kind | **PASS** | `by_name["…_start"]["kind"] == by_name["…_complete"]["kind"] == expected`, `result == "committed"` |
| Single publication under repeated scheduling | **PASS** | `len(executor.calls) == 1` across repeated `_maybe_schedule_mod_suggestion_migration` calls; `_migration_inflight` set at `window.py:9872` before submit, cleared first in the callback at `9890` |
| Kind cleared after successful publication | **PASS** | `kind is None` and `pending is False` post-commit |
| Generation invalidation | **PASS** | Stale generation unlinks the staged file and returns **without** clearing the newer `kind`/`pending` — both preserved (`window.py:9899-9907`) |
| Shutdown | **PASS** | Stage discarded, nothing published, `pending` preserved so a later session can retry |
| Superseding refresh | **PASS** | Mutual exclusion holds in both directions (`window.py:9781`/`9783` block refresh while migration is pending or in flight; `window.py:9859` blocks migration while a refresh is in flight), so a refresh cannot commit during an in-flight migration; refresh commit clears both `pending` and `kind` |

The original Q5 complaint — a first-time save being logged and tracked as a
"migration" — is resolved: the two cases are now distinguishable in both markers
without any change to control flow, cache format or publication count.

### 6.3 Observations (informational, not defects)

**H4 — the `or "v1_migration"` fallbacks are unreachable.** At `window.py:9871`
and `9891`, `kind = getattr(self, "_suggestion_cache_migration_kind", None) or "v1_migration"`.
Every site that sets `pending = True` also sets a non-None `kind`, so the
fallback never fires. Harmless defensive defaulting; recorded because it could
mislead a reader into thinking a missing kind is normal.

**H5 — `kind` is read at callback time, not captured at submission.**
`_on_mod_suggestion_migration_done` re-reads `kind` from current state
(`window.py:9891`) rather than closing over it like `gen` and `started` already
are (`window.py:9875-9877`). If a new DB load bumped the generation and reset
`kind` while a migration was in flight, a `worker_failed` marker could carry the
*newer* kind for the *older* submission. Instrumentation-only, extremely narrow,
and the `committed` path is protected by the generation check at
`window.py:9899-9901`. Optional one-line hardening: capture `kind` in the
`add_done_callback` closure.

---

## 7. Q6 — directory durability after staged publication

### 7.1 Implementation reviewed

Both GTK-thread publication callbacks now fsync the destination directory after
a successful atomic replace, reusing the existing shared helper imported at
`window.py:204`:

| Path | Replace | Fsync |
|---|---|---|
| Refresh publication | `window.py:9831` | `window.py:9832` |
| First-save / v1-migration publication | `window.py:9914` | `window.py:9915` |

### 7.2 Verification

| Requirement | Verdict | Evidence |
|---|---|---|
| Both paths fsync after a successful `os.replace` | **PASS** | Ordered event recording gives exactly `["replace", "fsync"]` for **both** paths |
| The **correct destination** directory is fsynced | **PASS** | `fsynced == [tmp_path]` — the destination's parent, not the stage's. Both are the same directory here because `build_staged_suggestion_migration` / `build_staged_suggestion_cache` stage with `path.with_name(...)` (`mod_suggestion_cache.py:325`, `336`), which also guarantees `os.replace` is same-filesystem |
| Ordering | **PASS (probe only — see H1)** | Replace strictly precedes fsync; matches the canonical pattern at `atomic_json.py:33-36`, `mod_suggestion_cache.py:309-311`, `server_mod_cache.py:181-183` |
| Atomic replacement semantics intact | **PASS** | `os.replace` remains the publication operation; fsync is strictly after and cannot alter it. Verified the destination holds the replacement bytes and the v2 format loads back (`format_version == 2`), and the in-memory index commits afterwards |
| Exception handling | **PASS** | Fsync sits inside the same `try/except OSError` as the replace. `_fsync_directory_best_effort` (`atomic_json.py:52-66`) swallows `OSError` internally, so it cannot trigger that handler — verified empirically, including against a non-existent path |
| No fsync when publication is not committed | **PASS** | `fsynced == []` when `os.replace` raises (stage unlinked, retry armed, no destination file) and when the generation is stale (stage unlinked) |

The original Q6 finding is resolved: these were the only two cache publications
in the tree missing the directory fsync, and both now match the established
pattern.

### 7.3 H3 — characterized latent fragility (hypothetical, not a defect)

Because the fsync call sits **inside** the replace's `except OSError` scope, a
post-replace fsync failure would be misreported: the handler would unlink the
already-renamed stage (a no-op via `missing_ok=True`), arm a retry, log
`publish_failed`, and `return False` **before** the in-memory commit — leaving
disk holding the new catalogue while memory keeps the old index.

I demonstrated the divergence by injecting a raising fsync:

```
[Q6-latent] on-disk content   = b'replacement'
[Q6-latent] in-memory count   = 0
[Q6-latent] retry armed       = True
```

**This is unreachable today.** The shipped helper swallows `OSError`
(`atomic_json.py:57-58`), and `destination.parent` is always a `Path`, so no
non-`OSError` is reachable either. It is recorded because the correctness depends
on an invariant held in a *different module*: if `_fsync_directory_best_effort`
were ever made strict, both publication callbacks would silently acquire this
divergence.

Cheapest hardening — move the fsync out of the `try` that guards the replace, so
a durability hint can never fail a successful publication:

```python
os.replace(staged_path, destination)          # inside try/except OSError
...commit in-memory state...
_fsync_directory_best_effort(destination.parent)   # best-effort, outside
```

Two lines per site. Optional; I would take it only alongside other work in these
callbacks, since the current code is correct as shipped.

---

## 8. Regression assessment

**No regression found.**

- **Q1 still fixed** — re-ran all probes: zero-row and all-invalid loads route to
  the synchronous empty state, no A2S, no Companion, no 25 s cache timer, warm
  records preserved, no empty deferred persistence, defensive empty terminal
  reports `"empty"`.
- **Q2 still fixed** — 11 probes across construction, publication,
  idle-submission, superseded and shutdown paths, plus success commit.
- **Q3 still fixed** — 13 probes: `sweep_exhausted` in both markers for all three
  non-counting drain paths in both warmup modes, `threshold` only when
  `completed >= target`, exactly-once reveal, complete accounting, timeout
  removal, continued sweep, stale-generation and shutdown rejection.
- **Q4 still fixed** — exactly-once `complete()` across all six terminal paths,
  ownership released on every injected failure, rows retained, partial A2S
  generations invalidated, manual refresh and deferred autocomplete recoverable,
  no exception escapes the idle callback.
- **V1 still closed** — transient titlebar → `published_titlebar_failed` with
  coordination continuing; persistent titlebar → `published_post_load_failed`;
  the two remain distinct.
- **G1/G2/G3 production behaviour** — all three verified independently *and*
  mutation-checked (§3–§5).

Suite growth is +5 tests with **zero** change in skips relative to my previous
pass, so nothing was skipped, deselected or xfailed to reach green.

### 8.1 Note on my own superseded evidence

Of the 79 probes now in `/tmp/pr16_audit_probe/`, **7 fail** — and that is the
expected result. Those 7 are my *original* defect-asserting probes from the first
audit (`test_probe_findings.py`, `test_probe_containment.py`,
`test_probe_cache_wipe.py`), written to fail once the defect is absent. Their
failure is positive evidence that Q1, Q3 and Q4 remain fixed.

Two further original probes (the Q2 map-choice pair) now pass **vacuously** and
should be disregarded: their host double contains no `map_model` or
`map_dropdown`, so `_snapshot_startup_map_state` returns `None` and the restore
is a no-op regardless of the fix. They are superseded by `test_verify_q2.py`,
which uses real widget doubles. I record this so the probe inventory is not
misread as stronger evidence than it is.

---

## 9. Classification summary

### 9.1 Confirmed source defects

**None.** All seven previously confirmed defects (Q1–Q6, V1) are closed.

### 9.2 Test-quality gaps

**H1 — Low. Q6 ordering is not asserted by the repository tests.**
`tests/test_mod_suggestion_cache.py:341` and `:367` both assert
`fsynced == [tmp_path]`, and the surrounding assertions prove the replace
succeeded (`format_version == 2`; `path.read_bytes() == b"replacement"`). Neither
asserts that the fsync happened *after* the replace — swapping `window.py:9831/9832`
or `9914/9915` would keep the suite green while destroying the durability
guarantee (an fsync before the rename persists nothing about it). Smallest
correction: record ordered `(operation, path)` tuples as my probe does and assert
`["replace", "fsync"]`. Confidence: High.

**H2 — Low. `format_version == 2` is not exercised through
`_on_deferred_suggestion_restore_done`.** The new parametrized test covers fv 0
and fv 1 (`tests/test_deferred_mod_suggestions.py:261-263`); `:136` asserts
`pending is True` for the fv-0 worker path and `:303` asserts `pending is False`
*after* a successful commit. No test feeds fv 2 into the deferred callback to
prove `kind is None`, `pending is False` and that no publication is scheduled —
the case where a spurious migration would rewrite an already-current v2 cache.
Adding `(2, "cache_hit", None)` to the existing parametrization is a one-line
change. Confidence: High.

**G4 — informational, carried forward.** The `threshold` arm of the exhaustion
ternary (`window.py:7899-7900`) is unreachable, so it cannot be covered.

### 9.3 Hypothetical risks

**H3 — characterized above (§7.3):** post-replace fsync failure would diverge
disk from memory. Unreachable today; depends on an invariant in `atomic_json.py`.

**H6 — informational:** on a genuine widget failure during map restore, the model
is restored but the selected index is not (§4.3). Benign because index 0 is
always `"All Maps"`.

Risks R1–R6 from the first audit remain open and untouched: slice-budget overrun
at row boundaries, the ~273–284 ms uninterrupted final publication, GIL
contention from the deferred worker, unmeasured peak RSS/USS during
suggestion-cache decode, orphaned `.pending` files on early exit, and
synchronous/batched conversion duplication.

### 9.4 Carried forward, still open

V2 (duplicate `_apply_titlebar_counts` per startup — now the mechanism behind the
`persistent_titlebar` test id), V3 (stale `_startup_live_rest_total` /
`_completed` after post-load failure recovery), V4 (empty-endpoint guard does not
clear `_deferred_suggestion_restore_pending`), and **M1** (stale tracker).

---

## 10. Conclusion and recommendation

G1, G2, G3, Q5 and Q6 are all **independently verified**. The three coverage
gaps are closed by tests that are genuinely load-bearing — I confirmed this by
mutation rather than by reading the assertions, which is the standard G1 was
originally raised against. Q5 and Q6 are minimal, correctly-scoped changes:
Q5 adds one descriptive field with no effect on scheduling, cache format or
publication count; Q6 adds one best-effort durability call after each of the two
atomic replaces, matching the pattern already used everywhere else in the tree.

Recommended next actions, in order:

1. **M1 — update the tracker.** This is now the only pre-commit blocker. It
   should record Q1–Q6, V1, G1–G3 as closed, and H1–H6 plus R1–R6 as open.
2. **H1, H2** — two small test additions (ordered fsync assertion; fv-2 case in
   the existing parametrization). Both are cheap and close the last gaps in this
   pass's own coverage.
3. Then take the single narrowly staged **default-off** commit using the first
   audit's exact path list, excluding `.qwen/`.
4. **Optional, opportunistic only:** H3 (move the fsync outside the replace's
   `except OSError`), H5 (capture `kind` in the callback closure). Neither is
   required for the default-off commit.
5. **Production default remains blocked.** Nothing in this pass supplies the
   runtime, frame/input, per-slice timing, memory or repeated full-sweep
   evidence required, and the post-publication failure paths still need a
   live-GTK fault-injection pass. Keep PR16-01 **IN PROGRESS**.

The remaining long-term item is unchanged from my first audit: the duplicated
synchronous/batched row conversion (R6) must be consolidated or deleted once the
experiment is accepted or rejected. Carrying two ingestion implementations
indefinitely is the largest maintenance risk in PR16-01, and it is correctly
out of scope for every hardening pass so far.
