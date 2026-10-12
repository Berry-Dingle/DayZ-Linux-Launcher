# PR16-01 A2S Sweep Diagnostics

## Result

Added opt-in aggregate startup A2S sweep diagnostics controlled by the exact-value gate `DZLL_A2S_SWEEP_DIAGNOSTICS=1`. The diagnostic remains off by default.

The instrumentation reports:

- initial distinct eligible endpoint count;
- actual warmup-threshold attainment with completed and target counts;
- terminal elapsed time from sweep start;
- dispatched attempts, completed attempts, and successfully responsive endpoints as separate totals;
- remaining queued and in-flight work;
- terminal reason: `exhausted`, `cancelled`, or `superseded`.

`FULL A2S SWEEP COMPLETE` is emitted only for an exhausted sweep with no queued or in-flight work and with every dispatched attempt completed. Cancelled, superseded, or incomplete-dispatch states cannot emit that marker.

No A2S scheduling, concurrency, timeout, readiness policy, request count, or per-server logging was changed. Existing generation and shutdown invalidation remains authoritative. Diagnostic logging failures are contained so they cannot alter lifecycle behavior.

## Files Changed

- `src/dzll_launcher/window.py`
- `tests/test_a2s_sweep_diagnostics.py`
- `devReports/PR16_01_A2S_SWEEP_DIAGNOSTICS.md`

Pre-existing worktree changes, including earlier edits in `src/dzll_launcher/window.py`, were preserved. No unrelated files were modified by this task.

## Verification

- Focused startup/A2S tests: `81 passed, 15 skipped, 1 warning`.
- Maintained full suite: `2798 passed, 26 skipped, 1 warning`.
- `python -m py_compile src/dzll_launcher/window.py tests/test_a2s_sweep_diagnostics.py`: passed.
- `git --no-pager diff --check`: passed.

The warning was the existing PyGObject deprecation warning for `GLib.unix_signal_add_full`. Skips were existing display-dependent coverage.

No mutating Git commands were used. Nothing was staged or committed.
