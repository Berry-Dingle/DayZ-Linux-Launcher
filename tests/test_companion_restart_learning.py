import json

from dzll_launcher import companion_restart_learning as learning


KEY = "test.example:2302"
HOUR = 3600


def outage(return_at, *, soft=False):
    duration = 75 if soft else 90
    return {
        "offline_at": return_at - duration,
        "online_at": return_at,
        "duration_seconds": duration,
        "name": "Regression server",
        "map": "Chernarus",
    }


def record(state, return_at, *, soft=False):
    return learning.record_confirmed_outage(state, KEY, outage(return_at, soft=soft))


def server(state):
    return state["servers"][KEY]


def add_coverage(state, start, end):
    item = server(state)
    item.setdefault("monitor_sessions", []).append({
        "started_at": start,
        "last_heartbeat_at": end,
        "ended_at": end,
        "ended_reason": "test",
    })


def legacy_twelve_hour_state(*, outer_version=1, scoring_version=None):
    anchor = 8_000_000
    intervals = [12 * HOUR, 12 * HOUR, 24 * HOUR, 5 * 24 * HOUR, 12 * HOUR, 12 * HOUR]
    events = [{
        "offline_at": anchor - 90,
        "online_at": anchor,
        "duration_seconds": 90,
        "interval_seconds": None,
        "cycle_seconds": None,
        "model_generation": 1,
        "soft": False,
    }]
    current = anchor
    for interval in intervals:
        current += interval
        events.append({
            "offline_at": current - 90,
            "online_at": current,
            "duration_seconds": 90,
            "interval_seconds": interval,
            "cycle_seconds": 12 * HOUR,
            "model_generation": 1,
            "soft": False,
        })
    item = {
        "name": "Legacy regression server",
        "raw_outages": [{"offline_at": anchor - 90, "online_at": anchor, "accepted": True}],
        "restart_events": events,
        "expected_window_misses": [],
        "monitor_sessions": [],
        "learned_cycle_seconds": 12 * HOUR,
        "confidence": 0.95,
        "evidence_score": 6.0,
        "last_restart_event_at": current,
        "model_generation": 1,
    }
    if scoring_version is not None:
        item["scoring_algorithm_version"] = scoring_version
    return {"version": outer_version, "servers": {KEY: item}}


def append_scored_event(item, *, start, end, cycle=12 * HOUR, kind="direct", coverage="uncovered_direct"):
    learning._append_restart_event(
        item,
        offline_at=end - 90,
        online_at=end,
        duration_seconds=90,
        interval_seconds=end - start,
        cycle_seconds=cycle,
        model_generation=1,
        evidence_kind=kind,
        coverage_quality=coverage,
        interval_start_at=start,
    )


def test_one_and_two_intervals_obey_confidence_caps():
    state = record({}, 1_000_000)
    state = record(state, 1_000_000 + 3 * HOUR)
    assert server(state)["confidence"] <= 0.25
    assert server(state)["confidence_cap"] == 0.25
    assert server(state)["independent_interval_count"] == 1

    state = record(state, 1_000_000 + 6 * HOUR)
    assert server(state)["confidence"] <= 0.45
    assert server(state)["confidence_cap"] == 0.45
    assert server(state)["independent_interval_count"] == 2


def test_valid_three_hour_direct_learning_remains_possible():
    anchor = 2_000_000
    state = record({}, anchor)
    for index in range(1, 7):
        add_coverage(state, anchor + (index - 1) * 3 * HOUR, anchor + index * 3 * HOUR)
        state = record(state, anchor + index * 3 * HOUR, soft=index % 3 == 0)

    item = server(state)
    assert item["learned_cycle_seconds"] == 3 * HOUR
    assert item["supporting_evidence_score"] > 4
    assert item["covered_direct_cycle_count"] >= 4


def test_unmonitored_projected_multiple_gets_little_exact_cycle_evidence():
    anchor = 3_000_000
    state = record({}, anchor)
    state = record(state, anchor + 12 * HOUR)
    before = server(state)["supporting_evidence_score"]
    state = record(state, anchor + 36 * HOUR)
    item = server(state)

    projected = item["restart_events"][-1]
    assert projected["evidence_kind"] == "projected_multiple"
    assert projected["coverage_quality"] == "unmonitored_multiple"
    assert projected["evidence_weight"] <= 0.05
    assert item["supporting_evidence_score"] <= before + 0.05
    assert item["independent_interval_count"] == 1


def test_covered_projected_multiple_is_distinguished_from_unmonitored_gap():
    anchor = 4_000_000
    state = record({}, anchor)
    state = record(state, anchor + 12 * HOUR)
    add_coverage(state, anchor + 12 * HOUR, anchor + 36 * HOUR)
    state = record(state, anchor + 36 * HOUR)
    event = server(state)["restart_events"][-1]

    assert event["coverage_quality"] == "covered_multiple"
    assert event["evidence_weight"] > 0.05
    assert event["evidence_weight"] < 1.0


def test_hard_and_soft_mismatches_persist_and_survive_recalculation():
    anchor = 5_000_000
    state = record({}, anchor)
    state = record(state, anchor + 12 * HOUR)
    state = record(state, anchor + 24 * HOUR)
    confidence_before = server(state)["confidence"]

    state = record(state, anchor + 27 * HOUR)  # hard 3h contradiction
    state = record(state, anchor + 31 * HOUR, soft=True)  # soft 4h contradiction
    persisted = json.loads(json.dumps(state))
    state = record(persisted, anchor + 43 * HOUR)  # later same-phase match
    item = server(state)

    assert any(not entry["soft"] for entry in item["contradictions"])
    assert any(entry["soft"] for entry in item["contradictions"])
    assert item["candidate_scores"][str(12 * HOUR)]["contradiction_penalty"] == 1.10
    assert item["confidence"] <= max(confidence_before, item["confidence_cap"])


def test_one_hard_mismatch_is_not_erased_by_one_later_match():
    anchor = 5_500_000
    state = record({}, anchor)
    state = record(state, anchor + 12 * HOUR)
    state = record(state, anchor + 24 * HOUR)
    state = record(state, anchor + 27 * HOUR)
    penalty_after_mismatch = server(state)["candidate_scores"][str(12 * HOUR)][
        "contradiction_penalty"
    ]
    state = record(state, anchor + 39 * HOUR)

    twelve_hour = server(state)["candidate_scores"][str(12 * HOUR)]
    assert twelve_hour["contradiction_penalty"] == penalty_after_mismatch
    assert twelve_hour["effective_evidence_score"] < twelve_hour["supporting_evidence_score"]


def test_expected_window_miss_remains_in_recalculated_penalty():
    anchor = 6_000_000
    state = record({}, anchor)
    state = record(state, anchor + 3 * HOUR)
    state = learning.record_expected_window_miss(state, KEY, {
        "expected_at": anchor + 6 * HOUR,
        "window_start": anchor + 6 * HOUR - 1800,
        "window_end": anchor + 6 * HOUR + 1800,
        "cycle_seconds": 3 * HOUR,
        "model_generation": 1,
        "observed_poll_count": 100,
        "coverage_ratio": 1.0,
        "max_gap_seconds": 10,
    })
    penalty = server(state)["contradiction_penalty"]
    state = record(state, anchor + 9 * HOUR)
    assert server(state)["expected_window_misses"]
    assert server(state)["candidate_scores"][str(3 * HOUR)]["contradiction_penalty"] >= penalty


def test_legacy_state_without_phase_one_fields_loads_safely():
    legacy = legacy_twelve_hour_state()
    history_before = json.loads(json.dumps(legacy["servers"][KEY]))
    normalized = learning.normalize_state(legacy)
    item = server(normalized)
    assert normalized["version"] == learning.STATE_VERSION
    assert item["scoring_algorithm_version"] == learning.SCORING_ALGORITHM_VERSION
    assert item["confidence"] <= 0.50
    assert item["raw_outages"] == history_before["raw_outages"]
    assert item["restart_events"] == history_before["restart_events"]
    assert learning.summarize_server(normalized, KEY) is None


def test_scoring_migration_is_idempotent_and_round_trip_stable():
    legacy = legacy_twelve_hour_state()
    legacy["servers"][KEY]["contradictions"] = [{
        "observed_at": 8_500_000,
        "contradicted_cycle_seconds": 12 * HOUR,
        "observed_cycle_seconds": 3 * HOUR,
        "reason": "cycle_mismatch",
        "soft": False,
        "penalty": learning.HARD_CONTRADICTION_PENALTY,
        "model_generation": 1,
    }]
    first = learning.normalize_state(legacy)
    first_json = json.dumps(first, sort_keys=True)
    second = learning.normalize_state(json.loads(first_json))
    third = learning.normalize_state(json.loads(json.dumps(second)))

    assert json.dumps(second, sort_keys=True) == first_json
    assert third == second
    assert len(server(third)["contradictions"]) == 1


def test_outer_v2_with_old_server_scoring_version_is_migrated():
    state = learning.normalize_state(legacy_twelve_hour_state(outer_version=2, scoring_version=1))
    assert server(state)["scoring_algorithm_version"] == learning.SCORING_ALGORITHM_VERSION
    assert server(state)["confidence"] <= 0.50


def test_five_day_unmonitored_gap_is_minimal_and_not_independent_direct_evidence():
    anchor = 8_500_000
    state = record({}, anchor)
    state = record(state, anchor + 12 * HOUR)
    state = record(state, anchor + 132 * HOUR)
    item = server(state)
    event = item["restart_events"][-1]

    assert event["projected_cycle_count"] == 10
    assert event["coverage_quality"] == "unmonitored_multiple"
    assert event["evidence_weight"] == 0.05
    assert item["direct_interval_count"] == 1
    assert item["projected_interval_count"] == 1
    assert item["independent_interval_count"] == 1


def test_ambiguous_projected_evidence_cap_is_reachable():
    item = learning._normalize_server({})
    anchor = 9_000_000
    for index in range(3):
        append_scored_event(item, start=anchor + index * 12 * HOUR, end=anchor + (index + 1) * 12 * HOUR)
    for index in range(3):
        append_scored_event(
            item,
            start=anchor,
            end=anchor + (index + 2) * 24 * HOUR,
            kind="projected_multiple",
            coverage="unmonitored_multiple",
        )
    learning._recalculate_model(item)

    assert item["candidate_scores"][str(12 * HOUR)]["ambiguous_projected_observation_ratio"] == 0.50
    assert item["confidence_cap"] == 0.35
    assert item["independent_interval_count"] == 3


def test_shared_anchor_direct_observations_count_once_for_independence():
    item = learning._normalize_server({})
    anchor = 9_500_000
    for index in range(1, 4):
        append_scored_event(item, start=anchor, end=anchor + index * 12 * HOUR)
    learning._recalculate_model(item)

    assert item["direct_interval_count"] == 3
    assert item["independent_interval_count"] == 1
    assert item["confidence_cap"] == 0.25


def test_one_outage_gets_one_penalty_when_two_mismatch_routes_apply():
    anchor = 10_000_000
    state = record({}, anchor)
    state = record(state, anchor + 3 * HOUR)
    state = record(state, anchor + 11 * HOUR)  # projected miss and direct 8h mismatch
    contradictions = server(state)["contradictions"]

    assert len(contradictions) == 1
    assert contradictions[0]["penalty"] == learning.HARD_CONTRADICTION_PENALTY
    assert contradictions[0]["reasons"] == ["cycle_mismatch", "projected_window_miss"]


def test_soft_contradiction_upgrades_to_hard_without_duplication():
    item = learning._normalize_server({})
    learning._append_contradiction(
        item, observed_at=10_500_000, contradicted_cycle_seconds=12 * HOUR,
        observed_cycle_seconds=None, reason="soft_projected_window_miss", soft=True,
    )
    learning._append_contradiction(
        item, observed_at=10_500_000, contradicted_cycle_seconds=12 * HOUR,
        observed_cycle_seconds=3 * HOUR, reason="cycle_mismatch", soft=False,
    )

    assert len(item["contradictions"]) == 1
    assert item["contradictions"][0]["soft"] is False
    assert item["contradictions"][0]["penalty"] == learning.HARD_CONTRADICTION_PENALTY
    assert item["contradictions"][0]["observed_cycle_seconds"] == 3 * HOUR


def test_separate_soft_contradictions_accumulate_and_serialize_exactly():
    item = learning._normalize_server({})
    for observed_at in (11_000_000, 11_010_800):
        learning._append_contradiction(
            item, observed_at=observed_at, contradicted_cycle_seconds=12 * HOUR,
            observed_cycle_seconds=3 * HOUR, reason="soft_cycle_mismatch", soft=True,
        )
    restored = json.loads(json.dumps(item))

    assert len(restored["contradictions"]) == 2
    assert sum(entry["penalty"] for entry in restored["contradictions"]) == 0.70


def test_positive_and_negative_evidence_ledgers_share_bounded_horizon():
    item = learning._normalize_server({})
    anchor = 11_250_000
    for index in range(learning.RESTART_EVENT_LIMIT + 5):
        append_scored_event(
            item,
            start=anchor + index * 3 * HOUR,
            end=anchor + (index + 1) * 3 * HOUR,
            cycle=3 * HOUR,
        )
        learning._append_contradiction(
            item,
            observed_at=anchor + (index + 1) * 3 * HOUR,
            contradicted_cycle_seconds=12 * HOUR,
            observed_cycle_seconds=3 * HOUR,
            reason="cycle_mismatch",
            soft=False,
        )
    for index in range(learning.EXPECTED_WINDOW_MISS_LIMIT + 5):
        learning._append_expected_window_miss(item, {
            "expected_at": anchor + index,
            "model_generation": 1,
            "cycle_seconds": 12 * HOUR,
        })

    assert len(item["restart_events"]) == 100
    assert len(item["contradictions"]) == 100
    assert len(item["expected_window_misses"]) == 100


def test_direct_six_hour_learning_is_not_forced_to_three_hours():
    anchor = 11_500_000
    state = record({}, anchor)
    state = record(state, anchor + 6 * HOUR)
    state = record(state, anchor + 12 * HOUR)
    assert server(state)["learned_cycle_seconds"] == 6 * HOUR


def test_twelve_hour_thresholds_block_early_confidence_but_allow_mature_model():
    anchor = 12_000_000
    state = record({}, anchor)
    for index in range(1, 9):
        add_coverage(state, anchor + (index - 1) * 12 * HOUR, anchor + index * 12 * HOUR)
        state = record(state, anchor + index * 12 * HOUR)
    assert server(state)["confidence"] <= 0.60

    add_coverage(state, anchor + 8 * 12 * HOUR, anchor + 9 * 12 * HOUR)
    state = record(state, anchor + 9 * 12 * HOUR)
    item = server(state)
    assert item["covered_direct_cycle_count"] == 9
    assert item["confidence"] >= 0.80
    assert item["confidence"] == 0.95


def test_selected_candidate_diagnostics_and_confidence_bounds_agree():
    anchor = 13_000_000
    state = record({}, anchor)
    for index in range(1, 5):
        state = record(state, anchor + index * 3 * HOUR, soft=index % 2 == 0)
    item = server(state)
    selected = item["candidate_scores"][str(item["learned_cycle_seconds"])]

    assert item["supporting_evidence_score"] == selected["supporting_evidence_score"]
    assert item["contradiction_penalty"] == selected["contradiction_penalty"]
    assert item["effective_evidence_score"] == selected["effective_evidence_score"]
    assert item["confidence_cap"] == selected["confidence_cap"]
    assert 0.0 <= item["confidence"] <= 1.0


def test_replayed_and_out_of_order_events_do_not_change_model_score():
    anchor = 13_500_000
    state = record({}, anchor)
    state = record(state, anchor + 3 * HOUR)
    before = {key: server(state).get(key) for key in (
        "learned_cycle_seconds", "confidence", "supporting_evidence_score",
        "contradiction_penalty", "effective_evidence_score", "matching_event_count",
    )}
    state = record(state, anchor + 3 * HOUR)
    state = record(state, anchor - 3 * HOUR)
    after = {key: server(state).get(key) for key in before}
    assert after == before


def test_low_confidence_model_cannot_drive_schedule_consumers_but_generic_outage_remains():
    state = learning.normalize_state(legacy_twelve_hour_state())
    item = server(state)
    event = item["restart_events"][-1]

    assert item["confidence"] < learning.UI_CONFIDENCE_THRESHOLD
    assert learning.is_established_model(item) is False
    assert learning.summarize_server(state, KEY) is None
    assert learning.scheduled_outage_alert_threshold(
        state, KEY, event["offline_at"], event["online_at"]
    ) is None
    assert learning.summarize_alert_usability(state, KEY) == {
        "usable": True, "mode": "outage", "message": ""
    }


def test_dawn_of_the_bobs_pattern_does_not_retain_high_confidence_twelve_hours():
    # Minimal forensic fixture: relative July return times and hard/soft quality only.
    day = 24 * HOUR
    july_8 = 10_000_000
    observations = [
        (july_8 + 1 * HOUR, False),
        (july_8 + 13 * HOUR, True),
        (july_8 + day + 1 * HOUR, False),
        (july_8 + 2 * day + 1 * HOUR, False),
        (july_8 + 6 * day + 16 * HOUR, True),
        (july_8 + 6 * day + 19 * HOUR, False),
        (july_8 + 6 * day + 22 * HOUR, True),
        (july_8 + 7 * day + 1 * HOUR, False),
        (july_8 + 7 * day + 10 * HOUR, True),
        (july_8 + 7 * day + 13 * HOUR, True),
        (july_8 + 7 * day + 16 * HOUR, True),
        (july_8 + 8 * day + 1 * HOUR, False),
    ]

    state = {}
    for return_at, soft in observations:
        state = record(state, return_at, soft=soft)

    item = server(state)
    retained = {event["online_at"] for event in item["restart_events"]}
    for hour in (16, 19, 22):
        assert july_8 + 6 * day + hour * HOUR in retained
    assert not (
        item["learned_cycle_seconds"] == 12 * HOUR and item["confidence"] >= 0.80
    )
    assert item["learned_cycle_seconds"] == 3 * HOUR
    assert item["confidence"] <= item["confidence_cap"]
    assert item["contradictions"]
    three_hour = item["candidate_scores"][str(3 * HOUR)]
    twelve_hour = item["candidate_scores"][str(12 * HOUR)]
    assert three_hour["supporting_evidence_score"] == 3.58
    assert three_hour["contradiction_penalty"] == 0.0
    assert three_hour["effective_evidence_score"] == 3.58
    assert twelve_hour["supporting_evidence_score"] == 1.55
    assert twelve_hour["contradiction_penalty"] == 1.10
    assert twelve_hour["effective_evidence_score"] == 0.45
    assert three_hour["effective_evidence_score"] > twelve_hour["effective_evidence_score"]
    assert item["confidence_cap"] == 0.50
    assert learning.is_established_model(item) is False


def test_malformed_diagnostics_are_safely_rescored_and_normalization_is_stable():
    malformed_values = (
        {"candidate_scores": "broken"},
        {"candidate_scores": ["broken"]},
        {"independent_direct_interval_anchors": 42},
        {"independent_direct_interval_anchors": {"anchor": 42}},
        {
            "confidence": float("nan"),
            "confidence_cap": float("inf"),
            "supporting_evidence_score": "broken",
            "contradiction_penalty": float("-inf"),
            "effective_evidence_score": True,
        },
        {"candidate_scores": {str(12 * HOUR): "broken"}},
    )

    for malformed in malformed_values:
        state = legacy_twelve_hour_state(scoring_version=1)
        original_raw = json.loads(json.dumps(state["servers"][KEY]["raw_outages"]))
        original_events = json.loads(json.dumps(state["servers"][KEY]["restart_events"]))
        state["servers"][KEY].update(malformed)
        state["servers"][KEY]["confidence"] = malformed.get("confidence", 0.95)
        contradictions_before = len(state["servers"][KEY].get("contradictions", []))

        normalized = learning.normalize_state(state)
        item = server(normalized)
        assert item["confidence"] < learning.UI_CONFIDENCE_THRESHOLD
        assert learning.is_established_model(item) is False
        assert learning.summarize_server(normalized, KEY) is None
        assert item["raw_outages"] == original_raw
        assert item["restart_events"] == original_events
        assert len(item["contradictions"]) == contradictions_before

        repeated = learning.normalize_state(normalized)
        restored = learning.normalize_state(json.loads(json.dumps(repeated, allow_nan=False)))
        assert repeated == normalized
        assert restored == normalized

    state = legacy_twelve_hour_state(scoring_version=learning.SCORING_ALGORITHM_VERSION)
    original_times = [event["online_at"] for event in state["servers"][KEY]["restart_events"]]
    projected = state["servers"][KEY]["restart_events"][-1]
    projected["evidence_kind"] = "projected_multiple"
    projected["projected_cycle_count"] = "broken"
    projected["evidence_weight"] = float("inf")
    state["servers"][KEY]["contradictions"] = [{
        "observed_at": projected["online_at"],
        "contradicted_cycle_seconds": 12 * HOUR,
        "penalty": float("nan"),
        "model_generation": 1,
    }]
    normalized = learning.normalize_state(state)
    item = server(normalized)
    assert [event["online_at"] for event in item["restart_events"]] == original_times
    assert "evidence_weight" not in item["restart_events"][-1]
    assert item["restart_events"][-1]["projected_cycle_count"] is None
    assert item["contradictions"][0]["penalty"] == 0.0
    assert item["confidence"] < learning.UI_CONFIDENCE_THRESHOLD
    assert learning.is_established_model(item) is False


def test_incomplete_projected_events_use_conservative_deterministic_weights():
    item = learning._normalize_server({})
    anchor = 20_000_000
    for index in range(3):
        append_scored_event(
            item,
            start=anchor + index * 12 * HOUR,
            end=anchor + (index + 1) * 12 * HOUR,
        )
    for cycle_count in (2, 4, 10):
        learning._append_restart_event(
            item,
            offline_at=anchor + cycle_count * 12 * HOUR - 90,
            online_at=anchor + cycle_count * 12 * HOUR,
            duration_seconds=90,
            interval_seconds=cycle_count * 12 * HOUR,
            cycle_seconds=12 * HOUR,
            model_generation=1,
            evidence_kind="projected_multiple",
            projected_cycle_count=cycle_count,
            interval_start_at=anchor,
        )
    learning._recalculate_model(item)
    candidate = item["candidate_scores"][str(12 * HOUR)]

    assert candidate["supporting_evidence_score"] == 3.09
    assert candidate["projected_interval_count"] == 3
    assert candidate["unmonitored_projected_interval_count"] == 3
    assert candidate["independent_direct_interval_count"] == 3
    assert candidate["covered_direct_interval_count"] == 0
    assert candidate["confidence_cap"] == learning.ONLY_UNMONITORED_MULTIPLES_CONFIDENCE_CAP
    assert learning._event_evidence_weight(item["restart_events"][-1], 12 * HOUR) == 0.01

    restored = learning.normalize_state(json.loads(json.dumps({"version": 2, "servers": {KEY: item}})))
    assert server(restored)["candidate_scores"] == item["candidate_scores"]
    assert server(restored)["supporting_evidence_score"] == item["supporting_evidence_score"]


def test_one_malformed_server_does_not_abort_valid_mature_server_normalization():
    valid_key = "valid.example:2302"
    valid = learning._normalize_server({})
    anchor = 30_000_000
    for index in range(9):
        append_scored_event(
            valid,
            start=anchor + index * 12 * HOUR,
            end=anchor + (index + 1) * 12 * HOUR,
            coverage="covered_direct",
        )
    learning._recalculate_model(valid)
    malformed = legacy_twelve_hour_state(scoring_version=1)["servers"][KEY]
    malformed["candidate_scores"] = object()
    malformed["independent_direct_interval_anchors"] = object()

    normalized = learning.normalize_state({
        "version": 2,
        "servers": {KEY: malformed, valid_key: valid},
    })

    assert learning.is_established_model(normalized["servers"][KEY]) is False
    assert normalized["servers"][valid_key]["confidence"] == 0.95
    assert learning.is_established_model(normalized["servers"][valid_key]) is True
