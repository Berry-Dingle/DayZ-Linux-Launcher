# PR16-01 — Final Pre-Commit Preparation

Date: 2026-10-11  
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`  
HEAD: `de504a4874bf8f8933ad8afc5ba3378b52d02ee0` (`main`, `origin/main`, tag `v0.4.1-beta`)  
Status: **IN PROGRESS**

## Outcome

H1 and H2 are closed with permanent repository regression coverage. The PR16-01
tracker now records Q1-Q6, V1, G1-G3, H1 and H2 as resolved while retaining the
remaining runtime, display-backed responsiveness and RSS/USS acceptance work as
explicitly open. This is preparation for a scoped default-off experimental
commit only; production-default acceptance is not claimed.

No production source was changed in this preparation pass. No DZLL or Steam
process was launched, no network request was made, and no Forge or Discord IPC
work was performed. No Git add, commit, reset, clean, checkout, stash or other
mutating Git command was used. All unrelated dirty and untracked material was
preserved.

Both experiment switches remain exact-value and default-off:

- `src/dzll_launcher/window.py:307` — `DZLL_STARTUP_BATCHING` is enabled only
  when its value is exactly `1`.
- `src/dzll_launcher/window.py:308` — `DZLL_A2S_WARMUP_500` is enabled only
  when its value is exactly `1`.

## Changes made

### H1 — ordered suggestion-cache publication

`tests/test_mod_suggestion_cache.py` now records exact publication operations
for both production completion handlers:

- refresh publication through `_on_mod_suggestion_refresh_done`; and
- shared first-save/v1-migration publication through
  `_on_mod_suggestion_migration_done`.

Each recorder calls the real `os.replace` first and records the replace only
after it returns successfully. The asserted sequence is exactly:

1. successful `os.replace(staged_path, destination)`;
2. directory fsync of `destination.parent`.

The assertions include the exact source, destination and directory paths, so an
fsync moved before the replace, a failed replace, or publication to the wrong
destination cannot satisfy the tests.

### H2 — current-format deferred restoration

`tests/test_deferred_mod_suggestions.py` adds `format_version=2` to the existing
deferred restoration matrix. The v2 case asserts:

- `_suggestion_cache_migration_pending is False`;
- `_suggestion_cache_migration_kind is None`; and
- two migration-scheduling attempts submit no publication job.

The existing format 0 and format 1 cases continue to verify `first_save` and
`v1_migration` respectively and exactly one publication submission.

### Tracker

`devReports/PR16_IMPROVEMENTS_TRACKER.md` now:

- records Q1-Q6, V1, G1-G3, H1 and H2 as resolved with concise evidence;
- describes the current official default-off implementation rather than the
  earlier contributor implementation;
- records the current focused and full offline test evidence;
- retains PR16-01 as **IN PROGRESS**;
- explicitly leaves controlled combined runtime, frame/input responsiveness,
  maximum/p95 slice timing, display-backed lifecycle checks, repeated A2S
  full-sweep validation and current/settled RSS/USS acceptance open; and
- makes no production-default acceptance claim.

## Files changed by this preparation pass

1. `tests/test_mod_suggestion_cache.py`
2. `tests/test_deferred_mod_suggestions.py`
3. `devReports/PR16_IMPROVEMENTS_TRACKER.md`
4. `devReports/PR16_01_PRECOMMIT_PREPARATION.md`

The first three were the only pre-existing files edited. The fourth is this
requested report. All production-source changes visible in the worktree predate
this pass and were only inspected.

## Verification

### Initial focused correction loop

The first two-file run exposed a test-only missing `Path` import introduced by
the new operation recorder:

```text
python -m pytest -q tests/test_mod_suggestion_cache.py tests/test_deferred_mod_suggestions.py
47 passed, 2 failed, 1 warning
```

Both failures were `NameError: name 'Path' is not defined`. Adding the missing
standard-library import corrected the tests; production code was not changed.

The corrected two-file run passed:

```text
python -m pytest -q tests/test_mod_suggestion_cache.py tests/test_deferred_mod_suggestions.py
49 passed, 1 warning in 0.24s
```

### Focused PR16-01 suite

```text
python -m pytest -q tests/test_startup_batching.py \
  tests/test_deferred_mod_suggestions.py tests/test_mod_suggestion_cache.py
73 passed, 1 warning in 0.20s
```

### Maintained full suite

```text
python -m pytest -q tests
2790 passed, 26 skipped, 1 warning in 11.02s
```

The 26 skips are display-dependent and are not claimed as executed coverage.
The warning is the existing PyGObject `GLib.unix_signal_add_full` deprecation.

### Compilation

```text
env PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-precommit-pyc \
  python -m compileall -q src/dzll_launcher \
  tests/test_startup_batching.py \
  tests/test_deferred_mod_suggestions.py \
  tests/test_mod_suggestion_cache.py
```

Result: passed with no output. Bytecode was redirected outside the repository.

### Whitespace and patch integrity

```text
git --no-pager diff --check
```

Result: passed with no output.

## Final working-tree inspection

The tracked dirty baseline remains the same seven accumulated PR16-01 files:

- `src/dzll_launcher/app.py`
- `src/dzll_launcher/mod_search.py`
- `src/dzll_launcher/mod_suggestions.py`
- `src/dzll_launcher/window.py`
- `tests/test_browser_server_ingestion_validation.py`
- `tests/test_core_startup_robustness.py`
- `tests/test_last_joined_calendar.py`

The tracked numstat remains `14/0`, `13/10`, `13/6`, `1727/36`, `577/1`,
`59/0` and `3/2` respectively, matching the pre-task baseline. This confirms
that this pass did not alter tracked production source or the three tracked
test files.

The accumulated PR16-01 untracked source, tests, tracker and reports remain
mixed with unrelated untracked packaging, release, diagnostic and tool-state
material. Nothing is staged.

## Exact file-by-file staging proposal

Stage only the following 43 paths for the single scoped PR16-01 commit. Do not
use a broad pathspec.

### Source — 8 files

1. `src/dzll_launcher/app.py`
2. `src/dzll_launcher/mod_parsed.py`
3. `src/dzll_launcher/mod_search.py`
4. `src/dzll_launcher/mod_suggestion_cache.py`
5. `src/dzll_launcher/mod_suggestions.py`
6. `src/dzll_launcher/server_mod_cache.py`
7. `src/dzll_launcher/startup_perf.py`
8. `src/dzll_launcher/window.py`

### Tests — 8 files

9. `tests/test_a2s_warmup_500.py`
10. `tests/test_browser_server_ingestion_validation.py`
11. `tests/test_core_startup_robustness.py`
12. `tests/test_deferred_mod_suggestions.py`
13. `tests/test_last_joined_calendar.py`
14. `tests/test_mod_suggestion_cache.py`
15. `tests/test_startup_batching.py`
16. `tests/test_startup_perf_instrumentation.py`

### Tracker and PR16-01 evidence — 27 files

17. `devReports/PR16_IMPROVEMENTS_TRACKER.md`
18. `devReports/PR16_01_A2S_STARTUP_REGRESSION.md`
19. `devReports/PR16_01_A2S_WARMUP_500_EXPERIMENT.md`
20. `devReports/PR16_01_CACHE_RESTORATION_OPTIMISATION_REPORT.md`
21. `devReports/PR16_01_CACHE_VERIFICATION_REPORT.md`
22. `devReports/PR16_01_DEFERRED_SUGGESTIONS_REPORT.md`
23. `devReports/PR16_01_FINAL_HARDENING_REPORT.md`
24. `devReports/PR16_01_FINAL_HARDENING_VERIFICATION.md`
25. `devReports/PR16_01_FINAL_INTEGRATION_AUDIT.md`
26. `devReports/PR16_01_GTK_BATCHING_EXPERIMENT_REPORT.md`
27. `devReports/PR16_01_GTK_PHASE_MEASUREMENT_REPORT.md`
28. `devReports/PR16_01_GTK_RESPONSIVENESS_DESIGN.md`
29. `devReports/PR16_01_MOD_INDEX_AUDIT.md`
30. `devReports/PR16_01_MOD_INDEX_OPTIMISATION_REPORT.md`
31. `devReports/PR16_01_PER_SERVER_MOD_CACHE_AUDIT.md`
32. `devReports/PR16_01_PER_SERVER_MOD_CACHE_IMPLEMENTATION_REPORT.md`
33. `devReports/PR16_01_PRECOMMIT_PREPARATION.md`
34. `devReports/PR16_01_Q1_Q4_INDEPENDENT_VERIFICATION.md`
35. `devReports/PR16_01_Q1_Q4_REMEDIATION.md`
36. `devReports/PR16_01_Q2_Q3_V1_INDEPENDENT_VERIFICATION.md`
37. `devReports/PR16_01_Q2_Q3_V1_REMEDIATION.md`
38. `devReports/PR16_01_QWEN_INDEPENDENT_AUDIT.md`
39. `devReports/PR16_01_STARTUP_BASELINE_AUDIT.md`
40. `devReports/PR16_01_STARTUP_INSTRUMENTATION_REPORT.md`
41. `devReports/PR16_01_SUGGESTION_CACHE_LOAD_AUDIT.md`
42. `devReports/PR16_01_SUGGESTION_CACHE_REPORT.md`
43. `devReports/PR16_01_SUGGESTION_CACHE_V2_REPORT.md`

All 43 proposed paths exist in the final worktree.

## Explicit exclusions

Exclude every path not listed above, including:

- `.qwen/`;
- `MEMORY.md`;
- `PKGBUILD`;
- `dzll-launcher-0.4.0-1-any.pkg.tar.zst`;
- `dzll-launcher-0.4.1-1-any.pkg.tar.zst`;
- `pkg/`;
- `release-v0.4.1-beta/`;
- `src/DayZ-Linux-Launcher-0.4.0-beta/`;
- `src/DayZ-Linux-Launcher-0.4.1-beta/`;
- `devReports/PythonCrash.txt` and every Discord diagnostic;
- every non-PR16-01 report under `devReports/`; and
- every other unrelated dirty or untracked file.

No staging command was run. The proposal is intentionally file-by-file so the
unrelated dirty and untracked baseline remains preserved.
