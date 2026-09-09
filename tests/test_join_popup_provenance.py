from types import MethodType
import threading

import pytest

from dzll_launcher import steam_client_mods
from dzll_launcher import steam_ugc_backend
from dzll_launcher import window as window_module
from dzll_launcher.join_popup_presentation import (
    JoinPopupActivityTracker,
    JoinPopupPresentationController,
)
from dzll_launcher.steam_ugc_backend import UGCModSession
from dzll_launcher.join_attempt import JoinAttemptTracker


class FakeGLib:
    def __init__(self):
        self.callbacks = []

    def idle_add(self, callback, *args):
        self.callbacks.append((callback, args))
        return len(self.callbacks)

    def drain(self):
        while self.callbacks:
            callback, args = self.callbacks.pop(0)
            callback(*args)


class FakeWidget:
    def __init__(self, name, writes):
        self.name = name
        self.writes = writes
        self.text = ""
        self.fractions = []

    def set_text(self, value):
        self.text = str(value)
        self.writes.append((self.name, "text", self.text))

    def get_text(self):
        return self.text

    def set_visible(self, value):
        self.writes.append((self.name, "visible", bool(value)))

    def set_spinning(self, value):
        self.writes.append((self.name, "spinning", bool(value)))

    def set_show_text(self, value):
        self.writes.append((self.name, "show_text", bool(value)))

    def set_fraction(self, value):
        value = float(value)
        self.fractions.append(value)
        self.writes.append((self.name, "fraction", value))


class RendererHarness:
    def __init__(self, attempt_id=1):
        self.glib = FakeGLib()
        self.current_attempt = attempt_id
        self.writes = []
        self.logs = []
        self._join_attempts = JoinAttemptTracker(log_sink=lambda _line: None)
        self._join_attempts.begin(
            ip="127.0.0.1", game_port=2302, query_port=27016,
            name="Renderer", skip_dayz_launcher=True,
        )
        self._join_attempts.initialize_download_counter(attempt_id, range(1, 16))
        self._steam_client_safe_cancel_requested = False
        self._steam_ugc_active_event = None
        self._mod_download_backend_active = "steam_client"
        self._steamcmd_install_in_progress = True
        self.steamcmd_task_heading = FakeWidget("heading", self.writes)
        self.steamcmd_line1 = FakeWidget("line1", self.writes)
        self.steamcmd_line2 = FakeWidget("line2", self.writes)
        self.steamcmd_spinner = FakeWidget("spinner", self.writes)
        self.steamcmd_prog_bar = FakeWidget("progress", self.writes)
        self.percent_label = FakeWidget("percent", self.writes)
        self._join_popup_item_activity = JoinPopupActivityTracker()
        self._join_popup_presentation = JoinPopupPresentationController(
            schedule=lambda callback: self.glib.idle_add(callback),
            commit=self._commit_join_popup_presentation,
            is_current_attempt=lambda attempt_id: attempt_id == self.current_attempt,
        )

    def _join_popup_attempt_id(self):
        return self.current_attempt

    def _join_attempt_is_active(self, attempt_id):
        return int(attempt_id) == int(self.current_attempt)

    def _join_log(self, attempt_id, event, **fields):
        self.logs.append((attempt_id, event, fields))
        return True

    def _steam_ugc_get_percent_label(self):
        return self.percent_label

    def _steam_ugc_set_layout_active(self, _active):
        return None

    def _steam_ugc_title(self):
        return "Checking/updating the required mods for this server"

    def _steam_ugc_start_progress_timer(self):
        return None

    def _steam_ugc_stop_progress_timer(self):
        return None

    def _set_server_companion_join_status(self, *_args, **_kwargs):
        return None

    @property
    def item_lines(self):
        return [value for name, operation, value in self.writes
                if name == "line2" and operation == "text"
                and (value.startswith("Downloading ") or value.startswith("Updating "))]


for _method_name in (
    "_steam_ugc_progress_from_worker",
    "_steam_ugc_progress_to_overlay",
    "_render_join_preparation_snapshot",
    "_join_popup_request",
    "_commit_join_popup_presentation",
    "_steam_ugc_render_status",
    "_steam_ugc_render_active_text",
    "_steam_ugc_format_size",
    "_join_popup_active_download_text",
    "_join_popup_note_genuine_transfer",
):
    setattr(RendererHarness, _method_name, getattr(window_module.DZLLWindow, _method_name))


def event(mid, *, source="initial", installed=True, needs_update=False,
          downloading=False, pending=False, ready=None, downloaded=0, total_bytes=0,
          attempted=False, accepted=False, attempt_id=1, name=None,
          normalized_missing=False, was_installed=None):
    if ready is None:
        ready = installed and not needs_update and not downloading and not pending
    return {
        "backend": "steam_ugc",
        "backend_owner": "steam_client",
        "join_attempt_id": attempt_id,
        "id": mid,
        "name": name or f"Mod {mid}",
        "event_source": source,
        "installed": installed,
        "needs_update": needs_update,
        "downloading": downloading,
        "download_pending": pending,
        "ready": ready,
        "download_bytes": downloaded,
        "total_bytes": total_bytes,
        "request_attempted": attempted,
        "request_accepted": accepted,
        "was_installed_before": installed if was_installed is None else was_installed,
        "filesystem_normalized_missing": normalized_missing,
        "state_names": [],
        "index": mid,
        "total": 15,
        "completed_count": 0,
    }


def send(harness, payload, *, drain=True):
    old_glib = window_module.GLib
    window_module.GLib = harness.glib
    try:
        harness._steam_ugc_progress_from_worker(payload)
        if drain:
            harness.glib.drain()
    finally:
        window_module.GLib = old_glib


@pytest.mark.parametrize("variant", ["ready", "pending", "downloading", "needs_update"])
def test_fifteen_installed_initial_shapes_render_zero_names(variant):
    harness = RendererHarness()
    for mid in range(1, 16):
        send(harness, event(
            mid,
            pending=variant == "pending",
            downloading=variant == "downloading",
            needs_update=variant == "needs_update",
            ready=variant == "ready",
        ))
    assert harness.item_lines == []


def test_installed_ready_false_without_active_flags_is_silent():
    harness = RendererHarness()
    send(harness, event(1, ready=False))
    assert harness.item_lines == []


def test_installed_size_metadata_changes_are_silent():
    harness = RendererHarness()
    send(harness, event(1, downloaded=0, total_bytes=0))
    send(harness, event(1, source="poll", downloaded=100, total_bytes=100, ready=True))
    assert harness.item_lines == []


def test_filesystem_normalized_missing_is_silent_before_request():
    harness = RendererHarness()
    send(harness, event(1, installed=False, ready=False, normalized_missing=True,
                        was_installed=True))
    send(harness, event(1, source="request", installed=False, ready=False,
                        normalized_missing=True, was_installed=True,
                        attempted=True, accepted=True))
    assert harness.item_lines == []


def test_missing_initial_then_accepted_request_stays_silent():
    harness = RendererHarness()
    send(harness, event(1, installed=False, ready=False, was_installed=False))
    assert harness.item_lines == []
    send(harness, event(1, source="request", installed=False, ready=False,
                        was_installed=False, attempted=True, accepted=True))
    assert harness.item_lines == []


def test_missing_rejected_request_stays_silent():
    harness = RendererHarness()
    send(harness, event(1, installed=False, ready=False, was_installed=False))
    send(harness, event(1, source="request", installed=False, ready=False,
                        was_installed=False, attempted=True, accepted=False))
    assert harness.item_lines == []
    assert any(fields.get("reason") == "download request rejected"
               for _attempt, _message, fields in harness.logs)


def test_outdated_initial_then_accepted_request_stays_silent():
    harness = RendererHarness()
    send(harness, event(1, needs_update=True, ready=False, was_installed=True))
    assert harness.item_lines == []
    send(harness, event(1, source="request", needs_update=True, ready=False,
                        was_installed=True, attempted=True, accepted=True))
    assert harness.item_lines == []


def test_transient_pending_then_ready_without_request_is_silent():
    harness = RendererHarness()
    send(harness, event(1, pending=True, ready=False))
    send(harness, event(1, source="final", ready=True))
    assert harness.item_lines == []


def test_static_nonzero_initial_bytes_are_not_progress_evidence():
    harness = RendererHarness()
    send(harness, event(1, installed=False, ready=False, downloaded=500,
                        total_bytes=1000, was_installed=False))
    assert harness.item_lines == []


@pytest.mark.parametrize("installed,needs_update,expected", [
    (False, False, "Downloading Mod: Mod 1 - 0 MB (1/15)"),
    (True, True, "Downloading Mod: Mod 1 - 0 MB (1/15)"),
])
def test_byte_increase_after_request_keeps_correct_action_and_advances_progress(
        installed, needs_update, expected):
    harness = RendererHarness()
    send(harness, event(1, installed=installed, needs_update=needs_update,
                        ready=False, downloaded=100, total_bytes=1000,
                        was_installed=installed))
    send(harness, event(1, source="request", installed=installed,
                        needs_update=needs_update, ready=False, downloaded=100,
                        total_bytes=1000, attempted=True, accepted=True,
                        was_installed=installed))
    send(harness, event(1, source="poll", installed=installed,
                        needs_update=needs_update, ready=False, downloaded=250,
                        total_bytes=1000, attempted=True, accepted=True,
                        was_installed=installed))
    assert harness.item_lines == [expected]
    assert 0.25 in harness.steamcmd_prog_bar.fractions
    assert any(fields.get("bytes_advanced") is True
               for _attempt, message, fields in harness.logs
               if message == "popup item presentation authorised")


def test_genuine_active_download_display_format_size_and_progress_are_preserved():
    harness = RendererHarness()
    total = 500 * 1024 * 1024
    send(harness, event(1, installed=False, ready=False, downloaded=0,
                        total_bytes=total, was_installed=False, name="Example Mod"))
    send(harness, event(1, source="request", installed=False, ready=False,
                        downloaded=0, total_bytes=total, was_installed=False,
                        attempted=True, accepted=True, name="Example Mod"))
    send(harness, event(1, source="poll", installed=False, ready=False,
                        downloaded=total // 2, total_bytes=total, was_installed=False,
                        attempted=True, accepted=True, name="Example Mod"))
    assert harness.item_lines == [
        "Downloading Mod: Example Mod - 500 MB (1/15)"
    ]
    assert harness.steamcmd_prog_bar.fractions[-1] == pytest.approx(0.5)
    assert harness.percent_label.text == "50%"


def test_genuine_active_mod_switching_preserves_existing_renderer_behavior():
    harness = RendererHarness()
    for mid, name in ((1, "First Mod"), (2, "Second Mod")):
        send(harness, event(mid, installed=False, ready=False, downloaded=0,
                            total_bytes=1000, was_installed=False, name=name))
        send(harness, event(mid, source="request", installed=False, ready=False,
                            downloaded=0, total_bytes=1000, was_installed=False,
                            attempted=True, accepted=True, name=name))
        send(harness, event(mid, source="poll", installed=False, ready=False,
                            downloaded=500, total_bytes=1000, was_installed=False,
                            attempted=True, accepted=True, name=name))
    assert harness.item_lines == [
        "Downloading Mod: First Mod - 0 MB (1/15)",
        "Downloading Mod: Second Mod - 0 MB (2/15)",
    ]


def test_many_accepted_requests_show_only_the_one_item_with_real_byte_activity():
    harness = RendererHarness()
    for mid in range(1, 11):
        send(harness, event(mid, installed=False, ready=False, downloaded=0,
                            total_bytes=1000, was_installed=False))
        send(harness, event(mid, source="request", installed=False, ready=False,
                            downloaded=0, total_bytes=1000, was_installed=False,
                            attempted=True, accepted=True))
    assert harness.item_lines == []
    send(harness, event(7, source="poll", installed=False, ready=False,
                        downloaded=500, total_bytes=1000, was_installed=False,
                        attempted=True, accepted=True, name="Only Active"))
    assert harness.item_lines == [
        "Downloading Mod: Only Active - 0 MB (1/15)"
    ]


def test_reverse_raw_backend_indices_render_fresh_ascending_ordinals():
    harness = RendererHarness()
    for mid in (15, 14, 13):
        send(harness, event(mid, installed=False, ready=False, downloaded=0,
                            total_bytes=1000, was_installed=False))
        send(harness, event(mid, source="request", installed=False, ready=False,
                            downloaded=0, total_bytes=1000, was_installed=False,
                            attempted=True, accepted=True))
        send(harness, event(mid, source="poll", installed=False, ready=False,
                            downloaded=500, total_bytes=1000, was_installed=False,
                            attempted=True, accepted=True))
    assert harness.item_lines == [
        "Downloading Mod: Mod 15 - 0 MB (1/15)",
        "Downloading Mod: Mod 14 - 0 MB (2/15)",
        "Downloading Mod: Mod 13 - 0 MB (3/15)",
    ]


def test_request_provenance_survives_session_and_rich_adapter(monkeypatch):
    raw_initial = {"id": 7, "type": "item", "installed": False,
                   "needs_update": False, "downloading": False,
                   "download_pending": False, "download_bytes": 0,
                   "total_bytes": 100, "state_names": [], "subscribed": False}
    raw_request = dict(raw_initial, type="request", download_requested=True,
                       high_priority=True, subscribe_call_result=1)
    session = UGCModSession(7)
    session.was_installed_before = False
    session.update_from_item(raw_initial, source="initial")
    initial = session.event()
    session.update_from_item(raw_request, source="request")
    request = session.event()
    session.update_from_item(dict(raw_initial, installed=True), source="poll")
    final = session.event()
    captured = []

    def fake_run(_ids, **kwargs):
        kwargs["progress_cb"](initial)
        kwargs["progress_cb"](request)
        kwargs["progress_cb"](final)
        return True

    monkeypatch.setattr(steam_client_mods, "run_ugc_install", fake_run)
    assert steam_client_mods.run_steam_client_install(
        workshop_dir="/fake", mod_ids=[7], progress_cb=captured.append,
    )
    assert captured[0]["event_source"] == "initial"
    assert captured[0]["request_attempted"] is False
    assert captured[1]["event_source"] == "request"
    assert captured[1]["request_attempted"] is True
    assert captured[1]["request_accepted"] is True
    assert captured[1]["backend_owner"] == "steam_client"
    assert captured[1]["id"] == 7
    assert captured[2]["event_source"] == "final"
    assert captured[2]["ready"] is True


def test_run_ugc_install_preserves_initial_request_and_poll_sources(monkeypatch):
    calls = []
    captured = []
    initial = {"type": "item", "id": 7, "subscribed": False, "installed": False,
               "needs_update": False, "downloading": False, "download_pending": False,
               "download_bytes": 0, "total_bytes": 100, "state_names": []}
    request = dict(initial, type="request", download_requested=True,
                   high_priority=True, subscribe_call_result=1)
    poll = dict(initial, installed=True, download_bytes=100, ready=True)

    monkeypatch.setattr(steam_ugc_backend, "_run_ugc_native_steam_preflight",
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(steam_ugc_backend, "_cache_ugc_state",
                        lambda *_args, **_kwargs: None)

    def fake_helper(command, **kwargs):
        calls.append(command)
        callback = kwargs.get("on_event")
        if command == "state":
            callback(dict(initial))
            callback({"type": "done", "ok": True})
        elif command == "subscribe-download":
            callback(dict(initial))
            callback(dict(request))
            callback(dict(poll))
            callback({"type": "done", "ok": True, "installed": [7], "ready": [7]})
        return True, 0

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", fake_helper)
    assert steam_ugc_backend.run_ugc_install([7], progress_cb=captured.append)
    sessions = [value for value in captured if value.get("type") == "session"]
    assert calls == ["state", "subscribe-download"]
    assert [value["event_source"] for value in sessions] == ["initial", "initial", "request", "poll"]
    assert sessions[2]["request_attempted"] is True
    assert sessions[2]["request_accepted"] is True
    assert sessions[3]["request_accepted"] is True


def test_run_ugc_install_hands_off_parent_and_helper_provenance_intersection(
    monkeypatch,
):
    cancel = threading.Event()
    calls = []
    initial = {
        1: {
            "type": "item", "id": 1, "subscribed": False,
            "installed": False, "needs_update": False,
            "downloading": False, "download_pending": False,
            "download_bytes": 0, "total_bytes": 100, "state_names": [],
        },
        2: {
            "type": "item", "id": 2, "subscribed": True,
            "installed": True, "needs_update": True,
            "downloading": True, "download_pending": False,
            "download_bytes": 10, "total_bytes": 100,
            "state_names": ["Subscribed", "Installed", "NeedsUpdate", "Downloading"],
        },
        3: {
            "type": "item", "id": 3, "subscribed": True,
            "installed": True, "needs_update": True,
            "downloading": False, "download_pending": True,
            "download_bytes": 0, "total_bytes": 100,
            "state_names": ["Subscribed", "Installed", "NeedsUpdate", "DownloadPending"],
        },
    }

    monkeypatch.setattr(
        steam_ugc_backend, "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    handoffs = []

    def fake_helper(command, **kwargs):
        calls.append((command, dict(kwargs)))
        callback = kwargs.get("on_event")
        if command == "state":
            for item_id in (1, 2, 3):
                event = dict(initial[item_id])
                callback(event)
            return True, 0
        if command == "subscribe-download":
            assert kwargs["cancel_cleanup_ids"] == [1]
            cancel.set()
            callback({
                "type": "command_result", "ok": False,
                "cancelled": True,
                "cancel_handoff": {
                    "parent_allowlisted": [1],
                    "helper_subscribe_attempted": [1, 2],
                    "cleanup_candidates": [1],
                },
            })
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", fake_helper,
    )
    assert steam_ugc_backend.run_ugc_install(
        [1, 2, 3], cancel_event=cancel, handoff_cb=handoffs.append,
    ) is False
    assert [call[0] for call in calls] == ["state", "subscribe-download"]
    assert handoffs == [{
        "parent_allowlisted": [1],
        "helper_subscribe_attempted": [1, 2],
        "cleanup_candidates": [1],
    }]


def test_next_ugc_install_freshly_requeries_state_after_cancel_cleanup(monkeypatch):
    first_cancel = threading.Event()
    calls = []
    state_query_count = 0
    monkeypatch.setattr(
        steam_ugc_backend, "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )

    def fake_helper(command, **kwargs):
        nonlocal state_query_count
        calls.append(command)
        callback = kwargs.get("on_event")
        if command == "state":
            state_query_count += 1
            ready_on_next_operation = state_query_count >= 2
            callback({
                "type": "item", "id": 7,
                "subscribed": ready_on_next_operation,
                "installed": ready_on_next_operation,
                "needs_update": False, "downloading": False,
                "download_pending": False,
                "download_bytes": 100 if ready_on_next_operation else 0,
                "total_bytes": 100,
                "state_names": (
                    ["Subscribed", "Installed"]
                    if ready_on_next_operation else []
                ),
            })
            return True, 0
        if command == "subscribe-download":
            first_cancel.set()
            callback({
                "type": "command_result", "ok": False,
                "cancelled": True,
                "cancel_handoff": {
                    "parent_allowlisted": [7],
                    "helper_subscribe_attempted": [7],
                    "cleanup_candidates": [7],
                },
            })
            return False, 0
        raise AssertionError(command)

    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", fake_helper,
    )
    assert steam_ugc_backend.run_ugc_install(
        [7], cancel_event=first_cancel,
    ) is False
    assert steam_ugc_backend.run_ugc_install(
        [7], cancel_event=threading.Event(),
    ) is True
    assert calls == ["state", "subscribe-download", "state"]


def test_fatal_cooperative_session_skips_secondary_refresh_and_cleanup(
        monkeypatch):
    session = steam_ugc_backend.CooperativeUGCSession()
    initial = {
        "type": "item", "id": 7, "subscribed": False, "installed": False,
        "needs_update": False, "downloading": False,
        "download_pending": False, "download_bytes": 0,
        "total_bytes": 100, "state_names": [],
    }

    monkeypatch.setattr(
        steam_ugc_backend, "_run_ugc_native_steam_preflight",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cache_ugc_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "active_ugc_session", lambda: session,
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_refresh_current_state",
        lambda *_args, **_kwargs: pytest.fail(
            "fatal cooperative session must not be queried again"
        ),
    )
    monkeypatch.setattr(
        steam_ugc_backend, "_cleanup_subscriptions",
        lambda *_args, **_kwargs: pytest.fail(
            "fatal cooperative session must not receive cleanup commands"
        ),
    )

    def fake_helper(command, **kwargs):
        if command == "state":
            kwargs["on_event"](dict(initial))
            return True, None
        session._fatal = True
        raise steam_ugc_backend.UGCSessionError("fatal unsubscribe")

    monkeypatch.setattr(
        steam_ugc_backend, "_run_helper_json_lines", fake_helper,
    )
    assert steam_ugc_backend.run_ugc_install([7]) is False


def test_streaming_fifteen_transient_items_never_relies_on_coalescing():
    harness = RendererHarness()
    for mid in range(1, 16):
        send(harness, event(mid, pending=True, ready=False))
    assert harness.item_lines == []


def test_burst_and_streaming_ambiguous_sequences_match():
    streaming = RendererHarness()
    burst = RendererHarness()
    payloads = [event(mid, downloading=True, ready=False) for mid in range(1, 16)]
    for payload in payloads:
        send(streaming, payload)
    old_glib = window_module.GLib
    window_module.GLib = burst.glib
    try:
        for payload in payloads:
            burst._steam_ugc_progress_from_worker(payload)
        burst.glib.drain()
    finally:
        window_module.GLib = old_glib
    assert streaming.item_lines == burst.item_lines == []


def test_final_and_refresh_snapshots_do_not_replace_visible_item():
    harness = RendererHarness()
    send(harness, event(1, installed=False, ready=False, downloaded=100,
                        total_bytes=1000, was_installed=False))
    send(harness, event(1, source="request", installed=False, ready=False,
                        downloaded=100, total_bytes=1000, was_installed=False,
                        attempted=True, accepted=True))
    send(harness, event(1, source="poll", installed=False, ready=False,
                        downloaded=250, total_bytes=1000, was_installed=False,
                        attempted=True, accepted=True))
    original = harness.steamcmd_line2.text
    send(harness, event(2, source="refresh", pending=True, ready=False))
    send(harness, event(1, source="final", ready=True))
    assert harness.steamcmd_line2.text == original == "Downloading Mod: Mod 1 - 0 MB (1/15)"


def test_old_attempt_event_is_rejected_after_new_attempt_starts():
    harness = RendererHarness(attempt_id=1)
    old = event(1, source="request", installed=False, ready=False,
                was_installed=False, attempted=True, accepted=True, attempt_id=1)
    old_glib = window_module.GLib
    window_module.GLib = harness.glib
    try:
        harness._steam_ugc_progress_from_worker(old)
        harness.current_attempt = 2
        harness.glib.drain()
    finally:
        window_module.GLib = old_glib
    assert harness.item_lines == []


@pytest.mark.parametrize("attempt_id", [0, None])
def test_missing_or_zero_attempt_cannot_render_item(attempt_id):
    harness = RendererHarness()
    payload = event(1, source="request", installed=False, ready=False,
                    was_installed=False, attempted=True, accepted=True,
                    attempt_id=attempt_id or 0)
    if attempt_id is None:
        payload.pop("join_attempt_id")
    send(harness, payload)
    assert harness.item_lines == []


def test_activity_map_clears_only_matching_attempt():
    tracker = JoinPopupActivityTracker()
    tracker.observe(event(1, attempt_id=1))
    tracker.observe(event(1, attempt_id=2))
    tracker.clear_attempt(1)
    assert tracker.get(1, 1) is None
    assert tracker.get(2, 1) is not None


def test_ugc_only_overlay_and_urgent_paths_remain_in_source():
    steamcmd_source = (window_module.Path(__file__).resolve().parents[1]
                       / "src/dzll_launcher/join_preparation_overlay_ui.py").read_text()
    window_source = (window_module.Path(__file__).resolve().parents[1]
                     / "src/dzll_launcher/window.py").read_text()
    assert "Steam Guard" not in steamcmd_source
    assert "steamcmd_cancel_btn" in steamcmd_source
    assert "PreparationPresentationReducer(" in window_source
    assert "JoinPopupPhase.ERROR" in window_source
    assert "_steam_ugc_render_cancelling()" in window_source


def test_ugc_backend_control_calls_and_join_handoff_remain():
    root = window_module.Path(__file__).resolve().parents[1]
    backend = (root / "src/dzll_launcher/steam_ugc_backend.py").read_text()
    join = (root / "src/dzll_launcher/join_prepare.py").read_text()
    assert '"subscribe-download"' in backend
    assert "time.sleep(1.0)" not in backend  # helper, not backend, owns the existing cadence
    assert "ok = win.run_steam_client_install(" in join
    assert "run_steamcmd_install" not in join
    assert "win.GLib.idle_add(after)" in join
