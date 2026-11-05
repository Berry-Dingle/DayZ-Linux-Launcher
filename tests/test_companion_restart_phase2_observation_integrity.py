"""Server Companion observation integrity, Stage 1 (F4 and F5).

Seeing a server in one state before an observation gap and another state
afterwards does not prove what happened during the gap.  These tests use
synthetic observations and isolated temporary state only.
"""

from __future__ import annotations

import json

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring


BASE = 2_100_000_000.0
SERVER = "integrity.example:2302"
SUSPEND = 8 * scoring.HOUR
PERIOD = 2 * scoring.HOUR
TIMEOUT = detection.InfoStatus.TIMEOUT
HEALTHY = detection.InfoStatus.HEALTHY
GAP_REASON = "outage_polling_continuity_broken"
ONSET_REASON = "outage_onset_unobserved"


# ---------------------------------------------------------------------------
# Engine-level helpers
# ---------------------------------------------------------------------------


def sample(at, players=None, *, info=HEALTHY, mono=None, monitor="monitor-1"):
    """One observation; ``mono`` defaults to ``at`` (no suspend)."""

    present = players is not None
    return detection.ObservationSample(
        wall_at=BASE + at,
        monotonic_at=float(at if mono is None else mono),
        app_session_id="app-1",
        monitoring_session_id=monitor,
        poll_generation=1,
        server_key=SERVER,
        info_status=info,
        player_status=(
            detection.FieldStatus.PRESENT if present else detection.FieldStatus.MISSING
        ),
        players=players,
        max_players=60,
    )


def feed(engine, observations):
    events = []
    for observation in observations:
        events.extend(engine.ingest(observation))
    return events


def healthy_preroll(start=0, stop=30, players=40):
    return [sample(at, players) for at in range(start, stop, 10)]


def failures(start, stop, step=3, *, mono_shift=0.0):
    return [
        sample(at, info=TIMEOUT, mono=at + mono_shift)
        for at in range(start, stop, step)
    ]


def fully_observed_restart(engine):
    events = feed(
        engine,
        [
            *healthy_preroll(),
            *failures(30, 90),
            sample(90, 5),
            sample(100, 9),
        ],
    )
    events.extend(engine.tick(130, BASE + 130))
    return events


# ---------------------------------------------------------------------------
# 1. Normal fully observed restart stays learning-eligible
# ---------------------------------------------------------------------------


def test_fully_observed_offline_restart_remains_learning_eligible():
    events = fully_observed_restart(detection.PhysicalEpisodeEngine())

    assert len(events) == 1
    event = events[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.coverage_complete
    assert event.lifecycle_interruption is None
    assert scoring.event_learning_eligible(event)
    assert event.canonical_phase_at == BASE + 30 == event.outage.first_failure_at
    assert GAP_REASON not in event.reason_codes
    assert ONSET_REASON not in event.reason_codes


def test_offline_polling_within_established_limit_is_not_interrupted():
    # The offline continuity limit is max(12, 4 * offline cadence) = 12 s.
    engine = detection.PhysicalEpisodeEngine()
    events = feed(
        engine,
        [
            *healthy_preroll(),
            sample(30, info=TIMEOUT),
            sample(33, info=TIMEOUT),
            sample(45, info=TIMEOUT),
            sample(57, 5),
        ],
    )
    events.extend(engine.tick(90, BASE + 90))

    assert len(events) == 1
    assert scoring.event_learning_eligible(events[0])
    assert events[0].outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART


# ---------------------------------------------------------------------------
# 2. Offline episode interrupted by a suspend-length gap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mono_advance",
    [3.0, SUSPEND],
    ids=["suspend-monotonic-frozen", "stalled-polling-monotonic-advances"],
)
def test_suspend_gap_inside_offline_episode_interrupts_without_restart(mono_advance):
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [*healthy_preroll(), *failures(30, 60)])
    assert engine.state is detection.EpisodeState.OFFLINE
    episode_id = engine.active_episode.event_id

    resumed_at = 57 + SUSPEND
    events = engine.ingest(sample(resumed_at, 7, mono=57 + mono_advance))

    assert len(events) == 1
    event = events[0]
    assert event.event_id == episode_id
    assert event.outcome is detection.EventOutcome.INCOMPLETE
    assert event.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    assert event.coverage_complete is False
    assert event.query_health.continuous is False
    assert GAP_REASON in event.reason_codes
    assert not scoring.event_learning_eligible(event)
    assert event.schedule_weight_suggestion == 0.0
    # The post-gap healthy sample starts a fresh sequence; nothing is open.
    assert engine.active_episode is None
    assert engine.state is detection.EpisodeState.IDLE
    assert engine.pre_roll and engine.pre_roll[-1].wall_at == BASE + resumed_at
    assert engine.tick(resumed_at + 120, BASE + resumed_at + 120) == ()


def test_offline_before_and_after_gap_requires_new_complete_sequence():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [*healthy_preroll(), *failures(30, 60)])

    # Still offline after resume: the pre-gap episode is interrupted, and the
    # post-gap outage has no observed onset.
    shift = 3.0 - SUSPEND
    resumed = 57 + SUSPEND
    events = feed(engine, failures(resumed, resumed + 30, mono_shift=shift))
    events.extend(feed(engine, [sample(resumed + 30, 6, mono=resumed + 30 + shift)]))
    events.extend(
        engine.tick(resumed + 70 + shift, BASE + resumed + 70)
    )

    assert [item.outcome for item in events] == [
        detection.EventOutcome.INCOMPLETE,
        detection.EventOutcome.CORROBORATED_OFFLINE_RESTART,
    ]
    interrupted, post_gap = events
    assert GAP_REASON in interrupted.reason_codes
    assert ONSET_REASON in post_gap.reason_codes
    assert not any(scoring.event_learning_eligible(item) for item in events)
    assert interrupted.event_id != post_gap.event_id

    # A later, fully observed restart is learnable again.
    later = resumed + 400
    events = feed(
        engine,
        [
            *[sample(at, 40, mono=at + shift) for at in range(resumed + 100, later, 10)],
            *failures(later, later + 30, mono_shift=shift),
            sample(later + 30, 5, mono=later + 30 + shift),
        ],
    )
    events.extend(engine.tick(later + 70 + shift, BASE + later + 70))
    assert len(events) == 1
    assert scoring.event_learning_eligible(events[0])


def test_pending_strike_never_confirms_offline_across_suspend():
    engine = detection.PhysicalEpisodeEngine()
    feed(engine, [*healthy_preroll(), sample(30, info=TIMEOUT)])
    assert engine.active_episode is None

    # Monotonic time froze during suspend, so the monotonic-only pending
    # lifetime would still consider strike one recent.
    engine.ingest(sample(30 + SUSPEND, info=TIMEOUT, mono=33))
    assert engine.active_episode is None
    assert engine.state is detection.EpisodeState.IDLE

    engine.ingest(sample(33 + SUSPEND, info=TIMEOUT, mono=36))
    episode = engine.active_episode
    assert episode is not None
    assert episode.first_failure_wall == BASE + 30 + SUSPEND
    assert episode.coverage_complete is False
    assert ONSET_REASON in episode.reason_codes


# ---------------------------------------------------------------------------
# 3 and 4. Unobserved outage onset
# ---------------------------------------------------------------------------


def test_outage_in_progress_when_monitoring_starts_keeps_class_but_is_ineligible():
    engine = detection.PhysicalEpisodeEngine()
    events = feed(engine, [*failures(0, 30), sample(30, 6), sample(40, 9)])
    events.extend(engine.tick(70, BASE + 70))

    assert len(events) == 1
    event = events[0]
    # Observable offline -> online classification is preserved.
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.outage.info_return_at == BASE + 30
    assert event.lifecycle_interruption is None
    assert event.coverage_complete is False
    assert ONSET_REASON in event.reason_codes
    assert not scoring.event_learning_eligible(event)


@pytest.mark.parametrize(
    "between",
    [
        # Protocol errors are not qualifying failures; the healthy reference
        # ages beyond the online limit before the first failure.
        [sample(at, info=detection.InfoStatus.ERROR) for at in range(30, 70, 10)],
        # No observations at all (idle suspend: wall advances, mono frozen).
        [],
    ],
    ids=["stale-through-protocol-errors", "stale-through-idle-gap"],
)
def test_outage_with_stale_healthy_observation_is_ineligible(between):
    engine = detection.PhysicalEpisodeEngine()
    first = 70 if between else 20 + SUSPEND
    mono_shift = 0.0 if between else 30.0 - first
    feed(engine, [*healthy_preroll(), *between])
    events = feed(engine, failures(first, first + 30, mono_shift=mono_shift))
    events.extend(feed(engine, [sample(first + 30, 6, mono=first + 30 + mono_shift)]))
    events.extend(engine.tick(first + 70 + mono_shift, BASE + first + 70))

    assert len(events) == 1
    event = events[0]
    assert event.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert event.coverage_complete is False
    assert ONSET_REASON in event.reason_codes
    assert not scoring.event_learning_eligible(event)


def test_onset_within_online_limit_after_healthy_is_observed():
    engine = detection.PhysicalEpisodeEngine()
    events = feed(
        engine,
        [sample(0, 40), *failures(30, 60), sample(60, 6)],
    )
    events.extend(engine.tick(100, BASE + 100))
    assert len(events) == 1
    assert ONSET_REASON not in events[0].reason_codes
    assert scoring.event_learning_eligible(events[0])


# ---------------------------------------------------------------------------
# 7. Query-visible behaviour is unchanged
# ---------------------------------------------------------------------------


def test_query_visible_restart_and_visible_gap_rule_are_unchanged():
    engine = detection.PhysicalEpisodeEngine()
    values = [12, 12, 12, 11, 9, 7, 5, 3, 0, 0, 0, 0, 0, 0, 0, 3, 6]
    times = [0, 10, 20, 40, 60, 80, 100, 120, 140, 150, 160, 170, 180, 190, 200, 210, 220]
    events = feed(engine, [sample(at, value) for at, value in zip(times, values)])
    assert len(events) == 1
    event = events[0]
    assert event.outcome in {
        detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
    }
    assert scoring.event_learning_eligible(event)
    assert GAP_REASON not in event.reason_codes
    assert ONSET_REASON not in event.reason_codes

    # A drain broken by a healthy-sample gap still follows the established
    # query-visible rule: no restart, standalone coverage invalidated.
    broken = detection.PhysicalEpisodeEngine()
    gap_events = feed(
        broken,
        [
            *[sample(at, value) for at, value in zip(times[:13], values[:13])],
            sample(400, 6),
            sample(410, 9),
        ],
    )
    assert gap_events == []
    assert broken.active_episode is None
    assert broken.provisional_drain is not None
    assert not broken.provisional_drain.standalone_coverage_complete


# ---------------------------------------------------------------------------
# 8 and 10. Event identity and duplicates
# ---------------------------------------------------------------------------


def test_interrupted_and_onset_events_have_deterministic_single_identity():
    def run():
        engine = detection.PhysicalEpisodeEngine()
        observations = [
            *healthy_preroll(),
            *failures(30, 60),
            sample(57 + SUSPEND, 7, mono=60),
        ]
        events = feed(engine, observations)
        # Replaying identical observations must not mint another event.
        events.extend(feed(engine, observations))
        events.extend(engine.tick(200, BASE + 57 + SUSPEND + 200))
        return events

    first, second = run(), run()
    assert len(first) == len(second) == 1
    assert first[0].event_id == second[0].event_id
    assert first[0].fingerprint == second[0].fingerprint


# ---------------------------------------------------------------------------
# Runtime-level helpers (isolated temporary schema-4 state)
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, value):
        self.value = value
        self.mono_shift = 0.0

    def poll(self, at, *, ok=True, players=40):
        info = {"ok": ok}
        if ok:
            info.update(players=players, max_players=60)
        else:
            info["err"] = "timeout"
        return self.value.ingest_live_result(
            SERVER,
            poll_generation=1,
            info=info,
            wall_at=BASE + at,
            monotonic_at=at + self.mono_shift,
        )

    def healthy(self, start, stop, players=40):
        for at in range(int(start), int(stop), 10):
            self.poll(at, players=players)

    def restart(self, at):
        """A fully observed offline restart starting at ``at``."""
        events = []
        for offset in range(0, 60, 3):
            events.extend(self.poll(at + offset, ok=False).finalized_events)
        for offset in (60, 70, 80, 90, 100):
            events.extend(self.poll(at + offset, players=6).finalized_events)
        return events


def _runtime(path, name="integrity", now=BASE):
    return runtime.Phase2RestartRuntime.initialize(
        active_path=path,
        legacy_path=path.parent / f"{name}-legacy.json",
        now=now,
        app_session_id=f"{name}-app",
        authoritative_schema4_runtime_enabled=True,
    )


def _persisted_record(path):
    state = schema4.deserialize_schema4_bytes(path.read_bytes()).state
    assert schema4.validate_schema4_state(state).valid
    return state["servers"][SERVER]


def _relationship_key(item):
    return (item["left_event_id"], item["right_event_id"], item["candidate_period_seconds"])


def _gap_scenario(tmp_path):
    path = tmp_path / "schema4.json"
    value = _runtime(path)
    assert value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1)
    clock = Clock(value)
    finalized = []

    clock.healthy(0, PERIOD)
    finalized += clock.restart(PERIOD)
    clock.healthy(PERIOD + 110, 2 * PERIOD)
    finalized += clock.restart(2 * PERIOD)
    clock.healthy(2 * PERIOD + 110, 3 * PERIOD)
    before_gap = _persisted_record(path)

    # Confirmed outage, then the machine suspends for eight hours.
    for offset in range(0, 30, 3):
        finalized += clock.poll(3 * PERIOD + offset, ok=False).finalized_events
    resumed = 3 * PERIOD + 27 + SUSPEND
    clock.mono_shift = -SUSPEND + 3.0
    finalized += clock.poll(resumed, players=6).finalized_events
    clock.healthy(resumed + 10, resumed + 120, players=6)
    after_gap = _persisted_record(path)

    # A fully observed restart after the gap, exactly 2 h later.
    clock.healthy(resumed + 120, resumed + PERIOD - 27, players=40)
    finalized += clock.restart(resumed + PERIOD - 27)
    clock.healthy(resumed + PERIOD + 83, resumed + PERIOD + 200)
    final = _persisted_record(path)
    return value, path, finalized, before_gap, after_gap, final


# ---------------------------------------------------------------------------
# 5, 6, 8, 9, 10. Runtime: evidence preserved, no cross-gap authority,
# schema-4 valid, persistence round-trip, no duplicate events.
# ---------------------------------------------------------------------------


def test_runtime_gap_preserves_prior_evidence_and_forms_no_cross_gap_authority(tmp_path):
    value, path, finalized, before_gap, after_gap, final = _gap_scenario(tmp_path)
    events = value._servers[SERVER].events

    # 10. Exactly one event per physical episode; no duplicates.
    assert len(finalized) == 4
    assert len({item.event_id for item in events}) == len(events) == 4
    assert len({item.fingerprint for item in events}) == 4
    first, second, interrupted, later = events
    assert [scoring.event_learning_eligible(item) for item in events] == [
        True,
        True,
        False,
        True,
    ]
    assert interrupted.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    assert interrupted.outcome is detection.EventOutcome.INCOMPLETE
    assert GAP_REASON in interrupted.reason_codes

    # 5. Evidence collected before the gap is unchanged.
    def by_id(record, name, field):
        return {item[field]: item for item in record[name]}

    before_events = by_id(before_gap, "physical_events", "event_id")
    after_events = by_id(after_gap, "physical_events", "event_id")
    for event_id in (first.event_id, second.event_id):
        assert after_events[event_id] == before_events[event_id]
    before_rel = {_relationship_key(item): item for item in before_gap["interval_relationships"]}
    after_rel = {_relationship_key(item): item for item in after_gap["interval_relationships"]}
    pre_gap_pair = (first.event_id, second.event_id, PERIOD)
    assert before_rel[pre_gap_pair]["high_authority_eligible"] is True
    assert after_rel[pre_gap_pair] == before_rel[pre_gap_pair]
    for key, item in before_rel.items():
        assert after_rel[key] == item

    # 6. No high-authority relationship is formed across the gap.
    relationships = final["interval_relationships"]
    assert any(
        _relationship_key(item) == pre_gap_pair and item["high_authority_eligible"]
        for item in relationships
    )
    for item in relationships:
        endpoints = {item["left_event_id"], item["right_event_id"]}
        crosses_gap = later.event_id in endpoints and bool(
            endpoints & {first.event_id, second.event_id}
        )
        if interrupted.event_id in endpoints or crosses_gap:
            assert item["high_authority_eligible"] is False, item
    assert not any(
        interrupted.event_id in {item["left_event_id"], item["right_event_id"]}
        for item in relationships
        if item["high_authority_eligible"]
    )
    value.shutdown(wall_at=BASE + 3 * PERIOD + SUSPEND + PERIOD + 300, monotonic_at=1e6)


def test_runtime_persistence_reload_preserves_interruption_and_onset_classification(tmp_path):
    path = tmp_path / "schema4.json"
    value = _runtime(path, "reload")
    assert value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1)
    clock = Clock(value)

    # Outage already in progress when monitoring starts.
    for offset in range(0, 30, 3):
        clock.poll(offset, ok=False)
    clock.healthy(30, 120, players=6)
    # Healthy, then outage interrupted by suspend.
    clock.healthy(120, 400)
    for offset in range(400, 430, 3):
        clock.poll(offset, ok=False)
    clock.mono_shift = -SUSPEND + 3.0
    clock.healthy(427 + SUSPEND, 427 + SUSPEND + 120, players=6)

    events = list(value._servers[SERVER].events)
    assert len(events) == 2
    onset, interrupted = events
    assert onset.outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert ONSET_REASON in onset.reason_codes and not onset.coverage_complete
    assert interrupted.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    value.shutdown(wall_at=BASE + 427 + SUSPEND + 200, monotonic_at=600)

    reloaded = _runtime(path, "reload-again", now=BASE + 427 + SUSPEND + 250)
    restored = {item.event_id: item for item in reloaded._servers[SERVER].events}
    assert set(restored) == {onset.event_id, interrupted.event_id}
    for original in events:
        again = restored[original.event_id]
        assert again.outcome is original.outcome
        assert again.coverage_complete is False
        assert again.lifecycle_interruption is original.lifecycle_interruption
        assert set(again.reason_codes) == set(original.reason_codes)
        assert again.canonical_phase_at == original.canonical_phase_at
        assert not scoring.event_learning_eligible(again)
    record = _persisted_record(path)
    persisted = {item["event_id"]: item for item in record["physical_events"]}
    assert persisted[interrupted.event_id]["lifecycle_interruption"] == (
        detection.LifecycleMarker.SLEEP_GAP.value
    )
    assert persisted[onset.event_id]["coverage_complete"] is False
    assert ONSET_REASON in persisted[onset.event_id]["reason_codes"]
    assert json.dumps(record, sort_keys=True)  # serialisable, schema-4 valid
    reloaded.shutdown(wall_at=BASE + 427 + SUSPEND + 300, monotonic_at=700)
