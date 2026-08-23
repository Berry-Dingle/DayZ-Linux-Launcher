from __future__ import annotations

import errno

import pytest

from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_schema4_runtime as live4
from dzll_launcher import window as window_module


BASE = 1_950_000_000.0
A = "recovery-a.example:2302"
B = "recovery-b.example:2302"
C = "recovery-c.example:2302"


def _runtime(tmp_path):
    active = tmp_path / "companion_restart_learning_phase2.json"
    value = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "legacy.json",
        now=BASE,
        generation_id="55555555-5555-4555-8555-555555555555",
        authoritative_schema4_runtime_enabled=True,
    )
    return value, active


def _begin(value, key, generation, offset):
    return value.begin_monitoring(
        key,
        wall_at=BASE + offset,
        monotonic_at=offset,
        poll_generation=generation,
    )


def _state(path):
    loaded = schema4.deserialize_schema4_bytes(
        path.read_bytes(), quarantine_invalid_servers=False
    )
    assert loaded.report.valid
    return loaded.state


def _fail_installs(monkeypatch, count, active):
    real = live4.os.replace
    calls = []

    def failing(source, destination):
        if destination != active:
            return real(source, destination)
        calls.append(source)
        if len(calls) <= count:
            raise OSError(errno.ENOSPC, "injected pre-commit failure")
        return real(source, destination)

    monkeypatch.setattr(live4.os, "replace", failing)
    return calls


def test_transient_precommit_failure_recovers_with_cross_server_mutation(
    tmp_path, monkeypatch
):
    value, active = _runtime(tmp_path)
    baseline = active.read_bytes()
    calls = _fail_installs(monkeypatch, 1, active)

    session_a = _begin(value, A, 1, 1)
    assert session_a
    assert active.read_bytes() == baseline
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    assert value.persistence_enabled
    assert value.pending_notice.kind == "persistence_write_failed"
    assert value._authoritative_schema4_backend.dirty

    session_b = _begin(value, B, 2, 2)
    assert session_b and len(calls) == 2
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert value.persistence_error is None
    assert value.pending_notice is None
    assert not value._authoritative_schema4_backend.dirty
    assert not value._servers[A].dirty
    assert not value._servers[B].dirty
    assert set(_state(active)["servers"]) == {A, B}
    value.shutdown(wall_at=BASE + 10, monotonic_at=10)


def test_same_server_lifecycle_retries_staged_snapshot_without_duplication(
    tmp_path, monkeypatch
):
    value, active = _runtime(tmp_path)
    _fail_installs(monkeypatch, 1, active)
    session = _begin(value, A, 1, 1)
    assert session

    ended = value.end_monitoring(
        A,
        marker=runtime.LifecycleMarker.PAUSE,
        wall_at=BASE + 2,
        monotonic_at=2,
    )
    assert ended.accepted
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    record = _state(active)["servers"][A]["legacy_schema3_record"]
    sessions = record["monitoring_sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == session
    assert sessions[0]["reason"] == "pause"
    value.shutdown(wall_at=BASE + 3, monotonic_at=3)


def test_shutdown_retries_retryable_staged_state(tmp_path, monkeypatch):
    value, active = _runtime(tmp_path)
    _fail_installs(monkeypatch, 1, active)
    assert _begin(value, A, 1, 1)
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )

    value.shutdown(wall_at=BASE + 2, monotonic_at=2)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    record = _state(active)["servers"][A]["legacy_schema3_record"]
    assert record["monitoring_sessions"][-1]["reason"] == "shutdown"


def test_repeated_precommit_failure_waits_for_later_durable_boundaries(
    tmp_path, monkeypatch
):
    value, active = _runtime(tmp_path)
    calls = _fail_installs(monkeypatch, 2, active)

    assert _begin(value, A, 1, 1)
    assert len(calls) == 1
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    assert _begin(value, B, 2, 2)
    assert len(calls) == 2
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    assert _begin(value, C, 3, 3)
    assert len(calls) == 3
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert set(_state(active)["servers"]) == {A, B, C}
    value.shutdown(wall_at=BASE + 4, monotonic_at=4)


def test_generation_conflict_is_terminal_and_begin_returns_no_session(tmp_path):
    value, active = _runtime(tmp_path)
    external = active.read_bytes() + b" "
    active.write_bytes(external)

    assert _begin(value, A, 1, 1) == ""
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    )
    assert "Schema4GenerationConflict" in value.persistence_error
    assert _begin(value, B, 2, 2) == ""
    assert active.read_bytes() == external
    value.shutdown(wall_at=BASE + 3, monotonic_at=3)
    assert active.read_bytes() == external


def test_post_replace_failure_is_terminal_and_complete_file_is_not_retried(
    tmp_path
):
    value, active = _runtime(tmp_path)
    backend = value._authoritative_schema4_backend
    backend._crash_injector = lambda point: (
        (_ for _ in ()).throw(RuntimeError("injected after replace"))
        if point is live4.Schema4CrashPoint.AFTER_REPLACE
        else None
    )

    assert _begin(value, A, 1, 1) == ""
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    )
    assert "injected after replace" in value.persistence_error
    installed = active.read_bytes()
    assert _state(active)["runtime_write_generation"] == 1
    assert _begin(value, B, 2, 2) == ""
    value.shutdown(wall_at=BASE + 3, monotonic_at=3)
    assert active.read_bytes() == installed


def test_validation_failure_is_terminal_and_restart_recovers(
    tmp_path, monkeypatch
):
    value, active = _runtime(tmp_path)
    backend = value._authoritative_schema4_backend
    real_update = backend.update_server_from_schema3
    monkeypatch.setattr(
        backend,
        "update_server_from_schema3",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            schema4.Schema4ValidationError("injected invalid state")
        ),
    )

    assert _begin(value, A, 1, 1) == ""
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    )
    value._record_schema4_persistence_failure(
        live4.Schema4WriteFailure(
            live4.Schema4WriteFailurePhase.CONFIRMED_PRE_COMMIT,
            OSError("later transient failure"),
        )
    )
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DISABLED_WRITE_FAILED
    )
    monkeypatch.setattr(backend, "update_server_from_schema3", real_update)
    assert _begin(value, B, 2, 2) == ""
    value.shutdown(wall_at=BASE + 3, monotonic_at=3)

    again = runtime.Phase2RestartRuntime.initialize(
        active_path=active,
        legacy_path=tmp_path / "legacy.json",
        now=BASE + 4,
        authoritative_schema4_runtime_enabled=True,
    )
    assert again.persistence_enabled
    assert _begin(again, B, 2, 5)
    again.shutdown(wall_at=BASE + 6, monotonic_at=6)


def test_persistence_notice_clears_when_retry_recovers():
    class Presenter:
        def __init__(self):
            self.shown = []
            self.hidden = 0
            self.is_visible = False

        def show(self, notice):
            self.shown.append(notice)
            self.is_visible = True
            return True

        def visible(self):
            return self.is_visible

        def hide(self):
            self.hidden += 1
            self.is_visible = False

    presenter = Presenter()
    host = type("Host", (), {})()
    host.restart_learning_notice_ui = presenter
    host._companion_restart_phase2 = type("Runtime", (), {})()
    host._companion_restart_phase2.pending_notice = runtime.RuntimeNotice(
        "persistence_write_failed", "Not saved", "Retry pending"
    )
    host._companion_restart_phase2.persistence_error = "OSError: disk"

    assert window_module.DZLLWindow._surface_restart_learning_persistence_notice(host)
    host._companion_restart_phase2.pending_notice = None
    assert not window_module.DZLLWindow._surface_restart_learning_persistence_notice(host)
    assert presenter.hidden == 1
    assert host._restart_learning_persistence_notice_signature is None


@pytest.mark.parametrize("failure_errno", [errno.EACCES, errno.ENOSPC, errno.EIO])
def test_precommit_oserror_classification_requires_unchanged_destination(
    tmp_path, monkeypatch, failure_errno
):
    value, active = _runtime(tmp_path)
    baseline = active.read_bytes()
    real_replace = live4.os.replace

    def fail_active_replace(source, destination):
        if destination == active:
            raise OSError(failure_errno, "injected replace refusal")
        return real_replace(source, destination)

    monkeypatch.setattr(live4.os, "replace", fail_active_replace)
    assert _begin(value, A, 1, 1)
    assert active.read_bytes() == baseline
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    value._authoritative_schema4_backend.close(flush=False)


def test_temp_creation_failure_is_retryable_when_active_is_unchanged(
    tmp_path, monkeypatch
):
    value, active = _runtime(tmp_path)
    baseline = active.read_bytes()
    real_mkstemp = live4.tempfile.mkstemp
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(kwargs.get("prefix"))
        if len(calls) == 1:
            raise OSError(errno.ENOSPC, "injected temp creation failure")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(live4.tempfile, "mkstemp", fail_once)
    assert _begin(value, A, 1, 1)
    assert active.read_bytes() == baseline
    assert value.persistence_status is (
        runtime.RuntimePersistenceStatus.DEGRADED_RETRYABLE_WRITE_FAILED
    )
    assert _begin(value, B, 2, 2)
    assert value.persistence_status is runtime.RuntimePersistenceStatus.ENABLED
    assert set(_state(active)["servers"]) == {A, B}
    value.shutdown(wall_at=BASE + 3, monotonic_at=3)
