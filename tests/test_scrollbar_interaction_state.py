from dzll_launcher.scrollbar_interaction import ScrollbarInteractionState


def test_begin_enters_once_and_duplicate_is_harmless():
    state = ScrollbarInteractionState()
    generation, began = state.begin(now=1.0, adjustment_value=10.0)
    assert began and state.active and generation == 1
    assert state.begin(now=2.0, adjustment_value=99.0) == (generation, False)
    assert state.interaction_start_time == 1.0
    assert state.latest_adjustment_value == 10.0


def test_adjustments_retain_newest_and_reject_stale_generation():
    state = ScrollbarInteractionState()
    generation, _ = state.begin(now=1.0)
    assert state.record_adjustment(20.0, generation=generation, now=1.25)
    assert state.record_adjustment(30.0, generation=generation, now=1.75)
    assert not state.record_adjustment(40.0, generation=generation - 1)
    assert state.first_adjustment_value == 20.0
    assert state.latest_adjustment_value == 30.0
    assert state.adjustment_events == 2
    settled = state.settle(reason="release", now=2.0, generation=generation)
    assert settled.last_adjustment_time == 1.75
    assert settled.last_adjustment_to_settle == 0.25


def test_deferred_work_coalesces_by_category():
    state = ScrollbarInteractionState()
    generation, _ = state.begin(now=1.0)
    assert state.defer("live", "old", generation=generation)
    assert state.defer("live", "new", generation=generation)
    assert state.defer("monitor", "refresh", generation=generation)
    settled = state.settle(reason="release", now=2.0, generation=generation)
    assert settled is not None
    assert settled.deferred_work == {"live": "new", "monitor": "refresh"}
    assert settled.deferred_requests == 3
    assert settled.coalesced_requests == 1


def test_release_without_movement_settles_exactly_once():
    state = ScrollbarInteractionState()
    generation, _ = state.begin(now=1.0, adjustment_value=4.0)
    assert state.request_settle(generation=generation)
    assert not state.request_settle(generation=generation)
    settled = state.settle(reason="release", now=1.1, generation=generation)
    assert settled is not None
    assert settled.adjustment_events == 0
    assert settled.latest_adjustment_value == 4.0
    assert state.settle(reason="release", now=1.2, generation=generation) is None


def test_cancel_watchdog_unmap_and_shutdown_each_clear_active_state():
    for reason in ("cancel", "watchdog", "unmap", "shutdown"):
        state = ScrollbarInteractionState()
        generation, _ = state.begin(now=1.0)
        settled = state.cancel(reason=reason, now=2.0, generation=generation)
        assert settled is not None and settled.reason == reason
        assert not state.active
        assert state.cancel(reason=reason, now=3.0, generation=generation) is None


def test_old_generation_cannot_settle_new_interaction():
    state = ScrollbarInteractionState()
    first, _ = state.begin(now=1.0)
    assert state.settle(reason="release", now=2.0, generation=first)
    second, _ = state.begin(now=3.0)
    assert second > first
    assert state.settle(reason="stale", now=4.0, generation=first) is None
    assert state.active
