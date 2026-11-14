"""Server Companion: one-player offline-to-online classification (Option A).

A zero-player return never establishes "population substantially unchanged".
A restart is expected to return with zero players, so a zero return cannot show
that players stayed connected.  Previously the one-player tolerance floor made
a zero return "unchanged" whenever the positive pre-roll median was one, and a
fully observed outage became ``service_interruption_non_restart``.

All tests use the real detector; window/alert tests use the deterministic
connectivity harness (fake probes, injected clocks, temporary learning file).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher.companion_restart_phase2_runtime import (
    RecoveryAlertExpectedWindow,
    RecoveryAlertWindowStatus,
)

import test_companion_restart_phase2_query_visible as qv
import test_server_companion_connectivity_integrity as ci


Outcome = detection.EventOutcome
MISSING = object()
WALL = 1_790_000_000.0


def obs(
    at,
    players=MISSING,
    *,
    info=detection.InfoStatus.HEALTHY,
    queue=0,
    wall_base=WALL,
    mono_base=0.0,
    server="198.51.100.9:2302",
    app="app-1",
    monitor="monitor-1",
    generation=1,
    lifecycle=detection.LifecycleMarker.NORMAL,
    chain=None,
    provenance=0,
):
    healthy = info is detection.InfoStatus.HEALTHY
    present = players is not MISSING and healthy
    queue_present = healthy and queue is not MISSING and queue is not None
    return detection.ObservationSample(
        wall_at=wall_base + at,
        monotonic_at=mono_base + at,
        app_session_id=app,
        monitoring_session_id=monitor,
        poll_generation=generation,
        server_key=server,
        info_status=info,
        info_latency=0.04 if healthy else None,
        player_status=detection.FieldStatus.PRESENT if present else detection.FieldStatus.MISSING,
        players=players if present else None,
        max_players=60,
        queue_status=detection.FieldStatus.PRESENT if queue_present else detection.FieldStatus.MISSING,
        queue=queue if queue_present else None,
        lifecycle=lifecycle,
        continuity_chain_id=chain,
        provenance_version=provenance,
    )


def timeout(at, **kwargs):
    return obs(at, info=detection.InfoStatus.TIMEOUT, **kwargs)


def feed(engine, observations):
    events = []
    for item in observations:
        events.extend(engine.ingest(item))
    return events


def oo_sequence(
    pre_counts,
    return_players,
    *,
    outage_seconds=54,
    post_players=None,
    return_queue=0,
    onset_gap=10,
    **kwargs,
):
    """Pre-roll at 10 s cadence, two strikes 10 s apart, 3 s offline polls, a return.

    Returns ``(events, first_failure_offset)``.  ``post_players`` defaults to
    the return population and is observed for 40 s so the episode finalizes.
    """

    observations = [obs(index * 10, value, **kwargs) for index, value in enumerate(pre_counts)]
    last_healthy = (len(pre_counts) - 1) * 10
    first = last_healthy + onset_gap
    observations.append(timeout(first, **kwargs))
    observations.append(timeout(first + 10, **kwargs))
    at = first + 10
    while at + 3 <= first + outage_seconds:
        at += 3
        observations.append(timeout(at, **kwargs))
    back = at + 3
    observations.append(obs(back, return_players, queue=return_queue, **kwargs))
    post = return_players if post_players is None else post_players
    for step in (10, 20, 30, 40):
        observations.append(obs(back + step, post, **kwargs))
    engine = detection.PhysicalEpisodeEngine()
    return feed(engine, observations), first


def only_event(events):
    assert len(events) == 1, events
    return events[0]


# ---------------------------------------------------------------------------
# Required classification table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pre_counts", "return_players", "expected", "authenticity"),
    [
        ((0,) * 60, 0, Outcome.CONFIRMED_OFFLINE_RESTART, 0.85),
        ((1,) * 60, 0, Outcome.CONFIRMED_OFFLINE_RESTART, 0.85),
        ((2,) * 60, 0, Outcome.CONFIRMED_OFFLINE_RESTART, 0.85),
        ((1,) * 60, 1, Outcome.SERVICE_INTERRUPTION, 0.20),
        ((1,) * 60, 2, Outcome.SERVICE_INTERRUPTION, 0.20),
        ((12,) * 60, 12, Outcome.SERVICE_INTERRUPTION, 0.20),
        ((15,) * 60, 14, Outcome.SERVICE_INTERRUPTION, 0.20),
        ((30,) * 60, 27, Outcome.SERVICE_INTERRUPTION, 0.20),
    ],
    ids=["0-0", "1-0", "2-0", "1-1", "1-2", "12-12", "15-14", "30-27"],
)
def test_oo_classification_table(pre_counts, return_players, expected, authenticity):
    events, first = oo_sequence(pre_counts, return_players)
    event = only_event(events)
    assert event.outcome is expected
    assert event.authenticity == pytest.approx(authenticity)
    assert event.coverage_complete is True
    assert event.lifecycle_interruption is None
    assert scoring.event_learning_eligible(event)
    assert event.outage.first_failure_at == WALL + first
    assert event.outage.confirmed_offline_at == WALL + first + 10
    if expected is Outcome.CONFIRMED_OFFLINE_RESTART:
        assert event.schedule_weight_suggestion == pytest.approx(0.85)
        assert event.canonical_phase_at == WALL + first
        assert authority._authentic_event(event)
    else:
        assert event.schedule_weight_suggestion == 0.0
        assert not authority._authentic_event(event)


def test_player_leaving_two_minutes_before_observed_outage_is_confirmed():
    # One player for most of the ten-minute pre-roll, then zero for two
    # minutes immediately before the outage, zero on return.
    events, first = oo_sequence((1,) * 48 + (0,) * 12, 0)
    event = only_event(events)
    assert event.outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert event.authenticity == pytest.approx(0.85)
    assert scoring.event_learning_eligible(event)
    assert event.reason_codes == ("recovery_finalized",)
    assert event.canonical_phase_at == WALL + first


def test_zero_return_is_never_population_unchanged_but_positive_return_still_is():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [obs(index * 10, 1) for index in range(60)])
    assert engine._population_substantially_unchanged(obs(600, 0)) is False
    assert engine._population_substantially_unchanged(obs(600, 1)) is True
    assert engine._population_substantially_unchanged(obs(600, 2)) is True
    assert engine._population_substantially_unchanged(obs(600, 3)) is False
    assert engine._population_substantially_unchanged(obs(600, MISSING)) is False


@pytest.mark.parametrize(
    ("pre_counts", "return_players", "return_queue", "expected"),
    [
        ((12,) * 60, 3, 0, Outcome.CORROBORATED_OFFLINE_RESTART),  # changed population
        ((0,) * 60, 1, 0, Outcome.CORROBORATED_OFFLINE_RESTART),  # positive return, no baseline
        ((1,) * 60, 0, 2, Outcome.CORROBORATED_OFFLINE_RESTART),  # queue on return
        ((12,) * 60, 12, 3, Outcome.CORROBORATED_OFFLINE_RESTART),  # queue overrides veto
    ],
    ids=["changed-population", "zero-to-one", "queue-1-0", "queue-12-12"],
)
def test_existing_corroborated_restart_behaviour(pre_counts, return_players, return_queue, expected):
    events, _first = oo_sequence(pre_counts, return_players, return_queue=return_queue)
    event = only_event(events)
    assert event.outcome is expected
    assert event.authenticity >= 0.95
    assert scoring.event_learning_eligible(event)


def test_inherited_player_drain_corroborates_as_before():
    # Pre-restart kick 9 -> 7 -> 0 two minutes before the outage.
    events, _first = oo_sequence((9,) * 40 + (7,) * 4 + (0,) * 12, 0)
    event = only_event(events)
    assert event.outcome is Outcome.CORROBORATED_OFFLINE_RESTART
    assert "inherited_provisional_player_drain" in event.reason_codes
    assert scoring.event_learning_eligible(event)


@pytest.mark.parametrize("pre", [(1,) * 60, (12,) * 60], ids=["quiet", "populated"])
def test_missing_population_on_return_keeps_confirmed(pre):
    events, _first = oo_sequence(pre, MISSING, post_players=MISSING)
    event = only_event(events)
    assert event.outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert event.authenticity == pytest.approx(0.85)


@pytest.mark.parametrize("return_players", [0, 1])
def test_single_a2s_failure_produces_no_event(return_players):
    engine = detection.PhysicalEpisodeEngine()
    observations = [obs(index * 10, 1) for index in range(60)]
    observations += [timeout(600), obs(610, return_players), obs(620, return_players)]
    assert feed(engine, observations) == []
    assert engine.tick(700, WALL + 700) == ()
    assert engine.active_episode is None


def test_unobserved_onset_keeps_outcome_but_is_learning_ineligible():
    events, _first = oo_sequence((1,) * 60, 0, onset_gap=45)
    event = only_event(events)
    assert event.outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert event.coverage_complete is False
    assert "outage_onset_unobserved" in event.reason_codes
    assert not scoring.event_learning_eligible(event)
    assert not authority._authentic_event(event)
    assert not authority.sparse_usable_event(event)


def test_offline_polling_gap_is_incomplete_and_ineligible():
    engine = detection.PhysicalEpisodeEngine()
    observations = [obs(index * 10, 1) for index in range(60)]
    observations += [timeout(600), timeout(610), timeout(613), timeout(640), obs(643, 0), obs(653, 0)]
    events = feed(engine, observations)
    event = only_event(events)
    assert event.outcome is Outcome.INCOMPLETE
    assert event.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    assert "outage_polling_continuity_broken" in event.reason_codes
    assert not scoring.event_learning_eligible(event)


@pytest.mark.parametrize(
    "marker",
    [detection.LifecycleMarker.SHUTDOWN, detection.LifecycleMarker.SERVER_SWITCH],
)
def test_monitoring_interruption_mid_outage_is_ineligible(marker):
    engine = detection.PhysicalEpisodeEngine()
    observations = [obs(index * 10, 1) for index in range(60)]
    observations += [timeout(600), timeout(610), timeout(613), obs(616, MISSING, lifecycle=marker)]
    event = only_event(feed(engine, observations))
    assert event.outcome is Outcome.INCOMPLETE
    assert event.lifecycle_interruption is marker
    assert not scoring.event_learning_eligible(event)


# ---------------------------------------------------------------------------
# Dawn of the Bobs, 2026-11-13 01:00 UTC (observation sequence replay)
# ---------------------------------------------------------------------------

BOB_SERVER = "51.195.74.63:3058"
BOB_FIRST_FAILURE = 1794531609.9874573  # 01:00:09.987Z
BOB_MONO_FIRST_FAILURE = 49998.13


def bob_observations():
    """The retained 13 Nov sequence at its real wall and monotonic times.

    Retained samples (compacted to run endpoints): 1 player from 00:50:08.095
    to 00:57:58.114, 0 from 00:58:08.084 to 00:59:58.083, 16 timeouts from
    01:00:09.987 (second strike 01:00:19.989, last 01:01:03.988), return
    01:01:07.303 with 0 players, 0 at 01:01:28.114, finalized 01:01:38.161.
    Intermediate pre-roll samples are filled at the observed 10 s cadence.
    """

    base_wall = BOB_FIRST_FAILURE - BOB_MONO_FIRST_FAILURE
    common = dict(
        wall_base=base_wall,
        server=BOB_SERVER,
        app="8a4e2320-eed9-41ed-954d-ba0762d66274",
        monitor="2d2ec419-4668-4c22-92dd-3aed056384cb",
        chain="c0d79ab4c3d0053d4b9e4894d26129770eaaca6120c1c5aa4780d3ee7c40faec",
        provenance=1,
    )
    observations = []
    for index in range(48):  # 49396.24 .. 49866.24 at 10 s, one player
        observations.append(obs(49396.24 + index * 10, 1, **common))
    for index in range(12):  # 49876.23 .. 49986.23 at 10 s, zero players
        observations.append(obs(49876.23 + index * 10, 0, **common))
    failures = [49998.13, 50008.13]  # strike 1, strike 2 (01:00:19.989)
    step = (50052.13 - 50008.13) / 14
    failures += [50008.13 + step * index for index in range(1, 15)]
    assert len(failures) == 16 and failures[-1] == pytest.approx(50052.13)
    observations += [timeout(at, **common) for at in failures]
    observations.append(obs(50055.45, 0, **common))  # 01:01:07.303
    observations.append(obs(50066.26, 0, **common))
    observations.append(obs(50076.26, 0, **common))  # 01:01:28.114
    observations.append(obs(50086.30, 0, **common))  # 01:01:38.161 (finalize)
    return observations


def test_dawn_of_the_bobs_13_november_replay_is_a_confirmed_restart():
    engine = detection.PhysicalEpisodeEngine()
    event = only_event(feed(engine, bob_observations()))

    assert event.outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert event.authenticity == pytest.approx(0.85)
    assert event.schedule_weight_suggestion == pytest.approx(0.85)
    assert event.coverage_complete is True
    assert event.lifecycle_interruption is None
    assert scoring.event_learning_eligible(event)
    assert authority._authentic_event(event)
    assert authority.sparse_usable_event(event)
    assert continuity.relationship_endpoint_outcome_eligible(event)
    assert not scoring.event_interrupts_interval_observation(event)
    # Same physical facts as the retained event.
    assert event.server_key == BOB_SERVER
    assert event.outage.failure_count == 16
    assert set(event.outage.failure_statuses) == {detection.InfoStatus.TIMEOUT}
    assert event.outage.first_failure_at == pytest.approx(BOB_FIRST_FAILURE)
    assert event.outage.confirmed_offline_at == pytest.approx(1794531619.9874573)
    assert event.outage.info_return_at - event.outage.first_failure_at == pytest.approx(57.32)
    assert event.canonical_phase_at == pytest.approx(BOB_FIRST_FAILURE)
    assert event.phase_uncertainty == 10.0
    assert event.drain.baseline_players is None
    assert event.recovery.first_queue_at is None
    assert event.recovery.first_player_at is None
    assert event.query_health.failed_samples == 16
    assert event.reason_codes == ("recovery_finalized",)
    assert event.sources == frozenset(
        {detection.SignalSource.INFO_OUTAGE, detection.SignalSource.INFO_RETURN}
    )
    # Identity derives from the first failure only (unchanged by the fix).
    assert event.fingerprint == (
        "51.195.74.63:3058|2d2ec419-4668-4c22-92dd-3aed056384cb|1794531610|outage|49998.13"
    )
    assert event.event_id == "85301cc4-e657-5c13-ad6d-64a0a55da72f"


def test_dawn_of_the_bobs_replay_with_preserved_player_stays_a_service_interruption():
    observations = bob_observations()
    # Same sequence, but the lone player is still connected on return.
    observations = [
        replace(item, players=1) if item.info_status is detection.InfoStatus.HEALTHY
        and item.monotonic_at >= 49876.0 else item
        for item in observations
    ]
    event = only_event(feed(detection.PhysicalEpisodeEngine(), observations))
    assert event.outcome is Outcome.SERVICE_INTERRUPTION
    assert event.authenticity == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# QV classification is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("baseline", "first", "stable", "expected"),
    [
        (20, 3, 10, Outcome.STRONG_QUERY_VISIBLE_RESTART),
        (4, 2, 2, Outcome.PROBABLE_QUERY_VISIBLE_RESTART),
    ],
)
def test_query_visible_restart_classification_is_unchanged(baseline, first, stable, expected):
    counts = (baseline,) * 3 + (0,) * 6 + (first, stable)
    events = feed(
        detection.PhysicalEpisodeEngine(),
        [qv.sample(index * 10, value) for index, value in enumerate(counts)],
    )
    event = only_event(events)
    assert event.outcome is expected
    assert event.outage.failure_count == 0
    assert scoring.event_learning_eligible(event)


def test_query_visible_stepped_drain_restart_is_unchanged():
    event = qv.valid_visible_event()
    assert event.outcome is Outcome.STRONG_QUERY_VISIBLE_RESTART
    assert scoring.event_learning_eligible(event)


# ---------------------------------------------------------------------------
# Downstream sparse evidence and continuous interval eligibility
# ---------------------------------------------------------------------------


def harness_event(hours, sequence, *, return_players=0):
    """A real-detector one-player OO event placed on the interval fixture lattice."""

    at = ci.BASE + hours * ci.HOUR
    events, first = oo_sequence(
        (1,) * 48 + (0,) * 12,
        return_players,
        wall_base=at - 600 - 10,
        mono_base=0.0,
        server=ci.SERVER_KEY,
    )
    event = only_event(events)
    assert event.outage.first_failure_at == pytest.approx(at)
    chain = continuity.deterministic_continuity_chain_id(ci.SERVER_KEY, "app", "mon", 1, ci.BASE)
    return replace(
        event,
        event_id=f"one-player-{sequence}",
        fingerprint=f"one-player-fp-{sequence}",
        sequence=sequence,
        app_session_id="app",
        monitoring_session_id="mon",
        poll_generation=1,
        continuity_chain_id=chain,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def test_one_player_restarts_are_sparse_onsets_and_direct_interval_endpoints():
    left = harness_event(0, 1)
    right = harness_event(3, 2)
    for event in (left, right):
        assert event.outcome is Outcome.CONFIRMED_OFFLINE_RESTART
        assert authority.sparse_usable_event(event)
        assert continuity.relationship_endpoint_outcome_eligible(event)

    onsets, last = authority.sparse_occurrence_onsets((left, right))
    assert [item.event_id for item in onsets] == ["one-player-1", "one-player-2"]
    assert last == pytest.approx(right.canonical_phase_at)

    assert ci._scored((left, right), 3).candidate(3 * ci.HOUR).strict_direct_interval_count == 1
    relationships = continuity.extract_interval_relationships(
        (left, right), ci._spans(3), candidate_periods=(3 * ci.HOUR,)
    )
    direct = ci._direct(relationships, "one-player-1", "one-player-2", 3 * ci.HOUR)
    assert direct and direct[0].high_authority_eligible


def test_preserved_player_service_interruption_still_blocks_and_never_counts():
    middle = harness_event(1.5, 2, return_players=1)
    assert middle.outcome is Outcome.SERVICE_INTERRUPTION
    assert not authority.sparse_usable_event(middle)
    assert not continuity.relationship_endpoint_outcome_eligible(middle)
    assert scoring.event_interrupts_interval_observation(middle)

    events = (ci._event(0, 1), middle, ci._event(3, 3))
    assert ci._scored(events, 3).candidate(3 * ci.HOUR).strict_direct_interval_count == 0
    onsets, _last = authority.sparse_occurrence_onsets(events)
    assert "one-player-2" not in {item.event_id for item in onsets}


# ---------------------------------------------------------------------------
# Window: local connectivity, alerts and learning (real runtime and detector)
# ---------------------------------------------------------------------------


def quiet_pre_roll(h):
    h.healthy(480, players=1)
    h.healthy(120, players=0)


def test_one_player_oo_with_healthy_connectivity_alerts_and_learns(monkeypatch, tmp_path):
    h = ci.Harness(monkeypatch, tmp_path)
    quiet_pre_roll(h)
    candidate = h.outage(54, probe=ci.HEALTHY, return_info=ci.healthy_info(0))
    assert candidate is not None
    assert h.alerts == ["back online"]
    h.healthy(90, players=0)
    events = h.events()
    assert len(events) == 1
    assert events[0].outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert events[0].authenticity == pytest.approx(0.85)
    assert h.eligible() == events
    assert h.alerts == ["back online"]
    assert h.guard.incident is None


@pytest.mark.parametrize(
    "probe",
    [ci.LOCAL, ci.UNKNOWN, OSError("probe failed")],
    ids=["local-failure", "mixed", "error"],
)
def test_one_player_oo_with_contaminated_connectivity_never_learns_or_alerts(
    monkeypatch, tmp_path, probe
):
    h = ci.Harness(monkeypatch, tmp_path)
    quiet_pre_roll(h)
    session_before = h.runtime.active_monitoring_session_id(ci.SERVER_KEY)
    h.outage(54, probe=probe, return_info=ci.healthy_info(0))
    h.healthy(90, players=0)
    assert h.alerts == []
    assert h.eligible() == []
    assert not any(authority._authentic_event(item) for item in h.events())
    for event in h.events():
        assert event.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
        assert event.outcome is Outcome.INCOMPLETE
    assert h.runtime.active_monitoring_session_id(ci.SERVER_KEY) != session_before


def force_out_of_window_hold(monkeypatch, h):
    def expected(_key):
        episode = h.server.engine.active_episode
        return RecoveryAlertExpectedWindow(
            RecoveryAlertWindowStatus.OUTSIDE_WINDOW,
            event_id=str(episode.event_id) if episode is not None else None,
        )

    monkeypatch.setattr(h.runtime, "recovery_alert_expected_window", expected)


def test_out_of_window_held_alert_is_released_by_one_player_confirmed_restart(
    monkeypatch, tmp_path
):
    h = ci.Harness(monkeypatch, tmp_path)
    force_out_of_window_hold(monkeypatch, h)
    quiet_pre_roll(h)
    h.outage(54, probe=ci.HEALTHY, return_info=ci.healthy_info(0))
    pending = h.host._server_companion_pending_recovery_alert
    assert pending is not None and h.alerts == []
    h.healthy(40, players=0)  # finalization inside the 45 s hold
    assert h.events()[0].outcome is Outcome.CONFIRMED_OFFLINE_RESTART
    assert h.host._server_companion_pending_recovery_alert is None
    assert h.alerts == ["back online"]
    release = [item for item in h.scheduled if item[2] is pending][0][1]
    release(pending)
    h.healthy(60, players=0)
    assert h.alerts == ["back online"]  # no duplicate


def test_out_of_window_held_alert_is_suppressed_by_preserved_player_interruption(
    monkeypatch, tmp_path
):
    h = ci.Harness(monkeypatch, tmp_path)
    force_out_of_window_hold(monkeypatch, h)
    h.healthy(600, players=1)
    h.outage(54, probe=ci.HEALTHY, return_info=ci.healthy_info(1))
    pending = h.host._server_companion_pending_recovery_alert
    assert pending is not None and h.alerts == []
    h.healthy(40, players=1)
    assert h.events()[0].outcome is Outcome.SERVICE_INTERRUPTION
    assert h.host._server_companion_pending_recovery_alert is None
    release = [item for item in h.scheduled if item[2] is pending][0][1]
    release(pending)
    h.healthy(60, players=1)
    assert h.alerts == []
