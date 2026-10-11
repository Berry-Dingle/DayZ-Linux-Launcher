# PR16-01 — A2S Startup Regression Investigation

Date: 2026-10-11  
Status: **IN PROGRESS**

## Conclusion

The reported no-window state is real in the two retained runtime logs, but its exact cause cannot be proven from the current source or available runtime evidence. The logs contain only:

```text
[PERF] lightweight server rows enabled
[STARTUP-PERF] phase=activation ...
```

They contain neither `window_construction`, `window_construction_failed` nor a Python traceback. Both affected logs were preserved under `/tmp/dzll-startup-baseline/` and were not modified.

Current source does not reproduce the failure. A real GTK window was constructed and presented under an isolated Xvfb display with both `DZLL_A2S_WARMUP_500=1` and performance logging enabled. A second display-backed test injected an exception late in `DZLLWindow.__init__`; activation emitted `window_construction_failed`, cleaned the partial window and presented the fatal startup window.

No A2S warmup code can execute between the observed `activation` marker and normal completion of `DZLLWindow.__init__`. The latest A2S patch added three scalar/set state initializations to the constructor, and those initializations completed in the real GTK test. The cohort selection, threshold, timeout and result code runs only later, after database application. Removing or changing the intended 500/60 implementation would therefore be speculative.

No production correction was made. The smallest evidence-backed change is display-backed regression coverage of the exact opt-in/performance activation configuration and its constructor-failure boundary.

## Activation and construction control flow

`DZLLApp.do_activate()` follows this order:

1. Re-present an already owned window, if one exists.
2. Stop or re-present the fatal startup window after a prior failure.
3. When `DZLL_PERF_LOG=1`, create the startup origin and emit `activation`.
4. Snapshot `self.get_windows()`.
5. Enter a `try` around `DZLLWindow(self)`.
6. On an ordinary Python `Exception`, emit `window_construction_failed`, clean newly registered partial windows, and try to present a fatal startup window.
7. On success, emit `window_construction`, assign `self.window`, call `present()`, and emit `window_present`.

The only operation between `activation` and the constructor `try` is `tuple(self.get_windows())`. The A2S patch did not change this code or application registration.

`DZLLWindow.__init__()` performs GTK window construction, local state loading, executor creation, model/UI construction, startup coordinator setup, live-refresh timer setup and finally queues `_begin_startup_update_from_idle()`. It does not run the startup A2S queue. The database update is queued for a later GTK idle callback; only a successful later database application reaches `_finish_db_rows_application()` and `_submit_startup_rest_batches()`.

There is no nested `GLib.MainLoop.run()`, blocking wait, thread join or sleep in the constructor. An `ep_poll` sample establishes that the sampled thread was waiting in a GLib poll at that moment; it does not identify which callback returned, failed or was never scheduled.

## Review of the latest A2S changes

### Constructor-visible changes

The patch added module constants read at import:

- `A2S_WARMUP_500_ENABLED`;
- `_A2S_WARMUP_EXPERIMENT_MAX = 500`;
- `_A2S_WARMUP_EXPERIMENT_READY_PERCENT = 60`.

It also initializes these ordinary Python attributes alongside the pre-existing warmup state:

- `_startup_warmup_mode = "default"`;
- `_startup_warmup_cohort_count = 0`;
- `_startup_warmup_ready_percent = 65`.

The imported default is the existing integer `STARTUP_WARMUP_READY_PERCENT = 65`. These assignments do not call GTK, schedule work, acquire locks, inspect the database or depend on user profile data. They completed in the display-backed constructor and activation tests.

### Post-construction changes

The remaining A2S changes are reached only through successful database application:

- record the list-publication timestamp when performance logging is enabled;
- select default or experimental warmup keys in `_submit_startup_rest_batches()`;
- use `ceil(60%)` for up to 500 distinct eligible endpoints in the experimental initial-startup path;
- carry an optional `reveal_reason` through `_complete_startup_warmup()`;
- add aggregate performance fields to warmup and full-sweep markers.

They cannot prevent initial window construction or `present()` because the app calls `present()` before `_begin_startup_update_from_idle()` can begin the database/A2S path.

### Call-site and signature check

`_complete_startup_warmup(generation, reveal_reason=None)` remains compatible with existing one-argument callers and tests. Current callers pass either the existing generation alone or a named `reveal_reason`. Timeout, threshold, empty-cohort and full-sweep paths all use a valid current generation.

`_submit_startup_rest_batches(keys, warmup_count=...)` retains its existing public shape. `_finish_db_rows_application()` still supplies the stable coarse-band queue and count of `bm_rank <= 1000` keys. No constructor or activation code calls either method.

The shutdown adjustment calls the existing `_cancel_startup_row_batches()` through the class so partial test doubles do not need a bound method. It runs only during cleanup, not during construction. Its body uses guarded `getattr()` state, removes an idle source when present and does not wait for a worker.

## Exception behavior and the missing marker

An ordinary Python exception raised by `DZLLWindow.__init__()` is caught by `DZLLApp.do_activate()`. With performance logging enabled, `window_construction_failed` is emitted before partial-window cleanup or fatal-window construction. The new injected-failure regression test confirms this ordering against real GTK.

Therefore, an ordinary constructor exception in the current A2S initialization does not match the retained logs: those logs would contain `window_construction_failed`. A failure before the `try`, a non-`Exception` control-flow event, process output loss, a different imported source tree, or an external GTK/session condition could produce different evidence, but none is established by the available logs. Selecting one would be speculation.

The absence of `window_construction` also means the A2S scheduler had not been reached according to current control flow. The observed timing correlation with the A2S patch does not establish that its later queue logic caused the failure.

The lack of normal Ctrl+C termination is also not sufficient to locate the fault. Current application code has no dedicated SIGINT diagnostic marker, and the supplied process-state observation contains no Python stack or exception record.

## Why existing tests did not settle this report

The A2S experiment tests use lightweight `SimpleNamespace` hosts and invoke scheduler methods directly. This is appropriate for deterministic queue, threshold, generation and timeout coverage, but it does not construct a `Gtk.ApplicationWindow` or execute `DZLLApp.do_activate()`.

The repository already has real-window startup tests in `tests/test_core_startup_robustness.py`. In the ordinary headless maintained suite they skip when `Gdk.Display.get_default()` is unavailable. Before this investigation, no display-backed test combined all three relevant conditions:

- real `DZLLApp.do_activate()` and `DZLLWindow.__init__()`;
- `DZLL_A2S_WARMUP_500=1`;
- performance-enabled activation markers.

The new tests close that coverage gap. They still use isolated HOME/XDG paths, disable the network startup updater, and replace desktop-integration writes. They exercise real GTK construction, presentation, partial-window cleanup and fatal-window presentation without launching DZLL as an application or accessing the network.

## Exact changes

| File | Change |
|---|---|
| `tests/test_core_startup_robustness.py` | Add a real GTK activation success test with A2S 500/60 and performance logging enabled, plus an injected constructor-failure test that requires `window_construction_failed` and a visible fatal window. |
| `devReports/PR16_01_A2S_STARTUP_REGRESSION.md` | Record control flow, source evidence, reproduction result, limitations and validation. |

No production source, A2S scheduling, GTK batching, deferred-suggestion behavior, threshold, cache, tracker, Forge or `MEMORY.md` file was changed by this investigation. PR16-01 remains **IN PROGRESS**.

## Regression verification

Display-backed activation tests were run under isolated Xvfb with software rendering:

```text
GDK_BACKEND=x11 GSK_RENDERER=cairo LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python -m pytest -q tests/test_core_startup_robustness.py::test_a2s_warmup_opt_in_perf_activation_constructs_and_presents_real_window tests/test_core_startup_robustness.py::test_a2s_warmup_opt_in_perf_activation_reports_constructor_failure
2 passed, 2 warnings
```

Focused headless startup tests:

```text
python -m pytest -q tests/test_a2s_warmup_500.py tests/test_startup_warmup_stable_live_list.py tests/test_startup_perf_instrumentation.py tests/test_core_startup_robustness.py
59 passed, 15 skipped, 1 warning
```

The 15 focused skips are display-dependent tests; the two new tests were separately executed under Xvfb above.

Maintained offline suite:

```text
python -m pytest -q tests
2768 passed, 26 skipped, 1 warning
```

The two additional skips are the new display-backed cases in the normal headless suite. All 26 skips require unavailable display-backed execution and are not counted as executed coverage.

Compilation and whitespace validation:

```text
PYTHONPYCACHEPREFIX=/tmp/dzll-pr16-a2s-regression-pycache python -m py_compile src/dzll_launcher/app.py src/dzll_launcher/window.py tests/test_core_startup_robustness.py tests/test_a2s_warmup_500.py
git --no-pager diff --check
```

Both passed with no output. The warnings are the existing PyGObject `GLib.unix_signal_add_full` and GTK CSS API deprecation warnings.

## Evidence required if the failure recurs

The next occurrence needs evidence from the interval after `activation` and before constructor success/failure. Preserve combined stdout/stderr, confirm the imported `dzll_launcher.app` and `dzll_launcher.window` file paths, and capture a Python-aware main-thread stack before termination. Those observations can distinguish an exception before the constructor `try`, execution from a different source tree, an environment/display problem, and a constructor call blocked below Python.

Until such evidence exists, there is no verified production defect to correct and no basis for rolling back the intended A2S 500/60 behavior.
