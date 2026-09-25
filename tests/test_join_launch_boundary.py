from types import SimpleNamespace
import threading

from dzll_launcher import join_prepare, window as window_module
from dzll_launcher.join_attempt import JoinAttemptTracker
from dzll_launcher.launch_utils import SteamLaunchResult


class Button:
    def __init__(self):
        self.label = "Cancel"
        self.visible = True
        self.sensitive = True
        self.tooltip = ""

    def set_label(self, value):
        self.label = str(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = str(value)


class ImmediateGLib:
    @staticmethod
    def idle_add(callback, *args):
        callback(*args)
        return 1


class BoundaryHarness:
    _enter_join_launch_boundary = window_module.DZLLWindow._enter_join_launch_boundary
    _launch_direct_steam_url = window_module.DZLLWindow._launch_direct_steam_url
    _join_preparation_cancel_clicked = (
        window_module.DZLLWindow._join_preparation_cancel_clicked
    )
    _show_join_launch_error = window_module.DZLLWindow._show_join_launch_error
    _steam_client_set_cancel_buttons = (
        window_module.DZLLWindow._steam_client_set_cancel_buttons
    )

    def __init__(self):
        self.settings = {"skip_dayz_launcher": True}
        self.logs = []
        self.errors = []
        self.messages = []
        self.cleanup_reasons = []
        self.watcher_starts = []
        self.filter_changes = 0
        self.join_preparation_cancel_button = Button()
        self._join_preparation_cancel_event = threading.Event()
        self._steam_client_safe_cancel_requested = False
        self._steam_ugc_worker_in_progress = False
        self._mod_download_backend_active = ""
        self._join_attempts = JoinAttemptTracker(log_sink=lambda _line: None)
        self.attempt = self._join_attempts.begin(
            ip="127.0.0.1", game_port=2302, query_port=27016,
            name="Boundary", skip_dayz_launcher=True,
        )

    def _join_log(self, attempt_id, event, **fields):
        self.logs.append((int(attempt_id), str(event), dict(fields)))
        return self._join_attempts.log(attempt_id, event, **fields)

    def _join_attempt_is_active(self, attempt_id):
        return self._join_attempts.matches(attempt_id)

    def _cleanup_join_attempt(self, attempt_id, reason, **_kwargs):
        self.cleanup_reasons.append(str(reason))
        return self._join_attempts.cleanup(attempt_id, reason)

    def _show_join_progress_overlay(self, message):
        self.messages.append(str(message))

    def _steam_ugc_render_status(self, message, *, error=False):
        if error:
            self.errors.append(str(message))
        else:
            self.messages.append(str(message))

    def _start_dayz_session_watch(self, *, attempt_id=0):
        self.watcher_starts.append(int(attempt_id))

    def _on_filter_changed(self, *_args, **_kwargs):
        self.filter_changes += 1

    def _set_updating(self, *_args, **_kwargs):
        return None


OBJ = SimpleNamespace(
    ip="127.0.0.1", gport=2302, name="Boundary", map_name="Chernarus",
)


def guarded_result(before_dispatch, *, submitted=True):
    if not before_dispatch():
        return SteamLaunchResult(
            False, error_kind="dispatch_guard",
            error="Join was cancelled or became stale before Steam dispatch.",
        )
    if submitted:
        return SteamLaunchResult(True, pid=4321)
    return SteamLaunchResult(False, error_kind="popen", error="synthetic failure")


def test_final_cancellation_immediately_before_dispatch_prevents_popen(monkeypatch):
    host = BoundaryHarness()
    popen_calls = []

    def launch(_win, _obj, **kwargs):
        host._join_preparation_cancel_event.set()
        result = guarded_result(kwargs["before_dispatch"])
        if result.submitted:
            popen_calls.append(True)
        return result

    monkeypatch.setattr(window_module, "launch_direct_steam_url", launch)
    result = host._launch_direct_steam_url(OBJ, attempt_id=host.attempt.attempt_id)

    assert not result.submitted
    assert popen_calls == []
    assert host.cleanup_reasons == ["cancelled or stale at Steam launch boundary"]
    assert host.join_preparation_cancel_button.label == "Close"
    assert host.join_preparation_cancel_button.sensitive is True


def test_stale_attempt_immediately_before_dispatch_prevents_popen(monkeypatch):
    host = BoundaryHarness()
    popen_calls = []

    def launch(_win, _obj, **kwargs):
        host._join_attempts.cleanup(host.attempt.attempt_id, "made stale")
        result = guarded_result(kwargs["before_dispatch"])
        if result.submitted:
            popen_calls.append(True)
        return result

    monkeypatch.setattr(window_module, "launch_direct_steam_url", launch)
    result = host._launch_direct_steam_url(OBJ, attempt_id=host.attempt.attempt_id)

    assert not result.submitted
    assert popen_calls == []
    assert host.cleanup_reasons == []
    assert host.errors == []


def test_successful_boundary_disables_cancel_before_popen_and_stays_disabled(
        monkeypatch):
    host = BoundaryHarness()
    dispatch_observations = []

    def launch(_win, _obj, **kwargs):
        assert kwargs["before_dispatch"]()
        dispatch_observations.append((
            host._join_attempts.active.phase,
            host.join_preparation_cancel_button.sensitive,
        ))
        return SteamLaunchResult(True, pid=4321)

    monkeypatch.setattr(window_module, "launch_direct_steam_url", launch)
    result = host._launch_direct_steam_url(OBJ, attempt_id=host.attempt.attempt_id)

    assert result.submitted
    assert dispatch_observations == [("launching", False)]
    assert host._join_attempts.active.phase == "watching"
    assert host.join_preparation_cancel_button.sensitive is False
    assert host.watcher_starts == [host.attempt.attempt_id]

    host._join_preparation_cancel_clicked()
    assert not host._join_preparation_cancel_event.is_set()
    assert host._join_attempts.active.phase == "watching"
    assert any(event == "post-handoff Cancel callback ignored"
               for _attempt, event, _fields in host.logs)


def test_prelaunch_download_state_restores_cancel_sensitivity():
    host = BoundaryHarness()
    host.join_preparation_cancel_button.set_sensitive(False)

    host._steam_client_set_cancel_buttons(safe_cancel=False)

    assert host.join_preparation_cancel_button.label == "Cancel"
    assert host.join_preparation_cancel_button.sensitive is True


def test_popen_failure_restores_dismissable_error_button(monkeypatch):
    host = BoundaryHarness()

    def launch(_win, _obj, **kwargs):
        assert kwargs["before_dispatch"]()
        assert host.join_preparation_cancel_button.sensitive is False
        return SteamLaunchResult(False, error_kind="popen", error="synthetic failure")

    monkeypatch.setattr(window_module, "launch_direct_steam_url", launch)
    result = host._launch_direct_steam_url(OBJ, attempt_id=host.attempt.attempt_id)

    assert not result.submitted
    assert host.join_preparation_cancel_button.label == "Close"
    assert host.join_preparation_cancel_button.visible is True
    assert host.join_preparation_cancel_button.sensitive is True
    assert not host._join_attempts.matches(host.attempt.attempt_id)
    assert any("synthetic failure" in error for error in host.errors)


class NoModBoundaryHarness(BoundaryHarness):
    def __init__(self):
        super().__init__()
        self.GLib = ImmediateGLib()
        self.events = []

    def _steam_ugc_progress_from_worker(self, _event):
        return None

    def scan_installed_mods_in_watch_folder(self, _path):
        self.events.append("scan")
        return []

    def bootstrap_launcher_state(self, **_kwargs):
        self.events.append("preset")
        return {}

    def _join_popup_show_launching(self, _attempt_id):
        self.events.append("launching-ui")
        return True


def test_no_mod_join_uses_same_non_cancellable_dispatch_boundary(monkeypatch):
    host = NoModBoundaryHarness()
    observed = []

    def launch(_win, _obj, **kwargs):
        assert kwargs["before_dispatch"]()
        observed.append((
            host._join_attempts.active.phase,
            host.join_preparation_cancel_button.sensitive,
        ))
        return SteamLaunchResult(True, pid=4321)

    monkeypatch.setattr(window_module, "launch_direct_steam_url", launch)
    join_prepare.join_prepare_and_launch(
        host, OBJ, [], "/workshop", "/prefix", "/watch", True, True,
        attempt_id=host.attempt.attempt_id,
    )

    assert host.events == ["scan", "preset", "launching-ui"]
    assert observed == [("launching", False)]
    assert host._join_attempts.active.phase == "watching"
    assert host.join_preparation_cancel_button.sensitive is False
