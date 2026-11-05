"""Phase-1 schema-4 evidence retention (spans, chains, episodes, raw samples).

All fixtures are synthetic.  They model the duplicated legacy-coverage span
problem: `legacy_coverage_spans` IDs include the segment's list index, so a
sliding schema-3 coverage list re-mints IDs for unchanged segments on every
rebuild.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumer_state as state4
from dzll_launcher import companion_restart_phase2_continuity as continuity
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import companion_restart_phase2_storage as storage


BASE = 1_900_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
SERVER = "retention.example:2302"
EVENT_COUNT = 4
LEGACY_WINDOW = 6
MISS_AT = BASE - 2 * DAY + 15 * 60


# ---------------------------------------------------------------- fixtures


def _sample(at: float, *, players: int = 40) -> detection.ObservationSample:
    return detection.ObservationSample(
        wall_at=at,
        monotonic_at=at - (BASE - 30 * DAY),
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        server_key=SERVER,
        info_status=detection.InfoStatus.HEALTHY,
        info_latency=0.05,
        player_status=detection.FieldStatus.PRESENT,
        players=players,
        max_players=60,
        continuity_chain_id="chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _event(sequence: int, *, samples: bool) -> detection.PhysicalRestartEvent:
    at = BASE + (sequence - 1) * 3 * HOUR
    # Post-recovery healthy samples only: they never create transition spans,
    # so they do not alter the fresh derivation under test.
    values = tuple(_sample(at + 5 + 10 * index) for index in range(6)) if samples else ()
    return detection.PhysicalRestartEvent(
        event_id=f"event-{sequence}-{int(at)}",
        sequence=sequence,
        fingerprint=f"{SERVER}|monitor|{sequence}|fixture|restart",
        server_key=SERVER,
        episode_started_at=at - 90,
        finalized_at=at + 40,
        canonical_phase_at=at,
        phase_uncertainty=0,
        sources=frozenset({detection.SignalSource.INFO_OUTAGE}),
        outcome=detection.EventOutcome.CONFIRMED_OFFLINE_RESTART,
        authenticity=0.85,
        schedule_weight_suggestion=0.85,
        drain=detection.DrainSummary(
            None, None, None, None, False, None, None, 0, 0, False
        ),
        outage=detection.OutageSummary(
            at - 70, at - 60, 3, (detection.InfoStatus.TIMEOUT,), at
        ),
        recovery=detection.RecoverySummary(None, None, at, 0, False),
        query_health=detection.QueryHealthSummary(2, 3, 3, True),
        coverage_complete=True,
        lifecycle_interruption=None,
        samples=values,
        reason_codes=("retention_fixture",),
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        continuity_chain_id="chain",
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _legacy_session(index: int) -> dict:
    started = BASE - 5 * DAY + index * 2 * HOUR
    return {
        "session_id": f"legacy-monitor-{index}",
        "app_session_id": "legacy-app",
        "poll_generation": 1,
        "started_at": started,
        "ended_at": started + 1800,
        "reason": "shutdown",
    }


def _legacy_segment(index: int) -> scoring.CoverageSegment:
    started = BASE - 5 * DAY + index * 2 * HOUR
    # No provenance: these become index-dependent legacy spans.
    return scoring.CoverageSegment(
        started + 60, started + 1700, scoring.CoverageKind.ONLINE_HEALTHY, 10.0
    )


def _explicit(start: float, end: float, kind, chain: str) -> scoring.CoverageSegment:
    return scoring.CoverageSegment(
        start,
        end,
        kind,
        10.0,
        server_key=SERVER,
        app_session_id="app",
        monitoring_session_id="monitor",
        poll_generation=1,
        continuity_chain_id=chain,
        provenance_version=continuity.CONTINUITY_PROVENANCE_VERSION,
    )


def _schema3_record(
    step: int,
    *,
    sampled_tail: int = EVENT_COUNT,
    observed_outage_window: bool = False,
) -> dict:
    """One schema-3 runtime projection; `step` slides every bounded horizon."""

    sequences = range(step + 1, step + 1 + EVENT_COUNT)
    events = [
        _event(sequence, samples=index >= EVENT_COUNT - sampled_tail)
        for index, sequence in enumerate(sequences)
    ]
    first = BASE + step * 3 * HOUR
    last = first + (EVENT_COUNT - 1) * 3 * HOUR
    legacy = range(step, step + LEGACY_WINDOW)
    coverage = [_legacy_segment(index) for index in legacy]
    expected_misses = []
    incomplete = []
    if observed_outage_window:
        coverage.append(
            _explicit(
                MISS_AT - 900,
                MISS_AT + 900,
                scoring.CoverageKind.OFFLINE_OBSERVED,
                "outage-chain",
            )
        )
        expected_misses.append({
            "period_seconds": int(3 * HOUR),
            "expected_at": MISS_AT,
            "key": f"{int(3 * HOUR)}:{int(MISS_AT)}",
            "weight": 1.0,
        })
        incomplete.append({
            "event_id": "historical-episode",
            "started_at": MISS_AT - 300,
            "finalized_at": MISS_AT + 300,
        })
    coverage.append(
        _explicit(first - 600, last + 600, scoring.CoverageKind.ONLINE_HEALTHY, "chain")
    )
    coverage.sort(key=lambda item: (item.start_at, item.end_at))
    sessions = [_legacy_session(index) for index in legacy]
    sessions.append({
        "session_id": "monitor",
        "app_session_id": "app",
        "poll_generation": 1,
        "continuity_chain_id": "chain",
        "continuity_chain_ids": ["chain"],
        "provenance_version": 1,
        "started_at": BASE - 3 * DAY,
        "ended_at": None,
        "reason": None,
    })
    return {
        "runtime_state_version": 1,
        "scoring_semantics_version": 1,
        "aggregate_semantics_version": 1,
        "events": [runtime._serialize_event(item) for item in events],
        "event_seq": step + EVENT_COUNT,
        "folded_through_event_seq": step,
        "coverage_segments": [runtime._serialize_coverage(item) for item in coverage],
        "monitoring_sessions": sessions,
        "expected_window_misses": expected_misses,
        "fired_keys": [],
        "routed_fingerprints": [item.fingerprint for item in events],
        "incumbent_period_seconds": None,
        "regime_generation": "retention-fixture",
        "aggregate": runtime._serialize_aggregate(scoring.LongTermAggregate()),
        "prior_regimes": [],
        "active_episode": None,
        "incomplete_episodes": incomplete,
        "candidate_diagnostics": {},
        "consumer_adapter_version": 1,
    }


def _rebuild(existing: dict, schema3_record: dict, *, step: int = 0) -> dict:
    return schema4.rebuild_schema4_server_record(
        copy.deepcopy(existing),
        copy.deepcopy(schema3_record),
        server_key=SERVER,
        updated_at=BASE + (step + EVENT_COUNT) * 3 * HOUR,
    )


def _without_retention(monkeypatch):
    """Patch retention to identity: reproduces the pre-Phase-1 union."""

    monkeypatch.setattr(
        schema4, "retain_schema4_evidence", lambda record, **_kwargs: {}
    )


def _slide(steps: int, **kwargs) -> list[dict]:
    record: dict = {}
    records = []
    for step in range(steps):
        record = _rebuild(record, _schema3_record(step, **kwargs), step=step)
        records.append(record)
    return records


def _ids(record: dict, name: str, field: str) -> set[str]:
    return {item[field] for item in record[name]}


def _size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")))


_LEARNING_COLLECTIONS = (
    "interval_relationships",
    "cadence_streaks",
    "expected_window_results",
    "regimes",
    "authority_decisions",
    "authority_decision",
    "prior_authority_decision_id",
    "authority_candidates",
    "challenger_contexts",
    "normal_evidence_ledger",
    "high_evidence_ledger",
    "consumer_state",
    "legacy_schema3_record",
)


def _event_metadata(record: dict) -> list[dict]:
    return [
        {key: value for key, value in item.items() if key != "samples"}
        for item in record["physical_events"]
    ]


def _semantics(record: dict) -> dict:
    decision = schema4.persisted_authority_decision(record)
    regime = decision.selected_shadow_regime
    consumer = consumers4.evaluate_authority_consumers(
        consumers4.AuthorityConsumerPolicyInput(
            authority_decision=decision,
            now=BASE + 20 * 3 * HOUR + 60,
            restart_alert_enabled=True,
            server_online_healthy=True,
        )
    )
    return {
        "decision_id": decision.decision_id,
        "state": decision.state,
        "regime_id": None if regime is None else regime.regime_id,
        "period": None if regime is None else regime.candidate_period_seconds,
        "phase": None if regime is None else regime.phase_offset,
        "gate": None if regime is None else regime.authority_gate,
        "combined": decision.combined_confidence,
        "high": decision.high_confidence,
        "prediction_usable": decision.prediction_usable,
        "countdown_safe": decision.countdown_safe,
        "suspicion": decision.suspicion_level,
        "presentation": consumer.presentation_state,
        "confidence_display": consumer.confidence_display_value,
        "next": consumer.next_expected_restart_at,
        "warning_key": consumer.scheduled_warning_suppression_key,
    }


# ------------------------------------------------- reference-closure rules


def test_duplicated_legacy_spans_accumulate_without_retention(monkeypatch):
    """Baseline: the pre-Phase-1 union keeps every re-minted span ID."""

    _without_retention(monkeypatch)
    records = _slide(8)
    span_counts = [len(item["continuity_spans"]) for item in records]
    chain_counts = [len(item["continuity_chains"]) for item in records]
    assert span_counts == sorted(span_counts)
    assert span_counts[-1] > 2 * span_counts[0]
    assert chain_counts[-1] > chain_counts[0]
    contents = {}
    for span in records[-1]["continuity_spans"]:
        payload = {k: v for k, v in span.items() if k != "reference_id"}
        contents.setdefault(json.dumps(payload, sort_keys=True), []).append(span)
    # Byte-identical content persisted under several IDs.
    assert max(len(value) for value in contents.values()) > 1


def test_unstable_span_ids_do_not_accumulate_with_retention():
    records = _slide(12)
    span_counts = [len(item["continuity_spans"]) for item in records]
    chain_counts = [len(item["continuity_chains"]) for item in records]
    assert len(set(span_counts[1:])) == 1
    assert len(set(chain_counts[1:])) == 1
    final = records[-1]
    contents = [
        json.dumps({k: v for k, v in span.items() if k != "reference_id"}, sort_keys=True)
        for span in final["continuity_spans"]
    ]
    assert len(contents) == len(set(contents))
    assert schema4._validate_server_record(SERVER, final) == []


def test_unreferenced_historical_spans_and_chains_are_removed(monkeypatch):
    with_retention = _slide(6)[-1]
    _without_retention(monkeypatch)
    baseline = _slide(6)[-1]
    fresh = schema4._migrate_server(
        SERVER,
        copy.deepcopy(_schema3_record(5)),
        state={"updated_at": BASE},
        source_bytes=b"fresh",
    )[0]
    fresh_spans = _ids(fresh, "continuity_spans", "reference_id")
    fresh_chains = _ids(fresh, "continuity_chains", "chain_id")

    kept_spans = _ids(with_retention, "continuity_spans", "reference_id")
    kept_chains = _ids(with_retention, "continuity_chains", "chain_id")
    removed_spans = _ids(baseline, "continuity_spans", "reference_id") - kept_spans
    removed_chains = _ids(baseline, "continuity_chains", "chain_id") - kept_chains
    assert removed_spans and removed_chains
    assert kept_spans <= _ids(baseline, "continuity_spans", "reference_id")
    assert kept_chains <= _ids(baseline, "continuity_chains", "chain_id")
    # Everything removed was historical (outside the current fresh projection).
    assert not removed_spans & fresh_spans
    assert not removed_chains & fresh_chains
    # Historical legacy sessions that left the schema-3 horizon are gone.
    legacy_chain_ids = {
        continuity.deterministic_continuity_chain_id(
            SERVER, "legacy-app", f"legacy-monitor-{index}", 1,
            _legacy_session(index)["started_at"],
        )
        for index in range(0, 5)
    }
    assert legacy_chain_ids & _ids(baseline, "continuity_chains", "chain_id")
    assert not legacy_chain_ids & kept_chains


def test_referenced_chains_spans_and_episodes_survive_leaving_the_horizon():
    """A window revision pins a span and an episode after both leave fresh."""

    first = _rebuild({}, _schema3_record(0, observed_outage_window=True))
    revisions = [
        item for item in first["expected_window_results"]
        if item.get("supersedes_result_id")
    ]
    assert revisions, "fixture must produce an observed-outage retraction"
    referenced = {
        ref for item in revisions for ref in item["overlapping_outage_episode_ids"]
    }
    span_refs = referenced & _ids(first, "continuity_spans", "reference_id")
    episode_refs = referenced & _ids(
        first, "expected_window_episode_evidence", "evidence_id"
    )
    assert span_refs and episode_refs == {"historical-episode"}

    second = _rebuild(first, _schema3_record(1), step=1)
    assert span_refs <= _ids(second, "continuity_spans", "reference_id")
    assert episode_refs <= _ids(
        second, "expected_window_episode_evidence", "evidence_id"
    )
    assert second["expected_window_results"] == first["expected_window_results"]
    # High relationships and streaks keep their chains, and chains their spans.
    chains = {item["chain_id"]: item for item in second["continuity_chains"]}
    for item in second["interval_relationships"]:
        if item["high_authority_eligible"]:
            assert item["continuity_chain_id"] in chains
    for item in second["cadence_streaks"]:
        assert item["continuity_chain_id"] in chains
    spans = _ids(second, "continuity_spans", "reference_id")
    for chain in chains.values():
        assert set(chain["reference_ids"]) <= spans
    assert schema4._validate_server_record(SERVER, second) == []


def test_unreferenced_historical_episode_evidence_is_removed():
    first = _rebuild({}, _schema3_record(0))
    injected = copy.deepcopy(first)
    injected["expected_window_episode_evidence"] = [{
        "schema_version": 1,
        "semantic_version": 2,
        "evidence_id": "orphan-episode",
        "start_at": BASE - 9 * DAY,
        "end_at": BASE - 9 * DAY + 60,
        "outage_observed": False,
        "recovery_observed": False,
        "unresolved": True,
        "restart_like": True,
        "reason_codes": ["persisted_incomplete_episode_diagnostic"],
    }]
    assert schema4._validate_server_record(SERVER, injected) == []
    rebuilt = _rebuild(injected, _schema3_record(0))
    assert "orphan-episode" not in _ids(
        rebuilt, "expected_window_episode_evidence", "evidence_id"
    )


def test_retention_function_closure_rules_on_minimal_record():
    record = {
        "physical_events": [],
        "continuity_spans": [
            {"reference_id": "s-chain"},
            {"reference_id": "s-window"},
            {"reference_id": "s-fresh"},
            {"reference_id": "s-orphan"},
            {"no_id": True},
        ],
        "continuity_chains": [
            {"chain_id": "c-high", "reference_ids": ["s-chain"]},
            {"chain_id": "c-streak", "reference_ids": []},
            {"chain_id": "c-normal", "reference_ids": []},
            {"chain_id": "c-orphan", "reference_ids": ["s-orphan"]},
        ],
        "interval_relationships": [
            {"high_authority_eligible": True, "continuity_chain_id": "c-high"},
            {"high_authority_eligible": False, "continuity_chain_id": "c-normal"},
        ],
        "cadence_streaks": [{"continuity_chain_id": "c-streak"}],
        "expected_window_results": [
            {"overlapping_outage_episode_ids": ["s-window", "e-window"]}
        ],
        "expected_window_episode_evidence": [
            {"evidence_id": "e-window"},
            {"evidence_id": "e-fresh"},
            {"evidence_id": "e-orphan"},
        ],
    }
    result = schema4.retain_schema4_evidence(
        record, fresh_span_ids=("s-fresh",), fresh_episode_ids=("e-fresh",)
    )
    assert [item.get("chain_id") for item in result["continuity_chains"]] == [
        "c-high", "c-streak", "c-normal",
    ]
    assert [item.get("reference_id") for item in result["continuity_spans"]] == [
        "s-chain", "s-window", "s-fresh", None,
    ]
    assert [item["evidence_id"] for item in result["expected_window_episode_evidence"]] == [
        "e-window", "e-fresh",
    ]
    again = schema4.retain_schema4_evidence(
        {**record, **result}, fresh_span_ids=("s-fresh",), fresh_episode_ids=("e-fresh",)
    )
    assert again == result
    # The input is never mutated.
    assert len(record["continuity_spans"]) == 5


# ----------------------------------------------------------------- samples


def test_obsolete_raw_samples_are_removed_without_touching_metadata(monkeypatch):
    records = _slide(4)
    final = records[-1]
    _without_retention(monkeypatch)
    baseline = _slide(4)[-1]

    schema3 = _schema3_record(3)
    horizon = schema4.schema3_sample_horizon_event_ids(schema3)
    assert horizon == {item["event_id"] for item in schema3["events"]}
    trimmed = [
        item for item in final["physical_events"] if item["event_id"] not in horizon
    ]
    assert trimmed and all(item["samples"] == [] for item in trimmed)
    assert all(
        item["samples"]
        for item in baseline["physical_events"]
        if item["event_id"] not in horizon
    )
    for item in final["physical_events"]:
        if item["event_id"] in horizon:
            assert item["samples"]
    assert _event_metadata(final) == _event_metadata(baseline)
    assert _ids(final, "physical_events", "event_id") == _ids(
        baseline, "physical_events", "event_id"
    )


def test_sample_horizon_is_latest_twelve_sample_bearing_events():
    events = [{"event_id": f"e{index:02d}", "samples": [{}]} for index in range(15)]
    events.insert(3, {"event_id": "empty", "samples": []})
    events.append({"event_id": "no-samples-key"})
    events.append("not-a-mapping")
    ids = schema4.schema3_sample_horizon_event_ids({"events": events})
    assert ids == {f"e{index:02d}" for index in range(3, 15)}
    assert schema4.schema3_sample_horizon_event_ids({}) == frozenset()
    assert schema4.schema3_sample_horizon_event_ids({"events": None}) == frozenset()


def test_event_leaving_schema3_sample_tail_loses_persisted_samples():
    """Schema-3 drops an event's samples; the old richer copy is not kept."""

    first = _rebuild({}, _schema3_record(0, sampled_tail=EVENT_COUNT))
    assert all(item["samples"] for item in first["physical_events"])
    second = _rebuild(first, _schema3_record(0, sampled_tail=2))
    by_id = {item["event_id"]: item for item in second["physical_events"]}
    schema3 = _schema3_record(0, sampled_tail=2)
    for raw in schema3["events"]:
        assert bool(by_id[raw["event_id"]]["samples"]) == bool(raw["samples"])
    assert _event_metadata(first) == _event_metadata(second)


def test_schema3_coverage_sessions_and_samples_are_not_modified():
    schema3 = _schema3_record(5)
    original = copy.deepcopy(schema3)
    record = _slide(5)[-1]
    rebuilt = _rebuild(record, schema3, step=5)
    assert schema3 == original
    assert rebuilt["legacy_schema3_record"] == original
    assert any(item["samples"] for item in rebuilt["legacy_schema3_record"]["events"])


# -------------------------------------------------- learning semantics


def test_learning_inputs_and_semantics_are_unchanged(monkeypatch):
    with_retention = _slide(10)
    _without_retention(monkeypatch)
    baseline = _slide(10)
    for retained, full in zip(with_retention, baseline):
        for name in _LEARNING_COLLECTIONS:
            assert retained[name] == full[name], name
        assert _event_metadata(retained) == _event_metadata(full)
        assert _semantics(retained) == _semantics(full)
    final = _semantics(with_retention[-1])
    # The fixture exercises an established cycle, not an empty decision.
    assert final["regime_id"] is not None
    assert final["period"] == int(3 * HOUR)


def test_learning_semantics_unchanged_when_window_references_leave_horizon(
    monkeypatch,
):
    def run() -> list[dict]:
        record: dict = {}
        records = []
        for step in range(8):
            record = _rebuild(
                record,
                _schema3_record(step, observed_outage_window=step < 2),
                step=step,
            )
            records.append(record)
        return records

    with_retention = run()
    _without_retention(monkeypatch)
    baseline = run()
    assert any(
        item.get("supersedes_result_id")
        for item in with_retention[-1]["expected_window_results"]
    )
    for retained, full in zip(with_retention, baseline):
        for name in _LEARNING_COLLECTIONS:
            assert retained[name] == full[name], name
        assert _event_metadata(retained) == _event_metadata(full)
        assert _semantics(retained) == _semantics(full)


def test_retention_preserves_alert_suppression_and_references(monkeypatch):
    seed = _slide(3)[-1]
    decision = schema4.persisted_authority_decision(seed)
    regime_id = decision.selected_shadow_regime.regime_id
    consumer_id = "retention-consumer"
    event_id = seed["physical_events"][0]["event_id"]
    alerts = (
        state4.alert_record(
            key=consumers4.AuthoritySuppressionKey(
                kind=consumers4.AuthoritySuppressionKind.GENERIC_RECOVERY,
                server_key=SERVER,
                event_id=event_id,
            ),
            authority_consumer_decision_id=consumer_id,
            created_at=BASE,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
        ),
        state4.alert_record(
            key=consumers4.AuthoritySuppressionKey(
                kind=consumers4.AuthoritySuppressionKind.SCHEDULED_WARNING,
                server_key=SERVER,
                regime_id=regime_id,
                expected_occurrence_at=int(BASE + 30 * HOUR),
            ),
            authority_consumer_decision_id=consumer_id,
            created_at=BASE + 1,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
        ),
    )
    ledger = replace(
        state4.ledger_from_mapping(state4.empty_consumer_state(SERVER), server_key=SERVER),
        current_authority_consumer_decision_id=consumer_id,
        decisions=(
            state4.ConsumerDecisionRecord(
                schema_version=state4.CONSUMER_STATE_SCHEMA_VERSION,
                semantic_version=state4.CONSUMER_STATE_SEMANTIC_VERSION,
                decision_id=consumer_id,
                authority_decision_id=decision.decision_id,
                server_key=SERVER,
                selected_regime_id=regime_id,
                presentation_state=consumers4.AuthorityPresentationKey.CONFIRMED_CYCLE.value,
                cycle_period_seconds=int(3 * HOUR),
                confidence_display_value=0.9,
                next_expected_restart_at=None,
                countdown_safe=False,
                prediction_suspended=False,
                reason_codes=("retention_fixture",),
            ),
        ),
        alert_records=alerts,
    )
    payload = state4.ledger_to_mapping(ledger)
    seed["consumer_state"] = payload
    assert schema4._validate_server_record(SERVER, seed) == []
    keys_before = state4.fired_suppression_keys(
        state4.ledger_from_mapping(payload, server_key=SERVER)
    )
    assert len(keys_before) == 2

    record = seed
    for step in range(3, 9):
        record = _rebuild(record, _schema3_record(step), step=step)
        assert schema4._validate_server_record(SERVER, record) == []
        assert record["consumer_state"] == payload
    assert event_id in _ids(record, "physical_events", "event_id")
    assert state4.fired_suppression_keys(
        state4.ledger_from_mapping(record["consumer_state"], server_key=SERVER)
    ) == keys_before
    assert decision.decision_id in {
        item["decision_id"] for item in record["authority_decisions"]
    }


# ---------------------------------------------- determinism and persistence


def _settled(steps: int) -> dict:
    """Slide, then rebuild once more with the same input.

    Independently of retention, the first identical rebuild only moves
    `prior_authority_decision_id` onto the current decision; afterwards the
    record is a fixed point.
    """

    record = _slide(steps)[-1]
    return _rebuild(record, _schema3_record(steps - 1), step=steps - 1)


def test_repeated_rebuilds_are_deterministic_and_idempotent(monkeypatch):
    first = _slide(6)[-1]
    settled = _rebuild(first, _schema3_record(5), step=5)
    changed = sorted(
        key for key in set(first) | set(settled) if first.get(key) != settled.get(key)
    )
    assert changed == ["prior_authority_decision_id"]
    record = settled
    for _ in range(3):
        again = _rebuild(record, _schema3_record(5), step=5)
        assert schema4.canonical_json_bytes(again) == schema4.canonical_json_bytes(record)
        record = again
    assert schema4.canonical_json_bytes(_settled(6)) == schema4.canonical_json_bytes(settled)
    # Retention itself is a fixed point of its own output.
    again = schema4.retain_schema4_evidence(
        settled,
        fresh_span_ids=_ids(settled, "continuity_spans", "reference_id"),
        fresh_chain_ids=_ids(settled, "continuity_chains", "chain_id"),
        fresh_episode_ids=(),
        sample_event_ids=schema4.schema3_sample_horizon_event_ids(_schema3_record(5)),
    )
    for name, values in again.items():
        assert values == settled[name], name
    # The settling step is pre-existing behaviour, identical without retention.
    _without_retention(monkeypatch)
    base_first = _slide(6)[-1]
    base_settled = _rebuild(base_first, _schema3_record(5), step=5)
    assert sorted(
        key for key in set(base_first) | set(base_settled)
        if base_first.get(key) != base_settled.get(key)
    ) == ["prior_authority_decision_id"]


def test_no_broken_references_across_long_slide():
    record: dict = {}
    for step in range(16):
        record = _rebuild(
            record,
            _schema3_record(step, observed_outage_window=step == 0),
            step=step,
        )
        assert schema4._validate_server_record(SERVER, record) == []


def test_retained_record_round_trips_through_serialisation(tmp_path: Path):
    record = _settled(7)
    state = _state_with(record, _schema3_record(6))
    payload = schema4.serialize_schema4_state(state)
    path = tmp_path / "retained.json"
    path.write_bytes(payload)
    loaded = schema4.deserialize_schema4_bytes(
        path.read_bytes(), quarantine_invalid_servers=False
    )
    assert loaded.report.valid
    reloaded = loaded.state["servers"][SERVER]
    assert _semantics(reloaded) == _semantics(record)
    rebuilt = _rebuild(reloaded, _schema3_record(6), step=6)
    assert schema4.canonical_json_bytes(rebuilt) == schema4.canonical_json_bytes(record)
    continued = _rebuild(rebuilt, _schema3_record(7), step=7)
    assert schema4._validate_server_record(SERVER, continued) == []


def test_older_unpruned_and_partial_records_are_handled(monkeypatch):
    _without_retention(monkeypatch)
    older = _slide(6)[-1]
    monkeypatch.undo()
    assert schema4._validate_server_record(SERVER, older) == []
    pruned = _rebuild(older, _schema3_record(5), step=5)
    assert len(pruned["continuity_spans"]) < len(older["continuity_spans"])
    assert len(pruned["continuity_chains"]) < len(older["continuity_chains"])
    assert schema4._validate_server_record(SERVER, pruned) == []
    with monkeypatch.context() as patch:
        _without_retention(patch)
        unpruned = _rebuild(older, _schema3_record(5), step=5)
    for name in _LEARNING_COLLECTIONS:
        assert pruned[name] == unpruned[name], name
    assert _event_metadata(pruned) == _event_metadata(unpruned)
    assert _semantics(pruned) == _semantics(unpruned)

    # Older or partially populated records: absent non-learning collections
    # and sample-free events are tolerated and repopulated from fresh.
    partial = copy.deepcopy(older)
    partial.pop("continuity_spans")
    partial.pop("expected_window_episode_evidence")
    partial["continuity_chains"] = []
    for item in partial["physical_events"]:
        item.pop("samples", None)
    rebuilt = _rebuild(partial, _schema3_record(5), step=5)
    assert schema4._validate_server_record(SERVER, rebuilt) == []
    assert _semantics(rebuilt)["regime_id"] == _semantics(older)["regime_id"]
    fresh_only = _rebuild({}, _schema3_record(5), step=5)
    assert schema4._validate_server_record(SERVER, fresh_only) == []

    assert schema4.retain_schema4_evidence({}) == {}
    odd = {"continuity_spans": None, "physical_events": ["not-a-mapping"]}
    assert schema4.retain_schema4_evidence(odd) == {
        "physical_events": ["not-a-mapping"]
    }


def _state_with(record: dict, schema3_record: dict) -> dict:
    source = storage.new_phase2_state(
        now=int(BASE), generation_id="00000000-0000-4000-8000-0000000000e1"
    )
    source["servers"] = {SERVER: copy.deepcopy(schema3_record)}
    raw = schema4.canonical_json_bytes(source)
    state = schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1_900_000_000_000_000_000
    )
    state["servers"][SERVER] = copy.deepcopy(record)
    return state


def test_unchanged_retained_state_causes_no_persistence_write(tmp_path: Path):
    record = _settled(6)
    schema3 = _schema3_record(5)
    path = tmp_path / "authoritative-schema4.json"
    path.write_bytes(schema4.serialize_schema4_state(_state_with(record, schema3)))
    backend = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        changed = backend.update_server_from_schema3(
            SERVER,
            schema3,
            updated_at=BASE + (5 + EVENT_COUNT) * 3 * HOUR,
            durable_reason="retention_fixture",
        )
        assert changed is False
        assert backend.dirty is False
        before = path.read_bytes()
        assert backend.flush().wrote is False
        assert path.read_bytes() == before
    finally:
        backend.close(flush=False)


def test_first_rebuild_of_unpruned_state_writes_once_then_stabilises(
    tmp_path: Path, monkeypatch
):
    _without_retention(monkeypatch)
    older = _slide(6)[-1]
    monkeypatch.undo()
    schema3 = _schema3_record(5)
    path = tmp_path / "authoritative-schema4.json"
    path.write_bytes(schema4.serialize_schema4_state(_state_with(older, schema3)))
    backend = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        updated_at = BASE + (5 + EVENT_COUNT) * 3 * HOUR
        assert backend.update_server_from_schema3(
            SERVER, schema3, updated_at=updated_at, durable_reason="retention_fixture"
        ) is True
        assert backend.flush().wrote is True
        snapshot = backend.state["schema3_snapshot"]["servers"][SERVER]
        assert snapshot == schema3
        assert backend.state["servers"][SERVER]["legacy_schema3_record"] == schema3
        assert backend.update_server_from_schema3(
            SERVER, schema3, updated_at=updated_at, durable_reason="retention_fixture"
        ) is False
        assert backend.flush().wrote is False
    finally:
        backend.close(flush=False)


def test_synthetic_before_after_sizes_are_reported(monkeypatch, capsys):
    """Records synthetic counts for the implementation report (no assertions on
    exact bytes beyond a strict reduction)."""

    retained = _slide(12, observed_outage_window=False)[-1]
    _without_retention(monkeypatch)
    baseline = _slide(12, observed_outage_window=False)[-1]
    rows = {}
    for name in (
        "continuity_spans",
        "continuity_chains",
        "expected_window_episode_evidence",
        "physical_events",
    ):
        rows[name] = (
            len(baseline[name]), _size(baseline[name]),
            len(retained[name]), _size(retained[name]),
        )
    rows["record"] = (0, _size(baseline), 0, _size(retained))
    with capsys.disabled():
        print("\nRETENTION_SIZES " + json.dumps(rows, sort_keys=True))
    assert rows["continuity_spans"][3] < rows["continuity_spans"][1]
    assert rows["physical_events"][3] < rows["physical_events"][1]
    assert rows["record"][3] < rows["record"][1]
