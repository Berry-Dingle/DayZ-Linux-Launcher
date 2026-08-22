import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_storage as storage
from dzll_launcher import companion_learning_transfer as transfer
from dzll_launcher.companion_learning_transfer import (
    ExternalLearningValidationError,
    MAX_IMPORT_BYTES,
    PendingImportError,
    apply_pending_import_at_startup,
    cancel_pending_import,
    default_export_filename,
    pending_import_paths,
    read_pending_import,
    stage_pending_import,
    validate_external_learning_bytes,
    write_export_file,
)


@pytest.mark.parametrize(
    ("count", "records"),
    [(1, "1 learned server record"), (6, "6 learned server records")],
)
def test_success_notice_describes_database_replacement(count, records):
    result = transfer.StartupImportResult("applied", server_count=count)
    assert result.notice_title == "Server Companion learning database replaced"
    assert "previous Server Companion learning database was replaced successfully" in result.notice_body
    assert f"new database contains {records}" in result.notice_body
    assert "permanent backup of the previous database was retained" in result.notice_body
    assert "(s)" not in result.notice_body


def valid_state(*, marker="original"):
    phase3 = storage.new_phase2_state(
        now=1_900_000_000,
        generation_id="00000000-0000-4000-8000-000000000099",
    )
    raw = schema4.canonical_json_bytes(phase3)
    state = schema4.migrate_schema3_state(
        json.loads(raw), source_bytes=raw, source_mtime_ns=1
    )
    state["reason_codes"] = [marker]
    return state


def valid_bytes(*, marker="original"):
    return schema4.serialize_schema4_state(valid_state(marker=marker))


def mutate(mutator):
    value = valid_state()
    mutator(value)
    return schema4.canonical_json_bytes(value)


def test_valid_schema4_and_valid_empty_dataset_round_trip():
    result = validate_external_learning_bytes(valid_bytes(), source_filename="input.json")
    assert result.schema_version == 4
    assert result.server_count == 0
    assert result.source_filename == "input.json"
    assert result.canonical_bytes == valid_bytes()


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"", "empty"),
        (b"\xff", "UTF-8"),
        (b"{", "Invalid JSON"),
        (b"{} {}", "Invalid JSON"),
        (b'{"schema_version": 4, "schema_version": 4}', "duplicate"),
        (b'{"schema_version": NaN}', "non-finite"),
        (b"[]", "top level"),
    ],
)
def test_rejects_invalid_json_envelopes(raw, message):
    with pytest.raises(ExternalLearningValidationError, match=message):
        validate_external_learning_bytes(raw)


def test_rejects_oversized_input():
    with pytest.raises(ExternalLearningValidationError, match="8 MiB"):
        validate_external_learning_bytes(b" " * (MAX_IMPORT_BYTES + 1))


@pytest.mark.parametrize("version", [3, 5])
def test_rejects_old_and_future_schema(version):
    with pytest.raises(ExternalLearningValidationError, match="schema 4"):
        validate_external_learning_bytes(
            mutate(lambda state: state.__setitem__("schema_version", version))
        )


def test_rejects_missing_unknown_and_invalid_manifest_fields():
    with pytest.raises(ExternalLearningValidationError, match="Missing required"):
        validate_external_learning_bytes(mutate(lambda state: state.pop("version_manifest")))
    with pytest.raises(ExternalLearningValidationError, match="Unknown root"):
        validate_external_learning_bytes(mutate(lambda state: state.__setitem__("surprise", 1)))
    with pytest.raises(ExternalLearningValidationError, match="semantic"):
        validate_external_learning_bytes(
            mutate(lambda state: state["version_manifest"].__setitem__("authority_semantics_version", 99))
        )


def test_rejects_boolean_numeric_and_invalid_timestamp_order():
    with pytest.raises(ExternalLearningValidationError, match="created_at"):
        validate_external_learning_bytes(mutate(lambda state: state.__setitem__("created_at", True)))
    with pytest.raises(ExternalLearningValidationError, match="updated_at"):
        validate_external_learning_bytes(mutate(lambda state: state.__setitem__("updated_at", -1)))


@pytest.mark.parametrize("key", ["missing-port", ":2302", "host:0", "host:70000"])
def test_rejects_invalid_server_identity(key):
    def change(state):
        state["servers"] = {key: {"server_key": key}}
    with pytest.raises(ExternalLearningValidationError, match="Invalid server identity"):
        validate_external_learning_bytes(mutate(change))


def test_rejects_key_mismatch_quarantine_and_broken_record():
    for record, message in (
        ({"server_key": "other:2302"}, "mismatch"),
        ({"server_key": "host:2302", "authority_status": "quarantined"}, "quarantined"),
        ({"server_key": "host:2302"}, "physical_events"),
    ):
        with pytest.raises(ExternalLearningValidationError, match=message):
            validate_external_learning_bytes(
                mutate(lambda state, record=record: state["servers"].__setitem__("host:2302", record))
            )


def test_stage_is_private_verified_and_independent_of_source(tmp_path):
    validated = validate_external_learning_bytes(valid_bytes(marker="imported"), source_filename="source.json")
    staged = stage_pending_import(validated, config_dir=tmp_path)
    assert staged.payload_path.read_bytes() == validated.canonical_bytes
    assert staged.payload_path.stat().st_mode & 0o077 == 0
    assert read_pending_import(tmp_path, strict=True).sha256 == validated.sha256


def test_selected_source_can_change_after_validation_without_affecting_stage(tmp_path):
    source = tmp_path / "selected.json"
    source.write_bytes(valid_bytes(marker="selected"))
    validated = transfer.validate_external_learning_file(source)
    source.write_bytes(b"changed after validation")
    staged = stage_pending_import(validated, config_dir=tmp_path / "config")
    assert staged.payload_path.read_bytes() == validated.canonical_bytes


def test_existing_pending_requires_confirmation_and_cancel_leaves_live(tmp_path):
    first = validate_external_learning_bytes(valid_bytes(marker="first"), source_filename="one.json")
    second = validate_external_learning_bytes(valid_bytes(marker="second"), source_filename="two.json")
    stage_pending_import(first, config_dir=tmp_path)
    with pytest.raises(PendingImportError, match="already exists"):
        stage_pending_import(second, config_dir=tmp_path)
    live = tmp_path / "live.json"
    live.write_bytes(valid_bytes())
    before = live.read_bytes()
    assert cancel_pending_import(tmp_path)
    assert live.read_bytes() == before
    assert read_pending_import(tmp_path, strict=False) is None


def test_metadata_sha_mismatch_is_rejected(tmp_path):
    validated = validate_external_learning_bytes(valid_bytes(), source_filename="source.json")
    stage_pending_import(validated, config_dir=tmp_path)
    payload, _meta = pending_import_paths(tmp_path)
    payload.write_bytes(valid_bytes(marker="changed"))
    with pytest.raises(PendingImportError, match="SHA-256"):
        read_pending_import(tmp_path)


def test_failed_replacement_staging_restores_prior_valid_pair(tmp_path, monkeypatch):
    first = validate_external_learning_bytes(valid_bytes(marker="first"), source_filename="one.json")
    second = validate_external_learning_bytes(valid_bytes(marker="second"), source_filename="two.json")
    stage_pending_import(first, config_dir=tmp_path)
    payload, meta = pending_import_paths(tmp_path)
    prior = (payload.read_bytes(), meta.read_bytes())
    real_replace = transfer.os.replace
    failed = {"done": False}

    def fail_metadata_once(source, destination):
        if Path(destination) == meta and not failed["done"]:
            failed["done"] = True
            raise OSError("injected metadata install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(transfer.os, "replace", fail_metadata_once)
    with pytest.raises(OSError, match="injected"):
        stage_pending_import(second, config_dir=tmp_path, replace_existing=True)
    assert (payload.read_bytes(), meta.read_bytes()) == prior
    assert read_pending_import(tmp_path).source_filename == "one.json"


def test_startup_applies_before_runtime_with_exact_permanent_backup(tmp_path):
    live = tmp_path / "companion_restart_learning_phase2.json"
    old = valid_bytes(marker="old")
    new = valid_bytes(marker="new")
    live.write_bytes(old)
    validated = validate_external_learning_bytes(new, source_filename="new.json")
    stage_pending_import(validated, config_dir=tmp_path)
    result = apply_pending_import_at_startup(
        config_dir=tmp_path,
        live_path=live,
        now=datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc),
    )
    assert result.status == "applied"
    assert live.read_bytes() == new
    assert result.backup_path.read_bytes() == old
    assert result.backup_path.name.endswith("20260803-163000.json")
    assert not any(path.exists() for path in pending_import_paths(tmp_path))


def test_backup_collision_suffix_and_first_import(tmp_path):
    live = tmp_path / "companion_restart_learning_phase2.json"
    live.write_bytes(valid_bytes(marker="old"))
    stamp = datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc)
    collision = tmp_path / "companion_restart_learning_phase2.import-backup-20260803-163000.json"
    collision.write_bytes(b"keep")
    stage_pending_import(
        validate_external_learning_bytes(valid_bytes(marker="new")), config_dir=tmp_path
    )
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live, now=stamp)
    assert result.backup_path.name.endswith("-01.json")
    assert collision.read_bytes() == b"keep"

    other = tmp_path / "first" / "companion_restart_learning_phase2.json"
    other.parent.mkdir()
    stage_pending_import(
        validate_external_learning_bytes(valid_bytes(marker="first")), config_dir=other.parent
    )
    first = apply_pending_import_at_startup(config_dir=other.parent, live_path=other, now=stamp)
    assert first.status == "applied" and first.backup_path is None and other.exists()


def test_pending_schema4_replaces_valid_schema3_with_exact_backup(tmp_path):
    live = tmp_path / "companion_restart_learning_phase2.json"
    schema3 = storage.new_phase2_state(
        now=1_800_000_000,
        generation_id="11111111-1111-4111-8111-111111111111",
    )
    original = schema4.canonical_json_bytes(schema3)
    imported = valid_bytes(marker="pending-over-schema3")
    live.write_bytes(original)
    stage_pending_import(
        validate_external_learning_bytes(imported, source_filename="replacement.json"),
        config_dir=tmp_path,
    )
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "applied" and result.safe_to_initialize
    assert result.backup_path.read_bytes() == original
    assert live.read_bytes() == imported
    assert not live.with_name(live.name + ".last-known-good").exists()
    assert not any(path.exists() for path in pending_import_paths(tmp_path))


def test_pending_schema4_failure_restores_schema3_bytes(tmp_path, monkeypatch):
    live = tmp_path / "companion_restart_learning_phase2.json"
    schema3 = storage.new_phase2_state(
        now=1_800_000_000,
        generation_id="22222222-2222-4222-8222-222222222222",
    )
    original = schema4.canonical_json_bytes(schema3)
    imported = valid_bytes(marker="pending-failure-over-schema3")
    live.write_bytes(original)
    stage_pending_import(
        validate_external_learning_bytes(imported, source_filename="replacement.json"),
        config_dir=tmp_path,
    )
    real_install = transfer._install_verified
    calls = {"count": 0}

    def fail_after_import(path, payload):
        calls["count"] += 1
        real_install(path, payload)
        if calls["count"] == 1:
            raise OSError("injected schema3 replacement verification failure")

    monkeypatch.setattr(transfer, "_install_verified", fail_after_import)
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "rolled_back" and result.safe_to_initialize
    assert live.read_bytes() == original
    assert result.backup_path.read_bytes() == original
    assert all(path.exists() for path in pending_import_paths(tmp_path))


def test_partial_or_corrupt_pending_never_changes_live(tmp_path):
    live = tmp_path / "companion_restart_learning_phase2.json"
    live.write_bytes(valid_bytes())
    before = live.read_bytes()
    payload, _meta = pending_import_paths(tmp_path)
    payload.write_bytes(b"bad")
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "pending_invalid"
    assert live.read_bytes() == before and payload.exists()


def test_corrupt_pending_metadata_has_distinct_diagnostic(tmp_path):
    payload, metadata = pending_import_paths(tmp_path)
    payload.write_bytes(valid_bytes())
    metadata.write_bytes(b"not-json")
    result = apply_pending_import_at_startup(
        config_dir=tmp_path,
        live_path=tmp_path / "missing-live.json",
    )
    assert result.status == "pending_invalid"
    assert not result.safe_to_initialize
    assert "metadata is invalid" in result.error
    assert payload.exists() and metadata.exists()


def test_backup_failure_leaves_live_and_pending_untouched(tmp_path, monkeypatch):
    live = tmp_path / "companion_restart_learning_phase2.json"
    old = valid_bytes(marker="old")
    live.write_bytes(old)
    stage_pending_import(
        validate_external_learning_bytes(valid_bytes(marker="new")), config_dir=tmp_path
    )
    monkeypatch.setattr(transfer, "_write_new_verified", lambda *_a, **_k: (_ for _ in ()).throw(OSError("backup failed")))
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "apply_failed"
    assert live.read_bytes() == old
    assert all(path.exists() for path in pending_import_paths(tmp_path))


def test_post_replace_failure_rolls_back_and_retains_pending(tmp_path, monkeypatch):
    live = tmp_path / "companion_restart_learning_phase2.json"
    old = valid_bytes(marker="old")
    new = valid_bytes(marker="new")
    live.write_bytes(old)
    stage_pending_import(validate_external_learning_bytes(new), config_dir=tmp_path)
    real_install = transfer._install_verified
    calls = {"count": 0}

    def fail_after_first_install(path, payload):
        calls["count"] += 1
        real_install(path, payload)
        if calls["count"] == 1:
            raise OSError("verification failed after replace")

    monkeypatch.setattr(transfer, "_install_verified", fail_after_first_install)
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "rolled_back"
    assert live.read_bytes() == old
    assert result.backup_path.read_bytes() == old
    assert all(path.exists() for path in pending_import_paths(tmp_path))


def test_rollback_failure_marks_learning_unsafe(tmp_path, monkeypatch):
    live = tmp_path / "companion_restart_learning_phase2.json"
    old = valid_bytes(marker="old")
    new = valid_bytes(marker="new")
    live.write_bytes(old)
    stage_pending_import(validate_external_learning_bytes(new), config_dir=tmp_path)

    def always_fail_after_write(path, payload):
        transfer._atomic_write_verified(path, payload)
        raise OSError("injected verification failure")

    monkeypatch.setattr(transfer, "_install_verified", always_fail_after_write)
    result = apply_pending_import_at_startup(config_dir=tmp_path, live_path=live)
    assert result.status == "rollback_failed"
    assert not result.safe_to_initialize
    assert all(path.exists() for path in pending_import_paths(tmp_path))


def test_export_filename_and_atomic_export(tmp_path):
    when = datetime(2026, 8, 3, 12, 34, 56, tzinfo=timezone.utc)
    assert default_export_filename(when) == "dzll-server-companion-learning-schema4-20260803-123456.json"
    destination = tmp_path / "export.json"
    write_export_file(destination, valid_bytes())
    assert validate_external_learning_bytes(destination.read_bytes()).server_count == 0


def test_atomic_export_failure_preserves_existing_destination(tmp_path, monkeypatch):
    destination = tmp_path / "export.json"
    destination.write_bytes(b"prior")
    monkeypatch.setattr(transfer.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        write_export_file(destination, valid_bytes())
    assert destination.read_bytes() == b"prior"
