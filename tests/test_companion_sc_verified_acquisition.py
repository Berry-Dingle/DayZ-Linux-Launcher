"""Verified acquisition (Stage 1): witnessed-interval verification and status.

Sparse evidence identifies a candidate, but a cycle is newly established only
once at least one fully witnessed restart-to-restart interval at the
candidate's period and phase exists (``_full_interval_quality > 0``, whenever
it was witnessed).  Until then the candidate is a LIKELY regime shown as
"Awaiting Cycle Verification" with its learning confidence, no countdown and
no scheduled warning.  Regimes established before verified acquisition are
grandfathered: never downgraded, earned confidence unchanged.

Synthetic data only; no runtime, profile, learning file or network.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace

import pytest

import test_companion_restart_compact_authority as compact_authority
import test_companion_restart_compact_cutover as r4
import test_companion_restart_learned_state_integration as integration
import test_companion_sc_historical_acquisition_recovery as har
import test_companion_sc_learning_policy_stage8_baseline as s8
from dzll_launcher import companion_restart_phase2_authority as authority
from dzll_launcher import companion_restart_phase2_authority_consumers as consumers4
from dzll_launcher import companion_restart_phase2_consumer_state as consumer_state
from dzll_launcher import companion_restart_phase2_detection as detection
from dzll_launcher import companion_restart_phase2_learned_state as learned
from dzll_launcher import companion_restart_phase2_schema4 as schema4
from dzll_launcher import companion_restart_phase2_scoring as scoring
from dzll_launcher import server_companion_ui


HOUR = scoring.HOUR
P3 = s8.P3
at = s8.at
State = authority.RegimeState
Presentation = consumers4.AuthorityPresentationKey
Outcome = s8.Outcome
QV = Outcome.STRONG_QUERY_VISIBLE_RESTART
OO = Outcome.CORROBORATED_OFFLINE_RESTART
AWAITING = authority.AWAITING_VERIFICATION_REASON
VERIFIED = authority.VERIFIED_ESTABLISHMENT_REASON
SPARSE = authority.SPARSE_ESTABLISHMENT_REASON
AWAITING_TEXT = "Awaiting Cycle Verification"
LEARNING_TEXT = "Learning Restart Pattern"
EIGHT = s8.SPARSE_DISCRIMINATING
INTERVAL_AFTER = (at(8, 9), at(8, 12))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def awaiting_pipeline():
    return s8.sparse_pipeline()


def with_interval(pipeline, times=INTERVAL_AFTER, *, outcome=OO, name="witnessed"):
    pipeline.observe(name, times, outcome=outcome)
    return pipeline


def assert_awaiting(step, *, period=P3):
    value = step.consumer()
    shown = s8.panel(step)
    regime = step.decision.selected_shadow_regime
    assert step.decision.state is State.LIKELY
    assert AWAITING in step.decision.reason_codes
    assert regime.status is authority.RegimeRecordStatus.LIKELY
    assert regime.candidate_period_seconds == period
    assert value.presentation_state is Presentation.AWAITING_CYCLE_VERIFICATION
    assert value.awaiting_period_seconds == period
    assert value.cycle_period_seconds is None and not value.visible_cycle
    assert shown["cycle_text"] == AWAITING_TEXT
    assert shown["confidence_label"] == "Learning Confidence:"
    assert shown["confidence_visible"]
    assert not shown["countdown_visible"] and not shown["next_visible"]
    assert not value.countdown_visible and value.next_expected_restart_at is None
    assert not value.scheduled_warning_eligible and value.scheduled_warning_suppression_key is None
    return value, shown


def evaluate_interval_only(times, *, outcome=OO, mutate=None):
    """Eight sparse restarts plus one candidate interval session."""

    pipeline = awaiting_pipeline()
    events = pipeline.observe("candidate-interval", times, outcome=outcome)
    if mutate is not None:
        mutate(pipeline, events)
    return s8.settled(pipeline)


@contextlib.contextmanager
def pre_verification_build(monkeypatch):
    """Acquire as the build before verified acquisition did.

    For a history without any witnessed interval this yields exactly the
    decision the previous build persisted: the verification gate passes and
    the marker is already among the regime's base reasons (so none is added).
    """

    with monkeypatch.context() as patch:
        patch.setattr(authority, "_witnessed_interval_verifies", lambda **_kwargs: True)
        patch.setattr(authority, "VERIFIED_ESTABLISHMENT_REASON", "regime_remains_reversible")
        yield


# --------------------------------------------------------------------------
# 1. Learning Restart Pattern with increasing learning confidence
# --------------------------------------------------------------------------


def test_learning_status_shows_growing_learning_confidence():
    # Stage 2: confirmed (unpopulated) offline restarts weigh 0.8, so four of
    # them (effective 3.2) are still learning.
    _pipeline, steps = s8.incremental(s8.Pipeline, EIGHT[:4], outcome=Outcome.CONFIRMED_OFFLINE_RESTART)
    shown = [s8.panel(step) for step in steps[1:]]
    for item in shown:
        assert item["cycle_text"] == LEARNING_TEXT
        assert item["confidence_label"] == "Learning Confidence:"
        assert item["confidence_visible"]
        assert not item["countdown_visible"]
    values = [item["confidence_percent"] for item in shown]
    assert values == sorted(values) and values[0] < values[-1] <= 79
    assert all(step.decision.state is State.NORMAL_LEARNING for step in steps)


# --------------------------------------------------------------------------
# 2-5. Awaiting: status, exact text, tooltip, learning confidence, no
# countdown or warning
# --------------------------------------------------------------------------


def test_identified_sparse_candidate_without_interval_awaits_verification():
    step = s8.settled(awaiting_pipeline())
    value, shown = assert_awaiting(step)
    assert shown["confidence_percent"] == 79
    assert step.decision.combined_confidence == pytest.approx(0.798)
    assert not step.decision.prediction_usable and not step.decision.countdown_safe
    # No warning at any time, not even at a would-be occurrence.
    for offset in (0, 300, 3 * HOUR - 300):
        later = step.consumer(now=step.now + offset)
        assert not later.scheduled_warning_eligible
        assert not later.scheduled_pre_restart_alert_eligible
        assert not later.countdown_visible
    # Awaiting is a fixed point (a rebuild alone never promotes it).
    rebuilt = awaiting_pipeline().evaluate(s8.persisted(step.decision))
    assert rebuilt.decision == awaiting_pipeline().evaluate(s8.persisted(rebuilt.decision)).decision
    assert rebuilt.decision.state is State.LIKELY


@pytest.mark.parametrize("hours", [2, 3, 4, 6, 8, 12])
def test_awaiting_tooltip_names_the_candidate_period(hours):
    presentation = server_companion_ui.restart_learning_presentation(
        {
            "presentation_key": "awaiting_cycle_verification",
            "cycle_text": AWAITING_TEXT,
            "confidence_percent": 79,
            "confidence_label": "Learning Confidence:",
            "awaiting_period_seconds": hours * HOUR,
        }
    )
    assert presentation["cycle_text"] == AWAITING_TEXT
    assert server_companion_ui.restart_presentation_tooltip(presentation) == (
        f"Likely every {hours} hours - Keep monitoring through one complete restart "
        "interval to confirm this schedule."
    )


def test_awaiting_text_and_tooltip_end_to_end():
    step = s8.settled(awaiting_pipeline())
    shown = s8.panel(step)
    summary = consumers4.authority_consumer_summary(step.consumer(), now=step.now)

    assert summary["cycle_text"] == AWAITING_TEXT
    assert summary["presentation_key"] == "awaiting_cycle_verification"
    assert summary["awaiting_period_seconds"] == P3
    assert summary["countdown_text"] == "--" and summary["next_text"] == "--"
    assert server_companion_ui.restart_presentation_tooltip(shown) == (
        "Likely every 3 hours - Keep monitoring through one complete restart interval "
        "to confirm this schedule."
    )
    # Learning and established tooltips are unchanged.
    assert server_companion_ui.restart_presentation_tooltip({"presentation_key": "pattern_only"}) == (
        server_companion_ui.RESTART_PRESENTATION_TOOLTIPS["pattern_only"]
    )
    assert server_companion_ui.restart_presentation_tooltip(
        {"presentation_key": "awaiting_cycle_verification"}
    ) is None


def test_awaiting_text_fits_the_existing_panel_row():
    assert len(AWAITING_TEXT) <= len("Prediction temporarily suspended")
    assert server_companion_ui.ServerCompanionPanel.WIDTH == 280


def test_learning_confidence_is_shown_in_learning_and_awaiting_states():
    learning = s8.panel(
        s8.settled(s8.sparse_pipeline(EIGHT[:4], outcome=Outcome.CONFIRMED_OFFLINE_RESTART))
    )
    awaiting = s8.panel(s8.settled(awaiting_pipeline()))
    for shown in (learning, awaiting):
        assert shown["confidence_label"] == "Learning Confidence:"
        assert shown["confidence_visible"]
        assert 1 <= shown["confidence_percent"] <= 79
    assert learning["confidence_percent"] < awaiting["confidence_percent"]


def test_window_summary_helpers_keep_the_awaiting_text():
    step = s8.settled(awaiting_pipeline())
    summary = {
        **consumers4.authority_consumer_summary(step.consumer(), now=step.now),
        "cycle_period_seconds": None,
    }
    kept = server_companion_ui.companion_display_confidence_summary(
        summary, (P3, 0.4), period_seconds=None
    )
    assert kept["cycle_text"] == AWAITING_TEXT
    assert kept["confidence_label"] == "Learning Confidence:"


# --------------------------------------------------------------------------
# 6-9. Verification and confidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome,percent", [(OO, 90), (QV, 80)], ids=["oo", "qv"])
def test_one_witnessed_interval_establishes_identified_candidate(outcome, percent):
    awaiting = s8.settled(awaiting_pipeline())
    pipeline = with_interval(awaiting_pipeline(), outcome=outcome)
    step = pipeline.evaluate(s8.persisted(awaiting.decision))
    value, shown = s8.assert_sparse_established(step)
    regime = step.decision.incumbent_shadow_regime

    assert VERIFIED in regime.reason_codes and SPARSE in regime.reason_codes
    assert regime.regime_id == awaiting.decision.selected_shadow_regime.regime_id
    assert shown["cycle_text"] == "Every 3 Hours"
    # Offline-observed: the base interval weight (about 90 %).  Query-visible:
    # its endpoints are identification evidence, so 80 % until a later
    # witnessed interval adds the weight.
    assert shown["confidence_percent"] == percent
    due = step.consumer(now=value.next_expected_restart_at - 300)
    assert due.scheduled_warning_eligible
    assert s8.settled(pipeline).decision.state is State.ESTABLISHED


def test_interval_witnessed_before_identification_counts():
    pipeline = s8.Pipeline()
    with_interval(pipeline, (at(-3, 3), at(-3, 6)))
    steps = []
    prior = None
    for index, when in enumerate(EIGHT[:4]):
        pipeline.sparse(f"inc-{index}", [when])
        step = pipeline.evaluate(prior)
        prior = s8.persisted(step.decision)
        steps.append(step)
    # Stage 2: identified at the fourth independent occurrence, and verified
    # at once by the interval witnessed days earlier.
    assert all(step.decision.state is not State.ESTABLISHED for step in steps[:-1])
    s8.assert_sparse_established(steps[-1])
    assert s8.percent(steps[-1]) == 90
    assert VERIFIED in steps[-1].decision.incumbent_shadow_regime.reason_codes


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: with_interval(s8.Pipeline()), id="interval_only"),
        pytest.param(
            lambda: with_interval(s8.sparse_pipeline(EIGHT[:2])), id="interval_and_two_sparse"
        ),
        pytest.param(
            lambda: with_interval(s8.sparse_pipeline(EIGHT[:3])), id="interval_and_three_sparse"
        ),
        pytest.param(
            lambda: with_interval(s8.sparse_pipeline(EIGHT[:4], outcome=Outcome.CONFIRMED_OFFLINE_RESTART)),
            id="interval_and_four_weaker",
        ),
    ],
)
def test_interval_without_sufficient_identification_does_not_establish(build):
    step = s8.settled(build())
    assert step.decision.state in {State.NORMAL_LEARNING, State.LIKELY}
    assert not step.consumer().countdown_visible
    assert s8.percent(step) <= 79


def test_two_independent_intervals_strengthen_confidence():
    one = s8.settled(with_interval(awaiting_pipeline()))
    two = s8.settled(with_interval(with_interval(awaiting_pipeline()), (at(9, 15), at(9, 18)), name="second"))
    assert s8.percent(one) == 90
    assert s8.percent(two) == 95
    assert two.decision.combined_confidence > one.decision.combined_confidence
    qv = s8.settled(s8.verify(awaiting_pipeline()))
    qv_later = qv
    pipeline = s8.verify(awaiting_pipeline())
    qv_later = pipeline.evaluate(s8.persisted(qv.decision))
    for index, times in enumerate(((at(9, 9), at(9, 12)), (at(10, 15), at(10, 18)))):
        with_interval(pipeline, times, outcome=QV, name=f"later-{index}")
        qv_later = pipeline.evaluate(s8.persisted(qv_later.decision))
        assert s8.percent(qv_later) == (90 if index == 0 else 95)


# --------------------------------------------------------------------------
# 10-12. Invalid intervals never verify
# --------------------------------------------------------------------------


def _endpoint(outcome=None, **changes):
    def mutate(pipeline, events):
        index = pipeline.events.index(events[-1])
        value = events[-1]
        if outcome is not None:
            value = replace(value, outcome=outcome)
        pipeline.events[index] = replace(value, **changes)

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_endpoint(Outcome.AMBIGUOUS_DRAIN), id="drain_endpoint"),
        pytest.param(_endpoint(Outcome.SERVICE_INTERRUPTION), id="service_interruption_endpoint"),
        pytest.param(_endpoint(coverage_complete=False), id="incomplete_endpoint"),
        pytest.param(
            _endpoint(
                coverage_complete=False,
                lifecycle_interruption=detection.LifecycleMarker.SLEEP_GAP,
            ),
            id="lifecycle_interrupted_endpoint",
        ),
        pytest.param(_endpoint(authenticity=0.2), id="inauthentic_endpoint"),
    ],
)
def test_invalid_endpoints_do_not_verify(mutate):
    step = evaluate_interval_only(INTERVAL_AFTER, mutate=mutate)
    assert_awaiting(step)


def test_local_connectivity_gap_invalidates_verification():
    # The session is ended by the local-connectivity guard (SLEEP_GAP): the
    # two restarts are in different continuity chains, never an interval.
    pipeline = awaiting_pipeline()
    pipeline.observe("before-gap", [INTERVAL_AFTER[0]], tail=60 * 60)
    pipeline.observe("after-gap", [INTERVAL_AFTER[1]], lead=60 * 60)
    step = s8.settled(pipeline)
    assert not step.high_direct(P3)
    assert_awaiting(step)


@pytest.mark.parametrize(
    "middle",
    [
        pytest.param(OO, id="intervening_restart"),
        pytest.param(Outcome.AMBIGUOUS_DRAIN, id="intervening_unobservable_episode"),
    ],
)
def test_intervening_episode_invalidates_the_interval(middle):
    pipeline = awaiting_pipeline()
    s8.observe_mixed(
        pipeline,
        "with-middle",
        [(INTERVAL_AFTER[0], OO), (at(8, 10, 30), middle), (INTERVAL_AFTER[1], OO)],
    )
    step = s8.settled(pipeline)
    assert_awaiting(step)


def test_interval_at_another_phase_or_period_does_not_verify():
    off_phase = s8.settled(with_interval(awaiting_pipeline(), (at(8, 10), at(8, 13))))
    other_period = s8.settled(with_interval(awaiting_pipeline(), (at(8, 9), at(8, 13))))
    assert off_phase.decision.state is not State.ESTABLISHED
    assert other_period.decision.state is not State.ESTABLISHED


# --------------------------------------------------------------------------
# 13. Harmonic and competing-period safeguards
# --------------------------------------------------------------------------


def test_witnessed_interval_never_overrides_harmonic_or_period_ambiguity():
    # A true 6 h server: a witnessed 6 h interval, divisors unresolved.
    six = s8.settled(s8.verify(s8.sparse_pipeline(s8.SIX_HOUR_SPARSE), (at(-2, 0), at(-2, 6))))
    assert six.decision.state is not State.ESTABLISHED
    assert not six.consumer().countdown_visible
    # Same clock time every day plus a witnessed 3 h interval.
    same_clock = s8.settled(with_interval(s8.sparse_pipeline(s8.SAME_CLOCK), (at(9, 9), at(9, 12))))
    assert same_clock.decision.state is not State.ESTABLISHED
    # An in-phase genuine miss withholds identification despite an interval.
    pipeline = with_interval(awaiting_pipeline())
    missed = pipeline.evaluate(ledger=(s8.genuine_miss(P3, at(3, 0), key="miss"),))
    assert missed.decision.state is not State.ESTABLISHED


def test_competing_h2_schedule_still_withholds_establishment():
    pipeline = with_interval(awaiting_pipeline())
    pipeline.observe("imprecise-four-hour", [at(-2, 1), at(-2, 5) + 68, at(-2, 9)])
    step = pipeline.evaluate()
    assert step.candidate(4 * HOUR).h2_gate
    assert step.decision.state is not State.ESTABLISHED


# --------------------------------------------------------------------------
# 14. QV verification survives compact folding and reopening
# --------------------------------------------------------------------------


def _qv_first_history():
    pipeline = s8.Pipeline()
    s8.verify(pipeline)
    pipeline.sparse("sparse", EIGHT)
    return pipeline


@pytest.mark.parametrize("capacity", [2, 3, 5])
def test_folded_qv_interval_still_verifies_with_raw_parity(capacity):
    pipeline = _qv_first_history()
    batches = compact_authority.batches_of(pipeline, 1)
    raw = compact_authority.raw_steps(pipeline, batches)
    runner = compact_authority.CompactOnly(pipeline, capacity=capacity)
    verify_ids = {item.event_id for item in pipeline.events[:2]}
    for index, batch in enumerate(batches):
        compact = runner.step(batch)
        label = (capacity, index)
        assert compact_authority.decision_summary(compact.decision) == (
            compact_authority.decision_summary(raw[index].decision)
        ), label
        if raw[index].decision.state is not State.NORMAL_LEARNING:
            # (In early learning the harness's schema-3 projection counts
            # ring events only; production feeds it the runtime horizon.)
            compact_authority.assert_same(raw[index], compact, label)
    assert verify_ids.isdisjoint({item.event_id for item in runner.ring})
    assert [item.count for item in runner.state.verifications] == [1]
    bin_ = runner.state.verifications[0]
    assert bin_.period == P3
    s8.assert_sparse_established(compact)
    assert compact.decision.incumbent_shadow_regime.reason_codes == (
        raw[-1].decision.incumbent_shadow_regime.reason_codes
    )


def test_production_rebuild_keeps_folded_qv_verification_and_reopens(monkeypatch, tiny_ring):
    tiny_ring(2)
    pipeline = _qv_first_history()
    records = compact_authority.run(pipeline, every=1, compact_from=0, monkeypatch=monkeypatch)
    final = records[-1]
    state = schema4.persisted_learned_state(final)
    assert state.schema_version == learned.LEARNED_STATE_SCHEMA_VERSION == 5
    assert state.verifications and len(final["physical_events"]) <= 2
    decision = schema4.persisted_authority_decision(final)
    assert decision.state is State.ESTABLISHED
    assert VERIFIED in decision.incumbent_shadow_regime.reason_codes
    reopened = json.loads(schema4.canonical_json_bytes(final))
    again = integration.rebuild(reopened, pipeline.events, pipeline.coverage)
    assert again["authority_decision"] == final["authority_decision"]
    assert again["learned_state"] == final["learned_state"]
    assert not schema4._validate_server_record(s8.SERVER, again)


# Reuse the cutover module's ring fixture.
tiny_ring = r4.tiny_ring


def test_verification_bin_existence_is_exact_at_the_tolerance_edge():
    state = learned.empty_learned_state(s8.SERVER)
    tolerance = scoring.candidate_phase_tolerance(P3)
    item = learned.VerificationBin(P3, 10, 2, 10.0, 200.0, at(0, 0), at(0, 3))
    state = replace(state, verifications=(item,))
    inside = item.start + 200.0 + tolerance
    outside = item.start + 200.0 + tolerance + 1.0
    assert learned.compact_witnessed_interval_exists(
        state, period=P3, phase_offsets=(inside,), tolerance=tolerance
    )
    assert not learned.compact_witnessed_interval_exists(
        state, period=P3, phase_offsets=(outside,), tolerance=tolerance
    )
    assert not learned.compact_witnessed_interval_exists(
        state, period=4 * HOUR, phase_offsets=(inside,), tolerance=tolerance
    )


# --------------------------------------------------------------------------
# 15. Schema-3/4 histories upgrade without evidence loss; schema 5 format
# --------------------------------------------------------------------------


def _folded_state():
    pipeline = _qv_first_history()
    pipeline.observe("oo-interval", INTERVAL_AFTER)
    step = s8.settled(pipeline)
    state = learned.empty_learned_state(s8.SERVER)
    for unit in learned.chain_fold_units(step.events):
        state = learned.fold_unit(state, events=unit, relationships=step.relationships)
    return learned.observe_decision(state, step.decision)


def _legacy_mapping(state, version):
    data = learned.learned_state_to_mapping(state)
    data["schema_version"] = version
    data.pop("verifications")
    data["epoch"]["values"] = data["epoch"]["values"][:3]
    if version == 3:
        del data["followers"]
    return json.loads(json.dumps(data))


def test_schema_5_round_trip_keeps_verification_bins():
    state = _folded_state()
    # The QV and the OO interval share the 3 h phase bin.
    assert sum(item.count for item in state.verifications) == 2
    payload = learned.learned_state_json_bytes(state)
    data = json.loads(payload)
    assert data["schema_version"] == 5
    assert len(data["verifications"]) == len(state.verifications)
    assert len(data["epoch"]["values"]) == 4
    assert learned.learned_state_from_json_bytes(payload) == state


@pytest.mark.parametrize("version", [3, 4])
def test_older_learned_states_load_with_every_folded_bin(version):
    state = replace(_folded_state(), followers=())
    loaded = learned.learned_state_from_mapping(_legacy_mapping(state, version))
    assert loaded.schema_version == 5
    assert loaded.verifications == ()  # nothing reconstructed
    for name in ("pool", "intervals", "misses", "normal_relationships", "watermark", "incumbent"):
        assert getattr(loaded, name) == getattr(state, name), name
    assert loaded.epoch.confirmation_count == state.epoch.confirmation_count
    assert loaded.epoch.confirmation_residual == state.epoch.confirmation_residual
    assert loaded.epoch.confirmation_base_residual == (
        1.0 if state.epoch.confirmation_count == 0 else None
    )
    # Rewritten as schema 5 and stable from then on.
    again = learned.learned_state_from_json_bytes(learned.learned_state_json_bytes(loaded))
    assert again == loaded


def test_legacy_epoch_with_folded_confirmations_has_unknown_base_residual():
    state = _folded_state()
    epoch = replace(state.epoch, confirmation_count=2, confirmation_residual=0.1, confirmation_base_residual=0.3)
    data = _legacy_mapping(replace(state, epoch=epoch), 4)
    loaded = learned.learned_state_from_mapping(data)
    assert loaded.epoch.confirmation_count == 2
    assert loaded.epoch.confirmation_residual == pytest.approx(0.1)
    assert loaded.epoch.confirmation_base_residual is None
    round_trip = learned.learned_state_from_json_bytes(learned.learned_state_json_bytes(loaded))
    assert round_trip.epoch.confirmation_base_residual is None


def test_legacy_high_intervals_still_verify_conservatively_after_upgrade():
    state = replace(_folded_state(), followers=())
    loaded = learned.learned_state_from_mapping(_legacy_mapping(state, 4))
    phase = s8.at(0, 3) % P3
    tolerance = scoring.candidate_phase_tolerance(P3)
    # The folded offline-observed interval is still in its interval bin.
    assert learned.compact_witnessed_interval_exists(
        loaded, period=P3, phase_offsets=(phase,), tolerance=tolerance
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data["verifications"][0].__setitem__(2, 0), id="zero_count"),
        pytest.param(lambda data: data["verifications"][0].__setitem__(1, 10**6), id="index_range"),
        pytest.param(lambda data: data["verifications"][0].__setitem__(0, 7), id="bad_period"),
        pytest.param(lambda data: data["verifications"][0].__setitem__(3, 301.0), id="phase_range"),
        pytest.param(lambda data: data["verifications"][0].__setitem__(4, -1.0), id="inverted_phase"),
        pytest.param(lambda data: data["verifications"][0].__setitem__(6, 0.0), id="inverted_times"),
        pytest.param(lambda data: data["verifications"].append(list(data["verifications"][0])), id="duplicate"),
        pytest.param(lambda data: data["verifications"][0].append(1.0), id="extra_field"),
        pytest.param(lambda data: data["epoch"]["values"].pop(), id="short_epoch"),
        pytest.param(lambda data: data.update(schema_version=4), id="v4_with_verifications"),
        pytest.param(lambda data: data["epoch"]["values"].__setitem__(3, 1.5), id="base_residual_range"),
        pytest.param(
            lambda data: data["epoch"]["values"].__setitem__(3, None) or data["epoch"]["values"].__setitem__(1, 0),
            id="unknown_base_without_confirmations",
        ),
    ],
)
def test_validation_rejects_malformed_schema_5_state(mutate):
    data = json.loads(learned.learned_state_json_bytes(_folded_state()))
    mutate(data)
    with pytest.raises(learned.LearnedStateError):
        learned.learned_state_from_mapping(data)


def test_verification_storage_is_bounded():
    pipeline = s8.Pipeline()
    for day in range(30):
        for hour in range(0, 24, 4):
            pipeline.observe(f"pair-{day}-{hour}", [at(day, hour, day % 5), at(day, hour + 3, day % 5)])
    step = pipeline.evaluate()
    state = learned.empty_learned_state(s8.SERVER)
    for unit in learned.chain_fold_units(step.events):
        state = learned.fold_unit(state, events=unit, relationships=step.relationships)
    assert state.verifications
    assert len(state.verifications) <= learned.INTERVAL_BIN_TOTAL
    assert sum(item.count for item in state.verifications) >= 30
    assert learned.learned_state_entry_count(state) <= learned.LEARNED_STATE_MAX_ENTRIES
    assert len(learned.learned_state_json_bytes(state)) <= learned.LEARNED_STATE_MAX_JSON_BYTES


# --------------------------------------------------------------------------
# 16. Existing established records are never downgraded
# --------------------------------------------------------------------------


def _legacy_sparse_established(monkeypatch, pipeline):
    with pre_verification_build(monkeypatch):
        first = pipeline.evaluate()
        established = pipeline.evaluate(s8.persisted(first.decision))
    regime = established.decision.incumbent_shadow_regime
    assert established.decision.state is State.ESTABLISHED
    assert SPARSE in regime.reason_codes and VERIFIED not in regime.reason_codes
    return established


def test_existing_sparse_established_regime_without_interval_is_never_downgraded(monkeypatch):
    pipeline = awaiting_pipeline()
    legacy = _legacy_sparse_established(monkeypatch, pipeline)
    before = legacy.decision.incumbent_shadow_regime
    step = pipeline.evaluate(s8.persisted(legacy.decision))
    for _ in range(3):
        step = pipeline.evaluate(s8.persisted(step.decision))
    regime = step.decision.incumbent_shadow_regime

    assert step.decision.state is State.ESTABLISHED
    assert (regime.regime_id, regime.phase_offset, regime.established_at) == (
        before.regime_id, before.phase_offset, before.established_at,
    )
    assert VERIFIED not in regime.reason_codes
    assert step.decision.combined_confidence == legacy.decision.combined_confidence == pytest.approx(0.798)
    value = step.consumer()
    assert value.presentation_state is Presentation.CONFIRMED_CYCLE
    assert value.countdown_visible
    assert step.consumer(now=value.next_expected_restart_at - 300).scheduled_warning_eligible


def test_existing_regime_keeps_the_legacy_confirmation_weight(monkeypatch):
    pipeline = awaiting_pipeline()
    legacy = _legacy_sparse_established(monkeypatch, pipeline)
    with_interval(pipeline)
    confirmed = pipeline.evaluate(s8.persisted(legacy.decision))
    assert authority.SPARSE_CONFIRMATION_REASON in confirmed.decision.reason_codes
    assert VERIFIED not in confirmed.decision.incumbent_shadow_regime.reason_codes
    # The Stage 8.5 confirmation (0.75) is kept for existing regimes: 95 %.
    assert s8.percent(confirmed) == 95
    # A newly verified regime with the same evidence shows about 90 %.
    assert s8.percent(s8.settled(with_interval(awaiting_pipeline()))) == 90


def test_existing_red_dawn_like_recovery_without_interval_stays_established(monkeypatch):
    pipeline = har.red_dawn_like(verified=False)
    legacy = _legacy_sparse_established(monkeypatch, pipeline)
    assert har.recovered(legacy)
    before = legacy.decision.incumbent_shadow_regime
    step = pipeline.evaluate(s8.persisted(legacy.decision))
    step = pipeline.evaluate(s8.persisted(step.decision))
    regime = step.decision.incumbent_shadow_regime
    assert step.decision.state is State.ESTABLISHED and har.recovered(step)
    assert (regime.regime_id, regime.phase_offset, regime.established_at) == (
        before.regime_id, before.phase_offset, before.established_at,
    )
    assert {item for item in regime.reason_codes if item.startswith("time_shift_")} == {
        item for item in before.reason_codes if item.startswith("time_shift_")
    }
    assert step.decision.combined_confidence == legacy.decision.combined_confidence
    assert step.consumer().countdown_visible
    # The same history as a new acquisition awaits verification instead.
    fresh = pipeline.evaluate()
    assert fresh.decision.state is State.LIKELY and AWAITING in fresh.decision.reason_codes
    assert fresh.decision.selected_shadow_regime.candidate_period_seconds == 4 * HOUR


def test_new_recovery_with_a_witnessed_interval_establishes():
    step = har.settle(har.red_dawn_like(), har.red_dawn_like().evaluate())
    har.assert_recovered_four_hour(step)
    assert VERIFIED in step.decision.incumbent_shadow_regime.reason_codes


def test_time_shift_adoption_needs_no_new_witnessed_cycle():
    pipeline = s8.verified_sparse_pipeline()
    original = s8.settled(pipeline)
    step = original
    for index, when in enumerate((at(9, 1), at(10, 7), at(11, 16))):
        pipeline.sparse(f"shift-{index}", [when])
        step = pipeline.evaluate(s8.persisted(step.decision))
    regime = step.decision.incumbent_shadow_regime
    assert authority.TIME_SHIFT_ADOPTED_REASON in regime.reason_codes
    assert VERIFIED in regime.reason_codes and step.decision.state is State.ESTABLISHED
    assert step.consumer().countdown_visible


def test_production_upgrade_of_a_legacy_record_keeps_establishment(monkeypatch):
    pipeline = awaiting_pipeline()
    with pre_verification_build(monkeypatch):
        record = integration.drive(pipeline, every=4)
    before = schema4.persisted_authority_decision(record)
    assert before.state is State.ESTABLISHED
    upgraded = integration.rebuild(record, pipeline.events, pipeline.coverage)
    again = integration.rebuild(json.loads(schema4.canonical_json_bytes(upgraded)), pipeline.events, pipeline.coverage)
    after = schema4.persisted_authority_decision(again)
    assert after.state is State.ESTABLISHED
    assert after.incumbent_shadow_regime.regime_id == before.incumbent_shadow_regime.regime_id
    assert after.incumbent_shadow_regime.established_at == before.incumbent_shadow_regime.established_at
    assert len(again["physical_events"]) == len(record["physical_events"])
    assert len(again["regimes"]) == len(record["regimes"])
    assert not schema4._validate_server_record(s8.SERVER, again)
    assert again["authority_decision"] == upgraded["authority_decision"]


# --------------------------------------------------------------------------
# 17. Real-shape regressions: Bobs-like continuity, Outcasts-like confirmed
# sparse, Red Dawn-like recovery (above)
# --------------------------------------------------------------------------


def test_continuity_established_histories_are_unchanged():
    two = s8.three_hour_chain(3).evaluate()
    three = s8.three_hour_chain(4).evaluate()
    assert two.decision.state is three.decision.state is State.ESTABLISHED
    assert s8.percent(two) == 80 and s8.percent(three) == 95
    # Stage 2: continuity establishments are verified by construction and
    # carry the marker (it selects the identification calibration later).
    assert VERIFIED in two.decision.incumbent_shadow_regime.reason_codes
    assert VERIFIED in three.decision.incumbent_shadow_regime.reason_codes


def test_outcasts_like_confirmed_sparse_regime_keeps_its_value(monkeypatch):
    # Sparse-established before verified acquisition, later confirmed by
    # offline-observed intervals: grandfathered, unchanged magnitude.
    pipeline = awaiting_pipeline()
    legacy = _legacy_sparse_established(monkeypatch, pipeline)
    with_interval(pipeline, (at(9, 9), at(9, 12)))
    with_interval(pipeline, (at(10, 12), at(10, 15)), name="second")
    step = pipeline.evaluate(s8.persisted(legacy.decision))
    for _ in range(2):
        again = pipeline.evaluate(s8.persisted(step.decision))
        assert again.decision == step.decision or again.decision.state is State.ESTABLISHED
        step = again
    assert step.decision.state is State.ESTABLISHED
    assert step.decision.incumbent_shadow_regime.regime_id == legacy.decision.incumbent_shadow_regime.regime_id
    assert s8.percent(step) == 95


# --------------------------------------------------------------------------
# 18. Consumer-state, warning and alert compatibility
# --------------------------------------------------------------------------


def test_awaiting_consumer_decision_persists_in_the_consumer_ledger():
    step = s8.settled(awaiting_pipeline())
    value = step.consumer()
    ledger = consumer_state.with_decision(
        consumer_state.ledger_from_mapping(None, server_key=s8.SERVER), value
    )
    data = json.loads(json.dumps(consumer_state.ledger_to_mapping(ledger)))
    restored = consumer_state.ledger_from_mapping(data, server_key=s8.SERVER)
    (record,) = restored.decisions
    assert record.presentation_state == "awaiting_cycle_verification"
    assert record.cycle_period_seconds is None
    assert record.next_expected_restart_at is None
    assert not record.countdown_safe
    assert consumer_state.fired_suppression_keys(restored) == frozenset()


def test_generic_recovery_alert_is_independent_of_the_awaiting_state():
    step = s8.settled(awaiting_pipeline())
    value = step.consumer(physical_recovery_event_id=step.events[-1].event_id)
    assert value.generic_recovery_suppression_key is not None
    assert not value.scheduled_warning_eligible
    assert value.restart_classification_policy is consumers4.AuthorityRestartClassification.NOT_CLASSIFIED


def test_established_display_has_no_new_labels():
    step = s8.settled(with_interval(awaiting_pipeline()))
    shown = s8.panel(step)
    assert shown["cycle_text"] == "Every 3 Hours"
    assert shown["confidence_label"] == "Cycle Confidence:"
    assert shown["countdown_visible"] and shown["next_visible"]
    for word in ("Provisional", "Strong", "Durable", "Verified", "Awaiting"):
        assert word not in shown["cycle_text"]
