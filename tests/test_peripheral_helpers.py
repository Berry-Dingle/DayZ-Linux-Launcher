"""Focused tests for helpers extracted from window.py in Stage 3."""

import json
import os
import pathlib
import urllib.error
from types import SimpleNamespace

import pytest

from dzll_launcher import desktop_integration, steam_global_players, storage, update_check
from dzll_launcher.config import APP_VERSION, DEAD_MAX_FAILS, GITHUB_LATEST_API, RELEASES_URL, STEAM_CURRENT_PLAYERS_URL
from dzll_launcher.steam_global_players import SteamGlobalPlayersPoller

NOW = 1_800_000_000


# ---------------------------------------------------------------- dead cache


def test_prune_drops_only_entries_expired_over_a_week_ago():
    dead = {
        "old": {"dead_until": NOW - 7 * 86400 - 1},
        "edge": {"dead_until": NOW - 7 * 86400},
        "active": {"dead_until": NOW + 60},
        "never": {"dead_until": 0},
    }

    assert storage.prune_expired_dead_entries(dead, now=NOW) is True
    assert sorted(dead) == ["active", "edge", "never"]
    assert storage.prune_expired_dead_entries(dead, now=NOW) is False


def test_prune_keeps_original_failure_on_malformed_entry():
    with pytest.raises(ValueError):
        storage.prune_expired_dead_entries({"bad": {"dead_until": "soon"}}, now=NOW)


def test_clamp_drops_stale_failures_and_caps_fail_count_in_place():
    entry = {"fail_count": DEAD_MAX_FAILS + 10, "last_fail": NOW - 86400}
    dead = {
        "stale": {"fail_count": 1, "last_fail": NOW - 30 * 86400 - 1},
        "capped": entry,
        "fine": {"fail_count": DEAD_MAX_FAILS, "last_fail": NOW},
        "malformed": {"fail_count": "x", "last_fail": "y"},
    }

    assert storage.clamp_dead_cache_entries(dead, now=NOW) is True
    assert sorted(dead) == ["capped", "fine", "malformed"]
    assert dead["capped"] is entry and entry["fail_count"] == DEAD_MAX_FAILS
    assert storage.clamp_dead_cache_entries(dead, now=NOW) is False
    assert storage.clamp_dead_cache_entries({"a": {"fail_count": 4}}, now=NOW, max_fails=3) is True


# ---------------------------------------------------------------- update check


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()


@pytest.fixture
def network(monkeypatch):
    state = {"result": {"tag_name": " v9.9.9 ", "html_url": " https://example.invalid/r "}, "requests": []}
    for name in ("DZLL_TEST_UPDATE_TAG", "DZLL_TEST_UPDATE_URL", "DZLL_FORCE_LATEST_TAG"):
        monkeypatch.delenv(name, raising=False)

    def urlopen(request, timeout=None):
        state["requests"].append((request.full_url, request.get_header("User-agent"), timeout))
        if isinstance(state["result"], Exception):
            raise state["result"]
        return Response(state["result"])

    monkeypatch.setattr(update_check.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(update_check.time, "time", lambda: NOW + 0.5)
    return state


def startup(settings):
    saves = []
    info = update_check.startup_update_info(settings, save_settings=lambda s: saves.append(dict(s)))
    return info, saves


def test_startup_check_fetches_records_and_reports_release(network):
    settings = {}

    info, saves = startup(settings)

    assert info == {"tag": "v9.9.9", "url": "https://example.invalid/r"}
    assert network["requests"] == [(GITHUB_LATEST_API, "DZLL", 3.0)]
    assert saves == [{"last_update_check_ts": NOW, "latest_release_tag": "v9.9.9",
                      "latest_release_url": "https://example.invalid/r"}]


def test_startup_check_uses_cached_release_within_a_day(network):
    info, saves = startup({"last_update_check_ts": NOW - 100, "latest_release_tag": "v5"})

    assert info == {"tag": "v5", "url": RELEASES_URL}
    assert network["requests"] == [] and saves == []
    assert startup({"last_update_check_ts": NOW - 100}) == (None, [])


def test_startup_check_failure_records_empty_release_and_leaves_info(network):
    network["result"] = urllib.error.URLError("offline")

    info, saves = startup({"last_update_check_ts": NOW - 90_000})

    assert info is None
    assert saves == [{"last_update_check_ts": NOW, "latest_release_tag": "", "latest_release_url": ""}]


def test_startup_check_disabled_and_environment_overrides(network, monkeypatch):
    assert startup({"auto_check_updates": False}) == (None, [])
    monkeypatch.setenv("DZLL_FORCE_LATEST_TAG", " v7 ")
    assert startup({}) == ({"tag": "v7", "url": RELEASES_URL}, [])
    monkeypatch.setenv("DZLL_TEST_UPDATE_TAG", "v8")
    assert startup({}) == ({"tag": "v8", "url": RELEASES_URL}, [])
    assert network["requests"] == []


def manual():
    saves = []
    result = update_check.manual_update_check({}, save_settings=lambda s: saves.append(dict(s)))
    return result, saves


def test_manual_check_states(network):
    assert manual()[0] == ("available", "v9.9.9", "https://example.invalid/r")

    network["result"] = {"tag_name": APP_VERSION}
    assert manual()[0] == ("latest", "", "")

    network["result"] = urllib.error.HTTPError(GITHUB_LATEST_API, 404, "missing", {}, None)
    assert manual() == (("no_stable_release", "", ""), [])

    network["result"] = urllib.error.HTTPError(GITHUB_LATEST_API, 500, "error", {}, None)
    assert manual() == (("failed", "", ""), [])

    network["result"] = b"{not json"
    assert manual() == (("failed", "", ""), [])


def test_manual_check_records_successful_lookup(network):
    _result, saves = manual()

    assert saves == [{"last_update_check_ts": NOW, "latest_release_tag": "v9.9.9",
                      "latest_release_url": "https://example.invalid/r"}]


# ---------------------------------------------------------------- global players


def test_fetch_global_player_count(monkeypatch):
    requests = []
    payloads = iter([{"response": {"player_count": "12345"}}, {"response": {}}, b"bad"])

    def urlopen(request, timeout=None):
        requests.append((request.full_url, request.get_header("User-agent"), timeout))
        return Response(next(payloads))

    monkeypatch.setattr(steam_global_players.urllib.request, "urlopen", urlopen)

    assert steam_global_players.fetch_steam_global_player_count() == 12345
    assert steam_global_players.fetch_steam_global_player_count() is None
    assert steam_global_players.fetch_steam_global_player_count() is None
    assert requests[0] == (STEAM_CURRENT_PLAYERS_URL, "DZLL", 3.0)


def poller(settings, *, shut=False, fetched=4321, submit=None):
    events = []

    def run_now(work):
        events.append("submit")
        work()

    instance = SteamGlobalPlayersPoller(
        settings=lambda: settings,
        is_shut_down=lambda: shut,
        submit=submit or run_now,
        schedule_idle=lambda fn, *args: events.append(("idle", fn(*args))),
        on_updated=lambda: events.append("titlebar"),
        fetch=lambda: fetched,
    )
    return instance, events


def test_poller_fetches_on_worker_and_applies_on_idle():
    instance, events = poller({"show_counts_in_title_bar": True})

    assert instance.tick() is True
    assert instance.count == 4321
    assert events == ["submit", "titlebar", ("idle", False)]


def test_poller_keeps_last_count_for_invalid_results():
    instance, _events = poller({"show_counts_in_title_bar": True}, fetched=-1)
    instance.count = 99

    instance.tick()

    assert instance.count == 99


@pytest.mark.parametrize("settings", [
    {},
    {"show_counts_in_title_bar": True, "show_counts_global_players": False},
])
def test_poller_skips_fetch_when_hidden(settings):
    instance, events = poller(settings)

    assert instance.tick() is True
    assert events == []


def test_poller_stops_after_shutdown_and_contains_submit_failure():
    instance, events = poller({"show_counts_in_title_bar": True}, shut=True)
    assert instance.tick() is False
    assert events == []

    def broken(_work):
        raise RuntimeError("executor gone")

    instance, _events = poller({"show_counts_in_title_bar": True}, submit=broken)
    assert instance.tick() is True


# ---------------------------------------------------------------- desktop integration


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"icon")
    runs = []
    real_exists = pathlib.Path.exists
    state = {"system": False}
    monkeypatch.setattr(desktop_integration.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        desktop_integration.Path, "exists",
        lambda self: state["system"] if str(self).startswith("/usr/share/applications/") else real_exists(self),
    )
    monkeypatch.setattr(desktop_integration.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    return SimpleNamespace(home=home, icon=icon, runs=runs, state=state)


def integrate(sandbox):
    desktop_integration.ensure_user_desktop_integration(
        app_id="com.bdingle.dzll", app_name="DayZ Linux Launcher", icon_src_path=str(sandbox.icon),
    )


def desktop_file(sandbox):
    return sandbox.home / ".local/share/applications/com.bdingle.dzll.desktop"


def test_desktop_integration_installs_entry_and_canonical_icon(sandbox):
    integrate(sandbox)

    text = desktop_file(sandbox).read_text()
    package_dir = pathlib.Path(desktop_integration.__file__).resolve().parent
    assert "Exec=" in text and "-m dzll_launcher" in text
    assert f"Path={package_dir.parent}\n" in text
    assert "Icon=com.bdingle.dzll\n" in text and "Categories=Game;\n" in text
    assert text.endswith("X-DZLL-AutoCreated=true\n")
    assert os.stat(desktop_file(sandbox)).st_mode & 0o777 == 0o755
    assert (sandbox.home / ".local/share/icons/hicolor/512x512/apps/com.bdingle.dzll.png").read_bytes() == b"icon"
    assert sandbox.runs == [
        ["update-desktop-database", str(sandbox.home / ".local/share/applications")],
        ["gtk-update-icon-cache", "-f", "-t", str(sandbox.home / ".local/share/icons/hicolor")],
    ]


def test_desktop_integration_never_overwrites_foreign_entry(sandbox):
    desktop_file(sandbox).parent.mkdir(parents=True)
    desktop_file(sandbox).write_text("[Desktop Entry]\nName=Packaged\n")

    integrate(sandbox)

    assert desktop_file(sandbox).read_text() == "[Desktop Entry]\nName=Packaged\n"
    assert sandbox.runs == []


def test_system_entry_removes_only_auto_created_user_entry(sandbox):
    sandbox.state["system"] = True
    desktop_file(sandbox).parent.mkdir(parents=True)
    desktop_file(sandbox).write_text("old\nX-DZLL-AutoCreated=true\n")

    integrate(sandbox)

    assert not desktop_file(sandbox).exists()
    assert sandbox.runs == []
