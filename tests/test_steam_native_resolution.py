from pathlib import Path

import pytest

from dzll_launcher import steam_native, steam_ugc_backend
from dzll_launcher.steam_native import SteamClientState, SteamRuntimeEvidence


def _write_executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _native_root(path: Path) -> tuple[Path, Path]:
    (path / "steamapps").mkdir(parents=True)
    launcher = _write_executable(path / "steam.sh")
    client = _write_executable(path / "ubuntu12_32/steam")
    return launcher, client


def _native_runtime(client: Path, *, flatpak: bool = False) -> SteamRuntimeEvidence:
    return SteamRuntimeEvidence(
        SteamClientState.UNKNOWN if flatpak else SteamClientState.NATIVE,
        native_client_running=True,
        flatpak_process_running=flatpak,
        native_client_executables=(str(client),),
    )


def _resolve_only(monkeypatch, candidates, *, root=None, runtime=None):
    monkeypatch.setattr(
        steam_native, "_NATIVE_STEAM_SYSTEM_CANDIDATES", tuple(candidates),
    )
    monkeypatch.setattr(
        steam_native, "resolve_native_steam_root", lambda: root,
    )
    monkeypatch.setattr(
        steam_native,
        "resolve_steam_runtime_state",
        lambda: runtime or SteamRuntimeEvidence(SteamClientState.OFFLINE),
    )
    return steam_native.resolve_native_steam_cmd()


def test_ubuntu_package_launcher_allows_harmless_embedded_xdg_open(
        monkeypatch, tmp_path):
    launcher = _write_executable(
        tmp_path / "usr/games/steam",
        "#!/bin/sh\n"
        "sed -e '1i#!/usr/bin/env xdg-open' steam.desktop\n"
        "exec /native/root/steam.sh \"$@\"\n",
    )
    assert _resolve_only(monkeypatch, [launcher]) == str(launcher.resolve())


@pytest.mark.parametrize(
    "body",
    [
        "#!/bin/sh\nexec flatpak run com.valvesoftware.Steam \"$@\"\n",
        "#!/bin/sh\nexec /opt/com.valvesoftware.Steam/steam \"$@\"\n",
        "#!/bin/sh\nexec xdg-open 'steam://open/main'\n",
        "#!/bin/sh\nexec gtk-launch steam.desktop\n",
        "#!/bin/sh\nexec steamcmd \"$@\"\n",
    ],
    ids=["flatpak", "flatpak-app-id", "uri-forwarder", "desktop-handler", "steamcmd"],
)
def test_non_native_system_wrappers_are_rejected(
        monkeypatch, tmp_path, body):
    launcher = _write_executable(tmp_path / "usr/bin/steam", body)
    assert _resolve_only(monkeypatch, [launcher]) is None


def test_fedora_system_launcher_remains_valid(monkeypatch, tmp_path):
    launcher = _write_executable(
        tmp_path / "usr/bin/steam",
        "#!/bin/sh\nexec /opt/steam/steam.sh \"$@\"\n",
    )
    assert _resolve_only(monkeypatch, [launcher]) == str(launcher.resolve())


def test_runtime_evidence_retains_native_client_executable(tmp_path):
    proc_root = tmp_path / "proc"
    process = proc_root / "123"
    process.mkdir(parents=True)
    client = _write_executable(tmp_path / "Steam/ubuntu12_32/steam")
    (process / "cmdline").write_bytes(f"{client}\0-silent\0".encode())
    (process / "exe").symlink_to(client)

    runtime = steam_native.resolve_steam_runtime_state(proc_root)

    assert runtime.state is SteamClientState.NATIVE
    assert runtime.native_client_executables == (str(client),)


@pytest.mark.parametrize("relative", [".local/share/Steam", ".steam/steam"])
def test_valid_native_root_steam_sh_fallback(
        monkeypatch, tmp_path, relative):
    root = tmp_path / relative
    launcher, client = _native_root(root)
    assert _resolve_only(
        monkeypatch, [], root=root.resolve(), runtime=_native_runtime(client),
    ) == str(launcher.resolve())


def test_debian_control_symlink_resolves_native_root_fallback(
        monkeypatch, tmp_path):
    installation = tmp_path / ".steam/debian-installation"
    launcher, client = _native_root(installation)
    control = tmp_path / ".steam/steam"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.symlink_to(installation, target_is_directory=True)
    monkeypatch.setattr(
        steam_native, "_native_steam_root_candidates", lambda: (control,),
    )
    monkeypatch.setattr(steam_native, "_NATIVE_STEAM_SYSTEM_CANDIDATES", ())
    monkeypatch.setattr(
        steam_native, "resolve_steam_runtime_state",
        lambda: _native_runtime(client),
    )
    assert steam_native.resolve_native_steam_root() == installation.resolve()
    assert steam_native.resolve_native_steam_cmd() == str(launcher.resolve())


def test_root_steam_sh_symlink_cannot_escape_trusted_root(
        monkeypatch, tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    client = _write_executable(root / "ubuntu12_32/steam")
    outside = _write_executable(tmp_path / "outside/steam.sh")
    (root / "steam.sh").symlink_to(outside)
    assert _resolve_only(
        monkeypatch, [], root=root.resolve(), runtime=_native_runtime(client),
    ) is None


def test_stale_first_root_does_not_mask_later_valid_root(
        monkeypatch, tmp_path):
    stale = tmp_path / ".local/share/Steam"
    stale.mkdir(parents=True)
    valid = tmp_path / ".steam/steam"
    _native_root(valid)
    monkeypatch.setattr(
        steam_native, "_native_steam_root_candidates", lambda: (stale, valid),
    )
    assert steam_native.resolve_native_steam_root() == valid.resolve()


def test_secondary_dayz_library_does_not_influence_command_resolution(
        monkeypatch, tmp_path):
    root = tmp_path / "Steam"
    launcher, client = _native_root(root)
    secondary = tmp_path / "Secondary Library"
    (secondary / "steamapps").mkdir(parents=True)
    (secondary / "steamapps/appmanifest_221100.acf").write_text(
        '"AppState"\n{\n\t"installdir"\t"DayZ"\n}\n', encoding="utf-8",
    )
    (root / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"1"\n\t{\n'
        f'\t\t"path"\t"{secondary}"\n\t}}\n}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        steam_native, "_native_steam_root_candidates", lambda: (root,),
    )
    monkeypatch.setattr(steam_native, "_NATIVE_STEAM_SYSTEM_CANDIDATES", ())
    monkeypatch.setattr(
        steam_native, "resolve_steam_runtime_state",
        lambda: _native_runtime(client),
    )
    assert steam_native.dayz_steam_library() == secondary.resolve()
    assert steam_native.resolve_native_steam_cmd() == str(launcher.resolve())


def test_running_native_ugc_does_not_require_launcher(
        monkeypatch):
    monkeypatch.setattr(steam_native, "is_flatpak_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "is_native_steam_running", lambda: True)
    monkeypatch.setattr(
        steam_native,
        "resolve_native_steam_cmd",
        lambda: pytest.fail("running native Steam must not require a launcher"),
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", lambda *_a, **_k: (True, 0),
    )
    assert steam_ugc_backend._run_ugc_native_steam_preflight(
        [123], appid=221100, timeout_s=1,
    )


def test_offline_backend_start_still_requires_valid_launcher(monkeypatch):
    events = []
    monkeypatch.setattr(steam_native, "is_flatpak_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "is_native_steam_running", lambda: False)
    monkeypatch.setattr(steam_native, "resolve_native_steam_cmd", lambda: None)
    assert not steam_ugc_backend._run_ugc_native_steam_preflight(
        [123], appid=221100, launch_policy="backend_allowed",
        progress_cb=events.append, timeout_s=1,
    )
    assert events[-1]["reason"] == "native_steam_not_found"


def test_mixed_runtime_never_bypasses_native_ugc_authority(monkeypatch):
    runtime = SteamRuntimeEvidence(
        SteamClientState.UNKNOWN,
        native_client_running=True,
        flatpak_process_running=True,
    )
    monkeypatch.setattr(
        steam_native, "resolve_steam_runtime_state", lambda: runtime,
    )
    monkeypatch.setattr(
        steam_native,
        "resolve_native_steam_cmd",
        lambda: pytest.fail("mixed state must stop before launcher resolution"),
    )
    monkeypatch.setattr(
        steam_ugc_backend,
        "_run_helper_json_lines",
        lambda *_a, **_k: pytest.fail("mixed state must not reach SteamAPI"),
    )
    assert not steam_ugc_backend._run_ugc_native_steam_preflight(
        [123], appid=221100, timeout_s=1,
    )
