from dzll_launcher import a2s
from dzll_launcher import a2s_status_diagnostics as diagnostics_module
from dzll_launcher.a2s.exceptions import BrokenMessageError
from dzll_launcher.a2s_status_diagnostics import MAX_PACKET_PREVIEW, MalformedPacketTrace
from dzll_launcher.live import query_server_live


IP = "172.111.51.149"
GPORT = 2502
QPORT = 2503


def enable_target(monkeypatch, lines):
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "environ",
        {
            "DZLL_A2S_STATUS_DEBUG": "1",
            "DZLL_A2S_STATUS_DEBUG_SERVER": f"{IP}:{GPORT}",
        },
    )
    monkeypatch.setattr(
        diagnostics_module.STATUS_DIAGNOSTICS,
        "writer",
        lambda line, flush=True: lines.append(line),
    )


def test_malformed_info_logs_specific_stage_reason_and_bounded_packet(monkeypatch):
    lines = []
    enable_target(monkeypatch, lines)
    packet = b"\xff\xff\xff\xff\x49" + (b"A" * 1000)

    def fail(_address, timeout, _trace=None):
        _trace.note_datagram(packet)
        _trace.stage = "info-source-server-name"
        error = BrokenMessageError("unterminated server name")
        error.a2s_diagnostic = _trace.fields(error)
        raise error

    monkeypatch.setattr(a2s, "info", fail)
    result = query_server_live(IP, QPORT, gport=GPORT, cycle_id="malformed-test")
    complete = next(line for line in lines if "event=query-complete" in line)
    assert not result["ok"]
    assert "classification=malformed" in complete
    assert "malformed_stage=info-source-server-name" in complete
    assert "malformed_reason=unterminated_server_name" in complete
    assert "exception_class=BrokenMessageError" in complete
    assert "exception_message=unterminated_server_name" in complete
    preview = complete.split("hex_preview=", 1)[1].split(" ", 1)[0]
    assert len(bytes.fromhex(preview)) == MAX_PACKET_PREVIEW


def test_hexadecimal_preview_is_capped_at_first_32_bytes():
    trace = MalformedPacketTrace()
    packet = bytes(range(128))
    trace.note_datagram(packet)
    fields = trace.fields(BrokenMessageError("bad packet"))
    assert fields["hex_preview"] == packet[:32].hex()
    assert len(bytes.fromhex(fields["hex_preview"])) == 32


def test_challenge_response_metadata_is_identified():
    trace = MalformedPacketTrace()
    trace.note_datagram(b"\xff\xff\xff\xff\x41\x78\x56\x34\x12")
    fields = trace.fields(BrokenMessageError("challenge malformed"))
    assert fields["a2s_header"] == "0xffffffff"
    assert fields["response_opcode"] == "0x41"
    assert fields["challenge_response"] is True
    assert fields["split_packet"] is False


def test_split_packet_metadata_is_identified():
    trace = MalformedPacketTrace()
    trace.note_datagram(b"\xfe\xff\xff\xff" + b"\x04\x03\x02\x01\x02\x01\x00\x04payload")
    trace.note_split(0x01020304, 2, 1)
    trace.reassembled_length = 77
    fields = trace.fields(BrokenMessageError("fragment rejected"))
    assert fields["split_packet"] is True
    assert fields["split_id"] == "0x01020304"
    assert fields["split_total"] == 2
    assert fields["split_sequence"] == 1
    assert fields["reassembly_attempted"] is True
    assert fields["reassembled_length"] == 77


def test_parser_exception_never_exposes_unbounded_body():
    trace = MalformedPacketTrace()
    secret_tail = b"private-player-or-rule-data" * 100
    trace.note_datagram(b"\xff\xff\xff\xff\x49" + secret_tail)
    fields = trace.fields(BrokenMessageError("X" * 1000))
    assert len(fields["malformed_reason"]) == 240
    assert len(fields["hex_preview"]) <= 64
    assert secret_tail.hex() not in fields["hex_preview"]


def test_diagnostics_disabled_does_not_pass_trace_or_change_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})

    def fail(address, timeout):
        calls.append((address, timeout))
        raise BrokenMessageError("Invalid response type: 0x58")

    monkeypatch.setattr(a2s, "info", fail)
    result = query_server_live(IP, QPORT, gport=GPORT)
    assert calls and result == {
        "ok": False,
        "err": "Invalid response type: 0x58",
        "a2s_classification": "malformed",
    }


def test_malformed_exception_containing_timeout_remains_malformed(monkeypatch):
    monkeypatch.setattr(diagnostics_module.STATUS_DIAGNOSTICS, "environ", {})
    monkeypatch.setattr(
        a2s,
        "info",
        lambda address, timeout: (_ for _ in ()).throw(
            BrokenMessageError("unexpected timeout field in truncated packet")
        ),
    )
    result = query_server_live(IP, QPORT, gport=GPORT)
    assert result["a2s_classification"] == "malformed"
