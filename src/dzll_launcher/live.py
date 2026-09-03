# live.py
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
