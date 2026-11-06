"""Compact persistent learned state for Server Companion (Retention Stage R1).

Schema 4 currently re-derives every candidate, gate, phase and confidence on
each rebuild from the full lifetime raw collections.  This module is the
bounded replacement foundation: a fixed-size per-server summary into which
genuine observations are *folded* once, plus pure evaluation functions that
reproduce the existing Stage 8 statistics from that summary and a small set
of not-yet-folded ("ring") raw items.

Stage R1 scope: representation, folding and evaluation only.  Nothing here is
persisted by the production runtime and production authority does not read
it yet.  It introduces no new confidence policy: every evaluation reuses the
existing schema-3 / schema-4 formulas, thresholds and helpers.  Where the
compact form cannot reproduce an existing calculation exactly, the docstring
of the function says so.

Storage bound.  Every collection is keyed by a bounded key space that does
not depend on the number of observed restarts:

* phase histograms: one bin per UTC minute of the day (1440).  All candidate
  periods divide a day, so ``(t mod day) mod P == t mod P`` and one day
  histogram yields the phase at every period;
* interval, miss and superseded-regime summaries: one entry per
  (candidate period, minute of that period): at most 2100 in total;
* divisor evidence: one entry per (period, exact divisor);
* recovery evidence: the two latest non-overlapping incumbent intervals;
* scalars: watermark, counters, regime knowledge.

No event-ID or timestamp list grows with the number of observations.  The
only ID tuple is the incumbent's ``defining_evidence_ids``, bounded by
``RECENT_RING_CAPACITY``.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping, Sequence

from .companion_restart_phase2_authority import (
    GATE_STREAK_BONUS_COEFFICIENT,
    H1_AUTHORITY_MINIMUM,
    H2_AUTHORITY_MINIMUM,
    H2_PHASE_AUTHORITY_MINIMUM,
    H3_AUTHORITY_MINIMUM,
    H3_PHASE_AUTHORITY_MINIMUM,
    H4_AUTHORITY_MINIMUM,
    HIGH_AUTHORITY_CAP,
    ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS,
    SPARSE_CONFIRMATION_INTERVAL_WEIGHT,
    SPARSE_CONSISTENCY_TOLERANCE,
    SPARSE_ESTABLISHMENT_MINIMUM_PERCENT,
    SPARSE_EVIDENCE_CEILING,
    SPARSE_PERIOD_AMBIGUOUS_REASON,
    SPARSE_PHASE_CONFIDENCE_MINIMUM,
    SPARSE_PHASE_QUALITY_BAND_MINIMUM,
    SPARSE_STRUCTURE_REASON,
    STREAK_BONUS_COEFFICIENT,
    AuthorityDecision,
    RegimeRecord,
    RegimeRecordStatus,
    _authentic_event,
    _circular_distance,
    _full_interval_quality,
    _non_overlapping_streaks,
    _relationship_phase,
    _relationship_phase_value,
    relationship_authority_quality,
)
from .companion_restart_phase2_continuity import (
    CadenceStreak,
    IntervalRelationship,
    RelationshipKind,
)
from .companion_restart_phase2_detection import PhysicalRestartEvent
from .companion_restart_phase2_expected_windows import (
    ExpectedWindowLedgerRecord,
    ExpectedWindowOutcome,
)
from .companion_restart_phase2_runtime import MAX_FINALIZED_EVENTS
from .companion_restart_phase2_scoring import (
    CANDIDATE_PERIODS,
    DAY,
    GATES as SCHEDULE_GATES,
    PHASE_UNCERTAINTY_CAP,
    CandidateScore,
    _event_count_cap,
    _event_weight,
    _signed_phase_residual,
    candidate_phase_tolerance,
    phase_quality_band,
)


LEARNED_STATE_SCHEMA_VERSION = 1
PHASE_BIN_SECONDS = 60
DAY_BINS = DAY // PHASE_BIN_SECONDS
# The ring of not-yet-folded raw events reuses the existing schema-3 event
# horizon; R1 introduces no new retention number.
RECENT_RING_CAPACITY = MAX_FINALIZED_EVENTS
HARMONIC_PERIODS = frozenset({6 * 3600, 8 * 3600, 12 * 3600})
MAX_REASON_CODES = 64
_EPSILON = 1e-9

PERIOD_BIN_TOTAL = sum(period // PHASE_BIN_SECONDS for period in CANDIDATE_PERIODS)
DIVISOR_KEYS = tuple(
    (period, divisor)
    for period in CANDIDATE_PERIODS
    for divisor in CANDIDATE_PERIODS
    if divisor < period and period % divisor == 0
)
# Worst-case number of stored entries, independent of observation count.
LEARNED_STATE_MAX_ENTRIES = (
    DAY_BINS  # pool
    + len(CANDIDATE_PERIODS) * DAY_BINS  # high-interval endpoints
    + PERIOD_BIN_TOTAL  # interval summaries
    + PERIOD_BIN_TOTAL  # miss/hit summaries
    + len(DIVISOR_KEYS)
    + PERIOD_BIN_TOTAL  # superseded regimes
    + PERIOD_BIN_TOTAL  # epoch challenger summaries
    + PERIOD_BIN_TOTAL  # epoch confirmation summaries
    + len(CANDIDATE_PERIODS) * DAY_BINS  # epoch confirmation endpoints
    + ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS
    + 1  # incumbent
)


class LearnedStateError(ValueError):
    """A learned-state mapping is malformed or exceeds its storage bound."""


# --------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseBin:
    """Folded restarts sharing one UTC minute of the day."""

    index: int
    weight: float
    count: int
    offset_sum: float
    first_at: float
    last_at: float
    uncertainty: float
    # Pool histogram only: the running pool weight total just before this
    # bin's first restart was folded.  Folding is time-ordered, so the pool
    # weight at or after ``first_at`` is ``pool_weight_total - weight_before``
    # (the exact consistency-domain total for a domain starting here).
    weight_before: float = 0.0

    @property
    def seconds(self) -> float:
        """Mean time of day of the folded restarts (a phase source)."""

        return self.index * PHASE_BIN_SECONDS + self.offset_sum / self.count


@dataclass(frozen=True)
class StreakSummary:
    interval_count: int
    event_count: int
    min_quality: float
    phase_estimate: float
    phase_spread: float
    start_at: float
    end_at: float


@dataclass(frozen=True)
class IntervalBin:
    """Folded continuous evidence at one (period, minute of period).

    ``base_residual`` is the exact running product ``prod(1 - 0.5 q)`` of the
    high-eligible DIRECT relationships (the schema-4 base-authority input);
    ``full_interval_residual`` is ``prod(1 - 0.75 q)`` over the same
    relationships that are full intervals (sparse confirmation input).  Folded
    maximal streaks contribute their exact bonus terms.
    """

    period: int
    index: int
    relationship_count: int = 0
    offset_sum: float = 0.0
    base_residual: float = 1.0
    full_interval_count: int = 0
    full_interval_residual: float = 1.0
    first_at: float | None = None
    last_at: float | None = None
    streak_count: int = 0
    streak_bonus: float = 0.0
    gate_bonus: float = 0.0
    best_streak: StreakSummary | None = None

    @property
    def phase(self) -> float:
        count = self.relationship_count
        if count <= 0 and self.best_streak is not None:
            return self.best_streak.phase_estimate % self.period
        return (
            self.index * PHASE_BIN_SECONDS
            + (self.offset_sum / count if count else PHASE_BIN_SECONDS / 2)
        ) % self.period


@dataclass(frozen=True)
class MissBin:
    """Folded expected-window outcomes at one (period, minute of period)."""

    period: int
    index: int
    genuine_miss_count: int = 0
    last_genuine_miss_at: float | None = None
    last_hit_at: float | None = None


@dataclass(frozen=True)
class DivisorEvidence:
    """Best schema-3 divisor resolution observed for (period, divisor)."""

    period: int
    divisor: int
    covered_window_count: int
    longer_interval_count: int


@dataclass(frozen=True)
class SupersededRegime:
    """A superseded regime boundary (evidence-separation key only)."""

    period: int
    phase_offset: float
    phase_tolerance: float
    superseded_at: float

    @property
    def key(self) -> tuple[int, int]:
        return (self.period, int((self.phase_offset % self.period) // PHASE_BIN_SECONDS))


@dataclass(frozen=True)
class RegimeKnowledge:
    """The incumbent regime as learned knowledge (survives evidence removal)."""

    regime_id: str
    period: int
    phase_offset: float
    phase_tolerance: float
    origin: str
    authority_gate: str
    status: str
    high_authority: float
    phase_authority: float
    combined_confidence: float
    established_at: float | None
    last_aligned_evidence_at: float | None
    defining_evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryInterval:
    left_at: float
    right_at: float


@dataclass(frozen=True)
class EpochEvidence:
    """Evidence scoped to the current incumbent establishment boundary.

    ``challenger`` holds high relationships starting at or after the
    incumbent's ``established_at`` (the Stage 6 post-establishment view);
    ``confirmation`` and ``confirmation_endpoints`` hold non-high full
    intervals ending after it (Stage 8.5 sparse confirmation); ``recovery``
    holds the two latest non-overlapping incumbent intervals (Stage 6
    original-cycle recovery).  The epoch is reset when the incumbent identity
    or its establishment boundary changes, so this window is bounded.
    """

    regime_id: str | None = None
    established_at: float | None = None
    challenger: tuple[IntervalBin, ...] = ()
    confirmation: tuple[IntervalBin, ...] = ()
    confirmation_endpoints: tuple[tuple[int, tuple[PhaseBin, ...]], ...] = ()
    recovery: tuple[RecoveryInterval, ...] = ()


@dataclass(frozen=True)
class FoldWatermark:
    """Protection against double credit.

    Raw items at or before ``(at, event_id)`` have been folded; an event at or
    before the watermark is never credited again (a replayed, re-imported or
    late event is rejected for learning).
    """

    at: float | None = None
    event_id: str | None = None
    folded_event_count: int = 0
    folded_learning_event_count: int = 0
    window_end_at: float | None = None


@dataclass(frozen=True)
class LearnedState:
    schema_version: int
    server_key: str
    watermark: FoldWatermark = field(default_factory=FoldWatermark)
    pool_weight_total: float = 0.0
    pool: tuple[PhaseBin, ...] = ()
    high_endpoints: tuple[tuple[int, tuple[PhaseBin, ...]], ...] = ()
    intervals: tuple[IntervalBin, ...] = ()
    misses: tuple[MissBin, ...] = ()
    divisors: tuple[DivisorEvidence, ...] = ()
    incumbent: RegimeKnowledge | None = None
    superseded: tuple[SupersededRegime, ...] = ()
    epoch: EpochEvidence = field(default_factory=EpochEvidence)


def empty_learned_state(server_key: str) -> LearnedState:
    if not isinstance(server_key, str) or not server_key:
        raise LearnedStateError("server_key must be a non-empty string")
    return LearnedState(LEARNED_STATE_SCHEMA_VERSION, server_key)


def learned_state_entry_count(state: LearnedState) -> int:
    """Number of stored entries; never exceeds ``LEARNED_STATE_MAX_ENTRIES``."""

    epoch = state.epoch
    return (
        len(state.pool)
        + sum(len(bins) for _period, bins in state.high_endpoints)
        + len(state.intervals)
        + len(state.misses)
        + len(state.divisors)
        + len(state.superseded)
        + len(epoch.challenger)
        + len(epoch.confirmation)
        + sum(len(bins) for _period, bins in epoch.confirmation_endpoints)
        + len(epoch.recovery)
        + (1 if state.incumbent is not None else 0)
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _event_at(event: PhysicalRestartEvent) -> float | None:
    return event.canonical_phase_at


def _capped_uncertainty(value: float) -> float:
    return min(PHASE_UNCERTAINTY_CAP, max(0.0, float(value)))


def _day_bin(at: float) -> tuple[int, float]:
    seconds = float(at) % DAY
    index = min(DAY_BINS - 1, int(seconds // PHASE_BIN_SECONDS))
    return index, seconds - index * PHASE_BIN_SECONDS


def _period_bin(value: float, period: int) -> tuple[int, float]:
    value = float(value) % period
    index = min(period // PHASE_BIN_SECONDS - 1, int(value // PHASE_BIN_SECONDS))
    return index, value - index * PHASE_BIN_SECONDS


def _watermark_key(state: LearnedState) -> tuple[float, str] | None:
    mark = state.watermark
    if mark.at is None:
        return None
    return (mark.at, mark.event_id or "")


def _newer(event: PhysicalRestartEvent, mark: tuple[float, str] | None) -> bool:
    at = _event_at(event)
    if at is None:
        return False
    return mark is None or (at, event.event_id) > mark


def _sparse_usable(event: PhysicalRestartEvent) -> bool:
    """The schema-4 sparse pool predicate (``authority._sparse_evidence``)."""

    return bool(
        _authentic_event(event) and _event_weight(event) > 0 and _event_at(event) is not None
    )


def _bin_add(
    bins: dict[int, PhaseBin],
    at: float,
    weight: float,
    uncertainty: float,
    *,
    weight_before: float = 0.0,
) -> None:
    index, offset = _day_bin(at)
    existing = bins.get(index)
    if existing is None:
        bins[index] = PhaseBin(index, weight, 1, offset, at, at, uncertainty, weight_before)
        return
    bins[index] = PhaseBin(
        index,
        existing.weight + weight,
        existing.count + 1,
        existing.offset_sum + offset,
        min(existing.first_at, at),
        max(existing.last_at, at),
        max(existing.uncertainty, uncertainty),
        existing.weight_before,
    )


def _period_histograms(
    values: tuple[tuple[int, tuple[PhaseBin, ...]], ...]
) -> dict[int, dict[int, PhaseBin]]:
    result = {period: {} for period in CANDIDATE_PERIODS}
    for period, bins in values:
        result[period] = {item.index: item for item in bins}
    return result


def _freeze_period_histograms(
    values: Mapping[int, Mapping[int, PhaseBin]]
) -> tuple[tuple[int, tuple[PhaseBin, ...]], ...]:
    return tuple(
        (period, tuple(values[period][key] for key in sorted(values[period])))
        for period in CANDIDATE_PERIODS
        if values.get(period)
    )


def _median_of_counts(values: list[tuple[float, int]]) -> float:
    """``statistics.median`` of a multiset given as (value, multiplicity)."""

    ordered = sorted((value, count) for value, count in values if count > 0)
    total = sum(count for _value, count in ordered)
    if total <= 0:
        raise LearnedStateError("median of an empty multiset")

    def nth(position: int) -> float:
        seen = 0
        for value, count in ordered:
            seen += count
            if position < seen:
                return value
        return ordered[-1][0]

    if total % 2:
        return nth(total // 2)
    return (nth(total // 2 - 1) + nth(total // 2)) / 2


def _interval_bin_add_relationship(
    existing: IntervalBin | None,
    relationship: IntervalRelationship,
    quality: float,
    full_quality: float,
) -> IntervalBin:
    period = relationship.candidate_period_seconds
    index, offset = _period_bin(_relationship_phase_value(relationship, period), period)
    value = existing or IntervalBin(period, index)
    return replace(
        value,
        relationship_count=value.relationship_count + 1,
        offset_sum=value.offset_sum + offset,
        base_residual=value.base_residual * (1.0 - 0.50 * quality),
        full_interval_count=value.full_interval_count + int(full_quality > 0),
        full_interval_residual=(
            value.full_interval_residual
            * (1.0 - SPARSE_CONFIRMATION_INTERVAL_WEIGHT * full_quality)
            if full_quality > 0
            else value.full_interval_residual
        ),
        first_at=(
            relationship.left_at
            if value.first_at is None
            else min(value.first_at, relationship.left_at)
        ),
        last_at=(
            relationship.right_at
            if value.last_at is None
            else max(value.last_at, relationship.right_at)
        ),
    )


def _streak_shape(interval_count: int, minimum_quality: float) -> float:
    return (1.0 - 2.0 ** -(interval_count - 1)) * minimum_quality


def _interval_bin_add_streak(
    existing: IntervalBin | None, streak: CadenceStreak, minimum_quality: float
) -> IntervalBin:
    period = streak.candidate_period_seconds
    index, _offset = _period_bin(streak.phase_estimate, period)
    value = existing or IntervalBin(period, index)
    shape = _streak_shape(streak.interval_count, minimum_quality)
    summary = StreakSummary(
        interval_count=streak.interval_count,
        event_count=streak.event_count,
        min_quality=round(minimum_quality, 6),
        phase_estimate=streak.phase_estimate,
        phase_spread=streak.phase_spread,
        start_at=streak.start_at,
        end_at=streak.end_at,
    )
    best = value.best_streak
    if best is None or (summary.interval_count, summary.end_at) > (
        best.interval_count,
        best.end_at,
    ):
        best = summary
    return replace(
        value,
        streak_count=value.streak_count + 1,
        streak_bonus=value.streak_bonus + round(STREAK_BONUS_COEFFICIENT * shape, 6),
        gate_bonus=value.gate_bonus + round(GATE_STREAK_BONUS_COEFFICIENT * shape, 6),
        best_streak=best,
    )


def _greedy_recovery(intervals: Iterable[RecoveryInterval]) -> tuple[RecoveryInterval, ...]:
    """The latest non-overlapping intervals, chosen backwards (at most two).

    Two non-overlapping intervals starting after any time T exist exactly when
    the backward-greedy pair (latest start, then the latest start ending at or
    before it) both start after T.
    """

    ordered = sorted(
        set(intervals), key=lambda item: (item.left_at, item.right_at), reverse=True
    )
    selected: list[RecoveryInterval] = []
    boundary = math.inf
    for item in ordered:
        if item.right_at <= boundary:
            selected.append(item)
            boundary = item.left_at
        if len(selected) >= ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS:
            break
    return tuple(sorted(selected, key=lambda item: (item.left_at, item.right_at)))


def _incumbent_recovery_interval(
    relationship: IntervalRelationship,
    incumbent: RegimeKnowledge,
    event_map: Mapping[str, PhysicalRestartEvent],
) -> bool:
    """``authority._original_cycle_recovery_confirmations`` eligibility."""

    period = incumbent.period
    tolerance = incumbent.phase_tolerance
    if (
        not relationship.high_authority_eligible
        or relationship.relationship_kind is not RelationshipKind.DIRECT
        or relationship.intervening_event_ids
        or relationship.candidate_period_seconds != period
        or _circular_distance(
            _relationship_phase_value(relationship, period), incumbent.phase_offset, period
        )
        > tolerance
    ):
        return False
    left = event_map.get(relationship.left_event_id)
    right = event_map.get(relationship.right_event_id)
    if left is None or right is None:
        return False
    if not all(
        _authentic_event(value)
        and _event_at(value) is not None
        and _circular_distance(_event_at(value), incumbent.phase_offset, period) <= tolerance
        for value in (left, right)
    ):
        return False
    return relationship_authority_quality(relationship, (left, right)) > 0.0


# --------------------------------------------------------------------------
# Folding genuine observations
# --------------------------------------------------------------------------


def chain_fold_units(
    events: Iterable[PhysicalRestartEvent],
) -> tuple[tuple[PhysicalRestartEvent, ...], ...]:
    """Group events into fold units: one continuity chain each, oldest first.

    High-eligible relationships and streaks never cross a continuity chain,
    so folding whole chains never splits a relationship or a streak.  Events
    without a chain are their own unit.
    """

    groups: dict[object, list[PhysicalRestartEvent]] = {}
    for event in events:
        if _event_at(event) is None:
            continue
        key = event.continuity_chain_id or ("event", event.event_id)
        groups.setdefault(key, []).append(event)
    units = [
        tuple(sorted(values, key=lambda item: (_event_at(item), item.event_id)))
        for values in groups.values()
    ]
    return tuple(sorted(units, key=lambda unit: (_event_at(unit[0]), unit[0].event_id)))


def ring_fold_units(
    events: Iterable[PhysicalRestartEvent], *, capacity: int = RECENT_RING_CAPACITY
) -> tuple[tuple[tuple[PhysicalRestartEvent, ...], ...], tuple[PhysicalRestartEvent, ...]]:
    """Split raw events into (units to fold, events kept in the ring).

    The oldest whole chains are folded until at most ``capacity`` events
    remain; the newest chain is never folded (it may still be extended).  A
    single chain longer than ``capacity`` stays whole in the ring: splitting
    it would split a streak (documented R1 limitation, not exercised by
    ordinary monitoring).
    """

    units = list(chain_fold_units(events))
    remaining = sum(len(unit) for unit in units)
    folded = []
    while len(units) > 1 and remaining > capacity:
        unit = units.pop(0)
        folded.append(unit)
        remaining -= len(unit)
    ring = tuple(event for unit in units for event in unit)
    return tuple(folded), ring


def fold_unit(
    state: LearnedState,
    *,
    events: Sequence[PhysicalRestartEvent],
    relationships: Iterable[IntervalRelationship] = (),
    streaks: Iterable[CadenceStreak] = (),
) -> LearnedState:
    """Fold one closed unit (normally one whole continuity chain).

    Events at or before the watermark are rejected; relationships and streaks
    are folded only when every event they name is folded in this unit.
    Ineligible or non-authentic events contribute no positive or negative
    evidence (AGENTS learning invariant) but still advance the watermark.
    Epoch-scoped evidence uses the incumbent recorded by ``observe_decision``.
    """

    mark = _watermark_key(state)
    accepted: dict[str, PhysicalRestartEvent] = {}
    for event in events:
        if event.server_key != state.server_key or not _newer(event, mark):
            continue
        accepted.setdefault(event.event_id, event)
    if not accepted:
        return state
    ordered = sorted(accepted.values(), key=lambda item: (_event_at(item), item.event_id))

    pool = {item.index: item for item in state.pool}
    endpoints = _period_histograms(state.high_endpoints)
    intervals = {(item.period, item.index): item for item in state.intervals}
    epoch = state.epoch
    challenger = {(item.period, item.index): item for item in epoch.challenger}
    confirmation = {(item.period, item.index): item for item in epoch.confirmation}
    confirmation_endpoints = _period_histograms(epoch.confirmation_endpoints)
    recovery = list(epoch.recovery)
    incumbent = state.incumbent
    last_aligned = None if incumbent is None else incumbent.last_aligned_evidence_at

    learning_count = 0
    pool_total = state.pool_weight_total
    for event in ordered:
        at = _event_at(event)
        if _sparse_usable(event):
            learning_count += 1
            weight = _event_weight(event)
            _bin_add(
                pool,
                at,
                weight,
                _capped_uncertainty(event.phase_uncertainty),
                weight_before=pool_total,
            )
            pool_total += weight
        if (
            incumbent is not None
            and _authentic_event(event)
            and _circular_distance(at, incumbent.phase_offset, incumbent.period)
            <= incumbent.phase_tolerance
        ):
            last_aligned = at if last_aligned is None else max(last_aligned, at)

    quality: dict[str, float] = {}
    endpoint_tags: set[tuple[int, str]] = set()
    confirmation_tags: set[tuple[int, str]] = set()
    unique_relationships = {
        item.relationship_id: item
        for item in relationships
        if item.server_key == state.server_key
        and item.left_event_id in accepted
        and item.right_event_id in accepted
    }
    for relationship in sorted(
        unique_relationships.values(),
        key=lambda item: (item.left_at, item.right_at, item.relationship_id),
    ):
        period = relationship.candidate_period_seconds
        pair = {
            relationship.left_event_id: accepted[relationship.left_event_id],
            relationship.right_event_id: accepted[relationship.right_event_id],
        }
        full_quality = _full_interval_quality(relationship, pair)
        if (
            relationship.high_authority_eligible
            and relationship.relationship_kind is RelationshipKind.DIRECT
        ):
            value = relationship_authority_quality(relationship, pair.values())
            quality[relationship.relationship_id] = value
            key = (period, _period_bin(_relationship_phase_value(relationship, period), period)[0])
            intervals[key] = _interval_bin_add_relationship(
                intervals.get(key), relationship, value, full_quality
            )
            endpoint_tags.update(
                (period, item) for item in (relationship.left_event_id, relationship.right_event_id)
            )
            if epoch.established_at is not None and relationship.left_at >= epoch.established_at:
                challenger[key] = _interval_bin_add_relationship(
                    challenger.get(key), relationship, value, full_quality
                )
            if incumbent is not None and _incumbent_recovery_interval(
                relationship, incumbent, pair
            ):
                recovery.append(RecoveryInterval(relationship.left_at, relationship.right_at))
        elif (
            incumbent is not None
            and epoch.established_at is not None
            and full_quality > 0
            and period == incumbent.period
            and relationship.right_at > epoch.established_at
            and _circular_distance(
                _relationship_phase_value(relationship, period), incumbent.phase_offset, period
            )
            <= incumbent.phase_tolerance
        ):
            key = (period, _period_bin(_relationship_phase_value(relationship, period), period)[0])
            confirmation[key] = _interval_bin_add_relationship(
                confirmation.get(key), relationship, 0.0, full_quality
            )
            confirmation_tags.update(
                (period, item) for item in (relationship.left_event_id, relationship.right_event_id)
            )

    for tags, target in ((endpoint_tags, endpoints), (confirmation_tags, confirmation_endpoints)):
        for period, event_id in sorted(tags):
            event = accepted[event_id]
            if _sparse_usable(event):
                _bin_add(
                    target[period],
                    _event_at(event),
                    _event_weight(event),
                    _capped_uncertainty(event.phase_uncertainty),
                )

    for streak in sorted(
        {
            item.streak_id: item
            for item in streaks
            if item.server_key == state.server_key
            and set(item.event_ids) <= set(accepted)
            and all(value in quality for value in item.relationship_ids)
        }.values(),
        key=lambda item: (item.start_at, item.streak_id),
    ):
        minimum = min(quality[value] for value in streak.relationship_ids)
        key = (
            streak.candidate_period_seconds,
            _period_bin(streak.phase_estimate, streak.candidate_period_seconds)[0],
        )
        intervals[key] = _interval_bin_add_streak(intervals.get(key), streak, minimum)
        if epoch.established_at is not None and streak.start_at >= epoch.established_at:
            challenger[key] = _interval_bin_add_streak(challenger.get(key), streak, minimum)

    last = ordered[-1]
    watermark = FoldWatermark(
        at=_event_at(last),
        event_id=last.event_id,
        folded_event_count=state.watermark.folded_event_count + len(ordered),
        folded_learning_event_count=state.watermark.folded_learning_event_count
        + learning_count,
        window_end_at=state.watermark.window_end_at,
    )
    if incumbent is not None and last_aligned != incumbent.last_aligned_evidence_at:
        incumbent = replace(incumbent, last_aligned_evidence_at=last_aligned)
    return replace(
        state,
        watermark=watermark,
        pool_weight_total=pool_total,
        pool=tuple(pool[key] for key in sorted(pool)),
        high_endpoints=_freeze_period_histograms(endpoints),
        intervals=tuple(intervals[key] for key in sorted(intervals)),
        incumbent=incumbent,
        epoch=replace(
            epoch,
            challenger=tuple(challenger[key] for key in sorted(challenger)),
            confirmation=tuple(confirmation[key] for key in sorted(confirmation)),
            confirmation_endpoints=_freeze_period_histograms(confirmation_endpoints),
            recovery=_greedy_recovery(recovery),
        ),
    )


def latest_window_records(
    records: Iterable[ExpectedWindowLedgerRecord],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values = {item.result_id: item for item in records}
    superseded = {
        item.supersedes_result_id
        for item in values.values()
        if item.supersedes_result_id is not None
    }
    return tuple(
        sorted(
            (item for item in values.values() if item.result_id not in superseded),
            key=lambda item: (item.window_end_at, item.result_id),
        )
    )


def fold_windows(
    state: LearnedState, records: Iterable[ExpectedWindowLedgerRecord]
) -> LearnedState:
    """Fold final expected-window outcomes (latest revision per lineage).

    Only windows ending after the window watermark are folded, so a window is
    credited once.  Callers must fold a window only after its lineage is
    final: a revision arriving after its window was folded is ignored.
    """

    misses = {(item.period, item.index): item for item in state.misses}
    mark = state.watermark.window_end_at
    latest_end = mark
    changed = False
    for record in latest_window_records(
        item for item in records if item.server_key == state.server_key
    ):
        if mark is not None and record.window_end_at <= mark:
            continue
        latest_end = (
            record.window_end_at if latest_end is None else max(latest_end, record.window_end_at)
        )
        period = record.candidate_period_seconds
        index, _offset = _period_bin(record.expected_phase_offset, period)
        value = misses.get((period, index)) or MissBin(period, index)
        if record.outcome is ExpectedWindowOutcome.GENUINE_MISS and record.negative_penalty_active:
            value = replace(
                value,
                genuine_miss_count=value.genuine_miss_count + 1,
                last_genuine_miss_at=max(value.last_genuine_miss_at or 0.0, record.expected_at),
            )
        elif record.outcome is ExpectedWindowOutcome.HIT:
            value = replace(value, last_hit_at=max(value.last_hit_at or 0.0, record.expected_at))
        else:
            continue
        misses[(period, index)] = value
        changed = True
    if latest_end == mark and not changed:
        return state
    return replace(
        state,
        misses=tuple(misses[key] for key in sorted(misses)),
        watermark=replace(state.watermark, window_end_at=latest_end),
    )


def fold_divisor_resolution(state: LearnedState, score) -> LearnedState:
    """Retain the best divisor resolution the schema-3 scorer has observed.

    Schema 3 recomputes divisor resolution from its 7-day / 16-event window
    and 60-day coverage, so it disappears after inactivity.  Here the largest
    covered-window and longer-interval counts per (period, divisor) persist
    for the current regime epoch (they are reset when an incumbent is
    superseded).  This is the intended persistent-knowledge semantics; it is
    more durable than the current recomputation, never more permissive while
    the live evidence is still present.
    """

    stored = {(item.period, item.divisor): item for item in state.divisors}
    for candidate in getattr(score, "candidates", ()):
        for item in candidate.divisor_resolution:
            key = (candidate.period_seconds, item.divisor_seconds)
            if key not in DIVISOR_KEYS:
                continue
            previous = stored.get(key)
            windows = max(
                item.covered_window_count,
                previous.covered_window_count if previous else 0,
            )
            longer = max(
                item.longer_interval_count,
                previous.longer_interval_count if previous else 0,
            )
            if windows or longer:
                stored[key] = DivisorEvidence(key[0], key[1], windows, longer)
    values = tuple(stored[key] for key in sorted(stored))
    return state if values == state.divisors else replace(state, divisors=values)


def _knowledge_from_regime(
    regime: RegimeRecord, *, last_aligned: float | None
) -> RegimeKnowledge:
    return RegimeKnowledge(
        regime_id=regime.regime_id,
        period=regime.candidate_period_seconds,
        phase_offset=regime.phase_offset,
        phase_tolerance=regime.phase_tolerance,
        origin=regime.origin.value,
        authority_gate=regime.authority_gate.value,
        status=regime.status.value,
        high_authority=regime.high_authority,
        phase_authority=regime.phase_authority,
        combined_confidence=regime.combined_confidence,
        established_at=regime.established_at,
        last_aligned_evidence_at=last_aligned,
        defining_evidence_ids=tuple(regime.defining_evidence_ids[:RECENT_RING_CAPACITY]),
        reason_codes=tuple(sorted(set(regime.reason_codes)))[:MAX_REASON_CODES],
    )


def observe_decision(state: LearnedState, decision: AuthorityDecision) -> LearnedState:
    """Record the authority's incumbent and regime boundaries as knowledge.

    The incumbent's identity, period, phase, gate and magnitudes are kept
    (learned knowledge that survives evidence removal).  Superseded regimes
    are kept as separation keys, merged per (period, minute of period); the
    union of their explained evidence is exactly the evidence up to the
    latest ``superseded_at``.  A change of incumbent identity or
    establishment boundary starts a new epoch.
    """

    superseded = {item.key: item for item in state.superseded}
    supersession_seen = False
    for regime in decision.regimes:
        if regime.status is not RegimeRecordStatus.SUPERSEDED or regime.superseded_at is None:
            continue
        value = SupersededRegime(
            regime.candidate_period_seconds,
            regime.phase_offset % regime.candidate_period_seconds,
            regime.phase_tolerance,
            regime.superseded_at,
        )
        previous = superseded.get(value.key)
        if previous is not None:
            if (
                previous.superseded_at >= value.superseded_at
                and previous.phase_tolerance >= value.phase_tolerance
            ):
                continue
            value = replace(
                value,
                phase_offset=previous.phase_offset,
                phase_tolerance=max(previous.phase_tolerance, value.phase_tolerance),
                superseded_at=max(previous.superseded_at, value.superseded_at),
            )
        else:
            supersession_seen = True
        superseded[value.key] = value

    regime = decision.incumbent_shadow_regime
    previous = state.incumbent
    if regime is None:
        incumbent = None
    else:
        same = previous is not None and previous.regime_id == regime.regime_id
        last_aligned = previous.last_aligned_evidence_at if same else None
        if not same:
            last_aligned = _folded_last_aligned(state, regime)
        incumbent = _knowledge_from_regime(regime, last_aligned=last_aligned)
    epoch = state.epoch
    key = (
        None if incumbent is None else incumbent.regime_id,
        None if incumbent is None else incumbent.established_at,
    )
    if key != (epoch.regime_id, epoch.established_at):
        epoch = EpochEvidence(regime_id=key[0], established_at=key[1])
    divisors = () if supersession_seen else state.divisors
    return replace(
        state,
        incumbent=incumbent,
        superseded=tuple(superseded[item] for item in sorted(superseded)),
        epoch=epoch,
        divisors=divisors,
    )


def _folded_last_aligned(state: LearnedState, regime: RegimeRecord) -> float | None:
    """Latest folded aligned restart for a newly observed incumbent.

    Bin-level (minute) phase resolution; the exact time of the bin's latest
    restart is used.
    """

    values = [
        item.last_at
        for item in state.pool
        if _circular_distance(item.seconds, regime.phase_offset, regime.candidate_period_seconds)
        <= regime.phase_tolerance
    ]
    return max(values, default=None)


# --------------------------------------------------------------------------
# Evaluation (pure, no time input)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Point:
    seconds: float
    weight: float
    count: int
    first_at: float
    last_at: float
    uncertainty: float
    event_id: str | None = None


def _bin_point(item: PhaseBin, *, weight: float | None = None, count: int | None = None) -> _Point:
    return _Point(
        item.seconds,
        item.weight if weight is None else weight,
        item.count if count is None else count,
        item.first_at,
        item.last_at,
        item.uncertainty,
    )


def _ring_point(event: PhysicalRestartEvent) -> _Point:
    at = _event_at(event)
    return _Point(
        at,
        _event_weight(event),
        1,
        at,
        at,
        _capped_uncertainty(event.phase_uncertainty),
        event.event_id,
    )


def ring_sparse_events(
    state: LearnedState, events: Iterable[PhysicalRestartEvent]
) -> tuple[PhysicalRestartEvent, ...]:
    """Unfolded sparse-usable events, newer than the watermark, once each."""

    mark = _watermark_key(state)
    values = {
        item.event_id: item
        for item in events
        if item.server_key == state.server_key and _sparse_usable(item) and _newer(item, mark)
    }
    return tuple(sorted(values.values(), key=lambda item: (_event_at(item), item.event_id)))


@dataclass(frozen=True)
class CompactSparseSupport:
    """Mirror of ``authority._SparseSupport`` computed from compact state."""

    value: float
    effective: float
    quality_band: float
    phase_confidence: float
    off_phase_weight: float
    consistency: float
    aligned_count: int
    first_aligned_at: float | None
    last_aligned_at: float | None
    aligned_seconds: tuple[float, ...]


_EMPTY_SUPPORT = CompactSparseSupport(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, None, None, ())


def compact_sparse_support(
    state: LearnedState,
    *,
    period: int,
    phase_offset: float,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    excluded_ring_event_ids: Iterable[str] = (),
    exclude_folded_high_endpoints: bool = False,
    exclude_folded_confirmation_endpoints: bool = False,
) -> CompactSparseSupport:
    """``authority._sparse_support`` over folded bins plus ring events.

    Exact for ring events.  Folded restarts are represented per UTC minute:
    phase residuals use the bin's mean time of day, the bin's largest capped
    uncertainty widens its tolerance, and a bin belongs to the consistency
    domain when its latest restart is at or after the first aligned restart
    (a bin straddling that instant is counted whole).  The candidate's own
    folded high endpoints are those folded at this period within tolerance of
    its phase.  Folded restarts explained by a conflicting superseded regime
    are excluded from the aligned set when the bin's first restart precedes
    the supersession (a straddling bin is excluded whole: old evidence never
    supports a conflicting regime).

    The consistency-domain total is exact: the pool weight folded at or after
    the first aligned restart is ``pool_weight_total - weight_before`` of the
    bin holding it (folding is time-ordered).  Two splits of that total are
    approximated when a single minute bin holds restarts on both sides of
    the domain start: excluded endpoints and superseded-explained restarts in
    such a bin are counted as off-phase.  Both approximations can only lower
    the result, never raise it.
    """

    tolerance = candidate_phase_tolerance(period)
    excluded = set(excluded_ring_event_ids)
    historical = [
        regime
        for regime in state.superseded
        if period != regime.period
        or _circular_distance(phase_offset, regime.phase_offset, period) > regime.phase_tolerance
    ]
    endpoint_bins = (
        _period_histograms(state.high_endpoints)[period] if exclude_folded_high_endpoints else {}
    )
    confirmation_bins = (
        _period_histograms(state.epoch.confirmation_endpoints)[period]
        if exclude_folded_confirmation_endpoints
        else {}
    )

    def aligned_to_candidate(point: _Point) -> bool:
        return abs(
            _signed_phase_residual(point.seconds, phase_offset, period),
        ) <= tolerance + point.uncertainty

    def explained_by_history(point: _Point) -> bool:
        return any(
            point.first_at <= regime.superseded_at
            and _circular_distance(point.seconds, regime.phase_offset, regime.period)
            <= regime.phase_tolerance
            for regime in historical
        )

    points: list[tuple[_Point, bool, PhaseBin | None]] = []
    removed_bins: list[PhaseBin] = []
    for item in state.pool:
        weight, count = item.weight, item.count
        if aligned_to_candidate(_bin_point(item)):
            for removed in (endpoint_bins.get(item.index), confirmation_bins.get(item.index)):
                if removed is not None:
                    weight -= removed.weight
                    count -= removed.count
                    removed_bins.append(removed)
        if count <= 0 or weight <= _EPSILON:
            continue
        point = _bin_point(item, weight=weight, count=count)
        points.append((point, explained_by_history(point), item))
    for event in ring_sparse_events(state, ring_events):
        if event.event_id in excluded:
            continue
        point = _ring_point(event)
        points.append((point, explained_by_history(point), None))

    aligned = [
        (point, source)
        for point, explained, source in points
        if not explained and aligned_to_candidate(point)
    ]
    if not aligned:
        return _EMPTY_SUPPORT
    anchor_point, anchor_bin = min(
        aligned, key=lambda item: (item[0].first_at, item[1] is None)
    )
    first = anchor_point.first_at
    effective = sum(point.weight for point, _source in aligned)
    ring_total = sum(
        point.weight
        for point, _explained, source in points
        if source is None and point.first_at >= first
    )
    if anchor_bin is not None:
        # Exact: every restart folded at or after the first aligned one.
        folded_total = state.pool_weight_total - anchor_bin.weight_before
        for removed in removed_bins:
            # Excluded endpoints are not in the pool.  A removed bin wholly
            # before the domain start never counted; one straddling it is
            # kept (conservative: a larger domain never raises confidence).
            if removed.first_at >= first:
                folded_total -= removed.weight
    else:
        folded_total = 0.0
    total = folded_total + ring_total
    # Historical (superseded-explained) restarts aligned with the candidate
    # stay in the domain but are neither aligned evidence nor off-phase.  A
    # folded bin is counted only when wholly inside the domain; a bin that
    # straddles the domain start is treated as off-phase (conservative: it
    # can only lower confidence).  Ring restarts are exact.
    historical_in_domain = sum(
        point.weight
        for point, explained, _source in points
        if explained and aligned_to_candidate(point) and point.first_at >= first
    )
    off_phase_weight = max(0.0, total - effective - historical_in_domain)
    mad = _median_of_counts(
        [
            (
                max(
                    abs(_signed_phase_residual(point.seconds, phase_offset, period)),
                    point.uncertainty,
                ),
                point.count,
            )
            for point, _source in aligned
        ]
    )
    band = phase_quality_band(mad)
    consistency = effective / total if total > 0 else 0.0
    phase_confidence = _event_count_cap(effective) * band * consistency
    value = (
        _event_count_cap(max(0.0, effective - off_phase_weight))
        * band
        * min(1.0, consistency / SPARSE_CONSISTENCY_TOLERANCE)
    )
    return CompactSparseSupport(
        round(min(1.0, max(0.0, value)), 6),
        effective,
        band,
        round(min(1.0, max(0.0, phase_confidence)), 6),
        round(off_phase_weight, 6),
        round(consistency, 6),
        sum(point.count for point, _source in aligned),
        first,
        max(point.last_at for point, _source in aligned),
        tuple(sorted(point.seconds for point, _source in aligned)),
    )


def compact_phase_proposal(
    state: LearnedState,
    *,
    period: int,
    ring_events: Iterable[PhysicalRestartEvent] = (),
) -> tuple[float | None, float | None, int, float | None]:
    """``scoring._phase_cluster`` over the sparse pool (folded plus ring).

    The anchor of a folded bin is its mean time of day; anchors are visited
    in order of first restart time, as the raw function visits events in
    time order.  The scan is windowed by the largest possible tolerance, so
    its cost is bounded by the (bounded) number of bins, not by restarts.
    """

    points = [_bin_point(item) for item in state.pool]
    points.extend(_ring_point(item) for item in ring_sparse_events(state, ring_events))
    if not points:
        return None, None, 0, None
    tolerance = candidate_phase_tolerance(period)
    radius = tolerance + PHASE_UNCERTAINTY_CAP
    phases = sorted(((point.seconds % period), index) for index, point in enumerate(points))
    extended = [(value - period, index) for value, index in phases] + phases + [
        (value + period, index) for value, index in phases
    ]
    extended_keys = [value for value, _index in extended]

    def aligned_with(anchor_phase: float) -> list[_Point]:
        low = bisect.bisect_left(extended_keys, anchor_phase - radius)
        high = bisect.bisect_right(extended_keys, anchor_phase + radius)
        seen = set()
        values = []
        for _value, index in extended[low:high]:
            if index in seen:
                continue
            seen.add(index)
            point = points[index]
            if abs(
                _signed_phase_residual(point.seconds, anchor_phase, period),
            ) <= tolerance + point.uncertainty:
                values.append(point)
        return values

    best = None
    for anchor in sorted(points, key=lambda item: (item.first_at, item.seconds)):
        anchor_phase = anchor.seconds % period
        aligned = aligned_with(anchor_phase)
        key = (
            sum(point.weight for point in aligned),
            sum(point.count for point in aligned),
            max(point.last_at for point in aligned),
        )
        if best is None or key > best[0]:
            best = (key, anchor_phase, aligned)
    _key, offset, aligned = best
    mad = _median_of_counts(
        [
            (
                max(abs(_signed_phase_residual(point.seconds, offset, period)), point.uncertainty),
                point.count,
            )
            for point in aligned
        ]
    )
    return offset, mad, sum(point.count for point in aligned), max(
        point.last_at for point in aligned
    )


def compact_harmonic_blockers(
    state: LearnedState,
    period: int,
    normal_candidate: CandidateScore | None = None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``authority._harmonic_blockers`` from persisted plus live evidence.

    Divisor resolution is the larger of the persisted best counts and the
    live schema-3 counts, with the scorer's unchanged thresholds (>= 2/2
    established, >= 4/3 high).  Healthy midpoints count folded genuine
    misses at the divisor period plus unfolded active ring misses.
    """

    if period not in HARMONIC_PERIODS:
        return (), ()
    stored = {item.divisor: item for item in state.divisors if item.period == period}
    live = (
        {item.divisor_seconds: item for item in normal_candidate.divisor_resolution}
        if normal_candidate is not None
        else {}
    )
    mark = state.watermark.window_end_at
    ring_misses = [
        item
        for item in latest_window_records(ring_windows)
        if item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and (mark is None or item.window_end_at > mark)
    ]
    h2: list[str] = []
    h3: list[str] = []
    for divisor in (value for value in CANDIDATE_PERIODS if value < period and period % value == 0):
        persisted = stored.get(divisor)
        current = live.get(divisor)
        windows = max(
            persisted.covered_window_count if persisted else 0,
            current.covered_window_count if current else 0,
        )
        intervals = max(
            persisted.longer_interval_count if persisted else 0,
            current.longer_interval_count if current else 0,
        )
        midpoints = sum(
            item.genuine_miss_count for item in state.misses if item.period == divisor
        ) + sum(item.candidate_period_seconds == divisor for item in ring_misses)
        established = bool(
            (windows >= 2 and intervals >= 2)
            or (current is not None and current.established_resolved)
            or midpoints >= 1
        )
        high = bool(
            (windows >= 4 and intervals >= 3)
            or (current is not None and current.high_resolved)
            or midpoints >= 2
        )
        if not established:
            h2.append(f"unresolved_harmonic_divisor_{divisor}")
        if not high:
            h3.append(f"high_harmonic_divisor_unresolved_{divisor}")
    return tuple(h2), tuple(h3)


def compact_sparse_structure_reasons(
    state: LearnedState,
    *,
    period: int,
    support: CompactSparseSupport,
    normal_candidate: CandidateScore | None = None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> tuple[str, ...]:
    """``authority._sparse_structure_reasons`` (SE1-SE5) from compact support."""

    if not support.aligned_count or support.first_aligned_at is None:
        return ()
    first = support.first_aligned_at
    refuted = all(
        any(
            _circular_distance(value, first, multiple) > candidate_phase_tolerance(multiple)
            for value in support.aligned_seconds
        )
        for multiple in CANDIDATE_PERIODS
        if multiple > period and multiple % period == 0
    )
    _h2, h3 = compact_harmonic_blockers(state, period, normal_candidate, ring_windows)
    if not refuted or h3:
        return (SPARSE_PERIOD_AMBIGUOUS_REASON,)
    gate = SCHEDULE_GATES[period]
    if (
        support.effective >= gate.established_events
        and support.last_aligned_at - first >= gate.established_span
        and support.quality_band >= SPARSE_PHASE_QUALITY_BAND_MINIMUM
        and support.phase_confidence >= SPARSE_PHASE_CONFIDENCE_MINIMUM
    ):
        return (SPARSE_STRUCTURE_REASON,)
    return ()


def compact_genuine_miss_since(
    state: LearnedState,
    *,
    period: int,
    phase_offset: float,
    phase_tolerance: float,
    since: float,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> bool:
    """SE6: an active in-phase genuine miss at or after ``since``.

    Folded misses keep their exact latest expected time per (period, minute
    of period); the phase test uses the minute bin (widened by half a bin).
    """

    if any(
        item.period == period
        and item.last_genuine_miss_at is not None
        and item.last_genuine_miss_at >= since
        and _circular_distance(
            item.index * PHASE_BIN_SECONDS + PHASE_BIN_SECONDS / 2, phase_offset, period
        )
        <= phase_tolerance + PHASE_BIN_SECONDS / 2
        for item in state.misses
    ):
        return True
    mark = state.watermark.window_end_at
    return any(
        item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and (mark is None or item.window_end_at > mark)
        and item.candidate_period_seconds == period
        and item.expected_at >= since
        and _circular_distance(item.expected_phase_offset, phase_offset, period) <= phase_tolerance
        for item in latest_window_records(ring_windows)
    )


def sparse_milestone_reached(aligned_normal_confidence: float) -> bool:
    """SE7: the sparse slot alone displays at the 80 % milestone."""

    return (
        int(round(SPARSE_EVIDENCE_CEILING * min(1.0, aligned_normal_confidence) * 100))
        >= SPARSE_ESTABLISHMENT_MINIMUM_PERCENT
    )


def compact_combined_confidence(high_authority: float, aligned_normal_confidence: float) -> float:
    """The unchanged schema-4 combination of continuous and sparse slots."""

    return round(
        high_authority
        + (1.0 - high_authority)
        * min(SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * aligned_normal_confidence),
        6,
    )


@dataclass(frozen=True)
class CompactCandidate:
    """A continuous-evidence candidate synthesised from compact state."""

    period: int
    phase_offset: float
    phase_spread: float
    phase_quality: float
    relationship_count: int
    base_authority: float
    streak_bonus: float
    high_authority: float
    gate_authority: float
    high_phase_authority: float
    aligned_normal_confidence: float
    combined_confidence: float
    h1_gate: bool
    h2_gate: bool
    h3_gate: bool
    h4_gate: bool
    harmonic_blockers: tuple[str, ...]
    first_evidence_at: float | None
    last_evidence_at: float | None
    ring_endpoint_ids: frozenset[str]
    has_folded_evidence: bool
    sparse_reason_codes: tuple[str, ...]


def _ring_high_relationships(
    state: LearnedState,
    relationships: Iterable[IntervalRelationship],
    ring_ids: set[str],
) -> tuple[IntervalRelationship, ...]:
    values = {
        item.relationship_id: item
        for item in relationships
        if item.server_key == state.server_key
        and item.high_authority_eligible
        and item.relationship_kind is RelationshipKind.DIRECT
        and item.left_event_id in ring_ids
        and item.right_event_id in ring_ids
    }
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (item.left_at, item.right_at, item.relationship_id),
        )
    )


def _circular_components(
    values: list[tuple[float, object]], period: int, tolerance: float
) -> list[list[object]]:
    """Single-linkage components on the circle (``authority._phase_components``)."""

    if not values:
        return []
    ordered = sorted(values, key=lambda item: item[0])
    groups: list[list[tuple[float, object]]] = [[ordered[0]]]
    for item in ordered[1:]:
        if item[0] - groups[-1][-1][0] <= tolerance:
            groups[-1].append(item)
        else:
            groups.append([item])
    if len(groups) > 1 and ordered[0][0] + period - ordered[-1][0] <= tolerance:
        groups[0] = groups.pop() + groups[0]
    return [[value for _phase, value in group] for group in groups]


def compact_candidates(
    state: LearnedState,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_relationships: Iterable[IntervalRelationship] = (),
    ring_streaks: Iterable[CadenceStreak] = (),
    normal_score=None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
    challenger: bool = False,
) -> tuple[CompactCandidate, ...]:
    """High (continuous) candidates from folded interval bins plus the ring.

    Base authority and streak bonuses are exact (running products and sums).
    Components are single-linkage clusters of relationship phases, where a
    folded bin participates at its mean phase.  The phase comes from the
    longest streak (ring or folded best streak) exactly as authority does;
    for a component with folded evidence and no streak, the phase and spread
    use folded bin mean phases (approximation, H1-only components).
    ``challenger=True`` evaluates the epoch's post-establishment evidence
    (the Stage 6 view) instead of the lifetime summary.
    """

    ring_events = tuple(ring_events)
    mark = _watermark_key(state)
    ring_map = {
        item.event_id: item
        for item in ring_events
        if item.server_key == state.server_key and _newer(item, mark)
    }
    ring_ids = set(ring_map)
    epoch_start = state.epoch.established_at
    ring_rels = tuple(
        item
        for item in _ring_high_relationships(state, ring_relationships, ring_ids)
        if not challenger or (epoch_start is not None and item.left_at >= epoch_start)
    )
    bins = state.epoch.challenger if challenger else state.intervals
    results = []
    for period in CANDIDATE_PERIODS:
        tolerance = candidate_phase_tolerance(period)
        members: list[tuple[float, object]] = [
            (_relationship_phase_value(item, period), item)
            for item in ring_rels
            if item.candidate_period_seconds == period
        ]
        members.extend((item.phase, item) for item in bins if item.period == period)
        for component in _circular_components(members, period, tolerance):
            rels = [item for item in component if isinstance(item, IntervalRelationship)]
            folded = [item for item in component if isinstance(item, IntervalBin)]
            quality_map = {
                item.relationship_id: relationship_authority_quality(item, ring_events)
                for item in rels
            }
            factor = 1.0
            for value in quality_map.values():
                factor *= 1.0 - 0.50 * value
            for item in folded:
                factor *= item.base_residual
            base = 1.0 - factor
            rel_ids = set(quality_map)
            component_streaks = [
                item
                for item in ring_streaks
                if item.server_key == state.server_key
                and item.candidate_period_seconds == period
                and set(item.relationship_ids) <= rel_ids
                and (not challenger or (epoch_start is not None and item.start_at >= epoch_start))
            ]
            chosen = _non_overlapping_streaks(component_streaks)
            streak_bonus = 0.0
            gate_bonus = 0.0
            summaries: list[StreakSummary] = []
            for streak in chosen:
                minimum = min(quality_map[value] for value in streak.relationship_ids)
                shape = _streak_shape(streak.interval_count, minimum)
                streak_bonus += round(STREAK_BONUS_COEFFICIENT * shape, 6)
                gate_bonus += round(GATE_STREAK_BONUS_COEFFICIENT * shape, 6)
                summaries.append(
                    StreakSummary(
                        streak.interval_count,
                        streak.event_count,
                        minimum,
                        streak.phase_estimate,
                        streak.phase_spread,
                        streak.start_at,
                        streak.end_at,
                    )
                )
            for item in folded:
                streak_bonus += item.streak_bonus
                gate_bonus += item.gate_bonus
                if item.best_streak is not None:
                    summaries.append(item.best_streak)
            high = min(HIGH_AUTHORITY_CAP, base + streak_bonus)
            gate = min(HIGH_AUTHORITY_CAP, base + gate_bonus)
            phase_streak = max(
                summaries, key=lambda item: (item.interval_count, item.end_at), default=None
            )
            if phase_streak is not None:
                phase_offset = phase_streak.phase_estimate % period
                phase_spread = phase_streak.phase_spread
            elif rels and not folded:
                phase_offset, phase_spread = _relationship_phase(tuple(rels), period)
            else:
                phases = [_relationship_phase_value(item, period) for item in rels]
                phases.extend(item.phase for item in folded)
                anchor = phases[0]
                residuals = sorted(
                    _signed_phase_residual(value, anchor, period) for value in phases
                )
                center = _median_of_counts([(value, 1) for value in residuals])
                phase_offset = (anchor + center) % period
                phase_spread = max(
                    abs(_signed_phase_residual(value, phase_offset, period)) for value in phases
                )
            phase_quality = max(0.0, 1.0 - phase_spread / tolerance)
            count = len(rels) + sum(item.relationship_count for item in folded)
            normal_candidate = normal_score.candidate(period) if normal_score is not None else None
            h2_harmonic, h3_harmonic = compact_harmonic_blockers(
                state, period, normal_candidate, ring_windows
            )
            has_h2_streak = any(
                item.interval_count >= 2 and item.event_count >= 3 for item in summaries
            )
            h1 = count >= 1 and gate >= H1_AUTHORITY_MINIMUM
            h2 = bool(
                has_h2_streak
                and gate >= H2_AUTHORITY_MINIMUM
                and gate * phase_quality >= H2_PHASE_AUTHORITY_MINIMUM
                and not h2_harmonic
            )
            h3_structure = bool(
                any(item.interval_count >= 3 for item in summaries)
                or (has_h2_streak and count >= 3)
            )
            h3 = bool(
                h3_structure
                and gate >= H3_AUTHORITY_MINIMUM
                and gate * phase_quality >= H3_PHASE_AUTHORITY_MINIMUM
                and not h3_harmonic
            )
            h4 = bool(h3 and count >= 4 and gate >= H4_AUTHORITY_MINIMUM)
            endpoints = frozenset(
                value for item in rels for value in (item.left_event_id, item.right_event_id)
            )
            support = compact_sparse_support(
                state,
                period=period,
                phase_offset=phase_offset,
                ring_events=ring_events,
                excluded_ring_event_ids=endpoints,
                exclude_folded_high_endpoints=bool(folded),
            )
            sparse_reasons = compact_sparse_structure_reasons(
                state,
                period=period,
                support=support,
                normal_candidate=normal_candidate,
                ring_windows=ring_windows,
            )
            firsts = [item.left_at for item in rels] + [
                item.first_at for item in folded if item.first_at is not None
            ]
            lasts = [item.right_at for item in rels] + [
                item.last_at for item in folded if item.last_at is not None
            ]
            results.append(
                CompactCandidate(
                    period=period,
                    phase_offset=round(phase_offset, 6),
                    phase_spread=round(phase_spread, 6),
                    phase_quality=round(phase_quality, 6),
                    relationship_count=count,
                    base_authority=round(base, 6),
                    streak_bonus=round(streak_bonus, 6),
                    high_authority=round(high, 6),
                    gate_authority=round(gate, 6),
                    high_phase_authority=round(high * phase_quality, 6),
                    aligned_normal_confidence=support.value,
                    combined_confidence=compact_combined_confidence(high, support.value),
                    h1_gate=h1,
                    h2_gate=h2,
                    h3_gate=h3,
                    h4_gate=h4,
                    harmonic_blockers=tuple(sorted(set(h2_harmonic + h3_harmonic))),
                    first_evidence_at=min(firsts, default=None),
                    last_evidence_at=max(lasts, default=None),
                    ring_endpoint_ids=endpoints,
                    has_folded_evidence=bool(folded),
                    sparse_reason_codes=sparse_reasons,
                )
            )
    return tuple(sorted(results, key=lambda item: (item.period, item.phase_offset)))


def compact_sparse_confirmation(
    state: LearnedState,
    candidate: CompactCandidate,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_relationships: Iterable[IntervalRelationship] = (),
) -> float:
    """``authority._sparse_confirmed_candidate`` combined value (Stage 8.5).

    Confirming intervals are full intervals at the incumbent's period and
    phase: every high-eligible one (folded interval bins and ring) and the
    non-high ones ending after establishment (the epoch confirmation bins
    and ring).  Their endpoints leave the sparse slot.  Returns the
    candidate's combined confidence when nothing confirms.
    """

    incumbent = state.incumbent
    if incumbent is None or incumbent.established_at is None:
        return candidate.combined_confidence
    period = incumbent.period
    established_at = incumbent.established_at
    ring_events = tuple(ring_events)
    mark = _watermark_key(state)
    ring_map = {
        item.event_id: item
        for item in ring_events
        if item.server_key == state.server_key and _newer(item, mark)
    }
    residual = 1.0
    confirming = False
    excluded: set[str] = set(candidate.ring_endpoint_ids)
    folded_high = False
    for item in state.intervals:
        if (
            item.period == period
            and item.full_interval_count
            and _circular_distance(item.phase, incumbent.phase_offset, period)
            <= incumbent.phase_tolerance
        ):
            residual *= item.full_interval_residual
            confirming = True
            folded_high = True
    folded_confirmation = False
    for item in state.epoch.confirmation:
        if item.period == period and item.full_interval_count:
            residual *= item.full_interval_residual
            confirming = True
            folded_confirmation = True
    for relationship in ring_relationships:
        if (
            relationship.server_key != state.server_key
            or relationship.left_event_id not in ring_map
            or relationship.right_event_id not in ring_map
            or relationship.candidate_period_seconds != period
            or (
                not relationship.high_authority_eligible
                and relationship.right_at <= established_at
            )
            or _circular_distance(
                _relationship_phase_value(relationship, period), incumbent.phase_offset, period
            )
            > incumbent.phase_tolerance
        ):
            continue
        quality = _full_interval_quality(relationship, ring_map)
        if quality > 0:
            residual *= 1.0 - SPARSE_CONFIRMATION_INTERVAL_WEIGHT * quality
            confirming = True
            excluded.update((relationship.left_event_id, relationship.right_event_id))
    if not confirming:
        return candidate.combined_confidence
    confirmation = min(HIGH_AUTHORITY_CAP, 1.0 - residual)
    support = compact_sparse_support(
        state,
        period=period,
        phase_offset=candidate.phase_offset,
        ring_events=ring_events,
        excluded_ring_event_ids=excluded,
        exclude_folded_high_endpoints=candidate.has_folded_evidence or folded_high,
        exclude_folded_confirmation_endpoints=folded_confirmation,
    )
    continuous = max(candidate.high_authority, confirmation)
    combined = continuous + (1.0 - continuous) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * support.value
    )
    return round(max(candidate.combined_confidence, combined), 6)


def compact_last_aligned_evidence_at(
    state: LearnedState,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> float | None:
    """``authority._last_aligned_evidence_at`` for the recorded incumbent."""

    incumbent = state.incumbent
    if incumbent is None:
        return None
    period = incumbent.period
    values = [incumbent.established_at or 0.0]
    if incumbent.last_aligned_evidence_at is not None:
        values.append(incumbent.last_aligned_evidence_at)
    mark = _watermark_key(state)
    values.extend(
        _event_at(item)
        for item in ring_events
        if item.server_key == state.server_key
        and _newer(item, mark)
        and _authentic_event(item)
        and _circular_distance(_event_at(item), incumbent.phase_offset, period)
        <= incumbent.phase_tolerance
    )
    values.extend(
        item.last_hit_at
        for item in state.misses
        if item.period == period
        and item.last_hit_at is not None
        and _circular_distance(
            item.index * PHASE_BIN_SECONDS + PHASE_BIN_SECONDS / 2, incumbent.phase_offset, period
        )
        <= incumbent.phase_tolerance + PHASE_BIN_SECONDS / 2
    )
    window_mark = state.watermark.window_end_at
    values.extend(
        item.expected_at
        for item in latest_window_records(ring_windows)
        if item.outcome is ExpectedWindowOutcome.HIT
        and (window_mark is None or item.window_end_at > window_mark)
        and item.candidate_period_seconds == period
        and _circular_distance(item.expected_phase_offset, incumbent.phase_offset, period)
        <= incumbent.phase_tolerance
    )
    return max(values)


def compact_isolated_off_phase_restart(
    state: LearnedState,
    anomaly: PhysicalRestartEvent,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
) -> bool:
    """``authority._isolated_off_phase_restart`` for one unfolded anomaly.

    Phase coherence with folded off-phase restarts uses their bin mean time
    of day and largest capped uncertainty.  The caller supplies the single
    active off-phase restart, exactly as authority does.
    """

    incumbent = state.incumbent
    if incumbent is None or not _authentic_event(anomaly) or _event_at(anomaly) is None:
        return False

    def off_phase(seconds: float) -> bool:
        return _circular_distance(
            seconds,
            incumbent.phase_offset,
            incumbent.period,
        ) > incumbent.phase_tolerance

    if not off_phase(_event_at(anomaly)):
        return False
    anomaly_point = _ring_point(anomaly)
    others = [_bin_point(item) for item in state.pool if off_phase(item.seconds)]
    mark = _watermark_key(state)
    others.extend(
        _ring_point(item)
        for item in ring_events
        if item.server_key == state.server_key
        and item.event_id != anomaly.event_id
        and _newer(item, mark)
        and _authentic_event(item)
        and off_phase(_event_at(item))
    )

    def coherent(other: _Point) -> bool:
        return any(
            abs(_signed_phase_residual(anomaly_point.seconds, other.seconds, period))
            <= max(
                candidate_phase_tolerance(period) + anomaly_point.uncertainty,
                candidate_phase_tolerance(period) + other.uncertainty,
            )
            for period in CANDIDATE_PERIODS
        )

    return not any(coherent(item) for item in others)


def compact_recovery_confirmations(
    state: LearnedState,
    *,
    challenger_last_evidence_at: float,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_relationships: Iterable[IntervalRelationship] = (),
) -> int:
    """Stage 6 original-cycle recovery count (capped at the required two)."""

    incumbent = state.incumbent
    if incumbent is None:
        return 0
    mark = _watermark_key(state)
    ring_map = {
        item.event_id: item
        for item in ring_events
        if item.server_key == state.server_key and _newer(item, mark)
    }
    intervals = list(state.epoch.recovery)
    for relationship in ring_relationships:
        if (
            relationship.left_event_id in ring_map
            and relationship.right_event_id in ring_map
            and _incumbent_recovery_interval(relationship, incumbent, ring_map)
        ):
            intervals.append(RecoveryInterval(relationship.left_at, relationship.right_at))
    eligible = sorted(
        (item for item in set(intervals) if item.left_at > challenger_last_evidence_at),
        key=lambda item: (item.right_at, item.left_at),
    )
    selected = 0
    latest_end = -math.inf
    for item in eligible:
        if item.left_at < latest_end:
            continue
        selected += 1
        latest_end = item.right_at
    return min(selected, ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS)


# --------------------------------------------------------------------------
# Serialization with strict, bounded validation
# --------------------------------------------------------------------------


def _finite(
    value: object,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LearnedStateError(f"{name} must be a finite number")
    value = float(value)
    if value < minimum - _EPSILON or (maximum is not None and value > maximum + _EPSILON):
        raise LearnedStateError(f"{name} out of range")
    return value


def _optional_finite(value: object, name: str) -> float | None:
    return None if value is None else _finite(value, name)


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LearnedStateError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise LearnedStateError(f"{name} out of range")
    return value


def _string(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise LearnedStateError(f"{name} must be a non-empty string")
    return value


def _sequence(value: object, name: str, *, maximum: int) -> list:
    if not isinstance(value, list):
        raise LearnedStateError(f"{name} must be a list")
    if len(value) > maximum:
        raise LearnedStateError(f"{name} exceeds its storage bound")
    return value


def _mapping(value: object, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise LearnedStateError(f"{name} must be a mapping")
    return value


def _phase_bin_to(item: PhaseBin) -> dict:
    return {
        "index": item.index,
        "weight": item.weight,
        "count": item.count,
        "offset_sum": item.offset_sum,
        "first_at": item.first_at,
        "last_at": item.last_at,
        "uncertainty": item.uncertainty,
        "weight_before": item.weight_before,
    }


def _phase_bin_from(value: object, name: str) -> PhaseBin:
    data = _mapping(value, name)
    count = _integer(data.get("count"), f"{name}.count", minimum=1)
    first_at = _finite(data.get("first_at"), f"{name}.first_at")
    last_at = _finite(data.get("last_at"), f"{name}.last_at")
    if last_at < first_at:
        raise LearnedStateError(f"{name} first_at is after last_at")
    return PhaseBin(
        index=_integer(data.get("index"), f"{name}.index", maximum=DAY_BINS - 1),
        weight=_finite(data.get("weight"), f"{name}.weight"),
        count=count,
        offset_sum=_finite(
            data.get("offset_sum"),
            f"{name}.offset_sum",
            maximum=PHASE_BIN_SECONDS * count,
        ),
        first_at=first_at,
        last_at=last_at,
        uncertainty=_finite(
            data.get("uncertainty"),
            f"{name}.uncertainty",
            maximum=PHASE_UNCERTAINTY_CAP,
        ),
        weight_before=_finite(data.get("weight_before"), f"{name}.weight_before"),
    )


def _histogram_from(value: object, name: str) -> tuple[PhaseBin, ...]:
    bins = tuple(
        _phase_bin_from(item, f"{name}[{index}]")
        for index, item in enumerate(_sequence(value, name, maximum=DAY_BINS))
    )
    if [item.index for item in bins] != sorted({item.index for item in bins}):
        raise LearnedStateError(f"{name} bins must be unique and ordered")
    return bins


def _period_histograms_to(values) -> list:
    return [
        {"period": period, "bins": [_phase_bin_to(item) for item in bins]}
        for period, bins in values
    ]


def _period_histograms_from(value: object, name: str) -> tuple[
    tuple[int, tuple[PhaseBin, ...]],
    ...,
]:
    result = []
    seen = set()
    for index, item in enumerate(_sequence(value, name, maximum=len(CANDIDATE_PERIODS))):
        data = _mapping(item, f"{name}[{index}]")
        period = data.get("period")
        if period not in CANDIDATE_PERIODS or period in seen:
            raise LearnedStateError(f"{name}[{index}].period invalid")
        seen.add(period)
        result.append((period, _histogram_from(data.get("bins"), f"{name}[{index}].bins")))
    if [period for period, _bins in result] != [p for p in CANDIDATE_PERIODS if p in seen]:
        raise LearnedStateError(f"{name} periods must be ordered")
    return tuple(result)


def _streak_to(item: StreakSummary | None) -> dict | None:
    if item is None:
        return None
    return {
        "interval_count": item.interval_count,
        "event_count": item.event_count,
        "min_quality": item.min_quality,
        "phase_estimate": item.phase_estimate,
        "phase_spread": item.phase_spread,
        "start_at": item.start_at,
        "end_at": item.end_at,
    }


def _streak_from(value: object, name: str) -> StreakSummary | None:
    if value is None:
        return None
    data = _mapping(value, name)
    return StreakSummary(
        interval_count=_integer(data.get("interval_count"), f"{name}.interval_count", minimum=1),
        event_count=_integer(data.get("event_count"), f"{name}.event_count", minimum=2),
        min_quality=_finite(data.get("min_quality"), f"{name}.min_quality", maximum=1.0),
        phase_estimate=_finite(data.get("phase_estimate"), f"{name}.phase_estimate"),
        phase_spread=_finite(data.get("phase_spread"), f"{name}.phase_spread"),
        start_at=_finite(data.get("start_at"), f"{name}.start_at"),
        end_at=_finite(data.get("end_at"), f"{name}.end_at"),
    )


def _interval_bin_to(item: IntervalBin) -> dict:
    return {
        "period": item.period,
        "index": item.index,
        "relationship_count": item.relationship_count,
        "offset_sum": item.offset_sum,
        "base_residual": item.base_residual,
        "full_interval_count": item.full_interval_count,
        "full_interval_residual": item.full_interval_residual,
        "first_at": item.first_at,
        "last_at": item.last_at,
        "streak_count": item.streak_count,
        "streak_bonus": item.streak_bonus,
        "gate_bonus": item.gate_bonus,
        "best_streak": _streak_to(item.best_streak),
    }


def _interval_bins_from(value: object, name: str) -> tuple[IntervalBin, ...]:
    result = []
    for index, item in enumerate(_sequence(value, name, maximum=PERIOD_BIN_TOTAL)):
        label = f"{name}[{index}]"
        data = _mapping(item, label)
        period = data.get("period")
        if period not in CANDIDATE_PERIODS:
            raise LearnedStateError(f"{label}.period invalid")
        count = _integer(data.get("relationship_count"), f"{label}.relationship_count")
        result.append(
            IntervalBin(
                period=period,
                index=_integer(
                    data.get("index"),
                    f"{label}.index",
                    maximum=period // PHASE_BIN_SECONDS - 1,
                ),
                relationship_count=count,
                offset_sum=_finite(
                    data.get("offset_sum"),
                    f"{label}.offset_sum",
                    maximum=PHASE_BIN_SECONDS * max(1, count),
                ),
                base_residual=_finite(
                    data.get("base_residual"),
                    f"{label}.base_residual",
                    maximum=1.0,
                ),
                full_interval_count=_integer(
                    data.get("full_interval_count"),
                    f"{label}.full_interval_count",
                ),
                full_interval_residual=_finite(
                    data.get("full_interval_residual"),
                    f"{label}.full_interval_residual",
                    maximum=1.0,
                ),
                first_at=_optional_finite(data.get("first_at"), f"{label}.first_at"),
                last_at=_optional_finite(data.get("last_at"), f"{label}.last_at"),
                streak_count=_integer(data.get("streak_count"), f"{label}.streak_count"),
                streak_bonus=_finite(data.get("streak_bonus"), f"{label}.streak_bonus"),
                gate_bonus=_finite(data.get("gate_bonus"), f"{label}.gate_bonus"),
                best_streak=_streak_from(data.get("best_streak"), f"{label}.best_streak"),
            )
        )
    keys = [(item.period, item.index) for item in result]
    if keys != sorted(set(keys)):
        raise LearnedStateError(f"{name} entries must be unique and ordered")
    return tuple(result)


def learned_state_to_mapping(state: LearnedState) -> dict:
    mark = state.watermark
    epoch = state.epoch
    incumbent = state.incumbent
    return {
        "schema_version": state.schema_version,
        "server_key": state.server_key,
        "pool_weight_total": state.pool_weight_total,
        "watermark": {
            "at": mark.at,
            "event_id": mark.event_id,
            "folded_event_count": mark.folded_event_count,
            "folded_learning_event_count": mark.folded_learning_event_count,
            "window_end_at": mark.window_end_at,
        },
        "pool": [_phase_bin_to(item) for item in state.pool],
        "high_endpoints": _period_histograms_to(state.high_endpoints),
        "intervals": [_interval_bin_to(item) for item in state.intervals],
        "misses": [
            {
                "period": item.period,
                "index": item.index,
                "genuine_miss_count": item.genuine_miss_count,
                "last_genuine_miss_at": item.last_genuine_miss_at,
                "last_hit_at": item.last_hit_at,
            }
            for item in state.misses
        ],
        "divisors": [
            {
                "period": item.period,
                "divisor": item.divisor,
                "covered_window_count": item.covered_window_count,
                "longer_interval_count": item.longer_interval_count,
            }
            for item in state.divisors
        ],
        "incumbent": None
        if incumbent is None
        else {
            "regime_id": incumbent.regime_id,
            "period": incumbent.period,
            "phase_offset": incumbent.phase_offset,
            "phase_tolerance": incumbent.phase_tolerance,
            "origin": incumbent.origin,
            "authority_gate": incumbent.authority_gate,
            "status": incumbent.status,
            "high_authority": incumbent.high_authority,
            "phase_authority": incumbent.phase_authority,
            "combined_confidence": incumbent.combined_confidence,
            "established_at": incumbent.established_at,
            "last_aligned_evidence_at": incumbent.last_aligned_evidence_at,
            "defining_evidence_ids": list(incumbent.defining_evidence_ids),
            "reason_codes": list(incumbent.reason_codes),
        },
        "superseded": [
            {
                "period": item.period,
                "phase_offset": item.phase_offset,
                "phase_tolerance": item.phase_tolerance,
                "superseded_at": item.superseded_at,
            }
            for item in state.superseded
        ],
        "epoch": {
            "regime_id": epoch.regime_id,
            "established_at": epoch.established_at,
            "challenger": [_interval_bin_to(item) for item in epoch.challenger],
            "confirmation": [_interval_bin_to(item) for item in epoch.confirmation],
            "confirmation_endpoints": _period_histograms_to(epoch.confirmation_endpoints),
            "recovery": [
                {"left_at": item.left_at, "right_at": item.right_at} for item in epoch.recovery
            ],
        },
    }


def learned_state_from_mapping(value: object) -> LearnedState:
    data = _mapping(value, "learned_state")
    if data.get("schema_version") != LEARNED_STATE_SCHEMA_VERSION:
        raise LearnedStateError("unsupported learned-state schema version")
    server_key = _string(data.get("server_key"), "server_key")
    mark = _mapping(data.get("watermark"), "watermark")
    watermark = FoldWatermark(
        at=_optional_finite(mark.get("at"), "watermark.at"),
        event_id=_string(mark.get("event_id"), "watermark.event_id", optional=True),
        folded_event_count=_integer(mark.get("folded_event_count"), "watermark.folded_event_count"),
        folded_learning_event_count=_integer(
            mark.get("folded_learning_event_count"), "watermark.folded_learning_event_count"
        ),
        window_end_at=_optional_finite(mark.get("window_end_at"), "watermark.window_end_at"),
    )
    if (watermark.at is None) != (watermark.event_id is None):
        raise LearnedStateError("watermark time and event identity must be set together")

    misses = []
    for index, item in enumerate(_sequence(data.get("misses"), "misses", maximum=PERIOD_BIN_TOTAL)):
        label = f"misses[{index}]"
        entry = _mapping(item, label)
        period = entry.get("period")
        if period not in CANDIDATE_PERIODS:
            raise LearnedStateError(f"{label}.period invalid")
        misses.append(
            MissBin(
                period=period,
                index=_integer(
                    entry.get("index"),
                    f"{label}.index",
                    maximum=period // PHASE_BIN_SECONDS - 1,
                ),
                genuine_miss_count=_integer(
                    entry.get("genuine_miss_count"),
                    f"{label}.genuine_miss_count",
                ),
                last_genuine_miss_at=_optional_finite(
                    entry.get("last_genuine_miss_at"),
                    f"{label}.last_genuine_miss_at",
                ),
                last_hit_at=_optional_finite(entry.get("last_hit_at"), f"{label}.last_hit_at"),
            )
        )
    if [(item.period, item.index) for item in misses] != sorted(
        {(item.period, item.index) for item in misses}
    ):
        raise LearnedStateError("misses entries must be unique and ordered")

    divisors = []
    for index, item in enumerate(
        _sequence(data.get("divisors"), "divisors", maximum=len(DIVISOR_KEYS)),
    ):
        label = f"divisors[{index}]"
        entry = _mapping(item, label)
        key = (entry.get("period"), entry.get("divisor"))
        if key not in DIVISOR_KEYS:
            raise LearnedStateError(f"{label} is not a (period, divisor) pair")
        divisors.append(
            DivisorEvidence(
                key[0],
                key[1],
                _integer(entry.get("covered_window_count"), f"{label}.covered_window_count"),
                _integer(entry.get("longer_interval_count"), f"{label}.longer_interval_count"),
            )
        )
    if [(item.period, item.divisor) for item in divisors] != sorted(
        {(item.period, item.divisor) for item in divisors}
    ):
        raise LearnedStateError("divisors entries must be unique and ordered")

    incumbent = None
    if data.get("incumbent") is not None:
        entry = _mapping(data.get("incumbent"), "incumbent")
        period = entry.get("period")
        if period not in CANDIDATE_PERIODS:
            raise LearnedStateError("incumbent.period invalid")
        ids = _sequence(
            entry.get("defining_evidence_ids"),
            "incumbent.defining_evidence_ids",
            maximum=RECENT_RING_CAPACITY,
        )
        reasons = _sequence(
            entry.get("reason_codes"),
            "incumbent.reason_codes",
            maximum=MAX_REASON_CODES,
        )
        incumbent = RegimeKnowledge(
            regime_id=_string(entry.get("regime_id"), "incumbent.regime_id"),
            period=period,
            phase_offset=_finite(
                entry.get("phase_offset"),
                "incumbent.phase_offset",
                maximum=period,
            ),
            phase_tolerance=_finite(entry.get("phase_tolerance"), "incumbent.phase_tolerance"),
            origin=_string(entry.get("origin"), "incumbent.origin"),
            authority_gate=_string(entry.get("authority_gate"), "incumbent.authority_gate"),
            status=_string(entry.get("status"), "incumbent.status"),
            high_authority=_finite(
                entry.get("high_authority"),
                "incumbent.high_authority",
                maximum=1.0,
            ),
            phase_authority=_finite(
                entry.get("phase_authority"),
                "incumbent.phase_authority",
                maximum=1.0,
            ),
            combined_confidence=_finite(
                entry.get("combined_confidence"),
                "incumbent.combined_confidence",
                maximum=1.0,
            ),
            established_at=_optional_finite(
                entry.get("established_at"),
                "incumbent.established_at",
            ),
            last_aligned_evidence_at=_optional_finite(
                entry.get("last_aligned_evidence_at"),
                "incumbent.last_aligned_evidence_at",
            ),
            defining_evidence_ids=tuple(
                _string(item, "incumbent.defining_evidence_ids[]") for item in ids
            ),
            reason_codes=tuple(_string(item, "incumbent.reason_codes[]") for item in reasons),
        )

    superseded = []
    for index, item in enumerate(
        _sequence(data.get("superseded"), "superseded", maximum=PERIOD_BIN_TOTAL),
    ):
        label = f"superseded[{index}]"
        entry = _mapping(item, label)
        period = entry.get("period")
        if period not in CANDIDATE_PERIODS:
            raise LearnedStateError(f"{label}.period invalid")
        superseded.append(
            SupersededRegime(
                period,
                _finite(entry.get("phase_offset"), f"{label}.phase_offset", maximum=period),
                _finite(entry.get("phase_tolerance"), f"{label}.phase_tolerance"),
                _finite(entry.get("superseded_at"), f"{label}.superseded_at"),
            )
        )
    if [item.key for item in superseded] != sorted({item.key for item in superseded}):
        raise LearnedStateError("superseded entries must be unique and ordered")

    epoch_data = _mapping(data.get("epoch"), "epoch")
    recovery = []
    for index, item in enumerate(
        _sequence(
            epoch_data.get("recovery"),
            "epoch.recovery",
            maximum=ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS,
        )
    ):
        entry = _mapping(item, f"epoch.recovery[{index}]")
        left = _finite(entry.get("left_at"), f"epoch.recovery[{index}].left_at")
        right = _finite(entry.get("right_at"), f"epoch.recovery[{index}].right_at")
        if right <= left:
            raise LearnedStateError("recovery interval must have positive length")
        recovery.append(RecoveryInterval(left, right))
    epoch = EpochEvidence(
        regime_id=_string(epoch_data.get("regime_id"), "epoch.regime_id", optional=True),
        established_at=_optional_finite(epoch_data.get("established_at"), "epoch.established_at"),
        challenger=_interval_bins_from(epoch_data.get("challenger"), "epoch.challenger"),
        confirmation=_interval_bins_from(epoch_data.get("confirmation"), "epoch.confirmation"),
        confirmation_endpoints=_period_histograms_from(
            epoch_data.get("confirmation_endpoints"), "epoch.confirmation_endpoints"
        ),
        recovery=tuple(recovery),
    )
    state = LearnedState(
        schema_version=LEARNED_STATE_SCHEMA_VERSION,
        server_key=server_key,
        watermark=watermark,
        pool_weight_total=_finite(data.get("pool_weight_total"), "pool_weight_total"),
        pool=_histogram_from(data.get("pool"), "pool"),
        high_endpoints=_period_histograms_from(data.get("high_endpoints"), "high_endpoints"),
        intervals=_interval_bins_from(data.get("intervals"), "intervals"),
        misses=tuple(misses),
        divisors=tuple(divisors),
        incumbent=incumbent,
        superseded=tuple(superseded),
        epoch=epoch,
    )
    if any(
        item.weight_before + item.weight > state.pool_weight_total + 1e-6 for item in state.pool
    ):
        raise LearnedStateError("pool bin domain accumulator exceeds the pool total")
    if learned_state_entry_count(state) > LEARNED_STATE_MAX_ENTRIES:
        raise LearnedStateError("learned state exceeds its storage bound")
    return state


def learned_state_json_bytes(state: LearnedState) -> bytes:
    """Compact canonical JSON (for size measurement and round-trip tests)."""

    return json.dumps(
        learned_state_to_mapping(state),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
