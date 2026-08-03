import socket
import logging
import time
import io

from .exceptions import AliveButInfoUnavailable, BrokenMessageError
from .a2s_fragment import decode_fragment
from .defaults import DEFAULT_RETRIES
from .byteio import ByteReader



HEADER_SIMPLE = b"\xFF\xFF\xFF\xFF"
HEADER_MULTI = b"\xFE\xFF\xFF\xFF"
A2S_CHALLENGE_RESPONSE = 0x41
A2S_PLAYER_RESPONSE = 0x44
A2S_RULES_RESPONSE = 0x45
MAX_VALID_UNRELATED_DATAGRAMS = 4

logger = logging.getLogger("a2s")


def request_sync(address, timeout, encoding, a2s_proto, trace=None):
    conn = A2SStream(address, timeout, trace=trace)
    deadline = (
        time.monotonic() + float(timeout)
        if bool(getattr(a2s_proto, "fixed_info_deadline", False))
        else None
    )
    try:
        response = request_sync_impl(
            conn,
            encoding,
            a2s_proto,
            trace=trace,
            deadline=deadline,
        )
    except Exception as error:
        if trace is not None:
            try:
                error.a2s_diagnostic = trace.fields(error)
            except Exception:
                pass
        if isinstance(error, AliveButInfoUnavailable):
            conn.close()
        raise
    conn.close()
    return response

def request_sync_impl(
    conn,
    encoding,
    a2s_proto,
    challenge=0,
    retries=0,
    ping=None,
    trace=None,
    deadline=None,
    unrelated_count=0,
):
    send_time = time.monotonic()
    conn.send(a2s_proto.serialize_request(challenge))
    while True:
        try:
            resp_data, sender = conn.recv(deadline=deadline)
        except socket.timeout:
            if deadline is not None:
                remaining = float(deadline) - time.monotonic()
                if remaining > 0:
                    if trace is not None:
                        trace.note_receive_timeout_retry(remaining * 1000.0)
                    continue
            else:
                remaining = 0.0
            if trace is not None:
                trace.final_remaining_deadline_ms = max(0.0, remaining * 1000.0)
            if unrelated_count:
                raise AliveButInfoUnavailable(
                    "deadline-after-valid-unrelated-response",
                    remaining_deadline_ms=max(0.0, remaining * 1000.0),
                )
            raise
        recv_time = time.monotonic()
        if ping is None:
            ping = recv_time - send_time

        reader = ByteReader(
            io.BytesIO(resp_data), endian="<", encoding=encoding, trace=trace)

        reader.set_stage("response-opcode")
        response_type = reader.read_uint8()
        if trace is not None:
            trace.response_opcode = response_type
            trace.challenge_response = response_type == A2S_CHALLENGE_RESPONSE
        if response_type == A2S_CHALLENGE_RESPONSE:
            if retries >= DEFAULT_RETRIES:
                raise BrokenMessageError(
                    "Server keeps sending challenge responses")
            reader.set_stage("challenge-value")
            next_challenge = reader.read_uint32()
            return request_sync_impl(
                conn,
                encoding,
                a2s_proto,
                next_challenge,
                retries + 1,
                ping,
                trace,
                deadline,
                unrelated_count,
            )

        if a2s_proto.validate_response_type(response_type):
            if trace is not None and deadline is not None:
                trace.final_remaining_deadline_ms = max(
                    0.0, (float(deadline) - time.monotonic()) * 1000.0
                )
            reader.set_stage("info-fields")
            return a2s_proto.deserialize_response(reader, response_type, ping)

        response_name = None
        if bool(getattr(a2s_proto, "ignore_valid_unrelated", False)):
            response_name = _valid_unrelated_response(reader, response_type)
        if response_name is None:
            if trace is not None:
                trace.note_parser(reader, "response-opcode-validation")
            raise BrokenMessageError(
                "Invalid response type: " + hex(response_type))

        unrelated_count += 1
        if trace is not None:
            trace.note_unrelated(
                opcode=response_type,
                response_type=response_name,
                sender=sender,
                expected=conn.expected_endpoint,
                count=unrelated_count,
                remaining_ms=(deadline - time.monotonic()) * 1000.0,
            )
        if unrelated_count >= MAX_VALID_UNRELATED_DATAGRAMS:
            remaining_ms = max(0.0, (float(deadline) - time.monotonic()) * 1000.0)
            if trace is not None:
                trace.final_remaining_deadline_ms = remaining_ms
            raise AliveButInfoUnavailable(
                "valid-unrelated-response-cap-reached",
                remaining_deadline_ms=remaining_ms,
            )


def _valid_unrelated_response(reader, response_type):
    """Validate only the two bounded response shapes INFO may safely ignore."""
    try:
        if response_type == A2S_PLAYER_RESPONSE:
            reader.set_stage("unrelated-player-validation")
            count = reader.read_uint8()
            for _index in range(count):
                reader.read_uint8()
                reader.read_cstring()
                reader.read_int32()
                reader.read_float()
            return "A2S_PLAYER_RESPONSE"
        if response_type == A2S_RULES_RESPONSE:
            reader.set_stage("unrelated-rules-validation")
            count = reader.read_uint16()
            for _index in range(count):
                reader.read_cstring()
                reader.read_cstring()
            return "A2S_RULES_RESPONSE"
    except BrokenMessageError:
        raise
    return None


class A2SStream:
    def __init__(self, address, timeout, trace=None):
        self.address = address
        self.timeout = float(timeout)
        self.trace = trace
        self.expected_endpoint = (socket.gethostbyname(str(address[0])), int(address[1]))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(timeout)

    def __del__(self):
        self.close()

    def send(self, data):
        logger.debug("Sending packet: %r", data)
        packet = HEADER_SIMPLE + data
        self._socket.sendto(packet, self.address)

    def _recv_expected(self, size, deadline):
        while True:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self._socket.settimeout(remaining)
            packet, sender = self._socket.recvfrom(size)
            normalized_sender = (str(sender[0]), int(sender[1]))
            if normalized_sender != self.expected_endpoint:
                if self.trace is not None:
                    self.trace.note_wrong_endpoint()
                continue
            return packet, normalized_sender

    def recv(self, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        packet, sender = self._recv_expected(65535, deadline)
        if self.trace is not None:
            self.trace.stage = "udp-header"
            self.trace.note_datagram(packet)
        header = packet[:4]
        data = packet[4:]
        if header == HEADER_SIMPLE:
            logger.debug("Received single packet: %r", data)
            return data, sender
        elif header == HEADER_MULTI:
            if self.trace is not None:
                self.trace.reassembly_attempted = True
            fragments = [decode_fragment(data, trace=self.trace)]
            while len(fragments) < fragments[0].fragment_count:
                packet, fragment_sender = self._recv_expected(4096, deadline)
                if self.trace is not None:
                    self.trace.note_datagram(packet)
                fragments.append(decode_fragment(packet[4:], trace=self.trace))
            fragments.sort(key=lambda f: f.fragment_id)
            reassembled = b"".join(fragment.payload for fragment in fragments)
            # Sometimes there's an additional header present
            if reassembled.startswith(b"\xFF\xFF\xFF\xFF"):
                reassembled = reassembled[4:]
            if self.trace is not None:
                self.trace.reassembled_length = len(reassembled)
                if reassembled:
                    self.trace.response_opcode = reassembled[0]
                    self.trace.challenge_response = reassembled[0] == A2S_CHALLENGE_RESPONSE
            logger.debug("Received %s part packet with content: %r",
                         len(fragments), reassembled)
            return reassembled, sender
        else:
            if self.trace is not None:
                self.trace.stage = "udp-header-validation"
            raise BrokenMessageError(
                "Invalid packet header: " + repr(header))

    def request(self, payload):
        self.send(payload)
        response, _sender = self.recv()
        return response

    def close(self):
        self._socket.close()
