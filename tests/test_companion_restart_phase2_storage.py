import json
import os
from pathlib import Path
import uuid

import pytest

from dzll_launcher import companion_restart_phase2_storage as phase2
from dzll_launcher.config import COMPANION_RESTART_LEARNING_PHASE2_PATH


GENERATION_ID = "00000000-0000-4000-8000-000000000001"
NOW = 1_789_000_000


def paths(tmp_path):
    return phase2.phase2_paths(tmp_path)


def fresh(**overrides):
    values = {"now": NOW, "generation_id": GENERATION_ID}
    values.update(overrides)
    return phase2.new_phase2_state(**values)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def temp_files(directory):
    return sorted(directory.glob(".*.tmp"))


def test_atomic_json_creates_parent_and_new_destination(tmp_path):
    destination = tmp_path / "nested" / "state.json"
    phase2.atomic_write_json(destination, {"value": 1})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"value": 1}
    assert temp_files(destination.parent) == []


def test_atomic_json_replaces_existing_destination_deterministically(tmp_path):
    destination = tmp_path / "state.json"
    destination.write_text("old", encoding="utf-8")

    phase2.atomic_write_json(destination, {"z": 1, "a": [2, 3]})

    assert destination.read_bytes() == b'{\n  "a": [\n    2,\n    3\n  ],\n  "z": 1\n}\n'


def test_atomic_json_preserves_unicode(tmp_path):
    destination = tmp_path / "state.json"
    phase2.atomic_write_json(destination, {"server": "Café 日本"})

    assert "Café 日本" in destination.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad_value", [{"nan": float("nan")}, {"bad": object()}])
def test_atomic_json_serialization_failure_preserves_original(tmp_path, bad_value):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")

    with pytest.raises(phase2.AtomicWriteError) as raised:
        phase2.atomic_write_json(destination, bad_value)

    assert raised.value.stage == "serialization"
    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_temporary_open_failure_preserves_original(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")

    def fail_fdopen(*_args, **_kwargs):
        raise PermissionError("temporary file denied")

    monkeypatch.setattr(phase2.os, "fdopen", fail_fdopen)
    with pytest.raises(phase2.AtomicWriteError) as raised:
        phase2.atomic_write_json(destination, {"value": 2})

    assert raised.value.stage == "temporary_write"
    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_temporary_write_failure_cleans_file(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")
    real_fdopen = phase2.os.fdopen

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def write(self, _payload):
            raise OSError("write failed")

        def flush(self):
            self.handle.flush()

        def fileno(self):
            return self.handle.fileno()

    monkeypatch.setattr(
        phase2.os,
        "fdopen",
        lambda fd, mode: FailingWriter(real_fdopen(fd, mode)),
    )
    with pytest.raises(phase2.AtomicWriteError):
        phase2.atomic_write_json(destination, {"value": 2})

    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_flush_failure_preserves_original_and_cleans_file(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")
    real_fdopen = phase2.os.fdopen

    class FailingFlusher:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def write(self, payload):
            return self.handle.write(payload)

        def flush(self):
            raise OSError("flush failed")

        def fileno(self):
            return self.handle.fileno()

    monkeypatch.setattr(
        phase2.os,
        "fdopen",
        lambda fd, mode: FailingFlusher(real_fdopen(fd, mode)),
    )
    with pytest.raises(phase2.AtomicWriteError):
        phase2.atomic_write_json(destination, {"value": 2})

    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_file_fsync_failure_preserves_original(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")
    monkeypatch.setattr(phase2.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))

    with pytest.raises(phase2.AtomicWriteError):
        phase2.atomic_write_json(destination, {"value": 2})

    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_replace_failure_preserves_original(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_bytes(b"original")
    monkeypatch.setattr(phase2.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))

    with pytest.raises(phase2.AtomicWriteError) as raised:
        phase2.atomic_write_json(destination, {"value": 2})

    assert raised.value.stage == "replacement"
    assert destination.read_bytes() == b"original"
    assert temp_files(tmp_path) == []


def test_atomic_json_directory_fsync_runs_when_supported(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    real_fsync = phase2.os.fsync
    calls = []

    def track_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(phase2.os, "fsync", track_fsync)
    phase2.atomic_write_json(destination, {"value": 1})

    assert len(calls) >= 2


def test_atomic_json_unsupported_directory_fsync_is_best_effort(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    real_open = phase2.os.open

    def maybe_fail(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError("directory fsync unsupported")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(phase2.os, "open", maybe_fail)
    phase2.atomic_write_json(destination, {"value": 1})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"value": 1}


def test_atomic_json_uses_unique_temporary_names(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    real_mkstemp = phase2.tempfile.mkstemp
    names = []

    def track_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        names.append(name)
        return fd, name

    monkeypatch.setattr(phase2.tempfile, "mkstemp", track_mkstemp)
    phase2.atomic_write_json(destination, {"value": 1})
    phase2.atomic_write_json(destination, {"value": 2})

    assert len(names) == 2
    assert names[0] != names[1]
    assert all(Path(name).parent == tmp_path for name in names)
    assert temp_files(tmp_path) == []


def test_fresh_phase2_state_has_minimum_schema_and_null_reset_metadata():
    state = fresh()

    assert state == {
        "schema_version": 3,
        "scoring_algorithm_version": 1,
        "migration_reset_version": "phase2-v032-1",
        "state_generation_id": GENERATION_ID,
        "created_at": NOW,
        "updated_at": NOW,
        "reset_from_checksum": None,
        "reset_backup_path": None,
        "reset_completed_at": None,
        "servers": {},
    }


def test_dormant_config_path_uses_chosen_phase2_filename():
    assert COMPANION_RESTART_LEARNING_PHASE2_PATH.endswith(
        "/.config/dzll/companion_restart_learning_phase2.json"
    )


def test_fresh_phase2_states_have_unique_generation_ids():
    first = phase2.new_phase2_state(now=NOW)
    second = phase2.new_phase2_state(now=NOW)

    assert uuid.UUID(first["state_generation_id"])
    assert first["state_generation_id"] != second["state_generation_id"]


def test_phase2_normalization_preserves_unknown_fields_and_copies_containers():
    original = fresh()
    original["future"] = {"nested": [1, 2]}
    original["servers"] = {1: {"future_server": True}}

    normalized = phase2.normalize_phase2_state(original)
    normalized["future"]["nested"].append(3)
    normalized["servers"]["1"]["future_server"] = False

    assert original["future"]["nested"] == [1, 2]
    assert original["servers"][1]["future_server"] is True
    assert normalized["servers"].keys() == {"1"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object()])
def test_phase2_normalization_rejects_non_json_unknown_fields(bad):
    state = fresh()
    state["future"] = bad

    with pytest.raises(phase2.InvalidPhase2StateError):
        phase2.normalize_phase2_state(state)


@pytest.mark.parametrize("value", [None, [], "legacy", 1])
def test_phase2_normalization_rejects_non_dictionary_top_level(value):
    with pytest.raises(phase2.InvalidPhase2StateError):
        phase2.normalize_phase2_state(value)


@pytest.mark.parametrize("servers", [None, [], "bad", 1, True])
def test_phase2_normalization_replaces_malformed_servers(servers):
    state = fresh()
    state["servers"] = servers

    assert phase2.normalize_phase2_state(state)["servers"] == {}


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 2},
        {"schema_version": 4},
        {"schema_version": "3"},
        {"schema_version": 3.2},
        {"migration_reset_version": "other"},
        {"state_generation_id": "not-a-uuid"},
    ],
)
def test_phase2_normalization_rejects_wrong_schema_identity(mutation):
    state = fresh()
    state.update(mutation)

    assert not phase2.is_phase2_state(state)
    with pytest.raises(phase2.InvalidPhase2StateError):
        phase2.normalize_phase2_state(state)


def test_phase2_normalization_sanitizes_reset_metadata_and_numbers():
    state = fresh()
    state.update({
        "scoring_algorithm_version": True,
        "created_at": True,
        "updated_at": -5,
        "reset_from_checksum": "not-a-checksum",
        "reset_backup_path": 12,
        "reset_completed_at": float("inf"),
    })

    normalized = phase2.normalize_phase2_state(state, now=123)

    assert normalized["scoring_algorithm_version"] == 1
    assert normalized["created_at"] == 123
    assert normalized["updated_at"] == 123
    assert normalized["reset_from_checksum"] is None
    assert normalized["reset_backup_path"] is None
    assert normalized["reset_completed_at"] is None


def test_phase2_normalization_default_fallback_is_deterministic():
    state = fresh()
    state["created_at"] = "invalid"
    state["updated_at"] = "invalid"

    first = phase2.normalize_phase2_state(state)
    second = phase2.normalize_phase2_state(state)

    assert first == second
    assert first["created_at"] == 0
    assert first["updated_at"] == 0


def test_phase2_state_has_no_shared_mutable_defaults():
    first = fresh(generation_id="00000000-0000-4000-8000-000000000002")
    second = fresh(generation_id="00000000-0000-4000-8000-000000000003")
    first["servers"]["one"] = {}

    assert second["servers"] == {}


def test_fresh_phase2_state_rejects_explicit_invalid_generation_id():
    with pytest.raises(phase2.InvalidPhase2StateError):
        phase2.new_phase2_state(now=NOW, generation_id="")


def test_phase2_json_round_trip_is_deterministic(tmp_path):
    path = tmp_path / "phase2.json"
    state = fresh()
    state["future"] = {"unicode": "Café"}

    phase2.atomic_write_json(path, state)
    first_bytes = path.read_bytes()
    loaded = phase2.load_phase2_state(path)
    phase2.atomic_write_json(path, loaded)

    assert loaded == state
    assert path.read_bytes() == first_bytes


@pytest.mark.parametrize("with_legacy", [False, True])
def test_valid_active_is_authoritative_and_never_resets(tmp_path, with_legacy):
    active, legacy = paths(tmp_path)
    state = fresh()
    write_json(active, state)
    if with_legacy:
        legacy.write_bytes(b'{"confidence": 0.95}')

    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW + 1,
    )

    assert result.status is phase2.Phase2InitializationStatus.ACTIVE_LOADED
    assert result.state == state
    assert not result.reset_performed
    assert result.persistence_enabled
    assert list(tmp_path.glob("*pre-phase2*")) == []
    if with_legacy:
        assert legacy.exists()


def test_neither_file_creates_fresh_active_without_reset(tmp_path):
    active, legacy = paths(tmp_path)

    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert result.status is phase2.Phase2InitializationStatus.FRESH_CREATED
    assert result.state == fresh()
    assert not result.reset_performed
    assert result.backup_path is None
    assert phase2.load_phase2_state(active) == fresh()
    assert list(tmp_path.glob("*pre-phase2*")) == []


def test_fresh_active_write_failure_is_structured(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)

    def fail_write(*_args, **_kwargs):
        raise PermissionError("fresh write denied")

    monkeypatch.setattr(phase2, "atomic_write_json", fail_write)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.FRESH_WRITE_FAILED
    assert result.error_kind == "permission"
    assert not result.persistence_enabled
    assert not result.reset_performed
    assert not active.exists()


@pytest.mark.parametrize("legacy_bytes", [b'{"version": 2, "confidence": 0.95}', b"\xffbroken\x00json"])
def test_legacy_migration_backs_up_exact_bytes_without_importing(legacy_bytes, tmp_path):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(legacy_bytes)

    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert result.status is phase2.Phase2InitializationStatus.LEGACY_MIGRATED
    assert result.reset_performed
    assert result.persistence_enabled
    assert result.backup_path.read_bytes() == legacy_bytes
    assert result.legacy_checksum == phase2.hashlib.sha256(legacy_bytes).hexdigest()
    assert result.state["reset_from_checksum"] == result.legacy_checksum
    assert result.state["reset_backup_path"] == str(result.backup_path)
    assert result.state["reset_completed_at"] == NOW
    assert result.state["servers"] == {}
    assert "confidence" not in result.state
    assert not legacy.exists()
    assert phase2.load_phase2_state(active) == result.state


def test_backup_filename_collision_uses_suffix(tmp_path):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"new legacy")
    collision = phase2._next_timestamped_path(tmp_path, phase2.LEGACY_BACKUP_PREFIX, NOW)
    collision.write_bytes(b"different legacy")

    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert result.reset_performed
    assert result.backup_path.name.endswith("-01.json")
    assert collision.read_bytes() == b"different legacy"
    assert result.backup_path.read_bytes() == b"new legacy"


def test_matching_backup_is_reused_after_interrupted_attempt(tmp_path):
    active, legacy = paths(tmp_path)
    payload = b"legacy exact bytes"
    legacy.write_bytes(payload)
    existing = tmp_path / "companion_restart_learning.pre-phase2-20200101-000000.json"
    existing.write_bytes(payload)

    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert result.reset_performed
    assert result.backup_path == existing
    assert len(list(tmp_path.glob("companion_restart_learning.pre-phase2-*.json"))) == 1


def test_active_prepare_failure_occurs_before_new_backup(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_prepare = phase2._prepare_atomic_bytes

    def fail_active(destination, payload):
        if Path(destination) == active:
            raise PermissionError("active temp denied")
        return real_prepare(destination, payload)

    monkeypatch.setattr(phase2, "_prepare_atomic_bytes", fail_active)
    result = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert result.status is phase2.Phase2InitializationStatus.FAILED
    assert result.failure is phase2.Phase2MigrationFailure.ACTIVE_PREPARE_FAILED
    assert result.error_kind == "permission"
    assert not result.reset_performed
    assert legacy.read_bytes() == b"legacy"
    assert not active.exists()
    assert list(tmp_path.glob("*pre-phase2*")) == []


def test_legacy_read_permission_failure_is_structured(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_read = Path.read_bytes

    def fail_legacy(self):
        if self == legacy:
            raise PermissionError("legacy denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", fail_legacy)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.LEGACY_READ_FAILED
    assert result.error_kind == "permission"
    assert not result.persistence_enabled
    assert result.state["servers"] == {}
    assert not result.reset_performed


def test_backup_temporary_write_failure_preserves_legacy(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_prepare = phase2._prepare_atomic_bytes

    def fail_backup(destination, payload):
        if "pre-phase2" in Path(destination).name:
            raise OSError("backup temp failed")
        return real_prepare(destination, payload)

    monkeypatch.setattr(phase2, "_prepare_atomic_bytes", fail_backup)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.BACKUP_WRITE_FAILED
    assert legacy.exists()
    assert not active.exists()
    assert temp_files(tmp_path) == []


def test_backup_file_fsync_failure_preserves_legacy(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_fsync = phase2.os.fsync
    calls = 0

    def fail_second_file_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("backup fsync")
        return real_fsync(fd)

    monkeypatch.setattr(phase2.os, "fsync", fail_second_file_fsync)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.BACKUP_WRITE_FAILED
    assert legacy.exists()
    assert not active.exists()
    assert temp_files(tmp_path) == []


def test_backup_replacement_failure_preserves_legacy(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    monkeypatch.setattr(phase2.os, "link", lambda *_args: (_ for _ in ()).throw(PermissionError("link")))

    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.BACKUP_WRITE_FAILED
    assert result.error_kind == "permission"
    assert legacy.exists()
    assert not active.exists()


def test_backup_verification_failure_blocks_active_install(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_verify = phase2._verify_exact_bytes

    def fail_verify(path, expected):
        if "pre-phase2" in Path(path).name:
            raise OSError("verification failed")
        return real_verify(path, expected)

    monkeypatch.setattr(phase2, "_verify_exact_bytes", fail_verify)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.BACKUP_VERIFICATION_FAILED
    assert legacy.exists()
    assert not active.exists()
    assert not result.reset_performed


def test_active_install_failure_leaves_verified_backup_and_retry_reuses_it(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    payload = b"legacy"
    legacy.write_bytes(payload)
    real_install = phase2._install_prepared

    def fail_active_install(temporary, destination):
        if Path(destination) == active:
            phase2._remove_file_best_effort(Path(temporary))
            raise OSError("active replace failed")
        return real_install(temporary, destination)

    monkeypatch.setattr(phase2, "_install_prepared", fail_active_install)
    failed = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert failed.failure is phase2.Phase2MigrationFailure.ACTIVE_INSTALL_FAILED
    assert failed.backup_path.read_bytes() == payload
    assert legacy.exists()
    assert not active.exists()
    assert not failed.reset_performed

    monkeypatch.setattr(phase2, "_install_prepared", real_install)
    retried = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)
    assert retried.reset_performed
    assert retried.backup_path == failed.backup_path
    assert len(list(tmp_path.glob("*pre-phase2*"))) == 1


def test_active_verification_failure_never_reports_completed_reset(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_verify = phase2._verify_exact_bytes

    def fail_active_verify(path, expected):
        if Path(path) == active:
            raise OSError("active verification failed")
        return real_verify(path, expected)

    monkeypatch.setattr(phase2, "_verify_exact_bytes", fail_active_verify)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.ACTIVE_VERIFICATION_FAILED
    assert not result.reset_performed
    assert not result.persistence_enabled
    assert legacy.exists()
    assert result.backup_path.read_bytes() == b"legacy"
    assert not active.exists()


def test_legacy_cleanup_failure_does_not_change_success(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_unlink = Path.unlink

    def fail_legacy_cleanup(self, *args, **kwargs):
        if self == legacy:
            raise PermissionError("cleanup denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_legacy_cleanup)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.status is phase2.Phase2InitializationStatus.LEGACY_MIGRATED
    assert result.reset_performed
    assert result.persistence_enabled
    assert active.exists()
    assert legacy.exists()


def test_valid_active_prevents_repeated_reset_even_when_cleanup_left_legacy(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    real_unlink = Path.unlink

    def leave_legacy(self, *args, **kwargs):
        if self == legacy:
            raise OSError("leave legacy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", leave_legacy)
    first = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)
    second = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)

    assert first.reset_performed
    assert second.status is phase2.Phase2InitializationStatus.ACTIVE_LOADED
    assert not second.reset_performed
    assert len(list(tmp_path.glob("*pre-phase2*"))) == 1


@pytest.mark.parametrize("malformed", [b"not json", b"[]", b'{"schema_version": 2}'])
def test_malformed_active_is_preserved_and_recovered_once(tmp_path, malformed):
    active, legacy = paths(tmp_path)
    active.write_bytes(malformed)
    legacy.write_bytes(b"legacy is ignored")

    recovered = phase2.initialize_phase2_state(
        active_path=active,
        legacy_path=legacy,
        now=NOW,
        generation_id=GENERATION_ID,
    )

    assert recovered.status is phase2.Phase2InitializationStatus.ACTIVE_RECOVERED
    assert recovered.recovery_performed
    assert not recovered.reset_performed
    assert recovered.backup_path.read_bytes() == malformed
    assert ".corrupt-" in recovered.backup_path.name
    assert phase2.load_phase2_state(active) == fresh()
    assert legacy.read_bytes() == b"legacy is ignored"

    second = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)
    assert second.status is phase2.Phase2InitializationStatus.ACTIVE_LOADED
    assert not second.recovery_performed
    assert len(list(tmp_path.glob("*.corrupt-*.json"))) == 1


def test_malformed_active_backup_failure_does_not_overwrite_original(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    active.write_bytes(b"malformed")
    monkeypatch.setattr(phase2.os, "link", lambda *_args: (_ for _ in ()).throw(PermissionError("no backup")))

    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.CORRUPT_BACKUP_FAILED
    assert active.read_bytes() == b"malformed"
    assert not result.recovery_performed
    assert not result.persistence_enabled


def test_active_read_permission_failure_is_structured(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    active.write_bytes(b"present")
    real_read = Path.read_bytes

    def fail_active(self):
        if self == active:
            raise PermissionError("active read denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", fail_active)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.ACTIVE_READ_FAILED
    assert result.error_kind == "permission"
    assert not result.persistence_enabled
    assert not result.reset_performed
    assert result.state["servers"] == {}


def test_malformed_active_recovery_write_failure_preserves_original_and_copy(tmp_path, monkeypatch):
    active, legacy = paths(tmp_path)
    active.write_bytes(b"malformed")
    real_atomic = phase2.atomic_write_json

    def fail_active_write(path, data):
        if Path(path) == active:
            raise phase2.AtomicWriteError(active, "replacement", OSError("failed"))
        return real_atomic(path, data)

    monkeypatch.setattr(phase2, "atomic_write_json", fail_active_write)
    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.failure is phase2.Phase2MigrationFailure.RECOVERY_WRITE_FAILED
    assert active.read_bytes() == b"malformed"
    assert result.backup_path.read_bytes() == b"malformed"
    assert not result.reset_performed


def test_deleted_backup_does_not_disturb_valid_active(tmp_path):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")
    migrated = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)
    migrated.backup_path.unlink()

    loaded = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)

    assert loaded.status is phase2.Phase2InitializationStatus.ACTIVE_LOADED
    assert not loaded.reset_performed


def test_rollback_created_legacy_is_ignored_when_active_is_valid(tmp_path):
    active, legacy = paths(tmp_path)
    phase2.atomic_write_json(active, fresh())
    legacy.write_bytes(b'{"learned_cycle_seconds": 43200, "confidence": 0.95}')

    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)

    assert result.status is phase2.Phase2InitializationStatus.ACTIVE_LOADED
    assert result.state == fresh()
    assert legacy.exists()
    assert list(tmp_path.glob("*pre-phase2*")) == []


def test_no_migration_marker_is_ever_created(tmp_path):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")

    result = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)

    assert result.reset_performed
    names = {path.name for path in tmp_path.iterdir()}
    assert not any("marker" in name or "migration.json" in name for name in names)
    assert names == {active.name, result.backup_path.name}


def test_reset_performed_is_true_only_during_completed_migration_invocation(tmp_path):
    active, legacy = paths(tmp_path)
    legacy.write_bytes(b"legacy")

    first = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW)
    second = phase2.initialize_phase2_state(active_path=active, legacy_path=legacy, now=NOW + 1)

    assert first.reset_performed is True
    assert second.reset_performed is False
