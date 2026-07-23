"""Opt-in diagnostics for live-browser A2S status observations.

This module is deliberately independent of GTK so the diagnostic contract can be
tested without constructing the application window.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field

from .a2s.exceptions import BrokenMessageError


PREFIX = "[A2S-STATUS]"
MAX_PACKET_PREVIEW = 32


def diagnostics_enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("DZLL_A2S_STATUS_DEBUG", "")).strip() == "1"


def parse_server_filter(value: str | None):
    value = str(value or "").strip()
    if not value:
        return None
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        return None
    try:
        game_port = int(port)
    except (TypeError, ValueError):
        return None
    if not 1 <= game_port <= 65535:
        return None
    return host.strip(), game_port


def result_classification(error=None, *, ok: bool | None = None) -> str:
    if ok is True:
        return "success"
    if isinstance(error, BrokenMessageError):
        return "malformed"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, OSError):
        return "socket-error"
    text = str(error or "").strip().lower()
    if any(
        token in text
        for token in (
            "malformed",
            "truncated",
            "invalid response",
            "invalid opcode",
            "unexpected response",
            "unexpected protocol",
            "protocol error",
            "invalid packet",
            "invalid header",
            "buffer",
        )
    ):
        return "malformed"
    if any(token in text for token in ("socket", "network is unreachable", "connection refused")):
        return "socket-error"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if text in {"offline", "explicit offline"}:
        return "explicit-offline"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    return "malformed" if error is not None else "explicit-offline"


@dataclass
class MalformedPacketTrace:
    """Bounded metadata captured only for an enabled, matching debug target."""

    stage: str = "udp-receive"
    datagrams_received: int = 0
    datagram_length: int | None = None
    preview: bytes = b""
    header: bytes = b""
    response_opcode: int | None = None
    challenge_response: bool = False
    split_packet: bool = False
    split_id: int | None = None
    split_total: int | None = None
    split_sequence: int | None = None
    reassembly_attempted: bool = False
    reassembled_length: int | None = None
    parser_offset: int | None = None
    parser_remaining: int | None = None
    unrelated_responses: list[dict] = field(default_factory=list)
    receive_timeout_retries: list[dict] = field(default_factory=list)
    wrong_endpoint_count: int = 0
    final_remaining_deadline_ms: float | None = None

    def note_unrelated(
        self,
        *,
        opcode: int,
        response_type: str,
        sender,
        expected,
        count: int,
        remaining_ms: float,
    ) -> None:
        self.unrelated_responses.append(
            {
                "received_opcode": f"0x{int(opcode):02x}",
                "decoded_response_type": response_type,
                "sender_endpoint": f"{sender[0]}:{sender[1]}",
                "expected_endpoint": f"{expected[0]}:{expected[1]}",
                "ignored_datagram_count": int(count),
                "remaining_deadline_ms_at_ignore": f"{max(0.0, remaining_ms):.1f}",
            }
        )

    def note_receive_timeout_retry(self, remaining_ms: float) -> None:
        self.receive_timeout_retries.append(
            {
                "remaining_deadline_ms_at_timeout": f"{max(0.0, remaining_ms):.1f}",
            }
        )

    def note_wrong_endpoint(self) -> None:
        self.wrong_endpoint_count += 1

    def note_datagram(self, packet: bytes) -> None:
        packet = bytes(packet or b"")
        self.datagrams_received += 1
        self.datagram_length = len(packet)
        self.preview = packet[:MAX_PACKET_PREVIEW]
        self.header = packet[:4]
        self.split_packet = self.header == b"\xfe\xff\xff\xff"
        payload = packet[4:]
        if not self.split_packet and payload:
            self.response_opcode = payload[0]
            self.challenge_response = self.response_opcode == 0x41

    def note_split(self, message_id=None, total=None, sequence=None) -> None:
        self.split_packet = True
        self.reassembly_attempted = True
        self.split_id = message_id
        self.split_total = total
        self.split_sequence = sequence

    def note_parser(self, reader, stage: str) -> None:
        self.stage = str(stage)
        try:
            self.parser_offset = int(reader.stream.tell())
            current = self.parser_offset
            reader.stream.seek(0, 2)
            end = int(reader.stream.tell())
            reader.stream.seek(current)
            self.parser_remaining = max(0, end - current)
        except Exception:
            pass

    def fields(self, error: BaseException) -> dict:
        message = str(error or error.__class__.__name__).replace("\n", " ")[:240]
        header_value = int.from_bytes(self.header.ljust(4, b"\x00")[:4], "little") if self.header else None
        return {
            "malformed_stage": self.stage,
            "exception_class": error.__class__.__name__,
            "exception_message": message or error.__class__.__name__,
            "malformed_reason": message or error.__class__.__name__,
            "datagram_length": self.datagram_length if self.datagram_length is not None else "unknown",
            "hex_preview": self.preview.hex(),
            "a2s_header": (f"0x{header_value:08x}" if header_value is not None else "unknown"),
            "response_opcode": (f"0x{self.response_opcode:02x}" if self.response_opcode is not None else "unknown"),
            "challenge_response": self.challenge_response,
            "split_packet": self.split_packet,
            "split_id": (f"0x{self.split_id:08x}" if self.split_id is not None else "unknown"),
            "split_total": self.split_total if self.split_total is not None else "unknown",
            "split_sequence": self.split_sequence if self.split_sequence is not None else "unknown",
            "datagrams_received": self.datagrams_received,
            "reassembly_attempted": self.reassembly_attempted,
            "reassembled_length": self.reassembled_length if self.reassembled_length is not None else "unknown",
            "parser_offset": self.parser_offset if self.parser_offset is not None else "unknown",
            "parser_remaining": self.parser_remaining if self.parser_remaining is not None else "unknown",
        }


@dataclass
class CycleObservations:
    """Tracks diagnostic ordering only; it never decides application behaviour."""

    failures: dict[str, int] = field(default_factory=dict)

    def failure_ordinal(self, cycle_id: str, classification: str) -> int:
        if classification in {"success", "alive-but-info-unavailable"}:
            return 0
        ordinal = self.failures.get(str(cycle_id), 0) + 1
        self.failures[str(cycle_id)] = ordinal
        return ordinal


class A2SStatusDiagnostics:
    def __init__(self, *, environ=None, writer=None, clock=None):
        self.environ = os.environ if environ is None else environ
        self.writer = print if writer is None else writer
        self.clock = time.monotonic if clock is None else clock
        self.cycles = CycleObservations()

    @property
    def enabled(self) -> bool:
        return diagnostics_enabled(self.environ)

    @property
    def target(self):
        return parse_server_filter(self.environ.get("DZLL_A2S_STATUS_DEBUG_SERVER"))

    def matches(self, ip: str, gport: int) -> bool:
        target = self.target
        return target is None or target == (str(ip), int(gport))

    def emit(self, event: str, *, ip: str, gport: int, qport: int, **fields) -> bool:
        if not self.enabled or not self.matches(ip, gport):
            return False
        values = [
            PREFIX,
            f"mono={self.clock():.6f}",
            f"event={event}",
            f"ip={ip}",
            f"gport={int(gport)}",
            f"qport={int(qport)}",
        ]
        values.extend(f"{key}={self._clean(value)}" for key, value in fields.items())
        self.writer(" ".join(values), flush=True)
        return True

    def observe_result(self, *, cycle_id: str, classification: str, **identity) -> int:
        ordinal = self.cycles.failure_ordinal(cycle_id, classification)
        self.emit(
            "query-complete",
            cycle=cycle_id,
            classification=classification,
            cycle_failure=("none" if ordinal == 0 else ("first" if ordinal == 1 else "second" if ordinal == 2 else ordinal)),
            **identity,
        )
        return ordinal

    @staticmethod
    def _clean(value) -> str:
        return str(value).replace("\n", "\\n").replace(" ", "_")


STATUS_DIAGNOSTICS = A2SStatusDiagnostics()
