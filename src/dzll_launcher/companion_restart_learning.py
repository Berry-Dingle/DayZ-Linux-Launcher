from __future__ import annotations

import os
import math
import time


MIN_RESTART_OUTAGE_SECONDS = 90
MIN_SOFT_RESTART_OUTAGE_SECONDS = 45
MIN_RESTART_INTERVAL_SECONDS = 3 * 3600
MAX_RESTART_INTERVAL_SECONDS = 24 * 3600
CYCLE_BUCKETS_SECONDS = (3 * 3600, 4 * 3600, 6 * 3600, 8 * 3600, 12 * 3600)
STATE_VERSION = 2
SCORING_ALGORITHM_VERSION = 2
UI_CONFIDENCE_THRESHOLD = 0.80
MODEL_RESET_CONFIDENCE = 0.40
RAW_OUTAGE_LIMIT = 100
RAW_QUERY_VISIBLE_RESTART_LIMIT = 100
RESTART_EVENT_LIMIT = 100
EXPECTED_WINDOW_MISS_LIMIT = 100
MONITOR_SESSION_LIMIT = 100
MONITOR_HEARTBEAT_SECONDS = 120
MONITOR_MAX_COVERAGE_GAP_SECONDS = 240
MONITOR_WINDOW_MIN_COVERAGE_RATIO = 0.75
CONTRADICTION_LIMIT = 100
PROJECTED_UNMONITORED_WEIGHT_FACTOR = 0.05
PROJECTED_COVERED_WEIGHT_FACTOR = 0.50
HARD_CONTRADICTION_PENALTY = 0.75
SOFT_CONTRADICTION_PENALTY = 0.35
NO_CYCLE_CONTRADICTION_PENALTY = 0.50
ONE_INTERVAL_CONFIDENCE_CAP = 0.25
TWO_INTERVAL_CONFIDENCE_CAP = 0.45
FEWER_THAN_FOUR_EVENTS_CONFIDENCE_CAP = 0.60
NO_COVERED_CYCLE_CONFIDENCE_CAP = 0.50
ONLY_UNMONITORED_MULTIPLES_CONFIDENCE_CAP = 0.35
AMBIGUOUS_PROJECTED_OBSERVATION_RATIO = 0.50
AMBIGUOUS_PROJECTED_MINIMUM_COUNT = 2
TWELVE_HOUR_CYCLE_SECONDS = 12 * 3600
MAX_LEARNING_EVENT_AGE_SECONDS = 48 * 3600
MIN_QUERY_VISIBLE_ZERO_SECONDS = 45
MAX_QUERY_VISIBLE_ZERO_SECONDS = 8 * 60
QUERY_VISIBLE_EVIDENCE_WEIGHT = 1.0
DEBUG_SC_ALERTS = os.environ.get("DZLL_DEBUG_SC_ALERTS") == "1"


def new_state() -> dict:
    return {"version": STATE_VERSION, "servers": {}}


def normalize_state(state: dict | None) -> dict:
    if not isinstance(state, dict):
        return new_state()
    servers = state.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    normalized_servers = {}
    for key, server in servers.items():
        try:
            normalized = _normalize_server(server)
            if _safe_int(normalized.get("scoring_algorithm_version"), 0) != SCORING_ALGORITHM_VERSION:
                _recalculate_model(normalized)
        except Exception as exc:
            # One damaged record must not expose stale confidence or prevent
            # other servers from being migrated at startup.
            try:
                normalized = _safe_server_fallback(server)
            except Exception:
                normalized = _safe_server_fallback({})
            if DEBUG_SC_ALERTS:
                print(f"[SC-ALERT] restart-learning normalization failed for {key!s}: {exc!r}", flush=True)
        normalized_servers[str(key)] = normalized
    return {
        "version": STATE_VERSION,
        "servers": normalized_servers,
    }


def make_server_key(ip: str, gport: int) -> str | None:
    ip = str(ip or "").strip()
    try:
        gport = int(gport)
    except Exception:
        gport = 0
    if not ip or gport <= 0:
        return None
    return f"{ip}:{gport}"


def is_established_model(server: dict | None) -> bool:
    server = _normalize_server(server)
    return (
        _safe_int(server.get("learned_cycle_seconds"), 0) > 0
        and _safe_float(server.get("confidence"), 0.0) >= UI_CONFIDENCE_THRESHOLD
        and _safe_int(server.get("scoring_algorithm_version"), 0) == SCORING_ALGORITHM_VERSION
    )


def scheduled_outage_alert_threshold(
    state: dict | None,
    key: str,
    offline_at: int,
    online_at: int,
) -> int | None:
    state = normalize_state(state)
    server = (state.get("servers") or {}).get(str(key))
    if not isinstance(server, dict):
        return None
    server = _normalize_server(server)
    threshold = _safe_int(server.get("learned_min_offline_alert_seconds"), 0)
    learned_cycle = _safe_int(server.get("learned_cycle_seconds"), 0)
    if threshold <= 0 or learned_cycle <= 0 or not is_established_model(server):
        return None
    offline_at = _safe_int(offline_at, 0)
    online_at = _safe_int(online_at, 0)
    for event in reversed(server.get("restart_events") or []):
        if (
            isinstance(event, dict)
            and str(event.get("source") or "") != "query_visible"
            and _safe_int(event.get("offline_at"), 0) == offline_at
            and _safe_int(event.get("online_at"), 0) == online_at
            and _safe_int(event.get("cycle_seconds"), 0) == learned_cycle
        ):
            return threshold
    return None


def record_confirmed_outage(state: dict | None, key: str, outage: dict) -> dict:
    state = normalize_state(state)
    if not key:
        return state

    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server

    now = int(time.time())
    name = str((outage or {}).get("name") or server.get("name") or "")
    map_name = str((outage or {}).get("map") or server.get("map") or "")
    offline_at = _safe_int((outage or {}).get("offline_at"), 0)
    online_at = _safe_int((outage or {}).get("online_at"), 0)
    duration_seconds = _safe_int((outage or {}).get("duration_seconds"), 0)
    if duration_seconds <= 0 and offline_at > 0 and online_at >= offline_at:
        duration_seconds = online_at - offline_at

    server["name"] = name
    server["map"] = map_name
    server["updated_at"] = now

    if offline_at <= 0 or online_at <= 0 or online_at < offline_at:
        _append_raw_outage(server, offline_at, online_at, duration_seconds, False, "invalid_outage")
        return state

    if duration_seconds < MIN_SOFT_RESTART_OUTAGE_SECONDS:
        _append_raw_outage(server, offline_at, online_at, duration_seconds, False, "short_outage")
        return state

    is_soft = duration_seconds < MIN_RESTART_OUTAGE_SECONDS
    previous_event = _latest_restart_event(server)
    if is_soft:
        previous_event = _latest_current_generation_restart_event(server)
        if previous_event is None:
            promoted_anchor = _latest_matching_soft_anchor_candidate(server, online_at)
            if promoted_anchor is not None:
                prior_outage, interval_seconds, cycle_seconds = promoted_anchor
                _append_raw_outage(server, offline_at, online_at, duration_seconds, True, "soft_anchor_promoted")
                if not _has_restart_event_at(server, _safe_int(prior_outage.get("online_at"), 0)):
                    _append_restart_event(
                        server,
                        offline_at=_safe_int(prior_outage.get("offline_at"), 0),
                        online_at=_safe_int(prior_outage.get("online_at"), 0),
                        duration_seconds=_safe_int(prior_outage.get("duration_seconds"), 0),
                        interval_seconds=None,
                        cycle_seconds=None,
                        model_generation=_model_generation(server),
                        soft=True,
                    )
                _append_restart_event(
                    server,
                    offline_at=offline_at,
                    online_at=online_at,
                    duration_seconds=duration_seconds,
                    interval_seconds=interval_seconds,
                    cycle_seconds=cycle_seconds,
                    model_generation=_model_generation(server),
                    soft=True,
                    interval_start_at=_safe_int(prior_outage.get("online_at"), 0),
                )
                _recalculate_model(server)
                server["last_restart_event_at"] = online_at
                server["last_observed_outage_at"] = online_at
                return state
            _append_raw_outage(server, offline_at, online_at, duration_seconds, False, "soft_no_anchor")
            server["last_observed_outage_at"] = online_at
            return state

    if previous_event is None:
        _append_raw_outage(server, offline_at, online_at, duration_seconds, True, None)
        _append_restart_event(
            server,
            offline_at=offline_at,
            online_at=online_at,
            duration_seconds=duration_seconds,
            interval_seconds=None,
            cycle_seconds=None,
            model_generation=_model_generation(server),
        )
        server["last_restart_event_at"] = online_at
        server["last_observed_outage_at"] = online_at
        server["confidence"] = 0.0
        server["matching_event_count"] = 0
        server["expected_restart_minutes_of_day"] = []
        return state

    interval_seconds = online_at - _safe_int(previous_event.get("online_at"), online_at)
    min_cycle = min(CYCLE_BUCKETS_SECONDS)
    min_cycle_floor = min_cycle - bucket_tolerance_seconds(min_cycle)
    if interval_seconds < min_cycle_floor:
        reason = "soft_short_interval" if is_soft else "short_interval"
        _append_raw_outage(server, offline_at, online_at, duration_seconds, not is_soft, reason)
        server["last_observed_outage_at"] = online_at
        return state

    learned_cycle = _safe_int(server.get("learned_cycle_seconds"), 0)
    projected_anchor = _latest_current_generation_restart_event(server)
    projected_match = nearest_projected_cycle_delta(
        _safe_int((projected_anchor or {}).get("online_at"), 0),
        online_at,
        learned_cycle,
    )
    projected_contradiction_recorded = False
    if projected_match is not None and projected_match[0] > 1:
        projected_cycle_count, projected_delta = projected_match
        projected_match_blocked = False
        if abs(projected_delta) <= projected_window_tolerance_seconds(learned_cycle):
            projected_match_blocked = has_observed_miss_between(
                server,
                _safe_int((projected_anchor or {}).get("online_at"), 0),
                online_at,
                learned_cycle,
                _model_generation(server),
            )
        if abs(projected_delta) <= projected_window_tolerance_seconds(learned_cycle) and not projected_match_blocked:
            evidence_weight, coverage_quality = projected_multiple_evidence_weight(
                server,
                _safe_int((projected_anchor or {}).get("online_at"), 0),
                online_at,
                learned_cycle,
                projected_cycle_count,
                soft=is_soft,
            )
            _append_raw_outage(
                server,
                offline_at,
                online_at,
                duration_seconds,
                True,
                "soft_projected_window_match" if is_soft else "projected_window_match",
            )
            _append_restart_event(
                server,
                offline_at=offline_at,
                online_at=online_at,
                duration_seconds=duration_seconds,
                interval_seconds=interval_seconds,
                cycle_seconds=learned_cycle,
                model_generation=_model_generation(server),
                soft=is_soft,
                evidence_weight=evidence_weight,
                evidence_kind="projected_multiple",
                coverage_quality=coverage_quality,
                projected_cycle_count=projected_cycle_count,
                interval_start_at=_safe_int((projected_anchor or {}).get("online_at"), 0),
            )
            _recalculate_model(server)
            server["last_restart_event_at"] = online_at
            server["last_observed_outage_at"] = online_at
            return state

        # A plausible outage outside the projection must continue through ordinary
        # bucket analysis. Otherwise the selected model suppresses its challengers.
        if not projected_match_blocked:
            _append_contradiction(
                server,
                observed_at=online_at,
                contradicted_cycle_seconds=learned_cycle,
                observed_cycle_seconds=None,
                reason="soft_projected_window_miss" if is_soft else "projected_window_miss",
                soft=is_soft,
            )
            projected_contradiction_recorded = True

    if interval_seconds > MAX_RESTART_INTERVAL_SECONDS:
        reason = "soft_too_long_interval" if is_soft else "too_long_interval"
        _append_raw_outage(server, offline_at, online_at, duration_seconds, True, reason)
        _append_restart_event(
            server,
            offline_at=offline_at,
            online_at=online_at,
            duration_seconds=duration_seconds,
            interval_seconds=interval_seconds,
            cycle_seconds=None,
            model_generation=_model_generation(server),
            soft=is_soft,
            evidence_kind="anchor",
            interval_start_at=_safe_int(previous_event.get("online_at"), 0),
        )
        if projected_contradiction_recorded:
            _recalculate_model(server)
        server["last_restart_event_at"] = online_at
        server["last_observed_outage_at"] = online_at
        return state

    plausible_soft_cycle = None
    if (
        is_soft
        and learned_cycle > 0
        and _safe_float(server.get("confidence"), 0.0) < UI_CONFIDENCE_THRESHOLD
    ):
        plausible_soft_cycle = _latest_plausible_soft_cycle_candidate(server, online_at)

    cycle_seconds = match_cycle_bucket(interval_seconds)
    if cycle_seconds is None:
        if plausible_soft_cycle is None:
            reason = "soft_no_cycle_bucket" if is_soft else "no_cycle_bucket"
            _append_raw_outage(server, offline_at, online_at, duration_seconds, not is_soft, reason)
            if learned_cycle > 0 and not projected_contradiction_recorded:
                _append_contradiction(
                    server,
                    observed_at=online_at,
                    contradicted_cycle_seconds=learned_cycle,
                    observed_cycle_seconds=None,
                    reason=reason,
                    soft=is_soft,
                )
                _recalculate_model(server)
            server["last_observed_outage_at"] = online_at
            return state
    elif learned_cycle <= 0 or learned_cycle == int(cycle_seconds):
        plausible_soft_cycle = None

    if plausible_soft_cycle is not None:
        anchor_event, plausible_interval_seconds, plausible_cycle_seconds = plausible_soft_cycle
        _append_raw_outage(
            server,
            offline_at,
            online_at,
            duration_seconds,
            True,
            "soft_cycle_match",
        )
        _append_restart_event(
            server,
            offline_at=offline_at,
            online_at=online_at,
            duration_seconds=duration_seconds,
            interval_seconds=plausible_interval_seconds,
            cycle_seconds=plausible_cycle_seconds,
            model_generation=_safe_int(anchor_event.get("model_generation"), _model_generation(server)),
            soft=True,
            evidence_kind="direct",
            coverage_quality=_interval_coverage_quality(server, anchor_event, online_at),
            interval_start_at=_safe_int(anchor_event.get("online_at"), 0),
        )
        if learned_cycle > 0 and learned_cycle != plausible_cycle_seconds:
            _append_contradiction(
                server,
                observed_at=online_at,
                contradicted_cycle_seconds=learned_cycle,
                observed_cycle_seconds=plausible_cycle_seconds,
                reason="soft_cycle_mismatch",
                soft=True,
            )
        _recalculate_model(server)
        server["last_restart_event_at"] = online_at
        server["last_observed_outage_at"] = online_at
        return state

    if learned_cycle > 0 and learned_cycle != int(cycle_seconds):
        reason = "soft_cycle_mismatch" if is_soft else "cycle_mismatch"
        _append_raw_outage(server, offline_at, online_at, duration_seconds, True, reason)
        _append_contradiction(
            server,
            observed_at=online_at,
            contradicted_cycle_seconds=learned_cycle,
            observed_cycle_seconds=cycle_seconds,
            reason=reason,
            soft=is_soft,
        )
        _append_restart_event(
            server,
            offline_at=offline_at,
            online_at=online_at,
            duration_seconds=duration_seconds,
            interval_seconds=interval_seconds,
            cycle_seconds=cycle_seconds,
            model_generation=_model_generation(server),
            soft=is_soft,
            evidence_kind="direct",
            coverage_quality=_interval_coverage_quality(server, previous_event, online_at),
            interval_start_at=_safe_int(previous_event.get("online_at"), 0),
        )
        _recalculate_model(server)
        server["last_restart_event_at"] = online_at
        server["last_observed_outage_at"] = online_at
        return state

    _append_raw_outage(
        server,
        offline_at,
        online_at,
        duration_seconds,
        True,
        "soft_cycle_match" if is_soft else None,
    )
    _append_restart_event(
        server,
        offline_at=offline_at,
        online_at=online_at,
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        cycle_seconds=cycle_seconds,
        model_generation=_model_generation(server),
        soft=is_soft,
        evidence_kind="direct",
        coverage_quality=_interval_coverage_quality(server, previous_event, online_at),
        interval_start_at=_safe_int(previous_event.get("online_at"), 0),
    )
    _recalculate_model(server)
    server["last_restart_event_at"] = online_at
    server["last_observed_outage_at"] = online_at
    return state


def record_query_visible_restart(state: dict | None, key: str, event: dict) -> dict:
    state = normalize_state(state)
    if not key:
        return state

    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server

    now = int(time.time())
    name = str((event or {}).get("name") or server.get("name") or "")
    map_name = str((event or {}).get("map") or server.get("map") or "")
    zero_start_at = _safe_int((event or {}).get("zero_start_at"), 0)
    players_return_at = _safe_int((event or {}).get("players_return_at"), 0)
    duration_seconds = _safe_int((event or {}).get("duration_seconds"), 0)
    pre_zero_players = _safe_int((event or {}).get("pre_zero_players"), 0)
    return_players = _safe_int((event or {}).get("return_players"), 0)
    if duration_seconds <= 0 and zero_start_at > 0 and players_return_at >= zero_start_at:
        duration_seconds = players_return_at - zero_start_at

    server["name"] = name
    server["map"] = map_name
    server["updated_at"] = now

    if zero_start_at <= 0 or players_return_at <= 0 or players_return_at < zero_start_at:
        _append_raw_query_visible_restart(
            server,
            zero_start_at,
            players_return_at,
            duration_seconds,
            pre_zero_players,
            return_players,
            False,
            "invalid_event",
        )
        return state

    if not (MIN_QUERY_VISIBLE_ZERO_SECONDS <= duration_seconds <= MAX_QUERY_VISIBLE_ZERO_SECONDS):
        _append_raw_query_visible_restart(
            server,
            zero_start_at,
            players_return_at,
            duration_seconds,
            pre_zero_players,
            return_players,
            False,
            "duration_out_of_range",
        )
        return state

    previous_event = _latest_restart_event(server)
    interval_seconds = None
    cycle_seconds = None
    if previous_event is not None:
        interval_seconds = players_return_at - _safe_int(previous_event.get("online_at"), players_return_at)
        if interval_seconds > 0:
            cycle_seconds = match_cycle_bucket(interval_seconds)
            learned_cycle = _safe_int(server.get("learned_cycle_seconds"), 0)
            if (
                cycle_seconds is not None
                and learned_cycle > 0
                and learned_cycle < int(cycle_seconds)
                and has_unmonitored_expected_windows_between(
                    server,
                    _safe_int(previous_event.get("online_at"), 0),
                    players_return_at,
                    learned_cycle,
                )
            ):
                cycle_seconds = None
                evidence_weight = 0.0
                reject_reason = "unwatched_intermediate_windows"
            else:
                evidence_weight = QUERY_VISIBLE_EVIDENCE_WEIGHT
                reject_reason = None
        else:
            evidence_weight = QUERY_VISIBLE_EVIDENCE_WEIGHT
            reject_reason = None
    else:
        evidence_weight = QUERY_VISIBLE_EVIDENCE_WEIGHT
        reject_reason = None

    _append_raw_query_visible_restart(
        server,
        zero_start_at,
        players_return_at,
        duration_seconds,
        pre_zero_players,
        return_players,
        True,
        reject_reason,
    )
    _append_restart_event(
        server,
        offline_at=zero_start_at,
        online_at=players_return_at,
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        cycle_seconds=cycle_seconds,
        model_generation=_model_generation(server),
        source="query_visible",
        evidence_weight=evidence_weight,
        pre_zero_players=pre_zero_players,
        return_players=return_players,
        name=name,
        map_name=map_name,
        reject_reason=reject_reason,
        interval_start_at=_safe_int((previous_event or {}).get("online_at"), 0),
    )
    server["last_restart_event_at"] = players_return_at
    server["last_query_visible_restart_at"] = players_return_at
    server["query_visible_event_count"] = _safe_int(server.get("query_visible_event_count"), 0) + 1
    if cycle_seconds is not None:
        _recalculate_model(server)
    elif previous_event is None:
        server["confidence"] = 0.0
        server["matching_event_count"] = 0
        server["expected_restart_minutes_of_day"] = []
    return state


def record_monitor_started(state: dict | None, key: str, now=None) -> dict:
    state = normalize_state(state)
    if not key:
        return state
    now = _safe_int(time.time() if now is None else now, 0)
    if now <= 0:
        return state
    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server
    active = _active_monitor_session(server)
    if active is None:
        server.setdefault("monitor_sessions", []).append({
            "started_at": now,
            "last_heartbeat_at": now,
            "ended_at": None,
            "ended_reason": None,
        })
    elif _monitor_session_stale(active, now):
        last_heartbeat_at = _safe_int(active.get("last_heartbeat_at"), 0)
        active["ended_at"] = last_heartbeat_at
        active["ended_reason"] = "heartbeat_gap"
        server.setdefault("monitor_sessions", []).append({
            "started_at": now,
            "last_heartbeat_at": now,
            "ended_at": None,
            "ended_reason": None,
        })
    else:
        active["last_heartbeat_at"] = max(_safe_int(active.get("last_heartbeat_at"), now), now)
    _trim_monitor_sessions(server)
    server["updated_at"] = now
    return state


def record_monitor_heartbeat(state: dict | None, key: str, now=None) -> dict:
    state = normalize_state(state)
    if not key:
        return state
    now = _safe_int(time.time() if now is None else now, 0)
    if now <= 0:
        return state
    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server
    active = _active_monitor_session(server)
    if active is None:
        server.setdefault("monitor_sessions", []).append({
            "started_at": now,
            "last_heartbeat_at": now,
            "ended_at": None,
            "ended_reason": None,
        })
    elif _monitor_session_stale(active, now):
        last_heartbeat_at = _safe_int(active.get("last_heartbeat_at"), 0)
        active["ended_at"] = last_heartbeat_at
        active["ended_reason"] = "heartbeat_gap"
        server.setdefault("monitor_sessions", []).append({
            "started_at": now,
            "last_heartbeat_at": now,
            "ended_at": None,
            "ended_reason": None,
        })
    else:
        active["last_heartbeat_at"] = max(_safe_int(active.get("last_heartbeat_at"), now), now)
    _trim_monitor_sessions(server)
    server["updated_at"] = now
    return state


def record_monitor_ended(state: dict | None, key: str, reason: str, now=None) -> dict:
    state = normalize_state(state)
    if not key:
        return state
    now = _safe_int(time.time() if now is None else now, 0)
    if now <= 0:
        return state
    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server
    active = _active_monitor_session(server)
    if active is not None:
        last_heartbeat_at = _safe_int(active.get("last_heartbeat_at"), 0)
        if _monitor_session_stale(active, now):
            active["ended_at"] = last_heartbeat_at
        else:
            active["last_heartbeat_at"] = max(last_heartbeat_at, now)
            active["ended_at"] = now
        active["ended_reason"] = str(reason or "")
    _trim_monitor_sessions(server)
    server["updated_at"] = now
    return state


def server_monitored_window(
    server: dict | None,
    window_start,
    window_end,
    max_gap_seconds=None,
) -> dict:
    server = _normalize_server(server)
    window_start = _safe_int(window_start, 0)
    window_end = _safe_int(window_end, 0)
    max_gap_seconds = _safe_int(
        MONITOR_MAX_COVERAGE_GAP_SECONDS if max_gap_seconds is None else max_gap_seconds,
        MONITOR_MAX_COVERAGE_GAP_SECONDS,
    )
    result = {
        "covered": False,
        "coverage_ratio": 0.0,
        "max_gap_seconds": max(0, max_gap_seconds),
        "observed_session_count": 0,
    }
    if window_start <= 0 or window_end <= window_start:
        return result

    segments = []
    for session in server.get("monitor_sessions") or []:
        if not isinstance(session, dict):
            continue
        started_at = _safe_int(session.get("started_at"), 0)
        ended_at = _safe_int(session.get("ended_at"), 0)
        last_heartbeat_at = _safe_int(session.get("last_heartbeat_at"), 0)
        session_end = ended_at if ended_at > 0 else last_heartbeat_at
        if started_at <= 0 or session_end < started_at:
            continue
        start = max(window_start, started_at)
        end = min(window_end, session_end)
        if end >= start:
            segments.append((start, end))

    result["observed_session_count"] = len(segments)
    if not segments:
        result["max_gap_seconds"] = window_end - window_start
        return result

    segments.sort()
    merged = []
    for start, end in segments:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    covered_seconds = sum(max(0, end - start) for start, end in merged)
    max_gap = max(0, merged[0][0] - window_start, window_end - merged[-1][1])
    for previous, current in zip(merged, merged[1:]):
        max_gap = max(max_gap, max(0, current[0] - previous[1]))
    coverage_ratio = covered_seconds / float(max(1, window_end - window_start))
    result["coverage_ratio"] = round(max(0.0, min(1.0, coverage_ratio)), 3)
    result["max_gap_seconds"] = int(max_gap)
    result["covered"] = (
        coverage_ratio >= float(MONITOR_WINDOW_MIN_COVERAGE_RATIO)
        and max_gap <= max(0, max_gap_seconds)
    )
    return result


def has_unmonitored_expected_windows_between(
    server: dict | None,
    anchor_at,
    event_at,
    candidate_cycle_seconds,
) -> bool:
    server = _normalize_server(server)
    anchor_at = _safe_int(anchor_at, 0)
    event_at = _safe_int(event_at, 0)
    candidate_cycle_seconds = _safe_int(candidate_cycle_seconds, 0)
    if anchor_at <= 0 or event_at <= anchor_at or candidate_cycle_seconds <= 0:
        return False
    tolerance = projected_window_tolerance_seconds(candidate_cycle_seconds)
    expected_at = anchor_at + candidate_cycle_seconds
    while expected_at + tolerance < event_at:
        coverage = server_monitored_window(
            server,
            expected_at - tolerance,
            expected_at + tolerance,
        )
        if not bool(coverage.get("covered", False)):
            return True
        expected_at += candidate_cycle_seconds
    return False


def projected_multiple_evidence_weight(
    server: dict | None,
    anchor_at,
    event_at,
    cycle_seconds,
    cycle_count,
    *,
    soft: bool,
) -> tuple[float, str]:
    """Return cautious exact-cycle evidence for a projected multiple."""
    base_weight = 0.5 if soft else 1.0
    cycle_count = _safe_int(cycle_count, 0)
    if cycle_count <= 1:
        return base_weight, "direct"
    if has_unmonitored_expected_windows_between(server, anchor_at, event_at, cycle_seconds):
        return round(base_weight * PROJECTED_UNMONITORED_WEIGHT_FACTOR, 2), "unmonitored_multiple"
    # Even covered multiples share an anchor and are weaker than an adjacent cycle.
    weight = base_weight * PROJECTED_COVERED_WEIGHT_FACTOR / max(1.0, cycle_count / 2.0)
    return round(max(0.05, weight), 2), "covered_multiple"


def _interval_coverage_quality(server: dict, previous_event: dict | None, online_at: int) -> str:
    anchor_at = _safe_int((previous_event or {}).get("online_at"), 0)
    if anchor_at <= 0 or online_at <= anchor_at:
        return "unknown"
    coverage = server_monitored_window(server, anchor_at, online_at)
    return "covered_direct" if bool(coverage.get("covered", False)) else "uncovered_direct"


def record_expected_window_miss(state: dict | None, key: str, miss: dict) -> dict:
    state = normalize_state(state)
    if not key:
        return state

    servers = state.setdefault("servers", {})
    server = _normalize_server(servers.get(key))
    servers[str(key)] = server

    expected_at = _safe_int((miss or {}).get("expected_at"), 0)
    window_start = _safe_int((miss or {}).get("window_start"), 0)
    window_end = _safe_int((miss or {}).get("window_end"), 0)
    cycle_seconds = _safe_int((miss or {}).get("cycle_seconds"), 0)
    model_generation = _safe_int((miss or {}).get("model_generation"), _model_generation(server))
    observed_poll_count = _safe_int((miss or {}).get("observed_poll_count"), 0)
    coverage_ratio = _safe_float((miss or {}).get("coverage_ratio"), 0.0)
    max_gap_seconds = _safe_int((miss or {}).get("max_gap_seconds"), 0)
    if (
        expected_at <= 0
        or window_start <= 0
        or window_end <= window_start
        or cycle_seconds <= 0
        or model_generation <= 0
        or observed_poll_count <= 0
        or coverage_ratio <= 0.0
    ):
        return state

    if _append_expected_window_miss(server, {
        "expected_at": expected_at,
        "window_start": window_start,
        "window_end": window_end,
        "cycle_seconds": cycle_seconds,
        "model_generation": model_generation,
        "observed_poll_count": observed_poll_count,
        "coverage_ratio": round(max(0.0, min(1.0, coverage_ratio)), 3),
        "max_gap_seconds": max(0, max_gap_seconds),
    }):
        server["last_mismatch_at"] = expected_at
        server["updated_at"] = int(time.time())
        _recalculate_model(server)

    return state


def has_observed_miss_between(
    server: dict | None,
    anchor_at: int | float,
    outage_at: int | float,
    cycle_seconds: int | float,
    generation: int | float,
) -> bool:
    server = _normalize_server(server)
    anchor_at = _safe_int(anchor_at, 0)
    outage_at = _safe_int(outage_at, 0)
    cycle_seconds = _safe_int(cycle_seconds, 0)
    generation = _safe_int(generation, 0)
    if anchor_at <= 0 or outage_at <= anchor_at or cycle_seconds <= 0 or generation <= 0:
        return False
    for miss in server.get("expected_window_misses") or []:
        if not isinstance(miss, dict):
            continue
        if _safe_int(miss.get("cycle_seconds"), 0) != cycle_seconds:
            continue
        if _safe_int(miss.get("model_generation"), 0) != generation:
            continue
        expected_at = _safe_int(miss.get("expected_at"), 0)
        if anchor_at < expected_at < outage_at:
            return True
    return False


def summarize_server(
    state: dict | None,
    key: str,
    now=None,
    *,
    server_online=None,
    offline_since_at=None,
) -> dict | None:
    state = normalize_state(state)
    server = state.get("servers", {}).get(str(key or ""))
    if not isinstance(server, dict):
        return None
    server = _normalize_server(server)

    cycle_seconds = _safe_int(server.get("learned_cycle_seconds"), 0)
    confidence = _safe_float(server.get("confidence"), 0.0)
    last_restart_event_at = _safe_int(server.get("last_restart_event_at"), 0)
    if last_restart_event_at <= 0:
        return None

    try:
        now = int(time.time() if now is None else now)
    except Exception:
        return None

    if not is_established_model(server):
        return None

    if cycle_seconds <= 0:
        return None
    due_restart = last_restart_event_at + cycle_seconds
    offline_reference = now
    if server_online is False:
        offline_reference = _safe_int(offline_since_at, 0) or now
    tolerance = projected_window_tolerance_seconds(cycle_seconds) if server_online is False else 0
    while due_restart + tolerance < offline_reference:
        due_restart += cycle_seconds
    next_restart = due_restart
    if server_online is False and due_restart <= now:
        confidence_percent = max(0, min(100, int(round(confidence * 100))))
        return {
            "cycle_text": _format_cycle_text(cycle_seconds),
            "next_text": f"~{_local_hhmm(due_restart)}",
            "countdown_text": "00:00",
            "confidence_percent": confidence_percent,
        }
    while next_restart <= now:
        next_restart += cycle_seconds
    countdown_seconds = next_restart - now
    if countdown_seconds < 0:
        return None

    confidence_percent = max(0, min(100, int(round(confidence * 100))))
    return {
        "cycle_text": _format_cycle_text(cycle_seconds),
        "next_text": f"~{_local_hhmm(next_restart)}",
        "countdown_text": _format_countdown_text(countdown_seconds),
        "confidence_percent": confidence_percent,
    }


def summarize_alert_usability(state: dict | None, key: str | None) -> dict:
    unavailable = {
        "usable": False,
        "mode": "",
        "message": "No restart pattern learned yet. Keep Server Companion open through a restart to enable alerts.",
    }
    if not key:
        return unavailable

    state = normalize_state(state)
    server = state.get("servers", {}).get(str(key or ""))
    if not isinstance(server, dict):
        return unavailable
    server = _normalize_server(server)

    if _has_confident_query_visible_model(server):
        if DEBUG_SC_ALERTS:
            print("[SC-ALERT] alert usability: preferring query-visible model over outage evidence", flush=True)
        return {"usable": True, "mode": "query_visible", "message": ""}

    if _has_credible_outage_evidence(server):
        return {"usable": True, "mode": "outage", "message": ""}

    if _has_query_visible_evidence(server):
        return {
            "usable": False,
            "mode": "",
            "message": "Learning restart pattern… Alerts will activate once Server Companion is confident.",
        }

    return unavailable


def match_cycle_bucket(interval_seconds: int | float) -> int | None:
    try:
        interval_seconds = int(interval_seconds)
    except Exception:
        return None
    best_bucket = None
    best_delta = None
    for bucket in CYCLE_BUCKETS_SECONDS:
        delta = abs(interval_seconds - bucket)
        tolerance = bucket_tolerance_seconds(bucket)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best_bucket = bucket
            best_delta = delta
    return best_bucket


def bucket_tolerance_seconds(bucket_seconds: int | float) -> int:
    return int(max(15 * 60, round(float(bucket_seconds) * 0.08)))


def projected_window_tolerance_seconds(cycle_seconds: int | float) -> int:
    return int(min(60 * 60, max(30 * 60, round(float(cycle_seconds) * 0.12))))


def nearest_projected_cycle_delta(
    anchor_time: int | float,
    new_time: int | float,
    cycle_seconds: int | float,
) -> tuple[int, int] | None:
    try:
        anchor_time = int(anchor_time)
        new_time = int(new_time)
        cycle_seconds = int(cycle_seconds)
    except Exception:
        return None
    if anchor_time <= 0 or new_time <= anchor_time or cycle_seconds <= 0:
        return None

    elapsed_seconds = new_time - anchor_time
    cycle_count = int((float(elapsed_seconds) / float(cycle_seconds)) + 0.5)
    if cycle_count <= 0:
        return None

    projected_time = anchor_time + (cycle_count * cycle_seconds)
    return cycle_count, new_time - projected_time


def confidence_for_match_count(count: int | float) -> float:
    try:
        count = float(count)
    except Exception:
        count = 0.0
    if count <= 0:
        return 0.0
    if count < 1:
        return 0.10
    if count < 1.5:
        return 0.25
    if count < 2:
        return 0.40
    if count < 2.5:
        return 0.55
    if count < 3:
        return 0.68
    if count < 4:
        return 0.80
    return 0.95


def _format_cycle_text(cycle_seconds: int) -> str:
    cycle_seconds = max(0, _safe_int(cycle_seconds, 0))
    hours = cycle_seconds // 3600
    minutes = (cycle_seconds % 3600) // 60
    if minutes <= 0:
        return f"~{hours}h"
    return f"~{hours}h {minutes}m"


def _format_countdown_text(seconds: int) -> str:
    seconds = max(0, _safe_int(seconds, 0))
    total_minutes = seconds // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def _format_elapsed_text(seconds: int) -> str:
    seconds = max(0, _safe_int(seconds, 0))
    total_minutes = seconds // 60
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    if days > 0:
        return f"~{days}d {hours}h ago"
    if hours > 0:
        return f"~{hours}h {minutes}m ago"
    return f"~{minutes}m ago"


def _normalize_server(server: dict | None) -> dict:
    if not isinstance(server, dict):
        server = {}
    restart_events, restart_events_valid = _normalize_restart_events(server.get("restart_events"))
    candidate_scores, candidate_scores_valid = _normalize_candidate_scores(
        server.get("candidate_scores")
    )
    anchors, anchors_valid = _normalize_anchor_list(
        server.get("independent_direct_interval_anchors")
    )
    scoring_version = _safe_nonnegative_int(server.get("scoring_algorithm_version"), 0)
    diagnostic_fields = (
        "confidence", "confidence_cap", "matching_event_count", "evidence_score",
        "supporting_evidence_score", "contradiction_penalty", "effective_evidence_score",
        "independent_interval_count", "covered_direct_cycle_count", "direct_interval_count",
        "projected_interval_count", "unmonitored_projected_interval_count",
        "covered_projected_cycle_count", "qualifying_covered_evidence_count",
    )
    expected_window_misses, misses_valid = _normalize_expected_window_misses(
        server.get("expected_window_misses")
    )
    contradictions, contradictions_valid = _normalize_contradictions(
        server.get("contradictions")
    )
    diagnostics_valid = all(
        field not in server or _is_nonnegative_number(server.get(field))
        for field in diagnostic_fields
    )
    if not all((candidate_scores_valid, anchors_valid, restart_events_valid,
                misses_valid, contradictions_valid, diagnostics_valid)):
        scoring_version = 0
    learned_min_offline_alert_seconds = _safe_int(
        server.get("learned_min_offline_alert_seconds"),
        0,
    )
    for event in restart_events:
        if not isinstance(event, dict) or str(event.get("source") or "") == "query_visible":
            continue
        cycle_seconds = _safe_int(event.get("cycle_seconds"), 0)
        duration_seconds = _safe_int(event.get("duration_seconds"), 0)
        if cycle_seconds <= 0 or duration_seconds <= 0:
            continue
        candidate = max(30, duration_seconds - 10)
        if learned_min_offline_alert_seconds <= 0:
            learned_min_offline_alert_seconds = candidate
        else:
            learned_min_offline_alert_seconds = min(learned_min_offline_alert_seconds, candidate)
    return {
        "name": str(server.get("name") or ""),
        "map": str(server.get("map") or ""),
        "raw_outages": _bounded_dict_list(server.get("raw_outages"), RAW_OUTAGE_LIMIT),
        "raw_query_visible_restarts": (
            _bounded_dict_list(
                server.get("raw_query_visible_restarts"), RAW_QUERY_VISIBLE_RESTART_LIMIT
            )
        ),
        "restart_events": restart_events,
        "learned_min_offline_alert_seconds": (
            learned_min_offline_alert_seconds
            if learned_min_offline_alert_seconds > 0
            else None
        ),
        "expected_window_misses": expected_window_misses,
        "contradictions": contradictions,
        "monitor_sessions": _normalize_monitor_sessions(server.get("monitor_sessions")),
        "learned_cycle_seconds": (
            _safe_int(server.get("learned_cycle_seconds"), 0)
            if _safe_int(server.get("learned_cycle_seconds"), 0) > 0
            else None
        ),
        "expected_restart_minutes_of_day": _normalize_minute_list(
            server.get("expected_restart_minutes_of_day")
        ),
        "confidence": _safe_bounded_float(server.get("confidence"), 0.0, 0.0, 1.0),
        "matching_event_count": _safe_nonnegative_int(server.get("matching_event_count"), 0),
        "evidence_score": _safe_nonnegative_float(server.get("evidence_score"), 0.0),
        "supporting_evidence_score": _safe_nonnegative_float(
            server.get("supporting_evidence_score"), 0.0
        ),
        "contradiction_penalty": _safe_nonnegative_float(
            server.get("contradiction_penalty"), 0.0
        ),
        "effective_evidence_score": _safe_nonnegative_float(
            server.get("effective_evidence_score"), 0.0
        ),
        "confidence_cap": _safe_bounded_float(server.get("confidence_cap"), 1.0, 0.0, 1.0),
        "independent_interval_count": _safe_nonnegative_int(
            server.get("independent_interval_count"), 0
        ),
        "covered_direct_cycle_count": _safe_nonnegative_int(
            server.get("covered_direct_cycle_count"), 0
        ),
        "candidate_scores": candidate_scores,
        "scoring_algorithm_version": scoring_version,
        "direct_interval_count": _safe_nonnegative_int(server.get("direct_interval_count"), 0),
        "projected_interval_count": _safe_nonnegative_int(
            server.get("projected_interval_count"), 0
        ),
        "unmonitored_projected_interval_count": _safe_nonnegative_int(
            server.get("unmonitored_projected_interval_count"), 0
        ),
        "covered_projected_cycle_count": _safe_nonnegative_int(
            server.get("covered_projected_cycle_count"), 0
        ),
        "qualifying_covered_evidence_count": _safe_nonnegative_int(
            server.get("qualifying_covered_evidence_count"), 0
        ),
        "independent_direct_interval_anchors": anchors,
        "model_generation": max(1, _safe_int(server.get("model_generation"), 1)),
        "last_restart_event_at": server.get("last_restart_event_at"),
        "last_observed_outage_at": server.get("last_observed_outage_at"),
        "last_query_visible_restart_at": _safe_int(server.get("last_query_visible_restart_at"), 0),
        "query_visible_event_count": _safe_int(server.get("query_visible_event_count"), 0),
        "last_mismatch_at": server.get("last_mismatch_at"),
        "updated_at": _safe_int(server.get("updated_at"), 0),
    }


def _safe_server_fallback(server) -> dict:
    source = server if isinstance(server, dict) else {}
    fallback = _normalize_server({
        "name": source.get("name"),
        "map": source.get("map"),
        "raw_outages": source.get("raw_outages"),
        "raw_query_visible_restarts": source.get("raw_query_visible_restarts"),
        "restart_events": source.get("restart_events"),
        "expected_window_misses": source.get("expected_window_misses"),
        "contradictions": source.get("contradictions"),
        "monitor_sessions": source.get("monitor_sessions"),
        "model_generation": source.get("model_generation"),
    })
    fallback["learned_cycle_seconds"] = None
    fallback["confidence"] = 0.0
    fallback["scoring_algorithm_version"] = 0
    return fallback


def _bounded_dict_list(value, limit: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)][-limit:]


def _normalize_restart_events(value) -> tuple[list[dict], bool]:
    events = _bounded_dict_list(value, RESTART_EVENT_LIMIT)
    valid = value is None or (
        isinstance(value, list) and all(isinstance(item, dict) for item in value)
    )
    for event in events:
        for field in ("offline_at", "online_at", "duration_seconds", "interval_seconds", "cycle_seconds", "model_generation", "projected_cycle_count", "interval_start_at"):
            if field in event:
                valid = valid and (event.get(field) is None or _is_nonnegative_number(event.get(field)))
                normalized = _safe_nonnegative_int(event.get(field), 0)
                event[field] = normalized if normalized > 0 else None
        if "evidence_weight" in event:
            weight = _optional_nonnegative_float(event.get("evidence_weight"))
            if weight is None:
                valid = False
                event.pop("evidence_weight", None)
            else:
                event["evidence_weight"] = weight
    return events, valid


def _normalize_expected_window_misses(value) -> tuple[list[dict], bool]:
    misses = _bounded_dict_list(value, EXPECTED_WINDOW_MISS_LIMIT)
    valid = value is None or (
        isinstance(value, list) and all(isinstance(item, dict) for item in value)
    )
    for miss in misses:
        for field in ("expected_at", "window_start", "window_end", "cycle_seconds", "model_generation"):
            if field in miss:
                valid = valid and _is_nonnegative_number(miss.get(field))
                miss[field] = _safe_nonnegative_int(miss.get(field), 0)
    return misses, valid


def _normalize_contradictions(value) -> tuple[list[dict], bool]:
    contradictions = _bounded_dict_list(value, CONTRADICTION_LIMIT)
    valid = value is None or (
        isinstance(value, list) and all(isinstance(item, dict) for item in value)
    )
    for item in contradictions:
        for field in ("observed_at", "contradicted_cycle_seconds", "observed_cycle_seconds", "model_generation"):
            if field in item:
                valid = valid and (
                    item.get(field) is None or _is_nonnegative_number(item.get(field))
                )
                item[field] = _safe_nonnegative_int(item.get(field), 0)
        valid = valid and _is_nonnegative_number(item.get("penalty", 0.0))
        item["penalty"] = _safe_nonnegative_float(item.get("penalty"), 0.0)
        reasons = item.get("reasons")
        item["reasons"] = sorted({str(reason) for reason in reasons if str(reason)}) if isinstance(reasons, list) else []
    return contradictions, valid


def _normalize_candidate_scores(value) -> tuple[dict, bool]:
    if value is None:
        return {}, True
    if not isinstance(value, dict):
        return {}, False
    normalized = {}
    valid = True
    numeric_fields = {
        "supporting_evidence_score", "contradiction_penalty", "effective_evidence_score",
        "confidence_cap", "ambiguous_projected_observation_ratio",
    }
    count_fields = {
        "direct_interval_count", "projected_interval_count", "independent_direct_interval_count",
        "covered_direct_interval_count", "covered_projected_interval_count",
        "qualifying_covered_evidence_count", "unmonitored_projected_interval_count",
        "qualifying_restart_event_count",
    }
    for key, entry in value.items():
        if not isinstance(entry, dict):
            valid = False
            continue
        clean = {}
        for field in numeric_fields:
            if field in entry:
                valid = valid and _is_nonnegative_number(entry.get(field))
                clean[field] = _safe_nonnegative_float(entry.get(field), 0.0)
        if "confidence_cap" in clean:
            clean["confidence_cap"] = min(1.0, clean["confidence_cap"])
        for field in count_fields:
            if field in entry:
                valid = valid and _is_nonnegative_number(entry.get(field))
                clean[field] = _safe_nonnegative_int(entry.get(field), 0)
        anchors, anchors_valid = _normalize_anchor_list(
            entry.get("independent_direct_interval_anchors")
        )
        if "independent_direct_interval_anchors" in entry:
            clean["independent_direct_interval_anchors"] = anchors
            valid = valid and anchors_valid
        normalized[str(key)] = clean
    return normalized, valid


def _normalize_anchor_list(value) -> tuple[list[int], bool]:
    if value is None:
        return [], True
    if not isinstance(value, list):
        return [], False
    return sorted({anchor for item in value if (anchor := _safe_nonnegative_int(item, 0)) > 0}), True


def _normalize_minute_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    return sorted({minute for item in value if 0 <= (minute := _safe_int(item, -1)) < 1440})


def _append_raw_outage(
    server: dict,
    offline_at: int,
    online_at: int,
    duration_seconds: int,
    accepted: bool,
    reject_reason: str | None,
) -> None:
    raw_outages = server.setdefault("raw_outages", [])
    raw_outages.append({
        "offline_at": int(offline_at),
        "online_at": int(online_at),
        "duration_seconds": int(duration_seconds),
        "accepted": bool(accepted),
        "reject_reason": reject_reason,
    })
    del raw_outages[:-RAW_OUTAGE_LIMIT]


def _append_raw_query_visible_restart(
    server: dict,
    zero_start_at: int,
    players_return_at: int,
    duration_seconds: int,
    pre_zero_players: int,
    return_players: int,
    accepted: bool,
    reject_reason: str | None,
) -> None:
    raw_events = server.setdefault("raw_query_visible_restarts", [])
    raw_events.append({
        "zero_start_at": int(zero_start_at),
        "players_return_at": int(players_return_at),
        "duration_seconds": int(duration_seconds),
        "pre_zero_players": int(pre_zero_players),
        "return_players": int(return_players),
        "accepted": bool(accepted),
        "reject_reason": reject_reason,
    })
    del raw_events[:-RAW_QUERY_VISIBLE_RESTART_LIMIT]


def _append_restart_event(
    server: dict,
    *,
    offline_at: int,
    online_at: int,
    duration_seconds: int,
    interval_seconds: int | None,
    cycle_seconds: int | None,
    model_generation: int,
    soft: bool = False,
    source: str | None = None,
    evidence_weight: float | None = None,
    pre_zero_players: int | None = None,
    return_players: int | None = None,
    name: str | None = None,
    map_name: str | None = None,
    reject_reason: str | None = None,
    evidence_kind: str | None = None,
    coverage_quality: str | None = None,
    projected_cycle_count: int | None = None,
    interval_start_at: int | None = None,
) -> None:
    item = {
        "offline_at": int(offline_at),
        "online_at": int(online_at),
        "duration_seconds": int(duration_seconds),
        "interval_seconds": None if interval_seconds is None else int(interval_seconds),
        "cycle_seconds": None if cycle_seconds is None else int(cycle_seconds),
        "local_time_hhmm": _local_hhmm(online_at),
        "minute_of_day": _minute_of_day(online_at),
        "model_generation": int(model_generation),
        "soft": bool(soft),
    }
    if source:
        item["source"] = str(source)
    if evidence_weight is not None:
        item["evidence_weight"] = round(max(0.0, float(evidence_weight)), 2)
    if pre_zero_players is not None:
        item["pre_zero_players"] = int(pre_zero_players)
    if return_players is not None:
        item["return_players"] = int(return_players)
    if name:
        item["name"] = str(name)
    if map_name:
        item["map"] = str(map_name)
    if reject_reason:
        item["reject_reason"] = str(reject_reason)
    if evidence_kind:
        item["evidence_kind"] = str(evidence_kind)
    if coverage_quality:
        item["coverage_quality"] = str(coverage_quality)
    if projected_cycle_count is not None:
        item["projected_cycle_count"] = max(1, int(projected_cycle_count))
    if interval_start_at is not None and _safe_int(interval_start_at, 0) > 0:
        item["interval_start_at"] = int(interval_start_at)
    restart_events = server.setdefault("restart_events", [])
    restart_events.append(item)
    del restart_events[:-RESTART_EVENT_LIMIT]
    if (
        str(source or "") != "query_visible"
        and _safe_int(cycle_seconds, 0) > 0
        and _safe_int(duration_seconds, 0) > 0
    ):
        candidate = max(30, int(duration_seconds) - 10)
        existing = _safe_int(server.get("learned_min_offline_alert_seconds"), 0)
        server["learned_min_offline_alert_seconds"] = (
            min(existing, candidate) if existing > 0 else candidate
        )


def _normalize_monitor_sessions(sessions) -> list[dict]:
    normalized = []
    if not isinstance(sessions, list):
        return normalized
    for session in sessions:
        if not isinstance(session, dict):
            continue
        started_at = _safe_int(session.get("started_at"), 0)
        last_heartbeat_at = _safe_int(session.get("last_heartbeat_at"), 0)
        ended_at = _safe_int(session.get("ended_at"), 0)
        if started_at <= 0:
            continue
        if last_heartbeat_at <= 0:
            last_heartbeat_at = started_at
        item = {
            "started_at": started_at,
            "last_heartbeat_at": max(started_at, last_heartbeat_at),
            "ended_at": None,
            "ended_reason": None,
        }
        if ended_at > 0:
            item["ended_at"] = max(started_at, ended_at)
            item["ended_reason"] = str(session.get("ended_reason") or "")
        normalized.append(item)
    normalized.sort(key=lambda item: _safe_int(item.get("started_at"), 0))
    return normalized[-MONITOR_SESSION_LIMIT:]


def _active_monitor_session(server: dict) -> dict | None:
    for session in reversed(server.get("monitor_sessions") or []):
        if isinstance(session, dict) and _safe_int(session.get("ended_at"), 0) <= 0:
            return session
    return None


def _monitor_session_stale(session: dict, now: int) -> bool:
    last_heartbeat_at = _safe_int(session.get("last_heartbeat_at"), 0)
    if last_heartbeat_at <= 0:
        last_heartbeat_at = _safe_int(session.get("started_at"), 0)
    return last_heartbeat_at > 0 and int(now) - last_heartbeat_at > MONITOR_MAX_COVERAGE_GAP_SECONDS


def _trim_monitor_sessions(server: dict) -> None:
    sessions = server.setdefault("monitor_sessions", [])
    if isinstance(sessions, list):
        del sessions[:-MONITOR_SESSION_LIMIT]


def _append_expected_window_miss(server: dict, miss: dict) -> bool:
    misses = server.setdefault("expected_window_misses", [])
    expected_at = _safe_int(miss.get("expected_at"), 0)
    generation = _safe_int(miss.get("model_generation"), 0)
    cycle_seconds = _safe_int(miss.get("cycle_seconds"), 0)
    for existing in misses:
        if (
            isinstance(existing, dict)
            and _safe_int(existing.get("expected_at"), 0) == expected_at
            and _safe_int(existing.get("model_generation"), 0) == generation
            and _safe_int(existing.get("cycle_seconds"), 0) == cycle_seconds
        ):
            return False
    misses.append(miss)
    del misses[:-EXPECTED_WINDOW_MISS_LIMIT]
    return True


def _latest_current_generation_restart_event(server: dict) -> dict | None:
    generation = _model_generation(server)
    for event in reversed(server.get("restart_events") or []):
        if (
            isinstance(event, dict)
            and _safe_int(event.get("model_generation"), 0) == generation
            and _safe_int(event.get("online_at"), 0) > 0
        ):
            return event
    return None


def _latest_restart_event(server: dict) -> dict | None:
    latest_event = _latest_current_generation_restart_event(server)
    mismatch_anchor = _latest_cycle_mismatch_raw_outage(server)
    if (
        _safe_int(server.get("learned_cycle_seconds"), 0) > 0
        and mismatch_anchor is not None
        and _safe_int(mismatch_anchor.get("online_at"), 0) > _safe_int((latest_event or {}).get("online_at"), 0)
    ):
        return mismatch_anchor
    return latest_event


def _latest_cycle_mismatch_raw_outage(server: dict) -> dict | None:
    for outage in reversed(server.get("raw_outages") or []):
        if (
            isinstance(outage, dict)
            and outage.get("reject_reason") == "cycle_mismatch"
            and _safe_int(outage.get("online_at"), 0) > 0
        ):
            return outage
    return None


def _latest_matching_soft_anchor_candidate(server: dict, online_at: int) -> tuple[dict, int, int] | None:
    online_at = _safe_int(online_at, 0)
    if online_at <= 0:
        return None

    for outage in reversed(server.get("raw_outages") or []):
        if not isinstance(outage, dict):
            continue
        if outage.get("reject_reason") != "soft_no_anchor":
            continue

        candidate_online_at = _safe_int(outage.get("online_at"), 0)
        candidate_duration = _safe_int(outage.get("duration_seconds"), 0)
        if candidate_online_at <= 0 or candidate_online_at >= online_at:
            continue
        if not (MIN_SOFT_RESTART_OUTAGE_SECONDS <= candidate_duration < MIN_RESTART_OUTAGE_SECONDS):
            continue

        interval_seconds = online_at - candidate_online_at
        if interval_seconds > MAX_RESTART_INTERVAL_SECONDS:
            return None

        cycle_seconds = match_cycle_bucket(interval_seconds)
        if cycle_seconds is not None:
            return outage, interval_seconds, cycle_seconds
    return None


def _latest_plausible_soft_cycle_candidate(server: dict, online_at: int) -> tuple[dict, int, int] | None:
    online_at = _safe_int(online_at, 0)
    if online_at <= 0:
        return None

    candidates = []
    generation = _model_generation(server)
    for event in reversed(server.get("restart_events") or []):
        if not isinstance(event, dict):
            continue
        if _safe_int(event.get("model_generation"), 0) != generation:
            continue

        event_online_at = _safe_int(event.get("online_at"), 0)
        if event_online_at <= 0 or event_online_at >= online_at:
            continue
        if online_at - event_online_at > MAX_LEARNING_EVENT_AGE_SECONDS:
            continue

        interval_seconds = online_at - event_online_at
        if interval_seconds < MIN_RESTART_INTERVAL_SECONDS or interval_seconds > MAX_RESTART_INTERVAL_SECONDS:
            continue

        cycle_seconds = match_cycle_bucket(interval_seconds)
        if cycle_seconds is None:
            continue
        if has_observed_miss_between(server, event_online_at, online_at, cycle_seconds, generation):
            continue
        candidates.append((event, interval_seconds, cycle_seconds))

    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[2], item[1]))[0]


def _has_restart_event_at(server: dict, online_at: int) -> bool:
    online_at = _safe_int(online_at, 0)
    if online_at <= 0:
        return False
    return any(
        isinstance(event, dict) and _safe_int(event.get("online_at"), 0) == online_at
        for event in (server.get("restart_events") or [])
    )


def _has_credible_outage_evidence(server: dict) -> bool:
    for outage in server.get("raw_outages") or []:
        if not isinstance(outage, dict):
            continue
        if bool(outage.get("accepted", False)):
            return True
        if outage.get("reject_reason") == "short_outage":
            continue
        if _safe_int(outage.get("duration_seconds"), 0) >= MIN_SOFT_RESTART_OUTAGE_SECONDS:
            return True

    for event in server.get("restart_events") or []:
        if isinstance(event, dict) and str(event.get("source") or "") != "query_visible":
            return True
    return False


def _has_confident_query_visible_model(server: dict) -> bool:
    confidence = _safe_float(server.get("confidence"), 0.0)
    learned_cycle = _safe_int(server.get("learned_cycle_seconds"), 0)
    if confidence < UI_CONFIDENCE_THRESHOLD or learned_cycle <= 0:
        return False

    generation = _model_generation(server)
    for event in server.get("restart_events") or []:
        if (
            isinstance(event, dict)
            and str(event.get("source") or "") == "query_visible"
            and _safe_int(event.get("model_generation"), 0) == generation
            and _safe_int(event.get("cycle_seconds"), 0) == learned_cycle
        ):
            return True
    return False


def _has_query_visible_evidence(server: dict) -> bool:
    for event in server.get("restart_events") or []:
        if isinstance(event, dict) and str(event.get("source") or "") == "query_visible":
            return True
    return any(isinstance(item, dict) for item in (server.get("raw_query_visible_restarts") or []))


def _append_contradiction(
    server: dict,
    *,
    observed_at: int,
    contradicted_cycle_seconds: int,
    observed_cycle_seconds: int | None,
    reason: str,
    soft: bool,
) -> None:
    penalty = SOFT_CONTRADICTION_PENALTY if soft else HARD_CONTRADICTION_PENALTY
    if observed_cycle_seconds is None:
        penalty = min(penalty, NO_CYCLE_CONTRADICTION_PENALTY)
    item = {
        "observed_at": int(observed_at),
        "contradicted_cycle_seconds": int(contradicted_cycle_seconds),
        "observed_cycle_seconds": (
            None if observed_cycle_seconds is None else int(observed_cycle_seconds)
        ),
        "reason": str(reason),
        "reasons": [str(reason)],
        "soft": bool(soft),
        "penalty": round(penalty, 2),
        "model_generation": _model_generation(server),
    }
    contradictions = server.setdefault("contradictions", [])
    existing_item = next((
        existing for existing in contradictions
        if isinstance(existing, dict)
        and _safe_int(existing.get("observed_at"), 0) == item["observed_at"]
        and _safe_int(existing.get("contradicted_cycle_seconds"), 0)
        == item["contradicted_cycle_seconds"]
    ), None)
    if existing_item is None:
        contradictions.append(item)
        del contradictions[:-CONTRADICTION_LIMIT]
    else:
        reasons = {
            str(value) for value in (existing_item.get("reasons") or []) if str(value)
        }
        prior_reason = str(existing_item.get("reason") or "")
        if prior_reason:
            reasons.add(prior_reason)
        reasons.add(str(reason))
        existing_item["reasons"] = sorted(reasons)
        existing_item["reason"] = str(reason)
        if (
            _safe_int(existing_item.get("observed_cycle_seconds"), 0) <= 0
            and item["observed_cycle_seconds"] is not None
        ):
            existing_item["observed_cycle_seconds"] = item["observed_cycle_seconds"]
        if penalty > _safe_float(existing_item.get("penalty"), 0.0):
            existing_item["penalty"] = round(penalty, 2)
            existing_item["soft"] = bool(soft)
            existing_item["observed_cycle_seconds"] = item["observed_cycle_seconds"]
    server["last_mismatch_at"] = int(observed_at)


def _event_evidence_kind(event: dict, cycle: int) -> str:
    kind = str(event.get("evidence_kind") or "")
    if kind:
        return kind
    projected = nearest_projected_cycle_delta(
        _safe_int(event.get("online_at"), 0) - _safe_int(event.get("interval_seconds"), 0),
        _safe_int(event.get("online_at"), 0),
        cycle,
    )
    return "projected_multiple" if projected is not None and projected[0] > 1 else "direct"


def _event_interval_pair(event: dict) -> tuple[int, int] | None:
    end_at = _safe_int(event.get("online_at"), 0)
    start_at = _safe_int(event.get("interval_start_at"), 0)
    if start_at <= 0:
        interval_seconds = _safe_int(event.get("interval_seconds"), 0)
        if end_at > 0 and interval_seconds > 0:
            start_at = end_at - interval_seconds
    if start_at <= 0 or end_at <= start_at:
        return None
    return start_at, end_at


def _event_evidence_weight(event: dict, cycle: int) -> float:
    stored_weight = _optional_nonnegative_float(event.get("evidence_weight"))
    if stored_weight is not None:
        return stored_weight

    base_weight = 0.5 if event.get("soft") else 1.0
    if _event_evidence_kind(event, cycle) != "projected_multiple":
        return base_weight

    cycle_count = _safe_nonnegative_int(event.get("projected_cycle_count"), 0)
    pair = _event_interval_pair(event)
    if cycle_count <= 1 and pair is not None and cycle > 0:
        cycle_count = max(2, int(round((pair[1] - pair[0]) / float(cycle))))
    cycle_count = max(2, cycle_count)
    multiple_discount = max(1.0, cycle_count / 2.0)
    if str(event.get("coverage_quality") or "") == "covered_multiple":
        return round(max(0.05, base_weight * PROJECTED_COVERED_WEIGHT_FACTOR / multiple_discount), 2)
    # Missing or malformed coverage is ambiguous, so incomplete projected
    # records receive weak support and an additional long-multiple discount.
    return round(max(0.0, base_weight * PROJECTED_UNMONITORED_WEIGHT_FACTOR / multiple_discount), 2)


def _candidate_evidence_diagnostics(cycle: int, events: list[dict]) -> dict:
    useful = [
        event for event in events
        if _event_evidence_weight(event, cycle) > 0.0
    ]
    direct_pairs = {
        pair for event in useful
        if _event_evidence_kind(event, cycle) == "direct"
        for pair in [_event_interval_pair(event)] if pair is not None
    }
    projected_pairs = {
        pair for event in useful
        if _event_evidence_kind(event, cycle) == "projected_multiple"
        for pair in [_event_interval_pair(event)] if pair is not None
    }
    # One reused anchor supplies at most one independent direct interval. An
    # adjacent A->B, B->C chain still has distinct anchors and counts normally.
    independent_anchors = sorted({start for start, _end in direct_pairs})
    covered_direct_count = sum(
        1 for event in useful
        if _event_evidence_kind(event, cycle) == "direct"
        and str(event.get("coverage_quality") or "") == "covered_direct"
    )
    covered_projected_count = sum(
        1 for event in useful
        if _event_evidence_kind(event, cycle) == "projected_multiple"
        and str(event.get("coverage_quality") or "") == "covered_multiple"
    )
    unmonitored_projected_count = sum(
        1 for event in useful
        if _event_evidence_kind(event, cycle) == "projected_multiple"
        and str(event.get("coverage_quality") or "") in {"", "unmonitored_multiple"}
    )
    observation_count = len(direct_pairs) + len(projected_pairs)
    ambiguous_ratio = unmonitored_projected_count / float(max(1, observation_count))
    return {
        "direct_interval_count": len(direct_pairs),
        "projected_interval_count": len(projected_pairs),
        "independent_direct_interval_count": len(independent_anchors),
        "independent_direct_interval_anchors": independent_anchors,
        "covered_direct_interval_count": covered_direct_count,
        "covered_projected_interval_count": covered_projected_count,
        "qualifying_covered_evidence_count": covered_direct_count + covered_projected_count,
        "unmonitored_projected_interval_count": unmonitored_projected_count,
        "ambiguous_projected_observation_ratio": round(ambiguous_ratio, 3),
    }


def _confidence_cap_for_cycle(
    cycle: int,
    events: list[dict],
    contradiction_penalty: float,
    supporting_score: float,
) -> tuple[float, dict]:
    diagnostics = _candidate_evidence_diagnostics(cycle, events)
    interval_count = diagnostics["independent_direct_interval_count"]
    restart_count = interval_count + (1 if interval_count else 0)
    covered_direct_count = diagnostics["covered_direct_interval_count"]
    cap = 1.0
    if interval_count <= 1:
        cap = min(cap, ONE_INTERVAL_CONFIDENCE_CAP)
    elif interval_count == 2:
        cap = min(cap, TWO_INTERVAL_CONFIDENCE_CAP)
    if restart_count < 4:
        cap = min(cap, FEWER_THAN_FOUR_EVENTS_CONFIDENCE_CAP)
    if covered_direct_count <= 0:
        cap = min(cap, NO_COVERED_CYCLE_CONFIDENCE_CAP)
    if (
        covered_direct_count <= 0
        and diagnostics["unmonitored_projected_interval_count"] >= AMBIGUOUS_PROJECTED_MINIMUM_COUNT
        and diagnostics["ambiguous_projected_observation_ratio"]
        >= AMBIGUOUS_PROJECTED_OBSERVATION_RATIO
    ):
        cap = min(cap, ONLY_UNMONITORED_MULTIPLES_CONFIDENCE_CAP)
    if contradiction_penalty >= max(1.0, supporting_score * 0.4):
        cap = min(cap, 0.60)
    if contradiction_penalty >= supporting_score and contradiction_penalty > 0:
        cap = min(cap, 0.35)

    if cycle == TWELVE_HOUR_CYCLE_SECONDS:
        if restart_count < 5:
            cap = min(cap, 0.45)
        elif restart_count < 10:
            cap = min(cap, 0.60)
        if covered_direct_count < 3:
            cap = min(cap, 0.60)
        if restart_count < 10 or covered_direct_count < 6:
            cap = min(cap, 0.79)
    elif cycle == 6 * 3600 and restart_count < 5:
        cap = min(cap, 0.60)
    elif cycle in {3 * 3600, 4 * 3600} and restart_count < 3:
        cap = min(cap, 0.45)
    diagnostics["qualifying_restart_event_count"] = restart_count
    return round(cap, 2), diagnostics


def _recalculate_model(server: dict) -> None:
    generation = _model_generation(server)
    events = [
        event for event in (server.get("restart_events") or [])
        if isinstance(event, dict)
        and _safe_int(event.get("model_generation"), 0) == generation
        and event.get("cycle_seconds") is not None
    ]
    counts = {}
    for event in events:
        cycle = _safe_int(event.get("cycle_seconds"), 0)
        if cycle > 0:
            counts[cycle] = counts.get(cycle, 0) + 1
    if not counts:
        server["learned_cycle_seconds"] = None
        server["expected_restart_minutes_of_day"] = []
        server["confidence"] = 0.0
        server["matching_event_count"] = 0
        server["evidence_score"] = 0.0
        server["supporting_evidence_score"] = 0.0
        server["contradiction_penalty"] = 0.0
        server["effective_evidence_score"] = 0.0
        server["confidence_cap"] = 1.0
        server["independent_interval_count"] = 0
        server["covered_direct_cycle_count"] = 0
        server["covered_projected_cycle_count"] = 0
        server["qualifying_covered_evidence_count"] = 0
        server["direct_interval_count"] = 0
        server["projected_interval_count"] = 0
        server["unmonitored_projected_interval_count"] = 0
        server["independent_direct_interval_anchors"] = []
        server["candidate_scores"] = {}
        server["scoring_algorithm_version"] = SCORING_ALGORITHM_VERSION
        return

    evidence = {}
    for event in events:
        cycle = _safe_int(event.get("cycle_seconds"), 0)
        if cycle > 0:
            weight = _event_evidence_weight(event, cycle)
            evidence[cycle] = evidence.get(cycle, 0.0) + max(0.0, weight)

    miss_penalties = {}
    for miss in server.get("expected_window_misses") or []:
        if not isinstance(miss, dict):
            continue
        if _safe_int(miss.get("model_generation"), 0) != generation:
            continue
        cycle = _safe_int(miss.get("cycle_seconds"), 0)
        if cycle > 0:
            miss_penalties[cycle] = miss_penalties.get(cycle, 0.0) + 1.0

    contradiction_penalties = {}
    for contradiction in server.get("contradictions") or []:
        if not isinstance(contradiction, dict):
            continue
        if _safe_int(contradiction.get("model_generation"), generation) != generation:
            continue
        cycle = _safe_int(contradiction.get("contradicted_cycle_seconds"), 0)
        if cycle > 0:
            contradiction_penalties[cycle] = contradiction_penalties.get(cycle, 0.0) + max(
                0.0, _safe_float(contradiction.get("penalty"), 0.0)
            )

    net_evidence = {
        cycle: max(
            0.0,
            score - miss_penalties.get(cycle, 0.0) - contradiction_penalties.get(cycle, 0.0),
        )
        for cycle, score in evidence.items()
    }
    candidate_scores = {}
    for cycle, score in net_evidence.items():
        cycle_events = [event for event in events if _safe_int(event.get("cycle_seconds"), 0) == cycle]
        cycle_penalty = miss_penalties.get(cycle, 0.0) + contradiction_penalties.get(cycle, 0.0)
        cap, diagnostics = _confidence_cap_for_cycle(
            cycle, cycle_events, cycle_penalty, evidence.get(cycle, 0.0)
        )
        candidate_scores[str(cycle)] = {
            "supporting_evidence_score": round(evidence.get(cycle, 0.0), 2),
            "contradiction_penalty": round(cycle_penalty, 2),
            "effective_evidence_score": round(score, 2),
            "confidence_cap": cap,
            **diagnostics,
        }
    server["candidate_scores"] = candidate_scores

    learned_cycle, evidence_score = sorted(net_evidence.items(), key=lambda item: (-item[1], item[0]))[0]
    matching_count = counts.get(learned_cycle, 0)
    server["learned_cycle_seconds"] = learned_cycle
    server["matching_event_count"] = matching_count
    server["evidence_score"] = round(evidence_score, 2)
    supporting_score = evidence.get(learned_cycle, 0.0)
    contradiction_penalty = (
        miss_penalties.get(learned_cycle, 0.0)
        + contradiction_penalties.get(learned_cycle, 0.0)
    )
    selected_diagnostics = candidate_scores[str(learned_cycle)]
    confidence_cap = _safe_float(selected_diagnostics.get("confidence_cap"), 1.0)
    server["supporting_evidence_score"] = round(supporting_score, 2)
    server["contradiction_penalty"] = round(contradiction_penalty, 2)
    server["effective_evidence_score"] = round(evidence_score, 2)
    server["confidence_cap"] = confidence_cap
    server["independent_interval_count"] = _safe_int(
        selected_diagnostics.get("independent_direct_interval_count"), 0
    )
    server["direct_interval_count"] = _safe_int(selected_diagnostics.get("direct_interval_count"), 0)
    server["projected_interval_count"] = _safe_int(
        selected_diagnostics.get("projected_interval_count"), 0
    )
    server["unmonitored_projected_interval_count"] = _safe_int(
        selected_diagnostics.get("unmonitored_projected_interval_count"), 0
    )
    server["covered_direct_cycle_count"] = _safe_int(
        selected_diagnostics.get("covered_direct_interval_count"), 0
    )
    server["covered_projected_cycle_count"] = _safe_int(
        selected_diagnostics.get("covered_projected_interval_count"), 0
    )
    server["qualifying_covered_evidence_count"] = _safe_int(
        selected_diagnostics.get("qualifying_covered_evidence_count"), 0
    )
    server["independent_direct_interval_anchors"] = list(
        selected_diagnostics.get("independent_direct_interval_anchors") or []
    )
    server["confidence"] = min(confidence_for_match_count(evidence_score), confidence_cap)
    server["scoring_algorithm_version"] = SCORING_ALGORITHM_VERSION
    minutes = [
        _safe_int(event.get("minute_of_day"), -1)
        for event in events
        if _safe_int(event.get("cycle_seconds"), 0) == learned_cycle
    ]
    server["expected_restart_minutes_of_day"] = sorted({m for m in minutes if 0 <= m < 1440})


def _model_generation(server: dict) -> int:
    return max(1, _safe_int(server.get("model_generation"), 1))


def _local_hhmm(epoch_seconds: int) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(int(epoch_seconds)))
    except Exception:
        return "--:--"


def _minute_of_day(epoch_seconds: int) -> int:
    try:
        local = time.localtime(int(epoch_seconds))
        return int(local.tm_hour) * 60 + int(local.tm_min)
    except Exception:
        return -1


def _safe_int(value, default: int) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        converted = int(value)
        return converted
    except Exception:
        return int(default)


def _safe_float(value, default: float) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        converted = float(value)
        return converted if math.isfinite(converted) else float(default)
    except Exception:
        return float(default)


def _is_finite_number(value) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _is_nonnegative_number(value) -> bool:
    return _is_finite_number(value) and float(value) >= 0.0


def _optional_nonnegative_float(value) -> float | None:
    if not _is_nonnegative_number(value):
        return None
    converted = float(value)
    return converted if converted >= 0.0 else None


def _safe_nonnegative_float(value, default: float) -> float:
    converted = _optional_nonnegative_float(value)
    return float(default) if converted is None else converted


def _safe_bounded_float(value, default: float, minimum: float, maximum: float) -> float:
    converted = _safe_float(value, default)
    return min(maximum, max(minimum, converted))


def _safe_nonnegative_int(value, default: int) -> int:
    converted = _safe_int(value, default)
    return max(0, converted)
