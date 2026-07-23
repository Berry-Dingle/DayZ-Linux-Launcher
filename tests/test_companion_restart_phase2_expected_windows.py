from __future__ import annotations

import inspect
from dataclasses import asdict, FrozenInstanceError

import pytest

from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_900_000_000.0
SERVER = "expected.example:2302"
APP = "app-v2"
MONITOR = "monitor-v2"
POLL = 9
PERIOD = 3 * scoring.HOUR
EXPECTED = BASE + 9 * 60
WINDOW_START = BASE
WINDOW_END = BASE + 18 * 60
MODEL = "model-revision-v2"


def chain_id(monitor=MONITOR, started=BASE - 60):
    return continuity.deterministic_continuity_chain_id(
        SERVER, APP, monitor, POLL, started
    )


def span(
    start=WINDOW_START,
    end=WINDOW_END,
    *,
    kind=continuity.ContinuitySpanKind.ONLINE_HEALTHY,
    reference="span",
    chain=None,
    monitor=MONITOR,
    provenance=continuity.CONTINUITY_PROVENANCE_VERSION,
    cadence=10.0,
):
    return continuity.ContinuitySpan(
        provenance_version=provenance,
        reference_id=reference,
        server_key=SERVER,
        app_session_id=APP if provenance else None,
        monitoring_session_id=monitor if provenance else None,
        poll_generation=POLL if provenance else None,
        continuity_chain_id=(chain or chain_id(monitor)) if provenance else None,
        start_at=start,
        end_at=end,
        start_monotonic=start - BASE,
        end_monotonic=end - BASE,
        kind=kind,
        cadence_seconds=cadence,
        reason_codes=(f"fixture_{kind.value}",),
    )


def event(
    event_id="restart-1",
    *,
    at=EXPECTED,
    outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
    authenticity=0.85,
):
    qualifying = outcome in {
        detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    }
    return detection.PhysicalRestartEvent(
        event_id=event_id,
        sequence=1,
        fingerprint=f"fingerprint-{event_id}",
        server_key=SERVER,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=3,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity if qualifying else 0,
        drain=detection.DrainSummary(
            None, None, None, None, False, None, None, 0, 0, False
        ),
        outage=detection.OutageSummary(
            at - 70,
            at - 60 if qualifying else None,
            3,
            (detection.InfoStatus.TIMEOUT,),
            at,
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("expected_window_fixture",),
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=chain_id(),
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def classify(*, period=PERIOD, events=(), spans=None, episodes=(), expected=EXPECTED):
    if spans is None:
        spans = (span(),)
    return windows.classify_expected_window(
        server_key=SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - 9 * 60,
        expected_at=expected,
        window_end_at=expected + 9 * 60,
        model_revision_id=MODEL,
        events=events,
        spans=spans,
        episodes=episodes,
    )


def legacy_miss(*, period=PERIOD, expected=EXPECTED, key="legacy-miss"):
    return windows.import_legacy_expected_miss(
        server_key=SERVER,
        candidate_period_seconds=period,
        expected_phase_offset=expected % period,
        window_start_at=expected - 9 * 60,
        expected_at=expected,
        window_end_at=expected + 9 * 60,
        model_revision_id=MODEL,
        legacy_key=key,
    )


def reconcile(records, classifications):
    return windows.reconcile_expected_window_ledger(
        records,
        classifications,
        reconciled_at=BASE + scoring.HOUR,
        evidence_revision_id="evidence-snapshot-1",
    )


def test_qualifying_finalized_event_is_hit_before_outage_semantics():
    value = classify(
        events=(event(),),
        spans=(
            span(end=EXPECTED - 30, reference="healthy"),
            span(
                start=EXPECTED - 30,
                kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
                reference="offline",
                cadence=3,
            ),
        ),
    )
    assert value.outcome is windows.ExpectedWindowOutcome.HIT
    assert value.qualifying_finalized_event_id == "restart-1"
    assert not value.negative_penalty_active
    assert "classification_order_hit_first" in value.reason_codes


def test_offline_observed_is_ambiguous_never_a_miss():
    value = classify(
        spans=(
            span(end=EXPECTED - 30, reference="healthy"),
            span(
                start=EXPECTED - 30,
                kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
                reference="offline",
                cadence=3,
            ),
        )
    )
    assert value.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert value.outage_observed
    assert value.overlapping_outage_episode_ids == ("offline",)
    assert not value.negative_penalty_active
    assert "classification_order_ambiguous_before_miss" in value.reason_codes


def test_observed_outage_recovery_without_final_event_is_ambiguous():
    evidence = windows.ExpectedWindowEpisodeEvidence(
        "episode-outage",
        EXPECTED - 60,
        EXPECTED + 20,
        True,
        True,
        False,
        True,
        ("direct_failure_recovery_chain",),
    )
    value = classify(episodes=(evidence,))
    assert value.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert value.outage_observed
    assert value.overlapping_outage_episode_ids == ("episode-outage",)


def test_active_unresolved_episode_is_ambiguous_but_not_proven_outage():
    evidence = windows.ExpectedWindowEpisodeEvidence(
        "episode-active",
        EXPECTED - 60,
        None,
        False,
        False,
        True,
        True,
        ("active_physical_episode",),
    )
    value = classify(episodes=(evidence,))
    assert value.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert value.unresolved_episode
    assert not value.outage_observed
    assert "overlapping_unresolved_episode" in value.reason_codes


def test_persisted_unresolved_episode_coverage_is_ambiguous_without_retraction_proof():
    value = classify(
        spans=(
            span(
                kind=continuity.ContinuitySpanKind.UNRESOLVED_EPISODE,
                reference="persisted-unresolved",
            ),
        )
    )
    assert value.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert value.unresolved_episode
    assert not value.outage_observed
    assert value.overlapping_outage_episode_ids == ("persisted-unresolved",)
    assert "overlapping_unresolved_episode_coverage" in value.reason_codes

    original = legacy_miss()
    assert reconcile((original,), (value,)) == (original,)


def test_complete_single_chain_healthy_window_is_one_genuine_miss():
    value = classify()
    assert value.outcome is windows.ExpectedWindowOutcome.GENUINE_MISS
    assert value.coverage_classification is windows.WindowCoverageClassification.COMPLETE_HEALTHY
    assert value.healthy_throughout
    assert value.negative_penalty_active
    assert value.observed_ratio == 1
    assert "complete_single_chain_healthy_observation" in value.reason_codes


def test_partial_monitoring_is_unknown():
    value = classify(spans=(span(end=EXPECTED, reference="half"),))
    assert value.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert not value.negative_penalty_active
    assert not value.healthy_throughout


@pytest.mark.parametrize(
    ("kind", "coverage"),
    [
        (continuity.ContinuitySpanKind.SHUTDOWN, windows.WindowCoverageClassification.LIFECYCLE_GAP),
        (continuity.ContinuitySpanKind.PAUSE, windows.WindowCoverageClassification.LIFECYCLE_GAP),
        (continuity.ContinuitySpanKind.SERVER_SWITCH, windows.WindowCoverageClassification.LIFECYCLE_GAP),
        (continuity.ContinuitySpanKind.SLEEP_GAP, windows.WindowCoverageClassification.LIFECYCLE_GAP),
        (continuity.ContinuitySpanKind.QUERY_HEALTH_GAP, windows.WindowCoverageClassification.UNHEALTHY),
        (continuity.ContinuitySpanKind.MISSING_FIELD_GAP, windows.WindowCoverageClassification.UNHEALTHY),
    ],
)
def test_boundary_or_unhealthy_window_is_unknown(kind, coverage):
    value = classify(
        spans=(
            span(end=EXPECTED - 30, reference="before"),
            span(
                start=EXPECTED - 30,
                end=EXPECTED + 30,
                kind=kind,
                reference=f"boundary-{kind.value}",
            ),
            span(start=EXPECTED + 30, reference="after"),
        )
    )
    assert value.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert value.coverage_classification is coverage
    assert not value.negative_penalty_active
    assert kind.value in value.reason_codes


def test_historical_continuity_without_proof_cannot_create_miss():
    value = classify(spans=(span(provenance=0),))
    assert value.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert value.coverage_classification is windows.WindowCoverageClassification.UNPROVEN
    assert value.blocker_kind == "historical_continuity_unproven"


def test_late_finalized_event_retracts_miss():
    original = legacy_miss()
    current = classify(events=(event(),))
    ledger = reconcile((original,), (current,))
    revision = ledger[-1]
    assert isinstance(revision, windows.ExpectedWindowRevision)
    assert revision.outcome is windows.ExpectedWindowOutcome.RETRACTED_MISS
    assert revision.reconciliation_reason is windows.ReconciliationReason.FINALIZED_EVENT
    assert revision.qualifying_finalized_event_id == "restart-1"
    assert revision.supersedes_result_id == original.result_id


def test_persisted_outage_retracts_miss():
    original = legacy_miss()
    current = classify(
        spans=(
            span(end=EXPECTED - 30, reference="healthy"),
            span(
                start=EXPECTED - 30,
                kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
                reference="offline-proof",
                cadence=3,
            ),
        )
    )
    revision = reconcile((original,), (current,))[-1]
    assert revision.reconciliation_reason is windows.ReconciliationReason.OBSERVED_OUTAGE
    assert revision.overlapping_outage_episode_ids == ("offline-proof",)
    assert "retracted_observed_outage" in revision.reason_codes


def test_reconciliation_preserves_original_and_links_revision():
    original = legacy_miss()
    current = classify(events=(event(),))
    ledger = reconcile((original,), (current,))
    assert ledger[0] is original
    assert ledger[1].supersedes_result_id == original.result_id
    assert len({item.result_id for item in ledger}) == 2


def test_reconciliation_is_idempotent_and_byte_equivalent():
    original = legacy_miss()
    current = classify(events=(event(),))
    once = reconcile((original,), (current,))
    twice = reconcile(once, (current,))
    assert twice == once
    assert tuple(asdict(item) for item in twice) == tuple(asdict(item) for item in once)


def test_replayed_evidence_creates_identical_result_and_revision_ids():
    first = classify(events=(event(), event()))
    second = classify(events=(event(),))
    assert first == second
    original = legacy_miss()
    assert reconcile((original,), (first,)) == reconcile((original,), (second,))


def test_retracted_miss_has_zero_active_penalty():
    original = legacy_miss()
    ledger = reconcile((original,), (classify(events=(event(),)),))
    active = windows.derive_active_miss_ledger(ledger)
    assert active.active_results == ()
    assert active.total_active_penalty == 0


def test_unrelated_miss_is_untouched():
    first = legacy_miss()
    unrelated = legacy_miss(expected=EXPECTED + PERIOD, key="unrelated")
    ledger = reconcile((first, unrelated), (classify(events=(event(),)),))
    active = windows.derive_active_miss_ledger(ledger)
    assert active.active_results == (unrelated,)
    assert ledger[0:2] == (first, unrelated)


def test_incomplete_or_unhealthy_uncertainty_does_not_retract():
    original = legacy_miss()
    unknown = classify(
        spans=(
            span(
                kind=continuity.ContinuitySpanKind.QUERY_HEALTH_GAP,
                reference="query-gap",
            ),
        )
    )
    assert unknown.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert reconcile((original,), (unknown,)) == (original,)


def test_unresolved_episode_without_positive_outage_does_not_retract():
    original = legacy_miss()
    ambiguous = classify(
        episodes=(
            windows.ExpectedWindowEpisodeEvidence(
                "unresolved", EXPECTED - 1, None, False, False, True, True
            ),
        )
    )
    assert ambiguous.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert not ambiguous.outage_observed
    assert reconcile((original,), (ambiguous,)) == (original,)


def test_candidate_periods_are_independent():
    three = classify(period=3 * scoring.HOUR)
    six = classify(period=6 * scoring.HOUR)
    assert three.outcome is six.outcome is windows.ExpectedWindowOutcome.GENUINE_MISS
    assert three.result_id != six.result_id
    assert three.candidate_period_seconds == 3 * scoring.HOUR
    assert six.candidate_period_seconds == 6 * scoring.HOUR


def test_classifier_has_no_incumbent_selection_confidence_or_consumer_inputs():
    parameters = inspect.signature(windows.classify_expected_window).parameters
    forbidden = {
        "incumbent_period",
        "incumbent_period_seconds",
        "selected_period",
        "selected_period_seconds",
        "confidence",
        "consumer_state",
        "prediction_usable",
        "countdown_visible",
    }
    assert forbidden.isdisjoint(parameters)


def test_expected_window_records_are_immutable():
    value = classify()
    with pytest.raises(FrozenInstanceError):
        value.outcome = windows.ExpectedWindowOutcome.UNKNOWN


def test_current_scorer_value_is_identical_before_and_after_v2_shadow():
    physical = tuple(event(f"event-{index}", at=BASE + index * PERIOD) for index in range(4))
    timeline = scoring.CoverageTimeline(
        (scoring.CoverageSegment(BASE, BASE + 3 * PERIOD, scoring.CoverageKind.ONLINE_HEALTHY),)
    )
    scorer = scoring.RestartScheduleScorer()
    before = scorer.score(physical, timeline, now=BASE + 3 * PERIOD)
    classify(events=physical, spans=(span(),))
    after = scorer.score(physical, timeline, now=BASE + 3 * PERIOD)
    assert asdict(after) == asdict(before)


def test_active_penalty_derivation_counts_only_latest_genuine_lineages():
    retained = legacy_miss(expected=EXPECTED + PERIOD, key="retained")
    retracted = legacy_miss()
    ledger = reconcile((retracted, retained), (classify(events=(event(),)),))
    active = windows.derive_active_miss_ledger(ledger)
    assert active.active_results == (retained,)
    assert active.total_active_penalty == 1
    assert "latest_lineage_revision_only" in active.reason_codes


def test_sanitized_bob_three_hour_false_misses_all_retract():
    expected_values = (BASE, BASE + PERIOD, BASE + 2 * PERIOD)
    originals = tuple(
        legacy_miss(expected=value, key=f"bob-3h-{index}")
        for index, value in enumerate(expected_values)
    )
    classifications = tuple(
        classify(
            expected=value,
            spans=(
                span(
                    start=value - 9 * 60,
                    end=value - 60,
                    reference=f"bob-healthy-{index}",
                ),
                span(
                    start=value - 60,
                    end=value + 60,
                    kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
                    reference=f"bob-outage-{index}",
                    cadence=3,
                ),
                span(
                    start=value + 60,
                    end=value + 9 * 60,
                    reference=f"bob-recovered-{index}",
                ),
            ),
        )
        for index, value in enumerate(expected_values)
    )
    ledger = reconcile(originals, classifications)
    revisions = [item for item in ledger if isinstance(item, windows.ExpectedWindowRevision)]
    assert len(revisions) == 3
    assert all(
        item.reconciliation_reason is windows.ReconciliationReason.OBSERVED_OUTAGE
        for item in revisions
    )
    assert windows.derive_active_miss_ledger(ledger).total_active_penalty == 0


def test_sanitized_bob_overlapping_six_hour_rows_classify_consistently():
    expected_values = (BASE, BASE + 2 * PERIOD)
    originals = tuple(
        legacy_miss(period=6 * scoring.HOUR, expected=value, key=f"bob-6h-{index}")
        for index, value in enumerate(expected_values)
    )
    classifications = tuple(
        classify(
            period=6 * scoring.HOUR,
            expected=value,
            spans=(
                span(
                    start=value - 9 * 60,
                    end=value - 30,
                    reference=f"six-healthy-{index}",
                ),
                span(
                    start=value - 30,
                    end=value + 30,
                    kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
                    reference=f"six-outage-{index}",
                    cadence=3,
                ),
                span(
                    start=value + 30,
                    end=value + 9 * 60,
                    reference=f"six-recovered-{index}",
                ),
            ),
        )
        for index, value in enumerate(expected_values)
    )
    ledger = reconcile(originals, classifications)
    revisions = [item for item in ledger if isinstance(item, windows.ExpectedWindowRevision)]
    assert len(revisions) == 2
    assert all(item.outage_observed for item in revisions)
    assert all(
        item.reconciliation_reason is windows.ReconciliationReason.OBSERVED_OUTAGE
        for item in revisions
    )


def test_bob_shaped_row_without_positive_proof_remains_untouched():
    original = legacy_miss(key="bob-unproven")
    classification = classify(spans=(span(provenance=0),))
    assert classification.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert reconcile((original,), (classification,)) == (original,)


def make_runtime(tmp_path, *, shadow=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return runtime.Phase2RestartRuntime.initialize(
        active_path=tmp_path / "active.json",
        legacy_path=tmp_path / "legacy.json",
        now=BASE,
        app_session_id=APP,
        generation_id="11111111-1111-4111-8111-111111111111",
        expected_window_v2_shadow_enabled=shadow,
    )


def prepare_expected_window_runtime(value):
    server = value._server(SERVER)
    server.regime_generation = "shared-regime-generation"
    physical = tuple(
        event(f"runtime-event-{index}", at=BASE + index * PERIOD)
        for index in range(4)
    )
    coverage = scoring.CoverageSegment(
        BASE - 60,
        BASE + 13 * scoring.HOUR,
        scoring.CoverageKind.ONLINE_HEALTHY,
        10,
        server_key=SERVER,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=chain_id(),
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )
    server.events = list(physical)
    server.event_seq = len(physical)
    server.coverage = [coverage]
    value._evaluate(server, now=BASE + 9 * scoring.HOUR)
    candidate = server.score.candidate(PERIOD)
    assert candidate.fundamental_relationship_count >= 2
    completed_at = BASE + 12 * scoring.HOUR + scoring.candidate_phase_tolerance(PERIOD)
    return server, completed_at


def test_v2_shadow_disabled_creates_no_result_or_diagnostic(tmp_path, caplog):
    value = make_runtime(tmp_path, shadow=False)
    server, completed_at = prepare_expected_window_runtime(value)
    with caplog.at_level("DEBUG"):
        value._evaluate_expected_windows(server, now=completed_at)
    assert value.expected_window_v2_shadow_snapshot(SERVER) is None
    assert not any("expected-window v2 shadow" in item.message for item in caplog.records)


def test_v2_shadow_enabled_does_not_change_v1_score_or_misses(tmp_path):
    disabled = make_runtime(tmp_path / "disabled", shadow=False)
    enabled = make_runtime(tmp_path / "enabled", shadow=True)
    disabled_server, disabled_at = prepare_expected_window_runtime(disabled)
    enabled_server, enabled_at = prepare_expected_window_runtime(enabled)
    disabled_before = asdict(disabled_server.score)
    enabled_before = asdict(enabled_server.score)
    disabled._evaluate_expected_windows(disabled_server, now=disabled_at)
    enabled._evaluate_expected_windows(enabled_server, now=enabled_at)
    assert asdict(disabled_server.score) == disabled_before
    assert asdict(enabled_server.score) == enabled_before == disabled_before
    assert enabled_server.expected_misses == disabled_server.expected_misses
    snapshot = enabled.expected_window_v2_shadow_snapshot(SERVER)
    assert snapshot is not None
    assert any(
        item.candidate_period_seconds == PERIOD
        and item.outcome is windows.ExpectedWindowOutcome.GENUINE_MISS
        for item in snapshot
    )


def test_v2_shadow_does_not_change_current_consumer_output(tmp_path):
    disabled = make_runtime(tmp_path / "disabled", shadow=False)
    enabled = make_runtime(tmp_path / "enabled", shadow=True)
    disabled_server, disabled_at = prepare_expected_window_runtime(disabled)
    enabled_server, enabled_at = prepare_expected_window_runtime(enabled)
    disabled._evaluate_expected_windows(disabled_server, now=disabled_at)
    enabled._evaluate_expected_windows(enabled_server, now=enabled_at)
    disabled_decision = disabled._evaluate(disabled_server, now=disabled_at)
    enabled_decision = enabled._evaluate(enabled_server, now=enabled_at)
    assert asdict(enabled_decision) == asdict(disabled_decision)
    assert runtime.phase2_learning_summary(
        enabled_decision, now=enabled_at
    ) == runtime.phase2_learning_summary(disabled_decision, now=disabled_at)


def test_v2_shadow_is_not_serialized_as_schema_authority(tmp_path):
    value = make_runtime(tmp_path, shadow=True)
    server, completed_at = prepare_expected_window_runtime(value)
    value._evaluate_expected_windows(server, now=completed_at)
    server.dirty = True
    assert value._persist_server(server, force=True, now=completed_at)
    raw = (tmp_path / "active.json").read_text(encoding="utf-8")
    assert "expected_window_v2" not in raw
    assert "retracted_miss" not in raw
