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
    assert config.pending_strike_lifetime == 25
    assert config.pre_roll_seconds == 600
    assert config.pre_roll_sample_cap == 60
    assert config.drain_to_outage_merge == 300
    with pytest.raises(ValueError):
        detection.DetectionConfig(online_poll_interval=0)
    with pytest.raises(ValueError):
        detection.DetectionConfig(recovery_spacing_minimum=40, recovery_spacing_maximum=35)
    with pytest.raises(ValueError):
        detection.DetectionConfig(finalization_grace=61, finalization_maximum=60)


def test_engine_starts_idle_and_populates_bounded_preroll():
    engine = detection.PhysicalEpisodeEngine()
    for index in range(80):
        engine.ingest(sample(index * 10, 10))

    assert engine.state is detection.EpisodeState.IDLE
    assert len(engine.pre_roll) == 60
    assert engine.pre_roll[0].monotonic_at == 200


def test_abrupt_drain_stays_provisional_and_does_not_open_episode():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, 0))
    assert engine.provisional_drain is None
    engine.ingest(sample(40, 0))

    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    assert engine.provisional_drain.baseline_players == 12
    assert engine.provisional_drain.zero_reached
    assert engine.provisional_drain.abrupt


def test_first_failure_observes_then_two_timed_failures_confirm_offline():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    engine.ingest(sample(36, MISSING, info=detection.InfoStatus.NETWORK_ERROR))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.confirmed_offline_mono == 36


def test_stale_pending_failure_expires_through_neutral_and_protocol_observations():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(40, MISSING, info=detection.InfoStatus.NEUTRAL),
            sample(50, MISSING, info=detection.InfoStatus.ERROR),
            sample(60, MISSING, info=detection.InfoStatus.TIMEOUT),
        ],
    )

    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    engine.ingest(sample(70, MISSING, info=detection.InfoStatus.NETWORK_ERROR))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 60


def test_recent_pending_failure_survives_neutral_until_qualifying_second_strike():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(40, MISSING, info=detection.InfoStatus.NEUTRAL),
            sample(47, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
        ],
    )

    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 30
    assert engine.active_episode.confirmed_offline_mono == 47


def test_lifecycle_boundary_clears_pending_failure():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    engine.ingest(sample(30, MISSING, info=detection.InfoStatus.TIMEOUT))
    engine.ingest(sample(35, MISSING, lifecycle=detection.LifecycleMarker.PAUSE))
    engine.ingest(sample(40, MISSING, info=detection.InfoStatus.TIMEOUT))

    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None


def test_weak_failure_then_healthy_closes_without_episode_or_stale_state():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (12, 12, 12))
    feed(engine, [
        sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
        sample(40, 12),
        sample(50, 12),
        sample(60, 12),
    ])

    assert engine.active_episode is None
    assert engine.state is detection.EpisodeState.IDLE


def test_player_drain_after_pending_strike_recovery_is_not_bypassed():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (12, 12, 12))
    feed(engine, [
        sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
        sample(40, 12),
        sample(50, 0),
        sample(60, 0),
    ])

    assert engine.active_episode is None
    assert engine.provisional_drain.baseline_players == 12
    assert engine.provisional_drain.minimum_players == 0
    assert engine.provisional_drain.zero_reached


def test_offline_return_enters_recovering():
    offline = detection.PhysicalEpisodeEngine()
    populated(offline)
    feed(
        offline,
        [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(36, MISSING, info=detection.InfoStatus.TIMEOUT)],
    )
    offline.ingest(sample(60, MISSING))
    assert offline.state is detection.EpisodeState.RECOVERING


def test_conventional_sequence_clusters_all_signals_into_one_event():
    engine = detection.PhysicalEpisodeEngine()
    events = offline_sequence(engine, drain=True, queue=4)

    assert len(events) == 1
    event = events[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert 0.95 <= event.authenticity <= 1.0
    assert event.outage.confirmed_offline_at == BASE_WALL + 96
    assert event.canonical_phase_at == BASE_WALL + 90
    assert event.canonical_phase_at == event.outage.first_failure_at
    assert event.outage.info_return_at == BASE_WALL + 120
    assert event.coverage_complete
    assert event.sources >= {
        detection.SignalSource.PLAYER_DRAIN,
        detection.SignalSource.INFO_OUTAGE,
        detection.SignalSource.INFO_RETURN,
        detection.SignalSource.QUEUE_RECOVERY,
    }


def test_variable_outage_recovery_duration_does_not_move_restart_start_phase():
    def completed(recovery_at):
        engine = detection.PhysicalEpisodeEngine()
        populated(engine)
        feed(
            engine,
            [
                sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
                sample(36, MISSING, info=detection.InfoStatus.TIMEOUT),
                sample(recovery_at, MISSING),
            ],
        )
        return engine.tick(recovery_at + 30, BASE_WALL + recovery_at + 30)[0]

    quick = completed(60)
    slow = completed(180)

    assert quick.outage.info_return_at == BASE_WALL + 60
    assert slow.outage.info_return_at == BASE_WALL + 180
    assert quick.canonical_phase_at == slow.canonical_phase_at == BASE_WALL + 30
    assert quick.phase_uncertainty == slow.phase_uncertainty == 10


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


def test_single_a2s_failure_with_unchanged_population_is_discarded():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (15, 15, 15))
    feed(engine, [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(40, 15)])
    assert engine.tick(70, BASE_WALL + 70) == ()
    assert engine.active_episode is None


@pytest.mark.parametrize(
    "return_players",
    [8, MISSING],
)
def test_single_failure_with_changed_or_missing_return_snapshot_is_discarded(return_players):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine, (15, 15, 15))
    feed(engine, [sample(30, MISSING, info=detection.InfoStatus.TIMEOUT), sample(40, return_players)])
    assert engine.tick(70, BASE_WALL + 70) == ()
    assert engine.active_episode is None


@pytest.mark.parametrize("gap", [5, 10, 15, 17, 25])
def test_shared_pending_strike_window_confirms_without_learner_only_bounds(gap):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(30 + gap, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
        ],
    )
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.confirmed_offline_mono == 30 + gap


def test_failure_after_shared_pending_strike_window_becomes_new_strike_one():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(56, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
        ],
    )
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None

    engine.ingest(sample(66, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 56


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
    failure = sample(30, MISSING, info=detection.InfoStatus.TIMEOUT)
    assert engine.ingest(failure) == ()
    assert engine.ingest(failure) == ()
    feed(engine, [sample(40, MISSING, info=detection.InfoStatus.TIMEOUT), sample(43, MISSING)])
    first = engine.tick(73, BASE_WALL + 73)
    second = engine.tick(73, BASE_WALL + 73)
    assert len(first) == 1
    assert second == ()


def test_flush_finalizes_confirmed_outage_once_and_clears_transient_state():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(40, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
        ],
    )
    assert engine.state is detection.EpisodeState.OFFLINE

    flushed = engine.flush(43, BASE_WALL + 43)
    assert len(flushed) == 1
    assert flushed[0].outcome is detection.EventOutcome.INCOMPLETE
    assert flushed[0].reason_codes == ("explicit_flush",)
    assert flushed[0].schedule_weight_suggestion == 0.0
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    assert engine.pre_roll == []
    assert engine.provisional_drain is None
    assert engine.flush(46, BASE_WALL + 46) == ()

    pending = detection.PhysicalEpisodeEngine()
    populated(pending)
    pending.ingest(sample(30, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert pending.flush(35, BASE_WALL + 35) == ()
    pending.ingest(sample(40, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert pending.state is detection.EpisodeState.IDLE
    assert pending.active_episode is None

    provisional = detection.PhysicalEpisodeEngine()
    populated(provisional)
    feed(provisional, [sample(30, 0), sample(40, 0)])
    assert provisional.provisional_drain is not None
    assert provisional.flush(45, BASE_WALL + 45) == ()
    assert provisional.provisional_drain is None
    assert provisional.pre_roll == []


def test_due_episode_finalizes_before_new_observation_starts_fresh_state():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(40, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
            sample(43, MISSING),
        ],
    )
    assert engine.active_episode.info_return_mono == 43
    assert engine.active_episode.stable_recovery_mono == 43

    events = engine.ingest(sample(73, MISSING, info=detection.InfoStatus.TIMEOUT))
    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert events[0].outage.info_return_at == BASE_WALL + 43
    assert events[0].recovery.stable_recovery_at == BASE_WALL + 43
    assert events[0].finalized_at == BASE_WALL + 73
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None

    engine.ingest(sample(83, MISSING, info=detection.InfoStatus.NETWORK_ERROR))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 73
    assert engine.active_episode.confirmed_offline_mono == 83
    assert engine.active_episode.event_id != events[0].event_id


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
    event1 = offline_sequence(first)[0]
    event2 = offline_sequence(second)[0]
    assert event1.fingerprint == event2.fingerprint
    assert event1.event_id == event2.event_id


def test_stable_normal_period_allows_a_later_separate_event():
    engine = detection.PhysicalEpisodeEngine()
    first = offline_sequence(engine)[0]
    feed(engine, [sample(160, 8), sample(220, 8), sample(230, MISSING, info=detection.InfoStatus.TIMEOUT), sample(240, MISSING, info=detection.InfoStatus.TIMEOUT)])
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.event_id != first.event_id


def test_idle_lifecycle_boundary_clears_positive_event_cooldown_and_transient_history():
    engine = detection.PhysicalEpisodeEngine()
    event = offline_sequence(engine)[0]

    assert event.schedule_weight_suggestion > 0.0
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    assert engine._cooldown_until_stable

    feed(
        engine,
        [
            sample(160, 12),
            sample(170, 12),
            sample(180, 12),
            sample(190, 0),
            sample(200, 0),
        ],
    )
    assert engine.pre_roll
    assert engine.provisional_drain is not None
    assert engine._normal_since_mono == 160

    assert engine.ingest(
        sample(205, MISSING, lifecycle=detection.LifecycleMarker.PAUSE)
    ) == ()
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    assert not engine._cooldown_until_stable
    assert engine._pending_failure is None
    assert engine.provisional_drain is None
    assert engine.pre_roll == []
    assert engine._normal_since_mono is None

    engine.ingest(sample(210, 9, monitor="monitor-2"))
    assert len(engine.pre_roll) == 1
    assert engine.pre_roll[0].players == 9
    assert not engine._cooldown_until_stable


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
def test_lifecycle_boundaries_discard_provisional_drain_without_event(marker):
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(engine, [sample(30, 0), sample(40, 0)])
    assert engine.provisional_drain is not None
    assert engine.ingest(sample(50, MISSING, lifecycle=marker)) == ()
    assert engine.pre_roll == []
    assert engine.provisional_drain is None


def test_lifecycle_interrupts_active_confirmed_outage_and_resets_for_next_session():
    engine = detection.PhysicalEpisodeEngine()
    populated(engine)
    feed(
        engine,
        [
            sample(30, MISSING, info=detection.InfoStatus.TIMEOUT),
            sample(40, MISSING, info=detection.InfoStatus.NETWORK_ERROR),
        ],
    )
    assert engine.state is detection.EpisodeState.OFFLINE

    events = engine.ingest(
        sample(43, MISSING, lifecycle=detection.LifecycleMarker.PAUSE)
    )
    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.INCOMPLETE
    assert events[0].schedule_weight_suggestion == 0.0
    assert events[0].lifecycle_interruption is detection.LifecycleMarker.PAUSE
    assert "lifecycle_pause" in events[0].reason_codes
    assert not events[0].coverage_complete
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    assert engine.pre_roll == []
    assert engine.provisional_drain is None
    assert engine._pending_failure is None
    assert not engine._cooldown_until_stable

    engine.ingest(sample(50, 9, monitor="monitor-2", server="other:2302"))
    engine.ingest(
        sample(
            60,
            MISSING,
            info=detection.InfoStatus.TIMEOUT,
            monitor="monitor-2",
            server="other:2302",
        )
    )
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    engine.ingest(
        sample(
            70,
            MISSING,
            info=detection.InfoStatus.NETWORK_ERROR,
            monitor="monitor-2",
            server="other:2302",
        )
    )
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.server_key == "other:2302"
    assert engine.active_episode.monitoring_session_id == "monitor-2"
    assert engine.active_episode.first_failure_mono == 60


def test_session_server_and_out_of_order_boundaries_do_not_preserve_continuity():
    for changed in (
        sample(40, 0, monitor="monitor-2"),
        sample(40, 0, server="other"),
        sample(25, 0),
    ):
        engine = detection.PhysicalEpisodeEngine()
        populated(engine)
        feed(engine, [sample(30, 0), sample(35, 0)])
        assert engine.ingest(changed) == ()
        assert engine.active_episode is None


def test_authenticity_is_signal_based_and_has_no_schedule_input():
    assert "phase" not in detection.PhysicalEpisodeEngine.ingest.__annotations__
    strong = offline_sequence(detection.PhysicalEpisodeEngine(), drain=True)[0]
    confirmed = offline_sequence(detection.PhysicalEpisodeEngine(), drain=False)[0]
    assert strong.authenticity >= 0.80
    assert 0.80 <= confirmed.authenticity <= 0.90


def test_queue_absence_is_not_an_authenticity_penalty():
    without_queue = offline_sequence(detection.PhysicalEpisodeEngine(), queue=MISSING)[0]
    with_queue = offline_sequence(detection.PhysicalEpisodeEngine(), queue=2)[0]
    assert without_queue.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
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
    for at in range(30, 131):
        repeated.ingest(sample(at, 0))
    assert repeated.active_episode is None
    assert repeated.provisional_drain.low_sample_count == 101
    assert len(repeated.provisional_drain.low_samples) <= 32
    assert len(repeated.provisional_drain.evidence_samples) <= 36


def test_tick_rejects_time_before_latest_observation():
    engine = detection.PhysicalEpisodeEngine()
    engine.ingest(sample(10, 5))
    with pytest.raises(ValueError):
        engine.tick(9, BASE_WALL + 9)


def test_finalized_event_and_nested_summaries_are_immutable():
    event = offline_sequence(detection.PhysicalEpisodeEngine())[0]
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
