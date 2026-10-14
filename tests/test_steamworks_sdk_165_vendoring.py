import ctypes
import hashlib
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from dzll_launcher import steam_ugc_backend, steam_ugc_helper


def _native_root(tmp_path):
    root = tmp_path / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "steam.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_bundled_binary_is_pinned_package_resource():
    resource = files("dzll_launcher").joinpath("native/linux_x86_64/libsteam_api.so")
    data = resource.read_bytes()
    assert hashlib.sha256(data).hexdigest() == steam_ugc_helper.SDK_165_LIB_SHA256
    assert data[:4] == b"\x7fELF"
    assert data[4] == 2  # ELF64
    assert data[18:20] == b"\x3e\x00"  # x86-64
    if sys.platform == "linux" and steam_ugc_helper.platform.machine().lower() in ("x86_64", "amd64"):
        assert steam_ugc_helper.bundled_steam_api_path() == Path(str(resource)).resolve()


def test_corrupt_bundled_binary_fails_closed(monkeypatch, tmp_path):
    fake_module = tmp_path / "dzll_launcher/steam_ugc_helper.py"
    fake_module.parent.mkdir()
    fake_module.write_text("", encoding="utf-8")
    corrupt = fake_module.parent / "native/linux_x86_64/libsteam_api.so"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"corrupt")
    monkeypatch.setattr(steam_ugc_helper, "__file__", str(fake_module))
    with pytest.raises(steam_ugc_helper.CliError, match="checksum mismatch"):
        steam_ugc_helper.bundled_steam_api_path()


def test_bundled_preferred_without_steam_copy(monkeypatch, tmp_path):
    root = _native_root(tmp_path)
    monkeypatch.setattr(steam_ugc_helper, "likely_steam_roots", lambda: [root])
    paths = steam_ugc_helper.detect_steam_paths()
    assert paths.root == root
    assert paths.libsteam_api == steam_ugc_helper.bundled_steam_api_path()


def test_steam_tree_fallback_and_missing_diagnostic(monkeypatch, tmp_path):
    root = _native_root(tmp_path)
    steam_lib = root / "steamrt64/libsteam_api.so"
    steam_lib.parent.mkdir()
    steam_lib.write_bytes(b"fake")
    monkeypatch.setattr(steam_ugc_helper, "likely_steam_roots", lambda: [root])
    monkeypatch.setattr(steam_ugc_helper, "bundled_steam_api_path", lambda: None)
    assert steam_ugc_helper.detect_steam_paths().libsteam_api == steam_lib
    steam_lib.unlink()
    with pytest.raises(steam_ugc_helper.CliError, match="could not find native Steam libsteam_api.so"):
        steam_ugc_helper.detect_steam_paths()


def test_unloadable_library_reports_selected_path(monkeypatch, tmp_path):
    selected = tmp_path / "libsteam_api.so"
    selected.write_bytes(b"bad ELF")
    monkeypatch.setattr(steam_ugc_helper.ctypes, "CDLL", lambda _path: (_ for _ in ()).throw(OSError("invalid ELF")))
    paths = steam_ugc_helper.SteamPaths(tmp_path, selected, ())
    with pytest.raises(steam_ugc_helper.CliError, match="could not load Steam libsteam_api.so.*invalid ELF"):
        steam_ugc_helper.SteamUGC(paths)


def test_inventory_passes_explicit_false_boolean():
    steam = steam_ugc_helper.SteamUGC.__new__(steam_ugc_helper.SteamUGC)
    steam.ugc = object()
    calls = []

    def count(ugc, include_disabled):
        calls.append(("count", ugc, include_disabled))
        return 2

    def items(ugc, output, maximum, include_disabled):
        calls.append(("items", ugc, maximum, include_disabled))
        output[0], output[1] = 9, 5
        return 2

    steam.GetNumSubscribedItems = count
    steam.GetSubscribedItems = items
    assert steam.subscribed_item_ids() == [5, 9]
    assert [type(call[-1]) for call in calls] == [ctypes.c_bool, ctypes.c_bool]
    assert [call[-1].value for call in calls] == [False, False]


def test_inventory_bindings_declare_sdk_165_boolean(monkeypatch, tmp_path):
    class Function:
        pass

    class Library:
        def __getattr__(self, name):
            if name.startswith("SteamAPI_"):
                value = Function()
                setattr(self, name, value)
                return value
            raise AttributeError(name)

    library = Library()
    monkeypatch.setattr(steam_ugc_helper.ctypes, "CDLL", lambda _path: library)
    monkeypatch.setattr(steam_ugc_helper, "nm_symbols", lambda _path: {
        "SteamAPI_SteamUGC_v021", "SteamAPI_SteamUtils_v011", "SteamAPI_SteamUser_v023",
    })
    steam = steam_ugc_helper.SteamUGC(
        steam_ugc_helper.SteamPaths(tmp_path, tmp_path / "libsteam_api.so", ()),
    )
    assert steam.GetNumSubscribedItems.argtypes == [ctypes.c_void_p, ctypes.c_bool]
    assert steam.GetSubscribedItems.argtypes[-1] is ctypes.c_bool


def test_bundled_native_provenance_keeps_identity_gates(monkeypatch, tmp_path):
    root = _native_root(tmp_path)
    bundled = steam_ugc_helper.bundled_steam_api_path()
    assert bundled is not None
    event = {
        "ok": True, "appid": 221100, "steam_root": str(root),
        "lib": str(bundled), "flatpak_environment": False,
    }
    terminal = {"logged_on": True, "steam_id": 76561198000000001, "identity_stable": True}
    verify = steam_ugc_backend._authenticated_native_helper_result
    assert verify(event, terminal, appid=221100)[0]
    assert not verify({**event, "appid": 1}, terminal, appid=221100)[0]
    assert not verify({**event, "flatpak_environment": True}, terminal, appid=221100)[0]
    assert not verify(event, {**terminal, "logged_on": False}, appid=221100)[0]
    assert not verify(event, {**terminal, "steam_id": 0}, appid=221100)[0]
    assert not verify(event, {**terminal, "identity_stable": False}, appid=221100)[0]
    assert not verify({**event, "lib": str(tmp_path / "other/libsteam_api.so")}, terminal, appid=221100)[0]
    root.joinpath("steam.sh").unlink()
    assert not verify(event, terminal, appid=221100)[0]


def test_flatpak_tty_progress_alone_does_not_identify_flatpak_steam(monkeypatch):
    monkeypatch.setattr(steam_ugc_helper.os, "environ", {"FLATPAK_TTY_PROGRESS": "1"})
    assert not steam_ugc_helper.flatpak_steam_environment()


def test_unrelated_environment_value_does_not_identify_flatpak_steam(
        monkeypatch, tmp_path):
    monkeypatch.setattr(steam_ugc_helper.os, "environ", {
        "FLATPAK_TTY_PROGRESS": "1",
        "UNRELATED_SETTING": "prefix-com.valvesoftware.steam-suffix",
    })
    assert not steam_ugc_helper.flatpak_steam_environment()
    root = _native_root(tmp_path)
    bundled = steam_ugc_helper.bundled_steam_api_path()
    event = {
        "ok": True, "appid": 221100, "steam_root": str(root),
        "lib": str(bundled),
        "flatpak_environment": steam_ugc_helper.flatpak_steam_environment(),
    }
    terminal = {"logged_on": True, "steam_id": 76561198000000001,
                "identity_stable": True}
    assert steam_ugc_backend._authenticated_native_helper_result(
        event, terminal, appid=221100,
    )[0]


def test_actual_flatpak_steam_identity_remains_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(steam_ugc_helper.os, "environ", {
        "FLATPAK_TTY_PROGRESS": "1", "FLATPAK_ID": "com.valvesoftware.Steam",
    })
    assert steam_ugc_helper.flatpak_steam_environment()
    root = _native_root(tmp_path)
    bundled = steam_ugc_helper.bundled_steam_api_path()
    event = {
        "ok": True, "appid": 221100, "steam_root": str(root),
        "lib": str(bundled), "flatpak_environment": True,
    }
    terminal = {"logged_on": True, "steam_id": 76561198000000001, "identity_stable": True}
    assert not steam_ugc_backend._authenticated_native_helper_result(
        event, terminal, appid=221100,
    )[0]


@pytest.mark.parametrize("flatpak_id", [None, "", "   ", "org.example.app",
                                         "prefix-com.valvesoftware.steam"])
def test_missing_empty_or_malformed_flatpak_identity_is_not_steam(
        monkeypatch, flatpak_id):
    env = {"FLATPAK_TTY_PROGRESS": "1"}
    if flatpak_id is not None:
        env["FLATPAK_ID"] = flatpak_id
    monkeypatch.setattr(steam_ugc_helper.os, "environ", env)
    assert not steam_ugc_helper.flatpak_steam_environment()


def test_architecture_specific_package_metadata():
    repo = Path(__file__).resolve().parents[1]
    assert "native/linux_x86_64/libsteam_api.so" in (repo / "pyproject.toml").read_text()
    assert "native/linux_x86_64/libsteam_api.so" in (repo / "MANIFEST.in").read_text()
    assert 'return "py3", "none", wheel_platform' in (repo / "setup.py").read_text()
    spec = (repo / "dzll_launcher.spec").read_text()
    assert "BuildArch:      x86_64" in spec
    assert "%{python3_sitelib}/dzll_launcher" in spec
    assert "Architecture: amd64" in (repo / "debian/control").read_text()
    arch_recipe = (repo / "packaging/arch/PKGBUILD.template").read_text()
    assert "arch=('x86_64')" in arch_recipe
    assert "v0.4.2-beta.tar.gz" in arch_recipe
    assert 'cd "DayZ-Linux-Launcher-0.4.2-beta"' in arch_recipe
    assert "REPLACE_WITH_V0_4_2_BETA_TAG_ARCHIVE_SHA256" in arch_recipe


def test_built_wheel_installs_pinned_resource(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(repo / "src", source / "src")
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE",
                 "requirements.txt", "com.bdingle.dzll.desktop"):
        shutil.copy2(repo / name, source / name)
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(dist), str(source)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    wheel = next(dist.glob("*.whl"))
    assert wheel.name.endswith("-py3-none-linux_x86_64.whl")
    installed = tmp_path / "installed"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(installed),
         str(wheel)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    library = installed / "dzll_launcher/native/linux_x86_64/libsteam_api.so"
    assert hashlib.sha256(library.read_bytes()).hexdigest() == steam_ugc_helper.SDK_165_LIB_SHA256
