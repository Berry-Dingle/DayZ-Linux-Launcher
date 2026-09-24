from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from dzll_launcher import background_prepare
from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    SingleServerBackgroundPreparation,
)
from dzll_launcher.preparation_contracts import (
    PreparationEventKind,
    PreparationOutcome,
    PreparationProgressEvent,
    PreparationStatus,
    RecordingPreparationPresenter,
)


SOURCE = Path(background_prepare.__file__).read_text(encoding="utf-8")
WINDOW_SOURCE = (
    Path(background_prepare.__file__).with_name("window.py").read_text(encoding="utf-8")
)


class FakeWindow:
    def __init__(self):
        self._steamcmd_cancel_event = threading.Event()
        self._join_steam_start_allowed = False
        self._join_attempts = SimpleNamespace(active=None)
        self.consent_calls = 0

    def _ensure_join_steam_start_consent(self, attempt_id):
        assert attempt_id == 0
        self.consent_calls += 1
        self._join_steam_start_allowed = True
        return True


@pytest.fixture
def snapshot():
    row = SimpleNamespace(
        ip="10.0.0.1", gport=2302, qport=27016, name="Full Server Name",
    )
    return BackgroundServerPreparationSnapshot.from_server(
        row, [(101, "One"), (202, "Two")],
    )


@pytest.fixture
def runtime():
    return BackgroundPreparationRuntime(
        workshop_dir="/workshop",
        mod_management_enabled=True,
        auto_install_missing=True,
    )


@pytest.mark.parametrize("status", list(PreparationStatus))
def test_background_calls_shared_once_passes_presenter_and_outcome_unchanged(
        monkeypatch, snapshot, runtime, status):
    win = FakeWindow()
    presenter = RecordingPreparationPresenter()
    expected = PreparationOutcome(status, reason=status.value, backend="steam_client")
    calls = []

    def fake_prepare(*args, **kwargs):
        calls.append((args, kwargs))
        assert args[0] is win
        assert args[1] == snapshot.required_mods
        assert kwargs["presenter"] is presenter
        kwargs["presenter"].on_terminal(expected)
        return expected

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    actual = SingleServerBackgroundPreparation(win).run(snapshot, runtime, presenter)
    assert actual is expected
    assert presenter.terminal is expected
    assert len(calls) == 1
    assert win.consent_calls == 1


def test_no_required_mods_skips_steam_consent_and_preserves_outcome(monkeypatch, runtime):
    win = FakeWindow()
    presenter = RecordingPreparationPresenter()
    empty = BackgroundServerPreparationSnapshot("1.2.3.4", 2302, 0, "Empty", ())
    expected = PreparationOutcome(PreparationStatus.NO_REQUIRED_MODS)

    def fake_prepare(*_args, **kwargs):
        kwargs["presenter"].on_terminal(expected)
        return expected

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    assert SingleServerBackgroundPreparation(win).run(empty, runtime, presenter) is expected
    assert win.consent_calls == 0


def test_snapshot_is_immutable_detached_and_contains_no_row_or_widget_reference():
    row = SimpleNamespace(ip="1.2.3.4", gport=2302, qport=27016, name="Original")
    mods = [[11, "First"]]
    snapshot = BackgroundServerPreparationSnapshot.from_server(row, mods)
    row.name = "Recycled"
    mods[0][1] = "Mutated"
    assert snapshot.identity == "1.2.3.4:2302"
    assert snapshot.name == "Original"
    assert snapshot.required_mods == ((11, "First"),)
    assert all(value is not row for value in vars(snapshot).values())
    with pytest.raises(FrozenInstanceError):
        snapshot.name = "Changed"


def test_recording_presenter_preserves_event_order_identity_and_values():
    presenter = RecordingPreparationPresenter()
    first = PreparationProgressEvent(
        PreparationEventKind.ITEM, operation_id=7, item_id=101,
        item_name="Exact", downloaded_bytes=13, total_bytes=29,
    )
    second = PreparationProgressEvent(
        PreparationEventKind.ITEM_COMPLETED, operation_id=7, item_id=101,
        item_name="Exact", downloaded_bytes=29, total_bytes=29,
    )
    presenter.on_event(first)
    presenter.on_event(second)
    assert presenter.events == (first, second)
    assert presenter.events[0] is first
    assert presenter.events[1] is second
    assert not hasattr(presenter, "select_item")
    assert not hasattr(presenter, "calculate_progress")
    assert not hasattr(presenter, "schedule")


def test_busy_operation_is_rejected_without_calling_shared_engine(monkeypatch, snapshot, runtime):
    win = FakeWindow()
    gate = background_prepare.preparation_operation_gate(win)
    lease = gate.try_acquire("join")
    calls = []
    monkeypatch.setattr(background_prepare, "prepare_required_mods", lambda *_a, **_k: calls.append(1))
    presenter = RecordingPreparationPresenter()
    outcome = SingleServerBackgroundPreparation(win).run(snapshot, runtime, presenter)
    assert outcome.status is PreparationStatus.FAILED
    assert outcome.reason == "preparation_busy"
    assert presenter.terminal is outcome
    assert calls == []
    assert gate.release(lease)


def test_active_join_is_rejected_before_gate_acquisition(monkeypatch, snapshot, runtime):
    win = FakeWindow()
    win._join_attempts.active = object()
    monkeypatch.setattr(
        background_prepare, "prepare_required_mods",
        lambda *_a, **_k: pytest.fail("shared engine must not run while Join is active"),
    )
    outcome = SingleServerBackgroundPreparation(win).run(snapshot, runtime)
    assert outcome.reason == "preparation_busy"
    assert background_prepare.preparation_operation_gate(win).active_owner == ""


@pytest.mark.parametrize("status", list(PreparationStatus))
def test_every_terminal_path_releases_ownership(monkeypatch, snapshot, runtime, status):
    win = FakeWindow()
    expected = PreparationOutcome(status)

    def fake_prepare(*_args, **kwargs):
        kwargs["presenter"].on_terminal(expected)
        return expected

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    controller = SingleServerBackgroundPreparation(win)
    assert controller.run(snapshot, runtime, RecordingPreparationPresenter()) is expected
    assert controller.active is False
    assert background_prepare.preparation_operation_gate(win).active_owner == ""


def test_unconfirmed_reap_failure_poison_is_recoverable_from_process_handle(
        monkeypatch, snapshot, runtime):
    win = FakeWindow()

    class Process:
        alive = True

        def poll(self):
            return None if self.alive else 0

    process = Process()
    monkeypatch.setattr(
        background_prepare,
        "prepare_required_mods",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            background_prepare.UGCHelperReapError(
                "helper unresolved", process=process,
            )
        ),
    )
    controller = SingleServerBackgroundPreparation(win)
    with pytest.raises(background_prepare.UGCHelperReapError):
        controller.run(snapshot, runtime)
    gate = background_prepare.preparation_operation_gate(win)
    assert gate.blocked_reap_failure and controller.active
    assert not gate.try_recover_reap_failure()
    process.alive = False
    assert gate.try_recover_reap_failure()
    assert not gate.blocked_reap_failure and not controller.active


def test_recovery_generation_cannot_clear_a_different_blocked_lease():
    win = FakeWindow()

    class Process:
        def poll(self):
            return 0

    gate = background_prepare.preparation_operation_gate(win)
    lease = gate.try_acquire("foreground_join")
    assert gate.mark_reap_failure(
        lease,
        background_prepare.UGCHelperReapError(
            "old helper exited", process=Process(),
        ),
    )
    assert not gate.try_recover_reap_failure(
        expected_generation=lease.generation + 1,
    )
    assert gate.blocked_reap_failure
    assert gate.try_recover_reap_failure(
        expected_generation=lease.generation,
    )


def test_process_dead_reader_failure_releases_gate_immediately(
        monkeypatch, snapshot, runtime):
    win = FakeWindow()
    monkeypatch.setattr(
        background_prepare,
        "prepare_required_mods",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            background_prepare.UGCHelperReapError(
                "reader survived",
                helper_process_confirmed_dead=True,
                helper_process_may_be_alive=False,
                reader_cleanup_only=True,
            )
        ),
    )
    controller = SingleServerBackgroundPreparation(win)
    with pytest.raises(background_prepare.UGCHelperReapError):
        controller.run(snapshot, runtime)
    gate = background_prepare.preparation_operation_gate(win)
    assert not gate.blocked_reap_failure and not controller.active


def test_cancel_signals_existing_event_and_releases_owner(monkeypatch, snapshot, runtime):
    win = FakeWindow()
    controller = SingleServerBackgroundPreparation(win)
    presenter = RecordingPreparationPresenter()
    entered = threading.Event()
    seen_cancel_events = []

    def fake_prepare(*_args, **kwargs):
        seen_cancel_events.append(kwargs["cancel_event"])
        entered.set()
        assert kwargs["cancel_event"].wait(timeout=2.0)
        outcome = PreparationOutcome(
            PreparationStatus.CANCELLED, reason="cancelled",
            error="Mod download cancelled",
        )
        kwargs["presenter"].on_terminal(outcome)
        return outcome

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    result = []
    worker = threading.Thread(target=lambda: result.append(controller.run(snapshot, runtime, presenter)))
    worker.start()
    assert entered.wait(timeout=2.0)
    assert controller.cancel() is True
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert result[0].status is PreparationStatus.CANCELLED
    assert presenter.cancelling_operation_ids
    assert presenter.terminal is result[0]
    assert controller.active is False
    assert seen_cancel_events == [controller.cancel_event]
    assert controller.cancel_event is not win._steamcmd_cancel_event
    assert controller.cancel_event.is_set() is False
    assert win._steamcmd_cancel_event.is_set() is False


def test_old_operation_validity_guard_expires_after_release(monkeypatch, snapshot, runtime):
    win = FakeWindow()
    guards = []
    expected = PreparationOutcome(PreparationStatus.READY)

    def fake_prepare(*_args, **kwargs):
        guards.append(kwargs["is_operation_current"])
        assert guards[-1]() is True
        kwargs["presenter"].on_terminal(expected)
        return expected

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    SingleServerBackgroundPreparation(win).run(snapshot, runtime, RecordingPreparationPresenter())
    assert guards[0]() is False


def test_sequential_background_operations_have_distinct_cancel_identity(
        monkeypatch, snapshot, runtime):
    win = FakeWindow()
    observed = []

    def fake_prepare(*_args, **kwargs):
        observed.append(kwargs["cancel_event"])
        return PreparationOutcome(PreparationStatus.READY, reason="ready")

    monkeypatch.setattr(background_prepare, "prepare_required_mods", fake_prepare)
    first = SingleServerBackgroundPreparation(win)
    second = SingleServerBackgroundPreparation(win)
    assert first.run(snapshot, runtime).status is PreparationStatus.READY
    first.cancel_event.set()
    assert second.run(snapshot, runtime).status is PreparationStatus.READY
    assert observed == [first.cancel_event, second.cancel_event]
    assert observed[0] is not observed[1]
    assert observed[1].is_set() is False


def test_declined_shared_steam_consent_is_cancelled_and_releases_gate(
        monkeypatch, snapshot, runtime):
    win = FakeWindow()
    win._ensure_join_steam_start_consent = lambda _operation_id: False
    monkeypatch.setattr(
        background_prepare, "prepare_required_mods",
        lambda *_a, **_k: pytest.fail("preparation must not start after declined consent"),
    )
    presenter = RecordingPreparationPresenter()
    outcome = SingleServerBackgroundPreparation(win).run(snapshot, runtime, presenter)
    assert outcome.status is PreparationStatus.CANCELLED
    assert outcome.reason == "steam_start_declined"
    assert presenter.terminal is outcome
    assert background_prepare.preparation_operation_gate(win).active_owner == ""


def test_background_caller_contains_no_scheduler_poller_selector_or_join_continuation():
    forbidden = (
        "wait_for_ugc_ready", "query_ugc_state", "ugc_item_ready",
        "run_steam_client_install", "run_steamcmd_install",
        "ensure_watch_symlinks", "bootstrap_launcher_state",
        "_launch_direct_steam_url", "set_server_companion_server",
        "set_installing_mods", "linux_to_win_path_under_prefix",
    )
    for name in forbidden:
        assert name not in SOURCE
    assert SOURCE.count("prepare_required_mods(") == 1


def test_normal_join_entry_rejects_background_gate_before_starting_attempt():
    entry = WINDOW_SOURCE.split("def _join_server_for_obj", 1)[1].split(
        "def _prune_expired_dead", 1
    )[0]
    assert entry.index("shared_join_preparation_busy(self)") < entry.index(
        'gate.try_acquire("foreground_join")'
    )
    assert entry.index('gate.try_acquire("foreground_join")') < entry.index(
        "self._join_attempts.begin("
    )
    assert entry.index("self._join_attempts.begin(") < entry.index(
        "self._steamcmd_cancel_event = threading.Event()"
    )
