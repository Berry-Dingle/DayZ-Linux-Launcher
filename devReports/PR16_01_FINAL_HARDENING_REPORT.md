# PR16-01 — Final Focused Hardening Report: G1/G2/G3/Q5/Q6

Date: 2026-10-11

Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`

Status: **PR16-01 remains IN PROGRESS**

## 1. Scope and safety

Implemented only the requested G1, G2, G3, Q5 and Q6 hardening work.

The checkout was dirty before this pass. Baseline status was recorded before editing. Existing modified and untracked files were preserved. This pass did not stage, commit, reset, clean, checkout, stash, merge, rebase, tag, push, or otherwise mutate Git state or history. No DZLL, Steam, network, Forge, packaging, Discord IPC, PR16 review-clone, tracker, or `MEMORY.md` operation was performed.

Both startup experiments remain exact-value, default-off switches:

- `DZLL_STARTUP_BATCHING` is enabled only when its value is exactly `1`.
- `DZLL_A2S_WARMUP_500` is enabled only when its value is exactly `1`.

No UI feature or UI behaviour was added. Cache formats, GTK ownership, lifecycle/generation guards, and publication counts are unchanged.

## 2. Files changed by this pass

- `src/dzll_launcher/window.py`
- `tests/test_startup_batching.py`
- `tests/test_deferred_mod_suggestions.py`
- `tests/test_mod_suggestion_cache.py`
- `devReports/PR16_01_FINAL_HARDENING_REPORT.md`

The source and test files above already formed part of the dirty/untracked PR16-01 working tree. Unrelated dirty and untracked paths were not modified.

## 3. Implementation

### G1 — load-bearing filter-state rollback coverage

Extended `test_publication_failure_rolls_back_and_recovers_overlay` so the injected `_on_filter_changed` first replaces `_filter_state` with a publication-dirty value and then raises. The existing full map-state equality assertion now proves publication rollback restores the original choices, selection and cached filter state. This test would fail if the explicit `_filter_state` restore were removed.

### G2 — suppression-depth recovery after widget failure

Added `test_map_restore_balances_filter_suppression_when_widget_raises`. It injects a map-dropdown selection exception during restoration and verifies:

- `_filter_refresh_suppress_depth` returns to zero;
- the snapshotted `_filter_state` is still restored.

The production rollback implementation did not require alteration; its existing nested `try/finally` and outer containment already provide the required behaviour.

### G3 — chained supersession rollback

Added `test_superseding_batch_snapshots_the_restored_map_state`. The regression test performs the complete chain:

1. transaction A snapshots the original map state and changes the map model;
2. transaction B supersedes A;
3. cancellation of A restores the original state before B takes its snapshot;
4. B changes the model;
5. cancellation of B restores the original state.

The test also rejects both late idle callbacks and verifies suppression depth remains zero.

### Q5 — first-save versus v1-migration instrumentation

Added the minimal `_suggestion_cache_migration_kind` field. It is descriptive instrumentation state only and does not participate in scheduling decisions.

The field is derived from the cache format already returned by the worker:

- format version `0` → `kind=first_save`;
- format version `1` → `kind=v1_migration`;
- format version `2` → no pending publication kind.

Both `mod_suggestion_cache_migration_start` and `mod_suggestion_cache_migration_complete` now include the `kind` field. The existing pending/inflight flags, worker submission, lifecycle guards, and single-publication behaviour remain unchanged. The kind is cleared after successful publication and when a normal refresh supersedes migration state.

Added a parameterized regression test covering both format versions. Each case verifies the emitted start and completion kind and proves repeated scheduling attempts still produce exactly one staged publication.

### Q6 — directory durability after staged publication

Reused the existing `_fsync_directory_best_effort` helper from `atomic_json.py`. After each successful `os.replace`, the GTK-thread publication callbacks now best-effort fsync the destination directory for:

- suggestion-cache refresh publication;
- first-save/v1-migration publication.

No cache serialization or on-disk format changed. Atomic replacement remains the publication operation.

Targeted tests monkeypatch and record the shared helper, proving it is called with the destination parent directory after both publication paths.

## 4. Verification

### Focused tests

Command:

```text
python -m pytest -q tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py tests/test_mod_suggestion_cache.py
```

Result:

```text
72 passed, 1 warning in 0.33s
```

### Full maintained suite

Command:

```text
python -m pytest -q tests
```

Result:

```text
2789 passed, 26 skipped, 1 warning in 10.90s
```

The warning was the existing PyGObject deprecation warning for `GLib.unix_signal_add_full`. The 26 skips were reported honestly; no test was suppressed, deselected or marked xfail by this pass.

### Compilation

Command:

```text
env PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-final-hardening-pyc python -m compileall -q src/dzll_launcher tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py tests/test_mod_suggestion_cache.py
```

Result: passed with no output.

### Diff validation

Command:

```text
git --no-pager diff --check
```

Result: passed with no output.

The final scoped diff and status were inspected. No mutating Git command was used.

## 5. Final disposition

G1, G2, G3, Q5 and Q6 are implemented and covered by focused regressions. Required focused tests, the full maintained suite, compilation and `git diff --check` all pass. The requested architecture and scope constraints are preserved. The tracker and project memory remain unchanged, and PR16-01 remains **IN PROGRESS** pending its separately required acceptance evidence.
