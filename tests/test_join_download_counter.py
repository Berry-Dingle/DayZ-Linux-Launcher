from pathlib import Path

from dzll_launcher.join_attempt import JoinAttemptTracker


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
JOIN_SOURCE = (ROOT / "src/dzll_launcher/join_prepare.py").read_text(encoding="utf-8")
STEAM_CLIENT_SOURCE = (ROOT / "src/dzll_launcher/steam_client_mods.py").read_text(encoding="utf-8")


def begin_counter(mod_ids):
    logs = []
    tracker = JoinAttemptTracker(log_sink=logs.append)
    attempt = tracker.begin(
        ip="127.0.0.1", game_port=2302, query_port=27016,
        name="Counter", skip_dayz_launcher=True,
    )
    initialized, total = tracker.initialize_download_counter(attempt.attempt_id, mod_ids)
    assert initialized
    return tracker, attempt, total, logs


def test_twenty_item_attempt_assigns_ascending_ordinals():
    tracker, attempt, total, _logs = begin_counter(range(100, 120))
    values = [tracker.note_genuine_mod_work(attempt.attempt_id, mid)
              for mid in (118, 117, 116)]
    assert total == 20
    assert [value.display_ordinal for value in values] == [1, 2, 3]
    assert [value.total for value in values] == [20, 20, 20]


def test_numerator_never_decreases_with_reordered_activity():
    tracker, attempt, _total, _logs = begin_counter([1, 2, 3, 4])
    values = [tracker.note_genuine_mod_work(attempt.attempt_id, mid)
              for mid in (4, 2, 3, 1, 4)]
    displayed = [value.display_ordinal for value in values]
    assert displayed == [1, 2, 3, 4, 4]
    assert displayed == sorted(displayed)


def test_denominator_does_not_shrink_as_items_finish():
    tracker, attempt, _total, _logs = begin_counter(range(1, 21))
    values = [tracker.note_genuine_mod_work(attempt.attempt_id, mid)
              for mid in (1, 2, 3, 4)]
    assert [value.total for value in values] == [20, 20, 20, 20]
    assert attempt.download_work_total == 20


def test_duplicate_progress_for_same_item_retains_ordinal():
    tracker, attempt, _total, _logs = begin_counter([10, 11])
    values = [tracker.note_genuine_mod_work(attempt.attempt_id, 10) for _ in range(5)]
    assert [value.display_ordinal for value in values] == [1, 1, 1, 1, 1]
    assert [value.status for value in values] == [
        "assigned", "duplicate", "duplicate", "duplicate", "duplicate",
    ]
    assert sum(value.should_log_duplicate for value in values) == 1


def test_switch_to_new_item_increments_once():
    tracker, attempt, _total, _logs = begin_counter([10, 11])
    first = tracker.note_genuine_mod_work(attempt.attempt_id, 10)
    second = tracker.note_genuine_mod_work(attempt.attempt_id, 11)
    duplicate = tracker.note_genuine_mod_work(attempt.attempt_id, 11)
    assert [first.display_ordinal, second.display_ordinal, duplicate.display_ordinal] == [1, 2, 2]


def test_return_to_assigned_item_reuses_assignment_without_decreasing_display():
    tracker, attempt, _total, _logs = begin_counter([10, 11])
    first = tracker.note_genuine_mod_work(attempt.attempt_id, 10)
    second = tracker.note_genuine_mod_work(attempt.attempt_id, 11)
    returned = tracker.note_genuine_mod_work(attempt.attempt_id, 10)
    assert (first.assigned_ordinal, second.assigned_ordinal) == (1, 2)
    assert returned.status == "reused"
    assert returned.assigned_ordinal == 1
    assert returned.display_ordinal == 2


def test_duplicate_workshop_ids_count_once():
    tracker, attempt, total, _logs = begin_counter([1, 1, 2, 2, 3, 1])
    assert total == 3
    assert attempt.download_work_ids == (1, 2, 3)


def test_ready_items_are_excluded_by_fixed_work_queue_input():
    required = [1, 2, 3, 4]
    ready = {1, 4}
    work_ids = [mid for mid in required if mid not in ready]
    _tracker, attempt, total, _logs = begin_counter(work_ids)
    assert total == 2
    assert attempt.download_work_ids == (2, 3)


def test_cleanup_clears_all_counter_state():
    tracker, attempt, _total, logs = begin_counter([1, 2, 3])
    tracker.note_genuine_mod_work(attempt.attempt_id, 1)
    assert tracker.cleanup(attempt.attempt_id, "cancel")
    assert attempt.download_counter_initialized is False
    assert attempt.download_work_ids == ()
    assert attempt.download_work_total == 0
    assert attempt.download_ordinals == {}
    assert attempt.download_next_ordinal == 1
    assert attempt.download_display_ordinal == 0
    assert attempt.active_mod_id is None
    assert any("download presentation counter cleared" in line for line in logs)


def test_restart_after_cancel_with_fourteen_items_begins_one_of_fourteen():
    tracker, first, _total, _logs = begin_counter(range(1, 21))
    tracker.note_genuine_mod_work(first.attempt_id, 18)
    tracker.cleanup(first.attempt_id, "cancel")
    second = tracker.begin(
        ip="127.0.0.1", game_port=2302, query_port=27016,
        name="Restart", skip_dayz_launcher=True,
    )
    assert tracker.initialize_download_counter(second.attempt_id, range(100, 114)) == (True, 14)
    value = tracker.note_genuine_mod_work(second.attempt_id, 113)
    assert (value.display_ordinal, value.total) == (1, 14)


def test_second_cancel_then_eight_items_begins_one_of_eight():
    tracker, first, _total, _logs = begin_counter(range(1, 21))
    tracker.cleanup(first.attempt_id, "cancel one")
    second = tracker.begin(ip="1", game_port=1, query_port=1, name="second")
    tracker.initialize_download_counter(second.attempt_id, range(1, 15))
    tracker.note_genuine_mod_work(second.attempt_id, 14)
    tracker.cleanup(second.attempt_id, "cancel two")
    third = tracker.begin(ip="1", game_port=1, query_port=1, name="third")
    tracker.initialize_download_counter(third.attempt_id, range(1, 9))
    value = tracker.note_genuine_mod_work(third.attempt_id, 8)
    assert (value.display_ordinal, value.total) == (1, 8)


def test_stale_cancelled_attempt_cannot_mutate_new_counter():
    tracker, old, _total, _logs = begin_counter([1, 2])
    tracker.cleanup(old.attempt_id, "cancel")
    new = tracker.begin(ip="2", game_port=2, query_port=2, name="new")
    tracker.initialize_download_counter(new.attempt_id, [8, 9])
    stale = tracker.note_genuine_mod_work(old.attempt_id, 1)
    current = tracker.note_genuine_mod_work(new.attempt_id, 9)
    assert stale.status == "stale"
    assert (current.display_ordinal, current.total) == (1, 2)
    assert new.download_ordinals == {9: 1}


def test_attempt_ids_isolate_consecutive_counters():
    tracker, old, _total, _logs = begin_counter([1, 2, 3])
    tracker.note_genuine_mod_work(old.attempt_id, 3)
    tracker.cleanup(old.attempt_id, "done")
    new = tracker.begin(ip="3", game_port=3, query_port=3, name="new")
    tracker.initialize_download_counter(new.attempt_id, [3])
    value = tracker.note_genuine_mod_work(new.attempt_id, 3)
    assert new.attempt_id == old.attempt_id + 1
    assert (value.display_ordinal, value.total) == (1, 1)


def test_counter_initialized_from_work_queue_and_raw_event_index_not_formatted():
    assert "_join_popup_initialize_download_counter(" in JOIN_SOURCE
    formatter = WINDOW_SOURCE.split("def _join_popup_active_download_text", 1)[1]
    formatter = formatter.split("def _show_join_progress_overlay", 1)[0]
    assert 'event.get("index")' not in formatter
    assert 'event.get("total")' not in formatter
    assert 'f" ({int(current)}/{int(total)})"' in formatter
    assert '"index": index' in STEAM_CLIENT_SOURCE  # retained for backend diagnostics only
