"""Option 2: Export and Import carry quarantined servers as inert wrappers."""

from __future__ import annotations

import copy
import json

import pytest

from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4

from test_companion_learning_deletion import A, B, BASE, C, _open, _store


def _exported(tmp_path):
    active, legacy, _lkg = _store(tmp_path, quarantine=C)
    backend = live4.AuthoritativeSchema4Runtime.open(active, enabled=True)
    try:
        snapshot = backend.export_snapshot()
    finally:
        backend.close(flush=False)
    return active, legacy, snapshot


def _mutated(raw, mutate):
    state = json.loads(raw)
    mutate(state)
    return schema4.canonical_json_bytes(state)


def test_export_with_a_quarantined_server_succeeds_and_reports_it(tmp_path):
    _active, _legacy, snapshot = _exported(tmp_path)
    assert snapshot.server_count == 3
    assert snapshot.quarantined_server_keys == (C,)
    validated = transfer.validate_external_learning_bytes(
        snapshot.canonical_bytes, source_filename="export.json"
    )
    assert validated.server_count == 3
    assert validated.quarantined_server_count == 1
    assert validated.canonical_bytes == snapshot.canonical_bytes


def test_export_file_round_trips_through_import_with_quarantine_preserved(tmp_path):
    active, legacy, snapshot = _exported(tmp_path)
    export_path = tmp_path / "export.json"
    transfer.write_export_file(export_path, snapshot.canonical_bytes)
    validated = transfer.validate_external_learning_file(export_path)
    pending = transfer.stage_pending_import(validated, config_dir=tmp_path)
    assert pending.quarantined_server_count == 1

    result = transfer.apply_pending_import_at_startup(config_dir=tmp_path, live_path=active)
    assert result.status == "applied"
    value = _open(active, legacy, BASE + 40_000)
    try:
        backend = value._authoritative_schema4_backend
        assert backend.snapshot().quarantined_server_keys == (C,)
        assert backend.server_write_block(C)[0] == "server_quarantined"
        assert backend.server_write_block(A) is None
        assert backend.server_write_block(B) is None
    finally:
        value.shutdown(wall_at=BASE + 40_100, monotonic_at=40_100)


def test_quarantined_schema3_entry_is_optional(tmp_path):
    _active, _legacy, snapshot = _exported(tmp_path)

    def drop_entry(state):
        state["schema3_snapshot"]["servers"].pop(C, None)

    validated = transfer.validate_external_learning_bytes(
        _mutated(snapshot.canonical_bytes, drop_entry)
    )
    assert validated.quarantined_server_count == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda w: w["quarantined_payload"].__setitem__("server_key", "x.example:1"), "SHA-256"),
        (lambda w: w.__setitem__("surprise", 1), "unknown fields"),
        (lambda w: w.pop("quarantined_payload_sha256"), "missing fields"),
        (lambda w: w.__setitem__("quarantined_payload_summary", {}), "exactly one"),
        (lambda w: w.__setitem__("validation_error_count", 0), "inconsistent"),
        (lambda w: w.__setitem__("schema_version", 3), "schema_version"),
        (
            lambda w: w.__setitem__(
                "validation_errors", [{"code": "x", "detail": "y"}] * 65
            ),
            "bounded list",
        ),
        (
            lambda w: w["validation_errors"].append({"code": "x", "detail": "y" * 600}),
            "bounded code",
        ),
    ],
)
def test_malformed_or_tampered_wrappers_are_rejected(tmp_path, mutate, message):
    _active, _legacy, snapshot = _exported(tmp_path)

    def change(state):
        mutate(state["servers"][C])

    with pytest.raises(transfer.ExternalLearningValidationError, match=message):
        transfer.validate_external_learning_bytes(_mutated(snapshot.canonical_bytes, change))


def test_a_damaged_unwrapped_record_is_still_rejected(tmp_path):
    _active, _legacy, snapshot = _exported(tmp_path)

    def unwrap(state):
        state["servers"][C] = copy.deepcopy(state["servers"][C]["quarantined_payload"])
        state["servers"][C]["server_key"] = C
        # A genuinely broken record that is not a quarantine wrapper.
        state["servers"][C]["physical_events"] = "not a list"

    with pytest.raises(transfer.ExternalLearningValidationError, match="physical_events"):
        transfer.validate_external_learning_bytes(_mutated(snapshot.canonical_bytes, unwrap))


def test_quarantined_note_wording():
    assert transfer.format_quarantined_note(0) == ""
    one = transfer.format_quarantined_note(1)
    assert one.startswith("1 quarantined record is included.")
    assert "never used for learning" in one and "deleted from Server Companion" in one
    assert transfer.format_quarantined_note(2).startswith("2 quarantined records are included.")
