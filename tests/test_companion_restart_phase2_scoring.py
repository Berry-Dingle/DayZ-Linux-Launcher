from dataclasses import FrozenInstanceError
import math

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_800_000_000.0
HOUR = scoring.HOUR
DAY = scoring.DAY


def event(
    hours,
    sequence,
    outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    *,
    event_id=None,
    uncertainty=0,
):
    authenticity = {
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART: 0.98,
        detection.EventOutcome.CONFIRMED_OFFLINE_RESTART: 0.85,
        detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART: 0.90,
        detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.60,
        detection.EventOutcome.AMBIGUOUS_DRAIN: 0.25,
        detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION: 0.10,
        detection.EventOutcome.INCOMPLETE: 0.10,
        detection.EventOutcome.EXPIRED: 0.15,
        detection.EventOutcome.SERVICE_INTERRUPTION: 0.20,
    }[outcome]
    at = BASE + hours * HOUR
    return detection.PhysicalRestartEvent(
        event_id=event_id or f"event-{sequence}-{hours}",
        sequence=sequence,
        fingerprint=f"fingerprint-{sequence}",
        server_key="server:2302",
        episode_started_at=at - 120,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=uncertainty,
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


def events_at(hours, outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART):
    return tuple(event(value, index + 1, outcome) for index, value in enumerate(hours))


def full_coverage(start_hour, end_hour, *, kind=scoring.CoverageKind.ONLINE_HEALTHY, cadence=10):
    return scoring.CoverageTimeline(
        (scoring.CoverageSegment(BASE + start_hour * HOUR, BASE + end_hour * HOUR, kind, cadence),)
    )


def score(hours, period=None, *, outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART, coverage=None, previous=None, aggregate=None):
    physical = events_at(hours, outcome)
    timeline = coverage if coverage is not None else full_coverage(min(hours), max(hours))
    result = scoring.RestartScheduleScorer().score(
        physical,
        timeline,
        now=BASE + max(hours) * HOUR,
        previous=previous,
        incumbent_period_seconds=period,
        aggregate=aggregate,
    )
    return result


def test_coverage_models_are_immutable_and_validate_values():
    segment = scoring.CoverageSegment(BASE, BASE + 60, scoring.CoverageKind.ONLINE_HEALTHY)
    with pytest.raises(FrozenInstanceError):
        segment.end_at = BASE + 70
    with pytest.raises(ValueError):
        scoring.CoverageSegment(BASE, BASE, scoring.CoverageKind.ONLINE_HEALTHY)
    with pytest.raises(ValueError):
        scoring.CoverageSegment(BASE, BASE + 1, "online")


def test_fully_covered_direct_interval_predicate():
    timeline = full_coverage(0, 3)
    assessment = timeline.assess(BASE, BASE + 3 * HOUR)
    assert assessment.fully_covered
    assert assessment.observed_ratio == 1
    assert assessment.query_health_adequate


@pytest.mark.parametrize(
    "kind",
    [
        scoring.CoverageKind.PARTIAL,
        scoring.CoverageKind.UNMONITORED,
        scoring.CoverageKind.QUERY_HEALTH_GAP,
        scoring.CoverageKind.APP_STOPPED,
        scoring.CoverageKind.PAUSED,
        scoring.CoverageKind.SERVER_SWITCH,
        scoring.CoverageKind.SLEEP_GAP,
        scoring.CoverageKind.MISSING_PLAYERS,
        scoring.CoverageKind.MISSING_INFO,
    ],
)
def test_explicit_coverage_breaks_block_exact_evidence(kind):
    timeline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(BASE, BASE + HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
            scoring.CoverageSegment(BASE + HOUR, BASE + HOUR + 10, kind),
            scoring.CoverageSegment(BASE + HOUR + 10, BASE + 3 * HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
        )
    )
    assessment = timeline.assess(BASE, BASE + 3 * HOUR)
    assert not assessment.fully_covered
    assert kind in assessment.blocking_kinds


def test_large_internal_gap_fails_even_above_ninety_percent_ratio():
    timeline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(BASE, BASE + 500, scoring.CoverageKind.ONLINE_HEALTHY),
            scoring.CoverageSegment(BASE + 531, BASE + 1000, scoring.CoverageKind.ONLINE_HEALTHY),
        )
    )
    result = timeline.assess(BASE, BASE + 1000)
    assert result.observed_ratio > 0.90
    assert result.largest_gap == 31
    assert not result.fully_covered


def test_online_gap_threshold_and_edge_gap_are_structural():
    exactly = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(BASE + 30, BASE + 500, scoring.CoverageKind.ONLINE_HEALTHY),
            scoring.CoverageSegment(BASE + 530, BASE + 1000, scoring.CoverageKind.ONLINE_HEALTHY),
        )
    ).assess(BASE, BASE + 1000)
    above = scoring.CoverageTimeline(
        (scoring.CoverageSegment(BASE + 31, BASE + 1000, scoring.CoverageKind.ONLINE_HEALTHY),)
    ).assess(BASE, BASE + 1000)
    assert exactly.fully_covered
    assert exactly.edge_gap == 30
    assert not above.fully_covered


@pytest.mark.parametrize(("gap", "covered"), [(12, True), (13, False)])
def test_offline_cadence_gap_threshold(gap, covered):
    timeline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(BASE, BASE + 100, scoring.CoverageKind.OFFLINE_OBSERVED, 3),
            scoring.CoverageSegment(BASE + 100 + gap, BASE + 300, scoring.CoverageKind.OFFLINE_OBSERVED, 3),
        )
    )
    assert timeline.assess(BASE, BASE + 300).fully_covered is covered


def test_offline_to_online_transition_uses_stricter_offline_gap_threshold():
    timeline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(BASE, BASE + 100, scoring.CoverageKind.OFFLINE_OBSERVED, 3),
            scoring.CoverageSegment(BASE + 113, BASE + 300, scoring.CoverageKind.ONLINE_HEALTHY, 10),
        )
    )
    assert not timeline.assess(BASE, BASE + 300).fully_covered


def test_unresolved_episode_blocks_direct_support_and_expected_miss():
    physical = (event(0, 1), event(3, 2, detection.EventOutcome.AMBIGUOUS_DRAIN), event(6, 3))
    timeline = full_coverage(0, 6)
    result = scoring.RestartScheduleScorer().score(physical, timeline, now=BASE + 6 * HOUR)
    assert result.candidate(6 * HOUR).strict_direct_interval_count == 0
    assert result.candidate(3 * HOUR).covered_miss_count == 0


def test_no_coverage_creates_hints_but_no_exact_support_or_penalty():
    result = score((0, 6), coverage=scoring.CoverageTimeline())
    candidate = result.candidate(6 * HOUR)
    assert candidate.strict_direct_interval_count == 0
    assert candidate.covered_miss_count == 0
    assert candidate.hints.event_count == 2
    assert candidate.fundamental_period_confidence == 0


def test_locally_incomplete_physical_event_cannot_supply_exact_evidence():
    complete = event(0, 1)
    incomplete = replace_event(event(3, 2), coverage_complete=False)
    result = scoring.RestartScheduleScorer().score(
        (complete, incomplete), full_coverage(0, 3), now=BASE + 3 * HOUR
    )
    assert result.candidate(3 * HOUR).direct_support == 0


def test_adjacent_three_hour_events_create_only_two_three_hour_intervals():
    result = score((0, 3, 6))
    assert result.candidate(3 * HOUR).strict_direct_interval_count == 2
    assert result.candidate(6 * HOUR).strict_direct_interval_count == 0


def test_duplicate_event_ids_are_ignored():
    physical = (event(0, 1), event(3, 2), event(3, 99, event_id="event-2-3"), event(6, 3))
    result = scoring.RestartScheduleScorer().score(physical, full_coverage(0, 6), now=BASE + 6 * HOUR)
    assert result.recent_event_count == 3
    assert result.candidate(3 * HOUR).strict_direct_interval_count == 2


@pytest.mark.parametrize(
    ("outcome", "expected_weight", "strict"),
    [
        (detection.EventOutcome.CORROBORATED_OFFLINE_RESTART, 1.0, 1),
        (detection.EventOutcome.CONFIRMED_OFFLINE_RESTART, 0.8, 1),
        (detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART, 0.8, 1),
        (detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART, 0.5, 0),
    ],
)
def test_direct_endpoint_weight_and_strict_quality(outcome, expected_weight, strict):
    result = score((0, 3), outcome=outcome)
    candidate = result.candidate(3 * HOUR)
    assert candidate.direct_support == expected_weight
    assert candidate.strict_direct_interval_count == strict


def test_weaker_endpoint_controls_pair_weight():
    physical = (event(0, 1), event(3, 2, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART))
    result = scoring.RestartScheduleScorer().score(physical, full_coverage(0, 3), now=BASE + 3 * HOUR)
    assert result.candidate(3 * HOUR).direct_support == 0.5


def test_probable_only_events_never_pass_strict_establishment_gate():
    result = score(tuple(range(0, 36, 3)), outcome=detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART)
    candidate = result.candidate(3 * HOUR)
    assert candidate.strict_direct_interval_count == 0
    assert not candidate.establishment_gates_passed
    assert candidate.fundamental_period_confidence <= 0.49


def test_probable_endpoints_do_not_create_full_expected_window_penalties():
    result = score(
        (0, 6),
        outcome=detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
    )
    assert result.candidate(6 * HOUR).weak_direct_interval_count == 1
    assert result.candidate(3 * HOUR).covered_miss_count == 0


@pytest.mark.parametrize(
    "outcome",
    [
        detection.EventOutcome.AMBIGUOUS_DRAIN,
        detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
    ],
)
def test_weak_episodes_are_hint_only_across_unmonitored_time(outcome):
    physical = (event(0, 1, outcome), event(3, 2, outcome))
    result = scoring.RestartScheduleScorer().score(
        physical, scoring.CoverageTimeline(), now=BASE + 3 * HOUR
    )
    candidate = result.candidate(3 * HOUR)
    assert candidate.hints.event_count == 2
    assert candidate.direct_support == 0
    assert candidate.covered_miss_count == 0


@pytest.mark.parametrize(
    ("shorter", "longer"),
    [(2, 6), (3, 6), (3, 12), (4, 8), (4, 12), (6, 12)],
)
def test_covered_longer_interval_records_shorter_expected_misses(shorter, longer):
    result = score((0, longer))
    assert result.candidate(longer * HOUR).strict_direct_interval_count == 1
    assert result.candidate(shorter * HOUR).covered_miss_count >= 1


def test_intermediate_event_supports_shorter_and_prevents_longer_confirmation():
    result = score((0, 3, 6))
    assert result.candidate(3 * HOUR).direct_support == 2
    assert result.candidate(6 * HOUR).direct_support == 0


def test_divisor_resolution_requires_windows_across_independent_longer_intervals():
    one = score((0, 6)).candidate(6 * HOUR)
    two = score((0, 6, 12)).candidate(6 * HOUR)
    assert not all(item.established_resolved for item in one.divisor_resolution)
    assert all(item.established_resolved for item in two.divisor_resolution)


def test_eight_and_twelve_are_shared_phase_non_divisors():
    result = score((0, 8, 16, 24))
    twelve = result.candidate(12 * HOUR)
    assert all(item.divisor_seconds != 8 * HOUR for item in twelve.divisor_resolution)
    assert result.candidate(8 * HOUR).strict_direct_interval_count == 3
    assert twelve.strict_direct_interval_count == 0


def test_unresolved_longer_harmonic_receives_confidence_cap():
    # Artificially missing resolution is produced by blocking the intermediate windows.
    segments = (
        scoring.CoverageSegment(BASE, BASE + 2.8 * HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
        scoring.CoverageSegment(BASE + 3.2 * HOUR, BASE + 6 * HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
    )
    result = score((0, 6), coverage=scoring.CoverageTimeline(segments))
    assert result.candidate(6 * HOUR).fundamental_period_confidence == 0


def test_phase_only_hints_raise_schedule_and_phase_but_never_period_confidence():
    hours = (0, 6, 27, 48)
    result = score(
        hours,
        outcome=detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
        coverage=scoring.CoverageTimeline(),
    )
    three = result.candidate(3 * HOUR)
    assert three.hints.event_count == 4
    assert three.fundamental_period_confidence == 0
    assert three.phase_confidence > 0
    assert result.schedule_existence_confidence >= 0.60
    assert result.schedule_existence_confidence <= 0.75
    assert result.pattern_status is scoring.PatternStatus.PATTERN_OBSERVED


def test_one_direct_interval_gets_no_hint_bonus():
    physical = events_at((0, 24, 48, 72, 75))
    timeline = full_coverage(72, 75)
    result = scoring.RestartScheduleScorer().score(physical, timeline, now=BASE + 75 * HOUR)
    assert result.candidate(3 * HOUR).strict_direct_interval_count == 1
    assert result.candidate(3 * HOUR).hint_bonus == 0


@pytest.mark.parametrize(("direct_intervals", "maximum"), [(2, 0.03), (3, 0.03), (4, 0.05)])
def test_covered_confirmation_unlocks_only_small_hint_bonus(direct_intervals, maximum):
    hints = (0, 24, 48)
    direct = tuple(72 + index * 3 for index in range(direct_intervals + 1))
    physical = (
        *events_at(hints, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART),
        *events_at(direct),
    )
    timeline = full_coverage(72, direct[-1])
    result = scoring.RestartScheduleScorer().score(physical, timeline, now=BASE + direct[-1] * HOUR)
    candidate = result.candidate(3 * HOUR)
    assert candidate.strict_direct_interval_count == direct_intervals
    assert 0 <= candidate.hint_bonus <= maximum
    assert candidate.hints.event_count >= 3


def test_same_day_hints_do_not_unlock_bonus():
    day_start = -(BASE % DAY) / HOUR
    hint_hours = (day_start + 1, day_start + 4, day_start + 7)
    direct_hours = (day_start + 30, day_start + 33, day_start + 36)
    physical = events_at((*hint_hours, *direct_hours))
    timeline = full_coverage(direct_hours[0], direct_hours[-1])
    result = scoring.RestartScheduleScorer().score(
        physical, timeline, now=BASE + direct_hours[-1] * HOUR
    )
    assert result.candidate(3 * HOUR).hints.calendar_day_count < 2
    assert result.candidate(3 * HOUR).hint_bonus == 0


def test_covered_contradiction_prevents_hint_bonus():
    physical = events_at((0, 24, 48, 72, 75, 81))
    timeline = full_coverage(72, 81)
    result = scoring.RestartScheduleScorer().score(physical, timeline, now=BASE + 81 * HOUR)
    candidate = result.candidate(3 * HOUR)
    assert candidate.covered_miss_count >= 1
    assert candidate.hint_bonus == 0


def test_misaligned_hints_do_not_unlock_period_bonus():
    physical = events_at((0, 5, 13, 48, 51, 54))
    result = scoring.RestartScheduleScorer().score(
        physical, full_coverage(48, 54), now=BASE + 54 * HOUR
    )
    assert result.candidate(3 * HOUR).hint_bonus == 0


@pytest.mark.parametrize(
    ("net", "expected"),
    [(0, 0), (1, 0.25), (1.5, 0.35), (2, 0.45), (3, 0.65), (4, 0.78), (5, 0.85), (6, 0.885), (7, 0.92), (10, 0.96), (20, 0.96)],
)
def test_confidence_curve_and_interpolation(net, expected):
    assert scoring.confidence_from_net_evidence(net) == pytest.approx(expected)


def test_phase_uncertainty_is_capped_at_five_minutes():
    left = event(0, 1, uncertainty=10_000)
    right = event(3 + 14 / 60, 2, uncertainty=10_000)
    result = scoring.RestartScheduleScorer().score((left, right), full_coverage(0, 4), now=BASE + 4 * HOUR)
    # 3h base tolerance 9m plus capped 5m accepts 14m, not arbitrary uncertainty.
    assert result.candidate(3 * HOUR).strict_direct_interval_count == 1
    far = event(3 + 20 / 60, 3, uncertainty=100_000)
    rejected = scoring.RestartScheduleScorer().score((left, far), full_coverage(0, 4), now=BASE + 4 * HOUR)
    assert rejected.candidate(3 * HOUR).strict_direct_interval_count == 0


def test_event_authenticity_is_not_changed_by_scoring():
    physical = events_at((0, 3, 6))
    before = tuple(item.authenticity for item in physical)
    scoring.RestartScheduleScorer().score(physical, full_coverage(0, 6), now=BASE + 6 * HOUR)
    assert tuple(item.authenticity for item in physical) == before


def test_schedule_existence_phase_and_period_are_independent_dimensions():
    result = score(
        (0, 6, 27, 48),
        outcome=detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
        coverage=scoring.CoverageTimeline(),
    )
    candidate = result.candidate(3 * HOUR)
    assert result.schedule_existence_confidence > 0
    assert candidate.phase_confidence > 0
    assert candidate.fundamental_period_confidence == 0
    assert not result.prediction_usable


def test_stale_phase_observations_apply_recency_cap_and_disable_prediction():
    physical = events_at(tuple(range(0, 36, 4)))
    result = scoring.RestartScheduleScorer().score(
        physical, full_coverage(0, 32), now=BASE + 100 * HOUR
    )
    candidate = result.candidate(4 * HOUR)
    assert candidate.phase_confidence <= 0.60
    assert candidate.confidence_cap <= 0.79
    assert not result.prediction_usable


def test_inactivity_neutral_phase_policy_preserves_evidence_derived_confidence():
    physical = events_at(tuple(range(0, 36, 4)))
    timeline = full_coverage(0, 32)
    recent = scoring.RestartScheduleScorer().score(
        physical,
        timeline,
        now=BASE + 32 * HOUR,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    stale = scoring.RestartScheduleScorer().score(
        physical,
        timeline,
        now=BASE + 32 * HOUR + 30 * DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    assert stale.candidate(4 * HOUR).phase_confidence == recent.candidate(
        4 * HOUR
    ).phase_confidence
    assert stale.candidate(4 * HOUR).fundamental_period_confidence == recent.candidate(
        4 * HOUR
    ).fundamental_period_confidence


def test_inactivity_neutral_policy_ignores_non_evidential_partial_gap():
    physical = events_at((0, 3, 6, 9))
    baseline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(
                BASE, BASE + 9 * HOUR, scoring.CoverageKind.ONLINE_HEALTHY
            ),
        )
    )
    with_unrelated_gap = scoring.CoverageTimeline(
        (
            *baseline.segments,
            scoring.CoverageSegment(
                BASE + 20 * HOUR,
                BASE + 21 * HOUR,
                scoring.CoverageKind.PARTIAL,
            ),
        )
    )
    scorer = scoring.RestartScheduleScorer()
    first = scorer.score(
        physical,
        baseline,
        now=BASE + 40 * DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    second = scorer.score(
        physical,
        with_unrelated_gap,
        now=BASE + 40 * DAY,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    assert second == first


def test_legacy_phase_policy_remains_the_default():
    physical = events_at(tuple(range(0, 36, 4)))
    timeline = full_coverage(0, 32)
    implicit = scoring.RestartScheduleScorer().score(
        physical, timeline, now=BASE + 100 * HOUR
    )
    explicit = scoring.RestartScheduleScorer().score(
        physical,
        timeline,
        now=BASE + 100 * HOUR,
        phase_recency_policy=scoring.PhaseRecencyPolicy.LEGACY_DECAY,
    )
    assert implicit == explicit
    assert implicit.candidate(4 * HOUR).phase_confidence <= 0.60


def test_phase_recency_policy_rejects_ambiguous_values():
    with pytest.raises(ValueError, match="PhaseRecencyPolicy"):
        scoring.RestartScheduleScorer().score(
            events_at((0, 3)),
            full_coverage(0, 3),
            now=BASE + 3 * HOUR,
            phase_recency_policy="neutral",
        )


@pytest.mark.parametrize(
    ("period_hours", "hours"),
    [
        (2, tuple(range(0, 12, 2))),
        (3, tuple(range(0, 15, 3))),
        (4, tuple(range(0, 20, 4))),
        (6, tuple(range(0, 30, 6))),
        (8, tuple(range(0, 40, 8))),
        (12, tuple(range(0, 84, 12))),
    ],
)
def test_exact_establishment_gate_boundary(period_hours, hours):
    candidate = score(hours).candidate(period_hours * HOUR)
    assert candidate.establishment_gates_passed


@pytest.mark.parametrize(
    ("period_hours", "hours"),
    [
        (2, tuple(range(0, 10, 2))),
        (3, tuple(range(0, 12, 3))),
        (4, tuple(range(0, 16, 4))),
        (6, tuple(range(0, 24, 6))),
        (8, tuple(range(0, 32, 8))),
        (12, tuple(range(0, 72, 12))),
    ],
)
def test_just_below_establishment_interval_event_boundary_fails(period_hours, hours):
    candidate = score(hours).candidate(period_hours * HOUR)
    assert not candidate.establishment_gates_passed


@pytest.mark.parametrize(
    ("period_hours", "hours"),
    [
        (2, tuple(range(0, 26, 2))),
        (3, tuple(range(0, 27, 3))),
        (4, tuple(range(0, 36, 4))),
        (6, tuple(range(0, 54, 6))),
        (8, tuple(range(0, 72, 8))),
        (12, tuple(range(0, 180, 12))),
    ],
)
def test_high_gate_count_boundaries(period_hours, hours):
    candidate = score(hours).candidate(period_hours * HOUR)
    assert candidate.high_gates_passed


def test_twelve_hour_high_gate_resolves_every_exact_divisor():
    candidate = score(tuple(range(0, 180, 12))).candidate(12 * HOUR)
    assert {item.divisor_seconds for item in candidate.divisor_resolution} == {
        2 * HOUR, 3 * HOUR, 4 * HOUR, 6 * HOUR
    }
    assert all(item.high_resolved for item in candidate.divisor_resolution)


def test_query_only_crowbar_four_hour_model_establishes_without_outages():
    result = score(tuple(range(0, 36, 4)), outcome=detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART)
    candidate = result.candidate(4 * HOUR)
    assert candidate.establishment_gates_passed
    assert candidate.fundamental_period_confidence >= 0.85
    assert result.selected_period_seconds == 4 * HOUR
    assert result.prediction_usable
    assert result.pattern_status is scoring.PatternStatus.CONFIRMED_PERIOD
    assert result.five_minute_warning_eligible
    assert result.scheduled_classification_eligible


def test_crowbar_single_off_grid_anomaly_does_not_displace_mature_model():
    physical = (*events_at(tuple(range(0, 36, 4)), detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART), event(37.8, 99, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART))
    result = scoring.RestartScheduleScorer().score(physical, full_coverage(0, 38), now=BASE + 38 * HOUR, incumbent_period_seconds=4 * HOUR)
    assert result.selected_period_seconds == 4 * HOUR
    assert result.candidate(4 * HOUR).off_grid_event_count == 1
    assert result.regime_status is not scoring.RegimeStatus.CHANGE_SUSPECTED


def test_dawn_sparse_twelve_hour_hints_do_not_beat_covered_three_hour_evidence():
    sparse = (0, 12, 24, 48, 168)
    covered = (180, 183, 186, 189, 192, 195, 198, 201)
    physical = events_at((*sparse, *covered))
    timeline = full_coverage(180, 201)
    result = scoring.RestartScheduleScorer().score(physical, timeline, now=BASE + 201 * HOUR)
    three = result.candidate(3 * HOUR)
    twelve = result.candidate(12 * HOUR)
    assert three.strict_direct_interval_count == 7
    assert three.fundamental_period_confidence > twelve.fundamental_period_confidence
    assert twelve.strict_direct_interval_count == 0
    assert twelve.hints.event_count > 0
    assert result.selected_period_seconds == 3 * HOUR


def test_uncertain_a2s_interruptions_do_not_disturb_mature_model():
    stable = list(events_at(tuple(range(0, 36, 4))))
    stable.extend(
        event(value, 100 + index, detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION)
        for index, value in enumerate((5, 13, 21, 29))
    )
    result = scoring.RestartScheduleScorer().score(stable, full_coverage(0, 36), now=BASE + 36 * HOUR)
    assert result.candidate(4 * HOUR).strict_direct_interval_count == 8
    assert result.candidate(4 * HOUR).off_grid_event_count == 0


def test_three_to_six_change_is_suspected_before_switch_and_suspends_prediction():
    old = events_at(tuple(range(0, 24, 3)))
    old_score = scoring.RestartScheduleScorer().score(old, full_coverage(0, 21), now=BASE + 21 * HOUR)
    transition = (*old, *events_at((24, 30, 36)))
    result = scoring.RestartScheduleScorer().score(
        transition,
        full_coverage(0, 36),
        now=BASE + 36 * HOUR,
        previous=old_score,
    )
    assert result.regime_status is scoring.RegimeStatus.CHANGE_SUSPECTED
    assert result.selected_period_seconds == 3 * HOUR
    assert not result.prediction_usable


def test_three_to_six_sustained_change_switches_and_retains_prior_regime():
    old = events_at(tuple(range(0, 24, 3)))
    old_score = scoring.RestartScheduleScorer().score(old, full_coverage(0, 21), now=BASE + 21 * HOUR)
    combined = (*old, *events_at((24, 30, 36, 42, 48, 54)))
    result = scoring.RestartScheduleScorer().score(
        combined,
        full_coverage(0, 54),
        now=BASE + 54 * HOUR,
        previous=old_score,
    )
    assert result.selected_period_seconds == 6 * HOUR
    assert result.regime_status is scoring.RegimeStatus.NEW_REGIME_ESTABLISHING
    assert result.prior_regimes[-1].period_seconds == 3 * HOUR
    assert not result.prediction_usable


def test_six_to_three_change_eventually_replaces_old_harmonic():
    old = events_at(tuple(range(0, 48, 6)))
    old_score = scoring.RestartScheduleScorer().score(old, full_coverage(0, 42), now=BASE + 42 * HOUR)
    combined = (*old, *events_at(tuple(range(45, 69, 3))))
    result = scoring.RestartScheduleScorer().score(
        combined,
        full_coverage(0, 66),
        now=BASE + 66 * HOUR,
        previous=old_score,
    )
    assert result.selected_period_seconds == 3 * HOUR
    assert result.prior_regimes[-1].period_seconds == 6 * HOUR
    assert result.candidate(6 * HOUR).strict_direct_interval_count < result.candidate(3 * HOUR).strict_direct_interval_count


def test_four_to_eight_change_uses_midpoint_misses_and_divisor_resolution():
    old = events_at(tuple(range(0, 36, 4)))
    old_score = scoring.RestartScheduleScorer().score(old, full_coverage(0, 32), now=BASE + 32 * HOUR)
    combined = (*old, *events_at((40, 48, 56, 64, 72, 80)))
    result = scoring.RestartScheduleScorer().score(
        combined,
        full_coverage(0, 80),
        now=BASE + 80 * HOUR,
        previous=old_score,
    )
    assert result.selected_period_seconds == 8 * HOUR
    assert result.candidate(4 * HOUR).covered_miss_count >= 2
    assert all(item.established_resolved for item in result.candidate(8 * HOUR).divisor_resolution)


def test_phase_shift_preserves_period_but_suspends_then_recovers_prediction():
    old = events_at(tuple(range(1, 33, 4)))
    old_score = scoring.RestartScheduleScorer().score(old, full_coverage(1, 29), now=BASE + 29 * HOUR)
    mixed = (*old, *events_at((34, 38, 42, 46)))
    transition = scoring.RestartScheduleScorer().score(
        mixed,
        full_coverage(1, 46),
        now=BASE + 46 * HOUR,
        previous=old_score,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    assert transition.selected_period_seconds == 4 * HOUR
    assert transition.candidate(4 * HOUR).phase_confidence < 0.80
    assert not transition.prediction_usable
    new_only = events_at(tuple(range(34, 82, 4)))
    recovered = scoring.RestartScheduleScorer().score(
        new_only, full_coverage(34, 78), now=BASE + 78 * HOUR, incumbent_period_seconds=4 * HOUR
    )
    assert recovered.selected_period_seconds == 4 * HOUR
    assert recovered.candidate(4 * HOUR).phase_confidence >= 0.80


def test_one_anomaly_does_not_trigger_regime_change_and_normal_schedule_recovers():
    stable = events_at(tuple(range(0, 36, 4)))
    initial = scoring.RestartScheduleScorer().score(stable, full_coverage(0, 32), now=BASE + 32 * HOUR)
    disrupted = (*stable, event(34, 99))
    anomaly = scoring.RestartScheduleScorer().score(
        disrupted, full_coverage(0, 34), now=BASE + 34 * HOUR, previous=initial
    )
    assert anomaly.selected_period_seconds == 4 * HOUR
    assert anomaly.regime_status is not scoring.RegimeStatus.CHANGE_SUSPECTED
    resumed = (*disrupted, *events_at((36, 40, 44)))
    recovered = scoring.RestartScheduleScorer().score(
        resumed, full_coverage(0, 44), now=BASE + 44 * HOUR, previous=anomaly
    )
    assert recovered.selected_period_seconds == 4 * HOUR


def test_one_covered_expected_miss_does_not_trigger_regime_change():
    stable = events_at(tuple(range(0, 36, 4)))
    initial = scoring.RestartScheduleScorer().score(
        stable, full_coverage(0, 32), now=BASE + 32 * HOUR
    )
    one_miss = (*stable, event(40, 99))
    result = scoring.RestartScheduleScorer().score(
        one_miss, full_coverage(0, 40), now=BASE + 40 * HOUR, previous=initial
    )
    assert result.candidate(4 * HOUR).covered_miss_count == 1
    assert result.regime_status is not scoring.RegimeStatus.CHANGE_SUSPECTED
    assert result.selected_period_seconds == 4 * HOUR


def test_inactivity_neutral_policy_still_suppresses_after_observed_misses():
    stable = events_at(tuple(range(0, 36, 4)))
    timeline = full_coverage(0, 44)
    scorer = scoring.RestartScheduleScorer()
    initial = scorer.score(
        stable,
        timeline,
        now=BASE + 32 * HOUR,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )
    misses = (
        scoring.CoveredExpectedMiss(4 * HOUR, BASE + 36 * HOUR, "4h:36"),
        scoring.CoveredExpectedMiss(4 * HOUR, BASE + 40 * HOUR, "4h:40"),
    )
    contradicted = scorer.score(
        stable,
        timeline,
        now=BASE + 44 * HOUR,
        previous=initial,
        expected_misses=misses,
        phase_recency_policy=scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL,
    )

    assert contradicted.candidate(4 * HOUR).covered_miss_count == 2
    assert contradicted.candidate(4 * HOUR).fundamental_period_confidence < initial.candidate(
        4 * HOUR
    ).fundamental_period_confidence
    assert not contradicted.prediction_usable


def test_old_aggregate_support_is_bounded_and_cannot_block_recent_change():
    aggregate = scoring.LongTermAggregate(
        candidates=tuple(
            scoring.CandidateAggregate(period, direct_count=1000, direct_weight=1000)
            if period == 3 * HOUR else scoring.CandidateAggregate(period)
            for period in scoring.CANDIDATE_PERIODS
        )
    )
    result = score((0, 6, 12, 18, 24, 30), period=3 * HOUR, aggregate=aggregate)
    three = result.candidate(3 * HOUR)
    six = result.candidate(6 * HOUR)
    assert three.long_term_support_used == 0
    assert six.fundamental_period_confidence > three.fundamental_period_confidence
    assert result.selected_period_seconds == 6 * HOUR


def test_hysteresis_keeps_incumbent_when_challenger_does_not_clear_margin():
    mature = score(tuple(range(0, 36, 4)))
    synthetic_candidates = list(mature.candidates)
    incumbent = mature.candidate(4 * HOUR)
    challenger = mature.candidate(3 * HOUR)
    synthetic_candidates[synthetic_candidates.index(incumbent)] = replace_candidate(incumbent, confidence=0.82, gate=True)
    synthetic_candidates[synthetic_candidates.index(challenger)] = replace_candidate(challenger, confidence=0.88, gate=True)
    selected = scoring._select_candidate(tuple(synthetic_candidates), 0.9, 4 * HOUR, mature, BASE)
    assert selected[0] == 4 * HOUR


def test_hysteresis_challenger_below_eighty_percent_does_not_replace():
    mature = score(tuple(range(0, 36, 4)))
    candidates = list(mature.candidates)
    incumbent = mature.candidate(4 * HOUR)
    challenger = mature.candidate(3 * HOUR)
    candidates[candidates.index(incumbent)] = replace_candidate(incumbent, confidence=0.68, gate=True)
    candidates[candidates.index(challenger)] = replace_candidate(challenger, confidence=0.79, gate=True)
    selected = scoring._select_candidate(tuple(candidates), 0.9, 4 * HOUR, mature, BASE)
    assert selected[0] == 4 * HOUR


def test_hysteresis_low_incumbent_uses_five_point_margin():
    mature = score(tuple(range(0, 36, 4)))
    candidates = list(mature.candidates)
    incumbent = mature.candidate(4 * HOUR)
    challenger = mature.candidate(3 * HOUR)
    candidates[candidates.index(incumbent)] = replace_candidate(incumbent, confidence=0.55, gate=True)
    candidates[candidates.index(challenger)] = replace_candidate(challenger, confidence=0.61, gate=True)
    selected = scoring._select_candidate(tuple(candidates), 0.9, 4 * HOUR, mature, BASE)
    assert selected[0] == 3 * HOUR


def test_unresolved_confidence_tie_suppresses_prediction():
    mature = score(tuple(range(0, 36, 4)))
    candidates = list(mature.candidates)
    four = replace_candidate(mature.candidate(4 * HOUR), confidence=0.84, phase=0.9, gate=True)
    eight = replace_candidate(mature.candidate(8 * HOUR), confidence=0.82, phase=0.9, gate=True)
    candidates[candidates.index(mature.candidate(4 * HOUR))] = four
    candidates[candidates.index(mature.candidate(8 * HOUR))] = eight
    selected = scoring._select_candidate(tuple(candidates), 0.9, None, None, BASE)
    assert selected[2] is scoring.RegimeStatus.PERIOD_UNCERTAIN
    assert not selected[3]


def replace_candidate(candidate, *, confidence, phase=None, gate=None):
    from dataclasses import replace
    return replace(
        candidate,
        fundamental_period_confidence=confidence,
        phase_confidence=candidate.phase_confidence if phase is None else phase,
        establishment_gates_passed=candidate.establishment_gates_passed if gate is None else gate,
    )


def replace_event(value, **changes):
    from dataclasses import replace
    return replace(value, **changes)


def primitive(kind, period, weight=1, observed=BASE, divisor=None):
    return scoring.EvidencePrimitive(kind, period, weight, observed, divisor_seconds=divisor)


def record(seq, primitives=(), samples=()):
    return scoring.DetailedEvidenceRecord(
        event_seq=seq,
        event_id=f"record-{seq}",
        observed_at=BASE + seq,
        outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
        authenticity=0.98,
        samples=tuple(samples),
        primitives=tuple(primitives),
    )


def observation(index):
    return detection.ObservationSample(
        wall_at=BASE + index,
        monotonic_at=index,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key="server:2302",
        info_status=detection.InfoStatus.HEALTHY,
        player_status=detection.FieldStatus.PRESENT,
        players=5,
        max_players=60,
        queue_status=detection.FieldStatus.MISSING,
        queue=None,
    )


def test_aggregate_folding_caps_events_samples_and_advances_high_water():
    records = tuple(
        record(
            seq,
            (primitive(scoring.EvidenceKind.DIRECT, 3 * HOUR),),
            tuple(observation(index) for index in range(60)),
        )
        for seq in range(1, 86)
    )
    folded = scoring.fold_evidence_ledger(scoring.EvidenceLedger(), records)
    assert len(folded.records) == 80
    assert folded.folded_through_event_seq == 5
    assert folded.event_seq == 85
    assert folded.aggregate.candidate(3 * HOUR).direct_count == 5
    assert all(not item.samples for item in folded.records[:-12])
    assert all(len(item.samples) == 48 for item in folded.records[-12:])


def test_fold_is_idempotent_and_does_not_double_apply_after_reload():
    limits = scoring.LedgerLimits(detailed_events=2, events_with_samples=1, samples_per_event=2)
    first = scoring.fold_evidence_ledger(
        scoring.EvidenceLedger(),
        (record(1, (primitive(scoring.EvidenceKind.DIRECT, 3 * HOUR),)), record(2), record(3)),
        limits=limits,
    )
    second = scoring.fold_evidence_ledger(first, (record(1),), limits=limits)
    assert second.folded_through_event_seq == 1
    assert second.aggregate == first.aggregate


def test_fold_preserves_semantic_primitive_totals_not_confidence():
    primitives = (
        primitive(scoring.EvidenceKind.DIRECT, 4 * HOUR, 0.8),
        primitive(scoring.EvidenceKind.EXPECTED_MISS, 4 * HOUR, 1),
        primitive(scoring.EvidenceKind.OFF_GRID, 4 * HOUR, 0.6),
        primitive(scoring.EvidenceKind.PHASE_HINT, 4 * HOUR, 0.1),
    )
    folded = scoring.fold_evidence_ledger(
        scoring.EvidenceLedger(), (record(1, primitives), record(2)), limits=scoring.LedgerLimits(1, 0, 0)
    )
    aggregate = folded.aggregate.candidate(4 * HOUR)
    assert aggregate.direct_weight == 0.8
    assert aggregate.miss_penalty == 1
    assert aggregate.off_grid_penalty == 0.6
    assert aggregate.hint_weight == 0.1
    assert aggregate.first_hint_at == BASE
    assert aggregate.last_hint_at == BASE
    assert not hasattr(aggregate, "confidence")


def test_fold_rejects_conflicting_or_malformed_sequences_and_versions():
    with pytest.raises(ValueError):
        scoring.DetailedEvidenceRecord(0, "bad", BASE, detection.EventOutcome.INCOMPLETE, 0.1)
    ledger = scoring.EvidenceLedger(records=(record(1),))
    with pytest.raises(ValueError):
        scoring.fold_evidence_ledger(ledger, (replace_record(record(1), event_id="different"),))
    with pytest.raises(ValueError):
        scoring.fold_evidence_ledger(scoring.EvidenceLedger(scoring_semantics_version=99))


def replace_record(value, **changes):
    from dataclasses import replace
    return replace(value, **changes)


def test_folded_prior_regime_summary_is_retained():
    regime = scoring.RegimeSummary(3 * HOUR, BASE, 0.9, 8, "changed")
    aggregate = scoring.LongTermAggregate(prior_regimes=(regime,))
    ledger = scoring.EvidenceLedger(aggregate=aggregate)
    result = scoring.fold_evidence_ledger(ledger, (record(1),))
    assert result.aggregate.prior_regimes == (regime,)


def test_bounded_scoring_history_applies_reviewed_limits_and_debug_policy():
    coverage = tuple(
        scoring.CoverageSegment(BASE + index, BASE + index + 1, scoring.CoverageKind.ONLINE_HEALTHY)
        for index in range(130)
    )
    rejected = tuple(
        scoring.DetailedEvidenceRecord(
            index,
            f"rejected-{index}",
            BASE + index,
            detection.EventOutcome.AMBIGUOUS_DRAIN,
            0.2,
        )
        for index in range(1, 36)
    )
    misses = tuple(
        primitive(scoring.EvidenceKind.EXPECTED_MISS, 3 * HOUR, observed=BASE + index)
        for index in range(70)
    )
    residuals = tuple(
        scoring.PhaseResidualRecord(period, BASE + index, index)
        for period in scoring.CANDIDATE_PERIODS
        for index in range(40)
    )
    debug = tuple(scoring.DebugRecord(BASE + index, f"debug-{index}") for index in range(50))
    history = scoring.BoundedScoringHistory(coverage, rejected, misses, residuals, debug)
    bounded = scoring.bound_scoring_history(history, now=BASE + 130, debug_enabled=False)
    assert len(bounded.coverage_segments) == 120
    assert len(bounded.rejected_summaries) == 30
    assert len(bounded.expected_miss_details) == 60
    assert all(
        sum(item.period_seconds == period for item in bounded.phase_residuals) == 32
        for period in scoring.CANDIDATE_PERIODS
    )
    assert bounded.debug_records == ()
    debug_bounded = scoring.bound_scoring_history(history, now=BASE + 130, debug_enabled=True)
    assert len(debug_bounded.debug_records) == 40


def test_coverage_history_drops_segments_older_than_sixty_days():
    history = scoring.BoundedScoringHistory(
        coverage_segments=(
            scoring.CoverageSegment(BASE, BASE + 1, scoring.CoverageKind.ONLINE_HEALTHY),
            scoring.CoverageSegment(BASE + 61 * DAY, BASE + 61 * DAY + 1, scoring.CoverageKind.ONLINE_HEALTHY),
        )
    )
    bounded = scoring.bound_scoring_history(history, now=BASE + 61 * DAY + 1)
    assert len(bounded.coverage_segments) == 1


def test_models_and_outputs_are_immutable_and_have_semantics_versions():
    result = score((0, 3, 6))
    with pytest.raises(FrozenInstanceError):
        result.selected_period_seconds = 6 * HOUR
    ledger = scoring.EvidenceLedger()
    assert ledger.scoring_semantics_version == scoring.SCORING_SEMANTICS_VERSION
    assert ledger.aggregate_semantics_version == scoring.AGGREGATE_SEMANTICS_VERSION


def test_stage_three_module_has_no_runtime_or_ui_dependencies():
    names = set(scoring.__dict__)
    for forbidden in ("Gtk", "GLib", "window", "config", "audio", "Path", "os"):
        assert forbidden not in names
