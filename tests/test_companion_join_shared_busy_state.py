import inspect
from types import SimpleNamespace

from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
    preparation_operation_gate,
)
from dzll_launcher.background_prepare_queue import BackgroundPreparationQueue
from dzll_launcher.preparation_contracts import PreparationOutcome, PreparationStatus
from dzll_launcher.server_companion_ui import ServerCompanionPanel
from dzll_launcher.join_preparation_busy import (
    shared_join_preparation_busy,
    shared_join_preparation_state,
)
from dzll_launcher.steam_ugc_backend import UGCHelperReapError
from dzll_launcher.window import DZLLWindow


class Button:
    def __init__(self):
        self.sensitive = True

    def set_sensitive(self, value):
        self.sensitive = bool(value)


class Controller:
    def cancel(self):
        return True


def _runtime():
    return BackgroundPreparationRuntime(
        "/workshop", "", "", False, False, True,
        "steam_client", True, False,
    )


def _snapshot(name="Busy Server"):
    return BackgroundServerPreparationSnapshot(
        "10.0.0.1", 2302, 27016, name, ((101, "One"),),
    )


def _host(*, join_active=None):
    return SimpleNamespace(
        _join_attempts=SimpleNamespace(active=join_active),
        _background_prepare_queue=BackgroundPreparationQueue(),
        list_view=None,
    )


def _panel(host, *, server=True):
    panel = SimpleNamespace(
        _snapshot={"name": "Server"} if server else None,
        _join_sensitivity_resolver=None,
        join_btn=Button(),
    )
    panel.refresh_join_sensitivity = lambda: (
        ServerCompanionPanel.refresh_join_sensitivity(panel)
    )
    host.server_companion_panel = panel
    host._refresh_server_companion_join_sensitivity = lambda: (
        DZLLWindow._refresh_server_companion_join_sensitivity(host)
    )
    ServerCompanionPanel.set_join_sensitivity_resolver(
        panel,
        lambda joinable: DZLLWindow._server_companion_join_sensitive(
            host, joinable,
        ),
    )
    return panel


def test_foreground_join_ownership_disables_companion_until_release():
    host = _host(join_active=SimpleNamespace(attempt_id=7))
    panel = _panel(host)
    assert shared_join_preparation_busy(host)
    assert panel.join_btn.sensitive is False

    host._join_attempts.active = None
    DZLLWindow._refresh_background_prepare_action_states(host)
    assert not shared_join_preparation_busy(host)
    assert panel.join_btn.sensitive is True


def test_opening_companion_during_background_gate_ownership_starts_disabled():
    host = _host()
    gate = preparation_operation_gate(host)
    lease = gate.try_acquire("background")
    assert lease is not None
    panel = _panel(host)
    assert panel.join_btn.sensitive is False
    assert gate.release(lease)
    panel.refresh_join_sensitivity()
    assert panel.join_btn.sensitive is True


def test_foreground_join_and_mod_repair_share_one_gate_owner():
    host = _host(join_active=SimpleNamespace(attempt_id=7))
    gate = preparation_operation_gate(host)
    join_lease = gate.try_acquire("foreground_join")
    assert join_lease is not None
    assert gate.try_acquire("mod_repair") is None

    assert gate.release(join_lease)
    host._join_attempts.active = None
    repair_lease = gate.try_acquire("mod_repair")
    assert repair_lease is not None
    assert gate.try_acquire("foreground_join") is None
    assert gate.release(repair_lease)


def test_shared_busy_distinguishes_unresolved_reap_block_from_active_work():
    host = _host()
    gate = preparation_operation_gate(host)
    assert gate.block_reap_failure(
        "foreground_join", UGCHelperReapError("helper unresolved")
    )
    panel = _panel(host)
    assert shared_join_preparation_state(host) == "blocked_reap_failure"
    assert shared_join_preparation_busy(host)
    assert panel.join_btn.sensitive is False


def test_background_queue_start_cancel_cleanup_controls_same_busy_state():
    host = _host()
    panel = _panel(host)
    transition = host._background_prepare_queue.enqueue(_snapshot(), _runtime())
    DZLLWindow._refresh_background_prepare_action_states(host)
    assert transition.accepted
    assert panel.join_btn.sensitive is False

    controller = Controller()
    assert host._background_prepare_queue.attach_controller(
        transition.dispatch, controller,
    )
    cancelled = host._background_prepare_queue.cancel_all()
    assert cancelled.snapshot.cancelling
    DZLLWindow._refresh_background_prepare_action_states(host)
    assert panel.join_btn.sensitive is False

    host._background_prepare_queue.finish(
        transition.dispatch,
        PreparationOutcome(PreparationStatus.CANCELLED, reason="cancelled"),
    )
    DZLLWindow._refresh_background_prepare_action_states(host)
    assert panel.join_btn.sensitive is True


def test_background_failure_and_protocol_cleanup_restore_after_release():
    for reason in ("download_failed", "ugc_helper_protocol_failure"):
        host = _host()
        panel = _panel(host)
        transition = host._background_prepare_queue.enqueue(_snapshot(reason), _runtime())
        assert host._background_prepare_queue.attach_controller(
            transition.dispatch, Controller(),
        )
        DZLLWindow._refresh_background_prepare_action_states(host)
        assert panel.join_btn.sensitive is False
        host._background_prepare_queue.finish(
            transition.dispatch,
            PreparationOutcome(
                PreparationStatus.FAILED, reason=reason, error=reason,
            ),
        )
        DZLLWindow._refresh_background_prepare_action_states(host)
        assert panel.join_btn.sensitive is True


def test_server_change_hide_reopen_and_dock_do_not_bypass_global_busy():
    host = _host(join_active=SimpleNamespace(attempt_id=9))
    panel = _panel(host)
    assert panel.join_btn.sensitive is False
    for identity in ("server-b", "hidden", "undocked", "redocked", "reopened"):
        panel._snapshot = {"name": identity}
        panel.refresh_join_sensitivity()
        assert panel.join_btn.sensitive is False


def test_existing_unavailable_rule_combines_with_shared_busy_state():
    host = _host()
    panel = _panel(host, server=False)
    assert panel.join_btn.sensitive is False
    host._join_attempts.active = SimpleNamespace(attempt_id=11)
    panel.refresh_join_sensitivity()
    assert panel.join_btn.sensitive is False
    host._join_attempts.active = None
    panel.refresh_join_sensitivity()
    assert panel.join_btn.sensitive is False


def test_no_download_immediate_join_does_not_leave_companion_stuck():
    host = _host(join_active=SimpleNamespace(attempt_id=12, phase="checking"))
    panel = _panel(host)
    assert panel.join_btn.sensitive is False
    host._join_attempts.active.phase = "watching"
    panel.refresh_join_sensitivity()
    assert panel.join_btn.sensitive is False
    host._join_attempts.active = None
    DZLLWindow._refresh_background_prepare_action_states(host)
    assert panel.join_btn.sensitive is True


def test_main_join_and_companion_use_one_authoritative_busy_predicate():
    join_source = inspect.getsource(DZLLWindow._join_server_for_obj)
    browser_source = inspect.getsource(DZLLWindow._background_prepare_join_available)
    companion_source = inspect.getsource(DZLLWindow._server_companion_join_sensitive)
    assert "shared_join_preparation_busy(self)" in join_source
    assert "shared_join_preparation_busy(self)" in browser_source
    assert "shared_join_preparation_busy(self)" in companion_source
    assert "_companion" not in inspect.getsource(shared_join_preparation_busy)
