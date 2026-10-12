# PR16-01 — targeted memory optimisation results

Date: 2026-10-12

Status: Phase 1 retained; Phase 2 prototype reverted; PR16-01 remains IN PROGRESS

## Executive decision

| Phase | Decision | Measured outcome |
|---|---|---|
| Per-server cache canonicalisation | **RETAIN** | The current 12,101-record real cache lost **15,412,264 bytes (14.70 MiB)** from its reachable Python graph and **9,342,976 bytes (8.91 MiB)** from the median matched no-tracemalloc USS delta. Warm record lookup also became faster in the isolated probe. |
| Compact autocomplete prefix postings | **REVERT** | The strongest prototype removed **37,376,460 bytes (35.65 MiB)** of traced warm Python allocations, but warm-load USS increased about **16.7 MiB**, cold construction peak and USS increased materially, restoration slowed, and every representative uncached lookup class was slightly slower. The production suggestion implementation and disk format are unchanged. |

The retained change is deliberately narrow. It does not alter startup batching,
A2S warmup, global autocomplete, cache framing or atomic persistence, GTK/GObject
ownership, row refresh behavior, or join-time mod authority. Both experiments
remain exact-value opt-ins and default off.

## Inputs and measurement method

The probes read, but did not modify:

- `/home/gbrown/.cache/dzll/server_mods_v1.json`: 22,309,555 bytes and 12,101 records;
- `/home/gbrown/.cache/dzll/mod_suggestions_v1.json`: 19,953,012 bytes and 20,335 entries; and
- `/home/gbrown/.local/share/dzll/dzll-servers.db`: 12,101 server rows.

No DZLL process was started or attached to. Cache benchmarks ran in short-lived
processes. USS came from `psutil.Process().memory_full_info().uss`; Python current
and peak came from `tracemalloc`; reachable graph size used an identity-deduplicated
`sys.getsizeof()` traversal. The no-tracemalloc USS comparison used five fresh
processes per arm. Cold autocomplete construction used three fresh processes per
arm, with `tracemalloc` active in both arms. Latency used seven alternating
rounds of 250 uncached calls per query. Because the production type gate had
already been restored for the final latency run, the packed map was exposed
through a minimal `dict`-compatible adapter which delegates `get()` to the same
span/array implementation; those small latency differences should therefore be
treated as prototype-harness results, not sub-percent production predictions.

These are component probes, not a new measurement of the supplied live plateau
(RSS about 918.8 MiB, USS about 815.8 MiB). Python allocators do not necessarily
return freed arenas to the OS, `tracemalloc` materially changes allocation
behavior, and a short process does not reproduce the complete GTK/GObject heap.
Accordingly, Python graph savings and USS savings are reported separately.

## Phase 1 — per-server cache canonicalisation

### Implementation

`server_mod_cache.load()` intentionally continues to return the decoded
JSON-shaped record dictionary after framing, digest, schema and top-level bound
checks (`src/dzll_launcher/server_mod_cache.py:71-120`). This preserves lazy
per-record validation and does not reject the whole file because one record has
invalid nested data.

On a successful `lookup()`, the decoded seven-element list is now replaced in
that same dictionary with the canonical four-element tuple
`(digest, count, preview, mod_index)` (`server_mod_cache.py:59-68`). The returned
`mod_index` is the exact object stored in the tuple. A subsequent hit takes the
tuple fast path and returns that same index object. Invalid records, digest
mismatches and restoration failures are not canonicalised.

Both startup paths already use `server_mod_cache.lookup()` and attach the
returned object directly to `ServerObject.mod_search_index`; the batched path is
visible at `src/dzll_launcher/window.py:6169-6198,6243-6250`, and the normal path
at `window.py:6813-6824,6908`. Therefore the restored frozenset index used for
row construction is also the index retained for later cache lookups. The batched
state's `dict(records)` is shallow (`window.py:6097-6107`), so canonical tuple
members remain shared rather than deep-copied.

The serializer already accepts canonical tuples and emits the original sorted
JSON lists (`server_mod_cache.py:132-145`). Framing, exact-payload SHA-256,
bounded I/O, same-directory temporary write, fsync, atomic replacement and
directory fsync are unchanged (`server_mod_cache.py:148-188`). The delayed
writer's unchanged-write detection, generation/shutdown checks and failed
publication handling remain at `window.py:6647-6701`.

### Measured result

| Metric | Original | Canonicalised | Difference |
|---|---:|---:|---:|
| Reachable Python graph after all 12,101 hits | 183,364,743 B | 167,952,479 B | **-15,412,264 B (-14.70 MiB)** |
| Median no-tracemalloc USS delta | 197,152,768 B | 187,809,792 B | **-9,342,976 B (-8.91 MiB)** |
| Median full-cache lookup time | about 161.35 ms | about 124.08 ms | **about 23% faster** |
| Traced lookup peak | about 183.36 MiB | about 185.20 MiB | about **+1.84 MiB transient** |

The 14.70 MiB graph reduction is the direct removal of redundant outer record
lists and their four nested posting lists after successful restoration. Strings
were already shared with the restored frozensets, so this does not claim string
deduplication. The smaller 8.91 MiB USS result is the observed process effect;
allocator arena retention explains why it is lower than the graph reduction.
The short transient traced-peak increase occurs while one raw record and its new
canonical object coexist during replacement. It does not survive steady state.

This confirms the audit's predicted container redundancy and places the actual
Python graph saving just below its 15–18 MiB estimate. It does **not**
imply that the live application plateau must fall by 14.70 MiB; the matched USS
probe is the more conservative process-level expectation.

### Correctness evidence

The new regression at
`tests/test_browser_server_ingestion_validation.py:1225-1259` verifies:

- decoded raw records are lists before first access;
- one successful lookup replaces only the accessed record;
- the stored and returned mod-index objects are identical;
- a repeated lookup reuses the same object;
- raw and canonical records coexist safely in one mapping; and
- mixed records serialize to the unchanged format, reload as raw JSON lists and
  restore equivalently.

The adjacent existing ingestion/cache tests exercise exact-digest hits and
misses, cold rebuild parity, invalid/bounded nested records, corrupt and versioned
files, pruning only after authoritative loads, repair, reload, unchanged-write
avoidance, failed timers/submissions/publications, generation and shutdown
guards, and global-suggestion fallback on per-server hits. The maintained full
suite additionally covers join preparation and confirms that raw `mods_json`,
not this display/search cache, remains join authority.

## Phase 2 — compact autocomplete prefix postings

### Prototype evaluated

The real restored index contains 435,823 prefix keys with 906,723 posting
references, plus 40,539 exact keys with 40,950 references. Of the prefix keys,
373,889 are singletons. A first per-posting `array('I')`/singleton hybrid was
therefore ineffective: per-object overhead consumed nearly all expected benefit.

The stronger prototype used one packed unsigned-32-bit array per lookup map and
a dictionary from key to a packed `(start, length)` span. Values were validated
as non-negative indices below the 20,335-entry count before packing. Queries
returned read-only views of the shared array. Entry order, sorted posting order,
ranking, deduplication, aliases, token matching, compact matching and workshop-ID
lookup were left unchanged. The persistent v1/v2 JSON payload continued to use
ordinary integer lists, so existing files remained readable and writes remained
compatible; no second compatibility index was retained.

The temporary implementation passed the focused cache/ingestion regression set
(223 tests) and produced byte-for-byte equivalent suggestion results for the
representative queries below. It was then removed because its process-level and
latency results failed the retention criteria. There is no Phase 2 source or
test diff in the final tree.

### Warm restoration and steady state

| Metric | Tuple postings | Packed prototype | Difference |
|---|---:|---:|---:|
| Traced Python current | 119,154,683 B | 81,778,223 B | **-37,376,460 B (-35.65 MiB)** |
| Traced Python restoration peak | about 211,581,451 B | about 211,581,451 B | no meaningful change |
| No-tracemalloc USS delta, observed range | about 177.99–178.00 MB | about 195.51–195.64 MB | **about +16.7 MiB** |
| Median no-tracemalloc restoration | about 374 ms | about 428 ms | **about 14% slower** |

The disagreement between `tracemalloc` and USS is real evidence against assuming
that packed arrays are automatically better here. The prototype trades hundreds
of thousands of small tuple/int relationships for a native array plus 476,362
span integers in large dictionaries. `tracemalloc` reports fewer Python-owned
bytes, while allocator/native behavior produces a worse process footprint.

### Cold construction

All three runs built 20,335 entries, 435,823 prefix keys and 40,539 exact keys
from the same read-only SQLite `mods` column.

| Metric (median of 3) | Tuple postings | Packed prototype | Difference |
|---|---:|---:|---:|
| Build time | 3.879 s | 4.186 s | **+0.307 s (+7.9%)** |
| Traced Python current | 86,967,703 B | 77,029,247 B | **-9,938,456 B (-9.48 MiB)** |
| Traced Python peak | 244,448,585 B | 278,678,057 B | **+34,229,472 B (+32.64 MiB)** |
| USS delta | 433,303,552 B | 530,018,304 B | **+96,714,752 B (+92.23 MiB)** |

The prototype still needed mutable sets and sorting during construction, then
allocated packed spans and arrays. That conversion overlap explains the worse
cold peak. A more invasive direct compact builder might reduce overlap, but it
would require redesigning deduplication/construction and is not justified by the
worse warm USS and lookup results measured here.

### Representative uncached lookup latency

Each result is the median per call over seven alternating rounds; both arms had
their 128-entry result cache cleared before every call.

| Query class | Query | Results | Tuple postings | Packed prototype | Change |
|---|---|---:|---:|---:|---:|
| Workshop ID | `1559212036` | 1 | 62.1 us | 63.7 us | +2.7% |
| Short prefix | `co` | 8 | 4.770 ms | 4.839 ms | +1.5% |
| Name prefix | `community` | 8 | 0.566 ms | 0.571 ms | +0.8% |
| Token | `weapons` | 8 | 1.293 ms | 1.315 ms | +1.7% |
| Multiword | `dayz expansion` | 8 | 3.079 ms | 3.097 ms | +0.6% |
| Compact exact | `codelock` | 8 | 0.694 ms | 0.699 ms | +0.8% |
| Miss/fallback | `zzzz-no-such-mod` | 0 | 7.883 ms | 7.935 ms | +0.7% |

All returned tuples were equal. The regressions are small enough that interactive
autocomplete would probably remain acceptable, but there is no latency win to
offset the measured USS and cold-peak regressions. The recommendation is
therefore **REVERT**, not retain-on-theory.

## Validation of the retained tree

All test commands used isolated HOME and XDG directories under `/tmp`; no test
import or write used the real profile.

- Focused startup/cache suite:
  `python -m pytest -q -p no:cacheprovider tests/test_browser_server_ingestion_validation.py tests/test_startup_batching.py tests/test_deferred_mod_suggestions.py tests/test_mod_suggestion_cache.py`
  — **169 passed**, one existing PyGObject deprecation warning.
- Maintained full suite:
  `python -m pytest -q -p no:cacheprovider tests`
  — **2,791 passed, 26 skipped**, one existing PyGObject deprecation warning.
  The skips are display-dependent and are not executed coverage.
- `python -m py_compile src/dzll_launcher/server_mod_cache.py tests/test_browser_server_ingestion_validation.py`
  — passed.
- `git --no-pager diff --check` — passed.

No application cache was cleared, overwritten or migrated. No mutating Git
command was used. Nothing was staged or committed.

## Regressions and remaining risks

No functional regression was found in the retained Phase 1 tree. The main
remaining qualification is measurement scope: this task did not perform a new
interactive full-launch A/B run, so the 8.91 MiB component USS saving must not
be represented as a verified reduction from the 815.8 MiB live plateau.

Other bounded risks:

1. Canonicalisation is lazy. Only successfully accessed records are converted;
   invalid, stale or unused records remain raw until repair/pruning. Normal
   startup accesses every accepted server, so the real 12,101-server warm path
   canonicalises its live records.
2. A raw record and its canonical replacement coexist briefly during restore,
   creating the measured roughly 1.84 MiB aggregate traced peak difference.
3. The delayed write path deliberately takes shallow dictionary snapshots for
   worker/lifecycle safety (`window.py:6654-6665`). Those dictionaries share the
   canonical tuples and indexes; this change does not add deep duplication.
4. Display-backed live shutdown/reload and complete settled-process A/B memory
   remain part of PR16-01 acceptance. The full headless suite does not replace
   that evidence.

## Files changed and recommendation

- `src/dzll_launcher/server_mod_cache.py` — retain successful-restore
  canonicalisation and identity reuse.
- `tests/test_browser_server_ingestion_validation.py` — retain focused mixed
  raw/canonical, identity-reuse and disk-round-trip regression.
- `devReports/PR16_IMPROVEMENTS_TRACKER.md` — record the measured Phase 1 result,
  rejected Phase 2 prototype and continuing IN PROGRESS/default-off status.
- `devReports/PR16_01_MEMORY_OPTIMISATION_RESULTS.md` — this report.

**Phase 1 recommendation: RETAIN.** It removes confirmed redundant containers,
has conservative measurable USS benefit, preserves the disk contract, and does
not change join authority or startup architecture.

**Phase 2 recommendation: REVERT (already done).** Its apparent Python-object
saving did not translate into lower USS, and it worsened cold peak, restoration
time and all sampled uncached query latencies. Revisit only if a future design
can avoid the 435,823-key span-dictionary overhead and demonstrate lower real
process memory under the same data and lifecycle.

PR16-01 remains **IN PROGRESS**. Neither `DZLL_STARTUP_BATCHING` nor
`DZLL_A2S_WARMUP_500` has been enabled by default.
