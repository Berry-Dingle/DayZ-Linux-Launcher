import json

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 2_100_000_000.0
SERVER = "203.0.113.44:2302"
MISSING = object()


def sample(
    at,
    players=MISSING,
    *,
    info=detection.InfoStatus.HEALTHY,
    lifecycle=detection.LifecycleMarker.NORMAL,
    server=SERVER,
    monitor="monitor-1",
):
    present = players is not MISSING
    return detection.ObservationSample(
        wall_at=BASE + at,
        monotonic_at=float(at),
        app_session_id="app-1",
        monitoring_session_id=monitor,
        poll_generation=1,
        server_key=server,
        info_status=info,
        player_status=(
            detection.FieldStatus.PRESENT if present else detection.FieldStatus.MISSING
        ),
        players=players if present else None,
        max_players=60,
        lifecycle=lifecycle,
    )


def feed(engine, observations):
    events = []
    for observation in observations:
        events.extend(engine.ingest(observation))
    return events


def finish_outage(engine, first_failure, *, recovery_players=0):
    assert engine.ingest(
        sample(first_failure, MISSING, info=detection.InfoStatus.TIMEOUT)
    ) == ()
    assert engine.ingest(
        sample(first_failure + 10, MISSING, info=detection.InfoStatus.NETWORK_ERROR)
    ) == ()
    assert engine.state is detection.EpisodeState.OFFLINE
    engine.ingest(sample(first_failure + 13, recovery_players))
    return engine.tick(first_failure + 43, BASE + first_failure + 43)[0]


def bob_seven_player_drain(engine):
    observations = [sample(at, 7) for at in (0, 10, 20)]
    observations.extend(
        [sample(120, 5), sample(240, 0), sample(250, 0)]
    )
    observations.extend(sample(at, 0) for at in range(260, 361, 10))
    return feed(engine, observations)


def test_bob_style_gradual_drain_is_provisional_then_inherited_by_outage():
    engine = detection.PhysicalEpisodeEngine()
    assert bob_seven_player_drain(engine) == []
    assert engine.active_episode is None
    assert engine.provisional_drain is not None

    event = finish_outage(engine, 370)

    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.schedule_weight_suggestion > 0
    assert event.drain.baseline_players == 7
    assert event.drain.zero_reached
    assert event.drain.inherited
    assert not event.drain.abrupt
    assert event.drain.decline_duration == 220
    assert detection.SignalSource.PLAYER_DRAIN in event.sources


def test_five_minute_ten_player_gradual_drain_retains_baseline_and_duration():
    engine = detection.PhysicalEpisodeEngine()
    feed(
        engine,
        [
            sample(0, 10),
            sample(10, 10),
            sample(20, 10),
            sample(60, 9),
            sample(120, 8),
            sample(180, 7),
            sample(240, 6),
            sample(280, 5),
            sample(300, 0),
            sample(310, 0),
        ],
    )
    provisional = engine.provisional_drain
    assert provisional is not None
    assert provisional.baseline_players >= 9
    assert not provisional.abrupt
    assert provisional.decline_duration >= 180

    event = finish_outage(engine, 330)
    assert event.drain.baseline_players >= 9
    assert event.drain.decline_duration >= 180
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART


def test_nonuniform_fourteen_player_decline_with_plateau_is_recognised():
    engine = detection.PhysicalEpisodeEngine()
    counts = [14, 14, 13, 13, 12, 11, 10, 9, 7, 7, 6, 4, 0, 0]
    feed(engine, [sample(index * 14, count) for index, count in enumerate(counts)])

    provisional = engine.provisional_drain
    assert provisional is not None
    assert provisional.baseline_players >= 12
    assert provisional.minimum_players == 0
    assert not provisional.abrupt

    event = finish_outage(engine, 200)
    assert event.drain.zero_reached
    assert event.drain.inherited


def test_drain_more_than_two_minutes_before_outage_remains_mergeable():
    engine = detection.PhysicalEpisodeEngine()
    bob_seven_player_drain(engine)
    event = finish_outage(engine, 500)
    assert event.drain.baseline_players == 7
    assert event.drain.low_started_at == BASE + 240


def test_drain_older_than_fixed_merge_expiry_is_not_inherited():
    engine = detection.PhysicalEpisodeEngine()
    feed(
        engine,
        [sample(0, 10), sample(10, 10), sample(20, 10), sample(100, 0), sample(110, 0)],
    )
    assert engine.provisional_drain is not None

    event = finish_outage(engine, 411)
    assert event.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert event.drain.baseline_players is None
    assert detection.SignalSource.PLAYER_DRAIN not in event.sources


def test_consumed_provisional_drain_cannot_authenticate_second_outage():
    engine = detection.PhysicalEpisodeEngine()
    bob_seven_player_drain(engine)
    first = finish_outage(engine, 370)
    assert first.drain.inherited

    second = finish_outage(engine, 500, recovery_players=MISSING)
    assert second.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert second.drain.baseline_players is None


@pytest.mark.parametrize(
    "marker",
    [
        detection.LifecycleMarker.SERVER_SWITCH,
        detection.LifecycleMarker.PAUSE,
        detection.LifecycleMarker.CLEAR,
        detection.LifecycleMarker.SHUTDOWN,
    ],
)
def test_lifecycle_boundary_clears_history_and_provisional_drain(marker):
    engine = detection.PhysicalEpisodeEngine()
    bob_seven_player_drain(engine)
    assert engine.provisional_drain is not None

    assert engine.ingest(sample(365, MISSING, lifecycle=marker)) == ()
    assert engine.pre_roll == []
    assert engine.provisional_drain is None
    assert engine.active_episode is None


def test_server_switch_cannot_leak_drain_into_other_server():
    engine = detection.PhysicalEpisodeEngine()
    bob_seven_player_drain(engine)
    feed(
        engine,
        [
            sample(370, MISSING, info=detection.InfoStatus.TIMEOUT, server="other:2302"),
            sample(380, MISSING, info=detection.InfoStatus.TIMEOUT, server="other:2302"),
        ],
    )
    assert engine.active_episode is not None
    assert engine.active_episode.baseline_players is None


def test_stable_empty_server_never_forms_drain_without_baseline():
    engine = detection.PhysicalEpisodeEngine()
    assert feed(engine, [sample(at, 0) for at in range(0, 601, 10)]) == []
    assert engine.provisional_drain is None
    assert engine.active_episode is None


def test_one_player_to_zero_is_not_meaningful_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 1), sample(10, 1), sample(20, 1), sample(30, 0), sample(40, 0)])
    assert engine.provisional_drain is None


def test_ordinary_population_fluctuation_leaves_no_provisional_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(index * 10, value) for index, value in enumerate((12, 10, 11, 9, 12))])
    assert engine.provisional_drain is None
    assert engine.active_episode is None


def test_gradual_drain_without_outage_expires_without_event():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 10), sample(10, 10), sample(20, 10), sample(100, 0), sample(110, 0)])
    assert engine.provisional_drain is not None
    assert engine.ingest(sample(411, 0)) == ()
    assert engine.provisional_drain is not None
    assert engine.ingest(sample(701, 0)) == ()
    assert engine.provisional_drain is None
    assert engine.active_episode is None


def test_repopulation_before_outage_clears_provisional_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 10), sample(10, 10), sample(20, 10), sample(100, 0), sample(110, 0)])
    assert engine.provisional_drain is not None
    engine.ingest(sample(120, 9))
    assert engine.provisional_drain is None

    event = finish_outage(engine, 200, recovery_players=MISSING)
    assert event.drain.baseline_players is None


def test_missing_player_samples_remain_unknown_and_do_not_strengthen_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 10), sample(10, 10), sample(20, 10), sample(100, 0)])
    engine.ingest(sample(110, MISSING))
    assert engine.provisional_drain is None
    engine.ingest(sample(120, 0))

    assert engine.provisional_drain is not None
    assert engine.provisional_drain.low_sample_count == 2
    assert any(item.player_status is detection.FieldStatus.MISSING for item in engine.pre_roll)


def test_neutral_and_protocol_samples_do_not_advance_player_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 10), sample(10, 10), sample(20, 10), sample(100, 0)])
    engine.ingest(sample(110, 0, info=detection.InfoStatus.NEUTRAL))
    engine.ingest(sample(120, 0, info=detection.InfoStatus.ERROR))
    assert engine.provisional_drain is None
    engine.ingest(sample(130, 0))
    assert engine.provisional_drain is not None
    assert engine.provisional_drain.low_sample_count == 2


def test_confirmed_outage_without_drain_remains_positive():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 12), sample(10, 12), sample(20, 12)])
    event = finish_outage(engine, 30, recovery_players=MISSING)
    assert event.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert event.schedule_weight_suggestion > 0
    assert event.drain.baseline_players is None


def test_weak_failure_recovery_does_not_block_later_provisional_drain():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(0, 10), sample(10, 10), sample(20, 10)])
    engine.ingest(sample(30, MISSING, info=detection.InfoStatus.TIMEOUT))
    engine.ingest(sample(40, 8))
    feed(engine, [sample(100, 0), sample(110, 0)])
    assert engine.active_episode is None
    assert engine.provisional_drain is not None


def test_history_is_bounded_during_abnormally_fast_polling():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [sample(index, 12) for index in range(200)])
    assert len(engine.pre_roll) == 60


def make_runtime(tmp_path):
    active = tmp_path / storage.PHASE2_ACTIVE_FILENAME
    legacy = tmp_path / storage.LEGACY_PHASE1_FILENAME
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE,
        app_session_id="app-1",
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1)
    return value, active, legacy


def live_poll(value, at, *, players=MISSING, ok=True):
    payload = {"ok": ok}
    if ok and players is not MISSING:
        payload.update(players=players, max_players=60, ping_ms=40)
    if not ok:
        payload.update(err="timed out", a2s_classification="timeout")
    return value.ingest_live_result(
        SERVER,
        poll_generation=1,
        info=payload,
        wall_at=BASE + at,
        monotonic_at=at,
    )


def runtime_bob_event(value):
    for at in (0, 10, 20):
        live_poll(value, at, players=7)
    live_poll(value, 120, players=5)
    for at in range(240, 361, 10):
        live_poll(value, at, players=0)
    live_poll(value, 370, ok=False)
    live_poll(value, 380, ok=False)
    live_poll(value, 383, players=0)
    return value.tick(SERVER, wall_at=BASE + 413, monotonic_at=413).finalized_events[0]


def test_inherited_drain_event_round_trip_and_old_optional_defaults(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    event = runtime_bob_event(value)
    value.shutdown(wall_at=BASE + 414, monotonic_at=414)

    loaded = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 415,
        app_session_id="app-2",
    )
    restored = loaded._servers[SERVER].events[-1]
    assert restored.drain == event.drain
    assert restored.drain.inherited
    assert restored.drain.baseline_at == BASE + 20

    raw = runtime._serialize_event(event)
    raw["drain"].pop("baseline_at")
    raw["drain"].pop("decline_duration")
    raw["drain"].pop("inherited")
    old = runtime._deserialize_event(json.loads(json.dumps(raw)))
    assert old.drain.baseline_at is None
    assert old.drain.decline_duration == 0
    assert not old.drain.inherited


def test_event_diagnostics_retain_baseline_low_outage_and_recovery(tmp_path):
    value, *_ = make_runtime(tmp_path)
    event = runtime_bob_event(value)
    samples = tuple(event.samples)

    assert samples[0].players == 7
    assert any(item.players == 0 for item in samples)
    assert sum(item.info_status is detection.InfoStatus.TIMEOUT for item in samples) == 2
    assert samples[-1].info_status is detection.InfoStatus.HEALTHY
    assert event.drain.baseline_at is not None
    assert event.drain.decline_duration > 120
