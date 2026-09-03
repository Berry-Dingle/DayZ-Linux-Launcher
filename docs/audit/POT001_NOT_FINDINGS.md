# POT-001 Not-Findings / Verified Design Context

Historical audit context from the Server Companion POT-001 review.

These entries document behaviours that were investigated and considered safe at the
time of the audit. They are not immutable specifications: later independent evidence
may supersede an individual conclusion.

Current production source and explicitly documented product invariants take precedence.

## Current product invariant (verified after this historical audit)

An event may contribute positive or negative schedule-learning evidence only when
`coverage_complete=True` and `lifecycle_interruption is None`. Diagnostic event
classification is separate from learning eligibility. Incomplete or lifecycle-
interrupted events may remain classified and persisted and may conservatively block
unsupported inference across uncertain intervals; that blocking is not negative
evidence.

The shared `event_learning_eligible()` policy now governs direct scoring, weak hints,
continuity endpoints, expected-window HIT qualification, authority event evidence,
runtime independent-authentic-event counts, and downstream consumer eligibility.
The implementation and regressions are recorded in commit `d3fad33` (`Enforce
Companion learning eligibility`). A diagnostically `CONFIRMED_OFFLINE_RESTART` event
can therefore remain learning-ineligible when observation continuity was interrupted.

The query-visible positive control remains intentional: an
`AMBIGUOUS_DRAIN` qualified by `query_visible_repopulation_timeout` may provide its
existing deliberately weak hint evidence when it has complete coverage and no
lifecycle interruption. The rule is continuity/eligibility-based, not outcome-name
based.

# POT-001 Report 07 — Not-Findings (Suspicious Areas Examined and Proven Correct)

Behaviors that looked suspect during the audit and were verified safe, with evidence. These should not be re-litigated without new evidence.

## NF-01 — Harmonic double-counting (one restart feeding several candidates)
**Suspicion:** a 4h restart interval could produce learning for 2h (k=2 multiple) or 8h.
**Proven safe:** covered empty divisor midpoints become misses for the shorter candidate and resolution evidence for the longer one; multiples require intermediate-window classification and reject covered misses. SIM02: 8×4h restarts → 4h 0.92 selected, 2h 0.000 with 13 misses, 8h 0. Tests: divisor-resolution suites in scoring/authority test files.

## NF-02 — UI "next_text" showing raw epoch seconds under schema-4
**Suspicion:** `authority_consumer_summary` returns `next_text = str(int(prediction))` while schema-3 formatted "%a %H:%M".
**Proven safe:** `server_companion_ui.py:119-131` prefers the numeric `next_restart_at` and formats via `format_restart_local_time`; raw `next_text` is only a fallback when no numeric occurrence exists. No user-visible epoch string.

## NF-03 — User-visible 95% confidence cap differing from internal gates
**Suspicion:** producer/consumer confidence mismatch.
**Proven safe:** the cap is display-only (`MAX_USER_VISIBLE_RESTART_CONFIDENCE_PERCENT = 95`), documented in the 2026-08-14 report as changing no internal threshold/gate. Warnings gate on structural flags, not the displayed number. Intentional.

## NF-04 — Query-visible-only schedules can never reach H2+ authority
**Suspicion:** producer allows QV learning but consumers can never warn on pure QV schedules — apparent inconsistency with the "crowbar" feature.
**Proven safe/intended:** design §11.2 explicitly restricts high authority to offline-grade endpoints ("Strong query-visible events remain normal evidence unless product policy later defines an equally strong direct-observation contract"). The 2026-08-14 normal-established path intentionally lets maximum-confidence normal/QV schedules warn (≥0.95 schedule confidence, ≥3 strict relationships, phase ≥0.85, no unresolved competitors/divisors) while withholding outage-threshold relaxation and scheduled classification. Coherent product policy, both paths tested.

## NF-05 — Old-schedule events suppressing a new schedule's phase confidence for days
**Suspicion:** after 4h→6h change, 6h phase_conf stayed 0.641 after 9 clean cycles → predictions withheld ~7 days.
**Proven conservative-by-design:** `_recent_events` retains qualifying events within 7 days/16 events (documented horizon); consistency divides by all retained authentic events. Design keeps the conservative normal tier unchanged and routes fast transition through the high-authority challenger machinery (which suspends predictions during transition anyway). Not a defect; noted in Report 02 §5.

## NF-06 — Degenerate conditional in `_transition_decision` final else
**Suspicion:** `state = ESTABLISHED if prior is None or prior.state not in {...} else ESTABLISHED` — both arms identical; possible lost transition-labeling logic.
**Proven harmless:** design §18 specifies NEW_REGIME_ESTABLISHED collapses to ESTABLISHED after one stable evaluation — the unconditional result is exactly the specified behavior. Cosmetic remnant only; flagged as POT1-QWEN-005-adjacent cleanup (not escalated separately).

## NF-07 — `_append_bounded` keeping element[0] instead of dropping oldest
**Suspicion:** off-by-one in bounded list trimming could lose or bias evidence.
**Proven intentional:** keeps first + newest (limit−1); the first element is used for duration measurement (e.g. `_has_confirmed_visible_low` elapsed checks). Stability checks use only the newest entries. No bias.

## NF-08 — Fired-key trimming is alphabetical, not chronological
**Suspicion:** `mark_fired` trims by `sorted(..., key=serialize)[-N:]`, possibly dropping still-relevant keys.
**Proven harmless:** keys embed unique event ids / regime generations / occurrence timestamps; dropped old keys cannot match future decisions in practice, and realistic concurrent-relevant key counts are far below the 200 cap.

## NF-09 — Legacy v1 expected misses imported with active penalties without v2 proof
**Suspicion:** migration could perpetuate incorrect v1 misses (v2 continuity unprovable → UNKNOWN, no retraction).
**Proven conservative:** v1 misses were themselves recorded only for fully-covered, query-healthy windows (runtime guard), so an imported miss represents a real covered-empty window at v1 time; keeping the penalty when v2 cannot re-prove continuity errs in the anti-false-positive direction for predictions. Retraction fires whenever v2 reclassification yields HIT/observed-outage. Tests: `test_retracted_miss_stays_inactive_after_reload`.

## NF-10 — Unfilled healthy↔failure transition edges in runtime coverage
**Suspicion:** coverage holes at outage boundaries could break full-coverage proofs or, worse, count as monitored time.
**Proven safe:** `CoverageTimeline.assess` tolerates them as small cadence gaps (≤30s online / ≤12s offline) with ratio accounting; high-tier continuity requires explicit transition spans reconstructed from event samples (`observed_transition_spans_from_events`) — the documented fix for design defect 9.2. Windows containing real outages classify HIT/AMBIGUOUS via events before coverage is consulted.

## NF-11 — Wall-clock backwards jumps / NTP corrections
**Suspicion:** wall-time anomalies could fabricate SLEEP_GAPs or hide gaps.
**Proven conservative:** `_extend_coverage` emits no segment when `wall_at <= previous.wall_at` (hole, treated as unmonitored); SLEEP_GAP detection uses elapsed wall time against cadence-derived thresholds (30s online / 12s offline) — a wall jump only widens a gap label, never manufactures monitored evidence. Detection itself runs on monotonic time.

## NF-12 — Pending strike loss across crash (first failure only)
**Suspicion:** crash after the first qualifying failure could lose or duplicate the outage.
**Proven safe:** pending strikes are intentionally session-local; after reload a fresh two-strike pair confirms the ongoing outage with phase lag ≤ one poll cycle (SIM04 scenario A). No duplicate identity possible (no episode existed yet).

## NF-13 — Schema-3 fallback path "fire-then-record" alert ordering vs schema-4 "reserve-then-fire"
**Suspicion:** crash between alert and `mark_fired` under the fallback could duplicate alerts after restart.
**Proven acceptable:** the fallback is only reachable when the schema-4 backend is unavailable; the duplicate window is the 60s warning window or a single recovery alert, and playback-success gating already prevents marking failed alerts. Production (cutover enabled, backend valid) uses reserve-before-fire exclusively. Recorded for completeness, not escalated.

## NF-14 — Learning-transfer imports as an evidence-injection vector
**Suspicion:** importing another machine's learning database could inject fabricated events and drive predictions.
**Proven bounded/by-design:** imports require full strict schema-4 validation, canonical round-trip stability, hash-pinned staged pending pairs, locked atomic apply with rollback, and explicit user confirmation UI; the feature is a documented "replace learning database" operation (trust boundary accepted by the user action). No learning-math defect; noted as a security design boundary.

## NF-15 — Two-strike window resetting when failures are spaced >25s
**Suspicion:** slow failure cadence could prevent outage confirmation.
**Proven intentional:** the pending-strike lifetime (2.5× online poll interval) distinguishes correlated failures from unrelated single failures; a reset simply re-arms on the newer failure (SIM-verified in detection flow; tests `test_weak_recovery_then_independent_outage_uses_new_failure_clock`, `test_pending_strike_expires_during_long_neutral_protocol_sequence`). Conservative by design.

## NF-16 — Reload changing regime-transition alternation state
**Suspicion:** live shadow prior starts empty after restart → TRANSITION_CONFIRMED↔NEW_REGIME_PROVISIONAL alternation could reset.
**Proven safe in production:** the authoritative consumer path sources the prior decision from the persisted schema-4 record (`rebuild_schema4_server_record` passes `prior = persisted decision`; consumers read backend authority when valid). The in-memory shadow is diagnostic/fallback only. Reload parity for summaries verified by `test_real_schema4_confirmed_offline_summary_is_safe_ephemeral_and_reloadable`.

## NF-17 — AMBIGUOUS_DRAIN events carrying small hint weight
**Suspicion:** ambiguous events could leak learning weight.
**Historical audit conclusion (superseded):** the audit verified the intended weak-hint
contract and found no path to direct support in the examined paths. Later verification
found lifecycle/incomplete-event bypasses in weak-hint collection, expected-window
qualification, authority evidence, and runtime consumer counts. Commit `d3fad33`
applies the shared eligibility policy at those learning boundaries.

The durable current behavior is documented above: complete, non-interrupted
query-visible repopulation-timeout evidence may retain its existing weak hint weight;
incomplete or lifecycle-interrupted events are learning-neutral while remaining
available for diagnostic and uncertainty-blocking purposes.

## NF-18 — Cooldown machinery gating nothing
Examined as a possible behavioral gap; proven inert but harmless (see POT1-QWEN-005, CLEANUP only).
