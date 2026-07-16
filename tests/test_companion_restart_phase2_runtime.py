import hashlib
import json
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
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0), (90, 0), (200, 2), (210, 4)):
        poll(value, at, players=players)
    return value.tick(SERVER, wall_at=BASE + 240, monotonic_at=240)


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
    saved = json.loads(active.read_text(encoding="utf-8"))
    assert len(saved["servers"][SERVER]["events"]) == 1
    assert {item["event_id"] for item in saved["servers"][SERVER]["events"]} == {event.event_id}


def test_crowbar_visible_drain_routes_one_strong_event_without_outage(tmp_path):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    update = crowbar(value)
    assert len(update.finalized_events) == 1
    event = update.finalized_events[0]
    assert event.outcome is detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART
    assert detection.SignalSource.INFO_OUTAGE not in event.sources
    assert len(json.loads(active.read_text())["servers"][SERVER]["events"]) == 1


def test_false_timeout_is_uncertain_and_does_not_create_exact_period(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    for at in (0, 10, 20):
        poll(value, at, players=15)
    poll(value, 30, ok=False)
    poll(value, 40, players=15)
    update = value.tick(SERVER, wall_at=BASE + 70, monotonic_at=70)
    assert len(update.finalized_events) == 1
    assert update.finalized_events[0].outcome is detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION
    assert all(item.strict_direct_interval_count == 0 for item in value._servers[SERVER].score.candidates)


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
    for at, players in ((0, 12), (10, 12), (20, 12), (30, 0)):
        poll(value, at, players=players)
    update = value.end_monitoring(
        SERVER, marker=detection.LifecycleMarker.PAUSE, wall_at=BASE + 35, monotonic_at=35
    )
    assert update.finalized_events[0].outcome is detection.EventOutcome.AMBIGUOUS_DRAIN
    record = json.loads(active.read_text())["servers"][SERVER]
    assert record["active_episode"] is None
    assert record["incomplete_episodes"]
    assert record["monitoring_sessions"][-1]["reason"] == detection.LifecycleMarker.PAUSE.value


def test_poll_coverage_is_dense_and_large_callback_gap_is_explicit(tmp_path):
    value, *_ = make_runtime(tmp_path)
    begin(value)
    poll(value, 0, players=10)
    poll(value, 10, players=10)
    poll(value, 50, players=10)
    kinds = [item.kind for item in value._servers[SERVER].coverage]
    assert kinds == [runtime.CoverageKind.ONLINE_HEALTHY, runtime.CoverageKind.SLEEP_GAP]


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
    poll(value, 20, players=0)
    assert calls


def test_atomic_save_failure_disables_future_persistence_without_corrupting_previous(tmp_path, monkeypatch):
    value, active, _ = make_runtime(tmp_path)
    begin(value)
    before = active.read_bytes()
    monkeypatch.setattr(runtime, "atomic_write_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    poll(value, 0, players=12)
    poll(value, 10, players=0)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    assert active.read_bytes() == before


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
    assert value.scheduled_outage_relaxation_usable(
        SERVER, observed_at=BASE + 28 * scoring.HOUR
    )
    assert not value.scheduled_outage_relaxation_usable(
        SERVER, observed_at=BASE + 30 * scoring.HOUR
    )


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


def test_window_preserves_immediate_generic_recovery_and_records_suppression():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/dzll_launcher/window.py").read_text()
    live = source[source.index("def _apply_server_companion_live_result"):source.index("def _maybe_play_server_companion_restart_warning")]
    assert "_server_companion_alert_armed" in live
    assert "COMPANION_ALERT_REARM_OFFLINE_SECONDS" in live
    assert "_server_companion_alert_back_online" in live
    assert "provisional_event_id" in live
    assert "AlertKeyKind.GENERIC_RECOVERY" in live
    assert "mark_fired" in live


def test_phase2_integration_has_no_release_or_packaging_side_effects():
    root = Path(__file__).resolve().parents[1]
    runtime_source = (root / "src/dzll_launcher/companion_restart_phase2_runtime.py").read_text()
    assert "APP_VERSION" not in runtime_source
    assert "subprocess" not in runtime_source
    assert "import audio" not in runtime_source.lower()
    assert "_play_" not in runtime_source
