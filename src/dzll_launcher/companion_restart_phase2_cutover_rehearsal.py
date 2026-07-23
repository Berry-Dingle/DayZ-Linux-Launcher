from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .companion_restart_phase2_authority_consumers import (
    AuthorityConsumerPolicyInput,
    evaluate_authority_consumers,
)
from .companion_restart_phase2_consumer_state import (
    fired_suppression_keys,
    ledger_from_mapping,
)
from .companion_restart_phase2_schema4 import (
    deserialize_schema4_bytes,
    persisted_authority_decision,
)


@dataclass(frozen=True)
class CutoverReadinessEvidence:
    full_suite_passed: bool
    migration_deterministic: bool
    backup_verified: bool
    schema4_runtime_load_passed: bool
    no_quarantined_active_server: bool
    no_reference_errors: bool
    authority_reload_stable: bool
    consumer_reload_stable: bool
    fired_key_reload_stable: bool
    scheduled_warning_once_passed: bool
    generic_recovery_once_passed: bool
    transition_soak_passed: bool
    compaction_soak_passed: bool
    rollback_exact_passed: bool
    schema3_fallback_passed: bool
    no_duplicate_action_path: bool
    production_switches_false: bool


@dataclass(frozen=True)
class CutoverReadinessResult:
    ready: bool
    blockers: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CutoverRehearsalResult:
    source_path: str
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    prepared_path: str
    prepared_size: int
    prepared_sha256: str
    backup_path: str
    backup_size: int
    backup_sha256: str
    backup_matches_source: bool
    schema4_valid: bool
    server_key: str
    server_quarantined: bool
    authority_decision_id: str | None
    consumer_decision_id: str | None
    presentation_key: str | None
    selected_period_seconds: int | None
    warning_key_count: int
    recovery_key_count: int
    cutover_resolver_source: str
    rollback_expected_sha256: str
    required_switches: tuple[str, ...]
    blockers: tuple[str, ...]
    final_status: str
    active_modified: bool


def evaluate_cutover_readiness(
    evidence: CutoverReadinessEvidence,
) -> CutoverReadinessResult:
    blockers = tuple(
        field
        for field, value in asdict(evidence).items()
        if not value
    )
    return CutoverReadinessResult(
        ready=not blockers,
        blockers=blockers,
        reason_codes=(
            "all_cutover_readiness_criteria_passed"
            if not blockers
            else "cutover_blocked_until_all_criteria_pass"
        ,),
    )


def rehearse_schema4_cutover(
    *,
    source_path: str | Path,
    prepared_path: str | Path,
    backup_path: str | Path,
    server_key: str,
    now: float,
) -> CutoverRehearsalResult:
    """Inspect a future cutover without opening any write or apply path."""

    source = Path(source_path)
    prepared = Path(prepared_path)
    backup = Path(backup_path)
    source_before = source.read_bytes()
    source_stat = source.stat()
    prepared_raw = prepared.read_bytes()
    backup_raw = backup.read_bytes()
    loaded = deserialize_schema4_bytes(
        prepared_raw, quarantine_invalid_servers=True
    )
    record = loaded.state.get("servers", {}).get(server_key)
    quarantined = not isinstance(record, dict) or record.get("authority_status") == "quarantined"
    authority_id = None
    consumer_id = None
    presentation = None
    period = None
    warning_count = 0
    recovery_count = 0
    blockers = []
    if quarantined:
        blockers.append("server_authority_quarantined_or_missing")
    else:
        decision = persisted_authority_decision(record)
        authority_id = decision.decision_id
        ledger = ledger_from_mapping(record.get("consumer_state"), server_key=server_key)
        fired = fired_suppression_keys(ledger)
        consumer = evaluate_authority_consumers(
            AuthorityConsumerPolicyInput(
                authority_decision=decision,
                now=now,
                fired_keys=fired,
            )
        )
        consumer_id = consumer.decision_id
        presentation = consumer.presentation_state.value
        period = consumer.cycle_period_seconds
        warning_count = sum(item.kind.value == "scheduled_warning" for item in fired)
        recovery_count = sum(item.kind.value == "generic_recovery" for item in fired)
    if not loaded.report.root_valid:
        blockers.append("schema4_root_invalid")
    if loaded.report.quarantined_server_keys:
        blockers.append("schema4_has_quarantined_servers")
    source_after = source.read_bytes()
    active_modified = source_after != source_before or source.stat().st_mtime_ns != source_stat.st_mtime_ns
    if active_modified:
        blockers.append("source_modified_during_rehearsal")
    return CutoverRehearsalResult(
        source_path=str(source),
        source_size=len(source_before),
        source_mtime_ns=source_stat.st_mtime_ns,
        source_sha256=_sha(source_before),
        prepared_path=str(prepared),
        prepared_size=len(prepared_raw),
        prepared_sha256=_sha(prepared_raw),
        backup_path=str(backup),
        backup_size=len(backup_raw),
        backup_sha256=_sha(backup_raw),
        backup_matches_source=backup_raw == source_before,
        schema4_valid=loaded.report.root_valid,
        server_key=server_key,
        server_quarantined=quarantined,
        authority_decision_id=authority_id,
        consumer_decision_id=consumer_id,
        presentation_key=presentation,
        selected_period_seconds=period,
        warning_key_count=warning_count,
        recovery_key_count=recovery_count,
        cutover_resolver_source=("schema4" if not quarantined else "schema3_safe_fallback"),
        rollback_expected_sha256=_sha(backup_raw),
        required_switches=(
            "AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED",
            "SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED",
            "SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED",
        ),
        blockers=tuple(sorted(set(blockers))),
        final_status="ready" if not blockers else "not_ready",
        active_modified=active_modified,
    )


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only schema-4 cutover rehearsal")
    parser.add_argument("--source", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--now", required=True, type=float)
    args = parser.parse_args(argv)
    result = rehearse_schema4_cutover(
        source_path=args.source,
        prepared_path=args.prepared,
        backup_path=args.backup,
        server_key=args.server,
        now=args.now,
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0 if result.final_status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
