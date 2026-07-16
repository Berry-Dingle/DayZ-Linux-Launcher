from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable

from .companion_restart_phase2_detection import (
    EventOutcome,
    ObservationSample,
    PhysicalRestartEvent,
)


HOUR = 3600
DAY = 24 * HOUR
CANDIDATE_PERIODS = (2 * HOUR, 3 * HOUR, 4 * HOUR, 6 * HOUR, 8 * HOUR, 12 * HOUR)
EXACT_DIVISORS = {
    2 * HOUR: (),
    3 * HOUR: (),
    4 * HOUR: (2 * HOUR,),
    6 * HOUR: (2 * HOUR, 3 * HOUR),
    8 * HOUR: (2 * HOUR, 4 * HOUR),
    12 * HOUR: (2 * HOUR, 3 * HOUR, 4 * HOUR, 6 * HOUR),
}
PHASE_UNCERTAINTY_CAP = 5 * 60
# Sixteen events are the smallest simple horizon that can contain the reviewed
# seven-day 12h high-confidence boundary (15 events / 14 adjacent intervals).
RECENT_EVENT_LIMIT = 16
SCORING_SEMANTICS_VERSION = 1
AGGREGATE_SEMANTICS_VERSION = 1


class CoverageKind(str, Enum):
    ONLINE_HEALTHY = "online_healthy"
    OFFLINE_OBSERVED = "offline_observed"
    PARTIAL = "partially_covered"
    UNMONITORED = "unmonitored"
    QUERY_HEALTH_GAP = "query_health_gap"
    APP_STOPPED = "app_stopped"
    PAUSED = "paused"
    SERVER_SWITCH = "server_switch"
    SLEEP_GAP = "sleep_heartbeat_gap"
    MISSING_PLAYERS = "missing_player_field"
    MISSING_INFO = "missing_info"
    UNRESOLVED_EPISODE = "unresolved_episode"


class PatternStatus(str, Enum):
    NO_PATTERN = "no_pattern"
    PATTERN_OBSERVED = "pattern_observed_period_unknown"
    LIKELY_PERIOD = "likely_period"
    CONFIRMED_PERIOD = "confirmed_period"
    PHASE_UNCERTAIN = "phase_uncertain"
    SCHEDULE_CHANGE_SUSPECTED = "schedule_change_suspected"
    PERIOD_UNCERTAIN = "period_uncertain_between_candidates"


class RegimeStatus(str, Enum):
    STABLE = "stable"
    CHANGE_SUSPECTED = "change_suspected"
    PERIOD_UNCERTAIN = "period_uncertain"
    NEW_REGIME_ESTABLISHING = "new_regime_establishing"
    PHASE_UNCERTAIN = "phase_uncertain"


class EvidenceKind(str, Enum):
    DIRECT = "covered_direct"
    EXPECTED_MISS = "covered_expected_miss"
    OFF_GRID = "off_grid"
    PHASE_HINT = "phase_hint"


@dataclass(frozen=True)
class CoverageSegment:
    start_at: float
    end_at: float
    kind: CoverageKind
    cadence: float = 10.0

    def __post_init__(self) -> None:
        _finite_nonnegative("start_at", self.start_at)
        _finite_nonnegative("end_at", self.end_at)
        _finite_positive("cadence", self.cadence)
        if self.end_at <= self.start_at:
            raise ValueError("coverage segment must have positive duration")
        if not isinstance(self.kind, CoverageKind):
            raise ValueError("kind must be a CoverageKind")


@dataclass(frozen=True)
class CoverageAssessment:
    start_at: float
    end_at: float
    fully_covered: bool
    observed_ratio: float
    largest_gap: float
    edge_gap: float
    blocking_kinds: tuple[CoverageKind, ...]
    unresolved_episode: bool
    query_health_adequate: bool


@dataclass(frozen=True)
class CoverageTimeline:
    segments: tuple[CoverageSegment, ...] = ()
    online_cadence: float = 10.0
    offline_cadence: float = 3.0
    minimum_observed_ratio: float = 0.90

    def __post_init__(self) -> None:
        _finite_positive("online_cadence", self.online_cadence)
        _finite_positive("offline_cadence", self.offline_cadence)
        if not 0 < self.minimum_observed_ratio <= 1:
            raise ValueError("minimum_observed_ratio must be within (0, 1]")
        if any(not isinstance(item, CoverageSegment) for item in self.segments):
            raise ValueError("segments must contain CoverageSegment values")
        object.__setattr__(self, "segments", tuple(sorted(self.segments, key=lambda item: (item.start_at, item.end_at))))

    @property
    def online_maximum_gap(self) -> float:
        return max(30.0, 3.0 * self.online_cadence)

    @property
    def offline_maximum_gap(self) -> float:
        return max(12.0, 4.0 * self.offline_cadence)

    def assess(self, start_at: float, end_at: float) -> CoverageAssessment:
        _finite_nonnegative("start_at", start_at)
        _finite_nonnegative("end_at", end_at)
        if end_at <= start_at:
            raise ValueError("coverage interval must have positive duration")

        blockers: set[CoverageKind] = set()
        acceptable: list[tuple[float, float, CoverageKind]] = []
        for segment in self.segments:
            start = max(start_at, segment.start_at)
            end = min(end_at, segment.end_at)
            if end <= start:
                continue
            if segment.kind in {CoverageKind.ONLINE_HEALTHY, CoverageKind.OFFLINE_OBSERVED}:
                acceptable.append((start, end, segment.kind))
            else:
                blockers.add(segment.kind)

        merged: list[list[object]] = []
        for start, end, kind in acceptable:
            if merged and start <= float(merged[-1][1]):
                merged[-1][1] = max(float(merged[-1][1]), end)
                if kind is CoverageKind.ONLINE_HEALTHY:
                    merged[-1][2] = kind
            else:
                merged.append([start, end, kind])

        observed = sum(float(item[1]) - float(item[0]) for item in merged)
        ratio = min(1.0, observed / (end_at - start_at))
        gaps: list[tuple[float, bool]] = []
        cursor = start_at
        previous_kind = CoverageKind.ONLINE_HEALTHY
        for start, end, kind in merged:
            start_value = float(start)
            if start_value > cursor:
                offline = previous_kind is CoverageKind.OFFLINE_OBSERVED or kind is CoverageKind.OFFLINE_OBSERVED
                gaps.append((start_value - cursor, offline))
            cursor = max(cursor, float(end))
            previous_kind = kind
        if cursor < end_at:
            gaps.append((end_at - cursor, previous_kind is CoverageKind.OFFLINE_OBSERVED))

        largest_gap = max((value for value, _offline in gaps), default=0.0)
        edge_gap = 0.0
        if merged:
            edge_gap = max(float(merged[0][0]) - start_at, end_at - float(merged[-1][1]))
        else:
            edge_gap = end_at - start_at
        gaps_ok = all(
            value <= (self.offline_maximum_gap if offline else self.online_maximum_gap)
            for value, offline in gaps
        )
        unresolved = CoverageKind.UNRESOLVED_EPISODE in blockers
        query_adequate = not blockers.intersection(
            {
                CoverageKind.QUERY_HEALTH_GAP,
                CoverageKind.MISSING_PLAYERS,
                CoverageKind.MISSING_INFO,
            }
        )
        fully = ratio >= self.minimum_observed_ratio and gaps_ok and not blockers and bool(merged)
        return CoverageAssessment(
            start_at=start_at,
            end_at=end_at,
            fully_covered=fully,
            observed_ratio=round(ratio, 6),
            largest_gap=largest_gap,
            edge_gap=edge_gap,
            blocking_kinds=tuple(sorted(blockers, key=lambda item: item.value)),
            unresolved_episode=unresolved,
            query_health_adequate=query_adequate,
        )

    def covers(self, start_at: float, end_at: float) -> bool:
        assessment = self.assess(start_at, end_at)
        return assessment.fully_covered and assessment.query_health_adequate


@dataclass(frozen=True)
class HintDiagnostics:
    event_count: int = 0
    weighted_alignment: float = 0.0
    first_at: float | None = None
    last_at: float | None = None
    residual_mad: float | None = None
    calendar_day_count: int = 0
    schedule_existence_contribution: float = 0.0


@dataclass(frozen=True)
class DivisorResolution:
    divisor_seconds: int
    covered_window_count: int
    longer_interval_count: int
    established_resolved: bool
    high_resolved: bool


@dataclass(frozen=True)
class CandidateScore:
    period_seconds: int
    direct_support: float
    strict_direct_interval_count: int
    weak_direct_interval_count: int
    qualifying_event_count: int
    covered_miss_penalty: float
    covered_miss_count: int
    off_grid_penalty: float
    off_grid_event_count: int
    net_covered_evidence: float
    long_term_support_used: float
    hint_bonus: float
    period_confidence_before_caps: float
    fundamental_period_confidence: float
    phase_confidence: float
    phase_offset: float | None
    phase_residual_mad: float | None
    aligned_event_count: int
    observation_span: float
    hints: HintDiagnostics
    divisor_resolution: tuple[DivisorResolution, ...]
    establishment_gates_passed: bool
    high_gates_passed: bool
    confidence_cap: float
    strong_covered_contradiction: bool
    recent_phase_observation_at: float | None


@dataclass(frozen=True)
class RegimeSummary:
    period_seconds: int
    ended_at: float
    final_confidence: float
    direct_interval_count: int
    reason: str


@dataclass(frozen=True)
class ScheduleScore:
    candidates: tuple[CandidateScore, ...]
    schedule_existence_confidence: float
    selected_period_seconds: int | None
    incumbent_period_seconds: int | None
    pattern_status: PatternStatus
    regime_status: RegimeStatus
    prediction_usable: bool
    five_minute_warning_eligible: bool
    scheduled_classification_eligible: bool
    unresolved_competitors: tuple[int, ...]
    recent_event_count: int
    prior_regimes: tuple[RegimeSummary, ...] = ()

    def candidate(self, period_seconds: int) -> CandidateScore:
        for candidate in self.candidates:
            if candidate.period_seconds == period_seconds:
                return candidate
        raise KeyError(period_seconds)


@dataclass(frozen=True)
class GateRequirements:
    established_intervals: int
    established_events: int
    established_span: float
    high_intervals: int
    high_events: int
    high_span: float
    require_divisors: bool = False


GATES = {
    2 * HOUR: GateRequirements(5, 6, 10 * HOUR, 8, 9, DAY),
    3 * HOUR: GateRequirements(4, 5, 12 * HOUR, 7, 8, DAY),
    4 * HOUR: GateRequirements(4, 5, 16 * HOUR, 7, 8, 32 * HOUR),
    6 * HOUR: GateRequirements(4, 5, 24 * HOUR, 7, 8, 48 * HOUR, True),
    8 * HOUR: GateRequirements(4, 5, 32 * HOUR, 7, 8, 64 * HOUR, True),
    12 * HOUR: GateRequirements(6, 7, 72 * HOUR, 9, 10, 7 * DAY, True),
}


@dataclass
class _CandidateWork:
    period: int
    direct_support: float = 0.0
    strict_pairs: list[tuple[str, str, float, float]] = field(default_factory=list)
    weak_pairs: list[tuple[str, str, float, float]] = field(default_factory=list)
    qualifying_ids: set[str] = field(default_factory=set)
    misses: list[tuple[float, float, str]] = field(default_factory=list)
    resolution_windows: dict[int, list[tuple[float, int]]] = field(default_factory=dict)
    hint_ids: set[str] = field(default_factory=set)
    hint_weights: dict[str, float] = field(default_factory=dict)
    hint_times: dict[str, float] = field(default_factory=dict)
    offgrid_ids: set[str] = field(default_factory=set)
    offgrid_penalty: float = 0.0


@dataclass(frozen=True)
class CandidateAggregate:
    period_seconds: int
    direct_count: int = 0
    direct_weight: float = 0.0
    miss_count: int = 0
    miss_penalty: float = 0.0
    off_grid_count: int = 0
    off_grid_penalty: float = 0.0
    hint_count: int = 0
    hint_weight: float = 0.0
    first_hint_at: float | None = None
    last_hint_at: float | None = None
    phase_vector_sin: float = 0.0
    phase_vector_cos: float = 0.0
    divisor_resolution_count: int = 0


@dataclass(frozen=True)
class LongTermAggregate:
    semantics_version: int = AGGREGATE_SEMANTICS_VERSION
    candidates: tuple[CandidateAggregate, ...] = tuple(
        CandidateAggregate(period) for period in CANDIDATE_PERIODS
    )
    source_quality_counts: tuple[tuple[str, int], ...] = ()
    first_observation_at: float | None = None
    last_observation_at: float | None = None
    anomaly_count: int = 0
    prior_regimes: tuple[RegimeSummary, ...] = ()

    def candidate(self, period_seconds: int) -> CandidateAggregate:
        for item in self.candidates:
            if item.period_seconds == period_seconds:
                return item
        return CandidateAggregate(period_seconds)


@dataclass(frozen=True)
class EvidencePrimitive:
    kind: EvidenceKind
    period_seconds: int
    weight: float
    observed_at: float
    phase_residual: float = 0.0
    divisor_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("kind must be EvidenceKind")
        if self.period_seconds not in CANDIDATE_PERIODS:
            raise ValueError("unsupported candidate period")
        _finite_nonnegative("weight", self.weight)
        _finite_nonnegative("observed_at", self.observed_at)
        if not math.isfinite(self.phase_residual):
            raise ValueError("phase_residual must be finite")


@dataclass(frozen=True)
class DetailedEvidenceRecord:
    event_seq: int
    event_id: str
    observed_at: float
    outcome: EventOutcome
    authenticity: float
    samples: tuple[ObservationSample, ...] = ()
    primitives: tuple[EvidencePrimitive, ...] = ()

    def __post_init__(self) -> None:
        if type(self.event_seq) is not int or self.event_seq <= 0:
            raise ValueError("event_seq must be a positive integer")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be non-empty")
        _finite_nonnegative("observed_at", self.observed_at)
        if not isinstance(self.outcome, EventOutcome):
            raise ValueError("outcome must be EventOutcome")
        if not math.isfinite(self.authenticity) or not 0 <= self.authenticity <= 1:
            raise ValueError("authenticity must be within [0, 1]")
        if any(not isinstance(item, EvidencePrimitive) for item in self.primitives):
            raise ValueError("primitives must be EvidencePrimitive values")
        if any(not isinstance(item, ObservationSample) for item in self.samples):
            raise ValueError("samples must be ObservationSample values")


@dataclass(frozen=True)
class EvidenceLedger:
    scoring_semantics_version: int = SCORING_SEMANTICS_VERSION
    aggregate_semantics_version: int = AGGREGATE_SEMANTICS_VERSION
    event_seq: int = 0
    folded_through_event_seq: int = 0
    records: tuple[DetailedEvidenceRecord, ...] = ()
    aggregate: LongTermAggregate = field(default_factory=LongTermAggregate)

    def __post_init__(self) -> None:
        for name in ("event_seq", "folded_through_event_seq"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.folded_through_event_seq > self.event_seq and self.event_seq != 0:
            raise ValueError("folded high-water mark cannot exceed event_seq")


@dataclass(frozen=True)
class PhaseResidualRecord:
    period_seconds: int
    observed_at: float
    residual: float

    def __post_init__(self) -> None:
        if self.period_seconds not in CANDIDATE_PERIODS:
            raise ValueError("unsupported candidate period")
        _finite_nonnegative("observed_at", self.observed_at)
        if not math.isfinite(self.residual):
            raise ValueError("residual must be finite")


@dataclass(frozen=True)
class DebugRecord:
    observed_at: float
    code: str

    def __post_init__(self) -> None:
        _finite_nonnegative("observed_at", self.observed_at)
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("debug code must be non-empty")


@dataclass(frozen=True)
class BoundedScoringHistory:
    coverage_segments: tuple[CoverageSegment, ...] = ()
    rejected_summaries: tuple[DetailedEvidenceRecord, ...] = ()
    expected_miss_details: tuple[EvidencePrimitive, ...] = ()
    phase_residuals: tuple[PhaseResidualRecord, ...] = ()
    debug_records: tuple[DebugRecord, ...] = ()


@dataclass(frozen=True)
class LedgerLimits:
    detailed_events: int = 80
    events_with_samples: int = 12
    samples_per_event: int = 48

    def __post_init__(self) -> None:
        for name in ("detailed_events", "events_with_samples", "samples_per_event"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.events_with_samples > self.detailed_events:
            raise ValueError("sample event limit must not exceed detailed event limit")


class RestartScheduleScorer:
    """Pure Phase 2 scorer; it has no runtime, persistence, or UI dependencies."""

    def score(
        self,
        events: Iterable[PhysicalRestartEvent],
        coverage: CoverageTimeline,
        *,
        now: float,
        previous: ScheduleScore | None = None,
        incumbent_period_seconds: int | None = None,
        aggregate: LongTermAggregate | None = None,
    ) -> ScheduleScore:
        _finite_nonnegative("now", now)
        unique = _unique_events(events)
        recent = _recent_events(unique)
        work = {period: _CandidateWork(period) for period in CANDIDATE_PERIODS}
        self._collect_interval_evidence(recent, coverage, work)
        self._collect_offgrid_evidence(recent, work)
        candidate_scores = tuple(
            self._finish_candidate(period, recent, work[period], now, aggregate)
            for period in CANDIDATE_PERIODS
        )
        schedule_confidence = _schedule_existence_confidence(recent, candidate_scores)
        incumbent = incumbent_period_seconds
        if incumbent is None and previous is not None:
            incumbent = previous.selected_period_seconds
        selection = _select_candidate(candidate_scores, schedule_confidence, incumbent, previous, now)
        return ScheduleScore(
            candidates=candidate_scores,
            schedule_existence_confidence=schedule_confidence,
            selected_period_seconds=selection[0],
            incumbent_period_seconds=incumbent,
            pattern_status=selection[1],
            regime_status=selection[2],
            prediction_usable=selection[3],
            five_minute_warning_eligible=selection[4],
            scheduled_classification_eligible=selection[5],
            unresolved_competitors=selection[6],
            recent_event_count=len(recent),
            prior_regimes=selection[7],
        )

    def _collect_interval_evidence(
        self,
        events: tuple[PhysicalRestartEvent, ...],
        coverage: CoverageTimeline,
        work: dict[int, _CandidateWork],
    ) -> None:
        endpoints = tuple(event for event in events if _event_weight(event) > 0)
        for left, right in zip(endpoints, endpoints[1:]):
            left_at = _event_at(left)
            right_at = _event_at(right)
            if left_at is None or right_at is None or right_at <= left_at:
                continue
            gap = right_at - left_at
            assessment = coverage.assess(left_at, right_at)
            unresolved_inside = any(
                _blocks_interval(event)
                and left_at < (_event_at(event) or left_at) < right_at
                for event in events
            )
            fully_covered = (
                assessment.fully_covered
                and assessment.query_health_adequate
                and not unresolved_inside
            )
            pair_weight = min(_event_weight(left), _event_weight(right))
            for period in CANDIDATE_PERIODS:
                tolerance = _pair_tolerance(period, left, right)
                direct_match = abs(gap - period) <= tolerance
                multiple = max(1, round(gap / period))
                multiple_match = abs(gap - multiple * period) <= tolerance
                candidate = work[period]
                if direct_match and fully_covered:
                    if pair_weight > 0:
                        candidate.direct_support += pair_weight
                        pair = (left.event_id, right.event_id, left_at, right_at)
                        if _strict_event(left) and _strict_event(right):
                            candidate.strict_pairs.append(pair)
                        else:
                            candidate.weak_pairs.append(pair)
                        candidate.qualifying_ids.update((left.event_id, right.event_id))
                    continue
                if multiple_match and not fully_covered:
                    self._add_hint(candidate, left)
                    self._add_hint(candidate, right)

            if not fully_covered:
                continue
            if not (_strict_event(left) and _strict_event(right)):
                continue
            for longer in CANDIDATE_PERIODS:
                tolerance = _pair_tolerance(longer, left, right)
                if abs(gap - longer) > tolerance:
                    continue
                interval_key = hash((left.event_id, right.event_id, longer))
                for divisor in EXACT_DIVISORS[longer]:
                    windows = range(1, longer // divisor)
                    for index in windows:
                        expected = left_at + index * divisor
                        if expected >= right_at - tolerance:
                            continue
                        window_tolerance = candidate_phase_tolerance(divisor)
                        window = coverage.assess(
                            max(left_at, expected - window_tolerance),
                            min(right_at, expected + window_tolerance),
                        )
                        if window.fully_covered and window.query_health_adequate and not window.unresolved_episode:
                            work[divisor].misses.append((expected, 1.0, f"{left.event_id}:{right.event_id}"))
                            work[longer].resolution_windows.setdefault(divisor, []).append(
                                (expected, interval_key)
                            )
        self._collect_weak_hint_evidence(events, coverage, work)

    def _collect_weak_hint_evidence(
        self,
        events: tuple[PhysicalRestartEvent, ...],
        coverage: CoverageTimeline,
        work: dict[int, _CandidateWork],
    ) -> None:
        hint_events = tuple(event for event in events if _hint_event_weight(event) > 0)
        for left, right in zip(hint_events, hint_events[1:]):
            if _event_weight(left) > 0 and _event_weight(right) > 0:
                continue
            left_at = _event_at(left)
            right_at = _event_at(right)
            if left_at is None or right_at is None or right_at <= left_at:
                continue
            if coverage.covers(left_at, right_at):
                continue
            gap = right_at - left_at
            for period in CANDIDATE_PERIODS:
                multiple = max(1, round(gap / period))
                if abs(gap - multiple * period) <= _pair_tolerance(period, left, right):
                    self._add_hint(work[period], left)
                    self._add_hint(work[period], right)

    @staticmethod
    def _add_hint(candidate: _CandidateWork, event: PhysicalRestartEvent) -> None:
        at = _event_at(event)
        if at is None:
            return
        weight = _hint_event_weight(event)
        if weight <= 0:
            return
        candidate.hint_ids.add(event.event_id)
        candidate.hint_weights[event.event_id] = max(
            candidate.hint_weights.get(event.event_id, 0.0), min(0.15 * weight, 0.15)
        )
        candidate.hint_times[event.event_id] = at

    def _collect_offgrid_evidence(
        self,
        events: tuple[PhysicalRestartEvent, ...],
        work: dict[int, _CandidateWork],
    ) -> None:
        for period, candidate in work.items():
            if len(candidate.strict_pairs) < 2:
                continue
            participants = {item for pair in candidate.strict_pairs for item in pair[:2]}
            evidence_started_at = min(pair[2] for pair in candidate.strict_pairs)
            phase_events = [event for event in events if event.event_id in participants]
            phase = _phase_cluster(phase_events, period)
            if phase[0] is None:
                continue
            offset = phase[0]
            for event in events:
                at = _event_at(event)
                if at is None or at < evidence_started_at or event.event_id in participants:
                    continue
                residual = abs(_signed_phase_residual(at, offset, period))
                if residual <= _event_tolerance(period, event):
                    continue
                weight = _event_weight(event)
                if weight <= 0:
                    continue
                candidate.offgrid_ids.add(event.event_id)
                if event.outcome is EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART:
                    candidate.offgrid_penalty += min(0.35, weight * 0.70)
                elif _strict_event(event):
                    candidate.offgrid_penalty += 0.75 * weight

    def _finish_candidate(
        self,
        period: int,
        events: tuple[PhysicalRestartEvent, ...],
        work: _CandidateWork,
        now: float,
        aggregate: LongTermAggregate | None,
    ) -> CandidateScore:
        strict_count = len(work.strict_pairs)
        weak_count = len(work.weak_pairs)
        hint = _hint_diagnostics(work, period)
        evidence_started_at = min((pair[2] for pair in work.strict_pairs), default=-math.inf)
        recent_misses = [item for item in work.misses if item[0] >= evidence_started_at]
        miss_penalty = sum(item[1] for item in recent_misses)
        direct_support = round(work.direct_support, 6)
        old = aggregate.candidate(period) if aggregate is not None else CandidateAggregate(period)
        recent_net = max(0.0, direct_support - miss_penalty - work.offgrid_penalty)
        long_term_used = 0.0
        if miss_penalty == 0 and work.offgrid_penalty == 0:
            long_term_used = min(old.direct_weight * 0.30, direct_support * 0.30)
        net = recent_net + long_term_used
        base_confidence = confidence_from_net_evidence(net)
        hint_bonus = 0.0
        if (
            hint.event_count >= 3
            and hint.calendar_day_count >= 2
            and hint.residual_mad is not None
            and hint.residual_mad <= candidate_phase_tolerance(period)
            and not recent_misses
            and not work.offgrid_ids
        ):
            if 2 <= strict_count <= 3:
                hint_bonus = min(0.03, hint.weighted_alignment)
            elif strict_count >= 4:
                hint_bonus = min(0.05, hint.weighted_alignment)
        before_caps = min(1.0, base_confidence + hint_bonus)

        strict_ids = {item for pair in work.strict_pairs for item in pair[:2]}
        qualifying_count = len(strict_ids)
        strict_times = [value for pair in work.strict_pairs for value in pair[2:]]
        span = max(strict_times) - min(strict_times) if strict_times else 0.0
        resolutions = _divisor_resolutions(period, work)
        requirements = GATES[period]
        divisors_established = all(item.established_resolved for item in resolutions)
        divisors_high = all(item.high_resolved for item in resolutions)
        gate_passed = (
            strict_count >= requirements.established_intervals
            and qualifying_count >= requirements.established_events
            and span >= requirements.established_span
            and (not requirements.require_divisors or divisors_established)
        )
        high_passed = (
            strict_count >= requirements.high_intervals
            and qualifying_count >= requirements.high_events
            and span >= requirements.high_span
            and (not requirements.require_divisors or divisors_high)
        )
        cap = 1.0
        if strict_count == 0:
            cap = min(cap, 0.49 if weak_count else 0.0)
        elif strict_count < requirements.established_intervals:
            cap = min(cap, 0.79)
        if qualifying_count < requirements.established_events or span < requirements.established_span:
            cap = min(cap, 0.79)
        if requirements.require_divisors and not divisors_established:
            cap = min(cap, 0.59)
        if miss_penalty + work.offgrid_penalty >= 2.0:
            cap = min(cap, 0.59)

        phase_events = [event for event in events if _event_weight(event) > 0]
        phase_offset, phase_mad, aligned_count, recent_phase = _phase_cluster(phase_events, period)
        phase_confidence = _phase_confidence(
            phase_mad,
            aligned_count,
            len(phase_events),
            recent_phase,
            now,
            period,
        )
        if recent_phase is None or now - recent_phase > 3 * period:
            cap = min(cap, 0.79)
        final_confidence = min(before_caps, cap)
        return CandidateScore(
            period_seconds=period,
            direct_support=direct_support,
            strict_direct_interval_count=strict_count,
            weak_direct_interval_count=weak_count,
            qualifying_event_count=qualifying_count,
            covered_miss_penalty=round(miss_penalty, 6),
            covered_miss_count=len(recent_misses),
            off_grid_penalty=round(work.offgrid_penalty, 6),
            off_grid_event_count=len(work.offgrid_ids),
            net_covered_evidence=round(net, 6),
            long_term_support_used=round(long_term_used, 6),
            hint_bonus=round(hint_bonus, 6),
            period_confidence_before_caps=round(before_caps, 6),
            fundamental_period_confidence=round(max(0.0, min(1.0, final_confidence)), 6),
            phase_confidence=round(phase_confidence, 6),
            phase_offset=phase_offset,
            phase_residual_mad=phase_mad,
            aligned_event_count=aligned_count,
            observation_span=span,
            hints=hint,
            divisor_resolution=resolutions,
            establishment_gates_passed=gate_passed,
            high_gates_passed=high_passed,
            confidence_cap=cap,
            strong_covered_contradiction=miss_penalty + work.offgrid_penalty >= 1.0,
            recent_phase_observation_at=recent_phase,
        )


def candidate_phase_tolerance(period_seconds: int) -> float:
    if period_seconds not in CANDIDATE_PERIODS:
        raise ValueError("unsupported candidate period")
    return max(5 * 60.0, min(15 * 60.0, 0.05 * period_seconds))


def confidence_from_net_evidence(net: float) -> float:
    _finite_nonnegative("net", net)
    points = ((0, 0.0), (1, 0.25), (2, 0.45), (3, 0.65), (4, 0.78), (5, 0.85), (7, 0.92), (10, 0.96))
    if net >= points[-1][0]:
        return points[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if left_x <= net <= right_x:
            fraction = (net - left_x) / (right_x - left_x)
            return left_y + fraction * (right_y - left_y)
    return 0.0


def fold_evidence_ledger(
    ledger: EvidenceLedger,
    new_records: Iterable[DetailedEvidenceRecord] = (),
    *,
    limits: LedgerLimits | None = None,
) -> EvidenceLedger:
    if not isinstance(ledger, EvidenceLedger):
        raise ValueError("ledger must be EvidenceLedger")
    limits = limits or LedgerLimits()
    if ledger.scoring_semantics_version != SCORING_SEMANTICS_VERSION:
        raise ValueError("unsupported scoring semantics version")
    if ledger.aggregate_semantics_version != AGGREGATE_SEMANTICS_VERSION:
        raise ValueError("unsupported aggregate semantics version")
    by_seq: dict[int, DetailedEvidenceRecord] = {}
    for record in ledger.records:
        if record.event_seq <= ledger.folded_through_event_seq:
            continue
        if record.event_seq in by_seq and by_seq[record.event_seq] != record:
            raise ValueError("conflicting event sequence")
        by_seq[record.event_seq] = record
    max_seq = max(ledger.event_seq, ledger.folded_through_event_seq)
    for record in new_records:
        if not isinstance(record, DetailedEvidenceRecord):
            raise ValueError("new records must be DetailedEvidenceRecord values")
        if record.event_seq <= ledger.folded_through_event_seq:
            continue
        existing = by_seq.get(record.event_seq)
        if existing is not None and existing != record:
            raise ValueError("conflicting event sequence")
        by_seq[record.event_seq] = record
        max_seq = max(max_seq, record.event_seq)
    ordered = sorted(by_seq.values(), key=lambda item: item.event_seq)
    aggregate = ledger.aggregate
    fold_count = max(0, len(ordered) - limits.detailed_events)
    to_fold = ordered[:fold_count]
    retained = ordered[fold_count:]
    folded_through = ledger.folded_through_event_seq
    for record in to_fold:
        if record.event_seq <= folded_through:
            continue
        aggregate = _fold_record(aggregate, record)
        folded_through = record.event_seq

    sample_start = max(0, len(retained) - limits.events_with_samples)
    normalized_records: list[DetailedEvidenceRecord] = []
    for index, record in enumerate(retained):
        samples = record.samples[-limits.samples_per_event :] if index >= sample_start else ()
        normalized_records.append(replace(record, samples=tuple(samples)))
    return EvidenceLedger(
        scoring_semantics_version=SCORING_SEMANTICS_VERSION,
        aggregate_semantics_version=AGGREGATE_SEMANTICS_VERSION,
        event_seq=max_seq,
        folded_through_event_seq=folded_through,
        records=tuple(normalized_records),
        aggregate=aggregate,
    )


def bound_scoring_history(
    history: BoundedScoringHistory,
    *,
    now: float,
    debug_enabled: bool = False,
) -> BoundedScoringHistory:
    if not isinstance(history, BoundedScoringHistory):
        raise ValueError("history must be BoundedScoringHistory")
    _finite_nonnegative("now", now)
    coverage = tuple(
        sorted(
            (item for item in history.coverage_segments if item.end_at >= now - 60 * DAY),
            key=lambda item: (item.start_at, item.end_at),
        )[-120:]
    )
    rejected = tuple(
        sorted(
            (item for item in history.rejected_summaries if _rejected_outcome(item.outcome)),
            key=lambda item: item.event_seq,
        )[-30:]
    )
    misses = tuple(
        sorted(
            (item for item in history.expected_miss_details if item.kind is EvidenceKind.EXPECTED_MISS),
            key=lambda item: item.observed_at,
        )[-60:]
    )
    residuals: list[PhaseResidualRecord] = []
    for period in CANDIDATE_PERIODS:
        values = sorted(
            (item for item in history.phase_residuals if item.period_seconds == period),
            key=lambda item: item.observed_at,
        )[-32:]
        residuals.extend(values)
    debug = tuple(sorted(history.debug_records, key=lambda item: item.observed_at)[-40:]) if debug_enabled else ()
    return BoundedScoringHistory(
        coverage_segments=coverage,
        rejected_summaries=rejected,
        expected_miss_details=misses,
        phase_residuals=tuple(residuals),
        debug_records=debug,
    )


def _fold_record(aggregate: LongTermAggregate, record: DetailedEvidenceRecord) -> LongTermAggregate:
    candidates = {item.period_seconds: item for item in aggregate.candidates}
    anomaly_count = aggregate.anomaly_count
    for primitive in record.primitives:
        current = candidates[primitive.period_seconds]
        values = current.__dict__.copy()
        if primitive.kind is EvidenceKind.DIRECT:
            values["direct_count"] += 1
            values["direct_weight"] += primitive.weight
        elif primitive.kind is EvidenceKind.EXPECTED_MISS:
            values["miss_count"] += 1
            values["miss_penalty"] += primitive.weight
            anomaly_count += 1
        elif primitive.kind is EvidenceKind.OFF_GRID:
            values["off_grid_count"] += 1
            values["off_grid_penalty"] += primitive.weight
            anomaly_count += 1
        elif primitive.kind is EvidenceKind.PHASE_HINT:
            values["hint_count"] += 1
            values["hint_weight"] += primitive.weight
            angle = 2 * math.pi * (primitive.phase_residual / primitive.period_seconds)
            values["phase_vector_sin"] += math.sin(angle) * primitive.weight
            values["phase_vector_cos"] += math.cos(angle) * primitive.weight
            values["first_hint_at"] = (
                primitive.observed_at
                if values["first_hint_at"] is None
                else min(values["first_hint_at"], primitive.observed_at)
            )
            values["last_hint_at"] = (
                primitive.observed_at
                if values["last_hint_at"] is None
                else max(values["last_hint_at"], primitive.observed_at)
            )
        if primitive.divisor_seconds is not None:
            values["divisor_resolution_count"] += 1
        candidates[primitive.period_seconds] = CandidateAggregate(**values)
    counts = dict(aggregate.source_quality_counts)
    counts[record.outcome.value] = counts.get(record.outcome.value, 0) + 1
    first = record.observed_at if aggregate.first_observation_at is None else min(aggregate.first_observation_at, record.observed_at)
    last = record.observed_at if aggregate.last_observation_at is None else max(aggregate.last_observation_at, record.observed_at)
    return LongTermAggregate(
        semantics_version=AGGREGATE_SEMANTICS_VERSION,
        candidates=tuple(candidates[period] for period in CANDIDATE_PERIODS),
        source_quality_counts=tuple(sorted(counts.items())),
        first_observation_at=first,
        last_observation_at=last,
        anomaly_count=anomaly_count,
        prior_regimes=aggregate.prior_regimes,
    )


def _event_at(event: PhysicalRestartEvent) -> float | None:
    value = event.canonical_phase_at
    if value is None or not math.isfinite(value) or value < 0:
        return None
    return value


def _event_weight(event: PhysicalRestartEvent) -> float:
    if not event.coverage_complete:
        return 0.0
    weights = {
        EventOutcome.CORROBORATED_OFFLINE_RESTART: 1.0,
        EventOutcome.CONFIRMED_OFFLINE_RESTART: 0.8,
        EventOutcome.STRONG_QUERY_VISIBLE_RESTART: 0.8,
        EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART: 0.5,
    }
    return weights.get(event.outcome, 0.0)


def _hint_event_weight(event: PhysicalRestartEvent) -> float:
    direct = _event_weight(event)
    if direct > 0:
        return direct
    if event.outcome is EventOutcome.AMBIGUOUS_DRAIN:
        return min(0.15, event.authenticity * 0.5)
    if event.outcome is EventOutcome.UNCERTAIN_A2S_INTERRUPTION:
        return min(0.05, event.authenticity * 0.5)
    return 0.0


def _strict_event(event: PhysicalRestartEvent) -> bool:
    return event.outcome in {
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
    }


def _unique_events(events: Iterable[PhysicalRestartEvent]) -> tuple[PhysicalRestartEvent, ...]:
    by_id: dict[str, PhysicalRestartEvent] = {}
    for event in events:
        if not isinstance(event, PhysicalRestartEvent):
            raise ValueError("events must be PhysicalRestartEvent values")
        at = _event_at(event)
        if at is None:
            continue
        by_id.setdefault(event.event_id, event)
    return tuple(sorted(by_id.values(), key=lambda item: (_event_at(item), item.event_id)))


def _recent_events(events: tuple[PhysicalRestartEvent, ...]) -> tuple[PhysicalRestartEvent, ...]:
    qualifying = [event for event in events if _event_weight(event) > 0]
    if not qualifying:
        return events[-12:]
    latest = max(_event_at(event) for event in qualifying)
    qualifying_within_days = [
        event for event in qualifying if _event_at(event) >= latest - 7 * DAY
    ][-RECENT_EVENT_LIMIT:]
    cutoff = _event_at(qualifying_within_days[0]) if qualifying_within_days else latest
    return tuple(event for event in events if _event_at(event) >= cutoff)


def _blocks_interval(event: PhysicalRestartEvent) -> bool:
    return event.outcome in {
        EventOutcome.AMBIGUOUS_DRAIN,
        EventOutcome.INCOMPLETE,
        EventOutcome.EXPIRED,
    }


def _rejected_outcome(outcome: EventOutcome) -> bool:
    return outcome in {
        EventOutcome.AMBIGUOUS_DRAIN,
        EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
        EventOutcome.INCOMPLETE,
        EventOutcome.EXPIRED,
        EventOutcome.SERVICE_INTERRUPTION,
    }


def _pair_tolerance(period: int, left: PhysicalRestartEvent, right: PhysicalRestartEvent) -> float:
    uncertainty = min(
        PHASE_UNCERTAINTY_CAP,
        max(0.0, left.phase_uncertainty, right.phase_uncertainty),
    )
    return candidate_phase_tolerance(period) + uncertainty


def _event_tolerance(period: int, event: PhysicalRestartEvent) -> float:
    return candidate_phase_tolerance(period) + min(PHASE_UNCERTAINTY_CAP, max(0.0, event.phase_uncertainty))


def _signed_phase_residual(at: float, offset: float, period: int) -> float:
    return ((at - offset + period / 2) % period) - period / 2


def _phase_cluster(
    events: Iterable[PhysicalRestartEvent], period: int
) -> tuple[float | None, float | None, int, float | None]:
    values = [(event, _event_at(event)) for event in events]
    values = [(event, at) for event, at in values if at is not None]
    if not values:
        return None, None, 0, None
    best: tuple[float, list[tuple[PhysicalRestartEvent, float]], float] | None = None
    for _event, anchor in values:
        aligned = [
            (candidate, at)
            for candidate, at in values
            if abs(_signed_phase_residual(at, anchor, period)) <= _event_tolerance(period, candidate)
        ]
        weight = sum(_event_weight(item) for item, _at in aligned)
        candidate_key = (weight, len(aligned), max(at for _item, at in aligned))
        if best is None or candidate_key > (best[2], len(best[1]), max(at for _item, at in best[1])):
            best = (anchor % period, aligned, weight)
    assert best is not None
    offset, aligned, _weight = best
    residuals = [
        max(
            abs(_signed_phase_residual(at, offset, period)),
            min(PHASE_UNCERTAINTY_CAP, max(0.0, event.phase_uncertainty)),
        )
        for event, at in aligned
    ]
    mad = statistics.median(residuals) if residuals else None
    recent = max((at for _event, at in aligned), default=None)
    return offset, mad, len(aligned), recent


def _phase_confidence(
    mad: float | None,
    aligned_count: int,
    total_count: int,
    recent_at: float | None,
    now: float,
    period: int,
) -> float:
    if mad is None or aligned_count == 0:
        return 0.0
    if mad <= 3 * 60:
        quality = 1.0
    elif mad <= 7 * 60:
        quality = 0.9
    elif mad <= 12 * 60:
        quality = 0.75
    elif mad <= 20 * 60:
        quality = 0.5
    else:
        quality = 0.0
    cap = _event_count_cap(aligned_count)
    consistency = aligned_count / max(1, total_count)
    confidence = cap * quality * consistency
    if recent_at is None or now - recent_at > 3 * period:
        confidence = min(confidence, 0.60)
    return min(1.0, confidence)


def _hint_diagnostics(work: _CandidateWork, period: int) -> HintDiagnostics:
    hint_ids = work.hint_ids - work.qualifying_ids
    if not hint_ids:
        return HintDiagnostics()
    times = sorted(work.hint_times[event_id] for event_id in hint_ids)
    phase_values = [time % period for time in times]
    anchor = phase_values[0]
    residuals = [abs(_signed_phase_residual(value, anchor, period)) for value in phase_values]
    mad = statistics.median(residuals)
    days = len({int(value // DAY) for value in times})
    weight = sum(work.hint_weights[event_id] for event_id in hint_ids)
    contribution = min(0.35, 0.05 * len(times) + 0.10 * min(1.0, weight))
    return HintDiagnostics(
        event_count=len(times),
        weighted_alignment=round(weight, 6),
        first_at=times[0],
        last_at=times[-1],
        residual_mad=mad,
        calendar_day_count=days,
        schedule_existence_contribution=contribution,
    )


def _divisor_resolutions(period: int, work: _CandidateWork) -> tuple[DivisorResolution, ...]:
    values = []
    for divisor in EXACT_DIVISORS[period]:
        windows = work.resolution_windows.get(divisor, [])
        intervals = len({item[1] for item in windows})
        values.append(
            DivisorResolution(
                divisor_seconds=divisor,
                covered_window_count=len(windows),
                longer_interval_count=intervals,
                established_resolved=len(windows) >= 2 and intervals >= 2,
                high_resolved=len(windows) >= 4 and intervals >= 3,
            )
        )
    return tuple(values)


def _event_count_cap(count: int) -> float:
    points = ((0, 0.0), (1, 0.20), (2, 0.40), (3, 0.60), (4, 0.75), (5, 0.85), (8, 0.95))
    if count >= 8:
        return 0.95
    for (left_count, left), (right_count, right) in zip(points, points[1:]):
        if left_count <= count <= right_count:
            fraction = (count - left_count) / (right_count - left_count)
            return left + fraction * (right - left)
    return 0.0


def _schedule_existence_confidence(
    events: tuple[PhysicalRestartEvent, ...], candidates: tuple[CandidateScore, ...]
) -> float:
    authentic = [event for event in events if _event_weight(event) >= 0.5]
    if not authentic:
        return 0.0
    best = max(candidates, key=lambda item: (item.aligned_event_count, item.hints.event_count))
    alignment = max(best.aligned_event_count, best.hints.event_count) / len(authentic)
    confidence = _event_count_cap(len(authentic)) * min(1.0, alignment)
    if max(item.strict_direct_interval_count for item in candidates) == 0:
        confidence = min(confidence + best.hints.schedule_existence_contribution, 0.75)
    return round(min(1.0, confidence), 6)


def _select_candidate(
    candidates: tuple[CandidateScore, ...],
    schedule_confidence: float,
    incumbent_period: int | None,
    previous: ScheduleScore | None,
    now: float,
) -> tuple[
    int | None,
    PatternStatus,
    RegimeStatus,
    bool,
    bool,
    bool,
    tuple[int, ...],
    tuple[RegimeSummary, ...],
]:
    ranked = sorted(candidates, key=lambda item: (item.fundamental_period_confidence, item.direct_support), reverse=True)
    top = ranked[0]
    competitors = tuple(
        item.period_seconds
        for item in ranked[1:]
        if item.fundamental_period_confidence >= 0.65
        and abs(item.fundamental_period_confidence - top.fundamental_period_confidence) <= 0.05
    )
    unresolved = bool(competitors)
    prior_regimes = previous.prior_regimes if previous is not None else ()
    selected = top.period_seconds if top.fundamental_period_confidence >= 0.65 else None
    regime = RegimeStatus.STABLE

    incumbent = next((item for item in candidates if item.period_seconds == incumbent_period), None)
    challenger = next((item for item in ranked if item.period_seconds != incumbent_period), top)
    change_evidence = False
    if incumbent is not None:
        change_evidence = (
            (incumbent.covered_miss_count >= 2 or incumbent.off_grid_event_count >= 2)
            and challenger.strict_direct_interval_count >= 2
        )
        selected = incumbent.period_seconds
        if change_evidence:
            regime = RegimeStatus.CHANGE_SUSPECTED
        switch = (
            challenger.establishment_gates_passed
            and (
                (
                    challenger.fundamental_period_confidence >= 0.80
                    and challenger.fundamental_period_confidence
                    >= incumbent.fundamental_period_confidence + 0.10
                )
                or (
                    incumbent.fundamental_period_confidence < 0.60
                    and challenger.fundamental_period_confidence
                    >= incumbent.fundamental_period_confidence + 0.05
                )
            )
        )
        if switch:
            selected = challenger.period_seconds
            regime = RegimeStatus.NEW_REGIME_ESTABLISHING
            prior_regimes = (*prior_regimes, RegimeSummary(
                period_seconds=incumbent.period_seconds,
                ended_at=now,
                final_confidence=incumbent.fundamental_period_confidence,
                direct_interval_count=incumbent.strict_direct_interval_count,
                reason="sustained_covered_competing_schedule",
            ))
    if unresolved:
        regime = RegimeStatus.PERIOD_UNCERTAIN
    selected_candidate = next((item for item in candidates if item.period_seconds == selected), None)
    if (
        selected_candidate is not None
        and selected_candidate.fundamental_period_confidence >= 0.65
        and selected_candidate.phase_confidence < 0.80
        and regime is RegimeStatus.STABLE
    ):
        regime = RegimeStatus.PHASE_UNCERTAIN

    if schedule_confidence < 0.60:
        pattern = PatternStatus.NO_PATTERN
    elif selected_candidate is None or selected_candidate.fundamental_period_confidence < 0.65:
        pattern = PatternStatus.PATTERN_OBSERVED
    elif regime is RegimeStatus.CHANGE_SUSPECTED:
        pattern = PatternStatus.SCHEDULE_CHANGE_SUSPECTED
    elif regime is RegimeStatus.PERIOD_UNCERTAIN:
        pattern = PatternStatus.PERIOD_UNCERTAIN
    elif selected_candidate.phase_confidence < 0.80:
        pattern = PatternStatus.PHASE_UNCERTAIN
    elif (
        schedule_confidence >= 0.80
        and selected_candidate.fundamental_period_confidence >= 0.80
        and selected_candidate.establishment_gates_passed
    ):
        pattern = PatternStatus.CONFIRMED_PERIOD
    else:
        pattern = PatternStatus.LIKELY_PERIOD

    transition = regime in {
        RegimeStatus.CHANGE_SUSPECTED,
        RegimeStatus.PERIOD_UNCERTAIN,
        RegimeStatus.NEW_REGIME_ESTABLISHING,
        RegimeStatus.PHASE_UNCERTAIN,
    }
    prediction = bool(
        selected_candidate
        and not transition
        and schedule_confidence >= 0.80
        and selected_candidate.fundamental_period_confidence >= 0.80
        and selected_candidate.phase_confidence >= 0.80
    )
    warning = bool(
        prediction
        and schedule_confidence >= 0.85
        and selected_candidate.fundamental_period_confidence >= 0.85
        and selected_candidate.phase_confidence >= 0.85
        and selected_candidate.recent_phase_observation_at is not None
    )
    scheduled = prediction
    return selected, pattern, regime, prediction, warning, scheduled, competitors, prior_regimes


def _finite_nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _finite_positive(name: str, value: object) -> None:
    _finite_nonnegative(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
