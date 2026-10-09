# live.py
import os
import time
import re

from .a2s_status_diagnostics import (
    STATUS_DIAGNOSTICS,
    MalformedPacketTrace,
    result_classification,
)
from .a2s.exceptions import AliveButInfoUnavailable
from .server_endpoint import (
    ServerEndpointValidationError,
    normalize_server_endpoint,
)

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_QUEUE_RE = re.compile(r"^lqs(\d+)$", re.IGNORECASE)

def is_valid_hhmm(s: str) -> bool:
    return bool(_TIME_RE.match((s or "").strip()))

def extract_queue_from_keywords(keywords) -> int | None:
    if not keywords:
        return None
    try:
        for token in str(keywords).split(","):
            match = _QUEUE_RE.fullmatch(token.strip())
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None

def extract_time_from_keywords(keywords: str) -> str:
    """
    Scan keywords and return the first valid HH:MM token (from the end).
    Prevents ports / random numeric tokens from being treated as time.
    """
    if not keywords:
        return ""
    try:
        parts = [p.strip() for p in str(keywords).split(",") if p.strip()]
        # Walk backwards so we prefer the last time token (as Tracky/servers often append it at the end)
        for token in reversed(parts):
            if is_valid_hhmm(token):
                return token
    except Exception:
        pass
    return ""

def query_server_live(
    ip: str,
    qport: int,
    timeout: float = 2.0,
    *,
    gport: int | None = None,
    cycle_id: str = "untracked",
    generation="-",
    row_id="-",
    model_id="-",
) -> dict:
    try:
        ip, resolved_gport, qport = normalize_server_endpoint(
            ip,
            qport if gport is None else gport,
            qport,
        )
    except ServerEndpointValidationError as error:
        return {
            "ok": False,
            "err": f"invalid server endpoint: {error}",
            "a2s_classification": "socket-error",
        }
    diag_identity = {
        "ip": ip,
        "gport": resolved_gport,
        "qport": qport,
        "query": "INFO",
        "generation": generation,
        "row_id": row_id,
        "model_id": model_id,
    }
    query_started = time.monotonic()
    STATUS_DIAGNOSTICS.emit("query-start", cycle=cycle_id, **diag_identity)
    try:
        from . import a2s
    except Exception as error:
        classification = result_classification(error)
        malformed_fields = {}
        if classification == "malformed":
            message = str(error).replace("\n", " ")[:240] or error.__class__.__name__
            malformed_fields = {
                "malformed_stage": "a2s-import",
                "exception_class": error.__class__.__name__,
                "exception_message": message,
                "malformed_reason": message,
            }
        STATUS_DIAGNOSTICS.observe_result(
            cycle_id=cycle_id,
            classification=classification,
            elapsed_ms=f"{(time.monotonic() - query_started) * 1000.0:.1f}",
            **malformed_fields,
            **diag_identity,
        )
        result = {
            "ok": False,
            "err": "vendored a2s unavailable",
            "a2s_classification": classification,
        }
        if STATUS_DIAGNOSTICS.enabled:
            result["_a2s_diag"] = {"classification": classification, "cycle_id": cycle_id, "generation": generation}
        return result

    addr = (ip, int(qport))
    packet_trace = None
    if STATUS_DIAGNOSTICS.enabled and STATUS_DIAGNOSTICS.matches(ip, resolved_gport):
        packet_trace = MalformedPacketTrace()

    def emit_unrelated(final_outcome):
        if packet_trace is None:
            return
        final_remaining = packet_trace.final_remaining_deadline_ms
        for timeout_event in packet_trace.receive_timeout_retries:
            STATUS_DIAGNOSTICS.emit(
                "intermediate-receive-timeout-retried",
                active_query_type="INFO",
                final_logical_outcome=final_outcome,
                remaining_deadline_ms=(
                    f"{max(0.0, final_remaining):.1f}"
                    if final_remaining is not None
                    else "unknown"
                ),
                **timeout_event,
                **diag_identity,
            )
        for event in packet_trace.unrelated_responses:
            STATUS_DIAGNOSTICS.emit(
                "unrelated-a2s-response-ignored",
                active_query_type="INFO",
                valid_info_later_arrived=(final_outcome == "success"),
                final_logical_outcome=final_outcome,
                remaining_deadline_ms=(
                    f"{max(0.0, final_remaining):.1f}"
                    if final_remaining is not None
                    else "unknown"
                ),
                status_preserved=True,
                streak_preserved=True,
                **event,
                **diag_identity,
            )
    try:
        t0 = time.monotonic()
        if packet_trace is None:
            info = a2s.info(addr, timeout=float(timeout))
        else:
            info = a2s.info(addr, timeout=float(timeout), _trace=packet_trace)
        t1 = time.monotonic()

        ping_s = None
        try:
            ping_s = float(getattr(info, "ping", None))
        except Exception:
            ping_s = None
        if ping_s is None or ping_s <= 0:
            ping_s = max(0.0, t1 - t0)
        ping_ms = int(round(ping_s * 1000))

        players = int(getattr(info, "player_count", 0) or 0)
        maxp = int(getattr(info, "max_players", 0) or 0)

        kw = getattr(info, "keywords", "") or ""
        t = extract_time_from_keywords(kw)
        queue = extract_queue_from_keywords(kw)

        pw = bool(getattr(info, "password_protected", False))

        parsed_info = {}
        if STATUS_DIAGNOSTICS.enabled and STATUS_DIAGNOSTICS.matches(ip, resolved_gport):
            parsed_info = {
                "server_name": str(getattr(info, "server_name", "") or "")[:160],
                "map_name": str(getattr(info, "map_name", "") or "")[:80],
                "folder": str(getattr(info, "folder", "") or "")[:80],
                "game": str(getattr(info, "game", "") or "")[:80],
                "version": str(getattr(info, "version", "") or "")[:80],
                "players": players,
                "max_players": maxp,
                "ping_seconds": f"{float(ping_s):.6f}",
                "ping_ms": ping_ms,
                "game_time": t or "unavailable",
                "time_acceleration": "not-in-a2s-info",
                "queue": (queue if queue is not None else "unavailable"),
                "password": pw,
                "keywords": str(kw)[:160],
                "edf": getattr(info, "edf", "unavailable"),
                "reported_game_port": getattr(info, "port", "unavailable"),
            }
            STATUS_DIAGNOSTICS.emit(
                "info-payload-parsed",
                cycle=cycle_id,
                **parsed_info,
                **diag_identity,
            )

        result = {
            "ok": True,
            "ping_ms": ping_ms,
            "players": players,
            "max_players": maxp,
            "queue": queue,
            "time": t,
            "password": pw,
            "name": str(getattr(info, "server_name", "") or ""),
            "map": str(getattr(info, "map_name", "") or ""),
        }
        emit_unrelated("success")
        STATUS_DIAGNOSTICS.observe_result(
            cycle_id=cycle_id,
            classification="success",
            final_logical_outcome="success",
            elapsed_ms=f"{(time.monotonic() - query_started) * 1000.0:.1f}",
            **diag_identity,
        )
        if STATUS_DIAGNOSTICS.enabled:
            result["_a2s_diag"] = {
                "classification": "success",
                "cycle_id": cycle_id,
                "generation": generation,
                "parsed_info": parsed_info,
            }
        return result
    except AliveButInfoUnavailable as error:
        emit_unrelated("alive-but-info-unavailable")
        STATUS_DIAGNOSTICS.observe_result(
            cycle_id=cycle_id,
            classification="alive-but-info-unavailable",
            final_logical_outcome="alive-but-info-unavailable",
            neutral_reason=str(error),
            remaining_deadline_ms=f"{max(0.0, error.remaining_deadline_ms):.1f}",
            status_preserved=True,
            streak_preserved=True,
            elapsed_ms=f"{(time.monotonic() - query_started) * 1000.0:.1f}",
            **diag_identity,
        )
        result = {
            "ok": False,
            "neutral": True,
            "outcome": "alive-but-info-unavailable",
            "endpoint_alive": True,
            "a2s_classification": "alive-but-info-unavailable",
        }
        if STATUS_DIAGNOSTICS.enabled:
            result["_a2s_diag"] = {
                "classification": "alive-but-info-unavailable",
                "cycle_id": cycle_id,
                "generation": generation,
            }
        return result
    except Exception as e:
        classification = result_classification(e)
        emit_unrelated("failure")
        malformed_fields = {}
        if classification == "malformed":
            malformed_fields = dict(getattr(e, "a2s_diagnostic", {}) or {})
            if not malformed_fields:
                if packet_trace is not None:
                    malformed_fields = packet_trace.fields(e)
                else:
                    malformed_fields = {
                        "malformed_stage": "query-wrapper",
                        "exception_class": e.__class__.__name__,
                        "exception_message": (str(e).replace("\n", " ")[:240] or e.__class__.__name__),
                        "malformed_reason": (str(e).replace("\n", " ")[:240] or e.__class__.__name__),
                    }
        STATUS_DIAGNOSTICS.observe_result(
            cycle_id=cycle_id,
            classification=classification,
            final_logical_outcome="failure",
            elapsed_ms=f"{(time.monotonic() - query_started) * 1000.0:.1f}",
            **malformed_fields,
            **diag_identity,
        )
        result = {
            "ok": False,
            "err": str(e),
            "a2s_classification": classification,
        }
        if STATUS_DIAGNOSTICS.enabled:
            result["_a2s_diag"] = {"classification": classification, "cycle_id": cycle_id, "generation": generation}
        return result


def _reassemble_dayz_rules_chunks(rules: dict) -> bytes | None:
    """Reassemble DayZ's chunked A2S_RULES payload into one byte buffer.

    DayZ splits its binary mod-list payload across multiple rules whose
    *key* (not value) is a 2-byte header: (chunk_index, total_chunks), both
    1-based uint8s. The value of each such rule is a slice of the payload.
    Requires rules() to have been queried with encoding=None - the payload
    is arbitrary binary data, not text, and decoding it as UTF-8 first
    (the normal/text rules() path) is lossy.
    """
    chunks: dict[int, bytes] = {}
    total_chunks = 0
    for key, value in rules.items():
        if not isinstance(key, bytes) or len(key) != 2 or not isinstance(value, bytes):
            continue
        index, total = key[0], key[1]
        if total <= 0 or not (1 <= index <= total):
            continue
        chunks[index] = value
        total_chunks = total
    if not chunks or len(chunks) < total_chunks:
        return None
    try:
        return b"".join(chunks[i] for i in range(1, total_chunks + 1))
    except KeyError:
        return None


# DayZ escapes exactly 4 bytes that can't appear raw in a rules payload -
# 0x00 (would truncate the null-terminated A2S_RULES value), 0x01 (the
# escape marker itself), and two others (0x02, 0xFF) that presumably collide
# with other protocol-level markers. Each is written as 0x01 followed by its
# index in this table, *not* its literal value - verified against a real
# server's confirmed Workshop IDs, where index 3 decodes to 0xFF, not 0x03.
_DAYZ_RULES_ESCAPE_TABLE = (0x00, 0x01, 0x02, 0xFF)


def _unescape_dayz_rules_payload(buf: bytes) -> bytes:
    """Undo DayZ's escaping of forbidden bytes within a rules payload.

    See _DAYZ_RULES_ESCAPE_TABLE for which 4 bytes are escaped and how.
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b == 0x01 and i + 1 < n and buf[i + 1] < len(_DAYZ_RULES_ESCAPE_TABLE):
            out.append(_DAYZ_RULES_ESCAPE_TABLE[buf[i + 1]])
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def parse_dayz_rules_mods(rules: dict) -> list[dict]:
    """Parse DayZ's chunked A2S_RULES mod-list binary payload.

    Layout after reassembly+unescaping: 1 byte protocol version, 3 byte
    flags, 1 byte mod count, then per mod: 4 byte hash, 1 byte Steam ID
    length, that many bytes of little-endian Steam Workshop ID, 1 byte
    name length, that many bytes of UTF-8 name (no null terminator).
    rules must have been queried with encoding=None (raw bytes), or this
    returns nothing - decoding this binary payload as text first is lossy.
    """
    if not isinstance(rules, dict):
        return []
    raw = _reassemble_dayz_rules_chunks(rules)
    if raw is None:
        return []
    buf = _unescape_dayz_rules_payload(raw)
    if len(buf) < 5:
        return []

    mod_count = buf[4]
    pos = 5
    mods = []
    seen = set()
    for _ in range(mod_count):
        if pos + 4 > len(buf):
            break
        pos += 4  # mod content hash, not used
        if pos + 1 > len(buf):
            break
        id_len = buf[pos]
        pos += 1
        if id_len not in (1, 2, 3, 4) or pos + id_len > len(buf):
            break
        workshop_id = int.from_bytes(buf[pos:pos + id_len], "little")
        pos += id_len
        if pos + 1 > len(buf):
            break
        name_len = buf[pos]
        pos += 1
        if pos + name_len > len(buf):
            break
        name = buf[pos:pos + name_len].decode("utf-8", errors="replace")
        pos += name_len
        if workshop_id <= 0 or workshop_id in seen:
            continue
        seen.add(workshop_id)
        mods.append({"steamWorkshopId": workshop_id, "name": name})
    return mods


def query_server_mods(ip: str, qport: int, timeout: float = 3.0) -> dict:
    """Query A2S_RULES for a server's required-mod list.

    Unlike query_server_live() (A2S_INFO), this hits A2S_RULES - the query
    DayZ servers actually use to advertise their Workshop mod list. Nothing
    else in this app calls A2S_RULES: the normal server list gets its mod
    data from the prebuilt database instead, so this only matters for
    servers added without that database entry (Direct Connect).
    """
    try:
        ip = str(ip).strip()
        qport = int(qport)
    except Exception:
        return {"ok": False, "err": "invalid server endpoint"}
    try:
        from . import a2s
    except Exception as e:
        return {"ok": False, "err": f"vendored a2s unavailable: {e}"}
    debug = os.environ.get("DZLL_MODS_DEBUG") == "1"
    try:
        raw_rules = a2s.rules((ip, qport), timeout=float(timeout), encoding=None)
    except Exception as e:
        if debug:
            print(f"[MODS-DEBUG] {ip}:{qport} A2S_RULES failed: {e!r}", flush=True)
        return {"ok": False, "err": str(e)}
    parsed = parse_dayz_rules_mods(raw_rules)
    if debug:
        print(f"[MODS-DEBUG] {ip}:{qport} raw_rules={raw_rules!r}", flush=True)
        print(f"[MODS-DEBUG] {ip}:{qport} parsed={len(parsed)} mods: {parsed!r}", flush=True)
    return {"ok": True, "mods": parsed}
