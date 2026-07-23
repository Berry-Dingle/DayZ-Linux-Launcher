from dataclasses import replace

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_800_000_000.0
HOUR = scoring.HOUR


def event(hours, sequence, *, outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART):
    at = BASE + hours * HOUR
    authenticity = 0.98 if outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART else 0.10
    return detection.PhysicalRestartEvent(
        event_id=f"phase3-{sequence}-{hours}",
        sequence=sequence,
        fingerprint=f"phase3-fingerprint-{sequence}",
        server_key="server:2302",
        episode_started_at=at - 120,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_RETURN}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(None, None, None, None, False, None, None, 0, 0, False),
        outage=detection.OutageSummary(None, None, 0, (), at),
        recovery=detection.RecoverySummary(None, at, at, 2, False),
        query_health=detection.QueryHealthSummary(4, 0, 0, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=(),
    )


def events(hours):
    return tuple(event(value, index + 1) for index, value in enumerate(hours))


def timeline(*segments):
    return scoring.CoverageTimeline(
        tuple(
            scoring.CoverageSegment(
                BASE + start * HOUR,
                BASE + end * HOUR,
                kind,
                10,
            )
            for start, end, kind in segments
        )
    )


def result(hours, coverage=None, *, previous=None, physical=None):
    values = physical if physical is not None else events(hours)
    return scoring.RestartScheduleScorer().score(
        values,
        coverage or scoring.CoverageTimeline(),
        now=BASE + max(hours) * HOUR,
        previous=previous,
    )


def test_one_unmonitored_six_hour_gap_is_bounded_three_hour_multiple_not_prediction():
    scored = result((0, 6))
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 1
    assert three.compatible_multiple_support == pytest.approx(0.65)
    assert three.covered_miss_count == 0
    assert three.fundamental_period_confidence > 0
    assert not three.establishment_gates_passed
    assert scored.selected_period_seconds is None
    assert not scored.prediction_usable


def test_two_sporadic_gaps_support_same_three_hour_fundamental_without_selecting():
    scored = result((0, 6, 27))
    three = scored.candidate(3 * HOUR)
    assert [item.multiplier for item in three.multiple_interval_diagnostics if item.accepted] == [2, 7]
    assert three.compatible_multiple_interval_count == 2
    assert three.compatible_multiple_support > 0.9
    assert three.fundamental_period_confidence > 0
    assert scored.selected_period_seconds is None


def test_repeated_independent_sporadic_observations_eventually_make_prediction_usable():
    scored = result(tuple(range(0, 48, 6)))
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 7
    assert three.fundamental_relationship_count == 7
    assert three.high_gates_passed
    assert three.fundamental_period_confidence >= 0.80
    assert scored.selected_period_seconds == 3 * HOUR
    assert scored.prediction_usable


def test_complete_intermediate_window_blocks_multiple_and_records_miss():
    scored = result(
        (0, 6),
        timeline((0, 6, scoring.CoverageKind.ONLINE_HEALTHY)),
    )
    three = scored.candidate(3 * HOUR)
    six = scored.candidate(6 * HOUR)
    assert three.compatible_multiple_interval_count == 0
    assert three.covered_miss_count == 1
    assert three.multiple_interval_diagnostics[0].reason == "covered_intermediate_miss"
    assert six.strict_direct_interval_count == 1


@pytest.mark.parametrize(
    "gap_kind, expected_status",
    [
        (scoring.CoverageKind.QUERY_HEALTH_GAP, scoring.IntermediateWindowStatus.QUERY_HEALTH_GAP),
        (scoring.CoverageKind.UNMONITORED, scoring.IntermediateWindowStatus.LIFECYCLE_GAP),
    ],
)
def test_query_or_monitoring_gap_permits_multiple_without_miss(gap_kind, expected_status):
    scored = result(
        (0, 6),
        timeline(
            (0, 2.8, scoring.CoverageKind.ONLINE_HEALTHY),
            (2.8, 3.2, gap_kind),
            (3.2, 6, scoring.CoverageKind.ONLINE_HEALTHY),
        ),
    )
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 1
    assert three.covered_miss_count == 0
    assert three.multiple_interval_diagnostics[0].intermediate_windows[0].status is expected_status


def test_mixed_intermediate_windows_apply_one_miss_without_multiple_credit():
    scored = result(
        (0, 9),
        timeline(
            (0, 5.8, scoring.CoverageKind.ONLINE_HEALTHY),
            (5.8, 6.2, scoring.CoverageKind.QUERY_HEALTH_GAP),
            (6.2, 9, scoring.CoverageKind.ONLINE_HEALTHY),
        ),
    )
    three = scored.candidate(3 * HOUR)
    diagnostic = three.multiple_interval_diagnostics[0]
    assert [item.status for item in diagnostic.intermediate_windows] == [
        scoring.IntermediateWindowStatus.ADEQUATELY_MONITORED_MISS,
        scoring.IntermediateWindowStatus.QUERY_HEALTH_GAP,
    ]
    assert not diagnostic.accepted
    assert three.compatible_multiple_support == 0
    assert three.covered_miss_count == 1


def test_one_interposed_manual_event_is_bounded_skip_over_and_retained_off_grid():
    scored = result((0, 2 + 10 / 60, 6))
    three = scored.candidate(3 * HOUR)
    accepted = [item for item in three.multiple_interval_diagnostics if item.accepted]
    assert len(accepted) == 1
    assert accepted[0].evidence_kind is scoring.IntervalEvidenceKind.SKIP_OVER_MULTIPLE
    assert accepted[0].skipped_event_ids == ("phase3-2-2.1666666666666665",)
    assert three.skip_over_interval_count == 1
    assert three.off_grid_event_count == 1
    assert three.compatible_multiple_support == pytest.approx(0.4875)


def test_one_isolated_off_grid_event_does_not_block_three_hour_establishment():
    scored = result((0, 3, 6, 8 + 10 / 60, 9, 12, 15, 18), timeline((0, 18, scoring.CoverageKind.ONLINE_HEALTHY)))
    three = scored.candidate(3 * HOUR)
    assert three.establishment_gates_passed
    assert three.off_grid_event_count == 1
    assert scored.selected_period_seconds == 3 * HOUR


def test_repeated_interposed_events_are_not_indiscriminately_skipped():
    hours = (0, 1, 3, 4, 6, 7, 9, 10, 12, 13, 15)
    scored = result(hours, timeline((0, 15, scoring.CoverageKind.ONLINE_HEALTHY)))
    three = scored.candidate(3 * HOUR)
    assert three.skip_over_interval_count == 0
    assert three.fundamental_relationship_count == 0
    assert not three.establishment_gates_passed


def test_repeated_off_grid_events_reduce_but_do_not_get_globally_discarded():
    clean = result(
        (0, 3, 6, 9, 12, 15, 18, 21, 24),
        timeline((0, 24, scoring.CoverageKind.ONLINE_HEALTHY)),
    ).candidate(3 * HOUR)
    noisy = result(
        (0, 3, 6, 7, 9, 12, 13, 15, 18, 21, 24),
        timeline((0, 24, scoring.CoverageKind.ONLINE_HEALTHY)),
    ).candidate(3 * HOUR)
    assert noisy.off_grid_event_count == 2
    assert noisy.off_grid_penalty == pytest.approx(1.5)
    assert noisy.fundamental_period_confidence < clean.fundamental_period_confidence


def test_multiplier_cap_keeps_very_long_pair_as_hint_only():
    scored = result((0, 27))
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 0
    assert three.compatible_multiple_support == 0
    assert three.multiple_interval_diagnostics[0].multiplier == 9
    assert three.multiple_interval_diagnostics[0].reason == "multiplier_exceeds_cap"


def test_genuine_four_hour_schedule_with_realistic_jitter_remains_learnable():
    hours = (0, 3.95, 8.02, 11.98, 16.03, 19.97)
    scored = result(hours, timeline((0, 20, scoring.CoverageKind.ONLINE_HEALTHY)))
    four = scored.candidate(4 * HOUR)
    assert four.strict_direct_interval_count == 5
    assert four.establishment_gates_passed
    assert scored.selected_period_seconds == 4 * HOUR


def test_bounded_pair_generation_looks_past_at_most_one_positive_event():
    scored = result((0, 6, 12, 18, 24))
    expected_pairs = 4 + 3
    assert all(item.bounded_pair_count == expected_pairs for item in scored.candidates)
    assert scoring.MAX_SKIPPED_POSITIVE_EVENTS == 1


def test_overlapping_endpoint_relationships_do_not_recount_same_cycles():
    scored = result((0, 6, 12))
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 2
    rejected = [item for item in three.multiple_interval_diagnostics if not item.accepted]
    assert any(item.reason == "intermediate_restart_observed" for item in rejected)


def test_interposed_zero_weight_event_is_diagnostic_not_positive_or_off_grid_support():
    physical = (
        event(0, 1),
        event(3, 2, outcome=detection.EventOutcome.AMBIGUOUS_DRAIN),
        event(6, 3),
    )
    scored = result((0, 3, 6), physical=physical)
    three = scored.candidate(3 * HOUR)
    assert three.compatible_multiple_interval_count == 1
    assert three.qualifying_event_count == 2
    assert three.off_grid_event_count == 0
    assert three.multiple_interval_diagnostics[0].intermediate_windows[0].status is scoring.IntermediateWindowStatus.INSUFFICIENT_MONITORING


def test_skip_over_relationship_does_not_cross_known_regime_boundary():
    previous = result((0, 3, 6, 9, 12))
    previous = replace(
        previous,
        prior_regimes=(scoring.RegimeSummary(3 * HOUR, BASE + 3 * HOUR, 0.9, 8, "test-boundary"),),
    )
    scored = result((0, 2 + 10 / 60, 6), previous=previous)
    three = scored.candidate(3 * HOUR)
    assert three.skip_over_interval_count == 0
    assert any(item.reason == "crosses_regime_boundary" for item in three.multiple_interval_diagnostics)


def test_three_hour_phase_consistency_beats_one_adjacent_3h50_four_hour_interval():
    hours = (0, 3, 6, 8 + 10 / 60, 12, 15, 18, 21)
    scored = result(hours, timeline((0, 21, scoring.CoverageKind.ONLINE_HEALTHY)))
    three = scored.candidate(3 * HOUR)
    four = scored.candidate(4 * HOUR)
    assert four.strict_direct_interval_count == 1
    assert not four.establishment_gates_passed
    assert three.fundamental_period_confidence > four.fundamental_period_confidence
    assert scored.selected_period_seconds == 3 * HOUR


def test_ambiguous_sparse_competitors_do_not_select_prematurely():
    scored = result((0, 6))
    assert scored.selected_period_seconds is None
    assert not scored.prediction_usable
    assert all(not candidate.establishment_gates_passed for candidate in scored.candidates)


def test_complete_coverage_miss_and_no_monitoring_gap_have_opposite_effects():
    covered = result((0, 6), timeline((0, 6, scoring.CoverageKind.ONLINE_HEALTHY))).candidate(3 * HOUR)
    absent = result((0, 6)).candidate(3 * HOUR)
    assert covered.covered_miss_count == 1
    assert covered.compatible_multiple_interval_count == 0
    assert absent.covered_miss_count == 0
    assert absent.compatible_multiple_interval_count == 1


def test_bob_shaped_fixture_combines_direct_multiple_phase_and_zero_weight_history():
    physical = [event(0, 1), event(3, 2), event(9, 3), event(10.2, 4), event(18, 5), event(27, 6)]
    physical.extend(
        event(value, 100 + index, outcome=detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION)
        for index, value in enumerate((1.1, 7.2, 16.4))
    )
    coverage = timeline(
        (0, 3, scoring.CoverageKind.ONLINE_HEALTHY),
        (3, 27, scoring.CoverageKind.UNMONITORED),
    )
    scored = result((0, 3, 9, 10.2, 18, 27), coverage, physical=tuple(physical))
    three = scored.candidate(3 * HOUR)
    assert three.strict_direct_interval_count == 1
    assert three.compatible_multiple_interval_count >= 3
    assert three.skip_over_interval_count >= 1
    assert three.phase_confidence > 0
    assert all("100-" not in item.left_event_id and "100-" not in item.right_event_id for item in three.multiple_interval_diagnostics)
    assert scored.candidate(4 * HOUR).fundamental_period_confidence < three.fundamental_period_confidence
    assert any(item.accepted and item.reason == "accepted" for item in three.multiple_interval_diagnostics)
