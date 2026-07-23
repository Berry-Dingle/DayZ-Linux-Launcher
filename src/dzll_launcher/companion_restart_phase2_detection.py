from __future__ import annotations

import math
import statistics
import uuid
from dataclasses import dataclass, field
from enum import Enum


PENDING_STRIKE_EXPIRY_POLL_MULTIPLIER = 2.5


def pending_strike_lifetime_seconds(online_poll_interval: float) -> float:
    """Return the shared cadence-aware lifetime for an unconfirmed first strike."""
    _require_finite_positive("online_poll_interval", online_poll_interval)
    return PENDING_STRIKE_EXPIRY_POLL_MULTIPLIER * online_poll_interval


def pending_strike_is_recent(
    first_monotonic_at: float | None,
    observed_monotonic_at: float,
    online_poll_interval: float,
) -> bool:
    """Whether a later observation may still use an earlier first strike."""
    if first_monotonic_at is None:
        return False
    if not math.isfinite(first_monotonic_at) or not math.isfinite(observed_monotonic_at):
        return False
    elapsed = observed_monotonic_at - first_monotonic_at
    return 0.0 <= elapsed <= pending_strike_lifetime_seconds(online_poll_interval)


class InfoStatus(str, Enum):
    HEALTHY = "healthy"
    NEUTRAL = "neutral_alive"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    ERROR = "protocol_error"
    MISSING = "unavailable"


class FieldStatus(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"


class LifecycleMarker(str, Enum):
    NORMAL = "normal"
    PAUSE = "pause"
    RESUME = "resume"
    SHUTDOWN = "shutdown"
    SERVER_SWITCH = "server_switch"
    CLEAR = "clear"
    SLEEP_GAP = "sleep_heartbeat_gap"
    APP_RESTART = "app_restart_boundary"


class EpisodeState(str, Enum):
    IDLE = "idle"
    OBSERVING = "observing"
    OFFLINE = "offline"
    RECOVERING = "recovering"


class EventOutcome(str, Enum):
    CORROBORATED_OFFLINE_RESTART = "corroborated_offline_restart"
    CONFIRMED_OFFLINE_RESTART = "confirmed_offline_restart"
    STRONG_QUERY_VISIBLE_RESTART = "strong_query_visible_restart"
    PROBABLE_QUERY_VISIBLE_RESTART = "probable_query_visible_restart"
    AMBIGUOUS_DRAIN = "ambiguous_drain"
    UNCERTAIN_A2S_INTERRUPTION = "uncertain_a2s_interruption"
    INCOMPLETE = "incomplete"
    EXPIRED = "expired"
    SERVICE_INTERRUPTION = "service_interruption_non_restart"


class SignalSource(str, Enum):
    PLAYER_DRAIN = "player_drain"
    VISIBLE_LOW = "query_visible_low"
    INFO_OUTAGE = "info_outage"
    INFO_RETURN = "info_return"
    QUEUE_RECOVERY = "queue_recovery"
    PLAYER_RECOVERY = "player_recovery"


@dataclass(frozen=True)
class ObservationSample:
    wall_at: float
    monotonic_at: float
    app_session_id: str
    monitoring_session_id: str
    poll_generation: int
    server_key: str
    info_status: InfoStatus
    info_latency: float | None = None
    player_status: FieldStatus = FieldStatus.MISSING
    players: int | None = None
    max_players: int | None = None
    queue_status: FieldStatus = FieldStatus.MISSING
    queue: int | None = None
    lifecycle: LifecycleMarker = LifecycleMarker.NORMAL
    continuity_chain_id: str | None = None
    provenance_version: int = 0

    def __post_init__(self) -> None:
        _require_finite_nonnegative("wall_at", self.wall_at)
        _require_finite_nonnegative("monotonic_at", self.monotonic_at)
        for name in ("app_session_id", "monitoring_session_id", "server_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if type(self.poll_generation) is not int or self.poll_generation < 0:
            raise ValueError("poll_generation must be a non-negative integer")
        if not isinstance(self.info_status, InfoStatus):
            raise ValueError("info_status must be an InfoStatus")
        if not isinstance(self.player_status, FieldStatus):
            raise ValueError("player_status must be a FieldStatus")
        if not isinstance(self.queue_status, FieldStatus):
            raise ValueError("queue_status must be a FieldStatus")
        if not isinstance(self.lifecycle, LifecycleMarker):
            raise ValueError("lifecycle must be a LifecycleMarker")
        if self.continuity_chain_id is not None and (
            not isinstance(self.continuity_chain_id, str)
            or not self.continuity_chain_id
        ):
            raise ValueError("continuity_chain_id must be a non-empty string or None")
        if type(self.provenance_version) is not int or self.provenance_version < 0:
            raise ValueError("provenance_version must be a non-negative integer")
        if self.info_latency is not None:
            _require_finite_nonnegative("info_latency", self.info_latency)
        _validate_field("players", self.player_status, self.players)
        _validate_optional_count("max_players", self.max_players)
        _validate_field("queue", self.queue_status, self.queue)


@dataclass(frozen=True)
class DetectionConfig:
    online_poll_interval: float = 10.0
    offline_poll_interval: float = 3.0
    abrupt_drain_minimum: float = 25.0
    low_state_minimum: float = 45.0
    drain_to_outage_merge: float = 300.0
    visible_recovery_strong_maximum: float = 480.0
    visible_recovery_absolute_maximum: float = 600.0
    recovery_spacing_minimum: float = 10.0
    recovery_spacing_maximum: float = 35.0
    queue_to_player_maximum: float = 120.0
    finalization_grace: float = 30.0
    finalization_maximum: float = 60.0
    stable_normal_reset: float = 60.0
    pre_roll_seconds: float = 600.0
    pre_roll_sample_cap: int = 60
    event_pre_roll_sample_cap: int = 36
    active_sample_cap: int = 96
    drain_baseline_minimum: int = 4
    drain_baseline_sample_minimum: int = 2
    drain_absolute_drop_minimum: int = 3
    drain_relative_drop_minimum: float = 0.70
    drain_low_sample_minimum: int = 2
    gradual_drain_maximum: float = 300.0
    drain_recovery_fraction: float = 0.80

    def __post_init__(self) -> None:
        numeric = (
            "online_poll_interval",
            "offline_poll_interval",
            "abrupt_drain_minimum",
            "low_state_minimum",
            "drain_to_outage_merge",
            "visible_recovery_strong_maximum",
            "visible_recovery_absolute_maximum",
            "recovery_spacing_minimum",
            "recovery_spacing_maximum",
            "queue_to_player_maximum",
            "finalization_grace",
            "finalization_maximum",
            "stable_normal_reset",
            "pre_roll_seconds",
            "drain_relative_drop_minimum",
            "gradual_drain_maximum",
            "drain_recovery_fraction",
        )
        for name in numeric:
            value = getattr(self, name)
            _require_finite_positive(name, value)
        for name in ("pre_roll_sample_cap", "event_pre_roll_sample_cap", "active_sample_cap"):
            value = getattr(self, name)
            if type(value) is not int or value < 2:
                raise ValueError(f"{name} must be an integer of at least two")
        for name in (
            "drain_baseline_minimum",
            "drain_baseline_sample_minimum",
            "drain_absolute_drop_minimum",
            "drain_low_sample_minimum",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("drain_relative_drop_minimum", "drain_recovery_fraction"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be greater than zero and at most one")
        if self.visible_recovery_strong_maximum > self.visible_recovery_absolute_maximum:
            raise ValueError("strong recovery maximum must not exceed absolute maximum")
        if self.recovery_spacing_minimum > self.recovery_spacing_maximum:
            raise ValueError("recovery spacing bounds are reversed")
        if self.finalization_grace > self.finalization_maximum:
            raise ValueError("finalization grace must not exceed its maximum")

    @property
    def abrupt_drain_window(self) -> float:
        return max(self.abrupt_drain_minimum, 2.5 * self.online_poll_interval)

    @property
    def pending_strike_lifetime(self) -> float:
        return pending_strike_lifetime_seconds(self.online_poll_interval)


@dataclass(frozen=True)
class DrainSummary:
    baseline_players: int | None
    last_positive_at: float | None
    drain_at: float | None
    low_started_at: float | None
    zero_reached: bool
    minimum_players: int | None
    drop_fraction: float | None
    low_sample_count: int
    low_duration: float
    abrupt: bool
    baseline_at: float | None = None
    decline_duration: float = 0.0
    inherited: bool = False


@dataclass(frozen=True)
class OutageSummary:
    first_failure_at: float | None
    confirmed_offline_at: float | None
    failure_count: int
    failure_statuses: tuple[InfoStatus, ...]
    info_return_at: float | None


@dataclass(frozen=True)
class RecoverySummary:
    first_queue_at: float | None
    first_player_at: float | None
    stable_recovery_at: float | None
    positive_sample_count: int
    bounced_to_zero: bool


@dataclass(frozen=True)
class QueryHealthSummary:
    healthy_samples: int
    failed_samples: int
    missing_player_samples: int
    continuous: bool


@dataclass(frozen=True)
class PhysicalRestartEvent:
    event_id: str
    sequence: int
    fingerprint: str
    server_key: str
    episode_started_at: float
    finalized_at: float
    canonical_phase_at: float | None
    phase_uncertainty: float
    sources: frozenset[SignalSource]
    outcome: EventOutcome
    authenticity: float
    schedule_weight_suggestion: float
    drain: DrainSummary
    outage: OutageSummary
    recovery: RecoverySummary
    query_health: QueryHealthSummary
    coverage_complete: bool
    lifecycle_interruption: LifecycleMarker | None
    samples: tuple[ObservationSample, ...]
    reason_codes: tuple[str, ...]
    app_session_id: str | None = None
    monitoring_session_id: str | None = None
    poll_generation: int | None = None
    continuity_chain_id: str | None = None
    provenance_version: int = 0


@dataclass
class _Episode:
    sequence: int
    server_key: str
    app_session_id: str
    monitoring_session_id: str
    poll_generation: int
    started_wall: float
    started_mono: float
    fingerprint: str
    event_id: str
    continuity_chain_id: str | None = None
    provenance_version: int = 0
    baseline_players: int | None = None
    baseline_wall: float | None = None
    baseline_mono: float | None = None
    last_positive_wall: float | None = None
    last_positive_mono: float | None = None
    drain_wall: float | None = None
    drain_mono: float | None = None
    low_start_wall: float | None = None
    low_start_mono: float | None = None
    zero_reached: bool = False
    minimum_players: int | None = None
    drop_fraction: float | None = None
    abrupt_drain: bool = False
    drain_inherited: bool = False
    low_samples: list[float] = field(default_factory=list)
    low_sample_count: int = 0
    first_failure_wall: float | None = None
    first_failure_mono: float | None = None
    last_failure_mono: float | None = None
    failure_count: int = 0
    failure_statuses: list[InfoStatus] = field(default_factory=list)
    confirmed_offline_wall: float | None = None
    confirmed_offline_mono: float | None = None
    info_return_wall: float | None = None
    info_return_mono: float | None = None
    first_queue_wall: float | None = None
    first_queue_mono: float | None = None
    first_player_wall: float | None = None
    first_player_mono: float | None = None
    recovery_positive_times: list[float] = field(default_factory=list)
    recovery_positive_count: int = 0
    stable_recovery_wall: float | None = None
    stable_recovery_mono: float | None = None
    recovery_bounced: bool = False
    healthy_samples: int = 0
    failed_samples: int = 0
    missing_player_samples: int = 0
    coverage_complete: bool = True
    lifecycle_interruption: LifecycleMarker | None = None
    sources: set[SignalSource] = field(default_factory=set)
    samples: list[ObservationSample] = field(default_factory=list)
    reason_codes: set[str] = field(default_factory=set)
    recovery_snapshot_unchanged: bool = False


@dataclass
class _ProvisionalDrain:
    baseline_players: int
    baseline_wall: float
    baseline_mono: float
    last_positive_wall: float | None
    last_positive_mono: float | None
    low_start_wall: float
    low_start_mono: float
    minimum_players: int
    zero_reached: bool
    drop_fraction: float
    low_sample_count: int
    low_samples: list[float]
    abrupt: bool
    decline_duration: float
    expires_mono: float
    evidence_samples: list[ObservationSample]


class PhysicalEpisodeEngine:
    """Pure detector that clusters restart signals into one physical episode."""

    _EVENT_NAMESPACE = uuid.UUID("a448045b-aad1-48c1-94d2-8c9df43b1f75")
    _SAMPLE_DEDUP_CAP = 512

    def __init__(self, config: DetectionConfig | None = None):
        self.config = config or DetectionConfig()
        self.state = EpisodeState.IDLE
        self.pre_roll: list[ObservationSample] = []
        self._episode: _Episode | None = None
        self._sequence = 0
        self._seen_samples: set[tuple[object, ...]] = set()
        self._seen_sample_order: list[tuple[object, ...]] = []
        self._last_sample: ObservationSample | None = None
        self._pending_failure: ObservationSample | None = None
        self._provisional_drain: _ProvisionalDrain | None = None
        self._normal_since_mono: float | None = None
        self._cooldown_until_stable = False

    @property
    def active_episode(self) -> _Episode | None:
        return self._episode

    @property
    def provisional_drain(self) -> _ProvisionalDrain | None:
        return self._provisional_drain

    def ingest(self, sample: ObservationSample) -> tuple[PhysicalRestartEvent, ...]:
        key = _sample_key(sample)
        if key in self._seen_samples:
            return ()
        self._seen_samples.add(key)
        self._seen_sample_order.append(key)
        if len(self._seen_sample_order) > self._SAMPLE_DEDUP_CAP:
            self._seen_samples.discard(self._seen_sample_order.pop(0))
        completed: list[PhysicalRestartEvent] = []

        if sample.lifecycle is not LifecycleMarker.NORMAL:
            completed.extend(self._handle_lifecycle(sample))
            self._last_sample = sample
            return tuple(completed)

        if self._last_sample is not None and not self._same_continuity(sample, self._last_sample):
            if self._episode is not None:
                completed.append(
                    self._finalize(
                        EventOutcome.INCOMPLETE,
                        sample.wall_at,
                        "monitoring_continuity_changed",
                        lifecycle=LifecycleMarker.APP_RESTART,
                    )
                )
            self._reset_normal_history()

        if self._episode is not None:
            completed.extend(self.tick(sample.monotonic_at, sample.wall_at))
        if self._episode is not None:
            self._append_episode_sample(sample)
            completed.extend(self._consume_active(sample))
        else:
            self._consume_idle(sample)

        self._last_sample = sample
        return tuple(event for event in completed if event is not None)

    def tick(self, monotonic_at: float, wall_at: float | None = None) -> tuple[PhysicalRestartEvent, ...]:
        _require_finite_nonnegative("monotonic_at", monotonic_at)
        if self._last_sample is not None and monotonic_at < self._last_sample.monotonic_at:
            raise ValueError("tick time must not precede the last observation")
        if wall_at is None:
            if self._last_sample is None:
                wall_at = monotonic_at
            else:
                wall_at = self._last_sample.wall_at + (monotonic_at - self._last_sample.monotonic_at)
        _require_finite_nonnegative("wall_at", wall_at)
        episode = self._episode
        if episode is None:
            return ()
        if episode.stable_recovery_mono is not None:
            ready_at = min(
                episode.stable_recovery_mono + self.config.finalization_grace,
                (episode.first_player_mono or episode.info_return_mono or episode.stable_recovery_mono)
                + self.config.finalization_maximum,
            )
            if monotonic_at >= ready_at:
                return (self._finalize_classified(wall_at),)
        if (
            episode.low_start_mono is not None
            and episode.confirmed_offline_mono is None
            and monotonic_at - episode.low_start_mono > self.config.visible_recovery_absolute_maximum
        ):
            return (self._finalize(EventOutcome.EXPIRED, wall_at, "visible_recovery_timeout"),)
        return ()

    def flush(
        self,
        monotonic_at: float,
        wall_at: float | None = None,
        *,
        reason: str = "explicit_flush",
    ) -> tuple[PhysicalRestartEvent, ...]:
        events = list(self.tick(monotonic_at, wall_at))
        if self._episode is not None:
            resolved_wall = wall_at
            if resolved_wall is None:
                resolved_wall = self._last_sample.wall_at if self._last_sample else monotonic_at
            events.append(self._finalize(EventOutcome.INCOMPLETE, resolved_wall, reason))
        self._reset_normal_history()
        return tuple(events)

    def _consume_idle(self, sample: ObservationSample) -> None:
        if _is_qualifying_failure(sample):
            self._expire_provisional_drain(sample.monotonic_at)
            pending = self._pending_failure
            if (
                pending is None
                or not self._same_continuity(sample, pending)
                or not pending_strike_is_recent(
                    pending.monotonic_at,
                    sample.monotonic_at,
                    self.config.online_poll_interval,
                )
            ):
                self._pending_failure = sample
                self._normal_since_mono = None
                return
            self._pending_failure = None
            self._cooldown_until_stable = False
            self._open_episode(pending, "outage")
            self._inherit_provisional_drain(pending)
            self._record_failure(pending)
            self._append_episode_sample(sample)
            self._consume_active(sample)
            return

        if sample.info_status is InfoStatus.HEALTHY:
            self._pending_failure = None
            self._observe_player_history(sample)

        if self._cooldown_until_stable:
            if _is_healthy_normal(sample):
                if self._normal_since_mono is None:
                    self._normal_since_mono = sample.monotonic_at
                if sample.monotonic_at - self._normal_since_mono >= self.config.stable_normal_reset:
                    self._cooldown_until_stable = False
            else:
                self._normal_since_mono = None
            return

        if not _is_detector_evidence(sample):
            return

        return

    def _consume_active(self, sample: ObservationSample) -> list[PhysicalRestartEvent]:
        episode = self._episode
        assert episode is not None
        completed: list[PhysicalRestartEvent] = []

        if not _is_detector_evidence(sample):
            return completed

        if sample.info_status is InfoStatus.HEALTHY:
            episode.healthy_samples += 1
        elif _is_qualifying_failure(sample):
            episode.failed_samples += 1

        if sample.player_status is not FieldStatus.PRESENT:
            episode.missing_player_samples += 1
            if (
                sample.info_status is InfoStatus.HEALTHY
                and episode.low_start_mono is not None
                and episode.confirmed_offline_mono is None
            ):
                episode.coverage_complete = False
                episode.reason_codes.add("player_data_missing_during_visible_episode")

        if _is_qualifying_failure(sample):
            self._record_failure(sample)
            return completed

        if (
            episode.first_failure_mono is not None
            and episode.confirmed_offline_mono is None
            and episode.info_return_mono is None
        ):
            self._record_info_return(sample)
            episode.recovery_snapshot_unchanged = self._population_substantially_unchanged(sample)
            episode.stable_recovery_wall = sample.wall_at
            episode.stable_recovery_mono = sample.monotonic_at
            self.state = EpisodeState.RECOVERING
            return completed

        if episode.confirmed_offline_mono is not None and episode.info_return_mono is None:
            self._record_info_return(sample)
            episode.recovery_snapshot_unchanged = self._population_substantially_unchanged(sample)
            episode.stable_recovery_wall = sample.wall_at
            episode.stable_recovery_mono = sample.monotonic_at
            self.state = EpisodeState.RECOVERING

        self._record_low_or_recovery(sample)
        return completed

    def _record_failure(self, sample: ObservationSample) -> None:
        episode = self._episode
        assert episode is not None
        assert _is_qualifying_failure(sample)
        if episode.first_failure_mono is None:
            episode.first_failure_wall = sample.wall_at
            episode.first_failure_mono = sample.monotonic_at
            episode.last_failure_mono = sample.monotonic_at
            episode.failure_count = 1
            _append_bounded(episode.failure_statuses, sample.info_status, 16)
            episode.sources.add(SignalSource.INFO_OUTAGE)
            self.state = EpisodeState.OBSERVING
            return

        if episode.confirmed_offline_mono is not None:
            if sample.monotonic_at != episode.last_failure_mono:
                episode.failure_count += 1
                _append_bounded(episode.failure_statuses, sample.info_status, 16)
            episode.last_failure_mono = sample.monotonic_at
            return

        if not pending_strike_is_recent(
            episode.first_failure_mono,
            sample.monotonic_at,
            self.config.online_poll_interval,
        ):
            episode.first_failure_wall = sample.wall_at
            episode.first_failure_mono = sample.monotonic_at
            episode.last_failure_mono = sample.monotonic_at
            episode.failure_count = 1
            episode.failure_statuses[:] = [sample.info_status]
            episode.reason_codes.add("pending_offline_strike_expired")
            self.state = EpisodeState.OBSERVING
            return

        if sample.monotonic_at != episode.last_failure_mono:
            episode.failure_count += 1
            _append_bounded(episode.failure_statuses, sample.info_status, 16)
        episode.last_failure_mono = sample.monotonic_at
        if episode.failure_count >= 2:
            episode.confirmed_offline_wall = sample.wall_at
            episode.confirmed_offline_mono = sample.monotonic_at
            self.state = EpisodeState.OFFLINE

    def _record_info_return(self, sample: ObservationSample) -> None:
        episode = self._episode
        assert episode is not None
        episode.info_return_wall = sample.wall_at
        episode.info_return_mono = sample.monotonic_at
        episode.sources.add(SignalSource.INFO_RETURN)

    def _record_low_or_recovery(self, sample: ObservationSample) -> None:
        episode = self._episode
        assert episode is not None
        if sample.queue_status is FieldStatus.PRESENT and sample.queue and sample.queue > 0:
            if episode.first_queue_mono is None:
                episode.first_queue_wall = sample.wall_at
                episode.first_queue_mono = sample.monotonic_at
            episode.sources.add(SignalSource.QUEUE_RECOVERY)
            self.state = EpisodeState.RECOVERING

        if sample.player_status is not FieldStatus.PRESENT:
            return
        assert sample.players is not None
        low_limit = _low_limit(episode.baseline_players)
        positive_recovery = sample.players > 0 and (
            episode.first_queue_mono is not None
            or bool(episode.recovery_positive_times)
            or _has_confirmed_visible_low(episode, self.config)
        )
        if episode.drain_mono is not None and sample.players <= low_limit and not positive_recovery:
            if episode.low_start_mono is None:
                episode.low_start_wall = sample.wall_at
                episode.low_start_mono = sample.monotonic_at
            episode.low_sample_count += 1
            _append_bounded(episode.low_samples, sample.monotonic_at, 16)
            episode.minimum_players = min(episode.minimum_players or sample.players, sample.players)
            episode.zero_reached = episode.zero_reached or sample.players == 0
            episode.sources.add(SignalSource.VISIBLE_LOW)
            if episode.recovery_positive_times:
                episode.recovery_bounced = True
                episode.recovery_positive_times.clear()
                episode.recovery_positive_count = 0
                episode.stable_recovery_mono = None
                episode.stable_recovery_wall = None
                self.state = EpisodeState.OBSERVING
            return

        if episode.drain_mono is None:
            if (
                episode.confirmed_offline_mono is not None
                and sample.players > 0
                and not episode.recovery_snapshot_unchanged
            ):
                if episode.first_player_mono is None:
                    episode.first_player_wall = sample.wall_at
                    episode.first_player_mono = sample.monotonic_at
                episode.recovery_positive_count += 1
                _append_bounded(episode.recovery_positive_times, sample.monotonic_at, 8)
                episode.sources.add(SignalSource.PLAYER_RECOVERY)
            return
        if sample.players <= 0:
            return
        if episode.first_player_mono is None:
            episode.first_player_wall = sample.wall_at
            episode.first_player_mono = sample.monotonic_at
        if not episode.recovery_positive_times or sample.monotonic_at != episode.recovery_positive_times[-1]:
            episode.recovery_positive_count += 1
            _append_bounded(episode.recovery_positive_times, sample.monotonic_at, 8)
        episode.sources.add(SignalSource.PLAYER_RECOVERY)
        self.state = EpisodeState.RECOVERING
        required = 3 if episode.recovery_bounced else 2
        queue_alternative = (
            episode.first_queue_mono is not None
            and sample.monotonic_at - episode.first_queue_mono <= self.config.queue_to_player_maximum
        )
        stable = queue_alternative and not episode.recovery_bounced
        if len(episode.recovery_positive_times) >= required:
            recent = episode.recovery_positive_times[-required:]
            gaps = [b - a for a, b in zip(recent, recent[1:])]
            stable = all(
                self.config.recovery_spacing_minimum <= gap <= self.config.recovery_spacing_maximum
                for gap in gaps
            )
        if stable and episode.stable_recovery_mono is None:
            episode.stable_recovery_mono = sample.monotonic_at
            episode.stable_recovery_wall = sample.wall_at

    def _observe_player_history(self, sample: ObservationSample) -> None:
        self._remember_pre_roll(sample)
        self._expire_provisional_drain(sample.monotonic_at)
        if sample.player_status is not FieldStatus.PRESENT or sample.players is None:
            return

        provisional = self._provisional_drain
        if provisional is not None:
            recovery_level = math.ceil(
                provisional.baseline_players * self.config.drain_recovery_fraction
            )
            if sample.players >= recovery_level:
                self._provisional_drain = None
                return
            if sample.players <= _low_limit(provisional.baseline_players):
                if not provisional.low_samples or sample.monotonic_at != provisional.low_samples[-1]:
                    provisional.low_sample_count += 1
                    _append_bounded(provisional.low_samples, sample.monotonic_at, 32)
                provisional.minimum_players = min(provisional.minimum_players, sample.players)
                provisional.zero_reached = provisional.zero_reached or sample.players == 0
                provisional.drop_fraction = (
                    provisional.baseline_players - provisional.minimum_players
                ) / provisional.baseline_players
                provisional.evidence_samples = _compact_observation_samples(
                    [*provisional.evidence_samples, sample],
                    self.config.event_pre_roll_sample_cap,
                )
            elif sample.players > 0:
                provisional.last_positive_wall = sample.wall_at
                provisional.last_positive_mono = sample.monotonic_at
                provisional.evidence_samples = _compact_observation_samples(
                    [*provisional.evidence_samples, sample],
                    self.config.event_pre_roll_sample_cap,
                )
            return

        self._provisional_drain = self._provisional_drain_from_history(sample)

    def _provisional_drain_from_history(
        self, sample: ObservationSample
    ) -> _ProvisionalDrain | None:
        if sample.player_status is not FieldStatus.PRESENT or sample.players is None:
            return None
        known = [
            item
            for item in self.pre_roll
            if item.info_status is InfoStatus.HEALTHY
            and item.player_status is FieldStatus.PRESENT
            and item.players is not None
        ]
        if len(known) < self.config.drain_baseline_sample_minimum + self.config.drain_low_sample_minimum:
            return None

        positive_counts = sorted(item.players for item in known if item.players and item.players > 0)
        if len(positive_counts) < self.config.drain_baseline_sample_minimum:
            return None
        upper_half = positive_counts[len(positive_counts) // 2 :]
        baseline = max(1, int(round(statistics.median(upper_half))))
        if baseline < self.config.drain_baseline_minimum:
            return None

        low_limit = _low_limit(baseline)
        trailing_low: list[ObservationSample] = []
        for item in reversed(known):
            if item.players is not None and item.players <= low_limit:
                trailing_low.append(item)
                continue
            break
        trailing_low.reverse()
        if len(trailing_low) < self.config.drain_low_sample_minimum:
            return None

        low_start = trailing_low[0]
        if sample.monotonic_at - low_start.monotonic_at > self.config.drain_to_outage_merge:
            return None
        before_low = [item for item in known if item.monotonic_at < low_start.monotonic_at]
        baseline_support = [
            item
            for item in before_low
            if item.players is not None and item.players >= baseline * self.config.drain_recovery_fraction
        ]
        if len(baseline_support) < self.config.drain_baseline_sample_minimum:
            return None
        baseline_sample = baseline_support[-1]
        decline_duration = low_start.monotonic_at - baseline_sample.monotonic_at
        if not 0.0 <= decline_duration <= self.config.gradual_drain_maximum:
            return None

        minimum_players = min(item.players for item in trailing_low if item.players is not None)
        absolute_drop = baseline - minimum_players
        drop_fraction = absolute_drop / baseline
        if (
            absolute_drop < self.config.drain_absolute_drop_minimum
            or drop_fraction < self.config.drain_relative_drop_minimum
        ):
            return None
        last_positive = next(
            (
                item
                for item in reversed(before_low)
                if item.players is not None and item.players > 0
            ),
            None,
        )
        evidence = [item for item in self.pre_roll if item.monotonic_at >= baseline_sample.monotonic_at]
        return _ProvisionalDrain(
            baseline_players=baseline,
            baseline_wall=baseline_sample.wall_at,
            baseline_mono=baseline_sample.monotonic_at,
            last_positive_wall=last_positive.wall_at if last_positive else None,
            last_positive_mono=last_positive.monotonic_at if last_positive else None,
            low_start_wall=low_start.wall_at,
            low_start_mono=low_start.monotonic_at,
            minimum_players=minimum_players,
            zero_reached=any(item.players == 0 for item in trailing_low),
            drop_fraction=drop_fraction,
            low_sample_count=len(trailing_low),
            low_samples=[item.monotonic_at for item in trailing_low[-32:]],
            abrupt=decline_duration <= self.config.abrupt_drain_window,
            decline_duration=decline_duration,
            expires_mono=low_start.monotonic_at + self.config.drain_to_outage_merge,
            evidence_samples=_compact_observation_samples(
                evidence, self.config.event_pre_roll_sample_cap
            ),
        )

    def _expire_provisional_drain(self, monotonic_at: float) -> None:
        provisional = self._provisional_drain
        if provisional is not None and monotonic_at > provisional.expires_mono:
            self._provisional_drain = None

    def _inherit_provisional_drain(self, first_failure: ObservationSample) -> None:
        episode = self._episode
        assert episode is not None
        provisional = self._provisional_drain
        if provisional is None:
            return
        merge_age = first_failure.monotonic_at - provisional.low_start_mono
        if not 0.0 <= merge_age <= self.config.drain_to_outage_merge:
            self._provisional_drain = None
            return
        episode.baseline_players = provisional.baseline_players
        episode.baseline_wall = provisional.baseline_wall
        episode.baseline_mono = provisional.baseline_mono
        episode.last_positive_wall = provisional.last_positive_wall
        episode.last_positive_mono = provisional.last_positive_mono
        episode.drain_wall = provisional.low_start_wall
        episode.drain_mono = provisional.low_start_mono
        episode.low_start_wall = provisional.low_start_wall
        episode.low_start_mono = provisional.low_start_mono
        episode.zero_reached = provisional.zero_reached
        episode.minimum_players = provisional.minimum_players
        episode.drop_fraction = provisional.drop_fraction
        episode.abrupt_drain = provisional.abrupt
        episode.drain_inherited = True
        episode.low_sample_count = provisional.low_sample_count
        episode.low_samples[:] = provisional.low_samples
        episode.sources.update((SignalSource.PLAYER_DRAIN, SignalSource.VISIBLE_LOW))
        episode.reason_codes.add("inherited_provisional_player_drain")
        self._provisional_drain = None

    def _open_episode(self, sample: ObservationSample, trigger: str) -> None:
        self._sequence += 1
        landmark = sample.monotonic_at
        fingerprint = _event_fingerprint(
            sample.server_key,
            sample.monitoring_session_id,
            sample.wall_at,
            trigger,
            landmark,
        )
        event_id = str(uuid.uuid5(self._EVENT_NAMESPACE, fingerprint))
        self._episode = _Episode(
            sequence=self._sequence,
            server_key=sample.server_key,
            app_session_id=sample.app_session_id,
            monitoring_session_id=sample.monitoring_session_id,
            poll_generation=sample.poll_generation,
            started_wall=sample.wall_at,
            started_mono=sample.monotonic_at,
            fingerprint=fingerprint,
            event_id=event_id,
            continuity_chain_id=sample.continuity_chain_id,
            provenance_version=sample.provenance_version,
            samples=self._event_history_slice(),
        )
        if sample.info_status is InfoStatus.HEALTHY:
            self._episode.healthy_samples = 1
        else:
            self._episode.failed_samples = 1
        if sample.player_status is not FieldStatus.PRESENT:
            self._episode.missing_player_samples = 1
        self.state = EpisodeState.OBSERVING
        self._append_episode_sample(sample)

    def _event_history_slice(self) -> list[ObservationSample]:
        provisional = self._provisional_drain
        source = provisional.evidence_samples if provisional is not None else self.pre_roll
        return _compact_observation_samples(source, self.config.event_pre_roll_sample_cap)

    def _population_substantially_unchanged(self, sample: ObservationSample) -> bool:
        if sample.player_status is not FieldStatus.PRESENT or sample.players is None:
            return False
        episode = self._episode
        if episode is not None and episode.baseline_players is not None:
            baseline = float(episode.baseline_players)
            tolerance = max(1.0, baseline * 0.20)
            return abs(sample.players - baseline) <= tolerance
        positives = [item.players for item in self.pre_roll if item.players is not None and item.players > 0]
        if not positives:
            return False
        baseline = statistics.median(positives)
        tolerance = max(1.0, baseline * 0.20)
        return abs(sample.players - baseline) <= tolerance

    def _remember_pre_roll(self, sample: ObservationSample) -> None:
        if not _is_healthy_normal(sample):
            return
        self.pre_roll.append(sample)
        cutoff = sample.monotonic_at - self.config.pre_roll_seconds
        self.pre_roll = [item for item in self.pre_roll if item.monotonic_at >= cutoff]
        self.pre_roll = self.pre_roll[-self.config.pre_roll_sample_cap :]

    def _append_episode_sample(self, sample: ObservationSample) -> None:
        episode = self._episode
        assert episode is not None
        samples = episode.samples
        signature = _sample_signature(sample)
        if len(samples) >= 2 and _sample_signature(samples[-1]) == signature and _sample_signature(samples[-2]) == signature:
            samples[-1] = sample
        else:
            samples.append(sample)
        if len(samples) > self.config.active_sample_cap:
            samples[:] = [samples[0], *samples[-(self.config.active_sample_cap - 1) :]]

    def _handle_lifecycle(self, sample: ObservationSample) -> list[PhysicalRestartEvent]:
        completed: list[PhysicalRestartEvent] = []
        if self._episode is not None:
            self._append_episode_sample(sample)
            outcome = EventOutcome.AMBIGUOUS_DRAIN if self._episode.drain_mono is not None else EventOutcome.INCOMPLETE
            completed.append(
                self._finalize(outcome, sample.wall_at, f"lifecycle_{sample.lifecycle.value}", lifecycle=sample.lifecycle)
            )
        self._reset_normal_history()
        return completed

    def _finalize_classified(self, wall_at: float) -> PhysicalRestartEvent:
        episode = self._episode
        assert episode is not None
        if episode.confirmed_offline_mono is not None:
            corroborated = (
                episode.drain_mono is not None
                or episode.first_queue_mono is not None
                or bool(episode.recovery_positive_times)
            )
            if episode.recovery_snapshot_unchanged and not corroborated:
                outcome = EventOutcome.SERVICE_INTERRUPTION
            else:
                outcome = (
                    EventOutcome.CORROBORATED_OFFLINE_RESTART
                    if corroborated
                    else EventOutcome.CONFIRMED_OFFLINE_RESTART
                )
        elif episode.first_failure_mono is not None:
            outcome = EventOutcome.UNCERTAIN_A2S_INTERRUPTION
        else:
            outcome = self._visible_outcome()
        return self._finalize(outcome, wall_at, "recovery_finalized")

    def _visible_outcome(self) -> EventOutcome:
        episode = self._episode
        assert episode is not None
        confirmed_low = _has_confirmed_visible_low(episode, self.config)
        recovery_delay = (
            (episode.first_player_mono or episode.first_queue_mono or 0.0)
            - (episode.low_start_mono or 0.0)
        )
        baseline = episode.baseline_players or 0
        strong = (
            baseline >= 5
            and (episode.drop_fraction or 0.0) >= 0.90
            and confirmed_low
            and episode.stable_recovery_mono is not None
            and episode.coverage_complete
            and episode.failed_samples == 0
            and recovery_delay <= self.config.visible_recovery_strong_maximum
        )
        if strong:
            return EventOutcome.STRONG_QUERY_VISIBLE_RESTART
        probable = (
            baseline >= 3
            and confirmed_low
            and episode.stable_recovery_mono is not None
            and episode.coverage_complete
            and recovery_delay <= self.config.visible_recovery_absolute_maximum
        )
        if probable:
            return EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART
        return EventOutcome.AMBIGUOUS_DRAIN

    def _finalize(
        self,
        outcome: EventOutcome,
        wall_at: float,
        reason: str,
        *,
        lifecycle: LifecycleMarker | None = None,
    ) -> PhysicalRestartEvent:
        episode = self._episode
        assert episode is not None
        episode.reason_codes.add(reason)
        if lifecycle is not None:
            episode.lifecycle_interruption = lifecycle
            episode.coverage_complete = False
        authenticity = _authenticity(outcome, episode)
        canonical, uncertainty = _canonical_phase(outcome, episode, self.config)
        event = PhysicalRestartEvent(
            event_id=episode.event_id,
            sequence=episode.sequence,
            fingerprint=episode.fingerprint,
            server_key=episode.server_key,
            episode_started_at=episode.started_wall,
            finalized_at=float(wall_at),
            canonical_phase_at=canonical,
            phase_uncertainty=uncertainty,
            sources=frozenset(episode.sources),
            outcome=outcome,
            authenticity=authenticity,
            schedule_weight_suggestion=_schedule_weight_suggestion(outcome, authenticity),
            drain=DrainSummary(
                baseline_players=episode.baseline_players,
                last_positive_at=episode.last_positive_wall,
                drain_at=episode.drain_wall,
                low_started_at=episode.low_start_wall,
                zero_reached=episode.zero_reached,
                minimum_players=episode.minimum_players,
                drop_fraction=episode.drop_fraction,
                low_sample_count=episode.low_sample_count,
                low_duration=_low_duration(episode),
                abrupt=episode.abrupt_drain,
                baseline_at=episode.baseline_wall,
                decline_duration=(
                    max(0.0, episode.low_start_mono - episode.baseline_mono)
                    if episode.low_start_mono is not None and episode.baseline_mono is not None
                    else 0.0
                ),
                inherited=episode.drain_inherited,
            ),
            outage=OutageSummary(
                first_failure_at=episode.first_failure_wall,
                confirmed_offline_at=episode.confirmed_offline_wall,
                failure_count=episode.failure_count,
                failure_statuses=tuple(episode.failure_statuses),
                info_return_at=episode.info_return_wall,
            ),
            recovery=RecoverySummary(
                first_queue_at=episode.first_queue_wall,
                first_player_at=episode.first_player_wall,
                stable_recovery_at=episode.stable_recovery_wall,
                positive_sample_count=episode.recovery_positive_count,
                bounced_to_zero=episode.recovery_bounced,
            ),
            query_health=QueryHealthSummary(
                healthy_samples=episode.healthy_samples,
                failed_samples=episode.failed_samples,
                missing_player_samples=episode.missing_player_samples,
                continuous=episode.coverage_complete,
            ),
            coverage_complete=episode.coverage_complete,
            lifecycle_interruption=episode.lifecycle_interruption,
            samples=tuple(episode.samples),
            reason_codes=tuple(sorted(episode.reason_codes)),
            app_session_id=episode.app_session_id,
            monitoring_session_id=episode.monitoring_session_id,
            poll_generation=episode.poll_generation,
            continuity_chain_id=episode.continuity_chain_id,
            provenance_version=episode.provenance_version,
        )
        self._episode = None
        self.state = EpisodeState.IDLE
        self.pre_roll.clear()
        self._pending_failure = None
        self._provisional_drain = None
        self._cooldown_until_stable = outcome in {
            EventOutcome.CORROBORATED_OFFLINE_RESTART,
            EventOutcome.CONFIRMED_OFFLINE_RESTART,
            EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
            EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
        }
        self._normal_since_mono = None
        return event

    def _same_continuity(self, current: ObservationSample, previous: ObservationSample) -> bool:
        return (
            current.server_key == previous.server_key
            and current.app_session_id == previous.app_session_id
            and current.monitoring_session_id == previous.monitoring_session_id
            and current.poll_generation == previous.poll_generation
            and current.monotonic_at >= previous.monotonic_at
            and current.wall_at >= previous.wall_at
        )

    def _reset_normal_history(self) -> None:
        self.pre_roll.clear()
        self._pending_failure = None
        self._provisional_drain = None
        self._normal_since_mono = None
        self._cooldown_until_stable = False


def _authenticity(outcome: EventOutcome, episode: _Episode) -> float:
    if outcome is EventOutcome.CORROBORATED_OFFLINE_RESTART:
        score = 0.95
        if episode.drain_mono is not None:
            score += 0.02
        if episode.first_queue_mono is not None:
            score += 0.01
        if episode.recovery_positive_times:
            score += 0.01
        return min(1.0, round(score, 3))
    if outcome is EventOutcome.CONFIRMED_OFFLINE_RESTART:
        return 0.80 if episode.recovery_snapshot_unchanged else 0.85
    if outcome is EventOutcome.STRONG_QUERY_VISIBLE_RESTART:
        score = 0.84
        if (episode.baseline_players or 0) >= 10 and episode.zero_reached:
            score += 0.06
        if episode.first_queue_mono is not None:
            score += 0.03
        if len(episode.recovery_positive_times) >= 3:
            score += 0.02
        return min(0.95, round(score, 3))
    if outcome is EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART:
        baseline = episode.baseline_players or 0
        return 0.60 if baseline <= 4 else 0.68
    if outcome is EventOutcome.AMBIGUOUS_DRAIN:
        baseline = episode.baseline_players or 0
        return 0.25 if baseline <= 2 else 0.35
    if outcome is EventOutcome.UNCERTAIN_A2S_INTERRUPTION:
        return 0.10 if episode.recovery_snapshot_unchanged else 0.25
    if outcome is EventOutcome.SERVICE_INTERRUPTION:
        return 0.20
    if outcome is EventOutcome.EXPIRED:
        return 0.15
    return 0.10


def _schedule_weight_suggestion(outcome: EventOutcome, authenticity: float) -> float:
    if outcome in {
        EventOutcome.CORROBORATED_OFFLINE_RESTART,
        EventOutcome.CONFIRMED_OFFLINE_RESTART,
        EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
    }:
        return round(authenticity, 3)
    if outcome is EventOutcome.AMBIGUOUS_DRAIN:
        return min(0.15, round(authenticity, 3))
    return 0.0


def _canonical_phase(
    outcome: EventOutcome,
    episode: _Episode,
    config: DetectionConfig,
) -> tuple[float | None, float]:
    if episode.confirmed_offline_mono is not None and episode.info_return_wall is not None:
        return episode.info_return_wall, config.offline_poll_interval
    if outcome in {
        EventOutcome.STRONG_QUERY_VISIBLE_RESTART,
        EventOutcome.PROBABLE_QUERY_VISIBLE_RESTART,
    }:
        candidates = [
            value
            for value in (episode.first_queue_wall, episode.first_player_wall)
            if value is not None
        ]
        if candidates:
            return min(candidates), config.recovery_spacing_maximum
    candidates = [
        value
        for value in (episode.info_return_wall, episode.first_player_wall, episode.started_wall)
        if value is not None
    ]
    return (min(candidates), config.visible_recovery_absolute_maximum) if candidates else (None, 600.0)


def _event_fingerprint(
    server_key: str,
    monitoring_session_id: str,
    wall_at: float,
    trigger: str,
    landmark: float,
) -> str:
    return f"{server_key}|{monitoring_session_id}|{round(wall_at)}|{trigger}|{round(landmark, 3)}"


def _sample_key(sample: ObservationSample) -> tuple[object, ...]:
    return (
        sample.server_key,
        sample.app_session_id,
        sample.monitoring_session_id,
        sample.poll_generation,
        sample.monotonic_at,
        sample.wall_at,
        sample.info_status,
        sample.player_status,
        sample.players,
        sample.queue_status,
        sample.queue,
        sample.lifecycle,
    )


def _sample_signature(sample: ObservationSample) -> tuple[object, ...]:
    return (
        sample.info_status,
        sample.player_status,
        sample.players,
        sample.queue_status,
        sample.queue,
        sample.lifecycle,
    )


def _low_limit(baseline: int | None) -> int:
    if not baseline:
        return 1
    return max(1, math.floor(0.10 * baseline))


def _low_duration(episode: _Episode) -> float:
    if episode.low_start_mono is None:
        return 0.0
    endpoint = (
        episode.first_player_mono
        or episode.first_queue_mono
        or episode.stable_recovery_mono
        or (episode.low_samples[-1] if episode.low_samples else episode.low_start_mono)
    )
    return max(0.0, endpoint - episode.low_start_mono)


def _has_confirmed_visible_low(episode: _Episode, config: DetectionConfig) -> bool:
    if episode.low_start_mono is None:
        return False
    return (
        _low_duration(episode) >= config.low_state_minimum
        and episode.low_sample_count >= 3
        and any(value - episode.low_start_mono >= 30.0 for value in episode.low_samples)
    )


def _is_healthy_normal(sample: ObservationSample) -> bool:
    return sample.info_status is InfoStatus.HEALTHY and sample.lifecycle is LifecycleMarker.NORMAL


def _is_qualifying_failure(sample: ObservationSample) -> bool:
    return sample.info_status in {InfoStatus.TIMEOUT, InfoStatus.NETWORK_ERROR}


def _is_detector_evidence(sample: ObservationSample) -> bool:
    return sample.info_status is InfoStatus.HEALTHY or _is_qualifying_failure(sample)


def _append_bounded(values: list, value: object, limit: int) -> None:
    values.append(value)
    if len(values) > limit:
        values[1 : len(values) - limit + 1] = []


def _compact_observation_samples(
    values: list[ObservationSample], limit: int
) -> list[ObservationSample]:
    compacted: list[ObservationSample] = []
    for sample in values:
        signature = _sample_signature(sample)
        if (
            len(compacted) >= 2
            and _sample_signature(compacted[-1]) == signature
            and _sample_signature(compacted[-2]) == signature
        ):
            compacted[-1] = sample
        else:
            compacted.append(sample)
    if len(compacted) > limit:
        compacted[:] = [compacted[0], *compacted[-(limit - 1) :]]
    return compacted


def _validate_field(name: str, status: FieldStatus, value: int | None) -> None:
    if status is FieldStatus.PRESENT:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer when present")
    elif value is not None:
        raise ValueError(f"{name} must be null when its field is not present")


def _validate_optional_count(name: str, value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or null")


def _require_finite_nonnegative(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _require_finite_positive(name: str, value: object) -> None:
    _require_finite_nonnegative(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
