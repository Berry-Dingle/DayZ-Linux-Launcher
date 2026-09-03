from types import MethodType, SimpleNamespace

import pytest

from dzll_launcher.config import (
    COMPANION_POLL_OFFLINE_SECONDS,
    COMPANION_POLL_ONLINE_SECONDS,
    COMPANION_RECOVERY_CONFIRM_DELAY_MS,
    COMPANION_RECOVERY_STABLE_ONLINE_SECONDS,
)
from dzll_launcher.companion_restart_phase2_runtime import (
    LiveResultDisposition,
    RECOVERY_ALERT_OUT_OF_WINDOW_HOLD_SECONDS,
    RecoveryAlertExpectedWindow,
    RecoveryAlertWindowStatus,
    classify_recovery_alert_expected_window,
    live_result_disposition,
)
from dzll_launcher.companion_restart_phase2_detection import EventOutcome
from dzll_launcher import window as window_module
from dzll_launcher.window import DZLLWindow


def companion_host():
    intervals = []
    host = SimpleNamespace(
        _server_companion_poll_token=7,
        _server_companion_poll_inflight=True,
        _server_companion_snapshot={
            "ip": "198.51.100.20",
            "qport": 2303,
            "online": True,
            "ping": 42,
            "players": 12,
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
        _server_companion_restart_alert_enabled=False,
        _server_companion_obj=None,
        server_companion_panel=None,
        _server_companion_should_poll=lambda: True,
        _server_companion_restart_learning_key=lambda: None,
        _debug_server_companion_alert=lambda *_args: None,
        _handle_phase2_finalized_events=lambda *_args: None,
        _refresh_server_companion_restart_learning_summary=lambda: None,
        _maybe_play_server_companion_restart_warning=lambda *_args: None,
        _server_companion_alert_back_online=lambda *_args, **_kwargs: None,
    )

    for name in (
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
    ):
        setattr(host, name, MethodType(getattr(DZLLWindow, name), host))

    def set_interval(value):
        host._server_companion_poll_interval_secs = value
        intervals.append(value)

    host._set_server_companion_poll_interval = set_interval
    return host, intervals


def apply(host, payload):
    host._server_companion_poll_inflight = True
    return DZLLWindow._apply_server_companion_live_result(host, 7, payload)


def test_structured_neutral_and_protocol_results_are_not_qualifying_failures():
    assert live_result_disposition({"ok": False, "neutral": True}) is LiveResultDisposition.NEUTRAL
    malformed = {
        "ok": False,
        "err": "truncated timeout field",
        "a2s_classification": "malformed",
    }
    assert live_result_disposition(malformed) is LiveResultDisposition.PROTOCOL_FAILURE


def test_first_qualifying_failure_preserves_online_and_normal_cadence():
    host, intervals = companion_host()
    apply(host, {"ok": False, "err": "timed out", "a2s_classification": "timeout"})

    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_snapshot["ping"] == 42
    assert host._server_companion_consecutive_offline_polls == 1
    assert intervals[-1] == COMPANION_POLL_ONLINE_SECONDS == 10


def test_neutral_and_protocol_observations_preserve_visible_state_and_strike():
    host, intervals = companion_host()
    apply(host, {"ok": False, "err": "timeout", "a2s_classification": "timeout"})
    before = dict(host._server_companion_snapshot)
    apply(host, {"ok": False, "neutral": True, "outcome": "alive-but-info-unavailable"})
    apply(host, {"ok": False, "err": "bad packet", "a2s_classification": "malformed"})

    assert host._server_companion_snapshot == before
    assert host._server_companion_consecutive_offline_polls == 1
    assert intervals == [COMPANION_POLL_ONLINE_SECONDS]


def test_stale_visible_strike_expires_through_neutral_and_protocol(monkeypatch):
    host, intervals = companion_host()
    clock = iter((0.0, 10.0, 20.0, 30.0))
    monkeypatch.setattr(window_module.time, "monotonic", lambda: next(clock))
    failure = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}

    apply(host, failure)
    apply(host, {"ok": False, "neutral": True})
    apply(host, {"ok": False, "err": "bad packet", "a2s_classification": "malformed"})
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_consecutive_offline_polls == 1
    assert host._server_companion_first_offline_strike_mono == 30.0
    assert intervals[-1] == COMPANION_POLL_ONLINE_SECONDS


def test_recent_visible_strike_survives_neutral_and_confirms_with_jitter(monkeypatch):
    host, intervals = companion_host()
    clock = iter((0.0, 10.0, 17.0))
    monkeypatch.setattr(window_module.time, "monotonic", lambda: next(clock))
    failure = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}

    apply(host, failure)
    apply(host, {"ok": False, "neutral": True})
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_consecutive_offline_polls == 2
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS


@pytest.mark.parametrize("gap", [10.0, 15.0, 17.0])
def test_visible_two_strike_timing_matches_realistic_poll_jitter(monkeypatch, gap):
    host, intervals = companion_host()
    clock = iter((0.0, gap))
    monkeypatch.setattr(window_module.time, "monotonic", lambda: next(clock))
    failure = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}

    apply(host, failure)
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_consecutive_offline_polls == 2
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS


def test_second_failure_confirms_offline_and_first_health_is_candidate_only(
    monkeypatch,
):
    host, intervals = companion_host()
    scheduled = []
    monkeypatch.setattr(
        window_module.GLib,
        "timeout_add",
        lambda delay, callback, candidate: (
            scheduled.append((delay, callback, candidate)) or 91
        ),
    )
    failure = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}
    apply(host, failure)
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_snapshot["ping"] == -1
    assert host._server_companion_consecutive_offline_polls == 2
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS == 3

    apply(host, {"ok": True, "ping_ms": 35, "players": 0, "max_players": 60})
    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_snapshot["ping"] == -1
    assert host._server_companion_consecutive_offline_polls == 2
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS == 3
    assert len(scheduled) == 1
    assert scheduled[0][0] == COMPANION_RECOVERY_CONFIRM_DELAY_MS == 2000
    assert host._server_companion_recovery_candidate is scheduled[0][2]


def test_healthy_between_failures_resets_visible_strike_count():
    host, _intervals = companion_host()
    failure = {"ok": False, "err": "refused", "a2s_classification": "socket-error"}
    apply(host, failure)
    apply(host, {"ok": True, "ping_ms": 30, "players": 12, "max_players": 60})
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_consecutive_offline_polls == 1


def recovery_host(monkeypatch, *, runtime=None, server_key=None):
    host, intervals = companion_host()
    alerts = []
    scheduled = []
    removed = []
    host._server_companion_restart_alert_enabled = True
    host._server_companion_alert_back_online = (
        lambda snapshot, alert_type="back online": alerts.append(
            (dict(snapshot), alert_type)
        )
    )
    if server_key is not None:
        host._server_companion_restart_learning_key = lambda: server_key
        host._companion_restart_phase2 = runtime
    monkeypatch.setattr(
        window_module.GLib,
        "timeout_add",
        lambda delay, callback, candidate: (
            scheduled.append((delay, callback, candidate)) or (90 + len(scheduled))
        ),
    )
    monkeypatch.setattr(
        window_module.GLib,
        "source_remove",
        lambda source_id: removed.append(source_id),
    )
    return host, intervals, alerts, scheduled, removed


def establish_offline(host, clock):
    failure = {
        "ok": False,
        "err": "timed out",
        "a2s_classification": "timeout",
    }
    clock[0] += 10.0
    apply(host, failure)
    clock[0] += 10.0
    apply(host, failure)
    assert host._server_companion_snapshot["online"] is False


def start_recovery_candidate(host, clock):
    clock[0] += 3.0
    apply(host, {"ok": True, "ping_ms": 31, "players": 0, "max_players": 60})
    candidate = host._server_companion_recovery_candidate
    assert candidate is not None
    assert host._server_companion_snapshot["online"] is False
    return candidate


def finish_recovery_confirmation(host, clock, payload):
    candidate = host._server_companion_recovery_candidate
    assert candidate is not None
    host._server_companion_recovery_confirmation_source_id = 0
    host._server_companion_recovery_confirmation_inflight = True
    host._server_companion_poll_inflight = True
    clock[0] += 2.0
    return host._apply_server_companion_recovery_confirmation_result(
        candidate, payload
    )


def test_short_outage_alerts_on_confirmation_without_learner_finalization(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(window_module.time, "time", lambda: 1_800_000_000 + clock[0])
    host, intervals, alerts, scheduled, _removed = recovery_host(monkeypatch)

    establish_offline(host, clock)
    offline_at = host._server_companion_offline_since
    start_recovery_candidate(host, clock)

    assert alerts == []
    assert len(scheduled) == 1
    assert scheduled[0][0] == 2000
    assert clock[0] - offline_at < 60

    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 28, "players": 1, "max_players": 60},
    )

    assert clock[0] - offline_at < 60
    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_consecutive_offline_polls == 0
    assert intervals[-1] == COMPANION_POLL_ONLINE_SECONDS
    assert len(alerts) == 1
    assert alerts[0][1] == "back online"
    assert host._server_companion_recovery_alert_suppressed


def test_recovery_confirmation_failure_stays_offline_and_resumes_offline_polling(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, intervals, alerts, _scheduled, _removed = recovery_host(monkeypatch)

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {
            "ok": False,
            "err": "timed out",
            "a2s_classification": "timeout",
        },
    )

    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_consecutive_offline_polls >= 2
    assert host._server_companion_recovery_candidate is None
    assert not host._server_companion_recovery_confirmation_inflight
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS
    assert alerts == []


def test_only_one_recovery_confirmation_candidate_can_be_pending(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, _intervals, alerts, scheduled, _removed = recovery_host(monkeypatch)

    establish_offline(host, clock)
    first = start_recovery_candidate(host, clock)
    clock[0] += 0.5
    apply(host, {"ok": True, "ping_ms": 29, "players": 0, "max_players": 60})

    assert host._server_companion_recovery_candidate is first
    assert len(scheduled) == 1
    assert alerts == []


def test_outage_sequence_advances_once_per_outage_and_again_after_recovery(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, _intervals, _alerts, _scheduled, _removed = recovery_host(monkeypatch)
    failure = {
        "ok": False,
        "err": "timed out",
        "a2s_classification": "timeout",
    }

    establish_offline(host, clock)
    first_offline_since = host._server_companion_offline_since
    assert host._server_companion_recovery_outage_sequence == 1

    clock[0] += 3.0
    apply(host, failure)
    assert host._server_companion_recovery_outage_sequence == 1
    assert host._server_companion_offline_since == first_offline_since

    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 1, "max_players": 60},
    )
    assert host._server_companion_offline_since is None

    establish_offline(host, clock)
    assert host._server_companion_recovery_outage_sequence == 2
    assert host._server_companion_offline_since > first_offline_since


def test_dedicated_timer_launches_exactly_one_confirmation_query(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, _intervals, alerts, scheduled, _removed = recovery_host(monkeypatch)
    queries = []

    class ImmediateExecutor:
        def submit(self, callback):
            callback()
            return SimpleNamespace()

    host._hi_executor = ImmediateExecutor()
    monkeypatch.setattr(
        window_module,
        "query_server_live",
        lambda ip, qport, **kwargs: (
            queries.append((ip, qport, kwargs))
            or {"ok": True, "ping_ms": 22, "players": 1, "max_players": 60}
        ),
    )
    monkeypatch.setattr(
        window_module.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    assert len(scheduled) == 1

    scheduled[0][1](scheduled[0][2])

    assert len(queries) == 1
    assert "companion-recovery-confirm" in queries[0][2]["cycle_id"]
    assert host._server_companion_snapshot["online"] is True
    assert len(alerts) == 1
    assert host._server_companion_recovery_candidate is None
    assert not host._server_companion_recovery_confirmation_inflight


class RecoveryRuntimeStub:
    def __init__(self):
        self.session_id = "session-1"
        self.ingested = []
        self.marked = []
        self.fired = set()
        self.window_result = RecoveryAlertExpectedWindow(
            RecoveryAlertWindowStatus.NO_SAFE_EXPECTATION,
            event_id="physical-event-1",
        )

    def active_monitoring_session_id(self, _key):
        return self.session_id

    def ingest_live_result(self, key, **kwargs):
        self.ingested.append((key, kwargs["info"]))
        return SimpleNamespace(accepted=True, finalized_events=())

    def provisional_event_id(self, _key):
        return "physical-event-1"

    def recovery_alert_expected_window(self, _key):
        return self.window_result

    def event_suppression_key(self, key, **kwargs):
        return (key, kwargs["kind"], kwargs["event_id"])

    def mark_fired(self, key, suppression, **_kwargs):
        if suppression in self.fired:
            return False
        self.fired.add(suppression)
        self.marked.append((key, suppression))
        return True


@pytest.mark.parametrize("stale_kind", ["generation", "session", "outage"])
def test_stale_recovery_confirmation_is_ignored(monkeypatch, stale_kind):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    host, _intervals, alerts, _scheduled, removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )

    establish_offline(host, clock)
    candidate = start_recovery_candidate(host, clock)
    assert len(runtime.ingested) == 2
    if stale_kind == "generation":
        host._server_companion_poll_token += 1
    elif stale_kind == "session":
        runtime.session_id = "session-2"
    else:
        host._server_companion_recovery_outage_sequence += 1

    host._server_companion_recovery_confirmation_source_id = 91
    host._server_companion_recovery_confirmation_inflight = True
    host._server_companion_poll_inflight = True
    host._apply_server_companion_recovery_confirmation_result(
        candidate,
        {"ok": True, "ping_ms": 25, "players": 1, "max_players": 60},
    )

    assert host._server_companion_snapshot["online"] is False
    assert alerts == []
    assert len(runtime.ingested) == 2
    assert host._server_companion_recovery_candidate is None
    assert removed == [91]


def test_confirmed_recovery_ingests_only_confirmation_and_reserves_before_alert(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    host, _intervals, alerts, _scheduled, _removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    assert len(runtime.ingested) == 2
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 1, "max_players": 60},
    )

    assert len(runtime.ingested) == 3
    assert runtime.ingested[-1][1]["ok"] is True
    assert len(runtime.marked) == 1
    assert len(alerts) == 1


def test_stable_online_rearm_blocks_flap_then_allows_later_genuine_cycle(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, _intervals, alerts, _scheduled, _removed = recovery_host(monkeypatch)

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 26, "players": 1, "max_players": 60},
    )
    assert len(alerts) == 1
    first_online = clock[0]

    # A brief confirmed flap is still queried and recovered, but cannot spam.
    clock[0] = first_online + 5
    apply(host, {"ok": False, "err": "timeout", "a2s_classification": "timeout"})
    clock[0] = first_online + 15
    apply(host, {"ok": False, "err": "timeout", "a2s_classification": "timeout"})
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 27, "players": 1, "max_players": 60},
    )
    assert len(alerts) == 1
    flap_recovered_at = clock[0]
    assert host._server_companion_recovery_alert_suppressed

    clock[0] = flap_recovered_at + COMPANION_RECOVERY_STABLE_ONLINE_SECONDS - 1
    apply(host, {"ok": True, "ping_ms": 27, "players": 1, "max_players": 60})
    assert host._server_companion_recovery_alert_suppressed
    clock[0] = flap_recovered_at + COMPANION_RECOVERY_STABLE_ONLINE_SECONDS
    apply(host, {"ok": True, "ping_ms": 27, "players": 1, "max_players": 60})
    assert not host._server_companion_recovery_alert_suppressed

    # Only an independently two-strike-confirmed outage can now produce alert two.
    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 25, "players": 1, "max_players": 60},
    )
    assert len(alerts) == 2


def test_five_minute_warning_state_does_not_interfere_with_recovery_alert(
    monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    host, _intervals, alerts, _scheduled, _removed = recovery_host(monkeypatch)
    host._server_companion_restart_warning_fired = {"five-minute-warning"}

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 23, "players": 1, "max_players": 60},
    )

    assert len(alerts) == 1
    assert host._server_companion_restart_warning_fired == {"five-minute-warning"}


@pytest.mark.parametrize("restart_offset", (-600.0, 600.0))
def test_safe_expected_restart_window_includes_exact_boundaries_and_alerts_immediately(
    monkeypatch, restart_offset
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    runtime.window_result = classify_recovery_alert_expected_window(
        event_id="physical-event-1",
        restart_started_at=10_000.0 + restart_offset,
        safe_prediction=True,
        candidate_period_seconds=3600,
        predicted_occurrence_at=10_000.0,
    )
    assert runtime.window_result.status is RecoveryAlertWindowStatus.INSIDE_WINDOW
    host, _intervals, alerts, scheduled, _removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 1, "max_players": 60},
    )

    assert len(alerts) == 1
    assert len(runtime.marked) == 1
    assert len(scheduled) == 1
    assert host._server_companion_pending_recovery_alert is None


@pytest.mark.parametrize("restart_offset", (-600.001, 600.001))
def test_safe_expected_restart_just_outside_boundaries_is_held(
    monkeypatch, restart_offset
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    runtime.window_result = classify_recovery_alert_expected_window(
        event_id="physical-event-1",
        restart_started_at=10_000.0 + restart_offset,
        safe_prediction=True,
        candidate_period_seconds=3600,
        predicted_occurrence_at=10_000.0,
    )
    assert runtime.window_result.status is RecoveryAlertWindowStatus.OUTSIDE_WINDOW
    host, _intervals, alerts, scheduled, _removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 1, "max_players": 60},
    )

    assert alerts == []
    assert runtime.marked == []
    assert host._server_companion_pending_recovery_alert is not None
    assert scheduled[-1][0] == int(
        RECOVERY_ALERT_OUT_OF_WINDOW_HOLD_SECONDS * 1000
    ) == 45_000


def held_recovery_host(monkeypatch, *, bob_like=False):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    restart_started_at = 1_786_748_318.1833496 if bob_like else 10_700.0
    prediction = 1_786_752_010.069199 if bob_like else 10_000.0
    period = 10_800 if bob_like else 3600
    runtime.window_result = classify_recovery_alert_expected_window(
        event_id="physical-event-1",
        restart_started_at=restart_started_at,
        safe_prediction=True,
        candidate_period_seconds=period,
        predicted_occurrence_at=prediction,
    )
    assert runtime.window_result.status is RecoveryAlertWindowStatus.OUTSIDE_WINDOW
    host, _intervals, alerts, scheduled, removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )
    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 12, "max_players": 60},
    )
    assert alerts == []
    return clock, runtime, host, alerts, scheduled, removed


def finalized_recovery_event(outcome):
    return SimpleNamespace(
        event_id="physical-event-1",
        monitoring_session_id="session-1",
        poll_generation=7,
        outcome=outcome,
    )


def test_held_service_interruption_suppresses_without_consuming_fired_key(monkeypatch):
    _clock, runtime, host, alerts, _scheduled, _removed = held_recovery_host(
        monkeypatch
    )

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(
            finalized_events=(finalized_recovery_event(EventOutcome.SERVICE_INTERRUPTION),)
        ),
        host._server_companion_snapshot,
    )

    assert alerts == []
    assert runtime.marked == []
    assert runtime.fired == set()
    assert host._server_companion_pending_recovery_alert is None


def test_bob_like_out_of_window_non_restart_is_suppressed(monkeypatch):
    _clock, runtime, host, alerts, _scheduled, _removed = held_recovery_host(
        monkeypatch, bob_like=True
    )
    assert runtime.window_result.residual_seconds == pytest.approx(3691.885849)

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(
            finalized_events=(finalized_recovery_event(EventOutcome.SERVICE_INTERRUPTION),)
        ),
        host._server_companion_snapshot,
    )

    assert alerts == []
    assert runtime.marked == []


@pytest.mark.parametrize(
    "outcome",
    (
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
    ),
)
def test_held_genuine_restart_releases_immediately_with_existing_deduplication(
    monkeypatch, outcome
):
    clock, runtime, host, alerts, _scheduled, _removed = held_recovery_host(
        monkeypatch
    )
    recovery_at = clock[0]
    clock[0] += 31.0

    DZLLWindow._handle_phase2_finalized_events(
        host,
        SimpleNamespace(finalized_events=(finalized_recovery_event(outcome),)),
        host._server_companion_snapshot,
    )

    assert len(alerts) == 1
    assert len(runtime.marked) == 1
    assert host._server_companion_recovery_online_since == recovery_at
    assert host._server_companion_pending_recovery_alert is None

    host._server_companion_recovery_alert_suppressed = False
    host._emit_server_companion_confirmed_recovery_alert(
        host._server_companion_snapshot,
        now=clock[0],
        now_wall=1_800_000_000 + clock[0],
        event_id="physical-event-1",
    )
    assert len(alerts) == 1
    assert len(runtime.marked) == 1


def test_unresolved_held_event_fails_open_after_45_seconds(monkeypatch):
    clock, runtime, host, alerts, scheduled, _removed = held_recovery_host(
        monkeypatch
    )
    recovery_at = clock[0]
    clock[0] += RECOVERY_ALERT_OUT_OF_WINDOW_HOLD_SECONDS

    scheduled[-1][1](scheduled[-1][2])

    assert len(alerts) == 1
    assert len(runtime.marked) == 1
    assert host._server_companion_recovery_online_since == recovery_at
    assert host._server_companion_pending_recovery_alert is None


@pytest.mark.parametrize("policy_case", ("unavailable", "unsafe", "stale", "suspended"))
def test_no_safe_expected_restart_cases_fail_open_to_immediate_alert(
    monkeypatch, policy_case
):
    clock = [0.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: clock[0])
    runtime = RecoveryRuntimeStub()
    runtime.window_result = classify_recovery_alert_expected_window(
        event_id="physical-event-1",
        restart_started_at=10_700.0,
        safe_prediction=False,
        candidate_period_seconds=3600,
        predicted_occurrence_at=10_000.0,
    )
    assert runtime.window_result.status is RecoveryAlertWindowStatus.NO_SAFE_EXPECTATION
    host, _intervals, alerts, scheduled, _removed = recovery_host(
        monkeypatch, runtime=runtime, server_key="198.51.100.20:2302"
    )

    establish_offline(host, clock)
    start_recovery_candidate(host, clock)
    finish_recovery_confirmation(
        host,
        clock,
        {"ok": True, "ping_ms": 24, "players": 1, "max_players": 60},
    )

    assert policy_case
    assert len(alerts) == 1
    assert len(runtime.marked) == 1
    assert len(scheduled) == 1


@pytest.mark.parametrize("stale_kind", ("server", "session", "generation"))
def test_held_alert_cannot_leak_across_stale_identity(monkeypatch, stale_kind):
    _clock, runtime, host, alerts, scheduled, _removed = held_recovery_host(
        monkeypatch
    )
    if stale_kind == "server":
        host._server_companion_restart_learning_key = lambda: "other:2302"
    elif stale_kind == "session":
        runtime.session_id = "session-2"
    else:
        host._server_companion_poll_token += 1

    scheduled[-1][1](scheduled[-1][2])

    assert alerts == []
    assert runtime.marked == []
    assert host._server_companion_pending_recovery_alert is None


def test_stopping_companion_polling_clears_pending_visible_strike():
    host, _intervals = companion_host()
    host._server_companion_consecutive_offline_polls = 1
    host._server_companion_first_offline_strike_mono = 12.0
    host._server_companion_offline_since = 11.0
    host._server_companion_recovery_outage_sequence = 4
    host._server_companion_poll_timer_id = 0

    DZLLWindow._stop_server_companion_polling(host)

    assert host._server_companion_consecutive_offline_polls == 0
    assert host._server_companion_first_offline_strike_mono is None
    assert host._server_companion_offline_since == 11.0
    assert host._server_companion_recovery_outage_sequence == 4


@pytest.mark.parametrize(
    "runtime_enabled,shadow_enabled,cutover_enabled",
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, False, True),
        (True, True, True),
    ],
)
def test_confirmed_offline_holds_available_restart_countdown_at_zero(
    monkeypatch, runtime_enabled, shadow_enabled, cutover_enabled
):
    host = SimpleNamespace(
        _server_companion_snapshot={"online": False},
        _server_companion_consecutive_offline_polls=2,
        _server_companion_restart_alert_enabled=True,
        _server_companion_restart_learning_key=lambda: "server:2302",
        _companion_restart_phase2=SimpleNamespace(
            decision=lambda *_args, **_kwargs: object(),
            authority_consumer_cutover_summary=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(
        window_module, "AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED", runtime_enabled
    )
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED", shadow_enabled
    )
    monkeypatch.setattr(
        window_module,
        "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED",
        cutover_enabled,
    )
    monkeypatch.setattr(
        window_module,
        "phase2_learning_summary",
        lambda _decision, now: {
            "countdown_text": "00:04",
            "prediction_usable": False,
        },
    )

    summary = DZLLWindow._server_companion_restart_learning_summary(host)
    assert summary["countdown_text"] == "00:00"
    assert summary["prediction_usable"] is True


def _authority_summary(
    *,
    presentation="confirmed_cycle",
    cycle_text="Confirmed: Every 3 hours",
    countdown_text="01:23",
    countdown_visible=True,
    countdown_safe=True,
    prediction_usable=True,
):
    return {
        "authority_consumer": True,
        "confidence_percent": 97,
        "confidence_kind": "period",
        "confidence_label": "Confidence:",
        "confidence_visible": True,
        "cycle_text": cycle_text,
        "next_text": "1900010800",
        "next_visible": countdown_visible,
        "countdown_text": countdown_text,
        "countdown_visible": countdown_visible,
        "countdown_safe": countdown_safe,
        "prediction_usable": prediction_usable,
        "presentation_key": presentation,
        "reason_codes": (f"presentation:{presentation}",),
    }


def _summary_host(authority_summary, *, online=False, strikes=2, debug=None):
    return SimpleNamespace(
        _server_companion_snapshot={"online": online},
        _server_companion_consecutive_offline_polls=strikes,
        _server_companion_restart_alert_enabled=True,
        _server_companion_restart_learning_key=lambda: "server:2302",
        _debug_server_companion_alert=(debug or (lambda *_args: None)),
        _companion_restart_phase2=SimpleNamespace(
            decision=lambda *_args, **_kwargs: object(),
            authority_consumer_cutover_summary=(
                lambda *_args, **_kwargs: authority_summary
            ),
        ),
    )


def test_confirmed_offline_safe_schema4_holds_only_displayed_countdown(monkeypatch):
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True
    )
    monkeypatch.setattr(window_module, "phase2_learning_summary", lambda *_args, **_kwargs: {"cycle_text": "legacy"})
    host = _summary_host(_authority_summary())

    summary = DZLLWindow._server_companion_restart_learning_summary(host)

    assert summary["authority_consumer"]
    assert summary["presentation_key"] == "confirmed_cycle"
    assert summary["cycle_text"] == "Confirmed: Every 3 hours"
    assert summary["confidence_percent"] == 97
    assert summary["next_text"] == "1900010800"
    assert summary["countdown_text"] == "00:00"
    assert summary["countdown_safe"] and summary["countdown_visible"]
    assert summary["prediction_usable"]
    assert "confirmed_offline_countdown_held_at_zero" in summary["reason_codes"]


@pytest.mark.parametrize(
    "summary",
    [
        _authority_summary(
            presentation="likely_cycle",
            cycle_text="Likely: Every 3 hours",
            countdown_text="--",
            countdown_visible=False,
            countdown_safe=False,
            prediction_usable=False,
        ),
        _authority_summary(
            presentation="likely_new_cycle",
            cycle_text="Likely new: Every 4 hours",
            countdown_text="--",
            countdown_visible=False,
            countdown_safe=False,
            prediction_usable=False,
        ),
        _authority_summary(
            presentation="schedule_change_suspected",
            cycle_text="Schedule change suspected",
            countdown_text="--",
            countdown_visible=False,
            countdown_safe=False,
            prediction_usable=False,
        ),
        _authority_summary(
            presentation="prediction_temporarily_suspended",
            cycle_text="Prediction temporarily suspended",
            countdown_text="--",
            countdown_visible=False,
            countdown_safe=False,
            prediction_usable=False,
        ),
    ],
)
def test_confirmed_offline_unsafe_schema4_summary_is_not_clamped(
    monkeypatch, summary
):
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True
    )
    monkeypatch.setattr(window_module, "phase2_learning_summary", lambda *_args, **_kwargs: {"countdown_text": "legacy"})

    result = DZLLWindow._server_companion_restart_learning_summary(
        _summary_host(summary)
    )

    assert result == summary
    assert result["countdown_text"] == "--"
    assert not result["countdown_visible"]
    assert not result["countdown_safe"]
    assert not result["prediction_usable"]
    assert "confirmed_offline_countdown_held_at_zero" not in result["reason_codes"]


@pytest.mark.parametrize("online,strikes", [(True, 1), (True, 0)])
def test_schema4_first_strike_and_recovery_do_not_apply_offline_hold(
    monkeypatch, online, strikes
):
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True
    )
    monkeypatch.setattr(window_module, "phase2_learning_summary", lambda *_args, **_kwargs: None)
    summary = _authority_summary()

    result = DZLLWindow._server_companion_restart_learning_summary(
        _summary_host(summary, online=online, strikes=strikes)
    )

    assert result == summary
    assert result["countdown_text"] == "01:23"


def test_restart_learning_summary_exception_records_diagnostic(monkeypatch):
    messages = []
    monkeypatch.setattr(
        window_module, "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED", True
    )
    monkeypatch.setattr(window_module, "phase2_learning_summary", lambda *_args, **_kwargs: {"countdown_text": "01:23"})
    host = SimpleNamespace(
        _server_companion_snapshot={"online": False},
        _server_companion_consecutive_offline_polls=2,
        _server_companion_restart_alert_enabled=True,
        _server_companion_restart_learning_key=lambda: "server:2302",
        _debug_server_companion_alert=messages.append,
        _companion_restart_phase2=SimpleNamespace(
            decision=lambda *_args, **_kwargs: object()
        ),
    )

    assert DZLLWindow._server_companion_restart_learning_summary(host) is None
    assert len(messages) == 1
    assert "restart-learning summary unavailable" in messages[0]
    assert "authority_consumer_cutover_summary" in messages[0]
