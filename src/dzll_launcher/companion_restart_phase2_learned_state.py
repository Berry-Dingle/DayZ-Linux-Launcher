"""Compact persistent learned state for Server Companion (Retention R1/R1a).

Schema 4 currently re-derives every candidate, gate, phase and confidence on
each rebuild from the full lifetime raw collections.  This module is the
bounded replacement foundation: a fixed-size per-server summary into which
genuine observations are *folded* once, plus pure evaluation functions that
reproduce the existing Stage 8 statistics from that summary and a strictly
bounded ring of not-yet-folded raw items.

Scope: representation, folding and evaluation only.  Nothing here is
persisted by the production runtime and production authority does not read
it yet.  No new confidence policy is introduced: every evaluation reuses the
existing schema-3 / schema-4 formulas, thresholds and helpers.

Bounded folding.  ``plan_ring_folding`` keeps at most ``capacity`` raw events
in the ring however long a continuity chain stays open.  An open chain is
folded up to a boundary restart (the *anchor*); the anchor stays in the ring
as already-credited context so the next genuine interval can still be
extracted, and the streak running through it is carried as a bounded
``OpenRun`` (counts, minimum quality, exact first phase and a quantised
residual histogram bounded by the streak-coherence rule).  No restart is
invented and no unmonitored interval becomes continuous: only relationships
that continuity extraction produced are ever folded.

Conservative representation.  Folded restarts sharing one UTC minute keep
their exact offset range; a bin counts as phase-aligned only when its whole
range is within tolerance, and an ambiguous bin is treated as off-phase.
Folded relationship bins keep exact phase extremes, so single-linkage
component formation is exact.  Every remaining approximation can only lower
confidence or withhold a gate; none can promote.

Storage.  Every collection has a fixed key space that does not depend on the
number of observed restarts (see ``LEARNED_STATE_MAX_ENTRIES`` and
``LEARNED_STATE_MAX_JSON_BYTES``, both enforced on serialisation and load).
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
    _relationship_phase,
    _relationship_phase_value,
    relationship_authority_quality,
)
from .companion_restart_phase2_continuity import IntervalRelationship, RelationshipKind
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


LEARNED_STATE_SCHEMA_VERSION = 3
PHASE_BIN_SECONDS = 60
DAY_BINS = DAY // PHASE_BIN_SECONDS
# Relationship, miss and superseded-regime bins.  Never wider than the
# smallest candidate tolerance, so restarts sharing a bin are always linked.
INTERVAL_BIN_SECONDS = 300
MINIMUM_TOLERANCE = min(candidate_phase_tolerance(period) for period in CANDIDATE_PERIODS)
MAXIMUM_TOLERANCE = max(candidate_phase_tolerance(period) for period in CANDIDATE_PERIODS)
assert INTERVAL_BIN_SECONDS <= MINIMUM_TOLERANCE
INTERVAL_BIN_TOTAL = sum(period // INTERVAL_BIN_SECONDS for period in CANDIDATE_PERIODS)
# Open-streak residual histogram.  A coherent streak keeps every phase within
# one tolerance of its estimate, so residuals from its first phase lie within
# two tolerances: the key space is bounded by the coherence rule itself.
RUN_RESIDUAL_QUANTUM_SECONDS = 5
RUN_RESIDUAL_KEY_RADIUS = math.ceil(2 * MAXIMUM_TOLERANCE / RUN_RESIDUAL_QUANTUM_SECONDS)
RUN_RESIDUAL_KEY_LIMIT = 2 * RUN_RESIDUAL_KEY_RADIUS + 1
# The ring of not-yet-folded raw events reuses the existing schema-3 event
# horizon; no new retention number is introduced.
RECENT_RING_CAPACITY = MAX_FINALIZED_EVENTS
MINIMUM_RING_CAPACITY = 2
HARMONIC_PERIODS = frozenset({6 * 3600, 8 * 3600, 12 * 3600})
MAX_DEFINING_EVIDENCE_IDS = 3
MAX_REASON_CODES = 64
MAX_STRING_LENGTH = 256
_EPSILON = 1e-9

DIVISOR_KEYS = tuple(
    (period, divisor)
    for period in CANDIDATE_PERIODS
    for divisor in CANDIDATE_PERIODS
    if divisor < period and period % divisor == 0
)
LEARNED_STATE_MAX_ENTRIES = (
    DAY_BINS  # pool (endpoint and confirmation tags live inside pool bins)
    + INTERVAL_BIN_TOTAL  # lifetime continuous evidence
    + INTERVAL_BIN_TOTAL  # epoch challenger evidence
    + INTERVAL_BIN_TOTAL  # expected-window outcomes
    + INTERVAL_BIN_TOTAL  # superseded regimes
    + INTERVAL_BIN_TOTAL  # non-high (normal) relationships
    + len(DIVISOR_KEYS)
    + ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS
    + RUN_RESIDUAL_KEY_LIMIT  # the single open streak
    + 2  # incumbent, open chain
)

# Serialised-size bound, derived from the structural bounds.  Every number is
# charged the longest JSON form a finite float can take (24 characters) plus
# one separator, every string the maximum validated length plus quotes.
_NUMBER = 25
_STRING = MAX_STRING_LENGTH * 6 + 3  # worst-case JSON escaping of non-ASCII
_POOL_BIN_NUMBERS = 10 + 8 * len(CANDIDATE_PERIODS) + 3 + 2 * len(CANDIDATE_PERIODS)
_INTERVAL_BIN_NUMBERS = 17 + 7
LEARNED_STATE_MAX_JSON_BYTES = (
    4096  # keys, brackets and scalar fields
    + DAY_BINS * (_POOL_BIN_NUMBERS * _NUMBER + 16 + 4 * len(CANDIDATE_PERIODS))
    + 2 * INTERVAL_BIN_TOTAL * (_INTERVAL_BIN_NUMBERS * _NUMBER + 8)
    + INTERVAL_BIN_TOTAL * (7 * _NUMBER + 4)
    + INTERVAL_BIN_TOTAL * (4 * _NUMBER + 4)
    + INTERVAL_BIN_TOTAL * (7 * _NUMBER + 4)
    + len(DIVISOR_KEYS) * (6 * _NUMBER + 4)
    + ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS * (2 * _NUMBER + 4)
    + RUN_RESIDUAL_KEY_LIMIT * (4 * _NUMBER + 4)
    + 40 * _NUMBER  # open chain and incumbent numbers
    + (5 + MAX_DEFINING_EVIDENCE_IDS + MAX_REASON_CODES) * (_STRING + 1)
)
PERIOD_INDEX = {period: index for index, period in enumerate(CANDIDATE_PERIODS)}


class LearnedStateError(ValueError):
    """A learned-state mapping is malformed or exceeds its storage bound."""


# --------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointTag:
    """Folded restarts in a bin that are endpoints at one period.

    For high-relationship tags (not confirmation tags) the tag also records
    the time span of the tagged restarts and the earliest *untagged* restart
    of the bin with the running pool and tagged totals just before it was
    folded.  Excluding a candidate's own endpoints therefore moves the
    consistency-domain start to the exact earliest remaining restart.
    """

    period: int
    weight: float
    count: int
    first_tagged_at: float | None = None
    last_tagged_at: float | None = None
    untagged_first_at: float | None = None
    untagged_weight_before: float | None = None
    untagged_tagged_before: float | None = None


@dataclass(frozen=True)
class PhaseBin:
    """Folded sparse-usable restarts sharing one UTC minute of the day.

    ``offset_min``/``offset_max`` are the exact extreme seconds within the
    minute; ``uncertainty_min``/``max`` the extreme capped uncertainties.
    ``weight_before`` is the running pool weight total just before the bin's
    first restart: folding is time-ordered, so the pool weight at or after
    ``first_at`` is exactly ``pool_weight_total - weight_before``.
    ``endpoints`` tag restarts that are high-relationship endpoints per
    period; ``confirmation`` tags endpoints of epoch confirmation intervals.
    """

    index: int
    weight: float
    count: int
    offset_min: float
    offset_max: float
    first_at: float
    last_at: float
    uncertainty_min: float
    uncertainty_max: float
    weight_before: float
    endpoints: tuple[EndpointTag, ...] = ()
    confirmation: EndpointTag | None = None
    # Running tagged (endpoint) weight per period just before the bin's first
    # restart was folded (only periods with a non-zero total).
    tagged_before: tuple[tuple[int, float], ...] = ()

    def tagged_before_at(self, period: int) -> float:
        return next((value for key, value in self.tagged_before if key == period), 0.0)

    @property
    def seconds_min(self) -> float:
        return self.index * PHASE_BIN_SECONDS + self.offset_min

    @property
    def seconds_max(self) -> float:
        return self.index * PHASE_BIN_SECONDS + self.offset_max

    @property
    def seconds_mid(self) -> float:
        return (self.seconds_min + self.seconds_max) / 2

    def tag(self, period: int) -> EndpointTag | None:
        return next((item for item in self.endpoints if item.period == period), None)


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
    """Folded continuous evidence at one (period, 5-minute phase bin).

    ``base_residual`` is the exact running product ``prod(1 - 0.5 q)`` of
    high-eligible DIRECT relationships whose phase falls in the bin, with the
    exact phase extremes (``phase_min``/``phase_max``, offsets from the bin
    start).  ``full_interval_residual`` is ``prod(1 - 0.75 q)`` over those
    that are full intervals.  Closed maximal streaks whose phase estimate
    falls in the bin contribute their exact bonus terms; ``streak_min`` /
    ``streak_max`` bound the phases of their relationships (offsets from the
    bin start), so a streak attaches only to a component containing them all.
    """

    period: int
    index: int
    relationship_count: int = 0
    phase_min: float | None = None
    phase_max: float | None = None
    base_residual: float = 1.0
    full_interval_count: int = 0
    full_interval_residual: float = 1.0
    first_at: float | None = None
    last_at: float | None = None
    streak_count: int = 0
    streak_bonus: float = 0.0
    gate_bonus: float = 0.0
    streak_min: float | None = None
    streak_max: float | None = None
    best_streak: StreakSummary | None = None

    @property
    def start(self) -> float:
        return float(self.index * INTERVAL_BIN_SECONDS)


@dataclass(frozen=True)
class MissBin:
    """Folded expected-window outcomes at one (period, 5-minute phase bin)."""

    period: int
    index: int
    phase_min: float
    phase_max: float
    genuine_miss_count: int = 0
    last_genuine_miss_at: float | None = None
    last_hit_at: float | None = None


@dataclass(frozen=True)
class NormalBin:
    """Folded non-high relationships at one (period, 5-minute phase bin).

    Folded once, when the relationship's left endpoint is folded (its right
    endpoints are already in the ring then).  Used for the latent
    normal-challenger context, which counts aligned normal relationships.
    """

    period: int
    index: int
    count: int
    phase_min: float
    phase_max: float
    first_at: float
    last_at: float


@dataclass(frozen=True)
class DivisorEvidence:
    """Phase-anchored divisor resolution learned from a clean schema-3 window.

    ``phase_offset`` is the period candidate's phase when it was learned and
    ``observed_at`` the latest restart of that window.  Any later authentic
    restart at one of the period's divisor midpoints (and not at its own
    phase) contradicts and removes it.
    """

    period: int
    divisor: int
    covered_window_count: int
    longer_interval_count: int
    phase_offset: float = 0.0
    observed_at: float = 0.0


@dataclass(frozen=True)
class SupersededRegime:
    period: int
    phase_offset: float
    phase_tolerance: float
    superseded_at: float

    @property
    def key(self) -> tuple[int, int]:
        return (self.period, int((self.phase_offset % self.period) // INTERVAL_BIN_SECONDS))


@dataclass(frozen=True)
class RegimeKnowledge:
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
class RunResidual:
    """Quantised residuals of an open streak: exact extremes per key."""

    key: int
    count: int
    deviation_min: float
    deviation_max: float


@dataclass(frozen=True)
class OpenRun:
    """The streak running through the open chain's anchor (not yet closed)."""

    period: int
    interval_count: int
    min_quality: float
    start_at: float
    end_at: float
    first_phase: float
    residuals: tuple[RunResidual, ...]
    relationship_phase_min: float
    relationship_phase_max: float


@dataclass(frozen=True)
class OpenChain:
    """A continuity chain folded up to (and including) its anchor restart."""

    chain_id: str
    anchor_event_id: str
    anchor_at: float
    anchor_index: int | None
    anchor_weight: float
    anchor_high_periods: tuple[int, ...]
    anchor_confirmation_tagged: bool
    run: OpenRun | None


@dataclass(frozen=True)
class EpochEvidence:
    """Evidence scoped to the current incumbent establishment boundary."""

    regime_id: str | None = None
    established_at: float | None = None
    challenger: tuple[IntervalBin, ...] = ()
    confirmation_count: int = 0
    confirmation_residual: float = 1.0
    recovery: tuple[RecoveryInterval, ...] = ()


@dataclass(frozen=True)
class FoldWatermark:
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
    intervals: tuple[IntervalBin, ...] = ()
    misses: tuple[MissBin, ...] = ()
    divisors: tuple[DivisorEvidence, ...] = ()
    incumbent: RegimeKnowledge | None = None
    superseded: tuple[SupersededRegime, ...] = ()
    epoch: EpochEvidence = field(default_factory=EpochEvidence)
    open_chain: OpenChain | None = None
    normal_relationships: tuple[NormalBin, ...] = ()
    # Running tagged (high-endpoint) weight per period, in fold order.
    tagged_weight_totals: tuple[tuple[int, float], ...] = ()
    # Divisor resolution is re-learned only from schema-3 windows that start
    # after this instant (latest contradiction or supersession).
    divisor_floor_at: float | None = None


def empty_learned_state(server_key: str) -> LearnedState:
    if not isinstance(server_key, str) or not server_key or len(server_key) > MAX_STRING_LENGTH:
        raise LearnedStateError("server_key must be a non-empty bounded string")
    return LearnedState(LEARNED_STATE_SCHEMA_VERSION, server_key)


def learned_state_entry_count(state: LearnedState) -> int:
    run = state.open_chain.run if state.open_chain is not None else None
    return (
        len(state.pool)
        + len(state.intervals)
        + len(state.epoch.challenger)
        + len(state.misses)
        + len(state.superseded)
        + len(state.divisors)
        + len(state.normal_relationships)
        + len(state.epoch.recovery)
        + (len(run.residuals) if run is not None else 0)
        + (state.incumbent is not None)
        + (state.open_chain is not None)
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


def _interval_bin(value: float, period: int) -> tuple[int, float]:
    value = float(value) % period
    index = min(period // INTERVAL_BIN_SECONDS - 1, int(value // INTERVAL_BIN_SECONDS))
    return index, value - index * INTERVAL_BIN_SECONDS


def _range_abs(low: float, high: float, reference: float, period: int) -> tuple[float, float]:
    """Smallest and largest absolute phase residual over ``[low, high]``."""

    first = _signed_phase_residual(low, reference, period)
    last = first + (high - low)
    if last > period / 2:
        return min(abs(first), abs(last - period)), period / 2
    smallest = 0.0 if first <= 0.0 <= last else min(abs(first), abs(last))
    return smallest, max(abs(first), abs(last))


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
        _authentic_event(event) and _event_weight(event) > 0 and _event_at(event) is not None,
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


def _greedy_recovery(intervals: Iterable[RecoveryInterval]) -> tuple[RecoveryInterval, ...]:
    """The latest non-overlapping intervals, chosen backwards (at most two)."""

    ordered = sorted(set(intervals), key=lambda item: (item.left_at, item.right_at), reverse=True)
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


def _high_direct(relationship: IntervalRelationship) -> bool:
    return bool(
        relationship.high_authority_eligible
        and relationship.relationship_kind is RelationshipKind.DIRECT
    )


def divisor_contradicted(entry: DivisorEvidence, at: float) -> bool:
    """A restart at a divisor midpoint of the period (not at its own phase)."""

    period, divisor = entry.period, entry.divisor
    if _circular_distance(at, entry.phase_offset, period) <= candidate_phase_tolerance(period):
        return False
    tolerance = max(candidate_phase_tolerance(period), candidate_phase_tolerance(divisor))
    return any(
        _circular_distance(at, entry.phase_offset + step * divisor, period) <= tolerance
        for step in range(1, period // divisor)
    )


def _invalidate_divisors(
    divisors: tuple[DivisorEvidence, ...], events: Iterable[PhysicalRestartEvent]
) -> tuple[tuple[DivisorEvidence, ...], float | None]:
    """Drop divisor evidence contradicted by later authentic restarts."""

    kept = []
    latest = None
    candidates = [
        _event_at(item) for item in events if _authentic_event(item) and _event_at(item) is not None
    ]
    for entry in divisors:
        contradictions = [
            at for at in candidates if at > entry.observed_at and divisor_contradicted(entry, at)
        ]
        if contradictions:
            first = min(contradictions)
            latest = first if latest is None else max(latest, first)
            continue
        kept.append(entry)
    return tuple(kept), latest


# --------------------------------------------------------------------------
# Streak runs (the build_cadence_streaks join rule, with a bounded seed)
# --------------------------------------------------------------------------


@dataclass
class _Run:
    period: int
    chain_id: str
    last_event_id: str
    start_at: float
    end_at: float
    first_phase: float
    interval_count: int
    min_quality: float
    folded: tuple[RunResidual, ...]
    exact: list[float]
    relationship_min: float
    relationship_max: float

    def residual(self, at: float) -> float:
        return _signed_phase_residual(at, self.first_phase, self.period)

    def phase(self, extra: Sequence[float] = ()) -> tuple[float, float]:
        """(phase estimate, spread upper bound).

        Exact residuals reproduce ``continuity._phase_estimate``; quantised
        folded residuals contribute their exact per-key extremes, so the
        returned spread is never smaller than the true spread.
        """

        quantum = RUN_RESIDUAL_QUANTUM_SECONDS
        items = [
            (
                item.key * quantum + item.deviation_min,
                item.key * quantum + item.deviation_max,
                item.count,
            )
            for item in self.folded
        ]
        items.extend((value, value, 1) for value in (*self.exact, *extra))
        low_median = _median_of_counts([(low, count) for low, _high, count in items])
        high_median = _median_of_counts([(high, count) for _low, high, count in items])
        center = _median_of_counts([((low + high) / 2, count) for low, high, count in items])
        highest = max(high for _low, high, _count in items)
        lowest = min(low for low, _high, _count in items)
        spread = max(highest - low_median, high_median - lowest)
        return (self.first_phase + center) % self.period, spread

    def joins(self, relationship: IntervalRelationship) -> bool:
        if (
            relationship.candidate_period_seconds != self.period
            or relationship.continuity_chain_id != self.chain_id
            or relationship.left_event_id != self.last_event_id
            or relationship.left_at != self.end_at
            or relationship.intervening_event_ids
        ):
            return False
        _phase, spread = self.phase((self.residual(relationship.right_at),))
        return spread <= candidate_phase_tolerance(self.period)

    def extend(self, relationship: IntervalRelationship, quality: float) -> None:
        self.exact.append(self.residual(relationship.right_at))
        self.last_event_id = relationship.right_event_id
        self.end_at = relationship.right_at
        self.interval_count += 1
        self.min_quality = min(self.min_quality, quality)
        value = self.residual(_relationship_phase_value(relationship, self.period))
        self.relationship_min = min(self.relationship_min, value)
        self.relationship_max = max(self.relationship_max, value)

    def summary(self) -> StreakSummary:
        estimate, spread = self.phase()
        return StreakSummary(
            self.interval_count,
            self.interval_count + 1,
            self.min_quality,
            estimate,
            spread,
            self.start_at,
            self.end_at,
        )

    def relationship_range(self) -> tuple[float, float]:
        return (self.first_phase + self.relationship_min, self.first_phase + self.relationship_max)

    def to_open(self) -> OpenRun:
        quantum = RUN_RESIDUAL_QUANTUM_SECONDS
        keys = {item.key: item for item in self.folded}
        for value in self.exact:
            key = int(round(value / quantum))
            deviation = value - key * quantum
            existing = keys.get(key)
            keys[key] = (
                RunResidual(key, 1, deviation, deviation)
                if existing is None
                else RunResidual(
                    key,
                    existing.count + 1,
                    min(existing.deviation_min, deviation),
                    max(existing.deviation_max, deviation),
                )
            )
        return OpenRun(
            period=self.period,
            interval_count=self.interval_count,
            min_quality=self.min_quality,
            start_at=self.start_at,
            end_at=self.end_at,
            first_phase=self.first_phase,
            residuals=tuple(keys[key] for key in sorted(keys)),
            relationship_phase_min=self.relationship_min,
            relationship_phase_max=self.relationship_max,
        )


def _run_from_relationship(relationship: IntervalRelationship, quality: float) -> _Run:
    period = relationship.candidate_period_seconds
    first_phase = relationship.left_at % period
    run = _Run(
        period=period,
        chain_id=relationship.continuity_chain_id,
        last_event_id=relationship.right_event_id,
        start_at=relationship.left_at,
        end_at=relationship.right_at,
        first_phase=first_phase,
        interval_count=1,
        min_quality=quality,
        folded=(),
        exact=[0.0],
        relationship_min=0.0,
        relationship_max=0.0,
    )
    run.exact.append(run.residual(relationship.right_at))
    value = run.residual(_relationship_phase_value(relationship, period))
    run.relationship_min = run.relationship_max = value
    return run


def _run_from_open(open_chain: OpenChain) -> _Run | None:
    value = open_chain.run
    if value is None:
        return None
    return _Run(
        period=value.period,
        chain_id=open_chain.chain_id,
        last_event_id=open_chain.anchor_event_id,
        start_at=value.start_at,
        end_at=value.end_at,
        first_phase=value.first_phase,
        interval_count=value.interval_count,
        min_quality=value.min_quality,
        folded=value.residuals,
        exact=[],
        relationship_min=value.relationship_phase_min,
        relationship_max=value.relationship_phase_max,
    )


def _build_runs(
    items: Iterable[tuple[IntervalRelationship, float]], seed: _Run | None
) -> list[_Run]:
    """``continuity.build_cadence_streaks`` runs, optionally seeded.

    Same eligibility, ordering and join rule; single-interval runs are
    returned too (the caller decides what is a streak or still open).
    """

    eligible = sorted(
        (
            (relationship, quality)
            for relationship, quality in items
            if _high_direct(relationship)
            and relationship.continuity_chain_id
            and not relationship.intervening_event_ids
        ),
        key=lambda item: (
            item[0].candidate_period_seconds,
            item[0].continuity_chain_id,
            item[0].left_at,
            item[0].right_at,
            item[0].relationship_id,
        ),
    )
    runs: list[_Run] = []
    active: _Run | None = None
    pending = seed
    for relationship, quality in eligible:
        group = (relationship.candidate_period_seconds, relationship.continuity_chain_id)
        if pending is not None and group == (pending.period, pending.chain_id):
            if active is not None:
                runs.append(active)
            active, pending = pending, None
        if active is not None and active.joins(relationship):
            active.extend(relationship, quality)
            continue
        if active is not None:
            runs.append(active)
        active = _run_from_relationship(relationship, quality)
    if active is not None:
        runs.append(active)
    if pending is not None:
        runs.append(pending)
    return runs


# --------------------------------------------------------------------------
# Folding genuine observations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldUnit:
    """Events to fold (may include already-folded context such as the anchor)."""

    events: tuple[PhysicalRestartEvent, ...]
    chain_open: bool


@dataclass(frozen=True)
class FoldPlan:
    units: tuple[FoldUnit, ...]
    ring: tuple[PhysicalRestartEvent, ...]


def chain_fold_units(
    events: Iterable[PhysicalRestartEvent],
) -> tuple[tuple[PhysicalRestartEvent, ...], ...]:
    """Group events by continuity chain, oldest chain first."""

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


def plan_ring_folding(
    state: LearnedState,
    events: Iterable[PhysicalRestartEvent],
    *,
    capacity: int = RECENT_RING_CAPACITY,
) -> FoldPlan:
    """Plan folds so that at most ``capacity`` raw events remain in the ring.

    Whole older chains are folded first.  If the newest chain alone exceeds
    the capacity it is folded up to a boundary restart that stays in the
    ring as the already-credited anchor (``chain_open=True``); the anchor
    counts against the capacity.  The ring never grows with chain length.
    """

    if capacity < MINIMUM_RING_CAPACITY:
        raise LearnedStateError("ring capacity must be at least 2")
    mark = _watermark_key(state)
    anchor_id = state.open_chain.anchor_event_id if state.open_chain is not None else None
    anchor = None
    unfolded: dict[str, PhysicalRestartEvent] = {}
    for event in events:
        if event.server_key != state.server_key:
            continue
        if event.event_id == anchor_id:
            anchor = event
        elif _newer(event, mark):
            unfolded.setdefault(event.event_id, event)
    chains = [list(unit) for unit in chain_fold_units(unfolded.values())]
    open_id = state.open_chain.chain_id if state.open_chain is not None else None
    units: list[FoldUnit] = []

    def context(chain: list[PhysicalRestartEvent]) -> tuple[PhysicalRestartEvent, ...]:
        if anchor is not None and chain and chain[0].continuity_chain_id == open_id:
            return (anchor, *chain)
        return tuple(chain)

    def ring_size() -> int:
        kept_anchor = anchor is not None and bool(
            chains,
        ) and chains[0][0].continuity_chain_id == open_id
        return sum(len(chain) for chain in chains) + int(kept_anchor)

    while chains and ring_size() > capacity:
        if len(chains) > 1:
            chain = chains.pop(0)
            units.append(FoldUnit(context(chain), chain_open=False))
            if chain[0].continuity_chain_id == open_id:
                anchor, open_id = None, None
            continue
        chain = chains[0]
        keep = capacity - 1
        prefix, rest = chain[:-keep], chain[-keep:]
        units.append(FoldUnit(context(prefix), chain_open=True))
        anchor, open_id = prefix[-1], prefix[-1].continuity_chain_id
        chains[0] = rest
    ring: list[PhysicalRestartEvent] = []
    if anchor is not None and chains and chains[0][0].continuity_chain_id == open_id:
        ring.append(anchor)
    ring.extend(event for chain in chains for event in chain)
    return FoldPlan(tuple(units), tuple(ring))


def _tag_bin(
    pool: dict[int, PhaseBin],
    index: int,
    weight: float,
    *,
    period: int | None,
    confirmation_period: int | None,
) -> None:
    value = pool[index]
    if period is not None:
        tags = {item.period: item for item in value.endpoints}
        previous = tags.get(period)
        tags[period] = EndpointTag(
            period,
            (previous.weight if previous else 0.0) + weight,
            (previous.count if previous else 0) + 1,
        )
        value = replace(value, endpoints=tuple(tags[key] for key in sorted(tags)))
    if confirmation_period is not None:
        previous = value.confirmation
        value = replace(
            value,
            confirmation=EndpointTag(
                confirmation_period,
                (previous.weight if previous else 0.0) + weight,
                (previous.count if previous else 0) + 1,
            ),
        )
    pool[index] = value


def _tag_anchor(
    pool: dict[int, PhaseBin], index: int, at: float, weight: float, period: int
) -> None:
    """Tag the (latest) open-chain anchor as a high endpoint after the fact."""

    value = pool[index]
    tags = {item.period: item for item in value.endpoints}
    previous = tags.get(period)
    if previous is None:
        untagged = (
            (value.first_at, value.weight_before, value.tagged_before_at(period))
            if value.count > 1 and value.first_at < at
            else (None, None, None)
        )
        tags[period] = EndpointTag(period, weight, 1, at, at, *untagged)
    else:
        untagged = (
            previous.untagged_first_at,
            previous.untagged_weight_before,
            previous.untagged_tagged_before,
        )
        if previous.untagged_first_at is not None and previous.untagged_first_at >= at:
            # The anchor is the newest restart: if it was the earliest untagged
            # one, no untagged restart remains in the bin.
            untagged = (None, None, None)
        tags[period] = replace(
            previous,
            weight=previous.weight + weight,
            count=previous.count + 1,
            first_tagged_at=min(previous.first_tagged_at or at, at),
            last_tagged_at=max(previous.last_tagged_at or at, at),
            untagged_first_at=untagged[0],
            untagged_weight_before=untagged[1],
            untagged_tagged_before=untagged[2],
        )
    pool[index] = replace(value, endpoints=tuple(tags[key] for key in sorted(tags)))


def _interval_bin_add_relationship(
    existing: IntervalBin | None,
    relationship: IntervalRelationship,
    quality: float,
    full_quality: float,
) -> IntervalBin:
    period = relationship.candidate_period_seconds
    index, offset = _interval_bin(_relationship_phase_value(relationship, period), period)
    value = existing or IntervalBin(period, index)
    return replace(
        value,
        relationship_count=value.relationship_count + 1,
        phase_min=offset if value.phase_min is None else min(value.phase_min, offset),
        phase_max=offset if value.phase_max is None else max(value.phase_max, offset),
        base_residual=value.base_residual * (1.0 - 0.50 * quality),
        full_interval_count=value.full_interval_count + int(full_quality > 0),
        full_interval_residual=(
            value.full_interval_residual * (
                1.0 - SPARSE_CONFIRMATION_INTERVAL_WEIGHT * full_quality
            )
            if full_quality > 0
            else value.full_interval_residual
        ),
        first_at=relationship.left_at if value.first_at is None else min(
            value.first_at,
            relationship.left_at,
        ),
        last_at=relationship.right_at if value.last_at is None else max(
            value.last_at,
            relationship.right_at,
        ),
    )


def _interval_bin_add_streak(
    existing: IntervalBin | None,
    run: _Run,
) -> tuple[tuple[int, int], IntervalBin]:
    summary = run.summary()
    period = run.period
    index, _offset = _interval_bin(summary.phase_estimate, period)
    value = existing if existing is not None and existing.index == index else None
    value = value or IntervalBin(period, index)
    low, _high = run.relationship_range()
    low = _signed_phase_residual(low, value.start, period)
    high = low + (run.relationship_max - run.relationship_min)
    shape = (1.0 - 2.0 ** -(summary.interval_count - 1)) * summary.min_quality
    best = value.best_streak
    if best is None or (
        summary.interval_count,
        summary.end_at,
    ) > (best.interval_count, best.end_at):
        best = summary
    return (period, index), replace(
        value,
        streak_count=value.streak_count + 1,
        streak_bonus=value.streak_bonus + round(STREAK_BONUS_COEFFICIENT * shape, 6),
        gate_bonus=value.gate_bonus + round(GATE_STREAK_BONUS_COEFFICIENT * shape, 6),
        streak_min=low if value.streak_min is None else min(value.streak_min, low),
        streak_max=high if value.streak_max is None else max(value.streak_max, high),
        best_streak=best,
    )


def fold_unit(
    state: LearnedState,
    *,
    events: Sequence[PhysicalRestartEvent],
    relationships: Iterable[IntervalRelationship] = (),
    chain_open: bool = False,
) -> LearnedState:
    """Fold one unit: a whole chain, or a chain prefix when ``chain_open``.

    Events at or before the watermark are rejected (they may be passed as
    context, such as the open chain's anchor).  A relationship is folded
    exactly once: when its right endpoint is folded, provided its left
    endpoint is folded now or is the open chain's anchor.  Ineligible events
    contribute no evidence but advance the watermark.  With ``chain_open``
    the last folded restart becomes the anchor and the streak through it is
    carried as an ``OpenRun``.
    """

    mark = _watermark_key(state)
    context: dict[str, PhysicalRestartEvent] = {}
    accepted: dict[str, PhysicalRestartEvent] = {}
    for event in events:
        if event.server_key != state.server_key or _event_at(event) is None:
            continue
        context.setdefault(event.event_id, event)
        if _newer(event, mark):
            accepted.setdefault(event.event_id, event)
    if not accepted:
        return state
    ordered = sorted(accepted.values(), key=lambda item: (_event_at(item), item.event_id))
    chain_id = ordered[-1].continuity_chain_id

    open_chain = state.open_chain
    seed = None
    stale = None
    anchor_id = None
    anchor_high_periods: set[int] = set()
    anchor_confirmation_tagged = False
    if (
        open_chain is not None
        and open_chain.chain_id == chain_id
        and open_chain.anchor_event_id in context
    ):
        seed = _run_from_open(open_chain)
        anchor_id = open_chain.anchor_event_id
        anchor_high_periods = set(open_chain.anchor_high_periods)
        anchor_confirmation_tagged = open_chain.anchor_confirmation_tagged
    elif open_chain is not None:
        # The open chain did not continue into this unit: its streak closes.
        stale = _run_from_open(open_chain)

    pool = {item.index: item for item in state.pool}
    intervals = {(item.period, item.index): item for item in state.intervals}
    epoch = state.epoch
    challenger = {(item.period, item.index): item for item in epoch.challenger}
    confirmation_count = epoch.confirmation_count
    confirmation_residual = epoch.confirmation_residual
    recovery = list(epoch.recovery)
    incumbent = state.incumbent
    last_aligned = None if incumbent is None else incumbent.last_aligned_evidence_at

    def left_ok(relationship: IntervalRelationship) -> bool:
        return relationship.left_event_id in accepted or relationship.left_event_id == anchor_id

    folded = sorted(
        {
            item.relationship_id: item
            for item in relationships
            if item.server_key == state.server_key
            and item.right_event_id in accepted
            and left_ok(item)
            and item.left_event_id in context
        }.values(),
        key=lambda item: (item.left_at, item.right_at, item.relationship_id),
    )
    tagged: set[tuple[int, str]] = set()
    confirmation_tagged: dict[str, int] = {}
    high_items: list[tuple[IntervalRelationship, float]] = []
    anchor_new_periods: list[int] = []
    anchor_confirmation_period: int | None = None

    def tag(event_id: str, *, period: int | None = None, confirmation: int | None = None) -> None:
        """Record tags; they are applied in time order after this pass."""

        nonlocal anchor_confirmation_tagged, anchor_confirmation_period
        event = context[event_id]
        if not _sparse_usable(event):
            return
        if event_id == anchor_id:
            if period is not None and period not in anchor_high_periods:
                anchor_high_periods.add(period)
                anchor_new_periods.append(period)
            if confirmation is not None and not anchor_confirmation_tagged:
                anchor_confirmation_tagged = True
                anchor_confirmation_period = confirmation
            return
        if period is not None and (period, event_id) not in tagged:
            tagged.add((period, event_id))
        if confirmation is not None and event_id not in confirmation_tagged:
            confirmation_tagged[event_id] = confirmation

    for relationship in folded:
        period = relationship.candidate_period_seconds
        pair = {
            relationship.left_event_id: context[relationship.left_event_id],
            relationship.right_event_id: context[relationship.right_event_id],
        }
        full_quality = _full_interval_quality(relationship, pair)
        if _high_direct(relationship):
            quality = relationship_authority_quality(relationship, pair.values())
            high_items.append((relationship, quality))
            key = (
                period,
                _interval_bin(_relationship_phase_value(relationship, period), period)[0],
            )
            intervals[key] = _interval_bin_add_relationship(
                intervals.get(key),
                relationship,
                quality,
                full_quality,
            )
            tag(relationship.left_event_id, period=period)
            tag(relationship.right_event_id, period=period)
            if epoch.established_at is not None and relationship.left_at >= epoch.established_at:
                challenger[key] = _interval_bin_add_relationship(
                    challenger.get(key), relationship, quality, full_quality
                )
            if incumbent is not None and _incumbent_recovery_interval(
                relationship,
                incumbent,
                pair,
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
            confirmation_count += 1
            confirmation_residual *= 1.0 - SPARSE_CONFIRMATION_INTERVAL_WEIGHT * full_quality
            tag(relationship.left_event_id, confirmation=period)
            tag(relationship.right_event_id, confirmation=period)

    def close(run: _Run) -> None:
        if run.interval_count < 2:
            return
        key = (run.period, _interval_bin(run.summary().phase_estimate, run.period)[0])
        key, intervals[key] = _interval_bin_add_streak(intervals.get(key), run)
        if epoch.established_at is not None and run.start_at >= epoch.established_at:
            key, challenger[key] = _interval_bin_add_streak(challenger.get(key), run)

    if stale is not None:
        close(stale)
    # Apply tags and fold restarts in time order (exact untagged tracking).
    totals = dict(state.tagged_weight_totals)
    if anchor_id is not None and anchor_new_periods and open_chain.anchor_index in pool:
        for period in anchor_new_periods:
            _tag_anchor(
                pool,
                open_chain.anchor_index,
                open_chain.anchor_at,
                open_chain.anchor_weight,
                period,
            )
            totals[period] = totals.get(period, 0.0) + open_chain.anchor_weight
    pool_total = state.pool_weight_total
    learning_count = 0
    for event in ordered:
        at = _event_at(event)
        if _sparse_usable(event):
            learning_count += 1
            weight = _event_weight(event)
            uncertainty = _capped_uncertainty(event.phase_uncertainty)
            index, offset = _day_bin(at)
            existing = pool.get(index)
            snapshot = dict(totals)
            if existing is None:
                pool[index] = PhaseBin(
                    index,
                    weight,
                    1,
                    offset,
                    offset,
                    at,
                    at,
                    uncertainty,
                    uncertainty,
                    pool_total,
                    tagged_before=tuple(
                        (key, value) for key, value in sorted(snapshot.items()) if value > 0
                    ),
                )
            else:
                pool[index] = replace(
                    existing,
                    weight=existing.weight + weight,
                    count=existing.count + 1,
                    offset_min=min(existing.offset_min, offset),
                    offset_max=max(existing.offset_max, offset),
                    first_at=min(existing.first_at, at),
                    last_at=max(existing.last_at, at),
                    uncertainty_min=min(existing.uncertainty_min, uncertainty),
                    uncertainty_max=max(existing.uncertainty_max, uncertainty),
                )
            event_periods = {period for period, event_id in tagged if event_id == event.event_id}
            tags = {item.period: item for item in pool[index].endpoints}
            for period in sorted(event_periods):
                previous = tags.get(period)
                if previous is None:
                    untagged = (
                        (
                            existing.first_at,
                            existing.weight_before,
                            existing.tagged_before_at(period),
                        )
                        if existing is not None
                        else (None, None, None)
                    )
                    tags[period] = EndpointTag(period, weight, 1, at, at, *untagged)
                else:
                    tags[period] = replace(
                        previous,
                        weight=previous.weight + weight,
                        count=previous.count + 1,
                        last_tagged_at=at,
                    )
                totals[period] = totals.get(period, 0.0) + weight
            for period, value in list(tags.items()):
                if period not in event_periods and value.untagged_first_at is None:
                    tags[period] = replace(
                        value,
                        untagged_first_at=at,
                        untagged_weight_before=pool_total,
                        untagged_tagged_before=snapshot.get(period, 0.0),
                    )
            pool[index] = replace(pool[index], endpoints=tuple(tags[key] for key in sorted(tags)))
            pool_total += weight
        if (
            incumbent is not None
            and _authentic_event(event)
            and _circular_distance(at, incumbent.phase_offset, incumbent.period)
            <= incumbent.phase_tolerance
        ):
            last_aligned = at if last_aligned is None else max(last_aligned, at)
    # Confirmation tags (epoch-scoped) keep the conservative bin-level rule.
    if anchor_confirmation_period is not None and open_chain.anchor_index in pool:
        _tag_bin(
            pool,
            open_chain.anchor_index,
            open_chain.anchor_weight,
            period=None,
            confirmation_period=anchor_confirmation_period,
        )
    for event_id, period in sorted(confirmation_tagged.items()):
        event = accepted.get(event_id) or context[event_id]
        index = _day_bin(_event_at(event))[0]
        if index in pool:
            _tag_bin(pool, index, _event_weight(event), period=None, confirmation_period=period)

    normal_bins = {(item.period, item.index): item for item in state.normal_relationships}
    for relationship in {
        item.relationship_id: item
        for item in relationships
        if item.server_key == state.server_key
        and item.left_event_id in accepted
        and not _high_direct(item)
    }.values():
        period = relationship.candidate_period_seconds
        index, offset = _interval_bin(_relationship_phase_value(relationship, period), period)
        existing = normal_bins.get((period, index))
        normal_bins[(period, index)] = (
            NormalBin(period, index, 1, offset, offset, relationship.left_at, relationship.right_at)
            if existing is None
            else replace(
                existing,
                count=existing.count + 1,
                phase_min=min(existing.phase_min, offset),
                phase_max=max(existing.phase_max, offset),
                first_at=min(existing.first_at, relationship.left_at),
                last_at=max(existing.last_at, relationship.right_at),
            )
        )
    divisors, contradicted_at = _invalidate_divisors(state.divisors, ordered)
    divisor_floor_at = state.divisor_floor_at
    if contradicted_at is not None:
        divisor_floor_at = (
            contradicted_at if divisor_floor_at is None else max(divisor_floor_at, contradicted_at)
        )

    new_anchor = ordered[-1] if chain_open and chain_id else None
    open_run = None
    for run in _build_runs(high_items, seed):
        if new_anchor is not None and run.last_event_id == new_anchor.event_id:
            open_run = run.to_open()
            continue
        close(run)

    if new_anchor is not None:
        high_periods = {period for period, event_id in tagged if event_id == new_anchor.event_id}
        index = _day_bin(_event_at(new_anchor))[0] if _sparse_usable(new_anchor) else None
        next_open = OpenChain(
            chain_id=chain_id,
            anchor_event_id=new_anchor.event_id,
            anchor_at=_event_at(new_anchor),
            anchor_index=index,
            anchor_weight=_event_weight(new_anchor) if index is not None else 0.0,
            anchor_high_periods=tuple(sorted(high_periods)),
            anchor_confirmation_tagged=new_anchor.event_id in confirmation_tagged,
            run=open_run,
        )
    else:
        next_open = None

    last = ordered[-1]
    watermark = FoldWatermark(
        at=_event_at(last),
        event_id=last.event_id,
        folded_event_count=state.watermark.folded_event_count + len(ordered),
        folded_learning_event_count=state.watermark.folded_learning_event_count + learning_count,
        window_end_at=state.watermark.window_end_at,
    )
    if incumbent is not None and last_aligned != incumbent.last_aligned_evidence_at:
        incumbent = replace(incumbent, last_aligned_evidence_at=last_aligned)
    return replace(
        state,
        watermark=watermark,
        pool_weight_total=pool_total,
        pool=tuple(pool[key] for key in sorted(pool)),
        intervals=tuple(intervals[key] for key in sorted(intervals)),
        incumbent=incumbent,
        epoch=replace(
            epoch,
            challenger=tuple(challenger[key] for key in sorted(challenger)),
            confirmation_count=confirmation_count,
            confirmation_residual=confirmation_residual,
            recovery=_greedy_recovery(recovery),
        ),
        open_chain=next_open,
        normal_relationships=tuple(normal_bins[key] for key in sorted(normal_bins)),
        tagged_weight_totals=tuple(
            (key, value) for key, value in sorted(totals.items()) if value > 0
        ),
        divisors=divisors,
        divisor_floor_at=divisor_floor_at,
    )


def apply_fold_plan(
    state: LearnedState, plan: FoldPlan, relationships: Iterable[IntervalRelationship]
) -> LearnedState:
    relationships = tuple(relationships)
    for unit in plan.units:
        state = fold_unit(
            state, events=unit.events, relationships=relationships, chain_open=unit.chain_open
        )
    return state


def latest_window_records(
    records: Iterable[ExpectedWindowLedgerRecord],
) -> tuple[ExpectedWindowLedgerRecord, ...]:
    values = {item.result_id: item for item in records}
    superseded = {
        item.supersedes_result_id for item in values.values(
        ) if item.supersedes_result_id is not None
    }
    return tuple(
        sorted(
            (item for item in values.values() if item.result_id not in superseded),
            key=lambda item: (item.window_end_at, item.result_id),
        )
    )


def fold_windows(
    state: LearnedState,
    records: Iterable[ExpectedWindowLedgerRecord],
) -> LearnedState:
    """Fold final expected-window outcomes (latest revision per lineage) once."""

    misses = {(item.period, item.index): item for item in state.misses}
    mark = state.watermark.window_end_at
    latest_end = mark
    changed = False
    for record in latest_window_records(
        item for item in records if item.server_key == state.server_key
    ):
        if mark is not None and record.window_end_at <= mark:
            continue
        latest_end = record.window_end_at if latest_end is None else max(
            latest_end,
            record.window_end_at,
        )
        genuine = (
            record.outcome is ExpectedWindowOutcome.GENUINE_MISS and record.negative_penalty_active
        )
        hit = record.outcome is ExpectedWindowOutcome.HIT
        if not (genuine or hit):
            continue
        period = record.candidate_period_seconds
        index, offset = _interval_bin(record.expected_phase_offset, period)
        value = misses.get((period, index)) or MissBin(period, index, offset, offset)
        value = replace(
            value,
            phase_min=min(value.phase_min, offset),
            phase_max=max(value.phase_max, offset),
        )
        if genuine:
            value = replace(
                value,
                genuine_miss_count=value.genuine_miss_count + 1,
                last_genuine_miss_at=max(value.last_genuine_miss_at or 0.0, record.expected_at),
            )
        else:
            value = replace(value, last_hit_at=max(value.last_hit_at or 0.0, record.expected_at))
        misses[(period, index)] = value
        changed = True
    if latest_end == mark and not changed:
        return state
    return replace(
        state,
        misses=tuple(misses[key] for key in sorted(misses)),
        watermark=replace(state.watermark, window_end_at=latest_end),
    )


def invalidate_contradicted_divisors(
    state: LearnedState, events: Iterable[PhysicalRestartEvent]
) -> LearnedState:
    """Remove divisor evidence contradicted by (unfolded) authentic restarts.

    Raises the divisor floor to the contradiction, so a schema-3 window that
    still contains pre-contradiction evidence cannot re-establish it.
    """

    mark = _watermark_key(state)
    values = [
        item for item in events if item.server_key == state.server_key and _newer(item, mark)
    ]
    divisors, contradicted_at = _invalidate_divisors(state.divisors, values)
    if contradicted_at is None:
        return state
    floor = state.divisor_floor_at
    return replace(
        state,
        divisors=divisors,
        divisor_floor_at=contradicted_at if floor is None else max(floor, contradicted_at),
    )


def fold_divisor_resolution(
    state: LearnedState, score, recent_events: Iterable[PhysicalRestartEvent] = ()
) -> LearnedState:
    """Learn divisor resolution only from a clean, current schema-3 window.

    ``recent_events`` are the schema-3 scorer's recent-window events (the only
    events its divisor resolution is computed from).  Resolution is learned
    for (period, divisor) only when the live scorer has it established, the
    window contains no restart at a divisor midpoint of the period's phase
    (it is not already contradicted), and the window starts after the
    divisor floor (the latest contradiction or supersession), so an old
    schema-3 window can never re-establish stale resolution.  Counts are the
    best observed within the same phase; a different phase starts afresh.
    The scorer's thresholds are unchanged.
    """

    events = [item for item in recent_events if _event_at(item) is not None]
    if not events:
        return state
    window_start = min(_event_at(item) for item in events)
    window_end = max(_event_at(item) for item in events)
    floor = state.divisor_floor_at
    if floor is not None and window_start <= floor:
        return state
    authentic = [_event_at(item) for item in events if _authentic_event(item)]
    stored = {(item.period, item.divisor): item for item in state.divisors}
    for candidate in getattr(score, "candidates", ()):
        phase = candidate.phase_offset
        if phase is None:
            continue
        period = candidate.period_seconds
        for item in candidate.divisor_resolution:
            key = (period, item.divisor_seconds)
            if key not in DIVISOR_KEYS or not item.established_resolved:
                continue
            probe = DivisorEvidence(
                period, item.divisor_seconds, 0, 0, phase % period, window_start
            )
            if any(divisor_contradicted(probe, at) for at in authentic):
                continue
            previous = stored.get(key)
            if previous is not None and (
                _circular_distance(previous.phase_offset, phase, period)
                > candidate_phase_tolerance(period)
            ):
                previous = None
            stored[key] = DivisorEvidence(
                period,
                item.divisor_seconds,
                max(item.covered_window_count, previous.covered_window_count if previous else 0),
                max(item.longer_interval_count, previous.longer_interval_count if previous else 0),
                previous.phase_offset if previous is not None else phase % period,
                max(window_end, previous.observed_at if previous is not None else window_end),
            )
    values = tuple(stored[key] for key in sorted(stored))
    return state if values == state.divisors else replace(state, divisors=values)


def _knowledge_from_regime(regime: RegimeRecord, *, last_aligned: float | None) -> RegimeKnowledge:
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
        # References into raw evidence only; identity is ``regime_id``.
        defining_evidence_ids=tuple(regime.defining_evidence_ids[:MAX_DEFINING_EVIDENCE_IDS]),
        reason_codes=tuple(sorted(set(regime.reason_codes)))[:MAX_REASON_CODES],
    )


def observe_decision(state: LearnedState, decision: AuthorityDecision) -> LearnedState:
    """Record the authority's incumbent and regime boundaries as knowledge.

    Superseded regimes are merged per (period, 5-minute phase bin) by
    widening the tolerance to cover both and keeping the later boundary, so
    the separated evidence is a superset of each regime's own (old evidence
    never supports a conflicting regime).  A change of incumbent identity or
    establishment boundary starts a new epoch.
    """

    superseded = {item.key: item for item in state.superseded}
    supersession_seen = False
    for regime in decision.regimes:
        if regime.status is not RegimeRecordStatus.SUPERSEDED or regime.superseded_at is None:
            continue
        period = regime.candidate_period_seconds
        value = SupersededRegime(
            period,
            regime.phase_offset % period,
            regime.phase_tolerance,
            regime.superseded_at,
        )
        previous = superseded.get(value.key)
        if previous is None:
            supersession_seen = True
            superseded[value.key] = value
            continue
        widened = max(
            previous.phase_tolerance,
            value.phase_tolerance + _circular_distance(
                previous.phase_offset,
                value.phase_offset,
                period,
            ),
        )
        merged = replace(
            previous,
            phase_tolerance=widened,
            superseded_at=max(previous.superseded_at, value.superseded_at),
        )
        superseded[value.key] = merged

    regime = decision.incumbent_shadow_regime
    previous = state.incumbent
    if regime is None:
        incumbent = None
    else:
        same = previous is not None and previous.regime_id == regime.regime_id
        last_aligned = previous.last_aligned_evidence_at if same else _folded_last_aligned(
            state,
            regime,
        )
        incumbent = _knowledge_from_regime(regime, last_aligned=last_aligned)
    epoch = state.epoch
    pool = state.pool
    open_chain = state.open_chain
    key = (
        None if incumbent is None else incumbent.regime_id,
        None if incumbent is None else incumbent.established_at,
    )
    if key != (epoch.regime_id, epoch.established_at):
        epoch = EpochEvidence(regime_id=key[0], established_at=key[1])
        pool = tuple(
            replace(item, confirmation=None) if item.confirmation else item for item in pool
        )
        if open_chain is not None:
            open_chain = replace(open_chain, anchor_confirmation_tagged=False)
    divisors = state.divisors
    divisor_floor_at = state.divisor_floor_at
    if supersession_seen:
        boundary = max(item.superseded_at for item in superseded.values())
        divisors = ()
        divisor_floor_at = boundary if divisor_floor_at is None else max(divisor_floor_at, boundary)
    return replace(
        state,
        pool=pool,
        incumbent=incumbent,
        superseded=tuple(superseded[item] for item in sorted(superseded)),
        epoch=epoch,
        open_chain=open_chain,
        divisors=divisors,
        divisor_floor_at=divisor_floor_at,
    )


def _folded_last_aligned(state: LearnedState, regime: RegimeRecord) -> float | None:
    """Latest folded restart wholly aligned with a newly observed incumbent."""

    values = [
        item.last_at
        for item in state.pool
        if _range_abs(
            item.seconds_min,
            item.seconds_max,
            regime.phase_offset,
            regime.candidate_period_seconds,
        )[1]
        <= regime.phase_tolerance
    ]
    return max(values, default=None)


# --------------------------------------------------------------------------
# Evaluation (pure, no time input)
# --------------------------------------------------------------------------


_ALIGNED, _OFF, _AMBIGUOUS = "aligned", "off", "ambiguous"


@dataclass(frozen=True)
class _Entry:
    low: float
    high: float
    weight: float
    count: int
    first_at: float
    last_at: float
    uncertainty_min: float
    uncertainty_max: float
    source: PhaseBin | None = None

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


def _bin_entry(item: PhaseBin, *, weight: float | None = None, count: int | None = None) -> _Entry:
    return _Entry(
        item.seconds_min,
        item.seconds_max,
        item.weight if weight is None else weight,
        item.count if count is None else count,
        item.first_at,
        item.last_at,
        item.uncertainty_min,
        item.uncertainty_max,
        item,
    )


def _ring_entry(event: PhysicalRestartEvent) -> _Entry:
    at = _event_at(event)
    uncertainty = _capped_uncertainty(event.phase_uncertainty)
    return _Entry(at, at, _event_weight(event), 1, at, at, uncertainty, uncertainty, None)


def _classify(entry: _Entry, phase: float, period: int) -> tuple[str, float]:
    """Conservative alignment class and the largest possible MAD value."""

    tolerance = candidate_phase_tolerance(period)
    smallest, largest = _range_abs(entry.low, entry.high, phase, period)
    if largest <= tolerance + entry.uncertainty_min:
        state = _ALIGNED
    elif smallest > tolerance + entry.uncertainty_max:
        state = _OFF
    else:
        state = _AMBIGUOUS
    return state, max(largest, entry.uncertainty_max)


def ring_sparse_events(
    state: LearnedState, events: Iterable[PhysicalRestartEvent]
) -> tuple[PhysicalRestartEvent, ...]:
    mark = _watermark_key(state)
    values = {
        item.event_id: item
        for item in events
        if item.server_key == state.server_key and _sparse_usable(item) and _newer(item, mark)
    }
    return tuple(sorted(values.values(), key=lambda item: (_event_at(item), item.event_id)))


def _ring_map(
    state: LearnedState,
    events: Iterable[PhysicalRestartEvent],
) -> dict[str, PhysicalRestartEvent]:
    """Unfolded ring events plus the open chain's anchor (context only)."""

    mark = _watermark_key(state)
    anchor_id = state.open_chain.anchor_event_id if state.open_chain is not None else None
    return {
        item.event_id: item
        for item in events
        if item.server_key == state.server_key and (
            _newer(item, mark) or item.event_id == anchor_id
        )
    }


def _ring_relationships(
    state: LearnedState,
    relationships: Iterable[IntervalRelationship],
    ring_map: Mapping[str, PhysicalRestartEvent],
) -> tuple[IntervalRelationship, ...]:
    """Unfolded relationships: right endpoint unfolded, left unfolded or anchor."""

    mark = _watermark_key(state)
    values = {
        item.relationship_id: item
        for item in relationships
        if item.server_key == state.server_key
        and item.left_event_id in ring_map
        and item.right_event_id in ring_map
        and _newer(ring_map[item.right_event_id], mark)
    }
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (item.left_at, item.right_at, item.relationship_id),
        ),
    )


@dataclass(frozen=True)
class CompactSparseSupport:
    value: float
    effective: float
    quality_band: float
    phase_confidence: float
    off_phase_weight: float
    consistency: float
    aligned_count: int
    first_aligned_at: float | None
    last_aligned_at: float | None
    aligned_ranges: tuple[tuple[float, float], ...]


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
    exclude_anchor: bool = False,
) -> CompactSparseSupport:
    """``authority._sparse_support`` over folded bins plus ring events.

    Exact for ring events.  A folded bin is aligned only when its whole
    offset range is within tolerance; an ambiguous bin counts as off-phase.
    Its MAD value is the largest residual in its range.  The domain total is
    exact (``pool_weight_total - weight_before``); excluded endpoints are
    removed from it only when the bin is wholly inside the domain, and
    superseded-explained restarts count as historical only then (otherwise
    off-phase).  Superseded explanation uses the bin's closest residual.
    Every approximation lowers, never raises, the result.
    """

    excluded = set(excluded_ring_event_ids)
    historical = [
        regime
        for regime in state.superseded
        if period != regime.period
        or _circular_distance(phase_offset, regime.phase_offset, period) > regime.phase_tolerance
    ]
    anchor = state.open_chain
    anchor_index = None
    if exclude_anchor and anchor is not None and anchor.anchor_index is not None:
        covered = (exclude_folded_high_endpoints and period in anchor.anchor_high_periods) or (
            exclude_folded_confirmation_endpoints and anchor.anchor_confirmation_tagged
        )
        if not covered:
            anchor_index = anchor.anchor_index

    def explained(entry: _Entry) -> bool:
        return any(
            entry.first_at <= regime.superseded_at
            and _range_abs(entry.low, entry.high, regime.phase_offset, regime.period)[0]
            <= regime.phase_tolerance
            for regime in historical
        )

    entries: list[tuple[_Entry, str, float, bool]] = []
    removed: list[tuple[float, float]] = []
    # Domain anchors per folded bin: (pool total before, tagged total before).
    befores: dict[int, tuple[float, float]] = {}
    other_tags: list[EndpointTag] = []
    for item in state.pool:
        entry = _bin_entry(item)
        state_class, mad_value = _classify(entry, phase_offset, period)
        weight, count = item.weight, item.count
        first_at = item.first_at
        before = (item.weight_before, item.tagged_before_at(period))
        tag = item.tag(period)
        if state_class == _ALIGNED:
            removals = []
            if exclude_folded_high_endpoints and tag is not None:
                # The candidate's own endpoints leave the pool; the domain
                # starts at the earliest remaining restart (exact).
                weight -= tag.weight
                count -= tag.count
                if tag.untagged_first_at is not None:
                    first_at = tag.untagged_first_at
                    before = (tag.untagged_weight_before or 0.0, tag.untagged_tagged_before or 0.0)
            if exclude_folded_confirmation_endpoints and item.confirmation is not None:
                removals.append((item.confirmation.weight, item.confirmation.count, item.first_at))
            if anchor_index == item.index:
                removals.append((anchor.anchor_weight, 1, anchor.anchor_at))
            for removed_weight, removed_count, removed_at in removals:
                weight -= removed_weight
                count -= removed_count
                removed.append((removed_weight, removed_at))
        elif exclude_folded_high_endpoints and tag is not None:
            other_tags.append(tag)
        if count <= 0 or weight <= _EPSILON:
            continue
        entry = _Entry(
            item.seconds_min,
            item.seconds_max,
            weight,
            count,
            first_at,
            item.last_at,
            item.uncertainty_min,
            item.uncertainty_max,
            item,
        )
        befores[item.index] = before
        entries.append((entry, state_class, mad_value, explained(entry)))
    for event in ring_sparse_events(state, ring_events):
        if event.event_id in excluded:
            continue
        entry = _ring_entry(event)
        state_class, mad_value = _classify(entry, phase_offset, period)
        entries.append((entry, state_class, mad_value, explained(entry)))

    aligned = [
        (entry, mad_value)
        for entry, state_class, mad_value, hist in entries
        if state_class == _ALIGNED and not hist
    ]
    if not aligned:
        return _EMPTY_SUPPORT
    anchor_entry = min(
        (entry for entry, _mad in aligned),
        key=lambda item: (item.first_at, item.source is None),
    )
    first = anchor_entry.first_at
    effective = sum(entry.weight for entry, _mad in aligned)
    ring_total = sum(
        entry.weight
        for entry, _class, _mad, _hist in entries
        if entry.source is None and entry.first_at >= first
    )
    if anchor_entry.source is not None:
        pool_before, tagged_before = befores[anchor_entry.source.index]
        # Exact: every restart folded at or after the first aligned one.
        folded_total = state.pool_weight_total - pool_before
        if exclude_folded_high_endpoints:
            # Excluded endpoints folded at or after the domain start: all
            # period-P tagged weight since then, minus tags of bins that stay
            # in the pool (over-estimated, so the domain is never too small).
            tagged_since = next(
                (value for key, value in state.tagged_weight_totals if key == period), 0.0
            ) - tagged_before
            tagged_since -= sum(
                item.weight
                for item in other_tags
                if item.last_tagged_at is None or item.last_tagged_at >= first
            )
            folded_total -= max(0.0, tagged_since)
        folded_total -= sum(weight for weight, at in removed if at >= first)
    else:
        folded_total = 0.0
    total = folded_total + ring_total
    historical_in_domain = sum(
        entry.weight
        for entry, state_class, _mad, hist in entries
        if hist and state_class == _ALIGNED and entry.first_at >= first
    )
    off_phase_weight = max(0.0, total - effective - historical_in_domain)
    mad = _median_of_counts([(mad_value, entry.count) for entry, mad_value in aligned])
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
        sum(entry.count for entry, _mad in aligned),
        first,
        max(entry.last_at for entry, _mad in aligned),
        tuple(sorted((entry.low, entry.high) for entry, _mad in aligned)),
    )


def compact_phase_proposal(
    state: LearnedState,
    *,
    period: int,
    ring_events: Iterable[PhysicalRestartEvent] = (),
) -> tuple[float | None, float | None, int, float | None]:
    """``scoring._phase_cluster`` over the sparse pool (folded plus ring).

    A folded bin's anchor is the middle of its offset range and it is counted
    only when wholly aligned with the anchor.  Bounded cost: a windowed scan
    over at most ``DAY_BINS`` bins plus the ring.
    """

    entries = [_bin_entry(item) for item in state.pool]
    entries.extend(_ring_entry(item) for item in ring_sparse_events(state, ring_events))
    if not entries:
        return None, None, 0, None
    tolerance = candidate_phase_tolerance(period)
    radius = tolerance + PHASE_UNCERTAINTY_CAP + PHASE_BIN_SECONDS
    phases = sorted((entry.mid % period, index) for index, entry in enumerate(entries))
    extended = (
        [(value - period, index) for value, index in phases]
        + phases
        + [(value + period, index) for value, index in phases]
    )
    keys = [value for value, _index in extended]

    def aligned_with(anchor_phase: float) -> list[tuple[_Entry, float]]:
        low = bisect.bisect_left(keys, anchor_phase - radius)
        high = bisect.bisect_right(keys, anchor_phase + radius)
        seen: set[int] = set()
        values = []
        for _value, index in extended[low:high]:
            if index in seen:
                continue
            seen.add(index)
            state_class, mad_value = _classify(entries[index], anchor_phase, period)
            if state_class == _ALIGNED:
                values.append((entries[index], mad_value))
        return values

    best = None
    for anchor in sorted(entries, key=lambda item: (item.first_at, item.mid)):
        anchor_phase = anchor.mid % period
        aligned = aligned_with(anchor_phase)
        if not aligned:
            continue
        key = (
            sum(entry.weight for entry, _mad in aligned),
            sum(entry.count for entry, _mad in aligned),
            max(entry.last_at for entry, _mad in aligned),
        )
        if best is None or key > best[0]:
            best = (key, anchor_phase, aligned)
    if best is None:
        return None, None, 0, None
    _key, offset, aligned = best
    mad = _median_of_counts([(mad_value, entry.count) for entry, mad_value in aligned])
    count = sum(entry.count for entry, _mad in aligned)
    return offset, mad, count, max(entry.last_at for entry, _mad in aligned)


def compact_harmonic_blockers(
    state: LearnedState,
    period: int,
    normal_candidate: CandidateScore | None = None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    phase_offset: float | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``authority._harmonic_blockers`` from live plus persisted evidence.

    A divisor is resolved when the live schema-3 resolution resolves it
    (exactly the raw rule), or when persisted divisor evidence does: learned
    from a clean window at a compatible phase and never contradicted since
    (folded contradictions removed it at fold time; unfolded ring restarts
    are checked here).  The scorer's thresholds are unchanged, so persisted
    evidence only keeps resolution through inactivity; it can never resolve
    a divisor that live or contradicting evidence leaves unresolved.
    """

    if period not in HARMONIC_PERIODS:
        return (), ()
    mark = _watermark_key(state)
    unfolded = [
        _event_at(item)
        for item in ring_events
        if item.server_key == state.server_key
        and _newer(item, mark)
        and _authentic_event(item)
    ]
    stored = {
        item.divisor: item
        for item in state.divisors
        if item.period == period
        and (
            phase_offset is None
            or _circular_distance(item.phase_offset, phase_offset, period)
            <= candidate_phase_tolerance(period)
        )
        and not any(at > item.observed_at and divisor_contradicted(item, at) for at in unfolded)
    }
    live = (
        {item.divisor_seconds: item for item in normal_candidate.divisor_resolution}
        if normal_candidate is not None
        else {}
    )
    window_mark = state.watermark.window_end_at
    ring_misses = [
        item
        for item in latest_window_records(ring_windows)
        if item.outcome is ExpectedWindowOutcome.GENUINE_MISS
        and item.negative_penalty_active
        and (window_mark is None or item.window_end_at > window_mark)
    ]
    h2: list[str] = []
    h3: list[str] = []
    for divisor in (value for value in CANDIDATE_PERIODS if value < period and period % value == 0):
        persisted = stored.get(divisor)
        current = live.get(divisor)
        midpoints = sum(
            item.genuine_miss_count for item in state.misses if item.period == divisor
        ) + sum(item.candidate_period_seconds == divisor for item in ring_misses)
        established = bool(
            (current is not None and current.established_resolved)
            or (
                persisted is not None
                and persisted.covered_window_count >= 2
                and persisted.longer_interval_count >= 2
            )
            or midpoints >= 1
        )
        high = bool(
            (current is not None and current.high_resolved)
            or (
                persisted is not None
                and persisted.covered_window_count >= 4
                and persisted.longer_interval_count >= 3
            )
            or midpoints >= 2
        )
        if not established:
            h2.append(f"unresolved_harmonic_divisor_{divisor}")
        if not high:
            h3.append(f"high_harmonic_divisor_unresolved_{divisor}")
    return tuple(h2), tuple(h3)


def ring_normal_relationships(
    state: LearnedState, relationships: Iterable[IntervalRelationship]
) -> tuple[IntervalRelationship, ...]:
    """Non-high relationships not yet folded (left endpoint unfolded)."""

    mark = _watermark_key(state)
    values = {
        item.relationship_id: item
        for item in relationships
        if item.server_key == state.server_key
        and not _high_direct(item)
        and (mark is None or (item.left_at, item.left_event_id) > mark)
    }
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (item.left_at, item.right_at, item.relationship_id),
        )
    )


@dataclass(frozen=True)
class FoldedNormalSummary:
    count: int
    first_at: float | None
    last_at: float | None


def compact_folded_normal_relationships(
    state: LearnedState, *, period: int, phase_offset: float, tolerance: float
) -> FoldedNormalSummary:
    """Folded normal relationships possibly aligned with a phase (superset)."""

    values = [
        item
        for item in state.normal_relationships
        if item.period == period
        and _range_abs(
            item.index * INTERVAL_BIN_SECONDS + item.phase_min,
            item.index * INTERVAL_BIN_SECONDS + item.phase_max,
            phase_offset,
            period,
        )[0]
        <= tolerance
    ]
    return FoldedNormalSummary(
        sum(item.count for item in values),
        min((item.first_at for item in values), default=None),
        max((item.last_at for item in values), default=None),
    )


def compact_folded_off_phase_ids(
    state: LearnedState, *, period: int, phase_offset: float, tolerance: float, since: float
) -> tuple[str, ...]:
    """Bounded references to folded restarts possibly off-phase after ``since``.

    One reference per folded minute bin whose latest restart is after
    ``since`` and whose range is not wholly within tolerance (conservative:
    a possibly off-phase restart keeps suspicion and prevents isolation).
    """

    return tuple(
        f"learned-off-phase:{item.index}"
        for item in state.pool
        if item.last_at > since
        and _range_abs(item.seconds_min, item.seconds_max, phase_offset, period)[1] > tolerance
    )


def compact_folded_miss_ids(
    state: LearnedState, *, period: int, phase_offset: float, tolerance: float, since: float
) -> tuple[str, ...]:
    """Bounded references to folded genuine misses possibly in phase after ``since``."""

    return tuple(
        f"learned-miss:{item.period}:{item.index}"
        for item in state.misses
        if item.period == period
        and item.last_genuine_miss_at is not None
        and item.last_genuine_miss_at > since
        and _range_abs(
            item.index * INTERVAL_BIN_SECONDS + item.phase_min,
            item.index * INTERVAL_BIN_SECONDS + item.phase_max,
            phase_offset,
            period,
        )[0]
        <= tolerance
    )


def compact_folded_restart_after(
    state: LearnedState,
    *,
    period: int,
    phase_offset: float,
    tolerance: float,
    after: float,
    aligned: bool,
) -> bool:
    """Whether a folded restart after ``after`` may be aligned (or off-phase)."""

    for item in state.pool:
        if item.last_at <= after:
            continue
        smallest, largest = _range_abs(item.seconds_min, item.seconds_max, phase_offset, period)
        if aligned and smallest <= tolerance + item.uncertainty_max:
            return True
        if not aligned and largest > tolerance:
            return True
    return False


def compact_sparse_structure_reasons(
    state: LearnedState,
    *,
    period: int,
    support: CompactSparseSupport,
    normal_candidate: CandidateScore | None = None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
    ring_events: Iterable[PhysicalRestartEvent] = (),
    phase_offset: float | None = None,
) -> tuple[str, ...]:
    """``authority._sparse_structure_reasons`` (SE1-SE5).

    SE4 period refutation requires certainty: an aligned folded bin refutes a
    longer multiple only when every restart it can hold differs from the
    first aligned restart by more than that multiple's tolerance.
    """

    if not support.aligned_count or support.first_aligned_at is None:
        return ()
    first = support.first_aligned_at
    refuted = all(
        any(
            _range_abs(low, high, first, multiple)[0] > candidate_phase_tolerance(multiple)
            for low, high in support.aligned_ranges
        )
        for multiple in CANDIDATE_PERIODS
        if multiple > period and multiple % period == 0
    )
    _h2, h3 = compact_harmonic_blockers(
        state,
        period,
        normal_candidate,
        ring_windows,
        ring_events=ring_events,
        phase_offset=phase_offset,
    )
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
    """SE6: an active, possibly in-phase genuine miss at or after ``since``."""

    if any(
        item.period == period
        and item.last_genuine_miss_at is not None
        and item.last_genuine_miss_at >= since
        and _range_abs(
            item.index * INTERVAL_BIN_SECONDS + item.phase_min,
            item.index * INTERVAL_BIN_SECONDS + item.phase_max,
            phase_offset,
            period,
        )[0]
        <= phase_tolerance
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
    return round(
        high_authority
        + (1.0 - high_authority)
        * min(SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * aligned_normal_confidence),
        6,
    )


@dataclass(frozen=True)
class CompactCandidate:
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
    excludes_anchor: bool
    sparse_reason_codes: tuple[str, ...]
    longest_streak: int
    ring_relationship_ids: tuple[str, ...] = ()
    ring_relationship_qualities: tuple[tuple[str, float], ...] = ()
    streaks: tuple[StreakSummary, ...] = ()


@dataclass
class _Component:
    low: float
    high: float
    members: list


def _components(
    members: list[tuple[float, float, object]],
    period: int,
    tolerance: float,
) -> list[_Component]:
    """Exact single-linkage components on the circle from member extremes."""

    if not members:
        return []
    ordered = sorted(members, key=lambda item: (item[0], item[1]))
    groups = [_Component(ordered[0][0], ordered[0][1], [ordered[0][2]])]
    for low, high, value in ordered[1:]:
        if low - groups[-1].high <= tolerance:
            groups[-1].high = max(groups[-1].high, high)
            groups[-1].members.append(value)
        else:
            groups.append(_Component(low, high, [value]))
    if len(groups) > 1 and groups[0].low + period - groups[-1].high <= tolerance:
        last = groups.pop()
        groups[0] = _Component(last.low, groups[0].high + period, last.members + groups[0].members)
    return groups


def _within(component: _Component, low: float, high: float, period: int) -> bool:
    start = component.low + ((low - component.low) % period)
    return start + (high - low) <= component.high + 1e-6


def compact_candidates(
    state: LearnedState,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_relationships: Iterable[IntervalRelationship] = (),
    normal_score=None,
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
    challenger: bool = False,
) -> tuple[CompactCandidate, ...]:
    """High (continuous) candidates from folded interval bins plus the ring.

    Base authority and streak bonuses are exact.  Streaks (ring runs, the
    open run and folded closed streaks) attach to a component only when all
    their relationship phases lie inside it (authority counts a streak only
    when it is contained in the component).  The phase comes from the
    longest streak; an open streak's spread is an upper bound.  A component
    with folded bins and no streak uses bin extremes for its spread.
    ``challenger=True`` evaluates the epoch's post-establishment evidence.
    """

    ring_events = tuple(ring_events)
    ring_map = _ring_map(state, ring_events)
    epoch_start = state.epoch.established_at
    anchor_id = state.open_chain.anchor_event_id if state.open_chain is not None else None
    rels = tuple(
        item
        for item in _ring_relationships(state, ring_relationships, ring_map)
        if _high_direct(item) and (
            not challenger or (epoch_start is not None and item.left_at >= epoch_start)
        )
    )
    qualities = {
        item.relationship_id: relationship_authority_quality(
            item, (ring_map[item.left_event_id], ring_map[item.right_event_id])
        )
        for item in rels
    }
    seed = _run_from_open(state.open_chain) if state.open_chain is not None else None
    runs = [
        run
        for run in _build_runs(((item, qualities[item.relationship_id]) for item in rels), seed)
        if run.interval_count >= 2
        and (not challenger or (epoch_start is not None and run.start_at >= epoch_start))
    ]
    bins = state.epoch.challenger if challenger else state.intervals
    results = []
    for period in CANDIDATE_PERIODS:
        tolerance = candidate_phase_tolerance(period)
        members: list[tuple[float, float, object]] = []
        for item in rels:
            if item.candidate_period_seconds == period:
                value = _relationship_phase_value(item, period)
                members.append((value, value, item))
        for item in bins:
            if item.period == period and item.relationship_count and item.phase_min is not None:
                members.append((item.start + item.phase_min, item.start + item.phase_max, item))
        for component in _components(members, period, tolerance):
            ring_rels = [
                item for item in component.members if isinstance(item, IntervalRelationship)
            ]
            folded = [item for item in component.members if isinstance(item, IntervalBin)]
            factor = 1.0
            for item in ring_rels:
                factor *= 1.0 - 0.50 * qualities[item.relationship_id]
            for item in folded:
                factor *= item.base_residual
            base = 1.0 - factor
            streak_bonus = 0.0
            gate_bonus = 0.0
            summaries: list[StreakSummary] = []
            for run in runs:
                if run.period != period:
                    continue
                low, high = run.relationship_range()
                if not _within(component, low, high, period):
                    continue
                summary = run.summary()
                shape = (1.0 - 2.0 ** -(summary.interval_count - 1)) * summary.min_quality
                streak_bonus += round(STREAK_BONUS_COEFFICIENT * shape, 6)
                gate_bonus += round(GATE_STREAK_BONUS_COEFFICIENT * shape, 6)
                summaries.append(summary)
            for item in bins:
                if item.period != period or not item.streak_count or item.streak_min is None:
                    continue
                if not _within(
                    component,
                    item.start + item.streak_min,
                    item.start + item.streak_max,
                    period,
                ):
                    continue
                streak_bonus += item.streak_bonus
                gate_bonus += item.gate_bonus
                if item.best_streak is not None:
                    summaries.append(item.best_streak)
            high = min(HIGH_AUTHORITY_CAP, base + streak_bonus)
            gate = min(HIGH_AUTHORITY_CAP, base + gate_bonus)
            phase_streak = max(
                summaries,
                key=lambda item: (item.interval_count, item.end_at),
                default=None,
            )
            if phase_streak is not None:
                phase_offset = phase_streak.phase_estimate % period
                phase_spread = phase_streak.phase_spread
            elif ring_rels and not folded:
                phase_offset, phase_spread = _relationship_phase(tuple(ring_rels), period)
            else:
                points = [(_relationship_phase_value(item, period),) * 2 for item in ring_rels] + [
                    (item.start + item.phase_min, item.start + item.phase_max) for item in folded
                ]
                anchor_phase = (points[0][0] + points[0][1]) / 2
                center = _median_of_counts(
                    [
                        (_signed_phase_residual((low + high) / 2, anchor_phase, period), 1)
                        for low, high in points
                    ]
                )
                phase_offset = (anchor_phase + center) % period
                phase_spread = max(
                    _range_abs(low, high, phase_offset, period)[1] for low, high in points
                )
            phase_quality = max(0.0, 1.0 - phase_spread / tolerance)
            count = len(ring_rels) + sum(item.relationship_count for item in folded)
            normal_candidate = normal_score.candidate(period) if normal_score is not None else None
            h2_harmonic, h3_harmonic = compact_harmonic_blockers(
                state,
                period,
                normal_candidate,
                ring_windows,
                ring_events=ring_events,
                phase_offset=phase_offset,
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
                any(
                    item.interval_count >= 3 for item in summaries
                ) or (has_h2_streak and count >= 3)
            )
            h3 = bool(
                h3_structure
                and gate >= H3_AUTHORITY_MINIMUM
                and gate * phase_quality >= H3_PHASE_AUTHORITY_MINIMUM
                and not h3_harmonic
            )
            h4 = bool(h3 and count >= 4 and gate >= H4_AUTHORITY_MINIMUM)
            endpoints = frozenset(
                value for item in ring_rels for value in (item.left_event_id, item.right_event_id)
            )
            excludes_anchor = anchor_id is not None and anchor_id in endpoints
            support = compact_sparse_support(
                state,
                period=period,
                phase_offset=phase_offset,
                ring_events=ring_events,
                excluded_ring_event_ids=endpoints,
                exclude_folded_high_endpoints=bool(folded),
                exclude_anchor=excludes_anchor,
            )
            sparse_reasons = compact_sparse_structure_reasons(
                state,
                period=period,
                support=support,
                normal_candidate=normal_candidate,
                ring_windows=ring_windows,
                ring_events=ring_events,
                phase_offset=phase_offset,
            )
            firsts = [item.left_at for item in ring_rels] + [
                item.first_at for item in folded if item.first_at is not None
            ]
            lasts = [item.right_at for item in ring_rels] + [
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
                    excludes_anchor=excludes_anchor,
                    sparse_reason_codes=sparse_reasons,
                    longest_streak=max((item.interval_count for item in summaries), default=0),
                    ring_relationship_ids=tuple(item.relationship_id for item in ring_rels),
                    ring_relationship_qualities=tuple(
                        (item.relationship_id, round(qualities[item.relationship_id], 6))
                        for item in ring_rels
                    ),
                    streaks=tuple(summaries),
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
    return compact_sparse_confirmation_details(
        state, candidate, ring_events=ring_events, ring_relationships=ring_relationships
    )[0]


def compact_sparse_confirmation_details(
    state: LearnedState,
    candidate: CompactCandidate,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_relationships: Iterable[IntervalRelationship] = (),
) -> tuple[float, bool]:
    """``authority._sparse_confirmed_candidate`` combined value (Stage 8.5).

    Folded high full intervals confirm only from bins wholly within the
    incumbent's phase tolerance (conservative); epoch confirmation scalars
    and ring intervals are exact.
    """

    incumbent = state.incumbent
    if incumbent is None or incumbent.established_at is None:
        return candidate.combined_confidence, False
    period = incumbent.period
    established_at = incumbent.established_at
    ring_events = tuple(ring_events)
    ring_map = _ring_map(state, ring_events)
    anchor_id = state.open_chain.anchor_event_id if state.open_chain is not None else None
    residual = 1.0
    confirming = False
    excluded: set[str] = set(candidate.ring_endpoint_ids)
    folded_high = False
    for item in state.intervals:
        if (
            item.period == period
            and item.full_interval_count
            and item.phase_min is not None
            and _range_abs(
                item.start + item.phase_min,
                item.start + item.phase_max,
                incumbent.phase_offset,
                period,
            )[1]
            <= incumbent.phase_tolerance
        ):
            residual *= item.full_interval_residual
            confirming = folded_high = True
    folded_confirmation = state.epoch.confirmation_count > 0
    if folded_confirmation:
        residual *= state.epoch.confirmation_residual
        confirming = True
    for relationship in _ring_relationships(state, ring_relationships, ring_map):
        if (
            relationship.candidate_period_seconds != period
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
        return candidate.combined_confidence, False
    confirmation = min(HIGH_AUTHORITY_CAP, 1.0 - residual)
    support = compact_sparse_support(
        state,
        period=period,
        phase_offset=candidate.phase_offset,
        ring_events=ring_events,
        excluded_ring_event_ids=excluded,
        exclude_folded_high_endpoints=candidate.has_folded_evidence or folded_high,
        exclude_folded_confirmation_endpoints=folded_confirmation,
        exclude_anchor=anchor_id is not None and anchor_id in excluded,
    )
    continuous = max(candidate.high_authority, confirmation)
    combined = continuous + (1.0 - continuous) * min(
        SPARSE_EVIDENCE_CEILING, SPARSE_EVIDENCE_CEILING * support.value
    )
    return round(max(candidate.combined_confidence, combined), 6), True


def compact_last_aligned_evidence_at(
    state: LearnedState,
    *,
    ring_events: Iterable[PhysicalRestartEvent] = (),
    ring_windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> float | None:
    """``authority._last_aligned_evidence_at`` for the recorded incumbent.

    Folded hits count only from bins wholly within tolerance (a later last
    aligned time could hide suspicion, so the folded side is conservative).
    """

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
        and _circular_distance(
            _event_at(item),
            incumbent.phase_offset,
            period,
        ) <= incumbent.phase_tolerance
    )
    values.extend(
        item.last_hit_at
        for item in state.misses
        if item.period == period
        and item.last_hit_at is not None
        and _range_abs(
            item.index * INTERVAL_BIN_SECONDS + item.phase_min,
            item.index * INTERVAL_BIN_SECONDS + item.phase_max,
            incumbent.phase_offset,
            period,
        )[1]
        <= incumbent.phase_tolerance
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

    Folded bins that may be off-phase are compared with the anomaly at their
    closest possible phase: a bin that might be coherent counts as coherent
    (conservative: the countdown is then withheld as authority would).
    """

    incumbent = state.incumbent
    if incumbent is None or not _authentic_event(anomaly) or _event_at(anomaly) is None:
        return False
    if (
        _circular_distance(_event_at(anomaly), incumbent.phase_offset, incumbent.period)
        <= incumbent.phase_tolerance
    ):
        return False
    anomaly_entry = _ring_entry(anomaly)
    others = [
        _bin_entry(item)
        for item in state.pool
        if _range_abs(
            item.seconds_min,
            item.seconds_max,
            incumbent.phase_offset,
            incumbent.period,
        )[1]
        > incumbent.phase_tolerance
    ]
    mark = _watermark_key(state)
    others.extend(
        _ring_entry(item)
        for item in ring_events
        if item.server_key == state.server_key
        and item.event_id != anomaly.event_id
        and _newer(item, mark)
        and _authentic_event(item)
        and _circular_distance(_event_at(item), incumbent.phase_offset, incumbent.period)
        > incumbent.phase_tolerance
    )

    def coherent(other: _Entry) -> bool:
        return any(
            _range_abs(other.low, other.high, anomaly_entry.low, period)[0]
            <= max(
                candidate_phase_tolerance(period) + anomaly_entry.uncertainty_max,
                candidate_phase_tolerance(period) + other.uncertainty_max,
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
    ring_map = _ring_map(state, ring_events)
    intervals = list(state.epoch.recovery)
    for relationship in _ring_relationships(state, ring_relationships, ring_map):
        if _incumbent_recovery_interval(relationship, incumbent, ring_map):
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


@dataclass(frozen=True)
class CompactAuthorityInputs:
    """The bounded raw evidence the compact authority reads besides the state."""

    ring_events: tuple[PhysicalRestartEvent, ...]
    ring_relationships: tuple[IntervalRelationship, ...]
    ring_windows: tuple[ExpectedWindowLedgerRecord, ...]


def compact_authority_inputs(
    state: LearnedState,
    events: Iterable[PhysicalRestartEvent],
    relationships: Iterable[IntervalRelationship],
    windows: Iterable[ExpectedWindowLedgerRecord] = (),
) -> CompactAuthorityInputs:
    """Ring events (with the anchor), unfolded relationships and windows.

    High (and full-interval) relationships are unfolded until their right
    endpoint folds; normal relationships until their left endpoint folds.
    Expected windows are unfolded until they end at or before the window
    watermark.  Everything here is bounded by the ring.
    """

    ring = plan_ring_folding(state, events).ring
    ring_map = _ring_map(state, ring)
    high = _ring_relationships(state, relationships, ring_map)
    normal = ring_normal_relationships(state, relationships)
    values = {item.relationship_id: item for item in (*high, *normal)}
    mark = state.watermark.window_end_at
    window_values = tuple(
        item for item in windows if mark is None or item.window_end_at > mark
    )
    return CompactAuthorityInputs(
        ring,
        tuple(
            sorted(
                values.values(),
                key=lambda item: (item.left_at, item.right_at, item.relationship_id),
            )
        ),
        window_values,
    )


def knowledge_for_regime(state: LearnedState, regime: RegimeRecord) -> LearnedState:
    """The state with incumbent knowledge describing ``regime``.

    Keeps the stored last aligned evidence when it is the same regime;
    otherwise derives it from bins wholly aligned with the regime.
    """

    previous = state.incumbent
    last_aligned = (
        previous.last_aligned_evidence_at
        if previous is not None and previous.regime_id == regime.regime_id
        else _folded_last_aligned(state, regime)
    )
    return replace(state, incumbent=_knowledge_from_regime(regime, last_aligned=last_aligned))


# --------------------------------------------------------------------------
# Serialisation: positional arrays, strict structural and byte bounds
# --------------------------------------------------------------------------


def _finite(
    value: object, name: str, *, minimum: float | None = 0.0, maximum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LearnedStateError(f"{name} must be a finite number")
    value = float(value)
    if (minimum is not None and value < minimum - _EPSILON) or (
        maximum is not None and value > maximum + _EPSILON
    ):
        raise LearnedStateError(f"{name} out of range")
    return value


def _optional_finite(value: object, name: str, **kwargs) -> float | None:
    return None if value is None else _finite(value, name, **kwargs)


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LearnedStateError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise LearnedStateError(f"{name} out of range")
    return value


def _string(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_STRING_LENGTH:
        raise LearnedStateError(f"{name} must be a non-empty bounded string")
    return value


def _array(
    value: object, name: str, *, length: int | None = None, maximum: int | None = None
) -> list:
    if not isinstance(value, list):
        raise LearnedStateError(f"{name} must be a list")
    if length is not None and len(value) != length:
        raise LearnedStateError(f"{name} must have {length} fields")
    if maximum is not None and len(value) > maximum:
        raise LearnedStateError(f"{name} exceeds its storage bound")
    return value


def _mapping(value: object, name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise LearnedStateError(f"{name} must be a mapping")
    return value


def _period(value: object, name: str) -> int:
    if isinstance(value, bool) or value not in CANDIDATE_PERIODS:
        raise LearnedStateError(f"{name} is not a candidate period")
    return value


def _ordered_unique(keys: list, name: str) -> None:
    if keys != sorted(set(keys)):
        raise LearnedStateError(f"{name} entries must be unique and ordered")


def _streak_to(item: StreakSummary | None) -> list | None:
    if item is None:
        return None
    return [
        item.interval_count,
        item.event_count,
        item.min_quality,
        item.phase_estimate,
        item.phase_spread,
        item.start_at,
        item.end_at,
    ]


def _streak_from(value: object, name: str) -> StreakSummary | None:
    if value is None:
        return None
    data = _array(value, name, length=7)
    return StreakSummary(
        _integer(data[0], f"{name}.interval_count", minimum=1),
        _integer(data[1], f"{name}.event_count", minimum=2),
        _finite(data[2], f"{name}.min_quality", maximum=1.0),
        _finite(data[3], f"{name}.phase_estimate"),
        _finite(data[4], f"{name}.phase_spread"),
        _finite(data[5], f"{name}.start_at"),
        _finite(data[6], f"{name}.end_at"),
    )


def _pool_bin_to(item: PhaseBin) -> list:
    confirmation = item.confirmation
    return [
        item.index,
        item.weight,
        item.count,
        item.offset_min,
        item.offset_max,
        item.first_at,
        item.last_at,
        item.uncertainty_min,
        item.uncertainty_max,
        item.weight_before,
        [
            [
                PERIOD_INDEX[tag.period],
                tag.weight,
                tag.count,
                tag.first_tagged_at,
                tag.last_tagged_at,
                tag.untagged_first_at,
                tag.untagged_weight_before,
                tag.untagged_tagged_before,
            ]
            for tag in item.endpoints
        ],
        None
        if confirmation is None
        else [PERIOD_INDEX[confirmation.period], confirmation.weight, confirmation.count],
        [[PERIOD_INDEX[key], value] for key, value in item.tagged_before],
    ]


def _tag_from(value: object, name: str, count_limit: int, weight_limit: float) -> EndpointTag:
    if not isinstance(value, list) or len(value) not in (3, 8):
        raise LearnedStateError(f"{name} must have 3 or 8 fields")
    data = value
    index = _integer(data[0], f"{name}.period", maximum=len(CANDIDATE_PERIODS) - 1)
    extra = [None] * 5
    if len(data) == 8:
        extra = [
            _optional_finite(data[3], f"{name}.first_tagged_at"),
            _optional_finite(data[4], f"{name}.last_tagged_at"),
            _optional_finite(data[5], f"{name}.untagged_first_at"),
            _optional_finite(data[6], f"{name}.untagged_weight_before"),
            _optional_finite(data[7], f"{name}.untagged_tagged_before"),
        ]
    return EndpointTag(
        CANDIDATE_PERIODS[index],
        _finite(data[1], f"{name}.weight", maximum=weight_limit),
        _integer(data[2], f"{name}.count", minimum=1, maximum=count_limit),
        *extra,
    )


def _period_values_from(value: object, name: str) -> tuple[tuple[int, float], ...]:
    values = []
    for index, item in enumerate(_array(value, name, maximum=len(CANDIDATE_PERIODS))):
        entry = _array(item, f"{name}[{index}]", length=2)
        period = CANDIDATE_PERIODS[
            _integer(entry[0], f"{name}[{index}].period", maximum=len(CANDIDATE_PERIODS) - 1)
        ]
        values.append((period, _finite(entry[1], f"{name}[{index}].value")))
    _ordered_unique([period for period, _value in values], name)
    return tuple(values)


def _pool_bin_from(value: object, name: str) -> PhaseBin:
    data = _array(value, name, length=13)
    count = _integer(data[2], f"{name}.count", minimum=1)
    weight = _finite(data[1], f"{name}.weight")
    offset_min = _finite(data[3], f"{name}.offset_min", maximum=PHASE_BIN_SECONDS)
    offset_max = _finite(data[4], f"{name}.offset_max", maximum=PHASE_BIN_SECONDS)
    first_at = _finite(data[5], f"{name}.first_at")
    last_at = _finite(data[6], f"{name}.last_at")
    uncertainty_min = _finite(data[7], f"{name}.uncertainty_min", maximum=PHASE_UNCERTAINTY_CAP)
    uncertainty_max = _finite(data[8], f"{name}.uncertainty_max", maximum=PHASE_UNCERTAINTY_CAP)
    if offset_max < offset_min or last_at < first_at or uncertainty_max < uncertainty_min:
        raise LearnedStateError(f"{name} extremes are inverted")
    tags = tuple(
        _tag_from(item, f"{name}.endpoints[{index}]", count, weight)
        for index, item in enumerate(
            _array(data[10], f"{name}.endpoints", maximum=len(CANDIDATE_PERIODS))
        )
    )
    _ordered_unique([tag.period for tag in tags], f"{name}.endpoints")
    confirmation = (
        None if data[11] is None else _tag_from(data[11], f"{name}.confirmation", count, weight)
    )
    return PhaseBin(
        index=_integer(data[0], f"{name}.index", maximum=DAY_BINS - 1),
        weight=weight,
        count=count,
        offset_min=offset_min,
        offset_max=offset_max,
        first_at=first_at,
        last_at=last_at,
        uncertainty_min=uncertainty_min,
        uncertainty_max=uncertainty_max,
        weight_before=_finite(data[9], f"{name}.weight_before"),
        endpoints=tags,
        confirmation=confirmation,
        tagged_before=_period_values_from(data[12], f"{name}.tagged_before"),
    )


def _interval_bin_to(item: IntervalBin) -> list:
    return [
        item.period,
        item.index,
        item.relationship_count,
        item.phase_min,
        item.phase_max,
        item.base_residual,
        item.full_interval_count,
        item.full_interval_residual,
        item.first_at,
        item.last_at,
        item.streak_count,
        item.streak_bonus,
        item.gate_bonus,
        item.streak_min,
        item.streak_max,
        _streak_to(item.best_streak),
    ]


def _interval_bins_from(value: object, name: str) -> tuple[IntervalBin, ...]:
    result = []
    for index, raw in enumerate(_array(value, name, maximum=INTERVAL_BIN_TOTAL)):
        label = f"{name}[{index}]"
        data = _array(raw, label, length=16)
        period = _period(data[0], f"{label}.period")
        count = _integer(data[2], f"{label}.relationship_count")
        streaks = _integer(data[10], f"{label}.streak_count")
        phase_min = _optional_finite(data[3], f"{label}.phase_min", maximum=INTERVAL_BIN_SECONDS)
        phase_max = _optional_finite(data[4], f"{label}.phase_max", maximum=INTERVAL_BIN_SECONDS)
        if (count > 0) != (phase_min is not None and phase_max is not None):
            raise LearnedStateError(f"{label} relationship phase extremes inconsistent")
        if phase_min is not None and phase_max < phase_min:
            raise LearnedStateError(f"{label} relationship phase extremes inverted")
        bound = 4 * MAXIMUM_TOLERANCE + INTERVAL_BIN_SECONDS
        streak_min = _optional_finite(
            data[13],
            f"{label}.streak_min",
            minimum=-bound,
            maximum=bound,
        )
        streak_max = _optional_finite(
            data[14],
            f"{label}.streak_max",
            minimum=-bound,
            maximum=bound,
        )
        if (streaks > 0) != (streak_min is not None and streak_max is not None):
            raise LearnedStateError(f"{label} streak phase extremes inconsistent")
        result.append(
            IntervalBin(
                period=period,
                index=_integer(
                    data[1],
                    f"{label}.index",
                    maximum=period // INTERVAL_BIN_SECONDS - 1,
                ),
                relationship_count=count,
                phase_min=phase_min,
                phase_max=phase_max,
                base_residual=_finite(data[5], f"{label}.base_residual", maximum=1.0),
                full_interval_count=_integer(
                    data[6],
                    f"{label}.full_interval_count",
                    maximum=count,
                ),
                full_interval_residual=_finite(
                    data[7],
                    f"{label}.full_interval_residual",
                    maximum=1.0,
                ),
                first_at=_optional_finite(data[8], f"{label}.first_at"),
                last_at=_optional_finite(data[9], f"{label}.last_at"),
                streak_count=streaks,
                streak_bonus=_finite(data[11], f"{label}.streak_bonus"),
                gate_bonus=_finite(data[12], f"{label}.gate_bonus"),
                streak_min=streak_min,
                streak_max=streak_max,
                best_streak=_streak_from(data[15], f"{label}.best_streak"),
            )
        )
    _ordered_unique([(item.period, item.index) for item in result], name)
    return tuple(result)


def _open_chain_to(item: OpenChain | None) -> dict | None:
    if item is None:
        return None
    run = item.run
    return {
        "chain_id": item.chain_id,
        "anchor_event_id": item.anchor_event_id,
        "anchor": [
            item.anchor_at,
            item.anchor_index,
            item.anchor_weight,
            [PERIOD_INDEX[period] for period in item.anchor_high_periods],
            item.anchor_confirmation_tagged,
        ],
        "run": None
        if run is None
        else [
            run.period,
            run.interval_count,
            run.min_quality,
            run.start_at,
            run.end_at,
            run.first_phase,
            run.relationship_phase_min,
            run.relationship_phase_max,
            [
                [value.key, value.count, value.deviation_min, value.deviation_max]
                for value in run.residuals
            ],
        ],
    }


def _open_chain_from(value: object) -> OpenChain | None:
    if value is None:
        return None
    data = _mapping(value, "open_chain")
    anchor = _array(data.get("anchor"), "open_chain.anchor", length=5)
    periods = tuple(
        CANDIDATE_PERIODS[
            _integer(item, "open_chain.anchor.periods[]", maximum=len(CANDIDATE_PERIODS) - 1)
        ]
        for item in _array(anchor[3], "open_chain.anchor.periods", maximum=len(CANDIDATE_PERIODS))
    )
    _ordered_unique(list(periods), "open_chain.anchor.periods")
    if not isinstance(anchor[4], bool):
        raise LearnedStateError("open_chain.anchor.confirmation must be a boolean")
    run = None
    if data.get("run") is not None:
        raw = _array(data.get("run"), "open_chain.run", length=9)
        period = _period(raw[0], "open_chain.run.period")
        half = RUN_RESIDUAL_QUANTUM_SECONDS / 2
        residuals = []
        for index, item in enumerate(
            _array(raw[8], "open_chain.run.residuals", maximum=RUN_RESIDUAL_KEY_LIMIT)
        ):
            entry = _array(item, f"open_chain.run.residuals[{index}]", length=4)
            low = _finite(entry[2], "residual.deviation_min", minimum=-half, maximum=half)
            high = _finite(entry[3], "residual.deviation_max", minimum=-half, maximum=half)
            if high < low:
                raise LearnedStateError("residual deviation extremes are inverted")
            residuals.append(
                RunResidual(
                    _integer(
                        entry[0],
                        "residual.key",
                        minimum=-RUN_RESIDUAL_KEY_RADIUS,
                        maximum=RUN_RESIDUAL_KEY_RADIUS,
                    ),
                    _integer(entry[1], "residual.count", minimum=1),
                    low,
                    high,
                )
            )
        _ordered_unique([item.key for item in residuals], "open_chain.run.residuals")
        if not residuals:
            raise LearnedStateError("open run needs residuals")
        bound = 4 * MAXIMUM_TOLERANCE
        run = OpenRun(
            period=period,
            interval_count=_integer(raw[1], "open_chain.run.interval_count", minimum=1),
            min_quality=_finite(raw[2], "open_chain.run.min_quality", maximum=1.0),
            start_at=_finite(raw[3], "open_chain.run.start_at"),
            end_at=_finite(raw[4], "open_chain.run.end_at"),
            first_phase=_finite(raw[5], "open_chain.run.first_phase", maximum=period),
            relationship_phase_min=_finite(
                raw[6],
                "open_chain.run.phase_min",
                minimum=-bound,
                maximum=bound,
            ),
            relationship_phase_max=_finite(
                raw[7],
                "open_chain.run.phase_max",
                minimum=-bound,
                maximum=bound,
            ),
            residuals=tuple(residuals),
        )
    index = anchor[1]
    return OpenChain(
        chain_id=_string(data.get("chain_id"), "open_chain.chain_id"),
        anchor_event_id=_string(data.get("anchor_event_id"), "open_chain.anchor_event_id"),
        anchor_at=_finite(anchor[0], "open_chain.anchor.at"),
        anchor_index=None
        if index is None
        else _integer(index, "open_chain.anchor.index", maximum=DAY_BINS - 1),
        anchor_weight=_finite(anchor[2], "open_chain.anchor.weight", maximum=1.0),
        anchor_high_periods=periods,
        anchor_confirmation_tagged=anchor[4],
        run=run,
    )


def learned_state_to_mapping(state: LearnedState) -> dict:
    mark = state.watermark
    epoch = state.epoch
    incumbent = state.incumbent
    return {
        "schema_version": state.schema_version,
        "server_key": state.server_key,
        "watermark": [
            mark.at,
            mark.event_id,
            mark.folded_event_count,
            mark.folded_learning_event_count,
            mark.window_end_at,
        ],
        "pool_weight_total": state.pool_weight_total,
        "tagged_weight_totals": [
            [PERIOD_INDEX[key], value] for key, value in state.tagged_weight_totals
        ],
        "pool": [_pool_bin_to(item) for item in state.pool],
        "intervals": [_interval_bin_to(item) for item in state.intervals],
        "misses": [
            [
                item.period,
                item.index,
                item.phase_min,
                item.phase_max,
                item.genuine_miss_count,
                item.last_genuine_miss_at,
                item.last_hit_at,
            ]
            for item in state.misses
        ],
        "divisors": [
            [
                item.period,
                item.divisor,
                item.covered_window_count,
                item.longer_interval_count,
                item.phase_offset,
                item.observed_at,
            ]
            for item in state.divisors
        ],
        "divisor_floor_at": state.divisor_floor_at,
        "normal_relationships": [
            [
                item.period,
                item.index,
                item.count,
                item.phase_min,
                item.phase_max,
                item.first_at,
                item.last_at,
            ]
            for item in state.normal_relationships
        ],
        "incumbent": None
        if incumbent is None
        else {
            "regime_id": incumbent.regime_id,
            "values": [
                incumbent.period,
                incumbent.phase_offset,
                incumbent.phase_tolerance,
                incumbent.high_authority,
                incumbent.phase_authority,
                incumbent.combined_confidence,
                incumbent.established_at,
                incumbent.last_aligned_evidence_at,
            ],
            "labels": [incumbent.origin, incumbent.authority_gate, incumbent.status],
            "defining_evidence_ids": list(incumbent.defining_evidence_ids),
            "reason_codes": list(incumbent.reason_codes),
        },
        "superseded": [
            [item.period, item.phase_offset, item.phase_tolerance, item.superseded_at]
            for item in state.superseded
        ],
        "epoch": {
            "regime_id": epoch.regime_id,
            "values": [epoch.established_at, epoch.confirmation_count, epoch.confirmation_residual],
            "challenger": [_interval_bin_to(item) for item in epoch.challenger],
            "recovery": [[item.left_at, item.right_at] for item in epoch.recovery],
        },
        "open_chain": _open_chain_to(state.open_chain),
    }


def learned_state_from_mapping(value: object) -> LearnedState:
    data = _mapping(value, "learned_state")
    if data.get("schema_version") != LEARNED_STATE_SCHEMA_VERSION:
        raise LearnedStateError("unsupported learned-state schema version")
    mark = _array(data.get("watermark"), "watermark", length=5)
    watermark = FoldWatermark(
        at=_optional_finite(mark[0], "watermark.at"),
        event_id=_string(mark[1], "watermark.event_id", optional=True),
        folded_event_count=_integer(mark[2], "watermark.folded_event_count"),
        folded_learning_event_count=_integer(mark[3], "watermark.folded_learning_event_count"),
        window_end_at=_optional_finite(mark[4], "watermark.window_end_at"),
    )
    if (watermark.at is None) != (watermark.event_id is None):
        raise LearnedStateError("watermark time and event identity must be set together")
    pool = tuple(
        _pool_bin_from(item, f"pool[{index}]")
        for index, item in enumerate(_array(data.get("pool"), "pool", maximum=DAY_BINS))
    )
    _ordered_unique([item.index for item in pool], "pool")
    total = _finite(data.get("pool_weight_total"), "pool_weight_total")
    if any(item.weight_before + item.weight > total + 1e-6 for item in pool):
        raise LearnedStateError("pool bin domain accumulator exceeds the pool total")

    misses = []
    for index, raw in enumerate(_array(data.get("misses"), "misses", maximum=INTERVAL_BIN_TOTAL)):
        label = f"misses[{index}]"
        entry = _array(raw, label, length=7)
        period = _period(entry[0], f"{label}.period")
        low = _finite(entry[2], f"{label}.phase_min", maximum=INTERVAL_BIN_SECONDS)
        high = _finite(entry[3], f"{label}.phase_max", maximum=INTERVAL_BIN_SECONDS)
        if high < low:
            raise LearnedStateError(f"{label} phase extremes are inverted")
        misses.append(
            MissBin(
                period,
                _integer(entry[1], f"{label}.index", maximum=period // INTERVAL_BIN_SECONDS - 1),
                low,
                high,
                _integer(entry[4], f"{label}.genuine_miss_count"),
                _optional_finite(entry[5], f"{label}.last_genuine_miss_at"),
                _optional_finite(entry[6], f"{label}.last_hit_at"),
            )
        )
    _ordered_unique([(item.period, item.index) for item in misses], "misses")

    divisors = []
    for index, raw in enumerate(
        _array(data.get("divisors"), "divisors", maximum=len(DIVISOR_KEYS)),
    ):
        entry = _array(raw, f"divisors[{index}]", length=6)
        if (entry[0], entry[1]) not in DIVISOR_KEYS:
            raise LearnedStateError(f"divisors[{index}] is not a (period, divisor) pair")
        divisors.append(
            DivisorEvidence(
                entry[0],
                entry[1],
                _integer(entry[2], "divisor.covered_window_count"),
                _integer(entry[3], "divisor.longer_interval_count"),
                _finite(entry[4], "divisor.phase_offset", maximum=entry[0]),
                _finite(entry[5], "divisor.observed_at"),
            )
        )
    _ordered_unique([(item.period, item.divisor) for item in divisors], "divisors")

    normal_bins = []
    for index, raw in enumerate(
        _array(data.get("normal_relationships"), "normal_relationships", maximum=INTERVAL_BIN_TOTAL)
    ):
        label = f"normal_relationships[{index}]"
        entry = _array(raw, label, length=7)
        period = _period(entry[0], f"{label}.period")
        low = _finite(entry[3], f"{label}.phase_min", maximum=INTERVAL_BIN_SECONDS)
        high = _finite(entry[4], f"{label}.phase_max", maximum=INTERVAL_BIN_SECONDS)
        first_at = _finite(entry[5], f"{label}.first_at")
        last_at = _finite(entry[6], f"{label}.last_at")
        if high < low or last_at < first_at:
            raise LearnedStateError(f"{label} extremes are inverted")
        normal_bins.append(
            NormalBin(
                period,
                _integer(entry[1], f"{label}.index", maximum=period // INTERVAL_BIN_SECONDS - 1),
                _integer(entry[2], f"{label}.count", minimum=1),
                low,
                high,
                first_at,
                last_at,
            )
        )
    _ordered_unique([(item.period, item.index) for item in normal_bins], "normal_relationships")

    incumbent = None
    if data.get("incumbent") is not None:
        entry = _mapping(data.get("incumbent"), "incumbent")
        values = _array(entry.get("values"), "incumbent.values", length=8)
        labels = _array(entry.get("labels"), "incumbent.labels", length=3)
        period = _period(values[0], "incumbent.period")
        incumbent = RegimeKnowledge(
            regime_id=_string(entry.get("regime_id"), "incumbent.regime_id"),
            period=period,
            phase_offset=_finite(values[1], "incumbent.phase_offset", maximum=period),
            phase_tolerance=_finite(values[2], "incumbent.phase_tolerance"),
            origin=_string(labels[0], "incumbent.origin"),
            authority_gate=_string(labels[1], "incumbent.authority_gate"),
            status=_string(labels[2], "incumbent.status"),
            high_authority=_finite(values[3], "incumbent.high_authority", maximum=1.0),
            phase_authority=_finite(values[4], "incumbent.phase_authority", maximum=1.0),
            combined_confidence=_finite(values[5], "incumbent.combined_confidence", maximum=1.0),
            established_at=_optional_finite(values[6], "incumbent.established_at"),
            last_aligned_evidence_at=_optional_finite(
                values[7],
                "incumbent.last_aligned_evidence_at",
            ),
            defining_evidence_ids=tuple(
                _string(item, "incumbent.defining_evidence_ids[]")
                for item in _array(
                    entry.get("defining_evidence_ids"),
                    "incumbent.defining_evidence_ids",
                    maximum=MAX_DEFINING_EVIDENCE_IDS,
                )
            ),
            reason_codes=tuple(
                _string(item, "incumbent.reason_codes[]")
                for item in _array(
                    entry.get("reason_codes"), "incumbent.reason_codes", maximum=MAX_REASON_CODES
                )
            ),
        )

    superseded = []
    for index, raw in enumerate(
        _array(data.get("superseded"), "superseded", maximum=INTERVAL_BIN_TOTAL)
    ):
        entry = _array(raw, f"superseded[{index}]", length=4)
        period = _period(entry[0], f"superseded[{index}].period")
        superseded.append(
            SupersededRegime(
                period,
                _finite(entry[1], "superseded.phase_offset", maximum=period),
                _finite(entry[2], "superseded.phase_tolerance", maximum=period / 2),
                _finite(entry[3], "superseded.superseded_at"),
            )
        )
    _ordered_unique([item.key for item in superseded], "superseded")

    epoch_data = _mapping(data.get("epoch"), "epoch")
    epoch_values = _array(epoch_data.get("values"), "epoch.values", length=3)
    recovery = []
    for index, raw in enumerate(
        _array(
            epoch_data.get("recovery"),
            "epoch.recovery",
            maximum=ORIGINAL_CYCLE_RECOVERY_CONFIRMATIONS,
        )
    ):
        entry = _array(raw, f"epoch.recovery[{index}]", length=2)
        left = _finite(entry[0], "recovery.left_at")
        right = _finite(entry[1], "recovery.right_at")
        if right <= left:
            raise LearnedStateError("recovery interval must have positive length")
        recovery.append(RecoveryInterval(left, right))
    epoch = EpochEvidence(
        regime_id=_string(epoch_data.get("regime_id"), "epoch.regime_id", optional=True),
        established_at=_optional_finite(epoch_values[0], "epoch.established_at"),
        challenger=_interval_bins_from(epoch_data.get("challenger"), "epoch.challenger"),
        confirmation_count=_integer(epoch_values[1], "epoch.confirmation_count"),
        confirmation_residual=_finite(epoch_values[2], "epoch.confirmation_residual", maximum=1.0),
        recovery=tuple(recovery),
    )
    state = LearnedState(
        schema_version=LEARNED_STATE_SCHEMA_VERSION,
        server_key=_string(data.get("server_key"), "server_key"),
        watermark=watermark,
        pool_weight_total=total,
        pool=pool,
        intervals=_interval_bins_from(data.get("intervals"), "intervals"),
        misses=tuple(misses),
        divisors=tuple(divisors),
        incumbent=incumbent,
        superseded=tuple(superseded),
        epoch=epoch,
        open_chain=_open_chain_from(data.get("open_chain")),
        normal_relationships=tuple(normal_bins),
        divisor_floor_at=_optional_finite(data.get("divisor_floor_at"), "divisor_floor_at"),
        tagged_weight_totals=_period_values_from(
            data.get("tagged_weight_totals"), "tagged_weight_totals"
        ),
    )
    if learned_state_entry_count(state) > LEARNED_STATE_MAX_ENTRIES:
        raise LearnedStateError("learned state exceeds its entry bound")
    return state


def learned_state_json_bytes(state: LearnedState) -> bytes:
    """Compact canonical JSON; refuses a state exceeding the byte bound.

    The byte bound is implied by the structural bounds, so a valid state can
    never reach it: knowledge is never dropped to satisfy it.
    """

    payload = json.dumps(
        learned_state_to_mapping(state),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > LEARNED_STATE_MAX_JSON_BYTES:
        raise LearnedStateError("learned state exceeds its serialised-size bound")
    return payload


def learned_state_from_json_bytes(payload: bytes) -> LearnedState:
    if len(payload) > LEARNED_STATE_MAX_JSON_BYTES:
        raise LearnedStateError("learned state exceeds its serialised-size bound")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearnedStateError(f"invalid learned-state JSON: {exc}") from exc
    return learned_state_from_mapping(value)
