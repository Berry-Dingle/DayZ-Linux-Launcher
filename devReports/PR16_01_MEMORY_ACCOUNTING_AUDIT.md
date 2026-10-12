# PR16-01 — Steady-State Memory Accounting Audit

Date: 2026-10-12  
Scope: strict read-only audit of the current PR16-01 implementation and the
maintainer-supplied real-profile plateau.  
Observed profile: `DZLL_STARTUP_BATCHING=1`, `DZLL_A2S_WARMUP_500=1`, about
12,101 servers, RSS about 918.8 MiB and USS about 815.8 MiB from 20–45 minutes,
with no meaningful continuing idle growth.

## Executive conclusion

The plateau is explainable as a large but stable object graph rather than a
confirmed leak. The dominant retained data is not the SQLite database itself or
two `Gio.ListStore` copies. It is:

1. one `ServerObject` per server, including the raw per-server mod JSON;
2. one derived per-server mod-search index per server;
3. the decoded persistent per-server cache kept in memory alongside those
   restored indexes; and
4. a global autocomplete catalogue whose 435,823-key prefix map is much larger
   in memory than its 19.0 MiB on-disk JSON representation.

The audit confirmed one meaningful but bounded redundancy: warm per-server
cache records remain as JSON-shaped lists while each row also retains a
restored frozenset-based search index. The strings are shared between the two
representations, so this is not a second full copy of the cache. On the current
12,101-entry profile, eliminating the obsolete list containers is estimated to
save about **15–18 MiB of Python heap**, not hundreds of MiB.

The two list stores, `_obj_by_key`, and transient filter/sort lists retain
additional references to the same `ServerObject` instances; they do not clone
the row payloads. The active ColumnView virtualizes row widgets, so source
inspection does not support 12,101 simultaneously retained `ServerRowWidget`
trees.

The global suggestion index is the largest individually measured Python data
structure: **119.2 MiB deep size** in the current cache, of which the prefix map
accounts for **91.6 MiB**. This is deliberate lookup acceleration and semantic
duplication of mod names already present in row data, not an accidental second
server cache. It is nevertheless the best material memory-optimization target
after the confirmed per-server-cache redundancy, provided autocomplete latency
and cache compatibility are protected.

No startup row transaction, SQLite row list, deferred suggestion payload tuple,
A2S result buffer, or completed worker future was found intentionally retained
at steady state. A few small completed-startup sets/counters survive, but their
cost is immaterial. Nothing in the inspected lifecycle explains continued idle
growth, consistent with the supplied plateau.

## Evidence and method

### Reviewed PR16-01 evidence

The tracker keeps PR16-01 **IN PROGRESS / ADAPT** and explicitly leaves current
and settled RSS/USS acceptance open
(`devReports/PR16_IMPROVEMENTS_TRACKER.md:71-184`). The audit also reviewed the
startup baseline, GTK phase measurement, GTK batching experiment, deferred
suggestions, A2S 500/60 experiment, mod-index work, suggestion-cache v2 work,
per-server-cache implementation, final integration audit and pre-commit
preparation reports.

The prior integration audit already identified the relevant open question: the
batched transaction temporarily retains input rows and staged structures, while
the per-server cache deliberately remains in memory; settled ownership had not
been measured (`devReports/PR16_01_FINAL_INTEGRATION_AUDIT.md:97-104,136-166`).
The present audit addresses that gap with the current 12,101-row profile.

### Non-disruptive measurements

No DZLL process was running during this audit, so no process was attached,
paused or instrumented. The supplied 918.8 MiB RSS / 815.8 MiB USS plateau is
therefore the authoritative live result. Measurements below used short-lived,
read-only Python processes against the current database and caches. They did not
launch DZLL, write caches, alter configuration, use the network, or mutate Git.

Current persisted inputs:

| Input | Size / population |
|---|---:|
| SQLite server database | 23,924,736 bytes; 12,101 rows |
| Raw `mods` text in SQLite | 17,506,327 characters |
| Distinct raw `mods` payload values | 9,393 |
| Per-server derived-mod cache | 22,309,555 bytes; 12,101 records |
| Global suggestion cache | 19,953,012 bytes; 20,335 mod entries |

The short-lived probes used `tracemalloc`, `sys.getsizeof()` graph traversal and
`psutil.Process.memory_full_info()` as complementary views. `tracemalloc` covers
Python-managed allocations but not GTK/GObject/native allocations and itself
increases resident memory. Deep size counts reachable Python objects once by
identity but excludes native storage. RSS/USS deltas include allocator arenas
and native allocations and do not reliably fall when objects are freed.
Consequently, figures are labeled as measurements of the probes, not exact
decomposition of the live 815.8 MiB USS.

## Retained server data

### SQLite and intermediate records

`read_servers_from_db()` fetches all SQLite rows, converts each to a dictionary,
closes the connection and returns the list (`src/dzll_launcher/db.py:122-157`).
In an isolated probe:

| State | Traced Python current | USS |
|---|---:|---:|
| Probe baseline | negligible | 7.7 MiB |
| SQLite row objects | 24.9 MiB | 51.3 MiB |
| SQLite rows plus dictionaries | 30.4 MiB | 58.7 MiB |
| Dictionaries after SQLite rows dropped | 27.5 MiB | 59.3 MiB |

These records are a material startup peak but are not a steady-state owner. In
the batched path, `_startup_row_batch_state["rows"]` deliberately holds a tuple
of the input dictionaries until publication (`window.py:6097-6127`). Successful
publication sets `_startup_row_batch_state = None` (`window.py:6361-6363`). The
GLib completion callback and worker locals are one-shot. The 30-second watchdog
also returns `False` and becomes unreachable after it fires
(`window.py:5572-5684`).

**Finding:** no confirmed steady-state SQLite row/dictionary duplication. The
roughly 27–30 MiB Python payload and its allocator arenas contribute to startup
peak and may leave resident allocator pages, but the records themselves are
released.

### `ServerObject` and row payloads

Each accepted row becomes one `ServerObject` with 24 declared GObject
properties, including `name`, endpoint fields, raw `mods_json`, preview, map,
time and snapshot sort keys (`src/dzll_launcher/ui_row.py:437-487`). Additional
Python attributes retain lower-cased name/endpoint text, a combined search blob,
filter key, test-server flag and the per-server mod-search index
(`window.py:6232-6257` for batched; `window.py:6876-6923` for synchronous).

Raw mod JSON is therefore retained for join-time/refresh functionality even
after the DB dictionaries are released. Its source payload alone is about
16.7 MiB of characters before Python string headers, GObject string storage and
allocator rounding. This is required data under the current design, not proven
redundancy.

An isolated reconstruction using the real 12,101 rows, real warm per-server
cache, `ServerObject`s, sidecars and two actual `Gio.ListStore`s measured:

| Probe state | USS without `tracemalloc` |
|---|---:|
| Imports/baseline | 27.7 MiB |
| Decoded per-server cache plus DB dictionaries | 157.9 MiB |
| Steady server graph after DB dictionaries and staging list dropped | 289.7 MiB |

This is an approximation: it did not construct the full DZLL window or apply
live A2S values, but it used the actual GObject type and actual persisted data.
The approximately 262 MiB increase over its import baseline includes Python
row/index data, native GObject properties and allocator overhead.

### Stores, filtering and sorting

The default architecture creates a source `Gio.ListStore` and a separate
visible `Gio.ListStore`; the legacy `Gtk.FilterListModel` and
`Gtk.SortListModel` exist only behind the opt-in legacy switch
(`window.py:1213-1235`). A rebuild scans the source into a temporary Python
`rows` list, sorts it and splices those object references into the visible store
(`window.py:7121-7324`). `_obj_by_key` also maps endpoint strings to the same
objects (`window.py:6322-6329`).

The isolated probe found no measurable USS increase at MiB precision when the
first and second `Gio.ListStore` were populated after the objects already
existed. This is consistent with reference arrays rather than cloned objects.
At 12,101 entries, Python-reference storage is on the order of 0.1 MiB per
12,101-element array before container overhead. `_obj_by_key` and endpoint keys
cost more, but again reference existing rows.

The sorted `rows` list and sort-key tuples are transient to each rebuild. The
sort key function creates lower-cased name/IP strings and tuples during sorting
(`window.py:7212-7282`), then releases them when sorting returns. The display
store persists only the row references. Filter state stores `self.live` by
reference, not a copy (`window.py:9173-9243,9245-9292`).

ColumnView list-item factories create and bind widgets for GTK list items; the
source does not construct one `ServerRowWidget` for every database row. This is
reasonable virtualized GTK overhead rather than a 12,101-row widget-tree
duplication (`src/dzll_launcher/column_view.py:2328-2518`).

**Finding:** two stores and side maps are confirmed multiple owners of row
references, not duplicate row payloads. Temporary rebuild allocations can raise
peak RSS and leave allocator arenas, but they are not retained application data.

## Per-server derived-mod cache

The disk format stores endpoint -> `[digest, count, preview, ids,
normalized_names, compact_names, tokens]` (`server_mod_cache.py:20-31,41-59`).
Loading reads and validates a bounded byte buffer, decodes JSON, and returns the
JSON-shaped entry dictionary (`server_mod_cache.py:70-129`). The raw bytes and
document envelope are temporary.

For each warm row, `lookup()` converts the four string lists into frozensets and
returns a new search-index dictionary (`server_mod_cache.py:41-68`). That index
is attached to the `ServerObject`, but the original list-shaped record remains
in `_server_mod_cache_records`. The same original record is also referenced by
`_server_mod_cache_candidate` (`window.py:6173-6198,6243-6255`). After the
25-second write timer, `_server_mod_cache_records = dict(candidate)` creates a
new mapping while `_server_mod_cache_candidate` remains present
(`window.py:6647-6667`). `_server_mod_cache_persisted` separately maps endpoints
to digest references (`window.py:6075-6086,6699-6701`).

Current-profile measurements:

| Python graph | Deep size |
|---|---:|
| Loaded JSON-shaped records | 91.4 MiB |
| Restored lookup results considered alone | 166.5 MiB |
| Raw records and restored results together, identity-deduplicated | 184.5 MiB |

The combined graph is much less than 91.4 + 166.5 MiB because the restored
frozensets reuse the exact string objects decoded into the raw lists. The
measured incremental cost of retaining both forms is therefore about
**18.1 MiB**, dominated by the old lists and record containers. Direct shallow
accounting found about **14.2 MiB** in the four per-record field-list containers,
about **1.4 MiB** in outer seven-element record lists, and about **0.4 MiB** in
each 12,101-entry dictionary table.

**Confirmed redundancy:** the JSON-shaped list containers survive after their
contents have been restored into the row-owned search indexes. This is a real,
bounded optimization opportunity. It is not a second 91 MiB copy of strings.

**Reasonable overhead:** `_server_mod_cache_records` is intentionally retained
for later database reload reuse, and the candidate/persisted maps support safe
publication and repair decisions. The current representation, rather than the
existence of a reusable cache, causes the avoidable portion.

## Global mod suggestion catalogue

The final suggestion index retains:

- 20,335 mod-entry dictionaries and their name/normalization tuples;
- 435,823 prefix keys mapping to 906,723 entry-index references;
- 40,539 exact keys mapping to 40,950 entry-index references; and
- a bounded query-result cache of at most roughly 129 query keys in normal use.

The builder constructs prefix postings for every prefix up to 32 characters of
normalized names, compact names, tokens and workshop IDs
(`mod_suggestions.py:199-229,232-295`). Suggestion lookup unions these postings
before ranking candidates (`mod_suggestions.py:438-515`).

Measured deep sizes of the restored current index:

| Component | Deep size |
|---|---:|
| Complete suggestion index | **119.2 MiB** |
| `mods` catalogue | 23.1 MiB |
| `prefix_map` | **91.6 MiB** |
| `exact_compact_map` | 6.5 MiB |
| Initially empty result cache | negligible |

Component figures overlap slightly through shared objects and should not be
summed. The loader's isolated `tracemalloc` current value was also 119.2 MiB,
which independently agrees with the graph traversal. The 19.0 MiB file reached
a 201.8 MiB traced allocation peak while decoding/restoring, then settled at
119.2 MiB. Without `tracemalloc`, adding the suggestion index to the synthetic
server graph increased USS by about **167 MiB**. That USS delta includes Python
allocator/native overhead and is not a direct heap-size measurement.

The catalogue does repeat mod names/IDs that exist in raw row JSON and
per-server indexes, but it provides a different global aggregation and fast
prefix-to-candidate lookup. This is **reasonable architectural duplication**,
not accidental retention. Its current prefix representation is nonetheless the
largest measured memory lever.

Within the catalogue, 668,048 distinct string objects represented 464,223 text
values. Perfect local value deduplication has a measured shallow-string upper
bound of about **10.3 MiB**. A narrower confirmed case is 20,325
`normalized_name` strings and 20,325 `compact_name` strings equal to, but
distinct from, the first element of their corresponding tuple; aliasing those
fields would save at most about **2.2 MiB** of shallow string storage. These are
estimates and exclude changes in allocator fragmentation.

## Temporary startup ownership and callbacks

### Experimental batching path

During construction, `_startup_row_batch_state` simultaneously owns:

- a tuple of DB dictionaries;
- a growing list of staged GObjects;
- staged endpoint and live dictionaries;
- a shallow copy of all loaded cache records; and
- the cache candidate dictionary (`window.py:6094-6127`).

This intentionally raises the batching path's peak compared with direct
insertion. On successful publication the stores and long-lived sidecars assume
ownership and the entire state dictionary is cleared before post-load work
continues (`window.py:6313-6363`). Failure, cancellation and shutdown also clear
the state (`window.py:6508-6621`). No surviving steady-state transaction was
found.

### Deferred suggestions

The deferred scheduler creates one tuple of `mods_json` results and submits it
to the single update executor (`window.py:9623-9681`). On a hit, the worker
temporarily owns the 19 MiB raw cache bytes and decoded JSON while producing the
final index (`mod_suggestion_cache.py:136-281`). The future is captured only
until its one-shot GLib result callback runs; publication retains `result.index`
and scalar metadata, not the request tuple or decoded envelope
(`window.py:9683-9771`). No steady duplicate final index was found.

On refresh, a replacement index exists alongside the active index until GTK
publishes it (`window.py:9773-9852`). Migration serialization similarly retains
the active index while creating temporary JSON/bytes. Those are transient peak
costs, not normal 20–45 minute owners.

### A2S queues and callbacks

Both A2S modes enqueue the same full server-key set. The experiment only limits
the readiness cohort to 500 and changes its threshold to 60%
(`window.py:7607-7697`). The queue is drained, the result buffer is emptied and
in-flight futures fall to zero before full-sweep completion
(`window.py:7780-7928`). The completed experimental `_startup_warmup_keys` set of
up to 500 shared key references remains on the window, as do scalar counters.
This is unnecessary after completion but far below 0.1 MiB and not material to
the plateau.

Periodic GTK callbacks and signal handlers retain the window by design. The
review found no callback retaining a completed DB row list, staging transaction,
suggestion payload, replacement catalogue or full A2S result set after its
one-shot lifecycle ends.

## Python heap versus native/allocator memory

The closest non-GUI synthetic steady graph—real `ServerObject`s, real row data,
both actual list stores, all server side maps/caches and the real suggestion
catalogue—measured about **456.8 MiB USS above its 27.7 MiB import baseline**
without `tracemalloc` (479.0 MiB total USS). This does not include the full DZLL
window, GTK widget tree, CSS/theme/font resources, render caches, executors,
socket/runtime state, Companion state or the effects of a real A2S sweep.

Comparing that synthetic total with the supplied 815.8 MiB live USS leaves
roughly **337 MiB unassigned**, but it would be incorrect to label all of that
as GTK. It can include:

- native GTK/GObject widget, property and rendering allocations;
- PyGObject wrapper/native ownership not visible to `tracemalloc`;
- Python allocator arenas retained after DB decoding, cache restoration,
  sorting and A2S callbacks;
- thread stacks, loaded libraries, fonts, icons and CSS/theme caches;
- application subsystems omitted from the synthetic reconstruction; and
- differences caused by the probe not following the exact live interaction
  history.

Conversely, RSS exceeds USS by about 103 MiB in the supplied plateau, which is
consistent with shared libraries/mappings and shared pages. RSS should not be
used as if every page were uniquely attributable to DZLL.

Without a live process or an opt-in in-process heap snapshot, the exact
Python/native split cannot be measured. The available evidence does show that
retained Python-shaped server/cache/catalogue data is already several hundred
MiB and that native/allocator overhead is substantial. It does not support a
claim that GTK alone consumes the unexplained balance or that 815.8 MiB USS is
intrinsically unacceptable.

## Normal versus experimental steady state

| Area | Normal synchronous path | Experimental batching/deferred path | Settled difference |
|---|---|---|---|
| DB rows | Held through one synchronous callback | Held in batch-state tuple until publication | None after completion |
| Row objects and stores | Built/appended directly | Built in slices, then bulk-published | Same final rows/stores |
| Per-server cache | Loaded and retained | Loaded, copied in transaction, then retained | Transaction copy released |
| Suggestion catalogue | Restored synchronously before initial visible rebuild | Restored on worker after reveal | Same final catalogue |
| Filter/sort | Temporary scan/sort data | Same final publication work | Same retained visible store |
| A2S | Full queue; larger default warmup cohort | Full queue; 500-key readiness cohort | Up to 500-key retained set; immaterial |

The experimental route has a higher transient ownership peak because it stages
an atomic transaction and later overlaps a replacement catalogue with existing
rows. Once `_startup_row_batch_state` and the deferred future are gone, source
inspection shows no large experimental-only steady owner. Therefore, the
measured plateau should not be interpreted as proof that batching leaks its
startup snapshot.

## Ranked minimal fixes

These are ranked by confidence and expected benefit, not by an assumption that
the current plateau is unacceptable.

### 1. Memoize restored per-server cache records in canonical tuple form

When `lookup()` successfully restores a JSON-shaped record, replace that entry
in the loaded record mapping with `(digest, count, preview, mod_index)` and use
the same `mod_index` object for the row and future lookups. This removes the
obsolete field lists while preserving the strings and reusable cache.

- Confidence: **high; confirmed redundancy**.
- Estimated steady saving on the current profile: **15–18 MiB Python heap**.
- Secondary benefit: subsequent in-process reloads avoid rebuilding frozensets.
- Risk: low-to-medium; serialization already supports tuple records, but tests
  must cover mixed raw/restored records, unchanged-write detection, pruning,
  repair, reload reuse and cold/warm parity.

### 2. Compact global prefix posting lists, without removing the index

Retain the current lookup semantics but represent entry indices more compactly,
for example with validated 32-bit arrays or another packed immutable form. The
current prefix map alone is 91.6 MiB deep, with 906,723 Python integer
references plus tuple and dictionary overhead.

- Confidence: **medium; architectural overhead, not a leak**.
- Estimated potential saving: **roughly 25–40 MiB** if postings alone are packed;
  this is an engineering estimate, not a measured implementation result.
- Risk: medium; cache serialization/restoration, candidate union speed, ranking,
  aliases, exact/ID/prefix/token results and cold builder memory must be
  benchmark- and regression-checked.
- Do not replace indexed lookup with a full 20,335-entry scan without measuring
  autocomplete latency.

### 3. Deduplicate equal strings locally while restoring the suggestion index

Use a load-local string pool or explicit field aliasing, then discard the pool
after restoration. At minimum, alias scalar normalized/compact names to their
equal tuple element. Broader local deduplication should remain bounded to the
index and must not use an unbounded process-global intern pool.

- Confidence: **medium-high; equal-but-distinct strings measured**.
- Estimated potential saving: **about 2.2 MiB** for the two explicit scalar
  aliases; **up to about 10.3 MiB shallow string storage** for perfect local
  value deduplication. Actual USS saving will be smaller or allocator-dependent.
- Risk: low for immutable strings, but validate complete index equality and all
  suggestion query classes.

### 4. Release completed cache-publication and warmup sidecars

After the per-server cache write is definitively skipped or successfully
published, clear or alias `_server_mod_cache_candidate` rather than retaining a
second 12,101-entry mapping. After full A2S completion, clear
`_startup_warmup_keys` and zero any no-longer-diagnostic collection state.

- Confidence: **high but low value**.
- Estimated saving: about **0.4 MiB** for the candidate dictionary plus well
  under **0.1 MiB** for the 500-key set; payloads are already shared.
- Risk: low if retry/repair semantics and instrumentation are preserved.

### Not recommended from current evidence

- Removing one `Gio.ListStore`: the stores hold references, and the source store
  is the authoritative unfiltered population while the visible store owns the
  active order. There is no measured payload-sized duplication to justify a
  risky model rewrite.
- Dropping raw `mods_json` from rows without a replacement authority: current
  join-time and refresh paths need authoritative raw mod data.
- Disabling either cache: this would exchange retained memory for repeated JSON
  parsing, longer stalls and larger transient allocation without evidence of a
  better overall outcome.
- Forcing allocator trimming or manual garbage collection: the plateau does not
  show ongoing growth, and allocator policy changes would not remove live data.
- Changing experimental defaults: memory evidence is incomplete for matched
  normal/experimental live runs and the task forbids such a change.

## Recommended verification before any implementation

1. Add an opt-in, non-default diagnostic that records RSS/USS and, separately,
   Python traced current/peak at pre-row-build, staged peak, post-publication,
   post-suggestion-apply and five minutes settled. Do not enable `tracemalloc`
   in normal runs.
2. Repeat matched isolated-profile A/B runs for synchronous versus combined
   experimental startup using byte-identical DB/cache seeds.
3. For a per-server canonicalization patch, compare steady Python heap and USS
   after the 25-second write decision; expected improvement is only tens of MiB.
4. Prototype packed suggestion postings separately and measure autocomplete
   latency, cold-build peak, warm-restore peak and steady USS before adopting it.
5. If exact native attribution is still needed, capture `/proc/<pid>/smaps_rollup`
   plus a native allocation profiler on an explicitly disposable run. Do not
   attach invasive tooling to the user's existing application session.

## Disposition

**ADAPT; no urgent memory fix required.** The 20–45 minute plateau and lifecycle
review argue against a continuing idle leak. There is one confirmed 15–18 MiB
per-server-cache representation redundancy and a much larger, intentional global
prefix index that merits a compact-representation experiment. The remaining
footprint is a combination of required server/mod data, fast lookup structures,
GObject/GTK state and allocator/native overhead that cannot be separated exactly
without a live opt-in profile.

PR16-01 should remain **IN PROGRESS** until matched normal/experimental settled
RSS/USS and display-backed acceptance are complete. No application source,
tests, caches, configuration, experimental defaults, Git state, Discord work or
CSS work was modified. This report is the only file created by the audit, and no
mutating Git command was used.
