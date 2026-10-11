# PR16-01 — load-local mod-index optimisation experiment

Date: 2026-10-10  
Repository: `/home/gbrown/GithubRepos/DayZ-Linux-Launcher`  
Status: **IN PROGRESS**. Offline regression checks passed; a controlled DZLL runtime comparison has not been performed.

## Scope and baseline

Three supplied runs on 11,980 servers measured 1,591–1,620 ms for `mod_preview_index`, 1,485–1,503 ms for `global_mod_suggestion_index`, 4,123–4,153 ms for the GTK DB callback, and 4,193–4,264 ms for the main-loop stall. These runs establish a repeatable pre-change observation, not this experiment's improvement. The earlier source audit is `devReports/PR16_01_MOD_INDEX_AUDIT.md`. No DZLL/Steam launch, live query, dependency installation, or mutating Git command occurred during this experiment.

## Exact files changed for this experiment

| File | Change |
|---|---|
| `src/dzll_launcher/mod_parsed.py:1-32` | New load-local decoded-list holder and cleaned-name memo, keyed by parsed item identity and the consumer's Workshop ID argument. |
| `src/dzll_launcher/mod_search.py:100-148` | Optional parsed input for the existing per-server search-index builder; the original raw-JSON call remains available. |
| `src/dzll_launcher/mod_suggestions.py:232-294` | Optional parsed sequence for the existing global builder; its raw-JSON call and aggregation/ordering code remain available. |
| `src/dzll_launcher/window.py:508-544`, `5808-5948`, `8418-8435` | Optional parsed input for preview; decode once per accepted row, share it with search, and build global suggestions from the parsed contexts belonging to the final canonical endpoint map. Discard the sidecar in `finally`. |
| `tests/test_browser_server_ingestion_validation.py:105-210` | Differential and loader regression tests. |
| `devReports/PR16_IMPROVEMENTS_TRACKER.md:71-76`, `577-584` | Record the experiment and pending runtime verification; retain IN PROGRESS status. |
| `devReports/PR16_01_MOD_INDEX_OPTIMISATION_REPORT.md` | This report. |

The working tree already contained startup instrumentation changes in `window.py` and `app.py`, the untracked `startup_perf.py` and its tests, and other untracked reports/packages/release artifacts. This experiment preserved them. The existing `DZLL_PERF_LOG` phase names, boundaries and warmup logic remain intact (`window.py:5839-5847`, `5909-5918`, `5925-5950`).

## Execution and semantic boundaries

For each endpoint-accepted DB row, the loader accepts only string `mods`, as before (`window.py:5839-5841`). `ParsedServerMods.from_json()` decodes a nonempty string once, retaining a list or an empty list for malformed/non-list JSON (`mod_parsed.py:17-24`). Preview and per-server search then read the same list (`window.py:5842-5844`, `5911-5914`). The temporary mapping is keyed like `_obj_by_key`; later duplicate endpoints replace their earlier parsed context (`window.py:5921-5923`). Global suggestions iterate keys from the **final** canonical object map (`window.py:8419-8427`), so an overwritten endpoint's mods cannot contribute. The mapping is released after global construction (`window.py:5940-5944`). Empty full loads and later empty-index fallback calls use the existing raw builder path (`window.py:5798-5804`, `8428-8435`). No persistent decoded-mod cache or extra worker was added.

Each consumer retains its own ID and result rules. Preview retains original list order, duplicate names, first-eight truncation and `", ..."` marker (`window.py:508-544`), and the DB `modCount` override still applies after preview (`window.py:5845-5850`). Search retains its original ID, normalized/compact name and token sets and frozenset aliases (`mod_search.py:100-148`). Global indexing retains ID-first identity, per-server deduplication, name frequencies, canonical/display names, ordered entries and lookup maps (`mod_suggestions.py:232-294`). The shared name memo uses **both** item identity and the consumer-specific Workshop ID argument, preserving the distinct `0` behavior: preview/suggestions pass no ID, whereas search passes `"0"` (`mod_parsed.py:26-32`; `window.py:526-531`; `mod_search.py:123-134`; `mod_suggestions.py:243-249`). Raw `mods_json` remains on `ServerObject` (`window.py:5892`) for the row popover and authoritative join-time parsing.

For an accepted row with a nonempty JSON string, the normal full-load path now invokes the decoder once rather than once each for preview, search and global indexing. Empty strings need no decode. Name cleaning is reused only when the same parsed item and Workshop ID argument recur. Normalization, global aggregation, entry construction and prefix-map work still occur. Later explicit raw-path global rebuilds can decode again; this experiment targets startup full loads.

## Behavioural equivalence evidence

- The original one-argument public builders remain callable. The tests compare their outputs against the optional parsed path on empty, malformed, non-list, mixed-type, duplicate-name/ID, numeric-zero, ID-only, name-only, Unicode/control-character and greater-than-eight-name payloads (`tests/test_browser_server_ingestion_validation.py:109-137`). They compare complete global index dictionaries and `suggest_mods()` ranking results for several query tokens, plus preview with normal and zero-name limits.
- The full-load test spies on JSON decoding: three accepted rows, including two identical canonical endpoints, produce exactly three decodes; an invalid endpoint produces none. It verifies raw JSON, DB count override and a global `server_count` computed from the two final endpoints (`tests/test_browser_server_ingestion_validation.py:139-167`).
- The name-cleaning spy verifies reuse for an ordinary numeric ID and separate cleaning for the `0` consumer rule (`tests/test_browser_server_ingestion_validation.py:170-187`). Empty-load and replacement-load tests verify no stale sidecar and the existing fallback path (`tests/test_browser_server_ingestion_validation.py:190-210`).
- Existing mod search/name, suggestion, ingestion, popup, startup instrumentation and warmup tests were included in the offline run. No source evidence or test indicates a change to GTK model ownership, sorting, filtering, A2S sequencing, Steam, or Server Companion.

## Verification performed

`python -m pytest -q tests/test_browser_server_ingestion_validation.py tests/test_mod_name_normalization.py tests/test_mod_name_resolver.py tests/test_mod_metadata_bulk_names.py tests/test_server_mod_popup_sort_cleanup.py tests/test_startup_perf_instrumentation.py tests/test_startup_warmup_stable_live_list.py tests/test_core_startup_robustness.py`: **260 passed, 13 skipped**, one existing PyGObject deprecation warning. The skipped cases require display facilities; they are not executed coverage. Pytest's repository `conftest.py` isolated HOME/XDG before application imports. `python -m py_compile` for the five touched Python files succeeded. `git --no-pager diff --check` succeeded. Final diff and status were inspected. No Git-state-changing command was used.

## Residual risks and limits

The decoded lists and per-item cleaned-name memo live until global indexing completes. Peak memory may rise; no RSS measurement was taken. Each accepted row still builds independent search sets, and global suggestion aggregation, sorting and prefix-map creation still run on GTK. The unchanged `mod_preview_index` timing bucket now includes the single decode and memo lookups, while `global_mod_suggestion_index` excludes normal-path JSON decoding; direct before/after bucket comparisons must account for that work moving between phases. A rare name-cleaner exception could have different propagation because preview historically catches its own processing exceptions while search and global do not; normal JSON scalar/dict payload tests passed, but no claim is made for arbitrary monkeypatched cleaner failures. Current tests use synthetic payloads and detached hosts, not a production DB or live GTK display. Runtime latency and RSS benefit are **unverified**.

## Baseline comparison and next step

On the same 11,980-server DB snapshot, settings, hardware and launch conditions as the three supplied baselines, run at least three cold-start measurements with `DZLL_PERF_LOG=1`. Capture the same `mod_preview_index`, `global_mod_suggestion_index`, GTK DB callback, main-loop stall and overlay-dismissal events; compare median and range against the supplied baseline and inspect run-to-run variation. Measure peak RSS, since the load-local parsed sidecar can trade memory for CPU. Compare the combined mod buckets and GTK callback as well as individual buckets. Run the existing offline suite and, where available, a display-backed startup smoke check before accepting the experiment. If the combined GTK pause does not improve meaningfully or RSS becomes excessive, use aggregate subphase profiling (decode/cleaning versus aggregation/prefix-map construction) before further code changes. Keep PR16-01 **IN PROGRESS** until those runtime and regression checks complete.
