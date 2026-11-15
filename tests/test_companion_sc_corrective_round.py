"""Consolidated corrective round: B1, A1, N1, F1/C1 (policy M2), S1, S2, P1
and the per-server Learning Blocked status line.

Raw and compact authority are both exercised.  The production path is the
real ``rebuild_schema4_server_record`` (compact authority, folding, pruning)
with a JSON reopen after every rebuild; folding across small rings uses the
compact-only runner of the compact-authority suite.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

import test_companion_restart_compact_authority as ca
import test_companion_restart_learned_state_integration as integ
import test_companion_sc_learning_policy_stage8_baseline as s8
import test_companion_sc_stage3_graduated_authority as st3
import test_companion_sc_time_shift_detection as ts

from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import server_companion_ui
from dzll_launcher.server_companion_ui import ServerCompanionPanel
from dzll_launcher.window import DZLLWindow

H = s8.HOUR
P3 = 3 * H
P4 = 4 * H
at = s8.at
State = s8.State
LATENT = "latent_normal_challenger_context"
CONSECUTIVE = authority.CONSECUTIVE_MISSES_REASON
SUSPECTED = authority.TIME_SHIFT_SUSPECTED_REASON
WITHDRAWN = authority.INCUMBENT_WITHDRAWN_CONTRADICTION_REASON
RINGS = (2, 3, 5)


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------


def production_sessions(sessions):
    """One continuous session per entry; production rebuild and JSON reopen
    after each.  Returns (record, steps)."""

    pipeline = s8.Pipeline()
    record = {}
    steps = []
    for index, times in enumerate(sessions):
        pipeline.observe(f"session-{index}", times)
        ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
        record = integ.rebuild(record, ordered, pipeline.coverage)
        record = json.loads(json.dumps(record))
        steps.append(integ.record_step(record))
    return record, steps


def countdown(step):
    return step.consumer().countdown_visible


def first_countdown(steps):
    return next((index + 1 for index, step in enumerate(steps) if countdown(step)), None)


def period(step):
    selected = step.decision.selected_shadow_regime
    return None if selected is None else selected.candidate_period_seconds


def evenings(period_seconds, count, *, base_hour, restarts=2, vary=False, jitter=0.0, seed=0):
    rng = random.Random(seed)
    values = []
    for day in range(count):
        hour = base_hour + (rng.choice((0, 0, 1, -1)) * period_seconds / H if vary else 0)
        start = at(day, 0) + hour * H
        values.append(
            [
                start + index * period_seconds + (rng.uniform(-jitter, jitter) if jitter else 0.0)
                for index in range(restarts)
            ]
        )
    return values


def with_blips(pipeline, times, start=900):
    """Non-authentic service interruptions: they fold the ring, never evidence."""

    for index, when in enumerate(times):
        session, event = ts.shaped(
            f"blip-{index}", when, start + index, **ts.INELIGIBLE_SHAPES["service_interruption"]
        )
        pipeline.coverage.append(session.coverage())
        pipeline.events.append(event)


def raw_and_compact(pipeline, ledger, capacity, *, first=1):
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    batches = [ordered[:first]] + [[item] for item in ordered[first:]]
    ledgers = [
        tuple(item for item in ledger if item.window_end_at <= batch[0].canonical_phase_at)
        for batch in batches
    ]
    raw = ca.raw_steps(pipeline, batches, ledgers=ledgers)
    runner = ca.CompactOnly(pipeline, capacity=capacity)
    compact = [runner.step(batch, windows=ledgers[index]) for index, batch in enumerate(batches)]
    return raw, compact, runner


def assert_compact_never_shows_more(raw, compact):
    for index, (raw_step, compact_step) in enumerate(zip(raw, compact)):
        if not countdown(raw_step):
            assert not countdown(compact_step), index
        if not raw_step.consumer().scheduled_warning_eligible:
            assert not compact_step.consumer().scheduled_warning_eligible, index


# --------------------------------------------------------------------------
# A1: one administrative restart never hides an E1 countdown
# --------------------------------------------------------------------------


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
@pytest.mark.parametrize("hour", [hour for hour in range(24) if hour % 3])
def test_a1_single_off_grid_restart_keeps_the_countdown(build, hour):
    pipeline, original = build()
    st3.add_sparse(pipeline, [at(8, hour)], "admin")
    step = st3.advance(pipeline, original)
    assert st3.regime(step).regime_id == st3.regime(original).regime_id
    assert LATENT not in step.decision.reason_codes
    assert countdown(step)


@pytest.mark.parametrize("offset", [-H, H], ids=["early", "late"])
def test_a1_h2_established_cycle_keeps_its_countdown_after_one_displaced_restart(offset):
    pipeline = s8.three_hour_chain(3)
    original = s8.settled(pipeline)
    assert original.decision.state is State.ESTABLISHED
    pipeline.sparse("admin", [at(2, 9) + offset])
    step = pipeline.evaluate(s8.persisted(original.decision))
    assert LATENT not in step.decision.reason_codes
    assert countdown(step)


def test_a1_latent_context_resting_on_incumbent_explained_history_is_not_challenger_evidence():
    pipeline, original = st3.e1()
    st3.add_sparse(pipeline, [at(8, 7)], "admin")
    step = st3.advance(pipeline, original)
    # Latent contexts carry schema-3 normal strength built on restarts the
    # 3 h incumbent also explains; none has two independent off-phase onsets
    # or a high relationship of its own.
    latent = [item for item in step.decision.challenger_contexts if item.normal_context_strength > 0]
    assert latent
    assert all(
        len(item.off_phase_event_ids) < 2 and authority.context_high_relationship_count(item) == 0
        for item in latent
    )
    assert LATENT not in step.decision.reason_codes
    assert countdown(step)
    st3.assert_fixed_point(pipeline, step)


def test_a1_genuine_change_beginning_with_a_coincidental_restart_is_still_recognised():
    pipeline, step = st3.e1()
    seen = []
    for index, when in enumerate((at(8, 7), at(9, 11), at(10, 19), at(11, 23))):
        st3.add_sparse(pipeline, [when], f"change-{index}")
        step = st3.advance(pipeline, step)
        seen.append((countdown(step), period(step)))
    assert seen[0] == (True, P3)  # the lone first restart is an admin restart so far
    assert not seen[1][0]  # withheld from the second coherent onset
    assert seen[3][1] == P4  # Awaiting the new cycle at the fourth
    st3.assert_fixed_point(pipeline, step)


# --------------------------------------------------------------------------
# B1: the E2 consecutive-miss suspension survives folding
# --------------------------------------------------------------------------


def _consecutive_miss_pipeline(*, aligned_after):
    pipeline = s8.three_hour_chain(4)
    ledger = (st3.miss(P3, at(8, 6), "m1"), st3.miss(P3, at(8, 9), "m2"))
    with_blips(pipeline, [at(8, 10, 30), at(8, 11, 30), at(8, 13, 40), at(8, 14, 50), at(8, 16, 20)])
    if aligned_after:
        pipeline.sparse("aligned", [at(8, 18)])
    return pipeline, ledger


@pytest.mark.parametrize("capacity", RINGS)
def test_b1_consecutive_miss_suspension_is_not_released_by_folding(capacity):
    pipeline, ledger = _consecutive_miss_pipeline(aligned_after=False)
    raw, compact, runner = raw_and_compact(pipeline, ledger, capacity)
    assert CONSECUTIVE in raw[-1].decision.reason_codes
    assert CONSECUTIVE in compact[-1].decision.reason_codes
    assert not countdown(compact[-1])
    assert_compact_never_shows_more(raw, compact)
    if capacity < 5:
        assert sum(item.genuine_miss_count for item in runner.state.misses) == 2
    # The regime (period and phase) and its E2 authority are kept throughout;
    # the compact regime keeps its own identity across the hold.
    for path in (raw, compact):
        identities = {st3.regime(step).regime_id for step in path[2:]}
        assert len(identities) == 1
    assert st3.regime(compact[-1]).candidate_period_seconds == P3
    assert st3.regime(compact[-1]).phase_offset == st3.regime(raw[-1]).phase_offset
    assert authority.REGIME_STRONG_REASON in st3.regime(compact[-1]).reason_codes


@pytest.mark.parametrize("capacity", RINGS)
def test_b1_genuine_aligned_restart_releases_the_hold(capacity):
    pipeline, ledger = _consecutive_miss_pipeline(aligned_after=True)
    raw, compact, _runner = raw_and_compact(pipeline, ledger, capacity)
    assert countdown(raw[-1]) and countdown(compact[-1])
    assert CONSECUTIVE not in compact[-1].decision.reason_codes


def test_b1_hold_is_inactive_on_the_raw_path():
    pipeline, ledger = _consecutive_miss_pipeline(aligned_after=False)
    step = s8.settled(pipeline)
    # Raw decides from the misses themselves (they never fold there).
    assert authority._consecutive_miss_hold(
        prior=step.decision,
        incumbent=step.decision.incumbent_shadow_regime,
        strong=True,
        windows=ledger,
        event_ids=frozenset(),
        last_aligned_at=0.0,
        compact=None,
    ) is False


# --------------------------------------------------------------------------
# S1: a withheld time-shift suspicion survives folding
# --------------------------------------------------------------------------


def _shift_pipeline(*, then=()):
    pipeline = s8.three_hour_chain(4)
    st3.add_sparse(pipeline, [at(8, 10), at(9, 10)], "shifted")
    with_blips(pipeline, [at(9, 11, 30), at(9, 13, 40), at(9, 14, 50), at(9, 16, 20), at(9, 17, 20)])
    if then:
        st3.add_sparse(pipeline, list(then), "after")
    return pipeline


@pytest.mark.parametrize("capacity", (2, 3))
def test_s1_suspected_shift_is_not_released_by_folding(capacity):
    raw, compact, _runner = raw_and_compact(_shift_pipeline(), (), capacity)
    assert SUSPECTED in raw[-1].decision.reason_codes
    assert SUSPECTED in compact[-1].decision.reason_codes
    assert not countdown(compact[-1])
    assert_compact_never_shows_more(raw, compact)


@pytest.mark.parametrize("capacity", (2, 3))
def test_s1_original_phase_restart_releases_the_suspicion(capacity):
    raw, compact, _runner = raw_and_compact(_shift_pipeline(then=(at(9, 21),)), (), capacity)
    assert countdown(raw[-1]) and countdown(compact[-1])
    assert SUSPECTED not in compact[-1].decision.reason_codes
    assert st3.regime(compact[-1]).phase_offset == st3.regime(raw[-1]).phase_offset


@pytest.mark.parametrize("capacity", (2, 3))
def test_s1_first_displaced_restart_alone_stays_internal_after_folding(capacity):
    pipeline = s8.three_hour_chain(4)
    st3.add_sparse(pipeline, [at(8, 10)], "displaced")
    with_blips(pipeline, [at(8, 11, 30), at(8, 13, 40), at(8, 14, 50), at(8, 16, 20)])
    raw, compact, _runner = raw_and_compact(pipeline, (), capacity)
    for step in (*raw[4:], *compact[4:]):
        assert SUSPECTED not in step.decision.reason_codes
        assert countdown(step)


# --------------------------------------------------------------------------
# S2: a contradiction withdrawal survives folding
# --------------------------------------------------------------------------


def _withdrawal_pipeline(*, back=()):
    pipeline, _original = st3.e1()
    st3.add_sparse(pipeline, [at(7, 0), at(7, 3, 4)], "aligned-after-establishment")
    for when in (at(7, 13, 37), at(8, 7, 11)):
        st3.add_sparse(pipeline, [when], f"off-{when}")
    ledger = (st3.miss(P3, at(7, 15), "c-miss-1"), st3.miss(P3, at(8, 18), "c-miss-2"))
    with_blips(pipeline, [at(8, 19, 30), at(8, 20, 40), at(8, 22, 50), at(8, 23, 20), at(9, 1, 20), at(9, 2, 40)])
    if back:
        st3.add_sparse(pipeline, list(back), "back")
    return pipeline, ledger


@pytest.mark.parametrize("capacity", (3, 5))
def test_s2_withdrawal_is_kept_across_folding(capacity):
    pipeline, ledger = _withdrawal_pipeline()
    raw, compact, _runner = raw_and_compact(pipeline, ledger, capacity, first=10)
    for raw_step, compact_step in zip(raw, compact):
        if WITHDRAWN in raw_step.decision.reason_codes:
            assert WITHDRAWN in compact_step.decision.reason_codes
    assert WITHDRAWN in compact[-1].decision.reason_codes
    assert_compact_never_shows_more(raw, compact)


@pytest.mark.parametrize("capacity", (3, 5))
def test_s2_genuine_reconfirmation_restores_on_both_paths(capacity):
    pipeline, ledger = _withdrawal_pipeline(back=(at(9, 9), at(10, 12)))
    raw, compact, _runner = raw_and_compact(pipeline, ledger, capacity, first=10)
    assert raw[-1].decision.selected_shadow_regime is not None
    assert countdown(raw[-1])
    assert compact[-1].decision.selected_shadow_regime is not None
    assert countdown(compact[-1])


def test_s2_ring_two_never_shows_a_countdown_raw_withholds():
    pipeline, ledger = _withdrawal_pipeline()
    raw, compact, _runner = raw_and_compact(pipeline, ledger, 2, first=10)
    assert_compact_never_shows_more(raw, compact)


# --------------------------------------------------------------------------
# N1: two midpoint administrative restarts never establish the divisor
# --------------------------------------------------------------------------


def _six_hour_with_midpoint_admins(days=14, admin_days=(3, 9)):
    return [
        [at(day, 18)] if day not in admin_days else [at(day, 18), at(day, 21)]
        for day in range(days)
    ]


def _four_hour_with_midpoint_admins():
    return [[at(day, 20)] if day not in (2, 6) else [at(day, 20), at(day, 22)] for day in range(12)]


def _discriminating_six_hour_with_midpoint_admins():
    sessions = [[at(day, (6 * day) % 24)] for day in range(10)]
    sessions[3] = [at(3, 18), at(3, 21)]
    sessions[7] = [at(7, 18), at(7, 21)]
    return sessions


@pytest.mark.parametrize(
    "sessions",
    [_six_hour_with_midpoint_admins, _four_hour_with_midpoint_admins, _discriminating_six_hour_with_midpoint_admins],
    ids=["6h_lone", "4h_lone", "6h_discriminating"],
)
def test_n1_midpoint_admin_intervals_never_establish_a_divisor(sessions):
    _record, steps = production_sessions(sessions())
    assert all(not countdown(step) for step in steps)
    assert all(step.decision.state is not State.ESTABLISHED for step in steps)
    pipeline = s8.Pipeline()
    for index, times in enumerate(sessions()):
        pipeline.observe(f"raw-{index}", times)
    raw = s8.settled(pipeline)
    assert raw.decision.state is not State.ESTABLISHED and not countdown(raw)


def test_n1_later_witnessed_multiple_interval_does_not_create_a_divisor_either():
    _record, steps = production_sessions(
        _six_hour_with_midpoint_admins() + [[at(15, 18), at(16, 0)]]
    )
    assert period(steps[-1]) != P3


def test_n1_refutation_rule():
    day = learned.DAY
    lone, own = 0, 1
    base = [(at(0, 18) % day, at(0, 18) % day, 1, lone)] * 6
    def refuted(extra):
        return learned.sparse_robust_refutation(base + extra, P3)
    admin = (at(0, 21) % day, at(0, 21) % day, 1, own)
    lone_admin = (at(0, 21) % day, at(0, 21) % day, 1, lone)
    assert not refuted([admin])  # one occurrence never decides (existing rule)
    assert not refuted([admin, admin])  # two own interval endpoints: the guard
    assert refuted([admin, admin, admin])  # three refuting occurrences suffice
    assert refuted([admin, lone_admin])  # an independent lone refuter: existing rule
    assert refuted([lone_admin, lone_admin])
    # Three-element entries (no marks) keep the existing behaviour.
    assert refuted([item[:3] for item in (admin, admin)])


# --------------------------------------------------------------------------
# F1 + C1: one witnessed interval is one identification occurrence (M2)
# --------------------------------------------------------------------------


EXPECTED_EVENINGS = {2: 6, 3: 4, 4: 4, 6: 4}
BASE_HOUR = {2: 20, 3: 21, 4: 20, 6: 18}


@pytest.mark.parametrize("hours", (2, 3, 4, 6))
@pytest.mark.parametrize(
    "shape", ["fixed", "varied", "jitter"],
)
def test_f1_exactly_two_restarts_per_session_establishes_the_correct_cycle(hours, shape):
    sessions = evenings(
        hours * H,
        EXPECTED_EVENINGS[hours] + 4,
        base_hour=BASE_HOUR[hours],
        vary=shape == "varied",
        jitter=60.0 if shape == "jitter" else 0.0,
        seed=3,
    )
    _record, steps = production_sessions(sessions)
    established_at = first_countdown(steps)
    assert established_at == EXPECTED_EVENINGS[hours]
    # Never before two witnessed intervals, and always the true period.
    assert established_at >= authority.INTERVAL_IDENTIFICATION_MINIMUM_INTERVALS
    assert all(period(step) in (None, hours * H) for step in steps)
    assert steps[-1].decision.state is State.ESTABLISHED
    assert steps[-1].consumer().scheduled_warning_eligible


def test_f1_long_running_two_restart_history_stays_established_and_is_a_fixed_point():
    record, steps = production_sessions(evenings(P3, 45, base_hour=21))
    assert all(countdown(step) for step in steps[3:])
    again = integ.rebuild(
        record,
        tuple(integ.record_step(record).events),
        (),
    )
    assert integ.record_step(again).decision.state is State.ESTABLISHED


def test_f1_one_interval_plus_three_lone_restarts_still_learns():
    pipeline = s8.sparse_pipeline(s8.SPARSE_DISCRIMINATING[:3])
    pipeline.observe("interval", [at(9, 3), at(9, 6)])
    step = s8.settled(pipeline)
    assert step.decision.state in {State.NORMAL_LEARNING, State.LIKELY}
    assert not countdown(step)


def test_f1_true_four_hour_observed_two_per_session_never_becomes_two_hours():
    _record, steps = production_sessions(evenings(P4, 8, base_hour=20))
    assert all(period(step) in (None, P4) for step in steps)
    assert period(steps[-1]) == P4


def test_f1_same_clock_with_one_witnessed_long_interval_stays_ambiguous():
    sessions = [[at(day, 9)] for day in range(10)] + [[at(11, 9), at(11, 21)]]
    _record, steps = production_sessions(sessions)
    assert not countdown(steps[-1])


def test_f1_interrupted_interval_never_identifies_or_verifies():
    from dataclasses import replace

    pipeline = s8.Pipeline()
    for day in range(6):
        events = pipeline.observe(f"evening-{day}", [at(day, 21), at(day + 1, 0)])
        for event in events:
            pipeline.events[pipeline.events.index(event)] = replace(
                event,
                coverage_complete=False,
                lifecycle_interruption=s8.detection.LifecycleMarker.SLEEP_GAP,
            )
    step = s8.settled(pipeline)
    assert not countdown(step)


@pytest.mark.parametrize("capacity", (2, 3, 5, 20, 80))
def test_f1_compact_never_establishes_earlier_than_raw(capacity):
    pipeline = s8.Pipeline()
    for index, times in enumerate(evenings(P3, 8, base_hour=21)):
        pipeline.observe(f"evening-{index}", times)
    batches = ca.batches_of(pipeline, 2)
    raw = ca.raw_steps(pipeline, batches)
    runner = ca.CompactOnly(pipeline, capacity=capacity)
    compact = [runner.step(batch) for batch in batches]
    assert_compact_never_shows_more(raw, compact)
    for raw_step, compact_step in zip(raw, compact):
        assert compact_step.decision.combined_confidence <= raw_step.decision.combined_confidence + 1e-9
    if capacity >= 20:
        for index, (raw_step, compact_step) in enumerate(zip(raw, compact)):
            ca.assert_same_display_bounded(raw_step, compact_step, f"step {index}")


def _interval_only_change(build, *, same_clock):
    pipeline, step = build()
    seen = []
    for index in range(7):
        base = at(10 + index, 1 if same_clock else 1 + 4 * (index % 4))
        pipeline.observe(f"interval-{index}", [base, base + P4])
        step = st3.advance(pipeline, step)
        seen.append(step)
    return pipeline, seen


@pytest.mark.parametrize("build", [st3.e1, st3.e2], ids=["e1", "e2"])
@pytest.mark.parametrize("same_clock", [False, True], ids=["varied", "same_clock"])
def test_c1_separate_witnessed_intervals_support_a_genuine_period_change(build, same_clock):
    pipeline, steps = _interval_only_change(build, same_clock=same_clock)
    switched = next(index + 1 for index, step in enumerate(steps) if period(step) == P4)
    assert 4 <= switched <= 5
    for step in steps[2 : switched - 1]:
        assert not countdown(step)  # the old cycle is suspected, not trusted
    final = steps[-1]
    assert countdown(final) and period(final) == P4
    superseded = [item for item in final.decision.regimes if item.status is s8.authority.RegimeRecordStatus.SUPERSEDED]
    assert any(item.candidate_period_seconds == P3 for item in superseded)
    st3.assert_fixed_point(pipeline, final)


def test_c1_strong_cycle_never_switches_on_one_or_two_intervals():
    _pipeline, steps = _interval_only_change(st3.e2, same_clock=True)
    assert all(period(step) == P3 for step in steps[:2])


# --------------------------------------------------------------------------
# P1: a restart both schedules predict never dilutes the challenger
# --------------------------------------------------------------------------


def test_p1_both_grid_restart_leaves_the_challenger_domain_but_counter_evidence_stays():
    pipeline, original = st3.e2()
    st3.add_sparse(pipeline, st3.FOUR_HOUR, "four")
    step = st3.advance(pipeline, original)
    incumbent = step.decision.incumbent_shadow_regime
    explained = (P3, incumbent.phase_offset, incumbent.phase_tolerance)
    challenger = next(
        item for item in step.decision.candidates if item.candidate_period_seconds == P4
    )

    def support(extra):
        events = tuple(sorted((*step.events, *extra), key=lambda item: item.canonical_phase_at))
        sparse = authority._sparse_evidence(events, None)
        return authority._sparse_support(
            period=P4,
            phase_offset=challenger.phase_offset,
            sparse=sparse,
            excluded_event_ids=frozenset(),
            after_at=incumbent.established_at,
            calibrated=True,
            explained_by=explained,
        )

    session = s8.Session("both", at(15, 21) - 1200, at(15, 21) + 1200)
    both_grid = s8.physical_event(9001, at(15, 21), session, outcome=s8.Outcome.CORROBORATED_OFFLINE_RESTART)
    session = s8.Session("three-only", at(15, 0) - 1200, at(15, 0) + 1200)
    three_only = s8.physical_event(9002, at(15, 0), session, outcome=s8.Outcome.CORROBORATED_OFFLINE_RESTART)
    base = support(())
    assert authority._sparse_off_phase(
        both_grid, period=P4, phase_offset=challenger.phase_offset
    ) is False
    assert support((both_grid,)).consistency == base.consistency
    assert support((three_only,)).consistency < base.consistency


# --------------------------------------------------------------------------
# Learning Blocked status line
# --------------------------------------------------------------------------


class _Label:
    def __init__(self):
        self.visible = False

    def set_visible(self, value):
        self.visible = bool(value)


def test_learning_blocked_panel_method_toggles_only_its_line():
    panel = SimpleNamespace(learning_blocked_label=_Label())
    ServerCompanionPanel.set_learning_blocked(panel, True)
    assert panel.learning_blocked_label.visible
    ServerCompanionPanel.set_learning_blocked(panel, False)
    assert not panel.learning_blocked_label.visible


def test_window_shows_learning_blocked_only_for_a_blocked_selected_server():
    calls = []
    blocked = {"bad:1"}

    def server_persistence_block(key):
        calls.append(key)
        return SimpleNamespace(reason_code="server_quarantined") if key in blocked else None

    shown = []
    panel = SimpleNamespace(set_learning_blocked=lambda value: shown.append(value))
    runtime = SimpleNamespace(server_persistence_block=server_persistence_block)
    host = SimpleNamespace(server_companion_panel=panel, _companion_restart_phase2=runtime)
    DZLLWindow._refresh_server_companion_learning_blocked(host, "bad:1")
    DZLLWindow._refresh_server_companion_learning_blocked(host, "good:1")
    DZLLWindow._refresh_server_companion_learning_blocked(host, None)
    assert shown == [True, False, False]
    # Only the authoritative per-server status is read; nothing else runs.
    assert calls == ["bad:1", "good:1"]


def test_window_learning_blocked_tolerates_runtime_failures_and_old_panels():
    def broken(_key):
        raise RuntimeError("backend closed")

    shown = []
    host = SimpleNamespace(
        server_companion_panel=SimpleNamespace(set_learning_blocked=shown.append),
        _companion_restart_phase2=SimpleNamespace(server_persistence_block=broken),
    )
    DZLLWindow._refresh_server_companion_learning_blocked(host, "k:1")
    assert shown == [False]
    host.server_companion_panel = SimpleNamespace()
    DZLLWindow._refresh_server_companion_learning_blocked(host, "k:1")


def test_learning_blocked_text_and_tooltip():
    assert server_companion_ui.LEARNING_BLOCKED_TEXT == "Error: Learning Blocked"
    assert "could not be validated" in server_companion_ui.LEARNING_BLOCKED_TOOLTIP
    assert "suspended to protect the stored data" in server_companion_ui.LEARNING_BLOCKED_TOOLTIP


def _gtk_available():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        return bool(Gtk.init_check())
    except Exception:
        return False


@pytest.mark.skipif(not _gtk_available(), reason="GTK 4 display unavailable")
def test_real_gtk_panel_learning_blocked_line():
    panel = ServerCompanionPanel()
    label = panel.learning_blocked_label
    assert label.get_text() == "Error: Learning Blocked"
    assert label.has_css_class("ping-orange")
    assert "could not be validated" in label.get_tooltip_text()
    assert not label.get_visible()
    panel.set_learning_blocked(True)
    assert label.get_visible()
    panel.set_learning_blocked(False)
    assert not label.get_visible()
    panel.set_learning_blocked(True)
    panel.clear_server()
    assert not label.get_visible()
    # The awaiting tooltip and normal presentation are unchanged.
    assert "Likely every 3 hours" in server_companion_ui.restart_presentation_tooltip(
        {
            "presentation_key": server_companion_ui.AWAITING_CYCLE_VERIFICATION_KEY,
            "awaiting_period_seconds": 3 * 3600,
        }
    )


# --------------------------------------------------------------------------
# Shared-endpoint period change (old and new grids share one restart)
# --------------------------------------------------------------------------


def _four_hour_incumbent(kind, outcome):
    pipeline = s8.Pipeline()
    if kind == "e1":
        pipeline.sparse("four", [at(day, (4 * day + 4) % 24) for day in range(8)], outcome=outcome)
        pipeline.observe("verify", [at(8, 12), at(8, 16)], outcome=outcome)
    else:
        for day in range(8):
            pipeline.observe(f"base-{day}", [at(day, 20), at(day + 1, 0)], outcome=outcome)
    return pipeline


def _three_hour_incumbent(kind, outcome):
    return (st3.e1 if kind == "e1" else st3.e2)()[0]


def _shared_endpoint_run(base, sessions, *, outcome, capacity=80, misses=None):
    """Raw and compact (JSON-reopened learned state) after each evening."""

    pipeline = base
    seed_ids = {item.event_id for item in pipeline.events}
    ledger = []
    for index, times in enumerate(sessions):
        pipeline.observe(f"change-{index}", times, outcome=outcome)
        if misses is not None:
            ledger.append(st3.miss(*misses(index)))
    ordered = sorted(pipeline.events, key=lambda item: item.canonical_phase_at)
    seed = [item for item in ordered if item.event_id in seed_ids]
    # One rebuild per monitoring session, as the runtime does; the last seed
    # batch is the established starting point (index 0).
    seed_chains = list(dict.fromkeys(item.continuity_chain_id for item in seed))
    seed_batches = [[item for item in seed if item.continuity_chain_id == chain] for chain in seed_chains]
    batches = list(seed_batches)
    for index, times in enumerate(sessions):
        chain = s8.Session(f"change-{index}", min(times) - 1200, max(times) + 1200).chain
        batches.append([item for item in ordered if item.continuity_chain_id == chain])
    ledgers = [
        tuple(item for item in ledger if item.window_end_at <= batch[-1].canonical_phase_at + 1)
        for batch in batches
    ]
    raw = ca.raw_steps(pipeline, batches, ledgers=ledgers)
    runner = ca.CompactOnly(pipeline, capacity=capacity)
    compact = [runner.step(batch, windows=ledgers[index]) for index, batch in enumerate(batches)]
    skip = len(seed_batches) - 1
    return raw[skip:], compact[skip:]


def _assert_change(steps, *, old, new, adopt_at):
    changed = steps[1:]
    assert countdown(changed[0]) and period(changed[0]) == old  # one interval: internal only
    for step in changed[1 : adopt_at - 1]:
        assert period(step) == old and not countdown(step)  # old prediction withheld
        assert st3.regime(step).candidate_period_seconds == old  # regime kept for rollback
    assert period(changed[adopt_at - 1]) == new
    assert countdown(changed[adopt_at]) and period(changed[adopt_at]) == new
    superseded = [
        item
        for item in changed[-1].decision.regimes
        if item.status is s8.authority.RegimeRecordStatus.SUPERSEDED
    ]
    assert any(item.candidate_period_seconds == old for item in superseded)


OUTCOMES = {
    "oo": (s8.Outcome.CORROBORATED_OFFLINE_RESTART, 4),
    "qv": (s8.Outcome.STRONG_QUERY_VISIBLE_RESTART, 5),
}


@pytest.mark.parametrize("kind", ["e1", "e2"])
@pytest.mark.parametrize("evidence", ["oo", "qv"])
@pytest.mark.parametrize("monitored", [False, True], ids=["unmonitored", "monitored_miss"])
def test_shared_endpoint_four_to_three_hour_change_is_adopted(kind, evidence, monitored):
    outcome, adopt_at = OUTCOMES[evidence]
    sessions = [[at(10 + day, 21), at(11 + day, 0)] for day in range(8)]
    raw, compact = _shared_endpoint_run(
        _four_hour_incumbent(kind, outcome),
        sessions,
        outcome=outcome,
        misses=(lambda index: (P4, at(10 + index, 20), f"old-20-{index}")) if monitored else None,
    )
    for path in (raw, compact):
        _assert_change(path, old=P4, new=P3, adopt_at=adopt_at)
    assert_compact_never_shows_more(raw, compact)


@pytest.mark.parametrize("kind", ["e1", "e2"])
def test_shared_endpoint_three_to_four_hour_change_is_adopted(kind):
    outcome = s8.Outcome.CORROBORATED_OFFLINE_RESTART
    sessions = [[at(10 + day, 20), at(11 + day, 0)] for day in range(8)]
    raw, compact = _shared_endpoint_run(_three_hour_incumbent(kind, outcome), sessions, outcome=outcome)
    for path in (raw, compact):
        _assert_change(path, old=P3, new=P4, adopt_at=4)


@pytest.mark.parametrize("kind", ["e1", "e2"])
def test_shared_endpoint_single_admin_restart_keeps_the_old_cycle(kind):
    outcome = s8.Outcome.CORROBORATED_OFFLINE_RESTART
    sessions = [[at(10, 21), at(11, 0)]] + [[at(10 + day, 20), at(11 + day, 0)] for day in range(1, 6)]
    raw, compact = _shared_endpoint_run(_four_hour_incumbent(kind, outcome), sessions, outcome=outcome)
    for step in (*raw[1:], *compact[1:]):
        assert period(step) == P4 and countdown(step)


@pytest.mark.parametrize("capacity", (3, 5, 20))
def test_shared_endpoint_change_compact_never_shows_more_than_raw(capacity):
    outcome = s8.Outcome.CORROBORATED_OFFLINE_RESTART
    sessions = [[at(10 + day, 21), at(11 + day, 0)] for day in range(8)]
    raw, compact = _shared_endpoint_run(
        _four_hour_incumbent("e2", outcome), sessions, outcome=outcome, capacity=capacity
    )
    assert_compact_never_shows_more(raw, compact)
    if capacity >= 8:
        assert period(compact[-1]) == P3 and countdown(compact[-1])
    # Below eight retained events the two-restart history itself cannot be
    # identified compactly (folded interval left endpoints are not recoverable
    # from schema 5): compact stays in learning, never ahead of raw.


def test_shared_endpoint_change_on_the_production_path_with_reopen():
    sessions = [[at(day, 20), at(day + 1, 0)] for day in range(8)]
    sessions += [[at(day, 21), at(day + 1, 0)] for day in range(8, 16)]
    _record, steps = production_sessions(sessions)
    assert period(steps[7]) == P4 and countdown(steps[7])
    assert countdown(steps[8]) and period(steps[8]) == P4  # first changed interval
    assert not countdown(steps[9]) and not countdown(steps[10])  # 2nd, 3rd: withheld
    assert period(steps[11]) == P3  # adopted at the 4th
    assert all(countdown(step) and period(step) == P3 for step in steps[12:])
