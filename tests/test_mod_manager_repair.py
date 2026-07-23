from pathlib import Path
from types import SimpleNamespace

from dzll_launcher.background_prepare import preparation_operation_gate
from dzll_launcher import mods_ui
from dzll_launcher.mods_ui import ModsManagerOverlay
from dzll_launcher.steam_ugc_backend import repair_ugc_item


def _clock():
    value = {"now": 0.0}

    def monotonic():
        return value["now"]

    def sleep(seconds):
        value["now"] += float(seconds)

    return monotonic, sleep


def _ready_state(**extra):
    state = {
        "subscribed": True,
        "installed": True,
        "needs_update": False,
        "downloading": False,
        "download_pending": False,
        "folder": True,
    }
    state.update(extra)
    return state


def _run_repair_with_states(states, *, request_ok=True, install_ok=True, events=None):
    sequence = list(states)
    calls = []
    monotonic, sleep = _clock()

    def request(ids, **_kwargs):
        calls.append(("unsubscribe", list(ids)))
        return request_ok, {}

    def query(ids, **_kwargs):
        state = sequence.pop(0) if sequence else {}
        calls.append(("query", list(ids), dict(state)))
        return bool(state), {int(ids[0]): state} if state else {}

    def install(ids, **kwargs):
        calls.append(("install", list(ids)))
        for event in events or []:
            kwargs["progress_cb"](dict(event))
        return install_ok

    result = repair_ugc_item(
        123,
        request_unsubscribe_fn=request,
        query_state_fn=query,
        install_fn=install,
        content_exists_fn=lambda _mid, state: bool(state.get("folder", False)),
        monotonic_fn=monotonic,
        sleep_fn=sleep,
        removal_timeout=2,
        poll_interval=1,
        progress_cb=lambda event: calls.append(("progress", dict(event))),
    )
    return result, calls


def test_repair_exact_sequence_and_progress_without_direct_delete():
    result, calls = _run_repair_with_states(
        [
            {"subscribed": False, "installed": True, "folder": True},
            {"subscribed": False, "installed": False, "folder": False},
            _ready_state(),
        ],
        events=[
            {"download_bytes": 0, "total_bytes": 100},
            {"download_bytes": 37, "total_bytes": 100},
        ],
    )
    assert result["ok"]
    operation_names = [item[0] for item in calls if item[0] in {"unsubscribe", "query", "install"}]
    assert operation_names == ["unsubscribe", "query", "query", "install", "query"]
    progress = [item[1] for item in calls if item[0] == "progress"]
    assert any(event.get("stage") == "waiting_removal" for event in progress)
    assert any(event.get("download_bytes") == 37 for event in progress)

    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "steam_ugc_backend.py"
    body = source.read_text(encoding="utf-8").split("def repair_ugc_item(", 1)[1].split("def _native_steam_roots", 1)[0]
    assert "_delete_dir_if_present" not in body
    assert "shutil.rmtree" not in body
    assert "unlink(" not in body


def test_repair_unsubscribe_failure():
    result, _calls = _run_repair_with_states([], request_ok=False)
    assert not result["ok"]
    assert result["reason"] == "unsubscribe_failed"


def test_repair_removal_timeout():
    result, _calls = _run_repair_with_states(
        [{"subscribed": False, "installed": True, "folder": True}] * 4
    )
    assert not result["ok"]
    assert result["reason"] == "removal_timeout"
    assert result["not_installed"]


def test_repair_subscribe_failure():
    result, _calls = _run_repair_with_states(
        [
            {"subscribed": False, "installed": False, "folder": False},
            _ready_state(subscribed=False, installed=False, folder=False),
        ],
        install_ok=False,
    )
    assert not result["ok"]
    assert result["reason"] == "subscribe_failed"


def test_repair_download_failure_and_terminal_unresolved():
    failed, _calls = _run_repair_with_states(
        [
            {"subscribed": False, "installed": False, "folder": False},
            _ready_state(installed=False, needs_update=True, folder=False),
        ],
        install_ok=False,
    )
    assert failed["reason"] == "download_failed"

    unresolved, _calls = _run_repair_with_states(
        [{"subscribed": False, "installed": False, "folder": False}, {}],
    )
    assert unresolved["reason"] == "terminal_unresolved"


def test_repair_rejects_missing_directory_after_ready():
    result, _calls = _run_repair_with_states(
        [
            {"subscribed": False, "installed": False, "folder": False},
            _ready_state(folder=False),
        ],
    )
    assert not result["ok"]
    assert result["reason"] == "missing_directory"


def _overlay_stub():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    owner = SimpleNamespace()
    overlay.host = SimpleNamespace(_win=owner)
    overlay._mod_operation_running = False
    overlay._mod_operation_pending = False
    overlay._batch_unsubscribe_running = False
    overlay._dayz_running_for_repair = lambda: False
    overlay._steam_management_is_verified = lambda: True
    overlay._update_batch_action_buttons = lambda: None
    overlay._set_mod_operation_status = lambda *args, **kwargs: None
    overlay._show_start_steam_manage_prompt = lambda: None
    overlay.started = []
    overlay._start_repair = lambda mid: overlay.started.append(mid)
    overlay.confirm = {}

    def confirm_show(*args, **kwargs):
        overlay.confirm["callback"] = args[3]

    overlay._confirm_show = confirm_show
    return overlay, owner


def test_repair_confirmation_cancelled_and_accepted():
    overlay, _owner = _overlay_stub()
    overlay._on_repair_clicked(123)
    overlay.confirm["callback"](False)
    assert overlay.started == []
    assert not overlay._mod_operation_pending

    overlay._on_repair_clicked(123)
    overlay.confirm["callback"](True)
    assert overlay.started == [123]
    overlay._release_repair_lease()


def test_repair_rejects_dayz_and_respects_one_global_owner():
    overlay, owner = _overlay_stub()
    overlay._dayz_running_for_repair = lambda: True
    overlay._on_repair_clicked(123)
    assert overlay.started == []

    overlay, owner = _overlay_stub()
    lease = preparation_operation_gate(owner).try_acquire("join")
    overlay._on_repair_clicked(123)
    overlay.confirm["callback"](True)
    assert overlay.started == []
    assert preparation_operation_gate(owner).release(lease)


def test_repair_progress_spinner_percentage_and_identity_guard():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._repair_state_by_id = {123: {"generation": 7, "phase": "spinner", "fraction": 0.0}}
    overlay._repair_ui_attached = True
    applied = []
    overlay._current_row_for_mod = lambda mid: f"row-{mid}"
    overlay._apply_repair_state_to_row = lambda row, state: applied.append((row, dict(state)))

    overlay._on_repair_progress(123, 6, {"download_bytes": 50, "total_bytes": 100})
    assert applied == []
    overlay._on_repair_progress(123, 7, {"download_bytes": 0, "total_bytes": 100})
    assert applied[-1][1]["phase"] == "spinner"
    overlay._on_repair_progress(123, 7, {"download_bytes": 37, "total_bytes": 100})
    assert applied[-1][1]["phase"] == "percent"
    assert applied[-1][1]["fraction"] == 0.37
    assert applied[-1][1]["percent"] == 37


def test_repair_progress_is_monotonic_and_control_events_do_not_reset_spinner():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._repair_state_by_id = {123: {"generation": 7, "phase": "spinner", "fraction": 0.0}}
    overlay._repair_ui_attached = False

    overlay._on_repair_progress(123, 7, {"download_bytes": 60, "total_bytes": 100})
    state = overlay._repair_state_by_id[123]
    assert state["phase"] == "percent"
    assert state["percent"] == 60
    assert state["fraction"] == 0.6

    overlay._on_repair_progress(123, 7, {"type": "helper_event", "event": {"type": "done"}})
    assert state["phase"] == "percent"
    assert state["percent"] == 60

    overlay._on_repair_progress(123, 7, {"download_bytes": 40, "total_bytes": 100})
    assert state["percent"] == 60
    assert state["fraction"] == 0.6


def test_repair_progress_normalizes_nested_bytes_and_malformed_totals():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._repair_state_by_id = {123: {"generation": 7, "phase": "spinner", "fraction": 0.0}}
    overlay._repair_ui_attached = False
    state = overlay._repair_state_by_id[123]

    for event in (
        {"download_bytes": 20, "total_bytes": 0},
        {"download_bytes": 20, "total_bytes": None},
        {"download_bytes": "bad", "total_bytes": 100},
        {"download_bytes": 20, "total_bytes": "bad"},
        {"download_bytes": 20, "total_bytes": -1},
    ):
        overlay._on_repair_progress(123, 7, event)
        assert state["phase"] == "spinner"
        assert state["fraction"] == 0.0

    overlay._on_repair_progress(
        123,
        7,
        {"type": "helper_event", "event": {"download_bytes": 25, "total_bytes": 100}},
    )
    assert state["phase"] == "percent"
    assert state["percent"] == 25
    assert state["fraction"] == 0.25


def test_repair_progress_final_success_is_100_percent():
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._repair_state_by_id = {
        123: {"generation": 7, "phase": "percent", "fraction": 0.72, "percent": 72}
    }
    overlay._repair_ui_attached = False
    overlay._on_repair_progress(123, 7, {"stage": "success", "fraction": 1.0})
    state = overlay._repair_state_by_id[123]
    assert state["phase"] == "success"
    assert state["percent"] == 100
    assert state["fraction"] == 1.0

    overlay._repair_state_by_id = {
        124: {"generation": 8, "phase": "spinner", "fraction": 0.0}
    }
    overlay._on_repair_progress(124, 8, {"download_bytes": 200, "total_bytes": 100})
    clamped = overlay._repair_state_by_id[124]
    assert clamped["percent"] == 100
    assert clamped["fraction"] == 1.0


def test_repair_success_and_failure_restore_row(monkeypatch):
    overlay = ModsManagerOverlay.__new__(ModsManagerOverlay)
    overlay._repair_state_by_id = {123: {"generation": 7, "phase": "spinner", "fraction": 0.0}}
    overlay._repair_ui_attached = True
    overlay._repair_lease = None
    overlay._repair_refresh_needed = False
    overlay._set_mod_operation_status = lambda *args, **kwargs: None
    overlay._release_repair_lease = lambda: None
    overlay._current_row_for_mod = lambda _mid: "row"
    applied = []
    overlay._apply_repair_state_to_row = lambda row, state: applied.append((row, state))
    overlay._mod_manager_is_visible = lambda: False
    overlay._confirm_show = lambda *args, **kwargs: None
    scheduled = []
    monkeypatch.setattr(mods_ui.GLib, "timeout_add", lambda delay, cb, *args: scheduled.append((delay, cb, args)) or 1)

    overlay._finish_repair(123, 7, {"ok": True})
    assert overlay._repair_state_by_id[123]["phase"] == "success"
    assert overlay._repair_state_by_id[123]["fraction"] == 1.0
    assert scheduled and scheduled[0][0] == 900

    overlay._repair_state_by_id = {123: {"generation": 8, "phase": "spinner", "fraction": 0.0}}
    overlay._finish_repair(
        123,
        8,
        {"ok": False, "error": "Download failed.", "not_installed": True},
    )
    assert 123 not in overlay._repair_state_by_id
    assert applied[-1] == ("row", None)


def test_repair_close_detaches_and_reopen_defers_refresh():
    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    hide = body.split("    def _hide_now", 1)[1].split("    def _mod_manager_is_visible", 1)[0]
    show = body.split("    def show(", 1)[1].split("    def hide(", 1)[0]
    assert "self._repair_ui_attached = False" in hide
    assert "self._repair_ui_attached = True" in show
    assert "_repair_refresh_needed" in show
    assert "_current_row_for_mod" in body
    assert "get_first_child()" in body


def test_repair_whole_row_progress_css_is_geometry_neutral():
    css = mods_ui.__name__  # retain module import coverage without constructing GTK widgets
    assert css
    styles_source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "styles.py"
    body = styles_source.read_text(encoding="utf-8")
    assert "row.mods-row.mods-repair-active > *" in body
    assert "mods-repair-progress-{percent}" in body
    assert "alpha(@dzll_accent, 0.34)" in body
    assert "border-bottom" not in body.split("repair_progress_rules = []", 1)[1]
    assert "min-height" not in body.split("repair_progress_rules = []", 1)[1]


def test_repair_retry_after_partial_failure():
    first, _calls = _run_repair_with_states([], request_ok=False)
    second, _calls = _run_repair_with_states(
        [
            {"subscribed": False, "installed": False, "folder": False},
            _ready_state(),
        ]
    )
    assert not first["ok"]
    assert second["ok"]


def test_repair_source_preserves_existing_architecture_boundaries():
    source = Path(__file__).resolve().parents[1] / "src" / "dzll_launcher" / "mods_ui.py"
    body = source.read_text(encoding="utf-8")
    assert 'try_acquire("mod_repair")' in body
    assert "join_prepare" not in body
    repair_region = body.split("    def _on_repair_clicked", 1)[1].split("    def _open_workshop_page", 1)[0]
    assert "steamcmd" not in repair_region.casefold()
    assert "delete_ugc_mod" not in repair_region
    assert "_dzll_mod_id" in body
    assert "generation" in repair_region
