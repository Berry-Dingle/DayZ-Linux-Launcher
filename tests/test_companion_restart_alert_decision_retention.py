"""Retention R5: bounded alert ledger, bounded decisions, unlimited transfer size.

All data is synthetic and every file lives under pytest's ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

import test_companion_restart_compact_cutover as r4
import test_companion_restart_learned_state_integration as integration
import test_companion_restart_phase2_consumer_state as fx
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers
from dzll_launcher import companion_restart_phase2_consumer_state as state4
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import companion_restart_phase2_storage as storage


SERVER = fx.SERVER
HOUR = fx.HOUR
P3 = 3 * HOUR
WARN = consumers.AuthoritySuppressionKind.SCHEDULED_WARNING
GENERIC = consumers.AuthoritySuppressionKind.GENERIC_RECOVERY
MAX_ALERT_RECORDS = (
    state4.CONSUMER_MAX_SCHEDULED_WARNING_KEYS + state4.CONSUMER_MAX_GENERIC_RECOVERY_KEYS
) * state4.CONSUMER_MAX_RECORDS_PER_KEY


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _warning_key(regime_id, occurrence, server=SERVER):
    return consumers.AuthoritySuppressionKey(
        kind=WARN, server_key=server, regime_id=regime_id, expected_occurrence_at=int(occurrence)
    )


def _generic_key(event_id, server=SERVER):
    return consumers.AuthoritySuppressionKey(kind=GENERIC, server_key=server, event_id=event_id)


def _warning_decision(base, *, tag, regime_id, occurrence):
    key = _warning_key(regime_id, occurrence, server=base.server_key)
    return replace(
        base,
        decision_id=f"r5-consumer-{tag}",
        selected_regime_id=regime_id,
        next_expected_restart_at=float(occurrence),
        scheduled_warning_eligible=True,
        scheduled_warning_at=float(occurrence - 300),
        scheduled_warning_window=(float(occurrence - 330), float(occurrence - 270)),
        scheduled_warning_suppression_key=key,
        scheduled_pre_restart_alert_eligible=True,
    ), key


def _emit(backend, decision, key, at, *, succeeded=True):
    reservation = backend.reserve_consumer_alert(decision=decision, key=key, created_at=at)
    assert reservation.reserved, reservation.reason_codes
    backend.complete_consumer_alert(
        decision=decision, reservation=reservation, completed_at=at + 0.5, action_succeeded=succeeded
    )
    return reservation


def _ledger(backend, server=SERVER):
    return state4.ledger_from_mapping(
        backend.state["servers"][server]["consumer_state"], server_key=server
    )


def _assert_bounded(record, *, server=SERVER):
    assert not schema4._validate_server_record(server, record)
    ledger = state4.ledger_from_mapping(record["consumer_state"], server_key=server)
    keys = {}
    for item in ledger.alert_records:
        keys.setdefault(item.key_id, []).append(item)
    scheduled = [key for key, items in keys.items() if items[0].namespace is WARN]
    generic = [key for key, items in keys.items() if items[0].namespace is GENERIC]
    assert len(scheduled) <= state4.CONSUMER_MAX_SCHEDULED_WARNING_KEYS
    assert len(generic) <= state4.CONSUMER_MAX_GENERIC_RECOVERY_KEYS
    assert all(len(items) <= state4.CONSUMER_MAX_RECORDS_PER_KEY for items in keys.values())
    assert len(ledger.alert_records) <= MAX_ALERT_RECORDS
    assert len(ledger.decisions) <= state4.CONSUMER_DECISION_RETENTION_LIMIT
    assert state4.CONSUMER_LEDGER_BOUNDED_REASON in ledger.reason_codes
    return ledger


def _counts(record):
    consumer = record["consumer_state"]
    return {
        "alert_records": len(consumer["alert_records"]),
        "consumer_decisions": len(consumer["decisions"]),
        "authority_decisions": len(record["authority_decisions"]),
        "physical_events": len(record["physical_events"]),
        "consumer_bytes": len(schema4.canonical_json_bytes(consumer)),
        "record_bytes": len(schema4.canonical_json_bytes(record)),
    }


def _first_occurrence(backend):
    regime = backend.authority_decision(SERVER).selected_shadow_regime
    return regime.regime_id, int(
        consumers.next_phase_occurrence(
            period_seconds=regime.candidate_period_seconds,
            phase_offset=regime.phase_offset,
            now=fx.BASE,
        )
    )


def _add_synthetic_regime(backend, regime_id, period):
    record = copy.deepcopy(backend.state["servers"][SERVER])
    synthetic = copy.deepcopy(record["regimes"][0])
    synthetic["regime_id"] = regime_id
    synthetic["candidate_period_seconds"] = period
    synthetic["status"] = authority.RegimeRecordStatus.PROVISIONAL.value
    record["regimes"].append(synthetic)
    backend.replace_server_record(SERVER, record, reason="r5_synthetic_regime")


# --------------------------------------------------------------------------
# 1. Thousands of scheduled warnings: bounded and still suppressed
# --------------------------------------------------------------------------


def test_thousands_of_scheduled_warnings_stay_bounded_suppressed_and_reopen(tmp_path):
    backend, path, _ = fx._backend(tmp_path)
    base = fx._decision(backend)
    regime_id, first = _first_occurrence(backend)
    total = 2000
    keys = []
    sizes = {}
    for index in range(total):
        occurrence = first + index * P3
        decision, key = _warning_decision(base, tag=index, regime_id=regime_id, occurrence=occurrence)
        backend.record_consumer_decision(decision, created_at=occurrence - 300)
        assert key not in backend.consumer_fired_keys(SERVER), index  # never falsely suppressed
        _emit(backend, decision, key, occurrence - 300)
        keys.append(key)
        record = backend.state["servers"][SERVER]
        if index % 100 == 0 or index == total - 1:
            _assert_bounded(record)
        if index + 1 in {10, 100, 1000, 2000}:
            sizes[index + 1] = _counts(record)
    assert sizes[1000]["alert_records"] == sizes[2000]["alert_records"]
    assert sizes[1000]["consumer_decisions"] == sizes[2000]["consumer_decisions"]
    assert abs(sizes[2000]["consumer_bytes"] - sizes[1000]["consumer_bytes"]) < 64
    ledger = _ledger(backend)
    assert ledger.scheduled_warning_suppressed_through is not None
    assert ledger.compacted_record_count >= 2 * (total - state4.CONSUMER_MAX_SCHEDULED_WARNING_KEYS)
    fired = backend.consumer_fired_keys(SERVER)
    assert all(key in fired for key in keys)
    last = keys[-1].expected_occurrence_at
    for key in keys[::37] + keys[-3:]:
        assert not backend.reserve_consumer_alert(decision=base, key=key, created_at=last).reserved
    # The next real occurrence is not suppressed by the mark.
    upcoming, upcoming_key = _warning_decision(
        base, tag="next", regime_id=regime_id, occurrence=last + P3
    )
    assert upcoming_key not in fired
    backend.close()

    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        fired = again.consumer_fired_keys(SERVER)
        assert all(key in fired for key in keys)
        assert upcoming_key not in fired
        policy_value = consumers.evaluate_authority_consumers(
            consumers.AuthorityConsumerPolicyInput(
                authority_decision=again.authority_decision(SERVER),
                now=float(keys[5].expected_occurrence_at - 300),
                restart_alert_enabled=True,
                server_online_healthy=True,
                fired_keys=fired,
            )
        )
        # Whatever key the consumer derives for an already-warned occurrence
        # is suppressed through ConsumerFiredKeys membership.
        if policy_value.scheduled_warning_suppression_key is not None:
            assert not policy_value.scheduled_pre_restart_alert_eligible
        _emit(again, upcoming, upcoming_key, last + P3 - 300)
        assert not again.reserve_consumer_alert(
            decision=upcoming, key=upcoming_key, created_at=last + P3 - 299
        ).reserved
        _assert_bounded(again.state["servers"][SERVER])
    finally:
        again.close()


# --------------------------------------------------------------------------
# 2. Pending, failed, delivered and duplicate delivery attempts
# --------------------------------------------------------------------------


def test_pending_failed_delivered_and_duplicate_attempts_survive_folding(tmp_path):
    backend, path, _ = fx._backend(tmp_path)
    base = fx._decision(backend)
    regime_id, first = _first_occurrence(backend)

    pending_decision, pending_key = _warning_decision(base, tag="pending", regime_id=regime_id, occurrence=first)
    pending = backend.reserve_consumer_alert(
        decision=pending_decision, key=pending_key, created_at=first - 300
    )
    assert pending.reserved  # crash before completion: never completed here
    failed_decision, failed_key = _warning_decision(
        base, tag="failed", regime_id=regime_id, occurrence=first + P3
    )
    _emit(backend, failed_decision, failed_key, first + P3 - 300, succeeded=False)
    delivered_decision, delivered_key = _warning_decision(
        base, tag="delivered", regime_id=regime_id, occurrence=first + 2 * P3
    )
    _emit(backend, delivered_decision, delivered_key, first + 2 * P3 - 300)
    # A duplicate delivery attempt is refused before and after folding.
    assert not backend.reserve_consumer_alert(
        decision=delivered_decision, key=delivered_key, created_at=first + 2 * P3 - 299
    ).reserved

    lifecycles = {item.key_id: item.lifecycle for item in state4.latest_alert_records(_ledger(backend))}
    assert lifecycles[state4.suppression_key_id(pending_key)] is state4.AlertLifecycle.RESERVED
    assert lifecycles[state4.suppression_key_id(failed_key)] is (
        state4.AlertLifecycle.ACTION_FAILED_AFTER_RESERVATION
    )

    late_key = None
    for index in range(3, 40):
        occurrence = first + index * P3
        decision, late_key = _warning_decision(base, tag=index, regime_id=regime_id, occurrence=occurrence)
        backend.record_consumer_decision(decision, created_at=occurrence - 300)
        _emit(backend, decision, late_key, occurrence - 300)
    remaining = {item.key_id for item in _ledger(backend).alert_records}
    for key in (pending_key, failed_key, delivered_key):
        assert state4.suppression_key_id(key) not in remaining  # folded
        assert key in backend.consumer_fired_keys(SERVER)
        assert not backend.reserve_consumer_alert(
            decision=base, key=key, created_at=late_key.expected_occurrence_at
        ).reserved
    # Completing the folded pending reservation neither raises nor re-adds it.
    result = backend.complete_consumer_alert(
        decision=pending_decision,
        reservation=pending,
        completed_at=late_key.expected_occurrence_at,
        action_succeeded=True,
    )
    assert result.wrote is False
    assert state4.suppression_key_id(pending_key) not in {
        item.key_id for item in _ledger(backend).alert_records
    }
    _assert_bounded(backend.state["servers"][SERVER])
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        fired = again.consumer_fired_keys(SERVER)
        assert all(key in fired for key in (pending_key, failed_key, delivered_key, late_key))
    finally:
        again.close()


def test_a_reservation_at_or_below_the_mark_is_refused_without_a_record(tmp_path):
    backend, _, _ = fx._backend(tmp_path)
    base = fx._decision(backend)
    regime_id, first = _first_occurrence(backend)
    for index in range(20):
        occurrence = first + index * P3
        decision, key = _warning_decision(base, tag=index, regime_id=regime_id, occurrence=occurrence)
        _emit(backend, decision, key, occurrence - 300)
    # An old occurrence (for example after the clock moved back) that was
    # never warned is at or below the mark: it is suppressed, no record added.
    old_decision, old_key = _warning_decision(base, tag="old", regime_id=regime_id, occurrence=first - P3)
    reservation = backend.reserve_consumer_alert(
        decision=old_decision, key=old_key, created_at=first + 30 * P3
    )
    # first - P3 is below the mark already: it is suppressed without a record.
    assert not reservation.reserved and "suppressed_already_fired" in reservation.reason_codes
    _assert_bounded(backend.state["servers"][SERVER])
    backend.close()


def test_protected_keys_are_never_folded():
    ledger = state4.ledger_from_mapping(None, server_key=SERVER)
    now = 2_000_000_000.0
    records = []
    for index in range(20):
        occurrence = int(now - (30 - index) * P3)
        records.append(
            state4.alert_record(
                key=_warning_key("regime-a", occurrence),
                authority_consumer_decision_id="decision",
                created_at=occurrence - 300,
                lifecycle=state4.AlertLifecycle.RESERVED,
                action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE,
            )
        )
        ledger = state4.append_alert(ledger, records[-1])
    oldest = records[0]
    compacted = state4.compact_consumer_ledger(ledger, now=now, protect_key_ids=(oldest.key_id,))
    assert oldest in compacted.alert_records  # same record ID: still completable
    plain = state4.compact_consumer_ledger(ledger, now=now)
    assert oldest not in plain.alert_records
    assert oldest.suppression_key() in state4.consumer_suppression_set(plain)
    generic = state4.ledger_from_mapping(None, server_key=SERVER)
    finalized = {}
    for index in range(20):
        event_id = f"event-{index:02d}"
        finalized[event_id] = now - (20 - index) * P3
        generic = state4.append_alert(
            generic,
            state4.alert_record(
                key=_generic_key(event_id),
                authority_consumer_decision_id="decision",
                created_at=finalized[event_id],
                lifecycle=state4.AlertLifecycle.EMITTED,
                action_outcome=state4.AlertActionOutcome.EMITTED,
            ),
        )
    folded = state4.compact_consumer_ledger(generic, now=now, event_finalized_at=finalized)
    assert len(folded.alert_records) == state4.CONSUMER_RETAINED_GENERIC_RECOVERY_KEYS
    assert folded.generic_recovery_suppressed_through == finalized["event-11"]
    fired = state4.consumer_suppression_set(folded, event_finalized_at=finalized)
    assert all(_generic_key(event_id) in fired for event_id in finalized)
    # An event finalized later is never covered by the mark.
    later = dict(finalized, **{"event-new": now})
    assert _generic_key("event-new") not in state4.consumer_suppression_set(
        folded, event_finalized_at=later
    )
    assert not state4.key_suppressed(folded, _generic_key("event-new"), event_finalized_at=later)


# --------------------------------------------------------------------------
# 3. Regime changes, reused and superseded schedules
# --------------------------------------------------------------------------


def test_regime_changes_reused_and_superseded_schedules_stay_bounded_and_deduplicated(tmp_path):
    backend, path, _ = fx._backend(tmp_path)
    base = fx._decision(backend)
    regime_a, first = _first_occurrence(backend)
    regime_b = "r5-synthetic-4h-regime"
    _add_synthetic_regime(backend, regime_b, 4 * HOUR)
    emitted = []
    occurrence = first
    for block in range(12):
        regime_id = regime_a if block % 2 == 0 else regime_b  # A is reused after B
        period = P3 if regime_id == regime_a else 4 * HOUR
        for index in range(25):
            decision, key = _warning_decision(
                base, tag=f"{block}-{index}", regime_id=regime_id, occurrence=occurrence
            )
            backend.record_consumer_decision(decision, created_at=occurrence - 300)
            assert key not in backend.consumer_fired_keys(SERVER)
            _emit(backend, decision, key, occurrence - 300)
            emitted.append(key)
            occurrence += period
        record = backend.state["servers"][SERVER]
        ledger = _assert_bounded(record)
        latest = state4.latest_alert_records(ledger)
        # Old-regime warnings that are still retained are invalidated, never replayable.
        for item in latest:
            if item.namespace is WARN and item.regime_id != regime_id:
                assert item.lifecycle is state4.AlertLifecycle.INVALIDATED_BY_REGIME_CHANGE
    # A suspected-change decision (no selected regime) invalidates everything retained.
    suspended = replace(
        base,
        decision_id="r5-suspended",
        selected_regime_id=None,
        scheduled_warning_suppression_key=None,
        scheduled_pre_restart_alert_eligible=False,
    )
    backend.record_consumer_decision(suspended, created_at=occurrence)
    fired = backend.consumer_fired_keys(SERVER)
    assert all(key in fired for key in emitted)
    # The reused regime's next real occurrence is allowed exactly once.
    decision, key = _warning_decision(base, tag="return", regime_id=regime_a, occurrence=occurrence + P3)
    backend.record_consumer_decision(decision, created_at=occurrence + P3 - 300)
    assert key not in backend.consumer_fired_keys(SERVER)
    _emit(backend, decision, key, occurrence + P3 - 300)
    assert not backend.reserve_consumer_alert(
        decision=decision, key=key, created_at=occurrence + P3 - 299
    ).reserved
    _assert_bounded(backend.state["servers"][SERVER])
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        fired = again.consumer_fired_keys(SERVER)
        assert all(item in fired for item in emitted + [key])
    finally:
        again.close()


def test_the_high_water_mark_never_suppresses_a_future_occurrence():
    ledger = state4.ledger_from_mapping(None, server_key=SERVER)
    now = 2_000_000_000.0
    for index in range(30):
        occurrence = int(now - (40 - index) * P3)
        record = state4.alert_record(
            key=_warning_key("regime-a", occurrence),
            authority_consumer_decision_id="decision",
            created_at=occurrence - 300,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
        )
        ledger = state4.append_alert(ledger, record)
    compacted = state4.compact_consumer_ledger(ledger, now=now)
    mark = compacted.scheduled_warning_suppressed_through
    assert mark is not None and mark <= now - state4.SCHEDULED_WARNING_FOLD_MARGIN_SECONDS
    fired = state4.consumer_suppression_set(compacted)
    # Past occurrences of any regime at or below the mark are suppressed ...
    assert _warning_key("regime-a", mark) in fired
    assert _warning_key("other-regime", mark - 10) in fired
    # ... while every occurrence that could still become due is not.
    for regime in ("regime-a", "other-regime"):
        assert _warning_key(regime, int(now) + 1) not in fired
        assert _warning_key(regime, int(now) + 300) not in fired
    assert len({item.key_id for item in compacted.alert_records}) == (
        state4.CONSUMER_RETAINED_SCHEDULED_WARNING_KEYS
    )


def test_recent_occurrences_are_kept_and_the_hard_cap_errs_towards_suppression():
    ledger = state4.ledger_from_mapping(None, server_key=SERVER)
    now = 2_000_000_000.0
    # 20 distinct keys, all occurrences recent (inside the fold margin).
    for index in range(20):
        record = state4.alert_record(
            key=_warning_key(f"regime-{index}", int(now) + 30 - index),
            authority_consumer_decision_id="decision",
            created_at=now,
            lifecycle=state4.AlertLifecycle.RESERVED,
            action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE,
        )
        ledger = state4.append_alert(ledger, record)
    compacted = state4.compact_consumer_ledger(ledger, now=now)
    retained = {item.key_id for item in compacted.alert_records}
    assert len(retained) == state4.CONSUMER_MAX_SCHEDULED_WARNING_KEYS
    fired = state4.consumer_suppression_set(compacted)
    for item in ledger.alert_records:
        assert item.suppression_key() in fired


# --------------------------------------------------------------------------
# 4. Legacy (pre-R5) unbounded ledgers are bounded on their first write
# --------------------------------------------------------------------------


def _legacy_ledger(backend, *, count, regime_id, first):
    auth_id = backend.authority_decision(SERVER).decision_id
    decisions = []
    alerts = []
    for index in range(count):
        decision_id = f"legacy-consumer-{index:04d}"
        decisions.append(
            state4.ConsumerDecisionRecord(
                schema_version=state4.CONSUMER_STATE_SCHEMA_VERSION,
                semantic_version=state4.CONSUMER_STATE_SEMANTIC_VERSION,
                decision_id=decision_id,
                authority_decision_id=auth_id,
                server_key=SERVER,
                selected_regime_id=regime_id,
                presentation_state=consumers.AuthorityPresentationKey.CONFIRMED_CYCLE.value,
                cycle_period_seconds=P3,
                confidence_display_value=0.97,
                next_expected_restart_at=None,
                countdown_safe=True,
                prediction_suspended=False,
                reason_codes=("r5_legacy_fixture",),
            )
        )
        occurrence = first + index * P3
        reserved = state4.alert_record(
            key=_warning_key(regime_id, occurrence),
            authority_consumer_decision_id=decision_id,
            created_at=occurrence - 300,
            lifecycle=state4.AlertLifecycle.RESERVED,
            action_outcome=state4.AlertActionOutcome.RESERVED_AT_MOST_ONCE,
        )
        emitted = state4.alert_record(
            key=reserved.suppression_key(),
            authority_consumer_decision_id=decision_id,
            created_at=occurrence - 299.5,
            lifecycle=state4.AlertLifecycle.EMITTED,
            action_outcome=state4.AlertActionOutcome.EMITTED,
            supersedes_record_id=reserved.record_id,
        )
        alerts.extend((reserved, emitted))
    ledger = replace(
        state4.ledger_from_mapping(None, server_key=SERVER),
        current_authority_consumer_decision_id=decisions[-1].decision_id,
        decisions=tuple(decisions),
        alert_records=tuple(alerts),
    )
    return ledger


def test_legacy_unbounded_ledger_loads_then_bounds_on_first_write_without_losing_suppression(
    tmp_path,
):
    backend, path, _ = fx._backend(tmp_path)
    regime_id, first = _first_occurrence(backend)
    legacy = _legacy_ledger(backend, count=400, regime_id=regime_id, first=first)
    record = copy.deepcopy(backend.state["servers"][SERVER])
    record["consumer_state"] = state4.ledger_to_mapping(legacy)
    # A pre-R5 ledger has no bounded marker: it is valid as it is.
    assert not schema4._validate_server_record(SERVER, record)
    backend.replace_server_record(SERVER, record, reason="r5_legacy_fixture")
    backend.flush()
    before = _counts(backend.state["servers"][SERVER])
    keys = [item.suppression_key() for item in state4.latest_alert_records(legacy)]
    assert all(key in backend.consumer_fired_keys(SERVER) for key in keys)

    base = fx._decision(backend)
    now = first + 401 * P3
    backend.record_consumer_decision(replace(base, decision_id="r5-first-write"), created_at=now)
    after_record = backend.state["servers"][SERVER]
    _assert_bounded(after_record)
    after = _counts(after_record)
    assert before["alert_records"] == 800 and before["consumer_decisions"] == 400
    assert after["alert_records"] <= MAX_ALERT_RECORDS
    assert after["consumer_decisions"] <= state4.CONSUMER_DECISION_RETENTION_LIMIT
    fired = backend.consumer_fired_keys(SERVER)
    assert all(key in fired for key in keys)
    backend.close()
    again = live4.AuthoritativeSchema4Runtime.open(path, enabled=True)
    try:
        fired = again.consumer_fired_keys(SERVER)
        assert all(key in fired for key in keys)
        for key in keys[::50]:
            assert not again.reserve_consumer_alert(decision=base, key=key, created_at=now).reserved
    finally:
        again.close()


# --------------------------------------------------------------------------
# 5. Strict validation of bounded records
# --------------------------------------------------------------------------


def _bounded_record(tmp_path):
    backend, _, _ = fx._backend(tmp_path)
    base = fx._decision(backend)
    regime_id, first = _first_occurrence(backend)
    for index in range(20):
        occurrence = first + index * P3
        decision, key = _warning_decision(base, tag=index, regime_id=regime_id, occurrence=occurrence)
        backend.record_consumer_decision(decision, created_at=occurrence - 300)
        _emit(backend, decision, key, occurrence - 300)
    record = copy.deepcopy(backend.state["servers"][SERVER])
    backend.close(flush=False)
    _assert_bounded(record)
    return record, regime_id, first


def _codes(record):
    return {item.detail.split(":")[0] for item in schema4._validate_server_record(SERVER, record)}


@pytest.mark.parametrize(
    "mutation, code",
    [
        ("negative_scheduled_mark", "invalid_consumer_state"),
        ("boolean_scheduled_mark", "invalid_consumer_state"),
        ("float_scheduled_mark", "invalid_consumer_state"),
        ("nonfinite_generic_mark", "invalid_consumer_state"),
        ("too_many_scheduled_keys", "consumer_scheduled_warning_retention_exceeded"),
        ("long_lineage", "consumer_alert_lineage_retention_exceeded"),
        ("too_many_decisions", "consumer_decision_retention_exceeded"),
    ],
)
def test_strict_validation_rejects_malformed_or_unbounded_bounded_ledgers(mutation, code, tmp_path):
    record, regime_id, first = _bounded_record(tmp_path)
    consumer = record["consumer_state"]
    meta = consumer["compaction_metadata"]
    ledger = state4.ledger_from_mapping(consumer, server_key=SERVER)
    if mutation == "negative_scheduled_mark":
        meta[state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD] = -1
    elif mutation == "boolean_scheduled_mark":
        meta[state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD] = True
    elif mutation == "float_scheduled_mark":
        meta[state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD] = 1.5
    elif mutation == "nonfinite_generic_mark":
        meta[state4.GENERIC_SUPPRESSED_THROUGH_FIELD] = float("inf")
    elif mutation == "too_many_scheduled_keys":
        extra = [
            state4.alert_record(
                key=_warning_key(regime_id, first - index * P3),
                authority_consumer_decision_id=ledger.current_authority_consumer_decision_id,
                created_at=fx.BASE,
                lifecycle=state4.AlertLifecycle.EMITTED,
                action_outcome=state4.AlertActionOutcome.EMITTED,
            )
            for index in range(1, 10)
        ]
        record["consumer_state"] = state4.ledger_to_mapping(
            replace(ledger, alert_records=ledger.alert_records + tuple(extra))
        )
    elif mutation == "long_lineage":
        latest = state4.latest_alert_records(ledger)[-1]
        chain = []
        previous = latest
        for step in range(3):
            previous = state4.alert_record(
                key=latest.suppression_key(),
                authority_consumer_decision_id=latest.authority_consumer_decision_id,
                created_at=latest.created_at + step + 1,
                lifecycle=(
                    state4.AlertLifecycle.INVALIDATED_BY_REGIME_CHANGE
                    if step % 2 == 0
                    else state4.AlertLifecycle.EMITTED
                ),
                action_outcome=state4.AlertActionOutcome.INVALIDATED,
                supersedes_record_id=previous.record_id,
            )
            chain.append(previous)
        record["consumer_state"] = state4.ledger_to_mapping(
            replace(ledger, alert_records=ledger.alert_records + tuple(chain))
        )
    elif mutation == "too_many_decisions":
        template = ledger.decisions[0]
        extra = tuple(
            replace(template, decision_id=f"extra-{index:03d}") for index in range(70)
        )
        record["consumer_state"] = state4.ledger_to_mapping(
            replace(ledger, decisions=ledger.decisions + extra)
        )
    issues = schema4._validate_server_record(SERVER, record)
    assert issues
    assert any(code in f"{item.code}:{item.detail}" for item in issues)


def test_strict_validation_enforces_the_authority_decision_bound_on_marked_records(tmp_path):
    record, _, _ = _bounded_record(tmp_path)
    template = record["authority_decisions"][0]
    record["reason_codes"] = sorted(
        set(record["reason_codes"]) | {schema4.DECISION_HISTORY_BOUNDED_REASON}
    )
    record["authority_decisions"] = [template] + [
        dict(template, decision_id=f"extra-authority-{index:03d}") for index in range(70)
    ]
    issues = schema4._validate_server_record(SERVER, record)
    assert any(item.code == "authority_decision_retention_exceeded" for item in issues)


# --------------------------------------------------------------------------
# 6. Production rebuild path: generic recovery, warnings, decisions, 80 events
# --------------------------------------------------------------------------


def test_production_rebuilds_bound_decisions_and_alerts_and_keep_every_alert_suppressed(tmp_path):
    active = tmp_path / "companion_restart_learning_phase2.json"
    schema4.prepare_schema4_state_for_startup(
        active_path=active, legacy_path=tmp_path / "companion_restart_learning.json", now=s8.DAY0
    )
    pipeline = r4.chain(400)
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    generic_keys = []
    warning_keys = []
    history = []
    try:
        for stop in range(20, 401, 4):
            updated_at = ordered[stop - 1].finalized_at + 60
            backend.update_server_from_schema3(
                s8.SERVER,
                integration.schema3_record(ordered[:stop], pipeline.coverage),
                updated_at=updated_at,
                durable_reason="r5_fixture",
            )
            backend.flush()
            record = backend.state["servers"][s8.SERVER]
            step = r4.production_step(record)
            decision = s8.consume(step, now=updated_at)
            backend.record_consumer_decision(decision, created_at=updated_at)
            generic = _generic_key(ordered[stop - 1].event_id, server=s8.SERVER)
            assert generic not in backend.consumer_fired_keys(s8.SERVER)
            _emit(backend, decision, generic, updated_at)
            generic_keys.append(generic)
            regime = schema4.persisted_authority_decision(record).selected_shadow_regime
            if regime is not None:
                warning = _warning_key(
                    regime.regime_id, ordered[stop - 1].canonical_phase_at + P3, server=s8.SERVER
                )
                assert warning not in backend.consumer_fired_keys(s8.SERVER)
                _emit(backend, decision, warning, updated_at + 1)
                warning_keys.append(warning)
            record = backend.state["servers"][s8.SERVER]
            _assert_bounded(record, server=s8.SERVER)
            assert len(record["physical_events"]) <= schema4.RAW_EVENT_RETENTION_LIMIT
            assert len(record["authority_decisions"]) <= schema4.AUTHORITY_DECISION_RETENTION_LIMIT
            assert schema4.DECISION_HISTORY_BOUNDED_REASON in record["reason_codes"]
            history.append(_counts(record))
        backend.flush()
        record = backend.state["servers"][s8.SERVER]
        assert len(record["authority_decisions"]) == schema4.AUTHORITY_DECISION_RETENTION_LIMIT
        retained = {item["event_id"] for item in record["physical_events"]}
        fired = backend.consumer_fired_keys(s8.SERVER)
        last_at = ordered[-1].finalized_at + 120
        for key in generic_keys:
            if key.event_id in retained:
                assert key in fired
            assert not backend.reserve_consumer_alert(
                decision=decision, key=key, created_at=last_at
            ).reserved
        assert all(key in fired for key in warning_keys)
        assert len(warning_keys) > 50
        ledger = _ledger(backend, s8.SERVER)
        assert ledger.generic_recovery_suppressed_through is not None
    finally:
        backend.close()
    # Steady state: the bookkeeping stops growing.
    late = history[-20:]
    assert len({item["alert_records"] for item in late}) <= 2
    assert max(item["consumer_decisions"] for item in history) <= 64
    reopened = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        fired = reopened.consumer_fired_keys(s8.SERVER)
        assert all(key in fired for key in warning_keys)
        _assert_bounded(reopened.state["servers"][s8.SERVER], server=s8.SERVER)
    finally:
        reopened.close(flush=False)


# --------------------------------------------------------------------------
# 7. Learning transfer: no 8 MiB limit, full validation, exact knowledge
# --------------------------------------------------------------------------


def _many_server_state(count):
    value = storage.new_phase2_state(
        now=fx.BASE + 10 * HOUR,
        generation_id="00000000-0000-4000-8000-0000000000b2",
    )
    value["servers"] = {SERVER: fx._server_record(SERVER)}
    for index in range(count):
        key = f"r5-transfer-{index:03d}.example:2302"
        value["servers"][key] = fx._server_record(key)
    raw = schema4.canonical_json_bytes(value)
    return schema4.migrate_schema3_state(json.loads(raw), source_bytes=raw, source_mtime_ns=1)


def _knowledge(state):
    fields = (
        "learned_state", "regimes", "authority_decision", "authority_decisions",
        "consumer_state", "physical_events", "interval_relationships",
        "expected_window_results", "legacy_schema3_record",
    )
    return {
        key: {name: record.get(name) for name in fields}
        for key, record in state["servers"].items()
    }


@pytest.mark.parametrize("extra_servers", [3, 100])
def test_learning_export_and_import_round_trip_below_and_above_8_mib(extra_servers, tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(schema4.serialize_schema4_state(_many_server_state(extra_servers)))
    backend = live4.AuthoritativeSchema4Runtime.open(source, enabled=True)
    try:
        base = fx._decision(backend)
        regime_id, first = _first_occurrence(backend)
        keys = []
        for index in range(30):
            occurrence = first + index * P3
            decision, key = _warning_decision(base, tag=index, regime_id=regime_id, occurrence=occurrence)
            backend.record_consumer_decision(decision, created_at=occurrence - 300)
            _emit(backend, decision, key, occurrence - 300)
            keys.append(key)
        generic = fx._generic_decision(backend)
        _emit(backend, generic, generic.generic_recovery_suppression_key, first + 31 * P3)
        backend.flush()
        snapshot = backend.export_snapshot()
        original = copy.deepcopy(backend.state)
    finally:
        backend.close()
    size = len(snapshot.canonical_bytes)
    if extra_servers >= 100:
        assert size > 8 * 1024 * 1024
    else:
        assert size < 8 * 1024 * 1024

    export = tmp_path / "exports" / transfer.default_export_filename()
    transfer.write_export_file(export, snapshot.canonical_bytes)
    validated = transfer.validate_external_learning_file(export)
    assert validated.server_count == extra_servers + 1
    assert validated.canonical_bytes == snapshot.canonical_bytes
    config = tmp_path / "config"
    pending = transfer.stage_pending_import(validated, config_dir=config)
    assert pending.sha256 == validated.sha256
    live = config / "companion_restart_learning_phase2.json"
    assert not live.exists()  # no prior database: no backup is created
    result = transfer.apply_pending_import_at_startup(config_dir=config, live_path=live)
    assert result.status == "applied" and result.backup_path is None
    names = {path.name for path in config.iterdir()}
    assert not any(transfer.IMPORT_BACKUP_PREFIX in name or "last-known-good" in name for name in names)
    assert not any(name.startswith(transfer.PENDING_PAYLOAD_NAME) for name in names)

    imported = live4.AuthoritativeSchema4Runtime.open(live, enabled=True)
    try:
        assert _knowledge(imported.state) == _knowledge(original)
        fired = imported.consumer_fired_keys(SERVER)
        assert all(key in fired for key in keys)
        assert generic.generic_recovery_suppression_key in fired
        ledger = _ledger(imported)
        assert ledger.scheduled_warning_suppressed_through is not None
        assert ledger.scheduled_warning_suppressed_through == state4.ledger_from_mapping(
            original["servers"][SERVER]["consumer_state"], server_key=SERVER
        ).scheduled_warning_suppressed_through
        for key in keys[::7]:
            assert not imported.reserve_consumer_alert(
                decision=base, key=key, created_at=first + 40 * P3
            ).reserved
    finally:
        imported.close(flush=False)


def test_large_inputs_are_still_fully_validated(tmp_path):
    raw = schema4.serialize_schema4_state(_many_server_state(100))
    assert len(raw) > 8 * 1024 * 1024
    with pytest.raises(transfer.ExternalLearningValidationError, match="Invalid JSON"):
        transfer.validate_external_learning_bytes(raw[:-10])
    state = json.loads(raw)
    record = state["servers"][SERVER]
    record["consumer_state"]["compaction_metadata"][state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD] = True
    with pytest.raises(transfer.ExternalLearningValidationError, match="numeric, not boolean"):
        transfer.validate_external_learning_bytes(schema4.canonical_json_bytes(state))
    record["consumer_state"]["compaction_metadata"][state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD] = -5
    with pytest.raises(transfer.ExternalLearningValidationError, match="invalid_consumer_state"):
        transfer.validate_external_learning_bytes(schema4.canonical_json_bytes(state))
    del record["consumer_state"]["compaction_metadata"][state4.SCHEDULED_SUPPRESSED_THROUGH_FIELD]
    state["servers"]["r5-transfer-050.example:2302"]["authority_decision"]["decision_id"] = "x"
    with pytest.raises(transfer.ExternalLearningValidationError):
        transfer.validate_external_learning_bytes(schema4.canonical_json_bytes(state))


def test_import_read_failures_fail_safely(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    with pytest.raises(transfer.ExternalLearningValidationError, match="Cannot read"):
        transfer.validate_external_learning_file(missing)
    path = tmp_path / "large.json"
    path.write_bytes(schema4.serialize_schema4_state(_many_server_state(3)))

    def no_memory(*_args, **_kwargs):
        raise MemoryError()

    monkeypatch.setattr(transfer, "_validate_external_learning_bytes", no_memory)
    with pytest.raises(transfer.ExternalLearningValidationError, match="not enough memory"):
        transfer.validate_external_learning_file(path)
