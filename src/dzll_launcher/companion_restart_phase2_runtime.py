from __future__ import annotations

import math
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Iterable

from .companion_restart_phase2_consumers import (
    CONSUMER_ADAPTER_VERSION,
    AlertKeyKind,
    BlockReason,
    CompatibilitySummary,
    ConsumerDecision,
    ConsumerModelStatus,
    ConsumerPolicyInput,
    EventPolicyContext,
    PredictionStatus,
    RecoveryAction,
    ScheduledEventClassification,
    SuppressionKey,
    compatibility_summary,
    evaluate_consumers,
)
from .companion_restart_phase2_detection import (
    DetectionConfig,
    DrainSummary,
    EpisodeState,
    EventOutcome,
    FieldStatus,
    InfoStatus,
    LifecycleMarker,
    ObservationSample,
    OutageSummary,
    PhysicalEpisodeEngine,
    PhysicalRestartEvent,
    QueryHealthSummary,
    RecoverySummary,
    SignalSource,
)
from .companion_restart_phase2_scoring import (
    AGGREGATE_SEMANTICS_VERSION,
    CANDIDATE_PERIODS,
    SCORING_SEMANTICS_VERSION,
    CandidateAggregate,
    CoveredExpectedMiss,
    CoverageKind,
    CoverageSegment,
    CoverageTimeline,
    LongTermAggregate,
    RegimeSummary,
    RegimeStatus,
    RestartScheduleScorer,
    ScheduleScore,
    candidate_phase_tolerance,
)
from .companion_restart_phase2_storage import (
    PHASE2_SCHEMA_VERSION,
    PHASE2_SCORING_ALGORITHM_VERSION,
    Phase2MigrationResult,
    atomic_write_json,
    initialize_phase2_state,
    normalize_phase2_state,
)


RUNTIME_STATE_VERSION = 1
MAX_FINALIZED_EVENTS = 80
MAX_COVERAGE_SEGMENTS = 120
MAX_FIRED_KEYS = 200
MAX_ROUTED_FINGERPRINTS = 160
MAX_INCOMPLETE_EPISODES = 30
MAX_MONITORING_SESSIONS = 120
PERSIST_HEARTBEAT_SECONDS = 60.0
COVERAGE_MAX_AGE_SECONDS = 60 * 24 * 3600


class RuntimePersistenceStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED_INITIALIZATION_FAILED = "disabled_initialization_failed"
    DISABLED_WRITE_FAILED = "disabled_write_failed"


@dataclass(frozen=True)
class RuntimeUpdate:
    accepted: bool
    finalized_events: tuple[PhysicalRestartEvent, ...] = ()
    decision: ConsumerDecision | None = None
    compatibility: CompatibilitySummary | None = None
    state_changed: bool = False
    persisted: bool = False
    rejected_reason: str | None = None


@dataclass(frozen=True)
class RuntimeNotice:
    kind: str
    title: str
    body: str
    backup_path: str | None = None


@dataclass
class _ServerRuntime:
    key: str
    engine: PhysicalEpisodeEngine
    events: list[PhysicalRestartEvent]
    coverage: list[CoverageSegment]
    fired_keys: set[SuppressionKey]
    routed_fingerprints: set[str]
    incumbent_period_seconds: int | None
    regime_generation: str
    aggregate: LongTermAggregate
    event_seq: int = 0
    folded_through_event_seq: int = 0
    prior_regimes: tuple[RegimeSummary, ...] = ()
    score: ScheduleScore | None = None
    decision: ConsumerDecision | None = None
    previous_sample: ObservationSample | None = None
    app_session_id: str = ""
    monitoring_session_id: str = ""
    poll_generation: int = 0
    dirty: bool = False
    last_saved_at: float = 0.0
    last_expected_miss_signature: tuple[tuple[int, int], ...] = ()
    incomplete_episodes: list[dict] | None = None
    extra_fields: dict = field(default_factory=dict)
    expected_misses: list[CoveredExpectedMiss] = field(default_factory=list)
    monitoring_sessions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.incomplete_episodes is None:
            self.incomplete_episodes = []


class Phase2RestartRuntime:
    """Application-facing coordinator for the pure Phase 2 components.

    Paths and clocks are explicit so integration tests never resolve or touch a
    user's real configuration.  The GTK window owns presentation and audio;
    this coordinator owns only restart state, evidence, and pure policy output.
    """

    def __init__(
        self,
        migration: Phase2MigrationResult,
        *,
        now: float | None = None,
        app_session_id: str | None = None,
        detection_config: DetectionConfig | None = None,
    ) -> None:
        self.migration = migration
        self.active_path = Path(migration.active_path)
        self.state = normalize_phase2_state(migration.state)
        self.app_session_id = app_session_id or str(uuid.uuid4())
        self.detection_config = detection_config or DetectionConfig()
        self.scorer = RestartScheduleScorer()
        self.persistence_status = (
            RuntimePersistenceStatus.ENABLED
            if migration.persistence_enabled
            else RuntimePersistenceStatus.DISABLED_INITIALIZATION_FAILED
        )
        self.persistence_error: str | None = migration.error
        self._servers: dict[str, _ServerRuntime] = {}
        self._shutdown = False
        self._load_servers(float(time.time() if now is None else now))

    @classmethod
    def initialize(
        cls,
        *,
        active_path: str | Path,
        legacy_path: str | Path,
        now: float | None = None,
        app_session_id: str | None = None,
        generation_id: str | None = None,
        detection_config: DetectionConfig | None = None,
    ) -> "Phase2RestartRuntime":
        migration = initialize_phase2_state(
            active_path=active_path,
            legacy_path=legacy_path,
            now=now,
            generation_id=generation_id,
        )
        return cls(
            migration,
            now=now,
            app_session_id=app_session_id,
            detection_config=detection_config,
        )

    @property
    def persistence_enabled(self) -> bool:
        return self.persistence_status is RuntimePersistenceStatus.ENABLED

    @property
    def pending_notice(self) -> RuntimeNotice | None:
        result = self.migration
        if result.reset_performed:
            return RuntimeNotice(
                kind="legacy_reset",
                title="Server Companion Restart Learning Reset",
                body=(
                    "DZLL has started fresh restart-cycle learning for Server Companion "
                    "because the previous learning format could produce unreliable "
                    "schedules. Your settings, favourites, alerts, and monitored-server "
                    "choice were not changed. The previous restart-learning data was "
                    "preserved in a backup."
                ),
                backup_path=str(result.backup_path) if result.backup_path else None,
            )
        if result.recovery_performed:
            return RuntimeNotice(
                kind="corrupt_state_recovered",
                title="Server Companion Restart Learning Recovered",
                body=(
                    "DZLL could not read the Phase 2 restart-learning file. The damaged "
                    "file was preserved for diagnosis and fresh learning was started."
                ),
                backup_path=str(result.backup_path) if result.backup_path else None,
            )
        if not result.persistence_enabled:
            return RuntimeNotice(
                kind="initialization_failed",
                title="Server Companion Restart Learning Unavailable",
                body=(
                    "DZLL could not safely initialize restart learning. Server browsing "
                    "and joining remain available, but restart learning is disabled for "
                    "this launch. Existing learning files were not used or deleted."
                ),
                backup_path=str(result.backup_path) if result.backup_path else None,
            )
        return None

    def begin_monitoring(
        self,
        server_key: str,
        *,
        wall_at: float,
        monotonic_at: float,
        poll_generation: int,
    ) -> str:
        if not self.persistence_enabled:
            return ""
        server = self._server(server_key)
        if server.monitoring_session_id:
            self.end_monitoring(
                server_key,
                marker=LifecycleMarker.SERVER_SWITCH,
                wall_at=wall_at,
                monotonic_at=monotonic_at,
            )
        server.engine = PhysicalEpisodeEngine(self.detection_config)
        server.app_session_id = self.app_session_id
        server.monitoring_session_id = str(uuid.uuid4())
        server.poll_generation = _nonnegative_int(poll_generation, 0)
        server.previous_sample = None
        server.monitoring_sessions.append({
            "session_id": server.monitoring_session_id,
            "app_session_id": server.app_session_id,
            "poll_generation": server.poll_generation,
            "started_at": wall_at,
            "ended_at": None,
            "reason": None,
        })
        server.monitoring_sessions[:] = server.monitoring_sessions[-MAX_MONITORING_SESSIONS:]
        server.dirty = True
        self._persist_server(server, force=True, now=wall_at)
        return server.monitoring_session_id

    def end_monitoring(
        self,
        server_key: str,
        *,
        marker: LifecycleMarker,
        wall_at: float,
        monotonic_at: float,
    ) -> RuntimeUpdate:
        server = self._servers.get(str(server_key))
        if server is None or not server.monitoring_session_id:
            return RuntimeUpdate(accepted=False, rejected_reason="no_active_monitoring_session")
        sample = ObservationSample(
            wall_at=wall_at,
            monotonic_at=monotonic_at,
            app_session_id=server.app_session_id,
            monitoring_session_id=server.monitoring_session_id,
            poll_generation=server.poll_generation,
            server_key=server.key,
            info_status=InfoStatus.MISSING,
            player_status=FieldStatus.MISSING,
            queue_status=FieldStatus.MISSING,
            lifecycle=marker,
        )
        update = self._ingest_sample(server, sample, persist_immediately=True)
        for session in reversed(server.monitoring_sessions):
            if session.get("session_id") == server.monitoring_session_id:
                session["ended_at"] = wall_at
                session["reason"] = marker.value
                break
        server.monitoring_session_id = ""
        server.previous_sample = None
        server.dirty = True
        persisted = self._persist_server(server, force=True, now=wall_at)
        return replace(update, persisted=update.persisted or persisted)

    def ingest_live_result(
        self,
        server_key: str,
        *,
        poll_generation: int,
        info: object,
        wall_at: float,
        monotonic_at: float,
    ) -> RuntimeUpdate:
        if not self.persistence_enabled:
            return RuntimeUpdate(accepted=False, rejected_reason="learning_disabled")
        server = self._servers.get(str(server_key))
        if server is None or not server.monitoring_session_id:
            return RuntimeUpdate(accepted=False, rejected_reason="no_active_monitoring_session")
        if poll_generation != server.poll_generation:
            return RuntimeUpdate(accepted=False, rejected_reason="stale_poll_generation")
        sample = observation_from_live_result(
            info,
            wall_at=wall_at,
            monotonic_at=monotonic_at,
            app_session_id=server.app_session_id,
            monitoring_session_id=server.monitoring_session_id,
            poll_generation=poll_generation,
            server_key=server.key,
        )
        return self._ingest_sample(server, sample)

    def ingest_sample(self, sample: ObservationSample) -> RuntimeUpdate:
        if not self.persistence_enabled:
            return RuntimeUpdate(accepted=False, rejected_reason="learning_disabled")
        server = self._servers.get(sample.server_key)
        if server is None or not server.monitoring_session_id:
            return RuntimeUpdate(accepted=False, rejected_reason="no_active_monitoring_session")
        if (
            sample.app_session_id != server.app_session_id
            or sample.monitoring_session_id != server.monitoring_session_id
            or sample.poll_generation != server.poll_generation
        ):
            return RuntimeUpdate(accepted=False, rejected_reason="stale_sample_continuity")
        return self._ingest_sample(server, sample)

    def tick(self, server_key: str, *, wall_at: float, monotonic_at: float) -> RuntimeUpdate:
        server = self._servers.get(str(server_key))
        if server is None:
            return RuntimeUpdate(accepted=False, rejected_reason="unknown_server")
        events = server.engine.tick(monotonic_at, wall_at)
        changed = bool(events)
        if events:
            events = self._route_finalized(server, events, now=wall_at)
        self._evaluate_expected_windows(server, now=wall_at)
        decision = self._evaluate(server, now=wall_at)
        persisted = self._persist_server(server, force=changed, now=wall_at)
        return RuntimeUpdate(
            accepted=True,
            finalized_events=events,
            decision=decision,
            compatibility=compatibility_summary(decision),
            state_changed=changed,
            persisted=persisted,
        )

    def decision(
        self,
        server_key: str,
        *,
        now: float,
        server_online_healthy: bool = True,
        restart_alert_enabled: bool = True,
        event: PhysicalRestartEvent | None = None,
        generic_duration_ok: bool = True,
    ) -> ConsumerDecision:
        server = self._server(server_key)
        return self._evaluate(
            server,
            now=now,
            server_online_healthy=server_online_healthy,
            restart_alert_enabled=restart_alert_enabled,
            event=event,
            generic_duration_ok=generic_duration_ok,
        )

    def compatibility(self, server_key: str, *, now: float) -> CompatibilitySummary:
        return compatibility_summary(self.decision(server_key, now=now))

    def mark_fired(self, server_key: str, key: SuppressionKey, *, now: float) -> bool:
        server = self._server(server_key)
        if key in server.fired_keys:
            return False
        server.fired_keys.add(key)
        if len(server.fired_keys) > MAX_FIRED_KEYS:
            server.fired_keys = set(
                sorted(server.fired_keys, key=lambda item: item.serialize())[-MAX_FIRED_KEYS:]
            )
        server.dirty = True
        self._persist_server(server, force=True, now=now)
        return True

    def event_suppression_key(
        self,
        server_key: str,
        *,
        kind: AlertKeyKind,
        event_id: str,
    ) -> SuppressionKey:
        server = self._server(server_key)
        selected = server.score.selected_period_seconds if server.score is not None else None
        return SuppressionKey(
            kind=kind,
            server_key=server.key,
            event_id=event_id,
            candidate_period_seconds=selected,
            regime_generation=server.regime_generation,
        )

    def provisional_event_id(self, server_key: str) -> str | None:
        server = self._servers.get(str(server_key))
        episode = server.engine.active_episode if server is not None else None
        return str(episode.event_id) if episode is not None else None

    def scheduled_outage_relaxation_usable(
        self, server_key: str, *, observed_at: float
    ) -> bool:
        server = self._servers.get(str(server_key))
        if server is None:
            return False
        decision = self._evaluate(server, now=observed_at)
        if (
            not decision.prediction_usable
            or decision.selected_period_seconds is None
            or server.score is None
        ):
            return False
        candidate = server.score.candidate(decision.selected_period_seconds)
        if candidate.phase_offset is None:
            return False
        residual = abs(
            (
                (observed_at - candidate.phase_offset + candidate.period_seconds / 2)
                % candidate.period_seconds
            )
            - candidate.period_seconds / 2
        )
        return residual <= candidate_phase_tolerance(candidate.period_seconds)

    def shutdown(self, *, wall_at: float, monotonic_at: float) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for key in tuple(self._servers):
            server = self._servers[key]
            if server.monitoring_session_id:
                self.end_monitoring(
                    key,
                    marker=LifecycleMarker.SHUTDOWN,
                    wall_at=wall_at,
                    monotonic_at=monotonic_at,
                )
            self._persist_server(server, force=True, now=wall_at)

    def _ingest_sample(
        self,
        server: _ServerRuntime,
        sample: ObservationSample,
        *,
        persist_immediately: bool = False,
    ) -> RuntimeUpdate:
        previous_state = server.engine.state
        previous_episode = _active_episode_signature(server.engine.active_episode)
        self._extend_coverage(server, sample)
        events = server.engine.ingest(sample)
        state_changed = (
            server.engine.state is not previous_state
            or _active_episode_signature(server.engine.active_episode) != previous_episode
            or bool(events)
            or sample.lifecycle is not LifecycleMarker.NORMAL
        )
        server.previous_sample = sample if sample.lifecycle is LifecycleMarker.NORMAL else None
        if events:
            events = self._route_finalized(server, events, now=sample.wall_at)
        if state_changed:
            server.dirty = True
        self._evaluate_expected_windows(server, now=sample.wall_at)
        decision = self._evaluate(
            server,
            now=sample.wall_at,
            server_online_healthy=sample.info_status is InfoStatus.HEALTHY,
        )
        current_misses = tuple(
            (item.period_seconds, item.covered_miss_count) for item in decision_score(server).candidates
        )
        misses_changed = (
            current_misses != server.last_expected_miss_signature
            and any(count > 0 for _period, count in current_misses)
        )
        if misses_changed:
            server.last_expected_miss_signature = current_misses
            server.dirty = True
        persisted = self._persist_server(
            server,
            force=persist_immediately or state_changed or misses_changed,
            now=sample.wall_at,
        )
        return RuntimeUpdate(
            accepted=True,
            finalized_events=events,
            decision=decision,
            compatibility=compatibility_summary(decision),
            state_changed=state_changed or misses_changed,
            persisted=persisted,
        )

    def _extend_coverage(self, server: _ServerRuntime, sample: ObservationSample) -> None:
        previous = server.previous_sample
        if previous is None or sample.lifecycle is not LifecycleMarker.NORMAL:
            return
        if (
            sample.app_session_id != previous.app_session_id
            or sample.monitoring_session_id != previous.monitoring_session_id
            or sample.poll_generation != previous.poll_generation
            or sample.server_key != previous.server_key
            or sample.wall_at <= previous.wall_at
        ):
            return
        elapsed = sample.wall_at - previous.wall_at
        previous_online = previous.info_status is InfoStatus.HEALTHY
        threshold = (
            max(30.0, 3.0 * self.detection_config.online_poll_interval)
            if previous_online
            else max(12.0, 4.0 * self.detection_config.offline_poll_interval)
        )
        if elapsed > threshold:
            kind = CoverageKind.SLEEP_GAP
        elif previous.info_status is InfoStatus.HEALTHY and sample.info_status is InfoStatus.HEALTHY:
            if previous.player_status is FieldStatus.PRESENT and sample.player_status is FieldStatus.PRESENT:
                kind = CoverageKind.ONLINE_HEALTHY
            else:
                kind = CoverageKind.MISSING_PLAYERS
        elif previous.info_status is not InfoStatus.HEALTHY and sample.info_status is not InfoStatus.HEALTHY:
            kind = CoverageKind.OFFLINE_OBSERVED
        else:
            # A healthy/failure boundary is timestamped by two real polls. Leave
            # the short edge unfilled so CoverageTimeline applies its cadence
            # gap limit; labelling the whole edge unhealthy would make every
            # genuine observed outage destroy otherwise continuous coverage.
            return
        cadence = (
            self.detection_config.online_poll_interval
            if previous_online
            else self.detection_config.offline_poll_interval
        )
        segment = CoverageSegment(previous.wall_at, sample.wall_at, kind, cadence)
        if (
            server.coverage
            and server.coverage[-1].kind is segment.kind
            and server.coverage[-1].end_at == segment.start_at
            and server.coverage[-1].cadence == segment.cadence
        ):
            prior = server.coverage[-1]
            server.coverage[-1] = CoverageSegment(
                prior.start_at, segment.end_at, segment.kind, segment.cadence
            )
        else:
            server.coverage.append(segment)
        cutoff = sample.wall_at - COVERAGE_MAX_AGE_SECONDS
        server.coverage = [item for item in server.coverage if item.end_at >= cutoff][
            -MAX_COVERAGE_SEGMENTS:
        ]
        server.dirty = True

    def _route_finalized(
        self,
        server: _ServerRuntime,
        events: Iterable[PhysicalRestartEvent],
        *,
        now: float,
    ) -> tuple[PhysicalRestartEvent, ...]:
        accepted: list[PhysicalRestartEvent] = []
        for event in events:
            if event.fingerprint in server.routed_fingerprints:
                continue
            server.event_seq += 1
            event = replace(event, sequence=server.event_seq)
            server.routed_fingerprints.add(event.fingerprint)
            server.events.append(event)
            accepted.append(event)
            if (
                event.outcome in {EventOutcome.INCOMPLETE, EventOutcome.EXPIRED}
                or event.lifecycle_interruption is not None
            ):
                server.incomplete_episodes.append(_event_diagnostic(event))
                server.incomplete_episodes[:] = server.incomplete_episodes[-MAX_INCOMPLETE_EPISODES:]
        if len(server.routed_fingerprints) > MAX_ROUTED_FINGERPRINTS:
            retained = {event.fingerprint for event in server.events[-MAX_FINALIZED_EVENTS:]}
            server.routed_fingerprints = retained
        if len(server.events) > MAX_FINALIZED_EVENTS:
            self._fold_old_events(server, now=now)
        if accepted:
            server.dirty = True
        return tuple(accepted)

    def _fold_old_events(self, server: _ServerRuntime, *, now: float) -> None:
        trim = len(server.events) - MAX_FINALIZED_EVENTS
        if trim <= 0:
            return
        old_events = tuple(server.events[:trim])
        old_score = self.scorer.score(
            old_events,
            CoverageTimeline(tuple(server.coverage)),
            now=now,
            aggregate=server.aggregate,
        )
        candidates = []
        for scored in old_score.candidates:
            prior = server.aggregate.candidate(scored.period_seconds)
            candidates.append(
                CandidateAggregate(
                    period_seconds=scored.period_seconds,
                    direct_count=prior.direct_count + scored.strict_direct_interval_count,
                    direct_weight=prior.direct_weight + scored.direct_support,
                    miss_count=prior.miss_count + scored.covered_miss_count,
                    miss_penalty=prior.miss_penalty + scored.covered_miss_penalty,
                    off_grid_count=prior.off_grid_count + scored.off_grid_event_count,
                    off_grid_penalty=prior.off_grid_penalty + scored.off_grid_penalty,
                    hint_count=prior.hint_count + scored.hints.event_count,
                    hint_weight=prior.hint_weight + scored.hints.weighted_alignment,
                    first_hint_at=_minimum_optional(prior.first_hint_at, scored.hints.first_at),
                    last_hint_at=_maximum_optional(prior.last_hint_at, scored.hints.last_at),
                    phase_vector_sin=prior.phase_vector_sin,
                    phase_vector_cos=prior.phase_vector_cos,
                    divisor_resolution_count=(
                        prior.divisor_resolution_count
                        + sum(item.covered_window_count for item in scored.divisor_resolution)
                    ),
                )
            )
        server.aggregate = LongTermAggregate(
            semantics_version=AGGREGATE_SEMANTICS_VERSION,
            candidates=tuple(candidates),
            source_quality_counts=server.aggregate.source_quality_counts,
            first_observation_at=_minimum_optional(
                server.aggregate.first_observation_at,
                min((_event_at(item) for item in old_events if _event_at(item) is not None), default=None),
            ),
            last_observation_at=_maximum_optional(
                server.aggregate.last_observation_at,
                max((_event_at(item) for item in old_events if _event_at(item) is not None), default=None),
            ),
            anomaly_count=server.aggregate.anomaly_count
            + sum(item.outcome in {EventOutcome.AMBIGUOUS_DRAIN, EventOutcome.UNCERTAIN_A2S_INTERRUPTION} for item in old_events),
            prior_regimes=server.aggregate.prior_regimes,
        )
        server.folded_through_event_seq = max(
            server.folded_through_event_seq,
            max((item.sequence for item in old_events), default=0),
        )
        server.events = server.events[trim:]

    def _evaluate(
        self,
        server: _ServerRuntime,
        *,
        now: float,
        server_online_healthy: bool = True,
        restart_alert_enabled: bool = True,
        event: PhysicalRestartEvent | None = None,
        generic_duration_ok: bool = True,
    ) -> ConsumerDecision:
        previous = server.score
        score = self.scorer.score(
            server.events,
            CoverageTimeline(tuple(server.coverage)),
            now=now,
            previous=previous,
            incumbent_period_seconds=server.incumbent_period_seconds,
            aggregate=server.aggregate,
            expected_misses=server.expected_misses,
        )
        if server.prior_regimes and not score.prior_regimes:
            score = replace(score, prior_regimes=server.prior_regimes)
        elif score.prior_regimes != server.prior_regimes:
            server.prior_regimes = score.prior_regimes
            server.dirty = True
        if score.selected_period_seconds != server.incumbent_period_seconds:
            if server.incumbent_period_seconds is not None:
                server.regime_generation = str(uuid.uuid4())
            server.incumbent_period_seconds = score.selected_period_seconds
            server.dirty = True
        server.score = score
        predicted = _next_prediction(score, now)
        context = (
            EventPolicyContext(event, server.regime_generation, generic_duration_ok)
            if event is not None
            else None
        )
        selected = None
        if score.selected_period_seconds is not None:
            selected = score.candidate(score.selected_period_seconds)
        decision = evaluate_consumers(
            ConsumerPolicyInput(
                score=score,
                server_key=server.key,
                regime_generation=server.regime_generation,
                now=now,
                independent_authentic_event_count=sum(
                    item.authenticity >= 0.50
                    and item.outcome not in {
                        EventOutcome.AMBIGUOUS_DRAIN,
                        EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
                        EventOutcome.INCOMPLETE,
                        EventOutcome.EXPIRED,
                        EventOutcome.SERVICE_INTERRUPTION,
                    }
                    for item in server.events
                ),
                predicted_restart_at=predicted,
                recent_covered_confirmation_count=(
                    selected.strict_direct_interval_count if selected is not None else 0
                ),
                server_online_healthy=server_online_healthy,
                restart_alert_enabled=restart_alert_enabled,
                fired_keys=frozenset(server.fired_keys),
                event_context=context,
                scorer_version=SCORING_SEMANTICS_VERSION,
                schema_version=PHASE2_SCHEMA_VERSION,
            )
        )
        server.decision = decision
        return decision

    def _evaluate_expected_windows(self, server: _ServerRuntime, *, now: float) -> None:
        score = server.score
        if score is None or not server.coverage:
            return
        timeline = CoverageTimeline(tuple(server.coverage))
        existing = {(item.period_seconds, item.key) for item in server.expected_misses}
        coverage_start = min(item.start_at for item in server.coverage)
        added = False
        for candidate in score.candidates:
            if (
                candidate.strict_direct_interval_count < 2
                or candidate.phase_offset is None
                or candidate.phase_confidence <= 0
            ):
                continue
            period = candidate.period_seconds
            tolerance = candidate_phase_tolerance(period)
            start = max(coverage_start, now - 7 * 24 * 3600)
            multiplier = math.ceil((start - candidate.phase_offset) / period)
            expected = candidate.phase_offset + multiplier * period
            checked = 0
            while expected + tolerance <= now and checked < 64:
                checked += 1
                key = f"{period}:{int(round(expected))}"
                identity = (period, key)
                if identity not in existing:
                    window_start = max(0.0, expected - tolerance)
                    window_end = expected + tolerance
                    assessment = timeline.assess(window_start, window_end)
                    events_inside = [
                        item for item in server.events
                        if _event_at(item) is not None
                        and window_start <= _event_at(item) <= window_end
                    ]
                    blocked = any(
                        item.outcome in {
                            EventOutcome.AMBIGUOUS_DRAIN,
                            EventOutcome.INCOMPLETE,
                            EventOutcome.EXPIRED,
                        }
                        for item in events_inside
                    )
                    active_episode = server.engine.active_episode
                    if (
                        active_episode is not None
                        and window_start <= active_episode.started_wall <= window_end
                    ):
                        blocked = True
                    observed = any(
                        item.outcome not in {
                            EventOutcome.AMBIGUOUS_DRAIN,
                            EventOutcome.UNCERTAIN_A2S_INTERRUPTION,
                            EventOutcome.INCOMPLETE,
                            EventOutcome.EXPIRED,
                            EventOutcome.SERVICE_INTERRUPTION,
                        }
                        and item.authenticity >= 0.50
                        for item in events_inside
                    )
                    if (
                        assessment.fully_covered
                        and assessment.query_health_adequate
                        and not assessment.unresolved_episode
                        and not blocked
                        and not observed
                    ):
                        server.expected_misses.append(
                            CoveredExpectedMiss(period, expected, key)
                        )
                        existing.add(identity)
                        added = True
                expected += period
        if added:
            ordered = sorted(
                server.expected_misses,
                key=lambda item: (item.expected_at, item.period_seconds, item.key),
            )
            if len(ordered) > 60:
                self._fold_expected_misses(server, ordered[:-60])
            server.expected_misses[:] = ordered[-60:]
            server.dirty = True

    def _fold_expected_misses(
        self, server: _ServerRuntime, misses: list[CoveredExpectedMiss]
    ) -> None:
        by_period: dict[int, list[CoveredExpectedMiss]] = {}
        for item in misses:
            by_period.setdefault(item.period_seconds, []).append(item)
        candidates = []
        for period in CANDIDATE_PERIODS:
            prior = server.aggregate.candidate(period)
            values = by_period.get(period, ())
            candidates.append(replace(
                prior,
                miss_count=prior.miss_count + len(values),
                miss_penalty=prior.miss_penalty + sum(item.weight for item in values),
            ))
        server.aggregate = replace(
            server.aggregate,
            candidates=tuple(candidates),
            last_observation_at=_maximum_optional(
                server.aggregate.last_observation_at,
                max((item.expected_at for item in misses), default=None),
            ),
        )

    def _persist_server(self, server: _ServerRuntime, *, force: bool, now: float) -> bool:
        if not self.persistence_enabled or not server.dirty:
            return False
        if not force and now - server.last_saved_at < PERSIST_HEARTBEAT_SECONDS:
            return False
        servers = self.state.setdefault("servers", {})
        servers[server.key] = _serialize_server(server)
        self.state["updated_at"] = max(int(now), int(self.state.get("created_at", 0) or 0))
        try:
            atomic_write_json(self.active_path, self.state)
        except Exception as exc:
            self.persistence_status = RuntimePersistenceStatus.DISABLED_WRITE_FAILED
            self.persistence_error = f"{type(exc).__name__}: {exc}"
            return False
        server.dirty = False
        server.last_saved_at = now
        return True

    def _server(self, key: str) -> _ServerRuntime:
        key = str(key or "").strip()
        if not key:
            raise ValueError("server key must be non-empty")
        if key not in self._servers:
            self._servers[key] = _new_server_runtime(key, self.detection_config)
        return self._servers[key]

    def _load_servers(self, now: float) -> None:
        raw_servers = self.state.get("servers")
        if not isinstance(raw_servers, dict):
            self.state["servers"] = {}
            return
        clean: dict[str, dict] = {}
        for raw_key, raw_record in raw_servers.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            try:
                server = _deserialize_server(key, raw_record, self.detection_config)
                server.score = self.scorer.score(
                    server.events,
                    CoverageTimeline(tuple(server.coverage)),
                    now=now,
                    incumbent_period_seconds=server.incumbent_period_seconds,
                    aggregate=server.aggregate,
                    expected_misses=server.expected_misses,
                )
                if server.prior_regimes and not server.score.prior_regimes:
                    server.score = replace(
                        server.score, prior_regimes=server.prior_regimes
                    )
                self._servers[key] = server
                clean[key] = _serialize_server(server)
            except Exception:
                # One malformed identity fails closed without discarding other
                # servers or exposing any persisted confidence as established.
                fallback = _new_server_runtime(key, self.detection_config)
                fallback.dirty = True
                self._servers[key] = fallback
                clean[key] = _serialize_server(fallback)
        self.state["servers"] = clean


def observation_from_live_result(
    info: object,
    *,
    wall_at: float,
    monotonic_at: float,
    app_session_id: str,
    monitoring_session_id: str,
    poll_generation: int,
    server_key: str,
) -> ObservationSample:
    payload = info if isinstance(info, dict) else {}
    healthy = payload.get("ok") is True
    if healthy:
        info_status = InfoStatus.HEALTHY
    else:
        error = str(payload.get("err") or payload.get("error") or "").lower()
        info_status = InfoStatus.TIMEOUT if "timeout" in error or "timed out" in error else InfoStatus.ERROR

    player_status, players = _field_from_payload(payload, "players", healthy=healthy)
    queue_status, queue = _field_from_payload(payload, "queue", healthy=healthy)
    max_players = _optional_count(payload.get("max_players")) if healthy else None
    latency = None
    if healthy:
        latency_ms = _finite_number(payload.get("ping_ms"))
        if latency_ms is not None and latency_ms >= 0:
            latency = latency_ms / 1000.0
    return ObservationSample(
        wall_at=wall_at,
        monotonic_at=monotonic_at,
        app_session_id=app_session_id,
        monitoring_session_id=monitoring_session_id,
        poll_generation=poll_generation,
        server_key=server_key,
        info_status=info_status,
        info_latency=latency,
        player_status=player_status,
        players=players,
        max_players=max_players,
        queue_status=queue_status,
        queue=queue,
    )


def _new_server_runtime(key: str, config: DetectionConfig) -> _ServerRuntime:
    return _ServerRuntime(
        key=key,
        engine=PhysicalEpisodeEngine(config),
        events=[],
        coverage=[],
        fired_keys=set(),
        routed_fingerprints=set(),
        incumbent_period_seconds=None,
        regime_generation=str(uuid.uuid4()),
        aggregate=LongTermAggregate(),
    )


def _serialize_server(server: _ServerRuntime) -> dict:
    active = server.engine.active_episode
    retained_events = server.events[-MAX_FINALIZED_EVENTS:]
    return {
        **server.extra_fields,
        "runtime_state_version": RUNTIME_STATE_VERSION,
        "scoring_semantics_version": SCORING_SEMANTICS_VERSION,
        "aggregate_semantics_version": AGGREGATE_SEMANTICS_VERSION,
        "events": [
            _serialize_event(item, include_samples=index >= max(0, len(retained_events) - 12))
            for index, item in enumerate(retained_events)
        ],
        "event_seq": server.event_seq,
        "folded_through_event_seq": server.folded_through_event_seq,
        "coverage_segments": [_serialize_coverage(item) for item in server.coverage[-MAX_COVERAGE_SEGMENTS:]],
        "monitoring_sessions": list(server.monitoring_sessions[-MAX_MONITORING_SESSIONS:]),
        "expected_window_misses": [
            {
                "period_seconds": item.period_seconds,
                "expected_at": item.expected_at,
                "key": item.key,
                "weight": item.weight,
            }
            for item in server.expected_misses[-60:]
        ],
        "fired_keys": sorted(item.serialize() for item in server.fired_keys)[-MAX_FIRED_KEYS:],
        "routed_fingerprints": sorted(server.routed_fingerprints)[-MAX_ROUTED_FINGERPRINTS:],
        "incumbent_period_seconds": server.incumbent_period_seconds,
        "regime_generation": server.regime_generation,
        "aggregate": _serialize_aggregate(server.aggregate),
        "prior_regimes": [asdict(item) for item in server.prior_regimes],
        "active_episode": (
            {
                "event_id": active.event_id,
                "fingerprint": active.fingerprint,
                "started_at": active.started_wall,
                "state": server.engine.state.value,
                "coverage_complete": active.coverage_complete,
            }
            if active is not None
            else None
        ),
        "incomplete_episodes": list(server.incomplete_episodes[-MAX_INCOMPLETE_EPISODES:]),
        "candidate_diagnostics": _serialize_score(server.score),
        "consumer_adapter_version": CONSUMER_ADAPTER_VERSION,
    }


def _deserialize_server(key: str, raw: object, config: DetectionConfig) -> _ServerRuntime:
    if not isinstance(raw, dict):
        raise ValueError("server record must be a mapping")
    events = []
    for item in raw.get("events") if isinstance(raw.get("events"), list) else []:
        try:
            events.append(_deserialize_event(item))
        except Exception:
            continue
    events = sorted(events, key=lambda item: (item.canonical_phase_at or item.episode_started_at, item.sequence))
    coverage = []
    for item in raw.get("coverage_segments") if isinstance(raw.get("coverage_segments"), list) else []:
        try:
            coverage.append(_deserialize_coverage(item))
        except Exception:
            continue
    fired = set()
    for item in raw.get("fired_keys") if isinstance(raw.get("fired_keys"), list) else []:
        try:
            fired.add(_deserialize_suppression_key(item))
        except Exception:
            continue
    fingerprints = {
        str(item) for item in (raw.get("routed_fingerprints") or [])
        if isinstance(item, str) and item
    }
    fingerprints.update(item.fingerprint for item in events)
    incumbent = raw.get("incumbent_period_seconds")
    if type(incumbent) is not int or incumbent not in CANDIDATE_PERIODS:
        incumbent = None
    generation = str(raw.get("regime_generation") or "").strip()
    if not generation:
        generation = str(uuid.uuid4())
    incomplete = raw.get("incomplete_episodes")
    if not isinstance(incomplete, list):
        incomplete = []
    known = {
        "runtime_state_version", "scoring_semantics_version", "aggregate_semantics_version",
        "events", "event_seq", "folded_through_event_seq", "coverage_segments",
        "expected_window_misses", "monitoring_sessions",
        "fired_keys", "routed_fingerprints", "incumbent_period_seconds",
        "regime_generation", "aggregate", "active_episode", "incomplete_episodes",
        "candidate_diagnostics", "consumer_adapter_version", "prior_regimes",
    }
    misses = []
    for item in raw.get("expected_window_misses") if isinstance(raw.get("expected_window_misses"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            misses.append(CoveredExpectedMiss(
                period_seconds=item.get("period_seconds"),
                expected_at=item.get("expected_at"),
                key=item.get("key"),
                weight=item.get("weight", 1.0),
            ))
        except Exception:
            continue
    sessions = []
    for item in raw.get("monitoring_sessions") if isinstance(raw.get("monitoring_sessions"), list) else []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        app_id = str(item.get("app_session_id") or "").strip()
        if not session_id or not app_id:
            continue
        sessions.append({
            "session_id": session_id,
            "app_session_id": app_id,
            "poll_generation": _nonnegative_int(item.get("poll_generation"), 0),
            "started_at": _safe_nonnegative_float(item.get("started_at"), 0.0),
            "ended_at": _optional_nonnegative_float(item.get("ended_at")),
            "reason": str(item.get("reason") or "") or None,
        })
    server = _ServerRuntime(
        key=key,
        engine=PhysicalEpisodeEngine(config),
        events=events[-MAX_FINALIZED_EVENTS:],
        coverage=sorted(coverage, key=lambda item: (item.start_at, item.end_at))[-MAX_COVERAGE_SEGMENTS:],
        fired_keys=fired,
        routed_fingerprints=fingerprints,
        incumbent_period_seconds=incumbent,
        regime_generation=generation,
        aggregate=_deserialize_aggregate(raw.get("aggregate")),
        event_seq=max(
            _nonnegative_int(raw.get("event_seq"), 0),
            max((item.sequence for item in events), default=0),
        ),
        folded_through_event_seq=_nonnegative_int(
            raw.get("folded_through_event_seq"), 0
        ),
        incomplete_episodes=[item for item in incomplete if isinstance(item, dict)][-MAX_INCOMPLETE_EPISODES:],
        extra_fields={name: value for name, value in raw.items() if name not in known},
        prior_regimes=_deserialize_prior_regimes(raw.get("prior_regimes")),
        expected_misses=misses[-60:],
        monitoring_sessions=sessions[-MAX_MONITORING_SESSIONS:],
    )
    server.folded_through_event_seq = min(
        server.folded_through_event_seq, server.event_seq
    )
    last_observed = max(
        (item.end_at for item in server.coverage),
        default=max((item.get("started_at", 0.0) for item in server.monitoring_sessions), default=0.0),
    )
    for session in server.monitoring_sessions:
        if session.get("ended_at") is None:
            session["ended_at"] = max(float(session.get("started_at", 0.0)), last_observed)
            session["reason"] = LifecycleMarker.APP_RESTART.value
            server.dirty = True
    active = raw.get("active_episode")
    if isinstance(active, dict):
        server.incomplete_episodes.append({
            "event_id": str(active.get("event_id") or ""),
            "fingerprint": str(active.get("fingerprint") or ""),
            "started_at": _safe_nonnegative_float(active.get("started_at"), 0.0),
            "outcome": EventOutcome.INCOMPLETE.value,
            "reason": "app_restart_boundary",
        })
        server.incomplete_episodes[:] = server.incomplete_episodes[-MAX_INCOMPLETE_EPISODES:]
        server.dirty = True
    return server


def _serialize_event(event: PhysicalRestartEvent, *, include_samples: bool = True) -> dict:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "fingerprint": event.fingerprint,
        "server_key": event.server_key,
        "episode_started_at": event.episode_started_at,
        "finalized_at": event.finalized_at,
        "canonical_phase_at": event.canonical_phase_at,
        "phase_uncertainty": event.phase_uncertainty,
        "sources": sorted(item.value for item in event.sources),
        "outcome": event.outcome.value,
        "authenticity": event.authenticity,
        "schedule_weight_suggestion": event.schedule_weight_suggestion,
        "drain": asdict(event.drain),
        "outage": {
            **asdict(event.outage),
            "failure_statuses": [item.value for item in event.outage.failure_statuses],
        },
        "recovery": asdict(event.recovery),
        "query_health": asdict(event.query_health),
        "coverage_complete": event.coverage_complete,
        "lifecycle_interruption": event.lifecycle_interruption.value if event.lifecycle_interruption else None,
        "samples": (
            [_serialize_sample(item) for item in event.samples[-48:]]
            if include_samples
            else []
        ),
        "reason_codes": list(event.reason_codes),
    }


def _deserialize_event(raw: object) -> PhysicalRestartEvent:
    if not isinstance(raw, dict):
        raise ValueError("event must be a mapping")
    drain = raw.get("drain") if isinstance(raw.get("drain"), dict) else {}
    outage = raw.get("outage") if isinstance(raw.get("outage"), dict) else {}
    recovery = raw.get("recovery") if isinstance(raw.get("recovery"), dict) else {}
    health = raw.get("query_health") if isinstance(raw.get("query_health"), dict) else {}
    samples = []
    for item in raw.get("samples") if isinstance(raw.get("samples"), list) else []:
        try:
            samples.append(_deserialize_sample(item))
        except Exception:
            continue
    lifecycle = raw.get("lifecycle_interruption")
    return PhysicalRestartEvent(
        event_id=_required_string(raw.get("event_id")),
        sequence=max(1, _nonnegative_int(raw.get("sequence"), 1)),
        fingerprint=_required_string(raw.get("fingerprint")),
        server_key=_required_string(raw.get("server_key")),
        episode_started_at=_safe_nonnegative_float(raw.get("episode_started_at"), 0.0),
        finalized_at=_safe_nonnegative_float(raw.get("finalized_at"), 0.0),
        canonical_phase_at=_optional_nonnegative_float(raw.get("canonical_phase_at")),
        phase_uncertainty=_safe_nonnegative_float(raw.get("phase_uncertainty"), 0.0),
        sources=frozenset(SignalSource(item) for item in raw.get("sources", []) if item in {value.value for value in SignalSource}),
        outcome=EventOutcome(raw.get("outcome")),
        authenticity=_bounded_float(raw.get("authenticity"), 0.0, 1.0),
        schedule_weight_suggestion=_bounded_float(raw.get("schedule_weight_suggestion"), 0.0, 1.0),
        drain=DrainSummary(
            baseline_players=_optional_count(drain.get("baseline_players")),
            last_positive_at=_optional_nonnegative_float(drain.get("last_positive_at")),
            drain_at=_optional_nonnegative_float(drain.get("drain_at")),
            low_started_at=_optional_nonnegative_float(drain.get("low_started_at")),
            zero_reached=bool(drain.get("zero_reached", False)),
            minimum_players=_optional_count(drain.get("minimum_players")),
            drop_fraction=_optional_bounded_float(drain.get("drop_fraction"), 0.0, 1.0),
            low_sample_count=_nonnegative_int(drain.get("low_sample_count"), 0),
            low_duration=_safe_nonnegative_float(drain.get("low_duration"), 0.0),
            abrupt=bool(drain.get("abrupt", False)),
        ),
        outage=OutageSummary(
            first_failure_at=_optional_nonnegative_float(outage.get("first_failure_at")),
            confirmed_offline_at=_optional_nonnegative_float(outage.get("confirmed_offline_at")),
            failure_count=_nonnegative_int(outage.get("failure_count"), 0),
            failure_statuses=tuple(
                InfoStatus(item) for item in outage.get("failure_statuses", [])
                if item in {value.value for value in InfoStatus}
            ),
            info_return_at=_optional_nonnegative_float(outage.get("info_return_at")),
        ),
        recovery=RecoverySummary(
            first_queue_at=_optional_nonnegative_float(recovery.get("first_queue_at")),
            first_player_at=_optional_nonnegative_float(recovery.get("first_player_at")),
            stable_recovery_at=_optional_nonnegative_float(recovery.get("stable_recovery_at")),
            positive_sample_count=_nonnegative_int(recovery.get("positive_sample_count"), 0),
            bounced_to_zero=bool(recovery.get("bounced_to_zero", False)),
        ),
        query_health=QueryHealthSummary(
            healthy_samples=_nonnegative_int(health.get("healthy_samples"), 0),
            failed_samples=_nonnegative_int(health.get("failed_samples"), 0),
            missing_player_samples=_nonnegative_int(health.get("missing_player_samples"), 0),
            continuous=bool(health.get("continuous", False)),
        ),
        coverage_complete=bool(raw.get("coverage_complete", False)),
        lifecycle_interruption=LifecycleMarker(lifecycle) if lifecycle in {item.value for item in LifecycleMarker} else None,
        samples=tuple(samples),
        reason_codes=tuple(str(item) for item in raw.get("reason_codes", []) if isinstance(item, str)),
    )


def _serialize_sample(sample: ObservationSample) -> dict:
    return {
        "wall_at": sample.wall_at,
        "monotonic_at": sample.monotonic_at,
        "app_session_id": sample.app_session_id,
        "monitoring_session_id": sample.monitoring_session_id,
        "poll_generation": sample.poll_generation,
        "server_key": sample.server_key,
        "info_status": sample.info_status.value,
        "info_latency": sample.info_latency,
        "player_status": sample.player_status.value,
        "players": sample.players,
        "max_players": sample.max_players,
        "queue_status": sample.queue_status.value,
        "queue": sample.queue,
        "lifecycle": sample.lifecycle.value,
    }


def _deserialize_sample(raw: object) -> ObservationSample:
    if not isinstance(raw, dict):
        raise ValueError("sample must be a mapping")
    return ObservationSample(
        wall_at=_safe_nonnegative_float(raw.get("wall_at"), 0.0),
        monotonic_at=_safe_nonnegative_float(raw.get("monotonic_at"), 0.0),
        app_session_id=_required_string(raw.get("app_session_id")),
        monitoring_session_id=_required_string(raw.get("monitoring_session_id")),
        poll_generation=_nonnegative_int(raw.get("poll_generation"), 0),
        server_key=_required_string(raw.get("server_key")),
        info_status=InfoStatus(raw.get("info_status")),
        info_latency=_optional_nonnegative_float(raw.get("info_latency")),
        player_status=FieldStatus(raw.get("player_status")),
        players=_optional_count(raw.get("players")),
        max_players=_optional_count(raw.get("max_players")),
        queue_status=FieldStatus(raw.get("queue_status")),
        queue=_optional_count(raw.get("queue")),
        lifecycle=LifecycleMarker(raw.get("lifecycle", LifecycleMarker.NORMAL.value)),
    )


def _serialize_coverage(segment: CoverageSegment) -> dict:
    return {
        "start_at": segment.start_at,
        "end_at": segment.end_at,
        "kind": segment.kind.value,
        "cadence": segment.cadence,
    }


def _deserialize_coverage(raw: object) -> CoverageSegment:
    if not isinstance(raw, dict):
        raise ValueError("coverage must be a mapping")
    return CoverageSegment(
        start_at=_safe_nonnegative_float(raw.get("start_at"), 0.0),
        end_at=_safe_nonnegative_float(raw.get("end_at"), 0.0),
        kind=CoverageKind(raw.get("kind")),
        cadence=max(0.001, _safe_nonnegative_float(raw.get("cadence"), 10.0)),
    )


def _serialize_aggregate(aggregate: LongTermAggregate) -> dict:
    return {
        "semantics_version": aggregate.semantics_version,
        "candidates": [asdict(item) for item in aggregate.candidates],
        "source_quality_counts": [list(item) for item in aggregate.source_quality_counts],
        "first_observation_at": aggregate.first_observation_at,
        "last_observation_at": aggregate.last_observation_at,
        "anomaly_count": aggregate.anomaly_count,
        "prior_regimes": [asdict(item) for item in aggregate.prior_regimes],
    }


def _deserialize_aggregate(raw: object) -> LongTermAggregate:
    if not isinstance(raw, dict) or raw.get("semantics_version") != AGGREGATE_SEMANTICS_VERSION:
        return LongTermAggregate()
    candidates = []
    lookup = {
        item.get("period_seconds"): item for item in raw.get("candidates", [])
        if isinstance(item, dict)
    }
    for period in CANDIDATE_PERIODS:
        item = lookup.get(period, {})
        candidates.append(CandidateAggregate(
            period_seconds=period,
            direct_count=_nonnegative_int(item.get("direct_count"), 0),
            direct_weight=_safe_nonnegative_float(item.get("direct_weight"), 0.0),
            miss_count=_nonnegative_int(item.get("miss_count"), 0),
            miss_penalty=_safe_nonnegative_float(item.get("miss_penalty"), 0.0),
            off_grid_count=_nonnegative_int(item.get("off_grid_count"), 0),
            off_grid_penalty=_safe_nonnegative_float(item.get("off_grid_penalty"), 0.0),
            hint_count=_nonnegative_int(item.get("hint_count"), 0),
            hint_weight=_safe_nonnegative_float(item.get("hint_weight"), 0.0),
            first_hint_at=_optional_nonnegative_float(item.get("first_hint_at")),
            last_hint_at=_optional_nonnegative_float(item.get("last_hint_at")),
            phase_vector_sin=_safe_finite_float(item.get("phase_vector_sin"), 0.0),
            phase_vector_cos=_safe_finite_float(item.get("phase_vector_cos"), 0.0),
            divisor_resolution_count=_nonnegative_int(item.get("divisor_resolution_count"), 0),
        ))
    source_counts = []
    for item in raw.get("source_quality_counts", []):
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
            source_counts.append((item[0], _nonnegative_int(item[1], 0)))
    return LongTermAggregate(
        semantics_version=AGGREGATE_SEMANTICS_VERSION,
        candidates=tuple(candidates),
        source_quality_counts=tuple(source_counts),
        first_observation_at=_optional_nonnegative_float(raw.get("first_observation_at")),
        last_observation_at=_optional_nonnegative_float(raw.get("last_observation_at")),
        anomaly_count=_nonnegative_int(raw.get("anomaly_count"), 0),
    )


def _deserialize_prior_regimes(raw: object) -> tuple[RegimeSummary, ...]:
    values = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        period = item.get("period_seconds")
        if type(period) is not int or period not in CANDIDATE_PERIODS:
            continue
        values.append(RegimeSummary(
            period_seconds=period,
            ended_at=_safe_nonnegative_float(item.get("ended_at"), 0.0),
            final_confidence=_bounded_float(item.get("final_confidence"), 0.0, 1.0),
            direct_interval_count=_nonnegative_int(item.get("direct_interval_count"), 0),
            reason=str(item.get("reason") or "historical_regime"),
        ))
    return tuple(values[-12:])


def _serialize_score(score: ScheduleScore | None) -> dict:
    if score is None:
        return {}
    return {
        "schedule_existence_confidence": score.schedule_existence_confidence,
        "selected_period_seconds": score.selected_period_seconds,
        "incumbent_period_seconds": score.incumbent_period_seconds,
        "pattern_status": score.pattern_status.value,
        "regime_status": score.regime_status.value,
        "prediction_usable": score.prediction_usable,
        "unresolved_competitors": list(score.unresolved_competitors),
        "recent_event_count": score.recent_event_count,
        "candidates": [
            {
                "period_seconds": item.period_seconds,
                "direct_support": item.direct_support,
                "strict_direct_interval_count": item.strict_direct_interval_count,
                "covered_miss_count": item.covered_miss_count,
                "covered_miss_penalty": item.covered_miss_penalty,
                "off_grid_penalty": item.off_grid_penalty,
                "fundamental_period_confidence": item.fundamental_period_confidence,
                "phase_confidence": item.phase_confidence,
                "establishment_gates_passed": item.establishment_gates_passed,
            }
            for item in score.candidates
        ],
    }


def _deserialize_suppression_key(value: object) -> SuppressionKey:
    if not isinstance(value, str):
        raise ValueError("suppression key must be a string")
    parts = value.split("|")
    if len(parts) != 6:
        raise ValueError("invalid suppression key")
    period = int(parts[3]) or None
    prediction = int(parts[5])
    return SuppressionKey(
        kind=AlertKeyKind(parts[0]),
        server_key=parts[1],
        event_id=None if parts[2] == "-" else parts[2],
        candidate_period_seconds=period,
        regime_generation=parts[4],
        prediction_at=None if prediction < 0 else prediction,
    )


def _event_diagnostic(event: PhysicalRestartEvent) -> dict:
    return {
        "event_id": event.event_id,
        "fingerprint": event.fingerprint,
        "started_at": event.episode_started_at,
        "finalized_at": event.finalized_at,
        "outcome": event.outcome.value,
        "reasons": list(event.reason_codes),
    }


def _active_episode_signature(episode: object) -> tuple[object, ...] | None:
    if episode is None:
        return None
    return (
        getattr(episode, "event_id", None),
        getattr(episode, "drain_wall", None),
        getattr(episode, "low_sample_count", None),
        getattr(episode, "failure_count", None),
        getattr(episode, "confirmed_offline_wall", None),
        getattr(episode, "info_return_wall", None),
        getattr(episode, "first_queue_wall", None),
        getattr(episode, "first_player_wall", None),
        getattr(episode, "stable_recovery_wall", None),
        getattr(episode, "coverage_complete", None),
    )


def _next_prediction(score: ScheduleScore, now: float) -> float | None:
    if score.selected_period_seconds is None:
        return None
    candidate = score.candidate(score.selected_period_seconds)
    if candidate.phase_offset is None:
        return None
    period = float(candidate.period_seconds)
    prediction = now - ((now - candidate.phase_offset) % period) + period
    return prediction if math.isfinite(prediction) and prediction > now else None


def decision_score(server: _ServerRuntime) -> ScheduleScore:
    if server.score is None:
        raise RuntimeError("server score was not evaluated")
    return server.score


def phase2_learning_summary(decision: ConsumerDecision | None, *, now: float) -> dict | None:
    if decision is None or decision.model_status is ConsumerModelStatus.NO_PATTERN:
        return None
    confidence = decision.fundamental_period_confidence
    cycle = decision.selected_period_seconds
    prediction = decision.prediction_at if decision.prediction_usable else None
    if decision.model_status is ConsumerModelStatus.PATTERN_OBSERVED:
        cycle_text = "Pattern observed"
    elif decision.model_status is ConsumerModelStatus.PERIOD_UNCERTAIN:
        cycle_text = "Period uncertain"
    elif decision.model_status is ConsumerModelStatus.SCHEDULE_CHANGE_SUSPECTED:
        cycle_text = "Schedule change suspected"
    elif decision.model_status is ConsumerModelStatus.NEW_REGIME_ESTABLISHING:
        cycle_text = "New schedule learning"
    elif cycle:
        prefix = "Likely " if decision.model_status is ConsumerModelStatus.LIKELY_PERIOD else ""
        cycle_text = f"{prefix}{cycle / 3600:g} hours"
    else:
        cycle_text = "Period uncertain"
    if prediction is None:
        next_text = "Prediction suspended"
        countdown = "--"
    else:
        next_text = time.strftime("%a %H:%M", time.localtime(prediction))
        remaining = max(0, int(prediction - now))
        countdown = f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}"
    return {
        "confidence_percent": int(round(confidence * 100)),
        "cycle_text": cycle_text,
        "next_text": next_text,
        "countdown_text": countdown,
        "model_status": decision.model_status.value,
        "prediction_usable": decision.prediction_usable,
        "phase2": True,
    }


def phase2_alert_usability(decision: ConsumerDecision | None) -> dict:
    if decision is None:
        return {"usable": False, "mode": "", "message": "Restart learning is unavailable."}
    usable = decision.prediction_usable
    if usable:
        return {"usable": True, "mode": "phase2", "message": "Restart schedule established."}
    reasons = ", ".join(item.value.replace("_", " ") for item in decision.prediction_reasons[:2])
    return {
        "usable": False,
        "mode": "phase2",
        "message": f"Restart alerts are learning: {reasons or 'more covered observations needed'}."
    }


def _field_from_payload(payload: dict, name: str, *, healthy: bool) -> tuple[FieldStatus, int | None]:
    if not healthy or name not in payload or payload.get(name) is None:
        return FieldStatus.MISSING, None
    value = payload.get(name)
    if isinstance(value, bool):
        return FieldStatus.INVALID, None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return FieldStatus.INVALID, None
    if parsed < 0:
        return FieldStatus.INVALID, None
    return FieldStatus.PRESENT, parsed


def _event_at(event: PhysicalRestartEvent) -> float | None:
    return event.canonical_phase_at


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("required string is missing")
    return value


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _safe_finite_float(value: object, default: float) -> float:
    parsed = _finite_number(value)
    return default if parsed is None else parsed


def _safe_nonnegative_float(value: object, default: float) -> float:
    parsed = _finite_number(value)
    return default if parsed is None or parsed < 0 else parsed


def _optional_nonnegative_float(value: object) -> float | None:
    parsed = _finite_number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _bounded_float(value: object, low: float, high: float) -> float:
    parsed = _finite_number(value)
    return low if parsed is None else min(high, max(low, parsed))


def _optional_bounded_float(value: object, low: float, high: float) -> float | None:
    parsed = _finite_number(value)
    return None if parsed is None else min(high, max(low, parsed))


def _nonnegative_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _optional_count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _minimum_optional(left: float | None, right: float | None) -> float | None:
    values = [item for item in (left, right) if item is not None]
    return min(values) if values else None


def _maximum_optional(left: float | None, right: float | None) -> float | None:
    values = [item for item in (left, right) if item is not None]
    return max(values) if values else None
