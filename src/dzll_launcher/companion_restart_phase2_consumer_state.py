from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Iterable, Mapping

from .companion_restart_phase2_authority_consumers import (
    AUTHORITY_CONSUMER_SEMANTIC_VERSION,
    AuthorityConsumerDecision,
    AuthoritySuppressionKey,
    AuthoritySuppressionKind,
)


CONSUMER_STATE_SCHEMA_VERSION = 1
CONSUMER_STATE_SEMANTIC_VERSION = 1
CONSUMER_STATE_ADAPTER_VERSION = AUTHORITY_CONSUMER_SEMANTIC_VERSION
AUTO_COMPACTION_ENABLED_DEFAULT = False


class AlertLifecycle(str, Enum):
    ELIGIBLE = "eligible"
    NOT_DUE = "not_due"
    DUE = "due"
    RESERVED = "reserved"
    EMITTED = "emitted"
    SUPPRESSED_ALREADY_FIRED = "suppressed_already_fired"
    SUPPRESSED_TRANSITION = "suppressed_transition"
    SUPPRESSED_UNSAFE_PREDICTION = "suppressed_unsafe_prediction"
    EXPIRED = "expired"
    INVALIDATED_BY_REGIME_CHANGE = "invalidated_by_regime_change"
    ACTION_FAILED_AFTER_RESERVATION = "action_failed_after_reservation"


class AlertActionOutcome(str, Enum):
    NONE = "none"
    RESERVED_AT_MOST_ONCE = "reserved_at_most_once"
    EMITTED = "emitted"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    ACTION_FAILED = "action_failed"


FIRED_LIFECYCLES = frozenset(
    {
        AlertLifecycle.RESERVED,
        AlertLifecycle.EMITTED,
        AlertLifecycle.ACTION_FAILED_AFTER_RESERVATION,
        AlertLifecycle.SUPPRESSED_ALREADY_FIRED,
        AlertLifecycle.INVALIDATED_BY_REGIME_CHANGE,
    }
)


@dataclass(frozen=True)
class ConsumerAlertRecord:
    schema_version: int
    semantic_version: int
    record_id: str
    key_id: str
    namespace: AuthoritySuppressionKind
    server_key: str
    authority_consumer_decision_id: str
    regime_id: str | None
    expected_occurrence_at: int | None
    physical_event_id: str | None
    created_at: float
    lifecycle: AlertLifecycle
    action_outcome: AlertActionOutcome
    supersedes_record_id: str | None
    compacted: bool
    reason_codes: tuple[str, ...]

    def suppression_key(self) -> AuthoritySuppressionKey:
        return AuthoritySuppressionKey(
            kind=self.namespace,
            server_key=self.server_key,
            regime_id=self.regime_id,
            expected_occurrence_at=self.expected_occurrence_at,
            event_id=self.physical_event_id,
        )


@dataclass(frozen=True)
class ConsumerDecisionRecord:
    schema_version: int
    semantic_version: int
    decision_id: str
    authority_decision_id: str
    server_key: str
    selected_regime_id: str | None
    presentation_state: str
    cycle_period_seconds: int | None
    confidence_display_value: float
    next_expected_restart_at: float | None
    countdown_safe: bool
    prediction_suspended: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ConsumerStateLedger:
    schema_version: int
    semantic_version: int
    consumer_adapter_version: int
    server_key: str
    current_authority_consumer_decision_id: str | None
    decisions: tuple[ConsumerDecisionRecord, ...]
    alert_records: tuple[ConsumerAlertRecord, ...]
    compacted_record_count: int
    compaction_digest: str | None
    last_compacted_at: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AutoCompactionPolicy:
    enabled: bool = AUTO_COMPACTION_ENABLED_DEFAULT
    file_size_threshold_bytes: int = 8 * 1024 * 1024
    raw_sample_threshold: int = 4096
    legacy_coverage_threshold: int = 4096
    decision_history_threshold: int = 64
    consumer_alert_record_threshold: int = 512
    retained_scheduled_occurrences_per_regime: int = 8


def empty_consumer_state(server_key: str) -> dict:
    if not server_key:
        raise ValueError("server_key is required")
    return {
        "schema_version": CONSUMER_STATE_SCHEMA_VERSION,
        "semantic_version": CONSUMER_STATE_SEMANTIC_VERSION,
        "consumer_adapter_version": CONSUMER_STATE_ADAPTER_VERSION,
        "server_key": server_key,
        "current_authority_consumer_decision_id": None,
        "decisions": [],
        "alert_records": [],
        "compaction_metadata": {
            "compacted_record_count": 0,
            "compaction_digest": None,
            "last_compacted_at": None,
        },
        "reason_codes": ["consumer_state_separate_from_learning_evidence"],
    }


def decision_record(decision: AuthorityConsumerDecision) -> ConsumerDecisionRecord:
    return ConsumerDecisionRecord(
        schema_version=CONSUMER_STATE_SCHEMA_VERSION,
        semantic_version=CONSUMER_STATE_SEMANTIC_VERSION,
        decision_id=decision.decision_id,
        authority_decision_id=decision.authority_decision_id,
        server_key=decision.server_key,
        selected_regime_id=decision.selected_regime_id,
        presentation_state=decision.presentation_state.value,
        cycle_period_seconds=decision.cycle_period_seconds,
        confidence_display_value=decision.confidence_display_value,
        next_expected_restart_at=decision.next_expected_restart_at,
        countdown_safe=decision.countdown_safe,
        prediction_suspended=decision.prediction_suspended,
        reason_codes=decision.reason_codes,
    )


def suppression_key_id(key: AuthoritySuppressionKey) -> str:
    return _stable_id("schema4-consumer-suppression-key", key.serialize())


def alert_record(
    *,
    key: AuthoritySuppressionKey,
    authority_consumer_decision_id: str,
    created_at: float,
    lifecycle: AlertLifecycle,
    action_outcome: AlertActionOutcome,
    supersedes_record_id: str | None = None,
    compacted: bool = False,
    reason_codes: Iterable[str] = (),
) -> ConsumerAlertRecord:
    _finite_nonnegative("created_at", created_at)
    key_id = suppression_key_id(key)
    reasons = tuple(sorted(set(str(item) for item in reason_codes if str(item))))
    payload = {
        "key_id": key_id,
        "decision": authority_consumer_decision_id,
        "created_at": round(created_at, 6),
        "lifecycle": lifecycle.value,
        "outcome": action_outcome.value,
        "supersedes": supersedes_record_id,
        "compacted": compacted,
        "reasons": reasons,
    }
    return ConsumerAlertRecord(
        schema_version=CONSUMER_STATE_SCHEMA_VERSION,
        semantic_version=CONSUMER_STATE_SEMANTIC_VERSION,
        record_id=_stable_id("schema4-consumer-alert-record", payload),
        key_id=key_id,
        namespace=key.kind,
        server_key=key.server_key,
        authority_consumer_decision_id=authority_consumer_decision_id,
        regime_id=key.regime_id,
        expected_occurrence_at=key.expected_occurrence_at,
        physical_event_id=key.event_id,
        created_at=float(created_at),
        lifecycle=lifecycle,
        action_outcome=action_outcome,
        supersedes_record_id=supersedes_record_id,
        compacted=bool(compacted),
        reason_codes=reasons,
    )


def ledger_from_mapping(value: Mapping[str, object] | None, *, server_key: str) -> ConsumerStateLedger:
    raw = empty_consumer_state(server_key) if value is None else value
    if not isinstance(raw, Mapping):
        raise ValueError("consumer_state must be a mapping")
    decisions = tuple(_decision_from(item) for item in raw.get("decisions", ()))
    alerts = tuple(_alert_from(item) for item in raw.get("alert_records", ()))
    meta = raw.get("compaction_metadata")
    if not isinstance(meta, Mapping):
        raise ValueError("consumer compaction metadata must be a mapping")
    ledger = ConsumerStateLedger(
        schema_version=int(raw.get("schema_version", 0)),
        semantic_version=int(raw.get("semantic_version", 0)),
        consumer_adapter_version=int(raw.get("consumer_adapter_version", 0)),
        server_key=str(raw.get("server_key") or ""),
        current_authority_consumer_decision_id=(
            str(raw["current_authority_consumer_decision_id"])
            if raw.get("current_authority_consumer_decision_id") is not None
            else None
        ),
        decisions=decisions,
        alert_records=alerts,
        compacted_record_count=int(meta.get("compacted_record_count", 0)),
        compaction_digest=(
            str(meta["compaction_digest"])
            if meta.get("compaction_digest") is not None
            else None
        ),
        last_compacted_at=(
            float(meta["last_compacted_at"])
            if meta.get("last_compacted_at") is not None
            else None
        ),
        reason_codes=tuple(str(item) for item in raw.get("reason_codes", ())),
    )
    _validate_ledger_shape(ledger, expected_server_key=server_key)
    return ledger


def ledger_to_mapping(ledger: ConsumerStateLedger) -> dict:
    return {
        "schema_version": ledger.schema_version,
        "semantic_version": ledger.semantic_version,
        "consumer_adapter_version": ledger.consumer_adapter_version,
        "server_key": ledger.server_key,
        "current_authority_consumer_decision_id": ledger.current_authority_consumer_decision_id,
        "decisions": [_primitive(item) for item in sorted(ledger.decisions, key=lambda item: item.decision_id)],
        "alert_records": [_primitive(item) for item in sorted(ledger.alert_records, key=lambda item: item.record_id)],
        "compaction_metadata": {
            "compacted_record_count": ledger.compacted_record_count,
            "compaction_digest": ledger.compaction_digest,
            "last_compacted_at": ledger.last_compacted_at,
        },
        "reason_codes": sorted(set(ledger.reason_codes)),
    }


def with_decision(
    ledger: ConsumerStateLedger, decision: AuthorityConsumerDecision, *, retain: int = 64
) -> ConsumerStateLedger:
    record = decision_record(decision)
    by_id = {item.decision_id: item for item in ledger.decisions}
    by_id[record.decision_id] = record
    values = sorted(by_id.values(), key=lambda item: item.decision_id)
    if len(values) > retain:
        protected = {
            record.decision_id,
            ledger.current_authority_consumer_decision_id,
            *(item.authority_consumer_decision_id for item in ledger.alert_records),
        }
        removable = [item for item in values if item.decision_id not in protected]
        remove = {item.decision_id for item in removable[: max(0, len(values) - retain)]}
        values = [item for item in values if item.decision_id not in remove]
    return replace(
        ledger,
        current_authority_consumer_decision_id=record.decision_id,
        decisions=tuple(values),
    )


def append_alert(ledger: ConsumerStateLedger, record: ConsumerAlertRecord) -> ConsumerStateLedger:
    if record.server_key != ledger.server_key:
        raise ValueError("alert record server does not match ledger")
    by_id = {item.record_id: item for item in ledger.alert_records}
    existing = by_id.get(record.record_id)
    if existing is not None:
        return ledger
    if record.supersedes_record_id is not None and record.supersedes_record_id not in by_id:
        raise ValueError("alert record supersedes an unknown record")
    by_id[record.record_id] = record
    return replace(
        ledger,
        alert_records=tuple(sorted(by_id.values(), key=lambda item: item.record_id)),
    )


def invalidate_superseded_regime_warnings(
    ledger: ConsumerStateLedger,
    *,
    active_regime_id: str | None,
    authority_consumer_decision_id: str,
    created_at: float,
) -> ConsumerStateLedger:
    updated = ledger
    for item in latest_alert_records(ledger):
        if (
            item.namespace is not AuthoritySuppressionKind.SCHEDULED_WARNING
            or item.regime_id == active_regime_id
            or item.lifecycle is AlertLifecycle.INVALIDATED_BY_REGIME_CHANGE
        ):
            continue
        revision = alert_record(
            key=item.suppression_key(),
            authority_consumer_decision_id=authority_consumer_decision_id,
            created_at=created_at,
            lifecycle=AlertLifecycle.INVALIDATED_BY_REGIME_CHANGE,
            action_outcome=AlertActionOutcome.INVALIDATED,
            supersedes_record_id=item.record_id,
            reason_codes=(
                "scheduled_warning_invalidated_by_regime_change",
                "old_regime_warning_cannot_replay",
            ),
        )
        updated = append_alert(updated, revision)
    return updated


def latest_alert_records(ledger: ConsumerStateLedger) -> tuple[ConsumerAlertRecord, ...]:
    superseded = {
        item.supersedes_record_id
        for item in ledger.alert_records
        if item.supersedes_record_id is not None
    }
    current = [item for item in ledger.alert_records if item.record_id not in superseded]
    by_key: dict[str, ConsumerAlertRecord] = {}
    for item in sorted(current, key=lambda value: (value.created_at, value.record_id)):
        by_key[item.key_id] = item
    return tuple(sorted(by_key.values(), key=lambda item: item.key_id))


def fired_suppression_keys(ledger: ConsumerStateLedger) -> frozenset[AuthoritySuppressionKey]:
    return frozenset(
        item.suppression_key()
        for item in latest_alert_records(ledger)
        if item.lifecycle in FIRED_LIFECYCLES
    )


def compact_consumer_ledger(
    ledger: ConsumerStateLedger,
    *,
    now: float,
    superseded_regime_ids: Iterable[str] = (),
    retained_scheduled_occurrences_per_regime: int = 8,
) -> ConsumerStateLedger:
    _finite_nonnegative("now", now)
    superseded = set(superseded_regime_ids)
    latest = list(latest_alert_records(ledger))
    scheduled: dict[str, list[ConsumerAlertRecord]] = {}
    retained: list[ConsumerAlertRecord] = []
    for item in latest:
        if item.namespace is AuthoritySuppressionKind.SCHEDULED_WARNING:
            scheduled.setdefault(item.regime_id or "", []).append(item)
        else:
            retained.append(item)
    for regime_id, values in scheduled.items():
        values.sort(key=lambda item: (item.expected_occurrence_at or -1, item.record_id))
        keep = retained_scheduled_occurrences_per_regime
        if regime_id not in superseded:
            keep = max(keep, 2)
        retained.extend(values[-keep:])
    retained_ids = {item.record_id for item in retained}
    summarized = []
    for item in retained:
        if item.supersedes_record_id is not None and item.supersedes_record_id not in retained_ids:
            summarized.append(
                alert_record(
                    key=item.suppression_key(),
                    authority_consumer_decision_id=item.authority_consumer_decision_id,
                    created_at=item.created_at,
                    lifecycle=item.lifecycle,
                    action_outcome=item.action_outcome,
                    compacted=True,
                    reason_codes=tuple(
                        sorted(set(item.reason_codes) | {"prior_lineage_compacted"})
                    ),
                )
            )
        else:
            summarized.append(item)
    retained = summarized
    removed = sorted(
        set(item.record_id for item in ledger.alert_records)
        - set(item.record_id for item in retained)
    )
    digest = (
        _stable_id("schema4-consumer-compaction", removed)
        if removed
        else ledger.compaction_digest
    )
    return ConsumerStateLedger(
        schema_version=ledger.schema_version,
        semantic_version=ledger.semantic_version,
        consumer_adapter_version=ledger.consumer_adapter_version,
        server_key=ledger.server_key,
        current_authority_consumer_decision_id=ledger.current_authority_consumer_decision_id,
        decisions=ledger.decisions,
        alert_records=tuple(sorted(retained, key=lambda item: item.record_id)),
        compacted_record_count=ledger.compacted_record_count + len(removed),
        compaction_digest=digest,
        last_compacted_at=now if removed else ledger.last_compacted_at,
        reason_codes=tuple(sorted(set(ledger.reason_codes) | {"consumer_state_reference_safe_compaction"})),
    )


def auto_compaction_reasons(
    *,
    policy: AutoCompactionPolicy,
    file_size: int,
    raw_sample_count: int,
    legacy_coverage_count: int,
    decision_history_count: int,
    consumer_alert_record_count: int,
) -> tuple[str, ...]:
    if not policy.enabled:
        return ()
    reasons = []
    if file_size > policy.file_size_threshold_bytes:
        reasons.append("file_size_threshold")
    if raw_sample_count > policy.raw_sample_threshold:
        reasons.append("raw_sample_threshold")
    if legacy_coverage_count > policy.legacy_coverage_threshold:
        reasons.append("legacy_coverage_threshold")
    if decision_history_count > policy.decision_history_threshold:
        reasons.append("decision_history_threshold")
    if consumer_alert_record_count > policy.consumer_alert_record_threshold:
        reasons.append("consumer_alert_record_threshold")
    return tuple(reasons)


def validate_consumer_state(
    value: Mapping[str, object] | None,
    *,
    server_key: str,
    regime_ids: set[str],
    physical_event_ids: set[str],
    authority_decision_ids: set[str],
) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        ledger = ledger_from_mapping(value, server_key=server_key)
    except Exception as exc:
        return (f"invalid_consumer_state:{exc}",)
    errors = []
    decision_ids = {item.decision_id for item in ledger.decisions}
    if (
        ledger.current_authority_consumer_decision_id is not None
        and ledger.current_authority_consumer_decision_id not in decision_ids
    ):
        errors.append("current_consumer_decision_reference_missing")
    record_ids = {item.record_id for item in ledger.alert_records}
    if len(record_ids) != len(ledger.alert_records):
        errors.append("duplicate_consumer_alert_record_id")
    key_lifecycle = set()
    superseded_record_ids = {
        item.supersedes_record_id
        for item in ledger.alert_records
        if item.supersedes_record_id is not None
    }
    active_key_ids = []
    for item in ledger.decisions:
        if item.authority_decision_id not in authority_decision_ids:
            errors.append(f"consumer_authority_decision_missing:{item.decision_id}")
        if item.selected_regime_id is not None and item.selected_regime_id not in regime_ids:
            errors.append(f"consumer_selected_regime_missing:{item.decision_id}")
    for item in ledger.alert_records:
        try:
            key = item.suppression_key()
        except Exception as exc:
            errors.append(f"malformed_consumer_suppression_key:{item.record_id}:{exc}")
            continue
        if item.key_id != suppression_key_id(key):
            errors.append(f"consumer_suppression_key_id_mismatch:{item.record_id}")
        if item.authority_consumer_decision_id not in decision_ids:
            errors.append(f"consumer_alert_decision_missing:{item.record_id}")
        if item.supersedes_record_id is not None and item.supersedes_record_id not in record_ids:
            errors.append(f"consumer_alert_supersedes_missing:{item.record_id}")
        if item.namespace is AuthoritySuppressionKind.SCHEDULED_WARNING:
            if item.regime_id not in regime_ids or item.expected_occurrence_at is None or item.physical_event_id is not None:
                errors.append(f"malformed_scheduled_warning_key:{item.record_id}")
        elif item.namespace is AuthoritySuppressionKind.GENERIC_RECOVERY:
            if item.regime_id is not None or item.expected_occurrence_at is not None or item.physical_event_id not in physical_event_ids:
                errors.append(f"malformed_generic_recovery_key:{item.record_id}")
        key_lifecycle.add((item.key_id, item.lifecycle.value, item.supersedes_record_id))
        if item.record_id not in superseded_record_ids:
            active_key_ids.append(item.key_id)
    if len(key_lifecycle) != len(ledger.alert_records):
        errors.append("duplicate_consumer_key_lifecycle_record")
    if len(active_key_ids) != len(set(active_key_ids)):
        errors.append("duplicate_active_consumer_suppression_key")
    return tuple(sorted(set(errors)))


def _validate_ledger_shape(ledger: ConsumerStateLedger, *, expected_server_key: str) -> None:
    if ledger.schema_version != CONSUMER_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported consumer state schema version")
    if ledger.semantic_version != CONSUMER_STATE_SEMANTIC_VERSION:
        raise ValueError("unsupported consumer state semantic version")
    if ledger.consumer_adapter_version != CONSUMER_STATE_ADAPTER_VERSION:
        raise ValueError("unsupported consumer adapter version")
    if ledger.server_key != expected_server_key:
        raise ValueError("consumer state server mismatch")
    decision_ids = [item.decision_id for item in ledger.decisions]
    record_ids = [item.record_id for item in ledger.alert_records]
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("duplicate consumer decision ID")
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("duplicate consumer alert record ID")


def _decision_from(value: object) -> ConsumerDecisionRecord:
    if not isinstance(value, Mapping):
        raise ValueError("consumer decision record must be a mapping")
    return ConsumerDecisionRecord(
        schema_version=int(value["schema_version"]),
        semantic_version=int(value["semantic_version"]),
        decision_id=str(value["decision_id"]),
        authority_decision_id=str(value["authority_decision_id"]),
        server_key=str(value["server_key"]),
        selected_regime_id=(str(value["selected_regime_id"]) if value.get("selected_regime_id") is not None else None),
        presentation_state=str(value["presentation_state"]),
        cycle_period_seconds=(int(value["cycle_period_seconds"]) if value.get("cycle_period_seconds") is not None else None),
        confidence_display_value=float(value["confidence_display_value"]),
        next_expected_restart_at=(float(value["next_expected_restart_at"]) if value.get("next_expected_restart_at") is not None else None),
        countdown_safe=bool(value["countdown_safe"]),
        prediction_suspended=bool(value["prediction_suspended"]),
        reason_codes=tuple(str(item) for item in value.get("reason_codes", ())),
    )


def _alert_from(value: object) -> ConsumerAlertRecord:
    if not isinstance(value, Mapping):
        raise ValueError("consumer alert record must be a mapping")
    return ConsumerAlertRecord(
        schema_version=int(value["schema_version"]),
        semantic_version=int(value["semantic_version"]),
        record_id=str(value["record_id"]),
        key_id=str(value["key_id"]),
        namespace=AuthoritySuppressionKind(value["namespace"]),
        server_key=str(value["server_key"]),
        authority_consumer_decision_id=str(value["authority_consumer_decision_id"]),
        regime_id=(str(value["regime_id"]) if value.get("regime_id") is not None else None),
        expected_occurrence_at=(int(value["expected_occurrence_at"]) if value.get("expected_occurrence_at") is not None else None),
        physical_event_id=(str(value["physical_event_id"]) if value.get("physical_event_id") is not None else None),
        created_at=float(value["created_at"]),
        lifecycle=AlertLifecycle(value["lifecycle"]),
        action_outcome=AlertActionOutcome(value["action_outcome"]),
        supersedes_record_id=(str(value["supersedes_record_id"]) if value.get("supersedes_record_id") is not None else None),
        compacted=bool(value.get("compacted", False)),
        reason_codes=tuple(str(item) for item in value.get("reason_codes", ())),
    )


def _primitive(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {key: _primitive(item) for key, item in asdict(value).items()}
    return value


def _stable_id(namespace: str, payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(f"{namespace}|{raw}".encode()).hexdigest()


def _finite_nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
