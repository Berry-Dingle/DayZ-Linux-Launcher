import hashlib
from dataclasses import replace
from pathlib import Path

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 2_100_000_000.0
SERVER = "198.51.100.88:2302"


def make_runtime(tmp_path, *, now=BASE):
    active = tmp_path / storage.PHASE2_ACTIVE_FILENAME
    legacy = tmp_path / storage.LEGACY_PHASE1_FILENAME
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=now,
        app_session_id="stage6-app",
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    return value, active, legacy


def sample(server_key, at, players=12):
    return detection.ObservationSample(
        wall_at=at,
        monotonic_at=max(0, at - BASE),
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key=server_key,
        info_status=detection.InfoStatus.HEALTHY,
        info_latency=0.04,
        player_status=detection.FieldStatus.PRESENT,
        players=players,
        max_players=60,
        queue_status=detection.FieldStatus.PRESENT,
        queue=0,
    )


def event(
    hour,
    sequence,
    *,
    server_key=SERVER,
    outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    fingerprint=None,
    samples=(),
):
    at = BASE + hour * scoring.HOUR
    return detection.PhysicalRestartEvent(
        event_id=f"{server_key}-event-{sequence}",
        sequence=sequence,
        fingerprint=fingerprint or f"{server_key}-fingerprint-{sequence}",
        server_key=server_key,
        episode_started_at=at - 120,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset(
            {detection.SignalSource.INFO_OUTAGE, detection.SignalSource.INFO_RETURN}
        ),
        outcome=outcome,
        authenticity=0.98,
        schedule_weight_suggestion=1.0,
        drain=detection.DrainSummary(
            12, at - 180, at - 150, at - 150, True, 0, 1.0, 3, 120, True
        ),
        outage=detection.OutageSummary(
            at - 60, at - 54, 2, (detection.InfoStatus.TIMEOUT,), at
        ),
        recovery=detection.RecoverySummary(None, at, at + 10, 2, False),
        query_health=detection.QueryHealthSummary(20, 2, 0, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=tuple(samples),
        reason_codes=("stage6_fixture",),
    )


def populate_server(server, hours, *, sample_payload=False):
    shared_samples = ()
    if sample_payload:
        shared_samples = tuple(
            sample(server.key, BASE + index * 10, index % 20) for index in range(48)
        )
    server.events = [
        event(hour, index + 1, server_key=server.key, samples=shared_samples)
        for index, hour in enumerate(hours)
    ]
    server.event_seq = len(server.events)
    server.routed_fingerprints = {item.fingerprint for item in server.events}
    if server.events:
        start = server.events[0].canonical_phase_at
        end = server.events[-1].canonical_phase_at
        server.coverage = [
            scoring.CoverageSegment(
                start,
                end + 60,
                scoring.CoverageKind.ONLINE_HEALTHY,
                10,
            )
        ]


def test_runtime_fold_preserves_boundary_intervals_once_across_reload(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = value._server(SERVER)
    server.coverage = [
        scoring.CoverageSegment(
            BASE,
            BASE + 300 * scoring.HOUR,
            scoring.CoverageKind.ONLINE_HEALTHY,
            10,
        )
    ]
    for index in range(100):
        value._route_finalized(
            server,
            (event(index * 3, index + 1),),
            now=BASE + index * 3 * scoring.HOUR + 30,
        )
    aggregate = server.aggregate.candidate(3 * scoring.HOUR)
    assert len(server.events) == runtime.MAX_FINALIZED_EVENTS
    assert server.folded_through_event_seq == 20
    assert aggregate.direct_count == 20
    assert aggregate.direct_weight == 20
    assert dict(server.aggregate.source_quality_counts) == {
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART.value: 20
    }
    before = server.aggregate
    server.dirty = True
    assert value._persist_server(server, force=True, now=BASE + 301 * scoring.HOUR)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 301 * scoring.HOUR,
        app_session_id="reload",
    )
    loaded = again._servers[SERVER]
    assert loaded.aggregate == before
    again._fold_old_events(loaded, now=BASE + 301 * scoring.HOUR)
    assert loaded.aggregate == before


def test_global_compaction_uses_full_compact_and_cold_tiers(tmp_path):
    value, *_ = make_runtime(tmp_path)
    for index in range(260):
        key = f"198.51.{index // 250}.{index % 250}:2302"
        server = runtime._new_server_runtime(key, value.detection_config)
        hours = tuple(index * 100 + offset * 3 for offset in range(13))
        populate_server(server, hours, sample_payload=True)
        value._servers[key] = server
        value.state["servers"][key] = runtime._serialize_server(server)
    active_key = "198.51.1.9:2302"
    value._compact_global_history(active_key=active_key, now=BASE + 30_000 * scoring.HOUR)
    lengths = [len(item.events) for item in value._servers.values()]
    assert lengths.count(13) == runtime.MAX_FULL_DETAIL_SERVERS
    assert lengths.count(runtime.COMPACT_EVENT_LIMIT) == (
        runtime.MAX_COMPACT_DETAIL_SERVERS - runtime.MAX_FULL_DETAIL_SERVERS
    )
    assert lengths.count(runtime.COLD_EVENT_LIMIT) == 10
    assert all(
        not event.samples
        for item in value._servers.values()
        if len(item.events) < 13
        for event in item.events
    )
    cold = next(item for item in value._servers.values() if len(item.events) == 2)
    assert cold.folded_through_event_seq == 11
    assert cold.aggregate.candidate(3 * scoring.HOUR).direct_count == 11


def test_representative_250_server_state_stays_below_soft_target(tmp_path):
    value, *_ = make_runtime(tmp_path)
    for index in range(250):
        key = f"203.0.{index // 250}.{index % 250}:2302"
        server = runtime._new_server_runtime(key, value.detection_config)
        count = 80 if index < 25 else 3
        hours = tuple(index * 400 + offset * 3 for offset in range(count))
        populate_server(server, hours, sample_payload=True)
        value._servers[key] = server
        value.state["servers"][key] = runtime._serialize_server(server)
    value._compact_global_history(
        active_key="203.0.0.249:2302", now=BASE + 200_000 * scoring.HOUR
    )
    destination = tmp_path / "bounded.json"
    storage.atomic_write_json(destination, value.state)
    assert destination.stat().st_size < 25 * 1024 * 1024
    assert storage.load_phase2_state(destination)["servers"].keys() == value.state[
        "servers"
    ].keys()


def test_future_persisted_evidence_is_removed_before_consumer_use(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = runtime._new_server_runtime(SERVER, value.detection_config)
    populate_server(server, tuple(24 * 365 * 10 + offset * 3 for offset in range(6)))
    state = storage.new_phase2_state(
        now=BASE, generation_id="22222222-2222-4222-8222-222222222222"
    )
    state["servers"][SERVER] = runtime._serialize_server(server)
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE, app_session_id="reload"
    )
    assert again._servers[SERVER].events == []
    assert again._servers[SERVER].coverage == []
    decision = again.decision(SERVER, now=BASE)
    assert not decision.period_confirmed
    assert not decision.prediction_usable
    assert not decision.warning_eligible


def test_malformed_boolean_cannot_turn_events_into_covered_proof(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = runtime._new_server_runtime(SERVER, value.detection_config)
    populate_server(server, (0, 3, 6, 9, 12, 15))
    record = runtime._serialize_server(server)
    for item in record["events"]:
        item["coverage_complete"] = "true"
        item["query_health"]["continuous"] = 1
    state = storage.new_phase2_state(
        now=BASE, generation_id="33333333-3333-4333-8333-333333333333"
    )
    state["servers"][SERVER] = record
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 16 * scoring.HOUR,
        app_session_id="reload",
    )
    candidate = again.decision(SERVER, now=BASE + 16 * scoring.HOUR)
    assert all(not item.coverage_complete for item in again._servers[SERVER].events)
    assert not candidate.period_confirmed
    assert not candidate.prediction_usable


def test_duplicate_physical_fingerprint_is_sanitized_and_not_scored_twice(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = runtime._new_server_runtime(SERVER, value.detection_config)
    populate_server(server, (0, 3, 6, 9, 12))
    duplicate = replace(
        server.events[-1],
        event_id="duplicate-id",
        sequence=99,
    )
    server.events.append(duplicate)
    state = storage.new_phase2_state(
        now=BASE, generation_id="44444444-4444-4444-8444-444444444444"
    )
    state["servers"][SERVER] = runtime._serialize_server(server)
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 13 * scoring.HOUR,
        app_session_id="reload",
    )
    assert len(again._servers[SERVER].events) == 5
    assert len({item.fingerprint for item in again._servers[SERVER].events}) == 5


def test_conflicting_event_sequence_fails_only_that_server_closed(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    bad = runtime._new_server_runtime("bad", value.detection_config)
    populate_server(bad, (0, 3))
    bad.events[1] = replace(bad.events[1], sequence=bad.events[0].sequence)
    good = runtime._new_server_runtime("good", value.detection_config)
    populate_server(good, (0, 3))
    state = storage.new_phase2_state(
        now=BASE, generation_id="55555555-5555-4555-8555-555555555555"
    )
    state["servers"] = {
        "bad": runtime._serialize_server(bad),
        "good": runtime._serialize_server(good),
    }
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 4 * scoring.HOUR,
        app_session_id="reload",
    )
    assert again._servers["bad"].events == []
    assert len(again._servers["good"].events) == 2
    assert not again.decision("bad", now=BASE + 4 * scoring.HOUR).prediction_usable


def test_fold_high_water_prevents_detailed_event_reapplication(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = runtime._new_server_runtime(SERVER, value.detection_config)
    populate_server(server, (0, 3, 6))
    record = runtime._serialize_server(server)
    record["folded_through_event_seq"] = 2
    record["aggregate"]["candidates"][1]["direct_count"] = 2
    record["aggregate"]["candidates"][1]["direct_weight"] = 2.0
    state = storage.new_phase2_state(
        now=BASE, generation_id="66666666-6666-4666-8666-666666666666"
    )
    state["servers"][SERVER] = record
    storage.atomic_write_json(active, state)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 7 * scoring.HOUR,
        app_session_id="reload",
    )
    loaded = again._servers[SERVER]
    assert [item.sequence for item in loaded.events] == [3]
    before = loaded.aggregate
    again._fold_old_events(loaded, now=BASE + 7 * scoring.HOUR, target_limit=0)
    assert loaded.aggregate.candidate(3 * scoring.HOUR).direct_count == before.candidate(
        3 * scoring.HOUR
    ).direct_count


def test_rollback_and_return_to_phase2_keeps_active_authoritative(tmp_path):
    active = tmp_path / storage.PHASE2_ACTIVE_FILENAME
    legacy = tmp_path / storage.LEGACY_PHASE1_FILENAME
    first_bytes = b'{"version": 2, "confidence": 0.95}\n'
    legacy.write_bytes(first_bytes)
    first = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE, app_session_id="first"
    )
    assert first.migration.reset_performed
    original_generation = first.state["state_generation_id"]
    rollback_bytes = b'{"version": 2, "confidence": 0.25}\n'
    legacy.write_bytes(rollback_bytes)
    second = runtime.Phase2RestartRuntime.initialize(
        active_path=active, legacy_path=legacy, now=BASE + 1, app_session_id="second"
    )
    assert not second.migration.reset_performed
    assert second.pending_notice is None
    assert second.state["state_generation_id"] == original_generation
    assert legacy.read_bytes() == rollback_bytes
    backups = list(tmp_path.glob(f"{storage.LEGACY_BACKUP_PREFIX}-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == first_bytes


def test_deleted_or_corrupt_active_has_deterministic_rollback_outcomes(tmp_path):
    active = tmp_path / storage.PHASE2_ACTIVE_FILENAME
    legacy = tmp_path / storage.LEGACY_PHASE1_FILENAME
    legacy.write_bytes(b"legacy-one")
    migrated = storage.initialize_phase2_state(
        active_path=active, legacy_path=legacy, now=BASE
    )
    active.unlink()
    legacy.write_bytes(b"legacy-two")
    repeated = storage.initialize_phase2_state(
        active_path=active, legacy_path=legacy, now=BASE + 1
    )
    assert repeated.reset_performed
    assert repeated.legacy_checksum == hashlib.sha256(b"legacy-two").hexdigest()
    active.write_bytes(b"corrupt phase two")
    legacy.write_bytes(b"rollback legacy")
    recovered = storage.initialize_phase2_state(
        active_path=active, legacy_path=legacy, now=BASE + 2
    )
    assert recovered.recovery_performed
    assert not recovered.reset_performed
    assert legacy.read_bytes() == b"rollback legacy"
    active.unlink()
    legacy.unlink()
    fresh = storage.initialize_phase2_state(
        active_path=active, legacy_path=legacy, now=BASE + 3
    )
    assert fresh.status is storage.Phase2InitializationStatus.FRESH_CREATED
    assert not fresh.reset_performed


def test_thousand_healthy_polls_coalesce_coverage_and_not_every_poll_write(
    tmp_path, monkeypatch
):
    value, *_ = make_runtime(tmp_path)
    writes = []
    real_write = runtime.atomic_write_json

    def counted_write(path, state):
        writes.append(path)
        return real_write(path, state)

    monkeypatch.setattr(runtime, "atomic_write_json", counted_write)
    value.begin_monitoring(
        SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1
    )
    for index in range(1_000):
        at = index * 10
        value.ingest_live_result(
            SERVER,
            poll_generation=1,
            info={"ok": True, "players": 12, "max_players": 60, "ping_ms": 40},
            wall_at=BASE + at,
            monotonic_at=at,
        )
    server = value._servers[SERVER]
    assert len(writes) < 180
    assert len(server.coverage) == 1
    assert server.coverage[0].kind is scoring.CoverageKind.ONLINE_HEALTHY


def test_runtime_schedule_change_suspends_then_persists_new_regime(tmp_path):
    value, active, legacy = make_runtime(tmp_path)
    server = value._server(SERVER)
    populate_server(server, tuple(range(0, 24, 3)))
    server.coverage = [
        scoring.CoverageSegment(
            BASE, BASE + 54 * scoring.HOUR, scoring.CoverageKind.ONLINE_HEALTHY
        )
    ]
    initial = value.decision(SERVER, now=BASE + 21 * scoring.HOUR)
    assert initial.selected_period_seconds == 3 * scoring.HOUR
    server.events.extend(
        event(hour, len(server.events) + index + 1)
        for index, hour in enumerate((24, 30, 36))
    )
    transition = value.decision(SERVER, now=BASE + 36 * scoring.HOUR)
    assert transition.regime_status is scoring.RegimeStatus.CHANGE_SUSPECTED
    assert not transition.prediction_usable
    server.events.extend(
        event(hour, len(server.events) + index + 1)
        for index, hour in enumerate((42, 48, 54))
    )
    changed = value.decision(SERVER, now=BASE + 54 * scoring.HOUR)
    assert changed.selected_period_seconds == 6 * scoring.HOUR
    assert not changed.prediction_usable
    assert value._servers[SERVER].prior_regimes[-1].period_seconds == 3 * scoring.HOUR
    server.dirty = True
    value._persist_server(server, force=True, now=BASE + 54 * scoring.HOUR)
    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE + 54 * scoring.HOUR,
        app_session_id="reload",
    )
    assert again._servers[SERVER].prior_regimes[-1].period_seconds == 3 * scoring.HOUR


def test_stage6_tests_and_runtime_do_not_resolve_real_config_paths():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "COMPANION_RESTART_LEARNING_PHASE2_PATH" not in source
    assert "COMPANION_RESTART_LEARNING_PATH" not in source
    assert "companion_restart_learning.py" not in source
