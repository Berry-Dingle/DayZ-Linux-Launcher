from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher.window import DZLLWindow


SERVER = "crowbar.example:2502"
APP = "crowbar-app"
MONITOR = "crowbar-monitor"
POLL = 4
CHAIN = "crowbar-chain"
BASE = 2_200_000_000.0
PERIOD = 3 * scoring.HOUR


def sample(at: float, players: int | None, *, info=detection.InfoStatus.HEALTHY):
    present = players is not None
    return detection.ObservationSample(
        wall_at=BASE + at,
        monotonic_at=at,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        server_key=SERVER,
        info_status=info,
        player_status=(
            detection.FieldStatus.PRESENT
            if present
            else detection.FieldStatus.MISSING
        ),
        players=players,
        max_players=60,
        continuity_chain_id=CHAIN,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def feed(engine, values):
    events = []
    for value in values:
        events.extend(engine.ingest(value))
    return events


def valid_visible_event(*, wall_shift=0.0):
    engine = detection.PhysicalEpisodeEngine()
    observations = [
        sample(0, 12),
        sample(10, 12),
        sample(20, 12),
        sample(40, 11),
        sample(60, 9),
        sample(80, 7),
        sample(100, 5),
        sample(120, 3),
        sample(140, 0),
        sample(150, 0),
        sample(160, 0),
        sample(170, 0),
        sample(180, 0),
        sample(190, 0),
        sample(200, 0),
        sample(210, 3),
        sample(220, 6),
    ]
    if wall_shift:
        observations = [
            replace(value, wall_at=value.wall_at + wall_shift) for value in observations
        ]
    events = feed(engine, observations)
    assert len(events) == 1
    assert events[0].finalized_at == BASE + wall_shift + 220
    assert engine.tick(250, BASE + wall_shift + 250) == ()
    return events[0]


def no_repopulation_event(*, wall_shift=0.0):
    engine = detection.PhysicalEpisodeEngine()
    observations = [
        sample(0, 12),
        sample(10, 12),
        sample(20, 12),
        sample(40, 11),
        sample(60, 9),
        sample(80, 7),
        sample(100, 5),
        sample(120, 3),
        sample(140, 0),
        *[sample(at, 0) for at in range(150, 741, 10)],
    ]
    if wall_shift:
        observations = [
            replace(value, wall_at=value.wall_at + wall_shift)
            for value in observations
        ]
    events = feed(engine, observations)
    assert len(events) == 1
    return events[0]


def test_valid_drain_sustained_zero_and_repopulation_completes_visible_restart():
    event = valid_visible_event()

    assert event.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert event.drain.zero_reached
    assert event.drain.low_duration >= 45
    assert event.recovery.positive_sample_count == 2
    assert event.recovery.stable_recovery_at == BASE + 220
    assert event.canonical_phase_at == BASE + 140
    assert event.canonical_phase_at == event.drain.low_started_at
    assert event.canonical_phase_at != event.recovery.first_player_at
    assert event.outage.first_failure_at is None
    assert event.query_health.continuous
    assert event.schedule_weight_suggestion < 1.0


def test_query_visible_recovery_delay_does_not_move_restart_start_phase():
    def completed(first_repopulation):
        engine = detection.PhysicalEpisodeEngine()
        values = [
            sample(0, 12), sample(10, 12), sample(20, 12),
            *[sample(at, 0) for at in range(30, first_repopulation, 10)],
            sample(first_repopulation, 3),
            sample(first_repopulation + 10, 6),
        ]
        return feed(engine, values)[0]

    quick = completed(90)
    slow = completed(300)

    assert quick.recovery.first_player_at == BASE + 90
    assert slow.recovery.first_player_at == BASE + 300
    assert quick.canonical_phase_at == slow.canonical_phase_at == BASE + 30


def test_unqualified_query_visible_collapse_never_contributes_start_phase():
    engine = detection.PhysicalEpisodeEngine()

    events = feed(
        engine,
        (sample(0, 20), sample(10, 20), sample(20, 20), sample(30, 0), sample(40, 20)),
    )

    assert events == []
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_live_crowbar_pre_candidate_drain_history_qualifies_visible_restart():
    engine = detection.PhysicalEpisodeEngine()
    counts = (
        60, 60, 60,
        59, 58, 57, 56, 55, 54, 53, 52, 51, 50,
        42,
        0, 0, 0, 0, 0,
        5, 11, 14, 20, 30, 40, 48,
    )

    events = feed(
        engine,
        [sample(index * 15, players) for index, players in enumerate(counts)],
    )

    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert events[0].drain.baseline_players >= 50
    assert events[0].drain.zero_reached
    assert events[0].recovery.stable_recovery_at is not None


@pytest.mark.parametrize(
    ("baseline", "first_repopulation", "stable_repopulation", "outcome"),
    [
        (20, 3, 10, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
        (4, 2, 2, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART),
    ],
)
def test_decisive_populated_collapse_qualifies_without_stepped_drain(
    baseline,
    first_repopulation,
    stable_repopulation,
    outcome,
):
    engine = detection.PhysicalEpisodeEngine()
    counts = (
        baseline, baseline, baseline,
        0, 0, 0, 0, 0, 0,
        first_repopulation, stable_repopulation,
    )

    events = feed(
        engine,
        [sample(index * 10, players) for index, players in enumerate(counts)],
    )

    assert len(events) == 1
    assert events[0].outcome is outcome
    assert events[0].drain.baseline_players == baseline
    assert events[0].drain.abrupt
    assert events[0].drain.zero_reached
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_live_single_large_collapse_after_populated_decline_qualifies():
    engine = detection.PhysicalEpisodeEngine()
    counts = (43, 41, 39, 37, 36, 9, 0, 0, 0, 0, 0, 0, 5, 22)

    events = feed(
        engine,
        [sample(index * 10, players) for index, players in enumerate(counts)],
    )

    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert events[0].drain.zero_reached


def test_one_isolated_zero_followed_by_immediate_recovery_does_not_qualify():
    engine = detection.PhysicalEpisodeEngine()

    events = feed(engine, (
        sample(0, 20), sample(10, 20), sample(20, 20),
        sample(30, 0), sample(40, 20),
    ))

    assert events == []
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def close_collapse_attribution_anomaly(*, wall_shift=0.0):
    engine = detection.PhysicalEpisodeEngine()
    observations = [
        sample(0, 45), sample(10, 45), sample(20, 45),
        sample(30, 0), sample(60, 0),
        sample(70, 5), sample(90, 10), sample(110, 15),
        sample(130, 21), sample(150, 26), sample(170, 32),
        sample(190, 34), sample(200, 32),
        sample(210, 0), sample(220, 0),
    ]
    if wall_shift:
        observations = [
            replace(value, wall_at=value.wall_at + wall_shift)
            for value in observations
        ]
    events = feed(engine, observations)
    assert len(events) == 1
    return events[0], engine


def test_close_collapse_pair_is_one_schedule_neutral_attribution_anomaly():
    event, engine = close_collapse_attribution_anomaly()

    assert event.outcome is detection.EventOutcome.INCOMPLETE
    assert event.schedule_weight_suggestion == 0.0
    assert "query_visible_close_collapse_attribution_anomaly" in event.reason_codes
    assert (
        "query_visible_low_run_interrupted_by_positive_recovery"
        in event.reason_codes
    )
    assert event.recovery.stable_recovery_at is None
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_separated_low_runs_are_never_summed_as_sustained_low_duration():
    event, _engine = close_collapse_attribution_anomaly()

    assert event.drain.low_started_at == BASE + 210
    assert event.drain.low_duration == 10
    assert event.drain.low_duration < 45


def test_close_collapse_anomaly_does_not_change_existing_schedule_learning():
    strong = tuple(
        valid_visible_event(wall_shift=index * PERIOD) for index in range(4)
    )
    anomaly, _engine = close_collapse_attribution_anomaly(
        wall_shift=4 * PERIOD
    )
    coverage = scoring.CoverageTimeline((
        scoring.CoverageSegment(
            strong[0].canonical_phase_at - 60,
            anomaly.finalized_at + 60,
            scoring.CoverageKind.ONLINE_HEALTHY,
        ),
    ))
    scorer = scoring.RestartScheduleScorer()
    before = scorer.score(
        strong,
        coverage,
        now=strong[-1].finalized_at,
        incumbent_period_seconds=PERIOD,
    )
    after = scorer.score(
        (*strong, anomaly),
        coverage,
        now=anomaly.finalized_at,
        incumbent_period_seconds=PERIOD,
    )
    before_candidate = before.candidate(PERIOD)
    after_candidate = after.candidate(PERIOD)
    relationships = continuity.extract_interval_relationships(
        (*strong, anomaly),
        (),
        candidate_periods=(PERIOD,),
    )

    assert after.schedule_existence_confidence == before.schedule_existence_confidence
    assert after.selected_period_seconds == before.selected_period_seconds
    assert after.incumbent_period_seconds == before.incumbent_period_seconds
    assert after.regime_status == before.regime_status
    assert after_candidate.fundamental_period_confidence == (
        before_candidate.fundamental_period_confidence
    )
    assert after_candidate.phase_offset == before_candidate.phase_offset
    assert after_candidate.phase_confidence == before_candidate.phase_confidence
    assert after_candidate.covered_miss_count == before_candidate.covered_miss_count
    assert after_candidate.hints.event_count == before_candidate.hints.event_count
    anomaly_relationships = tuple(
        item
        for item in relationships
        if anomaly.event_id in {item.left_event_id, item.right_event_id}
    )
    assert anomaly_relationships == ()


def test_close_collapse_anomaly_is_not_a_period_relationship_endpoint():
    anomaly, _engine = close_collapse_attribution_anomaly()
    left = valid_visible_event(wall_shift=-PERIOD)

    relationships = continuity.extract_interval_relationships((left, anomaly), ())

    assert relationships == ()


def test_single_short_collapse_below_clear_level_stays_neutral_without_second_collapse():
    engine = detection.PhysicalEpisodeEngine()
    observations = [
        sample(0, 45), sample(10, 45), sample(20, 45),
        sample(30, 0), sample(60, 0),
        sample(70, 5), sample(90, 10), sample(110, 15),
        sample(130, 21), sample(150, 26), sample(170, 32),
        sample(190, 34), sample(640, 34),
    ]

    assert feed(engine, observations) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_decisive_collapse_without_repopulation_keeps_weak_evidence_behavior():
    engine = detection.PhysicalEpisodeEngine()
    observations = [
        sample(0, 20), sample(10, 20), sample(20, 20), sample(30, 0),
        *[sample(at, 0) for at in range(40, 631, 10)],
    ]

    events = feed(engine, observations)

    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert events[0].recovery.stable_recovery_at is None
    assert events[0].canonical_phase_at == BASE + 30
    assert "query_visible_repopulation_timeout" in events[0].reason_codes


def test_visible_restart_does_not_wait_for_shared_finalization_grace():
    event = valid_visible_event()

    assert event.finalized_at == event.recovery.stable_recovery_at


def test_drain_zero_and_repopulation_within_ten_minute_grace_stays_normal():
    event = valid_visible_event()
    host, alerts, _fired = query_alert_host()

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(event,)),
        {"online": True, "players": 6},
    )

    assert event.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert event.recovery.stable_recovery_at == BASE + 220
    assert event.recovery.stable_recovery_at - event.drain.low_started_at < 600
    assert len(alerts) == 1
    assert alerts[0][1] == "query-visible rejoin"


def test_no_repopulation_by_grace_expiry_becomes_weak_drain_evidence_without_alert():
    event = no_repopulation_event()
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(event,)),
        {"online": True, "players": 0},
    )

    assert event.outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert event.recovery.stable_recovery_at is None
    assert event.schedule_weight_suggestion == 0.15
    assert "query_visible_repopulation_timeout" in event.reason_codes
    assert alerts == []
    assert fired == set()


def test_no_repopulation_weak_evidence_anchors_to_drain_not_grace_expiry():
    event = no_repopulation_event()

    assert event.drain.low_started_at == BASE + 140
    assert event.canonical_phase_at == event.drain.low_started_at
    assert event.finalized_at == BASE + 740
    assert event.canonical_phase_at != event.finalized_at


def test_late_join_after_expired_drain_does_not_retroactively_complete_it():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 12), sample(10, 12), sample(20, 12), sample(40, 11),
        sample(60, 9), sample(80, 7), sample(100, 5), sample(120, 3),
        sample(140, 0), *[sample(at, 0) for at in range(150, 741, 10)],
    ]
    events = feed(engine, values)
    events.extend(feed(engine, (sample(900, 3), sample(910, 6))))

    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert events[0].recovery.stable_recovery_at is None


def test_drain_without_repopulation_never_completes_restart_event():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 12), sample(10, 12), sample(20, 12), sample(50, 10),
        sample(80, 8), sample(110, 5), sample(140, 0), sample(160, 0),
        sample(180, 0), sample(200, 0), sample(451, 0),
    ]
    assert feed(engine, values) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is not None
    assert not engine.provisional_drain.standalone_coverage_complete


def test_low_population_noise_and_brief_low_do_not_qualify():
    engine = detection.PhysicalEpisodeEngine()
    values = [sample(index * 10, count) for index, count in enumerate(
        (1, 1, 0, 1, 0, 1, 4, 3, 4, 2, 4, 4)
    )]
    assert feed(engine, values) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_stable_zero_before_window_and_late_join_never_complete_visible_restart():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 0),
        sample(10, 0),
        sample(20, 0),
        sample(600, 0),
        sample(1200, 0),
        sample(1800, 0),
        sample(3600, 3),
        sample(3610, 6),
    ]

    assert feed(engine, values) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is None


def test_abrupt_crash_without_drain_is_only_strong_outage_evidence():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 12), sample(10, 12), sample(20, 12)])
    assert engine.ingest(sample(30, None, info=detection.InfoStatus.TIMEOUT)) == ()
    assert engine.ingest(sample(40, None, info=detection.InfoStatus.NETWORK_ERROR)) == ()
    engine.ingest(sample(60, 4))
    event = engine.tick(90, BASE + 90)[0]

    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.drain.drain_at is None
    assert event.outage.confirmed_offline_at is not None


def test_polling_gap_during_candidate_invalidates_visible_restart():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 12), sample(10, 12), sample(20, 12), sample(50, 10),
        sample(80, 8), sample(110, 5), sample(140, 0), sample(150, 0),
        sample(160, 0), sample(170, 0), sample(180, 0), sample(190, 0),
        sample(200, 0), sample(250, 6), sample(260, 9),
    ]
    assert feed(engine, values) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is not None
    assert not engine.provisional_drain.standalone_coverage_complete


def test_missing_player_coverage_cannot_be_bridged_by_visible_candidate():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 12), sample(10, 12), sample(20, 12), sample(50, 10),
        sample(80, 8), sample(110, 5), sample(140, 0), sample(150, 0),
        sample(160, None), sample(170, 0), sample(180, 0), sample(190, 0),
        sample(200, 0), sample(210, 6), sample(220, 9),
    ]
    assert feed(engine, values) == []
    assert engine.active_episode is None


def test_drain_followed_by_outage_stays_one_corroborated_outage_event():
    engine = detection.PhysicalEpisodeEngine()
    values = [
        sample(0, 12), sample(10, 12), sample(20, 12), sample(50, 10),
        sample(80, 8), sample(110, 5), sample(140, 0), sample(160, 0),
        sample(180, 0), sample(200, 0),
    ]
    feed(engine, values)
    engine.ingest(sample(210, None, info=detection.InfoStatus.TIMEOUT))
    engine.ingest(sample(220, None, info=detection.InfoStatus.NETWORK_ERROR))
    engine.ingest(sample(240, 6))
    event = engine.tick(270, BASE + 270)[0]

    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.drain.inherited
    assert event.outage.confirmed_offline_at == BASE + 220


def test_repeated_visible_cycles_raise_normal_evidence_slowly_and_never_high_authority():
    events = tuple(valid_visible_event(wall_shift=index * PERIOD) for index in range(4))
    coverage = scoring.CoverageTimeline((
        scoring.CoverageSegment(
            events[0].canonical_phase_at - 60,
            events[-1].canonical_phase_at + 60,
            scoring.CoverageKind.ONLINE_HEALTHY,
        ),
    ))
    scorer = scoring.RestartScheduleScorer()
    two = scorer.score(events[:2], coverage, now=events[1].finalized_at)
    four = scorer.score(events, coverage, now=events[-1].finalized_at)
    relationships = continuity.extract_interval_relationships(
        events,
        (),
        candidate_periods=(PERIOD,),
    )

    assert four.candidate(PERIOD).fundamental_period_confidence > two.candidate(
        PERIOD
    ).fundamental_period_confidence
    assert not two.prediction_usable
    assert relationships
    assert all(not item.high_authority_eligible for item in relationships)


def _window_span(start, end):
    return continuity.ContinuitySpan(
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
        reference_id=f"span-{start}-{end}",
        server_key=SERVER,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=CHAIN,
        start_at=start,
        end_at=end,
        start_monotonic=0,
        end_monotonic=end - start,
        kind=continuity.ContinuitySpanKind.ONLINE_HEALTHY,
        cadence_seconds=10,
        reason_codes=("synthetic_healthy_coverage",),
    )


def _classify_window(event, expected):
    return windows.classify_expected_window(
        server_key=SERVER,
        candidate_period_seconds=PERIOD,
        expected_phase_offset=expected % PERIOD,
        window_start_at=expected - 9 * 60,
        expected_at=expected,
        window_end_at=expected + 9 * 60,
        model_revision_id="crowbar-model",
        events=(event,),
        spans=(_window_span(expected - 9 * 60, expected + 9 * 60),),
        episodes=(),
    )


def test_visible_candidate_outside_expected_window_does_not_count_as_hit():
    event = valid_visible_event()
    result = _classify_window(event, event.canonical_phase_at + 30 * 60)

    assert result.outcome is windows.ExpectedWindowOutcome.GENUINE_MISS
    assert result.qualifying_finalized_event_id is None


def test_visible_candidate_near_expected_window_counts_as_hit():
    event = valid_visible_event()
    result = _classify_window(event, event.canonical_phase_at + 60)

    assert result.outcome is windows.ExpectedWindowOutcome.HIT
    assert result.qualifying_finalized_event_id == event.event_id


def test_query_visible_intermediate_window_without_population_transition_is_neutral():
    events = (
        runtime._deserialize_event(runtime._serialize_event(valid_visible_event(), include_samples=False)),
        runtime._deserialize_event(
            runtime._serialize_event(valid_visible_event(wall_shift=2 * PERIOD), include_samples=False)
        ),
    )
    coverage = scoring.CoverageTimeline((
        scoring.CoverageSegment(
            events[0].canonical_phase_at,
            events[1].canonical_phase_at,
            scoring.CoverageKind.ONLINE_HEALTHY,
        ),
    ))

    score = scoring.RestartScheduleScorer().score(
        events,
        coverage,
        now=events[-1].finalized_at,
    )
    candidate = score.candidate(PERIOD)

    assert candidate.covered_miss_count == 0
    assert candidate.compatible_multiple_interval_count == 0
    assert candidate.multiple_interval_diagnostics[0].intermediate_windows[0].status is (
        scoring.IntermediateWindowStatus.POPULATION_TRANSITION_UNOBSERVABLE
    )


def test_complete_healthy_window_is_unknown_when_query_population_transition_unobservable():
    expected = BASE + PERIOD
    result = windows.classify_expected_window(
        server_key=SERVER,
        candidate_period_seconds=PERIOD,
        expected_phase_offset=expected % PERIOD,
        window_start_at=expected - 9 * 60,
        expected_at=expected,
        window_end_at=expected + 9 * 60,
        model_revision_id="crowbar-zero-neutral-model",
        spans=(_window_span(expected - 9 * 60, expected + 9 * 60),),
        population_transition_observable=False,
    )

    assert result.outcome is windows.ExpectedWindowOutcome.UNKNOWN
    assert not result.negative_penalty_active
    assert "query_visible_population_transition_unobservable" in result.reason_codes


def test_no_repopulation_weak_case_blocks_miss_without_becoming_a_hit():
    event = no_repopulation_event()
    result = _classify_window(event, event.canonical_phase_at)

    assert result.outcome is windows.ExpectedWindowOutcome.AMBIGUOUS
    assert not result.negative_penalty_active
    assert result.qualifying_finalized_event_id is None


def test_repeated_weak_drains_add_small_support_without_shifting_phase():
    strong = tuple(valid_visible_event(wall_shift=index * PERIOD) for index in range(4))
    weak = tuple(
        no_repopulation_event(wall_shift=index * PERIOD)
        for index in range(3)
    )
    coverage = scoring.CoverageTimeline((
        scoring.CoverageSegment(
            strong[0].canonical_phase_at - 60,
            strong[-1].canonical_phase_at + 60,
            scoring.CoverageKind.ONLINE_HEALTHY,
        ),
    ))
    scorer = scoring.RestartScheduleScorer()
    before = scorer.score(strong, coverage, now=strong[-1].finalized_at)
    after = scorer.score((*strong, *weak), coverage, now=strong[-1].finalized_at)
    before_candidate = before.candidate(PERIOD)
    after_candidate = after.candidate(PERIOD)

    assert after_candidate.phase_offset == before_candidate.phase_offset
    contribution = (
        after_candidate.fundamental_period_confidence
        - before_candidate.fundamental_period_confidence
    )
    assert 0 < round(contribution, 6) <= 0.03
    assert after_candidate.hints.event_count == 3


def test_completed_visible_event_preserves_schema_compatible_round_trip():
    event = valid_visible_event()
    payload = runtime._serialize_event(event, include_samples=True)
    restored = runtime._deserialize_event(payload)

    assert restored == event
    assert restored.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART


def query_alert_host(*, server_key=SERVER, dispatch=None):
    fired = set()
    alerts = []

    def suppression_key(_server_key, *, kind, event_id):
        return kind.value, event_id

    def mark_fired(_server_key, key, *, now):
        del now
        if key in fired:
            return False
        fired.add(key)
        return True

    runtime_stub = SimpleNamespace(
        event_suppression_key=suppression_key,
        mark_fired=mark_fired,
    )
    if dispatch is None:
        dispatch = lambda snapshot, alert_type="back online": alerts.append(
            (dict(snapshot), alert_type)
        )
    host = SimpleNamespace(
        _server_companion_restart_alert_enabled=True,
        _server_companion_recovery_alert_suppressed=True,
        _server_companion_recovery_online_since=44.0,
        _companion_restart_phase2=runtime_stub,
        _server_companion_restart_learning_key=lambda: server_key,
        _server_companion_alert_back_online=dispatch,
        _debug_server_companion_alert=lambda *_args: None,
        _refresh_server_companion_restart_learning_summary=lambda: None,
        _server_companion_is_validated_query_visible_recovery=(
            DZLLWindow._server_companion_is_validated_query_visible_recovery
        ),
    )
    return host, alerts, fired


def test_completed_query_visible_recovery_fires_existing_online_alert_immediately():
    event = valid_visible_event()
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(event,)),
        {"name": "Crowbar", "online": True, "players": 6},
    )

    assert len(alerts) == 1
    assert alerts[0][1] == "query-visible rejoin"
    assert fired == {("generic_recovery", event.event_id)}


def test_qualified_first_positive_repopulation_alerts_before_stable_recovery():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, (
        sample(0, 12), sample(10, 12), sample(20, 12), sample(40, 11),
        sample(60, 9), sample(80, 7), sample(100, 5), sample(120, 3),
        sample(140, 0), sample(150, 0), sample(160, 0), sample(170, 0),
        sample(180, 0), sample(190, 0), sample(200, 0),
    ))
    assert engine.ingest(sample(210, 3)) == ()
    episode = engine.active_episode
    assert episode is not None
    assert episode.stable_recovery_mono is None
    assert 3 < episode.baseline_players * engine.config.drain_recovery_fraction
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id=episode.event_id),
        {"name": "Crowbar", "online": True, "players": 3},
    )

    assert len(alerts) == 1
    assert alerts[0][1] == "query-visible rejoin"
    assert alerts[0][0]["players"] == 3
    assert fired == {("generic_recovery", episode.event_id)}


def test_abrupt_collapse_alerts_on_first_repopulation_without_later_duplicate():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, (
        sample(0, 20), sample(10, 20), sample(20, 20),
        sample(30, 0), sample(40, 0), sample(50, 0), sample(60, 0),
        sample(70, 0), sample(80, 0),
    ))

    assert engine.ingest(sample(90, 3)) == ()
    episode = engine.active_episode
    assert episode is not None
    assert episode.stable_recovery_mono is None
    event_id = episode.event_id
    host, alerts, fired = query_alert_host()
    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id=event_id),
        {"name": "Crowbar", "online": True, "players": 3},
    )

    events = engine.ingest(sample(100, 10))
    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=events),
        {"name": "Crowbar", "online": True, "players": 10},
    )

    assert len(events) == 1
    assert events[0].event_id == event_id
    assert len(alerts) == 1
    assert alerts[0][0]["players"] == 3
    assert fired == {("generic_recovery", event_id)}


def test_later_stable_query_visible_finalization_does_not_duplicate_early_alert():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, (
        sample(0, 12), sample(10, 12), sample(20, 12), sample(40, 11),
        sample(60, 9), sample(80, 7), sample(100, 5), sample(120, 3),
        sample(140, 0), sample(150, 0), sample(160, 0), sample(170, 0),
        sample(180, 0), sample(190, 0), sample(200, 0),
    ))
    assert engine.ingest(sample(210, 3)) == ()
    event_id = engine.active_episode.event_id
    host, alerts, fired = query_alert_host()
    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id=event_id),
        {"online": True, "players": 3},
    )

    events = engine.ingest(sample(220, 6))
    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=events),
        {"online": True, "players": 6},
    )

    assert len(events) == 1
    assert events[0].event_id == event_id
    assert len(alerts) == 1
    assert fired == {("generic_recovery", event_id)}


def test_positive_player_without_qualified_candidate_does_not_alert():
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id=None),
        {"online": True, "players": 3},
    )

    assert alerts == []
    assert fired == set()


def test_duplicate_query_visible_finalization_does_not_alert_again():
    event = valid_visible_event()
    host, alerts, _fired = query_alert_host()
    update = SimpleNamespace(finalized_events=(event,))

    DZLLWindow._handle_phase2_finalized_events(host, update, {"online": True})
    DZLLWindow._handle_phase2_finalized_events(host, update, {"online": True})

    assert len(alerts) == 1


def test_incomplete_query_visible_candidate_produces_no_alert_event():
    engine = detection.PhysicalEpisodeEngine()
    events = feed(engine, (
        sample(0, 12), sample(10, 12), sample(20, 12), sample(50, 10),
        sample(80, 8), sample(110, 5), sample(140, 0), sample(160, 0),
        sample(180, 0), sample(200, 0),
    ))
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=tuple(events)),
        {"online": True, "players": 0},
    )

    assert events == []
    assert alerts == []
    assert fired == set()


def test_query_visible_alert_does_not_touch_strong_recovery_rearm_state():
    event = valid_visible_event()
    host, alerts, _fired = query_alert_host()
    suppressed_before = host._server_companion_recovery_alert_suppressed
    online_since_before = host._server_companion_recovery_online_since

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(event,)),
        {"online": True},
    )

    assert len(alerts) == 1
    assert host._server_companion_recovery_alert_suppressed is suppressed_before
    assert host._server_companion_recovery_online_since == online_since_before


def test_query_visible_later_finalization_keeps_existing_suppression():
    event = valid_visible_event()
    host, alerts, fired = query_alert_host()
    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id=event.event_id),
        {"online": True, "players": 3},
    )

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(event,)),
        {"online": True, "players": 6},
    )

    assert len(alerts) == 1
    assert fired == {("generic_recovery", event.event_id)}


def test_query_visible_alert_dispatch_failure_keeps_durable_reservation():
    def fail_dispatch(_snapshot, *, alert_type):
        del alert_type
        raise RuntimeError("synthetic dispatch failure")

    host, alerts, fired = query_alert_host(dispatch=fail_dispatch)

    with pytest.raises(RuntimeError, match="synthetic dispatch failure"):
        DZLLWindow._handle_phase2_query_visible_repopulation_alert(
            host,
            SimpleNamespace(query_visible_repopulation_event_id="failed-event"),
            {"online": True, "players": 5},
        )
    assert alerts == []
    assert fired == {("generic_recovery", "failed-event")}


def test_query_visible_alert_dispatch_works_for_normal_server_key():
    host, alerts, fired = query_alert_host()

    DZLLWindow._handle_phase2_query_visible_repopulation_alert(
        host,
        SimpleNamespace(query_visible_repopulation_event_id="other-event"),
        {"online": True, "players": 5},
    )

    assert len(alerts) == 1
    assert fired == {("generic_recovery", "other-event")}
