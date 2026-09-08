from pathlib import Path

from dzll_launcher.preparation_contracts import PreparationProgressEvent
from dzll_launcher.preparation_presentation import (
    PreparationPresentationReducer,
    PreparationProgressMode,
)


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
BROWSER_SOURCE = (
    ROOT / "src/dzll_launcher/background_prepare_browser.py"
).read_text(encoding="utf-8")


def raw(**payload):
    return PreparationProgressEvent.from_authoritative_payload({
        "backend": "steam_ugc",
        "backend_owner": "steam_client",
        "join_attempt_id": 7,
    } | payload)


def new_reducer(current=True):
    return PreparationPresentationReducer(
        operation_id=7, is_current=lambda operation_id: current and operation_id == 7,
    )


def work_set(*ids):
    return raw(type="presentation_work_set", work_ids=list(ids))


def item(mid, *, source, downloaded=0, total_bytes=1000, name=None,
         index=11, total=11, ready=False):
    return raw(
        type="session", id=mid, name=name or f"Mod {mid}", event_source=source,
        installed=False, ready=ready, request_attempted=source != "initial",
        request_accepted=source != "initial", was_installed_before=False,
        download_bytes=downloaded, total_bytes=total_bytes,
        index=index, total=total,
    )


def activate(reducer, mid=11, *, downloaded=100, total_bytes=1000, name=None):
    reducer.apply(item(mid, source="initial", total_bytes=total_bytes, name=name))
    reducer.apply(item(mid, source="request", total_bytes=total_bytes, name=name))
    return reducer.apply(item(
        mid, source="poll", downloaded=downloaded,
        total_bytes=total_bytes, name=name,
    )).snapshot


def snapshots(reducer, events):
    result = []
    for event in events:
        snapshot = reducer.apply(event).snapshot
        if snapshot is not None:
            result.append(snapshot)
    return result


def test_join_and_browser_reducer_instances_produce_identical_snapshots():
    sequence = [
        work_set(11, 9),
        raw(type="preflight", message="Waiting for native Steam"),
        raw(type="status", message="Checking arbitrary low-level detail"),
        item(11, source="initial"),
        item(11, source="request"),
        item(11, source="poll", downloaded=100),
        item(11, source="poll", downloaded=500),
        item(11, source="refresh", downloaded=0, total_bytes=0),
    ]
    assert snapshots(new_reducer(), sequence) == snapshots(new_reducer(), sequence)


def test_raw_first_index_eleven_is_replaced_by_shared_sequential_ordinal():
    reducer = new_reducer()
    reducer.apply(work_set(11, 9, 3))
    snapshot = activate(reducer, 11)
    assert (snapshot.current, snapshot.total) == (1, 3)
    assert "11/11" not in repr(snapshot)


def test_raw_count_is_omitted_until_shared_work_set_and_genuine_activity():
    reducer = new_reducer()
    assert reducer.apply(item(11, source="initial")).snapshot is None
    assert reducer.apply(item(11, source="request")).snapshot is None
    assert reducer.snapshot is None


def test_valid_determinate_progress_survives_empty_default_events():
    reducer = new_reducer()
    reducer.apply(work_set(11))
    stable = activate(reducer, 11, downloaded=400, total_bytes=1000)
    assert stable.progress_mode is PreparationProgressMode.DETERMINATE
    assert stable.fraction == 0.4
    for _ in range(3):
        assert reducer.apply(item(
            11, source="poll", downloaded=0, total_bytes=0,
        )).snapshot is None
    assert reducer.snapshot == stable


def test_determinate_progress_advances_and_duplicate_is_suppressed():
    reducer = new_reducer()
    reducer.apply(work_set(11))
    first = activate(reducer, 11, downloaded=250).fraction
    advanced = reducer.apply(item(11, source="poll", downloaded=750)).snapshot
    duplicate = reducer.apply(item(11, source="poll", downloaded=750)).snapshot
    assert first == 0.25
    assert advanced.fraction == 0.75
    assert duplicate is None


def test_genuine_item_transition_resets_mode_and_advances_shared_count():
    reducer = new_reducer()
    reducer.apply(work_set(11, 9))
    first = activate(reducer, 11, downloaded=500)
    second = activate(reducer, 9, downloaded=1, total_bytes=0, name="Second")
    assert (first.current, first.total) == (1, 2)
    assert (second.current, second.total) == (2, 2)
    assert second.current_mod_name == "Second"
    assert second.progress_mode is PreparationProgressMode.INDETERMINATE
    assert second.fraction is None


def test_determinate_after_indeterminate_restores_shared_mode():
    reducer = new_reducer()
    reducer.apply(work_set(11))
    pulse = activate(reducer, 11, downloaded=1, total_bytes=0)
    exact = reducer.apply(item(
        11, source="poll", downloaded=80, total_bytes=100,
    )).snapshot
    assert pulse.progress_mode is PreparationProgressMode.INDETERMINATE
    assert exact.progress_mode is PreparationProgressMode.DETERMINATE
    assert exact.fraction == 0.8


def test_startup_chatter_maps_to_stable_semantic_stages_only():
    reducer = new_reducer()
    states = snapshots(reducer, [
        raw(type="status", message="classification item 1"),
        raw(type="status", message="classification item 2"),
        raw(type="status", message="classification item 3"),
    ])
    assert len(states) == 1
    assert states[0].stage_text == "Checking & Preparing Mods for Join..."


def test_refresh_final_and_stale_events_cannot_replace_current_item():
    reducer = new_reducer()
    reducer.apply(work_set(11))
    stable = activate(reducer, 11, downloaded=500)
    assert reducer.apply(item(11, source="refresh", total_bytes=0)).snapshot is None
    assert reducer.apply(item(11, source="final", ready=True)).snapshot is None
    stale = PreparationProgressEvent.from_authoritative_payload({
        "join_attempt_id": 6, "backend": "steam_ugc",
        "backend_owner": "steam_client", "id": 99,
    })
    assert reducer.apply(stale).snapshot is None
    assert reducer.snapshot == stable


def test_join_and_browser_source_use_same_reducer_and_browser_does_not_render_raw():
    assert "PreparationPresentationReducer(" in WINDOW_SOURCE
    assert "self._reducer.apply(event)" in BROWSER_SOURCE
    assert "render_event" not in BROWSER_SOURCE
    for forbidden in (
        "event.item_name", "event.item_id", "event.fraction",
        'payload.get("index")', 'payload.get("total")', "download_bytes",
    ):
        assert forbidden not in BROWSER_SOURCE
