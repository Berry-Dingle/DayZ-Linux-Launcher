"""Server Companion Stages 3+4: local internet health and incident integrity.

All probes are deterministic fakes (no real network); learning state is a
temporary schema-4 file; clocks are injected.
"""

from __future__ import annotations

import socket
from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import local_connectivity as lc
from dzll_launcher import window as window_module
from dzll_launcher.companion_restart_phase2_runtime import (
    RecoveryAlertExpectedWindow,
    RecoveryAlertWindowStatus,
)
from dzll_launcher.window import DZLLWindow


HEALTHY = lc.ConnectivityStatus.HEALTHY
LOCAL = lc.ConnectivityStatus.LOCAL_FAILURE
UNKNOWN = lc.ConnectivityStatus.UNKNOWN
BASE = 2_400_000_000.0
SERVER_KEY = "203.0.113.7:2302"
FAILURE = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}


def healthy_info(players=40, queue=None):
    info = {"ok": True, "ping_ms": 30, "players": players, "max_players": 60}
    if queue is not None:
        info["queue"] = queue
    return info


# ---------------------------------------------------------------------------
# 8-11. Classification and the bounded probe
# ---------------------------------------------------------------------------


P = lc.ProviderResult


@pytest.mark.parametrize(
    ("google", "cloudflare", "expected"),
    [
        (P.SUCCESS, P.SUCCESS, HEALTHY),
        (P.FAILURE, P.FAILURE, LOCAL),
        (P.SUCCESS, P.FAILURE, UNKNOWN),
        (P.FAILURE, P.SUCCESS, UNKNOWN),
        (P.INCONCLUSIVE, P.FAILURE, UNKNOWN),
        (P.SUCCESS, P.INCONCLUSIVE, UNKNOWN),
        (P.INCONCLUSIVE, P.INCONCLUSIVE, UNKNOWN),
    ],
)
def test_two_independent_providers_must_agree(google, cloudflare, expected):
    assert lc.classify_connectivity({lc.GOOGLE: google, lc.CLOUDFLARE: cloudflare}) is expected


def test_missing_provider_is_unknown():
    assert lc.classify_connectivity({lc.GOOGLE: P.SUCCESS}) is UNKNOWN
    assert lc.classify_connectivity({}) is UNKNOWN


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ((lc.TargetOutcome.CONNECTED, lc.TargetOutcome.TIMEOUT), P.SUCCESS),
        ((lc.TargetOutcome.TIMEOUT, lc.TargetOutcome.UNREACHABLE), P.FAILURE),
        ((lc.TargetOutcome.TIMEOUT, lc.TargetOutcome.TIMEOUT), P.FAILURE),
        ((lc.TargetOutcome.REFUSED, lc.TargetOutcome.TIMEOUT), P.INCONCLUSIVE),
        ((lc.TargetOutcome.ERROR, lc.TargetOutcome.TIMEOUT), P.INCONCLUSIVE),
        ((), P.INCONCLUSIVE),
    ],
)
def test_provider_failure_requires_every_address_to_time_out_or_be_unreachable(
    outcomes, expected
):
    assert lc.classify_provider(outcomes) is expected


def test_errno_mapping_treats_refusal_and_unknown_errors_as_inconclusive():
    import errno

    assert lc.outcome_for_errno(errno.ETIMEDOUT) is lc.TargetOutcome.TIMEOUT
    assert lc.outcome_for_errno(errno.ENETUNREACH) is lc.TargetOutcome.UNREACHABLE
    assert lc.outcome_for_errno(errno.EHOSTUNREACH) is lc.TargetOutcome.UNREACHABLE
    assert lc.outcome_for_errno(errno.ECONNREFUSED) is lc.TargetOutcome.REFUSED
    assert lc.outcome_for_errno(errno.EACCES) is lc.TargetOutcome.ERROR
    assert lc.outcome_for_errno(None) is lc.TargetOutcome.ERROR


def test_default_targets_are_ip_literals_for_two_providers_on_443():
    providers = {target.provider for target in lc.DEFAULT_PROBE_TARGETS}
    assert providers == {lc.GOOGLE, lc.CLOUDFLARE}
    for target in lc.DEFAULT_PROBE_TARGETS:
        socket.inet_aton(target.host)  # literal IPv4: no DNS involved
        assert target.port == 443


def test_probe_targets_uses_loopback_only_connect_and_refusal():
    # Loopback only: no real network access.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    try:
        targets = (
            lc.ProbeTarget(lc.GOOGLE, "127.0.0.1", listener.getsockname()[1]),
            lc.ProbeTarget(lc.CLOUDFLARE, "127.0.0.1", closed_port),
        )
        outcomes = dict(lc.probe_targets(targets, timeout=2.0))
    finally:
        listener.close()
    assert outcomes[targets[0]] is lc.TargetOutcome.CONNECTED
    assert outcomes[targets[1]] is lc.TargetOutcome.REFUSED
    status, _providers = lc.classify_target_outcomes(outcomes.items())
    assert status is UNKNOWN  # a refusal is never evidence of health


def test_probe_error_becomes_unknown_not_evidence(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("no sockets")

    monkeypatch.setattr(lc, "probe_targets", boom)
    assert lc.default_probe().status is UNKNOWN


# ---------------------------------------------------------------------------
# Guard behaviour with deterministic fake probes
# ---------------------------------------------------------------------------


class FakeLoop:
    """Collects submitted workers and posted callbacks for manual stepping."""

    def __init__(self, clock):
        self.clock = clock
        self.workers = []
        self.posted = []
        self.results = []
        self.current = UNKNOWN  # status of any unscripted round

    def submit(self, fn):
        self.workers.append(fn)

    def post(self, fn, *args):
        self.posted.append((fn, args))
        return 1

    def probe(self):
        status = self.results.pop(0) if self.results else self.current
        if isinstance(status, Exception):
            raise status
        return lc.ProbeRound(status, {}, self.clock[0], BASE + self.clock[1])

    def run_workers(self):
        while self.workers:
            self.workers.pop(0)()

    def run_posted(self):
        while self.posted:
            fn, args = self.posted.pop(0)
            fn(*args)

    def step(self, *results):
        if self.workers:
            self.results.extend(results[: len(self.workers)])
        self.run_workers()
        self.run_posted()
        self.results.clear()


def make_guard(clock, **callbacks):
    loop = FakeLoop(clock)
    guard = lc.ServerCompanionConnectivityGuard(
        submit=loop.submit,
        post=loop.post,
        probe=loop.probe,
        monotonic=lambda: clock[0],
        wall=lambda: BASE + clock[1],
        log=lambda _message: None,
        **callbacks,
    )
    return guard, loop


def advance(clock, seconds, *, wall=None):
    clock[0] += seconds
    clock[1] += seconds if wall is None else wall


SCOPE = (SERVER_KEY, 7)


def test_no_probe_without_a_qualifying_failure():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=False) is lc.IncidentVerdict.NONE
    assert guard.rounds_started == 0 and loop.workers == []


def test_rounds_are_single_outstanding_and_bounded_by_cadence():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    for _ in range(8):
        advance(clock, 1)
        guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 1 and len(loop.workers) == 1
    loop.step(HEALTHY)
    for _ in range(20):  # 20 s more: still inside the 30 s healthy repeat
        advance(clock, 1)
        guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 1
    advance(clock, 3)
    guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 2
    loop.step(UNKNOWN)
    advance(clock, 3)
    guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 2  # unresolved retry is 10 s
    advance(clock, 8)
    guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 3


def test_lost_probe_result_is_abandoned_and_cannot_block_forever():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.workers.clear()  # the result never comes back
    advance(clock, lc.PROBE_ABANDON_SECONDS + 1)
    guard.note_failure(SCOPE, confirmed_offline=True)
    assert guard.rounds_started == 2


def test_verified_recovery_requires_fresh_healthy_rounds_only():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.step(HEALTHY)
    advance(clock, 3)
    guard.note_failure(SCOPE, confirmed_offline=True)
    advance(clock, 3)
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=True) is (
        lc.IncidentVerdict.VERIFIED_HEALTHY
    )


@pytest.mark.parametrize("second", [UNKNOWN, OSError("probe broke")])
def test_mixed_or_errored_round_leaves_incident_unresolved(second):
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.step(HEALTHY)
    for _ in range(11):
        advance(clock, 3)
        guard.note_failure(SCOPE, confirmed_offline=True)
    loop.step(second)
    for _ in range(4):
        advance(clock, 3)
        guard.note_failure(SCOPE, confirmed_offline=True)
    loop.step(HEALTHY)  # a later healthy round cannot launder the incident
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=True) is (
        lc.IncidentVerdict.UNRESOLVED
    )


def test_stale_healthy_round_is_unresolved():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.step(HEALTHY)
    for _ in range(20):  # polling continues, but rounds silently fail to start
        advance(clock, 3)
        guard.note_observation(SCOPE, confirmed_offline=True)
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=True) is (
        lc.IncidentVerdict.UNRESOLVED
    )
    assert "stale_healthy_round" in guard.incident.reasons


def test_suspend_gap_makes_incident_unresolved_even_with_frozen_monotonic():
    clock = [0.0, 0.0]
    guard, loop = make_guard(clock)
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.step(HEALTHY)
    advance(clock, 3)
    guard.note_failure(SCOPE, confirmed_offline=True)
    advance(clock, 3, wall=8 * 3600)  # suspend: wall jumps, monotonic barely moves
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=True) is (
        lc.IncidentVerdict.UNRESOLVED
    )
    assert guard.incident.unobserved_gap


def test_local_failure_contaminates_suspends_and_healthy_restores():
    clock = [0.0, 0.0]
    events = []
    guard, loop = make_guard(
        clock,
        on_local_failure=lambda incident: events.append(("local", incident.incident_id)),
        on_connectivity_restored=lambda incident: events.append(("restored", incident.incident_id)),
    )
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.step(LOCAL)
    assert guard.incident_contaminated() and guard.learning_suspended(SCOPE)
    assert guard.local_failure_active()
    for _ in range(4):
        advance(clock, 3)
        guard.note_failure(SCOPE, confirmed_offline=True)
    loop.step(HEALTHY)
    assert events == [("local", 1), ("restored", 1)]
    assert not guard.learning_suspended(SCOPE)
    assert not guard.local_failure_active()
    # Contamination is permanent for this incident.
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=True) is (
        lc.IncidentVerdict.CONTAMINATED
    )


def test_stale_result_after_session_switch_is_ignored():
    clock = [0.0, 0.0]
    events = []
    guard, loop = make_guard(clock, on_local_failure=lambda incident: events.append(incident))
    guard.note_failure(SCOPE, confirmed_offline=False)
    loop.run_workers()  # result is now posted but not yet applied
    loop.results.append(LOCAL)
    guard.note_failure((SERVER_KEY, 8), confirmed_offline=False)  # new generation
    loop.run_posted()
    assert events == []
    assert guard.incident.scope == (SERVER_KEY, 8)
    assert guard.incident.local_failure_rounds == 0


def test_shutdown_with_active_probe_is_safe_and_ignores_late_results():
    clock = [0.0, 0.0]
    events = []
    guard, loop = make_guard(clock, on_local_failure=lambda incident: events.append(incident))
    guard.note_failure(SCOPE, confirmed_offline=False)
    guard.shutdown()
    loop.step(LOCAL)
    assert events == [] and guard.incident is None
    guard.note_failure(SCOPE, confirmed_offline=False)
    assert guard.rounds_started == 1 and loop.workers == []


def test_submit_failure_is_recorded_as_unknown_not_healthy():
    clock = [0.0, 0.0]

    def broken_submit(_fn):
        raise RuntimeError("executor shut down")

    guard = lc.ServerCompanionConnectivityGuard(
        submit=broken_submit,
        post=lambda *_a: None,
        monotonic=lambda: clock[0],
        wall=lambda: BASE + clock[1],
        log=lambda _m: None,
    )
    guard.note_failure(SCOPE, confirmed_offline=False)
    assert guard.incident.unknown_rounds == 1
    assert guard.evaluate_recovery(SCOPE, confirmed_offline=False) is (
        lc.IncidentVerdict.UNRESOLVED
    )


# ---------------------------------------------------------------------------
# Window integration with a real temporary learning runtime
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self, monkeypatch, tmp_path, *, runtime_value=None, name="sc"):
        self.clock = [0.0, 0.0]
        self.alerts = []
        self.scheduled = []
        self.loop = FakeLoop(self.clock)
        monkeypatch.setattr(window_module.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(window_module.time, "time", lambda: BASE + self.clock[1])
        monkeypatch.setattr(
            window_module.GLib,
            "timeout_add",
            lambda delay, callback, data: self.scheduled.append((delay, callback, data))
            or (100 + len(self.scheduled)),
        )
        monkeypatch.setattr(window_module.GLib, "source_remove", lambda _sid: None)
        monkeypatch.setattr(
            DZLLWindow, "_surface_restart_learning_persistence_notice", lambda _self: None
        )
        if runtime_value is None:
            runtime_value = runtime.Phase2RestartRuntime.initialize(
                active_path=tmp_path / f"{name}.json",
                legacy_path=tmp_path / f"{name}-legacy.json",
                now=BASE,
                app_session_id=f"{name}-app",
                authoritative_schema4_runtime_enabled=True,
            )
        self.runtime = runtime_value
        self.path = tmp_path / f"{name}.json"
        host = SimpleNamespace(
            _server_companion_poll_token=7,
            _server_companion_poll_inflight=True,
            _server_companion_poll_timer_id=0,
            _server_companion_snapshot={
                "ip": "203.0.113.7",
                "qport": 2303,
                "online": True,
                "ping": 30,
                "players": 40,
                "max_players": 60,
            },
            _server_companion_visible_snapshot=None,
            _server_companion_consecutive_offline_polls=0,
            _server_companion_first_offline_strike_mono=None,
            _server_companion_recovery_confirmation_source_id=0,
            _server_companion_recovery_confirmation_inflight=False,
            _server_companion_recovery_confirmation_nonce=0,
            _server_companion_recovery_candidate=None,
            _server_companion_recovery_outage_sequence=0,
            _server_companion_recovery_alert_suppressed=False,
            _server_companion_recovery_online_since=None,
            _server_companion_pending_recovery_alert=None,
            _server_companion_pending_recovery_alert_source_id=0,
            _server_companion_offline_since=None,
            _server_companion_restart_alert_enabled=True,
            _server_companion_obj=None,
            _server_companion_phase2_first_accept_session_id="",
            _server_companion_phase2_logged_rejections=set(),
            server_companion_panel=None,
            _companion_restart_phase2=runtime_value,
            _server_companion_should_poll=lambda: True,
            _server_companion_restart_learning_key=lambda: SERVER_KEY,
            _debug_server_companion_alert=lambda *_args: None,
            _refresh_server_companion_restart_learning_summary=lambda: None,
            _maybe_play_server_companion_restart_warning=lambda *_args: None,
            _server_companion_alert_back_online=lambda snapshot, alert_type="back online": (
                self.alerts.append(alert_type)
            ),
        )
        for name_ in (
            "_cancel_server_companion_recovery_confirmation",
            "_server_companion_recovery_session_id",
            "_server_companion_recovery_confirmation_is_current",
            "_schedule_server_companion_recovery_confirmation",
            "_begin_server_companion_recovery_confirmation",
            "_apply_server_companion_recovery_confirmation_result",
            "_update_server_companion_recovery_alert_rearm",
            "_cancel_server_companion_pending_recovery_alert",
            "_server_companion_pending_recovery_alert_is_current",
            "_maybe_hold_server_companion_confirmed_recovery_alert",
            "_release_server_companion_pending_recovery_alert",
            "_resolve_server_companion_pending_recovery_alert",
            "_emit_server_companion_confirmed_recovery_alert",
            "_apply_server_companion_live_result",
            "_handle_phase2_finalized_events",
            "_record_server_companion_monitor_started",
            "_record_server_companion_monitor_ended",
        ):
            setattr(host, name_, MethodType(getattr(DZLLWindow, name_), host))
        host._set_server_companion_poll_interval = lambda value: None
        host._server_companion_is_validated_query_visible_recovery = (
            DZLLWindow._server_companion_is_validated_query_visible_recovery
        )
        self.host = host
        self.guard = lc.ServerCompanionConnectivityGuard(
            submit=self.loop.submit,
            post=self.loop.post,
            probe=self.loop.probe,
            monotonic=lambda: self.clock[0],
            wall=lambda: BASE + self.clock[1],
            on_local_failure=lambda incident: DZLLWindow._on_server_companion_local_connectivity_failure(
                host, incident
            ),
            on_connectivity_restored=lambda incident: DZLLWindow._on_server_companion_connectivity_restored(
                host, incident
            ),
            log=lambda _m: None,
        )
        host._server_companion_connectivity = self.guard
        host._record_server_companion_monitor_started(SERVER_KEY)

    # -- driving ----------------------------------------------------------

    def advance(self, seconds, *, wall=None):
        advance(self.clock, seconds, wall=wall)

    def poll(self, info, *, after=10.0, probe=None):
        self.advance(after)
        if probe is not None:
            self.loop.current = probe
        self.host._server_companion_poll_inflight = True
        self.host._apply_server_companion_live_result(7, info)
        self.loop.step()

    def healthy(self, seconds, players=40, queue=None):
        for _ in range(int(seconds // 10)):
            self.poll(healthy_info(players, queue))

    def outage(
        self,
        seconds,
        *,
        probe,
        return_info,
        confirm=True,
        gap_wall=None,
        later_probe=None,
        switch_after=None,
    ):
        """Strike 1, strike 2, then 3 s offline polls, then a return.

        ``later_probe`` replaces ``probe`` once ``switch_after`` seconds of
        offline polling have elapsed (for example HEALTHY, then LOCAL).
        """

        self.poll(FAILURE, probe=probe)
        self.poll(FAILURE, probe=probe)
        elapsed = 0.0
        while elapsed < seconds:
            if later_probe is not None and elapsed >= switch_after:
                self.loop.current = later_probe
            self.poll(FAILURE, after=3.0)
            elapsed += 3.0
        if gap_wall is not None:
            self.advance(3.0, wall=gap_wall)
            self.host._server_companion_poll_inflight = True
            self.host._apply_server_companion_live_result(7, return_info)
        else:
            self.poll(return_info, after=3.0)
        candidate = self.host._server_companion_recovery_candidate
        if candidate is not None and confirm:
            self.host._server_companion_recovery_confirmation_inflight = True
            self.host._server_companion_poll_inflight = True
            self.advance(2.0)
            self.host._apply_server_companion_recovery_confirmation_result(
                candidate, return_info
            )
        return candidate

    # -- inspection -------------------------------------------------------

    @property
    def server(self):
        return self.runtime._servers[SERVER_KEY]

    def events(self):
        return list(self.server.events)

    def eligible(self):
        return [item for item in self.events() if scoring.event_learning_eligible(item)]

    def schema4_record(self):
        state = schema4.deserialize_schema4_bytes(self.path.read_bytes()).state
        assert schema4.validate_schema4_state(state).valid
        return state["servers"][SERVER_KEY]


def test_genuine_oo_outage_with_healthy_probes_alerts_and_learns(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    candidate = h.outage(60, probe=HEALTHY, return_info=healthy_info(6))
    assert candidate is not None
    assert h.alerts == ["back online"]
    h.healthy(90, players=6)
    events = h.events()
    assert len(events) == 1
    assert events[0].outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert scoring.event_learning_eligible(events[0])
    assert h.alerts == ["back online"]  # generic consumer does not duplicate
    assert h.guard.incident is None


@pytest.mark.parametrize(
    ("players", "queue", "return_players", "return_queue"),
    [
        (2, None, 2, None),  # quiet server
        (40, None, 7, None),  # busy server, changed population on return
        (40, 5, 40, 9),  # pre-existing queue
    ],
    ids=["quiet", "busy", "queue"],
)
def test_local_outage_produces_no_restart_learning_or_alert(
    monkeypatch, tmp_path, players, queue, return_players, return_queue
):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60, players=players, queue=queue)
    session_before = h.runtime.active_monitoring_session_id(SERVER_KEY)
    h.outage(60, probe=LOCAL, return_info=healthy_info(return_players, return_queue))
    assert h.alerts == []
    assert h.host._server_companion_recovery_candidate is None
    h.healthy(90, players=return_players, queue=return_queue)
    assert h.eligible() == []
    for event in h.events():
        assert event.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
        assert event.outcome in {
            detection.EventOutcome.INCOMPLETE,
            detection.EventOutcome.AMBIGUOUS_DRAIN,
        }
    session_after = h.runtime.active_monitoring_session_id(SERVER_KEY)
    assert session_after and session_after != session_before
    assert h.alerts == []


def test_local_outage_marks_visible_state_as_no_internet_not_server_offline(
    monkeypatch, tmp_path
):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(30)
    h.poll(FAILURE, probe=LOCAL)
    h.poll(FAILURE, probe=LOCAL)
    assert h.host._server_companion_snapshot["online"] is False
    assert h.host._server_companion_snapshot["connectivity"] == "local_failure"
    # Contaminated failures are not forwarded to learning while suspended.
    ingested_before = len(h.server.coverage)
    h.poll(FAILURE, after=3.0, probe=LOCAL)
    assert len(h.server.coverage) == ingested_before
    h.poll(healthy_info(), after=3.0)
    assert "connectivity" not in h.host._server_companion_snapshot


def test_local_outage_during_qv_drain_discards_the_incident(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(40, players=12)
    for players in (9, 5, 0):
        h.poll(healthy_info(players))
    h.outage(30, probe=LOCAL, return_info=healthy_info(0))
    for players in (3, 6, 9, 12):
        h.poll(healthy_info(players))
    h.healthy(60, players=12)
    assert h.alerts == []
    assert h.eligible() == []
    assert h.server.engine.provisional_drain is None or (
        h.server.engine.provisional_drain.baseline_wall
        > h.events()[0].finalized_at
    )


def test_genuine_restart_hidden_by_local_outage_is_excluded_then_next_one_learned(
    monkeypatch, tmp_path
):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    # The server really restarts during a local outage (population differs).
    h.outage(90, probe=LOCAL, return_info=healthy_info(8))
    h.healthy(60, players=8)
    assert h.eligible() == [] and h.alerts == []
    # 7. A complete genuine restart after connectivity returns is learned.
    h.outage(45, probe=HEALTHY, return_info=healthy_info(3))
    h.healthy(90, players=3)
    eligible = h.eligible()
    assert len(eligible) == 1
    assert eligible[0].outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART
    assert h.alerts == ["back online"]


@pytest.mark.parametrize("probe", [UNKNOWN, OSError("probe failed")], ids=["mixed", "error"])
def test_unknown_connectivity_never_alerts_or_learns(monkeypatch, tmp_path, probe):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(45, probe=probe, return_info=healthy_info(5))
    h.healthy(90, players=5)
    assert h.alerts == []
    assert h.eligible() == []


def test_suspend_during_offline_episode_gives_no_false_recovery_alert(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(30, probe=HEALTHY, return_info=healthy_info(9), gap_wall=8 * 3600)
    h.healthy(90, players=9)
    assert h.alerts == []
    assert h.eligible() == []


def test_genuine_qv_restart_with_healthy_connectivity_is_unchanged(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    timeline = [
        (0, 12), (10, 12), (20, 12), (40, 11), (60, 9), (80, 7), (100, 5),
        (120, 3), (140, 0), (150, 0), (160, 0), (170, 0), (180, 0), (190, 0),
        (200, 0), (210, 3), (220, 6), (230, 8), (240, 9),
    ]
    last = 0
    for at, players in timeline:
        h.poll(healthy_info(players), after=at - last if at else 10.0)
        last = at
    h.healthy(90, players=10)
    assert h.guard.rounds_started == 0  # no failures, no probes
    assert "query-visible rejoin" in h.alerts
    assert any(
        item.outcome
        in {
            detection.EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
            detection.EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
        }
        and scoring.event_learning_eligible(item)
        for item in h.events()
    )


def test_previous_learning_is_unchanged_and_rebuild_is_deterministic(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(45, probe=HEALTHY, return_info=healthy_info(5))
    h.healthy(90, players=5)
    before = h.schema4_record()
    prior = {item["event_id"]: item for item in before["physical_events"]}
    prior_relationships = {
        item["relationship_id"]: item for item in before["interval_relationships"]
    }
    alerts_before = list(h.alerts)

    h.outage(60, probe=LOCAL, return_info=healthy_info(9))
    h.healthy(90, players=9)
    after = h.schema4_record()
    current = {item["event_id"]: item for item in after["physical_events"]}
    for event_id, item in prior.items():
        assert current[event_id] == item
    for relationship_id, item in prior_relationships.items():
        assert {r["relationship_id"]: r for r in after["interval_relationships"]}[
            relationship_id
        ] == item
    assert h.alerts == alerts_before

    # Repeated rebuilding (reload twice) is deterministic and emits nothing.
    h.runtime.shutdown(wall_at=BASE + h.clock[1] + 1, monotonic_at=h.clock[0] + 1)
    snapshots = []
    for index in range(2):
        again = runtime.Phase2RestartRuntime.initialize(
            active_path=h.path,
            legacy_path=tmp_path / "sc-legacy.json",
            now=BASE + h.clock[1] + 2 + index,
            app_session_id=f"reload-{index}",
            authoritative_schema4_runtime_enabled=True,
        )
        state = schema4.deserialize_schema4_bytes(h.path.read_bytes()).state
        record = state["servers"][SERVER_KEY]
        snapshots.append(
            (
                sorted(item["event_id"] for item in record["physical_events"]),
                sorted(item["relationship_id"] for item in record["interval_relationships"]),
                sorted(
                    (item["relationship_id"], item["high_authority_eligible"])
                    for item in record["interval_relationships"]
                ),
            )
        )
        again.shutdown(wall_at=BASE + h.clock[1] + 3 + index, monotonic_at=1.0)
    assert snapshots[0] == snapshots[1]
    assert h.alerts == alerts_before


def test_local_failure_at_first_strike_neutralises_before_any_episode(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(45, probe=LOCAL, return_info=healthy_info(6))
    assert h.events() == []  # nothing could be learned, not even an ineligible event
    assert h.alerts == []


def test_local_failure_mid_episode_finalizes_it_interrupted(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(
        90,
        probe=HEALTHY,
        later_probe=LOCAL,
        switch_after=30,
        return_info=healthy_info(6),
    )
    h.healthy(90, players=6)
    events = h.events()
    assert len(events) == 1
    assert events[0].lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    assert not scoring.event_learning_eligible(events[0])
    assert h.alerts == []


def test_generic_recovery_consumer_cannot_resurrect_contaminated_alert(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    h.healthy(60)
    h.outage(
        60, probe=HEALTHY, later_probe=LOCAL, switch_after=30, return_info=healthy_info(6)
    )
    contaminated = h.events()[0]
    assert contaminated.lifecycle_interruption is detection.LifecycleMarker.SLEEP_GAP
    snapshot = dict(h.host._server_companion_snapshot)
    h.host._handle_phase2_finalized_events(
        SimpleNamespace(finalized_events=(contaminated,)), snapshot
    )
    decision = h.runtime.decision(SERVER_KEY, now=BASE + h.clock[1], event=contaminated)
    assert not decision.generic_recovery_eligible
    assert h.alerts == []


class HoldRuntime:
    """Minimal runtime with an out-of-window expectation, forcing a hold."""

    def __init__(self):
        self.session = 1
        self.fired = set()
        self.ended = []

    def active_monitoring_session_id(self, _key):
        return f"session-{self.session}"

    def ensure_monitoring_session(self, _key, **_kwargs):
        return f"session-{self.session}", False

    def end_monitoring_if_current(self, _key, **kwargs):
        self.ended.append(kwargs["marker"])
        self.session += 1
        return SimpleNamespace(accepted=True, rejected_reason=None)

    def ingest_live_result(self, _key, **_kwargs):
        return SimpleNamespace(accepted=True, finalized_events=())

    def provisional_event_id(self, _key):
        return "held-event"

    def recovery_alert_expected_window(self, _key):
        return RecoveryAlertExpectedWindow(
            RecoveryAlertWindowStatus.OUTSIDE_WINDOW, event_id="held-event"
        )

    def event_suppression_key(self, key, **kwargs):
        return (key, kwargs["kind"], kwargs["event_id"])

    def mark_fired(self, _key, suppression, **_kwargs):
        if suppression in self.fired:
            return False
        self.fired.add(suppression)
        return True


def test_pending_recovery_hold_cannot_escape_after_local_failure(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, runtime_value=HoldRuntime())
    h.healthy(30)
    h.outage(30, probe=HEALTHY, return_info=healthy_info(5))
    pending = h.host._server_companion_pending_recovery_alert
    assert pending is not None and h.alerts == []
    release = [item for item in h.scheduled if item[2] is pending][0][1]
    # Local connectivity fails while the 45 s hold is pending.
    h.poll(FAILURE, probe=LOCAL)
    assert h.host._server_companion_pending_recovery_alert is None
    assert h.runtime.ended[-1] is detection.LifecycleMarker.SLEEP_GAP
    h.advance(45)
    release(pending)
    assert h.alerts == []


def test_held_alert_release_is_cancelled_while_failure_sequence_unresolved(
    monkeypatch, tmp_path
):
    h = Harness(monkeypatch, tmp_path, runtime_value=HoldRuntime())
    h.healthy(30)
    h.outage(30, probe=HEALTHY, return_info=healthy_info(5))
    pending = h.host._server_companion_pending_recovery_alert
    release = [item for item in h.scheduled if item[2] is pending][0][1]
    h.poll(FAILURE)  # strike 1 with no probe verdict yet
    h.loop.workers.clear()
    h.advance(45)
    release(pending)
    assert h.alerts == []


# ---------------------------------------------------------------------------
# 19. Direct intervals across unobservable episodes
# ---------------------------------------------------------------------------


HOUR = scoring.HOUR


def _event(hours, sequence, outcome=detection.EventOutcome.CORROBORATED_OFFLINE_RESTART, *, eligible=True, duration=60):
    at = BASE + hours * HOUR
    chain = continuity.deterministic_continuity_chain_id(SERVER_KEY, "app", "mon", 1, BASE)
    authenticity = 0.97 if outcome is detection.EventOutcome.CORROBORATED_OFFLINE_RESTART else (
        0.10 if outcome is detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION else 0.20
    )
    return detection.PhysicalRestartEvent(
        event_id=f"interval-{sequence}",
        sequence=sequence,
        fingerprint=f"interval-fp-{sequence}",
        server_key=SERVER_KEY,
        episode_started_at=at,
        finalized_at=at + duration + 30,
        canonical_phase_at=at,
        phase_uncertainty=3,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE, detection.SignalSource.INFO_RETURN}),
        outcome=outcome,
        authenticity=authenticity,
        schedule_weight_suggestion=authenticity,
        drain=detection.DrainSummary(None, None, None, None, False, None, None, 0, 0, False),
        outage=detection.OutageSummary(at, at + 3, 4, (), at + duration),
        recovery=detection.RecoverySummary(None, at + duration, at + duration, 2, False),
        query_health=detection.QueryHealthSummary(8, 4, 0, eligible),
        coverage_complete=eligible,
        lifecycle_interruption=None,
        samples=(),
        reason_codes=(),
        app_session_id="app",
        monitoring_session_id="mon",
        poll_generation=1,
        continuity_chain_id=chain,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _scored(events, end_hour):
    return scoring.RestartScheduleScorer().score(
        tuple(events),
        scoring.CoverageTimeline(
            (scoring.CoverageSegment(BASE, BASE + end_hour * HOUR, scoring.CoverageKind.ONLINE_HEALTHY, 10),)
        ),
        now=BASE + end_hour * HOUR,
    )


def _spans(end_hour):
    chain = continuity.deterministic_continuity_chain_id(SERVER_KEY, "app", "mon", 1, BASE)
    return (
        continuity.ContinuitySpan(
            provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
            reference_id="span-all",
            server_key=SERVER_KEY,
            app_session_id="app",
            monitoring_session_id="mon",
            poll_generation=1,
            continuity_chain_id=chain,
            start_at=BASE - 60,
            end_at=BASE + end_hour * HOUR + 600,
            start_monotonic=0.0,
            end_monotonic=end_hour * HOUR + 660,
            kind=continuity.ContinuitySpanKind.ONLINE_HEALTHY,
            cadence_seconds=10.0,
            reason_codes=("test",),
        ),
    )


def _direct(relationships, left, right, period):
    return [
        item
        for item in relationships
        if item.left_event_id == left
        and item.right_event_id == right
        and item.candidate_period_seconds == period
    ]


@pytest.mark.parametrize(
    "middle",
    [
        dict(outcome=detection.EventOutcome.SERVICE_INTERRUPTION),
        dict(eligible=False),
    ],
    ids=["service-interruption", "ineligible-outage"],
)
def test_unobservable_episode_inside_interval_blocks_direct_and_high(middle):
    plain = (_event(0, 1), _event(3, 3))
    blocked = (_event(0, 1), _event(1.5, 2, **middle), _event(3, 3))

    assert _scored(plain, 3).candidate(3 * HOUR).strict_direct_interval_count == 1
    three = _scored(blocked, 3).candidate(3 * HOUR)
    assert three.strict_direct_interval_count == 0
    assert three.hints.event_count >= 2  # still available as cautious sparse hints

    rel_plain = continuity.extract_interval_relationships(plain, _spans(3), candidate_periods=(3 * HOUR,))
    rel_blocked = continuity.extract_interval_relationships(blocked, _spans(3), candidate_periods=(3 * HOUR,))
    assert _direct(rel_plain, "interval-1", "interval-3", 3 * HOUR)[0].high_authority_eligible
    item = _direct(rel_blocked, "interval-1", "interval-3", 3 * HOUR)[0]
    assert not item.high_authority_eligible
    # A service interruption is non-authentic: the new reason applies.  An
    # ineligible authentic outage already makes the pair a skip-over.
    assert (
        "high_ineligible_intervening_unobservable_episode" in item.reason_codes
        or item.relationship_kind is continuity.RelationshipKind.SKIP_OVER
    )
    if middle.get("outcome") is detection.EventOutcome.SERVICE_INTERRUPTION:
        assert "high_ineligible_intervening_unobservable_episode" in item.reason_codes


def test_single_poll_uncertain_blip_does_not_block_direct_interval():
    events = (
        _event(0, 1),
        _event(1.5, 2, detection.EventOutcome.UNCERTAIN_A2S_INTERRUPTION),
        _event(3, 3),
    )
    assert _scored(events, 3).candidate(3 * HOUR).strict_direct_interval_count == 1


def test_local_outage_spanning_expected_window_records_no_miss(monkeypatch, tmp_path):
    period = 2 * HOUR
    tolerance = scoring.candidate_phase_tolerance(period)
    h = Harness(monkeypatch, tmp_path)

    def run_until(target, players=40):
        while h.clock[0] + 10 <= target:
            h.poll(healthy_info(players))

    for index in (1, 2, 3):  # three fully observed restarts, 2 h apart
        run_until(index * period - 10)
        h.advance(index * period - 10 - h.clock[0])
        h.outage(45, probe=HEALTHY, return_info=healthy_info(6))
        h.healthy(90, players=6)
    assert len(h.eligible()) == 3
    alerts_before = list(h.alerts)
    before = h.server.score.candidate(period)
    assert before.fundamental_relationship_count >= 2

    window_at = 4 * period
    run_until(window_at - tolerance - 300)
    # Local outage from before the window until after it.
    h.outage(2 * tolerance + 600, probe=LOCAL, return_info=healthy_info(9))
    run_until(window_at + tolerance + 1200, players=9)

    misses = [
        item
        for item in h.server.expected_misses
        if item.period_seconds == period
        and abs(item.expected_at - (h.eligible()[0].canonical_phase_at + 3 * period)) <= tolerance
    ]
    assert misses == []
    after = h.server.score.candidate(period)
    assert after.covered_miss_count == before.covered_miss_count == 0
    assert after.strict_direct_interval_count == before.strict_direct_interval_count
    assert h.server.score.regime_status is before_regime_status(before, h)
    assert len(h.eligible()) == 3
    assert h.alerts == alerts_before


def before_regime_status(_before, h):
    # Suspicion must not be raised by the contaminated incident.
    assert h.server.score.regime_status is not scoring.RegimeStatus.CHANGE_SUSPECTED
    return h.server.score.regime_status
