import queue
from pathlib import Path

from dzll_launcher import steam_ugc_backend, steam_ugc_helper


def snapshot(item_id, *, subscribed=True, installed=True, needs_update=False):
    state = (
        (1 if subscribed else 0)
        | (4 if installed else 0)
        | (8 if needs_update else 0)
    )
    return steam_ugc_helper.ItemSnapshot(
        item_id=int(item_id),
        state=state,
        state_names=[],
        subscribed=bool(subscribed),
        installed=bool(installed),
        needs_update=bool(needs_update),
        downloading=False,
        download_pending=False,
        download_bytes=0,
        total_bytes=0,
        size_on_disk=1 if installed else 0,
        install_folder=f"/workshop/{item_id}" if installed else None,
    )


class RefreshSteam:
    def __init__(self, states, outcomes=None):
        self.states = dict(states)
        self.outcomes = dict(outcomes or {})
        self.subscribe_calls = []
        self.completion_checks = []
        self.callbacks = 0

    def snapshot(self, item_id):
        return self.states[int(item_id)]

    def subscribe(self, item_id):
        mid = int(item_id)
        self.subscribe_calls.append(mid)
        outcome = self.outcomes.get(mid, {})
        return int(outcome.get("handle", mid + 1000))

    def run_callbacks(self):
        self.callbacks += 1

    def api_call_completed(self, handle):
        assert len(self.subscribe_calls) == len([
            state for state in self.states.values() if state.subscribed
        ])
        self.completion_checks.append(int(handle))
        mid = int(handle) - 1000
        outcome = self.outcomes.get(mid, {})
        return bool(outcome.get("completed", True)), bool(outcome.get("api_failed", False))

    def subscribe_call_result(self, handle):
        mid = int(handle) - 1000
        outcome = self.outcomes.get(mid, {})
        if outcome.get("make_stale"):
            self.states[mid] = snapshot(mid, needs_update=True)
        return (
            bool(outcome.get("retrieved", True)),
            bool(outcome.get("result_failed", False)),
            int(outcome.get("result", steam_ugc_helper.ERESULT_OK)),
            int(outcome.get("result_item_id", mid)),
        )


def test_eighty_subscribed_refreshes_are_queued_before_completion_wait():
    ids = list(range(1, 81))
    steam = RefreshSteam({mid: snapshot(mid) for mid in ids})

    final, result = steam_ugc_helper._refresh_subscribed_batch(
        steam, ids, 2.0, sleep_fn=lambda _seconds: None,
    )

    assert steam.subscribe_calls == ids
    assert len(steam.completion_checks) == 80
    assert steam.callbacks == 1
    assert result["refreshed"] == ids
    assert result["failed"] == result["timed_out"] == []
    assert all(not state.needs_update for state in final)


def test_refresh_requeries_current_and_newly_stale_states_without_touching_missing():
    states = {
        1: snapshot(1),
        2: snapshot(2),
        3: snapshot(3, installed=False),
        4: snapshot(4, subscribed=False, installed=False),
    }
    steam = RefreshSteam(states, {2: {"make_stale": True}})

    final, result = steam_ugc_helper._refresh_subscribed_batch(
        steam, [1, 2, 3, 4], 2.0, sleep_fn=lambda _seconds: None,
    )
    final_by_id = {state.item_id: state for state in final}

    assert steam.subscribe_calls == [1, 2, 3]
    assert result["refreshed"] == [1, 2, 3]
    assert not final_by_id[1].needs_update
    assert final_by_id[2].needs_update
    assert final_by_id[3].subscribed and not final_by_id[3].installed
    assert not final_by_id[4].subscribed


def test_invalid_handle_and_non_success_result_are_best_effort_failures():
    steam = RefreshSteam(
        {1: snapshot(1), 2: snapshot(2), 3: snapshot(3)},
        {
            1: {"handle": 0},
            2: {"result": 2},
            3: {"result_item_id": 999},
        },
    )

    final, result = steam_ugc_helper._refresh_subscribed_batch(
        steam, [1, 2, 3], 2.0, sleep_fn=lambda _seconds: None,
    )

    assert [state.item_id for state in final] == [1, 2, 3]
    assert result["refreshed"] == []
    assert result["failed"] == [1, 2, 3]
    assert {failure["reason"] for failure in result["failures"]} == {
        "invalid_call_handle", "subscribe_result_failed", "result_item_mismatch",
    }
    assert result["timed_out"] == []


def test_refresh_timeout_uses_one_batch_deadline_and_does_not_retry():
    clock = {"now": 10.0}
    steam = RefreshSteam(
        {1: snapshot(1), 2: snapshot(2)},
        {1: {"completed": False}, 2: {"completed": False}},
    )

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += float(seconds)

    _final, result = steam_ugc_helper._refresh_subscribed_batch(
        steam, [1, 2], 0.02, monotonic_fn=monotonic, sleep_fn=sleep,
    )

    assert steam.subscribe_calls == [1, 2]
    assert result["failed"] == []
    assert result["timed_out"] == [1, 2]
    assert clock["now"] <= 10.021


def test_refresh_session_cancellation_uses_existing_cooperative_control():
    steam = RefreshSteam(
        {1: snapshot(1)}, {1: {"completed": False}},
    )
    commands = queue.Queue()
    commands.put({
        "command": "cancel",
        "request_id": "cancel-2",
        "target_request_id": "refresh-1",
    })

    try:
        steam_ugc_helper._session_refresh_subscribed_state(
            steam, commands, "refresh-1", [1], 2.0,
        )
    except steam_ugc_helper._SessionCancelled as exc:
        assert exc.reason == "cancelled"
    else:
        raise AssertionError("refresh did not observe cooperative cancellation")


def test_backend_returns_final_snapshots_with_partial_refresh_details(monkeypatch):
    def helper(command, **kwargs):
        assert command == "refresh-subscribed-state"
        kwargs["on_event"](snapshot(1).event())
        kwargs["on_event"]({
            "type": "command_result",
            "ok": True,
            "refreshed": [],
            "failed": [1],
            "timed_out": [],
            "failures": [{"id": 1, "reason": "api_call_failed"}],
        })
        return True, None

    monkeypatch.setattr(steam_ugc_backend, "_run_helper_json_lines", helper)
    monkeypatch.setattr(steam_ugc_backend, "_cache_ugc_state", lambda *_a, **_k: None)

    ok, states, result = steam_ugc_backend.refresh_subscribed_ugc_state_checked([1])
    assert ok
    assert states[1]["ready"]
    assert result["failed"] == [1]


def test_join_refresh_boundary_contains_no_destructive_or_fallback_paths():
    root = Path(__file__).resolve().parents[1]
    helper_source = (
        root / "src/dzll_launcher/steam_ugc_helper.py"
    ).read_text(encoding="utf-8")
    refresh_body = helper_source.split("def _refresh_subscribed_batch(", 1)[1]
    refresh_body = refresh_body.split("def _session_refresh_subscribed_state(", 1)[0]
    lowered = refresh_body.casefold()
    for forbidden in (
        "unsubscribe", "download(", "steamcmd", "appworkshop", "acf",
        "rmtree", "unlink(", "repair", "restart",
    ):
        assert forbidden not in lowered

    join_source = (
        root / "src/dzll_launcher/join_prepare.py"
    ).read_text(encoding="utf-8")
    prepare_body = join_source.split("def prepare_required_mods(", 1)[1]
    prepare_body = prepare_body.split("def join_prepare_and_launch(", 1)[0]
    assert prepare_body.count("refresh_subscribed_ugc_state_checked(") == 1
