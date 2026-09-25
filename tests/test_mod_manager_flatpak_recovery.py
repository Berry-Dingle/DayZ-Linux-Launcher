import inspect
import threading
from types import SimpleNamespace

from dzll_launcher import mod_manager_recovery, settings_ui, steam_native
from dzll_launcher.settings_ui import SettingsUI
from dzll_launcher.steam_native import SteamClientState, SteamRuntimeEvidence


def test_flatpak_only_shutdown_targets_verified_main_client(monkeypatch):
    commands = []
    runtime = SteamRuntimeEvidence(
        SteamClientState.FLATPAK,
        flatpak_process_running=True,
        flatpak_client_running=True,
    )
    monkeypatch.setattr(
        mod_manager_recovery.shutil, "which", lambda _name: "/usr/bin/flatpak",
    )
    monkeypatch.setattr(
        mod_manager_recovery.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(list(command)),
    )
    assert mod_manager_recovery.request_graceful_flatpak_steam_shutdown(runtime) == []
    assert commands == [[
        "/usr/bin/flatpak", "run", "com.valvesoftware.Steam", "-shutdown",
    ]]


def test_residual_only_flatpak_evidence_never_runs_shutdown_command(monkeypatch):
    runtime = SteamRuntimeEvidence(
        SteamClientState.FLATPAK,
        flatpak_process_running=True,
        flatpak_client_running=False,
    )
    monkeypatch.setattr(
        mod_manager_recovery.shutil,
        "which",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    monkeypatch.setattr(
        mod_manager_recovery.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not run")
        ),
    )
    assert mod_manager_recovery.request_graceful_flatpak_steam_shutdown(runtime) == []


def test_flatpak_wait_uses_process_truth_and_does_not_reissue(monkeypatch):
    states = iter((
        SteamRuntimeEvidence(
            SteamClientState.FLATPAK,
            flatpak_process_running=True,
            flatpak_client_running=False,
        ),
        SteamRuntimeEvidence(SteamClientState.OFFLINE),
    ))
    monkeypatch.setattr(
        mod_manager_recovery, "resolve_steam_runtime_state", lambda: next(states),
    )
    monkeypatch.setattr(mod_manager_recovery, "STEAM_RECOVERY_POLL_S", 0)
    assert mod_manager_recovery.wait_for_flatpak_steam_exit(
        threading.Event(), timeout_s=2,
    ) == (True, "")


def test_flatpak_wait_timeout_is_actionable(monkeypatch):
    monkeypatch.setattr(
        mod_manager_recovery,
        "resolve_steam_runtime_state",
        lambda: SteamRuntimeEvidence(
            SteamClientState.FLATPAK,
            flatpak_process_running=True,
            flatpak_client_running=True,
        ),
    )
    ok, error = mod_manager_recovery.wait_for_flatpak_steam_exit(
        threading.Event(), timeout_s=0,
    )
    assert not ok
    assert error == "Flatpak Steam is still running. Close it, then Retry."


def test_native_launch_uses_absolute_system_candidate_without_path_lookup():
    source = "\n".join((
        inspect.getsource(steam_native.resolve_native_steam_cmd),
        inspect.getsource(steam_native._valid_native_steam_cmd),
    ))
    assert steam_native._NATIVE_STEAM_SYSTEM_CANDIDATES == (
        steam_native.Path("/usr/bin/steam"),
        steam_native.Path("/usr/games/steam"),
    )
    assert "shutil.which" not in source
    assert "steam://" in source


def test_native_launch_sanitizes_flatpak_environment_and_records_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(
        steam_native, "resolve_native_steam_cmd", lambda: "/native/steam",
    )
    monkeypatch.setenv("FLATPAK_ID", "com.valvesoftware.Steam")
    monkeypatch.setenv(
        "XDG_DATA_HOME", "/home/user/.var/app/com.valvesoftware.Steam/data",
    )
    monkeypatch.setenv("SAFE_NATIVE_VALUE", "preserved")
    monkeypatch.setenv("PATH", "/app/bin:/usr/bin:/home/user/.var/app/wrappers")

    def popen(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        return SimpleNamespace(pid=8080)

    monkeypatch.setattr(steam_native.subprocess, "Popen", popen)
    result = steam_native.launch_native_steam_silent_tracked()
    assert result.ok is True
    assert result.executable == "/native/steam"
    assert result.pid == 8080
    assert calls[0][0] == ["/native/steam", "-silent"]
    assert calls[0][1]["stdin"] is steam_native.subprocess.DEVNULL
    assert calls[0][1]["stdout"] is steam_native.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is steam_native.subprocess.DEVNULL
    environment = calls[0][1]["env"]
    assert "FLATPAK_ID" not in environment
    assert "XDG_DATA_HOME" not in environment
    assert environment["SAFE_NATIVE_VALUE"] == "preserved"
    assert environment["PATH"] == "/usr/bin"


def test_native_readiness_requires_stable_matching_authority(monkeypatch):
    runtime = SteamRuntimeEvidence(
        SteamClientState.NATIVE, native_client_running=True,
    )
    probes = []
    monkeypatch.setattr(
        mod_manager_recovery, "resolve_steam_runtime_state", lambda: runtime,
    )
    monkeypatch.setattr(
        mod_manager_recovery,
        "probe_native_mod_manager_readiness",
        lambda **_kwargs: probes.append(True)
        or SimpleNamespace(valid=True, steam_id=76561198000000001),
    )
    monkeypatch.setattr(mod_manager_recovery, "STEAM_READY_SETTLE_S", 0)
    assert mod_manager_recovery.wait_for_native_mod_manager_ready(
        threading.Event(), timeout_s=5,
    ) == (True, "")
    assert len(probes) == 2


def test_mixed_recheck_path_has_no_steam_control_calls():
    source = "\n".join((
        inspect.getsource(SettingsUI._start_mixed_mod_manager_recheck),
        inspect.getsource(SettingsUI._complete_mixed_mod_manager_recheck),
    ))
    forbidden = (
        "request_graceful", "launch_native_steam", "subprocess",
        "-shutdown", "flatpak run", "_show_flatpak_mod_manager_recovery",
        "_start_flatpak_mod_manager_recovery",
    )
    assert not any(token in source for token in forbidden)
    assert not hasattr(SettingsUI, "_transition_mixed_recheck_to_flatpak_only")
    assert not hasattr(SettingsUI, "_bind_mod_manager_gate_primary")


def test_dual_client_automation_symbols_are_removed():
    assert not hasattr(mod_manager_recovery, "request_graceful_steam_shutdown")
    assert not hasattr(mod_manager_recovery, "wait_for_all_steam_processes_exit")
    assert not hasattr(steam_native, "resolve_native_steam_process_context")


def test_normal_entry_and_recovery_share_native_ready_predicate():
    assert (
        settings_ui.native_steam_ready_for_mod_manager
        is mod_manager_recovery.native_steam_ready_for_mod_manager
    )
