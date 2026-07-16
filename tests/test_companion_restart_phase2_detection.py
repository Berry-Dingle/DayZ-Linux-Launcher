from dataclasses import FrozenInstanceError
import math

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection


BASE_WALL = 1_790_000_000.0
MISSING = object()


def sample(
    at,
    players=MISSING,
    *,
    info=detection.InfoStatus.HEALTHY,
    queue=MISSING,
    lifecycle=detection.LifecycleMarker.NORMAL,
    app="app-1",
    monitor="monitor-1",
    generation=1,
    server="server:2302",
    max_players=60,
):
    if players is MISSING:
        player_status = detection.FieldStatus.MISSING
        player_count = None
    else:
        player_status = detection.FieldStatus.PRESENT
        player_count = players
    if queue is MISSING:
        queue_status = detection.FieldStatus.MISSING
        queue_count = None
    else:
        queue_status = detection.FieldStatus.PRESENT
        queue_count = queue
    return detection.ObservationSample(
        wall_at=BASE_WALL + at,
        monotonic_at=float(at),
        app_session_id=app,
        monitoring_session_id=monitor,
        poll_generation=generation,
        server_key=server,
        info_status=info,
        info_latency=0.04 if info is detection.InfoStatus.HEALTHY else None,
        player_status=player_status,
        players=player_count,
        max_players=max_players,
        queue_status=queue_status,
        queue=queue_count,
        lifecycle=lifecycle,
    )


def feed(engine, observations):
    events = []
    for observation in observations:
        events.extend(engine.ingest(observation))
    return events


def populated(engine, counts=(12, 12, 12), start=0):
    feed(engine, [sample(start + index * 10, value) for index, value in enumerate(counts)])


def crowbar_sequence(engine, *, baseline=12, drain_at=30, recover_at=200, queue=MISSING):
    populated(engine, (baseline, baseline, baseline))
    observations = [
        sample(drain_at, 0),
        sample(drain_at + 30, 0),
        sample(drain_at + 60, 0),
        sample(recover_at, 2, queue=queue),
        sample(recover_at + 10, 4, queue=queue),
    ]
    feed(engine, observations)
    return engine.tick(recover_at + 40, BASE_WALL + recover_at + 40)


def offline_sequence(engine, *, drain=True, queue=MISSING, recovery_players=3):
    populated(engine)
    observations = []
    if drain:
        observations.extend([sample(30, 0), sample(60, 0)])
    observations.extend(
        [
            sample(90, 0 if drain else MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(96, 0 if drain else MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(120, recovery_players if drain else MISSING, queue=queue),
        ]
    )
    feed(engine, observations)
    return engine.tick(150, BASE_WALL + 150)


def test_observation_distinguishes_zero_from_missing_and_is_immutable():
    zero = sample(0, 0)
    missing = sample(0, MISSING)

    assert zero.player_status is detection.FieldStatus.PRESENT
    assert zero.players == 0
    assert missing.player_status is detection.FieldStatus.MISSING
    assert missing.players is None
    with pytest.raises(FrozenInstanceError):
        zero.players = 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wall_at", -1),
        ("wall_at", math.nan),
        ("monotonic_at", math.inf),
        ("players", -1),
        ("max_players", -1),
        ("queue", -1),
        ("poll_generation", -1),
    ],
)
def test_observation_rejects_invalid_numeric_values(field, value):
    values = {
        "wall_at": BASE_WALL,
        "monotonic_at": 0,
        "app_session_id": "app",
        "monitoring_session_id": "monitor",
        "poll_generation": 1,
        "server_key": "server",
        "info_status": detection.InfoStatus.HEALTHY,
        "player_status": detection.FieldStatus.PRESENT,
        "players": 0,
        "max_players": 60,
        "queue_status": detection.FieldStatus.PRESENT,
        "queue": 0,
    }
    values[field] = value
    with pytest.raises(ValueError):
        detection.ObservationSample(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"player_status": detection.FieldStatus.PRESENT, "players": None},
        {"player_status": detection.FieldStatus.MISSING, "players": 0},
        {"player_status": detection.FieldStatus.INVALID, "players": 0},
        {"queue_status": detection.FieldStatus.PRESENT, "queue": None},
        {"queue_status": detection.FieldStatus.MISSING, "queue": 0},
        {"info_status": "healthy"},
        {"player_status": "present"},
        {"queue_status": "missing"},
        {"lifecycle": "normal"},
    ],
)
def test_observation_rejects_inconsistent_or_malformed_status_fields(overrides):
    values = {
        "wall_at": BASE_WALL,
        "monotonic_at": 0,
        "app_session_id": "app",
        "monitoring_session_id": "monitor",
        "poll_generation": 1,
        "server_key": "server",
        "info_status": detection.InfoStatus.HEALTHY,
        "player_status": detection.FieldStatus.MISSING,
        "players": None,
        "queue_status": detection.FieldStatus.MISSING,
        "queue": None,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        detection.ObservationSample(**values)


def test_configuration_defaults_and_validation():
    config = detection.DetectionConfig()
    assert config.abrupt_drain_window == 25
    assert config.pre_roll_sample_cap == 12
    with pytest.raises(ValueError):
        detection.DetectionConfig(online_poll_interval=0)
    with pytest.raises(ValueError):
        detection.DetectionConfig(recovery_spacing_minimum=40, recovery_spacing_maximum=35)
    with pytest.raises(ValueError):
        detection.DetectionConfig(finalization_grace=61, finalization_maximum=60)


def test_engine_starts_idle_and_populates_bounded_preroll():
    engine = detection.PhysicalEpisodeEngine()
    for index in range(20):
        engine.ingest(sample(index * 10, 10))

    assert engine.state is detection.EpisodeState.IDLE
    assert len(engine.pre_roll) == 12
    assert engine.pre_roll[0].monotonic_at == 80


def test_abrupt_drain_opens_observing_and_records_landmarks():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, 0))

    assert engine.state is detection.EpisodeState.OBSERVING
    assert engine.active_episode.baseline_players == 12
    assert engine.active_episode.zero_reached
    assert engine.active_episode.drain_mono == 30


def test_first_failure_observes_then_two_timed_failures_confirm_offline():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert engine.state is detection.EpisodeState.OBSERVING
    engine.ingest(sample(36, MISSING, info=detection.InfoStatus.ERROR))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.confirmed_offline_mono == 36


def test_offline_return_and_visible_recovery_enter_recovering():
    offline = detection.PhysicalEpisodeEngine()
    populated(offline)
    feed(
        offline,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(36, MISSING, info=detection.InfoStatus.TIMEOUT)],
    )
    offline.ingest(sample(60, MISSING))
    assert offline.state is detection.EpisodeState.RECOVERING

    visible = detection.PhysicalEpisodeEngine()
    populated(visible)
    feed(visible, [sample(30, 0), sample(60, 0), sample(90, 0), sample(190, 2)])
    assert visible.state is detection.EpisodeState.RECOVERING


def test_crowbar_sequence_finalizes_one_strong_query_visible_event():
    engine = detection.PhysicalEpisodeEngine()
    events = crowbar_sequence(engine)

    assert len(events) == 1
    event = events[0]
    assert event.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert 0.80 <= event.authenticity <= 0.95
    assert event.outage.confirmed_offline_at is None
    assert event.drain.low_duration == 170
    assert event.canonical_phase_at == BASE_WALL + 200
    assert event.sources >= {
        detection.SignalSource.PLAYER_DRAIN,
        detection.SignalSource.VISIBLE_LOW,
        detection.SignalSource.PLAYER_RECOVERY,
    }


def test_conventional_sequence_clusters_all_signals_into_one_event():
    engine = detection.PhysicalEpisodeEngine()
    events = offline_sequence(engine, drain=True, queue=4)

    assert len(events) == 1
    event = events[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert 0.95 <= event.authenticity <= 1.0
    assert event.outage.confirmed_offline_at == BASE_WALL + 96
    assert event.canonical_phase_at == BASE_WALL + 120
    assert event.coverage_complete
    assert event.sources >= {
        detection.SignalSource.PLAYER_DRAIN,
        detection.SignalSource.INFO_OUTAGE,
        detection.SignalSource.INFO_RETURN,
        detection.SignalSource.QUEUE_RECOVERY,
    }


def test_clean_offline_without_player_or_queue_evidence_remains_valid():
    engine = detection.PhysicalEpisodeEngine()
    events = offline_sequence(engine, drain=False)

    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert 0.80 <= events[0].authenticity <= 0.90


def test_changed_player_repopulation_corroborates_offline_restart():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (12, 12, 12))
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(36, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(60, 3),
        ],
    )
    event = engine.tick(90, BASE_WALL + 90)[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert detection.SignalSource.PLAYER_RECOVERY in event.sources


def test_tick_and_flush_finalize_without_new_observation():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 2), sample(190, 3)])

    assert engine.tick(219, BASE_WALL + 219) == ()
    event = engine.tick(220, BASE_WALL + 220)[0]
    assert event.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert engine.tick(300, BASE_WALL + 300) == ()

    second = detection.PhysicalEpisodeEngine()
    populated(second)
    second.ingest(sample(30, 0))
    flushed = second.flush(40, BASE_WALL + 40)
    assert flushed[0].outcome is detection.EventOutcome.INCOMPLETE


def test_new_observation_advances_due_finalization_before_late_signal():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 2), sample(190, 3)])
    events = engine.ingest(sample(221, 4, queue=2))
    assert len(events) == 1
    assert events[0].recovery.first_queue_at is None


def test_single_a2s_failure_with_unchanged_population_is_uncertain():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (15, 15, 15))
    feed(engine, [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(40, 15)])
    event = engine.tick(70, BASE_WALL + 70)[0]

    assert event.outcome is detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION
    assert event.authenticity == 0.10
    assert event.outage.confirmed_offline_at is None


@pytest.mark.parametrize(
    ("return_players", "expected_score"),
    [(8, 0.25), (MISSING, 0.25)],
)
def test_single_failure_with_changed_or_missing_return_snapshot_stays_uncertain(
    return_players, expected_score
):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (15, 15, 15))
    feed(engine, [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(40, return_players)])
    event = engine.tick(70, BASE_WALL + 70)[0]
    assert event.outcome is detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION
    assert event.authenticity == expected_score


def test_offline_confirmation_timing_bounds():
    too_fast = detection.PhysicalEpisodeEngine()
    populated(too_fast)
    feed(
        too_fast,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(35, MISSING, info=detection.InfoStatus.TIMEOUT)],
    )
    assert too_fast.state is detection.EpisodeState.OBSERVING

    delayed = detection.PhysicalEpisodeEngine()
    populated(delayed)
    feed(
        delayed,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(45, MISSING, info=detection.InfoStatus.TIMEOUT)],
    )
    assert delayed.state is detection.EpisodeState.OFFLINE

    late = detection.PhysicalEpisodeEngine()
    populated(late)
    feed(
        late,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(46, MISSING, info=detection.InfoStatus.TIMEOUT)],
    )
    assert late.state is detection.EpisodeState.OBSERVING
    assert "offline_confirmation_window_missed" in late.active_episode.reason_codes


@pytest.mark.parametrize(
    ("baseline", "outcome", "maximum"),
    [
        (1, detection.EventOutcome.AMBIGUOUS_DRAIN, 0.35),
        (2, detection.EventOutcome.AMBIGUOUS_DRAIN, 0.35),
        (3, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART, 0.70),
        (4, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART, 0.70),
        (5, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART, 0.95),
        (12, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART, 0.95),
    ],
)
def test_population_strength_bands(baseline, outcome, maximum):
    engine = detection.PhysicalEpisodeEngine()
    events = crowbar_sequence(engine, baseline=baseline)
    assert events[0].outcome is outcome
    assert events[0].authenticity <= maximum


def test_five_to_one_is_probable_not_strong():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (5, 5, 5))
    feed(engine, [sample(30, 1), sample(60, 1), sample(90, 1), sample(180, 2), sample(190, 3)])
    event = engine.tick(220, BASE_WALL + 220)[0]
    assert event.outcome is detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART


def test_gradual_decline_over_sixty_seconds_does_not_open_abrupt_drain():
    engine = detection.PhysicalEpisodeEngine()
    counts = [(0, 10), (20, 9), (40, 7), (60, 5), (80, 3), (100, 1), (120, 0)]
    feed(engine, [sample(at, count) for at, count in counts])
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None


def test_partial_drain_only_becomes_strong_when_confirmed_outage_corroborates():
    no_outage = detection.PhysicalEpisodeEngine()
    populated(no_outage, (10, 10, 10))
    no_outage.ingest(sample(30, 3))
    assert no_outage.active_episode is None

    outage = detection.PhysicalEpisodeEngine()
    populated(outage, (10, 10, 10))
    feed(
        outage,
        [
            sample(30, 3),
            sample(40, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(46, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(70, 1),
        ],
    )
    event = outage.tick(100, BASE_WALL + 100)[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART


@pytest.mark.parametrize(
    ("low_times", "expected"),
    [
        ((30, 60), detection.EventOutcome.AMBIGUOUS_DRAIN),
        ((30, 40, 50), detection.EventOutcome.AMBIGUOUS_DRAIN),
        ((30, 50, 75), detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
    ],
)
def test_low_state_requires_three_samples_duration_and_thirty_second_confirmation(low_times, expected):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(at, 0) for at in low_times])
    recover_at = max(low_times) + 100
    feed(engine, [sample(recover_at, 2), sample(recover_at + 10, 3)])
    event = engine.tick(recover_at + 40, BASE_WALL + recover_at + 40)[0]
    assert event.outcome is expected


def test_missing_player_data_breaks_visible_episode_quality():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [sample(30, 0), sample(60, MISSING), sample(90, 0), sample(120, 0), sample(180, 2), sample(190, 3)],
    )
    event = engine.tick(220, BASE_WALL + 220)[0]
    assert event.outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert not event.coverage_complete


@pytest.mark.parametrize(
    ("recover_at", "outcome"),
    [
        (510, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
        (511, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART),
        (620, detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART),
    ],
)
def test_visible_recovery_quality_bands(recover_at, outcome):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0), sample(recover_at, 2), sample(recover_at + 10, 3)])
    event = engine.tick(recover_at + 40, BASE_WALL + recover_at + 40)[0]
    assert event.outcome is outcome


def test_visible_episode_expires_after_absolute_recovery_window():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0)])
    event = engine.tick(631, BASE_WALL + 631)[0]
    assert event.outcome is detection.EventOutcome.EXPIRED


def test_recovery_arriving_after_absolute_window_finalizes_expired_first():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0)])
    event = engine.ingest(sample(631, 2))[0]
    assert event.outcome is detection.EventOutcome.EXPIRED
    assert event.schedule_weight_suggestion == 0.0


def test_low_state_shorter_than_forty_five_seconds_remains_ambiguous():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(35, 0), sample(40, 0), sample(42, 2), sample(52, 3)])
    event = engine.tick(82, BASE_WALL + 82)[0]
    assert event.outcome is detection.EventOutcome.AMBIGUOUS_DRAIN


@pytest.mark.parametrize("gap", [10, 35])
def test_two_positive_samples_at_recovery_spacing_bounds_are_stable(gap):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 2), sample(180 + gap, 3)])
    assert engine.active_episode.stable_recovery_mono == 180 + gap


@pytest.mark.parametrize("gap", [9, 36])
def test_positive_samples_outside_spacing_bounds_are_not_stable(gap):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 2), sample(180 + gap, 3)])
    assert engine.active_episode.stable_recovery_mono is None


def test_bouncing_recovery_requires_three_new_positive_samples():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, 0), sample(60, 0), sample(90, 0),
            sample(180, 2), sample(190, 0),
            sample(200, 1), sample(210, 2), sample(220, 3),
        ],
    )
    assert engine.active_episode.recovery_bounced
    assert engine.active_episode.stable_recovery_mono == 220


def test_queue_does_not_bypass_three_samples_after_recovery_bounce():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, 0), sample(60, 0), sample(90, 0),
            sample(180, 2), sample(190, 0, queue=2), sample(200, 1),
        ],
    )
    assert engine.active_episode.stable_recovery_mono is None
    feed(engine, [sample(210, 2), sample(220, 3)])
    assert engine.active_episode.stable_recovery_mono == 220


def test_queue_then_player_is_alternative_recovery_but_queue_alone_is_not():
    queue_only = detection.PhysicalEpisodeEngine()
    populated(queue_only)
    feed(queue_only, [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 0, queue=2)])
    assert queue_only.active_episode.stable_recovery_mono is None

    with_player = detection.PhysicalEpisodeEngine()
    populated(with_player)
    feed(
        with_player,
        [sample(30, 0), sample(60, 0), sample(90, 0), sample(180, 0, queue=2), sample(250, 1)],
    )
    assert with_player.active_episode.stable_recovery_mono == 250


def test_query_failure_during_visible_episode_prevents_strong_visible_classification():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, 0),
            sample(60, 0),
            sample(90, 0, info=detection.InfoStatus.TIMEOUT),
            sample(100, 0),
        ],
    )
    event = engine.tick(130, BASE_WALL + 130)[0]
    assert event.outcome is detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION
    assert event.authenticity <= 0.30


def test_player_recovery_needs_no_queue_and_may_be_below_baseline():
    engine = detection.PhysicalEpisodeEngine()
    events = crowbar_sequence(engine, baseline=20)
    assert events[0].recovery.first_queue_at is None
    assert events[0].outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART


@pytest.mark.parametrize("failure_at", [150, 210])
def test_drain_to_outage_merge_boundary(failure_at):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, 0),
            sample(failure_at, 0, info=detection.InfoStatus.TIMEOUT),
            sample(failure_at + 6, 0, info=detection.InfoStatus.TIMEOUT),
            sample(failure_at + 30, 2),
        ],
    )
    events = engine.tick(failure_at + 60, BASE_WALL + failure_at + 60)
    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART


def test_outage_beyond_merge_window_does_not_corroborate_old_drain():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    events = feed(engine, [sample(30, 0), sample(211, 0, info=detection.InfoStatus.TIMEOUT)])
    assert events[0].outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert engine.active_episode.drain_mono is None


def test_outage_then_zero_return_is_clustered_as_one_reverse_order_episode():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(36, MISSING, info=detection.InfoStatus.TIMEOUT), sample(60, 0)],
    )
    event = engine.tick(90, BASE_WALL + 90)[0]
    assert event.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert event.outage.confirmed_offline_at == BASE_WALL + 36
    assert event.drain.drain_at is None


def test_duplicate_samples_and_finalization_are_idempotent():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    drain = sample(30, 0)
    assert engine.ingest(drain) == ()
    assert engine.ingest(drain) == ()
    assert engine.active_episode.drain_mono == 30
    feed(engine, [sample(60, 0), sample(90, 0), sample(180, 2), sample(190, 3)])
    first = engine.tick(220, BASE_WALL + 220)
    second = engine.tick(220, BASE_WALL + 220)
    assert len(first) == 1
    assert second == ()


def test_repeated_outage_segment_before_finalization_stays_one_episode():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    events = feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(36, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(60, MISSING),
            sample(70, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(76, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(90, MISSING),
        ],
    )
    events.extend(engine.tick(120, BASE_WALL + 120))
    assert len(events) == 1
    assert events[0].sequence == 1


def test_event_fingerprint_and_id_are_deterministic():
    first = detection.PhysicalEpisodeEngine()
    second = detection.PhysicalEpisodeEngine()
    event1 = crowbar_sequence(first)[0]
    event2 = crowbar_sequence(second)[0]
    assert event1.fingerprint == event2.fingerprint
    assert event1.event_id == event2.event_id


def test_stable_normal_period_allows_a_later_separate_event():
    engine = detection.PhysicalEpisodeEngine()
    first = crowbar_sequence(engine)[0]
    feed(engine, [sample(250, 8), sample(310, 8), sample(320, 8), sample(330, 8), sample(340, 0)])
    assert engine.active_episode is not None
    assert engine.active_episode.event_id != first.event_id


@pytest.mark.parametrize(
    "marker",
    [
        detection.LifecycleMarker.PAUSE,
        detection.LifecycleMarker.SHUTDOWN,
        detection.LifecycleMarker.SERVER_SWITCH,
        detection.LifecycleMarker.CLEAR,
        detection.LifecycleMarker.SLEEP_GAP,
        detection.LifecycleMarker.APP_RESTART,
    ],
)
def test_lifecycle_boundaries_finalize_incomplete_and_break_coverage(marker):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, 0))
    event = engine.ingest(sample(40, MISSING, lifecycle=marker))[0]
    assert event.outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    assert not event.coverage_complete
    assert event.lifecycle_interruption is marker
    assert engine.pre_roll == []


def test_session_server_and_out_of_order_boundaries_do_not_preserve_continuity():
    for changed in (
        sample(40, 0, monitor="monitor-2"),
        sample(40, 0, server="other"),
        sample(25, 0),
    ):
        engine = detection.PhysicalEpisodeEngine()
        populated(engine)
        engine.ingest(sample(30, 0))
        event = engine.ingest(changed)[0]
        assert event.outcome is detection.EventOutcome.INCOMPLETE
        assert not event.coverage_complete


def test_authenticity_is_signal_based_and_has_no_schedule_input():
    assert "phase" not in detection.PhysicalEpisodeEngine.ingest.__annotations__
    strong = crowbar_sequence(detection.PhysicalEpisodeEngine())[0]
    confirmed = offline_sequence(detection.PhysicalEpisodeEngine(), drain=False)[0]
    assert strong.authenticity >= 0.80
    assert 0.80 <= confirmed.authenticity <= 0.90


def test_queue_absence_is_not_an_authenticity_penalty():
    without_queue = crowbar_sequence(detection.PhysicalEpisodeEngine(), queue=MISSING)[0]
    with_queue = crowbar_sequence(detection.PhysicalEpisodeEngine(), queue=2)[0]
    assert without_queue.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert with_queue.authenticity >= without_queue.authenticity


def test_unchanged_snapshot_makes_confirmed_signal_non_restart_like():
    unchanged = detection.PhysicalEpisodeEngine()
    populated(unchanged, (12, 12, 12))
    feed(
        unchanged,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(36, MISSING, info=detection.InfoStatus.TIMEOUT), sample(60, 12)],
    )
    event = unchanged.tick(90, BASE_WALL + 90)[0]
    assert event.outcome is detection.EventOutcome.SERVICE_INTERRUPTION
    assert event.authenticity == 0.20
    assert event.schedule_weight_suggestion == 0.0


def test_active_samples_are_bounded_and_repeated_polls_are_downsampled():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, 0))
    for index in range(1, 150):
        status = detection.InfoStatus.TIMEOUT if index % 2 else detection.InfoStatus.ERROR
        engine.ingest(sample(30 + index, MISSING, info=status))
    assert len(engine.active_episode.samples) == 96
    assert engine.active_episode.samples[0].monotonic_at == 0

    repeated = detection.PhysicalEpisodeEngine()
    populated(repeated)
    repeated.ingest(sample(30, 0))
    for at in range(31, 131):
        repeated.ingest(sample(at, 0))
    low_samples = [item for item in repeated.active_episode.samples if item.players == 0]
    assert len(low_samples) <= 3
    assert low_samples[0].monotonic_at == 30
    assert low_samples[-1].monotonic_at == 130
    assert len(repeated.active_episode.low_samples) <= 16
    assert repeated.active_episode.low_sample_count == 101


def test_tick_rejects_time_before_latest_observation():
    engine = detection.PhysicalEpisodeEngine()
    engine.ingest(sample(10, 5))
    with pytest.raises(ValueError):
        engine.tick(9, BASE_WALL + 9)


def test_finalized_event_and_nested_summaries_are_immutable():
    event = crowbar_sequence(detection.PhysicalEpisodeEngine())[0]
    with pytest.raises(FrozenInstanceError):
        event.authenticity = 0
    with pytest.raises(FrozenInstanceError):
        event.drain.baseline_players = 1
    assert isinstance(event.samples, tuple)
    assert isinstance(event.sources, frozenset)


def test_engine_instances_do_not_share_mutable_defaults():
    first = detection.PhysicalEpisodeEngine()
    second = detection.PhysicalEpisodeEngine()
    first.ingest(sample(0, 5))
    assert len(first.pre_roll) == 1
    assert second.pre_roll == []


def test_no_live_paths_ui_or_runtime_modules_are_imported():
    module_names = set(detection.__dict__)
    assert "Gtk" not in module_names
    assert "GLib" not in module_names
    assert "window" not in module_names
    assert "config" not in module_names
