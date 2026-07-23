from pathlib import Path

from dzll_launcher.background_prepare_browser import (
    BrowserPreparationPresenter,
)
from dzll_launcher.preparation_presentation import (
    PreparationPresentationReducer,
    PreparationProgressMode,
)
from dzll_launcher.preparation_contracts import (
    PreparationEventKind,
    PreparationOutcome,
    PreparationProgressEvent,
    PreparationStatus,
)


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
COLUMN_SOURCE = (ROOT / "src/dzll_launcher/column_view.py").read_text(encoding="utf-8")
BROWSER_SOURCE = (
    ROOT / "src/dzll_launcher/background_prepare_browser.py"
).read_text(encoding="utf-8")
CALLER_SOURCE = (
    ROOT / "src/dzll_launcher/background_prepare.py"
).read_text(encoding="utf-8")
QUEUE_SOURCE = (
    ROOT / "src/dzll_launcher/background_prepare_queue.py"
).read_text(encoding="utf-8")


def raw(payload):
    return PreparationProgressEvent.from_authoritative_payload(
        {"join_attempt_id": 1, "backend": "steam_ugc",
         "backend_owner": "steam_client"} | payload
    )


def reducer():
    return PreparationPresentationReducer(operation_id=1, is_current=lambda value: value == 1)


def activate(target, *, mid=123, name="Exact Mod Name", index=11, total=11,
             downloaded=25, total_bytes=100):
    target.apply(raw({"type": "presentation_work_set", "work_ids": [mid, 456]}))
    target.apply(raw({"type": "session", "id": mid, "name": name,
                      "event_source": "initial", "installed": False,
                      "download_bytes": 0, "total_bytes": total_bytes,
                      "index": index, "total": total}))
    target.apply(raw({"type": "session", "id": mid, "name": name,
                      "event_source": "request", "installed": False,
                      "request_attempted": True, "request_accepted": True,
                      "was_installed_before": False, "download_bytes": 0,
                      "total_bytes": total_bytes, "index": index, "total": total}))
    return target.apply(raw({"type": "session", "id": mid, "name": name,
                             "event_source": "poll", "installed": False,
                             "request_attempted": True, "request_accepted": True,
                             "was_installed_before": False,
                             "download_bytes": downloaded,
                             "total_bytes": total_bytes,
                             "index": index, "total": total})).snapshot


def test_authoritative_item_identity_name_fraction_and_count_pass_through():
    snapshot = activate(reducer(), downloaded=25, total_bytes=100)
    assert snapshot.current_item_id == 123
    assert snapshot.current_mod_name == "Exact Mod Name"
    assert (snapshot.current, snapshot.total) == (1, 2)
    assert snapshot.fraction == 0.25
    assert snapshot.progress_mode is PreparationProgressMode.DETERMINATE


def test_item_identity_is_used_unchanged_when_name_is_absent():
    snapshot = activate(reducer(), mid=987654, name="")
    assert snapshot.current_mod_name == "987654"


def test_stage_text_is_authoritative_and_absent_count_is_not_inferred():
    snapshot = reducer().apply(raw({
        "type": "preflight", "message": "Waiting for native Steam", "total": 9,
    })).snapshot
    assert snapshot.stage_text == "Waiting for Steam…"
    assert (snapshot.current, snapshot.total) == (0, 0)
    assert snapshot.fraction is None


def test_indeterminate_and_later_determinate_values_remain_separate():
    target = reducer()
    pulse = activate(target, mid=1, downloaded=1, total_bytes=0)
    assert pulse.progress_mode is PreparationProgressMode.INDETERMINATE
    determinate = target.apply(raw({
        "type": "session", "id": 1, "name": "Exact Mod Name",
        "event_source": "poll", "installed": False,
        "request_attempted": True, "request_accepted": True,
        "was_installed_before": False, "download_bytes": 81, "total_bytes": 100,
    })).snapshot
    assert determinate.progress_mode is PreparationProgressMode.DETERMINATE
    assert determinate.fraction == 0.81


def test_browser_presenter_marshals_values_in_order_and_terminal_once():
    scheduled = []
    rendered = []
    current_generation = 3
    presenter = BrowserPreparationPresenter(
        generation=3,
        schedule=scheduled.append,
        is_current=lambda generation: generation == current_generation,
        reducer_factory=lambda _operation_id: reducer(),
        render_snapshot=lambda value: rendered.append(("event", value)),
        render_cancelling=lambda value: rendered.append(("cancelling", value)),
        render_terminal=lambda value: rendered.append(("terminal", value)),
    )
    first = raw({"type": "preflight", "message": "Waiting for Steam"})
    second = raw({"type": "status", "message": "Checking mods"})
    outcome = PreparationOutcome(PreparationStatus.READY)
    presenter.on_event(first)
    presenter.on_event(second)
    presenter.on_terminal(outcome)
    presenter.on_terminal(PreparationOutcome(PreparationStatus.FAILED))
    assert rendered == []
    for callback in scheduled:
        callback()
    assert rendered[0][0] == "event"
    assert rendered[-1] == ("terminal", outcome)


def test_stale_generation_drops_event_and_terminal_callbacks():
    scheduled = []
    rendered = []
    current = {"generation": 1}
    presenter = BrowserPreparationPresenter(
        generation=1,
        schedule=scheduled.append,
        is_current=lambda generation: generation == current["generation"],
        reducer_factory=lambda _operation_id: reducer(),
        render_snapshot=rendered.append,
        render_cancelling=rendered.append,
        render_terminal=rendered.append,
    )
    presenter.on_event(raw({"type": "status", "message": "Checking"}))
    presenter.on_terminal(PreparationOutcome(PreparationStatus.CANCELLED))
    current["generation"] = 2
    for callback in scheduled:
        callback()
    assert rendered == []


def test_presenter_has_no_preparation_decision_or_progress_calculation_surface():
    forbidden = (
        "poll", "query_ugc_state", "select_item", "classify", "schedule_mod",
        "retry", "ready_count", "ordinal", "downloaded_bytes / total_bytes",
    )
    for text in forbidden:
        assert text not in BROWSER_SOURCE


def test_status_block_is_between_search_area_and_complete_main_shell():
    section = WINDOW_SOURCE.split(
        "browser_stack = Gtk.Box", 1
    )[1].split("main = Gtk.Box", 1)[0]
    assert section.index("build_search_area(self)") < section.index(
        "background_prepare_status"
    ) < section.index("main_shell = Gtk.Overlay()")
    assert "get_first_child" not in section
    assert "header" not in section


def test_row_action_order_tooltip_snapshot_and_background_caller_wiring():
    build = COLUMN_SOURCE.split("def build_server_column_view", 1)[1]
    assert build.index("monitor_factory =") < build.index("download_factory =")
    assert build.index("download_factory =") < build.index("join_factory =")
    assert "Subscribe to and download required mods\\nwithout joining" in build
    assert "Gtk.AccessibleProperty.LABEL" in COLUMN_SOURCE
    handler = WINDOW_SOURCE.split("def _background_prepare_for_obj", 1)[1].split(
        "def _background_prepare_apply_queue_snapshot", 1
    )[0]
    assert handler.index("_background_prepare_resolve_frozen(obj)") < handler.index(
        "_background_prepare_queue.enqueue(snapshot, runtime)"
    ) < handler.index("_background_prepare_start_frozen_request")
    starter = WINDOW_SOURCE.split("def _background_prepare_start_frozen_request", 1)[1].split(
        "def _background_prepare_render_request_snapshot", 1
    )[0]
    assert "prepare_server_mods_without_joining(" in starter
    assert starter.index("queue.attach_controller") < starter.index(
        "_hi_executor.submit(worker)"
    )
    assert "_join_server_for_obj(" not in handler


def test_sensitivity_cancel_terminal_close_and_fifo_ownership_are_explicit():
    assert "refresh_download_mods_states" in WINDOW_SOURCE
    assert "refresh_join_states" in WINDOW_SOURCE
    assert "_background_prepare_cancel_requested" in WINDOW_SOURCE
    assert 'set_label("Cancelling…")' in WINDOW_SOURCE
    assert "_background_prepare_terminal_handled" in WINDOW_SOURCE
    assert 'set_label("Close")' in WINDOW_SOURCE
    assert "background_prepare_status.set_visible(False)" in WINDOW_SOURCE
    assert "deque" not in CALLER_SOURCE
    assert "queue" not in CALLER_SOURCE.lower()
    assert "class BackgroundPreparationQueue" in QUEUE_SOURCE
    assert "deque" in QUEUE_SOURCE
    assert "Gtk" not in QUEUE_SOURCE and "GLib" not in QUEUE_SOURCE
    assert "queue.finish(request, outcome)" in WINDOW_SOURCE


def test_background_browser_path_contains_no_join_continuation_or_state_machine():
    combined = BROWSER_SOURCE + CALLER_SOURCE
    forbidden = (
        "ensure_watch_symlinks", "bootstrap_launcher_state",
        "_launch_direct_steam_url", "query_ugc_state", "ugc_item_ready",
        "run_steam_client_install", "run_steamcmd_install",
        "set_server_companion_server", "set_installing_mods",
    )
    for text in forbidden:
        assert text not in combined
