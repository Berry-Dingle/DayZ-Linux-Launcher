# PR16-01 — GTK batching overruns and final-publication hitch follow-up

Date: 2026-10-12

Status: **targeted publication improvement retained; PR16-01 remains IN PROGRESS**

Recommendation: keep `DZLL_STARTUP_BATCHING` and `DZLL_A2S_WARMUP_500`
default-off. The final-publication hitch is materially smaller, but the 6 ms
batch budget is still exceeded and there is still no rendered-frame or physical
input-latency acceptance evidence.

## Outcome

The investigation demonstrated two independent causes:

1. **Batch overruns are cyclic-GC pauses inside individual row builds.** Normal
   warm-cache rows are not expensive: the isolated real-data probe measured a
   0.054 ms median and 0.074 ms p95 row. Automatic generation-1 collections
   interrupted individual `ServerObject` constructions for as much as 28.75 ms.
   With cyclic GC disabled only in the temporary component probe, the maximum
   row fell to 0.225 ms and the maximum simulated 6 ms batch fell from 31.30 ms
   to 6.06 ms. The identity of the slowest row moved between runs and did not
   correlate with mod payload size. This is a single interrupted row, not
   accumulated unchecked work or idle scheduling delay.
2. **The publication hitch is primarily GTK/native visible-model work.** The
   first visible `Gio.ListStore.splice` and its synchronous consumers cost
   193.6–214.2 ms in the matched baseline. Python filter scan cost 19.6–22.6 ms,
   Python sorting cost 20.4–24.0 ms, the all-row sort-key snapshot cost
   64.1–65.5 ms, and source-store publication cost only 5.4–6.9 ms. Replacing an
   already-populated visible store at A2S reveal cost only 2.9–3.4 ms, confirming
   that the large splice cost is a one-off first-publication/binding/layout path,
   not a recurring 12,101-row model cost.

The smallest safe improvement was to compute the already-required stable sort
keys while each `ServerObject` is constructed in the GTK idle batches, using one
captured timestamp for the complete transaction. This removes the 64–66 ms
all-row pass from the atomic publication callback without moving GObject work to
a worker, weakening atomic publication, changing data identity or adding a
second model.

Three matched before/after application runs reduced median instrumented final
publication from **310.4 ms to 252.1 ms** (-58.3 ms, -18.8%) and the median
externally sampled maximum main-loop gap from **426.7 ms to 356.7 ms** (-70.0
ms, -16.4%). The external wrapper around publication similarly fell from a
325.4 ms median to 267.9 ms. Batch maxima did not improve: the matched median
was 14.5 ms before and 15.9 ms after. The change therefore improves the
demonstrated publication hitch but does not claim to close the slice-budget
blocker.

## Scope and safety

The checkout was dirty before this work. Existing modifications to
`server_mod_cache.py`, its test, the tracker and all untracked reports/package
artifacts were preserved. No mutating Git command was used; nothing was staged,
committed, pushed, reset, checked out, stashed or cleaned. Discord IPC,
CSS/theme code, Steam behavior, cache formats and A2S policy were not changed.

Repository files changed by this follow-up:

- `src/dzll_launcher/window.py`
- `tests/test_startup_batching.py`
- `devReports/PR16_IMPROVEMENTS_TRACKER.md`
- `devReports/PR16_01_GTK_RESPONSIVENESS_FOLLOWUP.md`

Temporary measurement scripts and copied input profiles were kept under `/tmp`.
They did not modify the real profile.

Both experiment gates remain exact-value opt-ins at `window.py:307-311`.

## Inputs and method

Every component and application probe used the supplied 12,101-server inputs:

| Input | SHA-256 |
|---|---|
| `dzll-servers.db` | `68dfe4afadcedc38f6871c2293803d67b6a8ff5a362f9aed7a51cfae67774c4a` |
| `server_mods_v1.json` | `eff0d87203c98b22ede69a83527a2ee3f088c8bc155150fadbc2825d34a0bec3` |
| `mod_suggestions_v1.json` | `fda59c54f385e41cb32a450e9dfdcd4e8a3d4ccb98a59d919f431085220c0d88` |

Application runs used isolated profile copies, Python 3.14.8, GTK4, a
1440x900 Xvfb display, Cairo/software rendering, a successful local DB-fetch
boundary and deterministic 4 ms synthetic failed A2S replies. Update checks,
Discord and Server Companion were disabled only in the copies. Both startup
experiments were enabled for the batching measurements. A 5 ms GLib callback
sampled main-loop gaps. The same temporary method wrappers and inputs were used
before and after the source change.

The normal-path control from `PR16_01_FINAL_ACCEPTANCE.md` remains the relevant
comparison for whether batching is useful at all. This follow-up's matched
before/after pair isolates only the sort-key remediation inside the experimental
path.

## Phase 1 — forensic findings

### 1. Per-server GObject construction and assignment

`ServerObject` declares 26 GObject properties and assigns constructor keyword
arguments through Python `setattr` (`ui_row.py:437-487`). The batched builder
normalizes scalar values, restores the per-server mod cache, constructs the
GObject and attaches Python search/filter sidecars on the GTK thread
(`window.py:6145-6268`).

The warm-cache component probe processed all 12,101 rows with 12,101 hits and
zero misses:

| Metric | Normal automatic GC | GC disabled, diagnostic only |
|---|---:|---:|
| Row total | 774.0 ms | 672.8 ms |
| Mean row | 0.0640 ms | 0.0556 ms |
| p95 row | 0.0735 ms | 0.0698 ms |
| Maximum row | **28.91 ms** | **0.225 ms** |
| Simulated batch count | 122 | 114 |
| Maximum simulated batch | **31.30 ms** | **6.06 ms** |
| GC time | 89.16 ms | 0 |
| Maximum generation-1 GC | **28.75 ms** | 0 |

The slowest normal row attributed 28.89 of its 28.91 ms to the constructor
timer, exactly overlapping the 28.75 ms generation-1 GC. Other outliers moved
between object construction, cache restoration and scalar conversion because
the collector can run on any allocating Python operation. Rows with larger mod
JSON were fast when no collection occurred; the slowest identity changed on
repeat. There is no evidence of a pathological server record.

A temporary prototype that delegated all constructor keywords directly to the
native GObject constructor did not solve the problem. It produced a 43.25 ms
maximum row and a 48.08 ms maximum batch when a generation-2 collection ran.
That prototype was not retained.

### 2. Mod-search cache and sidecar work

The current cache was a 100% hit. In the representative component run,
aggregate fingerprinting cost 14.1 ms, record restoration 107.4 ms, scalar
conversion 49.2 ms and row-sidecar attachment 11.9 ms. There was no cold
`ParsedServerMods`/preview/index rebuild. These phases contribute ordinary
throughput but did not explain the tens-of-milliseconds row outliers.

Full application baselines similarly reported 100% hits and 237–250 ms for the
overlapping cache-load/apply envelope, including roughly 94–107 ms JSON decode,
about 14–15 ms fingerprinting and 101–106 ms record restoration. This work is
spread through setup and batches; it is not the final publication hitch.

### 3. Idle scheduling and deadline enforcement

The implementation sets `deadline = slice_start + 6 ms`, completes one whole
row, then checks the deadline (`window.py:6270-6290`). This is the correct
cooperative boundary: it cannot pre-empt Python or native work part-way through
a row. Consequently an automatic GC that begins within the row necessarily
overruns the batch.

Time between consecutive batch callbacks was small in the full baseline runs:
mean 0.16–0.20 ms and maximum 0.69–1.16 ms. The overrun is therefore batch
execution time, not delayed rescheduling. Most slices were close to the intended
boundary; baseline p95 callback duration was about 6.1–6.2 ms. The long tail is
GC.

Disabling GC, raising thresholds or forcing young-generation collections can
make the recorded row slices pass, but it defers/promotes work and can move a
larger collection into publication, A2S callbacks or post-overlay interaction.
The full probes also observed generation-2 collections around 80–86 ms outside
row construction. No lifecycle-safe policy was demonstrated that reduces total
GTK stalls rather than relabeling or postponing them, so no GC control was added.

### 4. Final source and visible publication

The transaction first splices the completed objects into the source store,
commits endpoint/live/cache sidecars, then rebuilds the visible store atomically
before A2S starts (`window.py:6324-6366`). The visible rebuild scans the source,
sorts matched objects and invokes `column_view_store.splice`; the call timer
includes synchronous GTK model consumers but not later drawing
(`window.py:7121-7458`).

Matched baseline phase ranges:

| Publication component | Range |
|---|---:|
| Source store and sidecar commit | 5.4–6.9 ms |
| Sort-key snapshot | 64.1–65.5 ms |
| Filter scan | 19.6–22.6 ms |
| Python sort | 20.4–24.0 ms |
| First visible-store splice/notifications | **193.6–214.2 ms** |
| Full initial visible rebuild | 238.7–259.2 ms |
| Full final publication | **310.4–332.0 ms** |

A signal timestamp placed after the ColumnView's already-connected model
consumer observed the source `items-changed` callback only 2.1–2.7 ms after
publication began, but the visible `items-changed` callback was not reached
until 309.7–331.3 ms. This independently agrees with the phase timers: Python
filter/sort plus synchronous work caused by the first visible model population
dominates; the source store does not.

### 5. Filter/sort rebuilding and one-off versus recurring cost

The first rebuild inserted 11,026 rows under the copied current settings. A2S
reveal later rescanned and resorted the same source, but replacing the already
populated visible store cost only 2.9–3.4 ms; the complete later rebuild cost
45.5–50.6 ms. The roughly 200 ms initial splice is therefore a startup-only
binding/layout/notification cost. Ordinary later Python filter/sort work remains
roughly 40–47 ms, and live browser updates are applied in separate callbacks.

Changing the visible-model identity or incrementally exposing partial rows might
reduce first-population work, but neither was proven compatible with atomic
publication, stable selection/model identity, all filter/sort paths and late
A2S mutations. That wider redesign was not adopted.

### 6. Frame clock and competing startup work

A2S submission begins only after publication (`window.py:7616-7706`), so it
cannot cause the measured build slices or final publication. In these synthetic
runs the 300/500 threshold was reached later and the warmup rebuild was separate.
The deferred suggestion worker ran off-main after reveal; its main-thread apply
was 0.02–0.05 ms. A later browser-live apply was about 36 ms and is recurring
post-startup activity, not part of the publication hitch.

The existing `initial_next_frame_tick` marker arrived 98–115 ms after initial
publication in baseline measurements. The additional Xvfb tick callback did not
produce usable rendered-frame samples. As documented in source, the existing
marker is a frame-clock tick, not proof that pixels were presented. No rendered
smoothness or physical input-latency claim is made.

## Phase 2 — retained remediation

The batched transaction now captures one `sort_snapshot_now_ts` at setup
(`window.py:6097-6129`). Each row derives the same `played`, `sort_ping`,
`sort_players` and `sort_played_days` values and initializes the existing
GObject properties during construction (`window.py:6228-6251`). Publication no
longer traverses all source objects solely to assign those keys
(`window.py:6324-6351`). A new aggregate `row_sort_key_snapshot` marker keeps the
work visible rather than hiding it (`window.py:6397-6412`).

This preserves:

- GTK/GObject main-thread ownership;
- one timestamp for every row, matching the prior all-row snapshot;
- atomic source/visible publication;
- the existing model objects and notification path;
- filtering, stable sort keys, favourites, Last Joined display and ordering;
- map choices, search, mod sidecars and autocomplete;
- A2S generation, late-update and reveal behavior;
- cancellation, rollback, failure and shutdown guards; and
- bounded staging memory and existing cache formats.

The parity test now compares all three sort-key properties between synchronous
and batched rows, including favourites, duplicate endpoint replacement and Last
Joined state (`tests/test_startup_batching.py:196-236`). Instrumentation coverage
requires the new row aggregate and rejects the removed publication component
(`tests/test_startup_batching.py:476-511`).

No GC policy or visible-model architecture change was retained. The 6 ms
constant was not changed and no instrumentation boundary was shortened to hide
work.

## Phase 3 — matched validation

### Before/after application benchmark

| Metric | Before, 3-run range (median) | After, 3-run range (median) | Median change |
|---|---:|---:|---:|
| Maximum recorded row slice | 11.1–22.8 ms (**14.5**) | 10.7–29.4 ms (**15.9**) | +1.4 ms; unresolved |
| Row-build wall interval | 876–909 ms (**880**) | 957–981 ms (**973**) | +93 ms |
| Final publication | 310.4–332.0 ms (**310.4**) | 239.0–261.6 ms (**252.1**) | **-58.3 ms (-18.8%)** |
| External publication wrapper | 324.7–347.3 ms (**325.4**) | 251.6–276.8 ms (**267.9**) | **-57.5 ms (-17.7%)** |
| Maximum sampled main-loop gap | 413.2–438.7 ms (**426.7**) | 338.1–365.0 ms (**356.7**) | **-70.0 ms (-16.4%)** |
| DB GTK completion | 1,358–1,389 ms (**1,378**) | 1,352–1,406 ms (**1,398**) | +20 ms (+1.5%) |
| Row-build HWM RSS | 467.4–486.5 MiB (**468.1**) | 466.8–468.2 MiB (**466.8**) | -1.3 MiB; noise-scale |

The trade-off is explicit: three existing GObject sort properties are now set
during each batch, so row-build wall time rose while the longest atomic GTK
callback fell materially. No per-row field or duplicate index was added; settled
memory shape is unchanged. The HWM comparison shows no material increase, but
it is not a replacement for the accepted 45-minute real-profile plateau.

The external gap is longer than the instrumented publication because it also
contains immediate GTK binding/layout/render preparation after the model call.
It improved in the same direction but remains a visible-risk several-hundred-ms
hitch.

### Correctness and regression validation

Focused command:

```text
python -m pytest -q tests/test_startup_batching.py \
  tests/test_startup_perf_instrumentation.py \
  tests/test_startup_warmup_stable_live_list.py \
  tests/test_live_ping_dampening.py tests/test_last_joined_calendar.py
```

Result: **86 passed**, 1 existing PyGObject deprecation warning.

Full isolated display-backed command:

```text
xvfb-run -a -s '-screen 0 1440x900x24' \
  python -m pytest -q -p no:cacheprovider tests
```

Result: **2,813 passed, 4 skipped**, 90 warnings, in 15.10 seconds. The four
skips are the same unrelated detached Bob/Server Companion fixture cases
documented by final acceptance. No test was suppressed, deselected or xfailed.

`compileall` passed for `src/dzll_launcher` and the modified test.
`git --no-pager diff --check` passed. The final scoped diff was inspected.

## Remaining blockers and disposition

PR16-01 remains **IN PROGRESS** and both switches remain default-off.

Production-default acceptance is still blocked by:

1. **Batch budget:** automatic GC still causes individual rows and batches to
   exceed 6 ms. The cause is precise, but no retained mitigation has yet proven
   that it removes rather than moves pauses while preserving memory and
   lifecycle behavior.
2. **Residual publication/render hitch:** publication is improved to a 252 ms
   median, but first visible-store notification/binding remains about 193–213 ms
   and the sampled maximum main-loop gap remains about 357 ms.
3. **Rendered interaction evidence:** no compositor-presented frame cadence,
   spinner continuity or physical pointer/typing latency was captured.
4. **Live A2S acceptance:** this task used deterministic synthetic replies to
   isolate GTK. It does not supply the still-missing completed matched live A2S
   sweeps or shutdown/reload interaction evidence.

The retained remediation is safe and measurably improves the demonstrated
atomic hitch, but it does not justify enabling either experiment by default.
