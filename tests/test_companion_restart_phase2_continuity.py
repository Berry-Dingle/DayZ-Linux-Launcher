from __future__ import annotations

import inspect
import json
from dataclasses import asdict, replace

import pytest

from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 1_800_000_000.0
HOUR = 3600
SERVER = "server:2302"
APP = "app-1"
MONITOR = "monitor-1"
POLL = 7


def chain_id(
    *,
    server=SERVER,
    app=APP,
    monitor=MONITOR,
    poll=POLL,
    started=BASE,
):
    return continuity.deterministic_continuity_chain_id(
        server, app, monitor, poll, started
    )


def event(
    hour,
    sequence,
    *,
    outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    server=SERVER,
    app=APP,
    monitor=MONITOR,
    poll=POLL,
    chain=None,
    provenance=continuity.CONTINUITY_PROVENANCE_VERSION,
    authenticity=None,
):
    at = BASE + hour * HOUR
    if authenticity is None:
        authenticity = {
            detection.EventOutcome.CORROBORATED_OFFLINE_RESTART: 0.97,
            detection.EventOutcome.CONFIRMED_OFFLINE_RESTART: 0.85,
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART: 0.90,
            detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.60,
            detection.EventOutcome.SERVICE_INTERRUPTION: 0.20,
        }.get(outcome, 0.10)
    direct_outage = outcome in {
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
        detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
    }
    return detection.PhysicalRestartEvent(
        event_id=f"event-{sequence}-{hour}",
        sequence=sequence,
        fingerprint=f"fingerprint-{sequence}-{hour}",
        server_key=server,
        episode_started_at=at - 180,
        finalized_at=at + 30,
        canonical_phase_at=at,
        phase_uncertainty=3,
        sources=frozenset(
            {
                detection.SignalSource.INFO_OUTAGE,
                detection.SignalSource.INFO_RETURN,
            }
        ),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(
            12, at - 190, at - 150, at - 140, True, 0, 1.0, 3, 120, False
        ),
        outage=detection.OutageSummary(
            at - 80 if direct_outage else None,
            at - 70 if direct_outage else None,
            10 if direct_outage else 0,
            (detection.InfoStatus.TIMEOUT,) if direct_outage else (),
            at if direct_outage else None,
        ),
        recovery=detection.RecoverySummary(
            None,
            None,
            at if direct_outage else None,
            0,
            False,
        ),
        query_health=detection.QueryHealthSummary(4, 10, 10, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=("continuity_fixture",),
        app_session_id=app if provenance else None,
        monitoring_session_id=monitor if provenance else None,
        poll_generation=poll if provenance else None,
        continuity_chain_id=(chain or chain_id(server=server, app=app, monitor=monitor, poll=poll))
        if provenance
        else None,
        provenance_version=provenance,
    )


def span(
    start_hour,
    end_hour,
    *,
    kind=continuity.ContinuitySpanKind.ONLINE_HEALTHY,
    reference=None,
    server=SERVER,
    app=APP,
    monitor=MONITOR,
    poll=POLL,
    chain=None,
    cadence=10.0,
    provenance=continuity.CONTINUITY_PROVENANCE_VERSION,
    monotonic_offset=0.0,
    reasons=(),
):
    start = BASE + start_hour * HOUR
    end = BASE + end_hour * HOUR
    return continuity.ContinuitySpan(
        provenance_version=provenance,
        reference_id=reference or f"span-{start_hour}-{end_hour}-{kind.value}",
        server_key=server,
        app_session_id=app,
        monitoring_session_id=monitor,
        poll_generation=poll,
        continuity_chain_id=chain or chain_id(
            server=server, app=app, monitor=monitor, poll=poll
        ),
        start_at=start,
        end_at=end,
        start_monotonic=start_hour * HOUR + monotonic_offset,
        end_monotonic=end_hour * HOUR + monotonic_offset,
        kind=kind,
        cadence_seconds=cadence,
        reason_codes=tuple(reasons),
    )


def scenario(hours, *, candidate_periods=(3 * HOUR,), **event_kwargs):
    events = tuple(
        event(hour, index + 1, **event_kwargs)
        for index, hour in enumerate(hours)
    )
    spans = (span(min(hours), max(hours)),)
    relationships = continuity.extract_interval_relationships(
        events, spans, candidate_periods=candidate_periods
    )
    chains = continuity.build_continuity_chains(spans)
    streaks = continuity.build_cadence_streaks(relationships, chains=chains)
    return events, spans, relationships, streaks


def encoded(values):
    return json.dumps(
        [asdict(item) for item in values],
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.value,
    ).encode()


@pytest.mark.parametrize(
    ("hours", "direct_count", "streak_intervals"),
    [
        ((12, 15), 1, ()),
        ((12, 15, 18), 2, (2,)),
        ((12, 15, 18, 21), 3, (3,)),
    ],
)
def test_unknown_server_clean_streak_contract(hours, direct_count, streak_intervals):
    _events, _spans, relationships, streaks = scenario(hours)
    direct = [
        item
        for item in relationships
        if item.relationship_kind is continuity.RelationshipKind.DIRECT
    ]
    assert len(direct) == direct_count
    assert all(item.high_authority_eligible for item in direct)
    assert tuple(item.interval_count for item in streaks) == streak_intervals
    assert all("high_authority_eligible" in item.reason_codes for item in direct)


@pytest.mark.parametrize("incumbent", [None, 3 * HOUR, 4 * HOUR])
def test_clean_four_hour_challenger_is_byte_equivalent_for_every_incumbent(incumbent):
    events, spans, baseline, _streaks = scenario(
        (0, 4, 8, 12), candidate_periods=(4 * HOUR,)
    )
    # Incumbent is deliberately fixture context only: the extractor cannot accept it.
    fixture_context = {"incumbent": incumbent}
    assert fixture_context["incumbent"] in {None, 3 * HOUR, 4 * HOUR}
    result = continuity.extract_interval_relationships(
        events, spans, candidate_periods=(4 * HOUR,)
    )
    assert encoded(result) == encoded(baseline)
    assert len(continuity.build_cadence_streaks(result)) == 1


def test_clean_three_hour_challenger_is_independent_of_four_hour_incumbent():
    events, spans, baseline, _streaks = scenario((0, 3, 6, 9))
    result = continuity.extract_interval_relationships(
        events, spans, candidate_periods=(3 * HOUR,)
    )
    assert encoded(result) == encoded(baseline)
    assert all(item.candidate_period_seconds == 3 * HOUR for item in result)


def test_extractor_api_has_no_incumbent_regime_prediction_or_confidence_gate():
    parameters = inspect.signature(
        continuity.extract_interval_relationships
    ).parameters
    forbidden = {
        "incumbent_period",
        "incumbent_period_seconds",
        "selected_period",
        "selected_period_seconds",
        "predicted_phase",
        "phase_offset",
        "confidence",
        "regime_boundary",
        "regime_boundaries",
    }
    assert forbidden.isdisjoint(parameters)


def test_physical_event_extraction_api_has_no_incumbent_or_prediction_input():
    signature = inspect.signature(detection.PhysicalEpisodeEngine.ingest)
    assert tuple(signature.parameters) == ("self", "sample")


def test_regime_boundary_cannot_reject_raw_skip_over_relationship():
    events = (event(0, 1), event(2, 2), event(3, 3))
    relationships = continuity.extract_interval_relationships(
        events, (span(0, 3),), candidate_periods=(3 * HOUR,)
    )
    outer = next(
        item
        for item in relationships
        if item.left_event_id == events[0].event_id
        and item.right_event_id == events[2].event_id
    )
    assert outer.relationship_kind is continuity.RelationshipKind.SKIP_OVER
    assert "high_ineligible_intervening_authentic_restart" in outer.reason_codes
    assert "crosses_regime_boundary" not in outer.reason_codes


def test_disconnected_monitoring_sessions_keep_normal_relationships_but_no_streak():
    first = event(0, 1, monitor="monitor-a")
    second = event(3, 2, monitor="monitor-b")
    third = event(6, 3, monitor="monitor-c")
    spans = (
        span(0, 3, monitor="monitor-a"),
        span(3, 6, monitor="monitor-c"),
    )
    relationships = continuity.extract_interval_relationships(
        (first, second, third), spans, candidate_periods=(3 * HOUR,)
    )
    direct = [
        item
        for item in relationships
        if item.relationship_kind is continuity.RelationshipKind.DIRECT
    ]
    assert len(direct) == 2
    assert all(item.normal_support_diagnostic > 0 for item in direct)
    assert all(not item.high_authority_eligible for item in direct)
    assert all("high_ineligible_continuity_identity" in item.reason_codes for item in direct)
    assert continuity.build_cadence_streaks(relationships) == ()


@pytest.mark.parametrize(
    ("break_kind", "expected_reason"),
    [
        (continuity.ContinuitySpanKind.PAUSE, "pause"),
        (continuity.ContinuitySpanKind.CLEAR, "clear"),
        (continuity.ContinuitySpanKind.SHUTDOWN, "shutdown"),
        (continuity.ContinuitySpanKind.SERVER_SWITCH, "server_switch"),
        (continuity.ContinuitySpanKind.MONITOR_REPLACEMENT, "monitor_replacement"),
        (continuity.ContinuitySpanKind.STALE_POLL_REJECTION, "stale_poll_rejection"),
        (continuity.ContinuitySpanKind.SLEEP_GAP, "sleep_like_gap"),
        (continuity.ContinuitySpanKind.QUERY_HEALTH_GAP, "query_health_gap"),
        (continuity.ContinuitySpanKind.MISSING_FIELD_GAP, "missing_field_gap"),
    ],
)
def test_explicit_monitoring_breaks_terminate_chain(break_kind, expected_reason):
    marker = span(1, 1, kind=break_kind, reference=f"marker-{break_kind.value}")
    chains = continuity.build_continuity_chains(
        (span(0, 1), marker, span(1, 2, reference="after"))
    )
    assert len(chains) == 2
    assert chains[0].termination_reason == expected_reason
    assert chains[0].lifecycle is continuity.ChainLifecycle.HISTORICAL


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"server": "other:2302"}, "canonical_server_changed"),
        ({"app": "app-2"}, "app_session_changed"),
        ({"monitor": "monitor-2"}, "monitoring_session_changed"),
        ({"poll": 8}, "poll_generation_changed"),
    ],
)
def test_identity_changes_terminate_chain(change, expected):
    chains = continuity.build_continuity_chains(
        (span(0, 1), span(1, 2, reference="changed", **change))
    )
    assert len(chains) == 2
    assert chains[0].termination_reason == expected


def test_wall_or_monotonic_reversal_terminates_chain():
    reversed_span = continuity.ContinuitySpan(
        **{
            **asdict(span(1, 2, reference="reversed")),
            "start_at": BASE - 1,
            "end_at": BASE + 1,
            "start_monotonic": -1,
            "end_monotonic": 1,
        }
    )
    chains = continuity.build_continuity_chains((span(0, 1), reversed_span))
    assert chains[0].termination_reason == "clock_reversal"


@pytest.mark.parametrize(
    "ui_kind",
    [
        continuity.ContinuitySpanKind.UI_ROW_RECYCLED,
        continuity.ContinuitySpanKind.UI_SORTED,
        continuity.ContinuitySpanKind.UI_FILTERED,
        continuity.ContinuitySpanKind.UI_DOCKED,
        continuity.ContinuitySpanKind.UI_UNDOCKED,
        continuity.ContinuitySpanKind.UI_WIDGET_REPLACED,
    ],
)
def test_ui_only_lifecycle_does_not_terminate_chain(ui_kind):
    marker = span(1, 1, kind=ui_kind, reference=f"ui-{ui_kind.value}")
    chains = continuity.build_continuity_chains(
        (span(0, 1), marker, span(1, 2, reference="after-ui"))
    )
    assert len(chains) == 1
    assert chains[0].reference_ids == (
        "span-0-1-online_healthy",
        "after-ui",
    )
    assert "ui_lifecycle_ignored" in chains[0].reason_codes


def test_valid_outage_transition_edges_are_continuity_qualified():
    left = event(0, 1)
    right = event(3, 2)
    spans = (
        span(0, 2.97, reference="online-before"),
        span(
            2.97,
            2.976,
            kind=continuity.ContinuitySpanKind.HEALTHY_TO_FAILURE,
            reference="healthy-failure",
            cadence=10,
        ),
        span(
            2.976,
            2.999,
            kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
            reference="offline",
            cadence=3,
        ),
        span(
            2.999,
            3,
            kind=continuity.ContinuitySpanKind.FAILURE_TO_HEALTHY,
            reference="failure-healthy",
            cadence=3,
        ),
    )
    relationship = continuity.extract_interval_relationships(
        (left, right), spans, candidate_periods=(3 * HOUR,)
    )[0]
    assessment = continuity.assess_interval_continuity(
        start_at=left.canonical_phase_at,
        end_at=right.canonical_phase_at,
        server_key=SERVER,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=chain_id(),
        spans=spans,
    )
    assert assessment.continuity_qualified
    assert assessment.largest_unexplained_gap == 0
    assert assessment.transition_edge_durations == pytest.approx((21.6, 3.6))
    assert "observed_transition_edge_accepted" in assessment.reason_codes
    assert relationship.high_authority_eligible


def test_short_healthy_gap_within_active_poll_cadence_is_not_severe():
    gap = 20 / HOUR
    spans = (
        span(0, 1, reference="healthy-before", cadence=10),
        span(1 + gap, 3, reference="healthy-after", cadence=10),
    )
    assessment = continuity.assess_interval_continuity(
        start_at=BASE,
        end_at=BASE + 3 * HOUR,
        server_key=SERVER,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=chain_id(),
        spans=spans,
    )
    assert assessment.continuity_qualified
    assert assessment.largest_unexplained_gap == 0
    assert "poll_gap_within_active_cadence" in assessment.reason_codes


def test_offline_adjacent_gap_still_needs_an_explicit_transition_edge():
    gap = 20 / HOUR
    spans = (
        span(0, 2, reference="healthy-before", cadence=10),
        span(
            2 + gap,
            3,
            kind=continuity.ContinuitySpanKind.OFFLINE_OBSERVED,
            reference="offline-after-gap",
            cadence=3,
        ),
    )
    assessment = continuity.assess_interval_continuity(
        start_at=BASE,
        end_at=BASE + 3 * HOUR,
        server_key=SERVER,
        app_session_id=APP,
        monitoring_session_id=MONITOR,
        poll_generation=POLL,
        continuity_chain_id=chain_id(),
        spans=spans,
    )
    assert not assessment.continuity_qualified
    assert assessment.largest_unexplained_gap == pytest.approx(20)
    assert "unexplained_polling_gap" in assessment.reason_codes


@pytest.mark.parametrize(
    "blocker",
    [
        continuity.ContinuitySpanKind.QUERY_HEALTH_GAP,
        continuity.ContinuitySpanKind.MISSING_FIELD_GAP,
        continuity.ContinuitySpanKind.SLEEP_GAP,
    ],
)
def test_severe_health_or_coverage_gap_blocks_high_but_keeps_normal(blocker):
    spans = (
        span(0, 1.4),
        span(1.4, 1.6, kind=blocker, reference=f"block-{blocker.value}"),
        span(1.6, 3, reference="after-block"),
    )
    relationship = continuity.extract_interval_relationships(
        (event(0, 1), event(3, 2)), spans, candidate_periods=(3 * HOUR,)
    )[0]
    assert not relationship.high_authority_eligible
    assert relationship.normal_support_diagnostic > 0
    assert "high_ineligible_coverage" in relationship.reason_codes


def test_intervening_authentic_restart_prevents_outer_direct_high_relationship():
    events = (event(0, 1), event(1, 2), event(3, 3))
    relationships = continuity.extract_interval_relationships(
        events, (span(0, 3),), candidate_periods=(3 * HOUR,)
    )
    outer = next(
        item
        for item in relationships
        if item.left_event_id == events[0].event_id
        and item.right_event_id == events[2].event_id
    )
    assert outer.relationship_kind is continuity.RelationshipKind.SKIP_OVER
    assert outer.intervening_event_ids == (events[1].event_id,)
    assert not outer.high_authority_eligible
    assert continuity.build_cadence_streaks(relationships) == ()


def test_off_phase_outlier_closes_but_does_not_erase_completed_streak():
    events = tuple(event(value, index + 1) for index, value in enumerate((0, 3, 6, 7, 9, 12)))
    relationships = continuity.extract_interval_relationships(
        events, (span(0, 12),), candidate_periods=(3 * HOUR,)
    )
    streaks = continuity.build_cadence_streaks(relationships)
    assert len(streaks) == 1
    assert streaks[0].event_ids == tuple(item.event_id for item in events[:3])
    assert streaks[0].interval_count == 2
    assert "maximal_continuity_streak" in streaks[0].reason_codes


def test_replay_has_identical_ids_and_no_duplicate_relationships_or_streaks():
    events, spans, relationships, streaks = scenario((0, 3, 6, 9))
    replay_relationships = continuity.extract_interval_relationships(
        (*events, *events), (*spans, *spans), candidate_periods=(3 * HOUR,)
    )
    replay_streaks = continuity.build_cadence_streaks(
        (*relationships, *relationships)
    )
    assert encoded(replay_relationships) == encoded(relationships)
    assert encoded(replay_streaks) == encoded(streaks)
    assert len({item.relationship_id for item in replay_relationships}) == len(
        replay_relationships
    )


def test_harmonic_candidate_interpretations_are_independent_and_unreserved():
    events = (event(0, 1), event(6, 2))
    relationships = continuity.extract_interval_relationships(
        events,
        (span(0, 6),),
        candidate_periods=(2 * HOUR, 3 * HOUR, 6 * HOUR),
    )
    by_period = {item.candidate_period_seconds: item for item in relationships}
    assert set(by_period) == {2 * HOUR, 3 * HOUR, 6 * HOUR}
    assert by_period[2 * HOUR].multiplier == 3
    assert by_period[3 * HOUR].multiplier == 2
    assert by_period[6 * HOUR].relationship_kind is continuity.RelationshipKind.DIRECT
    assert by_period[6 * HOUR].high_authority_eligible
    assert not by_period[2 * HOUR].high_authority_eligible
    assert len({item.relationship_id for item in relationships}) == 3


def test_strong_query_visible_event_remains_normal_only():
    relationships = continuity.extract_interval_relationships(
        (
            event(0, 1, outcome=detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
            event(3, 2, outcome=detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART),
        ),
        (span(0, 3),),
        candidate_periods=(3 * HOUR,),
    )
    assert relationships[0].normal_support_diagnostic > 0
    assert not relationships[0].high_authority_eligible
    assert "high_ineligible_endpoint_class" in relationships[0].reason_codes


def test_missing_schema_three_provenance_is_normal_and_unproven():
    relationship = continuity.extract_interval_relationships(
        (event(0, 1, provenance=0), event(3, 2, provenance=0)),
        (),
        candidate_periods=(3 * HOUR,),
    )[0]
    assert relationship.coverage_quality is continuity.CoverageQuality.UNPROVEN
    assert relationship.normal_support_diagnostic > 0
    assert not relationship.high_authority_eligible
    assert "historical_continuity_unproven" in relationship.reason_codes


def test_persisted_sample_or_v1_fingerprint_reconstruction_is_conservative():
    monitor = "legacy-monitor"
    session = {
        "session_id": monitor,
        "app_session_id": "legacy-app",
        "poll_generation": 3,
        "started_at": BASE - HOUR,
        "ended_at": BASE + 4 * HOUR,
    }
    legacy = event(0, 1, provenance=0)
    legacy = replace(
        legacy,
        fingerprint=f"{SERVER}|{monitor}|{int(BASE)}|outage|1.0",
    )
    proven = continuity.reconstruct_historical_event_provenance(legacy, (session,))
    unproven = continuity.reconstruct_historical_event_provenance(
        replace(legacy, fingerprint="unknown-format"),
        (session,),
    )
    assert proven.proven
    assert proven.event.continuity_chain_id
    assert "provenance_from_v1_fingerprint_and_session" in proven.reason_codes
    assert not unproven.proven
    assert unproven.reason_codes == ("historical_continuity_unproven",)


def test_shadow_extraction_does_not_mutate_or_change_current_scorer_value():
    physical = tuple(event(value, index + 1) for index, value in enumerate((0, 3, 6, 9)))
    timeline = scoring.CoverageTimeline(
        (
            scoring.CoverageSegment(
                BASE,
                BASE + 9 * HOUR,
                scoring.CoverageKind.ONLINE_HEALTHY,
            ),
        )
    )
    scorer = scoring.RestartScheduleScorer()
    before = scorer.score(physical, timeline, now=BASE + 9 * HOUR)
    continuity.extract_interval_relationships(
        physical, (span(0, 9),), candidate_periods=(3 * HOUR,)
    )
    after = scorer.score(physical, timeline, now=BASE + 9 * HOUR)
    assert asdict(after) == asdict(before)


def test_provenance_objects_are_immutable_and_ids_are_deterministic():
    first = chain_id()
    second = chain_id()
    assert first == second
    relationship = scenario((0, 3))[2][0]
    with pytest.raises(Exception):
        relationship.relationship_id = "changed"


def test_runtime_additive_provenance_rolls_chain_on_sleep_without_shadow(tmp_path):
    active = tmp_path / "active.json"
    legacy = tmp_path / "legacy.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE,
        app_session_id=APP,
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    value.begin_monitoring(
        SERVER,
        wall_at=BASE,
        monotonic_at=0,
        poll_generation=POLL,
    )
    initial_chain = value._servers[SERVER].continuity_chain_id
    assert value.continuity_shadow_snapshot(SERVER) is None
    value.ingest_live_result(
        SERVER,
        poll_generation=POLL,
        info={"ok": True, "players": 10, "max_players": 60},
        wall_at=BASE,
        monotonic_at=0,
    )
    value.ingest_live_result(
        SERVER,
        poll_generation=POLL,
        info={"ok": True, "players": 10, "max_players": 60},
        wall_at=BASE + 40,
        monotonic_at=40,
    )
    rolled_chain = value._servers[SERVER].continuity_chain_id
    assert rolled_chain != initial_chain
    assert value._servers[SERVER].coverage[-1].kind is scoring.CoverageKind.SLEEP_GAP
    assert value._servers[SERVER].coverage[-1].continuity_chain_id == rolled_chain
    value.shutdown(wall_at=BASE + 50, monotonic_at=50)
    persisted = json.loads(active.read_text(encoding="utf-8"))
    stored_session = persisted["servers"][SERVER]["monitoring_sessions"][-1]
    assert stored_session["provenance_version"] == 1
    assert stored_session["continuity_chain_ids"] == [initial_chain, rolled_chain]


def test_optional_shadow_builds_diagnostics_but_has_no_policy_output(tmp_path):
    active = tmp_path / "active.json"
    legacy = tmp_path / "legacy.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE,
        app_session_id=APP,
        generation_id="11111111-1111-4111-8111-111111111111",
        continuity_shadow_enabled=True,
    )
    value.begin_monitoring(
        SERVER,
        wall_at=BASE,
        monotonic_at=0,
        poll_generation=POLL,
    )
    before = value.compatibility(SERVER, now=BASE)
    for offset in (0, 10):
        value.ingest_live_result(
            SERVER,
            poll_generation=POLL,
            info={"ok": True, "players": 10, "max_players": 60},
            wall_at=BASE + offset,
            monotonic_at=offset,
        )
    snapshot = value.continuity_shadow_snapshot(SERVER)
    after = value.compatibility(SERVER, now=BASE)
    assert snapshot is not None
    assert len(snapshot.chains) == 1
    assert snapshot.relationships == ()
    assert snapshot.streaks == ()
    assert asdict(after) == asdict(before)


def test_new_physical_event_persists_explicit_continuity_provenance(tmp_path):
    active = tmp_path / "active.json"
    legacy = tmp_path / "legacy.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=legacy,
        now=BASE,
        app_session_id=APP,
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    monitor_id = value.begin_monitoring(
        SERVER,
        wall_at=BASE,
        monotonic_at=0,
        poll_generation=POLL,
    )
    expected_chain = value._servers[SERVER].continuity_chain_id

    def poll(offset, *, ok=True, players=12):
        info = {"ok": ok}
        if ok:
            info.update(players=players, max_players=60)
        else:
            info["err"] = "timeout"
        value.ingest_live_result(
            SERVER,
            poll_generation=POLL,
            info=info,
            wall_at=BASE + offset,
            monotonic_at=offset,
        )

    for offset, players in ((0, 12), (10, 12), (20, 12), (30, 0), (60, 0)):
        poll(offset, players=players)
    poll(90, ok=False)
    poll(100, ok=False)
    poll(103, players=0)
    update = value.tick(SERVER, wall_at=BASE + 133, monotonic_at=133)
    assert len(update.finalized_events) == 1
    finalized = update.finalized_events[0]
    assert finalized.app_session_id == APP
    assert finalized.monitoring_session_id == monitor_id
    assert finalized.poll_generation == POLL
    assert finalized.continuity_chain_id == expected_chain
    assert finalized.provenance_version == 1
    stored = json.loads(active.read_text(encoding="utf-8"))["servers"][SERVER]
    persisted = stored["events"][-1]
    assert persisted["app_session_id"] == APP
    assert persisted["monitoring_session_id"] == monitor_id
    assert persisted["poll_generation"] == POLL
    assert persisted["continuity_chain_id"] == expected_chain
    assert persisted["provenance_version"] == 1
