from types import SimpleNamespace

import pytest

from dzll_launcher.config import (
    COMPANION_POLL_OFFLINE_SECONDS,
    COMPANION_POLL_ONLINE_SECONDS,
)
from dzll_launcher.companion_restart_phase2_runtime import (
    LiveResultDisposition,
    live_result_disposition,
)
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
        _server_companion_offline_since=None,
        _server_companion_offline_since_wall=None,
        _server_companion_alert_armed=False,
        _server_companion_restart_alert_enabled=False,
        _server_companion_obj=None,
        _server_companion_last_online=True,
        server_companion_panel=None,
        _server_companion_should_poll=lambda: True,
        _server_companion_restart_learning_key=lambda: None,
        _debug_server_companion_alert=lambda *_args: None,
        _handle_phase2_finalized_events=lambda *_args: None,
        _maybe_play_server_companion_restart_warning=lambda *_args: None,
    )

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


def test_second_failure_confirms_offline_and_first_health_recovers_immediately():
    host, intervals = companion_host()
    failure = {"ok": False, "err": "timed out", "a2s_classification": "timeout"}
    apply(host, failure)
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is False
    assert host._server_companion_snapshot["ping"] == -1
    assert host._server_companion_consecutive_offline_polls == 2
    assert intervals[-1] == COMPANION_POLL_OFFLINE_SECONDS == 3

    apply(host, {"ok": True, "ping_ms": 35, "players": 0, "max_players": 60})
    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_snapshot["ping"] == 35
    assert host._server_companion_consecutive_offline_polls == 0
    assert host._server_companion_first_offline_strike_mono is None
    assert intervals[-1] == COMPANION_POLL_ONLINE_SECONDS == 10


def test_healthy_between_failures_resets_visible_strike_count():
    host, _intervals = companion_host()
    failure = {"ok": False, "err": "refused", "a2s_classification": "socket-error"}
    apply(host, failure)
    apply(host, {"ok": True, "ping_ms": 30, "players": 12, "max_players": 60})
    apply(host, failure)

    assert host._server_companion_snapshot["online"] is True
    assert host._server_companion_consecutive_offline_polls == 1


def test_stopping_companion_polling_clears_pending_visible_strike():
    host, _intervals = companion_host()
    host._server_companion_consecutive_offline_polls = 1
    host._server_companion_first_offline_strike_mono = 12.0
    host._server_companion_poll_timer_id = 0

    DZLLWindow._stop_server_companion_polling(host)

    assert host._server_companion_consecutive_offline_polls == 0
    assert host._server_companion_first_offline_strike_mono is None


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
