from __future__ import annotations

from types import SimpleNamespace

import pytest

from dzll_launcher import launch_utils, live
from dzll_launcher.background_prepare import BackgroundServerPreparationSnapshot
from dzll_launcher.server_endpoint import (
    ServerEndpointValidationError,
    normalize_ipv4_address,
    normalize_server_endpoint,
    validate_server_port,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.2.3.4", "1.2.3.4"),
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.0.1", "192.168.0.1"),
        ("255.255.255.255", "255.255.255.255"),
        ("  1.2.3.4\t", "1.2.3.4"),
    ],
)
def test_strict_ipv4_accepts_only_canonicalizable_dotted_decimal(value, expected):
    assert normalize_ipv4_address(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        None,
        1234,
        "1.2.3",
        "1.2.3.4.5",
        "256.1.1.1",
        "999.999.999.999",
        "-1.2.3.4",
        "1..2.3",
        "01.02.03.004",
        "1.2.3.a",
        "abc",
        "example.com",
        "127.1",
        "0x7f000001",
        "2130706433",
        "1.2.3.4\nJUNK",
        "junk1.2.3.4",
        "1.2.3.4junk",
        "1.2.3.4\x00",
        "::1",
        "2001:db8::1",
    ],
)
def test_strict_ipv4_rejects_ambiguous_or_malformed_values(value):
    with pytest.raises(ServerEndpointValidationError):
        normalize_ipv4_address(value)


@pytest.mark.parametrize("value", [1, 65535])
def test_port_accepts_actual_in_range_integers(value):
    assert validate_server_port(value) == value


@pytest.mark.parametrize("value", [0, 65536, -1, None, "2302", 2302.0, True, False])
def test_port_rejects_coercion_and_out_of_range_values(value):
    with pytest.raises(ServerEndpointValidationError):
        validate_server_port(value)


def test_query_port_fallback_is_only_for_absence_and_is_range_checked():
    assert normalize_server_endpoint(
        "1.2.3.4", 2302, None, allow_missing_query_port=True
    ) == ("1.2.3.4", 2302, 2303)
    assert normalize_server_endpoint(
        "1.2.3.4", 2302, 27016, allow_missing_query_port=True
    ) == ("1.2.3.4", 2302, 27016)

    with pytest.raises(ServerEndpointValidationError):
        normalize_server_endpoint(
            "1.2.3.4", 65535, None, allow_missing_query_port=True
        )
    with pytest.raises(ServerEndpointValidationError):
        normalize_server_endpoint(
            "1.2.3.4", 2302, "missing", allow_missing_query_port=True
        )


def test_a2s_rejects_invalid_endpoint_without_calling_protocol(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "dzll_launcher.a2s.info",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = live.query_server_live("999.999.999.999", 2303)
    port_result = live.query_server_live("1.2.3.4", 65536)

    assert result["ok"] is False
    assert port_result["ok"] is False
    assert "invalid server endpoint" in result["err"]
    assert calls == []


def test_a2s_valid_endpoint_preserves_protocol_call(monkeypatch):
    calls = []

    def info(address, timeout, **kwargs):
        calls.append((address, timeout, kwargs))
        return SimpleNamespace(
            ping=0.01,
            player_count=1,
            max_players=60,
            keywords="",
            password_protected=False,
        )

    monkeypatch.setattr("dzll_launcher.a2s.info", info)

    result = live.query_server_live(" 1.2.3.4 ", 2303, gport=2302)

    assert result["ok"] is True
    assert calls == [(('1.2.3.4', 2303), 2.0, {})]


class _LaunchWindow:
    settings = {
        "minimize_dayz_launcher": False,
        "skip_dayz_launcher": True,
        "windowed_mode": False,
        "force_fullscreen": False,
        "no_splash": False,
        "ingame_name": "",
        "additional_launch_params": "",
    }
    _discord = None

    def _steam_launch_prefix(self):
        return ["/synthetic/steam", "-applaunch", "221100", "--"]


def test_launch_rejects_invalid_endpoint_before_process_dispatch(monkeypatch):
    launches = []
    side_effects = []
    monkeypatch.setattr(
        launch_utils, "set_launcher_shutdown_mode", lambda *_: side_effects.append(True)
    )
    obj = SimpleNamespace(ip="example.com", gport=2302, name="Bad", map_name="")

    result = launch_utils.launch_direct_steam_url(
        _LaunchWindow(), obj, popen_factory=lambda cmd: launches.append(cmd)
    )

    assert result.submitted is False
    assert result.error_kind == "invalid_endpoint"
    assert launches == []
    assert side_effects == []


def test_launch_uses_canonical_valid_endpoint(monkeypatch):
    launches = []
    monkeypatch.setattr(launch_utils, "set_launcher_shutdown_mode", lambda *_: None)
    obj = SimpleNamespace(ip=" 1.2.3.4 ", gport=2302, name="Valid", map_name="")
    process = SimpleNamespace(pid=42)

    result = launch_utils.launch_direct_steam_url(
        _LaunchWindow(), obj, popen_factory=lambda cmd: launches.append(cmd) or process
    )

    assert result.submitted is True
    assert launches[0][-1] == "-connect=1.2.3.4:2302"


def test_background_snapshot_uses_same_endpoint_contract():
    valid = BackgroundServerPreparationSnapshot.from_server(
        SimpleNamespace(ip=" 1.2.3.4 ", gport=2302, qport=2303, name="Valid"),
        (),
    )
    assert valid.ip == "1.2.3.4"
    assert valid.identity == "1.2.3.4:2302"

    with pytest.raises(ServerEndpointValidationError):
        BackgroundServerPreparationSnapshot.from_server(
            SimpleNamespace(ip="::1", gport=2302, qport=2303, name="Bad"),
            (),
        )
