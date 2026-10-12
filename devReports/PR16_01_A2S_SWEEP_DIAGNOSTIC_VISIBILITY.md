# PR16-01 A2S Sweep Diagnostic Visibility

## Cause

DZLL does not configure Python logging during normal startup. The module logger `dzll_launcher.window` has level `NOTSET` and inherits the root logger's default effective level of `WARNING`. Therefore the opt-in diagnostic's `logger.info()` calls were filtered out before any terminal handler or fallback could emit them. This explains the absence of even the initial sweep-start marker in the Fedora desktop run.

## Change

The existing `_log_a2s_sweep()` sink now preserves the same percent-formatted message text, writes it directly to `sys.stderr`, appends one newline, and flushes immediately. Formatting, write, and flush failures remain contained and cannot affect A2S scheduling or lifecycle behavior.

No sweep counters, completion conditions, concurrency, timeouts, warmup thresholds, generation handling, shutdown handling, lifecycle behavior, or experimental defaults were changed.

## Files Changed

- `src/dzll_launcher/window.py`
- `tests/test_a2s_sweep_diagnostics.py`
- `devReports/PR16_01_A2S_SWEEP_DIAGNOSTIC_VISIBILITY.md`

Pre-existing worktree changes were preserved. Nothing was staged or committed.

## Verification

- Focused diagnostic and startup regressions: `82 passed, 15 skipped, 1 warning`.
- Maintained full suite: `2799 passed, 26 skipped, 1 warning`.
- Default-`WARNING` regression confirms the diagnostic remains visible on `stderr` with exact formatting.
- Diagnostic stderr failure regression confirms sweep lifecycle completion is unaffected.
- `python -m py_compile src/dzll_launcher/window.py tests/test_a2s_sweep_diagnostics.py`: passed.
- `git --no-pager diff --check`: passed.

The warning was the existing PyGObject deprecation warning for `GLib.unix_signal_add_full`. The skips were existing display-dependent coverage.

No mutating Git commands were used.
