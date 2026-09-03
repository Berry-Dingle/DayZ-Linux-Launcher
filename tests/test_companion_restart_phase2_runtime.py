import ast
import hashlib
import inspect
import json
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from dzll_launcher import companion_restart_phase2_consumers as consumers
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage
from dzll_launcher.startup_presentation import StartupPresentationCoordinator
from dzll_launcher.update_ui import UpdateUI


BASE = 2_000_000_000.0
SERVER = "198.51.100.10:2302"
DAY = 24 * scoring.HOUR


def paths(tmp_path):
    return tmp_path / storage.PHASE2_ACTIVE_FILENAME, tmp_path / storage.LEGACY_PHASE1_FILENAME


def make_runtime(tmp_path, *, legacy=None):
    active, old = paths(tmp_path)
    if legacy is not None:
        old.write_bytes(legacy)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=old,
        now=BASE,
        app_session_id="app-session",
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    return value, active, old


def begin(value, generation=1):
    return value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=generation)


def poll(value, at, *, ok=True, players=12, queue_marker=False, generation=1, err="timeout"):
    info = {"ok": ok}
    if ok:
        if players is not None:
            info.update(players=players, max_players=60, ping_ms=42)
        if queue_marker is not False:
            info["queue"] = queue_marker
    else:
        info["err"] = err
    return value.ingest_live_result(
        SERVER,
        poll_generation=generation,
        info=info,
        wall_at=BASE + at,
        monotonic_at=at,
    )


def crowbar(value):
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        poll(value, at, players=players)
    poll(value, 90, ok=False)
    poll(value, 100, ok=False)
    poll(value, 103, players=0)
    return value.tick(SERVER, wall_at=BASE + 133, monotonic_at=133)


def conventional(value):
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        poll(value, at, players=players)
    poll(value, 90, ok=False)
    poll(value, 96, ok=False)
    poll(value, 120, players=3, queue_marker=2)
    return value.tick(SERVER, wall_at=BASE + 150, monotonic_at=150)


def scored_event(hours, sequence, outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART):
    at = BASE + hours * scoring.HOUR
    authenticity = 0.90 if outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART else 0.98
    return detection.PhysicalRestartEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        fingerprint=f"fingerprint-{sequence}",
        server_key=SERVER,
        episode_started_at=at - 120,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.PLAYER_RECOVERY}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(12, at - 200, at - 180, at - 180, True, 0, 1.0, 3, 180, True),
        outage=detection.OutageSummary(None, None, 0, (), None),
        recovery=detection.RecoverySummary(None, at, at, 2, False),
        query_health=detection.QueryHealthSummary(20, 0, 0, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=(),
    )


def test_runtime_initializes_fresh_phase2_state_without_notice(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    assert active.exists()
    assert not legacy.exists()
    assert value.persistence_enabled
    assert value.pending_notice is None
    assert value.migration.reset_performed is False


def test_runtime_activates_exact_legacy_backup_and_reset_notice(tmp_path):
    legacy_bytes = b"not even json\x00legacy"
    value, active, legacy = make_runtime(tmp_path, legacy=legacy_bytes)
    assert active.exists()
    assert not legacy.exists()
    assert value.migration.reset_performed
    assert value.pending_notice.kind == "legacy_reset"
    backup = Path(value.pending_notice.backup_path)
    assert backup.read_bytes() == legacy_bytes
    assert value.state["reset_from_checksum"] == hashlib.sha256(legacy_bytes).hexdigest()


def test_valid_active_is_authoritative_over_rollback_legacy(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    legacy.write_text('{"confidence": 0.95}', encoding="utf-8")
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE + 1, app_session_id="second"
    )
    assert again.migration.reset_performed is False
    assert legacy.exists()
    assert again.pending_notice is None


def test_migration_failure_disables_learning_and_has_distinct_notice(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    failed = storage.Phase2MigrationResult(
        state=storage.new_phase2_state(now=BASE),
        status=storage.Phase2InitializationStatus.FAILED,
        reset_performed=False,
        recovery_performed=False,
        active_path=active,
        backup_path=None,
        legacy_checksum=None,
        persistence_enabled=False,
        failure=storage.Phase2MigrationFailure.ACTIVE_INSTALL_FAILED,
        error_kind="PermissionError",
        error="denied",
    )
    monkeypatch.setattr(runtime, "initialize_phase2_state", lambda **_kwargs: failed)
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE, app_session_id="app"
    )
    assert not value.persistence_enabled
    assert value.pending_notice.kind == "initialization_failed"
    assert not value.migration.reset_performed
    assert legacy.read_bytes() == b"legacy"
    assert value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1) == ""


@pytest.mark.parametrize(
    ("payload", "info_status", "player_status", "players", "queue_status", "queue"),
    [
        ({"ok": True, "players": 0, "queue": 0}, detection.InfoStatus.HEALTHY, detection.FieldStatus.PRESENT, 0, detection.FieldStatus.PRESENT, 0),
        ({"ok": True}, detection.InfoStatus.HEALTHY, detection.FieldStatus.MISSING, None, detection.FieldStatus.MISSING, None),
        ({"ok": True, "players": "bad", "queue": -1}, detection.InfoStatus.HEALTHY, detection.FieldStatus.INVALID, None, detection.FieldStatus.INVALID, None),
        ({"ok": False, "err": "timed out"}, detection.InfoStatus.TIMEOUT, detection.FieldStatus.MISSING, None, detection.FieldStatus.MISSING, None),
        ({"ok": False, "err": "refused", "a2s_classification": "socket-error"}, detection.InfoStatus.NETWORK_ERROR, detection.FieldStatus.MISSING, None, detection.FieldStatus.MISSING, None),
        ({"ok": False, "neutral": True}, detection.InfoStatus.NEUTRAL, detection.FieldStatus.MISSING, None, detection.FieldStatus.MISSING, None),
        ({"ok": False, "err": "bad packet"}, detection.InfoStatus.ERROR, detection.FieldStatus.MISSING, None, detection.FieldStatus.MISSING, None),
    ],
)
def test_live_mapping_preserves_query_truth(payload, info_status, player_status, players, queue_status, queue):
    sample = runtime.observation_from_live_result(
        payload,
        wall_at=BASE,
        monotonic_at=0,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key=SERVER,
    )
    assert sample.info_status is info_status
    assert sample.player_status is player_status
    assert sample.players == players
    assert sample.queue_status is queue_status
    assert sample.queue == queue


def test_stale_generation_and_wrong_server_are_rejected(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value, generation=4)
    stale = poll(value, 0, generation=3)
    wrong = value.ingest_live_result(
        "other", poll_generation=4, info={"ok": True, "players": 1}, wall_at=BASE, monotonic_at=0
    )
    assert stale.rejected_reason == "stale_poll_generation"
    assert wrong.rejected_reason == "no_active_monitoring_session"


def test_conventional_signals_route_one_correlated_physical_event(tmp_path):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    update = conventional(value)
    assert len(update.finalized_events) == 1
    event = update.finalized_events[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert update.query_visible_repopulation_event_id is None
    saved = json.loads(active.read_text(encoding="utf-8"))
    assert len(saved["servers"][SERVER]["events"]) == 1
    assert {item["event_id"] for item in saved["servers"][SERVER]["events"]} == {event.event_id}


def test_qualified_query_visible_first_repopulation_is_exposed_before_finalization(
    tmp_path,
):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    observations = (
        (0, 12), (10, 12), (20, 12), (40, 11), (60, 9),
        (80, 7), (100, 5), (120, 3), (140, 0), (150, 0),
        (160, 0), (170, 0), (180, 0), (190, 0), (200, 0),
    )
    for at, players in observations:
        update = poll(value, at, players=players)
        assert update.query_visible_repopulation_event_id is None

    first_positive = poll(value, 210, players=3)
    episode = value._servers[SERVER].engine.active_episode

    assert first_positive.finalized_events == ()
    assert first_positive.query_visible_repopulation_event_id == episode.event_id
    assert episode.stable_recovery_mono is None

    finalized = poll(value, 220, players=6)
    assert len(finalized.finalized_events) == 1
    assert finalized.finalized_events[0].event_id == first_positive.query_visible_repopulation_event_id
    assert finalized.query_visible_repopulation_event_id is None


def test_drain_without_outage_routes_no_event(tmp_path):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (40, 0), (50, 0)):
        update = poll(value, at, players=players)
    assert update.finalized_events == ()
    assert value._servers[SERVER].engine.active_episode is None
    assert value._servers[SERVER].engine.provisional_drain is not None
    assert len(json.loads(active.read_text())["servers"][SERVER]["events"]) == 0


def test_false_timeout_is_discarded_and_does_not_create_exact_period(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    for at in (0, 10, 20):
        poll(value, at, players=15)
    poll(value, 30, ok=False)
    poll(value, 40, players=15)
    update = value.tick(SERVER, wall_at=BASE + 70, monotonic_at=70)
    assert update.finalized_events == ()
    assert value._servers[SERVER].engine.active_episode is None
    assert all(item.strict_direct_interval_count == 0 for item in value._servers[SERVER].score.candidates)


def test_two_strikes_gate_learner_and_healthy_resets_pending_strike(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=12)
    poll(value, 10, ok=False)
    assert value._servers[SERVER].engine.active_episode is None
    poll(value, 20, players=12)
    poll(value, 30, ok=False)
    assert value._servers[SERVER].engine.active_episode is None
    poll(value, 40, ok=False)
    assert value._servers[SERVER].engine.state is detection.EpisodeState.OFFLINE
    assert value._servers[SERVER].engine.active_episode.first_failure_mono == 30


def test_neutral_and_protocol_results_are_not_offline_strikes(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=12)
    value.ingest_live_result(
        SERVER,
        poll_generation=1,
        info={"ok": False, "neutral": True, "outcome": "alive-but-info-unavailable"},
        wall_at=BASE + 10,
        monotonic_at=10,
    )
    poll(value, 20, ok=False, err="bad packet")
    poll(value, 30, ok=False, err="bad packet")
    assert value._servers[SERVER].engine.active_episode is None
    poll(value, 40, ok=False)
    assert value._servers[SERVER].engine.active_episode is None
    poll(value, 50, ok=False)
    assert value._servers[SERVER].engine.state is detection.EpisodeState.OFFLINE


def test_pending_strike_expires_during_long_neutral_protocol_sequence(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=12)
    poll(value, 10, ok=False)
    value.ingest_live_result(
        SERVER,
        poll_generation=1,
        info={"ok": False, "neutral": True},
        wall_at=BASE + 20,
        monotonic_at=20,
    )
    poll(value, 30, ok=False, err="bad packet")
    poll(value, 40, ok=False)

    engine = value._servers[SERVER].engine
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.active_episode is None
    poll(value, 50, ok=False)
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 40


@pytest.mark.parametrize("gap", [10, 15, 17])
def test_realistic_jittered_consecutive_failures_confirm_learner(tmp_path, gap):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=12)
    poll(value, 10, ok=False)
    poll(value, 10 + gap, ok=False)

    engine = value._servers[SERVER].engine
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.first_failure_mono == 10
    assert engine.active_episode.confirmed_offline_mono == 10 + gap


def test_weak_recovery_then_independent_outage_uses_new_failure_clock(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    for at in (0, 10, 20):
        poll(value, at, players=12)
    poll(value, 30, players=0)
    poll(value, 60, ok=False)
    for at in (70, 80, 90):
        poll(value, at, players=12)
    weak = poll(value, 100, players=12).finalized_events
    assert weak == ()
    assert value._servers[SERVER].engine.active_episode is None

    poll(value, 200, ok=False)
    poll(value, 210, ok=False)
    episode = value._servers[SERVER].engine.active_episode
    assert episode.first_failure_mono == 200
    assert episode.confirmed_offline_mono == 210


def test_post_finalization_cleanup_allows_new_two_strike_outage(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    update = conventional(value)
    assert update.finalized_events[0].schedule_weight_suggestion > 0
    poll(value, 160, players=0)
    assert value._servers[SERVER].engine.active_episode is None
    poll(value, 200, ok=False)
    poll(value, 210, ok=False)
    assert value._servers[SERVER].engine.state is detection.EpisodeState.OFFLINE
    assert value._servers[SERVER].engine.active_episode.first_failure_mono == 200


def test_confirmed_outage_without_player_drain_has_positive_schedule_weight(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    for at in (0, 10, 20):
        poll(value, at, players=12)
    poll(value, 30, ok=False)
    poll(value, 40, ok=False)
    poll(value, 100, players=None)
    event = value.tick(SERVER, wall_at=BASE + 130, monotonic_at=130).finalized_events[0]
    assert event.outcome is detection.EventOutcome.CONFIRMED_OFFLINE_RESTART
    assert event.outage.first_failure_at == BASE + 30
    assert event.outage.confirmed_offline_at == BASE + 40
    assert event.schedule_weight_suggestion > 0


def test_bobs_three_restart_shape_produces_three_distinct_weighted_events(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    finalized = []
    for index, restart_at in enumerate((7_200, 18_000, 28_800)):
        weak_at = restart_at - (4_100 if index == 0 else 6_800 if index == 1 else 5_400)
        poll(value, weak_at, ok=False)
        poll(value, weak_at + 10, players=12 + index)
        for offset, players in ((-50, 12 + index), (-40, 12 + index), (-30, 8), (-20, 0), (-10, 0)):
            poll(value, restart_at + offset, players=players)
        poll(value, restart_at, ok=False)
        poll(value, restart_at + 10, ok=False)
        poll(value, restart_at + 70, players=0)
        finalized.extend(poll(value, restart_at + 100, players=0).finalized_events)

    weighted = [event for event in finalized if event.schedule_weight_suggestion > 0]
    assert len(weighted) == 3
    assert len({event.event_id for event in weighted}) == 3
    assert [event.outage.first_failure_at for event in weighted] == [
        BASE + 7_200,
        BASE + 18_000,
        BASE + 28_800,
    ]
    assert all(event.drain.zero_reached for event in weighted)
    assert all(detection.SignalSource.PLAYER_DRAIN in event.sources for event in weighted)


def test_replayed_fingerprint_is_not_routed_twice(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    event = crowbar(value).finalized_events[0]
    server = value._servers[SERVER]
    value._route_finalized(server, (event, replace(event, event_id="other")), now=BASE + 300)
    assert len(server.events) == 1


def test_lifecycle_closes_active_episode_and_breaks_coverage(tmp_path):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (34, 0)):
        poll(value, at, players=players)
    update = value.end_monitoring(
        SERVER, marker=detection.LifecycleMarker.PAUSE, wall_at=BASE + 35, monotonic_at=35
    )
    assert update.finalized_events == ()
    record = json.loads(active.read_text())["servers"][SERVER]
    assert record["active_episode"] is None
    assert record["incomplete_episodes"] == []
    assert record["monitoring_sessions"][-1]["reason"] == detection.LifecycleMarker.PAUSE.value


def test_runtime_fold_does_not_persist_learning_hints_for_lifecycle_events(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        replace(
            scored_event(hour, sequence, detection.EventOutcome.AMBIGUOUS_DRAIN),
            authenticity=0.35,
            schedule_weight_suggestion=0.15,
            coverage_complete=False,
            lifecycle_interruption=detection.LifecycleMarker.SHUTDOWN,
            reason_codes=("lifecycle_shutdown",),
        )
        for sequence, hour in enumerate((0, 3), 1)
    ]
    server.event_seq = 2

    value._fold_old_events(server, now=BASE + 3 * scoring.HOUR, target_limit=0)

    assert server.events == []
    assert all(
        candidate.hint_count == 0 and candidate.hint_weight == 0
        for candidate in server.aggregate.candidates
    )
    assert dict(server.aggregate.source_quality_counts) == {
        detection.EventOutcome.AMBIGUOUS_DRAIN.value: 2
    }
    assert server.aggregate.anomaly_count == 2


def test_lifecycle_event_remains_diagnostic_but_neutral_after_reload(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = value._server(SERVER)
    interrupted = replace(
        scored_event(0, 1, detection.EventOutcome.AMBIGUOUS_DRAIN),
        authenticity=0.35,
        schedule_weight_suggestion=0.15,
        coverage_complete=False,
        lifecycle_interruption=detection.LifecycleMarker.SHUTDOWN,
        reason_codes=("lifecycle_shutdown",),
    )
    value._route_finalized(server, (interrupted,), now=BASE)
    value._evaluate(server, now=BASE)
    assert value._persist_server(server, force=True, now=BASE)

    stored = json.loads(active.read_text())["servers"][SERVER]
    assert stored["events"][0]["lifecycle_interruption"] == "shutdown"
    assert not stored["events"][0]["coverage_complete"]

    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 1,
        app_session_id="reloaded-app-session",
    )
    loaded = again._servers[SERVER]
    result = scoring.RestartScheduleScorer().score(
        loaded.events,
        scoring.CoverageTimeline(tuple(loaded.coverage)),
        now=BASE + 1,
    )

    assert len(loaded.events) == 1
    assert loaded.events[0].lifecycle_interruption is detection.LifecycleMarker.SHUTDOWN
    assert all(
        candidate.hints.event_count == 0
        and candidate.hints.weighted_alignment == 0
        for candidate in result.candidates
    )
    assert result.schedule_existence_confidence == 0


def test_incomplete_confirmed_event_cannot_unlock_consumer_pattern(tmp_path, monkeypatch):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    complete = [scored_event(hour, sequence) for sequence, hour in enumerate((0, 3), 1)]
    incomplete = replace(scored_event(6, 3), coverage_complete=False)
    server.events = [*complete, incomplete]
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 7 * scoring.HOUR,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    observed_policies = []
    real_evaluate_consumers = runtime.evaluate_consumers

    def capture_policy(policy):
        observed_policies.append(policy)
        return real_evaluate_consumers(policy)

    monkeypatch.setattr(runtime, "evaluate_consumers", capture_policy)

    value._evaluate(server, now=BASE + 7 * scoring.HOUR)
    assert incomplete.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert not scoring.event_learning_eligible(incomplete)
    assert observed_policies[-1].independent_authentic_event_count == 2
    assert server.decision.model_status is consumers.ConsumerModelStatus.NO_PATTERN
    assert consumers.BlockReason.INSUFFICIENT_EVENTS in server.decision.model_reasons

    server.events[-1] = replace(incomplete, coverage_complete=True)
    value._evaluate(server, now=BASE + 7 * scoring.HOUR)
    assert observed_policies[-1].independent_authentic_event_count == 3
    assert server.decision.model_status is not consumers.ConsumerModelStatus.NO_PATTERN


def test_poll_coverage_is_dense_and_large_callback_gap_is_explicit(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=10)
    poll(value, 10, players=10)
    poll(value, 50, players=10)
    kinds = [item.kind for item in value._servers[SERVER].coverage]
    assert kinds == [runtime.CoverageKind.ONLINE_HEALTHY, runtime.CoverageKind.SLEEP_GAP]


@pytest.mark.parametrize(
    "payloads",
    [
        (
            {"ok": False, "neutral": True},
            {"ok": False, "neutral": True},
        ),
        (
            {"ok": False, "err": "bad packet", "a2s_classification": "malformed"},
            {"ok": False, "err": "invalid opcode", "a2s_classification": "malformed"},
        ),
        (
            {"ok": False, "err": "timed out", "a2s_classification": "timeout"},
            {"ok": False, "err": "truncated", "a2s_classification": "malformed"},
        ),
    ],
)
def test_neutral_protocol_and_mixed_failures_create_query_health_gap_not_offline(
    tmp_path, payloads
):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    for index, payload in enumerate(payloads):
        value.ingest_live_result(
            SERVER,
            poll_generation=1,
            info=payload,
            wall_at=BASE + index * 10,
            monotonic_at=index * 10,
        )

    coverage = value._servers[SERVER].coverage
    assert [item.kind for item in coverage] == [runtime.CoverageKind.QUERY_HEALTH_GAP]
    assert all(item.kind is not runtime.CoverageKind.OFFLINE_OBSERVED for item in coverage)
    assert value._servers[SERVER].engine.state is detection.EpisodeState.IDLE


def test_confirmed_offline_coverage_begins_after_second_qualifying_failure(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=10)
    poll(value, 10, ok=False)
    poll(value, 27, ok=False)
    assert all(
        item.kind is not runtime.CoverageKind.OFFLINE_OBSERVED
        for item in value._servers[SERVER].coverage
    )

    poll(value, 30, ok=False)
    coverage = value._servers[SERVER].coverage
    assert coverage[-1].kind is runtime.CoverageKind.OFFLINE_OBSERVED
    assert coverage[-1].start_at == BASE + 27
    assert coverage[-1].end_at == BASE + 30
    assert scoring.CoverageTimeline(tuple(coverage)).assess(BASE + 27, BASE + 30).fully_covered


def test_query_limited_expected_window_cannot_be_recorded_as_covered_miss(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(hour, index + 1)
        for index, hour in enumerate((0, 3, 6, 9, 12))
    ]
    tolerance = scoring.candidate_phase_tolerance(3 * scoring.HOUR)
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 12 * scoring.HOUR,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    value.decision(SERVER, now=BASE + 12 * scoring.HOUR + 60)
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 15 * scoring.HOUR - tolerance,
            scoring.CoverageKind.ONLINE_HEALTHY,
        ),
        scoring.CoverageSegment(
            BASE + 15 * scoring.HOUR - tolerance,
            BASE + 15 * scoring.HOUR + tolerance,
            scoring.CoverageKind.QUERY_HEALTH_GAP,
        ),
    ]

    value._evaluate_expected_windows(
        server, now=BASE + 15 * scoring.HOUR + tolerance
    )
    assert not any(
        item.period_seconds == 3 * scoring.HOUR
        and item.expected_at == BASE + 15 * scoring.HOUR
        for item in server.expected_misses
    )


def test_missing_players_and_query_transition_do_not_claim_healthy_coverage(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=10)
    poll(value, 10, players=None)
    poll(value, 20, ok=False)
    assert [item.kind for item in value._servers[SERVER].coverage] == [
        runtime.CoverageKind.MISSING_PLAYERS,
    ]


def test_no_atomic_save_on_every_healthy_poll_but_transition_saves(tmp_path, monkeypatch):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    calls = []
    real = runtime.atomic_write_json
    monkeypatch.setattr(runtime, "atomic_write_json", lambda path, data: (calls.append(path), real(path, data))[1])
    poll(value, 1, players=12)
    poll(value, 10, players=12)
    assert calls == []
    poll(value, 20, ok=False)
    poll(value, 30, ok=False)
    assert calls


def test_atomic_save_failure_disables_future_persistence_without_corrupting_previous(tmp_path, monkeypatch):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    before = active.read_bytes()
    monkeypatch.setattr(runtime, "atomic_write_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    poll(value, 0, ok=False)
    poll(value, 10, ok=False)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert active.read_bytes() == before
    notice = value.pending_notice
    assert notice.kind == "persistence_write_failed"
    assert "not being saved" in notice.title.lower()
    assert "OSError: disk" in notice.body
    assert value.persistence_error == "OSError: disk"
    assert value.pending_notice == notice


def test_save_load_round_trip_preserves_events_coverage_and_suppression(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    begin(value)
    event = crowbar(value).finalized_events[0]
    decision = value.decision(SERVER, now=BASE + 250, event=event)
    key = decision.query_visible_key or consumers.event_alert_key(
        consumers.AlertKeyKind.QUERY_VISIBLE_RECOVERY,
        server_key=SERVER,
        event_id=event.event_id,
        candidate_period_seconds=None,
        regime_generation=value._servers[SERVER].regime_generation,
    )
    value.mark_fired(SERVER, key, now=BASE + 251)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE + 300, app_session_id="new"
    )
    server = again._servers[SERVER]
    assert [item.fingerprint for item in server.events] == [event.fingerprint]
    assert server.coverage
    assert key in server.fired_keys


def test_malformed_one_server_fails_closed_without_destroying_other_server(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    state = json.loads(active.read_text())
    state["servers"] = {
        "bad": {"events": "not-a-list", "regime_generation": 3},
        "good": {"runtime_state_version": 1, "events": [], "coverage_segments": [], "regime_generation": "good-regime"},
    }
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE + 1, app_session_id="new"
    )
    assert set(again._servers) == {"bad", "good"}
    assert again.decision("bad", now=BASE + 2).prediction_usable is False
    assert again._servers["good"].regime_generation == "good-regime"


def test_fired_key_is_recorded_once_and_regime_is_part_of_identity(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    key = consumers.event_alert_key(
        consumers.AlertKeyKind.GENERIC_RECOVERY,
        server_key=SERVER,
        event_id="event",
        candidate_period_seconds=None,
        regime_generation="regime-a",
    )
    assert value.mark_fired(SERVER, key, now=BASE)
    assert not value.mark_fired(SERVER, key, now=BASE + 1)


def test_phase2_summary_never_exposes_prediction_when_policy_blocks_it(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    decision = value.decision(SERVER, now=BASE)
    assert runtime.phase2_learning_summary(decision, now=BASE) is None
    usability = runtime.phase2_alert_usability(decision)
    assert usability["usable"] is False


def test_crowbar_query_only_covered_4h_model_reaches_active_consumer_gates(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(hour, index + 1, detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART)
        for index, hour in enumerate(range(0, 32, 4))
    ]
    server.coverage = [
        scoring.CoverageSegment(BASE, BASE + 28 * scoring.HOUR, scoring.CoverageKind.ONLINE_HEALTHY)
    ]
    decision = value.decision(
        SERVER,
        now=BASE + 28 * scoring.HOUR + 60,
        event=server.events[-1],
        restart_alert_enabled=True,
    )
    assert decision.selected_period_seconds == 4 * scoring.HOUR
    assert decision.period_confirmed
    assert decision.prediction_usable
    assert decision.query_visible_recovery_eligible


@pytest.mark.parametrize("inactive_days", [7, 31])
def test_runtime_scoring_retains_safe_query_visible_model_through_inactivity(
    tmp_path, inactive_days
):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(
            hour,
            index + 1,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        )
        for index, hour in enumerate(range(0, 36, 4))
    ]
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 32 * scoring.HOUR,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    recent_now = BASE + 32 * scoring.HOUR + 60
    value.decision(SERVER, now=recent_now)
    recent = server.score.candidate(4 * scoring.HOUR)

    stale_now = recent_now + inactive_days * DAY
    decision = value.decision(SERVER, now=stale_now)
    stale = server.score.candidate(4 * scoring.HOUR)
    prediction = runtime._next_prediction(server.score, stale_now)

    assert stale.phase_confidence == recent.phase_confidence
    assert stale.fundamental_period_confidence == recent.fundamental_period_confidence
    assert stale.confidence_cap == recent.confidence_cap
    assert stale.fundamental_period_confidence > 0.79
    assert stale.confidence_cap > 0.79
    assert server.score.regime_status is scoring.RegimeStatus.STABLE
    assert decision.prediction_usable
    assert prediction is not None and prediction > stale_now
    assert (prediction - stale.phase_offset) % (4 * scoring.HOUR) == 0


def test_reload_recomputes_retained_model_with_inactivity_neutral_policy(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(
            hour,
            index + 1,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        )
        for index, hour in enumerate(range(0, 36, 4))
    ]
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 32 * scoring.HOUR,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    recent_now = BASE + 32 * scoring.HOUR + 60
    value.decision(SERVER, now=recent_now)
    before = server.score.candidate(4 * scoring.HOUR)
    reload_now = recent_now + 31 * DAY
    server.score = value.scorer.score(
        server.events,
        scoring.CoverageTimeline(tuple(server.coverage)),
        now=reload_now,
        incumbent_period_seconds=server.incumbent_period_seconds,
        phase_recency_policy=scoring.PhaseRecencyPolicy.LEGACY_DECAY,
    )
    assert server.score.regime_status is scoring.RegimeStatus.PHASE_UNCERTAIN
    assert server.score.candidate(4 * scoring.HOUR).phase_confidence <= 0.60
    server.dirty = True
    assert value._persist_server(server, force=True, now=reload_now)

    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=reload_now,
        app_session_id="inactivity-neutral-reload",
    )
    restored = again._servers[SERVER]
    after = restored.score.candidate(4 * scoring.HOUR)

    assert after.phase_confidence == before.phase_confidence
    assert after.fundamental_period_confidence == before.fundamental_period_confidence
    assert after.confidence_cap == before.confidence_cap
    assert restored.score.regime_status is scoring.RegimeStatus.STABLE
    assert restored.score.prediction_usable
    assert runtime._next_prediction(restored.score, reload_now) > reload_now


def test_compaction_fold_uses_inactivity_neutral_phase_policy(tmp_path, monkeypatch):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [scored_event(hour, index + 1) for index, hour in enumerate((0, 4, 8))]
    policies = []
    score = value.scorer.score

    def recording_score(*args, **kwargs):
        policies.append(kwargs.get("phase_recency_policy"))
        return score(*args, **kwargs)

    monkeypatch.setattr(value.scorer, "score", recording_score)
    value._fold_old_events(server, now=BASE + 31 * DAY, target_limit=2)

    assert policies == [scoring.PhaseRecencyPolicy.INACTIVITY_NEUTRAL]


def test_dawn_sparse_12h_hints_cannot_beat_covered_3h_evidence(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    hours = (0, 12, 24, 100, 103, 106, 109, 112)
    server.events = [scored_event(hour, index + 1) for index, hour in enumerate(hours)]
    server.coverage = [
        scoring.CoverageSegment(BASE, BASE + 100 * scoring.HOUR, scoring.CoverageKind.UNMONITORED),
        scoring.CoverageSegment(BASE + 100 * scoring.HOUR, BASE + 112 * scoring.HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
    ]
    decision = value.decision(SERVER, now=BASE + 112 * scoring.HOUR + 60)
    score = value._servers[SERVER].score
    assert decision.selected_period_seconds == 3 * scoring.HOUR
    assert score.candidate(3 * scoring.HOUR).direct_support > score.candidate(12 * scoring.HOUR).direct_support
    assert score.candidate(12 * scoring.HOUR).strict_direct_interval_count == 0
    assert score.candidate(12 * scoring.HOUR).fundamental_period_confidence == 0


def test_completed_covered_candidate_window_records_one_parallel_miss(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [scored_event(hour, index + 1) for index, hour in enumerate((0, 3, 6, 9, 12))]
    tolerance = scoring.candidate_phase_tolerance(3 * scoring.HOUR)
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 15 * scoring.HOUR + tolerance,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    value.decision(SERVER, now=BASE + 12 * scoring.HOUR + 60)
    value._evaluate_expected_windows(server, now=BASE + 15 * scoring.HOUR + tolerance)
    first = tuple(server.expected_misses)
    value._evaluate_expected_windows(server, now=BASE + 15 * scoring.HOUR + tolerance + 1)
    assert tuple(server.expected_misses) == first
    assert any(item.period_seconds == 3 * scoring.HOUR for item in first)


def test_query_visible_stable_zero_expected_window_is_evidence_neutral(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(
            hour,
            index + 1,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        )
        for index, hour in enumerate((0, 3, 6, 9, 12))
    ]
    tolerance = scoring.candidate_phase_tolerance(3 * scoring.HOUR)
    expected = BASE + 15 * scoring.HOUR
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            expected + tolerance,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    value._evaluate(server, now=expected + tolerance)
    before = server.score.candidate(3 * scoring.HOUR)
    server.player_observations = [
        (at, 0)
        for at in (
            expected - tolerance,
            expected,
            expected + tolerance,
        )
    ]

    value._evaluate_expected_windows(server, now=expected + tolerance)
    value._evaluate(server, now=expected + tolerance)
    after = server.score.candidate(3 * scoring.HOUR)

    assert not any(
        item.period_seconds == 3 * scoring.HOUR and item.expected_at == expected
        for item in server.expected_misses
    )
    assert after.covered_miss_count == before.covered_miss_count
    assert after.fundamental_period_confidence == before.fundamental_period_confidence


def test_query_visible_populated_expected_window_still_records_covered_miss(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(
            hour,
            index + 1,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        )
        for index, hour in enumerate((0, 3, 6, 9, 12))
    ]
    tolerance = scoring.candidate_phase_tolerance(3 * scoring.HOUR)
    expected = BASE + 15 * scoring.HOUR
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            expected + tolerance,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    ]
    value._evaluate(server, now=expected + tolerance)
    server.player_observations = [
        (expected - tolerance, 8),
        (expected, 7),
        (expected + tolerance, 9),
    ]

    value._evaluate_expected_windows(server, now=expected + tolerance)

    assert any(
        item.period_seconds == 3 * scoring.HOUR and item.expected_at == expected
        for item in server.expected_misses
    )


def test_unmonitored_candidate_window_never_records_a_miss(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [scored_event(hour, index + 1) for index, hour in enumerate((0, 3, 6, 9, 12))]
    server.coverage = [
        scoring.CoverageSegment(BASE, BASE + 18 * scoring.HOUR, scoring.CoverageKind.UNMONITORED)
    ]
    value.decision(SERVER, now=BASE + 12 * scoring.HOUR + 60)
    value._evaluate_expected_windows(server, now=BASE + 18 * scoring.HOUR)
    assert server.expected_misses == []


def test_persisted_and_recomputed_expected_miss_cannot_double_penalize():
    events = tuple(scored_event(hour, index + 1) for index, hour in enumerate((0, 6, 12)))
    coverage = scoring.CoverageTimeline((
        scoring.CoverageSegment(BASE, BASE + 12 * scoring.HOUR, scoring.CoverageKind.ONLINE_HEALTHY),
    ))
    miss = scoring.CoveredExpectedMiss(3 * scoring.HOUR, BASE + 3 * scoring.HOUR, "3h:first")
    result = scoring.RestartScheduleScorer().score(
        events,
        coverage,
        now=BASE + 12 * scoring.HOUR,
        expected_misses=(miss, miss),
    )
    assert result.candidate(3 * scoring.HOUR).covered_miss_count == 2
    # One at 03:00 was supplied twice, while 09:00 is a distinct internally
    # derived covered midpoint miss.


def test_multiple_learned_candidate_records_later_fully_covered_expected_miss(tmp_path):
    value, *_ = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(hour, index + 1)
        for index, hour in enumerate((0, 6, 12, 18, 24))
    ]
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 24 * scoring.HOUR,
            scoring.CoverageKind.UNMONITORED,
        )
    ]
    value.decision(SERVER, now=BASE + 24 * scoring.HOUR)
    candidate = server.score.candidate(3 * scoring.HOUR)
    assert candidate.strict_direct_interval_count == 0
    assert candidate.compatible_multiple_interval_count == 4

    tolerance = scoring.candidate_phase_tolerance(3 * scoring.HOUR)
    server.coverage.append(
        scoring.CoverageSegment(
            BASE + 27 * scoring.HOUR - tolerance,
            BASE + 27 * scoring.HOUR + tolerance,
            scoring.CoverageKind.ONLINE_HEALTHY,
        )
    )
    value._evaluate_expected_windows(
        server,
        now=BASE + 27 * scoring.HOUR + tolerance,
    )
    assert sum(
        item.period_seconds == 3 * scoring.HOUR
        and item.expected_at == BASE + 27 * scoring.HOUR
        for item in server.expected_misses
    ) == 1


def test_multiple_scoring_is_recomputed_identically_after_store_reload(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.events = [
        scored_event(hour, index + 1)
        for index, hour in enumerate(range(0, 48, 6))
    ]
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 42 * scoring.HOUR,
            scoring.CoverageKind.UNMONITORED,
        )
    ]
    value.decision(SERVER, now=BASE + 42 * scoring.HOUR)
    before = server.score.candidate(3 * scoring.HOUR)
    server.dirty = True
    assert value._persist_server(server, force=True, now=BASE + 42 * scoring.HOUR)
    stored = json.loads(active.read_text())
    diagnostics = stored["servers"][SERVER]["candidate_diagnostics"]["candidates"]
    stored_three = next(
        item for item in diagnostics if item["period_seconds"] == 3 * scoring.HOUR
    )
    assert stored_three["compatible_multiple_interval_count"] == 7

    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 42 * scoring.HOUR,
        app_session_id="phase3-reload",
    )
    after = again._servers[SERVER].score.candidate(3 * scoring.HOUR)
    assert after.compatible_multiple_support == before.compatible_multiple_support
    assert after.compatible_multiple_interval_count == before.compatible_multiple_interval_count
    assert after.fundamental_period_confidence == before.fundamental_period_confidence
    assert after.establishment_gates_passed == before.establishment_gates_passed
    assert again._servers[SERVER].score.selected_period_seconds == 3 * scoring.HOUR


def test_shutdown_is_idempotent_and_flushes_active_episode(tmp_path):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=10)
    poll(value, 10, players=0)
    value.shutdown(wall_at=BASE + 20, monotonic_at=20)
    first = active.read_bytes()
    value.shutdown(wall_at=BASE + 30, monotonic_at=30)
    assert active.read_bytes() == first


def make_startup(pending=True, *, update=False, blocked=False, show_result=True):
    calls = []
    state = {"blocked": blocked, "update": update}
    notice = runtime.RuntimeNotice("legacy_reset", "Reset", "Body", "/tmp/backup") if pending else None
    coordinator = StartupPresentationCoordinator(
        pending_notice=notice,
        hide_startup=lambda: calls.append("hide"),
        maybe_show_update=lambda: calls.append("update") or state["update"],
        blockers_visible=lambda: state["blocked"],
        show_notice=lambda item: calls.append(("notice", item.kind)) or show_result,
    )
    return coordinator, state, calls


def test_startup_no_update_shows_notice_after_overlay_closes():
    coordinator, _state, calls = make_startup(update=False)
    result = coordinator.complete_startup()
    assert result.notice_shown
    assert calls == ["hide", "update", ("notice", "legacy_reset")]


def test_startup_update_shown_waits_for_canonical_visibility_callback():
    coordinator, _state, calls = make_startup(update=True)
    result = coordinator.complete_startup()
    assert result.update_shown and not result.notice_shown
    assert coordinator.update_visibility_changed(False)
    assert calls[-1] == ("notice", "legacy_reset")


def test_update_open_url_does_not_dismiss_or_reveal_notice():
    coordinator, _state, _calls = make_startup(update=True)
    coordinator.complete_startup()
    assert not coordinator.update_visibility_changed(True)
    assert coordinator.pending_notice is not None


@pytest.mark.parametrize(
    "settings",
    [
        {"skipped_release_tag": "v9.9.9"},
        {"update_remind_after_ts": int(BASE + 86400)},
    ],
)
def test_update_maybe_show_returns_false_when_suppressed(settings, monkeypatch):
    fake = type("Window", (), {})()
    fake._update_info = {"tag": "v9.9.9", "url": "https://example.invalid"}
    fake._update_card_dismissed = False
    fake.settings = settings
    fake.visibility = []
    fake._on_update_ui_visibility_changed = fake.visibility.append
    ui = UpdateUI(fake)
    monkeypatch.setattr("dzll_launcher.update_ui.time.time", lambda: BASE)
    assert ui.maybe_show() is False
    assert fake.visibility == []


def test_update_maybe_show_returns_true_only_when_card_is_revealed():
    fake = type("Window", (), {})()
    fake._update_info = {"tag": "v9.9.9", "url": "https://example.invalid"}
    fake._update_card_dismissed = False
    fake.settings = {}
    fake.visibility = []
    fake._on_update_ui_visibility_changed = fake.visibility.append
    ui = UpdateUI(fake)
    assert ui.maybe_show() is True
    assert fake.visibility == [True]


def test_all_update_dismissal_methods_use_canonical_visibility_callback(monkeypatch):
    fake = type("Window", (), {})()
    fake._update_info = {"tag": "v9.9.9", "url": "https://example.invalid"}
    fake._update_card_dismissed = False
    fake.settings = {}
    fake.visibility = []
    fake._on_update_ui_visibility_changed = fake.visibility.append
    monkeypatch.setattr("dzll_launcher.update_ui.save_settings", lambda _settings: None)
    ui = UpdateUI(fake)
    ui.close_for_session()
    ui.remind_later()
    ui.skip_this_version()
    assert fake.visibility == [False, False, False]


def test_blocking_overlay_defers_then_retries_notice():
    coordinator, state, _calls = make_startup(blocked=True)
    coordinator.complete_startup()
    assert coordinator.pending_notice is not None
    state["blocked"] = False
    assert coordinator.blocker_visibility_changed()


def test_repeated_startup_completion_attempts_update_and_notice_once():
    coordinator, _state, calls = make_startup()
    coordinator.complete_startup()
    coordinator.complete_startup()
    coordinator.update_visibility_changed(False)
    assert calls.count("hide") == 1
    assert calls.count("update") == 1
    assert calls.count(("notice", "legacy_reset")) == 1


def test_no_reset_means_no_notice_and_shutdown_prevents_late_notice():
    no_notice, _state, calls = make_startup(pending=False)
    no_notice.complete_startup()
    assert not any(isinstance(item, tuple) for item in calls)
    pending, _state, _calls = make_startup(update=True)
    pending.complete_startup()
    pending.begin_shutdown()
    assert not pending.update_visibility_changed(False)


def test_dormant_layers_do_not_resolve_real_config_paths():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/dzll_launcher/companion_restart_phase2_runtime.py").read_text()
    assert "/home/gbrown" not in source
    assert "Gtk" not in source and "GLib" not in source


def test_window_activates_phase2_without_phase1_learning_calls():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/dzll_launcher/window.py").read_text()
    assert "Phase2RestartRuntime.initialize" in source
    assert "load_companion_restart_learning" not in source
    assert "save_companion_restart_learning" not in source
    assert "companion_restart_learning." not in source
    assert "record_query_visible_restart" not in source
    assert "record_confirmed_outage(" not in source


def test_window_has_one_central_startup_completion_contract():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/dzll_launcher/window.py").read_text()
    assert "def _complete_startup_presentation" in source
    assert "GLib.idle_add(self.update_ui.maybe_show)" not in source
    assert source.count("self._complete_startup_presentation()") >= 6
    manual = source[source.index("def _manual_update_server_database"):source.index("def _check_updates_worker")]
    assert "_complete_startup_presentation" not in manual


def test_window_live_result_has_no_pre_cutover_immediate_recovery_branch():
    from dzll_launcher.window import DZLLWindow

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(DZLLWindow._apply_server_companion_live_result))
    )
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED" not in names
    assert "_server_companion_alert_armed" not in attributes
    assert "scheduled_outage_relaxation_usable" not in attributes
    assert "_maybe_hold_server_companion_confirmed_recovery_alert" in attributes
    assert "_emit_server_companion_confirmed_recovery_alert" in attributes


def test_phase2_integration_has_no_release_or_packaging_side_effects():
    root = Path(__file__).resolve().parents[1]
    runtime_source = (root / "src/dzll_launcher/companion_restart_phase2_runtime.py").read_text()
    assert "APP_VERSION" not in runtime_source
    assert "subprocess" not in runtime_source
    assert "import audio" not in runtime_source.lower()
    assert "_play_" not in runtime_source
