from types import SimpleNamespace
import threading

import pytest

from dzll_launcher import background_prepare
from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    SingleServerBackgroundPreparation,
)
from dzll_launcher.background_prepare_queue import (
    BackgroundPreparationQueue,
    BackgroundRetryItem,
    BackgroundRetrySetupFailure,
    BackgroundServerState,
)
from dzll_launcher.preparation_contracts import (
    PreparationOutcome,
    PreparationStatus,
    RecordingPreparationPresenter,
)


def snap(identity, mods=((101, "One"),), name=None):
    ip, port = identity.split(":")
    return BackgroundServerPreparationSnapshot(
        ip, int(port), 27016, name or identity, tuple(mods),
    )


def runtime():
    return BackgroundPreparationRuntime("/workshop", True, True)


class Controller:
    def __init__(self):
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        return True


def attach_and_return(queue, request, outcome):
    controller = Controller()
    assert queue.attach_controller(request, controller)
    transition = queue.finish(request, outcome)
    assert transition.accepted
    return transition, controller


READY = PreparationOutcome(PreparationStatus.READY, reason="ready")
FAILED = PreparationOutcome(
    PreparationStatus.FAILED, reason="failed", error="specific failure",
)
CANCELLED = PreparationOutcome(PreparationStatus.CANCELLED, reason="cancelled")


def test_two_and_three_servers_preserve_fifo_with_one_active_reservation():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    b = queue.enqueue(snap("10.0.0.2:2302"), runtime())
    c = queue.enqueue(snap("10.0.0.3:2302"), runtime())
    assert a.dispatch.identity == "10.0.0.1:2302"
    assert b.dispatch is None and c.dispatch is None
    assert [request.identity for request in queue.snapshot().pending] == [
        "10.0.0.2:2302", "10.0.0.3:2302",
    ]

    after_a, _ = attach_and_return(queue, a.dispatch, READY)
    assert after_a.dispatch.identity == "10.0.0.2:2302"
    after_b, _ = attach_and_return(queue, after_a.dispatch, READY)
    assert after_b.dispatch.identity == "10.0.0.3:2302"
    after_c, _ = attach_and_return(queue, after_b.dispatch, READY)
    assert after_c.dispatch is None
    assert [entry.identity for entry in after_c.snapshot.completed_batch.entries] == [
        "10.0.0.1:2302", "10.0.0.2:2302", "10.0.0.3:2302",
    ]


def test_duplicate_active_and_queued_clicks_are_noops_without_allocating_ids():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    b = queue.enqueue(snap("10.0.0.2:2302"), runtime())
    assert not queue.enqueue(snap("10.0.0.1:2302"), runtime()).accepted
    assert not queue.enqueue(snap("10.0.0.2:2302"), runtime()).accepted
    assert [request.identity for request in queue.snapshot().pending] == [
        "10.0.0.2:2302",
    ]
    c = queue.enqueue(snap("10.0.0.3:2302"), runtime())
    assert c.request.request_id == b.request.request_id + 1
    assert a.request.request_id == 1


def test_failure_is_retained_while_next_request_is_preparing_and_summary_is_ordered():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302", name="A"), runtime())
    queue.enqueue(snap("10.0.0.2:2302", name="B"), runtime())
    after_a, _ = attach_and_return(queue, a.dispatch, FAILED)
    record_a = queue.record_for("10.0.0.1:2302")
    assert record_a.state is BackgroundServerState.FAILED
    assert record_a.error == "specific failure"
    assert after_a.dispatch.display_name == "B"
    assert queue.record_for("10.0.0.2:2302").state is BackgroundServerState.PREPARING
    after_b, _ = attach_and_return(queue, after_a.dispatch, READY)
    batch = after_b.snapshot.completed_batch
    assert (batch.ready_count, batch.failed_count, batch.cancelled_count) == (1, 1, 0)
    assert [entry.display_name for entry in batch.entries] == ["A", "B"]
    assert [entry.display_name for entry in batch.failed_entries] == ["A"]


def test_early_terminal_observation_never_advances_before_worker_return():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    queue.enqueue(snap("10.0.0.2:2302"), runtime())
    controller = Controller()
    assert queue.attach_controller(a.dispatch, controller)
    assert queue.observe_terminal(a.dispatch, FAILED)
    assert queue.snapshot().active == a.dispatch
    assert queue.record_for(a.dispatch.identity).outcome is FAILED
    transition = queue.finish(a.dispatch, FAILED)
    assert transition.dispatch.identity == "10.0.0.2:2302"


def test_reap_block_terminalizes_active_without_dispatching_pending():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302", name="A"), runtime())
    queue.enqueue(snap("10.0.0.2:2302", name="B"), runtime())
    controller = Controller()
    assert queue.attach_controller(a.dispatch, controller)
    blocked = queue.finish_blocked_reap_failure(a.dispatch, FAILED)
    assert blocked.accepted and blocked.dispatch is None
    assert blocked.snapshot.active is None
    assert blocked.snapshot.blocked_reap_failure
    assert [item.display_name for item in blocked.snapshot.pending] == ["B"]
    assert queue.record_for(a.dispatch.identity).state is BackgroundServerState.FAILED
    assert not queue.accepting

    resumed = queue.clear_reap_block()
    assert resumed.accepted
    assert not resumed.snapshot.blocked_reap_failure
    assert resumed.dispatch.display_name == "B"
    assert queue.accepting


def test_cancel_blocked_pending_never_enters_cancelling_state():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302", name="A"), runtime())
    b = queue.enqueue(snap("10.0.0.2:2302", name="B"), runtime())
    queue.attach_controller(a.dispatch, Controller())
    queue.finish_blocked_reap_failure(a.dispatch, FAILED)
    cancelled = queue.cancel_blocked_pending()
    assert cancelled.accepted
    assert cancelled.snapshot.blocked_reap_failure
    assert cancelled.snapshot.active is None
    assert cancelled.snapshot.pending == ()
    assert not cancelled.snapshot.busy
    assert not cancelled.snapshot.cancelling
    assert queue.record_for(b.request.identity).state is BackgroundServerState.IDLE


def test_stale_progress_terminal_and_finish_cannot_mutate_retried_identity():
    queue = BackgroundPreparationQueue()
    first = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    done, _ = attach_and_return(queue, first.dispatch, FAILED)
    assert done.snapshot.completed_batch is not None
    retry = queue.enqueue(snap("10.0.0.1:2302", name="Fresh"), runtime())
    assert retry.request.request_id != first.request.request_id
    assert not queue.update_progress(first.request, object())
    assert not queue.observe_terminal(first.request, READY)
    assert not queue.finish(first.request, READY).accepted
    record = queue.record_for(first.request.identity)
    assert record.request_id == retry.request.request_id
    assert record.state is BackgroundServerState.PREPARING


def test_shared_mods_are_rechecked_by_existing_engine_and_not_cached_in_queue(monkeypatch):
    queue = BackgroundPreparationQueue()
    win = SimpleNamespace(
        _join_preparation_cancel_event=threading.Event(),
        _join_steam_start_allowed=False,
        _join_attempts=SimpleNamespace(active=None),
    )
    installed = set()
    work_sets = []

    def engine(_win, mods, *_args, **kwargs):
        work = [mid for mid, _name in mods if mid not in installed]
        work_sets.append(tuple(work))
        installed.update(work)
        outcome = PreparationOutcome(
            PreparationStatus.READY, reason="ready", did_work=bool(work),
        )
        kwargs["presenter"].on_terminal(outcome)
        return outcome

    monkeypatch.setattr(background_prepare, "prepare_required_mods", engine)
    a = queue.enqueue(
        snap("10.0.0.1:2302", ((1, "One"), (2, "Two"))), runtime(),
    )
    queue.enqueue(
        snap("10.0.0.2:2302", ((2, "Two"), (3, "Three"))), runtime(),
    )
    queue.enqueue(
        snap("10.0.0.3:2302", ((1, "One"), (2, "Two"))), runtime(),
    )
    request = a.dispatch
    while request is not None:
        controller = SingleServerBackgroundPreparation(win)
        assert queue.attach_controller(request, controller)
        outcome = controller.run(
            request.snapshot, request.runtime, RecordingPreparationPresenter(),
            ensure_steam_consent=lambda: True,
        )
        request = queue.finish(request, outcome).dispatch
    assert work_sets == [(1, 2), (3,), ()]


def test_individual_cancelled_outcome_advances_normally():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    queue.enqueue(snap("10.0.0.2:2302"), runtime())
    transition, _ = attach_and_return(queue, a.dispatch, CANCELLED)
    assert queue.record_for(a.request.identity).state is BackgroundServerState.CANCELLED
    assert transition.dispatch.identity == "10.0.0.2:2302"


def test_submission_failure_advances_once_and_remains_actionable():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    queue.enqueue(snap("10.0.0.2:2302"), runtime())
    transition = queue.submission_failed(a.dispatch, RuntimeError("executor closed"))
    assert transition.accepted
    assert transition.dispatch.identity == "10.0.0.2:2302"
    record = queue.record_for(a.request.identity)
    assert record.state is BackgroundServerState.FAILED
    assert "executor closed" in record.error


def test_batch_cancel_clears_pending_signals_only_active_and_waits_for_return():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    b = queue.enqueue(snap("10.0.0.2:2302"), runtime())
    c = queue.enqueue(snap("10.0.0.3:2302"), runtime())
    controller = Controller()
    assert queue.attach_controller(a.dispatch, controller)
    cancelled = queue.cancel_all()
    assert cancelled.controller_to_cancel is controller
    cancelled.controller_to_cancel.cancel()
    assert controller.cancel_calls == 1
    assert cancelled.snapshot.busy
    assert cancelled.snapshot.cancelling
    assert not cancelled.snapshot.accepting
    assert cancelled.snapshot.pending == ()
    assert queue.record_for(b.request.identity).state is BackgroundServerState.IDLE
    assert queue.record_for(c.request.identity).state is BackgroundServerState.IDLE
    assert not queue.enqueue(snap("10.0.0.4:2302"), runtime()).accepted

    finished = queue.finish(a.dispatch, READY)
    assert finished.accepted
    assert finished.dispatch is None
    assert not finished.snapshot.busy
    assert finished.snapshot.accepting
    assert finished.snapshot.completed_batch.cancelled_by_user
    assert queue.record_for(a.request.identity).state is BackgroundServerState.CANCELLED


def test_cancel_before_controller_attachment_invalidates_reserved_dispatch():
    queue = BackgroundPreparationQueue()
    active = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    transition = queue.cancel_all()
    assert transition.accepted
    assert transition.controller_to_cancel is None
    assert not transition.snapshot.busy
    assert not queue.is_current(active.dispatch)
    assert transition.snapshot.completed_batch.cancelled_count == 1


def test_completed_close_resets_only_batch_terminal_rows_to_idle():
    queue = BackgroundPreparationQueue()
    active = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    done, _ = attach_and_return(queue, active.dispatch, READY)
    assert done.snapshot.completed_batch is not None
    assert queue.record_for(active.request.identity).state is BackgroundServerState.READY
    closed = queue.clear_completed_batch()
    assert closed.accepted
    assert closed.snapshot.completed_batch is None
    assert queue.record_for(active.request.identity).state is BackgroundServerState.IDLE


def test_retry_failed_uses_new_batch_ids_request_ids_fresh_snapshots_and_order():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302", name="A-old"), runtime())
    queue.enqueue(snap("10.0.0.2:2302", name="B-old"), runtime())
    after_a, _ = attach_and_return(queue, a.dispatch, FAILED)
    finished, _ = attach_and_return(queue, after_a.dispatch, FAILED)
    old = finished.snapshot.completed_batch

    retry = queue.enqueue_retry_batch((
        BackgroundRetryItem(0, snap("10.0.0.1:2302", ((9, "Fresh"),), "A-new"), runtime()),
        BackgroundRetryItem(1, snap("10.0.0.2:2302", ((8, "Fresh"),), "B-new"), runtime()),
    ))
    assert retry.accepted
    assert retry.dispatch.batch_id != old.batch_id
    assert retry.dispatch.request_id > max(entry.request_id for entry in old.entries)
    assert retry.dispatch.snapshot.required_mods == ((9, "Fresh"),)
    assert [request.display_name for request in retry.snapshot.pending] == ["B-new"]


def test_retry_missing_server_is_failed_setup_result_and_does_not_block_others():
    queue = BackgroundPreparationQueue()
    transition = queue.enqueue_retry_batch(
        (BackgroundRetryItem(1, snap("10.0.0.2:2302", name="Available"), runtime()),),
        (BackgroundRetrySetupFailure(
            0, "10.0.0.1:2302", "Missing", "server disappeared",
        ),),
    )
    assert transition.dispatch.display_name == "Available"
    missing = queue.record_for("10.0.0.1:2302")
    assert missing.state is BackgroundServerState.FAILED
    assert "disappeared" in missing.error
    done, _ = attach_and_return(queue, transition.dispatch, READY)
    assert [entry.display_name for entry in done.snapshot.completed_batch.entries] == [
        "Missing", "Available",
    ]


def test_identity_state_survives_replacement_objects_and_frozen_mods_do_not_mutate():
    queue = BackgroundPreparationQueue()
    original_mods = ((1, "Original"),)
    transition = queue.enqueue(
        snap("10.0.0.1:2302", original_mods, "Original Name"), runtime(),
    )
    replacement = snap(
        "10.0.0.1:2302", ((1, "Original"), (2, "New")), "Replacement Name",
    )
    assert replacement.identity == transition.request.identity
    assert queue.record_for(replacement.identity).state is BackgroundServerState.PREPARING
    assert transition.request.snapshot.required_mods == original_mods
    assert transition.request.display_name == "Original Name"


def test_shutdown_clears_pending_cancels_only_active_and_invalidates_callbacks():
    queue = BackgroundPreparationQueue()
    a = queue.enqueue(snap("10.0.0.1:2302"), runtime())
    b = queue.enqueue(snap("10.0.0.2:2302"), runtime())
    controller = Controller()
    assert queue.attach_controller(a.dispatch, controller)
    stopped = queue.shutdown()
    assert stopped.controller_to_cancel is controller
    stopped.controller_to_cancel.cancel()
    assert controller.cancel_calls == 1
    assert stopped.snapshot.pending == ()
    assert not stopped.snapshot.accepting
    assert stopped.snapshot.shutdown
    assert queue.record_for(b.request.identity).state is BackgroundServerState.IDLE
    assert not queue.is_current(a.dispatch)
    finished = queue.finish(a.dispatch, CANCELLED)
    assert finished.accepted
    assert finished.dispatch is None
    assert queue.snapshot().completed_batch is None
