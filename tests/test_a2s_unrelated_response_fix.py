import io
import socket
import struct
from types import SimpleNamespace

import pytest

from dzll_launcher import a2s
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import a2s_status_diagnostics as diagnostics_module
from dzll_launcher.a2s.a2s_sync import (
    A2SStream,
    MAX_VALID_UNRELATED_DATAGRAMS,
    request_sync_impl,
)
from dzll_launcher.a2s import a2s_sync as a2s_sync_module
from dzll_launcher.a2s.exceptions import AliveButInfoUnavailable, BrokenMessageError
from dzll_launcher.a2s.info import InfoProtocol
from dzll_launcher.a2s_status_diagnostics import MalformedPacketTrace, result_classification
from dzll_launcher.live import query_server_live
from dzll_launcher.window import DZLLWindow, fav_key


IP = "172.111.51.149"
GPORT = 2502
QPORT = 2503
SENDER = (IP, QPORT)


def source_info_payload(players=17):
    return (
        b"\x49\x11"
        + b"Test Server\0chernarusplus\0dayz\0DayZ\0"
        + struct.pack("<HBBB", 221100 & 0xFFFF, players, 60, 0)
        + b"dl\x00\x01"
        + b"1.0\0"
        + b"\x00"
    )


def rules_payload():
    return b"\x45" + struct.pack("<H", 1) + b"key\0value\0"


def players_payload():
    return b"\x44\x01\x00Player\0" + struct.pack("<if", 5, 10.0)


class ScriptedConnection:
    timeout = 1.25
    expected_endpoint = SENDER

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.deadlines = []

    def send(self, payload):
        self.sent.append(payload)

    def recv(self, deadline=None):
        self.deadlines.append(deadline)
        if not self.responses:
            raise socket.timeout()
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value, SENDER


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class TimedScriptedConnection(ScriptedConnection):
    def __init__(self, clock, responses):
        super().__init__(responses)
        self.clock = clock

    def recv(self, deadline=None):
        self.deadlines.append(deadline)
        if not self.responses:
            raise socket.timeout()
        value, at = self.responses.pop(0)
        self.clock.value = float(at)
        if isinstance(value, BaseException):
            raise value
        return value, SENDER


def parse_script(responses, trace=None, deadline=100.0):
    conn = ScriptedConnection(responses)
    result = request_sync_impl(
        conn,
        "utf-8",
        InfoProtocol,
        trace=trace,
        deadline=deadline,
    )
    return result, conn


def status_host(*, ping=42, streak=0, players=10):
    obj = SimpleNamespace(
        ip=IP,
        gport=GPORT,
        qport=QPORT,
        ping=ping,
        players=players,
        max_players=60,
        password=False,
        time="12:00",
        queue=3,
    )
    key = fav_key(IP, GPORT)
    host = SimpleNamespace(
        _obj_by_key={key: obj},
        _browser_live_offline_streaks=({key: streak} if streak else {}),
        _browser_live_token=8,
        column_view_store=object(),
        live={key: {"offline": ping < 0}},
        dead={},
        _dead_session=set(),
        sort_key="ping",
        sort_asc=True,
        _active_filter_depends_on_live_values=lambda: False,
        _debug_browser_reorder=lambda *args, **kwargs: None,
        _debug_sort_note_live_update=lambda *args, **kwargs: None,
        _apply_titlebar_counts=lambda: None,
        _update_row_sort_ping=lambda value: None,
        _update_row_sort_players=lambda value: None,
        _on_filter_changed=lambda **kwargs: None,
    )
    return host, key, obj


def test_rules_then_info_returns_success_and_status_updates_and_resets_streak():
    info, conn = parse_script([rules_payload(), source_info_payload(players=17)])
    assert info.player_count == 17
    assert len(conn.sent) == 1
    host, key, obj = status_host(ping=-1, streak=2)
    result = {
        "ok": True,
        "ping_ms": 50,
        "players": info.player_count,
        "max_players": info.max_players,
        "time": "12:34",
        "password": False,
    }
    DZLLWindow._apply_live_results(host, [(key, result)], reason="browser-live")
    assert obj.ping == 50 and obj.players == 17
    assert key not in host._browser_live_offline_streaks


def test_players_then_info_returns_success():
    info, _conn = parse_script([players_payload(), source_info_payload(players=21)])
    assert info.player_count == 21


def test_unrelated_response_is_not_a_status_observation_or_streak_mutation():
    trace = MalformedPacketTrace()
    info, _conn = parse_script([rules_payload(), source_info_payload()], trace=trace)
    assert info.player_count == 17
    assert len(trace.unrelated_responses) == 1
    host, key, _obj = status_host(streak=1)
    assert host._browser_live_offline_streaks[key] == 1


@pytest.mark.parametrize("ping", [45, -1])
def test_neutral_preserves_online_or_offline_status_row_and_streak(ping):
    host, key, obj = status_host(ping=ping, streak=2, players=33)
    before = (obj.ping, obj.players, obj.max_players, obj.time, obj.queue)
    neutral = {"ok": False, "neutral": True, "outcome": "alive-but-info-unavailable"}
    DZLLWindow._apply_live_results(host, [(key, neutral)], reason="browser-live")
    assert (obj.ping, obj.players, obj.max_players, obj.time, obj.queue) == before
    assert host._browser_live_offline_streaks[key] == 2


def test_only_unrelated_until_deadline_returns_neutral():
    conn = ScriptedConnection([rules_payload(), socket.timeout()])
    with pytest.raises(AliveButInfoUnavailable, match="deadline"):
        request_sync_impl(conn, "utf-8", InfoProtocol, deadline=100.0)
    assert conn.deadlines == [100.0, 100.0]


def test_query_wrapper_records_endpoint_alive_for_neutral(monkeypatch):
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(
        a2s,
        "info",
        lambda address, timeout: (_ for _ in ()).throw(
            AliveButInfoUnavailable("deadline-after-valid-unrelated-response")
        ),
    )
    result = query_server_live(IP, QPORT, gport=GPORT)
    assert result == {
        "ok": False,
        "neutral": True,
        "outcome": "alive-but-info-unavailable",
        "endpoint_alive": True,
        "a2s_classification": "alive-but-info-unavailable",
    }


def test_unrelated_packets_do_not_extend_fixed_deadline():
    conn = ScriptedConnection([rules_payload(), players_payload(), socket.timeout()])
    with pytest.raises(AliveButInfoUnavailable):
        request_sync_impl(conn, "utf-8", InfoProtocol, deadline=77.25)
    assert conn.deadlines == [77.25, 77.25, 77.25]


def test_intermediate_timeout_with_time_remaining_then_info_succeeds(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(a2s_sync_module.time, "monotonic", clock)
    trace = MalformedPacketTrace()
    conn = TimedScriptedConnection(
        clock,
        [
            (rules_payload(), 0.10),
            (socket.timeout(), 0.40),
            (source_info_payload(players=27), 0.75),
        ],
    )
    info = request_sync_impl(conn, "utf-8", InfoProtocol, trace=trace, deadline=1.0)
    assert info.player_count == 27
    assert conn.deadlines == [1.0, 1.0, 1.0]
    assert trace.receive_timeout_retries == [{"remaining_deadline_ms_at_timeout": "600.0"}]
    assert trace.final_remaining_deadline_ms == pytest.approx(250.0)


def test_info_near_original_deadline_succeeds_without_restart(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(a2s_sync_module.time, "monotonic", clock)
    conn = TimedScriptedConnection(
        clock,
        [(rules_payload(), 0.05), (socket.timeout(), 0.70), (source_info_payload(players=29), 0.99)],
    )
    info = request_sync_impl(conn, "utf-8", InfoProtocol, deadline=1.0)
    assert info.player_count == 29
    assert conn.deadlines == [1.0, 1.0, 1.0]
    assert clock.value == 0.99


def test_deadline_neutral_only_when_remaining_is_not_positive(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(a2s_sync_module.time, "monotonic", clock)
    trace = MalformedPacketTrace()
    conn = TimedScriptedConnection(
        clock,
        [(rules_payload(), 0.10), (socket.timeout(), 0.50), (socket.timeout(), 1.0)],
    )
    with pytest.raises(AliveButInfoUnavailable, match="deadline") as caught:
        request_sync_impl(conn, "utf-8", InfoProtocol, trace=trace, deadline=1.0)
    assert caught.value.remaining_deadline_ms == 0.0
    assert len(trace.receive_timeout_retries) == 1
    assert trace.final_remaining_deadline_ms == 0.0
    assert conn.deadlines == [1.0, 1.0, 1.0]


def test_unrelated_datagram_cap_prevents_infinite_loop():
    conn = ScriptedConnection([rules_payload()] * 20)
    with pytest.raises(AliveButInfoUnavailable, match="cap-reached"):
        request_sync_impl(conn, "utf-8", InfoProtocol, deadline=100.0)
    assert len(conn.deadlines) == MAX_VALID_UNRELATED_DATAGRAMS
    assert MAX_VALID_UNRELATED_DATAGRAMS == 4


def test_wrong_endpoint_is_ignored_without_positive_evidence():
    class FakeSocket:
        def __init__(self):
            self.values = [(b"wrong", ("192.0.2.9", QPORT)), (b"right", SENDER)]

        def settimeout(self, value):
            self.timeout = value

        def recvfrom(self, size):
            return self.values.pop(0)

        def close(self):
            pass

    trace = MalformedPacketTrace()
    stream = A2SStream.__new__(A2SStream)
    stream.expected_endpoint = SENDER
    stream.trace = trace
    stream._socket = FakeSocket()
    packet, sender = stream._recv_expected(65535, deadline=10**9)
    assert packet == b"right" and sender == SENDER
    assert trace.wrong_endpoint_count == 1
    assert trace.unrelated_responses == []


def test_genuine_timeout_creates_exactly_one_failure(monkeypatch):
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(a2s, "info", lambda address, timeout: (_ for _ in ()).throw(socket.timeout()))
    result = query_server_live(IP, QPORT, gport=GPORT)
    host, key, obj = status_host()
    DZLLWindow._apply_live_results(host, [(key, result)], reason="browser-live")
    assert obj.ping == 42
    assert host._browser_live_offline_streaks[key] == 1


@pytest.mark.parametrize(
    "message",
    [
        "truncated timeout field",
        "invalid opcode after timeout marker",
        "unexpected protocol response: timeout token",
        "malformed buffer timeout",
    ],
)
def test_generic_protocol_error_with_timeout_text_remains_malformed(message):
    assert result_classification(Exception(message)) == "malformed"


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (TimeoutError("timed out"), "timeout"),
        (socket.timeout("timed out"), "timeout"),
        (OSError("network is unreachable"), "socket-error"),
    ],
)
def test_structured_timeout_and_network_errors_remain_qualifying(error, classification):
    assert result_classification(error) == classification


def test_live_mapping_fallback_does_not_promote_malformed_timeout_text():
    payload = {"ok": False, "err": "truncated timeout field"}
    assert runtime.live_result_disposition(payload) is runtime.LiveResultDisposition.PROTOCOL_FAILURE


def test_malformed_expected_info_retains_failure_behaviour(monkeypatch):
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(
        a2s,
        "info",
        lambda address, timeout: (_ for _ in ()).throw(BrokenMessageError("Invalid response type: 0x99")),
    )
    result = query_server_live(IP, QPORT, gport=GPORT)
    host, key, _obj = status_host(streak=1)
    DZLLWindow._apply_live_results(host, [(key, result)], reason="browser-live")
    assert host._browser_live_offline_streaks[key] == 2


def test_challenge_handling_uses_same_socket_and_limit_semantics():
    challenge = b"\x41" + struct.pack("<I", 0x12345678)
    info, conn = parse_script([challenge, source_info_payload()])
    assert info.player_count == 17
    assert len(conn.sent) == 2
    assert conn.sent[1].endswith(struct.pack("<I", 0x12345678))


def test_split_info_reassembly_remains_compatible():
    payload = source_info_payload()
    first, second = payload[:20], payload[20:]
    message_id = 123
    packets = [
        (b"\xfe\xff\xff\xff" + struct.pack("<IBBH", message_id, 2, 0, 1248) + first, SENDER),
        (b"\xfe\xff\xff\xff" + struct.pack("<IBBH", message_id, 2, 1, 1248) + second, SENDER),
    ]

    class FakeSocket:
        def settimeout(self, value):
            self.timeout = value

        def recvfrom(self, size):
            return packets.pop(0)

        def close(self):
            pass

    stream = A2SStream.__new__(A2SStream)
    stream.timeout = 1.25
    stream.expected_endpoint = SENDER
    stream.trace = None
    stream._socket = FakeSocket()
    reassembled, sender = stream.recv(deadline=10**9)
    assert reassembled == payload and sender == SENDER


def test_diagnostics_disabled_by_default_for_neutral(monkeypatch):
    lines = []
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "writer", lambda line, flush=True: lines.append(line))
    assert not diagnostics_module.STATUS_DIAGNOSTICS.enabled
    assert lines == []


def test_targeted_diagnostics_record_rules_opcode_and_final_success(monkeypatch):
    lines = []
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "environ",
        {"DZLL_A2S_STATUS_DEBUG": "1", "DZLL_A2S_STATUS_DEBUG_SERVER": f"{IP}:{GPORT}"},
    )
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "writer", lambda line, flush=True: lines.append(line))

    def scripted_info(address, timeout, _trace=None):
        info, _conn = parse_script([rules_payload(), source_info_payload()], trace=_trace)
        _trace.note_receive_timeout_retry(500.0)
        _trace.final_remaining_deadline_ms = 250.0
        return info

    monkeypatch.setattr(a2s, "info", scripted_info)
    result = query_server_live(IP, QPORT, gport=GPORT, cycle_id="targeted")
    assert result["ok"]
    ignored = next(line for line in lines if "event=unrelated-a2s-response-ignored" in line)
    assert "received_opcode=0x45" in ignored
    assert "decoded_response_type=A2S_RULES_RESPONSE" in ignored
    assert "final_logical_outcome=success" in ignored
    assert "valid_info_later_arrived=True" in ignored
    retried = next(line for line in lines if "event=intermediate-receive-timeout-retried" in line)
    assert "remaining_deadline_ms_at_timeout=500.0" in retried
    assert "remaining_deadline_ms=250.0" in retried
