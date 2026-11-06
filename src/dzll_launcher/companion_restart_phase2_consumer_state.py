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

# Retention R5: bounded alert and decision bookkeeping.
#
# Scheduled-warning keys are (regime, expected occurrence).  A warning can only
# be due while its occurrence is still in the future (the consumer predicts the
# first occurrence strictly after "now" and the warning window ends before it),
# so a key whose occurrence is in the past can never become due again.  Such
# keys are folded into one server-wide high-water mark: every scheduled key with
# an occurrence at or before it is suppressed, for every regime.  Folding only
# past occurrences (at least ``SCHEDULED_WARNING_FOLD_MARGIN_SECONDS`` old)
# means the mark can never suppress a warning that has not been issued yet.
#
# Generic-recovery keys are per physical event and are offered only for the
# event that has just finalized (its ``finalized_at`` is the wall time of
# finalization).  Older keys are folded into a high-water mark over the
# events' ``finalized_at``; every event finalized later lies above it.
#
# Folding never rewrites a retained record, so a pending reservation keeps its
# record ID.  The folded keys stay suppressed through the marks, which are
# saved in the same ledger as the records they replace.
CONSUMER_RETAINED_SCHEDULED_WARNING_KEYS = 8
CONSUMER_MAX_SCHEDULED_WARNING_KEYS = 12
CONSUMER_RETAINED_GENERIC_RECOVERY_KEYS = 8
CONSUMER_MAX_GENERIC_RECOVERY_KEYS = 12
CONSUMER_MAX_RECORDS_PER_KEY = 3
CONSUMER_DECISION_RETENTION_LIMIT = 64
SCHEDULED_WARNING_FOLD_MARGIN_SECONDS = 3600
CONSUMER_LEDGER_BOUNDED_REASON = "consumer_alert_ledger_bounded"
SCHEDULED_SUPPRESSED_THROUGH_FIELD = "scheduled_warning_suppressed_through_occurrence_at"
GENERIC_SUPPRESSED_THROUGH_FIELD = "generic_recovery_suppressed_through_finalized_at"


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
    scheduled_warning_suppressed_through: int | None = None
    generic_recovery_suppressed_through: float | None = None


class ConsumerFiredKeys(frozenset):
    """Explicitly fired keys plus the folded scheduled-warning high-water mark.

    Membership (``in``) also answers True for any scheduled-warning key of this
    server whose occurrence is at or before the mark, so consumers that test
    ``key not in fired_keys`` keep suppressing folded warnings.
    """

    scheduled_warning_suppressed_through: int | None
    server_key: str | None

    def __new__(
        cls,
        keys: Iterable[AuthoritySuppressionKey] = (),
        *,
        scheduled_warning_suppressed_through: int | None = None,
        server_key: str | None = None,
    ):
        value = super().__new__(cls, keys)
        value.scheduled_warning_suppressed_through = scheduled_warning_suppressed_through
        value.server_key = server_key
        return value

    def __contains__(self, key: object) -> bool:
        if frozenset.__contains__(self, key):
            return True
        through = self.scheduled_warning_suppressed_through
        return bool(
            through is not None
            and isinstance(key, AuthoritySuppressionKey)
            and key.kind is AuthoritySuppressionKind.SCHEDULED_WARNING
            and (self.server_key is None or key.server_key == self.server_key)
            and key.expected_occurrence_at is not None
            and key.expected_occurrence_at <= through
        )


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
        scheduled_warning_suppressed_through=_optional_occurrence_mark(
            meta.get(SCHEDULED_SUPPRESSED_THROUGH_FIELD)
        ),
        generic_recovery_suppressed_through=_optional_time_mark(
            meta.get(GENERIC_SUPPRESSED_THROUGH_FIELD)
        ),
    )
    _validate_ledger_shape(ledger, expected_server_key=server_key)
    return ledger


def _optional_occurrence_mark(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("scheduled-warning suppression mark must be a non-negative integer")
    return value


def _optional_time_mark(value: object) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("generic-recovery suppression mark must be finite and non-negative")
    return float(value)


def ledger_to_mapping(ledger: ConsumerStateLedger) -> dict:
    meta: dict[str, object] = {
        "compacted_record_count": ledger.compacted_record_count,
        "compaction_digest": ledger.compaction_digest,
        "last_compacted_at": ledger.last_compacted_at,
    }
    # The marks are written only once set, so ledgers that never folded
    # anything keep their earlier canonical form.
    if ledger.scheduled_warning_suppressed_through is not None:
        meta[SCHEDULED_SUPPRESSED_THROUGH_FIELD] = ledger.scheduled_warning_suppressed_through
    if ledger.generic_recovery_suppressed_through is not None:
        meta[GENERIC_SUPPRESSED_THROUGH_FIELD] = ledger.generic_recovery_suppressed_through
    return {
        "schema_version": ledger.schema_version,
        "semantic_version": ledger.semantic_version,
        "consumer_adapter_version": ledger.consumer_adapter_version,
        "server_key": ledger.server_key,
        "current_authority_consumer_decision_id": ledger.current_authority_consumer_decision_id,
        "decisions": [_primitive(item) for item in sorted(ledger.decisions, key=lambda item: item.decision_id)],
        "alert_records": [_primitive(item) for item in sorted(ledger.alert_records, key=lambda item: item.record_id)],
        "compaction_metadata": meta,
        "reason_codes": sorted(set(ledger.reason_codes)),
    }


def with_decision(
    ledger: ConsumerStateLedger,
    decision: AuthorityConsumerDecision,
    *,
    retain: int = CONSUMER_DECISION_RETENTION_LIMIT,
) -> ConsumerStateLedger:
    record = decision_record(decision)
    by_id = {item.decision_id: item for item in ledger.decisions}
    by_id[record.decision_id] = record
    updated = replace(
        ledger,
        current_authority_consumer_decision_id=record.decision_id,
        decisions=tuple(sorted(by_id.values(), key=lambda item: item.decision_id)),
    )
    # The decision being replaced (the immediately preceding one) is kept.
    return trim_consumer_decisions(
        updated,
        retain=retain,
        protect=(ledger.current_authority_consumer_decision_id,),
    )


def alert_referenced_decision_ids(ledger: ConsumerStateLedger) -> frozenset[str]:
    return frozenset(item.authority_consumer_decision_id for item in ledger.alert_records)


def trim_consumer_decisions(
    ledger: ConsumerStateLedger,
    *,
    retain: int = CONSUMER_DECISION_RETENTION_LIMIT,
    protect: Iterable[str | None] = (),
    keep_only: Iterable[str] | None = None,
) -> ConsumerStateLedger:
    """Keep at most ``retain`` decisions without breaking a reference.

    The current decision, ``protect`` and every decision referenced by a
    retained alert record are never removed.  ``keep_only``, when given,
    additionally removes every unprotected decision outside it.  Removal order
    among the rest is the established deterministic decision-ID order.
    """

    protected = {
        ledger.current_authority_consumer_decision_id,
        *protect,
        *alert_referenced_decision_ids(ledger),
    }
    allowed = None if keep_only is None else set(keep_only)
    values = sorted(ledger.decisions, key=lambda item: item.decision_id)
    if allowed is not None:
        values = [
            item for item in values if item.decision_id in protected or item.decision_id in allowed
        ]
    if len(values) > retain:
        removable = [item for item in values if item.decision_id not in protected]
        remove = {item.decision_id for item in removable[: max(0, len(values) - retain)]}
        values = [item for item in values if item.decision_id not in remove]
    if len(values) == len(ledger.decisions):
        return ledger
    return replace(ledger, decisions=tuple(values))


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
    event_finalized_at: Mapping[str, float] | None = None,
    protect_key_ids: Iterable[str] = (),
    retained_scheduled_keys: int = CONSUMER_RETAINED_SCHEDULED_WARNING_KEYS,
    max_scheduled_keys: int = CONSUMER_MAX_SCHEDULED_WARNING_KEYS,
    retained_generic_keys: int = CONSUMER_RETAINED_GENERIC_RECOVERY_KEYS,
) -> ConsumerStateLedger:
    """Fold old alert lineages into the suppression high-water marks.

    Kept unchanged (no record is rewritten, so pending reservations keep their
    record IDs): every record of the newest ``retained_scheduled_keys``
    scheduled-warning keys by occurrence, of scheduled keys whose occurrence
    is not yet ``SCHEDULED_WARNING_FOLD_MARGIN_SECONDS`` in the past (at most
    ``max_scheduled_keys`` in all), of the newest ``retained_generic_keys``
    generic-recovery keys by event ``finalized_at``, and of ``protect_key_ids``.

    Every other key is folded: its records are removed and its occurrence (or
    its event's ``finalized_at``) raises the matching mark, so it stays
    suppressed.  A key with more than ``CONSUMER_MAX_RECORDS_PER_KEY`` records
    is folded too.  A generic key whose event is unknown (no longer retained)
    is removed without raising the mark; such an event can never be reserved
    again (the runtime refuses events outside the retained ring).  The marks
    never decrease.  Folding beyond ``max_scheduled_keys`` is the only case
    that can fold a future occurrence; it requires the wall clock to have
    moved backwards and errs towards suppression, never towards a repeat.
    """

    _finite_nonnegative("now", now)
    finalized = dict(event_finalized_at or {})
    protected = set(protect_key_ids)
    records_by_key: dict[str, list[ConsumerAlertRecord]] = {}
    for item in ledger.alert_records:
        records_by_key.setdefault(item.key_id, []).append(item)
    latest = latest_alert_records(ledger)
    scheduled = sorted(
        (item for item in latest if item.namespace is AuthoritySuppressionKind.SCHEDULED_WARNING),
        key=lambda item: (item.expected_occurrence_at or 0, item.key_id),
    )
    generic = sorted(
        (item for item in latest if item.namespace is AuthoritySuppressionKind.GENERIC_RECOVERY),
        key=lambda item: (finalized.get(item.physical_event_id or "", -1.0), item.key_id),
    )
    folded: set[str] = set()
    for item in latest:
        if (
            item.key_id not in protected
            and len(records_by_key.get(item.key_id, ())) > CONSUMER_MAX_RECORDS_PER_KEY
        ):
            folded.add(item.key_id)
    newest_scheduled = (
        {item.key_id for item in scheduled[-retained_scheduled_keys:]}
        if retained_scheduled_keys > 0
        else set()
    )
    horizon = now - SCHEDULED_WARNING_FOLD_MARGIN_SECONDS
    for item in scheduled:
        if item.key_id in protected or item.key_id in newest_scheduled:
            continue
        if (item.expected_occurrence_at or 0) <= horizon:
            folded.add(item.key_id)
    remaining = [item for item in scheduled if item.key_id not in folded]
    excess = len(remaining) - max_scheduled_keys
    for item in remaining:
        if excess <= 0:
            break
        if item.key_id in protected:
            continue
        folded.add(item.key_id)
        excess -= 1
    newest_generic = (
        {item.key_id for item in generic[-retained_generic_keys:]}
        if retained_generic_keys > 0
        else set()
    )
    for item in generic:
        if item.key_id in protected or item.key_id in newest_generic:
            continue
        folded.add(item.key_id)
    for item in generic:
        if item.physical_event_id not in finalized and item.key_id not in protected:
            folded.add(item.key_id)
    # A retained record must never supersede a removed one.
    changed = True
    while changed:
        changed = False
        removed_ids = {
            record.record_id
            for key_id in folded
            for record in records_by_key.get(key_id, ())
        }
        for item in ledger.alert_records:
            if (
                item.key_id not in folded
                and item.supersedes_record_id is not None
                and item.supersedes_record_id in removed_ids
            ):
                folded.add(item.key_id)
                changed = True
    scheduled_mark = ledger.scheduled_warning_suppressed_through
    generic_mark = ledger.generic_recovery_suppressed_through
    for item in latest:
        if item.key_id not in folded:
            continue
        if item.namespace is AuthoritySuppressionKind.SCHEDULED_WARNING:
            occurrence = item.expected_occurrence_at
            if occurrence is not None:
                scheduled_mark = (
                    occurrence if scheduled_mark is None else max(scheduled_mark, occurrence)
                )
        else:
            at = finalized.get(item.physical_event_id or "")
            if at is not None:
                generic_mark = float(at) if generic_mark is None else max(generic_mark, float(at))
    retained = tuple(
        sorted(
            (item for item in ledger.alert_records if item.key_id not in folded),
            key=lambda item: item.record_id,
        )
    )
    removed = sorted(
        item.record_id for item in ledger.alert_records if item.key_id in folded
    )
    digest = (
        _stable_id("schema4-consumer-compaction", [ledger.compaction_digest, removed])
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
        alert_records=retained,
        compacted_record_count=ledger.compacted_record_count + len(removed),
        compaction_digest=digest,
        last_compacted_at=now if removed else ledger.last_compacted_at,
        reason_codes=tuple(
            sorted(
                set(ledger.reason_codes)
                | {"consumer_state_reference_safe_compaction", CONSUMER_LEDGER_BOUNDED_REASON}
            )
        ),
        scheduled_warning_suppressed_through=scheduled_mark,
        generic_recovery_suppressed_through=generic_mark,
    )


def bound_consumer_ledger(
    ledger: ConsumerStateLedger,
    *,
    now: float,
    event_finalized_at: Mapping[str, float] | None = None,
    protect_key_ids: Iterable[str] = (),
    protect_decision_ids: Iterable[str | None] = (),
    retain_decisions: int = CONSUMER_DECISION_RETENTION_LIMIT,
) -> ConsumerStateLedger:
    """Fold old alert lineages, then trim the decision history to its bound."""

    compacted = compact_consumer_ledger(
        ledger,
        now=now,
        event_finalized_at=event_finalized_at,
        protect_key_ids=protect_key_ids,
    )
    return trim_consumer_decisions(
        compacted, retain=retain_decisions, protect=protect_decision_ids
    )


def consumer_suppression_set(
    ledger: ConsumerStateLedger,
    *,
    event_finalized_at: Mapping[str, float] | None = None,
) -> ConsumerFiredKeys:
    """Every key the ledger suppresses, including folded ones.

    Generic-recovery keys folded into the mark are listed explicitly for the
    retained events at or before it; scheduled-warning keys at or before their
    mark are answered by ``ConsumerFiredKeys`` membership.
    """

    keys = set(fired_suppression_keys(ledger))
    mark = ledger.generic_recovery_suppressed_through
    if mark is not None:
        for event_id, at in (event_finalized_at or {}).items():
            if at <= mark:
                keys.add(
                    AuthoritySuppressionKey(
                        kind=AuthoritySuppressionKind.GENERIC_RECOVERY,
                        server_key=ledger.server_key,
                        event_id=event_id,
                    )
                )
    return ConsumerFiredKeys(
        keys,
        scheduled_warning_suppressed_through=ledger.scheduled_warning_suppressed_through,
        server_key=ledger.server_key,
    )


def key_suppressed(
    ledger: ConsumerStateLedger,
    key: AuthoritySuppressionKey,
    *,
    event_finalized_at: Mapping[str, float] | None = None,
) -> bool:
    if key in fired_suppression_keys(ledger):
        return True
    if key.kind is AuthoritySuppressionKind.SCHEDULED_WARNING:
        mark = ledger.scheduled_warning_suppressed_through
        return bool(
            mark is not None
            and key.expected_occurrence_at is not None
            and key.expected_occurrence_at <= mark
        )
    mark = ledger.generic_recovery_suppressed_through
    at = (event_finalized_at or {}).get(key.event_id or "")
    return bool(mark is not None and at is not None and at <= mark)


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
    if CONSUMER_LEDGER_BOUNDED_REASON in ledger.reason_codes:
        errors.extend(_bounded_ledger_errors(ledger))
    return tuple(sorted(set(errors)))


def _bounded_ledger_errors(ledger: ConsumerStateLedger) -> list[str]:
    """Retention R5 bounds of a ledger that has been compacted at least once."""

    errors = []
    records_by_key: dict[str, int] = {}
    kinds: dict[str, AuthoritySuppressionKind] = {}
    for item in ledger.alert_records:
        records_by_key[item.key_id] = records_by_key.get(item.key_id, 0) + 1
        kinds[item.key_id] = item.namespace
    scheduled = sum(
        1 for kind in kinds.values() if kind is AuthoritySuppressionKind.SCHEDULED_WARNING
    )
    generic = len(kinds) - scheduled
    if scheduled > CONSUMER_MAX_SCHEDULED_WARNING_KEYS:
        errors.append(f"consumer_scheduled_warning_retention_exceeded:{scheduled}")
    if generic > CONSUMER_MAX_GENERIC_RECOVERY_KEYS:
        errors.append(f"consumer_generic_recovery_retention_exceeded:{generic}")
    if any(count > CONSUMER_MAX_RECORDS_PER_KEY for count in records_by_key.values()):
        errors.append("consumer_alert_lineage_retention_exceeded")
    if len(ledger.decisions) > CONSUMER_DECISION_RETENTION_LIMIT:
        errors.append(f"consumer_decision_retention_exceeded:{len(ledger.decisions)}")
    return errors


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
