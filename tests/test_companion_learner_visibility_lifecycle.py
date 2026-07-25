import json
from types import MethodType, SimpleNamespace

from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher.window import DZLLWindow


A = "83.147.29.57:2502"
B = "198.51.100.8:2302"
BASE = 1_780_000_000.0


def make_runtime(tmp_path):
    active = tmp_path / "phase2.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "legacy.json",
        now=BASE,
        app_session_id="app",
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    return value, active


def healthy(value, key, generation, at):
    return value.ingest_live_result(
        key,
        poll_generation=generation,
        info={"ok": True, "players": 12, "max_players": 60, "queue": 0},
        wall_at=BASE + at,
        monotonic_at=at,
    )


def test_ensure_session_is_idempotent_and_replaces_stale_generation(tmp_path):
    value, _active = make_runtime(tmp_path)
    first, created = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=4
    )
    reused, created_again = value.ensure_monitoring_session(
        A, wall_at=BASE + 1, monotonic_at=1, poll_generation=4
    )
    replacement, replaced = value.ensure_monitoring_session(
        A, wall_at=BASE + 2, monotonic_at=2, poll_generation=5
    )

    assert first == reused
    assert created and not created_again
    assert replaced and replacement != first
    sessions = value._servers[A].monitoring_sessions
    assert sessions[-2]["reason"] == detection.LifecycleMarker.SERVER_SWITCH.value
    assert sessions[-1]["ended_at"] is None
    assert value._servers[A].poll_generation == 5


def test_hide_show_equivalent_recreates_before_first_healthy_poll(tmp_path):
    value, _active = make_runtime(tmp_path)
    old, _ = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=7
    )
    value.end_monitoring(
        A, marker=detection.LifecycleMarker.PAUSE,
        wall_at=BASE + 10, monotonic_at=10,
    )
    assert value.active_monitoring_session_id(A) == ""

    resumed, created = value.ensure_monitoring_session(
        A, wall_at=BASE + 11, monotonic_at=11, poll_generation=8
    )
    update = healthy(value, A, 8, 12)
    coverage_update = healthy(value, A, 8, 22)

    assert created and resumed != old
    assert update.accepted
    assert update.rejected_reason is None
    assert coverage_update.accepted
    assert value._servers[A].coverage
    assert value._servers[A].events == []


def test_server_switch_and_return_preserve_history_and_route_by_key(tmp_path):
    value, _active = make_runtime(tmp_path)
    first_a, _ = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=1
    )
    assert healthy(value, A, 1, 1).accepted
    assert healthy(value, A, 1, 11).accepted
    value.end_monitoring(
        A, marker=detection.LifecycleMarker.SERVER_SWITCH,
        wall_at=BASE + 2, monotonic_at=2,
    )
    first_b, _ = value.ensure_monitoring_session(
        B, wall_at=BASE + 3, monotonic_at=3, poll_generation=2
    )
    assert first_b
    assert healthy(value, B, 2, 4).accepted
    assert not healthy(value, A, 2, 4).accepted
    value.end_monitoring(
        B, marker=detection.LifecycleMarker.SERVER_SWITCH,
        wall_at=BASE + 5, monotonic_at=5,
    )
    second_a, _ = value.ensure_monitoring_session(
        A, wall_at=BASE + 6, monotonic_at=6, poll_generation=3
    )

    assert second_a != first_a
    assert len(value._servers[A].monitoring_sessions) == 2
    assert len(value._servers[A].coverage) >= 1
    assert value._servers[B].monitoring_sessions[-1]["reason"] == "server_switch"


def test_online_only_coverage_persists_on_runtime_lifecycle_boundary(tmp_path):
    value, active = make_runtime(tmp_path)
    value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=9
    )
    for at in (0, 10, 20, 30):
        assert healthy(value, A, 9, at).accepted
    assert value._servers[A].events == []
    value.end_monitoring(
        A, marker=detection.LifecycleMarker.SHUTDOWN,
        wall_at=BASE + 31, monotonic_at=31,
    )

    stored = json.loads(active.read_text())["servers"][A]
    assert stored["coverage_segments"][-1]["kind"] == "online_healthy"
    assert stored["monitoring_sessions"][-1]["reason"] == "shutdown"
    assert stored["events"] == []


class Widget:
    def __init__(self, value=False):
        self.value = value

    def set_visible(self, value):
        self.value = bool(value)

    def set_reveal_child(self, value):
        self.value = bool(value)

    def get_reveal_child(self):
        return self.value


def visibility_host(tmp_path):
    learner, _active = make_runtime(tmp_path)
    calls = []
    host = SimpleNamespace(
        _companion_restart_phase2=learner,
        _server_companion_snapshot={"ip": "83.147.29.57", "qport": 2503},
        _server_companion_poll_token=8,
        _server_companion_docked=True,
        server_companion_revealer=Widget(),
        main_browser_box=SimpleNamespace(set_size_request=lambda *_args: None),
        _server_companion_restart_learning_key=lambda: A,
        _dock_server_companion=lambda: calls.append("dock"),
        _restore_server_companion_if_enabled=lambda: calls.append("restore"),
        _cancel_server_companion_post_undock_shrink=lambda: None,
        _collapse_server_companion_dock_space=lambda: None,
        _refresh_server_companion_monitor_highlight=lambda: None,
        _refresh_server_companion_power_controls=lambda: None,
        _debug_server_companion_alert=lambda message: calls.append(message),
    )
    def end(reason, **_kwargs):
        calls.append(f"end:{reason}")
        learner.end_monitoring(
            A,
            marker=detection.LifecycleMarker.PAUSE,
            wall_at=BASE + 10,
            monotonic_at=10,
        )

    def stop():
        calls.append("stop")
        host._server_companion_poll_token += 1

    host._record_server_companion_monitor_ended = end
    host._stop_server_companion_polling = stop
    host._record_server_companion_monitor_started = MethodType(
        DZLLWindow._record_server_companion_monitor_started, host
    )
    def start():
        host._record_server_companion_monitor_started(A)
        calls.append("poll")

    host._start_server_companion_polling = start
    return host, learner, calls


def test_visible_retained_snapshot_ensures_session_before_poll_and_reuses_it(tmp_path):
    host, learner, calls = visibility_host(tmp_path)

    DZLLWindow.set_server_companion_visible(host, True)
    first = learner.active_monitoring_session_id(A)
    DZLLWindow.set_server_companion_visible(host, True)

    assert first
    assert learner.active_monitoring_session_id(A) == first
    assert len(learner._servers[A].monitoring_sessions) == 1
    assert calls.index("poll") > next(
        index for index, item in enumerate(calls)
        if item.startswith("restart-learning session created")
    )
    assert any("restart-learning session reused" in item for item in calls)


def test_window_hide_then_show_recreates_session_with_current_generation(tmp_path):
    host, learner, calls = visibility_host(tmp_path)
    DZLLWindow.set_server_companion_visible(host, True)
    first = learner.active_monitoring_session_id(A)

    DZLLWindow.set_server_companion_visible(host, False)
    assert learner.active_monitoring_session_id(A) == ""
    assert learner._servers[A].monitoring_sessions[-1]["reason"] == "pause"

    DZLLWindow.set_server_companion_visible(host, True)
    resumed = learner.active_monitoring_session_id(A)
    assert resumed and resumed != first
    assert learner._servers[A].poll_generation == 9
    assert calls[-1] == "poll"


def test_pause_resume_and_dock_neutrality_do_not_duplicate_session(tmp_path):
    host, learner, calls = visibility_host(tmp_path)
    DZLLWindow.set_server_companion_visible(host, True)
    session = learner.active_monitoring_session_id(A)

    host._dock_server_companion()
    host._dock_server_companion()
    assert learner.active_monitoring_session_id(A) == session
    assert len(learner._servers[A].monitoring_sessions) == 1
    assert calls.count("dock") >= 3


def test_stale_pause_cannot_end_newer_session(tmp_path):
    value, _active = make_runtime(tmp_path)
    old_session, _ = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=1
    )
    replacement, _ = value.ensure_monitoring_session(
        A, wall_at=BASE + 1, monotonic_at=1, poll_generation=2
    )

    stale = value.end_monitoring_if_current(
        A,
        marker=detection.LifecycleMarker.PAUSE,
        wall_at=BASE + 2,
        monotonic_at=2,
        expected_session_id=old_session,
        expected_poll_generation=1,
    )

    assert not stale.accepted
    assert stale.rejected_reason == "stale_monitoring_lifecycle"
    assert value.active_monitoring_session_id(A) == replacement
    assert value._servers[A].monitoring_sessions[-1]["ended_at"] is None


def test_stale_generation_cannot_pause_matching_session_id(tmp_path):
    value, _active = make_runtime(tmp_path)
    session, _ = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=8
    )

    stale = value.end_monitoring_if_current(
        A,
        marker=detection.LifecycleMarker.PAUSE,
        wall_at=BASE + 1,
        monotonic_at=1,
        expected_session_id=session,
        expected_poll_generation=7,
    )

    assert stale.rejected_reason == "stale_monitoring_lifecycle"
    assert value.active_monitoring_session_id(A) == session


def test_polling_start_boundary_repairs_missing_ownership_before_submit(
    tmp_path, monkeypatch
):
    learner, _active = make_runtime(tmp_path)
    calls = []
    host = SimpleNamespace(
        _companion_restart_phase2=learner,
        _server_companion_poll_token=12,
        _server_companion_poll_timer_id=0,
        _server_companion_should_poll=lambda: True,
        _server_companion_restart_learning_key=lambda: A,
        _debug_server_companion_alert=lambda message: calls.append(message),
        _submit_server_companion_poll=lambda: calls.append("submit"),
        _server_companion_poll_tick=lambda: True,
    )
    host._record_server_companion_monitor_started = MethodType(
        DZLLWindow._record_server_companion_monitor_started, host
    )

    class FakeGLib:
        @staticmethod
        def timeout_add_seconds(_interval, _callback):
            calls.append("timer")
            return 99

    host._server_companion_poll_interval_secs = 10
    from dzll_launcher import window as window_module

    monkeypatch.setattr(window_module, "GLib", FakeGLib)
    DZLLWindow._start_server_companion_polling(host)

    assert learner.active_monitoring_session_id(A)
    assert learner._servers[A].poll_generation == 12
    assert calls[-1] == "submit"
    assert next(i for i, item in enumerate(calls) if "session created" in item) < (
        calls.index("submit")
    )


def test_repeated_current_pause_is_idempotent(tmp_path):
    value, _active = make_runtime(tmp_path)
    session, _ = value.ensure_monitoring_session(
        A, wall_at=BASE, monotonic_at=0, poll_generation=4
    )
    first = value.end_monitoring_if_current(
        A,
        marker=detection.LifecycleMarker.PAUSE,
        wall_at=BASE + 1,
        monotonic_at=1,
        expected_session_id=session,
        expected_poll_generation=4,
    )
    second = value.end_monitoring_if_current(
        A,
        marker=detection.LifecycleMarker.PAUSE,
        wall_at=BASE + 2,
        monotonic_at=2,
        expected_session_id=session,
        expected_poll_generation=4,
    )

    assert first.accepted
    assert second.rejected_reason == "no_active_monitoring_session"
    assert len(value._servers[A].monitoring_sessions) == 1
