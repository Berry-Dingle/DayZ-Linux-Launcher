from pathlib import Path
from types import SimpleNamespace

from dzll_launcher import window as window_module
from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
)
from dzll_launcher.background_prepare_queue import (
    BackgroundPreparationQueue,
    BackgroundServerState,
)
from dzll_launcher.preparation_contracts import PreparationOutcome, PreparationStatus
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
COLUMN_SOURCE = (ROOT / "src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
STYLE_SOURCE = (ROOT / "src/dzll_launcher/styles.py").read_text(encoding="utf-8")
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")


class Widget:
    def __init__(self):
        self.text = ""
        self.visible = False
        self.sensitive = True
        self.tooltip = None

    def set_text(self, value):
        self.text = str(value)

    def set_label(self, value):
        self.text = str(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_tooltip_text(self, value):
        self.tooltip = value

    def set_fraction(self, _value):
        return None


class Controller:
    def __init__(self):
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        return True


def runtime():
    return BackgroundPreparationRuntime(
        "/workshop", "", "", False, False, True,
        "steam_client", True, False,
    )


def snap(obj, name=None):
    return BackgroundServerPreparationSnapshot(
        obj.ip, obj.gport, obj.qport, name or obj.name, ((101, "One"),),
    )


def row(ip, name):
    return ServerObject(ip=ip, gport=2302, qport=27016, name=name, mods_json="[]")


def state_host(queue):
    return SimpleNamespace(
        _background_prepare_queue=queue,
        _join_attempts=SimpleNamespace(active=None),
    )


def summary_host(queue):
    host = state_host(queue)
    host._background_prepare_active = False
    host._background_prepare_pulse_id = 0
    host._background_prepare_controller = None
    host.background_prepare_server_label = Widget()
    host.background_prepare_detail_label = Widget()
    host.background_prepare_count_label = Widget()
    host.background_prepare_progress = Widget()
    host.background_prepare_failed_label = Widget()
    host.background_prepare_retry_btn = Widget()
    host.background_prepare_action_btn = Widget()
    host.background_prepare_status = Widget()
    host._background_prepare_stop_pulse = lambda: None
    return host


def test_download_sensitivity_is_per_identity_and_cancel_disables_all():
    queue = BackgroundPreparationQueue()
    a, b, c = row("10.0.0.1", "A"), row("10.0.0.2", "B"), row("10.0.0.3", "C")
    active = queue.enqueue(snap(a), runtime())
    queued = queue.enqueue(snap(b), runtime())
    host = state_host(queue)
    assert not DZLLWindow._background_prepare_download_available(host, a)
    assert not DZLLWindow._background_prepare_download_available(host, b)
    assert DZLLWindow._background_prepare_download_available(host, c)
    controller = Controller()
    assert queue.attach_controller(active.dispatch, controller)
    cancelling = queue.cancel_all()
    assert cancelling.snapshot.cancelling
    assert not DZLLWindow._background_prepare_download_available(host, c)
    assert queue.record_for(queued.request.identity).state is BackgroundServerState.IDLE


def test_blocked_join_presentation_is_exact_and_returns_to_normal_when_idle():
    queue = BackgroundPreparationQueue()
    a = row("10.0.0.1", "A")
    transition = queue.enqueue(snap(a), runtime())
    host = state_host(queue)
    assert not DZLLWindow._background_prepare_join_available(host, a)
    assert DZLLWindow._background_prepare_join_presentation(host, a) == {
        "icon_name": "action-unavailable-symbolic",
        "tooltip": "Join unavailable while background\nmod preparation is running",
        "css_class": "dzll-join-blocked",
    }
    controller = Controller()
    assert queue.attach_controller(transition.dispatch, controller)
    queue.finish(transition.dispatch, PreparationOutcome(PreparationStatus.READY))
    assert DZLLWindow._background_prepare_join_available(host, a)
    assert DZLLWindow._background_prepare_join_presentation(host, a) == {}


def test_download_row_presentations_derive_from_central_identity_records():
    queue = BackgroundPreparationQueue()
    a, b = row("10.0.0.1", "A"), row("10.0.0.2", "B")
    active = queue.enqueue(snap(a), runtime())
    queue.enqueue(snap(b), runtime())
    host = state_host(queue)
    assert DZLLWindow._background_prepare_download_presentation(host, a)["icon_name"] == (
        "emblem-synchronizing-symbolic"
    )
    assert DZLLWindow._background_prepare_download_presentation(host, b)["icon_name"] == (
        "appointment-soon-symbolic"
    )
    replacement_a = row("10.0.0.1", "Replacement A")
    assert DZLLWindow._background_prepare_download_presentation(
        host, replacement_a,
    )["icon_name"] == "emblem-synchronizing-symbolic"
    controller = Controller()
    assert queue.attach_controller(active.dispatch, controller)
    queue.finish(active.dispatch, PreparationOutcome(
        PreparationStatus.FAILED, error="failed",
    ))
    assert DZLLWindow._background_prepare_download_presentation(
        host, replacement_a,
    )["icon_name"] == "dialog-error-symbolic"


def test_final_summary_counts_order_full_tooltip_and_close_reset():
    queue = BackgroundPreparationQueue()
    a, b, c = row("10.0.0.1", "A very long failed server name"), row(
        "10.0.0.2", "Ready Server",
    ), row("10.0.0.3", "Second failed server")
    first = queue.enqueue(snap(a), runtime())
    queue.enqueue(snap(b), runtime())
    queue.enqueue(snap(c), runtime())
    controller = Controller()
    queue.attach_controller(first.dispatch, controller)
    second = queue.finish(first.dispatch, PreparationOutcome(
        PreparationStatus.FAILED, error="first reason",
    )).dispatch
    queue.attach_controller(second, Controller())
    third = queue.finish(second, PreparationOutcome(PreparationStatus.READY)).dispatch
    queue.attach_controller(third, Controller())
    done = queue.finish(third, PreparationOutcome(
        PreparationStatus.FAILED, error="second reason",
    ))
    host = summary_host(queue)
    DZLLWindow._background_prepare_render_batch_summary(
        host, done.snapshot.completed_batch,
    )
    assert host.background_prepare_server_label.text == (
        "Background mod preparation finished"
    )
    assert host.background_prepare_detail_label.text == "1 ready · 2 failed"
    assert host.background_prepare_failed_label.text == (
        "Failed servers: A very long failed server name, Second failed server"
    )
    assert host.background_prepare_failed_label.tooltip == (
        "A very long failed server name: first reason\n"
        "Second failed server: second reason"
    )
    assert host.background_prepare_retry_btn.visible
    queue.clear_completed_batch()
    assert queue.record_for(first.request.identity).state is BackgroundServerState.IDLE
    assert queue.record_for(second.identity).state is BackgroundServerState.IDLE
    assert queue.record_for(third.identity).state is BackgroundServerState.IDLE


def test_retry_failed_window_flow_resolves_fresh_objects_and_reports_missing():
    queue = BackgroundPreparationQueue()
    old_a, old_b = row("10.0.0.1", "Old A"), row("10.0.0.2", "Old B")
    first = queue.enqueue(snap(old_a), runtime())
    queue.enqueue(snap(old_b), runtime())
    queue.attach_controller(first.dispatch, Controller())
    second = queue.finish(first.dispatch, PreparationOutcome(
        PreparationStatus.FAILED, error="A failed",
    )).dispatch
    queue.attach_controller(second, Controller())
    queue.finish(second, PreparationOutcome(
        PreparationStatus.FAILED, error="B failed",
    ))
    fresh_a = row("10.0.0.1", "Fresh A")
    started = []
    applied = []
    host = SimpleNamespace(
        _background_prepare_queue=queue,
        _obj_by_key={"10.0.0.1:2302": fresh_a},
        _background_prepare_ui_generation=5,
        _background_prepare_resolve_frozen=lambda obj: (
            BackgroundServerPreparationSnapshot(
                obj.ip, obj.gport, obj.qport, obj.name, ((999, "Fresh Mod"),),
            ),
            runtime(),
        ),
        _background_prepare_apply_queue_snapshot=applied.append,
        _background_prepare_start_frozen_request=started.append,
    )
    DZLLWindow._background_prepare_retry_failed_clicked(host, None)
    assert len(started) == 1
    assert started[0].snapshot.name == "Fresh A"
    assert started[0].snapshot.required_mods == ((999, "Fresh Mod"),)
    retry_snapshot = queue.snapshot()
    assert retry_snapshot.active_batch_id != 0
    missing = queue.record_for("10.0.0.2:2302")
    assert missing.state is BackgroundServerState.FAILED
    assert "no longer available" in missing.error


def test_column_factory_and_css_implement_narrow_blocked_join_state():
    assert "presentation=join_presentation" in COLUMN_SOURCE
    assert '"action-unavailable-symbolic"' in WINDOW_SOURCE
    assert '"Join unavailable while background\\nmod preparation is running"' in WINDOW_SOURCE
    assert '"dzll-join-blocked"' in WINDOW_SOURCE
    assert ".dzll-join-blocked" in STYLE_SOURCE
    assert "#ff5c5c" in STYLE_SOURCE


def test_summary_label_uses_ellipsis_without_mutating_stored_names():
    builder = WINDOW_SOURCE.split("def _build_background_prepare_status_block", 1)[1].split(
        "def _background_prepare_download_available", 1
    )[0]
    assert "background_prepare_failed_label.set_ellipsize(Pango.EllipsizeMode.END)" in builder
    assert "set_tooltip_text" in WINDOW_SOURCE
