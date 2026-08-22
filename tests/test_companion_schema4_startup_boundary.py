from pathlib import Path
from types import SimpleNamespace

import pytest

from dzll_launcher import app as app_module
from dzll_launcher import config
from dzll_launcher import companion_restart_phase2_runtime as runtime
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_storage as storage
from dzll_launcher import window as window_module


def _window_start(active: Path, legacy: Path):
    return window_module._initialize_companion_restart_runtime_for_window(
        active_path=active,
        legacy_path=legacy,
        authoritative_schema4_runtime_enabled=(
            config.AUTHORITATIVE_SCHEMA4_RUNTIME_ENABLED
        ),
        schema4_authority_consumer_shadow_enabled=(
            config.SCHEMA4_AUTHORITY_CONSUMER_SHADOW_ENABLED
        ),
        schema4_authority_production_cutover_enabled=(
            config.SCHEMA4_AUTHORITY_PRODUCTION_CUTOVER_ENABLED
        ),
    )


@pytest.mark.parametrize("state", ["fresh", "schema3", "residual_failure"])
def test_app_activation_with_boundary_window_retains_startup_states(
    tmp_path, monkeypatch, state
):
    """Exercise app activation without claiming a real GTK window was built."""
    active = tmp_path / "companion_restart_learning_phase2.json"
    legacy = tmp_path / "companion_restart_learning.json"
    if state == "schema3":
        active.write_bytes(
            schema4.canonical_json_bytes(
                storage.new_phase2_state(
                    now=1_900_000_000,
                    generation_id="11111111-1111-4111-8111-111111111111",
                )
            )
        )
    elif state == "residual_failure":
        active.write_bytes(b"unrecoverable-state")

    class BoundaryWindow:
        def __init__(self, application):
            self.application = application
            self.restart_runtime = _window_start(active, legacy)
            self.presented = False

        def present(self):
            self.presented = True

    monkeypatch.setattr(app_module, "DZLLWindow", BoundaryWindow)
    application = app_module.DZLLApp()
    application.do_activate()
    assert application.window is not None and application.window.presented
    value = application.window.restart_runtime
    if state == "residual_failure":
        assert not value.persistence_enabled
        assert value.pending_notice.kind == "initialization_failed"
        assert active.read_bytes() == b"unrecoverable-state"
    else:
        assert value.persistence_enabled
        assert schema4.deserialize_schema4_bytes(
            active.read_bytes(), quarantine_invalid_servers=False
        ).report.valid
        value._authoritative_schema4_backend.close(flush=False)


def test_window_boundary_converts_unexpected_initializer_error(tmp_path, monkeypatch):
    active = tmp_path / "companion_restart_learning_phase2.json"

    def fail(_cls, **_kwargs):
        raise RuntimeError("unexpected startup failure")

    monkeypatch.setattr(
        runtime.Phase2RestartRuntime,
        "initialize",
        classmethod(fail),
    )
    value = _window_start(active, tmp_path / "legacy.json")
    assert not value.persistence_enabled
    assert value.migration.error_kind == "RuntimeError"
    assert value.persistence_error == "RuntimeError: unexpected startup failure"
    assert value.pending_notice.kind == "initialization_failed"


def test_runtime_write_failure_notice_is_presented_once():
    notice = runtime.RuntimeNotice(
        "persistence_write_failed",
        "Learning Is Not Being Saved",
        "Technical error: disk",
    )
    shown = []
    boundary = SimpleNamespace(
        _companion_restart_phase2=SimpleNamespace(
            pending_notice=notice,
            persistence_error="OSError: disk",
        ),
        restart_learning_notice_ui=SimpleNamespace(
            show=lambda value: shown.append(value) or True
        ),
    )
    assert window_module.DZLLWindow._surface_restart_learning_persistence_notice(
        boundary
    )
    assert not window_module.DZLLWindow._surface_restart_learning_persistence_notice(
        boundary
    )
    assert shown == [notice]


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("pending_invalid", "payload is invalid", "pending_import_invalid"),
        (
            "pending_invalid",
            "payload and metadata are incomplete",
            "pending_import_metadata_invalid",
        ),
        ("apply_failed", "disk", "pending_import_apply_failure"),
        ("rollback_failed", "restore", "pending_import_rollback_failure"),
    ],
)
def test_pending_import_startup_error_kind_preserves_transaction_stage(
    status, error, expected
):
    result = SimpleNamespace(status=status, error=error)
    assert window_module._pending_import_startup_error_kind(result) == expected
