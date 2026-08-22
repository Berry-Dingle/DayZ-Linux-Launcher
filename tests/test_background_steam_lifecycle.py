import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from dzll_launcher import steam_native, steam_ugc_backend
from dzll_launcher import window as window_module
from dzll_launcher.background_prepare import (
    BackgroundConsentResult,
    BackgroundConsentStatus,
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    SingleServerBackgroundPreparation,
)


class ConsentHarness:
    _ensure_join_steam_start_consent = (
        window_module.DZLLWindow._ensure_join_steam_start_consent
    )

    def __init__(self, *, automatic=True, decision=(True, False), start_ok=True):
        self.settings = {"start_steam_on_join": automatic}
        self.decision = decision
        self.start_ok = start_ok
        self.started = 0
        self._join_steam_start_allowed = False
        self.updates = []

    def _join_log(self, *_args, **_kwargs):
        return None

    def _show_start_steam_join_consent_blocking(self, *, caller="join"):
        self.consent_caller = caller
        return self.decision

    def _start_native_steam_for_join(self):
        self.started += 1
        return (True, "") if self.start_ok else (False, "start exploded")

    def _set_updating(self, _active, text):
        self.updates.append(text)

    def _sync_start_steam_on_join_setting_widget(self):
        return None


def test_caller_owned_auto_start_is_once_and_exposes_start_then_wait(monkeypatch):
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: False)
    harness = ConsentHarness(automatic=True)
    progress = []
    result = harness._ensure_join_steam_start_consent(
        0, caller="background", progress_cb=progress.append,
    )
    assert result.status is BackgroundConsentStatus.ALLOWED
    assert result.startup_state == "start_submitted"
    assert result.steam_start_submitted
    assert not result.backend_may_launch
    assert harness.started == 1
    assert progress == ["Starting Steam…", "Waiting for Steam…"]


def test_background_manual_consent_accept_and_decline_are_caller_specific(monkeypatch):
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: False)
    monkeypatch.setattr(window_module, "save_settings", lambda _settings: None)
    accepted = ConsentHarness(automatic=False, decision=(True, False))
    accepted_result = accepted._ensure_join_steam_start_consent(
        0, caller="background",
    )
    assert accepted.consent_caller == "background"
    assert accepted_result.steam_start_submitted
    assert accepted_result.startup_state == "start_submitted"
    assert accepted.started == 1

    declined = ConsentHarness(automatic=False, decision=(False, False))
    declined_result = declined._ensure_join_steam_start_consent(
        0, caller="background",
    )
    assert declined.consent_caller == "background"
    assert declined_result.status is BackgroundConsentStatus.DECLINED
    assert "Join" not in declined_result.error
    assert declined.started == 0


def test_start_failure_is_error_not_cancelled(monkeypatch):
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: False)
    harness = ConsentHarness(automatic=True, start_ok=False)
    result = harness._ensure_join_steam_start_consent(
        0, caller="background",
    )
    assert result.status is BackgroundConsentStatus.ERROR
    assert "start exploded" in result.error
    assert harness.started == 1
    assert not harness._join_steam_start_allowed


def test_start_exception_is_explicit_error(monkeypatch):
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: False)
    harness = ConsentHarness(automatic=True)

    def explode():
        harness.started += 1
        raise RuntimeError("launcher exception")

    harness._start_native_steam_for_join = explode
    result = harness._ensure_join_steam_start_consent(
        0, caller="background",
    )
    assert result.status is BackgroundConsentStatus.ERROR
    assert result.startup_state == "error"
    assert "launcher exception" in result.error
    assert harness.started == 1
    assert not harness._join_steam_start_allowed


def test_strict_running_detection_excludes_runtime_and_webhelper():
    assert steam_native._is_native_steam_client_cmdline(
        "/home/user/.local/share/Steam/ubuntu12_32/steam -silent"
    )
    assert steam_native._is_native_steam_client_cmdline("/usr/bin/steam -silent")
    assert not steam_native._is_native_steam_client_cmdline(
        "/usr/bin/steam-runtime-steam-remote"
    )
    assert not steam_native._is_native_steam_client_cmdline(
        "/home/user/.local/share/Steam/steamrt64/steam-runtime-launcher-service"
    )
    assert not steam_native._is_native_steam_client_cmdline(
        "/home/user/.local/share/Steam/steamwebhelper"
    )
    assert not steam_native._is_native_steam_client_cmdline("/usr/bin/steamcmd")


def test_already_running_returns_confirmed_state_without_launch(monkeypatch):
    monkeypatch.setattr(window_module, "is_native_steam_client_running", lambda: True)
    harness = ConsentHarness(automatic=True)
    result = harness._ensure_join_steam_start_consent(
        0, caller="background",
    )
    assert result.status is BackgroundConsentStatus.ALLOWED
    assert result.startup_state == "already_running"
    assert result.steam_was_running
    assert not result.steam_start_submitted
    assert harness.started == 0


def test_flagless_allowed_result_fails_before_helper_or_engine(monkeypatch):
    host = SimpleNamespace(
        _steamcmd_cancel_event=threading.Event(),
        _join_steam_start_allowed=False,
        _join_attempts=SimpleNamespace(active=None),
    )
    monkeypatch.setattr(
        "dzll_launcher.background_prepare.prepare_required_mods",
        lambda *_args, **_kwargs: pytest.fail(
            "helper/shared engine must not start for unconfirmed allowed result"
        ),
    )
    outcome = SingleServerBackgroundPreparation(host).run(
        BackgroundServerPreparationSnapshot(
            "127.0.0.1", 2302, 27016, "Server", ((7, "Mod"),),
        ),
        BackgroundPreparationRuntime(
            "/workshop", "/steamcmd", "", False, False, True,
            "steam_client", True, False,
        ),
        ensure_steam_consent=lambda: BackgroundConsentResult(
            BackgroundConsentStatus.ALLOWED,
        ),
    )
    assert outcome.reason == "steam_start_not_submitted"
    assert "without confirming" in outcome.error


def test_wait_only_preflight_never_launches_across_false_process_probes(monkeypatch):
    monkeypatch.setattr(steam_native, "is_flatpak_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "is_native_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "resolve_native_steam_cmd", lambda: "/native/steam")
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("wait-only readiness must not launch Steam"),
    )
    attempts = []

    def helper(*_args, **_kwargs):
        attempts.append(1)
        return (len(attempts) >= 3, 0)

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    monkeypatch.setattr(steam_ugc_backend, "UGC_PREFLIGHT_RETRY_S", 0.0)
    assert steam_ugc_backend.wait_for_ugc_ready(
        [7], launch_policy="wait_only", timeout_s=1.0,
    )
    assert len(attempts) == 3


def test_wait_only_readiness_cancel_is_prompt_and_launches_nothing(monkeypatch):
    monkeypatch.setattr(steam_native, "is_flatpak_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "is_native_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "resolve_native_steam_cmd", lambda: "/native/steam")
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("cancelled wait-only readiness must not launch"),
    )
    cancel = threading.Event()

    def helper(*_args, **_kwargs):
        cancel.set()
        return False, 1

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    started = time.monotonic()
    assert not steam_ugc_backend.wait_for_ugc_ready(
        [7], cancel_event=cancel, launch_policy="wait_only", timeout_s=30.0,
    )
    assert time.monotonic() - started < 0.2


def test_direct_backend_launch_policy_still_launches(monkeypatch):
    monkeypatch.setattr(steam_native, "is_flatpak_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "is_native_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "resolve_native_steam_cmd", lambda: "/native/steam")
    launches = []
    monkeypatch.setattr(
        steam_ugc_backend.subprocess, "Popen",
        lambda args, **_kwargs: launches.append(tuple(args)) or SimpleNamespace(),
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", lambda *_a, **_k: (True, 0),
    )
    assert steam_ugc_backend.wait_for_ugc_ready(
        [7], launch_policy="backend_allowed", timeout_s=1.0,
    )
    assert launches == [("/native/steam", "-silent")]


class UnreapableProcess:
    pid = 4242

    def poll(self):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None

    def wait(self, timeout):
        raise subprocess.TimeoutExpired("helper", timeout)


def test_helper_failure_to_reap_is_observable():
    events = []
    process = UnreapableProcess()
    with pytest.raises(steam_ugc_backend.UGCHelperReapError) as raised:
        steam_ugc_backend._stop_helper_process(
            process, command="state", progress_cb=events.append,
        )
    assert raised.value.helper_process_may_be_alive
    assert not raised.value.helper_process_confirmed_dead
    assert raised.value._recovery_process is process
    assert any(
        event.get("message") == "[Steam UGC] Helper failure-to-reap"
        and event.get("helper_pid") == 4242
        for event in events
    )
