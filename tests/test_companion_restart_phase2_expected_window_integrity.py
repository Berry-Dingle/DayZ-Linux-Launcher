"""Server Companion observation integrity, Stage 2 (F6).

An unobservable restart window is not evidence that a restart was missed.
These tests use synthetic observations and isolated temporary state only.
"""

from __future__ import annotations

import pytest

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_expected_windows as windows
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 2_300_000_000.0
HOUR = scoring.HOUR
SERVER = "windows.example:2302"
PERIOD3 = 3 * HOUR
TOL3 = scoring.candidate_phase_tolerance(PERIOD3)
EXPECTED = BASE + 15 * HOUR
WINDOW_START = EXPECTED - TOL3
WINDOW_END = EXPECTED + TOL3
Outcome = detection.EventOutcome


# ---------------------------------------------------------------------------
# Synthetic events
# ---------------------------------------------------------------------------


def restart(hours, sequence, outcome=Outcome.CORROBORATED_OFFLINE_RESTART):
    at = BASE + hours * HOUR
    return outage(
        sequence,
        at,
        at + 60,
        outcome=outcome,
        authenticity=0.98,
    )


def outage(
    sequence,
    start,
    end,
    *,
    outcome=Outcome.CORROBORATED_OFFLINE_RESTART,
    canonical=None,
    authenticity=None,
    coverage_complete=True,
    lifecycle=None,
    reasons=(),
):
    """An outage observed from ``start`` (first failure) to ``end`` (return)."""

    if authenticity is None:
        authenticity = {
            Outcome.CORROBORATED_OFFLINE_RESTART: 0.98,
            Outcome.CONFIRMED_OFFLINE_RESTART: 0.85,
            Outcome.SERVICE_INTERRUPTION: 0.20,
            Outcome.INCOMPLETE: 0.10,
        }.get(outcome, 0.10)
    return detection.PhysicalRestartEvent(
        event_id=f"window-event-{sequence}",
        sequence=sequence,
        fingerprint=f"window-fingerprint-{sequence}",
        server_key=SERVER,
        episode_started_at=start,
        finalized_at=end,
        canonical_phase_at=start if canonical is None else canonical,
        phase_uncertainty=10,
        sources=frozenset(
            {detection.SignalSource.INFO_OUTAGE, detection.SignalSource.INFO_RETURN}
        ),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(None, None, None, None, False, None, None, 0, 0, False),
        outage=detection.OutageSummary(start, start + 3, 2, (), end),
        recovery=detection.RecoverySummary(None, None, end, 0, False),
        query_health=detection.QueryHealthSummary(4, 2, 0, coverage_complete),
        coverage_complete=coverage_complete,
        lifecycle_interruption=lifecycle,
        samples=(),
        reason_codes=tuple(reasons),
    )


def healthy(start, end):
    return scoring.CoverageSegment(start, end, scoring.CoverageKind.ONLINE_HEALTHY, 10)


# ---------------------------------------------------------------------------
# Schema-3 runtime window evaluation (direct, isolated rules)
# ---------------------------------------------------------------------------


def _learned_server(tmp_path, *, coverage=None):
    """A runtime with a learned 3 h cycle at 0..12 h and an open 15 h window."""

    value = runtime.Phase2RestartRuntime.initialize(
        active_path=tmp_path / storage.PHASE2_ACTIVE_FILENAME,
        legacy_path=tmp_path / storage.LEGACY_PHASE1_FILENAME,
        now=BASE,
        app_session_id="windows-app",
        generation_id="22222222-2222-4222-8222-222222222222",
    )
    server = value._server(SERVER)
    server.events = [restart(hour, index + 1) for index, hour in enumerate((0, 3, 6, 9, 12))]
    server.coverage = list(coverage or [healthy(BASE, WINDOW_END + 600)])
    value.decision(SERVER, now=BASE + 12 * HOUR + 120)
    assert server.score.candidate(PERIOD3).fundamental_relationship_count >= 2
    return value, server


def _miss_at_15h(server):
    return [
        item
        for item in server.expected_misses
        if item.period_seconds == PERIOD3 and item.expected_at == EXPECTED
    ]


def _evaluate(value, server, now=WINDOW_END + 600):
    value._evaluate_expected_windows(server, now=now)
    return _miss_at_15h(server)


def _open_outage_engine(started_at):
    """A real detector with an open confirmed outage starting at ``started_at``."""

    engine = detection.PhysicalEpisodeEngine()

    def sample(at, info):
        return detection.ObservationSample(
            wall_at=at,
            monotonic_at=at - BASE,
            app_session_id="windows-app",
            monitoring_session_id="windows-monitor",
            poll_generation=1,
            server_key=SERVER,
            info_status=info,
            player_status=(
                detection.FieldStatus.PRESENT
                if info is detection.InfoStatus.HEALTHY
                else detection.FieldStatus.MISSING
            ),
            players=40 if info is detection.InfoStatus.HEALTHY else None,
        )

    engine.ingest(sample(started_at - 10, detection.InfoStatus.HEALTHY))
    engine.ingest(sample(started_at, detection.InfoStatus.TIMEOUT))
    engine.ingest(sample(started_at + 3, detection.InfoStatus.TIMEOUT))
    assert engine.state is detection.EpisodeState.OFFLINE
    assert engine.active_episode.started_wall == started_at
    return engine


def test_fully_observed_healthy_window_without_restart_still_records_miss(tmp_path):
    value, server = _learned_server(tmp_path)
    misses = _evaluate(value, server)
    assert len(misses) == 1


@pytest.mark.parametrize(
    "started_at",
    [WINDOW_START - 1800, WINDOW_START, EXPECTED, WINDOW_END],
    ids=["before-window", "at-window-start", "inside", "at-window-end"],
)
def test_open_outage_overlapping_window_blocks_miss(tmp_path, started_at):
    value, server = _learned_server(tmp_path)
    server.engine = _open_outage_engine(started_at)
    assert _evaluate(value, server) == []


def test_open_outage_starting_after_window_end_does_not_block_miss(tmp_path):
    value, server = _learned_server(tmp_path)
    server.engine = _open_outage_engine(WINDOW_END + 1)
    assert len(_evaluate(value, server)) == 1


def test_window_blocked_while_open_stays_blocked_after_finalization(tmp_path):
    value, server = _learned_server(tmp_path)
    start = WINDOW_START - 900
    server.engine = _open_outage_engine(start)
    assert _evaluate(value, server, now=WINDOW_END + 60) == []

    # The outage finalizes with its canonical time (first failure) outside
    # the window.  Before Stage 2 this re-evaluation produced a false miss.
    server.engine = detection.PhysicalEpisodeEngine()
    server.events.append(outage(90, start, WINDOW_END + 120))
    assert _evaluate(value, server, now=WINDOW_END + 600) == []
    assert _evaluate(value, server, now=WINDOW_END + 1200) == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (WINDOW_START - 1800, WINDOW_END + 1800),  # straddles the whole window
        (WINDOW_START - 1800, WINDOW_START + 60),  # ends inside, canonical before
        (WINDOW_START - 1800, WINDOW_START),  # ends exactly at the boundary
        (WINDOW_END, WINDOW_END + 1800),  # starts exactly at the boundary
    ],
    ids=["straddles", "ends-inside", "ends-at-start-boundary", "starts-at-end-boundary"],
)
def test_finalized_outage_overlap_blocks_miss_even_with_canonical_outside(tmp_path, start, end):
    value, server = _learned_server(tmp_path)
    event = outage(90, start, end)
    if start < WINDOW_START:
        assert event.canonical_phase_at < WINDOW_START
    server.events.append(event)
    assert _evaluate(value, server) == []


@pytest.mark.parametrize(
    ("start", "end"),
    [(WINDOW_START - 1800, WINDOW_START - 1), (WINDOW_END + 1, WINDOW_END + 1800)],
    ids=["ends-before-window", "starts-after-window"],
)
def test_finalized_outage_not_overlapping_window_keeps_miss(tmp_path, start, end):
    value, server = _learned_server(tmp_path)
    server.events.append(outage(90, start, end))
    assert len(_evaluate(value, server)) == 1


@pytest.mark.parametrize("canonical_inside", [True, False])
def test_service_interruption_overlapping_window_blocks_miss(tmp_path, canonical_inside):
    value, server = _learned_server(tmp_path)
    start = EXPECTED - 30 if canonical_inside else WINDOW_START - 120
    server.events.append(
        outage(90, start, start + 180, outcome=Outcome.SERVICE_INTERRUPTION)
    )
    assert _evaluate(value, server) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(coverage_complete=False, reasons=("outage_onset_unobserved",)),
        dict(
            outcome=Outcome.INCOMPLETE,
            lifecycle=detection.LifecycleMarker.SLEEP_GAP,
            reasons=("outage_polling_continuity_broken",),
        ),
    ],
    ids=["onset-unobserved", "polling-gap-interrupted"],
)
def test_learning_ineligible_outage_overlapping_window_blocks_miss(tmp_path, kwargs):
    value, server = _learned_server(tmp_path)
    event = outage(90, WINDOW_START - 600, WINDOW_START + 120, **kwargs)
    assert not scoring.event_learning_eligible(event)
    server.events.append(event)
    assert _evaluate(value, server) == []


def test_offline_coverage_inside_window_blocks_miss_without_events(tmp_path):
    # Retained coverage alone (for example after old events were folded)
    # must not turn offline observation into "no restart happened".
    coverage = [
        healthy(BASE, WINDOW_START + 60),
        scoring.CoverageSegment(
            WINDOW_START + 60, WINDOW_START + 120, scoring.CoverageKind.OFFLINE_OBSERVED, 3
        ),
        healthy(WINDOW_START + 120, WINDOW_END + 600),
    ]
    value, server = _learned_server(tmp_path, coverage=coverage)
    assert _evaluate(value, server) == []


def test_offline_coverage_touching_window_boundary_only_keeps_miss(tmp_path):
    coverage = [
        healthy(BASE, WINDOW_START - 60),
        scoring.CoverageSegment(
            WINDOW_START - 60, WINDOW_START, scoring.CoverageKind.OFFLINE_OBSERVED, 3
        ),
        healthy(WINDOW_START, WINDOW_END + 600),
    ]
    value, server = _learned_server(tmp_path, coverage=coverage)
    assert len(_evaluate(value, server)) == 1


@pytest.mark.parametrize(
    "kind", [scoring.CoverageKind.SLEEP_GAP, scoring.CoverageKind.UNMONITORED]
)
def test_unobserved_monitoring_gap_in_window_records_no_miss(tmp_path, kind):
    coverage = [
        healthy(BASE, EXPECTED - 60),
        scoring.CoverageSegment(EXPECTED - 60, EXPECTED + 60, kind, 10),
        healthy(EXPECTED + 60, WINDOW_END + 600),
    ]
    value, server = _learned_server(tmp_path, coverage=coverage)
    assert _evaluate(value, server) == []


def test_restart_inside_window_is_positive_evidence_and_not_a_miss(tmp_path):
    value, server = _learned_server(tmp_path)
    before = server.score.candidate(PERIOD3).strict_direct_interval_count
    server.events.append(restart(15, 6))
    assert _evaluate(value, server) == []
    value.decision(SERVER, now=WINDOW_END + 600)
    after = server.score.candidate(PERIOD3)
    assert after.strict_direct_interval_count == before + 1
    assert after.covered_miss_count == 0


def test_blocking_overlap_never_adds_positive_evidence(tmp_path):
    value, server = _learned_server(tmp_path)
    server.events.append(
        outage(90, WINDOW_START - 120, WINDOW_START + 60, outcome=Outcome.SERVICE_INTERRUPTION)
    )
    before = server.score.candidate(PERIOD3)
    assert _evaluate(value, server) == []
    value.decision(SERVER, now=WINDOW_END + 600)
    after = server.score.candidate(PERIOD3)
    assert after.strict_direct_interval_count == before.strict_direct_interval_count
    assert after.direct_support == before.direct_support


def test_repeated_evaluation_is_deterministic_without_duplicate_misses(tmp_path):
    value, server = _learned_server(tmp_path)
    first = _evaluate(value, server)
    for offset in (1, 60, 3600):
        assert _evaluate(value, server, now=WINDOW_END + 600 + offset) == first
    assert len(first) == 1
    keys = [(item.period_seconds, item.key) for item in server.expected_misses]
    assert len(keys) == len(set(keys))


def test_event_overlap_semantics_match_schema4_classifier():
    cases = [
        outage(1, WINDOW_START - 600, WINDOW_START),
        outage(2, WINDOW_START - 600, WINDOW_START - 1),
        outage(3, WINDOW_END, WINDOW_END + 600),
        outage(4, WINDOW_END + 1, WINDOW_END + 600),
        outage(5, WINDOW_START - 600, WINDOW_END + 600),
        restart(15, 6),
    ]
    for event in cases:
        assert scoring.event_overlaps_window(
            event, WINDOW_START, WINDOW_END
        ) is windows._event_overlaps(event, WINDOW_START, WINDOW_END)


# ---------------------------------------------------------------------------
# Scorer miss sources: divisor resolution and intermediate windows
# ---------------------------------------------------------------------------


def _score(events, end_hour):
    return scoring.RestartScheduleScorer().score(
        tuple(events),
        scoring.CoverageTimeline((healthy(BASE, BASE + end_hour * HOUR),)),
        now=BASE + end_hour * HOUR,
    )


def test_overlapping_outage_blocks_divisor_resolution_miss_only_in_its_window():
    plain = _score((restart(0, 1), restart(6, 2)), 6)
    assert plain.candidate(PERIOD3).covered_miss_count >= 1

    # A service interruption at the 3 h mark of a covered 6 h interval: the
    # 3 h window was unobservable, so it is not a 3 h miss.
    at = BASE + 3 * HOUR
    interrupted = _score(
        (
            restart(0, 1),
            outage(3, at - 60, at + 120, outcome=Outcome.SERVICE_INTERRUPTION),
            restart(6, 2),
        ),
        6,
    )
    assert interrupted.candidate(PERIOD3).covered_miss_count == 0
    # Stages 3+4: an interval crossing a service interruption is not a fully
    # observed direct interval either (it remains available as a hint).
    assert plain.candidate(6 * HOUR).strict_direct_interval_count == 1
    assert interrupted.candidate(6 * HOUR).strict_direct_interval_count == 0


def test_ineligible_outage_in_intermediate_window_is_not_a_covered_miss():
    at = BASE + 3 * HOUR
    blocker = outage(
        3,
        at - 600,
        at + 60,
        canonical=at - 600 - scoring.candidate_phase_tolerance(PERIOD3),
        coverage_complete=False,
        reasons=("outage_onset_unobserved",),
    )
    scored = _score((restart(0, 1), blocker, restart(6, 2)), 6)
    three = scored.candidate(PERIOD3)
    assert three.covered_miss_count == 0
    statuses = [
        item.status
        for diagnostic in three.multiple_interval_diagnostics
        for item in diagnostic.intermediate_windows
    ]
    assert scoring.IntermediateWindowStatus.ADEQUATELY_MONITORED_MISS not in statuses


# ---------------------------------------------------------------------------
# End to end: schema-3 and schema-4 consistency on synthetic polling
# ---------------------------------------------------------------------------


PERIOD2 = 2 * HOUR
TOL2 = scoring.candidate_phase_tolerance(PERIOD2)
WINDOW_AT = 4 * PERIOD2


class Poller:
    def __init__(self, value):
        self.value = value

    def poll(self, at, *, ok=True, players=40):
        info = {"ok": ok}
        if ok:
            info.update(players=players, max_players=60)
        else:
            info["err"] = "timeout"
        return self.value.ingest_live_result(
            SERVER, poll_generation=1, info=info, wall_at=BASE + at, monotonic_at=at
        )

    def healthy(self, start, stop, players=40):
        for at in range(int(start), int(stop), 10):
            self.poll(at, players=players)

    def outage(self, start, stop, *, return_players=6):
        for at in range(int(start), int(stop), 3):
            self.poll(at, ok=False)
        for offset in (0, 10, 20, 30, 40, 50):
            self.poll(stop + offset, players=return_players)
        return stop + 60


def _consistency_run(tmp_path, scenario):
    path = tmp_path / f"{scenario}.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=path,
        legacy_path=tmp_path / f"{scenario}-legacy.json",
        now=BASE,
        app_session_id=f"{scenario}-app",
        authoritative_schema4_runtime_enabled=True,
    )
    assert value.begin_monitoring(SERVER, wall_at=BASE, monotonic_at=0, poll_generation=1)
    poller = Poller(value)
    cursor = 0
    for index in (1, 2, 3):
        poller.healthy(cursor, index * PERIOD2)
        cursor = poller.outage(index * PERIOD2, index * PERIOD2 + 60)

    if scenario == "healthy":
        poller.healthy(cursor, WINDOW_AT + TOL2 + 1200)
    elif scenario == "straddle":
        poller.healthy(cursor, WINDOW_AT - TOL2 - 300)
        cursor = poller.outage(WINDOW_AT - TOL2 - 300, WINDOW_AT + TOL2 + 300)
        poller.healthy(cursor, WINDOW_AT + TOL2 + 1200)
    elif scenario == "service_interruption":
        # Unchanged population on return; onset just before the window so
        # the window itself is fully covered (healthy plus offline).
        poller.healthy(cursor, WINDOW_AT - TOL2 - 60)
        cursor = poller.outage(WINDOW_AT - TOL2 - 60, WINDOW_AT - TOL2 + 60, return_players=40)
        poller.healthy(cursor, WINDOW_AT + TOL2 + 1200)
    elif scenario == "restart_inside":
        poller.healthy(cursor, WINDOW_AT)
        cursor = poller.outage(WINDOW_AT, WINDOW_AT + 60)
        poller.healthy(cursor, WINDOW_AT + TOL2 + 1200)
    else:  # pragma: no cover
        raise AssertionError(scenario)

    server = value._servers[SERVER]
    schema3 = [
        item
        for item in server.expected_misses
        if item.period_seconds == PERIOD2 and item.expected_at == BASE + WINDOW_AT
    ]
    state = schema4.deserialize_schema4_bytes(path.read_bytes()).state
    assert schema4.validate_schema4_state(state).valid
    records = state["servers"][SERVER].get("expected_window_results", [])
    superseded = {item.get("supersedes_result_id") for item in records}
    schema4_active = [
        item
        for item in records
        if item["result_id"] not in superseded
        and item["outcome"] == windows.ExpectedWindowOutcome.GENUINE_MISS.value
        and item["negative_penalty_active"]
        and item["candidate_period_seconds"] == PERIOD2
        and item["expected_at"] == BASE + WINDOW_AT
    ]
    candidate = server.score.candidate(PERIOD2)
    value.shutdown(wall_at=BASE + WINDOW_AT + TOL2 + 1300, monotonic_at=WINDOW_AT + TOL2 + 1300)
    return server, schema3, schema4_active, candidate


def test_schema3_and_schema4_record_a_genuine_miss_for_a_healthy_window(tmp_path):
    server, schema3, schema4_active, candidate = _consistency_run(tmp_path, "healthy")
    assert len(schema3) == 1
    assert len(schema4_active) == 1
    assert candidate.covered_miss_count >= 1


@pytest.mark.parametrize("scenario", ["straddle", "service_interruption"])
def test_schema3_and_schema4_record_no_miss_for_outage_overlap(tmp_path, scenario):
    server, schema3, schema4_active, candidate = _consistency_run(tmp_path, scenario)
    assert schema3 == []
    assert schema4_active == []
    assert candidate.covered_miss_count == 0
    overlapping = [
        event
        for event in server.events
        if scoring.event_overlaps_window(event, BASE + WINDOW_AT - TOL2, BASE + WINDOW_AT + TOL2)
    ]
    assert len(overlapping) == 1
    if scenario == "service_interruption":
        assert overlapping[0].outcome is Outcome.SERVICE_INTERRUPTION


def test_restart_inside_window_is_a_hit_in_both_systems(tmp_path):
    server, schema3, schema4_active, candidate = _consistency_run(tmp_path, "restart_inside")
    assert schema3 == []
    assert schema4_active == []
    assert candidate.covered_miss_count == 0
    assert candidate.strict_direct_interval_count >= 3
    assert len({event.event_id for event in server.events}) == len(server.events) == 4
